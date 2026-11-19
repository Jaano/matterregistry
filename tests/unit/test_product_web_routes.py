"""Focused web-route tests for the Product catalog (A.23) and Product merge.

Exercises `/products` list/filter, create, edit/update, Device
reassignment via `/devices/{id}`, the delete guard, and `/products/{id}/merge`,
all through FastAPI's TestClient against the real Jinja templates so template
rendering errors (e.g. undefined variables, TemplateNotFound) surface
as test failures rather than only at request time in production.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from markupsafe import escape
from sqlmodel import Session, SQLModel, create_engine, select

from app.app import create_app
from app.database import get_session
from app.matter import decode_setup_payload
from app.models import (
    Device,
    DeviceProtocol,
    FieldSource,
    Product,
    ProductLink,
    ProductLinkKind,
    Property,
    PropertyType,
)
from app.services import apply_scan_payload

MATTER_PAYLOAD = "MT:-24J0AFN00KA0648G00"
HOMEKIT_PAYLOAD = "X-HM://00527Y8011A2B"


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
    assert 'class="mr-page-head"' not in r.text

    partial = client.get("/products", headers={"HX-Request": "true"})
    assert partial.status_code == 200
    assert 'class="mr-results-actions"' in partial.text
    assert partial.text.index('href="/products/new"') < partial.text.index('class="mr-view-switch"')


def test_new_device_form_is_single_column_and_accepts_product(client: TestClient):
    with Session(_engine(client)) as session:
        product = Product(name="Aqara Hub", protocol=DeviceProtocol.matter)
        session.add(product)
        session.commit()
        product_id = product.id

    form = client.get("/devices/new")
    assert form.status_code == 200
    assert 'class="mr-new-device"' in form.text
    assert 'name="product_record_id"' in form.text
    assert "One durable record" not in form.text

    created = client.post(
        "/devices",
        data={"name": "Hall hub", "product_record_id": product_id},
        follow_redirects=False,
    )
    assert created.status_code == 303
    with Session(_engine(client)) as session:
        device = session.exec(select(Device).where(Device.name == "Hall hub")).one()
        assert device.product_record_id == product_id


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        ('<img src=x onerror="alert(1)">', "Unrecognised payload prefix"),
        ("MT:-----", "Out-of-range base-38 value"),
        ("X-HM://SHORT", "too short"),
    ],
)
def test_new_device_invalid_scan_keeps_form_and_creates_nothing(
    client: TestClient, payload: str, error: str, caplog: pytest.LogCaptureFixture
):
    caplog.set_level("INFO", logger="matterregistry.audit")
    name = 'Hall "lamp" & sensor'
    response = client.post(
        "/devices", data={"name": name, "qr_payload": payload}, follow_redirects=False
    )

    assert response.status_code == 400
    assert 'id="new-device-scan-error" role="alert"' in response.text
    assert error in response.text
    assert f'value="{escape(name)}"' in response.text
    assert f'value="{escape(payload)}"' in response.text
    assert 'aria-describedby="new-device-scan-error"' in response.text
    assert "action=device.create" not in caplog.text
    assert "action=device.scan" not in caplog.text
    with Session(_engine(client)) as session:
        for model in (Device, Product, Property):
            assert session.exec(select(model)).first() is None


@pytest.mark.parametrize(
    ("protocol", "wrong_payload", "valid_payload"),
    [
        (DeviceProtocol.matter, HOMEKIT_PAYLOAD, MATTER_PAYLOAD),
        (DeviceProtocol.homekit, MATTER_PAYLOAD, HOMEKIT_PAYLOAD),
    ],
)
def test_new_device_protocol_conflict_keeps_selected_product_and_allows_retry(
    client: TestClient, protocol: DeviceProtocol, wrong_payload: str, valid_payload: str
):
    with Session(_engine(client)) as session:
        sku = decode_setup_payload(MATTER_PAYLOAD)
        product = Product(
            name="Selected Product",
            protocol=protocol,
            vendor_id=sku.vendor_id if protocol == DeviceProtocol.matter else None,
            product_id=sku.product_id if protocol == DeviceProtocol.matter else None,
        )
        session.add(product)
        session.commit()
        product_id = product.id

    data = {"name": "Hall lamp", "product_record_id": product_id, "qr_payload": wrong_payload}
    response = client.post("/devices", data=data, follow_redirects=False)
    assert response.status_code == 409
    assert f"The selected Product uses {protocol.label}" in response.text
    assert f'<option value="{product_id}" selected>' in response.text
    assert f'value="{wrong_payload}"' in response.text
    with Session(_engine(client)) as session:
        assert session.exec(select(Device)).first() is None
        assert session.exec(select(Property)).first() is None
        assert session.exec(select(Product)).one().protocol == protocol

    data["qr_payload"] = valid_payload
    response = client.post("/devices", data=data, follow_redirects=False)
    assert response.status_code == 303
    with Session(_engine(client)) as session:
        device = session.exec(select(Device)).one()
        assert device.product_record_id == product_id
        assert session.exec(select(Product)).one().id == product_id
        properties = {p.type: p.value for p in device.properties}
        assert properties[PropertyType.qr_payload] == valid_payload
        assert properties[PropertyType.manual_code]


@pytest.mark.parametrize(
    ("protocol", "payload"),
    [(DeviceProtocol.matter, MATTER_PAYLOAD), (DeviceProtocol.homekit, HOMEKIT_PAYLOAD)],
)
def test_new_device_scan_resolves_one_product_without_placeholder(
    client: TestClient, protocol: DeviceProtocol, payload: str
):
    response = client.post(
        "/devices",
        data={"name": "Scanned lamp", "qr_payload": payload},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with Session(_engine(client)) as session:
        device = session.exec(select(Device)).one()
        assert response.headers["location"] == f"/devices/{device.id}"
        product = session.exec(select(Product)).one()
        assert device.product_record_id == product.id
        assert product.protocol == protocol
        assert {p.type: p.value for p in device.properties}[PropertyType.qr_payload] == payload


def test_new_device_scan_failure_rolls_back_flushed_device_and_scan(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    def fail_after_scan(session: Session, device: Device, payload: str):
        apply_scan_payload(session, device, payload)
        session.flush()
        raise ValueError("Fixture scan validation failed")

    monkeypatch.setattr("app.services.apply_scan_payload", fail_after_scan)
    response = client.post(
        "/devices",
        data={"name": "Failed lamp", "qr_payload": MATTER_PAYLOAD},
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert "Fixture scan validation failed" in response.text
    with Session(_engine(client)) as session:
        for model in (Device, Product, Property):
            assert session.exec(select(model)).first() is None


def test_refreshed_list_filters_views_and_quick_search(client: TestClient):
    with Session(_engine(client)) as session:
        matter = Product(name="Motion sensor", vendor="Aqara", protocol=DeviceProtocol.matter)
        homekit = Product(name="Door sensor", vendor="Eve", protocol=DeviceProtocol.homekit)
        session.add_all([matter, homekit])
        session.flush()
        session.add_all(
            [
                Device(name="Hall motion", product_record_id=matter.id, room="Hall"),
                Device(name="Kitchen door", product_record_id=homekit.id, room="Kitchen"),
            ]
        )
        session.commit()

    listing = client.get("/devices", params={"protocol": "matter", "view": "cards"})
    assert listing.status_code == 200
    assert 'data-view="cards"' in listing.text
    assert "1 device" in listing.text
    assert "Hall motion" in listing.text
    assert "Kitchen door" not in listing.text
    assert 'name="protocol"' in listing.text
    assert 'aria-current="page"' in listing.text

    search = client.get("/quick-search", params={"q": "Hall"})
    assert search.status_code == 200
    assert "Hall motion" in search.text
    assert "Kitchen door" not in search.text
    ingress = client.get(
        "/quick-search",
        params={"q": "Hall"},
        headers={"X-Ingress-Path": "/api/hassio_ingress/demo"},
    )
    assert 'href="/api/hassio_ingress/demo/devices/' in ingress.text


@pytest.mark.parametrize(
    "path",
    ["/devices", "/devices/new", "/products", "/network", "/settings", "/settings/integrations"],
)
def test_shared_header_has_refresh_and_hotkey_search_without_button(client: TestClient, path: str):
    page = client.get(path)
    assert page.status_code == 200
    header = page.text.split("</header>", 1)[0]
    assert header.count('id="sync-all-btn"') == 1
    assert "data-mr-search-open" not in header
    assert 'data-api-base="/api/integrations"' in header
    assert 'id="mr-search-dialog"' in page.text
    assert 'src="/static/quick_search.js"' in page.text


def test_device_results_actions_survive_filter_swap(client: TestClient):
    with Session(_engine(client)) as session:
        product = Product(name="Sensor", protocol=DeviceProtocol.matter)
        session.add(product)
        session.flush()
        device = Device(name="Hall sensor", product_record_id=product.id)
        session.add(device)
        session.flush()
        session.add(Property(device_id=device.id, type=PropertyType.qr_payload, value="MT:example"))
        session.commit()

    full = client.get("/devices")
    partial = client.get(
        "/devices",
        params={"q": "no match"},
        headers={"HX-Request": "true", "HX-Print-Available": "true"},
    )
    assert full.status_code == partial.status_code == 200
    assert 'id="sync-all-btn"' not in partial.text
    assert 'hx-headers=\'{"X-Print-Available": "true"}\'' in full.text
    detached = client.get("/devices", headers={"HX-Request": "true"})
    detached_print = re.search(
        r'<button[^>]*onclick="window.mrPrintQrSheet\(\)"[^>]*>', detached.text
    )
    assert detached_print is not None and "disabled" in detached_print.group()
    for page in (full, partial):
        results_head = page.text.split('class="mr-results-head"', 1)[1].split(
            "</div>\n  </div>", 1
        )[0]
        assert 'href="/devices/new"' in results_head
        assert 'class="mr-view-switch"' in results_head
        print_button = re.search(
            r'<button[^>]*onclick="window.mrPrintQrSheet\(\)"[^>]*>', results_head
        )
        assert print_button is not None
        assert "disabled" not in print_button.group()


def test_product_create_list_detail_and_links(client: TestClient):
    new = client.get("/products/new")
    assert new.status_code == 200
    assert re.search(r'<div id="mr-product-device-type-field"\s+hidden>', new.text)
    assert "On/Off Plug-in Unit (0x010A)" in new.text

    r = client.post(
        "/products",
        data={
            "name": "Aqara Hub",
            "protocol": "matter",
            "vendor": "Aqara",
            "model": "M2",
            "vendor_id": "0xFFF1",
            "product_id": "0x8001",
            "device_type_id": "266",
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
        assert product.device_type_id == 0x010A
        assert product.device_type_id_source is FieldSource.user
        assert len(product.links) == 2

    detail = client.get(f"/products/{product_id}")
    assert detail.status_code == 200
    assert "Aqara Hub" in detail.text
    assert "example.com/hub" in detail.text
    assert "On/Off Plug-in Unit (0x010A)" in detail.text

    listing = client.get("/products")
    assert "Aqara Hub" in listing.text

    filtered = client.get("/products", params={"protocol": "homekit"})
    assert "Aqara Hub" not in filtered.text


def test_product_editor_preserves_unknown_type_and_can_clear_it(client: TestClient):
    with Session(_engine(client)) as session:
        product = Product(
            name="Future Product",
            protocol=DeviceProtocol.matter,
            device_type_id=0xFFF10001,
            device_type_id_source=FieldSource.dcl,
        )
        session.add(product)
        session.commit()
        product_id = product.id

    path = f"/products/{product_id}"
    detail = client.get(path)
    assert "Unknown device type (0xFFF10001)" in detail.text
    assert 'title="CSA DCL">CSA DCL</span>' in detail.text
    edit = client.get(f"{path}/edit")
    assert f'<option value="{0xFFF10001}" selected>Unknown device type (0xFFF10001)' in edit.text

    fields = {"name": "Future Product", "protocol": "matter"}
    for data in [fields, {**fields, "device_type_id": str(0xFFF10001)}]:
        response = client.post(path, data=data, follow_redirects=False)
        assert response.status_code == 303
        record = client.get(f"/api{path}").json()
        assert record["device_type_id"] == 0xFFF10001
        assert record["sources"]["device_type_id"] == "dcl"

    for protocol, device_type_id in [("homekit", "266"), ("matter", "bad"), ("matter", "0")]:
        response = client.post(
            path,
            data={**fields, "protocol": protocol, "device_type_id": device_type_id},
            follow_redirects=False,
        )
        assert response.status_code == 422
    assert client.get(f"/api{path}").json()["device_type_id"] == 0xFFF10001

    response = client.post(path, data={**fields, "device_type_id": "none"}, follow_redirects=False)
    assert response.status_code == 303
    record = client.get(f"/api{path}").json()
    assert record["device_type_id"] is None
    assert record["sources"]["device_type_id"] == "user"
    assert "Unknown device type" not in client.get(path).text


def test_api_product_type_round_trip_and_protocol_change(client: TestClient):
    response = client.post(
        "/api/products",
        json={"name": "Future Product", "protocol": "matter", "device_type_id": 0xFFF10001},
    )
    assert response.status_code == 201
    record = response.json()
    assert record["device_type_id"] == 0xFFF10001
    assert record["sources"]["device_type_id"] == "user"
    path = f"/api/products/{record['id']}"
    assert client.get(path).json()["device_type_id"] == 0xFFF10001

    response = client.patch(path, json={"device_type_id": None})
    assert response.status_code == 200
    assert response.json()["device_type_id"] is None
    response = client.patch(path, json={"device_type_id": 0x010A})
    assert response.status_code == 200
    assert response.json()["device_type_id"] == 0x010A

    response = client.patch(path, json={"protocol": "homekit", "device_type_id": 0x010A})
    assert response.status_code == 422
    assert client.get(path).json()["device_type_id"] == 0x010A
    response = client.patch(path, json={"protocol": "homekit"})
    assert response.status_code == 200
    assert response.json()["device_type_id"] is None
    assert response.json()["protocol"] == "homekit"


@pytest.mark.parametrize(
    ("protocol", "type_id"),
    [
        (None, 266),
        ("homekit", 266),
        ("matter", 0),
        ("matter", -1),
        ("matter", 0x100000000),
        ("matter", True),
        ("matter", "266"),
        ("matter", 266.0),
    ],
)
def test_api_rejects_invalid_product_type(client: TestClient, protocol, type_id):
    response = client.post(
        "/api/products", json={"name": "Product", "protocol": protocol, "device_type_id": type_id}
    )
    assert response.status_code == 422
    assert client.get("/api/products").json() == []


def test_product_card_has_separate_product_and_device_links(client: TestClient):
    with Session(_engine(client)) as session:
        product = Product(name="Hall sensor", vendor="Eve", protocol=DeviceProtocol.homekit)
        session.add(product)
        session.flush()
        session.add(Device(name="Hall unit", product_record_id=product.id))
        session.commit()
        product_id = product.id

    response = client.get("/products", params={"q": "Hall", "view": "cards"})
    assert response.status_code == 200
    assert 'data-view="cards"' in response.text
    assert "view=table" in response.text
    card = re.search(r'<article class="mr-product-card">(.*?)</article>', response.text, re.S)
    assert card is not None

    class AnchorParser(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.depth = 0
            self.max_depth = 0
            self.hrefs: list[str] = []

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            if tag == "a":
                self.depth += 1
                self.max_depth = max(self.max_depth, self.depth)
                self.hrefs.extend(value for key, value in attrs if key == "href" and value)

        def handle_endtag(self, tag: str) -> None:
            if tag == "a":
                self.depth -= 1

    parser = AnchorParser()
    parser.feed(card.group(1))
    assert parser.max_depth == 1
    assert f"/products/{product_id}" in parser.hrefs
    assert any(href.startswith("/devices?") for href in parser.hrefs)


def test_settings_integrations_keep_all_cards_and_sync_actions(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("SUPERVISOR_TOKEN", "")
    page = client.get("/settings/integrations")
    assert page.status_code == 200
    titles = [
        "Home Assistant Core",
        "HA Matter Server",
        "OpenThread Border Router",
        "mDNS Discovery",
    ]
    assert [page.text.index(title) for title in titles] == sorted(
        page.text.index(title) for title in titles
    )
    for button_id in ("ha-sync-btn", "import-btn", "otbr-poll-btn", "mdns-sync-btn"):
        button = re.search(rf'<button[^>]*id="{button_id}"[^>]*>', page.text, re.S)
        assert button is not None and re.search(r"\sdisabled(?:\s|>)", button.group())

    status = SimpleNamespace(value="connected")
    common = dict(
        status=status,
        error_message=None,
        _last_sync=None,
        _last_synced_at=None,
        can_create_devices=True,
        can_update_devices=True,
        can_update_status=False,
        can_act_externally=False,
        supported_protocols={DeviceProtocol.matter},
    )
    client.app.state.ha_client = SimpleNamespace(
        _url="http://ha.test", _token="secret1234", **common
    )  # type: ignore[union-attr]
    client.app.state.matter_client = SimpleNamespace(_url="ws://matter.test", **common)  # type: ignore[union-attr]
    client.app.state.otbr_client = SimpleNamespace(_base_url="http://otbr.test", **common)  # type: ignore[union-attr]
    client.app.state.mdns_client = SimpleNamespace(discovered=lambda: [], **common)  # type: ignore[union-attr]

    configured = client.get("/settings/integrations")
    assert configured.status_code == 200
    for source in ("http://ha.test", "ws://matter.test", "http://otbr.test"):
        assert source in configured.text
    assert "…1234" in configured.text
    assert "secret1234" not in configured.text
    for button_id in ("ha-sync-btn", "import-btn", "otbr-poll-btn", "mdns-sync-btn"):
        button = re.search(rf'<button[^>]*id="{button_id}"[^>]*>', configured.text, re.S)
        assert button is not None and not re.search(r"\sdisabled(?:\s|>)", button.group())


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
            vendor_id=0xFFF1,
            product_id=0x8001,
            device_type_id=0xFFF10001,
            device_type_id_source=FieldSource.dcl,
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
            device_type_id_source=FieldSource.user,
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
    assert "Unknown device type (0xFFF10001)" in preview.text

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
        assert target.vid_pid_display == "0xFFF1 / 0x8001"
        assert target.device_type_id == 0xFFF10001
        assert target.device_type_id_source is FieldSource.dcl

        device = session.get(Device, device_id)
        assert device is not None
        assert device.product_record_id == target_id

        links = session.exec(
            select(ProductLink).where(ProductLink.product_record_id == target_id)
        ).all()
        assert len(links) == 1


def test_product_cannot_merge_into_itself(client: TestClient):
    product = client.post("/api/products", json={"name": "Keep me", "protocol": "matter"})
    product_id = product.json()["id"]
    response = client.post(f"/products/{product_id}/merge", data={"target_id": product_id})
    assert response.status_code == 409
    assert client.get(f"/api/products/{product_id}").status_code == 200


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


# ── VID/PID search (Product list) ────────────────────────────────────────────


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("0xFFF1", {0xFFF1}),
        ("0xfff1", {0xFFF1}),
        ("0x1", {1}),
        ("fff1", {0xFFF1}),
        ("65521", {65521}),
        ("4488", {4488, 0x4488}),
        ("8001", {8001, 0x8001}),
        ("2", {2}),
        ("0x10000", set()),
        ("70000", set()),
        ("eve", set()),
        ("0xZZ", set()),
        ("", set()),
    ],
)
def test_vid_pid_candidates(token, expected):
    from app.services import vid_pid_candidates

    assert vid_pid_candidates(token) == expected


@pytest.fixture
def search_products(client: TestClient) -> None:
    with Session(_engine(client)) as session:
        session.add_all(
            [
                Product(
                    name="Smart Plug",
                    vendor="SONOFF",
                    protocol=DeviceProtocol.matter,
                    vendor_id=0x1321,
                    product_id=0x0002,
                ),
                Product(
                    name="Test Bulb",
                    vendor="Acme",
                    protocol=DeviceProtocol.matter,
                    vendor_id=0xFFF1,
                    product_id=0x8001,
                ),
                Product(
                    name="Test Switch",
                    vendor="Acme",
                    protocol=DeviceProtocol.matter,
                    vendor_id=0xFFF1,
                    product_id=0x8002,
                ),
                Product(name="HomeKit Lamp", vendor="Eve", protocol=DeviceProtocol.homekit),
            ]
        )
        session.commit()


def _listed(client: TestClient, q: str) -> list[str]:
    response = client.get("/products", params={"q": q, "view": "table"})
    assert response.status_code == 200
    return [
        n for n in ("Smart Plug", "Test Bulb", "Test Switch", "HomeKit Lamp") if n in response.text
    ]


@pytest.mark.usefixtures("search_products")
def test_search_matches_vid_in_hex_forms(client: TestClient):
    assert _listed(client, "0xFFF1") == ["Test Bulb", "Test Switch"]
    assert _listed(client, "fff1") == ["Test Bulb", "Test Switch"]
    assert _listed(client, "0xfff1") == ["Test Bulb", "Test Switch"]


@pytest.mark.usefixtures("search_products")
def test_search_matches_vid_in_decimal(client: TestClient):
    assert _listed(client, "65521") == ["Test Bulb", "Test Switch"]


@pytest.mark.usefixtures("search_products")
def test_search_matches_pid(client: TestClient):
    assert _listed(client, "0x8002") == ["Test Switch"]
    assert _listed(client, "8002") == ["Test Switch"]


@pytest.mark.usefixtures("search_products")
def test_search_vid_pid_pair_narrows_to_that_product(client: TestClient):
    assert _listed(client, "0xFFF1 / 0x8001") == ["Test Bulb"]
    assert _listed(client, "0xfff1/0x8001") == ["Test Bulb"]
    assert _listed(client, "fff1 8002") == ["Test Switch"]
    # A pair is exact: a VID with a PID it does not have matches nothing.
    assert _listed(client, "0x1321 0x8001") == []


@pytest.mark.usefixtures("search_products")
def test_search_text_still_works_and_combines_with_filters(client: TestClient):
    assert _listed(client, "bulb") == ["Test Bulb"]
    assert _listed(client, "acme") == ["Test Bulb", "Test Switch"]
    response = client.get("/products", params={"q": "0xFFF1", "protocol": "homekit"})
    assert "Test Bulb" not in response.text


@pytest.mark.usefixtures("search_products")
def test_search_ignores_numbers_out_of_range(client: TestClient):
    assert _listed(client, "0x10000") == []
    assert _listed(client, "99999999") == []
