import hashlib

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel
from sqlmodel import Session, select

from ..audit import log as audit_log
from ..database import get_session
from ..models import Attachment, AttachmentKind, Device

router = APIRouter(tags=["attachments"])

_MAX_FILE_BYTES = 10 * 1024 * 1024  # 10 MB per file
_MAX_DEVICE_BYTES = 50 * 1024 * 1024  # 50 MB aggregate per device

_ALLOWED_MIME: dict[str, AttachmentKind] = {
    "image/jpeg": AttachmentKind.image,
    "image/png": AttachmentKind.image,
    "image/webp": AttachmentKind.image,
    "image/gif": AttachmentKind.image,
    "application/pdf": AttachmentKind.pdf,
}


class AttachmentMeta(BaseModel):
    id: str
    device_id: str
    kind: AttachmentKind
    filename: str
    mime_type: str
    sha256: str
    size_bytes: int

    model_config = {"from_attributes": True}


def _load_device(id: str, session: Session) -> Device:
    device = session.get(Device, id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    return device


def _load_attachment(id: str, session: Session) -> Attachment:
    att = session.get(Attachment, id)
    if not att:
        raise HTTPException(status_code=404, detail="Attachment not found")
    return att


@router.get("/devices/{device_id}/attachments", response_model=list[AttachmentMeta])
def list_attachments(device_id: str, session: Session = Depends(get_session)):
    _load_device(device_id, session)
    rows = session.exec(select(Attachment).where(Attachment.device_id == device_id)).all()
    return [AttachmentMeta.model_validate(r) for r in rows]


@router.post("/devices/{device_id}/attachments", response_model=AttachmentMeta, status_code=201)
async def upload_attachment(
    device_id: str,
    request: Request,
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
):
    _load_device(device_id, session)

    mime = file.content_type or ""
    if mime not in _ALLOWED_MIME:
        raise HTTPException(status_code=415, detail=f"Unsupported media type: {mime}")

    # Read with size guard - never load more than _MAX_FILE_BYTES+1
    content = await file.read(_MAX_FILE_BYTES + 1)
    if len(content) > _MAX_FILE_BYTES:
        raise HTTPException(status_code=413, detail="File exceeds 10 MB limit")

    # Check aggregate size
    existing = session.exec(select(Attachment).where(Attachment.device_id == device_id)).all()
    total_existing = sum(a.size_bytes for a in existing)
    if total_existing + len(content) > _MAX_DEVICE_BYTES:
        raise HTTPException(status_code=413, detail="Device attachment total exceeds 50 MB limit")

    sha = hashlib.sha256(content).hexdigest()
    att = Attachment(
        device_id=device_id,
        kind=_ALLOWED_MIME[mime],
        filename=file.filename or "upload",
        mime_type=mime,
        sha256=sha,
        size_bytes=len(content),
        content=content,
    )
    session.add(att)
    audit_log(
        session,
        action="attachment.create",
        entity=f"attachment:{att.id}",
        reason="api.attachments.upload",
    )
    session.commit()
    session.refresh(att)
    return AttachmentMeta.model_validate(att)


@router.get("/devices/{device_id}/attachments/{id}")
def download_attachment(device_id: str, id: str, session: Session = Depends(get_session)):
    att = _load_attachment(id, session)
    if att.device_id != device_id:
        raise HTTPException(status_code=404, detail="Attachment not found")
    return Response(
        content=att.content,
        media_type=att.mime_type,
        headers={"Content-Disposition": f'inline; filename="{att.filename}"'},
    )


@router.get("/devices/{device_id}/attachments/{id}/meta", response_model=AttachmentMeta)
def attachment_meta(device_id: str, id: str, session: Session = Depends(get_session)):
    att = _load_attachment(id, session)
    if att.device_id != device_id:
        raise HTTPException(status_code=404, detail="Attachment not found")
    return AttachmentMeta.model_validate(att)


@router.delete("/devices/{device_id}/attachments/{id}", status_code=204)
def delete_attachment(device_id: str, id: str, session: Session = Depends(get_session)):
    att = _load_attachment(id, session)
    if att.device_id != device_id:
        raise HTTPException(status_code=404, detail="Attachment not found")
    audit_log(
        session,
        action="attachment.delete",
        entity=f"attachment:{id}",
        reason="api.attachments.delete",
    )
    session.delete(att)
    session.commit()
