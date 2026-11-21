"""saved_query_list_key: page the saved-query lists in the database (#692).

`saved_queries.name_key` holds `str.casefold` of each name, the first key of the lists' order and of
their cursors, so the database can order, resume and cut a page itself instead of the service
reading every visible saved query. On Postgres it is collated by code point (``"C"``), the order
Python and SQLite compare in, so a cursor issued before resumes where it did. Existing rows get the
key from their name here. The index `ix_saved_queries_environment_visibility_name_key` finds the
shared queries of the environments a caller may query, in that order.

Revision ID: 7c1e5a9d3f20
Revises: 95737c487bf3
Create Date: 2026-11-20 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7c1e5a9d3f20"
down_revision: str | Sequence[str] | None = "95737c487bf3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX = "ix_saved_queries_environment_visibility_name_key"
_KEY = sa.String(600).with_variant(sa.String(600, collation="C"), "postgresql")


def upgrade() -> None:
    """Upgrade schema.

    Adds `name_key`, sets it from each row's name in Python (`str.casefold`, which no database
    function matches), makes it required and adds the index.
    """
    with op.batch_alter_table("saved_queries") as batch_op:
        batch_op.add_column(sa.Column("name_key", _KEY, nullable=True))
    saved = sa.table(
        "saved_queries", sa.column("id", sa.Uuid()), sa.column("name"), sa.column("name_key")
    )
    bind = op.get_bind()
    rows = bind.execute(sa.select(saved.c.id, saved.c.name)).all()
    for row_id, name in rows:
        bind.execute(sa.update(saved).where(saved.c.id == row_id).values(name_key=name.casefold()))
    with op.batch_alter_table("saved_queries") as batch_op:
        batch_op.alter_column("name_key", existing_type=_KEY, nullable=False)
    op.create_index(
        _INDEX, "saved_queries", ["environment_id", "visibility", "name_key"], unique=False
    )


def downgrade() -> None:
    """Downgrade schema.

    Drops the index and `name_key`; the release before sorts the lists in Python again.
    """
    op.drop_index(_INDEX, table_name="saved_queries")
    with op.batch_alter_table("saved_queries") as batch_op:
        batch_op.drop_column("name_key")
