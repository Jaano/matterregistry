"""Deleting a Device must not leave per-device rows behind.

Orphaned integration data kept a deleted device's addresses, which then looked
shared by two devices to ``AddressIndex`` and blocked every match on them.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select

from app.app import create_app
from app.database import get_session
from app.integrations import data as integration_data
from app.integrations.correlate import AddressIndex
from app.models import Device, DeviceIntegrationData, DeviceLink

OMR = "fd3e:1cca:93f4:0:24e5:3231:3b2b:1dbf"


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    os.environ.setdefault("SUPERVISOR_TOKEN", "")
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    SQLModel.metadata.create_all(engine)

    def _override() -> Iterator[Session]:
        with Session(engine) as session:
            yield session

    app = create_app()
    app.dependency_overrides[get_session] = _override
    app.state.test_engine = engine
    yield TestClient(app)


def _seed(client: TestClient) -> str:
    with Session(client.app.state.test_engine) as s:  # type: ignore[union-attr]
        dev = Device(name="Bulb")
        s.add(dev)
        s.flush()
        s.add(DeviceLink(device_id=dev.id, integration="mdns", external_id="AA:BB"))
        integration_data.upsert(
            s, device_id=dev.id, integration="mdns", payload={"ipv6_addresses": [OMR]}
        )
        integration_data.upsert(
            s, device_id=dev.id, integration="otbr", payload={"thread_ext_address": "BA:82"}
        )
        s.commit()
        return dev.id


def _leftovers(client: TestClient, device_id: str) -> tuple[int, int, int]:
    with Session(client.app.state.test_engine) as s:  # type: ignore[union-attr]
        return (
            len(s.exec(select(Device).where(Device.id == device_id)).all()),
            len(s.exec(select(DeviceLink).where(DeviceLink.device_id == device_id)).all()),
            len(
                s.exec(
                    select(DeviceIntegrationData).where(
                        DeviceIntegrationData.device_id == device_id
                    )
                ).all()
            ),
        )


def test_api_delete_removes_links_and_integration_data(client: TestClient):
    device_id = _seed(client)
    assert _leftovers(client, device_id) == (1, 1, 2)

    assert client.delete(f"/api/devices/{device_id}").status_code == 204

    assert _leftovers(client, device_id) == (0, 0, 0)


def test_web_delete_removes_links_and_integration_data(client: TestClient):
    device_id = _seed(client)

    assert client.delete(f"/devices/{device_id}").status_code == 200

    assert _leftovers(client, device_id) == (0, 0, 0)


def test_address_index_ignores_rows_of_a_deleted_device(client: TestClient):
    """An orphan holding the same address must not make the live owner ambiguous."""
    engine = client.app.state.test_engine  # type: ignore[union-attr]
    with Session(engine) as s:
        live = Device(name="Live")
        s.add(live)
        s.flush()
        integration_data.upsert(
            s, device_id=live.id, integration="mdns", payload={"ipv6_addresses": [OMR]}
        )
        # Row of a device that was deleted without cascading (pre-fix data).
        integration_data.upsert(
            s,
            device_id="deleted-device-id",
            integration="mdns",
            payload={"ipv6_addresses": [OMR], "mac_address": "BA:82:D3:8D:09:F4:46:A4"},
        )
        s.commit()

        index = AddressIndex(s)
        assert index.match(ipv6s=[OMR]) == live.id
        assert index.match(macs=["ba82d38d09f446a4"]) is None
