"""delete device_integration_data rows whose device no longer exists

Revision ID: 0033
Revises: 0032
Create Date: 2026-11-17

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0033"
down_revision: str | None = "0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Deleting a Device left its per-device integration data behind. The stale
    # rows kept the deleted device's addresses and made them look shared.
    op.execute("DELETE FROM device_integration_data WHERE device_id NOT IN (SELECT id FROM device)")


def downgrade() -> None:
    # Deleted rows were a rebuildable integration cache; nothing to restore.
    pass
