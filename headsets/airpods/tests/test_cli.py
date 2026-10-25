import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from airpods.cli import main
from airpods.config import Config
from airpods.mock import MockBackend


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config_path = Path(self.tmp.name) / "config.json"

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["--mock", "--config", str(self.config_path), *args])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_status_json(self):
        code, output, _ = self.run_cli("status", "--json")
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertEqual(payload["device"]["address"], "AC:12:34:56:78:90")
        self.assertEqual(payload["battery"]["lowest"], 65)
        self.assertEqual(payload["audio"]["profile"], "a2dp-sink")

    def test_status_human(self):
        code, output, _ = self.run_cli("status")
        self.assertEqual(code, 0)
        self.assertIn("AirPods Pro (2nd generation)", output)
        self.assertIn("Battery", output)

    def test_battery_json(self):
        code, output, _ = self.run_cli("battery", "--json")
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertEqual(payload["source"], "mock")
        self.assertEqual(len(payload["cells"]), 3)

    def test_scan(self):
        code, output, _ = self.run_cli("scan", "-t", "0.1")
        self.assertEqual(code, 0)
        self.assertIn("device(s) found", output)

    def test_pair_saves_address(self):
        code, output, _ = self.run_cli("pair", "-t", "0.1", "-y")
        self.assertEqual(code, 0)
        self.assertIn("Paired", output)
        config = Config.load(self.config_path)
        self.assertEqual(config.address, "AC:12:34:56:78:90")

    def test_pair_bredr(self):
        code, output, _ = self.run_cli("pair", "--bredr", "-t", "0.1", "-y")
        self.assertEqual(code, 0)
        self.assertIn("Paired", output)

    def test_scan_bredr(self):
        code, output, _ = self.run_cli("scan", "--bredr", "-t", "0.1")
        self.assertEqual(code, 0)
        self.assertIn("device(s) found", output)

    def test_forget(self):
        code, output, _ = self.run_cli("forget")
        self.assertEqual(code, 0)
        self.assertIn("Removed", output)

    def test_audio_switch(self):
        code, output, _ = self.run_cli("audio", "hfp")
        self.assertEqual(code, 0)
        self.assertIn("HFP", output)

    def test_disconnected_status_exit_code(self):
        backend = MockBackend()
        backend.connected = False
        with patch("airpods.cli._make_backend", return_value=backend):
            code, output, _ = self.run_cli("status")
        self.assertEqual(code, 1)
        self.assertIn("not connected", output)

    def test_missing_device_is_a_clean_error(self):
        backend = MockBackend()
        with patch("airpods.cli._make_backend", return_value=backend), patch.object(
            backend, "devices", return_value=[]
        ):
            code, _output, stderr = self.run_cli("status")
        self.assertEqual(code, 1)
        self.assertIn("No AirPods are paired", stderr + _output)

    def test_version(self):
        stdout = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as context:
                main(["--version"])
        self.assertEqual(context.exception.code, 0)
        self.assertIn("airpodsctl", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
