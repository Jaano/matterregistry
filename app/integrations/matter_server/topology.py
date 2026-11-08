"""Matter network topology (WS schema 13) helpers.

Pure functions over the graph dict the Matter Server returns from
``get_network_topology`` (nodes + connections). The graph is built by the server
from its own attribute cache, so reading it never touches a device.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ...thread import thread_role_label

_STRENGTH_RANK = {"strong": 0, "medium": 1, "weak": 2, "unknown": 3, "none": 4}
_ROLE_NAMES = {
    "router": "Router",
    "leader": "Leader",
    "reed": "REED",
    "end_device": "End Device",
    "sleepy_end_device": "Sleepy End Device",
    "station": "Wi-Fi station",
}

NameLookup = Callable[[int], str | None]


def role_label(role: str | None) -> str | None:
    """Display name for a topology role; Thread 1.4 names are added for Router and REED."""
    if not role:
        return None
    base = _ROLE_NAMES.get(role, role.replace("_", " ").capitalize())
    return thread_role_label(base)


def find_matter_node(topology: dict[str, Any], node_id: int) -> dict[str, Any] | None:
    for node in topology.get("nodes") or []:
        if node.get("kind") == "matter" and node.get("node_id") == node_id:
            return node
    return None


def node_label(node: dict[str, Any], name_for_node: NameLookup | None = None) -> str:
    """Human label for any topology node."""
    kind = node.get("kind")
    if kind == "matter":
        node_id = node.get("node_id")
        name = name_for_node(node_id) if name_for_node and node_id is not None else None
        return name or f"Node {node_id}"
    if kind == "border_router":
        vendor_model = " ".join(p for p in (node.get("vendor_name"), node.get("model_name")) if p)
        return node.get("host_name") or vendor_model or "Border Router"
    if kind == "wifi_ap":
        return node.get("ssid") or node.get("bssid") or "Wi-Fi access point"
    ext = node.get("ext_address")
    return f"Thread device {ext}" if ext else "Unknown device"


def _link_numbers(direction: Any) -> tuple[int | None, int | None]:
    if not isinstance(direction, dict):
        return None, None
    lqi, rssi = direction.get("lqi"), direction.get("rssi")
    return (
        lqi if isinstance(lqi, int) and not isinstance(lqi, bool) else None,
        rssi if isinstance(rssi, int) and not isinstance(rssi, bool) else None,
    )


def links_of(
    topology: dict[str, Any],
    node: dict[str, Any],
    name_for_node: NameLookup | None = None,
) -> list[dict[str, Any]]:
    """Links of *node*, strongest first, each seen from *node*'s side."""
    nodes_by_id = {n.get("id"): n for n in topology.get("nodes") or []}
    me = node.get("id")
    links: list[dict[str, Any]] = []
    for conn in topology.get("connections") or []:
        if conn.get("source") == me:
            peer_id, mine = conn.get("target"), conn.get("source_to_target")
        elif conn.get("target") == me:
            peer_id, mine = conn.get("source"), conn.get("target_to_source")
        else:
            continue
        peer = nodes_by_id.get(peer_id)
        if peer is None:
            continue
        lqi, rssi = _link_numbers(mine)
        links.append(
            {
                "peer": node_label(peer, name_for_node),
                "peer_kind": peer.get("kind"),
                "strength": conn.get("strength") or "unknown",
                "lqi": lqi,
                "rssi": rssi,
            }
        )
    links.sort(key=lambda lk: (_STRENGTH_RANK.get(lk["strength"], 3), lk["peer"]))
    return links


def link_text(link: dict[str, Any]) -> str:
    parts = [str(link["strength"])]
    if link.get("lqi") is not None:
        parts.append(f"LQI {link['lqi']}")
    if link.get("rssi") is not None:
        parts.append(f"RSSI {link['rssi']} dBm")
    return f"{link['peer']}: " + ", ".join(parts)


def node_network_name(node: dict[str, Any]) -> str | None:
    """Thread network name or Wi-Fi SSID for a topology node."""
    return node.get("network_name") or node.get("ssid") or None
