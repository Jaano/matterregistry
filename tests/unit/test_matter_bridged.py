"""Bridged Matter endpoints projected as separate Devices linked to their bridge."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import app.database
from app.integrations.data import read as read_integration_data
from app.integrations.matter_server.server_client import (
    BRIDGE_LINK,
    NodeInfo,
    _apply_nodes,
    _extract_bridged,
    _node_info_from_record,
)
from app.models import Device, DeviceFabricMembership, DeviceLink, DeviceProtocol, Product

BRIDGED_ATTRS = {
    # endpoint 3: a fully described bridged lamp
    "3/57/1": "Acme",
    "3/57/2": 0xFFF1,
    "3/57/3": "Smart Bulb",
    "3/57/4": 0x8001,
    "3/57/5": "Kitchen bulb",
    "3/57/8": "hw 1",
    "3/57/10": "fw 2",
    "3/57/15": "SN-3",
    "3/57/17": True,
    "3/57/18": "uid-bulb-3",
    # endpoint 4: only a unique id and reachability
    "4/57/17": False,
    "4/57/18": "uid-bulb-4",
    # noise that must be ignored
    "0/57/18": "root-endpoint",
    "5/57/17": True,  # reachable alone does not identify a device
    "6/57/3": "   ",
    "7/40/1": "Other cluster",
}


def test_extracts_identifying_endpoints_only():
    found = _extract_bridged(BRIDGED_ATTRS)
    assert [b["endpoint"] for b in found] == [3, 4]
    lamp = found[0]
    assert lamp == {
        "endpoint": 3,
        "vendor_name": "Acme",
        "vendor_id": 0xFFF1,
        "product_name": "Smart Bulb",
        "product_id": 0x8001,
        "node_label": "Kitchen bulb",
        "hardware_version_string": "hw 1",
        "firmware_version_string": "fw 2",
        "serial": "SN-3",
        "reachable": True,
        "unique_id": "uid-bulb-3",
    }
    assert found[1] == {"endpoint": 4, "reachable": False, "unique_id": "uid-bulb-4"}


@pytest.mark.parametrize(
    "attrs",
    [{}, {"x/57/18": "u"}, {"3/57/18": None}, {"3/57/2": 0}, {"3/57/2": True}, {"3/57": "u"}],
)
def test_malformed_or_empty_attributes_give_nothing(attrs):
    assert _extract_bridged(attrs) == []


@pytest.fixture
def engine(monkeypatch):
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(eng)
    monkeypatch.setattr(app.database, "engine", eng)
    return eng


def _integration():
    return SimpleNamespace(
        fabric_label=None,
        supported_protocols=frozenset({DeviceProtocol.matter}),
        assert_capabilities=lambda *a, **k: None,
    )


def _bridge(**over) -> NodeInfo:
    base = dict(
        node_id=5,
        available=True,
        vendor_id=0xFFF1,
        vendor_name="Hubco",
        product_id=0x1,
        product_name="Bridge",
        serial="BR-1",
        hardware_version_string=None,
        firmware_version_string=None,
        node_label="Living room bridge",
        unique_id="uid-bridge",
        manufacturing_date=None,
        product_url=None,
        part_number=None,
        is_bridge=True,
        bridged=_extract_bridged(BRIDGED_ATTRS),
    )
    base.update(over)
    return NodeInfo(**base)


def test_bridged_endpoints_become_devices_linked_to_the_bridge(engine):
    with Session(engine) as s:
        result = _apply_nodes(s, [_bridge()], integration=_integration())
        assert result["create"] == 3  # the bridge + two bridged devices

        bridge = s.exec(select(Device).where(Device.matter_unique_id == "uid-bridge")).one()
        lamp = s.exec(select(Device).where(Device.matter_unique_id == "uid-bulb-3")).one()
        other = s.exec(select(Device).where(Device.matter_unique_id == "uid-bulb-4")).one()

        assert lamp.name == "Kitchen bulb"
        assert lamp.serial == "SN-3"
        assert lamp.hardware_version == "hw 1"
        assert lamp.firmware_version == "fw 2"
        assert lamp.product_record is not None
        assert (lamp.product_record.vendor_id, lamp.product_record.product_id) == (0xFFF1, 0x8001)
        assert lamp.protocol == DeviceProtocol.matter
        assert other.name == "Bridged device (endpoint 4)"

        links = s.exec(select(DeviceLink).where(DeviceLink.integration == BRIDGE_LINK)).all()
        assert {(link.device_id, link.external_id) for link in links} == {
            (lamp.id, bridge.id),
            (other.id, bridge.id),
        }

        payload = read_integration_data(s, device_id=lamp.id, integration="matter_server")
        assert payload is not None
        assert payload["bridge_node_id"] == 5
        assert payload["bridge_device"] == "Living room bridge"
        assert payload["bridge_endpoint"] == 3
        assert payload["reachable"] is True


def test_bridged_devices_have_no_fabric_membership(engine):
    with Session(engine) as s:
        _apply_nodes(s, [_bridge()], integration=_integration())
        memberships = s.exec(select(DeviceFabricMembership)).all()
        assert len(memberships) == 1  # only the bridge node itself


def test_second_sync_updates_instead_of_duplicating(engine):
    with Session(engine) as s:
        _apply_nodes(s, [_bridge()], integration=_integration())
    changed = _extract_bridged({**BRIDGED_ATTRS, "3/57/10": "fw 3"})
    with Session(engine) as s:
        result = _apply_nodes(s, [_bridge(bridged=changed)], integration=_integration())
        assert result["create"] == 0
        assert result["update"] >= 1
        assert len(s.exec(select(Device)).all()) == 3
        lamp = s.exec(select(Device).where(Device.matter_unique_id == "uid-bulb-3")).one()
        assert lamp.firmware_version == "fw 3"
        assert len(s.exec(select(DeviceLink)).all()) == 2


def test_user_renamed_bridged_device_keeps_its_name(engine):
    with Session(engine) as s:
        _apply_nodes(s, [_bridge()], integration=_integration())
        lamp = s.exec(select(Device).where(Device.matter_unique_id == "uid-bulb-3")).one()
        lamp.name = "My lamp"
        s.add(lamp)
        s.commit()
    with Session(engine) as s:
        _apply_nodes(s, [_bridge()], integration=_integration())
        lamp = s.exec(select(Device).where(Device.matter_unique_id == "uid-bulb-3")).one()
        assert lamp.name == "My lamp"


def test_endpoint_fallback_identity_without_unique_id(engine):
    attrs = {"2/57/3": "Plug", "2/57/17": True}
    with Session(engine) as s:
        _apply_nodes(s, [_bridge(bridged=_extract_bridged(attrs))], integration=_integration())
        plug = s.exec(select(Device).where(Device.matter_unique_id == "bridge:5:2")).one()
        assert plug.name == "Plug"
        assert s.exec(select(Product).where(Product.name == "Plug")).one().vendor_id is None


def test_node_without_bridged_endpoints_is_unaffected(engine):
    with Session(engine) as s:
        result = _apply_nodes(s, [_bridge(bridged=[])], integration=_integration())
        assert result["create"] == 1
        assert s.exec(select(DeviceLink)).all() == []


def test_staging_record_round_trips_bridged():
    bridged = [{"endpoint": 3, "unique_id": "u", "product_name": "Bulb"}]
    record = SimpleNamespace(
        node_id=5,
        available=True,
        vendor_id=None,
        vendor_name=None,
        product_id=None,
        product_name=None,
        serial=None,
        hardware_version_string=None,
        firmware_version_string=None,
        node_label=None,
        unique_id=None,
        manufacturing_date=None,
        product_url=None,
        part_number=None,
        date_commissioned=None,
        is_bridge=True,
        product_label=None,
        product_appearance_json=None,
        batteries_json=None,
        bridged_json=json.dumps(bridged),
        spec_version_int=None,
        hardware_version_int=None,
        software_version_int=None,
        network_type_json="[]",
        mac_address=None,
        ip_addresses_json="[]",
        endpoint_json="{}",
    )
    assert _node_info_from_record(record).bridged == bridged
    record.bridged_json = None
    assert _node_info_from_record(record).bridged == []
