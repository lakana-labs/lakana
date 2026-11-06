"""Tests for the guard of managed scopes: changes outside the file, and the override (#46)."""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Callable

import pytest
from access_test_helpers import AccessWorld, access_world, seed_every_kind
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from lakana_core.access.managed import ManagedScopeError, guard_change
from lakana_core.access.service import AccessService
from lakana_core.audit.authorization import AuditingAuthorization
from lakana_core.audit.denial_limiter import DenialAuditLimiter
from lakana_core.auth import Action, AllowAllAuthorization, PrincipalRef, SecurableKind
from lakana_core.bootstrap import bootstrap_if_needed
from lakana_core.ci_oidc.config import GITHUB_ISSUER
from lakana_core.ci_oidc.rules import CiTrustRuleService
from lakana_core.errors import PermissionDenied, ValidationError
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.grants.roles import Role
from lakana_core.grants.service import GrantService
from lakana_core.identity.groups import GroupService
from lakana_core.identity.principals import PrincipalService
from lakana_core.models import (
    AccessOverride,
    AuditEvent,
    AuditEventType,
    CiTrustRule,
    ColumnMask,
    Grant,
    Principal,
    PrincipalType,
    RowFilter,
)
from lakana_core.policy.column_masks import ColumnMaskService
from lakana_core.policy.row_filters import RowFilterService

_ADMIN_KEPT = "grants:\n  - {to: user:admin, role: ADMIN, on: instance}\n"
_REASON = "Incident 42 hotfix"


def _authz(session: Session) -> AuditingAuthorization:
    """Build the authorization the server uses.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    AuditingAuthorization
        Over `GrantAuthorization`, auditing denials.
    """
    return AuditingAuthorization(GrantAuthorization(session), session, DenialAuditLimiter(100))


def _access(session: Session, world: AccessWorld) -> AccessService:
    """Build the access service.

    Parameters
    ----------
    session
        Database session.
    world
        The world.

    Returns
    -------
    AccessService
        With GitHub as the one CI issuer.
    """
    return AccessService(session, _authz(session), world.catalog, issuers=[GITHUB_ISSUER])


def _apply(session: Session, world: AccessWorld, text: str) -> None:
    """Plan a file and apply it with the plan's fingerprint.

    Parameters
    ----------
    session
        Database session.
    world
        The world.
    text
        The file.
    """
    service = _access(session, world)
    plan = service.plan(world.admin, text)
    assert not plan.problems, plan.problems
    service.apply(world.admin, text, plan_fingerprint=plan.fingerprint)


def _manage(session: Session, world: AccessWorld, scope: str, *, enabled: bool = False) -> None:
    """Seed every kind and apply the export of one scope, so that scope is managed.

    Parameters
    ----------
    session
        Database session.
    world
        The world.
    scope
        ``instance`` or ``prod``.
    enabled
        Enable the seeded CI rule first, so that disabling it is a change.
    """
    seed_every_kind(session, world)
    if enabled:
        rules = CiTrustRuleService(session, AllowAllAuthorization(), issuers=[GITHUB_ISSUER])
        rules.enable(world.admin, _rule_id(session))
    _apply(session, world, _access(session, world).export(world.admin, [scope]))


def _overriding(p: PrincipalRef) -> PrincipalRef:
    """Give a principal the override reason a request carries.

    Parameters
    ----------
    p
        The principal.

    Returns
    -------
    PrincipalRef
        A copy with `managed_override_reason` set.
    """
    return dataclasses.replace(p, managed_override_reason=_REASON)


def _rule_id(session: Session) -> uuid.UUID:
    """Read the id of the one CI trust rule.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    uuid.UUID
        Its id.
    """
    return session.scalars(select(CiTrustRule.id)).one()


def _id(session: Session, model: type[Grant | RowFilter | ColumnMask]) -> uuid.UUID:
    """Read the id of the first active row of a model in `prod`.

    Parameters
    ----------
    session
        Database session.
    model
        The model.

    Returns
    -------
    uuid.UUID
        Its id.
    """
    found = select(model.id).where(model.revoked_at.is_(None), model.environment == "prod")
    return session.scalars(found.limit(1)).one()


