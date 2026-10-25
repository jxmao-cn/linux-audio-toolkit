import unittest

from airpods.models import BatteryCell, BatterySnapshot, Device, detect_model, is_airpods


class ModelDetectionTests(unittest.TestCase):
    def test_generations_and_variants(self):
        cases = {
            "AirPods Pro (2nd generation)": "AirPods Pro (2nd generation)",
            "AirPods Pro 2": "AirPods Pro (2nd generation)",
            "AirPods Pro": "AirPods Pro",
            "AirPods Max": "AirPods Max",
            "AirPods (3rd generation)": "AirPods (3rd generation)",
            "AirPods 4": "AirPods 4",
            "AirPods 4 (ANC)": "AirPods 4 (ANC)",
            "AirPods 4 with Active Noise Cancellation": "AirPods 4 (ANC)",
            "AirPods 3": "AirPods (3rd generation)",
            "AirPods (2nd generation)": "AirPods (2nd generation)",
            "AirPods": "AirPods",
            "Steven's AirPods": "AirPods",
            "俊翔的AirPods": "AirPods",
            "我的AirPods Pro": "AirPods Pro",
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(detect_model(name), expected)

    def test_non_airpods_name_is_preserved(self):
        self.assertEqual(detect_model("WH-1000XM4"), "WH-1000XM4")
        self.assertFalse(is_airpods("WH-1000XM4"))

    def test_airpods_name_without_word_boundary(self):
        # Chinese characters are word characters for Python's \b, so a name
        # like "俊翔的AirPods" has no boundary before "AirPods".
        self.assertTrue(is_airpods("俊翔的AirPods"))
        device = Device(address="AC:12:34:56:78:90", name="俊翔的AirPods")
        self.assertTrue(device.is_airpods)
        self.assertEqual(device.model, "AirPods")

    def test_empty_name(self):
        self.assertEqual(detect_model(""), "Unknown device")

    def test_device_properties(self):
        device = Device(
            address="AC:12:34:56:78:90",
            name="AirPods Pro",
            alias="My Pods",
            connected=True,
            rssi=-42,
            manufacturer_data={0x004C: b"\x07\x19"},
        )
        self.assertTrue(device.is_airpods)
        self.assertTrue(device.apple)
        self.assertEqual(device.model, "AirPods Pro")
        self.assertEqual(device.display_name, "My Pods")
        payload = device.to_dict()
        self.assertEqual(payload["address"], "AC:12:34:56:78:90")
        self.assertTrue(payload["is_airpods"])

    def test_battery_snapshot(self):
        snapshot = BatterySnapshot(
            cells=[
                BatteryCell("left", 82),
                BatteryCell("right", 79),
                BatteryCell("case", 65),
            ]
        )
        self.assertTrue(snapshot.available)
        self.assertEqual(snapshot.lowest, 65)
        self.assertEqual(snapshot.cell("right").level, 79)
        self.assertIsNone(snapshot.cell("unknown"))
        self.assertEqual(snapshot.to_dict()["lowest"], 65)

    def test_battery_snapshot_unavailable(self):
        snapshot = BatterySnapshot(cells=[BatteryCell("left", None)])
        self.assertFalse(snapshot.available)
        self.assertIsNone(snapshot.lowest)


if __name__ == "__main__":
    unittest.main()
