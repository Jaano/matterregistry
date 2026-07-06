"""Audit log helper - writes to the ``matterregistry.audit`` Python logger.

Each call emits one INFO line:
    AUDIT action=<action> entity=<entity> reason=<reason>

The logger is fixed at INFO and propagates to the root handler (stdout /
uvicorn) so it is always visible regardless of MR_LOG_LEVEL.  Sensitive
values (passcodes, keys, tokens) must never appear in *action*, *entity*,
or *reason* - callers must use opaque IDs only.
"""

import logging

_audit_logger = logging.getLogger("matterregistry.audit")
_audit_logger.setLevel(logging.INFO)


def log(
    _session_or_none,
    action: str,
    entity: str,
    reason: str | None = None,
) -> None:
    """Emit an audit record.

    The *_session_or_none* parameter is accepted (and ignored) so that all
    existing call sites continue to compile unchanged during the migration.
    """
    _audit_logger.info("action=%s entity=%s reason=%s", action, entity, reason or "")
