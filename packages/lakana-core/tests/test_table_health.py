"""A table's health: its snapshot's numbers, last maintenance, checks and readers by use."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pyarrow as pa
import pytest
from core_test_helpers import ActionAllowList, _prepared_catalog, python_task_run
from pyiceberg.schema import Schema
from pyiceberg.types import LongType, NestedField, StringType
from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.auth import Action, AllowAllAuthorization, PrincipalRef, SecurableKind
from lakana_core.catalog import health
from lakana_core.catalog.health import (
    last_maintenance,
    latest_checks,
    readers,
    record_table_reads,
    snapshot_stats,
    sweep_table_reads,
)
from lakana_core.catalog.service import CatalogService
from lakana_core.errors import PermissionDenied
from lakana_core.grants.roles import Role
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.models import (
    CheckOutcome,
    CheckResult,
    Environment,
    Grant,
    Principal,
    PrincipalType,
    QueryRun,
    QueryRunStatus,
    RollbackOutcome,
    Table,
    TableRead,
    WriteMode,
)
from lakana_core.storage.resolver import StorageResolver

_SILVER = NamespacePath.from_parts(["silver"])
_ORDERS = TableRef("dev", _SILVER, "orders")
_SCHEMA = Schema(
    NestedField(1, "id", LongType(), required=True),
    NestedField(2, "status", StringType(), required=False),
)
_NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
_ARROW = pa.schema([pa.field("id", pa.int64(), nullable=False), pa.field("status", pa.string())])


def _data(ids: list[int]) -> pa.Table:
    """Build rows that fit `silver.orders`.

    Parameters
    ----------
    ids
        The ids; the status of each is ``"s"``.

    Returns
    -------
    pa.Table
        The rows.
    """
    return pa.table({"id": ids, "status": ["s"] * len(ids)}, schema=_ARROW)


def _person(session: Session, name: str) -> PrincipalRef:
    """Persist a principal.

    Parameters
    ----------
    session
        Database session.
    name
        Its name.

    Returns
    -------
    PrincipalRef
        A reference to the row.
    """
    ref = PrincipalRef(id=uuid.uuid4(), name=name, type=PrincipalType.USER)
    session.add(Principal(id=ref.id, type=ref.type, name=name, is_active=True))
    session.commit()
    return ref


@pytest.fixture
def catalog_with_orders(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> CatalogService:
    """Provide an allow-all catalog with `dev` and the empty table `silver.orders`.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        Storage root.
    principal
        The creator.

    Returns
    -------
    CatalogService
        The catalog.
    """
    catalog = _prepared_catalog(session, tmp_path, AllowAllAuthorization(), namespaces=[_SILVER])
    catalog.create_table(principal, _ORDERS, schema=_SCHEMA)
    return catalog


def _orders_id(session: Session) -> uuid.UUID:
    """Look up the id of `silver.orders`.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    uuid.UUID
        The table's id.
    """
    return session.scalars(select(Table.id).where(Table.name == "orders")).one()


def _maintenance(
    session: Session,
    principal: PrincipalRef,
    *,
    finished_at: datetime,
    status: QueryRunStatus = QueryRunStatus.SUCCEEDED,
    report: dict | None = None,
    dry_run: bool = False,
    table_id: uuid.UUID | None = None,
    write_mode: WriteMode = WriteMode.MAINTAIN,
) -> QueryRun:
    """Persist a finished maintenance run of `silver.orders`.

    Parameters
    ----------
    session
        Database session; commits.
    principal
        The submitter.
    finished_at
        When it ended.
    status
        Its status.
    report
        The stored report; a complete one that removed 2 snapshots and 3 files when `None`.
    dry_run
        Whether the request asked for a dry run.
    table_id
        The target; `silver.orders` when `None`.
    write_mode
        The run's mode.

    Returns
    -------
    QueryRun
        The run.
    """
    environment = session.scalars(select(Environment)).one()
    stored = {
        "dry_run": dry_run,
        "snapshots_expired": [1, 2],
        "files_deleted": 3,
        "bytes_deleted": 4096,
        "note": None,
        "conflict": False,
        "tags_removed": ["failed-a"],
    }
    run = QueryRun(
        id=uuid.uuid4(),
        environment_id=environment.id,
        principal_id=principal.id,
        sql_text="-- maintain silver.orders",
        status=status,
        write_mode=write_mode,
        target_name="silver.orders",
        target_table_id=table_id or _orders_id(session),
        maintenance={"request": {"dry_run": dry_run}, "report": report or stored},
        finished_at=finished_at,
    )
    session.add(run)
    session.commit()
    return run


def test_snapshot_stats_read_the_current_snapshots_summary(
    session: Session, principal: PrincipalRef, catalog_with_orders: CatalogService
) -> None:
    empty = catalog_with_orders.load_table(principal, _ORDERS)
    assert snapshot_stats(empty.metadata) == health.SnapshotStats(None, None, None, None)

    catalog_with_orders.append(principal, _ORDERS, _data([1, 2, 3]))
    stats = snapshot_stats(catalog_with_orders.load_table(principal, _ORDERS).metadata)
    assert stats.row_count == 3
    assert stats.file_count == 1
    assert stats.size_bytes is not None and stats.size_bytes > 0
    assert stats.last_write_at is not None
    assert stats.last_write_at.tzinfo is not None
    assert abs(stats.last_write_at - datetime.now(UTC)) < timedelta(minutes=5)


@pytest.mark.parametrize("value", [None, "", "abc", "-5", "1.5"])
def test_a_summary_value_that_is_not_a_whole_number_reads_as_missing(value: str | None) -> None:
    summary = {} if value is None else {"total-records": value}
    snapshot = SimpleNamespace(summary=SimpleNamespace(get=summary.get), timestamp_ms=1)
    metadata = SimpleNamespace(snapshot_by_id=lambda _: snapshot, current_snapshot_id=7)
    assert snapshot_stats(metadata).row_count is None  # ty: ignore[invalid-argument-type]


def test_a_snapshot_without_a_summary_has_a_time_and_no_numbers() -> None:
    snapshot = SimpleNamespace(summary=None, timestamp_ms=1_700_000_000_000)
    metadata = SimpleNamespace(snapshot_by_id=lambda _: snapshot, current_snapshot_id=7)
    stats = snapshot_stats(metadata)  # ty: ignore[invalid-argument-type]
    assert stats.last_write_at == datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC)
    assert (stats.row_count, stats.size_bytes, stats.file_count) == (None, None, None)


def test_the_last_maintenance_is_the_newest_complete_run_that_changed_something(
    session: Session, principal: PrincipalRef, catalog_with_orders: CatalogService
) -> None:
    table_id = _orders_id(session)
    assert last_maintenance(session, table_id) is None
    old = _maintenance(session, principal, finished_at=_NOW - timedelta(days=3))
    assert last_maintenance(session, table_id) is not None

    # Not eligible, though newer: a dry run, a failed run, a run that hit a conflict, a run of
    # another mode, one that is still running, one of another table.
    _maintenance(session, principal, finished_at=_NOW, dry_run=True)
    _maintenance(session, principal, finished_at=_NOW, status=QueryRunStatus.FAILED)
    _maintenance(
        session,
        principal,
        finished_at=_NOW,
        report={"dry_run": False, "conflict": True, "snapshots_expired": []},
    )
    _maintenance(session, principal, finished_at=_NOW, write_mode=WriteMode.APPEND)
    _maintenance(session, principal, finished_at=_NOW, table_id=uuid.uuid4())
    session.add(
        QueryRun(
            id=uuid.uuid4(),
            environment_id=old.environment_id,
            principal_id=principal.id,
            sql_text="-- maintain silver.orders",
            status=QueryRunStatus.RUNNING,
            write_mode=WriteMode.MAINTAIN,
            target_table_id=table_id,
            maintenance={"request": {}},
        )
    )
    session.commit()

    found = last_maintenance(session, table_id)
    assert found is not None
    assert found.finished_at == old.finished_at
    assert found.run_id == old.id
    assert (found.snapshots_expired, found.files_deleted, found.bytes_deleted) == (2, 3, 4096)
    assert found.tags_removed == 1
    assert found.status == "complete"

    newest = _maintenance(session, principal, finished_at=_NOW - timedelta(days=1))
    again = last_maintenance(session, table_id)
    assert again is not None and again.run_id == newest.id


def test_a_maintenance_run_that_stopped_at_its_time_limit_reads_as_stopped(
    session: Session, principal: PrincipalRef, catalog_with_orders: CatalogService
) -> None:
    _maintenance(
        session,
        principal,
        finished_at=_NOW,
        status=QueryRunStatus.FAILED,
        report={"dry_run": False, "stopped": True, "snapshots_expired": [5], "files_deleted": 1},
    )
    found = last_maintenance(session, _orders_id(session))
    assert found is not None
    assert (found.status, found.snapshots_expired, found.files_deleted) == ("stopped", 1, 1)
    assert (found.bytes_deleted, found.tags_removed) == (0, 0)


def _check(
    session: Session,
    principal: PrincipalRef,
    name: str,
    *,
    outcome: CheckOutcome,
    finished_at: datetime,
    on_failure: str = "block",
    rollback: RollbackOutcome | None = None,
    table_id: uuid.UUID | None = None,
    generated: bool = False,
) -> CheckResult:
    """Persist one check result of `silver.orders` on a task run of its own.

    Parameters
    ----------
    session
        Database session; commits.
    principal
        The run principal.
    name
        The check's short name.
    outcome
        How it ended.
    finished_at
        When it ended.
    on_failure
        ``block``, ``keep`` or ``warn``.
    rollback
        What the rollback did.
    table_id
        The checked table; `silver.orders` when `None`.
    generated
        Whether the check came from the asset's header.

    Returns
    -------
    CheckResult
        The result.
    """
    task = python_task_run(session, principal.id)
    run_id = task.run_id
    environment = session.scalars(select(Environment)).one()
    row = CheckResult(
        id=uuid.uuid4(),
        run_id=run_id,
        task_run_id=task.id,
        environment_id=environment.id,
        check_name=f"silver.orders/{name}",
        check_path=f"checks/silver/orders/{name}.sql",
        table_name="silver.orders",
        table_id=table_id or _orders_id(session),
        on_failure=on_failure,
        outcome=outcome,
        failing_rows=3 if outcome is CheckOutcome.FAILED else None,
        rollback_outcome=rollback,
        started_at=finished_at - timedelta(seconds=5),
        finished_at=finished_at,
        generated=generated,
    )
    session.add(row)
    session.commit()
    return row


def test_the_latest_result_of_each_check_with_its_marks(
    session: Session, principal: PrincipalRef, catalog_with_orders: CatalogService
) -> None:
    table_id = _orders_id(session)
    assert latest_checks(session, table_id) == []
    _check(session, principal, "unique_id", outcome=CheckOutcome.FAILED, finished_at=_NOW)
    newest_unique = _check(
        session,
        principal,
        "unique_id",
        outcome=CheckOutcome.PASSED,
        finished_at=_NOW + timedelta(hours=1),
    )
    _check(
        session,
        principal,
        "few_nulls",
        outcome=CheckOutcome.FAILED,
        on_failure="keep",
        finished_at=_NOW,
    )
    _check(
        session,
        principal,
        "positive",
        outcome=CheckOutcome.FAILED,
        rollback=RollbackOutcome.UNPUBLISHED,
        finished_at=_NOW,
    )
    _check(
        session,
        principal,
        "rolled",
        outcome=CheckOutcome.FAILED,
        rollback=RollbackOutcome.ROLLED_BACK,
        finished_at=_NOW,
    )
    _check(
        session,
        principal,
        "errors",
        outcome=CheckOutcome.ERRORED,
        on_failure="keep",
        finished_at=_NOW,
    )
    _check(
        session,
        principal,
        "other_table",
        outcome=CheckOutcome.PASSED,
        finished_at=_NOW,
        table_id=uuid.uuid4(),
    )

    found = {c.check: c for c in latest_checks(session, table_id)}
    assert sorted(found) == ["errors", "few_nulls", "positive", "rolled", "unique_id"]
    unique = found["unique_id"]
    assert (unique.outcome, unique.run_id, unique.finished_at) == (
        "passed",
        newest_unique.run_id,
        newest_unique.finished_at,
    )
    assert unique.name == "silver.orders/unique_id"
    assert (unique.kept, unique.not_published) == (False, False)
    assert (found["few_nulls"].kept, found["few_nulls"].failing_rows) == (True, 3)
    assert found["errors"].kept is True
    assert (found["positive"].kept, found["positive"].not_published) == (False, True)
    assert found["positive"].rollback_outcome == "unpublished"
    assert (found["rolled"].kept, found["rolled"].not_published) == (False, False)
    assert [c.check for c in latest_checks(session, table_id)] == sorted(found)


def test_a_kept_mark_needs_a_failure_of_a_keep_check(
    session: Session, principal: PrincipalRef, catalog_with_orders: CatalogService
) -> None:
    _check(
        session,
        principal,
        "fine",
        outcome=CheckOutcome.PASSED,
        on_failure="keep",
        finished_at=_NOW,
    )
    (only,) = latest_checks(session, _orders_id(session))
    assert only.kept is False


def test_a_generated_result_reads_back_with_its_mark(
    session: Session, principal: PrincipalRef, catalog_with_orders: CatalogService
) -> None:
    _check(
        session,
        principal,
        "key_unique",
        outcome=CheckOutcome.PASSED,
        finished_at=_NOW,
        generated=True,
    )
    _check(session, principal, "unique_id", outcome=CheckOutcome.PASSED, finished_at=_NOW)
    found = {c.check: c.generated for c in latest_checks(session, _orders_id(session))}
    assert found == {"key_unique": True, "unique_id": False}


def test_two_results_with_the_same_finish_time_give_one_row_per_check(
    session: Session, principal: PrincipalRef, catalog_with_orders: CatalogService
) -> None:
    _check(session, principal, "same", outcome=CheckOutcome.PASSED, finished_at=_NOW)
    _check(session, principal, "same", outcome=CheckOutcome.FAILED, finished_at=_NOW)
    assert len(latest_checks(session, _orders_id(session))) == 1


def _read(
    session: Session, table_id: uuid.UUID, who: PrincipalRef, day: date, count: int, at: datetime
) -> None:
    """Persist one day's count of reads.

    Parameters
    ----------
    session
        Database session; commits.
    table_id
        The table.
    who
        The reader.
    day
        The UTC day.
    count
        How many reads.
    at
        The last read.
    """
    session.add(
        TableRead(
            id=uuid.uuid4(),
            table_id=table_id,
            principal_id=who.id,
            day=day,
            read_count=count,
            last_read_at=at,
        )
    )
    session.commit()


def test_readers_add_up_the_days_of_the_window_per_principal(
    session: Session, principal: PrincipalRef, catalog_with_orders: CatalogService
) -> None:
    table_id = _orders_id(session)
    ada, bo = _person(session, "ada"), _person(session, "bo")
    assert readers(session, table_id, days=30, now=_NOW) == []
    today = _NOW.date()
    _read(session, table_id, ada, today, 2, _NOW - timedelta(hours=1))
    _read(session, table_id, ada, today - timedelta(days=29), 3, _NOW - timedelta(days=29))
    _read(session, table_id, bo, today - timedelta(days=5), 9, _NOW - timedelta(days=5))
    _read(session, table_id, bo, today - timedelta(days=30), 100, _NOW - timedelta(days=30))
    _read(session, table_id, principal, today, 1, _NOW)

    thirty = readers(session, table_id, days=30, now=_NOW)
    assert [(r.principal, r.read_count) for r in thirty] == [
        ("bo", 9),
        ("ada", 5),
        ("admin", 1),
    ]
    assert thirty[1].last_read_at == _NOW - timedelta(hours=1)
    assert thirty[0].last_read_at == _NOW - timedelta(days=5)

    one = readers(session, table_id, days=1, now=_NOW)
    assert [(r.principal, r.read_count) for r in one] == [("ada", 2), ("admin", 1)]
    assert [r.principal for r in readers(session, table_id, days=31, now=_NOW)] == [
        "bo",
        "ada",
        "admin",
    ]


def test_equal_counts_are_listed_by_name(
    session: Session, principal: PrincipalRef, catalog_with_orders: CatalogService
) -> None:
    table_id = _orders_id(session)
    for name in ("zed", "amy"):
        _read(session, table_id, _person(session, name), _NOW.date(), 4, _NOW)
    assert [r.principal for r in readers(session, table_id, days=7, now=_NOW)] == ["amy", "zed"]


def _reads(session: Session) -> list[tuple[str, date, int, datetime]]:
    """List the stored counts of reads.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    list of tuple
        Table name, day, count and last read, in a stable order.
    """
    session.expire_all()
    rows = session.execute(
        select(Table.name, TableRead.day, TableRead.read_count, TableRead.last_read_at)
        .join(Table, Table.id == TableRead.table_id)
        .order_by(Table.name, TableRead.day)
    ).all()
    return [(r[0], r[1], r[2], r[3]) for r in rows]


def test_a_completed_query_counts_one_read_per_table_it_names_per_day(
    session: Session, principal: PrincipalRef, catalog_with_orders: CatalogService
) -> None:
    catalog_with_orders.create_table(principal, TableRef("dev", _SILVER, "items"), schema=_SCHEMA)
    record_table_reads(
        session,
        environment="dev",
        principal_id=principal.id,
        dotted_refs=["silver.orders", "silver.orders", "silver.items"],
        now=_NOW,
    )
    session.commit()
    assert _reads(session) == [
        ("items", _NOW.date(), 1, _NOW),
        ("orders", _NOW.date(), 1, _NOW),
    ]

    later = _NOW + timedelta(hours=2)
    record_table_reads(
        session,
        environment="dev",
        principal_id=principal.id,
        dotted_refs=["silver.orders"],
        now=later,
    )
    record_table_reads(
        session,
        environment="dev",
        principal_id=principal.id,
        dotted_refs=["silver.orders"],
        now=later + timedelta(days=1),
    )
    session.commit()
    assert _reads(session) == [
        ("items", _NOW.date(), 1, _NOW),
        ("orders", _NOW.date(), 2, later),
        ("orders", (later + timedelta(days=1)).date(), 1, later + timedelta(days=1)),
    ]


def test_another_principal_has_its_own_count(
    session: Session, principal: PrincipalRef, catalog_with_orders: CatalogService
) -> None:
    other = _person(session, "other")
    for who in (principal, other, other):
        record_table_reads(
            session,
            environment="dev",
            principal_id=who.id,
            dotted_refs=["silver.orders"],
            now=_NOW,
        )
    session.commit()
    table_id = _orders_id(session)
    assert [(r.principal, r.read_count) for r in readers(session, table_id, days=1, now=_NOW)] == [
        ("other", 2),
        ("admin", 1),
    ]


def test_names_that_resolve_to_no_table_count_nothing(
    session: Session, principal: PrincipalRef, catalog_with_orders: CatalogService
) -> None:
    record_table_reads(
        session,
        environment="dev",
        principal_id=principal.id,
        dotted_refs=["silver.nothing", "orders", "silver.", "a.b.c.d", "dev..x", ""],
        now=_NOW,
    )
    record_table_reads(
        session,
        environment="other",
        principal_id=principal.id,
        dotted_refs=["silver.orders"],
        now=_NOW,
    )
    session.commit()
    assert _reads(session) == []


def test_no_reads_to_record_runs_no_statement(
    session: Session, principal: PrincipalRef, catalog_with_orders: CatalogService
) -> None:
    record_table_reads(
        session, environment="dev", principal_id=principal.id, dotted_refs=[], now=_NOW
    )
    assert _reads(session) == []


def test_the_sweep_deletes_the_days_before_the_kept_period(
    session: Session, principal: PrincipalRef, catalog_with_orders: CatalogService
) -> None:
    table_id = _orders_id(session)
    today = _NOW.date()
    for age in (0, 89, 90, 400):
        _read(session, table_id, principal, today - timedelta(days=age), 1, _NOW)
    assert sweep_table_reads(session, keep_days=90, now=_NOW) == 2
    assert [r[1] for r in _reads(session)] == [today - timedelta(days=89), today]
    assert sweep_table_reads(session, keep_days=90, now=_NOW) == 0
    assert sweep_table_reads(session, keep_days=1, now=_NOW) == 1
    assert [r[1] for r in _reads(session)] == [today]


def test_the_reads_go_with_the_table_and_with_the_principal(
    session: Session, principal: PrincipalRef, catalog_with_orders: CatalogService
) -> None:
    table_id = _orders_id(session)
    gone = _person(session, "gone")
    _read(session, table_id, gone, _NOW.date(), 1, _NOW)
    _read(session, table_id, principal, _NOW.date(), 1, _NOW)
    session.delete(session.get(Principal, gone.id))
    session.commit()
    assert [r[2] for r in _reads(session)] == [1]
    session.delete(session.get(Table, table_id))
    session.commit()
    assert _reads(session) == []


def _table_health_catalog(session: Session, tmp_path: Path, allowed: set[Action]) -> CatalogService:
    """Build a catalog that permits only some actions, with `silver.orders` present.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        Storage root.
    allowed
        The actions the authorization permits.

    Returns
    -------
    CatalogService
        The catalog.
    """
    return _prepared_catalog(
        session, tmp_path, ActionAllowList(*allowed), namespaces=[_SILVER], tables=[_ORDERS]
    )


def test_table_health_needs_read_table(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    catalog = _table_health_catalog(session, tmp_path, set())
    with pytest.raises(PermissionDenied):
        catalog.table_health(principal, _ORDERS, days=30, now=_NOW)


def test_table_health_puts_the_parts_together(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    catalog = _table_health_catalog(session, tmp_path, {Action.READ_TABLE, Action.MANAGE_GRANT})
    table_id = _orders_id(session)
    _maintenance(session, principal, finished_at=_NOW)
    _check(session, principal, "c", outcome=CheckOutcome.PASSED, finished_at=_NOW)
    _read(session, table_id, principal, _NOW.date(), 4, _NOW)

    session.add(
        Grant(
            id=uuid.uuid4(),
            principal_id=principal.id,
            role=Role.ADMIN,
            kind=SecurableKind.INSTANCE,
            granted_by=principal.id,
            granted_at=_NOW,
        )
    )
    session.commit()

    shown = catalog.table_health(principal, _ORDERS, days=30, now=_NOW)
    assert shown.days == 30
    assert shown.last_maintenance is not None and shown.last_maintenance.status == "complete"
    assert [c.check for c in shown.checks] == ["c"]
    assert shown.readers is not None
    assert [(r.principal, r.read_count) for r in shown.readers] == [("admin", 4)]


def test_readers_are_shown_only_to_someone_who_may_see_grants(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    _table_health_catalog(session, tmp_path, set())
    table_id = _orders_id(session)
    _read(session, table_id, principal, _NOW.date(), 4, _NOW)
    _maintenance(session, principal, finished_at=_NOW)

    def readers_for(authz: object) -> list | None:
        catalog = CatalogService(session, StorageResolver(session), authz)  # ty: ignore[invalid-argument-type]
        return catalog.table_health(principal, _ORDERS, days=30, now=_NOW).readers

    # No MANAGE_GRANT; MANAGE_GRANT without the ADMIN role; each hides the readers, not the rest.
    assert readers_for(ActionAllowList(Action.READ_TABLE)) is None
    assert readers_for(ActionAllowList(Action.READ_TABLE, Action.MANAGE_GRANT)) is None
    allowed = readers_for(AllowAllAuthorization())
    assert allowed is not None and [r.read_count for r in allowed] == [4]
    session.add(
        Grant(
            id=uuid.uuid4(),
            principal_id=principal.id,
            role=Role.ADMIN,
            kind=SecurableKind.INSTANCE,
            granted_by=principal.id,
            granted_at=_NOW,
        )
    )
    session.commit()
    assert readers_for(ActionAllowList(Action.READ_TABLE, Action.MANAGE_GRANT)) is not None


def test_table_health_of_a_missing_table_is_not_found(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    from lakana_core.errors import NotFoundError

    catalog = _table_health_catalog(session, tmp_path, {Action.READ_TABLE})
    with pytest.raises(NotFoundError):
        catalog.table_health(principal, TableRef("dev", _SILVER, "nope"), days=30, now=_NOW)


def test_describe_table_carries_the_numbers_of_the_current_snapshot(
    principal: PrincipalRef, catalog_with_orders: CatalogService
) -> None:
    before = catalog_with_orders.describe_table(principal, _ORDERS)
    assert (before.last_write_at, before.row_count, before.size_bytes, before.file_count) == (
        None,
        None,
        None,
        None,
    )
    catalog_with_orders.append(principal, _ORDERS, _data([1, 2]))
    after = catalog_with_orders.describe_table(principal, _ORDERS)
    assert (after.row_count, after.file_count) == (2, 1)
    assert after.size_bytes is not None and after.size_bytes > 0
    assert after.last_write_at is not None


def test_a_view_has_no_numbers(
    session: Session, principal: PrincipalRef, catalog_with_orders: CatalogService
) -> None:
    session.execute(
        Table.__table__.update().where(Table.name == "orders").values(table_type="VIEW")  # ty: ignore[unresolved-attribute]
    )
    session.commit()
    shown = catalog_with_orders.describe_table(principal, _ORDERS)
    assert (shown.last_write_at, shown.row_count, shown.size_bytes, shown.file_count) == (
        None,
        None,
        None,
        None,
    )


@pytest.mark.parametrize("timestamp_ms", [10**17, -(10**17), 2**62])
def test_a_snapshot_time_out_of_range_reads_as_no_time(timestamp_ms: int) -> None:
    summary = {"total-records": "4"}
    snapshot = SimpleNamespace(summary=SimpleNamespace(get=summary.get), timestamp_ms=timestamp_ms)
    metadata = SimpleNamespace(snapshot_by_id=lambda _: snapshot, current_snapshot_id=7)
    stats = snapshot_stats(metadata)  # ty: ignore[invalid-argument-type]
    assert stats.last_write_at is None
    assert stats.row_count == 4
