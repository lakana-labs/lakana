"""query_runs.merge_key: the key a SQL asset's merge run matches rows on.

Revision ID: b8d4f0a2c3e5
Revises: a7c3e9f1b2d4
Create Date: 2026-10-24 09:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b8d4f0a2c3e5"
down_revision: str | Sequence[str] | None = "a7c3e9f1b2d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema.

    Adds `query_runs.merge_key`, a JSON list of column names, empty on every existing row: only
    a merge run (`write_mode` `MERGE`, new in this release, or the create of a merge asset's
    first run) sets it. `write_mode` is a non-native enum without a check constraint, so the
    member `MERGE` needs no change to the column.
    """
    with op.batch_alter_table("query_runs") as batch:
        batch.add_column(sa.Column("merge_key", sa.JSON(), nullable=True))


def downgrade() -> None:
    """Downgrade schema.

    The previous release cannot load a `write_mode` of `MERGE`, so every such run is recorded as
    `APPEND` first: a merge added and updated rows and deleted none, which is the closest mode
    that release knows (the run's audit event still says `merge`). Then the column is dropped.
    """
    op.execute(sa.text("UPDATE query_runs SET write_mode = 'APPEND' WHERE write_mode = 'MERGE'"))
    with op.batch_alter_table("query_runs") as batch:
        batch.drop_column("merge_key")
