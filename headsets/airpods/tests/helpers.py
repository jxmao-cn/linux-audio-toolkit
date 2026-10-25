"""Shared test doubles."""

from __future__ import annotations

from typing import Optional

from airpods.audio import Card
from airpods.models import AudioStatus, BatteryCell, BatterySnapshot


class FakeClock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeNotifier:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str, str]] = []

    def send(
        self,
        title: str,
        body: str = "",
        urgency: str = "normal",
        icon: str = "audio-headphones",
    ) -> bool:
        self.messages.append((title, body, urgency))
        return True


class FakeAudio:
    def __init__(self, fail_first: int = 0, source_fail_first: int = 0) -> None:
        self.profile = "a2dp-sink"
        self.applied: list[str] = []
        self.default_sink: Optional[str] = None
        self.default_source: Optional[str] = None
        self.fail_first = fail_first
        self.source_fail_first = source_fail_first
        self.source_calls = 0
        self.wait_calls = 0

    def wait_for_card(self, address: str, timeout: float = 12.0) -> Optional[Card]:
        self.wait_calls += 1
        if self.wait_calls <= self.fail_first:
            return None
        return self.cards()[0]

    def cards(self) -> list[Card]:
        return [
            Card(
                name="bluez_card.AC_12_34_56_78_90",
                active_profile=self.profile,
                profiles={
                    "a2dp-sink": "High Fidelity Playback (A2DP Sink, codec SBC)",
                    "headset-head-unit": "Headset Head Unit (HSP/HFP, codec CVSD)",
                },
            )
        ]

    def set_profile(self, address: str, kind: str) -> Card:
        self.profile = "a2dp-sink" if kind == "a2dp" else "headset-head-unit"
        self.applied.append(self.profile)
        return self.cards()[0]

    def set_default_sink(self, address: str) -> str:
        self.default_sink = "bluez_output.AC_12_34_56_78_90.1"
        return self.default_sink

    def set_default_source(self, address: str) -> Optional[str]:
        self.source_calls += 1
        if self.source_calls <= self.source_fail_first:
            return None
        self.default_source = "bluez_input.AC_12_34_56_78_90"
        return self.default_source

    def status(self, address: str) -> AudioStatus:
        return AudioStatus(
            card="bluez_card.AC_12_34_56_78_90",
            profile=self.profile,
            codec="SBC",
            sink="bluez_output.AC_12_34_56_78_90.1",
        )

    def find_sink(self, address: str) -> str:
        return "bluez_output.AC_12_34_56_78_90.1"

    def find_source(self, address: str) -> str:
        return "bluez_input.AC_12_34_56_78_90"


def snapshot(left: int = 82, right: int = 79, case: int = 65) -> BatterySnapshot:
    return BatterySnapshot(
        cells=[
            BatteryCell("left", left),
            BatteryCell("right", right),
            BatteryCell("case", case),
        ],
        source="test",
    )
