"""Matter network topology (WS schema 13): graph helpers, snapshot storage, device payload."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import app.database
from app.integrations.data import read as read_integration_data
from app.integrations.matter_server.server_client import (
    MatterServerClient,
    NodeInfo,
    _apply_nodes,
    _mesh_payload,
    load_topology,
)
from app.integrations.matter_server.topology import (
    find_matter_node,
    link_text,
    links_of,
    node_label,
    role_label,
)
from app.models import Device, DeviceProtocol, MatterTopologySnapshot

TOPOLOGY = {
    "collected_at": "2026-11-08T10:00:00Z",
    "nodes": [
        {
            "id": "18",
            "kind": "matter",
            "node_id": 18,
            "network_type": "thread",
            "role": "sleepy_end_device",
            "ext_pan_id": "5059B0077AED49A3",
            "network_name": "MyHome",
        },
        {"id": "4", "kind": "matter", "node_id": 4, "network_type": "thread", "role": "router"},
        {
            "id": "br1",
            "kind": "border_router",
            "network_type": "thread",
            "host_name": "Kitchen",
            "vendor_name": "Acme",
            "model_name": "Hub",
        },
        {"id": "x1", "kind": "thread_unknown", "network_type": "thread", "ext_address": "AABB"},
        {"id": "ap1", "kind": "wifi_ap", "network_type": "wifi", "ssid": "Home", "bssid": "11:22"},
        {"id": "7", "kind": "matter", "node_id": 7, "network_type": "wifi", "role": "station"},
    ],
    "connections": [
        {
            "source": "18",
            "target": "4",
            "strength": "strong",
            "source_to_target": {"strength": "strong", "lqi": 3, "rssi": -54},
            "target_to_source": {"strength": "strong", "lqi": 2, "rssi": -51},
        },
        {
            "source": "br1",
            "target": "18",
            "strength": "weak",
            "source_to_target": {"lqi": 1, "rssi": -80},
            "target_to_source": {"lqi": 1, "rssi": -77},
        },
        {"source": "18", "target": "x1", "strength": "medium"},
        {"source": "7", "target": "ap1", "strength": "unknown"},
    ],
}


def _names(node_id):
    return {4: "Hallway router"}.get(node_id)


# ── pure helpers ──────────────────────────────────────────────────────────────


def test_role_labels_add_thread_14_names():
    assert role_label("router") == "Mesh Extender (Router)"
    assert role_label("reed") == "Standby Mesh Extender (REED)"
    assert role_label("sleepy_end_device") == "Sleepy End Device"
    assert role_label("station") == "Wi-Fi station"
    assert role_label(None) is None


def test_node_labels_per_kind():
    nodes = {n["id"]: n for n in TOPOLOGY["nodes"]}
    assert node_label(nodes["4"], _names) == "Hallway router"
    assert node_label(nodes["18"], _names) == "Node 18"
    assert node_label(nodes["br1"]) == "Kitchen"
    assert node_label({"kind": "border_router", "vendor_name": "Acme", "model_name": "Hub"}) == (
        "Acme Hub"
    )
    assert node_label({"kind": "border_router"}) == "Border Router"
    assert node_label(nodes["x1"]) == "Thread device AABB"
    assert node_label(nodes["ap1"]) == "Home"


def test_links_are_seen_from_the_node_and_sorted_strongest_first():
    node = find_matter_node(TOPOLOGY, 18)
    assert node is not None
    links = links_of(TOPOLOGY, node, _names)
    assert [lk["peer"] for lk in links] == ["Hallway router", "Thread device AABB", "Kitchen"]
    router, unknown, border = links
    assert (router["lqi"], router["rssi"]) == (3, -54)  # source_to_target: 18 is the source
    assert (border["lqi"], border["rssi"]) == (1, -77)  # target_to_source: 18 is the target
    assert unknown["lqi"] is None


def test_link_text_formats_numbers_when_present():
    node = find_matter_node(TOPOLOGY, 18)
    assert node is not None
    texts = [link_text(lk) for lk in links_of(TOPOLOGY, node, _names)]
    assert texts[0] == "Hallway router: strong, LQI 3, RSSI -54 dBm"
    assert texts[1] == "Thread device AABB: medium"


def test_find_matter_node_misses_other_kinds_and_unknown_ids():
    assert find_matter_node(TOPOLOGY, 999) is None
    assert find_matter_node({}, 18) is None


def test_mesh_payload_for_thread_and_wifi_nodes():
    thread = _mesh_payload(TOPOLOGY, 18, _names)
    assert thread["thread_network"] == "MyHome"
    assert thread["thread_ext_pan_id"] == "5059B0077AED49A3"
    assert thread["network_role"] == "Sleepy End Device"
    assert thread["network_peers"][0].startswith("Hallway router: strong")
    wifi = _mesh_payload(TOPOLOGY, 7, _names)
    assert "thread_network" not in wifi
    assert wifi["network_peers"] == ["Home: unknown"]


@pytest.mark.parametrize("topology", [None, {}, {"nodes": [], "connections": []}])
def test_mesh_payload_empty_without_topology(topology):
    assert _mesh_payload(topology, 18, _names) == {}


# ── storage and projection ────────────────────────────────────────────────────


@pytest.fixture
def engine(monkeypatch):
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(eng)
    monkeypatch.setattr(app.database, "engine", eng)
    return eng


@dataclass
class _Topo:
    collected_at: str = "2026-11-08T10:00:00Z"
    nodes: list = field(default_factory=lambda: TOPOLOGY["nodes"])
    connections: list = field(default_factory=lambda: TOPOLOGY["connections"])


class _FakeMc:
    def __init__(self, result=None, error=None):
        self.calls = 0
        self._result, self._error = result, error

    async def get_network_topology(self):
        self.calls += 1
        if self._error:
            raise self._error
        return self._result


def _connected(mc, schema):
    client = MatterServerClient("ws://localhost:5580/ws")
    client._mc = mc
    client._schema_version = schema
    return client


async def test_refresh_stores_snapshot(engine):
    mc = _FakeMc(_Topo())
    await _connected(mc, 14)._refresh_topology()
    assert mc.calls == 1
    with Session(engine) as s:
        row = s.exec(select(MatterTopologySnapshot)).one()
        assert row.collected_at == "2026-11-08T10:00:00Z"
        stored = load_topology(s)
    assert stored is not None
    assert len(stored["nodes"]) == 6


async def test_refresh_replaces_previous_snapshot(engine):
    await _connected(_FakeMc(_Topo()), 14)._refresh_topology()
    await _connected(
        _FakeMc(_Topo(nodes=[TOPOLOGY["nodes"][0]], connections=[])), 14
    )._refresh_topology()
    with Session(engine) as s:
        assert len(s.exec(select(MatterTopologySnapshot)).all()) == 1
        stored = load_topology(s)
    assert stored is not None
    assert len(stored["nodes"]) == 1


@pytest.mark.parametrize("schema", [None, 11, 12])
async def test_refresh_skipped_below_schema_13(engine, schema):
    mc = _FakeMc(_Topo())
    await _connected(mc, schema)._refresh_topology()
    assert mc.calls == 0
    with Session(engine) as s:
        assert load_topology(s) is None


async def test_refresh_skipped_when_client_has_no_typed_method(engine):
    client = _connected(SimpleNamespace(), 14)
    await client._refresh_topology()
    with Session(engine) as s:
        assert load_topology(s) is None


async def test_refresh_failure_is_swallowed_and_keeps_old_snapshot(engine):
    await _connected(_FakeMc(_Topo()), 14)._refresh_topology()
    await _connected(_FakeMc(error=RuntimeError("boom")), 14)._refresh_topology()
    with Session(engine) as s:
        assert load_topology(s) is not None


def _node(node_id: int) -> NodeInfo:
    return NodeInfo(
        node_id=node_id,
        available=True,
        vendor_id=0xFFF1,
        vendor_name="Acme",
        product_id=0x8001,
        product_name="Sensor",
        serial=f"S{node_id}",
        hardware_version_string="1.0",
        firmware_version_string="1.0",
        node_label=f"Sensor {node_id}",
        unique_id=f"uid-{node_id}",
        manufacturing_date=None,
        product_url=None,
        part_number=None,
    )


def _integration():
    return SimpleNamespace(
        fabric_label=None,
        supported_protocols=frozenset({DeviceProtocol.matter}),
        assert_capabilities=lambda *a, **k: None,
    )


def test_projection_adds_mesh_fields_to_integration_data(engine):
    with Session(engine) as s:
        s.add(
            MatterTopologySnapshot(
                collected_at=TOPOLOGY["collected_at"],
                payload_json=__import__("json").dumps(TOPOLOGY),
            )
        )
        s.commit()
    with Session(engine) as s:
        _apply_nodes(s, [_node(18), _node(4)], integration=_integration())
        device = s.exec(select(Device).where(Device.matter_unique_id == "uid-18")).one()
        payload = read_integration_data(s, device_id=device.id, integration="matter_server")
    assert payload is not None
    assert payload["thread_network"] == "MyHome"
    # Peer node 4 is named after the Registry device that represents it.
    assert payload["network_peers"][0].startswith("Sensor 4: strong")


def test_projection_without_topology_has_no_mesh_fields(engine):
    with Session(engine) as s:
        _apply_nodes(s, [_node(18)], integration=_integration())
        device = s.exec(select(Device)).one()
        payload = read_integration_data(s, device_id=device.id, integration="matter_server")
    assert payload is not None
    assert "network_peers" not in payload
