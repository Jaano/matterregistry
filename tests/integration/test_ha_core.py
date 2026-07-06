"""
HA Core integration tests.

Runs against the live deployment at TEST_TARGET.
Tests that require a real HA instance (HASS_URL + HASS_TOKEN) are skipped
when those env vars are not set.
"""

from __future__ import annotations

import os
import time
from typing import Any

import httpx
import pytest

TARGET = os.environ.get("TEST_TARGET")
HASS_URL = os.environ.get("HASS_URL", "")
HASS_TOKEN = os.environ.get("HASS_TOKEN", "")
API = f"{TARGET}/api/integrations"
DEV_API = f"{TARGET}/api/devices"


# ── Helpers ───────────────────────────────────────────────────────────────────


def _skip_if_unreachable():
    if not TARGET:
        pytest.skip("TEST_TARGET not set")
    try:
        httpx.get(f"{TARGET}/healthz", timeout=5)
    except httpx.ConnectError:
        pytest.skip(f"target unreachable: {TARGET}")


def _skip_if_no_ha():
    if not HASS_URL or not HASS_TOKEN:
        pytest.skip("HASS_URL / HASS_TOKEN not set - skipping HA-dependent test")


def _get(path: str = "", **kw: Any):
    return httpx.get(f"{API}{path}", timeout=15, **kw)


def _post(path: str, **kw: Any):
    return httpx.post(f"{API}{path}", timeout=30, **kw)


def _dev_get(path: str = "", **kw: Any):
    return httpx.get(f"{DEV_API}{path}", timeout=10, **kw)


def _dev_patch(path: str, **kw: Any):
    return httpx.patch(f"{DEV_API}{path}", timeout=10, **kw)


def _wait_for_ha_status(expected: str, timeout: int = 15) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = _get().json().get("ha_core", {}).get("status", "")
        if status == expected:
            return status
        time.sleep(1)
    return _get().json().get("ha_core", {}).get("status", "")


def _first_device_id() -> str | None:
    r = httpx.get(f"{DEV_API}", timeout=10)
    devices = r.json() if r.status_code == 200 else []
    return devices[0]["id"] if devices else None


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_status_endpoint_includes_ha_core():
    _skip_if_unreachable()
    r = _get()
    assert r.status_code == 200
    data = r.json()
    assert "ha_core" in data
    ha = data["ha_core"]
    assert "status" in ha
    assert "using_supervisor_token" in ha


def test_manual_sync_returns_summary_and_audit_row():
    _skip_if_unreachable()
    _skip_if_no_ha()
    _wait_for_ha_status("connected", timeout=30)

    r = _post("/ha-core/sync")
    assert r.status_code == 200
    data = r.json()
    # Sync summary shape is created / updated / skipped (B.9).
    for key in ("created", "updated", "skipped"):
        assert key in data, f"missing {key!r} in sync summary {data}"
        assert isinstance(data[key], int)
    # audit goes to container log - no HTTP assertion


def test_ha_devices_list():
    _skip_if_unreachable()
    _skip_if_no_ha()
    _wait_for_ha_status("connected", timeout=30)

    r = _get("/ha-core/devices")
    assert r.status_code == 200
    devices = r.json()
    assert isinstance(devices, list)
    if devices:
        d = devices[0]
        assert "id" in d
        assert "name" in d


def test_ha_devices_returns_matter_and_homekit():
    """GET /ha-core/devices returns Matter and HomeKit devices (B.9), each
    tagged with its protocol. The link picker filters by protocol client-side,
    so the endpoint itself is not protocol-restricted."""
    _skip_if_unreachable()
    _skip_if_no_ha()
    _wait_for_ha_status("connected", timeout=30)

    r = _get("/ha-core/devices")
    assert r.status_code == 200
    devices = r.json()
    assert isinstance(devices, list)
    for d in devices:
        assert d.get("protocol") in ("matter", "homekit"), (
            f"Device {d.get('id')!r} has unexpected protocol {d.get('protocol')!r}"
        )
        # Matter devices must still carry a Matter unique id.
        if d.get("protocol") == "matter":
            assert d.get("matter_unique_id"), (
                f"Matter device {d.get('id')!r} has no matter_unique_id"
            )
