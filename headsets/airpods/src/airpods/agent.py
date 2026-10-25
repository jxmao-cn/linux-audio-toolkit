"""BlueZ pairing agent.

BlueZ asks a registered agent for confirmation during Secure Simple Pairing.
AirPods use "Just Works" pairing, so RequestConfirmation is the only method
that normally runs; the other methods are implemented for completeness and
for other Bluetooth audio devices.
"""

from __future__ import annotations

import logging
from typing import Callable

import dbus
import dbus.service

logger = logging.getLogger(__name__)


class PairingAgent(dbus.service.Object):
    """A minimal, policy-driven BlueZ Agent1 implementation."""

    PATH = "/org/airpodsd/agent"

    def __init__(self, bus: dbus.Bus, confirm: Callable[[str], bool]) -> None:
        super().__init__(bus, self.PATH)
        self._confirm = confirm

    # -- helpers ---------------------------------------------------------

    def _allowed(self, device_path: str) -> bool:
        try:
            allowed = bool(self._confirm(device_path))
        except Exception:  # pragma: no cover - defensive
            logger.exception("pairing confirmation callback failed")
            allowed = False
        if not allowed:
            logger.warning("rejected pairing request for %s", device_path)
        return allowed

    @staticmethod
    def _reject() -> None:
        raise dbus.exceptions.DBusException(
            "Pairing was rejected by airpodsd", name="org.bluez.Error.Rejected"
        )

    # -- org.bluez.Agent1 ------------------------------------------------

    @dbus.service.method("org.bluez.Agent1", in_signature="", out_signature="")
    def Release(self) -> None:
        logger.debug("pairing agent released")

    @dbus.service.method("org.bluez.Agent1", in_signature="os", out_signature="")
    def DisplayPinCode(self, device: str, pincode: str) -> None:
        logger.info("pairing PIN for %s: %s", device, pincode)

    @dbus.service.method("org.bluez.Agent1", in_signature="ouq", out_signature="")
    def DisplayPasskey(self, device: str, passkey: int, entered: int) -> None:
        logger.info(
            "pairing passkey for %s: %06d (%d digits entered)",
            device,
            int(passkey),
            int(entered),
        )

    @dbus.service.method("org.bluez.Agent1", in_signature="ou", out_signature="")
    def RequestConfirmation(self, device: str, passkey: int) -> None:
        logger.info("confirming pairing with %s (passkey %06d)", device, int(passkey))
        if not self._allowed(device):
            self._reject()

    @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="")
    def RequestAuthorization(self, device: str) -> None:
        logger.info("authorization requested by %s", device)
        if not self._allowed(device):
            self._reject()

    @dbus.service.method("org.bluez.Agent1", in_signature="os", out_signature="")
    def AuthorizeService(self, device: str, uuid: str) -> None:
        logger.debug("service %s requested by %s", uuid, device)
        if not self._allowed(device):
            self._reject()

    @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="s")
    def RequestPinCode(self, device: str) -> str:
        if not self._allowed(device):
            self._reject()
        return "0000"

    @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="u")
    def RequestPasskey(self, device: str) -> dbus.UInt32:
        if not self._allowed(device):
            self._reject()
        return dbus.UInt32(0)

    @dbus.service.method("org.bluez.Agent1", in_signature="", out_signature="")
    def Cancel(self) -> None:
        logger.warning("pairing was cancelled by BlueZ")
