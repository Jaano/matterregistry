from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, HttpUrl, model_validator

from ..models import (
    DeviceProtocol,
    DeviceStatus,
    FieldSource,
    ProductLinkKind,
    PropertyType,
)


class PropertyOut(BaseModel):
    id: str
    device_id: str
    type: PropertyType
    value: str
    label: str | None
    source: FieldSource
    captured_at: datetime

    model_config = {"from_attributes": True}


class PropertyCreate(BaseModel):
    type: PropertyType
    value: str
    label: str | None = None


class PropertyUpdate(BaseModel):
    type: PropertyType | None = None
    value: str | None = None
    label: str | None = None


class DeviceOut(BaseModel):
    id: str
    name: str
    room: str | None
    vendor: str | None
    product: str | None
    device_model: str | None
    vendor_id: int | None
    product_id: int | None
    serial: str | None
    hardware_version: str | None
    firmware_version: str | None
    notes: str | None
    purchase_date: date | None
    warranty_until: date | None
    status: DeviceStatus
    protocol: DeviceProtocol | None
    product_record_id: str
    created_at: datetime
    updated_at: datetime
    ha_device_id: str | None = None
    matter_unique_id: str | None = None
    network_type: list[str] = []
    properties: list[PropertyOut] = []
    # Flat provenance map: field_name → FieldSource value string
    sources: dict[str, str] = {}

    model_config = {"from_attributes": True}

    @model_validator(mode="after")
    def _populate_sources(self) -> "DeviceOut":
        if not self.sources:
            # Populated by _device_out() in api/devices.py; this fallback
            # handles any path that uses model_validate() directly.
            pass
        return self


class DeviceCreate(BaseModel):
    name: str
    product_record_id: str | None = None
    room: str | None = None
    serial: str | None = None
    hardware_version: str | None = None
    firmware_version: str | None = None
    notes: str | None = None
    purchase_date: date | None = None
    warranty_until: date | None = None
    status: DeviceStatus = DeviceStatus.active

    model_config = ConfigDict(extra="forbid")


class DeviceUpdate(BaseModel):
    name: str | None = None
    product_record_id: str | None = None
    room: str | None = None
    serial: str | None = None
    hardware_version: str | None = None
    firmware_version: str | None = None
    notes: str | None = None
    purchase_date: date | None = None
    warranty_until: date | None = None
    status: DeviceStatus | None = None

    model_config = ConfigDict(extra="forbid")


class ProductLinkCreate(BaseModel):
    kind: ProductLinkKind
    url: HttpUrl
    label: str | None = None
    alt_text: str | None = None
    position: int = 0


class ProductLinkUpdate(BaseModel):
    kind: ProductLinkKind | None = None
    url: HttpUrl | None = None
    label: str | None = None
    alt_text: str | None = None
    position: int | None = None


class ProductLinkOut(BaseModel):
    id: str
    product_record_id: str
    kind: ProductLinkKind
    url: str
    label: str | None
    alt_text: str | None
    position: int

    model_config = {"from_attributes": True}


class ProductCreate(BaseModel):
    name: str
    protocol: DeviceProtocol | None = None
    vendor: str | None = None
    model: str | None = None
    vendor_id: int | None = None
    product_id: int | None = None
    description: str | None = None

    model_config = ConfigDict(extra="forbid")


class ProductUpdate(BaseModel):
    name: str | None = None
    protocol: DeviceProtocol | None = None
    vendor: str | None = None
    model: str | None = None
    vendor_id: int | None = None
    product_id: int | None = None
    description: str | None = None

    model_config = ConfigDict(extra="forbid")


class ProductOut(BaseModel):
    id: str
    name: str
    protocol: DeviceProtocol | None
    vendor: str | None
    model: str | None
    vendor_id: int | None
    product_id: int | None
    description: str | None
    created_at: datetime
    updated_at: datetime
    sources: dict[str, str] = {}
    links: list[ProductLinkOut] = []

    model_config = {"from_attributes": True}
