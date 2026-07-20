"""
Wrapper around python-matter-server's MatterClient.

All python-matter-server types are confined to this module; the rest of the app
uses the project-local NodeInfo / FabricInfo dataclasses.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import socket
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from ...models import DeviceProtocol
from ..base import SyncResult
from ..polled import PolledIntegration

logger = logging.getLogger(__name__)


class ClientStatus(StrEnum):
    disabled = "disabled"
    connecting = "connecting"
    connected = "connected"
    disconnected = "disconnected"
    error = "error"


@dataclass
class FabricInfo:
    fabric_id: int
    fabric_index: int
    fabric_label: str | None
    vendor_id: int
    vendor_name: str | None


# ── Network-diagnostics decode helpers ───────────────────────────────────────


def _kget(d: dict | None, k: str | int) -> Any:
    """Get from a dict using either string or int key form (handles JSON vs live SDK)."""
    if d is None:
        return None
    v = d.get(k)
    if v is None:
        try:
            v = d.get(str(k) if isinstance(k, int) else int(k))
        except (ValueError, TypeError):
            pass
    return v


def _b64_to_mac(b64: str | None) -> str | None:
    """Base64-encoded hardware address → colon-hex string."""
    if not b64:
        return None
    try:
        raw = base64.b64decode(b64)
        return ":".join(f"{b:02X}" for b in raw)
    except Exception:
        return None


def _b64_to_ipv4(b64: str | None) -> str | None:
    """Base64-encoded 4-byte IPv4 → dotted-quad string."""
    if not b64:
        return None
    try:
        raw = base64.b64decode(b64)
        if len(raw) != 4:
            return None
        return socket.inet_ntop(socket.AF_INET, raw)
    except Exception:
        return None


def _b64_to_ipv6(b64: str | None) -> str | None:
    """Base64-encoded 16-byte IPv6 → canonical string."""
    if not b64:
        return None
    try:
        raw = base64.b64decode(b64)
        if len(raw) != 16:
            return None
        return socket.inet_ntop(socket.AF_INET6, raw)
    except Exception:
        return None


_IFACE_TYPE: dict[int, str] = {1: "wifi", 2: "ethernet", 4: "thread"}
_ROUTING_ROLE: dict[int, str] = {
    0: "Unspecified",
    1: "Unassigned",
    2: "Sleepy End Device",
    3: "End Device",
    4: "REED",
    5: "Router",
    6: "Leader",
}
_WIFI_SECURITY: dict[int, str] = {
    0: "Unspecified",
    1: "None",
    2: "WEP",
    3: "WPA",
    4: "WPA2",
    5: "WPA3",
}
_WIFI_VERSION: dict[int, str] = {
    0: "a",
    1: "b",
    2: "g",
    3: "n",
    4: "ac",
    5: "ax",
    6: "ah",
}
_NETWORK_FAULT: dict[int, str] = {
    0: "Unspecified",
    1: "Hardware failure",
    2: "Network jammed",
    3: "Connection failed",
}
# Thread network-global attrs (same value for every device on this partition/network)
_THREAD_NETWORK_ATTRS: dict[str, str] = {
    "0/53/9": "Partition ID",
    "0/53/10": "Weighting",
    "0/53/11": "Data version",
    "0/53/12": "Stable data version",
    "0/53/13": "Leader router ID",
}

# Thread per-device traffic counters
_THREAD_COUNTER_ATTRS: dict[str, str] = {
    "0/53/22": "Tx total",
    "0/53/23": "Tx unicast",
    "0/53/24": "Tx broadcast",
    "0/53/32": "Tx retry",
    "0/53/39": "Rx total",
    "0/53/40": "Rx unicast",
    "0/53/41": "Rx broadcast",
    "0/53/53": "Rx FCS errors",
}


def _humanize_seconds(secs: int) -> str:
    """Convert seconds to a human-readable duration string (e.g. '82d 14h 12m')."""
    d, r = divmod(int(secs), 86400)
    h, r = divmod(r, 3600)
    m, _ = divmod(r, 60)
    parts: list[str] = []
    if d:
        parts.append(f"{d}d")
    if h:
        parts.append(f"{h}h")
    if m or not parts:
        parts.append(f"{m}m")
    return " ".join(parts)


@dataclass
class NetworkInfo:
    """Networking diagnostics decoded from Matter cluster attributes.

    Fields marked 'persisted' are stable enough to store on Device;
    everything else is live-only (rendered fresh from the cache).
    """

    # Persisted on Device (see Device.network_type / Device.mac_address)
    network_type: str | None = None  # "wifi" | "thread" | "ethernet" | None
    mac_address: str | None = None  # colon-hex, 6-byte (WiFi/Eth) or 8-byte (Thread)
    # Interface-level (live-only)
    interface_name: str | None = None
    ipv4_addresses: list[str] = field(default_factory=list)
    ipv6_addresses: list[str] = field(default_factory=list)
    # WiFi-specific (live-only)
    ssid: str | None = None
    bssid: str | None = None
    rssi: int | None = None
    channel: int | None = None
    wifi_version: str | None = None
    security_type: str | None = None
    # Interface availability (live-only)
    is_operational: bool | None = None
    # GeneralDiagnostics fields (live-only)
    uptime_seconds: int | None = None
    uptime_humanized: str | None = None
    reboot_count: int | None = None
    active_network_faults: list[int] = field(default_factory=list)
    # Thread-specific (live-only)
    thread_channel: int | None = None
    thread_role: str | None = None
    thread_network_name: str | None = None
    thread_pan_id: int | None = None
    thread_extended_pan_id: int | None = None
    thread_mesh_local_prefix: str | None = None
    thread_counters: dict[str, int] = field(default_factory=dict)
    thread_network_attrs: dict[str, int] = field(default_factory=dict)


def _extract_network_info(attrs: dict[str, Any]) -> NetworkInfo:
    """Parse networking diagnostics from a node's flat ep/cluster/attr dict.

    The dict keys use the python-matter-server wire format: "<ep>/<cluster>/<attr>".
    Struct field keys within values may be strings ("0", "1", …) from JSON
    or integers from the live SDK; _kget() handles both transparently.

    Clusters used:
      - 0/51/0  GeneralDiagnostics.NetworkInterfaces  (interface list)
      - 0/54/*  WiFiNetworkDiagnostics                (WiFi-only)
      - 0/49/6  NetworkCommissioning.LastConnectErrorValue SSID field (WiFi-only)
      - 0/53/*  ThreadNetworkDiagnostics              (Thread-only)

    Robust to missing attributes - returns all-None NetworkInfo on any gap.
    """
    # GeneralDiagnostics cluster 51, attribute 0 = NetworkInterfaces list
    ifaces = attrs.get("0/51/0") or []

    # Pick the first operational interface; fall back to first available
    iface: dict | None = None
    for i in ifaces:
        if isinstance(i, dict) and _kget(i, "1"):  # isOperational
            iface = i
            break
    if iface is None and ifaces:
        iface = ifaces[0] if isinstance(ifaces[0], dict) else None
    if iface is None:
        return NetworkInfo()

    iface_type_int = _kget(iface, "7")
    # Type=0 (Unspecified) and missing type both yield 'unknown' so the panel
    # still renders the common rows (MAC, IPs, uptime, reboots) even when the
    # device doesn't classify its interface (e.g. Roborock Saros10R).
    network_type = (
        _IFACE_TYPE.get(iface_type_int, "unknown") if iface_type_int is not None else "unknown"
    )

    is_operational_raw = _kget(iface, "1")
    is_operational = bool(is_operational_raw) if is_operational_raw is not None else None

    mac_address = _b64_to_mac(_kget(iface, "4"))
    interface_name = _kget(iface, "0")

    # GeneralDiagnostics (cluster 51)
    uptime_seconds_raw = attrs.get("0/51/2")
    uptime_seconds = int(uptime_seconds_raw) if uptime_seconds_raw is not None else None
    uptime_humanized = _humanize_seconds(uptime_seconds) if uptime_seconds is not None else None
    reboot_count_raw = attrs.get("0/51/1")
    reboot_count = int(reboot_count_raw) if reboot_count_raw is not None else None
    active_network_faults: list[int] = list(attrs.get("0/51/7") or [])

    ipv4_raw = _kget(iface, "5") or []
    ipv4_addresses = [x for x in (_b64_to_ipv4(ip) for ip in ipv4_raw) if x]

    ipv6_raw = _kget(iface, "6") or []
    ipv6_addresses = [x for x in (_b64_to_ipv6(ip) for ip in ipv6_raw) if x]

    # WiFi diagnostics (cluster 54)
    ssid = bssid = rssi = channel = wifi_version = security_type = None
    if network_type == "wifi":
        bssid = _b64_to_mac(attrs.get("0/54/0"))
        sec = attrs.get("0/54/1")
        security_type = _WIFI_SECURITY.get(sec) if sec is not None else None
        wv = attrs.get("0/54/2")
        wifi_version = f"802.11{_WIFI_VERSION[wv]}" if wv in _WIFI_VERSION else None
        channel = attrs.get("0/54/3")
        rssi = attrs.get("0/54/4")
        # SSID: NetworkCommissioning attr 6 holds the connected SSID as base64
        ssid_b64 = attrs.get("0/49/6")
        if ssid_b64:
            try:
                ssid = base64.b64decode(ssid_b64).decode("utf-8", errors="replace").strip()
            except Exception:
                pass

    # Thread diagnostics (cluster 53)
    thread_channel = thread_role = thread_network_name = None
    thread_pan_id = thread_extended_pan_id = thread_mesh_local_prefix = None
    thread_counters: dict[str, int] = {}
    thread_network_attrs: dict[str, int] = {}
    if network_type == "thread":
        thread_channel = attrs.get("0/53/0")
        role_int = attrs.get("0/53/1")
        thread_role = _ROUTING_ROLE.get(role_int) if role_int is not None else None
        thread_network_name = attrs.get("0/53/2")
        thread_pan_id = attrs.get("0/53/3")
        thread_extended_pan_id = attrs.get("0/53/4")
        mlp_b64 = attrs.get("0/53/5")
        if mlp_b64:
            try:
                raw = base64.b64decode(mlp_b64)
                # variable-length prefix
                padded = raw + b"\x00" * max(0, 16 - len(raw))
                prefix = socket.inet_ntop(socket.AF_INET6, padded[:16])
                thread_mesh_local_prefix = f"{prefix}/{len(raw) * 8}"
            except Exception:
                pass
        for attr_key, label in _THREAD_NETWORK_ATTRS.items():
            v = attrs.get(attr_key)
            if v is not None:
                try:
                    thread_network_attrs[label] = int(v)
                except (TypeError, ValueError):
                    pass
        for attr_key, label in _THREAD_COUNTER_ATTRS.items():
            v = attrs.get(attr_key)
            if v is not None:
                try:
                    thread_counters[label] = int(v)
                except (TypeError, ValueError):
                    pass

    return NetworkInfo(
        network_type=network_type,
        mac_address=mac_address,
        is_operational=is_operational,
        interface_name=interface_name,
        ipv4_addresses=ipv4_addresses,
        ipv6_addresses=ipv6_addresses,
        uptime_seconds=uptime_seconds,
        uptime_humanized=uptime_humanized,
        reboot_count=reboot_count,
        active_network_faults=active_network_faults,
        ssid=ssid,
        bssid=bssid,
        rssi=rssi,
        channel=channel,
        wifi_version=wifi_version,
        security_type=security_type,
        thread_channel=thread_channel,
        thread_role=thread_role,
        thread_network_name=thread_network_name,
        thread_pan_id=thread_pan_id,
        thread_extended_pan_id=thread_extended_pan_id,
        thread_mesh_local_prefix=thread_mesh_local_prefix,
        thread_counters=thread_counters,
        thread_network_attrs=thread_network_attrs,
    )


@dataclass
class NodeInfo:
    node_id: int
    available: bool
    # BasicInformation fields
    vendor_id: int | None
    vendor_name: str | None
    product_id: int | None
    product_name: str | None
    serial: str | None
    hardware_version_string: str | None
    firmware_version_string: str | None
    node_label: str | None
    unique_id: str | None
    manufacturing_date: str | None
    product_url: str | None
    part_number: str | None
    # B.22b - commissioning date + bridge flag (from MatterNodeData)
    commissioned_at: datetime | None = None
    is_bridge: bool = False
    # B.22c - extra BasicInformation attrs surfaced in B.12 payload
    product_label: str | None = None  # human label distinct from productName
    product_appearance: dict | None = None  # ProductAppearance finish/color
    spec_version_int: int | None = None  # SpecificationVersion (int)
    hardware_version_int: int | None = None  # HardwareVersion (int)
    software_version_int: int | None = None  # SoftwareVersion (int)
    # B.22a - real fabric memberships (from OperationalCredentials.Fabrics cache)
    fabrics: list[FabricInfo] = field(default_factory=list)
    # Cluster presence map: {endpoint_id: [cluster_ids]}
    endpoints: dict[int, list[int]] = field(default_factory=dict)
    # Live attribute snapshots (cluster class → instance)
    _node_obj: Any = field(default=None, repr=False, compare=False)
    # Network diagnostics extracted from matter cluster attributes (B.1)
    network_type: list[str] = field(default_factory=list)
    mac_address: str | None = None
    ip_addresses: list[str] = field(default_factory=list)
    network_info: NetworkInfo | None = field(default=None, repr=False, compare=False)

    def live_cluster(self, endpoint: int, cluster_cls: type) -> Any | None:
        """Return a live cluster object for a given endpoint if present."""
        if self._node_obj is None:
            return None
        try:
            return self._node_obj.get_cluster(endpoint, cluster_cls)
        except Exception:
            return None


def _extract_node_info(node: Any) -> NodeInfo:
    """Convert a MatterNode into a project-local NodeInfo."""

    info = node.device_info  # BasicInformation cluster on ep0, may be None

    def _attr(name: str) -> Any:
        if info is None:
            return None
        val = getattr(info, name, None)
        # python-matter-server uses a NullValue sentinel for optional attrs
        try:
            from chip.clusters.Types import NullValue

            if val is NullValue:
                return None
        except ImportError:
            pass
        return val

    # Build endpoint → cluster list map
    endpoints: dict[int, list[int]] = {}
    for ep_id, ep_obj in node.endpoints.items():
        endpoints[ep_id] = list(ep_obj.clusters.keys())

    try:
        ip_addresses = list(node.ip_addresses or [])
    except Exception:
        ip_addresses = []

    # B.22b: commissioning date + bridge flag from MatterNodeData
    nd = getattr(node, "node_data", None)
    commissioned_at: datetime | None = None
    is_bridge = False
    try:
        raw_ts = getattr(nd, "date_commissioned", None)
        if raw_ts is not None:
            # python-matter-server stores as timezone-aware UTC datetime
            commissioned_at = raw_ts.replace(tzinfo=UTC) if raw_ts.tzinfo is None else raw_ts
        is_bridge = bool(getattr(nd, "is_bridge", False))
    except Exception:
        pass

    # B.22c: extra BasicInformation attrs
    product_label = _attr("productLabel")
    spec_version_int: int | None = None
    hardware_version_int: int | None = None
    software_version_int: int | None = None
    product_appearance: dict | None = None
    try:
        sv = _attr("specificationVersion")
        spec_version_int = int(sv) if sv is not None else None
        hv = _attr("hardwareVersion")
        hardware_version_int = int(hv) if hv is not None else None
        swv = _attr("softwareVersion")
        software_version_int = int(swv) if swv is not None else None
        pa = _attr("productAppearance")
        if pa is not None:
            product_appearance = {
                "finish": int(getattr(pa, "finish", 0) or 0),
                "primaryColor": getattr(pa, "primaryColor", None),
            }
    except Exception:
        pass

    # B.22a: real fabric memberships from OperationalCredentials.Fabrics cache
    fabrics: list[FabricInfo] = []
    try:
        attrs = getattr(nd, "attributes", None) or {}
        raw_fabrics = attrs.get("0/62/1") or []
        for f in raw_fabrics:
            fid = int(getattr(f, "fabricID", 0) or 0)
            vid = int(getattr(f, "vendorID", 0) or 0)
            if fid:
                fabrics.append(
                    FabricInfo(
                        fabric_id=fid,
                        fabric_index=int(getattr(f, "fabricIndex", 0) or 0),
                        fabric_label=str(getattr(f, "label", "") or "") or None,
                        vendor_id=vid,
                        vendor_name=None,  # resolved only by the refresh_fabrics device action
                    )
                )
    except Exception:
        pass

    return NodeInfo(
        node_id=node.node_id,
        available=node.available,
        vendor_id=_attr("vendorID"),
        vendor_name=_attr("vendorName"),
        product_id=_attr("productID"),
        product_name=_attr("productName"),
        serial=_attr("serialNumber"),
        hardware_version_string=_attr("hardwareVersionString"),
        firmware_version_string=_attr("softwareVersionString"),
        node_label=_attr("nodeLabel"),
        unique_id=_attr("uniqueID"),
        manufacturing_date=_attr("manufacturingDate"),
        product_url=_attr("productURL"),
        part_number=_attr("partNumber"),
        commissioned_at=commissioned_at,
        is_bridge=is_bridge,
        product_label=product_label,
        spec_version_int=spec_version_int,
        hardware_version_int=hardware_version_int,
        software_version_int=software_version_int,
        product_appearance=product_appearance,
        fabrics=fabrics,
        endpoints=endpoints,
        _node_obj=node,
        ip_addresses=ip_addresses,
        **_node_network_fields(node),
    )


def _node_network_fields(node: Any) -> dict[str, Any]:
    """Extract network_type, mac_address, and network_info from a live node."""
    try:
        nd = getattr(node, "node_data", None)
        attrs = getattr(nd, "attributes", None) or {}
        ni = _extract_network_info(attrs)
        nt_str = ni.network_type
        network_types = [nt_str] if nt_str and nt_str not in ("unknown", "") else []
        return {
            "network_type": network_types,
            "mac_address": ni.mac_address,
            "network_info": ni,
        }
    except Exception:
        return {"network_type": [], "mac_address": None, "network_info": None}


class MatterServerClient(PolledIntegration):
    """Long-lived asyncio WS client for python-matter-server.

    Usage::

        client = MatterServerClient(url)
        await client.start()   # non-blocking; runs reconnect loop in background
        nodes = await client.list_nodes()
        await client.stop()
    """

    slug = "matter_server"
    short_name = "matter"
    long_name = "Matter Server"
    icon = "icon-protocol-matter"
    can_create_devices = True
    can_update_devices = True
    can_update_status = False
    can_act_externally = False
    supported_protocols = frozenset({DeviceProtocol.matter})

    def __init__(self, url: str) -> None:
        super().__init__()
        self._url = url
        self._status = ClientStatus.disconnected
        self._nodes: dict[int, NodeInfo] = {}
        self._mc: Any = None  # live MatterClient; set while connected

    def _on_stopped(self) -> None:
        self._status = ClientStatus.disconnected

    async def _poll_once(self) -> None:
        # _run_loop is overridden for the WS reconnect pattern; _poll_once is never called.
        raise NotImplementedError("MatterServerClient overrides _run_loop")

    async def list_nodes(self) -> list[NodeInfo]:
        """Return a snapshot of currently-known nodes (from cache)."""
        return list(self._nodes.values())

    def get_node(self, node_id: int) -> NodeInfo | None:
        return self._nodes.get(node_id)

    async def refresh_network(self, node_id: int) -> None:
        """Re-read all networking diagnostic attributes for *node_id* from the
        device and update the local cache.  Raises RuntimeError when not
        connected."""
        if self._mc is None or self._status != ClientStatus.connected:
            raise RuntimeError("Matter Server not connected")
        paths = [
            "0/51/0",
            "0/51/1",
            "0/51/2",
            "0/51/7",  # GeneralDiagnostics
            "0/54/0",
            "0/54/1",
            "0/54/2",
            "0/54/3",
            "0/54/4",  # WiFiDiag
            "0/53/0",
            "0/53/1",
            "0/53/2",
            "0/53/3",
            "0/53/4",
            "0/53/5",  # ThreadDiag
            "0/53/6",
            "0/49/6",  # NetworkCommissioning.LastNetworkID (SSID)
        ]
        for path in paths:
            try:
                await self._mc.refresh_attribute(node_id, path)
            except Exception:
                pass  # missing cluster - ignore
        raw_node = self._mc.get_node(node_id)
        if raw_node is not None:
            updated = _node_network_fields(raw_node)
            ni = self._nodes.get(node_id)
            if ni is not None:
                ni.network_type = updated["network_type"]
                ni.mac_address = updated["mac_address"]
                ni.network_info = updated["network_info"]

    # ── Integration interface ─────────────────────────────────────────────────

    async def ingest(self) -> None:
        """Phase 1: flush in-memory node cache to MatterNodeRecord staging table.

        Does nothing when not connected (cache is empty / stale).
        """
        if not self._nodes:
            return

        from sqlmodel import Session, select

        from ...database import engine
        from ...models import MatterNodeRecord

        now = datetime.now(UTC)
        known_ids = set(self._nodes.keys())

        with Session(engine) as session:
            for node in self._nodes.values():
                record = session.get(MatterNodeRecord, node.node_id)
                if record is None:
                    record = MatterNodeRecord(node_id=node.node_id)
                record.available = node.available
                record.vendor_id = node.vendor_id
                record.vendor_name = node.vendor_name
                record.product_id = node.product_id
                record.product_name = node.product_name
                record.serial = node.serial
                record.hardware_version_string = node.hardware_version_string
                record.firmware_version_string = node.firmware_version_string
                record.node_label = node.node_label
                record.unique_id = node.unique_id
                record.manufacturing_date = node.manufacturing_date
                record.product_url = node.product_url
                record.part_number = node.part_number
                record.network_type_json = json.dumps(node.network_type)
                record.mac_address = node.mac_address
                # Prefer cluster-derived IPs (GeneralDiagnostics) over the
                # python-matter-server node property, which is often empty.
                _ips = (
                    list(node.network_info.ipv6_addresses)
                    if node.network_info and node.network_info.ipv6_addresses
                    else list(node.ip_addresses)
                )
                record.ip_addresses_json = json.dumps(_ips)
                record.endpoint_json = json.dumps(node.endpoints)
                record.date_commissioned = (
                    node.commissioned_at.isoformat() if node.commissioned_at else None
                )
                record.is_bridge = node.is_bridge
                record.product_label = node.product_label
                record.product_appearance_json = (
                    json.dumps(node.product_appearance) if node.product_appearance else None
                )
                record.spec_version_int = node.spec_version_int
                record.hardware_version_int = node.hardware_version_int
                record.software_version_int = node.software_version_int
                record.last_synced = now
                session.add(record)
            # Remove records for nodes no longer in the WS cache
            stale = session.exec(
                select(MatterNodeRecord).where(
                    MatterNodeRecord.node_id.notin_(known_ids)  # type: ignore[union-attr,attr-defined]
                )
            ).all()
            for r in stale:
                session.delete(r)
            session.commit()

    def project(self, session: Any) -> SyncResult:
        """Phase 2: read MatterNodeRecord staging rows and call _apply_nodes."""
        from sqlmodel import Session, select

        from ...database import engine
        from ...models import MatterNodeRecord

        with Session(engine) as db:
            records = db.exec(select(MatterNodeRecord)).all()
            nodes = [_node_info_from_record(r) for r in records]
            result = _apply_nodes(db, nodes, integration=self)
        return self._record_sync(
            SyncResult(
                created=result["create"],
                updated=result["update"],
                skipped=result["unchanged"],
            )
        )

    def device_actions(self) -> list:
        """Declare retrieve-kind device actions (B.13 + B.22a/d)."""
        from ..base import ActionResult, DeviceAction

        client = self

        def _get_node_id(device: Any, session: Any) -> int | None:
            from sqlmodel import select

            from ...models import DeviceFabricMembership

            mem = session.exec(
                select(DeviceFabricMembership).where(
                    DeviceFabricMembership.device_id == device.id  # type: ignore[attr-defined]
                )
            ).first()
            return mem.node_id if mem else None

        def _has_membership(device: Any, session: Any) -> bool:
            return _get_node_id(device, session) is not None

        # ── refresh_network ───────────────────────────────────────────────────
        async def _run_refresh_network(device: Any, session: Any) -> ActionResult:
            if client._mc is None or client._status != ClientStatus.connected:
                raise RuntimeError("Matter Server not connected")
            node_id = _get_node_id(device, session)
            if node_id is None:
                raise ValueError("Device has no Matter fabric membership")
            await client.refresh_network(node_id)
            return ActionResult(message="Networking data refreshed")

        # ── refresh_fabrics ───────────────────────────────────────────────────
        async def _run_refresh_fabrics(device: Any, session: Any) -> ActionResult:
            if client._mc is None or client._status != ClientStatus.connected:
                raise RuntimeError("Matter Server not connected")
            node_id = _get_node_id(device, session)
            if node_id is None:
                raise ValueError("Device has no Matter fabric membership")
            fabrics = await client._mc.get_matter_fabrics(node_id)
            from sqlmodel import select

            from ...database import engine
            from ...models import Fabric

            with __import__("sqlmodel").Session(engine) as db:
                for fi in fabrics:
                    fid = int(getattr(fi, "fabric_id", 0) or 0)
                    if not fid:
                        continue
                    hex_id = f"{fid:016x}"
                    fab = db.exec(select(Fabric).where(Fabric.fabric_id == hex_id)).first()
                    vendor_name = getattr(fi, "vendor_name", None)
                    vendor_id = int(getattr(fi, "vendor_id", 0) or 0) or None
                    fabric_label = str(getattr(fi, "fabric_label", "") or "") or None
                    if fab is None:
                        fab = Fabric(
                            fabric_id=hex_id,
                            controller=fabric_label or hex_id,
                            fabric_label=fabric_label,
                            vendor_id=vendor_id,
                            vendor_name=vendor_name,
                        )
                        db.add(fab)
                    else:
                        if vendor_name and fab.vendor_name != vendor_name:
                            fab.vendor_name = vendor_name
                            db.add(fab)
                        if vendor_id and fab.vendor_id != vendor_id:
                            fab.vendor_id = vendor_id
                            db.add(fab)
                        if fabric_label and fab.fabric_label != fabric_label:
                            fab.fabric_label = fabric_label
                            db.add(fab)
                db.commit()
            return ActionResult(message=f"Fabric list refreshed ({len(fabrics)} fabric(s))")

        return [
            DeviceAction(
                key="refresh_network",
                label="Refresh networking",
                kind="retrieve",
                applicable_fn=_has_membership,
                run_fn=_run_refresh_network,
            ),
            DeviceAction(
                key="refresh_fabrics",
                label="Refresh fabric list",
                kind="retrieve",
                applicable_fn=_has_membership,
                run_fn=_run_refresh_fabrics,
            ),
        ]

    async def sync_now(self) -> SyncResult:
        """Override: project from live WS cache directly (no fresh HTTP ingest).

        Uses the in-memory ``_nodes`` dict populated by WS events so there
        is no extra round-trip to python-matter-server.
        """
        if self._status != ClientStatus.connected or not self._nodes:
            return SyncResult()
        nodes = list(self._nodes.values())
        from sqlmodel import Session

        from ...database import engine

        with Session(engine) as session:
            result = _apply_nodes(session, nodes, integration=self)
        return self._record_sync(
            SyncResult(
                created=result["create"],
                updated=result["update"],
                skipped=result["unchanged"],
            )
        )

    # ── internal ──────────────────────────────────────────────────────────────

    async def _run_loop(self) -> None:
        attempt = 0
        while not self._stop_event.is_set():
            try:
                self._status = ClientStatus.connecting
                await self._connect_once()
                attempt = 0  # reset on clean exit
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._error_msg = str(exc)
                self._status = ClientStatus.disconnected
                delay = self._BACKOFF[min(attempt, len(self._BACKOFF) - 1)]
                if attempt == 0:
                    logger.warning("Matter Server connection failed (%s); will retry", exc)
                else:
                    logger.info(
                        "Matter Server reconnecting in %ds (attempt %d)", delay, attempt + 1
                    )
                attempt += 1
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
                except TimeoutError:
                    pass

    async def _connect_once(self) -> None:
        import aiohttp
        from matter_server.client.client import MatterClient
        from matter_server.client.exceptions import ConnectionClosed

        async with aiohttp.ClientSession() as http:
            mc = MatterClient(self._url, http)
            ready = asyncio.Event()

            async def _on_ready() -> None:
                # Populate node cache after start_listening handshake completes
                for node in mc.get_nodes():
                    self._nodes[node.node_id] = _extract_node_info(node)
                self._status = ClientStatus.connected
                self._error_msg = None
                logger.debug("Matter Server connected; %d node(s) loaded", len(self._nodes))
                ready.set()

            # start_listening blocks until the connection is closed; we run it
            # as a task so we can call get_nodes() after the initial node list
            # arrives (signalled by init_ready event).
            init_event = asyncio.Event()
            listen_task = asyncio.create_task(
                mc.start_listening(init_ready=init_event), name="ms-listen"
            )
            try:
                await asyncio.wait_for(init_event.wait(), timeout=10)
            except TimeoutError:
                listen_task.cancel()
                raise ConnectionError("Timed out waiting for Matter Server handshake")

            for node in mc.get_nodes():
                self._nodes[node.node_id] = _extract_node_info(node)
            self._status = ClientStatus.connected
            self._error_msg = None
            self._mc = mc
            logger.debug("Matter Server connected; %d node(s) loaded", len(self._nodes))

            # Subscribe to node changes so the cache stays fresh
            def _on_event(event_type: Any, data: Any) -> None:
                from matter_server.common.models import EventType

                try:
                    if event_type == EventType.NODE_ADDED or event_type == EventType.NODE_UPDATED:
                        node = mc.get_node(data.node_id)
                        self._nodes[node.node_id] = _extract_node_info(node)
                    elif event_type == EventType.NODE_REMOVED:
                        self._nodes.pop(data, None)
                except Exception as exc:
                    logger.debug("Matter Server event handler error: %s", exc)

            mc.subscribe_events(_on_event)

            try:
                await listen_task
            except ConnectionClosed:
                logger.info("Matter Server connection closed; will reconnect")
            finally:
                if not listen_task.done():
                    listen_task.cancel()
                self._mc = None
                self._status = ClientStatus.disconnected


# ── Integration inheritance (B.2) ─────────────────────────────────────────────


def _node_info_from_record(record: Any) -> NodeInfo:
    """Reconstruct a minimal NodeInfo from a MatterNodeRecord staging row.

    ``_node_obj`` and ``network_info`` are None since they are live-only.
    Endpoint dict keys are ints (SQLite stores JSON strings; we re-cast them).
    ``fabrics`` is empty - managed via DeviceFabricMembership, not stored here.
    """
    commissioned_at: datetime | None = None
    try:
        if record.date_commissioned:
            commissioned_at = datetime.fromisoformat(record.date_commissioned)
    except Exception:
        pass

    product_appearance: dict | None = None
    try:
        if record.product_appearance_json:
            product_appearance = json.loads(record.product_appearance_json)
    except Exception:
        pass

    return NodeInfo(
        node_id=record.node_id,
        available=record.available,
        vendor_id=record.vendor_id,
        vendor_name=record.vendor_name,
        product_id=record.product_id,
        product_name=record.product_name,
        serial=record.serial,
        hardware_version_string=record.hardware_version_string,
        firmware_version_string=record.firmware_version_string,
        node_label=record.node_label,
        unique_id=record.unique_id,
        manufacturing_date=record.manufacturing_date,
        product_url=record.product_url,
        part_number=record.part_number,
        commissioned_at=commissioned_at,
        is_bridge=bool(record.is_bridge),
        product_label=record.product_label,
        spec_version_int=record.spec_version_int,
        hardware_version_int=record.hardware_version_int,
        software_version_int=record.software_version_int,
        product_appearance=product_appearance,
        network_type=json.loads(record.network_type_json),
        mac_address=record.mac_address,
        ip_addresses=json.loads(record.ip_addresses_json),
        endpoints={int(k): v for k, v in json.loads(record.endpoint_json).items()},
    )


def _apply_nodes(
    session: Any,
    nodes: list[NodeInfo],
    *,
    integration: PolledIntegration | None = None,
) -> dict:
    """Create/update Device + Fabric + DeviceFabricMembership rows from NodeInfo list.

    Returns ``{"create": int, "update": int, "unchanged": int}``.

    Pass *integration* so that ``assert_capabilities`` is called before commit
    to enforce that capability flags are not violated.

    This is the phase-2 projection for the Matter Server integration.  It was
    originally ``_apply_matter_nodes`` in ``app/api/integrations.py``; moved
    here (B.2) so that the Matter Server client owns its full sync lifecycle.
    """
    from sqlmodel import select

    from ...audit import log as audit_log
    from ...models import (
        Device,
        DeviceFabricMembership,
        Fabric,
        FieldSource,
        Property,
        PropertyType,
    )
    from ...services import set_field
    from ..data import upsert as upsert_integration_data

    CONTROLLER_LABEL = "HA Matter"
    UNKNOWN_FABRIC_HEX = "0000000000000000"

    fab_row = session.exec(select(Fabric).where(Fabric.fabric_id == UNKNOWN_FABRIC_HEX)).first()
    if fab_row is None:
        fab_row = Fabric(
            fabric_id=UNKNOWN_FABRIC_HEX,
            controller=CONTROLLER_LABEL,
            fabric_label=CONTROLLER_LABEL,
        )
        session.add(fab_row)
        session.flush()

    now = datetime.now(UTC)
    created = updated = unchanged = 0
    visited_node_ids: set[int] = set()
    # (node, device_id) pairs the loop resolved - reused for B.12 below so we
    # don't re-derive the node→device match (and stay in sync with the protocol
    # guard / serial-match tiers above).
    matched_pairs: list[tuple[NodeInfo, str]] = []

    for node in nodes:
        existing: Device | None = None

        if node.unique_id:
            existing = session.exec(
                select(Device).where(Device.matter_unique_id == node.unique_id)
            ).first()

        if existing is None:
            _mem_corr = session.exec(
                select(DeviceFabricMembership).where(
                    DeviceFabricMembership.node_id == node.node_id,
                )
            ).first()
            if _mem_corr:
                existing = session.get(Device, _mem_corr.device_id)

        if existing is None and node.vendor_id and node.product_id and node.serial:
            existing = session.exec(
                select(Device).where(
                    Device.vendor_id == node.vendor_id,
                    Device.product_id == node.product_id,
                    Device.serial == node.serial,
                )
            ).first()

        if (
            existing is not None
            and integration is not None
            and existing.protocol not in integration.supported_protocols
        ):
            continue

        if existing is None:
            dev_name = node.node_label or node.product_name or f"Matter Node {node.node_id}"
            notes_parts: list[str] = []
            if node.manufacturing_date:
                notes_parts.append(f"Manufactured: {node.manufacturing_date}")

            dev = Device(
                name=dev_name,
                name_source=FieldSource.matter,
                protocol=DeviceProtocol.matter,
                vendor=node.vendor_name,
                vendor_source=FieldSource.matter if node.vendor_name else FieldSource.generated,
                product=node.product_name,
                product_source=FieldSource.matter if node.product_name else FieldSource.generated,
                vendor_id=node.vendor_id,
                vendor_id_source=FieldSource.matter
                if node.vendor_id is not None
                else FieldSource.generated,
                product_id=node.product_id,
                product_id_source=FieldSource.matter
                if node.product_id is not None
                else FieldSource.generated,
                serial=node.serial,
                serial_source=FieldSource.matter if node.serial else FieldSource.generated,
                hardware_version=node.hardware_version_string,
                hardware_version_source=FieldSource.matter
                if node.hardware_version_string
                else FieldSource.generated,
                firmware_version=node.firmware_version_string,
                firmware_version_source=FieldSource.matter
                if node.firmware_version_string
                else FieldSource.generated,
                matter_unique_id=node.unique_id,
                matter_unique_id_source=FieldSource.matter
                if node.unique_id
                else FieldSource.generated,
                network_type=node.network_type,
                network_type_source=FieldSource.matter
                if node.network_type
                else FieldSource.generated,
                mac_address=node.mac_address,
                mac_address_source=FieldSource.matter
                if node.mac_address
                else FieldSource.generated,
                commissioned_at=node.commissioned_at,
                commissioned_at_source=FieldSource.matter
                if node.commissioned_at
                else FieldSource.generated,
                notes="\n".join(notes_parts) or None,
                notes_source=FieldSource.matter if notes_parts else FieldSource.generated,
                created_at=now,
                updated_at=now,
            )
            session.add(dev)
            session.flush()

            if node.product_url:
                session.add(
                    Property(
                        device_id=dev.id,
                        type=PropertyType.other,
                        value=node.product_url,
                        label="Product URL",
                        source=FieldSource.matter,
                    )
                )
            if node.part_number:
                session.add(
                    Property(
                        device_id=dev.id,
                        type=PropertyType.other,
                        value=node.part_number,
                        label="Part number",
                        source=FieldSource.matter,
                    )
                )

            audit_log(
                session,
                action="matter_server.import",
                entity=f"device:{dev.id}",
                reason=f"matter_server:node:{node.node_id}",
            )
            created += 1
            device_id = dev.id
        else:
            changed = False
            if node.hardware_version_string:
                changed |= set_field(
                    existing, "hardware_version", node.hardware_version_string, FieldSource.matter
                )
            if node.firmware_version_string:
                changed |= set_field(
                    existing, "firmware_version", node.firmware_version_string, FieldSource.matter
                )
            if node.unique_id:
                changed |= set_field(
                    existing, "matter_unique_id", node.unique_id, FieldSource.matter
                )
            if node.network_type:
                merged = sorted(set(existing.network_type or []) | set(node.network_type))
                changed |= set_field(existing, "network_type", merged, FieldSource.matter)
            if node.mac_address:
                changed |= set_field(existing, "mac_address", node.mac_address, FieldSource.matter)
            if node.commissioned_at:
                changed |= set_field(
                    existing, "commissioned_at", node.commissioned_at, FieldSource.matter
                )
            if changed:
                existing.updated_at = now
                session.add(existing)
                updated += 1
            else:
                unchanged += 1
            device_id = existing.id

        # ── B.22a: upsert real Fabric rows from cached OperationalCredentials.Fabrics ──
        # HA's Matter vendor_id (0x130D). Used to identify the HA fabric for membership.
        _HA_VENDOR_ID = 4877
        ha_fab_row: Fabric | None = None
        for fi in node.fabrics:
            _hex_id = f"{fi.fabric_id:016x}"
            _real_fab = session.exec(select(Fabric).where(Fabric.fabric_id == _hex_id)).first()
            if _real_fab is None:
                _real_fab = Fabric(
                    fabric_id=_hex_id,
                    controller=fi.fabric_label or _hex_id,
                    fabric_label=fi.fabric_label,
                    vendor_id=fi.vendor_id,
                    vendor_name=fi.vendor_name,
                )
                session.add(_real_fab)
                session.flush()
            else:
                _fab_dirty = False
                if fi.vendor_id is not None and _real_fab.vendor_id != fi.vendor_id:
                    _real_fab.vendor_id = fi.vendor_id
                    _fab_dirty = True
                if fi.fabric_label and _real_fab.fabric_label != fi.fabric_label:
                    _real_fab.fabric_label = fi.fabric_label
                    _fab_dirty = True
                if fi.vendor_name and _real_fab.vendor_name != fi.vendor_name:
                    _real_fab.vendor_name = fi.vendor_name
                    _fab_dirty = True
                if _fab_dirty:
                    session.add(_real_fab)
            if fi.vendor_id == _HA_VENDOR_ID:
                ha_fab_row = _real_fab

        # Target fabric for membership: real HA fabric (if identified) else placeholder
        target_fab = ha_fab_row if ha_fab_row is not None else fab_row

        # Upsert DeviceFabricMembership - keyed on (device_id, node_id) across all fabrics
        mem = session.exec(  # type: ignore[no-redef]
            select(DeviceFabricMembership).where(
                DeviceFabricMembership.device_id == device_id,
                DeviceFabricMembership.node_id == node.node_id,
            )
        ).first()
        purged = False

        # Remove duplicate memberships for this device on target_fab
        dupes = session.exec(
            select(DeviceFabricMembership).where(
                DeviceFabricMembership.fabric_id == target_fab.id,
                DeviceFabricMembership.device_id == device_id,
                DeviceFabricMembership.node_id != node.node_id,
            )
        ).all()
        for d in dupes:
            audit_log(
                session,
                action="matter_server.membership_prune",
                entity=f"device:{d.device_id}",
                reason="duplicate_per_device",
            )
            session.delete(d)
            purged = True

        # Remove conflicting memberships (same node_id on target_fab, different device)
        conflicting = session.exec(
            select(DeviceFabricMembership).where(
                DeviceFabricMembership.fabric_id == target_fab.id,
                DeviceFabricMembership.node_id == node.node_id,
                DeviceFabricMembership.device_id != device_id,
            )
        ).all()
        for c in conflicting:
            audit_log(
                session,
                action="matter_server.membership_prune",
                entity=f"device:{c.device_id}",
                reason="cross_device_node_id_collision",
            )
            session.delete(c)
            purged = True

        # When re-homing to a real fabric, remove stale placeholder memberships
        if target_fab is not fab_row:
            stale_ph = session.exec(
                select(DeviceFabricMembership).where(
                    DeviceFabricMembership.fabric_id == fab_row.id,
                    DeviceFabricMembership.device_id == device_id,
                )
            ).all()
            for sp in stale_ph:
                if mem is None or sp.id != getattr(mem, "id", None):
                    session.delete(sp)
                    purged = True

        if purged:
            session.flush()

        if mem is None:
            mem = DeviceFabricMembership(
                device_id=device_id,
                fabric_id=target_fab.id,  # type: ignore[arg-type]
                node_id=node.node_id,
                endpoint_json=json.dumps(node.endpoints),
            )
            session.add(mem)
        else:
            mem.fabric_id = target_fab.id  # re-home if needed
            mem.node_id = node.node_id
            mem.endpoint_json = json.dumps(node.endpoints)
            session.add(mem)
        visited_node_ids.add(node.node_id)
        matched_pairs.append((node, device_id))

    # Sweep stale memberships across all fabrics (nodes gone from WS cache)
    stale_mems = session.exec(
        select(DeviceFabricMembership).where(
            DeviceFabricMembership.node_id.notin_(visited_node_ids),  # type: ignore[union-attr,attr-defined]
        )
    ).all()
    for stale in stale_mems:
        audit_log(
            session,
            action="matter_server.membership_prune",
            entity=f"device:{stale.device_id}",
            reason="ui.manual_sync",
        )
        session.delete(stale)

    # Prune placeholder Fabric row if it now has no members
    placeholder_count = session.exec(
        select(DeviceFabricMembership).where(
            DeviceFabricMembership.fabric_id == fab_row.id,
        )
    ).all()
    if not placeholder_count:
        session.delete(fab_row)

    audit_log(
        session,
        action="matter_server.import",
        entity="matter_server",
        reason="ui.manual_sync",
    )
    if integration is not None:
        integration.assert_capabilities(session, created=created)

    # ── B.12: write per-device integration data for each matched node ──
    # Reuses the (node, device_id) pairs the main loop already resolved.
    for node, device_id in matched_pairs:
        upsert_integration_data(
            session,
            device_id=device_id,
            integration="matter_server",
            payload={
                "node_id": node.node_id,
                "available": node.available,
                "vendor_name": node.vendor_name,
                "product_name": node.product_name,
                "vendor_id": node.vendor_id,
                "product_id": node.product_id,
                "serial": node.serial,
                "hardware_version": node.hardware_version_string,
                "firmware_version": node.firmware_version_string,
                "node_label": node.node_label,
                "unique_id": node.unique_id,
                "network_type": node.network_type,
                "mac_address": node.mac_address,
                # B.22b
                "commissioned_at": node.commissioned_at.isoformat()
                if node.commissioned_at
                else None,
                "is_bridge": node.is_bridge,
                # B.22c
                "product_label": node.product_label,
                "spec_version_int": node.spec_version_int,
                "hardware_version_int": node.hardware_version_int,
                "software_version_int": node.software_version_int,
                "product_appearance": node.product_appearance,
                # B.22a - fabric list from OperationalCredentials.Fabrics cache
                "fabrics": [
                    {
                        "fabric_id": f"{fi.fabric_id:016x}",
                        "fabric_index": fi.fabric_index,
                        "fabric_label": fi.fabric_label,
                        "vendor_id": fi.vendor_id,
                        "vendor_name": fi.vendor_name,
                    }
                    for fi in node.fabrics
                ],
                "source": "matter_server",
            },
        )

    session.commit()

    return {"create": created, "update": updated, "unchanged": unchanged}
