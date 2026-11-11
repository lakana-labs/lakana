"""Your work: the repositories a person created or deployed, and their jobs, for Home.

Only for a person who may deploy somewhere (`GrantService.may_deploy_somewhere`); anyone else
gets nothing. A repository is the caller's when they created it (`Repository.created_by`) or
deployed it (`DeployRun.principal_id`). Its environments and jobs are shown only where the
caller may read the environment (`permits(READ_ENVIRONMENT)`), as the deploy and job views
are; every check is `permits`, which records nothing.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from lakana_core.auth import Action, AuthorizationService, PrincipalRef, Securable
from lakana_core.grants.service import GrantService
from lakana_core.identity.interface_state import refuse_tokens
from lakana_core.jobs.service import JobService
from lakana_core.models import (
    LIVE_ENVIRONMENT,
    DeployRun,
    Environment,
    EnvironmentDeployment,
    Repository,
)

WORK_LIMIT = 20
"""The most repositories, and the most jobs, Your work lists."""

_NEVER = datetime.min.replace(tzinfo=UTC)
"""Where a job that never ran sorts: after every job that did."""


@dataclass(frozen=True)
class WorkRepository:
    """A repository of the caller's.

    Parameters
    ----------
    name
        The repository's name.
    environments
        ``(environment, last deploy status, its time)`` for each environment the caller may
        read where it was deployed, by name; the status and time are `None` for a deployment
        without a deploy run.
    """

    name: str
    environments: tuple[tuple[str, str | None, datetime | None], ...]


@dataclass(frozen=True)
class WorkJob:
    """A job of one of the caller's repositories.

    Parameters
    ----------
    environment
        The environment's name.
    repository
        The repository's name.
    job
        The job's name.
    last_run_status
        The state of its newest run; `None` before the first.
    last_run_at
        When that run was created.
    """

    environment: str
    repository: str
    job: str
    last_run_status: str | None
    last_run_at: datetime | None


def _readable_environments(
    session: Session, authz: AuthorizationService, p: PrincipalRef
) -> dict[uuid.UUID, str]:
    """Find the live environments the caller may read.

    Parameters
    ----------
    session
        Database session.
    authz
        Authorization service, asked with `permits`.
    p
        The caller.

    Returns
    -------
    dict of uuid.UUID to str
        Their names, by id.
    """
    rows = session.execute(select(Environment.id, Environment.name).where(LIVE_ENVIRONMENT)).all()
    return {
        env_id: name
        for env_id, name in rows
        if authz.permits(p, Action.READ_ENVIRONMENT, Securable.environment_scope(name))
    }


def _last_deploys(
    session: Session, repo_ids: list[uuid.UUID], readable: dict[uuid.UUID, str]
) -> dict[uuid.UUID, dict[str, tuple[str | None, datetime | None]]]:
    """Read where each repository is deployed and how its newest deploy run there ended.

    Parameters
    ----------
    session
        Database session.
    repo_ids
        The repositories.
    readable
        The environments to look in.

    Returns
    -------
    dict
        By repository id, by environment name: the newest deploy run's status and time (its
        end, else its creation), or `None` twice for a deployment without a deploy run.
    """
    found: dict[uuid.UUID, dict[str, tuple[str | None, datetime | None]]] = {}
    envs = list(readable)
    for repo_id, env_id in session.execute(
        select(EnvironmentDeployment.repository_id, EnvironmentDeployment.environment_id).where(
            EnvironmentDeployment.repository_id.in_(repo_ids),
            EnvironmentDeployment.environment_id.in_(envs),
        )
    ):
        found.setdefault(repo_id, {})[readable[env_id]] = (None, None)
    ranked = (
        select(
            DeployRun.id,
            func.row_number()
            .over(
                partition_by=(DeployRun.repository_id, DeployRun.environment_id),
                order_by=(DeployRun.created_at.desc(), DeployRun.id.desc()),
            )
            .label("position"),
        )
        .where(DeployRun.repository_id.in_(repo_ids), DeployRun.environment_id.in_(envs))
        .subquery()
    )
    for run in session.execute(
        select(DeployRun).join(ranked, ranked.c.id == DeployRun.id).where(ranked.c.position == 1)
    ).scalars():
        found.setdefault(run.repository_id, {})[readable[run.environment_id]] = (
            run.status.value,
            run.finished_at or run.created_at,
        )
    return found


def your_work(
    session: Session, authz: AuthorizationService, jobs: JobService, p: PrincipalRef
) -> tuple[list[WorkRepository], list[WorkJob]]:
    """List the caller's repositories and their jobs.

    Parameters
    ----------
    session
        Database session; read only.
    authz
        Authorization service, asked with `permits` only.
    jobs
        The job service, asked only for environments the caller may read.
    p
        The caller.

    Returns
    -------
    tuple of (list of WorkRepository, list of WorkJob)
        At most `WORK_LIMIT` repositories, the most recently deployed (else created) first,
        and at most `WORK_LIMIT` of their jobs, the most recently run first; both empty for a
        caller who may deploy nowhere or read no environment.

    Raises
    ------
    PermissionDenied
        For a task or CI token.
    """
    refuse_tokens(p, "Your work")
    if not GrantService(session, authz).may_deploy_somewhere(p):
        return [], []
    readable = _readable_environments(session, authz, p)
    if not readable:
        return [], []
    deployed = select(DeployRun.repository_id).where(DeployRun.principal_id == p.id)
    moved = (
        select(
            DeployRun.repository_id,
            func.max(func.coalesce(DeployRun.finished_at, DeployRun.created_at)).label("at"),
        )
        .where(DeployRun.environment_id.in_(list(readable)))
        .group_by(DeployRun.repository_id)
        .subquery()
    )
    activity = func.coalesce(moved.c.at, Repository.created_at)
    repos = list(
        session.execute(
            select(Repository)
            .outerjoin(moved, moved.c.repository_id == Repository.id)
            .where(or_(Repository.created_by == p.id, Repository.id.in_(deployed)))
            .order_by(activity.desc(), Repository.name.desc())
            .limit(WORK_LIMIT)
        ).scalars()
    )
    deploys = _last_deploys(session, [repo.id for repo in repos], readable)
    shown = [
        WorkRepository(
            name=repo.name,
            environments=tuple(
                (env, status, at) for env, (status, at) in sorted(deploys.get(repo.id, {}).items())
            ),
        )
        for repo in repos
    ]
    found: list[WorkJob] = []
    for repo in shown:
        for env, *_ in repo.environments:
            found.extend(
                WorkJob(
                    environment=view.environment,
                    repository=view.repository,
                    job=view.name,
                    last_run_status=view.last_run_status,
                    last_run_at=view.last_run_at,
                )
                for view in jobs.list(p, env, repo.name)
            )
    found.sort(key=lambda job: (job.environment, job.repository, job.job))
    found.sort(key=lambda job: job.last_run_at or _NEVER, reverse=True)
    return shown, found[:WORK_LIMIT]
