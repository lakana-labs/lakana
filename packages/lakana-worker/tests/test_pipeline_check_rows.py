"""The rows a check found, read by a query bound to the check result.

Real catalog, real files, real grants, row filters and column masks, and the query runs in a
query child: the worker reads the one table the result names at the checked snapshot and
every other table as it is now.
"""

# ruff: file-ignore[redefined-while-unused]  (each test that takes the imported `admin` fixture)

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.ipc
import pytest
from policy_read_helpers import (
    ORDERS,
    admin,  # ruff: ignore[unused-import]
    catalog,
    filter,
    filter_eu,
    grant_read,
    mask,
    mask_as,
    orders,
    reader,
    run,
    write,
)
from pyiceberg.schema import Schema
from pyiceberg.table.update import (
    AddSchemaUpdate,
    RemoveSnapshotRefUpdate,
    SetCurrentSchemaUpdate,
)
from pyiceberg.types import LongType, NestedField, StringType, StructType
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from lakana_core.auth import (
    Action,
    AllowAllAuthorization,
    AuthorizationService,
    PrincipalRef,
    SecurableKind,
)
from lakana_core.catalog.identity import resolve_environment_id, resolve_table_id
from lakana_core.catalog.metadata import read_metadata
from lakana_core.catalog.query_service import QueryService, WriteTarget
from lakana_core.catalog.service import failed_snapshot_tag
from lakana_core.catalog.writes import SnapshotPin
from lakana_core.config import StorageConfig
from lakana_core.errors import (
    CheckedSnapshotGoneError,
    CheckResultGoneError,
    NoCheckedSnapshotError,
    PermissionDenied,
    ValidationError,
)
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.grants.service import GrantService
from lakana_core.identifiers import TableRef
from lakana_core.models import (
    AuditEvent,
    AuditEventType,
    CheckOutcome,
    CheckResult,
    ColumnMask,
    Environment,
    JobVersion,
    Principal,
    QueryErrorCause,
    QueryRun,
    QueryRunStatus,
    Repository,
    RepositoryKind,
    RollbackOutcome,
    RowFilter,
    Run,
    RunKind,
    RunStatus,
    Table,
    TaskRun,
    TaskStatus,
    WriteMode,
)
from lakana_core.policy.column_masks import ColumnMaskService
from lakana_core.policy.field_binding import PENDING, UNBOUND
from lakana_core.policy.read_errors import policy_read_failure
from lakana_core.policy.renames import policy_column_renamed_message
from lakana_core.policy.row_filters import RowFilterService
from lakana_core.projects.checks import CheckSpec
from lakana_core.query_rewrite import TableSource, rewrite_query
from lakana_core.runs.check_rows import submit_check_rows
from lakana_core.runs.checks import policy_needs_a_column_message, record_result
from lakana_core.runs.tasks import Written
from lakana_core.secrets.keys import KeyProvider
from lakana_core.storage.resolver import StorageResolver
from lakana_worker import pipeline
from lakana_worker.pipeline import (
    ReadOutcome,
    _columns_the_table_still_has,
    _resolve_sources,
    _schema_mismatch_failure,
    _withheld_column_failure,
    failure_cause,
    run_one,
)

_NEGATIVE = "SELECT * FROM gold.orders WHERE id < 0"
"""A check that finds orders with a negative id."""


_GOOD = orders([1], ["eu"])
"""What the table holds before and after the failed run: one good row."""

_BAD = orders([2, -1, -2], ["eu", "eu", "us"])
"""What the failed run wrote: three rows, two of which the check finds."""


def _table_id(session: Session, name: str = "orders") -> uuid.UUID:
    """Read the id of a table of `gold`, afresh.

    Parameters
    ----------
    session
        Database session.
    name
        The table's name.

    Returns
    -------
    uuid.UUID
        The row's id.
    """
    session.rollback()
    return session.execute(select(Table.id).where(Table.name == name)).scalar_one()


def _checked(
    session: Session,
    admin: PrincipalRef,
    *,
    snapshot_id: int,
    sql: str = _NEGATIVE,
    outcome: CheckOutcome = CheckOutcome.FAILED,
) -> CheckResult:
    """Record the result of a check of `gold.orders` for a new run, as the worker does.

    Parameters
    ----------
    session
        Database session; commits.
    admin
        The run's principal.
    snapshot_id
        The snapshot the check read.
    sql
        The check's SQL, stored in the manifest of the run's project version.
    outcome
        How the check ended.

    Returns
    -------
    CheckResult
        The stored result of ``gold.orders/no_negative_ids``.
    """
    spec = CheckSpec(
        name="gold.orders/no_negative_ids",
        table="gold.orders",
        check="no_negative_ids",
        path="checks/gold/orders/no_negative_ids.sql",
        on_failure="block",
        description=None,
        sql=sql,
    )
    environment_id = session.execute(
        select(Environment.id).where(Environment.name == "dev")
    ).scalar_one()
    repo = Repository(
        id=uuid.uuid4(),
        name=f"r{uuid.uuid4().hex[:6]}",
        kind=RepositoryKind.HTTPS,
        url="https://h/a.git",
    )
    session.add(repo)
    session.flush()
    version = JobVersion(
        id=uuid.uuid4(),
        repository_id=repo.id,
        environment_id=environment_id,
        commit_sha="a" * 40,
        manifest_json={
            "version": 4,
            "assets": [{"table": "gold.orders", "kind": "sql", "sql": ""}],
            "edges": [],
            "checks": [
                {
                    "name": spec.name,
                    "table": spec.table,
                    "check": spec.check,
                    "path": spec.path,
                    "on_failure": spec.on_failure,
                    "sql": spec.sql,
                }
            ],
        },
        code_uri="file:///x",
        code_sha256="0" * 64,
    )
    session.add(version)
    session.flush()
    run = Run(
        id=uuid.uuid4(),
        environment_id=environment_id,
        repository_id=repo.id,
        job_version_id=version.id,
        run_as_principal_id=admin.id,
        kind=RunKind.FULL,
        status=RunStatus.RUNNING,
        selection_json={},
    )
    session.add(run)
    session.flush()
    task = TaskRun(
        id=uuid.uuid4(),
        run_id=run.id,
        asset="gold.orders",
        status=TaskStatus.RUNNING,
        claim_count=1,
        attempt=1,
        lease_owner="w1",
        max_attempts=3,
        timeout_seconds=60,
        retry_delay_seconds=0,
        upstream_json=[],
        has_checks=True,
    )
    session.add(task)
    session.commit()
    assert record_result(
        session,
        task_run_id=task.id,
        worker="w1",
        claim=1,
        spec=spec,
        outcome=outcome,
        failing_rows=2,
        error=None,
        table_id=_table_id(session),
        checked_snapshot_id=snapshot_id,
        policy_applied=False,
        query_run_id=None,
        started_at=datetime(2026, 10, 12, 9, 0, tzinfo=UTC),
    )
    return session.scalars(select(CheckResult).where(CheckResult.task_run_id == task.id)).one()


def _failed_run(
    session: Session,
    admin: PrincipalRef,
    *,
    bad: pa.Table = _BAD,
    first_build: bool = False,
    sql: str = _NEGATIVE,
) -> CheckResult:
    """Let a run write failing rows into `gold.orders`, fail its check and be rolled back.

    Parameters
    ----------
    session
        Database session; commits.
    admin
        The run's principal.
    bad
        The rows the run writes; a column the table lacks is added to its schema.
    first_build
        The run creates the table (it is not published afterwards); else the table holds
        `_GOOD` before and after.
    sql
        The check's SQL.

    Returns
    -------
    CheckResult
        The failed result, whose checked snapshot is the one the run wrote.
    """
    before = None if first_build else write(session, admin, _GOOD, WriteMode.CREATE)
    after = write(
        session,
        admin,
        bad,
        WriteMode.CREATE if first_build else WriteMode.REPLACE,
        evolve_schema=not first_build,
    )
    result = _checked(session, admin, snapshot_id=after, sql=sql)
    rolled = catalog(session).roll_back(
        admin,
        ORDERS,
        run_id=result.run_id,
        task_run_id=result.task_run_id,
        written=Written(_table_id(session), before, after, publishes=first_build),
        failed_checks=[result.check_name],
    )
    assert rolled.outcome is (
        RollbackOutcome.UNPUBLISHED if first_build else RollbackOutcome.ROLLED_BACK
    )
    return session.scalars(select(CheckResult).where(CheckResult.id == result.id)).one()


def _submit(session: Session, reader: PrincipalRef, result: CheckResult) -> uuid.UUID:
    """Queue the query that shows the rows of a check, as the route will.

    Parameters
    ----------
    session
        Database session.
    reader
        The caller.
    result
        The check result to read the rows of.

    Returns
    -------
    uuid.UUID
        The id of the queued query run.
    """
    return submit_check_rows(
        session,
        AllowAllAuthorization(),
        reader,
        environment="dev",
        run_id=result.run_id,
        check=result.check_name,
    ).id


def _rows(
    session: Session, tmp_path: Path, reader: PrincipalRef, result: CheckResult
) -> list[dict[str, object]]:
    """Show a reader the rows of a check and require that the query succeeded.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        The storage root.
    reader
        The caller.
    result
        The check result.

    Returns
    -------
    list of dict
        The rows, ordered by `id`.
    """
    finished, rows = run(session, tmp_path, _submit(session, reader, result))
    assert (finished.status, finished.error_message) == (QueryRunStatus.SUCCEEDED, None)
    return rows


def _failure(
    session: Session, tmp_path: Path, reader: PrincipalRef, result: CheckResult
) -> tuple[str | None, str | None]:
    """Show a reader the rows of a check and require that the query failed.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        The storage root.
    reader
        The caller.
    result
        The check result.

    Returns
    -------
    tuple
        The failed run's message and cause.
    """
    finished, rows = run(session, tmp_path, _submit(session, reader, result))
    assert (finished.status, rows, finished.result_location) == (QueryRunStatus.FAILED, [], None)
    return finished.error_message, finished.error_cause


def _gone(run_id: uuid.UUID) -> str:
    """Write out the text of a checked snapshot that is gone from `gold.orders`.

    Parameters
    ----------
    run_id
        The run of the check result.

    Returns
    -------
    str
        The whole text a user reads.
    """
    return (
        "The rows of this check can no longer be read: the snapshot it checked is no longer in "
        "`gold.orders`. Maintenance removes a failed snapshot after 7 days, and a table that is "
        "dropped and built again starts anew. The number of rows the check found remains: "
        f"`lakana check list --run {run_id} --env dev`."
    )


# --- what is read ---


def test_the_rows_are_those_of_the_checked_snapshot(
    session: Session, admin: PrincipalRef, tmp_path: Path
) -> None:
    """The failed run's rows are shown although the table was set back to one good row."""
    result = _failed_run(session, admin)
    ana = reader(session, admin, "ana", "orders")

    rows = _rows(session, tmp_path, ana, result)

    assert rows == [
        {"id": -2, "region": "us", "email": "o-2@example.org"},
        {"id": -1, "region": "eu", "email": "o-1@example.org"},
    ]
    # The same SQL as an ordinary query reads the table as it is now: no such row.
    ordinary = QueryService(session, AllowAllAuthorization()).submit_query(ana, "dev", _NEGATIVE)
    finished, today = run(session, tmp_path, ordinary.id)
    assert (finished.status, today) == (QueryRunStatus.SUCCEEDED, [])


