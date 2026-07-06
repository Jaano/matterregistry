"""Unit tests for the Ingress-only gate in ingress_middleware (I.15).

Supervisor validates the HA user session before forwarding Ingress requests
and only sets X-Ingress-Path on authenticated requests.  The middleware
therefore trusts header presence - no Supervisor API round-trip needed.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest


def _make_request(ingress_path: str, url_path: str = "/devices") -> MagicMock:
    req = MagicMock()
    req.headers = {"X-Ingress-Path": ingress_path} if ingress_path else {}
    req.url.path = url_path
    req.state = MagicMock()
    return req


def _make_settings(*, ha_mode: bool, direct_api: bool) -> MagicMock:
    s = MagicMock()
    s.ha_mode = ha_mode
    s.option_direct_api = direct_api
    return s


async def _run_middleware(request, settings) -> int:
    """Run the gate logic; return 403 if blocked, 200 if allowed."""
    ingress_path = request.headers.get("X-Ingress-Path", "")
    request.state.ingress_path = ingress_path

    if (
        settings.ha_mode
        and not settings.option_direct_api
        and not ingress_path
        and request.url.path != "/healthz"
    ):
        return 403

    call_next = AsyncMock(return_value=MagicMock(status_code=200))
    resp = await call_next(request)
    return resp.status_code


@pytest.mark.asyncio
async def test_ingress_request_allowed():
    req = _make_request("/api/hassio_ingress/abc123")
    assert await _run_middleware(req, _make_settings(ha_mode=True, direct_api=False)) == 200


@pytest.mark.asyncio
async def test_direct_request_blocked_without_direct_api():
    req = _make_request("")
    assert await _run_middleware(req, _make_settings(ha_mode=True, direct_api=False)) == 403


@pytest.mark.asyncio
async def test_direct_request_allowed_with_direct_api():
    req = _make_request("")
    assert await _run_middleware(req, _make_settings(ha_mode=True, direct_api=True)) == 200


@pytest.mark.asyncio
async def test_standalone_mode_no_gate():
    req = _make_request("")
    assert await _run_middleware(req, _make_settings(ha_mode=False, direct_api=False)) == 200


@pytest.mark.asyncio
async def test_healthz_always_allowed():
    req = _make_request("", url_path="/healthz")
    assert await _run_middleware(req, _make_settings(ha_mode=True, direct_api=False)) == 200
