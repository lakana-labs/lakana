"""A bound task token over the real HTTP stack: everything it must not be able to do.

`TestClient`, SQLite and the real `GrantAuthorization`. The run principal
holds `DATA_ENGINEER` on the environment, so every refusal below comes from
the token's binding, not from a missing grant.
"""

from __future__ import annotations

import io
import uuid
from dataclasses import dataclass

import pyarrow as pa
import pyarrow.ipc
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from lakana_core.auth import AllowAllAuthorization
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.identity.task_tokens import TaskTokenService
from lakana_core.models import (
    AuditEvent,
    AuditEventType,
    Environment,
    JobVersion,
    Namespace,
    Principal,
    QueryRun,
    QueryRunStatus,
    Repository,
    RepositoryKind,
    Run,
    RunKind,
    RunStatus,
    Table,
    TaskRun,
    TaskStatus,
    TaskToken,
    WriteMode,
)
from lakana_worker.pipeline import run_one
from tests.conftest import AUTH_HEADER
from tests.server_test_helpers import create_environment

_ARROW = "application/vnd.apache.arrow.stream"
_SCHEMA = [{"name": "id", "type": "long", "nullable": False}]


def _ipc(ids: list[int]) -> bytes:
    """Serialize an `id` column as an Arrow IPC stream.

    Parameters
    ----------
    ids
        The ids.

    Returns
    -------
    bytes
        The stream.
    """
    table = pa.table(
        {"id": pa.array(ids, pa.int64())},
        schema=pa.schema([pa.field("id", pa.int64(), nullable=False)]),
    )
    sink = io.BytesIO()
    with pyarrow.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue()


@dataclass
class Bound:
    """A running Python task attempt and its token.

    Parameters
    ----------
    client
        The test client.
    task_run_id
        The task run.
    headers
        `Authorization` with the bound token.
    """

    client: TestClient
    task_run_id: uuid.UUID
    headers: dict[str, str]

    def session(self) -> Session:
        """Open a session on the app's database.

        Returns
        -------
        Session
            A new session.
        """
        return Session(self.client.app.state.engine)

    def get(self, environment: str, dotted: str) -> int:
        """Describe a table with the bound token.

        Parameters
        ----------
        environment
            Environment name.
        dotted
            ``namespace.table``.

        Returns
        -------
        int
            The status code.
        """
        namespace, name = dotted.rsplit(".", 1)
        return self.client.get(
            f"/api/v1/environments/{environment}/namespaces/{namespace}/tables/{name}",
            headers=self.headers,
        ).status_code

    def write(self, method: str, dotted: str, query: str = "") -> int:
        """Send an Arrow write with the bound token.

        Parameters
        ----------
        method
            ``PUT`` (replace) or ``POST`` (append).
        dotted
            ``namespace.table``.
        query
            A query string, with its leading ``?``.

        Returns
        -------
        int
            The status code.
        """
        namespace, name = dotted.rsplit(".", 1)
        return self.client.request(
            method,
            f"/api/v1/environments/dev/namespaces/{namespace}/tables/{name}/data{query}",
            headers={**self.headers, "Content-Type": _ARROW},
            content=_ipc([7]),
        ).status_code

    def query(self, sql: str) -> tuple[int, QueryRunStatus | None, str | None]:
        """Submit a query with the bound token and execute it as the worker would.

        Parameters
        ----------
        sql
            The SQL.

        Returns
        -------
        tuple
            The submission's status code, then the run's status and error.
        """
        submitted = self.client.post(
            "/api/v1/environments/dev/queries", headers=self.headers, json={"sql": sql}
        )
        if submitted.status_code >= 300:
            return submitted.status_code, None, None
        query_id = uuid.UUID(submitted.json()["query_id"])
        with self.session() as session:
            run_one(session, query_id, GrantAuthorization(session))
            row = session.get(QueryRun, query_id)
            assert row is not None
            return submitted.status_code, row.status, row.error_message


