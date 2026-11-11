"""worker_memory: the memory each worker last reported, for the instance status.

Revision ID: f3c7a9e1d5b2
Revises: e8a2c4f6b0d3
Create Date: 2026-11-10 09:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f3c7a9e1d5b2"
down_revision: str | Sequence[str] | None = "e8a2c4f6b0d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema.

    Adds three columns of `workers`: the resident memory a worker last reported, its limit then,
    and when it was written. They are empty on every existing row; a worker writes them with its
    next heartbeat.
    """
    with op.batch_alter_table("workers") as batch:
        batch.add_column(sa.Column("memory_used_bytes", sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column("memory_limit_bytes", sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column("memory_measured_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    """Downgrade schema.

    Drops the columns; the figures are measurements, reported again after a new upgrade.
    """
    with op.batch_alter_table("workers") as batch:
        batch.drop_column("memory_measured_at")
        batch.drop_column("memory_limit_bytes")
        batch.drop_column("memory_used_bytes")
