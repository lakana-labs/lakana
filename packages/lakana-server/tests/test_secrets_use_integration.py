"""The paths that USE a secret never show its value (#65).

`test_secrets_lifecycle_integration.py` proves that the secrets routes do not return a stored
value. This module exercises what the lifecycle test leaves out: an environment whose S3
storage can only be reached through a stored secret (the only decryption path), and what that
secret then flows through. Real Postgres and a real SeaweedFS (S3) behind the real app, over
real HTTP.

The secret holds the S3 key pair of a SeaweedFS started with credentials that are unique to
the run; the environment's own options hold the endpoint and nothing else, so a table can be
written and read only if the server decrypted the secret. Every response (body and headers) of
every call, the raw `secrets`, `environments`, `audit_events` and `query_runs` rows and the
captured log are searched for the key pair afterwards. A hit means a value leaked.

Covered: an environment that uses a secret, table create and append, the Iceberg
configuration, namespace, list-tables and load-table routes, a query run by the worker code
(its result stored through the secret and fetched through the route), every GET route with a
path parameter (read with the ids of a real run, task log, deploy and project version),
rotating a secret to a value the storage refuses (an error raised by storage after the secret
was resolved) and revoking a secret an environment still refers to. The log search includes
uvicorn's own loggers, and a 5xx answer fails the test unless the call is meant to fail.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import re
import uuid
from collections.abc import Generator
from dataclasses import dataclass, field

import httpx
import pyarrow as pa
import pyarrow.ipc
import pytest
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session
from testcontainers.community.postgres import PostgresContainer

from lakana_core.config import DatabaseConfig, SecretsConfig
from lakana_core.db import create_engine
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.log_setup import LEVEL_FOLLOWING_LOGGERS
from lakana_core.models import (
    Base,
    Environment,
    JobVersion,
    Principal,
    QueryRun,
    QueryRunStatus,
    Repository,
    Run,
    RunKind,
    RunStatus,
    Secret,
    TaskRun,
    TaskStatus,
)
from lakana_core.secrets.crypto import decrypt_secret_row
from lakana_core.secrets.keys import EnvKeyProvider
from lakana_server.app import create_app
from lakana_server.settings import Settings
from lakana_worker.pipeline import run_one
from tests.python_asset_helpers import serving
from tests.server_test_helpers import SeaweedFsContainer

pytestmark = pytest.mark.integration

_MASTER_KEY = base64.b64encode(b"k" * 32).decode()
_BOOTSTRAP = {"Authorization": "Bearer lakana_pat_m10secretuse_m10secretusesecret"}
_ENV = "dev"
_SECRET = "s3-creds"
_STORAGE_ROUTES = frozenset(
    {
        "/api/v1/environments/{env}/namespaces/{path}/tables",
        "/api/v1/environments/{env}/namespaces/{path}/tables/{name}",
        # Answers a bare 500 with wrong keys (s3fs's PermissionError is not mapped to a 503 as
        # the table routes' is); no value leaks. Mapping it is a separate change.
        "/api/v1/environments/{name}/orphaned-locations",
        "/v1/{prefix}/namespaces/{namespace}/tables/{table}",
    }
)
"""The route templates that read the environment's storage, and so may fail with the secret."""
_PARAMETER = re.compile(r"\{(\w+)\}")


