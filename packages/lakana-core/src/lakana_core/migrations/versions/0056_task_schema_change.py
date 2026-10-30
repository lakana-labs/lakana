"""task schema change: the columns a task removed or replaced, and the policies that use them.

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

    Adds `schema_change_json` to `task_runs`, null on every existing row: nothing was recorded
    before this release.
    """
    with op.batch_alter_table("task_runs") as batch:
        batch.add_column(sa.Column("schema_change_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    """Downgrade schema.

    Drops the column and what it holds; the previous release shows none of it.
    """
    with op.batch_alter_table("task_runs") as batch:
        batch.drop_column("schema_change_json")
