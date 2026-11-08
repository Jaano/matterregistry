"""HA registry template: parents of child devices are listed, older HA is unchanged."""

from __future__ import annotations

import json

import jinja2

from app.integrations.ha.client import _REGISTRY_TEMPLATE


def _render(entity_device: dict[str, str | None], devices: dict[str, dict]) -> list[dict]:
    """Render the real template against a fake registry (jinja2 stands in for HA)."""

    def device_id(entity_id: str) -> str | None:
        return entity_device.get(entity_id)

    def device_attr(did: str, attr: str):
        return devices.get(did, {}).get(attr)

    def area_name(did: str):
        return devices.get(did, {}).get("area_name")

    env = jinja2.Environment()
    env.filters["to_json"] = json.dumps
    states = [type("S", (), {"entity_id": e})() for e in entity_device]
    out = env.from_string(_REGISTRY_TEMPLATE).render(
        states=states, device_id=device_id, device_attr=device_attr, area_name=area_name
    )
    return json.loads(out)


def test_legacy_registry_without_parent_field_is_unchanged():
    devices = {
        "d1": {"name": "Lamp", "identifiers": [["matter", "serial_1"]]},
        "d2": {"name": "Plug", "identifiers": [["matter", "serial_2"]]},
    }
    items = _render({"light.a": "d1", "light.b": "d1", "switch.c": "d2", "sensor.x": None}, devices)
    assert [i["id"] for i in items] == ["d1", "d2"]
    assert items[0]["identifiers"] == [["matter", "serial_1"]]


def test_parent_of_child_device_is_listed_once():
    devices = {
        "parent": {
            "name": "Strip",
            "manufacturer": "Acme",
            "identifiers": [["matter", "serial_9"]],
        },
        "c1": {"name": "Outlet 1", "parent_device_id": "parent", "identifiers": [["tuya", "o1"]]},
        "c2": {"name": "Outlet 2", "parent_device_id": "parent", "identifiers": [["tuya", "o2"]]},
    }
    items = _render({"switch.o1": "c1", "switch.o2": "c2"}, devices)
    assert [i["id"] for i in items] == ["c1", "parent", "c2"]
    parent = items[1]
    assert parent["manufacturer"] == "Acme"
    assert parent["identifiers"] == [["matter", "serial_9"]]


def test_parent_with_own_entities_is_not_duplicated():
    devices = {
        "parent": {"name": "Strip", "identifiers": [["matter", "serial_9"]]},
        "c1": {"name": "Outlet 1", "parent_device_id": "parent", "identifiers": [["tuya", "o1"]]},
    }
    items = _render({"switch.o1": "c1", "sensor.p": "parent"}, devices)
    assert sorted(i["id"] for i in items) == ["c1", "parent"]


def test_entities_without_device_are_skipped():
    assert _render({"sensor.x": None}, {}) == []
