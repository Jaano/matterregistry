"""Unit tests for mDNS HomeKit/LTPDU/Matter discovery (D.3 + D.5 + D.6).

Covers the pure TXT parsers, the verified setup-hash correlation, and the
auto-create + correlate projections (deterministic keys only). The zeroconf
browser glue is exercised manually, not here.
"""

from __future__ import annotations

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import app.database
from app.homekit import setup_hash
from app.integrations.mdns.client import (
    MdnsClient,
    parse_hap_service,
    parse_matter_service,
    project_discovered,
    project_matter,
)
from app.models import (
    Device,
    DeviceIntegrationData,
    DeviceLink,
    DeviceProtocol,
    Property,
    PropertyType,
)

# ── TXT parsing (real captured vectors) ──────────────────────────────────────


def test_parse_hap_tcp_wifi():
    props = {
        b"id": b"1D:C3:F4:EF:74:68",
        b"md": b"PS-S02E",
        b"ci": b"10",
        b"sf": b"0",
        b"sh": b"8D51HA==",
        b"pv": b"1.1",
    }
    acc = parse_hap_service("Presence-Sensor-FP2-7DD0._hap._tcp.local.", "_hap._tcp.local.", props)
    assert acc == {
        "id": "1D:C3:F4:EF:74:68",
        "name": "Presence-Sensor-FP2-7DD0",
        "model": "PS-S02E",
        "category_id": 10,
        "paired": True,
        "setup_hash": "8D51HA==",
        "transport": "wifi",
    }


def test_parse_hap_udp_thread_and_unpaired():
    props = {b"id": b"af:96:99:21:95:9e", b"md": b"NL45", b"ci": b"5", b"sf": b"1"}
    acc = parse_hap_service("Nanoleaf A19 2XGA 39997._hap._udp.local.", "_hap._udp.local.", props)
    assert acc["id"] == "AF:96:99:21:95:9E"  # uppercased
    assert acc["transport"] == "thread"
    assert acc["category_id"] == 5
    assert acc["paired"] is False  # sf bit0 set => unpaired


def test_parse_hap_no_id_returns_none():
    assert parse_hap_service("X._hap._tcp.local.", "_hap._tcp.local.", {b"md": b"X"}) is None


# ── Setup hash (verified against real devices) ───────────────────────────────


def test_setup_hash_matches_real_devices():
    assert setup_hash("2XGA", "AF:96:99:21:95:9E") == "wrmPEQ=="
    assert setup_hash("94QH", "13:33:90:CC:5A:95") == "dwmBVQ=="
    assert setup_hash("6HW4", "35:27:C0:90:BC:5B") == "cakyLA=="
    # device id is upper-cased internally
    assert setup_hash("2XGA", "af:96:99:21:95:9e") == "wrmPEQ=="


# ── Projection ───────────────────────────────────────────────────────────────


@pytest.fixture
def engine(monkeypatch):
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(eng)
    monkeypatch.setattr(app.database, "engine", eng)
    return eng


@pytest.fixture
def client():
    return MdnsClient()


def _acc(acc_id, **over):
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


def _devices(engine):
    with Session(engine) as s:
        return list(s.exec(select(Device)).all())


def test_project_creates_homekit_device(engine, client):
    with Session(engine) as s:
        client_result = project_discovered(s, [_acc("1D:C3:F4:EF:74:68")], integration=client)
    assert client_result["created"] == 1
    devs = _devices(engine)
    assert len(devs) == 1
    d = devs[0]
    assert d.protocol == DeviceProtocol.homekit
    assert d.homekit_accessory_id == "1D:C3:F4:EF:74:68"
    assert d.product == "PS-S02E"
    assert d.network_type == ["wifi"]
    with Session(engine) as s:
        link = s.exec(select(DeviceLink).where(DeviceLink.integration == "mdns")).first()
        assert link and link.external_id == "1D:C3:F4:EF:74:68" and link.device_id == d.id


def test_project_is_idempotent(engine, client):
    with Session(engine) as s:
        project_discovered(s, [_acc("1D:C3:F4:EF:74:68")], integration=client)
    with Session(engine) as s:
        project_discovered(s, [_acc("1D:C3:F4:EF:74:68")], integration=client)
    assert len(_devices(engine)) == 1
    with Session(engine) as s:
        assert len(list(s.exec(select(DeviceLink)).all())) == 1


def test_project_dedupes_on_homekit_accessory_id(engine, client):
    """A device already carrying the accessory id (e.g. from the HA import) is
    correlated, not duplicated."""
    with Session(engine) as s:
        s.add(
            Device(
                name="From HA",
                protocol=DeviceProtocol.homekit,
                homekit_accessory_id="1D:C3:F4:EF:74:68",
            )
        )
        s.commit()
    with Session(engine) as s:
        res = project_discovered(s, [_acc("1D:C3:F4:EF:74:68")], integration=client)
    assert res["created"] == 0
    assert len(_devices(engine)) == 1  # no duplicate


def test_project_correlates_via_setup_hash(engine, client):
    """A scanned device (stored Setup ID) links to the discovered accessory
    whose setup hash it produces - no new device."""
    with Session(engine) as s:
        dev = Device(name="Scanned Nanoleaf", protocol=DeviceProtocol.homekit)
        s.add(dev)
        s.commit()
        s.refresh(dev)
        s.add(Property(device_id=dev.id, type=PropertyType.discriminator, value="2XGA"))
        s.commit()
        dev_id = dev.id

    acc = _acc("AF:96:99:21:95:9E", model="NL45", category_id=5, setup_hash="wrmPEQ==")
    with Session(engine) as s:
        res = project_discovered(s, [acc], integration=client)
    assert res["created"] == 0
    assert len(_devices(engine)) == 1
    with Session(engine) as s:
        link = s.exec(select(DeviceLink).where(DeviceLink.integration == "mdns")).first()
        assert link and link.device_id == dev_id  # linked to the scanned device
        dev = s.get(Device, dev_id)
        assert dev.homekit_accessory_id == "AF:96:99:21:95:9E"  # identity backfilled


