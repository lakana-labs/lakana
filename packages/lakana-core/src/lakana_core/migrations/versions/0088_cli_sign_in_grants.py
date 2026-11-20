"""cli_sign_in_grants: the one-time grants of `lakana login` through a browser (#32).

One row per loopback or device sign-in of the command line: the SHA-256 of its codes, the PKCE
challenge, the loopback port, the cleaned host name, the address a device grant was started
from, who approved it from which browser session, and the token it was exchanged for. A row
works once and expires after 2 minutes (loopback) or 10 minutes (device).

Revision ID: 7c3e91a0b5d2
Revises: 431d39830234
Create Date: 2026-11-20 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7c3e91a0b5d2"
down_revision: str | Sequence[str] | None = "431d39830234"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEXED = ("principal_id", "session_id", "expires_at")


def upgrade() -> None:
    """Upgrade schema.

    One empty table, with an index on its keys and on its expiry. No row changes.
    """
    op.create_table(
        "cli_sign_in_grants",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("host", sa.String(length=64), nullable=False),
        sa.Column("code_hash", sa.String(length=64), nullable=True),
        sa.Column("code_challenge", sa.String(length=64), nullable=True),
        sa.Column("port", sa.Integer(), nullable=True),
        sa.Column("device_code_hash", sa.String(length=64), nullable=True),
        sa.Column("user_code_hash", sa.String(length=64), nullable=True),
        sa.Column("principal_id", sa.Uuid(), nullable=True),
        sa.Column("session_id", sa.Uuid(), nullable=True),
        sa.Column("requested_from", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("denied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_polled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("pat_id", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(
            ["principal_id"],
            ["principals.id"],
            name=op.f("fk_cli_sign_in_grants_principal_id_principals"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["browser_sessions.id"],
            name=op.f("fk_cli_sign_in_grants_session_id_browser_sessions"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["pat_id"],
            ["personal_access_tokens.id"],
            name=op.f("fk_cli_sign_in_grants_pat_id_personal_access_tokens"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cli_sign_in_grants")),
        sa.UniqueConstraint("code_hash", name=op.f("uq_cli_sign_in_grants_code_hash")),
        sa.UniqueConstraint(
            "device_code_hash", name=op.f("uq_cli_sign_in_grants_device_code_hash")
        ),
        sa.UniqueConstraint("user_code_hash", name=op.f("uq_cli_sign_in_grants_user_code_hash")),
    )
    for column in _INDEXED:
        op.create_index(
            op.f(f"ix_cli_sign_in_grants_{column}"), "cli_sign_in_grants", [column], unique=False
        )


def downgrade() -> None:
    """Downgrade schema.

    Drops the table: a sign-in of the command line waiting in a browser has to start again. The
    tokens issued stay.
    """
    for column in reversed(_INDEXED):
        op.drop_index(op.f(f"ix_cli_sign_in_grants_{column}"), table_name="cli_sign_in_grants")
    op.drop_table("cli_sign_in_grants")
