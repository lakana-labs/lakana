"""Tests for GrantService: CRUD and the admin-lockout guard."""

import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from core_test_helpers import _catalog_with
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from lakana_core.audit.authorization import AuditingAuthorization
from lakana_core.audit.denial_limiter import DenialAuditLimiter
from lakana_core.auth import (
    Action,
    AllowAllAuthorization,
    PrincipalRef,
    Securable,
    SecurableKind,
    TaskScope,
)
from lakana_core.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.grants.roles import Role
from lakana_core.grants.service import LAST_ADMIN_MESSAGE, GrantService, assert_admin_headroom
from lakana_core.identifiers import NamespacePath
from lakana_core.identity.groups import GroupService
from lakana_core.identity.principals import PrincipalService
from lakana_core.models import Grant, Group, GroupMembership, Principal, PrincipalType


def _admin() -> PrincipalRef:
    """Build the acting principal used to drive every service call in these tests.

    Returns
    -------
    PrincipalRef
        A user principal, independent of any fixture instance.
    """
    return PrincipalRef(id=uuid.uuid4(), name="admin", type=PrincipalType.USER)


def _add_principal(principals: PrincipalService, p: PrincipalRef, name: str) -> None:
    """Create a principal through the service, under allow-all authorization."""
    principals.create_principal(p, name=name, type=PrincipalType.SERVICE_ACCOUNT)


def _persist_actor(session: Session, p: PrincipalRef) -> None:
    """Insert a Principal row backing an acting `PrincipalRef`.

    `Grant.granted_by`/`revoked_by` are non-nullable foreign keys onto
    `principals.id` — unlike every other service in this codebase, whose
    acting principal is never required to exist as a row. Any test that
    calls `GrantService.grant`/`revoke` needs its actor persisted first, or
    the insert/update fails its foreign key.

    Parameters
    ----------
    session
        Database session.
    p
        The acting principal to back with a real row, same id.
    """
    session.add(Principal(id=p.id, type=p.type, name=p.name, is_active=True))
    session.commit()


def test_grant_requires_exactly_one_subject(session: Session) -> None:
    """Passing both principal and group, or neither, is a ValidationError."""
    grants = GrantService(session, AllowAllAuthorization())
    admin = _admin()
    with pytest.raises(ValidationError):
        grants.grant(admin, role=Role.VIEWER, kind=SecurableKind.INSTANCE)
    with pytest.raises(ValidationError):
        grants.grant(admin, principal="a", group="b", role=Role.VIEWER, kind=SecurableKind.INSTANCE)


def test_grant_requires_exactly_one_of_role_or_action(session: Session) -> None:
    """Passing both role and action, or neither, is a ValidationError."""
    grants = GrantService(session, AllowAllAuthorization())
    principals = PrincipalService(session, AllowAllAuthorization())
    admin = _admin()
    _persist_actor(session, admin)
    _add_principal(principals, admin, "alice")
    with pytest.raises(ValidationError):
        grants.grant(admin, principal="alice", kind=SecurableKind.INSTANCE)
    with pytest.raises(ValidationError):
        grants.grant(
            admin,
            principal="alice",
            role=Role.VIEWER,
            action=Action.READ_TABLE,
            kind=SecurableKind.INSTANCE,
        )


def test_grant_raises_not_found_for_an_unknown_principal(session: Session) -> None:
    """Granting to a principal name that doesn't exist is a NotFoundError."""
    grants = GrantService(session, AllowAllAuthorization())
    with pytest.raises(NotFoundError):
        grants.grant(_admin(), principal="nobody", role=Role.VIEWER, kind=SecurableKind.INSTANCE)


def test_grant_then_list_for_principal(session: Session, tmp_path: Path) -> None:
    """A granted role shows up when listing that principal's grants."""
    _catalog_with(session, tmp_path, AllowAllAuthorization())
    grants = GrantService(session, AllowAllAuthorization())
    principals = PrincipalService(session, AllowAllAuthorization())
    admin = _admin()
    _persist_actor(session, admin)
    _add_principal(principals, admin, "alice")

    created = grants.grant(
        admin,
        principal="alice",
        role=Role.ANALYST,
        kind=SecurableKind.ENVIRONMENT,
        environment="dev",
    )
    assert created.role == Role.ANALYST
    assert created.environment == "dev"

    listed = grants.list_grants(admin, principal_name="alice").items
    assert [g.id for g in listed] == [created.id]


