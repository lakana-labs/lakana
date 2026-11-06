"""Tests for the managed-scope tables and for services that join a caller's transaction (#46)."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from types import SimpleNamespace
from typing import cast

import pytest
from access_test_helpers import AccessWorld, access_world, seed_every_kind
from sqlalchemy import ColumnElement, func, select
from sqlalchemy.orm import Session

from lakana_core.access.managed import ACCESS_LOCK_KEY, applying, finish_apply, lock_access
from lakana_core.audit.service import ACCESS_APPLY_KEY
from lakana_core.auth import AllowAllAuthorization
from lakana_core.ci_oidc.config import GITHUB_ISSUER
from lakana_core.ci_oidc.rules import CiTrustRuleService
from lakana_core.grants.service import GrantService
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
    PersonalAccessToken,
    PrincipalType,
    RowFilter,
)

_Call = Callable[[Session, AccessWorld], object]
_ALLOW = AllowAllAuthorization()


def _id_of(
    session: Session, model: type[RowFilter] | type[ColumnMask] | type[CiTrustRule]
) -> uuid.UUID:
    """Give the id of the first row of a model.

    Parameters
    ----------
    session
        Database session.
    model
        The model class.

    Returns
    -------
    uuid.UUID
        The row's id.
    """
    return session.scalars(select(model.id)).one()


def _rules(session: Session) -> CiTrustRuleService:
    """Build the CI rule service.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    CiTrustRuleService
        The service over the GitHub issuer.
    """
    return CiTrustRuleService(session, _ALLOW, issuers=[GITHUB_ISSUER])


def _issue_pat(session: Session, world: AccessWorld) -> None:
    """Issue a PAT for alice and commit it.

    Parameters
    ----------
    session
        Database session.
    world
        The seeded world.
    """
    PatService(session, _ALLOW).issue(world.admin, "alice", name="t")


def _revoke_pat(session: Session, world: AccessWorld) -> None:
    """Revoke alice's PAT with `commit=False`.

    Parameters
    ----------
    session
        Database session.
    world
        The seeded world.
    """
    key_id = session.scalars(select(PersonalAccessToken.key_id)).one()
    PatService(session, _ALLOW).revoke(world.admin, "alice", key_id, commit=False)


_PREPARE: dict[str, _Call] = {
    "revoke_pat": _issue_pat,
    "disable_ci_rule": lambda s, w: _rules(s).enable(w.admin, _id_of(s, CiTrustRule)),
}
"""Committed set-up a call needs before it is measured."""

_EVENTS: dict[str, AuditEventType] = {
    "create_group": AuditEventType.GROUP_CREATED,
    "delete_group": AuditEventType.GROUP_DELETED,
    "add_member": AuditEventType.GROUP_MEMBER_ADDED,
    "remove_member": AuditEventType.GROUP_MEMBER_REMOVED,
    "create_principal": AuditEventType.PRINCIPAL_CREATED,
    "deactivate_principal": AuditEventType.PRINCIPAL_DEACTIVATED,
    "revoke_pat": AuditEventType.PAT_REVOKED,
    "revoke_grant": AuditEventType.GRANT_REVOKED,
    "create_row_filter": AuditEventType.ROW_FILTER_CREATED,
    "revoke_row_filter": AuditEventType.ROW_FILTER_REVOKED,
    "create_column_mask": AuditEventType.COLUMN_MASK_CREATED,
    "revoke_column_mask": AuditEventType.COLUMN_MASK_REVOKED,
    "create_ci_rule": AuditEventType.CI_TRUST_RULE_CREATED,
    "update_ci_rule": AuditEventType.CI_TRUST_RULE_UPDATED,
    "enable_ci_rule": AuditEventType.CI_TRUST_RULE_ENABLED,
    "disable_ci_rule": AuditEventType.CI_TRUST_RULE_DISABLED,
    "delete_ci_rule": AuditEventType.CI_TRUST_RULE_DELETED,
}
"""The audit event each call records."""

_CALLS: dict[str, _Call] = {
    "create_group": lambda s, w: GroupService(s, _ALLOW).create_group(
        w.admin, name="g", commit=False
    ),
    "delete_group": lambda s, w: GroupService(s, _ALLOW).delete_group(
        w.admin, "nobody", commit=False
    ),
    "add_member": lambda s, w: GroupService(s, _ALLOW).add_member(
        w.admin, "pii", "alice", commit=False
    ),
    "remove_member": lambda s, w: GroupService(s, _ALLOW).remove_member(
        w.admin, "finance", "alice", commit=False
    ),
    "create_principal": lambda s, w: PrincipalService(s, _ALLOW).create_principal(
        w.admin, name="carol", type=PrincipalType.USER, commit=False
    ),
    "deactivate_principal": lambda s, w: PrincipalService(s, _ALLOW).deactivate_principal(
        w.admin, "bob", commit=False
    ),
    "revoke_pat": _revoke_pat,
    "revoke_grant": lambda s, w: GrantService(s, _ALLOW).revoke(
        w.admin,
        s.scalars(select(Grant.id).where(Grant.action.is_not(None))).first() or uuid.uuid4(),
        commit=False,
    ),
    "create_row_filter": lambda s, w: (
        __import__("lakana_core.policy.row_filters", fromlist=["x"])
        .RowFilterService(s, _ALLOW, w.catalog)
        .create(
            w.admin,
            group="pii",
            environment="prod",
            namespace="gold",
            table="orders",
            expression="TRUE",
            commit=False,
        )
    ),
    "revoke_row_filter": lambda s, w: (
        __import__("lakana_core.policy.row_filters", fromlist=["x"])
        .RowFilterService(s, _ALLOW, w.catalog)
        .revoke(w.admin, _id_of(s, RowFilter), commit=False)
    ),
    "create_column_mask": lambda s, w: (
        __import__("lakana_core.policy.column_masks", fromlist=["x"])
        .ColumnMaskService(s, _ALLOW, w.catalog)
        .create(
            w.admin,
            group="finance",
            environment="prod",
            namespace="gold",
            table="customers",
            column="email",
            expression="'x'",
            commit=False,
        )
    ),
    "revoke_column_mask": lambda s, w: (
        __import__("lakana_core.policy.column_masks", fromlist=["x"])
        .ColumnMaskService(s, _ALLOW, w.catalog)
        .revoke(w.admin, _id_of(s, ColumnMask), commit=False)
    ),
    "create_ci_rule": lambda s, w: _rules(s).create(
        w.admin,
        issuer="github",
        principal="etl-bot",
        environment="staging",
        repository="etl",
        kinds=["deploy"],
        conditions={"ref": "refs/heads/dev", "repository_id": "1", "event_name": "push"},
        commit=False,
    ),
    "update_ci_rule": lambda s, w: _rules(s).update(
        w.admin, _id_of(s, CiTrustRule), kinds=["deploy"], commit=False
    ),
    "enable_ci_rule": lambda s, w: _rules(s).enable(w.admin, _id_of(s, CiTrustRule), commit=False),
    "disable_ci_rule": lambda s, w: _rules(s).disable(
        w.admin, _id_of(s, CiTrustRule), commit=False
    ),
    "delete_ci_rule": lambda s, w: _rules(s).delete(w.admin, _id_of(s, CiTrustRule), commit=False),
}


def _state(session: Session) -> tuple[int, ...]:
    """Count what a change touches, and the audit events.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    tuple of int
        Counts, plus the rows that are revoked, deactivated or disabled.
    """
    from lakana_core.models import GroupMembership, Principal

    def count(*where: ColumnElement[bool], model: type = Group) -> int:
        """Count the rows of a model that match the conditions.

        Parameters
        ----------
        *where
            The conditions.
        model
            The model to count.

        Returns
        -------
        int
            The number of rows.
        """
        return session.scalar(select(func.count()).select_from(model).where(*where)) or 0

    counts = [
        count(model=model)
        for model in (
            Group,
            GroupMembership,
            Principal,
            Grant,
            RowFilter,
            ColumnMask,
            CiTrustRule,
            AuditEvent,
        )
    ]
    counts.append(count(Grant.revoked_at.is_not(None), model=Grant))
    counts.append(count(Principal.is_active.is_(False), model=Principal))
    counts.append(count(CiTrustRule.enabled.is_(True), model=CiTrustRule))
    return tuple(counts)


def test_the_managed_tables_hold_a_scope_and_an_override(session: Session) -> None:
    """Spec §4: a managed scope and an override are rows that can be stored and read back."""
    import datetime as dt

    world = access_world(session)
    now = dt.datetime.now(dt.UTC)
    session.add(
        AccessManagedScope(
            scope="prod",
            file_sha256="0" * 64,
            file_name="access.yaml",
            source_commit=None,
            apply_id=uuid.uuid4(),
            applied_by=world.admin.id,
            applied_at=now,
        )
    )
    session.add(
        AccessOverride(
            id=uuid.uuid4(),
            scope="prod",
            change="grant:x",
            reason="why",
            principal_id=world.admin.id,
            created_at=now,
            audit_event_id=uuid.uuid4(),
        )
    )
    session.commit()
    assert session.scalars(select(AccessOverride.resolved_at)).one() is None
    assert session.scalars(select(AccessManagedScope.scope)).one() == "prod"


def test_a_commit_inside_an_apply_is_refused(session: Session) -> None:
    """Review focus 3: a service that commits inside `applying` raises; nothing is written."""
    world = access_world(session)
    groups = GroupService(session, AllowAllAuthorization())
    with pytest.raises(RuntimeError, match="commits once"), applying(session, uuid.uuid4()):
        groups.create_group(world.admin, name="g")  # commit=True by default
    session.rollback()
    assert session.scalar(select(func.count()).select_from(Group).where(Group.name == "g")) == 0


def test_events_inside_an_apply_carry_its_id(session: Session) -> None:
    """Spec §4 audit: each change's event is linked to the apply."""
    world = access_world(session)
    apply_id = uuid.uuid4()
    with applying(session, apply_id):
        GroupService(session, AllowAllAuthorization()).create_group(
            world.admin, name="g", commit=False
        )
        finish_apply(session)
    event = session.scalars(
        select(AuditEvent).where(AuditEvent.event_type == AuditEventType.GROUP_CREATED)
    ).one()
    assert event.detail["access_apply_id"] == str(apply_id)
    assert ACCESS_APPLY_KEY not in session.info


