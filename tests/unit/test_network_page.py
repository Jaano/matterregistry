"""Web tests for the read-only /network page (stored topology)."""

from __future__ import annotations

import json
import os
import re
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
        device = Device(name="Hall lamp", room="Hall", product_record_id=product.id)
        fabric = Fabric(fabric_id="1" * 16, controller="HA")
        s.add_all([device, fabric])
        s.flush()
        s.add(DeviceFabricMembership(device_id=device.id, fabric_id=fabric.id, node_id=18))
        s.add(MatterTopologySnapshot(collected_at="x", payload_json=json.dumps(TOPOLOGY)))
        s.commit()


EXT_TOPOLOGY = {
    "collected_at": "2026-11-08T10:00:00Z",
    "nodes": [
        {
            "id": "unknown_BA82D38D09F446A4",
            "kind": "thread_unknown",
            "role": "router",
            "network_name": "MyHome",
            "ext_address": "BA82D38D09F446A4",
        },
        {
            "id": "unknown_826489F37BDA5203",
            "kind": "thread_unknown",
            "role": "router",
            "network_name": "MyHome",
            "ext_address": "826489F37BDA5203",
        },
        {"id": "br1", "kind": "border_router", "host_name": "Kitchen", "network_name": "MyHome"},
    ],
    "connections": [
        {
            "source": "unknown_BA82D38D09F446A4",
            "target": "br1",
            "network": "thread",
            "strength": "strong",
            "source_to_target": {"lqi": 3, "rssi": -60},
        },
        {
            "source": "unknown_826489F37BDA5203",
            "target": "br1",
            "network": "thread",
            "strength": "weak",
        },
    ],
}


def _seed_ext_topology(client: TestClient, *, stored_ext: str | None) -> str:
    """Seed a topology of two unidentified Thread routers; return the HomeKit Device id."""
    from app.integrations import data as integration_data

    with Session(client.app.state.test_engine) as s:  # type: ignore[union-attr]
        device = Device(name="Nanoleaf A19 2XGA", room="Aed")
        s.add(device)
        s.flush()
        if stored_ext:
            integration_data.merge(
                s,
                device_id=device.id,
                integration="otbr",
                payload={"thread_ext_address": stored_ext},
            )
        s.add(MatterTopologySnapshot(collected_at="x", payload_json=json.dumps(EXT_TOPOLOGY)))
        s.commit()
        return device.id


def test_unidentified_thread_router_resolves_through_stored_ext_address(client: TestClient):
    device_id = _seed_ext_topology(client, stored_ext="BA:82:D3:8D:09:F4:46:A4")
    mesh = client.get("/network").text
    assert f'href="/devices/{device_id}"' in mesh
    assert 'aria-label="Nanoleaf A19 2XGA · Mesh Extender (Router) · MyHome"' in mesh
    assert 'aria-label="Aed"' in mesh
    assert "Thread device BA82D38D09F446A4" not in mesh
    # The other router has no stored address and stays an unidentified Thread device.
    assert "Thread device 826489F37BDA5203" in mesh

    table = client.get("/network?view=table").text
    assert (
        f'<a class="mr-network-device-link" href="/devices/{device_id}">Nanoleaf A19 2XGA</a>'
        in table
    )
    assert "Thread device 826489F37BDA5203" in table


def test_network_table_links_matched_target_device(client: TestClient):
    device_id = _seed_ext_topology(client, stored_ext="BA:82:D3:8D:09:F4:46:A4")
    with Session(client.app.state.test_engine) as session:  # type: ignore[union-attr]
        snapshot = session.get(MatterTopologySnapshot, "matter_server")
        assert snapshot is not None
        payload = json.loads(snapshot.payload_json)
        payload["connections"] = [
            {"source": "br1", "target": "unknown_BA82D38D09F446A4", "network": "thread"}
        ]
        snapshot.payload_json = json.dumps(payload)
        session.add(snapshot)
        session.commit()

    table = client.get("/network?view=table").text
    assert re.search(
        rf'<td>Kitchen</td>\s*<td><a class="mr-network-device-link" '
        rf'href="/devices/{device_id}">Nanoleaf A19 2XGA</a></td>',
        table,
    )


def test_thread_routers_stay_unidentified_without_a_stored_ext_address(client: TestClient):
    _seed_ext_topology(client, stored_ext=None)
    mesh = client.get("/network").text
    assert "Thread device BA82D38D09F446A4" in mesh
    assert "Thread device 826489F37BDA5203" in mesh
    assert "Nanoleaf A19 2XGA" not in mesh
    assert 'aria-label="Unassigned"' in mesh


