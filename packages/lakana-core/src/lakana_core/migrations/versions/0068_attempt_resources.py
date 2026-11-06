"""What an attempt used: peak memory, processor time and bytes read.

Revision ID: c4e6a8b0d2f1
Revises: a9c3e5f7b1d2
Create Date: 2026-11-06 09:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4e6a8b0d2f1"
down_revision: str | Sequence[str] | None = "a9c3e5f7b1d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = ("task_attempts", "query_runs")


def upgrade() -> None:
    """Upgrade schema.

    Adds three nullable figures to task attempts and query runs. Existing rows stay null:
    nobody measured them.
    """
    for table in _TABLES:
        with op.batch_alter_table(table) as batch:
            batch.add_column(sa.Column("peak_memory_bytes", sa.BigInteger(), nullable=True))
            batch.add_column(sa.Column("cpu_seconds", sa.Float(), nullable=True))
            batch.add_column(sa.Column("bytes_read", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    """Downgrade schema.

    Drops the figures; the previous release does not read them.
    """
    for table in _TABLES:
        with op.batch_alter_table(table) as batch:
            batch.drop_column("bytes_read")
            batch.drop_column("cpu_seconds")
            batch.drop_column("peak_memory_bytes")
