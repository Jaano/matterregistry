from __future__ import annotations

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from app.exporter import build_export
from app.importer import apply_import, plan_import
from app.models import (
    Device,
    DeviceFabricMembership,
    DeviceProtocol,
    Fabric,
    FieldSource,
    Product,
    ProductLink,
    ProductLinkKind,
)
from app.services import resolve_product, set_field, set_product_field


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as current_session:
        yield current_session


def test_complete_matter_identity_reuses_product(session: Session):
    first = resolve_product(
        session,
        protocol=DeviceProtocol.matter,
        vendor_id=0xFFF1,
        product_id=0x8001,
    )
    second = resolve_product(
        session,
        protocol=DeviceProtocol.matter,
        vendor_id=0xFFF1,
        product_id=0x8001,
    )

    assert first.id == second.id
    assert len(session.exec(select(Product)).all()) == 1


def test_generic_device_field_write_updates_product_with_provenance(session: Session):
    product = Product(name="Original", protocol=DeviceProtocol.matter)
    device = Device(name="Kitchen light", product_record_id=product.id)
    device.product_record = product
    session.add(device)
    session.commit()

    assert set_field(device, "vendor", "Acme", FieldSource.ha)
    assert device.vendor == "Acme"
    assert device.product_record is not None
    assert device.product_record.vendor_source is FieldSource.ha
    assert set_product_field(device.product_record, "vendor", "Manual", FieldSource.user)
    assert not set_field(device, "vendor", "Lower priority", FieldSource.matter)
    assert device.vendor == "Manual"


@pytest.mark.parametrize("legacy", [False, True])
def test_export_import_preserves_product_and_flattened_device_values(
    session: Session, legacy: bool
):
    product = Product(
        name="Smart bulb",
        protocol=DeviceProtocol.matter,
        vendor="Acme",
        model="B-1",
        vendor_id=0xFFF1,
        product_id=0x8001,
        device_type_id=0xFFF10001,
        device_type_id_source=FieldSource.dcl,
        vendor_source=FieldSource.user,
    )
    device = Device(name="Kitchen bulb", product_record_id=product.id, serial="ABC123")
    device.product_record = product
    session.add(device)
    session.commit()

    payload = build_export(session, app_version="test")
    assert payload["format_version"] == 9
    assert payload["products"][0]["vendor"] == "Acme"
    assert payload["products"][0]["device_type_id"] == 0xFFF10001
    assert payload["products"][0]["_sources"]["device_type_id"] == "dcl"
    assert payload["devices"][0]["product_record_id"] == product.id

    if legacy:
        payload["products"][0].pop("device_type_id")
        payload["products"][0]["_sources"].pop("device_type_id")

    target_engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(target_engine)
    with Session(target_engine) as target_session:
        plan = apply_import(target_session, payload)
        assert plan.errors == []
        restored = target_session.exec(select(Device)).one()
        assert restored.product == "Smart bulb"
        assert restored.vendor == "Acme"
        assert restored.vendor_id == 0xFFF1
        assert restored.product_record is not None
        assert restored.product_record.device_type_id == (None if legacy else 0xFFF10001)
        expected_source = FieldSource.generated if legacy else FieldSource.dcl
        assert restored.product_record.device_type_id_source is expected_source


@pytest.mark.parametrize(
    ("protocol", "type_id"), [("homekit", 266), (None, 266), ("matter", 0), ("matter", True)]
)
def test_backup_invalid_device_type_stops_replace(session: Session, protocol, type_id):
    product = Product(name="Existing Product")
    session.add(product)
    session.commit()
    product_id = product.id
    payload = build_export(session, app_version="test")
    payload["products"][0].update(protocol=protocol, device_type_id=type_id)

    plan = apply_import(session, payload, policy="replace")

    assert len(plan.errors) == 1
    assert "Matter device type" in plan.errors[0]
    assert session.get(Product, product_id) is not None


def test_skip_import_reuses_matter_product_with_a_different_record_id(session: Session):
    product = Product(
        name="Local plug", protocol=DeviceProtocol.matter, vendor_id=0xFFF1, product_id=0x8001
    )
    existing_link = ProductLink(
        product_record_id=product.id,
        kind=ProductLinkKind.homepage,
        url="https://example.com/plug",
        label="My link label",
    )
    session.add(product)
    session.add(existing_link)
    session.commit()
    payload = {
        "format_version": 9,
        "products": [
            {
                "id": "backup-product",
                "name": "Backup plug",
                "protocol": "matter",
                "vendor_id": 0xFFF1,
                "product_id": 0x8001,
            }
        ],
        "product_links": [
            {
                "id": "backup-homepage",
                "product_record_id": "backup-product",
                "kind": "homepage",
                "url": "https://example.com/plug",
            },
            {
                "id": "backup-manual",
                "product_record_id": "backup-product",
                "kind": "documentation",
                "url": "https://example.com/manual.pdf",
            },
        ],
        "devices": [
            {"id": "backup-device", "name": "Bedroom plug", "product_record_id": "backup-product"}
        ],
    }

    preview = plan_import(session, payload)
    assert not preview.errors
    assert preview.skips == ["product:backup-product"]
    assert preview.creates == ["device:backup-device"]

    result = apply_import(session, payload)
    assert not result.errors and result.warnings == preview.warnings
    session.commit()
    assert session.exec(select(Product)).one().name == "Local plug"
    assert session.exec(select(Device)).one().product_record_id == product.id
    assert session.get(ProductLink, "backup-homepage") is None
    assert existing_link.label == "My link label"
    manual = session.get(ProductLink, "backup-manual")
    assert manual is not None and manual.product_record_id == product.id


def test_skip_import_reuses_fabrics_and_preserves_existing_node_assignments(session: Session):
    product = Product(name="Plug")
    local_device = Device(name="Local device", product_record_id=product.id, product_record=product)
    session.add(local_device)
    session.add(Fabric(id=1, fabric_id="shared", controller="Local controller"))
    session.add(DeviceFabricMembership(id=1, device_id=local_device.id, fabric_id=1, node_id=9))
    session.commit()
    payload = {
        "format_version": 9,
        "products": [],
        "devices": [{"id": "backup-device", "name": "Plug", "product_record_id": product.id}],
        "fabrics": [
            {"id": 2, "fabric_id": "shared", "controller": "Backup controller"},
            {"id": 1, "fabric_id": "another", "controller": "Another controller"},
        ],
        "device_fabric_memberships": [
            {"id": 2, "device_id": "backup-device", "fabric_id": 2, "node_id": 9},
            {"id": 1, "device_id": "backup-device", "fabric_id": 2, "node_id": 10},
            {"id": 3, "device_id": "backup-device", "fabric_id": 1, "node_id": 9},
        ],
    }

    preview = plan_import(session, payload)
    result = apply_import(session, payload)
    assert not result.errors and result.warnings == preview.warnings
    assert result.skips == ["fabric:2", "membership:2"]
    session.commit()
    fabrics = {row.fabric_id: row for row in session.exec(select(Fabric))}
    assert fabrics["shared"].controller == "Local controller"
    assert fabrics["another"].id != fabrics["shared"].id
    memberships = {
        (row.fabric_id, row.node_id): row.device_id
        for row in session.exec(select(DeviceFabricMembership))
    }
    assert memberships == {
        (fabrics["shared"].id, 9): local_device.id,
        (fabrics["shared"].id, 10): "backup-device",
        (fabrics["another"].id, 9): "backup-device",
    }
    retry = apply_import(session, payload)
    assert not retry.errors and not retry.creates