def test_grant_then_list_for_group(session: Session) -> None:
    """A granted action shows up when listing that group's grants."""
    grants = GrantService(session, AllowAllAuthorization())
    groups = GroupService(session, AllowAllAuthorization())
    admin = _admin()
    _persist_actor(session, admin)
    groups.create_group(admin, name="engineers")

    created = grants.grant(
        admin, group="engineers", action=Action.READ_TABLE, kind=SecurableKind.INSTANCE
    )
    listed = grants.list_grants(admin, group_name="engineers").items
    assert [g.id for g in listed] == [created.id]


def test_a_scope_filter_is_an_exact_match_not_ancestor_expanded(
    session: Session, tmp_path: Path
) -> None:
    """A grant on the environment does not show up when listing a specific table."""
    _catalog_with(session, tmp_path, AllowAllAuthorization())
    grants = GrantService(session, AllowAllAuthorization())
    principals = PrincipalService(session, AllowAllAuthorization())
    admin = _admin()
    _persist_actor(session, admin)
    _add_principal(principals, admin, "alice")
    grants.grant(
        admin,
        principal="alice",
        role=Role.ANALYST,
        kind=SecurableKind.ENVIRONMENT,
        environment="dev",
    )

    bronze = NamespacePath.from_parts(["bronze"])
    at_table = grants.list_grants(admin, scope=Securable.table_scope("dev", bronze, "orders"))
    assert at_table.items == []

    at_environment = grants.list_grants(admin, scope=Securable.environment_scope("dev"))
    assert len(at_environment.items) == 1


def test_list_grants_filters_by_kind_alone(session: Session) -> None:
    """`kind` without an object lists every grant on that level, the instance among them."""
    grants = GrantService(session, AllowAllAuthorization())
    principals = PrincipalService(session, AllowAllAuthorization())
    admin = _admin()
    _persist_actor(session, admin)
    _add_principal(principals, admin, "alice")
    on_instance = grants.grant(
        admin, principal="alice", role=Role.VIEWER, kind=SecurableKind.INSTANCE
    )
    listed = grants.list_grants(admin, kind=SecurableKind.INSTANCE).items
    assert [row.id for row in listed] == [on_instance.id]
    assert grants.list_grants(admin, kind=SecurableKind.TABLE).items == []


def test_list_grants_refuses_a_kind_other_than_the_objects(session: Session) -> None:
    """A kind and an object of another level could match nothing: the refusal says so."""
    grants = GrantService(session, AllowAllAuthorization())
    admin = _admin()
    _persist_actor(session, admin)
    with pytest.raises(ValidationError, match=r"instance.*environment"):
        grants.list_grants(
            admin, kind=SecurableKind.INSTANCE, scope=Securable.environment_scope("dev")
        )
    assert (
        grants.list_grants(
            admin, kind=SecurableKind.ENVIRONMENT, scope=Securable.environment_scope("dev")
        ).items
        == []
    )


def test_revoke_sets_revoked_at_and_revoked_by(session: Session) -> None:
    """Revoking a grant sets its revoked_at/revoked_by, not deleting the row."""
    grants = GrantService(session, AllowAllAuthorization())
    principals = PrincipalService(session, AllowAllAuthorization())
    admin = _admin()
    _persist_actor(session, admin)
    _add_principal(principals, admin, "alice")
    created = grants.grant(admin, principal="alice", role=Role.VIEWER, kind=SecurableKind.INSTANCE)

    grants.revoke(admin, created.id)

    row = session.get(Grant, created.id)
    assert row is not None
    assert row.revoked_at is not None
    assert row.revoked_by == admin.id
    assert grants.list_grants(admin, principal_name="alice").items == []


def test_revoke_raises_not_found_for_an_unknown_grant(session: Session) -> None:
    """Revoking a grant id that doesn't exist is a NotFoundError."""
    grants = GrantService(session, AllowAllAuthorization())
    with pytest.raises(NotFoundError):
        grants.revoke(_admin(), uuid.uuid4())


