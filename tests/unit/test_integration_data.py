"""Unit tests for the B.12 per-device integration data helpers.

Covers:
- upsert creates a row and returns it
- upsert overwrites payload + refreshes retrieved_at on repeat call
- read returns the stored dict, or None when absent
- read_all_for_device returns rows ordered by integration slug
- DeviceIntegrationData is excluded from backup exports
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.exporter import build_export
from app.integrations.data import read, read_all_for_device, upsert
from app.models import Device, DeviceIntegrationData


@pytest.fixture
def engine():
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(eng)
    return eng


@pytest.fixture
def device(engine):
    with Session(engine) as s:
        dev = Device(name="Test Device")
        s.add(dev)
        s.commit()
        s.refresh(dev)
        return dev


# ── upsert ────────────────────────────────────────────────────────────────────


def test_upsert_creates_row(engine, device):
    with Session(engine) as s:
        row = upsert(s, device_id=device.id, integration="mdns", payload={"a": 1})
        s.commit()
        assert row.device_id == device.id
        assert row.integration == "mdns"
        assert json.loads(row.payload_json) == {"a": 1}
        assert row.retrieved_at is not None

    with Session(engine) as s:
        rows = s.exec(select(DeviceIntegrationData)).all()
        assert len(rows) == 1


def test_upsert_overwrites_on_repeat_call(engine, device):
    with Session(engine) as s:
        upsert(s, device_id=device.id, integration="mdns", payload={"v": 1})
        s.commit()

    t_before = datetime.now(UTC).replace(tzinfo=None)

    with Session(engine) as s:
        row = upsert(s, device_id=device.id, integration="mdns", payload={"v": 2})
        s.commit()
        assert json.loads(row.payload_json) == {"v": 2}
        assert row.retrieved_at >= t_before

    with Session(engine) as s:
        rows = s.exec(select(DeviceIntegrationData)).all()
        assert len(rows) == 1  # no duplicate


def test_upsert_separate_integrations_create_separate_rows(engine, device):
    with Session(engine) as s:
        upsert(s, device_id=device.id, integration="mdns", payload={"x": 1})
        upsert(s, device_id=device.id, integration="ha_core", payload={"y": 2})
        s.commit()

    with Session(engine) as s:
        rows = s.exec(select(DeviceIntegrationData)).all()
        assert len(rows) == 2


# ── read ──────────────────────────────────────────────────────────────────────


def test_read_returns_stored_dict(engine, device):
    with Session(engine) as s:
        upsert(s, device_id=device.id, integration="otbr", payload={"channel": 15})
        s.commit()

    with Session(engine) as s:
        result = read(s, device_id=device.id, integration="otbr")
        assert result == {"channel": 15}


def test_read_returns_none_when_absent(engine, device):
    with Session(engine) as s:
        result = read(s, device_id=device.id, integration="missing")
        assert result is None


# ── read_all_for_device ───────────────────────────────────────────────────────


def test_read_all_for_device_ordered_by_integration(engine, device):
    with Session(engine) as s:
        upsert(s, device_id=device.id, integration="otbr", payload={})
        upsert(s, device_id=device.id, integration="ha_core", payload={})
        upsert(s, device_id=device.id, integration="mdns", payload={})
        s.commit()

    with Session(engine) as s:
        rows = read_all_for_device(s, device_id=device.id)
        assert [r.integration for r in rows] == ["ha_core", "mdns", "otbr"]


def test_read_all_for_device_empty_when_none(engine, device):
    with Session(engine) as s:
        assert read_all_for_device(s, device_id=device.id) == []


# ── export exclusion ──────────────────────────────────────────────────────────


def test_integration_data_excluded_from_export(engine, device):
    with Session(engine) as s:
        upsert(s, device_id=device.id, integration="mdns", payload={"key": "val"})
        s.commit()

    with Session(engine) as s:
        export = build_export(s, app_version="test")

    export_str = json.dumps(export)
    assert "device_integration_data" not in export_str
    assert "integration_data" not in export_str
    # The stored value itself must not leak either
    assert '"key": "val"' not in export_str
    assert "val" not in export_str
