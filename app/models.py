import uuid
from datetime import date, datetime
from enum import StrEnum

from sqlalchemy import JSON as SA_JSON
from sqlalchemy import Column, LargeBinary, UniqueConstraint
from sqlmodel import Field, Relationship, SQLModel


class DeviceProtocol(StrEnum):
    matter = "matter"
    homekit = "homekit"

    @property
    def label(self) -> str:
        """Human display name; falls back to the title-cased value."""
        return {"matter": "Matter", "homekit": "HomeKit"}.get(self.value, self.value.title())

    @property
    def icon(self) -> str:
        """CSS icon class for this protocol (see icons.css); "" when none."""
        return {
            "matter": "icon-protocol-matter",
            "homekit": "icon-protocol-homekit",
        }.get(self.value, "")


class NetworkType(StrEnum):
    wifi = "wifi"
    thread = "thread"
    ethernet = "ethernet"
    bluetooth = "bluetooth"  # operational BLE only - not commissioning channel


class DeviceStatus(StrEnum):
    active = "active"
    retired = "retired"
    spare = "spare"
    hidden = "hidden"


class FieldSource(StrEnum):
    """Priority-ranked provenance tag for user-editable Device fields.

    Higher numeric priority wins; equal-or-higher may overwrite the current
    value; strictly lower is ignored (see services.set_field).
    """

    user = "user"  # 255 - web form / REST API write
    scanned = "scanned"  # 200 - decoded from Matter QR / manual code
    ha = "ha"  # 150 - synced from HA Core registry
    matter = "matter"  # 100 - synced from Matter Server
    otbr = "otbr"  #  50 - synced from OpenThread Border Router
    mdns = "mdns"  #  40 - discovered via mDNS / HAP advertisement
    generated = "generated"  #  5 - app default / placeholder

    @classmethod
    def priority(cls, source: "FieldSource | str") -> int:
        _P = {
            "user": 255,
            "scanned": 200,
            "ha": 150,
            "matter": 100,
            "otbr": 50,
            "mdns": 40,
            "generated": 5,
        }
        v = source.value if isinstance(source, cls) else str(source)
        return _P.get(v, 0)

    @property
    def label(self) -> str:
        """Short human-readable label for the UI."""
        return {
            "user": "you",
            "scanned": "scanned",
            "matter": "Matter",
            "ha": "HomeAssistant",
            "otbr": "OTBR",
            "mdns": "mDNS",
        }.get(self.value, "")

    @property
    def icon(self) -> str:
        """CSS icon class for this source (see icons.css); '' when none."""
        return {
            "user": "icon-hand",
            "scanned": "icon-qr-code",
            "matter": "icon-protocol-matter",
            "ha": "icon-home-assistant",
            "otbr": "icon-thread",
            "mdns": "icon-mdns",
        }.get(self.value, "")


# Fields on Device that carry a sibling *_source column.
SOURCED_FIELDS: tuple[str, ...] = (
    "name",
    "room",
    "vendor",
    "product",
    "device_model",
    "vendor_id",
    "product_id",
    "serial",
    "hardware_version",
    "firmware_version",
    "matter_unique_id",
    "notes",
    "purchase_date",
    "warranty_until",
    "commissioned_at",
    "status",
    "network_type",
    "mac_address",
)


class PropertyType(StrEnum):
    setup_pin = "setup_pin"
    manual_code = "manual_code"
    qr_payload = "qr_payload"
    discriminator = "discriminator"
    fabric_id = "fabric_id"
    node_id = "node_id"
    root_cert = "root_cert"
    # HAP pairing keys - stored plaintext, same as setup_pin
    hap_accessory_ltpk = "hap_accessory_ltpk"
    hap_ios_pairing_id = "hap_ios_pairing_id"
    hap_ios_device_ltsk = "hap_ios_device_ltsk"
    hap_ios_device_ltpk = "hap_ios_device_ltpk"
    other = "other"


