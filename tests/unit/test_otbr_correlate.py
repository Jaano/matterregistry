"""
Unit tests for OTBR correlation logic.
No container, no network - pure in-process logic.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.integrations.otbr.client import (
    OTBRNodeInfo,
    RouterChild,
    RouterDiag,
    RouterNeighbor,
    correlate,
    extract_ext_mac_from_ll,
    extract_rloc16,
    ip_in_prefix,
)

# Shared prefix used across tests
PREFIX = "fd46:5a23:f008:e644::/64"
PREFIX_BASE = "fd46:5a23:f008:e644"


# Mesh-local RLOC address builder
def rloc_addr(rloc16: int) -> str:
    return f"{PREFIX_BASE}:0:ff:fe00:{rloc16:x}"


# Mesh-local EID (random IID - no RLOC pattern)
EID_ADDR = f"{PREFIX_BASE}:1234:5678:9abc:def0"

# Link-local from ext address ca1bcc6de177f673 (flip bit6 of first byte → c8...)
LL_ADDR = "fe80::c81b:ccff:fee1:77f673"  # intentionally wrong length for test
LL_ADDR_CORRECT = "fe80::c81b:ccff:fee1:7767"  # flipped bit6: 0xca ^ 0x02 = 0xc8


@dataclass
class FakeNet:
    id: int = 1
    network_name: str = "TestNet"
    channel: int = 25
    pan_id: str = "9C31"
    mesh_local_prefix: str = PREFIX
    border_router_url: str = "http://otbr.local:8081"
    network_key: str = "aabbccdd" * 4


SELF_NODE = OTBRNodeInfo(
    ba_id="ba123",
    state="router",
    rloc_address=rloc_addr(0x7000),
    ext_address="ca1bcc6de177f673",
    network_name="TestNet",
    rloc16="0x7000",
    router_id=28,
)

ROUTER_DIAG = RouterDiag(
    ext_address="828bf7d912250440",
    rloc16="0xe400",
    router_id=57,
    router_neighbors=[
        RouterNeighbor(
            rloc16="0x7000",
            ext_address="ca1bcc6de177f673",
            link_margin=18,
            average_rssi=-72,
            last_rssi=-70,
            connection_time=16253,
            frame_error_rate=0.01,
            message_error_rate=0.0,
        )
    ],
    child_table=[RouterChild(child_id=1, timeout=12, link_quality=3, rx_on_when_idle=False)],
)


# ── ip_in_prefix ──────────────────────────────────────────────────────────────


def test_ip_in_prefix_match():
    assert ip_in_prefix(rloc_addr(0xE400), PREFIX) is True


def test_ip_in_prefix_no_match():
    assert ip_in_prefix("fd00::1", PREFIX) is False


def test_ip_in_prefix_bad_input():
    assert ip_in_prefix("not-an-ip", PREFIX) is False


# ── extract_rloc16 ────────────────────────────────────────────────────────────


def test_extract_rloc16_router():
    assert extract_rloc16(rloc_addr(0xE400), PREFIX) == 0xE400


def test_extract_rloc16_child():
    assert extract_rloc16(rloc_addr(0xE401), PREFIX) == 0xE401


def test_extract_rloc16_eid_returns_none():
    # Random IID - not a RLOC form
    assert extract_rloc16(EID_ADDR, PREFIX) is None


def test_extract_rloc16_wrong_prefix():
    assert extract_rloc16(rloc_addr(0xE400), "fd00::/64") is None


# ── extract_ext_mac_from_ll ───────────────────────────────────────────────────


def test_extract_ext_mac_roundtrip():
    # Build a link-local from a known ext address and verify we can recover it.
    # ext_address = "828bf7d912250440"
    # modified EUI-64: flip bit6 of first byte: 0x82 ^ 0x02 = 0x80
    # IID bytes: 80 8b f7 d9 12 25 04 40
    ll = "fe80::808b:f7d9:1225:0440"
    mac = extract_ext_mac_from_ll(ll)
    assert mac == "828bf7d912250440"


def test_extract_ext_mac_non_ll_returns_none():
    assert extract_ext_mac_from_ll("fd46:5a23:f008:e644::1") is None


# ── correlate: router via RLOC16 ─────────────────────────────────────────────


def test_correlate_router_by_rloc16():
    nets = [FakeNet()]
    diags = [ROUTER_DIAG]
    node_ips = [rloc_addr(0xE400)]
    link = correlate(node_ips, nets, diags, SELF_NODE)
    assert link is not None
    assert link.thread_role == "router"
    assert link.rloc16 == 0xE400
    # Should pick up link telemetry from ROUTER_DIAG's routerNeighbors targeting 0xe400?
    # Actually in the test data, the neighbor entry in ROUTER_DIAG targets 0x7000 (self),
    # not 0xe400. The correlate function looks for a neighbor entry *about* 0xe400 in other routers.
    # No such entry here, so telemetry is None - that's correct.


def test_correlate_self_otbr_by_rloc16():
    nets = [FakeNet()]
    node_ips = [rloc_addr(0x7000)]  # self_node rloc16
    link = correlate(node_ips, nets, [ROUTER_DIAG], SELF_NODE)
    assert link is not None
    assert link.thread_role == "router"
    assert link.rloc16 == 0x7000


# ── correlate: child via RLOC16 ──────────────────────────────────────────────


def test_correlate_child_via_rloc16():
    # rloc16 = 0xe401 → parent 0xe400 (router 57), child_id = 1
    nets = [FakeNet()]
    node_ips = [rloc_addr(0xE401)]
    link = correlate(node_ips, nets, [ROUTER_DIAG], SELF_NODE)
    assert link is not None
    assert link.thread_role == "end_device"
    assert link.parent_rloc16 == 0xE400
    assert link.link_quality == 3  # from ROUTER_DIAG.child_table[0]


# ── correlate: prefix-only (EID) ─────────────────────────────────────────────


def test_correlate_prefix_only():
    nets = [FakeNet()]
    node_ips = [EID_ADDR]
    link = correlate(node_ips, nets, [ROUTER_DIAG], SELF_NODE)
    assert link is not None
    assert link.thread_role == "unknown"
    assert link.rloc16 is None


# ── correlate: no match ───────────────────────────────────────────────────────


def test_correlate_no_match():
    nets = [FakeNet()]
    node_ips = ["fd00::1", "192.168.1.1"]
    link = correlate(node_ips, nets, [ROUTER_DIAG], SELF_NODE)
    assert link is None


def test_correlate_no_networks():
    link = correlate([rloc_addr(0xE400)], [], [ROUTER_DIAG], SELF_NODE)
    assert link is None


# ── correlate: router-neighbor telemetry ─────────────────────────────────────


def test_correlate_router_gets_neighbor_telemetry():
    # Device at 0xe400 is a router; add a neighbor entry from 0x7000 (self) pointing at 0xe400
    self_as_router = RouterDiag(
        ext_address=SELF_NODE.ext_address,
        rloc16="0x7000",
        router_id=28,
        router_neighbors=[
            RouterNeighbor(
                rloc16="0xe400",
                ext_address="828bf7d912250440",
                link_margin=22,
                average_rssi=-65,
                last_rssi=-63,
                connection_time=5000,
                frame_error_rate=0.005,
                message_error_rate=0.0,
            )
        ],
        child_table=[],
    )
    nets = [FakeNet()]
    node_ips = [rloc_addr(0xE400)]
    link = correlate(node_ips, nets, [ROUTER_DIAG, self_as_router], SELF_NODE)
    assert link is not None
    assert link.thread_role == "router"
    assert link.link_margin == 22
    assert link.average_rssi == -65
    assert link.connection_time == 5000
