"""oidc_sign_in: a person's provider identity, and the OIDC sign-in's flow state (#32).

`principals` gains `oidc_issuer` and `oidc_subject` (unique together: one identity per account
and one account per identity), `oidc_email` and `oidc_name` (shown only) and
`last_provider_sign_in_at`. Every existing person stays unlinked (`NULL`): nothing changes for
them until they link.

`oidc_flows` holds one row per authorization request: the SHA-256 of its `state` and of the
browser's flow cookie, the nonce and the PKCE verifier, the app path to return to and the
callback's outcome. A row works once and expires after 10 minutes.

Revision ID: 431d39830234
Revises: 4d96fb277743
Create Date: 2026-11-20 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "431d39830234"
down_revision: str | Sequence[str] | None = "4d96fb277743"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEXED = ("principal_id", "session_id", "link_id", "expires_at")


def upgrade() -> None:
    """Upgrade schema.

    Five nullable columns and a unique constraint on `principals`, and one empty table with an
    index on each of its keys and on its expiry. No row changes.
    """
    with op.batch_alter_table("principals") as batch_op:
        batch_op.add_column(sa.Column("oidc_issuer", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("oidc_subject", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("oidc_email", sa.String(length=320), nullable=True))
        batch_op.add_column(sa.Column("oidc_name", sa.String(length=255), nullable=True))
        batch_op.add_column(
            sa.Column("last_provider_sign_in_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.create_unique_constraint(
            "uq_principals_oidc_identity", ["oidc_issuer", "oidc_subject"]
        )
    op.create_table(
        "oidc_flows",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("state_hash", sa.String(length=64), nullable=False),
        sa.Column("browser_hash", sa.String(length=64), nullable=False),
        sa.Column("nonce", sa.String(length=64), nullable=False),
        sa.Column("code_verifier", sa.String(length=128), nullable=False),
        sa.Column("purpose", sa.String(length=16), nullable=False),
        sa.Column("return_to", sa.String(length=2048), nullable=False),
        sa.Column("principal_id", sa.Uuid(), nullable=True),
        sa.Column("session_id", sa.Uuid(), nullable=True),
        sa.Column("link_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("called_back_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome", sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(
            ["principal_id"],
            ["principals.id"],
            name=op.f("fk_oidc_flows_principal_id_principals"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["browser_sessions.id"],
            name=op.f("fk_oidc_flows_session_id_browser_sessions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["link_id"],
            ["sign_in_links.id"],
            name=op.f("fk_oidc_flows_link_id_sign_in_links"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_oidc_flows")),
        sa.UniqueConstraint("state_hash", name=op.f("uq_oidc_flows_state_hash")),
        sa.UniqueConstraint("browser_hash", name=op.f("uq_oidc_flows_browser_hash")),
    )
    for column in _INDEXED:
        op.create_index(op.f(f"ix_oidc_flows_{column}"), "oidc_flows", [column], unique=False)


def downgrade() -> None:
    """Downgrade schema.

    Drops the flow table and the five columns: every link between a person and the provider is
    gone, so after an upgrade again linked people link anew.
    """
    for column in reversed(_INDEXED):
        op.drop_index(op.f(f"ix_oidc_flows_{column}"), table_name="oidc_flows")
    op.drop_table("oidc_flows")
    with op.batch_alter_table("principals") as batch_op:
        batch_op.drop_constraint("uq_principals_oidc_identity", type_="unique")
        batch_op.drop_column("last_provider_sign_in_at")
        batch_op.drop_column("oidc_name")
        batch_op.drop_column("oidc_email")
        batch_op.drop_column("oidc_subject")
        batch_op.drop_column("oidc_issuer")
