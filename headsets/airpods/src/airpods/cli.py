"""Command line interface: ``airpodsctl`` and ``airpodsd``."""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from pathlib import Path
from typing import Optional, Sequence

from . import __version__
from .audio import AudioController
from .backend import Backend
from .config import Config
from .constants import APP_NAME
from .doctor import Check, format_report, run_doctor
from .errors import AirPodsError, DeviceNotFound
from .mock import MockAudio, MockBackend
from .models import AudioStatus, BatterySnapshot, Device
from .notify import Notifier
from .state import read_state
from .ui import (
    battery_bar,
    bold,
    cell_label,
    cyan,
    dim,
    format_audio,
    format_battery,
    green,
    red,
    set_color_enabled,
    yellow,
)

logger = logging.getLogger(__name__)

_DEV_PATH_RE = re.compile(r"dev_([0-9A-Fa-f]{2}(?:_[0-9A-Fa-f]{2}){5})")


# -- helpers --------------------------------------------------------------


def _configure_logging(args: argparse.Namespace, config: Config) -> None:
    if args.verbose:
        level = logging.DEBUG
    elif args.quiet:
        level = logging.ERROR
    elif args.command == "daemon":
        level = getattr(logging, config.log_level.upper(), logging.INFO)
    else:
        level = logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def _make_backend(args: argparse.Namespace, config: Config) -> Backend:
    if args.mock:
        return MockBackend()
    from .bluez import BlueZBackend

    return BlueZBackend(adapter=config.adapter, battery_labels=config.battery_labels)


def _make_audio(args: argparse.Namespace) -> AudioController:
    if args.mock:
        return MockAudio()  # type: ignore[return-value]
    return AudioController()


def _path_address(path: str) -> str:
    match = _DEV_PATH_RE.search(path)
    return match.group(1).replace("_", ":").upper() if match else ""


def _error(message: str, hint: Optional[str] = None) -> None:
    print(f"{red('error:')} {message}", file=sys.stderr)
    if hint:
        print(f"       {dim(hint)}", file=sys.stderr)


def _select_device(
    backend: Backend, config: Config, address: Optional[str] = None
) -> Optional[Device]:
    if address:
        return backend.device(address)
    devices = backend.devices()
    if config.address:
        for device in devices:
            if device.address.upper() == config.address:
                return device
    candidates = [device for device in devices if device.is_airpods]
    if not candidates:
        return None
    candidates.sort(key=lambda device: (not device.connected, not device.paired))
    return candidates[0]


def _require_device(
    backend: Backend, config: Config, address: Optional[str] = None
) -> Device:
    device = _select_device(backend, config, address)
    if device is None:
        raise DeviceNotFound(
            "no AirPods were found",
            hint="run `airpodsctl pair` while the AirPods are in pairing mode",
        )
    return device


def _scan(
    backend: Backend,
    seconds: float,
    include_all: bool = False,
    announce: bool = True,
    transport: str = "auto",
) -> list[Device]:
    if announce:
        print(f"Scanning for {seconds:g}s...")
        print(
            dim(
                "Put the AirPods in pairing mode: open the case, keep the buds inside.\n"
                "AirPods 1-3 / Pro / Max: hold the case button. "
                "AirPods 4 / 4 ANC: double-tap the front of the case below the LED.\n"
                "Wait for the light to flash white."
            )
        )
    found: dict[str, Device] = {}
    backend.start_discovery(transport=transport)
    deadline = time.monotonic() + seconds
    try:
        while time.monotonic() < deadline:
            for device in backend.devices():
                if include_all or device.is_airpods:
                    found[device.address] = device
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(0.8, remaining))
    except KeyboardInterrupt:
        print()
    finally:
        backend.stop_discovery()
    devices = list(found.values())
    devices.sort(key=lambda device: (-(device.rssi or -999), device.address))
    return devices


