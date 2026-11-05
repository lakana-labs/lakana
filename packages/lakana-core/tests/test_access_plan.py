"""Tests for planning an access file: checks, diff, drift, lockout and the service (#46)."""

from __future__ import annotations

import random
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

import pytest
from access_test_helpers import AccessWorld, access_world, seed_every_kind
from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.access import service as access_service
from lakana_core.access.live import LiveAccess
from lakana_core.access.model import (
    AccessState,
    CiTrustEntry,
    ColumnMaskEntry,
    Entry,
    GrantEntry,
    GroupEntry,
    MembershipEntry,
    RowFilterEntry,
    Scopes,
    ServiceAccountEntry,
    Subject,
    Target,
    fingerprint,
)
from lakana_core.access.plan import PlannedChange, apply_order, drift
from lakana_core.access.service import AccessService
from lakana_core.auth import (
    Action,
    AllowAllAuthorization,
    AuthorizationService,
    PrincipalRef,
    Securable,
    SecurableKind,
)
from lakana_core.ci_oidc.config import GITHUB_ISSUER
from lakana_core.ci_oidc.rules import CiTrustRuleService
from lakana_core.errors import PermissionDenied, ValidationError
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.grants.roles import Role
from lakana_core.grants.service import GrantService
from lakana_core.models import Environment, Grant, Principal, PrincipalType
from lakana_core.policy.column_masks import ColumnMaskService
from lakana_core.policy.row_filters import RowFilterService

_PROD = "version: 1\nmanages: [prod]\n"
_INSTANCE = (
    "version: 1\nmanages: [instance]\ngrants:\n  - {to: user:admin, role: ADMIN, on: instance}\n"
)


def _line(text: str, needle: str) -> int:
    """Find the line of the first occurrence of some text.

    Parameters
    ----------
    text
        A file.
    needle
        What to look for.

    Returns
    -------
    int
        The 1-based line, minus one: the entry starts on the line before its ``role:``.
    """
    return text[: text.index(needle)].count("\n")


def _service(
    session: Session, world: AccessWorld, authz: AuthorizationService | None = None
) -> AccessService:
    """Build the service the tests plan with.

    Parameters
    ----------
    session
        Database session.
    world
        The world from `access_world`.
    authz
        The authorization; the real one over the session's grants by default.

    Returns
    -------
    AccessService
        With GitHub as the one configured CI issuer.
    """
    return AccessService(
        session, authz or GrantAuthorization(session), world.catalog, issuers=[GITHUB_ISSUER]
    )


def test_an_unedited_export_plans_nothing(session: Session) -> None:
    """Spec §4: export → plan is empty (no change, no drift, no problem)."""
    world = access_world(session)
    seed_every_kind(session, world)
    service = _service(session, world)
    for scopes in (["instance"], ["prod"]):
        text = service.export(world.admin, scopes)
        plan = service.plan(world.admin, text)
        assert (plan.changes, plan.drift, plan.problems) == ((), (), ()), text
        assert plan.is_empty
        assert plan.manages == tuple(scopes)
        assert len(plan.file_sha256) == 64


def test_the_plan_lists_creates_changes_and_removals(session: Session) -> None:
    """One grant added, one removed, one CI rule's kinds changed (R6)."""
    world = access_world(session)
    seed_every_kind(session, world)
    service = _service(session, world)
    text = service.export(world.admin, ["prod"])
    text = text.replace("role: ANALYST", "role: VIEWER").replace(
        "  kinds:\n  - deploy\n  - dry_run\n", ""
    )
    plan = service.plan(world.admin, text)
    assert [c.text for c in plan.creates] == ["grant role VIEWER on prod to group:finance"]
    assert [c.text for c in plan.removals] == ["grant role ANALYST on prod to group:finance"]
    assert len(plan.removals[0].ids) == 2
    assert [c.op for c in plan.changed] == ["change"]
    change = plan.changed[0]
    assert change.path == "ci_trust[0]"
    assert isinstance(change.before, CiTrustEntry) and change.before.kinds == ("deploy", "dry_run")
    assert len(change.ids) == 1
    assert change.text.endswith(
        "(event_name=push, ref=refs/heads/main, repository_id=123456; deploy; disabled)"
    )
    assert " becomes " in change.text
    assert not plan.is_empty
    assert plan.problems == ()


