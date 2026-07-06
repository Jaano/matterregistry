"""
Auto-link logic for matching MR Device rows to HA Core device registry entries.

Three deterministic 1:1 keys are tried in order (strongest first):

  Key 1 - (vendor_id, product_id, serial) triple:
    VID and PID must both be set on the MR device (quality gate - HA does not
    expose numeric VID/PID so they are not matched against HA).  Serial must
    be set on both sides, non-placeholder, and equal case-insensitively.
    Globally unique physical-unit identity; stable across re-pair / fabric move.

  Key 2 - matter_unique_id match:
    ``mr_device.matter_unique_id`` is in the pre-computed ``matter_uid_set``
    frozenset of the HA device dict (set of all second elements from
    ('matter', uid) identifier tuples, populated by
    ``client._parse_matter_identifiers()``).

  Key 3 - (fabric_id, node_id) structural match:
    Requires the caller to supply the ``memberships`` set of
    ``(fabric_id_hex, node_id_int)`` pairs for the MR device.
    HA device dicts must have been pre-enriched by
    ``client._parse_matter_identifiers()`` so that ``fabric_id`` and
    ``node_id`` are top-level keys.
    Deterministic but transient (node_id reallocates on re-pair); used only
    when Keys 1 and 2 both miss.

All three keys are deterministic 1:1 and auto-link on match.
Returns None when no key produces a unique match.
"""

from __future__ import annotations

# Known garbage serial strings shipped by some devices.
_PLACEHOLDER_SERIALS: frozenset[str] = frozenset(
    {"", "0", "00000000", "ffffffff", "11111111", "test", "unknown", "n/a", "none"}
)


def _is_placeholder(serial: str) -> bool:
    """Return True if *serial* is a known placeholder or all-identical-chars string."""
    s = serial.strip().lower()
    return s in _PLACEHOLDER_SERIALS or (len(s) > 0 and len(set(s)) == 1)


def auto_correlate(
    mr_device,
    ha_devices: list[dict],
    *,
    memberships: set[tuple[str, int]] | None = None,
) -> str | None:
    """Try to match mr_device to one HA device.

    Returns the matched ha_device_id or None.

    ``memberships`` - set of (fabric_id_lower_hex, node_id_int) pairs
    for mr_device, pre-computed by the caller from DeviceFabricMembership.
    Pass None or empty set to skip Key 3.
    """
    if not ha_devices:
        return None

    # ── Key 1: (vendor_id, product_id, serial) triple ────────────────────────
    if mr_device.vendor_id and mr_device.product_id and mr_device.serial:
        mr_serial = mr_device.serial
        if not _is_placeholder(mr_serial):
            matches = [
                d
                for d in ha_devices
                if d.get("serial") and d["serial"].lower() == mr_serial.lower()
            ]
            if len(matches) == 1:
                return matches[0]["id"]

    # ── Key 2: matter_unique_id match ─────────────────────────────────────────
    if mr_device.matter_unique_id:
        matches = [
            d
            for d in ha_devices
            if mr_device.matter_unique_id in d.get("matter_uid_set", frozenset())
        ]
        if len(matches) == 1:
            return matches[0]["id"]

    # ── Key 3: (fabric_id, node_id) structural match ──────────────────────────
    if memberships:
        matches = [
            d
            for d in ha_devices
            if d.get("fabric_id") is not None
            and d.get("node_id") is not None
            and (d["fabric_id"], d["node_id"]) in memberships
        ]
        if len(matches) == 1:
            return matches[0]["id"]

    return None