@dataclass
class _Stack:
    """Services, a running server and everything a test has seen so far.

    Parameters
    ----------
    http
        A client for the server that records every response.
    db
        An engine on the database.
    endpoint
        The SeaweedFS S3 endpoint, with its scheme.
    access_key
        The store's access key (also the secret's first value).
    secret_key
        The store's secret key (also the secret's second value).
    openapi
        The server's OpenAPI document.
    bodies
        Body and headers of every response, as text.
    log
        Every log line, formatted.
    needles
        Every value that was ever stored in the secret.
    ids
        The ids of the real objects the GET sweep reads: a run, its task's asset, a deploy and a
        project version.
    """

    http: httpx.Client
    db: Engine
    endpoint: str
    access_key: str
    secret_key: str
    openapi: dict[str, dict[str, dict[str, object]]]
    bodies: list[str] = field(default_factory=list)
    log: list[str] = field(default_factory=list)
    needles: set[str] = field(default_factory=set)
    ids: dict[str, str] = field(default_factory=dict)

    def seen(self, response: httpx.Response) -> httpx.Response:
        """Record a response for the final search.

        Parameters
        ----------
        response
            The response; its body is read.

        Returns
        -------
        httpx.Response
            The same response.
        """
        response.read()
        self.bodies.append(f"{response.request.method} {response.request.url}")
        self.bodies.append(response.text)
        self.bodies.append(repr(sorted(response.headers.multi_items())))
        return response

    def call(
        self,
        method: str,
        path: str,
        *,
        json: object = None,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
        allow_5xx: bool = False,
    ) -> httpx.Response:
        """Send a request with the bootstrap token and record the answer.

        Parameters
        ----------
        method
            The HTTP method.
        path
            The path, with its query string.
        json
            A JSON body.
        content
            A raw body.
        headers
            More headers.
        allow_5xx
            The call is meant to fail inside the server (an error raised by storage); any other
            5xx is a bug and fails the test at once.

        Returns
        -------
        httpx.Response
            The answer.
        """
        sent = {**_BOOTSTRAP, **(headers or {})}
        response = self.seen(
            self.http.request(method, path, json=json, content=content, headers=sent)
        )
        assert allow_5xx or response.status_code < 500, (
            f"{method} {path} answered {response.status_code}: {response.text[:300]}"
        )
        return response

    def store_secret_values(self, access_key: str, secret_key: str, *, rotate: bool) -> None:
        """Create or rotate the environment's storage secret.

        Parameters
        ----------
        access_key
            The `s3.access-key-id` value.
        secret_key
            The `s3.secret-access-key` value.
        rotate
            Rotate the existing secret instead of creating it.
        """
        self.needles.update((access_key, secret_key))
        values = {"s3.access-key-id": access_key, "s3.secret-access-key": secret_key}
        base = f"/api/v1/environments/{_ENV}/secrets"
        if rotate:
            answer = self.call("PATCH", f"{base}/{_SECRET}", json={"values": values})
            assert answer.status_code == 200, answer.text
        else:
            answer = self.call("POST", base, json={"name": _SECRET, "values": values})
            assert answer.status_code == 201, answer.text

    def parameters(self) -> dict[str, str]:
        """Name a value for every path parameter the GET routes use.

        Returns
        -------
        dict of str to str
            Parameter name to a value: a real object's id where the stack made one, an id that
            does not exist for the rest.
        """
        return {
            "env": _ENV,
            "environment_name": _ENV,
            "prefix": _ENV,
            "path": "bronze",
            "namespace": "bronze",
            "table": "t",
            "sha256": "0" * 64,
            "version": "1",
            "job": "j",
            "connection_name": "c",
            **self.ids,
        }

    def sweep(self, *, allow_5xx: bool = False) -> dict[str, int]:
        """Send a GET to every route that has path parameters, with values this stack knows.

        Parameters
        ----------
        allow_5xx
            The storage is meant to fail, so a route may answer 5xx.

        Returns
        -------
        dict of str to int
            The status each route template answered.
        """
        values = self.parameters()
        statuses: dict[str, int] = {}
        for template, operations in self.openapi.items():
            if "get" not in operations or "{" not in template:
                continue
            path = _fill(template, values)
            if path is not None:
                statuses[template] = self.call("GET", path, allow_5xx=allow_5xx).status_code
        return statuses

    def assert_only_storage_routes_failed(self, statuses: dict[str, int]) -> None:
        """Require that a route that answered 5xx is one that reads the environment's storage.

        Parameters
        ----------
        statuses
            What `sweep` returned.

        Raises
        ------
        AssertionError
            If a route outside `_STORAGE_ROUTES` answered 5xx.
        """
        failed = {template for template, status in statuses.items() if status >= 500}
        assert failed <= _STORAGE_ROUTES, sorted(failed - _STORAGE_ROUTES)

    def raw_rows(self) -> list[bytes]:
        """Read what the database holds, as bytes to search.

        Returns
        -------
        list of bytes
            The ciphertext columns of every secret, and the text of the audit events, the
            environments' storage options and the queries' errors and texts.
        """
        blobs: list[bytes] = []
        with Session(self.db) as session:
            for row in session.execute(select(Secret)).scalars():
                blobs += [bytes(row.ciphertext), bytes(row.nonce), bytes(row.wrapped_data_key)]
            for statement in (
                "SELECT event_type, detail FROM audit_events",
                "SELECT storage_options, storage_secret_ref, storage_root FROM environments",
                "SELECT error_message, sql_text FROM query_runs",
            ):
                for result in session.execute(text(statement)).all():
                    blobs.append(json.dumps(list(result), default=str).encode())
        return blobs

    def assert_nothing_leaked(self, *, expect_log: bool = False) -> None:
        """Search everything seen so far for every value ever stored.

        Parameters
        ----------
        expect_log
            Require that something was logged (a failing storage call logs).

        Raises
        ------
        AssertionError
            If a value is in a response, a database row or the log, or the controls show that
            the search did not look at anything.
        """
        body_text = "\n".join(self.bodies)
        log_text = "\n".join(self.log)
        blobs = self.raw_rows()
        # Controls: the search saw real traffic, a real log and the real rows.
        assert _SECRET in body_text
        if expect_log:
            assert log_text, "the log capture is empty"
        assert any(b"SECRET_CREATED" in blob for blob in blobs)
        assert any(self.endpoint.encode() in blob for blob in blobs)
        assert _MASTER_KEY not in body_text and _MASTER_KEY not in log_text
        for value in sorted(self.needles):
            assert value not in body_text, f"{value} was in a response"
            assert value not in log_text, f"{value} was in a log line"
            assert all(value.encode() not in blob for blob in blobs), f"{value} was in a raw row"