def _mint(client: TestClient, task_run_id: uuid.UUID, claim: int) -> dict[str, str]:
    """Mint a bound token as the worker does and return its header.

    Parameters
    ----------
    client
        The test client.
    task_run_id
        The task run.
    claim
        Its claim counter.

    Returns
    -------
    dict
        The `Authorization` header.
    """
    with Session(client.app.state.engine) as session:
        minted = TaskTokenService(session, AllowAllAuthorization()).mint_for_task(
            task_run_id, claim, ttl_seconds=300
        )
    return {"Authorization": f"Bearer {minted.token}"}


def _table(client: TestClient, environment: str, dotted: str) -> None:
    """Create a table with one row, as the admin.

    Parameters
    ----------
    client
        The test client.
    environment
        Environment name.
    dotted
        ``namespace.table``.
    """
    namespace, name = dotted.rsplit(".", 1)
    base = f"/api/v1/environments/{environment}/namespaces/{namespace}/tables"
    created = client.post(base, headers=AUTH_HEADER, json={"name": name, "schema": _SCHEMA})
    assert created.status_code == 201, created.text
    appended = client.post(
        f"{base}/{name}/data", headers={**AUTH_HEADER, "Content-Type": _ARROW}, content=_ipc([1])
    )
    assert appended.status_code == 200, appended.text


