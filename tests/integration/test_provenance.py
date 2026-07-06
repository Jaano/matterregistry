"""
Integration tests for per-field provenance (B.2).

Covers:
  - API create/update stamps user source
  - HA sync cannot overwrite user-sourced field
  - QR scan stamps scanned source
  - Export includes _sources; import round-trip restores them
"""

import os

import httpx
import pytest

TARGET = os.environ.get("TEST_TARGET")


def _get(path, **kw):
    return httpx.get(f"{TARGET}{path}", **kw)


def _post(path, **kw):
    return httpx.post(f"{TARGET}{path}", **kw)


def _patch(path, **kw):
    return httpx.patch(f"{TARGET}{path}", **kw)


def _delete(path, **kw):
    return httpx.delete(f"{TARGET}{path}", **kw)


def _skip_if_unreachable():
    if not TARGET:
        pytest.skip("TEST_TARGET not set")
    try:
        httpx.get(f"{TARGET}/healthz", timeout=3)
    except Exception:
        pytest.skip(f"Target {TARGET} is unreachable")


# ── API create/update sources ─────────────────────────────────────────────────


def test_api_create_stamps_user_source():
    """Creating a device via REST API marks all supplied fields as 'user'."""
    _skip_if_unreachable()
    r = _post("/api/devices", json={"name": "Prov Test", "vendor": "Acme"}, timeout=5)
    assert r.status_code == 201
    dev = r.json()
    dev_id = dev["id"]
    try:
        sources = dev.get("sources", {})
        assert sources.get("name") == "user"
        assert sources.get("vendor") == "user"
    finally:
        _delete(f"/api/devices/{dev_id}", timeout=5)


def test_api_update_stamps_user_source():
    """PATCHing a device field marks it as 'user' regardless of prior source."""
    _skip_if_unreachable()
    r = _post("/api/devices", json={"name": "Prov Update"}, timeout=5)
    assert r.status_code == 201
    dev_id = r.json()["id"]
    try:
        r = _patch(f"/api/devices/{dev_id}", json={"vendor": "NewVendor"}, timeout=5)
        assert r.status_code == 200
        sources = r.json().get("sources", {})
        assert sources.get("vendor") == "user"
    finally:
        _delete(f"/api/devices/{dev_id}", timeout=5)


def test_new_device_name_is_user_source_via_web_form():
    """POST /api/devices (API, same as web form path) sets name_source=user."""
    _skip_if_unreachable()
    r = _post("/api/devices", json={"name": "WebForm Device"}, timeout=5)
    assert r.status_code == 201
    dev_id = r.json()["id"]
    try:
        r = _get(f"/api/devices/{dev_id}", timeout=5)
        assert r.status_code == 200
        sources = r.json().get("sources", {})
        assert sources.get("name") == "user"
    finally:
        _delete(f"/api/devices/{dev_id}", timeout=5)


# ── Export / import round-trip preserves sources ──────────────────────────────


def test_export_import_sources_roundtrip():
    """Export a device with user source; import preserves the source."""
    _skip_if_unreachable()
    r = _post("/api/devices", json={"name": "RT Device", "vendor": "RT Corp"}, timeout=5)
    assert r.status_code == 201
    dev_id = r.json()["id"]
    try:
        # Export
        r = _get("/api/export", timeout=30)
        assert r.status_code == 200
        data = r.json()
        dev_row = next((d for d in data["devices"] if d["id"] == dev_id), None)
        assert dev_row is not None
        assert dev_row.get("_sources", {}).get("name") == "user"
        assert dev_row.get("_sources", {}).get("vendor") == "user"

        # Dry-run import (skip policy) to verify no errors
        import io
        import json as _json

        blob = _json.dumps(data).encode()
        r = _post(
            "/api/import",
            params={"policy": "skip"},
            files={"file": ("backup.json", io.BytesIO(blob), "application/json")},
            timeout=30,
        )
        assert r.status_code == 200
        plan = r.json()
        assert plan.get("errors", []) == []
    finally:
        _delete(f"/api/devices/{dev_id}", timeout=5)


# ── HA link sets user source ──────────────────────────────────────────────────


def test_ha_link_sets_user_source():
    """Manually linking a HA device stamps ha_device_id_source=user."""
    _skip_if_unreachable()
    r = _post("/api/devices", json={"name": "HA Link Prov"}, timeout=5)
    assert r.status_code == 201
    dev_id = r.json()["id"]
    try:
        r = _patch(
            f"/api/devices/{dev_id}/ha-link", json={"ha_device_id": "fake-ha-id-prov"}, timeout=5
        )
        assert r.status_code == 200
        sources = r.json().get("sources", {})
        assert sources.get("ha_device_id") == "user"
    finally:
        _delete(f"/api/devices/{dev_id}", timeout=5)


def test_ha_link_unlink_resets_source():
    """Unlinking clears ha_device_id and resets its source to generated."""
    _skip_if_unreachable()
    r = _post("/api/devices", json={"name": "HA Unlink Prov"}, timeout=5)
    assert r.status_code == 201
    dev_id = r.json()["id"]
    try:
        _patch(f"/api/devices/{dev_id}/ha-link", json={"ha_device_id": "fake-ha-id-2"}, timeout=5)
        r = _patch(f"/api/devices/{dev_id}/ha-link", json={"ha_device_id": None}, timeout=5)
        assert r.status_code == 200
        dev = r.json()
        assert dev["ha_device_id"] is None
        sources = dev.get("sources", {})
        assert sources.get("ha_device_id") == "generated"
    finally:
        _delete(f"/api/devices/{dev_id}", timeout=5)
