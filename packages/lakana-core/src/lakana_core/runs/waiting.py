"""Why a queued query or a waiting task has not started: computed when asked, never stored.

Nothing here is a column and nothing is cached: a status read asks, and the answer is true at
the moment it is given. The numbers are estimates by construction:

- A worker that died still counts as alive until its lease is over, and counts as busy if it
  held a run; the sweep ends that run after the same lease.
- A worker that is draining counts as free while it finishes.
- A worker whose own claim gate is closed (it has not managed a heartbeat for a lease and has
  not yet been counted lost) still counts as free.
- Workers claim concurrently, so "ahead" is a count at one moment, not a promise.

"Free for a query" follows the worker's dequeue loop (`lakana_worker.poller`): it takes the
oldest queued query run or deploy run of every environment, one at a time, and a SQL task is a
query run. A worker that is only supervising a Python task still serves queries, so it is free
for a query. It claims a task only when no query and no deploy is queued and it holds no run, so
"free for a task" also excludes a worker that holds the lease of a running task.

Busy and free are counted for one kind of work (`lakana_core.runs.pools`): a worker that does
not take the kind is neither busy nor free for it. Two reasons exist only because of the kinds:
`no_worker_of_kind` (workers are alive and none takes this kind) and `user_limit` (the
submitter already runs as many ad-hoc queries as the cap allows). A third, `lock_timeout`, comes
from state in the database: a worker whose claim timed out on the submitter's lock writes a time
until which workers pass that user's ad-hoc queries by (`pools.pass_by`).

The codes are stable; the sentences may be reworded. The function cannot tell whether the
instance is local or shared, so the `no_worker` sentence names both next steps.
"""

from __future__ import annotations

import bisect
import dataclasses
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.orm import Session

from lakana_core.identifiers import shown
from lakana_core.models import DeployRun, DeployStatus, QueryRun, QueryRunStatus, TaskRun
from lakana_core.models.run import TaskStatus
from lakana_core.runs import pools
from lakana_core.runs.pools import ALL, INTERACTIVE, PIPELINE, takes_kind
from lakana_core.runs.workers import alive_takes

NO_WORKER = "no_worker"
"""No worker is alive."""
STARTING = "starting"
"""A worker is free and the work is about to be claimed."""
WORKERS_BUSY = "workers_busy"
"""Workers are alive that take this kind of work, and none is free."""
NO_WORKER_OF_KIND = "no_worker_of_kind"
"""Workers are alive and none takes this kind of work."""
USER_LIMIT = "user_limit"
"""The submitter runs as many ad-hoc queries as the limit per user allows."""
LOCK_TIMEOUT = "lock_timeout"
"""A worker is free, but the submitter is passed by for a few seconds: a worker waited too long
for the lock that serialises the start of that user's queries (`Principal.query_passed_by_until`).
"""
UPSTREAM = "upstream"
"""A task waits for upstream tasks of its run."""
RETRY = "retry"
"""A task failed and its next attempt is in the future."""

SETTING_BACK = "setting_back"
"""A task's table may hold rows no check has passed; the next worker sets it back first."""
SETTING_BACK_MESSAGE = (
    "This task's table may hold rows no check has passed; the next free worker sets the table back."
)
"""The sentence of `SETTING_BACK`: its worker was lost after it may have written its table, or
its run was cancelled while it waited with such rows in the table."""

JOB_WAITS = "job_waits"
"""A start of a job waits for the active run of its project to end."""
JOB_WAITS_MESSAGE = (
    "A start of this job waits for run {run} of this project to finish; it starts then."
)
JOB_STARTS_SOON = "job_starts_soon"
"""A start of a job waits, the project is free, and the scheduler has not looked yet."""
JOB_STARTS_SOON_MESSAGE = (
    "A start of this job is waiting and begins within a few seconds, when the scheduler next looks."
)
NO_RUN_PRINCIPAL = "no_run_principal"
"""A job has a schedule and the deployment has no run principal bound, so it does not start."""
NO_RUN_PRINCIPAL_JOB_MESSAGE = (
    "This job has a schedule but no run principal is bound, so it does not run by itself; an "
    "admin can bind one with `lakana deployments set-run-as`."
)
RUN_PRINCIPAL_UNUSABLE = "run_principal_unusable"
"""A job has a schedule and the bound run principal is inactive or not a service account."""
RUN_PRINCIPAL_UNUSABLE_JOB_MESSAGE = (
    "This job has a schedule, but the run principal bound to the deployment is inactive or is "
    "not a service account, so it does not run by itself; an admin can bind another with "
    "`lakana deployments set-run-as`."
)

