"""Shared pieces of the Python asset integration tests.

The services, a real HTTP server, a bound token.

Two modules use them: `test_bound_tokens_integration.py` (Postgres and
SeaweedFS, no package installation) and `test_python_assets_integration.py`
(the same plus the real `uv` and the real harness, which need the offline
wheel index of `scripts/build_wheelhouse.sh`).
"""

from __future__ import annotations

import base64
import contextlib
import threading
import time
import uuid
from collections.abc import Callable, Generator
from dataclasses import dataclass, replace
from pathlib import Path

import pyarrow as pa
import pytest
import uvicorn
from fastapi import FastAPI
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session
from testcontainers.community.postgres import PostgresContainer

import lakana
from lakana_core.config import DatabaseConfig, SecretsConfig
from lakana_core.db import create_engine, db_now
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.identity.task_tokens import TaskTokenService
from lakana_core.models import (
    AuditEvent,
    AuditEventType,
    Base,
    Environment,
    JobVersion,
    Principal,
    QueryRun,
    Repository,
    RepositoryKind,
    Run,
    RunKind,
    RunStatus,
    TaskAttempt,
    TaskRun,
    TaskStatus,
)
from lakana_core.storage.resolver import StorageResolver
from lakana_server.settings import Settings
from tests.integration_helpers import unique_name
from tests.server_test_helpers import SeaweedFsContainer

TOKEN = "lakana_pat_m5c2aintegration_m5c2aintegrationsecret"
"""The bootstrap admin's token in both modules."""

MASTER_KEY = base64.b64encode(b"k" * 32).decode()
"""The secrets master key of the test server and its worker."""

WORKER_APPLICATION = "lakana-test-worker"
"""`application_name` of the worker's database connections, to tell them from the server's."""

_START_SECONDS = 60
_STOP_SECONDS = 30


@dataclass(frozen=True)
class Services:
    """Running Postgres and SeaweedFS, and the settings pointing at them.

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

    def environment(self, admin: lakana.Client, name: str) -> None:
        """Create an environment with its own storage prefix.

        Parameters
        ----------
        admin
            An SDK client with the bootstrap token.
        name
            The environment's name.
        """
        admin.environments.create(name, storage={**self.storage, "root": f"s3://lakana/{name}"})

    def worker_engine(self) -> Engine:
        """Create the engine a worker under test uses.

        Returns
        -------
        Engine
            An engine on the same database whose connections carry
            `WORKER_APPLICATION`, so a test can find (and cut) exactly them.
            The caller disposes of it.
        """
        url = self.settings.database.url
        separator = "&" if "?" in url else "?"
        return create_engine(
            DatabaseConfig(url=f"{url}{separator}application_name={WORKER_APPLICATION}")
        )


@contextlib.contextmanager
def running_services(tmp_path_factory: pytest.TempPathFactory) -> Generator[Services, None, None]:
    """Start Postgres and SeaweedFS and create the schema.

    Parameters
    ----------
    tmp_path_factory
        For the repo root.

    Yields
    ------
    Services
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
            bootstrap_pat_key_id="m5c2aintegration",
            bootstrap_pat_secret="m5c2aintegrationsecret",
            repo_roots=[str(roots)],
            secrets=SecretsConfig(master_key=MASTER_KEY),
        )
        engine = create_engine(settings.database)
        Base.metadata.create_all(engine)
        engine.dispose()
        config = seaweedfs.get_config()
        yield Services(
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


@contextlib.contextmanager
def serving(app: FastAPI) -> Generator[str, None, None]:
    """Serve an app over real HTTP on a loopback port, in a thread.

    Parameters
    ----------
    app
        The application.

    Yields
    ------
    str
        The server's base URL.

    Raises
    ------
    AssertionError
        If the server does not start within a minute or does not stop within
        half a minute.
    """
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    thread = threading.Thread(target=server.run, name="lakana-test-server", daemon=True)
    thread.start()
    deadline = time.monotonic() + _START_SECONDS
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.05)
    if not server.started:
        server.should_exit = True
        raise AssertionError("the test server did not start")
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=_STOP_SECONDS)
    assert not thread.is_alive(), "the test server did not stop"


