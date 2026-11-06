"""Tests for ColumnMaskService."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict, cast

import pytest
from core_test_helpers import FakeColumnCatalog, NoPolicyFields, ensure_table
from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.auth import Action, AllowAllAuthorization, PrincipalRef, Securable, SecurableKind
from lakana_core.catalog.json_schema import ColumnSpec
from lakana_core.catalog.service import TableDescription
from lakana_core.errors import (
    ConflictError,
    LakanaError,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.identifiers import TableRef
from lakana_core.identity.groups import GroupService
from lakana_core.identity.principals import PrincipalService
from lakana_core.models import (
    AuditEvent,
    AuditEventType,
    ColumnMask,
    Environment,
    Grant,
    Principal,
    PrincipalType,
    Table,
    TableType,
)
from lakana_core.policy.column_masks import ColumnMaskService
from lakana_core.policy.shown import ShownPolicy
from lakana_core.statement_limit import statement_too_long_message


class _RecordingAuthz:
    """An AuthorizationService that records exactly what it was called with.

    `AllowAllAuthorization`, used everywhere else in this file, ignores its
    arguments entirely, so it can't tell a test whether a service method
    checked the right action/securable — only whether it checked *something*.
    This double records the call so a test can assert on it directly.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[PrincipalRef, Action, Securable]] = []

    def authorize(self, principal: PrincipalRef, action: Action, securable: Securable) -> None:
        """Record the call; never denies.

        Parameters
        ----------
        principal
            The actor requesting the action.
        action
            The action requested.
        securable
            The object acted upon.
        """
        self.calls.append((principal, action, securable))

    def permits(self, principal: PrincipalRef, action: Action, securable: Securable) -> bool:
        """Record the call; always permits.

        Parameters
        ----------
        principal
            The actor the question is about.
        action
            The action in question.
        securable
            The object in question.

        Returns
        -------
        bool
            Always `True`.
        """
        self.calls.append((principal, action, securable))
        return True


def _admin() -> PrincipalRef:
    """Build the acting principal used to drive every service call in these tests.

    Returns
    -------
    PrincipalRef
        A user principal, independent of any fixture instance.
    """
    return PrincipalRef(id=uuid.uuid4(), name="admin", type=PrincipalType.USER)


def _persist_actor(session: Session, p: PrincipalRef) -> None:
    """Insert a Principal row backing an acting `PrincipalRef`.

    `ColumnMask.created_by`/`revoked_by` are non-nullable foreign keys onto
    `principals.id` — any test that calls `ColumnMaskService.create`/`revoke`
    needs its actor persisted first, or the insert/update fails its foreign key.

    Parameters
    ----------
    session
        Database session.
    p
        The acting principal to back with a real row, same id.
    """
    session.add(Principal(id=p.id, type=p.type, name=p.name, is_active=True))
    session.commit()


def test_create_requires_exactly_one_subject(session: Session) -> None:
    """Passing both principal and group, or neither, is a ValidationError."""
    service = ColumnMaskService(session, AllowAllAuthorization(), FakeColumnCatalog())
    admin = _admin()
    with pytest.raises(ValidationError) as exc_info:
        service.create(
            admin,
            environment="dev",
            namespace="gold",
            table="customers",
            column="ssn",
            expression="NULL",
        )
    assert str(exc_info.value) == "Exactly one of 'principal' or 'group' must be given."
    with pytest.raises(ValidationError) as exc_info:
        service.create(
            admin,
            principal="a",
            group="b",
            environment="dev",
            namespace="gold",
            table="customers",
            column="ssn",
            expression="NULL",
        )
    assert str(exc_info.value) == "Exactly one of 'principal' or 'group' must be given."


def test_create_validates_the_expression(session: Session) -> None:
    """An expression outside the mask grammar is a ValidationError, not stored."""
    service = ColumnMaskService(session, AllowAllAuthorization(), FakeColumnCatalog())
    principals = PrincipalService(session, AllowAllAuthorization())
    admin = _admin()
    _persist_actor(session, admin)
    principals.create_principal(admin, name="alice", type=PrincipalType.SERVICE_ACCOUNT)
    with pytest.raises(ValidationError):
        service.create(
            admin,
            principal="alice",
            environment="dev",
            namespace="gold",
            table="customers",
            column="ssn",
            expression="sha256(ssn)",
        )


def test_create_accepts_the_broader_mask_grammar(session: Session) -> None:
    """substr/||/md5 are valid for a mask, matching the design's mask-only grammar."""
    service = ColumnMaskService(session, AllowAllAuthorization(), FakeColumnCatalog())
    principals = PrincipalService(session, AllowAllAuthorization())
    admin = _admin()
    _persist_actor(session, admin)
    ensure_table(session, "dev", "gold", "customers")
    principals.create_principal(admin, name="alice", type=PrincipalType.SERVICE_ACCOUNT)
    created = service.create(
        admin,
        principal="alice",
        environment="dev",
        namespace="gold",
        table="customers",
        column="ssn",
        expression="substr(ssn, 1, 3) || '***'",
    )
    assert created.expression == "substr(ssn, 1, 3) || '***'"


def test_create_raises_not_found_for_an_unknown_principal(session: Session) -> None:
    """Creating a column mask for a principal name that doesn't exist is a NotFoundError."""
    service = ColumnMaskService(session, AllowAllAuthorization(), FakeColumnCatalog())
    with pytest.raises(NotFoundError, match=r"No principal named 'nobody'"):
        service.create(
            _admin(),
            principal="nobody",
            environment="dev",
            namespace="gold",
            table="customers",
            column="ssn",
            expression="NULL",
        )


