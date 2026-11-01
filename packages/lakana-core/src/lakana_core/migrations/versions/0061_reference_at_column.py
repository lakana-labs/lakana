"""table_references.at_column: the time column of a reference to a table that keeps history.

Revision ID: a3c7d9e1f5b6
Revises: f2b6c8d0e4a5
Create Date: 2026-10-31 19:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a3c7d9e1f5b6"
down_revision: str | Sequence[str] | None = "f2b6c8d0e4a5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema.

    Adds `table_references.at_column`, empty on every existing row: only a reference declared
    with `at:` (new in this release) sets it, at the next deploy of its project.
    """
    with op.batch_alter_table("table_references") as batch:
        batch.add_column(sa.Column("at_column", sa.String(1024), nullable=True))


def downgrade() -> None:
    """Downgrade schema.

    Drops the column; the previous release shows such a reference without its time.
    """
    with op.batch_alter_table("table_references") as batch:
        batch.drop_column("at_column")
