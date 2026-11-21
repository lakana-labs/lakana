"""Linking people to the identity provider, unlinking them, and the break-glass count (#32, PR 2).

`lakana_core.identity.oidc_links`: a person Lakana knows links one provider identity; the server
operator's account and service accounts never do; the last administrator who signs in with a
passkey is never linked, and no change that lowers that count may take it to none while a linked
administrator remains.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from oidc_link_test_helpers import ISSUER, RP_ID, identity, store_passkey
from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.auth import AllowAllAuthorization, PrincipalRef, SecurableKind
from lakana_core.bootstrap import bootstrap_if_needed, ensure_bootstrap_admin_grant
from lakana_core.errors import (
    ConflictError,
    NotFoundError,
    OidcSignInRefused,
    PermissionDenied,
    StepUpRequired,
)
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.grants.break_glass import LAST_UNLINKED_ADMIN_MESSAGE
from lakana_core.grants.roles import Role
from lakana_core.grants.service import GrantService
from lakana_core.identity.groups import GroupService
from lakana_core.identity.notices import NoticeService
from lakana_core.identity.oidc_links import (
    OidcLinkService,
    is_linked,
    linked_count,
    unlinked_admin_count,
)
from lakana_core.identity.passkeys import PasskeyService
from lakana_core.identity.pats import PatService
from lakana_core.identity.principals import PrincipalService
from lakana_core.identity.sessions import SessionService
from lakana_core.models import (
    AuditEvent,
    AuditEventType,
    BrowserSession,
    Grant,
    Passkey,
    PersonalAccessToken,
    Principal,
    PrincipalType,
    SignInNotice,
)

START = datetime(2026, 11, 20, 9, 0, tzinfo=UTC)


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
    """

    def __init__(self, session: Session) -> None:
        self.session = session
        self.clock = _Clock()
        bootstrap_if_needed(session, "admin", key_id="opkey", secret="opsecret")
        self.authz = GrantAuthorization(session)
        self.sessions = SessionService(session, self.authz, clock=self.clock)
        self.links = OidcLinkService(session, self.authz, clock=self.clock, bootstrap_name="admin")

    def person(
        self,
        name: str,
        *,
        admin: bool = False,
        passkey: bool = True,
        kind: PrincipalType = PrincipalType.USER,
    ) -> Principal:
        """Insert a person, optionally an administrator with a passkey.

        Parameters
        ----------
        name
            The name.
        admin
            Whether to grant ADMIN at instance scope.
        passkey
            Whether to give them a working passkey.
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
            ensure_bootstrap_admin_grant(self.session, self.ref(row), AllowAllAuthorization())
        if passkey:
            self.passkey(row)
        return row

    def passkey(self, row: Principal, *, disabled: bool = False) -> Passkey:
        """Store a passkey for a person.

        Parameters
        ----------
        row
            The person.
        disabled
            Whether it was disabled as a copy.

        Returns
        -------
        Passkey
            The row.
        """
        return store_passkey(self.session, row, self.clock(), disabled=disabled)

    @staticmethod
    def ref(row: Principal) -> PrincipalRef:
        """Refer to a person as a token would.

        Parameters
        ----------
        row
            The person.

        Returns
        -------
        PrincipalRef
            The reference.
        """
        return PrincipalRef(id=row.id, name=row.name, type=row.type)

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

    def link(self, row: Principal, subject: str | None = None) -> None:
        """Link a person through the service and commit.

        Parameters
        ----------
        row
            The person.
        subject
            The ``sub``; the person's name by default.
        """
        self.links.link(row.id, identity(subject or row.name), via="profile")
        self.session.commit()

    def events(self, kind: AuditEventType) -> list[dict]:
        """Read the details of every event of one type, in order.

        Parameters
        ----------
        kind
            The type.

        Returns
        -------
        list of dict
            The details.
        """
        return [
            dict(detail)
            for detail in self.session.execute(
                select(AuditEvent.detail)
                .where(AuditEvent.event_type == kind)
                .order_by(AuditEvent.occurred_at)
            ).scalars()
        ]


@pytest.fixture
def world(session: Session) -> World:
    """Provide a bootstrapped instance.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    World
        The services.
    """
    return World(session)


# -- link ---------------------------------------------------------------------------------------


def test_link_sets_the_identity_and_audits(world: World) -> None:
    alice = world.person("alice")
    world.links.link(alice.id, identity("s-alice"), via="invite")
    world.session.commit()
    world.session.refresh(alice)
    assert (alice.oidc_issuer, alice.oidc_subject) == (ISSUER, "s-alice")
    assert (alice.oidc_email, alice.oidc_name) == ("s-alice@example.com", "s-alice")
    assert alice.last_provider_sign_in_at == START
    assert is_linked(alice)
    assert linked_count(world.session) == 1
    [event] = world.events(AuditEventType.PRINCIPAL_OIDC_LINKED)
    assert event == {"principal": "alice", "iss": ISSUER, "sub": "s-alice", "via": "invite"}


def test_bootstrap_principal_can_never_be_linked(world: World) -> None:
    bootstrap = world.session.execute(
        select(Principal).where(Principal.name == "admin")
    ).scalar_one()
    with pytest.raises(OidcSignInRefused) as refused:
        world.links.link(bootstrap.id, identity(), via="profile")
    assert refused.value.reason == "cannot_link_operator"
    assert not is_linked(bootstrap)


def test_a_service_account_can_never_be_linked(world: World) -> None:
    robot = world.person("robot", kind=PrincipalType.SERVICE_ACCOUNT, passkey=False)
    with pytest.raises(OidcSignInRefused) as refused:
        world.links.link(robot.id, identity(), via="profile")
    assert refused.value.reason == "cannot_link_operator"


def test_an_unknown_person_cannot_be_linked(world: World) -> None:
    with pytest.raises(NotFoundError):
        world.links.link(uuid.uuid4(), identity(), via="profile")


def test_linking_twice_is_already_linked(world: World) -> None:
    alice = world.person("alice")
    world.link(alice)
    with pytest.raises(OidcSignInRefused) as refused:
        world.links.link(alice.id, identity("another"), via="profile")
    assert refused.value.reason == "already_linked"


def test_two_accounts_linking_one_identity_one_wins(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lakana_core.identity import oidc_links

    alice, mallory = world.person("alice"), world.person("mallory")
    world.link(alice, "shared")
    # Mallory's link looked before Alice's committed and found nobody: the unique constraint
    # decides, in a nested transaction, and the loser gets the refusal, never a database error.
    monkeypatch.setattr(oidc_links, "principal_for", lambda session, found: None)
    with pytest.raises(OidcSignInRefused) as refused:
        world.links.link(mallory.id, identity("shared"), via="profile")
    assert refused.value.reason == "identity_taken"
    world.session.commit()
    rows = world.session.execute(
        select(Principal.name).where(Principal.oidc_subject == "shared")
    ).scalars()
    assert list(rows) == ["alice"]
    assert len(world.events(AuditEventType.PRINCIPAL_OIDC_LINKED)) == 1


def test_identity_taken_names_no_account(world: World) -> None:
    alice, mallory = world.person("alice"), world.person("mallory")
    world.link(alice, "shared")
    with pytest.raises(OidcSignInRefused) as refused:
        world.links.link(mallory.id, identity("shared"), via="profile")
    assert refused.value.reason == "identity_taken"
    assert "alice" not in str(refused.value) and "mallory" not in str(refused.value)
    world.session.rollback()
    assert world.events(AuditEventType.PRINCIPAL_OIDC_LINKED) == [
        {"principal": "alice", "iss": ISSUER, "sub": "shared", "via": "profile"}
    ]


def test_last_unlinked_admin_is_refused(world: World) -> None:
    ann, bob = world.person("ann", admin=True), world.person("bob", admin=True)
    assert unlinked_admin_count(world.session) == 2
    world.link(ann)
    assert unlinked_admin_count(world.session) == 1
    with pytest.raises(OidcSignInRefused) as refused:
        world.links.link(bob.id, identity("bob"), via="profile")
    assert refused.value.reason == "last_unlinked_admin"
    world.session.rollback()
    assert unlinked_admin_count(world.session) == 1
    # A person who is no administrator links whatever the count.
    world.link(world.person("carol"))


def test_admin_through_a_group_counts(world: World) -> None:
    ann = world.person("ann", admin=True)
    bob = world.person("bob")
    operator = world.ref(
        world.session.execute(select(Principal).where(Principal.name == "admin")).scalar_one()
    )
    groups = GroupService(world.session, AllowAllAuthorization())
    groups.create_group(operator, name="admins")
    groups.add_member(operator, "admins", "bob")
    GrantService(world.session, AllowAllAuthorization()).grant(
        operator, group="admins", role=Role.ADMIN, kind=SecurableKind.INSTANCE
    )
    assert unlinked_admin_count(world.session) == 2
    world.link(ann)
    with pytest.raises(OidcSignInRefused) as refused:
        world.links.link(bob.id, identity("bob"), via="profile")
    assert refused.value.reason == "last_unlinked_admin"


def test_admin_without_a_working_passkey_does_not_count(world: World) -> None:
    ann = world.person("ann", admin=True)
    bob = world.person("bob", admin=True, passkey=False)
    world.passkey(bob, disabled=True)
    assert unlinked_admin_count(world.session) == 1
    with pytest.raises(OidcSignInRefused) as refused:
        world.links.link(ann.id, identity("ann"), via="profile")
    assert refused.value.reason == "last_unlinked_admin"
    world.session.rollback()
    # Bob has no working passkey, so linking him lowers nothing.
    world.link(bob)


def test_a_deactivated_admin_does_not_count(world: World) -> None:
    ann = world.person("ann", admin=True)
    bob = world.person("bob", admin=True)
    bob.is_active = False
    world.session.commit()
    with pytest.raises(OidcSignInRefused):
        world.links.link(ann.id, identity("ann"), via="profile")


def test_the_count_is_taken_under_the_admin_lock(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lakana_core.identity import oidc_links

    calls: list[str] = []
    real = oidc_links.breaks
    monkeypatch.setattr(oidc_links, "lock_admin_count", lambda s: calls.append("lock"))
    monkeypatch.setattr(
        oidc_links,
        "breaks",
        lambda *a, **k: calls.append("count") or real(*a, **k),
    )
    world.person("bob", admin=True)
    world.link(world.person("ann", admin=True))
    assert calls[0] == "lock" and "count" in calls


# -- unlink ------------------------------------------------------------------------------------


def _linked_with_everything(world: World) -> tuple[Principal, PrincipalRef]:
    """Make a linked person with a passkey, a token and a session, and an admin to act.

    Parameters
    ----------
    world
        The test's world.

    Returns
    -------
    tuple of (Principal, PrincipalRef)
        The person and the acting administrator's fresh session.
    """
    world.person("ann", admin=True)
    carol = world.person("carol")
    PatService(world.session, AllowAllAuthorization()).issue(world.ref(carol), "carol", name="t")
    world.signed_in(carol)
    world.link(carol)
    ann = world.session.execute(select(Principal).where(Principal.name == "ann")).scalar_one()
    return carol, world.signed_in(ann)


def test_unlink_deletes_passkeys_revokes_tokens_ends_sessions(world: World) -> None:
    carol, ann = _linked_with_everything(world)
    world.links.unlink(ann, "carol")
    world.session.refresh(carol)
    assert not is_linked(carol)
    assert carol.oidc_email is None and carol.last_provider_sign_in_at is None
    assert (
        world.session.execute(select(Passkey).where(Passkey.principal_id == carol.id)).all() == []
    )
    tokens = world.session.execute(
        select(PersonalAccessToken).where(PersonalAccessToken.principal_id == carol.id)
    ).scalars()
    assert all(token.revoked_at == START for token in tokens)
    open_sessions = world.session.execute(
        select(BrowserSession).where(
            BrowserSession.principal_id == carol.id, BrowserSession.ended_at.is_(None)
        )
    ).all()
    assert open_sessions == []
    [event] = world.events(AuditEventType.PRINCIPAL_OIDC_UNLINKED)
    assert event == {
        "principal": "carol",
        "iss": ISSUER,
        "sub": "carol",
        "reason": "administrator",
        "actor": "ann",
        "passkeys_deleted": 1,
        "tokens_revoked": 1,
        "sessions_ended": 1,
    }
    [revoked] = world.events(AuditEventType.PAT_REVOKED)
    assert (revoked["subject"], revoked["source"]) == ("carol", "oidc_unlinked")


def test_unlink_needs_step_up_and_admin(world: World) -> None:
    carol, ann = _linked_with_everything(world)
    stale = world.signed_in(
        world.session.execute(select(Principal).where(Principal.name == "ann")).scalar_one(),
        touched=False,
    )
    with pytest.raises(StepUpRequired):
        world.links.unlink(stale, "carol")
    dave = world.person("dave")
    with pytest.raises(PermissionDenied):
        world.links.unlink(world.signed_in(dave), "carol")
    assert is_linked(carol)
    with pytest.raises(NotFoundError):
        world.links.unlink(ann, "nobody")


def test_unlinking_someone_not_linked_is_409(world: World) -> None:
    world.person("dave")
    ann = world.signed_in(world.person("ann", admin=True))
    with pytest.raises(ConflictError):
        world.links.unlink(ann, "dave")


def test_unlinking_oneself_is_refused(world: World) -> None:
    world.person("bob", admin=True)
    ann = world.person("ann", admin=True)
    world.link(ann)
    with pytest.raises(ConflictError) as refused:
        world.links.unlink(world.signed_in(ann), "ann")
    assert "recovery link" in str(refused.value)


def test_operator_recovery_unlink_notifies_every_admin(world: World) -> None:
    world.person("bob", admin=True)
    ann = world.person("ann", admin=True)
    world.link(ann)
    link_id = uuid.uuid4()
    operator = world.ref(
        world.session.execute(select(Principal).where(Principal.name == "admin")).scalar_one()
    )
    world.links.unlink_for_operator_recovery(ann, link_id, operator=operator)
    world.session.commit()
    assert not is_linked(ann)
    [event] = world.events(AuditEventType.PRINCIPAL_OIDC_UNLINKED)
    assert event == {
        "principal": "ann",
        "iss": ISSUER,
        "sub": "ann",
        "reason": "operator_recovery",
        "link_id": str(link_id),
        "tokens_revoked": 0,
    }
    rows = world.session.execute(
        select(Principal.name, SignInNotice.detail)
        .join(Principal, Principal.id == SignInNotice.recipient_id)
        .where(SignInNotice.kind == "oidc_unlinked")
    ).all()
    recipients = {name: detail for name, detail in rows}
    assert set(recipients) == {"admin", "ann", "bob"}
    assert recipients["bob"] == {"name": "ann", "link_id": str(link_id)}


# -- the break-glass count on every change that lowers it ---------------------------------------


def _one_unlinked_admin_left(world: World) -> tuple[Principal, Principal, PrincipalRef]:
    """Make a linked administrator and the one unlinked administrator left.

    Parameters
    ----------
    world
        The test's world.

    Returns
    -------
    tuple of (Principal, Principal, PrincipalRef)
        The unlinked administrator, the linked one, and the linked one acting by token.
    """
    unlinked = world.person("una", admin=True)
    linked = world.person("lin", admin=True)
    world.link(linked)
    assert unlinked_admin_count(world.session) == 1
    return unlinked, linked, world.ref(linked)


def test_deactivating_the_last_unlinked_admin_is_refused(world: World) -> None:
    unlinked, _, linked = _one_unlinked_admin_left(world)
    with pytest.raises(ConflictError) as refused:
        PrincipalService(world.session, world.authz).deactivate_principal(linked, "una")
    assert str(refused.value) == LAST_UNLINKED_ADMIN_MESSAGE
    world.session.rollback()
    assert unlinked.is_active


def test_revoking_the_last_unlinked_admins_role_is_refused(world: World) -> None:
    unlinked, _, linked = _one_unlinked_admin_left(world)
    grant = world.session.execute(
        select(Grant).where(Grant.principal_id == unlinked.id, Grant.role == Role.ADMIN)
    ).scalar_one()
    with pytest.raises(ConflictError, match="passkey"):
        GrantService(world.session, world.authz).revoke(linked, grant.id)


def test_removing_the_last_unlinked_admin_from_the_admins_group_is_refused(world: World) -> None:
    operator = world.ref(
        world.session.execute(select(Principal).where(Principal.name == "admin")).scalar_one()
    )
    lin = world.person("lin", admin=True)
    una = world.person("una")
    groups = GroupService(world.session, AllowAllAuthorization())
    groups.create_group(operator, name="admins")
    groups.add_member(operator, "admins", "una")
    GrantService(world.session, AllowAllAuthorization()).grant(
        operator, group="admins", role=Role.ADMIN, kind=SecurableKind.INSTANCE
    )
    world.link(lin)
    assert unlinked_admin_count(world.session) == 1
    with pytest.raises(ConflictError, match="passkey"):
        GroupService(world.session, world.authz).remove_member(world.ref(lin), "admins", "una")
    assert una.is_active


def test_deleting_the_last_unlinked_admins_last_passkey_is_refused(world: World) -> None:
    unlinked, linked, _ = _one_unlinked_admin_left(world)
    passkeys = PasskeyService(
        world.session,
        world.authz,
        world.sessions,
        NoticeService(world.session, clock=world.clock),
        rp_id=RP_ID,
        origin="http://localhost:8000",
        clock=world.clock,
    )
    [key] = world.session.execute(
        select(Passkey).where(Passkey.principal_id == unlinked.id)
    ).scalars()
    with pytest.raises(ConflictError) as refused:
        passkeys.remove(world.ref(linked), "una", key.id)
    assert str(refused.value) == LAST_UNLINKED_ADMIN_MESSAGE
    # With a second working passkey the first may go.
    world.passkey(unlinked)
    passkeys.remove(world.ref(linked), "una", key.id)
    assert unlinked_admin_count(world.session) == 1


def test_lowering_the_count_is_allowed_while_no_administrator_is_linked(world: World) -> None:
    # An instance that does not use the provider keeps today's rules: nobody is locked out by
    # a provider outage when nobody signs in through it.
    world.person("una", admin=True)
    bob = world.person("bob", admin=True, passkey=False)
    PrincipalService(world.session, world.authz).deactivate_principal(world.ref(bob), "una")
    assert unlinked_admin_count(world.session) == 0


def test_a_rule_already_broken_blocks_nothing(world: World) -> None:
    # Broken before any check could refuse it (a passkey disabled as a copy, say): refusing
    # unrelated changes would help nobody.
    lin = world.person("lin", admin=True, passkey=False)
    lin.oidc_issuer, lin.oidc_subject = ISSUER, "lin"
    world.session.commit()
    world.person("other", admin=True, passkey=False)
    assert unlinked_admin_count(world.session) == 0
    PrincipalService(world.session, world.authz).deactivate_principal(world.ref(lin), "other")


def test_lowering_the_count_from_two_is_allowed(world: World) -> None:
    world.person("una", admin=True)
    world.person("uno", admin=True)
    lin = world.person("lin", admin=True)
    world.link(lin)
    PrincipalService(world.session, world.authz).deactivate_principal(world.ref(lin), "una")
    assert unlinked_admin_count(world.session) == 1


def test_the_count_of_nobody_reads_nothing(world: World) -> None:
    from lakana_core.grants.break_glass import breaks, unlinked_admin_ids

    una = world.person("una", admin=True)
    assert unlinked_admin_ids(world.session, []) == set()
    # Everybody gone: no linked administrator remains to be locked out.
    assert not breaks(world.session, before=[una.id], after=[])


# -- the rule is an invariant: raising the linked side is checked too (review of #780) ----------


def _operator(world: World) -> PrincipalRef:
    """Refer to the bootstrap principal.

    Parameters
    ----------
    world
        The test's world.

    Returns
    -------
    PrincipalRef
        The operator's principal.
    """
    return world.ref(
        world.session.execute(select(Principal).where(Principal.name == "admin")).scalar_one()
    )


def _linked_non_admin(world: World) -> Principal:
    """Make a linked person who is no administrator.

    Parameters
    ----------
    world
        The test's world.

    Returns
    -------
    Principal
        The person.
    """
    lin = world.person("lin")
    world.link(lin)
    return lin


def test_the_reviewers_counterexample_deactivate_then_grant(world: World) -> None:
    """The only unlinked administrator goes first, then a linked person becomes one: refused."""
    world.person("una", admin=True)
    _linked_non_admin(world)
    operator = _operator(world)
    # No administrator is linked yet, so the rule holds without Una.
    PrincipalService(world.session, world.authz).deactivate_principal(operator, "una")
    with pytest.raises(ConflictError) as refused:
        GrantService(world.session, world.authz).grant(
            operator, principal="lin", role=Role.ADMIN, kind=SecurableKind.INSTANCE
        )
    assert str(refused.value) == LAST_UNLINKED_ADMIN_MESSAGE
    world.session.rollback()
    assert not world.session.execute(
        select(Grant).where(Grant.principal_id == world.ref(_named(world, "lin")).id)
    ).first()


def test_the_reviewers_counterexample_grant_then_deactivate(world: World) -> None:
    """The other order: the grant goes through, the deactivation is refused."""
    world.person("una", admin=True)
    _linked_non_admin(world)
    operator = _operator(world)
    GrantService(world.session, world.authz).grant(
        operator, principal="lin", role=Role.ADMIN, kind=SecurableKind.INSTANCE
    )
    with pytest.raises(ConflictError, match="only administrators who sign in through"):
        PrincipalService(world.session, world.authz).deactivate_principal(operator, "una")


def test_adding_a_linked_person_to_the_admins_group_is_refused(world: World) -> None:
    _linked_non_admin(world)
    operator = _operator(world)
    groups = GroupService(world.session, AllowAllAuthorization())
    groups.create_group(operator, name="admins")
    GrantService(world.session, AllowAllAuthorization()).grant(
        operator, group="admins", role=Role.ADMIN, kind=SecurableKind.INSTANCE
    )
    with pytest.raises(ConflictError, match="only administrators who sign in through"):
        GroupService(world.session, world.authz).add_member(operator, "admins", "lin")
    # A group without ADMIN takes anyone.
    groups.create_group(operator, name="readers")
    GroupService(world.session, world.authz).add_member(operator, "readers", "lin")


def test_granting_admin_to_a_group_with_a_linked_member_is_refused(world: World) -> None:
    _linked_non_admin(world)
    operator = _operator(world)
    groups = GroupService(world.session, AllowAllAuthorization())
    groups.create_group(operator, name="admins")
    groups.add_member(operator, "admins", "lin")
    with pytest.raises(ConflictError, match="only administrators who sign in through"):
        GrantService(world.session, world.authz).grant(
            operator, group="admins", role=Role.ADMIN, kind=SecurableKind.INSTANCE
        )


def test_a_linked_administrator_may_join_while_an_unlinked_one_remains(world: World) -> None:
    world.person("una", admin=True)
    _linked_non_admin(world)
    GrantService(world.session, world.authz).grant(
        _operator(world), principal="lin", role=Role.ADMIN, kind=SecurableKind.INSTANCE
    )
    assert unlinked_admin_count(world.session) == 1


def test_a_deactivated_linked_person_may_be_granted_admin(world: World) -> None:
    lin = _linked_non_admin(world)
    lin.is_active = False
    world.session.commit()
    GrantService(world.session, world.authz).grant(
        _operator(world), principal="lin", role=Role.ADMIN, kind=SecurableKind.INSTANCE
    )


def test_only_passkeys_of_this_address_count(world: World) -> None:
    """A passkey of an earlier public address signs nobody in, so it is no way in."""
    from lakana_core.grants.break_glass import current_rp_id, record_rp_id

    assert current_rp_id(world.session) is None
    una = world.person("una", admin=True)
    assert unlinked_admin_count(world.session) == 1
    record_rp_id(world.session, "lakana.example.org")
    world.session.commit()
    assert current_rp_id(world.session) == "lakana.example.org"
    assert unlinked_admin_count(world.session) == 0
    record_rp_id(world.session, RP_ID)
    record_rp_id(world.session, RP_ID)
    world.session.commit()
    assert unlinked_admin_count(world.session) == 1
    assert una.is_active


def test_a_link_made_meanwhile_is_not_overwritten(world: World) -> None:
    """Two finishes for one person: the second finds the person linked and is refused."""
    from sqlalchemy import update

    alice = world.person("alice")
    world.session.get(Principal, alice.id)
    # Another transaction links Alice after this session read her row.
    world.session.execute(
        update(Principal)
        .where(Principal.id == alice.id)
        .values(oidc_issuer=ISSUER, oidc_subject="first")
        .execution_options(synchronize_session=False)
    )
    with pytest.raises(OidcSignInRefused) as refused:
        world.links.link(alice.id, identity("second"), via="recovery")
    assert refused.value.reason == "already_linked"
    subject = world.session.execute(
        select(Principal.oidc_subject).where(Principal.id == alice.id)
    ).scalar_one()
    assert subject == "first"


def _named(world: World, name: str) -> Principal:
    """Read a principal by name.

    Parameters
    ----------
    world
        The test's world.
    name
        The name.

    Returns
    -------
    Principal
        The row.
    """
    return world.session.execute(select(Principal).where(Principal.name == name)).scalar_one()


def test_the_sign_in_lookup_reads_the_link_afresh_under_its_lock(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #780: a sign-in sees an unlink that committed while it looked."""
    from lakana_core.identity import oidc_links
    from lakana_core.identity.oidc_links import linked_principal_for_sign_in

    alice = world.person("alice")
    world.link(alice, "s-alice")
    assert linked_principal_for_sign_in(world.session, identity("s-alice")) == alice
    assert linked_principal_for_sign_in(world.session, identity("nobody")) is None
    # The lookup found Alice, then an unlink cleared her link before the row lock was taken.
    monkeypatch.setattr(oidc_links, "principal_for", lambda session, found: alice)
    alice.oidc_subject = None
    world.session.commit()
    assert linked_principal_for_sign_in(world.session, identity("s-alice")) is None


def test_a_changed_address_is_reported_with_the_count_at_the_new_one(world: World) -> None:
    """Review of #780: the start-up warning names the new count and the operator's way in."""
    from lakana_core.grants.break_glass import address_change_warning, record_rp_id

    assert record_rp_id(world.session, RP_ID) is None
    assert record_rp_id(world.session, RP_ID) is None
    assert record_rp_id(world.session, "new.example.org") == RP_ID
    assert address_change_warning(None, "x", linked=3, unlinked_admins=0) is None
    assert address_change_warning("a", "b", linked=0, unlinked_admins=0) is None
    kept = address_change_warning("a", "b", linked=2, unlinked_admins=1)
    assert kept is not None and "1 administrator(s)" in kept and "recovery" not in kept
    lost = address_change_warning("a", "b", linked=2, unlinked_admins=0)
    assert lost is not None and "lakana recovery create" in lost
