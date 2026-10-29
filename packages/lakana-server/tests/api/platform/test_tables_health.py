"""Table health over REST: the numbers in `TableOut` and `GET .../tables/{name}/health`."""

from __future__ import annotations

import io
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pyarrow as pa
import pyarrow.ipc
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.config import TableReadsConfig
from lakana_core.models import (
    CheckOutcome,
    CheckResult,
    Environment,
    Principal,
    QueryRun,
    QueryRunStatus,
    Table,
    TableRead,
    TaskRun,
    WriteMode,
)
from lakana_server.app import create_app
from lakana_server.settings import Settings
from tests.api.platform.test_deployments_run_as import _deploy
from tests.api.platform.test_tables_descriptions import _person
from tests.conftest import AUTH_HEADER
from tests.server_test_helpers import create_environment

_TABLES = "/api/v1/environments/dev/namespaces/gold/tables"
_SCHEMA = [
    {"name": "id", "type": "long", "nullable": False},
    {"name": "note", "type": "string", "nullable": True},
]
_ARROW = pa.schema([pa.field("id", pa.int64(), nullable=False), pa.field("note", pa.string())])
_STREAM = {**AUTH_HEADER, "Content-Type": "application/vnd.apache.arrow.stream"}


def _table(client: TestClient, name: str = "t", rows: int = 0) -> None:
    """Create `dev` if needed and the table `gold.<name>`, with `rows` rows appended.

    Parameters
    ----------
    client
        The test client.
    name
        The table's name.
    rows
        How many rows to append; none when 0.
    """
    if client.get("/api/v1/environments/dev", headers=AUTH_HEADER).status_code == 404:
        create_environment(client)
    created = client.post(_TABLES, headers=AUTH_HEADER, json={"name": name, "schema": _SCHEMA})
    assert created.status_code == 201, created.text
    if rows:
        sink = io.BytesIO()
        data = pa.table({"id": list(range(rows)), "note": ["x"] * rows}, schema=_ARROW)
        with pyarrow.ipc.new_stream(sink, data.schema) as writer:
            writer.write_table(data)
        appended = client.post(f"{_TABLES}/{name}/data", headers=_STREAM, content=sink.getvalue())
        assert appended.status_code == 200, appended.text


def _health(
    client: TestClient, name: str = "t", headers: dict | None = None, **params: object
) -> httpx.Response:
    """Fetch the health of `gold.<name>`.

    Parameters
    ----------
    client
        The test client.
    name
        The table's name.
    headers
        The caller; the bootstrap administrator when `None`.
    **params
        Query parameters.

    Returns
    -------
    httpx.Response
        The response.
    """
    return client.get(f"{_TABLES}/{name}/health", headers=headers or AUTH_HEADER, params=params)


def _table_id(client: TestClient, name: str = "t") -> uuid.UUID:
    """Look up a table's id.

    Parameters
    ----------
    client
        The test client.
    name
        The table's name.

    Returns
    -------
    uuid.UUID
        The id.
    """
    with Session(client.app.state.engine) as session:
        return session.scalars(select(Table.id).where(Table.name == name)).one()


def _admin_id(client: TestClient) -> uuid.UUID:
    """Look up the bootstrap administrator's id.

    Parameters
    ----------
    client
        The test client.

    Returns
    -------
    uuid.UUID
        The id.
    """
    with Session(client.app.state.engine) as session:
        return session.scalars(select(Principal.id).where(Principal.name == "admin")).one()


def _add_reads(client: TestClient, who: uuid.UUID, days_ago: int, count: int) -> None:
    """Store a day's count of reads of `gold.t`.

    Parameters
    ----------
    client
        The test client.
    who
        The reader.
    days_ago
        How many UTC days before today.
    count
        The count.
    """
    at = datetime.now(UTC) - timedelta(days=days_ago)
    with Session(client.app.state.engine) as session:
        session.add(
            TableRead(
                id=uuid.uuid4(),
                table_id=_table_id(client),
                principal_id=who,
                day=at.date(),
                read_count=count,
                last_read_at=at,
            )
        )
        session.commit()


def test_a_table_without_data_has_no_numbers(client: TestClient) -> None:
    _table(client)
    for body in (
        client.get(f"{_TABLES}/t", headers=AUTH_HEADER).json(),
        client.get(_TABLES, headers=AUTH_HEADER).json()[0],
    ):
        assert (body["last_write_at"], body["row_count"], body["size_bytes"]) == (None, None, None)
        assert body["file_count"] is None