def _fill(template: str, values: dict[str, str]) -> str | None:
    """Put values into a path template.

    Parameters
    ----------
    template
        An OpenAPI path.
    values
        Parameter name to value.

    Returns
    -------
    str or None
        The path, or `None` for a template with a parameter this module has no value for.
    """
    path = template
    for name in _PARAMETER.findall(template):
        if name == "name":
            # `{name}` is the environment under /environments/{name}, a table or artifact below
            # a namespace, and a principal, repository or index elsewhere.
            if re.search(r"/environments/\{name\}", template):
                replacement = _ENV
            elif "/namespaces/" in template:
                replacement = "t"
            else:
                replacement = "x"
        elif name in values:
            replacement = values[name]
        elif name.endswith("_id"):
            replacement = str(uuid.uuid4())
        else:
            return None
        path = path.replace("{" + name + "}", replacement, 1)
    return path


class _LogCollector(logging.Handler):
    """Collect formatted log lines.

    Parameters
    ----------
    lines
        Where the lines go.
    """

    def __init__(self, lines: list[str]) -> None:
        super().__init__(logging.DEBUG)
        self._lines = lines

    def emit(self, record: logging.LogRecord) -> None:
        """Keep one line, with the traceback of an exception.

        Parameters
        ----------
        record
            The record.
        """
        self._lines.append(self.format(record))


