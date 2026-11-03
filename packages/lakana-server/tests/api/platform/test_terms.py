"""The glossary routes: the documented shape, the errors, and what a reader may see (#375)."""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.catalog.glossary import write_glossary
from lakana_core.models import Environment, Repository, RepositoryKind
from tests.conftest import AUTH_HEADER
from tests.server_test_helpers import create_environment

_TERMS = "/api/v1/environments/dev/terms"

_GLOSSARY = {
    "version": 13,
    "main_language": "en",
    "glossary": {
        "gross_revenue": {
            "name": "Gross revenue",
            "definition": "Before returns.",
            "owner": "finance",
            "related": ["net_revenue"],
        },
        "net_revenue": {
            "name": "Net revenue",
            "definition": "Revenue after returns.",
            "owner": "finance",
            "links": {
                "silver.orders": ["order_id"],
                "silver.payroll": ["salary"],
                "silver.gone": [],
            },
            "related": ["gross_revenue"],
            "translations": {"fr": {"name": "CA net"}},
        },
    },
}


def _table(client: TestClient, name: str, *columns: str) -> None:
    """Create `silver.<name>` whose columns are all strings but the first.

    Parameters
    ----------
    client
        The test client.
    name
        The table's name.
    *columns
        Its column names.
    """
    created = client.post(
        "/api/v1/environments/dev/namespaces/silver/tables",
        headers=AUTH_HEADER,
        json={
            "name": name,
            "schema": [
                {"name": c, "type": "long" if i == 0 else "string", "nullable": True}
                for i, c in enumerate(columns)
            ],
        },
    )
    assert created.status_code == 201, created.text


def _person(client: TestClient, name: str, *, table: str) -> dict[str, str]:
    """Create a user who may read the environment and one table, and return its headers.

    Parameters
    ----------
    client
        The test client.
    name
        The principal's name.
    table
        The table `silver.<table>` the user may read.

    Returns
    -------
    dict
        The `Authorization` header.
    """
    client.post("/api/v1/principals", headers=AUTH_HEADER, json={"name": name, "type": "USER"})
    scopes = (
        {"action": "READ_ENVIRONMENT", "kind": "ENVIRONMENT", "environment": "dev"},
        {
            "role": "VIEWER",
            "kind": "TABLE",
            "environment": "dev",
            "namespace": "silver",
            "table": table,
        },
    )
    for scope in scopes:
        granted = client.post(
            "/api/v1/grants", headers=AUTH_HEADER, json={"principal": name, **scope}
        )
        assert granted.status_code < 300, granted.text
    issued = client.post(
        f"/api/v1/principals/{name}/tokens", headers=AUTH_HEADER, json={"name": name}
    )
    return {"Authorization": f"Bearer {issued.json()['token']}"}


def _setup(client: TestClient) -> None:
    """Create `dev`, `silver.orders`, `silver.payroll`, the repository `shop` and its glossary.

    Parameters
    ----------
    client
        The test client.
    """
    create_environment(client)
    _table(client, "orders", "order_id", "status")
    _table(client, "payroll", "employee", "salary")
    with Session(client.app.state.engine) as session:
        env = session.scalars(select(Environment.id).where(Environment.name == "dev")).one()
        shop = Repository(
            id=uuid.uuid4(), name="shop", kind=RepositoryKind.HTTPS, url="https://h/shop"
        )
        session.add(shop)
        session.commit()
        write_glossary(session, env, shop.id, _GLOSSARY)
        session.commit()


def test_terms_list_and_get_have_the_documented_shape(client: TestClient) -> None:
    _setup(client)
    page = client.get(_TERMS, headers=AUTH_HEADER, params={"limit": 1})
    assert page.status_code == 200, page.text
    body = page.json()
    assert [t["key"] for t in body["items"]] == ["gross_revenue"] and body["next_cursor"]
    term = client.get(f"{_TERMS}/net_revenue", headers=AUTH_HEADER, params={"lang": "fr"}).json()
    assert {
        k: term[k] for k in ("key", "name", "definition", "owner", "repository", "language")
    } == {
        "key": "net_revenue",
        "name": "CA net",
        "definition": "Revenue after returns.",
        "owner": "finance",
        "repository": "shop",
        "language": "fr",
    }
    assert term["links"] == [
        {"table": "silver.gone", "column": None, "kind": "table", "missing": True},
        {"table": "silver.orders", "column": "order_id", "kind": "column", "missing": False},
        {"table": "silver.payroll", "column": "salary", "kind": "column", "missing": False},
    ]
    assert term["related"] == [{"key": "gross_revenue", "name": "Gross revenue", "missing": False}]
    assert term["related_by"] == ["gross_revenue"]
    assert isinstance(term["id"], str)


def test_a_missing_term_is_404_with_the_next_step_and_a_bad_lang_is_422(
    client: TestClient,
) -> None:
    _setup(client)
    missing = client.get(f"{_TERMS}/nope", headers=AUTH_HEADER)
    assert missing.status_code == 404
    assert "List the terms with: lakana term list --env dev" in missing.text
    assert client.get(_TERMS, headers=AUTH_HEADER, params={"lang": "not a tag"}).status_code == 422


def test_a_reader_of_one_table_sees_its_links_and_not_the_others(client: TestClient) -> None:
    _setup(client)
    alice = _person(client, "alice", table="orders")
    links = client.get(f"{_TERMS}/net_revenue", headers=alice).json()["links"]
    assert [link["table"] for link in links] == ["silver.gone", "silver.orders"]
    bob = _person(client, "bob", table="payroll")
    links = client.get(f"{_TERMS}/net_revenue", headers=bob).json()["links"]
    assert [link["table"] for link in links] == ["silver.gone", "silver.payroll"]
