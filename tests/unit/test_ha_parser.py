"""
Unit tests for app.ha.client._parse_matter_identifiers.

Verifies the Python-side parser that converts HA Matter device identifiers
into structured fabric_id / node_id / serial fields.
"""

from __future__ import annotations

from app.integrations.ha.client import _parse_matter_identifiers


def test_deviceid_parsed():
    """deviceid_ token yields fabric_id (lowercase hex) and node_id (decimal int)."""
    ids = [["matter", "deviceid_D990EA668A3939E7-000000000000003C-MatterNodeDevice"]]
    result = _parse_matter_identifiers(ids)
    assert result["fabric_id"] == "d990ea668a3939e7"
    assert result["node_id"] == 0x3C  # 60
    assert result["serial"] is None


def test_serial_parsed():
    """serial_ token yields serial string."""
    ids = [["matter", "serial_N25180B0K98"]]
    result = _parse_matter_identifiers(ids)
    assert result["serial"] == "N25180B0K98"
    assert result["fabric_id"] is None
    assert result["node_id"] is None


def test_both_tokens_in_one_device():
    """Device with both deviceid_ and serial_ tokens (e.g. Eve Energy sample)."""
    ids = [
        ["matter", "deviceid_D990EA668A3939E7-0000000000000043-MatterNodeDevice"],
        ["matter", "serial_RV10P1M00873"],
    ]
    result = _parse_matter_identifiers(ids)
    assert result["fabric_id"] == "d990ea668a3939e7"
    assert result["node_id"] == 0x43  # 67
    assert result["serial"] == "RV10P1M00873"


def test_non_matter_domain_ignored():
    """Identifiers in other domains must not affect output."""
    ids = [
        ["homeassistant", "serial_shouldbeignored"],
        ["matter", "serial_REAL"],
    ]
    result = _parse_matter_identifiers(ids)
    assert result["serial"] == "REAL"


def test_malformed_entries_skipped():
    """Short/null/string entries must not crash the parser."""
    ids = ["notalist", None, ["single"], ["matter", "serial_ok"]]
    result = _parse_matter_identifiers(ids)
    assert result["serial"] == "ok"


def test_empty_identifiers():
    result = _parse_matter_identifiers([])
    assert result["fabric_id"] is None
    assert result["node_id"] is None
    assert result["serial"] is None
    assert result["matter_uid_set"] == frozenset()
    assert result["matter_unique_id"] is None


def test_none_identifiers():
    result = _parse_matter_identifiers([])
    assert result["fabric_id"] is None
    assert result["node_id"] is None
    assert result["serial"] is None
    assert result["matter_uid_set"] == frozenset()
    assert result["matter_unique_id"] is None


def test_node_id_zero_parsed():
    """node_id = 0 is valid and must not be confused with falsy."""
    ids = [["matter", "deviceid_AAAA-0000000000000000-MatterNodeDevice"]]
    result = _parse_matter_identifiers(ids)
    assert result["node_id"] == 0


def test_fabric_id_lowercased():
    """fabric_id is always returned as a lowercase hex string."""
    ids = [["matter", "deviceid_DEADBEEF12345678-0000000000000001-MatterNodeDevice"]]
    result = _parse_matter_identifiers(ids)
    assert result["fabric_id"] == "deadbeef12345678"
