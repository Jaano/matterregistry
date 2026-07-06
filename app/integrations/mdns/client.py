"""
mDNS HomeKit + Thread-LTPDU + Matter operational discovery client (D.3/D.5/D.6).

Browses `_hap._tcp.local.`, `_hap._udp.local.` (HomeKit),
`_ltpdu._udp.local.` (Thread LTPDU accessories), and `_matter._tcp.local.`
(operational Matter nodes) and keeps in-memory sets of discovered services
(event-driven, like the Matter Server client).

HAP projection: auto-creates / correlates ``homekit`` Device rows, deduped on
the HAP accessory id and linked to stored onboarding codes via the setup hash.

LTPDU projection (D.5): enriches Matter / HomeKit devices with Thread firmware
and network data.  Unmatched records create protocol-unlabeled Device rows
(``protocol=None``, a first-class supported state).  LTPDU correlation order:
  1. existing ``DeviceLink(integration="mdns", external_id=eui64)`` (idempotent)
  2. ``id_is_hap`` and ``Device.homekit_accessory_id == id`` (NL45 HomeKit)
  3. ``thread_ext_addr`` colon-hex == ``Device.mac_address``, or mesh-local IPv6
     match against ``MatterNodeRecord.ip_addresses_json`` (NL67 Matter)
  4. no match → create unlabeled Device keyed by ``eui64``

Matter operational projection (D.6): enriches existing Matter devices with
operational networking (CASE port, IPv4/IPv6, session parameters).  Correlation
is deterministic - the instance name encodes ``{compressedFabricHex}-{nodeHex}``,
matched against ``Device.matter_unique_id``.  Unmatched records are dropped;
no new devices are created (a bare fabric+node tuple carries no vendor/product
info useful enough to create a device from).

HAP TXT record fields exposed: friendly name, ``id`` (HAP Device ID), ``md``
(model), ``ci`` (category), ``sf`` (paired flag), ``sh`` (setup hash).
LTPDU TXT record fields: ``eui64``, ``id``, ``srcvers``→firmware, ``xp``,
``md``→model; DNS-SD layer provides hostname (Thread operational ext addr) and
mesh-local IPv6.
Matter TXT record fields: ``SII`` (session idle interval ms), ``SAI`` (session
active interval ms), ``SAT`` (session active threshold); DNS-SD layer provides
CASE port and operational IPv4/IPv6.

BLE-only accessories do not advertise mDNS and are never seen here.
"""

from __future__ import annotations

import asyncio
import logging
import re
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from ...homekit import setup_hash
from ...models import DeviceProtocol, FieldSource
from ..base import Integration, SyncResult

if TYPE_CHECKING:
    from sqlmodel import Session

logger = logging.getLogger(__name__)

_HAP_TCP = "_hap._tcp.local."
_HAP_UDP = "_hap._udp.local."
_LTPDU = "_ltpdu._udp.local."
_MATTER_TCP = "_matter._tcp.local."

_HAP_TYPES = (_HAP_TCP, _HAP_UDP)
_ALL_TYPES = (_HAP_TCP, _HAP_UDP, _LTPDU, _MATTER_TCP)

# EUI-64 is always 16 hex chars without separators.
_EUI64_RE = re.compile(r"^[0-9A-Fa-f]{16}$")
# HAP accessory id in MAC form (6 colon-separated byte pairs, uppercase).
_HAP_ID_RE = re.compile(r"^([0-9A-F]{2}:){5}[0-9A-F]{2}$")
# _matter._tcp instance label: {compressedFabricHex}-{nodeHex} (both 16 hex chars).
_MATTER_INSTANCE_RE = re.compile(r"^([0-9A-Fa-f]{16})-([0-9A-Fa-f]{16})$")
# Matches the compressed fabric + node hex embedded in Device.matter_unique_id.
_MATTER_UID_RE = re.compile(r"deviceid_([0-9A-Fa-f]{16})-([0-9A-Fa-f]{16})-")


class ClientStatus(StrEnum):
    disabled = "disabled"
    browsing = "browsing"
    error = "error"


def _txt_str(properties: dict, key: str) -> str | None:
    """Read a TXT key from a zeroconf properties dict (bytes keys/values)."""
    raw = properties.get(key.encode()) if properties else None
    if raw is None:
        return None
    if isinstance(raw, bytes):
        try:
            return raw.decode()
        except UnicodeDecodeError:
            return None
    return str(raw)