class DeviceLinkSource(StrEnum):
    manual = "manual"  # user set the link explicitly via the picker
    auto = "auto"  # set by auto-correlation during HA sync


class AttachmentKind(StrEnum):
    image = "image"
    pdf = "pdf"
    other = "other"


def _now() -> datetime:
    return datetime.utcnow()


class Device(SQLModel, table=True):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
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
    matter_unique_id: str | None = Field(default=None, index=True)
    # HomeKit accessory identity (HAP Device ID, MAC-format) - the analog of
    # matter_unique_id. Shared dedupe key between mDNS discovery and the HA
    # HomeKit import. Not user-editable; set by whichever integration
    # sees the accessory first.
    homekit_accessory_id: str | None = Field(default=None, index=True)
    notes: str | None = None
    purchase_date: date | None = None
    warranty_until: date | None = None
    commissioned_at: datetime | None = None  # first commissioning timestamp
    status: DeviceStatus = DeviceStatus.active
    protocol: DeviceProtocol | None = None
    # Networking - populated by Matter Server sync
    network_type: list[str] = Field(
        default_factory=list,
        sa_column=Column(SA_JSON),
    )  # set of NetworkType values; empty = unknown
    mac_address: str | None = None  # colon-hex hardware address
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)

    # Provenance flags - one per user-editable field (see FieldSource).
    name_source: FieldSource = Field(default=FieldSource.generated)
    room_source: FieldSource = Field(default=FieldSource.generated)
    vendor_source: FieldSource = Field(default=FieldSource.generated)
    product_source: FieldSource = Field(default=FieldSource.generated)
    device_model_source: FieldSource = Field(default=FieldSource.generated)
    vendor_id_source: FieldSource = Field(default=FieldSource.generated)
    product_id_source: FieldSource = Field(default=FieldSource.generated)
    serial_source: FieldSource = Field(default=FieldSource.generated)
    hardware_version_source: FieldSource = Field(default=FieldSource.generated)
    firmware_version_source: FieldSource = Field(default=FieldSource.generated)
    matter_unique_id_source: FieldSource = Field(default=FieldSource.generated)
    notes_source: FieldSource = Field(default=FieldSource.generated)
    purchase_date_source: FieldSource = Field(default=FieldSource.generated)
    warranty_until_source: FieldSource = Field(default=FieldSource.generated)
    commissioned_at_source: FieldSource = Field(default=FieldSource.generated)
    status_source: FieldSource = Field(default=FieldSource.generated)
    network_type_source: FieldSource = Field(default=FieldSource.generated)
    mac_address_source: FieldSource = Field(default=FieldSource.generated)

    # ── Display helpers ──────────────────────────────────────────────────────
    # Formatting lives here, not in the template, which just renders the string.

    @property
    def vid_pid_display(self) -> str | None:
        """VID (and PID, when present) as ``0xFFF1 / 0x8001``; None when no VID."""
        if self.vendor_id is None:
            return None
        text = f"0x{self.vendor_id:04X}"
        if self.product_id:
            text += f" / 0x{self.product_id:04X}"
        return text

    properties: list["Property"] = Relationship(
        back_populates="device",
        sa_relationship_kwargs={"cascade": "all, delete-orphan", "lazy": "select"},
    )
    attachments: list["Attachment"] = Relationship(
        back_populates="device",
        sa_relationship_kwargs={"cascade": "all, delete-orphan", "lazy": "select"},
    )


class Property(SQLModel, table=True):
    __tablename__ = "property"  # type: ignore[assignment]
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    device_id: str = Field(foreign_key="device.id")
    type: PropertyType
    value: str
    label: str | None = None
    source: FieldSource = FieldSource.user
    captured_at: datetime = Field(default_factory=_now)

    device: Device | None = Relationship(back_populates="properties")


class Attachment(SQLModel, table=True):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    device_id: str = Field(foreign_key="device.id")
    kind: AttachmentKind
    filename: str
    mime_type: str
    sha256: str
    size_bytes: int
    content: bytes = Field(sa_type=LargeBinary)
    uploaded_at: datetime = Field(default_factory=_now)

    device: Device | None = Relationship(back_populates="attachments")