def test_only_the_checked_table_is_read_at_the_snapshot(
    session: Session, admin: PrincipalRef, tmp_path: Path
) -> None:
    """A second table the check joins changed since: its rows are today's."""
    limits = TableRef.from_dotted("dev", "gold.limits")
    floor = pa.table({"floor": pa.array([0], pa.int64())})
    then = write(session, admin, floor, WriteMode.CREATE, ref=limits)
    result = _failed_run(
        session,
        admin,
        sql="SELECT o.id, l.floor FROM gold.orders o JOIN gold.limits l ON o.id < l.floor",
    )
    lower = pa.table({"floor": pa.array([-1], pa.int64())})
    now = write(session, admin, lower, WriteMode.REPLACE, ref=limits)
    assert now != then
    ana = reader(session, admin, "ana", "orders", "limits")

    rows = _rows(session, tmp_path, ana, result)

    # With the floor of the time of the check (0) both negative ids would show.
    assert rows == [{"id": -2, "floor": -1}]


def test_the_snapshot_that_was_read_is_what_the_query_records(
    session: Session, admin: PrincipalRef, tmp_path: Path
) -> None:
    """The snapshot recorded per table is the checked one for the checked table, else today's."""
    limits = TableRef.from_dotted("dev", "gold.limits")
    current = write(
        session, admin, pa.table({"floor": pa.array([0], pa.int64())}), WriteMode.CREATE, ref=limits
    )
    result = _failed_run(
        session,
        admin,
        sql="SELECT o.id, l.floor FROM gold.orders o JOIN gold.limits l ON o.id < l.floor",
    )
    ana = reader(session, admin, "ana", "orders", "limits")
    seen: list[ReadOutcome] = []

    failure = run_one(
        session, _submit(session, ana, result), GrantAuthorization(session), on_result=seen.append
    )

    assert failure is None
    assert seen[0].snapshots == {
        "gold.orders": result.checked_snapshot_id,
        "gold.limits": current,
    }


# --- which columns are shown ---


def _not_shown(column: str, run_id: uuid.UUID) -> str:
    """Write out the text for a column of the checked snapshot that is not shown.

    Parameters
    ----------
    column
        The column's name.
    run_id
        The run of the check result.

    Returns
    -------
    str
        The whole text a user reads, for `gold.orders` in `dev`.
    """
    return (
        f"The column `{column}` of the checked snapshot is not shown: `gold.orders` does not "
        "have it in this form any more (it was added by the run that failed, or dropped or "
        "changed since), so the rows this check found cannot be listed with it. The number of "
        f"rows the check found remains: `lakana check list --run {run_id} --env dev`."
    )


def _column_gone(table: str, column: str) -> str:
    """Write out the refusal of a read whose bound policy's column is gone.

    Parameters
    ----------
    table
        The table's dotted name.
    column
        The policy's column, as written.

    Returns
    -------
    str
        The whole text a user reads.
    """
    return (
        f"`{table}` has a row filter or column mask on a column that was dropped or replaced "
        f"(created for `{column}`); an administrator creates it again for the column it is "
        "meant for, or revokes it"
    )


def _unbind(session: Session, policy_id: uuid.UUID, binding: str) -> None:
    """Make a policy one of an earlier release: not bound to field ids.

    Parameters
    ----------
    session
        Database session.
    policy_id
        The row filter's or column mask's id.
    binding
        `pending` or `unbound`.
    """
    row = session.get(ColumnMask, policy_id) or session.get(RowFilter, policy_id)
    assert row is not None  # nosec B101 -- test setup
    row.binding = binding
    row.field_ids = None
    if isinstance(row, ColumnMask):
        row.column_field_id = None
    session.commit()


def _policy_needs(column: str, run_id: uuid.UUID) -> str:
    """Write out the text for a policy that reads a column the snapshot has not in the same form.

    Parameters
    ----------
    column
        The column: a bound policy's by its name now, another as the policy spells it.
    run_id
        The run of the check result.

    Returns
    -------
    str
        The whole text a user reads, for `gold.orders` in `dev`.
    """
    return (
        f"A row filter or column mask of yours on `gold.orders` uses the column `{column}`, "
        "which is not the same column in the checked snapshot and in the table now (it was "
        "added, dropped or renamed since, or another column took its name), so the rows this "
        "check found cannot be shown under it. The number of rows the check found remains: "
        f"`lakana check list --run {run_id} --env dev`."
    )


def _stored_filter(session: Session, admin: PrincipalRef, name: str, expression: str) -> None:
    """Store a row filter on `gold.orders` the way an older release accepted it.

    Creating a filter now refuses a qualified column and a column the table lacks, so the
    row is written directly: filters stored before that check still exist and must keep
    failing closed. It records the table's current schema, as a created filter does, so that
    what these tests look at is the read of the checked snapshot and not the refusal after a
    rename.

    Parameters
    ----------
    session
        Database session.
    admin
        The principal that sets the filter.
    name
        The reader's name.
    expression
        The predicate.
    """
    reader = session.execute(select(Principal).where(Principal.name == name)).scalar_one()
    environment_id = resolve_environment_id(session, "dev")
    table_id = resolve_table_id(session, "dev", "gold", "orders")
    assert environment_id is not None and table_id is not None  # nosec B101 -- test setup
    session.add(
        RowFilter(
            id=uuid.uuid4(),
            principal_id=reader.id,
            environment="dev",
            namespace="gold",
            table="orders",
            environment_id=environment_id,
            table_id=table_id,
            expression=expression,
            schema_id=catalog(session).load_table(admin, ORDERS).metadata.current_schema_id,
            created_by=admin.id,
            created_at=datetime.now(UTC),
        )
    )
    session.commit()


def _current_columns(session: Session) -> dict[str, str]:
    """Read the current schema of `gold.orders` from its metadata file, published or not.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    dict
        Column name to type, as the worker spells types for the rewrite.
    """
    session.rollback()
    location = session.execute(
        select(Table.metadata_location).where(Table.name == "orders")
    ).scalar_one()
    assert location is not None
    metadata = read_metadata(StorageResolver(session).for_environment("dev").file_io, location)
    return {field.name: str(field.field_type) for field in metadata.schema().fields}


def _warned_run(session: Session, admin: PrincipalRef, rows: pa.Table, sql: str) -> CheckResult:
    """Let a run build `gold.orders` and fail a check that leaves the snapshot in place.

    Parameters
    ----------
    session
        Database session; commits.
    admin
        The run's principal.
    rows
        What the run writes; the table is created with them.
    sql
        The check's SQL.

    Returns
    -------
    CheckResult
        The result; its checked snapshot stays in the table's history.
    """
    snapshot = write(session, admin, rows, WriteMode.CREATE)
    return _checked(session, admin, snapshot_id=snapshot, sql=sql)


def _field_ids(session: Session) -> dict[str, int]:
    """Read the field id of each column of `gold.orders` from its current schema.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    dict
        Column name to Iceberg field id.
    """
    session.rollback()
    location = session.execute(
        select(Table.metadata_location).where(Table.name == "orders")
    ).scalar_one()
    assert location is not None
    metadata = read_metadata(StorageResolver(session).for_environment("dev").file_io, location)
    return {field.name: field.field_id for field in metadata.schema().fields}


def test_lakanas_own_writes_keep_the_field_ids_of_unchanged_columns(
    session: Session, admin: PrincipalRef
) -> None:
    """A rebuild, a rebuild that adds a column and an append that adds one keep the old ids.

    The read at a check compares field ids; if a write gave unchanged columns new ids, every
    column would be withheld after any rebuild.
    """
    write(session, admin, _GOOD, WriteMode.CREATE)
    created = _field_ids(session)

    write(session, admin, _BAD, WriteMode.REPLACE)
    rebuilt = _field_ids(session)
    write(session, admin, orders([3], ["eu"], note=["n"]), WriteMode.REPLACE, evolve_schema=True)
    widened = _field_ids(session)
    write(
        session,
        admin,
        orders([4], ["eu"], note=["n"], tier=["t"]),
        WriteMode.APPEND,
        evolve_schema=True,
    )
    appended = _field_ids(session)

    assert created == {"id": 1, "region": 2, "email": 3}
    assert rebuilt == created
    assert widened == created | {"note": 4}
    assert appended == widened | {"tier": 5}


def test_a_column_only_the_failed_snapshot_has_is_shown_to_nobody(
    session: Session, admin: PrincipalRef, tmp_path: Path
) -> None:
    """The failed run added `note` and the rollback set the schema back: `note` is not shown.

    No mask can be put on a column the table does not have now, so the column is withheld
    from every reader alike: one with a mask, one with a row filter only, one with neither.
    """
    bad = orders([2, -1, -2], ["eu", "eu", "us"], note=["n2", "n-1", "n-2"])
    result = _failed_run(session, admin, bad=bad)
    assert list(_current_columns(session)) == ["id", "region", "email"]
    ana = reader(session, admin, "ana", "orders")
    ben = reader(session, admin, "ben", "orders")
    cy = reader(session, admin, "cy", "orders")
    filter_eu(session, admin, "ana")
    filter_eu(session, admin, "ben")
    mask(session, admin, "ana", "email")

    masked = _rows(session, tmp_path, ana, result)
    filtered = _rows(session, tmp_path, ben, result)
    plain = _rows(session, tmp_path, cy, result)

    assert masked == [{"id": -1, "region": "eu", "email": "***"}]
    assert filtered == [{"id": -1, "region": "eu", "email": "o-1@example.org"}]
    assert plain == [
        {"id": -2, "region": "us", "email": "o-2@example.org"},
        {"id": -1, "region": "eu", "email": "o-1@example.org"},
    ]


@pytest.mark.parametrize("masked", [False, True], ids=["no-mask", "mask"])
def test_a_check_that_names_a_column_that_is_not_shown_fails_with_the_reason(
    session: Session, admin: PrincipalRef, tmp_path: Path, masked: bool
) -> None:
    """A check whose SQL reads the withheld column is told why, whoever asks.

    Parameters
    ----------
    session
        Database session.
    admin
        Sets the world up.
    tmp_path
        The storage root.
    masked
        Whether the reader has a column mask on the table.
    """
    bad = orders([2, -1], ["eu", "eu"], note=["n2", "n-1"])
    result = _failed_run(
        session, admin, bad=bad, sql="SELECT id FROM gold.orders WHERE note = 'n-1'"
    )
    ana = reader(session, admin, "ana", "orders")
    if masked:
        mask(session, admin, "ana", "email")

    message, cause = _failure(session, tmp_path, ana, result)

    assert message == _not_shown("note", result.run_id)
    assert cause == QueryErrorCause.INVALID_QUERY.value


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT id FROM gold.orders WHERE NOTE = 'n-1'",
        "SELECT id FROM gold.orders AS o WHERE o.note = 'n-1'",
        "SELECT o.id FROM gold.orders o WHERE o.NOTE = 'n-1'",
        "SELECT o.note FROM gold.orders o",
        "SELECT orders.note FROM gold.orders",
        "SELECT gold.orders.note FROM gold.orders",
        "SELECT id FROM gold.orders WHERE orders.note = 'n-1'",
    ],
)
def test_a_withheld_column_named_in_another_case_or_through_an_alias_gets_the_reason(
    session: Session, admin: PrincipalRef, tmp_path: Path, sql: str
) -> None:
    """The database words the failure differently, but the cause is the same withheld column."""
    bad = orders([2, -1], ["eu", "eu"], note=["n2", "n-1"])
    result = _failed_run(session, admin, bad=bad, sql=sql)
    ana = reader(session, admin, "ana", "orders")

    message, cause = _failure(session, tmp_path, ana, result)

    assert message == _not_shown("note", result.run_id)
    assert cause == QueryErrorCause.INVALID_QUERY.value


