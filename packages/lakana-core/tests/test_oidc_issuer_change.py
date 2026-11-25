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
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_identity_links import World as LinkWorld
from test_identity_links import _Clock as LinkClock
from test_identity_links import _passkey as link_passkey
from test_identity_links import parts
from test_oidc_links import World

from lakana_core.auth import AllowAllAuthorization
from lakana_core.grants.break_glass import breaks, holds
from lakana_core.grants.service import active_instance_admin_ids
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
    assert provider_link_of(world.session, alice, days=30) is None
    assert token_window_ends_at(world.session, alice) is None
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
    assert provider_link_of(world.session, alice, days=30) is None
    assert token_window_ends_at(world.session, alice) is None


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


# -- the token window -------------------------------------------------------------------------


def test_a_stale_linked_persons_tokens_have_no_window(world: World) -> None:
    alice = world.person("alice")
    pats = PatService(world.session, AllowAllAuthorization())
    issued = pats.issue(world.ref(alice), "alice", name="laptop")
    world.link(alice)
    alice.last_provider_sign_in_at = world.clock.now - timedelta(days=400)
    world.session.commit()
    key_id, secret = issued.token.removeprefix("lakana_pat_").split("_", 1)
    pat = world.session.execute(
        select(PersonalAccessToken).where(PersonalAccessToken.key_id == key_id)
    ).scalar_one()
    assert not within_token_window(world.session, pat, world.clock.now)
    record_oidc_issuer(world.session, NEW_ISSUER)
    assert within_token_window(world.session, pat, world.clock.now)
    assert pats.verify(key_id, secret).name == "alice"


# -- recording the issuer -----------------------------------------------------------------------


def test_recording_a_changed_issuer_audits_it_and_counts_the_stale_links(world: World) -> None:
    alice = world.person("alice")
    world.person("bob")
    world.link(alice)
    assert _settings_events(world.session) == []
    change = record_oidc_issuer(world.session, NEW_ISSUER)
    assert change == IssuerChange(before=ISSUER, after=NEW_ISSUER, not_counted=1)
    assert record_oidc_issuer(world.session, NEW_ISSUER) is None
    off = record_oidc_issuer(world.session, None)
    assert off == IssuerChange(before=NEW_ISSUER, after=None, not_counted=1)
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
    assert event["before"] == {"oidc_issuer": None}
    assert event["links_not_counted"] == 1


def test_the_start_up_warning_names_the_change_and_the_way_back() -> None:
    change = IssuerChange(before=ISSUER, after=NEW_ISSUER, not_counted=3)
    text = issuer_change_warning(change, linked=0, unlinked_admins=1, rule_holds=True)
    assert text is not None
    assert NEW_ISSUER in text and "3 people" in text and "link again" in text
    assert "recovery" in text  # someone without a passkey needs a recovery link
    off = issuer_change_warning(
        IssuerChange(before=ISSUER, after=None, not_counted=1),
        linked=0,
        unlinked_admins=1,
        rule_holds=True,
    )
    assert off is not None and "LAKANA_OIDC__ISSUER" in off
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