MAX_UPSTREAM_NAMES = 5
"""Most upstream task names a sentence spells out; the rest become "and N more"."""

MAX_UPSTREAM_BODY = 50
"""Most upstream task names the body lists; `upstream_count` holds the total."""

_START_A_WORKER = (
    "Ask the operator to start a worker; on a local instance, start it again with "
    "'lakana server start --local'."
)


def attempts_used(attempt: int, max_attempts: int) -> int:
    """Return the attempt number to show: never above the limit.

    Every claim raises the stored counter, also a claim that only runs the checks of a task whose
    write is in the table. That is not a new attempt, so what the run view and the wait reason
    show stops at the limit.

    Parameters
    ----------
    attempt
        The stored attempt counter.
    max_attempts
        The task's attempt limit.

    Returns
    -------
    int
        `attempt`, or `max_attempts` when it is higher.
    """
    return min(attempt, max_attempts)


@dataclass(frozen=True)
class WorkerCounts:
    """How many workers are alive, and how many of them take and can start one kind of work.

    Parameters
    ----------
    alive
        Workers whose heartbeat is younger than the lease, whatever they take.
    taking
        Alive workers that take this kind of work.
    free
        Alive workers that take this kind and are free for it: for a query, no `RUNNING` query
        run or deploy run carries the worker's id; for a task, also no `RUNNING` task run's
        lease.
    others
        Alive workers that take only the other kind (known values of `takes`); `None` when not
        known, which means every alive worker that does not take this kind. It lets
        `no_worker_of_kind` avoid naming a kind for a worker whose `takes` is none of the
        three values. Not part of the comparison.
    """

    alive: int
    taking: int
    free: int
    others: int | None = field(default=None, compare=False)


@dataclass(frozen=True)
class UserLimit:
    """A principal's running ad-hoc queries against the limit per user, as one reader sees it.

    Parameters
    ----------
    running
        The principal's `RUNNING` interactive query runs, in every environment.
    limit
        The limit per user now (`pools.cap_for`).
    own
        Whether the reader is that principal. For anyone else (a manager) the reason carries
        the limit and never the number running.
    """

    running: int
    limit: int
    own: bool = True


@dataclass(frozen=True)
class WaitReason:
    """Why something waits: a stable code, one sentence, and the numbers the sentence uses.

    Parameters
    ----------
    code
        `no_worker`, `no_worker_of_kind`, `user_limit`, `lock_timeout`, `starting`, `workers_busy`,
        `upstream`, `retry` or `setting_back`; for a job, `job_waits` or `no_run_principal`.
    message
        One plain sentence that names the cause and, where there is one, the next step.
    workers
        Alive workers that take this kind of work (instance-wide); 0 for `no_worker_of_kind`.
    ahead
        Queued queries and deploys, of every environment, that the workers for this kind take
        before this query: the same kind, and the other kind too while a worker takes `all`.
    upstream
        The first 50 unfinished upstream tasks of the same run that the reader may read, raw
        names (the sentence names at most five, escaped).
    upstream_count
        How many unfinished upstream tasks there are in all, the hidden ones included.
    hidden_upstream
        How many of them the reader may not read: counted, never named. Unset when there are
        none.
    attempt
        Attempts made so far.
    max_attempts
        Attempts allowed.
    next_attempt_at
        When the next attempt may start, as stored (UTC); for `lock_timeout`, until when the
        submitter is passed by.
    other_workers
        For `no_worker_of_kind`: the alive workers, all of which take only the other kind.
    running
        For `user_limit`: the principal's own running ad-hoc queries; never set for a reader
        who is not that principal.
    limit
        For `user_limit`: the limit per user.
    """

    code: str
    message: str
    workers: int | None = None
    ahead: int | None = None
    upstream: tuple[str, ...] | None = None
    upstream_count: int | None = None
    hidden_upstream: int | None = None
    attempt: int | None = None
    max_attempts: int | None = None
    next_attempt_at: datetime | None = None
    other_workers: int | None = None
    running: int | None = None
    limit: int | None = None

    def to_json(self) -> dict[str, object]:
        """Build the REST body: `code`, `message` and only the fields this code uses.

        Returns
        -------
        dict[str, object]
            The body; `upstream` is a list and `next_attempt_at` ISO text.
        """
        body: dict[str, object] = {"code": self.code, "message": self.message}
        for name in (
            "workers",
            "ahead",
            "other_workers",
            "running",
            "limit",
            "upstream_count",
            "hidden_upstream",
            "attempt",
            "max_attempts",
        ):
            value = getattr(self, name)
            if value is not None:
                body[name] = value
        if self.upstream is not None:
            body["upstream"] = list(self.upstream)
        if self.next_attempt_at is not None:
            body["next_attempt_at"] = self.next_attempt_at.isoformat()
        return body


