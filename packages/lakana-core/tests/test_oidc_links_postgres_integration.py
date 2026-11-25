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
from sqlalchemy import event, select, text, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from testcontainers.postgres import PostgresContainer

from lakana_core import migrate
from lakana_core.auth import AllowAllAuthorization, PrincipalRef, SecurableKind
from lakana_core.config import DatabaseConfig
from lakana_core.db import create_engine
from lakana_core.errors import ConflictError, LinkUnusableError, NotFoundError, OidcSignInRefused
from lakana_core.grants.admin_lock import lock_admin_count
from lakana_core.grants.break_glass import LAST_UNLINKED_ADMIN_MESSAGE
from lakana_core.grants.roles import Role
from lakana_core.grants.service import GrantService, active_instance_admin_ids
from lakana_core.identity.groups import GroupService
from lakana_core.identity.link_withdrawal import withdraw_open_links
from lakana_core.identity.links import LinkService
from lakana_core.identity.notices import NoticeService
from lakana_core.identity.oidc_flows import ProviderIdentity
from lakana_core.identity.oidc_links import (
    OidcLinkService,
    linked_principal_for_sign_in,
    unlinked_admin_count,
)
from lakana_core.identity.passkeys import PasskeyService
from lakana_core.identity.principals import PrincipalService
from lakana_core.identity.provider_issuer import (
    OIDC_ISSUER_KEY,
    IssuerChange,
    record_oidc_issuer,
    recorded_issuer,
)
from lakana_core.identity.sessions import SessionService
from lakana_core.models import (
    AuditEvent,
    AuditEventType,
    BrowserSession,
    Grant,
    InstanceSetting,
    Passkey,
    Principal,
    PrincipalType,
    SignInLink,
)

pytestmark = pytest.mark.integration

_DONE = "done"
_WAIT_LIMIT = 30.0
"""Seconds the test waits at most for the second change to block or finish."""

_ISSUER = "https://idp.example.com/realms/lakana"


def _bounded_lock_waits(connection: object, _record: object) -> None:
    """Make every lock wait of a new connection give up after 60 seconds.

    Parameters
    ----------
    connection
        The DBAPI connection.
    _record
        The pool's record, unused.
    """
    cursor = connection.cursor()  # ty: ignore[missing-attribute]
    cursor.execute("SET lock_timeout = '60s'")
    cursor.close()


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
        # A wait on a lock that never comes fails the test within a minute instead of hanging
        # the run: the races wait on the advisory lock only briefly.
        event.listen(engine, "connect", _bounded_lock_waits)
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


def _person(
    session: Session, actor: PrincipalRef, *, admin: bool, passkey: bool = True
) -> Principal:
    """Create a person, with a working passkey by default, optionally an administrator.

    Parameters
    ----------
    session
        Database session.
    actor
        The acting principal.
    admin
        Whether to grant ADMIN on the instance.
    passkey
        Whether to give them a working passkey.

    Returns
    -------
    Principal
        The person; committed.
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
    if not passkey:
        session.commit()
        return row
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
            except LinkUnusableError as refused:
                session.rollback()
                self.outcome = f"link {refused.state}"
                return
            except NotFoundError as refused:
                session.rollback()
                self.outcome = f"not found: {refused}"
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

    def blocked_or_done(self, wait_event: str | None = "advisory") -> None:
        """Wait until the change waits on a lock, or has finished.

        Parameters
        ----------
        wait_event
            The lock it waits on: ``advisory`` (the administrator-count lock), or `None` for any
            lock, a row lock included.

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
                        " AND wait_event_type = 'Lock'"
                        " AND (CAST(:event AS text) IS NULL OR wait_event = :event)"
                    ),
                    {"event": wait_event},
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


