"""table_sort_keys_descending: an index for each descending sort of the table list.

Revision ID: ada48b713c93
Revises: b9f4e2a7c058
Create Date: 2026-11-13 09:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "ada48b713c93"
down_revision: str | Sequence[str] | None = "b9f4e2a7c058"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEXED = ("row_count", "size_bytes", "last_write_at", "last_read_at", "health_rank", "created_at")


def upgrade() -> None:
    """Upgrade schema.

    Six indexes, each on a sort key and `id`, on PostgreSQL in the order a descending page reads
    them: `key DESC NULLS LAST, id DESC`. The ascending indexes of `0075` serve only the
    ascending pages there, since PostgreSQL reads an index backwards as `DESC NULLS FIRST` and
    the list sorts a null key last in both directions. SQLite refuses a null order on an index
    column; it gets `key DESC, id DESC`, the same order there, since a null is its smallest
    value. No row changes.
    """
    postgres = op.get_bind().dialect.name == "postgresql"
    for column in _INDEXED:
        key = sa.desc(sa.column(column))
        op.create_index(
            f"ix_tables_{column}_id_desc",
            "tables",
            [key.nulls_last() if postgres else key, sa.desc(sa.column("id"))],
            unique=False,
        )


def downgrade() -> None:
    """Downgrade schema.

    Drops the six indexes.
    """
    for column in reversed(_INDEXED):
        op.drop_index(f"ix_tables_{column}_id_desc", table_name="tables")
