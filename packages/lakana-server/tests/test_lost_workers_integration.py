"""On Postgres: what the unit tier cannot show about the write fence.

SQLite has no row locks, so the unit tests can only show that the fence's `WHERE` clause
refuses. Here a worker thread runs a real write on real Postgres and SeaweedFS and is held
at the fence, before or after `ProtectedWriteGuard.record_pending`, while the test ends the
run or tries to. Every wait has a bound.
"""

from __future__ import annotations

import secrets
import threading
import time
import uuid
from collections.abc import Generator, Iterable, Iterator
from contextlib import contextmanager
from datetime import timedelta

import pyarrow as pa
import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from lakana_core.auth import AllowAllAuthorization, PrincipalRef
from lakana_core.catalog import service as service_module
from lakana_core.catalog.query_service import QueryService
from lakana_core.catalog.service import CatalogService, Rollback
from lakana_core.catalog.writes import WriteResult
from lakana_core.config import ViewAuditConfig
from lakana_core.db import db_now, rowcount
from lakana_core.errors import WriteOutcomeUnknownError
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.identifiers import TableRef
from lakana_core.models import (
    AttemptOutcome,
    AuditEventType,
    DeployRun,
    DeployStatus,
    EnvironmentDeployment,
    JobVersion,
    PrincipalType,
    QueryOrigin,
    QueryRun,
    QueryRunStatus,
    RollbackOutcome,
    TaskAttempt,
    TaskRun,
    TaskStatus,
    Worker,
    WriteMode,
)
from lakana_core.projects.deploys import DeployService
from lakana_core.runs import workers
from lakana_core.runs.checks import failure_message as check_failure_message
from lakana_core.runs.checks import results_of_task, verdict
from lakana_core.runs.lockdown import ProtectedWriteGuard
from lakana_core.runs.scheduler import TaskTransition
from lakana_core.runs.store import (
    CHECK_LOST_MESSAGE,
    COMMITTED_BEFORE_LOST_MESSAGE,
    DEPLOY_LOST_MESSAGE,
    WORKER_LOST_WRITE_MESSAGE,
    apply_transitions,
    end_lost_runs,
    scheduler_tick,
)
from lakana_core.runs.tasks import LeaseState, renew_lease
from lakana_core.storage.resolver import StorageResolver
from lakana_worker import checks as worker_checks
from lakana_worker import pipeline
from lakana_worker import tasks as worker_tasks
from lakana_worker.deploy import DeploySettings
from lakana_worker.poller import poll_once
from lakana_worker.tasks import RunEngine, TaskSettings
from tests.integration_helpers import (
    HoldPoint,
    StatementPause,
    commit_files,
    run_in_thread,
    task_row,
    unique_name,
    wait_for_lock_waiter,
    write_check,
)
from tests.python_asset_helpers import IDS, QueryStack, completions, query_stack

pytestmark = pytest.mark.integration

_WAIT_SECONDS = 30.0
_JOIN_SECONDS = 60.0
_WORKER_ID = "fence-test-worker"
_LEASE_SECONDS = 60


class _Died(BaseException):
    """Raised inside the worker thread to make the worker "die" between two statements."""


@pytest.fixture(scope="module")
def stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[QueryStack]:
    """Start Postgres, SeaweedFS and a real HTTP server.

    Parameters
    ----------
    tmp_path_factory
        For the repo root.

    Yields
    ------
    QueryStack
        The stack.
    """
    with query_stack(tmp_path_factory) as built:
        yield built


@contextmanager
def _worker(
    stack: QueryStack, *, dies_with: type[BaseException] | None = None
) -> Generator[threading.Thread, None, None]:
    """Run a query worker under `_WORKER_ID` in a thread, its row registered first.

    Parameters
    ----------
    stack
        The stack.
    dies_with
        A private exception type the thread is expected to end with (the worker "dies"); it is
        not re-raised.

    Yields
    ------
    threading.Thread
        The worker's thread, alive.

    Raises
    ------
    BaseException
        Whatever ended the thread, re-raised after it was joined.
    AssertionError
        If the thread does not end in time.
    """
    with Session(stack.db) as session:
        workers.register(session, _WORKER_ID, host="test", version="test")
        session.commit()
    stop = threading.Event()
    errors: list[BaseException] = []

    def loop() -> None:
        """Claim and run queued queries until stopped, recording whatever escapes."""
        try:
            while not stop.is_set():
                with Session(stack.db) as session:
                    busy = poll_once(
                        session,
                        GrantAuthorization(session),
                        only_queries=True,
                        worker_id=_WORKER_ID,
                    )
                if not busy:
                    stop.wait(0.05)
        except BaseException as error:
            if dies_with is None or not isinstance(error, dies_with):
                errors.append(error)

    thread = threading.Thread(target=loop, name="lakana-test-fence-worker", daemon=True)
    thread.start()
    try:
        yield thread
    finally:
        stop.set()
        thread.join(timeout=_JOIN_SECONDS)
    assert not thread.is_alive(), "the worker thread did not end"
    if errors:
        raise errors[0]


class _Hold(HoldPoint):
    """Holds a fenced write at `record_pending`, before or after the real call.

    Parameters
    ----------
    monkeypatch
        Installs the wrapper on the class.
    after
        Hold after the real call (the fence has passed and its lock is held) instead of before.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, after: bool) -> None:
        super().__init__(_WAIT_SECONDS)
        real = ProtectedWriteGuard.record_pending
        held_by = self

        def wrapper(guard: ProtectedWriteGuard) -> None:
            """Wait at the hold for a fenced write; pass any other through.

            Parameters
            ----------
            guard
                The guard.
            """
            if guard._fence is None:  # the server's own writes share this process
                real(guard)
                return
            if not after:
                held_by.wait_here()
            real(guard)
            if after:
                held_by.wait_here()

        monkeypatch.setattr(ProtectedWriteGuard, "record_pending", wrapper)


def _ids(stack: QueryStack, environment: str) -> list[int]:
    """Read `bronze.nums` through a worker that is started for the read only.

    Parameters
    ----------
    stack
        The stack.
    environment
        The environment.

    Returns
    -------
    list of int
        The ids, sorted.
    """
    with _worker(stack):
        rows = stack.admin.sql("SELECT id FROM bronze.nums ORDER BY id", environment, timeout=60)
    return rows.column("id").to_pylist()


def _run_row(stack: QueryStack, run_id: str) -> QueryRun:
    """Read a run's committed row.

    Parameters
    ----------
    stack
        The stack.
    run_id
        The run's id.

    Returns
    -------
    QueryRun
        The row, detached.
    """
    return stack.row(run_id)


def test_a_write_resumed_after_its_run_was_ended_changes_nothing(
    stack: QueryStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Frozen before the fence, the run ended meanwhile: the table and the run stay as they were."""
    environment = stack.environment()
    before = _ids(stack, environment)
    hold = _Hold(monkeypatch, after=False)
    run = stack.admin.queries.submit(
        "SELECT id + 100 AS id FROM bronze.nums", environment, into="bronze.nums", mode="append"
    )
    with _worker(stack):
        assert hold.reached.wait(_WAIT_SECONDS), "the write never reached the fence"
        with Session(stack.db) as session:
            session.execute(
                text(
                    "UPDATE query_runs SET status = 'FAILED', error_message = 'ended' "
                    "WHERE id = :id"
                ),
                {"id": uuid.UUID(run.id)},
            )
            session.commit()
        hold.release.set()
    row = _run_row(stack, run.id)
    assert (row.status, row.error_message, row.rows_written) == (
        QueryRunStatus.FAILED,
        "ended",
        None,
    )
    assert _ids(stack, environment) == before


