"""Tests for applying an access file in one audited transaction, and for status (#46)."""

from __future__ import annotations

import dataclasses
import re
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
import yaml
from access_test_helpers import AccessWorld, access_world, seed_every_kind
from sqlalchemy import ColumnElement, func, select
from sqlalchemy.orm import Session

from lakana_core.access import service as access_service
from lakana_core.access.apply import admin_lost_part_way
from lakana_core.access.file import AccessFileError
from lakana_core.access.model import (
    GrantEntry,
    GroupEntry,
    MembershipEntry,
    ServiceAccountEntry,
    Subject,
    Target,
)
from lakana_core.access.plan import PlannedChange
from lakana_core.access.service import AccessService
from lakana_core.audit.authorization import AuditingAuthorization
from lakana_core.audit.denial_limiter import DenialAuditLimiter
from lakana_core.audit.service import ACCESS_APPLY_KEY, AuditService
from lakana_core.auth import Action, AllowAllAuthorization, PrincipalRef, Securable, SecurableKind
from lakana_core.ci_oidc.config import GITHUB_ISSUER
from lakana_core.errors import ConflictError, PermissionDenied, ValidationError
from lakana_core.grants.admin import holds_instance_admin
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.grants.roles import Role
from lakana_core.grants.service import GrantService
from lakana_core.identifiers import TableRef
from lakana_core.identity.groups import GroupService
from lakana_core.identity.pats import PatService
from lakana_core.identity.principals import PrincipalService
from lakana_core.models import (
    AccessManagedScope,
    AccessOverride,
    AuditEvent,
    AuditEventType,
    CiTrustRule,
    ColumnMask,
    Grant,
    Group,
    GroupMembership,
    PersonalAccessToken,
    Principal,
    PrincipalType,
    RowFilter,
)
from lakana_core.policy.expressions import normalized_expression, validate_restricted_expression
from lakana_core.policy.field_binding import FIELD_IDS, PolicySchema, resolve_expression
from lakana_core.policy.resolver import PolicyResolver

_AUTO = "admin"


class _Yaml12Loader(yaml.SafeLoader):
    """A safe loader that reads only ``true`` and ``false`` as booleans, as the parser does.

    PyYAML follows YAML 1.1, where the key ``on`` of every grant would load as `True`.
    """


