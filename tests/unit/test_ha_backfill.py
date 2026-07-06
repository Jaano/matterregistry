"""Unit tests for matter_unique_id backfill provenance rules.

These exercise set_field() directly (no DB required) to confirm the
FieldSource priority ordering: user(255) > scanned(200) > ha(150) > matter(100).
HA no longer writes to matter_unique_id at runtime (see app/ha/client.py);
these tests guard the underlying set_field() priority rules as a regression net.
"""

from __future__ import annotations

from app.models import Device, FieldSource
from app.services import set_field


def _device_with_uid(uid: str, source: FieldSource) -> Device:
    dev = Device(name="test")
    dev.matter_unique_id = uid
    dev.matter_unique_id_source = source
    return dev


def test_ha_cannot_overwrite_scanned_uid():
    """HA (priority 150) must not overwrite a scanned UID (priority 200)."""
    dev = _device_with_uid("scanned_uid_xyz", FieldSource.scanned)
    changed = set_field(dev, "matter_unique_id", "ha_uid_different", FieldSource.ha)
    assert not changed
    assert dev.matter_unique_id == "scanned_uid_xyz"
    assert dev.matter_unique_id_source == FieldSource.scanned


def test_ha_can_overwrite_matter_server_uid():
    """HA (priority 150) must overwrite a Matter Server UID (priority 100)."""
    dev = _device_with_uid("matter_server_uid", FieldSource.matter)
    changed = set_field(dev, "matter_unique_id", "ha_uid_wins", FieldSource.ha)
    assert changed
    assert dev.matter_unique_id == "ha_uid_wins"
    assert dev.matter_unique_id_source == FieldSource.ha
