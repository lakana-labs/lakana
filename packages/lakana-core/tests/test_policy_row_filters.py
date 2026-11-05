"""Tests for RowFilterService."""

import inspect
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from core_test_helpers import FakeColumnCatalog, NoPolicyFields, ensure_table
from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.auth import Action, AllowAllAuthorization, PrincipalRef, Securable, SecurableKind
from lakana_core.catalog.json_schema import ColumnSpec
from lakana_core.catalog.schema_inference import infer_schema
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
    Environment,
    Grant,
    Principal,
    PrincipalType,
    RowFilter,
    Table,
    TableType,
)
from lakana_core.policy.row_filters import RowFilterService
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

    `RowFilter.created_by`/`revoked_by` are non-nullable foreign keys onto
    `principals.id` — any test that calls `RowFilterService.create`/`revoke`
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
    service = RowFilterService(session, AllowAllAuthorization(), FakeColumnCatalog())
    admin = _admin()
    with pytest.raises(ValidationError) as exc_info:
        service.create(
            admin,
            environment="dev",
            namespace="gold",
            table="customers",
            expression="region = current_user()",
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
            expression="region = current_user()",
        )
    assert str(exc_info.value) == "Exactly one of 'principal' or 'group' must be given."


def test_create_validates_the_expression(session: Session) -> None:
    """An expression outside the restricted subset is a ValidationError, not stored.

    The message is pinned, not just the exception type: `create` always
    validates with `kind="filter"`, and `validate_restricted_expression`
    interpolates that literal into its message
    (`"... is not a valid {kind} expression: ..."`). A test that only
    checks for `ValidationError` cannot tell `kind="filter"` apart from a
    mutated `kind=None`/`"FILTER"`/`"XXfilterXX"` — the same message shape
    comes out regardless — so the exact text is checked here instead.
    """
    service = RowFilterService(session, AllowAllAuthorization(), FakeColumnCatalog())
    principals = PrincipalService(session, AllowAllAuthorization())
    admin = _admin()
    _persist_actor(session, admin)
    principals.create_principal(admin, name="alice", type=PrincipalType.SERVICE_ACCOUNT)
    with pytest.raises(ValidationError) as exc_info:
        service.create(
            admin,
            principal="alice",
            environment="dev",
            namespace="gold",
            table="customers",
            expression="(SELECT 1)",
        )
    assert str(exc_info.value) == (
        "'(SELECT 1)' is not a valid filter expression: Subquery is outside the restricted subset."
    )


def test_create_raises_not_found_for_an_unknown_principal(session: Session) -> None:
    """Creating a row filter for a principal name that doesn't exist is a NotFoundError."""
    service = RowFilterService(session, AllowAllAuthorization(), FakeColumnCatalog())
    with pytest.raises(NotFoundError, match=r"No principal named 'nobody'"):
        service.create(
            _admin(),
            principal="nobody",
            environment="dev",
            namespace="gold",
            table="customers",
            expression="TRUE",
        )


def test_create_raises_not_found_for_an_unknown_group(session: Session) -> None:
    """Creating a row filter for a group name that doesn't exist is a NotFoundError."""
    service = RowFilterService(session, AllowAllAuthorization(), FakeColumnCatalog())
    with pytest.raises(NotFoundError, match=r"No group named 'nonexistent'"):
        service.create(
            _admin(),
            group="nonexistent",
            environment="dev",
            namespace="gold",
            table="customers",
            expression="TRUE",
        )


def test_create_raises_not_found_for_a_missing_table(session: Session) -> None:
    """Creating a row filter on a table that doesn't exist is a NotFoundError."""
    service = RowFilterService(session, AllowAllAuthorization(), FakeColumnCatalog())
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
            expression="TRUE",
        )


def test_create_raises_not_found_when_only_the_table_is_missing(session: Session) -> None:
    """A row filter is refused even when the *environment* alone resolves.

    `create`'s guard is `environment_id is None or table_id is None` — a
    mutant testing `and` instead would refuse only when *both* fail to
    resolve, silently creating a filter with `table_id=None` (nullable, so
    no foreign-key violation would catch it either) whenever just the
    table is missing from an otherwise real environment.
    """
    service = RowFilterService(session, AllowAllAuthorization(), FakeColumnCatalog())
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
            expression="TRUE",
        )


def test_create_records_the_group_name_as_subject_for_a_group_filter(session: Session) -> None:
    """The audit event's `detail["subject"]` is the group's name, not the principal's (None).

    A mutant forcing the ternary to always pick `principal`
    (`principal if (principal is not None) or True else group`) would
    record `None` instead of the group's name whenever a filter is created
    for a group rather than a principal.
    """
    service = RowFilterService(session, AllowAllAuthorization(), FakeColumnCatalog())
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
        expression="TRUE",
    )

    event = session.execute(
        select(AuditEvent).where(AuditEvent.event_type == AuditEventType.ROW_FILTER_CREATED)
    ).scalar_one()
    assert event.detail["subject"] == "analysts"


