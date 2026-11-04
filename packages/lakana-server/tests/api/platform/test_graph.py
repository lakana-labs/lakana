"""The metadata graph routes: shape, refusals, and what each reader may see (#374)."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.auth import AllowAllAuthorization
from lakana_core.catalog.descriptions import sync_project_descriptions
from lakana_core.catalog.graph_model import NO_NODE
from lakana_core.catalog.graph_service import GraphService
from lakana_core.models import (
    Environment,
    EnvironmentDeployment,
    JobVersion,
    Repository,
    RepositoryKind,
    Table,
    TableColumn,
)
from lakana_server.deps import get_graph_service
from tests.conftest import AUTH_HEADER
from tests.server_test_helpers import create_environment

_GRAPH = "/api/v1/environments/dev/graph"


def _table(client: TestClient, namespace: str, name: str, *columns: str) -> None:
    """Create `<namespace>.<name>` with long columns.

    Parameters
    ----------
    client
        The test client.
    namespace
        Its namespace.
    name
        Its name.
    *columns
        Its column names.
    """
    created = client.post(
        f"/api/v1/environments/dev/namespaces/{namespace}/tables",
        headers=AUTH_HEADER,
        json={
            "name": name,
            "schema": [{"name": c, "type": "long", "nullable": True} for c in columns],
        },
    )
    assert created.status_code == 201, created.text


def _entry(table: str, reads: list[str], model: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build a SQL asset's manifest entry.

    Parameters
    ----------
    table
        Its table.
    reads
        What it reads.
    model
        Its model, if any.

    Returns
    -------
    dict
        The entry.
    """
    entry: dict[str, Any] = {
        "table": table,
        "kind": "sql",
        "references": reads,
        "key": ["store_id"],
    }
    if model is not None:
        entry["model"] = model
    return entry


def _deploy(session: Session, env_id: uuid.UUID, name: str, manifest: dict[str, Any]) -> None:
    """Make `manifest` the current project version of a new repository `name`.

    Parameters
    ----------
    session
        Database session; committed.
    env_id
        The environment.
    name
        The repository's name.
    manifest
        Its manifest.
    """
    repo = Repository(
        id=uuid.uuid4(), name=name, kind=RepositoryKind.HTTPS, url=f"https://h/{name}"
    )
    session.add(repo)
    session.flush()
    version = JobVersion(
        id=uuid.uuid4(),
        repository_id=repo.id,
        environment_id=env_id,
        commit_sha=uuid.uuid4().hex + "00000000",
        manifest_json=manifest,
        code_uri="u",
        code_sha256="0" * 64,
    )
    session.add(version)
    session.flush()
    session.add(
        EnvironmentDeployment(
            environment_id=env_id, repository_id=repo.id, current_job_version_id=version.id
        )
    )
    sync_project_descriptions(session, env_id, manifest)
    session.commit()


def _star(client: TestClient) -> None:
    """Create `dev`, the four tables, both repositories and a row filter.

    Parameters
    ----------
    client
        The test client.
    """
    create_environment(client)
    _table(client, "bronze", "stores_raw", "store_id")
    _table(client, "gold", "dim_store", "store_id")
    _table(client, "gold", "fact_sales", "store_id", "amount")
    reference = {"column": "store_id", "target": "gold.dim_store", "target_column": "store_id"}
    fact_model = {
        "kind": "fact",
        "grain": "g",
        "key_none": False,
        "checks": {},
        "references": [reference],
    }
    check = {
        "name": "gold.fact_sales/ref_store_id",
        "table": "gold.fact_sales",
        "check": "ref_store_id",
        "path": "p",
        "on_failure": "block",
        "description": None,
        "sql": "SELECT 1",
        "references": [],
        "generated": {
            "family": "references",
            "columns": ["store_id"],
            "target": "gold.dim_store",
            "target_column": "store_id",
        },
    }
    with Session(client.app.state.engine) as session:
        env_id = session.scalars(select(Environment.id).where(Environment.name == "dev")).one()
        _deploy(
            session,
            env_id,
            "stores",
            {"version": 13, "assets": [_entry("gold.dim_store", ["bronze.stores_raw"])]},
        )
        _deploy(
            session,
            env_id,
            "sales",
            {
                "version": 13,
                "assets": [_entry("gold.fact_sales", ["gold.dim_store"], fact_model)],
                "checks": [check],
                "jobs": {"nightly": {"select": ["gold.*"]}},
            },
        )
    client.post("/api/v1/principals", headers=AUTH_HEADER, json={"name": "alice", "type": "USER"})
    made = client.post(
        "/api/v1/row-filters",
        headers=AUTH_HEADER,
        json={
            "principal": "alice",
            "environment": "dev",
            "namespace": "gold",
            "table": "fact_sales",
            "expression": "store_id > 0",
        },
    )
    assert made.status_code == 201, made.text


