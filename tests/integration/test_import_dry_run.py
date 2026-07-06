"""Import dry-run integration tests."""

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


def _dry_import(payload: dict, policy: str = "skip") -> httpx.Response:
    blob = json.dumps(payload).encode()
    return httpx.post(
        f"{TARGET}/api/import",
        params={"policy": policy},
        files={"file": ("backup.json", io.BytesIO(blob), "application/json")},
        timeout=30,
    )


def test_dry_run_returns_plan_without_writing():
    """POST /api/import without commit=true returns a plan and does not create devices."""
    _skip_if_unreachable()

    before = _get("/api/devices", timeout=10).json()
    before_ids = {d["id"] for d in before}

    payload = {
        "format_version": 1,
        "devices": [
            {
                "id": "aaaaaaaa-0000-0000-0000-000000000001",
                "name": "Dry Run Device",
                "status": "active",
                "properties": [],
                "attachments": [],
            }
        ],
    }

    r = _dry_import(payload)
    assert r.status_code == 200
    plan = r.json()
    assert "creates" in plan
    assert "updates" in plan
    assert "skips" in plan
    assert "errors" in plan
    assert not plan["errors"]
    assert any("aaaaaaaa-0000-0000-0000-000000000001" in s for s in plan["creates"])

    after = _get("/api/devices", timeout=10).json()
    after_ids = {d["id"] for d in after}
    assert after_ids == before_ids, "Dry run must not create any rows"


def test_dry_run_invalid_payload():
    """Dry run with invalid payload still returns errors without writing."""
    _skip_if_unreachable()

    before_ids = {d["id"] for d in _get("/api/devices", timeout=10).json()}

    bad = {"format_version": 42, "devices": []}
    r = _dry_import(bad)
    assert r.status_code == 200
    plan = r.json()
    assert plan["errors"]

    after_ids = {d["id"] for d in _get("/api/devices", timeout=10).json()}
    assert after_ids == before_ids


# ── schema_version warning tests ──────────────────────────────────────────────


def _live_export() -> dict:
    r = httpx.get(f"{TARGET}/api/export", timeout=30)
    assert r.status_code == 200
    return r.json()


def test_schema_version_same_no_warning():
    """Backup with matching schema_version produces no warnings."""
    _skip_if_unreachable()

    payload = _live_export()
    assert "schema_version" in payload, "Exporter must include schema_version"

    r = _dry_import(payload)
    assert r.status_code == 200
    plan = r.json()
    assert "warnings" in plan
    assert not plan["errors"]
    # Matching schema_version → no schema warning
    assert not any("schema" in w.lower() for w in plan["warnings"])


def test_schema_version_older_produces_warning():
    """Backup with an older schema_version produces a warning but no errors."""
    _skip_if_unreachable()

    payload = _live_export()
    payload["schema_version"] = "0001"  # deliberately old

    r = _dry_import(payload)
    assert r.status_code == 200
    plan = r.json()
    assert not plan["errors"]
    assert any("0001" in w for w in plan["warnings"]), (
        "Expected a warning mentioning the old schema_version"
    )


def test_schema_version_newer_produces_warning():
    """Backup with a schema_version ahead of the running DB produces a warning but no errors."""
    _skip_if_unreachable()

    payload = _live_export()
    payload["schema_version"] = "9999"  # future revision

    r = _dry_import(payload)
    assert r.status_code == 200
    plan = r.json()
    assert not plan["errors"]
    assert any("9999" in w for w in plan["warnings"]), (
        "Expected a warning mentioning the future schema_version"
    )


def test_schema_version_missing_produces_warning():
    """Legacy backup without schema_version produces a warning but no errors."""
    _skip_if_unreachable()

    payload = _live_export()
    payload.pop("schema_version", None)

    r = _dry_import(payload)
    assert r.status_code == 200
    plan = r.json()
    assert not plan["errors"]
    assert plan["warnings"], "Expected at least one warning for missing schema_version"