_Call = Callable[[Session, AccessWorld, PrincipalRef], object]


def _grants(session: Session) -> GrantService:
    """Build the grant service.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    GrantService
        With the server's authorization.
    """
    return GrantService(session, _authz(session))


def _rules(session: Session) -> CiTrustRuleService:
    """Build the CI trust rule service.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    CiTrustRuleService
        With GitHub as the one issuer.
    """
    return CiTrustRuleService(session, _authz(session), issuers=[GITHUB_ISSUER])


_CALLS: dict[str, tuple[str, _Call]] = {
    "grant": (
        "prod",
        lambda s, w, p: _grants(s).grant(
            p, principal="bob", role=Role.VIEWER, kind=SecurableKind.ENVIRONMENT, environment="prod"
        ),
    ),
    "revoke": ("prod", lambda s, w, p: _grants(s).revoke(p, _id(s, Grant))),
    "create_group": (
        "instance",
        lambda s, w, p: GroupService(s, _authz(s)).create_group(p, name="ops"),
    ),
    "delete_group": (
        "instance",
        lambda s, w, p: GroupService(s, _authz(s)).delete_group(p, "nobody"),
    ),
    "add_member": (
        "instance",
        lambda s, w, p: GroupService(s, _authz(s)).add_member(p, "pii", "bob"),
    ),
    "remove_member": (
        "instance",
        lambda s, w, p: GroupService(s, _authz(s)).remove_member(p, "finance", "alice"),
    ),
    "create_service_account": (
        "instance",
        lambda s, w, p: PrincipalService(s, _authz(s)).create_principal(
            p, name="new-bot", type=PrincipalType.SERVICE_ACCOUNT
        ),
    ),
    "deactivate_service_account": (
        "instance",
        lambda s, w, p: PrincipalService(s, _authz(s)).deactivate_principal(p, "etl-bot"),
    ),
    "create_row_filter": (
        "prod",
        lambda s, w, p: RowFilterService(s, _authz(s), w.catalog).create(
            p,
            principal="bob",
            environment="prod",
            namespace="gold",
            table="orders",
            expression="region = 'EU'",
        ),
    ),
    "revoke_row_filter": (
        "prod",
        lambda s, w, p: RowFilterService(s, _authz(s), w.catalog).revoke(p, _id(s, RowFilter)),
    ),
    "create_column_mask": (
        "prod",
        lambda s, w, p: ColumnMaskService(s, _authz(s), w.catalog).create(
            p,
            principal="bob",
            environment="prod",
            namespace="gold",
            table="orders",
            column="email",
            expression="'***'",
        ),
    ),
    "revoke_column_mask": (
        "prod",
        lambda s, w, p: ColumnMaskService(s, _authz(s), w.catalog).revoke(p, _id(s, ColumnMask)),
    ),
    "create_ci_rule": (
        "prod",
        lambda s, w, p: _rules(s).create(
            p,
            issuer="github",
            principal="etl-bot",
            environment="prod",
            repository="etl",
            kinds=["deploy"],
            conditions={"ref": "refs/heads/release", "repository_id": "1", "event_name": "push"},
        ),
    ),
    "update_ci_rule": (
        "prod",
        lambda s, w, p: _rules(s).update(p, _rule_id(s), kinds=["deploy"]),
    ),
    "enable_ci_rule": ("prod", lambda s, w, p: _rules(s).enable(p, _rule_id(s))),
    "disable_ci_rule": ("prod", lambda s, w, p: _rules(s).disable(p, _rule_id(s))),
    "delete_ci_rule": ("prod", lambda s, w, p: _rules(s).delete(p, _rule_id(s))),
}


