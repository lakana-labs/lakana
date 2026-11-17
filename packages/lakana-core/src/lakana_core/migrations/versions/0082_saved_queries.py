"""saved_queries: SQL text a person keeps under a name in one environment.

The owner is held twice: `owner_id` sets `NULL` when the owner's principal row is deleted, which
keeps a shared query with no owner; `private_owner_id`, set only while the query is private,
cascades, so the same deletion removes the private ones. The check `private_owner` keeps the two
in step.

Revision ID: e9f3a6b2d847
Revises: d8e2f5a1c736
Create Date: 2026-11-16 15:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e9f3a6b2d847"
down_revision: str | Sequence[str] | None = "d8e2f5a1c736"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEXED = ("environment_id", "owner_id", "private_owner_id")


def upgrade() -> None:
    """Upgrade schema.

    One empty table, with an index on each of its three keys.
    """
    op.create_table(
        "saved_queries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("environment_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("sql_text", sa.Text(), nullable=False),
        sa.Column("visibility", sa.String(length=16), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=True),
        sa.Column("private_owner_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(visibility = 'private' AND private_owner_id IS NOT NULL "
            "AND private_owner_id = owner_id) "
            "OR (visibility = 'everyone' AND private_owner_id IS NULL)",
            name=op.f("ck_saved_queries_private_owner"),
        ),
        sa.ForeignKeyConstraint(
            ["environment_id"],
            ["environments.id"],
            name=op.f("fk_saved_queries_environment_id_environments"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["principals.id"],
            name=op.f("fk_saved_queries_owner_id_principals"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["private_owner_id"],
            ["principals.id"],
            name=op.f("fk_saved_queries_private_owner_id_principals"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_saved_queries")),
        sa.UniqueConstraint(
            "environment_id",
            "owner_id",
            "name",
            name="uq_saved_queries_environment_owner_name",
        ),
    )
    for column in _INDEXED:
        op.create_index(f"ix_saved_queries_{column}", "saved_queries", [column], unique=False)


def downgrade() -> None:
    """Downgrade schema.

    Drops the table: every saved query is gone.
    """
    for column in reversed(_INDEXED):
        op.drop_index(f"ix_saved_queries_{column}", table_name="saved_queries")
    op.drop_table("saved_queries")