def test_empty_state_without_topology(client: TestClient):
    r = client.get("/network")
    assert r.status_code == 200
    assert "No topology yet" in r.text
    assert "Thread Border Routers (mDNS)" not in r.text
    assert "Open for commissioning (mDNS)" not in r.text
    assert "Discoverable, not ready (mDNS)" not in r.text


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
    assert "2026-11-08 12:00" in r.text
    assert 'class="mr-room" aria-label="Hall"' in r.text
    assert 'data-source="18" data-target="br1"' in r.text
    assert 'data-network="thread"' in r.text
    assert 'data-lqi="3"' in r.text
    assert 'data-role="router"' in r.text
    assert 'data-role="border_router"' in r.text
    assert 'aria-label="Hall lamp · Mesh Extender (Router) · MyHome"' in r.text
    assert 'aria-label="Unassigned"' in r.text
    assert 'src="/static/network_graph.js' in r.text


def test_network_table_shows_saved_links_and_switches_back_to_mesh(client: TestClient):
    _seed_topology(client)
    response = client.get("/network?view=table")
    assert response.status_code == 200
    assert 'href="/network?view=table" aria-current="true"' in response.text
    assert 'href="/network?view=mesh"' in response.text
    assert '<table class="mr-device-table mr-network-table">' in response.text
    assert "<th>Link Quality</th><th>RSSI</th>" in response.text
    assert re.search(
        r'<a class="mr-network-device-link" href="/devices/[^"]+">Hall lamp</a></td>\s*'
        r'<td>Kitchen</td>\s*<td><span class="icon-thread mr-network-type-icon" '
        r'role="img" aria-label="Thread link" title="Thread link"></span></td>\s*'
        r"<td>3</td><td>-54 dBm</td>",
        response.text,
    )
    assert "gone" not in response.text
    assert 'id="mr-network-graph"' not in response.text
    assert 'src="/static/network_graph.js' not in response.text

    mesh = client.get("/network?view=mesh")
    assert 'href="/network?view=mesh" aria-current="true"' in mesh.text
    assert 'id="mr-network-graph"' in mesh.text
    assert '<table class="mr-device-table mr-network-table">' not in mesh.text
    assert "<span>Topology</span>" not in mesh.text


def test_duplicate_network_names_are_numbered_by_mac_in_mesh_and_table(client: TestClient):
    nodes = [
        {"id": "ap_b", "kind": "wifi_ap", "ssid": "Wifi", "bssid": "AA:00:00:00:00:02"},
        {"id": "station_b", "kind": "matter", "node_id": 2, "role": "station"},
        {"id": "ap_a", "kind": "wifi_ap", "ssid": "Wifi", "bssid": "AA:00:00:00:00:01"},
        {"id": "station_a", "kind": "matter", "node_id": 1, "role": "station"},
        {"id": "ap_guest", "kind": "wifi_ap", "ssid": "Guest", "bssid": "AA:00:00:00:00:03"},
    ]
    connections = [
        {"source": "ap_b", "target": "station_b", "network": "wifi"},
        {"source": "ap_a", "target": "station_a", "network": "wifi"},
    ]
    with Session(client.app.state.test_engine) as session:  # type: ignore[union-attr]
        session.add(
            MatterTopologySnapshot(
                payload_json=json.dumps({"nodes": nodes, "connections": connections})
            )
        )
        session.commit()

    for ordered_nodes in (nodes, list(reversed(nodes))):
        with Session(client.app.state.test_engine) as session:  # type: ignore[union-attr]
            snapshot = session.get(MatterTopologySnapshot, "matter_server")
            assert snapshot is not None
            snapshot.payload_json = json.dumps({"nodes": ordered_nodes, "connections": connections})
            session.add(snapshot)
            session.commit()

        mesh = client.get("/network?view=mesh").text
        assert re.search(
            r'data-node-id="ap_a"[^>]+aria-label="Wifi \(1\) · Wi-Fi access point', mesh
        )
        assert re.search(
            r'data-node-id="ap_b"[^>]+aria-label="Wifi \(2\) · Wi-Fi access point', mesh
        )
        assert re.search(
            r'data-node-id="ap_guest"[^>]+aria-label="Guest · Wi-Fi access point', mesh
        )
        table = client.get("/network?view=table").text
        assert "<td>Wifi (1)</td>" in table
        assert "<td>Wifi (2)</td>" in table


def test_legend_shows_link_colors_and_one_lqi_scale(client: TestClient):
    _seed_topology(client)
    text = client.get("/network").text
    assert '<span data-network="thread">Thread link</span>' in text
    assert '<span data-network="wifi">Wi-Fi link</span>' in text
    assert '<span class="mr-legend-heading">Link quality:</span>' in text
    for lqi in range(4):
        assert f'<span data-lqi="{lqi}">{lqi}</span>' in text


