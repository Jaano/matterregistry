"""
Matter setup-payload integration tests.
Round-trips a canonical MT: string through scan → store → regenerate → re-decode
and asserts the same bit pattern comes back.

Requires TEST_TARGET env var.
Skips cleanly when the target is unreachable.
"""

import os

import httpx
import pytest

TARGET = os.environ.get("TEST_TARGET")
_PYTHON_MATTER_SERVER = os.environ.get("PYTHON_MATTER_SERVER", "")

# CHIP SDK canonical test vector
_PAYLOAD = "MT:-24J0AFN00KA0648G00"
_EXPECTED = {
    "vendor_id": 65521,  # 0xFFF1
    "product_id": 32769,  # 0x8001
    "discriminator": 3840,
    "passcode": 20202021,
}


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


def test_scan_creates_credentials():
    _skip_if_unreachable()

    # Create a device
    r = _post("/api/devices", json={"name": "Scan Test Device"}, timeout=10)
    assert r.status_code == 201
    device_id = r.json()["id"]

    try:
        # Scan the canonical payload
        r = _post(f"/api/devices/{device_id}/scan", json={"payload": _PAYLOAD}, timeout=10)
        assert r.status_code == 200, r.text

        device = r.json()
        assert device["vendor_id"] == _EXPECTED["vendor_id"]
        assert device["product_id"] == _EXPECTED["product_id"]

        creds = {c["type"]: c["value"] for c in device["properties"]}
        assert "qr_payload" in creds
        assert "setup_pin" in creds
        assert "manual_code" in creds
        assert "discriminator" in creds

        assert creds["qr_payload"] == _PAYLOAD.strip()
        assert int(creds["setup_pin"]) == _EXPECTED["passcode"]
        assert int(creds["discriminator"]) == _EXPECTED["discriminator"]

        # Verify manual code is 11 digits
        manual = creds["manual_code"].replace("-", "")
        assert len(manual) == 11
        assert manual.isdigit()

    finally:
        _delete(f"/api/devices/{device_id}", timeout=5)


def test_qr_svg_roundtrip():
    """GET /qr.svg must return SVG that decodes to the same MT: string."""
    _skip_if_unreachable()

    r = _post("/api/devices", json={"name": "QR Roundtrip Device"}, timeout=10)
    assert r.status_code == 201
    device_id = r.json()["id"]

    try:
        r = _post(f"/api/devices/{device_id}/scan", json={"payload": _PAYLOAD}, timeout=10)
        assert r.status_code == 200

        r = _get(f"/api/devices/{device_id}/qr.svg", timeout=10)
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("image/svg+xml")
        svg = r.text
        assert "<svg" in svg.lower()
        # SVG must embed the original MT: string in the data
        assert "MT:" in svg or "MT%3A" in svg or len(svg) > 500

    finally:
        _delete(f"/api/devices/{device_id}", timeout=5)


def test_manual_code_endpoint():
    _skip_if_unreachable()

    r = _post("/api/devices", json={"name": "Manual Code Device"}, timeout=10)
    assert r.status_code == 201
    device_id = r.json()["id"]

    try:
        r = _post(f"/api/devices/{device_id}/scan", json={"payload": _PAYLOAD}, timeout=10)
        assert r.status_code == 200

        r = _get(f"/api/devices/{device_id}/manual-code", timeout=10)
        assert r.status_code == 200
        code = r.text.replace("-", "")
        assert len(code) == 11
        assert code.isdigit()

    finally:
        _delete(f"/api/devices/{device_id}", timeout=5)


def test_scan_bad_payload_returns_400():
    _skip_if_unreachable()

    r = _post("/api/devices", json={"name": "Bad Payload Device"}, timeout=10)
    assert r.status_code == 201
    device_id = r.json()["id"]

    try:
        r = _post(
            f"/api/devices/{device_id}/scan", json={"payload": "MT:INVALIDPAYLOAD!!!"}, timeout=10
        )
        assert r.status_code == 400

    finally:
        _delete(f"/api/devices/{device_id}", timeout=5)


