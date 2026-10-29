"""Over REST, a generated reference check is withheld from a reader who may not read its target."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.models import CheckOutcome, CheckResult, JobVersion, Run, Table, TaskRun
from lakana_core.runs.checks import check_not_in_run_message
from tests.api.platform.test_deployments_run_as import _deploy
from tests.conftest import AUTH_HEADER

_ENV = "/api/v1/environments/dev"
_REF = "silver.clean/ref_store_id"
_SQL = (
    "SELECT store_id FROM silver.clean s LEFT JOIN gold.dim_store d USING (store_id) "
    "WHERE d.store_id IS NULL"
)


def _run_with_checks(client: TestClient) -> str:
    """Deploy, start a run, create `silver.clean` and store two failed checks of it.

    One is the generated `ref_store_id` of a reference to `gold.dim_store`, the other a
    hand-written `positive`; the run's project version declares both.

    Parameters
    ----------
    client
        The test client.

    Returns
    -------
    str
        The run's id.
    """
    _deploy(client)
    run_id = client.post(f"{_ENV}/runs", headers=AUTH_HEADER, json={"repository": "analytics"})
    run_id = run_id.json()["id"]
    created = client.post(
        f"{_ENV}/namespaces/silver/tables",
        headers=AUTH_HEADER,
        json={"name": "clean", "schema": [{"name": "store_id", "type": "long", "nullable": True}]},
    )
    if created.status_code == 404:
        client.post(f"{_ENV}/namespaces", headers=AUTH_HEADER, json={"path": "silver"})
        created = client.post(
            f"{_ENV}/namespaces/silver/tables",
            headers=AUTH_HEADER,
            json={
                "name": "clean",
                "schema": [{"name": "store_id", "type": "long", "nullable": True}],
            },
        )
    assert created.status_code == 201, created.text
    now = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
    with Session(client.app.state.engine) as session:
        run = session.get(Run, uuid.UUID(run_id))
        assert run is not None
        task = session.scalars(select(TaskRun).where(TaskRun.run_id == run.id)).one()
        table_id = session.scalars(select(Table.id).where(Table.name == "clean")).one()
        for check, generated in (("ref_store_id", True), ("positive", False)):
            session.add(
                CheckResult(
                    id=uuid.uuid4(),
                    run_id=run.id,
                    task_run_id=task.id,
                    environment_id=run.environment_id,
                    check_name=f"silver.clean/{check}",
                    check_path="assets/silver/clean.sql:2",
                    table_name="silver.clean",
                    table_id=table_id,
                    on_failure="block",
                    outcome=CheckOutcome.FAILED,
                    failing_rows=2,
                    checked_snapshot_id=42,
                    started_at=now,
                    finished_at=now,
                    generated=generated,
                )
            )
        version = session.get(JobVersion, run.job_version_id)
        assert version is not None
        base = {"table": "silver.clean", "path": "assets/silver/clean.sql:2", "sql": _SQL}
        version.manifest_json = {
            **version.manifest_json,
            "checks": [
                {
                    **base,
                    "name": _REF,
                    "check": "ref_store_id",
                    "on_failure": "block",
                    "generated": {
                        "family": "references",
                        "columns": ["store_id"],
                        "target": "gold.dim_store",
                        "target_column": "store_id",
                    },
                },
                {**base, "name": "silver.clean/positive", "check": "positive"},
            ],
        }
        session.commit()
    return run_id


def _reader(client: TestClient) -> dict[str, str]:
    """Create a user who may read the runs and `silver.clean`, not `gold.dim_store`.

    Parameters
    ----------
    client
        The test client.

    Returns
    -------
    dict
        Its `Authorization` header.
    """
    client.post("/api/v1/principals", headers=AUTH_HEADER, json={"name": "rita", "type": "USER"})
    for grant in (
        {"action": "READ_ENVIRONMENT", "kind": "ENVIRONMENT"},
        {"action": "SUBMIT_QUERY", "kind": "ENVIRONMENT"},
        {"role": "VIEWER", "kind": "TABLE", "namespace": "silver", "table": "clean"},
    ):
        granted = client.post(
            "/api/v1/grants",
            headers=AUTH_HEADER,
            json={"principal": "rita", "environment": "dev", **grant},
        )
        assert granted.status_code < 300, granted.text
    issued = client.post(
        "/api/v1/principals/rita/tokens", headers=AUTH_HEADER, json={"name": "rita"}
    )
    return {"Authorization": f"Bearer {issued.json()['token']}"}


def test_the_run_route_withholds_the_check_and_counts_it(client: TestClient) -> None:
    run_id = _run_with_checks(client)
    (task,) = client.get(f"{_ENV}/runs/{run_id}", headers=_reader(client)).json()["tasks"]
    assert [c["check"] for c in task["checks"]] == ["positive"]
    assert task["hidden_checks"] == 1
    assert "dim_store" not in str(task)
    (full,) = client.get(f"{_ENV}/runs/{run_id}", headers=AUTH_HEADER).json()["tasks"]
    assert ([c["check"] for c in full["checks"]], full["hidden_checks"]) == (
        ["positive", "ref_store_id"],
        0,
    )


def test_the_run_checks_route_withholds_the_check(client: TestClient) -> None:
    run_id = _run_with_checks(client)
    answer = client.get(f"{_ENV}/runs/{run_id}/checks", headers=_reader(client))
    assert [c["check"] for c in answer.json()] == ["positive"]


def test_the_check_listing_withholds_the_check_and_counts_it(client: TestClient) -> None:
    _run_with_checks(client)
    page = client.get(f"{_ENV}/checks", headers=_reader(client)).json()
    assert ([c["check"] for c in page["items"]], page["hidden"]) == (["positive"], 1)
    every = client.get(f"{_ENV}/checks", headers=AUTH_HEADER).json()
    assert every["hidden"] == 0


def test_table_health_withholds_the_check_and_counts_it(client: TestClient) -> None:
    _run_with_checks(client)
    health = client.get(f"{_ENV}/namespaces/silver/tables/clean/health", headers=_reader(client))
    assert health.status_code == 200, health.text
    assert ([c["check"] for c in health.json()["checks"]], health.json()["hidden_checks"]) == (
        ["positive"],
        1,
    )


def test_the_rows_of_a_withheld_check_are_404_as_for_a_missing_check(client: TestClient) -> None:
    run_id = _run_with_checks(client)
    answer = client.post(
        f"{_ENV}/runs/{run_id}/checks/rows", headers=_reader(client), json={"check": _REF}
    )
    assert answer.status_code == 404
    assert answer.json()["detail"] == check_not_in_run_message(uuid.UUID(run_id), _REF, "dev")
