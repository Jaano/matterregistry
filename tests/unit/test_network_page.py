"""Web tests for the read-only /network page (stored topology + live mDNS inventory)."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine

from app.app import create_app
from app.database import get_session
from app.models import (
    Device,
    DeviceFabricMembership,
    Fabric,
    MatterTopologySnapshot,
    Product,
)

TOPOLOGY = {
    "collected_at": "2026-11-08T10:00:00Z",
    "nodes": [
        {
            "id": "18",
            "kind": "matter",
            "node_id": 18,
            "role": "router",
            "network_name": "MyHome",
            "ext_address": "4E51F6C23046C35E",
        },
        {"id": "br1", "kind": "border_router", "host_name": "Kitchen", "network_name": "MyHome"},
    ],
    "connections": [
        {
            "source": "18",
            "target": "br1",
            "network": "thread",
            "strength": "strong",
            "source_to_target": {"lqi": 3, "rssi": -54},
        },
        {"source": "18", "target": "gone", "strength": "weak"},
    ],
}


@pytest.fixture(autouse=True)
def _tallinn_tz(monkeypatch):
    monkeypatch.setenv("TZ", "Europe/Tallinn")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    os.environ.setdefault("SUPERVISOR_TOKEN", "")
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    SQLModel.metadata.create_all(engine)

    def _override() -> Iterator[Session]:
        with Session(engine) as session:
            yield session

    app = create_app()
    app.dependency_overrides[get_session] = _override
    app.state.test_engine = engine
    yield TestClient(app)


def _seed_topology(client: TestClient) -> None:
    engine = client.app.state.test_engine  # type: ignore[union-attr]
    with Session(engine) as s:
        product = Product(name="Lamp")
        s.add(product)
        s.flush()
        device = Device(name="Hall lamp", product_record_id=product.id)
        fabric = Fabric(fabric_id="1" * 16, controller="HA")
        s.add_all([device, fabric])
        s.flush()
        s.add(DeviceFabricMembership(device_id=device.id, fabric_id=fabric.id, node_id=18))
        s.add(MatterTopologySnapshot(collected_at="x", payload_json=json.dumps(TOPOLOGY)))
        s.commit()


def test_empty_state_without_topology_or_mdns(client: TestClient):
    r = client.get("/network")
    assert r.status_code == 200
    assert "No topology yet" in r.text
    assert "None seen" in r.text
    assert "No Matter device is advertising" in r.text


def test_nav_links_to_network_page(client: TestClient):
    assert 'href="/network"' in client.get("/network").text


def test_topology_rows_use_registry_device_names_and_link_to_them(client: TestClient):
    _seed_topology(client)
    r = client.get("/network")
    assert r.status_code == 200
    assert "Hall lamp" in r.text
    assert "/devices/" in r.text
    assert "Mesh Extender (Router)" in r.text
    assert "Kitchen" in r.text
    assert "LQI 3, RSSI -54 dBm" in r.text
    assert "2026-11-08 12:00" in r.text


@pytest.mark.parametrize(
    ("raw", "shown"),
    [
        (1794174796802, "2026-11-08 23:53"),
        ("1794174796802", "2026-11-08 23:53"),
        (1794174796, "2026-11-08 23:53"),
        ("2026-11-08T10:00:00+02:00", "2026-11-08 10:00"),
        ("garbage", "garbage"),
    ],
)
def test_collected_at_is_human_readable(client: TestClient, raw, shown):
    engine = client.app.state.test_engine  # type: ignore[union-attr]
    with Session(engine) as s:
        payload = {**TOPOLOGY, "collected_at": raw}
        s.add(MatterTopologySnapshot(payload_json=json.dumps(payload)))
        s.commit()
    r = client.get("/network")
    assert f"Collected: {shown}" in r.text.replace("</span>", "")


def test_links_to_unknown_nodes_are_skipped(client: TestClient):
    _seed_topology(client)
    assert "gone" not in client.get("/network").text


def test_live_mdns_inventory_is_listed(client: TestClient):
    client.app.state.mdns_client = SimpleNamespace(  # type: ignore[union-attr]
        border_routers=lambda: [
            {
                "instance": "Living Room Hub",
                "vendor": "Acme",
                "model": "Hub 2",
                "network_name": "MyHome",
                "thread_version": "1.4.0",
                "host_name": "hub.local",
                "addresses": [],
            }
        ],
        commissionable_devices=lambda: [
            {
                "instance": "A1B2",
                "device_name": "Desk lamp",
                "vendor_id": 65521,
                "product_id": 32769,
                "discriminator": 3840,
                "commissioning_mode": 2,
            }
        ],
    )
    text = client.get("/network").text
    assert "Living Room Hub" in text
    assert "Acme Hub 2" in text
    assert "1.4.0" in text
    assert "Desk lamp" in text
    assert "0xFFF1 / 0x8001" in text
    assert "enhanced" in text
    assert "None seen" not in text