def _colon_hex(s: str) -> str:
    """Normalize a run-together hex string to colon-separated uppercase byte pairs.

    ``8EDB88A95D5DF1B4`` → ``8E:DB:88:A9:5D:5D:F1:B4``
    """
    s = s.upper().replace(":", "")
    return ":".join(s[i : i + 2] for i in range(0, len(s), 2))


def parse_hap_service(name: str, service_type: str, properties: dict) -> dict | None:
    """Parse a HAP mDNS service into a discovered-accessory dict.

    *name* is the full service instance name (e.g.
    ``Presence-Sensor-FP2-7DD0._hap._tcp.local.``), *service_type* is
    ``_hap._tcp.local.`` or ``_hap._udp.local.``, *properties* is the TXT-record
    dict from zeroconf (bytes keys/values).

    Returns a dict keyed by the HAP accessory id, or None if no usable ``id``.
    """
    device_id = _txt_str(properties, "id")
    if not device_id:
        return None
    device_id = device_id.upper()

    # Friendly name = instance label minus the trailing ".<service_type>".
    friendly = name
    suffix = "." + service_type
    if friendly.endswith(suffix):
        friendly = friendly[: -len(suffix)]
    friendly = friendly.replace("\\032", " ").strip()

    category_id: int | None = None
    ci = _txt_str(properties, "ci")
    if ci is not None:
        try:
            category_id = int(ci)
        except ValueError:
            category_id = None

    # Status flags bit 0: 1 = unpaired/discoverable, 0 = paired.
    paired: bool | None = None
    sf = _txt_str(properties, "sf")
    if sf is not None:
        try:
            paired = (int(sf) & 0x1) == 0
        except ValueError:
            paired = None

    transport = "thread" if service_type == _HAP_UDP else "wifi"

    return {
        "id": device_id,
        "name": friendly or None,
        "model": _txt_str(properties, "md") or None,
        "category_id": category_id,
        "paired": paired,
        "setup_hash": _txt_str(properties, "sh"),
        "transport": transport,
    }


def parse_ltpdu_service(
    name: str,
    properties: dict,
    server: str | None,
    addresses: list[str],
) -> dict | None:
    """Parse a _ltpdu._udp service record per Algorithm 1 (D.5).

    Returns a dict keyed by ``eui64``, or None if eui64 is absent/malformed.
    """
    eui64_raw = _txt_str(properties, "eui64")
    if not eui64_raw or not _EUI64_RE.match(eui64_raw):
        return None
    eui64 = eui64_raw.upper()

    raw_id = _txt_str(properties, "id")
    if raw_id is not None:
        raw_id = raw_id.strip("\x00").upper()

    id_is_hap = bool(_HAP_ID_RE.match(raw_id or ""))

    firmware = _txt_str(properties, "srcvers")
    thread_ext_pan_id = _txt_str(properties, "xp")
    model = _txt_str(properties, "md")

    # Thread operational extended address from hostname (no trailing dot).
    thread_ext_addr: str | None = None
    if server:
        host = server.rstrip(".")
        if re.fullmatch(r"[0-9A-Fa-f]{16}", host):
            thread_ext_addr = _colon_hex(host)

    # Extract mesh-local (ULA, fd…) IPv6 addresses.
    mesh_local_ipv6 = [a for a in addresses if a.lower().startswith("fd")]

    # Instance label (friendly name) - the part before the first dot.
    instance_label = name.split(".")[0].replace("\\032", " ").strip()

    return {
        "eui64": eui64,
        "id": raw_id,
        "id_is_hap": id_is_hap,
        "firmware": firmware,
        "thread_ext_pan_id": thread_ext_pan_id,
        "model": model,
        "thread_ext_addr": thread_ext_addr,
        "mesh_local_ipv6": mesh_local_ipv6,
        "_instance_label": instance_label,
    }


