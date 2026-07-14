"""Shared business logic called by both API routes and web routes."""

from datetime import UTC, datetime
from typing import Any

from sqlmodel import Session, select

from .audit import log as audit_log
from .homekit import (
    category_name as homekit_category_name,
)
from .homekit import (
    decode_payload as decode_homekit_payload,
)
from .homekit import (
    format_manual_code as format_homekit_manual_code,
)
from .homekit import (
    validate_manual_code as validate_homekit_manual_code,
)
from .matter import compute_manual_code, decode_setup_payload, verify_manual_code
from .models import (
    SOURCED_FIELDS,
    Attachment,
    Device,
    DeviceFabricMembership,
    DeviceProtocol,
    FieldSource,
    Property,
    PropertyType,
)


class ProtocolMismatchError(Exception):
    """Raised when a scan's protocol differs from the device's existing onboarding code."""


def set_field(device: Device, field: str, value: Any, source: FieldSource) -> bool:
    """Conditionally set a Device field if *source* priority >= current source priority.

    Returns True when the value was applied, False when it was blocked by a
    higher-priority existing source.  Only operates on fields listed in
    SOURCED_FIELDS; raises ValueError for unknown fields.
    """
    if field not in SOURCED_FIELDS:
        raise ValueError(f"set_field: '{field}' is not a sourced field")
    source_attr = f"{field}_source"
    current_raw = getattr(device, source_attr, FieldSource.generated)
    current = FieldSource(current_raw) if isinstance(current_raw, str) else current_raw
    if FieldSource.priority(source) >= FieldSource.priority(current):
        setattr(device, field, value)
        setattr(device, source_attr, source)
        return True
    return False


def _upsert_credential(
    session: Session,
    device_id: str,
    cred_type: PropertyType,
    value: str,
    source: FieldSource = FieldSource.scanned,
) -> None:
    """Insert or update a Property, provenance-gated like ``set_field``.

    An existing row's value is overwritten only when *source* has equal-or-
    higher priority than the row's current source; a strictly-lower source is
    ignored so a user-entered value is never clobbered by a later scan/sync.
    """
    existing = session.exec(
        select(Property).where(
            Property.device_id == device_id,
            Property.type == cred_type,
        )
    ).first()
    if existing:
        if FieldSource.priority(source) >= FieldSource.priority(existing.source):
            existing.value = value
            existing.source = source
            session.add(existing)
    else:
        session.add(Property(device_id=device_id, type=cred_type, value=value, source=source))


def _onboarding_types_for_device(device: Device) -> set[PropertyType]:
    """Return the set of PropertyType values that constitute an onboarding code for this device."""
    if device.protocol == DeviceProtocol.matter:
        return {PropertyType.qr_payload, PropertyType.manual_code}
    # HomeKit also uses qr_payload for the X-HM:// string; manual_code for 8-digit code
    return {PropertyType.qr_payload, PropertyType.manual_code}


def _ensure_protocol_match(
    device: Device, incoming_protocol: DeviceProtocol, session: Session
) -> None:
    """Raise ProtocolMismatchError if the device already has a stored onboarding
    code whose protocol differs from *incoming_protocol*."""
    onboarding_creds = session.exec(
        select(Property).where(
            Property.device_id == device.id,
            Property.type.in_(  # type: ignore[attr-defined]
                [PropertyType.qr_payload, PropertyType.manual_code]
            ),
        )
    ).all()
    if not onboarding_creds:
        return
    device_protocol = device.protocol
    if device_protocol != incoming_protocol:
        raise ProtocolMismatchError(
            f"This device already has a {device_protocol.value if device_protocol else 'unknown'} setup code. "
            f"HomeKit and Matter codes cannot mix on the same device."
        )


def apply_scan_payload(session: Session, device: Device, payload_str: str) -> None:
    """Decode an MT: or X-HM:// string and persist properties + metadata on the device.

    Routing is by prefix: MT: → Matter, X-HM:// → HomeKit.
    Cross-protocol scans on a device with an existing code are rejected.
    """
    raw = payload_str.strip()
    upper = raw.upper()

    if upper.startswith("X-HM://"):
        _ensure_protocol_match(device, DeviceProtocol.homekit, session)
        _apply_scan_homekit_payload(session, device, raw)
    elif upper.startswith("MT:"):
        _ensure_protocol_match(device, DeviceProtocol.matter, session)
        _apply_scan_matter_payload(session, device, raw)
    else:
        raise ValueError("Unrecognised payload prefix - expected MT: or X-HM://")


