"""table_sort_keys: the numbers a table list sorts on, kept on the table's row.

Revision ID: f6c9d2a5b318
Revises: e5b8c1f4a297
Create Date: 2026-11-12 09:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f6c9d2a5b318"
down_revision: str | Sequence[str] | None = "e5b8c1f4a297"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEXED = ("row_count", "size_bytes", "last_write_at", "last_read_at", "health_rank", "created_at")


def upgrade() -> None:
    """Upgrade schema.

    Six columns every existing table starts with at their neutral value: rows, last write, last
    read and the health word unknown (`NULL`), the health rank that of "not judged" (16) and
    no other findings. Housekeeping fills them afterwards. Two things are done to the rows that
    exist:

    - `last_read_at` is taken from the read counts (`table_reads`), the newest of each table;
    - `size_metadata_location` of every managed table is reset, so the size sweep measures the
      table again and reads its rows and last write together with its size. `size_bytes` is kept
      until then. `updated_at` is left as it is: it is retention's clock, and a backfill is no
      activity.

    Seven indexes, each on the sort key and `id`, so a keyset page reads in index order.
    """
    with op.batch_alter_table("tables") as batch_op:
        batch_op.add_column(sa.Column("row_count", sa.BigInteger(), nullable=True))
        batch_op.add_column(sa.Column("last_write_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("last_read_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(
            sa.Column("health_rank", sa.SmallInteger(), nullable=False, server_default="16")
        )
        batch_op.add_column(sa.Column("health_item", sa.String(length=32), nullable=True))
        batch_op.add_column(
            sa.Column("health_others", sa.Integer(), nullable=False, server_default="0")
        )
    op.execute(
        "UPDATE tables SET last_read_at = "
        "(SELECT max(last_read_at) FROM table_reads WHERE table_reads.table_id = tables.id)"
    )
    op.execute("UPDATE tables SET size_metadata_location = NULL WHERE table_type = 'ICEBERG'")
    for column in _INDEXED:
        op.create_index(f"ix_tables_{column}_id", "tables", [column, "id"], unique=False)
    op.create_index("ix_tables_name_id", "tables", ["name", "id"], unique=False)


def downgrade() -> None:
    """Downgrade schema.

    Drops the indexes and the six columns. The reset `size_metadata_location` stays reset, so
    the next size sweep measures every managed table again.
    """
    op.drop_index("ix_tables_name_id", table_name="tables")
    for column in reversed(_INDEXED):
        op.drop_index(f"ix_tables_{column}_id", table_name="tables")
    with op.batch_alter_table("tables") as batch_op:
        batch_op.drop_column("health_others")
        batch_op.drop_column("health_item")
        batch_op.drop_column("health_rank")
        batch_op.drop_column("last_read_at")
        batch_op.drop_column("last_write_at")
        batch_op.drop_column("row_count")
