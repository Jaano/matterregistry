from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, col, or_, select

from ..audit import log as audit_log
from ..database import get_session
from ..i18n import get_t, resolve_lang
from ..models import (
    Attachment,
    Device,
    DeviceFabricMembership,
    DeviceLink,
    DeviceStatus,
    Fabric,
    FieldSource,
    HADeviceRecord,
    Property,
    PropertyType,
)
from ..settings import settings as app_settings

_TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

router = APIRouter()


def _url(request: Request, path: str) -> str:
    prefix = getattr(request.state, "ingress_path", "").rstrip("/")
    return prefix + "/" + path.lstrip("/")


def _ctx(request: Request, **kwargs) -> dict:
    lang = resolve_lang(
        request.cookies.get("mr_lang", ""),
        request.headers.get("accept-language", ""),
    )
    return {
        "request": request,
        "url": lambda p: _url(request, p),
        "t": get_t(lang),
        "lang": lang,
        **kwargs,
    }


# ── Devices ──────────────────────────────────────────────────────────────────


@router.get("/devices", response_class=HTMLResponse)
def device_list(
    request: Request,
    session: Session = Depends(get_session),
    q: str = "",
    status: str = "",
    vendor: str = "",
    room: str = "",
):
    stmt = select(Device)
    if q:
        pattern = f"%{q}%"
        stmt = stmt.where(
            or_(
                Device.name.ilike(pattern),  # type: ignore[attr-defined]
                Device.room.ilike(pattern),  # type: ignore[union-attr]
                Device.vendor.ilike(pattern),  # type: ignore[union-attr]
                Device.product.ilike(pattern),  # type: ignore[union-attr]
                Device.serial.ilike(pattern),  # type: ignore[union-attr]
                Device.notes.ilike(pattern),  # type: ignore[union-attr]
            )
        )
    if status:
        stmt = stmt.where(Device.status == status)
    else:
        stmt = stmt.where(col(Device.status) != DeviceStatus.hidden)
    if vendor:
        stmt = stmt.where(Device.vendor == vendor)
    if room:
        stmt = stmt.where(Device.room == room)

    devices = session.exec(stmt).all()

    # Comms flags - batch-load credentials to avoid N+1
    _ONBOARDING_TYPES = {PropertyType.qr_payload, PropertyType.manual_code}
    device_ids = [d.id for d in devices]
    all_creds = (
        session.exec(select(Property).where(Property.device_id.in_(device_ids))).all()  # type: ignore[attr-defined]
        if device_ids
        else []
    )
    _creds_by_device: dict[str, list] = {}
    for _c in all_creds:
        _creds_by_device.setdefault(_c.device_id, []).append(_c)

    device_comms: dict[str, dict] = {}
    for d in devices:
        creds = _creds_by_device.get(d.id, [])
        has_onboarding = any(c.type in _ONBOARDING_TYPES for c in creds)
        device_comms[d.id] = {"has_onboarding": has_onboarding}

    # Distinct values for filter dropdowns
    all_devices = session.exec(select(Device)).all()
    vendors = sorted({d.vendor for d in all_devices if d.vendor})
    rooms = sorted({d.room for d in all_devices if d.room})

    any_integration_configured = bool(
        getattr(request.app.state, "matter_client", None)
        or getattr(request.app.state, "otbr_client", None)
        or getattr(request.app.state, "ha_client", None)
        or getattr(request.app.state, "mdns_client", None)
    )

    from ..integrations.ha.client import HACoreClient
    from ..integrations.matter_server.server_client import MatterServerClient
    from ..integrations.mdns.client import MdnsClient
    from ..integrations.otbr.client import OTBRClient

    is_htmx = request.headers.get("HX-Request") == "true"
    ctx = _ctx(
        request,
        devices=devices,
        device_comms=device_comms,
        vendors=vendors,
        rooms=rooms,
        q=q,
        active_status=status,
        active_vendor=vendor,
        active_room=room,
        any_filter=bool(q or status or vendor or room),
        any_integration_configured=any_integration_configured,
        ms_long_name=MatterServerClient.long_name,
        otbr_long_name=OTBRClient.long_name,
        ha_long_name=HACoreClient.long_name,
        mdns_long_name=MdnsClient.long_name,
    )
    if is_htmx:
        return templates.TemplateResponse(request, "devices/_rows.html", ctx)
    return templates.TemplateResponse(request, "devices/list.html", ctx)