def test_list_for_principal_with_no_filters(session: Session) -> None:
    """Listing a principal with no filters returns an empty list, not a lookup error.

    With only one principal in the database, a query bug that resolves the
    named principal by "name != given name" instead of equality would match
    zero rows here too and (wrongly) raise NotFoundError instead of finding
    the principal and returning its (empty) filter list.
    """
    service = RowFilterService(session, AllowAllAuthorization(), FakeColumnCatalog())
    principals = PrincipalService(session, AllowAllAuthorization())
    admin = _admin()
    _persist_actor(session, admin)
    principals.create_principal(admin, name="bob", type=PrincipalType.SERVICE_ACCOUNT)

    assert service.list_for_principal(admin, "bob") == []


def test_list_for_group_with_no_filters(session: Session) -> None:
    """Listing a group with no filters returns an empty list, not a lookup error.

    Same reasoning as `test_list_for_principal_with_no_filters`, for groups.
    """
    service = RowFilterService(session, AllowAllAuthorization(), FakeColumnCatalog())
    groups = GroupService(session, AllowAllAuthorization())
    admin = _admin()
    _persist_actor(session, admin)
    groups.create_group(admin, name="readers")

    assert service.list_for_group(admin, "readers") == []


def test_create_then_list_for_principal(session: Session) -> None:
    """A created filter shows up when listing that principal's filters."""
    service = RowFilterService(session, AllowAllAuthorization(), FakeColumnCatalog())
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
        expression="region = current_user()",
    )
    assert created.expression == "region = current_user()"

    listed = service.list_for_principal(admin, "alice")
    assert [f.id for f in listed] == [created.id]
    # The fake catalog has no table metadata to rename from: shown as written, bound.
    assert service.shown(admin, listed) == {
        created.id: ShownPolicy("region = current_user()", None, True)
    }


def test_create_then_list_for_group(session: Session) -> None:
    """A created filter shows up when listing that group's filters."""
    service = RowFilterService(session, AllowAllAuthorization(), FakeColumnCatalog())
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
        expression="TRUE",
    )
    listed = service.list_for_group(admin, "analysts")
    assert [f.id for f in listed] == [created.id]


def test_list_for_principal_excludes_a_different_principals_filters(session: Session) -> None:
    """Listing alice's filters never returns a filter made to bob."""
    service = RowFilterService(session, AllowAllAuthorization(), FakeColumnCatalog())
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
        expression="TRUE",
    )
    service.create(
        admin,
        principal="bob",
        environment="dev",
        namespace="gold",
        table="customers",
        expression="TRUE",
    )

    listed = service.list_for_principal(admin, "alice")
    assert [f.id for f in listed] == [created.id]


def test_list_for_group_excludes_a_different_groups_filters(session: Session) -> None:
    """Listing the analysts' filters never returns a filter made to engineers."""
    service = RowFilterService(session, AllowAllAuthorization(), FakeColumnCatalog())
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
        expression="TRUE",
    )
    service.create(
        admin,
        group="engineers",
        environment="dev",
        namespace="gold",
        table="customers",
        expression="TRUE",
    )

    listed = service.list_for_group(admin, "analysts")
    assert [f.id for f in listed] == [created.id]


def test_list_for_group_excludes_a_revoked_filter(session: Session) -> None:
    """A revoked filter never shows up when listing a group's filters."""
    service = RowFilterService(session, AllowAllAuthorization(), FakeColumnCatalog())
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
        expression="TRUE",
    )
    service.revoke(admin, created.id)

    assert service.list_for_group(admin, "analysts") == []


def test_list_for_table_is_an_exact_match(session: Session) -> None:
    """Listing by table matches only the exact environment/namespace/table given."""
    service = RowFilterService(session, AllowAllAuthorization(), FakeColumnCatalog())
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
        expression="TRUE",
    )

    matching = service.list_for_table(admin, environment="dev", namespace="gold", table="customers")
    assert [f.id for f in matching] == [created.id]
    assert service.list_for_table(admin, environment="dev", namespace="gold", table="orders") == []
    assert (
        service.list_for_table(admin, environment="dev", namespace="silver", table="customers")
        == []
    )
    assert (
        service.list_for_table(admin, environment="prod", namespace="gold", table="customers") == []
    )


def test_list_for_table_excludes_a_revoked_filter(session: Session) -> None:
    """A revoked filter never shows up when listing by table."""
    service = RowFilterService(session, AllowAllAuthorization(), FakeColumnCatalog())
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
        expression="TRUE",
    )
    service.revoke(admin, created.id)

    assert (
        service.list_for_table(admin, environment="dev", namespace="gold", table="customers") == []
    )


def test_revoke_sets_revoked_at_and_revoked_by(session: Session) -> None:
    """Revoking a row filter sets its revoked_at/revoked_by, not deleting the row."""
    service = RowFilterService(session, AllowAllAuthorization(), FakeColumnCatalog())
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
        expression="TRUE",
    )

    service.revoke(admin, created.id)

    row = session.get(RowFilter, created.id)
    assert row is not None
    assert row.revoked_at is not None
    assert row.revoked_by == admin.id
    assert service.list_for_principal(admin, "alice") == []