def _print_device_table(devices: Sequence[Device]) -> None:
    for index, device in enumerate(devices, start=1):
        rssi = f"{device.rssi} dBm" if device.rssi is not None else "—"
        status = green("connected") if device.connected else dim("not connected")
        apple = dim(" [Apple]") if device.apple else ""
        print(f"  {index}. {bold(device.display_name)}  {device.address}  {rssi}  {status}{apple}")


def _choose_device(devices: Sequence[Device], assume_yes: bool) -> Device:
    if not devices:
        raise DeviceNotFound(
            "no AirPods were found",
            hint=(
                "keep the buds in the case with the lid open. AirPods 4 / 4 ANC: "
                "double-tap the front of the case just below the LED; other models: "
                "hold the case button until the light flashes white"
            ),
        )
    if len(devices) == 1:
        return devices[0]
    print()
    _print_device_table(devices)
    if assume_yes:
        raise AirPodsError(
            "more than one AirPods device was found",
            hint="pass --address AA:BB:CC:DD:EE:FF",
        )
    if not sys.stdin.isatty():
        raise AirPodsError(
            "more than one AirPods device was found",
            hint="pass --address AA:BB:CC:DD:EE:FF",
        )
    while True:
        try:
            answer = input(f"Select device [1-{len(devices)}] (1): ").strip() or "1"
        except EOFError as exc:
            raise AirPodsError(
                "no input is available to select a device",
                hint="pass --address AA:BB:CC:DD:EE:FF",
            ) from exc
        try:
            index = int(answer)
        except ValueError:
            continue
        if 1 <= index <= len(devices):
            return devices[index - 1]


def _print_battery(snapshot: BatterySnapshot) -> None:
    for cell in snapshot.cells:
        level = f"{cell.level:>3}%" if cell.valid else " -- "
        print(f"  {cell_label(cell.label):<6} {level}  {battery_bar(cell.level)}")


def _read_battery(backend: Backend, device: Device) -> Optional[BatterySnapshot]:
    try:
        return backend.battery(device.address)
    except AirPodsError as exc:
        logger.info("battery unavailable: %s", exc)
        return None


def _apply_audio(
    audio: AudioController, config: Config, device: Device, wait: float = 12.0
) -> Optional[AudioStatus]:
    if config.audio_profile == "keep":
        return None
    card = audio.wait_for_card(device.address, timeout=wait)
    if card is None:
        return None
    audio.set_profile(device.address, config.audio_profile)
    if config.set_default_sink:
        audio.set_default_sink(device.address)
    return audio.status(device.address)


# -- commands -------------------------------------------------------------


def cmd_scan(args: argparse.Namespace, config: Config) -> int:
    if args.timeout <= 0:
        raise AirPodsError("--timeout must be greater than zero")
    backend = _make_backend(args, config)
    try:
        devices = _scan(
            backend, args.timeout, include_all=args.all, transport=args.transport
        )
    finally:
        backend.close()

    if not devices:
        print()
        print(yellow("No AirPods found."))
        print("  • Put the buds in the case and open the lid.")
        print("  • AirPods 4 / 4 ANC: double-tap the front of the case, just below the LED.")
        print("  • AirPods 1-3 / Pro / Max: hold the case button until the light flashes white.")
        print("  • Make sure Bluetooth is on: `bluetoothctl show`.")
        return 1

    print()
    print(f"{len(devices)} device(s) found:")
    _print_device_table(devices)
    print()
    if any(device.is_airpods for device in devices):
        print(f"Next: {cyan('airpodsctl pair')}")
    return 0


