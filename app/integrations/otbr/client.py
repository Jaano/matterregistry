"""
OTBR REST client.

All OTBR-specific JSON shapes are confined to this module; the rest of the
app uses project-local dataclasses (ThreadNetworkSnapshot, RouterDiag, etc.).

Regular sync poll flow (server-only - does not touch the mesh):
  GET /node                  - node summary (ba_id, state, rloc16, ext_address …)
  GET /node/dataset/active   - full operational dataset (Accept: application/json)
  GET /node/coprocessor-version - NCP firmware version (optional; may 404)
  GET /api/diagnostics       - read the *already-collected* per-router telemetry cache

Diagnostic scan (active - wakes every router; explicit device action only, never sync):
  POST /api/actions          - trigger a broadcast diagnostic scan (0xfffe → all routers)
  GET /api/actions/{id}      - poll task status until "completed" or "failed"
  GET /api/diagnostics       - read the freshly populated per-router telemetry cache
  One router that does not answer fails the whole broadcast task and leaves no partial
  results, so a failed (or empty) broadcast falls back to the per-router unicast queries
  below, merged into the same cache. A scan that yields nothing raises instead of
  reporting success.

Locate action (active - unicasts one router at a time; manual or auto-locate):
  GET /node                  - the border router's own RLOC16
  POST /api/actions          - unicast diagnostic to the border router for its ``route``
                               TLV (lists every router id), then to each router in turn
                               until the device's stored IPv6 address appears in a reply
  GET /api/actions/{id}      - poll task status
  GET /api/diagnostics       - read the reply (extAddress + ipv6Addresses)
The Thread extended address of the matching router is persisted on the Device so the
Network page can name the node with no further traffic.

The background loop and every "Sync now" path do the server-only read above,
then may run the bounded auto-locate walk when enabled and needed. The active
broadcast scan runs solely via ``scan_diagnostics()`` behind a per-device button.
Background loop polls every 5 min when enabled.
Backoff on failure: 30s → 60s → 300s → 900s → 1800s (cap).
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
from collections.abc import AsyncGenerator, Sequence
from contextlib import aclosing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

import httpx

from ...models import DeviceProtocol
from ...thread import thread_role_label
from ..base import SyncResult
from ..polled import PermanentError, PolledIntegration

logger = logging.getLogger(__name__)

# Broadcast RLOC16 - reaches all routers in the Thread partition.
_DIAG_DEST = "0xfffe"
# Server-side scan timeout passed to OTBR; routers that don't answer within
# this window are simply absent from the results.
_DIAG_SCAN_TIMEOUT = 10  # seconds
# Unicast "locate" queries: only what is needed to identify one router.
_LOCATE_TYPES = ["extAddress", "rloc16", "ipv6Addresses"]
_ROUTE_TYPES = ["extAddress", "rloc16", "route"]
_UNICAST_TIMEOUT = 8  # server-side timeout per router (s)
_POLL_INTERVAL = 0.5  # task-status poll interval (s)
_UNICAST_GAP = 0.5  # pause between two routers (s) - never burst onto the mesh
_LOCATE_BUDGET = 75.0  # wall-clock cap for one locate run (s)
_SCAN_BUDGET = 150.0  # wall-clock cap for the per-router scan fallback (s)
_AUTO_LOCATE_COOLDOWN = 6 * 3600.0  # do not re-query an unresolved router sooner (s)
# TLV types requested from each router.
_DIAG_TYPES = [
    "rloc16",
    "extAddress",
    "mode",
    "connectivity",
    "route",
    "leaderData",
    "ipv6Addresses",
    "macCounters",
    "childTable",
    "routerNeighbors",
]


class ClientStatus(StrEnum):
    disabled = "disabled"
    connecting = "connecting"
    connected = "connected"
    disconnected = "disconnected"
    error = "error"


# ── OTBR-local dataclasses ────────────────────────────────────────────────────


@dataclass
class RouterNeighbor:
    rloc16: str
    ext_address: str
    link_margin: int | None
    average_rssi: int | None
    last_rssi: int | None
    connection_time: int | None
    frame_error_rate: float | None
    message_error_rate: float | None


@dataclass
class RouterChild:
    child_id: int
    timeout: int | None
    link_quality: int | None
    rx_on_when_idle: bool


@dataclass
class RouterDiag:
    ext_address: str
    rloc16: str  # hex string e.g. "0xe400"
    router_id: int
    router_neighbors: list[RouterNeighbor] = field(default_factory=list)
    child_table: list[RouterChild] = field(default_factory=list)


@dataclass
class ScanOutcome:
    """Result of one diagnostic scan: what answered and how it was gathered."""

    diagnostics: list[RouterDiag]
    method: str  # "broadcast" | "per-router"
    answered: int
    unreachable: int = 0

    @property
    def message(self) -> str:
        if self.method == "broadcast":
            return f"Thread diagnostics scan complete ({self.answered} routers answered)"
        return (
            "Thread diagnostics scan complete via per-router queries "
            f"({self.answered} answered, {self.unreachable} unreachable); "
            "the broadcast scan failed"
        )


@dataclass
class _WalkStats:
    """Counters for one walk over the routers (see ``_router_replies``)."""

    queried: int = 0
    failed: int = 0


@dataclass
class OTBRNodeInfo:
    ba_id: str | None
    state: str
    rloc_address: str | None
    ext_address: str
    network_name: str
    rloc16: str  # hex string e.g. "0x7000"
    router_id: int | None


@dataclass
class ThreadNetworkSnapshot:
    node_info: OTBRNodeInfo
    dataset: dict[str, Any]
    diagnostics: list[RouterDiag]
    ncp_version: str | None


@dataclass
class ThreadLink:
    """Render-time correlation result for one Matter device."""

    network_id: int
    network_name: str
    channel: int
    pan_id: str
    mesh_local_prefix: str
    border_router_url: str
    network_key: str  # passed through so reveal button can work
    rloc16: int | None  # None = prefix-only match
    thread_role: str  # "router" | "end_device" | "unknown"
    parent_rloc16: int | None  # set when thread_role == "end_device"
    link_margin: int | None
    average_rssi: int | None
    last_rssi: int | None
    connection_time: int | None
    frame_error_rate: float | None
    link_quality: int | None  # from childTable (end devices only)

    # ── Display helpers ──────────────────────────────────────────────────────
    # Formatting lives here, not in the template: the device-detail page just
    # renders these strings.

    @property
    def role_display(self) -> str:
        """Human-readable Thread role, with RLOC16 / parent router where known."""
        if self.thread_role == "router":
            return (
                thread_role_label("Router", f"RLOC16 0x{self.rloc16:04X}" if self.rloc16 else None)
                or "Router"
            )
        if self.thread_role == "end_device":
            if self.parent_rloc16:
                return f"End device of router 0x{self.parent_rloc16:04X}"
            return "End device"
        return "Unknown (prefix match only)"

    @property
    def rssi_display(self) -> str | None:
        """Average RSSI, with the last reading appended when available."""
        if self.average_rssi is None:
            return None
        if self.last_rssi is not None:
            return f"{self.average_rssi} dBm (last: {self.last_rssi} dBm)"
        return f"{self.average_rssi} dBm"

    @property
    def connection_time_display(self) -> str | None:
        """Connection uptime as ``Hh Mm``."""
        if self.connection_time is None:
            return None
        return f"{self.connection_time // 3600}h {(self.connection_time % 3600) // 60}m"

    @property
    def frame_error_rate_display(self) -> str | None:
        """Frame error rate as a one-decimal percentage."""
        if self.frame_error_rate is None:
            return None
        return f"{self.frame_error_rate * 100:.1f}%"


# ── IPv6 helpers ─────────────────────────────────────────────────────────────


def ip_in_prefix(ip: str, prefix: str) -> bool:
    try:
        return ipaddress.ip_address(ip) in ipaddress.ip_network(prefix, strict=False)
    except ValueError:
        return False


def extract_rloc16(ip: str, prefix: str) -> int | None:
    """Return the RLOC16 if `ip` is a mesh-local RLOC address, else None.

    A mesh-local RLOC has IID bytes: 00 00 00 ff fe 00 HH LL
    where HHLL is the 16-bit RLOC16.
    """
    try:
        addr = ipaddress.ip_address(ip)
        net = ipaddress.ip_network(prefix, strict=False)
        if addr not in net:
            return None
        iid = addr.packed[8:]
        if iid[:6] == bytes([0x00, 0x00, 0x00, 0xFF, 0xFE, 0x00]):
            return int.from_bytes(iid[6:], "big")
    except ValueError:
        pass
    return None


def extract_ext_mac_from_ll(ip: str) -> str | None:
    """Extract a Thread ext-MAC (hex) from a link-local fe80:: address."""
    try:
        addr = ipaddress.ip_address(ip)
        packed = addr.packed
        # link-local: first two bytes 0xfe80
        if packed[:2] != bytes([0xFE, 0x80]):
            return None
        iid = bytearray(packed[8:])
        iid[0] ^= 0x02  # undo the U/L bit flip
        return bytes(iid).hex()
    except ValueError:
        return None


def _rloc16_int(rloc16_str: str) -> int:
    """Parse "0xe400" or "e400" → 0xe400."""
    return int(rloc16_str, 16)


def router_rlocs_from_route(entry: dict[str, Any], *, exclude: int | None = None) -> list[int]:
    """RLOC16 of every router listed in a diagnostic reply's ``route`` TLV.

    A router id occupies the top six bits of its RLOC16 (``rloc16 = id << 10``).
    *exclude* drops one RLOC16 (the border router itself). Malformed entries
    are skipped; the result is sorted and de-duplicated.
    """
    route = entry.get("route")
    rows = route.get("routeData") if isinstance(route, dict) else None
    rlocs: set[int] = set()
    for row in rows or []:
        rid = row.get("routeId") if isinstance(row, dict) else None
        if isinstance(rid, int) and not isinstance(rid, bool) and 0 <= rid < 64:
            rlocs.add(rid << 10)
    if exclude is not None:
        rlocs.discard(exclude)
    return sorted(rlocs)


# ── JSON → dataclass parsers ─────────────────────────────────────────────────


def _parse_neighbor(raw: dict) -> RouterNeighbor:
    return RouterNeighbor(
        rloc16=raw.get("rloc16", "0x0"),
        ext_address=raw.get("extAddress", ""),
        link_margin=raw.get("linkMargin"),
        average_rssi=raw.get("averageRssi"),
        last_rssi=raw.get("lastRssi"),
        connection_time=raw.get("connectionTime"),
        frame_error_rate=raw.get("frameErrorRate"),
        message_error_rate=raw.get("messageErrorRate"),
    )


def _parse_child(raw: dict) -> RouterChild:
    mode = raw.get("mode", {})
    return RouterChild(
        child_id=raw.get("childId", 0),
        timeout=raw.get("timeout"),
        link_quality=raw.get("linkQuality"),
        rx_on_when_idle=mode.get("rxOnWhenIdle", False),
    )


def _parse_router(raw: dict) -> RouterDiag:
    return RouterDiag(
        ext_address=raw.get("extAddress", ""),
        rloc16=raw.get("rloc16", "0x0"),
        router_id=raw.get("routerId", 0),
        router_neighbors=[_parse_neighbor(n) for n in raw.get("routerNeighbors", [])],
        child_table=[_parse_child(c) for c in raw.get("childTable", [])],
    )


def _parse_node_info(raw: dict) -> OTBRNodeInfo:
    return OTBRNodeInfo(
        ba_id=raw.get("baId"),
        state=raw.get("state", "unknown"),
        rloc_address=raw.get("rlocAddress"),
        ext_address=raw.get("extAddress", ""),
        network_name=raw.get("networkName", ""),
        rloc16=raw.get("rloc16", "0x0"),
        router_id=raw.get("routerId"),
    )


# ── Correlation ───────────────────────────────────────────────────────────────


def correlate(
    node_ips: list[str],
    networks: Sequence[Any],  # list[ThreadNetwork] SQLModel rows
    diagnostics: list[RouterDiag],
    self_node: OTBRNodeInfo | None,
) -> ThreadLink | None:
    """Match a Matter node's IPv6 list against known Thread networks + OTBR diagnostics."""

    # Build lookup maps
    diag_by_rloc16: dict[int, RouterDiag] = {_rloc16_int(r.rloc16): r for r in diagnostics}
    diag_by_ext: dict[str, RouterDiag] = {r.ext_address.lower(): r for r in diagnostics}
    self_rloc16 = _rloc16_int(self_node.rloc16) if self_node else None

    # Pass 1: mesh-local RLOC form → precise role
    for net in networks:
        prefix = net.mesh_local_prefix
        for ip in node_ips:
            rloc16 = extract_rloc16(ip, prefix)
            if rloc16 is None:
                continue
            is_router = (rloc16 & 0x1FF) == 0

            # Check if this is the OTBR itself
            if self_node and rloc16 == self_rloc16:
                return ThreadLink(
                    network_id=net.id,
                    network_name=net.network_name,
                    channel=net.channel,
                    pan_id=net.pan_id,
                    mesh_local_prefix=prefix,
                    border_router_url=net.border_router_url,
                    network_key=net.network_key,
                    rloc16=rloc16,
                    thread_role="router",
                    parent_rloc16=None,
                    link_margin=None,
                    average_rssi=None,
                    last_rssi=None,
                    connection_time=None,
                    frame_error_rate=None,
                    link_quality=None,
                )

            if is_router:
                diag = diag_by_rloc16.get(rloc16)
                link_margin = average_rssi = last_rssi = connection_time = None
                frame_error_rate = None
                if diag:
                    # Link telemetry from neighbor entries in other routers
                    for other in diagnostics:
                        for nb in other.router_neighbors:
                            if _rloc16_int(nb.rloc16) == rloc16:
                                link_margin = nb.link_margin
                                average_rssi = nb.average_rssi
                                last_rssi = nb.last_rssi
                                connection_time = nb.connection_time
                                frame_error_rate = nb.frame_error_rate
                                break
                        if link_margin is not None:
                            break
                return ThreadLink(
                    network_id=net.id,
                    network_name=net.network_name,
                    channel=net.channel,
                    pan_id=net.pan_id,
                    mesh_local_prefix=prefix,
                    border_router_url=net.border_router_url,
                    network_key=net.network_key,
                    rloc16=rloc16,
                    thread_role="router",
                    parent_rloc16=None,
                    link_margin=link_margin,
                    average_rssi=average_rssi,
                    last_rssi=last_rssi,
                    connection_time=connection_time,
                    frame_error_rate=frame_error_rate,
                    link_quality=None,
                )
            else:
                # Child: parent = rloc16 & ~0x1FF, child_slot = rloc16 & 0x1FF
                parent = rloc16 & ~0x1FF
                child_slot = rloc16 & 0x1FF
                link_quality = None
                parent_diag = diag_by_rloc16.get(parent)
                if parent_diag:
                    for ch in parent_diag.child_table:
                        if ch.child_id == child_slot:
                            link_quality = ch.link_quality
                            break
                return ThreadLink(
                    network_id=net.id,
                    network_name=net.network_name,
                    channel=net.channel,
                    pan_id=net.pan_id,
                    mesh_local_prefix=prefix,
                    border_router_url=net.border_router_url,
                    network_key=net.network_key,
                    rloc16=rloc16,
                    thread_role="end_device",
                    parent_rloc16=parent,
                    link_margin=None,
                    average_rssi=None,
                    last_rssi=None,
                    connection_time=None,
                    frame_error_rate=None,
                    link_quality=link_quality,
                )

    # Pass 2: link-local ext-MAC → router match
    for ip in node_ips:
        ext_mac = extract_ext_mac_from_ll(ip)
        if ext_mac is None:
            continue
        diag = diag_by_ext.get(ext_mac.lower())
        if diag is None:
            continue
        rloc16 = _rloc16_int(diag.rloc16)
        # Look up telemetry from neighbor entries in other routers (same as Pass 1).
        link_margin = average_rssi = last_rssi = connection_time = None
        frame_error_rate = None
        for other in diagnostics:
            for nb in other.router_neighbors:
                if _rloc16_int(nb.rloc16) == rloc16:
                    link_margin = nb.link_margin
                    average_rssi = nb.average_rssi
                    last_rssi = nb.last_rssi
                    connection_time = nb.connection_time
                    frame_error_rate = nb.frame_error_rate
                    break
            if link_margin is not None:
                break
        for net in networks:
            return ThreadLink(
                network_id=net.id,
                network_name=net.network_name,
                channel=net.channel,
                pan_id=net.pan_id,
                mesh_local_prefix=net.mesh_local_prefix,
                border_router_url=net.border_router_url,
                network_key=net.network_key,
                rloc16=rloc16,
                thread_role="router",
                parent_rloc16=None,
                link_margin=link_margin,
                average_rssi=average_rssi,
                last_rssi=last_rssi,
                connection_time=connection_time,
                frame_error_rate=frame_error_rate,
                link_quality=None,
            )

    # Pass 3: prefix-only match
    for net in networks:
        for ip in node_ips:
            if ip_in_prefix(ip, net.mesh_local_prefix):
                return ThreadLink(
                    network_id=net.id,
                    network_name=net.network_name,
                    channel=net.channel,
                    pan_id=net.pan_id,
                    mesh_local_prefix=net.mesh_local_prefix,
                    border_router_url=net.border_router_url,
                    network_key=net.network_key,
                    rloc16=None,
                    thread_role="unknown",
                    parent_rloc16=None,
                    link_margin=None,
                    average_rssi=None,
                    last_rssi=None,
                    connection_time=None,
                    frame_error_rate=None,
                    link_quality=None,
                )

    return None


