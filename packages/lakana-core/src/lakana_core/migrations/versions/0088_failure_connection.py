"""failure_connection: what a task's failure concerns, and the exception it ended with.

`task_runs` and `task_attempts` gain two nullable columns, stored with `failure_code`:
`failure_connection` (`String(63)`, the declared connection an error of `lakana.ingest` named)
and `error_type` (`String(100)`, the class name of the exception a Python task's code ended
with). The Connections screen shows the newest run that failed on a connection through two
indexes on `task_runs`: `ix_task_runs_failure_connection`, partial (only the rows that name a
connection), finds the failures that name it, and `ix_task_runs_asset_finished_at` the later
runs of the same asset. Existing rows stay null: nothing recorded what they concerned.

Revision ID: 95737c487bf3
Revises: 431d39830234
Create Date: 2026-11-19 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "95737c487bf3"
down_revision: str | Sequence[str] | None = "431d39830234"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = ("task_runs", "task_attempts")

_NAMES_A_CONNECTION = sa.text("failure_connection IS NOT NULL")
"""Only a failure that names a connection: a few among all the task runs."""

# (index name, columns, partial condition) on task_runs.
_INDEXES: tuple[tuple[str, tuple[str, ...], sa.TextClause | None], ...] = (
    ("ix_task_runs_failure_connection", ("failure_connection",), _NAMES_A_CONNECTION),
    ("ix_task_runs_asset_finished_at", ("asset", "finished_at"), None),
)


def upgrade() -> None:
    """Upgrade schema.

    Adds the two nullable columns to both tables and the two indexes; no row changes.
    """
    for table in _TABLES:
        with op.batch_alter_table(table) as batch_op:
            batch_op.add_column(sa.Column("error_type", sa.String(length=100), nullable=True))
            batch_op.add_column(
                sa.Column("failure_connection", sa.String(length=63), nullable=True)
            )
    for name, columns, where in _INDEXES:
        op.create_index(
            name,
            "task_runs",
            list(columns),
            unique=False,
            sqlite_where=where,
            postgresql_where=where,
        )


def downgrade() -> None:
    """Downgrade schema.

    Drops the indexes and the columns: the previous release has no place for them.
    """
    for name, _, _ in reversed(_INDEXES):
        op.drop_index(name, table_name="task_runs")
    for table in reversed(_TABLES):
        with op.batch_alter_table(table) as batch_op:
            batch_op.drop_column("failure_connection")
            batch_op.drop_column("error_type")
