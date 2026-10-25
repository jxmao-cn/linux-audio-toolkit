import unittest
from unittest.mock import patch

from airpods.audio import AudioController, Card, _match_bluez_name, choose_profile, parse_cards
from airpods.constants import BLUETOOTH_SINK_RE

PACTL_CARDS = """Card #5
\tName: bluez_card.AC_12_34_56_78_90
\tDriver: module-bluez5-device.c
\tOwner Module: 42
\tProperties:
\t\tdevice.string = "AC:12:34:56:78:90"
\t\tdevice.api = "bluez"
\tProfiles:
\t\ta2dp-sink: High Fidelity Playback (A2DP Sink, codec SBC)
\t\ta2dp-sink-sbc_xq: High Fidelity Playback (A2DP Sink, codec SBC-XQ)
\t\theadset-head-unit: Headset Head Unit (HSP/HFP, codec CVSD)
\t\toff: Off
\tActive Profile: a2dp-sink
\tPorts:
\t\theadset-output: Headset (priority: 0)
\tActive Port: headset-output
Card #6
\tName: alsa_card.pci-0000_00_1f.3
\tDriver: module-alsa-card.c
\tProfiles:
\t\toff: Off
\tActive Profile: off
"""

PULSEAUDIO_HFP_CARDS = """Card #5
\tName: bluez_card.AC_12_34_56_78_90
\tProfiles:
\t\ta2dp_sink: High Fidelity Playback (A2DP Sink, codec SBC)
\t\thandsfree_head_unit: Handsfree Head Unit (HFP, codec mSBC)
\t\theadset_audio_gateway: Headset Audio Gateway (HSP)
\tActive Profile: a2dp_sink
"""


class PactlParsingTests(unittest.TestCase):
    def test_parse_cards(self):
        cards = parse_cards(PACTL_CARDS)
        self.assertEqual(len(cards), 2)
        bluetooth = cards[0]
        self.assertEqual(bluetooth.name, "bluez_card.AC_12_34_56_78_90")
        self.assertEqual(bluetooth.active_profile, "a2dp-sink")
        self.assertEqual(bluetooth.codec, "SBC")
        self.assertIn("headset-head-unit", bluetooth.profiles)
        self.assertNotIn("headset-output", bluetooth.profiles)
        self.assertTrue(bluetooth.is_bluetooth)
        self.assertFalse(cards[1].is_bluetooth)

    def test_profile_choices(self):
        card = parse_cards(PACTL_CARDS)[0]
        self.assertEqual(choose_profile(card, "a2dp"), "a2dp-sink")
        self.assertEqual(choose_profile(card, "hfp"), "headset-head-unit")

    def test_prefers_short_hfp_profile(self):
        card = Card(
            profiles={
                "headset-head-unit": "HSP/HFP",
                "headset-head-unit-cvsd": "HSP/HFP CVSD",
            }
        )
        self.assertEqual(choose_profile(card, "hfp"), "headset-head-unit")

    def test_pulseaudio_underscore_profiles_and_audio_gateway_exclusion(self):
        card = parse_cards(PULSEAUDIO_HFP_CARDS)[0]
        self.assertEqual(choose_profile(card, "a2dp"), "a2dp_sink")
        self.assertEqual(choose_profile(card, "hfp"), "handsfree_head_unit")

    def test_pulseaudio_headset_head_unit_alias(self):
        card = Card(profiles={"headset_head_unit": "Headset Head Unit (HFP)"})
        self.assertEqual(choose_profile(card, "hfp"), "headset_head_unit")

    def test_hfp_does_not_select_audio_gateway(self):
        card = Card(profiles={"headset_audio_gateway": "Headset Audio Gateway"})
        self.assertIsNone(choose_profile(card, "hfp"))

    def test_unknown_kind(self):
        with self.assertRaises(ValueError):
            choose_profile(Card(), "surround")

    def test_sink_name_matching(self):
        self.assertTrue(
            _match_bluez_name(BLUETOOTH_SINK_RE, "bluez_output.AC_12_34_56_78_90.1", "AC:12:34:56:78:90")
        )
        self.assertTrue(
            _match_bluez_name(
                BLUETOOTH_SINK_RE,
                "bluez_sink.AC_12_34_56_78_90.a2dp_sink",
                "AC:12:34:56:78:90",
            )
        )
        self.assertFalse(
            _match_bluez_name(BLUETOOTH_SINK_RE, "bluez_output.11_22_33_44_55_66.1", "AC:12:34:56:78:90")
        )

    def test_find_card_and_status(self):
        controller = AudioController()
        card = parse_cards(PACTL_CARDS)[0]
        with patch.object(controller, "cards", return_value=[card]), patch.object(
            controller, "find_sink", return_value="bluez_output.AC_12_34_56_78_90.1"
        ), patch.object(controller, "find_source", return_value="bluez_input.AC_12_34_56_78_90"):
            self.assertIs(controller.find_card("AC:12:34:56:78:90"), card)
            self.assertIsNone(controller.find_card("00:00:00:00:00:00"))
            status = controller.status("AC:12:34:56:78:90")
        self.assertEqual(status.profile, "a2dp-sink")
        self.assertEqual(status.codec, "SBC")
        self.assertTrue(status.connected)
        self.assertEqual(status.to_dict()["sink"], "bluez_output.AC_12_34_56_78_90.1")

    def test_empty_status(self):
        controller = AudioController()
        with patch.object(controller, "cards", return_value=[]):
            status = controller.status("AC:12:34:56:78:90")
        self.assertFalse(status.connected)
        self.assertEqual(status.to_dict()["card"], "")


if __name__ == "__main__":
    unittest.main()
