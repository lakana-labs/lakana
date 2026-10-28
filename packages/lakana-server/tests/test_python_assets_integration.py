"""Exit tests: Python assets end to end on Postgres and SeaweedFS.

A real HTTP server (uvicorn on a loopback port), one in-process worker with
the real `LocalProcessRunner`, the real venv cache and the real `uv`, and a
project whose packages come from a wheel directory committed into the test
repository: no test reaches a package index. One worker does everything,
which is the point: it serves the Python task's input queries itself.

The wheel directory is built by `scripts/build_wheelhouse.sh DIR`, the only
step that needs the network, and passed as `LAKANA_TEST_WHEELHOUSE=DIR`.
Without the variable this module is skipped (in CI it fails instead: there
the step is part of the job, so a missing variable is a broken job).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import importlib.util
import json
import os
import platform
import re
import shutil
import subprocess  # nosec B404 -- fixed argv in tests
import sys
import threading
import time
import uuid
import zipfile
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from types import ModuleType

import pyarrow as pa
import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from typer.testing import CliRunner

import lakana
from lakana._models import Deploy as SdkDeploy
from lakana._models import Run as SdkRun
from lakana.cli.app import build_app
from lakana.errors import DeployFailed, NotFound, QueryFailed
from lakana.errors import PermissionDenied as SdkPermissionDenied
from lakana_core.catalog.service import Rollback
from lakana_core.db import create_engine, db_now
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.models import (
    ArtifactUpload,
    ArtifactVersion,
    AttemptOutcome,
    AuditEvent,
    AuditEventType,
    Environment,
    LineageEdge,
    Namespace,
    QueryRun,
    RollbackOutcome,
    Table,
    TaskAttempt,
    TaskRun,
    TaskToken,
)
from lakana_core.projects.lockfile import used_indexes
from lakana_core.projects.package_indexes import registered_indexes
from lakana_core.projects.package_sources import KnownSources
from lakana_core.runs.checks import ROLLBACK_SENTENCES, results_of_task, verdict
from lakana_core.runs.checks import failure_message as check_failure_message
from lakana_core.runs.store import scheduler_tick
from lakana_core.secrets.keys import EnvKeyProvider
from lakana_server.app import create_app
from lakana_worker.deploy import DeploySettings
from lakana_worker.index_credentials import resolve_index_access
from lakana_worker.poller import poll_once
from lakana_worker.python_support import PythonSupport
from lakana_worker.python_tasks import PythonRuntime
from lakana_worker.runners import LocalProcessRunner, PythonTask, Runner, RunOutcome, StopSignal
from lakana_worker.tasks import RunEngine, TaskSettings
from lakana_worker.venvs import VenvSettings, acquire
from tests.index_registration import registered_index
from tests.integration_helpers import commit_files, unique_name
from tests.python_asset_helpers import (
    MASTER_KEY,
    TOKEN,
    Services,
    bound_attempt,
    cut_idle_connections,
    running_services,
    serving,
)

pytestmark = pytest.mark.integration

_WHEELHOUSE = "LAKANA_TEST_WHEELHOUSE"
_NO_WHEELHOUSE = (
    f"{_WHEELHOUSE} is not set: the Python-asset integration tests install from an offline "
    "wheel index. Build it once with `bash scripts/build_wheelhouse.sh DIR` (the only step "
    f"that needs the network) and run `{_WHEELHOUSE}=DIR uv run pytest -m integration`."
)
_UNREACHABLE_PROXY = "http://127.0.0.1:9"
"""The discard port: a venv build that reached for a package index would fail."""

_RUN_SECONDS = 240
"""Longest a run may take in these tests; every asset's own timeout is far below it."""

_WORK_SECONDS = 240
"""Longest one drain of the worker's queues may take outside a run (a deploy, a query).

One claimed item is bounded by the product (a query and an asset by their
timeouts, 90 s here; a venv build by `build_timeout`, 180 s), so a drain ends
at most that long after its deadline.
"""

_SCORES = '''\
import polars as pl
from lakana import asset


@asset(inputs={"clean": "silver.clean"}, columns={"score": "Twice the id."})
def scores(clean: pl.DataFrame) -> pl.DataFrame:
    """Score every clean event."""
    print("scoring", clean.height, "rows")
    return clean.with_columns(score=pl.col("id") * 2)
'''

_BROKEN = '''\
from lakana import asset


@asset(retries=1)
def broken():
    """Always fails."""
    raise RuntimeError("boom from the asset")
'''

_SLOW = '''\
import subprocess
import sys
import time

from lakana import asset


@asset(timeout="3s", retries=0)
def slow():
    """Outlives its timeout, with a child process."""
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
    print("child pid:", child.pid, flush=True)
    time.sleep(300)
'''

_RACY = '''\
import time

import polars as pl
from lakana import asset


@asset(retries=0)
def racy() -> pl.DataFrame:
    """Slow enough for another writer to get in first."""
    time.sleep(6)
    return pl.DataFrame({"id": [42]})
'''

_NESTED = '''\
import polars as pl
from lakana import asset


@asset(inputs={"fresh": "bronze.staging.fresh"})
def nested(fresh: pl.DataFrame) -> pl.DataFrame:
    """Read an input from a nested namespace."""
    return fresh.select(total=pl.col("id").sum())
'''

_FROM_VIEW = '''\
import polars as pl
from lakana import asset


@asset(inputs={"recent": "silver.recent"})
def from_view(recent: pl.DataFrame) -> pl.DataFrame:
    """Read an input that is a view."""
    return recent.sort("id")
'''

_SHAPED = '''\
import polars as pl
from lakana import asset


@asset(inputs={"knobs": "bronze.knobs"}, retries=2)
def shaped(knobs: pl.DataFrame) -> pl.DataFrame:
    """No rows until a knob above 100 exists; then the column has another name."""
    wide = knobs.filter(pl.col("id") > 100)
    return wide.rename({"id": "ident"}) if wide.height else wide
'''

_EVOLVING = '''\
import polars as pl
from lakana import asset


@asset(inputs={"dials": "bronze.dials"}, evolve_schema=True, retries=0)
def evolving(dials: pl.DataFrame) -> pl.DataFrame:
    """Each new dial changes the shape of the result."""
    step = dials["id"].max()
    if step == 1:
        return pl.DataFrame(
            {
                "id": pl.Series([1, 2], dtype=pl.Int32),
                "ratio": pl.Series([0.5, 1.5], dtype=pl.Float32),
                "code": [10, 20],
                "gone": ["a", "b"],
            }
        )
    if step == 2:
        # Polars' default Int64 and Float64 into the Int32 and Float32 columns: both widen.
        return pl.DataFrame(
            {"id": [3], "ratio": [2.5], "code": ["x"], "label": ["new"]}
        )
    # No rows, a Float32 into the Float64 column, `code` and `label` gone, `note` added.
    return pl.DataFrame(schema={"id": pl.Int64, "ratio": pl.Float32, "note": pl.String})
'''

_FILES = {
    "assets/silver/clean.sql": "SELECT id FROM bronze.events\n",
    "assets/silver/recent.sql": (
        "/* lakana\nmaterialize: view\n*/\nSELECT id FROM bronze.events WHERE id > 1\n"
    ),
    "assets/gold/scores.py": _SCORES,
    "assets/gold/top.sql": "SELECT max(score) AS top FROM gold.scores\n",
    "assets/gold/broken.py": _BROKEN,
    "assets/gold/slow.py": _SLOW,
    "assets/gold/racy.py": _RACY,
    "assets/gold/nested.py": _NESTED,
    "assets/gold/from_view.py": _FROM_VIEW,
    "assets/gold/shaped.py": _SHAPED,
    "assets/gold/evolving.py": _EVOLVING,
}


@pytest.fixture(scope="module")
def wheelhouse() -> Path:
    """Return the offline wheel directory, or skip the module without one.

    Returns
    -------
    Path
        A directory holding the `lakana` wheel, polars and their dependencies.
    """
    prepared = os.environ.get(_WHEELHOUSE)
    if not prepared:
        if os.environ.get("GITHUB_ACTIONS") == "true":
            pytest.fail(_NO_WHEELHOUSE)
        pytest.skip(_NO_WHEELHOUSE)
    directory = Path(prepared)
    wheels = sorted(directory.glob("lakana-*.whl"))
    if len(wheels) != 1:
        pytest.fail(
            f"{_WHEELHOUSE}={prepared} holds {len(wheels)} lakana wheels, not exactly one; "
            "rebuild it with `bash scripts/build_wheelhouse.sh DIR`."
        )
    return directory


class _Hold:
    """Holds back the answer to one artifact commit or one table replace, after it is stored.

    A commit is a database transaction: when its response is being written the version (or
    the table's new snapshot) exists. Holding that response is how a test ends an attempt
    between the commit and the harness's record of it, which no code of the asset can do.

    Parameters
    ----------
    seconds
        The longest a held response waits, whatever else happens.
    """

    def __init__(self, seconds: float = 25.0) -> None:
        self.seconds = seconds
        self.wait = seconds
        self.armed: str | None = None
        self.status: int | None = None
        self.held = threading.Event()
        self.release = threading.Event()

    def arm(self, name: str, seconds: float | None = None) -> None:
        """Hold the next commit of an artifact, or the next replace of a table, of this name.

        Parameters
        ----------
        name
            The artifact's or the table's name, without its namespace.
        seconds
            The longest this one response waits; `None` keeps the default `seconds`. It must
            outlast the timeout that is to end the attempt while its reply is held.
        """
        self.held.clear()
        self.release.clear()
        self.status = None
        self.wait = self.seconds if seconds is None else seconds
        self.armed = name


_COMMIT = re.compile(
    r"^/api/v1/environments/[^/]+/namespaces/[^/]+/artifacts/(?P<name>[^/]+)/uploads/[^/]+/commit$"
)


_REPLACE = re.compile(r"^/api/v1/environments/[^/]+/namespaces/[^/]+/tables/(?P<name>[^/]+)/data$")
"""The route a task's harness replaces its table through (`PUT`)."""


