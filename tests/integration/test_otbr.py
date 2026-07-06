"""
OTBR integration tests.

Tests run against the live deployment at TEST_TARGET.
WS-dependent tests require OTBR_URL to point at a real OTBR REST endpoint
(e.g. http://homeassistant.local:8081). They skip when OTBR_URL is not set.
"""

from __future__ import annotations

import os
import time
from typing import Any

import httpx
import pytest

TARGET = os.environ.get("TEST_TARGET")
OTBR_URL = os.environ.get("OTBR_URL", "")
API = f"{TARGET}/api/integrations"


# ── Helpers ───────────────────────────────────────────────────────────────────


def _skip_if_unreachable():
    if not TARGET:
        pytest.skip("TEST_TARGET not set")
    try:
        httpx.get(f"{TARGET}/healthz", timeout=5)
    except httpx.ConnectError:
        pytest.skip(f"target unreachable: {TARGET}")


def _skip_if_no_otbr():
    if not OTBR_URL:
        pytest.skip("OTBR_URL not set - skipping OTBR-dependent test")


def _post(path: str, **kw: Any):
    return httpx.post(f"{API}{path}", timeout=30, **kw)


def _get(path: str = "", **kw: Any):
    return httpx.get(f"{API}{path}", timeout=10, **kw)


def _wait_for_status(expected: str, timeout: int = 15) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = _get().json().get("otbr", {}).get("status", "")
        if status == expected:
            return status
        time.sleep(1)
    return _get().json().get("otbr", {}).get("status", "")


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_poll_apply_creates_thread_network():
    _skip_if_unreachable()
    _skip_if_no_otbr()
    _wait_for_status("connected", timeout=15)

    r = _post("/otbr/poll/apply")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["ok"] is True
    assert data["ext_pan_id"]

    # Second apply must not fail (idempotent - update, not create)
    r2 = _post("/otbr/poll/apply")
    assert r2.status_code == 200