def test_a_managed_scope_refuses_a_change_with_the_next_step(session: Session) -> None:
    """Spec §3: 409 text names the scope, the last apply and both ways forward."""
    world = access_world(session)
    _apply(session, world, "version: 1\nmanages: [prod]\n")
    grants = _grants(session)
    with pytest.raises(ManagedScopeError) as caught:
        grants.grant(
            world.admin,
            principal="bob",
            role=Role.VIEWER,
            kind=SecurableKind.ENVIRONMENT,
            environment="prod",
        )
    message = str(caught.value)
    assert message.startswith("`prod` is managed by an access file (last applied ")
    assert message.endswith(
        "); change the file and run `lakana access apply`, or, in an emergency, repeat with "
        "`--override-managed --reason …`"
    )
    grants.grant(
        world.admin,
        principal="bob",
        role=Role.VIEWER,
        kind=SecurableKind.ENVIRONMENT,
        environment="staging",
    )


@pytest.mark.parametrize("name", sorted(_CALLS))
def test_every_guarded_change_is_refused_and_can_be_overridden(session: Session, name: str) -> None:
    """Review focus 4: each of the 17 service calls; refused, then overridden and recorded."""
    world = access_world(session)
    scope, call = _CALLS[name]
    _manage(session, world, scope, enabled=name == "disable_ci_rule")
    with pytest.raises(ManagedScopeError, match=rf"^`{scope}` is managed"):
        call(session, world, world.admin)
    session.rollback()
    call(session, world, _overriding(world.admin))
    row = session.scalars(select(AccessOverride)).one()
    assert (row.scope, row.reason, row.principal_id) == (scope, _REASON, world.admin.id)
    assert len(row.change) <= 255
    event = session.get(AuditEvent, row.audit_event_id)
    assert event is not None
    assert event.event_type is AuditEventType.MANAGED_SCOPE_OVERRIDDEN
    assert event.detail == {"scope": scope, "change": row.change, "reason": _REASON}


def test_an_override_is_audited_and_shows_as_drift(session: Session) -> None:
    """Review focus 5: event + open row; plan lists it; next apply resolves and reverts it."""
    world = access_world(session)
    text = "version: 1\nmanages: [prod]\n"
    _apply(session, world, text)
    _grants(session).grant(
        _overriding(world.admin),
        principal="bob",
        role=Role.VIEWER,
        kind=SecurableKind.ENVIRONMENT,
        environment="prod",
    )
    plan = _access(session, world).plan(world.admin, text)
    [drift] = plan.drift
    assert (drift.scope, drift.text, drift.by, drift.reason) == (
        "prod",
        "grant VIEWER to bob (override)",
        "admin",
        _REASON,
    )
    _apply(session, world, text)
    row = session.scalars(select(AccessOverride)).one()
    assert row.resolved_at is not None
    live = select(func.count()).select_from(Grant).where(Grant.environment == "prod")
    assert session.scalar(live.where(Grant.revoked_at.is_(None))) == 0
    assert not _access(session, world).plan(world.admin, text).drift


def test_an_override_needs_instance_admin(session: Session) -> None:
    """Review focus 5: MANAGE_POLICY alone cannot override a managed scope."""
    world = access_world(session)
    GrantService(session, AllowAllAuthorization()).grant(
        world.admin, principal="alice", action=Action.MANAGE_POLICY, kind=SecurableKind.INSTANCE
    )
    _apply(session, world, "version: 1\nmanages: [prod]\n")
    _, call = _CALLS["create_row_filter"]
    with pytest.raises(PermissionDenied, match="Only an instance administrator may override"):
        call(session, world, _overriding(world.ref("alice")))
    denied = session.scalars(
        select(AuditEvent).where(AuditEvent.event_type == AuditEventType.AUTHORIZATION_DENIED)
    ).one()
    assert denied.detail["reason"] == "managed_override_not_admin"
    assert session.scalar(select(func.count()).select_from(AccessOverride)) == 0


