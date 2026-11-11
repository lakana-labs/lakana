"""interface_state: what the web interface remembers per person, and what they opened recently.

Revision ID: c109929efd3f
Revises: f3c7a9e1d5b2
Create Date: 2026-11-11 09:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c109929efd3f"
down_revision: str | Sequence[str] | None = "f3c7a9e1d5b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema.

    Two empty tables. A person without a row in `interface_states` gets the defaults (the tour
    not seen, Get started shown, no update note seen), so every existing person sees the tour
    once on their next sign-in.
    """
    op.create_table(
        "interface_states",
        sa.Column("principal_id", sa.Uuid(), nullable=False),
        sa.Column("tour_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("get_started_hidden", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("update_note_seen", sa.String(length=64), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["principal_id"],
            ["principals.id"],
            name=op.f("fk_interface_states_principal_id_principals"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("principal_id", name=op.f("pk_interface_states")),
    )
    op.create_table(
        "recent_opens",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("principal_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=8), nullable=False),
        sa.Column("environment_id", sa.Uuid(), nullable=False),
        sa.Column("ref", sa.String(length=1024), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["environment_id"],
            ["environments.id"],
            name=op.f("fk_recent_opens_environment_id_environments"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["principal_id"],
            ["principals.id"],
            name=op.f("fk_recent_opens_principal_id_principals"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_recent_opens")),
        sa.UniqueConstraint(
            "principal_id",
            "kind",
            "environment_id",
            "ref",
            name=op.f("uq_recent_opens_principal_id_kind_environment_id_ref"),
        ),
    )
    op.create_index(
        "ix_recent_opens_principal_id_opened_at",
        "recent_opens",
        ["principal_id", "opened_at"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema.

    Drops both tables: every person sees the tour and Get started again, and their recently
    opened list starts empty.
    """
    op.drop_index("ix_recent_opens_principal_id_opened_at", table_name="recent_opens")
    op.drop_table("recent_opens")
    op.drop_table("interface_states")