def test_a_grant_to_a_linked_person_and_a_deactivation_of_the_last_unlinked_admin_at_once(
    engine: Engine, actor: PrincipalRef
) -> None:
    """Review of #780: the last unlinked administrator goes while a linked person gains ADMIN.

    Each alone keeps the rule. The grant must wait for the deactivation and count again, or the
    instance ends with only linked administrators.
    """
    allow = AllowAllAuthorization()
    with Session(engine) as session:
        una = _person(session, actor, admin=True)
        _person(session, actor, admin=True, passkey=False)
        lin = _person(session, actor, admin=False)
        una_name, lin_id, lin_name = una.name, lin.id, lin.name
        OidcLinkService(session, allow, bootstrap_name="admin").link(
            lin_id, _identity(f"lin-{lin_id.hex}"), via="profile"
        )
        session.commit()
    assert _unlinked(engine) == 1
    with Session(engine) as session:
        PrincipalService(session, allow).deactivate_principal(actor, una_name, commit=False)

        def grant(other: Session) -> None:
            """Grant ADMIN to the linked person in `other` without committing.

            Parameters
            ----------
            other
                The change's own session.
            """
            GrantService(other, allow).grant(
                actor,
                principal=lin_name,
                role=Role.ADMIN,
                kind=SecurableKind.INSTANCE,
                commit=False,
            )

        racing = _Second(engine, grant)
        racing.blocked_or_done()
        session.commit()
    assert racing.finish() == LAST_UNLINKED_ADMIN_MESSAGE
    with Session(engine) as session:
        assert session.get(Principal, lin_id) is not None
        held = session.execute(
            select(Grant.id).where(Grant.principal_id == lin_id, Grant.revoked_at.is_(None))
        ).all()
    assert held == []


def test_two_finishes_linking_one_person_at_once(engine: Engine, actor: PrincipalRef) -> None:
    """Review of #780: two links of one person, two identities; the second overwrites nothing."""
    with Session(engine) as session:
        alice = _person(session, actor, admin=False)
        alice_id = alice.id
    first, second = f"a-{alice_id.hex}", f"b-{alice_id.hex}"
    with Session(engine) as session:
        _linking(alice_id, first)(session)
        racing = _Second(engine, _linking(alice_id, second))
        racing.blocked_or_done()
        session.commit()
    assert racing.finish() == "already_linked"
    with Session(engine) as session:
        row = session.get(Principal, alice_id)
        assert row is not None and row.oidc_subject == first


# -- round 2 of the review of #780 ---------------------------------------------------------------


def _linked_and_a_group(engine: Engine, actor: PrincipalRef) -> tuple[str, str]:
    """Make an administrator without a passkey, a linked person and an empty group.

    Parameters
    ----------
    engine
        The engine.
    actor
        The acting principal.

    Returns
    -------
    tuple of (str, str)
        The linked person's name and the group's name.
    """
    allow = AllowAllAuthorization()
    group = f"g-{uuid.uuid4().hex[:8]}"
    with Session(engine) as session:
        _person(session, actor, admin=True, passkey=False)
        lin = _person(session, actor, admin=False)
        lin_id, lin_name = lin.id, lin.name
        OidcLinkService(session, allow, bootstrap_name="admin").link(
            lin_id, _identity(f"lin-{lin_id.hex}"), via="profile"
        )
        GroupService(session, allow).create_group(actor, name=group)
        session.commit()
    return lin_name, group


def _group_grant(actor: PrincipalRef, group: str) -> Callable[[Session], None]:
    """Build a grant of instance ADMIN to a group that leaves its change uncommitted.

    Parameters
    ----------
    actor
        The acting principal.
    group
        The group.

    Returns
    -------
    Callable[[Session], None]
        The grant.
    """

    def run(session: Session) -> None:
        """Grant in `session` without committing.

        Parameters
        ----------
        session
            The grant's own session.
        """
        GrantService(session, AllowAllAuthorization()).grant(
            actor, group=group, role=Role.ADMIN, kind=SecurableKind.INSTANCE, commit=False
        )

    return run


def _membership(actor: PrincipalRef, group: str, member: str) -> Callable[[Session], None]:
    """Build a new member of a group that leaves its change uncommitted.

    Parameters
    ----------
    actor
        The acting principal.
    group
        The group.
    member
        The new member.

    Returns
    -------
    Callable[[Session], None]
        The change.
    """

    def run(session: Session) -> None:
        """Add the member in `session` without committing.

        Parameters
        ----------
        session
            The change's own session.
        """
        GroupService(session, AllowAllAuthorization()).add_member(
            actor, group, member, commit=False
        )

    return run


