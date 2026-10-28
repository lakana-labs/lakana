"""Jobs on Postgres: starts that wait, the scheduler's fires and the deploys they race.

SQLite has one writer and no row locks, so only a second real session can show the order of the
locks. A deploy locks the deployment's pointer first and the job rows after it, so a start,
a park, a pause and a resume take them in that order and read the manifest after the pointer
is theirs: a job the deploy removed is a 404, never a row that comes back. The deploy is held at
the statement that deletes the row (`StatementPause`), and the start is held at the entrance of
`JobService._park` (a wrapper the test installs; nothing in the product). The scheduler's fires
and waiting starts are driven with a fixed time (`fire_due(session, NOW)`), and each test arms the
one schedule it uses, so the real clock and the other tests' rows do not matter. One test does
the opposite: `test_a_due_schedule_is_fired_by_the_leader_and_a_worker_builds_its_table` runs the
worker's own `SchedulerLoop` on the database's clock, as production does, against a schedule that
is already due, and lets a worker build the job's table.
"""

from __future__ import annotations

import shutil
import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from lakana_core.auth import PrincipalRef
from lakana_core.db import db_now
from lakana_core.errors import NotFoundError
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.jobs.scheduler import FireOutcome, fire_due, start_waiting_jobs
from lakana_core.jobs.service import JobService
from lakana_core.models import (
    AuditEvent,
    AuditEventType,
    EnvironmentDeployment,
    JobFire,
    JobSchedule,
    Principal,
    PrincipalType,
    Repository,
    Run,
    RunStatus,
    RunTrigger,
    TaskRun,
    TaskStatus,
)
from lakana_worker.deploy import DeploySettings
from lakana_worker.poller import poll_once
from lakana_worker.python_support import PythonSupport
from lakana_worker.scheduler import SchedulerLoop
from lakana_worker.tasks import RunEngine, TaskSettings
from tests.integration_helpers import (
    HoldPoint,
    StatementPause,
    commit_files,
    run_in_thread,
    thread_result,
    unique_name,
    wait_for_lock_waiter,
)
from tests.python_asset_helpers import QueryStack, query_stack

pytestmark = pytest.mark.integration

_YAML = (
    "defaults:\n  retry_delay: 0\n  retries: 1\n"
    "jobs:\n"
    "  keep:\n    select: ['silver.*']\n"
    "  gone:\n    select: ['silver.*']\n    schedule:\n      cron: '0 3 * * *'\n"
)
_YAML_WITHOUT = _YAML.split("  gone:")[0]
_FILES = {"assets/silver/x.sql": "SELECT id FROM bronze.nums\n"}


@pytest.fixture(scope="module")
def stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[QueryStack]:
    """Start Postgres, SeaweedFS and a real HTTP server.

    Parameters
    ----------
    tmp_path_factory
        For the repo root.

    Yields
    ------
    QueryStack
        The stack.
    """
    with query_stack(tmp_path_factory) as built:
        yield built


@pytest.fixture(autouse=True)
def _no_leftovers(stack: QueryStack) -> None:
    """End every active run, so that one test's run does not make another's project busy.

    Parameters
    ----------
    stack
        The stack.
    """
    with Session(stack.db) as session:
        active = [RunStatus.QUEUED, RunStatus.RUNNING]
        session.execute(
            update(Run).where(Run.status.in_(active)).values(status=RunStatus.CANCELLED)
        )
        session.execute(
            update(TaskRun)
            .where(TaskRun.status.in_([TaskStatus.PENDING, TaskStatus.READY, TaskStatus.RUNNING]))
            .values(status=TaskStatus.CANCELLED)
        )
        session.commit()


NOW = datetime(2026, 1, 1, 3, 0, 30, tzinfo=UTC)
FIRE = datetime(2026, 1, 1, 3, 0, tzinfo=UTC)

_JOBS = (
    "defaults:\n  retry_delay: 0\n  retries: 1\n"
    "jobs:\n"
    "  fast:\n    select: ['silver.*']\n    schedule:\n      cron: '0 3 * * *'\n"
    "  slow:\n    select: ['silver.*']\n"
    "  gone:\n    select: ['silver.*']\n"
)
_JOBS_WITHOUT_GONE = _JOBS.split("  gone:")[0]
_JOBS_WITHOUT_FAST = _JOBS.split("  fast:")[0] + "  slow:\n    select: ['silver.*']\n"