def cmd_pair(args: argparse.Namespace, config: Config) -> int:
    if args.timeout <= 0:
        raise AirPodsError("--timeout must be greater than zero")
    backend = _make_backend(args, config)
    try:
        device = None
        if args.address:
            if args.transport == "bredr":
                known = backend.device(args.address)
                if known is not None and not known.paired:
                    # BlueZ keeps cached LE/Find My entries in its object list;
                    # remove the stale one so a BR/EDR-only scan cannot match a
                    # device that was not actually seen over classic Bluetooth.
                    try:
                        backend.remove(args.address)
                        logger.info("removed stale entry for %s", args.address)
                    except AirPodsError as exc:
                        logger.debug("could not remove stale entry: %s", exc)
            device = backend.device(args.address)
            if device is None or args.transport == "bredr":
                # An AirPods name may not have resolved yet, and a BR/EDR
                # refresh is needed when old BlueZ cached the LE bearer.
                devices = _scan(
                    backend,
                    args.timeout,
                    include_all=True,
                    transport=args.transport,
                )
                device = next(
                    (
                        item
                        for item in devices
                        if item.address.upper() == args.address.upper()
                    ),
                    None,
                )
        else:
            devices = _scan(backend, args.timeout, transport=args.transport)
            device = _choose_device(devices, args.yes)

        if device is None:
            raise DeviceNotFound(
                f"no AirPods with address {args.address} were found",
                hint="run `airpodsctl scan` while the AirPods are in pairing mode",
            )

        print()
        print(f"Pairing with {bold(device.display_name)} ({device.address})...")
        backend.register_agent(lambda path: _path_address(path) == device.address.upper())
        backend.pair(device.address)
        backend.set_trusted(device.address, True)

        config.address = device.address
        saved = True
        try:
            config.save()
        except OSError as exc:
            saved = False
            logger.warning("could not save config: %s", exc)

        print("Paired. Connecting...")
        backend.connect(device.address)
        if not backend.wait_connected(device.address, timeout=20.0):
            print(
                yellow("Paired, but the connection did not come up yet.")
                + " It will usually connect when audio starts."
            )
            print(f"Retry with {cyan('airpodsctl connect')}.")
            return 1

        print(f"{green('Connected.')}  {device.display_name}")
        try:
            audio_status = _apply_audio(_make_audio(args), config, device)
            if audio_status:
                print(f"Audio profile: {format_audio(audio_status)}")
        except AirPodsError as exc:
            logger.warning("audio setup failed: %s", exc)

        snapshot = _read_battery(backend, device)
        if snapshot and snapshot.available:
            print(f"Battery: {format_battery(snapshot)}")
        print()
        if saved:
            print(f"Default device saved to {config.path}")
        else:
            print(yellow("Paired, but the default device could not be saved."))
        return 0
    finally:
        backend.close()


def cmd_connect(args: argparse.Namespace, config: Config) -> int:
    backend = _make_backend(args, config)
    try:
        device = _require_device(backend, config, args.address)
        if not device.paired:
            raise AirPodsError(
                f"{device.display_name} is not paired yet",
                hint="run `airpodsctl pair` first",
            )
        if device.connected:
            print(f"{green('Already connected.')}  {device.model}")
            return 0

        print(f"Connecting to {bold(device.display_name)} ({device.address})...")
        if not device.trusted:
            backend.set_trusted(device.address, True)
        backend.register_agent(lambda path: _path_address(path) == device.address.upper())
        backend.connect(device.address)
        if not backend.wait_connected(device.address, timeout=25.0):
            raise AirPodsError(
                "the connection did not complete",
                hint=(
                    "take the AirPods out of the case, keep them near the computer, "
                    "and make sure they are not connected to an iPhone"
                ),
            )

        print(f"{green('Connected.')}  {device.display_name}")
        try:
            audio_status = _apply_audio(_make_audio(args), config, device)
            if audio_status:
                print(f"Audio profile: {format_audio(audio_status)}")
        except AirPodsError as exc:
            print(yellow(f"Audio setup warning: {exc}"))
            if exc.hint:
                print(f"  {exc.hint}")

        snapshot = _read_battery(backend, device)
        if snapshot and snapshot.available:
            print(f"Battery: {format_battery(snapshot)}")

        if not config.address:
            config.address = device.address
            try:
                config.save()
            except OSError:
                pass
        return 0
    finally:
        backend.close()


