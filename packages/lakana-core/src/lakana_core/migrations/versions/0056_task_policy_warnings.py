"""task policy warnings: row filters and column masks a task's schema change left out of date.

Revision ID: b6d0f8a2c4e5
Revises: a5c9e7f1b3d4
Create Date: 2026-10-30 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b6d0f8a2c4e5"
down_revision: str | Sequence[str] | None = "a5c9e7f1b3d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema.

    Adds `policy_warnings_json` to `task_runs`, null on every existing row: no warning was
    recorded before this release.
    """
    with op.batch_alter_table("task_runs") as batch:
        batch.add_column(sa.Column("policy_warnings_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    """Downgrade schema.

    Drops the column and the warnings in it; the previous release shows none.
    """
    with op.batch_alter_table("task_runs") as batch:
        batch.drop_column("policy_warnings_json")
