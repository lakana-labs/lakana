"""Changing the identity provider's issuer unlinks the linked people until they link again (#809).

`lakana_core.identity.provider_issuer`: a link counts only while its issuer is the one the server
recorded at start-up; with OIDC sign-in off nobody counts as linked. Every question "is this
person linked" gives the same answer: the passkey and recovery paths, the profile, the counts,
the break-glass rule and the token window.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from oidc_link_test_helpers import ISSUER, identity
from sqlalchemy import select, update
from sqlalchemy.orm import Session
from test_identity_links import World as LinkWorld
from test_identity_links import _Clock as LinkClock
from test_identity_links import _passkey as link_passkey
from test_identity_links import parts
from test_oidc_links import World

from lakana_core.auth import AllowAllAuthorization
from lakana_core.errors import AuthenticationError, ConflictError
from lakana_core.grants.break_glass import breaks, holds
from lakana_core.grants.service import active_instance_admin_ids, assert_break_glass_on_raise
from lakana_core.identity.oidc_flows import ProviderIdentity, principal_for
from lakana_core.identity.oidc_links import (
    is_linked,
    linked_count,
    linked_principal_for_sign_in,
    provider_link_of,
    unlinked_admin_count,
)
from lakana_core.identity.pats import PatService
from lakana_core.identity.provider_issuer import (
    OIDC_ISSUER_KEY,
    UNRECORDED,
    IssuerChange,
    issuer_change_warning,
    record_oidc_issuer,
    recorded_issuer,
)
from lakana_core.identity.sessions import token_window_ends_at, within_token_window
from lakana_core.models import (
    AuditEvent,
    AuditEventType,
    InstanceSetting,
    PersonalAccessToken,
    Principal,
)

NEW_ISSUER = "https://login.example.org/realms/lakana"
"""The issuer the provider moved to."""


def _new(subject: str) -> ProviderIdentity:
    """Build an identity the new provider verified.

    Parameters
    ----------
    subject
        The ``sub``.

    Returns
    -------
    ProviderIdentity
        With `NEW_ISSUER`.
    """
    return ProviderIdentity(
        issuer=NEW_ISSUER, subject=subject, email=None, name=None, auth_time=None
    )


def _settings_events(session: Session) -> list[dict]:
    """Read the details of every `SIGN_IN_SETTINGS_CHANGED` event, in order.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    list of dict
        The details.
    """
    return [
        dict(d)
        for d in session.execute(
            select(AuditEvent.detail)
            .where(AuditEvent.event_type == AuditEventType.SIGN_IN_SETTINGS_CHANGED)
            .order_by(AuditEvent.occurred_at)
        ).scalars()
    ]


@pytest.fixture
def world(session: Session) -> World:
    """Provide a bootstrapped instance whose server runs with `ISSUER`.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    World
        The services.
    """
    w = World(session)
    record_oidc_issuer(session, ISSUER)
    return w


# -- the one definition ---------------------------------------------------------------------------


def test_before_any_issuer_is_recorded_every_stored_link_counts(session: Session) -> None:
    w = World(session)
    alice = w.person("alice")
    w.link(alice)
    assert recorded_issuer(session) is UNRECORDED
    assert is_linked(session, alice)
    assert linked_count(session) == 1


def test_a_link_to_another_issuer_counts_as_no_link(world: World) -> None:
    alice = world.person("alice")
    world.link(alice)
    world.clock.now -= timedelta(days=90)
    alice.last_provider_sign_in_at = world.clock.now
    world.session.commit()
    assert is_linked(world.session, alice)
    record_oidc_issuer(world.session, NEW_ISSUER)
    world.session.refresh(alice)
    assert not is_linked(world.session, alice)
    assert linked_count(world.session) == 0
    # Shown, marked stale, so an administrator sees it and may unlink it.
    shown = provider_link_of(world.session, alice, days=30)
    assert shown is not None and shown.stale and shown.issuer == ISSUER
    # The token window stays (maintainer decision on #809): it ends 30 days after the last
    # provider sign-in, unless they link again.
    assert token_window_ends_at(world.session, alice) == world.clock.now + timedelta(days=30)
    assert world.links.refusal_for(alice) is None
    # The stored link is kept, not cleared: setting the issuer back counts it again.
    assert (alice.oidc_issuer, alice.oidc_subject) == (ISSUER, "alice")


def test_with_oidc_off_nobody_counts_as_linked(world: World) -> None:
    alice = world.person("alice")
    world.link(alice)
    record_oidc_issuer(world.session, None)
    assert recorded_issuer(world.session) is None
    assert not is_linked(world.session, alice)
    assert linked_count(world.session) == 0
    shown = provider_link_of(world.session, alice, days=30)
    assert shown is not None and shown.stale
    assert token_window_ends_at(world.session, alice) is not None


def test_setting_the_issuer_back_counts_the_links_again(world: World) -> None:
    alice = world.person("alice")
    world.link(alice)
    record_oidc_issuer(world.session, NEW_ISSUER)
    record_oidc_issuer(world.session, ISSUER)
    assert is_linked(world.session, alice)
    assert linked_count(world.session) == 1


# -- linking again ----------------------------------------------------------------------------


def test_a_person_with_a_stale_link_links_to_the_new_provider(world: World) -> None:
    alice = world.person("alice")
    world.link(alice, "old-sub")
    record_oidc_issuer(world.session, NEW_ISSUER)
    world.links.link(alice.id, _new("new-sub"), via="profile")
    world.session.commit()
    world.session.refresh(alice)
    assert (alice.oidc_issuer, alice.oidc_subject) == (NEW_ISSUER, "new-sub")
    assert is_linked(world.session, alice)
    linked = world.events(AuditEventType.PRINCIPAL_OIDC_LINKED)
    assert linked[-1]["iss"] == NEW_ISSUER
    assert linked[-1]["replaces"] == {"iss": ISSUER, "sub": "old-sub"}


def test_a_stale_link_never_matches_an_identity_of_the_new_issuer(world: World) -> None:
    """Another person at the new provider with the same ``sub`` is nobody here."""
    alice = world.person("alice")
    world.link(alice, "sub-1")
    record_oidc_issuer(world.session, NEW_ISSUER)
    assert principal_for(world.session, _new("sub-1")) is None
    assert linked_principal_for_sign_in(world.session, _new("sub-1")) is None
    # ... and may link to an account of their own: the stale link does not hold the identity.
    bob = world.person("bob")
    world.links.link(bob.id, _new("sub-1"), via="profile")
    world.session.commit()
    world.session.refresh(bob)
    assert is_linked(world.session, bob)


def test_a_stale_link_signs_nobody_in_even_with_its_own_identity(world: World) -> None:
    """A server that still verifies the old issuer's tokens finds nobody once it is not recorded."""
    alice = world.person("alice")
    world.link(alice, "sub-1")
    record_oidc_issuer(world.session, NEW_ISSUER)
    assert linked_principal_for_sign_in(world.session, identity("sub-1")) is None


