"""Unit tests for the integration protocol guard (B.8).

Each integration declares ``supported_protocols``; ``assert_capabilities``
refuses to create a Device whose protocol is outside that set, and the three
real clients all declare Matter-only support. This keeps a Matter source from
ever creating or touching a HomeKit device.
"""

from __future__ import annotations

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.integrations.base import Integration, SyncResult
from app.integrations.ha.client import HACoreClient
from app.integrations.matter_server.server_client import MatterServerClient
from app.integrations.otbr.client import OTBRClient
from app.models import Device, DeviceProtocol


class _StubIntegration(Integration):
    """Minimal concrete Integration for exercising assert_capabilities."""

    slug = "stub"
    display_name = "Stub"
    can_create_devices = True

    def __init__(self, supported: frozenset[DeviceProtocol]) -> None:
        self.supported_protocols = supported  # type: ignore[misc]

    @property
    def enabled(self) -> bool:
        return True

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def ingest(self) -> None: ...
    def project(self, session) -> SyncResult:
        return SyncResult()


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def test_assert_capabilities_refuses_unsupported_protocol_create(session):
    """A Matter-only integration must reject a newly-created HomeKit Device."""
    integ = _StubIntegration(frozenset({DeviceProtocol.matter}))
    session.add(Device(name="HK device", protocol=DeviceProtocol.homekit))
    with pytest.raises(RuntimeError, match="not in"):
        integ.assert_capabilities(session, created=1)


def test_assert_capabilities_allows_supported_protocol_create(session):
    """A Matter-only integration accepts a newly-created Matter Device."""
    integ = _StubIntegration(frozenset({DeviceProtocol.matter}))
    session.add(Device(name="Matter device", protocol=DeviceProtocol.matter))
    integ.assert_capabilities(session, created=1)  # must not raise


def test_assert_capabilities_guard_is_driven_by_declared_set(session):
    """An integration that declares HomeKit support may create a HomeKit Device."""
    integ = _StubIntegration(frozenset({DeviceProtocol.matter, DeviceProtocol.homekit}))
    session.add(Device(name="HK device", protocol=DeviceProtocol.homekit))
    integ.assert_capabilities(session, created=1)  # must not raise


@pytest.mark.parametrize("client_cls", [MatterServerClient, OTBRClient])
def test_real_matter_clients_declare_matter_only(client_cls):
    """Matter Server and OTBR correlate Matter devices only."""
    assert client_cls.supported_protocols == frozenset({DeviceProtocol.matter})


def test_ha_core_client_supports_both_protocols():
    """Home Assistant Core imports both Matter and HomeKit devices."""
    assert HACoreClient.supported_protocols == frozenset(
        {DeviceProtocol.matter, DeviceProtocol.homekit}
    )
