"""The search route: what it finds, in what order, and what a caller may not see."""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from lakana_core.catalog.descriptions import sync_project_descriptions
from lakana_core.models import DeclaredMeasure, Environment, Namespace, Principal, Table
from tests.conftest import AUTH_HEADER
from tests.server_test_helpers import create_environment

_SEARCH = "/api/v1/environments/dev/search"


def _table(client: TestClient, namespace: str, name: str, *columns: str) -> None:
    """Create a table whose columns are all strings but the first.

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
            "schema": [
                {"name": c, "type": "long" if i == 0 else "string", "nullable": True}
                for i, c in enumerate(columns)
            ],
        },
    )
    assert created.status_code == 201, created.text


def _person(client: TestClient, name: str, *, table: str | None) -> dict[str, str]:
    """Create a user who may read one table, or the whole environment, and return its headers.

    Parameters
    ----------
    client
        The test client.
    name
        The principal's name.
    table
        The table `silver.<table>` the user may read; the whole environment when `None`.

    Returns
    -------
    dict
        The `Authorization` header.
    """
    client.post("/api/v1/principals", headers=AUTH_HEADER, json={"name": name, "type": "USER"})
    scope = (
        {"kind": "ENVIRONMENT", "environment": "dev"}
        if table is None
        else {"kind": "TABLE", "environment": "dev", "namespace": "silver", "table": table}
    )
    granted = client.post(
        "/api/v1/grants", headers=AUTH_HEADER, json={"principal": name, "role": "VIEWER", **scope}
    )
    assert granted.status_code < 300, granted.text
    issued = client.post(
        f"/api/v1/principals/{name}/tokens", headers=AUTH_HEADER, json={"name": name}
    )
    return {"Authorization": f"Bearer {issued.json()['token']}"}


def _setup(client: TestClient) -> None:
    """Create `dev` with `silver.orders` and `silver.payroll`, each with a description.

    Parameters
    ----------
    client
        The test client.
    """
    create_environment(client)
    _table(client, "silver", "orders", "order_id", "status")
    _table(client, "silver", "payroll", "employee", "salary")
    with Session(client.app.state.engine) as session:
        for row in session.scalars(select(Table)):
            row.description_project = f"About {row.name}: salary and stuff"
        session.commit()


def test_a_search_returns_the_documented_shape_with_tables_before_columns(
    client: TestClient,
) -> None:
    _setup(client)

    response = client.get(_SEARCH, headers=AUTH_HEADER, params={"q": "order", "limit": 5})

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["limit"], body["offset"], body["has_more"]) == (5, 0, False)
    assert [(h["kind"], h["name"], h["matched"]) for h in body["results"]] == [
        ("table", "silver.orders", "name"),
        ("column", "silver.orders.order_id", "name"),
    ]
    table, column = body["results"]
    assert (table["namespace"], table["table"], table["table_kind"]) == (
        "silver",
        "orders",
        "TABLE",
    )
    assert (column["column"], column["column_type"], column["snippet"]) == (
        "order_id",
        "long",
        None,
    )


def test_a_description_match_carries_a_snippet(client: TestClient) -> None:
    _setup(client)

    hits = client.get(_SEARCH, headers=AUTH_HEADER, params={"q": "stuff", "kind": "table"}).json()[
        "results"
    ]

    assert [(h["name"], h["matched"], h["snippet"]) for h in hits] == [
        ("silver.orders", "description", "About orders: salary and stuff"),
        ("silver.payroll", "description", "About payroll: salary and stuff"),
    ]


def test_a_caller_sees_only_what_they_may_read_and_nothing_leaks_through_a_snippet(
    client: TestClient,
) -> None:
    _setup(client)
    alice = _person(client, "alice", table="orders")
    bob = _person(client, "bob", table=None)

    narrow = client.get(_SEARCH, headers=alice, params={"q": "salary"}).json()["results"]
    wide = client.get(_SEARCH, headers=bob, params={"q": "salary"}).json()["results"]

    assert [h["name"] for h in narrow] == ["silver.orders"]
    assert narrow[0]["snippet"] == "About orders: salary and stuff"
    assert [h["name"] for h in wide] == [
        "silver.payroll.salary",
        "silver.orders",
        "silver.payroll",
    ]
    for query in ("payroll", "employee"):
        assert client.get(_SEARCH, headers=alice, params={"q": query}).json()["results"] == []


def test_paging_reports_has_more(client: TestClient) -> None:
    _setup(client)

    first = client.get(_SEARCH, headers=AUTH_HEADER, params={"q": "o", "limit": 2}).json()
    rest = client.get(
        _SEARCH, headers=AUTH_HEADER, params={"q": "o", "limit": 2, "offset": 2}
    ).json()

    assert first["has_more"] is True and len(first["results"]) == 2
    assert rest["offset"] == 2 and len(rest["results"]) == 2
    assert {h["name"] for h in first["results"]}.isdisjoint(h["name"] for h in rest["results"])


def test_principals_appear_for_an_admin_and_not_for_a_viewer(client: TestClient) -> None:
    _setup(client)
    bob = _person(client, "bob", table=None)

    admin = client.get(_SEARCH, headers=AUTH_HEADER, params={"q": "bob"}).json()["results"]
    viewer = client.get(_SEARCH, headers=bob, params={"q": "bob"}).json()["results"]

    assert [(h["kind"], h["principal"], h["principal_type"]) for h in admin] == [
        ("principal", "bob", "USER")
    ]
    assert viewer == []


def test_a_bad_query_page_or_kind_is_422_with_the_next_step(client: TestClient) -> None:
    _setup(client)

    for params, words in (
        ({"q": "   "}, "between 1 and 200 characters"),
        ({"q": "x" * 201}, "between 1 and 200 characters"),
        ({"q": "o", "limit": 0}, "limit must be between 1 and 100"),
        ({"q": "o", "limit": 101}, "limit must be between 1 and 100"),
        ({"q": "o", "offset": -1}, "offset must be 0 or more"),
        ({"q": "o", "kind": "nope"}, "table"),
        ({}, "q"),
    ):
        response = client.get(_SEARCH, headers=AUTH_HEADER, params=params)
        assert response.status_code == 422, (params, response.text)
        assert words in response.text, (params, response.text)


def test_an_unknown_environment_is_404_and_a_missing_token_is_401(client: TestClient) -> None:
    _setup(client)

    missing = client.get("/api/v1/environments/nope/search", headers=AUTH_HEADER, params={"q": "o"})

    assert missing.status_code == 404, missing.text
    assert "nope" in missing.text
    assert client.get(_SEARCH, params={"q": "o"}).status_code == 401


def _declare(client: TestClient, table: str, kind: str, grain: str) -> None:
    """Write a declared kind and grain on `silver.<table>`, as a deploy does.

    Parameters
    ----------
    client
        The test client.
    table
        The table's name.
    kind
        The declared kind.
    grain
        The declared grain.
    """
    with Session(client.app.state.engine) as session:
        row = session.scalars(select(Table).where(Table.name == table)).one()
        row.model_kind = kind
        row.model_grain = grain
        session.commit()


def test_model_kind_filters_and_each_hit_carries_its_kind(client: TestClient) -> None:
    _setup(client)
    _declare(client, "orders", "dimension", "One row per order.")
    _declare(client, "payroll", "fact", "One row per order line.")

    hits = client.get(
        _SEARCH, headers=AUTH_HEADER, params={"q": "order", "model_kind": "dimension"}
    ).json()["results"]
    assert [(h["name"], h["model_kind"]) for h in hits] == [("silver.orders", "dimension")]
    columns = client.get(_SEARCH, headers=AUTH_HEADER, params={"q": "order_id"}).json()["results"]
    assert [h["model_kind"] for h in columns] == [None]
    refused = client.get(_SEARCH, headers=AUTH_HEADER, params={"q": "x", "model_kind": "cube"})
    assert refused.status_code == 422


def test_a_grain_of_a_table_the_caller_may_not_read_is_not_found(client: TestClient) -> None:
    _setup(client)
    _declare(client, "payroll", "fact", "One row per secret bonus.")
    alice = _person(client, "alice", table="orders")

    assert client.get(_SEARCH, headers=alice, params={"q": "secret bonus"}).json()["results"] == []
    hits = client.get(_SEARCH, headers=AUTH_HEADER, params={"q": "secret bonus"}).json()["results"]
    assert [(h["name"], h["matched"], h["snippet"]) for h in hits] == [
        ("silver.payroll", "grain", "One row per secret bonus.")
    ]


def test_a_measure_label_matches_through_rest_and_only_for_a_reader(client: TestClient) -> None:
    _setup(client)
    with Session(client.app.state.engine) as session:
        payroll = session.scalars(select(Table).where(Table.name == "payroll")).one()
        namespace = session.get(Namespace, payroll.namespace_id)
        assert namespace is not None
        session.add(
            DeclaredMeasure(
                id=uuid.uuid4(),
                environment_id=namespace.environment_id,
                table_id=payroll.id,
                column="salary",
                aggregation="sum",
                additivity="additive",
                not_over=[],
                currency="EUR",
                label="Gross pay",
                description=None,
            )
        )
        session.commit()
    alice = _person(client, "alice", table="orders")

    hits = client.get(_SEARCH, headers=AUTH_HEADER, params={"q": "gross pay"}).json()["results"]
    assert [(h["name"], h["matched"], h["snippet"]) for h in hits] == [
        ("silver.payroll.salary", "measure", "Gross pay")
    ]
    assert client.get(_SEARCH, headers=alice, params={"q": "gross pay"}).json()["results"] == []


def _translated(client: TestClient) -> None:
    """Create `gold.t (id)` labelled `Sales`, in French `Ventes`, as a deploy writes it.

    Parameters
    ----------
    client
        The test client.
    """
    create_environment(client)
    created = client.post(
        "/api/v1/environments/dev/namespaces/gold/tables",
        headers=AUTH_HEADER,
        json={"name": "t", "schema": [{"name": "id", "type": "long", "nullable": False}]},
    )
    assert created.status_code == 201, created.text
    with Session(client.app.state.engine) as session:
        env = session.scalars(select(Environment.id).where(Environment.name == "dev")).one()
        sync_project_descriptions(
            session,
            env,
            {
                "version": 11,
                "main_language": "en",
                "assets": [
                    {
                        "table": "gold.t",
                        "metadata": {"label": "Sales", "translations": {"fr": {"label": "Ventes"}}},
                    }
                ],
            },
        )
        session.commit()


def test_a_label_hit_through_rest_says_its_language(client: TestClient) -> None:
    """`matched: "label"` must be in `SearchHitOut`'s Literal, or this answers 500."""
    _translated(client)
    response = client.get(_SEARCH, params={"q": "ventes", "lang": "fr"}, headers=AUTH_HEADER)
    assert response.status_code == 200, response.text
    (hit,) = response.json()["results"]
    assert (hit["name"], hit["matched"], hit["language"]) == ("gold.t", "label", "fr")
    plain = client.get(_SEARCH, params={"q": "ventes"}, headers=AUTH_HEADER)
    assert plain.json()["results"] == []
    (english,) = client.get(_SEARCH, params={"q": "sales"}, headers=AUTH_HEADER).json()["results"]
    assert (english["matched"], english["language"]) == ("label", "en")


def test_search_reads_the_profile_and_accept_language_like_table_get(client: TestClient) -> None:
    _translated(client)
    by_header = client.get(
        _SEARCH, params={"q": "ventes"}, headers={**AUTH_HEADER, "Accept-Language": "fr-CH"}
    )
    assert [h["language"] for h in by_header.json()["results"]] == ["fr"]
    with Session(client.app.state.engine) as session:
        session.execute(update(Principal).values(language="fr"))
        session.commit()
    by_profile = client.get(_SEARCH, params={"q": "ventes"}, headers=AUTH_HEADER)
    assert [h["language"] for h in by_profile.json()["results"]] == ["fr"]


def test_an_invalid_lang_on_search_is_422_with_the_tag_text(client: TestClient) -> None:
    _translated(client)
    response = client.get(_SEARCH, params={"q": "ventes", "lang": " de_DE "}, headers=AUTH_HEADER)
    assert response.status_code == 422
    assert response.json()["detail"] == (
        "lang: `de_DE` is not a language tag Lakana takes: a language, then optionally a script "
        "and a region (BCP 47), such as en, de, fr-CH or zh-Hant; ask for a language such as de, "
        "or leave lang out"
    )
