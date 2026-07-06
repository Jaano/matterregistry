from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session

from ..audit import log as audit_log
from ..database import get_session
from ..models import FieldSource, Property
from .schemas import PropertyOut, PropertyUpdate

router = APIRouter(prefix="/devices/{device_id}/properties", tags=["properties"])


def _load_prop(id: str, session: Session) -> Property:
    prop = session.get(Property, id)
    if not prop:
        raise HTTPException(status_code=404, detail="Property not found")
    return prop


@router.patch("/{id}", response_model=PropertyOut)
def update_property(
    device_id: str, id: str, data: PropertyUpdate, session: Session = Depends(get_session)
):
    prop = _load_prop(id, session)
    if prop.device_id != device_id:
        raise HTTPException(status_code=404, detail="Property not found")
    patch = data.model_dump(exclude_unset=True)
    for field, value in patch.items():
        setattr(prop, field, value)
    # Editing the value is a human action - provenance becomes user.
    if "value" in patch:
        prop.source = FieldSource.user
    session.add(prop)
    audit_log(
        session,
        action="property.update",
        entity=f"property:{id}",
        reason="api.properties.update",
    )
    session.commit()
    session.refresh(prop)
    return PropertyOut.model_validate(prop)


@router.delete("/{id}", status_code=204)
def delete_property(device_id: str, id: str, session: Session = Depends(get_session)):
    prop = _load_prop(id, session)
    if prop.device_id != device_id:
        raise HTTPException(status_code=404, detail="Property not found")
    audit_log(
        session,
        action="property.delete",
        entity=f"property:{id}",
        reason="api.properties.delete",
    )
    session.delete(prop)
    session.commit()
