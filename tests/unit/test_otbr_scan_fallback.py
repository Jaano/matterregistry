"""Unit tests for the OTBR diagnostic scan failure handling.

One router that does not answer fails the whole broadcast task and leaves no
partial results. The scan must then query the routers one at a time, merge the
replies into the cache, and raise (never report success) when nothing answered.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.integrations.otbr import client as otbr_client
from app.integrations.otbr.client import OTBRClient

BASE = "http://otbr.test"


class _Resp:
    def __init__(self, payload: object, status: int = 200) -> None:
        self._payload = payload
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("err", request=None, response=None)  # type: ignore[arg-type]

    def json(self) -> object:
        return self._payload


class _Mesh:
    """Fake OTBR. ``mesh`` maps RLOC16 to a reply (None = that router does not answer).

    ``broadcast`` is how the 0xfffe task ends: "completed" (replies for every
    reachable router), "failed", or "error" (the POST itself is rejected).
    """

    def __init__(self, mesh: dict[int, dict | None], broadcast: str) -> None:
        self.mesh = mesh
        self.broadcast = broadcast
        self.destinations: list[int] = []
        self._last: int | None = None

    def client(self, *args: object, **kwargs: object) -> _Mesh:
        return self

    async def __aenter__(self) -> _Mesh:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def get(self, url: str, headers: dict | None = None) -> _Resp:
        if url.endswith("/node"):
            return _Resp({"state": "router", "extAddress": "cc", "rloc16": "0x1800"})
        if "/api/actions/" in url:
            if self._last == 0xFFFE:
                return _Resp({"status": self.broadcast})
            ok = self.mesh.get(self._last) is not None
            return _Resp({"status": "completed" if ok else "failed"})
        if url.endswith("/api/diagnostics"):
            if self._last == 0xFFFE:
                return _Resp([r for r in self.mesh.values() if r])
            reply = self.mesh.get(self._last)
            return _Resp([reply] if reply else [])
        return _Resp(None, 404)

    async def post(
        self, url: str, content: bytes | None = None, headers: dict | None = None
    ) -> _Resp:
        attrs = json.loads(content or b"{}")["data"][0]["attributes"]
        self._last = int(attrs["destination"], 16)
        self.destinations.append(self._last)
        if self.broadcast == "error" and self._last == 0xFFFE:
            return _Resp(None, 500)
        return _Resp({"data": [{"id": "task"}]})


def _reply(rloc: int, ext: str) -> dict:
    return {"extAddress": ext, "rloc16": f"0x{rloc:04x}", "routerId": rloc >> 10}


def _self(*router_ids: int) -> dict:
    return {
        "extAddress": "cc",
        "rloc16": "0x1800",
        "routerId": 6,
        "route": {"routeData": [{"routeId": r} for r in router_ids]},
    }


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(otbr_client, "_POLL_INTERVAL", 0)
    monkeypatch.setattr(otbr_client, "_UNICAST_GAP", 0)


def _use(monkeypatch, fake: _Mesh) -> None:
    monkeypatch.setattr(otbr_client.httpx, "AsyncClient", fake.client)


async def test_broadcast_success_does_not_unicast(monkeypatch):
    fake = _Mesh({0x0000: _reply(0, "aa"), 0x9000: _reply(0x9000, "bb")}, "completed")
    _use(monkeypatch, fake)
    client = OTBRClient(BASE)

    outcome = await client.scan_diagnostics()

    assert fake.destinations == [0xFFFE]
    assert outcome.method == "broadcast"
    assert outcome.answered == 2
    assert outcome.message == "Thread diagnostics scan complete (2 routers answered)"
    assert {d.ext_address for d in client.get_diagnostics()} == {"aa", "bb"}


async def test_failed_broadcast_falls_back_to_per_router_queries(monkeypatch):
    fake = _Mesh(
        {
            0x1800: _self(0, 36, 40),
            0x0000: _reply(0, "aa"),
            0x9000: _reply(0x9000, "bb"),
            0xA000: None,
        },
        "failed",
    )
    _use(monkeypatch, fake)
    client = OTBRClient(BASE)

    outcome = await client.scan_diagnostics()

    # Broadcast first, then the border router's own route table, then each router.
    assert fake.destinations == [0xFFFE, 0x1800, 0x0000, 0x9000, 0xA000]
    assert outcome.method == "per-router"
    assert (outcome.answered, outcome.unreachable) == (3, 1)
    assert "via per-router queries (3 answered, 1 unreachable)" in outcome.message
    assert "the broadcast scan failed" in outcome.message
    assert {d.ext_address for d in client.get_diagnostics()} == {"cc", "aa", "bb"}


async def test_rejected_broadcast_also_falls_back(monkeypatch):
    fake = _Mesh({0x1800: _self(0), 0x0000: _reply(0, "aa")}, "error")
    _use(monkeypatch, fake)

    outcome = await OTBRClient(BASE).scan_diagnostics()

    assert outcome.method == "per-router"
    assert outcome.answered == 2


async def test_empty_broadcast_result_is_not_reported_as_success(monkeypatch):
    fake = _Mesh({0x1800: _self(0), 0x0000: _reply(0, "aa")}, "completed")
    original = fake.get

    async def get(url: str, headers: dict | None = None) -> _Resp:
        if url.endswith("/api/diagnostics") and fake._last == 0xFFFE:
            return _Resp([])
        return await original(url, headers)

    fake.get = get  # type: ignore[method-assign]
    _use(monkeypatch, fake)

    outcome = await OTBRClient(BASE).scan_diagnostics()

    assert outcome.method == "per-router"


async def test_scan_raises_and_keeps_old_cache_when_nothing_answers(monkeypatch):
    fake = _Mesh({0x1800: None, 0x0000: None}, "failed")
    _use(monkeypatch, fake)
    client = OTBRClient(BASE)
    sentinel = [otbr_client.RouterDiag(ext_address="old", rloc16="0x0400", router_id=1)]
    client._diagnostics = sentinel

    with pytest.raises(RuntimeError, match="no router answered the per-router fallback"):
        await client.scan_diagnostics()

    assert client.get_diagnostics() == sentinel


async def test_scan_raises_when_border_router_is_unreachable(monkeypatch):
    class _Down(_Mesh):
        async def get(self, url: str, headers: dict | None = None) -> _Resp:
            raise httpx.ConnectError("down")

    fake = _Down({}, "failed")
    _use(monkeypatch, fake)

    with pytest.raises(RuntimeError, match="Thread diagnostic scan failed"):
        await OTBRClient(BASE).scan_diagnostics()


async def test_action_surfaces_the_outcome_message(monkeypatch):
    fake = _Mesh({0x1800: _self(0), 0x0000: _reply(0, "aa")}, "failed")
    _use(monkeypatch, fake)
    action = next(a for a in OTBRClient(BASE).device_actions() if a.key == "scan_diagnostics")

    result = await action.run(object(), object())

    assert "via per-router queries" in result.message