# -- break-glass ------------------------------------------------------------------------------


def test_the_break_glass_count_reads_stale_links_as_unlinked(world: World) -> None:
    ann = world.person("ann", admin=True)
    bob = world.person("bob", admin=True)
    world.link(ann)
    admins = active_instance_admin_ids(world.session)
    assert unlinked_admin_count(world.session) == 1  # bob; the operator has no passkey
    record_oidc_issuer(world.session, NEW_ISSUER)
    assert unlinked_admin_count(world.session) == 2
    # Nobody is linked now, so no change can break the rule.
    assert holds(world.session, admins)
    assert not breaks(world.session, before=admins, after=admins - {bob.id})
    record_oidc_issuer(world.session, None)
    assert unlinked_admin_count(world.session) == 2


def test_granting_admin_to_a_stale_linked_person_counts_them_as_unlinked(world: World) -> None:
    """The raising side (`_linked_ids`): a stale link is no linked administrator."""
    bob = world.person("bob", passkey=False)
    world.link(bob)
    # Linked, without a passkey, the only administrator would need the provider: refused.
    with pytest.raises(ConflictError):
        assert_break_glass_on_raise(world.session, principal_id=bob.id)
    world.session.rollback()
    record_oidc_issuer(world.session, NEW_ISSUER)
    assert_break_glass_on_raise(world.session, principal_id=bob.id)


# -- the token window -------------------------------------------------------------------------


def _stale_token(world: World) -> tuple[Principal, str, str]:
    """Give alice a token, link her, let her window run out 400 days ago, and move the issuer.

    Parameters
    ----------
    world
        The test's world.

    Returns
    -------
    tuple of (Principal, str, str)
        Alice, and her token's key id and secret.
    """
    alice = world.person("alice")
    pats = PatService(world.session, AllowAllAuthorization())
    issued = pats.issue(world.ref(alice), "alice", name="laptop")
    world.link(alice)
    alice.last_provider_sign_in_at = world.clock.now - timedelta(days=400)
    world.session.commit()
    record_oidc_issuer(world.session, NEW_ISSUER)
    key_id, secret = issued.token.removeprefix("lakana_pat_").split("_", 1)
    return alice, key_id, secret


def test_a_stale_linked_persons_token_stops_at_the_window(world: World) -> None:
    """Maintainer decision on #809: a stale link keeps the window, or offboarding misses tokens."""
    _, key_id, secret = _stale_token(world)
    pat = world.session.execute(
        select(PersonalAccessToken).where(PersonalAccessToken.key_id == key_id)
    ).scalar_one()
    assert not within_token_window(world.session, pat, world.clock.now)
    with pytest.raises(AuthenticationError) as refused:
        PatService(world.session, AllowAllAuthorization()).verify(key_id, secret)
    # OIDC on: the way back is linking to the current provider.
    assert "no longer uses" in str(refused.value)
    assert "link your account to the current provider" in str(refused.value)
    assert "unlink" not in str(refused.value)
    # The token is not revoked: linking again opens the window.
    assert pat.revoked_at is None


