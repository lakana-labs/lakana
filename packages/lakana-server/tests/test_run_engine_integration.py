"""The exit test: the run engine on Postgres and SeaweedFS.

Drives the platform through `lakana.Client` over the app's `TestClient`; deploys,
scheduler ticks and task runs are executed in-process — `poll_once` with a
`RunEngine`, `scheduler_tick` — exactly as worker processes would, against a
real Postgres (advisory locks, `FOR UPDATE SKIP LOCKED`, READ COMMITTED races)
and SeaweedFS.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pyarrow as pa
import pytest
from fastapi.testclient import TestClient
from pyiceberg.catalog.rest import RestCatalog
from pyiceberg.exceptions import NoSuchTableError
from sqlalchemy import Connection, Engine, bindparam, func, select, text, update
from sqlalchemy.orm import Session
from testcontainers.postgres import PostgresContainer

import lakana
from lakana._models import Run as SdkRun
from lakana._models import RunTask
from lakana.errors import Conflict, NotFound, QueryFailed
from lakana.errors import PermissionDenied as SdkPermissionDenied
from lakana_core.auth import AllowAllAuthorization, PrincipalRef, RunContext
from lakana_core.catalog import service as catalog_service
from lakana_core.catalog.metadata import read_metadata
from lakana_core.catalog.service import CatalogService, Rollback, failed_snapshot_tag
from lakana_core.config import DatabaseConfig
from lakana_core.db import create_engine, db_now
from lakana_core.errors import ConflictError, PermissionDenied
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.identifiers import TableRef
from lakana_core.models import (
    AttemptOutcome,
    AuditEvent,
    AuditEventType,
    Base,
    Environment,
    Principal,
    PrincipalType,
    QueryRun,
    QueryRunStatus,
    Repository,
    RollbackOutcome,
    Run,
    RunStatus,
    TaskAttempt,
    TaskRun,
    TaskStatus,
    WriteMode,
)
from lakana_core.projects.deploys import DeployService
from lakana_core.runs.checks import (
    ERROR_UNREADABLE,
    ROLLBACK_SENTENCES,
    checked_snapshot_gone_message,
    not_published_message,
    policy_note,
    results_of_task,
    verdict,
)
from lakana_core.runs.checks import failure_message as check_failure_message
from lakana_core.runs.leader import PostgresAdvisoryLock
from lakana_core.runs.lockdown import (
    PROTECTED_MESSAGE,
    SUPERSEDED_MESSAGE,
    ProtectedWriteGuard,
)
from lakana_core.runs.scheduler import TaskTransition
from lakana_core.runs.scheduler import tick as sched_tick
from lakana_core.runs.selection import Selection
from lakana_core.runs.service import RunService
from lakana_core.runs.store import (
    apply_transitions,
    load_snapshots,
    scheduler_tick,
)
from lakana_core.runs.tasks import ClaimedTask, LeaseState, claim_task, record_attempt, renew_lease
from lakana_core.storage.resolver import StorageResolver
from lakana_server.app import create_app
from lakana_server.settings import Settings
from lakana_worker import checks as worker_checks
from lakana_worker import pipeline
from lakana_worker.deploy import DeploySettings
from lakana_worker.pipeline import run_one
from lakana_worker.poller import poll_once
from lakana_worker.query_process import REGISTRY, QueryChild
from lakana_worker.scheduler import SchedulerLoop
from lakana_worker.tasks import RunEngine, TaskSettings
from tests.integration_helpers import HoldPoint
from tests.integration_helpers import backend_pid as _backend_pid
from tests.integration_helpers import commit_files as _commit
from tests.integration_helpers import unique_name as _name
from tests.integration_helpers import wait_for_lock_waiter as _wait_for_lock_waiter
from tests.integration_helpers import write_check as _write_check
from tests.python_asset_helpers import long_sql, serving
from tests.server_test_helpers import SeaweedFsContainer

pytestmark = pytest.mark.integration

_TOKEN = "lakana_pat_m5c1integration_m5c1integrationsecret"
_HEAVY = "SELECT sum(hash(x)) AS n FROM (SELECT unnest(range(6000000)) AS x)"
"""Takes a few hundred milliseconds, so two workers overlap."""


@dataclass(frozen=True)
class _Stack:
    """Running services and the settings pointing at them.

    Parameters
    ----------
    settings
        Server settings.
    storage
        The S3 storage block for `environments.create`.
    roots
        The repo root directory.
    """

    settings: Settings
    storage: dict[str, object]
    roots: Path


@pytest.fixture(scope="module")
def stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[_Stack]:
    """Start Postgres and SeaweedFS once for this module.

    Parameters
    ----------
    tmp_path_factory
        For the repo root.

    Yields
    ------
    _Stack
        The services.
    """
    roots = tmp_path_factory.mktemp("repos")
    with (
        PostgresContainer("postgres:16-alpine", driver="pg8000") as pg,
        SeaweedFsContainer() as seaweedfs,
    ):
        seaweedfs.get_client().make_bucket("lakana")
        settings = Settings(
            database=DatabaseConfig(url=pg.get_connection_url()),
            bootstrap_pat_key_id="m5c1integration",
            bootstrap_pat_secret="m5c1integrationsecret",
            repo_roots=[str(roots)],
        )
        engine = create_engine(settings.database)
        Base.metadata.create_all(engine)
        engine.dispose()
        config = seaweedfs.get_config()
        yield _Stack(
            settings=settings,
            storage={
                "scheme": "s3",
                "root": "s3://lakana/{env}",
                "options": {
                    "s3.endpoint": f"http://{config['endpoint']}",
                    "s3.access-key-id": seaweedfs.access_key,
                    "s3.secret-access-key": seaweedfs.secret_key,
                },
            },
            roots=roots,
        )


def _retire_leftovers(db: Engine) -> None:
    """Cancel every active run, so one test's tasks are never claimed by the next.

    Parameters
    ----------
    db
        The metadata database engine.
    """
    with Session(db) as session:
        session.execute(
            update(Run)
            .where(Run.status.in_([RunStatus.QUEUED, RunStatus.RUNNING]))
            .values(status=RunStatus.CANCELLED)
        )
        session.execute(
            update(TaskRun)
            .where(
                TaskRun.status.notin_(
                    [
                        TaskStatus.SUCCEEDED,
                        TaskStatus.FAILED,
                        TaskStatus.CANCELLED,
                        TaskStatus.UPSTREAM_FAILED,
                    ]
                )
            )
            .values(status=TaskStatus.CANCELLED)
        )
        session.commit()


def _serve_queries(client: TestClient, stop: threading.Event, errors: list[BaseException]) -> None:
    """Run queued queries and deploys, as a task-less worker process would.

    Parameters
    ----------
    client
        The test client.
    stop
        Set to end the loop.
    errors
        Receives whatever escapes the loop, so the fixture can fail the test
        instead of the daemon thread dying silently.
    """
    settings = DeploySettings(repo_roots=tuple(client.app.state.settings.repo_roots))
    try:
        while not stop.is_set():
            with Session(client.app.state.engine) as session:
                busy = poll_once(session, GrantAuthorization(session), deploy=settings)
            if not busy:
                stop.wait(0.05)
    except BaseException as error:
        errors.append(error)


@pytest.fixture
def sdk(stack: _Stack) -> Iterator[tuple[TestClient, lakana.Client]]:
    """Start an app on the shared stack and an admin SDK client.

    Parameters
    ----------
    stack
        The services.

    Yields
    ------
    tuple
        The test client and the SDK client.

    Raises
    ------
    BaseException
        Whatever ended the background worker thread, re-raised at teardown.
    """
    stop = threading.Event()
    errors: list[BaseException] = []
    with (
        TestClient(create_app(stack.settings)) as client,
        lakana.Client(url="http://testserver", token=_TOKEN, http_client=client) as admin,
    ):
        _retire_leftovers(client.app.state.engine)
        worker = threading.Thread(target=_serve_queries, args=(client, stop, errors), daemon=True)
        worker.start()
        try:
            yield client, admin
        finally:
            stop.set()
            worker.join(timeout=30)
        assert not worker.is_alive()
        if errors:
            raise errors[0]


def _engine(client: TestClient, worker: str = "w1") -> RunEngine:
    """Build a worker's run engine over the app's database.

    Parameters
    ----------
    client
        The test client.
    worker
        Worker id.

    Returns
    -------
    RunEngine
        The engine.
    """
    db: Engine = client.app.state.engine
    return RunEngine(
        session_factory=lambda: Session(db),
        authz_factory=GrantAuthorization,
        settings=TaskSettings(worker_id=worker, renew_seconds=1, lease_seconds=5),
    )


def _kill_lock_connection(loop: SchedulerLoop) -> None:
    """Simulate the holder's process dying: its session-level advisory lock connection closes.

    Parameters
    ----------
    loop
        A scheduler loop that holds the Postgres advisory lock.
    """
    lock = loop._election._lock
    assert isinstance(lock, PostgresAdvisoryLock)
    assert lock._connection is not None
    lock._connection.invalidate()
    lock._connection = None


def _deploy_and_drain(client: TestClient, run_engine: RunEngine | None = None) -> None:
    """Drain queries, deploys and ready tasks once, as one worker would.

    Parameters
    ----------
    client
        The test client.
    run_engine
        Executes tasks; `None` drains only queries and deploys.
    """
    settings = DeploySettings(repo_roots=tuple(client.app.state.settings.repo_roots))
    while True:
        with Session(client.app.state.engine) as session:
            if not poll_once(
                session, GrantAuthorization(session), deploy=settings, engine=run_engine
            ):
                return


def _tick(client: TestClient) -> None:
    """Run one scheduler tick.

    Parameters
    ----------
    client
        The test client.
    """
    with Session(client.app.state.engine) as session:
        scheduler_tick(session, db_now(session), lambda: 0.5)


def _until_finished(client: TestClient, admin: lakana.Client, env: str, run_id: str) -> SdkRun:
    """Tick and work until the run finishes (bounded).

    Parameters
    ----------
    client
        The test client.
    admin
        The SDK client.
    env
        Environment.
    run_id
        The run.

    Returns
    -------
    SdkRun
        The finished run.

    Raises
    ------
    AssertionError
        If the run is still active after 50 rounds.
    """
    run_engine = _engine(client)
    for _ in range(50):
        _tick(client)
        _deploy_and_drain(client, run_engine)
        current = admin.runs.get(env, run_id)
        if current.status in {"succeeded", "failed", "cancelled"}:
            return current
    raise AssertionError(f"run {run_id} did not finish")


def _project(
    stack: _Stack, admin: lakana.Client, client: TestClient, env: str, files: dict[str, str]
) -> str:
    """Create `env` with `bronze.events` (one row), register and deploy a project.

    Parameters
    ----------
    stack
        The services.
    admin
        The SDK client.
    client
        The test client.
    env
        Environment name.
    files
        Project files besides `lakana.yaml`.

    Returns
    -------
    str
        The repository name.
    """
    repo = _name()
    admin.environments.create(env, storage={**stack.storage, "root": f"s3://lakana/{env}"})
    admin.tables.create(env, "bronze.events", schema=pa.schema([pa.field("id", pa.int64())]))
    admin.tables.append(env, "bronze.events", pa.table({"id": pa.array([1, 2], pa.int64())}))
    path = stack.roots / repo
    _commit(path, {"lakana.yaml": f"repo: {repo}\ndefaults:\n  retry_delay: 0\n", **files})
    admin.repositories.create(repo, "local", str(path))
    deploy = admin.deploys.create(env, repo)
    _deploy_and_drain(client)
    admin.deploys.wait(env, deploy.id, timeout=30)
    return repo


_THREE = {
    "assets/silver/clean.sql": "SELECT id FROM bronze.events\n",
    "assets/gold/daily.sql": "SELECT count(*) AS n FROM silver.clean\n",
    "assets/gold/weekly.sql": "SELECT max(id) AS top FROM silver.clean\n",
}


def test_a_three_asset_project_runs_with_lineage(
    stack: _Stack, sdk: tuple[TestClient, lakana.Client]
) -> None:
    """Exit criterion 1: deploy, `lakana run`, every table built correctly, with lineage."""
    client, admin = sdk
    env = _name()
    repo = _project(stack, admin, client, env, _THREE)
    run = admin.runs.start(env, repo)
    finished = _until_finished(client, admin, env, run.id)
    assert finished.status == "succeeded", finished
    assert admin.sql("SELECT n FROM gold.daily", env).to_pylist() == [{"n": 2}]
    assert admin.sql("SELECT top FROM gold.weekly", env).to_pylist() == [{"top": 2}]
    assert "silver.clean" in repr(admin.tables.lineage(env, "gold.daily"))


def test_a_star_with_model_metadata_runs_its_generated_checks(
    stack: _Stack, sdk: tuple[TestClient, lakana.Client]
) -> None:
    """One fact, two dimensions and a stale reference, on Postgres and SeaweedFS.

    The fact's reference to `gold.dim_product` finds an orphan: with `checks: references: warn`
    the run succeeds with a warning, the check rows show the orphan, the catalog shows the model,
    search finds the fact by its grain, and the dimension lists the fact as referencing it.
    """
    client, admin = sdk
    env = _name()
    repo = _project(
        stack,
        admin,
        client,
        env,
        {
            "assets/bronze/sales.sql": (
                "SELECT * FROM (VALUES (DATE '2026-10-01', 1, 'a'), (DATE '2026-10-01', 2, 'zz'))"
                " AS t(day, store_id, sku)"
            ),
            "assets/gold/dim_store.sql": (
                "/* lakana\nkind: dimension\ngrain: One row per store.\nkey: store_id\n*/\n"
                "SELECT * FROM (VALUES (1), (2)) AS t(store_id)"
            ),
            "assets/gold/dim_product.sql": (
                "/* lakana\nkind: dimension\ngrain: One row per product.\nkey: sku\n*/\n"
                "SELECT * FROM (VALUES ('a')) AS t(sku)"
            ),
            "assets/gold/fact_sales.sql": (
                "/* lakana\nkind: fact\ngrain: One row per store, product and calendar day.\n"
                "key: [day, store_id, sku]\nreferences:\n  store_id: gold.dim_store\n"
                "  sku: gold.dim_product\nchecks:\n  references: warn\n*/\n"
                "SELECT day, store_id, sku FROM bronze.sales"
            ),
        },
    )
    run = admin.runs.start(env, repo)
    finished = _until_finished(client, admin, env, run.id)
    assert finished.status == "succeeded", finished
    results = {r.name: r for r in admin.checks.for_run(env, run.id)}
    orphan = results["gold.fact_sales/ref_sku"]
    assert orphan.outcome == "warned" and orphan.generated
    assert orphan.failing_rows == 1
    assert results["gold.fact_sales/ref_store_id"].outcome == "passed"
    assert results["gold.fact_sales/key_unique"].outcome == "passed"
    assert results["gold.dim_store/key_unique"].outcome == "passed"
    orphans = _rows_of_check(admin, env, run.id, "gold.fact_sales/ref_sku")
    assert [row["sku"] for row in orphans] == ["zz"]
    table = admin.tables.get(env, "gold.fact_sales")
    assert table.model is not None and table.model.kind == "fact"
    refs = admin.tables.references(env, "gold.dim_product")
    assert [r.id for r in refs.incoming] == ["gold.fact_sales/ref_sku"]
    hits = admin.search(env, "calendar day").results
    assert [h.name for h in hits] == ["gold.fact_sales"]


def test_the_sql_tasks_of_a_run_execute_in_query_children(
    stack: _Stack, sdk: tuple[TestClient, lakana.Client], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every SQL task of a run starts a query child; the run succeeds and none is left."""
    client, admin = sdk
    started: list[int | None] = []
    real_add = REGISTRY.add

    def counting_add(child: QueryChild) -> None:
        """Count the children that are registered, then register.

        Parameters
        ----------
        child
            The child being registered.
        """
        started.append(child.pid)
        real_add(child)

    monkeypatch.setattr(REGISTRY, "add", counting_add)
    env = _name()
    repo = _project(stack, admin, client, env, _THREE)
    run = admin.runs.start(env, repo)
    finished = _until_finished(client, admin, env, run.id)
    assert finished.status == "succeeded", finished
    assert admin.sql("SELECT n FROM gold.daily", env).to_pylist() == [{"n": 2}]
    assert len(started) >= 3
    assert REGISTRY.pids() == []


