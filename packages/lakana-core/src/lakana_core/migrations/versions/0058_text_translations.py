"""Translated labels and descriptions, and a principal's language.

Adds text_translations, the label columns, the text languages of a table and principals.language.

Revision ID: d0f4a6c8e2b3
Revises: c8e2f4a6b0d1
Create Date: 2026-10-30 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d0f4a6c8e2b3"
down_revision: str | Sequence[str] | None = "c8e2f4a6b0d1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema.

    No data migration: every existing description is a main-language text and shows unchanged.
    """
    op.create_table(
        "text_translations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("table_id", sa.Uuid(), nullable=False),
        sa.Column("subject", sa.String(length=16), nullable=False),
        sa.Column("name", sa.String(length=1024), nullable=False),
        sa.Column("language", sa.String(length=35), nullable=False),
        sa.Column("field", sa.String(length=16), nullable=False),
        sa.Column("text_project", sa.Text(), nullable=True),
        sa.Column("text_edited", sa.Text(), nullable=True),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["table_id"],
            ["tables.id"],
            name=op.f("fk_text_translations_table_id_tables"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by"],
            ["principals.id"],
            name=op.f("fk_text_translations_updated_by_principals"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_text_translations")),
        sa.UniqueConstraint(
            "table_id",
            "subject",
            "name",
            "language",
            "field",
            name=op.f("uq_text_translations_table_id_subject_name_language_field"),
        ),
    )
    with op.batch_alter_table("tables") as batch_op:
        batch_op.add_column(sa.Column("label_project", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("label_edited", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("text_language", sa.String(length=35), nullable=True))
        batch_op.add_column(sa.Column("text_languages", sa.JSON(), nullable=True))
    with op.batch_alter_table("column_descriptions") as batch_op:
        batch_op.add_column(sa.Column("label_project", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("label_edited", sa.Text(), nullable=True))
    with op.batch_alter_table("principals") as batch_op:
        batch_op.add_column(sa.Column("language", sa.String(length=35), nullable=True))


def downgrade() -> None:
    """Downgrade schema.

    Drops the translations, labels and languages; the manifests keep the project's texts, so
    upgrading again and deploying writes them back. Edits of labels and translations are lost.
    """
    with op.batch_alter_table("principals") as batch_op:
        batch_op.drop_column("language")
    with op.batch_alter_table("column_descriptions") as batch_op:
        batch_op.drop_column("label_edited")
        batch_op.drop_column("label_project")
    with op.batch_alter_table("tables") as batch_op:
        batch_op.drop_column("text_languages")
        batch_op.drop_column("text_language")
        batch_op.drop_column("label_edited")
        batch_op.drop_column("label_project")
    op.drop_table("text_translations")
