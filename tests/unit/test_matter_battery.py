"""Power Source battery facts read from the Matter Server attribute cache."""

from __future__ import annotations

import json
from types import SimpleNamespace

from app.integrations.matter_server.server_client import (
    NodeInfo,
    _extract_batteries,
    _node_info_from_record,
    battery_summary,
)


def test_extracts_replacement_facts_per_endpoint():
    attrs = {
        "1/47/19": "2x CR2032 coin cell",
        "1/47/20": 76,  # BatCommonDesignationEnum 76 = 18650
        "1/47/21": "5004LC",
        "1/47/22": "CR2032",
        "1/47/25": 2,
        "3/47/20": 2,  # AA on a second source
        "3/47/25": 4,
        "0/40/1": "Acme",  # unrelated cluster ignored
    }
    assert _extract_batteries(attrs) == [
        {
            "endpoint": 1,
            "replacement_description": "2x CR2032 coin cell",
            "common_designation": "18650",
            "ansi_designation": "5004LC",
            "iec_designation": "CR2032",
            "quantity": 2,
        },
        {"endpoint": 3, "common_designation": "AA", "quantity": 4},
    ]


def test_null_unspecified_and_blank_values_are_dropped():
    attrs = {
        "1/47/19": None,
        "1/47/20": 0,  # Unspecified
        "1/47/21": "   ",
        "1/47/22": "",
        "1/47/25": None,
    }
    assert _extract_batteries(attrs) == []


def test_unknown_designation_is_kept_visibly():
    assert _extract_batteries({"1/47/20": 999}) == [
        {"endpoint": 1, "common_designation": "unknown (999)"}
    ]


def test_ignores_malformed_keys_and_wrong_types():
    attrs = {"x/47/20": 2, "1/47/y": 2, "1/47": 2, "1/47/25": "two", "1/47/19": 5}
    assert _extract_batteries(attrs) == []


def test_battery_summary_formats_all_parts():
    summary = battery_summary(
        {
            "endpoint": 1,
            "common_designation": "AA",
            "quantity": 2,
            "iec_designation": "LR6",
            "ansi_designation": "15A",
            "replacement_description": "Alkaline preferred",
        }
    )
    assert summary == "ep 1: 2x AA, IEC LR6, ANSI 15A, Alkaline preferred"


def test_staging_record_round_trips_batteries():
    batteries = [{"endpoint": 1, "common_designation": "AAA", "quantity": 2}]
    record = SimpleNamespace(
        node_id=7,
        available=True,
        vendor_id=0xFFF1,
        vendor_name="Acme",
        product_id=1,
        product_name="Sensor",
        serial=None,
        hardware_version_string=None,
        firmware_version_string=None,
        node_label=None,
        unique_id=None,
        manufacturing_date=None,
        product_url=None,
        part_number=None,
        date_commissioned=None,
        is_bridge=False,
        product_label=None,
        product_appearance_json=None,
        batteries_json=json.dumps(batteries),
        spec_version_int=None,
        hardware_version_int=None,
        software_version_int=None,
        network_type_json="[]",
        mac_address=None,
        ip_addresses_json="[]",
        endpoint_json="{}",
    )
    node = _node_info_from_record(record)
    assert isinstance(node, NodeInfo)
    assert node.batteries == batteries


def test_staging_record_without_batteries_gives_empty_list():
    record = SimpleNamespace(
        node_id=7,
        available=True,
        vendor_id=None,
        vendor_name=None,
        product_id=None,
        product_name=None,
        serial=None,
        hardware_version_string=None,
        firmware_version_string=None,
        node_label=None,
        unique_id=None,
        manufacturing_date=None,
        product_url=None,
        part_number=None,
        date_commissioned=None,
        is_bridge=False,
        product_label=None,
        product_appearance_json=None,
        batteries_json=None,
        spec_version_int=None,
        hardware_version_int=None,
        software_version_int=None,
        network_type_json="[]",
        mac_address=None,
        ip_addresses_json="[]",
        endpoint_json="{}",
    )
    assert _node_info_from_record(record).batteries == []
