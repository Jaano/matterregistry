"""User-chosen behavior switches of integrations (``IntegrationSetting`` rows).

Connection config (URLs, tokens) is environment-only and never stored here.
A missing row means the caller's default. Functions take an open Session; the
caller controls the transaction.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlmodel import Session

from ..models import IntegrationSetting

OTBR = "otbr"
OTBR_AUTO_LOCATE = "auto_locate"


def get_bool(session: Session, integration: str, key: str, *, default: bool) -> bool:
    """The stored boolean for (integration, key), or *default* when unset or unreadable."""
    row = session.get(IntegrationSetting, (integration, key))
    if row is None:
        return default
    try:
        value = json.loads(row.value_json)
    except (TypeError, ValueError):
        return default
    return value if isinstance(value, bool) else default


def set_bool(session: Session, integration: str, key: str, value: bool) -> None:
    """Persist a boolean for (integration, key). The caller commits."""
    row = session.get(IntegrationSetting, (integration, key))
    if row is None:
        row = IntegrationSetting(integration=integration, key=key)
    row.value_json = json.dumps(bool(value))
    row.updated_at = datetime.now(UTC)
    session.add(row)


def otbr_auto_locate_enabled(session: Session) -> bool:
    """OTBR auto-locate switch (default on)."""
    return get_bool(session, OTBR, OTBR_AUTO_LOCATE, default=True)