class _HoldCommit:
    """ASGI middleware: delay the response of the commit or replace `_Hold` was armed for.

    Parameters
    ----------
    app
        The application.
    hold
        What to hold, shared with the test.
    """

    def __init__(self, app: ASGIApp, hold: _Hold) -> None:
        self.app = app
        self.hold = hold

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Pass a request on; delay the start of the response of an armed commit.

        Parameters
        ----------
        scope
            The ASGI scope.
        receive
            The ASGI receive channel.
        send
            The ASGI send channel.
        """
        found = None
        if scope["type"] == "http" and scope["method"] == "POST":
            found = _COMMIT.match(scope.get("path", ""))
        elif scope["type"] == "http" and scope["method"] == "PUT":
            found = _REPLACE.match(scope.get("path", ""))
        if found is None or found["name"] != self.hold.armed:
            await self.app(scope, receive, send)
            return
        self.hold.armed = None

        async def late(message: Message) -> None:
            """Wait, in steps and for at most `seconds`, before the response starts.

            Parameters
            ----------
            message
                The ASGI message about to be sent.
            """
            if message["type"] == "http.response.start":
                self.hold.status = message["status"]
                self.hold.held.set()
                for _ in range(int(self.hold.wait * 10)):
                    if self.hold.release.is_set():
                        break
                    await asyncio.sleep(0.1)
            await send(message)

        await self.app(scope, receive, late)


@dataclass(frozen=True)
class _Stack:
    """A running server, one worker, and an admin client.

    Parameters
    ----------
    services
        Postgres and SeaweedFS.
    db
        The test's own engine on the metadata database.
    worker_db
        The worker's engine.
    admin
        An SDK client with the bootstrap token.
    engine
        The single worker's run engine.
    runtime
        Its Python runtime.
    deploy
        Its deploy settings.
    url
        The server's base URL.
    hold
        Holds back the answer to an artifact commit or a table replace when armed.
    """

    services: Services

    db: Engine
    worker_db: Engine
    admin: lakana.Client
    engine: RunEngine
    runtime: PythonRuntime
    deploy: DeploySettings
    url: str
    hold: _Hold

    def work(self, deadline: float | None = None) -> None:
        """Tick the scheduler and drain every queue once, as the one worker.

        Parameters
        ----------
        deadline
            `time.monotonic()` value after which draining gives up;
            `_WORK_SECONDS` from now when `None`.

        Raises
        ------
        AssertionError
            If the queues are still not empty at the deadline: something
            keeps requeueing work (the projects retry without delay), which
            must fail the test instead of blocking it.
        """
        if deadline is None:
            deadline = time.monotonic() + _WORK_SECONDS
        with Session(self.worker_db) as session:
            scheduler_tick(session, db_now(session), lambda: 0.5)
        while True:
            with Session(self.worker_db) as session:
                if not poll_once(
                    session,
                    GrantAuthorization(session),
                    self.engine.key_provider,
                    deploy=self.deploy,
                    engine=self.engine,
                ):
                    return
            if time.monotonic() >= deadline:
                raise AssertionError("the worker's queues did not drain before the deadline")

    def finish(self, environment: str, run_id: str) -> SdkRun:
        """Work until a run finishes.

        Parameters
        ----------
        environment
            Environment name.
        run_id
            The run.

        Returns
        -------
        SdkRun
            The finished run.

        Raises
        ------
        AssertionError
            If the run is still active after `_RUN_SECONDS`.
        """
        deadline = time.monotonic() + _RUN_SECONDS
        while time.monotonic() < deadline:
            self.work(deadline)
            current = self.admin.runs.get(environment, run_id)
            if current.status in {"succeeded", "failed", "cancelled"}:
                return current
            time.sleep(0.1)
        raise AssertionError(f"run {run_id} did not finish: {current}")

    def sql(self, sql: str, environment: str) -> list[dict[str, object]]:
        """Run a query as the admin; the one worker serves it.

        Parameters
        ----------
        sql
            The query.
        environment
            Environment name.

        Returns
        -------
        list of dict
            The rows.
        """
        query = self.admin.queries.submit(sql, environment)
        self.work()
        query.wait(timeout=30)
        return query.result().to_pylist()

    def succeed(self, environment: str, run_id: str) -> SdkRun:
        """Work until a run finishes, and require that it succeeded.

        Parameters
        ----------
        environment
            Environment name.
        run_id
            The run.

        Returns
        -------
        SdkRun
            The finished run.
        """
        finished = self.finish(environment, run_id)
        logs = {
            task.asset: self.admin.runs.log(environment, run_id, task.asset).text
            for task in finished.tasks
            if task.status != "succeeded"
        }
        assert finished.status == "succeeded", (finished, logs)
        return finished


@pytest.fixture(scope="module")
def stack(tmp_path_factory: pytest.TempPathFactory, wheelhouse: Path) -> Iterator[_Stack]:
    """Start Postgres, SeaweedFS, a real HTTP server and one worker.

    Parameters
    ----------
    tmp_path_factory
        For the repo root and the worker's caches.
    wheelhouse
        Requested first, so that a run without a wheel index starts no container.

    Yields
    ------
    _Stack
        The stack.
    """
    uv = shutil.which("uv")
    assert uv is not None, "the Python-asset integration tests need uv on the PATH"
    cache = tmp_path_factory.mktemp("worker-cache")
    with running_services(tmp_path_factory) as services:
        db = create_engine(services.settings.database)
        worker_db = services.worker_engine()
        hold = _Hold()
        app = create_app(services.settings)
        app.add_middleware(_HoldCommit, hold=hold)
        try:
            with (
                serving(app) as url,
                lakana.Client(url, TOKEN, timeout=60.0) as admin,
            ):
                support = PythonSupport(server_url=url, uv=uv)
                runtime = PythonRuntime(
                    support=support,
                    work_dir=cache / "attempts",
                    venvs=VenvSettings(
                        uv=uv,
                        cache=cache / "venvs",
                        uv_cache=cache / "uv",
                        build_timeout=180.0,
                        https_proxy=_UNREACHABLE_PROXY,
                    ),
                    runner=LocalProcessRunner(kill_grace=2.0),
                    kill_grace=2.0,
                )
                engine = RunEngine(
                    session_factory=lambda: Session(worker_db),
                    authz_factory=GrantAuthorization,
                    settings=TaskSettings(worker_id="w1", renew_seconds=1, lease_seconds=5),
                    python=runtime,
                    key_provider=EnvKeyProvider(MASTER_KEY),
                )
                yield _Stack(
                    services=services,
                    db=db,
                    worker_db=worker_db,
                    admin=admin,
                    engine=engine,
                    runtime=runtime,
                    deploy=DeploySettings(repo_roots=(str(services.roots),), python=support),
                    url=url,
                    hold=hold,
                )
        finally:
            worker_db.dispose()
            db.dispose()


@dataclass(frozen=True)
class _Project:
    """A deployed project.

    Parameters
    ----------
    environment
        The environment it is deployed to.
    repository
        The repository name.
    deploy
        The finished deploy.
    gate
        The file that opens the gate of `gold.pruned_use` (only the `artifacts` project has it).
    """

    environment: str
    repository: str
    deploy: SdkDeploy | None = None
    gate: Path | None = None


_PRIVATE_VERSIONS = {"team-utils": "1.4.0", "sdonly": "1.0.0"}
"""The version of each package the index fixture serves."""

_GROUP_PACKAGE = "lakanagroupmarker"
"""A package only a dependency group names: no task of the project may have it installed."""


def _write_group_wheel(directory: Path) -> None:
    """Write a minimal pure-Python wheel of `_GROUP_PACKAGE` into a wheel directory.

    Parameters
    ----------
    directory
        The project's `wheelhouse/`.
    """
    dist = f"{_GROUP_PACKAGE}-1.0.dist-info"
    with zipfile.ZipFile(directory / f"{_GROUP_PACKAGE}-1.0-py3-none-any.whl", "w") as wheel:
        wheel.writestr(f"{_GROUP_PACKAGE}.py", "VALUE = 1\n")
        wheel.writestr(
            f"{dist}/METADATA", f"Metadata-Version: 2.1\nName: {_GROUP_PACKAGE}\nVersion: 1.0\n"
        )
        wheel.writestr(
            f"{dist}/WHEEL",
            "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        wheel.writestr(f"{dist}/RECORD", "")


def _deploy(
    stack: _Stack,
    wheelhouse: Path,
    files: dict[str, str],
    *,
    default_group: bool = False,
    environment: str | None = None,
    status: str = "succeeded",
    private_index: str | None = None,
    lock_credential: tuple[str, str] | None = None,
    private_package: str = "team-utils",
    index_url_after_lock: str | None = None,
) -> _Project:
    """Create an environment with its source tables; register, lock and deploy a project.

    The project's packages come from a copy of the wheel directory committed
    into the repository as ``wheelhouse/``: ``[tool.uv] no-index = true`` and
    ``find-links = ["wheelhouse"]``, locked with ``uv lock --offline``.

    Parameters
    ----------
    stack
        The stack.
    wheelhouse
        The offline wheel directory.
    files
        The project's assets.
    default_group
        Add a dependency group holding `_GROUP_PACKAGE` and make it a `default-groups` entry.
    environment
        Deploy another repository into this existing environment instead of creating one with
        its source tables.
    status
        The status the deploy must end in; ``failed`` for a project the deploy refuses.
    private_index
        The URL of a private index the project takes `private_package` from; the project then
        uses a flat default index instead of `no-index`.
    lock_credential
        The developer's user name and password for that index, used only to make the lock.
    private_package
        The package taken from `private_index`: `team-utils` (a wheel) or `sdonly` (a source
        distribution only).
    index_url_after_lock
        After the lock, rewrite the private index's URL in `pyproject.toml` and `uv.lock` to
        this one (another host), so the committed project names an index nobody registered.

    Returns
    -------
    _Project
        The environment and repository names and the finished deploy.
    """
    assert not (private_index and default_group), "default-groups would land in another table"
    repo = unique_name()
    env = environment or unique_name()
    admin = stack.admin
    if environment is None:
        stack.services.environment(admin, env)
        ids = pa.schema([pa.field("id", pa.int64())])
        admin.namespaces.create(env, "bronze.staging")
        for table, values in (
            ("bronze.events", [1, 2]),
            ("bronze.knobs", [1]),
            ("bronze.dials", [1]),
            ("bronze.staging.fresh", [3, 4]),
        ):
            admin.tables.create(env, table, schema=ids)
            admin.tables.append(env, table, pa.table({"id": pa.array(values, pa.int64())}))
    path = stack.services.roots / repo
    shutil.copytree(wheelhouse, path / "wheelhouse", ignore=shutil.ignore_patterns(".gitignore"))
    if default_group:
        _write_group_wheel(path / "wheelhouse")
    wheel = next((path / "wheelhouse").glob("lakana-*.whl"))
    version = wheel.name.split("-")[1]
    python = f"{sys.version_info.major}.{sys.version_info.minor}"
    marker = f"sys_platform == '{sys.platform}' and platform_machine == '{platform.machine()}'"
    (path / "pyproject.toml").write_text(
        "[project]\n"
        f'name = "{repo}"\n'
        'version = "0.1.0"\n'
        f'requires-python = "=={python}.*"\n'
        f'dependencies = ["lakana[assets]=={version}"'
        + (f', "{private_package}=={_PRIVATE_VERSIONS[private_package]}"' if private_index else "")
        + "]\n\n[tool.uv]\n"
        + (
            f'environments = ["{marker}"]\n\n'
            '[[tool.uv.index]]\nname = "wheels"\nurl = "wheelhouse"\nformat = "flat"\n'
            "default = true\n\n"
            f'[[tool.uv.index]]\nname = "team"\nurl = "{private_index}"\nexplicit = true\n\n'
            f'[tool.uv.sources]\n{private_package} = {{ index = "team" }}\n'
            if private_index
            else f'no-index = true\nfind-links = ["wheelhouse"]\nenvironments = ["{marker}"]\n'
        )
        + (
            f'default-groups = ["ml"]\n\n[dependency-groups]\nml = ["{_GROUP_PACKAGE}"]\n'
            if default_group
            else ""
        )
    )
    uv = shutil.which("uv")
    assert uv is not None
    lock_env = {**os.environ, "UV_NO_PROGRESS": "1", "https_proxy": _UNREACHABLE_PROXY}
    if private_index:
        assert lock_credential is not None
        lock_env["UV_INDEX_TEAM_USERNAME"], lock_env["UV_INDEX_TEAM_PASSWORD"] = lock_credential
    subprocess.run(  # nosec B603 -- fixed argv in tests
        [uv, "lock", *([] if private_index else ["--offline"]), "--python", sys.executable],
        cwd=path,
        check=True,
        capture_output=True,
        timeout=120,
        env=lock_env,
    )
    assert (path / "uv.lock").is_file()
    if private_index:
        assert (
            lock_credential is not None and lock_credential[1] not in (path / "uv.lock").read_text()
        )
        if index_url_after_lock is not None:
            old_base = private_index.removesuffix("/simple")
            new_base = index_url_after_lock.removesuffix("/simple")
            for name in ("pyproject.toml", "uv.lock"):
                text = (path / name).read_text()
                assert old_base in text
                (path / name).write_text(text.replace(old_base, new_base))
    commit_files(
        path,
        {"lakana.yaml": f"repo: {repo}\ndefaults:\n  retry_delay: 0\n  timeout: 90s\n", **files},
    )
    admin.repositories.create(repo, "local", str(path))
    deploy = admin.deploys.create(env, repo)
    stack.work()
    try:
        finished = admin.deploys.wait(env, deploy.id, timeout=120)
    except DeployFailed as failed:
        finished = failed.deploy
    assert finished.status == status, finished
    return _Project(env, repo, finished)


@pytest.fixture(scope="module")
def project(stack: _Stack, wheelhouse: Path) -> _Project:
    """Deploy the project most tests of this module run.

    Parameters
    ----------
    stack
        The stack.
    wheelhouse
        The offline wheel directory.

    Returns
    -------
    _Project
        The deployed project.
    """
    return _deploy(stack, wheelhouse, _FILES)


def _attempts(stack: _Stack, run_id: str, asset: str) -> list[TaskAttempt]:
    """Return a task's attempts, oldest first.

    Parameters
    ----------
    stack
        The stack.
    run_id
        The run.
    asset
        The task's asset.

    Returns
    -------
    list of TaskAttempt
        The attempts.
    """
    with Session(stack.db) as session:
        return list(
            session.scalars(
                select(TaskAttempt)
                .join(TaskRun, TaskRun.id == TaskAttempt.task_run_id)
                .where(TaskRun.run_id == run_id, TaskRun.asset == asset)
                .order_by(TaskAttempt.started_at)
            ).all()
        )


def _tokens(stack: _Stack, run_id: str) -> list[TaskToken]:
    """Return the bound tokens minted for a run's tasks.

    Parameters
    ----------
    stack
        The stack.
    run_id
        The run.

    Returns
    -------
    list of TaskToken
        One per Python attempt that got as far as its process.
    """
    with Session(stack.db) as session:
        return list(
            session.scalars(
                select(TaskToken)
                .join(TaskRun, TaskRun.id == TaskToken.task_run_id)
                .where(TaskRun.run_id == run_id)
            ).all()
        )


def test_sql_feeds_python_feeds_sql_on_a_single_worker(stack: _Stack, project: _Project) -> None:
    """Exit criterion: deploy, run, every table built, with dependency lineage."""
    admin, env = stack.admin, project.environment
    (deployment,) = [d for d in admin.deployments.list(env) if d.repository == project.repository]
    manifest = admin.project_versions.get(env, deployment.project_version_id).manifest
    kinds = {entry["table"]: entry["kind"] for entry in manifest["assets"]}
    assert kinds["gold.scores"] == "python" and kinds["silver.clean"] == "sql"
    assert len(manifest["lock_sha256"]) == 64
    run = admin.runs.start(env, project.repository, select=["gold.top"], upstream=True)
    stack.succeed(env, run.id)
    assert stack.sql("SELECT id, score FROM gold.scores ORDER BY id", env) == [
        {"id": 1, "score": 2},
        {"id": 2, "score": 4},
    ]
    assert stack.sql("SELECT top FROM gold.top", env) == [{"top": 4}]
    assert "silver.clean" in repr(admin.tables.lineage(env, "gold.scores"))
    assert "gold.scores" in repr(admin.tables.lineage(env, "gold.top"))
    log = admin.runs.log(env, run.id, "gold.scores").text
    assert "scoring 2 rows" in log
    assert "lakana_task_" not in log
    tokens = _tokens(stack, run.id)
    assert len(tokens) == 1
    assert all(token.revoked_at is not None for token in tokens)
    assert list((stack.runtime.work_dir).iterdir()) == []
    again = admin.runs.start(env, project.repository, select=["gold.scores"])
    stack.succeed(env, again.id)
    assert "Installed" not in admin.runs.log(env, again.id, "gold.scores").text


def test_a_failing_function_retries_then_fails(stack: _Stack, project: _Project) -> None:
    """The asset's own exception reaches the attempt and the log; `retries=1` is two attempts."""
    admin, env = stack.admin, project.environment
    run = admin.runs.start(env, project.repository, select=["gold.broken"])
    finished = stack.finish(env, run.id)
    assert finished.status == "failed"
    attempts = _attempts(stack, run.id, "gold.broken")
    assert len(attempts) == 2
    assert [attempt.error_message for attempt in attempts] == [
        "RuntimeError: boom from the asset"
    ] * 2
    log = admin.runs.log(env, run.id, "gold.broken").text
    assert "Traceback (most recent call last)" in log
    assert "RuntimeError: boom from the asset" in log
    assert all(token.revoked_at is not None for token in _tokens(stack, run.id))


