"""`run_one` reports the exception that failed a run."""

from __future__ import annotations

import logging
import os
import signal
import sqlite3
import sys
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import cast

import pyarrow as pa
import pytest
from sqlalchemy import Engine, event, select, update
from sqlalchemy.orm import Session
from sqlalchemy.pool import QueuePool
from sqlglot.errors import ParseError, TokenError

from lakana_core.auth import AllowAllAuthorization, PrincipalRef
from lakana_core.catalog.query_service import QUERY_CANCELLED_MESSAGE, QueryService
from lakana_core.catalog.service import CatalogService
from lakana_core.config import DatabaseConfig, QueryLimitsConfig, StorageConfig
from lakana_core.db import create_engine, db_now
from lakana_core.errors import (
    ConflictError,
    LakanaError,
    NotFoundError,
    PermissionDenied,
    UnsupportedTableError,
    ValidationError,
)
from lakana_core.models import (
    AuditEvent,
    AuditEventType,
    Environment,
    JobVersion,
    Principal,
    PrincipalType,
    QueryRun,
    QueryRunStatus,
    Repository,
    RepositoryKind,
    Run,
    RunKind,
    RunStatus,
    TaskRun,
    TaskStatus,
    WriteMode,
)
from lakana_core.runs import pools
from lakana_core.runs.lockdown import WriteFenceRefused
from lakana_core.runs.store import (
    CHECK_LOST_MESSAGE,
    end_lost_runs,
)
from lakana_core.storage.resolver import StorageResolver
from lakana_worker import executor as executor_module
from lakana_worker import pipeline
from lakana_worker.executor import (
    CANCELLED_MESSAGE,
    WORKER_STALE_MESSAGE,
    DuckDBInternalError,
    OutOfMemoryQuery,
    QueryCancelled,
    QueryCancelRequested,
    QueryTimeout,
    ResultTooLarge,
    TransientQueryError,
    WorkerStaleQuery,
)
from lakana_worker.pipeline import (
    _already_audited,
    _keep_a_prior_stamp,
    failure_cause,
    run_one,
)
from lakana_worker.query_process import REGISTRY


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
def principal(session: Session, tmp_path: Path) -> PrincipalRef:
    """Persist an admin and an unprotected `dev`.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        Storage root.

    Returns
    -------
    PrincipalRef
        The admin.
    """
    ref = PrincipalRef(id=uuid.uuid4(), name="admin", type=PrincipalType.USER)
    session.add(Principal(id=ref.id, type=ref.type, name=ref.name, is_active=True))
    session.commit()
    CatalogService(session, StorageResolver(session), AllowAllAuthorization()).create_environment(
        ref,
        name="dev",
        storage=StorageConfig(scheme="file", root=f"file://{tmp_path / 'storage'}", options={}),
    )
    return ref


def _run(session: Session, principal_id: uuid.UUID | None, sql: str) -> uuid.UUID:
    """Queue a read query.

    Parameters
    ----------
    session
        Database session.
    principal_id
        The submitter.
    sql
        The query.

    Returns
    -------
    uuid.UUID
        The run's id.
    """
    env = session.scalars(select(Environment)).one()
    run = QueryRun(
        id=uuid.uuid4(),
        environment_id=env.id,
        principal_id=principal_id,
        sql_text=sql,
        status=QueryRunStatus.QUEUED,
    )
    session.add(run)
    session.commit()
    return run.id


def test_success_returns_none(session: Session, principal: PrincipalRef) -> None:
    """A succeeded run reports nothing."""
    run_id = _run(session, principal.id, "SELECT 1 AS x")
    assert run_one(session, run_id, AllowAllAuthorization()) is None


def test_a_started_run_carries_the_workers_id(session: Session, principal: PrincipalRef) -> None:
    """The start update writes the id with the status."""
    run_id = _run(session, principal.id, "SELECT 1 AS x")
    assert run_one(session, run_id, AllowAllAuthorization(), worker_id="w1") is None
    session.expire_all()
    stored = session.get(QueryRun, run_id)
    assert stored is not None
    assert (stored.status, stored.worker_id) == (QueryRunStatus.SUCCEEDED, "w1")


def test_a_run_started_without_a_worker_id_has_none(
    session: Session, principal: PrincipalRef
) -> None:
    run_id = _run(session, principal.id, "SELECT 1 AS x")
    assert run_one(session, run_id, AllowAllAuthorization()) is None
    session.expire_all()
    stored = session.get(QueryRun, run_id)
    assert stored is not None
    assert stored.worker_id is None


def test_a_failed_run_returns_its_exception(session: Session, principal: PrincipalRef) -> None:
    """The caller gets the exception the run's `error_message` was built from."""
    run_id = _run(session, principal.id, "SELECT * FROM bronze.ghost")
    failure = run_one(session, run_id, AllowAllAuthorization())
    assert isinstance(failure, LakanaError)


