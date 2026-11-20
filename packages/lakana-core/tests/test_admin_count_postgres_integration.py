"""Removals of administrators at once on a real PostgreSQL: none leaves the instance without one.

The first removal runs in the test's thread and stays uncommitted after its count. The second
runs in a thread of its own. Without the administrator-count lock it counts the first one's
administrator as still there and commits, which leaves none. With the lock it waits for the
first's transaction; the test sees it waiting in `pg_stat_activity` (``wait_event = 'advisory'``),
commits the first, and the second counts again and is refused. Neither outcome depends on timing.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from sqlalchemy import text, update
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

_DONE = "done"
_WAIT_LIMIT = 30.0
"""Seconds the test waits at most for the second change to block or finish."""


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


class _Second:
    """A change run in a thread of its own, which commits as soon as it is through.

    Parameters
    ----------
    engine
        The engine; the change gets a session of its own.
    change
        The change, run in that session without committing.
    """

    def __init__(self, engine: Engine, change: Callable[[Session], None]) -> None:
        """Start the change.

        Parameters
        ----------
        engine
            The engine.
        change
            The change.
        """
        self.outcome = ""
        self._engine = engine
        self._change = change
        self._thread = threading.Thread(target=self._run)
        self._thread.start()

    def _run(self) -> None:
        """Run the change and commit it, recording ``done`` or what it raised."""
        with Session(self._engine) as session:
            try:
                self._change(session)
                session.commit()
            except ConflictError as refused:
                session.rollback()
                self.outcome = str(refused)
                return
            except Exception as failed:  # reported through the outcome
                session.rollback()
                self.outcome = repr(failed)
                return
            self.outcome = _DONE

    def blocked_or_done(self) -> None:
        """Wait until the change waits on an advisory lock, or has finished.

        Raises
        ------
        AssertionError
            When neither happens within `_WAIT_LIMIT` seconds.
        """
        deadline = time.monotonic() + _WAIT_LIMIT
        while time.monotonic() < deadline:
            if not self._thread.is_alive():
                return
            with self._engine.connect() as connection:
                waiting = connection.execute(
                    text(
                        "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
                        " AND wait_event_type = 'Lock' AND wait_event = 'advisory'"
                    )
                ).scalar_one()
            if waiting:
                return
            time.sleep(0.02)
        raise AssertionError("the second change neither waited on the lock nor finished")

    def finish(self) -> str:
        """Wait for the change to end.

        Returns
        -------
        str
            ``done``, or the message of what it raised.
        """
        self._thread.join(timeout=_WAIT_LIMIT)
        assert not self._thread.is_alive()
        return self.outcome


def _admin_count(engine: Engine) -> int:
    """Count the active administrators as committed.

    Parameters
    ----------
    engine
        The engine.

    Returns
    -------
    int
        How many there are.
    """
    with Session(engine) as session:
        return len(active_instance_admin_ids(session))


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
    """Of two administrators each losing ADMIN at once, one stays: the second change is refused."""
    with Session(engine) as session:
        admins = [_make_admin(session, actor, via=way) for way in via]
        session.commit()
    assert _admin_count(engine) == 2
    first, second = (
        _removal(kind, admin, actor) for kind, admin in zip(kinds, admins, strict=True)
    )
    with Session(engine) as session:
        first(session)
        racing = _Second(engine, second)
        racing.blocked_or_done()
        session.commit()
    assert racing.finish() == LAST_ADMIN_MESSAGE
    assert _admin_count(engine) == 1


def test_a_member_removal_decides_under_the_lock_while_its_group_gains_admin(
    engine: Engine, actor: PrincipalRef
) -> None:
    """Review of #760: grant ADMIN to a group, remove its one member, revoke the other admin.

    A holds ADMIN directly; group G has no grant and one member, X. T1 grants ADMIN to G. T2
    removes X from G while T1 is uncommitted, so it sees no ADMIN on G. T1 commits. T3 revokes
    A's grant while T2 is uncommitted and would count X through G. T3 must wait for T2 and be
    refused, or the instance ends with no administrator.
    """
    allow = AllowAllAuthorization()
    suffix = uuid.uuid4().hex[:8]
    group, member = f"g-{suffix}", f"x-{suffix}"
    with Session(engine) as session:
        direct = _make_admin(session, actor, via="direct")
        PrincipalService(session, allow).create_principal(
            actor, name=member, type=PrincipalType.USER
        )
        groups = GroupService(session, allow)
        groups.create_group(actor, name=group)
        groups.add_member(actor, group, member)
        session.commit()
    assert _admin_count(engine) == 1
    with Session(engine) as granting, Session(engine) as removing:
        GrantService(granting, allow).grant(
            actor, group=group, role=Role.ADMIN, kind=SecurableKind.INSTANCE, commit=False
        )
        GroupService(removing, allow).remove_member(actor, group, member, commit=False)
        granting.commit()
        racing = _Second(engine, _removal("revoke", direct, actor))
        racing.blocked_or_done()
        removing.commit()
    assert racing.finish() == LAST_ADMIN_MESSAGE
    assert _admin_count(engine) == 1
