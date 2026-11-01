"""query_runs.history: how a history asset's run writes its table.

Revision ID: f2b6c8d0e4a5
Revises: e1a5b7c9d3f4
Create Date: 2026-10-31 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f2b6c8d0e4a5"
down_revision: str | Sequence[str] | None = "e1a5b7c9d3f4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema.

    Adds `query_runs.history`, a JSON object (key, valid_from, close_missing), empty on every
    existing row: only a history run (`write_mode` `HISTORY`, new in this release, or the create
    of a history asset's first run) sets it. `write_mode` is a non-native enum without a check
    constraint, so the member `HISTORY` needs no change to the column.
    """
    with op.batch_alter_table("query_runs") as batch:
        batch.add_column(sa.Column("history", sa.JSON(), nullable=True))


def downgrade() -> None:
    """Downgrade schema.

    The previous release cannot load a `write_mode` of `HISTORY`, so every such run is recorded
    as `MERGE` first, as revision 0050 recorded a merge as an append: a history write rewrote the
    versions it closed and added new ones, and deleted none, which is the closest mode that
    release knows (the run's audit event still says `history`). Then the column is dropped.
    """
    op.execute(sa.text("UPDATE query_runs SET write_mode = 'MERGE' WHERE write_mode = 'HISTORY'"))
    with op.batch_alter_table("query_runs") as batch:
        batch.drop_column("history")
