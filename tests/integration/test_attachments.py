"""
Attachment integration tests: upload / read / delete / cascade.

Requires TEST_TARGET env var.
Skips cleanly when the target is unreachable.
"""

import os

import httpx
import pytest

TARGET = os.environ.get("TEST_TARGET")

# Minimal valid 1×1 PNG (67 bytes)
_TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
    b"\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00"
    b"\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18"
    b"\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)
# Minimal valid PDF header
_TINY_PDF = b"%PDF-1.4\n1 0 obj\n<< >>\nendobj\n%%EOF\n"


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


def _create_device(name: str) -> str:
    r = _post("/api/devices", json={"name": name}, timeout=10)
    assert r.status_code == 201
    return r.json()["id"]


def test_upload_and_list():
    _skip_if_unreachable()
    device_id = _create_device("Attachment Upload Test")
    try:
        r = _post(
            f"/api/devices/{device_id}/attachments",
            files={"file": ("test.png", _TINY_PNG, "image/png")},
            timeout=10,
        )
        assert r.status_code == 201, r.text
        meta = r.json()
        assert meta["filename"] == "test.png"
        assert meta["mime_type"] == "image/png"
        assert meta["kind"] == "image"
        assert meta["size_bytes"] == len(_TINY_PNG)
        assert len(meta["sha256"]) == 64

        # List
        r = _get(f"/api/devices/{device_id}/attachments", timeout=10)
        assert r.status_code == 200
        ids = [a["id"] for a in r.json()]
        assert meta["id"] in ids

    finally:
        _delete(f"/api/devices/{device_id}", timeout=5)


def test_download_attachment():
    _skip_if_unreachable()
    device_id = _create_device("Attachment Download Test")
    try:
        r = _post(
            f"/api/devices/{device_id}/attachments",
            files={"file": ("photo.png", _TINY_PNG, "image/png")},
            timeout=10,
        )
        assert r.status_code == 201
        att_id = r.json()["id"]

        r = _get(f"/api/devices/{device_id}/attachments/{att_id}", timeout=10)
        assert r.status_code == 200
        assert r.content == _TINY_PNG
        assert r.headers["content-type"].startswith("image/png")

    finally:
        _delete(f"/api/devices/{device_id}", timeout=5)


def test_upload_pdf():
    _skip_if_unreachable()
    device_id = _create_device("PDF Attachment Test")
    try:
        r = _post(
            f"/api/devices/{device_id}/attachments",
            files={"file": ("manual.pdf", _TINY_PDF, "application/pdf")},
            timeout=10,
        )
        assert r.status_code == 201, r.text
        assert r.json()["kind"] == "pdf"

    finally:
        _delete(f"/api/devices/{device_id}", timeout=5)


def test_upload_too_large_returns_413():
    _skip_if_unreachable()
    device_id = _create_device("Oversized Attachment Test")
    try:
        big = b"A" * (10 * 1024 * 1024 + 1)  # 10 MB + 1 byte
        r = _post(
            f"/api/devices/{device_id}/attachments",
            files={"file": ("big.png", big, "image/png")},
            timeout=30,
        )
        assert r.status_code == 413, r.status_code

    finally:
        _delete(f"/api/devices/{device_id}", timeout=5)


def test_unsupported_type_returns_415():
    _skip_if_unreachable()
    device_id = _create_device("Bad MIME Test")
    try:
        r = _post(
            f"/api/devices/{device_id}/attachments",
            files={"file": ("script.js", b"alert(1)", "application/javascript")},
            timeout=10,
        )
        assert r.status_code == 415

    finally:
        _delete(f"/api/devices/{device_id}", timeout=5)


def test_delete_attachment():
    _skip_if_unreachable()
    device_id = _create_device("Delete Attachment Test")
    try:
        r = _post(
            f"/api/devices/{device_id}/attachments",
            files={"file": ("del.png", _TINY_PNG, "image/png")},
            timeout=10,
        )
        assert r.status_code == 201
        att_id = r.json()["id"]

        r = _delete(f"/api/devices/{device_id}/attachments/{att_id}", timeout=10)
        assert r.status_code == 204

        r = _get(f"/api/devices/{device_id}/attachments/{att_id}", timeout=10)
        assert r.status_code == 404

    finally:
        _delete(f"/api/devices/{device_id}", timeout=5)


def test_cascade_delete_with_device():
    _skip_if_unreachable()
    device_id = _create_device("Cascade Delete Test")
    att_id = None
    try:
        r = _post(
            f"/api/devices/{device_id}/attachments",
            files={"file": ("cascade.png", _TINY_PNG, "image/png")},
            timeout=10,
        )
        assert r.status_code == 201
        att_id = r.json()["id"]

        # Delete the device
        r = _delete(f"/api/devices/{device_id}", timeout=10)
        assert r.status_code == 204
        device_id = None  # consumed

        # Attachment must be gone
        r = _get(f"/api/devices/{device_id}/attachments/{att_id}", timeout=10)
        assert r.status_code == 404

    finally:
        if device_id:
            _delete(f"/api/devices/{device_id}", timeout=5)


def test_attachment_wrong_device_returns_404():
    """A.6: accessing an attachment via a different device's URL must return 404."""
    _skip_if_unreachable()

    device_a = _create_device("Attachment Parent A")
    device_b = _create_device("Attachment Parent B")
    try:
        r = _post(
            f"/api/devices/{device_a}/attachments",
            files={"file": ("test.png", _TINY_PNG, "image/png")},
            timeout=10,
        )
        assert r.status_code == 201
        att_id = r.json()["id"]

        # GET via device B → 404
        r = _get(f"/api/devices/{device_b}/attachments/{att_id}", timeout=10)
        assert r.status_code == 404, f"Expected 404, got {r.status_code}"

        # GET /meta via device B → 404
        r = _get(f"/api/devices/{device_b}/attachments/{att_id}/meta", timeout=10)
        assert r.status_code == 404, f"Expected 404, got {r.status_code}"

        # DELETE via device B → 404
        r = _delete(f"/api/devices/{device_b}/attachments/{att_id}", timeout=10)
        assert r.status_code == 404, f"Expected 404, got {r.status_code}"

        # Attachment still accessible via correct device
        r = _get(f"/api/devices/{device_a}/attachments/{att_id}", timeout=10)
        assert r.status_code == 200
    finally:
        _delete(f"/api/devices/{device_a}", timeout=5)
        _delete(f"/api/devices/{device_b}", timeout=5)
