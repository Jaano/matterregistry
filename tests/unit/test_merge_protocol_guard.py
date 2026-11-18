"""Unit tests for the cross-protocol merge guard (ISSUES I.12).

`merge_devices` must refuse to merge devices whose commissioning protocols
differ - otherwise a HomeKit onboarding code could land on a Matter device
(or vice versa), the exact mismatch the A.14 scan guard prevents.
"""

from __future__ import annotations

import json

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from app.models import (
    Device,
    DeviceIntegrationData,
    DeviceLink,
    DeviceProtocol,
    FieldSource,
    Property,
    PropertyType,
)
from app.services import ProtocolMismatchError, build_merge_preview, merge_devices


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


def test_merge_ltpdu_shell_into_hap_device_preserves_identity_and_data(session):
    eui64 = "70AC08FFFEA8269A"
    hap_id = "A5:E1:CC:77:FC:3A"
    shell = Device(name=eui64, protocol=DeviceProtocol.homekit, name_source=FieldSource.mdns)
    primary = Device(
        name="Nanoleaf A19 6HW4",
        protocol=DeviceProtocol.homekit,
        name_source=FieldSource.ha,
        homekit_accessory_id=hap_id,
    )
    session.add_all([shell, primary])
    session.flush()
    session.add_all(
        [
            DeviceLink(device_id=shell.id, integration="mdns", external_id=eui64),
            DeviceLink(device_id=primary.id, integration="mdns", external_id=hap_id),
            DeviceIntegrationData(
                device_id=shell.id,
                integration="mdns",
                payload_json=json.dumps({"ltpdu_eui64": eui64, "ipv6_addresses": ["fd00::2"]}),
            ),
            DeviceIntegrationData(
                device_id=primary.id,
                integration="mdns",
                payload_json=json.dumps(
                    {"hap_accessory_id": hap_id, "ipv6_addresses": ["fd00::1"]}
                ),
            ),
            Property(device_id=primary.id, type=PropertyType.setup_pin, value="12345678"),
        ]
    )
    session.commit()

    preview = build_merge_preview(shell, primary)
    assert next(row for row in preview if row["label"] == "Name")["winner"] == "target"
    merge_devices(session, source_id=shell.id, target_id=primary.id)
    session.commit()

    assert session.get(Device, shell.id) is None
    assert session.get(Device, primary.id).name == "Nanoleaf A19 6HW4"
    assert session.exec(select(Property).where(Property.device_id == primary.id)).one().value == (
        "12345678"
    )
    links = session.exec(select(DeviceLink).where(DeviceLink.integration == "mdns")).all()
    assert [(link.device_id, link.external_id) for link in links] == [(primary.id, hap_id)]
    rows = session.exec(select(DeviceIntegrationData)).all()
    assert len(rows) == 1
    assert rows[0].device_id == primary.id
    payload = json.loads(rows[0].payload_json)
    assert payload["hap_accessory_id"] == hap_id
    assert payload["ltpdu_eui64"] == eui64
    assert set(payload["ipv6_addresses"]) == {"fd00::1", "fd00::2"}
