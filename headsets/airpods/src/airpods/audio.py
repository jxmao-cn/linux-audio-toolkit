"""PulseAudio / PipeWire control through ``pactl``.

Using ``pactl`` instead of linking against libpulse keeps the project free of
build dependencies and works identically on PulseAudio and on PipeWire's
PulseAudio-compatible server.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from typing import Optional, Sequence

from .constants import (
    BLUETOOTH_CARD_PREFIX,
    BLUETOOTH_SINK_RE,
    BLUETOOTH_SOURCE_RE,
)
from .errors import AudioError
from .models import AudioStatus

logger = logging.getLogger(__name__)

_CODEC_RE = re.compile(r"codec\s+([A-Za-z0-9_+.-]+)", re.IGNORECASE)


@dataclass
class Card:
    """A parsed ``pactl list cards`` block."""

    name: str = ""
    driver: str = ""
    active_profile: str = ""
    profiles: dict[str, str] = field(default_factory=dict)

    @property
    def codec(self) -> str:
        description = self.profiles.get(self.active_profile, "")
        match = _CODEC_RE.search(description)
        return match.group(1).upper() if match else ""

    @property
    def is_bluetooth(self) -> bool:
        return self.name.startswith(BLUETOOTH_CARD_PREFIX)


def _environment() -> dict[str, str]:
    env = os.environ.copy()
    # pactl translates its field names when LANG is set; force stable output.
    env["LC_ALL"] = "C"
    env["LANG"] = "C"
    return env


def parse_cards(text: str) -> list[Card]:
    """Parse the text output of ``pactl list cards``.

    The parser only relies on the ``Name``, ``Active Profile`` and
    ``Profiles`` fields, which are stable across PulseAudio 13+ and PipeWire.
    """
    cards: list[Card] = []
    current: Optional[Card] = None
    section: Optional[str] = None

    for line in text.splitlines():
        if line.startswith("Card #"):
            if current is not None and current.name:
                cards.append(current)
            current = Card()
            section = None
            continue
        if current is None:
            continue
        if line.startswith("\t\t"):
            if section == "profiles":
                key, separator, description = line.strip().partition(": ")
                if separator and key:
                    current.profiles[key] = description
            continue
        if line.startswith("\t"):
            stripped = line.strip()
            if stripped == "Profiles:":
                section = "profiles"
                continue
            section = None
            if line.startswith("\tName: "):
                current.name = line.split(": ", 1)[1].strip()
            elif line.startswith("\tDriver: "):
                current.driver = line.split(": ", 1)[1].strip()
            elif line.startswith("\tActive Profile: "):
                current.active_profile = line.split(": ", 1)[1].strip()
            continue
        if not line.strip():
            section = None

    if current is not None and current.name:
        cards.append(current)
    return cards


def _match_bluez_name(pattern: re.Pattern[str], name: str, address: str) -> bool:
    match = pattern.match(name)
    if not match:
        return False
    found = match.group("address").replace("_", ":").upper()
    return found == address.upper()


def choose_profile(card: Card, kind: str) -> Optional[str]:
    """Pick the best profile of *kind* (``a2dp`` or ``hfp``) for *card*."""
    profiles = card.profiles
    if kind == "a2dp":
        exact = ["a2dp-sink", "a2dp_sink"]
        prefixes = ("a2dp-sink", "a2dp_sink", "a2dp")
    elif kind == "hfp":
        # PulseAudio uses underscore-separated names while PipeWire commonly
        # exposes the hyphenated BlueZ profile names. Restrict matches to
        # head-unit profiles so headset audio-gateway profiles are excluded.
        exact = [
            "headset-head-unit",
            "headset_head_unit",
            "handsfree_head_unit",
            "handsfree-head-unit",
            "headset-head-unit-msbc",
        ]
        prefixes = (
            "headset-head-unit",
            "headset_head_unit",
            "handsfree_head_unit",
            "handsfree-head-unit",
        )
    else:
        raise ValueError(f"unknown profile kind: {kind}")

    for name in exact:
        if name in profiles:
            return name
    candidates = [name for name in profiles if name.startswith(prefixes)]
    if candidates:
        # Prefer short, stable names over codec-specific experimental ones.
        candidates.sort(key=lambda name: (len(name), name))
        return candidates[0]
    return None


class AudioController:
    """Thin wrapper around the ``pactl`` command line tool."""

    def __init__(self, pactl: str = "pactl") -> None:
        self._pactl = pactl

    # -- command helpers --------------------------------------------------

    def _run(self, args: Sequence[str], timeout: float = 10.0) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                [self._pactl, *args],
                capture_output=True,
                text=True,
                env=_environment(),
                timeout=timeout,
            )
        except FileNotFoundError as exc:
            raise AudioError(
                "pactl was not found",
                hint="sudo apt install pulseaudio-utils",
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise AudioError(f"pactl {args[0]} timed out") from exc

    def _checked(self, args: Sequence[str], timeout: float = 10.0) -> str:
        result = self._run(args, timeout=timeout)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise AudioError(f"pactl {' '.join(args)} failed: {detail}")
        return result.stdout

    def available(self) -> bool:
        try:
            return self._run(["info"], timeout=5.0).returncode == 0
        except AudioError:
            return False

    def server_name(self) -> str:
        try:
            output = self._checked(["info"], timeout=5.0)
        except AudioError:
            return ""
        for line in output.splitlines():
            if line.startswith("Server Name:"):
                return line.split(":", 1)[1].strip()
        return ""

    def module_names(self) -> set[str]:
        result = self._run(["list", "modules", "short"])
        if result.returncode != 0:
            return set()
        names = set()
        for line in result.stdout.splitlines():
            fields = line.split("\t")
            if len(fields) >= 2 and fields[1].strip():
                names.add(fields[1].strip())
        return names

    def server_summary(self) -> str:
        name = self.server_name()
        if not name:
            return ""
        try:
            output = self._checked(["info"], timeout=5.0)
        except AudioError:
            return name
        version = ""
        for line in output.splitlines():
            if line.startswith("Server Version:"):
                version = line.split(":", 1)[1].strip()
                break
        return f"{name} {version}".strip()

    # -- cards ------------------------------------------------------------

    def cards(self) -> list[Card]:
        result = self._run(["list", "cards"])
        if result.returncode != 0:
            return []
        return parse_cards(result.stdout)

    def find_card(self, address: str) -> Optional[Card]:
        wanted = BLUETOOTH_CARD_PREFIX + address.replace(":", "_").lower()
        for card in self.cards():
            if card.name.lower() == wanted:
                return card
        return None

    def wait_for_card(self, address: str, timeout: float = 12.0) -> Optional[Card]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            card = self.find_card(address)
            if card is not None:
                return card
            time.sleep(0.4)
        return None

    def status(self, address: str) -> AudioStatus:
        card = self.find_card(address)
        if card is None:
            return AudioStatus()
        return AudioStatus(
            card=card.name,
            profile=card.active_profile,
            codec=card.codec,
            sink=self.find_sink(address) or "",
            source=self.find_source(address) or "",
        )

    def set_profile(self, address: str, kind: str) -> Card:
        card = self.wait_for_card(address)
        if card is None:
            raise AudioError(
                f"no Bluetooth audio card for {address}",
                hint=(
                    "the audio server has not seen the device yet; check "
                    "`pactl list cards` and run `airpodsctl doctor`"
                ),
            )
        profile = choose_profile(card, kind)
        if profile is None:
            available = ", ".join(card.profiles) or "none"
            raise AudioError(
                f"{kind} profile is not available on {card.name}",
                hint=f"available profiles: {available}",
            )
        if profile != card.active_profile:
            self._checked(["set-card-profile", card.name, profile])
            card.active_profile = profile
        return card

    def prefer_a2dp(self, address: str) -> Card:
        return self.set_profile(address, "a2dp")

    # -- sinks and sources ------------------------------------------------

    def _find_device_name(self, pattern: re.Pattern[str], command: str, address: str) -> str:
        result = self._run(["list", command])
        if result.returncode != 0:
            return ""
        for line in result.stdout.splitlines():
            if not line.startswith("\tName: "):
                continue
            name = line.split(": ", 1)[1].strip()
            if _match_bluez_name(pattern, name, address):
                return name
        return ""

    def find_sink(self, address: str) -> Optional[str]:
        return self._find_device_name(BLUETOOTH_SINK_RE, "sinks", address) or None

    def find_source(self, address: str) -> Optional[str]:
        return self._find_device_name(BLUETOOTH_SOURCE_RE, "sources", address) or None

    def set_default_sink(self, address: str) -> Optional[str]:
        sink = self.find_sink(address)
        if sink:
            self._checked(["set-default-sink", sink])
        return sink

    def set_default_source(self, address: str) -> Optional[str]:
        source = self.find_source(address)
        if source:
            self._checked(["set-default-source", source])
        return source
