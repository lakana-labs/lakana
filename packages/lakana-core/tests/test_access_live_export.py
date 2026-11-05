"""Tests for reading the live access state and exporting it (#46)."""

from __future__ import annotations

from functools import partial

import yaml
from access_test_helpers import AccessWorld, access_world, seed_every_kind
from sqlalchemy.orm import Session

from lakana_core.access.export import export_document
from lakana_core.access.file import parse_access_file
from lakana_core.access.live import LiveAccess, read_live
from lakana_core.access.model import (
    AccessState,
    CiTrustEntry,
    ColumnMaskEntry,
    GrantEntry,
    GroupEntry,
    MembershipEntry,
    RowFilterEntry,
    Scopes,
    ServiceAccountEntry,
    Subject,
    Target,
)
from lakana_core.auth import AllowAllAuthorization
from lakana_core.identity.principals import PrincipalService
from lakana_core.policy.row_filters import RowFilterService


def _read(session: Session, world: AccessWorld) -> LiveAccess:
    """Read the live state the way a service does: reading columns as the administrator.

    Parameters
    ----------
    session
        Database session.
    world
        The world from `access_world`.

    Returns
    -------
    LiveAccess
        The live access state.
    """
    return read_live(session, partial(world.catalog.policy_fields, world.admin))


def test_live_state_names_every_kind_and_keeps_every_copy_id(session: Session) -> None:
    """Two identical grants are one entry with two ids (R5)."""
    world = access_world(session)
    seed_every_kind(session, world)
    live = _read(session, world)
    grant = GrantEntry(Subject("group", "finance"), "ANALYST", None, Target("prod"))
    assert grant in live.state.entries
    assert len(live.ids[grant]) == 2
    assert MembershipEntry("finance", "alice") in live.state.entries
    assert GroupEntry("nobody") in live.state.entries
    assert ServiceAccountEntry("etl-bot") in live.state.entries
    assert live.active == {"admin", "alice", "bob", "etl-bot"}


def test_a_deactivated_principal_is_outside_the_live_state(session: Session) -> None:
    """R4: its grants, memberships and its own entry disappear from the state."""
    world = access_world(session)
    seed_every_kind(session, world)
    PrincipalService(session, world.authz).deactivate_principal(world.admin, "bob")
    live = _read(session, world)
    assert not any("bob" in str(e) for e in live.state.entries)
    assert "bob" not in live.active


def test_export_reads_back_to_the_owned_state(session: Session) -> None:
    """Round trip: every entry kind, multi-level namespace, quotes, non-ASCII, disabled CI rule."""
    world = access_world(session)
    seed_every_kind(session, world)
    live = _read(session, world)
    for scopes in (Scopes(frozenset({"instance"})), Scopes(frozenset({"prod"}))):
        text = export_document(live.state, scopes)
        parsed = parse_access_file(text, file_name="access.yaml")
        assert parsed.state.entries == live.state.owned(scopes).entries, text
    assert "on: prod" in text
    assert "'on'" not in text


def test_a_policy_on_a_renamed_column_exports_the_current_name(session: Session) -> None:
    """#120: a bound mask follows its field; the file names the column as it is now."""
    world = access_world(session)
    seed_every_kind(session, world)
    world.catalog.rename("email", "mail")
    live = _read(session, world)
    text = export_document(live.state, Scopes(frozenset({"prod"})))
    assert "column: mail" in text
    stored = ColumnMaskEntry(
        Subject("group", "pii"), Target("prod", ("gold",), "customers"), "email", "'***'"
    )
    shown = ColumnMaskEntry(
        Subject("group", "pii"), Target("prod", ("gold",), "customers"), "mail", "'***'"
    )
    assert shown in live.state.entries
    assert live.stored[shown] == (stored,)
    assert len(live.ids[shown]) == 1


def test_names_that_are_yaml_1_1_booleans_export_as_plain_text(session: Session) -> None:
    """F1: `on:` stays unquoted and a name such as `no` or `yes` reads back as text."""
    state = AccessState(
        frozenset(
            {
                GroupEntry("no"),
                MembershipEntry("no", "yes"),
                ServiceAccountEntry("off"),
                GrantEntry(Subject("user", "yes"), "VIEWER", None, Target("prod")),
                GroupEntry("true"),
            }
        )
    )
    scopes = Scopes(frozenset({"instance"}))
    text = export_document(state, scopes)
    assert "  on: prod" in text
    assert "members: [yes]" in text or "- yes" in text
    assert parse_access_file(text, file_name="a.yaml").state == state
    assert yaml.safe_load(text)["groups"]  # still plain YAML for other tools


def test_an_empty_export_names_only_the_scopes(session: Session) -> None:
    """Nothing owned: the file is its header, version and manages."""
    text = export_document(AccessState(frozenset()), Scopes(frozenset({"prod"})))
    assert parse_access_file(text, file_name="a.yaml").state.entries == frozenset()
    assert text.startswith("# Lakana access file.")


def test_a_deactivated_account_takes_its_policies_and_ci_rules_with_it(session: Session) -> None:
    """R4 for the policy and CI rows: a row of an inactive principal is not read."""
    world = access_world(session)
    seed_every_kind(session, world)
    RowFilterService(session, AllowAllAuthorization(), world.catalog).create(
        world.admin,
        principal="bob",
        environment="prod",
        namespace="gold",
        table="orders",
        expression="id = '1'",
    )
    before = _read(session, world).state.entries
    assert any(isinstance(e, RowFilterEntry) and e.to.name == "bob" for e in before)
    service = PrincipalService(session, world.authz)
    service.deactivate_principal(world.admin, "bob")
    service.deactivate_principal(world.admin, "etl-bot")
    after = _read(session, world).state.entries
    assert not any(isinstance(e, RowFilterEntry) and e.to.name == "bob" for e in after)
    assert not any(isinstance(e, CiTrustEntry) for e in after)


def test_a_ci_rule_exports_every_setting_it_changes_from_the_default() -> None:
    """Non-default kinds, enabled and on-deploy jobs are written and read back."""
    rule = CiTrustEntry(
        "https://token.actions.githubusercontent.com",
        "etl",
        "etl-bot",
        "prod",
        (("ref", "main"),),
        ("deploy", "dry_run"),
        True,
        True,
    )
    plain = CiTrustEntry(rule.issuer, "etl", "etl-bot", "prod", (("ref", "dev"),))
    state = AccessState(frozenset({rule, plain}))
    text = export_document(state, Scopes(frozenset({"prod"})))
    assert text.count("kinds:") == 1
    assert "start_on_deploy_jobs: true" in text
    assert parse_access_file(text, file_name="a.yaml").state == state
