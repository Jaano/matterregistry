from datetime import date, datetime

from pydantic import BaseModel, model_validator

from ..models import DeviceProtocol, DeviceStatus, FieldSource, PropertyType


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
    protocol: DeviceProtocol
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
    room: str | None = None
    vendor: str | None = None
    product: str | None = None
    device_model: str | None = None
    vendor_id: int | None = None
    product_id: int | None = None
    serial: str | None = None
    hardware_version: str | None = None
    firmware_version: str | None = None
    notes: str | None = None
    purchase_date: date | None = None
    warranty_until: date | None = None
    status: DeviceStatus = DeviceStatus.active


class DeviceUpdate(BaseModel):
    name: str | None = None
    room: str | None = None
    vendor: str | None = None
    product: str | None = None
    device_model: str | None = None
    vendor_id: int | None = None
    product_id: int | None = None
    serial: str | None = None
    hardware_version: str | None = None
    firmware_version: str | None = None
    notes: str | None = None
    purchase_date: date | None = None
    warranty_until: date | None = None
    status: DeviceStatus | None = None
