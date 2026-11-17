"""run_list_indexes: the indexes that serve the lists of runs of every environment.

`GET /api/v1/runs` pages the runs of every environment the caller may read by
`(created_at, id)` with one cursor, and `GET /api/v1/runs/failed` ranks finished runs by
`finished_at`; neither is served by the per-environment indexes the table has.

Revision ID: f6b1d4e8a273
Revises: e9f3a6b2d847
Create Date: 2026-11-17 10:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f6b1d4e8a273"
down_revision: str | Sequence[str] | None = "e9f3a6b2d847"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (index name, table, columns); add a row to add an index.
_INDEXES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("ix_runs_created_at_id", "runs", ("created_at", "id")),
    ("ix_runs_finished_at", "runs", ("finished_at",)),
)


def upgrade() -> None:
    """Upgrade schema.

    Creates each index of `_INDEXES`; no table or column changes.
    """
    for name, table, columns in _INDEXES:
        op.create_index(name, table, list(columns), unique=False)


def downgrade() -> None:
    """Downgrade schema.

    Drops the indexes, last created first.
    """
    for name, table, _ in reversed(_INDEXES):
        op.drop_index(name, table_name=table)
