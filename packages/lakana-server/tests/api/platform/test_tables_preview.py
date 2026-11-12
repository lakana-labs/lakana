"""The preview route of a table: the first 100 rows, read as the caller."""

from __future__ import annotations

import uuid

import pyarrow as pa
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.models import Principal
from tests.api.platform.test_queries import result_url, stored_result
from tests.conftest import AUTH_HEADER
from tests.server_test_helpers import (
    create_environment,
    create_table,
    person,
    query_run_columns,
    upload_artifact,
)

PREVIEW_PATH = "/api/v1/environments/{env}/namespaces/{path}/tables/{name}/preview"
"""The route as OpenAPI spells it."""


def url(dotted: str) -> str:
    """Build the preview route of a table in `dev`.

    Parameters
    ----------
    dotted
        ``<namespace>.<table>``.

    Returns
    -------
    str
        The URL.
    """
    namespace, _, name = dotted.rpartition(".")
    return f"/api/v1/environments/dev/namespaces/{namespace}/tables/{name}/preview"


PREVIEW_URL = url("gold.orders")
"""The preview route of `dev.gold.orders`."""


def _grant(client: TestClient, name: str, body: dict[str, str]) -> None:
    """Grant a principal one action.

    Parameters
    ----------
    client
        Test client for the API.
    name
        The principal's name.
    body
        The grant's action and scope.
    """
    granted = client.post("/api/v1/grants", headers=AUTH_HEADER, json={"principal": name, **body})
    assert granted.status_code == 201, granted.text


@pytest.fixture
def previews(client: TestClient) -> TestClient:
    """Create `dev` with `gold.orders`, `gold.secret` and `gold.other`.

    Parameters
    ----------
    client
        Test client for the API.

    Returns
    -------
    TestClient
        The same client.
    """
    create_environment(client)
    for table in ("orders", "secret", "other"):
        create_table(client, "dev", "gold", table)
    return client


def test_a_viewer_is_refused_with_the_next_step(previews: TestClient) -> None:
    vic = person(previews, "vic", "VIEWER")
    response = previews.post(PREVIEW_URL, headers=vic)
    assert response.status_code == 403
    assert response.json()["detail"] == (
        "Preview runs a query as you, which needs the right to run queries in dev. "
        "Ask an administrator for the Analyst role there."
    )


def test_the_preview_route_accepts_no_sql_or_limit(previews: TestClient) -> None:
    op = previews.app.openapi()["paths"][PREVIEW_PATH]["post"]
    assert "requestBody" not in op
    # The bearer token is the only header; there is no query parameter at all.
    assert {(p["name"], p["in"]) for p in op["parameters"]} == {
        ("env", "path"),
        ("path", "path"),
        ("name", "path"),
        ("authorization", "header"),
    }
    ann = person(previews, "ann", "ANALYST")
    response = previews.post(
        PREVIEW_URL + "?limit=5000&sql=SELECT%201",
        json={"sql": "SELECT 1", "limit": 5000},
        headers=ann,
    )
    assert response.status_code == 202
    assert query_run_columns(previews, response.json()["query_id"], "sql_text") == {
        "sql_text": 'SELECT * FROM "gold"."orders" LIMIT 100'
    }


def test_a_preview_is_the_callers_own_run_bound_to_the_table(previews: TestClient) -> None:
    ann = person(previews, "ann", "ANALYST")
    qid = previews.post(PREVIEW_URL, headers=ann).json()["query_id"]
    status = previews.get(f"/api/v1/environments/dev/queries/{qid}", headers=ann)
    assert (status.status_code, status.json()["status"]) == (200, "QUEUED")
    columns = query_run_columns(previews, qid, "preview_table_id")
    assert isinstance(columns["preview_table_id"], uuid.UUID)
    assert (
        previews.get(f"/api/v1/environments/dev/queries/{qid}", headers=AUTH_HEADER).status_code
        == 404
    )


