"""Which runs failed and need attention: one rule for Home and the Jobs page.

A job needs attention when its newest finished run within the window failed: the newest run
that ended (``succeeded``, ``failed`` or ``cancelled``) of each environment, repository and job,
or of each environment and repository for runs outside a job, is kept when it failed. So a later
success or a later cancelled run hides an earlier failure. Home's ``run_failed`` rows
(`lakana_core.home.attention`) and ``GET /api/v1/runs/failed`` (`RunService.failed_page`) both
read `failed_runs`, so the two never disagree.

The runs are listed to everyone who may read their environment (`READ_ENVIRONMENT`), as the
per-environment runs list: the live environments are asked in one `permits_each` batch, which
records nothing, and an environment the caller may not read is left out, never refused.

A job's failures in a row count once: `failure_streaks` ranks the rows of each object (a job's
runs, a repository's deploys, a check's results) in one statement and keeps, for each object
whose newest row failed, that row with the number of failures since the last row that did not
fail and when the first of them ended. Home's rows carry that count ("failed 3 times since
Tuesday").

This module imports only the models and authorization, so the deploy service may use
`run_environments` without an import cycle.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, TypeAlias, cast

from sqlalchemy import ColumnElement, Subquery, case, func, select
from sqlalchemy.orm import InstrumentedAttribute, Session

from lakana_core.auth import Action, AuthorizationService, PrincipalRef, Securable, permits_each
from lakana_core.models import LIVE_ENVIRONMENT, Environment, Repository, Run, RunStatus

FAILED_RUN_WINDOW = timedelta(days=7)
"""How far back a run's end counts."""

FAILED_RUN_LIMIT = 200
"""The most failed runs read at once."""

_FINISHED = (RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED)

_Column: TypeAlias = "ColumnElement[Any] | InstrumentedAttribute[Any]"
"""A column of a mapped class, or an expression, that `failure_streaks` ranks by."""


@dataclass(frozen=True)
class FailedRun:
    """A job's (or a repository's) newest finished run, which failed.

    Parameters
    ----------
    run_id
        The run.
    environment
        Its environment's name.
    repository
        Its repository's name.
    job
        Its job; `None` for a run outside a job.
    finished_at
        When it ended.
    failures
        How many runs of the job failed in a row within the window, this one included.
    first_failed_at
        When the first of those failed runs ended; `None` for a single failure.
    """

    run_id: uuid.UUID
    environment: str
    repository: str
    job: str | None
    finished_at: datetime
    failures: int = 1
    first_failed_at: datetime | None = None


def run_environments(
    session: Session, authz: AuthorizationService, p: PrincipalRef
) -> dict[uuid.UUID, str]:
    """Find the live environments whose runs the caller may list.

    The live environments are read once and `READ_ENVIRONMENT` on each is asked in one
    `permits_each` batch, one question per environment.

    Parameters
    ----------
    session
        Database session.
    authz
        Authorization service, asked with `permits_each` only.
    p
        The caller.

    Returns
    -------
    dict of uuid.UUID to str
        The environments' names, by id; empty when the caller may read none.
    """
    rows = session.execute(
        select(Environment.id, Environment.name).where(LIVE_ENVIRONMENT).order_by(Environment.name)
    ).all()
    if not rows:
        return {}
    allowed = permits_each(
        authz,
        p,
        [(Action.READ_ENVIRONMENT, Securable.environment_scope(name)) for _, name in rows],
    )
    return {env_id: name for (env_id, name), ok in zip(rows, allowed, strict=True) if ok}


