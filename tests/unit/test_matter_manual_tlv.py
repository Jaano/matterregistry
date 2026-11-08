"""TLV extension, 21-digit manual codes, and multi-device QR payloads (Matter 1.5.1 §5.1)."""

from __future__ import annotations

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.matter import (
    compute_manual_code,
    decode_manual_code,
    decode_setup_payload,
    decode_tlv_extension,
    split_setup_payloads,
    verify_manual_code,
)
from app.models import Device, FieldSource, Property, PropertyType
from app.services import apply_scan_manual_code, apply_scan_payload
from tests.unit.test_matter_discovery_caps import _payload


def _ctx_string(tag: int, text: str) -> bytes:
    raw = text.encode()
    return bytes([(1 << 5) | 0x0C, tag, len(raw)]) + raw


def _ctx_uint1(tag: int, value: int) -> bytes:
    return bytes([(1 << 5) | 0x04, tag, value])


def _ctx_uint2(tag: int, value: int) -> bytes:
    return bytes([(1 << 5) | 0x05, tag]) + value.to_bytes(2, "little")


def _struct(*elements: bytes) -> bytes:
    return bytes([0x15]) + b"".join(elements) + bytes([0x18])


# ── manual codes ──────────────────────────────────────────────────────────────


def test_eleven_digit_known_vector():
    assert compute_manual_code(20202021, 3840) == "34970112332"


def test_eleven_digit_decode_round_trip():
    mc = decode_manual_code("34970112332")
    assert mc.passcode == 20202021
    assert mc.short_discriminator == 0xF
    assert mc.vendor_id is None
    assert mc.product_id is None


def test_twenty_one_digit_round_trip():
    code = compute_manual_code(20202021, 3840, 0xFFF1, 0x8001)
    assert len(code) == 21
    assert code[0] == "7"  # VID_PID_PRESENT << 2 | top 2 discriminator bits
    assert code[10:15] == "65521"
    assert code[15:20] == "32769"
    assert verify_manual_code(code)
    mc = decode_manual_code(code)
    assert (mc.passcode, mc.short_discriminator) == (20202021, 0xF)
    assert (mc.vendor_id, mc.product_id) == (0xFFF1, 0x8001)


def test_twenty_one_digit_shares_eleven_digit_body():
    short = compute_manual_code(20202021, 3840)
    long = compute_manual_code(20202021, 3840, 0xFFF1, 0x8001)
    assert long[1:10] == short[1:10]


def test_corrupted_twenty_one_digit_code_rejected():
    code = compute_manual_code(20202021, 3840, 0xFFF1, 0x8001)
    bad = code[:-1] + str((int(code[-1]) + 1) % 10)
    with pytest.raises(ValueError, match="Verhoeff"):
        decode_manual_code(bad)


@pytest.mark.parametrize("code", ["", "1234567890", "123456789012", "1" * 20, "1" * 22])
def test_wrong_length_manual_code_rejected(code):
    assert verify_manual_code(code) is False


# ── TLV extension ─────────────────────────────────────────────────────────────


def test_tlv_serial_devices_timeout_and_manufacturer_tag():
    tlv = _struct(
        _ctx_string(0x00, "SN-12345"),
        _ctx_uint1(0x03, 4),
        _ctx_uint2(0x04, 900),
        _ctx_uint1(0x80, 7),
    )
    decoded = decode_tlv_extension(tlv)
    assert decoded is not None
    assert decoded.serial_number == "SN-12345"
    assert decoded.number_of_devices == 4
    assert decoded.commissioning_timeout == 900
    assert decoded.manufacturer_specific == {0x80: 7}


def test_tlv_integer_serial_number():
    decoded = decode_tlv_extension(_struct(_ctx_uint2(0x00, 4242)))
    assert decoded is not None
    assert decoded.serial_number == "4242"


@pytest.mark.parametrize(
    "data",
    [None, b"", b"\x01\x02", bytes([0x15, (1 << 5) | 0x0C, 0x00, 0x09, 0x41]), bytes([0x15])],
)
def test_tlv_absent_or_malformed_is_none(data):
    assert decode_tlv_extension(data) is None


def test_tlv_survives_payload_round_trip():
    tlv = _struct(_ctx_string(0x00, "ABC123"))
    sp = decode_setup_payload(_payload(0x04, tlv=tlv))
    assert sp.tlv_extension is not None
    assert sp.tlv_extension.startswith(tlv)
    decoded = decode_tlv_extension(sp.tlv_extension)
    assert decoded is not None
    assert decoded.serial_number == "ABC123"
    assert sp.passcode == 20202021


# ── multi-device QR ───────────────────────────────────────────────────────────


def test_split_single_payload_unchanged():
    payload = _payload(0x04)
    assert split_setup_payloads(payload) == [payload]


def test_split_multi_payload_with_and_without_repeated_prefix():
    a, b = _payload(0x04), _payload(0x02, passcode=20202022)
    assert split_setup_payloads(f"{a}*{b[3:]}") == [a, b]
    assert split_setup_payloads(f"{a}*{b}") == [a, b]


def test_decode_multi_payload_gives_clear_error():
    a, b = _payload(0x04), _payload(0x02, passcode=20202022)
    with pytest.raises(ValueError, match="Multi-device QR code \\(2 payloads\\)"):
        decode_setup_payload(f"{a}*{b[3:]}")


# ── scan flow ─────────────────────────────────────────────────────────────────


@pytest.fixture
def session():
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(eng)
    with Session(eng) as s:
        yield s


def _device(session: Session, **over) -> Device:
    dev = Device(name="Test", **over)
    session.add(dev)
    session.flush()
    return dev


def test_scan_payload_prefills_serial_from_tlv(session):
    dev = _device(session)
    tlv = _struct(_ctx_string(0x00, "SN-777"))
    apply_scan_payload(session, dev, _payload(0x04, tlv=tlv))
    assert dev.serial == "SN-777"
    assert dev.serial_source == FieldSource.scanned


def test_scan_payload_keeps_existing_serial(session):
    dev = _device(session, serial="KEEP-ME", serial_source=FieldSource.user)
    tlv = _struct(_ctx_string(0x00, "SN-777"))
    apply_scan_payload(session, dev, _payload(0x04, tlv=tlv))
    assert dev.serial == "KEEP-ME"


def test_scan_multi_payload_rejected(session):
    dev = _device(session)
    a, b = _payload(0x04), _payload(0x02, passcode=20202022)
    with pytest.raises(ValueError, match="Multi-device"):
        apply_scan_payload(session, dev, f"{a}*{b[3:]}")


def test_scan_twenty_one_digit_code_sets_product_ids(session):
    dev = _device(session)
    code = compute_manual_code(20202021, 3840, 0xFFF1, 0x8001)
    apply_scan_manual_code(session, dev, code)
    session.flush()
    session.refresh(dev)
    assert dev.product_record is not None
    assert dev.product_record.vendor_id == 0xFFF1
    assert dev.product_record.product_id == 0x8001
    stored = {
        p.type: p.value
        for p in session.exec(select(Property).where(Property.device_id == dev.id)).all()
    }
    assert stored[PropertyType.setup_pin] == "20202021"
    assert stored[PropertyType.discriminator] == "3840"
    assert stored[PropertyType.manual_code] == "34970112332"


def test_scan_eleven_digit_code_stored_as_entered(session):
    dev = _device(session)
    apply_scan_manual_code(session, dev, "3497-011-2332")
    stored = {
        p.type: p.value
        for p in session.exec(select(Property).where(Property.device_id == dev.id)).all()
    }
    assert stored[PropertyType.manual_code] == "34970112332"