def test_a_timeout_kills_the_process_group(stack: _Stack, project: _Project) -> None:
    """The asset's timeout ends the harness and the child process it started."""
    admin, env = stack.admin, project.environment
    run = admin.runs.start(env, project.repository, select=["gold.slow"])
    finished = stack.finish(env, run.id)
    assert finished.status == "failed"
    (attempt,) = _attempts(stack, run.id, "gold.slow")
    assert attempt.error_message == "timed out after 3s"
    assert attempt.outcome is AttemptOutcome.TIMED_OUT
    log = admin.runs.log(env, run.id, "gold.slow").text
    pid = int(log.split("child pid:")[1].split()[0])
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    pytest.fail(f"the task's child process {pid} survived the timeout")


def test_a_concurrent_write_fails_the_attempt_with_a_conflict(
    stack: _Stack, project: _Project
) -> None:
    """Exit criterion: the 409. The one uncounted conflict retry then succeeds on fresh state."""
    admin, env = stack.admin, project.environment
    admin.tables.replace(
        env, "gold.racy", pa.table({"id": pa.array([0], pa.int64())}), expected_snapshot_id="none"
    )
    run = admin.runs.start(env, project.repository, select=["gold.racy"])
    wrote = threading.Event()
    errors: list[BaseException] = []

    def interfere() -> None:
        try:
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                if any(token.revoked_at is None for token in _tokens(stack, run.id)):
                    break
                time.sleep(0.1)
            with lakana.Client(stack.url, TOKEN, timeout=60.0) as other:
                other.tables.append(env, "gold.racy", pa.table({"id": pa.array([7], pa.int64())}))
            wrote.set()
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=interfere, daemon=True)
    thread.start()
    finished = stack.finish(env, run.id)
    thread.join(timeout=30)
    assert wrote.is_set(), errors
    attempts = _attempts(stack, run.id, "gold.racy")
    assert [attempt.outcome for attempt in attempts] == [
        AttemptOutcome.FAILED,
        AttemptOutcome.SUCCEEDED,
    ]
    assert "changed during the write" in (attempts[0].error_message or "")
    assert finished.status == "succeeded", finished
    assert stack.sql("SELECT id FROM gold.racy", env) == [{"id": 42}]


def test_an_input_in_a_nested_namespace(stack: _Stack, project: _Project) -> None:
    """A three-part input name is quoted part by part and resolved by the query path."""
    admin, env = stack.admin, project.environment
    run = admin.runs.start(env, project.repository, select=["gold.nested"])
    stack.succeed(env, run.id)
    assert stack.sql("SELECT total FROM gold.nested", env) == [{"total": 7}]
    with Session(stack.db) as session:
        queries = session.scalars(
            select(QueryRun.sql_text)
            .join(TaskRun, TaskRun.id == QueryRun.task_run_id)
            .where(TaskRun.run_id == run.id)
        ).all()
    assert queries == ['SELECT * FROM "bronze"."staging"."fresh"']
    assert "bronze.staging.fresh" in repr(admin.tables.lineage(env, "gold.nested"))


def test_a_view_as_input_is_read_under_the_bound_token(stack: _Stack, project: _Project) -> None:
    """The task reads a declared view input, which reads a table it did not declare."""
    admin, env = stack.admin, project.environment
    run = admin.runs.start(env, project.repository, select=["gold.from_view"], upstream=True)
    finished = stack.succeed(env, run.id)
    assert {task.asset for task in finished.tasks} == {"silver.recent", "gold.from_view"}
    assert stack.sql("SELECT id FROM gold.from_view", env) == [{"id": 2}]


def test_zero_rows_create_the_table_and_a_changed_schema_fails_for_good(
    stack: _Stack, project: _Project
) -> None:
    """A first run without rows creates the table; a renamed column is a permanent failure."""
    admin, env = stack.admin, project.environment
    for _ in range(2):  # the second run replaces a table that has no snapshot yet
        run = admin.runs.start(env, project.repository, select=["gold.shaped"])
        stack.succeed(env, run.id)
        assert stack.sql("SELECT count(*) AS n FROM gold.shaped", env) == [{"n": 0}]
    described = admin.tables.get(env, "gold.shaped")
    assert [column.name for column in described.columns] == ["id"]
    admin.tables.append(env, "bronze.knobs", pa.table({"id": pa.array([101], pa.int64())}))
    changed = admin.runs.start(env, project.repository, select=["gold.shaped"])
    assert stack.finish(env, changed.id).status == "failed"
    (attempt,) = _attempts(stack, changed.id, "gold.shaped")  # retries=2, and yet one attempt
    assert attempt.outcome is AttemptOutcome.FAILED
    message = attempt.error_message or ""
    assert "no longer matches the schema of gold.shaped" in message
    assert "renamed-looking id -> ident" in message
    assert "add evolve_schema=True to the asset's @asset(...)" in message
    assert "Pass evolve_schema" not in message
    assert stack.sql("SELECT count(*) AS n FROM gold.shaped", env) == [{"n": 0}]


def _columns(stack: _Stack, environment: str, table: str) -> list[tuple[str, str]]:
    """Return a table's columns as the catalog describes them.

    Parameters
    ----------
    stack
        The stack.
    environment
        Environment name.
    table
        The dotted table name.

    Returns
    -------
    list of tuple
        ``(name, type)`` per column, in order.
    """
    return [(c.name, c.type) for c in stack.admin.tables.get(environment, table).columns]


def test_an_evolving_replace_changes_the_schema_and_an_empty_one_still_proves_its_write(
    stack: _Stack, project: _Project
) -> None:
    """`evolve_schema=True` widens, retypes, drops and adds; every run proves its own write.

    The worker counts a Python task's success only when it finds the attempt's stamp on a
    snapshot the attempt committed, else it fails the task with `conflict`; a run that
    succeeds here therefore committed its own stamped snapshot, also with no rows.
    """
    admin, env = stack.admin, project.environment
    run = admin.runs.start(env, project.repository, select=["gold.evolving"])
    stack.succeed(env, run.id)
    assert _columns(stack, env, "gold.evolving") == [
        ("id", "int"),
        ("ratio", "float"),
        ("code", "long"),
        ("gone", "string"),
    ]
    before = admin.tables.get(env, "gold.evolving").snapshot_id

    admin.tables.append(env, "bronze.dials", pa.table({"id": pa.array([2], pa.int64())}))
    changed = admin.runs.start(env, project.repository, select=["gold.evolving"])
    stack.succeed(env, changed.id)
    assert _columns(stack, env, "gold.evolving") == [
        ("id", "long"),
        ("ratio", "double"),
        ("code", "string"),
        ("label", "string"),
    ]
    assert stack.sql("SELECT id, ratio, code, label FROM gold.evolving", env) == [
        {"id": 3, "ratio": 2.5, "code": "x", "label": "new"}
    ]
    with pytest.raises(QueryFailed) as dropped:
        stack.sql("SELECT gone FROM gold.evolving", env)
    assert "gone" in str(dropped.value)
    after = admin.tables.get(env, "gold.evolving").snapshot_id
    assert after not in (None, before)

    admin.tables.append(env, "bronze.dials", pa.table({"id": pa.array([3], pa.int64())}))
    emptied = admin.runs.start(env, project.repository, select=["gold.evolving"])
    stack.succeed(env, emptied.id)
    (attempt,) = _attempts(stack, emptied.id, "gold.evolving")
    assert attempt.outcome is AttemptOutcome.SUCCEEDED
    assert _columns(stack, env, "gold.evolving") == [
        ("id", "long"),
        ("ratio", "double"),
        ("note", "string"),
    ]
    assert stack.sql("SELECT count(*) AS n FROM gold.evolving", env) == [{"n": 0}]
    assert admin.tables.get(env, "gold.evolving").snapshot_id not in (None, after)


