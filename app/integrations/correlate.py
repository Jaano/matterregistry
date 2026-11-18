"""
Shared network-identity correlation: match a discovered record to a Device by
hardware MAC / IPv4 / IPv6 address.

Identity keys like ``matter_unique_id``, ``(vid,pid,serial)`` and
``homekit_accessory_id`` are stronger and are matched directly by each
integration.  This helper covers the *network-address* tier - useful when an
integration only knows a device by the addresses it advertises (mDNS / LTPDU).

Build an :class:`AddressIndex` once per projection run, then call
:meth:`AddressIndex.match` per discovered record.  The index harvests every
persisted address source:

  - ``Device.mac_address``                         (Matter-set colon-hex)
  - ``MatterNodeRecord`` IPv6 + MAC, joined to a Device via
    ``DeviceFabricMembership.node_id``
  - ``DeviceIntegrationData`` payloads carrying top-level ``mac_address`` /
    ``thread_ext_address`` / ``ipv4_addresses`` / ``ipv6_addresses`` (mDNS
    persists the addresses; the OTBR "locate" action persists the Thread
    extended address)

Addresses that map to more than one Device are marked *ambiguous* and never
produce a match - correctness over recall.  Match priority is MAC (a globally
unique hardware id) → IPv6 → IPv4 (most reassignable, weakest).
"""

from __future__ import annotations

import ipaddress
import json
import re
from collections.abc import Iterable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlmodel import Session

# Sentinel marking an address that resolves to multiple devices.
_AMBIGUOUS = object()

_HEX_RE = re.compile(r"^[0-9A-F]+$")


def normalize_mac(mac: str) -> str | None:
    """Uppercase colon-hex byte pairs from any MAC / ext-address form.

    Accepts ``8e:db:88:a9:5d:5d:f1:b4``, ``8EDB88A95D5DF1B4``,
    ``8e-db-88-…`` - returns ``8E:DB:88:A9:5D:5D:F1:B4``.  Returns None when
    the input is not an even-length hex string (so callers can skip it).
    """
    s = mac.strip().upper().replace(":", "").replace("-", "")
    if not s or len(s) % 2 != 0 or not _HEX_RE.match(s):
        return None
    return ":".join(s[i : i + 2] for i in range(0, len(s), 2))


def normalize_ip(ip: str) -> str | None:
    """Canonicalize an IPv4/IPv6 address (collapse zeros, drop zone id).

    Returns None for unparseable input.
    """
    raw = ip.strip().split("%", 1)[0]  # strip IPv6 zone id (fe80::1%eth0)
    try:
        return str(ipaddress.ip_address(raw))
    except ValueError:
        return None