def cut_idle_connections(db: Engine, application: str = WORKER_APPLICATION) -> int:
    """End every idle database connection of one application, as an idle timeout would.

    Parameters
    ----------
    db
        Any engine on the database; its own connection is spared.
    application
        The `application_name` whose idle backends are terminated.

    Returns
    -------
    int
        How many backends were terminated.
    """
    with db.connect() as connection:
        return len(
            connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE application_name = :name AND state = 'idle' "
                    "AND pid <> pg_backend_pid()"
                ),
                {"name": application},
            ).all()
        )


@dataclass(frozen=True)
class BoundAttempt:
    """A running attempt of a Python task, and the token bound to it.

    Parameters
    ----------
    environment
        The environment's name.
    task_run_id
        The task run.
    run_id
        Its run.
    claim
        The attempt's claim counter.
    principal_id
        The run principal.
    principal
        Its name.
    token
        The bound task token.
    key_id
        The token's public key id.
    """

    environment: str
    task_run_id: uuid.UUID
    run_id: uuid.UUID
    claim: int
    principal_id: uuid.UUID
    principal: str
    token: str
    key_id: str


def bound_attempt(
    db: Engine,
    admin: lakana.Client,
    environment: str,
    *,
    asset: str,
    inputs: dict[str, str],
    lease_seconds: int | None = 300,
    output: str = "table",
    keep_versions: int | None = None,
    input_kinds: dict[str, str] | None = None,
    connections: list[str] | None = None,
) -> BoundAttempt:
    """Put a Python task into `running` for a service account, and mint its token.

    The rows are what a deploy, `lakana run` and a claim leave behind; they
    are written directly so that no package has to be installed. The token is
    minted by the code the worker uses, `TaskTokenService.mint_for_task`.

    Parameters
    ----------
    db
        The metadata database engine.
    admin
        An SDK client with the bootstrap token.
    environment
        An existing environment.
    asset
        The task's own table.
    inputs
        The asset's declared inputs, parameter → table.
    lease_seconds
        How long the lease lasts on the database's clock (PostgreSQL); `None` sets no lease
        expiry, for a test on SQLite.
    output
        What the asset produces, ``table`` or ``artifact``; the manifest records it.
    keep_versions
        An artifact asset's retention (`None` keeps every version).
    input_kinds
        The kind of each input, parameter → ``table`` or ``artifact``; every input is a table
        when omitted.
    connections
        The connections the asset declares; the manifest entry has no `connections` when
        omitted.

    Returns
    -------
    BoundAttempt
        The attempt. Its run principal holds `DATA_ENGINEER` on the
        environment, so whatever the token is refused, the binding refused it.
    """
    account = f"etl-{uuid.uuid4().hex[:10]}"
    admin.principals.create(account, "SERVICE_ACCOUNT")
    admin.grants.grant(
        principal=account, role="DATA_ENGINEER", kind="ENVIRONMENT", environment=environment
    )
    with Session(db) as session:
        env = session.execute(
            select(Environment).where(Environment.name == environment)
        ).scalar_one()
        principal = session.execute(select(Principal).where(Principal.name == account)).scalar_one()
        repository = Repository(
            id=uuid.uuid4(),
            name=f"r{uuid.uuid4().hex[:10]}",
            kind=RepositoryKind.HTTPS,
            url="https://example.invalid/analytics.git",
        )
        session.add(repository)
        session.flush()
        version = JobVersion(
            id=uuid.uuid4(),
            repository_id=repository.id,
            environment_id=env.id,
            commit_sha="a" * 40,
            manifest_json={
                "version": 3,
                "assets": [
                    {
                        "table": asset,
                        "kind": "python",
                        "path": f"assets/{asset.replace('.', '/')}.py",
                        "inputs": inputs,
                        "sql": "",
                        "output": output,
                        "keep_versions": keep_versions,
                        "input_kinds": input_kinds or dict.fromkeys(inputs, "table"),
                        **({} if connections is None else {"connections": connections}),
                    }
                ],
                "edges": [],
            },
            code_uri=f"{env.storage_root.rstrip('/')}/code/none.tar.gz",
            code_sha256="0" * 64,
        )
        session.add(version)
        session.flush()
        run = Run(
            id=uuid.uuid4(),
            environment_id=env.id,
            repository_id=repository.id,
            job_version_id=version.id,
            run_as_principal_id=principal.id,
            kind=RunKind.FULL,
            status=RunStatus.RUNNING,
            selection_json={},
        )
        session.add(run)
        session.flush()
        task = TaskRun(
            id=uuid.uuid4(),
            run_id=run.id,
            asset=asset,
            status=TaskStatus.RUNNING,
            claim_count=1,
            attempt=1,
            lease_owner="w1",
            max_attempts=3,
            timeout_seconds=60,
            retry_delay_seconds=0,
            upstream_json=[],
        )
        session.add(task)
        session.flush()
        session.add(
            TaskAttempt(
                id=uuid.uuid4(),
                task_run_id=task.id,
                attempt=1,
                worker="w1",
                started_at=db_now(session),
            )
        )
        if lease_seconds is not None:
            session.execute(
                text(
                    "UPDATE task_runs SET lease_expires_at = now() + make_interval(secs => :s) "
                    "WHERE id = :id"
                ),
                {"s": lease_seconds, "id": task.id},
            )
        session.commit()
        minted = TaskTokenService(session, GrantAuthorization(session)).mint_for_task(
            task.id, 1, ttl_seconds=300
        )
        return BoundAttempt(
            environment=environment,
            task_run_id=task.id,
            run_id=run.id,
            claim=1,
            principal_id=principal.id,
            principal=account,
            token=minted.token,
            key_id=minted.key_id,
        )


