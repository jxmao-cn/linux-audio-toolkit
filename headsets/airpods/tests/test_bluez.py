import unittest

from airpods.bluez import BlueZBackend


class BlueZParsingTests(unittest.TestCase):
    def test_parse_device(self):
        props = {
            "Address": "AC:12:34:56:78:90",
            "Name": "AirPods Pro",
            "Alias": "AirPods Pro",
            "Icon": "audio-headphones",
            "Connected": True,
            "Paired": True,
            "Trusted": True,
            "RSSI": -48,
            "Adapter": "/org/bluez/hci0",
            "ManufacturerData": {76: bytearray([7, 25, 1])},
            "UUIDs": ["0000110b-0000-1000-8000-00805f9b34fb"],
            "Modalias": "usb:v004Cp1234d0001",
        }
        device = BlueZBackend._parse_device("/org/bluez/hci0/dev_AC_12_34_56_78_90", props)
        self.assertEqual(device.address, "AC:12:34:56:78:90")
        self.assertTrue(device.connected)
        self.assertTrue(device.paired)
        self.assertTrue(device.apple)
        self.assertTrue(device.is_airpods)
        self.assertEqual(device.manufacturer_data[76], b"\x07\x19\x01")
        self.assertEqual(device.rssi, -48)
        self.assertEqual(device.model, "AirPods Pro")

    def test_parse_device_without_optional_fields(self):
        device = BlueZBackend._parse_device(
            "/org/bluez/hci0/dev_11_22_33_44_55_66", {"Address": "11:22:33:44:55:66"}
        )
        self.assertIsNone(device.rssi)
        self.assertFalse(device.connected)
        self.assertFalse(device.is_airpods)

    def test_parse_adapter(self):
        adapter = BlueZBackend._parse_adapter(
            "/org/bluez/hci0",
            {
                "Address": "D4:F3:2D:34:AF:4D",
                "Name": "hci0",
                "Powered": True,
                "Discovering": False,
            },
        )
        self.assertEqual(adapter.id, "hci0")
        self.assertTrue(adapter.powered)

    def test_battery_cell_labels(self):
        backend = object.__new__(BlueZBackend)
        backend._labels = ["left", "right", "case"]
        single = backend._label_cells([82])
        self.assertEqual([(cell.label, cell.level) for cell in single], [("battery", 82)])
        triple = backend._label_cells([82, 79, 65])
        self.assertEqual(
            [(cell.label, cell.level) for cell in triple],
            [("left", 82), ("right", 79), ("case", 65)],
        )

    def test_uuid_normalisation(self):
        self.assertEqual(BlueZBackend._normalise_uuid(" 0000180F-0000-1000-8000-00805F9B34FB "), "0000180f-0000-1000-8000-00805f9b34fb")


if __name__ == "__main__":
    unittest.main()
