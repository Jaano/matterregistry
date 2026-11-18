"""Unit tests for resolving Thread nodes to registry Devices.

Covers the pieces that let the Network page name a Thread router that is not a
Matter node:

  * ``merge`` keeps unrelated keys in a device's integration-data payload.
  * ``AddressIndex`` harvests ``thread_ext_address``; ``device_ipv6_addresses``
    lists one Device's stored IPv6 addresses.
  * ``router_rlocs_from_route`` turns a ``route`` TLV into router RLOC16s.
  * The OTBR ``locate_thread_node`` device action unicasts one router at a time
    (never the broadcast), stops at the match, and persists the extended address.
"""

from __future__ import annotations

import json

import httpx
import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.integrations import data as integration_data
from app.integrations.correlate import AddressIndex, device_ipv6_addresses
from app.integrations.otbr import client as otbr_client
from app.integrations.otbr.client import OTBRClient, router_rlocs_from_route
from app.models import Device, DeviceFabricMembership, Fabric, MatterNodeRecord

BASE = "http://otbr.test"
EXT = "BA:82:D3:8D:09:F4:46:A4"
OMR = "fd3e:1cca:93f4:0:24e5:3231:3b2b:1dbf"


@pytest.fixture
def engine():
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(eng)
    return eng


def _device(session: Session, name: str, ipv6: list[str], **kw) -> Device:
    dev = Device(name=name, network_type=["thread"], **kw)
    session.add(dev)
    session.flush()
    integration_data.upsert(
        session, device_id=dev.id, integration="mdns", payload={"ipv6_addresses": ipv6}
    )
    session.commit()
    return dev


# ── merge ─────────────────────────────────────────────────────────────────────


def test_merge_keeps_keys_the_caller_did_not_write(engine):
    with Session(engine) as s:
        dev = _device(s, "Lamp", [OMR])
        integration_data.upsert(
            s, device_id=dev.id, integration="otbr", payload={"thread_ext_address": EXT, "x": 1}
        )
        integration_data.merge(
            s, device_id=dev.id, integration="otbr", payload={"x": 2, "node_id": 7}
        )
        s.commit()
        assert integration_data.read(s, device_id=dev.id, integration="otbr") == {
            "thread_ext_address": EXT,
            "x": 2,
            "node_id": 7,
        }


def test_merge_creates_the_row_when_absent(engine):
    with Session(engine) as s:
        dev = _device(s, "Lamp", [OMR])
        integration_data.merge(
            s, device_id=dev.id, integration="otbr", payload={"thread_ext_address": EXT}
        )
        s.commit()
        assert integration_data.read(s, device_id=dev.id, integration="otbr") == {
            "thread_ext_address": EXT
        }


# ── AddressIndex / device_ipv6_addresses ──────────────────────────────────────


def test_address_index_resolves_a_stored_thread_ext_address(engine):
    with Session(engine) as s:
        dev = _device(s, "Lamp", [OMR])
        integration_data.merge(
            s, device_id=dev.id, integration="otbr", payload={"thread_ext_address": EXT}
        )
        s.commit()
        index = AddressIndex(s)
        assert index.match(macs=["ba82d38d09f446a4"]) == dev.id
        assert index.match(macs=["0a82b11339b7b38a"]) is None


def test_address_index_ignores_ext_address_shared_by_two_devices(engine):
    with Session(engine) as s:
        a = _device(s, "A", ["fd00::1"])
        b = _device(s, "B", ["fd00::2"])
        for dev in (a, b):
            integration_data.merge(
                s, device_id=dev.id, integration="otbr", payload={"thread_ext_address": EXT}
            )
        s.commit()
        assert AddressIndex(s).match(macs=[EXT]) is None


def test_device_ipv6_addresses_reads_integration_data_and_matter_node(engine):
    with Session(engine) as s:
        dev = _device(s, "Plug", ["FD3E:1CCA:93F4:0:0:0:0:5", "192.168.0.4"])
        fabric = Fabric(fabric_id="1" * 16, controller="HA")
        s.add(fabric)
        s.flush()
        s.add(DeviceFabricMembership(device_id=dev.id, fabric_id=fabric.id, node_id=9))
        s.add(
            MatterNodeRecord(
                node_id=9, ip_addresses_json=json.dumps(["fd46:5a23:f008:e644::9", "10.0.0.9"])
            )
        )
        s.commit()
        assert device_ipv6_addresses(s, dev.id) == [
            "fd3e:1cca:93f4::5",
            "fd46:5a23:f008:e644::9",
        ]


def test_device_ipv6_addresses_empty_when_nothing_stored(engine):
    with Session(engine) as s:
        dev = Device(name="Bare")
        s.add(dev)
        s.commit()
        assert device_ipv6_addresses(s, dev.id) == []