class _CutConnectionsAfterTheProcess:
    """A runner that ends the worker's idle database connections once its process is done.

    This is what a long task leaves behind: by the time the process ends, an
    idle timeout between the worker and the database has closed the pooled
    connections the worker is about to use again.

    Parameters
    ----------
    inner
        The real runner.
    cut
        Ends the connections and returns how many.
    """

    def __init__(self, inner: Runner, cut: Callable[[], int]) -> None:
        self._inner = inner
        self._cut = cut
        self.cut: list[int] = []

    @property
    def isolated(self) -> bool:
        """Whether the wrapped runner isolates its tasks.

        Returns
        -------
        bool
            The inner runner's answer.
        """
        return self._inner.isolated

    def run(
        self, task: PythonTask, on_log: Callable[[bytes], None], stop: StopSignal
    ) -> RunOutcome:
        """Run the task, then cut the connections.

        Parameters
        ----------
        task
            What to run.
        on_log
            Receives the process's output.
        stop
            The supervisor's stop signal.

        Returns
        -------
        RunOutcome
            The real runner's outcome.
        """
        outcome = self._inner.run(task, on_log, stop)
        self.cut.append(self._cut())
        return outcome


def test_an_attempt_whose_database_connections_went_stale_still_records_its_outcome(
    stack: _Stack, project: _Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After a long process the pooled connections are dead; nothing may depend on them."""
    admin, env = stack.admin, project.environment
    runner = _CutConnectionsAfterTheProcess(
        stack.runtime.runner, lambda: cut_idle_connections(stack.db)
    )
    monkeypatch.setattr(stack.runtime, "runner", runner)
    run = admin.runs.start(env, project.repository, select=["gold.scores"], upstream=True)
    stack.succeed(env, run.id)
    assert runner.cut and runner.cut[0] >= 1
    (attempt,) = _attempts(stack, run.id, "gold.scores")
    assert attempt.outcome is AttemptOutcome.SUCCEEDED
    (token,) = _tokens(stack, run.id)
    assert token.revoked_at is not None
    assert "snapshot:" in admin.runs.log(env, run.id, "gold.scores").text


def test_the_protected_environment(stack: _Stack, wheelhouse: Path) -> None:
    """Protected: the run-as account writes, a user is refused, local Python is refused first.

    The SQL asset proves the deployed run writes. On this stack's local runner the Python
    asset ends `no_runtime` before any of its code runs: no token, no table.
    """
    project = _deploy(
        stack,
        wheelhouse,
        {
            "assets/silver/clean.sql": _FILES["assets/silver/clean.sql"],
            "assets/gold/scores.py": _SCORES,
        },
    )
    admin, env = stack.admin, project.environment
    account = "etl-" + env
    admin.principals.create(account, "SERVICE_ACCOUNT")
    admin.grants.grant(principal=account, role="DATA_ENGINEER", kind="ENVIRONMENT", environment=env)
    admin.deployments.set_run_as(env, project.repository, account)
    admin.environments.set_protection(env, True)
    run = admin.runs.start(env, project.repository)
    assert run.run_as == account
    finished = stack.finish(env, run.id)
    assert finished.status == "failed"
    tasks = {task.asset: task for task in finished.tasks}
    assert tasks["silver.clean"].status == "succeeded"
    assert stack.sql("SELECT count(*) AS n FROM silver.clean", env) == [{"n": 2}]
    scores = tasks["gold.scores"]
    assert scores.status == "failed"
    assert scores.failure is not None and scores.failure.code == "no_runtime"
    assert scores.error_message == (
        f"environment '{env}' is protected, and this worker runs Python tasks without "
        "isolation (LAKANA_RUN__RUNNER=local). Run the task on a worker with "
        'LAKANA_RUN__RUNNER=docker; the how-to "Run Python tasks in containers" says how.'
    )
    assert _tokens(stack, run.id) == []
    with pytest.raises(NotFound):
        admin.tables.get(env, "gold.scores")
    with pytest.raises(SdkPermissionDenied, match="is protected"):
        admin.tables.replace(env, "silver.clean", pa.table({"id": pa.array([9], pa.int64())}))


def test_a_default_dependency_group_is_not_installed_for_a_task(
    stack: _Stack, wheelhouse: Path
) -> None:
    """Real uv: `[tool.uv] default-groups` changes neither the lock nor what a task gets."""
    project = _deploy(
        stack,
        wheelhouse,
        {
            "assets/silver/clean.sql": _FILES["assets/silver/clean.sql"],
            "assets/gold/scores.py": _SCORES,
        },
        default_group=True,
    )
    admin, env = stack.admin, project.environment
    stack.succeed(env, admin.runs.start(env, project.repository).id)
    lock = (stack.services.roots / project.repository / "uv.lock").read_text()
    assert _GROUP_PACKAGE in lock
    cache = stack.runtime.venvs.cache
    venvs = [path for path in cache.iterdir() if (path / "bin" / "python").exists()]
    assert venvs
    assert not [
        found
        for path in venvs
        for found in path.glob(f"lib*/python*/site-packages/{_GROUP_PACKAGE}*")
    ]


@pytest.fixture
def register_index(stack: _Stack) -> Iterator[Callable[..., None]]:
    """Register package indexes for one test and remove them when it ends, also on a failure.

    The server outlives the test, and an index left behind keeps its URL.

    Parameters
    ----------
    stack
        The stack.

    Yields
    ------
    Callable
        ``register_index(name, url, credential_secret=None)``.
    """
    with contextlib.ExitStack() as cleanup:

        def register(name: str, url: str, *, credential_secret: str | None = None) -> None:
            """Register one index.

            Parameters
            ----------
            name
                The index's name.
            url
                The index's URL.
            credential_secret
                The secret that holds the index's credential, if it needs one.
            """
            cleanup.enter_context(registered_index(stack.admin, name, url, credential_secret))

        yield register


def _index_fixture() -> ModuleType:
    """Load the worker tests' package index fixture by path.

    A server test cannot import a worker test module by name. The module is registered in
    `sys.modules` before it runs, because its dataclasses read it while they are defined.

    Returns
    -------
    ModuleType
        The module `packages/lakana-worker/tests/index_fixture.py`.
    """
    path = Path(__file__).resolve().parents[2] / "lakana-worker" / "tests" / "index_fixture.py"
    spec = importlib.util.spec_from_file_location("index_fixture", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_PRIVATE = '''
import os

import polars as pl
import team_utils
from lakana import asset


@asset(inputs={"events": "bronze.events"})
def answers(events: pl.DataFrame) -> pl.DataFrame:
    """Use the private package, and show the task's whole environment to the test."""
    print("the private package says", team_utils.VALUE)
    seen = [f"{name}={value}" for name, value in os.environ.items() if "TOKEN" not in name]
    # HOME, TMPDIR, the code directory and the result file all live in the attempt directory.
    for folder in (os.path.dirname(os.environ["HOME"]),):
        for root, _, names in os.walk(folder):
            for name in names:
                path = os.path.join(root, name)
                if os.path.islink(path) or os.path.getsize(path) > 1_000_000:
                    continue
                with open(path, "rb") as handle:
                    seen.append(f"{path}:{handle.read().hex()}")
    return pl.DataFrame({"value": [team_utils.VALUE], "seen": ["\\n".join(seen)]})
'''
_PRIVATE_FILES = {"assets/gold/answers.py": _PRIVATE}


def _environment_with_events(stack: _Stack) -> str:
    """Create an environment that holds `bronze.events` with two rows.

    Parameters
    ----------
    stack
        The stack.

    Returns
    -------
    str
        The environment's name.
    """
    env = unique_name()
    stack.services.environment(stack.admin, env)
    stack.admin.tables.create(env, "bronze.events", schema=pa.schema([pa.field("id", pa.int64())]))
    stack.admin.tables.append(env, "bronze.events", pa.table({"id": pa.array([1, 2], pa.int64())}))
    return env


def _files_holding(root: Path, secret: str) -> list[str]:
    """List the files under a directory that contain a secret.

    Parameters
    ----------
    root
        The directory to search.
    secret
        The text to look for.

    Returns
    -------
    list of str
        The paths of the files that hold it.
    """
    needle = secret.encode()
    return [
        str(path)
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink() and needle in path.read_bytes()
    ]


def test_a_private_package_deploys_builds_and_runs(
    stack: _Stack, wheelhouse: Path, register_index: Callable[..., None]
) -> None:
    """The milestone's test: neither the project nor the task ever holds the password."""
    fixture = _index_fixture()
    admin, name, env = stack.admin, unique_name(), _environment_with_events(stack)
    secret = f"idx-{name}"
    with fixture.serving() as indexes:
        register_index(name, indexes.registered.url(), credential_secret=secret)
        admin.secrets.create(
            env,
            secret,
            {"username": fixture.USERNAME, "password": fixture.PASSWORD},
            kind="package_index",
            target=name,
        )
        project = _deploy(
            stack,
            wheelhouse,
            _PRIVATE_FILES,
            environment=env,
            private_index=indexes.registered.url(),
            lock_credential=(fixture.USERNAME, fixture.PASSWORD),
        )
        assert project.deploy is not None and project.deploy.project_version_id is not None
        manifest = admin.project_versions.get(env, project.deploy.project_version_id).manifest
        assert manifest["package_indexes"] == [name]
        asked_by_the_lock = len(indexes.registered.server.seen)
        run = admin.runs.start(env, project.repository, select=["gold.answers"])
        stack.succeed(env, run.id)
        assert len(indexes.registered.server.seen) > asked_by_the_lock
        assert set(indexes.registered.credentials_received) == {
            f"{fixture.USERNAME}:{fixture.PASSWORD}"
        }
    (row,) = stack.sql("SELECT value, seen FROM gold.answers", env)
    assert row["value"] == 42
    seen = str(row["seen"])
    assert "HOME=" in seen
    assert "UV_INDEX" not in seen and ".netrc" not in seen
    assert fixture.PASSWORD not in seen
    assert fixture.PASSWORD.encode().hex() not in seen
    # The pin that matters is `attempt_dir.parent == work_dir`: the directory the task walked
    # is an attempt directory of this worker. The last assertion only shows the walk read at
    # least one file there (the path of each is printed before its bytes).
    home = next(line for line in seen.splitlines() if line.startswith("HOME=")).removeprefix(
        "HOME="
    )
    attempt_dir = Path(home).parent
    assert attempt_dir.parent == stack.runtime.work_dir, (attempt_dir, stack.runtime.work_dir)
    walked = [line.split(":", 1)[0] for line in seen.splitlines() if ":" in line]
    assert any(Path(path).is_relative_to(attempt_dir) for path in walked), seen[:500]
    log = admin.runs.log(env, run.id, "gold.answers").text
    assert "the private package says 42" in log
    assert "+ team-utils==1.4.0" in log
    assert fixture.PASSWORD not in log
    for directory in (
        stack.services.roots / project.repository,
        stack.runtime.venvs.cache,
        stack.runtime.venvs.uv_cache,
    ):
        assert _files_holding(directory, fixture.PASSWORD) == []
    again = admin.runs.start(env, project.repository, select=["gold.answers"])
    stack.succeed(env, again.id)
    assert "Installed" not in admin.runs.log(env, again.id, "gold.answers").text


def test_a_deploy_without_the_secret_stops_and_names_the_command(
    stack: _Stack, wheelhouse: Path, register_index: Callable[..., None]
) -> None:
    fixture = _index_fixture()
    name, env = unique_name(), _environment_with_events(stack)
    secret = f"idx-{name}"
    with fixture.serving() as indexes:
        register_index(name, indexes.registered.url(), credential_secret=secret)
        project = _deploy(
            stack,
            wheelhouse,
            _PRIVATE_FILES,
            environment=env,
            private_index=indexes.registered.url(),
            lock_credential=(fixture.USERNAME, fixture.PASSWORD),
            status="failed",
        )
    assert project.deploy is not None
    assert project.deploy.error_message == (
        f"uv.lock: the package index '{name}' needs the secret '{secret}' in environment "
        f"'{env}', which does not exist. Create it with `lakana secret create {secret} --env "
        f'{env} --index {name} --values-stdin` (a JSON object with "username" and "password")'
    )


def test_a_wrong_password_fails_for_good_and_a_rotated_secret_recovers(
    stack: _Stack, wheelhouse: Path, register_index: Callable[..., None]
) -> None:
    """One attempt, the message with the command, and no new deploy afterwards."""
    fixture = _index_fixture()
    admin, name, env = stack.admin, unique_name(), _environment_with_events(stack)
    secret = f"idx-{name}"
    with fixture.serving() as indexes:
        register_index(name, indexes.registered.url(), credential_secret=secret)
        admin.secrets.create(
            env,
            secret,
            {"username": fixture.USERNAME, "password": "wrong-pw"},
            kind="package_index",
            target=name,
        )
        project = _deploy(
            stack,
            wheelhouse,
            _PRIVATE_FILES,
            environment=env,
            private_index=indexes.registered.url(),
            lock_credential=(fixture.USERNAME, fixture.PASSWORD),
        )
        run = admin.runs.start(env, project.repository, select=["gold.answers"])
        assert stack.finish(env, run.id).status == "failed"
        (attempt,) = _attempts(stack, run.id, "gold.answers")
        assert attempt.error_message == (
            f"environment build failed: The package index '{name}' refused the credential in "
            f"secret '{secret}' of environment '{env}'. Set it again with `lakana secret "
            f"rotate {secret} --env {env} --values-stdin`. If the password is right, check "
            "that its account may read the package: the index also answers 403 when the account "
            "may not read a package or file."
        )
        assert "wrong-pw" not in admin.runs.log(env, run.id, "gold.answers").text
        admin.secrets.rotate(
            env, secret, {"username": fixture.USERNAME, "password": fixture.PASSWORD}
        )
        again = admin.runs.start(env, project.repository, select=["gold.answers"])
        stack.succeed(env, again.id)


def test_a_removed_index_stops_the_next_run_and_says_so(
    stack: _Stack, wheelhouse: Path, register_index: Callable[..., None]
) -> None:
    fixture = _index_fixture()
    admin, name, env = stack.admin, unique_name(), _environment_with_events(stack)
    with fixture.serving() as indexes:
        url = indexes.registered.url()
        register_index(name, url)
        project = _deploy(
            stack,
            wheelhouse,
            _PRIVATE_FILES,
            environment=env,
            private_index=url,
            lock_credential=(fixture.USERNAME, fixture.PASSWORD),
        )
        admin.package_indexes.delete(name)
        run = admin.runs.start(env, project.repository, select=["gold.answers"])
        assert stack.finish(env, run.id).status == "failed"
    (attempt,) = _attempts(stack, run.id, "gold.answers")
    assert attempt.error_message is not None
    assert attempt.error_message.startswith(
        f"uv.lock: takes team-utils from the package index {url}, which is not registered"
    )
    assert attempt.error_message.endswith(
        "and start the run again, or change the project as the message says and deploy again."
    )


def test_a_package_without_a_wheel_fails_the_build_for_good(
    stack: _Stack, wheelhouse: Path, register_index: Callable[..., None]
) -> None:
    """R6.2: a project with a credential installs wheels only; the message is the how-to's."""
    fixture = _index_fixture()
    admin, name, env = stack.admin, unique_name(), _environment_with_events(stack)
    secret = f"idx-{name}"
    with fixture.serving() as indexes:
        register_index(name, indexes.registered.url(), credential_secret=secret)
        admin.secrets.create(
            env,
            secret,
            {"username": fixture.USERNAME, "password": fixture.PASSWORD},
            kind="package_index",
            target=name,
        )
        project = _deploy(
            stack,
            wheelhouse,
            _PRIVATE_FILES,
            environment=env,
            private_index=indexes.registered.url(),
            lock_credential=(fixture.USERNAME, fixture.PASSWORD),
            private_package="sdonly",
        )
        run = admin.runs.start(env, project.repository, select=["gold.answers"])
        assert stack.finish(env, run.id).status == "failed"
    (attempt,) = _attempts(stack, run.id, "gold.answers")
    assert attempt.error_message == (
        "environment build failed: sdonly has no wheel for this platform and Python; publish a "
        "wheel to your index, or remove the dependency. A project that uses a package index "
        "with a credential installs wheels only."
    )
    assert fixture.PASSWORD not in admin.runs.log(env, run.id, "gold.answers").text


def test_a_project_that_names_another_host_gives_that_host_no_credential(
    stack: _Stack, wheelhouse: Path, register_index: Callable[..., None]
) -> None:
    """The invariant end to end: the lock rule refuses, and a bypassed rule still leaks nothing.

    The index is registered as `team`, and the project's own index is also called `team`: a build
    that handed the credential over by the project's index name would send it to the other host.
    """
    fixture = _index_fixture()
    admin, env = stack.admin, _environment_with_events(stack)
    secret = f"idx-{unique_name()}"
    with fixture.serving() as indexes:
        registered, other = indexes.registered.url(), indexes.other.url()
        register_index("team", registered, credential_secret=secret)
        try:
            admin.secrets.create(
                env,
                secret,
                {"username": fixture.USERNAME, "password": fixture.PASSWORD},
                kind="package_index",
                target="team",
            )
            project = _deploy(
                stack,
                wheelhouse,
                _PRIVATE_FILES,
                environment=env,
                private_index=registered,
                lock_credential=(fixture.USERNAME, fixture.PASSWORD),
                index_url_after_lock=other,
                status="failed",
            )
            assert project.deploy is not None
            refusal = project.deploy.error_message or ""
            assert (
                f"uv.lock: takes team-utils from the package index {other}, which is not "
                in refusal
            )
            assert f"pyproject.toml: the index named team points at {other}, but" in refusal
            # Bypass the lock rule: build the refused project, with the access the worker
            # resolves from the registry (not one made by hand, so a change in how an access
            # is resolved shows here too).
            code_dir = stack.services.roots / project.repository
            lock = hashlib.sha256((code_dir / "uv.lock").read_bytes()).hexdigest()
            asked_registered = len(indexes.registered.server.seen)
            output: list[bytes] = []
            with Session(stack.worker_db) as session:
                sources = KnownSources(registered_indexes(session))
                # The lock names the other host, which nobody registered: the worker's own
                # step from lock to indexes finds no credential to hand over.
                assert used_indexes((code_dir / "uv.lock").read_bytes(), sources) == ()
                row = session.execute(
                    select(Environment).where(Environment.name == env)
                ).scalar_one()
                # Each test removes its index when it ends, but keep the filter: only this
                # test's index has a secret in this environment.
                ours = [found for found in sources.registered if found.name == "team"]
                (access,) = resolve_index_access(
                    session, stack.engine.key_provider, row.id, row.name, ours
                )
            assert (access.name, access.url) == ("team", registered)
            assert access.password == fixture.PASSWORD
            acquire(stack.runtime.venvs, lock, code_dir, output.append, lambda: [access]).release()
        finally:
            with contextlib.suppress(NotFound):
                admin.package_indexes.delete("team")
        assert indexes.other.server.seen, "the build never asked the other host"
        assert [header for _, header in indexes.other.server.seen if header is not None] == []
        assert indexes.other.credentials_received == []
        assert len(indexes.registered.server.seen) == asked_registered
        assert fixture.PASSWORD.encode() not in b"".join(output)


_TRAIN = '''\
import json
from pathlib import Path

import polars as pl
import lakana
from lakana import asset


@asset(inputs={"events": "bronze.events"}, output="artifact", keep_versions=2)
def slope(events: pl.DataFrame) -> Path:
    """A one-number model: the largest id seen."""
    out = lakana.output_dir() / "model.json"
    out.write_text(json.dumps({"factor": int(events["id"].max())}))
    return out
'''

_PREDICT = '''\
import json
from pathlib import Path

import polars as pl
from lakana import asset


@asset(inputs={"model": "models.slope", "events": "bronze.events"})
def predictions(model: Path, events: pl.DataFrame) -> pl.DataFrame:
    """Every id times the model's factor."""
    factor = json.loads(model.read_text())["factor"]
    return events.select(pl.col("id"), (pl.col("id") * factor).alias("prediction"))
'''

_MODEL_FILES = {
    "assets/models/slope.py": _TRAIN,
    "assets/gold/predictions.py": _PREDICT,
}


def _cli(stack: _Stack, home: Path, *args: str) -> str:
    """Run a `lakana` command in-process against the test server and return what it printed.

    The profile directory is a scratch directory, so nothing reaches the user's own.

    Parameters
    ----------
    stack
        The stack.
    home
        A scratch directory for the CLI's configuration.
    *args
        The command line after ``lakana``.

    Returns
    -------
    str
        The standard output; the command must have exited with status 0.
    """
    env = {
        "LAKANA_URL": stack.url,
        "LAKANA_TOKEN": TOKEN,
        "XDG_CONFIG_HOME": str(home),
        "COLUMNS": "200",
        "NO_COLOR": "1",
        "LAKANA_OUTPUT": None,
        "LAKANA_PROFILE": None,
    }
    result = CliRunner().invoke(build_app(), list(args), env=env)
    assert result.exit_code == 0, (args, result.output)
    return result.stdout


def test_train_then_predict(stack: _Stack, wheelhouse: Path, tmp_path: Path) -> None:
    """The milestone's test: a model version per run, used in the same run."""
    project = _deploy(stack, wheelhouse, _MODEL_FILES)
    env, admin = project.environment, stack.admin
    predictions = "SELECT id, prediction FROM gold.predictions ORDER BY id"
    (deployment,) = [d for d in admin.deployments.list(env) if d.repository == project.repository]
    manifest = admin.project_versions.get(env, deployment.project_version_id).manifest
    assert manifest["version"] == 3
    entries = {entry["table"]: entry for entry in manifest["assets"]}
    assert (entries["models.slope"]["output"], entries["models.slope"]["keep_versions"]) == (
        "artifact",
        2,
    )
    assert entries["gold.predictions"]["input_kinds"] == {"model": "artifact", "events": "table"}

    first_run = admin.runs.start(env, project.repository)
    stack.succeed(env, first_run.id)
    assert admin.artifacts.get(env, "models.slope").current_version == 1
    assert stack.sql(predictions, env) == [{"id": 1, "prediction": 2}, {"id": 2, "prediction": 4}]
    first = admin.artifacts.version(env, "models.slope", 1)
    assert (
        first.is_directory,
        first.file_count,
        first.run_id is not None,
        first.commit_sha is not None,
    ) == (False, 1, True, True)
    assert [(t.name, t.kind) for t in admin.tables.list(env, "models")] == [("slope", "ARTIFACT")]
    shown = _cli(stack, tmp_path / "config", "runs", "show", first_run.id, "--env", env)
    assert "models.slope" in shown and "gold.predictions" in shown and "None" not in shown
    listed = _cli(stack, tmp_path / "config", "artifact", "list", "--env", env)
    assert "slope" in listed and "None" not in listed

    admin.tables.append(env, "bronze.events", pa.table({"id": pa.array([3], pa.int64())}))
    stack.succeed(env, admin.runs.start(env, project.repository).id)
    assert admin.artifacts.get(env, "models.slope").current_version == 2
    assert stack.sql(predictions, env)[-1] == {"id": 3, "prediction": 9}
    got = admin.artifacts.download(env, "models.slope", tmp_path / "model")
    assert Path(got.path).read_text() == '{"factor": 3}'

    lineage = admin.tables.lineage(env, "gold.predictions", depth=2)
    kinds = {node.name: node.kind for node in lineage.nodes}
    assert kinds == {
        "gold.predictions": "TABLE",
        "models.slope": "ARTIFACT",
        "bronze.events": "TABLE",
    }
    by_name = {node.id: node.name for node in lineage.nodes}
    edges = {(by_name[e.source_id], by_name[e.target_id]): e for e in lineage.edges}
    assert edges["models.slope", "gold.predictions"].source_snapshot_id == 2
    assert edges["bronze.events", "models.slope"].target_snapshot_id == 2

    third_run = admin.runs.start(env, project.repository)
    stack.succeed(env, third_run.id)
    versions = admin.artifacts.versions(env, "models.slope")
    assert [(v.version, v.deleted_at is None) for v in versions] == [
        (3, True),
        (2, True),
        (1, False),
    ]
    with Session(stack.db) as session:
        artifact_id = session.scalars(
            select(Table.id)
            .join(Namespace, Table.namespace_id == Namespace.id)
            .join(Environment, Namespace.environment_id == Environment.id)
            .where(Environment.name == env, Table.name == "slope")
        ).one()
        kept = session.scalars(
            select(LineageEdge).where(
                LineageEdge.target_table_id == artifact_id, LineageEdge.target_snapshot_id == 1
            )
        ).all()
        assert [edge.source_name for edge in kept] == ["bronze.events"]
        attempts = session.scalars(
            select(ArtifactVersion).where(ArtifactVersion.artifact_id == artifact_id)
        ).all()
        assert all(row.task_run_id is not None and row.claim is not None for row in attempts)
    logs = admin.runs.log(env, third_run.id, "models.slope").text
    assert "artifact version: 3" in logs


_MODEL = '''\
import json
import os
from pathlib import Path

import lakana
from lakana import asset


@asset(output="artifact"EXTRA)
def NAME() -> Path:
    """A one-file model that remembers which process made it."""
    out = lakana.output_dir() / "model.json"
    out.write_text(json.dumps({"pid": os.getpid(), "name": "NAME"}))
    return out
'''

_USE = '''\
import json
import time
from pathlib import Path

import polars as pl
from lakana import asset

GATE = GATE_PATH
for _ in range(600):  # at most one minute: the test opens the gate as soon as it is ready
    if not GATE or Path(GATE).exists():
        break
    time.sleep(0.1)


@asset(inputs={"model": "models.MODEL"}EXTRA)
def NAME(model: Path) -> pl.DataFrame:
    """Report which process made the model that was read."""
    return pl.DataFrame({"pid": [json.loads(model.read_text())["pid"]]})
'''

_FORGED = '''\
import json
import os
from pathlib import Path

import lakana
from lakana import asset

FORGED_HASH = "f" * 64


@asset(output="artifact", retries=2)
def forged() -> Path:
    """Writes the harness's result file itself and exits as if it had succeeded."""
    record = {
        "status": "succeeded",
        "output": "artifact",
        "version": 7,
        "manifest_sha256": FORGED_HASH,
    }
    (lakana.output_dir().parent / "result.json").write_text(json.dumps(record))
    os._exit(0)
'''

_HEADER = """\
from pathlib import Path

import lakana
from lakana import asset

"""

_OUTSIDE = (
    _HEADER
    + '''
@asset(output="artifact", retries=2)
def outside() -> Path:
    """Returns a file that is not in the output directory."""
    return Path("/etc/passwd")
'''
)

_LINK = (
    _HEADER
    + '''
@asset(output="artifact", retries=2)
def link() -> Path:
    """Returns a symbolic link in the output directory."""
    out = lakana.output_dir()
    (out / "real.txt").write_text("real")
    (out / "model").symlink_to("/etc/passwd")
    return out / "model"
'''
)

_LINKDIR = (
    _HEADER
    + '''
@asset(output="artifact", retries=2)
def linkdir() -> Path:
    """Returns the output directory, which holds a symbolic link."""
    out = lakana.output_dir()
    (out / "real.txt").write_text("real")
    (out / "model").symlink_to("/etc/passwd")
    return out
'''
)


def _model(name: str, extra: str = "") -> str:
    """Write the source of an artifact asset.

    Parameters
    ----------
    name
        The asset's function name; the artifact is ``models.<name>``.
    extra
        More arguments for `@asset`, with a leading comma.

    Returns
    -------
    str
        The module's source.
    """
    return _MODEL.replace("EXTRA", extra).replace("NAME", name)


def _use(name: str, model: str, extra: str = "", gate: Path | None = None) -> str:
    """Write the source of a table asset that reads an artifact.

    Parameters
    ----------
    name
        The asset's function name; the table is ``gold.<name>``.
    model
        The artifact's name in `models`.
    extra
        More arguments for `@asset`, with a leading comma.
    gate
        A path the module waits for at import, before any input is downloaded, for at most a
        minute; `None` waits for nothing. The test creates the file when it is ready.

    Returns
    -------
    str
        The module's source.
    """
    return (
        _USE.replace("EXTRA", extra)
        .replace("NAME", name)
        .replace("MODEL", model)
        .replace("GATE_PATH", repr(str(gate) if gate else ""))
    )


def _artifact_files(gate: Path) -> dict[str, str]:
    """List the files of the project the adversarial artifact tests run assets of.

    Parameters
    ----------
    gate
        The file `gold.pruned_use` waits for at import.

    Returns
    -------
    dict[str, str]
        Repository path to source.
    """
    return {
        "assets/models/forged.py": _FORGED,
        "assets/models/outside.py": _OUTSIDE,
        "assets/models/link.py": _LINK,
        "assets/models/linkdir.py": _LINKDIR,
        "assets/models/hidden.py": _model("hidden"),
        "assets/models/unbuilt.py": _model("unbuilt"),
        "assets/gold/reads_hidden.py": _use("reads_hidden", "hidden"),
        "assets/gold/reads_unbuilt.py": _use("reads_unbuilt", "unbuilt"),
        "assets/models/flaky.py": _model("flaky", ', retries=1, timeout="10s"'),
        "assets/gold/flaky_use.py": _use("flaky_use", "flaky"),
        "assets/models/pruned.py": _model("pruned", ", keep_versions=1"),
        "assets/gold/pruned_use.py": _use("pruned_use", "pruned", ", retries=1", gate=gate),
        "assets/models/late.py": _model("late"),
    }


@pytest.fixture(scope="module")
def artifacts(
    stack: _Stack, wheelhouse: Path, tmp_path_factory: pytest.TempPathFactory
) -> _Project:
    """Deploy the project the adversarial artifact tests run assets of, one at a time.

    Parameters
    ----------
    stack
        The stack.
    wheelhouse
        The offline wheel directory.
    tmp_path_factory
        For the gate file.

    Returns
    -------
    _Project
        The deployed project.
    """
    gate = tmp_path_factory.mktemp("gate") / "open"
    return replace(_deploy(stack, wheelhouse, _artifact_files(gate)), gate=gate)


@dataclass(frozen=True)
class _Outcome:
    """Everything a person or a task could learn about one task of a run.

    Parameters
    ----------
    status
        The task's status.
    error
        Its last error.
    rows
        The rows it wrote.
    attempts
        Each attempt's outcome and error, oldest first.
    log
        The task log.
    """

    status: str
    error: str | None
    rows: int | None
    attempts: list[tuple[str, str | None]]
    log: str


def _task_outcome(stack: _Stack, env: str, run_id: str, asset: str) -> _Outcome:
    """Collect what the API shows of one task of a run.

    Parameters
    ----------
    stack
        The stack.
    env
        The environment.
    run_id
        The run.
    asset
        The task's asset.

    Returns
    -------
    _Outcome
        The task's status and error, each attempt's outcome and error, and the task log.
    """
    run = stack.admin.runs.get(env, run_id)
    (task,) = [t for t in run.tasks if t.asset == asset]
    return _Outcome(
        status=task.status,
        error=task.error_message,
        rows=task.rows_written,
        attempts=[(str(a.outcome), a.error_message) for a in _attempts(stack, run_id, asset)],
        log=stack.admin.runs.log(env, run_id, asset).text,
    )


def test_a_forged_success_is_not_a_version(
    stack: _Stack, artifacts: _Project, tmp_path: Path
) -> None:
    """End-to-end case 2: a result file written by the asset's own code proves nothing."""
    admin, env = stack.admin, artifacts.environment
    # A version of the same artifact exists before the forging run, stored by another task run
    # whose claim number is the same as the forger's (1): only the task run tells them apart.
    try:
        admin.namespaces.get(env, "models")
    except NotFound:  # the namespace is made by the first run that stores an artifact in it
        admin.namespaces.create(env, "models")
    earlier = bound_attempt(
        stack.db, admin, env, asset="models.forged", inputs={}, output="artifact"
    )
    source = tmp_path / "earlier.json"
    source.write_text('{"pid": 1, "name": "earlier"}')
    with lakana.Client(stack.url, earlier.token, timeout=60.0) as task:
        assert task.artifacts.upload(env, "models.forged", source).version == 1
    before = admin.artifacts.versions(env, "models.forged")
    assert [v.version for v in before] == [1]
    run = admin.runs.start(env, artifacts.repository, select=["models.forged"])
    assert stack.finish(env, run.id).status == "failed"
    outcome = _task_outcome(stack, env, run.id, "models.forged")
    ((_, message),) = outcome.attempts  # permanent: retries=2 and still one attempt
    assert message == (
        "the task reported success without storing a version of its artifact; return the path "
        "from the asset function and do not exit the process or write the result file yourself"
    )
    assert admin.artifacts.versions(env, "models.forged") == before  # nothing was added
    shown = repr((outcome, admin.runs.get(env, run.id)))
    assert "f" * 64 not in shown and "version: 7" not in shown and "version 7" not in shown


def _upload_rows(session: Session, env: str) -> int:
    """Count the upload rows of every artifact of an environment.

    Parameters
    ----------
    session
        A database session.
    env
        The environment.

    Returns
    -------
    int
        The number of rows, open or committed.
    """
    return (
        session.scalar(
            select(func.count())
            .select_from(ArtifactUpload)
            .join(Table, Table.id == ArtifactUpload.artifact_id)
            .join(Namespace, Table.namespace_id == Namespace.id)
            .join(Environment, Namespace.environment_id == Environment.id)
            .where(Environment.name == env)
        )
        or 0
    )


def test_a_hostile_return_value_fails_for_good_and_uploads_nothing(
    stack: _Stack, artifacts: _Project
) -> None:
    """End-to-end case 8: a path outside the output directory, and two links."""
    admin, env = stack.admin, artifacts.environment
    names = ("outside", "link", "linkdir")
    with Session(stack.db) as session:
        uploads_before = _upload_rows(session, env)
    run = admin.runs.start(env, artifacts.repository, select=[f"models.{n}" for n in names])
    assert stack.finish(env, run.id).status == "failed"
    expected = {
        "outside": "which is not inside lakana.output_dir()",
        "link": "which passes through a symbolic link",
        "linkdir": "is a symbolic link; an artifact holds only regular files and directories",
    }
    for name in names:
        outcome = _task_outcome(stack, env, run.id, f"models.{name}")
        ((kind, message),) = outcome.attempts
        assert kind == "failed" and expected[name] in str(message), outcome
    with Session(stack.db) as session:
        environment = session.scalars(select(Environment).where(Environment.name == env)).one()
        ids = session.scalars(
            select(Table.id)
            .join(Namespace, Table.namespace_id == Namespace.id)
            .where(Namespace.environment_id == environment.id, Table.name.in_(names))
        ).all()
        # No upload was begun: the harness refuses before it calls the server, and the
        # directory with a link is refused by the SDK's local scan before the first request.
        # So not even an artifact row exists, and the environment has no upload row it did not
        # have before.
        assert ids == []
        assert _upload_rows(session, env) == uploads_before
        assert (
            session.scalars(select(ArtifactUpload).where(ArtifactUpload.artifact_id.in_(ids))).all()
            == []
        )
        assert (
            session.scalars(
                select(ArtifactVersion).where(ArtifactVersion.artifact_id.in_(ids))
            ).all()
            == []
        )


def _model_pid(stack: _Stack, env: str, name: str, version: int, directory: Path) -> int:
    """Download a version of a one-file model made by `_model` and read its process id.

    Parameters
    ----------
    stack
        The stack.
    env
        The environment.
    name
        The artifact's dotted name.
    version
        The version.
    directory
        A directory that does not exist yet.

    Returns
    -------
    int
        The ``pid`` the model's producer recorded.
    """
    got = stack.admin.artifacts.download(env, name, directory, version=version)
    return int(json.loads(Path(got.path).read_text())["pid"])


def test_a_consumer_that_may_not_read_an_artifact_cannot_tell_whether_it_exists(
    stack: _Stack, artifacts: _Project
) -> None:
    """End-to-end case 4: one outcome, one text, no version, whatever exists."""
    admin, env, repo = stack.admin, artifacts.environment, artifacts.repository
    stack.succeed(env, admin.runs.start(env, repo, select=["models.hidden"]).id)
    assert admin.artifacts.get(env, "models.hidden").current_version == 1
    with pytest.raises(NotFound):  # deployed, never run: the other half of the comparison
        admin.artifacts.get(env, "models.unbuilt")
    # the control: a principal that may read runs the consumer, so the failures below are about read
    stack.succeed(env, admin.runs.start(env, repo, select=["gold.reads_hidden"]).id)
    account = f"restricted-{env}"
    admin.principals.create(account, "SERVICE_ACCOUNT")
    admin.grants.grant(
        principal=account,
        role="DATA_ENGINEER",
        kind="NAMESPACE",
        environment=env,
        namespace="gold",
    )
    admin.deployments.set_run_as(env, repo, account)
    try:
        outcomes = {}
        for name in ("hidden", "unbuilt"):
            run = admin.runs.start(env, repo, select=[f"gold.reads_{name}"])
            assert stack.finish(env, run.id).status == "failed"
            text = json.dumps(asdict(_task_outcome(stack, env, run.id, f"gold.reads_{name}")))
            text = re.sub(r"\d{4}-\d\d-\d\dT[\d:.]+(\+00:00)?", "<time>", text)
            outcomes[name] = text.replace(name, "<name>")
    finally:
        admin.deployments.clear_run_as(env, repo)
    assert outcomes["hidden"] == outcomes["unbuilt"]
    shown = json.loads(outcomes["hidden"])
    assert shown["attempts"] == [
        [
            "failed",
            f"models.<name> cannot be read by the run's principal '{account}', or does not "
            "exist; grant read on it or check the name",
        ]
    ]
    assert "version" not in shown["error"] and "version" not in str(shown["attempts"])


def test_a_consumer_deployed_before_its_producer_ran_needs_no_redeploy(
    stack: _Stack, artifacts: _Project, wheelhouse: Path, tmp_path: Path
) -> None:
    """End-to-end case 5: the kind comes from the producer's deploy."""
    admin, env = stack.admin, artifacts.environment
    consumer = _deploy(
        stack, wheelhouse, {"assets/gold/late_use.py": _use("late_use", "late")}, environment=env
    )
    (deployed,) = [d for d in admin.deployments.list(env) if d.repository == consumer.repository]
    manifest = admin.project_versions.get(env, deployed.project_version_id).manifest
    (entry,) = [e for e in manifest["assets"] if e["table"] == "gold.late_use"]
    assert entry["input_kinds"] == {"model": "artifact"}
    with pytest.raises(NotFound):
        admin.artifacts.get(env, "models.late")
    stack.succeed(env, admin.runs.start(env, artifacts.repository, select=["models.late"]).id)
    stack.succeed(env, admin.runs.start(env, consumer.repository).id)
    (after,) = [d for d in admin.deployments.list(env) if d.repository == consumer.repository]
    assert after.project_version_id == deployed.project_version_id
    pid = _model_pid(stack, env, "models.late", 1, tmp_path / "late")
    assert stack.sql("SELECT pid FROM gold.late_use", env) == [{"pid": pid}]


def test_an_attempt_killed_after_its_commit_leaves_the_version_and_the_retry_stores_a_higher_one(
    stack: _Stack, artifacts: _Project, tmp_path: Path
) -> None:
    """End-to-end case 6: the consumer of the same run reads the retry's version."""
    admin, env, repo = stack.admin, artifacts.environment, artifacts.repository
    stack.hold.arm("flaky")
    try:
        run = admin.runs.start(env, repo, select=["models.flaky", "gold.flaky_use"])
        stack.succeed(env, run.id)
    finally:
        stack.hold.release.set()
    assert stack.hold.held.is_set()
    attempts = _attempts(stack, run.id, "models.flaky")
    assert [a.outcome for a in attempts] == [AttemptOutcome.TIMED_OUT, AttemptOutcome.SUCCEEDED]
    versions = admin.artifacts.versions(env, "models.flaky")
    assert [(v.version, v.deleted_at) for v in versions] == [(2, None), (1, None)]
    with Session(stack.db) as session:
        claims = session.scalars(
            select(ArtifactVersion.claim)
            .join(Table, Table.id == ArtifactVersion.artifact_id)
            .join(Namespace, Table.namespace_id == Namespace.id)
            .join(Environment, Namespace.environment_id == Environment.id)
            .where(Environment.name == env, Table.name == "flaky")
            .order_by(ArtifactVersion.version)
        ).all()
    assert claims == [1, 2]
    killed = _model_pid(stack, env, "models.flaky", 1, tmp_path / "v1")
    retry = _model_pid(stack, env, "models.flaky", 2, tmp_path / "v2")
    assert killed != retry
    assert stack.sql("SELECT pid FROM gold.flaky_use", env) == [{"pid": retry}]


def test_a_version_pruned_before_the_download_fails_the_attempt_for_a_retry(
    stack: _Stack, artifacts: _Project, tmp_path: Path
) -> None:
    """End-to-end case 7: `keep_versions=1` prunes the pinned version; the retry reads v2."""
    admin, env, repo = stack.admin, artifacts.environment, artifacts.repository
    stack.succeed(env, admin.runs.start(env, repo, select=["models.pruned"]).id)
    assert admin.artifacts.get(env, "models.pruned").current_version == 1
    assert artifacts.gate is not None
    gate = artifacts.gate
    gate.unlink(missing_ok=True)  # closed: the consumer's module waits for it before any download
    run = admin.runs.start(env, repo, select=["gold.pruned_use"])
    errors: list[BaseException] = []
    stored: list[int] = []

    def interfere() -> None:
        """Commit version 2 as the producer's task once the consumer's attempt has its token."""
        try:
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                if any(token.revoked_at is None for token in _tokens(stack, run.id)):
                    break
                time.sleep(0.1)
            producer = bound_attempt(
                stack.db,
                admin,
                env,
                asset="models.pruned",
                inputs={},
                output="artifact",
                keep_versions=1,
            )
            source = tmp_path / "interfered"
            source.mkdir()
            (source / "model.json").write_text(json.dumps({"pid": 4242, "name": "interfered"}))
            with lakana.Client(stack.url, producer.token, timeout=60.0) as task:
                stored.append(
                    task.artifacts.upload(env, "models.pruned", source / "model.json").version
                )
        except BaseException as error:
            errors.append(error)
        finally:
            gate.touch()  # open the gate for the consumer's attempts, whatever happened

    thread = threading.Thread(target=interfere, daemon=True)
    thread.start()
    finished = stack.finish(env, run.id)
    thread.join(timeout=30)
    assert stored == [2], errors
    outcome = _task_outcome(stack, env, run.id, "gold.pruned_use")
    assert finished.status == "succeeded", outcome
    attempts = _attempts(stack, run.id, "gold.pruned_use")
    assert [a.outcome for a in attempts] == [AttemptOutcome.FAILED, AttemptOutcome.SUCCEEDED]
    assert attempts[0].error_message == (
        "reading input 'model' (models.pruned, version 1) failed: "
        "Version 1 of models.pruned was deleted; its bytes are gone. "
        f"Read a version that exists: `lakana artifact versions list models.pruned --env {env}` "
        "shows which."
    )
    assert [
        (v.version, v.deleted_at is None) for v in admin.artifacts.versions(env, "models.pruned")
    ] == [
        (2, True),
        (1, False),
    ]
    assert stack.sql("SELECT pid FROM gold.pruned_use", env) == [{"pid": 4242}]


_SQL_READS_MODEL = "SELECT * FROM models.base\n"

_BAD_PARAMETER = '''\
import json
from pathlib import Path

import polars as pl
from lakana import asset


@asset(inputs={"modèle": "models.base"})
def bad_name(modèle: Path) -> pl.DataFrame:
    """A parameter the harness cannot name a directory after."""
    return pl.DataFrame({"n": [1]})
'''


def test_the_deploy_refuses_what_an_artifact_cannot_do(stack: _Stack, wheelhouse: Path) -> None:
    """End-to-end case 9: three mistakes, each a deploy issue with its text; nothing deployed."""
    files = {
        "assets/models/base.py": _model("base"),
        "assets/gold/reads_it.sql": _SQL_READS_MODEL,
        "assets/bronze/events.py": _model("events"),
        "assets/gold/bad_name.py": _BAD_PARAMETER,
    }
    project = _deploy(stack, wheelhouse, files, status="failed")
    assert project.deploy is not None
    message = project.deploy.error_message or ""
    env = project.environment
    assert "`models.base` is an artifact; only a Python asset can read it." in message
    assert f"`bronze.events` already exists in {env} as a table; drop it first" in message
    assert (
        "parameter 'modèle' reads the artifact `models.base`, which the task stores in a "
        "directory of that name: rename the parameter" in message
    )
    assert [
        d for d in stack.admin.deployments.list(env) if d.repository == project.repository
    ] == []


_WIDE_TIMEOUT = 25
"""Attempt 1's whole budget: the process starts, reads its input and replaces the table within
it, and then its timeout ends it while the reply is held."""

_WIDE = f'''\
import polars as pl
from lakana import asset


@asset(inputs={{"events": "bronze.events"}}, retries=1, timeout="{_WIDE_TIMEOUT}s")
def wide(events: pl.DataFrame) -> pl.DataFrame:
    """Every event; the check of this table refuses the id 2."""
    return events
'''


def test_a_second_attempts_failed_check_sets_the_table_back_to_before_the_first_attempt(
    stack: _Stack, wheelhouse: Path
) -> None:
    """The baseline of a Python asset with a check, with the real harness process.

    Attempt 1 replaces the table and is then killed by its own timeout: the answer to its
    upload is held back, so the write is committed and the process never learns it. Attempt 2
    replaces the table again and its check fails. The table must go back to what it was
    before the task run, not to what attempt 1 left, and nothing downstream is built.
    """
    project = _deploy(
        stack,
        wheelhouse,
        {
            "assets/silver/wide.py": _WIDE,
            "assets/gold/after_wide.sql": "SELECT count(*) AS n FROM silver.wide\n",
            "checks/silver/wide/no_twos.sql": "SELECT id FROM silver.wide WHERE id = 2\n",
        },
    )
    admin, env = stack.admin, project.environment
    admin.tables.create(env, "silver.wide", schema=pa.schema([pa.field("id", pa.int64())]))
    before = admin.tables.append(
        env, "silver.wide", pa.table({"id": pa.array([7, 8], pa.int64())})
    ).snapshot_id
    assert before is not None
    stack.hold.arm("wide", seconds=_WIDE_TIMEOUT + 30)
    run = admin.runs.start(env, project.repository)
    during: list[tuple[int | None, int | None]] = []

    def look_while_the_reply_is_held() -> None:
        """Read the table and the task run's tip once attempt 1's upload is committed.

        Its reply is still held then, so the process has not learned of its write, and
        nothing has been rolled back. Read on a client and a session of this thread's own.
        """
        if not stack.hold.held.wait(_RUN_SECONDS):
            return
        with lakana.Client(stack.url, TOKEN, timeout=60.0) as other, Session(stack.db) as session:
            tip = session.scalars(
                select(TaskRun.own_snapshot_id).where(
                    TaskRun.run_id == uuid.UUID(run.id), TaskRun.asset == "silver.wide"
                )
            ).one()
            during.append((other.tables.get(env, "silver.wide").snapshot_id, tip))

    watcher = threading.Thread(target=look_while_the_reply_is_held, daemon=True)
    watcher.start()
    try:
        finished = stack.finish(env, run.id)
    finally:
        stack.hold.release.set()
        stack.hold.held.set()  # lets the watcher end if the upload was never held
        watcher.join(timeout=60)
    assert not watcher.is_alive()
    assert stack.hold.status == 200, (
        f"attempt 1 did not replace silver.wide within its {_WIDE_TIMEOUT} s timeout: the "
        f"reply the test holds back answered {stack.hold.status} (None: no replace arrived), "
        "so the case under test never happened. The attempts and the task's log say why. "
        f"Run: {finished}. Attempts: "
        + "; ".join(
            f"{a.attempt} {a.outcome} {a.failure_code}: {a.error_message}"
            for a in _attempts(stack, run.id, "silver.wide")
        )
        + f". Log:\n{stack.admin.runs.log(env, run.id, 'silver.wide').text}"
    )
    # Attempt 1 really committed: while its reply was held the table was at a new snapshot,
    # and that snapshot was the tip of the task run's own writes.
    ((first_snapshot, first_tip),) = during
    assert first_snapshot not in (None, before) and first_tip == first_snapshot

    assert finished.status == "failed", finished
    attempts = _attempts(stack, run.id, "silver.wide")
    assert [a.outcome for a in attempts] == [AttemptOutcome.TIMED_OUT, AttemptOutcome.FAILED]
    with Session(stack.db) as session:
        row = session.scalars(
            select(TaskRun).where(
                TaskRun.run_id == uuid.UUID(run.id), TaskRun.asset == "silver.wide"
            )
        ).one()
        # The baseline was recorded once, before the first process, and stays. The rollback
        # landed, so it cleared the stamp and the tip of the task run's own writes with it.
        assert (row.baseline_snapshot_id, row.baseline_publishes) == (before, False)
        assert (row.written_table_id, row.written_snapshot_after, row.own_snapshot_id) == (
            None,
            None,
            None,
        )
        rolled = session.scalars(
            select(AuditEvent).where(
                AuditEvent.event_type == AuditEventType.TABLE_ROLLED_BACK,
                AuditEvent.securable_environment == env,
            )
        ).all()
        details = [dict(event.detail) for event in rolled]
        expected = check_failure_message(
            verdict(results_of_task(session, row.id)).blocking,
            table="silver.wide",
            run_id=row.run_id,
            environment=env,
            rollback=Rollback(RollbackOutcome.ROLLED_BACK),
        )
    task = next(t for t in finished.tasks if t.asset == "silver.wide")
    assert (task.status, task.attempt, task.error_message) == ("failed", 2, expected)
    assert expected.startswith(
        "Check `silver.wide/no_twos` failed: 1 row (checks/silver/wide/no_twos.sql). "
        + ROLLBACK_SENTENCES[RollbackOutcome.ROLLED_BACK]
    )
    (result,) = admin.checks.for_run(env, run.id)
    assert (result.outcome, result.failing_rows, result.rollback_outcome) == (
        "failed",
        1,
        "rolled_back",
    )
    # What was set back is attempt 2's snapshot (the one its check read), a third one:
    # neither the table before the run nor what attempt 1 left.
    second_snapshot = result.checked_snapshot_id
    assert second_snapshot not in (None, before, first_snapshot)
    assert [(d["outcome"], d["before"], d["after"]) for d in details] == [
        ("rolled_back", before, second_snapshot)
    ]
    assert admin.tables.get(env, "silver.wide").snapshot_id == before

    assert stack.sql("SELECT id FROM silver.wide ORDER BY id", env) == [{"id": 7}, {"id": 8}]
    below = next(t for t in finished.tasks if t.asset == "gold.after_wide")
    assert (below.status, below.attempt) == ("upstream_failed", 0)
    with pytest.raises(NotFound):
        admin.tables.get(env, "gold.after_wide")


_APPEND_LOG = '''\
import polars as pl
from lakana import asset


@asset(inputs={"events": "bronze.events"}, write="append", watermark="id")
def orders_log(events: pl.DataFrame, ctx) -> pl.DataFrame:
    """Every event once: only the rows past the table's watermark."""
    return events.filter(pl.col("id") > ctx.watermark(default=0))
'''

_MERGE_LATEST = '''\
import polars as pl
from lakana import asset


@asset(inputs={"events": "bronze.events"}, write="merge", key=["id"])
def latest(events: pl.DataFrame) -> pl.DataFrame:
    """One row per event, with the number of events the run saw."""
    return events.with_columns(seen=pl.lit(events.height, dtype=pl.Int64))
'''


def test_python_assets_append_past_the_watermark_and_merge_on_their_key(
    stack: _Stack, wheelhouse: Path
) -> None:
    """Three runs with the real harness: create, append the new row, find nothing and write nothing.

    The merge asset runs beside it: its second run updates the rows it wrote and adds the new one.
    """
    project = _deploy(
        stack,
        wheelhouse,
        {"assets/bronze/orders_log.py": _APPEND_LOG, "assets/silver/latest.py": _MERGE_LATEST},
    )
    admin, env = stack.admin, project.environment
    log_rows = "SELECT id FROM bronze.orders_log ORDER BY id"
    latest_rows = "SELECT id, seen FROM silver.latest ORDER BY id"

    stack.succeed(env, admin.runs.start(env, project.repository).id)
    assert stack.sql(log_rows, env) == [{"id": 1}, {"id": 2}]
    assert stack.sql(latest_rows, env) == [{"id": 1, "seen": 2}, {"id": 2, "seen": 2}]

    admin.tables.append(env, "bronze.events", pa.table({"id": pa.array([3], pa.int64())}))
    second = admin.runs.start(env, project.repository)
    stack.succeed(env, second.id)
    assert stack.sql(log_rows, env) == [{"id": 1}, {"id": 2}, {"id": 3}]
    assert stack.sql(latest_rows, env) == [
        {"id": 1, "seen": 3},
        {"id": 2, "seen": 3},
        {"id": 3, "seen": 3},
    ]
    appended = admin.runs.log(env, second.id, "bronze.orders_log").text
    assert "write mode: append" in appended
    assert "watermark: long:2" in appended
    assert "rows: 1" in appended
    merged = admin.runs.log(env, second.id, "silver.latest").text
    assert "write mode: merge" in merged
    assert "rows: 3" in merged  # two updated, one inserted

    snapshot = admin.tables.get(env, "bronze.orders_log").snapshot_id
    third = admin.runs.start(env, project.repository)
    stack.succeed(env, third.id)
    assert admin.tables.get(env, "bronze.orders_log").snapshot_id == snapshot
    assert stack.sql(log_rows, env) == [{"id": 1}, {"id": 2}, {"id": 3}]
    nothing = admin.runs.log(env, third.id, "bronze.orders_log").text
    assert "watermark: long:3" in nothing
    assert "rows: 0" in nothing
    assert "snapshot:" not in nothing


_CRM_PASSWORD = "crm-e2e-s3cr3t"

_CRM_ROWS = '''\
import polars as pl
from lakana import asset
from lakana.ingest import connections


@asset(connections=["crm-db"])
def crm_rows() -> pl.DataFrame:
    """Fetch the declared connection, print its password, and record what it said."""
    crm = connections.get("crm-db")
    print("password:", crm.secret["password"])
    print("secret:", crm.secret)
    return pl.DataFrame({"host": [crm.address["host"]], "kind": [crm.kind]})
'''


def test_a_python_asset_declaring_a_connection_records_it_in_lineage(
    stack: _Stack, wheelhouse: Path
) -> None:
    """Deploy, run with the real harness: the task fetches its secret, the log hides it.

    The write records the declared connection as a source; the admin sees it in lineage.
    """
    admin, env = stack.admin, unique_name()
    stack.services.environment(admin, env)
    admin.connections.create(
        env,
        "crm-db",
        kind="postgresql",
        address={"host": "db.example.com", "database": "crm"},
        settings={"user": "reader"},
        secret_values={"password": _CRM_PASSWORD},
    )
    project = _deploy(stack, wheelhouse, {"assets/bronze/crm_rows.py": _CRM_ROWS}, environment=env)

    run = admin.runs.start(env, project.repository)
    stack.succeed(env, run.id)

    assert stack.sql("SELECT host, kind FROM bronze.crm_rows", env) == [
        {"host": "db.example.com", "kind": "postgresql"}
    ]
    log = admin.runs.log(env, run.id, "bronze.crm_rows").text
    assert "password: ***" in log
    assert "secret: {'password': '***'}" in log
    assert _CRM_PASSWORD not in log
    lineage = admin.tables.lineage(env, "bronze.crm_rows")
    (node,) = [n for n in lineage.nodes if n.kind == "CONNECTION"]
    assert (node.name, node.connection_kind, node.dropped) == ("crm-db", "postgresql", False)
    with Session(stack.db) as session:
        fetched = session.execute(
            select(AuditEvent).where(
                AuditEvent.event_type == AuditEventType.CONNECTION_CREDENTIAL_FETCHED,
                AuditEvent.securable_environment == env,
            )
        ).scalar_one()
    assert fetched.detail["asset"] == "bronze.crm_rows"
    assert _CRM_PASSWORD not in json.dumps(fetched.detail)
