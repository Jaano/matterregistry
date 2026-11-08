"""CSA Distributed Compliance Ledger (DCL) lookup for Matter products.

The DCL is the CSA's public registry of certified Matter products, keyed by
vendor ID and product ID. A lookup sends those two numbers to
``on.dcl.csa-iot.org``, so it is only ever run from an explicit per-Product
user action - never from an integration sync or a background task.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
from sqlmodel import Session, col, select

from .models import FieldSource, Product, ProductLink, ProductLinkKind
from .services import set_product_field

DCL_BASE_URL = "https://on.dcl.csa-iot.org"
DCL_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
_NOT_FOUND_CODE = 5


class DclError(Exception):
    """The DCL could not be reached or answered unexpectedly."""


@dataclass(frozen=True)
class DclModel:
    product_name: str | None
    product_label: str | None
    part_number: str | None
    product_url: str | None
    support_url: str | None
    manual_url: str | None


@dataclass(frozen=True)
class DclVendor:
    name: str | None
    landing_page_url: str | None


def _text(value: Any) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def _get(client: httpx.Client, path: str) -> dict[str, Any] | None:
    """GET a DCL resource; ``None`` when the DCL has no such entry."""
    try:
        resp = client.get(f"{DCL_BASE_URL}{path}")
        body = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise DclError(str(exc)) from exc
    if not isinstance(body, dict):
        raise DclError("unexpected DCL response")
    if body.get("code") == _NOT_FOUND_CODE:
        return None
    if resp.status_code >= 400:
        raise DclError(f"DCL returned HTTP {resp.status_code}")
    return body


def lookup_model(client: httpx.Client, vendor_id: int, product_id: int) -> DclModel | None:
    body = _get(client, f"/dcl/model/models/{vendor_id}/{product_id}")
    model = body.get("model") if body else None
    if not isinstance(model, dict):
        return None
    return DclModel(
        product_name=_text(model.get("productName")),
        product_label=_text(model.get("productLabel")),
        part_number=_text(model.get("partNumber")),
        product_url=_text(model.get("productUrl")),
        support_url=_text(model.get("supportUrl")),
        manual_url=_text(model.get("userManualUrl")),
    )


def lookup_vendor(client: httpx.Client, vendor_id: int) -> DclVendor | None:
    body = _get(client, f"/dcl/vendorinfo/vendors/{vendor_id}")
    info = body.get("vendorInfo") if body else None
    if not isinstance(info, dict):
        return None
    return DclVendor(
        name=_text(info.get("vendorName")),
        landing_page_url=_text(info.get("vendorLandingPageURL")),
    )


def fetch(vendor_id: int, product_id: int) -> tuple[DclModel | None, DclVendor | None]:
    """Look up a model and its vendor. Raises :class:`DclError` when unreachable.

    A vendor lookup failure alone is tolerated: the model data is still useful.
    """
    with httpx.Client(timeout=DCL_TIMEOUT, follow_redirects=False) as client:
        return _fetch_with(client, vendor_id, product_id)


def _fetch_with(
    client: httpx.Client, vendor_id: int, product_id: int
) -> tuple[DclModel | None, DclVendor | None]:
    model = lookup_model(client, vendor_id, product_id)
    if model is None:
        return None, None
    try:
        vendor = lookup_vendor(client, vendor_id)
    except DclError:
        vendor = None
    return model, vendor


def apply(session: Session, product: Product, model: DclModel, vendor: DclVendor | None) -> bool:
    """Merge DCL data into *product* at DCL priority and add missing links.

    Fields written by a higher-priority source (user, scanned, Matter, ...) are
    left alone. Existing links are never replaced. Returns True when anything changed.
    """
    changed = False
    if vendor and vendor.name:
        changed |= set_product_field(product, "vendor", vendor.name, FieldSource.dcl)
    if model.product_name:
        changed |= set_product_field(product, "name", model.product_name, FieldSource.dcl)
    if model.part_number:
        changed |= set_product_field(product, "model", model.part_number, FieldSource.dcl)
    if changed:
        session.add(product)

    wanted: list[tuple[ProductLinkKind, str | None]] = [
        (ProductLinkKind.support, model.support_url),
        (ProductLinkKind.documentation, model.manual_url),
        (ProductLinkKind.homepage, model.product_url),
        (ProductLinkKind.manufacturer, vendor.landing_page_url if vendor else None),
    ]
    existing = session.exec(
        select(ProductLink)
        .where(ProductLink.product_record_id == product.id)
        .order_by(col(ProductLink.position))
    ).all()
    known_urls = {link.url for link in existing}
    position = max((link.position for link in existing), default=-1) + 1
    for kind, url in wanted:
        if not url or not url.lower().startswith(("http://", "https://")) or url in known_urls:
            continue
        session.add(
            ProductLink(
                product_record_id=product.id,
                kind=kind,
                url=url,
                label="CSA DCL",
                position=position,
            )
        )
        known_urls.add(url)
        position += 1
        changed = True
    return changed
