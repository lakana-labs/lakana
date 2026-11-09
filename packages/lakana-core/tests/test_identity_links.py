"""Tests for single-use sign-in links (`lakana_core.identity.links`)."""

from __future__ import annotations

import json
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from lakana_core.auth import AllowAllAuthorization, PrincipalRef, SecurableKind
from lakana_core.bootstrap import bootstrap_if_needed, ensure_bootstrap_admin_grant
from lakana_core.config import DatabaseConfig
from lakana_core.db import create_engine
from lakana_core.errors import (
    AlreadyExistsError,
    ConflictError,
    LinkUnusableError,
    NotFoundError,
    PermissionDenied,
    SessionEndedError,
    StepUpRequired,
    ValidationError,
)
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.grants.roles import Role
from lakana_core.grants.service import GrantService
from lakana_core.identity.groups import GroupService
from lakana_core.identity.links import (
    LINK_LIFETIMES,
    IssuedLink,
    LinkService,
    Redeemed,
    is_operator,
    refuse_automation,
)
from lakana_core.identity.notices import NoticeService
from lakana_core.identity.pats import PatService
from lakana_core.identity.principals import PrincipalService
from lakana_core.identity.sessions import SessionService
from lakana_core.models import (
    AuditEvent,
    AuditEventType,
    Base,
    BrowserSession,
    Grant,
    Passkey,
    PersonalAccessToken,
    Principal,
    PrincipalType,
    SignInLink,
)

START = datetime(2026, 11, 8, 9, 0, tzinfo=UTC)
ORIGIN = "http://localhost:8000"


class _Clock:
    """A settable clock for the services."""

    def __init__(self) -> None:
        """Start at `START`."""
        self.now = START

    def __call__(self) -> datetime:
        """Read the clock.

        Returns
        -------
        datetime
            The time set.
        """
        return self.now


class World:
    """The services and people of one test.

    Parameters
    ----------
    session
        Database session.
    clock
        The clock.
    local_mode
        Whether local sign-in is on.
    """

    def __init__(self, session: Session, clock: _Clock, *, local_mode: bool = True) -> None:
        self.session = session
        self.clock = clock
        bootstrap_if_needed(session, "admin", key_id="opkey", secret="opsecret")
        self.authz = GrantAuthorization(session)
        self.sessions = SessionService(session, self.authz, clock=clock)
        self.links = LinkService(
            session,
            self.authz,
            self.sessions,
            origin=ORIGIN,
            local_mode=local_mode,
            bootstrap_name="admin",
            clock=clock,
        )
        self.operator = PatService(session, AllowAllAuthorization()).verify("opkey", "opsecret")

    def person(
        self, name: str, *, admin: bool = False, kind: PrincipalType = PrincipalType.USER
    ) -> Principal:
        """Insert a person, optionally an instance administrator.

        Parameters
        ----------
        name
            The name.
        admin
            Whether to grant ADMIN at instance scope.
        kind
            The principal's type.

        Returns
        -------
        Principal
            The row.
        """
        row = Principal(id=uuid.uuid4(), type=kind, name=name, is_active=True)
        self.session.add(row)
        self.session.commit()
        if admin:
            ensure_bootstrap_admin_grant(
                self.session,
                PrincipalRef(id=row.id, name=row.name, type=row.type),
                AllowAllAuthorization(),
            )
        return row

    def pat(self, row: Principal, *, name: str = "laptop") -> PrincipalRef:
        """Issue a personal access token for a person and authenticate with it.

        Parameters
        ----------
        row
            The person.
        name
            The token's label.

        Returns
        -------
        PrincipalRef
            The person, as their token authenticates them.
        """
        issued = PatService(self.session, AllowAllAuthorization()).issue(
            PrincipalRef(id=row.id, name=row.name, type=row.type), row.name, name=name
        )
        key_id, secret = issued.token.removeprefix("lakana_pat_").split("_", 1)
        return PatService(self.session, AllowAllAuthorization()).verify(key_id, secret)

    def signed_in(self, row: Principal, *, touched: bool = True) -> PrincipalRef:
        """Start a browser session for a person and authenticate with it.

        Parameters
        ----------
        row
            The person.
        touched
            Whether the session has a fresh passkey touch.

        Returns
        -------
        PrincipalRef
            The person, as their session authenticates them.
        """
        started = self.sessions.start(
            row.id, via="passkey", replacing=None, peer="p", user_agent="u", touched=touched
        )
        self.session.commit()
        return self.sessions.verify(started.cookie_value, extend=False)

    def redeem(self, link: IssuedLink, **kwargs: object) -> Redeemed:
        """Use a link from its URL.

        Parameters
        ----------
        link
            The issued link.
        **kwargs
            Overrides of `redeem`'s keyword arguments.

        Returns
        -------
        Redeemed
            What the link started.
        """
        public_id, secret = parts(link.url)
        arguments: dict = {
            "replacing": None,
            "peer": "127.0.0.1",
            "user_agent": "pytest",
            "has_session": False,
        }
        arguments.update(kwargs)
        return self.links.redeem(public_id, secret, **arguments)