@pytest.fixture
def bound(client: TestClient) -> Bound:
    """`dev` with an input, the task's own table and a bystander; a running attempt; its token.

    Parameters
    ----------
    client
        The test client.

    Returns
    -------
    Bound
        The attempt of `gold.scores`, reading `silver.weather`, as the service account `etl`.
    """
    create_environment(client)
    create_environment(client, name="prod")
    for dotted in ("silver.weather", "gold.scores", "gold.other"):
        _table(client, "dev", dotted)
    _table(client, "prod", "silver.weather")
    created = client.post(
        "/api/v1/principals", headers=AUTH_HEADER, json={"name": "etl", "type": "SERVICE_ACCOUNT"}
    )
    assert created.status_code == 201, created.text
    for environment in ("dev", "prod"):
        granted = client.post(
            "/api/v1/grants",
            headers=AUTH_HEADER,
            json={
                "principal": "etl",
                "role": "DATA_ENGINEER",
                "kind": "ENVIRONMENT",
                "environment": environment,
            },
        )
        assert granted.status_code < 300, granted.text
    with Session(client.app.state.engine) as session:
        env = session.execute(select(Environment).where(Environment.name == "dev")).scalar_one()
        etl = session.execute(select(Principal).where(Principal.name == "etl")).scalar_one()
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
            manifest_json={
                "version": 2,
                "assets": [
                    {
                        "table": "gold.scores",
                        "kind": "python",
                        "path": "assets/gold/scores.py",
                        "inputs": {"obs": "silver.weather"},
                        "sql": "",
                    }
                ],
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
            run_as_principal_id=etl.id,
            kind=RunKind.FULL,
            status=RunStatus.RUNNING,
            selection_json={},
        )
        session.add(run)
        session.flush()
        task = TaskRun(
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
        session.add(task)
        session.commit()
        task_run_id = task.id
    return Bound(client, task_run_id, _mint(client, task_run_id, 1))


def test_the_token_does_what_the_task_needs(bound: Bound) -> None:
    assert bound.get("dev", "silver.weather") == 200
    assert bound.query("SELECT id FROM silver.weather") == (202, QueryRunStatus.SUCCEEDED, None)
    assert bound.write("PUT", "gold.scores") == 200
    whoami = bound.client.get("/api/v1/whoami", headers=bound.headers)
    assert whoami.status_code == 200 and whoami.json()["name"] == "etl"


def test_it_cannot_read_a_table_that_is_not_a_declared_input(bound: Bound) -> None:
    assert bound.get("dev", "gold.other") == 403
    assert bound.get("dev", "gold.scores") == 403
    status, run_status, error = bound.query("SELECT id FROM gold.other")
    assert (status, run_status) == (202, QueryRunStatus.FAILED)
    assert error is not None and "PermissionDenied" in error
    status, run_status, _ = bound.query(
        "SELECT a.id FROM silver.weather a JOIN gold.other b USING (id)"
    )
    assert (status, run_status) == (202, QueryRunStatus.FAILED)
    listed = bound.client.get(
        "/api/v1/environments/dev/namespaces/gold/tables", headers=bound.headers
    )
    assert listed.status_code == 403


def _keep(bound: Bound, name: str) -> uuid.UUID:
    """Put the note of a failed `keep` check on a table of `dev`.

    Parameters
    ----------
    bound
        The attempt and its client.
    name
        The table's name; it is the only table of that name in `dev`'s namespaces used here.

    Returns
    -------
    uuid.UUID
        The run the note names.
    """
    run_id = uuid.uuid4()
    with bound.session() as session:
        table_id = session.execute(
            select(Table.id)
            .join(Namespace, Namespace.id == Table.namespace_id)
            .join(Environment, Environment.id == Namespace.environment_id)
            .where(Environment.name == "dev", Table.name == name)
        ).scalar_one()
        session.execute(
            update(Table)
            .where(Table.id == table_id)
            .values(kept_run_id=run_id, kept_task_run_id=uuid.uuid4(), kept_check="unique_id")
        )
        session.commit()
    return run_id


def test_it_cannot_read_an_input_that_was_kept_after_a_failed_check(bound: Bound) -> None:
    """The token's run has no task for the input, so both ways to it are closed.

    The query fails with the refusal as its message; the Iceberg load answers 404 with it. The
    table is still described (its state says `kept`), and an ad-hoc reader still reads it.
    """
    kept = _keep(bound, "weather")
    refusal = (
        f"`silver.weather` was kept after a failed check (`unique_id`, run {kept}) and has not "
        "passed its checks since, so this task does not read it. A run of the project that "
        "builds `silver.weather` clears this: build it again there "
        "(`lakana run --select silver.weather --env dev`), or repair the data and run that "
        "project. If no project builds it any more, drop it: "
        "`lakana table delete silver.weather --env dev`."
    )
    path = "/v1/dev/namespaces/silver/tables/weather"

    assert bound.query("SELECT id FROM silver.weather") == (202, QueryRunStatus.FAILED, refusal)
    loaded = bound.client.get(path, headers=bound.headers)
    assert loaded.status_code == 404, loaded.text
    assert loaded.json() == {
        "error": {"message": refusal, "type": "KeptAfterCheckError", "code": 404}
    }

    described = bound.client.get(
        "/api/v1/environments/dev/namespaces/silver/tables/weather", headers=bound.headers
    )
    assert described.status_code == 200 and described.json()["state"] == "kept"
    assert bound.client.get(path, headers=AUTH_HEADER).status_code == 200
    submitted = bound.client.post(
        "/api/v1/environments/dev/queries",
        headers=AUTH_HEADER,
        json={"sql": "SELECT id FROM silver.weather"},
    )
    with bound.session() as session:
        query_id = uuid.UUID(submitted.json()["query_id"])
        run_one(session, query_id, GrantAuthorization(session))
        row = session.get(QueryRun, query_id)
        assert row is not None and row.status is QueryRunStatus.SUCCEEDED


def test_a_kept_table_it_may_not_read_is_refused_as_any_other(bound: Bound) -> None:
    """The grant comes first: the token learns nothing about the note of `gold.other`."""
    kept = _keep(bound, "other")

    loaded = bound.client.get("/v1/dev/namespaces/gold/tables/other", headers=bound.headers)
    status, run_status, error = bound.query("SELECT id FROM gold.other")

    assert loaded.status_code == 403
    assert "kept" not in loaded.text and str(kept) not in loaded.text
    assert (status, run_status) == (202, QueryRunStatus.FAILED)
    assert error == "Resolving the tables this query reads failed (PermissionDenied)."


def test_it_cannot_write_a_table_other_than_its_own(bound: Bound) -> None:
    assert bound.write("PUT", "gold.other") == 403
    assert bound.write("POST", "gold.other") == 403
    assert bound.write("POST", "silver.weather") == 403
    assert bound.write("PUT", "gold.fresh", "?expected_snapshot_id=none") == 403
    created = bound.client.post(
        "/api/v1/environments/dev/namespaces/gold/tables",
        headers=bound.headers,
        json={"name": "fresh", "schema": _SCHEMA},
    )
    assert created.status_code == 403
    into = bound.client.post(
        "/api/v1/environments/dev/queries",
        headers=bound.headers,
        json={
            "sql": "SELECT 1::BIGINT AS id",
            "target": {"table": "gold.other", "mode": "replace"},
        },
    )
    assert into.status_code == 403


def test_a_write_run_into_another_table_is_refused_when_the_worker_runs_it(bound: Bound) -> None:
    """Even a queued write run stamped with the token (no submission check) writes nothing."""
    with bound.session() as session:
        token = session.execute(
            select(TaskToken).where(TaskToken.task_run_id == bound.task_run_id)
        ).scalar_one()
        task = session.get(TaskRun, bound.task_run_id)
        assert task is not None
        run = session.get(Run, task.run_id)
        assert run is not None
        forged = QueryRun(
            id=uuid.uuid4(),
            environment_id=run.environment_id,
            principal_id=token.principal_id,
            sql_text="SELECT 1::BIGINT AS id",
            status=QueryRunStatus.QUEUED,
            write_mode=WriteMode.CREATE,
            target_name="gold.fresh",
            task_token_id=token.id,
            task_run_id=bound.task_run_id,
        )
        session.add(forged)
        session.commit()
        run_one(session, forged.id, GrantAuthorization(session))
        row = session.get(QueryRun, forged.id)
        assert row is not None
        assert row.status is QueryRunStatus.FAILED
        assert "may write only its own asset 'gold.scores'" in (row.error_message or "")
    missing = bound.client.get(
        "/api/v1/environments/dev/namespaces/gold/tables/fresh", headers=AUTH_HEADER
    )
    assert missing.status_code == 404


def test_it_cannot_drop_a_table_or_do_anything_administrative(bound: Bound) -> None:
    for dotted in ("gold.scores", "gold.other", "silver.weather"):
        namespace, name = dotted.rsplit(".", 1)
        dropped = bound.client.delete(
            f"/api/v1/environments/dev/namespaces/{namespace}/tables/{name}", headers=bound.headers
        )
        assert dropped.status_code == 403, dotted
    assert bound.client.get("/api/v1/principals", headers=bound.headers).status_code == 403
    minted = bound.client.post(
        "/api/v1/task-tokens",
        headers=bound.headers,
        json={
            "principal": "etl",
            "environment": "dev",
            "asset_scope": ["gold.*"],
            "ttl_seconds": 60,
        },
    )
    assert minted.status_code == 403


def test_it_cannot_be_used_in_another_environment(bound: Bound) -> None:
    assert bound.get("prod", "silver.weather") == 403
    submitted = bound.client.post(
        "/api/v1/environments/prod/queries",
        headers=bound.headers,
        json={"sql": "SELECT id FROM silver.weather"},
    )
    assert submitted.status_code == 403


@pytest.mark.parametrize(
    "ending",
    [
        {"status": TaskStatus.SUCCEEDED},
        {"status": TaskStatus.FAILED},
        {"status": TaskStatus.PENDING},
        {"claim_count": 2},
    ],
)
def test_it_stops_working_when_the_attempt_ends_or_is_reclaimed(
    bound: Bound, ending: dict[str, object]
) -> None:
    assert bound.get("dev", "silver.weather") == 200
    with bound.session() as session:
        session.execute(update(TaskRun).where(TaskRun.id == bound.task_run_id).values(**ending))
        session.commit()
    assert bound.get("dev", "silver.weather") == 401
    assert bound.write("PUT", "gold.scores") == 401
    with bound.session() as session:
        failures = (
            session.execute(
                select(AuditEvent).where(
                    AuditEvent.event_type == AuditEventType.AUTHENTICATION_FAILED
                )
            )
            .scalars()
            .all()
        )
    assert failures


def test_a_superseded_attempts_token_fails_and_the_new_attempts_works(bound: Bound) -> None:
    with bound.session() as session:
        session.execute(
            update(TaskRun).where(TaskRun.id == bound.task_run_id).values(claim_count=2)
        )
        session.commit()
    fresh = _mint(bound.client, bound.task_run_id, 2)
    assert bound.get("dev", "silver.weather") == 401
    assert Bound(bound.client, bound.task_run_id, fresh).get("dev", "silver.weather") == 200


def test_a_query_queued_before_the_reclaim_is_refused_when_it_runs(bound: Bound) -> None:
    submitted = bound.client.post(
        "/api/v1/environments/dev/queries",
        headers=bound.headers,
        json={"sql": "SELECT id FROM silver.weather"},
    )
    assert submitted.status_code == 202, submitted.text
    query_id = uuid.UUID(submitted.json()["query_id"])
    with bound.session() as session:
        stamped = session.get(QueryRun, query_id)
        assert stamped is not None and stamped.task_run_id == bound.task_run_id
        session.execute(
            update(TaskRun).where(TaskRun.id == bound.task_run_id).values(claim_count=2)
        )
        session.commit()
    with bound.session() as session:
        run_one(session, query_id, GrantAuthorization(session))
        row = session.get(QueryRun, query_id)
        assert row is not None and row.status is QueryRunStatus.FAILED


def test_in_a_protected_environment_the_task_writes_and_a_user_does_not(bound: Bound) -> None:
    """Exit criterion: the Python task's write succeeds; a user token's same write is refused."""
    protected = bound.client.patch(
        "/api/v1/environments/dev", headers=AUTH_HEADER, json={"is_protected": True}
    )
    assert protected.status_code == 200, protected.text
    assert bound.write("PUT", "gold.scores") == 200
    assert bound.write("PUT", "gold.other") == 403
    as_user = bound.client.put(
        "/api/v1/environments/dev/namespaces/gold/tables/scores/data",
        headers={**AUTH_HEADER, "Content-Type": _ARROW},
        content=_ipc([9]),
    )
    assert as_user.status_code == 403
    assert "is protected" in as_user.text
    with bound.session() as session:
        session.execute(update(Run).values(cancel_requested=True))
        session.commit()
    # A cancel request ends the token itself (`resolve_task_token_scope`), before the guard.
    assert bound.write("PUT", "gold.scores") == 401


def test_a_bound_token_is_revocable_over_rest_only_by_an_instance_admin(bound: Bound) -> None:
    """Even the principal the token authenticates as cannot revoke it over REST."""
    with bound.session() as session:
        key_id = session.execute(
            select(TaskToken.key_id).where(TaskToken.task_run_id == bound.task_run_id)
        ).scalar_one()
    granted = bound.client.post(
        "/api/v1/grants",
        headers=AUTH_HEADER,
        json={"principal": "etl", "action": "MINT_TASK_TOKEN", "kind": "INSTANCE"},
    )
    assert granted.status_code < 300, granted.text
    issued = bound.client.post(
        "/api/v1/principals/etl/tokens", headers=AUTH_HEADER, json={"name": "etl"}
    )
    etl = {"Authorization": f"Bearer {issued.json()['token']}"}

    refused = bound.client.delete(f"/api/v1/task-tokens/{key_id}", headers=etl)
    assert refused.status_code == 403, refused.text
    assert bound.get("dev", "silver.weather") == 200
    revoked = bound.client.delete(f"/api/v1/task-tokens/{key_id}", headers=AUTH_HEADER)
    assert revoked.status_code == 204, revoked.text
    assert bound.get("dev", "silver.weather") == 401


def test_its_iceberg_commit_to_its_own_asset_answers_no_history(bound: Bound) -> None:
    """It may write its asset and not read it, so the answer is the one without history."""
    committed = bound.client.post(
        "/v1/dev/namespaces/gold/tables/scores",
        headers=bound.headers,
        json={"updates": [{"action": "set-properties", "updates": {"owner": "etl"}}]},
    )

    assert committed.status_code == 200, committed.text
    metadata = committed.json()["metadata"]
    for key in ("snapshots", "refs", "current-snapshot-id", "properties"):
        assert key not in metadata, key
    assert metadata["table-uuid"]
    loaded = bound.client.get("/v1/dev/namespaces/gold/tables/scores", headers=bound.headers)
    assert loaded.status_code == 403


def test_its_iceberg_commit_that_does_not_fit_tells_it_nothing_of_its_own_asset(
    bound: Bound,
) -> None:
    """Not the stored sequence number in pyiceberg's text, nor whether a property was there."""
    route = "/v1/dev/namespaces/gold/tables/scores"
    loaded = bound.client.get(route, headers=AUTH_HEADER).json()["metadata"]
    oldest = loaded["snapshots"][0]
    stale = {
        "action": "add-snapshot",
        "snapshot": {
            **oldest,
            "snapshot-id": 424242,
            "sequence-number": 1,
            "parent-snapshot-id": oldest["snapshot-id"],
        },
    }
    stored = str(loaded["last-sequence-number"])
    refused = bound.client.post(route, headers=bound.headers, json={"updates": [stale]})
    removals = [
        bound.client.post(
            route,
            headers=bound.headers,
            json={"updates": [{"action": "remove-properties", "removals": [key]}]},
        ).status_code
        for key in ("owner", "nobody-set-this")
    ]

    assert refused.status_code == 422, refused.text
    assert stored not in refused.text
    assert refused.json()["error"]["message"] == (
        "The updates do not fit the table's current metadata, so nothing was applied. The "
        "details need READ on the table."
    )
    assert removals == [200, 200]


def _declare_connections(bound: Bound, names: list[str]) -> None:
    """Make the running attempt's deployed asset declare `names` as its connections.

    Parameters
    ----------
    bound
        The attempt.
    names
        The connection names.
    """
    with bound.session() as session:
        task = session.get(TaskRun, bound.task_run_id)
        assert task is not None
        run = session.get(Run, task.run_id)
        assert run is not None
        version = session.get(JobVersion, run.job_version_id)
        assert version is not None
        manifest = dict(version.manifest_json)
        manifest["version"] = 7
        manifest["assets"] = [{**manifest["assets"][0], "connections": names}]
        version.manifest_json = manifest
        session.commit()


def test_its_write_names_the_declared_connections_in_lineage(bound: Bound) -> None:
    """End to end: the server derives the connection edge from the deployed manifest."""
    created = bound.client.post(
        "/api/v1/environments/dev/connections",
        headers=AUTH_HEADER,
        json={
            "name": "crm-db",
            "kind": "postgresql",
            "address": {"host": "db.example.com", "database": "crm"},
            "settings": {"user": "reader"},
        },
    )
    assert created.status_code == 201, created.text
    _declare_connections(bound, ["crm-db", "gone"])

    assert bound.write("PUT", "gold.scores", "?source=silver.weather") == 200

    response = bound.client.get(
        "/api/v1/environments/dev/namespaces/gold/tables/scores/lineage", headers=AUTH_HEADER
    )
    assert response.status_code == 200, response.text
    body = response.json()
    by_id = {node["id"]: node for node in body["nodes"]}
    [connection] = [node for node in body["nodes"] if node["kind"] == "CONNECTION"]
    assert (connection["name"], connection["connection_kind"], connection["dropped"]) == (
        "crm-db",
        "postgresql",
        False,
    )
    assert {by_id[edge["source_id"]]["name"] for edge in body["edges"]} == {
        "crm-db",
        "silver.weather",
    }
    assert all(set(edge) >= {"source_id", "target_id"} for edge in body["edges"])
    assert all("name" not in edge and "source_name" not in edge for edge in body["edges"])
    assert all(
        node["connection_kind"] is None for node in body["nodes"] if node["kind"] != "CONNECTION"
    )


def test_a_source_named_like_a_connection_is_refused_as_a_table(bound: Bound) -> None:
    """A client can never add a connection edge: `source` names tables only."""
    _declare_connections(bound, [])
    created = bound.client.post(
        "/api/v1/environments/dev/connections",
        headers=AUTH_HEADER,
        json={
            "name": "crm-db",
            "kind": "postgresql",
            "address": {"host": "db.example.com", "database": "crm"},
            "settings": {"user": "reader"},
        },
    )
    assert created.status_code == 201, created.text

    assert bound.write("PUT", "gold.scores", "?source=crm-db") in {403, 404, 422}

    response = bound.client.get(
        "/api/v1/environments/dev/namespaces/gold/tables/scores/lineage", headers=AUTH_HEADER
    )
    assert [node["kind"] for node in response.json()["nodes"]] == ["TABLE"]


def _merge(bound: Bound, dotted: str) -> tuple[int, dict[str, object]]:
    """Merge id 7 into a table on `id` with the bound token.

    Parameters
    ----------
    bound
        The attempt.
    dotted
        ``namespace.table``.

    Returns
    -------
    tuple
        The status code and the JSON answer.
    """
    namespace, name = dotted.rsplit(".", 1)
    response = bound.client.post(
        f"/api/v1/environments/dev/namespaces/{namespace}/tables/{name}/merge?key=id",
        headers={**bound.headers, "Content-Type": _ARROW},
        content=_ipc([7]),
    )
    return response.status_code, response.json()


def test_it_merges_into_its_own_asset_which_it_may_not_read(bound: Bound) -> None:
    """A merge compares with the own asset; the token may do it there and nowhere else."""
    status, body = _merge(bound, "gold.scores")
    assert status == 200, body
    assert (body["rows_inserted"], body["rows_updated"]) == (1, 0)
    assert bound.get("dev", "gold.scores") == 403
    assert _merge(bound, "gold.other")[0] == 403
    assert _merge(bound, "silver.weather")[0] == 403


def test_a_task_of_a_filtered_principal_may_not_merge_into_its_own_asset(bound: Bound) -> None:
    """The row filter of the principal the task acts for refuses the merge."""
    created = bound.client.post(
        "/api/v1/row-filters",
        headers=AUTH_HEADER,
        json={
            "principal": "etl",
            "environment": "dev",
            "namespace": "gold",
            "table": "scores",
            "expression": "id < 3",
        },
    )
    assert created.status_code < 300, created.text

    status, body = _merge(bound, "gold.scores")

    assert status == 422, body
    assert "row filter or column mask applies to you" in str(body["detail"])


def _only_actions(bound: Bound, actions: list[str]) -> None:
    """Replace `etl`'s role on `dev` with grants of exactly `actions`.

    Parameters
    ----------
    bound
        The attempt.
    actions
        The actions `etl` keeps on `dev`.
    """
    grants = bound.client.get(
        "/api/v1/grants",
        headers=AUTH_HEADER,
        params={"principal_name": "etl", "environment": "dev"},
    ).json()
    for grant in grants:
        revoked = bound.client.delete(f"/api/v1/grants/{grant['id']}", headers=AUTH_HEADER)
        assert revoked.status_code == 204, revoked.text
    for action in actions:
        granted = bound.client.post(
            "/api/v1/grants",
            headers=AUTH_HEADER,
            json={
                "principal": "etl",
                "action": action,
                "kind": "ENVIRONMENT",
                "environment": "dev",
            },
        )
        assert granted.status_code < 300, granted.text


def test_a_task_of_a_write_only_principal_may_not_merge(bound: Bound) -> None:
    """Only the token's narrowing is waived on the own asset; the principal still needs READ."""
    _only_actions(bound, ["WRITE_TABLE"])

    status, body = _merge(bound, "gold.scores")

    assert status == 403, body
    assert "READ_TABLE" in str(body["detail"])
    assert bound.write("POST", "gold.scores") == 200


def test_a_task_of_a_reading_and_writing_principal_merges(bound: Bound) -> None:
    """READ_TABLE and WRITE_TABLE alone are enough for the task's merge."""
    _only_actions(bound, ["WRITE_TABLE", "READ_TABLE"])

    status, body = _merge(bound, "gold.scores")

    assert status == 200, body
