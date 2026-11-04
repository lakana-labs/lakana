"""Name lookups shared by the deploy and run services.

One implementation of "find the environment, repository or deployment pointer
or raise 404", so the two services cannot drift apart in what they disclose.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.errors import NotFoundError
from lakana_core.models import (
    LIVE_ENVIRONMENT,
    Environment,
    EnvironmentDeployment,
    JobVersion,
    Repository,
)


def live_environment_names(session: Session) -> list[str]:
    """List the names of the environments that are not deleted.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    list of str
        The names, in no particular order.
    """
    return list(session.execute(select(Environment.name).where(LIVE_ENVIRONMENT)).scalars())


def environment_by_name(session: Session, name: str) -> Environment:
    """Look an environment up by name.

    Parameters
    ----------
    session
        Database session.
    name
        Environment name.

    Returns
    -------
    Environment
        The row.

    Raises
    ------
    NotFoundError
        If none has that name.
    """
    env = session.execute(
        select(Environment).where(Environment.name == name, LIVE_ENVIRONMENT)
    ).scalar_one_or_none()
    if env is None:
        raise NotFoundError(f"No environment named {name!r}.")
    return env


def current_manifests(
    session: Session, environment_id: uuid.UUID, *, exclude: uuid.UUID | None = None
) -> list[tuple[uuid.UUID, str, object]]:
    """Read the current project version of every repository deployed to an environment.

    Parameters
    ----------
    session
        Database session.
    environment_id
        The environment.
    exclude
        A repository to leave out, or `None` to include every repository.

    Returns
    -------
    list of tuple
        ``(project version id, repository name, manifest)``, ordered by repository name. The
        manifest is untrusted: read it with `lakana_core.projects.manifest`'s readers.
    """
    query = (
        select(JobVersion.id, Repository.name, JobVersion.manifest_json)
        .join(EnvironmentDeployment, EnvironmentDeployment.repository_id == Repository.id)
        .join(JobVersion, JobVersion.id == EnvironmentDeployment.current_job_version_id)
        .where(EnvironmentDeployment.environment_id == environment_id)
        .order_by(Repository.name)
    )
    if exclude is not None:
        query = query.where(EnvironmentDeployment.repository_id != exclude)
    return [(version_id, name, manifest) for version_id, name, manifest in session.execute(query)]


def repository_by_name(session: Session, name: str) -> Repository:
    """Look a repository up by name.

    Parameters
    ----------
    session
        Database session.
    name
        Repository name.

    Returns
    -------
    Repository
        The row.

    Raises
    ------
    NotFoundError
        If none has that name.
    """
    repo = session.execute(select(Repository).where(Repository.name == name)).scalar_one_or_none()
    if repo is None:
        raise NotFoundError(f"No repository named {name!r}.")
    return repo


def deployment_row(session: Session, env: Environment, repo: Repository) -> EnvironmentDeployment:
    """Return the deployment pointer of a repository in an environment.

    Parameters
    ----------
    session
        Database session.
    env
        The environment.
    repo
        The repository.

    Returns
    -------
    EnvironmentDeployment
        The row, freshly read.

    Raises
    ------
    NotFoundError
        If the repository is not deployed there.
    """
    row = session.execute(
        select(EnvironmentDeployment)
        .where(
            EnvironmentDeployment.environment_id == env.id,
            EnvironmentDeployment.repository_id == repo.id,
        )
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError(f"Repository {repo.name!r} is not deployed in {env.name!r}.")
    return row
