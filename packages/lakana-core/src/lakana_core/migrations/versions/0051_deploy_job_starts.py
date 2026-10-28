"""deploy_runs.job_starts_json: the jobs a deploy starts after it, and the trigger `deploy`.

Revision ID: c9e5a1b3d4f6
Revises: b8d4f0a2c3e5
Create Date: 2026-10-28 09:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c9e5a1b3d4f6"
down_revision: str | Sequence[str] | None = "b8d4f0a2c3e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema.

    Adds `deploy_runs.job_starts_json`, null on every existing row: only a deploy of this release
    that makes another project version current writes it, for its jobs with `on_deploy: true`.
    `runs.trigger` is a non-native enum without a check constraint (`String(8)`, holding the
    member's name), so the member `DEPLOY` needs no change to the column.
    """
    with op.batch_alter_table("deploy_runs") as batch:
        batch.add_column(sa.Column("job_starts_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    """Downgrade schema.

    The previous release cannot load a run whose `trigger` is `DEPLOY`, so each such run is
    recorded as a start through the API when the principal that started it is a service account,
    and as a start by hand otherwise, which is what the same start would have been called before.
    A waiting start of the trigger `deploy` (`job_schedules.pending_start`, JSON) is left as it
    is: the previous release drops it when it comes to start, as unreadable, with an audit event.
    Then the column is dropped.
    """
    op.execute(
        sa.text(
            "UPDATE runs SET trigger = CASE WHEN EXISTS (SELECT 1 FROM principals"
            " WHERE principals.id = runs.triggered_by AND principals.type = 'SERVICE_ACCOUNT')"
            " THEN 'API' ELSE 'MANUAL' END WHERE trigger = 'DEPLOY'"
        )
    )
    with op.batch_alter_table("deploy_runs") as batch:
        batch.drop_column("job_starts_json")