def _linked_admins(engine: Engine) -> int:
    """Count the active administrators linked to the provider, as committed.

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
        admins = active_instance_admin_ids(session)
        return len(
            session.execute(
                select(Principal.id).where(
                    Principal.id.in_(admins), Principal.oidc_subject.is_not(None)
                )
            ).all()
        )


@pytest.mark.parametrize("first", ["member", "grant"])
def test_a_group_grant_and_a_new_linked_member_at_once(
    engine: Engine, actor: PrincipalRef, first: str
) -> None:
    """Review of #780: the grant reads the group's members only under the lock, in either order."""
    lin, group = _linked_and_a_group(engine, actor)
    grant, member = _group_grant(actor, group), _membership(actor, group, lin)
    one, other = (member, grant) if first == "member" else (grant, member)
    with Session(engine) as session:
        one(session)
        racing = _Second(engine, other)
        racing.blocked_or_done()
        session.commit()
    assert racing.finish() == LAST_UNLINKED_ADMIN_MESSAGE
    assert _linked_admins(engine) == 0


def test_a_provider_finish_takes_the_admin_lock_before_the_links_row(
    engine: Engine, actor: PrincipalRef
) -> None:
    """Review of #780: a finish against a withdrawal of its link by the issuer's deactivation.

    The deactivation holds the administrator-count lock and then updates the link. The finish
    takes that lock before it claims the link, so it waits, and then finds the link withdrawn,
    instead of holding the link's row while it waits (a deadlock).
    """
    allow = AllowAllAuthorization()
    with Session(engine) as session:
        issuer = _person(session, actor, admin=True)
        _person(session, actor, admin=True)
        issuer_name = issuer.name
        issuer_ref = PrincipalRef(id=issuer.id, name=issuer.name, type=PrincipalType.USER)
        link = _links(session).issue_invite(
            issuer_ref, name=f"c-{uuid.uuid4().hex[:8]}", display_name=None
        )
        link_id = link.id

    def finish(other: Session) -> None:
        """Use the invite through the provider in `other`.

        Parameters
        ----------
        other
            The finish's own session.
        """
        _links(other).claim_for_provider(
            link_id, _identity(f"c-{link_id.hex}"), replacing=None, peer="p", user_agent="u"
        )

    with Session(engine) as session:
        lock_admin_count(session)
        racing = _Second(engine, finish)
        racing.blocked_or_done()
        PrincipalService(session, allow).deactivate_principal(actor, issuer_name, commit=False)
        session.commit()
    assert racing.finish() == "link_unusable"
    with Session(engine) as session:
        row = session.get(SignInLink, link_id)
        assert row is not None and row.withdrawn_at is not None and row.used_at is None


def _links(session: Session) -> LinkService:
    """Build the link service.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    LinkService
        Over allow-all authorization.
    """
    allow = AllowAllAuthorization()
    return LinkService(
        session,
        allow,
        SessionService(session, allow),
        origin="http://localhost:8000",
        local_mode=False,
        bootstrap_name="admin",
    )


def _uncommitted_unlink(engine: Engine, admin: PrincipalRef, name: str) -> Session:
    """Unlink a person in a session that stays open, its commit held back.

    Parameters
    ----------
    engine
        The engine.
    admin
        The acting administrator.
    name
        The person.

    Returns
    -------
    Session
        The open session; `Session.commit(session)` commits it.
    """
    session = Session(engine)
    session.commit = lambda: None
    OidcLinkService(session, AllowAllAuthorization(), bootstrap_name="admin").unlink(admin, name)
    return session