def _apply_scan_matter_payload(session: Session, device: Device, payload_str: str) -> None:
    sp = decode_setup_payload(payload_str)
    _upsert_credential(session, device.id, PropertyType.qr_payload, payload_str)
    _upsert_credential(session, device.id, PropertyType.setup_pin, str(sp.passcode))
    _upsert_credential(
        session,
        device.id,
        PropertyType.manual_code,
        compute_manual_code(sp.passcode, sp.discriminator),
    )
    _upsert_credential(session, device.id, PropertyType.discriminator, str(sp.discriminator))
    set_field(device, "vendor_id", sp.vendor_id, FieldSource.scanned)
    set_field(device, "product_id", sp.product_id, FieldSource.scanned)
    session.add(device)


def _apply_scan_homekit_payload(session: Session, device: Device, payload_str: str) -> None:
    sp = decode_homekit_payload(payload_str)
    _upsert_credential(session, device.id, PropertyType.qr_payload, payload_str)
    _upsert_credential(session, device.id, PropertyType.setup_pin, str(sp.setup_code))
    _upsert_credential(
        session,
        device.id,
        PropertyType.manual_code,
        format_homekit_manual_code(sp.setup_code),
    )
    _upsert_credential(session, device.id, PropertyType.discriminator, sp.setup_id)
    # Set protocol to homekit on first HomeKit scan
    device.protocol = DeviceProtocol.homekit
    session.add(device)


def apply_scan_manual_code(
    session: Session,
    device: Device,
    code: str,
    *,
    protocol: str | None = None,
) -> None:
    """Store credentials extracted from a manual setup code.

    For Matter: 11-digit code with Verhoeff check digit.
    For HomeKit: 8-digit code (no checksum).
    When *protocol* is given, it is used; otherwise detected from code length.
    """
    code = code.replace("-", "").strip()
    if protocol == "homekit" or (protocol is None and len(code) == 8):
        _ensure_protocol_match(device, DeviceProtocol.homekit, session)
        _apply_scan_homekit_manual_code(session, device, code)
    elif protocol == "matter" or (protocol is None and len(code) == 11):
        _ensure_protocol_match(device, DeviceProtocol.matter, session)
        _apply_scan_matter_manual_code(session, device, code)
    else:
        raise ValueError(
            f"Unrecognised manual code length: {len(code)} - expected 8 (HomeKit) or 11 (Matter)"
        )


def _apply_scan_matter_manual_code(session: Session, device: Device, code: str) -> None:
    """Store credentials extracted from an 11-digit Matter manual code.

    Reverses compute_manual_code(): recovers the 4-bit short discriminator
    (bits 8-11 of the original 12-bit discriminator - bits 0-7 are lost) and
    the full 27-bit passcode.
    """
    if not verify_manual_code(code):
        raise ValueError("Invalid manual code (Verhoeff check failed)")
    chunk1 = int(code[0])
    chunk2 = int(code[1:6])
    chunk3 = int(code[6:10])
    short_disc_4bit = ((chunk1 & 0x3) << 2) | ((chunk2 >> 14) & 0x3)
    pin = (chunk2 & 0x3FFF) | (chunk3 << 14)
    disc = short_disc_4bit << 8  # low 8 bits unrecoverable from manual code
    _upsert_credential(session, device.id, PropertyType.setup_pin, str(pin))
    _upsert_credential(session, device.id, PropertyType.manual_code, code)
    _upsert_credential(session, device.id, PropertyType.discriminator, str(disc))
    session.add(device)


def _apply_scan_homekit_manual_code(session: Session, device: Device, code: str) -> None:
    setup_code = validate_homekit_manual_code(code)
    formatted = format_homekit_manual_code(setup_code)
    _upsert_credential(session, device.id, PropertyType.setup_pin, str(setup_code))
    _upsert_credential(session, device.id, PropertyType.manual_code, formatted)
    device.protocol = DeviceProtocol.homekit
    session.add(device)