def test_preview_answers_the_same_for_a_missing_and_an_unreadable_table(
    previews: TestClient,
) -> None:
    previews.post("/api/v1/principals", headers=AUTH_HEADER, json={"name": "h", "type": "USER"})
    _grant(previews, "h", {"action": "SUBMIT_QUERY", "kind": "ENVIRONMENT", "environment": "dev"})
    _grant(
        previews,
        "h",
        {
            "action": "READ_TABLE",
            "kind": "TABLE",
            "environment": "dev",
            "namespace": "gold",
            "table": "other",
        },
    )
    token = previews.post(
        "/api/v1/principals/h/tokens", headers=AUTH_HEADER, json={"name": "h"}
    ).json()["token"]
    h = {"Authorization": f"Bearer {token}"}
    assert previews.post(url("gold.other"), headers=h).status_code == 202
    secret = previews.post(url("gold.secret"), headers=h)
    nothing = previews.post(url("gold.nothing"), headers=h)
    assert (secret.status_code, nothing.status_code) == (403, 403)
    assert secret.content == nothing.content


def test_an_artifact_preview_is_409_with_the_download_command(previews: TestClient) -> None:
    upload_artifact(previews, "dev", "gold.model", {"weights.bin": b"w"})
    ann = person(previews, "ann", "ANALYST")
    response = previews.post(url("gold.model"), headers=ann)
    assert response.status_code == 409
    assert "lakana artifact download gold.model DIRECTORY --env dev" in response.json()["detail"]


def test_a_preview_returns_at_most_100_rows(previews: TestClient) -> None:
    qid = stored_result(
        previews,
        "dev",
        pa.table({"i": pa.array(range(150), pa.int32())}),
        preview_table_id=uuid.uuid4(),
        sql_text='SELECT * FROM "gold"."orders" LIMIT 100',
    )
    page = previews.get(result_url(qid, "format=json"), headers=AUTH_HEADER).json()
    assert (len(page["rows"]), page["row_count"], page["next_offset"], page["preview"]) == (
        100,
        100,
        None,
        {"table": "gold.orders"},
    )
    assert (
        previews.get(result_url(qid, "format=json&offset=100"), headers=AUTH_HEADER).json()["rows"]
        == []
    )
    refused = previews.get(result_url(qid, "format=json&limit=101"), headers=AUTH_HEADER)
    assert (refused.status_code, refused.json()["detail"]) == (
        422,
        "A preview shows at most 100 rows; ask with limit=100 or less.",
    )


def test_another_principal_gets_404_for_a_preview_result(previews: TestClient) -> None:
    qid = stored_result(
        previews,
        "dev",
        pa.table({"i": [1]}),
        preview_table_id=uuid.uuid4(),
        sql_text='SELECT * FROM "gold"."orders" LIMIT 100',
        masked_columns=[{"table": "gold.orders", "column": "email"}],
    )
    bob = person(previews, "bob", "ADMIN")
    response = previews.get(result_url(qid, "format=json"), headers=bob)
    assert response.status_code == 404
    assert "email" not in response.text


def test_a_full_limiter_does_not_make_another_principals_run_a_503(previews: TestClient) -> None:
    """The lookup comes before the limiter, so a full limiter is no existence oracle.

    Both caps are full: the process's slots and bob's own. If `try_acquire` ran before
    `get_query_run`, bob would get 503 for the run that exists and 404 for one that does
    not, which tells him which ids exist.
    """
    qid = stored_result(
        previews,
        "dev",
        pa.table({"i": [1]}),
        preview_table_id=uuid.uuid4(),
        sql_text='SELECT * FROM "gold"."orders" LIMIT 100',
    )
    bob = person(previews, "bob", "ADMIN")
    with Session(previews.app.state.engine) as session:
        bob_id = session.scalars(select(Principal.id).where(Principal.name == "bob")).one()
    limiter = previews.app.state.json_page_limiter
    assert limiter.try_acquire(bob_id) is None
    while limiter.try_acquire(uuid.uuid4()) is None:
        pass
    assert limiter.try_acquire(bob_id) is not None
    assert limiter.try_acquire(uuid.uuid4()) == "all"
    others = previews.get(result_url(qid, "format=json"), headers=bob)
    missing = previews.get(result_url(str(uuid.uuid4()), "format=json"), headers=bob)
    assert (others.status_code, missing.status_code) == (404, 404)
    assert others.json()["type"] == missing.json()["type"]
