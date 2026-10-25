"""Constants shared by the airpodsd modules."""

from __future__ import annotations

import re

APP_NAME = "airpodsd"
VERSION = "0.1.0"

BLUEZ_SERVICE = "org.bluez"
BLUEZ_ROOT = "/org/bluez"
AGENT_MANAGER_IFACE = "org.bluez.AgentManager1"
AGENT_IFACE = "org.bluez.Agent1"
ADAPTER_IFACE = "org.bluez.Adapter1"
DEVICE_IFACE = "org.bluez.Device1"
BATTERY_IFACE = "org.bluez.Battery1"
GATT_SERVICE_IFACE = "org.bluez.GattService1"
GATT_CHARACTERISTIC_IFACE = "org.bluez.GattCharacteristic1"
OBJECT_MANAGER_IFACE = "org.freedesktop.DBus.ObjectManager"
PROPERTIES_IFACE = "org.freedesktop.DBus.Properties"

# Standard Bluetooth GATT battery service / characteristic.
BATTERY_SERVICE_UUID = "0000180f-0000-1000-8000-00805f9b34fb"
BATTERY_LEVEL_UUID = "00002a19-0000-1000-8000-00805f9b34fb"

# Apple's Bluetooth SIG company identifier.
APPLE_COMPANY_ID = 0x004C

AIRPODS_NAME_RE = re.compile(r"airpods", re.IGNORECASE)

# Ordered from most to least specific.  AirPods names are controlled by Apple
# and are a much more reliable model signal than the undocumented Apple
# Continuity manufacturer data, which BlueZ does not always expose.
MODEL_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern, re.IGNORECASE), model)
    for pattern, model in (
        (r"airpods\s+pro\s*(?:\(?\s*2nd\s*(?:gen(?:eration)?)?\s*\)?|2\b)", "AirPods Pro (2nd generation)"),
        (r"airpods\s+pro\b", "AirPods Pro"),
        (r"airpods\s+max\b", "AirPods Max"),
        (
            r"airpods\s*4\b.*\b(?:anc|noise\s*cancell?ation)\b",
            "AirPods 4 (ANC)",
        ),
        (r"airpods\s*(?:\(?\s*4th\s*(?:gen(?:eration)?)?\s*\)?|\b4\b)", "AirPods 4"),
        (r"airpods\s*(?:\(?\s*3rd\s*(?:gen(?:eration)?)?\s*\)?|\b3\b)", "AirPods (3rd generation)"),
        (r"airpods\s*(?:\(?\s*2nd\s*(?:gen(?:eration)?)?\s*\)?|\b2\b)", "AirPods (2nd generation)"),
        (r"airpods\s*(?:\(?\s*1st\s*(?:gen(?:eration)?)?\s*\)?|\b1\b)", "AirPods (1st generation)"),
        (r"airpods", "AirPods"),
    )
)

# PulseAudio / PipeWire names.
A2DP_PROFILE = "a2dp-sink"
HFP_PROFILE = "headset-head-unit"
BLUETOOTH_CARD_PREFIX = "bluez_card."
BLUETOOTH_SINK_RE = re.compile(
    r"^bluez_(?:output|sink)\.(?P<address>[0-9A-Fa-f_]+)(?:\..*)?$"
)
BLUETOOTH_SOURCE_RE = re.compile(
    r"^bluez_(?:input|source)\.(?P<address>[0-9A-Fa-f_]+)(?:\..*)?$"
)

# Default labels for the (unlabelled) battery cells AirPods expose.  The
# ordering is a heuristic; users can override it in config.json.
DEFAULT_BATTERY_LABELS = ("left", "right", "case")

# Bluetooth controller modes from /etc/bluetooth/main.conf.
VALID_CONTROLLER_MODES = ("dual", "bredr", "le")