def test_a_table_with_data_shows_the_numbers_of_its_snapshot(client: TestClient) -> None:
    _table(client, rows=3)
    detail = client.get(f"{_TABLES}/t", headers=AUTH_HEADER).json()
    (listed,) = client.get(_TABLES, headers=AUTH_HEADER).json()
    for body in (detail, listed):
        assert (body["row_count"], body["file_count"]) == (3, 1)
        assert body["size_bytes"] > 0
        written = datetime.fromisoformat(body["last_write_at"])
        assert abs(datetime.now(UTC) - written) < timedelta(minutes=5)


def test_the_health_of_a_fresh_table_is_empty(client: TestClient) -> None:
    _table(client)
    response = _health(client)
    assert response.status_code == 200, response.text
    assert response.json() == {
        "days": 30,
        "last_maintenance": None,
        "checks": [],
        "readers": [],
        "hidden_checks": 0,
    }


def test_a_query_that_read_the_table_shows_up_among_its_readers(client: TestClient) -> None:
    from lakana_core.grants.authorization import GrantAuthorization
    from lakana_worker.pipeline import run_one

    _table(client, rows=2)
    for _ in range(2):
        submitted = client.post(
            "/api/v1/environments/dev/queries",
            headers=AUTH_HEADER,
            json={"sql": "SELECT * FROM gold.t"},
        )
        assert submitted.status_code == 202, submitted.text
        with Session(client.app.state.engine) as session:
            run_one(session, uuid.UUID(submitted.json()["query_id"]), GrantAuthorization(session))
    (reader,) = _health(client).json()["readers"]
    assert (reader["principal"], reader["read_count"]) == ("admin", 2)
    assert abs(datetime.now(UTC) - datetime.fromisoformat(reader["last_read_at"])) < timedelta(
        minutes=5
    )


def test_days_limits_the_window_of_the_readers(client: TestClient) -> None:
    _table(client)
    admin = _admin_id(client)
    _add_reads(client, admin, 0, 1)
    _add_reads(client, admin, 10, 5)
    _add_reads(client, admin, 60, 7)
    counts = {
        days: [r["read_count"] for r in _health(client, days=days).json()["readers"]]
        for days in (1, 11, 90)
    }
    assert counts == {1: [1], 11: [6], 90: [13]}
    assert _health(client).json()["days"] == 30
    assert _health(client).json()["readers"][0]["read_count"] == 6
    assert _health(client, days=90).json()["days"] == 90


def test_days_outside_one_to_the_kept_period_is_refused_with_the_range(
    client: TestClient,
) -> None:
    _table(client)
    for days in (0, -1, 91):
        refused = _health(client, days=days)
        assert refused.status_code == 422, (days, refused.text)
        assert "between 1 and 90" in refused.json()["detail"]
        assert "LAKANA_TABLE_READS__KEEP_DAYS" in refused.json()["detail"]
    assert _health(client, days="many").status_code == 422


def test_an_administrator_can_change_the_kept_period_and_the_default_window(
    settings: Settings,
) -> None:
    custom = settings.model_copy(
        update={"table_reads": TableReadsConfig(keep_days=400, default_days=7)}
    )
    with TestClient(create_app(custom)) as client:
        _table(client)
        assert _health(client).json()["days"] == 7
        assert _health(client, days=400).status_code == 200
        refused = _health(client, days=401)
        assert refused.status_code == 422
        assert "between 1 and 400" in refused.json()["detail"]


def test_a_reader_sees_the_health_but_not_the_readers(client: TestClient) -> None:
    _table(client)
    _add_reads(client, _admin_id(client), 0, 3)
    reader = _person(client, "rita", "READ_TABLE")
    shown = _health(client, headers=reader)
    assert shown.status_code == 200, shown.text
    assert shown.json()["readers"] is None
    assert set(shown.json()) == {"days", "last_maintenance", "checks", "readers", "hidden_checks"}
    assert [r["principal"] for r in _health(client).json()["readers"]] == ["admin"]


def test_an_administrator_of_one_environment_only_sees_no_readers(
    client: TestClient,
) -> None:
    _table(client)
    holder = _person(client, "gina", "READ_TABLE")
    client.post(
        "/api/v1/grants",
        headers=AUTH_HEADER,
        json={"principal": "gina", "role": "ADMIN", "kind": "ENVIRONMENT", "environment": "dev"},
    )
    assert _health(client, headers=holder).json()["readers"] is None