@dataclass(frozen=True)
class WaitingTask:
    """What the reason of a task needs to know about it.

    Parameters
    ----------
    status
        The task's status value; only `pending` and `ready` wait.
    attempt
        Attempts counted so far.
    max_attempts
        Retries + 1.
    next_attempt_at
        When a pending retry may start.
    unfinished_upstream
        Tasks of the same run that this one waits for, that have not succeeded, and whose tables
        the reader may read.
    blocked
        An upstream task failed, was cancelled or is `upstream_failed`: the scheduler is about
        to mark this task, so it is not "waiting for" anything.
    checking
        The table is built and the task waits for its checks to run again: it has checks and its
        write's stamp is set (a rolled-back task has none, and its table was set back). Its retry
        says so instead of counting attempts.
    setting_back
        The task's table may hold rows no check has passed (its worker was lost after it may have
        written, or its run was cancelled while it waited with such rows), and the next claim
        only sets the table back. A ready task says so unless no worker could take it:
        starting one is then what helps.
    hidden_upstream
        How many more such tasks there are whose tables the reader may not read: counted in
        the reason, never named.
    """

    status: str
    attempt: int
    max_attempts: int
    next_attempt_at: datetime | None
    unfinished_upstream: Sequence[str]
    blocked: bool = False
    checking: bool = False
    setting_back: bool = False
    hidden_upstream: int = 0


@dataclass(frozen=True)
class WorkerSnapshot:
    """The alive workers with what they take, and the ids that carry work.

    Parameters
    ----------
    takes
        What each alive worker takes, by worker id (`workers.alive_takes`).
    on_queries
        Worker ids carried by a `RUNNING` query run or deploy run; an id that is not alive
        counts nowhere.
    on_tasks
        Worker ids that hold the lease of a `RUNNING` task run.
    """

    takes: Mapping[str, str]
    on_queries: frozenset[str] = frozenset()
    on_tasks: frozenset[str] = frozenset()

    def _taking(self, kind: str) -> set[str]:
        """Pick the alive workers that take one kind of work.

        Parameters
        ----------
        kind
            `INTERACTIVE` or `PIPELINE`.

        Returns
        -------
        set[str]
            Their ids.
        """
        return {worker for worker, takes in self.takes.items() if takes_kind(takes, kind)}

    def _only(self, kind: str) -> int:
        """Count the alive workers whose `takes` is exactly one kind.

        Parameters
        ----------
        kind
            `INTERACTIVE` or `PIPELINE`.

        Returns
        -------
        int
            How many.
        """
        return sum(1 for takes in self.takes.values() if takes == kind)

    def serves_both(self) -> bool:
        """Say whether an alive worker takes both kinds, so that both wait in one line.

        Returns
        -------
        bool
            `True` while a worker's `takes` is `all`.
        """
        return ALL in self.takes.values()

    def interactive(self) -> int:
        """Count the alive workers that take interactive work: the N of the `auto` cap.

        Returns
        -------
        int
            The count, `all` workers included.
        """
        return pools.interactive_workers(self.takes.values())

    def for_query(self, kind: str) -> WorkerCounts:
        """Count the workers for a queued query run of one kind.

        Parameters
        ----------
        kind
            `INTERACTIVE` or `PIPELINE`.

        Returns
        -------
        WorkerCounts
            Free: takes the kind and carries no `RUNNING` query run or deploy run.
        """
        taking = self._taking(kind)
        return WorkerCounts(
            len(self.takes),
            len(taking),
            len(taking - self.on_queries),
            self._only(INTERACTIVE if kind == PIPELINE else PIPELINE),
        )

    def for_task(self) -> WorkerCounts:
        """Count the workers for a ready task, which is pipeline work.

        Returns
        -------
        WorkerCounts
            Free: takes pipeline work, is free for a query and holds no `RUNNING` task's lease.
        """
        taking = self._taking(PIPELINE)
        return WorkerCounts(
            len(self.takes),
            len(taking),
            len(taking - self.on_queries - self.on_tasks),
            self._only(INTERACTIVE),
        )


