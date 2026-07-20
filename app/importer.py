"""Import backup JSON envelope into the database."""

import base64
import copy
import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime

from sqlalchemy import text
from sqlmodel import Session, select

from .models import (
    Attachment,
    AttachmentKind,
    Device,
    DeviceFabricMembership,
    DeviceLink,
    DeviceLinkSource,
    DeviceProtocol,
    DeviceStatus,
    Fabric,
    FieldSource,
    Product,
    ProductLink,
    ProductLinkKind,
    Property,
    PropertyType,
    ThreadNetwork,
)

# Legacy PropertySource values map onto the unified FieldSource.
# "imported" was removed from FieldSource; old backups using it get no badge.
_LEGACY_PROP_SOURCE = {"manual": "user", "imported": "generated"}


def _prop_source(raw: str | None) -> FieldSource:
    """Map a backup's property source string onto FieldSource, honouring legacy values."""
    value = raw or "user"
    return FieldSource(_LEGACY_PROP_SOURCE.get(value, value))


_VALID_STATUS = {s.value for s in DeviceStatus}
_VALID_PROTOCOL = {s.value for s in DeviceProtocol}
_VALID_PROP_TYPE = {s.value for s in PropertyType}
_VALID_PROP_SOURCE = {s.value for s in FieldSource} | set(_LEGACY_PROP_SOURCE)
_VALID_ATT_KIND = {s.value for s in AttachmentKind}
_VALID_FIELD_SOURCE = {s.value for s in FieldSource}


def _normal(value: object) -> str:
    return " ".join(str(value or "").casefold().split())


def _upgrade_legacy_products(payload: dict) -> dict:
    """Return a v9-shaped copy, conservatively synthesising Products for old backups."""
    if payload.get("products") is not None:
        return payload
    upgraded = copy.deepcopy(payload)
    products: list[dict] = []
    products_by_key: dict[tuple, dict] = {}
    for device in upgraded.get("devices", []):
        protocol = device.get("protocol", "matter")
        vendor_id = device.get("vendor_id")
        product_id = device.get("product_id")
        descriptive = tuple(
            _normal(device.get(field)) for field in ("vendor", "product", "device_model")
        )
        key: tuple[object, ...]
        if protocol == "matter" and vendor_id is not None and product_id is not None:
            key = ("matter", vendor_id, product_id)
        elif any(descriptive):
            key = ("descriptive", protocol, *descriptive)
        else:
            key = ("dedicated", device["id"])
        product = products_by_key.get(key)
        if product is None:
            sources = device.get("_sources") or {}
            product = {
                "id": str(uuid.uuid4()),
                "name": device.get("product") or f"Unresolved product for {device['name']}",
                "protocol": protocol,
                "vendor": device.get("vendor"),
                "model": device.get("device_model"),
                "vendor_id": vendor_id,
                "product_id": product_id,
                "description": None,
                "_sources": {
                    "name": sources.get("product", "generated"),
                    "vendor": sources.get("vendor", "generated"),
                    "model": sources.get("device_model", "generated"),
                    "vendor_id": sources.get("vendor_id", "generated"),
                    "product_id": sources.get("product_id", "generated"),
                    "description": "generated",
                },
            }
            products_by_key[key] = product
            products.append(product)
        device["product_record_id"] = product["id"]
    upgraded["products"] = products
    upgraded["product_links"] = []
    upgraded["format_version"] = 9
    return upgraded


@dataclass
class ImportPlan:
    creates: list[str] = field(default_factory=list)
    updates: list[str] = field(default_factory=list)
    skips: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _get_current_revision(session: Session) -> str | None:
    try:
        row = session.execute(text("SELECT version_num FROM alembic_version LIMIT 1")).first()
        return row[0] if row else None
    except Exception:
        return None


def _validate_top(payload: dict) -> list[str]:
    errors = []
    if not isinstance(payload, dict):
        return ["Payload is not a JSON object"]
    # Accept format_version 1-9; v1-3 may have a 'settings' key which is now ignored;
    # v4 may have a 'location_text' field on devices which is silently ignored;
    # v5 used 'credentials' key (renamed to 'properties' in v6);
    # v6 had ha_device_id on the device dict (moved to device_links in v7);
    # v7 used PropertySource 'manual' (mapped to FieldSource 'user' in v8).
    if payload.get("format_version") not in (1, 2, 3, 4, 5, 6, 7, 8, 9):
        errors.append(
            f"Unsupported format_version: {payload.get('format_version')!r} (expected 1-8)"
        )
    if "devices" not in payload:
        errors.append("Missing required key: 'devices'")
    return errors


