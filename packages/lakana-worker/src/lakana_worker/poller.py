"""The dequeue loop: claims and runs one queued query, deploy or ready task run at a time.

What a worker takes depends on its `takes` (`WorkerPool`): `all` takes everything, in
the order below; `interactive` takes ad-hoc query runs only; `pipeline` takes deploys, ready
tasks and the query runs that belong to a task run or are maintenance. The kind of a query run
is `lakana_core.runs.pools`' one rule.

One exception: while a Python task's process runs, query runs are
still served (`only_queries`), so the task's own input queries can be run by
the worker that supervises it. No deploy and no second task is claimed meanwhile, and the
queries are those of the worker's own kinds: a pipeline worker serves the task's input
queries and leaves ad-hoc queries to an interactive worker.
"""

from __future__ import annotations

import functools
import logging
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import ColumnElement, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from lakana_core.auth import AuthorizationService
from lakana_core.config import QueryLimitsConfig
from lakana_core.db import db_now
from lakana_core.log_events import LogEvent, log_event
from lakana_core.models import DeployRun, DeployStatus, QueryRun, QueryRunStatus
from lakana_core.runs import pools
from lakana_core.runs.store import end_orphaned_runs
from lakana_core.runs.tasks import claim_task
from lakana_core.runs.workers import alive_takes
from lakana_core.secrets.keys import KeyProvider
from lakana_worker.activity import ACTIVITY
from lakana_worker.deploy import DeploySettings, run_deploy
from lakana_worker.pipeline import run_one
from lakana_worker.tasks import RunEngine, run_claimed

logger = logging.getLogger(__name__)

ORPHAN_CHECK_SECONDS = 5.0
"""Least time between two looks for this worker's orphaned runs (one indexed statement)."""

_monotonic = time.monotonic
"""The clock that paces the orphan check; tests replace it."""


PASS_BY_SECONDS = 30.0
"""How long a poller passes by the ad-hoc queries of a principal whose cap lock timed out."""


