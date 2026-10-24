"""Table and environment retention: who may set it, the clock, and the sweep's expiry step.

A table expires a number of days after its last activity (the later of its last change in the
catalog and its last recorded read), or from a fixed day on. The worker's sweep drops a due table
through the drop path (`reason: retention`, no principal), so it leaves a tombstone that undrop
restores. Protected environments, a deployed project's assets, a table with a write running,
external tables and views never expire. These tests run on a SQLite file.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from core_test_helpers import ActionAllowList, python_task_run
from pyiceberg.schema import Schema
from pyiceberg.types import LongType, NestedField
from sqlalchemy import ColumnElement, select, update
from sqlalchemy.orm import Session

from lakana_core.auth import Action, AllowAllAuthorization, PrincipalRef
from lakana_core.catalog import retention as retention_module
from lakana_core.catalog.json_schema import ColumnSpec
from lakana_core.catalog.location_sweep import LocationSweepReport, sweep_table_locations
from lakana_core.catalog.retention import (
    NOT_MANAGED_MESSAGE,
    ONE_RULE_MESSAGE,
    PROTECTED_ENVIRONMENT_MESSAGE,
    PROTECTED_TABLE_MESSAGE,
    TableRetention,
    expiry_time,
)
from lakana_core.catalog.service import CatalogService
from lakana_core.config import DatabaseConfig, StorageConfig
from lakana_core.db import create_engine
from lakana_core.errors import NotFoundError, PermissionDenied, ValidationError
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.models import (
    AuditEvent,
    AuditEventType,
    Base,
    Environment,
    EnvironmentDeployment,
    JobVersion,
    Namespace,
    Principal,
    PrincipalType,
    QueryRun,
    QueryRunStatus,
    Repository,
    RepositoryKind,
    Table,
    TableLocation,
    TableLocationState,
    TableRead,
    TableType,
    TaskStatus,
    WriteMode,
)
from lakana_core.storage.resolver import StorageResolver

_GOLD = NamespacePath.from_parts(["gold"])
_ORDERS = TableRef("dev", _GOLD, "orders")


@pytest.fixture
def session(tmp_path: Path) -> Iterator[Session]:
    """Provide a session on a SQLite file (overrides the in-memory one).

    Parameters
    ----------
    tmp_path
        Holds the database file and, under `store/`, the environment's storage.

    Yields
    ------
    Session
        A session whose schema has been created from the models.
    """
    engine = create_engine(DatabaseConfig(url=f"sqlite:///{tmp_path / 'catalog.db'}"))
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s
    engine.dispose()


@pytest.fixture
def catalog(session: Session, principal: PrincipalRef, tmp_path: Path) -> CatalogService:
    """Create `dev` with `gold.orders` and return an allow-all catalog over it.

    Parameters
    ----------
    session
        Database session.
    principal
        The acting principal, who owns `gold.orders`.
    tmp_path
        Holds the storage root.

    Returns
    -------
    CatalogService
        The catalog.
    """
    store = tmp_path / "store"
    store.mkdir()
    built = _catalog(session)
    built.create_environment(
        principal,
        name="dev",
        storage=StorageConfig(scheme="file", root=f"file://{store}", options={}),
    )
    built.create_table(principal, _ORDERS, schema=_schema())
    return built


def _catalog(session: Session, authz: object | None = None, **options: int) -> CatalogService:
    """Build a catalog on `session`.

    Parameters
    ----------
    session
        Database session.
    authz
        Its authorization; allow-all when `None`.
    **options
        Passed to `CatalogService`.

    Returns
    -------
    CatalogService
        The catalog.
    """
    return CatalogService(
        session,
        StorageResolver(session),
        authz or AllowAllAuthorization(),  # ty: ignore[invalid-argument-type]
        **options,  # ty: ignore[invalid-argument-type]
    )


def _schema() -> Schema:
    """Build a one-column schema.

    Returns
    -------
    Schema
        ``id bigint``.
    """
    return Schema(NestedField(1, "id", LongType(), required=False))


def _user(session: Session, name: str) -> PrincipalRef:
    """Insert a user.

    Parameters
    ----------
    session
        Database session; commits.
    name
        The user's name.

    Returns
    -------
    PrincipalRef
        The user.
    """
    row = Principal(id=uuid.uuid4(), type=PrincipalType.USER, name=name)
    session.add(row)
    session.commit()
    return PrincipalRef(id=row.id, name=row.name, type=row.type)


def _row(session: Session, ref: TableRef = _ORDERS) -> Table:
    """Read a table's row fresh from the database.

    Parameters
    ----------
    session
        Database session.
    ref
        The table, in `gold`.

    Returns
    -------
    Table
        The row.
    """
    session.rollback()
    return session.scalars(
        select(Table).where(_named(ref)).execution_options(populate_existing=True)
    ).one()


def _named(ref: TableRef) -> ColumnElement[bool]:
    """Select the row of one table.

    Parameters
    ----------
    ref
        The table.

    Returns
    -------
    ColumnElement
        A where clause.
    """
    namespaces = (
        select(Namespace.id)
        .join(Environment, Namespace.environment_id == Environment.id)
        .where(Environment.name == ref.environment, Namespace.path == ref.namespace.joined())
    )
    return (Table.name == ref.name) & Table.namespace_id.in_(namespaces)


def _age(session: Session, days: float, ref: TableRef = _ORDERS) -> datetime:
    """Move a table's creation and last change `days` into the past.

    Parameters
    ----------
    session
        Database session; commits.
    days
        How far back.
    ref
        The table.

    Returns
    -------
    datetime
        The new time of its last change.
    """
    moment = datetime.now(UTC) - timedelta(days=days)
    session.execute(
        update(Table)
        .where(_named(ref))
        .values(created_at=moment, updated_at=moment, retention_set_at=None)
    )
    session.commit()
    return moment


def _read_at(session: Session, moment: datetime, principal: PrincipalRef) -> None:
    """Record a read of `gold.orders` at a time.

    Parameters
    ----------
    session
        Database session; commits.
    moment
        When the read completed.
    principal
        Who read it.
    """
    session.add(
        TableRead(
            id=uuid.uuid4(),
            table_id=_row(session).id,
            principal_id=principal.id,
            day=moment.date(),
            read_count=1,
            last_read_at=moment,
        )
    )
    session.commit()


def _events(session: Session, kind: AuditEventType) -> list[AuditEvent]:
    """Read the audit events of one kind, oldest first.

    Parameters
    ----------
    session
        Database session.
    kind
        The event type.

    Returns
    -------
    list of AuditEvent
        The events.
    """
    found = list(
        session.scalars(
            select(AuditEvent)
            .where(AuditEvent.event_type == kind)
            .order_by(AuditEvent.occurred_at, AuditEvent.id)
        )
    )
    session.rollback()
    return found


def _sweep(session: Session, now: datetime | None = None, **options: object) -> LocationSweepReport:
    """Run the location sweep once.

    Parameters
    ----------
    session
        Database session.
    now
        The sweep's time; the present when `None`.
    **options
        Passed through.

    Returns
    -------
    LocationSweepReport
        What it did.
    """
    return sweep_table_locations(
        session,
        StorageResolver(session),
        now=now or datetime.now(UTC),
        grace_hours=24,
        **options,  # ty: ignore[invalid-argument-type]
    )


def _close(left: datetime | None, right: datetime, seconds: float = 5) -> bool:
    """Say whether two times are within a few seconds of each other.

    Parameters
    ----------
    left
        One time.
    right
        The other.
    seconds
        The tolerance.

    Returns
    -------
    bool
        `True` if they are.
    """
    return left is not None and abs((left - right).total_seconds()) <= seconds


def _deploy(session: Session, principal: PrincipalRef, *assets: str) -> None:
    """Make a project version that declares `assets` the current one of `dev`.

    Parameters
    ----------
    session
        Database session; commits.
    principal
        Who deployed it.
    *assets
        The dotted names of its assets.
    """
    env_id = session.scalars(select(Environment.id).where(Environment.name == "dev")).one()
    repo = Repository(
        id=uuid.uuid4(),
        name=f"r{uuid.uuid4().hex[:6]}",
        kind=RepositoryKind.HTTPS,
        url="https://h/r",
    )
    session.add(repo)
    session.flush()
    version = JobVersion(
        id=uuid.uuid4(),
        repository_id=repo.id,
        environment_id=env_id,
        commit_sha="a" * 40,
        manifest_json={"version": 3, "assets": [{"table": a, "sql": ""} for a in assets]},
        code_uri="file:///x",
        code_sha256="0" * 64,
        deployed_by=principal.id,
    )
    session.add(version)
    session.flush()
    session.add(
        EnvironmentDeployment(
            environment_id=env_id, repository_id=repo.id, current_job_version_id=version.id
        )
    )
    session.commit()


# --- the clock ---------------------------------------------------------------------------------


def test_the_expiry_is_the_last_activity_plus_the_days_or_the_fixed_day() -> None:
    """Days count from the later of the activity and the floor; a fixed day is its midnight UTC."""
    activity = datetime(2026, 10, 1, 8, tzinfo=UTC)
    assert expiry_time(days=30, on=None, activity=activity, floor=None) == activity + timedelta(30)
    later = datetime(2026, 10, 5, tzinfo=UTC)
    assert expiry_time(days=30, on=None, activity=activity, floor=later) == later + timedelta(30)
    assert expiry_time(days=None, on=date(2026, 11, 2), activity=activity, floor=later) == (
        datetime(2026, 11, 2, tzinfo=UTC)
    )
    assert expiry_time(days=None, on=None, activity=activity, floor=None) is None


def test_a_read_pushes_the_expiry_later_and_never_earlier(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """The clock is the later of the last change and the last recorded read."""
    catalog.set_table_retention(principal, _ORDERS, days=30)
    written = _age(session, 20)
    assert _close(catalog.table_retention(principal, _ORDERS).expires_at, written + timedelta(30))

    _read_at(session, written - timedelta(days=5), principal)
    assert _close(catalog.table_retention(principal, _ORDERS).expires_at, written + timedelta(30))

    read = datetime.now(UTC) - timedelta(days=2)
    _read_at(session, read, principal)
    shown = catalog.table_retention(principal, _ORDERS)
    assert _close(shown.last_activity_at, read)
    assert _close(shown.expires_at, read + timedelta(30))


def test_a_write_moves_the_clock(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """A commit changes the table's row, which is its last activity."""
    catalog.set_table_retention(principal, _ORDERS, days=30)
    _age(session, 20)
    catalog.append(principal, _ORDERS, _rows())
    shown = catalog.table_retention(principal, _ORDERS)
    assert _close(shown.expires_at, datetime.now(UTC) + timedelta(30), seconds=30)


