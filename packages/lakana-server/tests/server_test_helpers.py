"""Shared helpers for the lakana-server test suite."""

import hashlib
import json
import tempfile
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from fastapi import FastAPI
from fastapi.testclient import TestClient
from minio import Minio
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy

from lakana_core.auth import AllowAllAuthorization
from lakana_core.identity.sessions import SessionService, utc_now
from lakana_core.models import (
    BrowserSession,
    Environment,
    EnvironmentDeployment,
    JobVersion,
    Principal,
    QueryRun,
    Repository,
    RepositoryKind,
)
from lakana_core.projects.source_map import ProjectFileRecord, write_source_map
from lakana_server.browser_session import CSRF_HEADER, cookie_name
from tests.conftest import AUTH_HEADER

_MINIMAL_SCHEMA = {
    "type": "struct",
    "schema-id": 0,
    "fields": [{"id": 1, "name": "id", "type": "long", "required": True}],
}
"""A minimal Iceberg schema for tables whose own schema is never under test."""

STORAGE_PARENT = Path(tempfile.gettempdir())
"""Parent directory `create_environment` builds a storage root under.

Only used as-is when this module is imported outside pytest. Under pytest,
`tests.conftest`'s autouse fixture replaces this with the running test's own
`tmp_path` (via `monkeypatch`), so `create_environment` never writes to a
path another test run could also be using.
"""


def create_environment(client: TestClient, name: str = "dev", root: str | None = None) -> None:
    """Create an environment via the platform API, as a bound target for a grant or policy.

    `CatalogService.create_environment` also creates the environment's
    `bronze`/`silver`/`gold` top-level namespaces, so a test only needs
    `create_table` on top of this when it also needs a real table for a
    `TABLE`-kind grant, row filter, or column mask to resolve against
    (these raise `NotFoundError` for a target that
    doesn't exist).

    Parameters
    ----------
    client
        The test client.
    name
        The environment's name.
    root
        The environment's storage root URI. Defaults to a location under
        `STORAGE_PARENT`, unique per call so a leftover directory from an
        earlier or concurrent run can't make one test's environment silently
        pick up another's files.

    Raises
    ------
    AssertionError
        If the environment creation request did not succeed — a silent
        404/409 here would otherwise surface much later, and confusingly,
        as a grant/policy `NotFoundError` in the calling test.
    """
    storage_root = (
        root if root is not None else f"file://{STORAGE_PARENT}/{name}-{uuid.uuid4().hex}"
    )
    response = client.post(
        "/api/v1/environments",
        headers=AUTH_HEADER,
        json={"name": name, "storage": {"scheme": "file", "root": storage_root, "options": {}}},
    )
    assert response.status_code == 201, response.text


def create_namespace(client: TestClient, environment: str, path: str) -> None:
    """Create a namespace via the platform API, as a bound target for a NAMESPACE-kind grant.

    `CatalogService.create_environment` only scaffolds the top-level
    `bronze`/`silver`/`gold` namespaces, so a test granting on a nested path
    such as `gold.sales` needs this first — `GrantService.grant` now raises
    `NotFoundError` for a NAMESPACE-kind grant whose namespace doesn't exist
    .

    Parameters
    ----------
    client
        The test client.
    environment
        The environment the namespace lives in. Must already exist (see
        `create_environment`).
    path
        The dot-joined namespace path, such as `gold.sales`.

    Raises
    ------
    AssertionError
        If the namespace creation request did not succeed.
    """
    response = client.post(
        f"/api/v1/environments/{environment}/namespaces",
        headers=AUTH_HEADER,
        json={"path": path},
    )
    assert response.status_code == 201, response.text


def create_table(client: TestClient, environment: str, namespace: str, table: str) -> None:
    """Create a real Iceberg table via the REST API, as a bound target for a grant or policy.

    Parameters
    ----------
    client
        The test client.
    environment
        The environment the table lives in. Must already exist (see
        `create_environment`), with `namespace` among its namespaces.
    namespace
        The (single-component) namespace to create the table in.
    table
        The table's name.

    Raises
    ------
    AssertionError
        If the table creation request did not succeed.
    """
    response = client.post(
        f"/v1/{environment}/namespaces/{namespace}/tables",
        headers=AUTH_HEADER,
        json={"name": table, "schema": _MINIMAL_SCHEMA, "properties": {}},
    )
    assert response.status_code == 200, response.text


