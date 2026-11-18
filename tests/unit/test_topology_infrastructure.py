"""Topology-only infrastructure gets durable, editable Device records."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select

from app.app import create_app
from app.database import get_session
from app.exporter import build_export
from app.importer import apply_import, plan_import
from app.integrations.matter_server.infrastructure import project_infrastructure
from app.integrations.matter_server.server_client import (
    MatterServerClient,
    _store_topology,
    project_saved_infrastructure,
)
from app.integrations.matter_server.topology import infrastructure_identity
from app.models import Device, DeviceLink, DeviceStatus, FieldSource, Product
from app.services import set_field

BR_EXT = "CA1BCC6DE177F673"
AP_BSSID = "AA:00:00:00:00:01"


def _topology() -> dict:
    return {
        "collected_at": 1760000000000,
        "nodes": [
            {"id": f"br_{BR_EXT}", "kind": "border_router", "host_name": "Hub"},
            {"id": "ap_AA0000000001", "kind": "wifi_ap", "ssid": "Wifi"},
            {"id": "unknown", "kind": "border_router", "host_name": "No address"},
        ],
        "connections": [
            {"source": f"br_{BR_EXT}", "target": "ap_AA0000000001", "network": "thread"}
        ],
    }


@pytest.fixture
def engine(tmp_path: Path):
    db = create_engine(f"sqlite:///{tmp_path / 'infrastructure.db'}")
    SQLModel.metadata.create_all(db)
    return db


def test_identity_accepts_documented_addresses_only():
    assert infrastructure_identity({"kind": "border_router", "id": f"br_{BR_EXT}"}) == (
        "topology_border_router",
        "CA:1B:CC:6D:E1:77:F6:73",
    )
    assert infrastructure_identity({"kind": "wifi_ap", "network_name": AP_BSSID}) == (
        "topology_wifi_ap",
        AP_BSSID,
    )
    assert infrastructure_identity({"kind": "wifi_ap", "id": "ap_not_a_mac"}) is None
    assert infrastructure_identity({"kind": "border_router", "id": "br1"}) is None


def test_snapshot_creates_hidden_devices_and_preserves_edits(engine, monkeypatch):
    monkeypatch.setattr("app.database.engine", engine)
    topology = _topology()
    integration = MatterServerClient("ws://matter-server.invalid/ws")
    _store_topology(topology, integration=integration)
    with Session(engine) as session:
        devices = session.exec(select(Device)).all()
        assert len(devices) == 2
        assert {d.status for d in devices} == {DeviceStatus.hidden}
        products = {p.id: p for p in session.exec(select(Product)).all()}
        assert {p.name for p in products.values()} == {
            "Thread Border Router",
            "Wi-Fi Router / Access Point",
        }
        assert all(
            p.protocol is None and p.vendor is None and p.model is None for p in products.values()
        )
        br = next(d for d in devices if d.network_type == ["thread"])
        wifi = next(d for d in devices if d.network_type == ["wifi"])
        assert br.mac_address == "CA:1B:CC:6D:E1:77:F6:73"
        assert wifi.mac_address == AP_BSSID
        assert br.name == "Hub"
        assert wifi.name == "Wifi"
        assert set_field(br, "room", "Living room", FieldSource.user)
        assert set_field(br, "name", "My border router", FieldSource.user)
        assert set_field(br, "status", DeviceStatus.active, FieldSource.user)
        own_product = Product(name="Specific router")
        session.add(own_product)
        session.flush()
        br.product_record_id = own_product.id
        session.add(br)
        session.commit()
        br_id = br.id

    topology["nodes"][0]["host_name"] = "New upstream name"
    _store_topology(topology, integration=integration)
    with Session(engine) as session:
        assert len(session.exec(select(Device)).all()) == 2
        br = session.get(Device, br_id)
        assert br is not None
        assert (br.name, br.room, br.status, br.product_record.name) == (
            "My border router",
            "Living room",
            DeviceStatus.active,
            "Specific router",
        )
        assert len(session.exec(select(DeviceLink)).all()) == 2

    _store_topology({"nodes": [], "connections": []}, integration=integration)
    with Session(engine) as session:
        assert len(session.exec(select(Device)).all()) == 2


def test_saved_snapshot_backfills_while_matter_server_is_offline(engine, monkeypatch):
    monkeypatch.setattr("app.database.engine", engine)
    from app.models import MatterTopologySnapshot

    with Session(engine) as session:
        session.add(MatterTopologySnapshot(payload_json=json.dumps(_topology())))
        session.commit()
    project_saved_infrastructure()
    with Session(engine) as session:
        assert len(session.exec(select(Device)).all()) == 2


def test_existing_device_is_linked_without_hiding_or_replacing(engine):
    with Session(engine) as session:
        product = Product(name="Known hub")
        session.add(product)
        session.flush()
        existing = Device(
            name="Known hub",
            room="Office",
            product_record_id=product.id,
            mac_address="ca:1b:cc:6d:e1:77:f6:73",
        )
        session.add(existing)
        session.commit()
        existing_id = existing.id

    with Session(engine) as session:
        assert project_infrastructure(session, _topology()) == 1
        session.commit()
        links = session.exec(select(DeviceLink)).all()
        br_link = next(link for link in links if link.integration == "topology_border_router")
        assert br_link.device_id == existing_id
        existing = session.get(Device, existing_id)
        assert existing is not None
        assert (existing.room, existing.status, existing.product_record.name) == (
            "Office",
            DeviceStatus.active,
            "Known hub",
        )


def test_ambiguous_address_does_not_create_third_device(engine):
    with Session(engine) as session:
        product = Product(name="Unknown")
        session.add(product)
        session.flush()
        session.add_all(
            [
                Device(name=name, product_record_id=product.id, mac_address=BR_EXT)
                for name in ("First", "Second")
            ]
        )
        session.commit()
    with Session(engine) as session:
        assert project_infrastructure(session, {"nodes": [_topology()["nodes"][0]]}) == 0
        session.commit()
        assert len(session.exec(select(Device)).all()) == 2
        assert session.exec(select(DeviceLink)).all() == []


def test_network_uses_hidden_device_room_and_links(engine, monkeypatch):
    monkeypatch.setattr("app.database.engine", engine)
    _store_topology(_topology())
    with Session(engine) as session:
        br = next(d for d in session.exec(select(Device)).all() if d.network_type == ["thread"])
        br.room = "Office"
        session.add(br)
        session.commit()
        br_id = br.id

    def _session() -> Iterator[Session]:
        with Session(engine) as session:
            yield session

    app = create_app()
    app.dependency_overrides[get_session] = _session
    client = TestClient(app)
    mesh = client.get("/network")
    table = client.get("/network?view=table")
    visible_devices = client.get("/devices")
    hidden_devices = client.get("/devices?status=hidden")
    products = client.get("/products")
    assert mesh.status_code == table.status_code == 200
    assert 'aria-label="Office"' in mesh.text
    assert f'href="/devices/{br_id}"' in mesh.text
    assert f'href="/devices/{br_id}"' in table.text
    assert "No address" in mesh.text
    assert "Hub" not in visible_devices.text
    assert "Hub" in hidden_devices.text
    assert "Thread Border Router" in products.text
    assert "Wi-Fi Router / Access Point" in products.text


def test_export_import_keeps_hidden_infrastructure(engine):
    with Session(engine) as session:
        assert project_infrastructure(session, _topology()) == 2
        session.commit()
        exported = json.loads(json.dumps(build_export(session, app_version="test")))
    restored_engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(restored_engine)
    with Session(restored_engine) as session:
        result = apply_import(session, exported)
        assert not result.errors
        assert len(session.exec(select(Device)).all()) == 2
        assert len(session.exec(select(DeviceLink)).all()) == 2
        assert {d.status for d in session.exec(select(Device)).all()} == {DeviceStatus.hidden}


def test_import_rejects_conflicting_topology_link(engine):
    with Session(engine) as session:
        assert project_infrastructure(session, _topology()) == 2
        session.commit()
        exported = build_export(session, app_version="test")
        link = next(
            row
            for row in exported["device_links"]
            if row["integration"] == "topology_border_router"
        )
        link["device_id"] = "another-device"
        plan = plan_import(session, exported)
        assert any("linked to multiple Devices" in error for error in plan.errors)
