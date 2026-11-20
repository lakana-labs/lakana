"""`lakana_core.runs.connection_failures`: the newest run that failed on a connection."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from lakana_core.auth import AllowAllAuthorization, PrincipalRef, SecurableKind
from lakana_core.connections.kinds import ConnectionKind
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.grants.roles import Role
from lakana_core.grants.service import GrantService
from lakana_core.models import (
    Connection,
    Environment,
    JobVersion,
    Principal,
    PrincipalType,
    Repository,
    RepositoryKind,
    Run,
    RunKind,
    RunStatus,
    TaskRun,
    TaskStatus,
)
from lakana_core.runs.connection_failures import LastRunFailure, last_run_failures, run_readers

_T0 = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


@dataclass
class _World:
    """Two environments, one repository each, and a connection `crm-db` in each.

    Parameters
    ----------
    session
        Database session.
    principal
        Who created everything.
    environments
        Environment name to its id.
    repositories
        Environment name to the id of its repository `analytics`.
    versions
        Environment name to the id of its project version.
    connections
        Environment name to its connection `crm-db`.
    """

    session: Session
    principal: PrincipalRef
    environments: dict[str, uuid.UUID]
    repositories: dict[str, uuid.UUID]
    versions: dict[str, uuid.UUID]
    connections: dict[str, Connection]

    def task(
        self,
        environment: str,
        asset: str,
        status: TaskStatus,
        minutes: int,
        connection: str | None = None,
        repository: uuid.UUID | None = None,
        task_id: uuid.UUID | None = None,
    ) -> TaskRun:
        """Store a run with one finished task.

        Parameters
        ----------
        environment
            The run's environment.
        asset
            The task's asset.
        status
            How the task ended.
        minutes
            When it ended, in minutes after `_T0`.
        connection
            The connection its failure names.
        repository
            The run's repository; the environment's `analytics` when `None`.
        task_id
            The task's id; a new one when `None`.

        Returns
        -------
        TaskRun
            The task.
        """
        repo = repository if repository is not None else self.repositories[environment]
        version = self.versions[environment]
        if repository is not None:
            version = uuid.uuid4()
            self.session.add(
                JobVersion(
                    id=version,
                    repository_id=repo,
                    environment_id=self.environments[environment],
                    commit_sha="b" * 40,
                    manifest_json={"version": 3, "assets": [], "edges": []},
                    code_uri="file:///y",
                    code_sha256="1" * 64,
                )
            )
        run = Run(
            id=uuid.uuid4(),
            environment_id=self.environments[environment],
            repository_id=repo,
            job_version_id=version,
            kind=RunKind.FULL,
            status=RunStatus.FAILED,
            selection_json={},
        )
        self.session.add(run)
        task = TaskRun(
            id=task_id if task_id is not None else uuid.uuid4(),
            run_id=run.id,
            asset=asset,
            status=status,
            max_attempts=1,
            timeout_seconds=60,
            retry_delay_seconds=0,
            upstream_json=[],
            failure_connection=connection,
            finished_at=_T0 + timedelta(minutes=minutes),
        )
        self.session.add(task)
        self.session.commit()
        return task

    def failures(self) -> dict[str, LastRunFailure]:
        """Read the failure of each environment's `crm-db`.

        Returns
        -------
        dict
            Environment name to its connection's failure, for those that have one.
        """
        found = last_run_failures(self.session, list(self.connections.values()))
        return {env: found[conn.id] for env, conn in self.connections.items() if conn.id in found}


@pytest.fixture
def world(session: Session) -> _World:
    """Build the world.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    _World
        The world.
    """
    principal = PrincipalRef(id=uuid.uuid4(), name="dana", type=PrincipalType.USER)
    session.add(Principal(id=principal.id, type=principal.type, name="dana", is_active=True))
    environments, repositories, versions, connections = {}, {}, {}, {}
    for name in ("dev", "prod"):
        env = Environment(id=uuid.uuid4(), name=name, storage_root=f"file:///srv/{name}")
        repo = Repository(
            id=uuid.uuid4(),
            name=f"analytics-{name}",
            kind=RepositoryKind.HTTPS,
            url="https://h/r.git",
        )
        session.add_all([env, repo])
        session.flush()
        version = JobVersion(
            id=uuid.uuid4(),
            repository_id=repo.id,
            environment_id=env.id,
            commit_sha="a" * 40,
            manifest_json={"version": 3, "assets": [], "edges": []},
            code_uri="file:///x",
            code_sha256="0" * 64,
        )
        connection = Connection(
            id=uuid.uuid4(),
            environment_id=env.id,
            name="crm-db",
            kind=ConnectionKind.POSTGRESQL,
            address={"host": "db.example.com", "port": 5432, "database": "crm"},
            settings={"user": "reader"},
            created_by=principal.id,
            created_at=_T0,
            updated_at=_T0,
        )
        session.add_all([version, connection])
        environments[name], repositories[name] = env.id, repo.id
        versions[name], connections[name] = version.id, connection
    session.commit()
    return _World(session, principal, environments, repositories, versions, connections)


def test_the_newest_failure_that_names_the_connection_is_shown(world: _World) -> None:
    """Of the failures naming it in its environment, the newest; nothing of the others."""
    world.task("dev", "silver.a", TaskStatus.FAILED, 1, "crm-db")
    newest = world.task("dev", "silver.b", TaskStatus.FAILED, 5, "crm-db")
    world.task("dev", "silver.c", TaskStatus.FAILED, 9, "other-db")
    world.task("dev", "silver.d", TaskStatus.FAILED, 10)
    world.task("dev", "silver.e", TaskStatus.CANCELLED, 11, "crm-db")

    assert world.failures() == {
        "dev": LastRunFailure(newest.run_id, "silver.b", _T0 + timedelta(minutes=5))
    }


def test_a_later_ending_of_the_same_asset_hides_the_failure(world: _World) -> None:
    """A later success or failure of the asset means its last run did not fail on it."""
    world.task("dev", "silver.a", TaskStatus.FAILED, 1, "crm-db")
    world.task("dev", "silver.a", TaskStatus.SUCCEEDED, 2)
    world.task("prod", "silver.a", TaskStatus.FAILED, 1, "crm-db")
    world.task("prod", "silver.a", TaskStatus.FAILED, 3)

    assert world.failures() == {}


def test_only_a_later_ending_of_that_asset_in_that_repository_hides_it(world: _World) -> None:
    """Another asset, another repository, a cancel or an older run hides nothing."""
    world.task("dev", "silver.a", TaskStatus.SUCCEEDED, 0)
    failed = world.task("dev", "silver.a", TaskStatus.FAILED, 1, "crm-db")
    world.task("dev", "silver.b", TaskStatus.SUCCEEDED, 2)
    world.task("dev", "silver.a", TaskStatus.CANCELLED, 3)
    other = Repository(
        id=uuid.uuid4(), name="other", kind=RepositoryKind.HTTPS, url="https://h/o.git"
    )
    world.session.add(other)
    world.session.commit()
    world.task("dev", "silver.a", TaskStatus.SUCCEEDED, 4, repository=other.id)
    world.task("prod", "silver.a", TaskStatus.SUCCEEDED, 5)

    assert world.failures() == {
        "dev": LastRunFailure(failed.run_id, "silver.a", _T0 + timedelta(minutes=1))
    }


@pytest.mark.parametrize("failure_first", [True, False], ids=["failure-id-larger", "smaller"])
def test_a_success_at_the_same_moment_hides_the_failure(world: _World, failure_first: bool) -> None:
    """Equal times: the success counts as the later ending, whichever task id is larger."""
    low, high = uuid.UUID(int=1), uuid.UUID(int=2)
    world.task(
        "dev", "silver.a", TaskStatus.FAILED, 1, "crm-db", task_id=high if failure_first else low
    )
    world.task("dev", "silver.a", TaskStatus.SUCCEEDED, 1, task_id=low if failure_first else high)

    assert world.failures() == {}


def test_of_two_failures_at_the_same_moment_the_larger_task_id_is_the_newest(
    world: _World,
) -> None:
    """Equal times and equal endings: the larger task id decides, so the answer is stable."""
    world.task("dev", "silver.a", TaskStatus.FAILED, 1, task_id=uuid.UUID(int=1))
    named = world.task("dev", "silver.a", TaskStatus.FAILED, 1, "crm-db", task_id=uuid.UUID(int=2))

    assert world.failures() == {
        "dev": LastRunFailure(named.run_id, "silver.a", _T0 + timedelta(minutes=1))
    }


def test_a_failure_before_the_connections_last_change_is_not_shown(world: _World) -> None:
    """The change may have fixed it; a connection deleted and made again is a new one."""
    world.task("dev", "silver.a", TaskStatus.FAILED, 1, "crm-db")
    world.connections["dev"].updated_at = _T0 + timedelta(minutes=2)
    world.session.commit()
    assert world.failures() == {}

    after = world.task("dev", "silver.b", TaskStatus.FAILED, 3, "crm-db")
    assert world.failures() == {
        "dev": LastRunFailure(after.run_id, "silver.b", _T0 + timedelta(minutes=3))
    }


def test_no_connection_reads_nothing(session: Session) -> None:
    assert last_run_failures(session, []) == {}


def test_the_runs_of_an_environment_are_read_by_whoever_may_read_a_run_there(
    session: Session,
) -> None:
    """`RUN` or `READ_ENVIRONMENT`, as `authorize_run_read`; anyone else reads none."""
    for name in ("dev", "prod"):
        session.add(Environment(id=uuid.uuid4(), name=name, storage_root=f"file:///srv/{name}"))
    people = {}
    for name in ("admin", "alice", "nobody"):
        people[name] = PrincipalRef(id=uuid.uuid4(), name=name, type=PrincipalType.USER)
        session.add(Principal(id=people[name].id, type=PrincipalType.USER, name=name))
    session.commit()
    grants = GrantService(session, AllowAllAuthorization())
    grants.grant(people["admin"], principal="admin", role=Role.ADMIN, kind=SecurableKind.INSTANCE)
    grants.grant(
        people["admin"],
        principal="alice",
        role=Role.ANALYST,
        kind=SecurableKind.ENVIRONMENT,
        environment="prod",
    )
    authz = GrantAuthorization(session)

    assert run_readers(authz, people["alice"], ["prod", "dev", "prod"]) == {"prod"}
    assert run_readers(authz, people["nobody"], ["prod", "dev"]) == set()
    assert run_readers(authz, people["admin"], ["dev"]) == {"dev"}
    assert run_readers(authz, people["nobody"], []) == set()
