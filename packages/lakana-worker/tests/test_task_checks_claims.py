"""The check phase across claims: lost leases, cancels, drains, retries, a worker that dies."""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time
import uuid
from datetime import UTC, datetime
from typing import Any

import pyarrow as pa
import pytest
import sqlalchemy
from check_run_helpers import (
    _DUPLICATE_IDS,
    _SEE_YOURSELF,
    _UNPUBLISHED,
    _after,
    _built_once_then_redeployed,
    _check,
    _checked,
    _claim_ready,
    _counting,
    _counts,
    _drain,
    _events,
    _expire_lease_and_tick,
    _ids,
    _marker,
    _results,
    _run_until,
    _source_checked,
    _tick,
    _unique_rows,
    _writes,
)
from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from task_run_helpers import (
    _SLOW_SQL,
    World,
    _attempts,
    _cancel_and_wait,
    _claim,
    _engine,
    _exists,
    _fake_run_one,
    _log,
    _protect,
    _set_cancel_requested,
    _start,
    _task,
)

import lakana_worker.checks as checks_module
import lakana_worker.executor as executor_module
import lakana_worker.memory as memory_module
import lakana_worker.task_checks as task_checks_module
import lakana_worker.tasks as tasks_module
from lakana_core.auth import (
    AllowAllAuthorization,
    PrincipalRef,
    RunContext,
)
from lakana_core.catalog.service import (
    CatalogService,
    Rollback,
)
from lakana_core.errors import (
    ConflictError,
    PermissionDenied,
)
from lakana_core.identifiers import TableRef
from lakana_core.models import (
    AttemptOutcome,
    AuditEventType,
    CheckOutcome,
    JobVersion,
    QueryRun,
    QueryRunStatus,
    RollbackOutcome,
    Run,
    TaskRun,
    TaskStatus,
)
from lakana_core.notifications.payload import facts_for_run
from lakana_core.projects.checks import CheckSpec
from lakana_core.runs.checks import (
    CHECKS_UNFINISHED_MESSAGE,
    MAX_CHECK_CLAIMS,
    RUN_CANCELLED_MESSAGE,
    RUN_STOPPED_MESSAGE,
    SET_BACK_DONE,
    mark_rollback_failed,
    record_result,
    set_back_failed_message,
)
from lakana_core.runs.lockdown import SUPERSEDED_MESSAGE
from lakana_core.runs.tasks import (
    FailureKind,
    Written,
    fail_run,
)
from lakana_core.storage.resolver import StorageResolver
from lakana_worker.activity import ACTIVITY
from lakana_worker.executor import (
    OutOfMemoryQuery,
    QueryCancelled,
    QueryTimeout,
    TransientQueryError,
)
from lakana_worker.query_process import REGISTRY
from lakana_worker.task_outcome import (
    RUN_ERROR_MESSAGE,
    TaskOutcome,
)
from lakana_worker.tasks import run_claimed


def _still_unchecked(run_id: uuid.UUID) -> str:
    """Build what follows the failure of an attempt that could not check a written table.

    Parameters
    ----------
    run_id
        The run.

    Returns
    -------
    str
        The sentences.
    """
    return (
        "The table holds data of this run that is not checked yet; the checks are tried again. "
        f"To stop: `lakana runs cancel {run_id} --env dev`."
    )


def _die_in_the_check_phase(monkeypatch: pytest.MonkeyPatch) -> threading.Event:
    """Make the worker die on its first entry into the check phase, and only then.

    Parameters
    ----------
    monkeypatch
        pytest's patcher.

    Returns
    -------
    threading.Event
        Set once the worker died there.
    """
    died = threading.Event()
    real = tasks_module.check_phase

    def dying(*args: object) -> TaskOutcome | None:
        if died.is_set():
            return real(*args)  # ty: ignore[invalid-argument-type]
        died.set()
        raise SystemExit("the worker died after the write")

    monkeypatch.setattr(tasks_module, "check_phase", dying)
    return died


def _written_and_lost(world: World, monkeypatch: pytest.MonkeyPatch) -> uuid.UUID:
    """Build `silver.orders`, lose the worker before its checks, and make the task ready again.

    Parameters
    ----------
    world
        The world, deployed by `_checked`.
    monkeypatch
        pytest's patcher.

    Returns
    -------
    uuid.UUID
        The run's id.
    """
    run_id = _start(world)
    claimed = _run_until(world, "silver.orders")
    died = _die_in_the_check_phase(monkeypatch)
    with pytest.raises(SystemExit):
        run_claimed(_engine(world), claimed)
    assert died.is_set()
    assert _task(world, "silver.orders").written_table_id is not None
    _expire_lease_and_tick(world, "silver.orders")
    return run_id


@pytest.mark.slow
def test_a_replaced_table_is_set_back(world: World) -> None:
    """Spec section 6, first row: the table reads what it read before the run."""
    _built_once_then_redeployed(world, _check("unique_id", _DUPLICATE_IDS))
    run_id = _start(world)
    _drain(world)

    task = _task(world, "silver.orders")
    assert (task.status, task.error_message) == (
        TaskStatus.FAILED,
        "Check `silver.orders/unique_id` failed: 1 row (checks/silver/orders/unique_id.sql). "
        "The table was set back to what it was before this run, and nothing downstream was "
        f"built. {_unique_rows(run_id)}{_after(run_id)}",
    )
    (result,) = _results(world, task.id)
    assert result.rollback_outcome is RollbackOutcome.ROLLED_BACK
    assert _ids(world, "silver.orders") == [1, 1, 2]
    assert _marker(world, "silver.orders") is None
    assert _ids(world, "gold.report") == [1, 1, 2]  # the first run's, not built again
    assert _task(world, "gold.report").status is TaskStatus.UPSTREAM_FAILED


def _transient(
    session: Session, query_run_id: uuid.UUID, *args: object, **kwargs: object
) -> BaseException | None:
    """Stand in for a count whose storage failed: the run is `FAILED`, the failure transient.

    Parameters
    ----------
    session
        Database session.
    query_run_id
        The count's query run.
    *args
        Ignored.
    **kwargs
        Ignored.

    Returns
    -------
    TransientQueryError
        The failure, with a text that must never be shown.
    """
    run = session.get(QueryRun, query_run_id)
    assert run is not None
    run.status = QueryRunStatus.FAILED
    run.error_message = "Query failed: IO Error: could not read s3://bucket/Alice"
    run.error_cause = "internal"
    session.commit()
    return TransientQueryError("SECRET location")