# ── router_rlocs_from_route ───────────────────────────────────────────────────


def test_router_rlocs_from_route_maps_ids_to_rloc16_and_excludes_self():
    entry = {
        "route": {"routeData": [{"routeId": 0}, {"routeId": 6}, {"routeId": 36}, {"routeId": 6}]}
    }
    assert router_rlocs_from_route(entry) == [0x0000, 0x1800, 0x9000]
    assert router_rlocs_from_route(entry, exclude=0x1800) == [0x0000, 0x9000]


@pytest.mark.parametrize(
    "entry",
    [{}, {"route": None}, {"route": {"routeData": "x"}}, {"route": {"routeData": [None, {}]}}],
)
def test_router_rlocs_from_route_tolerates_missing_or_malformed(entry):
    assert router_rlocs_from_route(entry) == []


def test_router_rlocs_from_route_rejects_out_of_range_ids():
    entry = {"route": {"routeData": [{"routeId": 64}, {"routeId": -1}, {"routeId": True}]}}
    assert router_rlocs_from_route(entry) == []


# ── locate_thread_node ────────────────────────────────────────────────────────


class _Resp:
    def __init__(self, payload: object, status: int = 200) -> None:
        self._payload = payload
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("err", request=None, response=None)  # type: ignore[arg-type]

    def json(self) -> object:
        return self._payload


class _FakeMesh:
    """Fake OTBR: ``mesh`` maps an RLOC16 to its diagnostic reply (None = unreachable)."""

    def __init__(self, mesh: dict[int, dict | None]) -> None:
        self.mesh = mesh
        self.destinations: list[int] = []
        self._last: int | None = None

    def client(self, *args: object, **kwargs: object) -> _FakeMesh:
        return self

    async def __aenter__(self) -> _FakeMesh:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def get(self, url: str, headers: dict | None = None) -> _Resp:
        if url.endswith("/node"):
            return _Resp({"state": "router", "extAddress": "cc", "rloc16": "0x1800"})
        if "/api/actions/" in url:
            reachable = self.mesh.get(self._last) is not None
            return _Resp({"status": "completed" if reachable else "failed"})
        if url.endswith("/api/diagnostics"):
            reply = self.mesh.get(self._last)
            return _Resp([reply] if reply else [])
        return _Resp(None, 404)

    async def post(
        self, url: str, content: bytes | None = None, headers: dict | None = None
    ) -> _Resp:
        attrs = json.loads(content or b"{}")["data"][0]["attributes"]
        self._last = int(attrs["destination"], 16)
        self.destinations.append(self._last)
        return _Resp({"data": [{"id": "task"}]})


def _reply(rloc: int, ext: str, *addrs: str) -> dict:
    return {"extAddress": ext, "rloc16": f"0x{rloc:04x}", "ipv6Addresses": list(addrs)}


def _self_reply(*router_ids: int) -> dict:
    return {
        "extAddress": "cc",
        "rloc16": "0x1800",
        "route": {"routeData": [{"routeId": r} for r in router_ids]},
    }


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setattr(otbr_client, "_POLL_INTERVAL", 0)
    monkeypatch.setattr(otbr_client, "_UNICAST_GAP", 0)


def _use(monkeypatch, mesh: _FakeMesh) -> None:
    monkeypatch.setattr(otbr_client.httpx, "AsyncClient", mesh.client)


async def test_locate_unicasts_until_match_and_persists_ext_address(engine, monkeypatch, fast):
    with Session(engine) as s:
        target = _device(s, "Nanoleaf 2XGA", [OMR])
        other = _device(s, "Nanoleaf 10R9", ["fd3e:1cca:93f4:0:3084:774c:4b8a:127d"])
        mesh = _FakeMesh(
            {
                0x1800: _self_reply(0, 6, 36, 40),
                0x0000: _reply(0x0000, "826489f37bda5203", "fd3e:1cca:93f4:0:3084:774c:4b8a:127d"),
                0x9000: _reply(0x9000, "ba82d38d09f446a4", "fe80::b882:d38d:9f4:46a4", OMR),
                0xA000: _reply(0xA000, "0000000000000001", "fd00::99"),
            }
        )
        _use(monkeypatch, mesh)

        result = await OTBRClient(BASE).locate_thread_node(target, s)
        s.commit()

        assert "BA:82:D3:8D:09:F4:46:A4" in result.message
        assert "Nanoleaf 10R9" in result.message
        assert integration_data.read(s, device_id=target.id, integration="otbr") == {
            "thread_ext_address": EXT
        }
        assert integration_data.read(s, device_id=other.id, integration="otbr") == {
            "thread_ext_address": "82:64:89:F3:7B:DA:52:03"
        }
        # Own route table first, then routers in id order, stopping at the match
        # (0xA000 is never queried) and never the 0xfffe broadcast.
        assert mesh.destinations == [0x1800, 0x0000, 0x9000]