def test_a_column_dropped_since_is_not_shown_from_an_older_snapshot(
    session: Session, admin: PrincipalRef, tmp_path: Path
) -> None:
    """A later run dropped `ssn`; the snapshot of an earlier failed check still has it.

    Nothing can mask the column any more. While the reader's mask on `ssn` is still stored,
    its field is gone and every read of the table under it is refused; once it is revoked the
    column is still not shown: neither with ``*`` nor by name.
    """
    bad = orders([2, -1], ["eu", "eu"], ssn=["s2", "s-1"])
    result = _warned_run(session, admin, bad, _NEGATIVE)
    by_name = _checked(
        session,
        admin,
        snapshot_id=result.checked_snapshot_id or 0,
        sql="SELECT ssn FROM gold.orders WHERE id < 0",
    )
    ana = reader(session, admin, "ana", "orders")
    mask_id = mask(session, admin, "ana", "ssn")
    write(session, admin, orders([3], ["eu"]), WriteMode.REPLACE, evolve_schema=True)
    assert list(_current_columns(session)) == ["id", "region", "email"]

    with_stale_mask, _ = _failure(session, tmp_path, ana, result)
    ColumnMaskService(session, AllowAllAuthorization(), catalog(session)).revoke(admin, mask_id)
    rows = _rows(session, tmp_path, ana, result)
    message, _ = _failure(session, tmp_path, ana, by_name)

    assert rows == [{"id": -1, "region": "eu", "email": "o-1@example.org"}]
    assert with_stale_mask == _column_gone("gold.orders", "ssn")
    assert message == _not_shown("ssn", by_name.run_id)


def test_a_column_whose_type_was_widened_since_is_not_shown(
    session: Session, admin: PrincipalRef, tmp_path: Path
) -> None:
    """The name is still there but the column is another one now: it is withheld."""
    narrow = pa.table({"id": pa.array([2, -1], pa.int64()), "qty": pa.array([5, 6], pa.int32())})
    result = _warned_run(session, admin, narrow, "SELECT * FROM gold.orders WHERE id < 0")
    wide = pa.table({"id": pa.array([3], pa.int64()), "qty": pa.array([7], pa.int64())})
    write(session, admin, wide, WriteMode.REPLACE, evolve_schema=True)
    assert _current_columns(session) == {"id": "long", "qty": "long"}
    ana = reader(session, admin, "ana", "orders")

    assert _rows(session, tmp_path, ana, result) == [{"id": -1}]


def test_a_struct_that_gained_a_field_is_another_type_and_not_shown() -> None:
    """Types are compared whole, so a nested field the table does not have is withheld too.

    Lakana's own writers make no nested column; one can arrive through the Iceberg REST
    catalog. The comparison is tested on the fields as the schemas hold them.
    """
    before = StructType(NestedField(3, "street", StringType(), required=False))
    after = StructType(
        NestedField(3, "street", StringType(), required=False),
        NestedField(4, "door_code", StringType(), required=False),
    )
    snapshot = (
        NestedField(1, "id", LongType(), required=False),
        NestedField(2, "address", after, required=False),
        NestedField(5, "note", StringType(), required=False),
    )
    current = (
        NestedField(1, "id", LongType(), required=True),
        NestedField(2, "address", before, required=False),
        NestedField(6, "extra", StringType(), required=False),
    )

    shown, withheld = _columns_the_table_still_has(snapshot, current)

    # Whether a column is required is not compared: `id` is shown.
    assert (shown, withheld) == ({"id": "long"}, ("address", "note"))
    assert _columns_the_table_still_has(snapshot, snapshot) == (
        {"id": "long", "address": str(after), "note": "string"},
        (),
    )


def test_two_columns_renamed_into_each_other_are_both_withheld() -> None:
    """A swap of two names (through the Iceberg catalog) keeps names and types; the ids tell.

    A mask on `a` is meant for what `a` holds now. The snapshot's `a` holds what is `b` now,
    so showing it under the mask of `a`, or unmasked, would be wrong either way.
    """
    snapshot = (
        NestedField(1, "a", StringType(), required=False),
        NestedField(2, "b", StringType(), required=False),
        NestedField(3, "c", StringType(), required=False),
    )
    current = (
        NestedField(2, "a", StringType(), required=False),
        NestedField(1, "b", StringType(), required=False),
        NestedField(3, "c", StringType(), required=False),
    )

    assert _columns_the_table_still_has(snapshot, current) == ({"c": "string"}, ("a", "b"))


def test_a_rebuild_with_the_same_columns_keeps_every_column_shown(
    session: Session, admin: PrincipalRef, tmp_path: Path
) -> None:
    """Later runs replaced the table and added a column: the old snapshot's columns are shown."""
    result = _warned_run(session, admin, _BAD, _NEGATIVE)
    write(session, admin, _GOOD, WriteMode.REPLACE)
    write(session, admin, orders([5], ["eu"], tier=["t"]), WriteMode.REPLACE, evolve_schema=True)
    ana = reader(session, admin, "ana", "orders")

    assert _rows(session, tmp_path, ana, result) == [
        {"id": -2, "region": "us", "email": "o-2@example.org"},
        {"id": -1, "region": "eu", "email": "o-1@example.org"},
    ]


# --- policies that name a column the read does not have ---


def test_a_row_filter_on_a_column_added_since_refuses_the_read_with_its_own_reason(
    session: Session, admin: PrincipalRef, tmp_path: Path
) -> None:
    """The filter cannot be applied to the snapshot and is never dropped: nothing is shown.

    `note` is withheld in the same read; the reason given is the filter's, which is the
    cause, and not that of a column the check's SQL never names.
    """
    bad = orders([2, -1], ["eu", "eu"], note=["n2", "n-1"])
    result = _failed_run(session, admin, bad=bad)
    write(session, admin, orders([6], ["eu"], tier=["gold"]), WriteMode.APPEND, evolve_schema=True)
    assert list(_current_columns(session)) == ["id", "region", "email", "tier"]
    ana = reader(session, admin, "ana", "orders")
    RowFilterService(session, AllowAllAuthorization(), catalog(session)).create(
        admin,
        principal="ana",
        environment="dev",
        namespace="gold",
        table="orders",
        expression="region = 'eu' AND Tier = 'gold'",
    )

    message, cause = _failure(session, tmp_path, ana, result)

    # The filter is bound to the field; it reads the column by its name in the table.
    assert message == _policy_needs("tier", result.run_id)
    assert cause == QueryErrorCause.INVALID_QUERY.value


def test_a_row_filter_on_a_column_dropped_since_refuses_the_read(
    session: Session, admin: PrincipalRef, tmp_path: Path
) -> None:
    """The snapshot has the filter's column and the table does not: the filter's field is gone.

    That refuses every read of the table under the filter, the read at the check included.
    """
    bad = orders([2, -1, -2], ["eu", "eu", "us"], ssn=["s2", "s-1", "s-2"])
    result = _warned_run(session, admin, bad, _NEGATIVE)
    ana = reader(session, admin, "ana", "orders")
    filter(session, admin, "ana", "SSN = 's-1'")
    write(session, admin, orders([3], ["eu"]), WriteMode.REPLACE, evolve_schema=True)

    message, cause = _failure(session, tmp_path, ana, result)

    assert message == _column_gone("gold.orders", "SSN")
    assert cause == QueryErrorCause.INVALID_QUERY.value


def _dropped_and_added_again(session: Session, admin: PrincipalRef) -> CheckResult:
    """Fail a check that leaves its snapshot, then drop `region` and add it again.

    Parameters
    ----------
    session
        Database session; commits.
    admin
        The run's principal.

    Returns
    -------
    CheckResult
        The result of the first run. The table's `region` is another column now (a new field
        id) under the old name and type; `id` and `email` are the columns they were.
    """
    result = _warned_run(session, admin, _BAD, _NEGATIVE)
    before = _field_ids(session)
    without = pa.table({"id": pa.array([7], pa.int64()), "email": pa.array(["x"], pa.string())})
    write(session, admin, without, WriteMode.REPLACE, evolve_schema=True)
    again = pa.table(
        {
            "id": pa.array([-8, 9], pa.int64()),
            "email": pa.array(["o-8@example.org", "o9@example.org"], pa.string()),
            "region": pa.array(["eu", "us"], pa.string()),
        }
    )
    write(session, admin, again, WriteMode.REPLACE, evolve_schema=True)
    after = _field_ids(session)
    assert (after["id"], after["email"]) == (before["id"], before["email"])
    assert after["region"] != before["region"]
    return result


def test_a_filter_on_a_column_dropped_and_added_again_refuses_the_read(
    session: Session, admin: PrincipalRef, tmp_path: Path
) -> None:
    """The filter is written for today's `region`; the snapshot's `region` is another column.

    The reader's ordinary query of the table is untouched by all of this.
    """
    result = _dropped_and_added_again(session, admin)
    ana = reader(session, admin, "ana", "orders")
    filter(session, admin, "ana", "region = 'eu'")

    message, cause = _failure(session, tmp_path, ana, result)
    ordinary = QueryService(session, AllowAllAuthorization()).submit_query(ana, "dev", _NEGATIVE)
    finished, today = run(session, tmp_path, ordinary.id)

    assert message == _policy_needs("region", result.run_id)
    assert cause == QueryErrorCause.INVALID_QUERY.value
    assert (finished.status, today) == (
        QueryRunStatus.SUCCEEDED,
        [{"id": -8, "email": "o-8@example.org", "region": "eu"}],
    )


def test_a_filter_written_before_the_column_was_dropped_and_added_again_refuses_the_read(
    session: Session, admin: PrincipalRef, tmp_path: Path
) -> None:
    """A legacy filter that names no schema is judged from the oldest one, also at a check.

    The refusal after a rename comes before the one about the checked snapshot: the policy may
    cover another column in every read of the table.
    """
    result = _dropped_and_added_again(session, admin)
    ana = reader(session, admin, "ana", "orders")
    filter(session, admin, "ana", "region = 'eu'")
    for stored in session.execute(select(RowFilter)).scalars().all():
        _unbind(session, stored.id, PENDING)
        stored.schema_id = None
    session.commit()

    message, cause = _failure(session, tmp_path, ana, result)

    assert message == policy_column_renamed_message("region", "gold.orders")
    assert cause == QueryErrorCause.INVALID_QUERY.value


def test_a_mask_whose_expression_reads_a_column_that_is_another_now_refuses_the_read(
    session: Session, admin: PrincipalRef, tmp_path: Path
) -> None:
    """The mask on `email` is built from `region`, which is not the column it was."""
    result = _dropped_and_added_again(session, admin)
    ana = reader(session, admin, "ana", "orders")
    mask_as(session, admin, "ana", "email", "substr(region, 1, 1) || '***'")

    message, _ = _failure(session, tmp_path, ana, result)

    assert message == _policy_needs("region", result.run_id)


def test_the_first_column_by_name_is_the_one_named(
    session: Session, admin: PrincipalRef, tmp_path: Path
) -> None:
    """Several columns of the reader's policies changed: the text names one, always the same."""
    result = _dropped_and_added_again(session, admin)
    ana = reader(session, admin, "ana", "orders")
    _stored_filter(session, admin, "ana", "zone = 'a' OR region = 'eu' OR area = 'b'")

    message, _ = _failure(session, tmp_path, ana, result)

    assert message == _policy_needs("area", result.run_id)


def test_a_mask_whose_expression_reads_unchanged_columns_is_applied(
    session: Session, admin: PrincipalRef, tmp_path: Path
) -> None:
    """A mask built from another column works on the snapshot as on the table."""
    result = _failed_run(session, admin)
    ana = reader(session, admin, "ana", "orders")
    filter_eu(session, admin, "ana")
    mask_as(session, admin, "ana", "email", "substr(region, 1, 1) || '***'")

    assert _rows(session, tmp_path, ana, result) == [{"id": -1, "region": "eu", "email": "e***"}]


