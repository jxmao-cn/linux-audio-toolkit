import tempfile
import unittest
from pathlib import Path

from airpods.state import clear_state, read_state, write_state


class StateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "nested" / "state.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_write_read_roundtrip(self):
        write_state({"device": {"address": "AC:12:34:56:78:90"}, "battery": 82}, self.path)
        state = read_state(self.path)
        self.assertIsNotNone(state)
        self.assertEqual(state["version"], 1)
        self.assertEqual(state["device"]["address"], "AC:12:34:56:78:90")
        self.assertIn("updated_at", state)

    def test_missing_state(self):
        self.assertIsNone(read_state(self.path))

    def test_invalid_state(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("not json", encoding="utf-8")
        self.assertIsNone(read_state(self.path))

    def test_clear_state(self):
        write_state({"x": 1}, self.path)
        clear_state(self.path)
        self.assertFalse(self.path.exists())
        clear_state(self.path)


if __name__ == "__main__":
    unittest.main()
