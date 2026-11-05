"""The record of the files an incremental files.read read.

Revision ID: e7a1b3c5d9f0
Revises: d6f0a2b4c8e9
Create Date: 2026-11-05 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e7a1b3c5d9f0"
down_revision: str | Sequence[str] | None = "d6f0a2b4c8e9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema.

    An empty table: the first run of each incremental asset after the upgrade seeds it.
    """
    op.create_table(
        "incremental_files_read",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("table_id", sa.Uuid(), nullable=False),
        sa.Column("connection", sa.String(length=63), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("modified", sa.DateTime(timezone=True), nullable=False),
        sa.Column("etag", sa.String(length=512), nullable=True),
        sa.Column("snapshot_id", sa.BigInteger(), nullable=False),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["table_id"],
            ["tables.id"],
            name=op.f("fk_incremental_files_read_table_id_tables"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_incremental_files_read")),
    )
    op.create_index(
        "ix_incremental_files_read_table_id_connection_modified",
        "incremental_files_read",
        ["table_id", "connection", "modified"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        "ix_incremental_files_read_table_id_connection_modified",
        table_name="incremental_files_read",
    )
    op.drop_table("incremental_files_read")