def test_someone_without_read_table_is_refused_like_on_the_other_routes(
    client: TestClient,
) -> None:
    _table(client)
    outsider = _person(client, "otto")
    assert client.get(f"{_TABLES}/t", headers=outsider).status_code == 403
    assert _health(client, headers=outsider).status_code == 403


def test_a_table_that_does_not_exist_is_404(client: TestClient) -> None:
    _table(client)
    assert _health(client, name="nope").status_code == 404


def test_the_health_route_needs_a_token(client: TestClient) -> None:
    _table(client)
    assert client.get(f"{_TABLES}/t/health").status_code == 401


def test_the_last_maintenance_and_the_checks_are_shown(client: TestClient) -> None:
    _deploy(client)
    created = client.post(
        "/api/v1/environments/dev/namespaces/gold/tables",
        headers=AUTH_HEADER,
        json={"name": "t", "schema": _SCHEMA},
    )
    assert created.status_code == 201, created.text
    run = client.post(
        "/api/v1/environments/dev/runs", headers=AUTH_HEADER, json={"repository": "analytics"}
    ).json()
    finished = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
    with Session(client.app.state.engine) as session:
        env = session.scalars(select(Environment)).one()
        task = session.scalars(select(TaskRun)).one()
        session.add(
            QueryRun(
                id=uuid.uuid4(),
                environment_id=env.id,
                principal_id=_admin_id(client),
                sql_text="-- maintain gold.t",
                status=QueryRunStatus.FAILED,
                write_mode=WriteMode.MAINTAIN,
                target_name="gold.t",
                target_table_id=_table_id(client),
                maintenance={
                    "request": {"dry_run": False},
                    "report": {
                        "dry_run": False,
                        "stopped": True,
                        "snapshots_expired": [1, 2, 3],
                        "files_deleted": 4,
                        "bytes_deleted": 2048,
                        "tags_removed": ["a"],
                    },
                },
                finished_at=finished,
            )
        )
        for name, outcome, on_failure in (
            ("a", CheckOutcome.PASSED, "block"),
            ("b", CheckOutcome.FAILED, "keep"),
        ):
            session.add(
                CheckResult(
                    id=uuid.uuid4(),
                    run_id=uuid.UUID(run["id"]),
                    task_run_id=task.id if name == "a" else _second_task(session, task),
                    environment_id=env.id,
                    check_name=f"gold.t/{name}",
                    check_path=f"checks/gold/t/{name}.sql",
                    table_name="gold.t",
                    table_id=_table_id(client),
                    on_failure=on_failure,
                    outcome=outcome,
                    failing_rows=2 if outcome is CheckOutcome.FAILED else 0,
                    started_at=finished,
                    finished_at=finished,
                    generated=name == "a",
                )
            )
        session.commit()
    body = _health(client).json()
    assert body["last_maintenance"] == {
        "run_id": body["last_maintenance"]["run_id"],
        "finished_at": "2026-10-11T12:00:00Z",
        "status": "stopped",
        "snapshots_expired": 3,
        "files_deleted": 4,
        "bytes_deleted": 2048,
        "tags_removed": 1,
    }
    by_check = {c["check"]: c for c in body["checks"]}
    assert set(by_check) == {"a", "b"}
    assert (by_check["a"]["outcome"], by_check["a"]["kept"]) == ("passed", False)
    assert (by_check["b"]["outcome"], by_check["b"]["kept"], by_check["b"]["failing_rows"]) == (
        "failed",
        True,
        2,
    )
    assert by_check["b"]["not_published"] is False
    assert by_check["b"]["run_id"] == run["id"]
    assert by_check["b"]["name"] == "gold.t/b"
    assert (by_check["a"]["generated"], by_check["b"]["generated"]) == (True, False)


def _second_task(session: Session, first: TaskRun) -> uuid.UUID:
    """Persist a second task run of the same run, as a check result needs one of its own.

    Parameters
    ----------
    session
        Database session; the caller commits.
    first
        The existing task run to copy.

    Returns
    -------
    uuid.UUID
        The id of the new task run.
    """
    second = TaskRun(
        id=uuid.uuid4(),
        run_id=first.run_id,
        asset="gold.other",
        status=first.status,
        claim_count=0,
        attempt=1,
        max_attempts=first.max_attempts,
        timeout_seconds=first.timeout_seconds,
        retry_delay_seconds=0,
        upstream_json=[],
    )
    session.add(second)
    session.flush()
    return second.id
