"""Using an invite or a recovery link through the identity provider (#32, PR 2).

`LinkService.check_open` checks a link at the provider flow's start without using it;
`LinkService.claim_for_provider` uses it at the finish, in the transaction that links the person
and starts the session, with the same conditional update as `LinkService.redeem`.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import pytest
from oidc_link_test_helpers import ISSUER, identity, store_passkey
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_identity_links import World, _Clock, parts

from lakana_core.errors import LinkUnusableError, OidcSignInRefused
from lakana_core.identity.links import Redeemed
from lakana_core.identity.oidc_links import OidcLinkService, is_linked
from lakana_core.models import (
    AuditEventType,
    BrowserSession,
    Passkey,
    Principal,
    SignInLink,
    SignInNotice,
)


def _used_at(world: World, link_id: uuid.UUID) -> datetime | None:
    """Read when a link was used.

    Parameters
    ----------
    world
        The test's world.
    link_id
        The link.

    Returns
    -------
    datetime or None
        Its `used_at`.
    """
    row = world.session.get(SignInLink, link_id)
    assert row is not None
    return row.used_at


def _claim(world: World, link_id: uuid.UUID | None, subject: str = "sub-1") -> Redeemed:
    """Use a link through the provider, as the provider's finish does.

    Parameters
    ----------
    world
        The test's world.
    link_id
        The link's row id.
    subject
        The ``sub`` the provider verified.

    Returns
    -------
    Redeemed
        What the link started.
    """
    return world.links.claim_for_provider(
        link_id,
        identity(subject),
        replacing=None,
        peer="127.0.0.1",
        user_agent="pytest",
    )


def _events(world: World, kind: AuditEventType) -> list[dict]:
    """Read the details of every event of one type, in order.

    Parameters
    ----------
    world
        The test's world.
    kind
        The type.

    Returns
    -------
    list of dict
        The details.
    """
    from lakana_core.models import AuditEvent

    return [
        dict(detail)
        for detail in world.session.execute(
            select(AuditEvent.detail)
            .where(AuditEvent.event_type == kind)
            .order_by(AuditEvent.occurred_at)
        ).scalars()
    ]


def _passkey(world: World, row: Principal) -> None:
    """Give a person a passkey.

    Parameters
    ----------
    world
        The test's world.
    row
        The person.
    """
    store_passkey(world.session, row, world.clock())


def _passkeys(world: World, row: Principal) -> int:
    """Count a person's stored passkeys.

    Parameters
    ----------
    world
        The test's world.
    row
        The person.

    Returns
    -------
    int
        How many.
    """
    return len(world.session.execute(select(Passkey).where(Passkey.principal_id == row.id)).all())


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
        The services and the operator.
    """
    return World(session, _Clock())


def test_check_open_does_not_use_the_link(world: World) -> None:
    link = world.links.issue_invite(world.operator, name="carol", display_name=None)
    public_id, secret = parts(link.url)
    for _ in range(2):
        assert world.links.check_open(public_id, secret).id == link.id
    assert _used_at(world, link.id) is None


def test_start_with_a_wrong_secret_is_like_unknown(world: World) -> None:
    link = world.links.issue_invite(world.operator, name="carol", display_name=None)
    public_id, _ = parts(link.url)
    with pytest.raises(LinkUnusableError) as refused:
        world.links.check_open(public_id, "wrong")
    assert refused.value.state == "unknown"
    with pytest.raises(LinkUnusableError) as refused:
        world.links.check_open("nope", "wrong")
    assert refused.value.state == "unknown"
    world.links.withdraw(world.operator, link.id)
    with pytest.raises(LinkUnusableError) as refused:
        world.links.check_open(public_id, parts(link.url)[1])
    assert refused.value.state == "withdrawn"