def test_linking_again_gives_the_current_window(world: World) -> None:
    alice, key_id, secret = _stale_token(world)
    world.links.link(alice.id, _new("alice-new"), via="profile")
    world.session.commit()
    assert PatService(world.session, AllowAllAuthorization()).verify(key_id, secret).name == "alice"
    assert token_window_ends_at(world.session, alice) == world.clock.now + timedelta(days=30)


def test_with_oidc_off_a_linked_persons_token_stops_at_the_window(world: World) -> None:
    _, key_id, secret = _stale_token(world)
    record_oidc_issuer(world.session, None)
    with pytest.raises(AuthenticationError) as refused:
        PatService(world.session, AllowAllAuthorization()).verify(key_id, secret)
    # OIDC off: there is no provider to link to, only an administrator's unlink.
    assert "ask an administrator to unlink you" in str(refused.value)
    assert "recovery link" in str(refused.value) and "current provider" not in str(refused.value)


def test_an_administrator_unlinks_a_stale_link(world: World) -> None:
    """With OIDC off there is no provider to link again to: the unlink frees the tokens' window."""
    ann = world.person("ann", admin=True)
    alice, _, _ = _stale_token(world)
    record_oidc_issuer(world.session, None)
    world.links.unlink(world.signed_in(ann), "alice")
    world.session.refresh(alice)
    assert alice.oidc_subject is None
    assert token_window_ends_at(world.session, alice) is None
    [event] = world.events(AuditEventType.PRINCIPAL_OIDC_UNLINKED)
    assert (event["iss"], event["reason"]) == (ISSUER, "administrator")


def test_the_operator_unlinks_a_stale_linked_sole_administrator(world: World) -> None:
    """With OIDC off, the sole administrator cannot unlink themselves: the bootstrap token can."""
    ann = world.person("ann", admin=True)
    # Stored directly: linking the last administrator who signs in with a passkey is refused.
    ann.oidc_issuer, ann.oidc_subject = ISSUER, "ann"
    world.session.commit()
    record_oidc_issuer(world.session, None)
    with pytest.raises(ConflictError):
        world.links.unlink(world.signed_in(ann), "ann")
    operator = PatService(world.session, AllowAllAuthorization()).verify("opkey", "opsecret")
    world.links.unlink(operator, "ann")
    world.session.refresh(ann)
    assert ann.oidc_subject is None and token_window_ends_at(world.session, ann) is None


# -- recording the issuer -----------------------------------------------------------------------


def test_recording_a_changed_issuer_audits_it_and_counts_the_stale_links(world: World) -> None:
    alice = world.person("alice")
    world.person("bob")
    world.link(alice)
    assert _settings_events(world.session) == []
    change = record_oidc_issuer(world.session, NEW_ISSUER)
    assert change == IssuerChange(before=ISSUER, after=NEW_ISSUER, not_counted=1, names=("alice",))
    assert record_oidc_issuer(world.session, NEW_ISSUER) is None
    off = record_oidc_issuer(world.session, None)
    assert off == IssuerChange(before=NEW_ISSUER, after=None, not_counted=1, names=("alice",))
    events = _settings_events(world.session)
    assert events == [
        {
            "source": "start-up",
            "before": {"oidc_issuer": ISSUER},
            "after": {"oidc_issuer": NEW_ISSUER},
            "links_not_counted": 1,
        },
        {
            "source": "start-up",
            "before": {"oidc_issuer": NEW_ISSUER},
            "after": {"oidc_issuer": None},
            "links_not_counted": 1,
        },
    ]
    actors = world.session.execute(
        select(AuditEvent.principal_id).where(
            AuditEvent.event_type == AuditEventType.SIGN_IN_SETTINGS_CHANGED
        )
    ).scalars()
    assert set(actors) == {None}


def test_recording_reads_the_issuer_afresh_under_the_lock(world: World) -> None:
    """Review of #814: a copy in the session's cache never decides; another server's record does."""
    # Held, so the session's identity map keeps it (it holds rows only weakly).
    cached = world.session.get(InstanceSetting, OIDC_ISSUER_KEY)
    assert cached is not None and cached.value == {"issuer": ISSUER}
    # Another server recorded OIDC off meanwhile, past this session's cache.
    world.session.execute(
        update(InstanceSetting)
        .where(InstanceSetting.key == OIDC_ISSUER_KEY)
        .values(value={"issuer": None})
        .execution_options(synchronize_session=False)
    )
    change = record_oidc_issuer(world.session, NEW_ISSUER)
    assert change is not None and change.before is None
    assert cached.value == {"issuer": NEW_ISSUER}
    assert _settings_events(world.session)[-1]["before"] == {"oidc_issuer": None}