def cmd_disconnect(args: argparse.Namespace, config: Config) -> int:
    backend = _make_backend(args, config)
    try:
        device = _require_device(backend, config, args.address)
        if not device.connected:
            print(f"{device.display_name} is already disconnected.")
            return 0
        backend.disconnect(device.address)
        print(f"Disconnected {device.display_name}.")
        return 0
    finally:
        backend.close()


def cmd_forget(args: argparse.Namespace, config: Config) -> int:
    backend = _make_backend(args, config)
    try:
        device = _require_device(backend, config, args.address)
        backend.remove(device.address)
        if config.address and config.address.upper() == device.address.upper():
            config.address = None
            try:
                config.save()
            except OSError as exc:
                logger.warning("could not update config: %s", exc)
        print(f"Removed {device.model} ({device.address}).")
        return 0
    finally:
        backend.close()


def cmd_status(args: argparse.Namespace, config: Config) -> int:
    backend = _make_backend(args, config)
    try:
        device = _select_device(backend, config, args.address)
        if device is None:
            if args.json:
                print(json.dumps({"device": None}, indent=2))
            else:
                print("No AirPods are paired.")
                print(f"  Run {cyan('airpodsctl pair')} to set them up.")
            return 1

        audio = _make_audio(args)
        battery = _read_battery(backend, device) if device.connected else None
        audio_status = None
        if device.connected:
            try:
                audio_status = audio.status(device.address)
            except AirPodsError:
                logger.debug("audio status failed", exc_info=True)

        state = read_state() or {}
        payload = {
            "device": device.to_dict(),
            "battery": battery.to_dict() if battery else None,
            "audio": audio_status.to_dict() if audio_status else None,
            "daemon_updated_at": state.get("updated_at"),
        }
        if args.json:
            print(json.dumps(payload, indent=2, ensure_ascii=False))
            return 0 if device.connected else 1

        print(f"{bold(device.display_name)}")
        print(f"  {'Address':<10} {device.address}")
        if device.adapter:
            print(f"  {'Adapter':<10} {device.adapter.rsplit('/', 1)[-1]}")
        if device.connected:
            rssi = f" · {device.rssi} dBm" if device.rssi is not None else ""
            print(f"  {'Link':<10} {green('connected')}{rssi}")
            print(f"  {'Audio':<10} {format_audio(audio_status)}")
            print(f"  {'Battery':<10} {format_battery(battery) if battery else 'unavailable'}")
            if state.get("updated_at"):
                print(f"  {'Daemon':<10} {dim('last update ' + str(state['updated_at']))}")
        else:
            print(f"  {'Link':<10} {yellow('not connected')}")
            print(f"  {'Hint':<10} run {cyan('airpodsctl connect')}")
        return 0 if device.connected else 1
    finally:
        backend.close()


def cmd_battery(args: argparse.Namespace, config: Config) -> int:
    if args.watch and args.json:
        raise AirPodsError("--watch cannot be combined with --json")
    if args.interval < 1:
        raise AirPodsError("--interval must be at least 1 second")
    backend = _make_backend(args, config)
    try:
        device = _require_device(backend, config, args.address)
        if not device.connected:
            raise AirPodsError(
                f"{device.model} is not connected",
                hint="run `airpodsctl connect` first",
            )

        if args.watch:
            try:
                while True:
                    if sys.stdout.isatty():
                        print("\033[2J\033[H", end="")
                    snapshot = backend.battery(device.address)
                    print(f"{bold(device.model)}  {dim(time.strftime('%H:%M:%S'))}")
                    _print_battery(snapshot)
                    print(dim("\nCtrl-C to stop"))
                    time.sleep(args.interval)
            except KeyboardInterrupt:
                print()
                return 0

        snapshot = backend.battery(device.address)
        if args.json:
            print(json.dumps(snapshot.to_dict(), indent=2, ensure_ascii=False))
            return 0
        print(f"{bold(device.display_name)}")
        _print_battery(snapshot)
        print(f"  {dim('source: ' + snapshot.source)}")
        return 0
    finally:
        backend.close()


