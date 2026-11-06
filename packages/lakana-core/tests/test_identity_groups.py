"""Tests for GroupService."""

import uuid

import pytest
from core_test_helpers import FakeColumnCatalog, ensure_table
from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.auth import AllowAllAuthorization, PrincipalRef, SecurableKind
from lakana_core.errors import AlreadyExistsError, ConflictError, NotFoundError
from lakana_core.grants.roles import Role
from lakana_core.grants.service import GrantService
from lakana_core.identity.groups import GroupService
from lakana_core.models import (
    AuditEvent,
    AuditEventType,
    Principal,
    PrincipalType,
)
from lakana_core.policy.column_masks import ColumnMaskService
from lakana_core.policy.row_filters import RowFilterService


def _admin() -> PrincipalRef:
    """Build an acting principal for tests that don't care who it is.

    Returns
    -------
    PrincipalRef
        A user principal, independent of any fixture instance.
    """
    return PrincipalRef(id=uuid.uuid4(), name="admin", type=PrincipalType.USER)


def _add_principal(session: Session, name: str) -> Principal:
    """Insert a principal directly, bypassing PrincipalService (out of scope here).

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
    principal = Principal(id=uuid.uuid4(), type=PrincipalType.USER, name=name)
    session.add(principal)
    session.commit()
    return principal


def _persisted_admin(session: Session) -> PrincipalRef:
    """Build a PrincipalRef for a real, persisted principal, for GrantService calls.

    `Grant.granted_by` is a non-nullable foreign key onto `principals.id` —
    unlike `GroupService`'s own acting principal, which is never required
    to exist as a row, a call through `GrantService.grant` needs its actor
    persisted first, or the insert fails its foreign key.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    PrincipalRef
        A reference to a freshly inserted principal.
    """
    row = _add_principal(session, "admin")
    return PrincipalRef(id=row.id, name=row.name, type=row.type)


def test_create_group_persists_and_returns_it(session: Session) -> None:
    """A created group is reloadable by name."""
    admin = _persisted_admin(session)
    service = GroupService(session, AllowAllAuthorization())
    service.create_group(admin, name="engineers")
    assert {g.name for g in service.list_groups(admin).items} == {"engineers"}


def test_create_group_rejects_a_duplicate_name(session: Session) -> None:
    """The unique name constraint is enforced at the service layer."""
    admin = _persisted_admin(session)
    service = GroupService(session, AllowAllAuthorization())
    service.create_group(admin, name="engineers")
    with pytest.raises(AlreadyExistsError):
        service.create_group(admin, name="engineers")


def test_delete_group_removes_it(session: Session) -> None:
    """A deleted group no longer appears in the list."""
    admin = _persisted_admin(session)
    service = GroupService(session, AllowAllAuthorization())
    service.create_group(admin, name="engineers")
    service.delete_group(admin, "engineers")
    assert service.list_groups(admin).items == []


def test_delete_group_raises_when_missing(session: Session) -> None:
    """Deleting a name nobody created is a NotFoundError."""
    service = GroupService(session, AllowAllAuthorization())
    with pytest.raises(NotFoundError):
        service.delete_group(_admin(), "nobody-home")


def test_add_member_then_list_members(session: Session) -> None:
    """A principal added to a group appears in its member list."""
    admin = _persisted_admin(session)
    _add_principal(session, "alice")
    service = GroupService(session, AllowAllAuthorization())
    service.create_group(admin, name="engineers")

    service.add_member(admin, "engineers", "alice")

    assert [m.name for m in service.list_members(admin, "engineers")] == ["alice"]


def test_list_members_is_ordered_by_name(session: Session) -> None:
    """Members come back sorted by name, whatever order they were added in."""
    admin = _persisted_admin(session)
    for name in ("carol", "alice", "bob"):
        _add_principal(session, name)
    service = GroupService(session, AllowAllAuthorization())
    service.create_group(admin, name="engineers")
    for name in ("carol", "alice", "bob"):
        service.add_member(admin, "engineers", name)

    assert [m.name for m in service.list_members(admin, "engineers")] == ["alice", "bob", "carol"]


def test_add_member_is_idempotent(session: Session) -> None:
    """Adding an existing member twice does not raise or duplicate the row."""
    admin = _persisted_admin(session)
    _add_principal(session, "alice")
    service = GroupService(session, AllowAllAuthorization())
    service.create_group(admin, name="engineers")

    service.add_member(admin, "engineers", "alice")
    service.add_member(admin, "engineers", "alice")

    assert len(service.list_members(admin, "engineers")) == 1


def test_add_member_raises_for_an_unknown_group(session: Session) -> None:
    """Adding to a group that doesn't exist is a NotFoundError, not a silent no-op."""
    _add_principal(session, "alice")
    service = GroupService(session, AllowAllAuthorization())
    with pytest.raises(NotFoundError):
        service.add_member(_admin(), "nonexistent", "alice")