def reclaim(db: Engine, attempt: BoundAttempt) -> BoundAttempt:
    """Reclaim a task as the scheduler does: bump the claim, and mint the new attempt's token.

    The previous attempt's token is invalid from here on (the claim it carries is superseded).

    Parameters
    ----------
    db
        The metadata database engine.
    attempt
        The attempt to supersede.

    Returns
    -------
    BoundAttempt
        The same task run with the next claim and a token bound to it.
    """
    with Session(db) as session:
        claim = session.execute(
            text(
                "UPDATE task_runs SET claim_count = claim_count + 1 WHERE id = :id "
                "RETURNING claim_count"
            ),
            {"id": attempt.task_run_id},
        ).scalar_one()
        session.commit()
        minted = TaskTokenService(session, GrantAuthorization(session)).mint_for_task(
            attempt.task_run_id, claim, ttl_seconds=300
        )
    return replace(attempt, claim=claim, token=minted.token, key_id=minted.key_id)


IDS = pa.schema([pa.field("id", pa.int64())])
"""A one-column schema for the small tables the query tests create."""


def long_sql(tag: str = "n") -> str:
    """Build a query that runs about nine seconds by itself and uses almost no memory.

    Parameters
    ----------
    tag
        Names the result column, so the audit events of two such queries tell apart.

    Returns
    -------
    str
        The statement.
    """
    return f"SELECT sum(hash(a.range + b.range)) AS {tag} FROM range(40000) a, range(40000) b"


