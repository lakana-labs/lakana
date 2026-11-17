"""connection_tests: index (connection_id, created_at) so a connection's newest test is cheap.

The single-column index on `connection_id` goes: the new index's leading column serves every
lookup it did (the foreign key's cascade, the read of a connection's open test), and the pair
also serves the rank of a connection's tests by creation time without a sort.

Revision ID: b6d2f8a4c913
Revises: e9f3a6b2d847
Create Date: 2026-11-17 10:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b6d2f8a4c913"
down_revision: str | Sequence[str] | None = "e9f3a6b2d847"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema.

    Replace the index on `connection_id` with one on `(connection_id, created_at)`.
    """
    op.drop_index(op.f("ix_connection_tests_connection_id"), table_name="connection_tests")
    op.create_index(
        "ix_connection_tests_connection_id_created_at",
        "connection_tests",
        ["connection_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema.

    Restore the index on `connection_id` alone.
    """
    op.drop_index("ix_connection_tests_connection_id_created_at", table_name="connection_tests")
    op.create_index(
        op.f("ix_connection_tests_connection_id"),
        "connection_tests",
        ["connection_id"],
        unique=False,
    )
