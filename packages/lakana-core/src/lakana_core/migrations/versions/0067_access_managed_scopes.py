"""Access as code: the scopes an access file manages, and the overrides of them.

Revision ID: a9c3e5f7b1d2
Revises: f8b2c4d6e0a1
Create Date: 2026-11-06 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a9c3e5f7b1d2"
down_revision: str | Sequence[str] | None = "f8b2c4d6e0a1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema.

    Two empty tables: no scope is managed until an access file is applied.
    """
    op.create_table(
        "access_managed_scopes",
        sa.Column("scope", sa.String(length=255), nullable=False),
        sa.Column("file_sha256", sa.String(length=64), nullable=False),
        sa.Column("file_name", sa.String(length=255), nullable=False),
        sa.Column("source_commit", sa.String(length=64), nullable=True),
        sa.Column("apply_id", sa.Uuid(), nullable=False),
        sa.Column("applied_by", sa.Uuid(), nullable=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["applied_by"],
            ["principals.id"],
            name=op.f("fk_access_managed_scopes_applied_by_principals"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("scope", name=op.f("pk_access_managed_scopes")),
    )
    op.create_table(
        "access_overrides",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("scope", sa.String(length=255), nullable=False),
        sa.Column("change", sa.String(length=255), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("principal_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("audit_event_id", sa.Uuid(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by_apply", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(
            ["principal_id"],
            ["principals.id"],
            name=op.f("fk_access_overrides_principal_id_principals"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_access_overrides")),
    )
    op.create_index(op.f("ix_access_overrides_scope"), "access_overrides", ["scope"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_access_overrides_scope"), table_name="access_overrides")
    op.drop_table("access_overrides")
    op.drop_table("access_managed_scopes")
