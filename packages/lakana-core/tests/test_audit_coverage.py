"""Audit coverage for identity, token, and policy mutations.

Each test drives one service method through `AllowAllAuthorization` with a
persisted actor, then asserts exactly one new `AuditEvent` of the expected
type was recorded, with the `detail` keys the brief specifies — and, for
PATs and task tokens, that the returned plaintext token never appears
anywhere in the recorded `detail`.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

from core_test_helpers import FakeColumnCatalog, ensure_table
from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.auth import AllowAllAuthorization, PrincipalRef, SecurableKind
from lakana_core.bootstrap import bootstrap_if_needed
from lakana_core.grants.roles import Role
from lakana_core.identifiers import NamespacePath
from lakana_core.identity.groups import GroupService
from lakana_core.identity.pats import PatService
from lakana_core.identity.principals import PrincipalService
from lakana_core.identity.task_tokens import TaskTokenService
from lakana_core.models import (
    AuditEvent,
    AuditEventType,
    Grant,
    Principal,
    PrincipalType,
    TaskToken,
)
from lakana_core.policy.column_masks import ColumnMaskService
from lakana_core.policy.row_filters import RowFilterService

_TWO_LEVEL_NAMESPACE = NamespacePath.from_parts(["gold", "sales"]).joined()
"""A two-level namespace, so policy-event tests exercise a real join, not a
single bare component that would pass even if `securable_namespace` were
built from `table.name` alone."""


def _add_principal(session: Session, name: str) -> Principal:
    """Insert a principal directly.

    Parameters
    ----------
    session
        Database session.
    name
        The principal's name.

    Returns
    -------
    Principal
        The inserted row.
    """
    row = Principal(id=uuid.uuid4(), type=PrincipalType.USER, name=name, is_active=True)
    session.add(row)
    session.commit()
    return row


def _make_instance_admin(session: Session, principal_id: uuid.UUID) -> None:
    """Give a persisted principal the `ADMIN` role at instance scope.

    Parameters
    ----------
    session
        Database session.
    principal_id
        The principal.
    """
    session.add(
        Grant(
            id=uuid.uuid4(),
            principal_id=principal_id,
            role=Role.ADMIN,
            kind=SecurableKind.INSTANCE,
            granted_by=principal_id,
            granted_at=datetime.now(UTC),
        )
    )
    session.commit()


def _events_of(session: Session, event_type: AuditEventType) -> list[AuditEvent]:
    """Fetch every recorded event of a given type.

    Parameters
    ----------
    session
        Database session.
    event_type
        The event type to filter on.

    Returns
    -------
    list of AuditEvent
        Matching rows.
    """
    return list(
        session.execute(select(AuditEvent).where(AuditEvent.event_type == event_type))
        .scalars()
        .all()
    )


def test_add_member_records_group_member_added(session: Session, principal: PrincipalRef) -> None:
    """`GroupService.add_member` records `GROUP_MEMBER_ADDED`."""
    groups = GroupService(session, AllowAllAuthorization())
    groups.create_group(principal, name="analysts")
    _add_principal(session, "alice")

    groups.add_member(principal, "analysts", "alice")

    events = _events_of(session, AuditEventType.GROUP_MEMBER_ADDED)
    assert len(events) == 1
    assert events[0].principal_id == principal.id
    assert events[0].detail == {"group": "analysts", "principal": "alice"}


def test_add_member_twice_records_only_one_event(session: Session, principal: PrincipalRef) -> None:
    """Adding an already-present member is a no-op per `GroupService`: no second event."""
    groups = GroupService(session, AllowAllAuthorization())
    groups.create_group(principal, name="analysts")
    _add_principal(session, "alice")
    groups.add_member(principal, "analysts", "alice")

    groups.add_member(principal, "analysts", "alice")

    assert len(_events_of(session, AuditEventType.GROUP_MEMBER_ADDED)) == 1


def test_remove_member_records_group_member_removed(
    session: Session, principal: PrincipalRef
) -> None:
    """`GroupService.remove_member` records `GROUP_MEMBER_REMOVED`."""
    groups = GroupService(session, AllowAllAuthorization())
    groups.create_group(principal, name="analysts")
    _add_principal(session, "alice")
    groups.add_member(principal, "analysts", "alice")

    groups.remove_member(principal, "analysts", "alice")

    events = _events_of(session, AuditEventType.GROUP_MEMBER_REMOVED)
    assert len(events) == 1
    assert events[0].principal_id == principal.id
    assert events[0].detail == {"group": "analysts", "principal": "alice"}


def test_issue_records_pat_issued_without_secret(session: Session, principal: PrincipalRef) -> None:
    """`PatService.issue` records `PAT_ISSUED` with no secret material."""
    _add_principal(session, "alice")
    pats = PatService(session, AllowAllAuthorization())

    issued = pats.issue(principal, "alice", name="laptop")

    events = _events_of(session, AuditEventType.PAT_ISSUED)
    assert len(events) == 1
    assert events[0].principal_id == principal.id
    assert events[0].detail == {
        "issuer": principal.name,
        "subject": "alice",
        "key_id": issued.key_id,
    }
    assert issued.token not in json.dumps(events[0].detail)


def test_revoke_records_pat_revoked_without_secret(
    session: Session, principal: PrincipalRef
) -> None:
    """`PatService.revoke` records `PAT_REVOKED` with no secret material."""
    _add_principal(session, "alice")
    pats = PatService(session, AllowAllAuthorization())
    issued = pats.issue(principal, "alice", name="laptop")

    pats.revoke(principal, "alice", issued.key_id)

    events = _events_of(session, AuditEventType.PAT_REVOKED)
    assert len(events) == 1
    assert events[0].principal_id == principal.id
    assert events[0].detail == {
        "actor": principal.name,
        "subject": "alice",
        "key_id": issued.key_id,
    }
    assert issued.token not in json.dumps(events[0].detail)


def test_revoke_pat_twice_is_idempotent(session: Session, principal: PrincipalRef) -> None:
    """Revoking an already-revoked PAT is a no-op: no second event, timestamp unchanged."""
    _add_principal(session, "alice")
    pats = PatService(session, AllowAllAuthorization())
    issued = pats.issue(principal, "alice", name="laptop")
    pats.revoke(principal, "alice", issued.key_id)
    [listed] = pats.list(principal, "alice")
    pat = listed.token
    first_revoked_at = pat.revoked_at

    pats.revoke(principal, "alice", issued.key_id)

    assert pat.revoked_at == first_revoked_at
    assert len(_events_of(session, AuditEventType.PAT_REVOKED)) == 1


def test_mint_records_task_token_minted_without_secret(
    session: Session, principal: PrincipalRef
) -> None:
    """`TaskTokenService.mint` records `TASK_TOKEN_MINTED` with no secret material."""
    _make_instance_admin(session, principal.id)
    _add_principal(session, "worker-1")
    task_tokens = TaskTokenService(session, AllowAllAuthorization())

    minted = task_tokens.mint(
        principal, "worker-1", environment="dev", asset_scope=["gold.sales"], ttl_seconds=300
    )

    events = _events_of(session, AuditEventType.TASK_TOKEN_MINTED)
    assert len(events) == 1
    assert events[0].principal_id == principal.id
    assert events[0].detail == {
        "issuer": principal.name,
        "subject": "worker-1",
        "key_id": minted.key_id,
        "environment": "dev",
        "asset_scope": ["gold.sales"],
    }
    assert minted.token not in json.dumps(events[0].detail)


def test_revoke_records_task_token_revoked_without_secret(
    session: Session, principal: PrincipalRef
) -> None:
    """`TaskTokenService.revoke` records `TASK_TOKEN_REVOKED` with no secret material."""
    _make_instance_admin(session, principal.id)
    _add_principal(session, "worker-1")
    task_tokens = TaskTokenService(session, AllowAllAuthorization())
    minted = task_tokens.mint(
        principal, "worker-1", environment="dev", asset_scope=["gold.sales"], ttl_seconds=300
    )

    task_tokens.revoke(principal, minted.key_id)

    events = _events_of(session, AuditEventType.TASK_TOKEN_REVOKED)
    assert len(events) == 1
    assert events[0].principal_id == principal.id
    assert events[0].detail == {
        "actor": principal.name,
        "subject": "worker-1",
        "key_id": minted.key_id,
        "environment": "dev",
        "asset_scope": ["gold.sales"],
    }
    assert minted.token not in json.dumps(events[0].detail)


def test_revoke_task_token_twice_is_idempotent(session: Session, principal: PrincipalRef) -> None:
    """Revoking an already-revoked task token is a no-op: no second event, timestamp unchanged."""
    _make_instance_admin(session, principal.id)
    _add_principal(session, "worker-1")
    task_tokens = TaskTokenService(session, AllowAllAuthorization())
    minted = task_tokens.mint(
        principal, "worker-1", environment="dev", asset_scope=["gold.sales"], ttl_seconds=300
    )
    task_tokens.revoke(principal, minted.key_id)
    row = session.execute(select(TaskToken).where(TaskToken.key_id == minted.key_id)).scalar_one()
    first_revoked_at = row.revoked_at

    task_tokens.revoke(principal, minted.key_id)

    assert row.revoked_at == first_revoked_at
    assert len(_events_of(session, AuditEventType.TASK_TOKEN_REVOKED)) == 1


def test_create_principal_records_principal_created(
    session: Session, principal: PrincipalRef
) -> None:
    """`PrincipalService.create_principal` records `PRINCIPAL_CREATED`."""
    principals = PrincipalService(session, AllowAllAuthorization())

    principals.create_principal(principal, name="alice", type=PrincipalType.USER)

    events = _events_of(session, AuditEventType.PRINCIPAL_CREATED)
    assert len(events) == 1
    assert events[0].principal_id == principal.id
    assert events[0].detail == {"principal": "alice", "type": "USER"}


def test_deactivate_principal_records_principal_deactivated(
    session: Session, principal: PrincipalRef
) -> None:
    """`PrincipalService.deactivate_principal` records `PRINCIPAL_DEACTIVATED`."""
    principals = PrincipalService(session, AllowAllAuthorization())
    principals.create_principal(principal, name="alice", type=PrincipalType.USER)

    principals.deactivate_principal(principal, "alice")

    events = _events_of(session, AuditEventType.PRINCIPAL_DEACTIVATED)
    assert len(events) == 1
    assert events[0].principal_id == principal.id
    assert events[0].detail == {"principal": "alice", "type": "USER"}


def test_deactivate_principal_twice_is_idempotent(
    session: Session, principal: PrincipalRef
) -> None:
    """Deactivating an already-inactive principal is a no-op: no second event."""
    principals = PrincipalService(session, AllowAllAuthorization())
    principals.create_principal(principal, name="alice", type=PrincipalType.USER)
    principals.deactivate_principal(principal, "alice")

    principals.deactivate_principal(principal, "alice")

    assert len(_events_of(session, AuditEventType.PRINCIPAL_DEACTIVATED)) == 1


def test_create_row_filter_records_row_filter_created(
    session: Session, principal: PrincipalRef
) -> None:
    """`RowFilterService.create` records `ROW_FILTER_CREATED` targeting the table."""
    _add_principal(session, "alice")
    ensure_table(session, "dev", _TWO_LEVEL_NAMESPACE, "customers")
    row_filters = RowFilterService(session, AllowAllAuthorization(), FakeColumnCatalog())

    row = row_filters.create(
        principal,
        principal="alice",
        environment="dev",
        namespace=_TWO_LEVEL_NAMESPACE,
        table="customers",
        expression="region = current_user()",
    )

    events = _events_of(session, AuditEventType.ROW_FILTER_CREATED)
    assert len(events) == 1
    assert events[0].principal_id == principal.id
    assert events[0].securable_kind == SecurableKind.TABLE
    assert events[0].securable_environment == "dev"
    assert events[0].securable_namespace == _TWO_LEVEL_NAMESPACE
    assert events[0].securable_table == "customers"
    assert events[0].detail == {"id": str(row.id), "subject": "alice"}


def test_revoke_row_filter_records_row_filter_revoked(
    session: Session, principal: PrincipalRef
) -> None:
    """`RowFilterService.revoke` records `ROW_FILTER_REVOKED` targeting the table."""
    _add_principal(session, "alice")
    ensure_table(session, "dev", _TWO_LEVEL_NAMESPACE, "customers")
    row_filters = RowFilterService(session, AllowAllAuthorization(), FakeColumnCatalog())
    row = row_filters.create(
        principal,
        principal="alice",
        environment="dev",
        namespace=_TWO_LEVEL_NAMESPACE,
        table="customers",
        expression="region = current_user()",
    )

    row_filters.revoke(principal, row.id)

    events = _events_of(session, AuditEventType.ROW_FILTER_REVOKED)
    assert len(events) == 1
    assert events[0].principal_id == principal.id
    assert events[0].securable_kind == SecurableKind.TABLE
    assert events[0].securable_environment == "dev"
    assert events[0].securable_namespace == _TWO_LEVEL_NAMESPACE
    assert events[0].securable_table == "customers"
    assert events[0].detail == {"id": str(row.id), "subject": "alice"}


def test_revoke_row_filter_twice_is_idempotent(session: Session, principal: PrincipalRef) -> None:
    """Revoking an already-revoked row filter is a no-op: no second event, timestamp unchanged."""
    _add_principal(session, "alice")
    ensure_table(session, "dev", _TWO_LEVEL_NAMESPACE, "customers")
    row_filters = RowFilterService(session, AllowAllAuthorization(), FakeColumnCatalog())
    row = row_filters.create(
        principal,
        principal="alice",
        environment="dev",
        namespace=_TWO_LEVEL_NAMESPACE,
        table="customers",
        expression="region = current_user()",
    )
    row_filters.revoke(principal, row.id)
    first_revoked_at = row.revoked_at

    row_filters.revoke(principal, row.id)

    assert row.revoked_at == first_revoked_at
    assert len(_events_of(session, AuditEventType.ROW_FILTER_REVOKED)) == 1


def test_create_column_mask_records_column_mask_created(
    session: Session, principal: PrincipalRef
) -> None:
    """`ColumnMaskService.create` records `COLUMN_MASK_CREATED` targeting the table."""
    _add_principal(session, "alice")
    ensure_table(session, "dev", _TWO_LEVEL_NAMESPACE, "customers")
    column_masks = ColumnMaskService(session, AllowAllAuthorization(), FakeColumnCatalog())

    row = column_masks.create(
        principal,
        principal="alice",
        environment="dev",
        namespace=_TWO_LEVEL_NAMESPACE,
        table="customers",
        column="ssn",
        expression="'***'",
    )

    events = _events_of(session, AuditEventType.COLUMN_MASK_CREATED)
    assert len(events) == 1
    assert events[0].principal_id == principal.id
    assert events[0].securable_kind == SecurableKind.TABLE
    assert events[0].securable_environment == "dev"
    assert events[0].securable_namespace == _TWO_LEVEL_NAMESPACE
    assert events[0].securable_table == "customers"
    assert events[0].detail == {"id": str(row.id), "subject": "alice"}


def test_revoke_column_mask_records_column_mask_revoked(
    session: Session, principal: PrincipalRef
) -> None:
    """`ColumnMaskService.revoke` records `COLUMN_MASK_REVOKED` targeting the table."""
    _add_principal(session, "alice")
    ensure_table(session, "dev", _TWO_LEVEL_NAMESPACE, "customers")
    column_masks = ColumnMaskService(session, AllowAllAuthorization(), FakeColumnCatalog())
    row = column_masks.create(
        principal,
        principal="alice",
        environment="dev",
        namespace=_TWO_LEVEL_NAMESPACE,
        table="customers",
        column="ssn",
        expression="'***'",
    )

    column_masks.revoke(principal, row.id)

    events = _events_of(session, AuditEventType.COLUMN_MASK_REVOKED)
    assert len(events) == 1
    assert events[0].principal_id == principal.id
    assert events[0].securable_kind == SecurableKind.TABLE
    assert events[0].securable_environment == "dev"
    assert events[0].securable_namespace == _TWO_LEVEL_NAMESPACE
    assert events[0].securable_table == "customers"
    assert events[0].detail == {"id": str(row.id), "subject": "alice"}


def test_revoke_column_mask_twice_is_idempotent(session: Session, principal: PrincipalRef) -> None:
    """Revoking an already-revoked column mask is a no-op: no second event, timestamp unchanged."""
    _add_principal(session, "alice")
    ensure_table(session, "dev", _TWO_LEVEL_NAMESPACE, "customers")
    column_masks = ColumnMaskService(session, AllowAllAuthorization(), FakeColumnCatalog())
    row = column_masks.create(
        principal,
        principal="alice",
        environment="dev",
        namespace=_TWO_LEVEL_NAMESPACE,
        table="customers",
        column="ssn",
        expression="'***'",
    )
    column_masks.revoke(principal, row.id)
    first_revoked_at = row.revoked_at

    column_masks.revoke(principal, row.id)

    assert row.revoked_at == first_revoked_at
    assert len(_events_of(session, AuditEventType.COLUMN_MASK_REVOKED)) == 1


def test_bootstrap_recovery_still_records_pat_issued(session: Session) -> None:
    """Bootstrap recovery's PAT mint goes through `PatService.issue`, so it is audited too."""
    token = bootstrap_if_needed(session, "admin", key_id="testkey", secret="testsecret")

    assert token == "lakana_pat_testkey_testsecret"
    events = _events_of(session, AuditEventType.PAT_ISSUED)
    assert len(events) == 1
    assert events[0].detail == {"issuer": "admin", "subject": "admin", "key_id": "testkey"}
    assert token not in json.dumps(events[0].detail)