async def test_locate_never_writes_for_a_device_that_no_longer_exists(engine, monkeypatch, fast):
    """A stale row of a deleted device must not be reported or written to."""
    with Session(engine) as s:
        target = _device(s, "Nanoleaf 6HW4", [OMR])
        integration_data.upsert(
            s,
            device_id="deleted-device-id",
            integration="mdns",
            payload={"ipv6_addresses": ["fd3e:1cca:93f4:0:3084:774c:4b8a:127d"]},
        )
        s.commit()
        mesh = _FakeMesh(
            {
                0x1800: _self_reply(0, 36),
                0x0000: _reply(0x0000, "826489f37bda5203", "fd3e:1cca:93f4:0:3084:774c:4b8a:127d"),
                0x9000: _reply(0x9000, "ba82d38d09f446a4", OMR),
            }
        )
        _use(monkeypatch, mesh)

        result = await OTBRClient(BASE).locate_thread_node(target, s)
        s.commit()

        assert "Also identified" not in result.message
        assert integration_data.read(s, device_id="deleted-device-id", integration="otbr") is None


async def test_locate_reports_not_found_and_writes_nothing(engine, monkeypatch, fast):
    with Session(engine) as s:
        target = _device(s, "Nanoleaf", [OMR])
        mesh = _FakeMesh(
            {
                0x1800: _self_reply(0, 36),
                0x0000: _reply(0x0000, "aa", "fd00::1"),
                0x9000: None,
            }
        )
        _use(monkeypatch, mesh)

        result = await OTBRClient(BASE).locate_thread_node(target, s)
        s.commit()

        assert result.message.startswith("Not found among 1 answering routers (1 unreachable)")
        assert integration_data.read(s, device_id=target.id, integration="otbr") is None


async def test_locate_raises_when_no_router_answers(engine, monkeypatch, fast):
    with Session(engine) as s:
        target = _device(s, "Nanoleaf", [OMR])
        _use(monkeypatch, _FakeMesh({0x1800: _self_reply(0, 36), 0x0000: None, 0x9000: None}))

        with pytest.raises(RuntimeError, match="No router answered"):
            await OTBRClient(BASE).locate_thread_node(target, s)


async def test_locate_raises_when_router_list_unavailable(engine, monkeypatch, fast):
    with Session(engine) as s:
        target = _device(s, "Nanoleaf", [OMR])
        _use(monkeypatch, _FakeMesh({0x1800: None}))

        with pytest.raises(RuntimeError, match="router list"):
            await OTBRClient(BASE).locate_thread_node(target, s)


async def test_locate_makes_no_request_without_a_stored_address(engine, monkeypatch, fast):
    with Session(engine) as s:
        bare = Device(name="Bare", network_type=["thread"])
        s.add(bare)
        s.commit()
        mesh = _FakeMesh({})
        _use(monkeypatch, mesh)

        result = await OTBRClient(BASE).locate_thread_node(bare, s)

        assert "No IPv6 address" in result.message
        assert mesh.destinations == []


async def test_locate_keeps_other_integration_data_keys(engine, monkeypatch, fast):
    with Session(engine) as s:
        target = _device(s, "Nanoleaf", [OMR])
        integration_data.upsert(
            s, device_id=target.id, integration="otbr", payload={"thread_role": "router"}
        )
        s.commit()
        _use(
            monkeypatch,
            _FakeMesh({0x1800: _self_reply(0), 0x0000: _reply(0x0000, "ba82d38d09f446a4", OMR)}),
        )

        await OTBRClient(BASE).locate_thread_node(target, s)
        s.commit()

        assert integration_data.read(s, device_id=target.id, integration="otbr") == {
            "thread_role": "router",
            "thread_ext_address": EXT,
        }


# ── action applicability ──────────────────────────────────────────────────────


def test_locate_action_needs_thread_and_a_stored_ipv6(engine):
    action = next(a for a in OTBRClient(BASE).device_actions() if a.key == "locate_thread_node")
    with Session(engine) as s:
        with_ip = _device(s, "Lamp", [OMR])
        no_ip = Device(name="Bare", network_type=["thread"])
        wifi = _device(s, "Wifi", [OMR])
        wifi.network_type = ["wifi"]
        s.add_all([no_ip, wifi])
        s.commit()

        assert action.applicable(with_ip, s) is True
        assert action.applicable(no_ip, s) is False
        assert action.applicable(wifi, s) is False