def create_table_with_fields(
    client: TestClient, environment: str, namespace: str, table: str, fields: Mapping[int, str]
) -> None:
    """Create an Iceberg table of string columns through the REST API.

    Parameters
    ----------
    client
        The test client.
    environment
        The environment; it must exist with `namespace` among its namespaces.
    namespace
        The (single-component) namespace.
    table
        The table's name.
    fields
        Each column's field id to its name; every column is an optional string.
    """
    schema = {
        "type": "struct",
        "schema-id": 0,
        "fields": [
            {"id": field_id, "name": name, "type": "string", "required": False}
            for field_id, name in fields.items()
        ],
    }
    response = client.post(
        f"/v1/{environment}/namespaces/{namespace}/tables",
        headers=AUTH_HEADER,
        json={"name": table, "schema": schema, "properties": {}},
    )
    assert response.status_code == 200, response.text


def rename_fields(
    client: TestClient, table: str, before: Mapping[int, str], after: Mapping[int, str]
) -> None:
    """Rename columns of `dev.gold.<table>` through a REST catalog commit, keeping field ids.

    Parameters
    ----------
    client
        The test client.
    table
        The table's name, in namespace `gold` of environment `dev`.
    before
        The columns now, as `create_table_with_fields` made them (schema id 0).
    after
        The columns after: the same field ids under their new names.
    """
    schema = {
        "type": "struct",
        "schema-id": 1,
        "fields": [
            {"id": field_id, "name": name, "type": "string", "required": False}
            for field_id, name in after.items()
        ],
    }
    response = client.post(
        f"/v1/dev/namespaces/gold/tables/{table}",
        headers=AUTH_HEADER,
        json={
            "requirements": [{"type": "assert-current-schema-id", "current-schema-id": 0}],
            "updates": [
                {"action": "add-schema", "schema": schema, "last-column-id": max(before)},
                {"action": "set-current-schema", "schema-id": -1},
            ],
        },
    )
    assert response.status_code == 200, response.text


class SeaweedFsContainer(DockerContainer):
    """A SeaweedFS container exposing an S3-compatible gateway.

    Mirrors the interface of testcontainers' S3 container class
    (`get_client`, `get_config`, `.access_key`/`.secret_key`) so integration
    tests can swap one for the other without touching call sites. Unlike
    some other S3 servers, SeaweedFS's S3 gateway refuses anonymous requests
    outright once an identity config is supplied, so a single-identity config file is
    generated per instance and copied into the container via the Docker API
    (`with_copy_into_container`, applied after the container is created but
    before its main process starts) rather than a host bind mount — the
    config never touches the host filesystem as a named path at all, which
    also sidesteps host/daemon path-translation issues a bind mount can hit
    in sandboxed or remote-daemon Docker setups.
    """

    def __init__(
        self,
        image: str = "chrislusf/seaweedfs:4.47",
        port: int = 8333,
        access_key: str = "lakanatest",
        secret_key: str = "lakanatestsecret12345",
    ) -> None:
        """Configure a SeaweedFS container; does not start it.

        Parameters
        ----------
        image
            Docker image to use.
        port
            Port the S3 gateway listens on inside the container.
        access_key
            Access key for the one identity this container is configured
            with.
        secret_key
            Secret key for that identity.
        """
        super().__init__(image, _wait_strategy=LogMessageWaitStrategy("Starting S3 API Server"))
        self.port = port
        self.access_key = access_key
        self.secret_key = secret_key

        identity_config = json.dumps(
            {
                "identities": [
                    {
                        "name": "lakanatest",
                        "credentials": [{"accessKey": access_key, "secretKey": secret_key}],
                        "actions": ["Admin", "Read", "Write"],
                    }
                ]
            }
        )
        self.with_exposed_ports(self.port)
        self.with_copy_into_container(identity_config.encode(), "/etc/seaweedfs/s3.json")
        self.with_command(
            f"server -s3 -s3.port={self.port} -s3.config=/etc/seaweedfs/s3.json "
            "-dir=/data -ip.bind=0.0.0.0"
        )

    def get_client(self) -> Minio:
        """Return a `minio`-SDK client for the running container.

        Returns
        -------
        Minio
            A client for this container's one configured identity. The
            `minio` SDK speaks plain S3, so it works against SeaweedFS's
            gateway the same way it works against any S3 server.
        """
        config = self.get_config()
        return Minio(
            endpoint=config["endpoint"],
            access_key=config["access_key"],
            secret_key=config["secret_key"],
            secure=False,
        )

    def get_config(self) -> dict[str, str]:
        """Return this container's endpoint and credentials.

        Returns
        -------
        dict[str, str]
            `endpoint`, `access_key`, `secret_key`.
        """
        host_ip = self.get_container_host_ip()
        exposed_port = self.get_exposed_port(self.port)
        return {
            "endpoint": f"{host_ip}:{exposed_port}",
            "access_key": self.access_key,
            "secret_key": self.secret_key,
        }