def _open_sessions(engine: Engine, principal_id: uuid.UUID) -> int:
    """Count a person's open browser sessions.

    Parameters
    ----------
    engine
        The engine.
    principal_id
        The person.

    Returns
    -------
    int
        How many there are.
    """
    with Session(engine) as session:
        return len(
            session.execute(
                select(BrowserSession.id).where(
                    BrowserSession.principal_id == principal_id,
                    BrowserSession.ended_at.is_(None),
                )
            ).all()
        )


def _admin_and_linked(engine: Engine, actor: PrincipalRef) -> tuple[PrincipalRef, Principal, str]:
    """Make an administrator and a linked person.

    Parameters
    ----------
    engine
        The engine.
    actor
        The acting principal.

    Returns
    -------
    tuple of (PrincipalRef, Principal, str)
        The administrator, the linked person and their subject.
    """
    with Session(engine, expire_on_commit=False) as session:
        admin = _person(session, actor, admin=True)
        alice = _person(session, actor, admin=False)
        subject = f"alice-{alice.id.hex}"
        OidcLinkService(session, AllowAllAuthorization(), bootstrap_name="admin").link(
            alice.id, _identity(subject), via="profile"
        )
        session.commit()
    return PrincipalRef(id=admin.id, name=admin.name, type=PrincipalType.USER), alice, subject


def _provider_sign_in(subject: str) -> Callable[[Session], None]:
    """Build a provider sign-in's session start, as the finish runs it.

    Parameters
    ----------
    subject
        The ``sub`` the provider verified.

    Returns
    -------
    Callable[[Session], None]
        The sign-in; it raises `OidcSignInRefused` (``not_linked``) when nobody is linked.
    """

    def run(session: Session) -> None:
        """Find the linked person under the row lock and start the session.

        Parameters
        ----------
        session
            The sign-in's own session.

        Raises
        ------
        OidcSignInRefused
            When nobody is linked to the identity.
        """
        found = linked_principal_for_sign_in(session, _identity(subject))
        if found is None:
            raise OidcSignInRefused("not_linked")
        SessionService(session, AllowAllAuthorization()).start(
            found.id, via="oidc", replacing=None, peer="p", user_agent="u", touched=False
        )

    return run


def test_a_provider_sign_in_waits_for_an_unlink_and_gets_no_session(
    engine: Engine, actor: PrincipalRef
) -> None:
    """Review of #780: an unlink holds the person's row; the sign-in then finds nobody linked."""
    admin, alice, subject = _admin_and_linked(engine, actor)
    unlinking = _uncommitted_unlink(engine, admin, alice.name)
    racing = _Second(engine, _provider_sign_in(subject))
    racing.blocked_or_done(wait_event=None)
    Session.commit(unlinking)
    unlinking.close()
    assert racing.finish() == "not_linked"
    assert _open_sessions(engine, alice.id) == 0


def test_an_unlink_waits_for_a_provider_sign_in_and_ends_its_session(
    engine: Engine, actor: PrincipalRef
) -> None:
    """Review of #780: the sign-in holds the row first; the unlink then ends its new session."""
    admin, alice, subject = _admin_and_linked(engine, actor)

    def unlink(other: Session) -> None:
        """Unlink Alice in `other`; it commits itself.

        Parameters
        ----------
        other
            The unlink's own session.
        """
        OidcLinkService(other, AllowAllAuthorization(), bootstrap_name="admin").unlink(
            admin, alice.name
        )

    with Session(engine) as session:
        _provider_sign_in(subject)(session)
        racing = _Second(engine, unlink)
        racing.blocked_or_done(wait_event=None)
        session.commit()
    assert racing.finish() == _DONE
    assert _open_sessions(engine, alice.id) == 0


