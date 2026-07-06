"""Unit tests for the integration_sync_interval validator in Settings."""

import os

import pytest


def _make_settings(**env_overrides):
    """Instantiate Settings with the given env vars, restoring originals after."""
    from importlib import reload

    import app.settings as settings_module

    old = {k: os.environ.get(k) for k in env_overrides}
    for k, v in env_overrides.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    try:
        reload(settings_module)
        return settings_module.Settings()
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        reload(settings_module)


def test_default_interval_is_600():
    s = _make_settings(MR_INTEGRATION_SYNC_INTERVAL=None)
    assert s.integration_sync_interval == 600


def test_positive_interval_accepted():
    s = _make_settings(MR_INTEGRATION_SYNC_INTERVAL="300")
    assert s.integration_sync_interval == 300


def test_zero_interval_accepted():
    s = _make_settings(MR_INTEGRATION_SYNC_INTERVAL="0")
    assert s.integration_sync_interval == 0


def test_minus_one_accepted():
    s = _make_settings(MR_INTEGRATION_SYNC_INTERVAL="-1")
    assert s.integration_sync_interval == -1


def test_minus_two_rejected():
    with pytest.raises(ValueError, match="MR_INTEGRATION_SYNC_INTERVAL"):
        _make_settings(MR_INTEGRATION_SYNC_INTERVAL="-2")


def test_non_integer_rejected():
    with pytest.raises(ValueError, match="MR_INTEGRATION_SYNC_INTERVAL"):
        _make_settings(MR_INTEGRATION_SYNC_INTERVAL="ten")
