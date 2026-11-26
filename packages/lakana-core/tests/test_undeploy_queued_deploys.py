"""An undeploy and a deploy or rollback of the same repository (#838).

An undeploy is refused while a deploy or rollback of the repository is queued or running in that
environment, and a submit and an undeploy take turns on one lock, so a deploy queued "at the same
moment" is either seen by the undeploy (which is refused) or queued after it (and deploys again,
as asked). The two-session scenarios are shared with the Postgres module
(`test_undeploy_queued_deploys_postgres_integration.py`).
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from core_test_helpers import _prepared_catalog
from sqlalchemy import event, select, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.orm import Session
from sqlalchemy.pool import ConnectionPoolEntry
from sqlalchemy.sql import ClauseElement
from test_projects_deploys import _claimed, _deployed, _manifest, _repo

from lakana_core.auth import AllowAllAuthorization, PrincipalRef
from lakana_core.config import DatabaseConfig
from lakana_core.db import create_engine
from lakana_core.errors import ConflictError, NotFoundError
from lakana_core.models import (
    Base,
    DeployKind,
    DeployRun,
    DeployStatus,
    Environment,
    EnvironmentDeployment,
    JobVersion,
    Principal,
    PrincipalType,
    Repository,
)
from lakana_core.projects.deploys import DEPLOY_QUEUE_LOCK_KEY, DeployService
from lakana_core.projects.diff import DeployDiff

_JOIN_SECONDS = 60.0


def _service(session: Session) -> DeployService:
    """Build a deploy service that allows everything.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    DeployService
        The service.
    """
    return DeployService(session, AllowAllAuthorization())


def _submit(session: Session, p: PrincipalRef, kind: DeployKind = DeployKind.DEPLOY) -> DeployRun:
    """Queue a deploy (or a rollback to the previous version) of `analytics` in `dev`.

    Parameters
    ----------
    session
        Database session.
    p
        The deployer.
    kind
        What to queue.

    Returns
    -------
    DeployRun
        The queued run.
    """
    if kind is DeployKind.ROLLBACK:
        view = _service(session).submit(
            p, "dev", repository="analytics", kind=kind, to_previous=True
        )
    else:
        view = _service(session).submit(p, "dev", repository="analytics", kind=kind)
    run = session.get(DeployRun, view.id)
    assert run is not None
    return run


def _record(session: Session, p: PrincipalRef, run: DeployRun, sha: str) -> JobVersion:
    """Claim and record a queued deploy, observing the pointer as the worker does.

    Parameters
    ----------
    session
        Database session.
    p
        The deployer.
    run
        The queued deploy.
    sha
        The commit it resolved to.

    Returns
    -------
    JobVersion
        The version it made current.
    """
    service = _service(session)
    run = _claimed(session, run)
    run.resolved_sha = sha
    current = service.current_job_version(run.environment_id, run.repository_id)
    return service.record_deploy(
        p,
        run,
        manifest=_manifest(),
        code_uri="u",
        code_sha256="0" * 64,
        diff=DeployDiff(),
        expected_job_version_id=current.id if current is not None else None,
        worker_id="w1",
    )


def _pointer(session: Session) -> EnvironmentDeployment | None:
    """Read the one deployment pointer, if any.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    EnvironmentDeployment or None
        The pointer.
    """
    session.expire_all()
    return session.scalars(select(EnvironmentDeployment)).one_or_none()


@pytest.fixture
def deployed(session: Session, tmp_path: Path, principal: PrincipalRef) -> JobVersion:
    """`analytics` deployed to `dev`, then a second version, so a rollback has a target.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        Storage root.
    principal
        The deployer.

    Returns
    -------
    JobVersion
        The current version.
    """
    _prepared_catalog(session, tmp_path, AllowAllAuthorization())
    repo = _repo(session)
    _deployed(session, principal, repo, "a" * 40)
    return _deployed(session, principal, repo, "b" * 40)


@pytest.mark.parametrize(
    ("kind", "status", "word"),
    [
        (DeployKind.DEPLOY, DeployStatus.QUEUED, "Deploy"),
        (DeployKind.DEPLOY, DeployStatus.RUNNING, "Deploy"),
        (DeployKind.ROLLBACK, DeployStatus.QUEUED, "Rollback"),
        (DeployKind.ROLLBACK, DeployStatus.RUNNING, "Rollback"),
    ],
)
def test_an_undeploy_is_refused_while_a_deploy_or_rollback_is_queued_or_running(
    session: Session,
    deployed: JobVersion,
    principal: PrincipalRef,
    kind: DeployKind,
    status: DeployStatus,
    word: str,
) -> None:
    """409 naming the deploy or rollback and its state; nothing changes until it ends."""
    run = _submit(session, principal, kind)
    if status is DeployStatus.RUNNING:
        _claimed(session, run)
    with pytest.raises(ConflictError) as refused:
        _service(session).undeploy(principal, "dev", "analytics")
    assert str(refused.value) == (
        f"{word} {run.id} of 'analytics' is {status.value} in 'dev'; wait for it to finish "
        "before undeploying."
    )
    pointer = _pointer(session)
    assert pointer is not None
    assert pointer.current_job_version_id == deployed.id
    run.status = DeployStatus.FAILED
    session.commit()
    _service(session).undeploy(principal, "dev", "analytics")
    assert _pointer(session) is None


def test_a_deploy_queued_before_the_undeploy_keeps_its_place_and_deploys(
    session: Session, deployed: JobVersion, principal: PrincipalRef
) -> None:
    """The refused undeploy changes nothing, so the deploy's swap finds what its worker saw."""
    run = _submit(session, principal)
    with pytest.raises(ConflictError, match=str(run.id)):
        _service(session).undeploy(principal, "dev", "analytics")
    version = _record(session, principal, run, "c" * 40)
    pointer = _pointer(session)
    assert pointer is not None
    assert pointer.current_job_version_id == version.id != deployed.id