def test_an_unexpected_error_is_returned_too(
    session: Session, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-Lakana exception is returned (the task runner treats it as transient)."""

    def explode(*args: object, **kwargs: object) -> None:
        raise ConnectionError("network down")

    monkeypatch.setattr("lakana_worker.pipeline.execute_query", explode)
    run_id = _run(session, principal.id, "SELECT 1 AS x")
    failure = run_one(session, run_id, AllowAllAuthorization())
    assert isinstance(failure, ConnectionError)
    assert not isinstance(failure, LakanaError)


def test_a_memory_error_fails_the_run_as_out_of_memory_without_crashing(
    session: Session, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worker's hard limit makes an allocation raise `MemoryError`; the run fails.

    The message names both settings and the next step; the cause is `out_of_memory`; the
    returned failure is permanent (another attempt would hit the same limit).
    """

    def explode(*args: object, **kwargs: object) -> None:
        raise MemoryError

    monkeypatch.setattr("lakana_worker.pipeline.execute_query", explode)
    run_id = _run(session, principal.id, "SELECT 1 AS x")
    failure = run_one(session, run_id, AllowAllAuthorization())
    assert isinstance(failure, OutOfMemoryQuery)
    session.expire_all()
    stored = session.get(QueryRun, run_id)
    assert stored is not None
    assert stored.status is QueryRunStatus.FAILED
    assert stored.error_cause == "out_of_memory"
    assert stored.error_message is not None
    assert "ran out of memory" in stored.error_message
    assert "run it on a machine or in a container with more memory" in stored.error_message
    assert "LAKANA_WORKER" not in stored.error_message


def test_a_refusal_before_execution_returns_none(session: Session, principal: PrincipalRef) -> None:
    """A run whose principal is gone fails without an exception to classify."""
    run_id = _run(session, None, "SELECT 1 AS x")
    assert run_one(session, run_id, AllowAllAuthorization()) is None
    stored = session.get(QueryRun, run_id)
    assert stored is not None
    assert stored.status is QueryRunStatus.FAILED


def test_an_inactive_principal_returns_none(session: Session, principal: PrincipalRef) -> None:
    """A deactivated principal's run is refused before execution, with nothing to classify."""
    row = session.get(Principal, principal.id)
    assert row is not None
    row.is_active = False
    session.commit()
    run_id = _run(session, principal.id, "SELECT 1 AS x")
    assert run_one(session, run_id, AllowAllAuthorization()) is None


def test_a_failed_resolution_returns_the_underlying_exception_not_the_opaque_wrapper(
    session: Session, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The opaque `_ResolutionFailed` carries no type; the cause is what classification needs."""

    def unreachable(*args: object, **kwargs: object) -> None:
        raise ConnectionError("secret-host unreachable")

    monkeypatch.setattr("lakana_worker.pipeline.StorageResolver.for_environment", unreachable)
    run_id = _run(session, principal.id, "SELECT 1 AS x")
    failure = run_one(session, run_id, AllowAllAuthorization())
    assert isinstance(failure, ConnectionError)
    stored = session.get(QueryRun, run_id)
    assert stored is not None
    assert "secret-host" not in (stored.error_message or "")


def _stamp_reclaimed(session: Session, run_id: uuid.UUID) -> None:
    """Commit what the scheduler's lost-lease reclaim writes on a task's query run.

    Parameters
    ----------
    session
        Database session.
    run_id
        The query run.
    """
    session.execute(
        update(QueryRun)
        .where(QueryRun.id == run_id)
        .values(status=QueryRunStatus.FAILED, error_message=_RECLAIMED)
    )
    session.commit()


_RECLAIMED = "The worker lost its lease."


@pytest.mark.parametrize("refusal", ["deleted", "inactive", "invalid-token"])
def test_an_early_refusal_keeps_a_racing_reclaims_stamp(
    session: Session, principal: PrincipalRef, refusal: str
) -> None:
    """The refusals before execution claim the run like the start does: a reclaim's text stands."""
    run_id = _run(session, None if refusal == "deleted" else principal.id, "SELECT 1 AS x")
    if refusal == "inactive":
        row = session.get(Principal, principal.id)
        assert row is not None
        row.is_active = False
    elif refusal == "invalid-token":
        stored = session.get(QueryRun, run_id)
        assert stored is not None
        stored.task_token_id = uuid.uuid4()
    session.commit()
    _stamp_reclaimed(session, run_id)
    assert run_one(session, run_id, AllowAllAuthorization()) is None
    session.expire_all()
    stored = session.get(QueryRun, run_id)
    assert stored is not None
    assert (stored.status, stored.error_message) == (QueryRunStatus.FAILED, _RECLAIMED)
    assert stored.finished_at is None
    assert not session.scalars(
        select(AuditEvent).where(AuditEvent.event_type == AuditEventType.QUERY_COMPLETED)
    ).all()


def test_a_field_flushed_before_a_lost_outcome_claim_does_not_land(
    session: Session, principal: PrincipalRef
) -> None:
    """The outcome CAS lost to a reclaim: what the attempt already flushed is rolled back too."""
    run_id = _run(session, principal.id, "SELECT 1 AS x")
    session.execute(
        update(QueryRun).where(QueryRun.id == run_id).values(status=QueryRunStatus.RUNNING)
    )
    session.commit()
    _stamp_reclaimed(session, run_id)
    run = session.get(QueryRun, run_id)
    assert run is not None
    run.result_location = "_artifacts/queries/stale/result.arrow"
    session.flush()
    _keep_a_prior_stamp(session, run, QueryRunStatus.SUCCEEDED)
    session.commit()
    session.expire_all()
    stored = session.get(QueryRun, run_id)
    assert stored is not None
    assert (stored.status, stored.result_location) == (QueryRunStatus.FAILED, None)


def _cause_of(session: Session, run_id: uuid.UUID) -> str | None:
    """Read the stored cause of a run's failure.

    Parameters
    ----------
    session
        Database session.
    run_id
        The query run.

    Returns
    -------
    str or None
        `QueryRun.error_cause`.
    """
    session.expire_all()
    stored = session.get(QueryRun, run_id)
    assert stored is not None
    return stored.error_cause


@pytest.mark.parametrize(
    ("sql", "cause"),
    [
        ("SELECT * FROM bronze.ghost", "not_found"),
        ("SELECT nope FROM range(1)", "invalid_query"),
        ("SELEC 1", "invalid_query"),
        ("SELECT 'unterminated", "invalid_query"),
    ],
)
def test_a_failed_run_stores_a_machine_readable_cause(
    session: Session, principal: PrincipalRef, sql: str, cause: str
) -> None:
    """The cause is set where the failure is recorded, from the exception's type."""
    run_id = _run(session, principal.id, sql)
    run_one(session, run_id, AllowAllAuthorization())
    assert _cause_of(session, run_id) == cause


def test_a_result_over_the_limit_has_the_cause_result_too_large(
    session: Session, principal: PrincipalRef
) -> None:
    run_id = _run(session, principal.id, "SELECT * FROM range(10)")
    run_one(session, run_id, AllowAllAuthorization(), limits=QueryLimitsConfig(max_result_rows=1))
    assert _cause_of(session, run_id) == "result_too_large"
    run_id = _run(session, principal.id, "SELECT * FROM range(10)")
    run_one(session, run_id, AllowAllAuthorization(), limits=QueryLimitsConfig(max_result_bytes=1))
    assert _cause_of(session, run_id) == "result_too_large"


def test_a_succeeded_run_has_no_cause(session: Session, principal: PrincipalRef) -> None:
    run_id = _run(session, principal.id, "SELECT 1 AS x")
    run_one(session, run_id, AllowAllAuthorization())
    assert _cause_of(session, run_id) is None


def test_an_unexpected_error_has_the_cause_internal(
    session: Session, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(*args: object, **kwargs: object) -> None:
        raise ConnectionError("network down")

    monkeypatch.setattr("lakana_worker.pipeline.execute_query", explode)
    run_id = _run(session, principal.id, "SELECT 1 AS x")
    run_one(session, run_id, AllowAllAuthorization())
    assert _cause_of(session, run_id) == "internal"


def test_a_failed_resolution_is_classified_by_its_cause_not_the_opaque_wrapper(
    session: Session, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unreachable(*args: object, **kwargs: object) -> None:
        raise PermissionDenied("no")

    monkeypatch.setattr("lakana_worker.pipeline.StorageResolver.for_environment", unreachable)
    run_id = _run(session, principal.id, "SELECT 1 AS x")
    run_one(session, run_id, AllowAllAuthorization())
    assert _cause_of(session, run_id) == "permission_denied"


@pytest.mark.parametrize("refusal", ["deleted", "inactive", "invalid-token"])
def test_an_early_refusal_has_the_cause_permission_denied(
    session: Session, principal: PrincipalRef, refusal: str
) -> None:
    run_id = _run(session, None if refusal == "deleted" else principal.id, "SELECT 1 AS x")
    if refusal == "inactive":
        row = session.get(Principal, principal.id)
        assert row is not None
        row.is_active = False
    elif refusal == "invalid-token":
        stored = session.get(QueryRun, run_id)
        assert stored is not None
        stored.task_token_id = uuid.uuid4()
    session.commit()
    run_one(session, run_id, AllowAllAuthorization())
    assert _cause_of(session, run_id) == "permission_denied"


@pytest.mark.parametrize(
    ("exc", "cause"),
    [
        (QueryCancelled("c"), "cancelled"),
        (QueryTimeout("t"), "timeout"),
        (ResultTooLarge("big"), "result_too_large"),
        (TransientQueryError("io"), "internal"),
        (OutOfMemoryQuery("oom"), "out_of_memory"),
        (DuckDBInternalError("bug"), "internal"),
        (ParseError("bad"), "invalid_query"),
        (TokenError("bad"), "invalid_query"),
        (NotFoundError("n"), "not_found"),
        (PermissionDenied("p"), "permission_denied"),
        (WriteFenceRefused(), "worker_lost"),
        (WorkerStaleQuery(WORKER_STALE_MESSAGE), "worker_lost"),
        (ValidationError("v"), "invalid_query"),
        (UnsupportedTableError("u"), "invalid_query"),
        (ConflictError("c"), "internal"),
        (RuntimeError("r"), "internal"),
    ],
)
def test_the_cause_follows_the_exception_type(exc: BaseException, cause: str) -> None:
    assert failure_cause(exc) == cause


def test_a_syntax_error_shows_the_parsers_position_without_escape_codes(
    session: Session, principal: PrincipalRef
) -> None:
    """The message names the parser's error and where it is, with no terminal codes."""
    run_id = _run(session, principal.id, "SELEC 1")
    run_one(session, run_id, AllowAllAuthorization())
    session.expire_all()
    stored = session.get(QueryRun, run_id)
    assert stored is not None
    assert stored.error_message is not None
    assert stored.error_message.startswith("SQL syntax error")
    assert "Invalid expression / Unexpected token" in stored.error_message
    assert "line 1, column 7" in stored.error_message
    assert "\x1b" not in stored.error_message
    assert "Unexpected error" not in stored.error_message


@pytest.mark.slow
def test_an_out_of_memory_run_has_its_own_cause(session: Session, principal: PrincipalRef) -> None:
    """End to end: DuckDB's out-of-memory error is stored as `out_of_memory`."""
    values = ",".join(f"({i},'value-{i:06d}')" for i in range(3000))
    sql = f"WITH a(x, s) AS (VALUES {values}) SELECT list(a.s || b.s) AS l FROM a, a b"
    run_id = _run(session, principal.id, sql)
    run_one(
        session,
        run_id,
        AllowAllAuthorization(),
        limits=QueryLimitsConfig(memory_limit="100MB", threads=2),
    )
    assert _cause_of(session, run_id) == "out_of_memory"


def test_a_run_is_registered_as_in_flight_while_it_executes(
    session: Session, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The memory watchdog can only fail what `run_one` has registered."""
    from lakana_worker.activity import ACTIVITY

    seen: list[tuple[uuid.UUID, ...]] = []

    def spy(*args: object, **kwargs: object) -> None:
        seen.append(ACTIVITY.snapshot()[0])
        raise ConnectionError("stop here")

    monkeypatch.setattr("lakana_worker.pipeline.execute_query", spy)
    run_id = _run(session, principal.id, "SELECT 1 AS x")
    run_one(session, run_id, AllowAllAuthorization())
    assert seen == [(run_id,)]
    assert ACTIVITY.snapshot()[0] == ()


def test_a_worker_stopped_for_memory_fails_its_running_query_for_good(
    session: Session, principal: PrincipalRef
) -> None:
    """The user sees the cause and the next step instead of a run stuck `RUNNING`."""
    from lakana_worker.activity import Activity
    from lakana_worker.memory import Limit, record_memory_failure

    running = _run(session, principal.id, "SELECT 1")
    queued = _run(session, principal.id, "SELECT 2")
    session.execute(
        update(QueryRun)
        .where(QueryRun.id == running)
        .values(status=QueryRunStatus.RUNNING, worker_id="w1")
    )
    session.commit()
    activity = Activity()
    with activity.query(running), activity.query(queued):
        record_memory_failure(
            lambda: session, Limit(4 * 1024**3, "4GB", "x"), lambda: 0.5, "w1", activity
        )
    session.expire_all()
    stored = session.get(QueryRun, running)
    assert stored is not None
    assert (stored.status, stored.error_cause) == (QueryRunStatus.FAILED, "out_of_memory")
    assert stored.finished_at is not None
    assert stored.error_message is not None
    assert "its limit is 4GB" in stored.error_message
    assert "LAKANA_WORKER__MEMORY_LIMIT" in stored.error_message
    untouched = session.get(QueryRun, queued)
    assert untouched is not None and untouched.status is QueryRunStatus.QUEUED


def test_a_run_is_not_stamped_by_the_main_thread_once_the_worker_is_stopping(
    session: Session, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The watchdog owns the outcome of a stop; `run_one` writes nothing more."""
    from lakana_worker.activity import ACTIVITY

    def explode(*args: object, **kwargs: object) -> None:
        raise ConnectionError("the connection died with the process")

    monkeypatch.setattr("lakana_worker.pipeline.execute_query", explode)
    monkeypatch.setattr(ACTIVITY, "stopping", True)
    run_id = _run(session, principal.id, "SELECT 1 AS x")
    failure = run_one(session, run_id, AllowAllAuthorization())
    assert isinstance(failure, ConnectionError)
    session.expire_all()
    stored = session.get(QueryRun, run_id)
    assert stored is not None
    assert stored.status is QueryRunStatus.RUNNING
    assert stored.finished_at is None and stored.error_message is None


def test_a_read_whose_child_is_killed_from_outside_stores_no_result(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """Spec 8: the kernel's out-of-memory killer ends the child; nothing is stored."""
    run_id = _run(
        session,
        principal.id,
        "SELECT sum(hash(a.range + b.range)) AS n FROM range(40000) a, range(40000) b",
    )

    def kill_when_started() -> None:
        """Wait for the query's child, give it a moment, kill its group with SIGKILL."""
        deadline = time.monotonic() + 10
        while not REGISTRY.pids() and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(0.3)
        for pid in REGISTRY.pids():
            os.killpg(pid, signal.SIGKILL)

    killer = threading.Thread(target=kill_when_started, daemon=True)
    killer.start()
    failure = run_one(session, run_id, AllowAllAuthorization())
    killer.join(5)

    assert isinstance(failure, TransientQueryError)
    session.expire_all()
    stored = session.get(QueryRun, run_id)
    assert stored is not None
    assert stored.status == QueryRunStatus.FAILED
    assert stored.error_cause == "internal"
    assert stored.error_message is not None
    assert stored.error_message.startswith(
        "Query failed: the query process was ended from outside the worker (signal 9)."
    )
    assert stored.result_location is None
    assert not (tmp_path / "storage" / "_artifacts" / "queries" / str(run_id)).exists()


def test_a_read_whose_child_ends_after_its_last_batch_stores_no_result(
    session: Session, principal: PrincipalRef, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R3.8: all 30 rows and the end marker arrived, then a signal ended the child: no result."""
    script = Path(__file__).parent / "scripted_child.py"
    monkeypatch.setattr(
        executor_module, "_ARGV", [sys.executable, str(script), "full_stream_then_killed"]
    )
    run_id = _run(session, principal.id, "SELECT 1 AS i")

    failure = run_one(session, run_id, AllowAllAuthorization())

    assert isinstance(failure, TransientQueryError)
    session.expire_all()
    stored = session.get(QueryRun, run_id)
    assert stored is not None
    assert (stored.status, stored.result_location) == (QueryRunStatus.FAILED, None)
    assert not (tmp_path / "storage" / "_artifacts" / "queries" / str(run_id)).exists()


_SLOW_SQL = "SELECT sum(hash(a.range + b.range)) AS n FROM range(40000) a, range(40000) b"
"""About nine seconds by itself at a flat 59 MB, which bounds a test whose cancel fails."""


def _events(session: Session, kind: AuditEventType) -> list[AuditEvent]:
    """List the audit events of one type.

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
    return list(session.scalars(select(AuditEvent).where(AuditEvent.event_type == kind)))


def _flag(session: Session, run_id: uuid.UUID) -> None:
    """Set `cancel_requested`, as a cancel request on a running query does.

    Parameters
    ----------
    session
        Database session; commits.
    run_id
        The run.
    """
    session.execute(update(QueryRun).where(QueryRun.id == run_id).values(cancel_requested=True))
    session.commit()


def _checked_out(session: Session) -> int:
    """Count the connections of the session's engine that are in use.

    Parameters
    ----------
    session
        A session on a SQLite file, whose engine pools its connections.

    Returns
    -------
    int
        How many are checked out of the pool.
    """
    engine = cast("Engine", session.get_bind())
    return cast("QueuePool", engine.pool).checkedout()


def _pollers() -> list[threading.Thread]:
    """List the live threads that watch a cancel flag.

    Returns
    -------
    list of threading.Thread
        The threads named `lakana-cancel-poll`.
    """
    return [t for t in threading.enumerate() if t.name == "lakana-cancel-poll"]


def test_a_cancel_seen_before_the_result_is_complete_wins_and_nothing_is_stored(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """The row, the stored result and the audit event agree."""
    run_id = _run(session, principal.id, _SLOW_SQL)
    _flag(session, run_id)
    started = time.monotonic()
    failure = run_one(session, run_id, AllowAllAuthorization())
    assert time.monotonic() - started < 6
    assert type(failure) is QueryCancelRequested
    run = session.get(QueryRun, run_id)
    assert run is not None
    assert (run.status, run.error_cause, run.error_message) == (
        QueryRunStatus.CANCELLED,
        "cancelled",
        CANCELLED_MESSAGE,
    )
    assert (run.result_location, run.finished_at is not None) == (None, True)
    assert not (tmp_path / "storage" / "_artifacts" / "queries" / str(run_id)).exists()
    (completed,) = _events(session, AuditEventType.QUERY_COMPLETED)
    assert completed.detail["status"] == "CANCELLED"
    assert _pollers() == []


def test_a_cancel_request_through_the_service_stops_the_running_query(
    session: Session, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real request, made from another session while the query runs, ends it as `CANCELLED`."""
    run_id = _run(session, principal.id, _SLOW_SQL)
    real = pipeline.execute_query
    asked: list[float] = []

    def cancel_then_run(
        sql: str,
        parameters: Mapping[str, object] | None = None,
        *,
        read_prefixes: Sequence[str] = (),
        spill: bool = False,
        limits: QueryLimitsConfig | None = None,
    ) -> pa.Table:
        with Session(session.get_bind()) as other:
            answered = QueryService(other, AllowAllAuthorization()).cancel_query(
                principal, "dev", run_id
            )
        assert answered.status is QueryRunStatus.RUNNING
        assert len(_pollers()) == 1
        asked.append(time.monotonic())
        return real(sql, parameters, read_prefixes=read_prefixes, spill=spill, limits=limits)

    monkeypatch.setattr("lakana_worker.pipeline.execute_query", cancel_then_run)
    failure = run_one(session, run_id, AllowAllAuthorization())
    elapsed = time.monotonic() - asked[0]
    assert type(failure) is QueryCancelRequested
    assert elapsed < 4, f"a cancel took {elapsed:.2f} s"
    print(f"cancel latency: {elapsed:.2f} s")
    session.expire_all()
    run = session.get(QueryRun, run_id)
    assert run is not None
    assert (run.status, run.cancel_requested, run.error_cause) == (
        QueryRunStatus.CANCELLED,
        True,
        "cancelled",
    )
    (event,) = _events(session, AuditEventType.QUERY_CANCELLED)
    assert event.detail["status"] == "RUNNING"
    (completed,) = _events(session, AuditEventType.QUERY_COMPLETED)
    assert completed.detail["status"] == "CANCELLED"


def test_a_cancel_after_the_run_succeeded_changes_nothing(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A finished run stays what it is, and no event is written."""
    run_id = _run(session, principal.id, "SELECT 1 AS x")
    assert run_one(session, run_id, AllowAllAuthorization()) is None
    answered = QueryService(session, AllowAllAuthorization()).cancel_query(principal, "dev", run_id)
    assert (answered.status, answered.cancel_requested, answered.error_cause) == (
        QueryRunStatus.SUCCEEDED,
        False,
        None,
    )
    assert answered.result_location is not None
    assert (tmp_path / "storage" / answered.result_location).is_file()
    assert _events(session, AuditEventType.QUERY_CANCELLED) == []
    assert _events(session, AuditEventType.QUERY_COMPLETED)[0].detail["status"] == "SUCCEEDED"


def test_a_flag_set_after_the_result_was_judged_complete_leaves_the_run_succeeded(
    session: Session, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The decision is the worker thread's, made once: a later flag does not undo a result."""
    run_id = _run(session, principal.id, "SELECT 1 AS x")

    def finish_then_flag(*args: object, **kwargs: object) -> pa.Table:
        _flag(session, run_id)
        return pa.table({"x": [1]})

    monkeypatch.setattr("lakana_worker.pipeline.execute_query", finish_then_flag)
    assert run_one(session, run_id, AllowAllAuthorization()) is None
    run = session.get(QueryRun, run_id)
    assert run is not None
    assert (run.status, run.error_cause) == (QueryRunStatus.SUCCEEDED, None)
    assert run.result_location is not None


def test_a_run_cancelled_while_queued_is_never_started(
    session: Session, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _run(session, principal.id, "SELECT 1 AS x")
    QueryService(session, AllowAllAuthorization()).cancel_query(principal, "dev", run_id)
    monkeypatch.setattr(
        "lakana_worker.pipeline.execute_query", lambda *a, **k: pytest.fail("it ran")
    )
    assert run_one(session, run_id, AllowAllAuthorization()) is None
    run = session.get(QueryRun, run_id)
    assert run is not None and run.status is QueryRunStatus.CANCELLED
    assert _events(session, AuditEventType.QUERY_COMPLETED) == []
    assert _pollers() == []


def test_the_cancel_read_flushes_nothing_of_the_run_in_progress(
    session: Session, principal: PrincipalRef
) -> None:
    """The read flushes nothing: a flush would send a half-set outcome."""
    run_id = _run(session, principal.id, "SELECT 1 AS x")
    assert pipeline._cancel_requested(session, run_id) is False
    _flag(session, run_id)
    run = session.get(QueryRun, run_id)
    assert run is not None
    run.error_message = "half an outcome"
    assert pipeline._cancel_requested(session, run_id) is True
    assert run in session.dirty
    assert pipeline._cancel_requested(session, uuid.uuid4()) is False


def test_the_core_and_the_worker_use_one_cancel_message() -> None:
    assert CANCELLED_MESSAGE == QUERY_CANCELLED_MESSAGE


@pytest.fixture
def quick_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    """Poll the flag every 10 ms instead of every half second.

    Parameters
    ----------
    monkeypatch
        Replaces the interval.
    """
    monkeypatch.setattr("lakana_worker.pipeline._CANCEL_POLL_SECONDS", 0.01)


@pytest.mark.usefixtures("quick_poll")
def test_the_watch_sees_a_flag_in_a_thread_of_its_own_and_ends_with_the_block(
    session: Session, principal: PrincipalRef
) -> None:
    run_id = _run(session, principal.id, "SELECT 1 AS x")
    with pipeline._watching_for_cancel(session, run_id) as requested:
        assert len(_pollers()) == 1
        assert requested() is False
        _flag(session, run_id)
        deadline = time.monotonic() + 5
        while not requested() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert requested() is True
    assert _pollers() == []
    assert _checked_out(session) == 0


def test_the_watch_holds_no_connection_between_two_reads(
    session: Session, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _run(session, principal.id, "SELECT 1 AS x")
    session.rollback()  # the main session holds no connection either
    monkeypatch.setattr("lakana_worker.pipeline._CANCEL_POLL_SECONDS", 0.3)
    reads: list[bool] = []
    real = pipeline._cancel_requested

    def counted(own: Session, query_run_id: uuid.UUID) -> bool:
        reads.append(real(own, query_run_id))
        return reads[-1]

    monkeypatch.setattr("lakana_worker.pipeline._cancel_requested", counted)
    with pipeline._watching_for_cancel(session, run_id):
        deadline = time.monotonic() + 5
        while not reads and time.monotonic() < deadline:
            time.sleep(0.01)
        deadline = time.monotonic() + 0.25  # less than the interval: the next read has not begun
        while _checked_out(session) != 0 and time.monotonic() < deadline:
            time.sleep(0.005)
        assert not any(reads)
        assert _checked_out(session) == 0


@pytest.mark.usefixtures("quick_poll")
def test_the_watch_ends_when_the_block_raises(session: Session, principal: PrincipalRef) -> None:
    run_id = _run(session, principal.id, "SELECT 1 AS x")
    with pytest.raises(RuntimeError, match="boom"), pipeline._watching_for_cancel(session, run_id):
        raise RuntimeError("boom")
    assert _pollers() == []


def test_run_one_leaves_no_thread_and_no_connection_behind_on_any_path(
    session: Session, principal: PrincipalRef
) -> None:
    """A success, a failure and a run that cannot even load all end the watch."""
    ok = _run(session, principal.id, "SELECT 1 AS x")
    assert run_one(session, ok, AllowAllAuthorization()) is None
    assert _pollers() == []
    bad = _run(session, principal.id, "SELECT * FROM bronze.ghost")
    assert run_one(session, bad, AllowAllAuthorization()) is not None
    assert _pollers() == []
    with pytest.raises(NotFoundError):
        run_one(session, uuid.uuid4(), AllowAllAuthorization())
    assert _pollers() == []
    session.rollback()
    assert _checked_out(session) == 0


def test_a_private_in_memory_database_gets_no_watch_thread(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No other thread can read it, so none is started, and it is logged."""
    engine = create_engine(DatabaseConfig(url="sqlite://"))
    try:
        with (
            caplog.at_level(logging.DEBUG, logger="lakana_worker.pipeline"),
            Session(engine) as own,
            pipeline._watching_for_cancel(own, uuid.uuid4()) as requested,
        ):
            assert _pollers() == []
            assert requested() is False
    finally:
        engine.dispose()
    assert [r.levelno for r in caplog.records if "no cancel watch" in r.getMessage()] == [
        logging.DEBUG
    ]


def test_a_read_that_hangs_does_not_hold_up_the_end_of_the_query(
    session: Session, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The join is bounded: the outcome is committed and returned while the read still blocks."""
    run_id = _run(session, principal.id, "SELECT 1 AS x")
    release = threading.Event()
    monkeypatch.setattr("lakana_worker.pipeline._CANCEL_WATCH_JOIN_SECONDS", 0.05)
    monkeypatch.setattr(
        "lakana_worker.pipeline._cancel_requested", lambda *a, **k: release.wait(5) and False
    )
    try:
        started = time.monotonic()
        assert run_one(session, run_id, AllowAllAuthorization()) is None
        assert time.monotonic() - started < 3
        assert len(_pollers()) == 1  # still blocked in its read
        run = session.get(QueryRun, run_id)
        assert run is not None and run.status is QueryRunStatus.SUCCEEDED
    finally:
        release.set()
        for thread in _pollers():
            thread.join(5)
    assert _pollers() == []


def test_the_callable_the_supervisor_asks_never_waits_for_the_database(
    session: Session, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read of the flag that hangs must not hold up the supervisor's reader."""
    run_id = _run(session, principal.id, "SELECT 1 AS x")
    release = threading.Event()
    monkeypatch.setattr(
        "lakana_worker.pipeline._cancel_requested", lambda *a, **k: release.wait(1) and False
    )
    try:
        with pipeline._watching_for_cancel(session, run_id) as requested:
            started = time.monotonic()
            assert [requested() for _ in range(5)] == [False] * 5
            assert time.monotonic() - started < 0.5
            release.set()
    finally:
        release.set()
    assert _pollers() == []


@pytest.mark.usefixtures("quick_poll")
def test_a_failing_read_is_logged_once_and_retried(
    session: Session,
    principal: PrincipalRef,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    run_id = _run(session, principal.id, "SELECT 1 AS x")
    calls: list[int] = []

    def flaky(*args: object, **kwargs: object) -> bool:
        calls.append(1)
        if len(calls) < 4:
            raise RuntimeError("database down")
        return True

    monkeypatch.setattr("lakana_worker.pipeline._cancel_requested", flaky)
    with caplog.at_level(logging.WARNING), pipeline._watching_for_cancel(session, run_id) as seen:
        deadline = time.monotonic() + 5
        while not seen() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert seen() is True
    assert len([r for r in caplog.records if "cancel" in r.getMessage()]) == 1
    assert _pollers() == []


def _completions(session: Session) -> list[AuditEvent]:
    """List the `QUERY_COMPLETED` audit events, oldest first.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    list of AuditEvent
        The events.
    """
    session.expire_all()
    return list(
        session.scalars(
            select(AuditEvent)
            .where(AuditEvent.event_type == AuditEventType.QUERY_COMPLETED)
            .order_by(AuditEvent.occurred_at, AuditEvent.id)
        )
    )


def _ended_from_inside(
    session: Session, monkeypatch: pytest.MonkeyPatch, how: str, run_id: uuid.UUID
) -> None:
    """Make the real query run, and then end the run from a second connection before it returns.

    The worker's own final update then meets a run somebody else ended.

    Parameters
    ----------
    session
        The worker's session (its engine is used for the second connection).
    monkeypatch
        Patches `pipeline.execute_query`.
    how
        `sweep`, `cancel` or `sweep-committed`.
    run_id
        The run.
    """
    real = cast(Callable[..., object], pipeline.execute_query)
    engine = session.get_bind()

    def run_then_end(*args: object, **kwargs: object) -> object:
        result = real(*args, **kwargs)
        with Session(engine) as other:
            if how == "sweep-committed":
                other.execute(
                    update(QueryRun)
                    .where(QueryRun.id == run_id)
                    .values(write_mode=WriteMode.APPEND, target_name="s.o", rows_written=2)
                )
                other.commit()
            if how == "cancel":
                service = QueryService(other, AllowAllAuthorization(), lease_seconds=60)
                admin = other.scalars(select(Principal)).one()
                service.cancel_query(
                    PrincipalRef(id=admin.id, name=admin.name, type=admin.type), "dev", run_id
                )
            else:
                assert end_lost_runs(other, db_now(other), 60) == 1
        return result

    monkeypatch.setattr(pipeline, "execute_query", run_then_end)


@pytest.mark.parametrize(
    ("how", "status", "cause"),
    [
        ("sweep", QueryRunStatus.FAILED, "worker_lost"),
        ("cancel", QueryRunStatus.CANCELLED, "cancelled"),
        ("sweep-committed", QueryRunStatus.SUCCEEDED, None),
    ],
)
def test_a_late_worker_records_no_second_completion(
    session: Session,
    principal: PrincipalRef,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    how: str,
    status: QueryRunStatus,
    cause: str | None,
) -> None:
    """R4.6, R4.5 a, R4.7.5, R4.7.11: whoever ended the run wrote its one event and its status."""
    run_id = _run(session, principal.id, "SELECT 1 AS x")
    _ended_from_inside(session, monkeypatch, how, run_id)
    with caplog.at_level("WARNING", logger="lakana_worker.pipeline"):
        run_one(session, run_id, AllowAllAuthorization(), worker_id="w1")
    assert (
        f"Query run {run_id} was ended by someone else (stored status {status.value}) before "
        "this worker recorded its result"
    ) in caplog.text
    session.expire_all()
    stored = session.get(QueryRun, run_id)
    assert stored is not None
    assert (stored.status, stored.error_cause) == (status, cause)
    assert stored.result_location is None  # the late worker's own fields did not land
    (event,) = _completions(session)
    assert event.detail["status"] == status.value


def _task_write_run(session: Session, principal_id: uuid.UUID) -> uuid.UUID:
    """Queue a read query shaped like a SQL task's own write (a task run, no task token).

    Parameters
    ----------
    session
        Database session.
    principal_id
        The run-as principal.

    Returns
    -------
    uuid.UUID
        The query run's id.
    """
    env = session.scalars(select(Environment)).one()
    repo = Repository(id=uuid.uuid4(), name="r", kind=RepositoryKind.HTTPS, url="https://h/a.git")
    session.add(repo)
    session.flush()
    version = JobVersion(
        id=uuid.uuid4(),
        repository_id=repo.id,
        environment_id=env.id,
        commit_sha="a" * 40,
        manifest_json={},
        code_uri="file:///x",
        code_sha256="0" * 64,
    )
    session.add(version)
    session.flush()
    pipeline_run = Run(
        id=uuid.uuid4(),
        environment_id=env.id,
        repository_id=repo.id,
        job_version_id=version.id,
        run_as_principal_id=principal_id,
        kind=RunKind.FULL,
        status=RunStatus.RUNNING,
        selection_json={},
    )
    session.add(pipeline_run)
    session.flush()
    task = TaskRun(
        id=uuid.uuid4(),
        run_id=pipeline_run.id,
        asset="a.b",
        status=TaskStatus.RUNNING,
        max_attempts=1,
        timeout_seconds=60,
        retry_delay_seconds=0,
        upstream_json=[],
    )
    session.add(task)
    session.flush()
    query = QueryRun(
        id=uuid.uuid4(),
        environment_id=env.id,
        principal_id=principal_id,
        sql_text="SELECT 1 AS x",
        status=QueryRunStatus.QUEUED,
        task_run_id=task.id,
    )
    session.add(query)
    session.commit()
    return query.id


@pytest.mark.parametrize("by", ["reclaim", "sweep"])
def test_a_tasks_write_stamped_by_a_reclaim_or_the_sweep_is_still_audited_with_the_stored_status(
    session: Session, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch, by: str
) -> None:
    """The task path writes no event, so the worker's one event stands (today's behaviour)."""
    run_id = _task_write_run(session, principal.id)
    real = cast(Callable[..., object], pipeline.execute_query)
    engine = session.get_bind()

    def run_then_end(*args: object, **kwargs: object) -> object:
        result = real(*args, **kwargs)
        with Session(engine) as other:
            if by == "reclaim":
                _stamp_reclaimed(other, run_id)
            else:
                assert end_lost_runs(other, db_now(other), 60) == 1
        return result

    monkeypatch.setattr(pipeline, "execute_query", run_then_end)
    run_one(session, run_id, AllowAllAuthorization(), worker_id="w1")
    session.expire_all()
    stored = session.get(QueryRun, run_id)
    assert stored is not None and stored.status is QueryRunStatus.FAILED
    (event,) = _completions(session)
    assert event.detail["status"] == "FAILED"
    if by == "sweep":
        # The fixture's run is a read with a task run and is not the task's `query_run_id`:
        # the shape of a check's count, which the sweep words as one.
        assert stored.error_message == CHECK_LOST_MESSAGE


@pytest.mark.parametrize(
    ("status", "cause", "task_run", "token", "expected"),
    [
        (QueryRunStatus.FAILED, "worker_lost", False, False, True),
        (QueryRunStatus.CANCELLED, "cancelled", False, False, True),
        (QueryRunStatus.SUCCEEDED, None, False, False, True),
        (QueryRunStatus.FAILED, None, False, False, False),
        (QueryRunStatus.FAILED, None, True, True, False),  # a Python task's query, reclaimed
        (QueryRunStatus.FAILED, "worker_lost", True, False, False),  # a SQL task's own write
        (QueryRunStatus.CANCELLED, "cancelled", True, False, False),
        (QueryRunStatus.SUCCEEDED, None, True, False, False),
        (QueryRunStatus.FAILED, "worker_lost", True, True, True),  # a Python task's query, swept
    ],
)
def test_the_audit_of_a_run_ended_in_the_workers_place_is_told_by_who_ended_it(
    status: QueryRunStatus, cause: str | None, task_run: bool, token: bool, expected: bool
) -> None:
    run = QueryRun(
        id=uuid.uuid4(),
        status=status,
        error_cause=cause,
        task_run_id=uuid.uuid4() if task_run else None,
        task_token_id=uuid.uuid4() if token else None,
    )
    assert _already_audited(run) is expected


def test_the_final_update_holds_to_the_workers_id_as_well(
    session: Session, principal: PrincipalRef
) -> None:
    """R4.7.6: a run another worker holds is not stamped by this one."""
    run_id = _run(session, principal.id, "SELECT 1 AS x")
    session.execute(
        update(QueryRun)
        .where(QueryRun.id == run_id)
        .values(status=QueryRunStatus.RUNNING, worker_id="other")
    )
    session.commit()
    run = session.get(QueryRun, run_id)
    assert run is not None
    assert _keep_a_prior_stamp(session, run, QueryRunStatus.SUCCEEDED, worker_id="w1") is False
    session.expire_all()
    again = session.get(QueryRun, run_id)
    assert again is not None and again.status is QueryRunStatus.RUNNING
    run = session.get(QueryRun, run_id)
    assert run is not None
    assert _keep_a_prior_stamp(session, run, QueryRunStatus.SUCCEEDED, worker_id="other") is True


def _stopped_for_memory(session: Session, run_id: uuid.UUID) -> QueryRun:
    """Stop the worker for memory while one `RUNNING` query run is in flight, then reload it.

    Parameters
    ----------
    session
        Database session.
    run_id
        The in-flight run.

    Returns
    -------
    QueryRun
        The run as stored afterwards.
    """
    from lakana_worker.activity import Activity
    from lakana_worker.memory import Limit, record_memory_failure

    session.execute(update(QueryRun).where(QueryRun.id == run_id).values(worker_id="w1"))
    session.commit()
    activity = Activity()
    with activity.query(run_id):
        record_memory_failure(
            lambda: session, Limit(4 * 1024**3, "4GB", "x"), lambda: 0.5, "w1", activity
        )
    session.expire_all()
    stored = session.get(QueryRun, run_id)
    assert stored is not None
    return stored


@pytest.mark.parametrize(
    ("fields", "status", "cause"),
    [
        ({"write_mode": WriteMode.APPEND, "rows_written": 3}, QueryRunStatus.SUCCEEDED, None),
        ({"write_mode": WriteMode.APPEND, "rows_written": 0}, QueryRunStatus.SUCCEEDED, None),
        ({"write_mode": WriteMode.VIEW, "rows_written": 0}, QueryRunStatus.SUCCEEDED, None),
        ({"write_mode": WriteMode.APPEND}, QueryRunStatus.FAILED, "out_of_memory"),
        ({}, QueryRunStatus.FAILED, "out_of_memory"),
        (
            {"write_mode": WriteMode.MAINTAIN, "rows_written": 1},
            QueryRunStatus.FAILED,
            "out_of_memory",
        ),
    ],
)
def test_a_worker_stopped_for_memory_ends_a_committed_write_succeeded(
    session: Session,
    principal: PrincipalRef,
    fields: dict[str, object],
    status: QueryRunStatus,
    cause: str | None,
) -> None:
    """M2, R4.1: the evidence decides; a write that landed is not reported failed."""
    run_id = _run(session, principal.id, "SELECT 1")
    session.execute(
        update(QueryRun)
        .where(QueryRun.id == run_id)
        .values(status=QueryRunStatus.RUNNING, worker_id="w1", **fields)
    )
    session.commit()
    stored = _stopped_for_memory(session, run_id)
    assert (stored.status, stored.error_cause) == (status, cause)
    assert stored.finished_at is not None
    assert (stored.error_message is None) == (status is QueryRunStatus.SUCCEEDED)
    assert _completions(session) == []  # the watchdog writes no audit event, as before


@pytest.mark.parametrize(
    ("rows", "status", "cause"),
    [
        (2, QueryRunStatus.SUCCEEDED, None),
        (0, QueryRunStatus.SUCCEEDED, None),
        (None, QueryRunStatus.FAILED, "out_of_memory"),
    ],
)
def test_a_sql_tasks_own_write_stopped_for_memory_ends_by_its_evidence(
    session: Session,
    principal: PrincipalRef,
    rows: int | None,
    status: QueryRunStatus,
    cause: str | None,
) -> None:
    """A SQL task's write that had landed is SUCCEEDED, as an ad-hoc one; the task follows."""
    run_id = _task_write_run(session, principal.id)
    session.execute(
        update(QueryRun)
        .where(QueryRun.id == run_id)
        .values(status=QueryRunStatus.RUNNING, write_mode=WriteMode.APPEND, rows_written=rows)
    )
    session.commit()
    stored = _stopped_for_memory(session, run_id)
    assert (stored.status, stored.error_cause) == (status, cause)


def test_a_python_tasks_query_with_evidence_is_ended_succeeded_by_the_memory_stop(
    session: Session, principal: PrincipalRef
) -> None:
    run_id = _task_write_run(session, principal.id)
    session.execute(
        update(QueryRun)
        .where(QueryRun.id == run_id)
        .values(
            status=QueryRunStatus.RUNNING,
            task_token_id=uuid.uuid4(),
            write_mode=WriteMode.APPEND,
            rows_written=2,
        )
    )
    session.commit()
    assert _stopped_for_memory(session, run_id).status is QueryRunStatus.SUCCEEDED


def test_evidence_that_arrives_between_the_two_statements_is_not_failed(
    session: Session, principal: PrincipalRef
) -> None:
    """R4.7.3: the failing statement carries the evidence in its own WHERE.

    The commit lands after the first statement (which found no evidence) and before the second:
    the run must stay `RUNNING` for the sweep, not become `FAILED` over a write that is there.
    """
    run_id = _run(session, principal.id, "SELECT 1")
    session.execute(
        update(QueryRun)
        .where(QueryRun.id == run_id)
        .values(status=QueryRunStatus.RUNNING, write_mode=WriteMode.APPEND)
    )
    session.commit()
    updates: list[str] = []

    def commit_lands(
        conn: object, cursor: object, statement: str, parameters: object, *args: object
    ) -> None:
        if statement.startswith("UPDATE query_runs SET status"):
            updates.append(statement)
            if len(updates) == 2:
                cast(sqlite3.Cursor, cursor).execute(
                    "UPDATE query_runs SET rows_written = 2 WHERE id = ?", (run_id.hex,)
                )

    engine = session.get_bind()
    event.listen(engine, "before_cursor_execute", commit_lands)
    try:
        stored = _stopped_for_memory(session, run_id)
    finally:
        event.remove(engine, "before_cursor_execute", commit_lands)
    assert len(updates) == 2
    assert (stored.status, stored.rows_written) == (QueryRunStatus.RUNNING, 2)


def test_a_run_whose_final_status_could_not_be_written_is_ended_when_the_worker_is_idle(
    session: Session, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """I3: the worker lives and beats, so the sweep never takes the run; the idle check does."""
    from lakana_worker.poller import end_own_orphans

    run_id = _run(session, principal.id, "SELECT 1 AS x")
    real = pipeline._keep_a_prior_stamp
    failures = [RuntimeError("the connection was lost")]

    def flaky(
        session: Session, run: QueryRun, outcome: QueryRunStatus, *, worker_id: str | None
    ) -> bool:
        if failures:
            raise failures.pop()
        return real(session, run, outcome, worker_id=worker_id)

    monkeypatch.setattr(pipeline, "_keep_a_prior_stamp", flaky)
    with pytest.raises(RuntimeError):
        run_one(session, run_id, AllowAllAuthorization(), worker_id="w1")
    session.rollback()
    session.expire_all()
    stored = session.get(QueryRun, run_id)
    assert stored is not None and stored.status is QueryRunStatus.RUNNING
    engine = session.get_bind()
    assert end_own_orphans(lambda: Session(engine), "other") == 0  # not another worker's
    assert end_own_orphans(lambda: Session(engine), "w1") == 1
    assert end_own_orphans(lambda: Session(engine), "w1") == 0
    session.expire_all()
    stored = session.get(QueryRun, run_id)
    assert stored is not None
    assert (stored.status, stored.error_cause) == (QueryRunStatus.FAILED, "worker_lost")
    assert stored.error_message == (
        "The worker running this query was lost, so the query was ended. Run it again."
    )
    (event,) = _completions(session)
    assert event.detail["status"] == "FAILED"


def _taken_by(session: Session, run_id: uuid.UUID, worker_id: str) -> None:
    """Mark a run `RUNNING` under a worker's id, as that worker's claim would.

    Parameters
    ----------
    session
        Database session.
    run_id
        The run.
    worker_id
        The claiming worker.
    """
    session.execute(
        update(QueryRun)
        .where(QueryRun.id == run_id)
        .values(status=QueryRunStatus.RUNNING, worker_id=worker_id)
    )
    session.commit()


def _no_query_may_start(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Make any query start recorded, and fail it.

    Parameters
    ----------
    monkeypatch
        Patches `pipeline.execute_query`.

    Returns
    -------
    list of str
        One entry per start.
    """
    started: list[str] = []

    def refuse(*args: object, **kwargs: object) -> object:
        started.append("started")
        raise AssertionError("a worker started a run another worker holds")

    monkeypatch.setattr(pipeline, "execute_query", refuse)
    return started


def test_a_second_workers_claim_of_a_running_run_matches_nothing(
    session: Session, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SQLite has no row lock between workers; the claim holds the run to its claimer."""
    run_id = _run(session, principal.id, "SELECT 1 AS x")
    _taken_by(session, run_id, "A")
    started = _no_query_may_start(monkeypatch)
    with Session(session.get_bind()) as second:
        assert run_one(second, run_id, AllowAllAuthorization(), worker_id="B") is None
    assert started == []
    assert _completions(session) == []
    stored = session.get(QueryRun, run_id)
    assert stored is not None
    assert (stored.status, stored.worker_id, stored.started_at) == (
        QueryRunStatus.RUNNING,
        "A",
        None,
    )


def test_a_tasks_write_run_inserted_under_a_worker_is_claimed_by_that_worker(
    session: Session, principal: PrincipalRef
) -> None:
    """A task's write run is inserted `RUNNING` under its worker's id; it claims it."""
    run_id = _task_write_run(session, principal.id)
    _taken_by(session, run_id, "A")
    assert run_one(session, run_id, AllowAllAuthorization(), worker_id="A") is None
    session.expire_all()
    stored = session.get(QueryRun, run_id)
    assert stored is not None
    assert (stored.status, stored.worker_id) == (QueryRunStatus.SUCCEEDED, "A")


def test_a_final_update_refused_on_a_run_another_worker_holds_writes_no_event(
    session: Session, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run is still `RUNNING` under B, whose own attempt writes the one event."""
    run_id = _run(session, principal.id, "SELECT 1 AS x")
    real = cast(Callable[..., object], pipeline.execute_query)
    engine = session.get_bind()

    def run_then_hand_over(*args: object, **kwargs: object) -> object:
        result = real(*args, **kwargs)
        with Session(engine) as other:
            _taken_by(other, run_id, "B")
        return result

    monkeypatch.setattr(pipeline, "execute_query", run_then_hand_over)
    run_one(session, run_id, AllowAllAuthorization(), worker_id="A")
    assert _completions(session) == []
    stored = session.get(QueryRun, run_id)
    assert stored is not None
    assert (stored.status, stored.worker_id) == (QueryRunStatus.RUNNING, "B")


def test_a_second_workers_refusal_of_a_running_run_matches_nothing(
    session: Session, principal: PrincipalRef
) -> None:
    """B's refusal of a run RUNNING under A fails nothing and audits nothing."""
    row = session.get(Principal, principal.id)
    assert row is not None
    row.is_active = False
    session.commit()
    run_id = _run(session, principal.id, "SELECT 1 AS x")
    _taken_by(session, run_id, "A")
    with Session(session.get_bind()) as second:
        assert run_one(second, run_id, AllowAllAuthorization(), worker_id="B") is None
    assert _completions(session) == []
    stored = session.get(QueryRun, run_id)
    assert stored is not None
    assert (stored.status, stored.worker_id, stored.error_message) == (
        QueryRunStatus.RUNNING,
        "A",
        None,
    )


def test_a_workers_refusal_of_its_own_running_run_fails_it(
    session: Session, principal: PrincipalRef
) -> None:
    """A task's write run is `RUNNING` under its own worker: its refusal still lands."""
    row = session.get(Principal, principal.id)
    assert row is not None
    row.is_active = False
    session.commit()
    run_id = _run(session, principal.id, "SELECT 1 AS x")
    _taken_by(session, run_id, "A")
    assert run_one(session, run_id, AllowAllAuthorization(), worker_id="A") is None
    session.expire_all()
    stored = session.get(QueryRun, run_id)
    assert stored is not None and stored.status is QueryRunStatus.FAILED


def _stored(session: Session, run_id: uuid.UUID) -> QueryRun:
    """Read a run on a second connection, past this session's identity map.

    Parameters
    ----------
    session
        Database session whose database is read.
    run_id
        The run.

    Returns
    -------
    QueryRun
        The stored row.
    """
    with Session(session.get_bind()) as second:
        stored = second.get(QueryRun, run_id)
        assert stored is not None
        second.expunge(stored)
        return stored


def _is_still_queued(session: Session, run_id: uuid.UUID) -> bool:
    """Say whether a run is untouched: `QUEUED`, no worker, never started.

    Parameters
    ----------
    session
        Database session whose database is read.
    run_id
        The run.

    Returns
    -------
    bool
        `True` if nothing was written on it.
    """
    stored = _stored(session, run_id)
    return (stored.status, stored.worker_id, stored.started_at) == (
        QueryRunStatus.QUEUED,
        None,
        None,
    )


def _maintenance_run(session: Session, principal_id: uuid.UUID) -> uuid.UUID:
    """Queue a maintenance run (pipeline work), by the given principal.

    Parameters
    ----------
    session
        Database session.
    principal_id
        The submitter.

    Returns
    -------
    uuid.UUID
        The run's id.
    """
    run_id = _run(session, principal_id, "SELECT 1 AS x")
    session.execute(
        update(QueryRun).where(QueryRun.id == run_id).values(write_mode=WriteMode.MAINTAIN)
    )
    session.commit()
    return run_id


def test_a_second_claim_for_the_same_principal_between_the_read_and_the_start_update_keeps_the_run_queued(  # ruff: ignore[line-too-long]
    session: Session, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hint said "below the cap"; another worker's claim lands before the start update."""
    mine = _run(session, principal.id, "SELECT 1 AS x")
    theirs = _run(session, principal.id, "SELECT 2 AS x")
    started = _no_query_may_start(monkeypatch)
    real = pipeline._task_scope_for_run  # the last read before the start update

    def claim_lands_first(inner: Session, run: QueryRun) -> object:
        with Session(session.get_bind()) as other:
            _taken_by(other, theirs, "B")  # commits: the principal now runs one query
        return real(inner, run)

    monkeypatch.setattr(pipeline, "_task_scope_for_run", claim_lands_first)
    assert run_one(session, mine, AllowAllAuthorization(), worker_id="A", running_cap=1) is None
    assert started == []
    assert _is_still_queued(session, mine)
    assert _completions(session) == []


def test_the_start_update_holds_the_cap_without_any_hint(
    session: Session, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`run_one` called directly: at the cap nothing starts or is written; above it, it runs."""
    _taken_by(session, _run(session, principal.id, "SELECT 9 AS x"), "B")
    mine = _run(session, principal.id, "SELECT 1 AS x")
    started = _no_query_may_start(monkeypatch)
    assert run_one(session, mine, AllowAllAuthorization(), worker_id="A", running_cap=1) is None
    assert started == []
    assert _is_still_queued(session, mine)
    assert _completions(session) == []
    monkeypatch.undo()
    assert run_one(session, mine, AllowAllAuthorization(), worker_id="A", running_cap=2) is None
    stored = _stored(session, mine)
    assert (stored.status, stored.worker_id) == (QueryRunStatus.SUCCEEDED, "A")


def test_the_start_update_counts_only_the_principals_own_runs(
    session: Session, principal: PrincipalRef
) -> None:
    """Others' running queries and this principal's maintenance are not in its count."""
    other = uuid.uuid4()
    session.add(Principal(id=other, type=PrincipalType.USER, name="other", is_active=True))
    session.commit()
    for number in range(3):
        _taken_by(session, _run(session, other, f"SELECT {number} AS x"), "B")
    _taken_by(session, _maintenance_run(session, principal.id), "B")
    mine = _run(session, principal.id, "SELECT 1 AS x")
    assert run_one(session, mine, AllowAllAuthorization(), worker_id="A", running_cap=1) is None
    assert _stored(session, mine).status is QueryRunStatus.SUCCEEDED


def test_run_one_without_a_cap_starts_as_before(session: Session, principal: PrincipalRef) -> None:
    """`running_cap=None` counts nothing, beside ten running queries of the same principal."""
    for number in range(10):
        _taken_by(session, _run(session, principal.id, f"SELECT {number} AS x"), "B")
    mine = _run(session, principal.id, "SELECT 1 AS x")
    assert run_one(session, mine, AllowAllAuthorization(), worker_id="A") is None
    assert _stored(session, mine).status is QueryRunStatus.SUCCEEDED


def test_a_tasks_write_run_starts_whatever_the_cap(
    session: Session, principal: PrincipalRef
) -> None:
    """A task's own write is pipeline work: never counted against the cap, never held by it."""
    _taken_by(session, _run(session, principal.id, "SELECT 9 AS x"), "B")
    run_id = _task_write_run(session, principal.id)
    _taken_by(session, run_id, "A")
    assert run_one(session, run_id, AllowAllAuthorization(), worker_id="A", running_cap=1) is None
    assert _stored(session, run_id).status is QueryRunStatus.SUCCEEDED


def test_a_maintenance_run_starts_whatever_the_cap(
    session: Session, principal: PrincipalRef
) -> None:
    """Maintenance is pipeline work: it starts although the principal is at the cap."""
    _taken_by(session, _run(session, principal.id, "SELECT 9 AS x"), "B")
    run_id = _maintenance_run(session, principal.id)
    run_one(session, run_id, AllowAllAuthorization(), worker_id="A", running_cap=1)
    stored = _stored(session, run_id)
    assert (stored.worker_id, stored.started_at is not None) == ("A", True)
    assert stored.status is not QueryRunStatus.QUEUED


def test_the_principal_is_locked_right_before_the_start_update_and_only_for_capped_work(
    session: Session, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lock is taken once, with the principal's id, directly before `UPDATE query_runs`."""
    log: list[str] = []
    locked: list[uuid.UUID] = []
    real = pools.lock_principal

    def record_lock(inner: Session, principal_id: uuid.UUID) -> bool:
        log.append("lock")
        locked.append(principal_id)
        return real(inner, principal_id)

    monkeypatch.setattr(pools, "lock_principal", record_lock)
    here = threading.get_ident()

    def record_statement(conn: object, cursor: object, statement: str, *rest: object) -> None:
        """Record a statement run on this thread; the engine's other threads are not the claim."""
        if threading.get_ident() == here:
            log.append(statement.split()[0])

    event.listen(session.get_bind(), "before_cursor_execute", record_statement)

    def statements_of(run_id: uuid.UUID, running_cap: int | None = None) -> list[str]:
        log.clear()
        run_one(session, run_id, AllowAllAuthorization(), worker_id="A", running_cap=running_cap)
        return list(log)

    capped = statements_of(_run(session, principal.id, "SELECT 1 AS x"), running_cap=5)
    assert locked == [principal.id]
    assert capped.count("lock") == 1
    # A read may sit between the two (it holds no row and takes no write lock); a write may not.
    after_lock = [word for word in capped[capped.index("lock") + 1 :] if word != "SELECT"]
    assert after_lock[0] == "UPDATE"
    assert "UPDATE" not in capped[: capped.index("lock")]

    locked.clear()
    task_write = _task_write_run(session, principal.id)
    _taken_by(session, task_write, "A")
    assert "lock" not in statements_of(task_write, running_cap=5)
    assert "lock" not in statements_of(_maintenance_run(session, principal.id), running_cap=5)
    assert "lock" not in statements_of(_run(session, principal.id, "SELECT 2 AS x"))
    assert locked == []


def test_a_refusal_needs_no_cap(session: Session, principal: PrincipalRef) -> None:
    """An inactive principal at the cap: the run is `FAILED` as today, not left waiting."""
    _taken_by(session, _run(session, principal.id, "SELECT 9 AS x"), "B")
    row = session.get(Principal, principal.id)
    assert row is not None
    row.is_active = False
    session.commit()
    mine = _run(session, principal.id, "SELECT 1 AS x")
    assert run_one(session, mine, AllowAllAuthorization(), worker_id="A", running_cap=1) is None
    stored = _stored(session, mine)
    assert (stored.status, stored.error_message) == (
        QueryRunStatus.FAILED,
        pipeline._INACTIVE_PRINCIPAL_MESSAGE,
    )


def test_a_run_whose_principal_was_deleted_is_refused_and_never_capped(
    session: Session, principal: PrincipalRef
) -> None:
    """No principal, no cap: the run is refused as today, whatever the cap is."""
    mine = _run(session, None, "SELECT 1 AS x")
    assert run_one(session, mine, AllowAllAuthorization(), worker_id="A", running_cap=1) is None
    stored = _stored(session, mine)
    assert (stored.status, stored.error_message) == (
        QueryRunStatus.FAILED,
        pipeline._DELETED_PRINCIPAL_MESSAGE,
    )


def test_a_lock_that_times_out_refuses_the_claim_and_leaves_the_run_queued(
    session: Session, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another claim holds the principal's lock too long: nothing starts, nothing is written."""
    mine = _run(session, principal.id, "SELECT 1 AS x")
    started = _no_query_may_start(monkeypatch)
    monkeypatch.setattr(pools, "lock_principal", lambda inner, principal_id: False)
    assert run_one(session, mine, AllowAllAuthorization(), worker_id="A", running_cap=5) is None
    assert started == []
    assert _is_still_queued(session, mine)
    assert _completions(session) == []


def test_a_lock_that_times_out_tells_the_caller_which_principal(
    session: Session, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The callback gets the principal whose cap lock timed out, and only then."""
    mine = _run(session, principal.id, "SELECT 1 AS x")
    _no_query_may_start(monkeypatch)
    monkeypatch.setattr(pools, "lock_principal", lambda inner, principal_id: False)
    told: list[uuid.UUID] = []
    assert (
        run_one(
            session,
            mine,
            AllowAllAuthorization(),
            worker_id="A",
            running_cap=5,
            on_cap_timeout=told.append,
        )
        is None
    )
    assert told == [principal.id]


def test_no_callback_when_the_claim_is_refused_for_another_reason(
    session: Session, principal: PrincipalRef
) -> None:
    """A principal at the cap is not a timeout: the callback stays silent."""
    _taken_by(session, _run(session, principal.id, "SELECT 9 AS x"), "B")
    mine = _run(session, principal.id, "SELECT 1 AS x")
    told: list[uuid.UUID] = []
    run_one(
        session,
        mine,
        AllowAllAuthorization(),
        worker_id="A",
        running_cap=1,
        on_cap_timeout=told.append,
    )
    assert told == []
    assert _is_still_queued(session, mine)
