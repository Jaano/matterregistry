"""
Device CRUD integration tests.
Requires TEST_TARGET env var.
Skips cleanly when the target is unreachable.
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
        httpx.get(f"{TARGET}/healthz", timeout=5)
    except httpx.ConnectError:
        pytest.skip(f"target unreachable: {TARGET}")


def test_device_crud():
    _skip_if_unreachable()

    # Create
    r = _post(
        "/api/devices", json={"name": "Test Light", "vendor": "Acme", "room": "Lab"}, timeout=5
    )
    assert r.status_code == 201
    device = r.json()
    device_id = device["id"]
    assert device["name"] == "Test Light"
    assert device["vendor"] == "Acme"
    assert device["room"] == "Lab"
    assert device["status"] == "active"

    try:
        # List - device appears
        r = _get("/api/devices", timeout=5)
        assert r.status_code == 200
        ids = [d["id"] for d in r.json()]
        assert device_id in ids

        # Read
        r = _get(f"/api/devices/{device_id}", timeout=5)
        assert r.status_code == 200
        assert r.json()["name"] == "Test Light"

        # Update
        r = _patch(f"/api/devices/{device_id}", json={"room": "Kitchen"}, timeout=5)
        assert r.status_code == 200
        assert r.json()["room"] == "Kitchen"
        assert r.json()["vendor"] == "Acme"  # unchanged field preserved

        # Delete
        r = _delete(f"/api/devices/{device_id}", timeout=5)
        assert r.status_code == 204
        device_id = None  # consumed

        # Gone
        r = _get(f"/api/devices/{device_id!r}", timeout=5)
        assert r.status_code in (404, 422)

    finally:
        if device_id:
            _delete(f"/api/devices/{device_id}", timeout=5)


def test_device_not_found():
    _skip_if_unreachable()
    r = _get("/api/devices/does-not-exist", timeout=5)
    assert r.status_code == 404


def test_device_merge():
    _skip_if_unreachable()

    # Manual device (no ha_device_id): should win name after merge
    r = _post(
        "/api/devices", json={"name": "Manual Light", "vendor": "Acme", "room": "Hall"}, timeout=5
    )
    assert r.status_code == 201
    manual_id = r.json()["id"]

    # Imported device (has ha_device_id set): should lose name, contribute ha fields
    r = _post(
        "/api/devices",
        json={
            "name": "HA Light",
            "firmware_version": "1.2.3",
        },
        timeout=5,
    )
    assert r.status_code == 201
    imported_id = r.json()["id"]
    # Set ha_device_id via the dedicated endpoint (DeviceCreate doesn't expose it)
    r = _patch(f"/api/devices/{imported_id}/ha-link", json={"ha_device_id": "abc123"}, timeout=5)
    assert r.status_code == 200

    # Add a property to the imported device
    r = _post(
        f"/api/devices/{imported_id}/properties",
        json={"type": "setup_pin", "value": "99999999"},
        timeout=5,
    )
    assert r.status_code == 201

    try:
        # POST merge: source=imported, target=manual
        r = _post(
            f"/devices/{imported_id}/merge",
            data={"target_id": manual_id},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
            timeout=5,
        )
        assert r.status_code == 303
        assert f"/devices/{manual_id}" in r.headers["location"]

        # Imported device is gone
        r = _get(f"/api/devices/{imported_id}", timeout=5)
        assert r.status_code == 404

        # Manual device survived with merged fields
        r = _get(f"/api/devices/{manual_id}", timeout=5)
        assert r.status_code == 200
        merged = r.json()
        assert merged["name"] == "Manual Light"  # manual name wins
        assert merged["vendor"] == "Acme"  # target field preserved
        assert merged["room"] == "Hall"  # target field preserved
        assert merged["ha_device_id"] == "abc123"  # source HA field filled in
        assert merged["firmware_version"] == "1.2.3"  # source field filled in

        # Property re-pointed to surviving device
        props = merged.get("properties", [])
        assert any(c["value"] == "99999999" for c in props)

    finally:
        _delete(f"/api/devices/{manual_id}", timeout=5)
        _delete(f"/api/devices/{imported_id}", timeout=5)


def test_warranty_and_dates():
    """A.5: warranty_until round-trip via web form; created_at and date fields render."""
    _skip_if_unreachable()

    # Create device without warranty date
    r = _post("/api/devices", json={"name": "Warranty Test Device"}, timeout=5)
    assert r.status_code == 201
    device_id = r.json()["id"]

    try:
        # created_at is non-null right away
        r = _get(f"/api/devices/{device_id}", timeout=5)
        assert r.status_code == 200
        assert r.json()["created_at"] is not None

        # Update warranty via the web form (form POST)
        r = httpx.post(
            f"{TARGET}/devices/{device_id}",
            data={
                "name": "Warranty Test Device",
                "status": "active",
                "warranty_until": "2027-06-01",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
            timeout=5,
        )
        assert r.status_code == 303

        # REST API reflects the new warranty date
        r = _get(f"/api/devices/{device_id}", timeout=5)
        assert r.status_code == 200
        assert r.json()["warranty_until"] == "2027-06-01"

        # Detail HTML renders "Added on" and "Warranty expires"
        r = _get(f"/devices/{device_id}", timeout=5)
        assert r.status_code == 200
        body = r.text
        assert "Added on" in body
        assert "Warranty expires" in body
        assert "2027-06-01" in body

        # Section order: Identity before Area
        assert body.index("Identity") < body.index("Area")

    finally:
        _delete(f"/api/devices/{device_id}", timeout=5)


def test_detail_section_order():
    """A.7: Onboarding H2 appears before Location H2 when QR payload is present."""
    _skip_if_unreachable()

    r = _post("/api/devices", json={"name": "Section Order Test"}, timeout=5)
    assert r.status_code == 201
    device_id = r.json()["id"]

    try:
        # Without QR: Identity still before Area
        r = _get(f"/devices/{device_id}", timeout=5)
        assert r.status_code == 200
        body = r.text
        assert body.index("Identity") < body.index("Area")

        # Add a QR payload property
        r = _post(
            f"/api/devices/{device_id}/properties",
            json={"type": "qr_payload", "value": "MT:Y.K90SO527JA0648G00"},
            timeout=5,
        )
        assert r.status_code == 201

        # With QR: Onboarding appears before Location
        r = _get(f"/devices/{device_id}", timeout=5)
        assert r.status_code == 200
        body = r.text
        assert "Onboarding" in body
        assert body.index("Onboarding") < body.index("Area")

    finally:
        _delete(f"/api/devices/{device_id}", timeout=5)


def test_scan_on_new_device():
    """POST /devices with qr_payload creates Device + qr_payload Credential atomically."""
    _skip_if_unreachable()

    # CHIP SDK canonical test vector (same as test_matter_payload.py)
    payload = "MT:-24J0AFN00KA0648G00"

    r = _post(
        "/devices",
        data={"name": "A6 scan-on-new test", "qr_payload": payload},
        follow_redirects=True,
        timeout=10,
    )
    assert r.status_code == 200, f"Expected redirect to detail, got {r.status_code}"

    device_id = str(r.url).rstrip("/").split("/")[-1]
    try:
        # qr_payload credential was created
        dev = _get(f"/api/devices/{device_id}", timeout=5).json()
        props = dev.get("properties", [])
        qr_creds = [c for c in props if c["type"] == "qr_payload"]
        assert len(qr_creds) == 1
        assert qr_creds[0]["value"] == payload

        # vendor_id / product_id populated from QR
        assert dev["vendor_id"] == 65521  # 0xFFF1
        assert dev["product_id"] == 32769  # 0x8001

        # detail page renders the Onboarding card
        html = _get(f"/devices/{device_id}", timeout=5).text
        assert "Onboarding" in html
    finally:
        _delete(f"/api/devices/{device_id}", timeout=5)


def test_comms_bluetooth_icon_not_shown_for_ble_commissioning_qr():
    """A.1 regression: BLE commissioning capability in QR payload must NOT light the
    bluetooth icon in the device list.  Only network_type membership drives
    transport icons.  This test uses a QR payload with BLE discovery bit set
    but leaves network_type empty, and asserts no bluetooth icon in the row.
    """
    _skip_if_unreachable()

    # MT:-24J0AFN00KA0648G00 is the CHIP SDK canonical test vector;
    # it has discovery_capabilities=0x04 (BLE) so would have triggered the
    # old bug.
    payload = "MT:-24J0AFN00KA0648G00"
    name = "A1-ble-commissioning-icon-test"

    r = _post("/api/devices", json={"name": name}, timeout=5)
    assert r.status_code == 201
    device_id = r.json()["id"]

    try:
        # Add QR payload property (BLE discovery bit set)
        r = _post(
            f"/api/devices/{device_id}/properties",
            json={"type": "qr_payload", "value": payload},
            timeout=5,
        )
        assert r.status_code == 201

        # network_type must be empty ([] default)
        dev = _get(f"/api/devices/{device_id}", timeout=5).json()
        assert dev["network_type"] == [], "network_type should be empty by default"

        # Load device list filtered to this device; check the row HTML
        html = _get(f"/devices?q={name}", timeout=5).text
        # The qr-code icon (onboarding) SHOULD appear
        assert "icon-qr-code" in html, "Expected qr-code icon for device with QR payload"
        # The bluetooth icon must NOT appear (network_type is empty)
        assert "BLE commissioning available" not in html, (
            "bluetooth icon must not be lit by QR commissioning capability"
        )

    finally:
        _delete(f"/api/devices/{device_id}", timeout=5)


def test_sync_all_btn_present_on_device_list():
    """A.8: /devices page renders the #sync-all-btn icon button in the header."""
    _skip_if_unreachable()

    r = _get("/devices", timeout=5)
    assert r.status_code == 200
    assert 'id="sync-all-btn"' in r.text, "#sync-all-btn missing from device list page"
    assert "icon-sync-circle" in r.text, "sync-circle icon not rendered inside sync-all-btn"


def test_onboarding_tile_empty_state():
    """A.10: device detail always renders #onboarding; empty state shows 'Scan / add code'."""
    _skip_if_unreachable()

    r = _post("/api/devices", json={"name": "A.10 Test Device"}, timeout=5)
    assert r.status_code == 201
    device_id = r.json()["id"]

    try:
        page = _get(f"/devices/{device_id}", timeout=5)
        assert page.status_code == 200
        assert 'id="onboarding"' in page.text, (
            "#onboarding tile missing for device with no credentials"
        )
        assert "Scan" in page.text and "add code" in page.text, (
            "empty-state CTA not rendered in onboarding tile"
        )
    finally:
        _delete(f"/api/devices/{device_id}", timeout=5)