def test_removals_in_a_scope_not_yet_managed_are_drift(session: Session) -> None:
    """Spec §4: changes made before the scope was managed show as drift."""
    world = access_world(session)
    seed_every_kind(session, world)
    plan = _service(session, world).plan(world.admin, "version: 1\nmanages: [staging]\n")
    assert [d.text for d in plan.drift] == [
        "grant role VIEWER on staging to user:bob exists outside the file; apply removes it"
    ]
    assert [(d.scope, d.by, d.reason, d.at) for d in plan.drift] == [("staging", None, None, None)]
    assert plan.fingerprint == fingerprint(
        AccessState(
            frozenset({GrantEntry(Subject("user", "bob"), "VIEWER", None, Target("staging"))})
        )
    )


def test_drift_in_an_instance_file_names_the_instance(session: Session) -> None:
    """A removal under ``manages: [instance]`` is drift of the instance scope."""
    world = access_world(session)
    plan = _service(session, world).plan(world.admin, _INSTANCE + "service_accounts: [etl-bot]\n")
    assert plan.problems == ()
    assert plan.drift == ()
    seeded = _service(session, world).plan(world.admin, _INSTANCE)
    assert [(d.scope, d.text) for d in seeded.drift] == [
        ("instance", "service account etl-bot exists outside the file; apply removes it")
    ]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            _PROD + "grants:\n  - {to: user:alise, role: VIEWER, on: prod}",
            "access.yaml: grants[0] (line 4): no active user alise; check the name with lakana "
            "principal list, or let the person sign in first",
        ),
        (
            _PROD + "grants:\n  - {to: group:nope, role: VIEWER, on: prod}",
            "access.yaml: grants[0] (line 4): no group nope; create it with lakana group create, "
            "or manage instance and list it under groups",
        ),
        (
            _PROD + "grants:\n  - {to: user:alice, action: READ_TABLE, table: prod.gold.nope}",
            "access.yaml: grants[0] (line 4): No table",
        ),
        (
            _PROD + "policies:\n  - {kind: row_filter, table: prod.gold.orders, "
            'expression: "nope = 1", to: user:alice}',
            "access.yaml: policies[0] (line 4): Table 'gold.orders' in environment 'prod' has no "
            "column 'nope'",
        ),
        (
            _PROD + "policies:\n  - {kind: column_mask, table: prod.gold.orders, column: nope, "
            "expression: \"'*'\", to: user:alice}",
            "access.yaml: policies[0] (line 4): Table 'gold.orders' in environment 'prod' has no "
            "column 'nope'",
        ),
        (
            _PROD + "ci_trust:\n  - {issuer: gitlab, repository: etl, as: service:etl-bot, "
            "on: prod, conditions: {ref: main}}",
            "access.yaml: ci_trust[0] (line 4): The issuer must be one of",
        ),
        (
            _PROD + "ci_trust:\n  - {issuer: github, repository: nope, as: service:etl-bot, "
            "on: prod, conditions: {ref: refs/heads/main, repository_id: '1', event_name: push}}",
            "access.yaml: ci_trust[0] (line 4): No repository",
        ),
        (
            _PROD + "grants:\n  - {to: user:etl-bot, role: VIEWER, on: prod}",
            "access.yaml: grants[0] (line 4): etl-bot is a service account; write service:etl-bot",
        ),
        (
            _PROD + "grants:\n  - {to: service:alice, role: VIEWER, on: prod}",
            "access.yaml: grants[0] (line 4): alice is a person; write user:alice",
        ),
        (
            _PROD + "grants:\n  - {to: service:ghost, role: VIEWER, on: prod}",
            "access.yaml: grants[0] (line 4): no active service account ghost; create it with "
            "lakana principal create ghost --type service-account, or manage instance and list "
            "it under service_accounts",
        ),
        (
            _INSTANCE + "  - {to: group:finance, role: VIEWER, on: prod}",
            "access.yaml: grants[1] (line 5): no group finance in the file; list it under groups",
        ),
        (
            _INSTANCE + "  - {to: service:etl-bot, role: VIEWER, on: prod}",
            "access.yaml: grants[1] (line 5): no service account etl-bot in the file; list it "
            "under service_accounts",
        ),
        (
            _INSTANCE + "groups:\n  g: {members: [ghost]}",
            "access.yaml: groups.g.members[0] (line 6): no active user ghost; check the name with "
            "lakana principal list, or let the person sign in first",
        ),
        (
            _INSTANCE + "groups:\n  g: {members: [etl-bot]}",
            "access.yaml: groups.g.members[0] (line 6): no service account etl-bot in the file; "
            "list it under service_accounts",
        ),
        (
            _INSTANCE + "service_accounts: [etl-bot, alice]",
            "access.yaml: service_accounts[1] (line 5): alice is a person; choose another name "
            "for the service account",
        ),
        (
            "version: 1\nmanages: [prod, nowhere]\n",
            "access.yaml: manages (line 2): no environment nowhere; create it with lakana env "
            "create, or remove it from manages",
        ),
        (
            _PROD + "ci_trust:\n"
            "  - {issuer: github, repository: etl, as: service:etl-bot, on: prod, conditions: "
            "{ref: refs/heads/main, repository_id: '1', event_name: push}}\n"
            f"  - {{issuer: '{GITHUB_ISSUER}', repository: etl, as: service:etl-bot, on: prod, "
            "conditions: {ref: refs/heads/main, repository_id: '1', event_name: push}}",
            "access.yaml: ci_trust[1] (line 5): repeats ci_trust[0]; remove one",
        ),
        (
            "not: [yaml",
            "access.yaml (line 2): is not valid YAML",
        ),
    ],
)
def test_each_reference_problem_names_its_entry(session: Session, text: str, expected: str) -> None:
    """Unknown user, group, service account, table; an invalid expression; an unknown issuer."""
    world = access_world(session)
    plan = _service(session, world).plan(world.admin, text + "\n")
    assert any(p.startswith(expected) for p in plan.problems), plan.problems


