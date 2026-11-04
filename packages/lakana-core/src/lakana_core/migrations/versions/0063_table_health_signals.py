"""Table health signals: one row per observation of a signal of a table.

Revision ID: c5e9f1a3b7d8
Revises: b4d8e0f2a6c7
Create Date: 2026-11-05 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c5e9f1a3b7d8"
down_revision: str | Sequence[str] | None = "b4d8e0f2a6c7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema.

    An empty table: a table's signals arrive with its next commit.
    """
    op.create_table(
        "table_health_signals",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("environment_id", sa.Uuid(), nullable=False),
        sa.Column("table_id", sa.Uuid(), nullable=False),
        sa.Column("signal", sa.String(length=16), nullable=False),
        sa.Column("column_name", sa.String(length=1024), nullable=True),
        sa.Column("snapshot_id", sa.BigInteger(), nullable=True),
        sa.Column("sequence_number", sa.BigInteger(), nullable=True),
        sa.Column("observed", sa.JSON(), nullable=True),
        sa.Column("band", sa.JSON(), nullable=True),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("effect", sa.String(length=8), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["environment_id"],
            ["environments.id"],
            name=op.f("fk_table_health_signals_environment_id_environments"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["table_id"],
            ["tables.id"],
            name=op.f("fk_table_health_signals_table_id_tables"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_table_health_signals")),
    )
    op.create_index(
        "ix_table_health_signals_table_id_observed_at",
        "table_health_signals",
        ["table_id", "observed_at"],
        unique=False,
    )
    op.create_index(
        "ix_table_health_signals_table_id_signal_sequence_number",
        "table_health_signals",
        ["table_id", "signal", "sequence_number", "observed_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_table_health_signals_table_id_state",
        "table_health_signals",
        ["table_id", "state", "signal", "column_name"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_table_health_signals_table_id_state", table_name="table_health_signals")
    op.drop_index(
        "ix_table_health_signals_table_id_signal_sequence_number",
        table_name="table_health_signals",
    )
    op.drop_index("ix_table_health_signals_table_id_observed_at", table_name="table_health_signals")
    op.drop_table("table_health_signals")
