"""Unit tests for HA Core's HomeKit device import (B.9).

`HACoreClient._sync_devices` takes a list of HA device-registry dicts and
applies the protocol-aware create/correlate split:
- HomeKit: create + correlate, deduped on serial, with stale-link re-point.
- Matter: correlate-only - HA Core must never create a Matter device.
"""

from __future__ import annotations

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import app.database
from app.integrations.ha.client import HACoreClient
from app.models import Device, DeviceLink, DeviceProtocol


@pytest.fixture
def engine(monkeypatch):
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(eng)
    # _sync_devices does `from ..database import engine` at call time.
    monkeypatch.setattr(app.database, "engine", eng)
    return eng


@pytest.fixture
def client():
    return HACoreClient("http://ha.test", "token")


def _hk(ha_id: str, serial: str | None, **over) -> dict:
    d = {
        "id": ha_id,
        "protocol": "homekit",
        "name": "Presence-Sensor-FP2",
        "manufacturer": "Aqara",
        "model": "PS-S02E",
        "area_name": "elutuba",
        "sw_version": "1.3.3",
        "hw_version": "1.0.0",
        "serial": serial,
    }
    d.update(over)
    return d


def _devices(engine) -> list[Device]:
    with Session(engine) as s:
        return list(s.exec(select(Device)).all())


def _links(engine) -> list[DeviceLink]:
    with Session(engine) as s:
        return list(s.exec(select(DeviceLink)).all())


def test_homekit_device_created_from_ha(engine, client):
    client._sync_devices([_hk("ha-1", "54EF44777DD0")], reason="t")
    devs = _devices(engine)
    assert len(devs) == 1
    d = devs[0]
    assert d.protocol == DeviceProtocol.homekit
    assert d.serial == "54EF44777DD0"
    assert d.vendor == "Aqara"
    assert d.product == "PS-S02E"
    assert d.firmware_version == "1.3.3"
    assert d.hardware_version == "1.0.0"
    assert d.room == "elutuba"
    links = _links(engine)
    assert len(links) == 1
    assert links[0].external_id == "ha-1"
    assert links[0].device_id == d.id


def test_homekit_resync_is_idempotent(engine, client):
    client._sync_devices([_hk("ha-1", "54EF44777DD0")], reason="t1")
    client._sync_devices([_hk("ha-1", "54EF44777DD0")], reason="t2")
    assert len(_devices(engine)) == 1
    assert len(_links(engine)) == 1


def test_homekit_fields_refresh_on_resync(engine, client):
    """Regression: an already-imported HomeKit device must pick up HA field
    changes on later syncs (the line-538 `created_new` fix)."""
    client._sync_devices([_hk("ha-1", "54EF44777DD0")], reason="t1")
    client._sync_devices(
        [_hk("ha-1", "54EF44777DD0", name="Renamed FP2", sw_version="2.0.0")],
        reason="t2",
    )
    devs = _devices(engine)
    assert len(devs) == 1
    assert devs[0].firmware_version == "2.0.0"
    assert devs[0].name == "Renamed FP2"


def test_homekit_stale_link_repoint_on_repair(engine, client):
    """Re-pairing assigns a new HA device id; serial match re-points the link."""
    client._sync_devices([_hk("ha-old", "AAA111")], reason="t1")
    client._sync_devices([_hk("ha-new", "AAA111")], reason="t2")
    assert len(_devices(engine)) == 1  # no duplicate
    links = _links(engine)
    assert len(links) == 1
    assert links[0].external_id == "ha-new"  # re-pointed


def test_homekit_without_serial_is_skipped(engine, client):
    res = client._sync_devices([_hk("ha-x", None)], reason="t")
    assert _devices(engine) == []
    assert res["skipped"] >= 1


def test_matter_device_not_created_by_ha_core(engine, client):
    """HA Core correlates Matter devices but never creates them."""
    client._sync_devices(
        [
            {
                "id": "ha-m",
                "protocol": "matter",
                "name": "Bulb",
                "serial": "S1",
                "matter_unique_id": "uid-1",
            }
        ],
        reason="t",
    )
    assert _devices(engine) == []