def test_a_file_that_does_not_parse_plans_only_its_problems(session: Session) -> None:
    """Every parse problem, the hash of the text, the fingerprint of nothing."""
    world = access_world(session)
    plan = _service(session, world).plan(world.admin, "version: 2\n", file_name="team.yaml")
    assert plan.problems[0].startswith("team.yaml: version")
    assert len(plan.problems) == 2
    assert (plan.changes, plan.drift, plan.manages) == ((), (), ())
    assert plan.fingerprint == fingerprint(AccessState(frozenset()))
    with pytest.raises(ValidationError, match="file_name"):
        _service(session, world).plan(world.admin, "", file_name="../a.yaml")


def test_a_reused_service_account_name_is_refused(session: Session) -> None:
    """R4: a deactivated account's name cannot be listed again."""
    world = access_world(session)
    session.add(
        Principal(id=uuid.uuid4(), type=PrincipalType.SERVICE_ACCOUNT, name="old", is_active=False)
    )
    session.commit()
    plan = _service(session, world).plan(world.admin, _INSTANCE + "service_accounts: [old]\n")
    assert plan.problems == (
        "access.yaml: service_accounts[0] (line 5): service account old exists and is "
        "deactivated; a name cannot be used again, choose another",
    )


def test_a_new_service_account_and_group_plan_as_creates(session: Session) -> None:
    """Instance files create what they list; members may be file service accounts."""
    world = access_world(session)
    plan = _service(session, world).plan(
        world.admin,
        _INSTANCE + "  - {to: service:new-bot, role: VIEWER, on: prod}\n"
        "service_accounts: [etl-bot, new-bot]\ngroups:\n  g: {members: [alice, new-bot]}\n",
    )
    assert plan.problems == ()
    assert [c.text for c in plan.creates] == [
        "group g",
        "member alice of group g",
        "member new-bot of group g",
        "service account new-bot",
        "grant role VIEWER on prod to service:new-bot",
    ]