@router.get("/devices/new", response_class=HTMLResponse)
def device_new(request: Request):
    return templates.TemplateResponse(
        request,
        "devices/form.html",
        _ctx(request, device=None, statuses=list(DeviceStatus)),
    )


@router.post("/devices")
def device_create(
    request: Request,
    name: str = Form(...),
    vendor: str = Form(""),
    product: str = Form(""),
    device_model: str = Form(""),
    room: str = Form(""),
    serial: str = Form(""),
    notes: str = Form(""),
    status: str = Form("active"),
    warranty_until: str = Form(""),
    qr_payload: str = Form(""),
    network_type: list[str] = Form(default=[]),
    session: Session = Depends(get_session),
):
    from datetime import date as date_type

    wu = None
    if warranty_until.strip():
        try:
            wu = date_type.fromisoformat(warranty_until.strip())
        except ValueError:
            wu = None
    device = Device(
        name=name,
        vendor=vendor or None,
        product=product or None,
        device_model=device_model or None,
        room=room or None,
        serial=serial or None,
        notes=notes or None,
        status=DeviceStatus(status),
        warranty_until=wu,
        network_type=sorted(set(network_type)),
    )
    # All fields supplied via the web form are user-entered.
    from ..models import SOURCED_FIELDS, FieldSource

    for f in SOURCED_FIELDS:
        if getattr(device, f, None) is not None:
            setattr(device, f"{f}_source", FieldSource.user)
    session.add(device)
    audit_log(
        session,
        action="device.create",
        entity=f"device:{device.id}",
        reason="web.device_create",
    )
    session.commit()
    if qr_payload.strip():
        try:
            from ..services import apply_scan_payload

            apply_scan_payload(session, device, qr_payload.strip())
            audit_log(
                session,
                action="device.scan",
                entity=f"device:{device.id}",
                reason="web.device_create",
            )
            session.commit()
        except Exception:
            session.rollback()
    return RedirectResponse(_url(request, f"/devices/{device.id}"), status_code=303)


@router.get("/devices/{id}/scan", response_class=HTMLResponse)
def device_scan(id: str, request: Request, session: Session = Depends(get_session)):
    device = session.get(Device, id)
    if not device:
        return HTMLResponse("Device not found", status_code=404)
    return templates.TemplateResponse(
        request,
        "devices/scan.html",
        _ctx(request, device=device),
    )


@router.post("/devices/{id}/scan")
def device_scan_post(
    id: str,
    request: Request,
    session: Session = Depends(get_session),
    payload: str = Form(""),
    manual_code: str = Form(""),
    protocol: str = Form(""),
    discriminator: str = Form(""),
    passcode: str = Form(""),
    vid: str = Form(""),
    pid: str = Form(""),
    homekit_category: str = Form(""),
):
    from ..services import apply_scan_fields, apply_scan_manual_code, apply_scan_payload

    device = session.get(Device, id)
    if not device:
        return HTMLResponse("Device not found", status_code=404)

    error: str | None = None
    try:
        if payload.strip():
            apply_scan_payload(session, device, payload.strip())
        elif manual_code.strip():
            apply_scan_manual_code(session, device, manual_code.strip(), protocol=protocol or None)
        elif passcode.strip() and discriminator.strip():
            if protocol == "homekit":
                apply_scan_fields(
                    session,
                    device,
                    passcode=int(passcode),
                    discriminator=0,
                    protocol="homekit",
                    homekit_setup_id=discriminator.strip(),
                    homekit_category=int(homekit_category) if homekit_category.strip() else None,
                )
            else:
                apply_scan_fields(
                    session,
                    device,
                    passcode=int(passcode),
                    discriminator=int(discriminator),
                    vid=int(vid, 16) if vid.strip() else None,
                    pid=int(pid, 16) if pid.strip() else None,
                )
        else:
            error = "Provide a QR payload, manual code, or passcode + discriminator."
    except (ValueError, Exception) as exc:
        error = str(exc)

    if error:
        device = session.get(Device, id)
        return templates.TemplateResponse(
            request,
            "devices/scan.html",
            _ctx(request, device=device, error=error),
            status_code=400,
        )
    audit_log(session, action="device.scan", entity=f"device:{id}", reason="web.device_scan")
    session.commit()
    return RedirectResponse(_url(request, f"/devices/{id}"), status_code=303)