def test_a_refused_override_without_an_auditing_authorization_records_nothing(
    session: Session,
) -> None:
    """Only `AuditingAuthorization` records a denial; a plain one refuses the same way."""
    world = access_world(session)
    GrantService(session, AllowAllAuthorization()).grant(
        world.admin, principal="alice", action=Action.MANAGE_PRINCIPAL, kind=SecurableKind.INSTANCE
    )
    alice = "  - {to: user:alice, action: MANAGE_PRINCIPAL, on: instance}\n"
    _apply(session, world, "version: 1\nmanages: [instance]\n" + _ADMIN_KEPT + alice)
    principals = PrincipalService(session, GrantAuthorization(session))
    with pytest.raises(PermissionDenied, match="Only an instance administrator may override"):
        principals.create_principal(
            _overriding(world.ref("alice")), name="bot2", type=PrincipalType.SERVICE_ACCOUNT
        )
    denied = select(func.count()).select_from(AuditEvent)
    assert (
        session.scalar(denied.where(AuditEvent.event_type == AuditEventType.AUTHORIZATION_DENIED))
        == 0
    )


@pytest.mark.parametrize("reason", ["short", "x" * 501, "two\nlines"])
def test_an_override_reason_is_checked(session: Session, reason: str) -> None:
    """10 to 500 characters on one line."""
    world = access_world(session)
    _apply(session, world, "version: 1\nmanages: [prod]\n")
    _, call = _CALLS["grant"]
    with pytest.raises(ValidationError, match="An override reason must"):
        call(session, world, dataclasses.replace(world.admin, managed_override_reason=reason))


def test_an_override_on_a_scope_nobody_manages_is_refused(session: Session) -> None:
    """R11."""
    world = access_world(session)
    _, call = _CALLS["grant"]
    with pytest.raises(ValidationError) as caught:
        call(session, world, _overriding(world.admin))
    assert str(caught.value) == (
        "`prod` is not managed by an access file; repeat without `--override-managed`"
    )


def test_instance_wide_objects_are_guarded_only_when_instance_is_managed(session: Session) -> None:
    """R3: create_group passes with manages [prod], refuses with [instance]."""
    world = access_world(session)
    groups = GroupService(session, _authz(session))
    _apply(session, world, "version: 1\nmanages: [prod]\n")
    groups.create_group(world.admin, name="ops")
    _apply(
        session,
        world,
        "version: 1\nmanages: [instance]\ngroups:\n  ops: {members: []}\n" + _ADMIN_KEPT,
    )
    with pytest.raises(ManagedScopeError, match=r"^`instance` is managed"):
        groups.create_group(world.admin, name="ops2")


def test_instance_manages_every_environment(session: Session) -> None:
    """A file that manages `instance` owns `prod` too: the refusal and the override name it."""
    world = access_world(session)
    _apply(session, world, "version: 1\nmanages: [instance]\n" + _ADMIN_KEPT)
    _, call = _CALLS["grant"]
    with pytest.raises(ManagedScopeError, match=r"^`instance` is managed"):
        call(session, world, world.admin)
    call(session, world, _overriding(world.admin))
    assert session.scalars(select(AccessOverride.scope)).one() == "instance"


def test_a_user_principal_is_never_guarded(session: Session) -> None:
    """People are not in an access file: creating and deactivating one passes."""
    world = access_world(session)
    _apply(session, world, "version: 1\nmanages: [instance]\n" + _ADMIN_KEPT)
    principals = PrincipalService(session, _authz(session))
    principals.create_principal(world.admin, name="carol", type=PrincipalType.USER)
    principals.deactivate_principal(world.admin, "carol")


def test_a_change_that_changes_nothing_is_not_guarded(session: Session) -> None:
    """Repeating an idempotent call in a managed scope passes: there is nothing to refuse."""
    world = access_world(session)
    _manage(session, world, "prod")
    _rules(session).disable(world.admin, _rule_id(session))
    _grants(session).revoke(world.admin, _revoked(session))
    assert session.scalar(select(func.count()).select_from(AccessOverride)) == 0


