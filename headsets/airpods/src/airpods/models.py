"""Plain data objects shared by the CLI, daemon and backends."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from .constants import AIRPODS_NAME_RE, MODEL_PATTERNS


def detect_model(name: str) -> str:
    """Return a human readable AirPods model for *name*.

    Falls back to ``"AirPods"`` for generic names and to *name* itself for
    devices that are not AirPods at all.
    """
    name = (name or "").strip()
    if not name:
        return "Unknown device"
    for pattern, model in MODEL_PATTERNS:
        if pattern.search(name):
            return model
    return name


def is_airpods(name: str) -> bool:
    """True when *name* looks like an AirPods product name."""
    return bool(AIRPODS_NAME_RE.search(name or ""))


@dataclass(frozen=True)
class Adapter:
    """A local Bluetooth controller exposed by BlueZ."""

    path: str
    address: str
    name: str = ""
    alias: str = ""
    powered: bool = False
    discovering: bool = False
    discoverable: bool = False
    pairable: bool = False

    @property
    def id(self) -> str:
        """Short controller id such as ``hci0``."""
        return self.path.rsplit("/", 1)[-1]


@dataclass(frozen=True)
class Device:
    """A Bluetooth device known to BlueZ."""

    address: str
    path: str = ""
    name: str = ""
    alias: str = ""
    icon: str = ""
    connected: bool = False
    paired: bool = False
    trusted: bool = False
    rssi: Optional[int] = None
    adapter: str = ""
    manufacturer_data: dict[int, bytes] = field(default_factory=dict)
    uuids: tuple[str, ...] = ()
    modalias: str = ""

    @property
    def display_name(self) -> str:
        return self.alias or self.name or self.address

    @property
    def model(self) -> str:
        return detect_model(self.name or self.alias)

    @property
    def is_airpods(self) -> bool:
        return is_airpods(self.name) or is_airpods(self.alias)

    @property
    def apple(self) -> bool:
        return 0x004C in self.manufacturer_data or "V004C" in (self.modalias or "").upper()

    def to_dict(self) -> dict[str, Any]:
        return {
            "address": self.address,
            "name": self.name,
            "alias": self.alias,
            "model": self.model,
            "icon": self.icon,
            "connected": self.connected,
            "paired": self.paired,
            "trusted": self.trusted,
            "rssi": self.rssi,
            "adapter": self.adapter,
            "is_airpods": self.is_airpods,
            "apple": self.apple,
        }


@dataclass(frozen=True)
class BatteryCell:
    """One battery reading, usually one pod or the charging case."""

    label: str
    level: Optional[int]

    @property
    def valid(self) -> bool:
        return self.level is not None and 0 <= int(self.level) <= 100

    def to_dict(self) -> dict[str, Any]:
        return {"label": self.label, "level": self.level}


@dataclass
class BatterySnapshot:
    """Battery state for a set of AirPods."""

    cells: list[BatteryCell] = field(default_factory=list)
    source: str = "gatt"
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def available(self) -> bool:
        return any(cell.valid for cell in self.cells)

    @property
    def lowest(self) -> Optional[int]:
        levels = [int(cell.level) for cell in self.cells if cell.valid]
        return min(levels) if levels else None

    def cell(self, label: str) -> Optional[BatteryCell]:
        for cell in self.cells:
            if cell.label == label:
                return cell
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "updated_at": self.updated_at.isoformat(),
            "lowest": self.lowest,
            "cells": [cell.to_dict() for cell in self.cells],
        }


@dataclass(frozen=True)
class AudioStatus:
    """The PulseAudio/PipeWire view of a connected Bluetooth device."""

    card: str = ""
    profile: str = ""
    codec: str = ""
    sink: str = ""
    source: str = ""

    @property
    def connected(self) -> bool:
        return bool(self.card and self.profile)

    def to_dict(self) -> dict[str, Any]:
        return {
            "card": self.card,
            "profile": self.profile,
            "codec": self.codec,
            "sink": self.sink,
            "source": self.source,
        }
