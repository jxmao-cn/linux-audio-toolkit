"""The long-running auto-connect and battery daemon."""

from __future__ import annotations

import json
import logging
import os
import signal
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import __version__
from .audio import AudioController
from .backend import Backend
from .config import Config
from .errors import AirPodsError
from .models import AudioStatus, BatterySnapshot, Device
from .notify import Notifier
from .state import clear_state, write_state
from .ui import format_battery

logger = logging.getLogger(__name__)


class Daemon:
    """Periodic state machine driving auto-connect, audio and battery.

    ``tick()`` is deliberately free of GLib and sleeps, so it can be unit
    tested with a fake clock and the mock backend.
    """

    def __init__(
        self,
        backend: Backend,
        config: Config,
        audio: Optional[AudioController] = None,
        notifier: Optional[Notifier] = None,
        state_path: Optional[Path] = None,
        clock=time.monotonic,
    ) -> None:
        self.backend = backend
        self.config = config
        self.audio = audio if audio is not None else AudioController()
        self.notifier = notifier if notifier is not None else Notifier(config.notifications)
        self.state_path = state_path
        self._clock = clock
        self._last_scan = float("-inf")
        self._last_connect = float("-inf")
        self._last_battery = float("-inf")
        self._last_audio = float("-inf")
        self._last_audio_attempt = float("-inf")
        self._discovery_until = 0.0
        self._was_connected = False
        self._audio_checked = False
        self._busy = False
        self._battery: Optional[BatterySnapshot] = None
        self._audio: Optional[AudioStatus] = None
        self._notified: set[tuple[str, int]] = set()
        self._last_state = ""
        self._started_at = datetime.now(timezone.utc)
        self._warned_multiple = False
        self._loop = None

    # -- selection --------------------------------------------------------

    def _select(self, devices: list[Device]) -> Optional[Device]:
        if self.config.address:
            for device in devices:
                if device.address.upper() == self.config.address:
                    return device
            return None
        candidates = [device for device in devices if device.is_airpods]
        if not candidates:
            return None
        if len(candidates) > 1 and not self._warned_multiple:
            logger.warning(
                "multiple AirPods found; set `address` in config.json to pick one "
                "(using %s)",
                candidates[0].address,
            )
            self._warned_multiple = True
        candidates.sort(key=lambda device: (not device.connected, not device.paired))
        return candidates[0]

    def _has_candidate(self, devices: list[Device]) -> bool:
        if self.config.address:
            return True
        return any(device.is_airpods for device in devices)

    # -- main loop step ---------------------------------------------------

    def tick(self) -> None:
        now = self._clock()
        try:
            devices = self.backend.devices()
        except AirPodsError as exc:
            logger.warning("cannot enumerate Bluetooth devices: %s", exc)
            return

        device = self._select(devices)
        if device is None:
            self._battery = None
            self._audio = None
            self._was_connected = False
            if self._discovery_until and now >= self._discovery_until:
                self._stop_scan()
            if self._has_candidate(devices):
                self._maybe_scan(now)
            self._write_state(None)
            return

        if device.connected:
            self._handle_connected(device, now)
        else:
            self._handle_disconnected(device, now)
        self._write_state(device)

    def _handle_connected(self, device: Device, now: float) -> None:
        self._stop_scan()
        if not self._was_connected:
            self._on_connected(device, now)
            self._was_connected = True
        if now - self._last_battery >= self.config.battery_interval:
            self._poll_battery(device, now)
        if (
            not self._audio_checked
            and self.config.audio_profile != "keep"
            and now - self._last_audio_attempt >= 15.0
        ):
            self._last_audio_attempt = now
            self._apply_audio(device, wait=3.0)
        if now - self._last_audio >= 30.0:
            self._write_audio(device)
            self._last_audio = now

    def _handle_disconnected(self, device: Device, now: float) -> None:
        if self._was_connected:
            logger.info("%s disconnected", device.display_name)
            self.notifier.send(
                "AirPods disconnected", f"{device.display_name} is no longer connected"
            )
            self._was_connected = False
            self._battery = None
            self._audio = None
            self._audio_checked = False
            self._notified.clear()

        if not self.config.auto_connect:
            return

        if now < self._discovery_until:
            return
        self._stop_scan()

        if now - self._last_scan >= self.config.scan_interval:
            self._start_scan(now)
            return

        if now - self._last_connect < self.config.connect_interval:
            return
        if not (device.paired or device.trusted):
            return

        logger.info("connecting to %s", device.display_name)
        self._last_connect = now
        try:
            if device.paired and not device.trusted:
                self.backend.set_trusted(device.address, True)
            self.backend.connect(device.address)
            self.backend.wait_connected(device.address, timeout=20.0)
        except AirPodsError as exc:
            logger.warning("auto-connect failed: %s", exc)

    def _maybe_scan(self, now: float) -> None:
        if not self.config.auto_connect:
            return
        if now < self._discovery_until:
            return
        self._stop_scan()
        if now - self._last_scan < self.config.scan_interval:
            return
        self._start_scan(now)

    def _start_scan(self, now: float) -> None:
        try:
            self.backend.start_discovery()
            self._last_scan = now
            self._discovery_until = now + self.config.scan_duration
            logger.debug("scanning for %ss", self.config.scan_duration)
        except AirPodsError as exc:
            logger.warning("could not start scanning: %s", exc)

    def _stop_scan(self) -> None:
        if not self._discovery_until:
            return
        self._discovery_until = 0.0
        try:
            self.backend.stop_discovery()
        except AirPodsError as exc:
            logger.warning("could not stop scanning: %s", exc)

    # -- actions ----------------------------------------------------------

    def _on_connected(self, device: Device, now: float) -> None:
        logger.info("%s connected", device.display_name)
        self._battery = None
        try:
            self._battery = self.backend.battery(device.address)
        except AirPodsError as exc:
            logger.info("battery not available yet: %s", exc)
        body = (
            format_battery(self._battery)
            if self._battery and self._battery.available
            else "Connected"
        )
        self.notifier.send(f"{device.display_name} connected", body)
        self._last_battery = now
        self._last_audio = float("-inf")
        self._last_audio_attempt = now
        self._audio_checked = False
        self._apply_audio(device, wait=12.0)

    def _poll_battery(self, device: Device, now: float) -> None:
        self._last_battery = now
        try:
            snapshot = self.backend.battery(device.address)
        except AirPodsError as exc:
            logger.info("battery poll failed: %s", exc)
            return
        if not snapshot.available:
            return
        self._battery = snapshot
        logger.debug("battery: %s", format_battery(snapshot))
        self._notify_low_battery(snapshot)

    def _notify_low_battery(self, snapshot: BatterySnapshot) -> None:
        critical = self.config.critical_battery_threshold
        low = self.config.low_battery_threshold
        for cell in snapshot.cells:
            if not cell.valid:
                continue
            level = int(cell.level)
            critical_key = (cell.label, critical)
            low_key = (cell.label, low)
            if level <= critical:
                self._notified.discard(low_key)
                if critical_key not in self._notified:
                    self._notified.add(critical_key)
                    self.notifier.send(
                        f"Critically low: {cell.label}",
                        f"{cell.label.title()} battery is at {level}%",
                        urgency="critical",
                    )
            elif level <= low:
                if low_key not in self._notified:
                    self._notified.add(low_key)
                    self.notifier.send(
                        f"Low battery: {cell.label}",
                        f"{cell.label.title()} battery is at {level}%",
                    )
            else:
                if level > critical + 5:
                    self._notified.discard(critical_key)
                if level > low + 5:
                    self._notified.discard(low_key)

    def _apply_audio(self, device: Device, wait: float = 12.0) -> None:
        if self.config.audio_profile == "keep":
            return
        try:
            card = self.audio.wait_for_card(device.address, timeout=wait)
            if card is None:
                return
            card = self.audio.set_profile(device.address, self.config.audio_profile)
            profile = card.active_profile
            if self.config.set_default_sink:
                sink = self.audio.set_default_sink(device.address)
            else:
                sink = None
            if self.config.audio_profile == "hfp":
                source = self.audio.set_default_source(device.address)
                if not source:
                    logger.info(
                        "audio profile %s selected; waiting for AirPods input source",
                        profile,
                    )
                    return
                logger.info(
                    "audio profile %s selected; default input source is %s",
                    profile,
                    source,
                )
            else:
                logger.info(
                    "audio profile %s selected%s",
                    profile,
                    f"; default output sink is {sink}" if sink else "",
                )
            self._audio_checked = True
        except AirPodsError as exc:
            logger.warning("could not configure audio: %s", exc)

    def _write_audio(self, device: Device) -> None:
        if self._audio_checked:
            try:
                self._audio = self.audio.status(device.address)
            except AirPodsError:
                logger.debug("audio status failed", exc_info=True)

    # -- state ------------------------------------------------------------

    def _write_state(self, device: Optional[Device]) -> None:
        payload = {
            "daemon": {
                "pid": os.getpid(),
                "started_at": self._started_at.isoformat(),
            },
            "device": device.to_dict() if device else None,
            "battery": self._battery.to_dict() if self._battery else None,
            "audio": self._audio.to_dict() if self._audio else None,
            "discovering": bool(
                self._discovery_until and self._clock() < self._discovery_until
            ),
        }
        serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        if serialized == self._last_state:
            return
        try:
            write_state(payload, path=self.state_path)
            self._last_state = serialized
        except OSError as exc:
            logger.warning("cannot write state file: %s", exc)

    # -- process lifecycle ------------------------------------------------

    def run(self) -> None:
        from gi.repository import GLib

        logger.info("airpodsd %s starting", __version__)
        try:
            self.tick()
        except Exception:  # pragma: no cover - keep the daemon alive
            logger.exception("initial tick failed")

        loop = GLib.MainLoop()
        self._loop = loop
        timer = GLib.timeout_add_seconds(2, self._on_timer)
        for signum in (signal.SIGINT, signal.SIGTERM):
            GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signum, self._quit)
        logger.info("watching for AirPods (Ctrl-C to stop)")
        try:
            loop.run()
        finally:
            try:
                GLib.source_remove(timer)
            except Exception:
                pass
            self.backend.close()
            if self.state_path is None:
                clear_state()
            else:
                try:
                    Path(self.state_path).unlink()
                except OSError:
                    pass
            logger.info("stopped")

    def _on_timer(self) -> bool:
        if self._busy:
            # Connect() runs a nested GLib loop; do not let the timeout source
            # re-enter the state machine while that is in flight.
            return True
        self._busy = True
        try:
            self.tick()
        except Exception:  # pragma: no cover - defensive
            logger.exception("tick failed")
        finally:
            self._busy = False
        return True

    def _quit(self, *_args) -> bool:
        logger.info("shutting down")
        if self._loop is not None:
            self._loop.quit()
        return False