def test_a_transient_failure_of_a_count_retries_the_checks_only(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec section 5: the next attempt finds the task written and runs the checks only."""
    _checked(world, _check("unique_id", _DUPLICATE_IDS))
    run_id = _start(world)
    claimed = _run_until(world, "silver.orders")
    _counting(monkeypatch, _transient)

    run_claimed(_engine(world), claimed)

    task = _task(world, "silver.orders")
    message = (
        "Check `silver.orders/unique_id` could not be counted right now (internal); the checks "
        "run again on the next attempt. The table is not built again."
    )
    assert (task.status, task.attempt, task.error_message) == (TaskStatus.PENDING, 1, message)
    assert task.written_table_id is not None
    assert _results(world, task.id) == []
    assert _ids(world, "silver.orders") == [1, 1, 2]
    (first,) = _attempts(world, task.id)
    assert (first.outcome, first.error_message) == (AttemptOutcome.FAILED, message)
    assert (task.failure_code, first.failure_code) == ("check_error", "check_error")

    monkeypatch.setattr(tasks_module, "_dispatch", lambda *a, **k: pytest.fail("built again"))
    _drain(world)

    task = _task(world, "silver.orders")
    assert (task.status, task.attempt) == (TaskStatus.FAILED, 2)
    assert task.failure_code == "check_blocked"
    assert task.error_message is not None and task.error_message.startswith(
        "Check `silver.orders/unique_id` failed: 1 row"
    )
    assert len(_writes(world, task.id)) == 1
    assert [a.outcome for a in _attempts(world, task.id)] == [
        AttemptOutcome.FAILED,
        AttemptOutcome.FAILED,
    ]
    assert _marker(world, "silver.orders") == run_id


def test_a_transient_failure_on_the_last_attempt_counts_as_errored(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No attempt is left: the check could not run, so the table is not published."""
    _checked(world, _check("unique_id", _DUPLICATE_IDS), retries=0)
    run_id = _start(world)
    claimed = _run_until(world, "silver.orders")
    _counting(monkeypatch, _transient)

    run_claimed(_engine(world), claimed)

    task = _task(world, "silver.orders")
    (result,) = _results(world, task.id)
    error = f"the check's query failed. {_SEE_YOURSELF}"
    assert (result.outcome, result.error, result.rollback_outcome) == (
        CheckOutcome.ERRORED,
        error,
        RollbackOutcome.UNPUBLISHED,
    )
    assert (task.status, task.attempt, task.max_attempts) == (TaskStatus.FAILED, 1, 1)
    assert task.failure_code == "check_error"
    assert task.error_message is not None
    assert "Alice" not in task.error_message and "SECRET" not in task.error_message
    assert _marker(world, "silver.orders") == run_id


def test_a_claim_of_a_written_task_runs_the_checks_and_never_the_build(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The next claim checks; it does not build."""
    _checked(world, _check("unique_id", _DUPLICATE_IDS))
    run_id = _written_and_lost(world, monkeypatch)

    monkeypatch.setattr(tasks_module, "_dispatch", lambda *a, **k: pytest.fail("built again"))
    claimed = _claim_ready(world)
    assert claimed is not None and claimed.asset == "silver.orders"
    run_claimed(_engine(world), claimed)

    task = _task(world, "silver.orders")
    assert task.status is TaskStatus.FAILED
    assert task.error_message == (
        "Check `silver.orders/unique_id` failed: 1 row (checks/silver/orders/unique_id.sql). "
        f"{_UNPUBLISHED} {_unique_rows(run_id)}{_after(run_id)}"
    )
    assert [a.outcome for a in _attempts(world, task.id)] == [
        AttemptOutcome.LOST,
        AttemptOutcome.FAILED,
    ]
    assert len(_writes(world, task.id)) == 1
    assert _marker(world, "silver.orders") == run_id


def test_a_claim_of_a_written_task_whose_checks_pass_succeeds_without_building(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    _checked(world, _check("no_nulls", "SELECT id FROM silver.orders WHERE id IS NULL"))
    _written_and_lost(world, monkeypatch)

    monkeypatch.setattr(tasks_module, "_dispatch", lambda *a, **k: pytest.fail("built again"))
    claimed = _claim_ready(world)
    assert claimed is not None
    run_claimed(_engine(world), claimed)

    task = _task(world, "silver.orders")
    assert (task.status, task.error_message) == (TaskStatus.SUCCEEDED, None)
    assert len(_writes(world, task.id)) == 1
    assert _ids(world, "silver.orders") == [1, 1, 2]


def test_a_written_table_changed_by_someone_else_is_checked_as_it_is_and_left_alone(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec section 9: the checks see the table as it is now; a failure discards nobody's rows."""
    _checked(world, _check("unique_id", _DUPLICATE_IDS))
    run_id = _written_and_lost(world, monkeypatch)
    with world.session() as session:
        CatalogService(session, StorageResolver(session), AllowAllAuthorization()).append(
            world.admin,
            TableRef.from_dotted("dev", "silver.orders"),
            pa.table({"id": pa.array([99], pa.int64())}),
        )
    assert _ids(world, "silver.orders") == [1, 1, 2, 99]

    claimed = _claim_ready(world)
    assert claimed is not None
    run_claimed(_engine(world), claimed)

    task = _task(world, "silver.orders")
    assert (task.status, task.error_message) == (
        TaskStatus.FAILED,
        "Check `silver.orders/unique_id` failed: 1 row (checks/silver/orders/unique_id.sql). "
        "The table was left as it is, because another writer changed it while this run was "
        "working on it; it may hold data of this run that failed its checks. Nothing downstream "
        f"was built. {_unique_rows(run_id)}{_after(run_id)}",
    )
    (result,) = _results(world, task.id)
    assert result.rollback_outcome is RollbackOutcome.CHANGED_SINCE
    assert result.checked_snapshot_id != task.written_snapshot_after
    assert _ids(world, "silver.orders") == [1, 1, 2, 99]
    assert _marker(world, "silver.orders") is None


def _set_back_by_the_next_worker_and_cancelled(world: World) -> TaskRun:
    """Check that the task waits for a set-back claim, and run that claim.

    The claim ends the task cancelled and sets the table back: the table was built by this run
    for the first time, so it is not published afterwards, and one rollback is audited.

    A live worker that saw the cancel after the task's write handed the task back: ready,
    `setting_back`. The next worker sets the table back and ends the task cancelled.

    Parameters
    ----------
    world
        The world.

    Returns
    -------
    TaskRun
        The task `silver.orders`, after the set-back claim.
    """
    handed = _task(world, "silver.orders")
    assert (handed.status, handed.setting_back) == (TaskStatus.READY, True)
    again = _claim(world)
    assert again.setting_back
    run_claimed(_engine(world), again)
    done = _task(world, "silver.orders")
    assert done.status is TaskStatus.CANCELLED
    assert done.error_message is not None and done.error_message.startswith(RUN_CANCELLED_MESSAGE)
    # The run built the table for the first time, so setting it back unpublishes it.
    assert _marker(world, "silver.orders") is not None
    assert len(_events(world, AuditEventType.TABLE_ROLLED_BACK)) == 1
    return done


def test_a_cancel_during_the_checks_hands_the_task_back_to_have_its_table_set_back(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The table kept what the run wrote: the next worker sets it back, then cancels."""
    _checked(world, _check("unique_id", _DUPLICATE_IDS))
    run_id = _start(world)
    claimed = _run_until(world, "silver.orders")
    _counting(
        monkeypatch,
        _fake_run_one(QueryCancelled("The query was cancelled."), _cancel_and_wait(world, run_id)),
    )

    run_claimed(_engine(world), claimed)

    task = _task(world, "silver.orders")
    assert (task.status, task.setting_back) == (TaskStatus.READY, True)
    assert [a.outcome for a in _attempts(world, task.id)] == [AttemptOutcome.CANCELLED]
    assert (task.failure_code, _attempts(world, task.id)[0].failure_code) == (
        "cancelled",
        "cancelled",
    )
    assert _results(world, task.id) == []
    assert _ids(world, "silver.orders") == [1, 1, 2]  # still there until the set-back claim
    assert _marker(world, "silver.orders") is None

    task = _set_back_by_the_next_worker_and_cancelled(world)
    assert [a.outcome for a in _attempts(world, task.id)] == [
        AttemptOutcome.CANCELLED,
        AttemptOutcome.CANCELLED,
    ]
    assert _results(world, task.id) == []
    assert len(_events(world, AuditEventType.TABLE_ROLLED_BACK)) == 1


def test_a_lost_lease_during_the_checks_records_nothing(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The claim moved on while a count ran: no result, no rollback, no outcome."""
    _checked(world, _check("unique_id", _DUPLICATE_IDS))
    _start(world)
    claimed = _run_until(world, "silver.orders")

    def steal() -> None:
        with world.session() as session:
            session.execute(
                update(TaskRun).where(TaskRun.id == claimed.task_run_id).values(lease_owner="thief")
            )
            session.commit()

    _counting(monkeypatch, _fake_run_one(None, steal, succeed=True))
    monkeypatch.setattr(
        CatalogService, "roll_back", lambda *a, **k: pytest.fail("a lost claim rolled back")
    )
    # The claim is looked for in the database before anything is decided: nothing is even tried.
    monkeypatch.setattr(
        tasks_module, "record_attempt", lambda *a, **k: pytest.fail("a lost claim was recorded")
    )

    run_claimed(_engine(world), claimed)

    task = _task(world, "silver.orders")
    assert (task.status, task.lease_owner) == (TaskStatus.RUNNING, "thief")
    assert _attempts(world, task.id)[0].finished_at is None
    assert _results(world, task.id) == []
    assert _ids(world, "silver.orders") == [1, 1, 2]
    assert not any(t.name == "lakana-lease" for t in threading.enumerate())


def test_a_drain_during_the_checks_releases_the_task_uncounted(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stamp stays, so the next claim runs the checks and not the build."""
    _checked(world, _check("unique_id", _DUPLICATE_IDS))
    _start(world)
    drain = threading.Event()
    engine = _engine(world, drain=drain, grace=0.0)
    claimed = _run_until(world, "silver.orders", engine)

    def drain_and_wait() -> None:
        drain.set()
        event = executor_module._CANCEL.get()
        assert event is not None
        assert event.wait(5)

    _counting(monkeypatch, _fake_run_one(QueryCancelled("stopped"), drain_and_wait))

    run_claimed(engine, claimed)

    task = _task(world, "silver.orders")
    assert (task.status, task.attempt, task.lease_owner) == (TaskStatus.PENDING, 0, None)
    assert task.written_table_id is not None
    assert [a.outcome for a in _attempts(world, task.id)] == [AttemptOutcome.RELEASED]
    assert _results(world, task.id) == []
    assert _ids(world, "silver.orders") == [1, 1, 2]


def test_a_cancel_requested_before_the_checks_start_is_honoured_without_a_count(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The write landed and then the run was cancelled: the table is set back, then cancelled."""
    _checked(world, _check("unique_id", _DUPLICATE_IDS))
    run_id = _start(world)
    claimed = _run_until(world, "silver.orders")
    real = tasks_module._dispatch

    def build_then_cancel(*args: object, **kwargs: object) -> TaskOutcome | None:
        outcome = real(*args, **kwargs)  # ty: ignore[invalid-argument-type]
        _set_cancel_requested(world, run_id)
        return outcome

    monkeypatch.setattr(tasks_module, "_dispatch", build_then_cancel)
    seen = _counting(monkeypatch)

    run_claimed(_engine(world), claimed)

    assert seen == []
    assert _ids(world, "silver.orders") == [1, 1, 2]  # until the set-back claim
    _set_back_by_the_next_worker_and_cancelled(world)


def test_a_rollback_refused_because_the_claim_is_gone_records_nothing(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale claim is not a failed rollback. Stop, record nothing."""
    _checked(world, _check("unique_id", _DUPLICATE_IDS))
    _start(world)
    claimed = _run_until(world, "silver.orders")

    def stale(self: CatalogService, *args: object, **kwargs: object) -> Rollback:
        with world.session() as session:
            session.execute(
                update(TaskRun)
                .where(TaskRun.id == claimed.task_run_id)
                .values(claim_count=TaskRun.claim_count + 1)
            )
            session.commit()
        raise PermissionDenied(SUPERSEDED_MESSAGE)

    monkeypatch.setattr(CatalogService, "roll_back", stale)
    monkeypatch.setattr(
        task_checks_module,
        "mark_rollback_failed",
        lambda *a, **k: pytest.fail("a stale claim noted a failed rollback"),
    )

    run_claimed(_engine(world), claimed)

    task = _task(world, "silver.orders")
    assert (task.status, task.error_message) == (TaskStatus.RUNNING, None)
    assert _attempts(world, task.id)[0].finished_at is None
    (result,) = _results(world, task.id)
    assert result.rollback_outcome is None


def test_a_rollback_refused_because_the_run_was_cancelled_cancels_the_task(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real guard refuses once the run is flagged: the task is handed back to be set back."""
    _checked(world, _check("unique_id", _DUPLICATE_IDS))
    run_id = _start(world)
    claimed = _run_until(world, "silver.orders")
    real = CatalogService.roll_back

    def cancelled_first(self: CatalogService, *args: object, **kwargs: object) -> Rollback:
        _set_cancel_requested(world, run_id)
        return real(self, *args, **kwargs)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(CatalogService, "roll_back", cancelled_first)

    run_claimed(_engine(world, renew=60), claimed)

    task = _task(world, "silver.orders")
    assert (task.status, task.setting_back) == (TaskStatus.READY, True)
    (result,) = _results(world, task.id)
    assert result.rollback_outcome is None
    assert _ids(world, "silver.orders") == [1, 1, 2]
    assert _marker(world, "silver.orders") is None
    assert _events(world, AuditEventType.TABLE_ROLLED_BACK) == []
    _set_back_by_the_next_worker_and_cancelled(world)


@pytest.mark.slow
def test_the_rollback_runs_under_the_claims_run_context(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The claim form, of this claim, also where only runs may write."""
    _built_once_then_redeployed(world, _check("unique_id", _DUPLICATE_IDS))
    _start(world)
    _protect(world)
    claimed = _run_until(world, "silver.orders")
    seen: list[tuple[PrincipalRef, dict[str, Any]]] = []
    real = CatalogService.roll_back

    def spying(self: CatalogService, p: PrincipalRef, *args: object, **kwargs: object) -> Rollback:
        seen.append((p, kwargs))
        return real(self, p, *args, **kwargs)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(CatalogService, "roll_back", spying)

    run_claimed(_engine(world), claimed)

    ((principal, kwargs),) = seen
    assert principal.run_context == RunContext(
        task_run_id=claimed.task_run_id, query_run_id=None, claim=claimed.claim
    )
    assert (principal.id, principal.name) == (world.admin.id, "admin")
    assert (kwargs["run_id"], kwargs["task_run_id"]) == (claimed.run_id, claimed.task_run_id)
    assert kwargs["failed_checks"] == ["silver.orders/unique_id"]
    task = _task(world, "silver.orders")
    (result,) = _results(world, task.id)
    assert result.rollback_outcome is RollbackOutcome.ROLLED_BACK
    assert _ids(world, "silver.orders") == [1, 1, 2]
    assert _events(world, AuditEventType.PROTECTED_WRITE_DENIED) == []


@pytest.mark.slow
def test_without_the_run_context_a_protected_environment_would_refuse_the_rollback(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control for the test above: the context is what lets the rollback through."""
    _built_once_then_redeployed(world, _check("unique_id", _DUPLICATE_IDS))
    run_id = _start(world)
    _protect(world)
    claimed = _run_until(world, "silver.orders")
    real = CatalogService.roll_back

    def stripped(
        self: CatalogService, p: PrincipalRef, *args: object, **kwargs: object
    ) -> Rollback:
        bare = PrincipalRef(id=p.id, name=p.name, type=p.type)
        return real(self, bare, *args, **kwargs)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(CatalogService, "roll_back", stripped)

    run_claimed(_engine(world), claimed)

    task = _task(world, "silver.orders")
    assert task.error_message == (
        "Check `silver.orders/unique_id` failed: 1 row (checks/silver/orders/unique_id.sql). "
        "The rollback was refused (permission denied: Environment 'dev' is protected: only "
        "runs of the deployed project version may write. See `lakana run`.); it did not change the "
        f"table. Nothing downstream was built. {_unique_rows(run_id)}{_after(run_id)}"
    )
    assert _ids(world, "silver.orders") == [11, 11, 12]


@pytest.mark.slow
def test_a_worker_lost_after_the_rollback_ends_the_task_failed_without_building_again(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rollback landed, the worker died before it recorded FAILED."""
    _built_once_then_redeployed(world, _check("unique_id", _DUPLICATE_IDS))
    run_id = _start(world)
    claimed = _run_until(world, "silver.orders")
    real_finish = tasks_module._finish

    def die(*args: object) -> None:
        raise SystemExit("the worker died after the rollback")

    monkeypatch.setattr(tasks_module, "_finish", die)
    with pytest.raises(SystemExit):
        run_claimed(_engine(world), claimed)
    monkeypatch.setattr(tasks_module, "_finish", real_finish)
    lost = _task(world, "silver.orders")
    assert (lost.status, lost.written_table_id) == (TaskStatus.RUNNING, None)
    assert _ids(world, "silver.orders") == [1, 1, 2]  # set back already
    _expire_lease_and_tick(world, "silver.orders")

    monkeypatch.setattr(tasks_module, "_dispatch", lambda *a, **k: pytest.fail("built again"))
    monkeypatch.setattr(tasks_module, "check_phase", lambda *a: pytest.fail("checked again"))
    monkeypatch.setattr(
        CatalogService, "roll_back", lambda *a, **k: pytest.fail("rolled back twice")
    )
    again = _claim_ready(world)
    assert again is not None and again.asset == "silver.orders"
    run_claimed(_engine(world), again)

    task = _task(world, "silver.orders")
    assert (task.status, task.error_message) == (
        TaskStatus.FAILED,
        "Check `silver.orders/unique_id` failed: 1 row (checks/silver/orders/unique_id.sql). "
        "The table was set back to what it was before this run, and nothing downstream was "
        f"built. {_unique_rows(run_id)}{_after(run_id)}",
    )
    assert [a.outcome for a in _attempts(world, task.id)] == [
        AttemptOutcome.LOST,
        AttemptOutcome.FAILED,
    ]
    assert [a.outcome for a in _attempts(world, task.id)] == [
        AttemptOutcome.LOST,
        AttemptOutcome.FAILED,
    ]
    assert [a.failure_code for a in _attempts(world, task.id)] == ["worker_lost", "check_blocked"]
    assert task.failure_code == "check_blocked"
    assert len(_writes(world, task.id)) == 1
    assert len(_events(world, AuditEventType.TABLE_ROLLED_BACK)) == 1
    assert _ids(world, "silver.orders") == [1, 1, 2]
    log = _log(world, run_id, "silver.orders")
    assert "  check unique_id: failed, 1 row\n  rollback: rolled_back\n" in log
    _tick(world)
    assert _task(world, "gold.report").status is TaskStatus.UPSTREAM_FAILED


@pytest.mark.slow
def test_a_set_back_claim_after_a_landed_rollback_ends_the_task_with_the_failed_check(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rollback landed and cleared the stamp; a cancel then sent the task to set back.

    The claim finds the landed rollback in the task's results and ends the task from them, as a
    normal claim would: the failed check is the verdict, and the table was set back already.
    """
    _built_once_then_redeployed(world, _check("unique_id", _DUPLICATE_IDS))
    run_id = _start(world)
    claimed = _run_until(world, "silver.orders")
    real_finish = tasks_module._finish

    def die(*args: object) -> None:
        raise SystemExit("the worker died after the rollback")

    monkeypatch.setattr(tasks_module, "_finish", die)
    with pytest.raises(SystemExit):
        run_claimed(_engine(world), claimed)
    monkeypatch.setattr(tasks_module, "_finish", real_finish)
    _set_cancel_requested(world, run_id)
    _expire_lease_and_tick(world, "silver.orders")
    ready = _task(world, "silver.orders")
    assert (ready.status, ready.setting_back) == (TaskStatus.READY, True)
    _nothing_but_a_set_back(monkeypatch)
    monkeypatch.setattr(
        CatalogService, "roll_back", lambda *a, **k: pytest.fail("rolled back twice")
    )
    again = _claim_ready(world)
    assert again is not None and again.setting_back

    run_claimed(_engine(world), again)

    task = _task(world, "silver.orders")
    assert (task.status, task.error_message) == (
        TaskStatus.FAILED,
        "Check `silver.orders/unique_id` failed: 1 row (checks/silver/orders/unique_id.sql). "
        "The table was set back to what it was before this run, and nothing downstream was "
        f"built. {_unique_rows(run_id)}{_after(run_id)}",
    )
    assert task.failure_code == "check_blocked"
    assert _ids(world, "silver.orders") == [1, 1, 2]
    log = _log(world, run_id, "silver.orders")
    assert "  check unique_id: failed, 1 row\n  rollback: rolled_back\n" in log


@pytest.mark.slow
def test_the_results_of_an_earlier_run_do_not_end_the_task_of_a_new_run(world: World) -> None:
    """Results belong to a task run; a new run builds and checks for itself."""
    _checked(world, _check("unique_id", _DUPLICATE_IDS))
    first = _start(world)
    _drain(world)
    earlier = _task(world, "silver.orders")
    assert earlier.status is TaskStatus.FAILED
    assert [r.rollback_outcome for r in _results(world, earlier.id)] == [
        RollbackOutcome.UNPUBLISHED
    ]
    _checked(world, _check("unique_id", "SELECT id FROM silver.orders WHERE id IS NULL"))

    second = _start(world)
    _drain(world)

    task = _task(world, "silver.orders")
    assert (task.run_id, task.status) == (second, TaskStatus.SUCCEEDED)
    assert task.id != earlier.id and first != second
    assert len(_writes(world, task.id)) == 1
    assert [r.outcome for r in _results(world, task.id)] == [CheckOutcome.PASSED]
    assert _marker(world, "silver.orders") is None
    assert _ids(world, "silver.orders") == [1, 1, 2]


def test_results_without_a_landed_rollback_do_not_stop_an_ordinary_claim(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only `rolled_back` and `unpublished` say that the table was set back."""
    _checked(world, _check("unique_id", _DUPLICATE_IDS))
    run_id = _start(world)
    claimed = _run_until(world, "silver.orders")
    spec = CheckSpec(
        name="silver.orders/unique_id",
        table="silver.orders",
        check="unique_id",
        path="checks/silver/orders/unique_id.sql",
        on_failure="block",
        description=None,
        sql=_DUPLICATE_IDS,
    )
    with world.session() as session:
        assert record_result(
            session,
            task_run_id=claimed.task_run_id,
            worker="w1",
            claim=claimed.claim,
            spec=spec,
            outcome=CheckOutcome.FAILED,
            failing_rows=7,
            error=None,
            table_id=None,
            checked_snapshot_id=None,
            policy_applied=False,
            query_run_id=None,
            started_at=datetime.now(UTC),
        )
        assert mark_rollback_failed(
            session, task_run_id=claimed.task_run_id, worker="w1", claim=claimed.claim
        )

    run_claimed(_engine(world), claimed)

    task = _task(world, "silver.orders")
    assert len(_writes(world, task.id)) == 1  # it built
    (result,) = _results(world, task.id)
    assert (result.failing_rows, result.rollback_outcome) == (1, RollbackOutcome.UNPUBLISHED)
    assert _marker(world, "silver.orders") == run_id


def test_an_unexpected_error_while_counting_one_check_makes_that_check_errored(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No permanent failure without a verdict; the text names the type only."""
    _checked(
        world,
        _check("a_unique", _DUPLICATE_IDS),
        _check("b_no_nulls", "SELECT id FROM silver.orders WHERE id IS NULL"),
    )
    run_id = _start(world)
    claimed = _run_until(world, "silver.orders")

    def explode(*args: object, **kwargs: object) -> BaseException | None:
        raise KeyError("Alice Smith")

    _counting(monkeypatch, explode)

    run_claimed(_engine(world), claimed)

    task = _task(world, "silver.orders")
    error = (
        "the check could not be counted: the worker hit an unexpected error (KeyError). See "
        "the worker's log."
    )
    results = _results(world, task.id)
    assert [(r.outcome, r.error, r.rollback_outcome) for r in results] == [
        (CheckOutcome.ERRORED, error, RollbackOutcome.UNPUBLISHED),
        (CheckOutcome.PASSED, None, None),
    ]
    assert (task.status, task.attempt) == (TaskStatus.FAILED, 1)
    assert task.error_message == (
        f"Check `silver.orders/a_unique` could not run (checks/silver/orders/a_unique.sql): "
        f"{error} Data that could not be checked is not published. {_UNPUBLISHED} "
        f"{_after(run_id)}"
    )
    assert "Alice" not in _log(world, run_id, "silver.orders")
    assert _marker(world, "silver.orders") == run_id


def test_an_unexpected_error_while_counting_under_a_cancel_is_a_cancel(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cancel is one of the two exceptions to "errored"."""
    _checked(world, _check("unique_id", _DUPLICATE_IDS))
    run_id = _start(world)
    claimed = _run_until(world, "silver.orders")

    def explode(*args: object, **kwargs: object) -> BaseException | None:
        _set_cancel_requested(world, run_id)
        raise RuntimeError("the connection went away")

    _counting(monkeypatch, explode)

    run_claimed(_engine(world, renew=60), claimed)

    task = _set_back_by_the_next_worker_and_cancelled(world)
    assert _results(world, task.id) == []


def test_an_unexpected_error_while_counting_for_a_claim_that_is_gone_records_nothing(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale claim is the other exception."""
    _checked(world, _check("unique_id", _DUPLICATE_IDS))
    _start(world)
    claimed = _run_until(world, "silver.orders")

    def explode(*args: object, **kwargs: object) -> BaseException | None:
        with world.session() as session:
            session.execute(
                update(TaskRun).where(TaskRun.id == claimed.task_run_id).values(lease_owner="thief")
            )
            session.commit()
        raise RuntimeError("the connection went away")

    _counting(monkeypatch, explode)
    monkeypatch.setattr(
        task_checks_module,
        "record_result",
        lambda *a, **k: pytest.fail("a lost claim was recorded"),
    )

    run_claimed(_engine(world, renew=60), claimed)

    task = _task(world, "silver.orders")
    assert (task.status, task.lease_owner) == (TaskStatus.RUNNING, "thief")
    assert _results(world, task.id) == []
    assert _ids(world, "silver.orders") == [1, 1, 2]


def test_an_unexpected_error_outside_a_count_is_retried_and_the_next_claim_checks_again(
    world: World, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The phase's own bookkeeping failed. Retry; the stamp makes it checks only."""
    _checked(world, _check("unique_id", _DUPLICATE_IDS), retries=0)
    run_id = _start(world)
    claimed = _run_until(world, "silver.orders")
    real = task_checks_module.record_result

    def explode(*args: object, **kwargs: object) -> bool:
        raise ConnectionError("the database went away with Alice")

    monkeypatch.setattr(task_checks_module, "record_result", explode)
    with caplog.at_level(logging.ERROR, logger="lakana_worker.tasks"):
        run_claimed(_engine(world), claimed)

    task = _task(world, "silver.orders")
    # `retries: 0`: a task that awaits its checks still does not run out of attempts.
    assert (task.status, task.attempt, task.max_attempts) == (TaskStatus.PENDING, 1, 1)
    assert task.error_message == f"{RUN_ERROR_MESSAGE}. {_still_unchecked(run_id)}"
    assert task.failure_code == "internal"
    assert task.written_table_id is not None
    assert _results(world, task.id) == []
    assert _ids(world, "silver.orders") == [1, 1, 2]
    assert any("ConnectionError" in record.getMessage() for record in caplog.records)

    monkeypatch.setattr(task_checks_module, "record_result", real)
    monkeypatch.setattr(tasks_module, "_dispatch", lambda *a, **k: pytest.fail("built again"))
    _drain(world)

    task = _task(world, "silver.orders")
    assert (task.status, task.attempt) == (TaskStatus.FAILED, 2)
    assert len(_writes(world, task.id)) == 1
    assert _marker(world, "silver.orders") == run_id


@pytest.mark.parametrize("missing", ["principal", "manifest"])
def test_what_the_phase_needs_and_cannot_read_is_retried_for_a_written_task(
    world: World, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    """No count is possible, so no verdict; the task waits, it does not fail."""
    _checked(world, _check("unique_id", _DUPLICATE_IDS), retries=0)
    run_id = _start(world)
    claimed = _run_until(world, "silver.orders")
    real = tasks_module._dispatch

    def build_then_break(*args: object, **kwargs: object) -> TaskOutcome | None:
        outcome = real(*args, **kwargs)  # ty: ignore[invalid-argument-type]
        with world.session() as session:
            if missing == "principal":
                session.execute(update(Run).values(run_as_principal_id=None))
            else:
                version = session.get(JobVersion, world.version_id)
                assert version is not None
                version.manifest_json = {**version.manifest_json, "version": 99}
            session.commit()
        return outcome

    monkeypatch.setattr(tasks_module, "_dispatch", build_then_break)

    run_claimed(_engine(world), claimed)

    task = _task(world, "silver.orders")
    assert (task.status, task.attempt, task.max_attempts) == (TaskStatus.PENDING, 1, 1)
    assert task.error_message is not None
    assert task.error_message.startswith(
        "the run principal is deactivated or deleted. The table holds data of this run"
        if missing == "principal"
        else "the project version's manifest has version 99"
    )
    assert task.error_message.endswith(_still_unchecked(run_id))
    assert task.failure_code == ("permission_denied" if missing == "principal" else "no_runtime")
    assert task.written_table_id is not None
    assert _results(world, task.id) == []
    assert _ids(world, "silver.orders") == [1, 1, 2]


def test_a_task_whose_run_vanished_before_the_checks_records_nothing(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    _source_checked(world, "SELECT id FROM bronze.events WHERE id IS NULL")
    _start(world)
    claimed = _claim_ready(world)
    assert claimed is not None
    with world.session() as session:
        session.execute(sqlalchemy.delete(Run).where(Run.id == claimed.run_id))
        session.commit()
    seen = _counting(monkeypatch)

    run_claimed(_engine(world), claimed)

    assert seen == []
    with world.session() as session:
        assert session.scalars(select(TaskRun)).all() == []


def test_a_check_of_a_view_asset_has_nothing_to_set_back(world: World) -> None:
    """Spec section 6, fifth row."""
    _checked(world, _check("unique_id", _DUPLICATE_IDS), first_write_mode="view", write_mode="view")
    run_id = _start(world)
    _drain(world)

    task = _task(world, "silver.orders")
    assert task.error_message == (
        "Check `silver.orders/unique_id` failed: 1 row (checks/silver/orders/unique_id.sql). "
        f"It is a view, so there is nothing to set back; nothing downstream was built. "
        f"{_after(run_id)}"
    )
    (result,) = _results(world, task.id)
    assert (result.rollback_outcome, result.checked_snapshot_id) == (
        RollbackOutcome.NOT_A_TABLE,
        None,
    )
    assert "  rollback: not_a_table\n" in _log(world, run_id, "silver.orders")


def test_a_worker_that_is_stopping_for_memory_decides_nothing_about_the_checks(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The watchdog owns the outcome: no result, no rollback, no attempt recorded here."""
    from lakana_worker.activity import ACTIVITY

    _checked(world, _check("unique_id", _DUPLICATE_IDS))
    _start(world)
    claimed = _run_until(world, "silver.orders")
    monkeypatch.setattr(ACTIVITY, "stopping", False)

    def stopped(*args: object, **kwargs: object) -> BaseException | None:
        ACTIVITY.stopping = True
        return OutOfMemoryQuery("the worker was stopped")

    _counting(monkeypatch, stopped)
    monkeypatch.setattr(
        CatalogService, "roll_back", lambda *a, **k: pytest.fail("a stopping worker rolled back")
    )

    run_claimed(_engine(world), claimed)

    task = _task(world, "silver.orders")
    assert (task.status, task.lease_owner) == (TaskStatus.RUNNING, "w1")
    assert task.written_table_id is not None
    assert _results(world, task.id) == []
    assert _attempts(world, task.id)[0].finished_at is None


def test_a_worker_that_starts_stopping_after_the_verdict_records_no_attempt(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lakana_worker.activity import ACTIVITY

    _checked(world, _check("no_nulls", "SELECT id FROM silver.orders WHERE id IS NULL"))
    _start(world)
    claimed = _run_until(world, "silver.orders")
    monkeypatch.setattr(ACTIVITY, "stopping", False)
    real = tasks_module.check_phase

    def checked_then_stopping(*args: object) -> TaskOutcome | None:
        outcome = real(*args)  # ty: ignore[invalid-argument-type]
        ACTIVITY.stopping = True
        return outcome

    monkeypatch.setattr(tasks_module, "check_phase", checked_then_stopping)

    run_claimed(_engine(world), claimed)

    task = _task(world, "silver.orders")
    assert task.status is TaskStatus.RUNNING
    assert [r.outcome for r in _results(world, task.id)] == [CheckOutcome.PASSED]


@pytest.mark.slow
def test_a_result_that_cannot_be_recorded_because_the_claim_is_gone_ends_the_phase(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    _checked(
        world,
        _check("a_unique", _DUPLICATE_IDS),
        _check("b_no_nulls", "SELECT id FROM silver.orders WHERE id IS NULL"),
    )
    _start(world)
    claimed = _run_until(world, "silver.orders")
    seen = _counting(monkeypatch)
    monkeypatch.setattr(task_checks_module, "record_result", lambda *a, **k: False)
    monkeypatch.setattr(
        CatalogService, "roll_back", lambda *a, **k: pytest.fail("rolled back without a claim")
    )

    run_claimed(_engine(world), claimed)

    assert len(seen) == 1
    task = _task(world, "silver.orders")
    assert (task.status, task.error_message) == (TaskStatus.RUNNING, None)


def test_a_failure_before_the_build_is_decided_is_retried(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reading the stamp or the results failed: nothing was built, and nothing escapes."""
    _checked(world, _check("unique_id", _DUPLICATE_IDS))
    _start(world)
    claimed = _run_until(world, "silver.orders")

    def explode(*args: object) -> BaseException | None:
        raise ConnectionError("the database went away")

    monkeypatch.setattr(task_checks_module, "read_written", explode)
    monkeypatch.setattr(tasks_module, "_dispatch", lambda *a, **k: pytest.fail("built blind"))

    run_claimed(_engine(world), claimed)

    task = _task(world, "silver.orders")
    assert (task.status, task.error_message) == (TaskStatus.PENDING, RUN_ERROR_MESSAGE)
    assert task.failure_code == "internal"
    assert not _exists(world, "silver.orders")


def test_a_written_task_without_its_stamp_reports_that_it_cannot_be_set_back(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Should not happen (the write and its stamp are one transaction); it is said, not hidden."""
    _checked(world, _check("unique_id", _DUPLICATE_IDS))
    run_id = _start(world)
    claimed = _run_until(world, "silver.orders")
    real = tasks_module._dispatch

    def build_and_lose_the_stamp(*args: object, **kwargs: object) -> TaskOutcome | None:
        outcome = real(*args, **kwargs)  # ty: ignore[invalid-argument-type]
        with world.session() as session:
            session.execute(
                update(TaskRun)
                .where(TaskRun.id == claimed.task_run_id)
                .values(
                    written_table_id=None,
                    written_snapshot_before=None,
                    written_snapshot_after=None,
                    written_publishes=None,
                )
            )
            session.commit()
        return outcome

    monkeypatch.setattr(tasks_module, "_dispatch", build_and_lose_the_stamp)
    monkeypatch.setattr(
        CatalogService, "roll_back", lambda *a, **k: pytest.fail("rolled back without a record")
    )

    run_claimed(_engine(world), claimed)

    task = _task(world, "silver.orders")
    assert (task.status, task.error_message) == (
        TaskStatus.FAILED,
        "Check `silver.orders/unique_id` failed: 1 row (checks/silver/orders/unique_id.sql). "
        "The rollback failed (what this run wrote was not recorded), so the table still holds "
        f"this run's data. Nothing downstream was built. {_unique_rows(run_id)}{_after(run_id)}",
    )
    (result,) = _results(world, task.id)
    assert result.rollback_outcome is RollbackOutcome.FAILED
    assert result.table_id is not None
    assert _ids(world, "silver.orders") == [1, 1, 2]


def test_a_conflict_while_counting_is_retried_like_a_transient_failure(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    _checked(world, _check("unique_id", _DUPLICATE_IDS))
    _start(world)
    claimed = _run_until(world, "silver.orders")
    _counting(monkeypatch, _fake_run_one(ConflictError("changed"), error_message="x"))

    run_claimed(_engine(world), claimed)

    task = _task(world, "silver.orders")
    assert (task.status, task.attempt, task.conflict_retried) == (TaskStatus.PENDING, 1, False)
    assert task.error_message is not None
    assert task.error_message.startswith("Check `silver.orders/unique_id` could not be counted")
    assert "(internal)" in task.error_message


def test_a_count_that_timed_out_is_an_errored_check_not_a_retry(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec section 5: the time limit makes the check `errored`; attempts are left unused."""
    _checked(world, _check("unique_id", _DUPLICATE_IDS, on_failure="warn"))
    _start(world)
    claimed = _run_until(world, "silver.orders")

    def timed_out(
        session: Session, query_run_id: uuid.UUID, *args: object, **kwargs: object
    ) -> BaseException | None:
        run = session.get(QueryRun, query_run_id)
        assert run is not None
        run.status, run.error_cause = QueryRunStatus.FAILED, "timeout"
        run.error_message = "Query exceeded the time limit"
        session.commit()
        return QueryTimeout("Query exceeded the time limit")

    _counting(monkeypatch, timed_out)

    run_claimed(_engine(world), claimed)

    task = _task(world, "silver.orders")
    (result,) = _results(world, task.id)
    assert (result.outcome, result.error) == (
        CheckOutcome.ERRORED,
        f"the check's query failed: it ran longer than the time limit of a query. {_SEE_YOURSELF}",
    )
    assert (task.status, task.attempt) == (TaskStatus.SUCCEEDED, 1)


@pytest.mark.slow
def test_a_rollback_that_raises_is_retried_by_the_next_claim_which_checks_again(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An error of the phase's own is retried; the stamp is still there."""
    _built_once_then_redeployed(world, _check("unique_id", _DUPLICATE_IDS), retries=0)
    run_id = _start(world)
    claimed = _run_until(world, "silver.orders")
    real = CatalogService.roll_back
    calls: list[int] = []

    def breaks_once(self: CatalogService, *args: object, **kwargs: object) -> Rollback:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("the catalog broke before it committed anything")
        return real(self, *args, **kwargs)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(CatalogService, "roll_back", breaks_once)

    run_claimed(_engine(world), claimed)

    task = _task(world, "silver.orders")
    assert (task.status, task.error_message) == (
        TaskStatus.PENDING,
        f"{RUN_ERROR_MESSAGE}. {_still_unchecked(run_id)}",
    )
    assert task.written_table_id is not None
    assert _ids(world, "silver.orders") == [11, 11, 12]

    monkeypatch.setattr(tasks_module, "_dispatch", lambda *a, **k: pytest.fail("built again"))
    _drain(world)

    task = _task(world, "silver.orders")
    assert task.status is TaskStatus.FAILED
    assert task.error_message == (
        "Check `silver.orders/unique_id` failed: 1 row (checks/silver/orders/unique_id.sql). "
        "The table was set back to what it was before this run, and nothing downstream was "
        f"built. {_unique_rows(run_id)}{_after(run_id)}"
    )
    assert len(calls) == 2
    assert _ids(world, "silver.orders") == [1, 1, 2]
    assert len(_writes(world, task.id)) == 1


def _database_error(*args: object, **kwargs: object) -> BaseException | None:
    """Stand in for a count whose own bookkeeping lost the database.

    Parameters
    ----------
    *args
        Ignored.
    **kwargs
        Ignored.

    Raises
    ------
    OperationalError
        Always, with a text that must never be shown.
    """
    raise OperationalError("INSERT INTO query_runs", {}, Exception("Alice went away"))


def test_a_database_error_while_counting_is_retried_while_attempts_are_left(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A blip of the database is not a failed check; good data is not rolled back."""
    _checked(world, _check("no_nulls", "SELECT id FROM silver.orders WHERE id IS NULL"))
    run_id = _start(world)
    claimed = _run_until(world, "silver.orders")
    _counting(monkeypatch, _database_error)
    monkeypatch.setattr(
        CatalogService, "roll_back", lambda *a, **k: pytest.fail("rolled back on a blip")
    )

    run_claimed(_engine(world), claimed)

    task = _task(world, "silver.orders")
    assert (task.status, task.attempt, task.error_message) == (
        TaskStatus.PENDING,
        1,
        "Check `silver.orders/no_nulls` could not be counted right now (internal); the checks "
        "run again on the next attempt. The table is not built again.",
    )
    assert _results(world, task.id) == []
    assert task.written_table_id is not None

    real_dispatch = tasks_module._dispatch
    monkeypatch.setattr(tasks_module, "_dispatch", lambda *a, **k: pytest.fail("built again"))
    again = _claim_ready(world)
    assert again is not None and again.asset == "silver.orders"
    run_claimed(_engine(world), again)
    monkeypatch.setattr(tasks_module, "_dispatch", real_dispatch)

    task = _task(world, "silver.orders")
    assert (task.status, task.attempt, task.run_id) == (TaskStatus.SUCCEEDED, 2, run_id)
    assert _ids(world, "silver.orders") == [1, 1, 2]


def test_a_database_error_while_counting_on_the_last_attempt_is_an_errored_check(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The last-attempt rule stays: fail closed, bounded; the text names the type only."""
    _checked(world, _check("no_nulls", "SELECT id FROM silver.orders WHERE id IS NULL"), retries=0)
    run_id = _start(world)
    claimed = _run_until(world, "silver.orders")
    _counting(monkeypatch, _database_error)

    run_claimed(_engine(world), claimed)

    task = _task(world, "silver.orders")
    (result,) = _results(world, task.id)
    assert (result.outcome, result.error, result.rollback_outcome) == (
        CheckOutcome.ERRORED,
        "the check could not be counted: the worker hit an unexpected error (OperationalError). "
        "See the worker's log.",
        RollbackOutcome.UNPUBLISHED,
    )
    assert (task.status, task.attempt) == (TaskStatus.FAILED, 1)
    assert task.error_message is not None and "Alice" not in task.error_message
    assert _marker(world, "silver.orders") == run_id


@pytest.mark.slow
def test_a_rollback_refused_after_the_catalog_found_nothing_to_set_back_does_not_blame_the_table(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real catalog decides `nothing_written`, then its guard refuses the record."""
    _built_once_then_redeployed(
        world,
        _check("unique_id", _DUPLICATE_IDS),
        sql="SELECT id FROM bronze.src WHERE id > 100",
        write_mode="append",
    )
    run_id = _start(world)
    claimed = _run_until(world, "silver.orders")
    real = CatalogService._roll_back_once
    decided: list[Rollback] = []

    def decide_then_lose_the_grant(
        self: CatalogService, *args: object, **kwargs: object
    ) -> Rollback:
        decided.append(real(self, *args, **kwargs))  # ty: ignore[invalid-argument-type]
        monkeypatch.setattr(
            self._guard,
            "check",
            lambda *a, **k: (_ for _ in ()).throw(PermissionDenied("no longer allowed")),
        )
        return decided[-1]

    monkeypatch.setattr(CatalogService, "_roll_back_once", decide_then_lose_the_grant)

    run_claimed(_engine(world), claimed)

    assert [r.outcome for r in decided] == [RollbackOutcome.NOTHING_WRITTEN]
    task = _task(world, "silver.orders")
    assert (task.status, task.error_message) == (
        TaskStatus.FAILED,
        "Check `silver.orders/unique_id` failed: 1 row (checks/silver/orders/unique_id.sql). "
        "The rollback was refused (permission denied: no longer allowed); it did not change the "
        f"table. Nothing downstream was built. {_unique_rows(run_id)}{_after(run_id)}",
    )
    assert "still holds" not in (task.error_message or "")
    assert _ids(world, "silver.orders") == [1, 1, 2]


def test_a_retryable_failure_reported_for_a_build_whose_write_landed_waits_for_the_next_claim(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stamp is there: the failure is recorded as it is, and the next claim only checks."""
    _checked(world, _check("unique_id", _DUPLICATE_IDS), retries=0)
    run_id = _start(world)
    claimed = _run_until(world, "silver.orders")
    real = tasks_module._dispatch

    def built_and_then_failed(*args: object, **kwargs: object) -> TaskOutcome | None:
        outcome = real(*args, **kwargs)  # ty: ignore[invalid-argument-type]
        assert outcome is not None and outcome.kind is None
        return TaskOutcome(FailureKind.TRANSIENT, "the connection dropped", outcome.environment)

    monkeypatch.setattr(tasks_module, "_dispatch", built_and_then_failed)
    seen = _counting(monkeypatch)

    run_claimed(_engine(world), claimed)

    task = _task(world, "silver.orders")
    assert seen == []
    assert (task.status, task.error_message) == (TaskStatus.PENDING, "the connection dropped")
    assert task.written_table_id is not None

    monkeypatch.setattr(tasks_module, "_dispatch", lambda *a, **k: pytest.fail("built again"))
    _drain(world)

    task = _task(world, "silver.orders")
    assert task.status is TaskStatus.FAILED
    assert _marker(world, "silver.orders") == run_id
    assert len(_writes(world, task.id)) == 1


def test_a_stamped_build_reported_failed_for_good_says_its_data_is_unchecked_when_retried(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The word "unchecked" follows what the phase found (a stamp), not what the build said."""
    _checked(world, _check("unique_id", _DUPLICATE_IDS), retries=0)
    run_id = _start(world)
    claimed = _run_until(world, "silver.orders")
    real = tasks_module._dispatch

    def built_and_then_failed(*args: object, **kwargs: object) -> TaskOutcome | None:
        outcome = real(*args, **kwargs)  # ty: ignore[invalid-argument-type]
        assert outcome is not None and outcome.kind is None
        return TaskOutcome(FailureKind.PERMANENT, "recording the write failed", outcome.environment)

    def explode(*args: object, **kwargs: object) -> bool:
        raise ConnectionError("the database went away")

    monkeypatch.setattr(tasks_module, "_dispatch", built_and_then_failed)
    monkeypatch.setattr(task_checks_module, "record_result", explode)

    run_claimed(_engine(world), claimed)

    task = _task(world, "silver.orders")
    assert (task.status, task.error_message) == (
        TaskStatus.PENDING,
        f"{RUN_ERROR_MESSAGE}. {_still_unchecked(run_id)}",
    )
    assert task.written_table_id is not None


def test_an_internal_error_of_a_source_check_makes_no_claim_about_a_table(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A source check wrote nothing: its retry message does not speak of unchecked data."""
    _source_checked(world, "SELECT id FROM bronze.events WHERE id IS NULL")
    _start(world)
    claimed = _claim_ready(world)
    assert claimed is not None and claimed.kind == "source_check"

    def explode(*args: object, **kwargs: object) -> bool:
        raise ConnectionError("the database went away")

    monkeypatch.setattr(task_checks_module, "record_result", explode)

    run_claimed(_engine(world), claimed)

    task = _task(world, "bronze.events")
    assert (task.status, task.error_message) == (TaskStatus.PENDING, RUN_ERROR_MESSAGE)


def test_an_internal_error_while_a_failed_build_is_looked_at_makes_no_claim_about_a_table(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing is known to be in the table yet: the plain internal error, and a retry."""
    _checked(world, _check("unique_id", _DUPLICATE_IDS), sql="SELECT nope FROM bronze.src")
    _start(world)
    claimed = _run_until(world, "silver.orders")
    real = task_checks_module.read_written
    calls: list[int] = []

    def breaks_in_the_phase(*args: object) -> Written | None:
        calls.append(1)
        if len(calls) == 1:  # the read before the build
            return real(*args)  # ty: ignore[invalid-argument-type]
        raise ConnectionError("the database went away")

    monkeypatch.setattr(task_checks_module, "read_written", breaks_in_the_phase)

    run_claimed(_engine(world), claimed)

    task = _task(world, "silver.orders")
    assert (task.status, task.error_message) == (TaskStatus.PENDING, RUN_ERROR_MESSAGE)


# -- memory while a check is counted -----------------------

_NO_BIG_ID = "SELECT id FROM silver.orders WHERE id > 99"
"""A check of `silver.orders` that passes in `_checked`'s world."""


_SLOW_COUNT = (
    "SELECT count(*) AS n FROM (SELECT o.id FROM silver.orders AS o, "
    f"({_SLOW_SQL}) AS q WHERE q.n IS NULL) AS lakana_check"
)
"""The count of a check that reads `silver.orders`, finds nothing, and runs for seconds."""


_FOUR_GB = memory_module.Limit(4 * 1024**3, "4GB", "x")


def _counting_slowly(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> tuple[threading.Thread, TaskRun, QueryRun]:
    """Build `silver.orders` and leave the worker inside the real count of its one check.

    The first count runs `_SLOW_COUNT` instead of the check's own count, in the real query
    child; every later count is the check's own. The worker runs in a thread, as the main
    thread of a worker process does while the watchdog's thread measures.

    Parameters
    ----------
    world
        The world, deployed by `_checked` with one check.
    monkeypatch
        Replaces the SQL of the first count, and restores `ACTIVITY.stopping` afterwards.

    Returns
    -------
    tuple
        The worker's thread, the task run (stamped *written*) and the count's query run,
        which is `RUNNING` in a live query child and registered as in flight.
    """
    real = checks_module.count_query
    slow = [_SLOW_COUNT]

    def count_query(sql: str) -> str:
        """Hand out the slow count once, then the real one.

        Parameters
        ----------
        sql
            The check's SQL.

        Returns
        -------
        str
            The count to run.
        """
        return slow.pop() if slow else real(sql)

    monkeypatch.setattr(checks_module, "count_query", count_query)
    monkeypatch.setattr(ACTIVITY, "stopping", False)
    _start(world)
    claimed = _run_until(world, "silver.orders")
    thread = threading.Thread(target=run_claimed, args=(_engine(world), claimed), daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while True:
        task = _task(world, "silver.orders")
        counts = _counts(world, task.id)
        if (
            counts
            and counts[0].status is QueryRunStatus.RUNNING
            and counts[0].id in ACTIVITY.snapshot()[0]
            and REGISTRY.pids()
        ):
            break
        assert thread.is_alive(), "the worker ended before it counted"
        assert time.monotonic() < deadline, "the count never started"
        time.sleep(0.01)
    assert (task.status, task.written_table_id is not None) == (TaskStatus.RUNNING, True)
    return thread, task, counts[0]


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="measures /proc")
def test_a_worker_stopped_for_memory_while_it_counts_a_check_leaves_the_task_to_be_checked(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec 16.6, with a real count in a real query child: the stop never ends a written task.

    The watchdog's stop of the worker runs as it does in production (`stop_worker`: mark,
    kill the children, record), with the process's exit replaced. The count's run ends, the
    task goes back to pending with its stamp (not failed although the blame is certain, not
    succeeded although its write is in the table), and the next claim runs the checks only.
    """
    _checked(world, _check("no_big_id", _NO_BIG_ID))
    thread, task, count = _counting_slowly(world, monkeypatch)
    exits: list[int] = []

    def kill_children() -> None:
        """Kill every query child's process group, as the worker's stop does, and let it end.

        The worker's thread wakes when its child dies, sees the stop and leaves its task; here
        it does so before the recording begins, the order that left the task running in #154.
        """
        for pid in REGISTRY.pids():
            os.killpg(pid, signal.SIGKILL)
        thread.join(30)
        assert not thread.is_alive(), "the worker's thread did not end after its child was killed"

    memory_module.stop_worker(
        5 * 1024**3,
        _FOUR_GB,
        record=lambda: memory_module.record_memory_failure(
            world.session, _FOUR_GB, lambda: 0.0, "w1"
        ),
        kill_children=kill_children,
        exit_process=exits.append,
    )
    thread.join(30)
    assert not thread.is_alive(), "the worker's thread did not end after its child was killed"
    assert exits == [memory_module.EXIT_CODE]

    # Wait for the event under test, the task set back to pending. `stop_worker` stops waiting
    # for its recording thread after `RECORD_TIMEOUT_SECONDS`, so on a loaded runner the reset
    # can land after it has returned and the status was read while it still said running.
    deadline = time.monotonic() + 30
    while (stopped := _task(world, "silver.orders")).status is TaskStatus.RUNNING:
        assert time.monotonic() < deadline, "the task was not set back after the stop"
        time.sleep(0.05)
    assert stopped.status is TaskStatus.PENDING  # neither failed nor succeeded
    assert (stopped.attempt, stopped.written_table_id) == (1, task.written_table_id)
    (attempt,) = _attempts(world, task.id)
    assert (attempt.outcome, attempt.error_message) == (
        AttemptOutcome.FAILED,
        memory_module.task_message(_FOUR_GB),
    )
    (ended,) = _counts(world, task.id)
    assert (ended.id, ended.status) == (count.id, QueryRunStatus.FAILED)
    assert _results(world, task.id) == []
    assert _marker(world, "silver.orders") is None  # nothing was rolled back
    assert _task(world, "gold.report").status is TaskStatus.PENDING

    ACTIVITY.stopping = False  # the next worker is a new process
    _drain(world)
    done = _task(world, "silver.orders")
    assert (done.status, done.attempt) == (TaskStatus.SUCCEEDED, 2)
    assert len(_writes(world, task.id)) == 1  # the build ran once
    assert [c.status for c in _counts(world, task.id)] == [
        QueryRunStatus.FAILED,
        QueryRunStatus.SUCCEEDED,
    ]
    assert [r.outcome for r in _results(world, task.id)] == [CheckOutcome.PASSED]
    assert _task(world, "gold.report").status is TaskStatus.SUCCEEDED
    assert _ids(world, "silver.orders") == [1, 1, 2]


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="measures /proc")
def test_a_count_the_watchdog_ended_for_memory_is_a_check_that_could_not_run(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec section 5, with the real watchdog: the count is the largest consumer and is ended.

    The watchdog measures the query children only, with a limit of one byte, and is started
    once the count runs, so the count's child is the one it ends. The worker goes on. A check
    whose count ran out of memory could not run: for a blocking check that is a failed
    check, so the table is set back (here: not published) and the task fails for good.
    """
    _checked(world, _check("no_big_id", _NO_BIG_ID))
    thread, task, count = _counting_slowly(world, monkeypatch)
    worker_stops: list[int] = []
    dog = memory_module.MemoryWatchdog(
        1,
        lambda: sum(memory_module.resident_bytes(pid) for pid in REGISTRY.pids()),
        worker_stops.append,
        interval=0.02,
        relieve=lambda total: memory_module.stop_largest_consumer(total, _FOUR_GB) is not None,
    )
    watching = dog.start()
    try:
        thread.join(30)
        assert not thread.is_alive(), "the worker's thread did not end after its count was ended"
    finally:
        dog.stop()
        watching.join(5)
    assert worker_stops == []  # the worker was not stopped

    (ended,) = _counts(world, task.id)
    assert (ended.id, ended.status, ended.error_cause, ended.error_message) == (
        count.id,
        QueryRunStatus.FAILED,
        "out_of_memory",
        memory_module.query_child_message(_FOUR_GB),
    )
    (result,) = _results(world, task.id)
    assert (result.outcome, result.failing_rows, result.rollback_outcome) == (
        CheckOutcome.ERRORED,
        None,
        RollbackOutcome.UNPUBLISHED,
    )
    assert result.error == f"the check's query failed: it ran out of memory. {_SEE_YOURSELF}"
    done = _task(world, "silver.orders")
    assert (done.status, done.attempt) == (TaskStatus.FAILED, 1)
    assert len(_writes(world, task.id)) == 1
    assert _marker(world, "silver.orders") is not None
    assert _task(world, "gold.report").status is not TaskStatus.SUCCEEDED


def _stamped_and_lost_in_a_cancelled_run(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """Build `silver.orders` anew, lose the worker after its stamp and cancel the run.

    Parameters
    ----------
    world
        The world, with `silver.orders` built once already (ids 1, 1, 2).
    monkeypatch
        pytest's patcher.
    """
    run_id = _start(world)
    claimed = _run_until(world, "silver.orders")
    died = _die_in_the_check_phase(monkeypatch)
    with pytest.raises(SystemExit):
        run_claimed(_engine(world), claimed)
    assert died.is_set()
    assert _task(world, "silver.orders").written_table_id is not None
    assert _ids(world, "silver.orders") == [11, 11, 12]
    _set_cancel_requested(world, run_id)
    _expire_lease_and_tick(world, "silver.orders")
    task = _task(world, "silver.orders")
    assert (task.status, task.setting_back) == (TaskStatus.READY, True)


def _nothing_but_a_set_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the test fail if a set-back claim builds or checks.

    Parameters
    ----------
    monkeypatch
        pytest's patcher.
    """
    monkeypatch.setattr(tasks_module, "check_phase", lambda *a: pytest.fail("a check ran"))
    monkeypatch.setattr(tasks_module, "before_build", lambda *a: pytest.fail("before_build ran"))


@pytest.mark.slow
def test_a_stamped_task_lost_in_a_cancelled_run_is_set_back_from_its_stamp(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stamp names the write; the claim sets it back and the task ends cancelled."""
    _built_once_then_redeployed(world, _check("unique_id", _DUPLICATE_IDS))
    _stamped_and_lost_in_a_cancelled_run(world, monkeypatch)
    claimed = _claim_ready(world)
    assert claimed is not None and claimed.setting_back
    _nothing_but_a_set_back(monkeypatch)

    run_claimed(_engine(world), claimed)

    task = _task(world, "silver.orders")
    assert (task.status, task.error_message) == (
        TaskStatus.CANCELLED,
        f"{RUN_CANCELLED_MESSAGE} {SET_BACK_DONE}",
    )
    assert task.failure_code == "cancelled"
    assert _ids(world, "silver.orders") == [1, 1, 2]
    assert task.written_table_id is None  # the set-back cleared the stamp


def test_a_task_set_back_in_a_run_failed_by_an_integrity_check_says_so(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cancel flag that `fail_run` set is no cancel by a user, and the text must not say so."""
    _built_once_then_redeployed(world, _check("unique_id", _DUPLICATE_IDS))
    _stamped_and_lost_in_a_cancelled_run(world, monkeypatch)
    claimed = _claim_ready(world)
    assert claimed is not None and claimed.setting_back
    with world.session() as session:
        fail_run(session, claimed.run_id, "code integrity check failed")
    _nothing_but_a_set_back(monkeypatch)

    run_claimed(_engine(world), claimed)

    task = _task(world, "silver.orders")
    assert (task.status, task.error_message) == (
        TaskStatus.CANCELLED,
        f"{RUN_STOPPED_MESSAGE} {SET_BACK_DONE}",
    )


def test_a_stamped_task_whose_set_back_is_refused_ends_instead_of_going_round_again(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stamp stays, so a retryable ending would send the task back to be set back again."""
    _built_once_then_redeployed(world, _check("unique_id", _DUPLICATE_IDS))
    _stamped_and_lost_in_a_cancelled_run(world, monkeypatch)
    claimed = _claim_ready(world)
    assert claimed is not None
    _nothing_but_a_set_back(monkeypatch)

    def refuse(self: CatalogService, *args: object, **kwargs: object) -> object:
        raise PermissionDenied("x")

    monkeypatch.setattr(CatalogService, "roll_back", refuse)

    run_claimed(_engine(world), claimed)

    task = _task(world, "silver.orders")
    assert (task.status, task.error_message) == (
        TaskStatus.CANCELLED,
        set_back_failed_message(RUN_CANCELLED_MESSAGE),
    )
    assert task.failure_code == "cancelled"
    assert task.written_table_id is not None
    assert _ids(world, "silver.orders") == [11, 11, 12]


def _always_die_in_the_check_phase(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every check phase kill its worker, as a check that needs more memory than it has.

    Parameters
    ----------
    monkeypatch
        pytest's patcher.
    """

    def dying(*args: object) -> TaskOutcome | None:
        raise SystemExit("the worker died in the checks")

    monkeypatch.setattr(tasks_module, "check_phase", dying)


def test_checks_whose_worker_is_lost_every_time_end_set_back_after_the_bound(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#99, loop 1: after the bound the table is set back and the task fails saying why."""
    _built_once_then_redeployed(world, _check("unique_id", _DUPLICATE_IDS))
    run_id = _start(world)
    claimed = _run_until(world, "silver.orders")
    _always_die_in_the_check_phase(monkeypatch)
    with pytest.raises(SystemExit):
        run_claimed(_engine(world), claimed)
    for _ in range(MAX_CHECK_CLAIMS):
        _expire_lease_and_tick(world, "silver.orders")
        again = _claim_ready(world)
        assert again is not None and again.asset == "silver.orders" and not again.setting_back
        with pytest.raises(SystemExit):
            run_claimed(_engine(world), again)
    assert _task(world, "silver.orders").check_claims == MAX_CHECK_CLAIMS

    _expire_lease_and_tick(world, "silver.orders")
    set_back = _claim_ready(world)
    assert set_back is not None and set_back.setting_back
    _nothing_but_a_set_back(monkeypatch)
    run_claimed(_engine(world), set_back)

    task = _task(world, "silver.orders")
    assert (task.status, task.failure_code) == (TaskStatus.FAILED, "checks_unfinished")
    assert task.error_message == f"{CHECKS_UNFINISHED_MESSAGE} {SET_BACK_DONE}"
    assert _ids(world, "silver.orders") == [1, 1, 2]
    for _ in range(3):
        _tick(world)
    with world.session() as session:
        run = session.get(Run, run_id)
        assert run is not None and run.status.value == "failed"


def test_checks_that_cannot_start_end_after_the_bound_and_say_the_table_was_not_set_back(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#99, loop 2: the run's principal is gone, so the set-back cannot run either; it says so."""
    _checked(world, _check("unique_id", _DUPLICATE_IDS), retries=0)
    _start(world)
    claimed = _run_until(world, "silver.orders")
    real = tasks_module._dispatch

    def build_then_lose_the_principal(*args: object, **kwargs: object) -> TaskOutcome | None:
        outcome = real(*args, **kwargs)  # ty: ignore[invalid-argument-type]
        with world.session() as session:
            session.execute(update(Run).values(run_as_principal_id=None))
            session.execute(
                update(TaskRun)
                .where(TaskRun.asset == "silver.orders")
                .values(check_claims=MAX_CHECK_CLAIMS)
            )
            session.commit()
        return outcome

    monkeypatch.setattr(tasks_module, "_dispatch", build_then_lose_the_principal)
    run_claimed(_engine(world), claimed)
    assert _task(world, "silver.orders").status is TaskStatus.PENDING

    set_back = _claim_ready(world)
    assert set_back is not None and set_back.setting_back
    _nothing_but_a_set_back(monkeypatch)
    run_claimed(_engine(world), set_back)

    task = _task(world, "silver.orders")
    assert (task.status, task.failure_code) == (TaskStatus.FAILED, "checks_unfinished")
    assert task.error_message == set_back_failed_message(CHECKS_UNFINISHED_MESSAGE)


def _at_the_bound_after_the_build(world: World, monkeypatch: pytest.MonkeyPatch) -> uuid.UUID:
    """Rebuild `silver.orders`, lose its worker in the checks, and give it the most check claims.

    Parameters
    ----------
    world
        The world.
    monkeypatch
        pytest's patcher.

    Returns
    -------
    uuid.UUID
        The run's id.
    """
    _built_once_then_redeployed(world, _check("unique_id", _DUPLICATE_IDS))
    run_id = _start(world)
    claimed = _run_until(world, "silver.orders")
    _always_die_in_the_check_phase(monkeypatch)
    with pytest.raises(SystemExit):
        run_claimed(_engine(world), claimed)
    with world.session() as session:
        session.execute(
            update(TaskRun)
            .where(TaskRun.asset == "silver.orders")
            .values(check_claims=MAX_CHECK_CLAIMS)
        )
        session.commit()
    _expire_lease_and_tick(world, "silver.orders")
    assert _task(world, "silver.orders").setting_back
    return run_id


def test_a_lost_set_back_claim_still_ends_the_run_as_checks_that_never_finished(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#99, review S2: the reason survives a lost set-back claim, into the task and the webhook."""
    run_id = _at_the_bound_after_the_build(world, monkeypatch)
    lost = _claim_ready(world)
    assert lost is not None and lost.setting_back
    _expire_lease_and_tick(world, "silver.orders")
    set_back = _claim_ready(world)
    assert set_back is not None and set_back.setting_back
    _nothing_but_a_set_back(monkeypatch)
    run_claimed(_engine(world), set_back)

    task = _task(world, "silver.orders")
    assert (task.status, task.failure_code) == (TaskStatus.FAILED, "checks_unfinished")
    assert task.error_message == f"{CHECKS_UNFINISHED_MESSAGE} {SET_BACK_DONE}"
    for _ in range(3):
        _tick(world)
    with world.session() as session:
        run = session.get(Run, run_id)
        assert run is not None and run.status.value == "failed"
        assert facts_for_run(session, run, uuid.uuid4()).checks_unfinished == ("silver.orders",)


def test_a_manifest_this_worker_cannot_read_does_not_stop_the_set_back(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#99, review N1: the set-back reads no check, so it needs no readable manifest."""
    _at_the_bound_after_the_build(world, monkeypatch)
    with world.session() as session:
        version = session.get(JobVersion, world.version_id)
        assert version is not None
        version.manifest_json = {**version.manifest_json, "version": 99}
        session.commit()
    set_back = _claim_ready(world)
    assert set_back is not None and set_back.setting_back
    _nothing_but_a_set_back(monkeypatch)
    run_claimed(_engine(world), set_back)

    task = _task(world, "silver.orders")
    assert (task.status, task.failure_code) == (TaskStatus.FAILED, "checks_unfinished")
    assert task.error_message == f"{CHECKS_UNFINISHED_MESSAGE} {SET_BACK_DONE}"
    assert _ids(world, "silver.orders") == [1, 1, 2]


def test_a_count_that_keeps_failing_for_a_temporary_reason_ends_at_the_bound_saying_so(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#99, review N2: with attempts to spare, a flaky count ends at the bound; the text says so."""
    _checked(world, _check("unique_id", _DUPLICATE_IDS))
    _start(world)
    claimed = _run_until(world, "silver.orders")
    with world.session() as session:
        session.execute(
            update(TaskRun).where(TaskRun.asset == "silver.orders").values(max_attempts=10)
        )
        session.commit()
    _counting(monkeypatch, *[_transient] * (MAX_CHECK_CLAIMS + 1))
    run_claimed(_engine(world), claimed)
    for _ in range(MAX_CHECK_CLAIMS):
        again = _claim_ready(world)
        assert again is not None and again.asset == "silver.orders" and not again.setting_back
        run_claimed(_engine(world), again)

    set_back = _claim_ready(world)
    assert set_back is not None and set_back.setting_back
    run_claimed(_engine(world), set_back)

    task = _task(world, "silver.orders")
    assert (task.status, task.failure_code) == (TaskStatus.FAILED, "checks_unfinished")
    assert task.error_message is not None
    assert "counting a check kept failing for a temporary reason" in task.error_message