def parse_matter_service(
    name: str,
    properties: dict,
    server: str | None,
    addresses: list[str],
) -> dict | None:
    """Parse a _matter._tcp service record per D.6.

    Instance name format: ``{compressedFabricHex}-{nodeHex}._matter._tcp.local.``
    Returns a dict keyed by ``(compressed_fabric_hex, node_id)``, or None if the
    label doesn't match the expected format.
    """
    label = name.split(".")[0]
    m = _MATTER_INSTANCE_RE.match(label)
    if not m:
        return None
    compressed_fabric_hex = m.group(1).upper()
    node_id = int(m.group(2), 16)
    instance_name = label.upper()

    sii_raw = _txt_str(properties, "SII")
    sai_raw = _txt_str(properties, "SAI")
    sat_raw = _txt_str(properties, "SAT")

    ipv4s = [a for a in addresses if "." in a]
    ipv6s = [a for a in addresses if ":" in a]

    return {
        "compressed_fabric_hex": compressed_fabric_hex,
        "node_id": node_id,
        "instance_name": instance_name,
        "ipv4_addresses": ipv4s,
        "ipv6_addresses": ipv6s,
        "session_idle_ms": int(sii_raw) if sii_raw else None,
        "session_active_ms": int(sai_raw) if sai_raw else None,
        "session_active_threshold": int(sat_raw) if sat_raw else None,
    }


def project_discovered(
    session: Session,
    discovered: list[dict],
    *,
    integration: Integration,
) -> dict:
    """Create / correlate ``homekit`` Device rows for discovered accessories.

    Correlation (deterministic only), in order:
      1. existing ``DeviceLink(integration="mdns", external_id=id)``;
      2. ``Device.homekit_accessory_id == id`` (shared key with the HA import);
      3. setup-hash: a homekit Device with a stored Setup ID whose
         ``setup_hash(setupID, id)`` equals the advertised ``sh``.
    No match → create a new homekit Device. Idempotent across re-syncs.
    """
    from datetime import UTC, datetime

    from sqlmodel import select

    from ...audit import log as audit_log
    from ...models import Device, DeviceLink, DeviceLinkSource, Property, PropertyType
    from ...services import set_field
    from ..data import read as read_integration_data
    from ..data import upsert as upsert_integration_data

    now = datetime.now(UTC)

    created = 0
    updated = 0
    skipped = 0

    # Existing mdns links keyed by external_id (the accessory id).
    mdns_links: dict[str, DeviceLink] = {
        lnk.external_id: lnk
        for lnk in session.exec(
            select(DeviceLink).where(DeviceLink.integration == "mdns")  # type: ignore[attr-defined]
        ).all()
    }

    # Stored Setup IDs per homekit device, for setup-hash correlation.
    hk_setup_ids: list[tuple[str, str]] = []  # (device_id, setup_id)
    disc_rows = session.exec(
        select(Property).where(Property.type == PropertyType.discriminator)
    ).all()
    for row in disc_rows:
        dev = session.get(Device, row.device_id)
        if dev is not None and dev.protocol == DeviceProtocol.homekit and row.value:
            hk_setup_ids.append((row.device_id, row.value))

    for acc in discovered:
        acc_id = acc.get("id")
        if not acc_id:
            skipped += 1
            continue

        device: Device | None = None

        # 1. existing mdns link
        link = mdns_links.get(acc_id)
        if link:
            device = session.get(Device, link.device_id)

        # 2. shared homekit identity key
        if device is None:
            device = session.exec(
                select(Device).where(Device.homekit_accessory_id == acc_id)  # type: ignore[arg-type]
            ).first()

        # 3. setup-hash → stored onboarding code
        if device is None and acc.get("setup_hash"):
            for dev_id, setup_id in hk_setup_ids:
                if setup_hash(setup_id, acc_id) == acc["setup_hash"]:
                    device = session.get(Device, dev_id)
                    break

        created_new = False
        if device is None:
            if not integration.can_create_devices:
                skipped += 1
                continue
            device = Device(
                name=acc.get("name") or f"HomeKit {acc.get('model') or acc_id}",
                name_source=FieldSource.mdns,
                product=acc.get("model") or None,
                product_source=FieldSource.mdns if acc.get("model") else FieldSource.generated,
                protocol=DeviceProtocol.homekit,
                homekit_accessory_id=acc_id,
                network_type=[acc["transport"]] if acc.get("transport") else [],
                network_type_source=FieldSource.mdns
                if acc.get("transport")
                else FieldSource.generated,
                created_at=now,
                updated_at=now,
            )
            session.add(device)
            session.flush()
            created_new = True
            created += 1

        if not created_new:
            changed = False
            if acc_id and not device.homekit_accessory_id:
                device.homekit_accessory_id = acc_id
                changed = True
            # Add-only transport enrichment (never removes; B.5 pattern).
            transport = acc.get("transport")
            if transport and transport not in (device.network_type or []):
                device.network_type = sorted([*(device.network_type or []), transport])
                changed = True
            # Low-priority name/product fill (won't clobber ha/user/scanned).
            changed |= set_field(device, "name", acc.get("name") or None, FieldSource.mdns)
            changed |= set_field(device, "product", acc.get("model") or None, FieldSource.mdns)
            if changed:
                device.updated_at = now
                session.add(device)
                updated += 1

        # Create / refresh the mdns DeviceLink.
        # Also fall back to a device-id lookup in case the external_id changed
        # (e.g. re-discovered under a different HAP id) or a link was created
        # earlier in this same sync run.
        if link is None:
            link = session.exec(
                select(DeviceLink).where(
                    DeviceLink.device_id == device.id,  # type: ignore[attr-defined]
                    DeviceLink.integration == "mdns",  # type: ignore[attr-defined]
                )
            ).first()

        if link is None:
            session.add(
                DeviceLink(
                    device_id=device.id,
                    integration="mdns",
                    external_id=acc_id,
                    link_source=DeviceLinkSource.auto,
                    linked_at=now,
                )
            )
            audit_log(
                session,
                action="mdns.discover",
                entity=f"device:{device.id}",
                reason=f"accessory_id:{acc_id}",
            )
        else:
            if link.external_id != acc_id:
                link.external_id = acc_id
            link.linked_at = now
            session.add(link)

        # ── B.12: persist what mDNS discovered for this device ──
        # Top-level ipv4_addresses / ipv6_addresses feed the shared AddressIndex
        # so HAP-over-IP devices become matchable by address on later syncs.
        existing = (
            read_integration_data(session, device_id=device.id, integration=integration.slug) or {}
        )
        upsert_integration_data(
            session,
            device_id=device.id,
            integration=integration.slug,
            payload={
                **{
                    k: v
                    for k, v in existing.items()
                    if k
                    not in (
                        "source",
                        "name",
                        "accessory_id",
                        "model",
                        "category_id",
                        "paired",
                        "transport",
                    )
                },
                "hap_accessory_id": acc_id,
                "hap_instance_name": acc.get("name"),
                "hap_model": acc.get("model"),
                "hap_category_id": acc.get("category_id"),
                "hap_paired": acc.get("paired"),
                "hap_transport": acc.get("transport"),
                "ipv4_addresses": acc.get("ipv4_addresses") or [],
                "ipv6_addresses": acc.get("ipv6_addresses") or [],
            },
        )

    integration.assert_capabilities(session, created=created)
    audit_log(session, action="mdns.sync", entity="mdns", reason="mdns.project")
    session.commit()
    return {"created": created, "updated": updated, "skipped": skipped}