@router.get("/devices/{id}", response_class=HTMLResponse)
def device_detail(id: str, request: Request, session: Session = Depends(get_session)):
    device = session.get(Device, id)
    if not device:
        return HTMLResponse("Device not found", status_code=404)
    properties = session.exec(select(Property).where(Property.device_id == id)).all()
    attachments = session.exec(select(Attachment).where(Attachment.device_id == id)).all()
    qr_payload = next((c for c in properties if c.type == PropertyType.qr_payload), None)
    pin_cred = next((c for c in properties if c.type == PropertyType.setup_pin), None)
    disc_cred = next((c for c in properties if c.type == PropertyType.discriminator), None)
    manual_formatted = None
    manual_plain = None
    is_homekit = device.protocol.value == "homekit" if device.protocol else False
    if pin_cred and disc_cred:
        if is_homekit:
            from ..homekit import format_manual_code

            manual_formatted = format_manual_code(int(pin_cred.value))
            manual_plain = str(int(pin_cred.value)).zfill(8)
        else:
            from ..matter import compute_manual_code

            code = compute_manual_code(int(pin_cred.value), int(disc_cred.value))
            manual_formatted = f"{code[:4]}-{code[4:7]}-{code[7:]}"
            manual_plain = code
    mt_version = mt_flow_label = mt_disc_label = None
    hk_category = hk_setup_id = hk_paired = hk_supports_ip = hk_supports_ble = None
    if qr_payload:
        raw_payload = qr_payload.value
        if raw_payload.upper().startswith("X-HM://"):
            try:
                from ..homekit import category_name, decode_payload

                hk_sp = decode_payload(raw_payload)
                hk_category = category_name(hk_sp.category_id)
                hk_setup_id = hk_sp.setup_id
                hk_paired = hk_sp.paired
                hk_supports_ip = hk_sp.supports_ip
                hk_supports_ble = hk_sp.supports_ble
            except Exception:
                pass
        elif raw_payload.upper().startswith("MT:"):
            try:
                from ..matter import decode_setup_payload

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

    # Matter Server live state
    matter_membership = session.exec(
        select(DeviceFabricMembership).where(DeviceFabricMembership.device_id == id)
    ).first()
    matter_node = None
    if matter_membership:
        mc = getattr(request.app.state, "matter_client", None)
        if mc:
            try:
                matter_node = mc.get_node(matter_membership.node_id)
            except Exception:
                pass

    # OTBR Thread panel - inferred at render time from IPv6 prefix match
    thread_link = None
    if matter_membership and matter_node is not None:
        try:
            from sqlmodel import select as sql_select

            from ..integrations.otbr.client import correlate
            from ..models import ThreadNetwork

            oc = getattr(request.app.state, "otbr_client", None)
            networks = session.exec(sql_select(ThreadNetwork)).all()
            if networks and oc is not None:
                # Use cluster-derived IPs (GeneralDiagnostics) - the same
                # source used by the Networking panel and stored in the DB.
                # _node_obj.ip_addresses is often empty in practice.
                node_ips: list[str] = []
                try:
                    if matter_node.network_info is not None:
                        node_ips = list(matter_node.network_info.ipv6_addresses or [])
                    if not node_ips:
                        node_ips = list(matter_node.ip_addresses or [])
                except Exception:
                    pass
                if node_ips:
                    thread_link = correlate(
                        node_ips,
                        networks,
                        oc.get_diagnostics(),
                        oc.get_self_node(),
                    )
        except Exception:
            pass

    # mDNS live networking data (IP addresses, hostname, port)
    mdns_networking: dict | None = None
    mdns_link = session.exec(
        select(DeviceLink)
        .where(DeviceLink.device_id == id)  # type: ignore[attr-defined]
        .where(DeviceLink.integration == "mdns")  # type: ignore[attr-defined]
    ).first()
    if mdns_link:
        mc = getattr(request.app.state, "mdns_client", None)
        if mc:
            rec = mc.discovered_by_id(mdns_link.external_id)
            if rec and (
                rec.get("ipv4_addresses") or rec.get("ipv6_addresses") or rec.get("hostname")
            ):
                mdns_networking = rec

    # HA Core panel - status drives whether the link picker is offered
    ha_client_status = "disabled"
    hc = getattr(request.app.state, "ha_client", None)
    if hc is not None:
        ha_client_status = hc.status.value

    # HA link and derived record for the HA panel
    ha_link = session.exec(
        select(DeviceLink)
        .where(DeviceLink.device_id == id)  # type: ignore[attr-defined]
        .where(DeviceLink.integration == "ha_core")  # type: ignore[attr-defined]
    ).first()
    ha_record = session.get(HADeviceRecord, ha_link.external_id) if ha_link else None

    # ── per-device integration data for the Integrations tile ──
    import json as _json

    from ..integrations.data import read_all_for_device as _read_integration_data
    from ..integrations.ha.client import HACoreClient
    from ..integrations.matter_server.server_client import MatterServerClient
    from ..integrations.mdns.client import MdnsClient
    from ..integrations.otbr.client import OTBRClient

    # Map stored integration slugs → human display names, matching the
    # curated sub-section headers ("Matter Server", "Home Assistant", …), and
    # → their CSS icon class so each block header can show its icon.
    _integration_classes = (MatterServerClient, OTBRClient, HACoreClient, MdnsClient)
    _integration_names = {cls.slug: cls.long_name for cls in _integration_classes}
    integration_icons = {cls.slug: cls.icon for cls in _integration_classes}

    integration_data = _read_integration_data(session=session, device_id=id)
    # Pre-parse payloads + resolve display name so the template needs no filter.
    for _row in integration_data:
        try:
            _row._payload = _json.loads(_row.payload_json)  # type: ignore[attr-defined]
        except Exception:
            _row._payload = {}  # type: ignore[attr-defined]
        _row._display_name = _integration_names.get(  # type: ignore[attr-defined]
            _row.integration, _row.integration
        )
    # Keyed by slug so each curated Integrations sub-section can attach its own
    # data snapshot; slugs not claimed by a curated block fall through to
    # the generic per-integration blocks in the template.
    integration_data_by_slug = {_row.integration: _row for _row in integration_data}

    # ── real Fabric rows for this device (skip placeholder) ───────────
    _PLACEHOLDER = "0000000000000000"
    device_fabrics: list[Fabric] = []
    for _mem in session.exec(
        select(DeviceFabricMembership).where(DeviceFabricMembership.device_id == id)
    ).all():
        _fab = session.get(Fabric, _mem.fabric_id)
        if _fab and _fab.fabric_id != _PLACEHOLDER:
            device_fabrics.append(_fab)

    # ── compute applicable device actions per integration ───────────────
    applicable_device_actions: dict[str, list] = {}
    for _intg in getattr(request.app.state, "integrations", []):
        _acts = [a for a in _intg.device_actions() if a.applicable(device, session)]
        if _acts:
            applicable_device_actions[_intg.slug] = _acts

    # ── HAP pairing keys for HomeKit devices ────────────────────────────
    _HAP_TYPES = {
        PropertyType.hap_accessory_ltpk,
        PropertyType.hap_ios_pairing_id,
        PropertyType.hap_ios_device_ltsk,
        PropertyType.hap_ios_device_ltpk,
    }
    _HAP_PROP_ORDER = [
        (PropertyType.hap_accessory_ltpk.value, "Accessory LTPK"),
        (PropertyType.hap_ios_pairing_id.value, "iOS Pairing ID"),
        (PropertyType.hap_ios_device_ltsk.value, "iOS Device LTSK"),
        (PropertyType.hap_ios_device_ltpk.value, "iOS Device LTPK"),
    ]
    hap_props = {p.type.value: p for p in properties if p.type in _HAP_TYPES}

    return templates.TemplateResponse(
        request,
        "devices/detail.html",
        _ctx(
            request,
            device=device,
            properties=properties,
            attachments=attachments,
            qr_payload=qr_payload,
            manual_formatted=manual_formatted,
            manual_plain=manual_plain,
            is_homekit=is_homekit,
            mt_version=mt_version,
            mt_flow_label=mt_flow_label,
            mt_disc_label=mt_disc_label,
            hk_category=hk_category,
            hk_setup_id=hk_setup_id,
            hk_paired=hk_paired,
            hk_supports_ip=hk_supports_ip,
            hk_supports_ble=hk_supports_ble,
            prop_types=list(PropertyType),
            hap_props=hap_props,
            hap_prop_types=_HAP_PROP_ORDER,
            matter_membership=matter_membership,
            matter_node=matter_node,
            matter_network=matter_node.network_info if matter_node else None,
            mdns_networking=mdns_networking,
            thread_link=thread_link,
            ha_client_status=ha_client_status,
            ha_link=ha_link,
            ha_record=ha_record,
            integration_data=integration_data,
            integration_data_by_slug=integration_data_by_slug,
            integration_icons=integration_icons,
            applicable_device_actions=applicable_device_actions,
            device_fabrics=device_fabrics,
            FieldSource=FieldSource,
        ),
    )


