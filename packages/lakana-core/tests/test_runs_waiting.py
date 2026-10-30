"""Tests for the wait reasons: the pure functions and the three small reads."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from lakana_core.db import db_now
from lakana_core.models import (
    DeployKind,
    DeployRun,
    DeployStatus,
    Environment,
    JobVersion,
    QueryRun,
    QueryRunStatus,
    Repository,
    RepositoryKind,
    Run,
    RunKind,
    RunStatus,
    TaskRun,
    TaskStatus,
    Worker,
    WriteMode,
)
from lakana_core.runs.waiting import (
    LOCK_TIMEOUT,
    MAX_UPSTREAM_BODY,
    MAX_UPSTREAM_NAMES,
    NO_WORKER,
    NO_WORKER_OF_KIND,
    RETRY,
    SETTING_BACK,
    SETTING_BACK_MESSAGE,
    STARTING,
    UPSTREAM,
    USER_LIMIT,
    WORKERS_BUSY,
    UserLimit,
    WaitingTask,
    WaitReason,
    WorkerCounts,
    WorkerSnapshot,
    ahead_kind,
    ahead_of,
    alive_workers,
    attempts_used,
    count_ahead,
    query_wait_reason,
    queued_times,
    settled_early,
    task_wait_reason,
    with_work,
)

_NO_WORKER_QUERY = (
    "No worker is running, so this query cannot start. Ask the operator to start a worker; on "
    "a local instance, start it again with 'lakana server start --local'."
)
_NO_WORKER_TASK = _NO_WORKER_QUERY.replace("this query", "this task")
_FREE_QUERY = "A worker is free; the query starts within about a second."


@pytest.mark.parametrize(
    ("counts", "ahead", "code", "message"),
    [
        (WorkerCounts(0, 0, 0), 3, "no_worker", _NO_WORKER_QUERY),
        (WorkerCounts(2, 2, 1), 0, "starting", _FREE_QUERY),
        (
            WorkerCounts(2, 2, 1),
            1,
            "starting",
            "A worker is free; about 1 query or deploy is ahead of this one, and it starts as "
            "soon as they are taken.",
        ),
        (
            WorkerCounts(2, 2, 1),
            5,
            "starting",
            "A worker is free; about 5 queries or deploys are ahead of this one, and it starts "
            "as soon as they are taken.",
        ),
        (
            WorkerCounts(2, 2, 0),
            4,
            "workers_busy",
            "All 2 workers are busy, and about 4 queries or deploys are ahead of this one.",
        ),
        (
            WorkerCounts(1, 1, 0),
            4,
            "workers_busy",
            "The only worker is busy, and about 4 queries or deploys are ahead of this one.",
        ),
        (
            WorkerCounts(2, 2, 0),
            0,
            "workers_busy",
            "All 2 workers are busy, and nothing is ahead of this one.",
        ),
        (
            WorkerCounts(2, 2, 0),
            1,
            "workers_busy",
            "All 2 workers are busy, and about 1 query or deploy is ahead of this one.",
        ),
        (
            WorkerCounts(2, 2, 1),
            2,
            "starting",
            "A worker is free; about 2 queries or deploys are ahead of this one, and it starts "
            "as soon as they are taken.",
        ),
    ],
)
def test_a_queued_query_says_why_it_waits(
    counts: WorkerCounts, ahead: int, code: str, message: str
) -> None:
    reason = query_wait_reason(counts, ahead)
    assert (reason.code, reason.message) == (code, message)


def c(alive: int, taking: int, free: int) -> WorkerCounts:
    """Build the counts for one kind.

    Parameters
    ----------
    alive
        Alive workers.
    taking
        Alive workers that take the kind.
    free
        Free ones among them.

    Returns
    -------
    WorkerCounts
        The counts.
    """
    return WorkerCounts(alive, taking, free)


def u(running: int, limit: int, own: bool = True) -> UserLimit:
    """Build a principal's count against the limit.

    Parameters
    ----------
    running
        Their running ad-hoc queries.
    limit
        The limit.
    own
        Whether the reader is that principal.

    Returns
    -------
    UserLimit
        The value.
    """
    return UserLimit(running, limit, own)


_NO_INTERACTIVE_2 = (
    "No worker that takes interactive work is running (2 workers take pipeline work only), so "
    "this query cannot start. Ask the operator to start a worker with "
    "LAKANA_WORKER__TAKES=interactive or all."
)
_NO_PIPELINE_1 = (
    "No worker that takes pipeline work is running (1 worker takes interactive work only), so "
    "this query cannot start. Ask the operator to start a worker with "
    "LAKANA_WORKER__TAKES=pipeline or all."
)
_NO_PIPELINE_MAINTENANCE = (
    "No worker that takes pipeline work is running (2 workers take interactive work only), so "
    "this maintenance run cannot start. Ask the operator to start a worker with "
    "LAKANA_WORKER__TAKES=pipeline or all."
)
_OWN_AT_2 = (
    "You have 2 queries running, which is the limit per user here; this one can start when one "
    "of them ends. 'lakana query list' shows them."
)
_OWN_AT_1 = (
    "You have 1 query running, which is the limit per user here; this one can start when it "
    "ends. 'lakana query list' shows it."
)
_OWN_ABOVE_2 = (
    "You have 3 queries running and the limit per user here is 2; this one can start when "
    "fewer than 2 are running. 'lakana query list' shows them."
)
_OWN_ABOVE_1 = (
    "You have 2 queries running and the limit per user here is 1; this one can start when "
    "none is running. 'lakana query list' shows them."
)
_MANAGER_2 = (
    "Its submitter has reached the limit of 2 running queries per user; it can start when "
    "fewer than 2 of theirs are running."
)
_MANAGER_1 = (
    "Its submitter has reached the limit of 1 running query per user; it can start when none "
    "of theirs is running."
)


@pytest.mark.parametrize(
    ("counts", "arguments", "code", "message"),
    [
        (c(0, 0, 0), {"limit": u(2, 2)}, "no_worker", _NO_WORKER_QUERY),
        (c(2, 0, 0), {"limit": u(2, 2)}, "no_worker_of_kind", _NO_INTERACTIVE_2),
        (c(1, 0, 0), {"kind": "pipeline"}, "no_worker_of_kind", _NO_PIPELINE_1),
        (
            c(2, 0, 0),
            {"kind": "pipeline", "maintenance": True},
            "no_worker_of_kind",
            _NO_PIPELINE_MAINTENANCE,
        ),
        (c(2, 2, 2), {"limit": u(2, 2)}, "user_limit", _OWN_AT_2),
        (c(1, 1, 1), {"limit": u(1, 1)}, "user_limit", _OWN_AT_1),
        (c(3, 3, 1), {"limit": u(3, 2)}, "user_limit", _OWN_ABOVE_2),
        (c(1, 1, 1), {"limit": u(2, 1)}, "user_limit", _OWN_ABOVE_1),
        (c(3, 3, 1), {"limit": u(2, 2, own=False)}, "user_limit", _MANAGER_2),
        (c(3, 3, 1), {"limit": u(3, 2, own=False)}, "user_limit", _MANAGER_2),
        (c(1, 1, 1), {"limit": u(1, 1, own=False)}, "user_limit", _MANAGER_1),
        (c(1, 1, 1), {"limit": u(2, 1, own=False)}, "user_limit", _MANAGER_1),
        (c(2, 2, 1), {"limit": u(1, 2)}, "starting", _FREE_QUERY),
        # at the limit with every worker of the kind busy: the limit holds nothing, so the busy
        # sentence, word for word the one before the limit existed
        (
            c(1, 1, 0),
            {"limit": u(1, 1)},
            "workers_busy",
            "The only worker is busy, and nothing is ahead of this one.",
        ),
        (
            c(2, 2, 0),
            {"limit": u(3, 2, own=False)},
            "workers_busy",
            "All 2 workers are busy, and nothing is ahead of this one.",
        ),
        (
            c(2, 2, 0),
            {},
            "workers_busy",
            "All 2 workers are busy, and nothing is ahead of this one.",
        ),
        (
            c(3, 2, 0),
            {},
            "workers_busy",
            "All 2 workers that take interactive work are busy, and nothing is ahead of this one.",
        ),
        (
            c(3, 1, 0),
            {"kind": "pipeline"},
            "workers_busy",
            "The only worker that takes pipeline work is busy, and nothing is ahead of this one.",
        ),
    ],
)
def test_a_queued_query_says_why_it_waits_by_kind(
    counts: WorkerCounts, arguments: dict[str, Any], code: str, message: str
) -> None:
    reason = query_wait_reason(counts, 0, **arguments)
    assert (reason.code, reason.message) == (code, message)


def test_no_worker_wins_over_a_reached_limit_and_no_worker_of_the_kind_wins_over_it_too() -> None:
    reached = u(2, 2)
    assert query_wait_reason(c(0, 0, 0), 0, limit=reached).code == NO_WORKER
    assert query_wait_reason(c(2, 0, 0), 0, limit=reached).code == NO_WORKER_OF_KIND


def test_a_reached_limit_holds_only_while_a_worker_of_the_kind_is_free() -> None:
    reached = u(2, 2)
    assert query_wait_reason(c(2, 2, 2), 5, limit=reached).code == USER_LIMIT
    assert query_wait_reason(c(3, 2, 1), 5, limit=reached).code == USER_LIMIT
    busy = query_wait_reason(c(2, 2, 0), 5, limit=reached)
    assert (busy.code, busy.ahead) == (WORKERS_BUSY, 5)
    assert query_wait_reason(c(2, 2, 0), 5, limit=u(1, 2)).code == WORKERS_BUSY
    assert query_wait_reason(c(2, 2, 1), 5, limit=u(1, 2)).code == STARTING


def test_the_bodies_of_the_two_new_reasons() -> None:
    kind = query_wait_reason(c(2, 0, 0), 0).to_json()
    assert kind == {
        "code": "no_worker_of_kind",
        "message": _NO_INTERACTIVE_2,
        "workers": 0,
        "other_workers": 2,
    }
    own = query_wait_reason(c(2, 2, 2), 7, limit=u(2, 2)).to_json()
    assert own == {"code": "user_limit", "message": _OWN_AT_2, "running": 2, "limit": 2}
    manager = query_wait_reason(c(2, 2, 2), 7, limit=u(2, 2, own=False)).to_json()
    assert manager == {"code": "user_limit", "message": _MANAGER_2, "limit": 2}
    # a manager's body is the same above the limit: the count running is never in it
    above = query_wait_reason(c(2, 2, 2), 7, limit=u(9, 2, own=False)).to_json()
    assert above == manager
    for counts in (c(3, 2, 1), c(3, 2, 0)):
        assert query_wait_reason(counts, 0).to_json()["workers"] == 2


def test_the_snapshot_counts_by_kind() -> None:
    snapshot = WorkerSnapshot(
        {"a": "all", "i": "interactive", "p": "pipeline"},
        frozenset({"a", "gone"}),
        frozenset({"p"}),
    )
    assert snapshot.for_query("interactive") == c(3, 2, 1)
    assert snapshot.for_query("pipeline") == c(3, 2, 1)
    assert snapshot.for_task() == c(3, 2, 0)
    assert snapshot.serves_both()
    assert snapshot.interactive() == 2
    without_all = WorkerSnapshot({"i": "interactive", "p": "pipeline"})
    assert not without_all.serves_both()
    assert without_all.for_query("interactive") == c(2, 1, 1)
    assert without_all.for_query("pipeline") == c(2, 1, 1)
    assert WorkerSnapshot({}).for_query("pipeline") == c(0, 0, 0)


def test_settled_early_is_exactly_the_first_three_cases() -> None:
    assert settled_early(c(0, 0, 0), None)
    assert settled_early(c(2, 0, 0), None)
    assert settled_early(c(2, 2, 1), u(2, 2))
    assert settled_early(c(2, 2, 1), u(3, 2))
    assert not settled_early(c(2, 2, 0), u(2, 2))  # every worker busy: the limit holds nothing
    assert not settled_early(c(2, 2, 0), u(3, 2))
    assert not settled_early(c(2, 2, 1), u(1, 2))
    assert not settled_early(c(2, 2, 1), None)


def test_a_worker_with_an_unknown_takes_is_not_named_as_taking_the_other_kind() -> None:
    snapshot = WorkerSnapshot({"a": "interactive", "b": "something-newer", "c": "interactive"})
    reason = query_wait_reason(snapshot.for_query("pipeline"), 0, kind="pipeline")
    assert reason.code == NO_WORKER_OF_KIND
    assert "(3 workers take other work only)" in reason.message
    assert reason.other_workers == 3
    only_unknown = WorkerSnapshot({"b": "something-newer"})
    assert (
        "(1 worker takes other work only)"
        in query_wait_reason(only_unknown.for_query("interactive"), 0).message
    )
    task = task_wait_reason(_task(status="ready"), snapshot.for_task(), _NOW)
    assert task is not None
    assert task.code == NO_WORKER_OF_KIND
    assert "(3 workers take other work only)" in task.message
    known = WorkerSnapshot({"a": "interactive", "c": "interactive"})
    assert (
        "(2 workers take interactive work only)"
        in query_wait_reason(known.for_query("pipeline"), 0, kind="pipeline").message
    )


@pytest.mark.parametrize("status", ["ready", "pending"])
def test_a_task_without_a_pipeline_worker_says_so(status: str) -> None:
    reason = task_wait_reason(_task(status=status), c(2, 0, 0), _NOW)
    assert reason is not None
    assert (reason.code, reason.message) == (
        "no_worker_of_kind",
        "No worker that takes pipeline work is running (2 workers take interactive work only), "
        "so this task cannot start. Ask the operator to start a worker with "
        "LAKANA_WORKER__TAKES=pipeline or all.",
    )
    assert reason.to_json()["other_workers"] == 2
    assert reason.to_json()["workers"] == 0


def test_a_built_task_that_waits_for_its_checks_keeps_the_retry_code_and_its_own_sentence() -> None:
    """The table is built, so "Attempt N of M failed" would be a false number: no numbers."""
    when = _NOW + timedelta(seconds=30)
    reason = task_wait_reason(
        _task(attempt=4, max_attempts=3, next_attempt_at=when, checking=True), c(2, 1, 1), _NOW
    )
    assert reason is not None and reason.code == RETRY
    assert reason.message == (
        "The table is built; its checks could not finish and run again at 14:00:30 UTC."
    )
    assert reason.to_json() == {
        "code": "retry",
        "message": reason.message,
        "next_attempt_at": when.isoformat(),
    }
    normal = task_wait_reason(
        _task(attempt=1, next_attempt_at=when, checking=False), c(2, 1, 1), _NOW
    )
    assert normal is not None and normal.to_json()["attempt"] == 1
    assert normal.message.startswith("Attempt 1 of 3 failed;")
    due = task_wait_reason(_task(next_attempt_at=_NOW, checking=True), c(2, 1, 1), _NOW)
    assert due is not None and due.code != RETRY


@pytest.mark.parametrize(("attempt", "shown"), [(1, 1), (3, 3), (4, 3), (9, 3)])
def test_the_attempt_shown_never_passes_the_limit(attempt: int, shown: int) -> None:
    """Below the limit as it is, at it as it is, above it capped; the retry reason agrees."""
    assert attempts_used(attempt, 3) == shown
    when = _NOW + timedelta(seconds=30)
    reason = task_wait_reason(
        _task(attempt=attempt, max_attempts=3, next_attempt_at=when, checking=False),
        c(2, 1, 1),
        _NOW,
    )
    assert reason is not None
    assert reason.to_json()["attempt"] == shown
    assert reason.message.startswith(f"Attempt {shown} of 3 failed;")


def test_a_task_says_upstream_and_retry_before_the_worker_reasons() -> None:
    nobody_takes = c(2, 0, 0)
    upstream = task_wait_reason(_task(unfinished_upstream=["a"]), nobody_takes, _NOW)
    assert upstream is not None and upstream.code == UPSTREAM
    retry = task_wait_reason(
        _task(attempt=1, next_attempt_at=_NOW + timedelta(seconds=30)), nobody_takes, _NOW
    )
    assert retry is not None and retry.code == RETRY
    none = task_wait_reason(_task(status="ready"), c(0, 0, 0), _NOW)
    assert none is not None and none.code == NO_WORKER


@pytest.mark.parametrize(
    ("counts", "message"),
    [
        (
            c(2, 2, 0),
            "Ready, and all 2 workers are busy. Queued queries and deploys are served first.",
        ),
        (
            c(3, 2, 0),
            "Ready, and all 2 workers that take pipeline work are busy. Queued queries and "
            "deploys are served first.",
        ),
        (
            c(3, 1, 0),
            "Ready, and the only worker that takes pipeline work is busy. Queued queries and "
            "deploys are served first.",
        ),
    ],
)
def test_a_busy_task_names_the_kind_only_when_another_worker_does_not_take_it(
    counts: WorkerCounts, message: str
) -> None:
    reason = task_wait_reason(_task(status="ready"), counts, _NOW)
    assert reason is not None
    assert (reason.code, reason.message) == ("workers_busy", message)
    assert reason.to_json()["workers"] == counts.taking


def test_ahead_kind_is_none_while_a_worker_takes_all() -> None:
    both = WorkerSnapshot({"a": "all", "i": "interactive"})
    assert ahead_kind(both, "interactive") is None
    assert ahead_kind(both, "pipeline") is None
    split = WorkerSnapshot({"i": "interactive", "p": "pipeline"})
    assert ahead_kind(split, "interactive") == "interactive"
    assert ahead_kind(split, "pipeline") == "pipeline"


def test_the_codes_of_the_two_new_reasons_are_stable_strings() -> None:
    assert (NO_WORKER_OF_KIND, USER_LIMIT) == ("no_worker_of_kind", "user_limit")


def test_a_snapshot_costs_one_statement_and_its_work_three(
    session: Session, environment: Environment
) -> None:
    _worker(session, "w", 5)
    seen = _recorded(session, lambda: alive_workers(session, db_now(session), 60))
    assert sum("FROM workers" in s for s in seen) == 1
    snapshot = alive_workers(session, db_now(session), 60)
    assert len(_recorded(session, lambda: with_work(session, snapshot))) == 3


def test_with_work_reads_nothing_when_no_worker_is_alive(session: Session) -> None:
    empty = WorkerSnapshot({})
    assert _recorded(session, lambda: with_work(session, empty)) == []
    assert with_work(session, empty) is empty


def test_ahead_counts_by_kind(session: Session, environment: Environment) -> None:
    base = db_now(session)
    at = [base + timedelta(seconds=n) for n in range(6)]
    _query(session, environment, QueryRunStatus.QUEUED, created_at=at[0])
    _query(session, environment, QueryRunStatus.QUEUED, created_at=at[1])
    _query(
        session, environment, QueryRunStatus.QUEUED, created_at=at[2], write_mode=WriteMode.MAINTAIN
    )
    _deploy(session, environment, DeployStatus.QUEUED, created_at=at[3])
    later = at[5]
    assert count_ahead(session, later, None) == 4
    assert count_ahead(session, later, "interactive") == 2
    assert count_ahead(session, later, "pipeline") == 2
    assert count_ahead(session, later) == 4
    assert len(queued_times(session)) == 4
    assert queued_times(session, kind="interactive") == at[:2]
    assert queued_times(session, kind="pipeline") == [at[2], at[3]]
    assert queued_times(session, 1, kind="interactive") == at[:1]
    assert queued_times(session, 1, kind="pipeline") == [at[2]]
    assert queued_times(session, 1) == at[:1]


def test_a_query_run_of_a_task_is_pipeline_work_ahead(
    session: Session, environment: Environment
) -> None:
    base = db_now(session)
    _task_run(session, environment, None)
    task_run_id = session.scalars(select(TaskRun.id)).one()
    _query(
        session,
        environment,
        QueryRunStatus.QUEUED,
        created_at=base - timedelta(seconds=1),
        task_run_id=task_run_id,
    )
    assert count_ahead(session, base, "pipeline") == 1
    assert count_ahead(session, base, "interactive") == 0


def test_one_statement_of_times_for_interactive_work_and_two_for_the_rest(
    session: Session, environment: Environment
) -> None:
    assert len(_recorded(session, lambda: queued_times(session, kind="interactive"))) == 1
    assert len(_recorded(session, lambda: queued_times(session, kind="pipeline"))) == 2
    assert len(_recorded(session, lambda: queued_times(session))) == 2


def test_a_free_worker_and_queries_ahead_is_starting_with_the_real_number_ahead() -> None:
    reason = query_wait_reason(WorkerCounts(3, 3, 1), 7)
    assert reason.code == STARTING
    assert reason.to_json() == {
        "code": "starting",
        "message": (
            "A worker is free; about 7 queries or deploys are ahead of this one, and it starts "
            "as soon as they are taken."
        ),
        "workers": 3,
        "ahead": 7,
    }


_NOW = datetime(2026, 10, 9, 14, 0, 0, tzinfo=UTC)


def _task(**fields: object) -> WaitingTask:
    """Build a pending task with no upstream and no retry, changed by `fields`.

    Parameters
    ----------
    **fields
        Field values that override the defaults.

    Returns
    -------
    WaitingTask
        The task.
    """
    values: dict[str, Any] = {
        "status": "pending",
        "attempt": 0,
        "max_attempts": 3,
        "next_attempt_at": None,
        "unfinished_upstream": (),
        "checking": False,
        "setting_back": False,
    }
    values.update(fields)
    return WaitingTask(**values)


@pytest.mark.parametrize(
    ("task", "counts", "code", "message"),
    [
        (
            _task(unfinished_upstream=["a", "b"]),
            WorkerCounts(0, 0, 0),
            "upstream",
            "Waiting for upstream tasks: a, b.",
        ),
        (
            _task(unfinished_upstream=list("abcdefgh")),
            WorkerCounts(1, 1, 1),
            "upstream",
            "Waiting for upstream tasks: a, b, c, d, e and 3 more.",
        ),
        (
            _task(attempt=2, next_attempt_at=_NOW + timedelta(seconds=200)),
            WorkerCounts(1, 1, 1),
            "retry",
            "Attempt 2 of 3 failed; the next one starts at 14:03:20 UTC.",
        ),
        (
            _task(
                attempt=2,
                next_attempt_at=(_NOW + timedelta(seconds=200)).astimezone(
                    timezone(timedelta(hours=2))
                ),
            ),
            WorkerCounts(1, 1, 1),
            "retry",
            "Attempt 2 of 3 failed; the next one starts at 14:03:20 UTC.",
        ),
        (
            _task(attempt=2, next_attempt_at=_NOW),
            WorkerCounts(1, 1, 1),
            "starting",
            "Starts within about a second.",
        ),
        (_task(), WorkerCounts(0, 0, 0), "no_worker", _NO_WORKER_TASK),
        (_task(status="ready"), WorkerCounts(0, 0, 0), "no_worker", _NO_WORKER_TASK),
        (
            _task(status="ready"),
            WorkerCounts(2, 2, 0),
            "workers_busy",
            "Ready, and all 2 workers are busy. Queued queries and deploys are served first.",
        ),
        (
            _task(status="ready"),
            WorkerCounts(1, 1, 0),
            "workers_busy",
            "Ready, and the only worker is busy. Queued queries and deploys are served first.",
        ),
        (_task(), WorkerCounts(1, 1, 0), "starting", "Starts within about a second."),
        (_task(status="ready"), WorkerCounts(1, 1, 1), "starting", "Starts within about a second."),
    ],
)
def test_a_waiting_task_says_why_it_waits(
    task: WaitingTask, counts: WorkerCounts, code: str, message: str
) -> None:
    reason = task_wait_reason(task, counts, _NOW)
    assert reason is not None
    assert (reason.code, reason.message) == (code, message)


@pytest.mark.parametrize(("count", "tail"), [(5, ""), (6, " and 1 more")])
def test_the_cap_of_named_upstream_tasks_is_exactly_five(count: int, tail: str) -> None:
    names = [f"t{n}" for n in range(count)]
    reason = task_wait_reason(_task(unfinished_upstream=names), WorkerCounts(1, 1, 1), _NOW)
    assert reason is not None
    assert reason.message == f"Waiting for upstream tasks: t0, t1, t2, t3, t4{tail}."


def test_the_body_lists_at_most_fifty_upstream_names_and_the_total() -> None:
    names = [f"t{n}" for n in range(MAX_UPSTREAM_BODY + 7)]
    reason = task_wait_reason(_task(unfinished_upstream=names), WorkerCounts(1, 1, 1), _NOW)
    assert reason is not None
    body = reason.to_json()
    assert body["upstream"] == names[:MAX_UPSTREAM_BODY]
    assert body["upstream_count"] == MAX_UPSTREAM_BODY + 7
    assert str(MAX_UPSTREAM_BODY + 2) + " more" in str(body["message"])


@pytest.mark.parametrize(
    ("names", "hidden", "message"),
    [
        (
            [],
            1,
            "Waiting for 1 upstream table you may not read; the task starts when it is built.",
        ),
        (
            [],
            2,
            "Waiting for 2 upstream tables you may not read; the task starts when they are built.",
        ),
        (["a"], 1, "Waiting for upstream tasks: a and 1 table you may not read."),
        (
            list("abcdefg"),
            3,
            "Waiting for upstream tasks: a, b, c, d, e, 2 more and 3 tables you may not read.",
        ),
    ],
)
def test_an_upstream_the_reader_may_not_read_is_counted_never_named(
    names: list[str], hidden: int, message: str
) -> None:
    task = _task(unfinished_upstream=names, hidden_upstream=hidden)
    reason = task_wait_reason(task, WorkerCounts(1, 1, 1), _NOW)
    assert reason is not None
    assert reason.message == message
    body = reason.to_json()
    assert body["upstream"] == names
    assert body["upstream_count"] == len(names) + hidden
    assert body["hidden_upstream"] == hidden


def test_no_hidden_upstream_leaves_the_body_without_the_count() -> None:
    reason = task_wait_reason(_task(unfinished_upstream=["a"]), WorkerCounts(1, 1, 1), _NOW)
    assert reason is not None
    assert "hidden_upstream" not in reason.to_json()


def test_an_upstream_name_is_escaped_in_the_sentence_and_raw_in_the_list() -> None:
    raw = "a\x1b[31m\nb"
    reason = task_wait_reason(_task(unfinished_upstream=[raw]), WorkerCounts(1, 1, 1), _NOW)
    assert reason is not None
    assert reason.message == "Waiting for upstream tasks: a\\x1b[31m\\nb."
    assert not any(ord(c) < 32 for c in reason.message)
    assert reason.to_json()["upstream"] == [raw]


def test_a_pending_task_behind_a_dead_upstream_has_no_reason() -> None:
    task = _task(unfinished_upstream=["a"], blocked=True)
    assert task_wait_reason(task, WorkerCounts(1, 1, 1), _NOW) is None
    ready = _task(status="ready", blocked=True)
    assert task_wait_reason(ready, WorkerCounts(1, 1, 1), _NOW) is not None


@pytest.mark.parametrize(
    "status", ["running", "succeeded", "failed", "upstream_failed", "cancelled"]
)
def test_a_task_that_is_not_waiting_has_no_reason(status: str) -> None:
    task = _task(status=status, unfinished_upstream=["a"])
    assert task_wait_reason(task, WorkerCounts(0, 0, 0), _NOW) is None


def test_a_ready_task_ignores_upstream_and_retry_left_over() -> None:
    task = _task(
        status="ready",
        unfinished_upstream=["a"],
        attempt=1,
        next_attempt_at=_NOW + timedelta(seconds=30),
    )
    reason = task_wait_reason(task, WorkerCounts(1, 1, 1), _NOW)
    assert reason is not None
    assert reason.code == STARTING


@pytest.mark.parametrize("counts", [WorkerCounts(2, 2, 0), WorkerCounts(1, 1, 1)])
def test_a_ready_task_that_is_setting_back_says_so(counts: WorkerCounts) -> None:
    reason = task_wait_reason(_task(status="ready", setting_back=True), counts, _NOW)
    assert reason is not None
    assert reason.code == SETTING_BACK == "setting_back"
    assert (
        reason.message
        == SETTING_BACK_MESSAGE
        == (
            "This task's table may hold rows no check has passed; the next free worker sets the "
            "table back."
        )
    )
    assert reason.to_json() == {"code": "setting_back", "message": reason.message}


@pytest.mark.parametrize(
    ("counts", "code"),
    [(WorkerCounts(0, 0, 0), NO_WORKER), (WorkerCounts(2, 0, 0), NO_WORKER_OF_KIND)],
)
def test_a_task_that_is_setting_back_says_no_worker_first_when_none_can_take_it(
    counts: WorkerCounts, code: str
) -> None:
    reason = task_wait_reason(_task(status="ready", setting_back=True), counts, _NOW)
    assert reason is not None
    assert reason.code == code


def test_a_pending_task_does_not_say_it_is_setting_back() -> None:
    task = _task(status="pending", setting_back=True, unfinished_upstream=["a"])
    reason = task_wait_reason(task, WorkerCounts(1, 1, 1), _NOW)
    assert reason is not None
    assert reason.code == "upstream"


def test_alive_busy_and_free_are_counted_from_the_ids() -> None:
    snapshot = WorkerSnapshot(
        {"a": "all", "b": "all", "c": "all"}, frozenset({"a", "gone"}), frozenset({"b"})
    )
    assert snapshot.for_query("interactive") == WorkerCounts(3, 3, 2)
    assert snapshot.for_task() == WorkerCounts(3, 3, 1)


def test_one_busy_worker_is_the_only_worker() -> None:
    counts = WorkerSnapshot({"a": "all"}, frozenset({"a"})).for_query("interactive")
    assert query_wait_reason(counts, 0).message == (
        "The only worker is busy, and nothing is ahead of this one."
    )


def test_the_body_has_only_the_fields_its_code_uses() -> None:
    nothing = WorkerCounts(0, 0, 0)
    busy = WorkerCounts(2, 2, 0)
    when = _NOW + timedelta(seconds=200)
    assert query_wait_reason(nothing, 3).to_json() == {
        "code": "no_worker",
        "message": _NO_WORKER_QUERY,
        "workers": 0,
    }
    assert set(query_wait_reason(busy, 4).to_json()) == {"code", "message", "workers", "ahead"}
    ready = task_wait_reason(_task(status="ready"), busy, _NOW)
    assert ready is not None
    assert set(ready.to_json()) == {"code", "message", "workers"}
    names = list("abcdefgh")
    upstream = task_wait_reason(_task(unfinished_upstream=names), busy, _NOW)
    assert upstream is not None
    assert upstream.to_json()["upstream"] == names
    assert upstream.to_json()["upstream_count"] == 8
    assert set(upstream.to_json()) == {"code", "message", "upstream", "upstream_count"}
    retry = task_wait_reason(_task(attempt=2, next_attempt_at=when), busy, _NOW)
    assert retry is not None
    assert retry.to_json()["next_attempt_at"] == when.isoformat()
    assert set(retry.to_json()) == {"code", "message", "attempt", "max_attempts", "next_attempt_at"}
    assert (retry.to_json()["attempt"], retry.to_json()["max_attempts"]) == (2, 3)
    starting = task_wait_reason(_task(), busy, _NOW)
    assert starting is not None
    assert starting.to_json() == {"code": "starting", "message": "Starts within about a second."}


def test_the_codes_are_the_stable_strings() -> None:
    assert (NO_WORKER, STARTING, WORKERS_BUSY, UPSTREAM, RETRY) == (
        "no_worker",
        "starting",
        "workers_busy",
        "upstream",
        "retry",
    )
    assert MAX_UPSTREAM_NAMES == 5


def test_ahead_counts_what_was_queued_earlier() -> None:
    times = [_NOW + timedelta(seconds=n) for n in (0, 1, 1, 2, 5)]
    assert ahead_of(times, _NOW + timedelta(seconds=1)) == 1
    assert ahead_of(times, _NOW + timedelta(seconds=3)) == 4
    assert ahead_of(times, _NOW) == 0
    assert ahead_of([], _NOW) == 0


# -- the reads ---------------------------------------------------------------


def _recorded(session: Session, call: Callable[[], object]) -> list[str]:
    """Run `call` and return the SQL statements it issued.

    Parameters
    ----------
    session
        Database session.
    call
        What to run.

    Returns
    -------
    list of str
        The statements, in order.
    """
    seen: list[str] = []

    def record(conn: object, cursor: object, statement: str, *args: object) -> None:
        seen.append(statement)

    bind = session.get_bind()
    event.listen(bind, "before_cursor_execute", record)
    try:
        call()
    finally:
        event.remove(bind, "before_cursor_execute", record)
    return seen


@pytest.fixture
def environment(session: Session) -> Environment:
    """Insert one environment.

    Parameters
    ----------
    session
        Database session to insert into.

    Returns
    -------
    Environment
        The row.
    """
    env = Environment(
        id=uuid.uuid4(),
        name="dev",
        storage_root="file:///tmp/lakana-test",
        storage_options={},
        is_protected=False,
        properties={},
    )
    session.add(env)
    session.flush()
    return env


def _worker(session: Session, worker_id: str, age: int) -> None:
    """Insert a worker whose heartbeat is `age` seconds old by the database's clock.

    Parameters
    ----------
    session
        Database session.
    worker_id
        The worker's id.
    age
        Seconds since its heartbeat.
    """
    beat = db_now(session) - timedelta(seconds=age)
    session.add(Worker(id=worker_id, host="h", version="1", started_at=beat, heartbeat_at=beat))
    session.flush()


def _query(
    session: Session, env: Environment, status: QueryRunStatus, **fields: object
) -> QueryRun:
    """Insert a query run.

    Parameters
    ----------
    session
        Database session.
    env
        Its environment.
    status
        Its status.
    **fields
        More column values.

    Returns
    -------
    QueryRun
        The flushed run.
    """
    values: dict[str, object] = {"sql_text": "SELECT 1"}
    values.update(fields)
    run = QueryRun(id=uuid.uuid4(), environment_id=env.id, status=status, **values)
    session.add(run)
    session.flush()
    return run


def _deploy(session: Session, env: Environment, status: DeployStatus, **fields: object) -> None:
    """Insert a deploy run (and a repository for it).

    Parameters
    ----------
    session
        Database session.
    env
        Its environment.
    status
        Its status.
    **fields
        More column values.
    """
    repo = Repository(
        id=uuid.uuid4(), name=f"r{uuid.uuid4().hex[:6]}", kind=RepositoryKind.HTTPS, url="https://h"
    )
    session.add(repo)
    session.flush()
    session.add(
        DeployRun(
            id=uuid.uuid4(),
            environment_id=env.id,
            repository_id=repo.id,
            kind=DeployKind.DEPLOY,
            status=status,
            **fields,
        )
    )
    session.flush()


def _task_run(session: Session, env: Environment, lease_owner: str | None) -> None:
    """Insert a `running` task run leased by `lease_owner`.

    Parameters
    ----------
    session
        Database session.
    env
        Its environment.
    lease_owner
        The worker holding the lease.
    """
    repo = Repository(
        id=uuid.uuid4(), name=f"r{uuid.uuid4().hex[:6]}", kind=RepositoryKind.HTTPS, url="https://h"
    )
    session.add(repo)
    session.flush()
    version = JobVersion(
        id=uuid.uuid4(),
        repository_id=repo.id,
        environment_id=env.id,
        commit_sha="a" * 40,
        manifest_json={},
        code_uri="file:///tmp/code",
        code_sha256="b" * 64,
    )
    session.add(version)
    session.flush()
    run = Run(
        id=uuid.uuid4(),
        environment_id=env.id,
        repository_id=repo.id,
        job_version_id=version.id,
        kind=RunKind.FULL,
        status=RunStatus.RUNNING,
        selection_json={},
    )
    session.add(run)
    session.flush()
    session.add(
        TaskRun(
            id=uuid.uuid4(),
            run_id=run.id,
            asset="a.b",
            status=TaskStatus.RUNNING,
            max_attempts=1,
            timeout_seconds=60,
            retry_delay_seconds=0,
            upstream_json=[],
            lease_owner=lease_owner,
        )
    )
    session.flush()


def test_workers_are_counted_from_the_runs_that_carry_their_ids(
    session: Session, environment: Environment
) -> None:
    for worker_id in ("on-query", "on-deploy", "on-task", "idle"):
        _worker(session, worker_id, 5)
    _worker(session, "dead", 61)
    _query(session, environment, QueryRunStatus.RUNNING, worker_id="on-query")
    _query(session, environment, QueryRunStatus.RUNNING, worker_id="dead")
    _query(session, environment, QueryRunStatus.SUCCEEDED, worker_id="idle")
    _deploy(session, environment, DeployStatus.RUNNING, worker_id="on-deploy")
    _task_run(session, environment, "on-task")
    snapshot = with_work(session, alive_workers(session, db_now(session), 60))
    assert snapshot.for_query("interactive") == WorkerCounts(alive=4, taking=4, free=2)
    assert snapshot.for_task() == WorkerCounts(alive=4, taking=4, free=1)


def test_a_worker_with_a_stale_heartbeat_is_not_counted(
    session: Session, environment: Environment
) -> None:
    _worker(session, "w", 61)
    _query(session, environment, QueryRunStatus.RUNNING, worker_id="w")
    stale = with_work(session, alive_workers(session, db_now(session), 60))
    assert stale.for_query("interactive") == WorkerCounts(0, 0, 0)
    assert stale.for_task() == WorkerCounts(0, 0, 0)
    # Well inside the 60 s window: on a loaded machine a second can pass before the count is taken.
    _worker(session, "z", 30)
    fresh = with_work(session, alive_workers(session, db_now(session), 60))
    assert fresh.for_query("interactive") == WorkerCounts(1, 1, 1)
    assert fresh.for_task() == WorkerCounts(1, 1, 1)


def test_ahead_counts_queued_queries_and_deploys_of_every_environment_that_are_older(
    session: Session, environment: Environment
) -> None:
    other = Environment(
        id=uuid.uuid4(),
        name="prod",
        storage_root="file:///tmp/lakana-test-2",
        storage_options={},
        is_protected=False,
        properties={},
    )
    session.add(other)
    session.flush()
    base = db_now(session)
    at = [base + timedelta(seconds=n) for n in range(6)]
    _query(session, other, QueryRunStatus.QUEUED, created_at=at[0])
    _deploy(session, other, DeployStatus.QUEUED, created_at=at[1])
    _query(session, environment, QueryRunStatus.QUEUED, created_at=at[2])
    _query(session, environment, QueryRunStatus.RUNNING, created_at=at[0])
    _deploy(session, environment, DeployStatus.RUNNING, created_at=at[0])
    _query(session, environment, QueryRunStatus.QUEUED, created_at=at[4])
    _deploy(session, environment, DeployStatus.QUEUED, created_at=at[5])
    assert count_ahead(session, at[3]) == 3
    assert count_ahead(session, at[2]) == 2
    assert count_ahead(session, at[0]) == 0
    assert queued_times(session) == [at[0], at[1], at[2], at[4], at[5]]
    assert ahead_of(queued_times(session), at[3]) == count_ahead(session, at[3])


def test_queued_times_with_a_limit_are_exactly_the_oldest_of_both_kinds(
    session: Session, environment: Environment
) -> None:
    base = db_now(session)
    at = [base + timedelta(seconds=n) for n in range(6)]
    for n in (0, 2, 4):
        _query(session, environment, QueryRunStatus.QUEUED, created_at=at[n])
    for n in (1, 3, 5):
        _deploy(session, environment, DeployStatus.QUEUED, created_at=at[n])
    assert queued_times(session, 4) == at[:4]
    assert queued_times(session, 1) == at[:1]
    assert queued_times(session, 100) == at


def test_a_capped_count_says_more_than_and_states_the_cap() -> None:
    free = WorkerCounts(2, 2, 1)
    busy = WorkerCounts(2, 2, 0)
    at_cap = query_wait_reason(busy, 7, ahead_cap=7)
    assert (at_cap.ahead, "more than" in at_cap.message) == (7, False)
    over = query_wait_reason(busy, 8, ahead_cap=7)
    assert over.ahead == 7
    assert over.message == (
        "All 2 workers are busy, and more than 7 queries or deploys are ahead of this one."
    )
    assert query_wait_reason(free, 99, ahead_cap=7).message == (
        "A worker is free; more than 7 queries or deploys are ahead of this one, "
        "and it starts as soon as they are taken."
    )
    assert query_wait_reason(busy, 99).ahead == 99


def test_the_details_of_a_reason_hold_only_numbers_and_this_runs_own_data(
    session: Session, environment: Environment
) -> None:
    other = Environment(
        id=uuid.uuid4(),
        name="secret-env",
        storage_root="file:///tmp/lakana-test-3",
        storage_options={},
        is_protected=False,
        properties={},
    )
    session.add(other)
    session.flush()
    base = db_now(session)
    _worker(session, "secret-worker-host", 5)
    _query(session, other, QueryRunStatus.RUNNING, worker_id="secret-worker-host")
    _query(
        session,
        other,
        QueryRunStatus.QUEUED,
        sql_text="SELECT 'secret-sql'",
        created_at=base - timedelta(seconds=9),
    )
    snapshot = with_work(session, alive_workers(session, base, 60))
    counts = snapshot.for_query("interactive")
    reason = query_wait_reason(counts, count_ahead(session, base))
    body = reason.to_json()
    assert set(body) == {"code", "message", "workers", "ahead"}
    assert body["workers"] == 1
    assert body["ahead"] == 1
    assert all(isinstance(v, int) for k, v in body.items() if k not in ("code", "message"))
    text = json.dumps(body)
    for secret in ("secret-env", "secret-sql", "secret-worker-host", str(other.id)):
        assert secret not in text


def test_a_reason_is_a_value_object() -> None:
    reason = WaitReason(NO_WORKER, "m", workers=0)
    assert reason == WaitReason(NO_WORKER, "m", workers=0)


_PASSED_BY_MESSAGE = (
    "A worker waited too long for the lock that serialises the start of this user's queries, "
    "so workers pass them by until 14:00:30 UTC. This query starts then, at the latest; "
    "nothing needs doing."
)


def test_a_query_passed_by_after_a_lock_timeout_names_the_lock_instead_of_starting() -> None:
    """#95: a worker is free, but the principal is passed by until a time the database holds."""
    until = _NOW + timedelta(seconds=30)
    reason = query_wait_reason(c(2, 2, 1), 0, passed_by_until=until)
    assert LOCK_TIMEOUT == "lock_timeout"
    assert (reason.code, reason.message) == (LOCK_TIMEOUT, _PASSED_BY_MESSAGE)
    assert reason.to_json() == {
        "code": "lock_timeout",
        "message": _PASSED_BY_MESSAGE,
        "workers": 2,
        "next_attempt_at": until.isoformat(),
    }


def test_the_pass_by_comes_after_the_cases_that_hold_the_query_for_a_truer_reason() -> None:
    """No worker, no worker of the kind, the user limit and busy workers all come first."""
    until = _NOW + timedelta(seconds=30)
    assert query_wait_reason(c(0, 0, 0), 0, passed_by_until=until).code == NO_WORKER
    assert query_wait_reason(c(2, 0, 0), 0, passed_by_until=until).code == NO_WORKER_OF_KIND
    reached = u(2, 2)
    assert query_wait_reason(c(2, 2, 1), 0, limit=reached, passed_by_until=until).code == USER_LIMIT
    assert query_wait_reason(c(2, 2, 0), 3, passed_by_until=until).code == WORKERS_BUSY
    assert query_wait_reason(c(2, 2, 1), 0, passed_by_until=None).code == STARTING
