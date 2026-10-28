"""Python task attempts in the worker: prepare, token, supervise, classify, serve queries.

The runner is a fake here (the real `LocalProcessRunner` is covered in
`test_runners.py`, and end to end in the integration tier): its script runs
in the supervisor thread and uses the real database, token service, query
service and catalog, so everything around the process is real.

No test sleeps to order events: scripts wait on `threading.Event`s or on the
stop signal, and every wait is bounded and fails the test when it runs out.
"""

from __future__ import annotations

import base64
import dataclasses
import gzip
import hashlib
import io
import json
import logging
import os
import re
import sys
import tarfile
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from importlib.metadata import version as package_version
from pathlib import Path
from typing import NoReturn, cast
from unittest import mock

import pyarrow as pa
import pytest
from check_run_helpers import (
    _check,
    _expire_lease_and_tick,
    _ids,
    _keep_note_on,
    _outside_table,
    _refused_text,
)
from fake_launcher import FakeLauncher
from sqlalchemy import delete as sqlalchemy_delete
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified
from task_run_helpers import World, _attempts, _claim, _engine, _entry, _log, _start, _task
from worker_test_helpers import ActionAllowList, ReadsById

import lakana._artifact_manifest as sdk_manifest
import lakana.errors as sdk_errors
import lakana.runner as harness_runner
import lakana_worker.launcher.requests as launcher_requests
import lakana_worker.python_tasks as python_tasks_module
import lakana_worker.task_checks as task_checks_module
import lakana_worker.task_lease as task_lease_module
import lakana_worker.tasks as tasks_module
from lakana_core.artifacts.service import ArtifactService
from lakana_core.auth import (
    Action,
    AllowAllAuthorization,
    BoundTask,
    PrincipalRef,
    RunContext,
    Securable,
)
from lakana_core.catalog.attempt_stamp import CLAIM_PROPERTY, TASK_RUN_PROPERTY
from lakana_core.catalog.json_schema import ColumnSpec
from lakana_core.catalog.metadata import read_metadata
from lakana_core.catalog.query_service import QueryService
from lakana_core.catalog.service import ROLLBACK_NOT_KNOWN_REASON, CatalogService, Rollback
from lakana_core.catalog.writes import ExpectedSnapshot, WriteResult
from lakana_core.config import DatabaseConfig, StorageConfig
from lakana_core.connections.kinds import ConnectionKind
from lakana_core.connections.service import ConnectionService
from lakana_core.db import create_engine
from lakana_core.errors import (
    ConflictError,
    LakanaError,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from lakana_core.identifiers import TableRef
from lakana_core.identity.task_tokens import TaskTokenService
from lakana_core.identity.tokens import parse_token
from lakana_core.models import (
    ArtifactVersion,
    AttemptOutcome,
    AuditEvent,
    AuditEventType,
    Base,
    CheckOutcome,
    Connection,
    ConnectionTest,
    ConnectionTestState,
    DeployKind,
    DeployRun,
    DeployStatus,
    Environment,
    EnvironmentDeployment,
    JobVersion,
    Namespace,
    PackageIndex,
    Principal,
    PrincipalType,
    QueryRun,
    QueryRunStatus,
    Repository,
    RepositoryKind,
    RollbackOutcome,
    Run,
    Secret,
    SecretUseKind,
    Table,
    TableType,
    TaskRun,
    TaskStatus,
    TaskToken,
    WriteMode,
)
from lakana_core.projects.issues import ProjectIssue
from lakana_core.projects.manifest import MANIFEST_VERSION, VERSION_WITHOUT_CHECKS
from lakana_core.projects.package_sources import RegisteredIndex
from lakana_core.runs.checks import (
    RUN_CANCELLED_MESSAGE,
    RUN_STOPPED_MESSAGE,
    SET_BACK_CHANGED,
    SET_BACK_DONE,
    SET_BACK_NOTHING,
    results_of_task,
    set_back_failed_message,
    set_back_result_message,
)
from lakana_core.runs.failures import FailureCode
from lakana_core.runs.scheduler import LOST_MESSAGE
from lakana_core.runs.selection import Selection
from lakana_core.runs.service import RunService
from lakana_core.runs.store import scheduler_tick
from lakana_core.runs.tasks import (
    Baseline,
    ClaimedTask,
    FailureKind,
    OwnTip,
    Written,
    fail_run,
    read_baseline,
    read_own_tip,
    record_attempt,
)
from lakana_core.secrets.crypto import SecretBinding, encrypt_secret_values
from lakana_core.secrets.forms import secret_forms
from lakana_core.secrets.keys import EnvKeyProvider
from lakana_core.secrets.service import SecretService
from lakana_core.secrets.uses import SecretUse
from lakana_core.storage.resolver import StorageResolver
from lakana_worker.attempt_sweep import AttemptSweeper
from lakana_worker.connection_tests import ConnectionTestLoop, DockerCheck
from lakana_worker.deploy_sweep import DeploySweeper
from lakana_worker.index_credentials import IndexAccess
from lakana_worker.launcher_client import LauncherClient, LogSinkFailed
from lakana_worker.poller import WorkerPool, poll_once
from lakana_worker.python_support import PythonSupport
from lakana_worker.python_tasks import KeeperStop, PythonRuntime
from lakana_worker.runners import (
    MAX_ERROR_MESSAGE_CHARS,
    MAX_TASK_OUTPUT_BYTES,
    CodeUnpackFailed,
    DockerRunner,
    LocalProcessRunner,
    PythonTask,
    RunOutcome,
    StopReason,
    StopSignal,
)
from lakana_worker.task_lease import LeaseKeeper, LeaseLost
from lakana_worker.task_outcome import TaskFailure, TaskOutcome
from lakana_worker.tasks import RunEngine, run_claimed
from lakana_worker.venvs import (
    IndexResolver,
    VenvBuildBroken,
    VenvBuildError,
    VenvBuildRefused,
    VenvBuildStopped,
    VenvCacheUnsafe,
    VenvLease,
    VenvSettings,
    acquire,
)

_WAIT = 20.0
"""Upper bound, in seconds, of every wait in this module; reaching it fails the test."""

_PYPROJECT = b'[project]\nname = "analytics"\nversion = "0.1.0"\nrequires-python = ">=3.11"\n'
_ASSET = (
    b"from lakana import asset\n\n\n"
    b"@asset(inputs={'clean': 'silver.clean'})\ndef f(clean):\n    return clean\n"
)
_NO_WRITE = "the task reported success without writing its table"


def _lock(lakana: str | None = None) -> bytes:
    """Build a `uv.lock` with `lakana` and `polars`.

    Parameters
    ----------
    lakana
        The SDK version; this worker's own when `None`.

    Returns
    -------
    bytes
        The lock file.
    """
    sdk = lakana or package_version("lakana-worker")
    return (
        'version = 1\nrequires-python = ">=3.11"\n\n'
        f'[[package]]\nname = "lakana"\nversion = "{sdk}"\n\n'
        '[[package]]\nname = "polars"\nversion = "1.44.2"\n'
    ).encode()


def _code(
    lock: bytes | None,
    *,
    with_link: bool = False,
    pyproject: bytes = _PYPROJECT,
    extra: tuple[tuple[str, bytes], ...] = (),
) -> bytes:
    """Build a code tarball as a deploy would.

    Parameters
    ----------
    lock
        The `uv.lock` to include; `None` leaves the file out.
    with_link
        Add a symlink member, which unpacking refuses.
    pyproject
        The `pyproject.toml`.
    extra
        More ``(name, content)`` files.

    Returns
    -------
    bytes
        The gzipped tar.
    """
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as archive:
        for name, content in (
            ("pyproject.toml", pyproject),
            *([("uv.lock", lock)] if lock is not None else []),
            ("assets/gold/scores.py", _ASSET),
            *extra,
        ):
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
        if with_link:
            link = tarfile.TarInfo("link")
            link.type = tarfile.SYMTYPE
            link.linkname = "uv.lock"
            archive.addfile(link)
    return gzip.compress(raw.getvalue(), mtime=0)


def _py_entry(table: str = "gold.scores", **extra: object) -> dict[str, object]:
    """Build a Python asset's manifest entry.

    Parameters
    ----------
    table
        Dotted name.
    **extra
        Overrides.

    Returns
    -------
    dict
        The entry.
    """
    entry: dict[str, object] = {
        "table": table,
        "kind": "python",
        "path": "assets/gold/scores.py",
        "inputs": {"clean": "silver.clean"},
        "sql": "",
        "write_mode": "replace",
        "first_write_mode": "create",
        "retries": 2,
        "retry_delay_seconds": 0,
        "timeout_seconds": 900,
    }
    entry.update(extra)
    return entry


Script = Callable[[PythonTask, Callable[[bytes], None], StopSignal], RunOutcome]
Acquire = Callable[
    [VenvSettings, str, Path, Callable[[bytes], None], IndexResolver | None], VenvLease
]


@dataclass
class FakeRunner:
    """A `Runner` whose behaviour is a function run in the supervisor thread.

    Parameters
    ----------
    script
        What `run` does.
    tasks
        Every task it was given.
    threads
        The thread each `run` was called from.
    isolated
        Whether it stands in for a container runner (`Runner.isolated`).
    """

    script: Script
    tasks: list[PythonTask] = field(default_factory=list)
    threads: list[threading.Thread] = field(default_factory=list)
    isolated: bool = False

    def run(
        self, task: PythonTask, on_log: Callable[[bytes], None], stop: StopSignal
    ) -> RunOutcome:
        """Record the task and run the script.

        Parameters
        ----------
        task
            The task.
        on_log
            The log sink.
        stop
            The stop signal.

        Returns
        -------
        RunOutcome
            The script's result.
        """
        self.tasks.append(task)
        self.threads.append(threading.current_thread())
        return self.script(task, on_log, stop)


@dataclass
class PyWorld:
    """`dev` with a deployed project: `silver.clean` (SQL) feeding `gold.scores` (Python).

    Parameters
    ----------
    world
        The database, admin and project version.
    root
        A per-test directory for the work directory and caches.
    leases
        Every venv lease handed out.
    runners
        Every fake runner built, newest last.
    resolvers
        The index resolver of every venv request, in order (`None` for a lock that uses no
        registered index).
    stops
        The stop of every venv request, in order.
    """

    world: World
    root: Path
    leases: list[VenvLease] = field(default_factory=list)
    runners: list[FakeRunner] = field(default_factory=list)
    resolvers: list[IndexResolver | None] = field(default_factory=list)
    stops: list[Callable[[], bool] | None] = field(default_factory=list)

    def deploy(
        self,
        lock: bytes | None = None,
        *,
        code: bytes | None = None,
        table: str = "gold.scores",
        extra: tuple[dict[str, object], ...] = (),
        entry: dict[str, object] | None = None,
        **overrides: object,
    ) -> None:
        """Store a code tarball and set the manifest.

        Parameters
        ----------
        lock
            The `uv.lock`; a compatible one when `None`.
        code
            The tarball; built from `lock` when `None`.
        table
            The Python asset's table.
        extra
            More manifest entries.
        entry
            Items merged into the task's own manifest entry (``output``,
            ``input_kinds``, ``inputs``), applied before `overrides`.
        **overrides
            More overrides of the Python entry, by keyword.
        """
        lock = lock or _lock()
        tarball = code or _code(lock)
        digest = hashlib.sha256(tarball).hexdigest()
        path = f"_artifacts/code/analytics/{digest}.tar.gz"
        with self.world.session() as session:
            StorageResolver(session).for_environment("dev").storage.write(path, tarball)
            version = session.get(JobVersion, self.world.version_id)
            assert version is not None
            version.code_uri = f"{self.world.storage_root}/{path}"
            version.code_sha256 = digest
            version.manifest_json = {
                "version": VERSION_WITHOUT_CHECKS,
                "assets": [
                    _entry("silver.clean", "SELECT 1::BIGINT AS id", kind="sql"),
                    _py_entry(table, **(entry or {}), **overrides),
                    *extra,
                ],
                "edges": [["silver.clean", table]],
                "lock_sha256": hashlib.sha256(lock).hexdigest(),
            }
            session.commit()

    def acquire(
        self,
        settings: VenvSettings,
        sha: str,
        project: Path,
        on_log: Callable[[bytes], None],
        indexes: IndexResolver | None = None,
        *,
        stop: Callable[[], bool] | None = None,
    ) -> VenvLease:
        """Stand in for the venv cache: no build, a lease that records its release.

        The lease holds one end of a pipe, so `release()` has a real descriptor to close.

        Parameters
        ----------
        settings
            Ignored.
        sha
            The lock digest.
        project
            The unpacked code directory.
        on_log
            Receives one line, as a build would log.
        indexes
            The index resolver the runtime passed; recorded, never called here.
        stop
            Says when the task is to stop; recorded.

        Returns
        -------
        VenvLease
            A lease on a directory under `root`.
        """
        self.resolvers.append(indexes)
        self.stops.append(stop)
        assert (project / "uv.lock").is_file()
        on_log(b"Resolved 2 packages\n")
        read_end, write_end = os.pipe()
        os.close(read_end)
        lease = VenvLease(
            path=self.root / "venvs" / sha, python=Path("/nonexistent"), descriptor=write_end
        )
        self.leases.append(lease)
        return lease

    def runtime(self, script: Script, **overrides: object) -> PythonRuntime:
        """Build the runtime around a scripted runner.

        Parameters
        ----------
        script
            The runner's behaviour.
        **overrides
            Fields of `PythonRuntime` to replace.

        Returns
        -------
        PythonRuntime
            The runtime.
        """
        runner = FakeRunner(script, isolated=overrides.get("environments") is not None)
        self.runners.append(runner)
        options: dict[str, object] = {
            "support": PythonSupport(server_url="http://server:8000", uv="/usr/local/bin/uv"),
            "work_dir": self.root / "work",
            "venvs": VenvSettings(uv="uv", cache=self.root / "venvs", uv_cache=self.root / "uv"),
            "runner": runner,
            "kill_grace": 0.2,
            "acquire_venv": self.acquire,
        }
        options.update(overrides)
        return PythonRuntime(**options)  # ty: ignore[invalid-argument-type]

    def engine(
        self,
        script: Script,
        *,
        drain: threading.Event | None = None,
        grace: float = 0.0,
        **overrides: object,
    ) -> RunEngine:
        """Build a run engine with a Python runtime.

        Parameters
        ----------
        script
            The runner's behaviour.
        drain
            The drain event.
        grace
            Drain grace in seconds.
        **overrides
            Fields of `PythonRuntime` to replace.

        Returns
        -------
        RunEngine
            The engine.
        """
        engine = _engine(self.world, drain=drain, grace=grace)
        engine.python = self.runtime(script, **overrides)
        return engine

    @property
    def runner(self) -> FakeRunner:
        """The newest fake runner.

        Returns
        -------
        FakeRunner
            The runner of the last engine built.
        """
        return self.runners[-1]

    def claim_python(self, engine: RunEngine, selection: Selection | None = None) -> ClaimedTask:
        """Run the SQL upstream, tick, and claim the Python task.

        Parameters
        ----------
        engine
            The engine.
        selection
            The run's selection.

        Returns
        -------
        ClaimedTask
            The claim on the Python asset.
        """
        _start(self.world, selection)
        run_claimed(engine, _claim(self.world))
        with self.world.session() as session:
            scheduler_tick(session, datetime.now(UTC), lambda: 0.5)
        claimed = _claim(self.world)
        assert claimed.asset != "silver.clean"
        return claimed

    def tokens(self) -> list[TaskToken]:
        """Return every task token row.

        Returns
        -------
        list of TaskToken
            The rows (detached).
        """
        with self.world.session() as session:
            rows = list(session.execute(select(TaskToken)).scalars().all())
            session.expunge_all()
            return rows

    def cancel(self, run_id: uuid.UUID) -> None:
        """Request cancellation of a run.

        Parameters
        ----------
        run_id
            The run.
        """
        with self.world.session() as session:
            RunService(session, AllowAllAuthorization()).cancel(self.world.admin, "dev", run_id)

    def steal(self, asset: str = "gold.scores") -> None:
        """Hand the task to another worker, as a reclaim and re-claim would.

        Parameters
        ----------
        asset
            The task's asset.
        """
        with self.world.session() as session:
            session.execute(
                update(TaskRun)
                .where(TaskRun.asset == asset)
                .values(claim_count=TaskRun.claim_count + 1, lease_owner="thief")
            )
            session.commit()


@pytest.fixture
def py(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[PyWorld]:
    """Build the world; keepers poll every 20 ms and the idle wait is 20 ms.

    Parameters
    ----------
    tmp_path
        Pytest's per-test directory.
    monkeypatch
        Speeds up the keeper and the serve loop.

    Yields
    ------
    PyWorld
        The world, with a compatible project deployed.
    """
    monkeypatch.setattr(task_lease_module, "_POLL_SECONDS", 0.02)
    monkeypatch.setattr(python_tasks_module, "_IDLE_SECONDS", 0.02)
    engine = create_engine(DatabaseConfig(url=f"sqlite:///{tmp_path}/lakana.db"))
    Base.metadata.create_all(engine)
    root = f"file://{tmp_path / 'storage'}"
    with Session(engine) as session:
        admin = PrincipalRef(id=uuid.uuid4(), name="admin", type=PrincipalType.USER)
        session.add(Principal(id=admin.id, type=admin.type, name=admin.name, is_active=True))
        session.commit()
        env = CatalogService(
            session, StorageResolver(session), AllowAllAuthorization()
        ).create_environment(
            admin, name="dev", storage=StorageConfig(scheme="file", root=root, options={})
        )
        repo = Repository(
            id=uuid.uuid4(), name="analytics", kind=RepositoryKind.HTTPS, url="https://h/a.git"
        )
        session.add(repo)
        session.flush()
        version = JobVersion(
            id=uuid.uuid4(),
            repository_id=repo.id,
            environment_id=env.id,
            commit_sha="a" * 40,
            manifest_json={},
            code_uri=f"{root}/x",
            code_sha256="0" * 64,
        )
        session.add(version)
        session.flush()
        session.add(
            EnvironmentDeployment(
                environment_id=env.id, repository_id=repo.id, current_job_version_id=version.id
            )
        )
        session.commit()
        world = World(engine, admin, root, version.id)
    built = PyWorld(world, tmp_path)
    built.deploy()
    yield built
    engine.dispose()


def _upload(world: World, task: PythonTask, rows: int = 1) -> WriteResult:
    """Replace the task's table as the harness would: with the token, pinned to the snapshot.

    Parameters
    ----------
    world
        The world.
    task
        The task, for its token, table and expected snapshot.
    rows
        How many rows to write.

    Returns
    -------
    WriteResult
        What the catalog wrote.
    """
    parsed = parse_token(task.token, "lakana_task")
    assert parsed is not None
    with world.session() as session:
        principal = TaskTokenService(session, AllowAllAuthorization()).verify(*parsed).principal
        return CatalogService(session, StorageResolver(session), AllowAllAuthorization()).replace(
            principal,
            TableRef.from_dotted("dev", task.table),
            pa.table({"id": pa.array(list(range(rows)), pa.int64())}),
            expected_snapshot_id=cast("ExpectedSnapshot", task.expected_snapshot_id),
        )


def _writer(world: World, rows: int = 1) -> Script:
    """Build a script that behaves like a well-behaved task.

    Parameters
    ----------
    world
        The world.
    rows
        How many rows it writes.

    Returns
    -------
    Script
        Logs a line, replaces the table, and reports success.
    """

    def script(task: PythonTask, on_log: Callable[[bytes], None], stop: StopSignal) -> RunOutcome:
        on_log(b"hello from the task\n")
        written = _upload(world, task, rows)
        return RunOutcome(succeeded=True, snapshot_id=written.snapshot_id, rows=written.rows)

    return script


def _forged(task: PythonTask, on_log: Callable[[bytes], None], stop: StopSignal) -> RunOutcome:
    """Report success without writing anything, as user code that forged its record would.

    Parameters
    ----------
    task
        The task.
    on_log
        The log sink.
    stop
        The stop signal.

    Returns
    -------
    RunOutcome
        A claim of three rows and a snapshot that does not exist.
    """
    return RunOutcome(succeeded=True, snapshot_id=42, rows=3)


def _until_stopped(
    task: PythonTask, on_log: Callable[[bytes], None], stop: StopSignal
) -> RunOutcome:
    """Behave like a long task: run until the supervisor asks to stop.

    Parameters
    ----------
    task
        The task.
    on_log
        The log sink.
    stop
        The stop signal.

    Returns
    -------
    RunOutcome
        Killed, as `LocalProcessRunner` reports a stopped process.
    """
    deadline = time.monotonic() + _WAIT
    while not stop.is_set() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert stop.reason is not None, "the supervisor never asked the task to stop"
    return RunOutcome(succeeded=False, error_message=f"stopped: {stop.reason.value}", killed=True)


def _fails(py: PyWorld, engine: RunEngine) -> tuple[TaskRun, str]:
    """Claim and run the Python task, expecting a recorded failure.

    Parameters
    ----------
    py
        The world.
    engine
        The engine.

    Returns
    -------
    tuple
        The task run and the attempt's error message.
    """
    claimed = py.claim_python(engine)
    run_claimed(engine, claimed)
    message = _attempts(py.world, claimed.task_run_id)[0].error_message
    assert message is not None
    return _task(py.world, claimed.asset), message


def test_one_worker_runs_a_python_task_and_serves_its_query(py: PyWorld) -> None:
    """Exit criterion: the worker that supervises the task also runs the task's input query."""
    world = py.world
    with world.session() as session:
        version = session.get(JobVersion, world.version_id)
        assert version is not None
        manifest = dict(version.manifest_json)
        manifest["assets"] = [
            *manifest["assets"],
            _entry("silver.other", "SELECT id FROM silver.clean", kind="sql"),
        ]
        manifest["edges"] = [["silver.clean", "gold.scores"], ["silver.clean", "silver.other"]]
        version.manifest_json = manifest
        session.commit()
    seen: dict[str, object] = {}

    def script(task: PythonTask, on_log: Callable[[bytes], None], stop: StopSignal) -> RunOutcome:
        on_log(b"reading inputs\n")
        seen["code"] = (task.code_dir / "assets" / "gold" / "scores.py").is_file()
        seen["private"] = task.attempt_dir.stat().st_mode & 0o777
        parsed = parse_token(task.token, "lakana_task")
        assert parsed is not None
        with world.session() as session:
            principal = TaskTokenService(session, AllowAllAuthorization()).verify(*parsed).principal
            assert principal.task_scope is not None and principal.task_scope.bound is not None
            seen["bound"] = principal.task_scope.bound
            seen["context"] = principal.run_context
            query = QueryService(session, AllowAllAuthorization()).submit_query(
                principal, "dev", "SELECT id FROM silver.clean"
            )
            query_id = query.id
        deadline = time.monotonic() + _WAIT
        status = QueryRunStatus.QUEUED
        while status is not QueryRunStatus.SUCCEEDED and time.monotonic() < deadline:
            time.sleep(0.02)
            with world.session() as session:
                row = session.get(QueryRun, query_id)
                assert row is not None
                status = row.status
                seen["other"] = session.execute(
                    select(TaskRun.status).where(TaskRun.asset == "silver.other")
                ).scalar_one()
        seen["query"] = status
        written = _upload(world, task)
        seen["snapshot"] = written.snapshot_id
        return RunOutcome(succeeded=True, snapshot_id=written.snapshot_id, rows=written.rows)

    engine = py.engine(script)
    _start(world)
    run_claimed(engine, _claim(world))
    with world.session() as session:
        scheduler_tick(session, datetime.now(UTC), lambda: 0.5)
        later = datetime(2100, 1, 1, tzinfo=UTC)
        session.execute(
            update(TaskRun).where(TaskRun.asset == "silver.other").values(ready_at=later)
        )
        session.commit()
    assert _task(world, "silver.other").status is TaskStatus.READY
    with world.session() as session:
        assert poll_once(session, AllowAllAuthorization(), engine=engine) is True
    scores = _task(world, "gold.scores")
    assert scores.status is TaskStatus.SUCCEEDED
    assert seen["query"] is QueryRunStatus.SUCCEEDED
    assert seen["other"] is TaskStatus.READY
    assert seen["code"] is True
    assert seen["private"] == 0o700
    (task,) = py.runner.tasks
    assert [thread.name for thread in py.runner.threads] == ["lakana-python-task"]
    assert py.runner.threads[0] is not threading.main_thread()
    assert task.expected_snapshot_id == "none"
    assert task.environment == "dev" and task.table == "gold.scores"
    assert task.inputs == {"clean": "silver.clean"}
    assert task.asset_path == "assets/gold/scores.py"
    assert task.server_url == "http://server:8000"
    assert task.code_dir == task.attempt_dir / "code"
    assert task.attempt_dir.parent == py.root / "work"
    assert not task.attempt_dir.exists()
    assert task.timeout_seconds == 900
    bound, context = seen["bound"], seen["context"]
    assert isinstance(bound, BoundTask) and isinstance(context, RunContext)
    assert (bound.task_run_id, bound.asset, bound.inputs) == (
        scores.id,
        "gold.scores",
        ("silver.clean",),
    )
    assert (context.task_run_id, context.claim) == (scores.id, bound.claim)
    (token,) = py.tokens()
    assert token.revoked_at is not None
    assert (token.task_run_id, token.principal_id) == (scores.id, world.admin.id)
    (lease,) = py.leases
    assert lease.descriptor is None
    attempt = _attempts(world, scores.id)[0]
    assert (attempt.outcome, attempt.rows_written) == (AttemptOutcome.SUCCEEDED, 1)
    log = _log(world, scores.run_id, "gold.scores")
    assert "succeeded" in log and "rows: 1" in log
    assert f"snapshot: {seen['snapshot']}" in log
    assert "    Resolved 2 packages" in log and "    reading inputs" in log
    assert task.token not in log and task.token not in repr(task)


def test_a_python_task_is_refused_a_kept_input(py: PyWorld) -> None:
    """A Python task reads a table input with its bound token, through the query path.

    The table was kept after the run started, so the run has no check task for it. The token's
    run context is the claim form; the query fails with the refusal as its whole message and a
    cause the harness does not retry (`not_found`), and the harness puts its own words in front.
    """
    world = py.world
    _outside_table(world, "bronze.events", [1, 2])
    py.deploy(inputs={"clean": "silver.clean", "events": "bronze.events"})
    seen: dict[str, object] = {}

    def script(task: PythonTask, on_log: Callable[[bytes], None], stop: StopSignal) -> RunOutcome:
        parsed = parse_token(task.token, "lakana_task")
        assert parsed is not None
        with world.session() as session:
            principal = TaskTokenService(session, AllowAllAuthorization()).verify(*parsed).principal
            seen["context"] = principal.run_context
            query_id = (
                QueryService(session, AllowAllAuthorization())
                .submit_query(principal, "dev", 'SELECT * FROM "bronze"."events"')
                .id
            )
        deadline = time.monotonic() + _WAIT
        ended = None
        while ended is None and time.monotonic() < deadline:
            time.sleep(0.02)
            with world.session() as session:
                row = session.get(QueryRun, query_id)
                assert row is not None
                if row.status in (QueryRunStatus.SUCCEEDED, QueryRunStatus.FAILED):
                    ended = (row.status, row.error_message, row.error_cause, row.result_location)
        seen["query"] = ended
        assert ended is not None
        return RunOutcome(
            succeeded=False,
            error_message=f"reading input 'events' (bronze.events) failed: {ended[1]}",
        )

    engine = py.engine(script)
    _start(world)
    run_claimed(engine, _claim(world))
    with world.session() as session:
        scheduler_tick(session, datetime.now(UTC), lambda: 0.5)
    kept = _keep_note_on(world, "bronze.events")

    with world.session() as session:
        assert poll_once(session, AllowAllAuthorization(), engine=engine) is True

    context = seen["context"]
    scores = _task(world, "gold.scores")
    assert isinstance(context, RunContext)
    assert (context.task_run_id, context.claim, context.query_run_id) == (
        scores.id,
        scores.claim_count,
        None,
    )
    assert seen["query"] == (
        QueryRunStatus.FAILED,
        _refused_text("bronze.events", kept),
        "not_found",
        None,
    )
    assert scores.error_message == (
        f"reading input 'events' (bronze.events) failed: {_refused_text('bronze.events', kept)}"
    )


def _ran(run: QueryRun) -> tuple[QueryRunStatus, str | None]:
    """Say how a query run ended and which worker ran it.

    Parameters
    ----------
    run
        The run.

    Returns
    -------
    tuple
        Its status and worker id.
    """
    return run.status, run.worker_id


def _task_beside_an_interactive_worker(py: PyWorld, *, ad_hoc_first: bool) -> dict[str, object]:
    """Run the Python task on a pipeline worker with an interactive worker polling beside it.

    The task's script submits its input query (and, with `ad_hoc_first`, an older ad-hoc query
    from the admin before it), polls as the interactive worker `interactive`, waits until the
    input query ran, and records what each poll did and which worker ran what.

    Parameters
    ----------
    py
        The world.
    ad_hoc_first
        Whether an ad-hoc query is queued before the input query.

    Returns
    -------
    dict[str, object]
        What the script saw.
    """
    world = py.world
    seen: dict[str, object] = {}
    interactive = WorkerPool(takes="interactive")

    def poll_as_interactive() -> bool:
        """Poll once as the interactive worker, on a session of its own.

        Returns
        -------
        bool
            What `poll_once` returned.
        """
        with world.session() as session:
            return poll_once(
                session,
                AllowAllAuthorization(),
                pool=interactive,
                worker_id="interactive",
            )

    def wait_for(run_id: uuid.UUID) -> QueryRun:
        """Wait, bounded, until a query run is no longer queued or running.

        Parameters
        ----------
        run_id
            The query run.

        Returns
        -------
        QueryRun
            The run as last read on a second session.
        """
        deadline = time.monotonic() + _WAIT
        while True:
            with world.session() as session:
                row = session.get(QueryRun, run_id)
                assert row is not None
                if row.status not in (QueryRunStatus.QUEUED, QueryRunStatus.RUNNING):
                    session.expunge(row)
                    return row
            assert time.monotonic() < deadline, "the query was never served"
            time.sleep(0.02)

    def script(task: PythonTask, on_log: Callable[[bytes], None], stop: StopSignal) -> RunOutcome:
        parsed = parse_token(task.token, "lakana_task")
        assert parsed is not None
        with world.session() as session:
            if ad_hoc_first:
                older = QueryService(session, AllowAllAuthorization())
                adhoc_id = older.submit_query(world.admin, "dev", "SELECT 1").id
            principal = TaskTokenService(session, AllowAllAuthorization()).verify(*parsed).principal
            input_id = (
                QueryService(session, AllowAllAuthorization())
                .submit_query(principal, "dev", "SELECT id FROM silver.clean")
                .id
            )
        if ad_hoc_first:
            ran = wait_for(input_id)
            seen["input"] = _ran(ran)
            with world.session() as session:
                waiting = session.get(QueryRun, adhoc_id)
                assert waiting is not None
                seen["adhoc_while_supervising"] = waiting.status
            seen["interactive_poll"] = poll_as_interactive()
            seen["adhoc_after"] = _ran(wait_for(adhoc_id))
        else:
            seen["interactive_first"] = poll_as_interactive()
            ran = wait_for(input_id)
            seen["input"] = _ran(ran)
            with world.session() as session:
                adhoc_id = (
                    QueryService(session, AllowAllAuthorization())
                    .submit_query(world.admin, "dev", "SELECT 1")
                    .id
                )
            seen["interactive_poll"] = poll_as_interactive()
            seen["adhoc_after"] = _ran(wait_for(adhoc_id))
        written = _upload(world, task)
        return RunOutcome(succeeded=True, snapshot_id=written.snapshot_id, rows=written.rows)

    engine = py.engine(script)
    _start(world)
    run_claimed(engine, _claim(world))
    with world.session() as session:
        scheduler_tick(session, datetime.now(UTC), lambda: 0.5)
    with world.session() as session:
        assert (
            poll_once(
                session,
                AllowAllAuthorization(),
                engine=engine,
                pool=WorkerPool(takes="pipeline"),
                worker_id="pipeline",
            )
            is True
        )
    assert _task(world, "gold.scores").status is TaskStatus.SUCCEEDED
    return seen


def test_a_python_task_on_the_only_pipeline_worker_gets_its_query_served_with_an_interactive_worker_beside_it(  # ruff: ignore[line-too-long]
    py: PyWorld,
) -> None:
    """The supervising worker serves the input query, whoever polls.

    That an interactive worker never selects the input query is not asserted here, where the
    interactive poll races the pipeline worker's: `test_each_worker_kind_takes_exactly_its_work`
    proves it deterministically.
    """
    seen = _task_beside_an_interactive_worker(py, ad_hoc_first=False)
    assert seen["input"] == (QueryRunStatus.SUCCEEDED, "pipeline")
    assert seen["interactive_poll"] is True
    assert seen["adhoc_after"] == (QueryRunStatus.SUCCEEDED, "interactive")


def test_a_pipeline_worker_supervising_a_task_leaves_an_older_ad_hoc_query_to_the_interactive_one(
    py: PyWorld,
) -> None:
    """It waits for the interactive worker, and the input query behind it is still reached."""
    seen = _task_beside_an_interactive_worker(py, ad_hoc_first=True)
    assert seen["input"] == (QueryRunStatus.SUCCEEDED, "pipeline")
    assert seen["adhoc_while_supervising"] is QueryRunStatus.QUEUED
    assert seen["interactive_poll"] is True
    assert seen["adhoc_after"] == (QueryRunStatus.SUCCEEDED, "interactive")


def test_a_second_run_passes_the_current_snapshot(py: PyWorld) -> None:
    engine = py.engine(_writer(py.world))
    run_claimed(engine, py.claim_python(engine))
    with py.world.session() as session:
        scheduler_tick(session, datetime.now(UTC), lambda: 0.5)
    run_claimed(engine, py.claim_python(engine))
    first, second = py.runner.tasks
    assert first.expected_snapshot_id == "none"
    assert isinstance(second.expected_snapshot_id, int)
    assert first.attempt_dir != second.attempt_dir
    assert _task(py.world, "gold.scores").status is TaskStatus.SUCCEEDED


def test_a_first_write_of_zero_rows_counts_as_written(py: PyWorld) -> None:
    """A table created empty has no snapshot, before as after; it still exists now."""
    engine = py.engine(_writer(py.world, rows=0))
    claimed = py.claim_python(engine)
    run_claimed(engine, claimed)
    assert _task(py.world, "gold.scores").status is TaskStatus.SUCCEEDED
    attempt = _attempts(py.world, claimed.task_run_id)[0]
    assert (attempt.outcome, attempt.rows_written) == (AttemptOutcome.SUCCEEDED, 0)
    assert "snapshot:" not in _log(py.world, claimed.run_id, "gold.scores")


def _write_as_admin(world: World, rows: int = 1, *, append: bool = False) -> None:
    """Write `gold.scores` as the administrator, without the task's token: another writer.

    Parameters
    ----------
    world
        The world.
    rows
        How many rows to write.
    append
        Append to the table instead of replacing (or creating) it.
    """
    data = pa.table({"id": pa.array(list(range(rows)), pa.int64())})
    ref = TableRef.from_dotted("dev", "gold.scores")
    with world.session() as session:
        catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
        if append:
            catalog.append(world.admin, ref, data)
        elif catalog.table_exists(world.admin, ref):
            catalog.replace(world.admin, ref, data)
        else:
            catalog.replace(world.admin, ref, data, expected_snapshot_id="none")


_NOT_ITS_WRITE = (
    "the task reported success, but gold.scores holds no write of this attempt: another writer "
    "changed it. If something else writes gold.scores, stop it and run again; the audit log "
    "shows who wrote (`lakana audit list --env dev`, which needs READ_AUDIT_LOG: ask an "
    "administrator if you do not hold it). If the server runs an older release than this "
    "worker, upgrade the server and the workers together. If table maintenance expired "
    "snapshots while the task ran, run again"
)


@pytest.mark.parametrize("exists", [False, True], ids=["first-run", "table-exists"])
def test_success_after_somebody_else_wrote_the_table_is_not_a_write(
    py: PyWorld, exists: bool
) -> None:
    """Issue #49: the table changed, but no snapshot names this attempt.

    Parameters
    ----------
    py
        The world.
    exists
        Whether the table exists before the task runs.
    """
    if exists:
        _write_as_admin(py.world)

    def script(task: PythonTask, on_log: Callable[[bytes], None], stop: StopSignal) -> RunOutcome:
        _write_as_admin(py.world, 3)
        return RunOutcome(succeeded=True, snapshot_id=None, rows=3)

    task, message = _fails(py, py.engine(script))
    assert (task.status, task.attempt, task.failure_code) == (TaskStatus.FAILED, 1, "conflict")
    assert message == _NOT_ITS_WRITE


def test_an_empty_table_somebody_else_created_is_not_a_write(py: PyWorld) -> None:
    """A table created without rows has no snapshot to stamp: the claim of its location tells."""

    def script(task: PythonTask, on_log: Callable[[bytes], None], stop: StopSignal) -> RunOutcome:
        _write_as_admin(py.world, 0)
        return RunOutcome(succeeded=True, snapshot_id=None, rows=0)

    task, message = _fails(py, py.engine(script))
    assert (task.status, task.failure_code) == (TaskStatus.FAILED, "conflict")
    assert message == _NOT_ITS_WRITE


def test_an_empty_table_somebody_else_put_in_place_of_the_old_one_is_not_a_write(
    py: PyWorld,
) -> None:
    """The table existed before; a new, empty table under its name is somebody else's."""
    _write_as_admin(py.world)

    def script(task: PythonTask, on_log: Callable[[bytes], None], stop: StopSignal) -> RunOutcome:
        with py.world.session() as session:
            CatalogService(session, StorageResolver(session), AllowAllAuthorization()).drop_table(
                py.world.admin, TableRef.from_dotted("dev", "gold.scores")
            )
        _write_as_admin(py.world, 0)
        return RunOutcome(succeeded=True, snapshot_id=None, rows=0)

    task, message = _fails(py, py.engine(script))
    assert (task.status, task.failure_code) == (TaskStatus.FAILED, "conflict")
    assert message == _NOT_ITS_WRITE


def test_a_cancel_wins_over_a_success_whose_write_is_somebody_elses(py: PyWorld) -> None:
    runs: list[uuid.UUID] = []

    def script(task: PythonTask, on_log: Callable[[bytes], None], stop: StopSignal) -> RunOutcome:
        _write_as_admin(py.world, 3)
        py.cancel(runs[0])
        return RunOutcome(succeeded=True, snapshot_id=None, rows=3)

    engine = py.engine(script)
    claimed = py.claim_python(engine)
    runs.append(claimed.run_id)
    run_claimed(engine, claimed)
    assert _task(py.world, "gold.scores").status is TaskStatus.CANCELLED


def test_a_write_another_writer_appended_to_afterwards_still_counts(py: PyWorld) -> None:
    """The attempt's own snapshot is under the current one: the write is proven."""

    def script(task: PythonTask, on_log: Callable[[bytes], None], stop: StopSignal) -> RunOutcome:
        written = _upload(py.world, task, 2)
        _write_as_admin(py.world, 1, append=True)
        return RunOutcome(succeeded=True, snapshot_id=written.snapshot_id, rows=written.rows)

    engine = py.engine(script)
    claimed = py.claim_python(engine)
    run_claimed(engine, claimed)
    assert _task(py.world, "gold.scores").status is TaskStatus.SUCCEEDED


def test_a_write_of_the_task_is_stamped_with_its_task_run_and_claim(py: PyWorld) -> None:
    engine = py.engine(_writer(py.world))
    claimed = py.claim_python(engine)
    run_claimed(engine, claimed)
    with py.world.session() as session:
        catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
        snapshot = catalog.load_table(
            py.world.admin, TableRef.from_dotted("dev", "gold.scores")
        ).metadata.current_snapshot()
    assert snapshot is not None and snapshot.summary is not None
    assert snapshot.summary.get(TASK_RUN_PROPERTY) == str(claimed.task_run_id)
    assert snapshot.summary.get(CLAIM_PROPERTY) == str(claimed.claim)


def test_success_without_a_write_is_a_permanent_failure(py: PyWorld) -> None:
    """The worker never trusts the record: it reads the table's snapshot itself."""
    task, message = _fails(py, py.engine(_forged))
    assert task.failure_code == "build_failed"
    assert (task.status, task.attempt) == (TaskStatus.FAILED, 1)
    assert message.startswith(_NO_WRITE)
    attempt = _attempts(py.world, task.id)[0]
    assert (attempt.outcome, attempt.rows_written) == (AttemptOutcome.FAILED, None)
    assert py.tokens()[0].revoked_at is not None


def test_a_forged_success_on_an_existing_table_is_a_permanent_failure(py: PyWorld) -> None:
    engine = py.engine(_writer(py.world))
    run_claimed(engine, py.claim_python(engine))
    with py.world.session() as session:
        scheduler_tick(session, datetime.now(UTC), lambda: 0.5)
    forging = py.engine(_forged)
    task, message = _fails(py, forging)
    assert task.failure_code == "build_failed"
    assert task.status is TaskStatus.FAILED
    assert message.startswith(_NO_WRITE)


@pytest.mark.parametrize(
    ("outcome", "status", "attempt", "recorded"),
    [
        pytest.param(
            RunOutcome(
                succeeded=False,
                error_message="ValueError: boom",
                failure_code=FailureCode.BUILD_FAILED,
            ),
            TaskStatus.PENDING,
            1,
            AttemptOutcome.FAILED,
            id="transient",
        ),
        pytest.param(
            RunOutcome(
                succeeded=False,
                error_message="bad return",
                retryable=False,
                failure_code=FailureCode.BUILD_FAILED,
            ),
            TaskStatus.FAILED,
            1,
            AttemptOutcome.FAILED,
            id="not-retryable",
        ),
        pytest.param(
            RunOutcome(
                succeeded=False,
                error_message="changed",
                conflict=True,
                failure_code=FailureCode.CONFLICT,
            ),
            TaskStatus.PENDING,
            0,
            AttemptOutcome.FAILED,
            id="conflict-flag",
        ),
        pytest.param(
            RunOutcome(
                succeeded=False,
                error_message="changed",
                error_type="Conflict",
                failure_code=FailureCode.BUILD_FAILED,
            ),
            TaskStatus.PENDING,
            1,
            AttemptOutcome.FAILED,
            id="error-type-is-display-text",
        ),
        pytest.param(
            RunOutcome(
                succeeded=False,
                error_message="timed out after 900s",
                killed=True,
                timed_out=True,
                failure_code=FailureCode.TIMEOUT,
            ),
            TaskStatus.PENDING,
            1,
            AttemptOutcome.TIMED_OUT,
            id="timeout",
        ),
        pytest.param(
            RunOutcome(
                succeeded=False,
                error_message="timed out after 900s",
                killed=True,
                timed_out=True,
                retryable=False,
                conflict=True,
                failure_code=FailureCode.TIMEOUT,
            ),
            TaskStatus.PENDING,
            1,
            AttemptOutcome.TIMED_OUT,
            id="timeout-before-the-record",
        ),
        pytest.param(
            RunOutcome(
                succeeded=False,
                error_message="could not supervise the task process: system error",
                killed=True,
                failure_code=FailureCode.INTERNAL,
            ),
            TaskStatus.PENDING,
            1,
            AttemptOutcome.FAILED,
            id="killed-alone-is-not-a-cancel",
        ),
    ],
)
def test_failures_are_classified(
    py: PyWorld, outcome: RunOutcome, status: TaskStatus, attempt: int, recorded: AttemptOutcome
) -> None:
    engine = py.engine(lambda _task, _log, _stop: outcome)
    claimed = py.claim_python(engine)
    run_claimed(engine, claimed)
    task = _task(py.world, "gold.scores")
    assert (task.status, task.attempt) == (status, attempt)
    row = _attempts(py.world, claimed.task_run_id)[0]
    assert (row.outcome, row.error_message) == (recorded, outcome.error_message)
    assert outcome.failure_code is not None
    assert (task.failure_code, row.failure_code) == (outcome.failure_code.value,) * 2
    assert py.tokens()[0].revoked_at is not None
    assert py.leases[0].descriptor is None


def test_outcome_text_is_sanitised_and_capped_again(py: PyWorld) -> None:
    text = "bad\x00 \x1b[31mvalue\u202e " + "x" * 5000
    engine = py.engine(lambda _task, _log, _stop: RunOutcome(succeeded=False, error_message=text))
    _, message = _fails(py, engine)
    assert message.startswith("bad? ?[31mvalue? xxx")
    assert len(message) == MAX_ERROR_MESSAGE_CHARS


def test_a_failure_without_a_message_still_says_something(py: PyWorld) -> None:
    engine = py.engine(lambda _task, _log, _stop: RunOutcome(succeeded=False))
    _, message = _fails(py, engine)
    assert message == "the task failed without a message"


def test_process_output_is_sanitised_and_capped_in_the_log(py: PyWorld) -> None:
    def script(task: PythonTask, on_log: Callable[[bytes], None], stop: StopSignal) -> RunOutcome:
        on_log(b"red \x1b[31malert\x07\r\nnul \x00 here\n\xff\n")
        on_log(b"y" * (2 * MAX_TASK_OUTPUT_BYTES))
        return RunOutcome(succeeded=False, error_message="boom")

    engine = py.engine(script)
    claimed = py.claim_python(engine)
    run_claimed(engine, claimed)
    log = _log(py.world, claimed.run_id, "gold.scores")
    assert "    red ?[31malert?\n    nul ? here\n    \ufffd\n" in log
    assert "\x1b" not in log and "\x00" not in log and "\r" not in log
    assert f"[log truncated: output exceeded {MAX_TASK_OUTPUT_BYTES} bytes]" in log
    assert len(log) < MAX_TASK_OUTPUT_BYTES + 4096


def test_a_cancel_request_stops_the_process_and_cancels_the_task(py: PyWorld) -> None:
    reasons: list[StopReason | None] = []

    def script(task: PythonTask, on_log: Callable[[bytes], None], stop: StopSignal) -> RunOutcome:
        py.cancel(claimed.run_id)
        outcome = _until_stopped(task, on_log, stop)
        reasons.append(stop.reason)
        return outcome

    engine = py.engine(script)
    claimed = py.claim_python(engine)
    run_claimed(engine, claimed)
    assert reasons == [StopReason.CANCELLED]
    assert _task(py.world, "gold.scores").status is TaskStatus.CANCELLED
    row = _attempts(py.world, claimed.task_run_id)[0]
    assert (row.outcome, row.error_message) == (AttemptOutcome.CANCELLED, RUN_CANCELLED_MESSAGE)
    assert py.tokens()[0].revoked_at is not None


def test_a_run_failed_by_an_integrity_check_stops_the_python_task_with_its_own_text(
    py: PyWorld,
) -> None:
    def script(task: PythonTask, on_log: Callable[[bytes], None], stop: StopSignal) -> RunOutcome:
        with py.world.session() as session:
            fail_run(session, claimed.run_id, "code integrity check failed")
        return _until_stopped(task, on_log, stop)

    engine = py.engine(script)
    claimed = py.claim_python(engine)
    run_claimed(engine, claimed)
    assert _task(py.world, "gold.scores").error_message == RUN_STOPPED_MESSAGE
    row = _attempts(py.world, claimed.task_run_id)[0]
    assert (row.outcome, row.error_message) == (AttemptOutcome.CANCELLED, RUN_STOPPED_MESSAGE)


def test_a_cancel_wins_over_a_record_that_says_not_retryable(py: PyWorld) -> None:
    """A bound token stops working on cancel; the harness's 401 is not a permanent failure.

    The script returns at once, so the keeper has usually not seen the cancel
    yet: the run's flag is re-read.
    """

    def script(task: PythonTask, on_log: Callable[[bytes], None], stop: StopSignal) -> RunOutcome:
        py.cancel(claimed.run_id)
        return RunOutcome(succeeded=False, error_message="401 Unauthorized", retryable=False)

    engine = py.engine(script)
    claimed = py.claim_python(engine)
    run_claimed(engine, claimed)
    assert _task(py.world, "gold.scores").status is TaskStatus.CANCELLED
    row = _attempts(py.world, claimed.task_run_id)[0]
    assert (row.outcome, row.error_message) == (AttemptOutcome.CANCELLED, RUN_CANCELLED_MESSAGE)


def test_a_write_that_landed_before_the_cancel_counts_as_succeeded(py: PyWorld) -> None:
    """A `succeeded` record of a process that was then stopped: the table decides."""

    def script(task: PythonTask, on_log: Callable[[bytes], None], stop: StopSignal) -> RunOutcome:
        written = _upload(py.world, task, rows=2)
        py.cancel(claimed.run_id)
        _until_stopped(task, on_log, stop)
        return RunOutcome(succeeded=True, snapshot_id=written.snapshot_id, rows=written.rows)

    engine = py.engine(script)
    claimed = py.claim_python(engine)
    run_claimed(engine, claimed)
    attempt = _attempts(py.world, claimed.task_run_id)[0]
    assert (attempt.outcome, attempt.rows_written) == (AttemptOutcome.SUCCEEDED, 2)


def test_a_forged_success_of_a_cancelled_attempt_is_a_cancellation(py: PyWorld) -> None:
    def script(task: PythonTask, on_log: Callable[[bytes], None], stop: StopSignal) -> RunOutcome:
        py.cancel(claimed.run_id)
        _until_stopped(task, on_log, stop)
        return _forged(task, on_log, stop)

    engine = py.engine(script)
    claimed = py.claim_python(engine)
    run_claimed(engine, claimed)
    assert _task(py.world, "gold.scores").status is TaskStatus.CANCELLED
    attempt = _attempts(py.world, claimed.task_run_id)[0]
    assert (attempt.outcome, attempt.rows_written) == (AttemptOutcome.CANCELLED, None)


def test_drain_stops_serving_queries_and_releases_the_task(py: PyWorld) -> None:
    """SIGTERM: no further query run is taken, the process is stopped, the task released."""
    world = py.world
    drain = threading.Event()
    reasons: list[StopReason | None] = []
    queued: list[uuid.UUID] = []

    def script(task: PythonTask, on_log: Callable[[bytes], None], stop: StopSignal) -> RunOutcome:
        drain.set()
        with world.session() as session:
            query = QueryService(session, AllowAllAuthorization()).submit_query(
                world.admin, "dev", "SELECT 1"
            )
            queued.append(query.id)
        outcome = _until_stopped(task, on_log, stop)
        reasons.append(stop.reason)
        return outcome

    engine = py.engine(script, drain=drain, grace=0.05)
    _start(world)
    run_claimed(engine, _claim(world))
    with world.session() as session:
        scheduler_tick(session, datetime.now(UTC), lambda: 0.5)
    started = time.monotonic()
    with world.session() as session:
        assert poll_once(session, AllowAllAuthorization(), engine=engine) is True
    assert time.monotonic() - started < _WAIT
    assert reasons == [StopReason.DRAIN]
    task = _task(world, "gold.scores")
    assert (task.status, task.attempt, task.lease_owner) == (TaskStatus.PENDING, 0, None)
    row = _attempts(world, task.id)[0]
    assert (row.outcome, row.error_message) == (AttemptOutcome.RELEASED, "stopped: worker draining")
    with world.session() as session:
        query_run = session.get(QueryRun, queued[0])
        assert query_run is not None and query_run.status is QueryRunStatus.QUEUED
    assert py.tokens()[0].revoked_at is not None


def test_a_drain_before_the_process_starts_releases_without_a_process(
    py: PyWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    drain = threading.Event()
    signalled = threading.Event()
    signal = LeaseKeeper._signal

    def recording(self: LeaseKeeper, reason: FailureKind) -> None:
        signal(self, reason)
        signalled.set()

    monkeypatch.setattr(LeaseKeeper, "_signal", recording)

    def slow(
        settings: VenvSettings,
        sha: str,
        project: Path,
        on_log: Callable[[bytes], None],
        indexes: IndexResolver | None = None,
        *,
        stop: Callable[[], bool] | None = None,
    ) -> VenvLease:
        drain.set()
        assert signalled.wait(_WAIT), "the keeper never released the task"
        return py.acquire(settings, sha, project, on_log)

    engine = py.engine(_writer(py.world), drain=drain, acquire_venv=slow)
    claimed = py.claim_python(engine)
    run_claimed(engine, claimed)
    assert py.runner.tasks == [] and py.tokens() == []
    task = _task(py.world, "gold.scores")
    assert (task.status, task.attempt) == (TaskStatus.PENDING, 0)
    row = _attempts(py.world, claimed.task_run_id)[0]
    assert (row.outcome, row.error_message) == (AttemptOutcome.RELEASED, "stopped: worker draining")
    assert py.leases[0].descriptor is None


def test_a_lost_lease_stops_the_process_and_records_nothing(py: PyWorld) -> None:
    reasons: list[StopReason | None] = []

    def script(task: PythonTask, on_log: Callable[[bytes], None], stop: StopSignal) -> RunOutcome:
        with py.world.session() as session:
            session.execute(
                update(TaskRun).where(TaskRun.asset == task.table).values(lease_owner="thief")
            )
            session.commit()
        outcome = _until_stopped(task, on_log, stop)
        reasons.append(stop.reason)
        return outcome

    engine = py.engine(script)
    claimed = py.claim_python(engine)
    run_claimed(engine, claimed)
    assert reasons == [StopReason.LEASE_LOST]
    task = _task(py.world, "gold.scores")
    assert (task.status, task.lease_owner) == (TaskStatus.RUNNING, "thief")
    assert _attempts(py.world, claimed.task_run_id)[0].finished_at is None
    assert py.tokens()[0].revoked_at is not None
    assert not py.runner.tasks[0].attempt_dir.exists()


@pytest.mark.parametrize(
    "outcome",
    [
        RunOutcome(succeeded=False, error_message="401 Unauthorized", retryable=False),
        RunOutcome(succeeded=True, snapshot_id=1, rows=1),
    ],
    ids=["not-retryable", "claims-success"],
)
def test_a_reclaimed_attempt_records_nothing_whatever_its_record_says(
    py: PyWorld, outcome: RunOutcome
) -> None:
    """The keeper has not seen the reclaim yet; the recording is guarded by owner and claim."""

    def script(task: PythonTask, on_log: Callable[[bytes], None], stop: StopSignal) -> RunOutcome:
        py.steal()
        return outcome

    engine = py.engine(script)
    claimed = py.claim_python(engine)
    run_claimed(engine, claimed)
    task = _task(py.world, "gold.scores")
    assert (task.status, task.lease_owner) == (TaskStatus.RUNNING, "thief")
    assert _attempts(py.world, claimed.task_run_id)[0].finished_at is None
    assert py.tokens()[0].revoked_at is not None


def test_the_runtime_itself_returns_nothing_for_a_lost_lease(py: PyWorld) -> None:
    """`run_claimed` drops such an outcome too; the runtime does not rely on that."""
    engine = py.engine(_forged)
    claimed = py.claim_python(engine)
    keeper = LeaseKeeper(
        py.world.session,
        claimed.task_run_id,
        "w1",
        claimed.claim,
        lease_seconds=60,
        renew_seconds=60,
    )

    def script(task: PythonTask, on_log: Callable[[bytes], None], stop: StopSignal) -> RunOutcome:
        keeper.lost = True
        keeper.cancel.set()
        assert stop.is_set() and stop.reason is StopReason.LEASE_LOST
        return RunOutcome(succeeded=False, error_message="401 Unauthorized", retryable=False)

    py.runner.script = script
    assert engine.python is not None
    assert engine.python.attempt(engine, claimed, keeper, "dev", None) is None
    assert py.tokens()[0].revoked_at is not None


def test_the_outcome_waits_for_the_query_run_in_hand(
    py: PyWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The order is observed from inside: the recorder and the query runner share one list.

    Both run on the main thread, so the recording cannot happen while the
    query run is in hand; the query outlives the supervisor thread.
    """
    events: list[str] = []
    in_query = threading.Event()
    process_done = threading.Event()
    record = tasks_module.record_attempt

    def recording(*args: object, **kwargs: object) -> bool:
        events.append(f"outcome recorded on {threading.current_thread().name}")
        return record(*args, **kwargs)  # ty: ignore[invalid-argument-type]

    write = _writer(py.world)

    def script(task: PythonTask, on_log: Callable[[bytes], None], stop: StopSignal) -> RunOutcome:
        assert in_query.wait(_WAIT), "the main thread never served a query"
        outcome = write(task, on_log, stop)
        events.append("process exited")
        process_done.set()
        return outcome

    def serve() -> bool:
        if in_query.is_set():
            return False
        events.append("query started")
        in_query.set()
        assert process_done.wait(_WAIT), "the task process never ended"
        supervisor = py.runner.threads[0]
        supervisor.join(_WAIT)
        assert not supervisor.is_alive(), "the supervisor thread never ended"
        events.append(f"query finished on {threading.current_thread().name}")
        return True

    engine = py.engine(script)
    claimed = py.claim_python(engine)
    monkeypatch.setattr(tasks_module, "record_attempt", recording)
    run_claimed(engine, claimed, serve_queries=serve)
    assert events == [
        "query started",
        "process exited",
        "query finished on MainThread",
        "outcome recorded on MainThread",
    ]
    assert _task(py.world, "gold.scores").status is TaskStatus.SUCCEEDED


def test_the_lease_is_renewed_until_the_query_in_hand_has_finished(
    py: PyWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    renewed_after_exit = threading.Event()
    process_done = threading.Event()
    renew = task_lease_module.renew_lease

    def recording(*args: object, **kwargs: object) -> object:
        if process_done.is_set() and not py.runner.threads[0].is_alive():
            renewed_after_exit.set()
        return renew(*args, **kwargs)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(task_lease_module, "renew_lease", recording)
    write = _writer(py.world)
    served: list[bool] = []

    def script(task: PythonTask, on_log: Callable[[bytes], None], stop: StopSignal) -> RunOutcome:
        outcome = write(task, on_log, stop)
        process_done.set()
        return outcome

    def serve() -> bool:
        if served or not process_done.wait(_WAIT):
            return False
        served.append(renewed_after_exit.wait(_WAIT))
        return True

    engine = py.engine(script)
    claimed = py.claim_python(engine)
    run_claimed(engine, claimed, serve_queries=serve)
    assert served == [True]
    assert _task(py.world, "gold.scores").status is TaskStatus.SUCCEEDED


def test_a_failing_serve_callback_does_not_abandon_the_task(
    py: PyWorld, caplog: pytest.LogCaptureFixture
) -> None:
    release = threading.Event()
    write = _writer(py.world)

    def script(task: PythonTask, on_log: Callable[[bytes], None], stop: StopSignal) -> RunOutcome:
        assert release.wait(_WAIT), "the main thread never served"
        return write(task, on_log, stop)

    def serve() -> bool:
        release.set()
        raise RuntimeError("the queue is on fire")

    engine = py.engine(script)
    claimed = py.claim_python(engine)
    run_claimed(engine, claimed, serve_queries=serve)
    assert _task(py.world, "gold.scores").status is TaskStatus.SUCCEEDED
    assert "Serving a query run during a Python task failed" in caplog.text


def test_an_instance_that_cannot_run_python_fails_permanently(py: PyWorld) -> None:
    engine = py.engine(_forged, support=PythonSupport(server_url=None, uv="/usr/local/bin/uv"))
    task, message = _fails(py, engine)
    assert task.failure_code == "no_runtime"
    assert task.status is TaskStatus.FAILED
    assert message == "this instance cannot run Python assets: LAKANA_RUN__SERVER_URL is not set"
    assert py.tokens() == [] and py.runner.tasks == []


def test_a_worker_without_a_python_runtime_fails_the_task_permanently(py: PyWorld) -> None:
    sql_only = py.engine(_forged)
    claimed = py.claim_python(sql_only)
    run_claimed(_engine(py.world), claimed)
    task = _task(py.world, "gold.scores")
    assert task.failure_code == "no_runtime"
    assert task.status is TaskStatus.FAILED
    message = _attempts(py.world, claimed.task_run_id)[0].error_message
    assert message == "this instance cannot run Python assets: this worker has no Python runtime"


def test_an_incompatible_lock_at_run_time_fails_permanently_and_says_to_redeploy(
    py: PyWorld,
) -> None:
    """The platform was upgraded after the deploy."""
    py.deploy(_lock("999.0.0"))
    task, message = _fails(py, py.engine(_forged))
    assert task.failure_code == "build_failed"
    assert (task.status, task.attempt) == (TaskStatus.FAILED, 1)
    running = package_version("lakana-worker")
    assert f"has lakana 999.0.0, but this instance runs {running}" in message
    assert message.endswith("update the lock and redeploy.")
    assert py.leases == [] and py.tokens() == [] and py.runner.tasks == []
    assert list((py.root / "work").iterdir()) == []


def test_a_lock_that_is_not_the_deployed_one_fails_permanently(py: PyWorld) -> None:
    py.deploy()
    with py.world.session() as session:
        version = session.get(JobVersion, py.world.version_id)
        assert version is not None
        version.manifest_json = {**version.manifest_json, "lock_sha256": "f" * 64}
        session.commit()
    task, message = _fails(py, py.engine(_forged))
    assert task.failure_code == "code_changed"
    assert task.status is TaskStatus.FAILED
    assert message == "uv.lock does not match the project version's lock_sha256; redeploy"


def test_deployed_code_without_a_lock_fails_permanently(py: PyWorld) -> None:
    py.deploy(code=_code(None))
    task, message = _fails(py, py.engine(_forged))
    assert task.failure_code == "code_changed"
    assert task.status is TaskStatus.FAILED
    assert message == "the deployed code has no readable pyproject.toml and uv.lock; redeploy"


def test_a_pyproject_that_is_not_utf8_fails_permanently(py: PyWorld) -> None:
    lock = _lock()
    py.deploy(lock, code=_code(lock, pyproject=b"\xff\xfe[project]\n"))
    task, message = _fails(py, py.engine(_forged))
    assert task.failure_code == "code_changed"
    assert task.status is TaskStatus.FAILED
    assert message == "the deployed code has no readable pyproject.toml and uv.lock; redeploy"


_SELF_INPUT_MESSAGE = (
    "gold.scores reads its own table as an input, but the table does not exist yet, so its first "
    "run can never succeed. Create the table once: deploy a version of the asset that does not "
    "read itself, run it, then deploy the self-reading version"
)


def test_an_asset_that_reads_its_own_missing_table_fails_permanently_and_says_what_to_do(
    py: PyWorld,
) -> None:
    py.deploy(inputs={"prev": "gold.scores"})
    engine = py.engine(_forged)
    task, message = _fails(py, engine)
    assert task.failure_code == "build_failed"
    assert (task.status, task.attempt) == (TaskStatus.FAILED, 1)
    assert message == _SELF_INPUT_MESSAGE
    assert py.runner.tasks == [] and py.tokens() == []
    assert all(lease.descriptor is None for lease in py.leases)


def test_an_asset_that_reads_its_own_missing_table_fails_before_its_environment_is_built(
    py: PyWorld,
) -> None:
    """Issue #48: a first run that can never succeed does not wait for a build first."""
    py.deploy(inputs={"prev": "gold.scores"})
    task, message = _fails(py, py.engine(_forged))
    assert message == _SELF_INPUT_MESSAGE
    assert (task.status, task.failure_code) == (TaskStatus.FAILED, "build_failed")
    assert py.leases == [] and py.resolvers == []
    assert list((py.root / "work").iterdir()) == []


def test_an_asset_that_reads_its_own_existing_table_runs(py: PyWorld) -> None:
    py.deploy()
    engine = py.engine(_writer(py.world))
    run_claimed(engine, py.claim_python(engine))
    assert _task(py.world, "gold.scores").status is TaskStatus.SUCCEEDED
    with py.world.session() as session:
        scheduler_tick(session, datetime.now(UTC), lambda: 0.5)
    py.deploy(inputs={"prev": "gold.scores"})
    run_claimed(engine, py.claim_python(engine))
    first_task, second_task = py.runner.tasks
    assert first_task.expected_snapshot_id == "none"
    assert second_task.expected_snapshot_id != "none"
    assert second_task.inputs == {"prev": "gold.scores"}
    assert _task(py.world, "gold.scores").status is TaskStatus.SUCCEEDED


def test_an_unsafe_code_tarball_fails_permanently(py: PyWorld) -> None:
    py.deploy(code=_code(_lock(), with_link=True))
    task, message = _fails(py, py.engine(_forged))
    assert task.failure_code == "code_changed"
    assert task.status is TaskStatus.FAILED
    assert "links and special files are not allowed" in message
    assert list((py.root / "work").iterdir()) == []


def test_a_machine_failure_while_unpacking_is_retried(
    py: PyWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    def full(*_args: object, **_kwargs: object) -> Path:
        raise CodeUnpackFailed("could not unpack the code: No space left on device")

    monkeypatch.setattr(python_tasks_module, "unpack_code", full)
    task, message = _fails(py, py.engine(_forged))
    assert task.failure_code == "internal"
    assert (task.status, task.attempt) == (TaskStatus.PENDING, 1)
    assert message == "could not unpack the code: No space left on device"


def test_a_failed_environment_build_is_retried_and_its_output_logged(py: PyWorld) -> None:
    def broken(
        settings: VenvSettings,
        sha: str,
        project: Path,
        on_log: Callable[[bytes], None],
        indexes: IndexResolver | None = None,
        *,
        stop: Callable[[], bool] | None = None,
    ) -> VenvLease:
        on_log(b"error: no solution found\n")
        raise VenvBuildError("environment build failed: uv exited with status 2")

    engine = py.engine(_forged, acquire_venv=broken)
    claimed = py.claim_python(engine)
    run_claimed(engine, claimed)
    task = _task(py.world, "gold.scores")
    assert task.failure_code == "build_failed"
    assert (task.status, task.attempt) == (TaskStatus.PENDING, 1)
    row = _attempts(py.world, claimed.task_run_id)[0]
    assert row.error_message == "environment build failed: uv exited with status 2"
    assert "    error: no solution found" in _log(py.world, claimed.run_id, "gold.scores")
    assert py.tokens() == []
    assert list((py.root / "work").iterdir()) == []


def test_an_unsafe_venv_cache_fails_permanently(py: PyWorld) -> None:
    def unsafe(
        settings: VenvSettings,
        sha: str,
        project: Path,
        on_log: Callable[[bytes], None],
        indexes: IndexResolver | None = None,
        *,
        stop: Callable[[], bool] | None = None,
    ) -> VenvLease:
        raise VenvCacheUnsafe("environment build failed: the venv cache directory is not private")

    task, message = _fails(py, py.engine(_forged, acquire_venv=unsafe))
    assert task.failure_code == "no_runtime"
    assert (task.status, task.attempt) == (TaskStatus.FAILED, 1)
    assert message == "environment build failed: the venv cache directory is not private"


def test_a_target_that_is_a_view_fails_permanently(py: PyWorld) -> None:
    world = py.world
    engine = py.engine(_forged)
    claimed = py.claim_python(engine)
    with world.session() as session:
        catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
        catalog.create_or_replace_view(
            world.admin,
            TableRef.from_dotted("dev", "gold.scores"),
            sql="SELECT id FROM silver.clean",
            references=catalog.resolve_lineage_sources(world.admin, "dev", ["silver.clean"]),
            columns=[ColumnSpec(name="id", type="long", nullable=True)],
        )
    run_claimed(engine, claimed)
    message = _attempts(world, claimed.task_run_id)[0].error_message
    assert message == (
        "gold.scores exists and is not a managed table; a Python asset replaces a table. "
        "Drop or rename it, or give the asset another name and redeploy."
    )
    assert _task(world, "gold.scores").status is TaskStatus.FAILED
    assert _task(world, "gold.scores").failure_code == "build_failed"
    assert py.tokens() == [] and py.runner.tasks == []


def test_a_storage_error_reading_the_target_is_retried(
    py: PyWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = py.engine(_writer(py.world))
    run_claimed(engine, py.claim_python(engine))
    with py.world.session() as session:
        scheduler_tick(session, datetime.now(UTC), lambda: 0.5)

    def explode(*_args: object, **_kwargs: object) -> object:
        raise OSError("connection reset")

    monkeypatch.setattr(python_tasks_module, "read_metadata", explode)
    task, message = _fails(py, py.engine(_forged))
    assert (task.status, task.attempt) == (TaskStatus.PENDING, 1)
    assert message == "the table gold.scores could not be read (storage error)"
    assert py.tokens()[-1].revoked_at is not None and len(py.tokens()) == 1


@pytest.mark.parametrize("path", [None, 7, "", "../outside.py", "/etc/passwd.py", "assets/x.txt"])
def test_a_malformed_python_entry_fails_permanently(py: PyWorld, path: object) -> None:
    py.deploy(path=path)
    task, message = _fails(py, py.engine(_forged))
    assert task.status is TaskStatus.FAILED
    assert message == "the project version has no Python asset file for gold.scores; redeploy"


@pytest.mark.parametrize("inputs", [["silver.clean"], {"clean": 7}, None])
def test_malformed_inputs_fail_permanently_and_are_not_a_lost_lease(
    py: PyWorld, inputs: object
) -> None:
    py.deploy(inputs=inputs)
    task, message = _fails(py, py.engine(_forged))
    assert (task.status, task.attempt) == (TaskStatus.FAILED, 1)
    assert message == (
        "the project version's inputs for gold.scores are not a mapping of parameter names "
        "to tables; redeploy"
    )
    assert py.tokens() == []


def test_a_malformed_asset_name_fails_permanently(py: PyWorld) -> None:
    py.deploy(extra=(_py_entry("nodots"),))
    engine = py.engine(_forged)
    selection = Selection(patterns=("silver.clean", "gold.scores"))
    claimed = dataclasses.replace(py.claim_python(engine, selection), asset="nodots")
    keeper = LeaseKeeper(
        py.world.session,
        claimed.task_run_id,
        "w1",
        claimed.claim,
        lease_seconds=60,
        renew_seconds=60,
    )
    assert engine.python is not None
    outcome = engine.python.attempt(engine, claimed, keeper, "dev", None)
    assert outcome is not None
    assert (outcome.kind, outcome.message) == (
        FailureKind.PERMANENT,
        "the asset name 'nodots' is not a valid table name; rename the asset and redeploy",
    )
    assert outcome.failure_code is FailureCode.CODE_CHANGED


def test_an_attempt_superseded_before_the_token_is_minted_records_nothing(py: PyWorld) -> None:
    def reclaimed(
        settings: VenvSettings,
        sha: str,
        project: Path,
        on_log: Callable[[bytes], None],
        indexes: IndexResolver | None = None,
        *,
        stop: Callable[[], bool] | None = None,
    ) -> VenvLease:
        py.steal()
        return py.acquire(settings, sha, project, on_log)

    engine = py.engine(_forged, acquire_venv=reclaimed)
    claimed = py.claim_python(engine)
    run_claimed(engine, claimed)
    assert py.runner.tasks == []
    assert _attempts(py.world, claimed.task_run_id)[0].finished_at is None
    assert py.tokens() == []
    assert py.leases[0].descriptor is None
    assert list((py.root / "work").iterdir()) == []


def test_a_lease_the_keeper_saw_lost_during_the_build_starts_no_process(
    py: PyWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    keepers: list[LeaseKeeper] = []
    start = LeaseKeeper.start

    def recording(self: LeaseKeeper) -> None:
        keepers.append(self)
        start(self)

    monkeypatch.setattr(LeaseKeeper, "start", recording)

    def reclaimed(
        settings: VenvSettings,
        sha: str,
        project: Path,
        on_log: Callable[[bytes], None],
        indexes: IndexResolver | None = None,
        *,
        stop: Callable[[], bool] | None = None,
    ) -> VenvLease:
        py.steal()
        assert keepers[-1].cancel.wait(_WAIT), "the keeper never noticed the lost lease"
        assert keepers[-1].lost
        return py.acquire(settings, sha, project, on_log)

    engine = py.engine(_forged, acquire_venv=reclaimed)
    claimed = py.claim_python(engine)
    run_claimed(engine, claimed)
    assert py.runner.tasks == [] and py.tokens() == []
    task = _task(py.world, "gold.scores")
    assert (task.status, task.lease_owner) == (TaskStatus.RUNNING, "thief")
    assert _attempts(py.world, claimed.task_run_id)[0].finished_at is None
    assert py.leases[0].descriptor is None


def test_a_cancel_during_the_build_cancels_without_a_process(py: PyWorld) -> None:
    def cancelled(
        settings: VenvSettings,
        sha: str,
        project: Path,
        on_log: Callable[[bytes], None],
        indexes: IndexResolver | None = None,
        *,
        stop: Callable[[], bool] | None = None,
    ) -> VenvLease:
        py.cancel(claimed.run_id)
        return py.acquire(settings, sha, project, on_log)

    engine = py.engine(_forged, acquire_venv=cancelled)
    claimed = py.claim_python(engine)
    run_claimed(engine, claimed)
    assert py.runner.tasks == [] and py.tokens() == []
    assert _task(py.world, "gold.scores").status is TaskStatus.CANCELLED
    row = _attempts(py.world, claimed.task_run_id)[0]
    assert (row.outcome, row.error_message) == (AttemptOutcome.CANCELLED, RUN_CANCELLED_MESSAGE)


def _building_until_stopped(
    seen: list[bool], before: Callable[[], None]
) -> Callable[..., VenvLease]:
    """Build a venv stand-in that builds until the task's stop says so, as a long build does.

    Parameters
    ----------
    seen
        Receives what the stop said when the build gave up.
    before
        Runs first: the cancel, the drain or the theft that the build is to notice.

    Returns
    -------
    callable
        The stand-in, raising `VenvBuildStopped` once stopped.
    """

    def building(
        settings: VenvSettings,
        sha: str,
        project: Path,
        on_log: Callable[[bytes], None],
        indexes: IndexResolver | None = None,
        *,
        stop: Callable[[], bool] | None = None,
    ) -> VenvLease:
        """Build until stopped.

        Parameters
        ----------
        settings
            Ignored.
        sha
            Ignored.
        project
            Ignored.
        on_log
            Ignored.
        indexes
            Ignored.
        stop
            The task's stop.

        Raises
        ------
        VenvBuildStopped
            When the stop says so.
        """
        assert stop is not None, "the build gets the task's stop"
        before()
        deadline = time.monotonic() + _WAIT
        while not stop() and time.monotonic() < deadline:
            time.sleep(0.01)
        seen.append(stop())
        raise VenvBuildStopped("environment build failed: stopped before it ended")

    return building


def test_a_cancel_during_a_long_build_stops_the_build_and_cancels_the_task(py: PyWorld) -> None:
    """#291: the build is told to stop, and the task ends as cancelled without a process."""
    seen: list[bool] = []
    engine = py.engine(
        _forged, acquire_venv=_building_until_stopped(seen, lambda: py.cancel(claimed.run_id))
    )
    claimed = py.claim_python(engine)
    started = time.monotonic()

    run_claimed(engine, claimed)

    assert seen == [True] and time.monotonic() - started < _WAIT
    assert py.runner.tasks == [] and py.tokens() == []
    assert _task(py.world, "gold.scores").status is TaskStatus.CANCELLED
    row = _attempts(py.world, claimed.task_run_id)[0]
    assert (row.outcome, row.error_message) == (AttemptOutcome.CANCELLED, RUN_CANCELLED_MESSAGE)


def test_a_drain_during_a_long_build_stops_the_build_and_releases_the_task(py: PyWorld) -> None:
    """#291: a drain past its grace stops the build; the task goes back to the queue."""
    drain = threading.Event()
    seen: list[bool] = []
    engine = py.engine(_forged, drain=drain, acquire_venv=_building_until_stopped(seen, drain.set))
    claimed = py.claim_python(engine)

    run_claimed(engine, claimed)

    assert seen == [True] and py.runner.tasks == []
    task = _task(py.world, "gold.scores")
    assert (task.status, task.attempt) == (TaskStatus.PENDING, 0)
    row = _attempts(py.world, claimed.task_run_id)[0]
    assert (row.outcome, row.error_message) == (AttemptOutcome.RELEASED, "stopped: worker draining")


def test_a_lease_lost_during_a_long_build_stops_the_build_and_records_nothing(
    py: PyWorld,
) -> None:
    """#291: the build of a task another worker took stops, and nothing is recorded."""
    seen: list[bool] = []
    engine = py.engine(_forged, acquire_venv=_building_until_stopped(seen, py.steal))
    claimed = py.claim_python(engine)

    run_claimed(engine, claimed)

    assert seen == [True] and py.runner.tasks == []
    assert _task(py.world, "gold.scores").lease_owner != engine.settings.worker_id


def test_a_build_stopped_without_a_reason_fails_and_is_retried(py: PyWorld) -> None:
    """A build that reports a stop nobody asked for is a transient build failure."""
    seen: list[bool] = []

    def stopped(
        settings: VenvSettings,
        sha: str,
        project: Path,
        on_log: Callable[[bytes], None],
        indexes: IndexResolver | None = None,
        *,
        stop: Callable[[], bool] | None = None,
    ) -> VenvLease:
        """Report a stop at once.

        Parameters
        ----------
        settings
            Ignored.
        sha
            Ignored.
        project
            Ignored.
        on_log
            Ignored.
        indexes
            Ignored.
        stop
            The task's stop, recorded.

        Raises
        ------
        VenvBuildStopped
            Always.
        """
        seen.append(stop is not None and stop())
        raise VenvBuildStopped("environment build failed: stopped before it ended")

    _, message = _fails(py, py.engine(_forged, acquire_venv=stopped))

    assert seen == [False]
    assert "neither a cancel, a drain nor a lost lease" in message


def test_a_principal_deactivated_during_the_build_fails_permanently(py: PyWorld) -> None:
    world = py.world

    def deactivate(
        settings: VenvSettings,
        sha: str,
        project: Path,
        on_log: Callable[[bytes], None],
        indexes: IndexResolver | None = None,
        *,
        stop: Callable[[], bool] | None = None,
    ) -> VenvLease:
        with world.session() as session:
            account = session.get(Principal, world.admin.id)
            assert account is not None
            account.is_active = False
            session.commit()
        return py.acquire(settings, sha, project, on_log)

    task, message = _fails(py, py.engine(_forged, acquire_venv=deactivate))
    assert task.status is TaskStatus.FAILED
    assert message == "the run principal is deactivated or deleted"
    assert py.tokens() == []


def test_a_mint_refused_for_a_malformed_manifest_fails_permanently(
    py: PyWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(self: TaskTokenService, *_args: object, **_kwargs: object) -> object:
        raise ValidationError("The manifest's inputs for 'gold.scores' are not a mapping.")

    monkeypatch.setattr(TaskTokenService, "mint_for_task", refuse)
    task, message = _fails(py, py.engine(_forged))
    assert (task.status, task.attempt) == (TaskStatus.FAILED, 1)
    assert message == (
        "the project version's inputs for gold.scores are not a mapping of parameter names "
        "to tables; redeploy"
    )


@pytest.mark.parametrize(
    "error",
    [
        ConflictError("not the running attempt"),
        IntegrityError("insert", {}, Exception("foreign key")),
    ],
    ids=["conflict", "task-run-deleted"],
)
def test_a_mint_that_finds_the_claim_gone_records_nothing(
    py: PyWorld, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    def refuse(self: TaskTokenService, *_args: object, **_kwargs: object) -> object:
        raise error

    monkeypatch.setattr(TaskTokenService, "mint_for_task", refuse)
    engine = py.engine(_forged)
    claimed = py.claim_python(engine)
    run_claimed(engine, claimed)
    assert py.runner.tasks == []
    task = _task(py.world, "gold.scores")
    assert (task.status, task.lease_owner) == (TaskStatus.RUNNING, "w1")
    assert _attempts(py.world, claimed.task_run_id)[0].finished_at is None
    assert py.leases[0].descriptor is None


def test_a_mint_refused_because_of_a_cancel_is_a_cancellation(
    py: PyWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(self: TaskTokenService, *_args: object, **_kwargs: object) -> object:
        py.cancel(claimed.run_id)
        raise ConflictError("not the running attempt")

    monkeypatch.setattr(TaskTokenService, "mint_for_task", refuse)
    engine = py.engine(_forged)
    claimed = py.claim_python(engine)
    run_claimed(engine, claimed)
    assert _task(py.world, "gold.scores").status is TaskStatus.CANCELLED


def test_a_storage_error_reading_the_code_is_retried(
    py: PyWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = py.engine(_forged)
    claimed = py.claim_python(engine)
    engine.verified.add(claimed.run_id)

    def explode(*_args: object, **_kwargs: object) -> bytes:
        raise OSError("connection reset")

    monkeypatch.setattr(python_tasks_module, "_read_code", explode)
    run_claimed(engine, claimed)
    task = _task(py.world, "gold.scores")
    assert (task.status, task.attempt) == (TaskStatus.PENDING, 1)
    row = _attempts(py.world, claimed.task_run_id)[0]
    assert row.error_message == "the code tarball could not be read (storage error)"


def test_code_that_vanished_after_the_integrity_check_fails_permanently(
    py: PyWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    def missing(*_args: object, **_kwargs: object) -> bytes:
        raise NotFoundError("No object at '_artifacts/code/analytics/x.tar.gz'.")

    monkeypatch.setattr(python_tasks_module, "_read_code", missing)
    task, message = _fails(py, py.engine(_forged))
    assert task.status is TaskStatus.FAILED
    assert message == "the code tarball is missing from storage; redeploy"


def _tampered_tarball_of_a_python_task(py: PyWorld) -> tuple[uuid.UUID, ClaimedTask, RunEngine]:
    """Claim the Python task of a run, then replace the code tarball in storage.

    The SQL task before it ran with the honest tarball. A second engine has no memory of that
    verification, so the Python task is the one that finds the mismatch.

    Parameters
    ----------
    py
        The world.

    Returns
    -------
    tuple
        The run, the claim and the engine that runs it.
    """
    claimed = py.claim_python(py.engine(_forged))
    with py.world.session() as session:
        version = session.get(JobVersion, py.world.version_id)
        assert version is not None
        path = version.code_uri.removeprefix(f"{py.world.storage_root}/")
        StorageResolver(session).for_environment("dev").storage.write(path, b"evil")
    return claimed.run_id, claimed, py.engine(_forged)


def _integrity_events(py: PyWorld) -> list[AuditEvent]:
    """Read the `CODE_INTEGRITY_FAILED` audit events.

    Parameters
    ----------
    py
        The world.

    Returns
    -------
    list of AuditEvent
        The events, detached.
    """
    with py.world.session() as session:
        return list(
            session.scalars(
                select(AuditEvent).where(
                    AuditEvent.event_type == AuditEventType.CODE_INTEGRITY_FAILED
                )
            )
        )


def test_a_tampered_tarball_of_a_python_task_fails_the_run_and_is_audited(py: PyWorld) -> None:
    """The Python path hands the run failure and the audit row to the attempt's record.

    Fails if `in_transaction` is not passed through `PythonRuntime.attempt`: the task would
    fail alone, the run would not be flagged and nothing would be audited.
    """
    run_id, claimed, engine = _tampered_tarball_of_a_python_task(py)

    run_claimed(engine, claimed)

    task = _task(py.world, "gold.scores")
    assert task.status is TaskStatus.FAILED
    assert "does not match its recorded code_sha256" in (task.error_message or "")
    with py.world.session() as session:
        run = session.get(Run, run_id)
        assert run is not None
        assert run.cancel_requested is True
        assert "code integrity check failed" in (run.error_message or "")
    (event,) = _integrity_events(py)
    assert event.detail["run_id"] == str(run_id)
    assert py.runner.tasks == []  # no process was started


def test_a_python_integrity_failure_that_breaks_before_the_commit_leaves_nothing(
    py: PyWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run failure and the audit row are rolled back with the attempt's record."""
    run_id, claimed, engine = _tampered_tarball_of_a_python_task(py)
    original = tasks_module.fail_run

    def fail_then_break(
        session: Session, run_id: uuid.UUID, message: str, *, commit: bool = True
    ) -> None:
        original(session, run_id, message, commit=commit)
        raise RuntimeError("the database went away")

    monkeypatch.setattr(tasks_module, "fail_run", fail_then_break)
    with pytest.raises(RuntimeError, match="went away"):
        run_claimed(engine, claimed)
    with py.world.session() as session:
        run = session.get(Run, run_id)
        assert run is not None and run.cancel_requested is False
    assert _integrity_events(py) == []
    assert _task(py.world, "gold.scores").status is TaskStatus.RUNNING


def test_a_failing_revoke_is_logged_and_does_not_lose_the_outcome(
    py: PyWorld, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def explode(self: TaskTokenService, key_id: str) -> None:
        raise OperationalError("revoke", {}, Exception("database is gone"))

    monkeypatch.setattr(TaskTokenService, "revoke_for_task", explode)
    engine = py.engine(_writer(py.world))
    claimed = py.claim_python(engine)
    run_claimed(engine, claimed)
    assert _task(py.world, "gold.scores").status is TaskStatus.SUCCEEDED
    assert "Could not revoke the task token" in caplog.text
    assert py.runner.tasks[0].token not in caplog.text
    assert py.leases[0].descriptor is None
    assert not py.runner.tasks[0].attempt_dir.exists()


def test_an_attempt_directory_that_cannot_be_removed_is_logged(
    py: PyWorld, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(python_tasks_module, "remove_private_tree", lambda _path: False)
    engine = py.engine(_writer(py.world))
    claimed = py.claim_python(engine)
    run_claimed(engine, claimed)
    assert _task(py.world, "gold.scores").status is TaskStatus.SUCCEEDED
    assert "was not removed completely" in caplog.text
    assert py.tokens()[0].revoked_at is not None and py.leases[0].descriptor is None


@pytest.mark.parametrize("mode", [0o777, 0o755, 0o750])
def test_a_work_directory_that_is_not_private_fails_permanently(
    py: PyWorld, caplog: pytest.LogCaptureFixture, mode: int
) -> None:
    work = py.root / "work"
    work.mkdir()
    work.chmod(mode)
    task, message = _fails(py, py.engine(_forged))
    assert (task.status, task.attempt) == (TaskStatus.FAILED, 1)
    assert message == (
        "the worker's task directory is not private; an operator must make it a real "
        "directory owned by the worker's user with mode 0700 (the worker log names it)"
    )
    assert str(work) not in message
    assert f"The task directory {work} is not private" in caplog.text
    assert py.leases == [] and py.runner.tasks == []


def test_a_work_directory_that_cannot_be_created_is_retried(py: PyWorld) -> None:
    (py.root / "blocked").write_text("a file, not a directory")
    engine = py.engine(_forged, work_dir=py.root / "blocked" / "work")
    task, message = _fails(py, engine)
    assert (task.status, task.attempt) == (TaskStatus.PENDING, 1)
    assert message.startswith("could not create the task's working directory: ")
    assert str(py.root) not in message


def test_an_unexpected_error_in_the_supervisor_thread_is_transient_and_generic(
    py: PyWorld,
) -> None:
    def script(task: PythonTask, on_log: Callable[[bytes], None], stop: StopSignal) -> RunOutcome:
        on_log(b"got this far\n")
        raise RuntimeError("secret detail that must not be stored")

    engine = py.engine(script)
    claimed = py.claim_python(engine)
    run_claimed(engine, claimed)
    row = _attempts(py.world, claimed.task_run_id)[0]
    assert row.error_message == "internal error while running the task; see the worker log"
    log = _log(py.world, claimed.run_id, "gold.scores")
    assert "    got this far" in log and "secret detail" not in log
    assert _task(py.world, "gold.scores").status is TaskStatus.PENDING
    assert py.tokens()[0].revoked_at is not None
    assert py.leases[0].descriptor is None
    assert not py.runner.tasks[0].attempt_dir.exists()


def test_keeper_stop_maps_the_keepers_state() -> None:
    @dataclass
    class Keeper:
        cancel: threading.Event = field(default_factory=threading.Event)
        reason: object = None
        lost: bool = False

    quiet = KeeperStop(Keeper())  # ty: ignore[invalid-argument-type]
    assert not quiet.is_set() and quiet.reason is None
    for lost, reason, expected in (
        (True, None, StopReason.LEASE_LOST),
        (True, FailureKind.CANCELLED, StopReason.LEASE_LOST),
        (False, FailureKind.RELEASED, StopReason.DRAIN),
        (False, FailureKind.CANCELLED, StopReason.CANCELLED),
    ):
        keeper = Keeper(reason=reason, lost=lost)
        keeper.cancel.set()
        stop = KeeperStop(keeper)  # ty: ignore[invalid-argument-type]
        assert stop.is_set() and stop.is_set()
        assert stop.reason is expected


def test_only_queries_claims_neither_deploys_nor_tasks(py: PyWorld) -> None:
    world = py.world
    engine = py.engine(_forged)
    _start(world)
    with world.session() as session:
        version = session.get(JobVersion, world.version_id)
        assert version is not None
        session.add(
            DeployRun(
                id=uuid.uuid4(),
                environment_id=version.environment_id,
                repository_id=version.repository_id,
                principal_id=world.admin.id,
                kind=DeployKind.DEPLOY,
                status=DeployStatus.QUEUED,
            )
        )
        session.commit()
    with world.session() as session:
        served = poll_once(session, AllowAllAuthorization(), engine=engine, only_queries=True)
        assert served is False
    assert _task(world, "silver.clean").status is TaskStatus.READY
    with world.session() as session:
        assert session.execute(select(DeployRun.status)).scalar_one() is DeployStatus.QUEUED
        queued = QueryService(session, AllowAllAuthorization()).submit_query(
            world.admin, "dev", "SELECT 1"
        )
        queued_id = queued.id
    with world.session() as session:
        served = poll_once(session, AllowAllAuthorization(), engine=engine, only_queries=True)
        assert served is True
    with world.session() as session:
        row = session.get(QueryRun, queued_id)
        assert row is not None and row.status is QueryRunStatus.SUCCEEDED
        assert session.execute(select(DeployRun.status)).scalar_one() is DeployStatus.QUEUED


def test_a_run_that_vanished_fails_the_attempt_without_a_process(py: PyWorld) -> None:
    """`run_claimed` sends a vanished run down the SQL path; the runtime itself is still safe."""
    engine = py.engine(_forged)
    claimed = py.claim_python(engine)
    keeper = LeaseKeeper(
        py.world.session,
        claimed.task_run_id,
        "w1",
        claimed.claim,
        lease_seconds=60,
        renew_seconds=60,
    )
    with py.world.session() as session:
        session.delete(session.get(Run, claimed.run_id))
        session.commit()
    assert engine.python is not None
    outcome = engine.python.attempt(engine, claimed, keeper, None, None)
    assert outcome is not None
    assert (outcome.kind, outcome.message) == (
        FailureKind.PERMANENT,
        "the run or its project version no longer exists",
    )
    assert py.tokens() == [] and py.runner.tasks == []


def test_an_inactive_run_principal_fails_the_python_task_permanently(py: PyWorld) -> None:
    engine = py.engine(_forged)
    claimed = py.claim_python(engine)
    with py.world.session() as session:
        account = session.get(Principal, py.world.admin.id)
        assert account is not None
        account.is_active = False
        session.commit()
    run_claimed(engine, claimed)
    assert _task(py.world, "gold.scores").status is TaskStatus.FAILED
    message = _attempts(py.world, claimed.task_run_id)[0].error_message
    assert message == "the run principal is deactivated or deleted"
    assert _task(py.world, "gold.scores").failure_code == "permission_denied"


def test_an_unbuilt_upstream_outside_the_selection_fails_the_python_task(py: PyWorld) -> None:
    engine = py.engine(_forged)
    _start(py.world, Selection(patterns=("gold.scores",)))
    run_claimed(engine, _claim(py.world))
    task = _task(py.world, "gold.scores")
    assert task.failure_code == "upstream_missing"
    assert (task.status, task.error_message) == (
        TaskStatus.FAILED,
        "upstream table 'silver.clean' has not been built",
    )
    assert py.runner.tasks == []


def test_the_worker_creates_a_missing_nested_namespace_for_a_python_asset(py: PyWorld) -> None:
    """A bound token never gets CREATE_NAMESPACE; the worker creates `gold.ml`."""
    py.deploy(table="gold.ml.scores")
    engine = py.engine(_writer(py.world))
    claimed = py.claim_python(engine)
    run_claimed(engine, claimed)
    assert _task(py.world, "gold.ml.scores").status is TaskStatus.SUCCEEDED


def test_a_permission_refusal_while_preparing_fails_permanently_and_says_so(py: PyWorld) -> None:
    py.deploy(table="gold.ml.scores")
    engine = py.engine(_forged)
    claimed = py.claim_python(engine)
    allowed = [action for action in Action if action is not Action.CREATE_NAMESPACE]
    engine.authz_factory = lambda _session: ActionAllowList(*allowed)
    run_claimed(engine, claimed)
    task = _task(py.world, "gold.ml.scores")
    assert (task.status, task.attempt) == (TaskStatus.FAILED, 1)
    assert task.error_message is not None
    assert task.error_message.startswith("permission denied")
    assert "CREATE_NAMESPACE" in task.error_message
    assert task.failure_code == "permission_denied"
    assert py.tokens() == [] and py.runner.tasks == []


def test_a_refusal_while_the_run_is_being_cancelled_is_a_cancellation(py: PyWorld) -> None:
    """A refusal that arrives with a cancel request is the cancel, not a permanent failure."""
    py.deploy(table="gold.ml.scores")
    engine = py.engine(_forged)
    claimed = py.claim_python(engine)
    allowed = [action for action in Action if action is not Action.CREATE_NAMESPACE]
    engine.authz_factory = lambda _session: ActionAllowList(*allowed)
    py.cancel(claimed.run_id)
    run_claimed(engine, claimed)
    assert _task(py.world, "gold.ml.scores").status is TaskStatus.CANCELLED
    assert py.runner.tasks == []


_HARNESS_HEAD = """\
import json, os, sys, time
description = json.loads(sys.stdin.read())

def write(record):
    with open(description["result_path"], "w") as handle:
        json.dump(record, handle)
"""


@dataclass
class RealProcess:
    """The real `LocalProcessRunner` running a scripted module from a stand-in venv.

    Parameters
    ----------
    py
        The world.
    body
        Python appended to the script's head; it stands in for `lakana.runner`.
    on_output
        Called with each piece of output the runner forwards, in the supervisor thread.
    tasks
        Every task it was given.
    isolated
        `False`, as `LocalProcessRunner`'s (`Runner.isolated`).
    """

    py: PyWorld
    body: str
    on_output: Callable[[bytes], None] = field(default=lambda _data: None)
    tasks: list[PythonTask] = field(default_factory=list)
    isolated: bool = False

    def __post_init__(self) -> None:
        """Build a venv whose interpreter is this one and whose site-packages has the module."""
        self.venv = self.py.root / "venv"
        (self.venv / "bin").mkdir(parents=True)
        os.symlink(sys.executable, self.venv / "bin" / "python")
        home = os.path.dirname(os.path.realpath(sys.executable))
        (self.venv / "pyvenv.cfg").write_text(
            f"home = {home}\ninclude-system-site-packages = false\n"
        )
        version = f"python{sys.version_info.major}.{sys.version_info.minor}"
        site = self.venv / "lib" / version / "site-packages"
        site.mkdir(parents=True)
        (site / "fake_harness.py").write_text(_HARNESS_HEAD + self.body)
        self._runner = LocalProcessRunner(kill_grace=0.2, module="fake_harness", poll_seconds=0.02)

    def acquire(
        self,
        settings: VenvSettings,
        sha: str,
        project: Path,
        on_log: Callable[[bytes], None],
        indexes: IndexResolver | None = None,
        *,
        stop: Callable[[], bool] | None = None,
    ) -> VenvLease:
        """Lease the stand-in venv.

        Parameters
        ----------
        settings
            Ignored.
        sha
            Ignored.
        project
            Ignored.
        on_log
            Ignored.
        indexes
            Ignored.
        stop
            Ignored.

        Returns
        -------
        VenvLease
            A lease without a lock.
        """
        return VenvLease(path=self.venv, python=self.venv / "bin" / "python")

    def run(
        self, task: PythonTask, on_log: Callable[[bytes], None], stop: StopSignal
    ) -> RunOutcome:
        """Run the scripted module with the real runner.

        Parameters
        ----------
        task
            The task.
        on_log
            The worker's capped sink.
        stop
            The supervisor's stop signal.

        Returns
        -------
        RunOutcome
            The real runner's outcome.
        """
        self.tasks.append(task)

        def forward(data: bytes) -> None:
            on_log(data)
            self.on_output(data)

        return self._runner.run(task, forward, stop)

    def engine(self) -> RunEngine:
        """Build an engine whose Python runtime uses this runner.

        Returns
        -------
        RunEngine
            The engine.
        """
        return self.py.engine(_forged, runner=self, acquire_venv=self.acquire)


def test_the_real_runner_gets_a_private_directory_and_its_record_is_classified(
    py: PyWorld,
) -> None:
    """The runtime and `LocalProcessRunner` fit: directories, stdin, redaction, cleanup."""
    process = RealProcess(
        py,
        "print('token:', description['token'])\n"
        "print('cwd:', os.getcwd())\n"
        "print('home:', os.environ['HOME'])\n"
        "print('parent:', description['parent_pid'] == os.getppid())\n"
        "print('expected:', description['expected_snapshot_id'], description['inputs'])\n"
        "write({'status': 'failed', 'error_type': 'ValueError', 'message': 'bad frame',"
        " 'retryable': False, 'phase': 'validate', 'conflict': False})\n"
        "sys.exit(1)\n",
    )
    engine = process.engine()
    claimed = py.claim_python(engine)
    run_claimed(engine, claimed)
    task = _task(py.world, "gold.scores")
    assert (task.status, task.attempt, task.error_message) == (TaskStatus.FAILED, 1, "bad frame")
    (ran,) = process.tasks
    log = _log(py.world, claimed.run_id, "gold.scores")
    assert "    token: ***" in log and ran.token not in log
    assert f"    cwd: {ran.code_dir}" in log
    assert f"    home: {ran.attempt_dir / 'home'}" in log
    assert "    parent: True" in log
    assert "    expected: none {'clean': 'silver.clean'}" in log
    assert not ran.attempt_dir.exists()
    assert py.tokens()[0].revoked_at is not None


def test_a_type_error_of_the_asset_fails_the_task_once_with_the_harness_message(
    py: PyWorld,
) -> None:
    """The record the real harness writes for a `TypeError` is not retried by the worker."""
    record = harness_runner._failure_record(
        harness_runner.Failure("function", TypeError("unsupported operand type(s) for *")), "t"
    )
    assert record["retryable"] is False
    process = RealProcess(py, f"write({record!r})\nsys.exit(1)\n")
    engine = process.engine()
    run_claimed(engine, py.claim_python(engine))
    task = _task(py.world, "gold.scores")
    assert (task.status, task.attempt) == (TaskStatus.FAILED, 1)
    assert task.error_message == record["message"]
    assert "almost always a mistake in the asset's code" in str(task.error_message)


def _input_error(cause: Exception) -> harness_runner.InputError:
    """Build the error the harness raises for a failed artifact download.

    Parameters
    ----------
    cause
        What the SDK raised.

    Returns
    -------
    InputError
        As `lakana.runner._fetch_artifact` raises it.
    """
    error = harness_runner.ArtifactInputError(
        f"reading input 'm' (models.m, version 1) failed: {cause}"
    )
    error.__cause__ = cause
    return error


@pytest.mark.parametrize(
    ("phase", "cause", "status"),
    [
        pytest.param(
            "inputs", sdk_manifest.StoredManifestDamaged("damaged"), TaskStatus.FAILED, id="damaged"
        ),
        pytest.param(
            "inputs",
            sdk_manifest.StoredManifestInvalid("over a limit"),
            TaskStatus.FAILED,
            id="invalid",
        ),
        pytest.param(
            "upload", sdk_errors.PayloadTooLarge("limit", status=413), TaskStatus.FAILED, id="413"
        ),
        # what may clear stays retried: the new classes must not make these permanent
        pytest.param("inputs", sdk_errors.ArtifactCorrupt("a file"), TaskStatus.PENDING, id="file"),
        pytest.param(
            "inputs", sdk_errors.ServerError("busy", status=503), TaskStatus.PENDING, id="5xx"
        ),
        pytest.param(
            "inputs", sdk_errors.LakanaConnectionError("reset"), TaskStatus.PENDING, id="connection"
        ),
        pytest.param(
            "upload",
            sdk_errors.ServerError("storage unavailable", status=503),
            TaskStatus.PENDING,
            id="503-after-the-resends",
        ),
    ],
)
def test_the_worker_retries_exactly_what_the_harness_says_may_clear(
    py: PyWorld, phase: str, cause: Exception, status: TaskStatus
) -> None:
    """The harness record decides; these are the records of the retry classes."""
    error = _input_error(cause) if phase == "inputs" else cause
    record = harness_runner._failure_record(harness_runner.Failure(phase, error), "t")
    process = RealProcess(py, f"write({record!r})\nsys.exit(1)\n")
    engine = process.engine()
    run_claimed(engine, py.claim_python(engine))
    task = _task(py.world, "gold.scores")
    assert (task.status, task.attempt) == (status, 1)
    assert task.error_message == record["message"]


def test_a_value_error_of_the_asset_is_retried_by_the_worker(py: PyWorld) -> None:
    record = harness_runner._failure_record(
        harness_runner.Failure("function", ValueError("odd")), "t"
    )
    assert record["retryable"] is True
    process = RealProcess(py, f"write({record!r})\nsys.exit(1)\n")
    engine = process.engine()
    run_claimed(engine, py.claim_python(engine))
    assert _task(py.world, "gold.scores").status is TaskStatus.PENDING


def test_a_cancel_kills_the_real_process_group_and_cancels_the_task(py: PyWorld) -> None:
    def cancel_when_ready(data: bytes) -> None:
        if b"ready" in data:
            py.cancel(claimed.run_id)

    py.deploy(timeout_seconds=30)
    process = RealProcess(py, "print('ready', flush=True)\ntime.sleep(600)\n", cancel_when_ready)
    engine = process.engine()
    claimed = py.claim_python(engine)
    started = time.monotonic()
    run_claimed(engine, claimed)
    assert time.monotonic() - started < _WAIT
    assert _task(py.world, "gold.scores").status is TaskStatus.CANCELLED
    row = _attempts(py.world, claimed.task_run_id)[0]
    assert (row.outcome, row.error_message) == (AttemptOutcome.CANCELLED, RUN_CANCELLED_MESSAGE)
    assert not process.tasks[0].attempt_dir.exists()
    assert py.tokens()[0].revoked_at is not None


def test_a_forged_success_with_the_table_dropped_meanwhile_is_not_a_success(py: PyWorld) -> None:
    """A vanished table is a changed state, and still nothing this attempt wrote."""
    first = py.engine(_writer(py.world))
    run_claimed(first, py.claim_python(first))
    with py.world.session() as session:
        scheduler_tick(session, datetime.now(UTC), lambda: 0.5)

    def script(task: PythonTask, on_log: Callable[[bytes], None], stop: StopSignal) -> RunOutcome:
        with py.world.session() as session:
            CatalogService(session, StorageResolver(session), AllowAllAuthorization()).drop_table(
                py.world.admin, TableRef.from_dotted("dev", task.table)
            )
        return _forged(task, on_log, stop)

    task, message = _fails(py, py.engine(script))
    assert (task.status, task.attempt) == (TaskStatus.FAILED, 1)
    assert message.startswith(_NO_WRITE)


def test_a_cancel_wins_over_a_storage_error_in_the_check_after_the_run(
    py: PyWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(*_args: object, **_kwargs: object) -> object:
        raise OSError("connection reset")

    def script(task: PythonTask, on_log: Callable[[bytes], None], stop: StopSignal) -> RunOutcome:
        written = _upload(py.world, task)
        py.cancel(claimed.run_id)
        monkeypatch.setattr(python_tasks_module, "read_metadata", explode)
        return RunOutcome(succeeded=True, snapshot_id=written.snapshot_id, rows=written.rows)

    engine = py.engine(script)
    claimed = py.claim_python(engine)
    run_claimed(engine, claimed)
    assert _task(py.world, "gold.scores").status is TaskStatus.CANCELLED
    row = _attempts(py.world, claimed.task_run_id)[0]
    assert (row.outcome, row.error_message) == (AttemptOutcome.CANCELLED, RUN_CANCELLED_MESSAGE)


def test_a_storage_error_in_the_check_after_the_run_is_retried(
    py: PyWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(*_args: object, **_kwargs: object) -> object:
        raise OSError("connection reset")

    def script(task: PythonTask, on_log: Callable[[bytes], None], stop: StopSignal) -> RunOutcome:
        written = _upload(py.world, task)
        monkeypatch.setattr(python_tasks_module, "read_metadata", explode)
        return RunOutcome(succeeded=True, snapshot_id=written.snapshot_id, rows=written.rows)

    task, message = _fails(py, py.engine(script))
    assert (task.status, task.attempt) == (TaskStatus.PENDING, 1)
    assert message == "the table gold.scores could not be read (storage error)"


def test_a_token_cap_below_the_timeout_fails_up_front_and_names_the_setting(py: PyWorld) -> None:
    """A token that cannot outlive the task is never minted: no opaque 401 mid-run."""
    engine = py.engine(_forged, max_ttl_seconds=600, kill_grace=10.0)
    task, message = _fails(py, engine)
    assert task.failure_code == "no_runtime"
    assert (task.status, task.attempt) == (TaskStatus.FAILED, 1)
    assert message == (
        "the task's token could not outlive the task: the asset's timeout (900 s) plus the "
        "kill grace and a margin needs 970 s, but LAKANA_TASK_TOKENS__MAX_TTL_SECONDS is 600; "
        "raise that setting to at least 970 or lower the asset's timeout"
    )
    assert py.tokens() == [] and py.runner.tasks == [] and py.leases == []


def test_a_python_attempt_that_cannot_start_is_a_generic_transient_failure(
    py: PyWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`run_claimed` never raises: a thread that cannot be started is recorded, not thrown."""
    engine = py.engine(_forged)
    claimed = py.claim_python(engine)

    def no_threads(self: threading.Thread) -> None:
        if self.name == "lakana-python-task":
            raise RuntimeError("can't start new thread")
        start(self)

    start = threading.Thread.start
    monkeypatch.setattr(threading.Thread, "start", no_threads)
    run_claimed(engine, claimed)
    task = _task(py.world, "gold.scores")
    assert task.failure_code == "internal"
    assert (task.status, task.attempt) == (TaskStatus.PENDING, 1)
    assert task.error_message == "internal error while running the task; see the worker log"


def test_a_failing_kind_lookup_is_a_generic_transient_failure(
    py: PyWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = py.engine(_forged)
    claimed = py.claim_python(engine)

    def explode(*_args: object) -> bool:
        raise OperationalError("select", {}, Exception("connection reset"))

    monkeypatch.setattr(tasks_module, "_is_python", explode)
    run_claimed(engine, claimed)
    task = _task(py.world, "gold.scores")
    assert task.failure_code == "internal"
    assert (task.status, task.attempt) == (TaskStatus.PENDING, 1)
    assert task.error_message == "internal error while running the task; see the worker log"
    assert py.runner.tasks == []


def test_a_deployed_uv_toml_fails_permanently_and_says_to_move_the_settings(py: PyWorld) -> None:
    """A project version deployed before the deploy check refused `uv.toml`."""
    lock = _lock()
    py.deploy(lock, code=_code(lock, extra=(("uv.toml", b"compile-bytecode = true\n"),)))
    task, message = _fails(py, py.engine(_forged))
    assert task.failure_code == "build_failed"
    assert task.status is TaskStatus.FAILED
    assert "uv.toml" in message and "[tool.uv]" in message and "redeploy" in message
    assert py.leases == [] and py.tokens() == [] and py.runner.tasks == []


class _SweepSpy:
    """Records what the runtime tells its sweeper."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    def hold(self, attempt_dir: Path) -> None:
        """Record a hold.

        Parameters
        ----------
        attempt_dir
            The directory.
        """
        self.events.append(("hold", attempt_dir.name))

    def release(self, attempt_dir: Path) -> None:
        """Record a release, noting whether the directory was already gone.

        Parameters
        ----------
        attempt_dir
            The directory.
        """
        self.events.append(("release", attempt_dir.name))
        assert not attempt_dir.exists()

    def maybe_sweep(self) -> None:
        """Record a sweep."""
        self.events.append(("sweep", ""))


def test_the_runtime_holds_an_attempt_directory_until_it_is_removed(py: PyWorld) -> None:
    """The sweeper is told which directories this worker's tasks are running in."""
    py.deploy(_lock("999.0.0"))
    spy = _SweepSpy()
    engine = py.engine(_forged, sweeper=spy)
    _fails(py, engine)
    assert engine.python is not None
    cast("python_tasks_module.PythonRuntime", engine.python).maintain()
    [(hold, name), (release, same), (sweep, _)] = spy.events
    assert (hold, release, sweep) == ("hold", "release", "sweep") and name == same
    assert name.startswith("attempt-")


def test_maintain_also_runs_the_deploy_checkout_sweep(py: PyWorld) -> None:
    """The periodic point that sweeps attempt directories sweeps deploy checkouts too."""
    spy = _SweepSpy()
    runtime = py.runtime(_forged)
    runtime.deploy_sweeper = cast("DeploySweeper", spy)
    runtime.maintain()
    assert spy.events == [("sweep", "")]


def test_maintain_also_starts_the_measurement_of_the_caches(py: PyWorld) -> None:
    """The periodic point that sweeps attempt directories measures the caches too."""
    keeper = mock.MagicMock()
    runtime = py.runtime(_forged)
    runtime.caches = keeper
    runtime.maintain()
    keeper.maybe_start.assert_called_once_with()


def test_maintain_without_a_sweeper_does_nothing(py: PyWorld) -> None:
    py.runtime(_forged).maintain()


def test_the_attempt_lock_is_released_when_the_attempt_fails(py: PyWorld) -> None:
    """Review R5: no descriptor leaks across tasks, also on a failure path."""
    py.deploy(_lock("999.0.0"))
    sweeper = AttemptSweeper(py.root / "work", max_age=1000.0, interval=0.0)
    engine = py.engine(_forged, sweeper=sweeper)
    _fails(py, engine)
    assert sweeper.holding() == 0
    assert list((py.root / "work").iterdir()) == []


def test_an_attempt_whose_directory_cannot_be_locked_fails_and_leaves_nothing(
    py: PyWorld,
) -> None:
    class Refusing(AttemptSweeper):
        def hold(self, attempt_dir: Path) -> None:
            raise OSError(5, "Input/output error")

    py.deploy()
    sweeper = Refusing(py.root / "work", max_age=1000.0, interval=0.0)
    _, message = _fails(py, py.engine(_forged, sweeper=sweeper))
    assert "could not create the task's working directory" in message
    assert list((py.root / "work").iterdir()) == []


_ARTIFACT_ENTRY: dict[str, object] = {"output": "artifact", "input_kinds": {"clean": "table"}}
_NO_VERSION = "the task reported success without storing a version of its artifact"


def _store(world: World, task: PythonTask, body: bytes = b"model") -> int:
    """Store a version of the task's artifact as the harness would: with the task's token.

    Parameters
    ----------
    world
        The world.
    task
        The task, for its token and its artifact's name.
    body
        The one file's contents.

    Returns
    -------
    int
        The committed version number.
    """
    parsed = parse_token(task.token, "lakana_task")
    assert parsed is not None
    digest = hashlib.sha256(body).hexdigest()
    document = {
        "format": 1,
        "is_directory": False,
        "files": [{"path": "model.bin", "size": len(body), "sha256": digest}],
    }
    with world.session() as session:
        principal = TaskTokenService(session, AllowAllAuthorization()).verify(*parsed).principal
        service = ArtifactService(session, StorageResolver(session), AllowAllAuthorization())
        ref = TableRef.from_dotted("dev", task.table)
        upload = service.begin_upload(principal, ref, document)
        for missing in upload.missing:
            service.put_blob(principal, ref, upload.upload_id, missing, [body])
        return service.commit_upload(principal, ref, upload.upload_id).version.version


def _producer(world: World) -> Script:
    """Build a script that behaves like a well-behaved artifact task.

    Parameters
    ----------
    world
        The world.

    Returns
    -------
    Script
        Stores one version and reports success.
    """

    def script(task: PythonTask, on_log: Callable[[bytes], None], stop: StopSignal) -> RunOutcome:
        on_log(b"training\n")
        _store(world, task)
        return RunOutcome(succeeded=True)

    return script


def _claims_a_version(
    task: PythonTask, on_log: Callable[[bytes], None], stop: StopSignal
) -> RunOutcome:
    """Report a stored version without storing one.

    Parameters
    ----------
    task
        The task.
    on_log
        The log sink.
    stop
        The stop signal.

    Returns
    -------
    RunOutcome
        A forged success.
    """
    return RunOutcome(succeeded=True)


def _commit_as_admin(world: World, dotted: str, body: bytes) -> int:
    """Commit a version of an artifact as a user, with no task behind it.

    Parameters
    ----------
    world
        The world.
    dotted
        The artifact's dotted name.
    body
        The one file's contents.

    Returns
    -------
    int
        The committed version number.
    """
    with world.session() as session:
        admin = world.admin
        service = ArtifactService(session, StorageResolver(session), AllowAllAuthorization())
        ref = TableRef.from_dotted("dev", dotted)
        document = {
            "format": 1,
            "is_directory": False,
            "files": [{"path": "m", "size": len(body), "sha256": hashlib.sha256(body).hexdigest()}],
        }
        upload = service.begin_upload(admin, ref, document)
        service.put_blob(admin, ref, upload.upload_id, upload.missing[0], [body])
        return service.commit_upload(admin, ref, upload.upload_id).version.version


def test_an_artifact_task_succeeds_when_this_attempt_committed_a_version(py: PyWorld) -> None:
    py.deploy(table="models.churn", entry=_ARTIFACT_ENTRY)
    engine = py.engine(_producer(py.world))
    claimed = py.claim_python(engine)
    run_claimed(engine, claimed)
    with py.world.session() as session:
        task = session.get(TaskRun, claimed.task_run_id)
        assert task is not None
        assert task.status is TaskStatus.SUCCEEDED
        row = session.scalars(select(ArtifactVersion)).one()
        assert (row.version, row.task_run_id, row.claim) == (1, claimed.task_run_id, claimed.claim)
    (given,) = py.runner.tasks
    assert (given.output, dict(given.input_kinds), dict(given.input_versions)) == (
        "artifact",
        {"clean": "table"},
        {},
    )
    assert given.expected_snapshot_id == "none"
    log = _log(py.world, claimed.run_id, "models.churn")
    assert "  artifact version: 1" in log
    assert "  write mode: -" in log
    assert py.tokens()[0].revoked_at is not None


def test_a_version_pruned_right_after_its_commit_still_makes_its_attempt_succeed(
    py: PyWorld,
) -> None:
    """An overlapping run's `keep_versions` may delete the version before the check."""
    py.deploy(table="models.churn", entry=_ARTIFACT_ENTRY)

    def script(task: PythonTask, on_log: Callable[[bytes], None], stop: StopSignal) -> RunOutcome:
        _store(py.world, task)
        with py.world.session() as session:
            session.execute(update(ArtifactVersion).values(deleted_at=datetime.now(UTC)))
            session.commit()
        return RunOutcome(succeeded=True)

    engine = py.engine(script)
    claimed = py.claim_python(engine)
    run_claimed(engine, claimed)
    with py.world.session() as session:
        task = session.get(TaskRun, claimed.task_run_id)
        assert task is not None
        assert (task.status, task.attempt) == (TaskStatus.SUCCEEDED, 1)
        row = session.scalars(select(ArtifactVersion)).one()
        assert (row.version, row.task_run_id, row.claim) == (1, claimed.task_run_id, claimed.claim)
        assert row.deleted_at is not None


def test_a_version_of_another_attempt_does_not_make_this_attempt_succeed(py: PyWorld) -> None:
    """The worker counts only a version with this task run and this claim."""
    py.deploy(table="models.churn", entry=_ARTIFACT_ENTRY)
    with py.world.session() as session:
        environment_id = session.scalars(
            select(Environment.id).where(Environment.name == "dev")
        ).one()
        session.add(
            Namespace(id=uuid.uuid4(), environment_id=environment_id, path="models", properties={})
        )
        session.commit()
    _commit_as_admin(py.world, "models.churn", b"x")
    task, message = _fails(py, py.engine(_claims_a_version))
    assert (task.status, task.attempt) == (TaskStatus.FAILED, 1)
    assert message.startswith(_NO_VERSION)
    with py.world.session() as session:
        assert session.scalars(select(ArtifactVersion.version)).all() == [1]


def test_an_artifact_input_without_a_version_fails_permanently_and_says_what_to_run(
    py: PyWorld,
) -> None:
    py.deploy(
        table="gold.scores",
        entry={
            "inputs": {"model": "models.churn", "clean": "silver.clean"},
            "input_kinds": {"model": "artifact", "clean": "table"},
        },
    )
    task, message = _fails(py, py.engine(_writer(py.world)))
    assert task.failure_code == "upstream_missing"
    assert (task.status, task.attempt) == (TaskStatus.FAILED, 1)
    assert message == "models.churn has no version yet; run the asset that produces it"
    assert py.runner.tasks == []


def test_a_consumer_reads_the_current_version_of_an_input_artifact(py: PyWorld) -> None:
    with py.world.session() as session:
        environment_id = session.scalars(
            select(Environment.id).where(Environment.name == "dev")
        ).one()
        session.add(
            Namespace(id=uuid.uuid4(), environment_id=environment_id, path="models", properties={})
        )
        session.commit()
    for body in (b"one", b"two"):
        _commit_as_admin(py.world, "models.churn", body)
    py.deploy(
        table="gold.scores",
        entry={
            "inputs": {"model": "models.churn", "clean": "silver.clean"},
            "input_kinds": {"model": "artifact", "clean": "table"},
        },
    )
    engine = py.engine(_writer(py.world))
    run_claimed(engine, py.claim_python(engine))
    (given,) = py.runner.tasks
    assert (given.output, dict(given.input_kinds), dict(given.input_versions)) == (
        "table",
        {"model": "artifact", "clean": "table"},
        {"model": 2},
    )


@pytest.mark.parametrize(
    ("entry", "decimals"),
    [({}, "float"), ({"decimals": "float"}, "float"), ({"decimals": "exact"}, "exact")],
)
def test_the_task_carries_the_decimals_option_of_the_manifest_entry(
    py: PyWorld, entry: dict[str, object], decimals: str
) -> None:
    """An entry deployed before the option existed has no key and means `float`."""
    py.deploy(entry=entry)
    engine = py.engine(_writer(py.world))
    run_claimed(engine, py.claim_python(engine))
    (given,) = py.runner.tasks
    assert given.decimals == decimals


def _fails_after_claim(
    py: PyWorld, engine: RunEngine, change: Callable[[Session], None]
) -> tuple[TaskRun, str]:
    """Claim the Python task, change the catalog, then run it, expecting a recorded failure.

    Parameters
    ----------
    py
        The world.
    engine
        The engine.
    change
        Runs on a session after the upstream ran and before the task does; commits itself.

    Returns
    -------
    tuple
        The task run and the attempt's error message.
    """
    claimed = py.claim_python(engine)
    with py.world.session() as session:
        change(session)
    run_claimed(engine, claimed)
    message = _attempts(py.world, claimed.task_run_id)[0].error_message
    assert message is not None
    return _task(py.world, claimed.asset), message


def test_a_python_task_an_older_server_started_for_a_table_with_checks_is_refused_first(
    py: PyWorld,
) -> None:
    """Before the process, the lease, the token and the baseline; the refusal is permanent."""

    def list_checks(session: Session) -> None:
        version = session.scalars(select(JobVersion)).one()
        version.manifest_json = {
            **version.manifest_json,
            "version": 4,
            "checks": [_check("range", "SELECT id FROM gold.scores", table="gold.scores")],
        }
        session.commit()

    engine = py.engine(_forged)
    claimed = py.claim_python(engine)
    with py.world.session() as session:
        list_checks(session)
    run_claimed(engine, claimed)

    task = _task(py.world, "gold.scores")
    assert task.status is TaskStatus.FAILED
    assert task.error_message == (
        "This run was started by a server that does not know checks, but the project has "
        "checks for `gold.scores`. Upgrade the server to the version of the workers, then run "
        "again."
    )
    assert py.leases == [] and py.tokens() == [] and py.runner.tasks == []


def _retype_clean(table_type: TableType) -> Callable[[Session], None]:
    """Build a change that gives `silver.clean` another kind.

    Parameters
    ----------
    table_type
        The new kind.

    Returns
    -------
    Callable
        The change.
    """

    def change(session: Session) -> None:
        """Retype the row.

        Parameters
        ----------
        session
            A session.
        """
        row = session.scalars(select(Table).where(Table.name == "clean")).one()
        row.table_type = table_type
        if table_type is not TableType.ICEBERG:
            row.metadata_location = None
        session.commit()

    return change


def test_an_input_recorded_as_an_artifact_that_is_a_table_fails_permanently(py: PyWorld) -> None:
    py.deploy(entry={"input_kinds": {"clean": "artifact"}})
    task, text = _fails(py, py.engine(_writer(py.world)))
    assert text == (
        "silver.clean is a table, but this deploy recorded it as an artifact; redeploy so that "
        "the asset reads it as a table"
    )
    assert (task.status, task.attempt) == (TaskStatus.FAILED, 1)
    assert task.failure_code == "code_changed"
    assert py.runner.tasks == []


@pytest.mark.parametrize(
    ("table_type", "words"),
    [(TableType.VIEW, "a view"), (TableType.EXTERNAL, "an external table")],
)
def test_the_other_kinds_are_named_in_words(py: PyWorld, table_type: TableType, words: str) -> None:
    py.deploy(entry={"input_kinds": {"clean": "artifact"}})
    _, text = _fails_after_claim(py, py.engine(_writer(py.world)), _retype_clean(table_type))
    assert text.startswith(f"silver.clean is {words}, but this deploy recorded it as an artifact")


def test_a_table_input_that_is_an_artifact_now_fails_permanently(py: PyWorld) -> None:
    task, text = _fails_after_claim(
        py, py.engine(_writer(py.world)), _retype_clean(TableType.ARTIFACT)
    )
    assert text == (
        "silver.clean is an artifact, but this deploy recorded it as a table; redeploy so that "
        "the asset receives a path"
    )
    assert (task.status, task.attempt) == (TaskStatus.FAILED, 1)
    assert task.failure_code == "code_changed"
    assert py.runner.tasks == []


def test_an_artifact_name_taken_by_a_table_fails_permanently(py: PyWorld) -> None:
    py.deploy(table="gold.scores", entry=_ARTIFACT_ENTRY)
    first = py.engine(_writer(py.world))
    with py.world.session() as session:
        CatalogService(session, StorageResolver(session), AllowAllAuthorization()).replace(
            py.world.admin,
            TableRef.from_dotted("dev", "gold.scores"),
            pa.table({"id": pa.array([1], pa.int64())}),
            expected_snapshot_id="none",
        )
    task, text = _fails(py, first)
    assert text == (
        "gold.scores is a table, not an artifact. Drop it (`lakana table delete "
        "gold.scores`) or give the asset another name and redeploy."
    )
    assert (task.status, task.attempt) == (TaskStatus.FAILED, 1)
    assert task.failure_code == "build_failed"
    assert py.runner.tasks == []


def test_a_cancel_wins_over_a_forged_artifact_success(py: PyWorld) -> None:
    py.deploy(table="models.churn", entry=_ARTIFACT_ENTRY)
    runs: list[uuid.UUID] = []

    def script(task: PythonTask, on_log: Callable[[bytes], None], stop: StopSignal) -> RunOutcome:
        py.cancel(runs[0])
        return RunOutcome(succeeded=True)

    engine = py.engine(script)
    claimed = py.claim_python(engine)
    runs.append(claimed.run_id)
    run_claimed(engine, claimed)
    with py.world.session() as session:
        task = session.get(TaskRun, claimed.task_run_id)
        assert task is not None
        assert task.status is TaskStatus.CANCELLED


def _versions(py: PyWorld) -> list[tuple[int, int | None]]:
    """List every artifact version row as ``(version, claim)``.

    Parameters
    ----------
    py
        The world.

    Returns
    -------
    list
        The rows, oldest first.
    """
    with py.world.session() as session:
        rows = session.execute(
            select(ArtifactVersion.version, ArtifactVersion.claim).order_by(ArtifactVersion.version)
        ).all()
        return [(row.version, row.claim) for row in rows]


def test_the_found_version_is_recorded_not_the_one_the_record_claims(py: PyWorld) -> None:
    """Point 1: the record's version is a hint; the log shows what the server holds."""
    py.deploy(table="models.churn", entry=_ARTIFACT_ENTRY)

    def script(task: PythonTask, on_log: Callable[[bytes], None], stop: StopSignal) -> RunOutcome:
        _store(py.world, task)
        return RunOutcome(succeeded=True)

    engine = py.engine(script)
    claimed = py.claim_python(engine)
    run_claimed(engine, claimed)
    assert _task(py.world, "models.churn").status is TaskStatus.SUCCEEDED
    log = _log(py.world, claimed.run_id, "models.churn")
    assert "  artifact version: 1" in log
    assert "99" not in log


def test_an_artifact_entry_is_never_run_as_a_table_write(py: PyWorld) -> None:
    """Point 6: before this task such an entry ran as a table write with `WriteMode.REPLACE`."""
    py.deploy(table="gold.scores", entry=_ARTIFACT_ENTRY)
    task, message = _fails(py, py.engine(_forged))
    assert (task.status, task.attempt) == (TaskStatus.FAILED, 1)
    assert message.startswith(_NO_VERSION)
    assert "table" not in message.split(";")[0]
    log = _log(py.world, task.run_id, "gold.scores")
    assert "write mode: replace" not in log
    with py.world.session() as session:
        assert session.scalars(select(Table).where(Table.name == "scores")).all() == []


@pytest.mark.parametrize("version", list(range(1, MANIFEST_VERSION + 1)))
def test_a_python_task_of_a_manifest_of_a_version_up_to_the_current_one_still_runs(
    py: PyWorld, version: int
) -> None:
    """I3: the fixture deploys the version of a project without checks; every version runs."""
    py.deploy()

    def change(session: Session) -> None:
        """Set the manifest's version.

        Parameters
        ----------
        session
            A session.
        """
        row = session.get(JobVersion, py.world.version_id)
        assert row is not None
        row.manifest_json = {**row.manifest_json, "version": version}
        if version >= 4:  # a deploy writes it only with a check, of another table
            row.manifest_json["checks"] = [
                _check("c", "SELECT id FROM silver.clean", table="silver.clean")
            ]
        flag_modified(row, "manifest_json")  # 3.0 == 3: without it nothing is written
        session.commit()

    engine = py.engine(_writer(py.world))
    claimed = py.claim_python(engine)
    with py.world.session() as session:
        change(session)
    run_claimed(engine, claimed)
    assert _task(py.world, "gold.scores").status is TaskStatus.SUCCEEDED


@pytest.mark.parametrize("output", ["model", "", None, 3, ["artifact"], "Artifact"])
def test_an_output_this_worker_does_not_know_fails_permanently_and_runs_nothing(
    py: PyWorld, output: object
) -> None:
    py.deploy(entry={"output": output})
    task, message = _fails(py, py.engine(_writer(py.world)))
    assert (task.status, task.attempt) == (TaskStatus.FAILED, 1)
    assert message.startswith("the project version declares an output of ")
    assert "cannot run" in message and "redeploy" in message
    assert py.runner.tasks == []
    assert _versions(py) == []


@pytest.mark.parametrize("version", [9, 99, "3", 3.0, True, [3], 0, -1, 10**30])
def test_a_manifest_this_worker_cannot_read_fails_permanently(py: PyWorld, version: object) -> None:
    py.deploy()

    def change(session: Session) -> None:
        """Make the manifest newer than the worker understands.

        Parameters
        ----------
        session
            A session.
        """
        row = session.get(JobVersion, py.world.version_id)
        assert row is not None
        row.manifest_json = {**row.manifest_json, "version": version}
        flag_modified(row, "manifest_json")  # 3.0 == 3: without it nothing is written
        session.commit()

    task, message = _fails_after_claim(py, py.engine(_writer(py.world)), change)
    assert (task.status, task.attempt) == (TaskStatus.FAILED, 1)
    assert message.startswith("the project version's manifest has version ")
    assert "update the worker" in message
    assert py.runner.tasks == []


def test_a_table_target_that_is_an_artifact_names_it_and_the_way_out(py: PyWorld) -> None:
    """Point 8: the Python path says artifact, and names `lakana artifact drop`."""
    engine = py.engine(_writer(py.world))
    claimed = py.claim_python(engine)
    _commit_as_admin(py.world, "gold.scores", b"x")
    run_claimed(engine, claimed)
    message = _attempts(py.world, claimed.task_run_id)[0].error_message
    assert message is not None
    task = _task(py.world, "gold.scores")
    assert (task.status, task.attempt) == (TaskStatus.FAILED, 1)
    assert message == (
        "gold.scores is an artifact, and this asset produces a table. Drop the artifact "
        "(`lakana artifact drop gold.scores`) or give the asset another name and redeploy."
    )
    assert py.runner.tasks == []


def test_a_version_committed_then_a_crash_is_a_failure_and_the_retry_needs_its_own_version(
    py: PyWorld,
) -> None:
    """Point 4, an older claim: attempt 1 committed and died; attempt 2's forged success fails."""
    py.deploy(table="models.churn", entry=_ARTIFACT_ENTRY)

    def crash(task: PythonTask, on_log: Callable[[bytes], None], stop: StopSignal) -> RunOutcome:
        _store(py.world, task)
        return RunOutcome(succeeded=False, error_message="the task process was killed by signal 9")

    engine = py.engine(crash)
    claimed = py.claim_python(engine)
    run_claimed(engine, claimed)
    task = _task(py.world, "models.churn")
    assert (task.status, task.attempt) == (TaskStatus.PENDING, 1)
    assert "artifact version" not in _log(py.world, claimed.run_id, "models.churn")
    assert _versions(py) == [(1, 1)]

    py.runner.script = _claims_a_version
    with py.world.session() as session:
        scheduler_tick(session, datetime.now(UTC) + timedelta(hours=1), lambda: 0.5)
    second = _claim(py.world)
    assert second.claim == 2
    run_claimed(engine, second)
    task = _task(py.world, "models.churn")
    assert (task.status, task.attempt) == (TaskStatus.FAILED, 2)
    message = _attempts(py.world, claimed.task_run_id)[1].error_message
    assert message is not None and message.startswith(_NO_VERSION)
    assert _versions(py) == [(1, 1)]


@pytest.mark.parametrize("when", ["before", "after"])
def test_an_attempt_that_lost_its_lease_records_nothing_whatever_it_committed(
    py: PyWorld, when: str
) -> None:
    """Point 4: a reclaimed attempt's outcome is discarded; its version is not its successor's."""
    py.deploy(table="models.churn", entry=_ARTIFACT_ENTRY)

    def script(task: PythonTask, on_log: Callable[[bytes], None], stop: StopSignal) -> RunOutcome:
        if when == "before":
            py.steal("models.churn")
        _store_or_fail(py, task, when == "before")
        if when == "after":
            py.steal("models.churn")
        return RunOutcome(succeeded=True)

    engine = py.engine(script)
    claimed = py.claim_python(engine)
    run_claimed(engine, claimed)
    task = _task(py.world, "models.churn")
    assert (task.status, task.lease_owner) == (TaskStatus.RUNNING, "thief")
    assert _attempts(py.world, claimed.task_run_id)[0].finished_at is None
    assert _versions(py) == ([] if when == "before" else [(1, 1)])
    assert py.tokens()[0].revoked_at is not None


def _store_or_fail(py: PyWorld, task: PythonTask, must_fail: bool) -> None:
    """Store a version; when the attempt was already reclaimed, show that the server refuses it.

    Parameters
    ----------
    py
        The world.
    task
        The task.
    must_fail
        Whether the claim has moved on, so the token no longer works.
    """
    if not must_fail:
        _store(py.world, task)
        return
    with pytest.raises(LakanaError):
        _store(py.world, task)


@pytest.mark.parametrize(
    ("outcome", "status", "attempt"),
    [
        pytest.param(
            RunOutcome(succeeded=False, error_message="upload conflict", conflict=True),
            TaskStatus.PENDING,
            1,
            id="upload-conflict-is-an-ordinary-retry",
        ),
        pytest.param(
            RunOutcome(
                succeeded=False,
                error_message="the server is busy with other downloads; try again shortly",
            ),
            TaskStatus.PENDING,
            1,
            id="busy-download-is-transient",
        ),
        pytest.param(
            RunOutcome(succeeded=False, error_message="no", retryable=False, conflict=True),
            TaskStatus.FAILED,
            1,
            id="not-retryable-wins-over-conflict",
        ),
    ],
)
def test_an_artifact_failure_is_classified(
    py: PyWorld, outcome: RunOutcome, status: TaskStatus, attempt: int
) -> None:
    """Points 13 and 15: a conflict gets no free immediate retry for an artifact."""
    py.deploy(table="models.churn", entry=_ARTIFACT_ENTRY)
    engine = py.engine(lambda _task, _log, _stop: outcome)
    claimed = py.claim_python(engine)
    run_claimed(engine, claimed)
    task = _task(py.world, "models.churn")
    assert (task.status, task.attempt, task.conflict_retried) == (status, attempt, False)
    assert _attempts(py.world, claimed.task_run_id)[0].error_message == outcome.error_message


def test_the_attempt_directory_is_absolute_and_holds_only_the_code_when_the_harness_starts(
    py: PyWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Point 10: `inputs` and `output` are the harness's to create; the path is absolute."""
    monkeypatch.chdir(py.root)
    real = tempfile.mkdtemp

    def relative_like_python_3_11(**options: str) -> str:
        """Make a directory and name it relative, as `mkdtemp` did before Python 3.12.

        Parameters
        ----------
        **options
            `mkdtemp`'s own.

        Returns
        -------
        str
            The path, relative to the working directory.
        """
        return os.path.relpath(real(**options))

    monkeypatch.setattr(python_tasks_module.tempfile, "mkdtemp", relative_like_python_3_11)
    seen: list[tuple[Path, set[str]]] = []

    def script(task: PythonTask, on_log: Callable[[bytes], None], stop: StopSignal) -> RunOutcome:
        seen.append((task.attempt_dir, {entry.name for entry in task.attempt_dir.iterdir()}))
        return _writer(py.world)(task, on_log, stop)

    engine = py.engine(script, work_dir=Path("relative-work"))
    run_claimed(engine, py.claim_python(engine))
    ((attempt_dir, names),) = seen
    assert attempt_dir.is_absolute()
    assert attempt_dir.parent == py.root / "relative-work"
    assert names == {"code"}


@pytest.mark.parametrize(
    ("entry", "extra_entries", "output"),
    [
        pytest.param({"materialize": "artifact"}, (), "table", id="materialize-is-not-the-output"),
        pytest.param(
            {"output": "artifact", "materialize": "artifact", "metadata": {"materialize": "table"}},
            (),
            "artifact",
            id="artifact-entry-as-deploy-writes-it",
        ),
        pytest.param(
            {"output": "artifact", "metadata": {"output": "table"}, "write_mode": "replace"},
            (),
            "artifact",
            id="metadata-and-write-mode-are-not-the-output",
        ),
        pytest.param(
            {},
            ({"table": "gold.scores", "kind": "python", "output": "artifact"},),
            "table",
            id="the-first-entry-of-a-name-wins",
        ),
    ],
)
def test_only_the_output_key_of_the_first_entry_tells_what_a_task_produces(
    py: PyWorld,
    entry: dict[str, object],
    extra_entries: tuple[dict[str, object], ...],
    output: str,
) -> None:
    """Point 5: never `materialize`, `write_mode` or `metadata`."""
    py.deploy(entry=entry, extra=extra_entries)
    engine = py.engine(lambda _task, _log, _stop: RunOutcome(succeeded=False, error_message="x"))
    run_claimed(engine, py.claim_python(engine))
    (given,) = py.runner.tasks
    assert given.output == output


class _DenyRead(AllowAllAuthorization):
    """Allow-all, except that the run principal may not read one table.

    Parameters
    ----------
    table
        The table's name, without its namespace.
    """

    def __init__(self, table: str) -> None:
        self.table = table
        self.asked: list[tuple[PrincipalRef, Securable]] = []

    def authorize(self, principal: PrincipalRef, action: Action, securable: Securable) -> None:
        """Refuse `READ_TABLE` on the hidden table, permit anything else.

        Parameters
        ----------
        principal
            The actor.
        action
            The action.
        securable
            The object.

        Raises
        ------
        PermissionDenied
            For `READ_TABLE` on the hidden table.
        """
        if action is Action.READ_TABLE and securable.table == self.table:
            raise PermissionDenied("denied")

    def permits(self, principal: PrincipalRef, action: Action, securable: Securable) -> bool:
        """Answer as `authorize` would, recording each `READ_TABLE` question.

        Parameters
        ----------
        principal
            The actor.
        action
            The action.
        securable
            The object.

        Returns
        -------
        bool
            Whether `authorize` would permit it.
        """
        if action is Action.READ_TABLE:
            self.asked.append((principal, securable))
        try:
            self.authorize(principal, action, securable)
        except PermissionDenied:
            return False
        return True


@pytest.mark.parametrize("run_as_may_read", [False, True])
def test_the_worker_asks_about_the_run_as_principal_and_no_other(
    py: PyWorld, run_as_may_read: bool
) -> None:
    """N2: run-as, trigger and deployer differ; only the run-as principal's grant counts."""
    py.deploy(
        entry={
            "inputs": {"x": "silver.model", "clean": "silver.clean"},
            "input_kinds": {"x": "artifact", "clean": "table"},
        }
    )
    engine = py.engine(lambda _t, _l, _s: RunOutcome(succeeded=False, error_message="boom"))
    claimed = py.claim_python(engine)
    _commit_as_admin(py.world, "silver.model", b"one")
    ids = {name: uuid.uuid4() for name in ("runner", "trigger", "deployer")}
    with py.world.session() as session:
        for name, principal_id in ids.items():
            session.add(
                Principal(id=principal_id, type=PrincipalType.USER, name=name, is_active=True)
            )
        session.flush()
        run = session.get(Run, claimed.run_id)
        version = session.get(JobVersion, py.world.version_id)
        assert run is not None and version is not None
        run.run_as_principal_id = ids["runner"]
        run.triggered_by = ids["trigger"]
        version.deployed_by = ids["deployer"]
        session.commit()
    readers = {ids["runner"]} if run_as_may_read else {ids["trigger"], ids["deployer"]}
    engine.authz_factory = lambda _session: ReadsById(readers)
    run_claimed(engine, claimed)
    message = _attempts(py.world, claimed.task_run_id)[0].error_message
    if run_as_may_read:
        assert [dict(t.input_versions) for t in py.runner.tasks] == [{"x": 1}]
        assert message == "boom"
    else:
        assert py.runner.tasks == []
        assert message == (
            "silver.model cannot be read by the run's principal 'runner', or does not exist; "
            "grant read on it or check the name"
        )


def _run_with_hidden_input(
    py: PyWorld, recorded: str, exists: str, deny: str, *, upstream: bool = False
) -> dict[str, object]:
    """Run a consumer of `silver.secret` and collect everything a person or task could see.

    Parameters
    ----------
    py
        The world.
    recorded
        The kind the manifest records for the input.
    exists
        ``absent``, ``artifact`` (three versions) or ``table``.
    deny
        The table the run principal may not read.
    upstream
        Make `silver.secret` a manifest asset with an edge to the consumer that the run
        does not select, so that the worker's upstream check meets it (N1).

    Returns
    -------
    dict
        The attempt's outcome and message, the task's status, what the runner was given
        and the task log, plus the denier's record of its questions.
    """
    py.deploy(
        entry={
            "inputs": {"x": "silver.secret", "clean": "silver.clean"},
            "input_kinds": {"x": recorded, "clean": "table"},
        }
    )
    selection = None
    if upstream:
        with py.world.session() as session:
            version = session.get(JobVersion, py.world.version_id)
            assert version is not None
            manifest = dict(version.manifest_json)
            manifest["assets"] = [
                *manifest["assets"],
                _entry("silver.secret", "SELECT 1::BIGINT AS id", kind="sql"),
            ]
            manifest["edges"] = [*manifest["edges"], ["silver.secret", "gold.scores"]]
            version.manifest_json = manifest
            session.commit()
        selection = Selection(patterns=("silver.clean", "gold.scores"))
    engine = py.engine(
        lambda _t, _l, _s: RunOutcome(succeeded=False, error_message="boom", retryable=False)
    )
    claimed = py.claim_python(engine, selection)
    if exists == "artifact":
        for body in (b"one", b"two", b"three"):
            _commit_as_admin(py.world, "silver.secret", body)
    elif exists == "table":
        with py.world.session() as session:
            CatalogService(session, StorageResolver(session), AllowAllAuthorization()).replace(
                py.world.admin,
                TableRef.from_dotted("dev", "silver.secret"),
                pa.table({"id": pa.array([1], pa.int64())}),
                expected_snapshot_id="none",
            )
    denier = _DenyRead(deny)
    engine.authz_factory = lambda _session: denier
    run_claimed(engine, claimed)
    (attempt,) = _attempts(py.world, claimed.task_run_id)
    task = _task(py.world, claimed.asset)
    return {
        "attempt": (attempt.outcome, attempt.error_message),
        "task": (task.status, task.attempt, task.error_message),
        "given": [
            (dict(t.inputs), dict(t.input_kinds), dict(t.input_versions)) for t in py.runner.tasks
        ],
        "log": _log(py.world, claimed.run_id, claimed.asset),
        "asked": [(p.name, s.table) for p, s in denier.asked],
    }


_UNREADABLE = (
    "silver.secret cannot be read by the run's principal 'admin', or does not exist; grant read "
    "on it or check the name"
)


_ATTEMPT_LINE = re.compile(r"^attempt 1 on w1 at \S+: ", re.MULTILINE)


@pytest.mark.parametrize("recorded", ["table", "artifact"])
@pytest.mark.parametrize("exists", ["absent", "artifact", "table"])
def test_an_input_the_run_principal_may_not_read_answers_as_one_that_does_not_exist(
    py: PyWorld, recorded: str, exists: str
) -> None:
    """C1: for one recorded kind, the result is the same whatever the object is."""
    result = _run_with_hidden_input(py, recorded, exists, "secret")
    log = _ATTEMPT_LINE.sub("attempt 1 on w1 at T: ", str(result["log"]))
    assert "three" not in repr(result) and "version" not in repr(result)
    if recorded == "artifact":
        assert result["asked"] == [("admin", "secret")]
        assert result["given"] == []
        assert result["attempt"] == (AttemptOutcome.FAILED, _UNREADABLE)
        assert result["task"] == (TaskStatus.FAILED, 1, _UNREADABLE)
        assert log == (
            "attempt 1 on w1 at T: permanent\n  write mode: replace\n  watermark: -\n  rows: -\n"
            f"  error: {_UNREADABLE}\n"
        )
    else:
        assert result["asked"] == [("admin", "secret"), ("admin", "clean")]
        assert result["given"] == [
            (
                {"x": "silver.secret", "clean": "silver.clean"},
                {"x": "table", "clean": "table"},
                {},
            )
        ]
        assert result["attempt"] == (AttemptOutcome.FAILED, "boom")
        assert result["task"] == (TaskStatus.FAILED, 1, "boom")
        assert log == (
            "attempt 1 on w1 at T: permanent\n  write mode: replace\n  watermark: -\n  rows: -\n"
            "  error: boom\n  output:\n    Resolved 2 packages\n"
        )


@pytest.mark.parametrize("recorded", ["table", "artifact"])
@pytest.mark.parametrize("exists", ["absent", "artifact", "table"])
def test_an_unreadable_upstream_outside_the_selection_answers_as_one_that_does_not_exist(
    py: PyWorld, recorded: str, exists: str
) -> None:
    """N1: the upstream check does not tell an absent object from one that may not be read.

    Every `exists` value must give the one result for its recorded kind.
    """
    result = _run_with_hidden_input(py, recorded, exists, "secret", upstream=True)
    assert "has not been built" not in repr(result)
    if recorded == "artifact":
        assert result["asked"] == [("admin", "secret")]
        assert result["given"] == []
        assert result["attempt"] == (AttemptOutcome.FAILED, _UNREADABLE)
        assert result["task"] == (TaskStatus.FAILED, 1, _UNREADABLE)
    else:
        assert result["asked"] == [("admin", "secret"), ("admin", "clean")]
        assert len(cast("list[object]", result["given"])) == 1
        assert result["attempt"] == (AttemptOutcome.FAILED, "boom")
        assert result["task"] == (TaskStatus.FAILED, 1, "boom")


@pytest.mark.parametrize("recorded", ["table", "artifact"])
def test_nothing_about_an_unreadable_upstream_is_looked_up(
    py: PyWorld, monkeypatch: pytest.MonkeyPatch, recorded: str
) -> None:
    """N1: no lookup of the object (existence, kind, version) precedes the authorization."""
    looked_up: list[str] = []
    real_table, real_kind, real_version = (
        tasks_module.resolve_table_id,
        python_tasks_module.kind_of,
        python_tasks_module.run_input_version,
    )

    def table_id(session: Session, environment: str, namespace: str, name: str) -> uuid.UUID | None:
        looked_up.append(f"{namespace}.{name}")
        return real_table(session, environment, namespace, name)

    def kind(session: Session, environment: str, dotted: str) -> str | None:
        looked_up.append(dotted)
        return real_kind(session, environment, dotted)

    def version(session: Session, environment: str, dotted: str, run_id: uuid.UUID) -> int | None:
        looked_up.append(dotted)
        return real_version(session, environment, dotted, run_id)

    monkeypatch.setattr(tasks_module, "resolve_table_id", table_id)
    monkeypatch.setattr(python_tasks_module, "kind_of", kind)
    monkeypatch.setattr(python_tasks_module, "run_input_version", version)
    _run_with_hidden_input(py, recorded, "artifact", "secret", upstream=True)
    assert "silver.secret" not in looked_up
    assert recorded == "artifact" or "silver.clean" in looked_up


@pytest.mark.parametrize("recorded", ["table", "artifact"])
@pytest.mark.slow
def test_a_readable_upstream_outside_the_selection_that_was_never_built_is_named(
    py: PyWorld, recorded: str
) -> None:
    """N1, the other side: a principal that may read gets the plain upstream message."""
    result = _run_with_hidden_input(py, recorded, "absent", "nothing", upstream=True)
    message = "upstream table 'silver.secret' has not been built"
    assert result["attempt"] == (AttemptOutcome.FAILED, message)
    assert result["given"] == []
    assert result["asked"] == [("admin", "secret")]


def test_a_principal_that_may_read_an_input_gets_its_version_and_one_check_per_name(
    py: PyWorld,
) -> None:
    """The permitted path is unchanged; one authorization question per input name, not per use."""
    py.deploy(
        entry={
            "inputs": {"a": "silver.model", "b": "silver.model", "clean": "silver.clean"},
            "input_kinds": {"a": "artifact", "b": "artifact", "clean": "table"},
        }
    )
    engine = py.engine(lambda _t, _l, _s: RunOutcome(succeeded=False, error_message="x"))
    claimed = py.claim_python(engine)
    for body in (b"one", b"two"):
        _commit_as_admin(py.world, "silver.model", body)
    denier = _DenyRead("secret")
    engine.authz_factory = lambda _session: denier
    run_claimed(engine, claimed)
    (given,) = py.runner.tasks
    assert dict(given.input_versions) == {"a": 2, "b": 2}
    assert [s.table for _p, s in denier.asked] == ["model", "clean"]


def test_two_parameters_that_read_one_artifact_get_one_version(
    py: PyWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """I5(a): a commit between two lookups must not give `a` and `b` different versions."""
    py.deploy(
        entry={
            "inputs": {"a": "silver.model", "b": "silver.model", "clean": "silver.clean"},
            "input_kinds": {"a": "artifact", "b": "artifact", "clean": "table"},
        }
    )
    lookups = iter(range(1, 10))
    monkeypatch.setattr(python_tasks_module, "run_input_version", lambda *_a: next(lookups))
    engine = py.engine(lambda _t, _l, _s: RunOutcome(succeeded=False, error_message="x"))
    run_claimed(engine, py.claim_python(engine))
    (given,) = py.runner.tasks
    assert dict(given.input_versions) == {"a": 1, "b": 1}


def test_a_kind_this_worker_has_no_word_for_is_named_neutrally(
    py: PyWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A kind a later platform version adds must not raise a KeyError."""
    py.deploy(entry={"input_kinds": {"clean": "artifact"}})
    monkeypatch.setattr(python_tasks_module, "kind_of", lambda *_a: "MATERIALIZED")
    _, text = _fails(py, py.engine(_writer(py.world)))
    assert text == (
        "silver.clean is another kind of object, but this deploy recorded it as an artifact; "
        "redeploy so that the asset reads it as a table"
    )


@pytest.mark.parametrize("prior", [True, False])
def test_an_artifact_asset_that_reads_itself_is_refused_in_artifact_words(
    py: PyWorld, prior: bool
) -> None:
    """M2, N3: no table text, and no "has no version yet" advice that can never work."""
    py.deploy(
        table="models.churn",
        entry={
            "output": "artifact",
            "inputs": {"previous": "models.churn", "clean": "silver.clean"},
            "input_kinds": {"previous": "artifact", "clean": "table"},
        },
    )
    engine = py.engine(_producer(py.world))
    claimed = py.claim_python(engine)
    with py.world.session() as session:
        environment_id = session.scalars(
            select(Environment.id).where(Environment.name == "dev")
        ).one()
        session.add(
            Namespace(id=uuid.uuid4(), environment_id=environment_id, path="models", properties={})
        )
        session.commit()
    if prior:
        _commit_as_admin(py.world, "models.churn", b"earlier")
    run_claimed(engine, claimed)
    message = _attempts(py.world, claimed.task_run_id)[0].error_message
    assert message == (
        "models.churn reads its own artifact as an input, which an artifact asset cannot do: it "
        "has no way to read its previous version. Remove that input and redeploy"
    )
    task = _task(py.world, "models.churn")
    assert (task.status, task.attempt) == (TaskStatus.FAILED, 1)
    assert py.runner.tasks == []
    assert _versions(py) == ([(1, None)] if prior else [])


@pytest.mark.parametrize("how", ["timeout", "stop"])
def test_a_process_stopped_after_it_committed_is_a_failure_and_its_version_stays(
    py: PyWorld, how: str
) -> None:
    """No record, so no success; the worker never deletes the version it committed."""
    py.deploy(table="models.churn", entry=_ARTIFACT_ENTRY)

    def script(task: PythonTask, on_log: Callable[[bytes], None], stop: StopSignal) -> RunOutcome:
        _store(py.world, task)
        if how == "timeout":
            return RunOutcome(
                succeeded=False, error_message="timed out after 900s", killed=True, timed_out=True
            )
        py.cancel(claimed.run_id)
        _until_stopped(task, on_log, stop)
        return RunOutcome(succeeded=False, error_message="stopped: cancelled", killed=True)

    engine = py.engine(script)
    claimed = py.claim_python(engine)
    run_claimed(engine, claimed)
    (attempt,) = _attempts(py.world, claimed.task_run_id)
    expected = AttemptOutcome.TIMED_OUT if how == "timeout" else AttemptOutcome.CANCELLED
    assert attempt.outcome is expected
    assert _task(py.world, "models.churn").status is not TaskStatus.SUCCEEDED
    assert "artifact version" not in _log(py.world, claimed.run_id, "models.churn")
    assert _versions(py) == [(1, 1)]


def _commit_then_drain(py: PyWorld) -> tuple[ClaimedTask, ClaimedTask]:
    """Commit a version under claim 1, release the task by a drain, and claim it again.

    A release gives the attempt back, so the second claim is attempt 1 again but claim 2.

    Parameters
    ----------
    py
        The world, with an artifact asset deployed.

    Returns
    -------
    tuple
        The first claim (attempt 1, claim 1) and the second (attempt 1, claim 2).
    """
    drain = threading.Event()

    def first(task: PythonTask, on_log: Callable[[bytes], None], stop: StopSignal) -> RunOutcome:
        _store(py.world, task)
        drain.set()
        return _until_stopped(task, on_log, stop)

    engine = py.engine(first, drain=drain, grace=0.05)
    one = py.claim_python(engine)
    run_claimed(engine, one)
    assert _attempts(py.world, one.task_run_id)[0].outcome is AttemptOutcome.RELEASED
    with py.world.session() as session:
        scheduler_tick(session, datetime.now(UTC) + timedelta(hours=1), lambda: 0.5)
    two = _claim(py.world)
    assert (one.attempt, one.claim, two.attempt, two.claim) == (1, 1, 1, 2)
    return one, two


def test_a_forged_success_after_a_release_does_not_borrow_the_version_of_the_earlier_claim(
    py: PyWorld,
) -> None:
    """I1: claim 2 is attempt 1; comparing the attempt number would find claim 1's version."""
    py.deploy(table="models.churn", entry=_ARTIFACT_ENTRY)
    _, two = _commit_then_drain(py)
    engine = py.engine(_claims_a_version)
    run_claimed(engine, two)
    task = _task(py.world, "models.churn")
    assert task.status is TaskStatus.FAILED
    message = _attempts(py.world, two.task_run_id)[1].error_message
    assert message is not None and message.startswith(_NO_VERSION)
    assert _versions(py) == [(1, 1)]


def test_a_real_commit_after_a_release_is_found_under_its_own_claim(py: PyWorld) -> None:
    """I1: the second claim's own version is the one recorded, though attempt and claim differ."""
    py.deploy(table="models.churn", entry=_ARTIFACT_ENTRY)
    one, two = _commit_then_drain(py)
    engine = py.engine(_producer(py.world))
    run_claimed(engine, two)
    assert _task(py.world, "models.churn").status is TaskStatus.SUCCEEDED
    assert _versions(py) == [(1, 1), (2, 2)]
    assert "  artifact version: 2" in _log(py.world, one.run_id, "models.churn")


def test_a_retry_after_a_pruned_input_resolves_again_and_an_input_with_nothing_left_fails(
    py: PyWorld,
) -> None:
    """I5(b): a download that found its version gone is retried; the next attempt looks again."""
    with py.world.session() as session:
        environment_id = session.scalars(
            select(Environment.id).where(Environment.name == "dev")
        ).one()
        session.add(
            Namespace(id=uuid.uuid4(), environment_id=environment_id, path="models", properties={})
        )
        session.commit()
    _commit_as_admin(py.world, "models.churn", b"one")
    py.deploy(
        entry={
            "inputs": {"model": "models.churn", "clean": "silver.clean"},
            "input_kinds": {"model": "artifact", "clean": "table"},
        }
    )
    gone = RunOutcome(succeeded=False, error_message="reading input 'model' failed: 404")
    engine = py.engine(lambda _t, _l, _s: gone)
    claimed = py.claim_python(engine)
    run_claimed(engine, claimed)
    assert _task(py.world, "gold.scores").status is TaskStatus.PENDING

    def delete(number: int) -> None:
        with py.world.session() as session:
            ArtifactService(
                session, StorageResolver(session), AllowAllAuthorization()
            ).delete_version(py.world.admin, TableRef.from_dotted("dev", "models.churn"), number)

    def next_attempt() -> ClaimedTask:
        with py.world.session() as session:
            scheduler_tick(session, datetime.now(UTC) + timedelta(hours=1), lambda: 0.5)
        return _claim(py.world)

    _commit_as_admin(py.world, "models.churn", b"two")
    delete(1)
    run_claimed(engine, next_attempt())
    assert [dict(t.input_versions) for t in py.runner.tasks] == [{"model": 1}, {"model": 2}]
    assert _task(py.world, "gold.scores").status is TaskStatus.PENDING

    delete(2)
    run_claimed(engine, next_attempt())
    task = _task(py.world, "gold.scores")
    assert (task.status, task.attempt) == (TaskStatus.FAILED, 3)
    assert task.error_message == "models.churn has no version yet; run the asset that produces it"
    assert len(py.runner.tasks) == 2


@pytest.mark.parametrize("recorded", ["table", "artifact"])
def test_an_input_that_is_no_valid_name_is_treated_as_one_the_principal_may_not_read(
    py: PyWorld, recorded: str
) -> None:
    """A malformed name answers as an unreadable one, and nothing raises."""
    py.deploy(
        entry={
            "inputs": {"x": "nodots", "clean": "silver.clean"},
            "input_kinds": {"x": recorded, "clean": "table"},
        }
    )
    engine = py.engine(lambda _t, _l, _s: RunOutcome(succeeded=False, error_message="boom"))
    claimed = py.claim_python(engine)
    run_claimed(engine, claimed)
    message = _attempts(py.world, claimed.task_run_id)[0].error_message
    if recorded == "artifact":
        assert message == (
            "nodots cannot be read by the run's principal 'admin', or does not exist; grant "
            "read on it or check the name"
        )
        assert py.runner.tasks == []
    else:
        assert message == "boom"
        (given,) = py.runner.tasks
        assert dict(given.input_kinds) == {"x": "table", "clean": "table"}


_TEAM_PACKAGE = (
    b'\n[[package]]\nname = "team-utils"\nversion = "1.4.0"\n'
    b'source = { registry = "https://pkgs.example.org/simple" }\n'
)


def test_a_lock_whose_index_is_no_longer_registered_fails_permanently_and_says_so(
    py: PyWorld,
) -> None:
    """Removing an index stops the next execution, with its own advice."""
    py.deploy(_lock() + _TEAM_PACKAGE)
    task, message = _fails(py, py.engine(_forged))
    assert (task.status, task.attempt) == (TaskStatus.FAILED, 1)
    assert task.failure_code == "build_failed"
    assert message == (
        "uv.lock: takes team-utils from the package index https://pkgs.example.org/simple, "
        "which is not registered on this instance. Ask an administrator to register it "
        "(`lakana index add NAME --url https://pkgs.example.org/simple`), or take the package "
        "from a registered index (`lakana index list`), then run `uv lock`. The package indexes "
        "registered on this instance no longer match this project version: register the index "
        "again (`lakana index list` shows what is registered) and start the run again, or change "
        "the project as the message says and deploy again."
    )
    assert py.leases == [] and py.tokens() == [] and py.runner.tasks == []


def test_an_index_declared_without_explicit_ends_with_what_the_author_must_do(
    py: PyWorld,
) -> None:
    """An index registered after the deploy; registering it again is not the remedy."""
    pyproject = _PYPROJECT + (
        b'\n[[tool.uv.index]]\nname = "team"\nurl = "https://pkgs.example.org/simple"\n'
    )
    py.deploy(code=_code(_lock(), pyproject=pyproject))
    with py.world.session() as session:
        session.add(
            PackageIndex(id=uuid.uuid4(), name="team", url="https://pkgs.example.org/simple")
        )
        session.commit()
    task, message = _fails(py, py.engine(_forged))
    assert task.status == TaskStatus.FAILED
    assert "needs `explicit = true`" in message
    assert message.endswith(
        ". A package index is registered on this instance that the project declares in a way "
        "that is not allowed: declare it as a `[[tool.uv.index]]` table with `explicit = true`, "
        "name its packages under `[tool.uv.sources]`, run `uv lock` and deploy again."
    )
    assert "register the index again" not in message


@pytest.mark.parametrize("count", [1, 3, 40])
def test_the_issues_at_execution_are_cut_at_an_issue_boundary(count: int) -> None:
    """Whole issues that fit, then `and N more`; never the middle of a command."""
    issues = [
        ProjectIssue(
            "uv.lock",
            None,
            f"takes pkg{n} from the package index https://pkgs{n}.example.org/simple, "
            f"which is not registered (`lakana index add NAME --url https://pkgs{n}.example.org/simple`)",
        )
        for n in range(count)
    ]
    listed = python_tasks_module._listed_issues(issues)
    assert len(listed) <= 1500
    whole = "; ".join(issue.render() for issue in issues)
    if len(whole) <= 1500:
        assert listed == whole
    else:
        head, _, tail = listed.rpartition("; and ")
        assert tail.endswith(" more") and head
        kept = int(tail.split()[0])
        assert head == "; ".join(issue.render() for issue in issues[: count - kept])
        assert all(issue.render() in head for issue in issues[: count - kept])


def test_both_kinds_of_issue_end_with_the_deploy_not_with_a_new_run() -> None:
    """After a declaration issue only a new deploy helps, so the mix says deploy."""
    declared = ProjectIssue("pyproject.toml", None, "a")
    locked = ProjectIssue("uv.lock", None, "b")
    closing = python_tasks_module._closing_for([declared, locked])
    assert closing == f"{python_tasks_module._DECLARED_AGAIN} {python_tasks_module._MIXED_ALSO}"
    assert "start the run again" not in closing
    assert python_tasks_module._closing_for([declared]) == python_tasks_module._DECLARED_AGAIN
    assert python_tasks_module._closing_for([locked]) == python_tasks_module._REGISTERED_AGAIN


def test_one_issue_longer_than_the_bound_is_cut_with_a_mark_never_lost() -> None:
    long = ProjectIssue("uv.lock", None, "x" * 2000)
    listed = python_tasks_module._listed_issues([long])
    assert len(listed) == 1500 and listed.startswith("uv.lock: xxx") and listed.endswith("…")
    two = python_tasks_module._listed_issues([long, long])
    assert len(two) <= 1500 and two.endswith("…; and 1 more")
    assert "problems" not in listed + two


def test_a_lock_whose_index_is_registered_reaches_the_environment(py: PyWorld) -> None:
    with py.world.session() as session:
        session.add(
            PackageIndex(id=uuid.uuid4(), name="team", url="https://pkgs.example.org/simple")
        )
        session.commit()
    py.deploy(_lock() + _TEAM_PACKAGE)
    engine = py.engine(_forged)
    run_claimed(engine, py.claim_python(engine))
    assert len(py.leases) == 1


def test_an_index_registered_later_under_a_name_the_project_uses_does_not_stop_it(
    py: PyWorld,
) -> None:
    """The name rule is a deploy rule: the build never depends on an index's name."""
    pyproject = _PYPROJECT + (
        b'\n[[tool.uv.index]]\nname = "lakana"\nurl = "wheelhouse"\nformat = "flat"\n'
        b"explicit = true\n"
        b'\n[[tool.uv.index]]\nname = "Team"\nurl = "https://mirror.example/simple"\n'
    )
    py.deploy(code=_code(_lock(), pyproject=pyproject))
    with py.world.session() as session:
        for name in ("lakana", "team"):
            session.add(
                PackageIndex(
                    id=uuid.uuid4(), name=name, url=f"https://{name}.pkgs.example.org/simple"
                )
            )
        session.commit()
    engine = py.engine(_forged)
    run_claimed(engine, py.claim_python(engine))
    assert len(py.leases) == 1


def test_the_operators_index_is_a_known_source_of_the_environment(py: PyWorld) -> None:
    py.deploy(_lock() + _TEAM_PACKAGE)
    venvs = VenvSettings(
        uv="uv",
        cache=py.root / "venvs",
        uv_cache=py.root / "uv",
        index_url="https://op:hunter2@pkgs.example.org/simple/",
    )
    engine = py.engine(_forged, venvs=venvs)
    run_claimed(engine, py.claim_python(engine))
    assert len(py.leases) == 1


def test_a_lock_that_is_incompatible_is_still_reported_with_its_own_advice(py: PyWorld) -> None:
    """The source check comes first, but a lock that passes it is still validated as a whole."""
    py.deploy(_lock("999.0.0"))
    _task_run, message = _fails(py, py.engine(_forged))
    assert message.endswith(
        "This project version was deployed for another platform version: update the lock and "
        "redeploy."
    )


_INDEX_KEY = EnvKeyProvider(base64.b64encode(b"0" * 32).decode())


_TEAM_INDEX_ID = uuid.UUID("00000000-0000-0000-0000-00000000a001")


def _register_team_index(py: PyWorld, *, password: str | None) -> None:
    """Register the index `team` and, with a password, its secret in the run's environment.

    Parameters
    ----------
    py
        The world.
    password
        The secret's password; `None` creates no secret.
    """
    with py.world.session() as session:
        session.add(
            PackageIndex(
                id=_TEAM_INDEX_ID,
                name="team",
                url="https://pkgs.example.org/simple",
                credential_secret="team-index",
            )
        )
        session.commit()
        if password is not None:
            owner = session.get(Principal, py.world.admin.id)
            assert owner is not None
            SecretService(session, AllowAllAuthorization(), _INDEX_KEY).create(
                PrincipalRef(id=owner.id, name=owner.name, type=owner.type),
                environment="dev",
                name="team-index",
                values={"username": "ci", "password": password},
                use=SecretUse(SecretUseKind.PACKAGE_INDEX, _TEAM_INDEX_ID, "team"),
            )


def _building(
    settings: VenvSettings,
    sha: str,
    project: Path,
    on_log: Callable[[bytes], None],
    indexes: IndexResolver | None = None,
    *,
    stop: Callable[[], bool] | None = None,
) -> NoReturn:
    """Stand in for a venv cache that must build: it asks for the credentials first.

    Parameters
    ----------
    settings
        Ignored.
    sha
        Ignored.
    project
        Ignored.
    on_log
        Ignored.
    indexes
        The resolver the runtime passed.
    stop
        Ignored.

    Raises
    ------
    VenvBuildRefused
        Always; its text says what was resolved.
    """
    accesses = list(indexes()) if indexes is not None else []
    raise VenvBuildRefused(
        "environment build failed: resolved "
        + ", ".join(f"{a.name}/{a.environment}/{a.secret}/{a.username}" for a in accesses)
    )


def _with_key(engine: RunEngine) -> RunEngine:
    """Give an engine the key that the test's secrets were stored under.

    Parameters
    ----------
    engine
        The engine.

    Returns
    -------
    RunEngine
        The same engine.
    """
    engine.key_provider = _INDEX_KEY
    return engine


def test_a_lock_without_a_registered_index_gets_no_resolver(py: PyWorld) -> None:
    py.deploy()
    engine = py.engine(_forged)
    run_claimed(engine, py.claim_python(engine))
    assert py.resolvers == [None]


def test_the_resolver_reads_the_secret_of_the_runs_environment_when_a_build_asks(
    py: PyWorld,
) -> None:
    _register_team_index(py, password="the-password")
    py.deploy(_lock() + _TEAM_PACKAGE)
    task, message = _fails(py, _with_key(py.engine(_forged, acquire_venv=_building)))
    assert message == "environment build failed: resolved team/dev/team-index/ci"
    assert (task.status, task.attempt) == (TaskStatus.FAILED, 1)
    assert "the-password" not in message


def test_an_index_whose_stored_url_is_not_valid_fails_the_next_build_for_good(py: PyWorld) -> None:
    """The cause is the stored row, so the message does not ask for a credential."""
    with py.world.session() as session:
        session.add(
            PackageIndex(
                id=uuid.uuid4(),
                name="testpypi",
                url="https://pypi.org/simple",
                credential_secret="team-index",
            )
        )
        session.commit()
    py.deploy(_lock() + _TEAM_PACKAGE.replace(b"pkgs.example.org", b"pypi.org"))
    task, message = _fails(py, _with_key(py.engine(_forged, acquire_venv=_building)))
    assert (task.status, task.attempt) == (TaskStatus.FAILED, 1)
    assert message == (
        "environment build failed: the stored URL of the package index 'testpypi' is not valid "
        "any more, so no credential is sent to it. An administrator registers it again with "
        "`lakana index update testpypi --url URL`"
    )
    assert py.tokens() == [] and py.runner.tasks == []


def test_a_testpypi_row_with_a_secret_is_told_to_clear_the_secret(py: PyWorld) -> None:
    """The stored URL is fine; the credential is what the host cannot take."""
    with py.world.session() as session:
        session.add(
            PackageIndex(
                id=uuid.uuid4(),
                name="testpypi",
                url="https://test.pypi.org/simple",
                credential_secret="team-index",
            )
        )
        session.commit()
    py.deploy(_lock() + _TEAM_PACKAGE.replace(b"pkgs.example.org", b"test.pypi.org"))
    task, message = _fails(py, _with_key(py.engine(_forged, acquire_venv=_building)))
    assert (task.status, task.attempt) == (TaskStatus.FAILED, 1)
    assert message == (
        "environment build failed: the package index 'testpypi' has a credential, but its host "
        "takes none, so no credential is sent to it. An administrator removes it with "
        "`lakana index update testpypi --clear credential_secret`"
    )


def test_an_unbound_index_secret_fails_the_build_for_good_with_the_bind_command(
    py: PyWorld,
) -> None:
    """The secret exists and holds a password, but no one bound it to the index."""
    _register_team_index(py, password="the-password")
    with py.world.session() as session:
        secret = session.execute(select(Secret).where(Secret.name == "team-index")).scalar_one()
        secret.use_kind, secret.use_target_id = None, None
        session.commit()
    py.deploy(_lock() + _TEAM_PACKAGE)
    task, message = _fails(py, _with_key(py.engine(_forged, acquire_venv=_building)))
    assert (task.status, task.attempt) == (TaskStatus.FAILED, 1)
    assert task.failure_code == "secret_refused"
    assert message == (
        "environment build failed: Secret 'team-index' of environment 'dev' is not bound to a "
        "use yet, so Lakana does not send it to package index 'team'. An administrator binds "
        "it: lakana secret bind team-index --env dev --index team"
    )
    assert "the-password" not in message
    assert py.tokens() == [] and py.runner.tasks == []


def test_a_removed_secret_fails_the_next_build_for_good(py: PyWorld) -> None:
    """The same message as at deploy, and no retry."""
    _register_team_index(py, password=None)
    py.deploy(_lock() + _TEAM_PACKAGE)
    task, message = _fails(py, _with_key(py.engine(_forged, acquire_venv=_building)))
    assert (task.status, task.attempt) == (TaskStatus.FAILED, 1)
    assert task.failure_code == "secret_refused"
    assert message == (
        "environment build failed: the package index 'team' needs the secret 'team-index' in "
        "environment 'dev', which does not exist. Create it with `lakana secret create "
        'team-index --env dev --index team --values-stdin` (a JSON object with "username" '
        'and "password")'
    )
    assert py.tokens() == [] and py.runner.tasks == []
    assert list((py.root / "work").iterdir()) == []


_UV_SCRIPT = """#!{python}
import os, sys
from pathlib import Path
with open(Path(__file__).parent / "calls", "a") as handle:
    handle.write(os.environ.get("UV_INDEX", "-") + "\\n")
venv = Path(os.environ["UV_PROJECT_ENVIRONMENT"])
(venv / "bin").mkdir(parents=True)
os.symlink(sys.executable, venv / "bin" / "python")
"""


def test_a_cached_environment_runs_the_task_without_reading_the_secret(py: PyWorld) -> None:
    """Review R5.5: the real `acquire` and a resolver that records its calls."""
    _register_team_index(py, password="the-password")
    py.deploy(_lock() + _TEAM_PACKAGE)
    uv = py.root / "fake-uv" / "uv"
    uv.parent.mkdir()
    uv.write_text(_UV_SCRIPT.format(python=sys.executable))
    uv.chmod(0o755)
    settings = VenvSettings(uv=str(uv), cache=py.root / "venvs", uv_cache=py.root / "uv-cache")
    reads: list[str] = []

    def real_acquire(
        venv_settings: VenvSettings,
        sha: str,
        project: Path,
        on_log: Callable[[bytes], None],
        indexes: IndexResolver | None = None,
        *,
        stop: Callable[[], bool] | None = None,
    ) -> VenvLease:
        assert indexes is not None

        def recording() -> list[IndexAccess]:
            """Record that the secret was read.

            Returns
            -------
            list of IndexAccess
                What the real resolver found.
            """
            reads.append("read")
            return list(indexes())

        return acquire(venv_settings, sha, project, on_log, recording)

    engine = _with_key(py.engine(_forged, venvs=settings, acquire_venv=real_acquire))
    claimed = py.claim_python(engine)
    keeper = LeaseKeeper(
        py.world.session,
        claimed.task_run_id,
        "w1",
        claimed.claim,
        lease_seconds=60,
        renew_seconds=60,
    )
    assert engine.python is not None
    engine.python.attempt(engine, claimed, keeper, "dev", None)
    assert reads == ["read"]
    (build,) = (uv.parent / "calls").read_text().splitlines()
    assert build.startswith("lakana-registered-0=https://ci:the-password@pkgs.example.org/")
    engine.python.attempt(engine, claimed, keeper, "dev", None)
    assert reads == ["read"]
    assert len((uv.parent / "calls").read_text().splitlines()) == 1
    assert len(py.runner.tasks) == 2


def test_the_task_process_is_given_nothing_of_the_credential(py: PyWorld) -> None:
    """Review R5.5: look at what the real runner started: environment, argv and stdin."""
    _register_team_index(py, password="the-password")
    py.deploy(_lock() + _TEAM_PACKAGE)
    seen: list[bytes] = []
    process = RealProcess(
        py,
        "print('SEEN', json.dumps({'env': dict(os.environ), 'stdin': description,"
        " 'argv': sys.argv}))\n"
        "write({'status': 'failed', 'error_type': 'ValueError', 'message': 'x',"
        " 'retryable': False, 'phase': 'validate', 'conflict': False})\n"
        "sys.exit(1)\n",
        on_output=seen.append,
    )
    resolved: list[list[IndexAccess]] = []

    def resolving(
        settings: VenvSettings,
        sha: str,
        project: Path,
        on_log: Callable[[bytes], None],
        indexes: IndexResolver | None = None,
        *,
        stop: Callable[[], bool] | None = None,
    ) -> VenvLease:
        assert indexes is not None
        resolved.append(list(indexes()))
        return process.acquire(settings, sha, project, on_log)

    engine = _with_key(py.engine(_forged, runner=process, acquire_venv=resolving))
    run_claimed(engine, py.claim_python(engine))
    ((access,),) = resolved
    assert (access.username, access.password) == ("ci", "the-password")
    line = next(
        line for line in b"".join(seen).decode().splitlines() if line.lstrip().startswith("SEEN ")
    )
    child = json.loads(line.split("SEEN ", 1)[1])
    assert set(child["env"]) >= {"PATH", "HOME", "TMPDIR"}
    assert not [name for name in child["env"] if name.startswith("UV")]
    (task,) = process.tasks
    everything = json.dumps(child) + repr(dataclasses.asdict(task)) + repr(task)
    for hidden in ("the-password", "team-index", "pkgs.example.org", "lakana-registered"):
        assert hidden not in everything


_COPYING_UV_SCRIPT = """#!{python}
import os, shutil, sys
from pathlib import Path
with open(Path(__file__).parent / "calls", "a") as handle:
    handle.write(os.environ.get("UV_INDEX", "-") + "\\n")
shutil.copytree({source!r}, os.environ["UV_PROJECT_ENVIRONMENT"], symlinks=True)
"""


def test_one_attempt_builds_with_the_credential_and_starts_a_child_that_has_none(
    py: PyWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real build and the real child of one attempt: nothing of the build reaches the child."""
    monkeypatch.delenv("UV_INDEX", raising=False)
    _register_team_index(py, password="the-password")
    py.deploy(_lock() + _TEAM_PACKAGE)
    seen: list[bytes] = []
    process = RealProcess(
        py,
        "print('SEEN', json.dumps({'env': dict(os.environ), 'stdin': description,"
        " 'argv': sys.argv}))\n"
        "write({'status': 'failed', 'error_type': 'ValueError', 'message': 'x',"
        " 'retryable': False, 'phase': 'validate', 'conflict': False})\n"
        "sys.exit(1)\n",
        on_output=seen.append,
    )
    uv = py.root / "copying-uv" / "uv"
    uv.parent.mkdir()
    uv.write_text(_COPYING_UV_SCRIPT.format(python=sys.executable, source=str(process.venv)))
    uv.chmod(0o755)
    settings = VenvSettings(uv=str(uv), cache=py.root / "built", uv_cache=py.root / "uv-cache")
    engine = _with_key(py.engine(_forged, runner=process, venvs=settings, acquire_venv=acquire))
    run_claimed(engine, py.claim_python(engine))
    (build,) = (uv.parent / "calls").read_text().splitlines()
    assert build.startswith("lakana-registered-0=https://ci:the-password@pkgs.example.org/")
    assert "UV_INDEX" not in os.environ
    line = next(
        line for line in b"".join(seen).decode().splitlines() if line.lstrip().startswith("SEEN ")
    )
    child = json.loads(line.split("SEEN ", 1)[1])
    assert set(child["env"]) >= {"PATH", "HOME", "TMPDIR"}
    assert not [name for name in child["env"] if name.startswith("UV")]
    (task,) = process.tasks
    assert task.venv.parent == py.root / "built"
    everything = json.dumps(child) + repr(dataclasses.asdict(task))
    for hidden in ("the-password", "team-index", "pkgs.example.org", "lakana-registered"):
        assert hidden not in everything


def test_the_resolver_refuses_when_the_environment_is_gone(py: PyWorld) -> None:
    runtime = py.runtime(_forged)
    with py.world.session() as session:
        resolver = runtime._index_resolver(
            session,
            py.engine(_forged),
            "no-such-environment",
            (
                RegisteredIndex(
                    "team", "https://pkgs.example.org/simple", "team-index", _TEAM_INDEX_ID
                ),
            ),
        )
        assert resolver is not None
        with pytest.raises(VenvBuildRefused, match="the environment no longer exists"):
            resolver()
        assert runtime._index_resolver(session, py.engine(_forged), "dev", ()) is None


# -- a Python table asset with checks ------------------------------------------------


def _with_checks(py: PyWorld, *checks: tuple[str, str]) -> None:
    """Add checks of `gold.scores` to the deployed manifest.

    Parameters
    ----------
    py
        The world.
    *checks
        Name and SELECT of each blocking check.
    """
    with py.world.session() as session:
        version = session.get(JobVersion, py.world.version_id)
        assert version is not None
        version.manifest_json = {
            **version.manifest_json,
            "checks": [
                {
                    "name": f"gold.scores/{name}",
                    "table": "gold.scores",
                    "check": name,
                    "path": f"checks/gold/scores/{name}.sql",
                    "on_failure": "block",
                    "description": None,
                    "sql": sql,
                    "references": ["gold.scores"],
                }
                for name, sql in checks
            ],
        }
        session.commit()


_NO_ROWS = ("no_rows", "SELECT id FROM gold.scores")
"""A check that fails as soon as the table has a row."""
_NO_NULLS = ("no_nulls", "SELECT id FROM gold.scores WHERE id IS NULL")
"""A check that passes for every table the test scripts write."""


def _marked_by(py: PyWorld) -> uuid.UUID | None:
    """Read the run `gold.scores` is marked not published by.

    Parameters
    ----------
    py
        The world.

    Returns
    -------
    uuid.UUID or None
        `Table.unpublished_run_id`.
    """
    with py.world.session() as session:
        return session.execute(
            select(Table.unpublished_run_id).where(Table.name == "scores")
        ).scalar_one()


def _stamps(monkeypatch: pytest.MonkeyPatch) -> list[tuple[uuid.UUID, str, int, Written]]:
    """Record every call of `mark_written` by the task runner, and let it through.

    Parameters
    ----------
    monkeypatch
        pytest's patcher.

    Returns
    -------
    list of tuple
        The task run, worker, claim and record of each call, filled as they come.
    """
    calls: list[tuple[uuid.UUID, str, int, Written]] = []
    real = task_checks_module.mark_written

    def recording(
        session: Session, task_run_id: uuid.UUID, worker: str, claim: int, written: Written
    ) -> bool:
        calls.append((task_run_id, worker, claim, written))
        return real(session, task_run_id, worker, claim, written)

    monkeypatch.setattr(task_checks_module, "mark_written", recording)
    return calls


def test_a_python_table_asset_is_stamped_and_checked(
    py: PyWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec section 5, step 4: the worker records what it read itself, then runs the checks."""
    _with_checks(py, _NO_ROWS)
    stamps = _stamps(monkeypatch)
    engine = py.engine(_writer(py.world, rows=2))
    claimed = py.claim_python(engine)
    assert claimed.has_checks

    run_claimed(engine, claimed)

    ((task_run_id, worker, claim, written),) = stamps
    assert (task_run_id, worker, claim) == (claimed.task_run_id, "w1", claimed.claim)
    with py.world.session() as session:
        table_id = session.execute(select(Table.id).where(Table.name == "scores")).scalar_one()
        (result,) = results_of_task(session, claimed.task_run_id)
        assert (result.outcome, result.failing_rows, result.rollback_outcome) == (
            CheckOutcome.FAILED,
            2,
            RollbackOutcome.UNPUBLISHED,
        )
        assert result.checked_snapshot_id == written.snapshot_after
    assert written.table_id == table_id
    assert (written.snapshot_before, written.publishes) == (None, True)
    assert written.snapshot_after is not None
    task = _task(py.world, "gold.scores")
    assert (task.status, task.attempt) == (TaskStatus.FAILED, 1)
    assert task.error_message == (
        "Check `gold.scores/no_rows` failed: 2 rows (checks/gold/scores/no_rows.sql). The table "
        "is not published: it had no published data before this run. Nothing downstream was "
        "built. Look at the rows: "
        f"`lakana check rows {claimed.run_id} gold.scores/no_rows --env dev`. "
        "After a fix, deploy and run again; if the project did not change: "
        f"`lakana runs resume {claimed.run_id} --env dev`."
    )
    assert _marked_by(py) == claimed.run_id
    assert len(py.runner.tasks) == 1
    log = _log(py.world, claimed.run_id, "gold.scores")
    assert "  check no_rows: failed, 2 rows\n  rollback: unpublished\n" in log
    assert "hello from the task" in log


def test_a_python_asset_that_replaced_a_table_is_set_back_when_its_check_fails(
    py: PyWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stamp says the table existed: the rollback gives back the rows of the run before."""
    engine = py.engine(_writer(py.world, rows=1))
    run_claimed(engine, py.claim_python(engine))
    with py.world.session() as session:
        scheduler_tick(session, datetime.now(UTC), lambda: 0.5)
    _with_checks(py, _NO_ROWS)
    stamps = _stamps(monkeypatch)
    engine = py.engine(_writer(py.world, rows=3))
    claimed = py.claim_python(engine)

    run_claimed(engine, claimed)

    ((_, _, _, written),) = stamps
    assert written.publishes is False
    assert written.snapshot_before is not None
    assert written.snapshot_after not in {None, written.snapshot_before}
    with py.world.session() as session:
        (result,) = results_of_task(session, claimed.task_run_id)
        assert (result.failing_rows, result.rollback_outcome) == (3, RollbackOutcome.ROLLED_BACK)
        loaded = CatalogService(
            session, StorageResolver(session), AllowAllAuthorization()
        ).load_table(py.world.admin, TableRef.from_dotted("dev", "gold.scores"))
        assert loaded.metadata.current_snapshot_id == written.snapshot_before
    assert _task(py.world, "gold.scores").status is TaskStatus.FAILED
    assert _marked_by(py) is None


@pytest.mark.parametrize("passes", [True, False])
@pytest.mark.slow
def test_a_python_asset_that_replaced_a_not_published_table_publishes_or_not_by_its_checks(
    py: PyWorld, monkeypatch: pytest.MonkeyPatch, passes: bool
) -> None:
    """Finding 3: a write that found the table not published publishes it, or leaves it so."""
    _with_checks(py, _NO_ROWS)
    engine = py.engine(_writer(py.world, rows=1))
    first = py.claim_python(engine)
    run_claimed(engine, first)
    assert _marked_by(py) == first.run_id
    with py.world.session() as session:
        scheduler_tick(session, datetime.now(UTC), lambda: 0.5)
    _with_checks(py, _NO_NULLS if passes else _NO_ROWS)
    stamps = _stamps(monkeypatch)
    engine = py.engine(_writer(py.world, rows=2))
    claimed = py.claim_python(engine)
    assert claimed.run_id != first.run_id

    run_claimed(engine, claimed)

    ((_, _, _, written),) = stamps
    assert (written.snapshot_before, written.publishes) == (None, True)
    task = _task(py.world, "gold.scores")
    if passes:
        assert (task.status, task.error_message) == (TaskStatus.SUCCEEDED, None)
        assert _marked_by(py) is None
    else:
        assert task.status is TaskStatus.FAILED
        assert _marked_by(py) == claimed.run_id
        assert task.error_message is not None
        assert "The table is not published: it had no published data before this run." in (
            task.error_message
        )


def test_a_python_asset_whose_checks_pass_succeeds_and_keeps_its_log(
    py: PyWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_checks(py, _NO_NULLS)
    engine = py.engine(_writer(py.world, rows=2))
    claimed = py.claim_python(engine)

    run_claimed(engine, claimed)

    task = _task(py.world, "gold.scores")
    assert (task.status, task.error_message) == (TaskStatus.SUCCEEDED, None)
    attempt = _attempts(py.world, claimed.task_run_id)[0]
    assert (attempt.outcome, attempt.rows_written) == (AttemptOutcome.SUCCEEDED, 2)
    log = _log(py.world, claimed.run_id, "gold.scores")
    assert "  snapshot: " in log and "  check no_nulls: passed\n" in log
    assert "hello from the task" in log
    assert task.written_table_id is not None


def test_a_python_task_whose_stamp_is_refused_records_nothing(
    py: PyWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The claim moved on between the end of the process and the stamp: no check, no outcome."""
    _with_checks(py, _NO_ROWS)
    engine = py.engine(_writer(py.world, rows=2))
    claimed = py.claim_python(engine)
    monkeypatch.setattr(task_checks_module, "mark_written", lambda *args: False)
    monkeypatch.setattr(
        task_checks_module.checks,
        "count_check",
        lambda *a, **k: pytest.fail("checked without a claim"),
    )

    run_claimed(engine, claimed)

    task = _task(py.world, "gold.scores")
    assert (task.status, task.error_message, task.written_table_id) == (
        TaskStatus.RUNNING,
        None,
        None,
    )
    with py.world.session() as session:
        assert results_of_task(session, claimed.task_run_id) == []


def test_a_python_task_without_checks_is_not_stamped(
    py: PyWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No checks: the outcome carries what was written, and nobody acts on it."""
    monkeypatch.setattr(task_checks_module, "mark_written", lambda *a: pytest.fail("stamped"))
    monkeypatch.setattr(tasks_module, "check_phase", lambda *a: pytest.fail("a phase ran"))
    engine = py.engine(_writer(py.world, rows=2))
    claimed = py.claim_python(engine)
    assert not claimed.has_checks

    run_claimed(engine, claimed)

    task = _task(py.world, "gold.scores")
    assert (task.status, task.written_table_id) == (TaskStatus.SUCCEEDED, None)


def test_a_python_rebuild_without_checks_clears_the_note_of_the_table(py: PyWorld) -> None:
    """Nothing is left that could fail the data, so the note goes with the rebuild."""
    engine = py.engine(_writer(py.world, rows=1))
    first = py.claim_python(engine)
    run_claimed(engine, first)
    with py.world.session() as session:
        session.execute(
            update(Table)
            .where(Table.name == "scores")
            .values(kept_run_id=uuid.uuid4(), kept_task_run_id=uuid.uuid4(), kept_check="x")
        )
        session.commit()
        scheduler_tick(session, datetime.now(UTC), lambda: 0.5)
    engine = py.engine(_writer(py.world, rows=2))
    second = py.claim_python(engine)
    assert second.run_id != first.run_id and not second.has_checks

    run_claimed(engine, second)

    assert _task(py.world, "gold.scores").status is TaskStatus.SUCCEEDED
    with py.world.session() as session:
        assert session.execute(
            select(Table.kept_run_id, Table.kept_check).where(Table.name == "scores")
        ).one() == (None, None)
        (event,) = [
            e.detail
            for e in session.scalars(
                select(AuditEvent).where(AuditEvent.event_type == AuditEventType.TABLE_KEPT_CLEARED)
            )
        ]
    assert event["why"] == "rebuilt_without_checks"
    assert event["run_id"] == str(second.run_id)


def test_an_artifact_asset_has_no_check_phase(py: PyWorld, monkeypatch: pytest.MonkeyPatch) -> None:
    """An artifact has no table to check: its outcome carries no record of a write."""
    py.deploy(table="models.churn", entry=_ARTIFACT_ENTRY)
    monkeypatch.setattr(tasks_module, "check_phase", lambda *a: pytest.fail("a phase ran"))
    outcomes: list[TaskOutcome | None] = []
    real = tasks_module._dispatch

    def recording(*args: object, **kwargs: object) -> TaskOutcome | None:
        outcomes.append(real(*args, **kwargs))  # ty: ignore[invalid-argument-type]
        return outcomes[-1]

    engine = py.engine(_producer(py.world))
    claimed = py.claim_python(engine)
    monkeypatch.setattr(tasks_module, "_dispatch", recording)

    run_claimed(engine, claimed)

    (outcome,) = outcomes
    assert outcome is not None
    assert (outcome.kind, outcome.written, outcome.artifact_version) == (None, None, 1)
    assert not claimed.has_checks
    assert _task(py.world, "models.churn").status is TaskStatus.SUCCEEDED


def test_the_outcome_of_a_python_table_attempt_carries_what_it_wrote(
    py: PyWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    outcomes: list[TaskOutcome | None] = []
    real = tasks_module._dispatch

    def recording(*args: object, **kwargs: object) -> TaskOutcome | None:
        outcomes.append(real(*args, **kwargs))  # ty: ignore[invalid-argument-type]
        return outcomes[-1]

    engine = py.engine(_writer(py.world, rows=2))
    claimed = py.claim_python(engine)
    monkeypatch.setattr(tasks_module, "_dispatch", recording)

    run_claimed(engine, claimed)

    (outcome,) = outcomes
    assert outcome is not None and outcome.written is not None
    with py.world.session() as session:
        table_id = session.execute(select(Table.id).where(Table.name == "scores")).scalar_one()
    assert outcome.written == Written(table_id, None, outcome.snapshot_id, publishes=True)


def test_the_target_state_compares_identity_and_snapshot_only() -> None:
    """A marker set by someone's rollback between two reads is not a write of this task."""
    from lakana_worker.python_tasks import _TargetState

    table_id = uuid.uuid4()
    assert _TargetState(table_id, 5, unpublished=True) == _TargetState(table_id, 5)
    assert _TargetState(table_id, None, unpublished=True) == _TargetState(table_id, None)
    assert _TargetState(table_id, 5) != _TargetState(table_id, 6)
    assert _TargetState(None, None).unpublished is False


# -- the baseline of a Python asset with checks ----------------------

_SET_BACK = (
    "The table was set back to what it was before this run, and nothing downstream was built."
)
_NOT_PUBLISHED = (
    "The table is not published: it had no published data before this run. Nothing downstream "
    "was built."
)
_NOT_SET_BACK = (
    "The table could not be set back; it may hold data of this run that was not checked."
)
_LEFT_UNCHECKED = (
    "The table was left as it is, because another writer changed it while this run was working "
    "on it; it may hold data of this run that was not checked. Nothing downstream was built."
)
_LEFT_FAILING = (
    "The table was left as it is, because another writer changed it while this run was working "
    "on it; it may hold data of this run that failed its checks. Nothing downstream was built."
)


def _writes_then_fails(world: World, rows: int, *, retryable: bool = True) -> Script:
    """Build a script that replaces the table and then fails, as a task that raises after it.

    Parameters
    ----------
    world
        The world.
    rows
        How many rows it writes before it fails.
    retryable
        Whether the failure may be retried.

    Returns
    -------
    Script
        Commits to the table, then reports a failure.
    """

    def script(task: PythonTask, on_log: Callable[[bytes], None], stop: StopSignal) -> RunOutcome:
        _upload(world, task, rows)
        return RunOutcome(
            succeeded=False, error_message="the task raised after its write", retryable=retryable
        )

    return script


def _next_attempt(py: PyWorld) -> ClaimedTask:
    """Tick and claim the Python task again.

    Parameters
    ----------
    py
        The world.

    Returns
    -------
    ClaimedTask
        The next claim of `gold.scores`.
    """
    with py.world.session() as session:
        scheduler_tick(session, datetime.now(UTC), lambda: 0.5)
    claimed = _claim(py.world)
    assert claimed.asset == "gold.scores"
    return claimed


def _published_before(py: PyWorld) -> None:
    """Build `gold.scores` with one row (id 0) in a run without checks.

    Parameters
    ----------
    py
        The world.
    """
    engine = py.engine(_writer(py.world, rows=1))
    run_claimed(engine, py.claim_python(engine))
    with py.world.session() as session:
        scheduler_tick(session, datetime.now(UTC), lambda: 0.5)
    assert _ids(py.world, "gold.scores") == [0]


def _rollbacks(py: PyWorld) -> list[dict[str, object]]:
    """Read the details of the rollback audit events.

    Parameters
    ----------
    py
        The world.

    Returns
    -------
    list of dict
        The details, oldest first.
    """
    with py.world.session() as session:
        return [
            dict(event.detail)
            for event in session.scalars(
                select(AuditEvent)
                .where(AuditEvent.event_type == AuditEventType.TABLE_ROLLED_BACK)
                .order_by(AuditEvent.occurred_at)
            )
        ]


def test_a_retry_after_an_attempt_that_wrote_is_set_back_to_before_the_first_attempt(
    py: PyWorld,
) -> None:
    """I1: attempt 1 wrote and failed; attempt 2's failed check must not leave attempt 1's rows."""
    _published_before(py)
    _with_checks(py, _NO_ROWS)
    engine = py.engine(_writes_then_fails(py.world, rows=2))
    first = py.claim_python(engine)
    run_claimed(engine, first)
    pending = _task(py.world, "gold.scores")
    assert (pending.status, pending.attempt) == (TaskStatus.PENDING, 1)
    assert _ids(py.world, "gold.scores") == [0, 1]  # unchecked, until the next attempt
    with py.world.session() as session:
        baseline = read_baseline(session, first.task_run_id)
    assert baseline is not None and baseline.publishes is False

    engine = py.engine(_writer(py.world, rows=3))
    second = _next_attempt(py)
    run_claimed(engine, second)

    task = _task(py.world, "gold.scores")
    assert (task.status, task.attempt) == (TaskStatus.FAILED, 2)
    assert task.error_message == (
        "Check `gold.scores/no_rows` failed: 3 rows (checks/gold/scores/no_rows.sql). "
        f"{_SET_BACK} Look at the rows: "
        f"`lakana check rows {second.run_id} gold.scores/no_rows --env dev`. "
        "After a fix, deploy and run again; if the project did not change: "
        f"`lakana runs resume {second.run_id} --env dev`."
    )
    assert _ids(py.world, "gold.scores") == [0]
    with py.world.session() as session:
        assert read_baseline(session, first.task_run_id) == baseline
    (rolled,) = _rollbacks(py)
    assert (rolled["outcome"], rolled["before"]) == ("rolled_back", baseline.snapshot_id)


def test_a_retry_after_an_attempt_that_created_the_table_ends_not_published(py: PyWorld) -> None:
    """I1: the table did not exist before the task run, whatever attempt 2 finds."""
    _with_checks(py, _NO_ROWS)
    engine = py.engine(_writes_then_fails(py.world, rows=2))
    first = py.claim_python(engine)
    run_claimed(engine, first)
    assert _ids(py.world, "gold.scores") == [0, 1]
    with py.world.session() as session:
        assert read_baseline(session, first.task_run_id) == Baseline(None, True)

    engine = py.engine(_writer(py.world, rows=3))
    second = _next_attempt(py)
    run_claimed(engine, second)

    task = _task(py.world, "gold.scores")
    assert task.status is TaskStatus.FAILED
    assert task.error_message is not None and _NOT_PUBLISHED in task.error_message
    assert _marked_by(py) == second.run_id
    (rolled,) = _rollbacks(py)
    assert (rolled["outcome"], rolled["before"]) == ("unpublished", None)


def test_an_attempt_lost_after_its_write_is_followed_by_one_that_is_checked_and_published(
    py: PyWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """I1: the worker died before the stamp; the retry replaces, passes and publishes."""
    _with_checks(py, _NO_NULLS)
    engine = py.engine(_writer(py.world, rows=2))
    first = py.claim_python(engine)
    real = tasks_module.check_phase

    def die(*args: object) -> TaskOutcome | None:
        raise SystemExit("the worker died after the process and before the stamp")

    monkeypatch.setattr(tasks_module, "check_phase", die)
    with pytest.raises(SystemExit):
        run_claimed(engine, first)
    monkeypatch.setattr(tasks_module, "check_phase", real)
    lost = _task(py.world, "gold.scores")
    assert (lost.status, lost.written_table_id) == (TaskStatus.RUNNING, None)
    _expire_lease_and_tick(py.world, "gold.scores")

    stamps = _stamps(monkeypatch)
    engine = py.engine(_writer(py.world, rows=3))
    second = _claim(py.world)
    run_claimed(engine, second)

    ((_, _, _, written),) = stamps
    assert (written.snapshot_before, written.publishes) == (None, True)  # the baseline
    task = _task(py.world, "gold.scores")
    assert (task.status, task.error_message) == (TaskStatus.SUCCEEDED, None)
    assert _ids(py.world, "gold.scores") == [0, 1, 2]
    assert _marked_by(py) is None
    assert _rollbacks(py) == []


@pytest.mark.parametrize("existed", [True, False])
def test_a_process_that_commits_and_then_fails_for_good_is_set_back_to_the_baseline(
    py: PyWorld, existed: bool
) -> None:
    """I1: the failure ends the task, so the unchecked write does not stay published."""
    if existed:
        _published_before(py)
    _with_checks(py, _NO_NULLS)
    engine = py.engine(_writes_then_fails(py.world, rows=2, retryable=False))
    claimed = py.claim_python(engine)

    run_claimed(engine, claimed)

    task = _task(py.world, "gold.scores")
    assert (task.status, task.attempt) == (TaskStatus.FAILED, 1)
    assert task.error_message == (
        f"the task raised after its write. {_SET_BACK if existed else _NOT_PUBLISHED}"
    )
    if existed:
        assert _ids(py.world, "gold.scores") == [0]
        assert _marked_by(py) is None
    else:
        assert _marked_by(py) == claimed.run_id
    (rolled,) = _rollbacks(py)
    assert rolled["outcome"] == ("rolled_back" if existed else "unpublished")
    assert rolled["checks"] == []
    with py.world.session() as session:
        assert results_of_task(session, claimed.task_run_id) == []
    log = _log(py.world, claimed.run_id, "gold.scores")
    assert f"  rollback: {rolled['outcome']}\n" in log


def test_a_retryable_failure_on_the_last_attempt_is_set_back_too(py: PyWorld) -> None:
    """I1: no attempt is left, so this failure ends the task."""
    _published_before(py)
    py.deploy(retries=0)
    _with_checks(py, _NO_NULLS)
    engine = py.engine(_writes_then_fails(py.world, rows=2))
    claimed = py.claim_python(engine)

    run_claimed(engine, claimed)

    task = _task(py.world, "gold.scores")
    assert (task.status, task.attempt, task.max_attempts) == (TaskStatus.FAILED, 1, 1)
    assert task.error_message == f"the task raised after its write. {_SET_BACK}"
    assert _ids(py.world, "gold.scores") == [0]


def test_a_retryable_failure_with_an_attempt_left_sets_nothing_back(
    py: PyWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The next attempt replaces the table; until then it holds unchecked data."""
    _published_before(py)
    _with_checks(py, _NO_NULLS)
    monkeypatch.setattr(
        CatalogService, "roll_back", lambda *a, **k: pytest.fail("set back before the last attempt")
    )
    engine = py.engine(_writes_then_fails(py.world, rows=2))
    claimed = py.claim_python(engine)

    run_claimed(engine, claimed)

    task = _task(py.world, "gold.scores")
    assert (task.status, task.error_message) == (
        TaskStatus.PENDING,
        "the task raised after its write",
    )
    assert _ids(py.world, "gold.scores") == [0, 1]


def test_a_failure_that_ends_the_task_without_a_write_sets_nothing_back(
    py: PyWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The table is at the baseline: there is nothing to undo, and the message stays the build's."""
    _published_before(py)
    _with_checks(py, _NO_NULLS)
    monkeypatch.setattr(
        CatalogService, "roll_back", lambda *a, **k: pytest.fail("set back an untouched table")
    )

    def script(task: PythonTask, on_log: Callable[[bytes], None], stop: StopSignal) -> RunOutcome:
        return RunOutcome(succeeded=False, error_message="it never wrote", retryable=False)

    engine = py.engine(script)
    claimed = py.claim_python(engine)

    run_claimed(engine, claimed)

    task = _task(py.world, "gold.scores")
    assert (task.status, task.error_message) == (TaskStatus.FAILED, "it never wrote")
    assert _ids(py.world, "gold.scores") == [0]


def test_a_cancel_after_a_write_that_then_failed_hands_the_task_back_to_be_set_back(
    py: PyWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worker that saw the cancel does not end the task over the rows.

    The task goes back ready with `setting_back`; the next worker sets the table back and ends it
    cancelled.
    """
    _published_before(py)
    _with_checks(py, _NO_NULLS)

    def script(task: PythonTask, on_log: Callable[[bytes], None], stop: StopSignal) -> RunOutcome:
        _upload(py.world, task, 2)
        py.cancel(task_run_ids[0])
        return RunOutcome(succeeded=False, error_message="boom", retryable=False)

    engine = py.engine(script)
    claimed = py.claim_python(engine)
    task_run_ids = [claimed.run_id]

    run_claimed(engine, claimed)

    _is_handed_back_then_set_back_and_cancelled(py, monkeypatch)


def _is_handed_back_then_set_back_and_cancelled(
    py: PyWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Check that the task waits for a set-back claim, which sets the table back and cancels.

    Parameters
    ----------
    py
        The world, with a task that wrote two rows and saw the cancel.
    monkeypatch
        pytest's patcher.
    """
    handed = _task(py.world, "gold.scores")
    assert (handed.status, handed.setting_back) == (TaskStatus.READY, True)
    assert _ids(py.world, "gold.scores") == [0, 1]
    run_claimed(_builds_nothing(py, monkeypatch), _claim(py.world))
    done = _task(py.world, "gold.scores")
    assert (done.status, done.error_message) == (
        TaskStatus.CANCELLED,
        f"{RUN_CANCELLED_MESSAGE} {SET_BACK_DONE}",
    )
    assert _ids(py.world, "gold.scores") == [0]


def test_a_refused_rollback_of_a_failed_build_says_that_it_changed_nothing(
    py: PyWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    _published_before(py)
    _with_checks(py, _NO_NULLS)

    def refuse(self: CatalogService, *args: object, **kwargs: object) -> object:
        raise PermissionDenied("x")

    monkeypatch.setattr(CatalogService, "roll_back", refuse)
    engine = py.engine(_writes_then_fails(py.world, rows=2, retryable=False))
    claimed = py.claim_python(engine)

    run_claimed(engine, claimed)

    task = _task(py.world, "gold.scores")
    assert (task.status, task.error_message) == (
        TaskStatus.FAILED,
        "the task raised after its write. The rollback was refused (permission denied: x); it "
        "did not change the table. Nothing downstream was built.",
    )
    assert _ids(py.world, "gold.scores") == [0, 1]


def test_the_baseline_of_a_task_without_checks_is_never_recorded(
    py: PyWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        python_tasks_module, "record_baseline", lambda *a: pytest.fail("a baseline was recorded")
    )
    monkeypatch.setattr(
        CatalogService, "roll_back", lambda *a, **k: pytest.fail("set back without checks")
    )
    engine = py.engine(_writes_then_fails(py.world, rows=2, retryable=False))
    claimed = py.claim_python(engine)
    assert not claimed.has_checks

    run_claimed(engine, claimed)

    task = _task(py.world, "gold.scores")
    assert (task.status, task.error_message) == (
        TaskStatus.FAILED,
        "the task raised after its write",
    )
    assert (task.baseline_snapshot_id, task.baseline_publishes) == (None, None)
    assert _ids(py.world, "gold.scores") == [0, 1]


def test_a_claim_that_is_gone_records_no_baseline_and_starts_no_process(
    py: PyWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """I1: the baseline is recorded under the claim; a stale attempt stops before its process."""
    _with_checks(py, _NO_NULLS)
    real = python_tasks_module.record_baseline

    def stolen_first(session: Session, *args: object) -> Baseline | None:
        py.steal()
        return real(session, *args)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(python_tasks_module, "record_baseline", stolen_first)
    minted: list[int] = []

    def no_token(*args: object, **kwargs: object) -> NoReturn:
        minted.append(1)
        raise LeaseLost

    monkeypatch.setattr(PythonRuntime, "_mint", no_token)
    engine = py.engine(_writer(py.world, rows=2))
    claimed = py.claim_python(engine)
    # No renewal in this test: the refusal of the baseline is what stops the attempt.
    engine.settings = dataclasses.replace(engine.settings, renew_seconds=60)

    run_claimed(engine, claimed)

    assert py.runner.tasks == []
    assert minted == []  # stopped at the baseline, before a token was asked for
    task = _task(py.world, "gold.scores")
    assert (task.status, task.lease_owner) == (TaskStatus.RUNNING, "thief")
    assert (task.baseline_snapshot_id, task.baseline_publishes) == (None, None)


def test_a_conflict_ends_the_task_only_after_its_free_retry_and_is_set_back_then(
    py: PyWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """I1: "ends the task" is judged as `record_attempt` judges it, the free retry included."""
    _published_before(py)
    py.deploy(retries=0)
    _with_checks(py, _NO_NULLS)

    def script(task: PythonTask, on_log: Callable[[bytes], None], stop: StopSignal) -> RunOutcome:
        _upload(py.world, task, 2)
        return RunOutcome(succeeded=False, error_message="the table changed", conflict=True)

    engine = py.engine(script)
    first = py.claim_python(engine)
    run_claimed(engine, first)
    task = _task(py.world, "gold.scores")
    assert (task.status, task.attempt, task.conflict_retried) == (TaskStatus.PENDING, 0, True)
    assert task.error_message == "the table changed"
    assert _ids(py.world, "gold.scores") == [0, 1]
    assert _rollbacks(py) == []

    run_claimed(engine, _next_attempt(py))

    task = _task(py.world, "gold.scores")
    assert (task.status, task.attempt) == (TaskStatus.FAILED, 1)
    assert task.error_message == f"the table changed. {_SET_BACK}"
    assert _ids(py.world, "gold.scores") == [0]


def test_a_failed_first_build_that_created_nothing_has_nothing_to_set_back(
    py: PyWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_checks(py, _NO_NULLS)
    monkeypatch.setattr(
        CatalogService, "roll_back", lambda *a, **k: pytest.fail("set back a table that is not")
    )

    def script(task: PythonTask, on_log: Callable[[bytes], None], stop: StopSignal) -> RunOutcome:
        return RunOutcome(succeeded=False, error_message="it never wrote", retryable=False)

    engine = py.engine(script)
    claimed = py.claim_python(engine)

    run_claimed(engine, claimed)

    task = _task(py.world, "gold.scores")
    assert (task.status, task.error_message) == (TaskStatus.FAILED, "it never wrote")
    assert (task.baseline_snapshot_id, task.baseline_publishes) == (None, True)


def test_a_failed_build_whose_table_cannot_be_read_keeps_its_failure_and_says_what_may_be_left(
    py: PyWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N2: the set-back could not be prepared; the build's own failure stays the message."""
    _published_before(py)
    _with_checks(py, _NO_NULLS)

    def broken(*args: object) -> object:
        raise OSError("storage is away")

    engine = py.engine(_writes_then_fails(py.world, rows=2, retryable=False))
    claimed = py.claim_python(engine)
    monkeypatch.setattr(task_checks_module, "read_metadata", broken)

    run_claimed(engine, claimed)

    task = _task(py.world, "gold.scores")
    assert (task.status, task.attempt, task.error_message) == (
        TaskStatus.FAILED,
        1,
        f"the task raised after its write. {_NOT_SET_BACK}",
    )
    assert _ids(py.world, "gold.scores") == [0, 1]


def test_a_failed_build_whose_run_lost_its_principal_says_that_the_table_was_not_set_back(
    py: PyWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    _published_before(py)
    _with_checks(py, _NO_NULLS)

    def script(task: PythonTask, on_log: Callable[[bytes], None], stop: StopSignal) -> RunOutcome:
        _upload(py.world, task, 2)
        with py.world.session() as session:
            session.execute(update(Run).values(run_as_principal_id=None))
            session.commit()
        return RunOutcome(succeeded=False, error_message="boom", retryable=False)

    engine = py.engine(script)
    claimed = py.claim_python(engine)

    run_claimed(engine, claimed)

    task = _task(py.world, "gold.scores")
    assert (task.status, task.error_message) == (TaskStatus.FAILED, f"boom. {_NOT_SET_BACK}")


def test_a_released_attempt_that_wrote_sets_nothing_back(
    py: PyWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A drain ends no task: the attempt is released, and the next one replaces the table."""
    _published_before(py)
    py.deploy(retries=0)
    _with_checks(py, _NO_NULLS)
    monkeypatch.setattr(
        CatalogService, "roll_back", lambda *a, **k: pytest.fail("set back on a release")
    )
    drain = threading.Event()

    def script(task: PythonTask, on_log: Callable[[bytes], None], stop: StopSignal) -> RunOutcome:
        _upload(py.world, task, 2)
        drain.set()
        return _until_stopped(task, on_log, stop)

    engine = py.engine(script, drain=drain, grace=0.0)
    claimed = py.claim_python(engine)

    run_claimed(engine, claimed)

    task = _task(py.world, "gold.scores")
    assert (task.status, task.attempt, task.max_attempts) == (TaskStatus.PENDING, 0, 1)
    assert _attempts(py.world, task.id)[0].outcome is AttemptOutcome.RELEASED
    assert _ids(py.world, "gold.scores") == [0, 1]


# -- a task run's own chain of writes --------------------------------


def _foreign_append(py: PyWorld, ids: list[int]) -> int:
    """Append rows to `gold.scores` as the administrator, outside any run.

    Parameters
    ----------
    py
        The world.
    ids
        The ids to append.

    Returns
    -------
    int
        The snapshot the append made.
    """
    with py.world.session() as session:
        appended = CatalogService(
            session, StorageResolver(session), AllowAllAuthorization()
        ).append(
            py.world.admin,
            TableRef.from_dotted("dev", "gold.scores"),
            pa.table({"id": pa.array(ids, pa.int64())}),
        )
        assert appended.snapshot_id is not None
        return appended.snapshot_id


def _state(py: PyWorld) -> tuple[int | None, list[str]]:
    """Read the current snapshot and the references of `gold.scores`, published or not.

    Parameters
    ----------
    py
        The world.

    Returns
    -------
    tuple
        The current snapshot and the names of the table's references.
    """
    with py.world.session() as session:
        row = session.scalars(select(Table).where(Table.name == "scores")).one()
        assert row.metadata_location is not None
        file_io = StorageResolver(session).for_environment("dev").file_io
        metadata = read_metadata(file_io, row.metadata_location)
        return metadata.current_snapshot_id, sorted(metadata.refs)


def _tip(py: PyWorld, task_run_id: uuid.UUID) -> OwnTip | None:
    """Read the tip of a task run's own chain of writes.

    Parameters
    ----------
    py
        The world.
    task_run_id
        The task run.

    Returns
    -------
    OwnTip or None
        The tip; `None` when the task run wrote nothing of its own.
    """
    with py.world.session() as session:
        return read_own_tip(session, task_run_id)


def test_the_tip_follows_the_task_runs_own_writes_and_is_what_the_stamp_names(
    py: PyWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N1: the write's own commit records its snapshot; the worker names that, not the table's."""
    _with_checks(py, _NO_NULLS)
    stamps = _stamps(monkeypatch)
    engine = py.engine(_writer(py.world, rows=2))
    claimed = py.claim_python(engine)
    assert _tip(py, claimed.task_run_id) is None

    run_claimed(engine, claimed)

    current, _ = _state(py)
    assert _tip(py, claimed.task_run_id) == OwnTip(current)
    ((_, _, _, written),) = stamps
    assert written.snapshot_after == current
    assert _task(py.world, "gold.scores").status is TaskStatus.SUCCEEDED


def test_a_failed_build_that_wrote_nothing_leaves_another_writers_commit_alone(
    py: PyWorld,
) -> None:
    """N1 (1): the run wrote nothing; what moved the table is someone else's and stays."""
    _published_before(py)
    _with_checks(py, _NO_NULLS)
    foreign: list[int] = []

    def script(task: PythonTask, on_log: Callable[[bytes], None], stop: StopSignal) -> RunOutcome:
        foreign.append(_foreign_append(py, [99]))
        return RunOutcome(succeeded=False, error_message="it never wrote", retryable=False)

    engine = py.engine(script)
    claimed = py.claim_python(engine)

    run_claimed(engine, claimed)

    task = _task(py.world, "gold.scores")
    assert (task.status, task.error_message) == (
        TaskStatus.FAILED,
        f"it never wrote. {_LEFT_UNCHECKED}",
    )
    assert _state(py) == (foreign[0], ["main"])
    assert _ids(py.world, "gold.scores") == [0, 99]
    assert _tip(py, claimed.task_run_id) is None
    (recorded,) = _rollbacks(py)
    assert (recorded["outcome"], recorded["checks"]) == ("changed_since", [])
    assert _marked_by(py) is None


def test_a_commit_by_someone_else_between_two_attempts_is_not_undone_with_them(
    py: PyWorld,
) -> None:
    """N1 (2): the chain is broken at the foreign commit, so the table is left as it is."""
    _published_before(py)
    _with_checks(py, _NO_ROWS)
    engine = py.engine(_writes_then_fails(py.world, rows=2))
    first = py.claim_python(engine)
    run_claimed(engine, first)
    own, _ = _state(py)
    assert _tip(py, first.task_run_id) == OwnTip(own)
    foreign = _foreign_append(py, [99])

    engine = py.engine(_writer(py.world, rows=3))
    second = _next_attempt(py)
    run_claimed(engine, second)

    task = _task(py.world, "gold.scores")
    assert task.status is TaskStatus.FAILED
    assert task.error_message == (
        "Check `gold.scores/no_rows` failed: 3 rows (checks/gold/scores/no_rows.sql). "
        f"{_LEFT_FAILING} Look at the rows: "
        f"`lakana check rows {second.run_id} gold.scores/no_rows --env dev`. "
        "After a fix, deploy and run again; if the project did not change: "
        f"`lakana runs resume {second.run_id} --env dev`."
    )
    current, refs = _state(py)
    assert current not in {own, foreign} and refs == ["main"]
    assert _ids(py.world, "gold.scores") == [
        0,
        1,
        2,
    ]  # attempt 2's rows, on top of the foreign commit
    assert _tip(py, first.task_run_id) == OwnTip(own)  # not moved by a write that started elsewhere
    (recorded,) = _rollbacks(py)
    assert recorded["outcome"] == "changed_since"


def test_a_table_somebody_else_created_meanwhile_stays_published_and_untouched(
    py: PyWorld,
) -> None:
    """N1 (4): the baseline said "missing"; the table that is there now is not this run's."""
    _with_checks(py, _NO_NULLS)

    def script(task: PythonTask, on_log: Callable[[bytes], None], stop: StopSignal) -> RunOutcome:
        data = pa.table({"id": pa.array([7], pa.int64())})
        with py.world.session() as session:
            CatalogService(
                session, StorageResolver(session), AllowAllAuthorization()
            ).write_batches(
                py.world.admin,
                TableRef.from_dotted("dev", "gold.scores"),
                mode=WriteMode.CREATE,
                schema=data.schema,
                batches=data.to_batches(),
            )
        return RunOutcome(succeeded=False, error_message="it never wrote", retryable=False)

    engine = py.engine(script)
    claimed = py.claim_python(engine)

    run_claimed(engine, claimed)

    task = _task(py.world, "gold.scores")
    assert (task.status, task.error_message) == (
        TaskStatus.FAILED,
        f"it never wrote. {_LEFT_UNCHECKED}",
    )
    assert (task.baseline_snapshot_id, task.baseline_publishes) == (None, True)
    assert _marked_by(py) is None
    assert _ids(py.world, "gold.scores") == [7]
    assert _state(py)[1] == ["main"]
    assert [r["outcome"] for r in _rollbacks(py)] == ["changed_since"]


def test_a_commit_by_someone_else_after_the_tasks_write_is_never_discarded(
    py: PyWorld,
) -> None:
    """N1 (5): the rollback names this run's own snapshot, not whatever the table is at."""
    _with_checks(py, _NO_ROWS)
    foreign: list[int] = []

    def script(task: PythonTask, on_log: Callable[[bytes], None], stop: StopSignal) -> RunOutcome:
        written = _upload(py.world, task, 2)
        foreign.append(_foreign_append(py, [99]))
        return RunOutcome(succeeded=True, snapshot_id=written.snapshot_id, rows=written.rows)

    engine = py.engine(script)
    claimed = py.claim_python(engine)

    run_claimed(engine, claimed)

    task = _task(py.world, "gold.scores")
    assert task.status is TaskStatus.FAILED
    assert task.error_message is not None
    assert _LEFT_FAILING in task.error_message
    assert _state(py) == (foreign[0], ["main"])
    assert _ids(py.world, "gold.scores") == [0, 1, 99]
    assert _marked_by(py) is None
    tip = _tip(py, claimed.task_run_id)
    assert tip is not None and tip.snapshot_id not in {None, foreign[0]}
    assert task.written_snapshot_after == tip.snapshot_id
    assert [r["outcome"] for r in _rollbacks(py)] == ["changed_since"]


def test_a_stale_claims_write_does_not_move_the_tip(py: PyWorld) -> None:
    """N1 (6): the write of a claim that is gone is refused, so the chain is the live claim's."""
    _with_checks(py, _NO_NULLS)
    refused: list[str] = []
    tips: list[OwnTip | None] = []
    claims: list[ClaimedTask] = []

    def script(task: PythonTask, on_log: Callable[[bytes], None], stop: StopSignal) -> RunOutcome:
        _upload(py.world, task, 2)
        tips.append(_tip(py, claims[0].task_run_id))
        py.steal()
        try:
            _upload(py.world, dataclasses.replace(task, expected_snapshot_id=_state(py)[0]), 5)
        except LakanaError as exc:
            refused.append(type(exc).__name__)
        return RunOutcome(succeeded=False, error_message="stopped", retryable=False)

    engine = py.engine(script)
    claimed = py.claim_python(engine)
    claims.append(claimed)

    run_claimed(engine, claimed)

    assert len(refused) == 1
    (before,) = tips
    assert before is not None and before.snapshot_id is not None
    assert _tip(py, claimed.task_run_id) == before
    assert _state(py)[0] == before.snapshot_id
    assert _ids(py.world, "gold.scores") == [0, 1]


def test_the_tip_of_a_task_without_checks_is_never_recorded(py: PyWorld) -> None:
    """N1 (7): no checks, no baseline, no tip: such a task's writes take no new statement's hit."""
    engine = py.engine(_writer(py.world, rows=2))
    claimed = py.claim_python(engine)

    run_claimed(engine, claimed)

    task = _task(py.world, "gold.scores")
    assert task.status is TaskStatus.SUCCEEDED
    assert (task.own_snapshot_id, task.baseline_publishes) == (None, None)
    assert _tip(py, claimed.task_run_id) is None


def test_a_first_build_without_rows_has_a_tip_without_a_snapshot_and_is_not_published(
    py: PyWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A zero-row create makes no snapshot: the tip says "wrote, no snapshot", not "never wrote"."""
    _with_checks(
        py, ("has_rows", "SELECT 1 AS missing WHERE (SELECT count(*) FROM gold.scores) = 0")
    )
    engine = py.engine(_writer(py.world, rows=0))
    claimed = py.claim_python(engine)
    seen: list[tuple[int | None, OwnTip | None]] = []
    real = CatalogService.roll_back

    def spying(self: CatalogService, *args: object, **kwargs: object) -> object:
        # The tip as the rollback finds it; a rollback that lands clears it.
        seen.append((_task(py.world, "gold.scores").own_snapshot_id, _tip(py, claimed.task_run_id)))
        return real(self, *args, **kwargs)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(CatalogService, "roll_back", spying)

    run_claimed(engine, claimed)

    task = _task(py.world, "gold.scores")
    assert task.status is TaskStatus.FAILED
    assert seen == [(0, OwnTip(None))]
    assert task.own_snapshot_id is None
    assert _marked_by(py) == claimed.run_id
    assert [r["outcome"] for r in _rollbacks(py)] == ["unpublished"]


def test_a_refused_set_back_of_a_failed_build_for_a_claim_that_is_gone_records_nothing(
    py: PyWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale claim is not a refused rollback: the attempt is dropped, as after a failed check."""
    _published_before(py)
    _with_checks(py, _NO_NULLS)

    def stale(self: CatalogService, *args: object, **kwargs: object) -> object:
        py.steal()
        raise PermissionDenied("superseded")

    monkeypatch.setattr(CatalogService, "roll_back", stale)
    engine = py.engine(_writes_then_fails(py.world, rows=2, retryable=False))
    claimed = py.claim_python(engine)

    run_claimed(engine, claimed)

    task = _task(py.world, "gold.scores")
    assert (task.status, task.lease_owner, task.error_message) == (
        TaskStatus.RUNNING,
        "thief",
        None,
    )
    assert _attempts(py.world, claimed.task_run_id)[0].finished_at is None


def test_a_failed_build_is_not_set_back_over_a_commit_that_came_after_its_write(
    py: PyWorld,
) -> None:
    """N1: also when the build fails for good, `after` is the run's own tip, not the table's."""
    _published_before(py)
    _with_checks(py, _NO_NULLS)
    foreign: list[int] = []

    def script(task: PythonTask, on_log: Callable[[bytes], None], stop: StopSignal) -> RunOutcome:
        _upload(py.world, task, 2)
        foreign.append(_foreign_append(py, [99]))
        return RunOutcome(succeeded=False, error_message="boom", retryable=False)

    engine = py.engine(script)
    claimed = py.claim_python(engine)

    run_claimed(engine, claimed)

    task = _task(py.world, "gold.scores")
    assert (task.status, task.error_message) == (
        TaskStatus.FAILED,
        f"boom. {_LEFT_UNCHECKED}",
    )
    assert _state(py) == (foreign[0], ["main"])
    assert _ids(py.world, "gold.scores") == [0, 1, 99]


def test_a_write_on_top_of_a_commit_that_came_before_it_is_not_taken_for_the_runs_own_chain(
    py: PyWorld,
) -> None:
    """N1: the chain never started (the first write was not staged on the baseline)."""
    _published_before(py)
    _with_checks(py, _NO_ROWS)
    foreign: list[int] = []

    def script(task: PythonTask, on_log: Callable[[bytes], None], stop: StopSignal) -> RunOutcome:
        foreign.append(_foreign_append(py, [99]))
        written = _upload(py.world, dataclasses.replace(task, expected_snapshot_id=foreign[0]), 2)
        return RunOutcome(succeeded=True, snapshot_id=written.snapshot_id, rows=written.rows)

    engine = py.engine(script)
    claimed = py.claim_python(engine)

    run_claimed(engine, claimed)

    task = _task(py.world, "gold.scores")
    assert task.status is TaskStatus.FAILED and task.error_message is not None
    assert _LEFT_FAILING in task.error_message
    assert "was not changed by this run" not in task.error_message
    assert _tip(py, claimed.task_run_id) is None
    assert task.written_snapshot_after is None
    assert _ids(py.world, "gold.scores") == [0, 1]  # the run's rows stay: nothing is set back
    assert _state(py)[1] == ["main"]
    assert [r["outcome"] for r in _rollbacks(py)] == ["changed_since"]


def test_an_empty_table_somebody_else_created_is_not_taken_for_the_runs_first_build(
    py: PyWorld,
) -> None:
    """N1: same snapshot as the baseline (none), but the table is somebody's: not unpublished."""
    _with_checks(py, _NO_ROWS)

    def script(task: PythonTask, on_log: Callable[[bytes], None], stop: StopSignal) -> RunOutcome:
        empty = pa.table({"id": pa.array([], pa.int64())})
        with py.world.session() as session:
            CatalogService(
                session, StorageResolver(session), AllowAllAuthorization()
            ).write_batches(
                py.world.admin,
                TableRef.from_dotted("dev", "gold.scores"),
                mode=WriteMode.CREATE,
                schema=empty.schema,
                batches=empty.to_batches(),
            )
        written = _upload(py.world, task, 2)
        return RunOutcome(succeeded=True, snapshot_id=written.snapshot_id, rows=written.rows)

    engine = py.engine(script)
    claimed = py.claim_python(engine)

    run_claimed(engine, claimed)

    task = _task(py.world, "gold.scores")
    assert task.status is TaskStatus.FAILED
    assert _tip(py, claimed.task_run_id) is None
    assert _marked_by(py) is None
    assert _ids(py.world, "gold.scores") == [0, 1]
    assert [r["outcome"] for r in _rollbacks(py)] == ["changed_since"]


def test_a_cancel_that_arrives_after_a_failed_build_hands_the_task_back_to_be_set_back(
    py: PyWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cancel still wins over the failure, and the table is set back."""
    _published_before(py)
    _with_checks(py, _NO_NULLS)
    engine = py.engine(_writes_then_fails(py.world, rows=2, retryable=False))
    claimed = py.claim_python(engine)
    real = tasks_module._dispatch

    def fail_then_cancel(*args: object, **kwargs: object) -> TaskOutcome | None:
        outcome = real(*args, **kwargs)  # ty: ignore[invalid-argument-type]
        py.cancel(claimed.run_id)
        return outcome

    monkeypatch.setattr(tasks_module, "_dispatch", fail_then_cancel)

    run_claimed(engine, claimed)

    _is_handed_back_then_set_back_and_cancelled(py, monkeypatch)


@pytest.mark.parametrize("tip_before", ["a snapshot", "none"])
@pytest.mark.slow
def test_an_append_of_no_rows_into_its_own_asset_does_not_break_the_tasks_chain(
    py: PyWorld, tip_before: str
) -> None:
    """M-a: a write that made no snapshot on a table that has one is no link of the chain."""
    _published_before(py)
    _with_checks(py, _NO_ROWS)
    empty = pa.table({"id": pa.array([], pa.int64())})

    def append_nothing(task: PythonTask) -> None:
        parsed = parse_token(task.token, "lakana_task")
        assert parsed is not None
        with py.world.session() as session:
            principal = TaskTokenService(session, AllowAllAuthorization()).verify(*parsed).principal
            assert principal.run_context is not None and principal.run_context.claim is not None
            result = CatalogService(
                session, StorageResolver(session), AllowAllAuthorization()
            ).write_batches(
                principal,
                TableRef.from_dotted("dev", "gold.scores"),
                mode=WriteMode.APPEND,
                schema=empty.schema,
                batches=empty.to_batches(),
            )
            assert (result.rows, result.snapshot_id) == (0, None)

    def script(task: PythonTask, on_log: Callable[[bytes], None], stop: StopSignal) -> RunOutcome:
        if tip_before == "a snapshot":
            first = _upload(py.world, task, 2)
            task = dataclasses.replace(task, expected_snapshot_id=first.snapshot_id)
        append_nothing(task)
        written = _upload(py.world, task, 3)
        return RunOutcome(succeeded=True, snapshot_id=written.snapshot_id, rows=written.rows)

    engine = py.engine(script)
    claimed = py.claim_python(engine)

    run_claimed(engine, claimed)

    task = _task(py.world, "gold.scores")
    assert task.status is TaskStatus.FAILED and task.error_message is not None
    assert _SET_BACK in task.error_message
    assert "was not changed by this run" not in task.error_message
    assert _ids(py.world, "gold.scores") == [0]
    assert [r["outcome"] for r in _rollbacks(py)] == ["rolled_back"]


def test_a_retry_after_a_set_back_whose_recording_was_lost_starts_a_new_chain(
    py: PyWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A table that is back at its baseline has no chain of this task run any more.

    Attempt 1 writes and fails for good; the set-back lands; the worker is lost before it
    records the failure, so the task is retried. Attempt 2's write is staged on the baseline
    again and is the task run's own: a failed check must set it back, not blame another writer.
    """
    _published_before(py)
    _with_checks(py, _NO_ROWS)
    engine = py.engine(_writes_then_fails(py.world, rows=2, retryable=False))
    first = py.claim_python(engine)
    real_finish = tasks_module._finish

    def die(*args: object) -> None:
        raise SystemExit("the worker died after the set-back and before recording")

    monkeypatch.setattr(tasks_module, "_finish", die)
    with pytest.raises(SystemExit):
        run_claimed(engine, first)
    monkeypatch.setattr(tasks_module, "_finish", real_finish)
    assert _ids(py.world, "gold.scores") == [0]  # set back already
    assert [r["outcome"] for r in _rollbacks(py)] == ["rolled_back"]
    assert _tip(py, first.task_run_id) is None  # cleared with the set-back
    _expire_lease_and_tick(py.world, "gold.scores")

    engine = py.engine(_writer(py.world, rows=3))
    second = _claim(py.world)
    run_claimed(engine, second)

    task = _task(py.world, "gold.scores")
    assert task.status is TaskStatus.FAILED and task.error_message is not None
    assert _SET_BACK in task.error_message
    assert "another writer" not in task.error_message
    assert _ids(py.world, "gold.scores") == [0]
    assert [r["outcome"] for r in _rollbacks(py)] == ["rolled_back", "rolled_back"]


# -- a claim that only sets the table back, for a task whose worker was lost --------------------


def _lost_after(
    py: PyWorld,
    monkeypatch: pytest.MonkeyPatch,
    script: Script,
    *,
    cancel: bool = False,
    stamped: bool = False,
) -> ClaimedTask:
    """Lose the worker of `gold.scores` on its last attempt, after `script` ran its process.

    The table held one row before the run. The task is put back by the scheduler for a set-back
    claim, which is returned.

    Parameters
    ----------
    py
        The world.
    monkeypatch
        pytest's patcher.
    script
        What the lost attempt's process does.
    cancel
        Whether the run is asked to cancel before the scheduler finds the lease expired.
    stamped
        Lose the worker after the stamp of the write instead of before it. The scheduler puts
        a stamped task back for a set-back claim only in a cancelled run.

    Returns
    -------
    ClaimedTask
        The set-back claim, taken by the worker `w1`.
    """
    _published_before(py)
    py.deploy(retries=0)
    _with_checks(py, _NO_NULLS)
    engine = py.engine(script)
    first = py.claim_python(engine)
    real = tasks_module.check_phase

    def die(*args: object) -> TaskOutcome | None:
        raise SystemExit("the worker was lost")

    if stamped:
        real_stop = task_checks_module._stop_if_asked
        monkeypatch.setattr(task_checks_module, "_stop_if_asked", die)
    else:
        monkeypatch.setattr(tasks_module, "check_phase", die)
    with pytest.raises(SystemExit):
        run_claimed(engine, first)
    if stamped:
        monkeypatch.setattr(task_checks_module, "_stop_if_asked", real_stop)
        assert _task(py.world, "gold.scores").written_table_id is not None
    else:
        monkeypatch.setattr(tasks_module, "check_phase", real)
    if cancel:
        py.cancel(first.run_id)
    _expire_lease_and_tick(py.world, "gold.scores")
    ready = _task(py.world, "gold.scores")
    assert (ready.status, ready.setting_back) == (TaskStatus.READY, True)
    claimed = _claim(py.world)
    assert (claimed.setting_back, claimed.attempt) == (True, first.attempt)
    return claimed


def _builds_nothing(py: PyWorld, monkeypatch: pytest.MonkeyPatch) -> RunEngine:
    """Build an engine for a claim that must start no process, mint no token, run no check.

    Parameters
    ----------
    py
        The world.
    monkeypatch
        pytest's patcher.

    Returns
    -------
    RunEngine
        The engine; the test fails if it is asked to build or to check.
    """

    def script(task: PythonTask, on_log: Callable[[bytes], None], stop: StopSignal) -> RunOutcome:
        pytest.fail("a set-back claim started a process")

    monkeypatch.setattr(
        TaskTokenService, "mint_for_task", lambda *a, **k: pytest.fail("a token was minted")
    )
    monkeypatch.setattr(tasks_module, "check_phase", lambda *a: pytest.fail("a check ran"))
    monkeypatch.setattr(tasks_module, "before_build", lambda *a: pytest.fail("before_build ran"))
    return py.engine(script)


def test_a_task_lost_after_its_write_is_set_back_by_a_claim_that_builds_nothing(
    py: PyWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The table is set back to the baseline, with no process, no token and no check."""
    claimed = _lost_after(py, monkeypatch, _writer(py.world, rows=2))
    assert _ids(py.world, "gold.scores") == [0, 1]  # the unchecked write
    engine = _builds_nothing(py, monkeypatch)
    tokens_before = len(py.tokens())

    run_claimed(engine, claimed)

    task = _task(py.world, "gold.scores")
    assert (task.status, task.attempt, task.set_back_claims) == (TaskStatus.FAILED, 1, 1)
    assert task.error_message == f"{LOST_MESSAGE} {SET_BACK_DONE}"
    assert task.failure_code == "worker_lost"
    assert _ids(py.world, "gold.scores") == [0]
    assert py.runner.tasks == [] and len(py.tokens()) == tokens_before
    (rolled,) = _rollbacks(py)
    assert rolled["outcome"] == "rolled_back"
    last = _attempts(py.world, claimed.task_run_id)[-1]
    assert (last.outcome, last.error_message) == (AttemptOutcome.FAILED, task.error_message)
    assert f"error: {task.error_message}" in _log(py.world, claimed.run_id, "gold.scores")


def test_a_task_lost_before_it_wrote_anything_changes_nothing(
    py: PyWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The table is at its baseline and the task has no tip: nothing is set back, and it says so."""

    def writes_nothing(
        task: PythonTask, on_log: Callable[[bytes], None], stop: StopSignal
    ) -> RunOutcome:
        return RunOutcome(succeeded=False, error_message="gone", retryable=False)

    claimed = _lost_after(py, monkeypatch, writes_nothing)
    engine = _builds_nothing(py, monkeypatch)
    monkeypatch.setattr(
        CatalogService, "roll_back", lambda *a, **k: pytest.fail("set back an untouched table")
    )

    run_claimed(engine, claimed)

    task = _task(py.world, "gold.scores")
    assert (task.status, task.error_message) == (
        TaskStatus.FAILED,
        f"{LOST_MESSAGE} {SET_BACK_NOTHING}",
    )
    assert task.failure_code == "worker_lost"
    assert _ids(py.world, "gold.scores") == [0]
    assert _rollbacks(py) == []


def test_a_cancelled_run_sets_the_table_back_and_the_task_ends_cancelled(
    py: PyWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cancel request does not stop the set-back, which is the cancel's own work."""
    claimed = _lost_after(py, monkeypatch, _writer(py.world, rows=2), cancel=True)
    engine = _builds_nothing(py, monkeypatch)

    run_claimed(engine, claimed)

    task = _task(py.world, "gold.scores")
    assert (task.status, task.error_message) == (
        TaskStatus.CANCELLED,
        f"{RUN_CANCELLED_MESSAGE} {SET_BACK_DONE}",
    )
    assert _ids(py.world, "gold.scores") == [0]
    assert [r["outcome"] for r in _rollbacks(py)] == ["rolled_back"]


def test_a_cancel_that_lands_during_the_set_back_ends_the_task_cancelled(
    py: PyWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cancel flag is read again after the rollback, so the run ends cancelled, not failed."""
    claimed = _lost_after(py, monkeypatch, _writer(py.world, rows=2))
    engine = _builds_nothing(py, monkeypatch)
    real_roll_back = CatalogService.roll_back

    def roll_back_then_cancel(self: CatalogService, *args: object, **kwargs: object) -> object:
        done = real_roll_back(self, *args, **kwargs)  # ty: ignore[invalid-argument-type]
        py.cancel(claimed.run_id)
        return done

    monkeypatch.setattr(CatalogService, "roll_back", roll_back_then_cancel)

    run_claimed(engine, claimed)

    task = _task(py.world, "gold.scores")
    assert (task.status, task.error_message) == (
        TaskStatus.CANCELLED,
        f"{RUN_CANCELLED_MESSAGE} {SET_BACK_DONE}",
    )
    assert _ids(py.world, "gold.scores") == [0]


def test_a_cancel_while_a_retry_waits_sets_back_what_the_failed_attempt_left(
    py: PyWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Attempt 1 wrote and failed; its rows wait for attempt 2. A cancel sets them back."""
    _published_before(py)
    _with_checks(py, _NO_ROWS)
    engine = py.engine(_writes_then_fails(py.world, rows=2))
    first = py.claim_python(engine)
    run_claimed(engine, first)
    assert _task(py.world, "gold.scores").status is TaskStatus.PENDING
    assert _ids(py.world, "gold.scores") == [0, 1]  # unchecked, until the next attempt
    py.cancel(first.run_id)
    engine = _builds_nothing(py, monkeypatch)
    claimed = _next_attempt(py)
    assert (claimed.setting_back, claimed.attempt) == (True, first.attempt)

    run_claimed(engine, claimed)

    task = _task(py.world, "gold.scores")
    assert (task.status, task.error_message) == (
        TaskStatus.CANCELLED,
        f"{RUN_CANCELLED_MESSAGE} {SET_BACK_DONE}",
    )
    assert _ids(py.world, "gold.scores") == [0]


def test_a_commit_of_another_writer_after_the_tip_is_left_alone(
    py: PyWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The catalog finds the table changed after the task's write and touches nothing."""
    claimed = _lost_after(py, monkeypatch, _writer(py.world, rows=2))
    _foreign_append(py, [7])
    engine = _builds_nothing(py, monkeypatch)

    run_claimed(engine, claimed)

    task = _task(py.world, "gold.scores")
    assert (task.status, task.error_message) == (
        TaskStatus.FAILED,
        f"{LOST_MESSAGE} {SET_BACK_CHANGED}",
    )
    assert task.failure_code == "worker_lost"
    assert _ids(py.world, "gold.scores") == [0, 1, 7]


def test_a_refused_set_back_says_that_the_table_could_not_be_set_back(
    py: PyWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refusal ends the task with the failed set-back text, not as a retry."""
    claimed = _lost_after(py, monkeypatch, _writer(py.world, rows=2))
    engine = _builds_nothing(py, monkeypatch)

    def refuse(self: CatalogService, *args: object, **kwargs: object) -> object:
        raise PermissionDenied("x")

    monkeypatch.setattr(CatalogService, "roll_back", refuse)

    run_claimed(engine, claimed)

    task = _task(py.world, "gold.scores")
    assert (task.status, task.error_message) == (
        TaskStatus.FAILED,
        set_back_failed_message(LOST_MESSAGE),
    )
    assert task.failure_code == "worker_lost"
    assert _ids(py.world, "gold.scores") == [0, 1]


@pytest.mark.parametrize(
    "error", [LeaseLost(), OperationalError("SELECT 1", {}, Exception("connection lost"))]
)
def test_a_lost_claim_or_a_database_error_while_setting_back_records_nothing(
    py: PyWorld, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    """The scheduler finds the lease expired and sets the task back for another claim."""
    claimed = _lost_after(py, monkeypatch, _writer(py.world, rows=2))
    engine = _builds_nothing(py, monkeypatch)

    def fail(self: CatalogService, *args: object, **kwargs: object) -> object:
        raise error

    monkeypatch.setattr(CatalogService, "roll_back", fail)

    run_claimed(engine, claimed)

    task = _task(py.world, "gold.scores")
    assert (task.status, task.setting_back) == (TaskStatus.RUNNING, True)
    assert _attempts(py.world, claimed.task_run_id)[-1].outcome is None
    _expire_lease_and_tick(py.world, "gold.scores")
    again = _task(py.world, "gold.scores")
    assert (again.status, again.setting_back, again.set_back_claims) == (
        TaskStatus.READY,
        True,
        1,
    )


def test_a_drain_puts_a_set_back_claim_back_with_the_flag_and_the_attempt(
    py: PyWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A drain is no loss: the task is claimed again as a set-back, uncounted."""
    claimed = _lost_after(py, monkeypatch, _writer(py.world, rows=2))
    drain = threading.Event()
    drain.set()
    real_keeper_for = tasks_module.keeper_for

    def draining(engine: RunEngine, claim: ClaimedTask) -> LeaseKeeper:
        keeper = real_keeper_for(engine, claim)
        real_start = keeper.start

        def start() -> None:
            real_start()
            keeper.renew_once()  # the drain's grace is zero: the keeper learns of it now

        keeper.start = start  # ty: ignore[invalid-assignment]
        return keeper

    monkeypatch.setattr(tasks_module, "keeper_for", draining)
    _builds_nothing(py, monkeypatch)
    engine = py.engine(lambda *a: pytest.fail("a process"), drain=drain, grace=0.0)

    run_claimed(engine, claimed)

    task = _task(py.world, "gold.scores")
    assert (task.status, task.setting_back, task.attempt, task.set_back_claims) == (
        TaskStatus.PENDING,
        True,
        1,
        0,
    )
    assert _ids(py.world, "gold.scores") == [0, 1]
    assert _attempts(py.world, claimed.task_run_id)[-1].outcome is AttemptOutcome.RELEASED


def _stopped_for_memory_after_a_write(
    py: PyWorld, monkeypatch: pytest.MonkeyPatch, kind: FailureKind, *, retries: int
) -> tuple[ClaimedTask, str]:
    """Run a Python task with checks until its process wrote, then end it as the memory stop does.

    The table held one row before the run; the process wrote two more. The worker is lost in the
    check phase, and the watchdog's record (`record_attempt(stopped=True)`) is made under the
    claim it held.

    Parameters
    ----------
    py
        The world.
    monkeypatch
        pytest's patcher.
    kind
        The blame the stop records.
    retries
        The task's retries.

    Returns
    -------
    tuple
        The claim of the stopped attempt, and the stop's message.
    """
    from lakana_worker.memory import COMMITTED_BEFORE_STOPPED_MESSAGE, Limit, task_message

    _published_before(py)
    py.deploy(retries=retries)
    _with_checks(py, _NO_NULLS)
    engine = py.engine(_writer(py.world, rows=2))
    first = py.claim_python(engine)

    def die(*args: object) -> TaskOutcome | None:
        raise SystemExit("the worker was stopped")

    monkeypatch.setattr(tasks_module, "check_phase", die)
    with pytest.raises(SystemExit):
        run_claimed(engine, first)
    assert _ids(py.world, "gold.scores") == [0, 1]  # the unchecked write
    message = task_message(Limit(4 * 1024**3, "4GB", "x"))
    with py.world.session() as session:
        assert record_attempt(
            session,
            first.task_run_id,
            engine.settings.worker_id,
            first.claim,
            kind=kind,
            rand=lambda: 0.5,
            error_message=message,
            failure_code=FailureCode.MEMORY,
            on_committed=COMMITTED_BEFORE_STOPPED_MESSAGE,
            stopped=True,
        )
    return first, message


def test_a_memory_stop_on_the_last_attempt_after_a_write_is_set_back_by_the_next_worker(
    py: PyWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stop hands the task back; the next worker sets the table back and ends it."""
    first, message = _stopped_for_memory_after_a_write(
        py, monkeypatch, FailureKind.TRANSIENT, retries=0
    )
    handed = _task(py.world, "gold.scores")
    assert (handed.status, handed.setting_back, handed.attempt) == (
        TaskStatus.READY,
        True,
        first.attempt,
    )
    assert _ids(py.world, "gold.scores") == [0, 1]  # nothing was set back yet
    claimed = _claim(py.world)
    assert (claimed.setting_back, claimed.attempt) == (True, first.attempt)
    engine = _builds_nothing(py, monkeypatch)

    run_claimed(engine, claimed)

    task = _task(py.world, "gold.scores")
    assert (task.status, task.attempt, task.set_back_claims) == (TaskStatus.FAILED, 1, 1)
    assert task.error_message == f"{message} {SET_BACK_DONE}"
    assert task.failure_code == "memory"
    assert _ids(py.world, "gold.scores") == [0]
    assert [r["outcome"] for r in _rollbacks(py)] == ["rolled_back"]


def test_a_run_cancelled_after_a_memory_stop_hand_back_ends_with_the_cancel_message(
    py: PyWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stop and a cancel meet: the user's cancel is the reason shown, as the docs say."""
    first, _ = _stopped_for_memory_after_a_write(py, monkeypatch, FailureKind.TRANSIENT, retries=0)
    py.cancel(first.run_id)
    claimed = _claim(py.world)
    assert claimed.setting_back

    run_claimed(_builds_nothing(py, monkeypatch), claimed)

    task = _task(py.world, "gold.scores")
    assert (task.status, task.error_message) == (
        TaskStatus.CANCELLED,
        f"{RUN_CANCELLED_MESSAGE} {SET_BACK_DONE}",
    )
    assert _ids(py.world, "gold.scores") == [0]


def test_a_memory_stop_that_ends_a_task_for_certain_after_a_write_hands_it_back_too(
    py: PyWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Certain blame ends the task on any attempt, so attempts left do not matter."""
    _stopped_for_memory_after_a_write(py, monkeypatch, FailureKind.PERMANENT, retries=2)
    handed = _task(py.world, "gold.scores")
    assert (handed.status, handed.setting_back) == (TaskStatus.READY, True)
    run_claimed(_builds_nothing(py, monkeypatch), _claim(py.world))
    assert _task(py.world, "gold.scores").status is TaskStatus.FAILED
    assert _ids(py.world, "gold.scores") == [0]


def test_a_memory_stop_with_attempts_left_and_uncertain_blame_retries_from_the_baseline(
    py: PyWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Kept as before: the next attempt replaces the table, nothing is handed back."""
    _stopped_for_memory_after_a_write(py, monkeypatch, FailureKind.TRANSIENT, retries=2)
    task = _task(py.world, "gold.scores")
    assert (task.status, task.setting_back) == (TaskStatus.PENDING, False)
    assert _ids(py.world, "gold.scores") == [0, 1]  # the next attempt starts from the baseline


def _cancelled_after_its_write(py: PyWorld, written_rows: int) -> tuple[ClaimedTask, RunEngine]:
    """Run a Python task with checks whose process writes and then sees the run cancelled.

    Parameters
    ----------
    py
        The world.
    written_rows
        Rows the process writes before the cancel; none writes nothing.

    Returns
    -------
    tuple
        The claim, and the engine that ran it.
    """
    _published_before(py)
    py.deploy(retries=0)
    _with_checks(py, _NO_NULLS)
    run: list[uuid.UUID] = []

    def script(task: PythonTask, on_log: Callable[[bytes], None], stop: StopSignal) -> RunOutcome:
        if written_rows:
            _upload(py.world, task, written_rows)
        py.cancel(run[0])
        return RunOutcome(succeeded=False, error_message="stopped: cancelled", retryable=False)

    engine = py.engine(script)
    first = py.claim_python(engine)
    run.append(first.run_id)
    run_claimed(engine, first)
    return first, engine


def test_a_cancel_seen_after_the_write_hands_the_task_back_and_the_table_is_set_back(
    py: PyWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The next worker sets the table back, then the task ends cancelled."""
    first, _ = _cancelled_after_its_write(py, written_rows=2)
    handed = _task(py.world, "gold.scores")
    assert (handed.status, handed.setting_back, handed.attempt) == (
        TaskStatus.READY,
        True,
        first.attempt,
    )
    assert _ids(py.world, "gold.scores") == [0, 1]
    claimed = _claim(py.world)
    assert claimed.setting_back

    run_claimed(_builds_nothing(py, monkeypatch), claimed)

    task = _task(py.world, "gold.scores")
    assert (task.status, task.error_message) == (
        TaskStatus.CANCELLED,
        f"{RUN_CANCELLED_MESSAGE} {SET_BACK_DONE}",
    )
    assert _ids(py.world, "gold.scores") == [0]


def test_a_cancel_after_the_baseline_but_before_any_write_says_nothing_was_written(
    py: PyWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A task with a baseline is handed back whether or not it wrote; the set-back says so."""
    _cancelled_after_its_write(py, written_rows=0)
    claimed = _claim(py.world)
    assert claimed.setting_back
    run_claimed(_builds_nothing(py, monkeypatch), claimed)
    task = _task(py.world, "gold.scores")
    assert (task.status, task.error_message) == (
        TaskStatus.CANCELLED,
        f"{RUN_CANCELLED_MESSAGE} {SET_BACK_NOTHING}",
    )
    assert _ids(py.world, "gold.scores") == [0]
    assert _rollbacks(py) == []


def test_a_cancel_before_the_process_was_prepared_ends_the_task_cancelled(py: PyWorld) -> None:
    """No baseline was recorded, so nothing can be in the table: the task ends as before."""

    def cancelled(
        settings: VenvSettings,
        sha: str,
        project: Path,
        on_log: Callable[[bytes], None],
        indexes: IndexResolver | None = None,
        *,
        stop: Callable[[], bool] | None = None,
    ) -> VenvLease:
        py.cancel(claimed.run_id)
        raise LakanaError("the environment build was interrupted")

    py.deploy(retries=0)
    _with_checks(py, _NO_NULLS)
    engine = py.engine(_forged, acquire_venv=cancelled)
    claimed = py.claim_python(engine)
    run_claimed(engine, claimed)
    task = _task(py.world, "gold.scores")
    with py.world.session() as session:
        assert read_baseline(session, claimed.task_run_id) is None
    assert (task.status, task.setting_back) == (TaskStatus.CANCELLED, False)


def test_a_stamped_python_task_is_set_back_from_its_stamp(
    py: PyWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stamp names the baseline and the tip; the claim sets the table back through them."""
    claimed = _lost_after(py, monkeypatch, _writer(py.world, rows=2), cancel=True, stamped=True)
    engine = _builds_nothing(py, monkeypatch)

    run_claimed(engine, claimed)

    task = _task(py.world, "gold.scores")
    assert (task.status, task.error_message) == (
        TaskStatus.CANCELLED,
        f"{RUN_CANCELLED_MESSAGE} {SET_BACK_DONE}",
    )
    assert _ids(py.world, "gold.scores") == [0]
    assert task.written_table_id is None


def test_a_table_that_is_not_managed_any_more_has_nothing_to_set_back(
    py: PyWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No managed table: nothing is set back, and the message says so."""
    claimed = _lost_after(py, monkeypatch, _writer(py.world, rows=2))
    engine = _builds_nothing(py, monkeypatch)
    monkeypatch.setattr(task_checks_module, "_table_now", lambda *a: None)
    monkeypatch.setattr(
        CatalogService, "roll_back", lambda *a, **k: pytest.fail("set back a table that is gone")
    )

    run_claimed(engine, claimed)

    task = _task(py.world, "gold.scores")
    assert (task.status, task.error_message) == (
        TaskStatus.FAILED,
        f"{LOST_MESSAGE} {SET_BACK_NOTHING}",
    )
    assert task.failure_code == "worker_lost"


@pytest.mark.parametrize(
    "outcome",
    [
        Rollback(
            RollbackOutcome.CHANGED_SINCE, "the table was dropped while the rollback was prepared"
        ),
        Rollback(RollbackOutcome.FAILED, ROLLBACK_NOT_KNOWN_REASON),
    ],
)
def test_the_outcome_of_the_catalog_is_told_in_the_words_of_the_failed_check_path(
    py: PyWorld, monkeypatch: pytest.MonkeyPatch, outcome: Rollback
) -> None:
    """A dropped table and a rollback that may have landed are not 'changed' or 'refused'."""
    claimed = _lost_after(py, monkeypatch, _writer(py.world, rows=2))
    engine = _builds_nothing(py, monkeypatch)
    monkeypatch.setattr(CatalogService, "roll_back", lambda *a, **k: outcome)

    run_claimed(engine, claimed)

    message = _task(py.world, "gold.scores").error_message
    assert message == set_back_result_message(
        LOST_MESSAGE, outcome, False, table="gold.scores", environment="dev"
    )
    assert SET_BACK_CHANGED not in message and "could not be set back" not in message


@pytest.mark.parametrize("failure", ["task", "unexpected"])
def test_a_set_back_that_cannot_be_prepared_ends_the_task_saying_so(
    py: PyWorld, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """Whatever else goes wrong, the task ends instead of waiting for a claim that fails again."""
    claimed = _lost_after(py, monkeypatch, _writer(py.world, rows=2))
    engine = _builds_nothing(py, monkeypatch)

    def broken(*args: object) -> object:
        if failure == "task":
            raise TaskFailure(FailureKind.PERMANENT, "the run is gone", FailureCode.NOT_FOUND)
        raise RuntimeError("boom")

    monkeypatch.setattr(task_checks_module, "_table_now", broken)

    run_claimed(engine, claimed)

    task = _task(py.world, "gold.scores")
    assert (task.status, task.error_message) == (
        TaskStatus.FAILED,
        set_back_failed_message(LOST_MESSAGE),
    )
    assert task.failure_code == "worker_lost"


def test_a_drain_noticed_while_the_catalog_refuses_releases_the_claim(
    py: PyWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal path looks for a stop; a drain is one, so the claim goes back as it was."""
    claimed = _lost_after(py, monkeypatch, _writer(py.world, rows=2))
    drain = threading.Event()
    keepers: list[LeaseKeeper] = []
    real_keeper_for = tasks_module.keeper_for

    def keeping(engine: RunEngine, claim: ClaimedTask) -> LeaseKeeper:
        keepers.append(real_keeper_for(engine, claim))
        return keepers[-1]

    monkeypatch.setattr(tasks_module, "keeper_for", keeping)
    _builds_nothing(py, monkeypatch)

    def refuse(self: CatalogService, *args: object, **kwargs: object) -> object:
        drain.set()
        keepers[0].renew_once()
        raise PermissionDenied("x")

    monkeypatch.setattr(CatalogService, "roll_back", refuse)
    engine = py.engine(lambda *a: pytest.fail("a process"), drain=drain, grace=0.0)

    run_claimed(engine, claimed)

    task = _task(py.world, "gold.scores")
    assert (task.status, task.setting_back, task.attempt, task.set_back_claims) == (
        TaskStatus.PENDING,
        True,
        1,
        0,
    )


def test_a_task_with_neither_stamp_nor_baseline_has_nothing_to_set_back(
    py: PyWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing records what the table was before the task, so nothing is set back."""
    claimed = _lost_after(py, monkeypatch, _writer(py.world, rows=2))
    engine = _builds_nothing(py, monkeypatch)
    monkeypatch.setattr(task_checks_module, "read_baseline", lambda *a: None)
    monkeypatch.setattr(
        CatalogService, "roll_back", lambda *a, **k: pytest.fail("set back without a record")
    )

    run_claimed(engine, claimed)

    task = _task(py.world, "gold.scores")
    assert (task.status, task.error_message) == (
        TaskStatus.FAILED,
        f"{LOST_MESSAGE} {SET_BACK_NOTHING}",
    )
    assert task.failure_code == "worker_lost"


@dataclass
class FakeEnvironments:
    """Stands in for `DockerEnvironments`, recording what each attempt asked for.

    Parameters
    ----------
    calls
        One entry per `acquire`.
    resolve
        Whether `acquire` calls the index resolver, as a build would.
    error
        Raised by `acquire` instead of handing out a lease.
    """

    calls: list[dict[str, object]] = field(default_factory=list)
    resolve: bool = False
    error: Exception | None = None

    def acquire(
        self,
        lock_sha256: str,
        project: Path,
        on_log: Callable[[bytes], None],
        indexes: IndexResolver | None,
        *,
        python: str,
        attempt: str,
        used: tuple[RegisteredIndex, ...],
        project_id: uuid.UUID,
        stop: Callable[[], bool] | None = None,
    ) -> VenvLease:
        """Record the request and hand out a key.

        Parameters
        ----------
        lock_sha256
            The lock digest.
        project
            The code directory.
        on_log
            The log sink.
        indexes
            The resolver.
        python
            The task's Python.
        attempt
            The attempt directory's name.
        used
            The registered indexes the lock uses.
        project_id
            The project the environment belongs to.
        stop
            Says when the task is to stop; recorded.

        Returns
        -------
        VenvLease
            A lease with a key and nothing to release.

        Raises
        ------
        Exception
            `error`, when set.
        """
        resolved = list(indexes()) if self.resolve and indexes is not None else []
        self.calls.append(
            {
                "python": python,
                "attempt": attempt,
                "project": project,
                "used": used,
                "project_id": project_id,
                "resolved": [access.name for access in resolved],
                "stop": stop,
            }
        )
        if self.error is not None:
            raise self.error
        return VenvLease(path=Path("/env"), python=Path("/env/bin/python"), key="e" * 64)


def _name_python(py: PyWorld, python: str) -> None:
    """Record `python:` in the deployed manifest, as a deploy of such a `lakana.yaml` would.

    Parameters
    ----------
    py
        The world.
    python
        The version.
    """
    with py.world.session() as session:
        version = session.get(JobVersion, py.world.version_id)
        assert version is not None
        version.manifest_json = {**version.manifest_json, "python": python}
        session.commit()


def test_a_docker_worker_builds_the_projects_python_and_the_task_carries_the_key(
    py: PyWorld,
) -> None:
    """The environment comes from the launcher; the venv cache is not asked."""
    py.deploy()
    _name_python(py, "3.12")
    environments = FakeEnvironments()
    engine = py.engine(_writer(py.world), environments=environments)

    run_claimed(engine, py.claim_python(engine))

    (call,) = environments.calls
    assert call["python"] == "3.12"
    assert str(call["attempt"]).startswith("attempt-")
    assert Path(str(call["project"])).name == "code"
    (task,) = py.runner.tasks
    assert (task.environment_key, task.python) == ("e" * 64, "3.12")
    assert py.leases == []
    assert _task(py.world, "gold.scores").status is TaskStatus.SUCCEEDED


def test_the_attempt_the_build_names_is_the_directory_the_task_runs_in(py: PyWorld) -> None:
    """The launcher mounts ``<attempt>/code`` of its work volume; the worker's paths agree.

    The work volume is mounted at the worker's work directory, so an attempt directory must be
    a direct child of it, its code must be ``code`` inside it, and its name must be one the
    launcher accepts.
    """
    py.deploy()
    environments = FakeEnvironments()
    engine = py.engine(_writer(py.world), environments=environments)

    run_claimed(engine, py.claim_python(engine))

    (call,) = environments.calls
    (task,) = py.runner.tasks
    assert task.attempt_dir.parent == (py.root / "work").absolute()
    assert call["attempt"] == task.attempt_dir.name
    assert call["project"] == task.code_dir == task.attempt_dir / "code"
    assert launcher_requests._ATTEMPT.match(str(call["attempt"])) is not None


def test_the_environment_belongs_to_the_project_in_its_environment(py: PyWorld) -> None:
    """The key's project is a UUID of the repository and the environment, never a name.

    Two environments of one repository never share an environment build: a deployer of dev
    could otherwise change vendored code without changing the lock, and prod would run it.
    """
    py.deploy()
    environments = FakeEnvironments()
    engine = py.engine(_writer(py.world), environments=environments)

    run_claimed(engine, py.claim_python(engine))

    with py.world.session() as session:
        version = session.get(JobVersion, py.world.version_id)
        assert version is not None
        repository, environment = version.repository_id, version.environment_id
    (call,) = environments.calls
    assert isinstance(call["project_id"], uuid.UUID)
    assert call["project_id"] == python_tasks_module.environment_owner(repository, environment)
    assert python_tasks_module.environment_owner(repository, uuid.uuid4()) != call["project_id"]
    assert python_tasks_module.environment_owner(uuid.uuid4(), environment) != call["project_id"]


def test_a_docker_build_resolves_only_the_indexes_its_lock_uses(py: PyWorld) -> None:
    """Another registered index with a secret in the same environment is never decrypted."""
    _register_team_index(py, password="the-password")
    with py.world.session() as session:
        other = uuid.uuid4()
        session.add(
            PackageIndex(
                id=other,
                name="other",
                url="https://other.example.org/simple",
                credential_secret="other-index",
            )
        )
        session.commit()
        owner = session.get(Principal, py.world.admin.id)
        assert owner is not None
        SecretService(session, AllowAllAuthorization(), _INDEX_KEY).create(
            PrincipalRef(id=owner.id, name=owner.name, type=owner.type),
            environment="dev",
            name="other-index",
            values={"username": "ci", "password": "other-password"},
            use=SecretUse(SecretUseKind.PACKAGE_INDEX, other, "other"),
        )
    py.deploy(_lock() + _TEAM_PACKAGE)
    environments = FakeEnvironments(resolve=True)
    engine = _with_key(py.engine(_writer(py.world), environments=environments))

    run_claimed(engine, py.claim_python(engine))

    (call,) = environments.calls
    assert [index.name for index in cast(tuple[RegisteredIndex, ...], call["used"])] == ["team"]
    assert call["resolved"] == ["team"]


@pytest.mark.parametrize(
    ("error", "status", "code", "message"),
    [
        (
            LogSinkFailed("the log sink failed"),
            TaskStatus.FAILED,
            "internal",
            "environment build failed: the task's log could not be written while its "
            "environment was built; the worker's log has the cause",
        ),
        (
            VenvBuildRefused("environment build failed: the launcher refused the build: no"),
            TaskStatus.FAILED,
            "secret_refused",
            "environment build failed: the launcher refused the build: no",
        ),
        (
            VenvBuildError("runner unavailable: the launcher is busy"),
            TaskStatus.PENDING,
            "build_failed",
            "runner unavailable: the launcher is busy",
        ),
        (
            VenvBuildBroken("environment build failed: the package brokenpkg could not be built"),
            TaskStatus.FAILED,
            "build_failed",
            "environment build failed: the package brokenpkg could not be built",
        ),
    ],
)
def test_a_failed_docker_build_ends_the_task_with_its_code(
    py: PyWorld, error: Exception, status: TaskStatus, code: str, message: str
) -> None:
    """A sink that fails while the build logs ends the task; the worker goes on.

    Parameters
    ----------
    py
        The world.
    error
        What `acquire` raises.
    status
        The task's status afterwards.
    code
        Its failure code.
    message
        The attempt's message.
    """
    py.deploy()
    engine = py.engine(_forged, environments=FakeEnvironments(error=error))

    task, recorded = _fails(py, engine)

    assert (task.status, task.failure_code, recorded) == (status, code, message)
    assert py.runner.tasks == [] and py.tokens() == []


def test_a_project_that_names_another_python_is_refused_on_the_local_runner(py: PyWorld) -> None:
    """The local runner has one interpreter: its own."""
    py.deploy()
    other = "3.12" if sys.version_info[:2] != (3, 12) else "3.13"
    _name_python(py, other)

    task, message = _fails(py, py.engine(_forged))

    own = f"{sys.version_info.major}.{sys.version_info.minor}"
    assert task.status is TaskStatus.FAILED
    assert task.failure_code == "no_runtime"
    assert message == (
        f"this project names Python {other} in lakana.yaml, and this worker runs Python tasks "
        f"on its own Python {own} without containers (LAKANA_RUN__RUNNER=local). Run it on a "
        f'worker with LAKANA_RUN__RUNNER=docker, or set python: "{own}" (or remove it) in '
        "lakana.yaml and deploy again"
    )
    assert py.runner.tasks == []


def test_a_project_that_names_the_workers_own_python_runs_on_the_local_runner(
    py: PyWorld,
) -> None:
    """Naming the version the worker runs is no reason to refuse."""
    py.deploy()
    _name_python(py, f"{sys.version_info.major}.{sys.version_info.minor}")
    engine = py.engine(_writer(py.world))

    run_claimed(engine, py.claim_python(engine))

    (task,) = py.runner.tasks
    assert task.python == f"{sys.version_info.major}.{sys.version_info.minor}"
    assert task.environment_key is None


def test_a_project_without_python_runs_on_the_workers_python(py: PyWorld) -> None:
    """No `python:` and either runner: the worker's own version."""
    py.deploy()
    environments = FakeEnvironments()
    engine = py.engine(_writer(py.world), environments=environments)

    run_claimed(engine, py.claim_python(engine))

    assert environments.calls[0]["python"] == f"{sys.version_info.major}.{sys.version_info.minor}"


def test_requires_python_is_checked_against_the_python_the_project_names(py: PyWorld) -> None:
    """Under docker the lock must admit the named Python, and the message names lakana.yaml."""
    py.deploy(_lock().replace(b'requires-python = ">=3.11"', b'requires-python = ">=3.13"'))
    _name_python(py, "3.12")
    environments = FakeEnvironments()

    task, message = _fails(py, py.engine(_forged, environments=environments))

    assert task.status is TaskStatus.FAILED
    assert (
        "uv.lock: requires-python '>=3.13' does not admit Python 3.12, which python: in "
        "lakana.yaml names" in message
    )
    assert environments.calls == []


def _protect(py: PyWorld, environment: str) -> None:
    """Mark an environment protected, as `lakana env protect` does.

    Parameters
    ----------
    py
        The world.
    environment
        Its name.
    """
    with py.world.session() as session:
        row = session.execute(
            select(Environment).where(Environment.name == environment)
        ).scalar_one()
        row.is_protected = True
        session.commit()


def test_a_protected_environment_refuses_python_tasks_on_the_local_runner(py: PyWorld) -> None:
    """The spec: a protected environment refuses Python tasks on a local-runner worker."""
    py.deploy()
    engine = py.engine(_forged)
    claimed = py.claim_python(engine)
    _protect(py, "dev")

    run_claimed(engine, claimed)

    message = _attempts(py.world, claimed.task_run_id)[0].error_message
    assert message == (
        "environment 'dev' is protected, and this worker runs Python tasks without isolation "
        "(LAKANA_RUN__RUNNER=local). Run the task on a worker with LAKANA_RUN__RUNNER=docker; "
        'the how-to "Run Python tasks in containers" says how.'
    )
    task = _task(py.world, claimed.asset)
    assert task.status is TaskStatus.FAILED
    assert task.failure_code == "no_runtime"
    assert py.runner.tasks == []


def test_a_protected_environment_runs_python_tasks_in_containers(py: PyWorld) -> None:
    """With DockerRunner the same task reaches its runner."""
    py.deploy()
    engine = py.engine(_writer(py.world), environments=FakeEnvironments())
    claimed = py.claim_python(engine)
    _protect(py, "dev")

    run_claimed(engine, claimed)

    assert len(py.runner.tasks) == 1


def _docker_runner() -> DockerRunner:
    """Build a `DockerRunner` that never reaches a launcher.

    Returns
    -------
    DockerRunner
        The runner.
    """
    return DockerRunner(LauncherClient(Path("l.sock")), owner="w", memory_mb=64, cpus=1, pids=16)


def test_a_runtime_is_isolated_exactly_when_its_runner_runs_containers(py: PyWorld) -> None:
    """`isolated` follows the runner: the docker one is, the local one is not."""
    docker = py.runtime(_forged, runner=_docker_runner(), environments=FakeEnvironments())
    local = py.runtime(_forged, runner=LocalProcessRunner(kill_grace=0.2))

    assert docker.isolated is True
    assert local.isolated is False


def test_a_container_runner_without_environments_is_refused(py: PyWorld) -> None:
    """A container runner needs the launcher's environments: no silent local builds."""
    with pytest.raises(ValueError, match="runs tasks in containers but has no environments"):
        py.runtime(_forged, runner=_docker_runner())


def test_a_local_runner_with_environments_is_refused(py: PyWorld) -> None:
    """The local runner with the launcher's environments would look isolated and not be."""
    with pytest.raises(ValueError, match="runs tasks without containers but has environments"):
        py.runtime(
            _forged, runner=LocalProcessRunner(kill_grace=0.2), environments=FakeEnvironments()
        )


def test_a_retried_attempt_of_a_protected_environment_is_refused_on_the_local_runner(
    py: PyWorld,
) -> None:
    """Protected between two attempts: the retry is refused before any code runs."""

    def broken(
        settings: VenvSettings,
        sha: str,
        project: Path,
        on_log: Callable[[bytes], None],
        indexes: IndexResolver | None = None,
        *,
        stop: Callable[[], bool] | None = None,
    ) -> VenvLease:
        raise VenvBuildError("environment build failed: uv exited with status 2")

    engine = py.engine(_forged, acquire_venv=broken)
    run_claimed(engine, py.claim_python(engine))
    assert _task(py.world, "gold.scores").status is TaskStatus.PENDING
    _protect(py, "dev")

    engine = py.engine(_writer(py.world))
    second = _next_attempt(py)
    run_claimed(engine, second)

    task = _task(py.world, "gold.scores")
    assert (task.status, task.attempt) == (TaskStatus.FAILED, 2)
    assert task.failure_code == "no_runtime"
    assert task.error_message is not None and "environment 'dev' is protected" in task.error_message
    assert py.runner.tasks == []


def test_a_python_task_with_checks_of_a_protected_environment_is_refused_on_the_local_runner(
    py: PyWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Checks change nothing: refused before the task runs, so nothing is stamped or checked."""
    _with_checks(py, _NO_ROWS)
    stamps = _stamps(monkeypatch)
    engine = py.engine(_writer(py.world, rows=2))
    claimed = py.claim_python(engine)
    assert claimed.has_checks
    _protect(py, "dev")

    run_claimed(engine, claimed)

    task = _task(py.world, "gold.scores")
    assert task.status is TaskStatus.FAILED
    assert task.failure_code == "no_runtime"
    assert py.runner.tasks == []
    assert stamps == []
    with py.world.session() as session:
        assert results_of_task(session, claimed.task_run_id) == []


def _harness_writes(world: World, *batches: list[int]) -> Script:
    """Build a script that writes as the harness does for the task's write mode.

    An append against a snapshot appends, a merge merges on the task's key, and anything else
    (a replace, the first run of an append) replaces; each call writes the next batch of ids.

    Parameters
    ----------
    world
        The world.
    *batches
        The ids each call writes, in order.

    Returns
    -------
    Script
        The script.
    """
    calls = iter(batches)

    def script(task: PythonTask, on_log: Callable[[bytes], None], stop: StopSignal) -> RunOutcome:
        data = pa.table({"id": pa.array(next(calls), pa.int64())})
        parsed = parse_token(task.token, "lakana_task")
        assert parsed is not None
        expected = cast("ExpectedSnapshot", task.expected_snapshot_id)
        with world.session() as session:
            principal = TaskTokenService(session, AllowAllAuthorization()).verify(*parsed).principal
            catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
            ref = TableRef.from_dotted("dev", task.table)
            try:
                if task.write == "merge":
                    merged = catalog.merge(
                        principal, ref, data, key=task.key, expected_snapshot_id=expected
                    )
                    rows = merged.rows_inserted + merged.rows_updated
                    return RunOutcome(succeeded=True, snapshot_id=merged.snapshot_id, rows=rows)
                if task.write == "append" and expected != "none":
                    appended = catalog.append(principal, ref, data, expected_snapshot_id=expected)
                    return RunOutcome(
                        succeeded=True, snapshot_id=appended.snapshot_id, rows=appended.rows
                    )
                written = catalog.replace(principal, ref, data, expected_snapshot_id=expected)
            except ConflictError:
                return RunOutcome(
                    succeeded=False,
                    error_message="gold.scores changed during the write; retry.",
                    conflict=True,
                    failure_code=FailureCode.CONFLICT,
                )
        return RunOutcome(succeeded=True, snapshot_id=written.snapshot_id, rows=written.rows)

    return script


def _rerun(py: PyWorld, engine: RunEngine) -> ClaimedTask:
    """Start a second run of the project and run its Python task.

    Parameters
    ----------
    py
        The world.
    engine
        The engine.

    Returns
    -------
    ClaimedTask
        The second run's claim of the Python task.
    """
    with py.world.session() as session:
        scheduler_tick(session, datetime.now(UTC), lambda: 0.5)
    claimed = py.claim_python(engine)
    run_claimed(engine, claimed)
    return claimed


def _snapshot(world: World, dotted: str = "gold.scores") -> int | None:
    """Read a table's current snapshot through the catalog.

    Parameters
    ----------
    world
        The world.
    dotted
        The table.

    Returns
    -------
    int or None
        Its current snapshot id.
    """
    with world.session() as session:
        return (
            CatalogService(session, StorageResolver(session), AllowAllAuthorization())
            .load_table(world.admin, TableRef.from_dotted("dev", dotted))
            .metadata.current_snapshot_id
        )


def test_an_appending_python_asset_gets_its_watermark_and_the_pin(py: PyWorld) -> None:
    """The first run creates the table; a later one appends against the watermark's snapshot."""
    py.deploy(write="append", watermark="id", write_mode="append")
    engine = py.engine(_harness_writes(py.world, [1, 2], [3]))
    first = py.claim_python(engine)
    run_claimed(engine, first)
    pinned = _snapshot(py.world)
    second = _rerun(py, engine)

    one, two = py.runner.tasks
    assert (one.write, one.watermark_column, one.watermark) == ("append", "id", None)
    assert one.expected_snapshot_id == "none"
    assert (two.write, two.watermark_column, two.watermark) == ("append", "id", "long:2")
    assert pinned is not None
    assert two.expected_snapshot_id == pinned
    assert _ids(py.world, "gold.scores") == [1, 2, 3]
    assert _task(py.world, "gold.scores").status is TaskStatus.SUCCEEDED
    log = _log(py.world, second.run_id, "gold.scores")
    assert "write mode: append" in log
    assert "watermark: long:2" in log
    assert "write mode: replace" not in _log(py.world, first.run_id, "gold.scores")


def test_a_table_without_a_snapshot_is_sent_as_an_existing_table(py: PyWorld) -> None:
    """A first run of zero rows created the table without a snapshot; the next run is no first."""
    py.deploy(write="append", watermark="id", write_mode="append")
    engine = py.engine(_harness_writes(py.world, [], [1]))
    run_claimed(engine, py.claim_python(engine))
    assert _snapshot(py.world) is None
    _rerun(py, engine)

    first, second = py.runner.tasks
    assert (first.table_exists, first.expected_snapshot_id) == (False, "none")
    assert (second.table_exists, second.expected_snapshot_id) == (True, "none")


def test_a_merging_python_asset_gets_its_key(py: PyWorld) -> None:
    py.deploy(write="merge", key=["id"], write_mode="merge")
    engine = py.engine(_harness_writes(py.world, [1, 2], [2, 3]))
    run_claimed(engine, py.claim_python(engine))
    claimed = _rerun(py, engine)

    one, two = py.runner.tasks
    assert (one.write, one.key, one.watermark_column) == ("merge", ("id",), None)
    assert one.expected_snapshot_id == "none"
    assert isinstance(two.expected_snapshot_id, int)
    assert _ids(py.world, "gold.scores") == [1, 2, 3]
    assert _task(py.world, "gold.scores").status is TaskStatus.SUCCEEDED
    assert "write mode: merge" in _log(py.world, claimed.run_id, "gold.scores")


def test_a_target_changed_after_the_watermark_was_read_is_retried_with_a_new_watermark(
    py: PyWorld,
) -> None:
    """A writer between the worker's read and the upload: the free retry reads it again.

    The other writer's rows are then below the new watermark, so they are not appended twice.
    """
    py.deploy(write="append", watermark="id", write_mode="append")
    engine = py.engine(_harness_writes(py.world, [1, 2]))
    run_claimed(engine, py.claim_python(engine))
    writes = _harness_writes(py.world, [5], [6])

    def interleaved(
        task: PythonTask, on_log: Callable[[bytes], None], stop: StopSignal
    ) -> RunOutcome:
        if len(py.runner.tasks) == 1:
            with py.world.session() as session:
                CatalogService(session, StorageResolver(session), AllowAllAuthorization()).append(
                    py.world.admin,
                    TableRef.from_dotted("dev", "gold.scores"),
                    pa.table({"id": pa.array([3, 4], pa.int64())}),
                )
        return writes(task, on_log, stop)

    engine = py.engine(interleaved)
    with py.world.session() as session:
        scheduler_tick(session, datetime.now(UTC), lambda: 0.5)
    run_claimed(engine, py.claim_python(engine))
    task = _task(py.world, "gold.scores")
    assert (task.status, task.attempt, task.conflict_retried) == (TaskStatus.PENDING, 0, True)
    assert _ids(py.world, "gold.scores") == [1, 2, 3, 4]

    run_claimed(engine, _next_attempt(py))

    first, retried = py.runner.tasks
    assert (first.watermark, retried.watermark) == ("long:2", "long:4")
    assert _task(py.world, "gold.scores").status is TaskStatus.SUCCEEDED
    assert _ids(py.world, "gold.scores") == [1, 2, 3, 4, 6]


def test_a_write_between_the_state_read_and_the_watermark_read_is_a_conflict(
    py: PyWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pin is the state the attempt is judged against, so a foreign write never passes.

    Otherwise the other writer's snapshot would count as this attempt's write.
    """
    py.deploy(write="append", watermark="id", write_mode="append")
    engine = py.engine(_harness_writes(py.world, [1, 2]))
    run_claimed(engine, py.claim_python(engine))
    real = CatalogService.read_watermark

    def after_a_write(self: CatalogService, *args: object) -> object:
        CatalogService(
            self._session, StorageResolver(self._session), AllowAllAuthorization()
        ).append(
            py.world.admin,
            TableRef.from_dotted("dev", "gold.scores"),
            pa.table({"id": pa.array([3], pa.int64())}),
        )
        self._session.commit()
        return real(self, *args)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(CatalogService, "read_watermark", after_a_write)
    with py.world.session() as session:
        scheduler_tick(session, datetime.now(UTC), lambda: 0.5)
    claimed = py.claim_python(engine)
    run_claimed(engine, claimed)

    assert len(py.runner.tasks) == 1  # no process for the second run's first attempt
    task = _task(py.world, "gold.scores")
    assert (task.status, task.attempt, task.conflict_retried) == (TaskStatus.PENDING, 0, True)
    assert _attempts(py.world, claimed.task_run_id)[0].failure_code == FailureCode.CONFLICT.value


@pytest.mark.parametrize("write", ["append", "merge"])
def test_an_empty_append_is_a_success(py: PyWorld, write: str) -> None:
    """Review Focus 2: a run that found nothing new leaves the table as it was and succeeds."""
    extra: dict[str, object] = (
        {"watermark": "id", "write_mode": "append"}
        if write == "append"
        else {"key": ["id"], "write_mode": "merge"}
    )
    py.deploy(entry={"write": write, **extra})
    engine = py.engine(_harness_writes(py.world, [1]))
    run_claimed(engine, py.claim_python(engine))
    before = _snapshot(py.world)
    nothing = py.engine(lambda _t, _l, _s: RunOutcome(succeeded=True, snapshot_id=None, rows=0))

    claimed = _rerun(py, nothing)

    task = _task(py.world, "gold.scores")
    assert task.status is TaskStatus.SUCCEEDED, task.error_message
    attempt = _attempts(py.world, claimed.task_run_id)[0]
    assert (attempt.outcome, attempt.rows_written) == (AttemptOutcome.SUCCEEDED, 0)
    assert _snapshot(py.world) == before
    log = _log(py.world, claimed.run_id, "gold.scores")
    assert f"write mode: {write}" in log
    assert "snapshot:" not in log
    assert _ids(py.world, "gold.scores") == [1]


@pytest.mark.parametrize("write", ["append", "merge"])
def test_an_empty_append_while_somebody_else_writes_is_still_a_success(
    py: PyWorld, write: str
) -> None:
    """Review S1: a run that found nothing new wrote nothing; another writer's commit is theirs.

    Parameters
    ----------
    py
        The world.
    write
        The asset's write mode.
    """
    extra: dict[str, object] = (
        {"watermark": "id", "write_mode": "append"}
        if write == "append"
        else {"key": ["id"], "write_mode": "merge"}
    )
    py.deploy(entry={"write": write, **extra})
    engine = py.engine(_harness_writes(py.world, [1]))
    run_claimed(engine, py.claim_python(engine))

    def foreign(task: PythonTask, on_log: Callable[[bytes], None], stop: StopSignal) -> RunOutcome:
        _write_as_admin(py.world, 1, append=True)
        return RunOutcome(succeeded=True, snapshot_id=None, rows=0)

    claimed = _rerun(py, py.engine(foreign))

    task = _task(py.world, "gold.scores")
    assert task.status is TaskStatus.SUCCEEDED, task.error_message
    attempt = _attempts(py.world, claimed.task_run_id)[0]
    assert (attempt.outcome, attempt.rows_written) == (AttemptOutcome.SUCCEEDED, 0)
    assert "snapshot:" not in _log(py.world, claimed.run_id, "gold.scores")


def _nothing_new(task: PythonTask, on_log: Callable[[bytes], None], stop: StopSignal) -> RunOutcome:
    """Report what an append or merge whose source had nothing new reports.

    Parameters
    ----------
    task
        The task.
    on_log
        The log sink.
    stop
        The stop signal.

    Returns
    -------
    RunOutcome
        Success with no rows and no snapshot.
    """
    return RunOutcome(succeeded=True, snapshot_id=None, rows=0)


def test_an_empty_retry_after_an_attempt_that_wrote_is_set_back_when_its_check_fails(
    py: PyWorld,
) -> None:
    """Attempt 1 appended and failed, attempt 2 found nothing new, and its check failed.

    The table goes back to before attempt 1: the empty success still names what the run wrote.
    """
    py.deploy(write="append", watermark="id", write_mode="append")
    _published_before(py)
    _with_checks(py, _NO_ROWS)
    engine = py.engine(_writes_then_fails(py.world, rows=2))
    first = py.claim_python(engine)
    run_claimed(engine, first)
    assert _ids(py.world, "gold.scores") == [0, 1]
    with py.world.session() as session:
        baseline = read_baseline(session, first.task_run_id)
    assert baseline is not None and baseline.publishes is False

    second = _next_attempt(py)
    run_claimed(py.engine(_nothing_new), second)

    task = _task(py.world, "gold.scores")
    assert (task.status, task.attempt) == (TaskStatus.FAILED, 2)
    assert task.error_message is not None and _SET_BACK in task.error_message
    assert _ids(py.world, "gold.scores") == [0]
    (rolled,) = _rollbacks(py)
    assert (rolled["outcome"], rolled["before"]) == ("rolled_back", baseline.snapshot_id)


def test_an_empty_retry_after_an_attempt_that_created_the_table_ends_not_published(
    py: PyWorld,
) -> None:
    """The table did not exist before the task run: nothing that failed its check is published."""
    py.deploy(write="append", watermark="id", write_mode="append")
    _with_checks(py, _NO_ROWS)
    engine = py.engine(_writes_then_fails(py.world, rows=2))
    first = py.claim_python(engine)
    run_claimed(engine, first)
    with py.world.session() as session:
        assert read_baseline(session, first.task_run_id) == Baseline(None, True)

    second = _next_attempt(py)
    run_claimed(py.engine(_nothing_new), second)

    task = _task(py.world, "gold.scores")
    assert task.status is TaskStatus.FAILED
    assert task.error_message is not None and _NOT_PUBLISHED in task.error_message
    assert _marked_by(py) == second.run_id
    (rolled,) = _rollbacks(py)
    assert (rolled["outcome"], rolled["before"]) == ("unpublished", None)


def test_an_empty_success_over_rows_that_fail_a_check_says_nothing_was_set_back(
    py: PyWorld,
) -> None:
    """The check fails on rows of an earlier run; this run wrote nothing, so nothing goes back."""
    py.deploy(write="append", watermark="id", write_mode="append")
    _published_before(py)
    _with_checks(py, _NO_ROWS)

    claimed = _rerun(py, py.engine(_nothing_new))

    task = _task(py.world, "gold.scores")
    assert task.status is TaskStatus.FAILED
    assert task.error_message is not None
    assert task.error_message.startswith(
        "Check `gold.scores/no_rows` failed: 1 row (checks/gold/scores/no_rows.sql). "
        "The table was not changed by this run, and nothing downstream was built."
    )
    assert "rollback failed" not in task.error_message.lower()
    with py.world.session() as session:
        (result,) = results_of_task(session, claimed.task_run_id)
    assert result.rollback_outcome is RollbackOutcome.NOTHING_WRITTEN
    assert _ids(py.world, "gold.scores") == [0]
    assert _rollbacks(py) == []


def test_an_empty_success_of_a_run_that_wrote_nothing_has_nothing_to_set_back(
    py: PyWorld,
) -> None:
    """Without an earlier write in the run the empty success names no write.

    The checks run on the table as it is.
    """
    py.deploy(write="append", watermark="id", write_mode="append")
    _published_before(py)
    _with_checks(py, _NO_NULLS)
    claimed = _rerun(py, py.engine(_nothing_new))
    assert _task(py.world, "gold.scores").status is TaskStatus.SUCCEEDED
    assert _attempts(py.world, claimed.task_run_id)[0].rows_written == 0
    assert _ids(py.world, "gold.scores") == [0]
    assert _rollbacks(py) == []


@pytest.mark.parametrize(
    "claim",
    [
        RunOutcome(succeeded=True, snapshot_id=None, rows=3),
        RunOutcome(succeeded=True, snapshot_id=42, rows=0),
    ],
    ids=["claims-rows", "claims-a-snapshot"],
)
def test_an_append_that_claims_a_write_it_did_not_make_still_fails(
    py: PyWorld, claim: RunOutcome
) -> None:
    py.deploy(write="append", watermark="id", write_mode="append")
    engine = py.engine(_harness_writes(py.world, [1]))
    run_claimed(engine, py.claim_python(engine))

    claimed = _rerun(py, py.engine(lambda _t, _l, _s: claim))

    task = _task(py.world, "gold.scores")
    assert (task.status, task.failure_code) == (TaskStatus.FAILED, "build_failed")
    message = _attempts(py.world, claimed.task_run_id)[0].error_message
    assert message is not None and message.startswith(_NO_WRITE)


def test_an_empty_replace_without_a_write_still_fails(py: PyWorld) -> None:
    """Only an append or a merge may find nothing to write: a replace always writes."""
    engine = py.engine(_writer(py.world))
    run_claimed(engine, py.claim_python(engine))

    claimed = _rerun(py, py.engine(lambda _t, _l, _s: RunOutcome(succeeded=True, rows=0)))

    assert _task(py.world, "gold.scores").status is TaskStatus.FAILED
    message = _attempts(py.world, claimed.task_run_id)[0].error_message
    assert message is not None and message.startswith(_NO_WRITE)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        (
            {"write": "upsert"},
            "the project version's write mode 'upsert' for gold.scores is not valid; deploy it "
            "again with this release",
        ),
        (
            {"write": "merge", "key": []},
            "the project version merges gold.scores without a key; deploy it again with this "
            "release",
        ),
        (
            {"write": "append", "watermark": None},
            "the project version appends to gold.scores without a watermark; deploy it again "
            "with this release",
        ),
    ],
    ids=["unknown", "merge-without-key", "append-without-watermark"],
)
def test_a_write_no_run_may_do_is_never_run_as_a_replace(
    py: PyWorld, overrides: dict[str, object], message: str
) -> None:
    py.deploy(entry=overrides)
    task, text = _fails(py, py.engine(_writer(py.world)))
    assert (task.status, task.attempt, task.failure_code) == (TaskStatus.FAILED, 1, "code_changed")
    assert text == message
    assert py.runner.tasks == []


def _connection(
    py: PyWorld,
    name: str = "crm-db",
    kind: ConnectionKind = ConnectionKind.POSTGRESQL,
    address: dict[str, object] | None = None,
    settings: dict[str, object] | None = None,
    environment: str = "dev",
) -> None:
    """Add a connection to an environment; a PostgreSQL one at db.example.com:5432 by default.

    Parameters
    ----------
    py
        The world.
    name
        Its name.
    kind
        Its kind.
    address
        Its address; the default PostgreSQL one when `None`.
    settings
        Its settings; none when `None`.
    environment
        The environment, which must exist.
    """
    with py.world.session() as session:
        env = session.execute(
            select(Environment).where(Environment.name == environment)
        ).scalar_one()
        session.add(
            Connection(
                id=uuid.uuid4(),
                environment_id=env.id,
                name=name,
                kind=kind,
                address=address
                if address is not None
                else {"host": "db.example.com", "port": 5432, "database": "crm"},
                settings=settings or {},
                created_by=py.world.admin.id,
            )
        )
        session.commit()


def test_a_docker_task_is_routed_to_every_declared_connection(py: PyWorld) -> None:
    """Proxy pairs, relay ports in declaration order, roots; routing in the description."""
    _connection(py)  # crm-db: postgresql db.example.com:5432
    _connection(
        py,
        "api",
        ConnectionKind.HTTP,
        {"base_url": "https://api.example.com"},
        {"auth": "oauth2", "token_url": "https://login.example.com/token"},
    )
    _connection(py, "exports", ConnectionKind.DIRECTORY, {"root": "shared", "path": "in"}, {})
    _connection(py, "erp", ConnectionKind.TCP, {"host": "erp.example.com", "port": 1433}, {})
    _connection(py, "files", ConnectionKind.SQLITE_FILE, {"root": "shared", "path": "a.db"}, {})
    _connection(py, "api2", ConnectionKind.HTTP, {"base_url": "https://api.example.com/v2"}, {})
    py.deploy(connections=["crm-db", "api", "exports", "erp", "files", "api2"])
    engine = py.engine(_writer(py.world), environments=FakeEnvironments())
    run_claimed(engine, py.claim_python(engine))

    (task,) = py.runner.tasks
    assert task.egress == (("api.example.com", 443), ("login.example.com", 443))
    assert task.relay == ((15001, "db.example.com", 5432), (15002, "erp.example.com", 1433))
    assert (task.roots, dict(task.root_users)) == (("shared",), {"shared": "exports"})
    assert task.connections == {
        "crm-db": {"kind": "postgresql", "grant": [["db.example.com", 5432]], "relay_port": 15001},
        "api": {"kind": "http", "grant": [["api.example.com", 443], ["login.example.com", 443]]},
        "exports": {"kind": "directory", "root": "shared", "root_dir": "/ingest/shared",
                    "path": "in", "root_path": "/ingest/shared/in"},
        "erp": {"kind": "tcp", "grant": [["erp.example.com", 1433]], "relay_port": 15002},
        "files": {"kind": "sqlite-file", "root": "shared", "root_dir": "/ingest/shared",
                  "path": "a.db", "root_path": "/ingest/shared/a.db"},
        "api2": {"kind": "http", "grant": [["api.example.com", 443]]},
    }  # fmt: skip
    assert _task(py.world, "gold.scores").status is TaskStatus.SUCCEEDED


def test_a_connection_of_the_same_name_in_another_environment_grants_nothing(
    py: PyWorld,
) -> None:
    """Only the run's own environment's connection reaches the token, the relay and the grant."""
    with py.world.session() as session:
        session.add(Environment(id=uuid.uuid4(), name="prod", storage_root="file:///prod"))
        session.commit()
    _connection(py)
    _connection(py, address={"host": "prod-db.example.com", "port": 6432}, environment="prod")
    _connection(py, "api", ConnectionKind.HTTP, {"base_url": "https://api.example.com"}, {})
    _connection(
        py, "api", ConnectionKind.HTTP, {"base_url": "https://prod.example.com"}, {}, "prod"
    )
    py.deploy(connections=["crm-db", "api"])
    engine = py.engine(_writer(py.world), environments=FakeEnvironments())
    run_claimed(engine, py.claim_python(engine))

    (task,) = py.runner.tasks
    assert task.relay == ((15001, "db.example.com", 5432),)
    assert task.egress == (("api.example.com", 443),)
    assert task.connections["crm-db"]["grant"] == [["db.example.com", 5432]]
    assert task.connections["api"]["grant"] == [["api.example.com", 443]]


def test_a_task_without_connections_sends_empty_lists(py: PyWorld) -> None:
    engine = py.engine(_writer(py.world), environments=FakeEnvironments())
    run_claimed(engine, py.claim_python(engine))

    (task,) = py.runner.tasks
    assert (task.egress, task.relay, task.roots, dict(task.root_users)) == ((), (), (), {})
    assert task.connections == {}


def test_the_local_runner_routes_directly_and_reads_its_roots(py: PyWorld, tmp_path: Path) -> None:
    """No proxy, no relay: the grant still travels, and a root is the worker's own directory."""
    _connection(py)
    _connection(py, "exports", ConnectionKind.DIRECTORY, {"root": "shared", "path": "in"}, {})
    _connection(py, "api", ConnectionKind.HTTP, {"base_url": "https://api.example.com"}, {})
    py.deploy(connections=["crm-db", "exports", "api"])
    engine = py.engine(_writer(py.world), ingestion_roots={"shared": str(tmp_path)})
    run_claimed(engine, py.claim_python(engine))

    (task,) = py.runner.tasks
    assert (task.egress, task.relay) == ((), ())
    assert (task.roots, dict(task.root_users)) == (("shared",), {"shared": "exports"})
    assert task.connections == {
        "crm-db": {"kind": "postgresql", "grant": [["db.example.com", 5432]]},
        "exports": {"kind": "directory", "root": "shared", "root_dir": str(tmp_path),
                    "path": "in", "root_path": str(tmp_path / "in")},
        "api": {"kind": "http", "grant": [["api.example.com", 443]]},
    }  # fmt: skip


_ROOT_TEXT = (
    "`exports` names ingestion root `shared`, which this worker does not have. Set it in "
    "`LAKANA_LAUNCHER__INGESTION_ROOTS` (Docker runner) or `LAKANA_RUN__INGESTION_ROOTS` "
    "(local runner)."
)


def test_a_root_the_local_worker_does_not_have_fails_before_any_process(py: PyWorld) -> None:
    _connection(py, "exports", ConnectionKind.DIRECTORY, {"root": "shared", "path": "in"}, {})
    py.deploy(connections=["exports"])
    task, message = _fails(py, py.engine(_writer(py.world), ingestion_roots={"other": "/srv/x"}))

    assert (task.status, task.attempt, task.failure_code) == (TaskStatus.FAILED, 1, "no_runtime")
    assert message == _ROOT_TEXT
    assert py.runner.tasks == []


def test_too_many_relay_endpoints_fail_naming_the_cap(py: PyWorld) -> None:
    """A stored manifest is untrusted: one with more connections than a deploy allows."""
    names = [f"db-{n:02d}" for n in range(18)]
    for n, name in enumerate(names):
        _connection(py, name, ConnectionKind.TCP, {"host": f"h{n}.example.com", "port": 5432}, {})
    py.deploy(connections=names)
    task, message = _fails(py, py.engine(_writer(py.world), environments=FakeEnvironments()))

    assert (task.status, task.attempt, task.failure_code) == (TaskStatus.FAILED, 1, "no_runtime")
    assert message == (
        "asset `gold.scores` reaches 18 database endpoints; a task's relay forwards at most 16 "
        "— split the asset"
    )
    assert py.runner.tasks == []


def test_relay_endpoints_beyond_the_cap_are_fine_on_the_local_runner(py: PyWorld) -> None:
    """The local runner has no relay, so its cap does not apply."""
    names = [f"db-{n:02d}" for n in range(17)]
    for n, name in enumerate(names):
        _connection(py, name, ConnectionKind.TCP, {"host": f"h{n}.example.com", "port": 5432}, {})
    py.deploy(connections=names)
    engine = py.engine(_writer(py.world))
    run_claimed(engine, py.claim_python(engine))

    (task,) = py.runner.tasks
    assert task.relay == ()
    assert "relay_port" not in task.connections["db-16"]


def test_too_many_egress_hosts_fail_naming_the_cap(py: PyWorld) -> None:
    names = [f"api-{n:02d}" for n in range(35)]
    for n, name in enumerate(names):
        _connection(
            py,
            name,
            ConnectionKind.HTTP,
            {"base_url": f"https://a{n}.example.com"},
            {"auth": "oauth2", "token_url": f"https://t{n}.example.com/token"},
        )
    py.deploy(connections=names)
    task, message = _fails(py, py.engine(_writer(py.world), environments=FakeEnvironments()))

    assert (task.status, task.attempt, task.failure_code) == (TaskStatus.FAILED, 1, "no_runtime")
    assert message == (
        "asset `gold.scores` reaches 70 hosts through the egress proxy; a task may reach at "
        "most 64 — split the asset"
    )


def test_too_many_ingestion_roots_fail_naming_the_cap(py: PyWorld) -> None:
    names = [f"dir-{n:02d}" for n in range(17)]
    for n, name in enumerate(names):
        _connection(py, name, ConnectionKind.DIRECTORY, {"root": f"r{n}", "path": "."}, {})
    py.deploy(connections=names)
    task, message = _fails(py, py.engine(_writer(py.world), environments=FakeEnvironments()))

    assert (task.status, task.attempt, task.failure_code) == (TaskStatus.FAILED, 1, "no_runtime")
    assert message == (
        "asset `gold.scores` reads 17 ingestion roots; a task mounts at most 16 — split the asset"
    )


def test_the_task_carries_the_kind_of_every_declared_connection(py: PyWorld) -> None:
    """Even for a replace: a helper of the task refuses a connection it is not told about."""
    _connection(py)
    py.deploy(connections=["crm-db"])
    engine = py.engine(_writer(py.world))
    run_claimed(engine, py.claim_python(engine))
    (task,) = py.runner.tasks
    assert task.write == "replace"
    assert task.connections == {
        "crm-db": {"kind": "postgresql", "grant": [["db.example.com", 5432]]}
    }
    assert _task(py.world, "gold.scores").status is TaskStatus.SUCCEEDED


@pytest.mark.parametrize(
    ("entry", "evolves"),
    [
        ({}, False),
        ({"evolve_schema": False}, False),
        ({"evolve_schema": True}, True),
        ({"evolve_schema": "true"}, False),
        ({"evolve_schema": True, "write": "append", "watermark": "id"}, False),
    ],
    ids=["default", "off", "on", "not-a-boolean", "append"],
)
def test_the_task_evolves_the_schema_only_for_a_replace_that_opted_in(
    py: PyWorld, entry: dict[str, object], evolves: bool
) -> None:
    """Deploy refuses `evolve_schema` with an append; a manifest with it there never evolves."""
    py.deploy(entry=entry)
    engine = py.engine(_writer(py.world))
    run_claimed(engine, py.claim_python(engine))
    (task,) = py.runner.tasks
    assert task.evolve_schema is evolves


def test_a_connection_deleted_after_the_attempt_was_prepared_hides_nothing(
    py: PyWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its row is gone, so it has no secret to hide; the task's own fetch is refused later."""
    _connection(py)
    py.deploy(connections=["crm-db"])
    real = python_tasks_module._connection_secrets

    def after_a_delete(session: Session, *args: object) -> tuple[str, ...]:
        session.execute(sqlalchemy_delete(Connection).where(Connection.name == "crm-db"))
        session.commit()
        return real(session, *args)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(python_tasks_module, "_connection_secrets", after_a_delete)
    engine = py.engine(_writer(py.world))
    run_claimed(engine, py.claim_python(engine))

    (task,) = py.runner.tasks
    assert task.connections == {
        "crm-db": {"kind": "postgresql", "grant": [["db.example.com", 5432]]}
    }
    assert task.hidden == ()
    assert _task(py.world, "gold.scores").status is TaskStatus.SUCCEEDED


def test_an_asset_without_connections_is_told_of_none(py: PyWorld) -> None:
    _connection(py)
    engine = py.engine(_writer(py.world))
    run_claimed(engine, py.claim_python(engine))
    assert py.runner.tasks[0].connections == {}


def test_a_declared_connection_that_is_gone_fails_naming_it(py: PyWorld) -> None:
    py.deploy(connections=["crm-db"])
    task, message = _fails(py, py.engine(_writer(py.world)))
    assert (task.status, task.attempt, task.failure_code) == (TaskStatus.FAILED, 1, "not_found")
    assert message == (
        "gold.scores declares the connection `crm-db`, which environment dev does not have; "
        "create it (lakana connection create) or remove it from the asset's connections, then "
        "deploy again"
    )
    assert py.runner.tasks == []


_CONNECTION_PASSWORD = "crm-db-pa55word"


def _connection_with_secret(
    py: PyWorld, name: str = "crm-db", password: str = _CONNECTION_PASSWORD
) -> None:
    """Add a `tcp` connection to `dev` with its own secret, under the engine's test key.

    Parameters
    ----------
    py
        The world.
    name
        Its name.
    password
        Its secret's `password`.
    """
    with py.world.session() as session:
        owner = session.get(Principal, py.world.admin.id)
        assert owner is not None
        ConnectionService(session, AllowAllAuthorization(), _INDEX_KEY).create(
            PrincipalRef(id=owner.id, name=owner.name, type=owner.type),
            "dev",
            name=name,
            kind=ConnectionKind.TCP,
            address={"host": "db.example.com", "port": 5432},
            settings={},
            secret_values={"user": "abc", "password": password},
        )


def test_the_declared_connections_secrets_are_hidden_from_the_tasks_output(py: PyWorld) -> None:
    """Decrypted with `require_use` and given to the runner only; values under 4 are not."""
    _connection_with_secret(py)
    py.deploy(connections=["crm-db"])
    engine = _with_key(py.engine(_writer(py.world)))
    run_claimed(engine, py.claim_python(engine))
    (task,) = py.runner.tasks
    assert task.hidden == (_CONNECTION_PASSWORD,)
    assert _CONNECTION_PASSWORD not in repr(task)
    assert _task(py.world, "gold.scores").status is TaskStatus.SUCCEEDED


def test_a_secret_bound_elsewhere_is_not_decrypted_and_the_attempt_still_runs(
    py: PyWorld, caplog: pytest.LogCaptureFixture
) -> None:
    _connection_with_secret(py)
    with py.world.session() as session:
        session.execute(
            update(Secret)
            .where(Secret.name == "crm-db-connection")
            .values(use_target_id=uuid.uuid4())
        )
        session.commit()
    py.deploy(connections=["crm-db"])
    engine = _with_key(py.engine(_writer(py.world)))
    with caplog.at_level(logging.WARNING):
        run_claimed(engine, py.claim_python(engine))
    assert py.runner.tasks[0].hidden == ()
    assert _task(py.world, "gold.scores").status is TaskStatus.SUCCEEDED
    assert "crm-db" in caplog.text
    assert _CONNECTION_PASSWORD not in caplog.text


def test_every_form_of_a_tricky_secret_is_hidden(py: PyWorld) -> None:
    password = "Tr0ub\\4dor\"&'x\x01é"
    _connection_with_secret(py, password=password)
    py.deploy(connections=["crm-db"])
    engine = _with_key(py.engine(_writer(py.world)))
    run_claimed(engine, py.claim_python(engine))
    assert set(py.runner.tasks[0].hidden) == secret_forms(password)


def test_without_a_master_key_a_task_whose_connection_has_a_secret_never_starts(
    py: PyWorld,
) -> None:
    """The server would hand the task a value this worker could not hide in its log."""
    _connection_with_secret(py)
    py.deploy(connections=["crm-db"])
    engine = py.engine(_writer(py.world))
    engine.key_provider = None
    task, message = _fails(py, engine)
    assert (task.status, task.attempt, task.failure_code) == (TaskStatus.FAILED, 1, "no_runtime")
    assert message == _CANNOT_HIDE.format(name="crm-db")
    assert py.runner.tasks == []
    assert _tokens_minted(py) == 0


def test_a_worker_whose_key_cannot_read_the_secret_never_starts_the_task(py: PyWorld) -> None:
    _connection_with_secret(py)
    py.deploy(connections=["crm-db"])
    engine = py.engine(_writer(py.world))
    engine.key_provider = EnvKeyProvider(base64.b64encode(b"9" * 32).decode())
    task, message = _fails(py, engine)
    assert (task.status, task.failure_code) == (TaskStatus.FAILED, "no_runtime")
    assert message == _CANNOT_HIDE.format(name="crm-db")
    assert py.runner.tasks == []


def test_without_a_master_key_a_connection_without_a_secret_still_runs(py: PyWorld) -> None:
    _connection(py)
    py.deploy(connections=["crm-db"])
    engine = py.engine(_writer(py.world))
    engine.key_provider = None
    run_claimed(engine, py.claim_python(engine))
    assert py.runner.tasks[0].hidden == ()
    assert _task(py.world, "gold.scores").status is TaskStatus.SUCCEEDED


_CANNOT_HIDE = (
    "this worker cannot decrypt the secret of connection `{name}`, so it could not hide the "
    "value in the task's log, which the server would hand the task; set "
    "LAKANA_SECRETS__MASTER_KEY on the worker to the server's value and restart it"
)


def _tokens_minted(py: PyWorld) -> int:
    """Count the task tokens minted so far.

    Parameters
    ----------
    py
        The world.

    Returns
    -------
    int
        How many there are.
    """
    with py.world.session() as session:
        return len(session.execute(select(TaskToken.id)).all())


def test_a_stored_value_that_is_not_text_fails_the_task_for_good_naming_the_cause(
    py: PyWorld,
) -> None:
    """An older row can hold one; the worker could not hide it, so the task never starts."""
    _connection_with_secret(py)
    with py.world.session() as session:
        secret = session.execute(
            select(Secret).where(Secret.name == "crm-db-connection")
        ).scalar_one()
        sealed = encrypt_secret_values(
            {"password": "x\ud800yz"},
            _INDEX_KEY,
            SecretBinding(secret.id, secret.environment_id, secret.name),
        )
        secret.ciphertext, secret.nonce = sealed.ciphertext, sealed.nonce
        secret.wrapped_data_key, secret.key_id = sealed.wrapped_data_key, sealed.key_id
        session.commit()
    py.deploy(connections=["crm-db"])
    task, message = _fails(py, _with_key(py.engine(_writer(py.world))))
    assert (task.status, task.attempt, task.failure_code) == (TaskStatus.FAILED, 1, "no_runtime")
    assert message == (
        "the secret of connection `crm-db` holds a value that is not valid text, so it could not "
        "be hidden in the task's log; set it again: lakana connection update crm-db --env dev "
        "--password-stdin (or --values-stdin)"
    )
    assert py.runner.tasks == []


@pytest.mark.parametrize(
    ("kind", "address", "settings"),
    [
        (ConnectionKind.POSTGRESQL, {"host": "db.example.com", "port": 5432, "database": "c"},
         {"user": "reader"}),
        (ConnectionKind.HTTP, {"base_url": "https://api.example.com"},
         {"auth": "oauth2", "client_id": "l", "token_url": "https://login.example.com/token"}),
        (ConnectionKind.TCP, {"host": "erp.example.com", "port": 1433}, {}),
        (ConnectionKind.DIRECTORY, {"root": "shared", "path": "in"}, {}),
    ],
    ids=["postgresql", "http", "tcp", "directory"],
)  # fmt: skip
def test_a_task_and_a_connection_test_get_the_same_routes_for_one_connection(
    py: PyWorld,
    tmp_path: Path,
    kind: ConnectionKind,
    address: dict[str, object],
    settings: dict[str, object],
) -> None:
    """The Docker runner's check reaches exactly what a task declaring the connection reaches.

    Parameters
    ----------
    py
        The world.
    tmp_path
        Pytest's directory, for the check's attempts directory.
    kind
        The connection's kind.
    address
        Its address.
    settings
        Its settings.
    """
    _connection(py, "src", kind, address, settings)
    py.deploy(connections=["src"])
    engine = py.engine(_writer(py.world), environments=FakeEnvironments())
    run_claimed(engine, py.claim_python(engine))
    (task,) = py.runner.tasks
    with py.world.session() as session:
        row = session.execute(select(Connection).where(Connection.name == "src")).scalar_one()
        session.add(
            ConnectionTest(
                id=uuid.uuid4(),
                connection_id=row.id,
                environment_id=row.environment_id,
                requested_by=py.world.admin.id,
                state=ConnectionTestState.QUEUED,
            )
        )
        session.commit()

    def answer(request: dict[str, object], on_log: Callable[[bytes], None]) -> dict[str, object]:
        on_log(b'{"outcome": "ok", "message": "reachable"}\n')
        return {"exit": 0}

    launcher = FakeLauncher(scripts={"check": answer})
    attempts = tmp_path / "check-attempts"
    attempts.mkdir(mode=0o700)
    check = ConnectionTestLoop(
        py.world.engine,
        owner="w1",
        key_provider=EnvKeyProvider(base64.b64encode(b"k" * 32).decode()),
        runner_kind="docker",
        ingestion_roots={},
        docker=DockerCheck(client=launcher, owner="box-1.abc", work_dir=attempts),
    )

    assert check.step()

    (request,) = launcher.requests
    assert request["egress"] == [[host, port] for host, port in task.egress]
    assert request["relay"] == [
        {"listen": listen, "host": host, "port": port} for listen, host, port in task.relay
    ]
    assert request["roots"] == list(task.roots)
    assert json.loads(request["stdin"])["routing"] == task.connections["src"]