@pytest.fixture(autouse=True)
def _quiet_schedules(stack: QueryStack) -> None:
    """Disarm every schedule and waiting start the other tests left behind.

    Parameters
    ----------
    stack
        The stack.
    """
    with Session(stack.db) as session:
        session.execute(update(JobSchedule).values(next_fire_at=None, pending_start=None))
        session.commit()


@dataclass(frozen=True)
class Project:
    """A deployed repository with the jobs `fast` (scheduled), `slow` and `gone`.

    Parameters
    ----------
    environment
        Environment name.
    repository
        Repository name.
    path
        Its checkout.
    """

    environment: str
    repository: str
    path: Path


def _project(stack: QueryStack, *, bind: bool = True, jobs: str = _JOBS) -> Project:
    """Deploy a repository with jobs and bind a service account to it.

    Parameters
    ----------
    stack
        The stack.
    jobs
        The `jobs:` part of `lakana.yaml`.
    bind
        Bind a run principal.

    Returns
    -------
    Project
        The deployed project.
    """
    environment = stack.environment()
    repository = unique_name()
    path = stack.services.roots / repository
    commit_files(path, {"lakana.yaml": jobs, **_FILES})
    stack.admin.repositories.create(repository, "local", str(path))
    deploy = stack.admin.deploys.create(environment, repository)
    _drain(stack)
    stack.admin.deploys.wait(environment, deploy.id, timeout=30)
    project = Project(environment, repository, path)
    if bind:
        _bind(stack, project)
    return project


def _bind(stack: QueryStack, project: Project) -> None:
    """Bind a new service account as the run principal of the project.

    Parameters
    ----------
    stack
        The stack.
    project
        The project.
    """
    with Session(stack.db) as session:
        account = Principal(
            id=uuid.uuid4(), type=PrincipalType.SERVICE_ACCOUNT, name=unique_name(), is_active=True
        )
        session.add(account)
        session.flush()
        repo_id = session.scalars(
            select(Repository.id).where(Repository.name == project.repository)
        ).one()
        session.execute(
            update(EnvironmentDeployment)
            .where(EnvironmentDeployment.repository_id == repo_id)
            .values(run_as_principal_id=account.id)
        )
        session.commit()


def _arm(stack: QueryStack, project: Project, job: str = "fast", at: datetime = FIRE) -> None:
    """Make a job's schedule due at a time.

    Parameters
    ----------
    stack
        The stack.
    project
        The project.
    job
        The job.
    at
        Its `next_fire_at`.
    """
    with Session(stack.db) as session:
        session.execute(
            update(JobSchedule)
            .where(JobSchedule.id == _schedule_id(session, project, job))
            .values(next_fire_at=at)
        )
        session.commit()


def _schedule_id(session: Session, project: Project, job: str) -> uuid.UUID:
    """Look up a schedule row's id.

    Parameters
    ----------
    session
        Database session.
    project
        The project.
    job
        The job.

    Returns
    -------
    uuid.UUID
        The id.
    """
    return session.scalars(
        select(JobSchedule.id)
        .join(Repository, Repository.id == JobSchedule.repository_id)
        .where(Repository.name == project.repository, JobSchedule.job == job)
    ).one()


def _schedule(stack: QueryStack, project: Project, job: str) -> JobSchedule | None:
    """Read a job's schedule row.

    Parameters
    ----------
    stack
        The stack.
    project
        The project.
    job
        The job.

    Returns
    -------
    JobSchedule or None
        The row, detached; `None` when a deploy removed it.
    """
    with Session(stack.db) as session:
        row = session.scalars(
            select(JobSchedule)
            .join(Repository, Repository.id == JobSchedule.repository_id)
            .where(Repository.name == project.repository, JobSchedule.job == job)
        ).one_or_none()
        session.expunge_all()
        return row


def _runs(stack: QueryStack, project: Project) -> list[Run]:
    """List the runs of the project, oldest first.

    Parameters
    ----------
    stack
        The stack.
    project
        The project.

    Returns
    -------
    list of Run
        The runs, detached.
    """
    with Session(stack.db) as session:
        rows = session.scalars(
            select(Run)
            .join(Repository, Repository.id == Run.repository_id)
            .where(Repository.name == project.repository)
            .order_by(Run.created_at)
        ).all()
        session.expunge_all()
        return list(rows)


