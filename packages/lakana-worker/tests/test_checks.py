"""Counting a check as a query run of the run's principal: real catalog, grants, child."""

from __future__ import annotations

import sys
import uuid
from collections.abc import Callable
from pathlib import Path

import pyarrow as pa
import pytest
from sqlalchemy import select, update
from sqlalchemy.orm import Session

import lakana_worker.checks as checks_module
import lakana_worker.executor as executor_module
from lakana_core.auth import (
    Action,
    AllowAllAuthorization,
    AuthorizationService,
    PrincipalRef,
    SecurableKind,
)
from lakana_core.catalog.service import CatalogService
from lakana_core.config import QueryLimitsConfig, StorageConfig
from lakana_core.errors import (
    KeptAfterCheckError,
    NotFoundError,
    NotPublishedError,
    PermissionDenied,
    ValidationError,
)
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.grants.service import GrantService
from lakana_core.identifiers import TableRef
from lakana_core.models import (
    AuditEvent,
    AuditEventType,
    ColumnMask,
    Environment,
    JobVersion,
    Principal,
    PrincipalType,
    QueryOrigin,
    QueryRun,
    QueryRunStatus,
    Repository,
    RepositoryKind,
    Run,
    RunKind,
    RunStatus,
    Table,
    TaskRun,
    TaskStatus,
    WriteMode,
)
from lakana_core.policy.column_masks import ColumnMaskService
from lakana_core.policy.read_errors import policy_read_failure
from lakana_core.policy.row_filters import RowFilterService
from lakana_core.projects.checks import CheckSpec
from lakana_core.runs.checks import error_text
from lakana_core.storage.resolver import StorageResolver
from lakana_worker.checks import Counted, count_check
from lakana_worker.executor import QueryTimeout
from lakana_worker.pipeline import ReadOutcome

_DUPLICATES = "SELECT id FROM gold.orders GROUP BY id HAVING count(*) > 1"
"""A check that finds duplicated ids; both ids of the fixture's table are duplicated."""


@pytest.fixture
def admin(session: Session, tmp_path: Path) -> PrincipalRef:
    """Provide the principal that sets the world up, and the `dev` environment.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        The environment's storage root.

    Returns
    -------
    PrincipalRef
        The administrator.
    """
    principal_id = uuid.uuid4()
    session.add(Principal(id=principal_id, type=PrincipalType.USER, name="admin"))
    session.commit()
    ref = PrincipalRef(id=principal_id, name="admin", type=PrincipalType.USER)
    CatalogService(session, StorageResolver(session), AllowAllAuthorization()).create_environment(
        ref, name="dev", storage=StorageConfig(scheme="file", root=f"file://{tmp_path}", options={})
    )
    return ref


def _table(session: Session, admin: PrincipalRef, dotted: str, data: pa.Table) -> int:
    """Create a table with rows.

    Parameters
    ----------
    session
        Database session.
    admin
        The writer.
    dotted
        The table's dotted name.
    data
        Its rows.

    Returns
    -------
    int
        The table's current snapshot.
    """
    written = CatalogService(
        session, StorageResolver(session), AllowAllAuthorization()
    ).write_batches(
        admin,
        TableRef.from_dotted("dev", dotted),
        mode=WriteMode.CREATE,
        schema=data.schema,
        batches=data.to_batches(),
    )
    assert written.snapshot_id is not None
    return written.snapshot_id


def _grant_read(session: Session, admin: PrincipalRef, name: str, table: str) -> None:
    """Grant a principal READ_TABLE on one table or view of `gold`.

    Parameters
    ----------
    session
        Database session.
    admin
        The granting principal.
    name
        The grantee's name.
    table
        The table or view, in `gold`.
    """
    GrantService(session, AllowAllAuthorization()).grant(
        admin,
        principal=name,
        action=Action.READ_TABLE,
        kind=SecurableKind.TABLE,
        environment="dev",
        namespace="gold",
        table=table,
    )


@pytest.fixture
def snapshot_id(session: Session, admin: PrincipalRef) -> int:
    """Provide `gold.orders` with the rows (1, eu), (1, eu), (2, us), (2, us).

    Parameters
    ----------
    session
        Database session.
    admin
        The writer.

    Returns
    -------
    int
        The table's current snapshot.
    """
    return _table(
        session,
        admin,
        "gold.orders",
        pa.table(
            {
                "id": pa.array([1, 1, 2, 2], pa.int64()),
                "region": pa.array(["eu", "eu", "us", "us"], pa.string()),
            }
        ),
    )


