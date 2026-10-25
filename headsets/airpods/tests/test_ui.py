import unittest

from airpods.models import AudioStatus
from airpods.ui import (
    battery_bar,
    format_audio,
    format_battery,
    profile_label,
    set_color_enabled,
)
from tests.helpers import snapshot


class UiTests(unittest.TestCase):
    def tearDown(self):
        set_color_enabled(None)

    def test_format_battery(self):
        self.assertEqual(format_battery(snapshot()), "L 82%   R 79%   Case 65%")
        self.assertEqual(format_battery(None), "unavailable")

    def test_battery_bar(self):
        self.assertEqual(battery_bar(50, 10), "█████░░░░░")
        self.assertEqual(battery_bar(100, 4), "████")
        self.assertEqual(battery_bar(None, 4), "░░░░")

    def test_audio(self):
        status = AudioStatus(
            card="bluez_card.AC_12_34_56_78_90",
            profile="a2dp-sink",
            codec="SBC",
            sink="bluez_output.AC_12_34_56_78_90.1",
        )
        self.assertEqual(
            format_audio(status),
            "A2DP (SBC) · bluez_output.AC_12_34_56_78_90.1",
        )
        self.assertEqual(format_audio(None), "not available")

    def test_profile_labels(self):
        self.assertEqual(profile_label("a2dp-sink"), "A2DP")
        self.assertEqual(profile_label("headset-head-unit"), "HFP")
        self.assertEqual(profile_label("off"), "off")

    def test_colors_can_be_disabled(self):
        set_color_enabled(False)
        self.assertEqual(format_battery(snapshot()), "L 82%   R 79%   Case 65%")


if __name__ == "__main__":
    unittest.main()