def test_failure_upstream_failed_resume_and_resume_after_redeploy(
    stack: _Stack, sdk: tuple[TestClient, lakana.Client]
) -> None:
    """Exit criterion 2: a failure leaves downstream upstream_failed; resume rebuilds only those."""
    client, admin = sdk
    env = _name()
    files = {
        "assets/silver/clean.sql": "SELECT id FROM bronze.events\n",
        "assets/gold/checked.sql": (
            "SELECT CASE WHEN (SELECT count(*) FROM bronze.events) < 3 "
            "THEN error('not enough events yet') ELSE 1 END AS ok FROM silver.clean\n"
        ),
        "assets/gold/after.sql": "SELECT ok FROM gold.checked\n",
    }
    repo = _project(stack, admin, client, env, files)
    first = admin.runs.start(env, repo)
    failed = _until_finished(client, admin, env, first.id)
    statuses = {t.asset: t.status for t in failed.tasks}
    assert failed.status == "failed"
    assert statuses == {
        "silver.clean": "succeeded",
        "gold.checked": "failed",
        "gold.after": "upstream_failed",
    }
    assert next(t for t in failed.tasks if t.asset == "gold.checked").attempt == 1  # permanent
    admin.tables.append(env, "bronze.events", pa.table({"id": pa.array([3], pa.int64())}))
    resumed = admin.runs.resume(env, first.id)
    assert sorted(t.asset for t in resumed.tasks) == ["gold.after", "gold.checked"]
    assert _until_finished(client, admin, env, resumed.id).status == "succeeded"
    _commit(stack.roots / repo, {"assets/gold/extra.sql": "SELECT 1 AS x\n"})
    deploy = admin.deploys.create(env, repo)
    _deploy_and_drain(client)
    admin.deploys.wait(env, deploy.id, timeout=30)
    with pytest.raises(Conflict, match="start a fresh run"):
        admin.runs.resume(env, first.id)


def test_two_workers_build_independent_branches_in_parallel(
    stack: _Stack, sdk: tuple[TestClient, lakana.Client]
) -> None:
    """Exit criterion 3a: two workers each claim tasks; no task runs twice."""
    client, admin = sdk
    env = _name()
    files = {f"assets/silver/part{i}.sql": f"{_HEAVY}\n" for i in range(4)}
    repo = _project(stack, admin, client, env, files)
    run = admin.runs.start(env, repo)
    _tick(client)
    barrier = threading.Barrier(2)

    def work(worker: str) -> None:
        barrier.wait()
        _deploy_and_drain(client, _engine(client, worker))

    threads = [threading.Thread(target=work, args=(w,)) for w in ("w1", "w2")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)
    assert _until_finished(client, admin, env, run.id).status == "succeeded"
    with Session(client.app.state.engine) as session:
        attempts = session.execute(
            select(TaskAttempt.task_run_id, TaskAttempt.worker)
            .join(TaskRun, TaskRun.id == TaskAttempt.task_run_id)
            .where(TaskRun.run_id == uuid.UUID(run.id))
        ).all()
    assert len(attempts) == 4
    assert len({task for task, _ in attempts}) == 4
    assert {worker for _, worker in attempts} == {"w1", "w2"}


def test_the_scheduler_role_moves_when_its_worker_stops(
    stack: _Stack, sdk: tuple[TestClient, lakana.Client]
) -> None:
    """Exit criterion 3b: the advisory lock follows the live worker; the API shows the holder."""
    client, admin = sdk
    db: Engine = client.app.state.engine
    first = SchedulerLoop(db, "worker-a")
    second = SchedulerLoop(db, "worker-b")
    assert first.step() is True
    assert second.step() is False
    assert admin.scheduler.status().holder == "worker-a"
    _kill_lock_connection(first)
    deadline = time.monotonic() + 10
    while not second.step():
        assert time.monotonic() < deadline
        time.sleep(0.2)
    assert admin.scheduler.status().holder == "worker-b"
    second._election.resign()


def test_a_killed_workers_task_is_reclaimed_and_nothing_is_half_written(
    stack: _Stack, sdk: tuple[TestClient, lakana.Client]
) -> None:
    """Exit criterion 3c: lease expiry → lost (counted) → another worker builds it once."""
    client, admin = sdk
    env = _name()
    repo = _project(
        stack, admin, client, env, {"assets/silver/clean.sql": "SELECT id FROM bronze.events\n"}
    )
    run = admin.runs.start(env, repo)
    _tick(client)
    with Session(client.app.state.engine) as session:
        claimed = claim_task(session, "dead", 1)
    assert claimed is not None
    time.sleep(1.5)
    _tick(client)
    with Session(client.app.state.engine) as session:
        assert (
            record_attempt(
                session, claimed.task_run_id, "dead", claimed.claim, kind=None, rand=lambda: 0.5
            )
            is False
        )
    finished = _until_finished(client, admin, env, run.id)
    assert finished.status == "succeeded"
    with Session(client.app.state.engine) as session:
        outcomes = session.scalars(
            select(TaskAttempt.outcome)
            .where(TaskAttempt.task_run_id == claimed.task_run_id)
            .order_by(TaskAttempt.started_at)
        ).all()
    assert outcomes == [AttemptOutcome.LOST, AttemptOutcome.SUCCEEDED]
    assert admin.sql("SELECT count(*) AS n FROM silver.clean", env).to_pylist() == [{"n": 2}]


def test_the_lockdown_end_to_end(stack: _Stack, sdk: tuple[TestClient, lakana.Client]) -> None:
    """Exit criterion 4: refused outside a run, built in one, break-glass audited, REST refused."""
    client, admin = sdk
    env = _name()
    repo = _project(
        stack, admin, client, env, {"assets/silver/clean.sql": "SELECT id FROM bronze.events\n"}
    )
    admin.principals.create("etl-" + env, "SERVICE_ACCOUNT")
    admin.grants.grant(
        principal="etl-" + env, role="DATA_ENGINEER", kind="ENVIRONMENT", environment=env
    )
    admin.deployments.set_run_as(env, repo, "etl-" + env)
    admin.environments.set_protection(env, True)
    with pytest.raises(SdkPermissionDenied, match="is protected"):
        admin.sql("SELECT 5::BIGINT AS id", env, into="bronze.events", mode="append")
    run = admin.runs.start(env, repo)
    assert run.run_as == "etl-" + env
    assert _until_finished(client, admin, env, run.id).status == "succeeded"
    admin.tables.append(
        env,
        "bronze.events",
        pa.table({"id": pa.array([9], pa.int64())}),
        break_glass_reason="INC-1: replay a lost event",
    )
    with Session(client.app.state.engine) as session:
        events = session.scalars(
            select(AuditEvent).where(
                AuditEvent.event_type == AuditEventType.BREAK_GLASS_WRITE,
                AuditEvent.securable_environment == env,
            )
        ).all()
    assert [e.detail["target"] for e in events] == ["bronze.events"]
    commit = client.post(
        f"/v1/{env}/namespaces/silver/tables/clean",
        headers={"Authorization": f"Bearer {_TOKEN}"},
        json={"requirements": [], "updates": []},
    )
    assert commit.status_code == 403
    assert "is protected" in commit.text


def _admin_ref(db: Engine) -> PrincipalRef:
    """Return the bootstrap admin as a principal reference.

    Parameters
    ----------
    db
        The metadata database engine.

    Returns
    -------
    PrincipalRef
        The admin.
    """
    with Session(db) as session:
        row = session.scalars(select(Principal).where(Principal.name == "admin")).one()
        return PrincipalRef(id=row.id, name=row.name, type=PrincipalType.USER)