def test_invite_with_the_provider_creates_and_links_without_a_passkey(world: World) -> None:
    link = world.links.issue_invite(world.operator, name="carol", display_name="Carol C.")
    redeemed = _claim(world, link.id)
    assert (redeemed.kind, redeemed.needs_passkey, redeemed.offer_passkey) == (
        "invite",
        False,
        False,
    )
    context = redeemed.started.principal.session
    assert context is not None and context.via == "oidc"
    # A provider sign-in is no step-up touch (PR 1).
    assert context.last_touch_at is None
    carol = world.session.execute(select(Principal).where(Principal.name == "carol")).scalar_one()
    assert (carol.oidc_issuer, carol.oidc_subject) == (ISSUER, "sub-1")
    assert carol.display_name == "Carol C."
    assert _passkeys(world, carol) == 0
    [created] = _events(world, AuditEventType.PRINCIPAL_CREATED)[-1:]
    assert (created["principal"], created["via"]) == ("carol", "invite")
    [linked] = _events(world, AuditEventType.PRINCIPAL_OIDC_LINKED)
    assert (linked["principal"], linked["via"]) == ("carol", "invite")
    row = world.session.get(SignInLink, link.id)
    assert row is not None and row.used_at is not None and row.principal_id == carol.id
    used = _events(world, AuditEventType.SIGN_IN_LINK_USED)
    assert [(e["kind"], e["principal"]) for e in used] == [("invite", "carol")]


def test_invite_withdrawn_after_start_creates_nobody(world: World) -> None:
    link = world.links.issue_invite(world.operator, name="carol", display_name=None)
    public_id, secret = parts(link.url)
    world.links.check_open(public_id, secret)
    world.links.withdraw(world.operator, link.id)
    with pytest.raises(OidcSignInRefused) as refused:
        _claim(world, link.id)
    assert refused.value.reason == "link_unusable"
    assert world.session.execute(select(Principal).where(Principal.name == "carol")).all() == []
    assert _events(world, AuditEventType.PRINCIPAL_OIDC_LINKED) == []


def test_invite_used_twice_one_wins(world: World) -> None:
    link = world.links.issue_invite(world.operator, name="carol", display_name=None)
    _claim(world, link.id)
    with pytest.raises(OidcSignInRefused) as refused:
        _claim(world, link.id, "sub-2")
    assert refused.value.reason == "link_unusable"
    with pytest.raises(LinkUnusableError):
        world.redeem(link)
    assert len(_events(world, AuditEventType.PRINCIPAL_OIDC_LINKED)) == 1


def test_an_unknown_link_id_is_unusable(world: World) -> None:
    for link_id in (uuid.uuid4(), None):
        with pytest.raises(OidcSignInRefused) as refused:
            _claim(world, link_id)
        assert refused.value.reason == "link_unusable"
    # A setup link is never used through the provider.
    setup = world.links.issue_setup(world.operator)
    with pytest.raises(OidcSignInRefused):
        _claim(world, setup.id)


def test_invite_whose_identity_is_taken_creates_nobody(world: World) -> None:
    alice = world.person("alice")
    OidcLinkService(world.session, world.authz, bootstrap_name="admin").link(
        alice.id, identity("sub-1"), via="profile"
    )
    world.session.commit()
    link = world.links.issue_invite(world.operator, name="carol", display_name=None)
    with pytest.raises(OidcSignInRefused) as refused:
        _claim(world, link.id)
    assert refused.value.reason == "identity_taken"
    assert world.session.execute(select(Principal).where(Principal.name == "carol")).all() == []
    # The invite was not used: the person may try again with their own identity.
    row = world.session.get(SignInLink, link.id)
    assert row is not None and row.used_at is None
    _claim(world, link.id, "sub-2")


def test_invite_whose_issuer_lapsed_is_closed(world: World) -> None:
    from lakana_core.errors import PermissionDenied
    from lakana_core.models import Grant

    ann = world.person("ann", admin=True)
    by_ann = world.signed_in(ann)
    link = world.links.issue_invite(by_ann, name="carol", display_name=None)
    for grant in world.session.execute(select(Grant).where(Grant.principal_id == ann.id)).scalars():
        world.session.delete(grant)
    world.session.commit()
    with pytest.raises(PermissionDenied):
        _claim(world, link.id)
    row = world.session.get(SignInLink, link.id)
    assert row is not None and row.withdrawn_at is not None


