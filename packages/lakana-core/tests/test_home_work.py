"""Your work: the repositories a person created or deployed, and their jobs."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from home_test_helpers import deployed_job, environment, finished_run, grant, person
from sqlalchemy.orm import Session

from lakana_core.auth import Action, PrincipalRef
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.grants.roles import Role
from lakana_core.home.work import WORK_LIMIT, WorkJob, WorkRepository, your_work
from lakana_core.jobs.service import JobService
from lakana_core.models import (
    DeployKind,
    DeployRun,
    DeployStatus,
    Repository,
    RepositoryKind,
    RunStatus,
)

_START = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)


@pytest.fixture
def deployer(session: Session) -> PrincipalRef:
    """Persist a data engineer who may deploy to `dev`.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    PrincipalRef
        Their reference.
    """
    ref = person(session, "dana")
    grant(session, ref, role=Role.DATA_ENGINEER, environment_name="dev")
    grant(session, ref, action=Action.DEPLOY, environment_name="dev")
    return ref


def _deploy(
    session: Session,
    repo: Repository,
    who: PrincipalRef,
    environment_name: str,
    *,
    status: DeployStatus,
    at: datetime,
) -> None:
    """Persist a deploy run of a repository.

    Parameters
    ----------
    session
        Database session.
    repo
        The repository.
    who
        The deployer.
    environment_name
        Where it deployed.
    status
        How it ended.
    at
        When it ended.
    """
    session.add(
        DeployRun(
            id=uuid.uuid4(),
            environment_id=environment(session, environment_name),
            repository_id=repo.id,
            principal_id=who.id,
            kind=DeployKind.DEPLOY,
            status=status,
            created_at=at - timedelta(minutes=1),
            finished_at=at,
        )
    )
    session.commit()


def _work(session: Session, p: PrincipalRef) -> tuple[list[WorkRepository], list[WorkJob]]:
    """Ask for a person's work with real authorization.

    Parameters
    ----------
    session
        Database session.
    p
        The person.

    Returns
    -------
    tuple of (list of WorkRepository, list of WorkJob)
        The answer.
    """
    authz = GrantAuthorization(session)
    return your_work(session, authz, JobService(session, authz), p)


def test_work_lists_repositories_created_or_deployed_by_the_caller(
    session: Session, deployer: PrincipalRef
) -> None:
    created = Repository(
        id=uuid.uuid4(),
        name="mine",
        kind=RepositoryKind.HTTPS,
        url="https://h/mine",
        created_by=deployer.id,
        created_at=_START,
    )
    session.add(created)
    session.commit()
    shop, version = deployed_job(session, "dev", "shop", "nightly")
    _deploy(session, shop, deployer, "dev", status=DeployStatus.SUCCEEDED, at=_START + timedelta(1))
    _deploy(session, shop, deployer, "prod", status=DeployStatus.FAILED, at=_START + timedelta(2))
    run = finished_run(
        session, shop, version, job="nightly", status=RunStatus.FAILED, finished_at=_START
    )
    deployed_job(session, "dev", "theirs", "hourly")
    repositories, jobs = _work(session, deployer)
    assert repositories == [
        WorkRepository(
            name="shop",
            environments=(("dev", "succeeded", _START + timedelta(1)),),
        ),
        WorkRepository(name="mine", environments=()),
    ]
    assert jobs == [
        WorkJob(
            environment="dev",
            repository="shop",
            job="nightly",
            last_run_status=run.status.value,
            last_run_at=_START,
        )
    ]


def test_work_cuts_each_list_at_its_limit(session: Session, deployer: PrincipalRef) -> None:
    for number in range(WORK_LIMIT + 2):
        repo, _ = deployed_job(session, "dev", f"r{number:02d}", f"j{number:02d}")
        _deploy(
            session,
            repo,
            deployer,
            "dev",
            status=DeployStatus.SUCCEEDED,
            at=_START + timedelta(minutes=number),
        )
    repositories, jobs = _work(session, deployer)
    assert (len(repositories), repositories[0].name) == (WORK_LIMIT, f"r{WORK_LIMIT + 1:02d}")
    assert len(jobs) == WORK_LIMIT


def test_work_is_empty_for_a_reader(session: Session) -> None:
    reader = person(session, "rita")
    grant(session, reader, role=Role.ANALYST, environment_name="dev")
    repo, _ = deployed_job(session, "dev", "shop", "nightly")
    repo.created_by = reader.id
    session.commit()
    assert _work(session, reader) == ([], [])


def test_work_is_empty_without_reading_any_environment(session: Session) -> None:
    only_deploy = person(session, "otto")
    grant(session, only_deploy, action=Action.DEPLOY, environment_name="dev")
    repo, _ = deployed_job(session, "dev", "shop", "nightly")
    repo.created_by = only_deploy.id
    session.commit()
    assert _work(session, only_deploy) == ([], [])
