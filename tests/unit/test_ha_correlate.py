"""
Unit tests for HA Core auto-correlation logic (B.6).
No container, no network - pure in-process logic.

Covers:
  Key 1 - (vendor_id, product_id, serial) triple: VID+PID gate on MR side,
           serial match case-insensitive, placeholder guard.
  Key 2 - matter_unique_id match via ``matter_uid_set`` frozenset on each HA device.
  Key 3 - (fabric_id, node_id) structural match via ``memberships`` set.
  Priority: Key 1 > Key 2 > Key 3.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.integrations.ha.correlate import _is_placeholder, auto_correlate

# ── Fake MR Device ────────────────────────────────────────────────────────────


@dataclass
class FakeMRDevice:
    name: str = "My Device"
    matter_unique_id: str | None = None
    vendor_id: int | None = None
    product_id: int | None = None
    serial: str | None = None


# ── Sample HA device registry entry (post-_parse_matter_identifiers) ─────────


def _ha_dev(
    id: str = "ha001",
    name: str = "HA Device",
    matter_uid_set: frozenset | None = None,
    fabric_id: str | None = None,
    node_id: int | None = None,
    serial: str | None = None,
) -> dict:
    return {
        "id": id,
        "name": name,
        "manufacturer": "ACME",
        "model": "Widget",
        "area_name": "Living Room",
        "area_id": "",
        "identifiers": [],
        "matter_uid_set": matter_uid_set or frozenset(),
        "fabric_id": fabric_id,
        "node_id": node_id,
        "serial": serial,
    }


# ── _is_placeholder ───────────────────────────────────────────────────────────


def test_placeholder_known_strings():
    for s in ("", "0", "00000000", "ffffffff", "11111111", "test", "unknown", "n/a", "none"):
        assert _is_placeholder(s), f"expected {s!r} to be a placeholder"


def test_placeholder_all_identical_chars():
    assert _is_placeholder("aaaaaaa")
    assert _is_placeholder("ZZZZZZZ")
    assert _is_placeholder("1111")
    assert _is_placeholder("####")


def test_placeholder_strips_whitespace():
    assert _is_placeholder("  00000000  ")
    assert _is_placeholder("  FFFFFFFF  ")


def test_placeholder_case_insensitive():
    assert _is_placeholder("UNKNOWN")
    assert _is_placeholder("TEST")
    assert _is_placeholder("FFFFFFFF")


def test_not_placeholder_real_serials():
    assert not _is_placeholder("N25180B0K98")
    assert not _is_placeholder("RV10P1M00873")
    assert not _is_placeholder("SN001")
    assert not _is_placeholder("AB12CD")


# ── Key 1: (vendor_id, product_id, serial) triple ────────────────────────────


def test_key1_unique_match():
    """All three MR fields set, real serial, single HA device matches."""
    dev = FakeMRDevice(vendor_id=0x100B, product_id=0x0043, serial="N25180B0K98")
    ha = [_ha_dev(id="z", serial="N25180B0K98")]
    assert auto_correlate(dev, ha) == "z"


def test_key1_case_insensitive():
    """Key 1 matches regardless of serial case difference."""
    dev = FakeMRDevice(vendor_id=0x100B, product_id=0x0050, serial="RV10P1M00873")
    ha = [_ha_dev(id="y", serial="rv10p1m00873")]
    assert auto_correlate(dev, ha) == "y"


def test_key1_two_candidates_no_match():
    """Two HA devices with identical serial - no auto-link."""
    dev = FakeMRDevice(vendor_id=0x1234, product_id=0x0001, serial="SHARED")
    ha = [_ha_dev(id="a", serial="SHARED"), _ha_dev(id="b", serial="SHARED")]
    assert auto_correlate(dev, ha) is None


def test_key1_placeholder_serial_skips():
    """Placeholder serial on MR device → Key 1 skipped; falls through."""
    for bad in ("00000000", "ffffffff", "aaaaaaa", "11111111", ""):
        dev = FakeMRDevice(vendor_id=0x1234, product_id=0x0001, serial=bad)
        ha = [_ha_dev(id="x", serial=bad)]
        assert auto_correlate(dev, ha) is None, f"serial {bad!r} should be treated as placeholder"


def test_key1_ha_serial_none_skipped():
    """HA device with serial=None is not matched."""
    dev = FakeMRDevice(vendor_id=0x1234, product_id=0x0001, serial="SN001")
    ha = [_ha_dev(id="x", serial=None)]
    assert auto_correlate(dev, ha) is None


def test_key1_no_serial_skips():
    """mr_device.serial is None - Key 1 is skipped."""
    dev = FakeMRDevice(vendor_id=0x1234, product_id=0x0001, serial=None)
    ha = [_ha_dev(id="x", serial="SOMETHING")]
    assert auto_correlate(dev, ha) is None


def test_key1_no_vendor_id_skips():
    """mr_device.vendor_id is None - Key 1 is skipped."""
    dev = FakeMRDevice(vendor_id=None, product_id=0x0001, serial="SN001")
    ha = [_ha_dev(id="x", serial="SN001")]
    assert auto_correlate(dev, ha) is None


def test_key1_no_product_id_skips():
    """mr_device.product_id is None - Key 1 is skipped."""
    dev = FakeMRDevice(vendor_id=0x1234, product_id=None, serial="SN001")
    ha = [_ha_dev(id="x", serial="SN001")]
    assert auto_correlate(dev, ha) is None


def test_key1_wins_over_key2():
    """Key 1 is checked before Key 2; if both match the same device, Key 1 takes it."""
    dev = FakeMRDevice(
        vendor_id=0x1234, product_id=0x0001, serial="SN001", matter_unique_id="serial_SN001"
    )
    ha = [_ha_dev(id="x", serial="SN001", matter_uid_set=frozenset({"serial_SN001"}))]
    assert auto_correlate(dev, ha) == "x"


def test_key1_wins_over_key3():
    """Key 1 is checked before Key 3; VID+PID+serial beats fabric+node."""
    dev = FakeMRDevice(vendor_id=0x1234, product_id=0x0001, serial="SN001")
    ha = [_ha_dev(id="x", serial="SN001", fabric_id="aaaa", node_id=1)]
    assert auto_correlate(dev, ha, memberships={("aaaa", 1)}) == "x"


def test_key1_no_match_falls_through_to_key2():
    """When Key 1 finds nothing, Key 2 is tried."""
    dev = FakeMRDevice(
        vendor_id=0x1234, product_id=0x0001, serial="NOMATCH", matter_unique_id="serial_SN001"
    )
    ha = [_ha_dev(id="x", serial="OTHER", matter_uid_set=frozenset({"serial_SN001"}))]
    assert auto_correlate(dev, ha) == "x"


def test_key1_no_match_falls_through_to_key3():
    """When Keys 1 and 2 both miss, Key 3 is tried."""
    dev = FakeMRDevice(vendor_id=0x1234, product_id=0x0001, serial="NOMATCH")
    ha = [_ha_dev(id="x", serial="OTHER", fabric_id="aaaa", node_id=1)]
    assert auto_correlate(dev, ha, memberships={("aaaa", 1)}) == "x"


def test_serial_alone_no_longer_links():
    """Serial match without VID+PID gate → no auto-link (serial-alone was removed)."""
    dev = FakeMRDevice(serial="SN001")  # vendor_id and product_id are None
    ha = [_ha_dev(id="x", serial="SN001")]
    assert auto_correlate(dev, ha) is None


# ── Key 2: matter_unique_id match ─────────────────────────────────────────────


def test_key2_unique_match():
    """MR matter_unique_id present in the HA device's matter_uid_set."""
    dev = FakeMRDevice(matter_unique_id="serial_SN001")
    ha = [_ha_dev(id="x", matter_uid_set=frozenset({"serial_SN001", "deviceid_aaaa-0001"}))]
    assert auto_correlate(dev, ha) == "x"


