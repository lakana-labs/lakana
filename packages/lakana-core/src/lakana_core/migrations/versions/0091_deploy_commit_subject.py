"""deploy_commit_subject: the first line of each deploy's commit message (#783).

`deploy_runs.commit_subject` holds the first line of the message of the commit a deploy or dry
run fetched, cut to 200 characters, and for a rollback the subject of the deploy that made its
target version. The history shows it beside the commit's ID. Runs from before the column have
none, and none is filled in here: the commits are in the repositories, which only a worker reads.

Revision ID: 4b8d2f6a1c93
Revises: 7c3e91a0b5d2
Create Date: 2026-11-21 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4b8d2f6a1c93"
down_revision: str | Sequence[str] | None = "7c3e91a0b5d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema.

    Adds the nullable `commit_subject`; existing runs have none.
    """
    with op.batch_alter_table("deploy_runs") as batch_op:
        batch_op.add_column(sa.Column("commit_subject", sa.String(length=200), nullable=True))


def downgrade() -> None:
    """Downgrade schema.

    Drops `commit_subject`; the release before shows each commit's ID only.
    """
    with op.batch_alter_table("deploy_runs") as batch_op:
        batch_op.drop_column("commit_subject")
