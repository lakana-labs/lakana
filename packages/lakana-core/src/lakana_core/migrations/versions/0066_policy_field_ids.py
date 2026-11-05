"""The field ids of row filters and column masks.

Revision ID: f8b2c4d6e0a1
Revises: e7a1b3c5d9f0
Create Date: 2026-11-05 19:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f8b2c4d6e0a1"
down_revision: str | Sequence[str] | None = "e7a1b3c5d9f0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema.

    Adds the field-id binding of row filters and column masks. Every existing row is `pending`:
    the worker's housekeeping (`policy.legacy_binding.bind_legacy_policies`) binds it from the
    table's metadata, which a migration cannot read. Until then it is read as before.
    """
    for table in ("row_filters", "column_masks"):
        with op.batch_alter_table(table) as batch:
            batch.add_column(sa.Column("field_ids", sa.JSON(none_as_null=True), nullable=True))
            batch.add_column(
                sa.Column("binding", sa.String(16), nullable=False, server_default="pending")
            )
    with op.batch_alter_table("column_masks") as batch:
        batch.add_column(sa.Column("column_field_id", sa.Integer(), nullable=True))


def downgrade() -> None:
    """Downgrade schema.

    Drops the columns; the previous release reads every policy by name with its rename rule.
    """
    with op.batch_alter_table("column_masks") as batch:
        batch.drop_column("column_field_id")
    for table in ("row_filters", "column_masks"):
        with op.batch_alter_table(table) as batch:
            batch.drop_column("binding")
            batch.drop_column("field_ids")