def test_an_apply_that_fails_leaves_the_session_usable(session: Session) -> None:
    """The listener and the key go even when the block raises, so later commits work.

    Parameters
    ----------
    session
        Database session.

    Raises
    ------
    ValueError
        Never; the test expects the block's error and catches it.
    """
    world = access_world(session)
    with pytest.raises(ValueError, match="boom"), applying(session, uuid.uuid4()):
        raise ValueError("boom")
    assert ACCESS_APPLY_KEY not in session.info
    GroupService(session, _ALLOW).create_group(world.admin, name="after")
    assert session.scalar(select(func.count()).select_from(Group).where(Group.name == "after")) == 1


def test_applying_clears_a_final_flag_left_behind(session: Session) -> None:
    """Task 5 review M1: a stray flag from earlier never opens a later apply to commits."""
    world = access_world(session)
    session.info["lakana.access_apply_final"] = True
    groups = GroupService(session, _ALLOW)
    with pytest.raises(RuntimeError, match="commits once"), applying(session, uuid.uuid4()):
        groups.create_group(world.admin, name="g")
    session.rollback()
    assert session.scalar(select(func.count()).select_from(Group).where(Group.name == "g")) == 0


def test_finish_apply_outside_an_apply_is_refused(session: Session) -> None:
    """Task 5 review M1: `finish_apply` commits only inside `applying`."""
    with pytest.raises(RuntimeError, match="only inside an access apply"):
        finish_apply(session)
    assert "lakana.access_apply_final" not in session.info


