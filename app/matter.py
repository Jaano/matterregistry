"""Matter setup-payload utilities.

Decodes MT: QR payloads per Matter spec §5.1.4 (base-38 + bit-field extraction),
computes 11-digit manual setup codes with Verhoeff check digit, and renders QR SVGs
via segno.

chip.setup_payload is not present in python-matter-server 6.x (the package ships only
the WS-client SDK, not the native CHIP bindings). This module implements the public
Matter spec algorithm directly - the spec is normative and the bit layout is fixed.
"""

from __future__ import annotations

from dataclasses import dataclass

import segno

# Base-38 alphabet (Matter spec §5.1.4.1, Table 38)
_B38 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ-."
_B38_IDX = {c: i for i, c in enumerate(_B38)}

# Bit-field widths (Matter spec §5.1.4.1)
_F_VERSION = 3
_F_VID = 16
_F_PID = 16
_F_FLOW = 2
_F_CAPS = 8
_F_DISC = 12
_F_PASS = 27
_F_PAD = 4  # padding to 88-bit / 11-byte boundary


@dataclass
class SetupPayload:
    version: int
    vendor_id: int
    product_id: int
    custom_flow: int
    discovery_capabilities: int
    discriminator: int
    passcode: int
    tlv_extension: bytes | None  # kept verbatim per §6b


def _b38_decode(s: str) -> bytes:
    """Decode a base-38 string to bytes (little-endian groups)."""
    try:
        indices = [_B38_IDX[c] for c in s]
    except KeyError as exc:
        raise ValueError(f"Invalid base-38 character: {exc}") from exc

    result = bytearray()
    i = 0
    length = len(indices)
    while i < length:
        remaining = length - i
        if remaining >= 5:
            # 5 chars → 3 bytes
            val = (
                indices[i]
                + indices[i + 1] * 38
                + indices[i + 2] * 1444  # 38**2
                + indices[i + 3] * 54872  # 38**3
                + indices[i + 4] * 2085136  # 38**4
            )
            result += val.to_bytes(3, "little")
            i += 5
        elif remaining >= 4:
            # 4 chars → 2 bytes
            val = indices[i] + indices[i + 1] * 38 + indices[i + 2] * 1444 + indices[i + 3] * 54872
            result += val.to_bytes(2, "little")
            i += 4
        elif remaining >= 2:
            # 2 chars → 1 byte
            val = indices[i] + indices[i + 1] * 38
            result += val.to_bytes(1, "little")
            i += 2
        else:
            raise ValueError("Unexpected odd trailing base-38 character")
    return bytes(result)


def _read_bits(data: bytes, offset: int, length: int) -> int:
    """Extract `length` bits starting at bit `offset` (little-endian bit order)."""
    val = 0
    for i in range(length):
        byte_idx = (offset + i) // 8
        bit_idx = (offset + i) % 8
        if byte_idx < len(data) and (data[byte_idx] >> bit_idx) & 1:
            val |= 1 << i
    return val