def test_create_raises_not_found_for_an_unknown_group(session: Session) -> None:
    """Creating a column mask for a group name that doesn't exist is a NotFoundError."""
    service = ColumnMaskService(session, AllowAllAuthorization(), FakeColumnCatalog())
    with pytest.raises(NotFoundError, match=r"No group named 'nonexistent'"):
        service.create(
            _admin(),
            group="nonexistent",
            environment="dev",
            namespace="gold",
            table="customers",
            column="ssn",
            expression="NULL",
        )


def test_create_raises_not_found_for_a_missing_table(session: Session) -> None:
    """Creating a column mask on a table that doesn't exist is a NotFoundError."""
    service = ColumnMaskService(session, AllowAllAuthorization(), FakeColumnCatalog())
    principals = PrincipalService(session, AllowAllAuthorization())
    admin = _admin()
    _persist_actor(session, admin)
    principals.create_principal(admin, name="alice", type=PrincipalType.SERVICE_ACCOUNT)
    with pytest.raises(NotFoundError, match=r"No table 'customers'"):
        service.create(
            admin,
            principal="alice",
            environment="nonexistent",
            namespace="gold",
            table="customers",
            column="ssn",
            expression="NULL",
        )


def test_create_raises_not_found_when_only_the_table_is_missing(session: Session) -> None:
    """A column mask is refused even when the *environment* alone resolves.

    `create`'s guard is `environment_id is None or table_id is None` — a
    mutant testing `and` instead would refuse only when *both* fail to
    resolve, silently creating a mask with `table_id=None` (nullable, so
    no foreign-key violation would catch it either) whenever just the
    table is missing from an otherwise real environment.
    """
    service = ColumnMaskService(session, AllowAllAuthorization(), FakeColumnCatalog())
    principals = PrincipalService(session, AllowAllAuthorization())
    admin = _admin()
    _persist_actor(session, admin)
    principals.create_principal(admin, name="alice", type=PrincipalType.SERVICE_ACCOUNT)
    session.add(
        Environment(
            id=uuid.uuid4(),
            name="dev",
            storage_root="file:///dev/null",
            storage_options={},
            properties={},
        )
    )
    session.commit()
    with pytest.raises(NotFoundError, match=r"No table 'customers'"):
        service.create(
            admin,
            principal="alice",
            environment="dev",
            namespace="gold",
            table="customers",
            column="ssn",
            expression="NULL",
        )


def test_create_records_the_group_name_as_subject_for_a_group_mask(session: Session) -> None:
    """The audit event's `detail["subject"]` is the group's name, not the principal's (None).

    A mutant forcing the ternary to always pick `principal`
    (`principal if (principal is not None) or True else group`) would
    record `None` instead of the group's name whenever a mask is created
    for a group rather than a principal.
    """
    service = ColumnMaskService(session, AllowAllAuthorization(), FakeColumnCatalog())
    groups = GroupService(session, AllowAllAuthorization())
    admin = _admin()
    _persist_actor(session, admin)
    ensure_table(session, "dev", "gold", "customers")
    groups.create_group(admin, name="analysts")

    service.create(
        admin,
        group="analysts",
        environment="dev",
        namespace="gold",
        table="customers",
        column="ssn",
        expression="NULL",
    )

    event = session.execute(
        select(AuditEvent).where(AuditEvent.event_type == AuditEventType.COLUMN_MASK_CREATED)
    ).scalar_one()
    assert event.detail["subject"] == "analysts"


def test_create_then_list_for_principal(session: Session) -> None:
    """A created mask shows up when listing that principal's masks."""
    service = ColumnMaskService(session, AllowAllAuthorization(), FakeColumnCatalog())
    principals = PrincipalService(session, AllowAllAuthorization())
    admin = _admin()
    _persist_actor(session, admin)
    ensure_table(session, "dev", "gold", "customers")
    principals.create_principal(admin, name="alice", type=PrincipalType.SERVICE_ACCOUNT)

    created = service.create(
        admin,
        principal="alice",
        environment="dev",
        namespace="gold",
        table="customers",
        column="ssn",
        expression="NULL",
    )
    listed = service.list_for_principal(admin, "alice")
    assert [m.id for m in listed] == [created.id]
    # The fake catalog has no table metadata to rename from: shown as written, bound.
    assert service.shown(admin, listed) == {created.id: ShownPolicy("NULL", "ssn", True)}


def test_create_then_list_for_group(session: Session) -> None:
    """A created mask shows up when listing that group's masks."""
    service = ColumnMaskService(session, AllowAllAuthorization(), FakeColumnCatalog())
    groups = GroupService(session, AllowAllAuthorization())
    admin = _admin()
    _persist_actor(session, admin)
    ensure_table(session, "dev", "gold", "customers")
    groups.create_group(admin, name="analysts")

    created = service.create(
        admin,
        group="analysts",
        environment="dev",
        namespace="gold",
        table="customers",
        column="ssn",
        expression="NULL",
    )
    listed = service.list_for_group(admin, "analysts")
    assert [m.id for m in listed] == [created.id]


def test_list_for_principal_excludes_a_different_principals_masks(session: Session) -> None:
    """Listing alice's masks never returns a mask made to bob."""
    service = ColumnMaskService(session, AllowAllAuthorization(), FakeColumnCatalog())
    principals = PrincipalService(session, AllowAllAuthorization())
    admin = _admin()
    _persist_actor(session, admin)
    ensure_table(session, "dev", "gold", "customers")
    principals.create_principal(admin, name="alice", type=PrincipalType.SERVICE_ACCOUNT)
    principals.create_principal(admin, name="bob", type=PrincipalType.SERVICE_ACCOUNT)
    created = service.create(
        admin,
        principal="alice",
        environment="dev",
        namespace="gold",
        table="customers",
        column="ssn",
        expression="NULL",
    )
    service.create(
        admin,
        principal="bob",
        environment="dev",
        namespace="gold",
        table="customers",
        column="ssn",
        expression="NULL",
    )

    listed = service.list_for_principal(admin, "alice")
    assert [m.id for m in listed] == [created.id]


