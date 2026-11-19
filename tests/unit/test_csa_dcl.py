"""CSA DCL lookup: client parsing, merge priority, link dedupe, and the explicit-only route."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select

from app import csa
from app.app import create_app
from app.database import get_session
from app.models import DeviceProtocol, FieldSource, Product, ProductLink, ProductLinkKind

MODEL = {
    "model": {
        "vid": 4447,
        "pid": 8194,
        "productName": "Smart Plug",
        "productLabel": "Plug label",
        "partNumber": "SP-1",
        "deviceTypeId": 0x010A,
        "productUrl": "https://example.com/plug",
        "supportUrl": "https://example.com/support",
        "userManualUrl": "https://example.com/manual.pdf",
    }
}
VENDOR = {"vendorInfo": {"vendorName": "Acme Corp", "vendorLandingPageURL": "https://acme.test"}}
NOT_FOUND = {"code": 5, "message": "not found"}


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _dcl_handler(model=MODEL, vendor=VENDOR, model_status=200, vendor_status=200):
    def handler(request: httpx.Request) -> httpx.Response:
        if "/model/models/" in request.url.path:
            return httpx.Response(model_status, json=model)
        return httpx.Response(vendor_status, json=vendor)

    return handler


def test_fetch_parses_model_and_vendor():
    model, vendor = csa._fetch_with(_client(_dcl_handler()), 4447, 8194)
    assert model == csa.DclModel(
        product_name="Smart Plug",
        product_label="Plug label",
        part_number="SP-1",
        product_url="https://example.com/plug",
        support_url="https://example.com/support",
        manual_url="https://example.com/manual.pdf",
        device_type_id=0x010A,
    )
    assert vendor == csa.DclVendor(name="Acme Corp", landing_page_url="https://acme.test")


@pytest.mark.parametrize("raw_type", [None, 0, -1, True, "266", 266.0, 0x100000000])
def test_invalid_or_missing_dcl_type_keeps_existing_value(engine, raw_type):
    model_fields = {} if raw_type is None else {"deviceTypeId": raw_type}
    with _client(_dcl_handler(model={"model": model_fields})) as client:
        model = csa.lookup_model(client, 4447, 8194)
    assert model is not None
    assert model.device_type_id is None
    with Session(engine) as session:
        product = _product(
            session, device_type_id=0xFFF10001, device_type_id_source=FieldSource.dcl
        )
        assert not csa.apply(session, product, model, None)
        assert product.device_type_id == 0xFFF10001
        assert product.device_type_id_source is FieldSource.dcl


def test_unknown_product_is_none_not_an_error():
    handler = _dcl_handler(model=NOT_FOUND, model_status=404)
    assert csa._fetch_with(_client(handler), 1, 2) == (None, None)


def test_vendor_failure_keeps_the_model():
    handler = _dcl_handler(vendor=NOT_FOUND, vendor_status=404)
    model, vendor = csa._fetch_with(_client(handler), 4447, 8194)
    assert model is not None and vendor is None


def test_server_error_and_network_failure_raise():
    with pytest.raises(csa.DclError):
        csa._fetch_with(_client(_dcl_handler(model={"x": 1}, model_status=500)), 1, 2)

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    with pytest.raises(csa.DclError):
        csa._fetch_with(_client(boom), 1, 2)


@pytest.fixture
def engine(tmp_path: Path):
    eng = create_engine(f"sqlite:///{tmp_path / 'dcl.db'}")
    SQLModel.metadata.create_all(eng)
    return eng


def _product(session: Session, **kw) -> Product:
    p = Product(
        name=kw.pop("name", "Unresolved product"),
        protocol=DeviceProtocol.matter,
        vendor_id=4447,
        product_id=8194,
        **kw,
    )
    session.add(p)
    session.commit()
    return p


def test_apply_fills_blanks_and_adds_links(engine):
    model, vendor = csa._fetch_with(_client(_dcl_handler()), 4447, 8194)
    assert model is not None
    with Session(engine) as s:
        p = _product(s)
        assert csa.apply(s, p, model, vendor)
        s.commit()
        assert (p.name, p.vendor, p.model) == ("Smart Plug", "Acme Corp", "SP-1")
        assert p.vendor_source == FieldSource.dcl
        assert p.device_type_id == 0x010A
        assert p.device_type_id_source is FieldSource.dcl
        links = s.exec(select(ProductLink).where(ProductLink.product_record_id == p.id)).all()
        assert {link.kind: link.url for link in links} == {
            ProductLinkKind.support: "https://example.com/support",
            ProductLinkKind.documentation: "https://example.com/manual.pdf",
            ProductLinkKind.homepage: "https://example.com/plug",
            ProductLinkKind.manufacturer: "https://acme.test",
        }
        assert all(link.label == link.url for link in links)


def test_apply_never_overrides_higher_sources_or_duplicates_links(engine):
    model, vendor = csa._fetch_with(_client(_dcl_handler()), 4447, 8194)
    assert model is not None
    with Session(engine) as s:
        p = _product(
            s,
            name="My plug",
            name_source=FieldSource.user,
            vendor="Mine",
            device_type_id=0xFFF10001,
            device_type_id_source=FieldSource.user,
        )
        p.vendor_source = FieldSource.matter
        s.add(p)
        s.add(
            ProductLink(
                product_record_id=p.id,
                kind=ProductLinkKind.other,
                url="https://example.com/support",
                label="Vendor help",
                position=0,
            )
        )
        s.commit()
        csa.apply(s, p, model, vendor)
        s.commit()
        assert (p.name, p.vendor) == ("My plug", "Mine")
        assert p.model == "SP-1"
        assert p.device_type_id == 0xFFF10001
        assert p.device_type_id_source is FieldSource.user
        links = s.exec(select(ProductLink).where(ProductLink.product_record_id == p.id)).all()
        urls = [link.url for link in links]
        assert urls.count("https://example.com/support") == 1
        assert next(link for link in links if link.url == "https://example.com/support").label == (
            "Vendor help"
        )
        assert len(urls) == 4
        assert csa.apply(s, p, model, vendor) is True  # model re-applied at same priority
        s.commit()
        assert len(s.exec(select(ProductLink)).all()) == 4  # still no duplicate links


def test_apply_ignores_non_http_links(engine):
    model = csa.DclModel(None, None, None, "javascript:alert(1)", "ftp://x.test/f", None)
    with Session(engine) as s:
        p = _product(s)
        assert csa.apply(s, p, model, None) is False
        assert s.exec(select(ProductLink)).all() == []


@pytest.fixture
def client(engine) -> Iterator[TestClient]:
    os.environ.setdefault("SUPERVISOR_TOKEN", "")

    def _override() -> Iterator[Session]:
        with Session(engine) as session:
            yield session

    app = create_app()
    app.dependency_overrides[get_session] = _override
    yield TestClient(app)


def test_route_lookup_updates_product_and_shows_banner(client, engine, monkeypatch):
    calls: list[tuple[int, int]] = []

    def fake_fetch(vid, pid):
        calls.append((vid, pid))
        return csa._fetch_with(_client(_dcl_handler()), vid, pid)

    monkeypatch.setattr(csa, "fetch", fake_fetch)
    with Session(engine) as s:
        pid = _product(s).id
    page = client.get(f"/products/{pid}")
    assert "Look up in CSA DCL" in page.text
    assert calls == []  # rendering a page never contacts the DCL
    r = client.post(f"/products/{pid}/dcl-lookup", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].endswith(f"/products/{pid}?dcl=ok")
    assert calls == [(4447, 8194)]
    page = client.get(r.headers["location"])
    assert "Updated from the CSA DCL." in page.text
    assert "Smart Plug" in page.text
    assert "On/Off Plug-in Unit (0x010A)" in page.text


def test_route_keeps_device_type_edited_during_dcl_lookup(client, engine, monkeypatch):
    with Session(engine) as session:
        product_id = _product(session).id

    def fake_fetch(vid, pid):
        with Session(engine) as session:
            product = session.get(Product, product_id)
            assert product is not None
            product.device_type_id = 0xFFF10001
            product.device_type_id_source = FieldSource.user
            session.commit()
        return csa._fetch_with(_client(_dcl_handler()), vid, pid)

    monkeypatch.setattr(csa, "fetch", fake_fetch)
    response = client.post(f"/products/{product_id}/dcl-lookup", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].endswith("?dcl=ok")
    product = client.get(f"/api/products/{product_id}").json()
    assert product["device_type_id"] == 0xFFF10001
    assert product["sources"]["device_type_id"] == FieldSource.user.value


@pytest.mark.parametrize(
    ("result", "status"), [((None, None), "none"), (csa.DclError("down"), "error")]
)
def test_route_reports_missing_entry_and_outage(client, engine, monkeypatch, result, status):
    def fake_fetch(vid, pid):
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(csa, "fetch", fake_fetch)
    with Session(engine) as s:
        pid = _product(s).id
    r = client.post(f"/products/{pid}/dcl-lookup", follow_redirects=False)
    assert r.headers["location"].endswith(f"?dcl={status}")


def test_route_rejects_products_without_matter_ids(client, engine, monkeypatch):
    monkeypatch.setattr(csa, "fetch", lambda *a: pytest.fail("must not contact the DCL"))
    with Session(engine) as s:
        homekit = Product(name="HK", protocol=DeviceProtocol.homekit, vendor_id=1, product_id=2)
        no_ids = Product(name="X", protocol=DeviceProtocol.matter)
        s.add(homekit)
        s.add(no_ids)
        s.commit()
        ids = [homekit.id, no_ids.id]
    for pid in ids:
        assert client.post(f"/products/{pid}/dcl-lookup").status_code == 400
        assert "Look up in CSA DCL" not in client.get(f"/products/{pid}").text
