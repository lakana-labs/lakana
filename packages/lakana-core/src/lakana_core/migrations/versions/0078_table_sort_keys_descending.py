"""table_sort_keys_descending: an index for each descending sort of the table list.

Revision ID: ada48b713c93
Revises: b9f4e2a7c058
Create Date: 2026-11-13 09:00:00.000000

"""

from collections.abc import Sequence

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
    the list sorts a null key last in both directions. SQLite takes no order on an index
    column's nulls; it gets ascending copies (its backwards read of `0075`'s indexes already
    gives the descending order). No row changes.
    """
    for column in _INDEXED:
        op.create_index(
            f"ix_tables_{column}_id_desc",
            "tables",
            [column, "id"],
            unique=False,
            postgresql_ops={column: "DESC NULLS LAST", "id": "DESC"},
        )


def downgrade() -> None:
    """Downgrade schema.

    Drops the six indexes.
    """
    for column in reversed(_INDEXED):
        op.drop_index(f"ix_tables_{column}_id_desc", table_name="tables")
