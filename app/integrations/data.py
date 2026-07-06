"""Per-device integration data helpers (B.12).

Upsert and read the ``DeviceIntegrationData`` table so integrations
don't hand-roll the same queries.  All functions accept an open
SQLModel Session so callers control transaction boundaries.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlmodel import Session, select

from ..models import DeviceIntegrationData


def upsert(
    session: Session,
    *,
    device_id: str,
    integration: str,
    payload: dict[str, Any],
) -> DeviceIntegrationData:
    """Write (or overwrite) the per-device payload for *integration*.

    Returns the persisted row.  The caller must commit the session.
    """
    import json

    now = datetime.now(UTC)
    row = session.exec(
        select(DeviceIntegrationData).where(
            DeviceIntegrationData.device_id == device_id,  # type: ignore[attr-defined]
            DeviceIntegrationData.integration == integration,  # type: ignore[attr-defined]
        )
    ).first()

    if row is None:
        row = DeviceIntegrationData(
            device_id=device_id,
            integration=integration,
            payload_json=json.dumps(payload, default=str),
            retrieved_at=now,
        )
        session.add(row)
    else:
        row.payload_json = json.dumps(payload, default=str)
        row.retrieved_at = now
        session.add(row)

    return row


def read(
    session: Session,
    *,
    device_id: str,
    integration: str,
) -> dict[str, Any] | None:
    """Return the stored payload dict for a device+integration pair, or None."""
    import json

    row = session.exec(
        select(DeviceIntegrationData).where(
            DeviceIntegrationData.device_id == device_id,  # type: ignore[attr-defined]
            DeviceIntegrationData.integration == integration,  # type: ignore[attr-defined]
        )
    ).first()
    if row is None:
        return None
    return json.loads(row.payload_json)


def read_all_for_device(
    session: Session,
    *,
    device_id: str,
) -> list[DeviceIntegrationData]:
    """Return all stored integration-data rows for a device, ordered by integration."""
    return list(
        session.exec(
            select(DeviceIntegrationData)
            .where(DeviceIntegrationData.device_id == device_id)  # type: ignore[attr-defined]
            .order_by(DeviceIntegrationData.integration)  # type: ignore[attr-defined]
        ).all()
    )
