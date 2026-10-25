"""Mock backend and audio controller for demos, docs and tests.

``airpodsctl --mock ...`` never talks to BlueZ or PulseAudio, which makes it
possible to try the UI and run the test suite on CI machines without hardware.
"""

from __future__ import annotations

from typing import Callable, Optional

from .audio import Card
from .models import Adapter, AudioStatus, BatteryCell, BatterySnapshot, Device

MOCK_ADDRESS = "AC:12:34:56:78:90"
MOCK_NAME = "AirPods Pro (2nd generation)"


class MockBackend:
    def __init__(self) -> None:
        self.connected = True
        self.paired = True
        self.trusted = True
        self.discovering = False
        self.battery_calls = 0

    # -- backend API ------------------------------------------------------

    def adapter(self) -> Adapter:
        return Adapter(
            path="/org/bluez/hci0",
            address="D4:F3:2D:34:AF:4D",
            name="hci0",
            alias="hci0",
            powered=True,
            discovering=self.discovering,
        )

    def devices(self) -> list[Device]:
        device = self.device(MOCK_ADDRESS)
        return [device] if device is not None else []

    def device(self, address: str) -> Optional[Device]:
        if address.upper() != MOCK_ADDRESS:
            return None
        return Device(
            address=MOCK_ADDRESS,
            path="/org/bluez/hci0/dev_AC_12_34_56_78_90",
            name=MOCK_NAME,
            alias=MOCK_NAME,
            icon="audio-headphones",
            connected=self.connected,
            paired=self.paired,
            trusted=self.trusted,
            rssi=-48 if self.connected else None,
            adapter="/org/bluez/hci0",
            uuids=("0000110b-0000-1000-8000-00805f9b34fb",),
        )

    def start_discovery(self, transport: str = "auto") -> None:
        self.discovering = True

    def stop_discovery(self) -> None:
        self.discovering = False

    def is_discovering(self) -> bool:
        return self.discovering

    def pair(self, address: str) -> None:
        self.paired = True
        self.connected = True

    def connect(self, address: str) -> None:
        self.connected = True

    def wait_connected(self, address: str, timeout: float = 20.0) -> bool:
        return self.connected

    def disconnect(self, address: str) -> None:
        self.connected = False

    def set_trusted(self, address: str, trusted: bool = True) -> None:
        self.trusted = trusted

    def remove(self, address: str) -> None:
        self.paired = False
        self.trusted = False
        self.connected = False

    def battery(self, address: str) -> BatterySnapshot:
        self.battery_calls += 1
        return BatterySnapshot(
            cells=[
                BatteryCell("left", 82),
                BatteryCell("right", 79),
                BatteryCell("case", 65),
            ],
            source="mock",
        )

    def register_agent(self, confirm: Callable[[str], bool]) -> None:
        pass

    def close(self) -> None:
        self.discovering = False


class MockAudio:
    """An AudioController look-alike backed by an in-memory card."""

    def __init__(self, address: str = MOCK_ADDRESS) -> None:
        self.address = address
        self._profile = "a2dp-sink"
        self._card = Card(
            name="bluez_card." + address.replace(":", "_"),
            driver="module-bluez5-device.c",
            active_profile=self._profile,
            profiles={
                "a2dp-sink": "High Fidelity Playback (A2DP Sink, codec SBC)",
                "headset-head-unit": "Headset Head Unit (HSP/HFP, codec CVSD)",
                "off": "Off",
            },
        )

    def available(self) -> bool:
        return True

    def server_summary(self) -> str:
        return "PulseAudio 15.99.1 (mock)"

    def cards(self) -> list[Card]:
        self._card.active_profile = self._profile
        return [self._card]

    def find_card(self, address: str) -> Optional[Card]:
        return self._card if address.upper() == self.address else None

    def wait_for_card(self, address: str, timeout: float = 12.0) -> Optional[Card]:
        return self.find_card(address)

    def status(self, address: str) -> AudioStatus:
        return AudioStatus(
            card=self._card.name,
            profile=self._profile,
            codec=self._card.codec,
            sink="bluez_output." + address.replace(":", "_") + ".1",
            source="bluez_input." + address.replace(":", "_"),
        )

    def set_profile(self, address: str, kind: str) -> Card:
        self._profile = "a2dp-sink" if kind == "a2dp" else "headset-head-unit"
        self._card.active_profile = self._profile
        return self._card

    def prefer_a2dp(self, address: str) -> Card:
        return self.set_profile(address, "a2dp")

    def find_sink(self, address: str) -> Optional[str]:
        return "bluez_output." + address.replace(":", "_") + ".1"

    def find_source(self, address: str) -> Optional[str]:
        return "bluez_input." + address.replace(":", "_")

    def set_default_sink(self, address: str) -> Optional[str]:
        return self.find_sink(address)

    def set_default_source(self, address: str) -> Optional[str]:
        return self.find_source(address)

    def module_names(self) -> set[str]:
        return {"module-bluetooth-discover", "module-bluetooth-policy"}