def test_a_dry_run_or_another_repositorys_deploy_does_not_hold_an_undeploy(
    session: Session, deployed: JobVersion, principal: PrincipalRef
) -> None:
    """A dry run never moves the pointer; another repository's deploy is not this one's."""
    _submit(session, principal, DeployKind.DRY_RUN)
    _repo(session, "other")
    _service(session).submit(principal, "dev", repository="other", kind=DeployKind.DEPLOY)
    _service(session).undeploy(principal, "dev", "analytics")
    assert _pointer(session) is None


def test_a_deploy_asked_for_after_an_undeploy_deploys_again(
    session: Session, deployed: JobVersion, principal: PrincipalRef
) -> None:
    """The swap with no pointer goes ahead: the deploy was asked for after the undeploy."""
    _service(session).undeploy(principal, "dev", "analytics")
    version = _record(session, principal, _submit(session, principal), "c" * 40)
    pointer = _pointer(session)
    assert pointer is not None
    assert (pointer.current_job_version_id, pointer.run_as_principal_id) == (version.id, None)


def test_a_rollback_asked_for_after_an_undeploy_deploys_its_target(
    session: Session, deployed: JobVersion, principal: PrincipalRef
) -> None:
    """A rollback reads the pointer when it runs, so after an undeploy it swaps with none."""
    previous = session.scalars(select(JobVersion).where(JobVersion.commit_sha == "a" * 40)).one()
    _service(session).undeploy(principal, "dev", "analytics")
    rollback = _service(session).submit(
        principal, "dev", repository="analytics", kind=DeployKind.ROLLBACK, to_sha="a" * 40
    )
    claimed = _claimed(session, session.get(DeployRun, rollback.id))
    assert _service(session).execute_rollback(principal, claimed, worker_id="w1").id == previous.id
    pointer = _pointer(session)
    assert pointer is not None
    assert pointer.current_job_version_id == previous.id


def test_an_undeploy_with_the_version_the_caller_saw_goes_ahead(
    session: Session, deployed: JobVersion, principal: PrincipalRef
) -> None:
    """The expected version is optional; when it matches, the undeploy is the plain one."""
    _service(session).undeploy(
        principal, "dev", "analytics", expected_project_version_id=deployed.id
    )
    assert _pointer(session) is None


def test_an_undeploy_of_a_deployment_that_changed_since_it_was_read_is_refused(
    session: Session, deployed: JobVersion, principal: PrincipalRef
) -> None:
    """Another version than the caller saw: 409, and the deployment stays."""
    seen = uuid.uuid4()
    with pytest.raises(ConflictError) as refused:
        _service(session).undeploy(principal, "dev", "analytics", expected_project_version_id=seen)
    assert str(refused.value) == (
        f"The deployment of 'analytics' in 'dev' changed since you read it: it is no longer "
        f"project version {seen}. Look at it again before undeploying."
    )
    assert _pointer(session) is not None


# --- Two sessions ------------------------------------------------------------------------------