def test_list_for_group_excludes_a_different_groups_masks(session: Session) -> None:
    """Listing the analysts' masks never returns a mask made to engineers."""
    service = ColumnMaskService(session, AllowAllAuthorization(), FakeColumnCatalog())
    groups = GroupService(session, AllowAllAuthorization())
    admin = _admin()
    _persist_actor(session, admin)
    ensure_table(session, "dev", "gold", "customers")
    groups.create_group(admin, name="analysts")
    groups.create_group(admin, name="engineers")
    created = service.create(
        admin,
        group="analysts",
        environment="dev",
        namespace="gold",
        table="customers",
        column="ssn",
        expression="NULL",
    )
    service.create(
        admin,
        group="engineers",
        environment="dev",
        namespace="gold",
        table="customers",
        column="ssn",
        expression="NULL",
    )

    listed = service.list_for_group(admin, "analysts")
    assert [m.id for m in listed] == [created.id]


def test_list_for_group_excludes_a_revoked_mask(session: Session) -> None:
    """A revoked mask never shows up when listing a group's masks."""
    service = ColumnMaskService(session, AllowAllAuthorization(), FakeColumnCatalog())
    groups = GroupService(session, AllowAllAuthorization())
    admin = _admin()
    _persist_actor(session, admin)
    ensure_table(session, "dev", "gold", "customers")
    groups.create_group(admin, name="analysts")
    created = service.create(
        admin,
        group="analysts",
        environment="dev",
        namespace="gold",
        table="customers",
        column="ssn",
        expression="NULL",
    )
    service.revoke(admin, created.id)

    assert service.list_for_group(admin, "analysts") == []


def test_list_for_table_is_an_exact_match(session: Session) -> None:
    """Listing by table matches only the exact environment/namespace/table given."""
    service = ColumnMaskService(session, AllowAllAuthorization(), FakeColumnCatalog())
    principals = PrincipalService(session, AllowAllAuthorization())
    admin = _admin()
    _persist_actor(session, admin)
    ensure_table(session, "dev", "gold", "customers")
    principals.create_principal(admin, name="alice", type=PrincipalType.SERVICE_ACCOUNT)
    created = service.create(
        admin,
        principal="alice",
        environment="dev",
        namespace="gold",
        table="customers",
        column="ssn",
        expression="NULL",
    )

    matching = service.list_for_table(admin, environment="dev", namespace="gold", table="customers")
    assert [m.id for m in matching] == [created.id]
    assert service.list_for_table(admin, environment="dev", namespace="gold", table="orders") == []
    assert (
        service.list_for_table(admin, environment="dev", namespace="silver", table="customers")
        == []
    )
    assert (
        service.list_for_table(admin, environment="prod", namespace="gold", table="customers") == []
    )


def test_list_for_table_excludes_a_revoked_mask(session: Session) -> None:
    """A revoked mask never shows up when listing by table."""
    service = ColumnMaskService(session, AllowAllAuthorization(), FakeColumnCatalog())
    principals = PrincipalService(session, AllowAllAuthorization())
    admin = _admin()
    _persist_actor(session, admin)
    ensure_table(session, "dev", "gold", "customers")
    principals.create_principal(admin, name="alice", type=PrincipalType.SERVICE_ACCOUNT)
    created = service.create(
        admin,
        principal="alice",
        environment="dev",
        namespace="gold",
        table="customers",
        column="ssn",
        expression="NULL",
    )
    service.revoke(admin, created.id)

    assert (
        service.list_for_table(admin, environment="dev", namespace="gold", table="customers") == []
    )


def test_revoke_sets_revoked_at_and_revoked_by(session: Session) -> None:
    """Revoking a column mask sets its revoked_at/revoked_by, not deleting the row."""
    service = ColumnMaskService(session, AllowAllAuthorization(), FakeColumnCatalog())
    principals = PrincipalService(session, AllowAllAuthorization())
    admin = _admin()
    _persist_actor(session, admin)
    ensure_table(session, "dev", "gold", "customers")
    principals.create_principal(admin, name="alice", type=PrincipalType.SERVICE_ACCOUNT)
    created = service.create(
        admin,
        principal="alice",
        environment="dev",
        namespace="gold",
        table="customers",
        column="ssn",
        expression="NULL",
    )

    service.revoke(admin, created.id)

    row = session.get(ColumnMask, created.id)
    assert row is not None
    assert row.revoked_at is not None
    assert row.revoked_by == admin.id
    assert service.list_for_principal(admin, "alice") == []


def test_revoke_raises_not_found_for_an_unknown_id(session: Session) -> None:
    """Revoking a column mask id that doesn't exist is a NotFoundError."""
    service = ColumnMaskService(session, AllowAllAuthorization(), FakeColumnCatalog())
    column_mask_id = uuid.uuid4()
    with pytest.raises(NotFoundError, match=rf"No column mask {column_mask_id}"):
        service.revoke(_admin(), column_mask_id)


def test_create_authorizes_manage_policy_at_instance_scope(session: Session) -> None:
    """create() checks MANAGE_POLICY at instance scope before doing anything else."""
    recorder = _RecordingAuthz()
    service = ColumnMaskService(session, recorder, FakeColumnCatalog())
    admin = _admin()
    with pytest.raises(ValidationError):
        service.create(
            admin,
            environment="dev",
            namespace="gold",
            table="customers",
            column="ssn",
            expression="NULL",
        )
    assert recorder.calls == [(admin, Action.MANAGE_POLICY, Securable.instance_scope())]