def upload_artifact(
    client: TestClient,
    environment: str,
    name: str,
    files: Mapping[str, bytes],
    *,
    is_directory: bool = True,
    headers: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Store one artifact version through the three REST steps.

    Parameters
    ----------
    client
        The test client.
    environment
        An existing environment.
    name
        Dotted artifact name; its namespace must exist.
    files
        Path to contents.
    is_directory
        Whether the artifact is a directory.
    headers
        Request headers; the bootstrap admin's when `None`.

    Returns
    -------
    dict
        The commit response's JSON.

    Raises
    ------
    AssertionError
        If any step does not answer as expected.
    """
    auth = dict(headers or AUTH_HEADER)
    namespace, _, short = name.rpartition(".")
    base = f"/api/v1/environments/{environment}/namespaces/{namespace}/artifacts/{short}"
    by_hash = {hashlib.sha256(data).hexdigest(): data for data in files.values()}
    document = {
        "format": 1,
        "is_directory": is_directory,
        "files": [
            {"path": path, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            for path, data in sorted(files.items(), key=lambda item: item[0].encode("utf-8"))
        ],
    }
    begun = client.post(f"{base}/uploads", headers=auth, json=document)
    assert begun.status_code == 201, begun.text
    upload_id = begun.json()["upload_id"]
    for digest in begun.json()["missing"]:
        sent = client.put(
            f"{base}/uploads/{upload_id}/blobs/{digest}", headers=auth, content=by_hash[digest]
        )
        assert sent.status_code == 200, sent.text
    committed = client.post(f"{base}/uploads/{upload_id}/commit", headers=auth, json={})
    assert committed.status_code == 201, committed.text
    return committed.json()


def signed_in(
    client: TestClient,
    principal: str,
    *,
    touched_at: datetime | None = None,
    via: str = "passkey",
    user_agent: str = "pytest",
) -> tuple[str, str, str]:
    """Start a browser session for a principal directly through the session service.

    Uses the app's clock (`app.state.clock`) and commits, as a sign-in route would.

    Parameters
    ----------
    client
        A started client of the app.
    principal
        The name of the person signing in (a user).
    touched_at
        The time of the session's last passkey touch, for step-up; `None` for none.
    via
        How the session began.
    user_agent
        The browser's user agent, as it named itself.

    Returns
    -------
    tuple of (str, str, str)
        The cookie's name, its value and the session's CSRF token.
    """
    app = cast(FastAPI, client.app)
    clock = getattr(app.state, "clock", utc_now)
    with Session(app.state.engine) as session:
        principal_id = session.execute(
            select(Principal.id).where(Principal.name == principal)
        ).scalar_one()
        started = SessionService(session, AllowAllAuthorization(), clock=clock).start(
            principal_id,
            via=via,
            replacing=None,
            peer="testclient",
            user_agent=user_agent,
            touched=False,
        )
        if touched_at is not None:
            row = session.execute(
                select(BrowserSession).where(BrowserSession.key_id == started.key_id)
            ).scalar_one()
            row.last_touch_at = touched_at
        session.commit()
    return cookie_name(app.state.settings.public_origin()), started.cookie_value, started.csrf_token


def session_headers(name: str, value: str, csrf: str | None = None) -> dict[str, str]:
    """Build the headers of a request the interface sends with its session.

    Parameters
    ----------
    name
        The cookie's name.
    value
        The cookie's value.
    csrf
        The CSRF token, sent in `X-Lakana-CSRF`; `None` leaves the header out.

    Returns
    -------
    dict of str to str
        `Cookie`, and the CSRF header when given.
    """
    headers = {"Cookie": f"{name}={value}"}
    if csrf is not None:
        headers[CSRF_HEADER] = csrf
    return headers


def person(client: TestClient, name: str, role: str, env: str = "dev") -> dict[str, str]:
    """Create a service account with a role on an environment, and return its header.

    Parameters
    ----------
    client
        Test client for the API.
    name
        The principal's name.
    role
        The role granted on the environment.
    env
        The environment.

    Returns
    -------
    dict of str to str
        The principal's Authorization header.
    """
    client.post(
        "/api/v1/principals", headers=AUTH_HEADER, json={"name": name, "type": "SERVICE_ACCOUNT"}
    )
    token = client.post(
        f"/api/v1/principals/{name}/tokens", headers=AUTH_HEADER, json={"name": name}
    ).json()["token"]
    granted = client.post(
        "/api/v1/grants",
        headers=AUTH_HEADER,
        json={"principal": name, "role": role, "kind": "ENVIRONMENT", "environment": env},
    )
    assert granted.status_code == 201, granted.text
    return {"Authorization": f"Bearer {token}"}


def query_run_columns(client: TestClient, query_id: str, *names: str) -> dict[str, object]:
    """Read the named attributes of a query run straight from the app's database.

    Parameters
    ----------
    client
        Test client for the API.
    query_id
        The query's id.
    *names
        Attributes of `QueryRun`, such as ``"sql_text"`` or ``"preview_table_id"``.

    Returns
    -------
    dict of str to object
        Each name with its value.
    """
    with Session(cast(FastAPI, client.app).state.engine) as session:
        run = session.get(QueryRun, uuid.UUID(query_id))
        assert run is not None
        return {name: getattr(run, name) for name in names}


DEPLOYED_REPOSITORY = "analytics"
"""The repository `deploy_file` registers, on GitHub so the location has a link."""


def deploy_file(
    session: Session,
    environment: str,
    table: str,
    text: str,
    *,
    gate_tables: Sequence[str] = (),
    gate_raw: bool = False,
) -> None:
    """Make a project version whose one SQL asset defines `table` the current one, with its file.

    Registers the repository `DEPLOYED_REPOSITORY` on first use and stores a version whose
    manifest has the asset `assets/<namespace path>/<name>.sql`, at the commit ``"a" * 40`` for
    the repository's first version in the environment and a new commit for each later one. Then
    it points the environment's deployment at that version and writes the source map with
    `write_source_map`, as a deploy does (the `_deploy` pattern of
    `tests/api/platform/test_graph.py`).

    Parameters
    ----------
    session
        Database session; committed.
    environment
        The environment's name; it must exist.
    table
        The dotted table name the file defines.
    text
        The file's text.
    gate_tables
        Every table the file uses; stored sorted and without duplicates.
    gate_raw
        Whether the file reads a file path or uses a connection.
    """
    namespace, _, name = table.rpartition(".")
    record = ProjectFileRecord(
        path=f"assets/{namespace.replace('.', '/')}/{name}.sql",
        table=table,
        text=text,
        normalized_sql=text,
        gate_tables=tuple(sorted(set(gate_tables))),
        gate_raw=gate_raw,
    )
    env_id = session.execute(
        select(Environment.id).where(Environment.name == environment)
    ).scalar_one()
    repo = session.execute(
        select(Repository).where(Repository.name == DEPLOYED_REPOSITORY)
    ).scalar_one_or_none()
    if repo is None:
        repo = Repository(
            id=uuid.uuid4(),
            name=DEPLOYED_REPOSITORY,
            kind=RepositoryKind.HTTPS,
            url=f"https://github.com/acme/{DEPLOYED_REPOSITORY}.git",
        )
        session.add(repo)
        session.flush()
    earlier = session.scalar(
        select(func.count())
        .select_from(JobVersion)
        .where(JobVersion.repository_id == repo.id, JobVersion.environment_id == env_id)
    )
    version = JobVersion(
        id=uuid.uuid4(),
        repository_id=repo.id,
        environment_id=env_id,
        commit_sha=f"{earlier:040x}" if earlier else "a" * 40,
        manifest_json={
            "assets": [
                {
                    "table": record.table,
                    "path": record.path,
                    "normalized_sql": record.normalized_sql,
                    "kind": "sql",
                }
            ]
        },
        code_uri="u",
        code_sha256="0" * 64,
    )
    session.add(version)
    session.flush()
    pointer = session.get(EnvironmentDeployment, (env_id, repo.id))
    if pointer is None:
        session.add(
            EnvironmentDeployment(
                environment_id=env_id, repository_id=repo.id, current_job_version_id=version.id
            )
        )
    else:
        pointer.current_job_version_id = version.id
    session.flush()
    write_source_map(session, version, [record])
    session.commit()
