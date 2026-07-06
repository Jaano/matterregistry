"""Unit tests for the shared MAC/IPv4/IPv6 correlation helper.

Covers the pure normalizers and the AddressIndex over all three persisted
address sources: Device.mac_address, MatterNodeRecord (joined via
DeviceFabricMembership), and DeviceIntegrationData payloads.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.integrations.correlate import AddressIndex, normalize_ip, normalize_mac
from app.models import (
    Device,
    DeviceFabricMembership,
    DeviceIntegrationData,
    DeviceProtocol,
    Fabric,
    MatterNodeRecord,
)

# ── normalizers ───────────────────────────────────────────────────────────────


def test_normalize_mac_forms():
    want = "8E:DB:88:A9:5D:5D:F1:B4"
    assert normalize_mac("8edb88a95d5df1b4") == want
    assert normalize_mac("8E:DB:88:A9:5D:5D:F1:B4") == want
    assert normalize_mac("8e-db-88-a9-5d-5d-f1-b4") == want


def test_normalize_mac_rejects_non_hex():
    assert normalize_mac("not-a-mac") is None
    assert normalize_mac("8edb8") is None  # odd length
    assert normalize_mac("") is None


def test_normalize_ip_canonicalizes():
    assert normalize_ip("FD00:0:0:0::1") == "fd00::1"
    assert normalize_ip("fe80::1%eth0") == "fe80::1"  # zone id stripped
    assert normalize_ip("192.168.1.5") == "192.168.1.5"
    assert normalize_ip("garbage") is None


# ── AddressIndex ──────────────────────────────────────────────────────────────


@pytest.fixture
def engine():
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(eng)
    return eng


def _device(s, **over):
    d = Device(name="Test", protocol=DeviceProtocol.matter)
    for k, v in over.items():
        setattr(d, k, v)
    s.add(d)
    s.flush()
    return d


def test_match_by_device_mac(engine):
    with Session(engine) as s:
        dev = _device(s, mac_address="AA:BB:CC:DD:EE:FF")
        dev_id = dev.id
        s.commit()
    with Session(engine) as s:
        idx = AddressIndex(s)
        # Different separator/case still matches.
        assert idx.match(macs=["aabbccddeeff"]) == dev_id
        assert idx.match(macs=["00:11:22:33:44:55"]) is None


def test_match_by_matter_node_ipv6(engine):
    with Session(engine) as s:
        dev = _device(s, mac_address=None)
        fab = Fabric(fabric_id="0" * 16, controller="HA Matter")
        s.add(fab)
        s.flush()
        s.add(DeviceFabricMembership(device_id=dev.id, fabric_id=fab.id, node_id=42))
        s.add(
            MatterNodeRecord(
                node_id=42,
                ip_addresses_json=json.dumps(["fd11:2222:3333::5", "fe80::abcd"]),
            )
        )
        dev_id = dev.id
        s.commit()
    with Session(engine) as s:
        idx = AddressIndex(s)
        assert idx.match(ipv6s=["FD11:2222:3333:0:0:0:0:5"]) == dev_id
        assert idx.match(ipv6s=["fd99::9"]) is None


def test_match_by_integration_data_ipv4(engine):
    with Session(engine) as s:
        dev = _device(s)
        s.add(
            DeviceIntegrationData(
                device_id=dev.id,
                integration="mdns",
                payload_json=json.dumps({"ipv4_addresses": ["192.168.1.50"], "ipv6_addresses": []}),
            )
        )
        dev_id = dev.id
        s.commit()
    with Session(engine) as s:
        idx = AddressIndex(s)
        assert idx.match(ipv4s=["192.168.1.50"]) == dev_id


def test_ambiguous_address_yields_no_match(engine):
    with Session(engine) as s:
        d1 = _device(s, mac_address="AA:AA:AA:AA:AA:AA")
        d2 = _device(s, mac_address="aaaaaaaaaaaa")  # same MAC, different device
        s.commit()
        assert d1.id != d2.id
    with Session(engine) as s:
        idx = AddressIndex(s)
        assert idx.match(macs=["AA:AA:AA:AA:AA:AA"]) is None


def test_is_ambiguous_distinguishes_from_no_match(engine):
    with Session(engine) as s:
        _device(s, mac_address="BB:BB:BB:BB:BB:BB")
        _device(s, mac_address="bbbbbbbbbbbb")  # same MAC normalized, second device
        _device(s, mac_address="CC:CC:CC:CC:CC:CC")  # unique MAC
        s.commit()
    with Session(engine) as s:
        idx = AddressIndex(s)
        # Shared MAC → is_ambiguous=True
        assert idx.is_ambiguous(macs=["BB:BB:BB:BB:BB:BB"]) is True
        # Unknown MAC → is_ambiguous=False (never seen)
        assert idx.is_ambiguous(macs=["00:00:00:00:00:00"]) is False
        # Unique MAC → match returns it, is_ambiguous=False
        assert idx.match(macs=["CC:CC:CC:CC:CC:CC"]) is not None
        assert idx.is_ambiguous(macs=["CC:CC:CC:CC:CC:CC"]) is False


def test_mac_takes_priority_over_ip(engine):
    with Session(engine) as s:
        mac_dev = _device(s, mac_address="11:22:33:44:55:66")
        ip_dev = _device(s)
        s.add(
            DeviceIntegrationData(
                device_id=ip_dev.id,
                integration="mdns",
                payload_json=json.dumps({"ipv6_addresses": ["fd00::1"]}),
            )
        )
        mac_id = mac_dev.id
        s.commit()
    with Session(engine) as s:
        idx = AddressIndex(s)
        # Both a MAC hit and an IPv6 hit are offered; MAC wins.
        assert idx.match(macs=["112233445566"], ipv6s=["fd00::1"]) == mac_id