def _no_worker_of_kind(counts: WorkerCounts, kind: str, thing: str) -> WaitReason:
    """Say that workers are alive and none takes this kind (`alive > 0`, `taking == 0`).

    Parameters
    ----------
    counts
        The counts for this kind.
    kind
        `INTERACTIVE` or `PIPELINE`: what the thing needs.
    thing
        `query`, `maintenance run` or `task`: what cannot start.

    Returns
    -------
    WaitReason
        The reason; every alive worker takes the other kind only.
    """
    other = PIPELINE if kind == INTERACTIVE else INTERACTIVE
    known = counts.alive if counts.others is None else counts.others
    named = other if known == counts.alive else "other"
    others = "1 worker takes" if counts.alive == 1 else f"{counts.alive} workers take"
    return WaitReason(
        NO_WORKER_OF_KIND,
        f"No worker that takes {kind} work is running ({others} {named} work only), so this "
        f"{thing} cannot start. Ask the operator to start a worker with "
        f"LAKANA_WORKER__TAKES={kind} or all.",
        workers=0,
        other_workers=counts.alive,
    )


def _busy(counts: WorkerCounts, kind: str) -> str:
    """Say that every worker for this kind is busy; name the kind only when it narrows.

    Parameters
    ----------
    counts
        The counts for this kind (`taking > 0`).
    kind
        The kind, named when another alive worker does not take it.

    Returns
    -------
    str
        The start of a sentence.
    """
    one = counts.taking == 1
    named = "" if counts.taking == counts.alive else f" that take{'s' if one else ''} {kind} work"
    return (
        f"The only worker{named} is busy" if one else f"All {counts.taking} workers{named} are busy"
    )


def _user_limit(limit: UserLimit) -> WaitReason:
    """Say that the principal is at or above the limit (`running >= limit`).

    Parameters
    ----------
    limit
        The principal's count and the limit, and whether the reader is that principal.

    Returns
    -------
    WaitReason
        The reason. A reader who is not the principal gets the limit and never the count.
    """
    n, above = limit.limit, limit.running > limit.limit
    if not limit.own:
        per = "1 running query" if n == 1 else f"{n} running queries"
        tail = "none of theirs is running" if n == 1 else f"fewer than {n} of theirs are running"
        message = (
            f"Its submitter has reached the limit of {per} per user; it can start when {tail}."
        )
        return WaitReason(USER_LIMIT, message, limit=n)
    if above:
        tail = "none is running" if n == 1 else f"fewer than {n} are running"
        message = (
            f"You have {limit.running} queries running and the limit per user here is {n}; this "
            f"one can start when {tail}. 'lakana query list' shows them."
        )
    elif n == 1:
        message = (
            "You have 1 query running, which is the limit per user here; this one can start "
            "when it ends. 'lakana query list' shows it."
        )
    else:
        message = (
            f"You have {n} queries running, which is the limit per user here; this one can "
            "start when one of them ends. 'lakana query list' shows them."
        )
    return WaitReason(USER_LIMIT, message, running=limit.running, limit=n)


def settled_early(counts: WorkerCounts, limit: UserLimit | None) -> bool:
    """Say whether the first three cases decide, so that the reader need not count what is ahead.

    The limit decides only while a worker that takes the kind is free: with every such worker
    busy the limit holds nothing the workers do not already hold, and `workers_busy` is the
    truth. `counts` must therefore know what the workers carry (`with_work`) for the limit.

    Parameters
    ----------
    counts
        The counts for the run's kind.
    limit
        The principal's count against the limit, or `None` when the run is not capped.

    Returns
    -------
    bool
        `True` for no worker alive, no worker that takes the kind, or a reached limit while a
        worker that takes the kind is free.
    """
    return (
        counts.alive == 0
        or counts.taking == 0
        or (limit is not None and limit.running >= limit.limit and counts.free > 0)
    )