def test_a_recovery_through_the_provider_waits_for_an_unlink(
    engine: Engine, actor: PrincipalRef
) -> None:
    """Review of #780: a recovery's finish waits for an unlink, which withdrew its link.

    The unlink holds the administrator lock and the person's row; the finish takes that lock
    first, so it waits, and then finds the link withdrawn: no session, and nobody linked again.
    """
    admin, alice, subject = _admin_and_linked(engine, actor)
    with Session(engine) as session:
        link = _links(session).issue_recovery(admin, principal_name=alice.name)
        link_id = link.id
    unlinking = _uncommitted_unlink(engine, admin, alice.name)

    def recover(other: Session) -> None:
        """Use the recovery link through the provider in `other`.

        Parameters
        ----------
        other
            The finish's own session.
        """
        _links(other).claim_for_provider(
            link_id, _identity(subject), replacing=None, peer="p", user_agent="u"
        )

    racing = _Second(engine, recover)
    racing.blocked_or_done(wait_event=None)
    Session.commit(unlinking)
    unlinking.close()
    assert racing.finish() == "link_unusable"
    with Session(engine) as session:
        row = session.get(Principal, alice.id)
        assert row is not None and row.oidc_subject is None
    assert _open_sessions(engine, alice.id) == 0


# -- round 3 of the review of #780 ---------------------------------------------------------------


def test_a_redeem_takes_the_admin_lock_before_the_links_row(
    engine: Engine, actor: PrincipalRef
) -> None:
    """Review of #780: using a recovery link against a deactivation of its person.

    The deactivation holds the administrator-count lock, locks the person's row, and then
    withdraws their links. The redeem takes that lock before it claims the link, so it waits and
    then finds the link withdrawn, instead of holding the link's row while it waits for the
    person's (a deadlock).
    """
    with Session(engine) as session:
        admin = _person(session, actor, admin=True)
        dana = _person(session, actor, admin=False)
        admin_ref = PrincipalRef(id=admin.id, name=admin.name, type=PrincipalType.USER)
        issued = _links(session).issue_recovery(admin_ref, principal_name=dana.name)
        dana_id, link_id = dana.id, issued.id
        public_id, secret = issued.url.split("#", 1)[1].split("&")
        public_id, secret = public_id.split("=", 1)[1], secret.split("=", 1)[1]

    def redeem(other: Session) -> None:
        """Use the recovery link on the passkey path in `other`.

        Parameters
        ----------
        other
            The redeem's own session.
        """
        _links(other).redeem(
            public_id, secret, replacing=None, peer="p", user_agent="u", has_session=False
        )

    with Session(engine) as session:
        # The deactivation's first steps: the administrator lock, then the person's row.
        lock_admin_count(session)
        session.execute(update(Principal).where(Principal.id == dana_id).values(is_active=False))
        racing = _Second(engine, redeem)
        racing.blocked_or_done(wait_event=None)
        withdraw_open_links(session, dana_id, now=datetime.now(UTC), reason="deactivated")
        session.commit()
    assert racing.finish() == "link withdrawn"
    with Session(engine) as session:
        row = session.get(SignInLink, link_id)
        assert row is not None and row.used_at is None and row.withdrawn_at is not None


def test_removing_a_passkey_while_its_person_is_unlinked(
    engine: Engine, actor: PrincipalRef
) -> None:
    """Review of #780: the removal waits for the unlink, then finds the passkey gone (no 500)."""
    admin, alice, _ = _admin_and_linked(engine, actor)
    with Session(engine) as session:
        [passkey_id] = session.execute(
            select(Passkey.id).where(Passkey.principal_id == alice.id)
        ).scalars()
    unlinking = _uncommitted_unlink(engine, admin, alice.name)

    def remove(other: Session) -> None:
        """Remove the passkey as an administrator in `other`.

        Parameters
        ----------
        other
            The removal's own session.
        """
        PasskeyService(
            other,
            AllowAllAuthorization(),
            SessionService(other, AllowAllAuthorization()),
            NoticeService(other),
            rp_id="localhost",
            origin="http://localhost:8000",
        ).remove(admin, alice.name, passkey_id)

    racing = _Second(engine, remove)
    racing.blocked_or_done()
    Session.commit(unlinking)
    unlinking.close()
    assert racing.finish().startswith("not found: ")


# -- the issuer change (#809) ---------------------------------------------------------------------

_NEW_ISSUER = "https://login.example.org/realms/lakana"


