"""
Unit tests for the OTBR sync/scan split (ISSUES.md I.18 / B.23).

The locked rule (TECHNICAL_DESIGN.md §3a) forbids the sync/poll path from
touching the mesh. These tests pin the boundary purely in-process by faking
``httpx.AsyncClient`` and asserting which endpoints each path hits:

  * ``poll_once`` (sync) must read only the border router - never POST a scan.
  * ``scan_diagnostics`` (device action) must POST the broadcast scan.
"""

from __future__ import annotations

import httpx

from app.integrations.otbr import client as otbr_client
from app.integrations.otbr.client import OTBRClient

BASE = "http://otbr.test"


class _FakeResp:
    def __init__(self, payload: object, status: int = 200) -> None:
        self._payload = payload
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("err", request=None, response=None)  # type: ignore[arg-type]

    def json(self) -> object:
        return self._payload


class _RecordingClient:
    """Fake httpx.AsyncClient that records (method, url) and returns canned data."""

    def __init__(self, calls: list[tuple[str, str]], *args: object, **kwargs: object) -> None:
        self._calls = calls

    async def __aenter__(self) -> _RecordingClient:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def get(self, url: str, headers: dict | None = None) -> _FakeResp:
        self._calls.append(("GET", url))
        if url.endswith("/node"):
            return _FakeResp(
                {"state": "leader", "extAddress": "aabb", "rloc16": "0x7000", "networkName": "n"}
            )
        if url.endswith("/node/dataset/active"):
            return _FakeResp(
                {
                    "extPanId": "1111111111111111",
                    "panId": 0x1234,
                    "channel": 15,
                    "networkName": "n",
                    "meshLocalPrefix": "fd00::/64",
                    "networkKey": "k",
                }
            )
        if url.endswith("/node/coprocessor-version"):
            return _FakeResp("ncp-1.0")
        if url.endswith("/api/diagnostics"):
            return _FakeResp([])
        if "/api/actions/" in url:
            return _FakeResp({"status": "completed"})
        return _FakeResp(None, 404)

    async def post(
        self, url: str, content: object = None, headers: dict | None = None
    ) -> _FakeResp:
        self._calls.append(("POST", url))
        return _FakeResp({"data": [{"id": "task1"}]})


def _patch(monkeypatch) -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(otbr_client.httpx, "AsyncClient", lambda *a, **k: _RecordingClient(calls))
    return calls


async def test_poll_once_is_server_only_no_scan(monkeypatch):
    """Sync poll reads the border router + cached diagnostics, never POSTs a scan."""
    calls = _patch(monkeypatch)
    client = OTBRClient(BASE)

    await client.poll_once(dry_run=True)

    methods = {m for m, _ in calls}
    assert "POST" not in methods, f"sync path issued a scan POST: {calls}"
    assert ("GET", f"{BASE}/api/diagnostics") in calls
    assert ("POST", f"{BASE}/api/actions") not in calls


async def test_scan_diagnostics_triggers_broadcast_scan(monkeypatch):
    """The device-action path actively POSTs the broadcast diagnostic scan."""
    calls = _patch(monkeypatch)
    client = OTBRClient(BASE)

    await client.scan_diagnostics()

    assert ("POST", f"{BASE}/api/actions") in calls
    assert ("GET", f"{BASE}/api/diagnostics") in calls