_Yaml12Loader.yaml_implicit_resolvers = {
    first: [(tag, regexp) for tag, regexp in resolvers if tag != "tag:yaml.org,2002:bool"]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


class _NoAliasDumper(yaml.SafeDumper):
    """A safe dumper that writes a value used twice out twice: the parser refuses aliases."""

    def ignore_aliases(self, data: object) -> bool:
        """Never write an alias.

        Parameters
        ----------
        data
            Any value.

        Returns
        -------
        bool
            Always true.
        """
        return True


_Yaml12Loader.add_implicit_resolver(
    "tag:yaml.org,2002:bool", re.compile(r"^(?:true|false)$"), list("tf")
)


def _service(session: Session, world: AccessWorld) -> AccessService:
    """Build the service the tests apply with.

    Parameters
    ----------
    session
        Database session.
    world
        The world from `access_world`.

    Returns
    -------
    AccessService
        With the authorization the server uses (`AuditingAuthorization` over
        `GrantAuthorization`) and GitHub as the one configured CI issuer.
    """
    authz = AuditingAuthorization(GrantAuthorization(session), session, DenialAuditLimiter(100))
    return AccessService(session, authz, world.catalog, issuers=[GITHUB_ISSUER])


def _edit(text: str, change: Callable[[dict[str, Any]], None]) -> str:
    """Edit an access file as data.

    Parameters
    ----------
    text
        The file.
    change
        Changes the loaded document in place.

    Returns
    -------
    str
        The changed file.
    """
    document = yaml.load(text, Loader=_Yaml12Loader)
    change(document)
    return yaml.dump(document, Dumper=_NoAliasDumper, sort_keys=False, allow_unicode=True)


def _with_three_more_grants(text: str) -> str:
    """Add three grants the live state does not have.

    Parameters
    ----------
    text
        An export of the instance.

    Returns
    -------
    str
        The file with three more grants.
    """

    def add(document: dict[str, Any]) -> None:
        """Append the grants.

        Parameters
        ----------
        document
            The loaded file.
        """
        document["grants"] += [
            {"to": "user:bob", "role": "VIEWER", "on": "prod"},
            {"to": "user:alice", "role": "VIEWER", "on": "staging"},
            {"to": "service:etl-bot", "role": "VIEWER", "on": "staging"},
        ]

    return _edit(text, add)


def _count(session: Session, model: type, *where: ColumnElement[bool]) -> int:
    """Count the rows of a model that match some conditions.

    Parameters
    ----------
    session
        Database session.
    model
        The model.
    *where
        The conditions.

    Returns
    -------
    int
        The number of rows.
    """
    return session.scalar(select(func.count()).select_from(model).where(*where)) or 0


def _snapshot(session: Session) -> tuple[int, ...]:
    """Count every access table and the audit events.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    tuple of int
        Active grants, groups, memberships, active principals, active policies, CI rules and
        enabled ones, active tokens, managed scopes, open overrides and audit events.
    """
    return (
        _count(session, Grant, Grant.revoked_at.is_(None)),
        _count(session, Group),
        _count(session, GroupMembership),
        _count(session, Principal, Principal.is_active.is_(True)),
        _count(session, RowFilter, RowFilter.revoked_at.is_(None)),
        _count(session, ColumnMask, ColumnMask.revoked_at.is_(None)),
        _count(session, CiTrustRule),
        _count(session, CiTrustRule, CiTrustRule.enabled.is_(True)),
        _count(session, PersonalAccessToken, PersonalAccessToken.revoked_at.is_(None)),
        _count(session, AccessManagedScope),
        _count(session, AccessOverride, AccessOverride.resolved_at.is_(None)),
        _count(session, AuditEvent),
    )


def _events(session: Session, event_type: AuditEventType) -> list[AuditEvent]:
    """List the audit events of one type.

    Parameters
    ----------
    session
        Database session.
    event_type
        The type.

    Returns
    -------
    list of AuditEvent
        The events.
    """
    return list(session.scalars(select(AuditEvent).where(AuditEvent.event_type == event_type)))


def _recorded(monkeypatch: pytest.MonkeyPatch) -> list[AuditEventType]:
    """Record the type of every audit event in the order it is written.

    Parameters
    ----------
    monkeypatch
        The fixture.

    Returns
    -------
    list of AuditEventType
        Filled as events are recorded.
    """
    seen: list[AuditEventType] = []
    original = AuditService.record

    def spy(
        self: AuditService, principal_id: object, event_type: AuditEventType, **kw: object
    ) -> AuditEvent:
        """Note the type, then record.

        Parameters
        ----------
        self
            The audit service.
        principal_id
            The principal.
        event_type
            The type.
        **kw
            The rest.

        Returns
        -------
        AuditEvent
            The recorded event.
        """
        seen.append(event_type)
        return original(self, principal_id, event_type, **kw)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(AuditService, "record", spy)
    return seen


def _override(session: Session, world: AccessWorld, scope: str, change: str) -> AccessOverride:
    """Store an open override, as a guarded change makes it.

    Parameters
    ----------
    session
        Database session; committed.
    world
        The world.
    scope
        The managed scope.
    change
        What was changed.

    Returns
    -------
    AccessOverride
        The row.
    """
    row = AccessOverride(
        id=uuid.uuid4(),
        scope=scope,
        change=change,
        reason="incident 42",
        principal_id=world.admin.id,
        created_at=datetime(2026, 11, 5, 9, 30, tzinfo=UTC),
        audit_event_id=uuid.uuid4(),
    )
    session.add(row)
    session.commit()
    return row


def test_apply_makes_the_plan_and_records_the_scopes(session: Session) -> None:
    """Creates, changes, removals; one ACCESS_APPLIED; scope rows with hash and commit."""
    world = access_world(session)
    seed_every_kind(session, world)
    service = _service(session, world)
    text = service.export(world.admin, ["prod"]).replace("role: ANALYST", "role: VIEWER")
    plan = service.plan(world.admin, text)
    result = service.apply(
        world.admin, text, plan_fingerprint=plan.fingerprint, source_commit="abc1234"
    )
    assert (result.created, result.changed, result.removed) == (1, 0, 1)
    assert (result.file_sha256, result.source_commit, result.released) == (
        plan.file_sha256,
        "abc1234",
        (),
    )
    assert service.plan(world.admin, text).is_empty
    row = session.get(AccessManagedScope, "prod")
    assert row is not None
    assert (row.file_sha256, row.source_commit, row.apply_id) == (
        plan.file_sha256,
        "abc1234",
        result.apply_id,
    )
    assert (row.applied_by, row.file_name) == (world.admin.id, "access.yaml")
    applied = _events(session, AuditEventType.ACCESS_APPLIED)
    assert len(applied) == 1
    linked = _events(session, AuditEventType.GRANT_REVOKED) + _events(
        session, AuditEventType.GRANT_CREATED
    )
    linked = [e for e in linked if "access_apply_id" in e.detail]
    assert len(linked) == 3  # two copies revoked, one created
    assert {e.detail["access_apply_id"] for e in [*linked, *applied]} == {str(result.apply_id)}
    assert applied[0].detail == {
        "file_name": "access.yaml",
        "file_sha256": plan.file_sha256,
        "source_commit": "abc1234",
        "created": 1,
        "changed": 0,
        "removed": 1,
        "scopes": ["prod"],
        "released": [],
        "access_apply_id": str(result.apply_id),
    }


def test_apply_answers_409_when_access_changed_since_the_plan(session: Session) -> None:
    """Review focus 2: a grant made after the plan (scope not yet managed): 409, nothing changes."""
    world = access_world(session)
    service = _service(session, world)
    text = "version: 1\nmanages: [prod]\n"
    plan = service.plan(world.admin, text)
    GrantService(session, GrantAuthorization(session)).grant(
        world.admin,
        principal="bob",
        role=Role.VIEWER,
        kind=SecurableKind.ENVIRONMENT,
        environment="prod",
    )
    with pytest.raises(ConflictError, match=r"access changed since your plan.*plan again"):
        service.apply(world.admin, text, plan_fingerprint=plan.fingerprint)
    assert session.get(AccessManagedScope, "prod") is None
    assert _count(session, Grant, Grant.revoked_at.is_(None)) == 2


def test_a_failing_entry_in_the_middle_changes_nothing(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review focus 3: the third change raises; no row, no event, no scope record remains."""
    world = access_world(session)
    seed_every_kind(session, world)
    service = _service(session, world)
    text = _with_three_more_grants(service.export(world.admin, ["instance"]))
    plan = service.plan(world.admin, text)
    assert len(plan.creates) == 3, plan.problems
    before = _snapshot(session)
    calls = iter(range(10))
    original = GrantService.grant

    def failing(self: GrantService, *args: object, **kwargs: object) -> Grant:
        """Fail on the third call.

        Parameters
        ----------
        self
            The service.
        *args
            Passed on.
        **kwargs
            Passed on.

        Returns
        -------
        Grant
            The grant.

        Raises
        ------
        ValidationError
            On the third call.
        """
        if next(calls) == 2:
            raise ValidationError("boom")
        return original(self, *args, **kwargs)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(GrantService, "grant", failing)
    with pytest.raises(ValidationError, match="boom"):
        service.apply(world.admin, text, plan_fingerprint=plan.fingerprint)
    assert _snapshot(session) == before
    monkeypatch.undo()
    result = service.apply(world.admin, text, plan_fingerprint=plan.fingerprint)
    assert result.created == 3


def test_every_kind_of_entry_is_created_changed_and_removed(session: Session) -> None:
    """Each create, change and removal goes through its service; a second plan is empty."""
    world = access_world(session)
    seed_every_kind(session, world)
    GrantService(session, AllowAllAuthorization()).grant(
        world.admin,
        principal="bob",
        role=Role.ADMIN,
        kind=SecurableKind.ENVIRONMENT,
        environment="staging",
    )
    service = _service(session, world)

    def add(document: dict[str, Any]) -> None:
        """Add one entry of every kind, change the CI rule's kinds and drop the mask.

        Parameters
        ----------
        document
            The loaded file.
        """
        document["groups"]["ops"] = {"members": ["alice", "loader"]}
        document["service_accounts"].append("loader")
        document["grants"] = [
            g for g in document["grants"] if g.get("role") != "ADMIN" or g["on"] == "instance"
        ]
        document["grants"].append({"to": "service:loader", "role": "VIEWER", "on": "prod"})
        document["policies"] = [
            {
                "kind": "row_filter",
                "table": "prod.gold.customers",
                "expression": "id <> ''",
                "to": "group:ops",
            },
            {
                "kind": "column_mask",
                "table": "prod.gold.orders",
                "column": "email",
                "expression": "'x'",
                "to": "group:ops",
            },
            *(p for p in document["policies"] if p["kind"] == "row_filter"),
        ]
        [rule] = document["ci_trust"]
        rule["kinds"] = ["deploy"]
        document["ci_trust"].append(
            {
                **rule,
                "as": "service:loader",
                "on": "staging",
                "conditions": {**rule["conditions"], "ref": "refs/heads/dev"},
                "enabled": True,
            }
        )
        document["ci_trust"].append(
            {**rule, "conditions": {**rule["conditions"], "ref": "refs/heads/release"}}
        )

    text = _edit(service.export(world.admin, ["instance"]), add)
    plan = service.plan(world.admin, text)
    assert plan.problems == ()
    result = service.apply(world.admin, text, plan_fingerprint=plan.fingerprint)
    assert (result.created, result.changed, result.removed) == (9, 1, 2)
    assert service.plan(world.admin, text).is_empty
    assert (
        _count(session, CiTrustRule),
        _count(session, CiTrustRule, CiTrustRule.enabled.is_(True)),
    ) == (
        3,
        1,
    )
    enabled = text.replace("enabled: false", "enabled: true")
    plan = service.plan(world.admin, enabled)
    assert [c.op for c in plan.changes] == ["change", "change"]
    service.apply(world.admin, enabled, plan_fingerprint=plan.fingerprint)
    session.expire_all()
    assert all(r.enabled for r in session.scalars(select(CiTrustRule)))
    assert len(_events(session, AuditEventType.CI_TRUST_RULE_UPDATED)) == 1


def test_a_refusal_by_a_service_is_passed_on_while_the_caller_is_admin(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A service that refuses for its own reason keeps its error; nothing changes."""
    world = access_world(session)
    service = _service(session, world)
    text = "version: 1\nmanages: [prod]\ngrants:\n  - {to: user:bob, role: VIEWER, on: prod}\n"
    plan = service.plan(world.admin, text)

    def refuse(*_: object, **__: object) -> None:
        """Refuse.

        Parameters
        ----------
        *_
            Ignored.
        **__
            Ignored.

        Raises
        ------
        PermissionDenied
            Always.
        """
        raise PermissionDenied("no")

    monkeypatch.setattr(GrantService, "grant", refuse)
    with pytest.raises(PermissionDenied, match="no"):
        service.apply(world.admin, text, plan_fingerprint=plan.fingerprint)
    assert session.get(AccessManagedScope, "prod") is None


def test_removing_a_service_account_revokes_its_tokens_and_rules_first(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review focus 6: PATs revoked (PAT_REVOKED linked), CI rule and grants gone, inactive."""
    world = access_world(session)
    seed_every_kind(session, world)
    pats = PatService(session, GrantAuthorization(session))
    pats.issue(world.admin, "etl-bot", name="ci")
    old = pats.issue(world.admin, "etl-bot", name="old")
    pats.revoke(world.admin, "etl-bot", old.key_id)
    service = _service(session, world)

    def drop(document: dict[str, Any]) -> None:
        """Remove etl-bot and everything that names it.

        Parameters
        ----------
        document
            The loaded file.
        """
        document["service_accounts"] = []
        document["grants"] = [g for g in document["grants"] if g["to"] != "service:etl-bot"]
        del document["ci_trust"]

    text = _edit(service.export(world.admin, ["instance"]), drop)
    plan = service.plan(world.admin, text)
    assert plan.problems == ()
    seen = _recorded(monkeypatch)
    result = service.apply(world.admin, text, plan_fingerprint=plan.fingerprint)
    assert result.removed == 3
    assert _count(session, PersonalAccessToken, PersonalAccessToken.revoked_at.is_(None)) == 0
    assert _count(session, CiTrustRule) == 0
    etl = session.scalars(select(Principal).where(Principal.name == "etl-bot")).one()
    assert not etl.is_active
    revoked = _events(session, AuditEventType.PAT_REVOKED)
    linked = [e for e in revoked if e.detail.get("access_apply_id") == str(result.apply_id)]
    assert (len(revoked), len(linked)) == (2, 1)
    last = seen.index(AuditEventType.PRINCIPAL_DEACTIVATED)
    for earlier in (
        AuditEventType.CI_TRUST_RULE_DELETED,
        AuditEventType.GRANT_REVOKED,
        AuditEventType.PAT_REVOKED,
    ):
        assert seen.index(earlier) < last


def test_removing_a_group_revokes_its_grants_and_policies_before_deleting_it(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """delete_group refuses while a grant names the group; removals run in reverse order."""
    world = access_world(session)
    seed_every_kind(session, world)
    service = _service(session, world)

    def drop(document: dict[str, Any]) -> None:
        """Remove the group finance and everything that names it.

        Parameters
        ----------
        document
            The loaded file.
        """
        del document["groups"]["finance"]
        document["grants"] = [g for g in document["grants"] if g["to"] != "group:finance"]
        document["policies"] = [p for p in document["policies"] if p["to"] != "group:finance"]

    text = _edit(service.export(world.admin, ["instance"]), drop)
    plan = service.plan(world.admin, text)
    assert plan.problems == ()
    seen = _recorded(monkeypatch)
    service.apply(world.admin, text, plan_fingerprint=plan.fingerprint)
    assert _count(session, Group, Group.name == "finance") == 0
    assert _count(session, GroupMembership) == 0
    assert _count(session, RowFilter, RowFilter.revoked_at.is_(None)) == 0
    deleted = seen.index(AuditEventType.GROUP_DELETED)
    assert seen[deleted:] == [AuditEventType.GROUP_DELETED, AuditEventType.ACCESS_APPLIED]
    assert seen.index(AuditEventType.ROW_FILTER_REVOKED) < seen.index(AuditEventType.GRANT_REVOKED)
    assert seen.index(AuditEventType.GRANT_REVOKED) < seen.index(
        AuditEventType.GROUP_MEMBER_REMOVED
    )


def _admin_to(document: dict[str, Any], subject: str) -> None:
    """Give the instance ADMIN role to another subject instead of the administrator.

    Parameters
    ----------
    document
        The loaded file; changed in place.
    subject
        The subject that holds the role afterwards.
    """
    for grant in document["grants"]:
        if grant["to"] == "user:admin":
            grant["to"] = subject


@pytest.mark.parametrize(
    ("subject", "accounts", "message"),
    [
        ("service:robot", ["etl-bot", "robot"], "leaves no active ADMIN"),
        ("user:bob", ["etl-bot"], "removes your own ADMIN"),
    ],
)
def test_apply_rechecks_the_lockout_inside_the_transaction(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
    subject: str,
    accounts: list[str],
    message: str,
) -> None:
    """Review focus 1: the static check passes (patched), the in-transaction check refuses.

    A service account the file creates never counts as a remaining administrator, and without
    `allow_lockout` the caller keeps their own ADMIN role; nothing changes either way.
    """
    world = access_world(session)
    service = _service(session, world)

    def edit(document: dict[str, Any]) -> None:
        """Hand ADMIN over.

        Parameters
        ----------
        document
            The loaded file.
        """
        document["service_accounts"] = accounts
        _admin_to(document, subject)

    text = _edit(service.export(world.admin, ["instance"]), edit)
    monkeypatch.setattr(access_service, "admins_after", lambda *_: frozenset({"admin"}))
    plan = service.plan(world.admin, text)
    assert plan.problems == ()
    before = _snapshot(session)
    with pytest.raises(ConflictError, match=message):
        service.apply(world.admin, text, plan_fingerprint=plan.fingerprint)
    assert _snapshot(session) == before


def _admin_through_group(session: Session, world: AccessWorld) -> None:
    """Make the administrator's only ADMIN path the group ``admins`` (members admin and bob).

    Parameters
    ----------
    session
        Database session.
    world
        The world.
    """
    allow = AllowAllAuthorization()
    groups = GroupService(session, allow)
    groups.create_group(world.admin, name="admins")
    groups.create_group(world.admin, name="spare")
    for member in ("admin", "bob"):
        groups.add_member(world.admin, "admins", member)
    grants = GrantService(session, allow)
    grants.grant(world.admin, group="admins", role=Role.ADMIN, kind=SecurableKind.INSTANCE)
    direct = session.scalars(select(Grant).where(Grant.principal_id == world.admin.id)).one()
    grants.revoke(world.admin, direct.id)


def test_a_handover_runs_the_callers_own_admin_removal_last(session: Session) -> None:
    """With allow_lockout, the caller keeps ADMIN until every other change is made.

    Removals run in reverse dependency order, so the caller's own ADMIN grant would go before
    the service account whose tokens need MANAGE_TOKEN; it is moved to the end instead.
    """
    world = access_world(session)
    seed_every_kind(session, world)
    PatService(session, GrantAuthorization(session)).issue(world.admin, "etl-bot", name="ci")
    service = _service(session, world)

    def edit(document: dict[str, Any]) -> None:
        """Hand ADMIN to bob and remove etl-bot.

        Parameters
        ----------
        document
            The loaded file.
        """
        _admin_to(document, "user:bob")
        document["service_accounts"] = []
        document["grants"] = [g for g in document["grants"] if g["to"] != "service:etl-bot"]
        del document["ci_trust"]

    text = _edit(service.export(world.admin, ["instance"]), edit)
    plan = service.plan(world.admin, text, allow_lockout=True)
    assert plan.problems == ()
    service.apply(world.admin, text, plan_fingerprint=plan.fingerprint, allow_lockout=True)
    assert not holds_instance_admin(session, world.admin.id)
    assert holds_instance_admin(session, world.ref("bob").id)
    assert _count(session, PersonalAccessToken, PersonalAccessToken.revoked_at.is_(None)) == 0


def test_leaving_an_admin_group_runs_last(session: Session) -> None:
    """The caller's membership of the group that makes them ADMIN goes after other removals."""
    world = access_world(session)
    _admin_through_group(session, world)
    service = _service(session, world)

    def edit(document: dict[str, Any]) -> None:
        """Leave the group admins and delete the group spare.

        Parameters
        ----------
        document
            The loaded file.
        """
        document["groups"]["admins"]["members"] = ["bob"]
        del document["groups"]["spare"]

    text = _edit(service.export(world.admin, ["instance"]), edit)
    plan = service.plan(world.admin, text, allow_lockout=True)
    assert plan.problems == ()
    service.apply(world.admin, text, plan_fingerprint=plan.fingerprint, allow_lockout=True)
    assert not holds_instance_admin(session, world.admin.id)
    assert _count(session, Group, Group.name == "spare") == 0


def test_losing_admin_part_way_through_is_refused_with_two_steps(session: Session) -> None:
    """The caller's last ADMIN path needs two removals: the second could not be authorized.

    Nothing changes, and the message says how to do it in two applies.
    """
    world = access_world(session)
    _admin_through_group(session, world)
    service = _service(session, world)

    def edit(document: dict[str, Any]) -> None:
        """Give bob ADMIN directly, drop the group's grant and leave the group.

        Parameters
        ----------
        document
            The loaded file.
        """
        document["groups"]["admins"]["members"] = ["bob"]
        _admin_to(document, "user:bob")
        for grant in document["grants"]:
            if grant["to"] == "group:admins":
                grant["to"] = "user:bob"

    text = _edit(service.export(world.admin, ["instance"]), edit)
    plan = service.plan(world.admin, text, allow_lockout=True)
    assert plan.problems == ()
    before = _snapshot(session)
    with pytest.raises(ConflictError, match="Apply in two steps"):
        service.apply(world.admin, text, plan_fingerprint=plan.fingerprint, allow_lockout=True)
    assert _snapshot(session) == before
    assert _events(session, AuditEventType.AUTHORIZATION_DENIED) == []


def test_dropping_an_admin_group_the_caller_is_in_revokes_its_grant_first(
    session: Session,
) -> None:
    """Review I2: the group's grant moves to the end, and its deletion moves with it, after it."""
    world = access_world(session)
    _admin_through_group(session, world)
    GrantService(session, AllowAllAuthorization()).grant(
        world.admin, principal="admin", role=Role.ADMIN, kind=SecurableKind.INSTANCE
    )
    service = _service(session, world)

    def edit(document: dict[str, Any]) -> None:
        """Delete the group admins and its grant.

        Parameters
        ----------
        document
            The loaded file.
        """
        del document["groups"]["admins"]
        document["grants"] = [g for g in document["grants"] if g["to"] != "group:admins"]

    text = _edit(service.export(world.admin, ["instance"]), edit)
    plan = service.plan(world.admin, text)
    assert plan.problems == ()
    service.apply(world.admin, text, plan_fingerprint=plan.fingerprint)
    assert _count(session, Group, Group.name == "admins") == 0
    assert holds_instance_admin(session, world.admin.id)


def test_a_service_account_cannot_apply_its_own_removal(session: Session) -> None:
    """Review R1: removing the account that applies ends every ADMIN path; refused up front.

    The plan names it, the apply refuses with that problem before any write, and no denial is
    recorded: two steps cannot help, since the account is gone either way.
    """
    world = access_world(session)
    GrantService(session, AllowAllAuthorization()).grant(
        world.admin, principal="etl-bot", role=Role.ADMIN, kind=SecurableKind.INSTANCE
    )
    service = _service(session, world)
    bot = world.ref("etl-bot")

    def drop(document: dict[str, Any]) -> None:
        """Remove etl-bot and its grant.

        Parameters
        ----------
        document
            The loaded file.
        """
        document["service_accounts"] = []
        document["grants"] = [g for g in document["grants"] if g["to"] != "service:etl-bot"]

    text = _edit(service.export(bot, ["instance"]), drop)
    plan = service.plan(bot, text, allow_lockout=True)
    [problem] = plan.problems
    assert "removes service:etl-bot, the account applying it" in problem
    assert "have another administrator apply this file" in problem
    before = _snapshot(session)
    with pytest.raises(AccessFileError, match="have another administrator apply this file"):
        service.apply(bot, text, plan_fingerprint=plan.fingerprint, allow_lockout=True)
    assert _snapshot(session) == before
    assert _events(session, AuditEventType.AUTHORIZATION_DENIED) == []


def test_removing_the_callers_own_account_ends_every_path() -> None:
    """Review R1: the pre-write check counts the deactivation of the caller as the end."""
    bot = PrincipalRef(id=uuid.uuid4(), name="bot", type=PrincipalType.SERVICE_ACCOUNT)
    ordered = [
        PlannedChange("remove", ServiceAccountEntry("bot"), None),
        PlannedChange(
            "remove", GrantEntry(Subject("service", "bot"), "ADMIN", None, Target()), None
        ),
    ]
    assert admin_lost_part_way(ordered, bot, direct=True, caller_groups=set(), admin_groups=set())


def test_a_denial_part_way_rolls_back_and_is_audited_once(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review I1: the apply is rolled back, then the denial is recorded in its own commit."""
    world = access_world(session)
    service = _service(session, world)
    text = _policy_file("region = 'eu'").replace(
        "policies:", "grants:\n- {to: user:bob, role: VIEWER, on: prod}\npolicies:"
    )
    plan = service.plan(world.admin, text)
    assert plan.problems == ()
    original = GrantAuthorization.authorize

    def deny_policies_in_an_apply(
        self: GrantAuthorization, principal: PrincipalRef, action: Action, securable: Securable
    ) -> None:
        """Refuse MANAGE_POLICY while an apply runs; delegate otherwise.

        Parameters
        ----------
        self
            The authorization.
        principal
            The actor.
        action
            The action.
        securable
            The object.

        Raises
        ------
        PermissionDenied
            For MANAGE_POLICY inside an apply.
        """
        if action is Action.MANAGE_POLICY and ACCESS_APPLY_KEY in session.info:
            raise PermissionDenied("no policies today")
        original(self, principal, action, securable)

    monkeypatch.setattr(GrantAuthorization, "authorize", deny_policies_in_an_apply)
    before = _snapshot(session)
    with pytest.raises(PermissionDenied, match="no policies today"):
        service.apply(world.admin, text, plan_fingerprint=plan.fingerprint)
    session.expire_all()
    after = _snapshot(session)
    assert (after[:-1], after[-1]) == (before[:-1], before[-1] + 1)
    [denied] = _events(session, AuditEventType.AUTHORIZATION_DENIED)
    assert denied.detail == {"action": "MANAGE_POLICY"}


def test_a_change_outside_the_fingerprint_is_caught_by_the_recomputed_lockout(
    session: Session,
) -> None:
    """Ruling 1: the other administrator is deactivated between plan and apply.

    Who is active is not in the fingerprint; the plan made again under the lock finds the file
    now leaves no active administrator and refuses with nothing written.
    """
    world = access_world(session)
    service = _service(session, world)
    text = _edit(service.export(world.admin, ["instance"]), lambda d: _admin_to(d, "user:bob"))
    plan = service.plan(world.admin, text, allow_lockout=True)
    assert plan.problems == ()
    PrincipalService(session, GrantAuthorization(session)).deactivate_principal(world.admin, "bob")
    assert service.plan(world.admin, text, allow_lockout=True).fingerprint == plan.fingerprint
    before = _snapshot(session)
    with pytest.raises(AccessFileError) as raised:
        service.apply(world.admin, text, plan_fingerprint=plan.fingerprint, allow_lockout=True)
    assert any("leaves no active ADMIN" in problem for problem in raised.value.problems)
    assert _snapshot(session) == before


def test_dropping_a_scope_releases_it_and_touches_nothing(session: Session) -> None:
    """Spec §3 releasing: manages [prod, staging] then [prod]; plan says stops managing staging."""
    world = access_world(session)
    seed_every_kind(session, world)
    service = _service(session, world)
    both = service.export(world.admin, ["prod", "staging"])
    service.apply(world.admin, both, plan_fingerprint=service.plan(world.admin, both).fingerprint)
    _override(session, world, "staging", "grant VIEWER to alice")
    text = service.export(world.admin, ["prod"])
    plan = service.plan(world.admin, text)
    assert (plan.changes, plan.releases, plan.is_empty) == ((), ("staging",), False)
    before = _snapshot(session)
    result = service.apply(world.admin, text, plan_fingerprint=plan.fingerprint)
    assert result.released == ("staging",)
    assert session.get(AccessManagedScope, "staging") is None
    assert session.get(AccessManagedScope, "prod") is not None
    after = _snapshot(session)
    # one fewer managed scope and open override, one more event; access itself unchanged
    assert after[:9] == before[:9]
    assert after[9:] == (before[9] - 1, before[10] - 1, before[11] + 1)
    detail = _events(session, AuditEventType.ACCESS_APPLIED)[-1].detail
    assert (detail["scopes"], detail["released"]) == (["prod"], ["staging"])
    assert service.plan(world.admin, text).is_empty


def test_apply_with_problems_is_refused_with_every_problem(session: Session) -> None:
    """422 text = the plan's problems joined; nothing applied."""
    world = access_world(session)
    service = _service(session, world)
    text = (
        "version: 1\nmanages: [prod]\ngrants:\n"
        "  - {to: user:nobody, role: VIEWER, on: prod}\n"
        "  - {to: group:ghosts, role: VIEWER, on: prod}\n"
    )
    plan = service.plan(world.admin, text)
    assert len(plan.problems) == 2
    before = _snapshot(session)
    with pytest.raises(AccessFileError) as raised:
        service.apply(world.admin, text, plan_fingerprint=plan.fingerprint)
    assert str(raised.value) == "\n".join(plan.problems)
    assert _snapshot(session) == before


def test_a_file_that_does_not_parse_is_refused_with_its_problems(session: Session) -> None:
    """The plan of an unparsable file gives a fingerprint; apply refuses with the parse problem."""
    world = access_world(session)
    service = _service(session, world)
    plan = service.plan(world.admin, "version: 9\n")
    with pytest.raises(AccessFileError) as raised:
        service.apply(world.admin, "version: 9\n", plan_fingerprint=plan.fingerprint)
    assert raised.value.problems == plan.problems


def test_a_bad_source_commit_is_refused_before_anything(session: Session) -> None:
    """--source-commit takes a commit hash only."""
    world = access_world(session)
    service = _service(session, world)
    text = "version: 1\nmanages: [prod]\n"
    plan = service.plan(world.admin, text)
    for bad in ("main", "abc12", "x" * 40, "a" * 65):
        with pytest.raises(ValidationError, match="--source-commit"):
            service.apply(world.admin, text, plan_fingerprint=plan.fingerprint, source_commit=bad)
    assert session.get(AccessManagedScope, "prod") is None
    result = service.apply(
        world.admin, text, plan_fingerprint=plan.fingerprint, source_commit="A" * 40
    )
    assert result.source_commit == "a" * 40


def test_apply_and_status_need_the_admin_role(session: Session) -> None:
    """Only an instance administrator applies or reads status; refused before anything."""
    world = access_world(session)
    service = _service(session, world)
    with pytest.raises(PermissionDenied):
        service.apply(world.ref("alice"), "version: 1\nmanages: [prod]\n", plan_fingerprint="x")
    with pytest.raises(PermissionDenied):
        service.status(world.ref("alice"))


def _policy_file(expression: str) -> str:
    """Write a file for prod with one row filter for alice.

    Parameters
    ----------
    expression
        The filter.

    Returns
    -------
    str
        The file.
    """
    return yaml.safe_dump(
        {
            "version": 1,
            "manages": ["prod"],
            "policies": [
                {
                    "kind": "row_filter",
                    "table": "prod.gold.orders",
                    "expression": expression,
                    "to": "user:alice",
                }
            ],
        },
        sort_keys=False,
    )


def test_a_policy_applied_from_a_file_is_bound_by_field_id(session: Session) -> None:
    """#120: the created row has binding FIELD_IDS and the column's field id."""
    world = access_world(session)
    service = _service(session, world)
    text = _policy_file("region = 'eu'")
    service.apply(world.admin, text, plan_fingerprint=service.plan(world.admin, text).fingerprint)
    row = session.scalars(select(RowFilter)).one()
    assert (row.binding, row.field_ids) == (FIELD_IDS, {"region": 2})


def test_a_normalized_policy_from_a_file_runs_on_read(session: Session) -> None:
    """Ruling 6: HAS_GROUP and a typed literal (stored as CAST) resolve and pass the checker."""
    world = access_world(session)
    world.catalog.types = {"region": "date"}
    service = _service(session, world)
    written = "HAS_GROUP('finance') OR region >= DATE '2026-01-01'"
    text = _policy_file(written)
    service.apply(world.admin, text, plan_fingerprint=service.plan(world.admin, text).fingerprint)
    row = session.scalars(select(RowFilter)).one()
    assert row.expression == normalized_expression(written)
    assert "CAST('2026-01-01' AS DATE)" in row.expression
    assert row.table_id is not None
    fields = world.catalog.policy_fields(world.admin, row.table_id)
    assert fields is not None
    schema = PolicySchema.of(fields)
    resolved = resolve_expression(
        row.expression, row.field_ids, column=None, column_field_id=None, schema=schema
    )
    assert resolved.gone == ()
    validate_restricted_expression(resolved.expression, kind="filter")
    policy = PolicyResolver(session).resolve(
        world.ref("alice"), TableRef.from_dotted("prod", "gold.orders"), schema=schema
    )
    assert policy.gone is None
    [predicate] = policy.row_filters
    assert '"region"' in predicate
    assert "HAS_GROUP" not in predicate.upper()


def test_in_a_managed_scope_only_open_overrides_are_drift(session: Session) -> None:
    """F7: a removal in a managed scope is a plain removal; an open override is drift."""
    world = access_world(session)
    service = _service(session, world)
    text = "version: 1\nmanages: [prod]\n"
    grants = GrantService(session, GrantAuthorization(session))
    grants.grant(
        world.admin,
        principal="bob",
        role=Role.VIEWER,
        kind=SecurableKind.ENVIRONMENT,
        environment="prod",
    )
    first = service.plan(world.admin, text)
    assert [d.text for d in first.drift] == [
        "grant role VIEWER on environment prod to user:bob exists outside the file; "
        "apply removes it"
    ]
    service.apply(world.admin, text, plan_fingerprint=first.fingerprint)
    grants.grant(
        dataclasses.replace(world.admin, managed_override_reason="incident 42 hotfix"),
        principal="bob",
        role=Role.VIEWER,
        kind=SecurableKind.ENVIRONMENT,
        environment="prod",
    )
    override = session.scalars(select(AccessOverride)).one()
    _override(session, world, "staging", "grant VIEWER to alice")  # not a scope of this file
    plan = service.plan(world.admin, text)
    assert len(plan.removals) == 1
    [drift] = plan.drift
    assert (drift.scope, drift.by, drift.reason, drift.at) == (
        "prod",
        _AUTO,
        "incident 42 hotfix",
        override.created_at,
    )
    assert drift.text == "grant VIEWER to bob (override)"
    result = service.apply(world.admin, text, plan_fingerprint=plan.fingerprint)
    session.refresh(override)
    assert (override.resolved_by_apply, override.resolved_at is not None) == (result.apply_id, True)
    assert service.plan(world.admin, text).drift == ()


def test_status_lists_scopes_and_open_overrides(session: Session) -> None:
    """Spec §4 status."""
    world = access_world(session)
    service = _service(session, world)
    assert service.status(world.admin).scopes == ()
    text = "version: 1\nmanages: [prod, staging]\n"
    result = service.apply(
        world.admin,
        text,
        file_name="team.yaml",
        plan_fingerprint=service.plan(world.admin, text, file_name="team.yaml").fingerprint,
        source_commit="abc1234",
    )
    override = _override(session, world, "prod", "grant VIEWER to bob")
    resolved = _override(session, world, "prod", "grant VIEWER to alice")
    resolved.resolved_at = datetime(2026, 11, 5, 10, tzinfo=UTC)
    session.commit()
    status = service.status(world.admin)
    assert [s.scope for s in status.scopes] == ["prod", "staging"]
    prod = status.scopes[0]
    assert (prod.file_name, prod.file_sha256, prod.source_commit, prod.applied_by) == (
        "team.yaml",
        result.file_sha256,
        "abc1234",
        "admin",
    )
    assert prod.applied_at == result.applied_at
    [shown] = status.overrides
    assert (shown.scope, shown.change, shown.reason, shown.by, shown.at) == (
        "prod",
        "grant VIEWER to bob",
        "incident 42",
        "admin",
        override.created_at,
    )


@pytest.mark.parametrize(
    ("created", "lost"),
    [
        ((), True),
        ((GrantEntry(Subject("user", "admin"), "ADMIN", None, Target()),), False),
        (
            (
                GrantEntry(Subject("group", "new"), "ADMIN", None, Target()),
                MembershipEntry("new", "admin"),
            ),
            False,
        ),
        ((GrantEntry(Subject("group", "new"), "ADMIN", None, Target()),), True),
    ],
)
def test_paths_the_file_creates_keep_the_caller_admin(
    created: tuple[GrantEntry | MembershipEntry, ...], lost: bool
) -> None:
    """A direct grant or an admin group the caller joins, both made first, carry the removals."""
    admin = PrincipalRef(id=uuid.uuid4(), name="admin", type=PrincipalType.USER)
    ordered = [
        *(PlannedChange("create", entry, "x") for entry in created),
        PlannedChange("remove", MembershipEntry("old", "admin"), None),
        PlannedChange("remove", GroupEntry("spare"), None),
    ]
    found = admin_lost_part_way(
        ordered, admin, direct=False, caller_groups={"old"}, admin_groups={"old"}
    )
    assert found is lost