def test_a_filter_on_one_of_two_columns_renamed_into_each_other_refuses_the_read(
    session: Session, admin: PrincipalRef, tmp_path: Path
) -> None:
    """Through the Iceberg catalog `region` and `email` swapped names; ids tell them apart.

    The filter ``region = 'eu'`` would be judged on the snapshot's `region`, which holds what
    the table calls `email` now: the wrong rows would be shown.
    """
    result = _warned_run(session, admin, _BAD, _NEGATIVE)
    ids = _field_ids(session)
    swapped = Schema(
        NestedField(ids["id"], "id", LongType(), required=False),
        NestedField(ids["email"], "region", StringType(), required=False),
        NestedField(ids["region"], "email", StringType(), required=False),
        schema_id=catalog(session).load_table(admin, ORDERS).metadata.current_schema_id + 1,
    )
    catalog(session).commit_table(
        admin,
        ORDERS,
        requirements=[],
        updates=[AddSchemaUpdate(schema_=swapped), SetCurrentSchemaUpdate(schema_id=-1)],
    )
    assert _field_ids(session) == {"id": ids["id"], "region": ids["email"], "email": ids["region"]}
    ana = reader(session, admin, "ana", "orders")
    cy = reader(session, admin, "cy", "orders")
    filter(session, admin, "ana", "region = 'eu'")

    message, _ = _failure(session, tmp_path, ana, result)
    plain = _rows(session, tmp_path, cy, result)

    assert message == _policy_needs("region", result.run_id)
    assert plain == [{"id": -2}, {"id": -1}]


def test_a_check_read_before_a_swap_never_shows_the_masked_field(
    session: Session, admin: PrincipalRef, tmp_path: Path
) -> None:
    """Review Focus 1: ana's mask on `email`; after the check, `email` and `region` swap names.

    The snapshot's `email` is the field now called `region`: both columns are withheld, and
    no row shows an unmasked value of the masked field under any name.
    """
    result = _warned_run(session, admin, _BAD, _NEGATIVE)
    ana = reader(session, admin, "ana", "orders")
    mask(session, admin, "ana", "email")
    ids = _field_ids(session)
    swapped = Schema(
        NestedField(ids["id"], "id", LongType(), required=False),
        NestedField(ids["email"], "region", StringType(), required=False),
        NestedField(ids["region"], "email", StringType(), required=False),
        schema_id=catalog(session).load_table(admin, ORDERS).metadata.current_schema_id + 1,
    )
    catalog(session).commit_table(
        admin,
        ORDERS,
        requirements=[],
        updates=[AddSchemaUpdate(schema_=swapped), SetCurrentSchemaUpdate(schema_id=-1)],
    )

    rows = _rows(session, tmp_path, ana, result)

    assert rows == [{"id": -2}, {"id": -1}]
    emails = set(_BAD.column("email").to_pylist())
    assert not any(value in emails for row in rows for value in row.values())


def test_a_filter_on_a_field_of_a_column_is_judged_by_the_column(
    session: Session, admin: PrincipalRef, tmp_path: Path
) -> None:
    """``region.country`` reads the column `region`; ``address.country`` a column nobody has.

    Lakana's writers make no struct column, so the first filter fails in the database (a text
    has no fields); what matters here is that it is not refused for a column `country`.
    """
    result = _failed_run(session, admin)
    ana = reader(session, admin, "ana", "orders")
    ben = reader(session, admin, "ben", "orders")
    _stored_filter(session, admin, "ana", "region.country = 'DE'")
    _stored_filter(session, admin, "ben", "address.country = 'DE'")

    has_the_column, _ = _failure(session, tmp_path, ana, result)
    lacks_it, _ = _failure(session, tmp_path, ben, result)

    assert has_the_column is not None
    assert "row filter or column mask of yours" not in has_the_column
    assert lacks_it == _policy_needs("address", result.run_id)


def test_a_mask_on_a_column_added_since_does_not_block_the_read(
    session: Session, admin: PrincipalRef, tmp_path: Path
) -> None:
    """The masked column is not in the snapshot, so there is nothing to mask and nothing shown.

    Its expression reads that same column; a mask that is skipped is not judged by what its
    expression reads.
    """
    result = _warned_run(session, admin, _BAD, _NEGATIVE)
    write(session, admin, orders([6], ["eu"], tier=["gold"]), WriteMode.APPEND, evolve_schema=True)
    ana = reader(session, admin, "ana", "orders")
    mask_as(session, admin, "ana", "tier", "substr(tier, 1, 1)")
    mask(session, admin, "ana", "email")

    assert _rows(session, tmp_path, ana, result) == [
        {"id": -2, "region": "us", "email": "***"},
        {"id": -1, "region": "eu", "email": "***"},
    ]


def test_a_filter_that_spells_an_unchanged_column_in_another_case_is_applied(
    session: Session, admin: PrincipalRef, tmp_path: Path
) -> None:
    """The database matches a column name without regard to case, and so does the check here."""
    result = _failed_run(session, admin)
    ana = reader(session, admin, "ana", "orders")
    filter(session, admin, "ana", "Region = 'eu'")

    assert _rows(session, tmp_path, ana, result) == [
        {"id": -1, "region": "eu", "email": "o-1@example.org"}
    ]


def test_a_mask_on_a_column_widened_since_does_not_block_the_read(
    session: Session, admin: PrincipalRef, tmp_path: Path
) -> None:
    """The column is withheld as another column than today's, and its mask has nothing to cover."""
    narrow = pa.table({"id": pa.array([2, -1], pa.int64()), "qty": pa.array([5, 6], pa.int32())})
    result = _warned_run(session, admin, narrow, _NEGATIVE)
    wide = pa.table({"id": pa.array([3], pa.int64()), "qty": pa.array([7], pa.int64())})
    write(session, admin, wide, WriteMode.REPLACE, evolve_schema=True)
    ana = reader(session, admin, "ana", "orders")
    ColumnMaskService(session, AllowAllAuthorization(), catalog(session)).create(
        admin,
        principal="ana",
        environment="dev",
        namespace="gold",
        table="orders",
        column="qty",
        expression="NULL",
    )

    assert _rows(session, tmp_path, ana, result) == [{"id": -1}]


def test_an_ordinary_query_with_a_mask_on_an_unknown_column_is_refused_as_before(
    session: Session, admin: PrincipalRef, tmp_path: Path
) -> None:
    """Skipping a mask is for the read at a check only; any other source keeps the refusal.

    The mask is one of an earlier release: a bound mask cannot name an unknown column.
    """
    write(session, admin, orders([1], ["eu"], ssn=["s1"]), WriteMode.CREATE)
    ana = reader(session, admin, "ana", "orders")
    _unbind(session, mask(session, admin, "ana", "ssn"), UNBOUND)
    write(session, admin, orders([3], ["eu"]), WriteMode.REPLACE, evolve_schema=True)

    ordinary = QueryService(session, AllowAllAuthorization()).submit_query(
        ana, "dev", "SELECT * FROM gold.orders"
    )
    finished, rows = run(session, tmp_path, ordinary.id)

    assert (finished.status, rows) == (QueryRunStatus.FAILED, [])
    assert finished.error_message == (
        "Column mask references unknown column 'ssn'; known columns: ['email', 'id', 'region']"
    )


def test_a_mask_that_differs_from_a_shown_column_only_in_case_is_not_skipped(
    session: Session, admin: PrincipalRef
) -> None:
    """A stored mask spelled `EMAIL` is meant for `email`: it stays, and the rewrite refuses.

    Skipping it as "not in the read" would show the column unmasked. Only a mask of an earlier
    release, not bound to field ids, can be spelled other than its column.
    """
    result = _failed_run(session, admin)
    ana = reader(session, admin, "ana", "orders")
    mask_id = mask(session, admin, "ana", "email")
    _unbind(session, mask_id, UNBOUND)
    session.execute(update(ColumnMask).where(ColumnMask.id == mask_id).values(column="EMAIL"))
    session.commit()

    sources = _resolve_sources(
        session, "dev", ana, ["gold.orders"], GrantAuthorization(session), check_result_id=result.id
    )

    assert sources["gold.orders"].column_masks == {"EMAIL": "'***'"}
    with pytest.raises(ValidationError, match="unknown column 'EMAIL'"):
        rewrite_query("SELECT * FROM gold.orders", sources)


_BINDER = (
    'Query failed: Binder Error: Referenced column "{referenced}" not found in FROM clause!\n'
    'Candidate bindings: "{candidate}"'
)
"""DuckDB's wording for a column that its relation does not have, with one suggestion."""


def test_the_reason_is_given_for_the_referenced_column_only() -> None:
    """A withheld column that the database merely suggests is not the cause."""
    withheld = {"note": "the reason for note"}
    suggested = ValidationError(_BINDER.format(referenced="tier", candidate="note"))
    referenced = ValidationError(_BINDER.format(referenced="note", candidate="tier"))
    worded_otherwise = ValidationError('Query failed: Binder Error: Ambiguous reference "note"')
    through_an_alias = ValidationError(
        'Query failed: Binder Error: Values list "o" does not have a column named "Note"'
    )

    assert _withheld_column_failure(suggested, withheld) is None
    assert str(_withheld_column_failure(referenced, withheld)) == "the reason for note"
    assert isinstance(_withheld_column_failure(referenced, withheld), ValidationError)
    assert _withheld_column_failure(worded_otherwise, withheld) is None
    assert str(_withheld_column_failure(through_an_alias, withheld)) == "the reason for note"
    assert _withheld_column_failure(through_an_alias, {"other": "x"}) is None
    assert _withheld_column_failure(referenced, {}) is None


def test_an_unchanged_schema_shows_every_column_under_the_readers_filter_and_mask(
    session: Session, admin: PrincipalRef, tmp_path: Path
) -> None:
    """The ordinary case: the rows of the checked snapshot, filtered and masked as today's."""
    result = _failed_run(session, admin)
    ana = reader(session, admin, "ana", "orders")
    filter_eu(session, admin, "ana")
    mask(session, admin, "ana", "email")

    assert _rows(session, tmp_path, ana, result) == [{"id": -1, "region": "eu", "email": "***"}]


def test_no_column_left_to_show_fails_with_a_text_of_its_own(
    session: Session, admin: PrincipalRef, tmp_path: Path
) -> None:
    """The table was rebuilt with other columns altogether: nothing of the snapshot is listed."""
    result = _warned_run(session, admin, _BAD, _NEGATIVE)
    other = pa.table({"key": pa.array(["a"], pa.string())})
    write(session, admin, other, WriteMode.REPLACE, evolve_schema=True)
    ana = reader(session, admin, "ana", "orders")

    message, cause = _failure(session, tmp_path, ana, result)

    assert message == (
        "None of the columns of the checked snapshot is in `gold.orders` in the same form any "
        "more, so the rows this check found cannot be listed. The number of rows the check "
        f"found remains: `lakana check list --run {result.run_id} --env dev`."
    )
    assert cause == QueryErrorCause.INVALID_QUERY.value


def test_another_error_keeps_its_own_message(
    session: Session, admin: PrincipalRef, tmp_path: Path
) -> None:
    """Only a query that names a withheld column gets that reason; other failures stay."""
    bad = orders([2, -1], ["eu", "eu"], note=["n2", "n-1"])
    result = _failed_run(
        session, admin, bad=bad, sql="SELECT CAST(region AS INTEGER) AS id FROM gold.orders"
    )
    ana = reader(session, admin, "ana", "orders")

    message, cause = _failure(session, tmp_path, ana, result)

    assert message is not None
    assert message.startswith("Query failed: a value could not be converted")
    assert "of the checked snapshot" not in message
    assert cause == QueryErrorCause.INVALID_QUERY.value


