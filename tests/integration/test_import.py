"""Import round-trip integration tests."""

import io
import json
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


def _export() -> dict:
    r = httpx.get(f"{TARGET}/api/export", timeout=30)
    assert r.status_code == 200
    return r.json()


def _import(payload: dict, *, policy: str = "skip", commit: bool = True) -> httpx.Response:
    blob = json.dumps(payload).encode()
    return httpx.post(
        f"{TARGET}/api/import",
        params={"policy": policy, "commit": str(commit).lower()},
        files={"file": ("backup.json", io.BytesIO(blob), "application/json")},
        timeout=30,
    )


def test_import_roundtrip():
    """Export → delete device → import → device and properties reappear."""
    _skip_if_unreachable()

    r = _post("/api/devices", json={"name": "Import RT Device", "vendor": "Acme"}, timeout=10)
    assert r.status_code == 201
    device_id = r.json()["id"]

    try:
        _post(
            f"/api/devices/{device_id}/properties",
            json={"type": "other", "value": "rt-cred-val", "source": "manual"},
            timeout=10,
        )

        snapshot = _export()

        _delete(f"/api/devices/{device_id}", timeout=5)
        r = _get(f"/api/devices/{device_id}", timeout=5)
        assert r.status_code == 404

        r = _import(snapshot, policy="skip", commit=True)
        assert r.status_code == 200
        plan = r.json()
        assert not plan["errors"]
        assert any(f"device:{device_id}" in s for s in plan["creates"])

        r = _get(f"/api/devices/{device_id}", timeout=10)
        assert r.status_code == 200
        assert r.json()["vendor"] == "Acme"
        props = r.json()["properties"]
        assert any(c["value"] == "rt-cred-val" for c in props)
        # audit goes to container log - no HTTP assertion
    finally:
        _delete(f"/api/devices/{device_id}", timeout=5)


def test_import_skip_policy():
    """Existing device with skip policy goes to skips, not creates."""
    _skip_if_unreachable()

    r = _post("/api/devices", json={"name": "Skip Policy Device"}, timeout=10)
    assert r.status_code == 201
    device_id = r.json()["id"]

    try:
        snapshot = _export()

        r = _import(snapshot, policy="skip", commit=True)
        assert r.status_code == 200
        plan = r.json()
        assert any(f"device:{device_id}" in s for s in plan["skips"])
        assert not any(f"device:{device_id}" in s for s in plan["creates"])
    finally:
        _delete(f"/api/devices/{device_id}", timeout=5)


def test_import_replace_policy():
    """Replace policy overwrites existing device."""
    _skip_if_unreachable()

    r = _post("/api/devices", json={"name": "Replace Before"}, timeout=10)
    assert r.status_code == 201
    device_id = r.json()["id"]

    try:
        snapshot = _export()
        device_entry = next(d for d in snapshot["devices"] if d["id"] == device_id)
        device_entry["name"] = "Replace After"

        r = _import(snapshot, policy="replace", commit=True)
        assert r.status_code == 200
        plan = r.json()
        assert not plan["errors"]
        assert any(f"device:{device_id}" in s for s in plan["updates"])

        r = _get(f"/api/devices/{device_id}", timeout=10)
        assert r.json()["name"] == "Replace After"
    finally:
        _delete(f"/api/devices/{device_id}", timeout=5)


def test_import_bad_format_version():
    """Missing / wrong format_version returns 422 with errors."""
    _skip_if_unreachable()

    bad = {"format_version": 99, "devices": []}
    r = _import(bad, commit=True)
    assert r.status_code == 422


def test_import_v3_backup_settings_key_ignored():
    """A v3 backup with a 'settings' array is imported successfully (settings silently ignored)."""
    _skip_if_unreachable()

    v3_backup = {
        "format_version": 3,
        "devices": [],
        "settings": [{"key": "matter_server.url", "value": "ws://old-host:5580/ws"}],
    }
    r = _import(v3_backup, commit=False)
    assert r.status_code == 200
    body = r.json()
    assert not body.get("errors"), f"Unexpected errors: {body.get('errors')}"


def test_import_sha256_mismatch():
    """Attachment with wrong sha256 is caught and returns 422."""
    _skip_if_unreachable()

    r = _post("/api/devices", json={"name": "SHA Mismatch Device"}, timeout=10)
    assert r.status_code == 201
    device_id = r.json()["id"]

    try:
        snapshot = _export()
        device_entry = next(d for d in snapshot["devices"] if d["id"] == device_id)
        device_entry["attachments"] = [
            {
                "id": "00000000-0000-0000-0000-000000000001",
                "device_id": device_id,
                "kind": "image",
                "filename": "bad.png",
                "mime_type": "image/png",
                "sha256": "aaaa",
                "size_bytes": 3,
                "content_b64": "AAEC",
            }
        ]

        _delete(f"/api/devices/{device_id}", timeout=5)
        device_id = None

        r = _import(snapshot, commit=True)
        assert r.status_code == 422
    finally:
        if device_id:
            _delete(f"/api/devices/{device_id}", timeout=5)
