"""Unit tests for B.26: integrations resolve a Product instead of writing
removed generic Device columns.

Covers the two correctness properties integration projections must satisfy:
- Matter Server: two Devices with the same complete (vendor_id, product_id)
  identity share one Product, and a later sync updates that shared Product's
  catalog fields via provenance-gated ``set_field``.
- mDNS: HomeKit devices with no global SKU identity never share a Product,
  even when their model string matches (no fuzzy-merge).
"""

from __future__ import annotations

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import app.database
from app.integrations.matter_server.server_client import NodeInfo, _apply_nodes
from app.integrations.mdns.client import MdnsClient, project_discovered
from app.models import Device, DeviceProtocol, FieldSource, Product


@pytest.fixture
def engine(monkeypatch):
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(eng)
    monkeypatch.setattr(app.database, "engine", eng)
    return eng


def _node(node_id: int, **over) -> NodeInfo:
    base = dict(
        node_id=node_id,
        available=True,
        vendor_id=0xFFF1,
        vendor_name="Acme",
        product_id=0x8001,
        product_name="Smart Bulb",
        serial=f"S{node_id}",
        hardware_version_string="1.0",
        firmware_version_string="1.0",
        node_label=None,
        unique_id=f"uid-{node_id}",
        manufacturing_date=None,
        product_url=None,
        part_number=None,
    )
    base.update(over)
    return NodeInfo(**base)


def test_matter_sync_shares_product_across_same_sku_devices(engine):
    with Session(engine) as s:
        result = _apply_nodes(s, [_node(1), _node(2)], integration=None)
        assert result["create"] == 2
        assert result["product_create"] == 1

        devices = list(s.exec(select(Device)).all())
        assert len(devices) == 2
        product_ids = {d.product_record_id for d in devices}
        assert len(product_ids) == 1

        products = list(s.exec(select(Product)).all())
        assert len(products) == 1
        assert products[0].vendor == "Acme"
        assert products[0].name == "Smart Bulb"


def test_matter_sync_updates_shared_product_catalog_fields(engine):
    with Session(engine) as s:
        _apply_nodes(s, [_node(1)], integration=None)

    with Session(engine) as s:
        result = _apply_nodes(s, [_node(1, product_name="Smart Bulb v2")], integration=None)
        assert result["update"] == 1
        assert result["product_update"] == 1
        product = s.exec(select(Product)).one()
        assert product.name == "Smart Bulb v2"
        assert product.name_source is FieldSource.matter


def test_matter_sync_does_not_overwrite_user_edited_product_name(engine):
    with Session(engine) as s:
        _apply_nodes(s, [_node(1)], integration=None)
        product = s.exec(select(Product)).one()
        product.name = "My custom label"
        product.name_source = FieldSource.user
        s.add(product)
        s.commit()

    with Session(engine) as s:
        _apply_nodes(s, [_node(1, product_name="Smart Bulb v2")], integration=None)
        product = s.exec(select(Product)).one()
        assert product.name == "My custom label"


def _acc(acc_id: str, **over) -> dict:
    d = {
        "id": acc_id,
        "name": "Presence-Sensor-FP2-7DD0",
        "model": "PS-S02E",
        "category_id": 10,
        "paired": True,
        "setup_hash": None,
        "transport": "wifi",
    }
    d.update(over)
    return d


def test_homekit_devices_never_fuzzy_merge_products(engine):
    """Two HomeKit devices sharing the same model string still get distinct
    Products - HomeKit has no global SKU identity to dedupe on."""
    client = MdnsClient()
    with Session(engine) as s:
        project_discovered(s, [_acc("1D:C3:F4:EF:74:68")], integration=client)
    with Session(engine) as s:
        project_discovered(s, [_acc("2D:C3:F4:EF:74:69")], integration=client)

    with Session(engine) as s:
        devices = list(s.exec(select(Device)).all())
        assert len(devices) == 2
        assert devices[0].protocol == DeviceProtocol.homekit
        product_ids = {d.product_record_id for d in devices}
        assert len(product_ids) == 2