def test_service_checks_run_only_on_creates_and_changes(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F8: an unchanged entry is not validated again, so the live state always re-plans."""
    world = access_world(session)
    seed_every_kind(session, world)
    service = _service(session, world)
    text = service.export(world.admin, ["prod"])

    def refuse(*args: object, **kwargs: object) -> None:
        raise ValidationError("refused")

    for owner, name in (
        (GrantService, "check_target"),
        (RowFilterService, "check_target"),
        (ColumnMaskService, "check_target"),
        (CiTrustRuleService, "check_rule"),
    ):
        monkeypatch.setattr(owner, name, refuse)
    assert service.plan(world.admin, text).problems == ()
    edited = service.plan(world.admin, text.replace("role: ANALYST", "role: VIEWER"))
    assert [p.split(": ", 2)[1:] for p in edited.problems] == [
        [f"{edited.creates[0].path} (line {_line(text, 'role: ANALYST')})", "refused"]
    ]


def test_a_renamed_column_keeps_an_older_export_planning_nothing(session: Session) -> None:
    """F8: a file in the stored names still matches a policy shown under new names (#120)."""
    world = access_world(session)
    seed_every_kind(session, world)
    service = _service(session, world)
    before = service.export(world.admin, ["prod"])
    world.catalog.rename("email", "mail")
    world.catalog.rename("region", "area")
    after = service.export(world.admin, ["prod"])
    assert "column: mail" in after and "area" in after
    for text in (before, after):
        plan = service.plan(world.admin, text)
        assert (plan.changes, plan.drift, plan.problems) == ((), (), ()), text


def test_live_copies_that_normalize_equal_keep_every_row_id(session: Session) -> None:
    """F6: two stored spellings of one filter are one entry whose removal names both rows."""
    world = access_world(session)
    filters = RowFilterService(session, AllowAllAuthorization(), world.catalog)
    for expression in ("region='EU'", "region = 'EU'"):
        filters.create(
            world.admin,
            principal="alice",
            environment="prod",
            namespace="gold",
            table="orders",
            expression=expression,
        )
    service = _service(session, world)
    assert "expression: region = 'EU'" in service.export(world.admin, ["prod"])
    plan = service.plan(world.admin, _PROD)
    assert [(c.text, len(c.ids)) for c in plan.removals] == [
        ("row filter on prod.gold.orders for user:alice: region = 'EU'", 2)
    ]
    kept = service.plan(
        world.admin,
        _PROD + "policies:\n  - {kind: row_filter, table: prod.gold.orders, "
        "expression: \"region  =  'EU'\", to: user:alice}\n",
    )
    assert kept.changes == ()


def test_a_preset_issuer_matches_the_stored_url(session: Session) -> None:
    """A file saying ``github`` matches the rule stored under the issuer URL."""
    world = access_world(session)
    seed_every_kind(session, world)
    service = _service(session, world)
    text = service.export(world.admin, ["prod"])
    assert f"issuer: {GITHUB_ISSUER}" in text
    plan = service.plan(world.admin, text.replace(f"issuer: {GITHUB_ISSUER}", "issuer: github"))
    assert (plan.changes, plan.problems) == ((), ())


def test_plan_refuses_a_caller_without_instance_admin(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review focus 1: a non-admin crafting a file is refused before it is read."""
    world = access_world(session)
    alice = world.ref("alice")
    GrantService(session, AllowAllAuthorization()).grant(
        world.admin, principal="alice", action=Action.MANAGE_POLICY, kind=SecurableKind.INSTANCE
    )

    def unreachable(*args: object, **kwargs: object) -> None:
        raise AssertionError("read before authorization")

    monkeypatch.setattr(access_service, "read_live", unreachable)
    monkeypatch.setattr(access_service, "parse_access_file", unreachable)
    service = _service(session, world)
    with pytest.raises(PermissionDenied):
        service.plan(alice, "not: [yaml")
    with pytest.raises(PermissionDenied):
        service.export(alice, ["instance"])


def test_plan_refuses_manage_grant_held_on_one_environment(session: Session) -> None:
    """Review focus 1: MANAGE_GRANT on `prod` does not let one manage `prod` (nor `staging`)."""
    world = access_world(session)
    alice = world.ref("alice")
    prod = session.execute(select(Environment).where(Environment.name == "prod")).scalar_one()
    session.add(
        Grant(
            id=uuid.uuid4(),
            principal_id=alice.id,
            action=Action.MANAGE_GRANT,
            kind=SecurableKind.ENVIRONMENT,
            environment="prod",
            environment_id=prod.id,
            granted_by=world.admin.id,
            granted_at=datetime.now(UTC),
        )
    )
    session.commit()
    with pytest.raises(PermissionDenied):
        _service(session, world).plan(alice, "version: 1\nmanages: [prod]\n")


def test_the_admin_role_alone_is_not_enough_without_policy_and_principal_rights(
    session: Session,
) -> None:
    """`_authorize` asks for MANAGE_POLICY and MANAGE_PRINCIPAL too, before reading."""

    class OnlyGrants(AllowAllAuthorization):
        """Allows everything but MANAGE_PRINCIPAL."""

        def authorize(self, principal: PrincipalRef, action: Action, securable: Securable) -> None:
            """Refuse MANAGE_PRINCIPAL.

            Parameters
            ----------
            principal
                The acting principal.
            action
                The action.
            securable
                Where.

            Raises
            ------
            PermissionDenied
                For MANAGE_PRINCIPAL.
            """
            if action is Action.MANAGE_PRINCIPAL:
                raise PermissionDenied("no")

    world = access_world(session)
    with pytest.raises(PermissionDenied):
        _service(session, world, OnlyGrants()).plan(world.admin, "version: 1\n")


def test_export_names_its_scopes_or_refuses_them(session: Session) -> None:
    """An unknown environment, ``instance`` with others, or no scope is refused."""
    world = access_world(session)
    service = _service(session, world)
    assert "manages:\n- prod\n- staging\n" in service.export(world.admin, ["staging", "prod"])
    for scopes, message in (
        (["nowhere"], "no environment nowhere; lakana env list shows them"),
        (["instance", "prod"], "instance already covers every environment"),
        ([], "name at least one scope"),
    ):
        with pytest.raises(ValidationError, match=message):
            service.export(world.admin, scopes)


def test_plan_lists_a_file_that_removes_the_callers_own_admin(session: Session) -> None:
    """R7: refused without allow_lockout, allowed with it while another admin remains."""
    world = access_world(session)
    seed_every_kind(session, world)
    service = _service(session, world)
    text = service.export(world.admin, ["instance"]).replace(
        "- to: user:admin\n  role: ADMIN\n  on: instance\n",
        "- to: user:alice\n  role: ADMIN\n  on: instance\n",
    )
    assert service.plan(world.admin, text).problems == (
        "access.yaml: the file removes your own ADMIN role on the instance (MANAGE_GRANT); keep "
        "it, or repeat with --allow-lockout if another administrator applies from now on",
    )
    assert service.plan(world.admin, text, allow_lockout=True).problems == ()


def test_plan_lists_a_file_that_leaves_no_admin_even_with_allow_lockout(session: Session) -> None:
    """R7: no active ADMIN at all is refused always, also when ADMIN goes to an empty group."""
    world = access_world(session)
    seed_every_kind(session, world)
    service = _service(session, world)
    text = service.export(world.admin, ["instance"]).replace(
        "- to: user:admin\n  role: ADMIN\n  on: instance\n",
        "- to: group:nobody\n  role: ADMIN\n  on: instance\n",
    )
    problems = service.plan(world.admin, text, allow_lockout=True).problems
    assert problems == (
        "access.yaml: the file leaves no active ADMIN on the instance; keep "
        "{to: user:admin, role: ADMIN, on: instance} or give ADMIN to another active person",
    )


def test_admin_through_a_group_or_a_removed_account_counts_as_it_should(session: Session) -> None:
    """A group member keeps ADMIN; a service account the file removes does not."""
    world = access_world(session)
    service = _service(session, world)
    by_group = service.plan(
        world.admin,
        "version: 1\nmanages: [instance]\ngroups:\n  admins: {members: [admin]}\n"
        "grants:\n  - {to: group:admins, role: ADMIN, on: instance}\n",
    )
    assert by_group.problems == ()
    to_removed = service.plan(
        world.admin,
        "version: 1\nmanages: [instance]\n"
        "grants:\n  - {to: service:etl-bot, role: ADMIN, on: instance}\n",
        allow_lockout=True,
    )
    assert any("leaves no active ADMIN" in p for p in to_removed.problems), to_removed.problems


def test_a_removal_in_a_managed_scope_is_not_drift() -> None:
    """F7: once a scope is managed, a removal there is a plain removal."""
    grant = GrantEntry(Subject("user", "bob"), "VIEWER", None, Target("prod"))
    other = GrantEntry(Subject("user", "bob"), "VIEWER", None, Target("staging"))
    changes = [
        PlannedChange("create", grant, "grants[0]"),
        PlannedChange("remove", grant, None),
        PlannedChange("remove", other, None),
    ]
    found = drift(changes, Scopes(frozenset({"prod", "staging"})), frozenset({"prod"}))
    assert [d.scope for d in found] == ["staging"]


def test_apply_order_creates_first_and_removes_in_reverse() -> None:
    """Creates and changes in dependency order, then removals in reverse order."""
    account = ServiceAccountEntry("bot")
    grant = GrantEntry(Subject("service", "bot"), "VIEWER", None, Target("prod"))
    group = GroupEntry("g")
    member = MembershipEntry("g", "bot")
    mask = ColumnMaskEntry(Subject("group", "g"), Target("prod", ("gold",), "t"), "c", "'*'")
    old_filter = RowFilterEntry(Subject("group", "g"), Target("prod", ("gold",), "t"), "TRUE")
    changes = [
        PlannedChange("remove", account, None),
        PlannedChange("remove", grant, None),
        PlannedChange("create", member, "groups.g.members[0]"),
        PlannedChange("create", group, "groups.g"),
        PlannedChange("create", mask, "policies[0]"),
        PlannedChange("remove", old_filter, None),
    ]
    random.Random(4).shuffle(changes)
    ordered = [(c.op, c.entry) for c in apply_order(changes)]
    assert ordered == [
        ("create", group),
        ("create", member),
        ("create", mask),
        ("remove", old_filter),
        ("remove", grant),
        ("remove", account),
    ]


def _filter(expression: str) -> RowFilterEntry:
    """Build a row filter entry of one subject and table.

    Parameters
    ----------
    expression
        The predicate.

    Returns
    -------
    RowFilterEntry
        For ``user:a`` on ``prod.g.t``.
    """
    return RowFilterEntry(Subject("user", "a"), Target("prod", ("g",), "t"), expression)


def _spaced(entry: Entry) -> Entry:
    """Normalize a filter's spacing around ``=``, as a stand-in for `normalized_expression`.

    Parameters
    ----------
    entry
        A row filter entry.

    Returns
    -------
    Entry
        The filter with exactly one space either side of each ``=``.
    """
    assert isinstance(entry, RowFilterEntry)
    return _filter(entry.expression.replace(" ", "").replace("=", " = "))


def test_live_access_is_rebuilt_with_mapped_entries() -> None:
    """`LiveAccess.mapped` re-keys ids and stored forms, merging the ids of entries made equal."""
    a, b, shown, plain = _filter("x=1"), _filter("x = 1"), _filter('"y"=1'), _filter("z=1")
    first, second, third, fourth = (uuid.uuid4() for _ in range(4))
    live = LiveAccess(
        AccessState(frozenset({a, b, shown, plain})),
        {a: (first,), b: (second,), shown: (third,), plain: (fourth,)},
        frozenset({"a"}),
        {shown: (_filter("y = 1"), _filter("y=1")), plain: (_filter("z = 1"),)},
    )
    mapped = live.mapped(_spaced)
    assert mapped.ids[b] == (first, second)
    assert dict(mapped.stored) == {_spaced(shown): (_filter("y = 1"),)}
    assert mapped.active == live.active
    assert len(mapped.state.entries) == 3


@pytest.mark.parametrize(
    ("owner", "name", "call"),
    [
        (
            GrantService,
            "check_target",
            lambda session, world: GrantService(session, AllowAllAuthorization()).grant(
                world.admin, principal="alice", role=Role.VIEWER, kind=SecurableKind.INSTANCE
            ),
        ),
        (
            RowFilterService,
            "check_target",
            lambda session, world: RowFilterService(
                session, AllowAllAuthorization(), world.catalog
            ).create(
                world.admin,
                principal="alice",
                environment="prod",
                namespace="gold",
                table="orders",
                expression="TRUE",
            ),
        ),
        (
            ColumnMaskService,
            "check_target",
            lambda session, world: ColumnMaskService(
                session, AllowAllAuthorization(), world.catalog
            ).create(
                world.admin,
                principal="alice",
                environment="prod",
                namespace="gold",
                table="orders",
                column="email",
                expression="'*'",
            ),
        ),
        (
            CiTrustRuleService,
            "check_rule",
            lambda session, world: CiTrustRuleService(
                session, AllowAllAuthorization(), issuers=[GITHUB_ISSUER]
            ).create(
                world.admin,
                issuer="github",
                principal="etl-bot",
                environment="prod",
                repository="etl",
                kinds=["deploy"],
                conditions={"ref": "refs/heads/main"},
            ),
        ),
    ],
)
def test_check_target_is_what_create_runs(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
    owner: type,
    name: str,
    call: Callable[[Session, AccessWorld], object],
) -> None:
    """The plan validates with the very method `grant`/`create` runs before it writes."""
    world = access_world(session)

    def refuse(*args: object, **kwargs: object) -> None:
        raise ValidationError("x")

    monkeypatch.setattr(owner, name, refuse)
    with pytest.raises(ValidationError, match=r"^x$"):
        call(session, world)