@pytest.fixture
def runner(session: Session, admin: PrincipalRef, snapshot_id: int) -> PrincipalRef:
    """Provide the run's principal: a service account that may read `gold.orders`, unfiltered.

    Parameters
    ----------
    session
        Database session.
    admin
        The granting principal.
    snapshot_id
        Makes sure the table exists.

    Returns
    -------
    PrincipalRef
        The run's principal.
    """
    principal_id = uuid.uuid4()
    session.add(Principal(id=principal_id, type=PrincipalType.SERVICE_ACCOUNT, name="runner"))
    session.commit()
    _grant_read(session, admin, "runner", "orders")
    return PrincipalRef(id=principal_id, name="runner", type=PrincipalType.SERVICE_ACCOUNT)


@pytest.fixture
def filtered(session: Session, admin: PrincipalRef, runner: PrincipalRef) -> None:
    """Give the run's principal the row filter ``region = 'eu'`` on `gold.orders`.

    Parameters
    ----------
    session
        Database session.
    admin
        The principal that sets the filter.
    runner
        The run's principal.
    """
    catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
    RowFilterService(session, AllowAllAuthorization(), catalog).create(
        admin,
        principal=runner.name,
        environment="dev",
        namespace="gold",
        table="orders",
        expression="region = 'eu'",
    )


@pytest.fixture
def authz(session: Session) -> GrantAuthorization:
    """Provide the real authorization.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    GrantAuthorization
        Authorization by grants.
    """
    return GrantAuthorization(session)


@pytest.fixture
def environment_id(session: Session, admin: PrincipalRef) -> uuid.UUID:
    """Provide the id of `dev`.

    Parameters
    ----------
    session
        Database session.
    admin
        Makes sure the environment exists.

    Returns
    -------
    uuid.UUID
        The environment's id.
    """
    return session.execute(select(Environment.id).where(Environment.name == "dev")).scalar_one()


