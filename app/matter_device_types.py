"""Bundled Matter 1.5.1 device type names from the upstream generated ID table.

Source (Apache-2.0):
https://github.com/project-chip/connectedhomeip/blob/bd73b5141729af92f3d021aeecaa53d9e0cadb89/docs/ids_and_codes/spec_device_types.md

To update, download the table from a pinned upstream commit and run
`python scripts/generate_matter_device_types.py spec_device_types.md <revision>`.
Replace the mapping below and update the Matter revision and source commit.
"""

from typing import TypeGuard

MAX_MATTER_DEVICE_TYPE_ID = 0xFFFFFFFF

MATTER_DEVICE_TYPES: dict[int, str] = {
    0x000A: "Door Lock",
    0x000B: "Door Lock Controller",
    0x000E: "Aggregator",
    0x000F: "Generic Switch",
    0x0011: "Power Source",
    0x0012: "OTA Requestor",
    0x0013: "Bridged Node",
    0x0014: "OTA Provider",
    0x0015: "Contact Sensor",
    0x0016: "Root Node",
    0x0017: "Solar Power",
    0x0018: "Battery Storage",
    0x0019: "Secondary Network Interface",
    0x0022: "Speaker",
    0x0023: "Casting Video Player",
    0x0024: "Content App",
    0x0027: "Mode Select",
    0x0028: "Basic Video Player",
    0x0029: "Casting Video Client",
    0x002A: "Video Remote Control",
    0x002B: "Fan",
    0x002C: "Air Quality Sensor",
    0x002D: "Air Purifier",
    0x0040: "Irrigation System",
    0x0041: "Water Freeze Detector",
    0x0042: "Water Valve",
    0x0043: "Water Leak Detector",
    0x0044: "Rain Sensor",
    0x0045: "Soil Sensor",
    0x0070: "Refrigerator",
    0x0071: "Temperature Controlled Cabinet",
    0x0072: "Room Air Conditioner",
    0x0073: "Laundry Washer",
    0x0074: "Robotic Vacuum Cleaner",
    0x0075: "Dishwasher",
    0x0076: "Smoke CO Alarm",
    0x0077: "Cook Surface",
    0x0078: "Cooktop",
    0x0079: "Microwave Oven",
    0x007A: "Extractor Hood",
    0x007B: "Oven",
    0x007C: "Laundry Dryer",
    0x0090: "Network Infrastructure Manager",
    0x0091: "Thread Border Router",
    0x0100: "On/Off Light",
    0x0101: "Dimmable Light",
    0x0103: "On/Off Light Switch",
    0x0104: "Dimmer Switch",
    0x0105: "Color Dimmer Switch",
    0x0106: "Light Sensor",
    0x0107: "Occupancy Sensor",
    0x010A: "On/Off Plug-in Unit",
    0x010B: "Dimmable Plug-In Unit",
    0x010C: "Color Temperature Light",
    0x010D: "Extended Color Light",
    0x010F: "Mounted On/Off Control",
    0x0110: "Mounted Dimmable Load Control",
    0x0130: "Joint Fabric Administrator",
    0x0140: "Intercom",
    0x0141: "Audio Doorbell",
    0x0142: "Camera",
    0x0143: "Video Doorbell",
    0x0144: "Floodlight Camera",
    0x0145: "Snapshot Camera",
    0x0146: "Chime",
    0x0147: "Camera Controller",
    0x0148: "Doorbell",
    0x0202: "Window Covering",
    0x0203: "Window Covering Controller",
    0x0230: "Closure",
    0x0231: "Closure Panel",
    0x023E: "Closure Controller",
    0x0301: "Thermostat",
    0x0302: "Temperature Sensor",
    0x0303: "Pump",
    0x0304: "Pump Controller",
    0x0305: "Pressure Sensor",
    0x0306: "Flow Sensor",
    0x0307: "Humidity Sensor",
    0x0309: "Heat Pump",
    0x030A: "Thermostat Controller",
    0x050C: "Energy EVSE",
    0x050D: "Device Energy Management",
    0x050F: "Water Heater",
    0x0510: "Electrical Sensor",
    0x0511: "Electrical Utility Meter",
    0x0512: "Meter Reference Point",
    0x0513: "Electrical Energy Tariff",
    0x0514: "Electrical Meter",
    0x0840: "Control Bridge",
    0x0850: "On/Off Sensor",
}


def is_matter_device_type_id(value: object) -> TypeGuard[int]:
    return type(value) is int and 0 < value <= MAX_MATTER_DEVICE_TYPE_ID


def format_matter_device_type(device_type_id: int, unknown_label: str) -> str:
    name = MATTER_DEVICE_TYPES.get(device_type_id, unknown_label)
    return f"{name} (0x{device_type_id:04X})"