@dataclass(frozen=True)
class QueryStack:
    """The services, a running server, an admin client and an engine on the metadata database.

    Parameters
    ----------
    services
        Postgres and SeaweedFS.
    url
        The server's base URL.
    admin
        An SDK client with the bootstrap token.
    db
        An engine for the test's own reads, and for the worker thread.
    """

    services: Services
    url: str
    admin: lakana.Client
    db: Engine

    def client(self, token: str) -> lakana.Client:
        """Open another SDK client on the server.

        Parameters
        ----------
        token
            The bearer token.

        Returns
        -------
        lakana.Client
            The client; the caller closes it.
        """
        return lakana.Client(self.url, token, timeout=60.0)

    def environment(self) -> str:
        """Create an environment with `bronze.nums` (ids 1 to 3).

        Returns
        -------
        str
            The environment's name.
        """
        name = unique_name()
        self.services.environment(self.admin, name)
        self.admin.tables.create(name, "bronze.nums", schema=IDS)
        self.admin.tables.append(
            name, "bronze.nums", pa.table({"id": pa.array([1, 2, 3], pa.int64())})
        )
        return name

    def events(self, environment: str, kind: AuditEventType) -> list[AuditEvent]:
        """List the audit events of one type in one environment, oldest first.

        Parameters
        ----------
        environment
            The environment.
        kind
            The event type.

        Returns
        -------
        list of AuditEvent
            The events.
        """
        with Session(self.db) as session:
            rows = session.scalars(
                select(AuditEvent)
                .where(
                    AuditEvent.event_type == kind, AuditEvent.securable_environment == environment
                )
                .order_by(AuditEvent.occurred_at)
            ).all()
            session.expunge_all()
            return list(rows)

    def principal_id(self, name: str) -> uuid.UUID:
        """Look up a principal's id.

        Parameters
        ----------
        name
            The principal's name.

        Returns
        -------
        uuid.UUID
            Its id.
        """
        with Session(self.db) as session:
            return session.scalars(select(Principal.id).where(Principal.name == name)).one()

    def row(self, query_id: str) -> QueryRun:
        """Read a query run's row.

        Parameters
        ----------
        query_id
            The run's id.

        Returns
        -------
        QueryRun
            The row, detached.
        """
        with Session(self.db) as session:
            row = session.get(QueryRun, uuid.UUID(query_id))
            assert row is not None
            session.expunge(row)
            return row

    def storage_listing(self, environment: str) -> set[str]:
        """List every object under an environment's storage root.

        Parameters
        ----------
        environment
            The environment.

        Returns
        -------
        set of str
            Root-relative paths.
        """
        with Session(self.db) as session:
            resolved = StorageResolver(session).for_environment(environment)
            return set(resolved.storage.list(""))


@contextlib.contextmanager
def query_stack(
    tmp_path_factory: pytest.TempPathFactory,
    settings: Callable[[Settings], Settings] | None = None,
) -> Generator[QueryStack, None, None]:
    """Start Postgres, SeaweedFS and a real HTTP server; no worker runs until a test starts one.

    Parameters
    ----------
    tmp_path_factory
        For the repo root.
    settings
        Changes the settings the containers produced (a shorter lease, say) before the server
        starts; the worker thread of a test reads them from `QueryStack.services`.

    Yields
    ------
    QueryStack
        The stack.
    """
    from lakana_server.app import create_app

    with running_services(tmp_path_factory) as services:
        if settings is not None:
            services = replace(services, settings=settings(services.settings))
        db = create_engine(services.settings.database)
        try:
            with (
                serving(create_app(services.settings)) as url,
                lakana.Client(url, TOKEN, timeout=60.0) as admin,
            ):
                yield QueryStack(services=services, url=url, admin=admin, db=db)
        finally:
            db.dispose()


def until(read: Callable[[], str], wanted: set[str], seconds: float) -> str:
    """Poll until `read` returns one of `wanted`, at most `seconds`.

    Parameters
    ----------
    read
        Returns the current status.
    wanted
        The statuses that end the wait.
    seconds
        The bound.

    Returns
    -------
    str
        The status reached.

    Raises
    ------
    AssertionError
        If none was reached in time.
    """
    deadline = time.monotonic() + seconds
    status = read()
    while status not in wanted:
        assert time.monotonic() < deadline, f"still {status} after {seconds:g} s, wanted {wanted}"
        time.sleep(0.05)
        status = read()
    return status


def completions(stack: QueryStack, run_id: str) -> list[str]:
    """List the statuses of the `QUERY_COMPLETED` events of one run's SQL text.

    Parameters
    ----------
    stack
        The stack.
    run_id
        The run's id.

    Returns
    -------
    list of str
        One status per event, oldest first.
    """
    sql = stack.row(run_id).sql_text
    with Session(stack.db) as session:
        events = session.scalars(
            select(AuditEvent)
            .where(AuditEvent.event_type == AuditEventType.QUERY_COMPLETED)
            .order_by(AuditEvent.occurred_at, AuditEvent.id)
        )
        return [e.detail["status"] for e in events if e.detail.get("sql_text") == sql]
