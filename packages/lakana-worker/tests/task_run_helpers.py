"""Build and drive a task run on SQLite, local storage and real DuckDB.

For the tests of `lakana_worker.tasks`, `task_checks`, `task_lease` and `python_tasks`. The
`world` fixture (in `conftest.py`) builds a `World`; the functions here start a run, claim its
tasks, run them with an engine, and read back what was recorded.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import sqlalchemy
from sqlalchemy import select, update
from sqlalchemy.orm import Session

import lakana_worker.executor as executor_module
from lakana_core.auth import (
    AllowAllAuthorization,
    AuthorizationService,
    PrincipalRef,
)
from lakana_core.catalog.identity import resolve_table_id
from lakana_core.config import QueryLimitsConfig
from lakana_core.identifiers import TableRef
from lakana_core.models import (
    Environment,
    JobVersion,
    Principal,
    PrincipalType,
    QueryRun,
    QueryRunStatus,
    Run,
    TaskAttempt,
    TaskRun,
)
from lakana_core.runs.selection import Selection
from lakana_core.runs.service import RunService, task_log_path
from lakana_core.runs.store import scheduler_tick
from lakana_core.runs.tasks import (
    ClaimedTask,
    claim_task,
)
from lakana_core.storage.resolver import StorageResolver
from lakana_worker.tasks import RunEngine, TaskSettings

_SLOW_SQL = "SELECT sum(hash(a.range + b.range)) AS n FROM range(40000) a, range(40000) b"
"""A query that runs for seconds, far longer than a test's limit: a cross join of two 40,000-row
ranges, single threaded (9.4 to 9.6 s at 1, 2 and 4 threads) and no growing memory (59 MB). If the
limit under test fails it still ends within a minute (it was a cross join of two 1,000,000-row
lists, hours)."""

_CODE = b"pretend tarball"
_CODE_PATH = "_artifacts/code/analytics/a.tar.gz"


def _entry(table: str, sql: str, **extra: object) -> dict[str, object]:
    """Build a manifest asset entry.

    Parameters
    ----------
    table
        Dotted name.
    sql
        Its SELECT.
    **extra
        Overrides.

    Returns
    -------
    dict
        The entry.
    """
    entry: dict[str, object] = {
        "table": table,
        "sql": sql,
        "write_mode": "replace",
        "first_write_mode": "create",
        "incremental_key": None,
        "evolve_schema": False,
        "retries": 2,
        "retry_delay_seconds": 30,
        "timeout_seconds": 900,
    }
    entry.update(extra)
    return entry


@dataclass
class World:
    """A file database with `dev`, a deployed repository and its code tarball.

    Parameters
    ----------
    engine
        The engine.
    admin
        The admin principal (also the run principal: `dev` is unprotected).
    storage_root
        The environment's storage root URL.
    version_id
        The deployed project version.
    """

    engine: sqlalchemy.Engine
    admin: PrincipalRef
    storage_root: str
    version_id: uuid.UUID

    def session(self) -> Session:
        """Open a session.

        Returns
        -------
        Session
            A new session.
        """
        return Session(self.engine)

    def set_manifest(
        self,
        *entries: dict[str, object],
        edges: list[list[str]] | None = None,
        checks: list[dict[str, object]] | None = None,
    ) -> None:
        """Replace the deployed manifest.

        Parameters
        ----------
        *entries
            Asset entries.
        edges
            DAG edges.
        checks
            Manifest check entries; when there are some, the manifest is written as version 4,
            the version that knows checks, as a deploy writes it (a deploy writes version 4 only
            for a project with a check); without any it has no `checks` key.
        """
        manifest: dict[str, object] = {
            "version": 1,
            "assets": list(entries),
            "edges": edges or [],
        }
        if checks:
            manifest.update(version=4, checks=checks)
        with self.session() as session:
            version = session.get(JobVersion, self.version_id)
            assert version is not None
            version.manifest_json = manifest
            session.commit()


def _engine(
    world: World,
    *,
    authz: Callable[[Session], AuthorizationService] | None = None,
    drain: threading.Event | None = None,
    grace: float = 0.0,
    renew: float = 0.05,
    limits: QueryLimitsConfig | None = None,
) -> RunEngine:
    """Build the run engine the poller would.

    Parameters
    ----------
    world
        The world.
    authz
        Authorization factory; allow-all when `None`.
    drain
        The drain event.
    grace
        Drain grace in seconds.
    renew
        Seconds between lease renewals.
    limits
        The worker's query limits; the defaults when `None`.

    Returns
    -------
    RunEngine
        The engine.
    """
    return RunEngine(
        session_factory=world.session,
        authz_factory=authz or (lambda _s: AllowAllAuthorization()),
        settings=TaskSettings(
            worker_id="w1",
            renew_seconds=renew,
            drain_grace_seconds=grace,
            limits=limits or QueryLimitsConfig(),
        ),
        drain=drain or threading.Event(),
        rand=lambda: 0.5,
    )


def _start(world: World, selection: Selection | None = None) -> uuid.UUID:
    """Start a run and tick once so its roots are ready.

    Parameters
    ----------
    world
        The world.
    selection
        The selection; everything when `None`.

    Returns
    -------
    uuid.UUID
        The run's id.
    """
    with world.session() as session:
        run = RunService(session, AllowAllAuthorization()).start(
            world.admin, "dev", "analytics", selection or Selection()
        )
        scheduler_tick(session, datetime.now(UTC), lambda: 0.5)
        return run.id


def _claim(world: World) -> ClaimedTask:
    """Claim the next ready task as `w1`.

    Parameters
    ----------
    world
        The world.

    Returns
    -------
    ClaimedTask
        The claim.
    """
    with world.session() as session:
        claimed = claim_task(session, "w1", 60)
    assert claimed is not None
    return claimed


def _task(world: World, asset: str) -> TaskRun:
    """Read a task run by asset (most recent run).

    Parameters
    ----------
    world
        The world.
    asset
        Its asset.

    Returns
    -------
    TaskRun
        The row (detached).
    """
    with world.session() as session:
        task = session.scalars(
            select(TaskRun).join(Run).where(TaskRun.asset == asset).order_by(Run.created_at.desc())
        ).first()
        assert task is not None
        session.expunge(task)
        return task


def _attempts(world: World, task_run_id: uuid.UUID) -> list[TaskAttempt]:
    """Read a task run's attempts.

    Parameters
    ----------
    world
        The world.
    task_run_id
        The task run.

    Returns
    -------
    list of TaskAttempt
        Oldest first (detached).
    """
    with world.session() as session:
        rows = list(
            session.scalars(
                select(TaskAttempt)
                .where(TaskAttempt.task_run_id == task_run_id)
                .order_by(TaskAttempt.started_at)
            )
        )
        session.expunge_all()
        return rows


def _log(world: World, run_id: uuid.UUID, asset: str) -> str:
    """Read a task's free-text log.

    Parameters
    ----------
    world
        The world.
    run_id
        The run.
    asset
        The task.

    Returns
    -------
    str
        The log.
    """
    with world.session() as session:
        storage = StorageResolver(session).for_environment("dev").storage
        return storage.read(task_log_path(run_id, asset)).decode()


def _exists(world: World, dotted: str) -> bool:
    """Report whether a table exists in `dev`.

    Parameters
    ----------
    world
        The world.
    dotted
        The table.

    Returns
    -------
    bool
        Whether it exists.
    """
    ref = TableRef.from_dotted("dev", dotted)
    with world.session() as session:
        return resolve_table_id(session, "dev", ref.namespace.joined(), ref.name) is not None


def _fake_run_one(
    failure: BaseException | None,
    before: Callable[[], None] | None = None,
    *,
    error_message: str | None = None,
    succeed: bool = False,
) -> Callable[..., BaseException | None]:
    """Replace `run_one`: optionally act, then finish the query run failed with `failure`.

    Parameters
    ----------
    failure
        What the fake returns (and writes as the error).
    before
        Called first, inside the cancellation block.
    error_message
        The redacted text stored on the query run; `str(failure)` when `None`.
    succeed
        Finish the query run as succeeded (a write that landed) instead.

    Returns
    -------
    callable
        A stand-in with `run_one`'s signature.
    """

    def fake(
        session: Session,
        query_run_id: uuid.UUID,
        authz: object,
        key_provider: object = None,
        limits: object = None,
        **kwargs: object,
    ) -> BaseException | None:
        if before is not None:
            before()
        run = session.get(QueryRun, query_run_id)
        assert run is not None
        if succeed:
            run.status = QueryRunStatus.SUCCEEDED
            run.rows_written = 3
        else:
            run.status = QueryRunStatus.FAILED
            run.error_message = error_message if error_message is not None else str(failure)
        session.commit()
        return failure

    return fake


def _cancel_and_wait(world: World, run_id: uuid.UUID) -> Callable[[], None]:
    """Build a `before` hook that requests cancellation and waits for the interrupt.

    Parameters
    ----------
    world
        The world.
    run_id
        The run to cancel.

    Returns
    -------
    callable
        The hook.
    """

    def hook() -> None:
        with world.session() as session:
            session.execute(update(Run).where(Run.id == run_id).values(cancel_requested=True))
            session.commit()
        event = executor_module._CANCEL.get()
        assert event is not None
        assert event.wait(5)

    return hook


def _three_principals(world: World, run_id: uuid.UUID) -> dict[str, uuid.UUID]:
    """Make the run's run-as principal, its trigger and the deployer three different people.

    Parameters
    ----------
    world
        The world.
    run_id
        The run to change.

    Returns
    -------
    dict
        Ids by ``runner``, ``trigger`` and ``deployer`` (created on the first call); the
        admin is a fourth principal.
    """
    ids: dict[str, uuid.UUID] = {}
    with world.session() as session:
        for name in ("runner", "trigger", "deployer"):
            row = session.scalars(select(Principal).where(Principal.name == name)).first()
            if row is None:
                row = Principal(id=uuid.uuid4(), type=PrincipalType.USER, name=name, is_active=True)
                session.add(row)
                session.flush()
            ids[name] = row.id
        run = session.get(Run, run_id)
        version = session.get(JobVersion, world.version_id)
        assert run is not None and version is not None
        run.run_as_principal_id = ids["runner"]
        run.triggered_by = ids["trigger"]
        version.deployed_by = ids["deployer"]
        session.commit()
    return ids


def _protect(world: World) -> None:
    """Make `dev` a protected environment (only task runs may write).

    Parameters
    ----------
    world
        The world.
    """
    with world.session() as session:
        session.execute(update(Environment).values(is_protected=True))
        session.commit()


def _set_cancel_requested(world: World, run_id: uuid.UUID) -> None:
    """Flag a run for cancellation, as `POST /runs/{id}/cancel` does.

    Parameters
    ----------
    world
        The world.
    run_id
        The run.
    """
    with world.session() as session:
        session.execute(update(Run).where(Run.id == run_id).values(cancel_requested=True))
        session.commit()