@pytest.fixture(scope="module")
def stack(tmp_path_factory: pytest.TempPathFactory) -> Generator[_Stack, None, None]:
    """Start Postgres, SeaweedFS with unique credentials and the server; set up the secret.

    Parameters
    ----------
    tmp_path_factory
        For the repository root.

    Yields
    ------
    _Stack
        The stack, with an environment `dev` that reaches its storage only through a secret.
    """
    access_key = f"AK{uuid.uuid4().hex}"
    secret_key = f"SK{uuid.uuid4().hex}"
    roots = tmp_path_factory.mktemp("repos")
    lines: list[str] = []
    collector = _LogCollector(lines)
    # The logging a debugging operator gets (`configure_logging("DEBUG")`): Lakana's own loggers
    # at DEBUG, the root logger, which every library inherits, at WARNING.
    root_logger = logging.getLogger()
    collector.setLevel(logging.DEBUG)
    previous = {name: logging.getLogger(name).level for name in LEVEL_FOLLOWING_LOGGERS}
    with (
        PostgresContainer("postgres:16-alpine", driver="pg8000") as pg,
        SeaweedFsContainer(access_key=access_key, secret_key=secret_key) as seaweedfs,
    ):
        seaweedfs.get_client().make_bucket("lakana")
        settings = Settings(
            database=DatabaseConfig(url=pg.get_connection_url()),
            bootstrap_pat_key_id="m10secretuse",
            bootstrap_pat_secret="m10secretusesecret",
            repo_roots=[str(roots)],
            secrets=SecretsConfig(master_key=_MASTER_KEY),
        )
        schema_engine = create_engine(settings.database)
        Base.metadata.create_all(schema_engine)
        schema_engine.dispose()
        endpoint = f"http://{seaweedfs.get_config()['endpoint']}"
        app = create_app(settings)
        db = create_engine(settings.database)
        root_logger.addHandler(collector)
        # uvicorn's loggers do not propagate to the root logger, and "Exception in ASGI
        # application" with its traceback is logged there: capture them as well.
        uvicorn_loggers = [logging.getLogger(n) for n in ("uvicorn", "uvicorn.access")]
        for uvicorn_logger in uvicorn_loggers:
            uvicorn_logger.addHandler(collector)
        for name in LEVEL_FOLLOWING_LOGGERS:
            logging.getLogger(name).setLevel(logging.DEBUG)
        try:
            with (
                serving(app) as url,
                httpx.Client(
                    base_url=url,
                    timeout=120.0,
                    # A refused append is answered before its body is read and the server then
                    # closes the connection: never reuse one.
                    limits=httpx.Limits(max_keepalive_connections=0),
                ) as http,
            ):
                state = _Stack(
                    http=http,
                    db=db,
                    endpoint=endpoint,
                    access_key=access_key,
                    secret_key=secret_key,
                    openapi=app.openapi()["paths"],
                    log=lines,
                )
                created = state.call(
                    "POST",
                    "/api/v1/environments",
                    json={
                        "name": _ENV,
                        "storage": {
                            "scheme": "s3",
                            "root": f"s3://lakana/{_ENV}",
                            "options": {"s3.endpoint": endpoint},
                        },
                    },
                )
                assert created.status_code == 201, created.text
                state.store_secret_values(access_key, secret_key, rotate=False)
                attached = state.call(
                    "PATCH",
                    f"/api/v1/environments/{_ENV}/storage",
                    json={"secret_ref": _SECRET},
                )
                assert attached.status_code == 200, attached.text
                _seed(state)
                yield state
        finally:
            root_logger.removeHandler(collector)
            for uvicorn_logger in uvicorn_loggers:
                uvicorn_logger.removeHandler(collector)
            for name, level in previous.items():
                logging.getLogger(name).setLevel(level)
            db.dispose()


def _seed(stack: _Stack) -> None:
    """Make the real objects the GET sweep reads: a deploy, a run with a task, a project version.

    Parameters
    ----------
    stack
        The stack; its `ids` are filled.
    """
    repository = stack.call(
        "POST",
        "/api/v1/repositories",
        json={"name": "crm", "kind": "https", "url": "https://example.invalid/crm.git"},
    )
    assert repository.status_code == 201, repository.text
    deploy = stack.call("POST", f"/api/v1/environments/{_ENV}/deploys", json={"repository": "crm"})
    assert deploy.status_code == 202, deploy.text
    stack.ids["deploy_id"] = deploy.json()["id"]
    with Session(stack.db) as session:
        env = session.scalars(select(Environment).where(Environment.name == _ENV)).one()
        owner = session.scalars(select(Principal)).first()
        repo = session.scalars(select(Repository).where(Repository.name == "crm")).one()
        assert owner is not None
        version = JobVersion(
            id=uuid.uuid4(),
            repository_id=repo.id,
            environment_id=env.id,
            commit_sha="a" * 40,
            manifest_json={
                "version": 3,
                "assets": [{"table": "gold.scores", "kind": "sql", "inputs": {}, "sql": ""}],
                "edges": [],
            },
            code_uri="file:///x",
            code_sha256="0" * 64,
        )
        session.add(version)
        session.flush()
        run = Run(
            id=uuid.uuid4(),
            environment_id=env.id,
            repository_id=repo.id,
            job_version_id=version.id,
            run_as_principal_id=owner.id,
            kind=RunKind.FULL,
            status=RunStatus.RUNNING,
            selection_json={},
        )
        session.add(run)
        session.flush()
        session.add(
            TaskRun(
                id=uuid.uuid4(),
                run_id=run.id,
                asset="gold.scores",
                status=TaskStatus.RUNNING,
                claim_count=1,
                attempt=1,
                lease_owner="w1",
                max_attempts=3,
                timeout_seconds=60,
                retry_delay_seconds=0,
                upstream_json=[],
            )
        )
        session.commit()
        stack.ids.update(
            run_id=str(run.id), project_version_id=str(version.id), asset="gold.scores"
        )