def test_key2_matches_any_uid_in_set():
    """Key 2 matches even when MR stores the deviceid form."""
    dev = FakeMRDevice(
        matter_unique_id="deviceid_D990EA668A3939E7-000000000000003C-MatterNodeDevice"
    )
    ha = [
        _ha_dev(
            id="y",
            matter_uid_set=frozenset(
                {
                    "serial_N25180B0K98",
                    "deviceid_D990EA668A3939E7-000000000000003C-MatterNodeDevice",
                }
            ),
        )
    ]
    assert auto_correlate(dev, ha) == "y"


def test_key2_two_candidates_no_match():
    """Two HA devices share the same matter UID - no auto-link."""
    dev = FakeMRDevice(matter_unique_id="serial_SN001")
    ha = [
        _ha_dev(id="a", matter_uid_set=frozenset({"serial_SN001"})),
        _ha_dev(id="b", matter_uid_set=frozenset({"serial_SN001"})),
    ]
    assert auto_correlate(dev, ha) is None


def test_key2_no_matter_unique_id_skipped():
    """mr_device.matter_unique_id is None - Key 2 is skipped."""
    dev = FakeMRDevice(matter_unique_id=None)
    ha = [_ha_dev(id="x", matter_uid_set=frozenset({"serial_SN001"}))]
    assert auto_correlate(dev, ha) is None


