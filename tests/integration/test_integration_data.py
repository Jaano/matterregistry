"""Integration tests for B.12 per-device integration data.

Verifies over HTTP:
- the Integrations-data tile is absent on a fresh device (no spurious rendering)
- export payload never contains DeviceIntegrationData rows

The upsert/read/ordering unit tests live in tests/unit/test_integration_data.py.
End-to-end tile population is covered by the per-integration sync tests
(test_ha_core.py, test_mdns_discovery unit tests, etc.) when those integrations
are configured.
"""

from __future__ import annotations

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


def test_integration_data_tile_absent_on_fresh_device():
    """Device-detail page must not show the Integration-data tile when no data exists."""
    _skip_if_unreachable()

    r = _post("/api/devices", json={"name": "B12 Tile Test"}, timeout=10)
    assert r.status_code == 201
    device_id = r.json()["id"]

    try:
        r = _get(f"/devices/{device_id}", timeout=10)
        assert r.status_code == 200
        assert "Integration data" not in r.text
    finally:
        _delete(f"/api/devices/{device_id}", timeout=5)


def test_export_excludes_integration_data():
    """GET /api/export must not include DeviceIntegrationData rows or their payloads."""
    _skip_if_unreachable()

    r = _get("/api/export", timeout=30)
    assert r.status_code == 200

    body = r.text
    assert "device_integration_data" not in body
    assert "integration_data" not in body
    # No per-device integration-data key should appear in any device object
    data = r.json()
    for device in data.get("devices", []):
        assert "integration_data" not in device
        assert "device_integration_data" not in device