def _fires(stack: QueryStack, project: Project) -> list[JobFire]:
    """List the booked fires of the project's jobs.

    Parameters
    ----------
    stack
        The stack.
    project
        The project.

    Returns
    -------
    list of JobFire
        The fires, detached.
    """
    with Session(stack.db) as session:
        rows = session.scalars(
            select(JobFire)
            .join(JobSchedule, JobSchedule.id == JobFire.job_schedule_id)
            .join(Repository, Repository.id == JobSchedule.repository_id)
            .where(Repository.name == project.repository)
        ).all()
        session.expunge_all()
        return list(rows)


def _merged(stack: QueryStack, project: Project, run: Run) -> int:
    """Read how many starts a run stands for, from its `RUN_STARTED` event.

    Parameters
    ----------
    stack
        The stack.
    project
        The project.
    run
        The run.

    Returns
    -------
    int
        The `merged` count of the event.
    """
    (event,) = [
        e
        for e in stack.events(project.environment, AuditEventType.RUN_STARTED)
        if e.detail["run_id"] == str(run.id)
    ]
    return int(event.detail["merged"])


def _end_runs(stack: QueryStack, project: Project) -> None:
    """End the project's active runs, so that it is free.

    Parameters
    ----------
    stack
        The stack.
    project
        The project.
    """
    with Session(stack.db) as session:
        ids = [r.id for r in _runs(stack, project)]
        session.execute(update(Run).where(Run.id.in_(ids)).values(status=RunStatus.SUCCEEDED))
        session.commit()


def _by_hand(stack: QueryStack, project: Project, job: str) -> object:
    """Start a job as the administrator, in a session of its own.

    Parameters
    ----------
    stack
        The stack.
    project
        The project.
    job
        The job.

    Returns
    -------
    StartResult
        What the start did.
    """
    with Session(stack.db) as session:
        return JobService(session, GrantAuthorization(session)).start(
            _admin(stack), project.environment, project.repository, job
        )


def _fire(stack: QueryStack, now: datetime = NOW, lock_timeout: float = 20.0) -> list[object]:
    """Run the fire step in a session of its own.

    Parameters
    ----------
    stack
        The stack.
    now
        The scheduler's time.
    lock_timeout
        Seconds a lock wait lasts; long, so that a test can hold the lock as long as it needs.

    Returns
    -------
    list
        The fires.
    """
    with Session(stack.db) as session:
        return list(fire_due(session, now, lock_timeout=lock_timeout))


def _start_waiting(stack: QueryStack) -> list[Run]:
    """Run the waiting-start step in a session of its own.

    Parameters
    ----------
    stack
        The stack.

    Returns
    -------
    list of Run
        The runs it started.
    """
    with Session(stack.db) as session:
        started = start_waiting_jobs(session, lock_timeout=20.0)
        for run in started:
            session.refresh(run)
        session.expunge_all()
        return started


def _waiter_queries(stack: QueryStack, blocker: int) -> list[str]:
    """List the statements of the backends that wait on a lock the blocker holds.

    Parameters
    ----------
    stack
        The stack.
    blocker
        The blocking backend's pid.

    Returns
    -------
    list of str
        The statements they are stuck in.
    """
    with stack.db.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT query FROM pg_stat_activity "
                "WHERE wait_event_type = 'Lock' AND :blocker = ANY(pg_blocking_pids(pid))"
            ),
            {"blocker": blocker},
        )
        return [row[0] for row in rows]


