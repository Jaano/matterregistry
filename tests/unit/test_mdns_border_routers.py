"""Passive Thread Border Router and commissioning-mode mDNS inventory."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
import zeroconf.asyncio
from zeroconf import ServiceStateChange

from app.integrations.mdns import client as mdns
from app.integrations.mdns.client import (
    MdnsClient,
    parse_matterc_service,
    parse_meshcop_service,
)

MESHCOP_NAME = "Living Room Hub._meshcop._udp.local."
MATTERC_NAME = "A1B2C3D4E5F60718._matterc._udp.local."


def _props(**kv) -> dict:
    return {k.encode(): v if isinstance(v, bytes) else v.encode() for k, v in kv.items()}


# ── _meshcop._udp ─────────────────────────────────────────────────────────────


def test_meshcop_record_is_parsed_with_binary_fields_as_hex():
    props = _props(
        vn="Acme",
        mn="Hub 2",
        nn="MyHome",
        tv="1.4.0",
        xp=bytes.fromhex("5059b0077aed49a3"),
        id=bytes.fromhex("00112233445566778899aabbccddeeff"),
    )
    rec = parse_meshcop_service(MESHCOP_NAME, props, "hub.local.", ["fd00::1", "10.0.0.5"], 49154)
    assert rec == {
        "instance": "Living Room Hub",
        "vendor": "Acme",
        "model": "Hub 2",
        "network_name": "MyHome",
        "thread_version": "1.4.0",
        "ext_pan_id": "5059B0077AED49A3",
        "border_agent_id": "00112233445566778899AABBCCDDEEFF",
        "host_name": "hub.local",
        "addresses": ["fd00::1", "10.0.0.5"],
        "port": 49154,
    }


def test_meshcop_minimal_record_and_escaped_space_in_name():
    rec = parse_meshcop_service(
        "Home\\032Hub._meshcop._udp.local.", _props(nn="Net"), None, [], None
    )
    assert rec is not None
    assert rec["instance"] == "Home Hub"
    assert rec["vendor"] is None
    assert rec["host_name"] is None


def test_meshcop_without_network_identity_is_ignored():
    assert parse_meshcop_service(MESHCOP_NAME, _props(vn="Acme"), None, [], None) is None
    assert parse_meshcop_service(MESHCOP_NAME, {}, None, [], None) is None


def test_meshcop_instance_name_may_contain_dots():
    rec = parse_meshcop_service("Hub v1.2._meshcop._udp.local.", _props(nn="Net"), None, [], None)
    assert rec is not None
    assert rec["instance"] == "Hub v1.2"


# ── _matterc._udp ─────────────────────────────────────────────────────────────


def test_matterc_record_with_vendor_product_and_mode():
    props = _props(D="3840", VP="65521+32769", CM="1", DT="257", DN="Desk lamp")
    rec = parse_matterc_service(MATTERC_NAME, props, "lamp.local.", ["10.0.0.9"], 5540)
    assert rec == {
        "instance": "A1B2C3D4E5F60718",
        "discriminator": 3840,
        "vendor_id": 65521,
        "product_id": 32769,
        "commissioning_mode": 1,
        "device_type": 257,
        "device_name": "Desk lamp",
        "host_name": "lamp.local",
        "addresses": ["10.0.0.9"],
        "port": 5540,
    }


def test_matterc_vendor_only_vp_and_missing_optional_keys():
    rec = parse_matterc_service(MATTERC_NAME, _props(D="12", VP="4874"), None, [], None)
    assert rec is not None
    assert (rec["vendor_id"], rec["product_id"]) == (4874, None)
    assert rec["commissioning_mode"] is None


@pytest.mark.parametrize("props", [{}, _props(VP="1+2"), _props(D="abc")])
def test_matterc_without_valid_discriminator_is_ignored(props):
    assert parse_matterc_service(MATTERC_NAME, props, None, [], None) is None


# ── client handlers ───────────────────────────────────────────────────────────


class _FakeInfo:
    def __init__(self, service_type, name):
        self.properties = _props(nn="MyHome", xp=bytes.fromhex("5059b0077aed49a3"))
        self.server = "hub.local."
        self.port = 49154

    async def async_request(self, zc, timeout):
        return True

    def parsed_addresses(self):
        return ["fd00::1"]


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(zeroconf.asyncio, "AsyncServiceInfo", _FakeInfo)
    c = MdnsClient()
    c._azc = SimpleNamespace(zeroconf=object())
    return c


async def test_added_service_is_resolved_and_listed(client):
    client._on_meshcop_state_change(None, mdns._MESHCOP, MESHCOP_NAME, ServiceStateChange.Added)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    routers = client.border_routers()
    assert [r["network_name"] for r in routers] == ["MyHome"]
    assert routers[0]["ext_pan_id"] == "5059B0077AED49A3"
    assert client.commissionable_devices() == []


async def test_removed_service_disappears(client):
    client._meshcop_discovered[MESHCOP_NAME] = {"network_name": "MyHome", "instance": "x"}
    client._on_meshcop_state_change(None, mdns._MESHCOP, MESHCOP_NAME, ServiceStateChange.Removed)
    assert client.border_routers() == []


def test_lists_are_sorted_by_name():
    c = MdnsClient()
    c._meshcop_discovered = {
        "b": {"network_name": "Zed", "instance": "b"},
        "a": {"network_name": "Alpha", "instance": "a"},
    }
    c._matterc_discovered = {"2": {"instance": "B"}, "1": {"instance": "A"}}
    assert [r["network_name"] for r in c.border_routers()] == ["Alpha", "Zed"]
    assert [r["instance"] for r in c.commissionable_devices()] == ["A", "B"]


async def test_unparsable_record_is_not_stored(client, monkeypatch):
    class _Empty(_FakeInfo):
        def __init__(self, service_type, name):
            super().__init__(service_type, name)
            self.properties = {}

    monkeypatch.setattr(zeroconf.asyncio, "AsyncServiceInfo", _Empty)
    client._on_meshcop_state_change(None, mdns._MESHCOP, MESHCOP_NAME, ServiceStateChange.Added)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert client.border_routers() == []


def test_browsing_types_include_new_services():
    assert mdns._MESHCOP in mdns._ALL_TYPES
    assert mdns._MATTERC in mdns._ALL_TYPES
