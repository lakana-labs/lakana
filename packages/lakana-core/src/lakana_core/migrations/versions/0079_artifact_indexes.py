"""artifact_indexes: the artifact rows other rows point at, and a principal's open uploads.

Revision ID: c4e8a1f2d937
Revises: ada48b713c93
Create Date: 2026-11-14 10:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4e8a1f2d937"
down_revision: str | Sequence[str] | None = "ada48b713c93"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEXES = (
    ("artifact_versions", "ix_artifact_versions_task_run_id", ["task_run_id"]),
    ("artifact_versions", "ix_artifact_versions_job_version_id", ["job_version_id"]),
    ("artifact_versions", "ix_artifact_versions_created_by", ["created_by"]),
    ("artifact_versions", "ix_artifact_versions_deleted_by", ["deleted_by"]),
    ("artifact_uploads", "ix_artifact_uploads_task_run_id", ["task_run_id"]),
    (
        "artifact_uploads",
        "ix_artifact_uploads_principal_id_expires_at",
        ["principal_id", "expires_at"],
    ),
)


def upgrade() -> None:
    """Upgrade schema.

    Six indexes. Each foreign key of the two tables that points at a principal, a task run or
    a job version gets one, so the `SET NULL`/`CASCADE` of a delete there finds its rows
    without a scan; `artifact_uploads.principal_id` is served by `(principal_id, expires_at)`,
    which is also the order the listing of a principal's open uploads reads. No row changes.
    """
    for table, name, columns in _INDEXES:
        op.create_index(name, table, columns, unique=False)


def downgrade() -> None:
    """Downgrade schema.

    Drops the six indexes.
    """
    for table, name, _columns in reversed(_INDEXES):
        op.drop_index(name, table_name=table)
