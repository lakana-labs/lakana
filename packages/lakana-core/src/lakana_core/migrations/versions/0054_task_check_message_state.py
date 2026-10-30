"""task check message state: what a task's check message was built from, besides its results.

Revision ID: f4b8d6e0a2c3
Revises: e3a7c5d9f1b2
Create Date: 2026-10-29 23:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f4b8d6e0a2c3"
down_revision: str | Sequence[str] | None = "e3a7c5d9f1b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema.

    Adds `check_message_state` to `task_runs` and `task_attempts`, null on every existing row: a
    message recorded before this release is shown as it is, unless it names a check its reader
    may not see.
    """
    for table in ("task_runs", "task_attempts"):
        with op.batch_alter_table(table) as batch:
            batch.add_column(sa.Column("check_message_state", sa.JSON(), nullable=True))


def downgrade() -> None:
    """Downgrade schema.

    Drops the columns. The messages stay; the previous release shows them as they are.
    """
    for table in ("task_attempts", "task_runs"):
        with op.batch_alter_table(table) as batch:
            batch.drop_column("check_message_state")
