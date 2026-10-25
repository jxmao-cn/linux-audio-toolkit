"""Exception types with user-facing hints."""

from __future__ import annotations

from typing import Optional


class AirPodsError(Exception):
    """Base class for errors that should be printed without a traceback."""

    def __init__(self, message: str, hint: Optional[str] = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint

    def __str__(self) -> str:
        return self.message


class BackendUnavailable(AirPodsError):
    """BlueZ (or its D-Bus bindings) is not available."""


class DeviceNotFound(AirPodsError):
    """The requested device is not known to BlueZ."""


class PairingError(AirPodsError):
    """Pairing failed or was rejected."""


class AudioError(AirPodsError):
    """The audio server could not satisfy a request."""


class ConfigError(AirPodsError):
    """The configuration file is invalid."""
