import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from airpods.config import Config, default_config_path, default_state_path
from airpods.errors import ConfigError


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "config.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_defaults(self):
        config = Config.from_dict({}, path=self.path)
        self.assertTrue(config.auto_connect)
        self.assertEqual(config.battery_labels, ["left", "right", "case"])
        self.assertEqual(config.audio_profile, "a2dp")
        self.assertEqual(config.scan_interval, 120)

    def test_address_is_normalised(self):
        config = Config.from_dict({"address": "ac:12:34:56:78:90"}, path=self.path)
        self.assertEqual(config.address, "AC:12:34:56:78:90")

    def test_invalid_values(self):
        invalid = [
            {"address": "not-a-mac"},
            {"auto_connect": 1},
            {"scan_interval": 1},
            {"battery_interval": "60"},
            {"battery_labels": []},
            {"audio_profile": "surround"},
            {"low_battery_threshold": 10, "critical_battery_threshold": 20},
        ]
        for payload in invalid:
            with self.subTest(payload=payload):
                with self.assertRaises(ConfigError):
                    Config.from_dict(payload, path=self.path)

    def test_save_and_load(self):
        config = Config.from_dict(
            {
                "address": "AC:12:34:56:78:90",
                "battery_labels": ["L", "R", "Case"],
                "notifications": False,
            },
            path=self.path,
        )
        saved = config.save()
        self.assertEqual(saved, self.path)
        self.assertTrue(self.path.exists())
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

        loaded = Config.load(self.path)
        self.assertEqual(loaded.address, "AC:12:34:56:78:90")
        self.assertEqual(loaded.battery_labels, ["L", "R", "Case"])
        self.assertFalse(loaded.notifications)

    def test_missing_file_uses_defaults(self):
        config = Config.load(self.path)
        self.assertTrue(config.auto_connect)
        self.assertEqual(config.path, self.path)

    def test_malformed_json(self):
        self.path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(ConfigError):
            Config.load(self.path)

    def test_non_object_json(self):
        self.path.write_text("[]", encoding="utf-8")
        with self.assertRaises(ConfigError):
            Config.load(self.path)

    def test_xdg_paths(self):
        with patch.dict(
            os.environ,
            {"XDG_CONFIG_HOME": "/tmp/cfg", "XDG_STATE_HOME": "/tmp/state"},
        ):
            self.assertEqual(default_config_path(), Path("/tmp/cfg/airpodsd/config.json"))
            self.assertEqual(default_state_path(), Path("/tmp/state/airpodsd/state.json"))

    def test_to_dict_does_not_include_path(self):
        config = Config.from_dict({}, path=self.path)
        self.assertNotIn("path", config.to_dict())
        json.dumps(config.to_dict())


if __name__ == "__main__":
    unittest.main()