def project_ltpdu(
    session: Session,
    ltpdu_records: list[dict],
    *,
    integration: Integration,
) -> dict:
    """Correlate LTPDU records to Device rows and enrich them (D.5, Algorithm 2).

    Correlation order (first match wins):
      1. existing DeviceLink(integration="mdns", external_id=eui64) - idempotent
      2. id_is_hap + Device.homekit_accessory_id == id  (NL45)
      3. network-address match (shared AddressIndex): thread_ext_addr against any
         device MAC, then mesh-local IPv6 against any device IP  (NL67)
      4. no match → create unlabeled Device (protocol=None)
    """
    from datetime import UTC, datetime

    from sqlmodel import select

    from ...audit import log as audit_log
    from ...models import Device, DeviceLink, DeviceLinkSource
    from ...services import set_field
    from ..correlate import AddressIndex
    from ..data import read as read_integration_data
    from ..data import upsert as upsert_integration_data

    now = datetime.now(UTC)

    created = 0
    updated = 0
    skipped = 0

    # Pre-load all mdns links.  eui64s are 16-char hex strings without colons,
    # while HAP acc_ids are MAC-form (AA:BB:…).  We key only by eui64 here for
    # idempotency step 1 - links with acc_id keys are ignored.
    mdns_links_by_eui64: dict[str, DeviceLink] = {
        lnk.external_id: lnk
        for lnk in session.exec(
            select(DeviceLink).where(DeviceLink.integration == "mdns")  # type: ignore[attr-defined]
        ).all()
        if _EUI64_RE.match(lnk.external_id)
    }

    # Shared MAC/IPv4/IPv6 index over every persisted address source (joins
    # Matter nodes to devices via DeviceFabricMembership - the real node link).
    addr_index = AddressIndex(session)

    for rec in ltpdu_records:
        eui64 = rec.get("eui64")
        if not eui64:
            skipped += 1
            continue

        device: Device | None = None
        link: DeviceLink | None = None

        # 1. Idempotency: existing mdns DeviceLink keyed by eui64.
        link = mdns_links_by_eui64.get(eui64)
        if link:
            device = session.get(Device, link.device_id)

        # 2. HomeKit: id_is_hap and Device.homekit_accessory_id matches.
        if device is None and rec.get("id_is_hap") and rec.get("id"):
            device = session.exec(
                select(Device).where(Device.homekit_accessory_id == rec["id"])  # type: ignore[arg-type]
            ).first()

        # 3. Network-address match: Thread ext addr as MAC, mesh-local as IPv6.
        if device is None:
            macs = [rec["thread_ext_addr"]] if rec.get("thread_ext_addr") else []
            ipv6s = rec.get("mesh_local_ipv6") or []
            dev_id = addr_index.match(macs=macs, ipv6s=ipv6s)
            if dev_id:
                device = session.get(Device, dev_id)
            elif addr_index.is_ambiguous(macs=macs, ipv6s=ipv6s):
                # Address is known but shared by multiple devices - don't create
                # a third one.  Log and skip until the duplicates are merged.
                logger.warning(
                    "LTPDU eui64=%s: ambiguous address (shared by 2+ devices), skipping",
                    eui64,
                )
                skipped += 1
                continue

        # 4. No match → create unlabeled device (protocol=None).
        created_new = False
        if device is None:
            if not integration.can_create_devices:
                skipped += 1
                continue
            instance_label = rec.get("_instance_label") or eui64
            device = Device(
                name=instance_label,
                name_source=FieldSource.mdns,
                product=rec.get("model") or None,
                product_source=FieldSource.mdns if rec.get("model") else FieldSource.generated,
                protocol=None,
                network_type=["thread"],
                network_type_source=FieldSource.mdns,
                created_at=now,
                updated_at=now,
            )
            session.add(device)
            session.flush()
            created_new = True
            created += 1

        if not created_new:
            changed = False
            # Add-only Thread transport enrichment (B.5 pattern).
            if "thread" not in (device.network_type or []):
                device.network_type = sorted([*(device.network_type or []), "thread"])
                changed = True
            changed |= set_field(device, "product", rec.get("model") or None, FieldSource.mdns)
            if rec.get("firmware"):
                changed |= set_field(device, "firmware_version", rec["firmware"], FieldSource.mdns)
            if changed:
                device.updated_at = now
                session.add(device)
                updated += 1

        # Create / refresh DeviceLink keyed by eui64.  For NL45 devices that
        # already have an mdns link keyed by HAP acc_id, we skip re-keying
        # (the HAP link is the anchor) and just refresh linked_at.
        if link is None:
            link = session.exec(
                select(DeviceLink).where(
                    DeviceLink.device_id == device.id,  # type: ignore[attr-defined]
                    DeviceLink.integration == "mdns",  # type: ignore[attr-defined]
                )
            ).first()

        if link is None:
            session.add(
                DeviceLink(
                    device_id=device.id,
                    integration="mdns",
                    external_id=eui64,
                    link_source=DeviceLinkSource.auto,
                    linked_at=now,
                )
            )
            audit_log(
                session,
                action="mdns.ltpdu_discover",
                entity=f"device:{device.id}",
                reason=f"eui64:{eui64}",
            )
        else:
            link.linked_at = now
            session.add(link)

        # Merge LTPDU data into the existing mdns integration-data payload so
        # HAP fields (paired, accessory_id, …) are preserved for NL45 devices.
        # Top-level mac_address / ipv6_addresses feed the shared AddressIndex on
        # subsequent syncs; the nested "ltpdu" block carries the detail for display.
        existing = (
            read_integration_data(session, device_id=device.id, integration=integration.slug) or {}
        )
        merged_ipv6 = sorted(
            set(existing.get("ipv6_addresses") or []) | set(rec.get("mesh_local_ipv6") or [])
        )
        upsert_integration_data(
            session,
            device_id=device.id,
            integration=integration.slug,
            payload={
                **{
                    k: v
                    for k, v in existing.items()
                    if k
                    not in (
                        "ltpdu",
                        "source",
                        "instance_name",
                        "eui64",
                        "firmware",
                        "thread_ext_pan_id",
                    )
                },
                "mac_address": existing.get("mac_address") or rec.get("thread_ext_addr"),
                "ipv6_addresses": merged_ipv6,
                "ltpdu_instance_name": rec.get("_instance_label"),
                "ltpdu_eui64": eui64,
                "ltpdu_firmware": rec.get("firmware"),
                "ltpdu_thread_ext_pan_id": rec.get("thread_ext_pan_id"),
            },
        )

    integration.assert_capabilities(session, created=created)
    audit_log(session, action="mdns.ltpdu_sync", entity="mdns", reason="mdns.project_ltpdu")
    session.commit()
    return {"created": created, "updated": updated, "skipped": skipped}