# ── OTBRClient ────────────────────────────────────────────────────────────────


class OTBRClient(PolledIntegration):
    """Long-lived async HTTP client for the OpenThread Border Router REST API."""

    slug = "otbr"
    short_name = "otbr"
    long_name = "OpenThread Border Router"
    icon = "icon-thread"
    can_create_devices = False
    can_update_devices = True
    can_update_status = False
    can_act_externally = False
    supported_protocols = frozenset({DeviceProtocol.matter})

    _BACKOFF = [30, 60, 300, 900, 1800]  # type: ignore[assignment]
    _poll_interval = 300  # type: ignore[assignment]

    def __init__(self, base_url: str) -> None:
        super().__init__()
        self._base_url = base_url.rstrip("/")
        self._status = ClientStatus.disconnected
        self._diagnostics: list[RouterDiag] = []
        self._self_node: OTBRNodeInfo | None = None
        # Serialises unicast walks (manual locate, auto-locate) so two never overlap.
        self._mesh_lock = asyncio.Lock()
        # Unresolved router ext address -> loop time of the last auto-locate attempt.
        self._auto_locate_attempts: dict[str, float] = {}

    def get_diagnostics(self) -> list[RouterDiag]:
        return list(self._diagnostics)

    def get_self_node(self) -> OTBRNodeInfo | None:
        return self._self_node

    def _on_stopped(self) -> None:
        self._status = ClientStatus.disconnected

    # ── Integration interface ─────────────────────────────────────────────────

    async def ingest(self) -> None:
        """Phase 1: poll OTBR and upsert the ThreadNetwork staging row.

        OTBR's own persistent model (ThreadNetwork) doubles as the phase-1
        staging table; no separate staging table is needed.
        """
        await self.poll_once()
        await self.run_auto_locate()

    def project(self, session: object) -> SyncResult:  # type: ignore[override]
        """Phase 2: add 'thread' to network_type for correlated devices.

        Add-only: never removes 'thread' from a device that already has it.
        Provenance gate (set_field) blocks the write when a higher-priority
        source owns network_type.
        """
        from sqlmodel import Session, select

        from ...database import engine
        from ...models import (
            Device,
            DeviceFabricMembership,
            FieldSource,
            MatterNodeRecord,
            ThreadNetwork,
        )
        from ...services import set_field
        from ..data import merge as merge_integration_data

        result = SyncResult()

        with Session(engine) as db:
            networks = db.exec(select(ThreadNetwork)).all()
            if not networks:
                return self._record_sync(result)

            diagnostics = self._diagnostics
            self_node = self._self_node

            node_records = db.exec(select(MatterNodeRecord)).all()
            for record in node_records:
                node_ips: list[str] = json.loads(record.ip_addresses_json or "[]")
                if not node_ips:
                    continue

                thread_link = correlate(node_ips, networks, diagnostics, self_node)
                if thread_link is None:
                    continue

                memberships = db.exec(
                    select(DeviceFabricMembership).where(
                        DeviceFabricMembership.node_id == record.node_id  # type: ignore[arg-type]
                    )
                ).all()

                for membership in memberships:
                    device = db.get(Device, membership.device_id)
                    if device is None:
                        continue
                    if device.protocol not in self.supported_protocols:  # type: ignore[union-attr]
                        continue

                    # ── B.12: persist what OTBR correlated for this device ──
                    merge_integration_data(
                        db,
                        device_id=device.id,
                        integration=self.slug,
                        payload={
                            "node_id": record.node_id,
                            "network_name": thread_link.network_name,
                            "channel": thread_link.channel,
                            "pan_id": thread_link.pan_id,
                            "rloc16": thread_link.rloc16,
                            "thread_role": thread_link.thread_role,
                            "source": "otbr",
                        },
                    )

                    current = list(device.network_type or [])
                    if "thread" in current:
                        result.skipped += 1
                        continue
                    new_types = current + ["thread"]
                    if set_field(device, "network_type", new_types, FieldSource.otbr):
                        db.add(device)
                        result.updated += 1
                    else:
                        result.skipped += 1

            self.assert_capabilities(db)
            db.commit()

        return self._record_sync(result)

    # ── OTBR polling ─────────────────────────────────────────────────────────

    async def _read_diagnostics(self, http: httpx.AsyncClient) -> list[Any]:
        """GET the border router's *already-collected* diagnostics cache.

        Server-only read - issues no scan, so it never touches the mesh and is
        safe in the sync/poll path (TECHNICAL_DESIGN §3a). Returns whatever the
        OTBR has cached (possibly empty until a scan has run).
        """
        try:
            diag_resp = await http.get(
                f"{self._base_url}/api/diagnostics", headers={"Accept": "application/json"}
            )
            diag_resp.raise_for_status()
            return list(diag_resp.json() or [])
        except Exception:
            return []

    async def _run_task(
        self, http: httpx.AsyncClient, destination: str, types: list[str], timeout: int
    ) -> bool:
        """POST one diagnostic task and wait for it; True only if it completed.

        ``failed`` is terminal (OTBR marks the task failed when a destination
        does not answer), so it is not polled until the deadline. Any transport
        or parse error counts as not completed.
        """
        hdrs = {"Accept": "application/json"}
        body = json.dumps(
            {
                "data": [
                    {
                        "type": "getNetworkDiagnosticTask",
                        "attributes": {
                            "destination": destination,
                            "destinationType": "rloc",
                            "types": types,
                            "timeout": timeout,
                        },
                    }
                ]
            }
        ).encode()
        try:
            post = await http.post(
                f"{self._base_url}/api/actions",
                content=body,
                headers={**hdrs, "Content-Type": "application/vnd.api+json"},
            )
            post.raise_for_status()
            task_id = post.json()["data"][0]["id"]
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout + 4
            status = None
            while loop.time() < deadline:
                await asyncio.sleep(_POLL_INTERVAL)
                st = await http.get(f"{self._base_url}/api/actions/{task_id}", headers=hdrs)
                status = st.json().get("status")
                if status in ("completed", "failed"):
                    break
            return status == "completed"
        except Exception:
            logger.debug("OTBR diagnostic task to %s failed", destination, exc_info=True)
            return False

    async def _broadcast_scan(self, http: httpx.AsyncClient) -> list[Any] | None:
        """Broadcast diagnostic scan (RLOC16 0xfffe); the raw replies, or None.

        **Actively wakes every router**, so this is an explicit device action
        only (``scan_diagnostics``), never part of sync (§3a). None when the task
        failed, timed out, or returned no replies.
        """
        if not await self._run_task(http, _DIAG_DEST, _DIAG_TYPES, _DIAG_SCAN_TIMEOUT):
            return None
        return await self._read_diagnostics(http) or None

    async def _query_router(
        self, http: httpx.AsyncClient, rloc16: int, types: list[str]
    ) -> dict[str, Any] | None:
        """Unicast one diagnostic request to *rloc16* and return that router's reply.

        Reaches onto the mesh (one router), so it is only ever called from an
        explicit device action. Returns None when the task fails, times out, or
        the reply is missing - callers treat that router as unreachable.
        """
        if not await self._run_task(http, f"0x{rloc16:04x}", types, _UNICAST_TIMEOUT):
            return None
        try:
            for entry in await self._read_diagnostics(http):
                if isinstance(entry, dict) and _rloc16_int(entry.get("rloc16", "")) == rloc16:
                    return entry
        except ValueError:
            pass
        return None

    async def _own_entry(
        self, http: httpx.AsyncClient, types: list[str]
    ) -> tuple[int, dict[str, Any] | None]:
        """The border router's own RLOC16 and its diagnostic reply for *types*.

        The ``route`` TLV of that reply lists every router id, so the router
        list needs no other integration and no mesh traffic beyond the local node.
        """
        own = self._self_node
        if own is None:
            resp = await http.get(f"{self._base_url}/node", headers={"Accept": "application/json"})
            resp.raise_for_status()
            own = _parse_node_info(resp.json())
        own_rloc = _rloc16_int(own.rloc16)
        return own_rloc, await self._query_router(http, own_rloc, types)

    async def _router_rlocs(self, http: httpx.AsyncClient) -> list[int]:
        """RLOC16 of every router except the border router itself."""
        own_rloc, entry = await self._own_entry(http, _ROUTE_TYPES)
        return router_rlocs_from_route(entry, exclude=own_rloc) if entry else []

    async def _scan_per_router(self, http: httpx.AsyncClient) -> tuple[list[Any], int]:
        """Unicast the full diagnostic request to each router in turn.

        Returns the raw replies (own node first) and the count of routers that
        did not answer. Paced and capped so it never bursts onto the mesh.
        """
        own_rloc, own = await self._own_entry(http, _DIAG_TYPES)
        if own is None:
            return [], 0
        entries: list[Any] = [own]
        unreachable = 0
        loop = asyncio.get_running_loop()
        started = loop.time()
        for rloc in router_rlocs_from_route(own, exclude=own_rloc):
            if loop.time() - started > _SCAN_BUDGET:
                unreachable += 1
                continue
            await asyncio.sleep(_UNICAST_GAP)
            entry = await self._query_router(http, rloc, _DIAG_TYPES)
            if entry is None:
                unreachable += 1
            else:
                entries.append(entry)
        return entries, unreachable

    async def scan_diagnostics(self) -> ScanOutcome:
        """Actively scan the Thread mesh for fresh per-router telemetry.

        Device action (TECHNICAL_DESIGN §3a/§3b): reaches onto the mesh and wakes
        every router, so it runs only on an explicit per-device button - never in
        sync or the background poll. Tries the broadcast first; when that task
        fails (one silent router fails all of it) it queries the routers one at
        a time. Updates the in-memory diagnostics cache used by render-time
        correlation. Raises ``RuntimeError`` when nothing answered, leaving the
        previous cache untouched, so the action never reports an empty success.
        """
        unreachable = 0
        async with httpx.AsyncClient(timeout=_DIAG_SCAN_TIMEOUT + 10) as http:
            raw = await self._broadcast_scan(http)
            method = "broadcast"
            if not raw:
                method = "per-router"
                try:
                    raw, unreachable = await self._scan_per_router(http)
                except Exception as exc:
                    raise RuntimeError(f"Thread diagnostic scan failed: {exc}") from exc
        if not raw:
            raise RuntimeError(
                "Thread diagnostic scan failed: the broadcast task failed and "
                "no router answered the per-router fallback"
            )
        diagnostics = [_parse_router(r) for r in raw]
        self._diagnostics = diagnostics
        return ScanOutcome(
            diagnostics=diagnostics,
            method=method,
            answered=len(diagnostics),
            unreachable=unreachable,
        )

    async def _router_replies(
        self, http: httpx.AsyncClient, stats: _WalkStats, *, budget: float
    ) -> AsyncGenerator[tuple[int, str, list[str]]]:
        """Walk the routers one at a time; yield ``(rloc16, ext_address, ipv6s)`` per reply.

        The router list comes from the border router's own route table. Queries
        are paced (``_UNICAST_GAP``) and the whole walk is capped at *budget*
        seconds. Routers that do not answer are counted in ``stats.failed``.
        Raises ``RuntimeError`` when the router list cannot be read.
        """
        from ..correlate import normalize_ip, normalize_mac

        rlocs = await self._router_rlocs(http)
        if not rlocs:
            raise RuntimeError("Could not read the Thread router list from the border router")
        loop = asyncio.get_running_loop()
        started = loop.time()
        for rloc in rlocs:
            if loop.time() - started > budget:
                return
            if stats.queried + stats.failed:
                await asyncio.sleep(_UNICAST_GAP)
            entry = await self._query_router(http, rloc, _LOCATE_TYPES)
            ext = normalize_mac(str(entry.get("extAddress", ""))) if entry else None
            if entry is None or ext is None:
                stats.failed += 1
                continue
            stats.queried += 1
            addrs = [a for a in map(normalize_ip, entry.get("ipv6Addresses") or []) if a]
            yield rloc, ext, addrs

    async def locate_thread_node(self, device: Any, session: Any) -> Any:
        """Find which Thread router *device* is and persist its extended address.

        Device action (TECHNICAL_DESIGN §3a/§3b): unicasts one router at a time,
        stops at the first reply whose IPv6 list contains one of the device's
        stored addresses, and writes ``thread_ext_address`` to the device's OTBR
        integration data. Every other registry Device recognised in a reply on
        the way is recorded too - that data was already received.
        """
        from ...models import Device
        from ..base import ActionResult
        from ..correlate import AddressIndex, device_ipv6_addresses
        from ..data import merge as merge_integration_data

        wanted = set(device_ipv6_addresses(session, device.id))
        if not wanted:
            return ActionResult(message="No IPv6 address is stored for this device")

        index = AddressIndex(session)
        stats = _WalkStats()
        also: list[str] = []
        found_ext: str | None = None
        found_rloc: int | None = None

        async with self._mesh_lock, httpx.AsyncClient(timeout=_UNICAST_TIMEOUT + 10) as http:
            async with aclosing(
                self._router_replies(http, stats, budget=_LOCATE_BUDGET)
            ) as replies:
                async for rloc, ext, addrs in replies:
                    if wanted.intersection(addrs):
                        merge_integration_data(
                            session,
                            device_id=device.id,
                            integration=self.slug,
                            payload={"thread_ext_address": ext},
                        )
                        found_ext, found_rloc = ext, rloc
                        break
                    owner = index.match(ipv6s=addrs)
                    other = session.get(Device, owner) if owner and owner != device.id else None
                    if other is not None:
                        merge_integration_data(
                            session,
                            device_id=other.id,
                            integration=self.slug,
                            payload={"thread_ext_address": ext},
                        )
                        also.append(other.name)

        extra = f" Also identified: {', '.join(also)}." if also else ""
        if found_ext is not None and found_rloc is not None:
            return ActionResult(
                message=f"Located on router {found_ext} (RLOC16 0x{found_rloc:04X}).{extra}",
                data={"thread_ext_address": found_ext},
            )
        if stats.queried == 0:
            raise RuntimeError(f"No router answered ({stats.failed} unreachable)")
        return ActionResult(
            message=(
                f"Not found among {stats.queried} answering routers ({stats.failed} unreachable). "
                f"A sleepy end device is not a router and cannot be located this way.{extra}"
            )
        )

    async def auto_locate(self) -> str | None:
        """Identify unrecognised Thread routers on its own; a summary, or None if idle.

        Runs after an OTBR poll when the user's auto-locate setting is on
        (default). It acts only when an unidentified Thread router is in the
        saved topology *and* a registry Device could be that router (a Thread
        Device with a stored IPv6 address whose extended address is unknown or
        no longer on the mesh); otherwise it sends nothing. One paced unicast
        walk records the extended address of every registry Device it
        recognises, and stops as soon as every unresolved router has answered.
        A router that stays unresolved is not queried again for several hours.
        """
        from sqlmodel import Session, select

        from ...audit import log as audit_log
        from ...database import engine
        from ...models import Device
        from ..correlate import AddressIndex, device_ipv6_addresses, normalize_mac
        from ..data import merge as merge_integration_data
        from ..data import read as read_integration_data
        from ..matter_server.server_client import load_topology
        from ..settings_store import otbr_auto_locate_enabled

        if self._mesh_lock.locked():
            return None
        loop = asyncio.get_running_loop()
        with Session(engine) as db:
            if not otbr_auto_locate_enabled(db):
                return None
            nodes = (load_topology(db) or {}).get("nodes") or []
            on_mesh: set[str] = set()
            unknown: set[str] = set()
            for node in nodes:
                ext = normalize_mac(node["ext_address"]) if node.get("ext_address") else None
                if ext:
                    on_mesh.add(ext)
                    if node.get("kind") == "thread_unknown":
                        unknown.add(ext)
            if not unknown:
                return None
            index = AddressIndex(db)
            now = loop.time()
            targets = {
                ext
                for ext in unknown
                if index.match(macs=[ext]) is None
                and now - self._auto_locate_attempts.get(ext, -_AUTO_LOCATE_COOLDOWN)
                >= _AUTO_LOCATE_COOLDOWN
            }
            if not targets:
                return None
            has_candidate = False
            for dev in db.exec(select(Device)).all():
                if "thread" not in (dev.network_type or []) or not device_ipv6_addresses(
                    db, dev.id
                ):
                    continue
                stored = (
                    read_integration_data(db, device_id=dev.id, integration=self.slug) or {}
                ).get("thread_ext_address")
                if not stored or normalize_mac(str(stored)) not in on_mesh:
                    has_candidate = True
                    break
            if not has_candidate:
                return None

            stats = _WalkStats()
            seen: set[str] = set()
            identified: list[str] = []
            async with self._mesh_lock, httpx.AsyncClient(timeout=_UNICAST_TIMEOUT + 10) as http:
                async with aclosing(
                    self._router_replies(http, stats, budget=_LOCATE_BUDGET)
                ) as replies:
                    async for _rloc, ext, addrs in replies:
                        seen.add(ext)
                        owner = index.match(ipv6s=addrs)
                        device = db.get(Device, owner) if owner else None
                        if device is not None:
                            merge_integration_data(
                                db,
                                device_id=device.id,
                                integration=self.slug,
                                payload={"thread_ext_address": ext},
                            )
                            identified.append(device.name)
                        if targets <= seen:
                            break
            for ext in targets:
                self._auto_locate_attempts[ext] = now
            summary = (
                f"auto-locate: identified {len(identified)} Thread device(s)"
                f"{' (' + ', '.join(identified) + ')' if identified else ''}; "
                f"reached {len(targets & seen)} of {len(targets)} unresolved routers"
            )
            audit_log(db, action="otbr.auto_locate", entity="integration:otbr", reason=summary)
            db.commit()
            return summary

    async def run_auto_locate(self) -> None:
        """``auto_locate()`` that never raises: a failure must not fail the poll or sync."""
        try:
            summary = await self.auto_locate()
            if summary:
                logger.info("OTBR %s", summary)
        except Exception:
            logger.warning("OTBR auto-locate failed", exc_info=True)

    def device_actions(self) -> list:
        """Declare the OTBR ``retrieve``-kind device actions (B.13).

        ``scan_diagnostics`` broadcasts to every router; ``locate_thread_node``
        unicasts one router at a time to learn which router a device is.
        """
        from ..base import ActionResult, DeviceAction
        from ..correlate import device_ipv6_addresses

        client = self

        async def _run(_device: Any, _session: Any) -> ActionResult:
            outcome = await client.scan_diagnostics()
            return ActionResult(message=outcome.message)

        def _applicable(device: Any, _session: Any) -> bool:
            return "thread" in (device.network_type or [])

        async def _run_locate(device: Any, session: Any) -> ActionResult:
            return await client.locate_thread_node(device, session)

        def _locate_applicable(device: Any, session: Any) -> bool:
            return "thread" in (device.network_type or []) and bool(
                device_ipv6_addresses(session, device.id)
            )

        return [
            DeviceAction(
                key="scan_diagnostics",
                label="Scan diagnostics",
                kind="retrieve",
                applicable_fn=_applicable,
                run_fn=_run,
            ),
            DeviceAction(
                key="locate_thread_node",
                label="Locate on Thread network",
                kind="retrieve",
                applicable_fn=_locate_applicable,
                run_fn=_run_locate,
            ),
        ]

    async def poll_once(
        self,
        *,
        dry_run: bool = False,
        reason: str = "otbr.background_poll",
    ) -> ThreadNetworkSnapshot:
        """Fetch /node + /node/dataset/active and read the cached diagnostics.

        Server-only (§3a): reads the OTBR's already-collected diagnostics cache
        but issues **no** scan - the active scan is the ``scan_diagnostics``
        device action. Returns a ThreadNetworkSnapshot. Upserts the
        ThreadNetwork DB row unless dry_run=True.
        """
        async with httpx.AsyncClient(timeout=10) as http:
            headers = {"Accept": "application/json"}
            # The hex TLV form of the active dataset comes from the same
            # endpoint with Accept: text/plain - both read the border router's
            # own config, so server-only (§3a), safe in the poll path.
            node_req, dataset_req, dataset_hex_req, ncp_req = await asyncio.gather(
                http.get(f"{self._base_url}/node", headers=headers),
                http.get(f"{self._base_url}/node/dataset/active", headers=headers),
                http.get(f"{self._base_url}/node/dataset/active", headers={"Accept": "text/plain"}),
                http.get(f"{self._base_url}/node/coprocessor-version", headers=headers),
                return_exceptions=True,
            )
            # Server-only cached read - no scan task (that's scan_diagnostics).
            diag_data = await self._read_diagnostics(http)

        def _json(resp: Any) -> Any:
            if isinstance(resp, Exception):
                return None
            try:
                resp.raise_for_status()
                return resp.json()
            except Exception:
                return None

        def _text(resp: Any) -> str | None:
            if isinstance(resp, Exception):
                return None
            try:
                resp.raise_for_status()
                text = resp.text.strip().strip('"')
                return text or None
            except Exception:
                return None

        node_data = _json(node_req)
        dataset_data = _json(dataset_req)
        ncp_data = _json(ncp_req)

        # Hex dataset is a bare hex string in the response body (text/plain).
        active_dataset_hex = _text(dataset_hex_req)

        if node_data is None or dataset_data is None:
            raise ConnectionError(
                "OTBR did not return valid data for /node or /node/dataset/active"
            )

        node_info = _parse_node_info(node_data)
        diagnostics = [_parse_router(r) for r in (diag_data or [])]
        ncp_version = ncp_data if isinstance(ncp_data, str) else None

        # Upsert ThreadNetwork row (skip when caller requested a dry run)
        if not dry_run:
            self._upsert_thread_network(
                node_info, dataset_data, ncp_version, active_dataset_hex, reason=reason
            )

        self._diagnostics = diagnostics
        self._self_node = node_info
        return ThreadNetworkSnapshot(
            node_info=node_info,
            dataset=dataset_data,
            diagnostics=diagnostics,
            ncp_version=ncp_version,
        )

    def _upsert_thread_network(
        self,
        node_info: OTBRNodeInfo,
        dataset: dict,
        ncp_version: str | None,
        active_dataset_hex: str | None = None,
        *,
        reason: str = "otbr.background_poll",
    ) -> None:
        from sqlmodel import Session, select

        from ...database import engine
        from ...models import ThreadNetwork

        ext_pan_id = dataset.get("extPanId", "")
        if not ext_pan_id:
            return

        pan_id_raw = dataset.get("panId", 0)
        pan_id_hex = (
            format(int(pan_id_raw), "04X") if isinstance(pan_id_raw, int) else str(pan_id_raw)
        )

        active_ts_raw = dataset.get("activeTimestamp")
        active_timestamp = None
        if isinstance(active_ts_raw, dict):
            active_timestamp = active_ts_raw.get("seconds")
        elif isinstance(active_ts_raw, int):
            active_timestamp = active_ts_raw

        now = datetime.now(UTC)
        with Session(engine) as session:
            row = session.exec(
                select(ThreadNetwork).where(ThreadNetwork.ext_pan_id == ext_pan_id)
            ).first()

            if row is None:
                row = ThreadNetwork(
                    name=dataset.get("networkName", ext_pan_id),
                    network_name=dataset.get("networkName", ""),
                    ext_pan_id=ext_pan_id,
                    pan_id=pan_id_hex,
                    channel=int(dataset.get("channel", 0)),
                    mesh_local_prefix=dataset.get("meshLocalPrefix", ""),
                    network_key=dataset.get("networkKey", ""),
                    pskc=dataset.get("pskc"),
                    active_timestamp=active_timestamp,
                    active_dataset_hex=active_dataset_hex,
                    border_router_url=self._base_url,
                    border_agent_id=node_info.ba_id,
                    ncp_version=ncp_version,
                    last_polled=now,
                )
            else:
                row.network_name = dataset.get("networkName", row.network_name)
                row.pan_id = pan_id_hex
                row.channel = int(dataset.get("channel", row.channel))
                row.mesh_local_prefix = dataset.get("meshLocalPrefix", row.mesh_local_prefix)
                row.network_key = dataset.get("networkKey", row.network_key)
                row.pskc = dataset.get("pskc", row.pskc)
                row.active_timestamp = active_timestamp
                if active_dataset_hex:
                    row.active_dataset_hex = active_dataset_hex
                row.border_router_url = self._base_url
                row.border_agent_id = node_info.ba_id
                row.ncp_version = ncp_version
                row.last_polled = now

            old_key = None
            if row.id is not None:
                from sqlmodel import Session as _S

                with _S(engine) as _s:
                    old = _s.get(ThreadNetwork, row.id)
                    old_key = old.network_key if old else None

            session.add(row)
            session.flush()

            from ...audit import log as audit_log

            if old_key is not None and old_key != dataset.get("networkKey", ""):
                audit_log(
                    session,
                    action="otbr.network_key_changed",
                    entity=f"thread_network:{row.id}",
                    reason=reason,
                )
            else:
                audit_log(
                    session,
                    action="otbr.poll",
                    entity=f"thread_network:{ext_pan_id}",
                    reason=reason,
                )
            session.commit()

    # ── PolledIntegration hooks ───────────────────────────────────────────────

    async def _poll_once(self) -> None:
        """One OTBR poll cycle: fetch network snapshot.  Raises PermanentError on 401."""
        self._status = ClientStatus.connecting
        try:
            await self.poll_once()
        except httpx.HTTPStatusError as exc:
            self._status = ClientStatus.error
            if exc.response.status_code == 401:
                logger.error(
                    "OTBR authentication failed (401 Unauthorized). "
                    "Update credentials in Settings → Integrations to reconnect."
                )
                raise PermanentError(str(exc)) from exc
            raise
        except Exception:
            self._status = ClientStatus.error
            raise
        logger.debug("OTBR polled successfully")
        self._status = ClientStatus.connected
        await self.run_auto_locate()

    def _on_poll_error(self, exc: Exception, attempt: int, delay: int) -> None:
        if attempt == 1:
            logger.warning("OTBR poll failed (%s); will retry in %ds", exc, delay)
        else:
            logger.info("OTBR retry in %ds (attempt %d)", delay, attempt)