def test_revoke_grant_twice_is_idempotent(session: Session) -> None:
    """Revoking an already-revoked grant is a no-op.

    Matches PATs, task tokens, row filters, and column masks: no second
    `GRANT_REVOKED` event, and the original `revoked_at`/`revoked_by`
    untouched.
    """
    grants = GrantService(session, AllowAllAuthorization())
    principals = PrincipalService(session, AllowAllAuthorization())
    admin = _admin()
    _persist_actor(session, admin)
    _add_principal(principals, admin, "alice")
    created = grants.grant(admin, principal="alice", role=Role.VIEWER, kind=SecurableKind.INSTANCE)
    grants.revoke(admin, created.id)
    row = session.get(Grant, created.id)
    assert row is not None
    first_revoked_at = row.revoked_at
    first_revoked_by = row.revoked_by

    grants.revoke(admin, created.id)

    session.refresh(row)
    assert row.revoked_at == first_revoked_at
    assert row.revoked_by == first_revoked_by
    from lakana_core.models import AuditEvent, AuditEventType

    events = session.query(AuditEvent).filter_by(event_type=AuditEventType.GRANT_REVOKED).all()
    assert len(events) == 1


def test_revoking_the_last_admin_grant_is_refused(session: Session) -> None:
    """Revoking the sole instance-scoped admin grant is a ConflictError."""
    grants = GrantService(session, AllowAllAuthorization())
    principals = PrincipalService(session, AllowAllAuthorization())
    admin = _admin()
    _persist_actor(session, admin)
    _add_principal(principals, admin, "root")
    created = grants.grant(admin, principal="root", role=Role.ADMIN, kind=SecurableKind.INSTANCE)

    with pytest.raises(ConflictError):
        grants.revoke(admin, created.id)


def test_revoking_one_of_two_admin_grants_is_allowed(session: Session) -> None:
    """A second active admin grant means revoking the first is not a lockout."""
    grants = GrantService(session, AllowAllAuthorization())
    principals = PrincipalService(session, AllowAllAuthorization())
    admin = _admin()
    _persist_actor(session, admin)
    _add_principal(principals, admin, "root1")
    _add_principal(principals, admin, "root2")
    first = grants.grant(admin, principal="root1", role=Role.ADMIN, kind=SecurableKind.INSTANCE)
    grants.grant(admin, principal="root2", role=Role.ADMIN, kind=SecurableKind.INSTANCE)

    grants.revoke(admin, first.id)  # does not raise

    row = session.get(Grant, first.id)
    assert row is not None
    assert row.revoked_at is not None


def test_assert_admin_headroom_follows_group_membership(session: Session) -> None:
    """An admin grant on a group counts every active member as headroom."""
    grants = GrantService(session, AllowAllAuthorization())
    principals = PrincipalService(session, AllowAllAuthorization())
    groups = GroupService(session, AllowAllAuthorization())
    admin = _admin()
    _persist_actor(session, admin)
    _add_principal(principals, admin, "root")
    groups.create_group(admin, name="admins")
    groups.add_member(admin, "admins", "root")
    grants.grant(admin, group="admins", role=Role.ADMIN, kind=SecurableKind.INSTANCE)

    assert_admin_headroom(session)  # does not raise: root is an active admin via the group


def test_grant_raises_validation_error_for_mismatched_securable_fields(
    session: Session,
) -> None:
    """INSTANCE grant with environment is ValidationError, not silently accepted."""
    grants = GrantService(session, AllowAllAuthorization())
    principals = PrincipalService(session, AllowAllAuthorization())
    admin = _admin()
    _persist_actor(session, admin)
    _add_principal(principals, admin, "alice")
    with pytest.raises(ValidationError):
        grants.grant(
            admin,
            principal="alice",
            role=Role.VIEWER,
            kind=SecurableKind.INSTANCE,
            environment="dev",
        )


def test_grant_raises_not_found_for_an_unknown_group(session: Session) -> None:
    """Granting to a group name that doesn't exist is a NotFoundError."""
    grants = GrantService(session, AllowAllAuthorization())
    with pytest.raises(NotFoundError):
        grants.grant(_admin(), group="nonexistent", role=Role.VIEWER, kind=SecurableKind.INSTANCE)


