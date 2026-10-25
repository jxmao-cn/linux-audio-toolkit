"""Real BlueZ backend implemented on top of the system D-Bus.

Only this module (and :mod:`airpods.agent`) depends on ``dbus-python`` and
PyGObject.  Both are imported lazily so the rest of the package can be
installed and tested on machines without a Bluetooth stack.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Optional

from .constants import (
    ADAPTER_IFACE,
    AGENT_MANAGER_IFACE,
    BATTERY_IFACE,
    BATTERY_LEVEL_UUID,
    BATTERY_SERVICE_UUID,
    BLUEZ_ROOT,
    BLUEZ_SERVICE,
    DEFAULT_BATTERY_LABELS,
    DEVICE_IFACE,
    GATT_CHARACTERISTIC_IFACE,
    GATT_SERVICE_IFACE,
    OBJECT_MANAGER_IFACE,
    PROPERTIES_IFACE,
)
from .errors import AirPodsError, BackendUnavailable, DeviceNotFound, PairingError
from .models import Adapter, BatteryCell, BatterySnapshot, Device

logger = logging.getLogger(__name__)


class BlueZBackend:
    """Talk to ``org.bluez`` over the system bus."""

    def __init__(
        self,
        adapter: Optional[str] = None,
        battery_labels: Optional[list[str]] = None,
        bus: Any = None,
    ) -> None:
        try:
            import dbus
            from dbus.mainloop.glib import DBusGMainLoop
        except ImportError as exc:  # pragma: no cover - depends on host packages
            raise BackendUnavailable(
                "python3-dbus / python3-gi are not installed",
                hint="sudo apt install python3-dbus python3-gi",
            ) from exc

        try:
            DBusGMainLoop(set_as_default=True)
        except Exception:  # pragma: no cover - non-fatal convenience
            logger.debug("could not install the GLib D-Bus main loop", exc_info=True)

        self._dbus = dbus
        try:
            self.bus = bus if bus is not None else dbus.SystemBus()
        except dbus.exceptions.DBusException as exc:  # pragma: no cover
            raise BackendUnavailable(
                "cannot connect to the system D-Bus",
                hint="is the bluetooth service running? try `sudo systemctl start bluetooth`",
            ) from exc

        self._adapter_selector = adapter or None
        self._labels = list(battery_labels or DEFAULT_BATTERY_LABELS)
        self._agent = None
        self._started_discovery = False

    # -- low level helpers ------------------------------------------------

    def _managed_objects(self) -> dict[str, dict[str, dict[str, Any]]]:
        try:
            manager = self._dbus.Interface(
                self.bus.get_object(BLUEZ_SERVICE, "/"), OBJECT_MANAGER_IFACE
            )
            return manager.GetManagedObjects()
        except self._dbus.exceptions.DBusException as exc:
            raise BackendUnavailable(
                "BlueZ is not answering on the system bus",
                hint="sudo systemctl status bluetooth",
            ) from exc

    def _properties(self, path: str) -> Any:
        return self._dbus.Interface(
            self.bus.get_object(BLUEZ_SERVICE, path), PROPERTIES_IFACE
        )

    def _adapter_iface(self, path: str) -> Any:
        return self._dbus.Interface(self.bus.get_object(BLUEZ_SERVICE, path), ADAPTER_IFACE)

    def _device_iface(self, path: str) -> Any:
        return self._dbus.Interface(self.bus.get_object(BLUEZ_SERVICE, path), DEVICE_IFACE)

    def _adapter_candidates(self) -> list[tuple[str, dict[str, Any]]]:
        objects = self._managed_objects()
        found = [
            (path, interfaces[ADAPTER_IFACE])
            for path, interfaces in objects.items()
            if ADAPTER_IFACE in interfaces
        ]
        found.sort(key=lambda item: item[0])
        return found

    def _adapter_entry(self) -> tuple[str, dict[str, Any]]:
        candidates = self._adapter_candidates()
        if not candidates:
            raise BackendUnavailable(
                "no Bluetooth adapter was found",
                hint="check `rfkill list bluetooth` and `sudo systemctl status bluetooth`",
            )
        selector = self._adapter_selector
        if selector:
            selector_l = selector.lower()
            for path, props in candidates:
                short = path.rsplit("/", 1)[-1].lower()
                names = {
                    short,
                    str(props.get("Address", "")).lower(),
                    str(props.get("Name", "")).lower(),
                    str(props.get("Alias", "")).lower(),
                }
                if selector_l in names:
                    return path, props
            raise BackendUnavailable(
                f"Bluetooth adapter {selector!r} was not found",
                hint="run `bluetoothctl list` to see the available controllers",
            )
        for path, props in candidates:
            if bool(props.get("Powered")):
                return path, props
        return candidates[0]

    def _device_path(self, address: str) -> Optional[str]:
        address = (address or "").upper()
        adapter_path = None
        if self._adapter_selector:
            try:
                adapter_path = self._adapter_entry()[0]
            except AirPodsError:
                adapter_path = None
        for path, interfaces in self._managed_objects().items():
            props = interfaces.get(DEVICE_IFACE)
            if not props:
                continue
            if str(props.get("Address", "")).upper() != address:
                continue
            if adapter_path and str(props.get("Adapter", "")) != adapter_path:
                continue
            return path
        return None

    @staticmethod
    def _parse_device(path: str, props: dict[str, Any]) -> Device:
        manufacturer: dict[int, bytes] = {}
        for key, value in dict(props.get("ManufacturerData", {}) or {}).items():
            try:
                manufacturer[int(key)] = bytes(bytearray(value))
            except (TypeError, ValueError):  # pragma: no cover - defensive
                continue
        rssi = int(props["RSSI"]) if "RSSI" in props else None
        return Device(
            address=str(props.get("Address", "")),
            path=path,
            name=str(props.get("Name", "")),
            alias=str(props.get("Alias", "")),
            icon=str(props.get("Icon", "")),
            connected=bool(props.get("Connected", False)),
            paired=bool(props.get("Paired", False)),
            trusted=bool(props.get("Trusted", False)),
            rssi=rssi,
            adapter=str(props.get("Adapter", "")),
            manufacturer_data=manufacturer,
            uuids=tuple(str(uuid) for uuid in props.get("UUIDs", ())),
            modalias=str(props.get("Modalias", "")),
        )

    @staticmethod
    def _parse_adapter(path: str, props: dict[str, Any]) -> Adapter:
        return Adapter(
            path=str(path),
            address=str(props.get("Address", "")),
            name=str(props.get("Name", "")),
            alias=str(props.get("Alias", "")),
            powered=bool(props.get("Powered", False)),
            discovering=bool(props.get("Discovering", False)),
            discoverable=bool(props.get("Discoverable", False)),
            pairable=bool(props.get("Pairable", False)),
        )

    @staticmethod
    def _dbus_error(exc: Exception, action: str) -> AirPodsError:
        name = ""
        message = str(exc)
        if hasattr(exc, "get_dbus_name"):
            name = exc.get_dbus_name() or ""
            message = exc.get_dbus_message() or message
        if name.endswith("Failed") and ("Page Timeout" in message or "Timeout" in message):
            return AirPodsError(
                f"{action} timed out",
                hint=(
                    "keep the buds in the open case and make sure the light flashes "
                    "white (AirPods 4: double-tap the front below the LED). If it "
                    "still times out, try `airpodsctl pair --bredr`; on old BlueZ "
                    "also try `sudo btmgmt le off` before pairing and `sudo btmgmt le on` after"
                ),
            )
        if name.endswith("AuthenticationFailed") or name.endswith("AuthenticationRejected"):
            return PairingError(
                f"{action} failed: authentication failed or timed out",
                hint=(
                    "remove the device and retry: `airpodsctl forget -a <MAC>` then "
                    "`airpodsctl pair --bredr`. Make sure the AirPods are in pairing "
                    "mode (AirPods 4: double-tap the front) and not connected to an iPhone"
                ),
            )
        if name.endswith("NotReady"):
            return BackendUnavailable(
                f"{action} failed: the Bluetooth adapter is not ready",
                hint="sudo systemctl restart bluetooth",
            )
        if name.endswith("InProgress") or name.endswith("AlreadyConnected"):
            return AirPodsError(f"{action} is already in progress")
        return AirPodsError(
            f"{action} failed: {message}",
            hint="run `airpodsctl doctor` for diagnostics",
        )

    def _async_call(self, interface: Any, method: str, timeout: float = 30.0) -> Any:
        """Call a D-Bus method while iterating GLib so agent callbacks work."""
        try:
            from gi.repository import GLib
        except ImportError:  # pragma: no cover - host without PyGObject
            return getattr(interface, method)(timeout=timeout)

        state: dict[str, Any] = {}
        loop = GLib.MainLoop()

        def reply(*args: Any) -> None:
            state["result"] = args[0] if len(args) == 1 else args
            loop.quit()

        def error(exc: Exception) -> None:
            state["error"] = exc
            loop.quit()

        def expired() -> bool:
            state["timeout"] = True
            loop.quit()
            return False

        timer = GLib.timeout_add_seconds(int(timeout) + 2, expired)
        getattr(interface, method)(
            reply_handler=reply, error_handler=error, timeout=timeout
        )
        loop.run()
        try:
            GLib.source_remove(timer)
        except Exception:  # pragma: no cover
            pass
        if state.get("timeout"):
            raise AirPodsError(f"{method} timed out after {int(timeout)}s")
        if "error" in state:
            raise state["error"]
        return state.get("result")

    # -- public backend API ----------------------------------------------

    def adapter(self) -> Adapter:
        path, props = self._adapter_entry()
        return self._parse_adapter(path, props)

    def ensure_powered(self) -> None:
        path, props = self._adapter_entry()
        if bool(props.get("Powered")):
            return
        try:
            self._properties(path).Set(ADAPTER_IFACE, "Powered", self._dbus.Boolean(True))
        except self._dbus.exceptions.DBusException as exc:
            raise BackendUnavailable(
                "could not power on the Bluetooth adapter",
                hint="check `rfkill list bluetooth` and the GNOME Bluetooth settings",
            ) from exc

    def devices(self) -> list[Device]:
        result = []
        for path, interfaces in self._managed_objects().items():
            props = interfaces.get(DEVICE_IFACE)
            if props:
                result.append(self._parse_device(path, props))
        result.sort(key=lambda dev: (not dev.connected, dev.address))
        return result

    def device(self, address: str) -> Optional[Device]:
        path = self._device_path(address)
        if not path:
            return None
        props = self._managed_objects().get(path, {}).get(DEVICE_IFACE, {})
        return self._parse_device(path, props) if props else None

    def _require_device(self, address: str) -> Device:
        device = self.device(address)
        if device is None:
            raise DeviceNotFound(
                f"no Bluetooth device with address {address} is known",
                hint="run `airpodsctl scan` while the AirPods are in pairing mode",
            )
        return device

    def start_discovery(self, transport: str = "auto") -> None:
        self.ensure_powered()
        path, _ = self._adapter_entry()
        adapter = self._adapter_iface(path)
        # "auto" covers BR/EDR and LE. "bredr" is an explicit workaround for
        # BlueZ versions that pick the unconnectable LE bearer when pairing
        # with AirPods (see docs/TROUBLESHOOTING.md).
        try:
            adapter.SetDiscoveryFilter(
                self._dbus.Dictionary(
                    {"Transport": self._dbus.String(transport)}, signature="sv"
                )
            )
        except self._dbus.exceptions.DBusException:
            logger.debug("SetDiscoveryFilter(%s) failed", transport, exc_info=True)
        try:
            adapter.StartDiscovery()
            self._started_discovery = True
        except self._dbus.exceptions.DBusException as exc:
            name = exc.get_dbus_name() or ""
            if name.endswith("InProgress"):
                self._started_discovery = True
                return
            raise self._dbus_error(exc, "starting Bluetooth discovery") from exc

    def stop_discovery(self) -> None:
        if not self._started_discovery:
            return
        try:
            path, _ = self._adapter_entry()
            self._adapter_iface(path).StopDiscovery()
        except (self._dbus.exceptions.DBusException, AirPodsError):
            logger.debug("StopDiscovery failed", exc_info=True)
        finally:
            self._started_discovery = False

    def is_discovering(self) -> bool:
        try:
            _path, props = self._adapter_entry()
        except AirPodsError:
            return False
        return bool(props.get("Discovering", False))

    def pair(self, address: str) -> None:
        self.ensure_powered()
        device = self._require_device(address)
        if device.paired:
            return
        if self._agent is None:
            raise BackendUnavailable(
                "no pairing agent is registered",
                hint="this is a bug in airpodsd; please report it",
            )
        try:
            self._async_call(self._device_iface(device.path), "Pair", timeout=60.0)
        except self._dbus.exceptions.DBusException as exc:
            name = exc.get_dbus_name() or ""
            if name.endswith("AlreadyExists"):
                return
            raise self._dbus_error(exc, f"pairing with {device.display_name}") from exc

    def connect(self, address: str) -> None:
        self.ensure_powered()
        device = self._require_device(address)
        if device.connected:
            return
        try:
            self._async_call(self._device_iface(device.path), "Connect", timeout=30.0)
        except self._dbus.exceptions.DBusException as exc:
            name = exc.get_dbus_name() or ""
            if name.endswith(("AlreadyConnected", "InProgress")):
                return
            raise self._dbus_error(exc, f"connecting to {device.display_name}") from exc

    def wait_connected(self, address: str, timeout: float = 20.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            device = self.device(address)
            if device and device.connected:
                return True
            time.sleep(0.4)
        return False

    def disconnect(self, address: str) -> None:
        device = self._require_device(address)
        if not device.connected:
            return
        try:
            self._async_call(self._device_iface(device.path), "Disconnect", timeout=15.0)
        except self._dbus.exceptions.DBusException as exc:
            name = exc.get_dbus_name() or ""
            if name.endswith("NotConnected"):
                return
            raise self._dbus_error(exc, f"disconnecting from {device.display_name}") from exc

    def set_trusted(self, address: str, trusted: bool = True) -> None:
        device = self._require_device(address)
        try:
            self._properties(device.path).Set(
                DEVICE_IFACE, "Trusted", self._dbus.Boolean(bool(trusted))
            )
        except self._dbus.exceptions.DBusException as exc:
            raise self._dbus_error(exc, "updating the trusted flag") from exc

    def remove(self, address: str) -> None:
        device = self._require_device(address)
        path = device.adapter or self._adapter_entry()[0]
        try:
            self._adapter_iface(path).RemoveDevice(self._dbus.ObjectPath(device.path))
        except self._dbus.exceptions.DBusException as exc:
            name = exc.get_dbus_name() or ""
            if name.endswith("DoesNotExist"):
                return
            raise self._dbus_error(exc, f"removing {device.display_name}") from exc

    # -- battery ----------------------------------------------------------

    @staticmethod
    def _normalise_uuid(value: Any) -> str:
        return str(value).strip().lower()

    def _wait_services_resolved(self, path: str, timeout: float = 8.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            props = (
                self._managed_objects().get(path, {}).get(DEVICE_IFACE, {})
            )
            if bool(props.get("ServicesResolved", False)):
                return True
            if not bool(props.get("Connected", False)):
                return False
            time.sleep(0.3)
        return False

    def _read_gatt_battery(self, device_path: str) -> list[int]:
        objects = self._managed_objects()
        services = {
            path
            for path, interfaces in objects.items()
            if path.startswith(device_path + "/")
            and GATT_SERVICE_IFACE in interfaces
            and self._normalise_uuid(interfaces[GATT_SERVICE_IFACE].get("UUID", ""))
            == BATTERY_SERVICE_UUID
        }
        characteristics = []
        for path, interfaces in objects.items():
            props = interfaces.get(GATT_CHARACTERISTIC_IFACE)
            if not props:
                continue
            if str(props.get("Service", "")) not in services:
                continue
            if self._normalise_uuid(props.get("UUID", "")) != BATTERY_LEVEL_UUID:
                continue
            characteristics.append(path)

        levels: list[int] = []
        for path in sorted(characteristics):
            try:
                characteristic = self._dbus.Interface(
                    self.bus.get_object(BLUEZ_SERVICE, path), GATT_CHARACTERISTIC_IFACE
                )
                value = characteristic.ReadValue(
                    self._dbus.Dictionary({}, signature="sv")
                )
                raw = bytes(bytearray(value))
                if raw:
                    levels.append(int(raw[0]))
            except self._dbus.exceptions.DBusException:
                logger.warning("could not read battery characteristic %s", path, exc_info=True)
        return levels

    def _read_battery_property(self, device_path: str) -> Optional[int]:
        props = self._managed_objects().get(device_path, {}).get(BATTERY_IFACE)
        if not props or "Percentage" not in props:
            return None
        try:
            return int(props["Percentage"])
        except (TypeError, ValueError):
            return None

    def battery(self, address: str) -> BatterySnapshot:
        device = self._require_device(address)
        if not device.connected:
            raise DeviceNotFound(
                f"{device.display_name} is not connected",
                hint="run `airpodsctl connect` first",
            )
        self._wait_services_resolved(device.path)
        levels: list[int] = []
        for attempt in range(3):
            levels = self._read_gatt_battery(device.path)
            if levels:
                break
            if attempt < 2:
                time.sleep(0.5)
        source = "gatt"
        if not levels:
            fallback = self._read_battery_property(device.path)
            if fallback is not None:
                levels = [fallback]
                source = "bluez"
        if not levels:
            raise AirPodsError(
                f"{device.display_name} did not expose any battery information",
                hint=(
                    "battery reading needs the GATT battery service; make sure the buds "
                    "are out of the case, then try again. `airpodsctl doctor` shows more."
                ),
            )
        return BatterySnapshot(cells=self._label_cells(levels), source=source)

    def _label_cells(self, levels: list[int]) -> list[BatteryCell]:
        """Attach labels to unlabelled GATT battery values.

        A single value is an aggregate (or at least indistinguishable from
        one), so it must not be called "left".  With two or three values the
        configured order is used, but it remains a heuristic.
        """
        if len(levels) == 1:
            return [BatteryCell("battery", levels[0])]
        labels = list(self._labels)
        while len(labels) < len(levels):
            labels.append(f"cell {len(labels) + 1}")
        return [
            BatteryCell(label=labels[index], level=level)
            for index, level in enumerate(levels)
        ]

    # -- pairing agent ----------------------------------------------------

    def register_agent(self, confirm: Callable[[str], bool]) -> None:
        if self._agent is not None:
            return
        from .agent import PairingAgent

        self._agent = PairingAgent(self.bus, confirm)
        manager = self._dbus.Interface(
            self.bus.get_object(BLUEZ_SERVICE, BLUEZ_ROOT), AGENT_MANAGER_IFACE
        )
        try:
            manager.RegisterAgent(PairingAgent.PATH, "KeyboardDisplay")
        except self._dbus.exceptions.DBusException as exc:
            name = exc.get_dbus_name() or ""
            if not name.endswith("AlreadyExists"):
                self._agent = None
                raise self._dbus_error(exc, "registering the pairing agent") from exc
        try:
            manager.RequestDefaultAgent(PairingAgent.PATH)
        except self._dbus.exceptions.DBusException:
            logger.warning(
                "another Bluetooth agent is already the default; pairing may still work",
                exc_info=True,
            )

    def close(self) -> None:
        self.stop_discovery()
        if self._agent is not None:
            from .agent import PairingAgent

            try:
                manager = self._dbus.Interface(
                    self.bus.get_object(BLUEZ_SERVICE, BLUEZ_ROOT), AGENT_MANAGER_IFACE
                )
                manager.UnregisterAgent(PairingAgent.PATH)
            except self._dbus.exceptions.DBusException:
                logger.debug("could not unregister the pairing agent", exc_info=True)
            try:
                self._agent.remove_from_connection()
            except Exception:  # pragma: no cover - defensive
                pass
            self._agent = None
