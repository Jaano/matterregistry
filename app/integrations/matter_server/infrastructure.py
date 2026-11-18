"""Project saved topology infrastructure into durable registry Devices."""

from __future__ import annotations

import uuid
from typing import Any

from sqlmodel import Session, select

from ...audit import log as audit_log
from ...models import Device, DeviceLink, DeviceLinkSource, DeviceStatus, FieldSource, Product
from ..correlate import AddressIndex
from .topology import infrastructure_identity, node_label

_DEFAULT_PRODUCTS = {
    "topology_border_router": ("Thread Border Router", "thread"),
    "topology_wifi_ap": ("Wi-Fi Router / Access Point", "wifi"),
}


def _default_product(session: Session, integration: str) -> Product:
    name, _ = _DEFAULT_PRODUCTS[integration]
    product_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"matterregistry:product:{integration}"))
    product = session.get(Product, product_id)
    if product is None:
        product = Product(id=product_id, name=name)
        session.add(product)
        session.flush()
    return product


def project_infrastructure(session: Session, topology: dict[str, Any]) -> int:
    """Correlate identified BR/AP nodes without changing existing Device fields.

    The caller has already persisted *topology*. This phase reads only that
    snapshot and local records. Unidentified nodes are left as graph-only nodes.
    """
    nodes = topology.get("nodes")
    if not isinstance(nodes, list):
        return 0
    address_index = AddressIndex(session)
    created = 0
    seen: dict[tuple[str, str], str] = {}
    devices_by_address: dict[str, str] = {}
    for node in nodes:
        if not isinstance(node, dict):
            continue
        identity = infrastructure_identity(node)
        if identity is None or identity in seen:
            continue
        integration, address = identity
        link = session.exec(
            select(DeviceLink).where(
                DeviceLink.integration == integration, DeviceLink.external_id == address
            )
        ).first()
        device = session.get(Device, link.device_id) if link else None
        if link is not None and device is None:
            session.delete(link)
            session.flush()
        if device is None:
            owner_id = devices_by_address.get(address) or address_index.match(macs=[address])
            if owner_id:
                device = session.get(Device, owner_id)
            elif address_index.is_ambiguous(macs=[address]):
                continue
        if device is not None and link is None:
            other_link = session.exec(
                select(DeviceLink).where(
                    DeviceLink.device_id == device.id,
                    DeviceLink.integration == integration,
                )
            ).first()
            if other_link is not None and other_link.external_id != address:
                continue
        if device is None:
            product = _default_product(session, integration)
            _, transport = _DEFAULT_PRODUCTS[integration]
            device = Device(
                name=node_label(node),
                name_source=FieldSource.matter,
                product_record_id=product.id,
                status=DeviceStatus.hidden,
                network_type=[transport],
                network_type_source=FieldSource.matter,
                mac_address=address,
                mac_address_source=FieldSource.matter,
            )
            session.add(device)
            session.flush()
            audit_log(
                session,
                action="matter_server.infrastructure_create",
                entity=f"device:{device.id}",
                reason=integration,
            )
            created += 1
        if link is None or link.device_id != device.id:
            session.add(
                DeviceLink(
                    device_id=device.id,
                    integration=integration,
                    external_id=address,
                    link_source=DeviceLinkSource.auto,
                )
            )
        seen[identity] = device.id
        devices_by_address[address] = device.id
    return created