def _ipc(table: pa.Table) -> bytes:
    """Serialise a table as an Arrow IPC stream.

    Parameters
    ----------
    table
        The rows.

    Returns
    -------
    bytes
        The stream.
    """
    sink = io.BytesIO()
    with pyarrow.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue()


def _run_query(stack: _Stack, sql: str) -> tuple[str, QueryRunStatus]:
    """Submit a query and run it with the worker's code, in this process.

    Parameters
    ----------
    stack
        The stack.
    sql
        The query.

    Returns
    -------
    tuple of str and QueryRunStatus
        The query's id and how it ended.
    """
    submitted = stack.call("POST", f"/api/v1/environments/{_ENV}/queries", json={"sql": sql})
    assert submitted.status_code == 202, submitted.text
    query_id = submitted.json()["query_id"]
    stack.ids["query_id"] = query_id
    with Session(stack.db) as session:
        run_one(
            session,
            uuid.UUID(query_id),
            GrantAuthorization(session),
            EnvKeyProvider(_MASTER_KEY),
        )
        run = session.get(QueryRun, uuid.UUID(query_id))
        assert run is not None
        return query_id, run.status


def test_an_environment_that_uses_a_secret_shows_no_value(stack: _Stack) -> None:
    """Writes, reads, the Iceberg routes, a query and every parameterised GET stay clean."""
    # The environment itself holds the endpoint and a reference, never the keys.
    described = stack.call("GET", f"/api/v1/environments/{_ENV}")
    assert described.status_code == 200, described.text
    assert described.json()["storage_secret_ref"] == _SECRET

    table = stack.call(
        "POST",
        f"/api/v1/environments/{_ENV}/namespaces/bronze/tables",
        json={"name": "t", "schema": [{"name": "id", "type": "long", "nullable": False}]},
    )
    assert table.status_code in {200, 201}, table.text
    rows = pa.table(
        {"id": pa.array([1, 2, 3], pa.int64())},
        schema=pa.schema([pa.field("id", pa.int64(), nullable=False)]),
    )
    appended = stack.call(
        "POST",
        f"/api/v1/environments/{_ENV}/namespaces/bronze/tables/t/data",
        content=_ipc(rows),
        headers={"Content-Type": "application/vnd.apache.arrow.stream"},
    )
    assert appended.status_code == 200, appended.text

    # The Iceberg configuration and load-table responses (these carry the table's file io).
    config = stack.call("GET", f"/v1/config?warehouse={_ENV}")
    namespaces = stack.call("GET", f"/v1/{_ENV}/namespaces")
    listed = stack.call("GET", f"/v1/{_ENV}/namespaces/bronze/tables")
    loaded = stack.call("GET", f"/v1/{_ENV}/namespaces/bronze/tables/t")
    assert [config.status_code, namespaces.status_code, listed.status_code] == [200, 200, 200]
    assert loaded.status_code == 200, loaded.text
    assert "metadata-location" in loaded.json()

    query_id, status = _run_query(stack, "SELECT id FROM bronze.t ORDER BY id")
    assert status == QueryRunStatus.SUCCEEDED
    result = stack.call("GET", f"/api/v1/environments/{_ENV}/queries/{query_id}/result")
    assert result.status_code == 200, result.text
    with pyarrow.ipc.open_file(io.BytesIO(result.content)) as reader:
        assert reader.read_all().column("id").to_pylist() == [1, 2, 3]

    statuses = stack.sweep()
    # Real objects: these routes answered with a resource, not a 404.
    for route in (
        "/api/v1/environments/{name}",
        "/api/v1/environments/{env}/queries/{query_id}/result",
        "/api/v1/environments/{env}/runs/{run_id}",
        "/api/v1/environments/{env}/runs/{run_id}/tasks/{asset}/log",
        "/api/v1/environments/{env}/deploys/{deploy_id}",
        "/api/v1/environments/{env}/project-versions/{project_version_id}",
        "/v1/{prefix}/namespaces/{namespace}/tables/{table}",
        "/api/v1/environments/{env}/namespaces/{path}/tables/{name}",
    ):
        assert statuses.get(route) == 200, (route, statuses.get(route))
    assert sum(1 for status in statuses.values() if status == 200) >= 12, statuses

    # Control: the stored secret decrypts to the keys the store accepted.
    with Session(stack.db) as session:
        row = session.execute(select(Secret)).scalar_one()
        assert decrypt_secret_row(row, EnvKeyProvider(_MASTER_KEY), environment_name=_ENV) == {
            "s3.access-key-id": stack.access_key,
            "s3.secret-access-key": stack.secret_key,
        }
    stack.assert_nothing_leaked()


