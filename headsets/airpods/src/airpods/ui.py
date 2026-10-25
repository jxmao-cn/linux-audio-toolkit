"""Small terminal formatting helpers (no third-party dependencies)."""

from __future__ import annotations

import os
import sys
from datetime import datetime
from typing import Optional, TextIO

from .models import AudioStatus, BatterySnapshot

CELL_LABELS = {"left": "L", "right": "R", "case": "Case"}
_COLOR_OVERRIDE: Optional[bool] = None


def set_color_enabled(enabled: Optional[bool]) -> None:
    """Force colors on/off (``None`` restores automatic detection)."""
    global _COLOR_OVERRIDE
    _COLOR_OVERRIDE = enabled


def color_enabled(stream: Optional[TextIO] = None) -> bool:
    if _COLOR_OVERRIDE is not None:
        return _COLOR_OVERRIDE
    if os.environ.get("NO_COLOR"):
        return False
    stream = stream or sys.stdout
    return bool(getattr(stream, "isatty", lambda: False)())


def _paint(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if color_enabled() else text


def bold(text: str) -> str:
    return _paint("1", text)


def dim(text: str) -> str:
    return _paint("2", text)


def green(text: str) -> str:
    return _paint("32", text)


def yellow(text: str) -> str:
    return _paint("33", text)


def red(text: str) -> str:
    return _paint("31", text)


def cyan(text: str) -> str:
    return _paint("36", text)


def cell_label(label: str) -> str:
    return CELL_LABELS.get(label.lower(), label.title())


def format_battery(snapshot: Optional[BatterySnapshot]) -> str:
    if snapshot is None or not snapshot.cells:
        return "unavailable"
    parts = []
    for cell in snapshot.cells:
        name = cell_label(cell.label)
        parts.append(f"{name} {cell.level}%" if cell.valid else f"{name} --")
    return "   ".join(parts)


def battery_bar(level: Optional[int], width: int = 10) -> str:
    if level is None:
        return "░" * width
    filled = max(0, min(width, round(int(level) / 100 * width)))
    return "█" * filled + "░" * (width - filled)


def profile_label(profile: str) -> str:
    if profile.startswith("a2dp"):
        return "A2DP"
    if profile.startswith("headset"):
        return "HFP"
    return profile or "unknown"


def format_audio(status: Optional[AudioStatus]) -> str:
    if status is None or not status.card:
        return "not available"
    label = profile_label(status.profile)
    if status.codec:
        label = f"{label} ({status.codec})"
    if status.sink:
        label = f"{label} · {status.sink}"
    return label


def format_timestamp(value: Optional[datetime]) -> str:
    if value is None:
        return "unknown"
    local = value.astimezone() if value.tzinfo else value
    return local.strftime("%Y-%m-%d %H:%M:%S")