def test_list_for_principal_authorizes_manage_policy_at_instance_scope(session: Session) -> None:
    """list_for_principal() checks MANAGE_POLICY at instance scope before looking anything up."""
    recorder = _RecordingAuthz()
    service = ColumnMaskService(session, recorder, FakeColumnCatalog())
    admin = _admin()
    with pytest.raises(NotFoundError):
        service.list_for_principal(admin, "nobody")
    assert recorder.calls == [(admin, Action.MANAGE_POLICY, Securable.instance_scope())]


def test_list_for_group_authorizes_manage_policy_at_instance_scope(session: Session) -> None:
    """list_for_group() checks MANAGE_POLICY at instance scope before looking anything up."""
    recorder = _RecordingAuthz()
    service = ColumnMaskService(session, recorder, FakeColumnCatalog())
    admin = _admin()
    with pytest.raises(NotFoundError):
        service.list_for_group(admin, "nobody")
    assert recorder.calls == [(admin, Action.MANAGE_POLICY, Securable.instance_scope())]


def test_list_for_table_authorizes_manage_policy_at_instance_scope(session: Session) -> None:
    """list_for_table() checks MANAGE_POLICY at instance scope."""
    recorder = _RecordingAuthz()
    service = ColumnMaskService(session, recorder, FakeColumnCatalog())
    admin = _admin()
    result = service.list_for_table(admin, environment="dev", namespace="gold", table="customers")
    assert result == []
    assert recorder.calls == [(admin, Action.MANAGE_POLICY, Securable.instance_scope())]


def test_revoke_authorizes_manage_policy_at_instance_scope(session: Session) -> None:
    """revoke() checks MANAGE_POLICY at instance scope before looking anything up."""
    recorder = _RecordingAuthz()
    service = ColumnMaskService(session, recorder, FakeColumnCatalog())
    admin = _admin()
    with pytest.raises(NotFoundError):
        service.revoke(admin, uuid.uuid4())
    assert recorder.calls == [(admin, Action.MANAGE_POLICY, Securable.instance_scope())]


def test_list_for_principal_with_no_masks(session: Session) -> None:
    """Listing a principal with no masks returns an empty list."""
    service = ColumnMaskService(session, AllowAllAuthorization(), FakeColumnCatalog())
    principals = PrincipalService(session, AllowAllAuthorization())
    admin = _admin()
    _persist_actor(session, admin)
    principals.create_principal(admin, name="bob", type=PrincipalType.SERVICE_ACCOUNT)

    listed = service.list_for_principal(admin, "bob")
    assert listed == []


def test_list_for_group_with_no_masks(session: Session) -> None:
    """Listing a group with no masks returns an empty list."""
    service = ColumnMaskService(session, AllowAllAuthorization(), FakeColumnCatalog())
    groups = GroupService(session, AllowAllAuthorization())
    admin = _admin()
    _persist_actor(session, admin)
    groups.create_group(admin, name="readers")

    listed = service.list_for_group(admin, "readers")
    assert listed == []


def test_create_refuses_a_view(session: Session) -> None:
    """A column mask on a view would be silently ignored (views expand first), so it's refused."""
    service = ColumnMaskService(session, AllowAllAuthorization(), FakeColumnCatalog())
    principals = PrincipalService(session, AllowAllAuthorization())
    admin = _admin()
    _persist_actor(session, admin)
    principals.create_principal(admin, name="alice", type=PrincipalType.SERVICE_ACCOUNT)
    _, view_id = ensure_table(session, "dev", "gold", "recent")
    view = session.get(Table, view_id)
    assert view is not None
    view.table_type = TableType.VIEW
    session.commit()

    with pytest.raises(ValidationError) as exc_info:
        service.create(
            admin,
            principal="alice",
            environment="dev",
            namespace="gold",
            table="recent",
            column="region",
            expression="TRUE",
        )

    assert str(exc_info.value) == (
        "'gold.recent' is a view; row filters and column masks apply to tables. "
        "Attach them to the view's base tables."
    )
    assert session.execute(select(ColumnMask)).first() is None


class _DenyAuthz:
    """Refuses every action, as a caller without MANAGE_POLICY would be."""

    def authorize(self, principal: PrincipalRef, action: Action, securable: Securable) -> None:
        raise PermissionDenied("You may not manage policies.")

    def permits(self, principal: PrincipalRef, action: Action, securable: Securable) -> bool:
        return False


class _MaskArgs(TypedDict):
    """The keyword arguments of `create`, typed so that `**` unpacking checks."""

    principal: str
    environment: str
    namespace: str
    table: str
    column: str
    expression: str


def _mask_args(column: str, **override: str) -> _MaskArgs:
    return cast(
        _MaskArgs,
        {
            **{
                "principal": "alice",
                "environment": "dev",
                "namespace": "gold",
                "table": "customers",
                "column": column,
                "expression": "NULL",
            },
            **override,
        },
    )


def _ready(
    session: Session, catalog: FakeColumnCatalog | None = None
) -> tuple[ColumnMaskService, PrincipalRef]:
    service = ColumnMaskService(session, AllowAllAuthorization(), catalog or FakeColumnCatalog())
    admin = _admin()
    _persist_actor(session, admin)
    ensure_table(session, "dev", "gold", "customers")
    PrincipalService(session, AllowAllAuthorization()).create_principal(
        admin, name="alice", type=PrincipalType.SERVICE_ACCOUNT
    )
    return service, admin


def test_create_accepts_a_column_the_table_has(session: Session) -> None:
    """A column the table has is accepted; the catalog is asked as the caller."""
    catalog = FakeColumnCatalog(names=["id", "ssn"])
    service, admin = _ready(session, catalog)
    row = service.create(admin, **_mask_args("ssn"))
    assert row.column == "ssn"
    assert [(p, ref.name) for p, ref in catalog.calls] == [(admin, "customers")]