def test_revoke_raises_not_found_for_an_unknown_id(session: Session) -> None:
    """Revoking a row filter id that doesn't exist is a NotFoundError."""
    service = RowFilterService(session, AllowAllAuthorization(), FakeColumnCatalog())
    row_filter_id = uuid.uuid4()
    with pytest.raises(NotFoundError, match=rf"No row filter {row_filter_id}"):
        service.revoke(_admin(), row_filter_id)


def test_create_authorizes_manage_policy_at_instance_scope(session: Session) -> None:
    """create() checks MANAGE_POLICY at instance scope before doing anything else."""
    recorder = _RecordingAuthz()
    service = RowFilterService(session, recorder, FakeColumnCatalog())
    admin = _admin()
    with pytest.raises(ValidationError):
        service.create(
            admin, environment="dev", namespace="gold", table="customers", expression="TRUE"
        )
    assert recorder.calls == [(admin, Action.MANAGE_POLICY, Securable.instance_scope())]


def test_list_for_principal_authorizes_manage_policy_at_instance_scope(session: Session) -> None:
    """list_for_principal() checks MANAGE_POLICY at instance scope before looking anything up."""
    recorder = _RecordingAuthz()
    service = RowFilterService(session, recorder, FakeColumnCatalog())
    admin = _admin()
    with pytest.raises(NotFoundError):
        service.list_for_principal(admin, "nobody")
    assert recorder.calls == [(admin, Action.MANAGE_POLICY, Securable.instance_scope())]


def test_list_for_group_authorizes_manage_policy_at_instance_scope(session: Session) -> None:
    """list_for_group() checks MANAGE_POLICY at instance scope before looking anything up."""
    recorder = _RecordingAuthz()
    service = RowFilterService(session, recorder, FakeColumnCatalog())
    admin = _admin()
    with pytest.raises(NotFoundError):
        service.list_for_group(admin, "nobody")
    assert recorder.calls == [(admin, Action.MANAGE_POLICY, Securable.instance_scope())]


def test_list_for_table_authorizes_manage_policy_at_instance_scope(session: Session) -> None:
    """list_for_table() checks MANAGE_POLICY at instance scope."""
    recorder = _RecordingAuthz()
    service = RowFilterService(session, recorder, FakeColumnCatalog())
    admin = _admin()
    result = service.list_for_table(admin, environment="dev", namespace="gold", table="customers")
    assert result == []
    assert recorder.calls == [(admin, Action.MANAGE_POLICY, Securable.instance_scope())]


def test_revoke_authorizes_manage_policy_at_instance_scope(session: Session) -> None:
    """revoke() checks MANAGE_POLICY at instance scope before looking anything up."""
    recorder = _RecordingAuthz()
    service = RowFilterService(session, recorder, FakeColumnCatalog())
    admin = _admin()
    with pytest.raises(NotFoundError):
        service.revoke(admin, uuid.uuid4())
    assert recorder.calls == [(admin, Action.MANAGE_POLICY, Securable.instance_scope())]


def test_create_refuses_a_view(session: Session) -> None:
    """A row filter on a view would be silently ignored (views expand first), so it's refused."""
    service = RowFilterService(session, AllowAllAuthorization(), FakeColumnCatalog())
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
            expression="TRUE",
        )

    assert str(exc_info.value) == (
        "'gold.recent' is a view; row filters and column masks apply to tables. "
        "Attach them to the view's base tables."
    )
    assert session.execute(select(RowFilter)).first() is None


# --- the filter's columns are checked against the table's when it is created ---


def _filter_args(expression: str) -> dict[str, str]:
    """Build the arguments of a filter for `alice` on `dev`/`gold.customers`.

    Parameters
    ----------
    expression
        The filter's expression.

    Returns
    -------
    dict
        The keyword arguments of `RowFilterService.create`.
    """
    return {
        "principal": "alice",
        "environment": "dev",
        "namespace": "gold",
        "table": "customers",
        "expression": expression,
    }


def _ready(
    session: Session, catalog: FakeColumnCatalog | None = None
) -> tuple[RowFilterService, PrincipalRef]:
    """Build a service over `catalog`, with the actor, the table and `alice` in place.

    Parameters
    ----------
    session
        Database session.
    catalog
        The column source; every column of `COMMON_MASK_COLUMNS` unless given.

    Returns
    -------
    tuple
        The service and the acting principal.
    """
    service = RowFilterService(session, AllowAllAuthorization(), catalog or FakeColumnCatalog())
    admin = _admin()
    _persist_actor(session, admin)
    ensure_table(session, "dev", "gold", "customers")
    PrincipalService(session, AllowAllAuthorization()).create_principal(
        admin, name="alice", type=PrincipalType.SERVICE_ACCOUNT
    )
    return service, admin


def test_create_accepts_columns_the_table_has_in_any_letter_case(session: Session) -> None:
    """The database matches a column without regard to case, so the filter does too."""
    catalog = FakeColumnCatalog(names=["id", "Region"], types={"id": "long"})
    service, admin = _ready(session, catalog)
    row = service.create(admin, **_filter_args("REGION = 'EU' AND id > 1"))
    assert row.expression == "REGION = 'EU' AND id > 1"
    assert [(p.id, ref.name) for p, ref in catalog.calls] == [(admin.id, "customers")]


