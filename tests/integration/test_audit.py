"""Audit log unit tests - verify the logger-based audit helper.

These tests run entirely in-process; no running container required.
"""

import logging

import pytest

from app.audit import log as audit_log


def test_audit_emits_info_log(caplog):
    """audit.log() emits an INFO record on matterregistry.audit."""
    with caplog.at_level(logging.INFO, logger="matterregistry.audit"):
        audit_log(None, action="device.create", entity="device:abc123", reason="test")

    assert len(caplog.records) == 1
    rec = caplog.records[0]
    assert rec.levelno == logging.INFO
    assert rec.name == "matterregistry.audit"
    assert "device.create" in rec.message
    assert "device:abc123" in rec.message
    assert "test" in rec.message


def test_audit_action_entity_reason_all_present(caplog):
    with caplog.at_level(logging.INFO, logger="matterregistry.audit"):
        audit_log(
            None,
            action="property.create",
            entity="property:xyz",
            reason="api.devices.add_property",
        )

    msg = caplog.records[0].message
    assert "property.create" in msg
    assert "property:xyz" in msg
    assert "api.devices.add_property" in msg


def test_audit_none_reason_does_not_crash(caplog):
    with caplog.at_level(logging.INFO, logger="matterregistry.audit"):
        audit_log(None, action="device.delete", entity="device:001", reason=None)

    assert len(caplog.records) == 1


def test_audit_session_arg_ignored(caplog):
    """Session argument is accepted but ignored (backwards compatibility)."""
    sentinel = object()
    with caplog.at_level(logging.INFO, logger="matterregistry.audit"):
        audit_log(sentinel, action="device.update", entity="device:002", reason="web.device_update")

    assert len(caplog.records) == 1
    assert "device.update" in caplog.records[0].message


def test_audit_logger_level_is_info():
    """The audit logger must be pinned at INFO regardless of root level."""
    import logging

    from app.audit import _audit_logger

    assert _audit_logger.level == logging.INFO


@pytest.mark.parametrize(
    "action,entity,reason",
    [
        ("device.create", "device:a1", "web.device_create"),
        ("device.update", "device:a2", "api.devices.update"),
        ("device.delete", "device:a3", "api.devices.delete"),
        ("device.scan", "device:a4", "web.device_scan"),
        ("property.create", "property:b1", "api.devices.add_property"),
        ("property.update", "property:b2", "web.property_update"),
        ("property.delete", "property:b3", "web.property_delete"),
        ("attachment.delete", "attachment:c1", "web.attachment_delete"),
        ("device.ha_link", "device:d1", "ui.manual_link"),
        ("device.ha_unlink", "device:d1", "ui.manual_link"),
        ("import.full", "import:5", "ui.replace"),
        ("import.skip", "import:0", "ui.skip"),
        ("export.download", "export", "api.export"),
    ],
)
def test_audit_vocabulary(caplog, action, entity, reason):
    """All action/reason pairs from the vocabulary round-trip through the logger."""
    with caplog.at_level(logging.INFO, logger="matterregistry.audit"):
        audit_log(None, action=action, entity=entity, reason=reason)
    msg = caplog.records[0].message
    assert action in msg
    assert entity in msg
