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

This module imports only the models and authorization, so the deploy service may use
`run_environments` without an import cycle.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import cast

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from lakana_core.auth import Action, AuthorizationService, PrincipalRef, Securable, permits_each
from lakana_core.models import LIVE_ENVIRONMENT, Environment, Repository, Run, RunStatus

FAILED_RUN_WINDOW = timedelta(days=7)
"""How far back a run's end counts."""

FAILED_RUN_LIMIT = 200
"""The most failed runs read at once."""

_FINISHED = (RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED)


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
    """

    run_id: uuid.UUID
    environment: str
    repository: str
    job: str | None
    finished_at: datetime


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
    its repository's other such runs); only the environments asked for are ranked, which
    changes no rank since each environment is ranked on its own.

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
    ranked = (
        select(
            Run.id,
            func.row_number()
            .over(
                partition_by=(Run.environment_id, Run.repository_id, Run.job),
                order_by=(Run.finished_at.desc(), Run.id.desc()),
            )
            .label("position"),
        )
        .where(Run.finished_at >= since, Run.status.in_(_FINISHED), Run.environment_id.in_(ids))
        .subquery()
    )
    rows = session.execute(
        select(Run.id, Environment.name, Repository.name, Run.job, Run.finished_at)
        .join(ranked, ranked.c.id == Run.id)
        .join(Repository, Repository.id == Run.repository_id)
        .join(Environment, Environment.id == Run.environment_id)
        .where(ranked.c.position == 1, Run.status == RunStatus.FAILED, LIVE_ENVIRONMENT)
        .order_by(Run.finished_at.desc(), Run.id.desc())
        .limit(limit)
    ).all()
    # The window's condition holds only for a run with an end.
    return [
        FailedRun(run_id, env, repository, job, cast("datetime", finished_at))
        for run_id, env, repository, job, finished_at in rows
    ]