def _race_two_starts(db: Engine, ref: PrincipalRef, env: str, repo: str) -> list[str]:
    """Start the same repository's run from two sessions at once.

    Parameters
    ----------
    db
        The metadata database engine.
    ref
        The caller.
    env
        Environment name.
    repo
        Repository name.

    Returns
    -------
    list of str
        ``started`` or ``conflict`` per attempt.
    """
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def start() -> None:
        with Session(db) as session:
            barrier.wait()
            try:
                RunService(session, AllowAllAuthorization()).start(ref, env, repo, Selection())
                outcomes.append("started")
            except ConflictError:
                outcomes.append("conflict")

    threads = [threading.Thread(target=start) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    return outcomes


def _active_runs(db: Engine, repo: str) -> list[Run]:
    """List a repository's queued or running runs.

    Parameters
    ----------
    db
        The metadata database engine.
    repo
        Repository name.

    Returns
    -------
    list of Run
        The active runs.
    """
    with Session(db) as session:
        repository = session.scalars(select(Repository).where(Repository.name == repo)).one()
        return list(
            session.scalars(
                select(Run).where(
                    Run.repository_id == repository.id,
                    Run.status.in_([RunStatus.QUEUED, RunStatus.RUNNING]),
                )
            )
        )


def _finish_active_runs(db: Engine, repo: str, status: RunStatus = RunStatus.CANCELLED) -> None:
    """Move a repository's active runs to a terminal status.

    Parameters
    ----------
    db
        The metadata database engine.
    repo
        Repository name.
    status
        The terminal status.
    """
    with Session(db) as session:
        for row in _active_runs(db, repo):
            session.execute(update(Run).where(Run.id == row.id).values(status=status))
        session.commit()


def test_two_concurrent_starts_make_exactly_one_run(
    stack: _Stack, sdk: tuple[TestClient, lakana.Client]
) -> None:
    """The partial unique index and the pointer lock hold under a real race."""
    client, admin = sdk
    env = _name()
    repo = _project(
        stack, admin, client, env, {"assets/silver/clean.sql": "SELECT id FROM bronze.events\n"}
    )
    db: Engine = client.app.state.engine
    ref = _admin_ref(db)
    for _ in range(5):
        assert sorted(_race_two_starts(db, ref, env, repo)) == ["conflict", "started"]
        assert len(_active_runs(db, repo)) == 1
        _finish_active_runs(db, repo)


def test_the_loser_of_a_start_race_is_told_the_active_run(
    stack: _Stack, sdk: tuple[TestClient, lakana.Client]
) -> None:
    """The 409 names the run that won."""
    client, admin = sdk
    env = _name()
    repo = _project(
        stack, admin, client, env, {"assets/silver/clean.sql": "SELECT id FROM bronze.events\n"}
    )
    first = admin.runs.start(env, repo)
    with pytest.raises(Conflict, match=first.id):
        admin.runs.start(env, repo)


def test_the_one_active_run_index_is_partial_on_postgres(
    stack: _Stack, sdk: tuple[TestClient, lakana.Client]
) -> None:
    """Finished runs pile up freely; one queued run inserts; a second active one is refused."""
    client, admin = sdk
    env = _name()
    repo = _project(
        stack, admin, client, env, {"assets/silver/clean.sql": "SELECT id FROM bronze.events\n"}
    )
    db: Engine = client.app.state.engine
    ref = _admin_ref(db)
    for status in (RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.SUCCEEDED):
        with Session(db) as session:
            RunService(session, AllowAllAuthorization()).start(ref, env, repo, Selection())
        _finish_active_runs(db, repo, status)
    with Session(db) as session:
        view = RunService(session, AllowAllAuthorization()).start(ref, env, repo, Selection())
    assert [r.id for r in _active_runs(db, repo)] == [view.id]
    with Session(db) as session, pytest.raises(ConflictError, match="already active"):
        RunService(session, AllowAllAuthorization()).start(ref, env, repo, Selection())


def test_claims_race_on_postgres_without_double_execution(
    stack: _Stack, sdk: tuple[TestClient, lakana.Client]
) -> None:
    """Four racing claimers take each ready task exactly once."""
    client, admin = sdk
    env = _name()
    files = {f"assets/silver/t{i}.sql": "SELECT id FROM bronze.events\n" for i in range(12)}
    repo = _project(stack, admin, client, env, files)
    admin.runs.start(env, repo)
    _tick(client)
    db: Engine = client.app.state.engine
    claimed: list[uuid.UUID] = []
    lock = threading.Lock()
    barrier = threading.Barrier(4)

    def claimer(worker: str) -> None:
        barrier.wait()
        while True:
            with Session(db) as session:
                got = claim_task(session, worker, 60)
            if got is None:
                return
            with lock:
                claimed.append(got.task_run_id)

    threads = [threading.Thread(target=claimer, args=(f"c{i}",)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert len(claimed) == 12
    assert len(set(claimed)) == 12
    with Session(db) as session:
        attempts = session.scalars(
            select(TaskAttempt.task_run_id).where(TaskAttempt.task_run_id.in_(claimed))
        ).all()
    assert sorted(attempts) == sorted(claimed)


def test_the_advisory_lock_is_exclusive_and_dies_with_its_connection(
    sdk: tuple[TestClient, lakana.Client],
) -> None:
    """One holder at a time; a dead holder's lock is taken over."""
    client, _ = sdk
    db: Engine = client.app.state.engine
    key = 0x5EED0000 + uuid.uuid4().int % 0xFFFF
    holder, rival = PostgresAdvisoryLock(db, key), PostgresAdvisoryLock(db, key)
    try:
        assert holder.acquire() is True
        assert rival.acquire() is False
        assert holder.acquire() is True
        assert holder._connection is not None
        holder._connection.invalidate()  # the process died: Postgres frees the session lock
        holder._connection = None
        deadline = time.monotonic() + 10
        while not rival.acquire():
            assert time.monotonic() < deadline, "the dead holder's lock was never freed"
            time.sleep(0.1)
        assert holder.acquire() is False
        rival.release()
        assert holder.acquire() is True
    finally:
        holder.release()
        rival.release()


_KEEPALIVE_NAMES = (
    "tcp_keepalives_idle",
    "tcp_keepalives_interval",
    "tcp_keepalives_count",
    "tcp_user_timeout",
)


def _session_keepalives(connection: Connection) -> dict[str, int]:
    """Read the TCP settings of the server session behind a connection.

    Parameters
    ----------
    connection
        An open connection.

    Returns
    -------
    dict of str to int
        Setting name to its raw value (seconds, a count, milliseconds).
    """
    rows = connection.execute(
        text("SELECT name, setting FROM pg_settings WHERE name IN :names").bindparams(
            bindparam("names", expanding=True)
        ),
        {"names": list(_KEEPALIVE_NAMES)},
    ).all()
    connection.commit()
    return {name: int(setting) for name, setting in rows}


def test_the_role_connection_and_every_other_connection_ask_the_server_to_probe_the_client(
    stack: _Stack,
) -> None:
    """The server session behind the lock, and a pooled one, carry values derived from the lease.

    This proves the settings reach the server, not that a cut network frees the role: that needs
    the kernel to drop a connection, which one host and one container network cannot produce
    deterministically.
    """
    engine = create_engine(stack.settings.database, lease_seconds=30)
    lock = PostgresAdvisoryLock(engine, 0x5EED0000 + uuid.uuid4().int % 0xFFFF)
    try:
        assert lock.acquire() is True
        assert lock._connection is not None
        expected = {
            "tcp_keepalives_idle": 10,
            "tcp_keepalives_interval": 5,
            "tcp_keepalives_count": 2,
            "tcp_user_timeout": 20_000,
        }
        assert _session_keepalives(lock._connection) == expected
        with engine.connect() as other:
            assert _session_keepalives(other) == expected
    finally:
        lock.release()
        engine.dispose()


def _claim_with_short_lease(
    stack: _Stack, admin: lakana.Client, client: TestClient, tasks: int
) -> tuple[str, str, list[ClaimedTask]]:
    """Deploy `tasks` independent assets, start a run and claim every task on a 1 s lease.

    Parameters
    ----------
    stack
        The services.
    admin
        The SDK client.
    client
        The test client.
    tasks
        How many assets.

    Returns
    -------
    tuple
        Environment, repository and the claims.
    """
    env = _name()
    files = {f"assets/silver/t{i}.sql": "SELECT id FROM bronze.events\n" for i in range(tasks)}
    repo = _project(stack, admin, client, env, files)
    admin.runs.start(env, repo)
    _tick(client)
    claims: list[ClaimedTask] = []
    with Session(client.app.state.engine) as session:
        while (claim := claim_task(session, "slow", 1)) is not None:
            claims.append(claim)
    assert len(claims) == tasks
    return env, repo, claims


def _expire_leases(db: Engine) -> None:
    """Wait until every held lease is past its expiry on the database's clock.

    Parameters
    ----------
    db
        The metadata database engine.
    """
    with Session(db) as session:
        newest = session.scalars(select(func.max(TaskRun.lease_expires_at))).one()
        remaining = (newest - db_now(session)).total_seconds() if newest else 0
    time.sleep(max(remaining, 0) + 0.3)


def test_a_late_renewal_beats_a_reclaim_that_has_read_the_expired_lease(
    stack: _Stack, sdk: tuple[TestClient, lakana.Client]
) -> None:
    """Scheduler snapshot sees an expired lease; a renewal lands; the reclaim must miss."""
    client, admin = sdk
    db: Engine = client.app.state.engine
    _, _, claims = _claim_with_short_lease(stack, admin, client, 1)
    task = claims[0]
    _expire_leases(db)
    with Session(db) as session:
        now = db_now(session)
        transitions = sched_tick(load_snapshots(session), now, lambda: 0.5)
        assert any(isinstance(t, TaskTransition) and t.lost for t in transitions)
        with Session(db) as worker:
            assert renew_lease(worker, task.task_run_id, "slow", task.claim, 60) is LeaseState.HELD
        assert apply_transitions(session, transitions, now) is False
    with Session(db) as session:
        row = session.get(TaskRun, task.task_run_id)
        assert row is not None
        assert (row.status, row.lease_owner, row.attempt) == (TaskStatus.RUNNING, "slow", 1)
        open_attempts = session.scalars(
            select(TaskAttempt).where(
                TaskAttempt.task_run_id == task.task_run_id, TaskAttempt.finished_at.is_(None)
            )
        ).all()
        assert len(open_attempts) == 1


def test_a_reclaim_beats_a_late_finish_and_the_zombie_writes_nothing(
    stack: _Stack, sdk: tuple[TestClient, lakana.Client]
) -> None:
    """After the reclaim the old attempt can neither renew nor finish; the new one runs once."""
    client, admin = sdk
    db: Engine = client.app.state.engine
    _, _, claims = _claim_with_short_lease(stack, admin, client, 1)
    zombie = claims[0]
    _expire_leases(db)
    _tick(client)
    _tick(client)  # retry_delay is 0: pending → ready
    with Session(db) as session:
        assert renew_lease(session, zombie.task_run_id, "slow", zombie.claim, 60) is LeaseState.LOST
    with Session(db) as session:
        assert (
            record_attempt(
                session, zombie.task_run_id, "slow", zombie.claim, kind=None, rand=lambda: 0.5
            )
            is False
        )
    with Session(db) as session:
        fresh = claim_task(session, "fast", 60)
    assert fresh is not None
    assert (fresh.task_run_id, fresh.attempt) == (zombie.task_run_id, 2)
    with Session(db) as session:  # the zombie of attempt 1 still cannot finish attempt 2
        assert (
            record_attempt(
                session, zombie.task_run_id, "slow", zombie.claim, kind=None, rand=lambda: 0.5
            )
            is False
        )
    with Session(db) as session:
        assert (
            record_attempt(
                session, fresh.task_run_id, "fast", fresh.claim, kind=None, rand=lambda: 0.5
            )
            is True
        )
        outcomes = session.scalars(
            select(TaskAttempt.outcome)
            .where(TaskAttempt.task_run_id == zombie.task_run_id)
            .order_by(TaskAttempt.attempt)
        ).all()
    assert outcomes == [AttemptOutcome.LOST, AttemptOutcome.SUCCEEDED]


def test_reclaim_and_finish_racing_exactly_one_wins_per_task(
    stack: _Stack, sdk: tuple[TestClient, lakana.Client]
) -> None:
    """A real race over expired leases: each task is either finished or lost, never both."""
    client, admin = sdk
    db: Engine = client.app.state.engine
    _, _, claims = _claim_with_short_lease(stack, admin, client, 10)
    _expire_leases(db)
    barrier = threading.Barrier(2)
    finished: dict[uuid.UUID, bool] = {}

    def scheduler() -> None:
        barrier.wait()
        with Session(db) as session:
            scheduler_tick(session, db_now(session), lambda: 0.5)

    def worker() -> None:
        barrier.wait()
        for claim in claims:
            with Session(db) as session:
                finished[claim.task_run_id] = record_attempt(
                    session, claim.task_run_id, "slow", claim.claim, kind=None, rand=lambda: 0.5
                )

    threads = [threading.Thread(target=scheduler), threading.Thread(target=worker)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert len(finished) == 10
    with Session(db) as session:
        for claim in claims:
            row = session.get(TaskRun, claim.task_run_id)
            assert row is not None
            attempt = session.scalars(
                select(TaskAttempt).where(TaskAttempt.task_run_id == claim.task_run_id)
            ).one()
            if finished[claim.task_run_id]:
                assert row.status is TaskStatus.SUCCEEDED
                assert attempt.outcome is AttemptOutcome.SUCCEEDED
            else:
                assert row.status in {TaskStatus.PENDING, TaskStatus.READY}
                assert row.lease_owner is None
                assert attempt.outcome is AttemptOutcome.LOST


def _start_in_thread(
    db: Engine, ref: PrincipalRef, env: str, repo: str
) -> tuple[threading.Thread, list[BaseException | str]]:
    """Start a run from a background thread, recording the outcome.

    Parameters
    ----------
    db
        The metadata database engine.
    ref
        The caller.
    env
        Environment name.
    repo
        Repository name.

    Returns
    -------
    tuple
        The started thread and a list that receives the run id or the exception.
    """
    outcome: list[BaseException | str] = []

    def start() -> None:
        try:
            with Session(db) as session:
                view = RunService(session, AllowAllAuthorization()).start(
                    ref, env, repo, Selection()
                )
            outcome.append(str(view.id))
        except BaseException as error:
            outcome.append(error)

    thread = threading.Thread(target=start)
    thread.start()
    return thread, outcome


def _user(admin: lakana.Client, db: Engine) -> PrincipalRef:
    """Create a user principal.

    Parameters
    ----------
    admin
        The SDK client.
    db
        The metadata database engine.

    Returns
    -------
    PrincipalRef
        The new user.
    """
    name = _name()
    admin.principals.create(name, "USER")
    with Session(db) as session:
        row = session.scalars(select(Principal).where(Principal.name == name)).one()
        return PrincipalRef(id=row.id, name=row.name, type=PrincipalType.USER)


def test_a_run_start_sees_a_protection_flip_that_commits_first(
    stack: _Stack, sdk: tuple[TestClient, lakana.Client]
) -> None:
    """The start waits on the flip's row lock, then refuses: no caller-run in a protected env."""
    client, admin = sdk
    db: Engine = client.app.state.engine
    env = _name()
    repo = _project(
        stack, admin, client, env, {"assets/silver/clean.sql": "SELECT id FROM bronze.events\n"}
    )
    ref = _user(admin, db)
    with db.connect() as flipper:
        flipper.execute(
            text("UPDATE environments SET is_protected = true WHERE name = :n"), {"n": env}
        )
        thread, outcome = _start_in_thread(db, ref, env, repo)
        _wait_for_lock_waiter(db, _backend_pid(flipper))
        assert outcome == []
        flipper.commit()
    thread.join(timeout=30)
    assert len(outcome) == 1
    assert isinstance(outcome[0], ConflictError)
    assert _active_runs(db, repo) == []


def test_a_protection_flip_waits_for_a_run_start_that_holds_the_environment(
    stack: _Stack, sdk: tuple[TestClient, lakana.Client]
) -> None:
    """A start's FOR SHARE on the environment row blocks the flip until the start commits."""
    client, admin = sdk
    db: Engine = client.app.state.engine
    env = _name()
    _project(
        stack, admin, client, env, {"assets/silver/clean.sql": "SELECT id FROM bronze.events\n"}
    )
    done: list[object] = []
    with db.connect() as starter:
        starter.execute(
            text("SELECT is_protected FROM environments WHERE name = :n FOR SHARE"), {"n": env}
        )

        def flip() -> None:
            done.append(admin.environments.set_protection(env, True))

        thread = threading.Thread(target=flip)
        thread.start()
        _wait_for_lock_waiter(db, _backend_pid(starter))
        assert done == []
        starter.commit()
    thread.join(timeout=30)
    assert len(done) == 1


def test_a_run_start_sees_a_principal_deactivation_that_commits_first(
    stack: _Stack, sdk: tuple[TestClient, lakana.Client]
) -> None:
    """The start waits on the deactivation's row lock, then refuses the inactive principal."""
    client, admin = sdk
    db: Engine = client.app.state.engine
    env = _name()
    repo = _project(
        stack, admin, client, env, {"assets/silver/clean.sql": "SELECT id FROM bronze.events\n"}
    )
    ref = _user(admin, db)
    with db.connect() as deactivator:
        deactivator.execute(
            text("UPDATE principals SET is_active = false WHERE id = :i"), {"i": ref.id}
        )
        thread, outcome = _start_in_thread(db, ref, env, repo)
        _wait_for_lock_waiter(db, _backend_pid(deactivator))
        assert outcome == []
        deactivator.commit()
    thread.join(timeout=30)
    assert len(outcome) == 1
    assert isinstance(outcome[0], ConflictError)
    assert "deactivated" in str(outcome[0])
    assert _active_runs(db, repo) == []


def test_deleting_an_environment_cannot_deadlock_a_run_start(
    stack: _Stack, sdk: tuple[TestClient, lakana.Client], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`delete_environment` locks env then pointer, a start pointer then env: no deadlock."""
    client, admin = sdk
    db: Engine = client.app.state.engine
    env = _name()
    repo = _project(
        stack, admin, client, env, {"assets/silver/clean.sql": "SELECT id FROM bronze.events\n"}
    )
    ref = _admin_ref(db)
    pointer_locked, delete_finished = threading.Event(), threading.Event()
    original = DeployService.lock_deployment

    def paused(
        self: DeployService,
        environment_id: uuid.UUID,
        repository_id: uuid.UUID,
        job_version_id: uuid.UUID,
    ) -> None:
        original(self, environment_id, repository_id, job_version_id)
        pointer_locked.set()  # this transaction now holds the pointer row
        delete_finished.wait(timeout=4)

    monkeypatch.setattr(DeployService, "lock_deployment", paused)
    start, started = _start_in_thread(db, ref, env, repo)
    assert pointer_locked.wait(timeout=10)
    deleted: list[BaseException | None] = []

    def delete() -> None:
        try:
            with Session(db) as session:
                CatalogService(
                    session, StorageResolver(session, None), AllowAllAuthorization()
                ).delete_environment(ref, env)
            deleted.append(None)
        except BaseException as error:
            deleted.append(error)
        finally:
            delete_finished.set()

    remover = threading.Thread(target=delete)
    remover.start()
    remover.join(timeout=30)
    start.join(timeout=30)
    errors = [str(e) for e in (*deleted, *started) if isinstance(e, BaseException)]
    assert not any("deadlock" in message.lower() for message in errors), errors
    assert len(deleted) == 1
    assert isinstance(deleted[0], ConflictError), deleted
    assert len(started) == 1
    assert isinstance(started[0], str), started
    assert len(_active_runs(db, repo)) == 1


def test_db_now_is_aware_utc_and_fresh_per_transaction(
    sdk: tuple[TestClient, lakana.Client],
) -> None:
    """Postgres hands back an aware timestamp; a new transaction gets a later one."""
    client, _ = sdk
    db: Engine = client.app.state.engine
    stamps = []
    for _ in range(3):
        with Session(db) as session:
            stamps.append(db_now(session))
        time.sleep(0.05)
    assert all(s.tzinfo is not None and s.utcoffset() == timedelta(0) for s in stamps)
    assert stamps == sorted(stamps)
    assert len(set(stamps)) == 3
    assert abs((stamps[0] - datetime.now(UTC)).total_seconds()) < 30


def _claimed_write(
    stack: _Stack, admin: lakana.Client, client: TestClient, *, protected: bool
) -> tuple[str, ClaimedTask, uuid.UUID, PrincipalRef]:
    """Deploy one asset as a bound service account, claim its task and bind a write to it.

    Parameters
    ----------
    stack
        The services.
    admin
        The SDK client.
    client
        The test client.
    protected
        Whether the environment is protected.

    Returns
    -------
    tuple
        The environment, the claim, the attempt's write run id and the run
        principal with the attempt's run context.
    """
    db: Engine = client.app.state.engine
    env = _name()
    repo = _project(
        stack, admin, client, env, {"assets/bronze/events.sql": "SELECT 1::BIGINT AS id\n"}
    )
    service = "etl-" + env
    admin.principals.create(service, "SERVICE_ACCOUNT")
    admin.grants.grant(principal=service, role="DATA_ENGINEER", kind="ENVIRONMENT", environment=env)
    admin.deployments.set_run_as(env, repo, service)
    if protected:
        admin.environments.set_protection(env, True)
    admin.runs.start(env, repo)
    _tick(client)
    with Session(db) as session:
        claimed = claim_task(session, "zombie", 60)
        assert claimed is not None
        run_row = session.scalars(select(Principal).where(Principal.name == service)).one()
        env_row = session.scalars(select(Environment).where(Environment.name == env)).one()
        write = QueryRun(
            id=uuid.uuid4(),
            environment_id=env_row.id,
            principal_id=run_row.id,
            sql_text="SELECT 42::BIGINT AS id",
            status=QueryRunStatus.RUNNING,
            write_mode=WriteMode.APPEND,
            target_name=claimed.asset,
            task_run_id=claimed.task_run_id,
        )
        session.add(write)
        session.flush()
        session.execute(
            update(TaskRun).where(TaskRun.id == claimed.task_run_id).values(query_run_id=write.id)
        )
        session.commit()
        principal = PrincipalRef(
            id=run_row.id,
            name=run_row.name,
            type=run_row.type,
            run_context=RunContext(task_run_id=claimed.task_run_id, query_run_id=write.id),
        )
        return env, claimed, write.id, principal


def _reclaim_now(db: Engine, claimed: ClaimedTask) -> int:
    """Expire the claim's lease and run one real scheduler tick in its own session.

    Parameters
    ----------
    db
        The metadata database engine.
    claimed
        The claim to reclaim.

    Returns
    -------
    int
        What the tick returned.
    """
    with Session(db) as other:
        other.execute(
            update(TaskRun)
            .where(TaskRun.id == claimed.task_run_id)
            .values(lease_expires_at=db_now(other) - timedelta(seconds=5))
        )
        other.commit()
        return scheduler_tick(other, db_now(other), lambda: 0.5)


@pytest.mark.parametrize("protected", [True, False], ids=["protected", "unprotected"])
def test_a_reclaim_between_the_guard_check_and_the_commit_lands_no_write(
    stack: _Stack,
    sdk: tuple[TestClient, lakana.Client],
    monkeypatch: pytest.MonkeyPatch,
    protected: bool,
) -> None:
    """The guard passed for a running attempt; a reclaim lands before the write commits (I1)."""
    client, admin = sdk
    db: Engine = client.app.state.engine
    env, claimed, _, principal = _claimed_write(stack, admin, client, protected=protected)
    before = admin.sql("SELECT count(*) AS n FROM bronze.events", env).to_pylist()
    reclaimed: list[int] = []
    original = catalog_service.commit_metadata_pointer

    def reclaim_first(
        pointer_session: Session,
        table_id: uuid.UUID,
        *,
        expected_location: str | None,
        new_location: str,
        publish: bool = False,
    ) -> None:
        reclaimed.append(_reclaim_now(db, claimed))
        original(
            pointer_session,
            table_id,
            expected_location=expected_location,
            new_location=new_location,
            publish=publish,
        )

    monkeypatch.setattr(catalog_service, "commit_metadata_pointer", reclaim_first)
    with Session(db) as session:
        catalog = CatalogService(
            session, StorageResolver(session, None), GrantAuthorization(session)
        )
        ref = TableRef.from_dotted(env, claimed.asset)
        with pytest.raises(PermissionDenied) as info:
            catalog.append(principal, ref, pa.table({"id": pa.array([42], pa.int64())}))
    expected = PROTECTED_MESSAGE.format(environment=env) if protected else SUPERSEDED_MESSAGE
    assert str(info.value) == expected
    assert reclaimed
    assert admin.sql("SELECT count(*) AS n FROM bronze.events", env).to_pylist() == before
    with Session(db) as session:
        row = session.get(TaskRun, claimed.task_run_id)
        assert row is not None
        assert row.status is not TaskStatus.RUNNING


def test_a_write_committed_just_before_its_reclaim_ends_the_task_succeeded_without_a_rerun(
    stack: _Stack, sdk: tuple[TestClient, lakana.Client], monkeypatch: pytest.MonkeyPatch
) -> None:
    """On Postgres: the reclaim finds the evidence; the task is succeeded and not rerun."""
    client, admin = sdk
    db: Engine = client.app.state.engine
    env, claimed, write_id, _ = _claimed_write(stack, admin, client, protected=False)
    original = pipeline._run_write

    def reclaim_after_commit(*args: object, **kwargs: object) -> int:
        rows = original(*args, **kwargs)  # ty: ignore[invalid-argument-type]
        _reclaim_now(db, claimed)
        return rows

    monkeypatch.setattr(pipeline, "_run_write", reclaim_after_commit)
    with Session(db) as session:
        assert run_one(session, write_id, GrantAuthorization(session)) is None
    with Session(db) as session:
        write = session.get(QueryRun, write_id)
        assert write is not None
        assert (write.status, write.error_message) == (QueryRunStatus.SUCCEEDED, None)
        task = session.get(TaskRun, claimed.task_run_id)
        assert task is not None
        assert (task.status, task.attempt) == (TaskStatus.SUCCEEDED, 1)
        (attempt,) = session.scalars(
            select(TaskAttempt).where(TaskAttempt.task_run_id == claimed.task_run_id)
        )
        assert attempt.outcome is AttemptOutcome.SUCCEEDED
    ids = admin.sql("SELECT id FROM bronze.events ORDER BY id", env).to_pylist()
    assert ids.count({"id": 42}) == 1  # the write committed once, and nothing will build it again


def test_a_reclaim_waits_for_a_write_that_already_holds_its_task_and_then_applies(
    stack: _Stack, sdk: tuple[TestClient, lakana.Client], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The waiting side of `record_pending`'s FOR SHARE: the write lands, then the reclaim."""
    client, admin = sdk
    db: Engine = client.app.state.engine
    env, claimed, _, principal = _claimed_write(stack, admin, client, protected=True)
    before = admin.sql("SELECT count(*) AS n FROM bronze.events", env).to_pylist()[0]["n"]
    with Session(db) as other:
        other.execute(
            update(TaskRun)
            .where(TaskRun.id == claimed.task_run_id)
            .values(lease_expires_at=db_now(other) - timedelta(seconds=5))
        )
        other.commit()
    applied: list[int | BaseException] = []
    threads: list[threading.Thread] = []
    seen_waiting: list[bool] = []

    def reclaim() -> None:
        try:
            with Session(db) as scheduler:
                applied.append(scheduler_tick(scheduler, db_now(scheduler), lambda: 0.5))
        except BaseException as error:
            applied.append(error)

    original = ProtectedWriteGuard.record_pending

    def hold_then_let_the_reclaim_queue(self: ProtectedWriteGuard) -> None:
        original(self)  # the task run and run rows are now locked FOR SHARE
        thread = threading.Thread(target=reclaim)
        thread.start()
        threads.append(thread)
        _wait_for_lock_waiter(db, _backend_pid(self._session))
        seen_waiting.append(applied == [])

    monkeypatch.setattr(ProtectedWriteGuard, "record_pending", hold_then_let_the_reclaim_queue)
    with Session(db) as session:
        catalog = CatalogService(
            session, StorageResolver(session, None), GrantAuthorization(session)
        )
        ref = TableRef.from_dotted(env, claimed.asset)
        result = catalog.append(principal, ref, pa.table({"id": pa.array([42], pa.int64())}))
    assert result.rows == 1
    assert seen_waiting and all(seen_waiting)
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive()
    assert all(isinstance(count, int) and count >= 1 for count in applied), applied
    after = admin.sql("SELECT count(*) AS n FROM bronze.events", env).to_pylist()[0]["n"]
    assert after == before + 1
    with Session(db) as session:
        row = session.get(TaskRun, claimed.task_run_id)
        assert row is not None
        assert row.status is not TaskStatus.RUNNING
        attempt = session.scalars(
            select(TaskAttempt).where(TaskAttempt.task_run_id == claimed.task_run_id)
        ).one()
        assert attempt.outcome is AttemptOutcome.LOST


# --- data quality checks, the rollback and the not-published state -------------------

_WAIT = 30.0
"""Seconds a thread of a check test waits for an event before its assertion fails."""

_IDS = pa.schema([pa.field("id", pa.int64())])
_UNIQUE = "SELECT id FROM silver.orders GROUP BY id HAVING count(*) > 1"
_ONES = "SELECT CAST(x AS BIGINT) AS id FROM (VALUES (1), (1)) AS t(x)\n"
_REPORT = "SELECT count(*) AS n FROM silver.orders\n"
_READINGS = pa.schema([pa.field("ts", pa.int64()), pa.field("v", pa.int64())])
_INCREMENT = (
    "/* lakana\nmaterialize: incremental\nincremental_key: ts\n*/\n"
    "SELECT ts, v FROM bronze.readings\n"
)


def _redeploy(stack: _Stack, admin: lakana.Client, client: TestClient, env: str, repo: str) -> None:
    """Commit whatever changed in a project and deploy it.

    Parameters
    ----------
    stack
        The services.
    admin
        The SDK client.
    client
        The test client.
    env
        Environment name.
    repo
        Repository name.
    """
    _commit(stack.roots / repo, {})
    deploy = admin.deploys.create(env, repo)
    _deploy_and_drain(client)
    admin.deploys.wait(env, deploy.id, timeout=30)


def _checked_project(
    stack: _Stack,
    admin: lakana.Client,
    client: TestClient,
    env: str,
    files: dict[str, str],
    checks: dict[tuple[str, str], str],
    *,
    on_failure: str | None = None,
    config: str = "",
) -> str:
    """Create `env`, and register and deploy a project with assets and checks.

    Parameters
    ----------
    stack
        The services.
    admin
        The SDK client.
    client
        The test client.
    env
        Environment name.
    files
        Project files besides `lakana.yaml`.
    checks
        ``(table, check)`` to the check's SQL.
    on_failure
        The ``on_failure`` of every check; `None` writes none, so the default applies.
    config
        Lines appended to the project's ``lakana.yaml``, e.g. a ``checks:`` section.

    Returns
    -------
    str
        The repository name.
    """
    repo = _name()
    admin.environments.create(env, storage={**stack.storage, "root": f"s3://lakana/{env}"})
    admin.tables.create(env, "bronze.events", schema=_IDS)
    admin.tables.append(env, "bronze.events", pa.table({"id": pa.array([1, 2], pa.int64())}))
    path = stack.roots / repo
    for (table, check), sql in checks.items():
        _write_check(path, table, check, sql, on_failure=on_failure)
    _commit(path, {"lakana.yaml": f"repo: {repo}\ndefaults:\n  retry_delay: 0\n{config}", **files})
    admin.repositories.create(repo, "local", str(path))
    deploy = admin.deploys.create(env, repo)
    _deploy_and_drain(client)
    admin.deploys.wait(env, deploy.id, timeout=30)
    return repo


def _task(run: SdkRun, asset: str) -> RunTask:
    """Pick one task of a run.

    Parameters
    ----------
    run
        The run.
    asset
        The task's asset.

    Returns
    -------
    RunTask
        The task.
    """
    return next(task for task in run.tasks if task.asset == asset)


def _events(db: Engine, env: str, kind: AuditEventType) -> list[dict[str, object]]:
    """List the details of the audit events of one type in one environment, oldest first.

    Parameters
    ----------
    db
        The metadata database engine.
    env
        Environment name.
    kind
        The event type.

    Returns
    -------
    list of dict
        One detail per event.
    """
    with Session(db) as session:
        return [
            dict(event.detail)
            for event in session.scalars(
                select(AuditEvent)
                .where(AuditEvent.event_type == kind, AuditEvent.securable_environment == env)
                .order_by(AuditEvent.occurred_at, AuditEvent.id)
            )
        ]


def _failure(
    db: Engine,
    env: str,
    run_id: str,
    asset: str,
    rollback: Rollback | None,
    *,
    refused: bool = False,
) -> str:
    """Build the message a task with failed blocking checks must carry, from its stored results.

    Parameters
    ----------
    db
        The metadata database engine.
    env
        Environment name.
    run_id
        The run.
    asset
        The task's asset.
    rollback
        What the rollback did; `None` for a source check.
    refused
        Whether the catalog refused the rollback.

    Returns
    -------
    str
        `lakana_core.runs.checks.failure_message` of the task's blocking results.
    """
    with Session(db) as session:
        task_id = session.scalars(
            select(TaskRun.id).where(TaskRun.run_id == uuid.UUID(run_id), TaskRun.asset == asset)
        ).one()
        return check_failure_message(
            verdict(results_of_task(session, task_id)).blocking,
            table=asset,
            run_id=uuid.UUID(run_id),
            environment=env,
            rollback=rollback,
            refused=refused,
        )


def _ids(admin: lakana.Client, env: str, table: str = "silver.orders") -> list[int]:
    """Read a table's ids through a query.

    Parameters
    ----------
    admin
        The SDK client.
    env
        Environment name.
    table
        The table.

    Returns
    -------
    list of int
        The ids, sorted.
    """
    return admin.sql(f"SELECT id FROM {table} ORDER BY id", env).column("id").to_pylist()


def _refs(db: Engine, env: str, table: str) -> set[str]:
    """Read the names of a table's snapshot references from its current metadata file.

    Parameters
    ----------
    db
        The metadata database engine.
    env
        Environment name.
    table
        The table's dotted name.

    Returns
    -------
    set of str
        The reference names (``main`` and tags).
    """
    with Session(db) as session:
        ref = TableRef.from_dotted(env, table)
        row = CatalogService(
            session, StorageResolver(session, None), AllowAllAuthorization()
        ).load_table_row(_admin_ref(db), ref)
        assert row.metadata_location is not None
        resolved = StorageResolver(session, None).for_environment(env)
        return set(read_metadata(resolved.file_io, row.metadata_location).refs)


def _storage(db: Engine, env: str) -> set[str]:
    """List every object under an environment's storage root.

    Parameters
    ----------
    db
        The metadata database engine.
    env
        Environment name.

    Returns
    -------
    set of str
        Root-relative paths.
    """
    with Session(db) as session:
        return set(StorageResolver(session, None).for_environment(env).storage.list(""))


def test_a_failing_check_sets_a_replaced_table_back_and_the_next_run_builds_it_again(
    stack: _Stack, sdk: tuple[TestClient, lakana.Client]
) -> None:
    client, admin = sdk
    db: Engine = client.app.state.engine
    env = _name()
    files = {
        "assets/silver/orders.sql": "SELECT id FROM bronze.events\n",
        "assets/gold/report.sql": _REPORT,
    }
    repo = _project(stack, admin, client, env, files)
    first = _until_finished(client, admin, env, admin.runs.start(env, repo).id)
    assert first.status == "succeeded", first
    assert _ids(admin, env) == [1, 2]

    path = stack.roots / repo
    (path / "assets/silver/orders.sql").write_text(_ONES)
    _write_check(path, "silver.orders", "unique_id", _UNIQUE)
    _redeploy(stack, admin, client, env, repo)
    second = _until_finished(client, admin, env, admin.runs.start(env, repo).id)
    assert second.status == "failed", second
    orders = _task(second, "silver.orders")
    assert (orders.status, orders.attempt) == ("failed", 1)
    assert orders.error_message is not None and orders.error_message.startswith(
        "Check `silver.orders/unique_id` failed: 1 row (checks/silver/orders/unique_id.sql). "
        + ROLLBACK_SENTENCES[RollbackOutcome.ROLLED_BACK]
    )
    assert orders.error_message == _failure(
        db, env, second.id, "silver.orders", Rollback(RollbackOutcome.ROLLED_BACK)
    )
    assert _task(second, "gold.report").status == "upstream_failed"
    assert _ids(admin, env) == [1, 2]  # what the table was before the run
    assert admin.sql("SELECT n FROM gold.report", env).to_pylist() == [{"n": 2}]
    (result,) = admin.checks.for_run(env, second.id)
    assert (result.name, result.outcome, result.failing_rows, result.rollback_outcome) == (
        "silver.orders/unique_id",
        "failed",
        1,
        "rolled_back",
    )
    assert (result.policy_applied, result.policy_note) == (False, None)
    failed = _events(db, env, AuditEventType.CHECK_FAILED)
    rolled = _events(db, env, AuditEventType.TABLE_ROLLED_BACK)
    assert [(e["check"], e["rows"], e["run_id"]) for e in failed] == [
        ("silver.orders/unique_id", 1, second.id)
    ]
    assert [(e["outcome"], e["table"], e["checks"]) for e in rolled] == [
        ("rolled_back", "silver.orders", ["silver.orders/unique_id"])
    ]
    assert _refs(db, env, "silver.orders") == {"main", failed_snapshot_tag(uuid.UUID(second.id))}
    # A count hands its one row to the worker: no result object is stored for it, while the
    # reads above each left one.
    stored = {p.split("/")[2] for p in _storage(db, env) if p.startswith("_artifacts/queries/")}
    assert stored and result.query_run_id is not None
    assert result.query_run_id not in stored

    (path / "assets/silver/orders.sql").write_text(
        "SELECT CAST(x AS BIGINT) AS id FROM (VALUES (3), (4)) AS t(x)\n"
    )
    _redeploy(stack, admin, client, env, repo)
    third = _until_finished(client, admin, env, admin.runs.start(env, repo).id)
    assert third.status == "succeeded", third
    assert _ids(admin, env) == [3, 4]
    assert [
        (r.name, r.outcome, r.rollback_outcome) for r in admin.checks.for_run(env, third.id)
    ] == [("silver.orders/unique_id", "passed", None)]
    assert admin.sql("SELECT n FROM gold.report", env).to_pylist() == [{"n": 2}]


def test_an_increment_is_taken_again_after_a_rollback(
    stack: _Stack, sdk: tuple[TestClient, lakana.Client]
) -> None:
    """Review focus 5: the rolled-back rows are appended once by the next run, not twice."""
    client, admin = sdk
    db: Engine = client.app.state.engine
    env, repo = _name(), _name()
    admin.environments.create(env, storage={**stack.storage, "root": f"s3://lakana/{env}"})
    admin.tables.create(env, "bronze.readings", schema=_READINGS)
    admin.tables.create(env, "silver.readings", schema=_READINGS)
    admin.tables.append(
        env, "bronze.readings", pa.table({"ts": [1, 2, 3], "v": [10, 20, 30]}, schema=_READINGS)
    )
    path = stack.roots / repo
    _write_check(path, "silver.readings", "positive", "SELECT ts FROM silver.readings WHERE v < 0")
    _commit(
        path,
        {
            "lakana.yaml": f"repo: {repo}\ndefaults:\n  retry_delay: 0\n",
            "assets/silver/readings.sql": _INCREMENT,
        },
    )
    admin.repositories.create(repo, "local", str(path))
    _redeploy(stack, admin, client, env, repo)

    def counts() -> dict[str, int]:
        """Count the rows and the distinct keys of `silver.readings`.

        Returns
        -------
        dict
            ``n`` and ``d``.
        """
        return admin.sql(
            "SELECT count(*) AS n, count(DISTINCT ts) AS d FROM silver.readings", env
        ).to_pylist()[0]

    first = _until_finished(client, admin, env, admin.runs.start(env, repo).id)
    assert first.status == "succeeded", first
    assert (_task(first, "silver.readings").rows_written, counts()) == (3, {"n": 3, "d": 3})

    admin.tables.append(
        env, "bronze.readings", pa.table({"ts": [4, 5], "v": [40, -1]}, schema=_READINGS)
    )
    second = _until_finished(client, admin, env, admin.runs.start(env, repo).id)
    task = _task(second, "silver.readings")
    assert (second.status, task.status, task.rows_written) == ("failed", "failed", 2)
    assert task.error_message == _failure(
        db, env, second.id, "silver.readings", Rollback(RollbackOutcome.ROLLED_BACK)
    )
    assert counts() == {"n": 3, "d": 3}

    _write_check(path, "silver.readings", "positive", "SELECT ts FROM silver.readings WHERE v < -5")
    _redeploy(stack, admin, client, env, repo)
    third = _until_finished(client, admin, env, admin.runs.start(env, repo).id)
    assert third.status == "succeeded", third
    assert _task(third, "silver.readings").rows_written == 2  # the same two rows, once
    assert counts() == {"n": 5, "d": 5}


def _unpublished(
    stack: _Stack, admin: lakana.Client, client: TestClient
) -> tuple[str, str, SdkRun, str]:
    """Deploy a project whose first build of `silver.orders` fails its check, and run it.

    Parameters
    ----------
    stack
        The services.
    admin
        The SDK client.
    client
        The test client.

    Returns
    -------
    tuple
        The environment, the repository, the failed run and the text every read of the
        table must give (`not_published_message`).
    """
    db: Engine = client.app.state.engine
    env = _name()
    repo = _checked_project(
        stack,
        admin,
        client,
        env,
        {"assets/silver/orders.sql": "SELECT id FROM bronze.events\n"},
        {("silver.orders", "no_twos"): "SELECT id FROM silver.orders WHERE id = 2"},
    )
    run = _until_finished(client, admin, env, admin.runs.start(env, repo).id)
    assert run.status == "failed", run
    assert _task(run, "silver.orders").error_message == _failure(
        db, env, run.id, "silver.orders", Rollback(RollbackOutcome.UNPUBLISHED)
    )
    table = admin.tables.get(env, "silver.orders")
    with Session(db) as session:
        text_ = not_published_message(
            session, "silver.orders", uuid.UUID(table.uuid), uuid.UUID(run.id), env
        )
    assert f"the check `no_twos` (run {run.id})" in text_
    return env, repo, run, text_


def test_a_first_build_that_fails_is_not_published_and_a_read_says_why(
    stack: _Stack, sdk: tuple[TestClient, lakana.Client]
) -> None:
    client, admin = sdk
    db: Engine = client.app.state.engine
    env, repo, run, why = _unpublished(stack, admin, client)
    table = admin.tables.get(env, "silver.orders")
    assert (table.state, table.state_message, table.snapshot_id) == ("not_published", why, None)
    assert [(t.name, t.state) for t in admin.tables.list(env, "silver")] == [
        ("orders", "not_published")
    ]
    (result,) = admin.checks.for_run(env, run.id)
    assert (result.outcome, result.failing_rows, result.rollback_outcome) == (
        "failed",
        1,
        "unpublished",
    )
    with pytest.raises(QueryFailed) as refused:
        admin.sql("SELECT * FROM silver.orders", env)
    assert (str(refused.value), refused.value.cause) == (why, "not_found")
    # An engine that speaks the Iceberg REST protocol: its own "no such table", with the text.
    with serving(create_app(stack.settings)) as url:
        catalog = RestCatalog(name="lakana", uri=url, prefix=env, token=_TOKEN)
        with pytest.raises(NoSuchTableError) as missing:
            catalog.load_table(("silver", "orders"))
    assert why in str(missing.value)
    assert _refs(db, env, "silver.orders") == {failed_snapshot_tag(uuid.UUID(run.id))}  # no main

    # A second build that fails as well finds the table not published: it is that again, by
    # the newer run, and the first run's kept snapshot is still there.
    twice = _until_finished(client, admin, env, admin.runs.start(env, repo).id)
    assert twice.status == "failed", twice
    assert _task(twice, "silver.orders").error_message == _failure(
        db, env, twice.id, "silver.orders", Rollback(RollbackOutcome.UNPUBLISHED)
    )
    table = admin.tables.get(env, "silver.orders")
    assert table.state == "not_published" and table.state_message is not None
    assert f"(run {twice.id})" in table.state_message
    assert _refs(db, env, "silver.orders") == {
        failed_snapshot_tag(uuid.UUID(run.id)),
        failed_snapshot_tag(uuid.UUID(twice.id)),
    }

    _write_check(
        stack.roots / repo, "silver.orders", "no_twos", "SELECT id FROM silver.orders WHERE id > 9"
    )
    _redeploy(stack, admin, client, env, repo)
    again = _until_finished(client, admin, env, admin.runs.start(env, repo).id)
    assert again.status == "succeeded", again
    table = admin.tables.get(env, "silver.orders")
    assert (table.state, table.state_message) == ("published", None)
    assert table.snapshot_id is not None
    assert _ids(admin, env) == [1, 2]


def test_a_first_build_without_rows_that_fails_is_not_published_and_has_no_snapshot(
    stack: _Stack, sdk: tuple[TestClient, lakana.Client]
) -> None:
    """The rollback that moves no pointer: a table created without rows has nothing to tag."""
    client, admin = sdk
    db: Engine = client.app.state.engine
    env = _name()
    empty = "SELECT 1 AS missing FROM (SELECT count(*) AS n FROM silver.orders) AS c WHERE c.n = 0"
    repo = _checked_project(
        stack,
        admin,
        client,
        env,
        {"assets/silver/orders.sql": "SELECT id FROM bronze.events WHERE id > 99\n"},
        {("silver.orders", "not_empty"): empty},
    )
    run = _until_finished(client, admin, env, admin.runs.start(env, repo).id)
    task = _task(run, "silver.orders")
    assert (run.status, task.status, task.rows_written) == ("failed", "failed", 0)
    assert task.error_message == _failure(
        db, env, run.id, "silver.orders", Rollback(RollbackOutcome.UNPUBLISHED)
    )
    (result,) = admin.checks.for_run(env, run.id)
    assert (result.outcome, result.failing_rows, result.rollback_outcome) == (
        "failed",
        1,
        "unpublished",
    )
    assert result.checked_snapshot_id is None
    table = admin.tables.get(env, "silver.orders")
    assert (table.state, table.snapshot_id) == ("not_published", None)
    assert _refs(db, env, "silver.orders") == set()
    with pytest.raises(QueryFailed) as refused:
        admin.sql("SELECT * FROM silver.orders", env)
    assert (str(refused.value), refused.value.cause) == (table.state_message, "not_found")

    admin.tables.append(env, "bronze.events", pa.table({"id": pa.array([100], pa.int64())}))
    again = _until_finished(client, admin, env, admin.runs.start(env, repo).id)
    assert again.status == "succeeded", again
    assert admin.tables.get(env, "silver.orders").state == "published"
    assert _ids(admin, env) == [100]


def test_a_table_that_is_not_published_takes_grants_policies_and_a_delete(
    stack: _Stack, sdk: tuple[TestClient, lakana.Client]
) -> None:
    """Spec section 7: its identity does not change, so governance can be set meanwhile."""
    client, admin = sdk
    env, _, _, why = _unpublished(stack, admin, client)
    reader = _name()
    admin.principals.create(reader, "USER")
    where = {"environment": env, "namespace": "silver", "table": "orders"}
    grant = admin.grants.grant(principal=reader, role="VIEWER", kind="TABLE", **where)
    row_filter = admin.row_filters.create(principal=reader, expression="id > 1", **where)
    mask = admin.column_masks.create(principal=reader, column="id", expression="0", **where)
    assert [g.id for g in admin.grants.list(principal_name=reader)] == [grant.id]
    assert [f.id for f in admin.row_filters.list(principal_name=reader)] == [row_filter.id]
    assert [m.id for m in admin.column_masks.list(principal_name=reader)] == [mask.id]
    assert admin.tables.get(env, "silver.orders").state_message == why  # still not published
    admin.tables.delete(env, "silver.orders")
    with pytest.raises(NotFound):
        admin.tables.get(env, "silver.orders")
    assert admin.tables.list(env, "silver") == []


def test_a_replace_without_rows_publishes_a_table_that_was_not_published(
    stack: _Stack, sdk: tuple[TestClient, lakana.Client]
) -> None:
    """A REPLACE of zero rows stages a snapshot, so it is a commit that publishes."""
    client, admin = sdk
    env, _, _, _ = _unpublished(stack, admin, client)
    written = admin.tables.replace(env, "silver.orders", pa.table({"id": pa.array([], pa.int64())}))
    assert (written.rows, written.snapshot_id is not None) == (0, True)
    table = admin.tables.get(env, "silver.orders")
    assert (table.state, table.state_message, table.snapshot_id) == (
        "published",
        None,
        written.snapshot_id,
    )
    assert _ids(admin, env) == []


class _CountHold(HoldPoint):
    """Holds a worker in its check phase, before the first check is counted.

    Parameters
    ----------
    monkeypatch
        Installs the wrapper around `lakana_worker.checks.count_check`.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        super().__init__(_WAIT)
        real = worker_checks.count_check
        held_by = self

        def held(*args: object, **kwargs: object) -> worker_checks.Counted:
            """Wait at the first count, then count.

            Parameters
            ----------
            *args
                Passed through.
            **kwargs
                Passed through.

            Returns
            -------
            Counted
                What the real call returns.
            """
            if not held_by.reached.is_set():
                held_by.wait_here()
            return real(*args, **kwargs)  # ty: ignore[invalid-argument-type]

        monkeypatch.setattr(worker_checks, "count_check", held)


def _finish_in_a_thread(
    client: TestClient, admin: lakana.Client, env: str, run_id: str
) -> tuple[threading.Thread, list[SdkRun | BaseException]]:
    """Tick and work until a run finishes, in a thread, so that the test can act meanwhile.

    Parameters
    ----------
    client
        The test client.
    admin
        The SDK client.
    env
        Environment name.
    run_id
        The run.

    Returns
    -------
    tuple
        The started thread and a list that receives the finished run or what was raised.
    """
    outcome: list[SdkRun | BaseException] = []

    def work() -> None:
        """Run the run to its end, recording the result."""
        try:
            outcome.append(_until_finished(client, admin, env, run_id))
        except BaseException as error:
            outcome.append(error)

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    return thread, outcome


def _finished(thread: threading.Thread, outcome: list[SdkRun | BaseException]) -> SdkRun:
    """Join the thread of `_finish_in_a_thread` and return its run.

    Parameters
    ----------
    thread
        The thread.
    outcome
        Its result list.

    Returns
    -------
    SdkRun
        The finished run.

    Raises
    ------
    BaseException
        Whatever the thread raised.
    """
    thread.join(timeout=120)
    assert not thread.is_alive(), "the run did not finish"
    (result,) = outcome
    if isinstance(result, BaseException):
        raise result
    return result


def test_a_second_writer_between_write_and_rollback_leaves_the_table_alone(
    stack: _Stack, sdk: tuple[TestClient, lakana.Client], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review focus 1: a rollback never discards another writer's commit."""
    client, admin = sdk
    db: Engine = client.app.state.engine
    env = _name()
    repo = _project(
        stack, admin, client, env, {"assets/silver/orders.sql": "SELECT id FROM bronze.events\n"}
    )
    assert _until_finished(client, admin, env, admin.runs.start(env, repo).id).status == "succeeded"
    path = stack.roots / repo
    (path / "assets/silver/orders.sql").write_text(_ONES)
    _write_check(path, "silver.orders", "unique_id", _UNIQUE)
    _redeploy(stack, admin, client, env, repo)

    hold = _CountHold(monkeypatch)
    run = admin.runs.start(env, repo)
    thread, outcome = _finish_in_a_thread(client, admin, env, run.id)
    try:
        assert hold.reached.wait(_WAIT), "the task never reached its checks"
        assert _ids(admin, env) == [1, 1]  # the run's write is in the table, unchecked
        admin.tables.append(env, "silver.orders", pa.table({"id": pa.array([99], pa.int64())}))
    finally:
        hold.release.set()
    finished = _finished(thread, outcome)

    task = _task(finished, "silver.orders")
    assert (finished.status, task.status, task.attempt) == ("failed", "failed", 1)
    changed = ROLLBACK_SENTENCES[RollbackOutcome.CHANGED_SINCE].format(
        unchecked="that failed its checks"
    )
    assert task.error_message is not None and changed in task.error_message
    assert task.error_message == _failure(
        db, env, run.id, "silver.orders", Rollback(RollbackOutcome.CHANGED_SINCE)
    )
    assert _ids(admin, env) == [1, 1, 99]  # the run's rows and the other writer's row
    assert _refs(db, env, "silver.orders") == {"main"}  # no tag: nothing was set back
    (result,) = admin.checks.for_run(env, run.id)
    assert (result.outcome, result.rollback_outcome) == ("failed", "changed_since")
    rolled = _events(db, env, AuditEventType.TABLE_ROLLED_BACK)
    assert [e["outcome"] for e in rolled] == ["changed_since"]


def test_a_source_check_stops_the_assets_that_read_the_table(
    stack: _Stack, sdk: tuple[TestClient, lakana.Client]
) -> None:
    client, admin = sdk
    db: Engine = client.app.state.engine
    env = _name()
    check = (
        "SELECT id FROM bronze.events WHERE id = 1 "
        "AND NOT EXISTS (SELECT 1 FROM bronze.events WHERE id = 100)"
    )
    repo = _checked_project(
        stack,
        admin,
        client,
        env,
        {"assets/silver/clean.sql": "SELECT id FROM bronze.events\n"},
        {("bronze.events", "has_a_hundred"): check},
    )
    run = _until_finished(client, admin, env, admin.runs.start(env, repo).id)
    source = _task(run, "bronze.events")
    assert (run.status, source.kind, source.status, source.attempt) == (
        "failed",
        "source_check",
        "failed",
        1,
    )
    assert source.error_message is not None and source.error_message.startswith(
        "Check `bronze.events/has_a_hundred` failed: 1 row "
        "(checks/bronze/events/has_a_hundred.sql). This project does not build `bronze.events`"
    )
    assert source.error_message == _failure(db, env, run.id, "bronze.events", None)
    assert _task(run, "silver.clean").status == "upstream_failed"
    assert _ids(admin, env, "bronze.events") == [1, 2]
    assert _refs(db, env, "bronze.events") == {"main"}
    assert _events(db, env, AuditEventType.TABLE_ROLLED_BACK) == []
    (result,) = admin.checks.for_run(env, run.id)
    assert (result.outcome, result.failing_rows, result.rollback_outcome) == ("failed", 1, None)

    admin.tables.append(env, "bronze.events", pa.table({"id": pa.array([100], pa.int64())}))
    resumed = admin.runs.resume(env, run.id)
    assert sorted((t.asset, t.kind) for t in resumed.tasks) == [
        ("bronze.events", "source_check"),
        ("silver.clean", "asset"),
    ]
    finished = _until_finished(client, admin, env, resumed.id)
    assert finished.status == "succeeded", finished
    assert [r.outcome for r in admin.checks.for_run(env, resumed.id)] == ["passed"]
    assert _ids(admin, env, "silver.clean") == [1, 2, 100]


def _service_account(admin: lakana.Client, env: str, repo: str) -> tuple[str, str]:
    """Bind a new service account with `DATA_ENGINEER` on the environment to a deployment.

    Parameters
    ----------
    admin
        The SDK client.
    env
        Environment name.
    repo
        Repository name.

    Returns
    -------
    tuple of str
        The account's name and the id of its grant.
    """
    account = "etl-" + env
    admin.principals.create(account, "SERVICE_ACCOUNT")
    grant = admin.grants.grant(
        principal=account, role="DATA_ENGINEER", kind="ENVIRONMENT", environment=env
    )
    admin.deployments.set_run_as(env, repo, account)
    return account, grant.id


def _replaced_project(
    stack: _Stack, admin: lakana.Client, client: TestClient, check: str
) -> tuple[str, str]:
    """Deploy a project that replaces an existing `silver.orders` (ids 7, 8) and checks it.

    Parameters
    ----------
    stack
        The services.
    admin
        The SDK client.
    client
        The test client.
    check
        The SQL of the check `silver.orders/wrong`.

    Returns
    -------
    tuple of str
        The environment and the repository. The asset writes ids 1 and 2, and
        `gold.report` reads it.
    """
    env = _name()
    repo = _checked_project(
        stack,
        admin,
        client,
        env,
        {
            "assets/silver/orders.sql": "SELECT id FROM bronze.events\n",
            "assets/gold/report.sql": _REPORT,
        },
        {("silver.orders", "wrong"): check},
    )
    admin.tables.create(env, "silver.orders", schema=_IDS)
    admin.tables.append(env, "silver.orders", pa.table({"id": pa.array([7, 8], pa.int64())}))
    return env, repo


def test_a_check_counts_under_the_run_principals_row_filter(
    stack: _Stack, sdk: tuple[TestClient, lakana.Client]
) -> None:
    """Real grants: the count is the run principal's query, so its row filter decides it."""
    client, admin = sdk
    env, repo = _replaced_project(stack, admin, client, "SELECT id FROM silver.orders WHERE id = 2")
    account, _ = _service_account(admin, env, repo)
    admin.row_filters.create(
        principal=account, environment=env, namespace="silver", table="orders", expression="id <> 2"
    )
    admin.environments.set_protection(env, True)
    run = _until_finished(client, admin, env, admin.runs.start(env, repo).id)
    assert (run.status, run.run_as) == ("succeeded", account), run
    (result,) = admin.checks.for_run(env, run.id)
    assert (result.outcome, result.failing_rows, result.policy_applied) == ("passed", 0, True)
    assert result.policy_note == policy_note(account)
    assert account in policy_note(account)
    # Counted as anyone who sees the whole table, the same check finds its row.
    assert _ids(admin, env) == [1, 2]


def test_a_row_filter_on_another_table_a_check_reads_decides_the_count_too(
    stack: _Stack, sdk: tuple[TestClient, lakana.Client]
) -> None:
    """The filter is on a table only the check reads, and it makes the check fail.

    The administrator would count no row; the run's principal does not see id 2 in
    `bronze.known`, so for it the order with id 2 is unknown. The result says that a policy
    applied, and the task rolls its own table back in the protected environment.
    """
    client, admin = sdk
    db: Engine = client.app.state.engine
    env = _name()
    unknown = "SELECT o.id FROM silver.orders AS o WHERE o.id NOT IN (SELECT id FROM bronze.known)"
    admin.environments.create(env, storage={**stack.storage, "root": f"s3://lakana/{env}"})
    for table, ids in (("bronze.events", [1, 2]), ("bronze.known", [1, 2]), ("silver.orders", [7])):
        admin.tables.create(env, table, schema=_IDS)
        admin.tables.append(env, table, pa.table({"id": pa.array(ids, pa.int64())}))
    repo = _name()
    path = stack.roots / repo
    _write_check(path, "silver.orders", "unknown", unknown)
    _commit(
        path,
        {
            "lakana.yaml": f"repo: {repo}\ndefaults:\n  retry_delay: 0\n",
            "assets/silver/orders.sql": "SELECT id FROM bronze.events\n",
        },
    )
    admin.repositories.create(repo, "local", str(path))
    _redeploy(stack, admin, client, env, repo)
    account, _ = _service_account(admin, env, repo)
    admin.row_filters.create(
        principal=account, environment=env, namespace="bronze", table="known", expression="id <> 2"
    )
    admin.environments.set_protection(env, True)
    run = _until_finished(client, admin, env, admin.runs.start(env, repo).id)
    assert (run.status, run.run_as) == ("failed", account), run
    (result,) = admin.checks.for_run(env, run.id)
    assert (result.outcome, result.failing_rows, result.rollback_outcome) == (
        "failed",
        1,
        "rolled_back",
    )
    assert (result.policy_applied, result.policy_note) == (True, policy_note(account))
    assert _ids(admin, env) == [7]
    assert admin.sql(unknown.replace("silver.orders", "bronze.events"), env).num_rows == 0
    assert _events(db, env, AuditEventType.PROTECTED_WRITE_DENIED) == []


def test_a_run_principal_that_lost_its_grants_after_the_write_cannot_check_or_roll_back(
    stack: _Stack, sdk: tuple[TestClient, lakana.Client], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real grants: the checks and the rollback are the run principal's, with no side door."""
    client, admin = sdk
    db: Engine = client.app.state.engine
    env, repo = _replaced_project(
        stack, admin, client, "SELECT id FROM silver.orders WHERE id > 99"
    )
    account, grant_id = _service_account(admin, env, repo)
    hold = _CountHold(monkeypatch)
    run = admin.runs.start(env, repo)
    thread, outcome = _finish_in_a_thread(client, admin, env, run.id)
    try:
        assert hold.reached.wait(_WAIT), "the task never reached its checks"
        admin.grants.revoke(grant_id)
    finally:
        hold.release.set()
    finished = _finished(thread, outcome)

    task = _task(finished, "silver.orders")
    assert (finished.status, task.status, task.attempt) == ("failed", "failed", 1)
    (result,) = admin.checks.for_run(env, run.id)
    assert (result.outcome, result.failing_rows, result.rollback_outcome) == (
        "errored",
        None,
        "failed",
    )
    assert result.error == ERROR_UNREADABLE.format(principal=account)
    message = task.error_message
    assert message is not None
    assert message.startswith(
        "Check `silver.orders/wrong` could not run (checks/silver/orders/wrong.sql): "
        f"{result.error} The rollback was refused (permission denied"
    )
    assert "it did not change the table" in message
    assert "set back" not in message and "not published" not in message
    # And that is true: the table holds what the run wrote, and nothing downstream was built.
    assert _ids(admin, env) == [1, 2]
    assert _refs(db, env, "silver.orders") == {"main"}
    assert _events(db, env, AuditEventType.TABLE_ROLLED_BACK) == []
    assert _task(finished, "gold.report").status == "upstream_failed"


def test_a_run_principal_that_lost_its_grants_cannot_unpublish_a_first_build_without_rows(
    stack: _Stack, sdk: tuple[TestClient, lakana.Client], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rollback that moves no pointer has one gate only: the rollback's own authorization.

    A first build without rows leaves a table without a snapshot; its rollback is a marker,
    an audit event and the cleared stamp in a transaction of its own, and never reaches the
    catalog's commit, which would authorize again. The run's principal lost its grants while
    the worker was held before its first count: it may not mark the table not published.
    """
    client, admin = sdk
    db: Engine = client.app.state.engine
    env = _name()
    empty = "SELECT 1 AS missing FROM (SELECT count(*) AS n FROM silver.orders) AS c WHERE c.n = 0"
    repo = _checked_project(
        stack,
        admin,
        client,
        env,
        {
            "assets/silver/orders.sql": "SELECT id FROM bronze.events WHERE id > 99\n",
            "assets/gold/report.sql": _REPORT,
        },
        {("silver.orders", "not_empty"): empty},
    )
    account, grant_id = _service_account(admin, env, repo)
    hold = _CountHold(monkeypatch)
    run = admin.runs.start(env, repo)
    thread, outcome = _finish_in_a_thread(client, admin, env, run.id)
    try:
        assert hold.reached.wait(_WAIT), "the task never reached its checks"
        assert admin.tables.get(env, "silver.orders").snapshot_id is None  # built, without rows
        admin.grants.revoke(grant_id)
    finally:
        hold.release.set()
    finished = _finished(thread, outcome)

    task = _task(finished, "silver.orders")
    assert (finished.status, finished.run_as, task.status) == ("failed", account, "failed")
    (result,) = admin.checks.for_run(env, run.id)
    assert (result.outcome, result.error, result.rollback_outcome) == (
        "errored",
        ERROR_UNREADABLE.format(principal=account),
        "failed",
    )
    message = task.error_message
    assert message is not None
    assert message.startswith(
        "Check `silver.orders/not_empty` could not run (checks/silver/orders/not_empty.sql): "
        f"{result.error} The rollback was refused (permission denied"
    )
    assert "it did not change the table" in message and "not published" not in message
    # And that is true: no marker, no event, and the stamp of the write is still there.
    table = admin.tables.get(env, "silver.orders")
    assert (table.state, table.state_message, table.snapshot_id) == ("published", None, None)
    assert _events(db, env, AuditEventType.TABLE_ROLLED_BACK) == []
    with Session(db) as session:
        stamp = session.scalars(
            select(TaskRun.written_table_id).where(
                TaskRun.run_id == uuid.UUID(run.id), TaskRun.asset == "silver.orders"
            )
        ).one()
    assert stamp == uuid.UUID(table.uuid)
    assert _ids(admin, env) == []
    assert _task(finished, "gold.report").status == "upstream_failed"


def test_a_protected_environment_lets_the_task_roll_its_own_table_back(
    stack: _Stack, sdk: tuple[TestClient, lakana.Client]
) -> None:
    client, admin = sdk
    db: Engine = client.app.state.engine
    env, repo = _replaced_project(stack, admin, client, "SELECT id FROM silver.orders WHERE id = 2")
    account, _ = _service_account(admin, env, repo)
    admin.environments.set_protection(env, True)
    run = _until_finished(client, admin, env, admin.runs.start(env, repo).id)
    task = _task(run, "silver.orders")
    assert (run.status, run.run_as, task.status) == ("failed", account, "failed")
    assert task.error_message == _failure(
        db, env, run.id, "silver.orders", Rollback(RollbackOutcome.ROLLED_BACK)
    )
    assert _ids(admin, env) == [7, 8]
    (result,) = admin.checks.for_run(env, run.id)
    assert (result.outcome, result.rollback_outcome) == ("failed", "rolled_back")
    assert _events(db, env, AuditEventType.PROTECTED_WRITE_DENIED) == []
    assert [e["outcome"] for e in _events(db, env, AuditEventType.TABLE_ROLLED_BACK)] == [
        "rolled_back"
    ]
    # Outside the run the lockdown still refuses the same table.
    with pytest.raises(SdkPermissionDenied, match="is protected"):
        admin.tables.append(env, "silver.orders", pa.table({"id": pa.array([5], pa.int64())}))


def test_a_cancel_stops_a_running_count_and_a_resume_appends_nothing_twice(
    stack: _Stack, sdk: tuple[TestClient, lakana.Client]
) -> None:
    """A cancel during the checks ends the real count child; the table is set back.

    The check runs about nine seconds by itself and then passes. A count that was left to run
    would end `SUCCEEDED`; the cancelled one must not. The worker hands the task back, the next
    claim sets the table back to before the run, and the task ends cancelled. The resume then
    builds the append task again: its lower bound comes from the table, which is empty again.
    """
    client, admin = sdk
    db: Engine = client.app.state.engine
    env, repo = _name(), _name()
    admin.environments.create(env, storage={**stack.storage, "root": f"s3://lakana/{env}"})
    admin.tables.create(env, "bronze.readings", schema=_READINGS)
    admin.tables.create(env, "silver.readings", schema=_READINGS)
    admin.tables.append(
        env, "bronze.readings", pa.table({"ts": [1, 2, 3], "v": [10, 20, 30]}, schema=_READINGS)
    )
    path = stack.roots / repo
    slow = f"SELECT r.ts FROM silver.readings AS r, ({long_sql('n')}) AS q WHERE q.n IS NULL"
    _write_check(path, "silver.readings", "slow", slow)
    _commit(
        path,
        {
            "lakana.yaml": f"repo: {repo}\ndefaults:\n  retry_delay: 0\n",
            "assets/silver/readings.sql": _INCREMENT,
        },
    )
    admin.repositories.create(repo, "local", str(path))
    _redeploy(stack, admin, client, env, repo)
    run = admin.runs.start(env, repo)
    thread, outcome = _finish_in_a_thread(client, admin, env, run.id)

    def count_run() -> QueryRun | None:
        """Read the count's query run: the task's run that is not its write.

        Returns
        -------
        QueryRun or None
            The run, detached; `None` until the worker inserted it.
        """
        with Session(db) as session:
            row = session.scalars(
                select(QueryRun)
                .join(TaskRun, TaskRun.id == QueryRun.task_run_id)
                .where(TaskRun.run_id == uuid.UUID(run.id), QueryRun.write_mode.is_(None))
            ).one_or_none()
            if row is not None:
                session.expunge(row)
            return row

    deadline = time.monotonic() + _WAIT
    while (count := count_run()) is None or count.status is not QueryRunStatus.RUNNING:
        assert time.monotonic() < deadline, "the count never started"
        assert thread.is_alive(), outcome
        time.sleep(0.05)
    admin.runs.cancel(env, run.id)
    finished = _finished(thread, outcome)

    task = _task(finished, "silver.readings")
    assert (finished.status, task.status) == ("cancelled", "cancelled")
    with Session(db) as session:
        written = session.scalars(
            select(TaskAttempt.rows_written)
            .join(TaskRun, TaskRun.id == TaskAttempt.task_run_id)
            .where(TaskRun.run_id == uuid.UUID(run.id), TaskRun.asset == "silver.readings")
            .order_by(TaskAttempt.started_at)
        ).all()
    assert written[0] == 3  # the write had committed three rows before the cancel
    count = count_run()
    assert count is not None
    assert (count.status, count.error_cause) == (QueryRunStatus.FAILED, "cancelled")
    assert REGISTRY.pids() == []  # no child is left
    assert len(_events(db, env, AuditEventType.TABLE_ROLLED_BACK)) == 1
    assert admin.checks.for_run(env, run.id) == []
    rows = "SELECT count(*) AS n, count(DISTINCT ts) AS d FROM silver.readings"
    assert admin.sql(rows, env).to_pylist() == [{"n": 0, "d": 0}]  # what the run wrote is gone

    resumed = admin.runs.resume(env, run.id)
    again = _until_finished(client, admin, env, resumed.id)
    assert again.status == "succeeded", again
    assert _task(again, "silver.readings").rows_written == 3
    assert [r.outcome for r in admin.checks.for_run(env, resumed.id)] == ["passed"]
    assert admin.sql(rows, env).to_pylist() == [{"n": 3, "d": 3}]


_NEW_INCREMENT = (
    "/* lakana\nmaterialize: incremental\nincremental_key: id\n*/\nSELECT id FROM bronze.events\n"
)


def test_a_new_incremental_asset_creates_its_table_and_appends_only_the_new_rows(
    stack: _Stack, sdk: tuple[TestClient, lakana.Client]
) -> None:
    """No table made by hand. Run 1 creates it with every row; run 2 adds only new ones."""
    client, admin = sdk
    env = _name()
    repo = _project(stack, admin, client, env, {"assets/silver/inc.sql": _NEW_INCREMENT})
    with pytest.raises(NotFound):
        admin.tables.get(env, "silver.inc")

    first = _until_finished(client, admin, env, admin.runs.start(env, repo).id)
    assert first.status == "succeeded", first
    assert _task(first, "silver.inc").rows_written == 2
    assert _ids(admin, env, "silver.inc") == [1, 2]

    admin.tables.append(env, "bronze.events", pa.table({"id": pa.array([3, 4], pa.int64())}))
    second = _until_finished(client, admin, env, admin.runs.start(env, repo).id)
    assert second.status == "succeeded", second
    assert _task(second, "silver.inc").rows_written == 2
    assert _ids(admin, env, "silver.inc") == [1, 2, 3, 4]  # no row twice

    third = _until_finished(client, admin, env, admin.runs.start(env, repo).id)
    assert third.status == "succeeded", third
    assert _task(third, "silver.inc").rows_written == 0
    assert _ids(admin, env, "silver.inc") == [1, 2, 3, 4]


_HAS_A_HUNDRED = (
    "SELECT id FROM silver.orders WHERE id = 1 "
    "AND NOT EXISTS (SELECT 1 FROM silver.orders WHERE id = 100)"
)
"""Fails while `silver.orders` lacks the id 100; appending 100 to `bronze.events` repairs it."""


def _kept_project(stack: _Stack, admin: lakana.Client, client: TestClient) -> tuple[str, str]:
    """Deploy `silver.orders` with a `keep` check that fails, and `gold.report` that reads it.

    Parameters
    ----------
    stack
        The services.
    admin
        The SDK client.
    client
        The test client.

    Returns
    -------
    tuple of str
        The environment and the repository.
    """
    env = _name()
    repo = _checked_project(
        stack,
        admin,
        client,
        env,
        {
            "assets/silver/orders.sql": "SELECT id FROM bronze.events\n",
            "assets/gold/report.sql": _REPORT,
        },
        {("silver.orders", "has_a_hundred"): _HAS_A_HUNDRED},
        on_failure="keep",
    )
    return env, repo


def test_a_kept_table_stops_later_runs_until_its_checks_pass(
    stack: _Stack, sdk: tuple[TestClient, lakana.Client]
) -> None:
    """On PostgreSQL and S3: a kept table stops later runs of its project until one repairs it."""
    client, admin = sdk
    db: Engine = client.app.state.engine
    env, repo = _kept_project(stack, admin, client)

    first = _until_finished(client, admin, env, admin.runs.start(env, repo).id)
    assert first.status == "failed", first
    orders = _task(first, "silver.orders")
    assert orders.status == "failed" and orders.kept_check is False
    assert orders.error_message is not None and orders.error_message.startswith(
        "Check `silver.orders/has_a_hundred` failed: 1 row "
        "(checks/silver/orders/has_a_hundred.sql). Look at the rows: "
        f"`lakana check rows {first.id} silver.orders/has_a_hundred --env {env}`. "
        "The table keeps this run's data "
        "(`on_failure: keep`), and nothing that reads it was built. Repair the data or the "
        "asset; after a fix, deploy and run again; if the project did not change: "
        f"`lakana runs resume {first.id} --env {env}`."
    )
    assert _task(first, "gold.report").status == "upstream_failed"
    assert _ids(admin, env) == [1, 2]  # an ad-hoc reader still reads the kept data
    table = admin.tables.get(env, "silver.orders")
    assert table.state == "kept"
    assert table.state_message == (
        "Kept after a failed check (`silver.orders/has_a_hundred`, run "
        f"{first.id}): the table holds the data that failed. "
        "Tasks do not read it until its checks pass."
    )
    with pytest.raises(NotFound):
        admin.tables.get(env, "gold.report")
    (result,) = admin.checks.for_run(env, first.id)
    assert (result.outcome, result.on_failure, result.rollback_outcome) == ("failed", "keep", None)
    assert _refs(db, env, "silver.orders") == {"main"}  # nothing was set back
    assert len(_events(db, env, AuditEventType.CHECK_FAILED)) == 1
    assert len(_events(db, env, AuditEventType.TABLE_KEPT)) == 1
    assert _events(db, env, AuditEventType.TABLE_ROLLED_BACK) == []

    second = _until_finished(
        client, admin, env, admin.runs.start(env, repo, select=["gold.report"]).id
    )
    assert second.status == "failed", second
    check_task = _task(second, "silver.orders")
    assert (check_task.kind, check_task.kept_check, check_task.status) == (
        "source_check",
        True,
        "failed",
    )
    assert check_task.error_message is not None and check_task.error_message.endswith(
        f"`silver.orders` was kept after a failed check in run {first.id} and still fails, so "
        "the assets that read it were not built. Build it again "
        f"(`lakana run --select silver.orders --env {env}`), or repair the data and run again."
    )
    assert _task(second, "gold.report").status == "upstream_failed"
    assert admin.tables.get(env, "silver.orders").state == "kept"
    assert len(_events(db, env, AuditEventType.TABLE_KEPT)) == 1  # the note did not move
    assert _events(db, env, AuditEventType.TABLE_KEPT_CLEARED) == []

    admin.tables.append(env, "bronze.events", pa.table({"id": pa.array([100], pa.int64())}))
    third = _until_finished(client, admin, env, admin.runs.start(env, repo).id)
    assert third.status == "succeeded", third
    assert _task(third, "gold.report").status == "succeeded"
    assert admin.tables.get(env, "silver.orders").state == "published"
    assert _ids(admin, env) == [1, 2, 100]
    assert [r.outcome for r in admin.checks.for_run(env, third.id)] == ["passed"]
    assert len(_events(db, env, AuditEventType.TABLE_KEPT)) == 1
    (cleared,) = _events(db, env, AuditEventType.TABLE_KEPT_CLEARED)
    assert cleared["why"] == "checks_passed" and cleared["run_id"] == third.id


def test_a_task_of_another_project_is_refused_a_kept_table(
    stack: _Stack, sdk: tuple[TestClient, lakana.Client]
) -> None:
    """A real worker refuses another project's task the kept table; that project cannot clear it."""
    client, admin = sdk
    env, repo = _kept_project(stack, admin, client)
    first = _until_finished(client, admin, env, admin.runs.start(env, repo).id)
    assert first.status == "failed", first

    other = _name()
    path = stack.roots / other
    _commit(
        path,
        {
            "lakana.yaml": f"repo: {other}\ndefaults:\n  retry_delay: 0\n",
            "assets/gold/other.sql": "SELECT count(*) AS n FROM silver.orders\n",
        },
    )
    admin.repositories.create(other, "local", str(path))
    deploy = admin.deploys.create(env, other)
    _deploy_and_drain(client)
    admin.deploys.wait(env, deploy.id, timeout=30)

    run = _until_finished(client, admin, env, admin.runs.start(env, other).id)
    task = _task(run, "gold.other")
    assert (run.status, task.status, task.attempt) == ("failed", "failed", 1)
    assert (
        task.error_message is not None
        and (
            "`silver.orders` was kept after a failed check (`silver.orders/has_a_hundred`, run "
            f"{first.id}) and has not passed its checks since, so this task does not read it. A "
            "run of the project that builds `silver.orders` clears this: build it again there "
            f"(`lakana run --select silver.orders --env {env}`), or repair the data and run that "
            "project. If no project builds it any more, drop it: "
            f"`lakana table delete silver.orders --env {env}`."
        )
        in task.error_message
    )
    assert admin.tables.get(env, "silver.orders").state == "kept"  # it cannot clear the note
    assert _ids(admin, env) == [1, 2]


def test_the_project_default_reaches_the_run(
    stack: _Stack, sdk: tuple[TestClient, lakana.Client]
) -> None:
    """A default from `lakana.yaml` is stored at deploy and decides what a failing check does."""
    client, admin = sdk
    env = _name()
    repo = _checked_project(
        stack,
        admin,
        client,
        env,
        {"assets/silver/orders.sql": "SELECT id FROM bronze.events\n"},
        {("silver.orders", "no_twos"): "SELECT id FROM silver.orders WHERE id = 2"},
        config="checks:\n  on_failure: warn\n",
    )
    run = _until_finished(client, admin, env, admin.runs.start(env, repo).id)
    assert run.status == "succeeded", run
    (result,) = admin.checks.for_run(env, run.id)
    assert (result.outcome, result.on_failure, result.failing_rows) == ("warned", "warn", 1)
    assert admin.tables.get(env, "silver.orders").state == "published"
    assert _events(client.app.state.engine, env, AuditEventType.CHECK_FAILED) == []
    assert _ids(admin, env) == [1, 2]


def _rows_of_check(reader: lakana.Client, env: str, run_id: str, check: str) -> list[dict]:
    """Ask for the rows a check found in a run, wait for the query and fetch them.

    Parameters
    ----------
    reader
        The SDK client of the person who asks.
    env
        Environment name.
    run_id
        The run.
    check
        The check as `lakana check list` shows it.

    Returns
    -------
    list of dict
        The rows, in the order the check's SQL returns them.
    """
    handle = reader.checks.rows(run_id, check, environment=env)
    handle.wait(timeout=60)
    return handle.result().to_pylist()


def _rolled_back_by_a_duplicate(
    stack: _Stack, admin: lakana.Client, client: TestClient
) -> tuple[str, str, SdkRun]:
    """Build `silver.orders` once, then replace it with a duplicate key that its check rejects.

    Parameters
    ----------
    stack
        The services.
    admin
        The SDK client.
    client
        The test client.

    Returns
    -------
    tuple
        The environment, the repository and the second run, which failed and set the table
        back to ids 1 and 2.
    """
    env = _name()
    repo = _project(
        stack, admin, client, env, {"assets/silver/orders.sql": "SELECT id FROM bronze.events\n"}
    )
    first = _until_finished(client, admin, env, admin.runs.start(env, repo).id)
    assert first.status == "succeeded", first
    path = stack.roots / repo
    (path / "assets/silver/orders.sql").write_text(_ONES)
    _write_check(path, "silver.orders", "unique_id", _UNIQUE)
    _redeploy(stack, admin, client, env, repo)
    second = _until_finished(client, admin, env, admin.runs.start(env, repo).id)
    assert second.status == "failed", second
    assert _ids(admin, env) == [1, 2]
    return env, repo, second


def test_the_rows_of_a_rolled_back_check_are_shown_as_they_were(
    stack: _Stack, sdk: tuple[TestClient, lakana.Client]
) -> None:
    """The table was set back, so the check's SQL on today's table finds nothing; the rows do.

    Passed at once: the behaviour was built before this test. The two reads differ, which is
    what proves that the rows come from the snapshot the check read.
    """
    client, admin = sdk
    env, _, run = _rolled_back_by_a_duplicate(stack, admin, client)

    assert _rows_of_check(admin, env, run.id, "silver.orders/unique_id") == [{"id": 1}]
    assert admin.sql(_UNIQUE, env).to_pylist() == []


def test_the_rows_of_a_first_build_that_is_not_published_are_shown(
    stack: _Stack, sdk: tuple[TestClient, lakana.Client]
) -> None:
    """A table with no published data still shows what its first build was checked on.

    Passed at once: the behaviour was built before this test. An ordinary read of the table is
    refused, and the same check shows its rows.
    """
    client, admin = sdk
    env, _, run, _ = _unpublished(stack, admin, client)

    assert _rows_of_check(admin, env, run.id, "silver.orders/no_twos") == [{"id": 2}]
    with pytest.raises(QueryFailed):
        admin.sql("SELECT * FROM silver.orders", env)


def test_the_rows_of_a_kept_check_are_shown_as_they_were_after_the_table_is_repaired(
    stack: _Stack, sdk: tuple[TestClient, lakana.Client]
) -> None:
    """A `keep` failure shows its rows after a later run repaired the table.

    Passed at once: the behaviour was built before this test. The table now holds the id 100
    that the check lacked, so the check's SQL on today's table finds nothing; the failed run's
    rows are still the row it found then.
    """
    client, admin = sdk
    env, repo = _kept_project(stack, admin, client)
    failed = _until_finished(client, admin, env, admin.runs.start(env, repo).id)
    assert failed.status == "failed", failed
    check = "silver.orders/has_a_hundred"
    assert _rows_of_check(admin, env, failed.id, check) == [{"id": 1}]

    admin.tables.append(env, "bronze.events", pa.table({"id": pa.array([100], pa.int64())}))
    repaired = _until_finished(client, admin, env, admin.runs.start(env, repo).id)
    assert repaired.status == "succeeded", repaired

    assert admin.sql(_HAS_A_HUNDRED, env).to_pylist() == []
    assert _rows_of_check(admin, env, failed.id, check) == [{"id": 1}]
    assert _rows_of_check(admin, env, repaired.id, check) == []


def test_the_rows_are_gone_after_maintenance_removed_the_snapshot(
    stack: _Stack, sdk: tuple[TestClient, lakana.Client]
) -> None:
    """Eight days on, maintenance removes the tag and the snapshot; the count remains.

    Passed at once: the behaviour was built before this test. Nothing can move the clock of
    the server's own maintenance, and an object store's times cannot be aged from outside, so
    the test runs the catalog's `maintain` with a later `now`, as the materialization
    integration test does, instead of sleeping or patching a module.
    """
    client, admin = sdk
    db: Engine = client.app.state.engine
    env, _, run = _rolled_back_by_a_duplicate(stack, admin, client)
    tag = failed_snapshot_tag(uuid.UUID(run.id))
    assert _refs(db, env, "silver.orders") == {"main", tag}
    assert _rows_of_check(admin, env, run.id, "silver.orders/unique_id") == [{"id": 1}]

    with Session(db) as session:
        report = CatalogService(
            session, StorageResolver(session, None), AllowAllAuthorization()
        ).maintain(
            _admin_ref(db),
            TableRef.from_dotted(env, "silver.orders"),
            now=datetime.now(UTC) + timedelta(days=8),
        )
    assert report.tags_removed == (tag,)
    assert _refs(db, env, "silver.orders") == {"main"}

    handle = admin.checks.rows(run.id, "silver.orders/unique_id", environment=env)
    with pytest.raises(QueryFailed) as gone:
        handle.wait(timeout=60)
    assert str(gone.value) == checked_snapshot_gone_message("silver.orders", uuid.UUID(run.id), env)
    (result,) = admin.checks.for_run(env, run.id)
    assert (result.name, result.outcome, result.failing_rows) == (
        "silver.orders/unique_id",
        "failed",
        1,
    )


def test_a_caller_with_a_row_filter_sees_only_their_rows(
    stack: _Stack, sdk: tuple[TestClient, lakana.Client]
) -> None:
    """The rows are the caller's own query: an analyst's row filter applies to them.

    Passed at once: the behaviour was built before this test. The check found the ids 1 and 2
    in the table it then set back to 7 and 8; a person who may not see the id 2 is shown
    the id 1 only, and the person without a filter is shown both.
    """
    client, admin = sdk
    env, repo = _replaced_project(stack, admin, client, "SELECT id FROM silver.orders WHERE id > 0")
    run = _until_finished(client, admin, env, admin.runs.start(env, repo).id)
    assert run.status == "failed", run
    assert _ids(admin, env) == [7, 8]
    analyst = "analyst-" + env
    admin.principals.create(analyst, "USER")
    admin.grants.grant(principal=analyst, role="ANALYST", kind="ENVIRONMENT", environment=env)
    admin.row_filters.create(
        principal=analyst, environment=env, namespace="silver", table="orders", expression="id <> 2"
    )

    with lakana.Client(
        url="http://testserver", token=admin.tokens.issue(analyst, "pat").token, http_client=client
    ) as reader:
        assert _rows_of_check(reader, env, run.id, "silver.orders/wrong") == [{"id": 1}]
    assert _rows_of_check(admin, env, run.id, "silver.orders/wrong") == [{"id": 1}, {"id": 2}]
