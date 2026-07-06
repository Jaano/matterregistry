"""
Matter Server integration tests.

Tests run against the live deployment at TEST_TARGET.

WS-dependent tests require MATTER_SERVER_WS_URL to point at a real
python-matter-server WebSocket endpoint (e.g. ws://localhost:5580/ws).
They skip when the env var is not set.
"""

from __future__ import annotations

import os
import time
from typing import Any

import httpx
import pytest

TARGET = os.environ.get("TEST_TARGET")
MATTER_SERVER_WS_URL = os.environ.get("PYTHON_MATTER_SERVER", "")
API = f"{TARGET}/api/integrations"


# ── Fixtures / helpers ────────────────────────────────────────────────────────


def _skip_if_unreachable():
    if not TARGET:
        pytest.skip("TEST_TARGET not set")
    try:
        httpx.get(f"{TARGET}/healthz", timeout=5)
    except httpx.ConnectError:
        pytest.skip(f"target unreachable: {TARGET}")


def _skip_if_no_matter_server():
    if not MATTER_SERVER_WS_URL:
        pytest.skip("PYTHON_MATTER_SERVER not set - skipping WS-dependent test")


def _post(path: str, **kw: Any):
    return httpx.post(f"{API}{path}", timeout=30, **kw)


def _get(path: str = "", **kw: Any):
    return httpx.get(f"{API}{path}", timeout=10, **kw)


def _wait_for_status(expected: str, timeout: int = 10) -> str:
    """Poll until status matches expected or timeout. Returns actual status."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = _get().json()["matter_server"]["status"]
        if status == expected:
            return status
        time.sleep(1)
    return _get().json()["matter_server"]["status"]


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_import_apply_creates_devices():
    _skip_if_unreachable()
    _skip_if_no_matter_server()
    _wait_for_status("connected", timeout=15)

    r = _post("/matter-server/import/apply")
    assert r.status_code == 200, r.text
    data = r.json()
    assert "summary" in data

    devices_r = httpx.get(f"{TARGET}/api/devices", timeout=10)
    assert devices_r.status_code == 200

    # Idempotency: apply again → 0 creates
    r2 = _post("/matter-server/import/apply")
    assert r2.status_code == 200
    assert r2.json()["summary"]["create"] == 0


def test_import_preserves_user_edits():
    _skip_if_unreachable()
    _skip_if_no_matter_server()
    _wait_for_status("connected", timeout=15)
    _post("/matter-server/import/apply")

    devices_r = httpx.get(f"{TARGET}/api/devices", timeout=10)
    devs = devices_r.json()
    if not devs:
        pytest.skip("no devices in Matter Server to test with")
    dev_id = devs[0]["id"]
    original_name = devs[0]["name"]

    custom_name = "Integration Test Rename"
    try:
        httpx.patch(f"{TARGET}/api/devices/{dev_id}", json={"name": custom_name}, timeout=10)

        _post("/matter-server/import/apply")

        dev_r = httpx.get(f"{TARGET}/api/devices/{dev_id}", timeout=10)
        assert dev_r.json()["name"] == custom_name
    finally:
        httpx.patch(f"{TARGET}/api/devices/{dev_id}", json={"name": original_name}, timeout=10)
