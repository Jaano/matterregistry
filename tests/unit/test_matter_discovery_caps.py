"""Discovery Capabilities Bitmask labels (Matter 1.5.1 Table 58)."""

from __future__ import annotations

import pytest

from app.matter import decode_setup_payload, discovery_capabilities_label

_B38 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ-."


def _b38_encode(data: bytes) -> str:
    out = []
    i = 0
    while i < len(data):
        chunk = data[i : i + 3]
        n = int.from_bytes(chunk, "little")
        chars = {3: 5, 2: 4, 1: 2}[len(chunk)]
        for _ in range(chars):
            out.append(_B38[n % 38])
            n //= 38
        i += 3
    return "".join(out)


def _payload(caps: int, *, passcode: int = 20202021, disc: int = 3840, tlv: bytes = b"") -> str:
    fields = [(0, 3), (0xFFF1, 16), (0x8001, 16), (0, 2), (caps, 8), (disc, 12), (passcode, 27)]
    value = 0
    offset = 0
    for val, width in fields:
        value |= val << offset
        offset += width
    return "MT:" + _b38_encode(value.to_bytes(11, "little") + tlv)


@pytest.mark.parametrize(
    ("caps", "expected"),
    [
        (0x00, "0x00"),
        (0x01, "SoftAP (pre-1.4)"),
        (0x02, "BLE"),
        (0x04, "On Network"),
        (0x08, "Wi-Fi PAF"),
        (0x10, "NFC"),
        (0x06, "BLE, On Network"),
        (0x1E, "BLE, On Network, Wi-Fi PAF, NFC"),
        (0x22, "BLE, 0x20"),
        (0xE0, "0xE0"),
    ],
)
def test_label(caps, expected):
    assert discovery_capabilities_label(caps) == expected


@pytest.mark.parametrize("caps", [0x02, 0x04, 0x08, 0x10, 0x1E])
def test_decoded_payload_carries_capability_bits(caps):
    decoded = decode_setup_payload(_payload(caps))
    assert decoded.discovery_capabilities == caps
    assert decoded.discriminator == 3840
    assert decoded.passcode == 20202021
