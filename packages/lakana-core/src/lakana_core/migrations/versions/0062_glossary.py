"""The glossary: terms, their texts, links and related ids.

Revision ID: b4d8e0f2a6c7
Revises: a3c7d9e1f5b6
Create Date: 2026-11-01 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b4d8e0f2a6c7"
down_revision: str | Sequence[str] | None = "a3c7d9e1f5b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CHILDREN = ("glossary_term_texts", "glossary_term_links", "glossary_term_related")


def _child_columns(table: str) -> tuple[list[sa.Column], list[str]]:
    """Give a child table's own columns and its primary key.

    Parameters
    ----------
    table
        One of `_CHILDREN`.

    Returns
    -------
    tuple
        The columns besides `term_id`, and the primary key's column names.
    """
    if table == "glossary_term_texts":
        return [
            sa.Column("language", sa.String(length=35), nullable=False),
            sa.Column("field", sa.String(length=16), nullable=False),
            sa.Column("position", sa.Integer(), nullable=False),
            sa.Column("text", sa.Text(), nullable=False),
        ], ["term_id", "language", "field", "position"]
    if table == "glossary_term_links":
        return [
            sa.Column("position", sa.Integer(), nullable=False),
            sa.Column("table_name", sa.String(length=1024), nullable=False),
            sa.Column("column_name", sa.String(length=1024), nullable=True),
        ], ["term_id", "position"]
    return [
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("related_key", sa.String(length=63), nullable=False),
    ], ["term_id", "position"]


def upgrade() -> None:
    """Upgrade schema.

    Four empty tables: a repository's terms arrive with its next deploy that has `glossary/`.
    """
    op.create_table(
        "glossary_terms",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("environment_id", sa.Uuid(), nullable=False),
        sa.Column("repository_id", sa.Uuid(), nullable=False),
        sa.Column("term_key", sa.String(length=63), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("definition", sa.Text(), nullable=False),
        sa.Column("owner", sa.Text(), nullable=False),
        sa.Column("main_language", sa.String(length=35), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["environment_id"],
            ["environments.id"],
            name=op.f("fk_glossary_terms_environment_id_environments"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["repository_id"],
            ["repositories.id"],
            name=op.f("fk_glossary_terms_repository_id_repositories"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_glossary_terms")),
        sa.UniqueConstraint(
            "environment_id", "term_key", name=op.f("uq_glossary_terms_environment_id_term_key")
        ),
    )
    for table in _CHILDREN:
        columns, keys = _child_columns(table)
        op.create_table(
            table,
            sa.Column("term_id", sa.Uuid(), nullable=False),
            *columns,
            sa.ForeignKeyConstraint(
                ["term_id"],
                ["glossary_terms.id"],
                name=op.f(f"fk_{table}_term_id_glossary_terms"),
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint(*keys, name=op.f(f"pk_{table}")),
        )
    op.create_index(
        "ix_glossary_term_links_table_name", "glossary_term_links", ["table_name"], unique=False
    )


def downgrade() -> None:
    """Downgrade schema.

    Drops the glossary; the manifests keep it, so upgrading again and deploying writes it back.
    """
    op.drop_index("ix_glossary_term_links_table_name", table_name="glossary_term_links")
    for table in (*_CHILDREN, "glossary_terms"):
        op.drop_table(table)
