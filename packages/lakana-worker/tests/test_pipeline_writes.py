"""Write runs end to end in the worker: SQLite plus local storage."""

from __future__ import annotations

import base64
import contextlib
import datetime as dt
import logging
import sys
import time
import uuid
from collections.abc import Callable, Generator, Iterator, Mapping, Sequence
from pathlib import Path

import pyarrow as pa
import pytest
from pyiceberg.table import StaticTable
from pyiceberg.types import TimestamptzType
from sqlalchemy import select, update
from sqlalchemy.orm import Session
from worker_test_helpers import ActionAllowList

from lakana_core.auth import (
    Action,
    AllowAllAuthorization,
    AuthorizationService,
    PrincipalRef,
    Securable,
    WriteFence,
)
from lakana_core.catalog.query_service import QueryService, WriteTarget
from lakana_core.catalog.service import CatalogService
from lakana_core.config import QueryLimitsConfig, StorageConfig
from lakana_core.errors import PermissionDenied, ValidationError, WriteOutcomeUnknownError
from lakana_core.identifiers import TableRef
from lakana_core.models import (
    AuditEvent,
    AuditEventType,
    LineageEdge,
    Principal,
    PrincipalType,
    QueryErrorCause,
    QueryRun,
    QueryRunStatus,
    WriteMode,
)
from lakana_core.policy.column_masks import ColumnMaskService
from lakana_core.policy.row_filters import RowFilterService
from lakana_core.runs.lockdown import (
    WRITE_FENCE_MESSAGE,
    ProtectedWriteGuard,
    Target,
    WriteOperation,
)
from lakana_core.secrets.keys import EnvKeyProvider
from lakana_core.secrets.service import SecretService
from lakana_core.storage.resolver import StorageResolver
from lakana_worker import executor as executor_module
from lakana_worker import pipeline as pipeline_module
from lakana_worker.pipeline import run_one


@pytest.fixture
def session(file_session: Session) -> Session:
    """Run this module on a SQLite file, which the cancel flag's poller thread can read.

    Parameters
    ----------
    file_session
        The session on the file.

    Returns
    -------
    Session
        The same session.
    """
    return file_session


@pytest.fixture
def principal(session: Session) -> PrincipalRef:
    """Provide the acting principal, persisted.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    PrincipalRef
        The principal `admin`.
    """
    principal_id = uuid.uuid4()
    session.add(Principal(id=principal_id, type=PrincipalType.USER, name="admin"))
    session.commit()
    return PrincipalRef(id=principal_id, name="admin", type=PrincipalType.USER)


@pytest.fixture
def catalog(session: Session, principal: PrincipalRef, tmp_path: Path) -> CatalogService:
    """Provide an allow-all catalog over `dev` at `tmp_path`.

    Parameters
    ----------
    session
        Database session.
    principal
        The acting principal.
    tmp_path
        The storage root.

    Returns
    -------
    CatalogService
        The catalog.
    """
    catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
    catalog.create_environment(
        principal,
        name="dev",
        storage=StorageConfig(scheme="file", root=f"file://{tmp_path}", options={}),
    )
    return catalog


def _seed(
    catalog: CatalogService, principal: PrincipalRef, dotted: str, data: pa.Table, mode: WriteMode
) -> None:
    """Write `data` into `dotted` directly through the core write service.

    Parameters
    ----------
    catalog
        The catalog.
    principal
        The acting principal.
    dotted
        The table.
    data
        The rows.
    mode
        The write mode.
    """
    catalog.write_batches(
        principal,
        TableRef.from_dotted("dev", dotted),
        mode=mode,
        schema=data.schema,
        batches=data.to_batches(),
    )


def _run(
    session: Session,
    principal: PrincipalRef,
    sql: str,
    target: WriteTarget,
    authz: AuthorizationService | None = None,
    limits: QueryLimitsConfig | None = None,
) -> QueryRun:
    """Submit a write run and run it in-process.

    Parameters
    ----------
    session
        Database session.
    principal
        The submitter.
    sql
        The SELECT.
    target
        Where it goes.
    authz
        The worker's authorization; allow-all when `None`.
    limits
        The worker's limits.

    Returns
    -------
    QueryRun
        The finished run, refreshed.
    """
    run = QueryService(session, AllowAllAuthorization()).submit_write(principal, "dev", sql, target)
    run_one(session, run.id, authz or AllowAllAuthorization(), limits=limits)
    session.refresh(run)
    return run


def _rows(catalog: CatalogService, principal: PrincipalRef, dotted: str) -> list[dict[str, object]]:
    """Read a table back, sorted by `id`.

    Parameters
    ----------
    catalog
        The catalog.
    principal
        The acting principal.
    dotted
        The table.

    Returns
    -------
    list of dict
        The rows.
    """
    loaded = catalog.load_table(principal, TableRef.from_dotted("dev", dotted))
    rows = StaticTable.from_metadata(loaded.metadata_location).scan().to_arrow().to_pylist()
    return sorted(rows, key=lambda row: row["id"])