def query_wait_reason(
    counts: WorkerCounts,
    ahead: int,
    *,
    kind: str = INTERACTIVE,
    maintenance: bool = False,
    limit: UserLimit | None = None,
    ahead_cap: int | None = None,
    passed_by_until: datetime | None = None,
    thing: str = "query",
) -> WaitReason:
    """Say why a `QUEUED` query run has not started, tested in this order.

    No worker alive; workers alive and none takes this kind; the principal is at or above the
    limit while a worker that takes this kind is free (the limit is then what holds the run;
    with every such worker busy the reason is `workers_busy`, as without a limit); a worker that
    takes this kind is free and the submitter is passed by after a lock timeout
    (`lock_timeout`); a worker that takes this kind is free (`starting`, whatever is ahead: the
    sentence then says how many are); otherwise `workers_busy`. For up to a lease after a worker
    died, it still counts as alive and, if it held a run, as busy; a draining worker counts as
    free.

    Parameters
    ----------
    counts
        The workers for `kind`, from `WorkerSnapshot.for_query`.
    ahead
        Queued work that the workers for this kind take before this run.
    kind
        `INTERACTIVE` or `PIPELINE`: the run's kind.
    maintenance
        The run is a maintenance run, which only changes what the sentence calls it.
    limit
        The principal's count against the limit, or `None` for a run that is not capped (not
        interactive, or without a principal). The kind is never read to decide the limit. It
        counts only while `counts.free` is above 0.
    ahead_cap
        For a listing that counted only up to a cap: when `ahead` is above it, the reason says
        "more than N" and `ahead` is N. `None` (the status read) counts exactly.
    passed_by_until
        The time, still in the future, until which workers pass the submitter's ad-hoc queries
        by (`Principal.query_passed_by_until`), or `None`. It counts only while a worker that
        takes this kind is free; with every such worker busy, `workers_busy` is the truth.
    thing
        What waits, as the sentences call it: ``query``, or ``deploy`` for a deploy run (which is
        pipeline work and never capped).

    Returns
    -------
    WaitReason
        The reason; `workers` and `ahead` are the numbers it states.
    """
    if counts.alive == 0:
        return WaitReason(
            NO_WORKER,
            f"No worker is running, so this {thing} cannot start. {_START_A_WORKER}",
            workers=0,
        )
    if counts.taking == 0:
        return _no_worker_of_kind(counts, kind, "maintenance run" if maintenance else thing)
    if limit is not None and limit.running >= limit.limit and counts.free > 0:
        return _user_limit(limit)
    if passed_by_until is not None and counts.free > 0:
        return WaitReason(
            LOCK_TIMEOUT,
            "A worker waited too long for the lock that serialises the start of this user's "
            f"queries, so workers pass them by until {passed_by_until.astimezone(UTC):%H:%M:%S} "
            "UTC. This query starts then, at the latest; nothing needs doing.",
            workers=counts.taking,
            next_attempt_at=passed_by_until,
        )
    if ahead_cap is not None and ahead > ahead_cap:
        ahead = ahead_cap
        queue = f"more than {ahead_cap} queries or deploys are ahead of this one"
    elif ahead == 0:
        queue = "nothing is ahead of this one"
    elif ahead == 1:
        queue = "about 1 query or deploy is ahead of this one"
    else:
        queue = f"about {ahead} queries or deploys are ahead of this one"
    if counts.free > 0:
        message = (
            f"A worker is free; the {thing} starts within about a second."
            if ahead == 0
            else f"A worker is free; {queue}, and it starts as soon as they are taken."
        )
        return WaitReason(STARTING, message, workers=counts.taking, ahead=ahead)
    return WaitReason(
        WORKERS_BUSY, f"{_busy(counts, kind)}, and {queue}.", workers=counts.taking, ahead=ahead
    )


def _upstream_sentence(names: Sequence[str], hidden: int) -> str:
    """Say which upstream tasks a task waits for, counting those the reader may not read.

    Parameters
    ----------
    names
        The unfinished upstream tasks the reader may read.
    hidden
        How many unfinished upstream tasks the reader may not read.

    Returns
    -------
    str
        One sentence: at most five names, escaped, then how many more, then the hidden count.
    """
    noun = "table" if hidden == 1 else "tables"
    if not names:
        return f"Waiting for {hidden} upstream {noun} you may not read."
    listed = ", ".join(shown(name) for name in names[:MAX_UPSTREAM_NAMES])
    more = len(names) - MAX_UPSTREAM_NAMES
    if not hidden:
        return f"Waiting for upstream tasks: {listed}{f' and {more} more' if more > 0 else ''}."
    if more > 0:
        listed = f"{listed}, {more} more"
    return f"Waiting for upstream tasks: {listed} and {hidden} {noun} you may not read."