def _user(client: TestClient, name: str, *grants: dict[str, str]) -> dict[str, str]:
    """Create a user (unless it exists) with the given grants, and return its headers.

    Parameters
    ----------
    client
        The test client.
    name
        The principal's name.
    *grants
        Grant bodies without `principal`.

    Returns
    -------
    dict
        The `Authorization` header.
    """
    client.post("/api/v1/principals", headers=AUTH_HEADER, json={"name": name, "type": "USER"})
    for grant in grants:
        granted = client.post(
            "/api/v1/grants", headers=AUTH_HEADER, json={"principal": name, **grant}
        )
        assert granted.status_code < 300, granted.text
    issued = client.post(
        f"/api/v1/principals/{name}/tokens", headers=AUTH_HEADER, json={"name": name}
    )
    return {"Authorization": f"Bearer {issued.json()['token']}"}


def _alice(client: TestClient) -> dict[str, str]:
    """Give `alice` `READ_ENVIRONMENT` and `VIEWER` on `gold.fact_sales` only.

    Parameters
    ----------
    client
        The test client.

    Returns
    -------
    dict
        Her headers.
    """
    return _user(
        client,
        "alice",
        {"action": "READ_ENVIRONMENT", "kind": "ENVIRONMENT", "environment": "dev"},
        {
            "role": "VIEWER",
            "kind": "TABLE",
            "environment": "dev",
            "namespace": "gold",
            "table": "fact_sales",
        },
    )


def test_lineage_and_neighbours_have_the_documented_shape(client: TestClient) -> None:
    _star(client)
    answer = client.get(f"{_GRAPH}/lineage", headers=AUTH_HEADER, params={"node": "gold.dim_store"})
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert set(body) == {"nodes", "edges", "next_cursor"}
    assert {(n["kind"], n["name"], n["distance"]) for n in body["nodes"]} == {
        ("table", "gold.dim_store", 0),
        ("asset", "gold.dim_store", 1),
        ("table", "bronze.stores_raw", 1),
        ("asset", "gold.fact_sales", 1),
        ("table", "gold.fact_sales", 1),
    }
    root = body["nodes"][0]
    assert root["id"].startswith("table:") and root["repository"] == "stores"
    assert set(root) >= {
        "id",
        "kind",
        "name",
        "distance",
        "table_type",
        "model_kind",
        "grain",
        "key",
        "owner",
        "label",
    }
    assert {e["kind"] for e in body["edges"]} == {"builds", "reads"}
    near = client.get(
        f"{_GRAPH}/neighbours",
        headers=AUTH_HEADER,
        params={"node": "gold.fact_sales", "kinds": ["checks", "filters"]},
    ).json()
    assert {(n["kind"], n["name"]) for n in near["nodes"]} == {
        ("table", "gold.fact_sales"),
        ("check", "gold.fact_sales/ref_store_id"),
        ("row_filter", "row filter on gold.fact_sales"),
    }


def test_a_viewer_of_one_table_sees_what_she_may_and_nothing_counts_the_rest(
    client: TestClient,
) -> None:
    _star(client)
    alice = _alice(client)
    body = client.get(f"{_GRAPH}/lineage", headers=alice, params={"node": "gold.fact_sales"}).json()
    assert {(n["kind"], n["name"]) for n in body["nodes"]} == {
        ("table", "gold.fact_sales"),
        ("asset", "gold.fact_sales"),
    }
    assert set(body) == {"nodes", "edges", "next_cursor"}
    near = client.get(
        f"{_GRAPH}/neighbours", headers=alice, params={"node": "gold.fact_sales"}
    ).json()
    kinds = {n["kind"] for n in near["nodes"]}
    assert (
        "row_filter" not in kinds and "check" not in kinds
    )  # no MANAGE_POLICY; the check's target is hidden
    hidden = client.get(f"{_GRAPH}/lineage", headers=alice, params={"node": "gold.dim_store"})
    missing = client.get(f"{_GRAPH}/lineage", headers=alice, params={"node": "gold.nope"})
    assert hidden.status_code == missing.status_code == 404
    assert hidden.json()["detail"] == NO_NODE.format(node="gold.dim_store", env="dev")
    assert missing.json()["detail"] == NO_NODE.format(node="gold.nope", env="dev")