def test_grant_on_missing_environment_is_not_found(session: Session) -> None:
    """Granting on an environment that doesn't exist is a NotFoundError."""
    grants = GrantService(session, AllowAllAuthorization())
    principals = PrincipalService(session, AllowAllAuthorization())
    admin = _admin()
    _persist_actor(session, admin)
    _add_principal(principals, admin, "alice")
    with pytest.raises(NotFoundError):
        grants.grant(
            admin,
            principal="alice",
            role=Role.ANALYST,
            kind=SecurableKind.ENVIRONMENT,
            environment="nonexistent",
        )


def test_grant_on_missing_namespace_is_not_found(session: Session, tmp_path: Path) -> None:
    """Granting a NAMESPACE-kind grant on a namespace that doesn't exist is a NotFoundError.

    The environment itself exists (`dev`, with its auto-scaffolded
    `bronze`/`silver`/`gold` namespaces), so this pins the namespace-specific
    check, distinct from `test_grant_on_missing_environment_is_not_found`.
    Without it, a NAMESPACE-kind grant on `gold.sales` created before
    `gold.sales` itself exists would never be revoked when a later
    `gold.sales` is dropped (`revoke_attached` only ever sees rows that were
    actually bound), so a namespace created after the fact would silently
    inherit it.
    """
    _catalog_with(session, tmp_path, AllowAllAuthorization())
    grants = GrantService(session, AllowAllAuthorization())
    principals = PrincipalService(session, AllowAllAuthorization())
    admin = _admin()
    _persist_actor(session, admin)
    _add_principal(principals, admin, "alice")
    with pytest.raises(NotFoundError):
        grants.grant(
            admin,
            principal="alice",
            role=Role.VIEWER,
            kind=SecurableKind.NAMESPACE,
            environment="dev",
            namespace=NamespacePath.from_parts(["gold", "sales"]).joined(),
        )


def test_grant_on_missing_table_is_not_found(session: Session, tmp_path: Path) -> None:
    """Granting a TABLE-kind grant on a table that doesn't exist is a NotFoundError.

    The environment itself exists (`dev`) so this pins the table-specific
    check, distinct from `test_grant_on_missing_environment_is_not_found`.
    """
    _catalog_with(session, tmp_path, AllowAllAuthorization())
    grants = GrantService(session, AllowAllAuthorization())
    principals = PrincipalService(session, AllowAllAuthorization())
    admin = _admin()
    _persist_actor(session, admin)
    _add_principal(principals, admin, "alice")
    with pytest.raises(NotFoundError):
        grants.grant(
            admin,
            principal="alice",
            action=Action.READ_TABLE,
            kind=SecurableKind.TABLE,
            environment="dev",
            namespace=NamespacePath.from_parts(["bronze"]).joined(),
            table="orders",
        )


def test_grant_records_a_grant_created_audit_event(session: Session, tmp_path: Path) -> None:
    """Creating a grant writes a matching AuditEvent in the same commit."""
    from lakana_core.models import AuditEvent, AuditEventType

    _catalog_with(session, tmp_path, AllowAllAuthorization())
    principals = PrincipalService(session, AllowAllAuthorization())
    admin = PrincipalRef(id=uuid.uuid4(), name="admin", type=PrincipalType.USER)
    _persist_actor(session, admin)
    grants = GrantService(session, AllowAllAuthorization())
    _add_principal(principals, admin, "alice")

    created = grants.grant(
        admin,
        principal="alice",
        role=Role.ANALYST,
        kind=SecurableKind.ENVIRONMENT,
        environment="dev",
    )

    events = session.query(AuditEvent).filter_by(event_type=AuditEventType.GRANT_CREATED).all()
    assert len(events) == 1
    assert events[0].principal_id == admin.id
    assert events[0].securable_environment == "dev"
    assert events[0].detail["grant_id"] == str(created.id)
    assert events[0].detail["role"] == "ANALYST"


