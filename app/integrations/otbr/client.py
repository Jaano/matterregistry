"""
OTBR REST client.

All OTBR-specific JSON shapes are confined to this module; the rest of the
app uses project-local dataclasses (ThreadNetworkSnapshot, RouterDiag, etc.).

Sync poll flow (server-only - never touches the mesh, per TECHNICAL_DESIGN §3a):
  GET /node                  - node summary (ba_id, state, rloc16, ext_address …)
  GET /node/dataset/active   - full operational dataset (Accept: application/json)
  GET /node/coprocessor-version - NCP firmware version (optional; may 404)
  GET /api/diagnostics       - read the *already-collected* per-router telemetry cache

Diagnostic scan (active - wakes every router; explicit device action only, never sync):
  POST /api/actions          - trigger a broadcast diagnostic scan (0xfffe → all routers)
  GET /api/actions/{id}      - poll task status until "completed"
  GET /api/diagnostics       - read the freshly populated per-router telemetry cache

The background loop and every "Sync now" path do the **server-only** read above;
the active scan runs solely via ``scan_diagnostics()`` behind a per-device button
(ISSUES.md I.18 / B.23). Background loop polls every 5 min when enabled.
Backoff on failure: 30s → 60s → 300s → 900s → 1800s (cap).
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

import httpx

from ...models import DeviceProtocol
from ..base import SyncResult
from ..polled import PermanentError, PolledIntegration

logger = logging.getLogger(__name__)

# Broadcast RLOC16 - reaches all routers in the Thread partition.
_DIAG_DEST = "0xfffe"
# Server-side scan timeout passed to OTBR; routers that don't answer within
# this window are simply absent from the results.
_DIAG_SCAN_TIMEOUT = 10  # seconds
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
            return f"Router (RLOC16 0x{self.rloc16:04X})" if self.rloc16 else "Router"
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
        from ..data import upsert as upsert_integration_data

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
                    upsert_integration_data(
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

    async def _trigger_diagnostics(self, http: httpx.AsyncClient) -> list[Any]:
        """POST a broadcast diagnostic scan task, wait for completion, return raw list.

        Uses RLOC16 0xfffe (Thread broadcast - all routers) so one request
        covers the whole partition.  **Actively wakes every router**, so this is
        an explicit device action only (``scan_diagnostics``), never part of
        sync (§3a). Falls back to whatever is already cached if the task POST or
        polling fails.
        """
        base_hdrs = {"Accept": "application/json"}
        try:
            body = json.dumps(
                {
                    "data": [
                        {
                            "type": "getNetworkDiagnosticTask",
                            "attributes": {
                                "destination": _DIAG_DEST,
                                "destinationType": "rloc",
                                "types": _DIAG_TYPES,
                                "timeout": _DIAG_SCAN_TIMEOUT,
                            },
                        }
                    ]
                }
            ).encode()
            post_resp = await http.post(
                f"{self._base_url}/api/actions",
                content=body,
                headers={**base_hdrs, "Content-Type": "application/vnd.api+json"},
            )
            post_resp.raise_for_status()
            task_id = post_resp.json()["data"][0]["id"]

            # Poll until completed or the server-side timeout elapses.
            deadline = asyncio.get_running_loop().time() + _DIAG_SCAN_TIMEOUT + 4
            while asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(2)
                st_resp = await http.get(
                    f"{self._base_url}/api/actions/{task_id}", headers=base_hdrs
                )
                if st_resp.json().get("status") == "completed":
                    break
        except Exception:
            pass  # fall through and read whatever is already cached

        return await self._read_diagnostics(http)

    async def scan_diagnostics(self) -> list[RouterDiag]:
        """Actively scan the Thread mesh for fresh per-router telemetry.

        Device action (TECHNICAL_DESIGN §3a/§3b, ISSUES.md I.18 / B.23): reaches
        onto the mesh and wakes every router, so it runs only on an explicit
        per-device button - never in sync or the background poll. Updates the
        in-memory diagnostics cache used by render-time correlation and returns
        the fresh results.
        """
        async with httpx.AsyncClient(timeout=_DIAG_SCAN_TIMEOUT + 10) as http:
            diag_data = await self._trigger_diagnostics(http)
        diagnostics = [_parse_router(r) for r in (diag_data or [])]
        self._diagnostics = diagnostics
        return diagnostics

    def device_actions(self) -> list:
        """Declare the ``scan_diagnostics`` retrieve-kind device action (B.13)."""
        from ..base import ActionResult, DeviceAction

        client = self

        async def _run(_device: Any, _session: Any) -> ActionResult:
            await client.scan_diagnostics()
            return ActionResult(message="Thread diagnostics scan complete")

        def _applicable(device: Any, _session: Any) -> bool:
            return "thread" in (device.network_type or [])

        return [
            DeviceAction(
                key="scan_diagnostics",
                label="Scan diagnostics",
                kind="retrieve",
                applicable_fn=_applicable,
                run_fn=_run,
            )
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

    def _on_poll_error(self, exc: Exception, attempt: int, delay: int) -> None:
        if attempt == 1:
            logger.warning("OTBR poll failed (%s); will retry in %ds", exc, delay)
        else:
            logger.info("OTBR retry in %ds (attempt %d)", delay, attempt)