def task_wait_reason(task: WaitingTask, counts: WorkerCounts, now: datetime) -> WaitReason | None:
    """Say why a `pending` or `ready` task has not started.

    Tasks are pipeline work and are never capped.

    Parameters
    ----------
    task
        The task.
    counts
        The workers, from `WorkerSnapshot.for_task`.
    now
        The database's current time.

    Returns
    -------
    WaitReason or None
        `None` for a task in any other status.
    """
    if task.status not in ("pending", "ready"):
        return None
    pending = task.status == "pending"
    if pending and task.blocked:
        return None
    if pending and (task.unfinished_upstream or task.hidden_upstream):
        names = tuple(task.unfinished_upstream)
        return WaitReason(
            UPSTREAM,
            _upstream_sentence(names, task.hidden_upstream),
            upstream=names[:MAX_UPSTREAM_BODY],
            upstream_count=len(names) + task.hidden_upstream,
            hidden_upstream=task.hidden_upstream or None,
        )
    if pending and task.next_attempt_at is not None and task.next_attempt_at > now:
        when = task.next_attempt_at.astimezone(UTC)
        if task.checking:
            return WaitReason(
                RETRY,
                f"The table is built; its checks could not finish and run again at "
                f"{when:%H:%M:%S} UTC.",
                next_attempt_at=task.next_attempt_at,
            )
        used = attempts_used(task.attempt, task.max_attempts)
        return WaitReason(
            RETRY,
            f"Attempt {used} of {task.max_attempts} failed; the next one starts at "
            f"{when:%H:%M:%S} UTC.",
            attempt=used,
            max_attempts=task.max_attempts,
            next_attempt_at=task.next_attempt_at,
        )
    if counts.alive == 0:
        return WaitReason(
            NO_WORKER,
            f"No worker is running, so this task cannot start. {_START_A_WORKER}",
            workers=0,
        )
    if counts.taking == 0:
        return _no_worker_of_kind(counts, PIPELINE, "task")
    if not pending and task.setting_back:
        return WaitReason(SETTING_BACK, SETTING_BACK_MESSAGE)
    if not pending and counts.free == 0:
        busy = _busy(counts, PIPELINE)
        return WaitReason(
            WORKERS_BUSY,
            f"Ready, and {busy[0].lower()}{busy[1:]}. Queued queries and deploys are served first.",
            workers=counts.taking,
        )
    return WaitReason(STARTING, "Starts within about a second.")


def alive_workers(session: Session, now: datetime, lease_seconds: int) -> WorkerSnapshot:
    """Read the alive workers with what they take: one statement.

    Parameters
    ----------
    session
        Database session.
    now
        The database's current time.
    lease_seconds
        The lease length (`LAKANA_RUN__LEASE_SECONDS`).

    Returns
    -------
    WorkerSnapshot
        The workers, carrying no work yet (`with_work`).
    """
    return WorkerSnapshot(takes=alive_takes(session, now, lease_seconds))


def with_work(session: Session, snapshot: WorkerSnapshot) -> WorkerSnapshot:
    """Read which workers carry work: three indexed statements, none when no worker is alive.

    Parameters
    ----------
    session
        Database session.
    snapshot
        The alive workers (`alive_workers`).

    Returns
    -------
    WorkerSnapshot
        The same workers with the ids of those that carry a `RUNNING` query run or deploy run
        and those that hold a `RUNNING` task's lease; `snapshot` itself when it is empty.
    """
    if not snapshot.takes:
        return snapshot
    on_queries = set(
        session.scalars(
            select(QueryRun.worker_id)
            .where(QueryRun.status == QueryRunStatus.RUNNING, QueryRun.worker_id.is_not(None))
            .distinct()
        )
    ) | set(
        session.scalars(
            select(DeployRun.worker_id)
            .where(DeployRun.status == DeployStatus.RUNNING, DeployRun.worker_id.is_not(None))
            .distinct()
        )
    )
    on_tasks = set(
        session.scalars(
            select(TaskRun.lease_owner)
            .where(TaskRun.status == TaskStatus.RUNNING, TaskRun.lease_owner.is_not(None))
            .distinct()
        )
    )
    return dataclasses.replace(
        snapshot, on_queries=frozenset(on_queries), on_tasks=frozenset(on_tasks)
    )


