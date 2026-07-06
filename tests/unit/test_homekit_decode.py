"""HomeKit HAP payload decode/encode tests."""

import pytest

from app.homekit import (
    HAPSetupPayload,
    category_name,
    decode_payload,
    encode_payload,
    format_manual_code,
    validate_manual_code,
)


def test_decode_encode_round_trip():
    """Encode → decode → re-encode produces identical bits."""
    sp = HAPSetupPayload(
        version=0,
        category_id=5,  # Lightbulb
        setup_flags=2,  # IP only (no BLE, not paired)
        setup_code=12344321,
        setup_id="1A2B",
        paired=False,
        supports_ip=True,
        supports_ble=False,
    )
    payload = encode_payload(sp)
    assert payload.startswith("X-HM://")
    decoded = decode_payload(payload)
    assert decoded.version == sp.version
    assert decoded.category_id == sp.category_id
    assert decoded.setup_flags == sp.setup_flags
    assert decoded.setup_code == sp.setup_code
    assert decoded.setup_id == sp.setup_id
    assert decoded.paired == sp.paired
    assert decoded.supports_ip == sp.supports_ip
    assert decoded.supports_ble == sp.supports_ble

    # Re-encode and verify exact match
    re_encoded = encode_payload(decoded)
    assert re_encoded == payload


def test_decode_encode_round_trip_paired_ble():
    """Round trip with all flags set and different category."""
    sp = HAPSetupPayload(
        version=0,
        category_id=17,  # IP Camera
        setup_flags=7,  # paired + IP + BLE
        setup_code=98765432,
        setup_id="XYZW",
        paired=True,
        supports_ip=True,
        supports_ble=True,
    )
    payload = encode_payload(sp)
    decoded = decode_payload(payload)
    assert decoded.version == 0
    assert decoded.category_id == 17
    assert decoded.setup_code == 98765432
    assert decoded.setup_id == "XYZW"
    assert decoded.paired is True
    assert decoded.supports_ip is True
    assert decoded.supports_ble is True

    re_encoded = encode_payload(decoded)
    assert re_encoded == payload


def test_decode_payload_invalid_prefix():
    with pytest.raises(ValueError, match="must start with"):
        decode_payload("MT:...")
    with pytest.raises(ValueError, match="must start with"):
        decode_payload("invalid")


def test_decode_payload_too_short():
    # Anything shorter than 9 base-36 + 4-char Setup ID = 13 chars is rejected.
    with pytest.raises(ValueError, match="too short"):
        decode_payload("X-HM://ABCDEFGH")  # 8
    with pytest.raises(ValueError, match="too short"):
        decode_payload("X-HM://ABC")  # 3
    with pytest.raises(ValueError, match="too short"):
        decode_payload("X-HM://ABCDEFGHI")  # 9 - no room for a Setup ID
    with pytest.raises(ValueError, match="too short"):
        decode_payload("X-HM://ABCDEFGHIIJK")  # 12 - Setup ID truncated


def test_decode_real_payload_with_reserved_bits_and_trailing_data():
    """Real accessory payload: reserved bits set, 18 chars of trailing data.

    Vector captured from hardware (Setup ID 94QH, PIN 9638-3056). Locks the
    decoder against the standard HAP layout as emitted by a real device.
    """
    d = decode_payload("X-HM://0L6VX21CG94QH9COFZOP4Y8B8EQOHDS")
    assert d.setup_code == 96383056
    assert format_manual_code(d.setup_code) == "9638-3056"
    assert d.setup_id == "94QH"
    assert d.category_id == 5  # Lightbulb
    assert d.version == 0


def test_format_manual_code():
    assert format_manual_code(12344321) == "1234-4321"
    assert format_manual_code(1) == "0000-0001"
    assert format_manual_code(99999999) == "9999-9999"


def test_format_manual_code_too_large():
    with pytest.raises(ValueError, match="more than 8 digits"):
        format_manual_code(100000000)


def test_validate_manual_code_valid():
    assert validate_manual_code("12344321") == 12344321
    assert validate_manual_code("123-44-321") == 12344321
    assert validate_manual_code("00000001") == 1


def test_validate_manual_code_invalid():
    with pytest.raises(ValueError, match="exactly 8 digits"):
        validate_manual_code("1234567")
    with pytest.raises(ValueError, match="exactly 8 digits"):
        validate_manual_code("123456789")
    with pytest.raises(ValueError, match="only digits"):
        validate_manual_code("ABCDEFGH")


def test_validate_manual_code_reserved():
    with pytest.raises(ValueError, match="reserved"):
        validate_manual_code("12345678")
    with pytest.raises(ValueError, match="reserved"):
        validate_manual_code("11111111")


def test_validate_manual_code_zero():
    with pytest.raises(ValueError, match="must not be zero"):
        validate_manual_code("00000000")


def test_category_name():
    assert category_name(5) == "Lightbulb"
    assert category_name(6) == "Door Lock"
    assert category_name(999) == "Unknown (999)"


def test_decode_payload_round_trip_category_names():
    """Every known category round-trips."""
    from app.homekit import HAP_CATEGORIES

    for cat_id in HAP_CATEGORIES:
        sp = HAPSetupPayload(
            version=0,
            category_id=cat_id,
            setup_flags=2,
            setup_code=11112222,
            setup_id="ABCD",
            paired=False,
            supports_ip=True,
            supports_ble=False,
        )
        payload = encode_payload(sp)
        decoded = decode_payload(payload)
        assert decoded.category_id == cat_id, f"Category {cat_id} failed round-trip"
        assert encode_payload(decoded) == payload


def test_9_character_base36_range():
    """Verify that the 9-char base-36 encoding round-trips field extremes."""
    for version in (0, 7):  # 3-bit field
        for category_id in (1, 255):  # 8-bit field
            for flags in (0, 2, 7, 0xF):  # 4-bit field
                for setup_code in (1, 12344321, 2**27 - 1):  # 27-bit field
                    sp = HAPSetupPayload(
                        version=version,
                        category_id=category_id,
                        setup_flags=flags,
                        setup_code=setup_code,
                        setup_id="TEST",
                        paired=bool(flags & 0x1),
                        supports_ip=bool(flags & 0x2),
                        supports_ble=bool(flags & 0x4),
                    )
                    encoded = encode_payload(sp)
                    prefix = "X-HM://"
                    assert encoded.startswith(prefix)
                    body = encoded[len(prefix) :]
                    assert len(body) == 13  # 9 base-36 + 4 setup ID
                    b36_part = body[:9]
                    assert all(c in "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ" for c in b36_part)
                    decoded = decode_payload(encoded)
                    assert decoded.version == sp.version
                    assert decoded.category_id == sp.category_id
                    assert decoded.setup_flags == sp.setup_flags
                    assert decoded.setup_code == sp.setup_code
