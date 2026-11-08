"""add bridged_json to matter_node_record

Revision ID: 0032
Revises: 0031
Create Date: 2026-11-08

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0032"
down_revision: str | None = "0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("matter_node_record", sa.Column("bridged_json", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("matter_node_record") as batch:
        batch.drop_column("bridged_json")
