"""The measures a project declares for a table.

Adds the table_measures rows.

Revision ID: c8e2f4a6b0d1
Revises: b6d0f8a2c4e5
Create Date: 2026-10-30 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c8e2f4a6b0d1"
down_revision: str | Sequence[str] | None = "b6d0f8a2c4e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema.

    Creates `table_measures`, empty. No data migration: no project declares measures before this
    release, and the next deploy of one that does writes them.
    """
    op.create_table(
        "table_measures",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("environment_id", sa.Uuid(), nullable=False),
        sa.Column("table_id", sa.Uuid(), nullable=False),
        sa.Column("column", sa.String(length=1024), nullable=False),
        sa.Column("aggregation", sa.String(length=16), nullable=False),
        sa.Column("additivity", sa.String(length=16), nullable=False),
        sa.Column("not_over", sa.JSON(), nullable=False),
        sa.Column("unit", sa.String(length=32), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=True),
        sa.Column("label", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["environment_id"],
            ["environments.id"],
            name=op.f("fk_table_measures_environment_id_environments"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["table_id"],
            ["tables.id"],
            name=op.f("fk_table_measures_table_id_tables"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_table_measures")),
        sa.UniqueConstraint("table_id", "column", name=op.f("uq_table_measures_table_id_column")),
    )


def downgrade() -> None:
    """Downgrade schema.

    Drops `table_measures`. The previous release shows no measures; the manifests keep them, so
    upgrading again and deploying writes them back.
    """
    op.drop_table("table_measures")
