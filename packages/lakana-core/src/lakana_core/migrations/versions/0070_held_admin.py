"""Who has ever held instance ADMIN: the mark that keeps administrators' recovery with the operator.

Revision ID: e8a2c4f6b0d3
Revises: d7f1b3c5e9a2
Create Date: 2026-11-09 09:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e8a2c4f6b0d3"
down_revision: str | Sequence[str] | None = "d7f1b3c5e9a2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_principals = sa.table(
    "principals",
    sa.column("id", sa.Uuid()),
    sa.column("name", sa.String()),
    sa.column("held_admin", sa.Boolean()),
)
_grants = sa.table(
    "grants",
    sa.column("principal_id", sa.Uuid()),
    sa.column("group_id", sa.Uuid()),
    sa.column("role", sa.String()),
    sa.column("kind", sa.String()),
)
_groups = sa.table("groups", sa.column("id", sa.Uuid()), sa.column("name", sa.String()))
_memberships = sa.table(
    "group_memberships", sa.column("group_id", sa.Uuid()), sa.column("principal_id", sa.Uuid())
)
_audit = sa.table(
    "audit_events",
    sa.column("event_type", sa.String()),
    sa.column("securable_kind", sa.String()),
    sa.column("detail", sa.JSON()),
)


def _admin_history(connection: sa.Connection) -> tuple[set[object], set[str]]:
    """Find everyone the database shows as having held instance ADMIN, by id and by name.

    Parameters
    ----------
    connection
        The migration's connection.

    Returns
    -------
    tuple of (set, set of str)
        Principal ids (direct grants, revoked or not, and current members of a group that ever
        had one) and principal names (from `GRANT_CREATED` and `GROUP_MEMBER_ADDED` events
        that concern an instance ADMIN grant or a group that had one).
    """
    admin = sa.and_(_grants.c.role == "ADMIN", _grants.c.kind == "INSTANCE")
    ids = set(
        connection.execute(
            sa.select(_grants.c.principal_id).where(admin, _grants.c.principal_id.is_not(None))
        ).scalars()
    )
    group_ids = set(
        connection.execute(
            sa.select(_grants.c.group_id).where(admin, _grants.c.group_id.is_not(None))
        ).scalars()
    )
    if group_ids:
        ids.update(
            connection.execute(
                sa.select(_memberships.c.principal_id).where(_memberships.c.group_id.in_(group_ids))
            ).scalars()
        )
    group_names = set(
        connection.execute(sa.select(_groups.c.name).where(_groups.c.id.in_(group_ids))).scalars()
    )
    names: set[str] = set()
    created = connection.execute(
        sa.select(_audit.c.detail).where(
            _audit.c.event_type == "GRANT_CREATED", _audit.c.securable_kind == "INSTANCE"
        )
    ).scalars()
    for detail in created:
        if isinstance(detail, dict) and detail.get("role") == "ADMIN":
            if isinstance(detail.get("principal"), str):
                names.add(detail["principal"])
            if isinstance(detail.get("group"), str):
                group_names.add(detail["group"])
    added = connection.execute(
        sa.select(_audit.c.detail).where(_audit.c.event_type == "GROUP_MEMBER_ADDED")
    ).scalars()
    for detail in added:
        if (
            isinstance(detail, dict)
            and detail.get("group") in group_names
            and isinstance(detail.get("principal"), str)
        ):
            names.add(detail["principal"])
    return ids, names


def upgrade() -> None:
    """Upgrade schema.

    Adds `principals.held_admin` (false) and sets it for everyone the database shows as having
    held instance ADMIN: a direct grant, revoked or not; a current member of a group that had
    one; and a name an audit event shows receiving one, directly or by joining such a group.
    A name that was reused may mark someone too many, never too few: the cost is that the
    server operator recovers them.
    """
    with op.batch_alter_table("principals") as batch:
        batch.add_column(
            sa.Column("held_admin", sa.Boolean(), server_default=sa.false(), nullable=False)
        )
    connection = op.get_bind()
    ids, names = _admin_history(connection)
    if ids:
        connection.execute(
            _principals.update().where(_principals.c.id.in_(ids)).values(held_admin=True)
        )
    if names:
        connection.execute(
            _principals.update().where(_principals.c.name.in_(names)).values(held_admin=True)
        )


def downgrade() -> None:
    """Downgrade schema.

    Drops the mark; the previous release checks only who holds ADMIN now.
    """
    with op.batch_alter_table("principals") as batch:
        batch.drop_column("held_admin")
