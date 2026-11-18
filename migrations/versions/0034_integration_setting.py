"""add integration_setting

Revision ID: 0034
Revises: 0033
Create Date: 2026-11-17

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0034"
down_revision: str | None = "0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "integration_setting",
        sa.Column("integration", sa.String(), nullable=False),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("value_json", sa.String(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("integration", "key"),
    )


def downgrade() -> None:
    op.drop_table("integration_setting")