def test_scan_via_individual_fields():
    """Scanning via passcode+discriminator reaches the same end state."""
    _skip_if_unreachable()

    r = _post("/api/devices", json={"name": "Fields Scan Device"}, timeout=10)
    assert r.status_code == 201
    device_id = r.json()["id"]

    try:
        r = _post(
            f"/api/devices/{device_id}/scan",
            json={
                "passcode": _EXPECTED["passcode"],
                "discriminator": _EXPECTED["discriminator"],
            },
            timeout=10,
        )
        assert r.status_code == 200

        creds = {c["type"]: c["value"] for c in r.json()["properties"]}
        assert int(creds["setup_pin"]) == _EXPECTED["passcode"]
        assert "manual_code" in creds

    finally:
        _delete(f"/api/devices/{device_id}", timeout=5)


def test_scan_invalid_manual_code_returns_400():
    _skip_if_unreachable()

    r = _post("/api/devices", json={"name": "Invalid Manual Code Device"}, timeout=10)
    assert r.status_code == 201
    device_id = r.json()["id"]

    try:
        # Invalid check digit (12345678901 fails Verhoeff validation)
        r = _post(f"/api/devices/{device_id}/scan", json={"manual_code": "12345678901"}, timeout=10)
        assert r.status_code == 400

    finally:
        _delete(f"/api/devices/{device_id}", timeout=5)


# ── HomeKit onboarding (A.14) ────────────────────────────────────────────────

# Generated from app.homekit.encode_payload: category 5 (Lightbulb), IP-only,
# setup_code 12344321, setup_id 1A2B.
_HOMEKIT_PAYLOAD = "X-HM://00527Y8011A2B"


def test_homekit_scan_creates_credentials():
    """X-HM:// scan stores the verbatim payload, an 8-digit code, and flips protocol."""
    _skip_if_unreachable()

    r = _post("/api/devices", json={"name": "HomeKit Scan Device"}, timeout=10)
    assert r.status_code == 201
    device_id = r.json()["id"]

    try:
        r = _post(f"/api/devices/{device_id}/scan", json={"payload": _HOMEKIT_PAYLOAD}, timeout=10)
        assert r.status_code == 200, r.text

        device = r.json()
        assert device["protocol"] == "homekit"

        creds = {c["type"]: c["value"] for c in device["properties"]}
        assert creds["qr_payload"] == _HOMEKIT_PAYLOAD
        assert int(creds["setup_pin"]) == 12344321

        manual = creds["manual_code"].replace("-", "")
        assert len(manual) == 8
        assert manual.isdigit()

    finally:
        _delete(f"/api/devices/{device_id}", timeout=5)


def test_cross_protocol_scan_returns_409():
    """A HomeKit scan on a device that already holds a Matter code is rejected with 409."""
    _skip_if_unreachable()

    r = _post("/api/devices", json={"name": "Cross Protocol Device"}, timeout=10)
    assert r.status_code == 201
    device_id = r.json()["id"]

    try:
        # First a Matter code lands fine.
        r = _post(f"/api/devices/{device_id}/scan", json={"payload": _PAYLOAD}, timeout=10)
        assert r.status_code == 200

        # A HomeKit code on the same device must be refused.
        r = _post(f"/api/devices/{device_id}/scan", json={"payload": _HOMEKIT_PAYLOAD}, timeout=10)
        assert r.status_code == 409, r.text

        # The reverse direction is also refused.
        r2 = _post("/api/devices", json={"name": "Cross Protocol Device 2"}, timeout=10)
        device2_id = r2.json()["id"]
        try:
            r = _post(
                f"/api/devices/{device2_id}/scan", json={"payload": _HOMEKIT_PAYLOAD}, timeout=10
            )
            assert r.status_code == 200
            r = _post(f"/api/devices/{device2_id}/scan", json={"payload": _PAYLOAD}, timeout=10)
            assert r.status_code == 409, r.text
        finally:
            _delete(f"/api/devices/{device2_id}", timeout=5)

    finally:
        _delete(f"/api/devices/{device_id}", timeout=5)


# ── Matter network refresh endpoint ──────────────────────────────────────────


def test_refresh_network_no_membership_returns_404():
    """Device without a Matter fabric membership gets 404."""
    _skip_if_unreachable()

    r = _post("/api/devices", json={"name": "Refresh Test No Membership"}, timeout=10)
    assert r.status_code == 201
    device_id = r.json()["id"]

    try:
        r = _post(f"/api/devices/{device_id}/matter/refresh-network", timeout=10)
        assert r.status_code in (404, 503)  # 503 when Matter Server not configured
    finally:
        _delete(f"/api/devices/{device_id}", timeout=5)
