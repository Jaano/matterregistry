"""add Product catalog and move generic identity from Device

Revision ID: 0029
Revises: 0028
Create Date: 2026-07-20

"""

import uuid
from collections import defaultdict
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_GENERIC_FIELDS = ("vendor", "product", "device_model", "vendor_id", "product_id")
_PRIORITY = {
    "generated": 5,
    "mdns": 40,
    "otbr": 50,
    "matter": 100,
    "ha": 150,
    "scanned": 200,
    "user": 255,
}


def _normal(value: object) -> str:
    return " ".join(str(value or "").casefold().split())


def _best(rows: list[dict], field: str) -> tuple[object, str]:
    candidates = [row for row in rows if row.get(field) is not None]
    if not candidates:
        return None, "generated"
    winner = max(
        candidates,
        key=lambda row: (
            _PRIORITY.get(str(row.get(f"{field}_source") or "generated"), 0),
            str(row.get("updated_at") or ""),
        ),
    )
    return winner[field], str(winner.get(f"{field}_source") or "generated")


def _product_key(row: dict) -> tuple:
    protocol = row.get("protocol")
    if (
        protocol == "matter"
        and row.get("vendor_id") is not None
        and row.get("product_id") is not None
    ):
        return ("matter-id", protocol, row["vendor_id"], row["product_id"])
    descriptive = tuple(_normal(row.get(field)) for field in ("vendor", "product", "device_model"))
    if any(descriptive):
        return ("descriptive", protocol, *descriptive)
    # Never auto-share an unidentifiable product.
    return ("dedicated", row["id"])


def upgrade() -> None:
    op.create_table(
        "product",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("protocol", sa.String(length=16)),
        sa.Column("vendor", sa.String()),
        sa.Column("model", sa.String()),
        sa.Column("vendor_id", sa.Integer()),
        sa.Column("product_id", sa.Integer()),
        sa.Column("description", sa.String()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("name_source", sa.String(), nullable=False, server_default="generated"),
        sa.Column("vendor_source", sa.String(), nullable=False, server_default="generated"),
        sa.Column("model_source", sa.String(), nullable=False, server_default="generated"),
        sa.Column("vendor_id_source", sa.String(), nullable=False, server_default="generated"),
        sa.Column("product_id_source", sa.String(), nullable=False, server_default="generated"),
        sa.Column("description_source", sa.String(), nullable=False, server_default="generated"),
    )
    op.create_index(
        "uq_product_matter_vid_pid",
        "product",
        ["protocol", "vendor_id", "product_id"],
        unique=True,
        sqlite_where=sa.text(
            "protocol = 'matter' AND vendor_id IS NOT NULL AND product_id IS NOT NULL"
        ),
    )
    op.create_table(
        "product_link",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("product_record_id", sa.String(), sa.ForeignKey("product.id"), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("url", sa.String(), nullable=False),
        sa.Column("label", sa.String()),
        sa.Column("alt_text", sa.String()),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index("ix_product_link_product_record_id", "product_link", ["product_record_id"])
    op.add_column("device", sa.Column("product_record_id", sa.String(), nullable=True))

    bind = op.get_bind()
    rows = [dict(row._mapping) for row in bind.execute(sa.text("SELECT * FROM device"))]
    grouped: dict[tuple, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[_product_key(row)].append(row)

    device_products: dict[str, str] = {}
    product_table = sa.table(
        "product",
        sa.column("id"),
        sa.column("name"),
        sa.column("protocol"),
        sa.column("vendor"),
        sa.column("model"),
        sa.column("vendor_id"),
        sa.column("product_id"),
        sa.column("created_at"),
        sa.column("updated_at"),
        sa.column("name_source"),
        sa.column("vendor_source"),
        sa.column("model_source"),
        sa.column("vendor_id_source"),
        sa.column("product_id_source"),
        sa.column("description_source"),
    )
    for group in grouped.values():
        product_id = str(uuid.uuid4())
        name, name_source = _best(group, "product")
        vendor, vendor_source = _best(group, "vendor")
        model, model_source = _best(group, "device_model")
        vendor_id, vendor_id_source = _best(group, "vendor_id")
        numeric_product_id, numeric_product_id_source = _best(group, "product_id")
        protocol = group[0].get("protocol")
        created_at = min(
            (row.get("created_at") for row in group if row.get("created_at")), default=None
        )
        updated_at = max(
            (row.get("updated_at") for row in group if row.get("updated_at")), default=None
        )
        bind.execute(
            product_table.insert().values(
                id=product_id,
                name=name or f"Unresolved product for {group[0]['name']}",
                protocol=protocol,
                vendor=vendor,
                model=model,
                vendor_id=vendor_id,
                product_id=numeric_product_id,
                created_at=created_at,
                updated_at=updated_at,
                name_source=name_source,
                vendor_source=vendor_source,
                model_source=model_source,
                vendor_id_source=vendor_id_source,
                product_id_source=numeric_product_id_source,
                description_source="generated",
            )
        )
        for row in group:
            device_products[row["id"]] = product_id
    for device_id, product_id in device_products.items():
        bind.execute(
            sa.text("UPDATE device SET product_record_id = :product_id WHERE id = :device_id"),
            {"product_id": product_id, "device_id": device_id},
        )

    with op.batch_alter_table("device") as batch:
        batch.alter_column("product_record_id", nullable=False)
        batch.create_foreign_key(
            "fk_device_product_record", "product", ["product_record_id"], ["id"]
        )
        for field in _GENERIC_FIELDS:
            batch.drop_column(field)
            batch.drop_column(f"{field}_source")
        batch.drop_column("protocol")
    op.create_index("ix_device_product_record_id", "device", ["product_record_id"])


def downgrade() -> None:
    op.drop_index("ix_device_product_record_id", table_name="device")
    with op.batch_alter_table("device") as batch:
        batch.drop_constraint("fk_device_product_record", type_="foreignkey")
        batch.add_column(sa.Column("protocol", sa.String(length=16)))
        batch.add_column(sa.Column("vendor", sa.String()))
        batch.add_column(sa.Column("product", sa.String()))
        batch.add_column(sa.Column("device_model", sa.String()))
        batch.add_column(sa.Column("vendor_id", sa.Integer()))
        batch.add_column(sa.Column("product_id", sa.Integer()))
        for field in _GENERIC_FIELDS:
            batch.add_column(
                sa.Column(
                    f"{field}_source", sa.String(), nullable=False, server_default="generated"
                )
            )
        batch.drop_column("product_record_id")

    bind = op.get_bind()
    bind.execute(
        sa.text(
            "UPDATE device SET "
            "protocol = product.protocol, vendor = product.vendor, product = product.name, "
            "device_model = product.model, vendor_id = product.vendor_id, product_id = product.product_id, "
            "vendor_source = product.vendor_source, product_source = product.name_source, "
            "device_model_source = product.model_source, vendor_id_source = product.vendor_id_source, "
            "product_id_source = product.product_id_source "
            "FROM product WHERE device.product_record_id = product.id"
        )
    )
    op.drop_index("ix_product_link_product_record_id", table_name="product_link")
    op.drop_table("product_link")
    op.drop_index("uq_product_matter_vid_pid", table_name="product")
    op.drop_table("product")