@pytest.mark.parametrize(
    ("current", "expression", "recorded"),
    [(7, "region = 'EU'", 7), (7, "TRUE", None), (None, "region = 'EU'", None)],
)
def test_create_records_the_schema_the_filter_was_written_against(
    session: Session, current: int | None, expression: str, recorded: int | None
) -> None:
    """The current schema id is stored with a filter that reads a column, and none otherwise."""
    service, admin = _ready(session, FakeColumnCatalog(schema_id=current))
    assert service.create(admin, **_filter_args(expression)).schema_id == recorded


def test_create_refuses_a_column_the_table_does_not_have(session: Session) -> None:
    """The refusal names the column, the table and the columns the table has."""
    service, admin = _ready(session, FakeColumnCatalog(names=["id", "region"]))
    with pytest.raises(ValidationError) as exc_info:
        service.create(admin, **_filter_args("id > 1 AND regoin = 'EU'"))
    assert str(exc_info.value) == (
        "Table 'gold.customers' in environment 'dev' has no column 'regoin', which the row "
        "filter reads. Its columns are: id, region. Check the spelling."
    )
    assert session.execute(select(RowFilter)).scalars().all() == []


def test_create_names_the_first_unknown_column_alphabetically(
    session: Session,
) -> None:
    """With two unknown columns the message names the same one on every attempt."""
    service, admin = _ready(session, FakeColumnCatalog(names=["id"]))
    with pytest.raises(ValidationError, match="no column 'alpha'"):
        service.create(admin, **_filter_args("zeta = 1 AND alpha = 2"))


def test_create_counts_the_columns_beyond_the_listing(session: Session) -> None:
    """A wide table's message lists twenty columns and counts the rest."""
    names = [f"c{n:02d}" for n in range(25)]
    service, admin = _ready(session, FakeColumnCatalog(names=names))
    with pytest.raises(ValidationError) as exc_info:
        service.create(admin, **_filter_args("nope = 1"))
    assert str(exc_info.value).endswith(
        f"Its columns are: {', '.join(names[:20])}, and 5 more. Check the spelling."
    )


def test_create_says_when_the_table_has_no_recorded_columns(session: Session) -> None:
    """An empty column list is stated, not printed as an empty list."""
    service, admin = _ready(session, FakeColumnCatalog(names=[]))
    with pytest.raises(ValidationError) as exc_info:
        service.create(admin, **_filter_args("region = 'EU'"))
    assert str(exc_info.value) == (
        "Table 'gold.customers' in environment 'dev' has no column 'region', which the row "
        "filter reads. The table has no recorded columns."
    )


@pytest.mark.parametrize("expression", ["TRUE", "has_group('staff')", "current_user() = 'bob'"])
def test_a_filter_that_reads_no_column_does_not_ask_the_catalog(
    session: Session, expression: str
) -> None:
    """Nothing to check, so a table with no schema yet still takes such a filter."""
    catalog = FakeColumnCatalog(error=NotFoundError("no metadata"))
    service, admin = _ready(session, catalog)
    service.create(admin, **_filter_args(expression))
    assert catalog.calls == []


def test_create_says_the_table_has_no_schema_yet(session: Session) -> None:
    """A table nothing was written to has no columns to check; the message says what to do."""
    service, admin = _ready(session, FakeColumnCatalog(error=NotFoundError("no metadata")))
    with pytest.raises(ConflictError) as exc_info:
        service.create(admin, **_filter_args("region = 'EU'"))
    assert str(exc_info.value) == (
        "Table 'gold.customers' has no schema recorded yet, so its columns cannot be checked. "
        "Create the table with its columns first "
        "(lakana table create NAMESPACE.TABLE --column NAME:TYPE), then "
        "create the row filter, then grant READ_TABLE or write data."
    )
    assert session.execute(select(RowFilter)).scalars().all() == []


