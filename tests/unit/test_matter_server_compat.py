"""Compatibility tests for the Matter Server integration across server generations.

Python Matter Server 8.0.0 (schema 11) stays the baseline; matter.js server
capabilities are detected through ``server_info.schema_version``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import app.database
from app.integrations.matter_server.server_client import (
    DEFAULT_CONTROLLER_LABEL,
    MatterServerClient,
    NodeInfo,
    _apply_nodes,
    _read_fabric_label,
    _schema_version_of,
)
from app.models import Device, DeviceProtocol, Fabric


@pytest.fixture
def engine(monkeypatch):
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(eng)
    monkeypatch.setattr(app.database, "engine", eng)
    return eng


def _node(**over) -> NodeInfo:
    base = dict(
        node_id=1,
        available=True,
        vendor_id=0xFFF1,
        vendor_name="Acme",
        product_id=0x8001,
        product_name="Smart Bulb",
        serial="SN-1",
        hardware_version_string="1.0",
        firmware_version_string="2.3.4",
        node_label=None,
        unique_id="uid-1",
        manufacturing_date=None,
        product_url=None,
        part_number=None,
        mac_address="AA:BB:CC:DD:EE:FF",
        network_type=["thread"],
    )
    base.update(over)
    return NodeInfo(**base)


# ── schema_version detection ──────────────────────────────────────────────────


def test_schema_version_read_from_server_info():
    mc = SimpleNamespace(server_info=SimpleNamespace(schema_version=14))
    assert _schema_version_of(mc) == 14


@pytest.mark.parametrize(
    "mc",
    [
        SimpleNamespace(server_info=None),
        SimpleNamespace(),
        SimpleNamespace(server_info=SimpleNamespace(schema_version=None)),
        SimpleNamespace(server_info=SimpleNamespace(schema_version="not-a-number")),
    ],
)
def test_schema_version_missing_or_invalid_is_none(mc):
    assert _schema_version_of(mc) is None


def test_supports_schema_false_until_connected():
    client = MatterServerClient("ws://localhost:5580/ws")
    assert client.schema_version is None
    assert client.supports_schema(11) is False


def test_supports_schema_compares_against_minimum():
    client = MatterServerClient("ws://localhost:5580/ws")
    client._schema_version = 11  # Python Matter Server 8.0.0
    assert client.supports_schema(11) is True
    assert client.supports_schema(12) is False
    client._schema_version = 14  # matter.js 1.5.x
    assert client.supports_schema(12) is True
    assert client.supports_schema(13) is True


def test_stop_clears_schema_version():
    client = MatterServerClient("ws://localhost:5580/ws")
    client._schema_version = 14
    client._on_stopped()
    assert client.schema_version is None


# ── null attribute updates must not erase stored values ───────────────────────


def test_sync_with_removed_attributes_keeps_stored_device_fields(engine):
    """matter.js reports an attribute a device dropped as ``null``; the next
    sync sees ``None`` for it and must leave the stored value in place."""
    with Session(engine) as s:
        _apply_nodes(s, [_node()], integration=None)

    stripped = _node(
        serial=None,
        hardware_version_string=None,
        firmware_version_string=None,
        unique_id=None,
        mac_address=None,
        network_type=[],
    )
    with Session(engine) as s:
        result = _apply_nodes(s, [stripped], integration=None)
        # Still correlates to the same Device via the node_id membership.
        assert result["create"] == 0
        device = s.exec(select(Device)).one()
        assert device.serial == "SN-1"
        assert device.hardware_version == "1.0"
        assert device.firmware_version == "2.3.4"
        assert device.matter_unique_id == "uid-1"
        assert device.mac_address == "AA:BB:CC:DD:EE:FF"
        assert device.network_type == ["thread"]


def test_sync_with_removed_catalog_attributes_keeps_product(engine):
    with Session(engine) as s:
        _apply_nodes(s, [_node()], integration=None)

    with Session(engine) as s:
        _apply_nodes(
            s,
            [_node(vendor_name=None, product_name=None, vendor_id=None, product_id=None)],
            integration=None,
        )
        device = s.exec(select(Device)).one()
        assert device.product_record is not None
        assert device.product_record.vendor == "Acme"
        assert device.product_record.name == "Smart Bulb"
        assert device.product_record.vendor_id == 0xFFF1
        assert device.product_record.product_id == 0x8001


# ── effective fabric label (schema 12) ─────────────────────────────────────────────


class _TypedClient:
    def __init__(self, label):
        self._label = label

    async def get_fabric_label(self):
        return self._label


class _RawClient:
    def __init__(self, reply):
        self._reply = reply
        self.calls: list[tuple] = []

    async def send_command(self, command, require_schema=None, **args):
        self.calls.append((command, require_schema))
        return self._reply


@pytest.mark.parametrize("label", [None, "", "   "])
async def test_blank_fabric_label_is_none(label):
    assert await _read_fabric_label(_TypedClient(label)) is None


async def test_typed_client_fabric_label_is_stripped():
    assert await _read_fabric_label(_TypedClient(" Kodu ")) == "Kodu"


async def test_raw_command_used_when_client_has_no_typed_getter():
    client = _RawClient({"fabric_label": "Kodu"})
    assert await _read_fabric_label(client) == "Kodu"
    assert client.calls == [("get_fabric_label", 12)]


async def test_raw_command_null_label_is_none():
    assert await _read_fabric_label(_RawClient({"fabric_label": None})) is None


def test_fabric_label_cleared_on_stop():
    client = MatterServerClient("ws://localhost:5580/ws")
    client._fabric_label = "Kodu"
    client._on_stopped()
    assert client.fabric_label is None


def _integration(label):
    return SimpleNamespace(
        fabric_label=label,
        supported_protocols=frozenset({DeviceProtocol.matter}),
        assert_capabilities=lambda *a, **k: None,
    )


def test_placeholder_fabric_uses_server_label(engine):
    with Session(engine) as s:
        _apply_nodes(s, [_node()], integration=_integration("Kodu"))
        fabric = s.exec(select(Fabric)).one()
        assert fabric.controller == "Kodu"
        assert fabric.fabric_label == "Kodu"


@pytest.mark.parametrize("integration", [None, _integration(None)])
def test_placeholder_fabric_keeps_default_label_without_server_label(engine, integration):
    with Session(engine) as s:
        _apply_nodes(s, [_node()], integration=integration)
        fabric = s.exec(select(Fabric)).one()
        assert fabric.controller == DEFAULT_CONTROLLER_LABEL


def test_placeholder_fabric_label_follows_server_label_change(engine):
    with Session(engine) as s:
        _apply_nodes(s, [_node()], integration=_integration(None))
    with Session(engine) as s:
        _apply_nodes(s, [_node()], integration=_integration("Kodu"))
        assert s.exec(select(Fabric)).one().controller == "Kodu"