def test_add_member_raises_for_an_unknown_principal(session: Session) -> None:
    """Adding an unknown principal is a NotFoundError, not a silent no-op."""
    admin = _persisted_admin(session)
    service = GroupService(session, AllowAllAuthorization())
    service.create_group(admin, name="engineers")
    with pytest.raises(NotFoundError):
        service.add_member(admin, "engineers", "nobody")


def test_remove_member_raises_when_not_a_member(session: Session) -> None:
    """Removing a non-member is a NotFoundError, matching the router's DELETE semantics."""
    admin = _persisted_admin(session)
    _add_principal(session, "alice")
    service = GroupService(session, AllowAllAuthorization())
    service.create_group(admin, name="engineers")
    with pytest.raises(NotFoundError):
        service.remove_member(admin, "engineers", "alice")


def test_remove_member_removes_the_membership(session: Session) -> None:
    """A removed member no longer appears in the member list."""
    admin = _persisted_admin(session)
    _add_principal(session, "alice")
    service = GroupService(session, AllowAllAuthorization())
    service.create_group(admin, name="engineers")
    service.add_member(admin, "engineers", "alice")

    service.remove_member(admin, "engineers", "alice")

    assert service.list_members(admin, "engineers") == []


def test_delete_group_holding_the_last_admin_grant_is_refused(session: Session) -> None:
    """Deleting a group cascades away its grants; refused if it's the last admin source."""
    admin = _persisted_admin(session)
    _add_principal(session, "root")
    groups = GroupService(session, AllowAllAuthorization())
    grants = GrantService(session, AllowAllAuthorization())
    groups.create_group(admin, name="ops")
    groups.add_member(admin, "ops", "root")
    grants.grant(admin, group="ops", role=Role.ADMIN, kind=SecurableKind.INSTANCE)

    with pytest.raises(ConflictError):
        groups.delete_group(admin, "ops")


def test_delete_group_with_another_admin_elsewhere_is_allowed_once_its_grant_is_revoked(
    session: Session,
) -> None:
    """Deleting a formerly admin-holding group succeeds once its own grant is revoked.

    Before the fix, deleting a group cascaded away its
    grants silently (re-run finding); now `delete_group` refuses while
    *any* active grant references the group, admin or not, so the caller
    must revoke it first. This is still allowed once that grant is gone,
    with another active admin (`root2`) making no difference to the
    outcome — the refusal is about the group's own active references, not
    instance-wide admin headroom (covered separately, see the "last admin
    grant" test below).
    """
    admin = _persisted_admin(session)
    _add_principal(session, "root")
    _add_principal(session, "root2")
    groups = GroupService(session, AllowAllAuthorization())
    grants = GrantService(session, AllowAllAuthorization())
    groups.create_group(admin, name="ops")
    groups.add_member(admin, "ops", "root")
    grant = grants.grant(admin, group="ops", role=Role.ADMIN, kind=SecurableKind.INSTANCE)
    grants.grant(admin, principal="root2", role=Role.ADMIN, kind=SecurableKind.INSTANCE)

    grants.revoke(admin, grant.id)
    groups.delete_group(admin, "ops")  # does not raise


def test_remove_member_who_is_the_last_active_admin_is_refused(session: Session) -> None:
    """Removing the sole active admin from their only admin-granting group is refused."""
    admin = _persisted_admin(session)
    _add_principal(session, "root")
    groups = GroupService(session, AllowAllAuthorization())
    grants = GrantService(session, AllowAllAuthorization())
    groups.create_group(admin, name="ops")
    groups.add_member(admin, "ops", "root")
    grants.grant(admin, group="ops", role=Role.ADMIN, kind=SecurableKind.INSTANCE)

    with pytest.raises(ConflictError):
        groups.remove_member(admin, "ops", "root")


def test_the_last_admin_may_leave_a_group_without_the_admin_role(session: Session) -> None:
    """A group that grants no instance ADMIN cannot take the role away: the guard is not asked."""
    admin = _persisted_admin(session)
    _add_principal(session, "root")
    groups = GroupService(session, AllowAllAuthorization())
    grants = GrantService(session, AllowAllAuthorization())
    grants.grant(admin, principal="root", role=Role.ADMIN, kind=SecurableKind.INSTANCE)
    ensure_table(session, "prod", "gold", "orders")
    groups.create_group(admin, name="engineers")
    groups.add_member(admin, "engineers", "root")
    grants.grant(
        admin,
        group="engineers",
        role=Role.ADMIN,
        kind=SecurableKind.ENVIRONMENT,
        environment="prod",
    )

    groups.remove_member(admin, "engineers", "root")  # does not raise


