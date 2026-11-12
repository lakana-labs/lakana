"""query run preview: the table a preview is bound to, and the masks a query applied.

Revision ID: a7d3e9c1b406
Revises: f6c9d2a5b318
Create Date: 2026-11-12 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7d3e9c1b406"
down_revision: str | Sequence[str] | None = "f6c9d2a5b318"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema.

    Adds two nullable columns to `query_runs`:

    - `preview_table_id`, the table or view a preview reads, without a foreign key. As with
      `check_result_id`, a dropped table must leave the id behind, so that the worker fails
      the run; a foreign key that emptied the column would turn the preview into an ordinary
      query of whatever now has that name.
    - `masked_columns`, the masks the worker applied for the reader.

    A query run written before this reads as no preview, with its masks not recorded.
    """
    with op.batch_alter_table("query_runs") as batch_op:
        batch_op.add_column(sa.Column("preview_table_id", sa.Uuid(), nullable=True))
        batch_op.add_column(sa.Column("masked_columns", sa.JSON(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("query_runs") as batch_op:
        batch_op.drop_column("masked_columns")
        batch_op.drop_column("preview_table_id")