@pytest.mark.parametrize(("current", "recorded"), [(7, 7), (None, None)])
def test_create_records_the_schema_the_mask_was_written_against(
    session: Session, current: int | None, recorded: int | None
) -> None:
    """The current schema id of the table is stored with the mask; none when the table has none."""
    service, admin = _ready(session, FakeColumnCatalog(schema_id=current))
    assert service.create(admin, **_mask_args("ssn")).schema_id == recorded


def test_create_refuses_a_column_the_table_lacks(session: Session) -> None:
    """A typo names the column, the table and the columns that exist, and stores nothing."""
    catalog = FakeColumnCatalog(names=["id", "ssn"])
    service, admin = _ready(session, catalog)
    with pytest.raises(ValidationError) as exc_info:
        service.create(admin, **_mask_args("sn"))
    assert str(exc_info.value) == (
        "Table 'gold.customers' in environment 'dev' has no column 'sn'. "
        "Its columns are: id, ssn. Check the spelling; column names are case-sensitive."
    )
    assert session.execute(select(ColumnMask)).scalars().all() == []
    created = select(AuditEvent).where(AuditEvent.event_type == AuditEventType.COLUMN_MASK_CREATED)
    assert session.execute(created).scalars().all() == []


def test_create_column_check_is_case_sensitive_and_hints_the_right_case(session: Session) -> None:
    """`SSN` is not `ssn`, as at query time; the message suggests the real spelling."""
    catalog = FakeColumnCatalog(names=["id", "ssn"])
    service, admin = _ready(session, catalog)
    with pytest.raises(ValidationError, match=r"Did you mean 'ssn'\?"):
        service.create(admin, **_mask_args("SSN"))


@pytest.mark.parametrize("column", ["address.city", '"ssn"', "ssn "])
def test_create_refuses_nested_and_quoted_spellings(session: Session, column: str) -> None:
    """Dotted paths and quoted names are not column names and are refused."""
    catalog = FakeColumnCatalog(names=["id", "ssn", "address"])
    service, admin = _ready(session, catalog)
    with pytest.raises(ValidationError, match=r"has no column"):
        service.create(admin, **_mask_args(column))


def test_create_bounds_the_listed_columns(session: Session) -> None:
    """A very wide table lists the first twenty columns and counts the rest."""
    catalog = FakeColumnCatalog(names=[f"c{i:02d}" for i in range(45)])
    service, admin = _ready(session, catalog)
    with pytest.raises(ValidationError) as exc_info:
        service.create(admin, **_mask_args("nope"))
    message = str(exc_info.value)
    assert "c19" in message
    assert "c20" not in message
    assert "and 25 more" in message


def test_create_still_refuses_a_view_before_reading_columns(session: Session) -> None:
    """A view is refused with the view message; its columns are never read."""
    catalog = FakeColumnCatalog(names=["ssn"])
    service, admin = _ready(session, catalog)
    table = session.execute(select(Table).where(Table.name == "customers")).scalar_one()
    table.table_type = TableType.VIEW
    session.commit()
    with pytest.raises(ValidationError, match=r"is a view"):
        service.create(admin, **_mask_args("ssn"))
    assert catalog.calls == []


def test_create_refuses_a_table_without_data_and_says_to_write_first(session: Session) -> None:
    """A table with no metadata yet has no columns to check; the message says what to do."""
    catalog = FakeColumnCatalog(error=NotFoundError("Table 'customers' has no metadata yet."))
    service, admin = _ready(session, catalog)
    with pytest.raises(ConflictError) as exc_info:
        service.create(admin, **_mask_args("ssn"))
    assert str(exc_info.value) == (
        "Table 'gold.customers' has no schema recorded yet, so its columns cannot be checked. "
        "Create the table with its columns first "
        "(lakana table create NAMESPACE.TABLE --column NAME:TYPE), then "
        "create the mask, then grant READ_TABLE or write data."
    )
    assert session.execute(select(ColumnMask)).scalars().all() == []


