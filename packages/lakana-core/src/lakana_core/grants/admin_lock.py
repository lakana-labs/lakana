"""One lock around every count of administrators that a change may lower (#724).

A change that can take ADMIN away from someone (revoking an instance ADMIN grant, held directly or
through a group; removing a member from a group that holds one; deactivating a principal; an
access apply) counts the active administrators inside its own transaction and refuses when none
would remain (`lakana_core.grants.service.assert_admin_headroom`). On PostgreSQL that count reads
committed rows and locks none, so two such changes at once could each see the other's
administrator, both commit, and leave the instance with none. `lock_admin_count` makes them take
turns: the second waits for the first's commit and then counts what it left.

The lock comes before any read that decides whether to count at all. `GroupService.remove_member`
asks whether the group holds ADMIN only after taking it, and takes it for every group: a grant to
the group may commit right after that question, and a count that relies on the member must then
wait for the removal's commit. Changes that raise the count (a grant, a new member, a
reactivation) take no lock: a count never sees them before they commit, so they can only make a
check refuse, never let one through.

Any later rule of the same kind, a count a change may lower and must never bring to zero, counts
under this lock too, so that every such check sees the others' commits.

Order: a change takes the access lock (`lakana_core.access.managed.lock_access`) first, when it
takes it at all, and this lock second, before it counts. An apply holds the access lock alone and
takes this one inside it; taking them the other way round could deadlock against an apply.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

ADMIN_COUNT_LOCK_KEY = int.from_bytes(b"lkadmins", "big", signed=True)
"""The Postgres advisory lock key of the administrator count, apart from the access lock's."""


def lock_admin_count(session: Session) -> None:
    """Take the administrator-count lock, exclusively, until the transaction ends (Postgres only).

    Call it before the first count of a change that may lower the number of administrators. Off
    Postgres nothing is taken, as with the access lock.

    Parameters
    ----------
    session
        The change's session.
    """
    if session.get_bind().dialect.name != "postgresql":
        return
    session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": ADMIN_COUNT_LOCK_KEY})