@router.get("/devices/{id}/edit", response_class=HTMLResponse)
def device_edit(id: str, request: Request, session: Session = Depends(get_session)):
    device = session.get(Device, id)
    if not device:
        return HTMLResponse("Device not found", status_code=404)
    return templates.TemplateResponse(
        request,
        "devices/form.html",
        _ctx(request, device=device, statuses=list(DeviceStatus)),
    )


@router.post("/devices/{id}")
def device_update(
    id: str,
    request: Request,
    name: str = Form(...),
    vendor: str = Form(""),
    product: str = Form(""),
    device_model: str = Form(""),
    room: str = Form(""),
    serial: str = Form(""),
    notes: str = Form(""),
    status: str = Form("active"),
    warranty_until: str = Form(""),
    network_type: list[str] = Form(default=[]),
    session: Session = Depends(get_session),
):
    device = session.get(Device, id)
    if not device:
        return HTMLResponse("Device not found", status_code=404)
    from datetime import date as date_type

    from ..models import FieldSource
    from ..services import set_field

    wu = None
    if warranty_until.strip():
        try:
            wu = date_type.fromisoformat(warranty_until.strip())
        except ValueError:
            wu = None
    # Only fields the user actually changed are stamped as user-entered -
    # otherwise the integration provenance (matter/ha/otbr/...) is preserved.
    submitted = {
        "name": name,
        "vendor": vendor or None,
        "product": product or None,
        "device_model": device_model or None,
        "room": room or None,
        "serial": serial or None,
        "notes": notes or None,
        "status": DeviceStatus(status),
        "warranty_until": wu,
        "network_type": sorted(set(network_type)),
    }
    for field, value in submitted.items():
        if getattr(device, field) != value:
            set_field(device, field, value, FieldSource.user)
    device.updated_at = datetime.now(UTC)
    session.add(device)
    audit_log(
        session,
        action="device.update",
        entity=f"device:{id}",
        reason="web.device_update",
    )
    session.commit()
    return RedirectResponse(_url(request, f"/devices/{id}"), status_code=303)