def cmd_audio(args: argparse.Namespace, config: Config) -> int:
    backend = _make_backend(args, config)
    try:
        device = _require_device(backend, config, args.address)
        if not device.connected:
            raise AirPodsError(
                f"{device.model} is not connected",
                hint="run `airpodsctl connect` first",
            )
        audio = _make_audio(args)
        if args.action in ("a2dp", "hfp"):
            card = audio.wait_for_card(device.address, timeout=12.0)
            if card is None:
                raise AirPodsError(
                    "the Bluetooth audio card did not appear",
                    hint="run `airpodsctl doctor` and check the audio server",
                )
            audio.set_profile(device.address, args.action)
            if args.action == "a2dp" and config.set_default_sink:
                audio.set_default_sink(device.address)
            if args.action == "hfp":
                audio.set_default_source(device.address)

        status = audio.status(device.address)
        if args.json:
            print(json.dumps(status.to_dict(), indent=2, ensure_ascii=False))
            return 0
        print(f"{bold(device.display_name)}")
        print(f"  {'Profile':<10} {format_audio(status)}")
        if status.card:
            print(f"  {'Card':<10} {status.card}")
        if status.source:
            print(f"  {'Source':<10} {status.source}")
        return 0
    finally:
        backend.close()


def cmd_doctor(args: argparse.Namespace, config: Config) -> int:
    backend = None
    backend_error: Optional[AirPodsError] = None
    try:
        backend = _make_backend(args, config)
    except AirPodsError as exc:
        backend_error = exc
    try:
        report = run_doctor(backend=backend, audio=_make_audio(args), config=config)
        if backend_error is not None:
            report.checks.insert(
                0, Check("Bluetooth backend", "fail", backend_error.message, backend_error.hint or "")
            )
        print(format_report(report))
        return report.exit_code
    finally:
        if backend is not None:
            backend.close()


def cmd_daemon(args: argparse.Namespace, config: Config) -> int:
    from .daemon import Daemon

    backend = _make_backend(args, config)
    daemon = Daemon(
        backend=backend,
        config=config,
        audio=_make_audio(args),
        notifier=Notifier(config.notifications),
    )
    if args.once:
        try:
            daemon.tick()
            print("tick complete")
            return 0
        finally:
            backend.close()
    daemon.run()
    return 0


def cmd_config(args: argparse.Namespace, config: Config) -> int:
    if args.action == "path":
        print(config.path)
        return 0
    if args.action == "show":
        print(json.dumps(config.to_dict(), indent=2, ensure_ascii=False))
        print(f"# {config.path}", file=sys.stderr)
        return 0
    if config.path.exists() and not args.force:
        raise AirPodsError(
            f"{config.path} already exists",
            hint="use --force to overwrite it",
        )
    try:
        path = config.save()
    except OSError as exc:
        raise AirPodsError(f"cannot write {config.path}: {exc}") from exc
    print(f"Wrote {path}")
    return 0


def cmd_version(_args: argparse.Namespace, _config: Config) -> int:
    print(f"{APP_NAME} {__version__}")
    return 0


