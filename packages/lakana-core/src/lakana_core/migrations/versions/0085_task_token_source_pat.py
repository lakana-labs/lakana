"""task_token_source_pat: the personal access token a task token was minted with.

`task_tokens.source_pat_id` names the PAT behind a token minted over REST, so the token works
only while that PAT does (`lakana_core.identity.task_tokens.resolve_task_token_scope`). The
foreign key deletes a task token with its PAT. Existing tokens keep `NULL`: nothing recorded
which PAT minted them, so they stay valid until their own `expires_at`, at most
`LAKANA_TASK_TOKENS__MAX_TTL_SECONDS` after they were minted.

Revision ID: 532b5154629b
Revises: f6b1d4e8a273
Create Date: 2026-11-18 10:00:00.000000

"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "532b5154629b"
down_revision: str | Sequence[str] | None = "f6b1d4e8a273"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_task_tokens = sa.table(
    "task_tokens",
    sa.column("id", sa.Uuid()),
    sa.column("principal_id", sa.Uuid()),
    sa.column("key_id", sa.String()),
    sa.column("environment", sa.String()),
    sa.column("asset_scope", sa.JSON()),
    sa.column("expires_at", sa.DateTime(timezone=True)),
    sa.column("revoked_at", sa.DateTime(timezone=True)),
    sa.column("source_pat_id", sa.Uuid()),
)
_pats = sa.table(
    "personal_access_tokens",
    sa.column("id", sa.Uuid()),
    sa.column("principal_id", sa.Uuid()),
    sa.column("key_id", sa.String()),
    sa.column("expires_at", sa.DateTime(timezone=True)),
    sa.column("revoked_at", sa.DateTime(timezone=True)),
)
_principals = sa.table(
    "principals",
    sa.column("id", sa.Uuid()),
    sa.column("name", sa.String()),
    sa.column("is_active", sa.Boolean()),
)
# jscpd:ignore-start -- a migration spells out its tables; it must never share code with another
# one, which a later change could alter under an applied revision.
_audit = sa.table(
    "audit_events",
    sa.column("id", sa.Uuid()),
    sa.column("occurred_at", sa.DateTime(timezone=True)),
    sa.column("principal_id", sa.Uuid()),
    sa.column("event_type", sa.String()),
    sa.column("detail", sa.JSON()),
)
# jscpd:ignore-end


def upgrade() -> None:
    """Upgrade schema.

    Adds the nullable column with its foreign key (``ON DELETE CASCADE``) and an index, which
    serves the revocation of a PAT's task tokens. No row changes.
    """
    with op.batch_alter_table("task_tokens") as batch_op:
        batch_op.add_column(sa.Column("source_pat_id", sa.Uuid(), nullable=True))
        batch_op.create_foreign_key(
            op.f("fk_task_tokens_source_pat_id_personal_access_tokens"),
            "personal_access_tokens",
            ["source_pat_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch_op.create_index(op.f("ix_task_tokens_source_pat_id"), ["source_pat_id"])


def _revoke_orphaned(connection: sa.Connection) -> None:
    """Revoke every live task token whose PAT no longer works.

    Without the column the previous release judges a task token by its own row alone, so a
    token of a PAT that has expired, or whose owner is deactivated, would work again. Each gets
    `revoked_at` and a `TASK_TOKEN_REVOKED` event without an actor, ``reason: pat_ended``.
    A token whose PAT still works stays live, as it was before this migration. A token past its
    own `expires_at` is dead either way, so it is left alone and gets no event.

    Parameters
    ----------
    connection
        The migration's connection.
    """
    now = datetime.now(UTC)
    rows = connection.execute(
        sa.select(
            _task_tokens.c.id,
            _task_tokens.c.key_id,
            _task_tokens.c.environment,
            _task_tokens.c.asset_scope,
            _pats.c.key_id.label("pat_key_id"),
            _pats.c.expires_at,
            _pats.c.revoked_at.label("pat_revoked_at"),
            _principals.c.is_active,
        )
        .join(_pats, _pats.c.id == _task_tokens.c.source_pat_id)
        .join(_principals, _principals.c.id == _pats.c.principal_id)
        .where(_task_tokens.c.revoked_at.is_(None), _task_tokens.c.expires_at > now)
    ).all()
    for row in rows:
        expires_at = row.expires_at
        if expires_at is not None and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        expired = expires_at is not None and expires_at <= now
        if row.pat_revoked_at is None and not expired and row.is_active:
            continue
        connection.execute(
            _task_tokens.update().where(_task_tokens.c.id == row.id).values(revoked_at=now)
        )
        connection.execute(
            _audit.insert().values(
                id=uuid.uuid4(),
                occurred_at=now,
                principal_id=None,
                event_type="TASK_TOKEN_REVOKED",
                detail={
                    "key_id": row.key_id,
                    "environment": row.environment,
                    "asset_scope": list(row.asset_scope),
                    "reason": "pat_ended",
                    "pat_key_id": row.pat_key_id,
                },
            )
        )


def downgrade() -> None:
    """Downgrade schema.

    Revokes the live task tokens whose PAT no longer works first (`_revoke_orphaned`), then
    drops the index, the foreign key and the column.
    """
    _revoke_orphaned(op.get_bind())
    with op.batch_alter_table("task_tokens") as batch_op:
        batch_op.drop_index(op.f("ix_task_tokens_source_pat_id"))
        batch_op.drop_constraint(
            op.f("fk_task_tokens_source_pat_id_personal_access_tokens"), type_="foreignkey"
        )
        batch_op.drop_column("source_pat_id")
