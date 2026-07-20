"""Focused web-route tests for the Product catalog (A.23) and Product merge.

Exercises `/products` list/filter, create, edit/update, Device
reassignment via `/devices/{id}`, the delete guard, and `/products/{id}/merge`,
all through FastAPI's TestClient against the real Jinja templates so template
rendering errors (e.g. undefined variables, TemplateNotFound) surface
as test failures rather than only at request time in production.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select

from app.app import create_app
from app.database import get_session
from app.models import Device, DeviceProtocol, FieldSource, Product, ProductLink, ProductLinkKind


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    os.environ.setdefault("SUPERVISOR_TOKEN", "")
    # A real sqlite file (rather than "sqlite://") so every connection the
    # TestClient's worker thread opens sees the same schema/data.
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    SQLModel.metadata.create_all(engine)

    def _get_session_override() -> Iterator[Session]:
        with Session(engine) as session:
            yield session

    app = create_app()
    app.dependency_overrides[get_session] = _get_session_override
    app.state.test_engine = engine
    # Plain instantiation (no context manager) - avoids triggering the app's
    # lifespan, which creates the HA App's `/config` data directory and is
    # irrelevant to these route/template-rendering tests.
    yield TestClient(app)


def _engine(client: TestClient):
    return client.app.state.test_engine  # type: ignore[union-attr]


def test_product_list_empty_state(client: TestClient):
    r = client.get("/products")
    assert r.status_code == 200
    assert "No products yet" in r.text


def test_product_create_list_detail_and_links(client: TestClient):
    r = client.post(
        "/products",
        data={
            "name": "Aqara Hub",
            "protocol": "matter",
            "vendor": "Aqara",
            "model": "M2",
            "vendor_id": "0xFFF1",
            "product_id": "0x8001",
            "description": "A hub",
            "link_kind": ["homepage", "image"],
            "link_url": ["https://example.com/hub", "https://example.com/hub.png"],
            "link_label": ["Homepage", ""],
            "link_alt": ["", "Hub photo"],
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    product_id = r.headers["location"].rsplit("/", 1)[-1]

    with Session(_engine(client)) as session:
        product = session.get(Product, product_id)
        assert product is not None
        assert product.vendor == "Aqara"
        assert product.vid_pid_display == "0xFFF1 / 0x8001"
        assert len(product.links) == 2

    detail = client.get(f"/products/{product_id}")
    assert detail.status_code == 200
    assert "Aqara Hub" in detail.text
    assert "example.com/hub" in detail.text

    listing = client.get("/products")
    assert "Aqara Hub" in listing.text

    filtered = client.get("/products", params={"protocol": "homekit"})
    assert "Aqara Hub" not in filtered.text


def test_product_update_replaces_links_and_fields(client: TestClient):
    created = client.post(
        "/products",
        data={"name": "Eve Door", "protocol": "homekit", "vendor": "Eve"},
        follow_redirects=False,
    )
    product_id = created.headers["location"].rsplit("/", 1)[-1]

    r = client.post(
        f"/products/{product_id}",
        data={
            "name": "Eve Door",
            "protocol": "homekit",
            "vendor": "Eve Systems",
            "link_kind": ["support"],
            "link_url": ["https://example.com/support"],
            "link_label": [""],
            "link_alt": [""],
        },
        follow_redirects=False,
    )
    assert r.status_code == 303

    with Session(_engine(client)) as session:
        product = session.get(Product, product_id)
        assert product is not None
        assert product.vendor == "Eve Systems"
        assert len(product.links) == 1
        assert product.links[0].kind.value == "support"


def test_device_form_reassigns_product_and_blocks_referenced_delete(client: TestClient):
    created = client.post(
        "/products",
        data={"name": "Aqara Hub", "protocol": "matter", "vendor": "Aqara"},
        follow_redirects=False,
    )
    product_id = created.headers["location"].rsplit("/", 1)[-1]

    with Session(_engine(client)) as session:
        device = Device(name="Living Room Hub")
        session.add(device)
        session.commit()
        session.refresh(device)
        device_id = device.id

    r = client.post(
        f"/devices/{device_id}",
        data={
            "name": "Living Room Hub",
            "room": "",
            "serial": "",
            "notes": "",
            "status": "active",
            "warranty_until": "",
            "network_type": "",
            "product_record_id": product_id,
        },
        follow_redirects=False,
    )
    assert r.status_code == 303

    with Session(_engine(client)) as session:
        device = session.get(Device, device_id)
        assert device is not None
        assert device.product_record_id == product_id

    edit_page = client.get(f"/devices/{device_id}/edit")
    assert edit_page.status_code == 200
    assert "Aqara Hub" in edit_page.text

    blocked = client.delete(f"/products/{product_id}")
    assert blocked.status_code == 409

    with Session(_engine(client)) as session:
        device = session.get(Device, device_id)
        assert device is not None
        session.delete(device)
        session.commit()

    allowed = client.delete(f"/products/{product_id}")
    assert allowed.status_code == 200
    assert allowed.headers.get("hx-redirect", "").endswith("/products")


def test_product_merge_preview_and_confirm(client: TestClient):
    with Session(_engine(client)) as session:
        source = Product(
            name="Aqara Hub M2",
            protocol=DeviceProtocol.matter,
            vendor="Aqara",
            vendor_source=FieldSource.user,
            description="Old description",
            description_source=FieldSource.mdns,
        )
        target = Product(
            name="Aqara Hub",
            protocol=DeviceProtocol.matter,
            model="M2",
            model_source=FieldSource.user,
        )
        session.add(source)
        session.add(target)
        session.commit()
        session.refresh(source)
        session.refresh(target)
        source_id, target_id = source.id, target.id
        session.add(
            ProductLink(
                product_record_id=source_id,
                kind=ProductLinkKind.homepage,
                url="https://example.com/hub",
            )
        )
        device = Device(name="Kitchen hub", product_record_id=source_id, product_record=source)
        session.add(device)
        session.commit()
        device_id = device.id

    preview = client.get(f"/products/{source_id}/merge", params={"target_id": target_id})
    assert preview.status_code == 200
    assert "Old description" in preview.text

    r = client.post(
        f"/products/{source_id}/merge", data={"target_id": target_id}, follow_redirects=False
    )
    assert r.status_code == 303
    assert r.headers["location"].endswith(f"/products/{target_id}")

    with Session(_engine(client)) as session:
        assert session.get(Product, source_id) is None
        target = session.get(Product, target_id)
        assert target is not None
        # target's user-sourced model wins; source's user-sourced vendor and
        # mdns-sourced description fill target's blank fields.
        assert target.model == "M2"
        assert target.vendor == "Aqara"
        assert target.description == "Old description"

        device = session.get(Device, device_id)
        assert device is not None
        assert device.product_record_id == target_id

        links = session.exec(
            select(ProductLink).where(ProductLink.product_record_id == target_id)
        ).all()
        assert len(links) == 1


def test_product_merge_rejects_mismatched_protocol(client: TestClient):
    with Session(_engine(client)) as session:
        matter_product = Product(name="Aqara Hub", protocol=DeviceProtocol.matter)
        homekit_product = Product(name="Eve Door", protocol=DeviceProtocol.homekit)
        session.add(matter_product)
        session.add(homekit_product)
        session.commit()
        session.refresh(matter_product)
        session.refresh(homekit_product)
        matter_id, homekit_id = matter_product.id, homekit_product.id

    r = client.post(
        f"/products/{matter_id}/merge", data={"target_id": homekit_id}, follow_redirects=False
    )
    assert r.status_code == 409

    with Session(_engine(client)) as session:
        assert session.get(Product, matter_id) is not None
        assert session.get(Product, homekit_id) is not None