@pytest.fixture
def recorded(engine: Engine) -> Iterator[None]:
    """Let a test record issuers, and forget them afterwards so the other tests see none.

    Parameters
    ----------
    engine
        The engine.

    Yields
    ------
    None
        While the test runs.
    """
    yield
    with Session(engine) as session:
        row = session.get(InstanceSetting, OIDC_ISSUER_KEY)
        if row is not None:
            session.delete(row)
            session.commit()


def _relinking(principal_id: uuid.UUID, subject: str) -> Callable[[Session], None]:
    """Build one link to the new issuer that leaves its change uncommitted.

    Parameters
    ----------
    principal_id
        The person to link.
    subject
        The ``sub`` at the new issuer.

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
        identity = ProviderIdentity(
            issuer=_NEW_ISSUER, subject=subject, email=None, name=None, auth_time=None
        )
        OidcLinkService(session, AllowAllAuthorization(), bootstrap_name="admin").link(
            principal_id, identity, via="profile"
        )

    return run


def test_two_finishes_replacing_one_stale_link_at_once(
    engine: Engine, actor: PrincipalRef, recorded: None
) -> None:
    """The conditional update's three-way `unlinked_clause` on Postgres: one replaces it."""
    with Session(engine) as session:
        record_oidc_issuer(session, _ISSUER)
        alice = _person(session, actor, admin=False)
        alice_id = alice.id
        _linking(alice_id, f"old-{alice_id.hex}")(session)
        session.commit()
        record_oidc_issuer(session, _NEW_ISSUER)
    first, second = f"a-{alice_id.hex}", f"b-{alice_id.hex}"
    with Session(engine) as session:
        _relinking(alice_id, first)(session)
        racing = _Second(engine, _relinking(alice_id, second))
        racing.blocked_or_done()
        session.commit()
    assert racing.finish() == "already_linked"
    with Session(engine) as session:
        row = session.get(Principal, alice_id)
        assert row is not None
        assert (row.oidc_issuer, row.oidc_subject) == (_NEW_ISSUER, first)
        replaces = [
            d.get("replaces")
            for d in session.execute(
                select(AuditEvent.detail).where(
                    AuditEvent.event_type == AuditEventType.PRINCIPAL_OIDC_LINKED,
                    AuditEvent.principal_id == alice_id,
                )
            ).scalars()
        ]
        assert {"iss": _ISSUER, "sub": f"old-{alice_id.hex}"} in replaces


def test_recording_the_issuer_waits_for_the_admin_lock_and_reads_afresh(
    engine: Engine, recorded: None
) -> None:
    """Two servers starting at once: the second reads what the first recorded under the lock."""
    with Session(engine) as session:
        record_oidc_issuer(session, _ISSUER)
    seen: list[IssuerChange | None] = []

    def record(other: Session) -> None:
        """Read the recorded issuer into the session's cache, then record the new one.

        Parameters
        ----------
        other
            The second server's session.
        """
        # Held, so the identity map keeps the copy read before the lock.
        cached = other.get(InstanceSetting, OIDC_ISSUER_KEY)
        assert cached is not None and recorded_issuer(other) == _ISSUER
        seen.append(record_oidc_issuer(other, _NEW_ISSUER))

    with Session(engine) as session:
        lock_admin_count(session)
        row = session.get(InstanceSetting, OIDC_ISSUER_KEY)
        assert row is not None
        row.value = {"issuer": None}
        session.flush()
        racing = _Second(engine, record)
        racing.blocked_or_done()
        session.commit()
    assert racing.finish() == _DONE
    [change] = seen
    assert change is not None and change.before is None and change.after == _NEW_ISSUER
    with Session(engine) as session:
        assert recorded_issuer(session) == _NEW_ISSUER
        details = session.execute(
            select(AuditEvent.detail)
            .where(AuditEvent.event_type == AuditEventType.SIGN_IN_SETTINGS_CHANGED)
            .order_by(AuditEvent.occurred_at.desc())
            .limit(1)
        ).scalars()
        [last] = list(details)
        assert (last["before"], last["after"]) == (
            {"oidc_issuer": None},
            {"oidc_issuer": _NEW_ISSUER},
        )