def ahead_kind(snapshot: WorkerSnapshot, kind: str) -> str | None:
    """Say which queued work is ahead of a run of this kind: its own, or everything.

    While an alive worker takes `all`, the workers that take this kind also take the other
    kind's queued work first-come first-served, so all of it is ahead.

    Parameters
    ----------
    snapshot
        The alive workers.
    kind
        The run's kind.

    Returns
    -------
    str or None
        `None` (count every kind) while a worker takes `all`, else `kind`.
    """
    return None if snapshot.serves_both() else kind


def _of_kind(kind: str | None) -> tuple[ColumnElement[bool], ...]:
    """Build the condition on a query run that selects one kind.

    Parameters
    ----------
    kind
        `None`, `INTERACTIVE` or `PIPELINE`.

    Returns
    -------
    tuple[ColumnElement[bool], ...]
        Nothing for `None`, else the kind's condition (`pools`).
    """
    if kind == INTERACTIVE:
        return (pools.is_interactive(),)
    if kind == PIPELINE:
        return (pools.is_pipeline(),)
    return ()


def count_ahead(session: Session, created_at: datetime, kind: str | None = None) -> int:
    """Count the queued query runs and deploy runs, of every environment, older than a time.

    One statement; the query part uses the index on `query_runs (status, created_at)`. The
    dequeue order is global, so no environment filter. Only a number leaves this function.

    Parameters
    ----------
    session
        Database session.
    created_at
        The `created_at` of the query being asked about.
    kind
        `None` counts every kind. `INTERACTIVE` counts the interactive query runs; `PIPELINE`
        the pipeline query runs and the deploy runs (deploys are pipeline work).

    Returns
    -------
    int
        Queued runs with an earlier `created_at`.
    """
    query_count = (
        select(func.count())
        .select_from(QueryRun)
        .where(
            QueryRun.status == QueryRunStatus.QUEUED,
            QueryRun.created_at < created_at,
            *_of_kind(kind),
        )
        .scalar_subquery()
    )
    if kind == INTERACTIVE:
        return int(session.execute(select(query_count)).scalar_one())
    deploys = (
        select(func.count())
        .select_from(DeployRun)
        .where(DeployRun.status == DeployStatus.QUEUED, DeployRun.created_at < created_at)
        .scalar_subquery()
    )
    return int(session.execute(select(query_count + deploys)).scalar_one())


def queued_times(
    session: Session, limit: int | None = None, *, kind: str | None = None
) -> list[datetime]:
    """Read the `created_at` of the queued query runs and deploy runs, all environments.

    For a listing that needs the number ahead of many queries from one read: two statements,
    one for interactive work.

    Parameters
    ----------
    session
        Database session.
    limit
        Load only the oldest `limit` of them (the queries part by the index on
        `query_runs (status, created_at)`); `None` loads all. The oldest `limit` of both kinds
        together are exactly the first `limit` of the result, so `ahead_of` is exact for any
        row with fewer than `limit` ahead, and a row with `limit` or more ahead gets `limit`.
    kind
        `None` reads every kind. `INTERACTIVE` reads the interactive query runs; `PIPELINE` the
        pipeline query runs and the deploy runs.

    Returns
    -------
    list of datetime
        The times, oldest first, at most `limit`.
    """
    queries = select(QueryRun.created_at).where(
        QueryRun.status == QueryRunStatus.QUEUED, *_of_kind(kind)
    )
    if limit is not None:
        queries = queries.order_by(QueryRun.created_at).limit(limit)
    times = list(session.scalars(queries))
    if kind != INTERACTIVE:
        deploys = select(DeployRun.created_at).where(DeployRun.status == DeployStatus.QUEUED)
        if limit is not None:
            deploys = deploys.order_by(DeployRun.created_at).limit(limit)
        times = sorted([*times, *session.scalars(deploys)])
    return times if limit is None else times[:limit]


def ahead_of(times: Sequence[datetime], created_at: datetime) -> int:
    """Count the sorted times that are strictly older than one.

    Parameters
    ----------
    times
        Sorted times, from `queued_times`.
    created_at
        The time of the query being asked about; an equal time counts as not ahead.

    Returns
    -------
    int
        How many are older.
    """
    return bisect.bisect_left(times, created_at)