class PassedBy:
    """The principals this worker passes by for a while, after their cap lock timed out.

    A claim waits at most `pools.CAP_LOCK_TIMEOUT_SECONDS` for the lock that serialises one
    principal's claims. When that runs out, the oldest queued ad-hoc query is still the same
    one at the next poll, and a worker that took nothing else would wait on it again and again
    while other principals' queries stand behind it. The poller remembers the principal here and
    leaves its ad-hoc queries out of the next selections, as it does for a principal at the cap.
    An entry ends by time only. Pipeline work is never passed by. One instance belongs to one
    polling loop; nothing is shared between workers or kept in the module.

    Parameters
    ----------
    seconds
        How long a principal is passed by, counted from its latest timeout.
    clock
        Monotonic seconds; tests replace it.
    """

    def __init__(
        self, seconds: float = PASS_BY_SECONDS, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._seconds = seconds
        self._clock = clock
        self._until: dict[uuid.UUID, float] = {}

    def add(self, principal_id: uuid.UUID) -> None:
        """Pass a principal by from now on, for the full time.

        Parameters
        ----------
        principal_id
            The principal whose cap lock timed out.
        """
        self._until[principal_id] = self._clock() + self._seconds

    def active(self) -> list[uuid.UUID]:
        """List the principals passed by right now; the ones whose time is up are forgotten.

        Returns
        -------
        list[uuid.UUID]
            The principals.
        """
        now = self._clock()
        self._until = {who: until for who, until in self._until.items() if until > now}
        return list(self._until)


def _remember_timeout(session: Session, passed_by: PassedBy, principal_id: uuid.UUID) -> None:
    """Pass a principal by after its cap lock timed out, in memory and in the database.

    The database copy is what the wait reason reads (`waiting.LOCK_TIMEOUT`); failing to write
    it costs only that reason, so the error is logged and the loop goes on.

    Parameters
    ----------
    session
        The claim's session, whose transaction the timeout rolled back.
    passed_by
        The polling loop's memory.
    principal_id
        The principal whose cap lock timed out.
    """
    passed_by.add(principal_id)
    try:
        pools.pass_by(session, principal_id, PASS_BY_SECONDS)
    except SQLAlchemyError:
        session.rollback()
        logger.warning(
            "The pass-by of principal %s could not be recorded; its queued queries show no "
            "reason for it.",
            principal_id,
            exc_info=True,
        )


@dataclass(frozen=True)
class WorkerPool:
    """What this worker takes. `None` in its place takes everything.

    Parameters
    ----------
    takes
        `all`, `interactive` or `pipeline` (`LAKANA_WORKER__TAKES`).
    running_per_principal
        `LAKANA_QUERY_LIMITS__RUNNING_PER_PRINCIPAL`: `auto` or a whole number of 1 or more, the
        most ad-hoc queries one principal runs at once. `None` applies no cap.
    lease_seconds
        The lease length (`LAKANA_RUN__LEASE_SECONDS`): a worker is alive while its heartbeat is
        younger; `auto` counts the alive workers that take interactive work.
    """

    takes: str = pools.ALL
    running_per_principal: int | Literal["auto"] | None = None
    lease_seconds: int = 60


def _next_query(session: Session, takes: str, capped: Sequence[uuid.UUID] = ()) -> QueryRun | None:
    """Select the oldest queued query run this worker takes, passing by capped principals.

    One statement in every case, ordered by `created_at` with `LIMIT 1 FOR UPDATE SKIP LOCKED`:
    an `all` worker keeps one order across both kinds. A capped principal's ad-hoc runs are
    passed by; pipeline work never is.

    Parameters
    ----------
    session
        Database session.
    takes
        `all`, `interactive` or `pipeline`.
    capped
        Principals whose ad-hoc query runs are not selected: the cap's hint (the claim holds the
        cap itself).

    Returns
    -------
    QueryRun or None
        The locked run, or `None` when none of its kinds is queued.
    """
    conditions: list[ColumnElement[bool]] = [QueryRun.status == QueryRunStatus.QUEUED]
    passes_the_cap = or_(QueryRun.principal_id.is_(None), QueryRun.principal_id.not_in(capped))
    if takes == pools.INTERACTIVE:
        conditions.append(pools.is_interactive())
        if capped:
            conditions.append(passes_the_cap)
    elif takes == pools.PIPELINE:
        conditions.append(pools.is_pipeline())
    elif takes != pools.ALL:
        return None  # fail closed, as `takes_kind` does: an unknown value takes nothing
    elif capped:
        conditions.append(or_(pools.is_pipeline(), passes_the_cap))
    return session.execute(
        select(QueryRun)
        .where(*conditions)
        .order_by(QueryRun.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    ).scalar_one_or_none()


def end_own_orphans(session_factory: Callable[[], Session], worker_id: str) -> int:
    """End the `RUNNING` query runs that still carry this worker's id (I3).

    Called only by `poll_forever`, when this worker runs nothing, so every such run is one whose
    final status could not be written. See `lakana_core.runs.store.end_orphaned_runs`.

    Parameters
    ----------
    session_factory
        Opens a short-lived database session.
    worker_id
        This worker's id.

    Returns
    -------
    int
        The number of runs ended.
    """
    with session_factory() as session:
        return end_orphaned_runs(session, worker_id, db_now(session))


def poll_once(
    session: Session,
    authz: AuthorizationService,
    key_provider: KeyProvider | None = None,
    limits: QueryLimitsConfig | None = None,
    deploy: DeploySettings | None = None,
    engine: RunEngine | None = None,
    only_queries: bool = False,
    *,
    worker_id: str | None = None,
    may_claim: Callable[[], bool] | None = None,
    pool: WorkerPool | None = None,
    passed_by: PassedBy | None = None,
) -> bool:
    """Claim and run the oldest queued item across queries and deploys.

    Parameters
    ----------
    session
        Database session.
    authz
        Authorization service passed through to the runner.
    key_provider
        Passed through. `None` if no secrets are configured.
    limits
        Passed through to `run_one`.
    deploy
        Passed through to `run_deploy`; `None` uses `DeploySettings()`.
    engine
        Executes ready task runs after queries and deploys; `None`
        claims none. While its drain is set nothing at all is claimed, a
        query or a deploy neither: the drain is looked at after the queues
        are read, so a run queued after a SIGTERM that landed during the
        poll stays queued.
    only_queries
        Claim only query runs: used while this worker supervises a Python
        task, whose own input queries must be served. No deploy and
        no task is claimed.
    worker_id
        The id of this worker; handed to `run_one` and `run_deploy`, which write it on the
        run they start.
    may_claim
        Says whether this worker may claim work at all (its heartbeat is fresh). Asked before
        anything is read, so a worker that cannot reach the database for a lease takes no
        run; `None` always claims.
    pool
        What this worker takes; `None` takes everything. A worker that does not take pipeline
        work reads no deploy run and claims no task; one that takes interactive work only reads
        ad-hoc query runs. With a `running_per_principal` it passes by the queued ad-hoc runs of
        a principal that already has that many running, and hands the number to `run_one`,
        whose start update holds it exactly; without one (or without interactive work) no cap
        is resolved or applied.
    passed_by
        The principals whose cap lock timed out lately: their ad-hoc runs are passed by
        like those of a capped principal, and a timeout of this poll's claim is added to it.
        `None` remembers nothing.

    Returns
    -------
    bool
        `True` if an item was claimed and run (or its claim was refused: the poll did not idle),
        `False` if the queues of its kinds were empty, the worker may not claim, the engine is
        draining, or the memory watchdog is stopping it (`Activity.stopping`).
    """
    if may_claim is not None and not may_claim():
        return False
    if ACTIVITY.stopping:
        # The memory watchdog is stopping the worker: the loop idles until the exit. A child
        # started now would outlive the worker. What was claimed before (or by a poll that read
        # the flag just before it was set) is left to the lease sweep if the stop misses it.
        return False
    takes = pool.takes if pool is not None else pools.ALL
    pipeline_too = pools.takes_kind(takes, pools.PIPELINE)
    cap: int | None = None
    capped: Sequence[uuid.UUID] = ()
    if (
        pool is not None
        and pool.running_per_principal is not None
        and pools.takes_kind(takes, pools.INTERACTIVE)
    ):
        # Resolved once per poll, by the database's clock, so that the hint and the claim use
        # one number. A worker that takes no interactive work reads neither of these.
        alive = alive_takes(session, db_now(session), pool.lease_seconds)
        cap = pools.cap_for(pool.running_per_principal, pools.interactive_workers(alive.values()))
        capped = list(pools.running_by_principal(session, at_least=cap))
    if passed_by is not None:
        capped = [*capped, *passed_by.active()]
    query = _next_query(session, takes, capped)
    deploy_run = (
        None
        if only_queries or not pipeline_too
        else session.execute(
            select(DeployRun)
            .where(DeployRun.status == DeployStatus.QUEUED)
            .order_by(DeployRun.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        ).scalar_one_or_none()
    )
    if engine is not None and engine.drain.is_set():
        # Looked at again after the read, not only before the poll: a run queued after a
        # SIGTERM that landed during this poll is read here, and must stay queued. Rolling
        # back drops the row locks, so another worker can take what was read.
        session.rollback()
        return False
    if query is None and deploy_run is None:
        if only_queries or not pipeline_too or engine is None:
            return False
        claimed = claim_task(session, engine.settings.worker_id, engine.settings.lease_seconds)
        if claimed is None:
            return False

        def serve_queries() -> bool:
            """Run one query-only iteration on a fresh session.

            Returns
            -------
            bool
                Whether a query run was executed.
            """
            return poll_iteration(
                engine.session_factory,
                engine.authz_factory,
                key_provider,
                limits=limits,
                engine=engine,
                only_queries=True,
                worker_id=worker_id,
                may_claim=may_claim,
                pool=pool,
                passed_by=passed_by,
            )

        run_claimed(engine, claimed, serve_queries=serve_queries)
        return True
    if deploy_run is not None and (query is None or deploy_run.created_at <= query.created_at):
        run_deploy(
            session,
            deploy_run.id,
            authz,
            key_provider,
            deploy or DeploySettings(),
            worker_id=worker_id,
        )
        return True
    assert query is not None  # nosec B101 -- narrowed by the branch above
    run_one(
        session,
        query.id,
        authz,
        key_provider,
        limits=limits,
        worker_id=worker_id,
        running_cap=cap,
        on_cap_timeout=(
            None if passed_by is None else functools.partial(_remember_timeout, session, passed_by)
        ),
    )
    return True


def poll_iteration(
    session_factory: Callable[[], Session],
    authz_factory: Callable[[Session], AuthorizationService],
    key_provider: KeyProvider | None = None,
    limits: QueryLimitsConfig | None = None,
    deploy: DeploySettings | None = None,
    engine: RunEngine | None = None,
    only_queries: bool = False,
    *,
    worker_id: str | None = None,
    may_claim: Callable[[], bool] | None = None,
    pool: WorkerPool | None = None,
    passed_by: PassedBy | None = None,
) -> bool:
    """Run one `poll_once` against a fresh session, absorbing any failure.

    `run_one` already guarantees that a bad *query* cannot fail an
    iteration; this is the same invariant one layer up, for everything
    outside a query — a dropped database connection while claiming a row, a
    session that cannot be opened at all. A worker process that exits on the
    first transient error stops draining the queue for everyone.

    Parameters
    ----------
    session_factory
        Called to get a fresh `Session` for this iteration.
    authz_factory
        Called with that same session to build the authorization service
        for this iteration. A fresh call per iteration, not a shared
        instance, because an authorization service that queries the
        database (like `GrantAuthorization`) must be bound to the same
        session as the work it authorizes, and that session is itself
        fresh every iteration.
    key_provider
        Passed through to `poll_once`, unlike `authz_factory` not rebuilt
        per iteration — a `KeyProvider` holds no session state, so one
        instance safely serves the worker's whole lifetime.
    limits
        Passed through to `run_one`.
    deploy
        Passed through to `poll_once`.
    engine
        Passed through to `poll_once`.
    only_queries
        Passed through to `poll_once`.
    worker_id
        Passed through to `poll_once`.
    may_claim
        Passed through to `poll_once`.
    pool
        Passed through to `poll_once`.
    passed_by
        Passed through to `poll_once`.

    Returns
    -------
    bool
        `True` if an item was claimed and run. `False` if the queue was
        empty **or** the iteration failed, so the caller backs off either
        way rather than spinning on a persistent failure.
    """
    try:
        with session_factory() as session:
            return poll_once(
                session,
                authz_factory(session),
                key_provider,
                limits=limits,
                deploy=deploy,
                engine=engine,
                only_queries=only_queries,
                worker_id=worker_id,
                may_claim=may_claim,
                pool=pool,
                passed_by=passed_by,
            )
    except Exception:
        logger.exception(
            "Polling iteration failed; continuing.", extra=log_event(LogEvent.POLL_FAILED)
        )
        return False


def poll_forever(
    session_factory: Callable[[], Session],
    authz_factory: Callable[[Session], AuthorizationService],
    key_provider: KeyProvider | None = None,
    limits: QueryLimitsConfig | None = None,
    deploy: DeploySettings | None = None,
    engine: RunEngine | None = None,
    drain: threading.Event | None = None,
    poll_interval_seconds: float = 1.0,
    maintenance: Callable[[], None] | None = None,
    *,
    worker_id: str | None = None,
    may_claim: Callable[[], bool] | None = None,
    pool: WorkerPool | None = None,
) -> None:
    """Run `poll_iteration` in a loop until drained, sleeping between empty polls.

    Parameters
    ----------
    session_factory
        Called once per iteration to get a fresh `Session` — never held open
        across iterations, so a long-running worker does not hold one
        connection idle indefinitely.
    authz_factory
        Called once per iteration, with that iteration's session, to build
        the authorization service to use for it.
    key_provider
        Passed through to `poll_once`, unlike `authz_factory` not rebuilt
        per iteration — a `KeyProvider` holds no session state, so one
        instance safely serves the worker's whole lifetime.
    limits
        Passed through to `run_one`.
    deploy
        Passed through to `poll_once`.
    engine
        Passed through to `poll_once`.
    drain
        Set on SIGTERM; the loop returns before its next poll.
    poll_interval_seconds
        How long to sleep after an empty poll before trying again.
    maintenance
        Called before every poll and expected to pace itself (the sweep of
        orphaned attempt directories); a failure is logged and the loop
        goes on.
    worker_id
        Passed through to `poll_once`.
    may_claim
        Passed through to `poll_once`: a worker whose heartbeat is stale sleeps
        `poll_interval_seconds` per iteration, as on an empty queue.
    pool
        Passed through to `poll_once`: what this worker takes.

    Notes
    -----
    Before a poll, once after every item it ran and otherwise at most every
    `ORPHAN_CHECK_SECONDS`, a worker with an id that may claim and runs nothing ends the query
    runs that still carry its id (`end_own_orphans`): a run whose
    final status could not be written would otherwise stay `RUNNING` for as long as the worker
    lives and beats.
    """
    last_orphan_check: float | None = None
    passed_by = PassedBy()
    while drain is None or not drain.is_set():
        # Idle point: this loop is not inside a query, a deploy or a task (a query served beside
        # a Python task runs through `poll_iteration`, not through here), so a run that still
        # carries this worker's id is an orphan. Not when the worker may not claim (its heartbeat
        # is stale: the sweep owns its runs) or while ACTIVITY shows a query in flight.
        moment = _monotonic()
        if (
            worker_id is not None
            and (last_orphan_check is None or moment - last_orphan_check >= ORPHAN_CHECK_SECONDS)
            and (may_claim is None or may_claim())
            and not ACTIVITY.snapshot()[0]
        ):
            last_orphan_check = moment
            try:
                end_own_orphans(session_factory, worker_id)
            except Exception:
                logger.exception("Ending this worker's orphaned runs failed; continuing.")
        if maintenance is not None:
            try:
                maintenance()
            except Exception:
                logger.exception("Worker maintenance failed; continuing.")
        claimed = poll_iteration(
            session_factory,
            authz_factory,
            key_provider,
            limits=limits,
            deploy=deploy,
            engine=engine,
            worker_id=worker_id,
            may_claim=may_claim,
            pool=pool,
            passed_by=passed_by,
        )
        if claimed:
            last_orphan_check = None  # look once right after each item it ran
        else:
            time.sleep(poll_interval_seconds)
