"""Unit tests for the cross-protocol merge guard (ISSUES I.12).

`merge_devices` must refuse to merge devices whose commissioning protocols
differ - otherwise a HomeKit onboarding code could land on a Matter device
(or vice versa), the exact mismatch the A.14 scan guard prevents.
"""

from __future__ import annotations

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.models import Device, DeviceProtocol
from app.services import ProtocolMismatchError, merge_devices


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _add(session: Session, name: str, protocol: DeviceProtocol) -> Device:
    dev = Device(name=name, protocol=protocol)
    session.add(dev)
    session.commit()
    session.refresh(dev)
    return dev


def test_merge_refuses_cross_protocol(session):
    src = _add(session, "HomeKit dev", DeviceProtocol.homekit)
    tgt = _add(session, "Matter dev", DeviceProtocol.matter)
    with pytest.raises(ProtocolMismatchError, match="protocols must match"):
        merge_devices(session, source_id=src.id, target_id=tgt.id)


def test_merge_allows_same_protocol(session):
    src = _add(session, "Matter A", DeviceProtocol.matter)
    tgt = _add(session, "Matter B", DeviceProtocol.matter)
    merge_devices(session, source_id=src.id, target_id=tgt.id)
    session.commit()
    assert session.get(Device, src.id) is None  # source consumed
    assert session.get(Device, tgt.id) is not None  # target survives