@pytest.mark.parametrize(
    "error",
    [
        OSError(2, "Failed to open local file '/tmp/secret-bucket/x.json'"),
        LakanaError("Metadata at 'file:///tmp/secret-bucket/0.metadata.json' is broken"),
    ],
)
def test_create_shows_no_path_when_the_columns_cannot_be_read(
    session: Session, error: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    """The cause goes to the server's log, never to the caller."""
    service, admin = _ready(session, FakeColumnCatalog(error=error))
    with caplog.at_level("ERROR"), pytest.raises(ConflictError) as exc_info:
        service.create(admin, **_filter_args("region = 'EU'"))
    assert str(exc_info.value) == (
        "The columns of table 'gold.customers' could not be read; the server's log has the "
        "cause. Create the row filter again when the table is readable."
    )
    assert "secret-bucket" in caplog.text


def test_a_qualified_column_is_refused_with_the_bare_name_as_advice(session: Session) -> None:
    """`t.region` where `t` is no column of the table: a qualifier, so write `region`."""
    service, admin = _ready(session, FakeColumnCatalog(names=["id", "region"]))
    with pytest.raises(ValidationError, match="Write the bare column name, 'region'"):
        service.create(admin, **_filter_args("read_parquet.region = 'EU'"))
    assert session.execute(select(RowFilter)).scalars().all() == []


def test_a_field_of_a_column_is_refused_as_a_struct_field(session: Session) -> None:
    """`address.country` where `address` is a column: no advice to write `country`."""
    service, admin = _ready(session, FakeColumnCatalog(names=["id", "address"]))
    with pytest.raises(ValidationError) as exc_info:
        service.create(admin, **_filter_args("address.country = 'DE'"))
    assert str(exc_info.value) == (
        "'address.country = 'DE'' is not a valid filter expression: 'address.country' reads a "
        "field of the column 'address', and a row filter does not support struct fields. Use a "
        "column of the table as a whole."
    )


@pytest.mark.parametrize(
    "error", [NotFoundError("no metadata"), OSError("gone"), PermissionDenied("no READ_TABLE")]
)
def test_a_qualified_column_is_refused_even_when_the_columns_cannot_be_read(
    session: Session, error: Exception
) -> None:
    """The columns only choose the wording: unreadable ones leave the qualifier message.

    `address.country` would read as a struct field if the caller could read the columns, so the
    qualifier wording here shows that nothing of them reached the message.
    """
    service, admin = _ready(session, FakeColumnCatalog(error=error))
    with pytest.raises(ValidationError, match="is written with a qualifier"):
        service.create(admin, **_filter_args("address.country = 'DE'"))


class _DenyAuthz:
    """Refuses every action, as a caller without MANAGE_POLICY would be."""

    def authorize(self, principal: PrincipalRef, action: Action, securable: Securable) -> None:
        """Refuse.

        Parameters
        ----------
        principal
            The actor.
        action
            The action requested.
        securable
            The object acted upon.

        Raises
        ------
        PermissionDenied
            Always.
        """
        raise PermissionDenied("You may not manage policies.")

    def permits(self, principal: PrincipalRef, action: Action, securable: Securable) -> bool:
        """Say no.

        Parameters
        ----------
        principal
            The actor.
        action
            The action in question.
        securable
            The object in question.

        Returns
        -------
        bool
            Always `False`.
        """
        return False


def test_a_caller_without_manage_policy_learns_nothing_about_the_columns(
    session: Session,
) -> None:
    """The same denial for a column the table has and one it does not; no column was read."""
    catalog = FakeColumnCatalog(names=["region"])
    service = RowFilterService(session, _DenyAuthz(), catalog)
    messages = []
    for column in ("region", "missing"):
        with pytest.raises(PermissionDenied) as exc_info:
            service.create(_admin(), **_filter_args(f"{column} = 1"))
        messages.append(str(exc_info.value))
    assert messages[0] == messages[1]
    assert catalog.calls == []


@dataclass
class _GrantCheckingCatalog(NoPolicyFields):
    """A catalog whose `describe_table` asks the real grant authorization for READ_TABLE."""

    authz: GrantAuthorization
    names: list[str]

    def describe_table(self, p: PrincipalRef, ref: TableRef) -> TableDescription:
        """Describe the table after asking for READ_TABLE as `p`.

        Parameters
        ----------
        p
            The acting principal.
        ref
            The table reference.

        Returns
        -------
        TableDescription
            A description with `names` as string columns.
        """
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
    """MANAGE_POLICY alone cannot read the columns: the same denial whatever the column."""
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
    service = RowFilterService(session, authz, _GrantCheckingCatalog(authz, names=["region"]))
    messages = []
    for column in ("region", "missing"):
        with pytest.raises(PermissionDenied) as exc_info:
            service.create(manager, **_filter_args(f"{column} = 1"))
        messages.append(str(exc_info.value))
    assert messages[0] == messages[1]
    assert messages[0] == (
        "You need READ_TABLE on 'gold.customers' so that the column can be checked."
    )
    assert session.execute(select(RowFilter)).scalars().all() == []


def test_a_policy_manager_without_read_table_gets_the_qualifier_wording_for_a_dotted_name(
    session: Session,
) -> None:
    """With the real grants, MANAGE_POLICY alone cannot learn that `address` is a column."""
    admin = _admin()
    _persist_actor(session, admin)
    ensure_table(session, "dev", "gold", "customers")
    PrincipalService(session, AllowAllAuthorization()).create_principal(
        admin, name="alice", type=PrincipalType.SERVICE_ACCOUNT
    )
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
    authz = GrantAuthorization(session)
    service = RowFilterService(session, authz, _GrantCheckingCatalog(authz, names=["address"]))
    with pytest.raises(ValidationError, match="is written with a qualifier"):
        service.create(admin, **_filter_args("address.country = 'DE'"))


def test_create_has_no_way_to_leave_the_columns_unchecked() -> None:
    """`create` takes no `catalog` argument, and the constructor requires one."""
    assert "catalog" not in inspect.signature(RowFilterService.create).parameters
    assert "catalog" in inspect.signature(RowFilterService).parameters


_FILTER_PAGE = Path(__file__).parents[3] / "docs" / "site" / "how-to" / "filter-rows.md"
"""The how-to page whose "When Lakana refuses" table quotes the messages below."""


def _page_quotes() -> list[str]:
    """Read the messages the page's refusal table quotes, one per row.

    Returns
    -------
    list of str
        The text between the backticks at the start of each row of the table.
    """
    page = _FILTER_PAGE.read_text()
    table = page.split("## When Lakana refuses", 1)[1].split("\n\n", 3)[2]
    return [
        row.split("|")[1].strip().split("` (")[0].lstrip("`")
        for row in table.splitlines()
        if row.startswith("| `")
    ]


def test_the_page_quotes_seventeen_refusals() -> None:
    """The table the next tests read has the rows they expect."""
    assert len(_page_quotes()) == 17


def _create_error(session: Session, expression: str = "id > 1", **override: str) -> Exception:
    """Provoke the refusal of a filter create and return the error.

    Parameters
    ----------
    session
        Database session.
    expression
        The filter's expression.
    **override
        Fields of the create call to change from `_filter_args`.

    Returns
    -------
    Exception
        The error `RowFilterService.create` raised.
    """
    service, admin = _ready(session, FakeColumnCatalog(names=["id", "region", "address"]))
    with pytest.raises(LakanaError) as caught:
        service.create(admin, **{**_filter_args(expression), **override})
    return caught.value


def test_a_qualified_column_is_refused_as_the_page_says(session: Session) -> None:
    """Row `... is written with a qualifier ...`: the text is the code's."""
    assert str(_create_error(session, "t.region = 'EU'")) in _page_quotes()


def test_a_struct_field_is_refused_as_the_page_says(session: Session) -> None:
    """Row `... reads a field of the column ...`: the text is the code's."""
    assert str(_create_error(session, "address.country = 'DE'")) in _page_quotes()


def test_an_unknown_column_is_refused_as_the_page_says(session: Session) -> None:
    """Row `Table ... has no column 'regoin' ...`: the page's example on the page's table."""
    service, admin = _ready(session, FakeColumnCatalog(names=["id", "region"]))
    with pytest.raises(ValidationError) as caught:
        service.create(admin, **_filter_args("regoin = 'EU'"))
    assert str(caught.value) == (
        "Table 'gold.customers' in environment 'dev' has no column 'regoin', which the row "
        "filter reads. Its columns are: id, region. Check the spelling."
    )
    assert str(caught.value) in _page_quotes()


def test_a_table_without_a_schema_is_refused_as_the_page_says(session: Session) -> None:
    """Row `Table ... has no schema recorded yet ...`: the text is the code's."""
    service, admin = _ready(session, FakeColumnCatalog(error=NotFoundError("no metadata")))
    with pytest.raises(ConflictError) as caught:
        service.create(admin, **_filter_args("region = 'EU'"))
    assert str(caught.value) in _page_quotes()


def test_an_unreadable_table_is_refused_as_the_page_says(session: Session) -> None:
    """Row `The columns of table ... could not be read ...`: the text is the code's."""
    service, admin = _ready(session, FakeColumnCatalog(error=OSError("gone")))
    with pytest.raises(ConflictError) as caught:
        service.create(admin, **_filter_args("region = 'EU'"))
    assert str(caught.value) in _page_quotes()


def test_a_missing_read_table_is_refused_as_the_page_says(session: Session) -> None:
    """Row `You need READ_TABLE on ...`: the text is the code's."""
    admin = _admin()
    _persist_actor(session, admin)
    ensure_table(session, "dev", "gold", "customers")
    PrincipalService(session, AllowAllAuthorization()).create_principal(
        admin, name="alice", type=PrincipalType.SERVICE_ACCOUNT
    )
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
    authz = GrantAuthorization(session)
    service = RowFilterService(session, authz, _GrantCheckingCatalog(authz, names=["id"]))
    with pytest.raises(PermissionDenied) as caught:
        service.create(admin, **_filter_args("id > 1"))
    assert str(caught.value) in _page_quotes()


def test_an_invalid_expression_is_refused_as_the_page_says(session: Session) -> None:
    """Row `'sha256(a)' is not a valid filter expression: ...`: the code's first words."""
    message = str(_create_error(session, "sha256(id)"))
    [quoted] = [q for q in _page_quotes() if q.startswith("'sha256(a)' is not a valid filter")]
    assert message.startswith("'sha256(id)' is not a valid filter expression: ")
    assert quoted.endswith("expression: ...")


def test_a_missing_principal_is_refused_as_the_page_says(session: Session) -> None:
    """Row `No principal named ...`: the text is the code's."""
    assert str(_create_error(session, principal="nobody")) in _page_quotes()


def test_a_missing_table_is_refused_as_the_page_says(session: Session) -> None:
    """Row `No table ...`: the text is the code's."""
    assert str(_create_error(session, table="nope")) in _page_quotes()


def test_a_view_is_refused_as_the_page_says(session: Session) -> None:
    """Row `'gold.names' is a view; ...`: the text is the code's, with the page's name."""
    _, view_id = ensure_table(session, "dev", "gold", "names")
    view = session.get(Table, view_id)
    assert view is not None
    view.table_type = TableType.VIEW
    session.commit()
    assert str(_create_error(session, table="names")) in _page_quotes()


def test_an_artifact_is_refused_as_the_page_says(session: Session) -> None:
    """Row `'gold.churn' is an artifact; ...`: the text is the code's."""
    _, table_id = ensure_table(session, "dev", "gold", "churn")
    row = session.get(Table, table_id)
    assert row is not None
    row.table_type = TableType.ARTIFACT
    session.commit()
    assert str(_create_error(session, table="churn")) in _page_quotes()


def test_an_expression_that_is_too_long_is_refused_as_the_page_says() -> None:
    """Row `The row-filter expression is 100001 characters long; ...`: the code's first words."""
    message = statement_too_long_message("The row-filter expression", 100_001, 100_000)
    [quoted] = [q for q in _page_quotes() if q.startswith("The row-filter expression is")]
    assert message.startswith(quoted.removesuffix(" ...").rstrip())


_TYPED = {"id": "long", "region": "string", "salary": "double", "owner": "boolean"}
"""The declared types the type-check tests give the fake table's columns."""


def _create_filter(
    session: Session, expression: str, catalog: FakeColumnCatalog | None = None
) -> RowFilter:
    """Create a row filter for a service account on a table with typed columns.

    Parameters
    ----------
    session
        Database session.
    expression
        The filter's expression.
    catalog
        The table's columns; typed ones (`_TYPED`) unless given.

    Returns
    -------
    RowFilter
        The stored filter.
    """
    service = RowFilterService(
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
        expression=expression,
    )


@pytest.mark.parametrize(
    ("expression", "fragment"),
    [
        ("region = 5", "`region` is text; compare it with '5', not 5."),
        ("region = salary", "`region` is text but `salary` is double; compare columns"),
        ("region", "`region` is text, not boolean; a condition is needed here."),
        ("id = 'x'", "`id` is long; compare it with a number, not 'x'."),
        ("owner = 1", "`owner` is boolean; compare it with TRUE or FALSE, not 1."),
    ],
)
def test_create_refuses_an_expression_that_needs_an_implicit_cast(
    session: Session, expression: str, fragment: str
) -> None:
    """A comparison across types, or a text used as a condition, is refused and not stored."""
    with pytest.raises(ValidationError) as exc_info:
        _create_filter(session, expression)
    assert fragment in str(exc_info.value)
    assert session.execute(select(RowFilter)).scalars().all() == []


@pytest.mark.parametrize(
    "expression",
    ["id = 5", "id < 5", "region = 'EU'", "owner", "owner AND id > 1", "has_group('x')"],
)
def test_create_accepts_an_expression_that_compares_like_with_like(
    session: Session, expression: str
) -> None:
    """The allowed shapes are stored, with the schema id the check read."""
    created = _create_filter(session, expression)
    assert created.expression == expression


def test_create_refuses_a_constant_that_is_not_a_condition_without_reading_the_table(
    session: Session,
) -> None:
    """A filter that reads no column is checked on its own, so the table is not read."""
    catalog = FakeColumnCatalog(types=_TYPED)
    with pytest.raises(ValidationError, match="the number 1 is not a condition"):
        _create_filter(session, "1", catalog)
    assert catalog.calls == []


def test_create_names_a_missing_column_before_it_checks_types(session: Session) -> None:
    """A column the table lacks keeps its own message."""
    with pytest.raises(ValidationError, match="has no column 'ghost'"):
        _create_filter(session, "ghost = 5")


def test_a_comparison_across_types_is_refused_as_the_page_says(session: Session) -> None:
    """Row `'region = 5' is not a valid filter expression ...`: the text is the code's."""
    with pytest.raises(ValidationError) as caught:
        _create_filter(session, "region = 5")
    assert str(caught.value) in _page_quotes()


def test_a_column_used_as_a_condition_is_refused_as_the_page_says(session: Session) -> None:
    """Row `'region' is not a valid filter expression ...`: the code's first words."""
    with pytest.raises(ValidationError) as caught:
        _create_filter(session, "region")
    [quoted] = [q for q in _page_quotes() if q.startswith("'region' is not a valid filter")]
    assert str(caught.value).startswith(quoted.removesuffix(" ...").rstrip())


def test_a_bad_typed_date_is_refused_as_the_page_says(session: Session) -> None:
    """Row `'born = DATE '2026-13-01'' is not a valid expression ...`: the code's first words."""
    with pytest.raises(ValidationError) as caught:
        _create_filter(session, "born = DATE '2026-13-01'")
    [quoted] = [q for q in _page_quotes() if q.startswith("'born = DATE '2026-13-01''")]
    assert str(caught.value).startswith(quoted.removesuffix(" ...").rstrip())


@pytest.mark.parametrize("file_format", ["csv", "json"])
@pytest.mark.parametrize("expression", ["id = 5", "id = 'x'", "region = 5", "region"])
def test_a_filter_on_a_csv_or_json_table_is_checked_only_against_the_subset(
    session: Session, file_format: str, expression: str
) -> None:
    """The stored types of such a table are a guess; DuckDB reads its own, so no type check."""
    catalog = FakeColumnCatalog(types={"id": "int64"}, file_format=file_format)
    assert _create_filter(session, expression, catalog).expression == expression


def test_a_filter_on_a_parquet_table_is_type_checked(session: Session) -> None:
    """A Parquet file carries its schema, so the stored types are the ones the engine reads."""
    catalog = FakeColumnCatalog(types={"id": "int64"}, file_format="parquet")
    assert _create_filter(session, "id = 5", catalog).expression == "id = 5"


def test_a_filter_on_a_parquet_table_keeps_the_strict_check(session: Session) -> None:
    """A mismatch on a Parquet column is refused with the stored type named."""
    catalog = FakeColumnCatalog(types={"id": "int64"}, file_format="parquet")
    with pytest.raises(ValidationError, match="`id` is int64; compare it with a number"):
        _create_filter(session, "id = 'x'", catalog)


@pytest.mark.parametrize(
    ("file_format", "sample", "expression"),
    [
        ("csv", b"id,ts\n007,2026-01-01 10:00:00\n008,2026-01-02 10:00:00\n", "id = 7"),
        ("csv", b"id,ts\n1,2026-01-01 10:00:00\n", "ts > TIMESTAMP '2026-01-01 00:00:00'"),
        ("json", b'{"u": "550e8400-e29b-41d4-a716-446655440000", "t": "10:00:00"}\n', "u = 'x'"),
        ("json", b'{"u": "550e8400-e29b-41d4-a716-446655440000", "t": "10:00:00"}\n', "t = 'x'"),
    ],
)
def test_a_filter_on_the_types_infer_schema_really_stores_is_not_refused(
    session: Session, file_format: str, sample: bytes, expression: str
) -> None:
    """The cases where the stored guess and the engine's own reading differ are not checked."""
    stored = infer_schema(sample, file_format)
    catalog = FakeColumnCatalog(names=list(stored), types=stored, file_format=file_format)

    assert _create_filter(session, expression, catalog).expression == expression


# --- the filter's columns are bound to field ids when it is created ---


def test_a_new_row_filter_stores_the_field_ids_of_the_columns_it_reads(session: Session) -> None:
    """`Region = 'eu'` against a table whose `region` is field 2; the name stays as written."""
    catalog = FakeColumnCatalog(names=["id", "region"], schema_id=1)
    service, admin = _ready(session, catalog)

    row = service.create(admin, **_filter_args("Region = 'eu'"))

    assert (row.binding, row.field_ids) == ("field_ids", {"Region": 2})


def test_a_filter_that_reads_no_column_is_bound_and_empty(session: Session) -> None:
    """`TRUE` has nothing to follow: `field_ids` with an empty map."""
    service, admin = _ready(session, FakeColumnCatalog(schema_id=1))

    row = service.create(admin, **_filter_args("TRUE"))

    assert (row.binding, row.field_ids) == ("field_ids", {})


def test_a_filter_that_reads_no_column_on_a_raw_table_is_bound_and_empty(
    session: Session,
) -> None:
    """Even on a raw table `TRUE` is `field_ids` with `{}`: it does not ask the catalog."""
    catalog = FakeColumnCatalog(file_format="parquet")
    service, admin = _ready(session, catalog)

    row = service.create(admin, **_filter_args("TRUE"))

    assert (row.binding, row.field_ids, catalog.calls) == ("field_ids", {}, [])


def test_a_filter_on_a_raw_table_is_bound_by_name(session: Session) -> None:
    """An external table has no field ids: `names`, nothing stored."""
    catalog = FakeColumnCatalog(names=["id", "region"], file_format="parquet")
    service, admin = _ready(session, catalog)

    row = service.create(admin, **_filter_args("region = 'eu'"))

    assert (row.binding, row.field_ids) == ("names", None)


def test_creation_binds_ids_from_the_description_it_type_checked(session: Session) -> None:
    """Regression pin: one `describe_table` call gives the schema id, the types and the ids."""
    catalog = FakeColumnCatalog(names=["id", "region"], schema_id=1)
    service, admin = _ready(session, catalog)

    service.create(admin, **_filter_args("region = 'eu'"))

    assert len(catalog.calls) == 1


def test_creation_refuses_an_ambiguous_column(session: Session) -> None:
    """A table with `Maß` and `MASS`: the filter `Maß = 'x'` is refused with the ambiguity text."""
    catalog = FakeColumnCatalog(names=["Maß", "MASS"], schema_id=1)
    service, admin = _ready(session, catalog)

    with pytest.raises(ValidationError) as exc_info:
        service.create(admin, **_filter_args("Maß = 'x'"))

    assert str(exc_info.value) == (
        "The policy names column 'Maß', and the table has the columns 'MASS' and 'Maß', "
        "which Lakana cannot tell apart for a policy. Ask the owner of the table to rename "
        "one of them, then create the policy again."
    )
    assert session.execute(select(RowFilter)).scalars().all() == []
