"""Unit tests for provenance-gated property upserts (B.7).

`_upsert_credential` mirrors `set_field`: a property value is overwritten only
when the incoming source's priority is equal-or-higher than the stored row's.
Property.source uses the unified FieldSource enum.
"""

from __future__ import annotations

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from app.models import FieldSource, Property, PropertyType
from app.services import _upsert_credential


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _get(session: Session, device_id: str, ptype: PropertyType) -> Property | None:
    return session.exec(
        select(Property).where(Property.device_id == device_id, Property.type == ptype)
    ).first()


def test_insert_new_property(session):
    _upsert_credential(session, "dev1", PropertyType.setup_pin, "1234", FieldSource.scanned)
    session.commit()
    row = _get(session, "dev1", PropertyType.setup_pin)
    assert row is not None
    assert row.value == "1234"
    assert row.source == FieldSource.scanned


def test_higher_priority_overwrites(session):
    _upsert_credential(session, "dev1", PropertyType.setup_pin, "old", FieldSource.generated)
    session.commit()
    _upsert_credential(session, "dev1", PropertyType.setup_pin, "new", FieldSource.scanned)
    session.commit()
    row = _get(session, "dev1", PropertyType.setup_pin)
    assert row is not None
    assert row.value == "new"
    assert row.source == FieldSource.scanned


def test_equal_priority_overwrites(session):
    _upsert_credential(session, "dev1", PropertyType.setup_pin, "first", FieldSource.scanned)
    session.commit()
    _upsert_credential(session, "dev1", PropertyType.setup_pin, "second", FieldSource.scanned)
    session.commit()
    row = _get(session, "dev1", PropertyType.setup_pin)
    assert row is not None
    assert row.value == "second"


def test_lower_priority_does_not_overwrite(session):
    # A user-entered value (255) must survive a later scan (200).
    _upsert_credential(session, "dev1", PropertyType.setup_pin, "user-pin", FieldSource.user)
    session.commit()
    _upsert_credential(session, "dev1", PropertyType.setup_pin, "scan-pin", FieldSource.scanned)
    session.commit()
    row = _get(session, "dev1", PropertyType.setup_pin)
    assert row is not None
    assert row.value == "user-pin"
    assert row.source == FieldSource.user


def test_default_source_is_scanned(session):
    _upsert_credential(session, "dev1", PropertyType.qr_payload, "MT:ABC")
    session.commit()
    row = _get(session, "dev1", PropertyType.qr_payload)
    assert row is not None
    assert row.source == FieldSource.scanned