def test_project_skips_without_can_create(engine):
    class _NoCreate(MdnsClient):
        can_create_devices = False

    with Session(engine) as s:
        res = project_discovered(s, [_acc("1D:C3:F4:EF:74:68")], integration=_NoCreate())
    assert res["created"] == 0
    assert _devices(engine) == []


# ── parse_matter_service ──────────────────────────────────────────────────────


def test_parse_matter_service_valid():
    """Real captured vector from sniff_hap_thread.json."""
    props = {b"SII": b"800", b"SAI": b"800", b"SAT": b"4000", b"T": b"0"}
    rec = parse_matter_service(
        "D990EA668A3939E7-000000000000003D._matter._tcp.local.",
        props,
        "8EDB88A95D5DF1B4.local",
        ["fd3e:1cca:93f4:0:1a30:a795:7094:5d36", "fe80::1"],
    )
    assert rec is not None
    assert rec["compressed_fabric_hex"] == "D990EA668A3939E7"
    assert rec["node_id"] == 0x3D  # 61
    assert rec["session_idle_ms"] == 800
    assert rec["session_active_ms"] == 800
    assert rec["session_active_threshold"] == 4000
    assert "fd3e:1cca:93f4:0:1a30:a795:7094:5d36" in rec["ipv6_addresses"]
    assert rec["ipv4_addresses"] == []


def test_parse_matter_service_no_txt():
    """Empty TXT (some devices omit session params)."""
    rec = parse_matter_service(
        "D990EA668A3939E7-000000000000004A._matter._tcp.local.",
        {},
        None,
        ["192.168.0.76"],
    )
    assert rec is not None
    assert rec["node_id"] == 0x4A
    assert rec["session_idle_ms"] is None
    assert rec["ipv4_addresses"] == ["192.168.0.76"]


def test_parse_matter_service_invalid_label():
    """Non-matter instance names return None."""
    assert parse_matter_service("Nanoleaf-A19._matter._tcp.local.", {}, None, []) is None
    assert parse_matter_service("._matter._tcp.local.", {}, None, []) is None


# ── project_matter ────────────────────────────────────────────────────────────


def _matter_rec(fabric: str, node_id: int, **over) -> dict:
    d = {
        "compressed_fabric_hex": fabric.upper(),
        "node_id": node_id,
        "instance_name": f"{fabric.upper()}-{node_id:016X}",
        "ipv4_addresses": ["192.168.1.10"],
        "ipv6_addresses": ["fd3e::1"],
        "session_idle_ms": 800,
        "session_active_ms": 800,
        "session_active_threshold": 4000,
        "port": 5540,
    }
    d.update(over)
    return d


def test_project_matter_enriches_matched_device(engine, client):
    """A device with a matching matter_unique_id gets integration data."""
    import json

    fabric = "D990EA668A3939E7"
    node_id = 0x3D
    uid = f"deviceid_{fabric}-{node_id:016X}-MatterNodeDevice"

    with Session(engine) as s:
        dev = Device(name="NL67", protocol=DeviceProtocol.matter, matter_unique_id=uid)
        s.add(dev)
        s.commit()
        dev_id = dev.id

    with Session(engine) as s:
        res = project_matter(s, [_matter_rec(fabric, node_id)], integration=client)

    assert res["updated"] == 1
    assert res["skipped"] == 0

    with Session(engine) as s:
        row = s.exec(
            select(DeviceIntegrationData).where(DeviceIntegrationData.device_id == dev_id)
        ).first()
        assert row is not None
        payload = json.loads(row.payload_json)
        assert payload["matter_node_id"] == node_id
        assert payload["matter_instance_name"] == f"{fabric}-{node_id:016X}"
        assert payload["matter_operational_port"] == 5540
        assert payload["matter_session_idle_ms"] == 800
        assert "fd3e::1" in payload["ipv6_addresses"]


def test_project_matter_skips_unmatched(engine, client):
    """Records with no matching device are skipped; no device is created."""
    with Session(engine) as s:
        res = project_matter(s, [_matter_rec("AAAAAAAAAAAAAAAA", 99)], integration=client)
    assert res["updated"] == 0
    assert res["skipped"] == 1
    assert _devices(engine) == []


def test_project_matter_merges_ipv6_with_existing(engine, client):
    """Existing ipv6_addresses from HAP/LTPDU are merged, not overwritten."""
    import json

    fabric = "D990EA668A3939E7"
    node_id = 1

    with Session(engine) as s:
        dev = Device(
            name="Test",
            protocol=DeviceProtocol.matter,
            matter_unique_id=f"deviceid_{fabric}-{node_id:016X}-MatterNodeDevice",
        )
        s.add(dev)
        s.flush()
        s.add(
            DeviceIntegrationData(
                device_id=dev.id,
                integration="mdns",
                payload_json=json.dumps({"ipv6_addresses": ["fe80::1"]}),
            )
        )
        s.commit()
        dev_id = dev.id

    with Session(engine) as s:
        project_matter(
            s, [_matter_rec(fabric, node_id, ipv6_addresses=["fd00::2"])], integration=client
        )

    with Session(engine) as s:
        row = s.exec(
            select(DeviceIntegrationData).where(DeviceIntegrationData.device_id == dev_id)
        ).first()
        assert row is not None
        payload = json.loads(row.payload_json)
        assert set(payload["ipv6_addresses"]) == {"fe80::1", "fd00::2"}
