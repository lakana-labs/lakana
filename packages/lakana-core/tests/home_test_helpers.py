"""Rows the Home tests build directly: people, grants, jobs, runs and queries.

Plain functions, imported by `test_home_recent.py` and `test_home_get_started.py`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.auth import Action, PrincipalRef, SecurableKind
from lakana_core.grants.roles import Role
from lakana_core.models import (
    Environment,
    EnvironmentDeployment,
    Grant,
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
)


def person(session: Session, name: str, kind: PrincipalType = PrincipalType.USER) -> PrincipalRef:
    """Persist an active principal.

    Parameters
    ----------
    session
        Database session.
    name
        Its name.
    kind
        Its type.

    Returns
    -------
    PrincipalRef
        Its reference.
    """
    ref = PrincipalRef(id=uuid.uuid4(), name=name, type=kind)
    session.add(Principal(id=ref.id, type=kind, name=name, is_active=True))
    session.commit()
    return ref


def environment(session: Session, name: str = "dev") -> uuid.UUID:
    """Persist an environment, or find the one of that name.

    Parameters
    ----------
    session
        Database session.
    name
        Its name.

    Returns
    -------
    uuid.UUID
        Its id.
    """
    found = session.execute(select(Environment.id).where(Environment.name == name)).scalar()
    if found is not None:
        return found
    row = Environment(
        id=uuid.uuid4(), name=name, storage_root="file:///dev/null", storage_options={}
    )
    session.add(row)
    session.commit()
    return row.id


def grant(
    session: Session,
    who: PrincipalRef,
    *,
    role: Role | None = None,
    action: Action | None = None,
    environment_name: str | None = None,
) -> Grant:
    """Grant a role or an action on an environment, or on the instance without one.

    Parameters
    ----------
    session
        Database session.
    who
        The grantee; it grants itself, which the tests do not mind.
    role
        The role, or `None` for an action.
    action
        The action, or `None` for a role.
    environment_name
        The environment; `None` for the instance.

    Returns
    -------
    Grant
        The row.
    """
    row = Grant(
        id=uuid.uuid4(),
        principal_id=who.id,
        role=role,
        action=action,
        kind=SecurableKind.INSTANCE if environment_name is None else SecurableKind.ENVIRONMENT,
        environment=environment_name,
        environment_id=None if environment_name is None else environment(session, environment_name),
        granted_by=who.id,
        granted_at=datetime.now(UTC),
    )
    session.add(row)
    session.commit()
    return row


def deployed_job(
    session: Session, environment_name: str, repository: str, job: str
) -> tuple[Repository, JobVersion]:
    """Persist a repository deployed to an environment with one job.

    Parameters
    ----------
    session
        Database session.
    environment_name
        The environment.
    repository
        The repository's name.
    job
        The job's name.

    Returns
    -------
    tuple of (Repository, JobVersion)
        The repository and its current version there.
    """
    env_id = environment(session, environment_name)
    repo = Repository(
        id=uuid.uuid4(), name=repository, kind=RepositoryKind.HTTPS, url=f"https://h/{repository}"
    )
    session.add(repo)
    session.flush()
    version = JobVersion(
        id=uuid.uuid4(),
        repository_id=repo.id,
        environment_id=env_id,
        commit_sha="a" * 40,
        manifest_json={"jobs": {job: {"assets": []}}},
        code_uri="file:///dev/null",
        code_sha256="0" * 64,
    )
    session.add(version)
    session.flush()
    session.add(
        EnvironmentDeployment(
            environment_id=env_id, repository_id=repo.id, current_job_version_id=version.id
        )
    )
    session.commit()
    return repo, version


def run_of(session: Session, repo: Repository, version: JobVersion) -> Run:
    """Persist a finished run of a deployed repository.

    Parameters
    ----------
    session
        Database session.
    repo
        The repository.
    version
        Its version.

    Returns
    -------
    Run
        The row.
    """
    run = Run(
        id=uuid.uuid4(),
        environment_id=version.environment_id,
        repository_id=repo.id,
        job_version_id=version.id,
        kind=RunKind.FULL,
        status=RunStatus.SUCCEEDED,
    )
    session.add(run)
    session.commit()
    return run


def query_of(
    session: Session,
    who: PrincipalRef,
    environment_name: str = "dev",
    *,
    status: QueryRunStatus = QueryRunStatus.SUCCEEDED,
    pipeline: Run | None = None,
) -> QueryRun:
    """Persist a query run of a person, ad hoc or for a task of a pipeline run.

    Parameters
    ----------
    session
        Database session.
    who
        The submitter.
    environment_name
        The environment.
    status
        Its state.
    pipeline
        A run whose SQL task the query executes; `None` for an ad-hoc query.

    Returns
    -------
    QueryRun
        The row.
    """
    task_run_id = None
    if pipeline is not None:
        task = TaskRun(
            id=uuid.uuid4(),
            run_id=pipeline.id,
            asset="silver.clean",
            status=TaskStatus.SUCCEEDED,
            max_attempts=1,
            timeout_seconds=60,
            retry_delay_seconds=0,
        )
        session.add(task)
        session.flush()
        task_run_id = task.id
    row = QueryRun(
        id=uuid.uuid4(),
        environment_id=environment(session, environment_name),
        principal_id=who.id,
        sql_text="SELECT 1",
        status=status,
        task_run_id=task_run_id,
    )
    session.add(row)
    session.commit()
    return row
