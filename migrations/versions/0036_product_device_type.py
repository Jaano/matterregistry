"""Add the Matter device type and its provenance to Products.

Revision ID: 0036
Revises: 0035
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0036"
down_revision: str | None = "0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINT = "ck_product_matter_device_type"


def upgrade() -> None:
    op.add_column(
        "product",
        sa.Column(
            "device_type_id",
            sa.Integer(),
            sa.CheckConstraint(
                "device_type_id IS NULL OR (protocol IS NOT NULL AND protocol = 'matter' "
                "AND typeof(device_type_id) = 'integer' "
                "AND device_type_id BETWEEN 1 AND 4294967295)",
                name=_CONSTRAINT,
            ),
        ),
    )
    op.add_column(
        "product",
        sa.Column("device_type_id_source", sa.String(), nullable=False, server_default="generated"),
    )


def downgrade() -> None:
    # SQLite drops the CHECK declared on the column along with the column.
    op.drop_column("product", "device_type_id_source")
    op.drop_column("product", "device_type_id")