def compute_onboarding_display(device: Device, properties: list[Property]) -> dict[str, Any]:
    """Derive the onboarding fields the QR sticker (device detail print, bulk
    QR sheet) and the on-screen Onboarding tile both need: QR payload
    presence, formatted manual code, and decoded Matter/HomeKit metadata."""
    qr_payload = next((c for c in properties if c.type == PropertyType.qr_payload), None)
    pin_cred = next((c for c in properties if c.type == PropertyType.setup_pin), None)
    disc_cred = next((c for c in properties if c.type == PropertyType.discriminator), None)
    manual_formatted = None
    manual_plain = None
    is_homekit = device.protocol == DeviceProtocol.homekit if device.protocol else False
    if pin_cred and disc_cred:
        if is_homekit:
            manual_formatted = format_homekit_manual_code(int(pin_cred.value))
            manual_plain = str(int(pin_cred.value)).zfill(8)
        else:
            code = compute_manual_code(int(pin_cred.value), int(disc_cred.value))
            manual_formatted = f"{code[:4]}-{code[4:7]}-{code[7:]}"
            manual_plain = code
    mt_version = mt_flow_label = mt_disc_label = None
    hk_category = hk_setup_id = hk_paired = hk_supports_ip = hk_supports_ble = None
    if qr_payload:
        raw_payload = qr_payload.value
        if raw_payload.upper().startswith("X-HM://"):
            try:
                hk_sp = decode_homekit_payload(raw_payload)
                hk_category = homekit_category_name(hk_sp.category_id)
                hk_setup_id = hk_sp.setup_id
                hk_paired = hk_sp.paired
                hk_supports_ip = hk_sp.supports_ip
                hk_supports_ble = hk_sp.supports_ble
            except Exception:
                pass
        elif raw_payload.upper().startswith("MT:"):
            try:
                mt_sp = decode_setup_payload(raw_payload)
                mt_version = mt_sp.version
                mt_flow_label = {0: "Standard", 1: "User Action Required", 2: "Custom"}.get(
                    mt_sp.custom_flow, str(mt_sp.custom_flow)
                )
                caps = mt_sp.discovery_capabilities
                parts = []
                if caps & 0x01:
                    parts.append("SoftAP")
                if caps & 0x02:
                    parts.append("BLE")
                if caps & 0x04:
                    parts.append("On Network")
                mt_disc_label = ", ".join(parts) if parts else f"0x{caps:02X}"
            except Exception:
                pass
    return {
        "qr_payload": qr_payload,
        "manual_formatted": manual_formatted,
        "manual_plain": manual_plain,
        "is_homekit": is_homekit,
        "mt_version": mt_version,
        "mt_flow_label": mt_flow_label,
        "mt_disc_label": mt_disc_label,
        "hk_category": hk_category,
        "hk_setup_id": hk_setup_id,
        "hk_paired": hk_paired,
        "hk_supports_ip": hk_supports_ip,
        "hk_supports_ble": hk_supports_ble,
    }


_MERGE_SCALAR_FIELDS = [
    "room",
    "vendor",
    "product",
    "device_model",
    "vendor_id",
    "product_id",
    "serial",
    "hardware_version",
    "firmware_version",
    "matter_unique_id",
    "notes",
    "purchase_date",
    "warranty_until",
]

_MERGE_PREVIEW_LABELS = [
    ("Name", "name"),
    ("Room", "room"),
    ("Vendor", "vendor"),
    ("Product", "product"),
    ("Model", "device_model"),
    ("Serial", "serial"),
    ("Hardware version", "hardware_version"),
    ("Firmware version", "firmware_version"),
    ("Notes", "notes"),
    ("Purchase date", "purchase_date"),
    ("Warranty until", "warranty_until"),
]


def build_merge_preview(source: Device, target: Device) -> list[dict]:
    """Return per-field preview of which value wins after merge."""
    source_is_manual = source.name_source != FieldSource.ha
    target_is_manual = target.name_source != FieldSource.ha
    rows = []
    for label, field in _MERGE_PREVIEW_LABELS:
        sv = getattr(source, field)
        tv = getattr(target, field)
        if field == "name":
            winner = "source" if (source_is_manual and not target_is_manual) else "target"
        elif tv is not None:
            winner = "target"
        elif sv is not None:
            winner = "source"
        else:
            winner = None
        rows.append(
            {
                "label": label,
                "source_val": str(sv) if sv is not None else None,
                "target_val": str(tv) if tv is not None else None,
                "winner": winner,
                "conflict": sv is not None and tv is not None and str(sv) != str(tv),
            }
        )
    return rows