@pytest.mark.parametrize(
    "error",
    [
        OSError(2, "Failed to open local file '/tmp/secret-bucket/x.json'"),
        LakanaError(
            "Metadata at 'file:///tmp/secret-bucket/00000.metadata.json' could not be parsed"
        ),
    ],
)
def test_create_names_no_path_or_exception_text_when_the_schema_is_unreadable(
    session: Session, error: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    """The message points to the server log; the cause is logged, never shown to the caller."""
    catalog = FakeColumnCatalog(error=error)
    service, admin = _ready(session, catalog)
    with caplog.at_level("ERROR"), pytest.raises(ConflictError) as exc_info:
        service.create(admin, **_mask_args("ssn"))
    message = str(exc_info.value)
    assert message == (
        "The columns of table 'gold.customers' could not be read; the server's log has the "
        "cause. Create the mask again when the table is readable."
    )
    assert "/tmp" not in message and "Errno" not in message and "metadata" not in message
    assert "secret-bucket" in caplog.text
    assert session.execute(select(ColumnMask)).scalars().all() == []


def test_create_says_when_the_table_has_no_recorded_columns(session: Session) -> None:
    """An empty column list is stated, not printed as an empty list."""
    service, admin = _ready(session, FakeColumnCatalog(names=[]))
    with pytest.raises(ValidationError) as exc_info:
        service.create(admin, **_mask_args("ssn"))
    assert str(exc_info.value) == (
        "Table 'gold.customers' in environment 'dev' has no column 'ssn'. "
        "The table has no recorded columns."
    )


@dataclass
class _GrantCheckingCatalog(NoPolicyFields):
    """A catalog whose `describe_table` asks the real grant authorization for READ_TABLE."""

    authz: GrantAuthorization
    names: list[str]

    def describe_table(self, p: PrincipalRef, ref: TableRef) -> TableDescription:
        self.authz.authorize(
            p, Action.READ_TABLE, Securable.table_scope(ref.environment, ref.namespace, ref.name)
        )
        return TableDescription(
            ref=ref,
            id=uuid.uuid4(),
            kind="TABLE",
            metadata_location=None,
            columns=[ColumnSpec(name=n, type="string", nullable=True) for n in self.names],
        )


def test_a_policy_manager_without_read_table_is_told_to_get_it(session: Session) -> None:
    """MANAGE_POLICY granted alone is refused as a permission problem, the same for any column."""
    admin = _admin()
    _persist_actor(session, admin)
    ensure_table(session, "dev", "gold", "customers")
    PrincipalService(session, AllowAllAuthorization()).create_principal(
        admin, name="alice", type=PrincipalType.SERVICE_ACCOUNT
    )
    manager = PrincipalRef(id=uuid.uuid4(), name="manager", type=PrincipalType.USER)
    session.add(Principal(id=manager.id, type=manager.type, name=manager.name, is_active=True))
    session.commit()
    session.add(
        Grant(
            id=uuid.uuid4(),
            principal_id=manager.id,
            action=Action.MANAGE_POLICY,
            kind=SecurableKind.INSTANCE,
            granted_by=admin.id,
            granted_at=datetime.now(UTC),
        )
    )
    session.commit()
    authz = GrantAuthorization(session)
    service = ColumnMaskService(session, authz, _GrantCheckingCatalog(authz, names=["ssn"]))
    messages = []
    for column in ("ssn", "missing"):
        with pytest.raises(PermissionDenied) as exc_info:
            service.create(manager, **_mask_args(column))
        messages.append(str(exc_info.value))
    assert messages[0] == messages[1]
    assert messages[0] == (
        "You need READ_TABLE on 'gold.customers' so that the column can be checked."
    )
    assert session.execute(select(ColumnMask)).scalars().all() == []


def test_create_without_authority_learns_nothing_about_the_columns(session: Session) -> None:
    """A caller who may not manage policies gets the same denial for any column."""
    catalog = FakeColumnCatalog(names=["ssn"])
    service = ColumnMaskService(session, _DenyAuthz(), catalog)
    messages = []
    for column in ("ssn", "missing"):
        with pytest.raises(PermissionDenied) as exc_info:
            service.create(_admin(), **_mask_args(column))
        messages.append(str(exc_info.value))
    assert messages[0] == messages[1]
    assert catalog.calls == []


def test_a_service_checks_the_column_without_being_asked_per_call(session: Session) -> None:
    """The catalog is the service's own: no call can skip the column check."""
    admin = _admin()
    _persist_actor(session, admin)
    ensure_table(session, "dev", "gold", "customers")
    PrincipalService(session, AllowAllAuthorization()).create_principal(
        admin, name="alice", type=PrincipalType.SERVICE_ACCOUNT
    )
    service = ColumnMaskService(session, AllowAllAuthorization(), FakeColumnCatalog(names=["id"]))
    with pytest.raises(ValidationError, match="has no column 'ssn'"):
        service.create(admin, **_mask_args("ssn"))
    assert session.execute(select(ColumnMask)).first() is None
    assert service.create(admin, **_mask_args("id")).column == "id"


def test_create_has_no_way_to_leave_the_column_unchecked() -> None:
    """`create` takes no `catalog` argument, and the constructor requires one."""
    import inspect

    assert "catalog" not in inspect.signature(ColumnMaskService.create).parameters
    assert (
        inspect.signature(ColumnMaskService).parameters["catalog"].default
        is inspect.Parameter.empty
    )


_MASK_PAGE = Path(__file__).parents[3] / "docs" / "site" / "how-to" / "mask-a-column.md"
"""The how-to page whose "When Lakana refuses" table quotes the messages below."""


def _page_quotes() -> list[str]:
    """Read the messages the page's refusal table quotes, one per row.

    Returns
    -------
    list of str
        The text between the backticks at the start of each row of the table.
    """
    page = _MASK_PAGE.read_text()
    table = page.split("## When Lakana refuses", 1)[1].split("\n\n", 3)[2]
    return [
        row.split("|")[1].strip().split("` (")[0].lstrip("`")
        for row in table.splitlines()
        if row.startswith("| `")
    ]


def test_the_page_quotes_seventeen_refusals() -> None:
    """The table the next tests read has the rows they expect."""
    assert len(_page_quotes()) == 17


def test_a_qualified_mask_expression_is_refused_as_the_page_says(session: Session) -> None:
    """Row `'read_parquet.ssn' is not a valid mask expression ...`: the text is the code's."""
    assert str(_create_error(session, expression="read_parquet.ssn")) in _page_quotes()


def _create_error(session: Session, **override: str) -> Exception:
    """Provoke the refusal of a mask create and return the error.

    Parameters
    ----------
    session
        Database session.
    **override
        Fields of the create call to change from `_mask_args`.

    Returns
    -------
    Exception
        The error `ColumnMaskService.create` raised.
    """
    service, admin = _ready(session)
    with pytest.raises(LakanaError) as caught:
        service.create(admin, **_mask_args("ssn", **override))
    return caught.value


def test_a_missing_table_is_refused_as_the_page_says(session: Session) -> None:
    """Row `No table 'nope' in 'dev'/gold.`: the text is the code's."""
    assert str(_create_error(session, table="nope")) in _page_quotes()


def test_an_artifact_is_refused_as_the_page_says(session: Session) -> None:
    """Row `'gold.churn' is an artifact; ...`: the text is the code's."""
    _, table_id = ensure_table(session, "dev", "gold", "churn")
    row = session.get(Table, table_id)
    assert row is not None
    row.table_type = TableType.ARTIFACT
    session.commit()
    assert str(_create_error(session, table="churn")) in _page_quotes()


def test_a_missing_read_table_is_refused_as_the_page_says(session: Session) -> None:
    """Row `You need READ_TABLE on ...`: the text is the code's."""
    service, admin = _ready(session)
    authz = GrantAuthorization(session)
    session.add(
        Grant(
            id=uuid.uuid4(),
            principal_id=admin.id,
            action=Action.MANAGE_POLICY,
            kind=SecurableKind.INSTANCE,
            granted_by=admin.id,
            granted_at=datetime.now(UTC),
        )
    )
    session.commit()
    service = ColumnMaskService(session, authz, _GrantCheckingCatalog(authz, names=["ssn"]))
    with pytest.raises(PermissionDenied) as caught:
        service.create(admin, **_mask_args("ssn"))
    assert str(caught.value) in _page_quotes()


def test_a_table_without_a_schema_is_refused_as_the_page_says(session: Session) -> None:
    """Row `Table ... has no schema recorded yet ...`: the text is the code's."""
    catalog = FakeColumnCatalog(error=NotFoundError("no metadata"))
    service, admin = _ready(session, catalog)
    with pytest.raises(ConflictError) as caught:
        service.create(admin, **_mask_args("ssn"))
    assert str(caught.value) in _page_quotes()


def test_an_unreadable_table_is_refused_as_the_page_says(session: Session) -> None:
    """Row `The columns of table ... could not be read ...`: the text is the code's."""
    service, admin = _ready(session, FakeColumnCatalog(error=OSError("gone")))
    with pytest.raises(ConflictError) as caught:
        service.create(admin, **_mask_args("ssn"))
    assert str(caught.value) in _page_quotes()


def test_an_expression_that_is_too_long_is_refused_as_the_page_says() -> None:
    """Row `The column-mask expression is 100001 characters long; ...`: the code's first words."""
    message = statement_too_long_message("The column-mask expression", 100_001, 100_000)
    [quoted] = [q for q in _page_quotes() if q.startswith("The column-mask expression is")]
    assert message.startswith(quoted.removesuffix(" ...").rstrip())


def test_a_mask_expression_that_qualifies_a_column_is_refused(session: Session) -> None:
    """The same refusal as a filter's: write the bare column name."""
    service, admin = _ready(session, FakeColumnCatalog(names=["id", "ssn"]))
    with pytest.raises(ValidationError) as exc_info:
        service.create(admin, **_mask_args("ssn", expression="read_parquet.ssn"))
    assert str(exc_info.value) == (
        "'read_parquet.ssn' is not a valid mask expression: the column 'read_parquet.ssn' is "
        "written with a qualifier. Write the bare column name, 'ssn'; a column mask always reads "
        "the table it is set on."
    )
    assert session.execute(select(ColumnMask)).scalars().all() == []


_TYPED = {"id": "long", "ssn": "string", "salary": "double", "owner": "boolean", "c": "date"}
"""The declared types the type-check tests give the fake table's columns."""


def _create_mask(
    session: Session, column: str, expression: str, catalog: FakeColumnCatalog | None = None
) -> ColumnMask:
    """Create a column mask for a service account on a table with typed columns.

    Parameters
    ----------
    session
        Database session.
    column
        The masked column.
    expression
        The mask's expression.
    catalog
        The table's columns; typed ones (`_TYPED`) unless given.

    Returns
    -------
    ColumnMask
        The stored mask.
    """
    service = ColumnMaskService(
        session,
        AllowAllAuthorization(),
        catalog or FakeColumnCatalog(types=_TYPED, schema_id=7),
    )
    admin = _admin()
    _persist_actor(session, admin)
    ensure_table(session, "dev", "gold", "customers")
    PrincipalService(session, AllowAllAuthorization()).create_principal(
        admin, name="alice", type=PrincipalType.SERVICE_ACCOUNT
    )
    return service.create(
        admin,
        principal="alice",
        environment="dev",
        namespace="gold",
        table="customers",
        column=column,
        expression=expression,
    )


@pytest.mark.parametrize(
    ("column", "expression"),
    [
        ("id", "0"),
        ("id", "NULL"),
        ("ssn", "'***'"),
        ("ssn", "substr(ssn, 1, 3) || '***'"),
        ("salary", "0"),
        ("owner", "FALSE"),
        ("c", "DATE '1900-01-01'"),
    ],
)
def test_create_accepts_a_mask_of_the_columns_own_type(
    session: Session, column: str, expression: str
) -> None:
    """INTEGER, TEXT, DOUBLE, BOOLEAN and DATE columns keep their type under a mask."""
    assert _create_mask(session, column, expression).expression == expression


@pytest.mark.parametrize(
    ("column", "expression", "fragment"),
    [
        (
            "id",
            "'***'",
            "`id` is long, but the mask is text. Use a value of its own type: 0 or NULL.",
        ),
        ("ssn", "0", "`ssn` is text, but the mask is a number. Use a value of its own type: '***'"),
        ("c", "'1900-01-01'", "`c` is date, but the mask is text."),
        ("owner", "0", "`owner` is boolean, but the mask is a number."),
        ("ssn", "id = 1 AND ssn = 5", "`ssn` is text; compare it with '5', not 5."),
    ],
)
def test_create_refuses_a_mask_of_another_type(
    session: Session, column: str, expression: str, fragment: str
) -> None:
    """A mask that changes the column's type names both types and a value that keeps it."""
    with pytest.raises(ValidationError) as exc_info:
        _create_mask(session, column, expression)
    assert fragment in str(exc_info.value)
    assert session.execute(select(ColumnMask)).scalars().all() == []


def test_a_mask_of_another_type_is_refused_as_the_page_says(session: Session) -> None:
    """Row `'0' is not a valid mask expression ...`: the code's first words."""
    with pytest.raises(ValidationError) as caught:
        _create_mask(session, "ssn", "0")
    [quoted] = [q for q in _page_quotes() if q.startswith("'0' is not a valid mask")]
    assert str(caught.value).startswith(quoted.removesuffix(" ...").rstrip())


def test_a_comparison_across_types_in_a_mask_is_refused_as_the_page_says(
    session: Session,
) -> None:
    """Row `'ssn = 5' is not a valid mask expression ...`: the text is the code's."""
    with pytest.raises(ValidationError) as caught:
        _create_mask(session, "owner", "ssn = 5")
    assert str(caught.value) in _page_quotes()


def test_create_records_that_the_mask_was_type_checked(session: Session) -> None:
    """A mask created now is marked, so a read casts it to the column's type."""
    assert _create_mask(session, "ssn", "'***'").type_checked is True


@pytest.mark.parametrize("file_format", ["csv", "json"])
def test_a_mask_on_a_csv_or_json_table_is_not_type_checked(
    session: Session, file_format: str
) -> None:
    """Such a mask is stored like an earlier release's: unchecked and never cast on a read."""
    catalog = FakeColumnCatalog(types={"id": "int64"}, file_format=file_format)
    created = _create_mask(session, "id", "'***'", catalog)
    assert created.expression == "'***'"
    assert created.type_checked is False


def test_a_mask_on_a_parquet_table_is_type_checked(session: Session) -> None:
    """A Parquet mask keeps the check and is marked, so a read casts it."""
    catalog = FakeColumnCatalog(types={"id": "int64"}, file_format="parquet")
    assert _create_mask(session, "id", "0", catalog).type_checked is True


def test_a_mask_of_another_type_on_a_parquet_table_is_refused(session: Session) -> None:
    """A text mask on an `int64` Parquet column is refused with the stored type named."""
    catalog = FakeColumnCatalog(types={"id": "int64"}, file_format="parquet")
    with pytest.raises(ValidationError, match="`id` is int64, but the mask is text"):
        _create_mask(session, "id", "'***'", catalog)


def test_a_mask_on_a_nanosecond_parquet_timestamp_is_stored_as_not_type_checked(
    session: Session,
) -> None:
    """DuckDB reads it as TIMESTAMP_NS, which has no cast target: the mask is used as written."""
    catalog = FakeColumnCatalog(names=["c"], types={"c": "timestamp[ns]"}, file_format="parquet")
    created = _create_mask(session, "c", "TIMESTAMP '1970-01-01 00:00:00'", catalog)
    assert created.type_checked is False


# --- the mask's columns are bound to field ids when it is created ---


def test_a_new_column_mask_stores_its_column_field_id(session: Session) -> None:
    """A mask on `email` reading `email`: both are field 3."""
    catalog = FakeColumnCatalog(names=["id", "name", "email"], schema_id=1)
    service, admin = _ready(session, catalog)

    row = service.create(admin, **_mask_args("email", expression="md5(email)"))

    assert (row.binding, row.field_ids, row.column_field_id) == ("field_ids", {"email": 3}, 3)


def test_a_mask_reading_another_column_stores_both_ids(session: Session) -> None:
    """The mask's own column is a key of its own, beside the column its expression reads."""
    catalog = FakeColumnCatalog(names=["id", "ssn", "dept"], schema_id=1)
    service, admin = _ready(session, catalog)

    row = service.create(admin, **_mask_args("ssn", expression="dept"))

    assert (row.field_ids, row.column_field_id) == ({"dept": 3, "ssn": 2}, 2)


def test_a_mask_on_a_raw_table_is_bound_by_name(session: Session) -> None:
    """An external table has no field ids: `names`, nothing stored."""
    catalog = FakeColumnCatalog(names=["id", "ssn"], file_format="csv")
    service, admin = _ready(session, catalog)

    row = service.create(admin, **_mask_args("ssn"))

    assert (row.binding, row.field_ids, row.column_field_id) == ("names", None, None)


def test_creation_binds_ids_from_the_description_it_type_checked(session: Session) -> None:
    """Regression pin: one `describe_table` call gives the schema id, the types and the ids."""
    catalog = FakeColumnCatalog(names=["id", "ssn"], schema_id=1)
    service, admin = _ready(session, catalog)

    service.create(admin, **_mask_args("ssn"))

    assert len(catalog.calls) == 1


def test_creation_refuses_a_mask_on_an_ambiguous_column(session: Session) -> None:
    """A table with `ssn` and `SSN`: a mask on `ssn` cannot be told apart, so it is refused."""
    catalog = FakeColumnCatalog(names=["ssn", "SSN"], schema_id=1)
    service, admin = _ready(session, catalog)

    with pytest.raises(ValidationError, match="cannot tell apart for a policy"):
        service.create(admin, **_mask_args("ssn"))

    assert session.execute(select(ColumnMask)).scalars().all() == []


def test_a_mask_reading_a_missing_column_gets_the_type_check_message(session: Session) -> None:
    """The expression's own message wins over the binding's "no column to bind to"."""
    catalog = FakeColumnCatalog(names=["id", "ssn"], schema_id=1)
    service, admin = _ready(session, catalog)

    with pytest.raises(ValidationError) as exc_info:
        service.create(admin, **_mask_args("ssn", expression="md5(nope)"))

    assert "to bind the policy to" not in str(exc_info.value)
    assert "md5(nope)" in str(exc_info.value)