def test_mesh_uses_highest_directional_lqi_and_preserves_zero(client: TestClient):
    _seed_topology(client)
    with Session(client.app.state.test_engine) as session:  # type: ignore[union-attr]
        snapshot = session.get(MatterTopologySnapshot, "matter_server")
        assert snapshot is not None
        payload = json.loads(snapshot.payload_json)
        payload["nodes"].extend(
            {"id": node_id, "kind": "thread_unknown"} for node_id in ("weak", "dead", "unmeasured")
        )
        payload["connections"] = [
            {
                "source": "18",
                "target": "weak",
                "network": "thread",
                "source_to_target": {"lqi": 1},
                "target_to_source": {"lqi": 2, "rssi": -48},
            },
            {
                "source": "18",
                "target": "dead",
                "network": "thread",
                "source_to_target": {"lqi": 0},
            },
            {"source": "18", "target": "unmeasured", "network": "thread"},
        ]
        snapshot.payload_json = json.dumps(payload)
        session.add(snapshot)
        session.commit()

    text = client.get("/network?view=mesh").text
    assert 'data-source="18" data-target="weak" data-network="thread" data-lqi="2"' in text
    assert 'data-source="18" data-target="dead" data-network="thread" data-lqi="0"' in text
    assert (
        'data-source="18" data-target="unmeasured" data-network="thread" data-lqi="unknown"' in text
    )
    table = client.get("/network?view=table").text
    assert "<td>2</td><td>-48 dBm</td>" in table
    assert "<td>0</td><td>-</td>" in table
    assert "<td>-</td><td>-</td>" in table


def test_wifi_lqi_uses_rssi_thresholds_and_leaves_missing_signal_unknown(client: TestClient):
    levels = [(-67, 3), (-68, 2), (-75, 2), (-76, 1), (-80, 1), (-81, 0)]
    connections = [
        {
            "source": "ap",
            "target": f"station_{index}",
            "network": "wifi",
            "source_to_target": {"rssi": rssi, "lqi": 3},
        }
        for index, (rssi, _) in enumerate(levels)
    ]
    connections.extend(
        [
            {
                "source": "ap",
                "target": "directional",
                "network": "wifi",
                "source_to_target": {"rssi": -90},
                "target_to_source": {"rssi": -67},
            },
            {"source": "ap", "target": "missing", "network": "wifi"},
            {
                "source": "ap",
                "target": "invalid",
                "network": "wifi",
                "source_to_target": {"rssi": True, "lqi": 3},
            },
        ]
    )
    nodes = [{"id": "ap", "kind": "wifi_ap", "ssid": "Home"}] + [
        {"id": connection["target"], "kind": "matter", "role": "station"}
        for connection in connections
    ]
    with Session(client.app.state.test_engine) as session:  # type: ignore[union-attr]
        session.add(
            MatterTopologySnapshot(
                payload_json=json.dumps({"nodes": nodes, "connections": connections})
            )
        )
        session.commit()

    mesh = client.get("/network?view=mesh").text
    for index, (_, level) in enumerate(levels):
        assert (
            f'data-source="ap" data-target="station_{index}" data-network="wifi" data-lqi="{level}"'
        ) in mesh
    assert 'data-source="ap" data-target="directional" data-network="wifi" data-lqi="3"' in mesh
    for target in ("missing", "invalid"):
        assert (
            f'data-source="ap" data-target="{target}" data-network="wifi" data-lqi="unknown"'
        ) in mesh

    table = client.get("/network?view=table").text
    assert (
        '<td><span class="icon-wifi mr-network-type-icon" role="img" '
        'aria-label="Wi-Fi link" title="Wi-Fi link"></span></td>' in table
    )
    assert "<td>3</td><td>-67 dBm</td>" in table
    assert "<td>0</td><td>-81 dBm</td>" in table
    assert "<td>-</td><td>-</td>" in table


def test_stylesheet_colours_links_and_icons_by_network_type():
    root = Path(__file__).resolve().parents[2] / "app" / "web" / "static"
    css = (root / "ui.css").read_text()
    script = (root / "network_graph.js").read_text()
    assert "path.style.stroke = network" in script
    assert '"var(--mr-net-" + network + ", var(--mr-primary))"' in script
    for name, color in {
        "wifi": "#64748b",
        "bluetooth": "#0082fc",
        "thread": "#f37035",
        "zigbee": "#e6b800",
        "ethernet": "#10b981",
    }.items():
        assert f"--mr-net-{name}: {color};" in css
    icons = (root / "icons.css").read_text()
    for name, color in {
        "wifi": "%2364748b",
        "bluetooth": "%230082fc",
        "thread": "%23f37035",
        "ethernet": "%2310b981",
    }.items():
        block = icons[icons.index(f".icon-{name} {{") :].split("\n}\n", 1)[0]
        assert color in block and "%23374151" not in block


