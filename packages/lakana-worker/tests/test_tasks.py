"""Executing task runs on SQLite, local storage, real DuckDB.

The lease keeper, the memory stop and the check phase have files of their own
(`test_task_lease.py`, `test_task_memory.py`, `test_task_checks*.py`).
"""

from __future__ import annotations

import dataclasses
import logging
import threading
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

import pyarrow as pa
import pytest
import sqlalchemy.event
from check_run_helpers import (
    _DUPLICATE_IDS,
    _UNPUBLISHED,
    _after,
    _check,
    _checked,
    _drain,
    _ids,
    _marker,
    _outside_table,
    _refused,
    _results,
    _run_until,
    _source_checked,
    _started_by_an_older_server,
    _writes,
)
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified
from task_run_helpers import (
    _CODE_PATH,
    _SLOW_SQL,
    World,
    _attempts,
    _cancel_and_wait,
    _claim,
    _engine,
    _entry,
    _exists,
    _fake_run_one,
    _log,
    _protect,
    _set_cancel_requested,
    _start,
    _task,
    _three_principals,
)
from worker_test_helpers import ActionAllowList, ReadsById

import lakana_worker.executor as executor_module
import lakana_worker.task_lease as task_lease_module
import lakana_worker.tasks as tasks_module
from lakana_core.auth import (
    Action,
    AllowAllAuthorization,
    PrincipalRef,
    Securable,
)
from lakana_core.catalog.identity import namespace_exists
from lakana_core.catalog.service import (
    CatalogService,
)
from lakana_core.errors import (
    AlreadyExistsError,
    ConflictError,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from lakana_core.identifiers import TableRef
from lakana_core.models import (
    AttemptOutcome,
    AuditEvent,
    AuditEventType,
    CheckOutcome,
    Environment,
    JobVersion,
    Namespace,
    Principal,
    QueryRun,
    QueryRunStatus,
    RollbackOutcome,
    Run,
    RunStatus,
    Table,
    TableType,
    TaskAttempt,
    TaskRun,
    TaskStatus,
    WriteMode,
)
from lakana_core.projects.manifest import MANIFEST_VERSION
from lakana_core.runs.checks import RUN_CANCELLED_MESSAGE, RUN_STOPPED_MESSAGE
from lakana_core.runs.failures import FailureCode
from lakana_core.runs.selection import Selection
from lakana_core.runs.service import RunService, task_log_path
from lakana_core.runs.store import scheduler_tick
from lakana_core.runs.tasks import (
    ClaimedTask,
    FailureKind,
    LeaseState,
    fail_run,
)
from lakana_core.storage.fsspec_storage import FsspecStorage
from lakana_core.storage.resolver import StorageResolver
from lakana_worker.executor import (
    OutOfMemoryQuery,
    QueryCancelled,
    QueryTimeout,
    TransientQueryError,
    WorkerStaleQuery,
)
from lakana_worker.task_integrity import verify_code
from lakana_worker.task_lease import LeaseKeeper, cancel_kind
from lakana_worker.task_logs import write_task_log
from lakana_worker.task_outcome import (
    INTERNAL_ERROR_MESSAGE,
    RUN_ERROR_MESSAGE,
    TaskOutcome,
    failure_code_of,
    stop_code,
)
from lakana_worker.tasks import run_claimed


def test_a_full_run_builds_every_table_in_order(world: World) -> None:
    """Exit criterion (unit tier): each task writes its asset as the run principal, with lineage."""
    run_id = _start(world)
    engine = _engine(world)
    run_claimed(engine, _claim(world))
    assert _task(world, "silver.clean").status is TaskStatus.SUCCEEDED
    with world.session() as session:
        scheduler_tick(session, datetime.now(UTC), lambda: 0.5)
    run_claimed(engine, _claim(world))
    gold = _task(world, "gold.daily")
    assert gold.status is TaskStatus.SUCCEEDED
    with world.session() as session:
        scheduler_tick(session, datetime.now(UTC), lambda: 0.5)
        run = session.get(Run, run_id)
        assert run is not None
        assert run.status is RunStatus.SUCCEEDED
        write = session.get(QueryRun, gold.query_run_id)
        assert write is not None
        assert (write.task_run_id, write.rows_written, write.principal_id) == (
            gold.id,
            1,
            world.admin.id,
        )
        assert write.lineage_json
    log = _log(world, run_id, "gold.daily")
    assert "attempt 1 on w1" in log and "succeeded" in log and "rows: 1" in log
    assert _attempts(world, gold.id)[0].outcome is AttemptOutcome.SUCCEEDED


def test_a_sql_error_fails_permanently_on_the_first_attempt(world: World) -> None:
    """Validation errors are never retried."""
    world.set_manifest(_entry("silver.clean", "SELECT error('boom')"))
    run_id = _start(world)
    run_claimed(_engine(world), _claim(world))
    task = _task(world, "silver.clean")
    assert (task.status, task.attempt) == (TaskStatus.FAILED, 1)
    assert "boom" in (task.error_message or "")
    assert "boom" in _log(world, run_id, "silver.clean")
    assert task.failure_code == "query_invalid"
    assert _attempts(world, task.id)[0].failure_code == "query_invalid"


def test_a_task_that_reads_a_table_that_is_not_published_fails_at_once(world: World) -> None:
    """A permanent failure: attempt 1 of 3, no retry, and the reason is the whole message."""
    _start(world)
    engine = _engine(world)
    run_claimed(engine, _claim(world))
    assert _task(world, "silver.clean").status is TaskStatus.SUCCEEDED
    marker = uuid.uuid4()
    with world.session() as session:
        session.execute(
            update(Table).where(Table.name == "clean").values(unpublished_run_id=marker)
        )
        session.commit()
        scheduler_tick(session, datetime.now(UTC), lambda: 0.5)

    run_claimed(engine, _claim(world))

    task = _task(world, "gold.daily")
    assert (task.status, task.attempt, task.max_attempts) == (TaskStatus.FAILED, 1, 3)
    assert task.failure_code == "not_found"
    assert task.error_message == (
        "`silver.clean` has not been published: its first build did not finish, "
        f"or failed a check (run {marker}). It is published by the next run that builds it "
        "and passes its checks."
    )


@pytest.mark.parametrize(
    ("failure", "status", "attempt", "outcome", "code"),
    [
        (
            ConnectionError("network down"),
            TaskStatus.PENDING,
            1,
            AttemptOutcome.FAILED,
            "internal",
        ),
        (
            TransientQueryError("Query failed: IO"),
            TaskStatus.PENDING,
            1,
            AttemptOutcome.FAILED,
            "internal",
        ),
        (
            QueryTimeout("Query exceeded the time limit"),
            TaskStatus.PENDING,
            1,
            AttemptOutcome.TIMED_OUT,
            "timeout",
        ),
        (
            ConflictError("changed; retry"),
            TaskStatus.PENDING,
            0,
            AttemptOutcome.FAILED,
            "conflict",
        ),
        (ValidationError("bad"), TaskStatus.FAILED, 1, AttemptOutcome.FAILED, "query_invalid"),
        (
            PermissionDenied("not allowed"),
            TaskStatus.FAILED,
            1,
            AttemptOutcome.FAILED,
            "permission_denied",
        ),
        (NotFoundError("no such table"), TaskStatus.FAILED, 1, AttemptOutcome.FAILED, "not_found"),
        (
            OutOfMemoryQuery("out of memory"),
            TaskStatus.FAILED,
            1,
            AttemptOutcome.FAILED,
            "memory",
        ),
        (
            WorkerStaleQuery("no heartbeat"),
            TaskStatus.PENDING,
            1,
            AttemptOutcome.FAILED,
            "worker_lost",
        ),
    ],
)
def test_failures_are_classified(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
    status: TaskStatus,
    attempt: int,
    outcome: AttemptOutcome,
    code: str,
) -> None:
    """Transient and timeout retry (counted), a conflict retries uncounted, validation fails.

    Each failure is recorded with the code of its cause, on the task and on the attempt.
    """
    _start(world)
    monkeypatch.setattr(tasks_module, "run_one", _fake_run_one(failure))
    claimed = _claim(world)
    run_claimed(_engine(world), claimed)
    task = _task(world, "silver.clean")
    assert (task.status, task.attempt) == (status, attempt)
    assert _attempts(world, claimed.task_run_id)[0].outcome is outcome
    assert task.failure_code == code
    assert _attempts(world, claimed.task_run_id)[0].failure_code == code


def test_a_real_timeout_is_retried(world: World) -> None:
    """A task whose SQL outlives its timeout is interrupted by DuckDB and retried, counted."""
    world.set_manifest(_entry("silver.slow", _SLOW_SQL, timeout_seconds=1))
    _start(world)
    claimed = _claim(world)
    run_claimed(_engine(world), claimed)
    task = _task(world, "silver.slow")
    assert (task.status, task.attempt) == (TaskStatus.PENDING, 1)
    assert _attempts(world, claimed.task_run_id)[0].outcome is AttemptOutcome.TIMED_OUT
    assert (task.failure_code, _attempts(world, claimed.task_run_id)[0].failure_code) == (
        "timeout",
        "timeout",
    )
    assert not _exists(world, "silver.slow")


def test_a_cancel_request_interrupts_the_running_task(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The keeper sees the flag at its next renewal and interrupts."""
    run_id = _start(world)

    def request_cancel_and_wait() -> None:
        with world.session() as session:
            session.execute(update(Run).where(Run.id == run_id).values(cancel_requested=True))
            session.commit()
        event = executor_module._CANCEL.get()
        assert event is not None
        assert event.wait(5)

    monkeypatch.setattr(
        tasks_module,
        "run_one",
        _fake_run_one(QueryCancelled("The query was cancelled."), request_cancel_and_wait),
    )
    run_claimed(_engine(world), _claim(world))
    task = _task(world, "silver.clean")
    assert task.status is TaskStatus.CANCELLED
    assert task.failure_code == "cancelled"


def test_an_in_flight_permission_denial_under_cancel_is_cancelled(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The keeper's reason wins over the type: a refusal caused by the cancel is not permanent."""
    run_id = _start(world)
    fake = _fake_run_one(PermissionDenied("revoked"), _cancel_and_wait(world, run_id))
    monkeypatch.setattr(tasks_module, "run_one", fake)
    claimed = _claim(world)
    run_claimed(_engine(world), claimed)
    assert _task(world, "silver.clean").status is TaskStatus.CANCELLED
    assert _attempts(world, claimed.task_run_id)[0].outcome is AttemptOutcome.CANCELLED


def test_a_write_that_landed_before_the_cancel_counts_as_succeeded(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The data is committed, so the task succeeded whatever the keeper saw."""
    run_id = _start(world)
    fake = _fake_run_one(None, _cancel_and_wait(world, run_id), succeed=True)
    monkeypatch.setattr(tasks_module, "run_one", fake)
    claimed = _claim(world)
    run_claimed(_engine(world), claimed)
    task = _task(world, "silver.clean")
    assert task.status is TaskStatus.SUCCEEDED
    attempt = _attempts(world, claimed.task_run_id)[0]
    assert (attempt.outcome, attempt.rows_written) == (AttemptOutcome.SUCCEEDED, 3)


def test_only_the_redacted_error_message_is_stored(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`run_one`'s returned exception can carry a storage location: it is never persisted."""
    run_id = _start(world)
    leak = TransientQueryError("failed at s3://bucket/key?X-Amz-Signature=SECRET")
    monkeypatch.setattr(
        tasks_module, "run_one", _fake_run_one(leak, error_message="Query failed: <redacted>")
    )
    claimed = _claim(world)
    run_claimed(_engine(world), claimed)
    task = _task(world, "silver.clean")
    assert task.error_message == "Query failed: <redacted>"
    attempt = _attempts(world, claimed.task_run_id)[0]
    assert attempt.error_message == "Query failed: <redacted>"
    log = _log(world, run_id, "silver.clean")
    assert "SECRET" not in log and "Query failed: <redacted>" in log


def test_a_failure_without_stored_text_gets_a_generic_message(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing falls back to `str(failure)`."""
    _start(world)
    leak = TransientQueryError("SECRET")
    monkeypatch.setattr(tasks_module, "run_one", _fake_run_one(leak, error_message=""))
    run_claimed(_engine(world), _claim(world))
    assert _task(world, "silver.clean").error_message == "the task failed"


def test_a_permission_refusal_in_the_write_is_prefixed(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A denial reported by `run_one` is permanent and says so."""
    _start(world)
    fake = _fake_run_one(PermissionDenied("x"), error_message="may not read silver.x")
    monkeypatch.setattr(tasks_module, "run_one", fake)
    run_claimed(_engine(world), _claim(world))
    task = _task(world, "silver.clean")
    assert (task.status, task.error_message) == (
        TaskStatus.FAILED,
        "permission denied: may not read silver.x",
    )


def test_a_sql_tasks_write_run_carries_the_workers_id(world: World) -> None:
    """The write run `_prepare` inserts has the worker's id, also when it fails before `run_one`."""
    _start(world)
    run_claimed(_engine(world), _claim(world))
    task = _task(world, "silver.clean")
    with world.session() as session:
        write = session.get(QueryRun, task.query_run_id)
        assert write is not None
        assert (write.status, write.worker_id) == (QueryRunStatus.SUCCEEDED, "w1")


def test_a_write_run_failed_before_run_one_carries_the_workers_id(world: World) -> None:
    """The id is written with the run, not by `run_one`'s start update."""
    world.set_manifest(_entry("mart.eu.daily", "SELECT 1::BIGINT AS id"))
    _start(world)
    allowed = [a for a in Action if a is not Action.CREATE_NAMESPACE]
    run_claimed(_engine(world, authz=lambda _s: ActionAllowList(*allowed)), _claim(world))
    task = _task(world, "mart.eu.daily")
    with world.session() as session:
        write = session.get(QueryRun, task.query_run_id)
        assert write is not None
        assert (write.status, write.worker_id) == (QueryRunStatus.FAILED, "w1")


def test_run_one_raising_is_a_generic_transient_failure(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing escapes `run_claimed`; the write run is closed, the keeper stopped."""
    _start(world)

    def explode(*args: object, **kwargs: object) -> object:
        raise RuntimeError("secret internals")

    monkeypatch.setattr(tasks_module, "run_one", explode)
    claimed = _claim(world)
    run_claimed(_engine(world), claimed)
    task = _task(world, "silver.clean")
    assert (task.status, task.error_message) == (TaskStatus.PENDING, INTERNAL_ERROR_MESSAGE)
    assert task.failure_code == "internal"
    with world.session() as session:
        write = session.get(QueryRun, task.query_run_id)
        assert write is not None
        assert write.status is QueryRunStatus.FAILED
    assert not any(t.name == "lakana-lease" for t in threading.enumerate())


def test_the_keeper_is_stopped_before_the_outcome_is_recorded(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No renewal can race `record_attempt`."""
    _start(world)
    alive: list[bool] = []
    real: Callable[..., bool] = tasks_module.record_attempt

    def spying(
        session: Session,
        task_run_id: uuid.UUID,
        worker: str,
        attempt: int,
        *,
        kind: FailureKind | None,
        rand: Callable[[], float],
        error_message: str | None = None,
        failure_code: FailureCode | None = None,
        rows_written: int | None = None,
        on_committed: str | None = None,
        in_transaction: Callable[[Session], None] | None = None,
        check_message_state: dict[str, object] | None = None,
    ) -> bool:
        alive.append(any(t.name == "lakana-lease" for t in threading.enumerate()))
        return real(
            session,
            task_run_id,
            worker,
            attempt,
            kind=kind,
            rand=rand,
            error_message=error_message,
            failure_code=failure_code,
            rows_written=rows_written,
            on_committed=on_committed,
            in_transaction=in_transaction,
            check_message_state=check_message_state,
        )

    monkeypatch.setattr(tasks_module, "record_attempt", spying)
    run_claimed(_engine(world), _claim(world))
    assert alive == [False]


def test_the_write_carries_the_claimed_attempts_query_run(world: World) -> None:
    """The task's `query_run_id` is the attempt's write run, set before execution."""
    _start(world)
    claimed = _claim(world)
    run_claimed(_engine(world), claimed)
    task = _task(world, "silver.clean")
    with world.session() as session:
        writes = list(session.scalars(select(QueryRun).where(QueryRun.task_run_id == task.id)))
    assert [w.id for w in writes] == [task.query_run_id]


def test_sigterm_releases_a_long_task_uncounted_and_writes_nothing(world: World) -> None:
    """A drained worker interrupts the real query after the grace and releases it."""
    world.set_manifest(_entry("silver.slow", _SLOW_SQL))
    _start(world)
    drain = threading.Event()
    drain.set()
    claimed = _claim(world)
    started = time.monotonic()
    run_claimed(_engine(world, drain=drain, grace=0.1), claimed)
    assert time.monotonic() - started < 10
    task = _task(world, "silver.slow")
    assert (task.status, task.attempt, task.lease_owner) == (TaskStatus.PENDING, 0, None)
    attempt = _attempts(world, claimed.task_run_id)[0]
    assert (attempt.outcome, attempt.finished_at is not None) == (AttemptOutcome.RELEASED, True)
    assert not _exists(world, "silver.slow")
    with world.session() as session:
        write = session.get(QueryRun, task.query_run_id)
        assert write is not None
        assert write.status is QueryRunStatus.FAILED


def test_a_worker_that_lost_its_lease_records_nothing(
    world: World, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The scheduler reclaimed the task meanwhile: the old worker's outcome is dropped."""
    _start(world)
    claimed = _claim(world)

    def steal() -> None:
        with world.session() as session:
            session.execute(
                update(TaskRun).where(TaskRun.id == claimed.task_run_id).values(lease_owner="thief")
            )
            session.commit()
        event = executor_module._CANCEL.get()
        assert event is not None
        assert event.wait(5)

    monkeypatch.setattr(tasks_module, "run_one", _fake_run_one(QueryCancelled("x"), steal))
    run_claimed(_engine(world), claimed)
    task = _task(world, "silver.clean")
    assert (task.status, task.lease_owner) == (TaskStatus.RUNNING, "thief")
    assert "lease_lost" in {getattr(r, "event", None) for r in caplog.records}
    assert _attempts(world, claimed.task_run_id)[0].finished_at is None
    with world.session() as session:
        write = session.get(QueryRun, task.query_run_id)
        assert write is not None
        assert write.status is QueryRunStatus.FAILED
    assert not _exists(world, "silver.clean")


def test_review_focus_5_missing_create_namespace_fails_permanently_and_says_so(
    world: World,
) -> None:
    """Without CREATE_NAMESPACE: one attempt, "permission denied" naming the action."""
    world.set_manifest(_entry("gold.eu.daily", "SELECT 1::BIGINT AS id"))
    run_id = _start(world)
    allowed = [a for a in Action if a is not Action.CREATE_NAMESPACE]
    run_claimed(_engine(world, authz=lambda _s: ActionAllowList(*allowed)), _claim(world))
    task = _task(world, "gold.eu.daily")
    assert (task.status, task.attempt) == (TaskStatus.FAILED, 1)
    assert task.error_message is not None
    assert task.error_message.startswith("permission denied")
    assert "CREATE_NAMESPACE" in task.error_message
    assert "permission denied" in _log(world, run_id, "gold.eu.daily")


def test_the_runner_creates_a_missing_nested_namespace(world: World) -> None:
    """`gold.eu` is created inside the run context before the write."""
    world.set_manifest(_entry("gold.eu.daily", "SELECT 1::BIGINT AS id"))
    _start(world)
    run_claimed(_engine(world), _claim(world))
    assert _task(world, "gold.eu.daily").status is TaskStatus.SUCCEEDED
    assert _exists(world, "gold.eu.daily")


def test_a_generator_only_asset_runs_and_writes(world: World) -> None:
    """An asset reading only `range(...)` has no inputs and still builds."""
    world.set_manifest(_entry("gold.series", "SELECT i::BIGINT AS id FROM range(5) AS r(i)"))
    _start(world)
    run_claimed(_engine(world), _claim(world))
    assert _task(world, "gold.series").status is TaskStatus.SUCCEEDED
    assert _exists(world, "gold.series")


def test_a_sql_asset_with_a_lambda_and_a_json_arrow_argument_runs(world: World) -> None:
    """An asset written for DuckDB 1.5 with `x -> …` and `upper(j -> 'a')` builds (#96, #284)."""
    world.set_manifest(
        _entry(
            "gold.series",
            "SELECT i::BIGINT AS id, list_transform([i], x -> x + 1)[1] AS next, "
            "upper(j -> 'a') AS a "
            "FROM (SELECT i, json_object('a', 'b') AS j FROM range(2) AS r(i))",
        )
    )
    _start(world)
    run_claimed(_engine(world), _claim(world))
    task = _task(world, "gold.series")
    assert task.status is TaskStatus.SUCCEEDED, task.error_message
    assert _exists(world, "gold.series")


def test_an_unbuilt_upstream_outside_the_selection_fails_the_task(world: World) -> None:
    """An unbuilt upstream outside the selection fails the task."""
    _start(world, Selection(patterns=("gold.daily",)))
    run_claimed(_engine(world), _claim(world))
    task = _task(world, "gold.daily")
    assert (task.status, task.error_message) == (
        TaskStatus.FAILED,
        "upstream table 'silver.clean' has not been built",
    )
    assert task.failure_code == "upstream_missing"


class _HideClean(AllowAllAuthorization):
    """Allow-all, except that nobody may read `silver.clean`; records every `READ_TABLE` ask."""

    def __init__(self) -> None:
        self.asked: list[str] = []

    def permits(self, principal: PrincipalRef, action: Action, securable: Securable) -> bool:
        """Refuse `READ_TABLE` on `clean`; permit anything else.

        Parameters
        ----------
        principal
            The actor.
        action
            The action.
        securable
            The object.

        Returns
        -------
        bool
            Whether the action is permitted.
        """
        if action is Action.READ_TABLE:
            self.asked.append(securable.table or "")
            return securable.table != "clean"
        return True

    def authorize(self, principal: PrincipalRef, action: Action, securable: Securable) -> None:
        """Raise as `permits` answers.

        Parameters
        ----------
        principal
            The actor.
        action
            The action.
        securable
            The object.

        Raises
        ------
        PermissionDenied
            When `permits` is false.
        """
        if not self.permits(principal, action, securable):
            raise PermissionDenied("denied")


class _HideNothing(AllowAllAuthorization):
    """Allow-all that records every `READ_TABLE` ask, for the readable side."""

    def __init__(self) -> None:
        self.asked: list[str] = []

    def permits(self, principal: PrincipalRef, action: Action, securable: Securable) -> bool:
        """Record a `READ_TABLE` ask and permit everything.

        Parameters
        ----------
        principal
            The actor.
        action
            The action.
        securable
            The object.

        Returns
        -------
        bool
            Always `True`.
        """
        if action is Action.READ_TABLE:
            self.asked.append(securable.table or "")
        return True


def test_an_unreadable_upstream_outside_the_selection_is_not_looked_up(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N1, SQL path: absent and existing give one outcome, and no existence lookup runs."""
    hide = _HideClean()
    looked_up: list[str] = []
    real = tasks_module.resolve_table_id

    def recording(
        session: Session, environment: str, namespace: str, name: str
    ) -> uuid.UUID | None:
        looked_up.append(f"{namespace}.{name}")
        return real(session, environment, namespace, name)

    monkeypatch.setattr(tasks_module, "resolve_table_id", recording)
    outcomes = []
    for built in (False, True):
        if built:
            with world.session() as session:
                CatalogService(session, StorageResolver(session), AllowAllAuthorization()).replace(
                    world.admin,
                    TableRef.from_dotted("dev", "silver.clean"),
                    pa.table({"id": pa.array([1], pa.int64())}),
                    expected_snapshot_id="none",
                )
        run_id = _start(world, Selection(patterns=("gold.daily",)))
        run_claimed(_engine(world, authz=lambda _s: hide), _claim(world))
        task = _task(world, "gold.daily")
        outcomes.append((task.status, task.error_message, _log(world, run_id, "gold.daily")))
        with world.session() as session:
            scheduler_tick(session, datetime.now(UTC), lambda: 0.5)
    assert "silver.clean" not in looked_up
    assert "has not been built" not in repr(outcomes)
    assert hide.asked[0] == "clean"
    assert outcomes[0][:2] == outcomes[1][:2]


def test_a_readable_unbuilt_upstream_outside_the_selection_is_still_named(world: World) -> None:
    """N1, SQL path: a principal that may read gets the plain message."""
    seen = _HideNothing()
    _start(world, Selection(patterns=("gold.daily",)))
    run_claimed(_engine(world, authz=lambda _s: seen), _claim(world))
    assert _task(world, "gold.daily").error_message == (
        "upstream table 'silver.clean' has not been built"
    )
    assert seen.asked[0] == "clean"


def _outcome_of_the_sql_task(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    readers: Callable[[dict[str, uuid.UUID]], set[uuid.UUID]],
    built: bool,
) -> tuple[tuple[object, object, str], list[str]]:
    """Run `gold.daily` once with `silver.clean` absent or built, and say what was looked up.

    Parameters
    ----------
    world
        The world.
    monkeypatch
        Records the existence lookups.
    readers
        Picks, from the three principals, the ids that may read a table.
    built
        Build `silver.clean` first.

    Returns
    -------
    tuple
        The task's status, error message and log, and the names the existence lookup was asked.
    """
    if built:
        with world.session() as session:
            CatalogService(session, StorageResolver(session), AllowAllAuthorization()).replace(
                world.admin,
                TableRef.from_dotted("dev", "silver.clean"),
                pa.table({"id": pa.array([1], pa.int64())}),
                expected_snapshot_id="none",
            )
    run_id = _start(world, Selection(patterns=("gold.daily",)))
    ids = _three_principals(world, run_id)
    looked_up: list[str] = []
    real = tasks_module.resolve_table_id

    def recording(
        session: Session, environment: str, namespace: str, name: str
    ) -> uuid.UUID | None:
        looked_up.append(f"{namespace}.{name}")
        return real(session, environment, namespace, name)

    monkeypatch.setattr(tasks_module, "resolve_table_id", recording)
    readable = readers(ids)
    run_claimed(_engine(world, authz=lambda _s: ReadsById(readable)), _claim(world))
    task = _task(world, "gold.daily")
    with world.session() as session:
        scheduler_tick(session, datetime.now(UTC), lambda: 0.5)  # let the run end
    return (task.status, task.error_message, _log(world, run_id, "gold.daily")), looked_up


def test_the_sql_path_asks_about_the_run_as_principal_when_it_may_not_read(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The trigger, the deployer and others may read; the run-as may not.

    Which principal the question is asked about decides here: a worker that asked about
    any other principal (a made-up id, the trigger, the deployer) would look the upstream up.
    """
    outcomes = []
    for built in (False, True):
        outcome, looked_up = _outcome_of_the_sql_task(
            world,
            monkeypatch,
            lambda ids: {ids["trigger"], ids["deployer"], world.admin.id},
            built,
        )
        assert "silver.clean" not in looked_up
        outcomes.append(outcome)
    assert outcomes[0][:2] == outcomes[1][:2]
    assert "has not been built" not in repr(outcomes)


def test_the_sql_path_names_an_unbuilt_upstream_to_a_run_as_that_may_read(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the run-as principal may read, and it gets today's message."""
    outcome, looked_up = _outcome_of_the_sql_task(
        world, monkeypatch, lambda ids: {ids["runner"]}, False
    )
    assert "silver.clean" in looked_up
    assert outcome[:2] == (TaskStatus.FAILED, "upstream table 'silver.clean' has not been built")


def test_a_tampered_tarball_fails_the_run_and_is_audited(world: World) -> None:
    """Integrity mismatch → task failed, run flagged, CODE_INTEGRITY_FAILED."""
    with world.session() as session:
        StorageResolver(session).for_environment("dev").storage.write(_CODE_PATH, b"evil")
    run_id = _start(world)
    run_claimed(_engine(world), _claim(world))
    task = _task(world, "silver.clean")
    assert task.status is TaskStatus.FAILED
    assert task.failure_code == "code_changed"
    assert "does not match its recorded code_sha256" in (task.error_message or "")
    with world.session() as session:
        run = session.get(Run, run_id)
        assert run is not None
        assert run.cancel_requested is True
        assert "code integrity check failed" in (run.error_message or "")
        event = session.scalars(
            select(AuditEvent).where(AuditEvent.event_type == AuditEventType.CODE_INTEGRITY_FAILED)
        ).one()
        assert event.detail["run_id"] == str(run_id)


def test_the_failed_run_and_the_attempt_are_written_in_one_transaction(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure between failing the run and recording the attempt leaves neither."""
    import lakana_worker.tasks as tasks_module

    with world.session() as session:
        StorageResolver(session).for_environment("dev").storage.write(_CODE_PATH, b"evil")
    run_id = _start(world)
    original = tasks_module.fail_run

    def fail_then_break(
        session: Session, run_id: uuid.UUID, message: str, *, commit: bool = True
    ) -> None:
        original(session, run_id, message, commit=commit)
        raise RuntimeError("the database went away")

    monkeypatch.setattr(tasks_module, "fail_run", fail_then_break)
    with pytest.raises(RuntimeError, match="went away"):
        run_claimed(_engine(world), _claim(world))
    with world.session() as session:
        run = session.get(Run, run_id)
        assert run is not None
        assert (run.cancel_requested, run.error_message) == (False, None)
        assert (
            session.scalars(
                select(AuditEvent).where(
                    AuditEvent.event_type == AuditEventType.CODE_INTEGRITY_FAILED
                )
            ).all()
            == []
        )
        attempt = session.scalars(select(TaskAttempt)).one()
        assert attempt.finished_at is None


@pytest.mark.parametrize(
    ("code_uri", "message"),
    [
        ("file:///elsewhere/a.tar.gz", "not under the environment's storage root"),
        ("{root}/_artifacts/code/analytics/missing.tar.gz", "is missing"),
        ("{root}/../escape.tar.gz", "not a valid storage path"),
    ],
)
def test_unverifiable_code_locations(world: World, code_uri: str, message: str) -> None:
    """Outside the root, missing, or escaping paths are integrity failures."""
    with world.session() as session:
        version = session.get(JobVersion, world.version_id)
        assert version is not None
        env = version.environment_id
        version.code_uri = code_uri.format(root=world.storage_root)
        session.commit()
        environment = session.get(Environment, env)
        assert environment is not None
        assert message in (verify_code(session, None, environment, version) or "")


def test_a_storage_error_while_verifying_is_retried(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read failure that is not a missing object is transient."""

    def broken(*args: object) -> str | None:
        raise OSError("connection reset")

    monkeypatch.setattr(tasks_module, "verify_code", broken)
    _start(world)
    run_claimed(_engine(world), _claim(world))
    task = _task(world, "silver.clean")
    assert (task.status, task.attempt) == (TaskStatus.PENDING, 1)
    assert "could not be read" in (task.error_message or "")
    assert task.failure_code == "internal"


def test_integrity_is_checked_once_per_run(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """The verification is cached per run in the worker process, not per project version."""
    calls: list[int] = []
    real = tasks_module.verify_code

    def counting(
        session: Session, key_provider: object, environment: Environment, version: JobVersion
    ) -> str | None:
        calls.append(1)
        return real(session, None, environment, version)

    monkeypatch.setattr(tasks_module, "verify_code", counting)
    run_id = _start(world)
    engine = _engine(world)
    run_claimed(engine, _claim(world))
    with world.session() as session:
        scheduler_tick(session, datetime.now(UTC), lambda: 0.5)
    run_claimed(engine, _claim(world))
    assert calls == [1]
    assert engine.verified == {run_id}


def test_an_inactive_run_principal_fails_permanently(world: World) -> None:
    """Execution re-checks the run principal."""
    _start(world)
    with world.session() as session:
        session.execute(
            update(Principal).where(Principal.id == world.admin.id).values(is_active=False)
        )
        session.commit()
    run_claimed(_engine(world), _claim(world))
    task = _task(world, "silver.clean")
    assert (task.status, task.error_message) == (
        TaskStatus.FAILED,
        "the run principal is deactivated or deleted",
    )
    assert task.failure_code == "permission_denied"


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ({"table": "silver.clean"}, "has no SQL for silver.clean"),
        (_entry("silver.clean", "SELECT 1", first_write_mode="explode"), "write mode 'explode'"),
    ],
)
def test_a_malformed_manifest_entry_fails_permanently(
    world: World, entry: dict, message: str
) -> None:
    """A manifest read back from the database never raises out of the runner."""
    world.set_manifest(entry)
    _start(world)
    run_claimed(_engine(world), _claim(world))
    task = _task(world, "silver.clean")
    assert task.status is TaskStatus.FAILED
    assert message in (task.error_message or "")
    assert task.failure_code == "code_changed"


def test_an_unexpected_preparation_error_is_transient_and_generic(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Untrusted input never surfaces a raw exception: a generic message, a retry."""

    def explode(*args: object) -> object:
        raise RuntimeError("secret internals")

    _start(world)
    monkeypatch.setattr(tasks_module.ManifestGraph, "from_manifest", explode)
    run_claimed(_engine(world), _claim(world))
    task = _task(world, "silver.clean")
    assert (task.status, task.error_message) == (TaskStatus.PENDING, INTERNAL_ERROR_MESSAGE)
    assert task.failure_code == "internal"


def test_an_unexpected_error_while_running_an_sql_attempt_is_a_recorded_transient_failure(
    world: World, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """It counts an attempt and is retried at once, instead of waiting for the lease to lapse."""

    def explode(*args: object) -> object:
        raise RuntimeError("secret internals")

    _start(world)
    monkeypatch.setattr(tasks_module, "_attempt", explode)
    run_claimed(_engine(world), _claim(world))
    task = _task(world, "silver.clean")
    assert (task.status, task.attempt, task.error_message) == (
        TaskStatus.PENDING,
        1,
        RUN_ERROR_MESSAGE,
    )
    assert "failed unexpectedly" in caplog.text
    assert "internal_error" in {getattr(r, "event", None) for r in caplog.records}
    assert "secret internals" not in (task.error_message or "")
    assert task.failure_code == "internal"


def test_a_vanished_run_is_ignored(world: World) -> None:
    """A claim whose run is gone (deleted environment) ends quietly."""
    ghost = ClaimedTask(uuid.uuid4(), uuid.uuid4(), "silver.clean", 1, 60, 1)
    run_claimed(_engine(world), ghost)
    with world.session() as session:
        assert session.scalars(select(QueryRun)).first() is None
        assert session.scalars(select(TaskAttempt)).first() is None


def test_a_failing_log_write_does_not_lose_the_outcome(
    world: World, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Logs are best effort; the attempt is still recorded.

    The real resolver and storage are used: only the log object's write
    fails, so the task's own data still lands and `_append_log` itself runs.
    """
    run_id = _start(world)
    log_path = task_log_path(run_id, "silver.clean")
    original = FsspecStorage.write
    refused: list[str] = []

    def full_for_logs(self: FsspecStorage, path: str, data: bytes) -> None:
        if path == log_path:
            refused.append(path)
            raise OSError("disk full")
        original(self, path, data)

    monkeypatch.setattr(FsspecStorage, "write", full_for_logs)
    with caplog.at_level(logging.WARNING, logger="lakana_worker.tasks"):
        run_claimed(_engine(world), _claim(world))
    assert refused == [log_path]
    assert _task(world, "silver.clean").status is TaskStatus.SUCCEEDED
    assert _exists(world, "silver.clean")
    with world.session() as session:
        storage = StorageResolver(session).for_environment("dev").storage
        assert not storage.exists(log_path)
    (record,) = [r for r in caplog.records if r.name == "lakana_worker.tasks"]
    assert record.getMessage().startswith("Could not write the log of silver.clean")


@pytest.mark.parametrize("failing", [False, True])
def test_the_log_is_stored_before_the_attempt_ends(
    world: World, monkeypatch: pytest.MonkeyPatch, failing: bool
) -> None:
    """A reader that sees the task finished sees its log: the block lands before the status."""
    run_id = _start(world)
    log_path = task_log_path(run_id, "silver.clean")
    original = FsspecStorage.write
    seen: list[TaskStatus] = []

    def watching(self: FsspecStorage, path: str, data: bytes) -> None:
        if path == log_path:
            seen.append(_task(world, "silver.clean").status)
        original(self, path, data)

    monkeypatch.setattr(FsspecStorage, "write", watching)
    if failing:
        monkeypatch.setattr(tasks_module, "run_one", _fake_run_one(ConnectionError("blip")))
    run_claimed(_engine(world), _claim(world))
    assert seen == [TaskStatus.RUNNING]
    assert _task(world, "silver.clean").status is not TaskStatus.RUNNING
    assert "attempt 1 on w1" in _log(world, run_id, "silver.clean")


def test_a_superseded_attempt_writes_no_log(world: World) -> None:
    """A claim the scheduler took over records nothing and so appends no block."""
    run_id = _start(world)
    stale = dataclasses.replace(_claim(world), claim=99)
    engine = _engine(world)
    with world.session() as session:
        tasks_module._finish(
            session, engine, stale, TaskOutcome(kind=None, message=None, environment="dev")
        )
    with world.session() as session:
        storage = StorageResolver(session).for_environment("dev").storage
        assert not storage.exists(task_log_path(run_id, "silver.clean"))


def test_logs_accumulate_across_attempts(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """Each attempt appends its block."""
    run_id = _start(world)
    monkeypatch.setattr(tasks_module, "run_one", _fake_run_one(ConnectionError("blip")))
    run_claimed(_engine(world), _claim(world))
    monkeypatch.undo()
    monkeypatch.setattr(task_lease_module, "_POLL_SECONDS", 0.02)
    with world.session() as session:
        session.execute(
            update(TaskRun).where(TaskRun.asset == "silver.clean").values(status=TaskStatus.READY)
        )
        session.commit()
    run_claimed(_engine(world), _claim(world))
    log = _log(world, run_id, "silver.clean")
    assert "attempt 1 on w1" in log and "attempt 2 on w1" in log and "blip" in log


def _namespace_exists(world: World, path: str) -> bool:
    """Report whether a namespace exists in `dev`.

    Parameters
    ----------
    world
        The world.
    path
        Dotted namespace path.

    Returns
    -------
    bool
        Whether it exists.
    """
    with world.session() as session:
        joined = TableRef.from_dotted("dev", f"{path}.t").namespace.joined()
        return namespace_exists(session, "dev", joined)


def test_a_protected_environment_lets_the_current_attempt_create_its_namespace(
    world: World,
) -> None:
    """The write run is inserted and bound first, so the run context covers the namespace."""
    world.set_manifest(_entry("gold.eu.daily", "SELECT 1::BIGINT AS id"))
    _start(world)
    _protect(world)
    run_claimed(_engine(world), _claim(world))
    assert _task(world, "gold.eu.daily").status is TaskStatus.SUCCEEDED
    assert _namespace_exists(world, "gold.eu")


@pytest.mark.parametrize("zombie", ["reclaimed", "stolen"])
def test_a_zombie_attempt_creates_nothing(world: World, zombie: str) -> None:
    """A task re-claimed (or leased elsewhere) holds no authority: no write run, no namespace."""
    world.set_manifest(_entry("mart.eu.daily", "SELECT 1::BIGINT AS id"))
    _start(world)
    _protect(world)
    claimed = _claim(world)
    # Re-claimed after an uncounted retry: same attempt number, a later claim.
    values = (
        {"claim_count": claimed.claim + 1} if zombie == "reclaimed" else {"lease_owner": "thief"}
    )
    with world.session() as session:
        session.execute(update(TaskRun).where(TaskRun.id == claimed.task_run_id).values(**values))
        session.commit()
    run_claimed(_engine(world), claimed)
    assert not _namespace_exists(world, "mart.eu")
    assert not _namespace_exists(world, "mart")
    with world.session() as session:
        assert session.scalars(select(QueryRun)).first() is None
    task = _task(world, "mart.eu.daily")
    assert task.status is TaskStatus.RUNNING
    assert _attempts(world, claimed.task_run_id)[0].finished_at is None


def test_a_namespace_failure_closes_the_write_run(world: World) -> None:
    """The write run inserted before the namespaces is failed, not left RUNNING."""
    world.set_manifest(_entry("mart.eu.daily", "SELECT 1::BIGINT AS id"))
    _start(world)
    allowed = [a for a in Action if a is not Action.CREATE_NAMESPACE]
    run_claimed(_engine(world, authz=lambda _s: ActionAllowList(*allowed)), _claim(world))
    task = _task(world, "mart.eu.daily")
    with world.session() as session:
        write = session.get(QueryRun, task.query_run_id)
        assert write is not None
        assert write.status is QueryRunStatus.FAILED
        assert write.finished_at is not None
        assert (write.error_message or "").startswith("permission denied")


def test_the_verified_set_is_bounded(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """A long-lived worker does not remember every run it ever verified."""
    monkeypatch.setattr(tasks_module, "_MAX_VERIFIED", 2)
    run_id = _start(world)
    engine = _engine(world)
    engine.verified.update({uuid.uuid4(), uuid.uuid4(), uuid.uuid4()})
    run_claimed(engine, _claim(world))
    assert engine.verified == {run_id}


def test_a_tampered_tarball_is_rechecked_by_the_next_task_of_the_run(world: World) -> None:
    """A failed verification is never cached as good."""
    with world.session() as session:
        StorageResolver(session).for_environment("dev").storage.write(_CODE_PATH, b"evil")
    _start(world)
    engine = _engine(world)
    run_claimed(engine, _claim(world))
    assert engine.verified == set()


@pytest.mark.parametrize(
    "manifest",
    [
        {"version": 1, "assets": [{"table": "silver.other", "sql": "SELECT 1"}], "edges": []},
        {"version": 1, "assets": "garbage", "edges": []},
        {"version": 1, "assets": ["not a mapping"], "edges": []},
    ],
)
def test_an_asset_missing_from_the_manifest_fails_permanently(
    world: World, manifest: dict[str, object]
) -> None:
    """A manifest that no longer lists the claimed asset never raises out of the runner."""
    _start(world)
    with world.session() as session:
        version = session.get(JobVersion, world.version_id)
        assert version is not None
        version.manifest_json = manifest
        session.commit()
    run_claimed(_engine(world), _claim(world))
    task = _task(world, "silver.clean")
    assert (task.status, task.error_message) == (
        TaskStatus.FAILED,
        "the project version has no SQL for silver.clean",
    )
    assert task.failure_code == "code_changed"


def test_a_built_upstream_outside_the_selection_lets_the_task_run(world: World) -> None:
    """The other side: an unselected upstream that exists is read, not refused."""
    _start(world)
    engine = _engine(world)
    run_claimed(engine, _claim(world))
    with world.session() as session:
        scheduler_tick(session, datetime.now(UTC), lambda: 0.5)
    run_claimed(engine, _claim(world))
    with world.session() as session:
        scheduler_tick(session, datetime.now(UTC), lambda: 0.5)
    _start(world, Selection(patterns=("gold.daily",)))
    run_claimed(engine, _claim(world))
    assert _task(world, "gold.daily").status is TaskStatus.SUCCEEDED


def test_a_lost_lease_seen_by_the_keeper_drops_the_outcome_without_a_thread(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_execute` returns nothing when the keeper saw the lease lost, with no thread.

    The thread-level test above waits for the real keeper thread to notice the theft; this one
    calls `renew_once` itself, so the branch is taken on every run.
    """
    _start(world)
    claimed = _claim(world)
    keepers: list[LeaseKeeper] = []
    monkeypatch.setattr(LeaseKeeper, "start", lambda self: keepers.append(self))
    real_execute = tasks_module._execute
    executed: list[TaskOutcome | None] = []

    def recording(*args: object) -> TaskOutcome | None:
        executed.append(real_execute(*args))  # ty: ignore[invalid-argument-type]
        return executed[-1]

    monkeypatch.setattr(tasks_module, "_execute", recording)

    def steal() -> None:
        with world.session() as session:
            session.execute(
                update(TaskRun).where(TaskRun.id == claimed.task_run_id).values(lease_owner="thief")
            )
            session.commit()
        keepers[0].renew_once()
        assert keepers[0].lost

    monkeypatch.setattr(tasks_module, "run_one", _fake_run_one(QueryCancelled("x"), steal))
    run_claimed(_engine(world, renew=0), claimed)
    assert executed == [None]
    task = _task(world, "silver.clean")
    assert (task.status, task.lease_owner) == (TaskStatus.RUNNING, "thief")
    assert _attempts(world, claimed.task_run_id)[0].finished_at is None


def test_cancel_kind_trusts_a_keeper_that_saw_a_reason_and_rereads_the_run_otherwise(
    world: World,
) -> None:
    """Both ways to learn of a stop, with a keeper that is never started.

    The keeper learns of a cancel request only at a renewal; whether it had by the time a
    refusal arrived depends on thread timing in the tests that run a whole task.
    """
    run_id = _start(world)
    keeper = LeaseKeeper(world.session, uuid.uuid4(), "w1", 1, lease_seconds=60, renew_seconds=60)
    with world.session() as session:
        assert cancel_kind(session, keeper, run_id) is None
        _set_cancel_requested(world, run_id)
        assert cancel_kind(session, keeper, run_id) == (
            FailureKind.CANCELLED,
            RUN_CANCELLED_MESSAGE,
        )
        assert cancel_kind(session, keeper, uuid.uuid4()) is None
        keeper._signal(FailureKind.RELEASED)
        assert cancel_kind(session, keeper, uuid.uuid4()) == (FailureKind.RELEASED, None)


def test_cancel_kind_words_a_run_failed_by_an_integrity_check_as_stopped_not_cancelled(
    world: World,
) -> None:
    """`fail_run` sets the cancel flag too, but nobody cancelled, so the text must not say so."""
    run_id = _start(world)
    with world.session() as session:
        fail_run(session, run_id, "code integrity check failed")
    keeper = LeaseKeeper(world.session, uuid.uuid4(), "w1", 1, lease_seconds=60, renew_seconds=60)
    with world.session() as session:
        assert cancel_kind(session, keeper, run_id) == (FailureKind.CANCELLED, RUN_STOPPED_MESSAGE)


def test_cancel_kind_is_no_cancel_for_a_claim_that_sets_the_table_back(world: World) -> None:
    """Setting the table back is the cancel's own work; a drain still releases it."""
    run_id = _start(world)
    _set_cancel_requested(world, run_id)
    keeper = LeaseKeeper(
        world.session,
        uuid.uuid4(),
        "w1",
        1,
        lease_seconds=60,
        renew_seconds=60,
        setting_back=True,
    )
    with world.session() as session:
        assert cancel_kind(session, keeper, run_id) is None
        keeper._signal(FailureKind.RELEASED)
        assert cancel_kind(session, keeper, run_id) == (FailureKind.RELEASED, None)


def test_a_cancel_the_keeper_has_not_seen_yet_is_cancelled_not_denied(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The protected-write guard refuses at once on `cancel_requested`; the keeper sees it later."""
    run_id = _start(world)
    fake = _fake_run_one(
        PermissionDenied("protected"),
        lambda: _set_cancel_requested(world, run_id),
        error_message="Protected environment: no writes",
    )
    monkeypatch.setattr(tasks_module, "run_one", fake)
    claimed = _claim(world)
    run_claimed(_engine(world, renew=3600), claimed)
    task = _task(world, "silver.clean")
    assert task.status is TaskStatus.CANCELLED
    assert _attempts(world, claimed.task_run_id)[0].outcome is AttemptOutcome.CANCELLED
    assert "Protected" not in (task.error_message or "")


def test_a_cancel_during_preparation_is_cancelled_not_denied(world: World) -> None:
    """The same race in the namespace step."""
    world.set_manifest(_entry("gold.eu.daily", "SELECT 1::BIGINT AS id"))
    run_id = _start(world)

    class CancelThenRefuse(ActionAllowList):
        """Refuses every action after the run was flagged for cancellation."""

        def authorize(self, principal: PrincipalRef, action: Action, securable: Securable) -> None:
            """Flag the run, then refuse.

            Parameters
            ----------
            principal
                The actor.
            action
                The action.
            securable
                The object.

            Raises
            ------
            PermissionDenied
                Always.
            """
            _set_cancel_requested(world, run_id)
            raise PermissionDenied("protected")

    claimed = _claim(world)
    run_claimed(_engine(world, authz=lambda _s: CancelThenRefuse(), renew=3600), claimed)
    assert _task(world, "gold.eu.daily").status is TaskStatus.CANCELLED
    assert _attempts(world, claimed.task_run_id)[0].outcome is AttemptOutcome.CANCELLED


def test_a_denial_without_a_cancel_stays_permanent(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cancel re-read does not turn every refusal into a cancellation."""
    _start(world)
    fake = _fake_run_one(PermissionDenied("no"), error_message="may not write")
    monkeypatch.setattr(tasks_module, "run_one", fake)
    run_claimed(_engine(world, renew=3600), _claim(world))
    assert _task(world, "silver.clean").status is TaskStatus.FAILED


@pytest.mark.parametrize("raises", [AlreadyExistsError, IntegrityError])
def test_a_namespace_created_concurrently_is_not_a_failure(
    world: World, monkeypatch: pytest.MonkeyPatch, raises: type[Exception]
) -> None:
    """A sibling task won the check-then-create race: the namespace is there, carry on."""
    world.set_manifest(_entry("gold.fresh", "SELECT 1::BIGINT AS id"))
    _start(world)
    assert _namespace_exists(world, "gold")
    seen: set[str] = set()
    real_exists = tasks_module.namespace_exists

    def stale(session: Session, environment: str, path: str) -> bool:
        if path not in seen:
            seen.add(path)
            return False
        return real_exists(session, environment, path)

    def create(self: CatalogService, *args: object, **kwargs: object) -> object:
        if raises is IntegrityError:
            raise IntegrityError("insert", None, Exception("duplicate"))
        raise AlreadyExistsError("exists")

    monkeypatch.setattr(tasks_module, "namespace_exists", stale)
    monkeypatch.setattr(CatalogService, "create_namespace", create)
    run_claimed(_engine(world), _claim(world))
    assert _task(world, "gold.fresh").status is TaskStatus.SUCCEEDED
    assert _exists(world, "gold.fresh")


def test_an_integrity_error_for_a_missing_namespace_is_transient(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An IntegrityError that is not a lost race is not swallowed."""
    _start(world)

    def create(self: CatalogService, *args: object, **kwargs: object) -> object:
        raise IntegrityError("insert", None, Exception("fk"))

    monkeypatch.setattr(CatalogService, "create_namespace", create)
    monkeypatch.setattr(tasks_module, "namespace_exists", lambda *args: False)
    run_claimed(_engine(world), _claim(world))
    task = _task(world, "silver.clean")
    assert (task.status, task.error_message) == (TaskStatus.PENDING, INTERNAL_ERROR_MESSAGE)
    assert task.failure_code == "internal"


def test_the_lease_is_renewed_while_the_tarball_is_verified(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The keeper runs during preparation, so a slow integrity check cannot outlive the lease."""
    _start(world)
    renewals: list[int] = []
    real_renew = task_lease_module.renew_lease
    real_verify = tasks_module.verify_code

    def counting(
        session: Session, task_run_id: uuid.UUID, worker: str, attempt: int, lease_seconds: int
    ) -> LeaseState:
        renewals.append(attempt)
        return real_renew(session, task_run_id, worker, attempt, lease_seconds)

    seen: list[int] = []

    def slow(
        session: Session, key_provider: object, environment: Environment, version: JobVersion
    ) -> str | None:
        time.sleep(0.4)
        seen.append(len(renewals))
        return real_verify(session, None, environment, version)

    monkeypatch.setattr(task_lease_module, "renew_lease", counting)
    monkeypatch.setattr(tasks_module, "verify_code", slow)
    run_claimed(_engine(world), _claim(world))
    assert seen and seen[0] > 0
    assert _task(world, "silver.clean").status is TaskStatus.SUCCEEDED


def test_a_task_finishing_within_the_drain_grace_is_recorded(world: World) -> None:
    """SIGTERM only releases a task that outlives the grace."""
    _start(world)
    drain = threading.Event()
    drain.set()
    claimed = _claim(world)
    run_claimed(_engine(world, drain=drain, grace=30), claimed)
    assert _task(world, "silver.clean").status is TaskStatus.SUCCEEDED
    assert _attempts(world, claimed.task_run_id)[0].outcome is AttemptOutcome.SUCCEEDED


def test_a_pre_m5c2a_manifest_still_runs_as_sql(world: World) -> None:
    """Version 1, entries without `kind`; the Python runtime is never asked."""

    class Never:
        """A Python runtime that must not be reached."""

        def attempt(self, *_args: object, **_kwargs: object) -> None:
            """Fail the test.

            Parameters
            ----------
            *_args
                Ignored.
            **_kwargs
                Ignored.

            Raises
            ------
            AssertionError
                Always.
            """
            raise AssertionError("a SQL task must not reach the Python runtime")

    _start(world)
    engine = _engine(world)
    engine.python = Never()
    with world.session() as session:
        version = session.get(JobVersion, world.version_id)
        assert version is not None
        assert version.manifest_json["version"] == 1
        assert all("kind" not in entry for entry in version.manifest_json["assets"])
    claimed = _claim(world)
    run_claimed(engine, claimed)
    assert _task(world, "silver.clean").status is TaskStatus.SUCCEEDED
    assert _exists(world, "silver.clean")
    log = _log(world, claimed.run_id, "silver.clean")
    assert "output:" not in log and "snapshot:" not in log


def test_an_artifact_outcome_logs_its_version_and_no_rows_or_snapshot(world: World) -> None:
    _start(world)
    claimed = _claim(world)
    engine = _engine(world)
    with world.session() as session:
        tasks_module._finish(
            session, engine, claimed, TaskOutcome(None, None, "dev", None, artifact_version=4)
        )
    log = _log(world, claimed.run_id, "silver.clean")
    assert "  write mode: -" in log
    assert "  rows: -" in log
    assert "  artifact version: 4" in log
    assert "  snapshot:" not in log
    assert "None" not in log


def test_a_sql_asset_whose_name_is_an_artifact_is_refused_with_the_next_step(world: World) -> None:
    """Point 8, the SQL path (`tasks._prepare` bypasses `submit_write`): the catalog refuses."""
    with world.session() as session:
        namespace_id = session.scalars(select(Namespace.id).where(Namespace.path == "silver")).one()
        session.add(
            Table(
                id=uuid.uuid4(),
                namespace_id=namespace_id,
                name="clean",
                table_type=TableType.ARTIFACT,
            )
        )
        session.commit()
    _start(world)
    claimed = _claim(world)
    run_claimed(_engine(world), claimed)
    task = _task(world, "silver.clean")
    assert task.status is TaskStatus.FAILED
    message = _attempts(world, claimed.task_run_id)[0].error_message
    assert message == (
        "silver.clean is an artifact; only managed tables can be written. Drop the artifact "
        "(`lakana artifact drop silver.clean`) or write to another name."
    )


@pytest.mark.parametrize("declared", [10, "3", 3.0, 0, -1, True])
def test_a_sql_task_of_a_manifest_this_worker_cannot_read_fails_permanently(
    world: World, declared: object
) -> None:
    _start(world)
    with world.session() as session:
        version = session.get(JobVersion, world.version_id)
        assert version is not None
        version.manifest_json = {**version.manifest_json, "version": declared}
        flag_modified(version, "manifest_json")  # 1 == True: without it nothing is written
        session.commit()
    claimed = _claim(world)
    run_claimed(_engine(world), claimed)
    assert _task(world, "silver.clean").status is TaskStatus.FAILED
    message = _attempts(world, claimed.task_run_id)[0].error_message
    assert message is not None and message.startswith("the project version's manifest has version ")
    assert _attempts(world, claimed.task_run_id)[0].failure_code == "no_runtime"


def _checks_for(declared: int) -> dict[str, object]:
    """Give a manifest of version 4 or later what a deploy always puts into one: a check.

    Parameters
    ----------
    declared
        The manifest's version.

    Returns
    -------
    dict
        A `checks` list of a check of a table the test does not run, from version 4 on; nothing
        else.
    """
    if declared < 4:
        return {}
    return {"checks": [_check("c", "SELECT id FROM gold.daily", table="gold.daily")]}


@pytest.mark.parametrize("declared", list(range(1, MANIFEST_VERSION + 1)))
def test_a_sql_task_of_a_manifest_of_a_version_up_to_the_current_one_still_runs(
    world: World, declared: int
) -> None:
    """I3: a manifest of the current version is the one every real deploy writes."""
    _start(world)
    with world.session() as session:
        version = session.get(JobVersion, world.version_id)
        assert version is not None
        version.manifest_json = {
            **version.manifest_json,
            "version": declared,
            **_checks_for(declared),
        }
        flag_modified(version, "manifest_json")  # 1 == True: without it nothing is written
        session.commit()
    run_claimed(_engine(world), _claim(world))
    assert _task(world, "silver.clean").status is TaskStatus.SUCCEEDED


@pytest.mark.parametrize(
    ("declared", "status"),
    [(8, TaskStatus.SUCCEEDED), (9, TaskStatus.SUCCEEDED), (10, TaskStatus.FAILED)],
)
def test_a_manifest_of_version_9_is_accepted_and_10_is_refused(
    world: World, declared: int, status: TaskStatus
) -> None:
    """Version 9 is the one a project that declares a model field gets; 10 is a future one's."""
    _start(world)
    with world.session() as session:
        version = session.get(JobVersion, world.version_id)
        assert version is not None
        version.manifest_json = {
            **version.manifest_json,
            "version": declared,
            **_checks_for(declared),
        }
        flag_modified(version, "manifest_json")
        session.commit()
    run_claimed(_engine(world), _claim(world))
    assert _task(world, "silver.clean").status is status


def test_a_manifest_without_a_version_still_runs(world: World) -> None:
    _start(world)
    with world.session() as session:
        version = session.get(JobVersion, world.version_id)
        assert version is not None
        version.manifest_json = {k: v for k, v in version.manifest_json.items() if k != "version"}
        session.commit()
    run_claimed(_engine(world), _claim(world))
    assert _task(world, "silver.clean").status is TaskStatus.SUCCEEDED


def test_a_failing_log_write_names_the_asset_escaped(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The asset's name comes from a project; a line break cannot forge a log record."""

    def refuse(*args: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("lakana_worker.task_logs._append_log", refuse)
    with caplog.at_level(logging.WARNING, logger="lakana_worker.tasks"):
        write_task_log(
            None,  # ty: ignore[invalid-argument-type]
            None,
            "dev",
            uuid.uuid4(),
            "silver.x\n2026-10-08 ERROR forged record\x1b[2J",
            "block",
        )
    [record] = caplog.records
    message = record.getMessage()
    assert "\n" not in message and "\x1b" not in message
    assert "silver.x\\n2026-10-08 ERROR forged record\\x1b[2J" in message


def _stamp_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the transaction that stamps a run's outcome raise, after the write has committed.

    Parameters
    ----------
    monkeypatch
        pytest's patcher.
    """
    import lakana_worker.pipeline as pipeline_module

    def raises(*args: object, **kwargs: object) -> bool:
        raise RuntimeError("the connection was lost")

    monkeypatch.setattr(pipeline_module, "_keep_a_prior_stamp", raises)


def test_a_stamp_that_fails_after_the_commit_ends_the_task_succeeded_and_not_rerun(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The write is in the table; recording its details failed. No second attempt."""
    run_id = _start(world)
    _stamp_fails(monkeypatch)
    claimed = _claim(world)
    run_claimed(_engine(world), claimed)
    monkeypatch.undo()
    task = _task(world, "silver.clean")
    assert (task.status, task.attempt) == (TaskStatus.SUCCEEDED, 1)
    assert task.query_run_id is not None
    (attempt,) = _attempts(world, claimed.task_run_id)
    assert (attempt.outcome, attempt.rows_written) == (AttemptOutcome.SUCCEEDED, 1)
    with world.session() as session:
        write = session.get(QueryRun, task.query_run_id)
        assert write is not None
        assert (write.status, write.error_message, write.rows_written) == (
            QueryRunStatus.SUCCEEDED,
            None,
            1,
        )
        scheduler_tick(session, datetime.now(UTC), lambda: 0.5)
    run_claimed(_engine(world), _claim(world))  # the task below runs
    assert _task(world, "gold.daily").status is TaskStatus.SUCCEEDED
    assert len(_attempts(world, claimed.task_run_id)) == 1
    assert run_id is not None


@pytest.mark.parametrize(
    "stop", [None, FailureKind.RELEASED, FailureKind.CANCELLED, FailureKind.PERMANENT]
)
def test_the_evidence_decides_the_attempt_even_if_the_run_could_not_be_closed(
    world: World, monkeypatch: pytest.MonkeyPatch, stop: FailureKind | None
) -> None:
    """The second layer: `_close_write` did nothing; a drain, cancel or failure still loses."""
    _start(world)
    _stamp_fails(monkeypatch)
    monkeypatch.setattr(tasks_module, "_close_write", lambda *args: None)
    if stop is not None:
        monkeypatch.setattr(tasks_module, "cancel_kind", lambda *args: (stop, "stopped"))
    claimed = _claim(world)
    run_claimed(_engine(world), claimed)
    task = _task(world, "silver.clean")
    assert (task.status, task.attempt) == (TaskStatus.SUCCEEDED, 1)
    (attempt,) = _attempts(world, claimed.task_run_id)
    assert attempt.outcome is AttemptOutcome.SUCCEEDED


def _append_ids(world: World, dotted: str, ids: list[int]) -> None:
    """Append rows to a table the project does not build.

    Parameters
    ----------
    world
        The world.
    dotted
        Its dotted name.
    ids
        The `id` values to add.
    """
    data = pa.table({"id": pa.array(ids, pa.int64())})
    with world.session() as session:
        CatalogService(session, StorageResolver(session), AllowAllAuthorization()).write_batches(
            world.admin,
            TableRef.from_dotted("dev", dotted),
            mode=WriteMode.APPEND,
            schema=data.schema,
            batches=data.to_batches(),
        )


def _incremental(
    world: World,
    checks: list[dict[str, object]] | None = None,
    sql: str = "SELECT id FROM bronze.events",
    **extra: object,
) -> None:
    """Deploy `silver.orders` as an incremental asset over `bronze.events`, as a deploy writes it.

    Parameters
    ----------
    world
        The world.
    checks
        Manifest check entries, if the project has any.
    sql
        The asset's SELECT.
    **extra
        Overrides of the manifest entry.
    """
    entry = {
        "references": ["bronze.events"],
        "materialize": "incremental",
        "write_mode": "append",
        "first_write_mode": "create",
        "incremental_key": "id",
        "retry_delay_seconds": 0,
        **extra,
    }
    world.set_manifest(_entry("silver.orders", sql, **entry), checks=checks)


def test_the_first_run_of_an_incremental_asset_creates_its_table_and_the_next_one_appends(
    world: World,
) -> None:
    """A new incremental asset has no table; run 1 creates it, run 2 adds the new rows."""
    _outside_table(world, "bronze.events", [1, 2, 3])
    _incremental(world)
    assert not _exists(world, "silver.orders")

    _start(world)
    _drain(world)

    first = _task(world, "silver.orders")
    assert first.status is TaskStatus.SUCCEEDED
    (write,) = _writes(world, first.id)
    assert (write.write_mode, write.watermark, write.rows_written) == (WriteMode.CREATE, None, 3)
    assert write.snapshot_id is not None
    assert _ids(world, "silver.orders") == [1, 2, 3]
    _append_ids(world, "bronze.events", [4, 5])

    _start(world)
    _drain(world)

    second = _task(world, "silver.orders")
    assert second.status is TaskStatus.SUCCEEDED
    (write,) = _writes(world, second.id)
    assert (write.write_mode, write.watermark, write.rows_written) == (
        WriteMode.APPEND,
        "long:3",
        2,
    )
    assert _ids(world, "silver.orders") == [1, 2, 3, 4, 5]


def test_a_first_incremental_run_without_rows_creates_the_empty_table_and_the_next_takes_all(
    world: World,
) -> None:
    """An empty create has no snapshot, so the next run has no watermark and appends all."""
    _outside_table(world, "bronze.events", [1, 2])
    _incremental(world, sql="SELECT id FROM bronze.events WHERE id > 100")

    _start(world)
    _drain(world)

    assert _task(world, "silver.orders").status is TaskStatus.SUCCEEDED
    assert _exists(world, "silver.orders")
    assert _ids(world, "silver.orders") == []
    _incremental(world)

    _start(world)
    _drain(world)

    (write,) = _writes(world, _task(world, "silver.orders").id)
    assert (write.write_mode, write.watermark, write.rows_written) == (WriteMode.APPEND, None, 2)
    assert _ids(world, "silver.orders") == [1, 2]


def test_a_job_version_deployed_before_the_fix_still_creates_the_table_on_the_first_run(
    world: World,
) -> None:
    """Such a manifest says `first_write_mode: append`, which could only fail."""
    _outside_table(world, "bronze.events", [1, 2])
    _incremental(world, first_write_mode="append")

    _start(world)
    _drain(world)

    task = _task(world, "silver.orders")
    assert task.status is TaskStatus.SUCCEEDED
    (write,) = _writes(world, task.id)
    assert write.write_mode is WriteMode.CREATE
    assert _ids(world, "silver.orders") == [1, 2]


def test_a_first_incremental_run_without_the_right_to_create_names_the_right_and_the_table(
    world: World,
) -> None:
    """The first run creates the table, which needs CREATE_TABLE and not only WRITE_TABLE."""
    _outside_table(world, "bronze.events", [1, 2])
    _incremental(world)
    _start(world)
    allowed = ActionAllowList(Action.READ_TABLE, Action.WRITE_TABLE)

    run_claimed(_engine(world, authz=lambda _s: allowed), _claim(world))

    task = _task(world, "silver.orders")
    assert task.status is TaskStatus.FAILED
    assert task.error_message == (
        "permission denied: may not CREATE_TABLE. The first run of an incremental asset creates "
        "silver.orders, which needs the CREATE_TABLE right on it. Grant it to the run's "
        "principal, or create the table first."
    )
    assert not _exists(world, "silver.orders")


def test_an_incremental_asset_that_failed_its_first_check_and_then_finds_no_rows_stays_unpublished(
    world: World,
) -> None:
    """A zero-row append commits nothing, the checks cannot read the table, nothing loops."""
    _outside_table(world, "bronze.events", [1, 1, 2])
    failing = _check("unique_id", _DUPLICATE_IDS, table="silver.orders")
    _incremental(world, [failing], retries=0)
    first = _start(world)
    _drain(world)
    assert _marker(world, "silver.orders") == first
    _incremental(world, [failing], sql="SELECT id FROM bronze.events WHERE id > 100", retries=0)

    run_id = _start(world)
    _drain(world)

    task = _task(world, "silver.orders")
    error = (
        "a table this check reads is not published: its first build did not finish, or failed a "
        "check, and no run has published it since."
    )
    (write,) = _writes(world, task.id)
    assert (write.write_mode, write.rows_written, write.snapshot_id) == (WriteMode.APPEND, 0, None)
    (result,) = _results(world, task.id)
    assert (result.outcome, result.error) == (CheckOutcome.ERRORED, error)
    assert (task.status, task.attempt) == (TaskStatus.FAILED, 1)
    assert task.error_message == (
        f"Check `silver.orders/unique_id` could not run (checks/silver/orders/unique_id.sql): "
        f"{error} Data that could not be checked is not published. {_UNPUBLISHED} "
        f"{_after(run_id)}"
    )
    assert _marker(world, "silver.orders") == run_id
    with world.session() as session:
        run = session.get(Run, run_id)
        assert run is not None and run.status is RunStatus.FAILED


def test_a_selected_first_incremental_run_creates_its_table(world: World) -> None:
    """`--select` changes which tasks run, not how the first one writes."""
    _outside_table(world, "bronze.events", [7])
    _incremental(world)

    _start(world, Selection(patterns=("silver.orders",)))
    _drain(world)

    assert _ids(world, "silver.orders") == [7]


def test_a_first_incremental_run_in_a_protected_environment_creates_its_table(
    world: World,
) -> None:
    """The create is a write of the task's own write run, which the guard allows."""
    _outside_table(world, "bronze.events", [1, 2])
    _incremental(world)
    _start(world)
    _protect(world)

    run_claimed(_engine(world), _claim(world))

    assert _task(world, "silver.orders").status is TaskStatus.SUCCEEDED
    assert _ids(world, "silver.orders") == [1, 2]


def test_a_first_incremental_run_that_fails_a_check_is_not_published_and_the_next_builds_it_again(
    world: World,
) -> None:
    """A first build of an incremental asset ends as a table's does, and can be redone."""
    _outside_table(world, "bronze.events", [1, 1, 2])
    failing = _check("unique_id", _DUPLICATE_IDS, table="silver.orders")
    _incremental(world, [failing])
    run_id = _start(world)
    _drain(world)

    task = _task(world, "silver.orders")
    assert task.status is TaskStatus.FAILED
    (result,) = _results(world, task.id)
    assert (result.outcome, result.rollback_outcome) == (
        CheckOutcome.FAILED,
        RollbackOutcome.UNPUBLISHED,
    )
    assert _marker(world, "silver.orders") == run_id
    passing = _check(
        "not_null", "SELECT id FROM silver.orders WHERE id IS NULL", table="silver.orders"
    )
    _incremental(world, [passing])

    _start(world)
    _drain(world)

    assert _task(world, "silver.orders").status is TaskStatus.SUCCEEDED
    assert _marker(world, "silver.orders") is None
    assert _ids(world, "silver.orders") == [1, 1, 2]


def test_a_run_an_older_server_started_for_a_table_with_checks_is_not_built(
    world: World,
) -> None:
    """Fail closed before anything is written, and say to upgrade the server."""
    _checked(world, _check("unique_id", _DUPLICATE_IDS))
    _started_by_an_older_server(world)

    _drain(world)

    task = _task(world, "silver.orders")
    assert (task.status, task.attempt) == (TaskStatus.FAILED, 1)
    assert task.error_message == _refused("`silver.orders`")
    assert task.failure_code == "no_runtime"
    assert _writes(world, task.id) == []
    assert _results(world, task.id) == []
    assert not _exists(world, "silver.orders")
    assert _task(world, "bronze.src").status is TaskStatus.SUCCEEDED


def test_a_run_an_older_server_started_does_not_build_a_reader_of_a_checked_source(
    world: World,
) -> None:
    """The task cannot see a missing `source_check` task, so it works it out."""
    _source_checked(world, "SELECT id FROM bronze.events WHERE id IS NULL")
    _started_by_an_older_server(world)

    _drain(world)

    task = _task(world, "silver.orders")
    assert task.status is TaskStatus.FAILED
    assert task.error_message == _refused("`bronze.events`, which `silver.orders` reads")
    assert _writes(world, task.id) == []
    assert not _exists(world, "silver.orders")


def _task_list_queries(world: World, claimed: ClaimedTask) -> int:
    """Run a claim and count the statements that list the task runs of its run for the refusal.

    Parameters
    ----------
    world
        The world.
    claimed
        The claim.

    Returns
    -------
    int
        How many such statements the claim issued.
    """
    seen: list[str] = []

    def record(conn: object, cursor: object, statement: str, *rest: object) -> None:
        if statement.lstrip().startswith("SELECT task_runs.asset, task_runs.kind"):
            seen.append(statement)

    sqlalchemy.event.listen(world.engine, "before_cursor_execute", record)
    try:
        run_claimed(_engine(world), claimed)
    finally:
        sqlalchemy.event.remove(world.engine, "before_cursor_execute", record)
    return len(seen)


def test_a_task_of_a_project_without_checks_does_not_list_the_tasks_of_its_run(
    world: World,
) -> None:
    """The refusal costs one read of the manifest, which it shares with the build."""
    _start(world)
    assert _task_list_queries(world, _claim(world)) == 0
    assert _task(world, "silver.clean").status is TaskStatus.SUCCEEDED


def test_a_task_the_refusal_has_to_look_at_lists_the_tasks_of_its_run_once(world: World) -> None:
    """The counter above sees the statement when there is one to see."""
    _checked(world, _check("unique_id", _DUPLICATE_IDS))
    _started_by_an_older_server(world)
    claimed = _run_until(world, "silver.orders")
    assert _task_list_queries(world, claimed) == 1


def test_a_selected_run_over_a_checked_source_is_not_refused(world: World) -> None:
    """The run's own source_check task exists, so the reader is not refused."""
    _source_checked(world, "SELECT id FROM bronze.events WHERE id IS NULL")
    _start(world, Selection(patterns=("silver.orders",)))

    _drain(world)

    assert _task(world, "silver.orders").status is TaskStatus.SUCCEEDED


def test_a_resumed_run_over_a_checked_source_is_not_refused(world: World) -> None:
    """A resume makes its own source_check task, so the reader is not refused."""
    _source_checked(world, "SELECT id FROM bronze.events")
    run_id = _start(world)
    _drain(world)
    assert _task(world, "bronze.events").status is TaskStatus.FAILED
    world.set_manifest(
        _entry("silver.orders", "SELECT id FROM bronze.events", references=["bronze.events"]),
        checks=[
            _check(
                "unique_id", "SELECT id FROM bronze.events WHERE id IS NULL", table="bronze.events"
            )
        ],
    )
    with world.session() as session:
        RunService(session, AllowAllAuthorization()).resume(world.admin, "dev", run_id)

    _drain(world)

    assert _task(world, "bronze.events").status is TaskStatus.SUCCEEDED
    assert _task(world, "silver.orders").status is TaskStatus.SUCCEEDED


def test_a_run_an_older_server_started_builds_a_table_without_checks_as_before(
    world: World,
) -> None:
    """Only a table the manifest lists checks for is refused."""
    _checked(world, _check("unique_id", _DUPLICATE_IDS, table="gold.report"))
    _started_by_an_older_server(world)

    _drain(world)

    assert _task(world, "silver.orders").status is TaskStatus.SUCCEEDED
    assert _exists(world, "silver.orders")
    assert _task(world, "gold.report").status is TaskStatus.FAILED


@pytest.mark.parametrize(
    ("cause", "expected"),
    [
        ("not_found", "not_found"),
        ("permission_denied", "permission_denied"),
        ("result_too_large", "query_invalid"),
        ("out_of_memory", "memory"),
        ("invalid_query", "query_invalid"),
        ("timeout", "timeout"),
        ("cancelled", "cancelled"),
        ("internal", "internal"),
        ("worker_lost", "worker_lost"),
        (None, "internal"),
        ("a_cause_from_a_newer_release", "internal"),
    ],
)
def test_a_failed_write_run_without_an_exception_gets_the_code_of_its_stored_cause(
    cause: str | None, expected: str
) -> None:
    """When `run_one` returned no exception, the cause stored on the query run decides."""
    assert failure_code_of(None, cause).value == expected


def test_a_conflict_is_a_conflict_whatever_the_stored_cause() -> None:
    """A query has no code for it; a task has."""
    assert failure_code_of(ConflictError("changed"), "internal").value == "conflict"


def test_only_a_cancel_has_a_code_among_the_stops() -> None:
    """A release is no failure."""
    assert stop_code(FailureKind.CANCELLED) is not None
    assert stop_code(FailureKind.RELEASED) is None


def _merging(world: World, **extra: object) -> None:
    """Deploy `silver.orders` as a `write: merge` asset on `id`, as a deploy writes it.

    Parameters
    ----------
    world
        The world.
    **extra
        Overrides of the manifest entry.
    """
    entry = {
        "references": ["bronze.events"],
        "materialize": "table",
        "write_mode": "merge",
        "first_write_mode": "create",
        "incremental_key": None,
        "write": "merge",
        "key": ["id"],
        "watermark": None,
        "retry_delay_seconds": 0,
        **extra,
    }
    world.set_manifest(_entry("silver.orders", "SELECT id FROM bronze.events", **entry))


def test_a_merge_asset_creates_its_table_and_later_runs_merge_on_its_key(world: World) -> None:
    """Run 2 reads every source row again; the rows of run 1 are matched, not added twice."""
    _outside_table(world, "bronze.events", [1, 2, 3])
    _merging(world)

    _start(world)
    _drain(world)

    first = _task(world, "silver.orders")
    assert first.status is TaskStatus.SUCCEEDED, first.error_message
    (write,) = _writes(world, first.id)
    assert (write.write_mode, write.merge_key, write.rows_written) == (
        WriteMode.CREATE,
        ["id"],
        3,
    )
    _append_ids(world, "bronze.events", [4, 5])

    _start(world)
    _drain(world)

    second = _task(world, "silver.orders")
    assert second.status is TaskStatus.SUCCEEDED, second.error_message
    (write,) = _writes(world, second.id)
    assert (write.write_mode, write.merge_key, write.rows_written) == (
        WriteMode.MERGE,
        ["id"],
        2,
    )
    assert write.snapshot_id is not None
    assert _ids(world, "silver.orders") == [1, 2, 3, 4, 5]


def test_a_merge_that_changes_nothing_says_the_run_left_the_table_as_it_was(
    world: World,
) -> None:
    """A failing blocking check after a merge that committed nothing: nothing to set back."""
    _outside_table(world, "bronze.events", [1, 2, 3])
    entry = {
        "references": ["bronze.events"],
        "materialize": "table",
        "write_mode": "merge",
        "first_write_mode": "create",
        "incremental_key": None,
        "write": "merge",
        "key": ["id"],
        "watermark": None,
        "retry_delay_seconds": 0,
        "retries": 0,
    }
    world.set_manifest(_entry("silver.orders", "SELECT id FROM bronze.events", **entry))
    _start(world)
    _drain(world)
    assert _task(world, "silver.orders").status is TaskStatus.SUCCEEDED
    world.set_manifest(
        _entry("silver.orders", "SELECT id FROM bronze.events", **entry),
        checks=[_check("no_threes", "SELECT id FROM silver.orders WHERE id = 3")],
    )

    run_id = _start(world)
    _drain(world)

    task = _task(world, "silver.orders")
    assert task.status is TaskStatus.FAILED
    assert task.error_message is not None
    assert task.error_message.startswith(
        "Check `silver.orders/no_threes` failed: 1 row (checks/silver/orders/no_threes.sql). "
        "The table was not changed by this run, and nothing downstream was built."
    )
    assert "rollback failed" not in task.error_message.lower()
    (result,) = _results(world, task.id)
    assert result.rollback_outcome is RollbackOutcome.NOTHING_WRITTEN
    assert "rollback: nothing_written" in _log(world, run_id, "silver.orders")
    assert _ids(world, "silver.orders") == [1, 2, 3]


def test_the_first_run_of_a_merge_asset_refuses_a_repeated_key_and_creates_nothing(
    world: World,
) -> None:
    """The create is checked as every later merge is: a repeated key would collapse later."""
    _outside_table(world, "bronze.events", [1, 1, 2])
    _merging(world, retries=0)

    _start(world)
    _drain(world)

    task = _task(world, "silver.orders")
    assert task.status is TaskStatus.FAILED
    assert task.error_message is not None
    assert "key `id` repeats in 2 rows of the asset's result; keep one row per key in its " in (
        task.error_message
    )
    assert "QUALIFY ROW_NUMBER() OVER (PARTITION BY id ORDER BY" in task.error_message
    assert "unique(subset" not in task.error_message
    assert not _exists(world, "silver.orders")


def test_a_later_run_of_a_merge_asset_refuses_a_repeated_key_with_advice_for_its_select(
    world: World,
) -> None:
    _outside_table(world, "bronze.events", [1, 2])
    _merging(world, retries=0)
    _start(world)
    _drain(world)
    assert _task(world, "silver.orders").status is TaskStatus.SUCCEEDED
    _append_ids(world, "bronze.events", [2])

    _start(world)
    _drain(world)

    task = _task(world, "silver.orders")
    assert task.status is TaskStatus.FAILED
    assert task.error_message is not None
    assert "key `id` repeats in 2 rows of the asset's result" in task.error_message
    assert "QUALIFY ROW_NUMBER()" in task.error_message
    assert _ids(world, "silver.orders") == [1, 2]


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ({"write": "upsert"}, "the project version's write mode 'upsert' for silver.orders"),
        ({"key": []}, "the project version merges silver.orders without a key"),
        ({"key": "id"}, "the project version merges silver.orders without a key"),
        (
            {"first_write_mode": "replace"},
            "the project version's write mode 'replace' for silver.orders is not valid",
        ),
    ],
    ids=["unknown-write", "no-key", "key-not-a-list", "merge-written-as-replace"],
)
def test_a_merge_entry_that_cannot_be_read_fails_without_writing(
    world: World, extra: dict[str, object], message: str
) -> None:
    _outside_table(world, "bronze.events", [1, 2])
    _merging(world, retries=0, **extra)

    _start(world)
    _drain(world)

    task = _task(world, "silver.orders")
    assert task.status is TaskStatus.FAILED
    assert task.error_message is not None and task.error_message.startswith(message)
    assert not _exists(world, "silver.orders")


def _orders_with_region(world: World) -> None:
    """Create `silver.orders` with the columns `id` and `region`, outside the project.

    Parameters
    ----------
    world
        The world.
    """
    data = pa.table({"id": pa.array([1], pa.int64()), "region": pa.array(["eu"], pa.string())})
    with world.session() as session:
        catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
        ref = TableRef.from_dotted("dev", "silver.orders")
        if not namespace_exists(session, "dev", ref.namespace.joined()):
            catalog.create_namespace(world.admin, "dev", ref.namespace)
        catalog.write_batches(
            world.admin, ref, mode=WriteMode.CREATE, schema=data.schema, batches=data.to_batches()
        )


_MISMATCH = "The result does not match silver.orders's schema (dropped region (string))."


def test_a_sql_table_asset_whose_result_lost_a_column_is_told_a_step_it_can_take(
    world: World,
) -> None:
    """A table asset cannot set `evolve_schema`, so it is not told to (#377)."""
    _outside_table(world, "bronze.events", [1, 2])
    _orders_with_region(world)
    world.set_manifest(
        _entry(
            "silver.orders",
            "SELECT id FROM bronze.events",
            references=["bronze.events"],
            materialize="table",
            retries=0,
        )
    )

    _start(world)
    _drain(world)

    task = _task(world, "silver.orders")
    assert task.status is TaskStatus.FAILED
    assert task.error_message == (
        f"{_MISMATCH} A SQL asset that replaces its table cannot drop, rename or retype the "
        "table's columns: make its SELECT return the columns and types silver.orders has (list "
        "them instead of `*`), then run it again."
    )


def test_a_sql_append_asset_whose_result_lost_a_column_is_told_its_front_matter_key(
    world: World,
) -> None:
    """An append may set `evolve_schema: true`, in its front matter, not as a CLI flag (#377)."""
    _outside_table(world, "bronze.events", [1, 2])
    _orders_with_region(world)
    _incremental(world, retries=0)

    _start(world)
    _drain(world)

    task = _task(world, "silver.orders")
    assert task.status is TaskStatus.FAILED
    assert task.error_message == (
        f"{_MISMATCH} To let silver.orders take the new schema, add `evolve_schema: true` to "
        "the asset's front matter and deploy again (a column the result no longer has is then "
        "removed from the table); otherwise make its SELECT return the columns and types "
        "silver.orders has."
    )