def test_key2_uid_not_in_set_no_match():
    """MR matter_unique_id not found in any HA device's matter_uid_set."""
    dev = FakeMRDevice(matter_unique_id="serial_OTHER")
    ha = [_ha_dev(id="x", matter_uid_set=frozenset({"serial_SN001"}))]
    assert auto_correlate(dev, ha) is None


def test_key2_wins_over_key3():
    """Key 2 is checked before Key 3; matter_uid beats fabric+node."""
    dev = FakeMRDevice(matter_unique_id="serial_SN001")
    ha = [_ha_dev(id="x", matter_uid_set=frozenset({"serial_SN001"}), fabric_id="aaaa", node_id=1)]
    assert auto_correlate(dev, ha, memberships={("aaaa", 1)}) == "x"


def test_key2_no_match_falls_through_to_key3():
    """When Key 2 finds nothing, Key 3 is tried."""
    dev = FakeMRDevice(matter_unique_id="serial_NOMATCH")
    ha = [_ha_dev(id="x", matter_uid_set=frozenset({"serial_SN001"}), fabric_id="aaaa", node_id=1)]
    assert auto_correlate(dev, ha, memberships={("aaaa", 1)}) == "x"


# ── Key 3: (fabric_id, node_id) structural match ──────────────────────────────


def test_key3_unique_match():
    """Single HA device whose (fabric_id, node_id) matches the membership set."""
    dev = FakeMRDevice()
    ha = [_ha_dev(id="x", fabric_id="d990ea668a3939e7", node_id=60)]
    assert auto_correlate(dev, ha, memberships={("d990ea668a3939e7", 60)}) == "x"


def test_key3_two_candidates_no_match():
    """Two HA devices matching the same pair - no auto-link."""
    dev = FakeMRDevice()
    ha = [
        _ha_dev(id="a", fabric_id="aaaa", node_id=1),
        _ha_dev(id="b", fabric_id="aaaa", node_id=1),
    ]
    assert auto_correlate(dev, ha, memberships={("aaaa", 1)}) is None


def test_key3_fabric_mismatch():
    dev = FakeMRDevice()
    ha = [_ha_dev(id="x", fabric_id="bbbb", node_id=1)]
    assert auto_correlate(dev, ha, memberships={("aaaa", 1)}) is None


def test_key3_node_mismatch():
    dev = FakeMRDevice()
    ha = [_ha_dev(id="x", fabric_id="aaaa", node_id=2)]
    assert auto_correlate(dev, ha, memberships={("aaaa", 1)}) is None


def test_key3_no_memberships_skipped():
    dev = FakeMRDevice()
    ha = [_ha_dev(id="x", fabric_id="aaaa", node_id=1)]
    assert auto_correlate(dev, ha, memberships=None) is None


def test_key3_empty_memberships_skipped():
    dev = FakeMRDevice()
    ha = [_ha_dev(id="x", fabric_id="aaaa", node_id=1)]
    assert auto_correlate(dev, ha, memberships=set()) is None


def test_key3_ha_device_missing_fabric_id_skipped():
    dev = FakeMRDevice()
    ha = [_ha_dev(id="x", fabric_id=None, node_id=1)]
    assert auto_correlate(dev, ha, memberships={("aaaa", 1)}) is None


# ── Edge cases ────────────────────────────────────────────────────────────────


def test_empty_ha_devices():
    dev = FakeMRDevice(serial="ABC")
    assert auto_correlate(dev, []) is None


def test_all_none_fields():
    dev = FakeMRDevice()
    ha = [_ha_dev(id="x")]
    assert auto_correlate(dev, ha) is None
