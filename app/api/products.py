from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlmodel import Session, col, select

from ..audit import log as audit_log
from ..database import get_session
from ..models import PRODUCT_SOURCED_FIELDS, Device, FieldSource, Product, ProductLink
from .schemas import (
    ProductCreate,
    ProductLinkCreate,
    ProductLinkOut,
    ProductLinkUpdate,
    ProductOut,
    ProductUpdate,
)

router = APIRouter(prefix="/products", tags=["products"])


def _load_product(product_id: str, session: Session) -> Product:
    product = session.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")
    return product


def _product_out(product: Product, session: Session) -> ProductOut:
    out = ProductOut.model_validate(product)
    out.sources = {
        field: getattr(product, f"{field}_source", FieldSource.generated).value
        for field in PRODUCT_SOURCED_FIELDS
    }
    out.links = [
        ProductLinkOut.model_validate(link)
        for link in session.exec(
            select(ProductLink)
            .where(ProductLink.product_record_id == product.id)
            .order_by(col(ProductLink.position), col(ProductLink.id))
        ).all()
    ]
    return out


@router.get("", response_model=list[ProductOut])
def list_products(session: Session = Depends(get_session)):
    return [_product_out(product, session) for product in session.exec(select(Product)).all()]


@router.post("", response_model=ProductOut, status_code=201)
def create_product(data: ProductCreate, session: Session = Depends(get_session)):
    product = Product(**data.model_dump())
    for field in PRODUCT_SOURCED_FIELDS:
        if getattr(product, field, None) is not None:
            setattr(product, f"{field}_source", FieldSource.user)
    session.add(product)
    audit_log(
        session,
        action="product.create",
        entity=f"product:{product.id}",
        reason="api.products.create",
    )
    session.commit()
    session.refresh(product)
    return _product_out(product, session)


@router.get("/{product_id}", response_model=ProductOut)
def get_product(product_id: str, session: Session = Depends(get_session)):
    return _product_out(_load_product(product_id, session), session)


@router.patch("/{product_id}", response_model=ProductOut)
def update_product(product_id: str, data: ProductUpdate, session: Session = Depends(get_session)):
    product = _load_product(product_id, session)
    for field, value in data.model_dump(exclude_unset=True).items():
        if getattr(product, field) != value:
            setattr(product, field, value)
            setattr(product, f"{field}_source", FieldSource.user)
    product.updated_at = datetime.now(UTC)
    session.add(product)
    audit_log(
        session,
        action="product.update",
        entity=f"product:{product_id}",
        reason="api.products.update",
    )
    session.commit()
    session.refresh(product)
    return _product_out(product, session)


@router.delete("/{product_id}", status_code=204)
def delete_product(product_id: str, session: Session = Depends(get_session)):
    product = _load_product(product_id, session)
    if session.exec(select(Device.id).where(Device.product_record_id == product.id)).first():
        raise HTTPException(status_code=409, detail="Reassign devices before deleting this product")
    session.delete(product)
    audit_log(
        session,
        action="product.delete",
        entity=f"product:{product_id}",
        reason="api.products.delete",
    )
    session.commit()
    return Response(status_code=204)


@router.post("/{product_id}/links", response_model=ProductLinkOut, status_code=201)
def create_product_link(
    product_id: str, data: ProductLinkCreate, session: Session = Depends(get_session)
):
    _load_product(product_id, session)
    link = ProductLink(product_record_id=product_id, **data.model_dump(mode="json"))
    session.add(link)
    audit_log(
        session,
        action="product_link.create",
        entity=f"product_link:{link.id}",
        reason="api.products.link_create",
    )
    session.commit()
    session.refresh(link)
    return ProductLinkOut.model_validate(link)


@router.patch("/{product_id}/links/{link_id}", response_model=ProductLinkOut)
def update_product_link(
    product_id: str,
    link_id: str,
    data: ProductLinkUpdate,
    session: Session = Depends(get_session),
):
    link = session.get(ProductLink, link_id)
    if link is None or link.product_record_id != product_id:
        raise HTTPException(status_code=404, detail="Product link not found")
    for field, value in data.model_dump(exclude_unset=True, mode="json").items():
        setattr(link, field, value)
    session.add(link)
    audit_log(
        session,
        action="product_link.update",
        entity=f"product_link:{link_id}",
        reason="api.products.link_update",
    )
    session.commit()
    session.refresh(link)
    return ProductLinkOut.model_validate(link)


@router.delete("/{product_id}/links/{link_id}", status_code=204)
def delete_product_link(product_id: str, link_id: str, session: Session = Depends(get_session)):
    link = session.get(ProductLink, link_id)
    if link is None or link.product_record_id != product_id:
        raise HTTPException(status_code=404, detail="Product link not found")
    session.delete(link)
    audit_log(
        session,
        action="product_link.delete",
        entity=f"product_link:{link_id}",
        reason="api.products.link_delete",
    )
    session.commit()
    return Response(status_code=204)
