"""`GrantService.has_any_grant`: whether a person has any right at all, at any scope."""

from __future__ import annotations

import dataclasses
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from lakana_core.auth import (
    Action,
    AllowAllAuthorization,
    PrincipalRef,
    SecurableKind,
    TaskScope,
)
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.grants.roles import Role
from lakana_core.grants.service import GrantService
from lakana_core.models import Grant, Group, GroupMembership, Principal, PrincipalType

pytestmark = pytest.mark.filterwarnings("error")


def _principal(session: Session) -> PrincipalRef:
    """Persist an active principal.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    PrincipalRef
        A reference to it.
    """
    ref = PrincipalRef(id=uuid.uuid4(), name=f"p-{uuid.uuid4().hex[:8]}", type=PrincipalType.USER)
    session.add(Principal(id=ref.id, type=ref.type, name=ref.name, is_active=True))
    session.commit()
    return ref


def _grant(session: Session, granter: uuid.UUID, **fields: object) -> Grant:
    """Persist a grant.

    Parameters
    ----------
    session
        Database session.
    granter
        `granted_by`.
    **fields
        The grant's columns.

    Returns
    -------
    Grant
        The row.
    """
    row = Grant(id=uuid.uuid4(), granted_by=granter, granted_at=datetime.now(UTC), **fields)
    session.add(row)
    session.commit()
    return row


def _has(session: Session, ref: PrincipalRef) -> bool:
    """Ask the service.

    Parameters
    ----------
    session
        Database session.
    ref
        The principal.

    Returns
    -------
    bool
        `has_any_grant`'s answer.
    """
    return GrantService(session, AllowAllAuthorization()).has_any_grant(ref)


def test_a_person_without_grants_has_none(session: Session) -> None:
    assert _has(session, _principal(session)) is False


@pytest.mark.parametrize(
    "scope",
    [
        {"kind": SecurableKind.INSTANCE, "action": Action.READ_AUDIT_LOG},
        {"kind": SecurableKind.ENVIRONMENT, "environment": "dev", "role": Role.ANALYST},
        {
            "kind": SecurableKind.NAMESPACE,
            "environment": "dev",
            "namespace": "sales",
            "action": Action.READ_NAMESPACE,
        },
        {
            "kind": SecurableKind.TABLE,
            "environment": "dev",
            "namespace": "sales",
            "table": "orders",
            "action": Action.READ_TABLE,
        },
    ],
    ids=["instance", "environment", "namespace-only", "table-only"],
)
def test_a_direct_grant_at_any_scope_counts(session: Session, scope: dict[str, object]) -> None:
    subject = _principal(session)
    other = _principal(session)
    _grant(session, subject.id, principal_id=subject.id, **scope)

    assert _has(session, subject) is True
    assert _has(session, other) is False


def test_a_grant_through_a_group_counts(session: Session) -> None:
    subject = _principal(session)
    group = Group(id=uuid.uuid4(), name="analysts")
    session.add(group)
    session.flush()
    _grant(
        session,
        subject.id,
        group_id=group.id,
        kind=SecurableKind.TABLE,
        environment="dev",
        namespace="sales",
        table="orders",
        action=Action.READ_TABLE,
    )
    assert _has(session, subject) is False

    session.add(GroupMembership(group_id=group.id, principal_id=subject.id))
    session.commit()

    assert _has(session, subject) is True


def test_a_revoked_grant_does_not_count(session: Session) -> None:
    subject = _principal(session)
    grant = _grant(
        session, subject.id, principal_id=subject.id, kind=SecurableKind.INSTANCE, role=Role.ADMIN
    )
    grant.revoked_at = datetime.now(UTC)
    grant.revoked_by = subject.id
    session.commit()

    assert _has(session, subject) is False


def test_a_deactivated_person_has_none(session: Session) -> None:
    subject = _principal(session)
    _grant(
        session, subject.id, principal_id=subject.id, kind=SecurableKind.INSTANCE, role=Role.ADMIN
    )
    row = session.get(Principal, subject.id)
    assert row is not None
    row.is_active = False
    session.commit()

    assert _has(session, subject) is False


def test_is_admin_asks_the_instance_admin_rule(session: Session) -> None:
    subject = _principal(session)
    service = GrantService(session, AllowAllAuthorization())
    assert service.is_admin(subject) is False

    _grant(
        session, subject.id, principal_id=subject.id, kind=SecurableKind.INSTANCE, role=Role.ADMIN
    )

    assert service.is_admin(subject) is True


def test_is_admin_follows_the_credentials_scope(session: Session) -> None:
    """An administrator's personal token is admin; a task token of the same principal is not."""
    subject = _principal(session)
    _grant(
        session, subject.id, principal_id=subject.id, kind=SecurableKind.INSTANCE, role=Role.ADMIN
    )
    service = GrantService(session, GrantAuthorization(session))
    task = dataclasses.replace(
        subject, task_scope=TaskScope(environment="dev", asset_scope=[], token_id=uuid.uuid4())
    )

    assert (service.is_admin(subject), service.is_admin(task)) == (True, False)