def _rows() -> object:
    """Build one row for `gold.orders`.

    Returns
    -------
    object
        A pyarrow table.
    """
    import pyarrow as pa

    return pa.table({"id": pa.array([1], pa.int64())})


# --- table retention: rights --------------------------------------------------------------------


def test_setting_a_retention_records_it_and_who_set_it(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """`--days` stores the rule, the setter and the time, and audits `TABLE_RETENTION_SET`."""
    shown = catalog.set_table_retention(principal, _ORDERS, days=30)
    assert isinstance(shown, TableRetention)
    assert (shown.source, shown.days, shown.on, shown.never) == ("table", 30, None, False)
    assert shown.set_by == principal.name
    assert _close(shown.expires_at, datetime.now(UTC) + timedelta(30))
    row = _row(session)
    assert (row.retention_days, row.retention_set_by) == (30, principal.id)
    (event,) = _events(session, AuditEventType.TABLE_RETENTION_SET)
    assert event.principal_id == principal.id
    assert event.detail["days"] == 30
    assert event.detail["shortened"] is True
    assert event.securable_table == "orders"


def test_a_writer_who_is_not_the_owner_cannot_set_or_shorten_a_retention(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """Setting one where none applies, or an earlier expiry, needs `DROP_TABLE`."""
    writer = _user(session, "writer")
    authz = ActionAllowList(Action.READ_TABLE, Action.WRITE_TABLE)
    as_writer = _catalog(session, authz)
    with pytest.raises(PermissionDenied):
        as_writer.set_table_retention(writer, _ORDERS, days=30)
    assert (Action.DROP_TABLE in {a for a, _ in authz.seen}) is True

    catalog.set_table_retention(principal, _ORDERS, days=30)
    with pytest.raises(PermissionDenied):
        as_writer.set_table_retention(writer, _ORDERS, days=5)
    with pytest.raises(PermissionDenied):
        as_writer.set_table_retention(
            writer, _ORDERS, on=datetime.now(UTC).date() + timedelta(days=3)
        )
    assert _row(session).retention_days == 30
    assert len(_events(session, AuditEventType.TABLE_RETENTION_SET)) == 1


def test_a_writer_may_extend_or_clear_a_retention(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """A later expiry, or none (`--never`), needs only `WRITE_TABLE`."""
    catalog.set_table_retention(principal, _ORDERS, days=5)
    writer = _user(session, "writer")
    as_writer = _catalog(session, ActionAllowList(Action.READ_TABLE, Action.WRITE_TABLE))

    extended = as_writer.set_table_retention(writer, _ORDERS, days=60)
    assert (extended.days, extended.set_by) == (60, "writer")
    event = _events(session, AuditEventType.TABLE_RETENTION_SET)[-1]
    assert event.detail["shortened"] is False

    cleared = as_writer.set_table_retention(writer, _ORDERS, never=True)
    assert (cleared.never, cleared.expires_at) == (True, None)
    (event,) = _events(session, AuditEventType.TABLE_RETENTION_CLEARED)
    assert event.principal_id == writer.id


def test_someone_who_may_not_write_cannot_even_extend(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """Without `WRITE_TABLE` an extension is refused too."""
    catalog.set_table_retention(principal, _ORDERS, days=5)
    reader = _user(session, "reader")
    with pytest.raises(PermissionDenied):
        _catalog(session, ActionAllowList(Action.READ_TABLE)).set_table_retention(
            reader, _ORDERS, never=True
        )


def test_the_owner_may_shorten_without_drop_rights(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """The table's owner may set and shorten it like a principal with `DROP_TABLE`."""
    authz = ActionAllowList(Action.READ_TABLE)
    shown = _catalog(session, authz).set_table_retention(principal, _ORDERS, days=2)
    assert shown.days == 2
    assert Action.DROP_TABLE not in {a for a, _ in authz.seen}


def test_exactly_one_rule_is_given(catalog: CatalogService, principal: PrincipalRef) -> None:
    """None, or two of `days`, `on` and `never`, is refused with the options named."""
    with pytest.raises(ValidationError, match="--days"):
        catalog.set_table_retention(principal, _ORDERS)
    with pytest.raises(ValidationError) as caught:
        catalog.set_table_retention(principal, _ORDERS, days=3, never=True)
    assert str(caught.value) == ONE_RULE_MESSAGE


@pytest.mark.parametrize("days", [0, -1, 3651])
def test_days_out_of_range_are_refused(
    catalog: CatalogService, principal: PrincipalRef, days: int
) -> None:
    """1 to 3,650 days."""
    with pytest.raises(ValidationError, match="3650"):
        catalog.set_table_retention(principal, _ORDERS, days=days)


def test_a_day_in_the_past_is_refused(catalog: CatalogService, principal: PrincipalRef) -> None:
    """`--on` must be later than today; to drop a table now, drop it."""
    with pytest.raises(ValidationError, match="lakana table delete"):
        catalog.set_table_retention(principal, _ORDERS, on=datetime.now(UTC).date())


def test_a_missing_table_is_not_found_for_a_writer_and_refused_otherwise(
    session: Session, catalog: CatalogService
) -> None:
    """The answer does not tell someone without write rights what exists."""
    missing = TableRef("dev", _GOLD, "missing")
    writer = _user(session, "writer")
    with pytest.raises(NotFoundError):
        _catalog(session, ActionAllowList(Action.WRITE_TABLE)).set_table_retention(
            writer, missing, days=3
        )
    with pytest.raises(PermissionDenied):
        _catalog(session, ActionAllowList()).set_table_retention(writer, missing, days=3)


def test_views_and_external_tables_have_no_retention(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """Retention applies to managed tables; a view is refused with the reason."""
    catalog.create_or_replace_view(
        principal,
        TableRef("dev", _GOLD, "v"),
        sql="SELECT 1 AS id",
        references=[],
        columns=[ColumnSpec(name="id", type="bigint", nullable=True)],
    )
    with pytest.raises(ValidationError) as caught:
        catalog.set_table_retention(principal, TableRef("dev", _GOLD, "v"), days=3)
    assert str(caught.value) == NOT_MANAGED_MESSAGE.format(table="gold.v")
    with pytest.raises(ValidationError):
        catalog.table_retention(principal, TableRef("dev", _GOLD, "v"))
    assert catalog.describe_table(principal, TableRef("dev", _GOLD, "v")).expires_at is None


def test_a_protected_environment_refuses_a_retention_but_allows_never(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """Tables there never expire; `--never` still clears one set before the protection."""
    catalog.set_table_retention(principal, _ORDERS, days=3)
    catalog.set_environment_protection(principal, "dev", True)
    with pytest.raises(ValidationError) as caught:
        catalog.set_table_retention(principal, _ORDERS, days=30)
    assert str(caught.value) == PROTECTED_TABLE_MESSAGE.format(environment="dev")
    shown = catalog.table_retention(principal, _ORDERS)
    assert (shown.expires_at, shown.kept) == (None, "protected")
    assert catalog.set_table_retention(principal, _ORDERS, never=True).never is True


def test_a_deployed_asset_shows_that_it_never_expires(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """A table the current project version declares is kept, whatever its retention says."""
    catalog.set_table_retention(principal, _ORDERS, days=3)
    _deploy(session, principal, "gold.orders")
    shown = catalog.table_retention(principal, _ORDERS)
    assert (shown.days, shown.expires_at, shown.kept) == (3, None, "project_asset")


# --- environment retention ------------------------------------------------------------------------


def test_an_environment_retention_applies_to_tables_that_set_none(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """The default counts from the later of the table's activity and when it was set."""
    _age(session, 100)
    env = catalog.set_environment_retention(principal, "dev", 30)
    assert (env.retention_days, env.retention_set_by) == (30, principal.id)
    shown = catalog.table_retention(principal, _ORDERS)
    assert (shown.source, shown.days, shown.set_by) == ("environment", 30, principal.name)
    assert _close(shown.expires_at, datetime.now(UTC) + timedelta(30))
    (event,) = _events(session, AuditEventType.ENVIRONMENT_RETENTION_SET)
    assert event.detail == {"days": 30, "previous_days": None}


def test_a_table_setting_overrides_the_environment(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """A table's own days, date or `never` win over the environment's retention."""
    catalog.set_environment_retention(principal, "dev", 30)
    assert catalog.set_table_retention(principal, _ORDERS, days=90).source == "table"
    kept = catalog.set_table_retention(principal, _ORDERS, never=True)
    assert (kept.source, kept.expires_at) == ("table", None)


def test_turning_an_environment_retention_off(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """`None` turns it off; the tables then have no retention."""
    catalog.set_environment_retention(principal, "dev", 30)
    catalog.set_environment_retention(principal, "dev", None)
    shown = catalog.table_retention(principal, _ORDERS)
    assert (shown.source, shown.expires_at) == (None, None)
    assert _events(session, AuditEventType.ENVIRONMENT_RETENTION_SET)[-1].detail == {
        "days": None,
        "previous_days": 30,
    }


def test_an_environment_retention_needs_manage_environment(
    session: Session, catalog: CatalogService
) -> None:
    """Without `MANAGE_ENVIRONMENT` on the environment it is refused."""
    someone = _user(session, "someone")
    authz = ActionAllowList(Action.DROP_TABLE)
    with pytest.raises(PermissionDenied):
        _catalog(session, authz).set_environment_retention(someone, "dev", 30)
    assert authz.seen[0][0] is Action.MANAGE_ENVIRONMENT


@pytest.mark.parametrize("days", [0, 3651])
def test_environment_days_out_of_range_are_refused(
    catalog: CatalogService, principal: PrincipalRef, days: int
) -> None:
    """1 to 3,650 days."""
    with pytest.raises(ValidationError, match="3650"):
        catalog.set_environment_retention(principal, "dev", days)


def test_a_protected_environment_refuses_a_retention(
    catalog: CatalogService, principal: PrincipalRef
) -> None:
    """Tables there never expire, so a default would mislead; turning it off is allowed."""
    catalog.set_environment_retention(principal, "dev", 30)
    catalog.set_environment_protection(principal, "dev", True)
    with pytest.raises(ValidationError) as caught:
        catalog.set_environment_retention(principal, "dev", 7)
    assert str(caught.value) == PROTECTED_ENVIRONMENT_MESSAGE.format(environment="dev")
    assert catalog.set_environment_retention(principal, "dev", None).retention_days is None


# --- warnings and the expiring listing ---------------------------------------------------------


def test_describe_says_when_a_table_expires_soon(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """Within the warning window `expires_soon` is set; further out only `expires_at`."""
    catalog.set_table_retention(principal, _ORDERS, days=30)
    far = catalog.describe_table(principal, _ORDERS)
    assert _close(far.expires_at, datetime.now(UTC) + timedelta(30))
    assert far.expires_soon is False
    catalog.set_table_retention(principal, _ORDERS, days=3)
    near = _catalog(session, expiry_warning_days=7).describe_table(principal, _ORDERS)
    assert near.expires_soon is True
    nothing = TableRef("dev", _GOLD, "other")
    catalog.create_table(principal, nothing, schema=_schema())
    plain = catalog.describe_table(principal, nothing)
    assert (plain.expires_at, plain.expires_soon) == (None, False)


def test_a_warning_window_of_zero_turns_the_warning_off_even_for_a_due_table(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """`LAKANA_TABLES__EXPIRY_WARNING_DAYS=0` never sets `expires_soon`, not even when due."""
    _due(session, catalog, principal)
    due = _catalog(session, expiry_warning_days=0).describe_table(principal, _ORDERS)
    assert due.expires_at is not None and due.expires_at <= datetime.now(UTC)
    assert due.expires_soon is False
    assert _catalog(session, expiry_warning_days=1).describe_table(principal, _ORDERS).expires_soon


def test_the_expiring_listing_names_what_is_due_and_who_set_it(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """Soonest first, within the window, with the rule's source; kept tables are left out."""
    for name in ("a", "b", "c", "kept"):
        catalog.create_table(principal, TableRef("dev", _GOLD, name), schema=_schema())
    catalog.set_table_retention(principal, TableRef("dev", _GOLD, "a"), days=5)
    catalog.set_table_retention(principal, TableRef("dev", _GOLD, "b"), days=2)
    catalog.set_table_retention(principal, TableRef("dev", _GOLD, "c"), days=60)
    catalog.set_table_retention(principal, TableRef("dev", _GOLD, "kept"), days=1)
    _deploy(session, principal, "gold.kept")

    listed = catalog.list_expiring_tables(principal, "dev", within_days=7)
    assert [entry.ref.name for entry in listed] == ["b", "a"]
    assert listed[0].set_by == principal.name
    assert listed[0].source == "table"


def test_the_expiring_listing_leaves_out_what_the_caller_may_not_read(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """A table the caller may not read is not listed."""
    catalog.set_table_retention(principal, _ORDERS, days=2)
    reader = _user(session, "reader")
    assert (
        _catalog(session, ActionAllowList(Action.READ_ENVIRONMENT)).list_expiring_tables(
            reader, "dev", within_days=7
        )
        == []
    )


def test_the_expiring_listing_counts_the_environment_default(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """Tables that set nothing appear once the default makes them due within the window."""
    catalog.set_environment_retention(principal, "dev", 3)
    (entry,) = catalog.list_expiring_tables(principal, "dev", within_days=7)
    assert (entry.ref, entry.source, entry.days) == (_ORDERS, "environment", 3)
    assert catalog.list_expiring_tables(principal, "dev", within_days=1) == []


def test_the_expiring_window_is_bounded(catalog: CatalogService, principal: PrincipalRef) -> None:
    """1 to 3,650 days."""
    with pytest.raises(ValidationError, match="3650"):
        catalog.list_expiring_tables(principal, "dev", within_days=0)


# --- the sweep ------------------------------------------------------------------------------------


def _due(session: Session, catalog: CatalogService, principal: PrincipalRef) -> uuid.UUID:
    """Give `gold.orders` a 30-day retention whose time has passed.

    Parameters
    ----------
    session
        Database session.
    catalog
        The catalog.
    principal
        The acting principal.

    Returns
    -------
    uuid.UUID
        The table's id.
    """
    catalog.set_table_retention(principal, _ORDERS, days=30)
    _age(session, 40)
    return _row(session).id


def test_the_sweep_drops_a_due_table_as_an_expiry_that_undrop_restores(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """A tombstone with the undo period, `reason: retention`, no principal; undrop restores it."""
    table_id = _due(session, catalog, principal)
    report = _sweep(session, undo_days=7)
    assert report.tables_expired == 1
    assert session.scalars(select(Table).where(Table.id == table_id)).one_or_none() is None
    record = session.scalars(select(TableLocation).where(TableLocation.table_id == table_id)).one()
    assert record.state is TableLocationState.DROPPED
    assert record.purge_after is not None
    assert _close(record.purge_after, datetime.now(UTC) + timedelta(days=7), seconds=30)
    session.rollback()
    (event,) = _events(session, AuditEventType.TABLE_DROPPED)
    assert event.principal_id is None
    assert event.detail["reason"] == "retention"
    assert event.detail["retention"]["days"] == 30
    assert event.detail["retention"]["source"] == "table"

    restored = catalog.undrop_table(principal, _ORDERS)
    assert restored.uuid == table_id
    again = catalog.table_retention(principal, _ORDERS)
    assert _close(again.expires_at, datetime.now(UTC) + timedelta(30), seconds=30)
    assert _sweep(session).tables_expired == 0


def test_an_undrop_removes_a_fixed_day_that_has_passed(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """Otherwise the next sweep would drop the restored table again."""
    catalog.set_table_retention(principal, _ORDERS, on=datetime.now(UTC).date() + timedelta(days=2))
    session.execute(update(Table).values(retention_on=datetime.now(UTC).date() - timedelta(days=1)))
    session.commit()
    assert _sweep(session).tables_expired == 1
    catalog.undrop_table(principal, _ORDERS)
    shown = catalog.table_retention(principal, _ORDERS)
    assert (shown.on, shown.source, shown.expires_at) == (None, None, None)
    (event,) = _events(session, AuditEventType.TABLE_UNDROPPED)
    assert event.detail["retention_cleared"] is True


def test_a_fixed_day_expires_the_table_from_that_day_on(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """Not before midnight UTC of the day, whatever the activity; on the day it goes."""
    day = datetime.now(UTC).date() + timedelta(days=2)
    catalog.set_table_retention(principal, _ORDERS, on=day)
    midnight = datetime(day.year, day.month, day.day, tzinfo=UTC)
    assert _sweep(session, now=midnight - timedelta(seconds=1)).tables_expired == 0
    assert _sweep(session, now=midnight).tables_expired == 1


def test_a_table_that_is_not_due_is_left_alone(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """Twenty days into a thirty-day retention nothing happens."""
    catalog.set_table_retention(principal, _ORDERS, days=30)
    _age(session, 20)
    assert _sweep(session).tables_expired == 0
    assert _row(session).name == "orders"


def test_the_environment_default_expires_tables_that_set_none(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """Counted from when it was set, so turning it on gives every table its full period."""
    _age(session, 100)
    catalog.set_environment_retention(principal, "dev", 30)
    assert _sweep(session).tables_expired == 0
    assert _sweep(session, now=datetime.now(UTC) + timedelta(days=31)).tables_expired == 1


def test_a_table_that_says_never_outlives_the_environment_default(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """`never` keeps the table."""
    catalog.set_table_retention(principal, _ORDERS, never=True)
    catalog.set_environment_retention(principal, "dev", 30)
    assert _sweep(session, now=datetime.now(UTC) + timedelta(days=400)).tables_expired == 0


def test_a_protected_environment_keeps_a_table_whose_retention_was_set_before(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """Protection wins over a retention set before it."""
    _due(session, catalog, principal)
    catalog.set_environment_protection(principal, "dev", True)
    assert _sweep(session).tables_expired == 0
    assert _row(session).name == "orders"


def test_a_deployed_asset_is_never_expired(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """A table the current project version declares stays."""
    _due(session, catalog, principal)
    _deploy(session, principal, "gold.orders")
    assert _sweep(session).tables_expired == 0
    assert _row(session).name == "orders"


@pytest.mark.parametrize("status", [QueryRunStatus.QUEUED, QueryRunStatus.RUNNING])
@pytest.mark.slow
def test_a_table_with_a_query_writing_it_is_postponed(
    session: Session, catalog: CatalogService, principal: PrincipalRef, status: QueryRunStatus
) -> None:
    """A queued or running write (by id or by name) keeps the table for this sweep."""
    table_id = _due(session, catalog, principal)
    env_id = session.scalars(select(Environment.id)).one()
    run = QueryRun(
        id=uuid.uuid4(),
        environment_id=env_id,
        sql_text="SELECT 1",
        status=status,
        write_mode=WriteMode.APPEND,
        target_name="gold.orders",
    )
    session.add(run)
    session.commit()
    report = _sweep(session)
    assert (report.tables_expired, report.expiries_postponed) == (0, 1)
    session.execute(
        update(QueryRun)
        .where(QueryRun.id == run.id)
        .values(target_name=None, target_table_id=table_id)
    )
    session.commit()
    assert _sweep(session).expiries_postponed == 1
    session.execute(
        update(QueryRun).where(QueryRun.id == run.id).values(status=QueryRunStatus.SUCCEEDED)
    )
    session.commit()
    assert _sweep(session).tables_expired == 1


def test_a_table_whose_task_is_running_is_postponed(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """A pipeline task that builds the table and has not ended keeps it."""
    _due(session, catalog, principal)
    task = python_task_run(session, principal.id, asset="gold.orders", status=TaskStatus.RUNNING)
    assert _sweep(session).expiries_postponed == 1
    task.status = TaskStatus.SUCCEEDED
    session.commit()
    assert _sweep(session).tables_expired == 1


def test_a_table_whose_location_is_not_its_own_live_record_is_kept(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """Without a tombstone an expiry could not be undone, so it does not happen."""
    _due(session, catalog, principal)
    session.execute(update(TableLocation).values(table_id=uuid.uuid4()))
    session.commit()
    report = _sweep(session)
    assert (report.tables_expired, report.expiries_postponed) == (0, 1)
    session.execute(update(Table).values(metadata_location=None, updated_at=Table.updated_at))
    session.commit()
    assert _sweep(session).expiries_postponed == 1


@pytest.mark.parametrize("kind", [TableType.EXTERNAL, TableType.VIEW])
def test_external_tables_and_views_are_never_expired(
    session: Session, catalog: CatalogService, principal: PrincipalRef, kind: TableType
) -> None:
    """Even a row that carries a retention (written behind the service's back) stays."""
    _due(session, catalog, principal)
    session.execute(update(Table).values(table_type=kind, updated_at=Table.updated_at))
    session.commit()
    assert _sweep(session).tables_expired == 0
    assert _row(session).name == "orders"


def test_the_sweep_expires_at_most_its_cap_per_run(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """The rest waits for the next run; the most overdue goes first."""
    second = TableRef("dev", _GOLD, "second")
    catalog.create_table(principal, second, schema=_schema())
    catalog.set_table_retention(principal, _ORDERS, days=30)
    catalog.set_table_retention(principal, second, days=30)
    _age(session, 40)
    _age(session, 50, second)
    first = _sweep(session, max_expiries=1)
    assert first.tables_expired == 1
    assert session.scalars(select(Table.name)).all() == ["orders"]
    session.rollback()
    assert _sweep(session, max_expiries=1).tables_expired == 1


def test_an_undo_period_of_zero_still_keeps_an_expired_table_for_a_day(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """Nobody chose to drop it, so it can always be restored for at least a day."""
    table_id = _due(session, catalog, principal)
    _sweep(session, undo_days=0)
    record = session.scalars(select(TableLocation).where(TableLocation.table_id == table_id)).one()
    assert record.state is TableLocationState.DROPPED
    assert _close(record.purge_after, datetime.now(UTC) + timedelta(days=1), seconds=30)


def test_the_deadline_stops_the_expiries(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """A run past its time drops nothing more."""
    _due(session, catalog, principal)
    ticks = iter([0.0, 0.0, 100.0, 100.0, 100.0, 100.0, 100.0])
    report = _sweep(session, max_seconds=10, monotonic=lambda: next(ticks, 100.0))
    assert report.tables_expired == 0
    assert report.stopped_early is True


def test_a_table_extended_after_the_listing_is_left_alone(
    session: Session,
    catalog: CatalogService,
    principal: PrincipalRef,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The decision is taken again under the table's lock, with what is stored then."""
    _due(session, catalog, principal)
    real = retention_module.expire_table

    def extend_first(*args: object, **kwargs: object) -> object:
        session.execute(update(Table).values(retention_days=None, retention_never=True))
        session.commit()
        return real(*args, **kwargs)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(retention_module, "expire_table", extend_first)
    assert _sweep(session).tables_expired == 0
    assert _row(session).name == "orders"


def test_a_table_dropped_after_the_listing_is_skipped(
    session: Session,
    catalog: CatalogService,
    principal: PrincipalRef,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A row gone by the time of the lock is nothing to do."""
    _due(session, catalog, principal)
    real = retention_module.expire_table

    def drop_first(*args: object, **kwargs: object) -> object:
        catalog.drop_table(principal, _ORDERS)
        return real(*args, **kwargs)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(retention_module, "expire_table", drop_first)
    report = _sweep(session)
    assert report.tables_expired == 0
    assert len(_events(session, AuditEventType.TABLE_DROPPED)) == 1


def test_namespaces_of_other_environments_are_not_mixed_up(
    session: Session, catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A due table in one environment does not make a namesake in another due."""
    other = tmp_path / "other"
    other.mkdir()
    catalog.create_environment(
        principal,
        name="test",
        storage=StorageConfig(scheme="file", root=f"file://{other}", options={}),
    )
    namesake = TableRef("test", _GOLD, "orders")
    catalog.create_table(principal, namesake, schema=_schema())
    catalog.set_table_retention(principal, namesake, days=30)
    _due(session, catalog, principal)
    assert _sweep(session).tables_expired == 1
    assert _row(session, namesake).name == "orders"
