"""
Home Assistant Core REST client.

Auth header: Authorization: Bearer <token>.
Endpoints consumed:
  GET  /api/config               - sanity probe / version
  GET  /api/states               - all entity states (cached 30 s for live-state)
  POST /api/template             - device registry lookup via Jinja template

Background poll every 10 min. Backoff on failure: [1, 2, 5, 15, 60] s.
Status: disabled | connecting | connected | error
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from enum import StrEnum

import httpx

from ...models import DeviceProtocol
from ..base import SyncResult
from ..polled import PermanentError, PolledIntegration

logger = logging.getLogger(__name__)

_POLL_INTERVAL = 600  # kept as module constant for the docstring; base owns the loop value

# Single unified template fetches all devices with their full metadata.
# Protocol is derived Python-side from identifiers.
_REGISTRY_TEMPLATE = (
    "{%- set seen = namespace(ids=[]) -%}"
    "{%- set out = namespace(items=[]) -%}"
    "{%- for s in states -%}"
    "{%- set did = device_id(s.entity_id) -%}"
    "{%- if did and did not in seen.ids -%}"
    "{%- set seen.ids = seen.ids + [did] -%}"
    "{%- set out.items = out.items + [{"
    "'id': did,"
    "'name': (device_attr(did,'name_by_user') or device_attr(did,'name') or ''),"
    "'manufacturer': (device_attr(did,'manufacturer') or ''),"
    "'model': (device_attr(did,'model') or ''),"
    "'area_name': (area_name(did) or ''),"
    "'area_id': (device_attr(did,'area_id') or ''),"
    "'identifiers': (device_attr(did,'identifiers') or []) | list,"
    "'connections': (device_attr(did,'connections') or []) | list,"
    "'sw_version': (device_attr(did,'sw_version') or ''),"
    "'hw_version': (device_attr(did,'hw_version') or '')"
    "}] -%}"
    "{%- endif -%}"
    "{%- endfor -%}"
    "{{ out.items | to_json }}"
)


def _determine_protocol(identifiers: list) -> str | None:
    """Return 'matter' or 'homekit' based on identifier domain tuples."""
    for ident in identifiers or []:
        if not isinstance(ident, (list, tuple)) or len(ident) < 2:
            continue
        domain = str(ident[0])
        if domain == "matter":
            return "matter"
        if domain == "homekit_controller:accessory-id":
            return "homekit"
    return None


def _parse_matter_identifiers(identifiers: list) -> dict:
    """Parse HA Matter device identifiers into structured fields.

    Recognises two HA Matter identifier formats:
      - ``serial_<SERIAL>``                            → serial
      - ``deviceid_<FABRIC_HEX>-<NODE_HEX16>-...``    → fabric_id + node_id

    Returns dict with keys:
      ``matter_uid_set``   - frozenset of all second elements of ('matter', *) tuples;
                             used by ``auto_correlate`` Key 0.
      ``matter_unique_id`` - first matter identifier value (kept for back-fill).
      ``fabric_id``        - lowercase hex str or None.
      ``node_id``          - int or None.
      ``serial``           - str or None (parsed from ``serial_<S>`` format).
    """
    fabric_id = None
    node_id = None
    serial = None
    matter_unique_id = None
    matter_uid_set: set[str] = set()
    for ident in identifiers or []:
        if not isinstance(ident, (list, tuple)) or len(ident) < 2:
            continue
        domain, value = str(ident[0]), str(ident[1])
        if domain != "matter":
            continue
        matter_uid_set.add(value)
        if matter_unique_id is None:
            matter_unique_id = value  # take the first matter identifier as the canonical UID
        if value.startswith("serial_"):
            serial = value[7:]
        elif value.startswith("deviceid_"):
            # format: deviceid_<FABRIC_HEX>-<NODE_HEX_16>-MatterNodeDevice
            parts = value[9:].split("-")
            if len(parts) >= 2:
                fabric_id = parts[0].lower()
                try:
                    node_id = int(parts[1], 16)
                except ValueError:
                    pass
    return {
        "matter_uid_set": frozenset(matter_uid_set),
        "matter_unique_id": matter_unique_id,
        "fabric_id": fabric_id,
        "node_id": node_id,
        "serial": serial,
    }


def _homekit_accessory_id(identifiers: list) -> str | None:
    """Return the HAP accessory id from a HomeKit device's HA identifiers.

    HA stores it as ``('homekit_controller:accessory-id', '<id>:aid:<n>')``;
    the prefix before ``:aid:`` is the HAP Device ID (mDNS ``id``), the shared
    dedupe key with mDNS discovery (D.3). Returned uppercase.
    """
    for ident in identifiers or []:
        if not isinstance(ident, (list, tuple)) or len(ident) < 2:
            continue
        if str(ident[0]) == "homekit_controller:accessory-id":
            value = str(ident[1])
            return value.rsplit(":aid:", 1)[0].upper()
    return None


def _parse_homekit_data(d: dict) -> dict:
    """Extract HomeKit-specific metadata from an HA device-registry dict.

    Returns dict with keys:
      ``serial``            - serial_number from device_attr or None
      ``sw_version``        - sw_version from device_attr or None
      ``hw_version``        - hw_version from device_attr or None
      ``mac_address``       - MAC from connections BLE tuple or None
      ``network_type``      - list of transport strings (e.g. ["bluetooth"])
    """
    result: dict = {
        "serial": None,
        "sw_version": None,
        "hw_version": None,
        "mac_address": None,
        "network_type": [],
    }  # type: ignore[var-annotated]
    result["sw_version"] = d.get("sw_version") or None
    result["hw_version"] = d.get("hw_version") or None

    identifiers = d.get("identifiers") or []
    for ident in identifiers:
        if not isinstance(ident, (list, tuple)) or len(ident) < 2:
            continue
        domain, value = str(ident[0]), str(ident[1])
        if domain == "serial_number":
            result["serial"] = value

    connections = d.get("connections") or []
    for conn in connections:
        if not isinstance(conn, (list, tuple)) or len(conn) < 2:
            continue
        transport = str(conn[0]).lower()
        mac = str(conn[1])
        if transport == "bluetooth" or transport == "ble":
            if result["mac_address"] is None:
                result["mac_address"] = mac
            if "bluetooth" not in result["network_type"]:
                result["network_type"].append("bluetooth")

    return result


class ClientStatus(StrEnum):
    disabled = "disabled"
    connecting = "connecting"
    connected = "connected"
    error = "error"


class HACoreClient(PolledIntegration):
    """Long-lived async HTTP client for Home Assistant Core REST API."""

    slug = "ha_core"
    short_name = "hass"
    long_name = "Home Assistant"
    icon = "icon-home-assistant"
    can_create_devices = True
    can_update_devices = True
    can_update_status = False
    can_act_externally = False
    supported_protocols = frozenset({DeviceProtocol.matter, DeviceProtocol.homekit})

    def __init__(self, url: str, token: str) -> None:
        super().__init__()
        self._url = url.rstrip("/")
        self._token = token
        self._status = ClientStatus.connecting
        # HA device registry cache (refreshed during poll_once)
        self._ha_devices: list[dict] = []
        self._ha_devices_fetched: datetime | None = None
        # Entity states cache (used for synchronous get_device_state_from_cache)
        self._states_cache: list[dict] = []
        self._states_fetched: datetime | None = None

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    def _on_stopped(self) -> None:
        self._status = ClientStatus.disabled

    async def poll_once(self, *, reason: str = "ha_core.background_poll") -> dict:
        """Fetch HA device registry, correlate/refresh MR Device ha_* columns.

        Returns {created: N, updated: N, skipped: N}
        """
        await self.ingest()
        return self._sync_devices(self._ha_devices, reason=reason)

    # ── Integration interface ─────────────────────────────────────────────────

    async def ingest(self) -> None:
        """Phase 1: fetch HA device registry and persist to HADeviceRecord table."""
        async with httpx.AsyncClient(timeout=15, headers=self._headers()) as http:
            cfg = await http.get(f"{self._url}/api/config")
            cfg.raise_for_status()

            tmpl_resp = await http.post(
                f"{self._url}/api/template",
                json={"template": _REGISTRY_TEMPLATE},
            )
            tmpl_resp.raise_for_status()
            try:
                raw_devices: list[dict] = json.loads(tmpl_resp.text)
            except Exception as exc:
                logger.warning("HA template parse failed: %s | raw: %.300s", exc, tmpl_resp.text)
                raw_devices = []

            states_resp = await http.get(f"{self._url}/api/states")
            states_resp.raise_for_status()
            self._states_cache = states_resp.json()
            self._states_fetched = datetime.now(UTC)

        # Determine protocol and enrich each device
        for d in raw_devices:
            identifiers = d.get("identifiers") or []
            protocol = _determine_protocol(identifiers)
            d["protocol"] = protocol
            if protocol == "matter":
                d.update(_parse_matter_identifiers(identifiers))
            elif protocol == "homekit":
                d.update(_parse_homekit_data(d))
            d["connections"] = d.get("connections") or []
            d["sw_version"] = d.get("sw_version") or None
            d["hw_version"] = d.get("hw_version") or None

        # Filter to only Matter and HomeKit devices
        ha_devices = [d for d in raw_devices if d.get("protocol") in ("matter", "homekit")]

        now = datetime.now(UTC)

        from sqlmodel import Session
        from sqlmodel import delete as _delete

        from ...database import engine
        from ...models import HADeviceRecord

        with Session(engine) as session:
            session.exec(_delete(HADeviceRecord))  # type: ignore[call-overload]
            for d in ha_devices:
                uid_set_list = sorted(d.get("matter_uid_set", frozenset()))
                session.add(
                    HADeviceRecord(
                        ha_device_id=d["id"],
                        name=d.get("name") or "",
                        manufacturer=d.get("manufacturer") or "",
                        model=d.get("model") or "",
                        area_name=d.get("area_name") or "",
                        area_id=d.get("area_id") or "",
                        identifiers_json=json.dumps(d.get("identifiers") or []),
                        connections_json=json.dumps(d.get("connections") or []),
                        matter_uid_set_json=json.dumps(uid_set_list),
                        fabric_id=d.get("fabric_id"),
                        node_id=d.get("node_id"),
                        serial=d.get("serial"),
                        matter_unique_id=d.get("matter_unique_id"),
                        protocol=d.get("protocol"),
                        sw_version=d.get("sw_version"),
                        hw_version=d.get("hw_version"),
                    )
                )
            session.commit()

        self._ha_devices = ha_devices
        self._ha_devices_fetched = now

    def project(self, session: object) -> SyncResult:  # type: ignore[override]
        """Phase 2: read HADeviceRecord staging data and sync ha_* Device columns.

        The provided session is ignored; _sync_devices manages its own session.
        """
        from sqlmodel import Session, select

        from ...database import engine
        from ...models import HADeviceRecord

        with Session(engine) as db:
            records = db.exec(select(HADeviceRecord)).all()
        ha_devices = [r.to_ha_dict() for r in records]
        result = self._sync_devices(ha_devices, reason="ha_core.project")
        return self._record_sync(
            SyncResult(
                created=result["created"], updated=result["updated"], skipped=result["skipped"]
            )
        )

    # ── Core fetch / sync logic ───────────────────────────────────────────────

    def _sync_devices(self, ha_devices: list[dict], *, reason: str) -> dict:
        """Sync DeviceLink rows and Device fields for all MR Device rows.

        Protocol-aware create/correlate split:
        - Matter devices: correlate-only. HA Core must never create a Matter
          Device (the Matter Server owns creation). Links via existing Matter
          correlate keys.
        - HomeKit devices: HA Core creates + correlates (it is the only
          HomeKit device source). Serial-based dedupe prevents duplicate
          creation; stale-link re-point handles re-paired HA devices.
        """
        from sqlmodel import Session, select

        from ...audit import log as audit_log
        from ...database import engine
        from ...models import (
            Device,
            DeviceFabricMembership,
            DeviceLink,
            DeviceLinkSource,
            Fabric,
            FieldSource,
        )
        from ...services import set_field
        from ..data import upsert as upsert_integration_data
        from .correlate import _is_placeholder, auto_correlate

        ha_by_id = {d["id"]: d for d in ha_devices}
        matter_ha = [d for d in ha_devices if d.get("protocol") == "matter"]
        homekit_ha = [d for d in ha_devices if d.get("protocol") == "homekit"]
        now = datetime.now(UTC)
        created = 0
        updated = 0
        skipped = 0

        with Session(engine) as session:
            # Pre-compute (fabric_id_hex, node_id_int) → device_id map for Key 3.
            membership_by_device: dict[str, set[tuple[str, int]]] = {}
            rows = session.exec(
                select(DeviceFabricMembership, Fabric).join(
                    Fabric,
                    DeviceFabricMembership.fabric_id == Fabric.id,  # type: ignore[arg-type]
                )
            ).all()
            for dfm, fab in rows:
                key: tuple[str, int] = (fab.fabric_id.lower(), dfm.node_id)
                membership_by_device.setdefault(dfm.device_id, set()).add(key)

            # Load existing HA links keyed by device_id.
            existing_links: dict[str, DeviceLink] = {
                lnk.device_id: lnk
                for lnk in session.exec(
                    select(DeviceLink).where(DeviceLink.integration == "ha_core")  # type: ignore[attr-defined]
                ).all()
            }

            # ── Matter: correlate-only ────────────────────────────────────────
            matter_devices = session.exec(
                select(Device).where(Device.protocol == DeviceProtocol.matter)  # type: ignore[union-attr]
            ).all()
            for dev in matter_devices:
                link = existing_links.get(dev.id)
                if link:
                    ha_dev = ha_by_id.get(link.external_id)
                    if ha_dev:
                        changed = False
                        changed |= set_field(
                            dev, "name", ha_dev.get("name") or None, FieldSource.ha
                        )
                        changed |= set_field(
                            dev, "room", ha_dev.get("area_name") or None, FieldSource.ha
                        )
                        ha_uid = ha_dev.get("matter_unique_id")
                        if ha_uid:
                            changed |= set_field(dev, "matter_unique_id", ha_uid, FieldSource.ha)
                        if changed:
                            dev.updated_at = now
                            session.add(dev)
                            updated += 1
                        link.linked_at = now
                        session.add(link)
                else:
                    ha_device_id = auto_correlate(
                        dev,
                        matter_ha,
                        memberships=membership_by_device.get(dev.id, set()),
                    )
                    if ha_device_id is None:
                        continue
                    ha_dev = ha_by_id.get(ha_device_id, {})
                    set_field(dev, "name", ha_dev.get("name") or None, FieldSource.ha)
                    set_field(dev, "room", ha_dev.get("area_name") or None, FieldSource.ha)
                    ha_uid = ha_dev.get("matter_unique_id")
                    if ha_uid:
                        set_field(dev, "matter_unique_id", ha_uid, FieldSource.ha)
                    dev.updated_at = now
                    session.add(dev)
                    new_link = DeviceLink(
                        device_id=dev.id,
                        integration="ha_core",
                        external_id=ha_device_id,
                        link_source=DeviceLinkSource.auto,
                        linked_at=now,
                    )
                    session.add(new_link)
                    existing_links[dev.id] = new_link
                    audit_log(
                        session,
                        action="ha_core.auto_link",
                        entity=f"device:{dev.id}",
                        reason=f"ha_device_id:{ha_device_id}",
                    )
                    created += 1

            # ── HomeKit: create + correlate ───────────────────────────────────
            hk_devices = session.exec(
                select(Device).where(Device.protocol == DeviceProtocol.homekit)  # type: ignore[union-attr]
            ).all()
            hk_by_serial_lower: dict[str, Device] = {}
            hk_by_accessory_id: dict[str, Device] = {}
            for d in hk_devices:
                if d.serial and not _is_placeholder(d.serial):
                    hk_by_serial_lower[d.serial.lower()] = d
                if d.homekit_accessory_id:
                    hk_by_accessory_id[d.homekit_accessory_id] = d

            for hd in homekit_ha:
                ha_id = hd["id"]
                serial = hd.get("serial")
                accessory_id = _homekit_accessory_id(hd.get("identifiers") or [])
                hk_device: Device | None = None
                is_stale_repoint = False
                created_new = False

                # Check existing link by HA device ID
                for dev_id, link in existing_links.items():
                    if link.external_id == ha_id:
                        hk_device = session.get(Device, dev_id)
                        break

                # HAP accessory-id dedupe - matches mDNS-discovered rows with no serial
                if hk_device is None and accessory_id:
                    hk_device = hk_by_accessory_id.get(accessory_id)

                # Serial-based dedupe if not linked
                if hk_device is None and serial and not _is_placeholder(serial):
                    hk_device = hk_by_serial_lower.get(serial.lower())

                # Serial-based stale-link re-point
                if hk_device is not None:
                    existing_link = existing_links.get(hk_device.id)
                    if existing_link and existing_link.external_id != ha_id:
                        # Re-paired HomeKit accessory gets new HA device ID
                        existing_link.external_id = ha_id
                        existing_link.linked_at = now
                        session.add(existing_link)
                        is_stale_repoint = True
                        audit_log(
                            session,
                            action="ha_core.link_repoint",
                            entity=f"device:{hk_device.id}",
                            reason=f"old_ha_id:{existing_link.external_id}→new_ha_id:{ha_id}",
                        )

                # Create new HomeKit device
                if hk_device is None:
                    if not serial or _is_placeholder(serial):
                        skipped += 1
                        continue
                    dev_name = (
                        hd.get("name")
                        or f"HomeKit {hd.get('manufacturer', '')} {hd.get('model', '')}".strip()
                        or "HomeKit Device"
                    )
                    hk_device = Device(
                        name=dev_name,
                        name_source=FieldSource.ha,
                        vendor=hd.get("manufacturer") or None,
                        vendor_source=FieldSource.ha
                        if hd.get("manufacturer")
                        else FieldSource.generated,
                        product=hd.get("model") or None,
                        product_source=FieldSource.ha if hd.get("model") else FieldSource.generated,
                        serial=serial,
                        serial_source=FieldSource.ha if serial else FieldSource.generated,
                        room=hd.get("area_name") or None,
                        room_source=FieldSource.ha
                        if hd.get("area_name")
                        else FieldSource.generated,
                        hardware_version=hd.get("hw_version"),
                        hardware_version_source=FieldSource.ha
                        if hd.get("hw_version")
                        else FieldSource.generated,
                        firmware_version=hd.get("sw_version"),
                        firmware_version_source=FieldSource.ha
                        if hd.get("sw_version")
                        else FieldSource.generated,
                        mac_address=hd.get("mac_address"),
                        mac_address_source=FieldSource.ha
                        if hd.get("mac_address")
                        else FieldSource.generated,
                        network_type=hd.get("network_type") or [],
                        network_type_source=FieldSource.ha
                        if hd.get("network_type")
                        else FieldSource.generated,
                        protocol=DeviceProtocol.homekit,
                        homekit_accessory_id=accessory_id,
                        created_at=now,
                        updated_at=now,
                    )
                    session.add(hk_device)
                    session.flush()
                    created_new = True

                # Refresh fields on an existing (correlated) HomeKit device;
                # a just-created one already has every field set above.
                if not created_new:
                    changed = False
                    changed |= set_field(hk_device, "name", hd.get("name") or None, FieldSource.ha)
                    changed |= set_field(
                        hk_device, "room", hd.get("area_name") or None, FieldSource.ha
                    )
                    changed |= set_field(
                        hk_device, "vendor", hd.get("manufacturer") or None, FieldSource.ha
                    )
                    changed |= set_field(
                        hk_device, "product", hd.get("model") or None, FieldSource.ha
                    )
                    changed |= set_field(hk_device, "serial", serial, FieldSource.ha)
                    if hd.get("hw_version"):
                        changed |= set_field(
                            hk_device, "hardware_version", hd["hw_version"], FieldSource.ha
                        )
                    if hd.get("sw_version"):
                        changed |= set_field(
                            hk_device, "firmware_version", hd["sw_version"], FieldSource.ha
                        )
                    mac = hd.get("mac_address")
                    if mac:
                        changed |= set_field(hk_device, "mac_address", mac, FieldSource.ha)
                    nt = hd.get("network_type") or []
                    if nt:
                        changed |= set_field(hk_device, "network_type", nt, FieldSource.ha)
                    # Backfill the shared HomeKit identity key (dedupe with mDNS, D.3).
                    if accessory_id and not hk_device.homekit_accessory_id:
                        hk_device.homekit_accessory_id = accessory_id
                        changed = True
                    if changed:
                        hk_device.updated_at = now
                        session.add(hk_device)
                        updated += 1

                # Create or update DeviceLink
                existing_link = existing_links.get(hk_device.id)
                if existing_link:
                    if existing_link.external_id != ha_id:
                        existing_link.external_id = ha_id
                        existing_link.linked_at = now
                        session.add(existing_link)
                else:
                    new_hk_link = DeviceLink(
                        device_id=hk_device.id,
                        integration="ha_core",
                        external_id=ha_id,
                        link_source=DeviceLinkSource.auto,
                        linked_at=now,
                    )
                    session.add(new_hk_link)
                    existing_links[hk_device.id] = new_hk_link
                    if not is_stale_repoint:
                        audit_log(
                            session,
                            action="ha_core.auto_link_homekit",
                            entity=f"device:{hk_device.id}",
                            reason=f"ha_device_id:{ha_id}",
                        )
                    created += 1

            # ── B.12: write per-device integration data for each linked device ──
            # Reuses existing_links (new links registered above as they're made),
            # so no extra query and always in sync with what we linked this run.
            for device_id, lnk in existing_links.items():
                ha_dev = ha_by_id.get(lnk.external_id)
                if ha_dev:
                    upsert_integration_data(
                        session,
                        device_id=device_id,
                        integration=self.slug,
                        payload={
                            "name": ha_dev.get("name"),
                            "manufacturer": ha_dev.get("manufacturer"),
                            "model": ha_dev.get("model"),
                            "area_name": ha_dev.get("area_name"),
                            "sw_version": ha_dev.get("sw_version"),
                            "hw_version": ha_dev.get("hw_version"),
                            "protocol": ha_dev.get("protocol"),
                            "source": "ha_core",
                        },
                    )

            audit_log(session, action="ha_core.sync", entity="ha_core", reason=reason)
            self.assert_capabilities(session, created=created)
            session.commit()

        return {"created": created, "updated": updated, "skipped": skipped}

    def get_device_state_from_cache(self, ha_device_id: str) -> dict | None:
        """Return live-state data from the in-memory states cache (synchronous).

        Returns None when cache is empty (first poll not yet completed).
        Returns {online, battery, signal, entities} otherwise.
        """
        if not self._states_cache:
            return None

        entities = [
            s
            for s in self._states_cache
            if s.get("attributes", {}).get("device_id") == ha_device_id
        ]
        if not entities:
            return None

        battery: int | None = None
        signal: int | None = None
        any_available = False

        for s in entities:
            attrs = s.get("attributes", {})
            state = s.get("state", "")
            device_class = attrs.get("device_class", "")

            if state != "unavailable":
                any_available = True

            if device_class == "battery" and battery is None:
                try:
                    battery = int(float(state))
                except (ValueError, TypeError):
                    pass

            if device_class == "signal_strength" and signal is None:
                try:
                    signal = int(float(state))
                except (ValueError, TypeError):
                    pass

        return {
            "online": any_available if entities else None,
            "battery": battery,
            "signal": signal,
            "entities": [
                {
                    "entity_id": s.get("entity_id"),
                    "state": s.get("state"),
                    "device_class": s.get("attributes", {}).get("device_class"),
                    "unit": s.get("attributes", {}).get("unit_of_measurement"),
                }
                for s in entities
            ],
        }

    async def get_ha_devices(self) -> list[dict]:
        """Return HA device list (used by link picker). Fetches if cache empty."""
        if not self._ha_devices:
            await self.ingest()
        return list(self._ha_devices)

    async def get_device_state(self, ha_device_id: str) -> dict:
        """Fetch fresh entity states from HA and return state for ha_device_id."""
        async with httpx.AsyncClient(timeout=10, headers=self._headers()) as http:
            resp = await http.get(f"{self._url}/api/states")
            resp.raise_for_status()
            self._states_cache = resp.json()
            self._states_fetched = datetime.now(UTC)

        result = self.get_device_state_from_cache(ha_device_id)
        return result or {"online": None, "battery": None, "signal": None, "entities": []}

    async def _poll_once(self) -> None:
        """One HA poll cycle: ingest + sync_devices.  Raises PermanentError on 401."""
        self._status = ClientStatus.connecting
        try:
            await self.poll_once(reason="ha_core.background_poll")
        except httpx.HTTPStatusError as exc:
            self._status = ClientStatus.error
            if exc.response.status_code == 401:
                # Wrong / expired token - retrying with the same credentials
                # won't ever succeed.  Stop the loop.
                # Recovery: update HA_CORE_TOKEN env var and restart the
                # container, or POST /api/integrations/ha-core/config with
                # {"token": "<new_token>"} to update without a restart.
                logger.error(
                    "HA Core authentication failed (401 Unauthorized). "
                    "To reconnect: set a new token via HA_CORE_TOKEN env var "
                    "(and restart), or POST /api/integrations/ha-core/config "
                    'with {"token": "<new_long_lived_token>"}.'
                )
                raise PermanentError(str(exc)) from exc
            raise
        except Exception:
            self._status = ClientStatus.error
            raise
        self._status = ClientStatus.connected

    def _on_poll_error(self, exc: Exception, attempt: int, delay: int) -> None:
        logger.warning("HA Core poll failed (attempt %d, retry in %ds): %s", attempt, delay, exc)