def test_a_column_the_query_names_that_nobody_has_keeps_the_databases_message(
    session: Session, admin: PrincipalRef, tmp_path: Path
) -> None:
    """A query that names a column no snapshot has is not blamed on the withheld one."""
    bad = orders([2, -1], ["eu", "eu"], note=["n2", "n-1"])
    result = _failed_run(session, admin, bad=bad, sql="SELECT nowhere FROM gold.orders")
    ana = reader(session, admin, "ana", "orders")

    message, cause = _failure(session, tmp_path, ana, result)

    assert message is not None
    assert "Binder Error" in message
    assert "is not shown" not in message
    assert cause == QueryErrorCause.INVALID_QUERY.value


def test_a_column_that_another_table_of_the_query_has_is_not_explained_as_withheld(
    session: Session, admin: PrincipalRef, tmp_path: Path
) -> None:
    """The reason is given by the bare column name, so the name must mean one table only.

    `gold.notes` has a column `note` of its own. A "column not found" for `note` may then be
    about either table, and the database's own message stands.
    """
    bad = orders([2, -1], ["eu", "eu"], note=["n2", "n-1"])
    sql = "SELECT id FROM gold.orders WHERE note = 'x' AND EXISTS (SELECT 1 FROM gold.notes)"
    result = _failed_run(session, admin, bad=bad, sql=sql)
    notes = TableRef.from_dotted("dev", "gold.notes")
    write(
        session,
        admin,
        pa.table({"NOTE": pa.array(["a"], pa.string())}),
        WriteMode.CREATE,
        ref=notes,
    )
    ana = reader(session, admin, "ana", "orders", "notes")
    authz = GrantAuthorization(session)
    alone: dict[str, str] = {}
    beside: dict[str, str] = {}

    _resolve_sources(
        session, "dev", ana, ["gold.orders"], authz, check_result_id=result.id, withheld=alone
    )
    _resolve_sources(
        session,
        "dev",
        ana,
        ["gold.notes", "gold.orders"],
        authz,
        check_result_id=result.id,
        withheld=beside,
    )
    message, cause = _failure(session, tmp_path, ana, result)

    assert alone == {"note": _not_shown("note", result.run_id)}
    assert beside == {}
    assert message is not None
    assert "Binder Error" in message
    assert "is not shown" not in message
    assert cause == QueryErrorCause.INVALID_QUERY.value


def test_the_source_of_the_checked_table_lists_its_columns_and_another_table_does_not(
    session: Session, admin: PrincipalRef
) -> None:
    """What the rewrite gets: an explicit column list for the checked table only.

    The explicit list is what keeps a withheld column out of the read; a reader without any
    policy would otherwise read the snapshot's files with ``*``.
    """
    bad = orders([2, -1], ["eu", "eu"], note=["n2", "n-1"])
    result = _failed_run(session, admin, bad=bad)
    limits = TableRef.from_dotted("dev", "gold.limits")
    floor = pa.table({"floor": pa.array([0], pa.int64())})
    write(session, admin, floor, WriteMode.CREATE, ref=limits)
    ben = reader(session, admin, "ben", "orders", "limits")
    authz = GrantAuthorization(session)
    names = ["gold.orders", "gold.limits"]

    at_check = _resolve_sources(session, "dev", ben, names, authz, check_result_id=result.id)
    today = _resolve_sources(session, "dev", ben, names, authz)

    assert dict(at_check["gold.orders"].columns) == {
        "id": "long",
        "region": "string",
        "email": "string",
    }
    assert (at_check["gold.orders"].declared_columns_only, at_check["gold.orders"].row_filters) == (
        True,
        [],
    )
    assert len(at_check["gold.orders"].urls) == 1
    assert set(at_check["gold.orders"].urls).isdisjoint(today["gold.orders"].urls)
    assert today["gold.orders"].declared_columns_only is False
    for sources in (at_check, today):
        assert sources["gold.limits"].declared_columns_only is False
        assert sources["gold.limits"].urls == today["gold.limits"].urls
    rewritten = rewrite_query("SELECT * FROM gold.orders", {"gold.orders": at_check["gold.orders"]})
    assert 'SELECT "id", "region", "email" FROM READ_PARQUET(' in rewritten.sql
    assert "note" not in rewritten.sql


# --- the audit trail ---


def _completed(session: Session) -> list[dict[str, object]]:
    """Read the details of the completion events of queries, oldest first.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    list of dict
        The detail of each `QUERY_COMPLETED` event.
    """
    session.rollback()
    return [
        event.detail
        for event in session.scalars(
            select(AuditEvent)
            .where(AuditEvent.event_type == AuditEventType.QUERY_COMPLETED)
            .order_by(AuditEvent.occurred_at, AuditEvent.id)
        )
    ]


def test_the_event_of_a_query_that_shows_the_rows_of_a_check_names_the_result(
    session: Session, admin: PrincipalRef, tmp_path: Path
) -> None:
    """The trail tells a read of a checked snapshot from an ordinary query of the table."""
    result = _failed_run(session, admin, first_build=True)
    ana = reader(session, admin, "ana", "orders")
    before = len(_completed(session))

    _rows(session, tmp_path, ana, result)

    assert _completed(session)[before:] == [
        {
            "sql_text": _NEGATIVE,
            "tables_touched": ["gold.orders"],
            "row_count": 2,
            "status": "SUCCEEDED",
            "check_result_id": str(result.id),
        }
    ]


def test_the_event_of_a_failed_or_refused_query_of_the_rows_names_the_result_too(
    session: Session, admin: PrincipalRef, tmp_path: Path
) -> None:
    """Failed in the query or refused before it: the event says which result was asked for."""
    result = _failed_run(session, admin)
    result_id = str(result.id)
    stranger = reader(session, admin, "stranger")
    ana = reader(session, admin, "ana", "orders")
    before = len(_completed(session))

    _failure(session, tmp_path, stranger, result)
    refused = _submit(session, ana, result)
    session.execute(update(Principal).where(Principal.id == ana.id).values(is_active=False))
    session.commit()
    run(session, tmp_path, refused)

    assert _completed(session)[before:] == [
        {
            "sql_text": _NEGATIVE,
            "tables_touched": ["gold.orders"],
            "row_count": None,
            "status": "FAILED",
            "check_result_id": result_id,
        },
        {
            "sql_text": _NEGATIVE,
            "tables_touched": [],
            "row_count": None,
            "status": "FAILED",
            "check_result_id": result_id,
        },
    ]


def test_the_event_of_an_ordinary_query_has_the_keys_it_always_had(
    session: Session, admin: PrincipalRef, tmp_path: Path
) -> None:
    """A query that is not bound to a check result gains no key, completed or refused."""
    write(session, admin, _GOOD, WriteMode.CREATE)
    ana = reader(session, admin, "ana", "orders")
    queries = QueryService(session, AllowAllAuthorization())
    before = len(_completed(session))

    done, _ = run(
        session, tmp_path, queries.submit_query(ana, "dev", "SELECT id FROM gold.orders").id
    )
    refused = queries.submit_query(ana, "dev", "SELECT id FROM gold.orders").id
    session.execute(update(Principal).where(Principal.id == ana.id).values(is_active=False))
    session.commit()
    run(session, tmp_path, refused)

    assert done.status is QueryRunStatus.SUCCEEDED
    details = _completed(session)[before:]
    assert [d["status"] for d in details] == ["SUCCEEDED", "FAILED"]
    for detail in details:
        assert set(detail) == {"sql_text", "tables_touched", "row_count", "status"}


# --- through a view, twice, and beside another table ---


def test_the_checked_table_is_read_at_the_snapshot_through_a_view(
    session: Session, admin: PrincipalRef, tmp_path: Path
) -> None:
    """The check's SQL names a view; the base table inside the view is the one that is pinned."""
    result = _failed_run(session, admin, sql="SELECT * FROM gold.negative")
    view = QueryService(session, AllowAllAuthorization()).submit_write(
        admin, "dev", _NEGATIVE, WriteTarget(table="gold.negative", mode=WriteMode.VIEW)
    )
    assert run_one(session, view.id, AllowAllAuthorization()) is None
    ana = reader(session, admin, "ana", "orders", "negative")

    rows = _rows(session, tmp_path, ana, result)

    assert [row["id"] for row in rows] == [-2, -1]


def test_a_query_through_a_view_that_fails_keeps_its_own_error(
    session: Session, admin: PrincipalRef, tmp_path: Path
) -> None:
    """The view is not planned again against the table as it is now to explain the failure.

    That diagnosis reads every table as it is now. Here the table is not published, so it
    would blame the view for that and point back at the command that is running.
    """
    after = write(session, admin, _BAD, WriteMode.CREATE)
    view = QueryService(session, AllowAllAuthorization()).submit_write(
        admin, "dev", _NEGATIVE, WriteTarget(table="gold.negative", mode=WriteMode.VIEW)
    )
    assert run_one(session, view.id, AllowAllAuthorization()) is None
    result = _checked(
        session,
        admin,
        snapshot_id=after,
        sql="SELECT CAST(region AS INTEGER) AS id FROM gold.negative",
    )
    rolled = catalog(session).roll_back(
        admin,
        ORDERS,
        run_id=result.run_id,
        task_run_id=result.task_run_id,
        written=Written(_table_id(session), None, after, publishes=True),
        failed_checks=[result.check_name],
    )
    assert rolled.outcome is RollbackOutcome.UNPUBLISHED
    ana = reader(session, admin, "ana", "orders", "negative")

    message, cause = _failure(session, tmp_path, ana, result)

    assert message is not None
    assert message.startswith("Query failed: a value could not be converted")
    assert "no longer plans" not in message
    assert "has not been published" not in message
    assert "lakana check rows" not in message
    assert cause == QueryErrorCause.INVALID_QUERY.value


def test_both_references_of_a_self_join_are_read_at_the_snapshot(
    session: Session, admin: PrincipalRef, tmp_path: Path
) -> None:
    """One source for one table: no reference of it reads the table as it is now."""
    result = _failed_run(
        session,
        admin,
        sql=(
            "SELECT a.id, b.region FROM gold.orders a JOIN gold.orders b ON a.id = b.id "
            "WHERE a.id < 0"
        ),
    )
    ana = reader(session, admin, "ana", "orders")

    rows = _rows(session, tmp_path, ana, result)

    assert rows == [{"id": -2, "region": "us"}, {"id": -1, "region": "eu"}]


def test_a_second_table_that_is_not_published_is_refused_as_in_any_query(
    session: Session, admin: PrincipalRef, tmp_path: Path
) -> None:
    """Only the checked table is read at a snapshot; a joined table keeps its refusal."""
    limits = TableRef.from_dotted("dev", "gold.limits")
    result = _failed_run(
        session,
        admin,
        sql="SELECT o.id, l.floor FROM gold.orders o JOIN gold.limits l ON o.id < l.floor",
    )
    first = write(
        session, admin, pa.table({"floor": pa.array([0], pa.int64())}), WriteMode.CREATE, ref=limits
    )
    rolled = catalog(session).roll_back(
        admin,
        limits,
        run_id=result.run_id,
        task_run_id=result.task_run_id,
        written=Written(_table_id(session, "limits"), None, first, publishes=True),
        failed_checks=["gold.limits/any"],
    )
    assert rolled.outcome is RollbackOutcome.UNPUBLISHED
    ana = reader(session, admin, "ana", "orders", "limits")

    message, cause = _failure(session, tmp_path, ana, result)

    assert message is not None
    assert message.startswith("`gold.limits` has not been published: ")
    assert cause == QueryErrorCause.NOT_FOUND.value