def test_an_error_raised_by_storage_after_the_secret_was_resolved_shows_no_value(
    stack: _Stack,
) -> None:
    """A secret the store refuses is decrypted, used and fails; no value is in the error."""
    wrong_access = f"WRONGAK{uuid.uuid4().hex}"
    wrong_secret = f"WRONGSK{uuid.uuid4().hex}"
    stack.store_secret_values(wrong_access, wrong_secret, rotate=True)

    refused = [
        stack.call(
            "POST",
            f"/api/v1/environments/{_ENV}/namespaces/bronze/tables/t/data",
            content=_ipc(pa.table({"id": pa.array([9], pa.int64())})),
            headers={"Content-Type": "application/vnd.apache.arrow.stream"},
            allow_5xx=True,
        ),
        stack.call("GET", f"/v1/{_ENV}/namespaces/bronze/tables/t", allow_5xx=True),
        stack.call("GET", f"/api/v1/environments/{_ENV}/orphaned-locations", allow_5xx=True),
    ]
    # Control: the storage refused the call (503, "could not be reached"), not a missing table,
    # so the failing path ran with the wrong keys.
    assert any(answer.status_code == 503 for answer in refused), [a.status_code for a in refused]
    _, status = _run_query(stack, "SELECT id FROM bronze.t")
    assert status == QueryRunStatus.FAILED
    stack.assert_only_storage_routes_failed(stack.sweep(allow_5xx=True))

    stack.assert_nothing_leaked(expect_log=True)

    stack.store_secret_values(stack.access_key, stack.secret_key, rotate=True)
    restored = stack.call("GET", f"/v1/{_ENV}/namespaces/bronze/tables/t")
    assert restored.status_code == 200, restored.text


def test_revoking_a_secret_an_environment_still_refers_to_shows_no_value(stack: _Stack) -> None:
    """The dangling reference fails closed, names the secret and nothing else."""
    revoked = stack.call("DELETE", f"/api/v1/environments/{_ENV}/secrets/{_SECRET}")
    assert revoked.status_code == 204, revoked.text

    answers = [
        stack.call("GET", f"/v1/{_ENV}/namespaces/bronze/tables/t", allow_5xx=True),
        stack.call(
            "POST",
            f"/api/v1/environments/{_ENV}/namespaces/bronze/tables/t/data",
            content=_ipc(pa.table({"id": pa.array([9], pa.int64())})),
            headers={"Content-Type": "application/vnd.apache.arrow.stream"},
            allow_5xx=True,
        ),
    ]
    assert all(answer.status_code >= 400 for answer in answers), [a.status_code for a in answers]
    _, status = _run_query(stack, "SELECT id FROM bronze.t")
    assert status == QueryRunStatus.FAILED
    stack.assert_only_storage_routes_failed(stack.sweep(allow_5xx=True))
    stack.call("GET", f"/api/v1/environments/{_ENV}/secrets")

    stack.assert_nothing_leaked()
