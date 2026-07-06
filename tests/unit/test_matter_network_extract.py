"""Unit tests for _extract_network_info.

Drives the extractor with fixture data from matter_data/node_details.json
(no live Matter Server needed).

Node inventory in fixture:
  node_id=1  - Wi-Fi  (0/51/0 type=1, 0/54/x present)
  node_id=4  - Thread (0/51/0 type=4 "ieee802154", 0/53/x present)
  No-cluster node is synthesised inline.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from app.integrations.matter_server.server_client import (
    NetworkInfo,
    _extract_network_info,
    _node_network_fields,
)

_FIXTURE = pathlib.Path(__file__).parents[2] / "matter_data" / "node_details.json"


def _node_attrs(node_id: int) -> dict:
    data = json.loads(_FIXTURE.read_text())
    node = next((n for n in data if n["node_id"] == node_id), None)
    if node is None:
        pytest.skip(f"node_id={node_id} not found in fixture")
    return node["attributes"]


# ── Wi-Fi node (node_id=1) ───────────────────────────────────────────────────


def test_wifi_network_type():
    ni = _extract_network_info(_node_attrs(1))
    assert ni.network_type == "wifi"


def test_wifi_mac_address():
    ni = _extract_network_info(_node_attrs(1))
    # 6-byte MAC: "ZOgzwgV8" base64 → 64:E8:33:C2:05:7C
    assert ni.mac_address is not None
    parts = ni.mac_address.split(":")
    assert len(parts) == 6
    assert all(len(p) == 2 for p in parts)


def test_wifi_interface_name():
    ni = _extract_network_info(_node_attrs(1))
    assert ni.interface_name == "WIFI_STA_DEF"


def test_wifi_ipv4():
    ni = _extract_network_info(_node_attrs(1))
    # 0/51/0[0]["5"] = ["wKgAPg=="] → 192.168.0.62
    assert len(ni.ipv4_addresses) >= 1
    assert all("." in ip for ip in ni.ipv4_addresses)


def test_wifi_ipv6():
    ni = _extract_network_info(_node_attrs(1))
    assert len(ni.ipv6_addresses) >= 1
    assert all(":" in ip for ip in ni.ipv6_addresses)


def test_wifi_bssid():
    ni = _extract_network_info(_node_attrs(1))
    # 0/54/0 = "jO3heDZx" → 8C:ED:E1:78:36:71
    assert ni.bssid is not None
    parts = ni.bssid.split(":")
    assert len(parts) == 6


def test_wifi_channel():
    ni = _extract_network_info(_node_attrs(1))
    assert ni.channel == 6  # 0/54/3


def test_wifi_rssi():
    ni = _extract_network_info(_node_attrs(1))
    assert ni.rssi == -61  # 0/54/4


def test_wifi_version():
    ni = _extract_network_info(_node_attrs(1))
    assert ni.wifi_version == "802.11n"  # 0/54/2 = 3


def test_wifi_ssid():
    ni = _extract_network_info(_node_attrs(1))
    # 0/49/6 = "fiBWYW5ha29waSB+" → "~ Vanakopai ~" or similar UTF-8
    assert ni.ssid is not None
    assert len(ni.ssid) > 0


def test_wifi_no_thread_fields():
    ni = _extract_network_info(_node_attrs(1))
    assert ni.thread_channel is None
    assert ni.thread_role is None
    assert ni.thread_network_name is None


# ── Thread node (node_id=4) ──────────────────────────────────────────────────


def test_thread_network_type():
    ni = _extract_network_info(_node_attrs(4))
    assert ni.network_type == "thread"


def test_thread_mac_address():
    ni = _extract_network_info(_node_attrs(4))
    # "AlI2PwiXfOQ=" = 8 bytes = Thread extended MAC
    assert ni.mac_address is not None
    parts = ni.mac_address.split(":")
    assert len(parts) == 8  # Thread ext addr is 8 bytes


def test_thread_channel():
    ni = _extract_network_info(_node_attrs(4))
    assert ni.thread_channel == 25  # 0/53/0


def test_thread_routing_role():
    ni = _extract_network_info(_node_attrs(4))
    assert ni.thread_role == "Router"  # 0/53/1 = 5


def test_thread_network_name():
    ni = _extract_network_info(_node_attrs(4))
    assert ni.thread_network_name == "MyHome500889113"  # 0/53/2


def test_thread_pan_id():
    ni = _extract_network_info(_node_attrs(4))
    assert ni.thread_pan_id == 39985  # 0/53/3


def test_thread_extended_pan_id():
    ni = _extract_network_info(_node_attrs(4))
    assert ni.thread_extended_pan_id == 5789852342135114147  # 0/53/4


def test_thread_mesh_local_prefix():
    ni = _extract_network_info(_node_attrs(4))
    # "QP1GWiPwCOZE" base64 → 9 bytes → IPv6 /72 prefix
    assert ni.thread_mesh_local_prefix is not None
    assert "/" in ni.thread_mesh_local_prefix  # has prefix length


def test_thread_no_wifi_fields():
    ni = _extract_network_info(_node_attrs(4))
    assert ni.ssid is None
    assert ni.bssid is None
    assert ni.rssi is None
    assert ni.channel is None


# ── No diagnostic clusters ────────────────────────────────────────────────────


def test_no_clusters_returns_empty_networkinfo():
    """Node with no diagnostic clusters returns all-None NetworkInfo; no exception."""
    ni = _extract_network_info({})
    assert isinstance(ni, NetworkInfo)
    assert ni.network_type is None
    assert ni.mac_address is None
    assert ni.ipv4_addresses == []
    assert ni.ipv6_addresses == []
    assert ni.ssid is None
    assert ni.thread_channel is None


def test_empty_interfaces_returns_empty_networkinfo():
    """Empty interface list → all-None NetworkInfo."""
    ni = _extract_network_info({"0/51/0": []})
    assert ni.network_type is None
    assert ni.mac_address is None


def test_non_operational_iface_used_as_fallback():
    """When no operational interface exists, fall back to the first one."""
    attrs = {
        "0/51/0": [
            {"0": "eth0", "1": False, "4": "AQIDBAUG", "5": [], "6": [], "7": 2},
        ]
    }
    ni = _extract_network_info(attrs)
    assert ni.network_type == "ethernet"
    assert ni.interface_name == "eth0"


def test_type_zero_unspecified_yields_unknown_network_type():
    """Type=0 (Unspecified) interface → network_type='unknown', not None.

    Reproduces the Roborock Saros10R pattern where all interfaces have Type=0
    but still carry useful MAC / IP / uptime data.
    """
    attrs = {
        "0/51/0": [
            {
                "0": "wlan0",
                "1": True,
                "4": "JJ59HMHB",  # base64 → 24:9E:7D:1C:C1:C1 (6 bytes)
                "5": ["wKgATA=="],  # 192.168.0.76
                "6": [],
                "7": 0,  # Unspecified
            }
        ],
        "0/51/1": 140,  # reboot count
        "0/51/2": 86400,  # uptime
        "0/51/7": [],
    }
    ni = _extract_network_info(attrs)
    assert ni.network_type == "unknown"
    assert ni.interface_name == "wlan0"
    assert ni.mac_address is not None
    assert ni.ipv4_addresses == ["192.168.0.76"]
    assert ni.reboot_count == 140
    assert ni.uptime_seconds == 86400
    # WiFi/Thread specific fields must stay empty
    assert ni.ssid is None
    assert ni.bssid is None
    assert ni.thread_channel is None


def test_missing_type_field_yields_unknown():
    """When the type field is absent entirely, network_type is 'unknown'."""
    attrs = {
        "0/51/0": [{"0": "eth0", "1": True, "4": "AQIDBAUG", "5": [], "6": []}]
        # no "7" key
    }
    ni = _extract_network_info(attrs)
    assert ni.network_type == "unknown"
    assert ni.interface_name == "eth0"


# ── New fields: is_operational, uptime, reboot_count, faults ─────────────────


def test_is_operational_wifi():
    ni = _extract_network_info(_node_attrs(1))
    # fixture node 1 has "1": True on its interface
    assert ni.is_operational is True


def test_is_operational_thread():
    ni = _extract_network_info(_node_attrs(4))
    assert ni.is_operational is True


def test_uptime_seconds_wifi():
    ni = _extract_network_info(_node_attrs(1))
    # fixture: 0/51/2 = 5032587
    assert ni.uptime_seconds == 5032587


def test_uptime_humanized_wifi():
    ni = _extract_network_info(_node_attrs(1))
    # 5032587s ≈ 58d 5h 56m - just check it's a non-empty string with 'd'
    assert ni.uptime_humanized is not None
    assert "d" in ni.uptime_humanized


def test_reboot_count_wifi():
    ni = _extract_network_info(_node_attrs(1))
    # fixture: 0/51/1 = 46
    assert ni.reboot_count == 46


def test_reboot_count_thread():
    ni = _extract_network_info(_node_attrs(4))
    # fixture: 0/51/1 = 28
    assert ni.reboot_count == 28


def test_active_network_faults_empty():
    ni = _extract_network_info(_node_attrs(4))
    # fixture: 0/51/7 = []
    assert ni.active_network_faults == []


def test_active_network_faults_present():
    attrs = {
        "0/51/0": [{"0": "eth0", "1": True, "4": "AQIDBAUG", "5": [], "6": [], "7": 2}],
        "0/51/7": [2, 3],
    }
    ni = _extract_network_info(attrs)
    assert ni.active_network_faults == [2, 3]


# ── Thread counters ────────────────────────────────────────────────────────────


def test_thread_counters_present():
    ni = _extract_network_info(_node_attrs(4))
    # fixture has 0/53/22 (Tx total) and 0/53/39 (Rx total) as large ints
    assert "Tx total" in ni.thread_counters
    assert "Rx total" in ni.thread_counters
    assert isinstance(ni.thread_counters["Tx total"], int)


def test_thread_counters_partition_id():
    ni = _extract_network_info(_node_attrs(4))
    assert "Partition ID" not in ni.thread_counters
    assert "Partition ID" in ni.thread_network_attrs
    assert ni.thread_network_attrs["Partition ID"] == 381775150


def test_wifi_has_no_thread_counters():
    ni = _extract_network_info(_node_attrs(1))
    assert ni.thread_counters == {}


# ── _node_network_fields adapter (A.1 regression) ────────────────────────────
# These tests exercise the adapter that reads from a live MatterNode-like object
# via node.node_data.attributes. They would have caught the bug where
# getattr(node, "attributes", …) silently returned {} because MatterNode stores
# the attribute dict on node_data, not directly on node.


class _FakeNodeData:
    def __init__(self, attributes: dict):
        self.attributes = attributes


class _FakeNode:
    def __init__(self, attributes: dict):
        self.node_data = _FakeNodeData(attributes)


def test_node_network_fields_wifi_reads_node_data():
    """_node_network_fields must reach node.node_data.attributes (A.1)."""
    fields = _node_network_fields(_FakeNode(_node_attrs(1)))
    assert fields["network_type"] == ["wifi"]
    assert fields["mac_address"] is not None
    assert isinstance(fields["network_info"], NetworkInfo)
    assert fields["network_info"].interface_name == "WIFI_STA_DEF"


def test_node_network_fields_thread_reads_node_data():
    fields = _node_network_fields(_FakeNode(_node_attrs(4)))
    assert fields["network_type"] == ["thread"]
    assert fields["network_info"].thread_network_name == "MyHome500889113"


def test_node_network_fields_missing_node_data_returns_none():
    """A node with no node_data (e.g. broken object) must not raise; returns empty fields."""

    class _Broken:
        pass  # no node_data attribute

    fields = _node_network_fields(_Broken())
    assert fields["network_type"] == []
    assert fields["mac_address"] is None
    # attrs falls back to {} so network_info is an empty (but valid) NetworkInfo
    assert isinstance(fields["network_info"], NetworkInfo)
    assert fields["network_info"].interface_name is None


def test_node_network_fields_empty_attributes_returns_none_type():
    """Empty attribute dict → no interface → network_type is empty list."""
    fields = _node_network_fields(_FakeNode({}))
    assert fields["network_type"] == []
    assert fields["network_info"].interface_name is None
