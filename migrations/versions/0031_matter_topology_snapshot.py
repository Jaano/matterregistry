"""add matter_topology_snapshot

Revision ID: 0031
Revises: 0030
Create Date: 2026-11-08

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0031"
down_revision: str | None = "0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "matter_topology_snapshot",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("collected_at", sa.String(), nullable=True),
        sa.Column("payload_json", sa.String(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("matter_topology_snapshot")