@router.delete("/devices/{id}", response_class=HTMLResponse)
def device_delete(id: str, request: Request, session: Session = Depends(get_session)):
    device = session.get(Device, id)
    if device:
        audit_log(
            session,
            action="device.delete",
            entity=f"device:{id}",
            reason="web.device_delete",
        )
        session.delete(device)
        session.commit()
    resp = HTMLResponse("")
    resp.headers["HX-Redirect"] = _url(request, "/devices")
    return resp


# ── Device merge ─────────────────────────────────────────────────────────────


@router.get("/devices/{id}/merge", response_class=HTMLResponse)
def device_merge_get(
    id: str,
    request: Request,
    session: Session = Depends(get_session),
    target_id: str | None = None,
):
    from ..services import build_merge_preview

    source = session.get(Device, id)
    if not source:
        return HTMLResponse("Device not found", status_code=404)
    # Only same-protocol devices are mergeable - commissioning protocol must match.
    others = session.exec(
        select(Device).where(Device.id != id).where(Device.protocol == source.protocol)
    ).all()
    target = session.get(Device, target_id) if target_id else None
    preview = build_merge_preview(source, target) if target else None
    return templates.TemplateResponse(
        request,
        "devices/merge.html",
        _ctx(request, source=source, others=others, target=target, preview=preview),
    )


@router.post("/devices/{id}/merge", response_class=HTMLResponse)
def device_merge_post(
    id: str,
    request: Request,
    target_id: str = Form(...),
    session: Session = Depends(get_session),
):
    from ..services import ProtocolMismatchError, build_merge_preview, merge_devices

    source = session.get(Device, id)
    if not source:
        return HTMLResponse("Device not found", status_code=404)
    target = session.get(Device, target_id)
    if not target:
        return HTMLResponse("Target device not found", status_code=404)
    try:
        merge_devices(session, source_id=id, target_id=target_id)
    except ProtocolMismatchError as exc:
        session.rollback()
        others = session.exec(
            select(Device).where(Device.id != id).where(Device.protocol == source.protocol)
        ).all()
        return templates.TemplateResponse(
            request,
            "devices/merge.html",
            _ctx(
                request,
                source=source,
                others=others,
                target=target,
                preview=build_merge_preview(source, target),
                error=str(exc),
            ),
            status_code=409,
        )
    session.commit()
    return RedirectResponse(_url(request, f"/devices/{target_id}"), status_code=303)


