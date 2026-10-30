"""deploy error withheld: the lines of a deploy's error that other readers are shown otherwise.

Revision ID: a5c9e7f1b3d4
Revises: f4b8d6e0a2c3
Create Date: 2026-10-30 09:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a5c9e7f1b3d4"
down_revision: str | Sequence[str] | None = "f4b8d6e0a2c3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema.

    Adds `error_withheld_json` to `deploy_runs`, null on every existing row: an error stored
    before this release is shown to every reader as it is.
    """
    with op.batch_alter_table("deploy_runs") as batch:
        batch.add_column(sa.Column("error_withheld_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    """Downgrade schema.

    Drops the column. The errors stay, whole; the previous release shows them to every reader.
    """
    with op.batch_alter_table("deploy_runs") as batch:
        batch.drop_column("error_withheld_json")