def test_remove_member_who_is_not_an_admin_is_unaffected_by_the_lockout_guard(
    session: Session,
) -> None:
    """Removing a non-admin member from a group is unaffected by the lockout guard."""
    admin = _persisted_admin(session)
    _add_principal(session, "alice")
    groups = GroupService(session, AllowAllAuthorization())
    groups.create_group(admin, name="engineers")
    groups.add_member(admin, "engineers", "alice")

    groups.remove_member(admin, "engineers", "alice")  # does not raise


def test_create_group_records_group_created(session: Session) -> None:
    """`create_group` records a `GROUP_CREATED` event."""
    admin = _persisted_admin(session)
    service = GroupService(session, AllowAllAuthorization())

    service.create_group(admin, name="engineers")

    events = (
        session.execute(
            select(AuditEvent).where(AuditEvent.event_type == AuditEventType.GROUP_CREATED)
        )
        .scalars()
        .all()
    )
    assert len(events) == 1
    assert events[0].principal_id == admin.id
    assert events[0].detail == {"group": "engineers"}


def test_delete_group_is_refused_while_an_active_grant_references_it(session: Session) -> None:
    """A group with an active (non-admin) grant cannot be deleted, and nothing is removed.

    Reproduces the finding directly: before this fix,
    `delete_group` hard-deleted the group and let `ON DELETE CASCADE` FKs
    silently remove every grant, row filter, and column mask that targeted
    it — a member holding a *direct* grant on the same table would then
    read it unfiltered and unmasked. Only `Role.VIEWER` is granted here (not
    `ADMIN`), to prove the refusal is not just the pre-existing admin
    headroom guard.
    """
    admin = _persisted_admin(session)
    _add_principal(session, "alice")
    groups = GroupService(session, AllowAllAuthorization())
    grants = GrantService(session, AllowAllAuthorization())
    groups.create_group(admin, name="analysts")
    groups.add_member(admin, "analysts", "alice")
    grants.grant(admin, group="analysts", role=Role.VIEWER, kind=SecurableKind.INSTANCE)

    with pytest.raises(ConflictError, match="1 active grant"):
        groups.delete_group(admin, "analysts")

    assert {g.name for g in groups.list_groups(admin).items} == {"analysts"}


def test_delete_group_is_refused_while_an_active_row_filter_references_it(
    session: Session,
) -> None:
    """A group with an active row filter cannot be deleted."""
    admin = _persisted_admin(session)
    groups = GroupService(session, AllowAllAuthorization())
    groups.create_group(admin, name="analysts")
    ensure_table(session, "dev", "gold", "customers")
    RowFilterService(session, AllowAllAuthorization(), FakeColumnCatalog()).create(
        admin,
        group="analysts",
        environment="dev",
        namespace="gold",
        table="customers",
        expression="region = current_user()",
    )

    with pytest.raises(ConflictError, match="1 active row filter"):
        groups.delete_group(admin, "analysts")


def test_delete_group_is_refused_while_an_active_column_mask_references_it(
    session: Session,
) -> None:
    """A group with an active column mask cannot be deleted."""
    admin = _persisted_admin(session)
    groups = GroupService(session, AllowAllAuthorization())
    groups.create_group(admin, name="analysts")
    ensure_table(session, "dev", "gold", "customers")
    ColumnMaskService(session, AllowAllAuthorization(), FakeColumnCatalog()).create(
        admin,
        group="analysts",
        environment="dev",
        namespace="gold",
        table="customers",
        column="ssn",
        expression="'<redacted>'",
    )

    with pytest.raises(ConflictError, match="1 active column mask"):
        groups.delete_group(admin, "analysts")


def test_delete_group_succeeds_after_revoking_its_grant_and_records_cascaded_events(
    session: Session,
) -> None:
    """Once its only active reference is revoked, deleting a group audits the cascade.

    Records `GROUP_MEMBER_REMOVED` for each membership the delete cascades
    away, then a `GROUP_DELETED` event, all before the group row itself is
    gone.
    """
    admin = _persisted_admin(session)
    _add_principal(session, "alice")
    groups = GroupService(session, AllowAllAuthorization())
    grants = GrantService(session, AllowAllAuthorization())
    groups.create_group(admin, name="analysts")
    groups.add_member(admin, "analysts", "alice")
    grant = grants.grant(admin, group="analysts", role=Role.VIEWER, kind=SecurableKind.INSTANCE)
    grants.revoke(admin, grant.id)

    groups.delete_group(admin, "analysts")

    member_removed = (
        session.execute(
            select(AuditEvent).where(AuditEvent.event_type == AuditEventType.GROUP_MEMBER_REMOVED)
        )
        .scalars()
        .all()
    )
    assert len(member_removed) == 1
    assert member_removed[0].detail == {"group": "analysts", "principal": "alice"}
    deleted = (
        session.execute(
            select(AuditEvent).where(AuditEvent.event_type == AuditEventType.GROUP_DELETED)
        )
        .scalars()
        .all()
    )
    assert len(deleted) == 1
    assert deleted[0].detail == {"group": "analysts"}
    assert deleted[0].principal_id == admin.id