# --- what is refused ---


def test_a_reader_without_the_grant_learns_nothing(
    session: Session, admin: PrincipalRef, tmp_path: Path
) -> None:
    """The read grant on the checked table is asked for like in any query."""
    result = _failed_run(session, admin)
    eve = reader(session, admin, "eve")

    message, cause = _failure(session, tmp_path, eve, result)

    assert message == "Resolving the tables this query reads failed (PermissionDenied)."
    assert cause == QueryErrorCause.PERMISSION_DENIED.value


def test_a_table_built_again_under_the_name_is_not_read_at_the_old_snapshot(
    session: Session, admin: PrincipalRef, tmp_path: Path
) -> None:
    """The result is bound to the table's id; a new table of the name has no such snapshot."""
    result = _failed_run(session, admin)
    ana = reader(session, admin, "ana", "orders")
    catalog(session).drop_table(admin, ORDERS)
    write(session, admin, orders([-7], ["eu"]), WriteMode.CREATE)

    # The grant went with the old table: without one on the new table nothing is said.
    message, cause = _failure(session, tmp_path, ana, result)
    assert message == "Resolving the tables this query reads failed (PermissionDenied)."

    grant_read(session, admin, "ana", "orders")
    message, cause = _failure(session, tmp_path, ana, result)

    assert message == _gone(result.run_id)
    assert cause == QueryErrorCause.NOT_FOUND.value


def test_a_renamed_table_gives_the_snapshot_gone_text_to_who_may_read_the_name(
    session: Session, admin: PrincipalRef, tmp_path: Path
) -> None:
    """The name the check's SQL uses leads nowhere any more: the reader is told what remains.

    A reader whose grant is on the table itself has none on the name once the table is
    renamed, and gets the opaque refusal as in any query. A reader who may read whatever is
    in the namespace passes the grant, and gets the text instead of a bare "not found".
    """
    result = _failed_run(session, admin)
    ana = reader(session, admin, "ana", "orders")
    nia = reader(session, admin, "nia")
    GrantService(session, AllowAllAuthorization()).grant(
        admin,
        principal="nia",
        action=Action.READ_TABLE,
        kind=SecurableKind.NAMESPACE,
        environment="dev",
        namespace="gold",
    )
    catalog(session).rename_table(admin, ORDERS, TableRef.from_dotted("dev", "gold.orders_old"))

    by_table, _ = _failure(session, tmp_path, ana, result)
    by_namespace, cause = _failure(session, tmp_path, nia, result)
    ordinary = QueryService(session, AllowAllAuthorization()).submit_query(nia, "dev", _NEGATIVE)
    unbound, _ = run(session, tmp_path, ordinary.id)

    assert by_table == "Resolving the tables this query reads failed (PermissionDenied)."
    assert by_namespace == _gone(result.run_id)
    assert cause == QueryErrorCause.NOT_FOUND.value
    assert unbound.error_message == "Resolving the tables this query reads failed (NotFoundError)."


def test_another_table_of_the_check_that_is_gone_keeps_the_opaque_text(
    session: Session, admin: PrincipalRef, tmp_path: Path
) -> None:
    """Only the checked table's own name gets the snapshot-gone text."""
    limits = TableRef.from_dotted("dev", "gold.limits")
    write(
        session, admin, pa.table({"floor": pa.array([0], pa.int64())}), WriteMode.CREATE, ref=limits
    )
    result = _failed_run(
        session,
        admin,
        sql="SELECT o.id FROM gold.orders o JOIN gold.limits l ON o.id < l.floor",
    )
    catalog(session).drop_table(admin, limits)
    ana = reader(session, admin, "ana", "orders")
    GrantService(session, AllowAllAuthorization()).grant(
        admin,
        principal="ana",
        action=Action.READ_TABLE,
        kind=SecurableKind.NAMESPACE,
        environment="dev",
        namespace="gold",
    )

    message, _ = _failure(session, tmp_path, ana, result)

    assert message == "Resolving the tables this query reads failed (NotFoundError)."


def test_an_ordinary_query_still_cannot_read_a_table_that_is_not_published(
    session: Session, admin: PrincipalRef, tmp_path: Path
) -> None:
    """A failed first build: only the query bound to the check result reads its rows."""
    result = _failed_run(session, admin, first_build=True)
    ana = reader(session, admin, "ana", "orders")
    # The rollback of a first build removes the snapshot and leaves the schema the build
    # made, so every column of the failed snapshot is one the table has.
    assert _current_columns(session) == {"id": "long", "region": "string", "email": "string"}

    ordinary = QueryService(session, AllowAllAuthorization()).submit_query(ana, "dev", _NEGATIVE)
    finished, rows = run(session, tmp_path, ordinary.id)

    assert (finished.status, rows) == (QueryRunStatus.FAILED, [])
    assert finished.error_message is not None
    assert finished.error_message.startswith("`gold.orders` has not been published: ")
    assert ordinary.check_result_id is None
    assert _rows(session, tmp_path, ana, result) == [
        {"id": -2, "region": "us", "email": "o-2@example.org"},
        {"id": -1, "region": "eu", "email": "o-1@example.org"},
    ]


def test_a_result_that_is_gone_fails_the_query_with_its_message(
    session: Session, admin: PrincipalRef, tmp_path: Path
) -> None:
    """The result went with its run after the query was queued: nothing is read as it is now."""
    result = _failed_run(session, admin)
    ana = reader(session, admin, "ana", "orders")
    query_run_id = _submit(session, ana, result)
    result_id = result.id
    session.execute(delete(Run).where(Run.id == result.run_id))
    session.commit()
    session.expire_all()
    assert session.get(CheckResult, result_id) is None

    finished, rows = run(session, tmp_path, query_run_id)

    assert (finished.status, rows) == (QueryRunStatus.FAILED, [])
    assert finished.error_message == (
        "The check result this query reads is no longer there: it was recorded again by a new "
        "attempt of its task, or removed with its run. Run `lakana check rows` again."
    )
    assert finished.error_cause == QueryErrorCause.NOT_FOUND.value
    assert finished.check_result_id == result_id


def test_a_result_recorded_again_by_a_new_attempt_fails_the_query_with_what_to_do(
    session: Session, admin: PrincipalRef, tmp_path: Path
) -> None:
    """A result recorded again has a new id: the query waiting for the old one says so."""
    result = _failed_run(session, admin)
    ana = reader(session, admin, "ana", "orders")
    query_run_id = _submit(session, ana, result)
    old_id, snapshot_id, task_run_id = result.id, result.checked_snapshot_id, result.task_run_id
    assert record_result(
        session,
        task_run_id=task_run_id,
        worker="w1",
        claim=1,
        spec=CheckSpec(
            name="gold.orders/no_negative_ids",
            table="gold.orders",
            check="no_negative_ids",
            path="checks/gold/orders/no_negative_ids.sql",
            on_failure="block",
            description=None,
            sql=_NEGATIVE,
        ),
        outcome=CheckOutcome.FAILED,
        failing_rows=2,
        error=None,
        table_id=_table_id(session),
        checked_snapshot_id=snapshot_id,
        policy_applied=False,
        query_run_id=None,
        started_at=datetime(2026, 10, 12, 9, 5, tzinfo=UTC),
    )
    session.expire_all()
    again = session.scalars(select(CheckResult).where(CheckResult.task_run_id == task_run_id)).one()
    assert again.id != old_id

    finished, rows = run(session, tmp_path, query_run_id)

    assert (finished.status, rows) == (QueryRunStatus.FAILED, [])
    assert finished.error_message == (
        "The check result this query reads is no longer there: it was recorded again by a new "
        "attempt of its task, or removed with its run. Run `lakana check rows` again."
    )
    assert finished.error_cause == QueryErrorCause.NOT_FOUND.value


def test_a_result_of_another_environment_is_no_result_for_the_query(
    session: Session, admin: PrincipalRef, tmp_path: Path
) -> None:
    """The worker looks the result up in the query run's own environment."""
    result = _failed_run(session, admin)
    ana = reader(session, admin, "ana", "orders")
    query_run_id = _submit(session, ana, result)
    other = catalog(session).create_environment(
        admin,
        name="prod",
        storage=StorageConfig(scheme="file", root=f"file://{tmp_path}/prod", options={}),
    )
    session.execute(
        update(CheckResult).where(CheckResult.id == result.id).values(environment_id=other.id)
    )
    session.commit()

    finished, rows = run(session, tmp_path, query_run_id)

    assert (finished.status, rows) == (QueryRunStatus.FAILED, [])
    assert finished.error_message == (
        "The check result this query reads is no longer there: it was recorded again by a new "
        "attempt of its task, or removed with its run. Run `lakana check rows` again."
    )


def test_the_snapshot_removed_by_maintenance_gives_r1(
    session: Session, admin: PrincipalRef, tmp_path: Path
) -> None:
    """Once maintenance removed the failed snapshot the query says so and where the count is."""
    result = _failed_run(session, admin)
    ana = reader(session, admin, "ana", "orders")
    service = catalog(session)
    # Maintenance keeps every snapshot a tag names and the newest one, so the tag is removed
    # here and the table is built again first, as it would be after a failure.
    write(session, admin, orders([3], ["eu"]), WriteMode.APPEND)
    service.commit_table(
        admin,
        ORDERS,
        requirements=[],
        updates=[RemoveSnapshotRefUpdate(ref_name=failed_snapshot_tag(result.run_id))],
        action=Action.MAINTAIN_TABLE,
    )
    report = service.maintain(
        admin, ORDERS, keep_days=0, keep_last=1, now=datetime.now(UTC) + timedelta(days=8)
    )
    assert result.checked_snapshot_id in report.snapshots_expired
    session.rollback()

    message, cause = _failure(session, tmp_path, ana, result)

    assert message == _gone(result.run_id)
    assert cause == QueryErrorCause.NOT_FOUND.value


def _expire_the_failed_snapshot(session: Session, admin: PrincipalRef, result: CheckResult) -> None:
    """Let maintenance remove the failed snapshot of a check, as it does after seven days.

    Parameters
    ----------
    session
        Database session.
    admin
        The principal that maintains the table.
    result
        The failed check result whose snapshot goes.
    """
    service = catalog(session)
    write(session, admin, orders([3], ["eu"]), WriteMode.APPEND)
    service.commit_table(
        admin,
        ORDERS,
        requirements=[],
        updates=[RemoveSnapshotRefUpdate(ref_name=failed_snapshot_tag(result.run_id))],
        action=Action.MAINTAIN_TABLE,
    )
    report = service.maintain(
        admin, ORDERS, keep_days=0, keep_last=1, now=datetime.now(UTC) + timedelta(days=8)
    )
    assert result.checked_snapshot_id in report.snapshots_expired
    session.rollback()


_RAW_STORAGE_FAILURE = (
    'Query failed: IO Error: Could not read from file "<redacted>": Parquet file is missing'
)
"""What the database says when a file went between resolving and reading it."""