def failure_streaks(
    row_id: _Column,
    finished_at: _Column,
    failed: ColumnElement[bool],
    partition: Sequence[_Column],
    where: Sequence[ColumnElement[bool]],
) -> Subquery:
    """Rank rows per object and keep the newest of each object whose newest row failed.

    The rows matching `where` are ranked per `partition`, newest end first (then by id). A
    running sum counts, for each row, the rows at or above it that did not fail, so the rows
    where it is still 0 are the failures since the last row that did not fail; they are counted
    and their oldest end taken per object, and the newest of them kept. An object whose newest
    row did not fail has none.

    Parameters
    ----------
    row_id
        The rows' id column.
    finished_at
        When each row ended.
    failed
        Whether a row failed.
    partition
        The columns that name an object (a job's environment, repository and job).
    where
        The rows that count: within the window, finished, in the environments asked for.

    Returns
    -------
    Subquery
        Columns ``id`` (the newest failed row of each object), ``failures`` (how many in a row)
        and ``first_failed_at`` (when the first of them ended).
    """
    order = (finished_at.desc(), row_id.desc())
    ranked = (
        select(
            row_id.label("id"),
            finished_at.label("finished_at"),
            *(column.label(f"part_{number}") for number, column in enumerate(partition)),
            func.row_number().over(partition_by=partition, order_by=order).label("position"),
            func.sum(case((failed, 0), else_=1))
            .over(partition_by=partition, order_by=order, rows=(None, 0))
            .label("passed_since"),
        )
        .where(*where)
        .subquery()
    )
    parts = [ranked.c[f"part_{number}"] for number in range(len(partition))]
    streak = (
        select(
            ranked.c.id,
            ranked.c.position,
            func.count().over(partition_by=parts).label("failures"),
            func.min(ranked.c.finished_at).over(partition_by=parts).label("first_failed_at"),
        )
        .where(ranked.c.passed_since == 0)
        .subquery()
    )
    return (
        select(streak.c.id, streak.c.failures, streak.c.first_failed_at)
        .where(streak.c.position == 1)
        .subquery()
    )


def failed_runs(
    session: Session,
    authz: AuthorizationService,
    p: PrincipalRef,
    *,
    since: datetime,
    environment_ids: Collection[uuid.UUID] | None = None,
    limit: int = FAILED_RUN_LIMIT,
) -> list[FailedRun]:
    """List the jobs whose newest finished run within the window failed, in one statement.

    The runs are ranked per environment, repository and job (a run outside a job ranks with
    its repository's other such runs) by `failure_streaks`, which also counts each job's
    failures in a row; only the environments asked for are ranked, which changes no rank since
    each environment is ranked on its own.

    Parameters
    ----------
    session
        Database session.
    authz
        Authorization service, asked only when `environment_ids` is `None`.
    p
        The caller.
    since
        The window's start: runs that ended before it do not count.
    environment_ids
        Only these environments, which the caller already may read; `None` for every
        environment the caller may read (`run_environments`).
    limit
        The most runs returned.

    Returns
    -------
    list of FailedRun
        Newest end first, then by run id; a run of a deleted environment is left out.
    """
    ids = list(run_environments(session, authz, p) if environment_ids is None else environment_ids)
    if not ids:
        return []
    streaks = failure_streaks(
        Run.id,
        Run.finished_at,
        Run.status == RunStatus.FAILED,
        (Run.environment_id, Run.repository_id, Run.job),
        (Run.finished_at >= since, Run.status.in_(_FINISHED), Run.environment_id.in_(ids)),
    )
    rows = session.execute(
        select(
            Run.id,
            Environment.name,
            Repository.name,
            Run.job,
            Run.finished_at,
            streaks.c.failures,
            streaks.c.first_failed_at,
        )
        .join(streaks, streaks.c.id == Run.id)
        .join(Repository, Repository.id == Run.repository_id)
        .join(Environment, Environment.id == Run.environment_id)
        .where(LIVE_ENVIRONMENT)
        .order_by(Run.finished_at.desc(), Run.id.desc())
        .limit(limit)
    ).all()
    # The window's condition holds only for a run with an end.
    return [
        FailedRun(
            run_id,
            env,
            repository,
            job,
            cast("datetime", finished_at),
            failures,
            first_failed_at if failures > 1 else None,
        )
        for run_id, env, repository, job, finished_at, failures, first_failed_at in rows
    ]
