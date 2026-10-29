"""check_results.generated: whether a result is of a check generated from an asset's header.

Revision ID: d1f6b2c4e5a7
Revises: c9e5a1b3d4f6
Create Date: 2026-10-29 09:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d1f6b2c4e5a7"
down_revision: str | Sequence[str] | None = "c9e5a1b3d4f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema.

    Adds `check_results.generated`, false on every existing row: every result recorded before
    this release is of a check written under `checks/`, since no check was generated before it.
    """
    with op.batch_alter_table("check_results") as batch:
        batch.add_column(
            sa.Column("generated", sa.Boolean(), nullable=False, server_default=sa.false())
        )


def downgrade() -> None:
    """Downgrade schema.

    Drops the column. A result of a generated check keeps its name, path, outcome and error, and
    the previous release shows it like any other result; only the mark is lost.
    """
    with op.batch_alter_table("check_results") as batch:
        batch.drop_column("generated")
