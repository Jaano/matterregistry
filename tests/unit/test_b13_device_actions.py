"""
Unit tests for B.13 - integration device-actions framework.

Covers:
  * DeviceAction / ActionResult dataclasses behave correctly.
  * Base Integration.device_actions() returns [].
  * MatterServerClient and OTBRClient declare the expected actions.
  * applicable_fn logic is correct per client.
  * write-kind actions are only declared by integrations with can_act_externally.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from app.integrations.base import ActionResult, DeviceAction, Integration, SyncResult

# ── stub integration ──────────────────────────────────────────────────────────


class _Stub(Integration):
    slug = "stub"
    short_name = "stub"
    long_name = "Stub"
    can_create_devices = False
    can_update_devices = False

    @property
    def enabled(self) -> bool:
        return True

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def ingest(self) -> None:
        pass

    def project(self, session: object) -> SyncResult:
        return SyncResult()


# ── ActionResult ──────────────────────────────────────────────────────────────


def test_action_result_defaults():
    r = ActionResult()
    assert r.message == ""
    assert r.data == {}


def test_action_result_values():
    r = ActionResult(message="ok", data={"k": 1})
    assert r.message == "ok"
    assert r.data == {"k": 1}


# ── DeviceAction ──────────────────────────────────────────────────────────────


def test_device_action_applicable_delegates():
    seen = []

    def fn(device, session):
        seen.append((device, session))
        return True

    async def run_fn(device, session):
        return ActionResult()

    action = DeviceAction(key="x", label="X", kind="retrieve", applicable_fn=fn, run_fn=run_fn)
    assert action.applicable("d", "s") is True
    assert seen == [("d", "s")]


async def test_device_action_run_delegates():
    async def run_fn(device, session):
        return ActionResult(message=f"{device}-{session}")

    action = DeviceAction(
        key="x",
        label="X",
        kind="retrieve",
        applicable_fn=lambda d, s: True,
        run_fn=run_fn,
    )
    result = await action.run("dev", "ses")
    assert isinstance(result, ActionResult)
    assert result.message == "dev-ses"


# ── Base Integration ──────────────────────────────────────────────────────────


def test_base_integration_returns_no_actions():
    assert _Stub().device_actions() == []


# ── MatterServerClient ────────────────────────────────────────────────────────


def test_matter_client_declares_refresh_network():
    from app.integrations.matter_server.server_client import MatterServerClient

    client = MatterServerClient("ws://localhost:5580")
    actions = client.device_actions()
    keys = [a.key for a in actions]
    assert "refresh_network" in keys
    assert "refresh_fabrics" in keys
    action = next(a for a in actions if a.key == "refresh_network")
    assert action.kind == "retrieve"
    assert action.label  # non-empty


def test_matter_refresh_network_applicable_with_membership():
    from app.integrations.matter_server.server_client import MatterServerClient

    client = MatterServerClient("ws://localhost:5580")
    action = next(a for a in client.device_actions() if a.key == "refresh_network")

    device = MagicMock()
    device.id = "dev-1"

    session = MagicMock()
    session.exec.return_value.first.return_value = MagicMock()
    assert action.applicable(device, session) is True


def test_matter_refresh_network_not_applicable_without_membership():
    from app.integrations.matter_server.server_client import MatterServerClient

    client = MatterServerClient("ws://localhost:5580")
    action = next(a for a in client.device_actions() if a.key == "refresh_network")

    device = MagicMock()
    device.id = "dev-1"

    session = MagicMock()
    session.exec.return_value.first.return_value = None
    assert action.applicable(device, session) is False


# ── OTBRClient ────────────────────────────────────────────────────────────────


def test_otbr_client_declares_scan_diagnostics():
    from app.integrations.otbr.client import OTBRClient

    client = OTBRClient("http://otbr.local")
    actions = client.device_actions()
    assert len(actions) == 1
    action = actions[0]
    assert action.key == "scan_diagnostics"
    assert action.kind == "retrieve"
    assert action.label


def test_otbr_scan_applicable_when_thread_in_network_type():
    from app.integrations.otbr.client import OTBRClient

    client = OTBRClient("http://otbr.local")
    action = client.device_actions()[0]
    session = MagicMock()

    d_thread = MagicMock()
    d_thread.network_type = ["thread"]
    assert action.applicable(d_thread, session) is True

    d_wifi = MagicMock()
    d_wifi.network_type = ["wifi"]
    assert action.applicable(d_wifi, session) is False

    d_empty = MagicMock()
    d_empty.network_type = []
    assert action.applicable(d_empty, session) is False

    d_none = MagicMock()
    d_none.network_type = None
    assert action.applicable(d_none, session) is False


# ── write-kind guard ──────────────────────────────────────────────────────────


def test_write_actions_require_can_act_externally():
    """Any integration that declares write actions must have can_act_externally=True."""
    from app.integrations.matter_server.server_client import MatterServerClient
    from app.integrations.otbr.client import OTBRClient

    for cls, url in [(MatterServerClient, "ws://localhost:5580"), (OTBRClient, "http://x")]:
        client = cls(url)
        write_actions = [a for a in client.device_actions() if a.kind == "write"]
        if write_actions:
            assert client.can_act_externally, (
                f"{cls.__name__} declares write actions but can_act_externally=False"
            )