def _in_thread(work: Callable[[], object]) -> tuple[threading.Thread, dict[str, object]]:
    """Start work in a thread and keep its result or its exception.

    Parameters
    ----------
    work
        What to run.

    Returns
    -------
    tuple
        The started thread and the dict that receives ``result`` or ``error``.
    """
    outcome: dict[str, object] = {}

    def run() -> None:
        """Run the work and keep what it gave."""
        try:
            outcome["result"] = work()
        except Exception as exc:
            outcome["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, outcome


class _HeldCommit:
    """Holds a session inside its commit, after its flush and before the database commits.

    Parameters
    ----------
    session
        The session to hold, once.
    """

    def __init__(self, session: Session) -> None:
        self.flushed = threading.Event()
        self.release = threading.Event()
        event.listen(session, "after_flush", self._hold, once=True)

    def _hold(self, *_: object) -> None:
        """Say the flush happened and wait for the release."""
        self.flushed.set()
        self.release.wait(_JOIN_SECONDS)


class InFlight:
    """What each thread is executing on an engine right now, and on which Postgres backend.

    A statement is in flight from `before_cursor_execute` until `after_cursor_execute` (or
    `handle_error`) in the same thread: a thread whose write is in flight while another session
    holds the lock it needs is waiting inside the database, not merely slow.

    Parameters
    ----------
    engine
        The engine to watch; `close` stops watching.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self.database = engine.url.database or ""
        self._lock = threading.Lock()
        self._statements: dict[int, tuple[str, int | None]] = {}
        self._listeners: list[tuple[str, Callable[..., None]]] = [
            ("checkout", self._checkout),
            ("before_cursor_execute", self._before),
            ("after_cursor_execute", self._after),
            ("handle_error", self._after),
        ]
        for name, listener in self._listeners:
            event.listen(engine, name, listener)

    def close(self) -> None:
        """Stop watching the engine."""
        for name, listener in self._listeners:
            event.remove(self._engine, name, listener)

    def _checkout(
        self, dbapi_connection: DBAPIConnection, record: ConnectionPoolEntry, *_: object
    ) -> None:
        """Note the Postgres backend of a connection once.

        Parameters
        ----------
        dbapi_connection
            The driver's connection.
        record
            Its pool record, whose `info` keeps the backend's pid.
        """
        if self._engine.dialect.name != "postgresql" or "pid" in record.info:
            return
        cursor = dbapi_connection.cursor()
        cursor.execute("SELECT pg_backend_pid()")
        row = cursor.fetchone()
        assert row is not None
        record.info["pid"] = row[0]
        cursor.close()

    def _before(self, connection: Connection, _cursor: object, statement: str, *_: object) -> None:
        """Note the statement this thread starts.

        Parameters
        ----------
        connection
            The SQLAlchemy connection, whose `info` may hold the backend's pid.
        statement
            The statement.
        """
        with self._lock:
            self._statements[threading.get_ident()] = (statement, connection.info.get("pid"))

    def _after(self, *_: object) -> None:
        """Note that this thread's statement ended."""
        with self._lock:
            self._statements.pop(threading.get_ident(), None)

    def of(self, thread: threading.Thread) -> tuple[str, int | None] | None:
        """Return the statement a thread is executing, with its backend's pid, if any.

        Parameters
        ----------
        thread
            The thread.

        Returns
        -------
        tuple or None
            The statement and the pid (`None` off Postgres), or `None` when nothing is in flight.
        """
        with self._lock:
            return None if thread.ident is None else self._statements.get(thread.ident)


Waiter = Callable[[threading.Thread, InFlight], None]
"""Returns once a thread waits inside the database for the other session's lock, else fails."""


def _set_up(engine: Engine, tmp_path: Path) -> tuple[PrincipalRef, uuid.UUID]:
    """Deploy `analytics` to `dev` on a fresh schema.

    Parameters
    ----------
    engine
        The engine.
    tmp_path
        Storage root.

    Returns
    -------
    tuple
        The deployer and the current version's id.
    """
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    p = PrincipalRef(id=uuid.uuid4(), name="admin", type=PrincipalType.USER)
    with Session(engine) as setup:
        setup.add(Principal(id=p.id, type=p.type, name=p.name, is_active=True))
        setup.commit()
        _prepared_catalog(setup, tmp_path, AllowAllAuthorization())
        return p, _deployed(setup, p, _repo(setup), "a" * 40).id


def a_submit_first_refuses_the_undeploy(
    engine: Engine, tmp_path: Path, wait_until_blocked: Waiter
) -> None:
    """Check that a submit still inside its commit makes an undeploy wait, then refuse.

    Parameters
    ----------
    engine
        The engine both sessions use.
    tmp_path
        Storage root.
    wait_until_blocked
        Returns once the undeploy's thread waits for the submit's lock.
    """
    p, version_id = _set_up(engine, tmp_path)
    in_flight = InFlight(engine)
    with Session(engine) as submitting, Session(engine) as undeploying:
        held = _HeldCommit(submitting)
        submit, submitted = _in_thread(lambda: _submit(submitting, p).id)
        assert held.flushed.wait(_JOIN_SECONDS)
        undeploy, undeployed = _in_thread(
            lambda: _service(undeploying).undeploy(p, "dev", "analytics")
        )
        try:
            wait_until_blocked(undeploy, in_flight)
        finally:
            held.release.set()
            submit.join(_JOIN_SECONDS)
            undeploy.join(_JOIN_SECONDS)
            in_flight.close()
        assert "error" not in submitted
        error = undeployed.get("error")
        assert isinstance(error, ConflictError), undeployed
        assert str(submitted["result"]) in str(error)
    with Session(engine) as check:
        pointer = _pointer(check)
        assert pointer is not None
        assert pointer.current_job_version_id == version_id


def an_undeploy_first_lets_the_submit_deploy_again(
    engine: Engine, tmp_path: Path, wait_until_blocked: Waiter
) -> None:
    """Check that an undeploy inside its commit makes a submit wait, and the deploy goes ahead.

    Parameters
    ----------
    engine
        The engine both sessions use.
    tmp_path
        Storage root.
    wait_until_blocked
        Returns once the submit's thread waits for the undeploy's lock.
    """
    p, _ = _set_up(engine, tmp_path)
    in_flight = InFlight(engine)
    with Session(engine) as undeploying, Session(engine) as submitting:
        held = _HeldCommit(undeploying)
        undeploy, undeployed = _in_thread(
            lambda: _service(undeploying).undeploy(p, "dev", "analytics")
        )
        assert held.flushed.wait(_JOIN_SECONDS)
        submit, submitted = _in_thread(lambda: _submit(submitting, p).id)
        try:
            wait_until_blocked(submit, in_flight)
        finally:
            held.release.set()
            undeploy.join(_JOIN_SECONDS)
            submit.join(_JOIN_SECONDS)
            in_flight.close()
        assert undeployed == {"result": None}
        assert "error" not in submitted
        run_id = submitted["result"]
    with Session(engine) as worker:
        run = worker.get_one(DeployRun, run_id)
        assert _pointer(worker) is None
        version = _record(worker, p, run, "b" * 40)
        pointer = _pointer(worker)
        assert pointer is not None
        assert pointer.current_job_version_id == version.id


@pytest.fixture
def sqlite_file(tmp_path: Path) -> Iterator[Engine]:
    """Open an engine on a SQLite file, so two sessions have two connections.

    Parameters
    ----------
    tmp_path
        Holds the file.

    Yields
    ------
    Engine
        The engine.
    """
    engine = create_engine(DatabaseConfig(url=f"sqlite:///{tmp_path / 'lakana.db'}"))
    yield engine
    engine.dispose()


def _waiting_for_the_writer(thread: threading.Thread, in_flight: InFlight) -> None:
    """Return once the thread waits for SQLite's writer lock, which the other session holds.

    Observed, not timed: the thread's write statement is in flight (SQLite's busy timeout is
    retrying it inside `sqlite3`), and at that moment a probe connection that does not wait is
    refused the writer lock (`BEGIN IMMEDIATE`: "database is locked").

    Parameters
    ----------
    thread
        The waiting thread.
    in_flight
        What each thread executes.

    Raises
    ------
    AssertionError
        If the thread ended, or was not seen waiting within 30 s.
    """
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline and thread.is_alive():
        current = in_flight.of(thread)
        if current is not None and current[0].lstrip().upper().startswith(("DELETE", "INSERT")):
            database = in_flight.database
            probe = sqlite3.connect(database, timeout=0)
            try:
                probe.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as refused:
                if "locked" in str(refused) and in_flight.of(thread) == current:
                    return
            else:
                probe.rollback()
            finally:
                probe.close()
        time.sleep(0.05)
    raise AssertionError("the second session never waited for SQLite's writer lock")


def test_on_sqlite_a_submit_first_refuses_the_undeploy(sqlite_file: Engine, tmp_path: Path) -> None:
    """SQLite's one writer orders them; the undeploy then sees the queued deploy."""
    a_submit_first_refuses_the_undeploy(sqlite_file, tmp_path / "store", _waiting_for_the_writer)


def test_on_sqlite_an_undeploy_first_lets_the_submit_deploy_again(
    sqlite_file: Engine, tmp_path: Path
) -> None:
    """The submit waits for the undeploy's commit, then its deploy makes a new pointer."""
    an_undeploy_first_lets_the_submit_deploy_again(
        sqlite_file, tmp_path / "store", _waiting_for_the_writer
    )


def _meanwhile(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, change: Callable[[Session], object]
) -> None:
    """Make another session change the pointer right after the undeploy has read it.

    Parameters
    ----------
    engine
        The engine of both sessions.
    monkeypatch
        Wraps `DeployService._deployment_row` for its first call.
    change
        What the other session does before it commits.
    """
    real = DeployService._deployment_row

    def read_then_change(
        self: DeployService, env: Environment, repo: Repository
    ) -> EnvironmentDeployment:
        row = real(self, env, repo)
        monkeypatch.undo()
        with Session(engine) as other:
            change(other)
            other.commit()
        return row

    monkeypatch.setattr(DeployService, "_deployment_row", read_then_change)


def a_pointer_moved_meanwhile_is_not_deleted(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Check that a deploy landing between the undeploy's read and its delete stays.

    Parameters
    ----------
    engine
        The engine of both sessions.
    tmp_path
        Storage root.
    monkeypatch
        Installs the change.
    """
    p, version_id = _set_up(engine, tmp_path)
    with Session(engine) as setup:
        newer = _deployed(setup, p, setup.scalars(select(Repository)).one(), "b" * 40).id
        setup.execute(update(EnvironmentDeployment).values(current_job_version_id=version_id))
        setup.commit()
    _meanwhile(
        engine,
        monkeypatch,
        lambda other: other.execute(
            update(EnvironmentDeployment).values(current_job_version_id=newer)
        ),
    )
    with Session(engine) as session:
        with pytest.raises(ConflictError) as refused:
            _service(session).undeploy(p, "dev", "analytics")
        assert str(refused.value) == (
            "The deployment of 'analytics' in 'dev' changed meanwhile; look at it again and retry."
        )
        pointer = _pointer(session)
        assert pointer is not None
        assert pointer.current_job_version_id == newer


def a_pointer_removed_meanwhile_is_not_found(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Check that an undeploy landing between another's read and its delete leaves a 404.

    Parameters
    ----------
    engine
        The engine of both sessions.
    tmp_path
        Storage root.
    monkeypatch
        Installs the change.
    """
    p, _ = _set_up(engine, tmp_path)
    _meanwhile(
        engine,
        monkeypatch,
        lambda other: other.query(EnvironmentDeployment).delete(synchronize_session=False),
    )
    with Session(engine) as session, pytest.raises(NotFoundError, match="not deployed"):
        _service(session).undeploy(p, "dev", "analytics")


def test_on_sqlite_a_pointer_moved_meanwhile_is_not_deleted(
    sqlite_file: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """409, and the newer deployment stays."""
    a_pointer_moved_meanwhile_is_not_deleted(sqlite_file, tmp_path / "store", monkeypatch)


def test_on_sqlite_a_pointer_removed_meanwhile_is_not_found(
    sqlite_file: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """404."""
    a_pointer_removed_meanwhile_is_not_found(sqlite_file, tmp_path / "store", monkeypatch)


def test_the_deploy_queue_lock_is_a_two_key_postgres_advisory_lock() -> None:
    """On PostgreSQL the lock is `pg_advisory_xact_lock(<key>::integer, <32 bits>::integer)`.

    Its behaviour under contention is pinned by the Postgres module; here only the statement and
    its keys are checked, on a stub session.
    """
    statements: list[str] = []

    class StubSession:
        """Reports a PostgreSQL bind and records what it is asked to run."""

        def get_bind(self) -> SimpleNamespace:
            """Return a bind that looks like PostgreSQL.

            Returns
            -------
            SimpleNamespace
                An object with ``dialect.name``.
            """
            return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

        def execute(self, statement: ClauseElement) -> None:
            """Record the compiled statement.

            Parameters
            ----------
            statement
                The statement.
            """
            compiled = statement.compile(
                dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
            )
            statements.append(str(compiled))

    environment_id, repository_id = uuid.UUID(int=7), uuid.UUID(int=9)
    service = DeployService(StubSession(), AllowAllAuthorization())  # ty: ignore[invalid-argument-type]
    service._lock_deploy_queue(environment_id, repository_id)
    digest = hashlib.blake2b(environment_id.bytes + repository_id.bytes, digest_size=4).digest()
    second = int.from_bytes(digest, "big", signed=True)
    assert statements == [
        f"SELECT pg_advisory_xact_lock(CAST({DEPLOY_QUEUE_LOCK_KEY} AS INTEGER), "
        f"CAST({second} AS INTEGER)) AS pg_advisory_xact_lock_1"
    ]
