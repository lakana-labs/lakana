"""secret_rotated_at: when a secret's values were last replaced.

`secrets.rotated_at` is set by every rotation (`lakana_core.secrets.service.rotate_values`), so
the web interface and `lakana secret list` can show it. Existing secrets keep `NULL`: nothing
stored when they were rotated before, and the audit log's `SECRET_ROTATED` events remain the
history of those rotations.

Revision ID: 4d96fb277743
Revises: 532b5154629b
Create Date: 2026-11-18 20:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4d96fb277743"
down_revision: str | Sequence[str] | None = "532b5154629b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema.

    Adds the nullable column; no row changes.
    """
    with op.batch_alter_table("secrets") as batch_op:
        batch_op.add_column(sa.Column("rotated_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    """Downgrade schema.

    Drops the column; the rows stay.
    """
    with op.batch_alter_table("secrets") as batch_op:
        batch_op.drop_column("rotated_at")