# ── Attachments ──────────────────────────────────────────────────────────────


@router.delete("/devices/{device_id}/attachments/{att_id}", response_class=HTMLResponse)
def attachment_delete(
    device_id: str,
    att_id: str,
    request: Request,
    session: Session = Depends(get_session),
):
    att = session.get(Attachment, att_id)
    if att:
        audit_log(
            session,
            action="attachment.delete",
            entity=f"attachment:{att_id}",
            reason="web.attachment_delete",
        )
        session.delete(att)
        session.commit()
    attachments = session.exec(select(Attachment).where(Attachment.device_id == device_id)).all()
    return templates.TemplateResponse(
        request,
        "attachments/_grid.html",
        _ctx(request, attachments=attachments),
    )


# ── Properties ───────────────────────────────────────────────────────────────


@router.post("/devices/{device_id}/properties", response_class=HTMLResponse)
def property_add(
    device_id: str,
    request: Request,
    cred_type: str = Form(...),
    value: str = Form(...),
    label: str = Form(""),
    session: Session = Depends(get_session),
):
    device = session.get(Device, device_id)
    if not device:
        return HTMLResponse("Device not found", status_code=404)
    prop = Property(
        device_id=device_id,
        type=PropertyType(cred_type),
        value=value,
        label=label or None,
        source=FieldSource.user,
    )
    session.add(prop)
    audit_log(
        session,
        action="property.create",
        entity=f"property:{prop.id}",
        reason="web.property_add",
    )
    session.commit()
    session.refresh(prop)
    properties = session.exec(select(Property).where(Property.device_id == device_id)).all()
    return templates.TemplateResponse(
        request,
        "properties/_list.html",
        _ctx(request, properties=properties, device_id=device_id),
    )


@router.get("/devices/{device_id}/properties/{id}/row", response_class=HTMLResponse)
def property_row(
    device_id: str, id: str, request: Request, session: Session = Depends(get_session)
):
    prop = session.get(Property, id)
    if not prop or prop.device_id != device_id:
        return HTMLResponse("", status_code=404)
    return templates.TemplateResponse(
        request,
        "properties/_row.html",
        _ctx(request, c=prop),
    )


@router.get("/devices/{device_id}/properties/{id}/edit", response_class=HTMLResponse)
def property_edit_row(
    device_id: str, id: str, request: Request, session: Session = Depends(get_session)
):
    prop = session.get(Property, id)
    if not prop or prop.device_id != device_id:
        return HTMLResponse("", status_code=404)
    return templates.TemplateResponse(
        request,
        "properties/_row_edit.html",
        _ctx(
            request,
            c=prop,
            prop_types=list(PropertyType),
        ),
    )


@router.post("/devices/{device_id}/properties/{id}", response_class=HTMLResponse)
def property_update_row(
    device_id: str,
    id: str,
    request: Request,
    cred_type: str = Form(...),
    value: str = Form(...),
    label: str = Form(""),
    session: Session = Depends(get_session),
):
    prop = session.get(Property, id)
    if not prop or prop.device_id != device_id:
        return HTMLResponse("", status_code=404)
    prop.type = PropertyType(cred_type)
    prop.value = value
    prop.label = label or None
    prop.source = FieldSource.user
    session.add(prop)
    audit_log(
        session,
        action="property.update",
        entity=f"property:{id}",
        reason="web.property_update",
    )
    session.commit()
    session.refresh(prop)
    return templates.TemplateResponse(
        request,
        "properties/_row.html",
        _ctx(request, c=prop),
    )


@router.delete("/devices/{device_id}/properties/{id}", response_class=HTMLResponse)
def property_delete(
    device_id: str, id: str, request: Request, session: Session = Depends(get_session)
):
    prop = session.get(Property, id)
    if prop and prop.device_id == device_id:
        audit_log(
            session,
            action="property.delete",
            entity=f"property:{id}",
            reason="web.property_delete",
        )
        session.delete(prop)
        session.commit()
        properties = session.exec(select(Property).where(Property.device_id == device_id)).all()
        return templates.TemplateResponse(
            request,
            "properties/_list.html",
            _ctx(request, properties=properties, device_id=device_id),
        )
    return HTMLResponse("")


