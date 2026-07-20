from __future__ import annotations

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from app.exporter import build_export
from app.importer import apply_import
from app.models import Device, DeviceProtocol, FieldSource, Product
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


def test_export_import_preserves_product_and_flattened_device_values(session: Session):
    product = Product(
        name="Smart bulb",
        protocol=DeviceProtocol.matter,
        vendor="Acme",
        model="B-1",
        vendor_id=0xFFF1,
        product_id=0x8001,
        vendor_source=FieldSource.user,
    )
    device = Device(name="Kitchen bulb", product_record_id=product.id, serial="ABC123")
    device.product_record = product
    session.add(device)
    session.commit()

    payload = build_export(session, app_version="test")
    assert payload["format_version"] == 9
    assert payload["products"][0]["vendor"] == "Acme"
    assert payload["devices"][0]["product_record_id"] == product.id

    target_engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(target_engine)
    with Session(target_engine) as target_session:
        plan = apply_import(target_session, payload)
        assert plan.errors == []
        restored = target_session.exec(select(Device)).one()
        assert restored.product == "Smart bulb"
        assert restored.vendor == "Acme"
        assert restored.vendor_id == 0xFFF1
