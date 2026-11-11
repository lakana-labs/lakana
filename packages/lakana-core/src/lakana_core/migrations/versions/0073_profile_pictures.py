"""profile_pictures: a person's profile picture, re-encoded on upload.

Revision ID: d4a7e2b9c613
Revises: c109929efd3f
Create Date: 2026-11-11 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d4a7e2b9c613"
down_revision: str | Sequence[str] | None = "c109929efd3f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema.

    One empty table: every existing person starts without a picture and is shown with their
    initials.
    """
    op.create_table(
        "profile_pictures",
        sa.Column("principal_id", sa.Uuid(), nullable=False),
        sa.Column("image", sa.LargeBinary(), nullable=False),
        sa.Column("etag", sa.String(length=64), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["principal_id"],
            ["principals.id"],
            name=op.f("fk_profile_pictures_principal_id_principals"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("principal_id", name=op.f("pk_profile_pictures")),
    )


def downgrade() -> None:
    """Downgrade schema.

    Drops the table: every picture is gone and people are shown with their initials again.
    """
    op.drop_table("profile_pictures")