def test_the_run_is_locked_from_the_fence_to_the_commit(
    stack: QueryStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After the fence, nobody else can end the run until the write's transaction is over."""
    environment = stack.environment()
    hold = _Hold(monkeypatch, after=True)
    run = stack.admin.queries.submit(
        "SELECT id + 100 AS id FROM bronze.nums", environment, into="bronze.nums", mode="append"
    )
    run_id = uuid.UUID(run.id)
    with _worker(stack):
        assert hold.reached.wait(_WAIT_SECONDS), "the write never reached the fence"
        skipped: list[object] = []
        with Session(stack.db) as session:
            skipped.extend(
                session.execute(
                    text("SELECT id FROM query_runs WHERE id = :id FOR UPDATE SKIP LOCKED"),
                    {"id": run_id},
                ).all()
            )
            blocker = session.execute(
                text(
                    "SELECT pid FROM pg_locks l JOIN pg_class c ON c.oid = l.relation "
                    "WHERE c.relname = 'query_runs' AND l.mode = 'RowExclusiveLock' "
                    "AND l.granted AND l.pid <> pg_backend_pid() LIMIT 1"
                )
            ).scalar_one()
        assert skipped == []
        matched: list[int] = []

        def late_sweep() -> None:
            """Try to end the run, as a sweep would; it waits for the write's transaction."""
            with Session(stack.db) as session:
                result = session.execute(
                    text(
                        "UPDATE query_runs SET status = 'FAILED' WHERE id = :id "
                        "AND status = 'RUNNING' AND rows_written IS NULL"
                    ),
                    {"id": run_id},
                )
                matched.append(rowcount(result))
                session.commit()

        sweeper = threading.Thread(target=late_sweep, daemon=True)
        sweeper.start()
        wait_for_lock_waiter(stack.db, blocker, timeout=10)
        hold.release.set()
        sweeper.join(timeout=_JOIN_SECONDS)
        assert not sweeper.is_alive()
        status = run.wait(timeout=60)
    assert matched == [0]  # it found a run that already has its evidence
    assert (status.status, status.rows_written) == ("SUCCEEDED", 3)
    assert _ids(stack, environment) == [1, 2, 3, 101, 102, 103]
    assert _run_row(stack, run.id).rows_written == 3


def _age_the_heartbeat(stack: QueryStack) -> None:
    """Make the test worker's heartbeat a lease and a second old, by the database's clock."""
    with Session(stack.db) as session:
        session.execute(
            text(
                "UPDATE workers SET heartbeat_at = now() - make_interval(secs => :age) "
                "WHERE id = :id"
            ),
            {"age": _LEASE_SECONDS + 1, "id": _WORKER_ID},
        )
        session.commit()


def _sweep(stack: QueryStack) -> int:
    """Run the scheduler's sweep once, as the leader would.

    Parameters
    ----------
    stack
        The stack.

    Returns
    -------
    int
        The number of runs ended.
    """
    with Session(stack.db) as session:
        return end_lost_runs(session, db_now(session), _LEASE_SECONDS)


def _assert_key_share_lock_held(stack: QueryStack, run_id: str) -> None:
    """Assert the held worker has a `FOR KEY SHARE` lock on the run's row, and nothing stronger.

    This is the precondition of the lock-mode pin: if the lineage insert ever moved after the
    fence, a sweep with `FOR UPDATE` would pass the first test as well and the pin would be lost.

    Parameters
    ----------
    stack
        The stack.
    run_id
        The run's id.
    """
    query = "SELECT id FROM query_runs WHERE id = :id FOR {mode} SKIP LOCKED"
    with Session(stack.db) as session:
        for_update = session.execute(text(query.format(mode="UPDATE")), {"id": uuid.UUID(run_id)})
        assert for_update.all() == [], "the row is not locked by the held worker"
        session.rollback()
        no_key = session.execute(
            text(query.format(mode="NO KEY UPDATE")), {"id": uuid.UUID(run_id)}
        )
        assert len(no_key.all()) == 1, "the held worker holds more than a key-share lock"
        session.rollback()


def test_a_write_held_before_its_commit_is_ended_and_then_changes_nothing(
    stack: QueryStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = stack.environment()
    before = _ids(stack, environment)
    hold = _Hold(monkeypatch, after=False)
    run = stack.admin.queries.submit(
        "SELECT id + 200 AS id FROM bronze.nums", environment, into="bronze.nums", mode="append"
    )
    with _worker(stack):
        assert hold.reached.wait(_WAIT_SECONDS), "the write never reached the fence"
        _age_the_heartbeat(stack)
        _assert_key_share_lock_held(stack, run.id)
        ended: list[int] = []
        sweeper = threading.Thread(target=lambda: ended.append(_sweep(stack)), daemon=True)
        sweeper.start()
        sweeper.join(timeout=20)
        finished = not sweeper.is_alive()
        hold.release.set()
        assert finished, "the sweep waited for the lock of a frozen write"
        assert ended == [1]
    row = _run_row(stack, run.id)
    assert (row.status, row.error_cause, row.rows_written) == (
        QueryRunStatus.FAILED,
        "worker_lost",
        None,
    )
    assert row.error_message is not None and "Nothing was written to bronze.nums" in (
        row.error_message
    )
    assert _ids(stack, environment) == before
    assert completions(stack, run.id) == ["FAILED"]  # the sweep's; none from the worker


def test_a_write_that_committed_before_its_worker_died_ends_succeeded_with_its_rows(
    stack: QueryStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = stack.environment()

    def die(*args: object, **kwargs: object) -> bool:
        raise _Died

    monkeypatch.setattr(pipeline, "_keep_a_prior_stamp", die)
    run = stack.admin.queries.submit(
        "SELECT id + 300 AS id FROM bronze.nums", environment, into="bronze.nums", mode="append"
    )
    with _worker(stack, dies_with=_Died) as thread:
        thread.join(timeout=_JOIN_SECONDS)
        assert not thread.is_alive(), "the worker did not die at its status update"
        row = _run_row(stack, run.id)
        assert (row.status, row.rows_written) == (QueryRunStatus.RUNNING, 3)
        _age_the_heartbeat(stack)
        assert _sweep(stack) == 1
    monkeypatch.undo()
    assert _run_row(stack, run.id).status is QueryRunStatus.SUCCEEDED
    assert _ids(stack, environment) == [1, 2, 3, 301, 302, 303]
    assert completions(stack, run.id) == ["SUCCEEDED"]


def test_the_sweep_does_not_wait_for_a_write_that_holds_its_run(
    stack: QueryStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = stack.environment()
    hold = _Hold(monkeypatch, after=True)
    run = stack.admin.queries.submit(
        "SELECT id + 400 AS id FROM bronze.nums", environment, into="bronze.nums", mode="append"
    )
    with _worker(stack):
        assert hold.reached.wait(_WAIT_SECONDS), "the write never reached the fence"
        _age_the_heartbeat(stack)
        ended: list[int] = []
        sweeper = threading.Thread(target=lambda: ended.append(_sweep(stack)), daemon=True)
        sweeper.start()
        sweeper.join(timeout=20)
        released = not sweeper.is_alive()
        hold.release.set()
        assert released, "the sweep waited for the row lock of a write"
        assert ended == [0]
        assert _run_row(stack, run.id).status in (QueryRunStatus.RUNNING, QueryRunStatus.SUCCEEDED)
        status = run.wait(timeout=60)
    assert status.status == "SUCCEEDED"
    assert completions(stack, run.id) == ["SUCCEEDED"]  # the worker's own


def test_a_cancel_of_a_lost_run_answers_cancelled_at_once(
    stack: QueryStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = stack.environment()
    before = _ids(stack, environment)
    hold = _Hold(monkeypatch, after=False)
    run = stack.admin.queries.submit(
        "SELECT id + 500 AS id FROM bronze.nums", environment, into="bronze.nums", mode="append"
    )
    with _worker(stack):
        assert hold.reached.wait(_WAIT_SECONDS), "the write never reached the fence"
        _age_the_heartbeat(stack)
        assert run.cancel().status == "CANCELLED"
        hold.release.set()
    assert _run_row(stack, run.id).status is QueryRunStatus.CANCELLED
    assert _ids(stack, environment) == before


class _DeployHold(HoldPoint):
    """Holds a deploy's finishing call, at the entry of `record_deploy` or right after the lock.

    Parameters
    ----------
    monkeypatch
        Installs the wrapper on the class.
    after_lock
        Hold after `_hold` returned (the run's row is locked) instead of at the entry.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, after_lock: bool) -> None:
        super().__init__(_WAIT_SECONDS)
        held_by = self
        if after_lock:
            real_hold = DeployService._hold

            def hold(service: DeployService, *args: object, **kwargs: object) -> None:
                """Take the lock, then wait to be released.

                Parameters
                ----------
                service
                    The deploy service.
                *args
                    Passed through.
                **kwargs
                    Passed through.
                """
                real_hold(service, *args, **kwargs)  # ty: ignore[invalid-argument-type]
                held_by.wait_here()

            monkeypatch.setattr(DeployService, "_hold", hold)
        else:
            real_record = DeployService.record_deploy

            def record(service: DeployService, *args: object, **kwargs: object) -> object:
                """Wait to be released, then record.

                Parameters
                ----------
                service
                    The deploy service.
                *args
                    Passed through.
                **kwargs
                    Passed through.

                Returns
                -------
                object
                    What the real call returns.
                """
                held_by.wait_here()
                return real_record(service, *args, **kwargs)  # ty: ignore[invalid-argument-type]

            monkeypatch.setattr(DeployService, "record_deploy", record)


@contextmanager
def _deploy_worker(stack: QueryStack) -> Generator[threading.Thread, None, None]:
    """Run one poll of a worker under `_WORKER_ID` that claims a deploy, in a thread.

    Parameters
    ----------
    stack
        The stack.

    Yields
    ------
    threading.Thread
        The worker's thread, alive.

    Raises
    ------
    BaseException
        Whatever ended the thread, re-raised after it was joined.
    AssertionError
        If the thread does not end in time.
    """
    with Session(stack.db) as session:
        workers.register(session, _WORKER_ID, host="test", version="test")
        session.commit()
    errors: list[BaseException] = []
    settings = DeploySettings(repo_roots=(str(stack.services.roots),))

    def poll() -> None:
        """Claim and run the one queued deploy, recording whatever escapes."""
        try:
            with Session(stack.db) as session:
                poll_once(
                    session, GrantAuthorization(session), deploy=settings, worker_id=_WORKER_ID
                )
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=poll, name="lakana-test-deploy-worker", daemon=True)
    thread.start()
    try:
        yield thread
    finally:
        thread.join(timeout=_JOIN_SECONDS)
    assert not thread.is_alive(), "the worker thread did not end"
    if errors:
        raise errors[0]


def _queued_deploy(stack: QueryStack) -> tuple[str, str, str]:
    """Register a repository with one asset and queue a deploy of it.

    Parameters
    ----------
    stack
        The stack.

    Returns
    -------
    tuple of str
        The environment's name, the repository's name and the run's id.
    """
    environment, repository = stack.environment(), unique_name()
    commit_files(
        stack.services.roots / repository,
        {"lakana.yaml": "", "assets/silver/x.sql": "SELECT id FROM bronze.nums\n"},
    )
    stack.admin.repositories.create(repository, "local", str(stack.services.roots / repository))
    return environment, repository, stack.admin.deploys.create(environment, repository).id


def _deploy_state(stack: QueryStack, run_id: str) -> tuple[DeployStatus, str | None, int, int]:
    """Read a deploy run's status and message, and how many versions and pointers exist for it.

    Parameters
    ----------
    stack
        The stack.
    run_id
        The run's id.

    Returns
    -------
    tuple
        Status, message, project versions of its repository, pointers of its repository.
    """
    with Session(stack.db) as session:
        run = session.get(DeployRun, uuid.UUID(run_id))
        assert run is not None
        versions = session.scalar(
            select(func.count())
            .select_from(JobVersion)
            .where(JobVersion.repository_id == run.repository_id)
        )
        pointers = session.scalar(
            select(func.count())
            .select_from(EnvironmentDeployment)
            .where(EnvironmentDeployment.repository_id == run.repository_id)
        )
        return run.status, run.error_message, versions or 0, pointers or 0


def test_a_deploy_held_before_it_records_is_ended_and_then_activates_nothing(
    stack: QueryStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, run_id = _queued_deploy(stack)
    hold = _DeployHold(monkeypatch, after_lock=False)
    with _deploy_worker(stack):
        assert hold.reached.wait(_WAIT_SECONDS), "the deploy never reached its record"
        _age_the_heartbeat(stack)
        assert _sweep(stack) >= 1
        assert _deploy_state(stack, run_id)[:2] == (DeployStatus.FAILED, DEPLOY_LOST_MESSAGE)
        hold.release.set()
    assert _deploy_state(stack, run_id) == (DeployStatus.FAILED, DEPLOY_LOST_MESSAGE, 0, 0)


def test_a_deploy_that_recorded_is_not_touched_by_the_sweep(
    stack: QueryStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, run_id = _queued_deploy(stack)
    hold = _DeployHold(monkeypatch, after_lock=True)
    with _deploy_worker(stack):
        assert hold.reached.wait(_WAIT_SECONDS), "the deploy never took its lock"
        _age_the_heartbeat(stack)
        swept: list[int] = []
        sweeper = threading.Thread(target=lambda: swept.append(_sweep(stack)), daemon=True)
        sweeper.start()
        sweeper.join(timeout=20)
        returned = not sweeper.is_alive()
        state_while_held = _deploy_state(stack, run_id)[0]
        hold.release.set()
        assert returned, "the sweep waited for the row lock of a finishing deploy"
        assert state_while_held is DeployStatus.RUNNING
    status, message, versions, pointers = _deploy_state(stack, run_id)
    assert (status, message, versions, pointers) == (DeployStatus.SUCCEEDED, None, 1, 1)


def test_a_deploy_that_waits_behind_the_sweeps_open_transaction_sees_the_run_ended(
    stack: QueryStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """READ COMMITTED: `_hold` re-reads the row once the sweep's lock is released."""
    _, _, run_id = _queued_deploy(stack)
    hold = _DeployHold(monkeypatch, after_lock=False)
    with _deploy_worker(stack):
        assert hold.reached.wait(_WAIT_SECONDS), "the deploy never reached its record"
        with Session(stack.db) as sweep:
            blocker = sweep.execute(text("SELECT pg_backend_pid()")).scalar_one()
            sweep.execute(
                text(
                    "UPDATE deploy_runs SET status = 'FAILED', error_message = :message "
                    "WHERE id = :id AND status = 'RUNNING'"
                ),
                {"id": uuid.UUID(run_id), "message": DEPLOY_LOST_MESSAGE},
            )
            hold.release.set()
            wait_for_lock_waiter(stack.db, blocker, timeout=20)  # `_hold` waits for the sweep
            sweep.commit()
    assert _deploy_state(stack, run_id) == (DeployStatus.FAILED, DEPLOY_LOST_MESSAGE, 0, 0)


def test_the_query_list_of_ended_runs_reads_the_finished_at_index(stack: QueryStack) -> None:
    """`query list --all` on Postgres: the plan names the index, or the plan is reported."""
    environment = stack.environment()
    others = [stack.environment() for _ in range(9)]
    with Session(stack.db) as session:
        names = ", ".join(f"'{name}'" for name in [environment, *others])
        ids = session.execute(
            text(f"SELECT id FROM environments WHERE name IN ({names}) ORDER BY name")
        ).scalars()
        array = ", ".join(f"'{env_id}'::uuid" for env_id in ids)
        session.execute(
            text(
                "INSERT INTO query_runs (id, environment_id, sql_text, status, created_at, "
                "updated_at, finished_at) "
                f"SELECT gen_random_uuid(), (ARRAY[{array}])[1 + mod(n, 10)], 'SELECT 1', "
                "'SUCCEEDED', now(), now(), now() - make_interval(secs => n) "
                "FROM generate_series(1, 20000) AS n"
            )
        )
        session.commit()
        session.execute(text("ANALYZE query_runs"))
        session.commit()
        statements: list[tuple[str, object]] = []

        def capture(
            conn: object, cursor: object, statement: str, parameters: object, *_: object
        ) -> None:
            """Remember the statements of the listing.

            Parameters
            ----------
            conn
                The connection.
            cursor
                The cursor.
            statement
                The SQL.
            parameters
                Its parameters.
            *_
                Ignored.
            """
            statements.append((statement, parameters))

        manager = PrincipalRef(id=uuid.uuid4(), name="manager", type=PrincipalType.USER)
        # This test reads the plan of the listing; the view audit (which needs the manager as a
        # row of `principals`) is pinned by `test_view_audit.py`.
        quiet = ViewAuditConfig(enabled=False)
        event.listen(stack.db, "before_cursor_execute", capture)
        try:
            rows = QueryService(session, AllowAllAuthorization(), view_audit=quiet).list_queries(
                manager, environment, include_ended=True, limit=100
            )
        finally:
            event.remove(stack.db, "before_cursor_execute", capture)
        assert len(rows) == 100
        ended = [
            (sql, params)
            for sql, params in statements
            if "finished_at >=" in sql and "ORDER BY query_runs.finished_at DESC" in sql
        ]
        assert len(ended) == 1, [sql for sql, _ in statements]
        sql, params = ended[0]
        plan = "\n".join(
            row[0]
            for row in session.connection().exec_driver_sql("EXPLAIN " + sql, params)  # ty: ignore[invalid-argument-type]
        )
    print(plan)  # shown on failure and with -s
    assert "ix_query_runs_environment_id_finished_at" in plan, plan


def _sql_task_with_a_write(stack: QueryStack, rows: int | None) -> tuple[uuid.UUID, uuid.UUID]:
    """Make a deployed pipeline run whose SQL task is `running` with a write of a dead worker.

    The task holds a lease of 50 seconds; its write run carries a worker id whose heartbeat is
    a lease and a second old.

    Parameters
    ----------
    stack
        The stack.
    rows
        The write's `rows_written` evidence, or `None`.

    Returns
    -------
    tuple of uuid.UUID
        The task run's id and the write run's id.
    """
    dead = f"task-dead-{uuid.uuid4().hex[:8]}"
    environment, repository, deploy_id = _queued_deploy(stack)
    with _deploy_worker(stack):
        pass
    assert _deploy_state(stack, deploy_id)[0] is DeployStatus.SUCCEEDED
    started = stack.admin.runs.start(environment, repository)
    with Session(stack.db) as session:
        scheduler_tick(session, db_now(session), secrets.SystemRandom().random)
        task = session.scalars(
            select(TaskRun).where(
                TaskRun.asset == "silver.x", TaskRun.run_id == uuid.UUID(started.id)
            )
        ).one()
        pipeline_run = task.run_id
        owner = session.execute(
            text("SELECT run_as_principal_id, environment_id FROM runs WHERE id = :id"),
            {"id": pipeline_run},
        ).one()
        beat = db_now(session) - timedelta(seconds=_LEASE_SECONDS + 1)
        session.add(Worker(id=dead, host="h", version="1", started_at=beat, heartbeat_at=beat))
        write = QueryRun(
            origin=QueryOrigin.TASK,
            id=uuid.uuid4(),
            environment_id=owner.environment_id,
            principal_id=owner.run_as_principal_id,
            sql_text="SELECT 1",
            status=QueryRunStatus.RUNNING,
            worker_id=dead,
            started_at=db_now(session),
            task_run_id=task.id,
            write_mode=WriteMode.APPEND,
            target_name="silver.x",
            rows_written=rows,
        )
        session.add(write)
        session.flush()
        task.status = TaskStatus.RUNNING
        task.attempt = 1
        task.claim_count = 1
        task.lease_owner = dead
        task.lease_expires_at = db_now(session) + timedelta(seconds=50)
        task.query_run_id = write.id
        session.add(
            TaskAttempt(
                id=uuid.uuid4(),
                task_run_id=task.id,
                attempt=1,
                worker=dead,
                started_at=db_now(session),
            )
        )
        session.commit()
        return task.id, write.id


@pytest.mark.parametrize("rows", [None, 4])
def test_a_sql_tasks_write_under_a_live_task_lease_is_left_to_the_task_path(
    stack: QueryStack, rows: int | None
) -> None:
    """On Postgres the sweep skips the write under a live lease; the task path ends it once."""
    task_id, write_id = _sql_task_with_a_write(stack, rows)
    rand = secrets.SystemRandom().random
    with Session(stack.db) as session:
        now = db_now(session)
        # The counts are the instance's, which earlier tests of this module share: only this
        # test's own write is asserted on.
        end_lost_runs(session, now, _LEASE_SECONDS)
        scheduler_tick(session, now, rand, _LEASE_SECONDS)
        session.commit()
    assert stack.row(str(write_id)).status is QueryRunStatus.RUNNING
    assert completions(stack, str(write_id)) == []
    with Session(stack.db) as session:
        session.execute(
            text(
                "UPDATE task_runs SET lease_expires_at = now() - interval '5 seconds' "
                "WHERE id = :id"
            ),
            {"id": task_id},
        )
        session.commit()
        now = db_now(session)
        assert scheduler_tick(session, now, rand, _LEASE_SECONDS) >= 1
        ended = stack.row(str(write_id))
        scheduler_tick(session, now, rand, _LEASE_SECONDS)  # a second tick changes nothing
    again = stack.row(str(write_id))
    assert (ended.status, ended.error_cause, ended.error_message) == (
        (QueryRunStatus.FAILED, None, WORKER_LOST_WRITE_MESSAGE)  # the task path's stamp
        if rows is None
        else (QueryRunStatus.SUCCEEDED, None, None)  # the evidence: the task is not repeated
    )
    assert (again.status, again.error_cause, again.error_message, again.finished_at) == (
        ended.status,
        ended.error_cause,
        ended.error_message,
        ended.finished_at,
    )
    assert completions(stack, str(write_id)) == []


def _expire_lease(stack: QueryStack, task_id: uuid.UUID) -> None:
    """Make a task's lease run out, by the database's clock.

    Parameters
    ----------
    stack
        The stack.
    task_id
        The task run.
    """
    with Session(stack.db) as session:
        session.execute(
            text(
                "UPDATE task_runs SET lease_expires_at = now() - interval '5 seconds' "
                "WHERE id = :id"
            ),
            {"id": task_id},
        )
        session.commit()


def test_a_commit_that_lands_while_the_reclaim_waits_for_the_task_stops_the_retry(
    stack: QueryStack,
) -> None:
    """The reclaim read "no evidence"; the write commits while it waits; it must not retry.

    The write holds the task's row `FOR SHARE` from its fence to its commit, so the reclaim's
    first statement (the lock) waits for it. The statements after the lock are new statements
    with new snapshots: a single `UPDATE` that waited would judge the evidence by the snapshot
    it took before the wait, and put the task back.
    """
    task_id, write_id = _sql_task_with_a_write(stack, None)
    _expire_lease(stack, task_id)
    with Session(stack.db) as session:
        task = session.get(TaskRun, task_id)
        assert task is not None
        run_id, owner = task.run_id, task.lease_owner
        stale = TaskTransition(
            run_id,
            task_id,
            TaskStatus.RUNNING,
            TaskStatus.PENDING,
            expected_owner=owner,
            expected_claim=1,
            next_attempt_at=db_now(session),
            lost=True,
        )
    results: list[bool] = []
    with Session(stack.db) as writer:
        writer.execute(text("SELECT id FROM task_runs WHERE id = :id FOR SHARE"), {"id": task_id})
        writer.execute(
            text("UPDATE query_runs SET rows_written = 5 WHERE id = :id"), {"id": write_id}
        )
        blocker = writer.execute(text("SELECT pg_backend_pid()")).scalar_one()

        def reclaim() -> None:
            """Apply the stale reclaim; it waits for the writer's lock."""
            with Session(stack.db) as session:
                results.append(apply_transitions(session, [stale], db_now(session)))

        thread = threading.Thread(target=reclaim, daemon=True)
        thread.start()
        wait_for_lock_waiter(stack.db, blocker, timeout=10)
        writer.commit()
    thread.join(timeout=_JOIN_SECONDS)
    assert not thread.is_alive()
    assert results == [False]  # it saw the evidence the writer committed
    with Session(stack.db) as session:
        task = session.get(TaskRun, task_id)
        assert task is not None
        assert (task.status, task.lease_owner) == (TaskStatus.RUNNING, owner)
    assert stack.row(str(write_id)).status is QueryRunStatus.RUNNING


_FRONT_MATTER = "/* lakana\nmaterialize: incremental\nincremental_key: id\n*/\n"


@contextmanager
def _task_worker(
    stack: QueryStack, worker_id: str, *, dies_with: type[BaseException] | None = None
) -> Generator[threading.Thread, None, None]:
    """Run a worker that claims tasks (and deploys and queries), in a thread.

    Parameters
    ----------
    stack
        The stack.
    worker_id
        The worker's id.
    dies_with
        A private exception type the thread is expected to end with.

    Yields
    ------
    threading.Thread
        The worker's thread, alive.

    Raises
    ------
    BaseException
        Whatever ended the thread, re-raised after it was joined.
    AssertionError
        If the thread does not end in time.
    """
    with Session(stack.db) as session:
        workers.register(session, worker_id, host="test", version="test")
        session.commit()
    engine = RunEngine(
        session_factory=lambda: Session(stack.db),
        authz_factory=GrantAuthorization,
        settings=TaskSettings(
            worker_id=worker_id, lease_seconds=_LEASE_SECONDS, renew_seconds=20.0
        ),
    )
    settings = DeploySettings(repo_roots=(str(stack.services.roots),))
    stop = threading.Event()
    errors: list[BaseException] = []

    def loop() -> None:
        """Poll until the queues are empty (or the thread dies), recording what escapes."""
        try:
            while not stop.is_set():
                with Session(stack.db) as session:
                    busy = poll_once(
                        session,
                        GrantAuthorization(session),
                        deploy=settings,
                        engine=engine,
                        worker_id=worker_id,
                    )
                if not busy:
                    return
        except BaseException as error:
            if dies_with is None or not isinstance(error, dies_with):
                errors.append(error)

    thread = threading.Thread(target=loop, name=f"lakana-test-{worker_id}", daemon=True)
    thread.start()
    try:
        yield thread
    finally:
        stop.set()
        thread.join(timeout=_JOIN_SECONDS)
    assert not thread.is_alive(), "the worker thread did not end"
    if errors:
        raise errors[0]


def _tick(stack: QueryStack) -> None:
    """Run one scheduler tick with the sweep, as the leader would.

    Parameters
    ----------
    stack
        The stack.
    """
    with Session(stack.db) as session:
        scheduler_tick(session, db_now(session), secrets.SystemRandom().random, _LEASE_SECONDS)


def test_a_sql_tasks_write_that_committed_before_its_worker_died_is_not_run_again(
    stack: QueryStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first worker dies after the commit; the table has the rows once, the task ran once.

    The asset's key is all NULL, so no watermark can tell a second run what the first wrote: the
    old behaviour appended the three rows again. The downstream task then runs after the
    succeeded one, as after any success.
    """
    environment, repository = stack.environment(), unique_name()
    stack.admin.tables.create(environment, "silver.x", schema=IDS)
    commit_files(
        stack.services.roots / repository,
        {
            "lakana.yaml": "defaults:\n  retry_delay: 0\n",
            "assets/silver/x.sql": _FRONT_MATTER
            + "SELECT CAST(NULL AS BIGINT) AS id FROM bronze.nums\n",
            "assets/gold/y.sql": "SELECT count(*) AS n FROM silver.x\n",
        },
    )
    stack.admin.repositories.create(repository, "local", str(stack.services.roots / repository))
    deploy = stack.admin.deploys.create(environment, repository)
    with _task_worker(stack, "task-deployer"):
        pass
    assert _deploy_state(stack, deploy.id)[0] is DeployStatus.SUCCEEDED
    started = stack.admin.runs.start(environment, repository)
    _tick(stack)  # the run starts, the root task is ready

    def die(*args: object, **kwargs: object) -> bool:
        raise _Died

    monkeypatch.setattr(pipeline, "_keep_a_prior_stamp", die)
    with _task_worker(stack, "task-doomed", dies_with=_Died) as thread:
        thread.join(timeout=_JOIN_SECONDS)
        assert not thread.is_alive(), "the worker did not die at its status update"
    monkeypatch.undo()
    with Session(stack.db) as session:
        task = session.scalars(
            select(TaskRun).where(
                TaskRun.run_id == uuid.UUID(started.id), TaskRun.asset == "silver.x"
            )
        ).one()
        task_id = task.id
        assert task.status is TaskStatus.RUNNING and task.attempt == 1
        write = session.get(QueryRun, task.query_run_id)
        assert write is not None and (write.status, write.rows_written) == (
            QueryRunStatus.RUNNING,
            3,
        )
    _expire_lease(stack, task_id)
    for _ in range(6):
        _tick(stack)
        with _task_worker(stack, "task-second"):
            pass
        finished = stack.admin.runs.get(environment, started.id)
        if finished.status in {"succeeded", "failed", "cancelled"}:
            break
    assert finished.status == "succeeded", finished
    with Session(stack.db) as session:
        task = session.get(TaskRun, task_id)
        assert task is not None
        assert (task.status, task.attempt, task.error_message) == (
            TaskStatus.SUCCEEDED,
            1,
            COMMITTED_BEFORE_LOST_MESSAGE,
        )
        (attempt,) = session.scalars(select(TaskAttempt).where(TaskAttempt.task_run_id == task_id))
        assert (attempt.outcome, attempt.rows_written) == (AttemptOutcome.SUCCEEDED, 3)
        assert stack.row(str(task.query_run_id)).status is QueryRunStatus.SUCCEEDED
        downstream = session.scalars(
            select(TaskRun).where(
                TaskRun.run_id == uuid.UUID(started.id), TaskRun.asset == "gold.y"
            )
        ).one()
        assert (downstream.status, downstream.attempt) == (TaskStatus.SUCCEEDED, 1)
    with _worker(stack):
        count = stack.admin.sql("SELECT count(*) AS n FROM silver.x", environment, timeout=60)
        built = stack.admin.sql("SELECT n FROM gold.y", environment, timeout=60)
    assert count.column("n").to_pylist() == [3]  # once, not twice
    assert built.column("n").to_pylist() == [3]


class _InFlight:
    """A commit that was answered with an error and is still being applied by the server.

    The writer's own transaction is rolled back inside `commit` and a second connection moves
    the table's pointer to the location the writer produced, without committing: the state of a
    backend that is inside its `COMMIT` when the client gives up. The writer's `commit` then
    raises, as a dropped connection would.

    Parameters
    ----------
    stack
        The stack.
    monkeypatch
        Installs the wrapper on the session.
    environment
        The environment whose `bronze.nums` is appended to.
    """

    def __init__(
        self, stack: QueryStack, monkeypatch: pytest.MonkeyPatch, environment: str
    ) -> None:
        self.environment = environment
        self.stack = stack
        self.monkeypatch = monkeypatch
        self.in_flight = Session(stack.db)
        self.blocker: int | None = None
        self.deleted: list[list[str]] = []

    def install(self, session: Session) -> None:
        """Make the writer's first `commit` leave the commit in flight and raise.

        Parameters
        ----------
        session
            The writer's session.
        """
        done: list[int] = []

        def commit() -> None:
            """Roll back, move the pointer on the second connection, and fail.

            Raises
            ------
            OperationalError
                Always, as a connection that dropped after the commit was sent.
            """
            if done:
                session.rollback()
                return
            done.append(1)
            row = session.execute(
                text(
                    "SELECT t.id, t.metadata_location FROM tables t "
                    "JOIN namespaces n ON n.id = t.namespace_id "
                    "JOIN environments e ON e.id = n.environment_id "
                    "WHERE e.name = :env AND t.name = 'nums'"
                ),
                {"env": self.environment},
            ).one()
            session.rollback()
            self.in_flight.execute(
                text("UPDATE tables SET metadata_location = :l WHERE id = :id"),
                {"l": row.metadata_location, "id": row.id},
            )
            self.blocker = self.in_flight.execute(text("SELECT pg_backend_pid()")).scalar_one()
            raise OperationalError("COMMIT", None, ConnectionResetError("connection reset"))

        self.commit = commit
        self.monkeypatch.setattr(session, "commit", commit)
        real = service_module.cleanup_written_paths

        def recording(resolved: object, paths: Iterable[str]) -> None:
            """Remember what the clean-up deletes, and delete it."""
            self.deleted.append(list(paths))
            real(resolved, self.deleted[-1])  # ty: ignore[invalid-argument-type]

        self.monkeypatch.setattr(service_module, "cleanup_written_paths", recording)


def _append_through(
    stack: QueryStack,
    environment: str,
    flight: _InFlight,
    results: list[object],
) -> threading.Thread:
    """Start an append whose commit is left in flight, in a thread.

    Parameters
    ----------
    stack
        The stack.
    environment
        The environment (its table `bronze.nums` is appended to).
    flight
        The in-flight commit.
    results
        Receives the result or the exception.

    Returns
    -------
    threading.Thread
        The started thread.
    """
    admin = PrincipalRef(id=stack.principal_id("admin"), name="admin", type=PrincipalType.USER)

    def run() -> None:
        """Write, recording what comes out."""
        try:
            with Session(stack.db) as session:
                catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
                flight.install(session)
                data = pa.table({"id": pa.array([10, 11], pa.int64())})
                results.append(
                    catalog.write_batches(
                        admin,
                        TableRef.from_dotted(environment, "bronze.nums"),
                        mode=WriteMode.APPEND,
                        schema=data.schema,
                        batches=data.to_batches(),
                    )
                )
        except BaseException as error:
            results.append(error)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


@pytest.mark.parametrize("ends", ["commits", "rolls back"])
def test_the_check_after_a_failed_commit_waits_for_a_commit_still_in_flight(
    stack: QueryStack, monkeypatch: pytest.MonkeyPatch, ends: str
) -> None:
    """A plain read would answer "not landed" and delete files the commit then names."""
    environment = stack.environment()
    flight = _InFlight(stack, monkeypatch, environment)
    results: list[object] = []
    thread = _append_through(stack, environment, flight, results)
    deadline = time.monotonic() + _WAIT_SECONDS
    while flight.blocker is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert flight.blocker is not None, "the write never reached its commit"
    wait_for_lock_waiter(stack.db, flight.blocker, timeout=10)  # the check is waiting
    assert results == [] and flight.deleted == []
    if ends == "commits":
        flight.in_flight.commit()
    else:
        flight.in_flight.rollback()
    thread.join(timeout=_JOIN_SECONDS)
    assert not thread.is_alive()
    flight.in_flight.close()
    if ends == "commits":
        (result,) = results
        assert isinstance(result, WriteResult) and result.rows == 2
        assert flight.deleted == []
        assert _ids(stack, environment) == [1, 2, 3, 10, 11]
    else:
        (error,) = results
        assert isinstance(error, OperationalError)
        assert len(flight.deleted) == 1 and flight.deleted[0]
        assert _ids(stack, environment) == [1, 2, 3]


def test_a_check_that_times_out_waiting_makes_the_outcome_unknown(
    stack: QueryStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wait is bounded, and a timeout keeps the files: never "not landed"."""
    monkeypatch.setattr(service_module, "LOCK_TIMEOUT_SECONDS", 1)
    environment = stack.environment()
    flight = _InFlight(stack, monkeypatch, environment)
    results: list[object] = []
    thread = _append_through(stack, environment, flight, results)
    thread.join(timeout=_JOIN_SECONDS)
    assert not thread.is_alive()
    (error,) = results
    assert isinstance(error, WriteOutcomeUnknownError)
    assert flight.deleted == []
    flight.in_flight.rollback()
    flight.in_flight.close()
    assert _ids(stack, environment) == [1, 2, 3]


# --- a task with checks whose worker is lost, or is waited for ------------------------

_PASSES = "SELECT id FROM silver.x WHERE id < 0"
_FAILS = "SELECT id FROM silver.x WHERE id > 2"


def _retire_leftovers(stack: QueryStack) -> None:
    """Cancel every active run and waiting task, so that a worker claims only this test's.

    Earlier tests of this module leave tasks that a tick makes ready again; a worker that is
    meant to die at one place of this test's task would die in one of those instead.

    Parameters
    ----------
    stack
        The stack.
    """
    with Session(stack.db) as session:
        session.execute(
            text("UPDATE runs SET status = 'CANCELLED' WHERE status IN ('QUEUED', 'RUNNING')")
        )
        session.execute(
            text(
                "UPDATE task_runs SET status = 'CANCELLED' "
                "WHERE status IN ('PENDING', 'READY', 'RUNNING')"
            )
        )
        session.commit()


def _run_with_checks(
    stack: QueryStack, files: dict[str, str], check: str, ids: list[int] | None = None
) -> tuple[str, str, str]:
    """Deploy a project whose ``silver.x`` has one check, and start a run whose root is ready.

    Parameters
    ----------
    stack
        The stack.
    files
        The project's assets; ``silver.x`` among them.
    check
        The SQL of the check ``silver.x/rule``.
    ids
        Create ``silver.x`` with these ids before the deploy (an empty list creates it
        without rows); `None` leaves the table to the run.

    Returns
    -------
    tuple of str
        The environment, the repository and the run's id.
    """
    _retire_leftovers(stack)
    environment, repository = stack.environment(), unique_name()
    if ids is not None:
        stack.admin.tables.create(environment, "silver.x", schema=IDS)
    if ids:
        stack.admin.tables.append(
            environment, "silver.x", pa.table({"id": pa.array(ids, pa.int64())})
        )
    path = stack.services.roots / repository
    write_check(path, "silver.x", "rule", check)
    commit_files(path, {"lakana.yaml": "defaults:\n  retry_delay: 0\n", **files})
    stack.admin.repositories.create(repository, "local", str(path))
    deploy = stack.admin.deploys.create(environment, repository)
    with _task_worker(stack, "task-deployer"):
        pass
    assert _deploy_state(stack, deploy.id)[0] is DeployStatus.SUCCEEDED
    run = stack.admin.runs.start(environment, repository)
    _tick(stack)
    return environment, repository, run.id


def _task_queries(stack: QueryStack, task_id: uuid.UUID, *, writes: bool) -> list[QueryRun]:
    """Read a task's own query runs: its writes, or the counts of its checks.

    Parameters
    ----------
    stack
        The stack.
    task_id
        The task run.
    writes
        The runs with a write mode; otherwise those without one (the counts).

    Returns
    -------
    list of QueryRun
        The rows, oldest first, detached.
    """
    mode = QueryRun.write_mode.is_not(None) if writes else QueryRun.write_mode.is_(None)
    with Session(stack.db) as session:
        rows = list(
            session.scalars(
                select(QueryRun)
                .where(QueryRun.task_run_id == task_id, mode)
                .order_by(QueryRun.created_at, QueryRun.id)
            )
        )
        session.expunge_all()
        return rows


def _attempt_outcomes(stack: QueryStack, task_id: uuid.UUID) -> list[AttemptOutcome | None]:
    """Read the outcomes of a task's attempts, oldest first.

    Parameters
    ----------
    stack
        The stack.
    task_id
        The task run.

    Returns
    -------
    list
        One outcome per attempt.
    """
    with Session(stack.db) as session:
        return list(
            session.scalars(
                select(TaskAttempt.outcome)
                .where(TaskAttempt.task_run_id == task_id)
                .order_by(TaskAttempt.started_at, TaskAttempt.attempt)
            )
        )


def test_the_written_stamp_commits_while_a_lease_renewal_waits(stack: QueryStack) -> None:
    """The stamp's `UPDATE` of a row its own transaction holds `FOR SHARE`, with a writer queued.

    The real write of a SQL task is paused right before it sends the stamp: its guard has
    locked the task run `FOR SHARE` and has not yet updated it. The task's own lease renewal,
    from a second connection, then queues for that row, and is seen waiting. The stamp must
    now pass the queued writer (the lock is upgraded in place), not deadlock with it.
    """
    environment, _, run_id = _run_with_checks(
        stack, {"assets/silver/x.sql": "SELECT id FROM bronze.nums\n"}, _PASSES
    )
    renewed: tuple[threading.Thread, list[object]] | None = None
    with (
        StatementPause(
            stack.db, "UPDATE task_runs SET written_table_id", "lakana-test-task-stamper"
        ) as pause,
        _task_worker(stack, "task-stamper"),
    ):
        try:
            blocker = pause.wait()
            task = task_row(stack.db, run_id)
            assert (task.status, task.written_table_id) == (TaskStatus.RUNNING, None)

            def renew() -> object:
                """Renew the task's lease as its keeper does, on a connection of its own.

                Returns
                -------
                object
                    What the renewal found.
                """
                with Session(stack.db) as session:
                    return renew_lease(session, task.id, "task-stamper", task.claim_count, 60)

            renewed = run_in_thread("lakana-test-other-session", renew)
            wait_for_lock_waiter(stack.db, blocker)  # the renewal waits for the write's lock
            assert renewed[1] == []
        finally:
            pause.release.set()  # before the worker is joined, whatever was asserted
        assert renewed is not None
        renewed[0].join(timeout=_JOIN_SECONDS)

        assert not renewed[0].is_alive(), "the renewal never got the row: a deadlock"
    assert renewed is not None and renewed[1] == [LeaseState.HELD]
    done = task_row(stack.db, run_id)
    assert (done.status, done.attempt) == (TaskStatus.SUCCEEDED, 1)
    (write,) = _task_queries(stack, done.id, writes=True)
    assert (write.status, write.rows_written) == (QueryRunStatus.SUCCEEDED, 3)
    assert (done.written_table_id, done.written_snapshot_after) == (
        write.target_table_id,
        write.snapshot_id,
    )  # the stamp and the evidence landed together
    assert done.written_snapshot_after is not None
    assert [r.outcome for r in stack.admin.checks.for_run(environment, run_id)] == ["passed"]


def test_the_written_stamp_commits_while_a_reclaim_waits_and_the_reclaim_then_misses(
    stack: QueryStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same hold, with the scheduler's reclaim queued (`FOR UPDATE`) instead of a renewal.

    The lease has run out. The tick read the task before the write committed (no stamp, no
    evidence) and computed an ordinary reclaim; its lock waits for the write. The stamp
    passes it, the write commits, and the reclaim's statement then finds a written task and
    misses: the attempt is not `lost`, and the worker goes on to check what it wrote.
    """
    environment, _, run_id = _run_with_checks(
        stack, {"assets/silver/x.sql": "SELECT id FROM bronze.nums\n"}, _PASSES
    )
    fence = _Hold(monkeypatch, after=False)
    with (
        StatementPause(
            stack.db, "UPDATE task_runs SET written_table_id", "lakana-test-task-stamper"
        ) as pause,
        _task_worker(stack, "task-stamper"),
    ):
        try:
            assert fence.reached.wait(_WAIT_SECONDS), "the write never reached the fence"
            task = task_row(stack.db, run_id)
            _expire_lease(stack, task.id)  # nothing holds the task run's row yet
            fence.release.set()
            blocker = pause.wait()

            def reclaim() -> object:
                """Run one scheduler tick on a connection of its own.

                Returns
                -------
                object
                    The transitions the tick applied.
                """
                with Session(stack.db) as session:
                    return scheduler_tick(session, db_now(session), secrets.SystemRandom().random)

            ticked = run_in_thread("lakana-test-other-session", reclaim)
            wait_for_lock_waiter(stack.db, blocker)  # the reclaim's lock waits for the write
            assert ticked[1] == []
        finally:
            fence.release.set()  # before the worker is joined, whatever was asserted
            pause.release.set()
        ticked[0].join(timeout=_JOIN_SECONDS)
        assert not ticked[0].is_alive(), "the reclaim never got the row: a deadlock"
    assert isinstance(ticked[1][0], int), ticked[1]
    done = task_row(stack.db, run_id)
    assert (done.status, done.attempt) == (TaskStatus.SUCCEEDED, 1)
    assert _attempt_outcomes(stack, done.id) == [AttemptOutcome.SUCCEEDED]  # never `lost`
    assert len(_task_queries(stack, done.id, writes=True)) == 1
    assert [r.outcome for r in stack.admin.checks.for_run(environment, run_id)] == ["passed"]


def test_a_reclaim_that_waits_for_a_committing_write_sees_the_stamp(stack: QueryStack) -> None:
    """A commit that lands while the reclaim waits, for a task with checks (the form of 's).

    The writer's transaction holds the task `FOR SHARE` and carries the evidence and the
    stamp. The tick that waited must not retry the task (it would build again) and must not
    end it `succeeded` (downstream would start on unchecked data): it misses, and the next
    tick puts the task back as written and unchecked.
    """
    task_id, write_id = _sql_task_with_a_write(stack, None)
    with Session(stack.db) as session:
        session.execute(
            text("UPDATE task_runs SET has_checks = true WHERE id = :id"), {"id": task_id}
        )
        session.commit()
        task = session.get(TaskRun, task_id)
        assert task is not None
        run_id, owner = task.run_id, task.lease_owner
    _expire_lease(stack, task_id)

    def tick() -> object:
        """Run one scheduler tick on a connection of its own.

        Returns
        -------
        object
            The transitions the tick applied.
        """
        with Session(stack.db) as session:
            return scheduler_tick(session, db_now(session), secrets.SystemRandom().random)

    table_id = uuid.uuid4()
    with Session(stack.db) as writer:
        writer.execute(text("SELECT id FROM task_runs WHERE id = :id FOR SHARE"), {"id": task_id})
        writer.execute(
            text("UPDATE query_runs SET rows_written = 5, snapshot_id = 11 WHERE id = :id"),
            {"id": write_id},
        )
        writer.execute(
            text(
                "UPDATE task_runs SET written_table_id = :t, written_snapshot_after = 11, "
                "written_publishes = false WHERE id = :id AND has_checks"
            ),
            {"t": table_id, "id": task_id},
        )
        blocker = writer.execute(text("SELECT pg_backend_pid()")).scalar_one()
        waited = run_in_thread("lakana-test-other-session", tick)
        wait_for_lock_waiter(stack.db, blocker, timeout=10)
        assert waited[1] == []
        writer.commit()
    waited[0].join(timeout=_JOIN_SECONDS)
    assert not waited[0].is_alive()
    assert isinstance(waited[1][0], int), waited[1]
    with Session(stack.db) as session:
        task = session.get(TaskRun, task_id)
        assert task is not None
        # The reclaim that waited missed: neither retried nor ended.
        assert (task.status, task.lease_owner, task.attempt) == (TaskStatus.RUNNING, owner, 1)
        succeeded = TaskTransition(
            run_id,
            task_id,
            TaskStatus.RUNNING,
            TaskStatus.SUCCEEDED,
            expected_owner=owner,
            expected_claim=1,
            lost=True,
        )
        assert apply_transitions(session, [succeeded], db_now(session)) is False
    assert _attempt_outcomes(stack, task_id) == [None]
    assert tick() != 0  # the next tick knows it as written and unchecked
    with Session(stack.db) as session:
        task = session.get(TaskRun, task_id)
        assert task is not None
        assert task.status in {TaskStatus.PENDING, TaskStatus.READY}
        assert (task.lease_owner, task.attempt, task.written_table_id) == (None, 1, table_id)
    assert _attempt_outcomes(stack, task_id) == [AttemptOutcome.LOST]
    assert stack.row(str(write_id)).status is QueryRunStatus.SUCCEEDED  # the write did land


def test_a_worker_lost_after_its_rollback_leaves_a_task_that_fails_from_its_results(
    stack: QueryStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real rollback, then the worker dies before it records the failure.

    The rollback's commit cleared the stamp, so only the write run's evidence still says that
    this task wrote. The tick must not take that for a success (the rule for a task without
    checks): the task goes back, and the next claim ends it failed from the recorded results,
    without building and without checking again. The table stays as the rollback left it.
    """
    files = {
        "assets/silver/x.sql": "SELECT id FROM bronze.nums\n",
        "assets/gold/y.sql": "SELECT count(*) AS n FROM silver.x\n",
    }
    # The table exists before the run, so that the rollback has a snapshot to go back to.
    environment, _, run_id = _run_with_checks(stack, files, _FAILS, ids=[7, 8])
    before = stack.admin.tables.get(environment, "silver.x").snapshot_id

    def die(*args: object, **kwargs: object) -> bool:
        """Stand in for `record_attempt`: the worker dies before it records.

        Parameters
        ----------
        *args
            Ignored.
        **kwargs
            Ignored.

        Raises
        ------
        _Died
            Always.
        """
        raise _Died

    monkeypatch.setattr(worker_tasks, "record_attempt", die)
    with _task_worker(stack, "task-doomed", dies_with=_Died) as thread:
        thread.join(timeout=_JOIN_SECONDS)
        assert not thread.is_alive(), "the worker did not die at its record"
    monkeypatch.undo()
    lost = task_row(stack.db, run_id)
    assert (lost.status, lost.attempt, lost.written_table_id) == (TaskStatus.RUNNING, 1, None)
    results = stack.admin.checks.for_run(environment, run_id)
    assert [(r.outcome, r.rollback_outcome) for r in results] == [("failed", "rolled_back")]
    assert stack.admin.tables.get(environment, "silver.x").snapshot_id == before
    (write,) = _task_queries(stack, lost.id, writes=True)
    assert (write.status, write.rows_written) == (QueryRunStatus.SUCCEEDED, 3)  # the evidence

    _expire_lease(stack, lost.id)
    for _ in range(6):
        _tick(stack)
        assert task_row(stack.db, run_id).status is not TaskStatus.SUCCEEDED
        with _task_worker(stack, "task-second"):
            pass
        finished = stack.admin.runs.get(environment, run_id)
        if finished.status in {"succeeded", "failed", "cancelled"}:
            break
    assert finished.status == "failed", finished
    done = task_row(stack.db, run_id)
    assert (done.status, done.attempt) == (TaskStatus.FAILED, 2)
    assert _attempt_outcomes(stack, done.id) == [AttemptOutcome.LOST, AttemptOutcome.FAILED]
    with Session(stack.db) as session:
        expected = check_failure_message(
            verdict(results_of_task(session, done.id)).blocking,
            table="silver.x",
            run_id=done.run_id,
            environment=environment,
            rollback=Rollback(RollbackOutcome.ROLLED_BACK),
        )
    assert done.error_message == expected
    assert len(_task_queries(stack, done.id, writes=True)) == 1  # built once
    assert len(_task_queries(stack, done.id, writes=False)) == 1  # checked once
    assert stack.admin.checks.for_run(environment, run_id) == results  # not recorded again
    rolled = stack.events(environment, AuditEventType.TABLE_ROLLED_BACK)
    assert [e.detail["outcome"] for e in rolled] == ["rolled_back"]
    downstream = task_row(stack.db, run_id, "gold.y")
    assert (downstream.status, downstream.attempt, downstream.ready_at) == (
        TaskStatus.UPSTREAM_FAILED,
        0,
        None,
    )
    assert stack.admin.tables.get(environment, "silver.x").snapshot_id == before
    with _worker(stack):
        rows = stack.admin.sql("SELECT id FROM silver.x ORDER BY id", environment, timeout=60)
    assert rows.column("id").to_pylist() == [7, 8]


def test_the_sweep_ends_a_lost_workers_count_with_its_own_text_and_the_next_claim_checks(
    stack: QueryStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worker dies inside the count of a check: the sweep ends the count, nobody builds twice.

    The count is the task's own query run but not its write, so the task's live lease does
    not shield it: the sweep ends it once the worker's heartbeat is a lease old, with the text
    of a count. The task waits for its lease, goes back as written and unchecked, and the next
    claim runs the checks only. The asset appends rows whose key is NULL, so a second build
    would show as six rows.
    """
    environment, _, run_id = _run_with_checks(
        stack,
        {
            "assets/silver/x.sql": _FRONT_MATTER
            + "SELECT CAST(NULL AS BIGINT) AS id FROM bronze.nums\n",
            "assets/gold/y.sql": "SELECT count(*) AS n FROM silver.x\n",
        },
        _PASSES,
        ids=[],
    )

    def die(*args: object, **kwargs: object) -> None:
        """Stand in for the count's `run_one`: the worker dies inside the count.

        Parameters
        ----------
        *args
            Ignored.
        **kwargs
            Ignored.

        Raises
        ------
        _Died
            Always.
        """
        raise _Died

    monkeypatch.setattr(worker_checks, "run_one", die)

    with _task_worker(stack, "task-doomed", dies_with=_Died) as thread:
        thread.join(timeout=_JOIN_SECONDS)
        assert not thread.is_alive(), "the worker did not die in its count"
    monkeypatch.undo()
    task = task_row(stack.db, run_id)
    assert (task.status, task.attempt) == (TaskStatus.RUNNING, 1)
    assert task.written_table_id is not None
    (count,) = _task_queries(stack, task.id, writes=False)
    assert (count.status, count.worker_id) == (QueryRunStatus.RUNNING, "task-doomed")

    with Session(stack.db) as session:
        session.execute(
            text(
                "UPDATE workers SET heartbeat_at = now() - make_interval(secs => :age) "
                "WHERE id = 'task-doomed'"
            ),
            {"age": _LEASE_SECONDS + 1},
        )
        session.commit()
    assert _sweep(stack) >= 1
    (count,) = _task_queries(stack, task.id, writes=False)
    assert (count.status, count.error_message) == (QueryRunStatus.FAILED, CHECK_LOST_MESSAGE)
    assert count.rows_written is None
    assert task_row(stack.db, run_id).status is TaskStatus.RUNNING  # the lease decides for the task

    _expire_lease(stack, task.id)
    for _ in range(6):
        _tick(stack)
        with _task_worker(stack, "task-second"):
            pass
        finished = stack.admin.runs.get(environment, run_id)
        if finished.status in {"succeeded", "failed", "cancelled"}:
            break
    assert finished.status == "succeeded", finished
    done = task_row(stack.db, run_id)
    assert (done.status, done.attempt) == (TaskStatus.SUCCEEDED, 2)
    assert _attempt_outcomes(stack, done.id) == [AttemptOutcome.LOST, AttemptOutcome.SUCCEEDED]
    assert len(_task_queries(stack, done.id, writes=True)) == 1  # one build
    counts = _task_queries(stack, done.id, writes=False)
    assert [c.status for c in counts] == [QueryRunStatus.FAILED, QueryRunStatus.SUCCEEDED]
    assert [r.outcome for r in stack.admin.checks.for_run(environment, run_id)] == ["passed"]
    with _worker(stack):
        rows = stack.admin.sql("SELECT count(*) AS n FROM silver.x", environment, timeout=60)
        built = stack.admin.sql("SELECT n FROM gold.y", environment, timeout=60)
    assert rows.column("n").to_pylist() == [3]  # once, not twice
    assert built.column("n").to_pylist() == [3]