def merge_devices(session: Session, source_id: str, target_id: str) -> None:
    """Merge source into target, then delete source.

    Policy: target wins, source fills blanks.
    Name exception: the row without ha_device_id (manual side) always wins.
    """
    source = session.get(Device, source_id)
    target = session.get(Device, target_id)
    if source is None or target is None:
        raise ValueError("Device not found")

    if source.protocol != target.protocol:
        src_p = source.protocol.value if source.protocol else "unlabeled"
        tgt_p = target.protocol.value if target.protocol else "unlabeled"
        raise ProtocolMismatchError(
            f"Cannot merge a {src_p} device into a "
            f"{tgt_p} device - commissioning protocols must match."
        )

    source_is_manual = source.name_source != FieldSource.ha
    target_is_manual = target.name_source != FieldSource.ha

    for field in _MERGE_SCALAR_FIELDS:
        if getattr(target, field) is None:
            setattr(target, field, getattr(source, field))

    if source_is_manual and not target_is_manual:
        target.name = source.name

    target.updated_at = datetime.now(UTC)
    session.add(target)

    for cred in session.exec(select(Property).where(Property.device_id == source_id)).all():
        cred.device_id = target_id
        session.add(cred)

    for att in session.exec(select(Attachment).where(Attachment.device_id == source_id)).all():
        att.device_id = target_id
        session.add(att)

    for mem in session.exec(
        select(DeviceFabricMembership).where(DeviceFabricMembership.device_id == source_id)
    ).all():
        mem.device_id = target_id
        session.add(mem)

    # Move the ha_core DeviceLink: if target has no link, adopt source's; else drop source's.
    from .models import DeviceLink

    src_link = session.exec(
        select(DeviceLink)
        .where(DeviceLink.device_id == source_id)  # type: ignore[attr-defined]
        .where(DeviceLink.integration == "ha_core")  # type: ignore[attr-defined]
    ).first()
    if src_link:
        tgt_link = session.exec(
            select(DeviceLink)
            .where(DeviceLink.device_id == target_id)  # type: ignore[attr-defined]
            .where(DeviceLink.integration == "ha_core")  # type: ignore[attr-defined]
        ).first()
        if tgt_link is None:
            src_link.device_id = target_id
            session.add(src_link)
        else:
            session.delete(src_link)

    session.flush()
    session.delete(source)
    audit_log(
        session,
        action="device.merge",
        entity=f"device:{target_id}",
        reason=f"merged_from:{source_id}",
    )


def apply_scan_fields(
    session: Session,
    device: Device,
    passcode: int,
    discriminator: int,
    vid: int | None = None,
    pid: int | None = None,
    *,
    protocol: str | None = None,
    homekit_setup_id: str | None = None,
    homekit_category: int | None = None,
) -> None:
    """Store credentials from individually-entered fields.

    When *protocol* is "homekit", *discriminator* carries the 4-char setup ID
    (base-36-decoded to an int; we cast to str) and *passcode* is the 8-digit
    HomeKit setup code.  *vid*/*pid* are ignored for HomeKit.
    """
    if protocol == "homekit":
        _ensure_protocol_match(device, DeviceProtocol.homekit, session)
        setup_id = homekit_setup_id or str(discriminator).zfill(4)
        _upsert_credential(session, device.id, PropertyType.setup_pin, str(passcode))
        _upsert_credential(
            session,
            device.id,
            PropertyType.manual_code,
            format_homekit_manual_code(passcode),
        )
        _upsert_credential(session, device.id, PropertyType.discriminator, setup_id)
        device.protocol = DeviceProtocol.homekit
        session.add(device)
        return

    # Matter path
    _upsert_credential(session, device.id, PropertyType.setup_pin, str(passcode))
    _upsert_credential(
        session, device.id, PropertyType.manual_code, compute_manual_code(passcode, discriminator)
    )
    _upsert_credential(session, device.id, PropertyType.discriminator, str(discriminator))
    if vid is not None:
        set_field(device, "vendor_id", vid, FieldSource.scanned)
    if pid is not None:
        set_field(device, "product_id", pid, FieldSource.scanned)
    session.add(device)