def decode_setup_payload(raw: str) -> SetupPayload:
    """Decode an 'MT:...' string into structured fields.

    Raises ValueError on malformed input.
    """
    raw = raw.strip()
    if not raw.upper().startswith("MT:"):
        raise ValueError("Setup payload must start with 'MT:'")
    encoded = raw[3:]
    if not encoded:
        raise ValueError("Empty setup payload after 'MT:'")

    try:
        data = _b38_decode(encoded.upper())
    except ValueError:
        raise

    if len(data) < 11:
        raise ValueError(f"Decoded payload too short: {len(data)} bytes, expected ≥11")

    offset = 0
    version = _read_bits(data, offset, _F_VERSION)
    offset += _F_VERSION
    vendor_id = _read_bits(data, offset, _F_VID)
    offset += _F_VID
    product_id = _read_bits(data, offset, _F_PID)
    offset += _F_PID
    custom_flow = _read_bits(data, offset, _F_FLOW)
    offset += _F_FLOW
    caps = _read_bits(data, offset, _F_CAPS)
    offset += _F_CAPS
    discriminator = _read_bits(data, offset, _F_DISC)
    offset += _F_DISC
    passcode = _read_bits(data, offset, _F_PASS)
    offset += _F_PASS
    offset += _F_PAD  # skip padding bits

    tlv = bytes(data[offset // 8 :]) if (offset // 8) < len(data) else None
    if tlv == b"":
        tlv = None

    if passcode in (
        0,
        11111111,
        22222222,
        33333333,
        44444444,
        55555555,
        66666666,
        77777777,
        88888888,
        99999999,
        12345678,
        87654321,
    ):
        raise ValueError("Invalid passcode (reserved value)")

    return SetupPayload(
        version=version,
        vendor_id=vendor_id,
        product_id=product_id,
        custom_flow=custom_flow,
        discovery_capabilities=caps,
        discriminator=discriminator,
        passcode=passcode,
        tlv_extension=tlv,
    )


# Verhoeff algorithm tables (for 11-digit manual code check digit)
_V_D = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9],
    [1, 2, 3, 4, 0, 6, 7, 8, 9, 5],
    [2, 3, 4, 0, 1, 7, 8, 9, 5, 6],
    [3, 4, 0, 1, 2, 8, 9, 5, 6, 7],
    [4, 0, 1, 2, 3, 9, 5, 6, 7, 8],
    [5, 9, 8, 7, 6, 0, 4, 3, 2, 1],
    [6, 5, 9, 8, 7, 1, 0, 4, 3, 2],
    [7, 6, 5, 9, 8, 2, 1, 0, 4, 3],
    [8, 7, 6, 5, 9, 3, 2, 1, 0, 4],
    [9, 8, 7, 6, 5, 4, 3, 2, 1, 0],
]
_V_P = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9],
    [1, 5, 7, 6, 2, 8, 3, 0, 9, 4],
    [5, 8, 0, 3, 7, 9, 6, 1, 4, 2],
    [8, 9, 1, 6, 0, 4, 3, 5, 2, 7],
    [9, 4, 5, 3, 1, 2, 6, 8, 7, 0],
    [4, 2, 8, 6, 5, 7, 3, 9, 0, 1],
    [2, 7, 9, 3, 8, 0, 6, 4, 1, 5],
    [7, 0, 4, 6, 9, 1, 3, 2, 5, 8],
]
_V_INV = [0, 4, 3, 2, 1, 5, 6, 7, 8, 9]


def _verhoeff_checksum(digits: list[int]) -> int:
    """Return the Verhoeff check digit for a list of digit ints (right-to-left)."""
    c = 0
    for i, d in enumerate(reversed(digits)):
        c = _V_D[c][_V_P[(i + 1) % 8][d]]
    return _V_INV[c]


def compute_manual_code(passcode: int, discriminator: int) -> str:
    """Return the 11-digit manual setup code with Verhoeff check digit.

    Per Matter spec §5.1.3 / CHIP SDK ManualSetupPayloadGenerator:
      chunk1 (1 digit) = bits[3:2] of 4-bit short discriminator    (= bits[11:10] of 12-bit disc)
      chunk2 (5 digits, 16 bits) = bits[1:0] of short disc << 14   (= bits[9:8] of 12-bit disc)
                                  | passcode & 0x3FFF              (low 14 bits of passcode)
      chunk3 (4 digits, 13 bits) = passcode >> 14                  (high 13 bits of passcode)
      check digit (1 digit, Verhoeff)
    The full 4-bit short discriminator is split across chunk1 and chunk2.
    """
    short_disc_4bit = (discriminator >> 8) & 0xF
    chunk1 = (short_disc_4bit >> 2) & 0x3
    chunk2 = ((short_disc_4bit & 0x3) << 14) | (passcode & 0x3FFF)
    chunk3 = (passcode >> 14) & 0x1FFF
    raw = f"{chunk1:01d}{chunk2:05d}{chunk3:04d}"
    digits = [int(c) for c in raw]
    check = _verhoeff_checksum(digits)
    return raw + str(check)


def verify_manual_code(code: str) -> bool:
    """Return True if the 11-digit code has a valid Verhoeff check digit."""
    code = code.replace("-", "").strip()
    if len(code) != 11 or not code.isdigit():
        return False
    digits = [int(c) for c in code]
    # Verhoeff validation: running checksum over all 11 digits should be 0
    c = 0
    for i, d in enumerate(reversed(digits)):
        c = _V_D[c][_V_P[i % 8][d]]
    return c == 0


def render_qr_svg(raw_payload: str) -> bytes:
    """Render an SVG QR for the raw 'MT:...' string using segno."""
    qr = segno.make(raw_payload, error="M")
    import io

    buf = io.BytesIO()
    qr.save(buf, kind="svg", scale=4, border=4)
    return buf.getvalue()
