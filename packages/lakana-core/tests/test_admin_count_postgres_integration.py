"""Two removals of administrators at once on a real PostgreSQL: one is refused (#724).

Each removal runs in its own session and thread and waits, before its commit, at a barrier for
the other. Without one lock around the count both see two administrators, both pass the barrier
and both commit, which leaves none. With the lock the second waits for the first's commit, so
the first times out at the barrier and commits, and the second counts again and is refused.
"""

from __future__ import annotations

import contextlib
import threading
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from sqlalchemy import update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from testcontainers.postgres import PostgresContainer

from lakana_core import migrate
from lakana_core.auth import AllowAllAuthorization, PrincipalRef, SecurableKind
from lakana_core.config import DatabaseConfig
from lakana_core.db import create_engine
from lakana_core.errors import ConflictError
from lakana_core.grants.roles import Role
from lakana_core.grants.service import LAST_ADMIN_MESSAGE, GrantService, active_instance_admin_ids
from lakana_core.identity.groups import GroupService
from lakana_core.identity.principals import PrincipalService
from lakana_core.models import Grant, Principal, PrincipalType

pytestmark = pytest.mark.integration

_BARRIER_WAIT = 2.0
"""Seconds a removal waits before its commit for the other to reach the same point."""
_DONE = "done"


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    """Start one Postgres, migrated to head.

    Yields
    ------
    Engine
        An engine on it.
    """
    with PostgresContainer("postgres:16-alpine", driver="pg8000") as pg:
        url = pg.get_connection_url()
        migrate.upgrade(url)
        engine = create_engine(DatabaseConfig(url=url))
        yield engine
        engine.dispose()


@pytest.fixture
def actor(engine: Engine) -> PrincipalRef:
    """Give the acting principal a row and revoke the ADMIN grants an earlier test left.

    Parameters
    ----------
    engine
        The engine.

    Returns
    -------
    PrincipalRef
        The actor (no ADMIN role of its own; the services run under allow-all).
    """
    name = f"actor-{uuid.uuid4().hex[:8]}"
    ref = PrincipalRef(id=uuid.uuid4(), name=name, type=PrincipalType.USER)
    with Session(engine) as session:
        session.add(Principal(id=ref.id, type=ref.type, name=ref.name, is_active=True))
        session.flush()
        session.execute(
            update(Grant)
            .where(
                Grant.role == Role.ADMIN,
                Grant.kind == SecurableKind.INSTANCE,
                Grant.revoked_at.is_(None),
            )
            .values(revoked_at=datetime.now(UTC), revoked_by=ref.id)
        )
        session.commit()
    return ref


@dataclass(frozen=True)
class _Admin:
    """One administrator of a race and how ADMIN reaches them.

    Attributes
    ----------
    name
        The principal's name.
    grant_id
        The ADMIN grant that covers them: their own, or their group's.
    group
        Their group's name when ADMIN comes through one, else `None`.
    """

    name: str
    grant_id: uuid.UUID
    group: str | None


def _make_admin(session: Session, actor: PrincipalRef, *, via: str) -> _Admin:
    """Create a principal holding instance ADMIN directly or through a group of its own.

    Parameters
    ----------
    session
        Database session; committed.
    actor
        The acting principal.
    via
        ``direct`` or ``group``.

    Returns
    -------
    _Admin
        The administrator.
    """
    allow = AllowAllAuthorization()
    name = f"admin-{uuid.uuid4().hex[:8]}"
    PrincipalService(session, allow).create_principal(actor, name=name, type=PrincipalType.USER)
    grants = GrantService(session, allow)
    if via == "direct":
        grant = grants.grant(actor, principal=name, role=Role.ADMIN, kind=SecurableKind.INSTANCE)
        return _Admin(name=name, grant_id=grant.id, group=None)
    group = f"admins-{uuid.uuid4().hex[:8]}"
    groups = GroupService(session, allow)
    groups.create_group(actor, name=group)
    groups.add_member(actor, group, name)
    grant = grants.grant(actor, group=group, role=Role.ADMIN, kind=SecurableKind.INSTANCE)
    return _Admin(name=name, grant_id=grant.id, group=group)


def _removal(kind: str, admin: _Admin, actor: PrincipalRef) -> Callable[[Session], None]:
    """Build one removal that leaves its change uncommitted.

    Parameters
    ----------
    kind
        ``revoke`` (the grant that covers them), ``remove`` (them from their group) or
        ``deactivate`` (them).
    admin
        The administrator it removes.
    actor
        The acting principal.

    Returns
    -------
    Callable[[Session], None]
        The removal, run in the session it is given.
    """

    def run(session: Session) -> None:
        """Make the change in `session` without committing it.

        Parameters
        ----------
        session
            The removal's own session.
        """
        allow = AllowAllAuthorization()
        if kind == "revoke":
            GrantService(session, allow).revoke(actor, admin.grant_id, commit=False)
        elif kind == "remove":
            assert admin.group is not None  # nosec B101 -- the case gives this admin a group
            GroupService(session, allow).remove_member(actor, admin.group, admin.name, commit=False)
        else:
            PrincipalService(session, allow).deactivate_principal(actor, admin.name, commit=False)

    return run


def _race(engine: Engine, removals: list[Callable[[Session], None]]) -> list[str]:
    """Run two removals at once, each waiting for the other before it commits.

    Parameters
    ----------
    engine
        The engine.
    removals
        The two removals.

    Returns
    -------
    list of str
        Per removal: ``done`` when it committed, else the message of what it raised.
    """
    barrier = threading.Barrier(len(removals))
    outcomes = [""] * len(removals)

    def run(index: int) -> None:
        """Run one removal and record its outcome.

        Parameters
        ----------
        index
            Which removal.
        """
        with Session(engine) as session:
            try:
                removals[index](session)
            except ConflictError as refused:
                session.rollback()
                outcomes[index] = str(refused)
                return
            except Exception as failed:  # reported through the outcome
                session.rollback()
                outcomes[index] = repr(failed)
                return
            with contextlib.suppress(threading.BrokenBarrierError):
                barrier.wait(timeout=_BARRIER_WAIT)
            session.commit()
            outcomes[index] = _DONE

    threads = [threading.Thread(target=run, args=(index,)) for index in range(len(removals))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not any(thread.is_alive() for thread in threads)
    return outcomes


@pytest.mark.parametrize(
    ("via", "kinds"),
    [
        (("direct", "direct"), ("revoke", "revoke")),
        (("group", "direct"), ("remove", "deactivate")),
        (("direct", "group"), ("revoke", "remove")),
        (("group", "group"), ("revoke", "revoke")),
        (("direct", "direct"), ("deactivate", "deactivate")),
    ],
    ids=[
        "grant-against-grant",
        "membership-against-deactivation",
        "grant-against-membership",
        "group-grant-against-group-grant",
        "deactivation-against-deactivation",
    ],
)
def test_one_of_two_concurrent_removals_is_refused(
    engine: Engine, actor: PrincipalRef, via: tuple[str, str], kinds: tuple[str, str]
) -> None:
    """Of two administrators each losing ADMIN at once, one stays: the other change is refused."""
    with Session(engine) as session:
        admins = [_make_admin(session, actor, via=way) for way in via]
        session.commit()
        assert len(active_instance_admin_ids(session)) == 2
    removals = [_removal(kind, admin, actor) for kind, admin in zip(kinds, admins, strict=True)]
    outcomes = _race(engine, removals)
    assert sorted(outcomes) == sorted([_DONE, LAST_ADMIN_MESSAGE])
    with Session(engine) as session:
        assert len(active_instance_admin_ids(session)) == 1