def test_network_table_has_empty_link_state(client: TestClient):
    with Session(client.app.state.test_engine) as session:  # type: ignore[union-attr]
        session.add(MatterTopologySnapshot(payload_json=json.dumps({"nodes": TOPOLOGY["nodes"]})))
        session.commit()
    response = client.get("/network?view=table")
    assert response.status_code == 200
    assert "No links in the saved topology." in response.text
    assert '<table class="mr-device-table mr-network-table">' not in response.text


def test_honeycomb_icons_cover_topology_roles(client: TestClient):
    nodes = [
        {"id": "br", "kind": "border_router", "host_name": "Hub"},
        {"id": "ap", "kind": "wifi_ap", "ssid": "Home"},
        {"id": "router", "kind": "matter", "node_id": 1, "role": "reed"},
        {"id": "end", "kind": "matter", "node_id": 2, "role": "end_device"},
        {"id": "sleepy", "kind": "matter", "node_id": 3, "role": "sleepy_end_device"},
        {"id": "station", "kind": "matter", "node_id": 4, "role": "station"},
        {"id": "other", "kind": "matter", "node_id": 5},
    ]
    with Session(client.app.state.test_engine) as session:  # type: ignore[union-attr]
        session.add(MatterTopologySnapshot(payload_json=json.dumps({"nodes": nodes})))
        session.commit()
    text = client.get("/network").text
    for role in (
        "border_router",
        "wifi_ap",
        "router",
        "end_device",
        "sleepy_end_device",
        "station",
        "unknown",
    ):
        assert f'data-role="{role}"' in text
    assert text.count('class="mr-node"') == len(nodes)
    symbols = set(re.findall(r'<symbol id="([^"]+)"', text))
    icon_targets = re.findall(
        r'<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><use href="#([^"]+)"',
        text,
    )
    assert len(icon_targets) == len(nodes)
    assert set(icon_targets) <= symbols