def _validate_device(d: dict) -> list[str]:
    errors = []
    if not isinstance(d.get("id"), str) or not d["id"]:
        errors.append(f"Device missing valid 'id' (name={d.get('name', '?')!r})")
        return errors
    if not d.get("name"):
        errors.append(f"Device {d['id']}: missing 'name'")
    if d.get("status", "active") not in _VALID_STATUS:
        errors.append(f"Device {d['id']}: unknown status {d.get('status')!r}")
    # protocol may be None (device never scanned); absent defaults to matter (legacy).
    proto = d.get("protocol", "matter")
    if proto is not None and proto not in _VALID_PROTOCOL:
        errors.append(f"Device {d['id']}: unknown protocol {proto!r}")
    return errors


def _validate_property(c: dict, dev_id: str) -> list[str]:
    errors = []
    cid = c.get("id", "?")
    if c.get("type") not in _VALID_PROP_TYPE:
        errors.append(f"Property {cid} (device {dev_id}): unknown type {c.get('type')!r}")
    src = c.get("source", "user")
    if src not in _VALID_PROP_SOURCE:
        errors.append(f"Property {cid} (device {dev_id}): unknown source {src!r}")
    return errors


def _validate_attachment(a: dict, dev_id: str) -> list[str]:
    errors = []
    aid = a.get("id", "?")
    if a.get("kind") not in _VALID_ATT_KIND:
        errors.append(f"Attachment {aid} (device {dev_id}): unknown kind {a.get('kind')!r}")
    content_b64 = a.get("content_b64")
    if not content_b64:
        errors.append(f"Attachment {aid} (device {dev_id}): missing content_b64")
        return errors
    try:
        content = base64.b64decode(content_b64)
    except Exception:
        errors.append(f"Attachment {aid} (device {dev_id}): invalid base64")
        return errors
    expected = a.get("sha256", "")
    if expected and hashlib.sha256(content).hexdigest() != expected:
        errors.append(f"Attachment {aid} (device {dev_id}): sha256 mismatch")
    return errors