# -- argument parser ------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="airpodsctl",
        description="A userspace AirPods driver for Linux (BlueZ + PulseAudio/PipeWire).",
        epilog="Run `airpodsctl doctor` if pairing does not work.",
    )
    parser.add_argument("--mock", action="store_true", help="use a simulated AirPods (demos/tests)")
    parser.add_argument(
        "--config", type=Path, help="config file (default: ~/.config/airpodsd/config.json)"
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    parser.add_argument("-q", "--quiet", action="store_true", help="only print errors")
    parser.add_argument("--no-color", action="store_true", help="disable ANSI colors")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    sub = parser.add_subparsers(dest="command", metavar="command", required=True)

    scan = sub.add_parser("scan", help="discover nearby AirPods")
    scan.add_argument("-t", "--timeout", type=float, default=10.0, help="seconds (default: 10)")
    scan.add_argument("--all", action="store_true", help="show every Bluetooth device")
    scan.add_argument(
        "--bredr",
        dest="transport",
        action="store_const",
        const="bredr",
        default="auto",
        help="force classic BR/EDR discovery (AirPods workaround)",
    )
    scan.set_defaults(func=cmd_scan)

    pair = sub.add_parser("pair", help="pair AirPods (wizard)")
    pair.add_argument("-a", "--address", help="pair a specific address")
    pair.add_argument("-t", "--timeout", type=float, default=30.0, help="scan seconds (default: 30)")
    pair.add_argument(
        "-y", "--yes", action="store_true", help="never prompt; fail if several devices are found"
    )
    pair.add_argument(
        "--bredr",
        dest="transport",
        action="store_const",
        const="bredr",
        default="auto",
        help="force classic BR/EDR discovery (workaround for older BlueZ)",
    )
    pair.set_defaults(func=cmd_pair)

    connect = sub.add_parser("connect", help="connect paired AirPods")
    connect.add_argument("-a", "--address")
    connect.set_defaults(func=cmd_connect)

    disconnect = sub.add_parser("disconnect", help="disconnect AirPods")
    disconnect.add_argument("-a", "--address")
    disconnect.set_defaults(func=cmd_disconnect)

    forget = sub.add_parser("forget", help="remove a paired device from BlueZ")
    forget.add_argument("-a", "--address")
    forget.set_defaults(func=cmd_forget)

    status = sub.add_parser("status", help="show connection, audio and battery state")
    status.add_argument("-a", "--address")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=cmd_status)

    battery = sub.add_parser("battery", help="read AirPods battery levels")
    battery.add_argument("-a", "--address")
    battery.add_argument("--json", action="store_true")
    battery.add_argument("--watch", action="store_true", help="refresh until Ctrl-C")
    battery.add_argument("--interval", type=float, default=15.0, help="watch interval (default: 15)")
    battery.set_defaults(func=cmd_battery)

    audio = sub.add_parser("audio", help="inspect or switch A2DP/HFP")
    audio.add_argument("action", nargs="?", choices=("status", "a2dp", "hfp"), default="status")
    audio.add_argument("-a", "--address")
    audio.add_argument("--json", action="store_true")
    audio.set_defaults(func=cmd_audio)

    doctor = sub.add_parser("doctor", help="diagnose Bluetooth/audio problems")
    doctor.set_defaults(func=cmd_doctor)

    daemon = sub.add_parser("daemon", help="run the auto-connect/battery daemon")
    daemon.add_argument("--once", action="store_true", help=argparse.SUPPRESS)
    daemon.set_defaults(func=cmd_daemon)

    config_cmd = sub.add_parser("config", help="show or create the config file")
    config_cmd.add_argument("action", nargs="?", choices=("path", "show", "init"), default="show")
    config_cmd.add_argument("--force", action="store_true", help="overwrite an existing config")
    config_cmd.set_defaults(func=cmd_config)

    version = sub.add_parser("version", help="print the version")
    version.set_defaults(func=cmd_version)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.no_color:
        set_color_enabled(False)
    else:
        set_color_enabled(None)

    try:
        config = Config.load(args.config)
        _configure_logging(args, config)
        return int(args.func(args, config))
    except AirPodsError as exc:
        _error(exc.message, exc.hint)
        return 1
    except KeyboardInterrupt:
        print()
        return 130
    except BrokenPipeError:
        return 0
    except OSError as exc:
        _error(str(exc))
        return 1


def daemon_main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point for the ``airpodsd`` console script.

    Global options (``--mock``, ``--config``, ...) come before the implicit
    ``daemon`` subcommand; ``--once`` is the only daemon-specific flag that is
    also accepted here, because it is handy for debugging.
    """
    arguments = list(argv if argv is not None else sys.argv[1:])
    if arguments and arguments[0] == "daemon":
        return main(arguments)
    once = "--once" in arguments
    if once:
        arguments.remove("--once")
    command = [*arguments, "daemon"]
    if once:
        command.append("--once")
    return main(command)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