# ── Settings ──────────────────────────────────────────────────────────────────


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, session: Session = Depends(get_session)):
    try:
        db_size_mb = round(app_settings.db_path.stat().st_size / 1024**2, 1)
    except Exception:
        db_size_mb = None
    imported = request.query_params.get("imported")
    return templates.TemplateResponse(
        request,
        "settings/index.html",
        _ctx(
            request,
            app_version=app_settings.version,
            data_dir=str(app_settings.data_dir),
            db_size_mb=db_size_mb,
            log_level=app_settings.log_level,
            imported=imported,
        ),
    )


@router.post("/settings/import/preview")
async def settings_import_preview(
    request: Request,
    file: UploadFile = File(...),
    policy: str = Form("skip"),
    session: Session = Depends(get_session),
):
    import json

    from ..importer import plan_import
    from ..models import Device, Fabric, ThreadNetwork

    try:
        content = await file.read()
        payload = json.loads(content)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    plan = plan_import(session, payload, policy=policy)
    if plan.errors:
        return JSONResponse({"error": plan.errors[0]}, status_code=422)
    existing_devices = len(session.exec(select(Device)).all())
    existing_fabrics = len(session.exec(select(Fabric)).all())
    existing_networks = len(session.exec(select(ThreadNetwork)).all())
    return JSONResponse(
        {
            "existing_devices": existing_devices,
            "existing_fabrics": existing_fabrics,
            "existing_networks": existing_networks,
            "creates": len(plan.creates),
            "updates": len(plan.updates),
            "skips": len(plan.skips),
        }
    )


@router.post("/settings/import", response_class=HTMLResponse)
async def settings_import(
    request: Request,
    file: UploadFile = File(...),
    policy: str = Form("skip"),
    session: Session = Depends(get_session),
):
    import json

    from ..importer import apply_import

    try:
        content = await file.read()
        payload = json.loads(content)
    except Exception as exc:
        return templates.TemplateResponse(
            request,
            "settings/index.html",
            _ctx(request, import_error=f"Could not parse file: {exc}"),
            status_code=400,
        )
    plan = apply_import(session, payload, policy=policy)
    if plan.errors:
        return templates.TemplateResponse(
            request,
            "settings/index.html",
            _ctx(request, import_errors=plan.errors),
            status_code=422,
        )
    n = len(plan.creates) + len(plan.updates)
    action = "import.full" if policy == "replace" else "import.skip"
    reason = "ui.replace" if policy == "replace" else "ui.skip"
    audit_log(session, action=action, entity=f"import:{n}", reason=reason)
    session.commit()
    if plan.warnings:
        return templates.TemplateResponse(
            request,
            "settings/index.html",
            _ctx(request, imported=str(n), import_warnings=plan.warnings),
        )
    return RedirectResponse(_url(request, f"/settings?imported={n}"), status_code=303)


# ── Integrations settings ─────────────────────────────────────────────────────


