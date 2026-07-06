"""
Search + filter integration tests.

Requires TEST_TARGET env var.
Skips cleanly when the target is unreachable.
"""

import os

import httpx
import pytest

TARGET = os.environ.get("TEST_TARGET")


def _get(path, **kw):
    return httpx.get(f"{TARGET}{path}", **kw)


def _post(path, **kw):
    return httpx.post(f"{TARGET}{path}", **kw)


def _delete(path, **kw):
    return httpx.delete(f"{TARGET}{path}", **kw)


def _skip_if_unreachable():
    if not TARGET:
        pytest.skip("TEST_TARGET not set")
    try:
        httpx.get(f"{TARGET}/healthz", timeout=5)
    except httpx.ConnectError:
        pytest.skip(f"target unreachable: {TARGET}")


def test_text_search_filters_results():
    _skip_if_unreachable()

    ids = []
    try:
        # Create two devices with distinct names
        r = _post(
            "/api/devices",
            json={"name": "Kitchen Light M2Test", "room": "Kitchen", "vendor": "Acme"},
            timeout=10,
        )
        assert r.status_code == 201
        ids.append(r.json()["id"])

        r = _post(
            "/api/devices",
            json={"name": "Bedroom Sensor M2Test", "room": "Bedroom", "vendor": "Beta"},
            timeout=10,
        )
        assert r.status_code == 201
        ids.append(r.json()["id"])

        # Search by partial name
        r = _get("/api/devices", params={"q": "Kitchen"}, timeout=10)
        assert r.status_code == 200
        names = [d["name"] for d in r.json()]
        # The Kitchen device must appear; Bedroom must not (unless user has other Kitchen devices)
        assert any("Kitchen Light M2Test" in n for n in names)

        # Search by vendor
        r = _get("/api/devices", params={"q": "Beta"}, timeout=10)
        assert r.status_code == 200
        names = [d["name"] for d in r.json()]
        assert any("Bedroom Sensor M2Test" in n for n in names)

    finally:
        for id_ in ids:
            _delete(f"/api/devices/{id_}", timeout=5)


def test_status_filter():
    _skip_if_unreachable()

    ids = []
    try:
        r = _post(
            "/api/devices", json={"name": "Active Device M2Test", "status": "active"}, timeout=10
        )
        assert r.status_code == 201
        ids.append(r.json()["id"])

        r = _post(
            "/api/devices", json={"name": "Retired Device M2Test", "status": "retired"}, timeout=10
        )
        assert r.status_code == 201
        ids.append(r.json()["id"])

        # Filter by retired
        r = _get("/api/devices", params={"status": "retired"}, timeout=10)
        assert r.status_code == 200
        statuses = [d["status"] for d in r.json()]
        assert all(s == "retired" for s in statuses)
        names = [d["name"] for d in r.json()]
        assert any("Retired Device M2Test" in n for n in names)

    finally:
        for id_ in ids:
            _delete(f"/api/devices/{id_}", timeout=5)


def test_combined_text_and_status_filter():
    _skip_if_unreachable()

    ids = []
    try:
        r = _post(
            "/api/devices",
            json={"name": "Combo Active M2Test", "vendor": "ComboVendor", "status": "active"},
            timeout=10,
        )
        assert r.status_code == 201
        ids.append(r.json()["id"])

        r = _post(
            "/api/devices",
            json={"name": "Combo Retired M2Test", "vendor": "ComboVendor", "status": "retired"},
            timeout=10,
        )
        assert r.status_code == 201
        ids.append(r.json()["id"])

        r = _get("/api/devices", params={"q": "ComboVendor", "status": "active"}, timeout=10)
        assert r.status_code == 200
        results = r.json()
        names = [d["name"] for d in results]
        statuses = [d["status"] for d in results]

        assert any("Combo Active M2Test" in n for n in names)
        assert not any("Combo Retired M2Test" in n for n in names)
        assert all(s == "active" for s in statuses)

    finally:
        for id_ in ids:
            _delete(f"/api/devices/{id_}", timeout=5)


def test_search_no_results():
    _skip_if_unreachable()

    r = _get("/api/devices", params={"q": "xyzzy_no_such_device_8675309"}, timeout=10)
    assert r.status_code == 200
    assert r.json() == []


def test_device_list_page_renders_with_search():
    """Web UI device list page accepts q param and returns HTML."""
    _skip_if_unreachable()

    r = _get("/devices", params={"q": "test"}, timeout=10)
    assert r.status_code == 200
    assert "text/html" in r.headers.get("content-type", "")
