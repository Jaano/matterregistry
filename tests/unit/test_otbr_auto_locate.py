"""Unit tests for OTBR auto-locate and its Settings checkbox.

Auto-locate runs after an OTBR poll/sync and identifies Thread routers the saved
topology cannot name. It must send nothing unless a router is unidentified AND a
registry Device could be it, honour the user's setting (default on), and never
fail the poll.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

import app.database
from app.app import create_app
from app.database import get_session
from app.integrations import data as integration_data
from app.integrations import settings_store
from app.integrations.otbr import client as otbr_client
from app.integrations.otbr.client import OTBRClient
from app.models import Device, IntegrationSetting, MatterTopologySnapshot

from .test_thread_locate import _FakeMesh, _reply, _self_reply

BASE = "http://otbr.test"
EXT_A = "ba82d38d09f446a4"
EXT_B = "826489f37bda5203"
IP_A = "fd3e:1cca:93f4:0:24e5:3231:3b2b:1dbf"
IP_B = "fd3e:1cca:93f4:0:3084:774c:4b8a:127d"


@pytest.fixture
def engine(monkeypatch):
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(eng)
    monkeypatch.setattr(app.database, "engine", eng)
    monkeypatch.setattr(otbr_client, "_POLL_INTERVAL", 0)
    monkeypatch.setattr(otbr_client, "_UNICAST_GAP", 0)
    return eng


def _topology(*exts: str) -> dict:
    return {
        "collected_at": "x",
        "nodes": [
            {"id": f"unknown_{e.upper()}", "kind": "thread_unknown", "ext_address": e.upper()}
            for e in exts
        ]
        + [{"id": "br", "kind": "border_router", "ext_address": "CA1BCC6DE177F673"}],
        "connections": [],
    }


def _seed(engine, *exts: str, devices: dict[str, str] | None = None) -> dict[str, str]:
    """Save a topology with unidentified routers; add Thread devices keyed by name -> ipv6."""
    ids: dict[str, str] = {}
    with Session(engine) as s:
        s.add(MatterTopologySnapshot(collected_at="x", payload_json=json.dumps(_topology(*exts))))
        for name, ip in (devices or {}).items():
            dev = Device(name=name, network_type=["thread"])
            s.add(dev)
            s.flush()
            integration_data.upsert(
                s, device_id=dev.id, integration="mdns", payload={"ipv6_addresses": [ip]}
            )
            ids[name] = dev.id
        s.commit()
    return ids


def _mesh(monkeypatch) -> _FakeMesh:
    fake = _FakeMesh(
        {
            0x1800: _self_reply(0, 6, 36),
            0x0000: _reply(0x0000, EXT_A, IP_A),
            0x9000: _reply(0x9000, EXT_B, IP_B),
        }
    )
    monkeypatch.setattr(otbr_client.httpx, "AsyncClient", fake.client)
    return fake


def _ext(engine, device_id: str) -> str | None:
    with Session(engine) as s:
        return (integration_data.read(s, device_id=device_id, integration="otbr") or {}).get(
            "thread_ext_address"
        )


# ── settings store ────────────────────────────────────────────────────────────


def test_auto_locate_is_on_by_default_and_round_trips(engine):
    with Session(engine) as s:
        assert settings_store.otbr_auto_locate_enabled(s) is True
        settings_store.set_bool(s, "otbr", "auto_locate", False)
        s.commit()
    with Session(engine) as s:
        assert settings_store.otbr_auto_locate_enabled(s) is False
        settings_store.set_bool(s, "otbr", "auto_locate", True)
        s.commit()
    with Session(engine) as s:
        assert settings_store.otbr_auto_locate_enabled(s) is True


def test_unreadable_stored_value_falls_back_to_the_default(engine):
    with Session(engine) as s:
        s.add(IntegrationSetting(integration="otbr", key="auto_locate", value_json="not json"))
        s.add(IntegrationSetting(integration="otbr", key="other", value_json='"yes"'))
        s.commit()
        assert settings_store.get_bool(s, "otbr", "auto_locate", default=True) is True
        assert settings_store.get_bool(s, "otbr", "other", default=False) is False


# ── auto_locate ───────────────────────────────────────────────────────────────


async def test_identifies_every_unresolved_router_in_one_walk(engine, monkeypatch):
    ids = _seed(engine, EXT_A, EXT_B, devices={"Bulb A": IP_A, "Bulb B": IP_B})
    fake = _mesh(monkeypatch)

    summary = await OTBRClient(BASE).auto_locate()

    assert "identified 2" in summary
    assert _ext(engine, ids["Bulb A"]) == "BA:82:D3:8D:09:F4:46:A4"
    assert _ext(engine, ids["Bulb B"]) == "82:64:89:F3:7B:DA:52:03"
    # Own route table, then routers in id order, stopping once both targets answered.
    assert fake.destinations == [0x1800, 0x0000, 0x9000]


async def test_stops_walking_as_soon_as_all_targets_answered(engine, monkeypatch):
    _seed(engine, EXT_A, devices={"Bulb A": IP_A})
    fake = _mesh(monkeypatch)

    await OTBRClient(BASE).auto_locate()

    assert fake.destinations == [0x1800, 0x0000]  # 0x9000 is never queried


async def test_does_nothing_when_the_setting_is_off(engine, monkeypatch):
    _seed(engine, EXT_A, devices={"Bulb A": IP_A})
    with Session(engine) as s:
        settings_store.set_bool(s, "otbr", "auto_locate", False)
        s.commit()
    fake = _mesh(monkeypatch)

    assert await OTBRClient(BASE).auto_locate() is None
    assert fake.destinations == []


async def test_sends_nothing_without_an_unidentified_router(engine, monkeypatch):
    _seed(engine, devices={"Bulb A": IP_A})
    fake = _mesh(monkeypatch)

    assert await OTBRClient(BASE).auto_locate() is None
    assert fake.destinations == []


async def test_sends_nothing_without_a_candidate_device(engine, monkeypatch):
    _seed(engine, EXT_A)  # an unidentified router, but no registry device to match it to
    fake = _mesh(monkeypatch)

    assert await OTBRClient(BASE).auto_locate() is None
    assert fake.destinations == []


async def test_a_resolved_router_is_not_queried_again(engine, monkeypatch):
    _seed(engine, EXT_A, devices={"Bulb A": IP_A})
    fake = _mesh(monkeypatch)
    client = OTBRClient(BASE)

    assert await client.auto_locate() is not None
    first = list(fake.destinations)
    assert await client.auto_locate() is None
    assert fake.destinations == first


async def test_an_unresolved_router_is_not_retried_within_the_cooldown(engine, monkeypatch):
    # The router answers but matches no registry device (the candidate has another address).
    _seed(engine, EXT_A, devices={"Other bulb": "fd00::99"})
    fake = _mesh(monkeypatch)
    client = OTBRClient(BASE)

    first = await client.auto_locate()
    assert first is not None and "identified 0" in first
    sent = len(fake.destinations)

    assert await client.auto_locate() is None
    assert len(fake.destinations) == sent


async def test_a_stale_extended_address_makes_the_device_a_candidate(engine, monkeypatch):
    ids = _seed(engine, EXT_A, devices={"Bulb A": IP_A})
    with Session(engine) as s:
        integration_data.merge(
            s,
            device_id=ids["Bulb A"],
            integration="otbr",
            payload={"thread_ext_address": "11:22:33:44:55:66:77:88"},  # no longer on the mesh
        )
        s.commit()
    _mesh(monkeypatch)

    await OTBRClient(BASE).auto_locate()

    assert _ext(engine, ids["Bulb A"]) == "BA:82:D3:8D:09:F4:46:A4"


async def test_skips_while_another_unicast_walk_is_running(engine, monkeypatch):
    _seed(engine, EXT_A, devices={"Bulb A": IP_A})
    fake = _mesh(monkeypatch)
    client = OTBRClient(BASE)

    async with client._mesh_lock:
        assert await client.auto_locate() is None
    assert fake.destinations == []


async def test_run_auto_locate_never_raises(engine, monkeypatch):
    _seed(engine, EXT_A, devices={"Bulb A": IP_A})
    fake = _FakeMesh({0x1800: None})  # border router cannot list routers
    monkeypatch.setattr(otbr_client.httpx, "AsyncClient", fake.client)

    await OTBRClient(BASE).run_auto_locate()  # must swallow the RuntimeError


async def test_a_failed_walk_is_retried_on_the_next_poll(engine, monkeypatch):
    _seed(engine, EXT_A, devices={"Bulb A": IP_A})
    client = OTBRClient(BASE)
    monkeypatch.setattr(otbr_client.httpx, "AsyncClient", _FakeMesh({0x1800: None}).client)
    await client.run_auto_locate()

    fake = _mesh(monkeypatch)
    assert await client.auto_locate() is not None
    assert fake.destinations


# ── hooks ─────────────────────────────────────────────────────────────────────


async def test_background_poll_and_sync_run_auto_locate_but_poll_once_does_not(monkeypatch):
    client = OTBRClient(BASE)
    calls: list[str] = []

    async def fake_poll_once(**kwargs):
        calls.append("poll_once")

    async def fake_auto():
        calls.append("auto")

    monkeypatch.setattr(client, "poll_once", fake_poll_once)
    monkeypatch.setattr(client, "run_auto_locate", fake_auto)

    await client._poll_once()
    await client.ingest()
    assert calls == ["poll_once", "auto", "poll_once", "auto"]


# ── Settings checkbox (web + API) ─────────────────────────────────────────────


@pytest.fixture
def web(tmp_path: Path) -> Iterator[TestClient]:
    os.environ.setdefault("SUPERVISOR_TOKEN", "")
    eng = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    SQLModel.metadata.create_all(eng)

    def _override() -> Iterator[Session]:
        with Session(eng) as session:
            yield session

    application = create_app()
    application.dependency_overrides[get_session] = _override
    yield TestClient(application)


def _checkbox(html: str) -> str:
    start = html.index('id="otbr-auto-locate"')
    return html[start : html.index(">", start)]


def test_settings_page_shows_the_checkbox_checked_by_default(web: TestClient):
    html = web.get("/settings/integrations").text
    assert "checked" in _checkbox(html)
    assert 'hx-post="/settings/integrations/otbr/auto-locate"' in _checkbox(html)


def test_unchecking_saves_and_the_page_reflects_it(web: TestClient):
    assert web.post("/settings/integrations/otbr/auto-locate", data={}).status_code == 204
    assert "checked" not in _checkbox(web.get("/settings/integrations").text)

    assert (
        web.post("/settings/integrations/otbr/auto-locate", data={"enabled": "1"}).status_code
        == 204
    )
    assert "checked" in _checkbox(web.get("/settings/integrations").text)


def test_api_reads_and_patches_the_setting(web: TestClient):
    assert web.get("/api/integrations/otbr/settings").json() == {"auto_locate": True}
    assert web.patch("/api/integrations/otbr/settings", json={"auto_locate": False}).json() == {
        "auto_locate": False
    }
    assert web.get("/api/integrations/otbr/settings").json() == {"auto_locate": False}
    # An empty patch changes nothing.
    assert web.patch("/api/integrations/otbr/settings", json={}).json() == {"auto_locate": False}