@router.get("/settings/integrations", response_class=HTMLResponse)
def settings_integrations(request: Request, session: Session = Depends(get_session)):
    import os

    from ..integrations.ha.client import HACoreClient
    from ..integrations.matter_server.server_client import MatterServerClient
    from ..integrations.mdns.client import MdnsClient
    from ..integrations.otbr.client import OTBRClient
    from ..models import ThreadNetwork

    mc = getattr(request.app.state, "matter_client", None)
    ms_url = mc._url if mc else os.getenv("PYTHON_MATTER_SERVER", "")
    ms_status = "unconfigured" if mc is None else mc.status.value
    ms_error = None if mc is None else mc.error_message
    ms_last_sync = mc._last_sync if mc else None
    ms_last_synced_at = mc._last_synced_at if mc else None
    ms_capabilities = (
        {
            "can_create_devices": mc.can_create_devices,
            "can_update_devices": mc.can_update_devices,
            "can_update_status": mc.can_update_status,
            "can_act_externally": mc.can_act_externally,
            "supported_protocols": sorted(p.value for p in mc.supported_protocols),  # type: ignore[union-attr]
        }
        if mc
        else None
    )

    oc = getattr(request.app.state, "otbr_client", None)
    otbr_url = oc._base_url if oc else os.getenv("OTBR_URL", "")
    otbr_status = "unconfigured" if oc is None else oc.status.value
    otbr_error = None if oc is None else oc.error_message
    otbr_last_sync = oc._last_sync if oc else None
    otbr_last_synced_at = oc._last_synced_at if oc else None
    otbr_capabilities = (
        {
            "can_create_devices": oc.can_create_devices,
            "can_update_devices": oc.can_update_devices,
            "can_update_status": oc.can_update_status,
            "can_act_externally": oc.can_act_externally,
            "supported_protocols": sorted(p.value for p in oc.supported_protocols),  # type: ignore[union-attr]
        }
        if oc
        else None
    )
    # Thread networks this OTBR has polled - drives the dataset + decoded table.
    otbr_networks = (
        session.exec(select(ThreadNetwork).where(ThreadNetwork.border_router_url == otbr_url)).all()
        if oc
        else []
    )

    hc = getattr(request.app.state, "ha_client", None)
    ha_status = "unconfigured" if hc is None else hc.status.value
    ha_error = None if hc is None else hc.error_message
    ha_last_sync = hc._last_sync if hc else None
    ha_last_synced_at = hc._last_synced_at if hc else None
    ha_capabilities = (
        {
            "can_create_devices": hc.can_create_devices,
            "can_update_devices": hc.can_update_devices,
            "can_update_status": hc.can_update_status,
            "can_act_externally": hc.can_act_externally,
            "supported_protocols": sorted(p.value for p in hc.supported_protocols),  # type: ignore[union-attr]
        }
        if hc
        else None
    )

    nc = getattr(request.app.state, "mdns_client", None)
    mdns_status = "unconfigured" if nc is None else nc.status.value
    mdns_error = None if nc is None else nc.error_message
    mdns_last_sync = nc._last_sync if nc else None
    mdns_last_synced_at = nc._last_synced_at if nc else None
    mdns_discovered_count = len(nc.discovered()) if nc else 0
    mdns_capabilities = (
        {
            "can_create_devices": nc.can_create_devices,
            "can_update_devices": nc.can_update_devices,
            "can_update_status": nc.can_update_status,
            "can_act_externally": nc.can_act_externally,
            "supported_protocols": sorted(p.value for p in nc.supported_protocols),  # type: ignore[union-attr]
        }
        if nc
        else None
    )

    supervisor_token = os.environ.get("SUPERVISOR_TOKEN", "")
    using_supervisor = bool(supervisor_token)
    if hc:
        ha_url = hc._url
        token_raw = "" if using_supervisor else hc._token
    elif using_supervisor:
        ha_url = "http://supervisor/core"
        token_raw = ""
    else:
        ha_url = os.environ.get("HA_CORE_URL", "")
        token_raw = os.environ.get("HA_CORE_TOKEN", "")

    return templates.TemplateResponse(
        request,
        "settings/integrations.html",
        _ctx(
            request,
            ms_long_name=MatterServerClient.long_name,
            ms_short_name=MatterServerClient.short_name,
            ms_url=ms_url,
            ms_status=ms_status,
            ms_error=ms_error,
            ms_last_sync=ms_last_sync,
            ms_last_synced_at=ms_last_synced_at,
            ms_capabilities=ms_capabilities,
            otbr_long_name=OTBRClient.long_name,
            otbr_short_name=OTBRClient.short_name,
            otbr_url=otbr_url,
            otbr_status=otbr_status,
            otbr_error=otbr_error,
            otbr_last_sync=otbr_last_sync,
            otbr_last_synced_at=otbr_last_synced_at,
            otbr_capabilities=otbr_capabilities,
            otbr_networks=otbr_networks,
            ha_long_name=HACoreClient.long_name,
            ha_short_name=HACoreClient.short_name,
            ha_url=ha_url,
            ha_status=ha_status,
            ha_error=ha_error,
            ha_last_sync=ha_last_sync,
            ha_last_synced_at=ha_last_synced_at,
            ha_capabilities=ha_capabilities,
            ha_token_hint=("…" + token_raw[-4:])
            if len(token_raw) >= 4
            else ("…" if token_raw else ""),
            ha_using_supervisor=using_supervisor,
            mdns_long_name=MdnsClient.long_name,
            mdns_short_name=MdnsClient.short_name,
            mdns_status=mdns_status,
            mdns_error=mdns_error,
            mdns_last_sync=mdns_last_sync,
            mdns_last_synced_at=mdns_last_synced_at,
            mdns_discovered_count=mdns_discovered_count,
            mdns_capabilities=mdns_capabilities,
        ),
    )