@pytest.mark.parametrize("when", ["reading", "resolving"])
def test_maintenance_during_the_read_gives_the_snapshot_gone_text(
    session: Session,
    admin: PrincipalRef,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    when: str,
) -> None:
    """The snapshot goes between the resolution and the read: asked again, it is gone.

    The race is made by running the maintenance inside the step that then fails, so what
    the query meets is a raw storage error and a catalog that no longer holds the snapshot.

    Parameters
    ----------
    session
        Database session.
    admin
        Sets the world up.
    tmp_path
        The storage root.
    monkeypatch
        Replaces the failing step.
    when
        Which step fails: the read or the resolution.
    """
    result = _failed_run(session, admin)
    ana = reader(session, admin, "ana", "orders")
    real_resolve = pipeline._resolve_sources
    calls: list[int] = []

    def raced_read(*args: object, **kwargs: object) -> pa.Table:
        """Run maintenance, then fail as the storage does.

        Parameters
        ----------
        *args
            Ignored.
        **kwargs
            Ignored.

        Raises
        ------
        ValidationError
            Always, with the raw storage text.
        """
        _expire_the_failed_snapshot(session, admin, result)
        raise ValidationError(_RAW_STORAGE_FAILURE)

    def raced_resolve(
        db: Session,
        environment: str,
        principal: PrincipalRef,
        refs: list[str],
        authz: AuthorizationService,
        key_provider: KeyProvider | None = None,
        pins: dict[TableRef, SnapshotPin] | None = None,
        *,
        check_result_id: uuid.UUID | None = None,
        withheld: dict[str, str] | None = None,
        prefixes: list[str] | None = None,
    ) -> dict[str, TableSource]:
        """Fail the first resolution after maintenance ran; resolve for real afterwards.

        Parameters
        ----------
        db
            Database session.
        environment
            The environment.
        principal
            The reader.
        refs
            The tables.
        authz
            Authorization.
        key_provider
            Passed through.
        pins
            Passed through.
        check_result_id
            Passed through.
        withheld
            Passed through.
        prefixes
            Passed through.

        Returns
        -------
        dict
            What the real resolution returns, on every call but the first.

        Raises
        ------
        OSError
            On the first call.
        """
        calls.append(1)
        if len(calls) == 1:
            _expire_the_failed_snapshot(session, admin, result)
            raise OSError("storage went away")
        return real_resolve(
            db,
            environment,
            principal,
            refs,
            authz,
            key_provider,
            pins,
            check_result_id=check_result_id,
            withheld=withheld,
            prefixes=prefixes,
        )

    if when == "reading":
        monkeypatch.setattr(pipeline, "execute_query", raced_read)
    else:
        monkeypatch.setattr(pipeline, "_resolve_sources", raced_resolve)

    message, cause = _failure(session, tmp_path, ana, result)

    assert message == _gone(result.run_id)
    assert cause == QueryErrorCause.NOT_FOUND.value


def test_a_storage_failure_with_the_snapshot_still_there_keeps_its_own_text(
    session: Session,
    admin: PrincipalRef,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The catalog still holds the snapshot, so the failure is not blamed on maintenance."""
    result = _failed_run(session, admin)
    ana = reader(session, admin, "ana", "orders")

    def broken_read(*args: object, **kwargs: object) -> pa.Table:
        """Fail as the storage does.

        Parameters
        ----------
        *args
            Ignored.
        **kwargs
            Ignored.

        Raises
        ------
        ValidationError
            Always, with the raw storage text.
        """
        raise ValidationError(_RAW_STORAGE_FAILURE)

    monkeypatch.setattr(pipeline, "execute_query", broken_read)

    message, cause = _failure(session, tmp_path, ana, result)

    assert message == _RAW_STORAGE_FAILURE
    assert cause == QueryErrorCause.INVALID_QUERY.value


def test_an_ordinary_query_is_never_told_a_snapshot_is_gone(
    session: Session,
    admin: PrincipalRef,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only a query bound to a check result asks: another one keeps its failure."""
    write(session, admin, _GOOD, WriteMode.CREATE)
    ana = reader(session, admin, "ana", "orders")

    def broken_read(*args: object, **kwargs: object) -> pa.Table:
        """Fail as the storage does.

        Parameters
        ----------
        *args
            Ignored.
        **kwargs
            Ignored.

        Raises
        ------
        ValidationError
            Always, with the raw storage text.
        """
        raise ValidationError(_RAW_STORAGE_FAILURE)

    monkeypatch.setattr(pipeline, "execute_query", broken_read)
    run_id = (
        QueryService(session, AllowAllAuthorization())
        .submit_query(ana, "dev", "SELECT id FROM gold.orders")
        .id
    )

    finished, _ = run(session, tmp_path, run_id)

    assert finished.error_message == _RAW_STORAGE_FAILURE


def test_a_kept_table_is_read_by_this_query(
    session: Session, admin: PrincipalRef, tmp_path: Path
) -> None:
    """A kept table holds the failing rows for people to look at; this query has no task."""
    write(session, admin, _GOOD, WriteMode.CREATE)
    kept = write(session, admin, _BAD, WriteMode.REPLACE)
    result = _checked(session, admin, snapshot_id=kept)
    session.execute(
        update(Table)
        .where(Table.id == _table_id(session))
        .values(
            kept_run_id=result.run_id,
            kept_task_run_id=result.task_run_id,
            kept_check=result.check_name,
        )
    )
    session.commit()
    ana = reader(session, admin, "ana", "orders")

    rows = _rows(session, tmp_path, ana, result)

    assert [row["id"] for row in rows] == [-2, -1]


def test_a_passed_check_shows_no_rows(
    session: Session, admin: PrincipalRef, tmp_path: Path
) -> None:
    """A check that passed is read at its snapshot like any other and returns nothing."""
    passed = write(session, admin, _GOOD, WriteMode.CREATE)
    result = _checked(session, admin, snapshot_id=passed, outcome=CheckOutcome.PASSED)
    write(session, admin, _BAD, WriteMode.REPLACE)
    ana = reader(session, admin, "ana", "orders")

    assert _rows(session, tmp_path, ana, result) == []


@pytest.mark.parametrize(
    "error", [CheckedSnapshotGoneError, CheckResultGoneError, NoCheckedSnapshotError]
)
def test_the_refusals_of_a_read_at_a_check_are_not_found_for_a_program(
    error: type[Exception],
) -> None:
    """A client that branches on the cause sees `not_found`, as for a table not published.

    Parameters
    ----------
    error
        The type of the refusal.
    """
    assert failure_cause(error("gone")) is QueryErrorCause.NOT_FOUND
    assert failure_cause(PermissionDenied("no")) is QueryErrorCause.PERMISSION_DENIED


# --- a file of the snapshot that lacks a column the read needs ---

_GLOB_MISMATCH = (
    'Query failed: Invalid Input Error: Failed to read file "<redacted>": schema mismatch in '
    'glob: column "{column}" was read from the original file "<redacted>", but could not be '
    'found in file "<redacted>".\nCandidate names: id, region, ssn\n'
    "If you are trying to read files with different schemas, try setting union_by_name=True"
)
"""DuckDB's wording when files of one read have different columns, candidates included."""


def _files_lack(column: str, tables: str) -> str:
    """Write out the text of a read whose files do not all have a column.

    Parameters
    ----------
    column
        The column.
    tables
        The table or tables, as the text names them.

    Returns
    -------
    str
        The whole text a user reads.
    """
    return (
        f"The data files of {tables} do not all have the column `{column}` (it was added after "
        "some or all of them were written). A read under a row filter or column mask selects "
        "every column of the table, so it fails on them whichever column the policy names. Ask "
        "the owner of the table to write it again, so that every file has the column."
    )


_NOT_FOUND_LINE = 'Query failed: Binder Error: Referenced column "tier" not found in FROM clause!'
"""The first line of the database's "not found", which the text for a named column starts with."""


def _named_column_text(tables: str) -> str:
    """Write out the text for a column the reader named that a table declares and its files lack.

    Parameters
    ----------
    tables
        The table or tables, as the text names them.

    Returns
    -------
    str
        The whole text a user reads, for the column `tier`.
    """
    return (
        f"{_NOT_FOUND_LINE} {tables} declares the column `tier`; if the query reads it from "
        "there, older data files of the table lack it (it was added after some or all of them "
        "were written). Ask the owner of the table to write it again, so that every file has "
        "the column."
    )


def test_a_files_mismatch_names_the_table_and_never_the_candidates() -> None:
    """The database lists the columns of one file; the reader is told the table and the column."""
    tables = {"gold.orders": frozenset({"id", "region", "tier"})}
    glob = ValidationError(_GLOB_MISMATCH.format(column="Tier"))

    failure = _schema_mismatch_failure(glob, tables, frozenset(), frozenset({"tier"}))

    assert isinstance(failure, ValidationError)
    assert str(failure) == _files_lack("tier", "`gold.orders`")
    assert "ssn" not in str(failure) and "Candidate" not in str(failure)


def test_a_column_not_found_that_the_reader_never_wrote_is_explained_as_the_files() -> None:
    """Only the generated read of the table can have named it: the files lack the column."""
    tables = {"gold.orders": frozenset({"id", "tier"})}
    binder = ValidationError(_BINDER.format(referenced="tier", candidate="ssn"))

    failure = _schema_mismatch_failure(binder, tables, frozenset({"gold.orders"}), frozenset())

    assert str(failure) == _files_lack("tier", "`gold.orders`")
    assert "ssn" not in str(failure)
    undeclared = {"gold.orders": frozenset({"id"})}
    assert _schema_mismatch_failure(binder, undeclared, frozenset(), frozenset()) is None
    # A plain `*` read: the same words are what a misspelled column gets, so they stay as they are.
    assert _schema_mismatch_failure(binder, tables, frozenset(), frozenset()) is None


@pytest.mark.parametrize("wording", ["binder", "glob"])
def test_a_column_the_reader_named_gets_one_text_whatever_the_wording(wording: str) -> None:
    """The reader's SQL names `tier`: the database's line plus a hint, in either file order."""
    tables = {"gold.orders": frozenset({"id", "tier"})}
    raw = (
        _BINDER.format(referenced="tier", candidate="ssn")
        if wording == "binder"
        else _GLOB_MISMATCH.format(column="tier")
    )

    failure = _schema_mismatch_failure(
        ValidationError(raw), tables, frozenset({"gold.orders"}), frozenset({"tier"})
    )

    assert str(failure) == _named_column_text("`gold.orders`")
    assert "ssn" not in str(failure)


def test_a_column_the_reader_named_of_an_unprojected_table_keeps_the_databases_text() -> None:
    """A plain `*` read has no generated projection to blame: only the candidates are cut."""
    tables = {"gold.orders": frozenset({"id", "tier"})}
    binder = ValidationError(_BINDER.format(referenced="tier", candidate="ssn"))

    failure = _schema_mismatch_failure(binder, tables, frozenset(), frozenset({"tier"}))

    assert failure is None


def test_a_files_mismatch_names_every_table_that_has_the_column() -> None:
    """Two tables of the query have the column: the text does not guess which one."""
    tables = {"gold.a": frozenset({"tier"}), "gold.b": frozenset({"tier"}), "gold.c": frozenset()}
    glob = ValidationError(_GLOB_MISMATCH.format(column="tier"))

    failure = _schema_mismatch_failure(glob, tables, frozenset(), frozenset())

    assert str(failure) == _files_lack("tier", "`gold.a` and `gold.b`")


def test_a_files_mismatch_on_a_column_no_table_declares_keeps_the_databases_message() -> None:
    """Nothing says whose files these are, so no text of ours: the cause stays visible."""
    tables = {"gold.orders": frozenset({"id"})}

    glob = ValidationError(_GLOB_MISMATCH.format(column="tier"))
    conversion = ValidationError("Query failed: Conversion Error")
    assert _schema_mismatch_failure(glob, tables, frozenset(), frozenset()) is None
    assert _schema_mismatch_failure(conversion, tables, frozenset(), frozenset()) is None


def test_a_row_filter_on_a_column_an_older_file_lacks_reads_it_as_null(
    session: Session, admin: PrincipalRef, tmp_path: Path
) -> None:
    """The checked snapshot has a file without `tier`; the filter reads NULL there and applies."""
    write(session, admin, _GOOD, WriteMode.CREATE)
    snapshot = write(
        session, admin, orders([-1], ["eu"], tier=["gold"]), WriteMode.APPEND, evolve_schema=True
    )
    result = _checked(
        session, admin, snapshot_id=snapshot, sql="SELECT id FROM gold.orders WHERE id < 0"
    )
    ana = reader(session, admin, "ana", "orders")
    filter(session, admin, "ana", "tier = 'gold'")

    assert _rows(session, tmp_path, ana, result) == [{"id": -1}]
    plain = reader(session, admin, "cy", "orders")
    assert _rows(session, tmp_path, plain, result) == [{"id": -1}]


def test_an_ordinary_read_under_a_filter_on_a_column_an_older_file_lacks_shows_what_passes(
    session: Session, admin: PrincipalRef, tmp_path: Path
) -> None:
    """The row written before `tier` existed reads NULL and fails the filter; nothing else does."""
    write(session, admin, _GOOD, WriteMode.CREATE)
    write(session, admin, orders([6], ["eu"], tier=["gold"]), WriteMode.APPEND, evolve_schema=True)
    ana = reader(session, admin, "ana", "orders")
    filter(session, admin, "ana", "tier = 'gold'")
    run_id = (
        QueryService(session, AllowAllAuthorization())
        .submit_query(ana, "dev", "SELECT id FROM gold.orders")
        .id
    )

    finished, rows = run(session, tmp_path, run_id)

    assert (finished.status, rows) == (QueryRunStatus.SUCCEEDED, [{"id": 6}])


@pytest.mark.parametrize("policy", ["filter", "mask"])
def test_a_policy_on_a_column_widened_since_is_named_as_the_policys_need(
    session: Session, admin: PrincipalRef, tmp_path: Path, policy: str
) -> None:
    """A widened column is not shown in the checked snapshot, and the read maps only shown ones.

    The policy reads `qty`, which the read of the snapshot does not map. The reader is told, before
    the query runs, that their policy needs a column the snapshot does not show, and to ask
    whoever manages policies; the text about a column in their own SQL would send them to a
    query that never names it.

    Parameters
    ----------
    session
        Database session.
    admin
        Sets the world up.
    tmp_path
        The storage root.
    policy
        A row filter on `qty`, or a column mask on `region` whose expression reads `qty`.
    """
    narrow = pa.table({"id": pa.array([2, -1], pa.int64()), "qty": pa.array([5, 6], pa.int32())})
    result = _warned_run(session, admin, narrow, "SELECT * FROM gold.orders WHERE id < 0")
    wide = pa.table({"id": pa.array([3], pa.int64()), "qty": pa.array([7], pa.int64())})
    write(session, admin, wide, WriteMode.REPLACE, evolve_schema=True)
    ana = reader(session, admin, "ana", "orders")
    if policy == "filter":
        filter(session, admin, "ana", "qty > 6")
    else:
        mask_as(session, admin, "ana", "id", "qty")

    message, cause = _failure(session, tmp_path, ana, result)

    assert message == policy_needs_a_column_message("qty", "gold.orders", result.run_id, "dev")
    assert cause == QueryErrorCause.INVALID_QUERY.value


def _add_a_column_without_writing(session: Session, admin: PrincipalRef) -> None:
    """Add the column `tier` to `gold.orders` through the catalog, writing no file.

    Parameters
    ----------
    session
        Database session.
    admin
        The principal that changes the table.
    """
    ids = _field_ids(session)
    schema = Schema(
        NestedField(ids["id"], "id", LongType(), required=False),
        NestedField(ids["region"], "region", StringType(), required=False),
        NestedField(ids["email"], "email", StringType(), required=False),
        NestedField(99, "tier", StringType(), required=False),
        schema_id=catalog(session).load_table(admin, ORDERS).metadata.current_schema_id + 1,
    )
    catalog(session).commit_table(
        admin,
        ORDERS,
        requirements=[],
        updates=[AddSchemaUpdate(schema_=schema), SetCurrentSchemaUpdate(schema_id=-1)],
    )


@pytest.mark.parametrize(("policy", "expected"), [("mask", [{"id": 1}]), ("filter", [])])
def test_a_column_added_without_a_write_reads_as_null_under_a_policy(
    session: Session,
    admin: PrincipalRef,
    tmp_path: Path,
    policy: str,
    expected: list[dict[str, int]],
) -> None:
    """No file has `tier` yet: a mask built from it is NULL, a filter on it passes nothing.

    Parameters
    ----------
    session
        Database session.
    admin
        Sets the world up.
    tmp_path
        The storage root.
    policy
        What makes the read name `tier`: a column mask's expression or a row filter.
    expected
        The rows the reader sees.
    """
    write(session, admin, _GOOD, WriteMode.CREATE)
    _add_a_column_without_writing(session, admin)
    ana = reader(session, admin, "ana", "orders")
    if policy == "mask":
        mask_as(session, admin, "ana", "email", "tier")
    else:
        filter(session, admin, "ana", "tier = 'x'")
    run_id = (
        QueryService(session, AllowAllAuthorization())
        .submit_query(ana, "dev", "SELECT id FROM gold.orders")
        .id
    )

    finished, rows = run(session, tmp_path, run_id)

    assert (finished.status, rows) == (QueryRunStatus.SUCCEEDED, expected)


def test_a_reader_who_wrote_a_missing_column_gets_the_fixed_message_and_no_hint(
    session: Session, admin: PrincipalRef, tmp_path: Path
) -> None:
    """`SELECT tier FROM (SELECT id FROM gold.orders)` is the reader's own mistake.

    Errors on reads under a row filter or column mask always show the fixed message.
    """
    write(session, admin, _GOOD, WriteMode.CREATE)
    _add_a_column_without_writing(session, admin)
    ana = reader(session, admin, "ana", "orders")
    filter(session, admin, "ana", "region = 'eu'")
    sql = "SELECT tier FROM (SELECT id FROM gold.orders) AS sub"
    run_id = QueryService(session, AllowAllAuthorization()).submit_query(ana, "dev", sql).id

    finished, rows = run(session, tmp_path, run_id)

    assert (finished.status, rows) == (QueryRunStatus.FAILED, [])
    assert finished.error_message == policy_read_failure("gold.orders")
    assert "older data files" not in finished.error_message


def test_a_view_that_leaves_the_column_out_is_not_blamed_on_the_files(
    session: Session, admin: PrincipalRef, tmp_path: Path
) -> None:
    """The view's own SELECT does not expose `tier`; the expanded SQL names it, so it is theirs."""
    write(session, admin, _GOOD, WriteMode.CREATE)
    _add_a_column_without_writing(session, admin)
    view = QueryService(session, AllowAllAuthorization()).submit_write(
        admin, "dev", "SELECT id FROM gold.orders", WriteTarget(table="gold.v", mode=WriteMode.VIEW)
    )
    run_one(session, view.id, AllowAllAuthorization())
    ana = reader(session, admin, "ana", "orders", "v")
    filter(session, admin, "ana", "region = 'eu'")
    run_id = (
        QueryService(session, AllowAllAuthorization())
        .submit_query(ana, "dev", "SELECT tier FROM gold.v")
        .id
    )

    finished, rows = run(session, tmp_path, run_id)

    assert (finished.status, rows) == (QueryRunStatus.FAILED, [])
    assert finished.error_message is not None
    assert "older data files" not in finished.error_message


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT id, tier FROM gold.orders", [{"id": 1, "tier": None}]),
        ("SELECT id FROM gold.orders WHERE tier = 'x'", []),
        ("SELECT o.id, o.tier FROM gold.orders AS o", [{"id": 1, "tier": None}]),
        ("SELECT id FROM gold.orders", [{"id": 1}]),
    ],
)
def test_a_reader_who_names_a_column_no_file_has_yet_reads_it_as_null(
    session: Session,
    admin: PrincipalRef,
    tmp_path: Path,
    sql: str,
    expected: list[dict[str, object]],
) -> None:
    """Under a filter on another column, by bare name, in a WHERE and through an alias.

    Parameters
    ----------
    session
        Database session.
    admin
        Sets the world up.
    tmp_path
        The storage root.
    sql
        A query that names `tier` in another place each time, or not at all.
    expected
        The rows the reader sees.
    """
    write(session, admin, _GOOD, WriteMode.CREATE)
    _add_a_column_without_writing(session, admin)
    ana = reader(session, admin, "ana", "orders")
    filter(session, admin, "ana", "region = 'eu'")
    run_id = QueryService(session, AllowAllAuthorization()).submit_query(ana, "dev", sql).id

    finished, rows = run(session, tmp_path, run_id)

    assert (finished.status, rows) == (QueryRunStatus.SUCCEEDED, expected)