def parts(url: str) -> tuple[str, str]:
    """Split a link's URL into its public id and secret.

    Parameters
    ----------
    url
        The link.

    Returns
    -------
    tuple of (str, str)
        The public id and the secret, both from after the ``#``.
    """
    base, fragment = url.split("#", 1)
    assert base == f"{ORIGIN}/sign-in"
    values = dict(item.split("=", 1) for item in fragment.split("&"))
    return values["l"], values["s"]


@pytest.fixture
def clock() -> _Clock:
    """Provide the services' clock.

    Returns
    -------
    _Clock
        Set `.now` to move time.
    """
    return _Clock()


@pytest.fixture
def world(session: Session, clock: _Clock) -> World:
    """Provide a bootstrapped instance with local sign-in on.

    Parameters
    ----------
    session
        Database session.
    clock
        The clock.

    Returns
    -------
    World
        The services and the operator.
    """
    return World(session, clock)


def _invite(world: World, name: str = "carol", display_name: str | None = None) -> IssuedLink:
    """Issue an invite as the operator.

    Parameters
    ----------
    world
        The test's world.
    name
        The invited name.
    display_name
        The invitee's display name.

    Returns
    -------
    IssuedLink
        The invite.
    """
    return world.links.issue_invite(world.operator, name=name, display_name=display_name)


def test_secret_is_stored_only_as_a_hash(world: World) -> None:
    """The row keeps a digest; neither the secret nor the URL is stored."""
    link = _invite(world)
    public_id, secret = parts(link.url)
    row = world.session.get(SignInLink, link.id)
    assert row is not None
    assert row.public_id == public_id
    assert secret not in row.secret_hash
    assert len(row.secret_hash) == 64
    stored = json.dumps([str(getattr(row, c.key)) for c in SignInLink.__table__.columns])
    assert secret not in stored


def test_describe_never_consumes(world: World) -> None:
    """Describing a link three times leaves it usable."""
    link = _invite(world, display_name="Carol C.")
    public_id, _ = parts(link.url)
    for _ in range(3):
        description = world.links.describe(public_id)
        assert (description.kind, description.name, description.state) == (
            "invite",
            "carol",
            "open",
        )
        assert description.display_name == "Carol C."
    assert world.redeem(link).kind == "invite"


def test_describe_of_an_unknown_link(world: World) -> None:
    """An unknown public id is `unknown`."""
    with pytest.raises(LinkUnusableError) as e:
        world.links.describe("nope")
    assert e.value.state == "unknown"


def test_link_works_once(world: World) -> None:
    """A second use is refused as used, and describe says so."""
    link = _invite(world)
    world.redeem(link)
    with pytest.raises(LinkUnusableError) as e:
        world.redeem(link)
    assert e.value.state == "used"
    assert "new link" in str(e.value)
    assert world.links.describe(parts(link.url)[0]).state == "used"


def test_a_wrong_secret_answers_like_an_unknown_link(world: World) -> None:
    """A right public id with a wrong secret is `unknown`, and the link stays usable."""
    link = _invite(world)
    public_id, _ = parts(link.url)
    with pytest.raises(LinkUnusableError) as e:
        world.links.redeem(
            public_id, "wrong", replacing=None, peer="p", user_agent="u", has_session=False
        )
    assert e.value.state == "unknown"
    assert world.redeem(link).kind == "invite"


def test_link_at_its_expiry_instant_is_expired(world: World, clock: _Clock) -> None:
    """At exactly `expires_at` the link is expired."""
    link = _invite(world)
    clock.now = link.expires_at
    assert world.links.describe(parts(link.url)[0]).state == "expired"
    with pytest.raises(LinkUnusableError) as e:
        world.redeem(link)
    assert e.value.state == "expired"


def test_withdraw_and_use_race_one_wins(world: World) -> None:
    """Withdrawing a used link fails; using a withdrawn link fails."""
    used = _invite(world, "carol")
    world.redeem(used)
    with pytest.raises(ConflictError):
        world.links.withdraw(world.operator, used.id)
    withdrawn = _invite(world, "dave")
    world.links.withdraw(world.operator, withdrawn.id)
    with pytest.raises(LinkUnusableError) as e:
        world.redeem(withdrawn)
    assert e.value.state == "withdrawn"


def test_withdraw_of_an_unknown_link(world: World) -> None:
    """Withdrawing a link that does not exist is 404."""
    with pytest.raises(NotFoundError):
        world.links.withdraw(world.operator, uuid.uuid4())