class Fabric(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("fabric_id", name="uq_fabric_fabric_id"),)

    id: int | None = Field(default=None, primary_key=True)
    fabric_label: str | None = None
    fabric_id: str  # 64-bit Matter fabric ID as hex
    controller: str  # e.g. "HA Matter" or resolved vendor name
    vendor_id: int | None = None  # Matter VID of the fabric controller
    vendor_name: str | None = None  # resolved vendor name from CSA registry
    root_ca_fingerprint: str | None = None
    notes: str | None = None

    memberships: list["DeviceFabricMembership"] = Relationship(
        back_populates="fabric",
        sa_relationship_kwargs={"cascade": "all, delete-orphan", "lazy": "select"},
    )


class DeviceFabricMembership(SQLModel, table=True):
    __tablename__ = "device_fabric_membership"  # type: ignore
    __table_args__ = (UniqueConstraint("fabric_id", "node_id", name="uq_dfm_fabric_node"),)

    id: int | None = Field(default=None, primary_key=True)
    device_id: str = Field(foreign_key="device.id")
    fabric_id: int = Field(foreign_key="fabric.id")
    node_id: int
    endpoint_json: str = Field(default="{}")  # JSON: {endpoint_id: [cluster_ids]}

    fabric: Fabric | None = Relationship(back_populates="memberships")


class DeviceLink(SQLModel, table=True):
    """Generic device ↔ external-system link.

    One row per (device, integration) - unique constraint enforces this.
    For HA Core: integration="ha_core", external_id=ha_device_id.
    """

    __tablename__ = "device_link"  # type: ignore[assignment]
    __table_args__ = (
        UniqueConstraint("device_id", "integration", name="uq_device_link_device_integration"),
    )

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    device_id: str = Field(foreign_key="device.id", index=True)
    integration: str  # slug, e.g. "ha_core"
    external_id: str  # the link key, e.g. HA device UUID
    link_source: DeviceLinkSource = DeviceLinkSource.auto
    linked_at: datetime = Field(default_factory=_now)


class ThreadNetwork(SQLModel, table=True):
    __tablename__ = "thread_network"  # type: ignore
    __table_args__ = (UniqueConstraint("ext_pan_id", name="uq_thread_ext_pan_id"),)

    id: int | None = Field(default=None, primary_key=True)
    name: str
    network_name: str
    ext_pan_id: str = Field(index=True)  # 16 hex chars; unique identifier
    pan_id: str  # 4 hex chars e.g. "9C31"
    channel: int
    mesh_local_prefix: str  # IPv6 CIDR e.g. "fd46:5a23:f008:e644::/64"
    network_key: str  # PLAINTEXT per §9
    pskc: str | None = None
    active_timestamp: int | None = None
    active_dataset_hex: str | None = None  # full active operational dataset as hex TLVs
    border_router_url: str
    border_agent_id: str | None = None
    ncp_version: str | None = None
    last_polled: datetime | None = None
    notes: str | None = None


# ── Integration staging tables ────────────────────────────────────────────────


