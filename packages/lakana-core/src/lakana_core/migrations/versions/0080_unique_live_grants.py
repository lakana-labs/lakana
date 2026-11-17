"""unique_live_grants: at most one live grant per subject, role or action, kind and target.

Revision ID: 15081774c52d
Revises: c4e8a1f2d937
Create Date: 2026-11-16 12:00:00.000000

"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "15081774c52d"
down_revision: str | Sequence[str] | None = "c4e8a1f2d937"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TARGETS: dict[str, tuple[str, ...]] = {
    "INSTANCE": (),
    "ENVIRONMENT": ("environment_id",),
    "NAMESPACE": ("environment_id", "namespace"),
    "TABLE": ("table_id",),
}
"""The columns that bind a grant of each kind to its target, as `Grant`'s indexes have them."""

_SUBJECT_AND_ACCESS = (
    "coalesce(principal_id, group_id)",
    "coalesce(role, '')",
    "coalesce(action, '')",
)

_grants = sa.table(
    "grants",
    sa.column("id", sa.Uuid()),
    sa.column("principal_id", sa.Uuid()),
    sa.column("group_id", sa.Uuid()),
    sa.column("role", sa.String()),
    sa.column("action", sa.String()),
    sa.column("kind", sa.String()),
    sa.column("environment", sa.String()),
    sa.column("namespace", sa.String()),
    sa.column("table", sa.String()),
    sa.column("environment_id", sa.Uuid()),
    sa.column("table_id", sa.Uuid()),
    sa.column("granted_at", sa.DateTime(timezone=True)),
    sa.column("revoked_at", sa.DateTime(timezone=True)),
)
_audit = sa.table(
    "audit_events",
    sa.column("id", sa.Uuid()),
    sa.column("occurred_at", sa.DateTime(timezone=True)),
    sa.column("principal_id", sa.Uuid()),
    sa.column("event_type", sa.String()),
    sa.column("securable_kind", sa.String()),
    sa.column("securable_environment", sa.String()),
    sa.column("securable_namespace", sa.String()),
    sa.column("securable_table", sa.String()),
    sa.column("detail", sa.JSON()),
)


def _revoke_twins(connection: sa.Connection) -> None:
    """Keep the oldest live grant of each set of identical ones, and revoke the others.

    Identical as `lakana_core.grants.duplicates` defines it: the subject, the role or action,
    the kind, and the ids (or the namespace) that bind the target. A grant whose target id is
    gone is never a twin. Each revoked copy gets a `GRANT_REVOKED` event without an actor, with
    `reason` ``duplicate`` and the kept grant's id in `kept_grant_id`. The access is the same
    before and after: every copy gave exactly what the kept one gives.

    Parameters
    ----------
    connection
        The migration's connection.
    """
    live = connection.execute(
        sa.select(_grants)
        .where(_grants.c.revoked_at.is_(None))
        .order_by(_grants.c.granted_at, _grants.c.id)
    ).all()
    kept: dict[tuple[object, ...], object] = {}
    now = datetime.now(UTC)
    for row in live:
        target = tuple(getattr(row, name) for name in _TARGETS[row.kind])
        if any(value is None for value in target):
            continue
        key = (row.kind, row.principal_id or row.group_id, row.role, row.action, *target)
        if key not in kept:
            kept[key] = row.id
            continue
        connection.execute(_grants.update().where(_grants.c.id == row.id).values(revoked_at=now))
        instance = row.kind == "INSTANCE"
        connection.execute(
            _audit.insert().values(
                id=uuid.uuid4(),
                occurred_at=now,
                principal_id=None,
                event_type="GRANT_REVOKED",
                securable_kind=row.kind,
                securable_environment=None if instance else row.environment,
                securable_namespace=row.namespace,
                securable_table=row.table,
                detail={
                    "grant_id": str(row.id),
                    "principal_id": str(row.principal_id) if row.principal_id else None,
                    "group_id": str(row.group_id) if row.group_id else None,
                    "role": row.role,
                    "action": row.action,
                    "reason": "duplicate",
                    "kept_grant_id": str(kept[key]),
                },
            )
        )


def upgrade() -> None:
    """Upgrade schema.

    Revokes every live grant that repeats an older live one (#673), then adds one unique index
    per kind over the live grants, `ux_grants_live_<kind>`, so that the database refuses the
    next copy. No grant gives less or more access than before.
    """
    _revoke_twins(op.get_bind())
    for kind, columns in _TARGETS.items():
        where = sa.text(f"revoked_at IS NULL AND kind = '{kind}'")
        op.create_index(
            f"ux_grants_live_{kind.lower()}",
            "grants",
            [sa.text(e) for e in (*_SUBJECT_AND_ACCESS, *columns)],
            unique=True,
            sqlite_where=where,
            postgresql_where=where,
        )


def downgrade() -> None:
    """Downgrade schema.

    Drops the four indexes. The copies the upgrade revoked stay revoked, as their audit events
    say: a revoked grant gives no access, and the kept one gives the same.
    """
    for kind in reversed(_TARGETS):
        op.drop_index(f"ux_grants_live_{kind.lower()}", table_name="grants")
