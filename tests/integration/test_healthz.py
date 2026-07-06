"""
Heartbeat integration test - canonical pattern for all future integration tests.
Target host comes from TEST_TARGET env var.
Skips cleanly when the host is unreachable.
"""

import os

import httpx
import pytest

TARGET = os.environ.get("TEST_TARGET")


def test_healthz():
    if not TARGET:
        pytest.skip("TEST_TARGET not set")
    try:
        response = httpx.get(f"{TARGET}/healthz", timeout=5)
    except httpx.ConnectError:
        pytest.skip(f"target unreachable: {TARGET}")
        return  # unreachable; satisfies type-checker

    assert response.status_code == 200
    body = response.json()
    assert body.get("status") == "ok"
    assert isinstance(body.get("version"), str) and body["version"]