@pytest.fixture
def task(session: Session, runner: PrincipalRef, environment_id: uuid.UUID) -> TaskRun:
    """Provide a running task run of `gold.orders`, of a run bound to the run's principal.

    Parameters
    ----------
    session
        Database session.
    runner
        The run's principal.
    environment_id
        The environment.

    Returns
    -------
    TaskRun
        The task run, held by ``w1`` under claim 1, with no write run.
    """
    repo = Repository(
        id=uuid.uuid4(), name="analytics", kind=RepositoryKind.HTTPS, url="https://h/a.git"
    )
    session.add(repo)
    session.flush()
    version = JobVersion(
        id=uuid.uuid4(),
        repository_id=repo.id,
        environment_id=environment_id,
        commit_sha="a" * 40,
        manifest_json={},
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
        run_as_principal_id=runner.id,
        kind=RunKind.FULL,
        status=RunStatus.RUNNING,
        selection_json={},
    )
    session.add(run)
    session.flush()
    row = TaskRun(
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
    session.add(row)
    session.commit()
    return row


def _spec(sql: str = _DUPLICATES, table: str = "gold.orders") -> CheckSpec:
    """Build a check of a table of `gold`.

    Parameters
    ----------
    sql
        The check's SELECT.
    table
        The checked table.

    Returns
    -------
    CheckSpec
        The check `<table>/unique_id`.
    """
    return CheckSpec(
        name=f"{table}/unique_id",
        table=table,
        check="unique_id",
        path=f"checks/{table.replace('.', '/')}/unique_id.sql",
        on_failure="block",
        description=None,
        sql=sql,
    )


def _count(
    session: Session,
    task: TaskRun,
    principal: PrincipalRef,
    authz: AuthorizationService,
    spec: CheckSpec,
    limits: QueryLimitsConfig | None = None,
) -> Counted:
    """Count one check for the fixture's task.

    Parameters
    ----------
    session
        Database session.
    task
        The task run.
    principal
        The principal the count is submitted as.
    authz
        The authorization.
    spec
        The check.
    limits
        The query limits; the defaults when `None`.

    Returns
    -------
    Counted
        What `count_check` returned.
    """
    environment_id = session.execute(
        select(Run.environment_id).where(Run.id == task.run_id)
    ).scalar_one()
    return count_check(
        session,
        spec=spec,
        environment_id=environment_id,
        principal_id=principal.id,
        task_run_id=task.id,
        worker_id="w1",
        authz=authz,
        key_provider=None,
        limits=limits or QueryLimitsConfig(),
    )


def _stored(session: Session, counted: Counted) -> QueryRun:
    """Read the query run of a count.

    Parameters
    ----------
    session
        Database session.
    counted
        The count.

    Returns
    -------
    QueryRun
        Its run, as stored.
    """
    session.rollback()
    run = session.get(QueryRun, counted.query_run_id)
    assert run is not None
    session.refresh(run)
    return run


@pytest.mark.usefixtures("filtered")
def test_the_count_is_taken_under_the_run_principals_row_filter(
    session: Session,
    runner: PrincipalRef,
    authz: GrantAuthorization,
    task: TaskRun,
    environment_id: uuid.UUID,
    snapshot_id: int,
) -> None:
    """The count sees what the run's principal sees, and the result says so."""
    counted = count_check(
        session,
        spec=_spec(),
        environment_id=environment_id,
        principal_id=runner.id,
        task_run_id=task.id,
        worker_id="w1",
        authz=authz,
        key_provider=None,
        limits=QueryLimitsConfig(),
    )

    assert (counted.rows, counted.failure, counted.policy_applied) == (1, None, True)
    assert (counted.error_message, counted.error_cause) == (None, None)
    assert counted.checked_snapshot_id == snapshot_id
    run = _stored(session, counted)
    assert (run.status, run.task_run_id, run.worker_id, run.result_location, run.write_mode) == (
        QueryRunStatus.SUCCEEDED,
        task.id,
        "w1",
        None,
        None,
    )
    assert (run.principal_id, run.environment_id) == (runner.id, environment_id)
    assert run.sql_text == (
        "SELECT COUNT(*) AS failing FROM (SELECT id FROM gold.orders GROUP BY id "
        "HAVING COUNT(*) > 1) AS lakana_check"
    )
    assert counted.started_at is not None and run.started_at is not None
    # Without the filter the count would be 2: both ids are duplicated.


def test_the_same_check_counted_as_a_principal_without_the_filter_sees_both_ids(
    session: Session, admin: PrincipalRef, authz: GrantAuthorization, task: TaskRun
) -> None:
    """Control for the test above: the filter, not the data, made the count 1."""
    other = uuid.uuid4()
    session.add(Principal(id=other, type=PrincipalType.SERVICE_ACCOUNT, name="unfiltered"))
    session.commit()
    _grant_read(session, admin, "unfiltered", "orders")
    unfiltered = PrincipalRef(id=other, name="unfiltered", type=PrincipalType.SERVICE_ACCOUNT)

    counted = _count(session, task, unfiltered, authz, _spec())

    assert (counted.rows, counted.policy_applied) == (2, False)


def test_a_check_count_run_is_of_origin_check(
    session: Session,
    runner: PrincipalRef,
    authz: GrantAuthorization,
    task: TaskRun,
) -> None:
    """The count of a check is a run of the worker's own, marked as a check's."""
    counted = _count(session, task, runner, authz, _spec())

    assert _stored(session, counted).origin is QueryOrigin.CHECK


def test_a_counted_check_stores_no_result_file(
    session: Session,
    runner: PrincipalRef,
    authz: GrantAuthorization,
    task: TaskRun,
    tmp_path: Path,
) -> None:
    """The one row comes back to the worker; nothing of it reaches the object store."""
    for _ in range(3):
        counted = _count(session, task, runner, authz, _spec())
        assert counted.rows == 2
    assert not (tmp_path / "_artifacts" / "queries").exists()
    events = session.scalars(
        select(AuditEvent).where(AuditEvent.event_type == AuditEventType.QUERY_COMPLETED)
    ).all()
    assert [event.detail["row_count"] for event in events] == [1, 1, 1]


def test_a_count_without_a_policy_says_so(
    session: Session, runner: PrincipalRef, authz: GrantAuthorization, task: TaskRun
) -> None:
    counted = _count(session, task, runner, authz, _spec())
    assert (counted.rows, counted.policy_applied, counted.failure) == (2, False, None)


def test_a_column_mask_on_a_joined_table_counts_as_a_policy(
    session: Session,
    admin: PrincipalRef,
    runner: PrincipalRef,
    authz: GrantAuthorization,
    task: TaskRun,
    snapshot_id: int,
) -> None:
    """Finding 9: a filter or a mask on ANY table the check reads, not only the checked one."""
    _table(
        session,
        admin,
        "gold.customers",
        pa.table({"id": pa.array([1, 2], pa.int64()), "ssn": pa.array(["a", "b"], pa.string())}),
    )
    _grant_read(session, admin, "runner", "customers")
    catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
    ColumnMaskService(session, AllowAllAuthorization(), catalog).create(
        admin,
        principal="runner",
        environment="dev",
        namespace="gold",
        table="customers",
        column="ssn",
        expression="NULL",
    )
    spec = _spec(
        "SELECT o.id FROM gold.orders o LEFT JOIN gold.customers c ON c.id = o.id "
        "WHERE c.ssn IS NULL"
    )

    counted = _count(session, task, runner, authz, spec)

    # Every ssn reads as NULL under the mask, so all four orders "have no customer".
    assert (counted.rows, counted.policy_applied) == (4, True)
    assert counted.checked_snapshot_id == snapshot_id


def test_a_check_the_principal_may_not_read_fails_with_its_cause(
    session: Session,
    admin: PrincipalRef,
    runner: PrincipalRef,
    authz: GrantAuthorization,
    task: TaskRun,
) -> None:
    """Finding 7: the stored text names no table; the cause is what the worker words it from."""
    _table(session, admin, "gold.secret", pa.table({"id": pa.array([1], pa.int64())}))
    spec = _spec("SELECT o.id FROM gold.orders o JOIN gold.secret s ON s.id = o.id")

    counted = _count(session, task, runner, authz, spec)

    assert (counted.rows, counted.error_cause) == (None, "permission_denied")
    assert isinstance(counted.failure, PermissionDenied)
    assert counted.error_message == (
        "Resolving the tables this query reads failed (PermissionDenied)."
    )
    assert (counted.checked_snapshot_id, counted.policy_applied) == (None, False)
    run = _stored(session, counted)
    assert (run.status, run.result_location) == (QueryRunStatus.FAILED, None)


def test_a_check_of_a_missing_table_has_the_cause_not_found(
    session: Session, runner: PrincipalRef, task: TaskRun
) -> None:
    spec = _spec("SELECT o.id FROM gold.orders o JOIN gold.nowhere n ON n.id = o.id")

    counted = _count(session, task, runner, AllowAllAuthorization(), spec)

    assert (counted.rows, counted.error_cause) == (None, "not_found")
    assert isinstance(counted.failure, NotFoundError)
    assert counted.error_message == "Resolving the tables this query reads failed (NotFoundError)."


def test_a_check_of_a_table_that_is_not_published_fails_with_that_error(
    session: Session, runner: PrincipalRef, authz: GrantAuthorization, task: TaskRun
) -> None:
    """Addition B.8: the worker tells this refusal from a missing table by the failure's type."""
    session.execute(
        update(Table).where(Table.name == "orders").values(unpublished_run_id=uuid.uuid4())
    )
    session.commit()

    counted = _count(session, task, runner, authz, _spec())

    assert (counted.rows, counted.error_cause) == (None, "not_found")
    assert isinstance(counted.failure, NotPublishedError)


def _keep(session: Session, name: str = "orders") -> uuid.UUID:
    """Put the note of a failed `keep` check on a table of `gold`.

    Parameters
    ----------
    session
        Database session; commits.
    name
        The table.

    Returns
    -------
    uuid.UUID
        The run the note names.
    """
    run_id = uuid.uuid4()
    session.execute(
        update(Table)
        .where(Table.name == name)
        .values(kept_run_id=run_id, kept_task_run_id=uuid.uuid4(), kept_check=f"gold.{name}/old")
    )
    session.commit()
    return run_id


def _builds(session: Session, task: TaskRun, *tables: str) -> None:
    """Make the task's project one that builds the given tables.

    Parameters
    ----------
    session
        Database session; commits.
    task
        The task run; its run's project version gets the manifest.
    *tables
        The assets of the manifest.
    """
    version_id = session.execute(
        select(Run.job_version_id).where(Run.id == task.run_id)
    ).scalar_one()
    session.execute(
        update(JobVersion)
        .where(JobVersion.id == version_id)
        .values(manifest_json={"version": 3, "assets": [{"table": t} for t in tables]})
    )
    session.commit()


def test_the_check_of_a_kept_table_counts_it(
    session: Session, runner: PrincipalRef, authz: GrantAuthorization, task: TaskRun
) -> None:
    """The count runs as the task that owns the check, and that task's asset is the table."""
    _keep(session)
    _builds(session, task, "gold.orders")
    assert task.asset == "gold.orders"

    counted = _count(session, task, runner, authz, _spec())

    assert (counted.rows, counted.failure, counted.error_cause) == (2, None, None)


def test_the_check_task_of_a_project_that_does_not_build_the_kept_table_cannot_count(
    session: Session, runner: PrincipalRef, authz: GrantAuthorization, task: TaskRun
) -> None:
    """A `source_check` task of another project has the table as its asset, and no standing."""
    _keep(session)
    _builds(session, task, "gold.report")
    assert task.asset == "gold.orders"

    counted = _count(session, task, runner, authz, _spec())

    assert (counted.rows, counted.error_cause) == (None, "not_found")
    assert isinstance(counted.failure, KeptAfterCheckError)


def test_a_count_of_another_table_of_the_project_reads_the_kept_table(
    session: Session, runner: PrincipalRef, authz: GrantAuthorization, task: TaskRun
) -> None:
    """The count of a check builds nothing, so it reads a kept table of its own project."""
    _keep(session)
    _builds(session, task, "gold.orders", "gold.report")
    task.asset = "gold.report"
    session.commit()

    counted = _count(session, task, runner, authz, _spec())

    assert (counted.rows, counted.failure) == (2, None)


def test_a_check_of_another_task_that_reads_a_kept_table_fails_with_that_error(
    session: Session,
    runner: PrincipalRef,
    authz: GrantAuthorization,
    task: TaskRun,
    tmp_path: Path,
) -> None:
    """The worker tells this refusal by its type, and the text is safe to store."""
    kept = _keep(session)
    task.asset = "gold.report"
    session.commit()

    counted = _count(session, task, runner, authz, _spec())

    assert (counted.rows, counted.error_cause) == (None, "not_found")
    assert isinstance(counted.failure, KeptAfterCheckError)
    assert counted.error_message == (
        f"`gold.orders` was kept after a failed check (`gold.orders/old`, run {kept}) and has "
        "not passed its checks since, so this task does not read it. A run of the project that "
        "builds `gold.orders` clears this: build it again there "
        "(`lakana run --select gold.orders --env dev`), or repair the data and run that project. "
        "If no project builds it any more, drop it: `lakana table delete gold.orders --env dev`."
    )
    assert str(tmp_path) not in counted.error_message
    assert "file://" not in counted.error_message
    assert ".parquet" not in counted.error_message and "metadata" not in counted.error_message
    assert _stored(session, counted).result_location is None


def test_a_task_that_may_not_read_a_kept_table_learns_nothing_about_the_note(
    session: Session,
    admin: PrincipalRef,
    runner: PrincipalRef,
    authz: GrantAuthorization,
    task: TaskRun,
) -> None:
    """The refusal comes after the grant: without it, the opaque permission text and no more."""
    _table(session, admin, "gold.secret", pa.table({"id": pa.array([1], pa.int64())}))
    kept = _keep(session, "secret")
    spec = _spec("SELECT o.id FROM gold.orders o JOIN gold.secret s ON s.id = o.id")

    counted = _count(session, task, runner, authz, spec)

    assert (counted.rows, counted.error_cause) == (None, "permission_denied")
    assert isinstance(counted.failure, PermissionDenied)
    assert counted.error_message is not None
    assert "kept" not in counted.error_message and str(kept) not in counted.error_message


def test_a_check_that_does_not_plan_fails_with_invalid_query(
    session: Session, runner: PrincipalRef, authz: GrantAuthorization, task: TaskRun
) -> None:
    counted = _count(session, task, runner, authz, _spec("SELECT nope FROM gold.orders"))

    assert (counted.rows, counted.error_cause) == (None, "invalid_query")
    assert isinstance(counted.failure, ValidationError)
    assert counted.error_message is not None and "Binder Error" in counted.error_message
    assert _stored(session, counted).status is QueryRunStatus.FAILED


def test_a_value_the_query_engine_quotes_stays_out_of_the_checks_error(
    session: Session,
    admin: PrincipalRef,
    runner: PrincipalRef,
    authz: GrantAuthorization,
    task: TaskRun,
) -> None:
    """Addition B.7: DuckDB names the value it could not convert; `error_text` does not."""
    _table(
        session,
        admin,
        "gold.people",
        pa.table({"name": pa.array(["Alice Smith"], pa.string())}),
    )
    _grant_read(session, admin, "runner", "people")

    # The cast is in the WHERE clause: under a count the select list is never evaluated.
    counted = _count(
        session,
        task,
        runner,
        authz,
        _spec("SELECT name FROM gold.people WHERE CAST(name AS INTEGER) > 0"),
    )

    assert counted.error_cause == "invalid_query"
    # A table is read by field id, and the query child words a failed conversion without the
    # value; `error_text` still cuts a raw engine message, as another read path can store it.
    assert counted.error_message is not None
    assert counted.error_message.startswith("Query failed: a value could not be converted")
    assert "Alice Smith" not in counted.error_message
    raw = "Query failed: Conversion Error: Could not convert string 'Alice Smith' to INT32"
    worded = error_text(counted.error_cause, raw, runner.name)
    assert worded == (
        "Conversion Error: the check's query failed. Run the check's SQL yourself to see the "
        "full message."
    )


def test_a_check_under_a_failing_mask_has_the_fixed_text_in_its_error(
    session: Session,
    admin: PrincipalRef,
    runner: PrincipalRef,
    authz: GrantAuthorization,
    task: TaskRun,
) -> None:
    """The count's run and the check's `error` hold the fixed message, never the stored value."""
    _table(
        session,
        admin,
        "gold.people",
        pa.table({"name": pa.array(["HIDDEN-X"], pa.string())}),
    )
    _grant_read(session, admin, "runner", "people")
    catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
    ColumnMaskService(session, AllowAllAuthorization(), catalog).create(
        admin,
        principal="runner",
        environment="dev",
        namespace="gold",
        table="people",
        column="name",
        expression="NULL",
    )
    # The service refuses a cast; an edit in the database can store one.
    session.execute(update(ColumnMask).values(expression="CAST(name AS INTEGER) || 'x'"))
    session.commit()

    # The mask is in the WHERE clause: under a count the select list is never evaluated.
    counted = _count(
        session,
        task,
        runner,
        authz,
        _spec("SELECT name FROM gold.people WHERE name IS NOT NULL", table="gold.people"),
    )

    assert counted.error_cause == "invalid_query"
    assert counted.error_message == policy_read_failure("gold.people")
    worded = error_text(counted.error_cause, counted.error_message, runner.name)
    assert worded == policy_read_failure("gold.people")
    assert "HIDDEN-X" not in worded
    assert "HIDDEN-X" not in (_stored(session, counted).error_message or "")


def test_a_check_of_a_view_has_no_checked_snapshot(
    session: Session,
    admin: PrincipalRef,
    runner: PrincipalRef,
    authz: GrantAuthorization,
    task: TaskRun,
) -> None:
    """A view has no snapshot of its own; the count is taken through it all the same."""
    catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
    catalog.create_or_replace_view(
        admin,
        TableRef.from_dotted("dev", "gold.eu_orders"),
        sql="SELECT id FROM gold.orders WHERE region = 'eu'",
        references=catalog.resolve_lineage_sources(admin, "dev", ["gold.orders"]),
        columns=[],
    )
    _grant_read(session, admin, "runner", "eu_orders")
    spec = _spec("SELECT id FROM gold.eu_orders", table="gold.eu_orders")

    counted = _count(session, task, runner, authz, spec)

    assert (counted.rows, counted.failure) == (2, None)
    assert counted.checked_snapshot_id is None


def test_the_count_runs_under_the_given_limits(
    session: Session,
    runner: PrincipalRef,
    authz: GrantAuthorization,
    task: TaskRun,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The limits are the caller's: a count that outlives the time limit is stopped."""
    script = Path(__file__).parent / "scripted_child.py"
    monkeypatch.setattr(executor_module, "_ARGV", [sys.executable, str(script), "stops_when_asked"])

    counted = _count(
        session,
        task,
        runner,
        authz,
        _spec(),
        QueryLimitsConfig(timeout_seconds=0.5, kill_grace_seconds=0.4),
    )

    assert counted.rows is None
    assert isinstance(counted.failure, QueryTimeout)
    run = _stored(session, counted)
    assert (run.status, run.error_cause) == (QueryRunStatus.FAILED, "timeout")
    assert counted.error_cause == "timeout"


def test_the_counts_run_does_not_become_the_tasks_write_run(
    session: Session, runner: PrincipalRef, authz: GrantAuthorization, task: TaskRun
) -> None:
    """The task's `query_run_id` is its write: a count never takes that place."""
    counted = _count(session, task, runner, authz, _spec())

    session.rollback()
    bound = session.execute(select(TaskRun.query_run_id).where(TaskRun.id == task.id)).scalar_one()
    assert bound is None
    assert _stored(session, counted).task_run_id == task.id


def test_a_count_that_was_refused_before_it_ran_is_a_failure(
    session: Session, runner: PrincipalRef, authz: GrantAuthorization, task: TaskRun
) -> None:
    """`run_one` refuses a deactivated principal and returns nothing: that is not a count of 0."""
    session.execute(update(Principal).where(Principal.id == runner.id).values(is_active=False))
    session.commit()

    counted = _count(session, task, runner, authz, _spec())

    assert (counted.rows, counted.error_cause) == (None, "permission_denied")
    assert isinstance(counted.failure, RuntimeError)
    assert counted.error_message == "Principal is no longer active."


def test_a_check_that_is_not_one_select_is_never_submitted(
    session: Session, runner: PrincipalRef, authz: GrantAuthorization, task: TaskRun
) -> None:
    """A manifest edited by hand: `count_query` refuses, and no query run is left behind."""
    with pytest.raises(ValidationError):
        _count(session, task, runner, authz, _spec("DROP TABLE gold.orders"))
    session.rollback()
    assert session.scalars(select(QueryRun)).all() == []


def _ended(status: QueryRunStatus, *, hands_over: bool) -> Callable[..., BaseException | None]:
    """Build a stand-in for `run_one` that ends the count's run without running a query.

    Parameters
    ----------
    status
        How the run ends.
    hands_over
        Whether the stand-in hands a result of one row (the value 0) to the callback first.

    Returns
    -------
    callable
        A stand-in with `run_one`'s signature, which returns no failure.
    """

    def stand_in(
        session: Session,
        query_run_id: uuid.UUID,
        *args: object,
        on_result: Callable[[ReadOutcome], None],
        **kwargs: object,
    ) -> BaseException | None:
        if hands_over:
            on_result(ReadOutcome(pa.table({"failing": [0]}), {"gold.orders": 7}, frozenset()))
        run = session.get(QueryRun, query_run_id)
        assert run is not None
        run.status = status
        session.commit()
        return None

    return stand_in


@pytest.mark.parametrize(
    ("status", "hands_over"),
    [
        (QueryRunStatus.SUCCEEDED, False),
        (QueryRunStatus.FAILED, True),
        (QueryRunStatus.CANCELLED, True),
        (QueryRunStatus.RUNNING, True),
    ],
)
def test_a_count_needs_both_a_succeeded_run_and_a_result(
    session: Session,
    runner: PrincipalRef,
    authz: GrantAuthorization,
    task: TaskRun,
    monkeypatch: pytest.MonkeyPatch,
    status: QueryRunStatus,
    hands_over: bool,
) -> None:
    """Nothing turns a missing or disowned count into "the check found no rows"."""
    monkeypatch.setattr(checks_module, "run_one", _ended(status, hands_over=hands_over))

    counted = _count(session, task, runner, authz, _spec())

    assert counted.rows is None
    assert isinstance(counted.failure, RuntimeError)
    assert (counted.checked_snapshot_id, counted.policy_applied) == (None, False)


def test_a_succeeded_run_with_a_result_is_a_count(
    session: Session,
    runner: PrincipalRef,
    authz: GrantAuthorization,
    task: TaskRun,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The control of the test above."""
    monkeypatch.setattr(checks_module, "run_one", _ended(QueryRunStatus.SUCCEEDED, hands_over=True))

    counted = _count(session, task, runner, authz, _spec())

    assert (counted.rows, counted.failure, counted.checked_snapshot_id) == (0, None, 7)
