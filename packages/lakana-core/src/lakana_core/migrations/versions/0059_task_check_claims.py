"""task check claims: the claims a written task took only to run its checks.

Revision ID: e1a5b7c9d3f4
Revises: d0f4a6c8e2b3
Create Date: 2026-10-31 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e1a5b7c9d3f4"
down_revision: str | Sequence[str] | None = "d0f4a6c8e2b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema.

    Adds `task_runs.check_claims`, zero on every existing row: a task that waits for its checks
    when the upgrade lands gets the whole bound from then on.
    """
    with op.batch_alter_table("task_runs") as batch:
        batch.add_column(
            sa.Column("check_claims", sa.Integer(), server_default="0", nullable=False)
        )


def downgrade() -> None:
    """Downgrade schema.

    Drops the column; the previous release tries the checks without a bound.
    """
    with op.batch_alter_table("task_runs") as batch:
        batch.drop_column("check_claims")
