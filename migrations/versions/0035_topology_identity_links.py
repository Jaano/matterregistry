"""enforce unique infrastructure topology identity

Revision ID: 0035
Revises: 0034

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0035"
down_revision: str | None = "0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "uq_device_link_topology_identity",
        "device_link",
        ["integration", "external_id"],
        unique=True,
        sqlite_where=sa.text("integration IN ('topology_border_router', 'topology_wifi_ap')"),
    )


def downgrade() -> None:
    op.drop_index("uq_device_link_topology_identity", table_name="device_link")