def test_lock_access_does_nothing_off_postgres(session: Session) -> None:
    """On SQLite the advisory lock is skipped; the key is a signed 64-bit integer."""
    lock_access(session, shared=False)
    lock_access(session, shared=True)
    assert -(2**63) <= ACCESS_LOCK_KEY < 2**63


class _PostgresSession:
    """A session double whose bind says Postgres and that records what it executes."""

    def __init__(self) -> None:
        """Start with nothing executed."""
        self.executed: list[tuple[str, dict[str, int]]] = []

    def get_bind(self) -> SimpleNamespace:
        """Return a bind whose dialect is Postgres.

        Returns
        -------
        SimpleNamespace
            An object with ``dialect.name == "postgresql"``.
        """
        return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

    def execute(self, statement: object, params: dict[str, int]) -> None:
        """Record a statement and its parameters.

        Parameters
        ----------
        statement
            The statement.
        params
            Its parameters.
        """
        self.executed.append((str(statement), params))


@pytest.mark.parametrize(
    ("shared", "function"),
    [(True, "pg_advisory_xact_lock_shared"), (False, "pg_advisory_xact_lock")],
)
def test_lock_access_takes_the_transaction_lock_on_postgres(shared: bool, function: str) -> None:
    """On Postgres a guarded change takes the shared lock and an apply the exclusive one."""
    fake = _PostgresSession()
    lock_access(cast(Session, fake), shared=shared)
    assert fake.executed == [(f"SELECT {function}(:key)", {"key": ACCESS_LOCK_KEY})]


def test_every_call_is_covered() -> None:
    """The brief names 17 methods; the table below must list all of them."""
    assert len(_CALLS) == 17
    assert set(_EVENTS) == set(_CALLS)


def _events_of(session: Session, event_type: AuditEventType) -> int:
    """Count the audit events of one type the session holds, flushed or committed.

    Parameters
    ----------
    session
        Database session.
    event_type
        The type.

    Returns
    -------
    int
        The number of events.
    """
    found = select(func.count()).select_from(AuditEvent)
    return session.scalar(found.where(AuditEvent.event_type == event_type)) or 0


@pytest.mark.parametrize("name", list(_CALLS))
def test_commit_false_leaves_the_change_uncommitted(session: Session, name: str) -> None:
    """Each of the 17 methods: with commit=False, a rollback undoes it and its audit event."""
    world = access_world(session)
    seed_every_kind(session, world)
    if name in _PREPARE:
        _PREPARE[name](session, world)
    before = _state(session)
    events = _events_of(session, _EVENTS[name])
    _CALLS[name](session, world)
    session.flush()
    assert _state(session) != before
    assert _events_of(session, _EVENTS[name]) == events + 1
    session.rollback()
    assert _state(session) == before