def test_honeycomb_renders_forty_devices_across_eight_rooms(client: TestClient):
    rooms = [
        "Bedroom",
        "Hall",
        "Kitchen",
        "Living room",
        "Office",
        "Bathroom",
        "Garage",
        "Workshop",
    ]
    roles = ["router", "end_device", "sleepy_end_device", "station"]
    engine = client.app.state.test_engine  # type: ignore[union-attr]
    with Session(engine) as session:
        product = Product(name="Example")
        fabric = Fabric(fabric_id="2" * 16, controller="HA")
        session.add_all([product, fabric])
        session.flush()
        nodes = []
        for index in range(40):
            device = Device(
                name=f"Device {index + 1}", room=rooms[index // 5], product_record_id=product.id
            )
            session.add(device)
            session.flush()
            session.add(
                DeviceFabricMembership(device_id=device.id, fabric_id=fabric.id, node_id=index + 1)
            )
            nodes.append(
                {
                    "id": str(index + 1),
                    "kind": "matter",
                    "node_id": index + 1,
                    "role": roles[index % 4],
                }
            )
        connections = [
            {"source": str(i), "target": str(i + 1), "network": "thread"} for i in range(1, 40)
        ]
        connections.extend(
            {"source": "1", "target": str(i), "network": "thread"} for i in range(6, 41, 5)
        )
        session.add(
            MatterTopologySnapshot(
                payload_json=json.dumps({"nodes": nodes, "connections": connections})
            )
        )
        session.commit()
    text = client.get("/network").text
    assert text.count('class="mr-room"') == 8
    assert text.count('class="mr-node"') == 40
    thread_link = (
        r'<li data-source="[^"]*" data-target="[^"]*" data-network="thread" data-lqi="unknown">'
    )
    assert len(re.findall(thread_link, text)) == 46
    room_order = re.findall(r'<section class="mr-room" aria-label="([^"]+)"', text)
    assert set(room_order) == set(rooms)
    assert "40" in text


def test_room_placement_centers_room_with_most_inter_room_links(client: TestClient):
    room_by_node = {1: "Many", 2: "Many", 3: "Many", 4: "Connected", 5: "Other"}
    with Session(client.app.state.test_engine) as session:  # type: ignore[union-attr]
        product = Product(name="Example")
        fabric = Fabric(fabric_id="4" * 16, controller="HA")
        session.add_all([product, fabric])
        session.flush()
        for node_id, room in room_by_node.items():
            device = Device(name=f"Device {node_id}", room=room, product_record_id=product.id)
            session.add(device)
            session.flush()
            session.add(
                DeviceFabricMembership(device_id=device.id, fabric_id=fabric.id, node_id=node_id)
            )
        session.add(
            MatterTopologySnapshot(
                payload_json=json.dumps(
                    {
                        "nodes": [
                            {"id": str(node_id), "kind": "matter", "node_id": node_id}
                            for node_id in room_by_node
                        ],
                        "connections": [
                            {"source": "1", "target": "4"},
                            {"source": "4", "target": "2"},
                            {"source": "4", "target": "5"},
                            {"source": "1", "target": "2"},
                            {"source": "2", "target": "3"},
                        ],
                    }
                )
            )
        )
        session.commit()

    response = client.get("/network")
    room_order = re.findall(r'<section class="mr-room" aria-label="([^"]+)"', response.text)
    assert room_order == ["Connected", "Many", "Other"]


def test_room_placement_ranks_remaining_rooms_by_device_average_rssi(
    client: TestClient,
):
    room_by_node = {
        1: "Hub",
        2: "Hub",
        3: "Hub",
        4: "Uneven",
        5: "Uneven",
        6: "Steady",
        7: "Weak",
        8: "Unmeasured",
    }
    connections = [
        {
            "source": "4",
            "target": str(peer),
            "source_to_target": {"rssi": -30},
            "target_to_source": {"rssi": -20},
        }
        for peer in (1, 2, 3)
    ]
    connections.extend(
        [
            {"source": "5", "target": "1", "source_to_target": {"rssi": -90}},
            {"source": "6", "target": "2", "source_to_target": {"rssi": -55}},
            {"source": "3", "target": "7", "target_to_source": {"rssi": -80}},
            {"source": "8", "target": "1", "source_to_target": {"rssi": True}},
            {"source": "8", "target": "missing", "source_to_target": {"rssi": -1}},
        ]
    )
    engine = client.app.state.test_engine  # type: ignore[union-attr]
    with Session(engine) as session:
        product = Product(name="Example")
        fabric = Fabric(fabric_id="3" * 16, controller="HA")
        session.add_all([product, fabric])
        session.flush()
        for node_id, room in room_by_node.items():
            device = Device(name=f"Device {node_id}", room=room, product_record_id=product.id)
            session.add(device)
            session.flush()
            session.add(
                DeviceFabricMembership(device_id=device.id, fabric_id=fabric.id, node_id=node_id)
            )
        session.add(
            MatterTopologySnapshot(
                payload_json=json.dumps(
                    {
                        "nodes": [
                            {"id": str(node_id), "kind": "matter", "node_id": node_id}
                            for node_id in room_by_node
                        ],
                        "connections": connections,
                    }
                )
            )
        )
        session.commit()

    response = client.get("/network")
    assert response.status_code == 200
    room_order = re.findall(r'<section class="mr-room" aria-label="([^"]+)"', response.text)
    assert room_order == ["Hub", "Steady", "Uneven", "Weak", "Unmeasured"]


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
@pytest.mark.parametrize("view", ["mesh", "table"])
def test_collected_at_is_human_readable(client: TestClient, raw, shown, view):
    engine = client.app.state.test_engine  # type: ignore[union-attr]
    with Session(engine) as s:
        payload = {**TOPOLOGY, "collected_at": raw}
        s.add(MatterTopologySnapshot(payload_json=json.dumps(payload)))
        s.commit()
    r = client.get(f"/network?view={view}")
    assert re.search(
        rf'<div class="mr-network-footer"><small>Collected: {re.escape(shown)}</small></div>',
        r.text,
    )
    assert "mr-network-heading" not in r.text


def test_links_to_unknown_nodes_are_skipped(client: TestClient):
    _seed_topology(client)
    assert "gone" not in client.get("/network").text


def test_network_page_does_not_read_or_show_mdns_inventory(client: TestClient):
    _seed_topology(client)

    def unexpected_mdns_read():
        raise AssertionError("Network page should not read mDNS inventory")

    client.app.state.mdns_client = SimpleNamespace(  # type: ignore[union-attr]
        border_routers=unexpected_mdns_read,
        commissionable_devices=unexpected_mdns_read,
    )
    response = client.get("/network")
    assert response.status_code == 200
    assert "Hall lamp" in response.text
    assert "Thread Border Routers (mDNS)" not in response.text
    assert "Open for commissioning (mDNS)" not in response.text
    assert "Discoverable, not ready (mDNS)" not in response.text