def test_concurrent_redeems_exactly_one_wins_sqlite(tmp_path: Path) -> None:
    """Two threads use one link at once on a file database: one session, one refusal."""
    engine = create_engine(DatabaseConfig(url=f"sqlite:///{tmp_path / 'links.db'}"))
    # Durability is not under test; without fsync the schema takes milliseconds, not a second.
    event.listen(engine, "connect", lambda dbapi, _: dbapi.execute("PRAGMA synchronous=OFF"))
    Base.metadata.create_all(engine)
    with Session(engine) as setup:
        world = World(setup, _Clock())
        link = world.links.issue_setup(world.operator)
    public_id, secret = parts(link.url)
    barrier = threading.Barrier(2)
    results: list[object] = []

    def use() -> None:
        """Use the link from its own connection."""
        with Session(engine) as own:
            authz = AllowAllAuthorization()
            clock = _Clock()
            sessions = SessionService(own, authz, clock=clock)
            service = LinkService(
                own,
                authz,
                sessions,
                origin=ORIGIN,
                local_mode=False,
                bootstrap_name="admin",
                clock=clock,
            )
            barrier.wait()
            try:
                results.append(
                    service.redeem(
                        public_id,
                        secret,
                        replacing=None,
                        peer="p",
                        user_agent="u",
                        has_session=False,
                    )
                )
            except LinkUnusableError as exc:
                results.append(exc)

    threads = [threading.Thread(target=use) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    engine.dispose()
    assert sorted(type(r).__name__ for r in results) == ["LinkUnusableError", "Redeemed"]
    refused = next(r for r in results if isinstance(r, LinkUnusableError))
    assert refused.state == "used"


@pytest.mark.parametrize(
    ("kind", "lifetime"),
    [("setup", 600), ("local", 600), ("recovery", 3600), ("invite", 604800)],
)
def test_lifetimes(kind: str, lifetime: int) -> None:
    """Setup and local 10 minutes, recovery 1 hour, invite 7 days."""
    assert LINK_LIFETIMES[kind].total_seconds() == lifetime


def test_issued_links_expire_after_their_lifetime(world: World) -> None:
    """An issued link's expiry is the issue time plus its kind's lifetime."""
    assert _invite(world).expires_at == START + LINK_LIFETIMES["invite"]
    assert world.links.issue_setup(world.operator).expires_at == START + LINK_LIFETIMES["setup"]


def test_invite_creates_a_new_account_with_no_rights(world: World) -> None:
    """An invite creates a USER with no grants, who must register a passkey."""
    redeemed = world.redeem(_invite(world, "carol", "Carol C."))
    row = world.session.execute(select(Principal).where(Principal.name == "carol")).scalar_one()
    assert row.type is PrincipalType.USER
    assert row.display_name == "Carol C."
    assert redeemed.started.principal.id == row.id
    grants = world.session.execute(select(Grant).where(Grant.principal_id == row.id)).all()
    assert grants == []
    assert (redeemed.needs_passkey, redeemed.offer_passkey) == (True, False)


@pytest.mark.parametrize("existing_active", [True, False])
def test_invite_refuses_a_case_variant_of_an_existing_name(
    world: World, existing_active: bool
) -> None:
    """``Alice`` is refused while ``alice`` exists, also when ``alice`` is deactivated."""
    row = world.person("alice")
    row.is_active = existing_active
    world.session.commit()
    with pytest.raises(AlreadyExistsError, match="alice"):
        _invite(world, "Alice")


def test_principal_create_refuses_a_case_variant(world: World) -> None:
    """Creating ``Alice`` while ``alice`` exists is refused (F25)."""
    world.person("alice")
    with pytest.raises(AlreadyExistsError, match="letter case"):
        PrincipalService(world.session, world.authz).create_principal(
            world.operator, name="Alice", type=PrincipalType.USER
        )


def test_invite_name_is_reserved_while_open(world: World, clock: _Clock) -> None:
    """While an invite is open its name cannot be created or invited again; after expiry it can."""
    link = _invite(world, "carol")
    principals = PrincipalService(world.session, world.authz)
    with pytest.raises(AlreadyExistsError, match="invite"):
        principals.create_principal(world.operator, name="Carol", type=PrincipalType.USER)
    with pytest.raises(AlreadyExistsError, match="invite"):
        _invite(world, "CAROL")
    clock.now = link.expires_at
    assert _invite(world, "carol").id != link.id


def test_withdrawing_an_invite_frees_its_name(world: World) -> None:
    """A withdrawn invite no longer reserves its name."""
    link = _invite(world, "carol")
    world.links.withdraw(world.operator, link.id)
    assert _invite(world, "carol").id != link.id


def test_invite_refused_when_name_taken_meanwhile(world: World) -> None:
    """A principal created under the name by another way makes the invite fail, naming why."""
    link = _invite(world, "carol")
    world.person("carol")
    with pytest.raises(ConflictError, match="taken meanwhile"):
        world.redeem(link)


def test_invite_never_for_the_bootstrap_name(world: World) -> None:
    """The bootstrap name is never invited, in any letter case."""
    with pytest.raises(AlreadyExistsError):
        _invite(world, "ADMIN")


def test_invite_needs_an_administrator_with_step_up(world: World) -> None:
    """A non-administrator is refused; an administrator's stale session needs step-up."""
    bob = world.person("bob")
    with pytest.raises(PermissionDenied):
        world.links.issue_invite(world.pat(bob), name="carol", display_name=None)
    boss = world.person("boss", admin=True)
    with pytest.raises(StepUpRequired):
        world.links.issue_invite(
            world.signed_in(boss, touched=False), name="carol", display_name=None
        )
    assert world.links.issue_invite(world.signed_in(boss), name="carol", display_name=None)


def test_recovery_of_an_admin_only_by_the_operator(world: World) -> None:
    """An ordinary administrator cannot recover an administrator; the operator may."""
    boss = world.person("boss", admin=True)
    other = world.person("other", admin=True)
    for caller in (world.signed_in(other), world.pat(other)):
        with pytest.raises(PermissionDenied, match="server operator"):
            world.links.issue_recovery(caller, principal_name="boss")
    link = world.links.issue_recovery(world.operator, principal_name="boss")
    assert link.kind == "recovery"
    assert world.redeem(link).started.principal.id == boss.id


def test_recovery_of_a_group_admin_only_by_the_operator(world: World) -> None:
    """ADMIN held through a group counts as an administrator."""
    member = world.person("member")
    allow = AllowAllAuthorization()
    GroupService(world.session, allow).create_group(world.operator, name="admins")
    GroupService(world.session, allow).add_member(world.operator, "admins", "member")
    GrantService(world.session, allow).grant(
        world.operator, group="admins", role=Role.ADMIN, kind=SecurableKind.INSTANCE
    )
    boss = world.person("boss", admin=True)
    with pytest.raises(PermissionDenied, match="server operator"):
        world.links.issue_recovery(world.pat(boss), principal_name=member.name)


def test_admins_pat_minted_for_the_bootstrap_principal_is_not_the_operator(world: World) -> None:
    """A PAT named ``bootstrap`` an administrator issued for ``admin`` is not the operator's."""
    admin = world.session.execute(select(Principal).where(Principal.name == "admin")).scalar_one()
    impostor = world.pat(admin, name="bootstrap")
    assert not is_operator(world.session, impostor, bootstrap_name="admin")
    assert is_operator(world.session, world.operator, bootstrap_name="admin")
    world.person("boss", admin=True)
    with pytest.raises(PermissionDenied, match="server operator"):
        world.links.issue_recovery(impostor, principal_name="boss")
    with pytest.raises(PermissionDenied, match="bootstrap token"):
        world.links.issue_setup(impostor)


def test_a_bootstrap_pat_of_another_principal_is_not_the_operator(world: World) -> None:
    """The operator check also needs the bootstrap principal's name."""
    renamed = PrincipalRef(
        id=world.operator.id,
        name="someone",
        type=world.operator.type,
        credential="pat",
        token_id=world.operator.token_id,
    )
    assert not is_operator(world.session, renamed, bootstrap_name="admin")


@pytest.mark.parametrize("caller", ["task_token", "ci_token", "service_account_pat"])
def test_automation_never_issues_a_link(world: World, caller: str) -> None:
    """Task, CI and service-account credentials never issue a link of any kind."""
    if caller == "service_account_pat":
        ref = world.pat(world.person("robot", kind=PrincipalType.SERVICE_ACCOUNT))
    else:
        ref = PrincipalRef(
            id=world.operator.id,
            name=world.operator.name,
            type=world.operator.type,
            credential="task" if caller == "task_token" else "ci",
        )
    world.person("bob")
    calls = [
        lambda: world.links.issue_invite(ref, name="carol", display_name=None),
        lambda: world.links.issue_recovery(ref, principal_name="bob"),
        lambda: world.links.issue_local(ref),
        lambda: world.links.issue_setup(ref),
    ]
    for call in calls:
        with pytest.raises(PermissionDenied, match="Links are for people"):
            call()
    with pytest.raises(PermissionDenied, match="Links are for people"):
        refuse_automation(ref)


def test_recovery_of_oneself_is_refused(world: World) -> None:
    """Recovering one's own account is refused, naming Add passkey."""
    boss = world.person("boss", admin=True)
    with pytest.raises(ConflictError, match="Add passkey"):
        world.links.issue_recovery(world.pat(boss), principal_name="boss")


def test_recovery_of_a_service_account_is_refused(world: World) -> None:
    """Service accounts have no passkeys to recover."""
    world.person("robot", kind=PrincipalType.SERVICE_ACCOUNT)
    with pytest.raises(ConflictError, match="service account"):
        world.links.issue_recovery(world.operator, principal_name="robot")


def test_recovery_needs_an_administrator(world: World) -> None:
    """A non-administrator cannot recover another person."""
    bob = world.person("bob")
    world.person("dave")
    with pytest.raises(PermissionDenied):
        world.links.issue_recovery(world.pat(bob), principal_name="dave")


def _passkey(world: World, row: Principal) -> None:
    """Register a stand-in passkey for a person.

    Parameters
    ----------
    world
        The test's world.
    row
        The person.
    """
    world.session.add(
        Passkey(
            id=uuid.uuid4(),
            principal_id=row.id,
            credential_id=uuid.uuid4().hex,
            public_key=b"k",
            sign_count=0,
            rp_id="localhost",
            name="key",
            backup_eligible=False,
            created_at=START,
            created_user_agent="u",
        )
    )
    world.session.commit()


def test_recovery_replaces_passkeys_ends_sessions_and_notifies(world: World) -> None:
    """Recovery removes the passkeys, ends the sessions and notifies the person and the admins."""
    bob = world.person("bob")
    boss = world.person("boss", admin=True)
    _passkey(world, bob)
    old = world.sessions.start(
        bob.id, via="passkey", replacing=None, peer="p", user_agent="u", touched=True
    )
    world.session.commit()
    link = world.links.issue_recovery(world.pat(boss), principal_name="bob")
    redeemed = world.redeem(link)
    assert (redeemed.needs_passkey, redeemed.offer_passkey) == (True, False)
    assert world.session.execute(select(Passkey).where(Passkey.principal_id == bob.id)).all() == []
    with pytest.raises(SessionEndedError) as e:
        world.sessions.verify(old.cookie_value, extend=False)
    assert e.value.reason == "recovery"
    assert world.sessions.verify(redeemed.started.cookie_value, extend=False).id == bob.id
    notices = NoticeService(world.session)
    bob_ref = world.pat(bob)
    assert [n.kind for n in notices.list_for(bob_ref)] == ["recovery_used"]
    assert [n.subject_id for n in notices.list_for(world.pat(boss))] == [bob.id]
    assert len(notices.list_for(world.operator)) == 1


def test_notices_are_dismissed_by_their_recipient_only(world: World) -> None:
    """A person dismisses their own notice; another person's is not found."""
    bob = world.person("bob")
    notices = NoticeService(world.session)
    notice = notices.add(bob.id, "recovery_used", bob.id, {"name": "bob"})
    world.session.commit()
    with pytest.raises(NotFoundError):
        notices.dismiss(world.operator, notice.id)
    notices.dismiss(world.pat(bob), notice.id)
    assert notices.list_for(world.pat(bob)) == []


def test_new_recovery_link_withdraws_the_previous(world: World) -> None:
    """A second recovery link for the same person withdraws the first."""
    world.person("bob")
    first = world.links.issue_recovery(world.operator, principal_name="bob")
    second = world.links.issue_recovery(world.operator, principal_name="bob")
    with pytest.raises(LinkUnusableError) as e:
        world.redeem(first)
    assert e.value.state == "withdrawn"
    assert [r.id for r in world.links.list_open(world.operator, "recovery")] == [second.id]


def test_list_open_needs_an_administrator(world: World) -> None:
    """Only an administrator lists open invites."""
    _invite(world)
    with pytest.raises(PermissionDenied):
        world.links.list_open(world.pat(world.person("bob")), "invite")
    assert [r.invite_name for r in world.links.list_open(world.operator, "invite")] == ["carol"]


def test_setup_link_only_while_the_bootstrap_principal_has_no_passkey(world: World) -> None:
    """Once the first administrator has a passkey, the setup link names recovery."""
    redeemed = world.redeem(world.links.issue_setup(world.operator))
    assert redeemed.kind == "setup"
    assert (redeemed.needs_passkey, redeemed.offer_passkey) == (True, False)
    admin = world.session.get(Principal, world.operator.id)
    assert admin is not None
    _passkey(world, admin)
    with pytest.raises(ConflictError, match="lakana recovery create"):
        world.links.issue_setup(world.operator)


def test_setup_link_needs_the_bootstrap_token_not_a_session(world: World) -> None:
    """The bootstrap principal's own session is not the operator."""
    admin = world.session.get(Principal, world.operator.id)
    assert admin is not None
    with pytest.raises(PermissionDenied, match="bootstrap token"):
        world.links.issue_setup(world.signed_in(admin))


def test_local_link_only_in_local_mode_for_a_users_own_pat(world: World, session: Session) -> None:
    """A local link needs local mode and the person's own PAT; a session is refused."""
    bob = world.person("bob")
    link = world.links.issue_local(world.pat(bob))
    assert link.kind == "local"
    row = world.session.get(SignInLink, link.id)
    assert row is not None
    assert row.principal_id == bob.id and row.source_pat_id is not None
    with pytest.raises(PermissionDenied, match="personal access token"):
        world.links.issue_local(world.signed_in(bob))
    off = LinkService(
        session,
        world.authz,
        world.sessions,
        origin=ORIGIN,
        local_mode=False,
        bootstrap_name="admin",
        clock=world.clock,
    )
    with pytest.raises(NotFoundError, match="recovery link"):
        off.issue_local(world.pat(bob))


def test_local_link_not_used_automatically_over_an_existing_session(world: World) -> None:
    """A local link is refused (and stays open) while the browser has a session."""
    bob = world.person("bob")
    link = world.links.issue_local(world.pat(bob))
    with pytest.raises(ConflictError, match="already signed in"):
        world.redeem(link, has_session=True)
    assert world.redeem(link).kind == "local"


def test_local_link_session_needs_no_passkey(world: World) -> None:
    """A local sign-in of an account without a passkey offers one and needs none."""
    bob = world.person("bob")
    redeemed = world.redeem(world.links.issue_local(world.pat(bob)))
    assert (redeemed.needs_passkey, redeemed.offer_passkey) == (False, True)
    _passkey(world, bob)
    again = world.redeem(world.links.issue_local(world.pat(bob)))
    assert (again.needs_passkey, again.offer_passkey) == (False, False)


@pytest.mark.parametrize("kind", ["setup", "invite", "recovery"])
def test_link_session_without_a_passkey_needs_one(world: World, kind: str) -> None:
    """Setup, invite and recovery sessions of an account without a passkey must register one."""
    if kind == "setup":
        link = world.links.issue_setup(world.operator)
    elif kind == "invite":
        link = _invite(world)
    else:
        world.person("bob")
        link = world.links.issue_recovery(world.operator, principal_name="bob")
    redeemed = world.redeem(link)
    assert redeemed.kind == kind
    assert (redeemed.needs_passkey, redeemed.offer_passkey) == (True, False)
    session_row = world.session.execute(
        select(BrowserSession).where(BrowserSession.key_id == redeemed.started.key_id)
    ).scalar_one()
    assert session_row.via == kind
    assert session_row.last_touch_at is None


def test_local_session_ends_when_its_pat_is_revoked(world: World) -> None:
    """Revoking the token a local link was minted with ends the session."""
    bob = world.person("bob")
    token = world.pat(bob)
    redeemed = world.redeem(world.links.issue_local(token))
    pat_row = world.session.execute(
        select(BrowserSession).where(BrowserSession.key_id == redeemed.started.key_id)
    ).scalar_one()
    assert pat_row.source_pat_id == token.token_id
    pat = world.session.get(PersonalAccessToken, token.token_id)
    assert pat is not None
    PatService(world.session, AllowAllAuthorization()).revoke(token, "bob", pat.key_id)
    with pytest.raises(SessionEndedError) as e:
        world.sessions.verify(redeemed.started.cookie_value, extend=False)
    assert e.value.reason == "token_revoked"


def test_redeem_replaces_the_browsers_previous_session(world: World) -> None:
    """The session the browser presented ends as replaced; the new one has a fresh id."""
    bob = world.person("bob")
    old = world.sessions.start(
        bob.id, via="passkey", replacing=None, peer="p", user_agent="u", touched=True
    )
    world.session.commit()
    redeemed = world.redeem(_invite(world), replacing=old.cookie_value)
    assert redeemed.started.key_id != old.key_id
    with pytest.raises(SessionEndedError) as e:
        world.sessions.verify(old.cookie_value, extend=False)
    assert e.value.reason == "replaced"


def test_deactivation_withdraws_open_links(world: World) -> None:
    """Deactivating a person withdraws their open recovery and local links."""
    bob = world.person("bob")
    recovery = world.links.issue_recovery(world.operator, principal_name="bob")
    local = world.links.issue_local(world.pat(bob))
    PrincipalService(world.session, world.authz).deactivate_principal(world.operator, "bob")
    for link in (recovery, local):
        assert world.links.describe(parts(link.url)[0]).state == "withdrawn"
    assert world.links.withdraw_for_principal(bob.id) == 0


def test_every_link_event_is_audited_without_the_secret(world: World) -> None:
    """Issue, use and withdraw are audited, and no event holds the secret or its hash."""
    used = _invite(world, "carol")
    withdrawn = _invite(world, "dave")
    world.redeem(used)
    world.links.withdraw(world.operator, withdrawn.id)
    events = world.session.execute(select(AuditEvent)).scalars().all()
    kinds = [e.event_type for e in events]
    assert kinds.count(AuditEventType.SIGN_IN_LINK_ISSUED) == 2
    assert kinds.count(AuditEventType.SIGN_IN_LINK_USED) == 1
    assert kinds.count(AuditEventType.SIGN_IN_LINK_WITHDRAWN) == 1
    text = json.dumps([e.detail for e in events], default=str)
    for link in (used, withdrawn):
        _, secret = parts(link.url)
        row = world.session.get(SignInLink, link.id)
        assert row is not None
        assert secret not in text
        assert row.secret_hash not in text
        assert link.url not in text


def test_issued_link_hides_its_url_from_repr(world: World) -> None:
    """The URL carries the secret, so the dataclass leaves it out of `repr`."""
    link = _invite(world)
    assert parts(link.url)[1] not in repr(link)


def test_a_redeem_that_fails_after_the_update_leaves_the_link_open(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When starting the session fails, the link's use is rolled back with it."""
    link = _invite(world)

    def boom(*args: object, **kwargs: object) -> None:
        """Fail the way a broken insert would.

        Parameters
        ----------
        *args
            Ignored.
        **kwargs
            Ignored.

        Raises
        ------
        RuntimeError
            Always.
        """
        raise RuntimeError("boom")

    monkeypatch.setattr(world.sessions, "start", boom)
    with pytest.raises(RuntimeError):
        world.redeem(link)
    monkeypatch.undo()
    assert world.links.describe(parts(link.url)[0]).state == "open"
    assert world.redeem(link).kind == "invite"


def test_issued_link_never_lasts_past_its_lifetime(world: World, clock: _Clock) -> None:
    """One second before expiry the link works."""
    link = _invite(world)
    clock.now = link.expires_at - timedelta(seconds=1)
    assert world.redeem(link).kind == "invite"


def test_the_operator_may_recover_the_bootstrap_principal(world: World) -> None:
    """The operator, who has no other way back in, may recover their own account."""
    link = world.links.issue_recovery(world.operator, principal_name="admin")
    assert world.redeem(link).started.principal.id == world.operator.id


def test_recovery_link_refused_when_the_person_became_an_administrator(world: World) -> None:
    """A recovery link an administrator issued stops working once its person is an admin."""
    bob = world.person("bob")
    boss = world.person("boss", admin=True)
    link = world.links.issue_recovery(world.pat(boss), principal_name="bob")
    ensure_bootstrap_admin_grant(
        world.session,
        PrincipalRef(id=bob.id, name=bob.name, type=bob.type),
        AllowAllAuthorization(),
    )
    with pytest.raises(PermissionDenied, match="lakana recovery create bob"):
        world.redeem(link)
    assert world.links.describe(parts(link.url)[0]).state == "withdrawn"
    assert world.links.list_open(world.operator, "recovery") == []


def test_operators_recovery_link_for_an_administrator_still_works(world: World) -> None:
    """A recovery link the operator issued for an administrator is used normally."""
    world.person("boss", admin=True)
    link = world.links.issue_recovery(world.operator, principal_name="boss")
    assert world.redeem(link).kind == "recovery"


def test_setup_link_refused_once_the_bootstrap_account_has_a_passkey(world: World) -> None:
    """A setup link issued before the first passkey is refused when used after it."""
    link = world.links.issue_setup(world.operator)
    admin = world.session.get(Principal, world.operator.id)
    assert admin is not None
    _passkey(world, admin)
    with pytest.raises(ConflictError, match="lakana recovery create"):
        world.redeem(link)
    assert world.links.describe(parts(link.url)[0]).state == "withdrawn"


def test_invite_taken_meanwhile_is_closed_and_names_the_next_step(world: World) -> None:
    """An invite whose name was taken is closed (not listed as open) and names the next step."""
    link = _invite(world, "carol")
    world.person("carol")
    with pytest.raises(ConflictError, match="lakana invite create"):
        world.redeem(link)
    assert world.links.list_open(world.operator, "invite") == []
    assert world.links.describe(parts(link.url)[0]).state == "withdrawn"


@pytest.mark.parametrize(
    ("existing", "candidate"),
    [
        ("alice", "\uff41\uff4c\uff49\uff43\uff45"),  # fullwidth
        ("straße", "STRASSE"),
        ("\ufb01le", "file"),  # ligature
    ],
)
def test_look_alike_names_collide(world: World, existing: str, candidate: str) -> None:
    """Names are compared after NFKC and case folding, for invites and for creation."""
    world.person(existing)
    with pytest.raises(AlreadyExistsError):
        _invite(world, candidate)
    with pytest.raises(AlreadyExistsError):
        PrincipalService(world.session, world.authz).create_principal(
            world.operator, name=candidate, type=PrincipalType.USER
        )


@pytest.mark.parametrize(
    "name",
    ["al​ice", "​alice", "ali­ce", "alice\u0007", "bob\nx", "́alice"],
)
def test_names_with_invisible_or_control_characters_are_refused(world: World, name: str) -> None:
    """Zero-width and other format characters, control characters and a leading combining mark."""
    with pytest.raises(ValidationError, match="invisible"):
        _invite(world, name)
    with pytest.raises(ValidationError, match="invisible"):
        PrincipalService(world.session, world.authz).create_principal(
            world.operator, name=name, type=PrincipalType.USER
        )


@pytest.mark.parametrize("name", ["\ufb03" * 100, "a" * 256], ids=["ligatures", "raw"])
def test_a_name_whose_normalised_form_is_too_long_is_refused(world: World, name: str) -> None:
    """A name over 255 characters, raw or normalised (U+FB03 becomes ``ffi``), is a 422."""
    with pytest.raises(ValidationError, match="255"):
        _invite(world, name)
    with pytest.raises(ValidationError, match="255"):
        PrincipalService(world.session, world.authz).create_principal(
            world.operator, name=name, type=PrincipalType.USER
        )


def _admin_grant_of(world: World, row: Principal) -> uuid.UUID:
    """Find a person's direct, unrevoked instance ADMIN grant.

    Parameters
    ----------
    world
        The test's world.
    row
        The person.

    Returns
    -------
    uuid.UUID
        The grant's id.
    """
    return world.session.execute(
        select(Grant.id).where(
            Grant.principal_id == row.id,
            Grant.role == Role.ADMIN,
            Grant.kind == SecurableKind.INSTANCE,
            Grant.revoked_at.is_(None),
        )
    ).scalar_one()


def test_recovery_refused_for_a_former_administrator_after_revoking_the_grant(
    world: World,
) -> None:
    """Revoking someone's ADMIN grant first does not let an administrator recover them."""
    boss = world.person("boss", admin=True)
    bob = world.person("bob", admin=True)
    GrantService(world.session, AllowAllAuthorization()).revoke(
        world.pat(boss), _admin_grant_of(world, bob)
    )
    with pytest.raises(PermissionDenied, match="has held the administrator role") as refused:
        world.links.issue_recovery(world.pat(boss), principal_name="bob")
    assert "lakana recovery create bob" in str(refused.value)
    link = world.links.issue_recovery(world.operator, principal_name="bob")
    assert world.redeem(link).started.principal.id == bob.id


@pytest.mark.parametrize("member_first", [True, False])
def test_recovery_refused_for_a_former_administrator_after_leaving_the_admin_group(
    world: World, member_first: bool
) -> None:
    """Removing someone from the admin group first does not let an administrator recover them.

    Parameters
    ----------
    world
        The test's world.
    member_first
        Whether the person joins the group before or after the group gets ADMIN.
    """
    boss = world.person("boss", admin=True)
    world.person("bob")
    allow = AllowAllAuthorization()
    groups = GroupService(world.session, allow)
    groups.create_group(world.operator, name="admins")
    if member_first:
        groups.add_member(world.operator, "admins", "bob")
    GrantService(world.session, allow).grant(
        world.operator, group="admins", role=Role.ADMIN, kind=SecurableKind.INSTANCE
    )
    if not member_first:
        groups.add_member(world.operator, "admins", "bob")
    groups.remove_member(world.pat(boss), "admins", "bob")
    with pytest.raises(PermissionDenied, match="has held the administrator role"):
        world.links.issue_recovery(world.pat(boss), principal_name="bob")
    assert world.links.issue_recovery(world.operator, principal_name="bob")


def test_recovery_link_refused_at_use_when_the_person_was_an_administrator_meanwhile(
    world: World,
) -> None:
    """ADMIN granted and revoked between issue and use still closes an administrator's link."""
    boss = world.person("boss", admin=True)
    bob = world.person("bob")
    link = world.links.issue_recovery(world.pat(boss), principal_name="bob")
    ensure_bootstrap_admin_grant(
        world.session,
        PrincipalRef(id=bob.id, name=bob.name, type=bob.type),
        AllowAllAuthorization(),
    )
    GrantService(world.session, AllowAllAuthorization()).revoke(
        world.pat(boss), _admin_grant_of(world, bob)
    )
    with pytest.raises(PermissionDenied, match="lakana recovery create bob"):
        world.redeem(link)
    assert world.links.describe(parts(link.url)[0]).state == "withdrawn"


def test_admin_granted_to_a_deactivated_person_counts_as_held(world: World) -> None:
    """ADMIN given while the person is deactivated still marks them as an administrator."""
    boss = world.person("boss", admin=True)
    bob = world.person("bob")
    PrincipalService(world.session, AllowAllAuthorization()).deactivate_principal(
        world.operator, "bob"
    )
    GrantService(world.session, AllowAllAuthorization()).grant(
        world.operator, principal="bob", role=Role.ADMIN, kind=SecurableKind.INSTANCE
    )
    world.session.refresh(bob)
    assert bob.held_admin
    with pytest.raises(ConflictError, match="deactivated"):
        world.links.issue_recovery(world.pat(boss), principal_name="bob")


def test_a_person_who_never_held_admin_is_recoverable_by_an_administrator(world: World) -> None:
    """Other grants never mark a person as an administrator."""
    boss = world.person("boss", admin=True)
    bob = world.person("bob")
    GrantService(world.session, AllowAllAuthorization()).grant(
        world.operator, principal="bob", role=Role.VIEWER, kind=SecurableKind.INSTANCE
    )
    world.session.refresh(bob)
    assert not bob.held_admin
    assert world.links.issue_recovery(world.pat(boss), principal_name="bob")
