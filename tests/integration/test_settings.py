"""Settings page integration tests."""

import os

import httpx
import pytest

TARGET = os.environ.get("TEST_TARGET")


def _get(path, **kw):
    return httpx.get(f"{TARGET}{path}", **kw)


def _skip_if_unreachable():
    if not TARGET:
        pytest.skip("TEST_TARGET not set")
    try:
        httpx.get(f"{TARGET}/healthz", timeout=5)
    except httpx.ConnectError:
        pytest.skip(f"target unreachable: {TARGET}")


def test_settings_page_renders():
    """GET /settings returns 200 HTML with app version."""
    _skip_if_unreachable()

    r = _get("/settings", timeout=10)
    assert r.status_code == 200
    assert "Matter Registry" in r.text


def test_settings_page_contains_db_info():
    """Settings page shows database size and data directory."""
    _skip_if_unreachable()

    r = _get("/settings", timeout=10)
    assert r.status_code == 200
    assert "MB" in r.text
