"""Configuration loading, validation and persistence.

The file lives at ``$XDG_CONFIG_HOME/airpodsd/config.json`` (usually
``~/.config/airpodsd/config.json``).  JSON is used instead of TOML so the
project keeps zero runtime dependencies and works on Python 3.9, which does
not ship :mod:`tomllib`.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from .constants import APP_NAME, DEFAULT_BATTERY_LABELS
from .errors import ConfigError

ADDRESS_RE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")
AUDIO_PROFILES = ("a2dp", "hfp", "keep")
LOG_LEVELS = ("debug", "info", "warning", "error")


def config_home() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    return Path(base) if base else Path.home() / ".config"


def state_home() -> Path:
    base = os.environ.get("XDG_STATE_HOME")
    return Path(base) if base else Path.home() / ".local" / "state"


def default_config_path() -> Path:
    return config_home() / APP_NAME / "config.json"


def default_state_path() -> Path:
    return state_home() / APP_NAME / "state.json"


@dataclass
class Config:
    """User configuration with conservative defaults."""

    address: Optional[str] = None
    adapter: Optional[str] = None
    auto_connect: bool = True
    scan_interval: int = 120
    scan_duration: int = 8
    connect_interval: int = 30
    battery_interval: int = 60
    battery_labels: list[str] = field(default_factory=lambda: list(DEFAULT_BATTERY_LABELS))
    notifications: bool = True
    low_battery_threshold: int = 20
    critical_battery_threshold: int = 10
    audio_profile: str = "a2dp"
    set_default_sink: bool = False
    log_level: str = "info"
    path: Path = field(default_factory=default_config_path, repr=False, compare=False)

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "Config":
        """Load configuration from *path* (or the default path).

        A missing file is not an error: it yields the defaults.  Invalid JSON
        or invalid values raise :class:`ConfigError`.
        """
        path = Path(path) if path else default_config_path()
        if not path.exists():
            return cls(path=path)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ConfigError(f"cannot read {path}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise ConfigError(
                f"{path} is not valid JSON (line {exc.lineno}, column {exc.colno})",
                hint="fix the file or run `airpodsctl config init --force`",
            ) from exc
        if not isinstance(raw, dict):
            raise ConfigError(f"{path} must contain a JSON object")
        return cls.from_dict(raw, path=path)

    @classmethod
    def from_dict(cls, raw: dict[str, Any], path: Optional[Path] = None) -> "Config":
        cfg = cls(path=Path(path) if path else default_config_path())

        if "address" in raw and raw["address"] not in (None, ""):
            address = _expect_str(raw["address"], "address").upper()
            if not ADDRESS_RE.match(address):
                raise ConfigError(f"invalid Bluetooth address in config: {address!r}")
            cfg.address = address

        if "adapter" in raw and raw["adapter"] not in (None, ""):
            cfg.adapter = _expect_str(raw["adapter"], "adapter")

        cfg.auto_connect = _expect_bool(raw.get("auto_connect", cfg.auto_connect), "auto_connect")
        cfg.scan_interval = _expect_int(
            raw.get("scan_interval", cfg.scan_interval), "scan_interval", minimum=10, maximum=3600
        )
        cfg.scan_duration = _expect_int(
            raw.get("scan_duration", cfg.scan_duration), "scan_duration", minimum=2, maximum=120
        )
        cfg.connect_interval = _expect_int(
            raw.get("connect_interval", cfg.connect_interval),
            "connect_interval",
            minimum=5,
            maximum=600,
        )
        cfg.battery_interval = _expect_int(
            raw.get("battery_interval", cfg.battery_interval),
            "battery_interval",
            minimum=10,
            maximum=3600,
        )
        cfg.battery_labels = _expect_str_list(
            raw.get("battery_labels", cfg.battery_labels), "battery_labels"
        )
        cfg.notifications = _expect_bool(
            raw.get("notifications", cfg.notifications), "notifications"
        )
        cfg.low_battery_threshold = _expect_int(
            raw.get("low_battery_threshold", cfg.low_battery_threshold),
            "low_battery_threshold",
            minimum=1,
            maximum=100,
        )
        cfg.critical_battery_threshold = _expect_int(
            raw.get("critical_battery_threshold", cfg.critical_battery_threshold),
            "critical_battery_threshold",
            minimum=1,
            maximum=100,
        )
        if cfg.critical_battery_threshold >= cfg.low_battery_threshold:
            raise ConfigError(
                "critical_battery_threshold must be lower than low_battery_threshold"
            )
        cfg.audio_profile = _expect_choice(
            raw.get("audio_profile", cfg.audio_profile), "audio_profile", AUDIO_PROFILES
        )
        cfg.set_default_sink = _expect_bool(
            raw.get("set_default_sink", cfg.set_default_sink), "set_default_sink"
        )
        cfg.log_level = _expect_choice(
            raw.get("log_level", cfg.log_level), "log_level", LOG_LEVELS
        )
        return cfg

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("path", None)
        data["battery_labels"] = list(self.battery_labels)
        return data

    def save(self, path: Optional[Path] = None) -> Path:
        """Write the configuration atomically and return the path used."""
        target = Path(path) if path else self.path
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        text = json.dumps(self.to_dict(), indent=2, ensure_ascii=False) + "\n"
        tmp.write_text(text, encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
        self.path = target
        return target


def _expect_bool(value: Any, key: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{key} must be true or false")
    return value


def _expect_int(value: Any, key: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{key} must be an integer")
    if not minimum <= value <= maximum:
        raise ConfigError(f"{key} must be between {minimum} and {maximum}")
    return value


def _expect_str(value: Any, key: str) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"{key} must be a string")
    return value.strip()


def _expect_str_list(value: Any, key: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ConfigError(f"{key} must be a non-empty list of strings")
    if not all(isinstance(item, str) and item.strip() for item in value):
        raise ConfigError(f"{key} must be a non-empty list of strings")
    return [item.strip() for item in value]


def _expect_choice(value: Any, key: str, choices: tuple[str, ...]) -> str:
    if value not in choices:
        raise ConfigError(f"{key} must be one of: {', '.join(choices)}")
    return str(value)