def test_the_first_record_audits_only_when_it_stops_a_link_from_counting(
    session: Session,
) -> None:
    w = World(session)
    assert record_oidc_issuer(session, None) == IssuerChange(
        before=UNRECORDED, after=None, not_counted=0
    )
    assert _settings_events(session) == []
    alice = w.person("alice")
    alice.oidc_issuer, alice.oidc_subject = ISSUER, "alice"
    session.commit()
    # A database a server of an earlier release used: the first record finds a stale link.
    row = session.get(InstanceSetting, OIDC_ISSUER_KEY)
    session.delete(row)
    session.commit()
    change = record_oidc_issuer(session, NEW_ISSUER)
    assert change is not None and change.not_counted == 1
    [event] = _settings_events(session)
    # Not `before: null`, which would read as "OIDC sign-in was off".
    assert "before" not in event and event["first_record"] is True
    assert event["links_not_counted"] == 1


def test_the_start_up_warning_names_the_change_and_the_way_back() -> None:
    change = IssuerChange(
        before=ISSUER, after=NEW_ISSUER, not_counted=3, names=("alice", "bob", "carol")
    )
    text = issuer_change_warning(change, linked=0, unlinked_admins=1, rule_holds=True)
    assert text is not None
    assert NEW_ISSUER in text and "3 people" in text and "(alice, bob, carol)" in text
    assert "link again" in text and "token window" in text
    assert "recovery" in text  # someone without a passkey needs a recovery link
    # Someone who left at the old provider gets their passkeys back: the operator acts.
    assert "Deactivate in Lakana anyone of them who left at the old provider" in text
    many = IssuerChange(
        before=ISSUER, after=NEW_ISSUER, not_counted=25, names=tuple(f"p{i:02}" for i in range(25))
    )
    long = issuer_change_warning(many, linked=0, unlinked_admins=1, rule_holds=True)
    assert long is not None and "p19 and 5 more" in long and "p20" not in long
    off = issuer_change_warning(
        IssuerChange(before=ISSUER, after=None, not_counted=1),
        linked=0,
        unlinked_admins=1,
        rule_holds=True,
    )
    assert off is not None and "LAKANA_OIDC__ISSUER" in off
    # With OIDC off there is no profile link: the warning says what an unlink does instead.
    assert "link again from their profile" not in off
    assert "lakana principal unlink-provider NAME" in off and "deletes their passkeys" in off
    assert "unlink-provider" not in text
    quiet = IssuerChange(before=None, after=ISSUER, not_counted=0)
    assert issuer_change_warning(quiet, linked=2, unlinked_admins=1, rule_holds=True) is None
    broken = issuer_change_warning(quiet, linked=2, unlinked_admins=0, rule_holds=False)
    assert broken is not None and "lakana recovery create" in broken


# -- recovery links ---------------------------------------------------------------------------


@pytest.fixture
def links_world(session: Session) -> LinkWorld:
    """Provide the link services, on a server that runs with `NEW_ISSUER`.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    LinkWorld
        The services and the operator.
    """
    w = LinkWorld(session, LinkClock())
    record_oidc_issuer(session, NEW_ISSUER)
    return w


def test_an_admins_recovery_link_of_a_stale_linked_person_adds_a_passkey(
    links_world: LinkWorld,
) -> None:
    """Not `ProviderSignInRequired`: the person is unlinked, so the link works as for anyone."""
    bob = links_world.person("bob")
    boss = links_world.person("boss", admin=True)
    link_passkey(links_world, bob)
    bob.oidc_issuer, bob.oidc_subject = ISSUER, "bob"
    links_world.session.commit()
    link = links_world.links.issue_recovery(links_world.pat(boss), principal_name="bob")
    assert links_world.links.describe(parts(link.url)[0]).provider_only is False
    redeemed = links_world.redeem(link)
    assert (redeemed.kind, redeemed.needs_passkey) == ("recovery", True)


def test_a_recovery_through_the_new_provider_links_a_stale_linked_person(
    links_world: LinkWorld,
) -> None:
    bob = links_world.person("bob")
    bob.oidc_issuer, bob.oidc_subject = ISSUER, "bob"
    links_world.session.commit()
    link = links_world.links.issue_recovery(links_world.operator, principal_name="bob")
    links_world.links.claim_for_provider(
        link.id, _new("bob-new"), replacing=None, peer="127.0.0.1", user_agent="pytest"
    )
    links_world.session.refresh(bob)
    assert (bob.oidc_issuer, bob.oidc_subject) == (NEW_ISSUER, "bob-new")