def _data_files(tmp_path: Path) -> set[str]:
    """List every Parquet file under the storage root.

    Parameters
    ----------
    tmp_path
        The storage root.

    Returns
    -------
    set of str
        Root-relative paths.
    """
    return {str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*.parquet")}


def _orders() -> pa.Table:
    """Three orders in two regions.

    Returns
    -------
    pa.Table
        The rows.
    """
    return pa.table({"id": pa.array([1, 2, 3], pa.int64()), "region": pa.array(["eu", "us", "eu"])})


def test_create_writes_the_result_into_a_new_table(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """`--mode create`: the result becomes a table and the run reports the write."""
    _seed(catalog, principal, "bronze.orders", _orders(), WriteMode.CREATE)

    run = _run(
        session,
        principal,
        "SELECT region AS id, count(*) AS n FROM bronze.orders GROUP BY region",
        WriteTarget(table="gold.daily", mode=WriteMode.CREATE),
    )

    assert run.status == QueryRunStatus.SUCCEEDED, run.error_message
    loaded = catalog.load_table(principal, TableRef.from_dotted("dev", "gold.daily"))
    assert run.rows_written == 2
    assert run.snapshot_id == loaded.metadata.current_snapshot_id
    assert run.target_table_id == loaded.uuid
    assert run.result_location is None
    assert _rows(catalog, principal, "gold.daily") == [{"id": "eu", "n": 2}, {"id": "us", "n": 1}]
    edge = session.execute(select(LineageEdge)).scalar_one()
    assert (edge.source_name, edge.target_name, edge.query_run_id) == (
        "bronze.orders",
        "gold.daily",
        run.id,
    )


def test_a_refresh_can_read_its_own_target(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """`replace` over `FROM <target>` reads the old snapshot, writes the new."""
    _seed(catalog, principal, "gold.daily", _orders(), WriteMode.CREATE)

    run = _run(
        session,
        principal,
        "SELECT id, region FROM gold.daily WHERE id > 1",
        WriteTarget(table="gold.daily", mode=WriteMode.REPLACE),
    )

    assert run.status == QueryRunStatus.SUCCEEDED, run.error_message
    assert [row["id"] for row in _rows(catalog, principal, "gold.daily")] == [2, 3]
    edges = session.execute(select(LineageEdge)).scalars().all()
    assert [(e.source_name, e.target_name) for e in edges] == [("gold.daily", "gold.daily")]


def test_an_incremental_append_takes_only_rows_past_the_watermark(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """New rows are appended; late rows (key ≤ watermark) are skipped."""
    events = pa.table(
        {
            "id": pa.array([1, 2], pa.int64()),
            "ts": pa.array([dt.datetime(2026, 1, 1), dt.datetime(2026, 1, 2)], pa.timestamp("us")),
        }
    )
    _seed(catalog, principal, "bronze.events", events, WriteMode.CREATE)
    _seed(catalog, principal, "gold.events", events, WriteMode.CREATE)
    later = pa.table(
        {
            "id": pa.array([3, 4, 5], pa.int64()),
            "ts": pa.array(
                [dt.datetime(2026, 1, 3), dt.datetime(2026, 1, 4), dt.datetime(2025, 12, 31)],
                pa.timestamp("us"),
            ),
        }
    )
    _seed(catalog, principal, "bronze.events", later, WriteMode.APPEND)

    run = _run(
        session,
        principal,
        "SELECT id, ts FROM bronze.events",
        WriteTarget(table="gold.events", mode=WriteMode.APPEND, incremental_key="ts"),
    )

    assert run.status == QueryRunStatus.SUCCEEDED, run.error_message
    assert run.watermark == "timestamp:2026-01-02T00:00:00"
    assert run.rows_written == 2
    assert [row["id"] for row in _rows(catalog, principal, "gold.events")] == [1, 2, 3, 4]


def test_an_incremental_write_sets_the_watermark_beside_the_url_lists(
    session: Session,
    principal: PrincipalRef,
    catalog: CatalogService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every value is a variable: two URL lists and the watermark, and no placeholder.

    Runs against real DuckDB (the spy only records what it is handed), with
    two sources so more than one URL list sits beside the watermark.
    """
    events = pa.table({"id": pa.array([1, 2], pa.int64())})
    _seed(catalog, principal, "bronze.events", events, WriteMode.CREATE)
    _seed(
        catalog,
        principal,
        "bronze.more",
        pa.table({"id": pa.array([3, 4], pa.int64())}),
        WriteMode.CREATE,
    )
    _seed(catalog, principal, "gold.events", events, WriteMode.CREATE)
    seen: list[tuple[str, object]] = []
    real_stream_query = pipeline_module.stream_query

    def spy(sql: str, parameters: object, **kwargs: object) -> object:
        seen.append((sql, parameters))
        return real_stream_query(sql, parameters, **kwargs)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(pipeline_module, "stream_query", spy)

    run = _run(
        session,
        principal,
        "SELECT id FROM bronze.events UNION ALL SELECT id FROM bronze.more",
        WriteTarget(table="gold.events", mode=WriteMode.APPEND, incremental_key="id"),
    )

    assert run.status == QueryRunStatus.SUCCEEDED, run.error_message
    assert run.watermark == "long:2"
    assert run.rows_written == 2
    assert [row["id"] for row in _rows(catalog, principal, "gold.events")] == [1, 2, 3, 4]
    [(sql, parameters)] = seen
    assert isinstance(parameters, dict)
    assert sorted(parameters) == [
        "v_0_files",
        "v_0_schema",
        "v_1_files",
        "v_1_schema",
        "v_watermark",
    ]
    assert parameters["v_watermark"] == 2
    assert "> getvariable('v_watermark')" in sql
    assert "$" not in sql


def test_an_incremental_append_into_an_empty_table_takes_every_row(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """With no watermark nothing is wrapped."""
    _seed(catalog, principal, "bronze.orders", _orders(), WriteMode.CREATE)
    _seed(catalog, principal, "gold.orders", _orders().slice(0, 0), WriteMode.CREATE)

    run = _run(
        session,
        principal,
        "SELECT id, region FROM bronze.orders",
        WriteTarget(table="gold.orders", mode=WriteMode.APPEND, incremental_key="id"),
    )

    assert run.status == QueryRunStatus.SUCCEEDED, run.error_message
    assert run.watermark is None
    assert run.rows_written == 3


def _first_incremental_run(
    session: Session,
    principal: PrincipalRef,
    sql: str,
    table: str,
    key: str = "id",
    authz: AuthorizationService | None = None,
) -> QueryRun:
    """Run what a task's first run of an incremental asset is: a create that carries the key.

    `WriteTarget` refuses a key with `create`, because the public API has no use for one; the
    worker builds the write run of a task itself, with the manifest's `first_write_mode`.

    Parameters
    ----------
    session
        Database session.
    principal
        The submitter.
    sql
        The SELECT.
    table
        The target, which does not exist yet.
    key
        The incremental key.
    authz
        The worker's authorization; allow-all when `None`.

    Returns
    -------
    QueryRun
        The finished run, refreshed.
    """
    run = QueryService(session, AllowAllAuthorization()).submit_write(
        principal, "dev", sql, WriteTarget(table=table, mode=WriteMode.CREATE)
    )
    run.incremental_key = key
    session.commit()
    run_one(session, run.id, authz or AllowAllAuthorization())
    session.refresh(run)
    return run


def test_the_first_run_of_an_incremental_asset_creates_its_table_with_every_row(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """No table yet means no watermark to read; the second run appends past the first."""
    _seed(catalog, principal, "bronze.orders", _orders(), WriteMode.CREATE)

    first = _first_incremental_run(
        session, principal, "SELECT id, region FROM bronze.orders", "gold.orders"
    )

    assert first.status == QueryRunStatus.SUCCEEDED, first.error_message
    assert (first.watermark, first.rows_written) == (None, 3)
    loaded = catalog.load_table(principal, TableRef.from_dotted("dev", "gold.orders"))
    assert first.snapshot_id == loaded.metadata.current_snapshot_id is not None
    more = pa.table({"id": pa.array([4, 5], pa.int64()), "region": pa.array(["us", "eu"])})
    _seed(catalog, principal, "bronze.orders", more, WriteMode.APPEND)

    second = _run(
        session,
        principal,
        "SELECT id, region FROM bronze.orders",
        WriteTarget(table="gold.orders", mode=WriteMode.APPEND, incremental_key="id"),
    )

    assert second.status == QueryRunStatus.SUCCEEDED, second.error_message
    assert (second.watermark, second.rows_written) == ("long:3", 2)
    assert [row["id"] for row in _rows(catalog, principal, "gold.orders")] == [1, 2, 3, 4, 5]


def test_a_first_run_that_returns_no_rows_creates_the_empty_table_so_the_next_run_can_append(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """An empty create keeps the schema; a table without a snapshot has no watermark."""
    _seed(catalog, principal, "bronze.orders", _orders(), WriteMode.CREATE)

    first = _first_incremental_run(
        session, principal, "SELECT id, region FROM bronze.orders WHERE id > 100", "gold.orders"
    )

    assert first.status == QueryRunStatus.SUCCEEDED, first.error_message
    assert (first.rows_written, first.snapshot_id) == (0, None)
    created = catalog.load_table(principal, TableRef.from_dotted("dev", "gold.orders"))
    assert [f.name for f in created.metadata.schema().fields] == ["id", "region"]
    assert created.metadata.current_snapshot_id is None

    second = _run(
        session,
        principal,
        "SELECT id, region FROM bronze.orders",
        WriteTarget(table="gold.orders", mode=WriteMode.APPEND, incremental_key="id"),
    )

    assert second.status == QueryRunStatus.SUCCEEDED, second.error_message
    assert (second.watermark, second.rows_written) == (None, 3)
    assert [row["id"] for row in _rows(catalog, principal, "gold.orders")] == [1, 2, 3]


def test_a_first_incremental_run_whose_result_lacks_the_key_creates_nothing(
    session: Session, principal: PrincipalRef, catalog: CatalogService, tmp_path: Path
) -> None:
    """The key is checked on a create as on an append, before a file is written."""
    _seed(catalog, principal, "bronze.orders", _orders(), WriteMode.CREATE)
    before = _data_files(tmp_path)

    run = _first_incremental_run(
        session, principal, "SELECT region FROM bronze.orders", "gold.orders"
    )

    assert run.status == QueryRunStatus.FAILED
    assert "'id' is not a column of the result" in (run.error_message or "")
    assert _data_files(tmp_path) == before


def test_a_first_incremental_run_whose_key_cannot_be_used_later_creates_nothing(
    session: Session, principal: PrincipalRef, catalog: CatalogService, tmp_path: Path
) -> None:
    """A string key is refused on the create, not on the run after it."""
    _seed(catalog, principal, "bronze.orders", _orders(), WriteMode.CREATE)
    before = _data_files(tmp_path)

    run = _first_incremental_run(
        session, principal, "SELECT id, region FROM bronze.orders", "gold.orders", key="region"
    )

    assert run.status == QueryRunStatus.FAILED
    assert "must be an int, long" in (run.error_message or "")
    assert _data_files(tmp_path) == before


def test_rerunning_an_incremental_append_with_nothing_new_is_a_quiet_success(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """The scheduled no-op run succeeds, commits nothing, and is audited."""
    _seed(catalog, principal, "bronze.orders", _orders(), WriteMode.CREATE)
    _seed(catalog, principal, "gold.orders", _orders(), WriteMode.CREATE)
    before = catalog.load_table(principal, TableRef.from_dotted("dev", "gold.orders"))

    run = _run(
        session,
        principal,
        "SELECT id, region FROM bronze.orders",
        WriteTarget(table="gold.orders", mode=WriteMode.APPEND, incremental_key="id"),
    )

    assert run.status == QueryRunStatus.SUCCEEDED, run.error_message
    assert run.rows_written == 0
    assert run.snapshot_id is None
    assert run.watermark == "long:3"
    after = catalog.load_table(principal, TableRef.from_dotted("dev", "gold.orders"))
    assert after.metadata.current_snapshot_id == before.metadata.current_snapshot_id
    audited = session.execute(
        select(AuditEvent).where(AuditEvent.event_type == AuditEventType.TABLE_WRITE)
    ).scalars()
    assert [event.detail["rows"] for event in audited][-1] == 0


def test_an_incremental_key_missing_from_the_result_fails_before_writing(
    session: Session, principal: PrincipalRef, catalog: CatalogService, tmp_path: Path
) -> None:
    """The key must exist in both the result and the target."""
    _seed(catalog, principal, "bronze.orders", _orders(), WriteMode.CREATE)
    _seed(catalog, principal, "gold.orders", _orders().slice(0, 0), WriteMode.CREATE)
    before = _data_files(tmp_path)

    run = _run(
        session,
        principal,
        "SELECT region FROM bronze.orders",
        WriteTarget(table="gold.orders", mode=WriteMode.APPEND, incremental_key="id"),
    )

    assert run.status == QueryRunStatus.FAILED
    assert "'id' is not a column of the result" in (run.error_message or "")
    assert _data_files(tmp_path) == before


def test_an_unorderable_incremental_key_fails(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """A string key is refused."""
    _seed(catalog, principal, "gold.orders", _orders(), WriteMode.CREATE)

    run = _run(
        session,
        principal,
        "SELECT id, region FROM gold.orders",
        WriteTarget(table="gold.orders", mode=WriteMode.APPEND, incremental_key="region"),
    )

    assert run.status == QueryRunStatus.FAILED
    assert "must be an int, long" in (run.error_message or "")


def test_an_incremental_append_is_refused_under_a_policy_on_the_target(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """A watermark over rows the caller can't see is refused."""
    _seed(catalog, principal, "gold.orders", _orders(), WriteMode.CREATE)
    RowFilterService(session, AllowAllAuthorization(), catalog).create(
        principal,
        principal="admin",
        environment="dev",
        namespace="gold",
        table="orders",
        expression="id > 1",
    )

    run = _run(
        session,
        principal,
        "SELECT id, region FROM gold.orders",
        WriteTarget(table="gold.orders", mode=WriteMode.APPEND, incremental_key="id"),
    )

    assert run.status == QueryRunStatus.FAILED
    assert "row filter or column mask" in (run.error_message or "")


def test_masked_and_filtered_source_data_lands_masked_and_filtered(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """Security: the target receives exactly what the caller may see."""
    customers = pa.table(
        {"id": pa.array([1, 2, 3], pa.int64()), "ssn": pa.array(["111", "222", "333"])}
    )
    _seed(catalog, principal, "bronze.customers", customers, WriteMode.CREATE)
    ColumnMaskService(session, AllowAllAuthorization(), catalog).create(
        principal,
        principal="admin",
        environment="dev",
        namespace="bronze",
        table="customers",
        column="ssn",
        expression="'***'",
    )
    RowFilterService(session, AllowAllAuthorization(), catalog).create(
        principal,
        principal="admin",
        environment="dev",
        namespace="bronze",
        table="customers",
        expression="id > 1",
    )

    run = _run(
        session,
        principal,
        "SELECT id, ssn FROM bronze.customers",
        WriteTarget(table="gold.customers", mode=WriteMode.CREATE),
    )

    assert run.status == QueryRunStatus.SUCCEEDED, run.error_message
    assert _rows(catalog, principal, "gold.customers") == [
        {"id": 2, "ssn": "***"},
        {"id": 3, "ssn": "***"},
    ]


def test_a_write_run_cannot_persist_the_rewritten_sql_through_current_query(
    session: Session, principal: PrincipalRef, catalog: CatalogService, tmp_path: Path
) -> None:
    """`current_query()` would write the filter and mask text into a readable table."""
    customers = pa.table(
        {"id": pa.array([1, 2, 3], pa.int64()), "ssn": pa.array(["111", "222", "333"])}
    )
    _seed(catalog, principal, "bronze.customers", customers, WriteMode.CREATE)
    ColumnMaskService(session, AllowAllAuthorization(), catalog).create(
        principal,
        principal="admin",
        environment="dev",
        namespace="bronze",
        table="customers",
        column="ssn",
        expression="'***'",
    )
    RowFilterService(session, AllowAllAuthorization(), catalog).create(
        principal,
        principal="admin",
        environment="dev",
        namespace="bronze",
        table="customers",
        expression="id > 1",
    )

    run = _run(
        session,
        principal,
        "SELECT id, current_query() AS q FROM bronze.customers",
        WriteTarget(table="gold.customers", mode=WriteMode.CREATE),
    )

    assert run.status == QueryRunStatus.FAILED
    assert "current_query may not be called" in (run.error_message or "")
    assert "id > 1" not in (run.error_message or "")
    assert "'***'" not in (run.error_message or "")
    assert run.rows_written is None
    assert not any("gold" in path and "customers" in path for path in _data_files(tmp_path))


def test_max_write_rows_fails_the_run_and_leaves_the_target_untouched(
    session: Session, principal: PrincipalRef, catalog: CatalogService, tmp_path: Path
) -> None:
    """Spec: exceeding `max_write_rows` fails the run and cleans up."""
    _seed(catalog, principal, "gold.orders", _orders(), WriteMode.CREATE)
    before = _data_files(tmp_path)

    run = _run(
        session,
        principal,
        "SELECT id, region FROM gold.orders",
        WriteTarget(table="gold.orders", mode=WriteMode.REPLACE),
        limits=QueryLimitsConfig(max_write_rows=2),
    )

    assert run.status == QueryRunStatus.FAILED
    assert "max_write_rows" in (run.error_message or "")
    assert _data_files(tmp_path) == before
    assert [row["id"] for row in _rows(catalog, principal, "gold.orders")] == [1, 2, 3]


def test_writes_are_not_bound_by_the_result_caps(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """Spec: writes are not bound by `max_result_rows`/`max_result_bytes`."""
    _seed(catalog, principal, "bronze.orders", _orders(), WriteMode.CREATE)

    run = _run(
        session,
        principal,
        "SELECT id, region FROM bronze.orders",
        WriteTarget(table="gold.orders", mode=WriteMode.CREATE),
        limits=QueryLimitsConfig(max_result_rows=1, max_result_bytes=1),
    )

    assert run.status == QueryRunStatus.SUCCEEDED, run.error_message
    assert run.rows_written == 3


def test_a_replace_that_fails_mid_query_leaves_the_old_snapshot_and_no_new_files(
    session: Session, principal: PrincipalRef, catalog: CatalogService, tmp_path: Path
) -> None:
    """Security: `replace` is atomic even when DuckDB fails partway through the stream."""
    big = pa.table({"id": pa.array(range(400_000), pa.int64())})
    _seed(catalog, principal, "bronze.big", big, WriteMode.CREATE)
    _seed(catalog, principal, "gold.big", big.slice(0, 5), WriteMode.CREATE)
    snapshot = catalog.load_table(principal, TableRef.from_dotted("dev", "gold.big"))
    before = _data_files(tmp_path)

    run = _run(
        session,
        principal,
        "SELECT CASE WHEN id > 300000 THEN error('boom') ELSE id END AS id FROM bronze.big",
        WriteTarget(table="gold.big", mode=WriteMode.REPLACE),
    )

    assert run.status == QueryRunStatus.FAILED
    after = catalog.load_table(principal, TableRef.from_dotted("dev", "gold.big"))
    assert after.metadata.current_snapshot_id == snapshot.metadata.current_snapshot_id
    assert _data_files(tmp_path) == before


def test_a_cancelled_write_commits_nothing(
    session: Session, principal: PrincipalRef, catalog: CatalogService, tmp_path: Path
) -> None:
    """A cancel request stops the write's query; no table, no snapshot and no file is left."""
    before = _data_files(tmp_path)
    run = QueryService(session, AllowAllAuthorization()).submit_write(
        principal,
        "dev",
        "SELECT sum(hash(a.range + b.range)) AS n FROM range(40000) a, range(40000) b",
        WriteTarget(table="gold.cancelled", mode=WriteMode.CREATE),
    )
    session.execute(update(QueryRun).where(QueryRun.id == run.id).values(cancel_requested=True))
    session.commit()
    started = time.monotonic()
    failure = run_one(session, run.id, AllowAllAuthorization())
    assert time.monotonic() - started < 6
    assert isinstance(failure, executor_module.QueryCancelRequested)
    session.refresh(run)
    assert run.status == QueryRunStatus.CANCELLED
    assert run.error_cause == "cancelled"
    assert (run.rows_written, run.snapshot_id, run.target_table_id) == (None, None, None)
    assert not catalog.table_exists(principal, TableRef.from_dotted("dev", "gold.cancelled"))
    assert _data_files(tmp_path) == before


def test_the_worker_reauthorizes_the_write(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """Security: a grant lost between submission and execution fails the run unwritten."""
    _seed(catalog, principal, "gold.orders", _orders(), WriteMode.CREATE)
    before = catalog.load_table(principal, TableRef.from_dotted("dev", "gold.orders"))
    no_write = ActionAllowList(*(action for action in Action if action is not Action.WRITE_TABLE))

    run = _run(
        session,
        principal,
        "SELECT id, region FROM gold.orders",
        WriteTarget(table="gold.orders", mode=WriteMode.REPLACE),
        authz=no_write,
    )

    assert run.status == QueryRunStatus.FAILED
    assert "WRITE_TABLE" in (run.error_message or "")
    after = catalog.load_table(principal, TableRef.from_dotted("dev", "gold.orders"))
    assert after.metadata.current_snapshot_id == before.metadata.current_snapshot_id


def test_a_create_without_the_right_to_create_says_what_the_authorization_said(
    session: Session, principal: PrincipalRef, catalog: CatalogService, tmp_path: Path
) -> None:
    """Only the first run of an incremental asset adds a hint about the right it lacks."""
    _seed(catalog, principal, "bronze.orders", _orders(), WriteMode.CREATE)
    before = _data_files(tmp_path)
    no_create = ActionAllowList(*(a for a in Action if a is not Action.CREATE_TABLE))

    plain = _run(
        session,
        principal,
        "SELECT id, region FROM bronze.orders",
        WriteTarget(table="gold.plain", mode=WriteMode.CREATE),
        authz=no_create,
    )
    first = _first_incremental_run(
        session,
        principal,
        "SELECT id, region FROM bronze.orders",
        "gold.orders",
        authz=no_create,
    )

    assert plain.status == first.status == QueryRunStatus.FAILED
    assert plain.error_message == "may not CREATE_TABLE"
    assert first.error_message == (
        "may not CREATE_TABLE. The first run of an incremental asset creates gold.orders, which "
        "needs the CREATE_TABLE right on it. Grant it to the run's principal, or create the "
        "table first."
    )
    assert _data_files(tmp_path) == before


class _Superseded(ActionAllowList):
    """Refuses every create the way the lockdown guard does: not for a missing right."""

    def authorize(self, principal: PrincipalRef, action: Action, securable: Securable) -> None:
        """Refuse a create with a reason that is not a missing right.

        Parameters
        ----------
        principal
            The actor requesting the action.
        action
            The action requested.
        securable
            What it is requested on.

        Raises
        ------
        PermissionDenied
            For a create.
        """
        if action is Action.CREATE_TABLE:
            raise PermissionDenied("This task attempt was superseded.")


def test_a_create_refused_for_another_reason_gets_no_advice_about_a_right(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """Granting CREATE_TABLE would not help, so the hint must not be there."""
    _seed(catalog, principal, "bronze.orders", _orders(), WriteMode.CREATE)

    first = _first_incremental_run(
        session,
        principal,
        "SELECT id, region FROM bronze.orders",
        "gold.orders",
        authz=_Superseded(*Action),
    )

    assert first.status == QueryRunStatus.FAILED
    assert first.error_message == "This task attempt was superseded."


def test_a_timestamptz_result_becomes_a_timestamptz_column(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """The created column is `timestamptz`."""
    run = _run(
        session,
        principal,
        "SELECT 1 AS id, TIMESTAMPTZ '2026-01-01 12:00:00+00' AS ts",
        WriteTarget(table="gold.tz", mode=WriteMode.CREATE),
    )

    assert run.status == QueryRunStatus.SUCCEEDED, run.error_message
    loaded = catalog.load_table(principal, TableRef.from_dotted("dev", "gold.tz"))
    assert loaded.metadata.schema().find_field("ts").field_type == TimestamptzType()


def test_a_nanosecond_timestamptz_result_is_refused_with_the_cast_to_write(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """DuckDB 2.0's `TIMESTAMPTZ_NS` exports nanoseconds, which a Lakana table cannot store."""
    run = _run(
        session,
        principal,
        "SELECT 1 AS id, CAST('2026-01-01 12:00:00+00' AS TIMESTAMPTZ_NS) AS ts",
        WriteTarget(table="gold.ns", mode=WriteMode.CREATE),
    )

    assert run.status == QueryRunStatus.FAILED
    assert run.error_message == (
        "Column 'ts' has type timestamp[ns, tz=UTC], which a Lakana table can't store; CAST it "
        "in the SELECT (for example to BIGINT, DOUBLE, VARCHAR or TIMESTAMP)."
    )
    assert not catalog.table_exists(principal, TableRef.from_dotted("dev", "gold.ns"))


def test_an_untyped_null_column_is_refused_with_the_cast_to_write(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """On DuckDB 2.0 `NULL AS x` exports as Arrow `null` (1.5 created an integer column)."""
    run = _run(
        session,
        principal,
        "SELECT 1 AS id, NULL AS x",
        WriteTarget(table="gold.untyped", mode=WriteMode.CREATE),
    )

    assert run.status == QueryRunStatus.FAILED
    assert run.error_message == (
        "Column 'x' is NULL without a type, which a Lakana table can't store; write "
        "`CAST(NULL AS <type>) AS x`, for example `CAST(NULL AS VARCHAR)`."
    )
    assert not catalog.table_exists(principal, TableRef.from_dotted("dev", "gold.untyped"))


def test_a_generate_series_alias_is_written_as_a_number_column(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """Characterisation: DuckDB 2.0 makes `i` the number, so the column is a `long`.

    On DuckDB 1.5 `i` was a struct, which a Lakana table refuses.
    """
    run = _run(
        session,
        principal,
        "SELECT i AS id FROM generate_series(1, 2) i",
        WriteTarget(table="gold.series", mode=WriteMode.CREATE),
    )

    assert run.status == QueryRunStatus.SUCCEEDED, run.error_message
    assert _rows(catalog, principal, "gold.series") == [{"id": 1}, {"id": 2}]


def test_a_write_run_records_query_completed_with_its_row_count(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """`QUERY_COMPLETED` still closes every run; its `row_count` is the rows written."""
    _seed(catalog, principal, "bronze.orders", _orders(), WriteMode.CREATE)

    run = _run(
        session,
        principal,
        "SELECT id, region FROM bronze.orders",
        WriteTarget(table="gold.orders", mode=WriteMode.CREATE),
    )

    completed = session.execute(
        select(AuditEvent).where(AuditEvent.event_type == AuditEventType.QUERY_COMPLETED)
    ).scalar_one()
    assert completed.detail["row_count"] == 3
    assert completed.detail["status"] == run.status.value


def _interleave(monkeypatch: pytest.MonkeyPatch, concurrent_write: Callable[[], None]) -> None:
    """Commit another writer's change after the run has read, before it writes.

    `stream_query` is where the run has finished resolving (and reading the
    watermark) and starts producing rows; the other writer lands there, as
    an overlapping scheduled run or an Arrow append would.

    Parameters
    ----------
    monkeypatch
        Pytest's monkeypatch.
    concurrent_write
        The other writer's commit.
    """
    real_stream_query = pipeline_module.stream_query

    def interleaved(sql: str, parameters: object, **kwargs: object) -> object:
        concurrent_write()
        return real_stream_query(sql, parameters, **kwargs)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(pipeline_module, "stream_query", interleaved)


def _ids(ids: list[int]) -> pa.Table:
    """Build rows shaped like `_orders`.

    Parameters
    ----------
    ids
        The `id` values; every region is ``"eu"``.

    Returns
    -------
    pa.Table
        The rows.
    """
    return pa.table(
        {"id": pa.array(ids, pa.int64()), "region": pa.array(["eu"] * len(ids), pa.string())}
    )


def test_an_incremental_append_conflicts_when_the_target_gains_rows_after_its_watermark(
    session: Session,
    principal: PrincipalRef,
    catalog: CatalogService,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """I1: A reads watermark 2, B appends 3 and 4, A must fail rather than append them again."""
    _seed(catalog, principal, "bronze.orders", _ids([1, 2, 3, 4, 5]), WriteMode.CREATE)
    _seed(catalog, principal, "gold.orders", _ids([1, 2]), WriteMode.CREATE)
    files_after_b: list[set[str]] = []

    def concurrent_append() -> None:
        _seed(catalog, principal, "gold.orders", _ids([3, 4]), WriteMode.APPEND)
        files_after_b.append(_data_files(tmp_path))

    _interleave(monkeypatch, concurrent_append)

    run = _run(
        session,
        principal,
        "SELECT id, region FROM bronze.orders",
        WriteTarget(table="gold.orders", mode=WriteMode.APPEND, incremental_key="id"),
    )

    assert run.status == QueryRunStatus.FAILED
    assert run.error_message == "gold.orders changed during the write; retry."
    assert [row["id"] for row in _rows(catalog, principal, "gold.orders")] == [1, 2, 3, 4]
    assert [_data_files(tmp_path)] == files_after_b


def test_an_incremental_append_into_an_empty_target_conflicts_with_a_concurrent_first_run(
    session: Session,
    principal: PrincipalRef,
    catalog: CatalogService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two first runs into an empty target must not both take every row."""
    _seed(catalog, principal, "bronze.orders", _ids([1, 2, 3]), WriteMode.CREATE)
    _seed(catalog, principal, "gold.orders", _ids([]), WriteMode.CREATE)
    _interleave(
        monkeypatch,
        lambda: _seed(catalog, principal, "gold.orders", _ids([1, 2, 3]), WriteMode.APPEND),
    )

    run = _run(
        session,
        principal,
        "SELECT id, region FROM bronze.orders",
        WriteTarget(table="gold.orders", mode=WriteMode.APPEND, incremental_key="id"),
    )

    assert run.status == QueryRunStatus.FAILED
    assert run.error_message == "gold.orders changed during the write; retry."
    assert [row["id"] for row in _rows(catalog, principal, "gold.orders")] == [1, 2, 3]


def test_a_replace_over_its_own_target_conflicts_with_a_concurrent_append(
    session: Session,
    principal: PrincipalRef,
    catalog: CatalogService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """I1: a refresh reading its own target must not delete a row it never read."""
    _seed(catalog, principal, "gold.daily", _ids([1, 2, 3]), WriteMode.CREATE)
    _interleave(
        monkeypatch,
        lambda: _seed(catalog, principal, "gold.daily", _ids([4]), WriteMode.APPEND),
    )

    run = _run(
        session,
        principal,
        "SELECT id, region FROM gold.daily WHERE id > 1",
        WriteTarget(table="gold.daily", mode=WriteMode.REPLACE),
    )

    assert run.status == QueryRunStatus.FAILED
    assert run.error_message == "gold.daily changed during the write; retry."
    assert [row["id"] for row in _rows(catalog, principal, "gold.daily")] == [1, 2, 3, 4]


def test_a_replace_reading_its_own_target_through_a_view_conflicts_with_a_concurrent_append(
    session: Session,
    principal: PrincipalRef,
    catalog: CatalogService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same, with the target read only through a view the query names."""
    _seed(catalog, principal, "gold.daily", _ids([1, 2, 3]), WriteMode.CREATE)
    view = _run(
        session,
        principal,
        "SELECT id, region FROM gold.daily",
        WriteTarget(table="silver.recent", mode=WriteMode.VIEW),
    )
    assert view.status == QueryRunStatus.SUCCEEDED, view.error_message
    _interleave(
        monkeypatch,
        lambda: _seed(catalog, principal, "gold.daily", _ids([4]), WriteMode.APPEND),
    )

    run = _run(
        session,
        principal,
        "SELECT id, region FROM silver.recent WHERE id > 1",
        WriteTarget(table="gold.daily", mode=WriteMode.REPLACE),
    )

    assert run.status == QueryRunStatus.FAILED
    assert run.error_message == "gold.daily changed during the write; retry."
    assert [row["id"] for row in _rows(catalog, principal, "gold.daily")] == [1, 2, 3, 4]


def test_an_incremental_self_read_conflicts_when_the_target_moves_before_the_watermark(
    session: Session,
    principal: PrincipalRef,
    catalog: CatalogService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resolution read one snapshot of the target and the watermark another: conflict."""
    _seed(catalog, principal, "gold.orders", _ids([1, 2]), WriteMode.CREATE)
    real_read_watermark = CatalogService.read_watermark

    def read_after_a_concurrent_append(self: CatalogService, *args: object) -> object:
        _seed(catalog, principal, "gold.orders", _ids([3]), WriteMode.APPEND)
        return real_read_watermark(self, *args)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(CatalogService, "read_watermark", read_after_a_concurrent_append)

    run = _run(
        session,
        principal,
        "SELECT id + 10 AS id, region FROM gold.orders",
        WriteTarget(table="gold.orders", mode=WriteMode.APPEND, incremental_key="id"),
    )

    assert run.status == QueryRunStatus.FAILED
    assert run.error_message == "gold.orders changed during the write; retry."
    assert [row["id"] for row in _rows(catalog, principal, "gold.orders")] == [1, 2, 3]


def test_a_plain_append_still_lands_beside_a_concurrent_append(
    session: Session,
    principal: PrincipalRef,
    catalog: CatalogService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An append that doesn't read its target keeps M5a's behaviour: both writers land."""
    _seed(catalog, principal, "bronze.orders", _ids([10, 11]), WriteMode.CREATE)
    _seed(catalog, principal, "gold.orders", _ids([1]), WriteMode.CREATE)
    _interleave(
        monkeypatch,
        lambda: _seed(catalog, principal, "gold.orders", _ids([2]), WriteMode.APPEND),
    )

    run = _run(
        session,
        principal,
        "SELECT id, region FROM bronze.orders",
        WriteTarget(table="gold.orders", mode=WriteMode.APPEND),
    )

    assert run.status == QueryRunStatus.SUCCEEDED, run.error_message
    assert [row["id"] for row in _rows(catalog, principal, "gold.orders")] == [1, 2, 10, 11]


def test_a_replace_that_does_not_read_its_target_lands_after_a_concurrent_append(
    session: Session,
    principal: PrincipalRef,
    catalog: CatalogService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Its result doesn't depend on the target, so B-then-A is a valid serial order."""
    _seed(catalog, principal, "bronze.orders", _ids([10, 11]), WriteMode.CREATE)
    _seed(catalog, principal, "gold.orders", _ids([1]), WriteMode.CREATE)
    _interleave(
        monkeypatch,
        lambda: _seed(catalog, principal, "gold.orders", _ids([2]), WriteMode.APPEND),
    )

    run = _run(
        session,
        principal,
        "SELECT id, region FROM bronze.orders",
        WriteTarget(table="gold.orders", mode=WriteMode.REPLACE),
    )

    assert run.status == QueryRunStatus.SUCCEEDED, run.error_message
    assert [row["id"] for row in _rows(catalog, principal, "gold.orders")] == [10, 11]


def _attach_secret(
    session: Session, catalog: CatalogService, principal: PrincipalRef, value: str
) -> EnvKeyProvider:
    """Back `dev`'s storage with a real encrypted secret holding `value`.

    Parameters
    ----------
    session
        Database session.
    catalog
        The catalog.
    principal
        The acting principal.
    value
        The `secret_access_key` value.

    Returns
    -------
    EnvKeyProvider
        The key provider the worker needs to decrypt it.
    """
    provider = EnvKeyProvider(base64.b64encode(b"0" * 32).decode())
    SecretService(session, AllowAllAuthorization(), provider).create(
        principal, environment="dev", name="creds", values={"secret_access_key": value}
    )
    catalog.update_environment_storage(principal, "dev", secret_ref="creds")
    return provider


def test_a_rewrite_failure_never_leaks_a_storage_credential(
    session: Session,
    principal: PrincipalRef,
    catalog: CatalogService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Final review minor 2: credentials are on the redaction list before the rewrite runs."""
    _seed(catalog, principal, "bronze.orders", _orders(), WriteMode.CREATE)
    provider = _attach_secret(session, catalog, principal, "REWRITE_SECRET_MUST_NOT_LEAK")

    def failing_rewrite(*args: object, **kwargs: object) -> object:
        raise ValidationError("Rewrite failed: secret_access_key=REWRITE_SECRET_MUST_NOT_LEAK")

    monkeypatch.setattr(pipeline_module, "rewrite_query", failing_rewrite)
    run = QueryService(session, AllowAllAuthorization()).submit_write(
        principal,
        "dev",
        "SELECT id, region FROM bronze.orders",
        WriteTarget(table="gold.orders", mode=WriteMode.CREATE),
    )

    run_one(session, run.id, AllowAllAuthorization(), provider)
    session.refresh(run)

    assert run.status == QueryRunStatus.FAILED
    assert run.error_message is not None
    assert "Rewrite failed:" in run.error_message
    assert "<redacted>" in run.error_message
    assert "REWRITE_SECRET_MUST_NOT_LEAK" not in run.error_message


def test_a_write_failure_never_leaks_a_storage_credential_or_a_resolved_url(
    session: Session,
    principal: PrincipalRef,
    catalog: CatalogService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure inside `write_batches` echoing a credential and a source URL is redacted."""
    _seed(catalog, principal, "bronze.orders", _orders(), WriteMode.CREATE)
    provider = _attach_secret(session, catalog, principal, "WRITE_SECRET_MUST_NOT_LEAK")
    urls: list[str] = []
    real_stream_query = pipeline_module.stream_query

    def spy(sql: str, parameters: dict[str, object], **kwargs: object) -> object:
        urls.extend(
            url for value in parameters.values() if isinstance(value, list) for url in value
        )
        return real_stream_query(sql, parameters, **kwargs)  # ty: ignore[invalid-argument-type]

    def failing_write(self: CatalogService, *args: object, **kwargs: object) -> None:
        raise RuntimeError(f"Upload failed: {urls[0]} secret_access_key=WRITE_SECRET_MUST_NOT_LEAK")

    monkeypatch.setattr(pipeline_module, "stream_query", spy)
    monkeypatch.setattr(CatalogService, "write_batches", failing_write)
    run = QueryService(session, AllowAllAuthorization()).submit_write(
        principal,
        "dev",
        "SELECT id, region FROM bronze.orders",
        WriteTarget(table="gold.orders", mode=WriteMode.CREATE),
    )

    run_one(session, run.id, AllowAllAuthorization(), provider)
    session.refresh(run)

    assert urls
    assert run.status == QueryRunStatus.FAILED
    assert run.error_message is not None
    assert "Upload failed:" in run.error_message
    assert "WRITE_SECRET_MUST_NOT_LEAK" not in run.error_message
    assert urls[0] not in run.error_message


def test_a_storage_resolution_failure_reports_only_its_class_name(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """Resolving credentials fails before any of them is on the redaction list: stay opaque."""
    _seed(catalog, principal, "bronze.orders", _orders(), WriteMode.CREATE)
    _attach_secret(session, catalog, principal, "UNRESOLVED_SECRET_VALUE")

    run = QueryService(session, AllowAllAuthorization()).submit_write(
        principal,
        "dev",
        "SELECT id, region FROM bronze.orders",
        WriteTarget(table="gold.orders", mode=WriteMode.CREATE),
    )
    run_one(session, run.id, AllowAllAuthorization())
    session.refresh(run)

    assert run.status == QueryRunStatus.FAILED
    assert run.error_message is not None
    assert run.error_message.startswith("Resolving this environment's storage failed (")


def test_a_write_run_past_the_time_limit_fails_and_deletes_its_files(
    session: Session, principal: PrincipalRef, catalog: CatalogService, tmp_path: Path
) -> None:
    """An interrupted write run fails with the time-limit message, leaving no files."""
    big = pa.table({"id": pa.array(range(400_000), pa.int64())})
    _seed(catalog, principal, "bronze.big", big, WriteMode.CREATE)
    before = _data_files(tmp_path)

    run = _run(
        session,
        principal,
        "SELECT id FROM bronze.big UNION ALL "
        "SELECT CAST(sum(hash(a.id + b.id)) % 7 AS BIGINT) FROM bronze.big a, bronze.big b",
        WriteTarget(table="gold.big", mode=WriteMode.CREATE),
        limits=QueryLimitsConfig(timeout_seconds=1),
    )

    assert run.status == QueryRunStatus.FAILED
    assert "Query exceeded the time limit" in (run.error_message or "")
    assert _data_files(tmp_path) == before


@pytest.mark.parametrize(
    ("behaviour", "status"),
    [
        ("no_end_marker", "exit status 0"),
        ("exit_7_after_a_full_stream", "exit status 7"),
    ],
)
def test_a_write_whose_stream_is_not_complete_commits_nothing(
    session: Session,
    principal: PrincipalRef,
    catalog: CatalogService,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    behaviour: str,
    status: str,
) -> None:
    """Security: 30 rows arrive and look like a result; without proof they are not one.

    Without the end marker, or with a failing exit status after a full stream, the batches have
    reached the writer before the supervisor can judge them. The iterator then raises instead
    of ending, so the write never reaches its commit.
    """
    script = Path(__file__).parent / "scripted_child.py"
    _seed(catalog, principal, "bronze.orders", _orders(), WriteMode.CREATE)
    before = _data_files(tmp_path)
    monkeypatch.setattr(executor_module, "_ARGV", [sys.executable, str(script), behaviour])

    run = _run(
        session,
        principal,
        "SELECT 1 AS i",
        WriteTarget(table="gold.partial", mode=WriteMode.CREATE),
    )

    assert run.status == QueryRunStatus.FAILED
    assert run.error_cause == "internal"
    assert run.error_message == (
        f"Query failed: the query process ended without a complete result ({status}). "
        "Nothing was stored. Run the query again; if it fails the same way, tell the operator: "
        "the worker's log has the details."
    )
    assert (run.rows_written, run.snapshot_id, run.target_table_id) == (None, None, None)
    assert not catalog.table_exists(principal, TableRef.from_dotted("dev", "gold.partial"))
    assert _data_files(tmp_path) == before


@pytest.mark.parametrize("mode", [WriteMode.CREATE, WriteMode.REPLACE, WriteMode.APPEND])
def test_a_write_whose_child_is_killed_after_its_last_batch_commits_nothing(
    session: Session,
    principal: PrincipalRef,
    catalog: CatalogService,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: WriteMode,
) -> None:
    """R3.8: every batch and the end marker arrived; a signal from outside then ended the child.

    The run is a failure a retry may cure (cause `internal`, the message names the signal), and
    neither a snapshot nor a data file remains. A replace and an append go through the other
    branch of `write_batches` than a create, and a replace is where a partial result would hurt
    most: the target keeps its snapshot.
    """
    script = Path(__file__).parent / "scripted_child.py"
    seeded = pa.table({"i": pa.array([1, 2, 3, 4, 5], pa.int64())})
    target = TableRef.from_dotted("dev", "gold.partial")
    snapshot: int | None = None
    if mode is not WriteMode.CREATE:
        _seed(catalog, principal, "gold.partial", seeded, WriteMode.CREATE)
        snapshot = catalog.load_table(principal, target).metadata.current_snapshot_id
    before = _data_files(tmp_path)
    monkeypatch.setattr(
        executor_module, "_ARGV", [sys.executable, str(script), "full_stream_then_killed"]
    )

    run = _run(session, principal, "SELECT 1 AS i", WriteTarget(table="gold.partial", mode=mode))

    assert run.status == QueryRunStatus.FAILED
    assert run.error_cause == "internal"
    assert run.error_message is not None
    assert run.error_message.startswith(
        "Query failed: the query process was ended from outside the worker (signal 9)."
    )
    assert (run.rows_written, run.snapshot_id) == (None, None)
    assert _data_files(tmp_path) == before
    if mode is WriteMode.CREATE:
        assert run.target_table_id is None
        assert not catalog.table_exists(principal, target)
    else:
        assert catalog.load_table(principal, target).metadata.current_snapshot_id == snapshot


def _claimed_write(
    session: Session, principal: PrincipalRef, sql: str, target: WriteTarget
) -> QueryRun:
    """Submit a write run for `run_one` to execute.

    Parameters
    ----------
    session
        Database session.
    principal
        The submitter.
    sql
        The SELECT.
    target
        Where it goes.

    Returns
    -------
    QueryRun
        The queued run.
    """
    return QueryService(session, AllowAllAuthorization()).submit_write(
        principal, "dev", sql, target
    )


def test_a_write_run_is_executed_under_its_fence(
    session: Session,
    principal: PrincipalRef,
    catalog: CatalogService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Security: the principal the write runs as names the run and the worker that holds it."""
    seen: list[WriteFence | None] = []
    real = ProtectedWriteGuard.check

    def spy(
        guard: ProtectedWriteGuard,
        p: PrincipalRef,
        environment: str,
        operation: WriteOperation,
        target: Target,
        *,
        record_break_glass: bool = True,
    ) -> None:
        seen.append(p.write_fence)
        real(guard, p, environment, operation, target, record_break_glass=record_break_glass)

    monkeypatch.setattr(ProtectedWriteGuard, "check", spy)
    run = _claimed_write(
        session,
        principal,
        "SELECT 1 AS id",
        WriteTarget(table="gold.fenced", mode=WriteMode.CREATE),
    )
    seen.clear()  # submission checked as the submitter, who carries no fence
    assert run_one(session, run.id, AllowAllAuthorization(), worker_id="w1") is None
    assert seen and set(seen) == {WriteFence(run.id, "w1")}


def test_a_write_whose_run_was_ended_while_it_streamed_commits_nothing_and_keeps_the_stamp(
    session: Session,
    principal: PrincipalRef,
    catalog: CatalogService,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Security: a worker that wakes after its run was ended writes nothing and stamps nothing."""
    _seed(catalog, principal, "gold.orders", _orders(), WriteMode.CREATE)
    before = catalog.load_table(principal, TableRef.from_dotted("dev", "gold.orders"))
    run = _claimed_write(
        session,
        principal,
        "SELECT id, region FROM gold.orders",
        WriteTarget(table="gold.orders", mode=WriteMode.REPLACE),
    )
    real = pipeline_module.stream_query
    engine = session.get_bind()

    @contextlib.contextmanager
    def ended_meanwhile(
        sql: str,
        parameters: Mapping[str, object] | None = None,
        *,
        read_prefixes: Sequence[str] = (),
        spill: bool = False,
        limits: QueryLimitsConfig | None = None,
    ) -> Generator[tuple[pa.Schema, Iterator[pa.RecordBatch]]]:
        with real(
            sql, parameters, read_prefixes=read_prefixes, spill=spill, limits=limits
        ) as streamed:
            with Session(engine) as other:
                other.execute(
                    update(QueryRun)
                    .where(QueryRun.id == run.id)
                    .values(
                        status=QueryRunStatus.FAILED,
                        error_message="ended by the sweep",
                        error_cause=QueryErrorCause.WORKER_LOST.value,
                    )
                )
                other.commit()
            yield streamed

    monkeypatch.setattr(pipeline_module, "stream_query", ended_meanwhile)
    with caplog.at_level(logging.WARNING, logger="lakana_worker.pipeline"):
        failure = run_one(session, run.id, AllowAllAuthorization(), worker_id="w1")
    (record,) = [r for r in caplog.records if "refused" in r.getMessage()]
    assert record.getMessage() == (
        f"Query run {run.id} was ended while this worker was not seen alive, so its write was "
        "refused and nothing was committed."
    )
    assert isinstance(failure, PermissionDenied)
    assert str(failure) == WRITE_FENCE_MESSAGE
    assert pipeline_module.failure_cause(failure) is QueryErrorCause.WORKER_LOST
    session.expire_all()
    stored = session.get(QueryRun, run.id)
    assert stored is not None
    assert (stored.status, stored.error_message, stored.error_cause) == (
        QueryRunStatus.FAILED,
        "ended by the sweep",
        "worker_lost",
    )
    assert (stored.rows_written, stored.snapshot_id) == (None, None)
    after = catalog.load_table(principal, TableRef.from_dotted("dev", "gold.orders"))
    assert after.metadata_location == before.metadata_location
    assert _rows(catalog, principal, "gold.orders") == _rows_of(_orders())


def _rows_of(data: pa.Table) -> list[dict[str, object]]:
    """Return a table's rows sorted by `id`.

    Parameters
    ----------
    data
        The table.

    Returns
    -------
    list of dict
        The rows.
    """
    return sorted(data.to_pylist(), key=lambda row: row["id"])


def test_a_successful_write_has_its_evidence_before_the_status_is_stamped(
    session: Session,
    principal: PrincipalRef,
    catalog: CatalogService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Read on a second connection: `_run_write` sets the numbers on the ORM object too.

    The commit's own transaction must have recorded them (the sweep and the memory stop read
    them when the worker dies right after the commit), so the proof is what is committed.
    """
    run = _claimed_write(
        session,
        principal,
        "SELECT 1 AS id UNION ALL SELECT 2",
        WriteTarget(table="gold.evidence", mode=WriteMode.CREATE),
    )
    committed: list[tuple[object, ...]] = []
    real = pipeline_module._keep_a_prior_stamp
    engine = session.get_bind()

    def read_then_stamp(
        stamp_session: Session,
        stamped: QueryRun,
        outcome: QueryRunStatus,
        *,
        worker_id: str | None = None,
    ) -> bool:
        with Session(engine) as other:
            row = other.execute(
                select(
                    QueryRun.status,
                    QueryRun.rows_written,
                    QueryRun.snapshot_id,
                    QueryRun.target_table_id,
                ).where(QueryRun.id == run.id)
            ).one()
            committed.append(tuple(row))
        return real(stamp_session, stamped, outcome, worker_id=worker_id)

    monkeypatch.setattr(pipeline_module, "_keep_a_prior_stamp", read_then_stamp)
    assert run_one(session, run.id, AllowAllAuthorization(), worker_id="w1") is None
    loaded = catalog.load_table(principal, TableRef.from_dotted("dev", "gold.evidence"))
    assert committed == [
        (QueryRunStatus.RUNNING, 2, loaded.metadata.current_snapshot_id, loaded.uuid)
    ]


@pytest.mark.parametrize("error", [RuntimeError, WriteOutcomeUnknownError])
def test_a_write_that_committed_and_then_failed_ends_succeeded_with_one_event(
    session: Session,
    principal: PrincipalRef,
    catalog: CatalogService,
    monkeypatch: pytest.MonkeyPatch,
    error: type[Exception],
) -> None:
    """The evidence is in the run: the outcome is SUCCEEDED, never FAILED over rows in the table.

    `WriteOutcomeUnknownError` is the junction case: the acknowledgement was lost, the check
    failed too, and the database was back by the time the run is stamped.
    """
    original = pipeline_module._run_write

    def committed_then_fails(*args: object, **kwargs: object) -> int:
        original(*args, **kwargs)  # ty: ignore[invalid-argument-type]
        raise error("after the commit")

    monkeypatch.setattr(pipeline_module, "_run_write", committed_then_fails)
    run = _run(
        session, principal, "SELECT 1::BIGINT AS id", WriteTarget("gold.daily", WriteMode.CREATE)
    )

    assert (run.status, run.error_message, run.error_cause, run.rows_written) == (
        QueryRunStatus.SUCCEEDED,
        None,
        None,
        1,
    )
    assert _rows(catalog, principal, "gold.daily") == [{"id": 1}]
    events = session.scalars(
        select(AuditEvent).where(AuditEvent.event_type == AuditEventType.QUERY_COMPLETED)
    ).all()
    assert [(e.detail["status"], e.detail["row_count"]) for e in events] == [("SUCCEEDED", 1)]


def test_a_write_that_failed_before_the_commit_still_fails(
    session: Session,
    principal: PrincipalRef,
    catalog: CatalogService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No evidence, no change."""

    def fails(*args: object, **kwargs: object) -> int:
        raise ValidationError("refused")

    monkeypatch.setattr(pipeline_module, "_run_write", fails)
    run = _run(
        session, principal, "SELECT 1::BIGINT AS id", WriteTarget("gold.daily", WriteMode.CREATE)
    )
    assert run.status == QueryRunStatus.FAILED


def _customers(ids: list[int], names: list[str], days: list[int]) -> pa.Table:
    """Build customer rows with an `updated_at` day of January 2026.

    Parameters
    ----------
    ids
        The keys.
    names
        A name per key.
    days
        The day of `updated_at` per key.

    Returns
    -------
    pa.Table
        The rows.
    """
    return pa.table(
        {
            "id": pa.array(ids, pa.int64()),
            "name": pa.array(names, pa.string()),
            "updated_at": pa.array([dt.datetime(2026, 1, day) for day in days], pa.timestamp("us")),
        }
    )


def _merge_run(
    session: Session,
    principal: PrincipalRef,
    sql: str,
    table: str,
    *,
    watermark: str | None = None,
    limits: QueryLimitsConfig | None = None,
) -> QueryRun:
    """Run what a task's later run of a `write: merge` asset is: a merge on `id`.

    `WriteTarget` refuses `merge`, which only assets and `tables.merge` write; the worker builds
    the write run of a task itself, as `_first_incremental_run` does for a create.

    Parameters
    ----------
    session
        Database session.
    principal
        The submitter.
    sql
        The SELECT.
    table
        The target, which exists.
    watermark
        The watermark column, if the asset has one.
    limits
        The worker's limits.

    Returns
    -------
    QueryRun
        The finished run, refreshed.
    """
    run = QueryService(session, AllowAllAuthorization()).submit_write(
        principal, "dev", sql, WriteTarget(table=table, mode=WriteMode.REPLACE)
    )
    run.write_mode = WriteMode.MERGE
    run.merge_key = ["id"]
    run.incremental_key = watermark
    session.commit()
    run_one(session, run.id, AllowAllAuthorization(), limits=limits)
    session.refresh(run)
    return run


def _by_id(catalog: CatalogService, principal: PrincipalRef, dotted: str) -> list[tuple]:
    """Read `(id, name)` of every row, sorted.

    Parameters
    ----------
    catalog
        The catalog.
    principal
        The acting principal.
    dotted
        The table.

    Returns
    -------
    list of tuple
        The rows.
    """
    return [(row["id"], row["name"]) for row in _rows(catalog, principal, dotted)]


def test_a_merge_run_updates_matching_rows_and_inserts_the_rest_without_a_pin(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """A merge that neither reads its target nor has a watermark commits unpinned."""
    _seed(
        catalog,
        principal,
        "gold.customers",
        _customers([1, 2], ["a", "b"], [1, 2]),
        WriteMode.CREATE,
    )
    _seed(
        catalog,
        principal,
        "bronze.customers",
        _customers([2, 3], ["B", "c"], [3, 4]),
        WriteMode.CREATE,
    )

    run = _merge_run(
        session, principal, "SELECT id, name, updated_at FROM bronze.customers", "gold.customers"
    )

    assert run.status == QueryRunStatus.SUCCEEDED, run.error_message
    assert _by_id(catalog, principal, "gold.customers") == [(1, "a"), (2, "B"), (3, "c")]
    loaded = catalog.load_table(principal, TableRef.from_dotted("dev", "gold.customers"))
    assert (run.rows_written, run.snapshot_id, run.target_table_id, run.watermark) == (
        2,
        loaded.metadata.current_snapshot_id,
        loaded.uuid,
        None,
    )
    write = (
        session.execute(
            select(AuditEvent).where(
                AuditEvent.event_type == AuditEventType.TABLE_WRITE,
                AuditEvent.detail["target"].as_string() == "gold.customers",
            )
        )
        .scalars()
        .all()[-1]
    )
    assert (write.detail["mode"], write.detail["run_id"], write.detail["key"]) == (
        "merge",
        str(run.id),
        ["id"],
    )
    assert (write.detail["rows_inserted"], write.detail["rows_updated"]) == (1, 1)
    edge = session.execute(
        select(LineageEdge).where(LineageEdge.query_run_id == run.id)
    ).scalar_one()
    assert (edge.source_name, edge.target_name) == ("bronze.customers", "gold.customers")


def test_a_merge_with_a_watermark_takes_only_rows_past_it(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """Row 1 changed in the source before the watermark: the wrapped SELECT leaves it out."""
    _seed(
        catalog,
        principal,
        "gold.customers",
        _customers([1, 2], ["a", "b"], [1, 2]),
        WriteMode.CREATE,
    )
    source = _customers([1, 2, 3], ["x", "B", "c"], [1, 3, 4])
    _seed(catalog, principal, "bronze.customers", source, WriteMode.CREATE)

    run = _merge_run(
        session,
        principal,
        "SELECT id, name, updated_at FROM bronze.customers",
        "gold.customers",
        watermark="updated_at",
    )

    assert run.status == QueryRunStatus.SUCCEEDED, run.error_message
    assert run.watermark == "timestamp:2026-01-02T00:00:00"
    assert run.rows_written == 2
    assert _by_id(catalog, principal, "gold.customers") == [(1, "a"), (2, "B"), (3, "c")]


def test_a_merge_conflicts_when_its_target_moves_after_the_watermark_was_read(
    session: Session,
    principal: PrincipalRef,
    catalog: CatalogService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The merge commits against the watermark's pin; a writer in between makes it retry."""
    _seed(catalog, principal, "gold.customers", _customers([1], ["a"], [1]), WriteMode.CREATE)
    _seed(catalog, principal, "bronze.customers", _customers([2], ["b"], [2]), WriteMode.CREATE)
    _interleave(
        monkeypatch,
        lambda: _seed(
            catalog, principal, "gold.customers", _customers([9], ["z"], [9]), WriteMode.APPEND
        ),
    )

    run = _merge_run(
        session,
        principal,
        "SELECT id, name, updated_at FROM bronze.customers",
        "gold.customers",
        watermark="updated_at",
    )

    assert run.status == QueryRunStatus.FAILED
    assert run.error_message == "gold.customers changed during the write; retry."
    assert _by_id(catalog, principal, "gold.customers") == [(1, "a"), (9, "z")]


def test_a_merge_of_more_rows_than_max_write_rows_writes_nothing(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    _seed(catalog, principal, "gold.customers", _customers([1], ["a"], [1]), WriteMode.CREATE)
    _seed(
        catalog,
        principal,
        "bronze.customers",
        _customers([2, 3], ["b", "c"], [2, 3]),
        WriteMode.CREATE,
    )
    before = catalog.load_table(principal, TableRef.from_dotted("dev", "gold.customers"))

    run = _merge_run(
        session,
        principal,
        "SELECT id, name, updated_at FROM bronze.customers",
        "gold.customers",
        limits=QueryLimitsConfig(max_write_rows=1),
    )

    assert run.status == QueryRunStatus.FAILED
    assert run.error_message == "The write exceeds max_write_rows (1); nothing was committed."
    after = catalog.load_table(principal, TableRef.from_dotted("dev", "gold.customers"))
    assert after.metadata_location == before.metadata_location


def test_a_merge_that_reads_its_own_target_commits_against_the_snapshot_it_read(
    session: Session,
    principal: PrincipalRef,
    catalog: CatalogService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed(catalog, principal, "gold.customers", _customers([1], ["a"], [1]), WriteMode.CREATE)
    _interleave(
        monkeypatch,
        lambda: _seed(
            catalog, principal, "gold.customers", _customers([2], ["b"], [2]), WriteMode.APPEND
        ),
    )

    run = _merge_run(
        session,
        principal,
        "SELECT id, upper(name) AS name, updated_at FROM gold.customers",
        "gold.customers",
    )

    assert run.status == QueryRunStatus.FAILED
    assert run.error_message == "gold.customers changed during the write; retry."
    assert _by_id(catalog, principal, "gold.customers") == [(1, "a"), (2, "b")]
