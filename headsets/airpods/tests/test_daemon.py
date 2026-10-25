import json
import tempfile
import unittest
from pathlib import Path

from airpods.config import Config
from airpods.daemon import Daemon
from airpods.mock import MOCK_ADDRESS, MockBackend
from tests.helpers import FakeAudio, FakeClock, FakeNotifier, snapshot


class LowBatteryBackend(MockBackend):
    def battery(self, address):
        self.battery_calls += 1
        return snapshot(left=15, right=79, case=5)


class DaemonTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_path = Path(self.tmp.name) / "state.json"
        self.config = Config(
            address=MOCK_ADDRESS,
            battery_interval=60,
            scan_interval=120,
            scan_duration=8,
            connect_interval=30,
            path=Path(self.tmp.name) / "config.json",
        )
        self.clock = FakeClock()
        self.backend = MockBackend()
        self.audio = FakeAudio()
        self.notifier = FakeNotifier()
        self.daemon = Daemon(
            backend=self.backend,
            config=self.config,
            audio=self.audio,
            notifier=self.notifier,
            state_path=self.state_path,
            clock=self.clock,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_first_tick_reads_battery_and_writes_state(self):
        self.daemon.tick()
        self.assertEqual(self.backend.battery_calls, 1)
        self.assertTrue(self.state_path.exists())
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertTrue(state["device"]["connected"])
        self.assertEqual(state["battery"]["lowest"], 65)
        self.assertEqual(state["audio"]["profile"], "a2dp-sink")
        self.assertTrue(any("connected" in title for title, _, _ in self.notifier.messages))
        self.assertIn("L 82%", self.notifier.messages[0][1])

    def test_battery_poll_respects_interval(self):
        self.daemon.tick()
        self.clock.advance(30)
        self.daemon.tick()
        self.assertEqual(self.backend.battery_calls, 1)
        self.clock.advance(31)
        self.daemon.tick()
        self.assertEqual(self.backend.battery_calls, 2)

    def test_auto_reconnect_after_disconnect(self):
        self.daemon.tick()
        self.backend.connected = False
        self.clock.advance(1)
        self.daemon.tick()
        self.assertTrue(self.backend.discovering)
        disconnected_state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertFalse(disconnected_state["device"]["connected"])
        self.assertTrue(any("disconnected" in title for title, _, _ in self.notifier.messages))

        # Still inside the 8 second discovery window.
        self.clock.advance(1)
        self.daemon.tick()
        self.assertFalse(self.backend.connected)

        # Discovery window over: stop scanning and connect.
        self.clock.advance(7)
        self.daemon.tick()
        self.assertTrue(self.backend.connected)
        self.assertFalse(self.backend.discovering)

    def test_low_battery_notifications(self):
        self.daemon.backend = LowBatteryBackend()
        self.daemon.tick()
        self.clock.advance(61)
        self.daemon.tick()
        titles = [title for title, _, _ in self.notifier.messages]
        self.assertTrue(any("Critically low" in title for title in titles), titles)
        self.assertTrue(any("Low battery" in title for title in titles), titles)

    def test_scan_stops_when_window_ends_without_connecting(self):
        self.daemon.tick()
        self.backend.connected = False
        self.backend.paired = False
        self.backend.trusted = False
        self.clock.advance(1)
        self.daemon.tick()
        self.assertTrue(self.backend.discovering)
        self.clock.advance(8)
        self.daemon.tick()
        self.assertFalse(self.backend.discovering)
        self.assertFalse(self.backend.connected)
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertFalse(state["discovering"])

    def test_audio_profile_is_retried_when_card_is_late(self):
        self.audio = FakeAudio(fail_first=1)
        self.daemon.audio = self.audio
        self.daemon.tick()
        self.assertFalse(self.daemon._audio_checked)
        self.clock.advance(16)
        self.daemon.tick()
        self.assertTrue(self.daemon._audio_checked)
        self.assertEqual(self.audio.applied, ["a2dp-sink"])

    def test_hfp_sets_default_input_source(self):
        self.config.audio_profile = "hfp"
        self.daemon.tick()
        self.assertEqual(self.audio.profile, "headset-head-unit")
        self.assertEqual(self.audio.default_source, "bluez_input.AC_12_34_56_78_90")
        self.assertTrue(self.daemon._audio_checked)

    def test_hfp_retries_until_default_input_source_appears(self):
        self.config.audio_profile = "hfp"
        self.audio = FakeAudio(source_fail_first=1)
        self.daemon.audio = self.audio

        self.daemon.tick()
        self.assertIsNone(self.audio.default_source)
        self.assertFalse(self.daemon._audio_checked)

        self.clock.advance(16)
        self.daemon.tick()
        self.assertEqual(self.audio.default_source, "bluez_input.AC_12_34_56_78_90")
        self.assertTrue(self.daemon._audio_checked)
        self.assertEqual(self.audio.source_calls, 2)

    def test_a2dp_does_not_set_default_input_source(self):
        self.config.set_default_sink = True
        self.daemon.tick()
        self.assertEqual(self.audio.default_sink, "bluez_output.AC_12_34_56_78_90.1")
        self.assertIsNone(self.audio.default_source)
        self.assertEqual(self.audio.source_calls, 0)

    def test_timer_does_not_reenter(self):
        self.daemon._busy = True
        self.daemon._on_timer()
        self.assertEqual(self.backend.battery_calls, 0)

    def test_no_auto_connect_when_disabled(self):
        self.config.auto_connect = False
        self.daemon.tick()
        self.backend.connected = False
        self.clock.advance(1)
        self.daemon.tick()
        self.assertFalse(self.backend.discovering)
        self.clock.advance(500)
        self.daemon.tick()
        self.assertFalse(self.backend.connected)

    def test_state_is_not_rewritten_when_unchanged(self):
        self.daemon.tick()
        first = self.state_path.stat().st_mtime_ns
        self.clock.advance(1)
        self.daemon.tick()
        second = self.state_path.stat().st_mtime_ns
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