def project_matter(
    session: Session,
    matter_records: list[dict],
    *,
    integration: Integration,
) -> dict:
    """Enrich Matter devices from operational _matter._tcp records (D.6).

    Correlation: parse ``{compressedFabricHex}-{nodeHex}`` from the instance
    name and match against ``Device.matter_unique_id`` (which embeds the same
    compressed fabric id and node id).  Deterministic, no address guessing.
    Unmatched records are dropped - no new devices are created.
    """
    from sqlmodel import select

    from ...audit import log as audit_log
    from ...models import Device
    from ..data import read as read_integration_data
    from ..data import upsert as upsert_integration_data

    updated = 0
    skipped = 0

    # Build (compressed_fabric_hex_upper, node_id) → device_id from matter_unique_id.
    uid_index: dict[tuple[str, int], str] = {}
    for dev in session.exec(select(Device)).all():
        if dev.matter_unique_id:
            m = _MATTER_UID_RE.match(dev.matter_unique_id)
            if m:
                uid_index[(m.group(1).upper(), int(m.group(2), 16))] = dev.id

    for rec in matter_records:
        key = (rec["compressed_fabric_hex"], rec["node_id"])
        device_id = uid_index.get(key)
        if not device_id:
            skipped += 1
            continue

        existing = (
            read_integration_data(session, device_id=device_id, integration=integration.slug) or {}
        )
        merged_ipv6 = sorted(
            set(existing.get("ipv6_addresses") or []) | set(rec.get("ipv6_addresses") or [])
        )
        merged_ipv4 = sorted(
            set(existing.get("ipv4_addresses") or []) | set(rec.get("ipv4_addresses") or [])
        )
        upsert_integration_data(
            session,
            device_id=device_id,
            integration=integration.slug,
            payload={
                **{
                    k: v
                    for k, v in existing.items()
                    if k
                    not in (
                        "instance_name",
                        "node_id",
                        "operational_port",
                        "session_idle_ms",
                        "session_active_ms",
                        "session_active_threshold",
                    )
                },
                "ipv4_addresses": merged_ipv4,
                "ipv6_addresses": merged_ipv6,
                "matter_instance_name": rec.get("instance_name"),
                "matter_node_id": rec["node_id"],
                "matter_operational_port": rec.get("port"),
                "matter_session_idle_ms": rec.get("session_idle_ms"),
                "matter_session_active_ms": rec.get("session_active_ms"),
                "matter_session_active_threshold": rec.get("session_active_threshold"),
            },
        )
        updated += 1

    audit_log(session, action="mdns.matter_sync", entity="mdns", reason="mdns.project_matter")
    session.commit()
    return {"created": 0, "updated": updated, "skipped": skipped}