def plan_import(session: Session, payload: dict, *, policy: str = "skip") -> ImportPlan:
    payload = _upgrade_legacy_products(payload)
    plan = ImportPlan()
    top_errors = _validate_top(payload)
    if top_errors:
        plan.errors.extend(top_errors)
        return plan

    current_rev = _get_current_revision(session)
    backup_rev = payload.get("schema_version")
    if backup_rev is None:
        plan.warnings.append(
            f"Backup has no schema_version (legacy). Current is {current_rev!r}. "
            "Newer columns will load as NULL / extra columns will be ignored."
        )
    elif backup_rev != current_rev:
        plan.warnings.append(
            f"Backup was taken at schema {backup_rev!r}; current is {current_rev!r}. "
            "Newer columns will load as NULL / extra columns will be ignored."
        )

    existing_product_ids = {row.id for row in session.exec(select(Product)).all()}
    product_ids = set(existing_product_ids)
    for product in payload.get("products", []):
        product_id = product.get("id")
        if not product_id or not product.get("name"):
            plan.errors.append("Product missing valid id or name")
            continue
        product_ids.add(product_id)
        if product_id in existing_product_ids:
            (plan.updates if policy == "replace" else plan.skips).append(f"product:{product_id}")
        else:
            plan.creates.append(f"product:{product_id}")

    existing_ids = {row.id for row in session.exec(select(Device)).all()}

    for d in payload.get("devices", []):
        dev_errors = _validate_device(d)
        if dev_errors:
            plan.errors.extend(dev_errors)
            continue

        dev_id = d["id"]
        if d.get("product_record_id") not in product_ids:
            plan.errors.append(f"Device {dev_id}: unknown product_record_id")
            continue
        if dev_id in existing_ids:
            if policy == "replace":
                plan.updates.append(f"device:{dev_id}")
            else:
                plan.skips.append(f"device:{dev_id}")
                continue
        else:
            plan.creates.append(f"device:{dev_id}")

        for c in d.get("properties") or d.get("credentials", []):
            errs = _validate_property(c, dev_id)
            if errs:
                plan.errors.extend(errs)
            else:
                plan.creates.append(f"property:{c.get('id', '?')}")

        for a in d.get("attachments", []):
            errs = _validate_attachment(a, dev_id)
            if errs:
                plan.errors.extend(errs)
            else:
                plan.creates.append(f"attachment:{a.get('id', '?')}")

    existing_fabric_ids = {row.id for row in session.exec(select(Fabric)).all()}
    for f in payload.get("fabrics", []):
        fid = f.get("id")
        if fid in existing_fabric_ids:
            (plan.updates if policy == "replace" else plan.skips).append(f"fabric:{fid}")
        else:
            plan.creates.append(f"fabric:{fid}")

    existing_tn_ids = {row.ext_pan_id for row in session.exec(select(ThreadNetwork)).all()}
    for tn in payload.get("thread_networks", []):
        epid = tn.get("ext_pan_id", "?")
        if epid in existing_tn_ids:
            (plan.updates if policy == "replace" else plan.skips).append(f"thread_network:{epid}")
        else:
            plan.creates.append(f"thread_network:{epid}")

    for m in payload.get("device_fabric_memberships", []):
        plan.creates.append(f"membership:{m.get('id', '?')}")

    return plan


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def apply_import(session: Session, payload: dict, *, policy: str = "skip") -> ImportPlan:
    """Validate then apply. Returns plan (with errors list if invalid)."""
    payload = _upgrade_legacy_products(payload)
    plan = plan_import(session, payload, policy=policy)
    if plan.errors:
        return plan

    if policy == "replace":
        # Full wipe in FK order before inserting backup contents.
        for row in session.exec(select(DeviceFabricMembership)).all():
            session.delete(row)
        for row in session.exec(select(Attachment)).all():  # type: ignore[assignment]
            session.delete(row)
        for row in session.exec(select(Property)).all():  # type: ignore[assignment]
            session.delete(row)
        for row in session.exec(select(DeviceLink)).all():  # type: ignore[assignment]
            session.delete(row)
        for row in session.exec(select(Device)).all():  # type: ignore[assignment]
            session.delete(row)
        for row in session.exec(select(ProductLink)).all():  # type: ignore[assignment]
            session.delete(row)
        for row in session.exec(select(Product)).all():  # type: ignore[assignment]
            session.delete(row)
        for row in session.exec(select(Fabric)).all():  # type: ignore[assignment]
            session.delete(row)
        for row in session.exec(select(ThreadNetwork)).all():  # type: ignore[assignment]
            session.delete(row)
        session.flush()

    # ── Product / ProductLink ───────────────────────────────────────────────
    for product_data in payload.get("products", []):
        product_id = product_data["id"]
        if session.get(Product, product_id):
            continue
        raw_sources: dict[str, str] = product_data.get("_sources") or {}

        def _product_src(field: str) -> FieldSource:
            raw = raw_sources.get(field)
            return FieldSource(raw) if raw in _VALID_FIELD_SOURCE else FieldSource.generated

        protocol = product_data.get("protocol")
        session.add(
            Product(
                id=product_id,
                name=product_data["name"],
                protocol=DeviceProtocol(protocol) if protocol else None,
                vendor=product_data.get("vendor"),
                model=product_data.get("model"),
                vendor_id=product_data.get("vendor_id"),
                product_id=product_data.get("product_id"),
                description=product_data.get("description"),
                name_source=_product_src("name"),
                vendor_source=_product_src("vendor"),
                model_source=_product_src("model"),
                vendor_id_source=_product_src("vendor_id"),
                product_id_source=_product_src("product_id"),
                description_source=_product_src("description"),
            )
        )
    session.flush()
    for link_data in payload.get("product_links", []):
        if session.get(ProductLink, link_data["id"]):
            continue
        session.add(
            ProductLink(
                id=link_data["id"],
                product_record_id=link_data["product_record_id"],
                kind=ProductLinkKind(link_data["kind"]),
                url=link_data["url"],
                label=link_data.get("label"),
                alt_text=link_data.get("alt_text"),
                position=link_data.get("position", 0),
            )
        )

    # ── Device / Credential / Attachment ────────────────────────────────────
    for d in payload.get("devices", []):
        dev_id = d["id"]
        if session.get(Device, dev_id):
            continue  # skip policy: row exists; replace: impossible after wipe

        pd = d.get("purchase_date")
        wu = d.get("warranty_until")
        # Restore per-field provenance.  Fall back to 'imported' for fields
        # that were non-null but had no _sources entry (older backups).
        device_sources: dict[str, str] = d.get("_sources") or {}

        def _src(field: str) -> FieldSource:
            raw = device_sources.get(field)
            if raw in _VALID_FIELD_SOURCE:
                return FieldSource(raw)
            # Source not in current enum (e.g. old "imported" tag) → leave empty.
            return FieldSource.generated

        session.add(
            Device(
                id=dev_id,
                name=d["name"],
                product_record_id=d["product_record_id"],
                name_source=_src("name"),
                room=d.get("room"),
                room_source=_src("room"),
                serial=d.get("serial"),
                serial_source=_src("serial"),
                hardware_version=d.get("hardware_version"),
                hardware_version_source=_src("hardware_version"),
                firmware_version=d.get("firmware_version"),
                firmware_version_source=_src("firmware_version"),
                matter_unique_id=d.get("matter_unique_id"),
                matter_unique_id_source=_src("matter_unique_id"),
                homekit_accessory_id=d.get("homekit_accessory_id"),
                notes=d.get("notes"),
                notes_source=_src("notes"),
                status=DeviceStatus(d.get("status", "active")),
                status_source=_src("status"),
                purchase_date=date.fromisoformat(pd) if pd else None,
                purchase_date_source=_src("purchase_date"),
                warranty_until=date.fromisoformat(wu) if wu else None,
                warranty_until_source=_src("warranty_until"),
            )
        )

        for c in d.get("properties") or d.get("credentials", []):
            session.add(
                Property(
                    id=c["id"],
                    device_id=dev_id,
                    type=PropertyType(c["type"]),
                    value=c["value"],
                    label=c.get("label"),
                    source=_prop_source(c.get("source")),
                )
            )

        for a in d.get("attachments", []):
            content = base64.b64decode(a["content_b64"])
            session.add(
                Attachment(
                    id=a["id"],
                    device_id=dev_id,
                    kind=AttachmentKind(a["kind"]),
                    filename=a["filename"],
                    mime_type=a["mime_type"],
                    sha256=a["sha256"],
                    size_bytes=a["size_bytes"],
                    content=content,
                )
            )
        # Back-compat: v1-6 stored ha_device_id on the device dict.
        # Convert to a DeviceLink row if present and not already in device_links.
        legacy_ha_id = d.get("ha_device_id")
        if legacy_ha_id and not any(
            lnk.get("device_id") == dev_id and lnk.get("integration") == "ha_core"
            for lnk in payload.get("device_links", [])
        ):
            session.add(
                DeviceLink(
                    device_id=dev_id,
                    integration="ha_core",
                    external_id=legacy_ha_id,
                    link_source=DeviceLinkSource.auto,
                )
            )

    # ── DeviceLink ───────────────────────────────────────────────────────────────
    for lnk in payload.get("device_links", []):
        if session.get(DeviceLink, lnk["id"]):
            continue
        # Don't reintroduce a link orphaned before export (or a corrupt backup
        # referencing a device that isn't in this payload) - autoflush above
        # means devices added earlier in this same import are already visible.
        if not session.get(Device, lnk["device_id"]):
            continue
        session.add(
            DeviceLink(
                id=lnk["id"],
                device_id=lnk["device_id"],
                integration=lnk["integration"],
                external_id=lnk["external_id"],
                link_source=DeviceLinkSource(lnk.get("link_source", "auto")),
                linked_at=_parse_dt(lnk.get("linked_at")) or datetime.utcnow(),
            )
        )
    # ── Fabric ───────────────────────────────────────────────────────────────
    for f in payload.get("fabrics", []):
        if session.get(Fabric, f["id"]):
            continue
        session.add(
            Fabric(
                id=f["id"],
                fabric_label=f.get("fabric_label"),
                fabric_id=f["fabric_id"],
                controller=f["controller"],
                vendor_id=f.get("vendor_id"),
                vendor_name=f.get("vendor_name"),
                root_ca_fingerprint=f.get("root_ca_fingerprint"),
                notes=f.get("notes"),
            )
        )

    # ── DeviceFabricMembership ────────────────────────────────────────────────
    for m in payload.get("device_fabric_memberships", []):
        if session.get(DeviceFabricMembership, m["id"]):
            continue
        session.add(
            DeviceFabricMembership(
                id=m["id"],
                device_id=m["device_id"],
                fabric_id=m["fabric_id"],
                node_id=m["node_id"],
                endpoint_json=m.get("endpoint_json", "{}"),
            )
        )

    # ── ThreadNetwork ─────────────────────────────────────────────────────────
    for tn in payload.get("thread_networks", []):
        existing = session.exec(
            select(ThreadNetwork).where(ThreadNetwork.ext_pan_id == tn["ext_pan_id"])
        ).first()
        if existing:
            continue
        session.add(
            ThreadNetwork(
                name=tn["name"],
                network_name=tn["network_name"],
                ext_pan_id=tn["ext_pan_id"],
                pan_id=tn["pan_id"],
                channel=tn["channel"],
                mesh_local_prefix=tn["mesh_local_prefix"],
                network_key=tn["network_key"],
                pskc=tn.get("pskc"),
                active_timestamp=tn.get("active_timestamp"),
                border_router_url=tn["border_router_url"],
                border_agent_id=tn.get("border_agent_id"),
                ncp_version=tn.get("ncp_version"),
                notes=tn.get("notes"),
            )
        )

    return plan