def test_revoke_records_a_grant_revoked_audit_event(session: Session, tmp_path: Path) -> None:
    """Revoking a grant writes a matching AuditEvent in the same commit."""
    from lakana_core.models import AuditEvent, AuditEventType

    _catalog_with(session, tmp_path, AllowAllAuthorization())
    principals = PrincipalService(session, AllowAllAuthorization())
    admin = PrincipalRef(id=uuid.uuid4(), name="admin", type=PrincipalType.USER)
    _persist_actor(session, admin)
    grants = GrantService(session, AllowAllAuthorization())
    _add_principal(principals, admin, "alice")
    created = grants.grant(
        admin,
        principal="alice",
        role=Role.ANALYST,
        kind=SecurableKind.ENVIRONMENT,
        environment="dev",
    )

    grants.revoke(admin, created.id)

    events = session.query(AuditEvent).filter_by(event_type=AuditEventType.GRANT_REVOKED).all()
    assert len(events) == 1
    assert events[0].detail["grant_id"] == str(created.id)


def test_revoke_records_the_namespace_and_subject_role_in_its_audit_event(
    session: Session, tmp_path: Path
) -> None:
    """Revoking a NAMESPACE-scoped grant populates securable_namespace and full detail.

    Regression test: `revoke`'s audit call used to hardcode `namespace=None`
    regardless of the revoked grant's own scope, so a NAMESPACE/TABLE-scoped
    revocation produced a `GRANT_REVOKED` row claiming a namespaced
    `securable_kind` while `securable_namespace` stayed NULL. `detail` also
    used to carry only `grant_id`, dropping the subject and role/action the
    audit design requires.
    """
    from lakana_core.models import AuditEvent, AuditEventType

    _catalog_with(session, tmp_path, AllowAllAuthorization())
    principals = PrincipalService(session, AllowAllAuthorization())
    admin = PrincipalRef(id=uuid.uuid4(), name="admin", type=PrincipalType.USER)
    _persist_actor(session, admin)
    grants = GrantService(session, AllowAllAuthorization())
    _add_principal(principals, admin, "alice")
    created = grants.grant(
        admin,
        principal="alice",
        role=Role.VIEWER,
        kind=SecurableKind.NAMESPACE,
        environment="dev",
        namespace="gold",
    )

    grants.revoke(admin, created.id)

    event = session.query(AuditEvent).filter_by(event_type=AuditEventType.GRANT_REVOKED).one()
    assert event.securable_kind == SecurableKind.NAMESPACE
    assert event.securable_environment == "dev"
    assert event.securable_namespace == "gold"
    assert event.detail["grant_id"] == str(created.id)
    assert event.detail["principal_id"] == str(created.principal_id)
    assert event.detail["group_id"] is None
    assert event.detail["role"] == "VIEWER"
    assert event.detail["action"] is None


