"""Build the full-backup JSON export envelope."""

import base64
from datetime import UTC, datetime

from sqlalchemy import text
from sqlmodel import Session, select

from .models import (
    PRODUCT_SOURCED_FIELDS,
    SOURCED_FIELDS,
    Attachment,
    Device,
    DeviceFabricMembership,
    DeviceLink,
    Fabric,
    FieldSource,
    Product,
    ProductLink,
    Property,
    ThreadNetwork,
)


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.isoformat()


def _get_schema_version(session: Session) -> str | None:
    try:
        row = session.execute(text("SELECT version_num FROM alembic_version LIMIT 1")).first()
        return row[0] if row else None
    except Exception:
        return None


def build_export(session: Session, *, app_version: str) -> dict:
    """Return the export envelope dict. Caller handles JSON serialisation."""
    products_out = [
        {
            "id": product.id,
            "name": product.name,
            "protocol": product.protocol.value if product.protocol else None,
            "vendor": product.vendor,
            "model": product.model,
            "vendor_id": product.vendor_id,
            "product_id": product.product_id,
            "description": product.description,
            "created_at": _iso(product.created_at),
            "updated_at": _iso(product.updated_at),
            "_sources": {
                field: getattr(product, f"{field}_source", FieldSource.generated).value
                for field in PRODUCT_SOURCED_FIELDS
            },
        }
        for product in session.exec(select(Product)).all()
    ]
    product_links_out = [
        {
            "id": link.id,
            "product_record_id": link.product_record_id,
            "kind": link.kind.value,
            "url": link.url,
            "label": link.label,
            "alt_text": link.alt_text,
            "position": link.position,
        }
        for link in session.exec(select(ProductLink)).all()
    ]
    devices_out = []
    for device in session.exec(select(Device)).all():
        properties = session.exec(select(Property).where(Property.device_id == device.id)).all()
        attachments = session.exec(
            select(Attachment).where(Attachment.device_id == device.id)
        ).all()
        devices_out.append(
            {
                "id": device.id,
                "name": device.name,
                "product_record_id": device.product_record_id,
                "room": device.room,
                "vendor": device.vendor,
                "product": device.product,
                "device_model": device.device_model,
                "vendor_id": device.vendor_id,
                "product_id": device.product_id,
                "serial": device.serial,
                "hardware_version": device.hardware_version,
                "firmware_version": device.firmware_version,
                "matter_unique_id": device.matter_unique_id,
                "homekit_accessory_id": device.homekit_accessory_id,
                "notes": device.notes,
                "purchase_date": str(device.purchase_date) if device.purchase_date else None,
                "warranty_until": str(device.warranty_until) if device.warranty_until else None,
                "status": device.status.value,
                "protocol": device.protocol.value if device.protocol else None,
                "created_at": _iso(device.created_at),
                "updated_at": _iso(device.updated_at),
                # Provenance flags (format_version 3+)
                "_sources": {
                    f: getattr(device, f"{f}_source", FieldSource.generated).value
                    if isinstance(getattr(device, f"{f}_source", None), FieldSource)
                    else str(getattr(device, f"{f}_source", FieldSource.generated.value))
                    for f in SOURCED_FIELDS
                },
                "properties": [
                    {
                        "id": c.id,
                        "device_id": c.device_id,
                        "type": c.type.value,
                        "value": c.value,
                        "label": c.label,
                        "source": c.source.value,
                        "captured_at": _iso(c.captured_at),
                    }
                    for c in properties
                ],
                "attachments": [
                    {
                        "id": a.id,
                        "device_id": a.device_id,
                        "kind": a.kind.value,
                        "filename": a.filename,
                        "mime_type": a.mime_type,
                        "sha256": a.sha256,
                        "size_bytes": a.size_bytes,
                        "content_b64": base64.b64encode(a.content).decode(),
                    }
                    for a in attachments
                ],
            }
        )

    fabrics_out = [
        {
            "id": f.id,
            "fabric_label": f.fabric_label,
            "fabric_id": f.fabric_id,
            "controller": f.controller,
            "vendor_id": f.vendor_id,
            "vendor_name": f.vendor_name,
            "root_ca_fingerprint": f.root_ca_fingerprint,
            "notes": f.notes,
        }
        for f in session.exec(select(Fabric)).all()
    ]

    memberships_out = [
        {
            "id": m.id,
            "device_id": m.device_id,
            "fabric_id": m.fabric_id,
            "node_id": m.node_id,
            "endpoint_json": m.endpoint_json,
        }
        for m in session.exec(select(DeviceFabricMembership)).all()
    ]

    thread_networks_out = [
        {
            "id": tn.id,
            "name": tn.name,
            "network_name": tn.network_name,
            "ext_pan_id": tn.ext_pan_id,
            "pan_id": tn.pan_id,
            "channel": tn.channel,
            "mesh_local_prefix": tn.mesh_local_prefix,
            "network_key": tn.network_key,
            "pskc": tn.pskc,
            "active_timestamp": tn.active_timestamp,
            "border_router_url": tn.border_router_url,
            "border_agent_id": tn.border_agent_id,
            "ncp_version": tn.ncp_version,
            "last_polled": _iso(tn.last_polled),
            "notes": tn.notes,
        }
        for tn in session.exec(select(ThreadNetwork)).all()
    ]

    # Skip links orphaned by a device delete that predates the I.25 cascade
    # fix - don't perpetuate them into every future backup/restore cycle.
    existing_device_ids = set(session.exec(select(Device.id)).all())
    device_links_out = [
        {
            "id": lnk.id,
            "device_id": lnk.device_id,
            "integration": lnk.integration,
            "external_id": lnk.external_id,
            "link_source": lnk.link_source.value,
            "linked_at": _iso(lnk.linked_at),
        }
        for lnk in session.exec(select(DeviceLink)).all()
        if lnk.device_id in existing_device_ids
    ]

    return {
        "format_version": 9,
        "app_version": app_version,
        "exported_at": datetime.now(UTC).isoformat(),
        "schema_version": _get_schema_version(session),
        "products": products_out,
        "product_links": product_links_out,
        "devices": devices_out,
        "fabrics": fabrics_out,
        "device_fabric_memberships": memberships_out,
        "thread_networks": thread_networks_out,
        "device_links": device_links_out,
    }
