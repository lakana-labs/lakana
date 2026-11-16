"""Count the rows a data quality check returns, as a query run of the run's principal.

A check is a SELECT that must return no rows. The worker wraps it into a count
(`lakana_core.projects.checks.count_query`) and runs that as an ordinary read through
`pipeline.run_one`: the same authorization per table, the same row filters and column masks,
the same query child and the same limits as any query of that principal. The one difference is
that the result is handed back to the worker (`on_result`) and never stored.

This module knows nothing of tasks, leases or verdicts: `lakana_worker.tasks` decides what a
count means.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.auth import AuthorizationService
from lakana_core.config import QueryLimitsConfig
from lakana_core.db import db_now
from lakana_core.models import QueryOrigin, QueryRun, QueryRunStatus
from lakana_core.projects.checks import CheckSpec, count_query
from lakana_core.secrets.keys import KeyProvider
from lakana_worker.pipeline import ReadOutcome, run_one


@dataclass(frozen=True)
class Counted:
    """What counting one check gave.

    Parameters
    ----------
    query_run_id
        The query run that counted.
    rows
        The rows the check returned; `None` when the count did not succeed.
    failure
        Why the count did not succeed, for its type only: its text is unredacted. `None` for a
        count that succeeded.
    error_message
        The stored, redacted `QueryRun.error_message` of a failed count.
    error_cause
        The stored `QueryRun.error_cause` of a failed count.
    checked_snapshot_id
        The snapshot of the checked table the count read; `None` for a view, an external table
        and a failed count.
    policy_applied
        Whether a row filter or a column mask of the principal applied to any table the check
        read.
    started_at
        When the count started, by the database's clock.
    """

    query_run_id: uuid.UUID
    rows: int | None
    failure: BaseException | None
    error_message: str | None
    error_cause: str | None
    checked_snapshot_id: int | None
    policy_applied: bool
    started_at: datetime


def count_check(
    session: Session,
    *,
    spec: CheckSpec,
    environment_id: uuid.UUID,
    principal_id: uuid.UUID,
    task_run_id: uuid.UUID,
    worker_id: str,
    authz: AuthorizationService,
    key_provider: KeyProvider | None,
    limits: QueryLimitsConfig,
) -> Counted:
    """Count the rows one check returns, as a query run of the run's principal.

    The run is inserted `RUNNING` under the worker's id, as the write run of a SQL task is, so
    that `run_one`'s claim matches it and no other worker takes it. It carries the task run
    (so it is pipeline work, and a lost lease ends it), and it is never bound as the task's
    `query_run_id`: that stays the task's write.

    Parameters
    ----------
    session
        Database session; commits.
    spec
        The check.
    environment_id
        The run's environment.
    principal_id
        The run's principal: who the count is authorized and filtered as.
    task_run_id
        The task run the check belongs to.
    worker_id
        This worker's id.
    authz
        The authorization service.
    key_provider
        Decrypts secret-backed storage.
    limits
        The query limits the count runs under.

    Returns
    -------
    Counted
        The count, or why there is none. A run that did not end `SUCCEEDED`, or that handed
        back no result, is a failure whatever `run_one` returned: nothing here turns a missing
        count into "no rows".

    Raises
    ------
    ValidationError
        If the check's SQL is not one SELECT (a manifest edited by hand); nothing is submitted.
    """
    sql = count_query(spec.sql)
    started = db_now(session)
    run = QueryRun(
        id=uuid.uuid4(),
        environment_id=environment_id,
        principal_id=principal_id,
        sql_text=sql,
        status=QueryRunStatus.RUNNING,
        origin=QueryOrigin.CHECK,
        task_run_id=task_run_id,
        worker_id=worker_id,
    )
    session.add(run)
    session.commit()
    run_id = run.id
    seen: list[ReadOutcome] = []
    failure = run_one(
        session,
        run_id,
        authz,
        key_provider,
        limits=limits,
        worker_id=worker_id,
        on_result=seen.append,
    )
    session.rollback()
    stored = session.execute(
        select(QueryRun.status, QueryRun.error_message, QueryRun.error_cause).where(
            QueryRun.id == run_id
        )
    ).one()
    if stored.status is not QueryRunStatus.SUCCEEDED or not seen:
        return Counted(
            run_id,
            None,
            failure or RuntimeError("the count did not succeed"),
            stored.error_message,
            stored.error_cause,
            None,
            False,
            started,
        )
    outcome = seen[0]
    return Counted(
        run_id,
        int(outcome.table.column(0)[0].as_py()),
        None,
        None,
        None,
        outcome.snapshots.get(spec.table),
        bool(outcome.restricted),
        started,
    )
