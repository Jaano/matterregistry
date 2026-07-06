"""Regression tests for importing devices with a null/absent protocol.

A device that was never scanned has ``protocol = None``; the exporter writes
``"protocol": null`` for it. Import must accept that (validation) and restore it
as ``None`` (apply), not reject it as "unknown protocol None". An absent
``protocol`` key (legacy backups predating the field) defaults to ``matter``.
"""

from __future__ import annotations

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.importer import apply_import, plan_import
from app.models import Device, DeviceProtocol


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _payload(*devices: dict) -> dict:
    return {"format_version": 8, "devices": list(devices)}


def test_plan_import_accepts_null_protocol(session):
    payload = _payload({"id": "dev-null", "name": "Unscanned", "protocol": None})
    plan = plan_import(session, payload)
    assert plan.errors == []


def test_apply_import_restores_null_protocol(session):
    payload = _payload({"id": "dev-null", "name": "Unscanned", "protocol": None})
    apply_import(session, payload)
    device = session.get(Device, "dev-null")
    assert device is not None
    assert device.protocol is None


def test_apply_import_absent_protocol_defaults_to_matter(session):
    payload = _payload({"id": "dev-legacy", "name": "Legacy"})
    apply_import(session, payload)
    device = session.get(Device, "dev-legacy")
    assert device is not None
    assert device.protocol is DeviceProtocol.matter


def test_apply_import_preserves_explicit_protocol(session):
    payload = _payload({"id": "dev-hk", "name": "HomeKit", "protocol": "homekit"})
    apply_import(session, payload)
    device = session.get(Device, "dev-hk")
    assert device is not None
    assert device.protocol is DeviceProtocol.homekit


def test_plan_import_still_rejects_bad_protocol(session):
    payload = _payload({"id": "dev-bad", "name": "Bad", "protocol": "zigbee"})
    plan = plan_import(session, payload)
    assert any("unknown protocol" in e for e in plan.errors)
