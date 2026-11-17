"""query_run_origin: where a run came from, its result size and the position of its error.

An old run is given its origin by the first of these rules that holds:

1. `preview_table_id` is set: `preview`;
2. `check_result_id` is set: `check`;
3. `write_mode` is `MAINTAIN`: `maintenance`;
4. `task_run_id` or `task_token_id` is set: `task`;
5. `write_mode` is set: `write`;
6. otherwise: `written`.

An old check run of a task is therefore `task`; only runs made after this revision say `check`.

The column keeps a database default of `written`, so that a server or worker of the previous
release, which does not know the column, still inserts runs during an upgrade; they are
recorded as `written`. The model has no default: new code names an origin everywhere.

Revision ID: d8e2f5a1c736
Revises: 15081774c52d
Create Date: 2026-11-16 09:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d8e2f5a1c736"
down_revision: str | Sequence[str] | None = "15081774c52d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX = "ix_query_runs_principal_id_finished_at"
_COLUMNS = ("result_rows", "result_bytes", "error_line", "error_column")


def upgrade() -> None:
    """Upgrade schema.

    Adds `origin` (nullable at first), fills it by the rule in the module docstring with one
    `UPDATE`, then makes it not null with the default `written`. Adds the four nullable size
    and position columns, which stay `NULL` for old runs, and the index
    `(principal_id, finished_at)` that serves a person's list of their own runs.
    """
    op.add_column("query_runs", sa.Column("origin", sa.String(16), nullable=True))
    op.add_column("query_runs", sa.Column("result_rows", sa.BigInteger(), nullable=True))
    op.add_column("query_runs", sa.Column("result_bytes", sa.BigInteger(), nullable=True))
    op.add_column("query_runs", sa.Column("error_line", sa.Integer(), nullable=True))
    op.add_column("query_runs", sa.Column("error_column", sa.Integer(), nullable=True))
    op.execute(
        "UPDATE query_runs SET origin = CASE "
        "WHEN preview_table_id IS NOT NULL THEN 'preview' "
        "WHEN check_result_id IS NOT NULL THEN 'check' "
        "WHEN write_mode = 'MAINTAIN' THEN 'maintenance' "
        "WHEN task_run_id IS NOT NULL OR task_token_id IS NOT NULL THEN 'task' "
        "WHEN write_mode IS NOT NULL THEN 'write' "
        "ELSE 'written' END"
    )
    with op.batch_alter_table("query_runs") as batch:
        batch.alter_column(
            "origin",
            existing_type=sa.String(16),
            nullable=False,
            server_default="written",
        )
    op.create_index(_INDEX, "query_runs", ["principal_id", "finished_at"], unique=False)


def downgrade() -> None:
    """Downgrade schema.

    Drops the index and the five columns; the origin of a run is not kept.
    """
    op.drop_index(_INDEX, table_name="query_runs")
    with op.batch_alter_table("query_runs") as batch:
        for column in (*reversed(_COLUMNS), "origin"):
            batch.drop_column(column)