class AddressIndex:
    """Reverse index of device network addresses → device id.

    Built once from all persisted address sources; query with :meth:`match`.
    """

    def __init__(self, session: Session) -> None:
        self._by_mac: dict[str, object] = {}
        self._by_ip: dict[str, object] = {}
        self._build(session)

    # ── construction ──────────────────────────────────────────────────────────

    def _add_mac(self, mac: str | None, device_id: str) -> None:
        norm = normalize_mac(mac) if mac else None
        if norm:
            self._add(self._by_mac, norm, device_id)

    def _add_ip(self, ip: str | None, device_id: str) -> None:
        norm = normalize_ip(ip) if ip else None
        if norm:
            self._add(self._by_ip, norm, device_id)

    @staticmethod
    def _add(table: dict[str, object], key: str, device_id: str) -> None:
        existing = table.get(key)
        if existing is None:
            table[key] = device_id
        elif existing is not device_id and existing is not _AMBIGUOUS:
            # Same address seen on a different device → unusable for matching.
            table[key] = _AMBIGUOUS

    def _build(self, session: Session) -> None:
        from sqlmodel import select

        from ..models import (
            Device,
            DeviceFabricMembership,
            DeviceIntegrationData,
            MatterNodeRecord,
        )

        # 1. Device.mac_address (the one network field on Device itself).
        for dev in session.exec(select(Device)).all():
            self._add_mac(dev.mac_address, dev.id)

        # 2. Matter node IPv6 + MAC, joined to a Device via fabric membership.
        node_to_device: dict[int, str] = {}
        for mem in session.exec(select(DeviceFabricMembership)).all():
            node_to_device.setdefault(mem.node_id, mem.device_id)
        for rec in session.exec(select(MatterNodeRecord)).all():
            device_id = node_to_device.get(rec.node_id)
            if not device_id:
                continue
            self._add_mac(rec.mac_address, device_id)
            try:
                for ip in json.loads(rec.ip_addresses_json or "[]"):
                    self._add_ip(ip, device_id)
            except (ValueError, TypeError):
                pass

        # 3. Integration-data payloads with persisted addresses (mDNS, B.12).
        # Rows left behind by a deleted Device are skipped: their addresses would
        # otherwise look shared by two devices and block every match.
        live_ids = set(session.exec(select(Device.id)).all())
        for row in session.exec(select(DeviceIntegrationData)).all():
            if row.device_id not in live_ids:
                continue
            try:
                payload = json.loads(row.payload_json)
            except (ValueError, TypeError):
                continue
            if not isinstance(payload, dict):
                continue
            self._add_mac(payload.get("mac_address"), row.device_id)
            self._add_mac(payload.get("thread_ext_address"), row.device_id)
            for ip in payload.get("ipv4_addresses") or []:
                self._add_ip(ip, row.device_id)
            for ip in payload.get("ipv6_addresses") or []:
                self._add_ip(ip, row.device_id)

    # ── query ───────────────────────────────────────────────────────────────

    def match(
        self,
        *,
        macs: Iterable[str] = (),
        ipv6s: Iterable[str] = (),
        ipv4s: Iterable[str] = (),
    ) -> str | None:
        """Return a uniquely-matching device id, or None.

        Priority: MAC → IPv6 → IPv4.  Ambiguous addresses (shared by multiple
        devices) are skipped rather than returning an arbitrary device.
        """
        for mac in macs:
            norm = normalize_mac(mac)
            if norm:
                hit = self._by_mac.get(norm)
                if isinstance(hit, str):
                    return hit
        for ip in list(ipv6s) + list(ipv4s):
            norm = normalize_ip(ip)
            if norm:
                hit = self._by_ip.get(norm)
                if isinstance(hit, str):
                    return hit
        return None

    def is_ambiguous(
        self,
        *,
        macs: Iterable[str] = (),
        ipv6s: Iterable[str] = (),
        ipv4s: Iterable[str] = (),
    ) -> bool:
        """True if any supplied address is known but shared by multiple devices.

        Use this after :meth:`match` returns None to distinguish "never seen
        this address" (safe to create a new device) from "address seen on two+
        devices" (ambiguous data - skip, don't create a third device).
        """
        for mac in macs:
            norm = normalize_mac(mac)
            if norm and self._by_mac.get(norm) is _AMBIGUOUS:
                return True
        for ip in list(ipv6s) + list(ipv4s):
            norm = normalize_ip(ip)
            if norm and self._by_ip.get(norm) is _AMBIGUOUS:
                return True
        return False


def device_ipv6_addresses(session: Session, device_id: str) -> list[str]:
    """Every persisted IPv6 address of one Device, canonicalized, in stable order.

    Same sources as :class:`AddressIndex` (integration-data payloads and the
    Matter node record joined through fabric membership), but for a single
    Device and without the cross-device ambiguity filter.
    """
    from sqlmodel import select

    from ..models import DeviceFabricMembership, DeviceIntegrationData, MatterNodeRecord

    found: dict[str, None] = {}

    def _take(ip: object) -> None:
        norm = normalize_ip(ip) if isinstance(ip, str) else None
        if norm:
            found.setdefault(norm)

    for row in session.exec(
        select(DeviceIntegrationData).where(DeviceIntegrationData.device_id == device_id)  # type: ignore[arg-type]
    ).all():
        try:
            payload = json.loads(row.payload_json)
        except (ValueError, TypeError):
            continue
        if isinstance(payload, dict):
            for ip in payload.get("ipv6_addresses") or []:
                _take(ip)

    node_ids = [
        mem.node_id
        for mem in session.exec(
            select(DeviceFabricMembership).where(DeviceFabricMembership.device_id == device_id)  # type: ignore[arg-type]
        ).all()
    ]
    for node_id in node_ids:
        for rec in session.exec(
            select(MatterNodeRecord).where(MatterNodeRecord.node_id == node_id)  # type: ignore[arg-type]
        ).all():
            try:
                for ip in json.loads(rec.ip_addresses_json or "[]"):
                    _take(ip)
            except (ValueError, TypeError):
                pass
    return [ip for ip in found if ipaddress.ip_address(ip).version == 6]


def homekit_accessory_id_free(session: Session, accessory_id: str, device_id: str) -> bool:
    """True when no *other* Device already stores *accessory_id*.

    The id is the shared HomeKit identity key, so a sync that learns a new id
    for a Device (the accessory was re-paired and its HAP Device ID changed)
    only adopts it when that cannot make two Devices claim the same accessory.
    """
    from sqlmodel import select

    from ..models import Device

    return (
        session.exec(
            select(Device.id).where(
                Device.homekit_accessory_id == accessory_id,  # type: ignore[arg-type]
                Device.id != device_id,  # type: ignore[arg-type]
            )
        ).first()
        is None
    )
