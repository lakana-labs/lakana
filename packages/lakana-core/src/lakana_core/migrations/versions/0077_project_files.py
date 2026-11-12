"""project files: the source map of each project version, and the texts it names.

Revision ID: b9f4e2a7c058
Revises: a7d3e9c1b406
Create Date: 2026-11-12 20:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b9f4e2a7c058"
down_revision: str | Sequence[str] | None = "a7d3e9c1b406"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema.

    Two empty tables: a version's files arrive with its next deploy, and a version deployed
    before has none (its "Defined in" answers `not_recorded`).

    - `project_file_texts`, one text per content digest.
    - `project_files`, one row per asset file of a version, keyed by the path's digest and
      indexed by the table name's digest, since neither name has a length limit and a btree
      entry has one. A text a file names cannot be deleted (`RESTRICT`); the files go with
      their version (`CASCADE`).
    """
    op.create_table(
        "project_file_texts",
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("sha256", name=op.f("pk_project_file_texts")),
    )
    op.create_table(
        "project_files",
        sa.Column("job_version_id", sa.Uuid(), nullable=False),
        sa.Column("path_sha256", sa.String(length=64), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("table_sha256", sa.String(length=64), nullable=False),
        sa.Column("table_name", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("line", sa.Integer(), nullable=False),
        sa.Column("bytes", sa.Integer(), nullable=False),
        sa.Column("gate_tables", sa.JSON(), nullable=False),
        sa.Column("gate_raw", sa.Boolean(), nullable=False),
        sa.Column("gate_version", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["job_version_id"],
            ["job_versions.id"],
            name=op.f("fk_project_files_job_version_id_job_versions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["sha256"],
            ["project_file_texts.sha256"],
            name=op.f("fk_project_files_sha256_project_file_texts"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("job_version_id", "path_sha256", name=op.f("pk_project_files")),
    )
    op.create_index(
        "ix_project_files_version_table",
        "project_files",
        ["job_version_id", "table_sha256"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema.

    Drops the source map; the next deploy of each commit after upgrading again records it.
    """
    op.drop_index("ix_project_files_version_table", table_name="project_files")
    op.drop_table("project_files")
    op.drop_table("project_file_texts")
