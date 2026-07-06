"""HomeKit HAP setup-payload utilities.

Decodes X-HM:// QR payloads per the HAP Accessory Protocol specification:
base-36 encoding of a 46-bit "Setup Payload" plus a 4-character Setup ID.

Bit layout (46 bits, MSB first per the HAP spec; LSB bit numbering below):
    bits 43-45 (3 bits):  Version
    bits 39-42 (4 bits):  Reserved (spec says zero; real devices may set it -
                          decoded but not validated)
    bits 31-38 (8 bits):  Category Identifier
    bits 27-30 (4 bits):  Setup Flags
    bits  0-26 (27 bits): Setup Code (8-digit PIN as an integer)

The body is ``9 base-36 chars + 4-char Setup ID``; some accessories append
extra vendor/product data after the Setup ID, which is tolerated and ignored.

The 8-digit manual code (XXXX-XXXX) has no checksum - HomeKit does not
define one.  Validation rejects non-digit, wrong-length, and reserved-value
codes.

HomeKit category identifiers map to human-readable names via a fixed static
dict (HAP spec §13.2.1).
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass

# Base-36 alphabet (same as HAP uses - uppercase)
_B36 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_B36_IDX = {c: i for i, c in enumerate(_B36)}

# Bit-field widths and shifts (LSB numbering; setup code occupies the low bits)
_F_CODE_W = 27
_F_FLAGS_W = 4
_F_CATEGORY_W = 8
_F_RESERVED_W = 4
_F_VERSION_W = 3

_CODE_OFFSET = 0
_FLAGS_OFFSET = _CODE_OFFSET + _F_CODE_W
_CATEGORY_OFFSET = _FLAGS_OFFSET + _F_FLAGS_W
_RESERVED_OFFSET = _CATEGORY_OFFSET + _F_CATEGORY_W
_VERSION_OFFSET = _RESERVED_OFFSET + _F_RESERVED_W
# Total: 27 + 4 + 8 + 4 + 3 = 46 bits

# HomeKit category ID → human-readable name (HAP spec §13.2.1)
HAP_CATEGORIES: dict[int, str] = {
    1: "Other",
    2: "Bridge",
    3: "Fan",
    4: "Garage Door Opener",
    5: "Lightbulb",
    6: "Door Lock",
    7: "Outlet",
    8: "Switch",
    9: "Thermostat",
    10: "Sensor",
    11: "Alarm System",
    12: "Door",
    13: "Window",
    14: "Window Covering",
    15: "Programmable Switch",
    16: "Range Extender",
    17: "IP Camera",
    18: "Video Doorbell",
    19: "Air Purifier",
    20: "Air Heater",
    21: "Air Conditioner",
    22: "Air Humidifier",
    23: "Air Dehumidifier",
    24: "Apple TV",
    25: "HomePod",
    26: "Speaker",
    27: "AirPort",
    28: "Sprinkler",
    29: "Faucet",
    30: "Shower Head",
    31: "Television",
    32: "Target Controller",
    33: "Router",
    34: "Audio Receiver",
    35: "TV Set Top Box",
    36: "TV Streaming Stick",
}


@dataclass
class HAPSetupPayload:
    version: int
    category_id: int
    setup_flags: int
    setup_code: int
    setup_id: str  # 4-character alphanumeric
    paired: bool
    supports_ip: bool
    supports_ble: bool


def _b36_decode_nine(s: str) -> int:
    """Decode 9 base-36 chars into the 46-bit integer."""
    s = s.upper().strip()
    if len(s) != 9:
        raise ValueError(f"Expected 9 base-36 characters, got {len(s)}")
    v = 0
    for c in s:
        if c not in _B36_IDX:
            raise ValueError(f"Invalid base-36 character: {c!r}")
        v = v * 36 + _B36_IDX[c]
    return v


def _b36_encode_nine(value: int) -> str:
    """Encode a 46-bit integer as 9 base-36 characters."""
    if value < 0 or value >= 2**46:
        raise ValueError(f"Value {value} out of range for 46-bit encoding")
    chars = []
    for _ in range(9):
        chars.append(_B36[value % 36])
        value //= 36
    return "".join(reversed(chars))


def decode_payload(raw: str) -> HAPSetupPayload:
    """Decode an ``X-HM://`` string into structured fields.

    Raises ValueError on malformed input.
    """
    raw = raw.strip()
    upper = raw.upper()
    if not upper.startswith("X-HM://"):
        raise ValueError("Setup payload must start with 'X-HM://'")

    body = raw[len("X-HM://") :]
    if len(body) < 13:
        raise ValueError(
            f"Payload after 'X-HM://' too short: {len(body)} chars, "
            "expected ≥13 (9 base-36 + 4-char Setup ID)"
        )

    encoded = body[:9]
    setup_id = body[9:13]
    # body[13:], if present, is optional vendor/product data that some
    # accessories append after the Setup ID. It is preserved in the stored
    # verbatim payload but is not part of the HAP setup-payload fields.

    value = _b36_decode_nine(encoded)

    setup_code = (value >> _CODE_OFFSET) & ((1 << _F_CODE_W) - 1)
    flags = (value >> _FLAGS_OFFSET) & ((1 << _F_FLAGS_W) - 1)
    category_id = (value >> _CATEGORY_OFFSET) & ((1 << _F_CATEGORY_W) - 1)
    version = (value >> _VERSION_OFFSET) & ((1 << _F_VERSION_W) - 1)
    # Reserved bits (39-42) are intentionally NOT validated: real accessories
    # have been observed setting them non-zero, and the spec only requires a
    # compliant encoder to zero them - a decoder must tolerate any value.

    if setup_code == 0:
        raise ValueError("Setup code must not be zero")

    paired = bool(flags & 0x1)
    supports_ip = bool(flags & 0x2)
    supports_ble = bool(flags & 0x4)

    return HAPSetupPayload(
        version=version,
        category_id=category_id,
        setup_flags=flags,
        setup_code=setup_code,
        setup_id=setup_id,
        paired=paired,
        supports_ip=supports_ip,
        supports_ble=supports_ble,
    )


def encode_payload(sp: HAPSetupPayload) -> str:
    """Encode structured fields back into an ``X-HM://`` string."""
    if sp.setup_code <= 0 or sp.setup_code >= (1 << _F_CODE_W):
        raise ValueError(f"Setup code {sp.setup_code} out of 27-bit range")
    if len(sp.setup_id) != 4:
        raise ValueError(f"Setup ID must be exactly 4 characters, got {len(sp.setup_id)!r}")

    flags = sp.setup_flags & 0xF

    value = sp.setup_code << _CODE_OFFSET
    value |= flags << _FLAGS_OFFSET
    value |= sp.category_id << _CATEGORY_OFFSET
    value |= sp.version << _VERSION_OFFSET

    encoded = _b36_encode_nine(value)
    return f"X-HM://{encoded}{sp.setup_id}"


