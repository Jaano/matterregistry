"""
Property CRUD integration tests.
Requires TEST_TARGET env var.
"""

import os

import httpx
import pytest

TARGET = os.environ.get("TEST_TARGET")


def _skip_if_unreachable():
    if not TARGET:
        pytest.skip("TEST_TARGET not set")
    try:
        httpx.get(f"{TARGET}/healthz", timeout=5)
    except httpx.ConnectError:
        pytest.skip(f"target unreachable: {TARGET}")


def test_property_crud():
    _skip_if_unreachable()

    # Create a device to attach properties to
    r = httpx.post(f"{TARGET}/api/devices", json={"name": "Cred Test Device"}, timeout=5)
    assert r.status_code == 201
    device_id = r.json()["id"]

    try:
        # Add a property
        r = httpx.post(
            f"{TARGET}/api/devices/{device_id}/properties",
            json={"type": "setup_pin", "value": "12345678", "label": "factory PIN"},
            timeout=5,
        )
        assert r.status_code == 201
        cred = r.json()
        cred_id = cred["id"]
        assert cred["type"] == "setup_pin"
        assert cred["value"] == "12345678"
        assert cred["label"] == "factory PIN"
        assert cred["device_id"] == device_id

        # Property appears on device detail
        r = httpx.get(f"{TARGET}/api/devices/{device_id}", timeout=5)
        assert r.status_code == 200
        cred_ids = [c["id"] for c in r.json()["properties"]]
        assert cred_id in cred_ids

        # Update property
        r = httpx.patch(
            f"{TARGET}/api/devices/{device_id}/properties/{cred_id}",
            json={"label": "updated label"},
            timeout=5,
        )
        assert r.status_code == 200
        assert r.json()["label"] == "updated label"
        assert r.json()["value"] == "12345678"  # unchanged

        # Delete property
        r = httpx.delete(f"{TARGET}/api/devices/{device_id}/properties/{cred_id}", timeout=5)
        assert r.status_code == 204

        # Gone from device
        r = httpx.get(f"{TARGET}/api/devices/{device_id}", timeout=5)
        cred_ids = [c["id"] for c in r.json()["properties"]]
        assert cred_id not in cred_ids

    finally:
        httpx.delete(f"{TARGET}/api/devices/{device_id}", timeout=5)


def test_property_cascade_delete():
    """Deleting a device deletes its properties."""
    _skip_if_unreachable()

    r = httpx.post(f"{TARGET}/api/devices", json={"name": "Cascade Test"}, timeout=5)
    device_id = r.json()["id"]
    httpx.post(
        f"{TARGET}/api/devices/{device_id}/properties",
        json={"type": "setup_pin", "value": "99999999"},
        timeout=5,
    )

    r = httpx.get(f"{TARGET}/api/devices/{device_id}", timeout=5)
    cred_id = r.json()["properties"][0]["id"]

    httpx.delete(f"{TARGET}/api/devices/{device_id}", timeout=5)

    # Device is gone
    r = httpx.get(f"{TARGET}/api/devices/{device_id}", timeout=5)
    assert r.status_code == 404

    # Property is also gone
    r = httpx.delete(f"{TARGET}/api/devices/{device_id}/properties/{cred_id}", timeout=5)
    assert r.status_code == 404


def test_property_wrong_device_returns_404():
    """A.6: accessing a property via a different device's URL must return 404."""
    _skip_if_unreachable()

    r = httpx.post(f"{TARGET}/api/devices", json={"name": "Cred Parent A"}, timeout=5)
    assert r.status_code == 201
    device_a = r.json()["id"]

    r = httpx.post(f"{TARGET}/api/devices", json={"name": "Cred Parent B"}, timeout=5)
    assert r.status_code == 201
    device_b = r.json()["id"]

    try:
        r = httpx.post(
            f"{TARGET}/api/devices/{device_a}/properties",
            json={"type": "setup_pin", "value": "11111111"},
            timeout=5,
        )
        assert r.status_code == 201
        cred_id = r.json()["id"]

        # PATCH via device B → 404
        r = httpx.patch(
            f"{TARGET}/api/devices/{device_b}/properties/{cred_id}",
            json={"label": "hijack"},
            timeout=5,
        )
        assert r.status_code == 404, f"Expected 404, got {r.status_code}"

        # DELETE via device B → 404
        r = httpx.delete(f"{TARGET}/api/devices/{device_b}/properties/{cred_id}", timeout=5)
        assert r.status_code == 404, f"Expected 404, got {r.status_code}"

        # Property still belongs to device A
        r = httpx.patch(
            f"{TARGET}/api/devices/{device_a}/properties/{cred_id}",
            json={"label": "legit"},
            timeout=5,
        )
        assert r.status_code == 200
    finally:
        httpx.delete(f"{TARGET}/api/devices/{device_a}", timeout=5)
        httpx.delete(f"{TARGET}/api/devices/{device_b}", timeout=5)