def test_an_outsider_gets_the_same_404_for_any_start_or_environment(client: TestClient) -> None:
    _star(client)
    outsider = _user(client, "mallory")
    for env, node in (
        ("dev", "gold.fact_sales"),
        ("dev", "gold.nope"),
        ("prod", "gold.fact_sales"),
    ):
        answer = client.get(
            f"/api/v1/environments/{env}/graph/neighbours", headers=outsider, params={"node": node}
        )
        assert answer.status_code == 404
        assert answer.json()["detail"].startswith(
            f"There is no node `{node}` in environment `{env}` that you may see."
        )
    admin = client.get(
        "/api/v1/environments/prod/graph/neighbours", headers=AUTH_HEADER, params={"node": "gold.x"}
    )
    assert (
        admin.json()["detail"]
        == "Environment 'prod' does not exist. List them with 'lakana env list'."
    )


def test_bad_parameters_are_422_with_the_next_step(client: TestClient) -> None:
    _star(client)
    cases = [
        ("lineage", {"node": "gold.\x00x"}, "is not a node id. Give a table's dotted name"),
        ("lineage", {"node": "x" * 3000}, "is not a node id."),
        (
            "lineage",
            {"node": "gold.dim_store", "depth": 0},
            "depth must be between 1 and 10; got 0.",
        ),
        (
            "lineage",
            {"node": "gold.dim_store", "limit": 501},
            "The page size must be between 1 and 500 (`limit` in the REST API, `page_size` in",
        ),
        ("neighbours", {"node": "gold.dim_store", "kinds": "nope"}, "`nope` is not an edge kind."),
        ("lineage", {"node": "term:x"}, "Lineage starts at a table, a column or an asset"),
    ]
    for route, params, text in cases:
        answer = client.get(f"{_GRAPH}/{route}", headers=AUTH_HEADER, params=params)
        assert answer.status_code == 422, (route, params, answer.text)
        assert text in answer.json()["detail"]
    foreign = "eyJrIjogInRlcm1zIiwgIm4iOiAieCJ9"  # {"k": "terms", "n": "x"}
    answer = client.get(
        f"{_GRAPH}/neighbours",
        headers=AUTH_HEADER,
        params={"node": "gold.dim_store", "cursor": foreign},
    )
    assert answer.status_code == 422 and "came from the `terms` list" in answer.json()["detail"]
    assert (
        client.get(
            f"{_GRAPH}/lineage",
            headers=AUTH_HEADER,
            params={"node": "gold.dim_store", "direction": "sideways"},
        ).status_code
        == 422
    )


def test_a_walk_past_its_time_is_503_with_retry_after(client: TestClient) -> None:
    _star(client)
    ticks = iter([0.0, 100.0, 200.0, 300.0])

    def slow() -> GraphService:
        """Build a service whose clock runs out at once.

        Returns
        -------
        GraphService
            The service.
        """
        return GraphService(
            Session(client.app.state.engine), AllowAllAuthorization(), clock=lambda: next(ticks)
        )

    client.app.dependency_overrides[get_graph_service] = slow
    try:
        answer = client.get(
            f"{_GRAPH}/neighbours", headers=AUTH_HEADER, params={"node": "gold.dim_store"}
        )
    finally:
        client.app.dependency_overrides.pop(get_graph_service)
    assert answer.status_code == 503 and answer.headers["Retry-After"] == "5"
    assert answer.json()["detail"].startswith(
        "The graph walk took longer than 10 s and was stopped."
    )


def test_the_next_cursor_after_a_long_column_name_is_accepted(client: TestClient) -> None:
    _star(client)
    long_name = "a" * 300
    _table(client, "gold", "wide", "b")
    # Longer than the REST API takes for a new column; an older or engine-written schema has it.
    with Session(client.app.state.engine) as session:
        wide = session.scalars(select(Table.id).where(Table.name == "wide")).one()
        session.add(
            TableColumn(id=uuid.uuid4(), table_id=wide, name=long_name, type="long", position=1)
        )
        session.commit()
    first = client.get(
        f"{_GRAPH}/neighbours", headers=AUTH_HEADER, params={"node": "gold.wide", "limit": 2}
    )
    assert first.status_code == 200, first.text
    assert first.json()["nodes"][-1]["name"] == f"gold.wide.{long_name}"
    cursor = first.json()["next_cursor"]
    assert len(cursor) > 512
    second = client.get(
        f"{_GRAPH}/neighbours",
        headers=AUTH_HEADER,
        params={"node": "gold.wide", "limit": 2, "cursor": cursor},
    )
    assert second.status_code == 200, second.text
    assert [n["name"] for n in second.json()["nodes"]] == ["gold.wide.b"]