def _revoked(session: Session) -> uuid.UUID:
    """Revoke a prod grant by hand, as before the scope was managed, and give its id.

    Parameters
    ----------
    session
        Database session; committed.

    Returns
    -------
    uuid.UUID
        The revoked grant's id.
    """
    grant = session.get_one(Grant, _id(session, Grant))
    grant.revoked_at = grant.granted_at
    session.commit()
    return grant.id


def test_apply_passes_its_own_guard(session: Session) -> None:
    """A second apply in a managed scope succeeds (the guard returns inside `applying`)."""
    world = access_world(session)
    _apply(session, world, "version: 1\nmanages: [prod]\n")
    _apply(
        session,
        world,
        "version: 1\nmanages: [prod]\ngrants:\n  - {to: user:bob, role: VIEWER, on: prod}\n",
    )
    assert session.scalar(select(func.count()).select_from(AccessOverride)) == 0


def test_a_long_change_is_cut(session: Session) -> None:
    """The change text fits its column: 255 characters, the last an ellipsis."""
    world = access_world(session)
    _apply(session, world, "version: 1\nmanages: [instance]\n" + _ADMIN_KEPT)
    guard_change(session, _overriding(world.admin), environment=None, change="x" * 300)
    row = session.scalars(select(AccessOverride)).one()
    assert (len(row.change), row.change[-1]) == (255, "…")


def test_lockout_recovery_passes_the_guard_and_shows_as_drift(session: Session) -> None:
    """F15: the server-start admin grant is the one exemption; the next plan lists it."""
    world = access_world(session)
    text = "version: 1\nmanages: [instance]\n" + _ADMIN_KEPT
    _apply(session, world, text)
    admin = session.get(Principal, world.admin.id)
    assert admin is not None
    admin.is_active = False
    session.commit()
    assert bootstrap_if_needed(session, "root", key_id="testkey", secret="testsecret")
    row = session.scalars(select(AccessOverride)).one()
    assert (row.scope, row.change) == ("instance", "grant ADMIN to root")
    event = session.get(AuditEvent, row.audit_event_id)
    assert event is not None and event.event_type is AuditEventType.MANAGED_SCOPE_OVERRIDDEN
    root = session.scalars(select(Principal).where(Principal.name == "root")).one()
    root_ref = PrincipalRef(id=root.id, name="root", type=PrincipalType.USER)
    whats = [d.text for d in _access(session, world).plan(root_ref, text).drift]
    assert whats == ["grant ADMIN to root (override)"]


def test_the_recovery_exemption_is_only_for_the_bootstrap(session: Session) -> None:
    """Outside lockout recovery the same grant is refused: nothing else is exempt."""
    world = access_world(session)
    _apply(session, world, "version: 1\nmanages: [instance]\n" + _ADMIN_KEPT)
    with pytest.raises(ManagedScopeError):
        GrantService(session, AllowAllAuthorization()).grant(
            world.admin, principal="bob", role=Role.ADMIN, kind=SecurableKind.INSTANCE
        )


def test_lockout_recovery_without_a_managed_scope_records_no_override(session: Session) -> None:
    """With no file applied, the recovery grant is a plain grant: nothing to show as drift."""
    assert bootstrap_if_needed(session, "root", key_id="testkey", secret="testsecret")
    assert session.scalar(select(func.count()).select_from(AccessOverride)) == 0


def test_an_override_on_a_person_is_refused(session: Session) -> None:
    """Strict, as R11: people are never in a file, so an override on one is a stale flag."""
    world = access_world(session)
    principals = PrincipalService(session, _authz(session))
    message = "people are not managed by an access file; repeat without `--override-managed`"
    with pytest.raises(ValidationError) as created:
        principals.create_principal(_overriding(world.admin), name="carol", type=PrincipalType.USER)
    assert str(created.value) == message
    with pytest.raises(ValidationError) as deactivated:
        principals.deactivate_principal(_overriding(world.admin), "bob")
    assert str(deactivated.value) == message
    bob = session.scalars(select(Principal).where(Principal.name == "bob")).one()
    assert bob.is_active