def test_recovery_of_a_linked_person_needs_the_same_identity(world: World) -> None:
    dave = world.person("dave")
    _passkey(world, dave)
    OidcLinkService(world.session, world.authz, bootstrap_name="admin").link(
        dave.id, identity("dave"), via="profile"
    )
    world.session.commit()
    old = world.signed_in(dave)
    assert old.session is not None
    link = world.links.issue_recovery(world.operator, principal_name="dave")
    with pytest.raises(OidcSignInRefused) as refused:
        _claim(world, link.id, "someone-else")
    assert refused.value.reason == "identity_mismatch"
    assert _used_at(world, link.id) is None
    redeemed = _claim(world, link.id, "dave")
    assert redeemed.kind == "recovery" and not redeemed.needs_passkey
    ended = world.session.execute(
        select(BrowserSession.end_reason).where(BrowserSession.key_id == old.session.key_id)
    ).scalar_one()
    assert ended == "recovery"
    # Recovery through the provider deletes no passkey: it does not bring one back either.
    assert _passkeys(world, dave) == 1
    kinds = world.session.execute(
        select(SignInNotice.kind).where(SignInNotice.subject_id == dave.id)
    ).scalars()
    assert set(kinds) == {"recovery_used"}
    world.session.refresh(dave)
    assert dave.last_provider_sign_in_at == world.clock.now
    assert len(_events(world, AuditEventType.PRINCIPAL_OIDC_LINKED)) == 1


def test_recovery_of_an_unlinked_person_may_link(world: World) -> None:
    erin = world.person("erin")
    _passkey(world, erin)
    link = world.links.issue_recovery(world.operator, principal_name="erin")
    redeemed = _claim(world, link.id, "erin")
    assert redeemed.started.principal.name == "erin"
    world.session.refresh(erin)
    assert is_linked(erin)
    [linked] = _events(world, AuditEventType.PRINCIPAL_OIDC_LINKED)
    assert linked["via"] == "recovery"
    assert _passkeys(world, erin) == 1


def test_recovery_of_someone_who_became_an_admin_is_closed(world: World) -> None:
    from lakana_core.bootstrap import ensure_bootstrap_admin_grant
    from lakana_core.errors import PermissionDenied

    ann = world.person("ann", admin=True)
    frank = world.person("frank")
    link = world.links.issue_recovery(world.signed_in(ann), principal_name="frank")
    from lakana_core.auth import AllowAllAuthorization, PrincipalRef

    ensure_bootstrap_admin_grant(
        world.session,
        PrincipalRef(id=frank.id, name=frank.name, type=frank.type),
        AllowAllAuthorization(),
    )
    with pytest.raises(PermissionDenied):
        _claim(world, link.id, "frank")
    assert not is_linked(frank)


def test_describe_says_provider_only_for_a_linked_person(world: World) -> None:
    dave = world.person("dave")
    link = world.links.issue_recovery(world.operator, principal_name="dave")
    public_id, _ = parts(link.url)
    assert world.links.describe(public_id).provider_only is False
    ann = world.person("ann", admin=True)
    OidcLinkService(world.session, world.authz, bootstrap_name="admin").link(
        dave.id, identity("dave"), via="profile"
    )
    world.session.commit()
    by_ann = world.links.issue_recovery(world.signed_in(ann), principal_name="dave")
    assert world.links.describe(parts(by_ann.url)[0]).provider_only is True
    # The operator's recovery link is the break-glass path: it keeps the passkey way.
    by_operator = world.links.issue_recovery(world.operator, principal_name="dave")
    assert world.links.describe(parts(by_operator.url)[0]).provider_only is False
    invite = world.links.issue_invite(world.operator, name="gina", display_name=None)
    assert world.links.describe(parts(invite.url)[0]).provider_only is False