def _held_fixture(session: Session) -> tuple[PrincipalRef, PrincipalRef, uuid.UUID]:
    """Persist an administrator and bob, with a direct grant and a group grant for bob.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    tuple
        Bob's reference, the administrator's reference and the `analysts` group's id.
    """
    ada = PrincipalRef(id=uuid.uuid4(), name="ada", type=PrincipalType.USER)
    bob = PrincipalRef(id=uuid.uuid4(), name="bob", type=PrincipalType.USER)
    group_id = uuid.uuid4()
    for ref in (ada, bob):
        session.add(Principal(id=ref.id, type=ref.type, name=ref.name, is_active=True))
    session.add(Group(id=group_id, name="analysts"))
    session.flush()
    session.add(GroupMembership(group_id=group_id, principal_id=bob.id))
    session.add(
        Grant(
            id=uuid.uuid4(),
            principal_id=ada.id,
            role=Role.ADMIN,
            kind=SecurableKind.INSTANCE,
            granted_by=ada.id,
            granted_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
    )
    session.add(
        Grant(
            id=uuid.uuid4(),
            principal_id=bob.id,
            role=Role.ANALYST,
            kind=SecurableKind.ENVIRONMENT,
            environment="dev",
            granted_by=ada.id,
            granted_at=datetime(2026, 2, 1, tzinfo=UTC),
        )
    )
    session.add(
        Grant(
            id=uuid.uuid4(),
            group_id=group_id,
            role=Role.DATA_SCIENTIST,
            kind=SecurableKind.NAMESPACE,
            environment="dev",
            namespace="sales",
            granted_by=ada.id,
            granted_at=datetime(2026, 3, 1, tzinfo=UTC),
        )
    )
    session.commit()
    return bob, ada, group_id


def _auditing(session: Session) -> AuditingAuthorization:
    """Build the production pairing: grants, wrapped to audit denials.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    AuditingAuthorization
        The wrapper around `GrantAuthorization`.
    """
    return AuditingAuthorization(GrantAuthorization(session), session, DenialAuditLimiter(10_000))


def test_held_grants_lists_direct_and_group_grants_with_the_group_name(session: Session) -> None:
    """Bob's own and his group's grants come newest first, the group's with its name."""
    bob, _, _ = _held_fixture(session)
    held = GrantService(session, _auditing(session)).held_grants(bob, "bob")
    assert [(h.grant.role, h.group) for h in held] == [
        (Role.DATA_SCIENTIST, "analysts"),
        (Role.ANALYST, None),
    ]


def test_a_revoked_grant_and_a_left_group_are_not_held(session: Session) -> None:
    """A revoked grant and a group the person left drop out of the list."""
    bob, ada, group_id = _held_fixture(session)
    direct = session.scalars(select(Grant).where(Grant.principal_id == bob.id)).one()
    direct.revoked_at = datetime(2026, 4, 1, tzinfo=UTC)
    direct.revoked_by = ada.id
    session.commit()
    service = GrantService(session, _auditing(session))
    assert [h.group for h in service.held_grants(bob, "bob")] == ["analysts"]
    session.execute(delete(GroupMembership).where(GroupMembership.group_id == group_id))
    session.commit()
    assert service.held_grants(bob, "bob") == []


def test_someone_elses_grants_are_403_whether_or_not_they_exist(session: Session) -> None:
    """The refusal comes before any lookup: one text for a real and an unknown principal."""
    bob, _, _ = _held_fixture(session)
    service = GrantService(session, _auditing(session))
    messages = set()
    for name in ("ada", "nobody"):
        with pytest.raises(PermissionDenied) as caught:
            service.held_grants(bob, name)
        messages.add(str(caught.value))
    assert len(messages) == 1
    assert not any(name in text for text in messages for name in ("ada", "nobody"))


def test_an_administrator_asks_about_anyone_and_404s_an_unknown_name(session: Session) -> None:
    """An administrator reads bob's grants; an unknown name is 404 for them."""
    _, ada, _ = _held_fixture(session)
    service = GrantService(session, _auditing(session))
    assert len(service.held_grants(ada, "bob")) == 2
    with pytest.raises(NotFoundError):
        service.held_grants(ada, "nobody")


def test_a_task_token_may_not_ask_even_about_its_own_principal(session: Session) -> None:
    """A task or CI token is refused about its own principal too."""
    bob, _, _ = _held_fixture(session)
    task = PrincipalRef(
        id=bob.id,
        name=bob.name,
        type=bob.type,
        task_scope=TaskScope(environment="dev", asset_scope=["gold.*"], token_id=uuid.uuid4()),
    )
    service = GrantService(session, _auditing(session))
    with pytest.raises(PermissionDenied, match="Task tokens"):
        service.held_grants(task, "bob")


def test_the_last_administrator_refusal_names_the_next_step(session: Session) -> None:
    """Revoking the last ADMIN grant, or leaving the last ADMIN group, says what to do first."""
    grants = GrantService(session, AllowAllAuthorization())
    principals = PrincipalService(session, AllowAllAuthorization())
    groups = GroupService(session, AllowAllAuthorization())
    admin = _admin()
    _persist_actor(session, admin)
    _add_principal(principals, admin, "root")
    created = grants.grant(admin, principal="root", role=Role.ADMIN, kind=SecurableKind.INSTANCE)

    with pytest.raises(ConflictError) as revoked:
        grants.revoke(admin, created.id)
    assert str(revoked.value) == LAST_ADMIN_MESSAGE

    _add_principal(principals, admin, "ops-lead")
    groups.create_group(admin, name="ops")
    groups.add_member(admin, "ops", "ops-lead")
    grants.grant(admin, group="ops", role=Role.ADMIN, kind=SecurableKind.INSTANCE)
    grants.revoke(admin, created.id)
    with pytest.raises(ConflictError) as removed:
        groups.remove_member(admin, "ops", "ops-lead")
    assert str(removed.value) == LAST_ADMIN_MESSAGE
    assert LAST_ADMIN_MESSAGE == (
        "This would leave the instance without an active administrator. Give another person "
        "the ADMIN role on the instance first, then try again."
    )