def _wait_for_pointer_waiters(stack: QueryStack, count: int, timeout: float = 20.0) -> None:
    """Wait until at least `count` backends wait for a lock in a statement on the pointers.

    The second and later waiters for one row queue behind the first one, so they are not
    blocked by the holder's pid; they are found by what they run.

    Parameters
    ----------
    stack
        The stack.
    count
        How many waiters.
    timeout
        Seconds to wait.

    Raises
    ------
    AssertionError
        If fewer wait in time.
    """
    sql = text(
        "SELECT count(*) FROM pg_stat_activity "
        "WHERE wait_event_type = 'Lock' AND query LIKE '%environment_deployments%'"
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with stack.db.connect() as connection:
            if connection.execute(sql).scalar_one() >= count:
                return
        time.sleep(0.05)
    raise AssertionError(f"fewer than {count} backends wait for a pointer")


def _drain(stack: QueryStack) -> None:
    """Run queued deploys in the calling thread.

    Parameters
    ----------
    stack
        The stack.
    """
    settings = DeploySettings(
        repo_roots=(str(stack.services.roots),),
        python=PythonSupport(server_url=stack.url, uv=shutil.which("uv") or "/usr/bin/uv"),
    )
    while True:
        with Session(stack.db) as session:
            if not poll_once(session, GrantAuthorization(session), deploy=settings):
                return


def _admin(stack: QueryStack) -> PrincipalRef:
    """Return the bootstrap administrator.

    Parameters
    ----------
    stack
        The stack.

    Returns
    -------
    PrincipalRef
        The administrator.
    """
    return PrincipalRef(id=stack.principal_id("admin"), name="admin", type=PrincipalType.USER)


def test_a_start_that_waits_behind_a_deploy_that_removes_its_job_leaves_nothing_behind(
    stack: QueryStack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The start meets an active run, is held before it parks, and the deploy removes its job."""
    environment = stack.environment()
    repository = unique_name()
    path = stack.services.roots / repository
    commit_files(path, {"lakana.yaml": _YAML, **_FILES})
    stack.admin.repositories.create(repository, "local", str(path))
    first = stack.admin.deploys.create(environment, repository)
    _drain(stack)
    stack.admin.deploys.wait(environment, first.id, timeout=30)
    admin = _admin(stack)
    with Session(stack.db) as session:
        started = JobService(session, GrantAuthorization(session)).start(
            admin, environment, repository, "keep"
        )
        assert started.run is not None

    commit_files(path, {"lakana.yaml": _YAML_WITHOUT, **_FILES})
    second = stack.admin.deploys.create(environment, repository)

    entered = HoldPoint()
    real = JobService._park

    def held(self: JobService, *args: object, **kwargs: object) -> object:
        if threading.current_thread().name == "starter" and not entered.reached.is_set():
            entered.wait_here()
        return real(self, *args, **kwargs)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(JobService, "_park", held)

    def start() -> object:
        with Session(stack.db) as session:
            return JobService(session, GrantAuthorization(session)).start(
                admin, environment, repository, "gone"
            )

    starter, outcome = run_in_thread("starter", start)
    assert entered.reached.wait(30), "the start never reached the park"
    with StatementPause(stack.db, "DELETE FROM job_schedules", "deployer") as pause:
        deployer, deployed = run_in_thread("deployer", lambda: _drain(stack))
        pause.wait()
        entered.release.set()
        wait_for_lock_waiter(stack.db, pause.backend or 0)
    assert thread_result(deployer, deployed) is None
    failure = thread_result(starter, outcome)
    assert isinstance(failure, NotFoundError), failure
    assert "gone" in str(failure)
    stack.admin.deploys.wait(environment, second.id, timeout=30)

    with Session(stack.db) as session:
        assert session.scalars(select(JobSchedule).where(JobSchedule.job == "gone")).all() == []
        waiting = session.scalars(
            select(AuditEvent).where(AuditEvent.event_type == AuditEventType.JOB_START_WAITING)
        ).all()
        assert [e for e in waiting if e.detail.get("job") == "gone"] == []


def test_two_leaders_firing_at_the_same_moment_start_one_run(stack: QueryStack) -> None:
    """A smoke test of the overlap; the held-booking test below pins the order of the locks."""
    project = _project(stack)
    _arm(stack, project)
    barrier = threading.Barrier(2)

    def leader() -> object:
        barrier.wait(30)
        return _fire(stack)

    threads = [run_in_thread(f"leader-{n}", leader) for n in range(2)]
    results = [thread_result(thread, out) for thread, out in threads]
    assert sorted(len(r) for r in results) == [0, 1]  # ty: ignore[invalid-argument-type]
    (run,) = _runs(stack, project)
    assert (run.trigger, run.scheduled_for, run.job) == (RunTrigger.SCHEDULE, FIRE, "fast")
    assert len(_fires(stack, project)) == 1


def test_a_leader_held_while_it_books_makes_the_other_skip_the_time(stack: QueryStack) -> None:
    """One leader is held at the insert of the fire, the other waits for the lock and skips."""
    project = _project(stack)
    _arm(stack, project)
    with StatementPause(stack.db, "INSERT INTO job_fires", "first") as pause:
        first, first_out = run_in_thread("first", lambda: _fire(stack))
        pause.wait()
        second, second_out = run_in_thread("second", lambda: _fire(stack))
        wait_for_lock_waiter(stack.db, pause.backend or 0)
        # It waits for the deployment's pointer, the first lock of a deploy's order.
        assert any(
            "environment_deployments" in q for q in _waiter_queries(stack, pause.backend or 0)
        )
    assert len(thread_result(first, first_out)) == 1  # ty: ignore[invalid-argument-type]
    assert thread_result(second, second_out) == []
    assert len(_runs(stack, project)) == 1 and len(_fires(stack, project)) == 1
    row = _schedule(stack, project, "fast")
    assert row is not None and row.last_fired_for == FIRE


def test_a_fire_during_an_active_run_starts_when_that_run_ends(stack: QueryStack) -> None:
    """The fire waits behind the run, is consumed, and runs once the project is free."""
    project = _project(stack)
    assert _by_hand(stack, project, "slow").run is not None  # ty: ignore[unresolved-attribute]
    _arm(stack, project)
    (fire,) = _fire(stack)
    assert fire.outcome is FireOutcome.WAITING  # ty: ignore[unresolved-attribute]
    assert _start_waiting(stack) == []
    assert len(_runs(stack, project)) == 1
    _end_runs(stack, project)
    (started,) = _start_waiting(stack)
    assert (started.job, started.trigger, started.scheduled_for) == (
        "fast",
        RunTrigger.SCHEDULE,
        FIRE,
    )
    row = _schedule(stack, project, "fast")
    assert row is not None and row.pending_start is None


def test_a_start_by_hand_while_busy_then_the_fire_give_one_waiting_start_and_one_run(
    stack: QueryStack,
) -> None:
    """The two requests merge into the one waiting start; one run follows."""
    project = _project(stack)
    assert _by_hand(stack, project, "slow").run is not None  # ty: ignore[unresolved-attribute]
    waiting = _by_hand(stack, project, "fast").waiting  # ty: ignore[unresolved-attribute]
    assert waiting is not None and waiting.trigger == "manual"
    _arm(stack, project)
    (fire,) = _fire(stack)
    assert fire.outcome is FireOutcome.WAITING  # ty: ignore[unresolved-attribute]
    row = _schedule(stack, project, "fast")
    assert row is not None and row.pending_start is not None
    assert (row.pending_start["trigger"], row.pending_start["merged"]) == ("schedule", 2)
    _end_runs(stack, project)
    (started,) = _start_waiting(stack)
    assert started.trigger is RunTrigger.SCHEDULE and _merged(stack, project, started) == 2
    assert _start_waiting(stack) == []
    assert len(_runs(stack, project)) == 2


def test_a_deploy_that_removes_a_job_with_a_waiting_start_leaves_no_start_behind(
    stack: QueryStack,
) -> None:
    """The row goes with the job, and the project's later free moment starts nothing."""
    project = _project(stack)
    assert _by_hand(stack, project, "slow").run is not None  # ty: ignore[unresolved-attribute]
    assert _by_hand(stack, project, "gone").waiting is not None  # ty: ignore[unresolved-attribute]
    commit_files(project.path, {"lakana.yaml": _JOBS_WITHOUT_GONE, **_FILES})
    deploy = stack.admin.deploys.create(project.environment, project.repository)
    _drain(stack)
    stack.admin.deploys.wait(project.environment, deploy.id, timeout=30)
    assert _schedule(stack, project, "gone") is None
    _end_runs(stack, project)
    assert _start_waiting(stack) == []
    assert len(_runs(stack, project)) == 1


def test_start_waiting_racing_a_start_by_hand_gives_one_run_and_loses_no_start(
    stack: QueryStack,
) -> None:
    """The waiting start is held at its insert; the start by hand then parks instead of failing."""
    project = _project(stack)
    assert _by_hand(stack, project, "slow").run is not None  # ty: ignore[unresolved-attribute]
    assert _by_hand(stack, project, "fast").waiting is not None  # ty: ignore[unresolved-attribute]
    _end_runs(stack, project)
    with StatementPause(stack.db, "INSERT INTO runs", "waiter") as pause:
        waiter, waited = run_in_thread("waiter", lambda: _start_waiting(stack))
        pause.wait()
        hand, handed = run_in_thread("hand", lambda: _by_hand(stack, project, "gone"))
        wait_for_lock_waiter(stack.db, pause.backend or 0)
        # The start by hand is held at the pointer, which `start_waiting` took first; without
        # that lock it would pass it and meet the unique index of the runs instead.
        assert any(
            "environment_deployments" in q for q in _waiter_queries(stack, pause.backend or 0)
        )
    (started,) = thread_result(waiter, waited)  # ty: ignore[not-iterable]
    result = thread_result(hand, handed)
    assert started.job == "fast"
    assert result.run is None and result.waiting is not None  # ty: ignore[unresolved-attribute]
    assert result.waiting.waits_for_run_id == started.id  # ty: ignore[unresolved-attribute]
    row = _schedule(stack, project, "gone")
    assert row is not None and row.pending_start is not None
    assert len([r for r in _runs(stack, project) if r.job == "fast"]) == 1


def test_start_waiting_racing_a_park_merges_into_the_started_run(stack: QueryStack) -> None:
    """A park that holds the locks first adds to the waiting start; the run it starts says so."""
    project = _project(stack)
    assert _by_hand(stack, project, "slow").run is not None  # ty: ignore[unresolved-attribute]
    assert _by_hand(stack, project, "fast").waiting is not None  # ty: ignore[unresolved-attribute]
    with StatementPause(stack.db, "UPDATE job_schedules", "parker") as pause:
        parker, parked = run_in_thread("parker", lambda: _by_hand(stack, project, "fast"))
        pause.wait()
        _end_runs(stack, project)
        waiter, waited = run_in_thread("waiter", lambda: _start_waiting(stack))
        wait_for_lock_waiter(stack.db, pause.backend or 0)
    assert thread_result(parker, parked).waiting is not None  # ty: ignore[unresolved-attribute]
    (started,) = thread_result(waiter, waited)  # ty: ignore[not-iterable]
    assert started.job == "fast" and _merged(stack, project, started) == 2
    assert len(_runs(stack, project)) == 2


def test_a_deploy_a_fire_a_waiting_start_and_a_pause_do_not_deadlock(stack: QueryStack) -> None:
    """The deploy holds the pointer; the three others queue behind it and then all finish.

    A step that took the schedule row before the pointer would hold the row the deploy needs
    next, and Postgres would end one of them with a deadlock.
    """
    project = _project(stack)
    admin = _admin(stack)
    assert _by_hand(stack, project, "gone").run is not None  # ty: ignore[unresolved-attribute]
    assert _by_hand(stack, project, "slow").waiting is not None  # ty: ignore[unresolved-attribute]
    _end_runs(stack, project)
    _arm(stack, project)
    commit_files(project.path, {"lakana.yaml": _JOBS.replace("0 3 * * *", "5 3 * * *"), **_FILES})
    deploy = stack.admin.deploys.create(project.environment, project.repository)

    def pausing() -> object:
        with Session(stack.db) as session:
            service = JobService(session, GrantAuthorization(session))
            service.pause(admin, project.environment, project.repository, "fast")
            service.resume(admin, project.environment, project.repository, "fast")

    with StatementPause(stack.db, "job_schedules", "deployer") as pause:
        deployer, deployed = run_in_thread("deployer", lambda: _drain(stack))
        pause.wait()
        threads = [
            run_in_thread("firing", lambda: _fire(stack)),
            run_in_thread("waiting", lambda: _start_waiting(stack)),
            run_in_thread("pausing", pausing),
        ]
        _wait_for_pointer_waiters(stack, 3)
    assert thread_result(deployer, deployed) is None
    results = [thread_result(thread, out) for thread, out in threads]
    assert not [r for r in results if isinstance(r, BaseException)], results
    stack.admin.deploys.wait(project.environment, deploy.id, timeout=30)
    # The waiting start met a pointer that had moved and kept waiting; the next tick starts it.
    (started,) = _start_waiting(stack)
    assert started.job == "slow"


def test_a_deploy_that_removes_a_job_holds_the_fire_and_it_does_not_fire(
    stack: QueryStack,
) -> None:
    """The fire waits for the pointer, reads the manifest after it, and finds the job gone."""
    project = _project(stack)
    _arm(stack, project)
    commit_files(
        project.path,
        {
            "lakana.yaml": _JOBS.split("  fast:")[0] + "  slow:\n    select: ['silver.*']\n",
            **_FILES,
        },
    )
    deploy = stack.admin.deploys.create(project.environment, project.repository)
    with StatementPause(stack.db, "DELETE FROM job_schedules", "deployer") as pause:
        deployer, deployed = run_in_thread("deployer", lambda: _drain(stack))
        pause.wait()
        firing, fired = run_in_thread("firing", lambda: _fire(stack))
        wait_for_lock_waiter(stack.db, pause.backend or 0)
    assert thread_result(deployer, deployed) is None
    assert thread_result(firing, fired) == []
    stack.admin.deploys.wait(project.environment, deploy.id, timeout=30)
    assert _runs(stack, project) == [] and _fires(stack, project) == []
    assert _schedule(stack, project, "fast") is None


def test_a_deploy_that_holds_the_pointer_makes_the_tick_give_up_and_the_next_one_fire(
    stack: QueryStack,
) -> None:
    """The lock wait is bounded: the tick returns, the time stays due, a later tick fires it."""
    project = _project(stack)
    _arm(stack, project)
    commit_files(project.path, {"lakana.yaml": _JOBS_WITHOUT_GONE, **_FILES})
    deploy = stack.admin.deploys.create(project.environment, project.repository)
    with StatementPause(stack.db, "DELETE FROM job_schedules", "deployer") as pause:
        deployer, deployed = run_in_thread("deployer", lambda: _drain(stack))
        pause.wait()
        started = time.monotonic()
        with Session(stack.db) as session:
            assert fire_due(session, NOW, lock_timeout=1.0) == []
            assert start_waiting_jobs(session, lock_timeout=1.0) == []
        assert time.monotonic() - started < 8
        row = _schedule(stack, project, "fast")
        assert row is not None and row.next_fire_at == FIRE
        assert _fires(stack, project) == [] and _runs(stack, project) == []
    assert thread_result(deployer, deployed) is None
    stack.admin.deploys.wait(project.environment, deploy.id, timeout=30)
    (fire,) = _fire(stack)
    assert fire.outcome is FireOutcome.STARTED  # ty: ignore[unresolved-attribute]


def test_binding_a_principal_after_fire_times_were_consumed_gives_no_burst(
    stack: QueryStack,
) -> None:
    """Nothing started while unbound; after the binding only the next time fires."""
    project = _project(stack, bind=False)
    _arm(stack, project)
    (fire,) = _fire(stack)
    assert fire.outcome is FireOutcome.NO_RUN_PRINCIPAL  # ty: ignore[unresolved-attribute]
    assert _runs(stack, project) == []
    _bind(stack, project)
    assert _fire(stack) == []
    assert _runs(stack, project) == []
    (next_fire,) = _fire(stack, NOW + timedelta(days=1))
    assert next_fire.scheduled_for == FIRE + timedelta(days=1)  # ty: ignore[unresolved-attribute]
    assert next_fire.missed == 0  # ty: ignore[unresolved-attribute]
    (run,) = _runs(stack, project)
    assert run.scheduled_for == FIRE + timedelta(days=1)


def test_a_due_schedule_is_fired_by_the_leader_and_a_worker_builds_its_table(
    stack: QueryStack,
) -> None:
    """The real leader loop fires a due schedule on the database's clock and a worker builds.

    Nothing here is a fixed clock: `SchedulerLoop` reads the database's time each step, as in
    production. The schedule is made due by setting its `next_fire_at` to the most recent 03:00
    UTC (the job's cron time), so it is due now whatever the hour, with no earlier time missed.
    The test drives `SchedulerLoop.step()`, not the thread wrapper (`run`, `start`): the interval,
    the jitter and the thread are not covered here.
    """
    project = _project(stack, bind=False)
    account = unique_name()
    stack.admin.principals.create(account, "SERVICE_ACCOUNT")
    stack.admin.grants.grant(
        principal=account,
        role="DATA_ENGINEER",
        kind="ENVIRONMENT",
        environment=project.environment,
    )
    stack.admin.deployments.set_run_as(project.environment, project.repository, account)
    with Session(stack.db) as session:
        now = db_now(session)
    due = now.replace(hour=3, minute=0, second=0, microsecond=0)
    if due > now:
        due -= timedelta(days=1)
    _arm(stack, project, "fast", at=due)

    loop = SchedulerLoop(stack.db, f"leader-{uuid.uuid4().hex[:8]}")
    run_engine = RunEngine(
        session_factory=lambda: Session(stack.db),
        authz_factory=GrantAuthorization,
        settings=TaskSettings(worker_id="scheduled-worker", renew_seconds=1, lease_seconds=30),
    )
    deploy = DeploySettings(repo_roots=(str(stack.services.roots),))

    def work() -> None:
        """Poll as a worker until nothing is queued: queries, deploys and ready tasks."""
        while True:
            with Session(stack.db) as session:
                if not poll_once(
                    session, GrantAuthorization(session), deploy=deploy, engine=run_engine
                ):
                    return

    try:
        deadline = time.monotonic() + 60
        runs = _runs(stack, project)
        while time.monotonic() < deadline:
            # A lease an earlier test left behind lasts up to STALE_AFTER: step until this
            # worker leads.
            if loop.step():
                work()
            runs = _runs(stack, project)
            if runs and runs[0].status in {RunStatus.SUCCEEDED, RunStatus.FAILED}:
                break
            time.sleep(0.2)
    finally:
        # The advisory lock is held by a connection of the election until it resigns; `stop()`
        # only ends `run()`, which is not used here.
        loop.stop()
        loop._election.resign()

    (run,) = runs
    assert (run.trigger, run.job, run.scheduled_for, run.missed) == (
        RunTrigger.SCHEDULE,
        "fast",
        due,
        0,
    )
    assert run.status is RunStatus.SUCCEEDED, run.error_message
    assert len(_fires(stack, project)) == 1
    row = _schedule(stack, project, "fast")
    assert row is not None and row.last_fired_for == due
    assert row.next_fire_at is not None and row.next_fire_at > now

    reader, result = run_in_thread(
        "reader",
        lambda: stack.admin.sql("SELECT id FROM silver.x ORDER BY id", project.environment),
    )
    until = time.monotonic() + 60
    while reader.is_alive() and time.monotonic() < until:
        work()
        reader.join(0.1)
    table = thread_result(reader, result)
    assert table.to_pylist() == [{"id": 1}, {"id": 2}, {"id": 3}]  # ty: ignore[unresolved-attribute]


_ON_DEPLOY = (
    "defaults:\n  retry_delay: 0\n  retries: 1\n"
    "jobs:\n  refresh:\n    select: ['silver.*']\n    on_deploy: true\n"
)


def test_a_deploy_starts_its_job_and_the_next_deploy_waits_for_that_run(
    stack: QueryStack,
) -> None:
    """On Postgres, through the worker: the deploy's start runs, the next one waits, then runs.

    The same commit deployed again starts nothing.
    """
    environment = stack.environment()
    repository = unique_name()
    path = stack.services.roots / repository
    commit_files(path, {"lakana.yaml": _ON_DEPLOY, **_FILES})
    stack.admin.repositories.create(repository, "local", str(path))
    project = Project(environment, repository, path)

    first = stack.admin.deploys.create(environment, repository)
    _drain(stack)
    (start,) = stack.admin.deploys.wait(environment, first.id, timeout=30).job_starts
    (run,) = _runs(stack, project)
    assert (start.outcome, start.run_id) == ("started", str(run.id))
    assert (run.trigger, run.triggered_by) == (RunTrigger.DEPLOY, _admin(stack).id)

    commit_files(path, {"assets/silver/y.sql": "SELECT id FROM bronze.nums\n"})
    second = stack.admin.deploys.create(environment, repository)
    _drain(stack)
    (waiting,) = stack.admin.deploys.wait(environment, second.id, timeout=30).job_starts
    assert (waiting.outcome, waiting.waits_for_run_id) == ("waiting", str(run.id))
    assert len(_runs(stack, project)) == 1

    _end_runs(stack, project)
    (started,) = _start_waiting(stack)
    assert (started.trigger, started.triggered_by) == (RunTrigger.DEPLOY, _admin(stack).id)

    _end_runs(stack, project)
    same = stack.admin.deploys.create(environment, repository)
    _drain(stack)
    assert stack.admin.deploys.wait(environment, same.id, timeout=30).job_starts == ()
    assert len(_runs(stack, project)) == 2