class HADeviceRecord(SQLModel, table=True):
    """Phase-1 staging table for HA Core device registry entries.

    Populated by HACoreClient.ingest(); read by HACoreClient.project().
    Excluded from backups (§4a).
    """

    __tablename__ = "ha_device_record"  # type: ignore[assignment]

    ha_device_id: str = Field(primary_key=True)
    name: str = Field(default="")
    manufacturer: str = Field(default="")
    model: str = Field(default="")
    area_name: str = Field(default="")
    area_id: str = Field(default="")
    identifiers_json: str = Field(default="[]")  # JSON: raw HA identifiers list
    connections_json: str = Field(default="[]")  # JSON: [["transport","mac"], ...]
    matter_uid_set_json: str = Field(default="[]")  # JSON: sorted list of matter UID strings
    fabric_id: str | None = Field(default=None)
    node_id: int | None = Field(default=None)
    serial: str | None = Field(default=None)
    matter_unique_id: str | None = Field(default=None)
    protocol: str | None = Field(default=None)  # "matter" | "homekit"
    sw_version: str | None = Field(default=None)
    hw_version: str | None = Field(default=None)

    def to_ha_dict(self) -> dict:
        """Reconstruct the ha_dev dict consumed by ``_sync_devices`` and the link picker."""
        import json as _json

        return {
            "id": self.ha_device_id,
            "name": self.name,
            "manufacturer": self.manufacturer,
            "model": self.model,
            "area_name": self.area_name,
            "area_id": self.area_id,
            "identifiers": _json.loads(self.identifiers_json),
            "connections": _json.loads(self.connections_json),
            "matter_uid_set": frozenset(_json.loads(self.matter_uid_set_json)),
            "fabric_id": self.fabric_id,
            "node_id": self.node_id,
            "serial": self.serial,
            "matter_unique_id": self.matter_unique_id,
            "protocol": self.protocol,
            "sw_version": self.sw_version,
            "hw_version": self.hw_version,
        }


class MatterNodeRecord(SQLModel, table=True):
    """Phase-1 staging table for Matter Server node snapshots.

    Populated by MatterServerClient.ingest(); read by MatterServerClient.project().
    Excluded from backups (§4a).
    """

    __tablename__ = "matter_node_record"  # type: ignore[assignment]

    node_id: int = Field(primary_key=True)
    available: bool = Field(default=False)
    vendor_id: int | None = Field(default=None)
    vendor_name: str | None = Field(default=None)
    product_id: int | None = Field(default=None)
    product_name: str | None = Field(default=None)
    serial: str | None = Field(default=None)
    hardware_version_string: str | None = Field(default=None)
    firmware_version_string: str | None = Field(default=None)
    node_label: str | None = Field(default=None)
    unique_id: str | None = Field(default=None)
    manufacturing_date: str | None = Field(default=None)
    product_url: str | None = Field(default=None)
    part_number: str | None = Field(default=None)
    network_type_json: str = Field(default="[]")  # JSON list of transport strings
    mac_address: str | None = Field(default=None)
    ip_addresses_json: str = Field(default="[]")  # JSON list of IPv6 address strings
    endpoint_json: str = Field(default="{}")  # JSON: {endpoint_id: [cluster_ids]}
    last_synced: datetime = Field(default_factory=_now)
    # commissioning date + bridge flag
    date_commissioned: str | None = Field(default=None)  # ISO datetime string
    is_bridge: bool = Field(default=False)
    # extra BasicInformation attrs
    product_label: str | None = Field(default=None)
    product_appearance_json: str | None = Field(default=None)  # JSON of ProductAppearance struct
    spec_version_int: int | None = Field(default=None)
    hardware_version_int: int | None = Field(default=None)
    software_version_int: int | None = Field(default=None)


# ── Per-device integration data store ─────────────────────────────────────────


class DeviceIntegrationData(SQLModel, table=True):
    """Generic per-(device, integration) retrieved-value cache.

    Each integration writes its own row in project()/ingest() - a durable
    snapshot of "here is exactly what this integration last read about this
    device."  The tile on the device-detail page lists all stored payloads.

    Integration-owned cache → **excluded from backups** (§4a); rebuilt on
    next sync.
    """

    __tablename__ = "device_integration_data"  # type: ignore[assignment]
    __table_args__ = (
        UniqueConstraint("device_id", "integration", name="uq_did_device_integration"),
    )

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    device_id: str = Field(foreign_key="device.id", index=True)
    integration: str  # slug, e.g. "ha_core", "matter_server", "otbr", "mdns"
    payload_json: str = Field(default="{}")  # JSON blob of retrieved key/values
    retrieved_at: datetime = Field(default_factory=_now)
