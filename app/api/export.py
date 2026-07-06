import json
from datetime import UTC, datetime

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from sqlmodel import Session

from ..audit import log as audit_log
from ..database import get_session
from ..exporter import build_export
from ..settings import settings

router = APIRouter(tags=["export"])


@router.get("/export")
def export_backup(session: Session = Depends(get_session)):
    """Return a full JSON backup of all devices, credentials, attachments, and audit log."""
    data = build_export(session, app_version=settings.version)
    audit_log(session, action="export.full", entity="export:json", reason="api.export")
    session.commit()
    filename = f"matterregistry-{datetime.now(UTC).strftime('%Y-%m-%d')}.json"
    return Response(
        content=json.dumps(data, ensure_ascii=False, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