def format_manual_code(setup_code: int) -> str:
    """Format an 8-digit setup code as ``XXXX-XXXX``."""
    s = str(setup_code).zfill(8)
    if len(s) > 8:
        raise ValueError(f"Setup code {setup_code} has more than 8 digits")
    return f"{s[:4]}-{s[4:]}"


def validate_manual_code(code: str) -> int:
    """Validate an 8-digit HomeKit manual code and return the integer.

    Raises ValueError if the code is invalid (non-digit, wrong length,
    reserved value, or all-same digits).
    """
    code = code.replace("-", "").strip()
    if len(code) != 8:
        raise ValueError(f"HomeKit manual code must be exactly 8 digits, got {len(code)}")
    if not code.isdigit():
        raise ValueError("HomeKit manual code must contain only digits")
    value = int(code)
    if value == 0:
        raise ValueError("HomeKit setup code must not be zero")
    # Reserved values - reject trivially guessable codes
    if code in (
        "11111111",
        "22222222",
        "33333333",
        "44444444",
        "55555555",
        "66666666",
        "77777777",
        "88888888",
        "99999999",
        "12345678",
        "87654321",
    ):
        raise ValueError("HomeKit setup code is a reserved/trivial value")
    return value


def category_name(category_id: int) -> str:
    """Return the human-readable category name, or 'Unknown'."""
    return HAP_CATEGORIES.get(category_id, f"Unknown ({category_id})")


def setup_hash(setup_id: str, device_id: str) -> str:
    """Compute the HAP mDNS setup hash (``sh`` TXT field) for an accessory.

    ``sh = base64( SHA-512(setupID + deviceID)[:4] )`` where *setup_id* is the
    4-character Setup ID and *device_id* is the accessory's HAP Device ID
    (the mDNS ``id`` field, MAC-format, uppercase). Lets a discovered accessory
    be matched to the stored onboarding code whose Setup ID produces this hash.
    """
    digest = hashlib.sha512((setup_id + device_id.upper()).encode()).digest()[:4]
    return base64.b64encode(digest).decode()


# Precompute the reversed map for encode round-trip tests.
_CATEGORY_REVERSE: dict[str, int] = {v: k for k, v in HAP_CATEGORIES.items()}
