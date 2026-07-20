"""
/api/integrations - Matter Server and OTBR integration config, status, and import/poll endpoints.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlmodel import Session, select

from ..audit import log as audit_log
from ..database import get_session

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/integrations", tags=["integrations"])


# ── Helpers ───────────────────────────────────────────────────────────────────


def _matter_client(request: Request):
    return getattr(request.app.state, "matter_client", None)


def _otbr_client(request: Request):
    return getattr(request.app.state, "otbr_client", None)


def _ha_client(request: Request):
    return getattr(request.app.state, "ha_client", None)


def _mdns_client(request: Request):
    return getattr(request.app.state, "mdns_client", None)


# ── Status endpoint ───────────────────────────────────────────────────────────


@router.get("")
def integrations_status(request: Request):
    import os

    mc = _matter_client(request)
    ms_url = mc._url if mc else os.getenv("PYTHON_MATTER_SERVER", "")
    ms_status = "unconfigured" if mc is None else mc.status.value
    ms_error = None if mc is None else mc.error_message

    oc = _otbr_client(request)
    otbr_url = oc._base_url if oc else os.getenv("OTBR_URL", "")
    otbr_status = "unconfigured" if oc is None else oc.status.value
    otbr_error = None if oc is None else oc.error_message

    hc = _ha_client(request)
    using_supervisor = bool(os.getenv("SUPERVISOR_TOKEN", ""))
    if hc:
        ha_url = hc._url
        token_raw = "" if using_supervisor else hc._token
    else:
        ha_url = "http://supervisor/core" if using_supervisor else os.getenv("HA_CORE_URL", "")
        token_raw = "" if using_supervisor else os.getenv("HA_CORE_TOKEN", "")
    ha_status = "unconfigured" if hc is None else hc.status.value
    ha_error = None if hc is None else hc.error_message

    return {
        "matter_server": {
            "url": ms_url,
            "status": ms_status,
            "error": ms_error,
        },
        "otbr": {
            "url": otbr_url,
            "status": otbr_status,
            "error": otbr_error,
        },
        "ha_core": {
            "url": ha_url,
            "token_hint": ("…" + token_raw[-4:])
            if len(token_raw) >= 4
            else ("…" if token_raw else ""),
            "using_supervisor_token": using_supervisor,
            "status": ha_status,
            "error": ha_error,
        },
    }


@router.post("/matter-server/import/apply")
async def matter_server_import_apply(request: Request):
    """Apply import: creates/updates Device + Fabric + DeviceFabricMembership rows.

    Delegates to ``MatterServerClient.sync_now()`` which projects the live WS
    node cache directly (no extra round-trip to python-matter-server).
    """
    client = _matter_client(request)
    if client is None:
        raise HTTPException(
            status_code=503, detail="Matter Server integration not configured or not connected"
        )

    from ..integrations.matter_server.server_client import ClientStatus

    if client.status != ClientStatus.connected:
        raise HTTPException(
            status_code=503,
            detail=f"Matter Server not connected (status: {client.status.value})",
        )

    result = await client.sync_now()
    return {
        "summary": {
            "create": result.created,
            "update": result.updated,
            "unchanged": result.skipped,
        }
    }


# ── OTBR: poll apply ──────────────────────────────────────────────────────────


@router.post("/otbr/poll/apply")
async def otbr_poll_apply(request: Request):
    """Trigger an immediate poll and upsert the ThreadNetwork row."""
    client = _otbr_client(request)
    if client is None:
        raise HTTPException(status_code=503, detail="OTBR integration not configured")

    try:
        snapshot = await client.poll_once(reason="ui.manual_sync")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"OTBR poll failed: {exc}") from exc

    return {
        "ok": True,
        "network_name": snapshot.dataset.get("networkName", ""),
        "ext_pan_id": snapshot.dataset.get("extPanId", ""),
    }


# ── HA Core: config ────────────────────────────────────────────────────────────


@router.get("/ha-core/config")
def ha_core_config_get(request: Request):
    import os

    supervisor_token = os.environ.get("SUPERVISOR_TOKEN", "")
    using_supervisor = bool(supervisor_token)
    hc = _ha_client(request)
    if hc:
        url = hc._url
        token_raw = "" if using_supervisor else hc._token
    elif using_supervisor:
        url = "http://supervisor/core"
        token_raw = ""
    else:
        url = os.environ.get("HA_CORE_URL", "")
        token_raw = os.environ.get("HA_CORE_TOKEN", "")
    return {
        "url": url,
        "token_hint": ("…" + token_raw[-4:]) if len(token_raw) >= 4 else ("…" if token_raw else ""),
        "using_supervisor_token": using_supervisor,
        "status": "unconfigured" if hc is None else hc.status.value,
        "error": None if hc is None else hc.error_message,
    }


# ── HA Core: sync ──────────────────────────────────────────────────────────────


@router.post("/ha-core/sync")
async def ha_core_sync(request: Request):
    """Trigger an immediate HA Core poll (manual sync)."""
    hc = _ha_client(request)
    if hc is None:
        raise HTTPException(status_code=503, detail="HA Core integration not configured")

    from ..integrations.ha.client import ClientStatus

    if hc.status not in (ClientStatus.connected, ClientStatus.connecting):
        raise HTTPException(
            status_code=503,
            detail=f"HA Core not reachable (status: {hc.status.value})",
        )

    try:
        result = await hc.poll_once(reason="ui.manual_sync")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"HA Core sync failed: {exc}") from exc

    return result


@router.post("/mdns/sync")
async def mdns_sync(request: Request):
    """Project the live mDNS discovery cache into the registry (manual sync)."""
    nc = _mdns_client(request)
    if nc is None:
        raise HTTPException(status_code=503, detail="mDNS discovery not enabled")
    try:
        result = await nc.sync_now()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"mDNS sync failed: {exc}") from exc
    return {"created": result.created, "updated": result.updated, "skipped": result.skipped}


@router.get("/mdns/devices")
async def mdns_devices(request: Request):
    """Return the currently-discovered HAP accessories (for the UI)."""
    nc = _mdns_client(request)
    if nc is None:
        raise HTTPException(status_code=503, detail="mDNS discovery not enabled")
    return nc.discovered()


@router.get("/mdns/ltpdu-devices")
async def mdns_ltpdu_devices(request: Request):
    """Return the currently-discovered LTPDU accessories (for diagnostics)."""
    nc = _mdns_client(request)
    if nc is None:
        raise HTTPException(status_code=503, detail="mDNS discovery not enabled")
    return nc.ltpdu_discovered()


@router.get("/mdns/matter-devices")
async def mdns_matter_devices(request: Request):
    """Return the currently-discovered Matter operational nodes (for diagnostics)."""
    nc = _mdns_client(request)
    if nc is None:
        raise HTTPException(status_code=503, detail="mDNS discovery not enabled")
    return nc.matter_discovered()


# ── HA Core: device list (link picker) ────────────────────────────────────────


@router.get("/ha-core/devices")
async def ha_core_devices(request: Request, session: Session = Depends(get_session)):
    """Return the HA device registry list for the link picker.

    Already-linked HA devices (those whose ID appears on any Device row) are
    excluded to enforce the 1-MR-Device : 1-HA-device invariant.
    """
    hc = _ha_client(request)
    if hc is None:
        raise HTTPException(status_code=503, detail="HA Core integration not configured")

    try:
        devices = await hc.get_ha_devices()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"HA Core fetch failed: {exc}") from exc

    # Exclude HA devices already linked to any MR Device row.
    # Joined against Device so a DeviceLink orphaned by a device delete (no
    # cascade at the DB level) doesn't silently blackhole an HA device from
    # every future picker.
    from ..models import Device, DeviceLink

    linked_ids: set[str] = set(
        session.exec(
            select(DeviceLink.external_id)
            .join(Device, DeviceLink.device_id == Device.id)  # type: ignore[arg-type]
            .where(DeviceLink.integration == "ha_core")  # type: ignore[arg-type]
        ).all()
    )

    # Return only fields useful for the picker
    return [
        {
            "id": d.get("id"),
            "name": d.get("name"),
            "manufacturer": d.get("manufacturer"),
            "model": d.get("model"),
            "area_name": d.get("area_name"),
            "fabric_id": d.get("fabric_id"),
            "node_id": d.get("node_id"),
            "serial": d.get("serial"),
            "matter_unique_id": d.get("matter_unique_id"),
            "protocol": d.get("protocol"),
        }
        for d in devices
        if d.get("id") not in linked_ids
    ]


# ── B.13: generic device-action dispatch ──────────────────────────────────────


@router.post("/{slug}/devices/{device_id}/actions/{action_key}")
async def run_device_action(
    slug: str,
    device_id: str,
    action_key: str,
    request: Request,
    session: Session = Depends(get_session),
) -> Response:
    """Generic per-device action dispatcher (B.13).

    Resolves the integration by slug, finds the declared DeviceAction by key,
    checks ``applicable()``, runs ``run()``, and audit-logs the invocation.
    Returns 200 + JSON ``{"message": "..."}`` on success so callers can show
    the result message in a notification.

    404 when the integration or action is not found.
    422 when the action is not applicable to this device.
    503 when the integration is not enabled.
    502 on action execution failure.
    """
    from fastapi.responses import JSONResponse

    integrations = getattr(request.app.state, "integrations", [])
    integration = next((i for i in integrations if i.slug == slug), None)
    if integration is None:
        raise HTTPException(
            status_code=404, detail=f"Integration '{slug}' not found or not enabled"
        )

    from ..models import Device

    device = session.get(Device, device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found")

    actions = integration.device_actions()
    action = next((a for a in actions if a.key == action_key), None)
    if action is None:
        raise HTTPException(
            status_code=404, detail=f"Action '{action_key}' not declared by '{slug}'"
        )

    if not action.applicable(device, session):
        raise HTTPException(
            status_code=422, detail=f"Action '{action_key}' is not applicable to this device"
        )

    try:
        result = await action.run(device, session)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    audit_log(
        session,
        action=f"device.action.{slug}.{action_key}",
        entity=f"device:{device_id}",
        reason="api.device_action",
    )
    session.commit()
    return JSONResponse({"message": result.message or action.label})