class MdnsClient(Integration):
    """Passive mDNS browser for HAP (HomeKit), Thread-LTPDU, and Matter operational."""

    slug = "mdns"
    short_name = "mdns"
    long_name = "mDNS Discovery"
    icon = "icon-mdns"
    can_create_devices = True
    can_update_devices = True
    can_update_status = False
    can_act_externally = False
    supported_protocols = frozenset({DeviceProtocol.homekit, DeviceProtocol.matter})

    def __init__(self) -> None:
        self._status = ClientStatus.disabled
        self._error_msg: str | None = None
        self._azc: Any = None
        self._browsers: list[Any] = []
        # HAP accessories keyed by HAP id; each value is a parse dict
        # plus a "_service_name" used for removal.
        self._discovered: dict[str, dict] = {}
        self._name_to_id: dict[str, str] = {}
        # LTPDU accessories keyed by eui64.
        self._ltpdu_discovered: dict[str, dict] = {}
        # Matter operational records keyed by (compressed_fabric_hex, node_id).
        self._matter_discovered: dict[tuple[str, int], dict] = {}

    @property
    def status(self) -> ClientStatus:
        return self._status

    @property
    def error_message(self) -> str | None:
        return self._error_msg

    async def start(self) -> None:
        from zeroconf.asyncio import AsyncServiceBrowser, AsyncZeroconf

        try:
            self._azc = AsyncZeroconf()
            self._browsers = [
                AsyncServiceBrowser(
                    self._azc.zeroconf, st, handlers=[self._on_service_state_change]
                )
                for st in _HAP_TYPES
            ] + [
                AsyncServiceBrowser(
                    self._azc.zeroconf, _LTPDU, handlers=[self._on_ltpdu_state_change]
                ),
                AsyncServiceBrowser(
                    self._azc.zeroconf, _MATTER_TCP, handlers=[self._on_matter_state_change]
                ),
            ]
            self._status = ClientStatus.browsing
            logger.info(
                "mDNS discovery started (browsing %s, %s, %s, %s)",
                _HAP_TCP,
                _HAP_UDP,
                _LTPDU,
                _MATTER_TCP,
            )
        except Exception as exc:  # pragma: no cover - network/setup failure
            self._status = ClientStatus.error
            self._error_msg = str(exc)
            logger.exception("mDNS discovery failed to start")

    async def stop(self) -> None:
        for browser in self._browsers:
            try:
                await browser.async_cancel()
            except Exception:  # pragma: no cover
                pass
        self._browsers = []
        if self._azc is not None:
            try:
                await self._azc.async_close()
            except Exception:  # pragma: no cover
                pass
            self._azc = None
        self._status = ClientStatus.disabled

    def _on_service_state_change(
        self, zeroconf: Any, service_type: str, name: str, state_change: Any
    ) -> None:
        from zeroconf import ServiceStateChange

        if state_change is ServiceStateChange.Removed:
            acc_id = self._name_to_id.pop(name, None)
            if acc_id:
                self._discovered.pop(acc_id, None)
            return
        asyncio.ensure_future(self._resolve(service_type, name))

    async def _resolve(self, service_type: str, name: str) -> None:
        from zeroconf.asyncio import AsyncServiceInfo

        if self._azc is None:
            return
        info = AsyncServiceInfo(service_type, name)
        try:
            await info.async_request(self._azc.zeroconf, 3000)
        except Exception:  # pragma: no cover
            return
        parsed = parse_hap_service(name, service_type, info.properties or {})
        if parsed is None:
            return
        addresses = info.parsed_addresses()
        if addresses:
            parsed["ipv4_addresses"] = [a for a in addresses if "." in a]
            parsed["ipv6_addresses"] = [a for a in addresses if ":" in a]
        if info.server:
            parsed["hostname"] = info.server.rstrip(".")
        if info.port:
            parsed["port"] = info.port
        parsed["_service_name"] = name
        self._discovered[parsed["id"]] = parsed
        self._name_to_id[name] = parsed["id"]

    def _on_ltpdu_state_change(
        self, zeroconf: Any, service_type: str, name: str, state_change: Any
    ) -> None:
        from zeroconf import ServiceStateChange

        if state_change is ServiceStateChange.Removed:
            # Remove by instance name; eui64 is in the record but we'd need
            # to scan to find it.  Just drop any record whose _service_name matches.
            to_remove = [
                eui64
                for eui64, rec in self._ltpdu_discovered.items()
                if rec.get("_service_name") == name
            ]
            for eui64 in to_remove:
                self._ltpdu_discovered.pop(eui64, None)
            return
        asyncio.ensure_future(self._resolve_ltpdu(name))

    async def _resolve_ltpdu(self, name: str) -> None:
        from zeroconf.asyncio import AsyncServiceInfo

        if self._azc is None:
            return
        info = AsyncServiceInfo(_LTPDU, name)
        try:
            await info.async_request(self._azc.zeroconf, 3000)
        except Exception:  # pragma: no cover
            return
        addresses = info.parsed_addresses() if hasattr(info, "parsed_addresses") else []
        parsed = parse_ltpdu_service(name, info.properties or {}, info.server, addresses)
        if parsed is None:
            return
        parsed["port"] = info.port
        parsed["_service_name"] = name
        self._ltpdu_discovered[parsed["eui64"]] = parsed

    def discovered(self) -> list[dict]:
        """Snapshot of currently-discovered HAP accessories (for the API/UI)."""
        return [
            {k: v for k, v in acc.items() if not k.startswith("_")}
            for acc in self._discovered.values()
        ]

    def discovered_by_id(self, acc_id: str) -> dict | None:
        """Return the live discovered-accessory dict for a given HAP id, or None."""
        acc = self._discovered.get(acc_id)
        if acc is None:
            return None
        return {k: v for k, v in acc.items() if not k.startswith("_")}

    def ltpdu_discovered(self) -> list[dict]:
        """Snapshot of currently-discovered LTPDU accessories."""
        return [
            {k: v for k, v in rec.items() if not k.startswith("_")}
            for rec in self._ltpdu_discovered.values()
        ]

    def _on_matter_state_change(
        self, zeroconf: Any, service_type: str, name: str, state_change: Any
    ) -> None:
        from zeroconf import ServiceStateChange

        if state_change is ServiceStateChange.Removed:
            label = name.split(".")[0]
            m = _MATTER_INSTANCE_RE.match(label)
            if m:
                key = (m.group(1).upper(), int(m.group(2), 16))
                self._matter_discovered.pop(key, None)
            return
        asyncio.ensure_future(self._resolve_matter(name))

    async def _resolve_matter(self, name: str) -> None:
        from zeroconf.asyncio import AsyncServiceInfo

        if self._azc is None:
            return
        info = AsyncServiceInfo(_MATTER_TCP, name)
        try:
            await info.async_request(self._azc.zeroconf, 3000)
        except Exception:  # pragma: no cover
            return
        addresses = info.parsed_addresses() if hasattr(info, "parsed_addresses") else []
        parsed = parse_matter_service(name, info.properties or {}, info.server, addresses)
        if parsed is None:
            return
        parsed["port"] = info.port
        key = (parsed["compressed_fabric_hex"], parsed["node_id"])
        self._matter_discovered[key] = parsed

    def matter_discovered(self) -> list[dict]:
        """Snapshot of currently-discovered Matter operational nodes."""
        return list(self._matter_discovered.values())

    async def ingest(self) -> None:
        """No-op: the browser keeps a live cache; projection reads it directly."""
        return None

    def project(self, session: Any) -> SyncResult:
        from sqlmodel import Session

        from ...database import engine

        accessories = self.discovered()
        ltpdu_records = self.ltpdu_discovered()
        matter_records = self.matter_discovered()

        hap_result: dict = {"created": 0, "updated": 0, "skipped": 0}
        ltpdu_result: dict = {"created": 0, "updated": 0, "skipped": 0}
        matter_result: dict = {"created": 0, "updated": 0, "skipped": 0}

        with Session(engine) as db:
            hap_result = project_discovered(db, accessories, integration=self)

        if ltpdu_records:
            with Session(engine) as db:
                ltpdu_result = project_ltpdu(db, ltpdu_records, integration=self)

        if matter_records:
            with Session(engine) as db:
                matter_result = project_matter(db, matter_records, integration=self)

        return self._record_sync(
            SyncResult(
                created=hap_result["created"] + ltpdu_result["created"] + matter_result["created"],
                updated=hap_result["updated"] + ltpdu_result["updated"] + matter_result["updated"],
                skipped=hap_result["skipped"] + ltpdu_result["skipped"] + matter_result["skipped"],
            )
        )

    async def sync_now(self) -> SyncResult:
        """Project the live discovered cache (event-driven; no fresh fetch)."""
        return self.project(None)
