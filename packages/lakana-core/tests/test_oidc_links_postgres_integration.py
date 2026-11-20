"""Linking at once on a real PostgreSQL: the break-glass count and one account per identity (#32).

The first change runs in the test's thread and stays uncommitted after its count. The second runs
in a thread of its own and takes the administrator-count lock
(`lakana_core.grants.admin_lock.lock_admin_count`) before it counts, so it waits for the first
one's transaction; the test sees it waiting in `pg_stat_activity` (``wait_event = 'advisory'``),
commits the first, and the second counts again. Neither outcome depends on timing.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from testcontainers.postgres import PostgresContainer

from lakana_core import migrate
from lakana_core.auth import AllowAllAuthorization, PrincipalRef, SecurableKind
from lakana_core.config import DatabaseConfig
from lakana_core.db import create_engine
from lakana_core.errors import ConflictError, OidcSignInRefused
from lakana_core.grants.break_glass import LAST_UNLINKED_ADMIN_MESSAGE
from lakana_core.grants.roles import Role
from lakana_core.grants.service import GrantService
from lakana_core.identity.oidc_flows import ProviderIdentity
from lakana_core.identity.oidc_links import OidcLinkService, unlinked_admin_count
from lakana_core.identity.principals import PrincipalService
from lakana_core.models import Grant, Passkey, Principal, PrincipalType

pytestmark = pytest.mark.integration

_DONE = "done"
_WAIT_LIMIT = 30.0
"""Seconds the test waits at most for the second change to block or finish."""

_ISSUER = "https://idp.example.com/realms/lakana"


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


def _person(session: Session, actor: PrincipalRef, *, admin: bool) -> Principal:
    """Create a person with a working passkey, optionally an administrator; committed.

    Parameters
    ----------
    session
        Database session.
    actor
        The acting principal.
    admin
        Whether to grant ADMIN on the instance.

    Returns
    -------
    Principal
        The person.
    """
    allow = AllowAllAuthorization()
    name = f"p-{uuid.uuid4().hex[:8]}"
    row = PrincipalService(session, allow).create_principal(
        actor, name=name, type=PrincipalType.USER
    )
    if admin:
        GrantService(session, allow).grant(
            actor, principal=name, role=Role.ADMIN, kind=SecurableKind.INSTANCE
        )
    session.add(
        Passkey(
            id=uuid.uuid4(),
            principal_id=row.id,
            credential_id=uuid.uuid4().hex,
            public_key=b"k",
            sign_count=0,
            rp_id="localhost",
            name="Passkey",
            backup_eligible=False,
            created_at=datetime.now(UTC),
            created_user_agent="pytest",
        )
    )
    session.commit()
    return row


def _identity(subject: str) -> ProviderIdentity:
    """Build a verified provider identity.

    Parameters
    ----------
    subject
        The ``sub``.

    Returns
    -------
    ProviderIdentity
        The identity.
    """
    return ProviderIdentity(issuer=_ISSUER, subject=subject, email=None, name=None, auth_time=None)


def _linking(principal_id: uuid.UUID, subject: str) -> Callable[[Session], None]:
    """Build one link that leaves its change uncommitted.

    Parameters
    ----------
    principal_id
        The person to link.
    subject
        The ``sub`` to link them to.

    Returns
    -------
    Callable[[Session], None]
        The link, run in the session it is given.
    """

    def run(session: Session) -> None:
        """Link in `session` without committing.

        Parameters
        ----------
        session
            The link's own session.
        """
        OidcLinkService(session, AllowAllAuthorization(), bootstrap_name="admin").link(
            principal_id, _identity(subject), via="profile"
        )

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
        """Run the change and commit it, recording ``done``, the refusal's reason or message."""
        with Session(self._engine) as session:
            try:
                self._change(session)
                session.commit()
            except OidcSignInRefused as refused:
                session.rollback()
                self.outcome = refused.reason
                return
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
            ``done``, or what refused it.
        """
        self._thread.join(timeout=_WAIT_LIMIT)
        assert not self._thread.is_alive()
        return self.outcome


def _unlinked(engine: Engine) -> int:
    """Count the unlinked administrators as committed.

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
        return unlinked_admin_count(session)


def _linked_subjects(engine: Engine, subject: str) -> list[str]:
    """Name the people linked to one subject.

    Parameters
    ----------
    engine
        The engine.
    subject
        The ``sub``.

    Returns
    -------
    list of str
        Their names.
    """
    with Session(engine) as session:
        return list(
            session.execute(
                select(Principal.name).where(Principal.oidc_subject == subject)
            ).scalars()
        )


def test_last_two_unlinked_admins_link_concurrently(engine: Engine, actor: PrincipalRef) -> None:
    """Of the last two administrators who sign in with a passkey linking at once, one is refused."""
    with Session(engine) as session:
        ann = _person(session, actor, admin=True)
        bob = _person(session, actor, admin=True)
        ann_id, bob_id = ann.id, bob.id
    assert _unlinked(engine) == 2
    with Session(engine) as session:
        _linking(ann_id, f"ann-{ann_id.hex}")(session)
        racing = _Second(engine, _linking(bob_id, f"bob-{bob_id.hex}"))
        racing.blocked_or_done()
        session.commit()
    assert racing.finish() == "last_unlinked_admin"
    assert _unlinked(engine) == 1


def test_a_link_and_a_deactivation_of_the_other_admin_at_once(
    engine: Engine, actor: PrincipalRef
) -> None:
    """Linking one and deactivating the other unlinked administrator cannot both commit."""
    with Session(engine) as session:
        ann = _person(session, actor, admin=True)
        bob = _person(session, actor, admin=True)
        ann_id, bob_name = ann.id, bob.name
    with Session(engine) as session:
        _linking(ann_id, f"ann-{ann_id.hex}")(session)

        def deactivate(other: Session) -> None:
            """Deactivate Bob in `other` without committing.

            Parameters
            ----------
            other
                The change's own session.
            """
            PrincipalService(other, AllowAllAuthorization()).deactivate_principal(
                actor, bob_name, commit=False
            )

        racing = _Second(engine, deactivate)
        racing.blocked_or_done()
        session.commit()
    assert racing.finish() == LAST_UNLINKED_ADMIN_MESSAGE
    assert _unlinked(engine) == 1


def test_two_accounts_linking_one_identity_concurrently(
    engine: Engine, actor: PrincipalRef
) -> None:
    """Two accounts linking the same identity at once: exactly one is linked to it."""
    subject = f"shared-{uuid.uuid4().hex[:8]}"
    with Session(engine) as session:
        alice = _person(session, actor, admin=False)
        mallory = _person(session, actor, admin=False)
        alice_id, alice_name, mallory_id = alice.id, alice.name, mallory.id
    with Session(engine) as session:
        _linking(alice_id, subject)(session)
        racing = _Second(engine, _linking(mallory_id, subject))
        racing.blocked_or_done()
        session.commit()
    assert racing.finish() == "identity_taken"
    assert _linked_subjects(engine, subject) == [alice_name]
