"""repository_probes: repository checks a worker runs, and the deploy keys Lakana makes.

Revision ID: e5b8c1f4a297
Revises: d4a7e2b9c613
Create Date: 2026-11-11 15:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e5b8c1f4a297"
down_revision: str | Sequence[str] | None = "d4a7e2b9c613"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KINDS = ("HOST_KEYS", "ACCESS")
_STATES = ("QUEUED", "RUNNING", "DONE")
_OUTCOMES = (
    "OK",
    "HOST_KEY_CHANGED",
    "HOST_KEY_UNKNOWN",
    "AUTH_REFUSED",
    "NOT_FOUND",
    "UNREACHABLE",
    "REFUSED_ADDRESS",
    "TIMEOUT",
    "NO_WORKER",
    "FAILED",
)


def upgrade() -> None:
    """Upgrade schema.

    One empty table, and two nullable columns every existing secret starts without: no secret
    is a deploy key Lakana made yet. `generated_for` is the id of the repository a key was made
    for (no foreign key: like `use_target_id`, it keeps the id of a repository that is removed),
    `public_key` the public half shown to add at the git host.
    """
    op.create_table(
        "repository_probes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(*_KINDS, name="repositoryprobekind", native_enum=False, length=16),
            nullable=False,
        ),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("repository_id", sa.Uuid(), nullable=True),
        sa.Column("environment_id", sa.Uuid(), nullable=True),
        sa.Column("requested_by", sa.Uuid(), nullable=False),
        sa.Column(
            "state",
            sa.Enum(*_STATES, name="repositoryprobestate", native_enum=False, length=16),
            nullable=False,
        ),
        # jscpd:ignore-start -- a migration spells out its columns; it must never share code
        # with another one, which a later change could alter under an applied revision.
        sa.Column(
            "outcome",
            sa.Enum(*_OUTCOMES, name="probeoutcome", native_enum=False, length=16),
            nullable=True,
        ),
        sa.Column("message", sa.String(length=1000), nullable=True),
        sa.Column("host_keys", sa.JSON(), nullable=True),
        sa.Column("lease_owner", sa.String(length=255), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        # jscpd:ignore-end
        sa.ForeignKeyConstraint(
            ["repository_id"],
            ["repositories.id"],
            name=op.f("fk_repository_probes_repository_id_repositories"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["environment_id"],
            ["environments.id"],
            name=op.f("fk_repository_probes_environment_id_environments"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by"],
            ["principals.id"],
            name=op.f("fk_repository_probes_requested_by_principals"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_repository_probes")),
    )
    op.create_index(
        "ix_repository_probes_claim",
        "repository_probes",
        ["state", "lease_expires_at"],
        unique=False,
    )
    with op.batch_alter_table("secrets") as batch_op:
        batch_op.add_column(sa.Column("generated_for", sa.Uuid(), nullable=True))
        batch_op.add_column(sa.Column("public_key", sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema.

    Drops the table and the two columns: every pending or finished check is gone (they are kept
    for a day only), and a deploy key Lakana made becomes an ordinary secret bound to its
    repository: its private key stays and deploys keep working, its public half is no longer
    shown, and it can then be rotated and bound by hand.
    """
    with op.batch_alter_table("secrets") as batch_op:
        batch_op.drop_column("public_key")
        batch_op.drop_column("generated_for")
    op.drop_index("ix_repository_probes_claim", table_name="repository_probes")
    op.drop_table("repository_probes")
