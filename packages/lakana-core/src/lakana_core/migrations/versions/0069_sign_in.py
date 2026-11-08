"""Signing in to the web interface: links, browser sessions, passkeys and their settings.

Revision ID: d7f1b3c5e9a2
Revises: c4e6a8b0d2f1
Create Date: 2026-11-08 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pydantic_settings import BaseSettings, SettingsConfigDict

# revision identifiers, used by Alembic.
revision: str = "d7f1b3c5e9a2"
down_revision: str | Sequence[str] | None = "c4e6a8b0d2f1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LINK_SESSION_FK = "fk_sign_in_links_session_id_browser_sessions"


class _BootstrapName(BaseSettings):
    """The bootstrap principal's name, read the way the server reads it.

    Parameters
    ----------
    bootstrap_admin
        `LAKANA_BOOTSTRAP_ADMIN`, ``admin`` by default.
    """

    model_config = SettingsConfigDict(env_prefix="LAKANA_", extra="ignore")

    bootstrap_admin: str = "admin"


def _fk(table: str, column: str, referred: str) -> str:
    """Name a foreign key the way the models' naming convention does.

    Parameters
    ----------
    table
        The constrained table.
    column
        The constrained column.
    referred
        The referred table.

    Returns
    -------
    str
        ``fk_<table>_<column>_<referred>``.
    """
    return f"fk_{table}_{column}_{referred}"


def upgrade() -> None:
    """Upgrade schema.

    Six new, empty tables, a nullable WebAuthn user handle and display name on principals, and
    the `bootstrap` mark on personal access tokens. The mark is set on every unrevoked PAT
    named ``bootstrap`` of the bootstrap principal (`LAKANA_BOOTSTRAP_ADMIN`): the name
    `bootstrap_if_needed` has always given the token it mints, so an upgraded operator's token
    keeps counting as the operator's. A PAT named ``bootstrap`` of any other principal does
    not.
    """
    with op.batch_alter_table("principals") as batch:
        batch.add_column(sa.Column("webauthn_handle", sa.LargeBinary(length=32), nullable=True))
        batch.add_column(sa.Column("display_name", sa.String(length=255), nullable=True))
        batch.create_unique_constraint("uq_principals_webauthn_handle", ["webauthn_handle"])
    with op.batch_alter_table("personal_access_tokens") as batch:
        batch.add_column(
            sa.Column("bootstrap", sa.Boolean(), server_default=sa.false(), nullable=False)
        )
    pats = sa.table(
        "personal_access_tokens",
        sa.column("bootstrap", sa.Boolean()),
        sa.column("name", sa.String()),
        sa.column("revoked_at", sa.DateTime()),
        sa.column("principal_id", sa.Uuid()),
    )
    principals = sa.table("principals", sa.column("id", sa.Uuid()), sa.column("name", sa.String()))
    op.execute(
        pats.update()
        .where(
            pats.c.name == "bootstrap",
            pats.c.revoked_at.is_(None),
            pats.c.principal_id.in_(
                sa.select(principals.c.id).where(
                    principals.c.name == _BootstrapName().bootstrap_admin
                )
            ),
        )
        .values(bootstrap=sa.true())
    )

    op.create_table(
        "sign_in_links",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("public_id", sa.String(length=32), nullable=False),
        sa.Column("secret_hash", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("principal_id", sa.Uuid(), nullable=True),
        sa.Column("invite_name", sa.String(length=255), nullable=True),
        sa.Column("invite_display_name", sa.String(length=255), nullable=True),
        sa.Column("reserved_name", sa.String(length=255), nullable=True),
        sa.Column("issued_by", sa.Uuid(), nullable=True),
        sa.Column("source_pat_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("withdrawn_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("session_id", sa.Uuid(), nullable=True),
        sa.Column("used_peer", sa.String(length=64), nullable=True),
        sa.Column("used_user_agent", sa.String(length=255), nullable=True),
        sa.ForeignKeyConstraint(
            ["principal_id"],
            ["principals.id"],
            name=_fk("sign_in_links", "principal_id", "principals"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["issued_by"],
            ["principals.id"],
            name=_fk("sign_in_links", "issued_by", "principals"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["source_pat_id"],
            ["personal_access_tokens.id"],
            name=_fk("sign_in_links", "source_pat_id", "personal_access_tokens"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_sign_in_links"),
        sa.UniqueConstraint("public_id", name="uq_sign_in_links_public_id"),
        sa.UniqueConstraint("reserved_name", name="uq_sign_in_links_reserved_name"),
    )
    op.create_index(
        "ix_sign_in_links_principal_id", "sign_in_links", ["principal_id"], unique=False
    )
    op.create_table(
        "browser_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("key_id", sa.String(length=32), nullable=False),
        sa.Column("secret_hash", sa.String(length=64), nullable=False),
        sa.Column("principal_id", sa.Uuid(), nullable=False),
        sa.Column("via", sa.String(length=16), nullable=False),
        sa.Column("link_id", sa.Uuid(), nullable=True),
        sa.Column("source_pat_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("idle_seconds", sa.Integer(), nullable=False),
        sa.Column("absolute_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_touch_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end_reason", sa.String(length=32), nullable=True),
        sa.Column("peer", sa.String(length=64), nullable=False),
        sa.Column("user_agent", sa.String(length=255), nullable=False),
        sa.ForeignKeyConstraint(
            ["principal_id"],
            ["principals.id"],
            name=_fk("browser_sessions", "principal_id", "principals"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["link_id"],
            ["sign_in_links.id"],
            name=_fk("browser_sessions", "link_id", "sign_in_links"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["source_pat_id"],
            ["personal_access_tokens.id"],
            name=_fk("browser_sessions", "source_pat_id", "personal_access_tokens"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_browser_sessions"),
        sa.UniqueConstraint("key_id", name="uq_browser_sessions_key_id"),
    )
    op.create_index(
        "ix_browser_sessions_principal_id", "browser_sessions", ["principal_id"], unique=False
    )
    with op.batch_alter_table("sign_in_links") as batch:
        batch.create_foreign_key(
            _LINK_SESSION_FK, "browser_sessions", ["session_id"], ["id"], ondelete="SET NULL"
        )
    op.create_table(
        "passkeys",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("principal_id", sa.Uuid(), nullable=False),
        sa.Column("credential_id", sa.String(length=1366), nullable=False),
        sa.Column("public_key", sa.LargeBinary(), nullable=False),
        sa.Column("sign_count", sa.Integer(), nullable=False),
        sa.Column("rp_id", sa.String(length=255), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("backup_eligible", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_session_id", sa.Uuid(), nullable=True),
        sa.Column("created_user_agent", sa.String(length=255), nullable=False),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("disabled_reason", sa.String(length=32), nullable=True),
        sa.ForeignKeyConstraint(
            ["principal_id"],
            ["principals.id"],
            name=_fk("passkeys", "principal_id", "principals"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_session_id"],
            ["browser_sessions.id"],
            name=_fk("passkeys", "created_session_id", "browser_sessions"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_passkeys"),
        sa.UniqueConstraint("credential_id", name="uq_passkeys_credential_id"),
    )
    op.create_index("ix_passkeys_principal_id", "passkeys", ["principal_id"], unique=False)
    op.create_table(
        "webauthn_challenges",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("challenge", sa.LargeBinary(length=32), nullable=False),
        sa.Column("purpose", sa.String(length=16), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=True),
        sa.Column("principal_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["browser_sessions.id"],
            name=_fk("webauthn_challenges", "session_id", "browser_sessions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["principal_id"],
            ["principals.id"],
            name=_fk("webauthn_challenges", "principal_id", "principals"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_webauthn_challenges"),
    )
    op.create_table(
        "sign_in_notices",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("recipient_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=True),
        sa.Column("detail", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["recipient_id"],
            ["principals.id"],
            name=_fk("sign_in_notices", "recipient_id", "principals"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["subject_id"],
            ["principals.id"],
            name=_fk("sign_in_notices", "subject_id", "principals"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_sign_in_notices"),
    )
    op.create_index(
        "ix_sign_in_notices_recipient_id", "sign_in_notices", ["recipient_id"], unique=False
    )
    op.create_table(
        "instance_settings",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(
            ["updated_by"],
            ["principals.id"],
            name=_fk("instance_settings", "updated_by", "principals"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("key", name="pk_instance_settings"),
    )


def downgrade() -> None:
    """Downgrade schema.

    Drops every sign-in table and the three new columns: sessions, links and passkeys are gone,
    and the previous release signs in with tokens only.
    """
    op.drop_table("instance_settings")
    op.drop_index("ix_sign_in_notices_recipient_id", table_name="sign_in_notices")
    op.drop_table("sign_in_notices")
    op.drop_table("webauthn_challenges")
    op.drop_index("ix_passkeys_principal_id", table_name="passkeys")
    op.drop_table("passkeys")
    with op.batch_alter_table("sign_in_links") as batch:
        batch.drop_constraint(_LINK_SESSION_FK, type_="foreignkey")
    op.drop_index("ix_browser_sessions_principal_id", table_name="browser_sessions")
    op.drop_table("browser_sessions")
    op.drop_index("ix_sign_in_links_principal_id", table_name="sign_in_links")
    op.drop_table("sign_in_links")
    with op.batch_alter_table("personal_access_tokens") as batch:
        batch.drop_column("bootstrap")
    with op.batch_alter_table("principals") as batch:
        batch.drop_constraint("uq_principals_webauthn_handle", type_="unique")
        batch.drop_column("display_name")
        batch.drop_column("webauthn_handle")
