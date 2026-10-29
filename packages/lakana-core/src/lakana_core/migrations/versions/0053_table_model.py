"""The model a project declares for a table.

Adds tables.model_kind, model_grain and model_key, and the table_references rows.

Revision ID: e3a7c5d9f1b2
Revises: d1f6b2c4e5a7
Create Date: 2026-10-29 15:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e3a7c5d9f1b2"
down_revision: str | Sequence[str] | None = "d1f6b2c4e5a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema.

    Adds `tables.model_kind`, `model_grain` and `model_key`, null on every existing row, and
    creates `table_references`, empty. No data migration: the next deploy, rollback or table
    creation of each project writes the model its asset headers declare.
    """
    with op.batch_alter_table("tables") as batch:
        batch.add_column(sa.Column("model_kind", sa.String(length=16), nullable=True))
        batch.add_column(sa.Column("model_grain", sa.Text(), nullable=True))
        batch.add_column(sa.Column("model_key", sa.JSON(), nullable=True))
    op.create_table(
        "table_references",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("environment_id", sa.Uuid(), nullable=False),
        sa.Column("source_table_id", sa.Uuid(), nullable=False),
        sa.Column("source_column", sa.String(length=1024), nullable=False),
        sa.Column("target_name", sa.String(length=1024), nullable=False),
        sa.Column("target_column", sa.String(length=1024), nullable=False),
        sa.ForeignKeyConstraint(
            ["environment_id"],
            ["environments.id"],
            name=op.f("fk_table_references_environment_id_environments"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_table_id"],
            ["tables.id"],
            name=op.f("fk_table_references_source_table_id_tables"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_table_references")),
        sa.UniqueConstraint(
            "source_table_id",
            "source_column",
            name=op.f("uq_table_references_source_table_id_source_column"),
        ),
    )
    op.create_index(
        "ix_table_references_environment_target",
        "table_references",
        ["environment_id", "target_name"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema.

    Drops `table_references` and the three model columns. The previous release does not show a
    model; the manifests keep it, so upgrading again and deploying writes it back.
    """
    op.drop_index("ix_table_references_environment_target", table_name="table_references")
    op.drop_table("table_references")
    with op.batch_alter_table("tables") as batch:
        batch.drop_column("model_key")
        batch.drop_column("model_grain")
        batch.drop_column("model_kind")