def test_a_reader_reads_a_new_column_of_a_table_with_mixed_files(
    session: Session, admin: PrincipalRef, tmp_path: Path
) -> None:
    """An older file and a newer one that has `tier` read NULL and the value respectively."""
    write(session, admin, _GOOD, WriteMode.CREATE)
    write(session, admin, orders([6], ["eu"], tier=["gold"]), WriteMode.APPEND, evolve_schema=True)
    ana = reader(session, admin, "ana", "orders")
    filter(session, admin, "ana", "region = 'eu'")
    run_id = (
        QueryService(session, AllowAllAuthorization())
        .submit_query(ana, "dev", "SELECT id, tier FROM gold.orders")
        .id
    )

    finished, rows = run(session, tmp_path, run_id)

    assert (finished.status, rows) == (
        QueryRunStatus.SUCCEEDED,
        [{"id": 1, "tier": None}, {"id": 6, "tier": "gold"}],
    )


def test_a_reader_without_the_grant_keeps_the_original_failure_at_the_second_look(
    session: Session, admin: PrincipalRef
) -> None:
    """The second resolution asks for READ_TABLE first: no grant, no text about the snapshot."""
    result = _failed_run(session, admin)
    ana = reader(session, admin, "ana", "orders")
    nobody = reader(session, admin, "nobody")
    query = session.get(QueryRun, _submit(session, ana, result))
    assert query is not None
    _expire_the_failed_snapshot(session, admin, result)

    asked_as_ana = pipeline._snapshot_gone_after_failure(
        session,
        query,
        "dev",
        ana,
        ["gold.orders"],
        GrantAuthorization(session),
        None,
        ValidationError("storage"),
    )
    asked_as_nobody = pipeline._snapshot_gone_after_failure(
        session,
        query,
        "dev",
        nobody,
        ["gold.orders"],
        GrantAuthorization(session),
        None,
        ValidationError("storage"),
    )

    assert isinstance(asked_as_ana, CheckedSnapshotGoneError)
    assert asked_as_nobody is None


def test_a_stopping_worker_does_not_look_again(
    session: Session, admin: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """While the worker stops the run's outcome is rolled back: the second look is skipped."""
    result = _failed_run(session, admin)
    ana = reader(session, admin, "ana", "orders")
    query = session.get(QueryRun, _submit(session, ana, result))
    assert query is not None
    _expire_the_failed_snapshot(session, admin, result)
    monkeypatch.setattr(pipeline.ACTIVITY, "stopping", True)

    asked = pipeline._snapshot_gone_after_failure(
        session,
        query,
        "dev",
        ana,
        ["gold.orders"],
        GrantAuthorization(session),
        None,
        ValidationError("storage"),
    )

    assert asked is None
