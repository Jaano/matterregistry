from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse, Response
from pydantic import BaseModel
from sqlmodel import Session, col, select

from ..audit import log as audit_log
from ..database import get_session
from ..matter import render_qr_svg
from ..models import (
    SOURCED_FIELDS,
    Device,
    DeviceLink,
    DeviceLinkSource,
    DeviceStatus,
    FieldSource,
    Product,
    Property,
    PropertyType,
)
from ..services import (
    ProtocolMismatchError,
    apply_scan_fields,
    apply_scan_manual_code,
    apply_scan_payload,
    set_field,
)
from .schemas import DeviceCreate, DeviceOut, DeviceUpdate, PropertyCreate, PropertyOut

router = APIRouter(prefix="/devices", tags=["devices"])


class ScanRequest(BaseModel):
    payload: str | None = None
    manual_code: str | None = None
    vid: int | None = None
    pid: int | None = None
    discriminator: int | None = None
    passcode: int | None = None
    protocol: str | None = None
    homekit_setup_id: str | None = None
    homekit_category: int | None = None


def _load_device(id: str, session: Session) -> Device:
    device = session.get(Device, id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    return device


def _device_out(device: Device, session: Session) -> DeviceOut:
    properties = session.exec(select(Property).where(Property.device_id == device.id)).all()
    out = DeviceOut.model_validate(device)
    out.properties = [PropertyOut.model_validate(c) for c in properties]
    out.sources = {
        f: getattr(device, f"{f}_source", FieldSource.generated).value for f in SOURCED_FIELDS
    }
    if device.product_record:
        out.sources.update(
            {
                "vendor": device.product_record.vendor_source.value,
                "product": device.product_record.name_source.value,
                "device_model": device.product_record.model_source.value,
                "vendor_id": device.product_record.vendor_id_source.value,
                "product_id": device.product_record.product_id_source.value,
            }
        )
    # Populate ha_device_id from DeviceLink (not a Device column any more)
    link = session.exec(
        select(DeviceLink)
        .where(DeviceLink.device_id == device.id)  # type: ignore[attr-defined]
        .where(DeviceLink.integration == "ha_core")  # type: ignore[attr-defined]
    ).first()
    if link:
        out.ha_device_id = link.external_id
        out.sources["ha_device_id"] = (
            "user" if link.link_source == DeviceLinkSource.manual else "ha"
        )
    else:
        out.sources["ha_device_id"] = FieldSource.generated.value
    return out


@router.get("", response_model=list[DeviceOut])
def list_devices(
    session: Session = Depends(get_session),
    q: str = "",
    status: str = "",
    vendor: str = "",
    room: str = "",
):
    from sqlmodel import or_

    stmt = select(Device).join(Product)
    if q:
        pattern = f"%{q}%"
        stmt = stmt.where(
            or_(
                Device.name.ilike(pattern),  # type: ignore[attr-defined]
                Device.room.ilike(pattern),  # type: ignore[union-attr]
                col(Product.vendor).ilike(pattern),
                col(Product.name).ilike(pattern),
                col(Product.model).ilike(pattern),
                Device.serial.ilike(pattern),  # type: ignore[union-attr]
                Device.notes.ilike(pattern),  # type: ignore[union-attr]
            )
        )
    if status:
        stmt = stmt.where(Device.status == status)
    else:
        stmt = stmt.where(col(Device.status) != DeviceStatus.hidden)
    if vendor:
        stmt = stmt.where(Product.vendor == vendor)
    if room:
        stmt = stmt.where(Device.room == room)
    devices = session.exec(stmt).all()
    return [DeviceOut.model_validate(d) for d in devices]


@router.post("", response_model=DeviceOut, status_code=201)
def create_device(data: DeviceCreate, session: Session = Depends(get_session)):
    values = data.model_dump()
    product_record_id = values.pop("product_record_id")
    if product_record_id and not session.get(Product, product_record_id):
        raise HTTPException(status_code=422, detail="Product not found")
    device = Device(product_record_id=product_record_id, **values)
    # Stamp user provenance on every non-None field supplied by the caller.
    for f in SOURCED_FIELDS:
        if getattr(device, f, None) is not None:
            setattr(device, f"{f}_source", FieldSource.user)
    session.add(device)
    audit_log(
        session, action="device.create", entity=f"device:{device.id}", reason="api.devices.create"
    )
    session.commit()
    session.refresh(device)
    return _device_out(device, session)


@router.get("/{id}", response_model=DeviceOut)
def get_device(id: str, session: Session = Depends(get_session)):
    device = _load_device(id, session)
    return _device_out(device, session)


@router.patch("/{id}", response_model=DeviceOut)
def update_device(id: str, data: DeviceUpdate, session: Session = Depends(get_session)):
    device = _load_device(id, session)
    for field, value in data.model_dump(exclude_unset=True).items():
        if field == "product_record_id":
            if value is None or not session.get(Product, value):
                raise HTTPException(status_code=422, detail="Product not found")
            setattr(device, field, value)
            continue
        if field in SOURCED_FIELDS:
            if getattr(device, field) != value:
                set_field(device, field, value, FieldSource.user)
        else:
            setattr(device, field, value)
    device.updated_at = datetime.now(UTC)
    session.add(device)
    audit_log(session, action="device.update", entity=f"device:{id}", reason="api.devices.update")
    session.commit()
    session.refresh(device)
    return _device_out(device, session)


@router.delete("/{id}", status_code=204)
def delete_device(id: str, session: Session = Depends(get_session)):
    device = _load_device(id, session)
    # Deleting a device does not cascade at the DB level (no ON DELETE CASCADE
    # on device_link.device_id), so links to other integrations would be left
    # as orphans - which then silently disappear from link pickers forever
    # since the picker only excludes by external_id, not device existence.
    links = session.exec(
        select(DeviceLink).where(DeviceLink.device_id == id)  # type: ignore[attr-defined]
    ).all()
    for link in links:
        session.delete(link)
    audit_log(session, action="device.delete", entity=f"device:{id}", reason="api.devices.delete")
    session.delete(device)
    session.commit()


@router.post("/{device_id}/properties", response_model=PropertyOut, status_code=201)
def add_property(
    device_id: str,
    data: PropertyCreate,
    session: Session = Depends(get_session),
):
    _load_device(device_id, session)
    cred = Property(device_id=device_id, source=FieldSource.user, **data.model_dump())
    session.add(cred)
    audit_log(
        session,
        action="property.create",
        entity=f"property:{cred.id}",
        reason="api.devices.add_property",
    )
    session.commit()
    session.refresh(cred)
    return PropertyOut.model_validate(cred)


@router.post("/{id}/scan", response_model=DeviceOut)
def scan_device(id: str, data: ScanRequest, session: Session = Depends(get_session)):
    """Decode MT: / X-HM:// payload (or manual code / individual fields) and store credentials."""
    device = _load_device(id, session)

    try:
        if data.payload:
            apply_scan_payload(session, device, data.payload)
        elif data.manual_code:
            apply_scan_manual_code(session, device, data.manual_code, protocol=data.protocol)
        elif data.passcode is not None and data.discriminator is not None:
            if data.protocol == "homekit":
                apply_scan_fields(
                    session,
                    device,
                    data.passcode,
                    0,
                    protocol="homekit",
                    homekit_setup_id=data.homekit_setup_id or str(data.discriminator).zfill(4),
                    homekit_category=data.homekit_category,
                )
            else:
                apply_scan_fields(
                    session, device, data.passcode, data.discriminator, data.vid, data.pid
                )
        else:
            raise HTTPException(
                status_code=400,
                detail="Provide one of: payload (MT:... / X-HM://), manual_code, or passcode+discriminator",
            )
    except ProtocolMismatchError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    audit_log(session, action="device.scan", entity=f"device:{id}", reason="api.devices.scan")
    session.commit()
    session.refresh(device)
    return _device_out(device, session)


@router.get("/{id}/qr.svg")
def device_qr_svg(id: str, session: Session = Depends(get_session)):
    """Return a fresh SVG QR regenerated from the stored qr_payload property."""
    _load_device(id, session)
    qr_cred = session.exec(
        select(Property).where(
            Property.device_id == id,
            Property.type == PropertyType.qr_payload,
        )
    ).first()
    if not qr_cred:
        raise HTTPException(status_code=404, detail="No QR payload stored for this device")
    svg = render_qr_svg(qr_cred.value)
    return Response(content=svg, media_type="image/svg+xml")


@router.get("/{id}/manual-code", response_class=PlainTextResponse)
def device_manual_code(id: str, session: Session = Depends(get_session)):
    """Return the 11-digit manual code formatted as XXXX-XXX-XXXX.

    Recomputes from the stored passcode + discriminator so old (pre-fix) stored
    manual-code rows don't need a re-scan to show correctly.
    """
    _load_device(id, session)
    pin_cred = session.exec(
        select(Property).where(
            Property.device_id == id,
            Property.type == PropertyType.setup_pin,
        )
    ).first()
    disc_cred = session.exec(
        select(Property).where(
            Property.device_id == id,
            Property.type == PropertyType.discriminator,
        )
    ).first()
    if not (pin_cred and disc_cred):
        raise HTTPException(status_code=404, detail="No manual code stored for this device")
    from ..matter import compute_manual_code

    code = compute_manual_code(int(pin_cred.value), int(disc_cred.value))
    return f"{code[:4]}-{code[4:7]}-{code[7:]}"


# ── HA link / unlink ───────────────────────────────────────────────────────────


class HALinkRequest(BaseModel):
    ha_device_id: str | None


@router.patch("/{id}/ha-link", response_model=DeviceOut)
def device_ha_link(
    id: str,
    data: HALinkRequest,
    session: Session = Depends(get_session),
):
    """Set or clear the HA device link for a MatterRegistry device."""
    device = _load_device(id, session)
    from datetime import datetime

    existing_link = session.exec(
        select(DeviceLink)
        .where(DeviceLink.device_id == id)  # type: ignore[attr-defined]
        .where(DeviceLink.integration == "ha_core")  # type: ignore[attr-defined]
    ).first()

    if data.ha_device_id:
        # User explicitly chose an HA device - manual link.
        if existing_link:
            existing_link.external_id = data.ha_device_id
            existing_link.link_source = DeviceLinkSource.manual
            existing_link.linked_at = datetime.now(UTC)
            session.add(existing_link)
        else:
            session.add(
                DeviceLink(
                    device_id=id,
                    integration="ha_core",
                    external_id=data.ha_device_id,
                    link_source=DeviceLinkSource.manual,
                    linked_at=datetime.now(UTC),
                )
            )
        audit_log(session, action="device.ha_link", entity=f"device:{id}", reason="ui.manual_link")
    else:
        if existing_link:
            session.delete(existing_link)
        audit_log(
            session, action="device.ha_unlink", entity=f"device:{id}", reason="ui.manual_link"
        )

    device.updated_at = datetime.now(UTC)
    session.add(device)
    session.commit()
    session.refresh(device)
    return _device_out(device, session)


# ── HA live state ──────────────────────────────────────────────────────────────


@router.get("/{id}/ha-state")
async def device_ha_state(id: str, request: Request, session: Session = Depends(get_session)):
    """Return fresh live state from HA for the device's linked ha_device_id."""
    _load_device(id, session)  # 404 guard
    link = session.exec(
        select(DeviceLink)
        .where(DeviceLink.device_id == id)  # type: ignore[attr-defined]
        .where(DeviceLink.integration == "ha_core")  # type: ignore[attr-defined]
    ).first()
    if not link:
        raise HTTPException(status_code=404, detail="Device is not linked to HA")

    hc = getattr(request.app.state, "ha_client", None)
    if hc is None:
        raise HTTPException(status_code=503, detail="HA Core integration is disabled")

    try:
        state = await hc.get_device_state(link.external_id)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"HA state fetch failed: {exc}") from exc

    return state


# ── Matter unlink ──────────────────────────────────────────────────────────────


@router.delete("/{id}/matter-link", status_code=204)
def device_matter_unlink(
    id: str,
    session: Session = Depends(get_session),
):
    """Delete all DeviceFabricMembership rows for this device.

    Manual recovery path for re-paired devices that have a stale node_id.
    The next Matter Server sync will re-create the membership with the new
    node_id via deterministic matter_unique_id correlation.
    """
    _load_device(id, session)
    from ..models import DeviceFabricMembership

    memberships = session.exec(
        select(DeviceFabricMembership).where(DeviceFabricMembership.device_id == id)
    ).all()
    for mem in memberships:
        session.delete(mem)
    audit_log(
        session, action="device.matter_unlink", entity=f"device:{id}", reason="ui.manual_unlink"
    )
    session.commit()
    return Response(status_code=204)
