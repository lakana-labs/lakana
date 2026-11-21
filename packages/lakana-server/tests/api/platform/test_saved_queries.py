"""Tests for the saved query API (rulings S2 to S6 of the saved queries plan)."""

import uuid

from fastapi.testclient import TestClient

from lakana_core.catalog.saved_queries import TASK_TOKEN_REFUSED
from tests.conftest import AUTH_HEADER
from tests.server_test_helpers import create_environment, person

_ENV_URL = "/api/v1/environments/dev/saved-queries"
_ALL_URL = "/api/v1/saved-queries"
_OUT_KEYS = {
    "id",
    "environment",
    "name",
    "sql",
    "visibility",
    "owner",
    "former_owner",
    "actions",
    "created_at",
    "updated_at",
}


def _analyst(client: TestClient, name: str = "bob") -> dict[str, str]:
    """Create an environment `dev` and an analyst on it, and return the analyst's header.

    Parameters
    ----------
    client
        Test client for the API.
    name
        The analyst's name.

    Returns
    -------
    dict of str to str
        The analyst's Authorization header.
    """
    create_environment(client, "dev")
    return person(client, name, "ANALYST")


def _save(
    client: TestClient,
    headers: dict[str, str],
    name: str = "Revenue",
    sql: str = "SELECT 1",
    visibility: str = "private",
) -> dict[str, object]:
    """Create a saved query in `dev` and return its answer.

    Parameters
    ----------
    client
        Test client for the API.
    headers
        The owner's Authorization header.
    name
        The saved query's name.
    sql
        Its SQL.
    visibility
        ``private`` or ``everyone``.

    Returns
    -------
    dict of str to object
        The 201 body.
    """
    created = client.post(
        _ENV_URL, headers=headers, json={"name": name, "sql": sql, "visibility": visibility}
    )
    assert created.status_code == 201, created.text
    return created.json()


def test_create_answers_201_with_the_saved_query(client: TestClient) -> None:
    """A created saved query is answered with its fields, its SQL and both actions."""
    bob = _analyst(client)

    created = client.post(_ENV_URL, headers=bob, json={"name": "  Revenue ", "sql": "SELECT 1"})

    assert created.status_code == 201
    body = created.json()
    assert set(body) == _OUT_KEYS
    assert (body["name"], body["sql"], body["visibility"]) == ("Revenue", "SELECT 1", "private")
    assert (body["environment"], body["owner"], body["former_owner"]) == ("dev", "bob", False)
    assert body["actions"] == ["update", "delete"]
    uuid.UUID(body["id"])


def test_create_with_sql_longer_than_the_statement_limit_is_422(client: TestClient) -> None:
    """The routes check the SQL's length as a submitted query's is checked, on create and update."""
    bob = _analyst(client)
    saved = _save(client, bob)
    too_long = "SELECT 1 " + "-" * 100_000

    created = client.post(_ENV_URL, headers=bob, json={"name": "Long", "sql": too_long})
    updated = client.put(
        f"{_ENV_URL}/{saved['id']}",
        headers=bob,
        json={"name": "Revenue", "sql": too_long, "visibility": "private"},
    )

    assert created.status_code == updated.status_code == 422
    assert "LAKANA_QUERY_LIMITS__MAX_STATEMENT_CHARS" in created.json()["detail"]
    assert client.get(f"{_ENV_URL}/{saved['id']}", headers=bob).json()["sql"] == "SELECT 1"


def test_a_name_is_checked_after_stripping_with_the_s4_sentence(client: TestClient) -> None:
    """A 200-character name padded with blanks is kept stripped; 201 characters or none is 422."""
    bob = _analyst(client)
    longest = "n" * 200
    sentence = "A saved query's name is one line of 1 to 200 visible characters."

    padded = client.post(
        _ENV_URL, headers=bob, json={"name": f"   {longest}   ", "sql": "SELECT 1"}
    )
    too_long = client.post(_ENV_URL, headers=bob, json={"name": "n" * 201, "sql": "SELECT 1"})
    empty = client.post(_ENV_URL, headers=bob, json={"name": "", "sql": "SELECT 1"})
    blank = client.post(_ENV_URL, headers=bob, json={"name": "   ", "sql": "SELECT 1"})

    assert padded.status_code == 201, padded.text
    assert padded.json()["name"] == longest
    assert too_long.status_code == empty.status_code == blank.status_code == 422
    assert too_long.json()["detail"] == empty.json()["detail"] == blank.json()["detail"] == sentence


def test_a_put_without_visibility_is_422_and_changes_nothing(client: TestClient) -> None:
    """A PUT replaces every field, so a missing `visibility` never makes a shared query private."""
    bob = _analyst(client)
    shared = _save(client, bob, visibility="everyone")
    url = f"{_ENV_URL}/{shared['id']}"

    put = client.put(url, headers=bob, json={"name": "Revenue", "sql": "SELECT 2"})

    assert put.status_code == 422
    assert client.get(url, headers=bob).json() == shared


def test_another_persons_private_query_is_404_with_the_sentence(client: TestClient) -> None:
    """Someone who may query the environment gets the 404 of a missing query for a private one."""
    bob = _analyst(client)
    carol = person(client, "carol", "ANALYST")
    saved = _save(client, bob)

    hidden = client.get(f"{_ENV_URL}/{saved['id']}", headers=carol)

    assert hidden.status_code == 404
    assert hidden.json()["detail"] == f"No saved query {saved['id']} in 'dev'."
    assert client.get(_ENV_URL, headers=carol).json()["items"] == []


def test_a_duplicate_name_is_409_naming_the_existing_id(client: TestClient) -> None:
    """Saving a second query under one's own name is a 409 that names the first."""
    bob = _analyst(client)
    first = _save(client, bob)
    other = _save(client, bob, name="Other")

    again = client.post(_ENV_URL, headers=bob, json={"name": "Revenue", "sql": "SELECT 2"})
    renamed = client.put(
        f"{_ENV_URL}/{other['id']}",
        headers=bob,
        json={"name": "Revenue", "sql": "SELECT 2", "visibility": "private"},
    )

    assert again.status_code == renamed.status_code == 409
    assert again.json()["detail"] == (
        f"You already have a saved query named 'Revenue' in 'dev' ({first['id']}); "
        "choose another name, or update that one."
    )


def test_get_put_delete_round_trip_as_the_owner(client: TestClient) -> None:
    """The owner reads, replaces and deletes a saved query by its id."""
    bob = _analyst(client)
    saved = _save(client, bob)
    url = f"{_ENV_URL}/{saved['id']}"

    assert client.get(url, headers=bob).json() == saved
    put = client.put(
        url, headers=bob, json={"name": "Revenue 2", "sql": "SELECT 2", "visibility": "everyone"}
    )
    assert put.status_code == 200, put.text
    assert (put.json()["name"], put.json()["sql"], put.json()["visibility"]) == (
        "Revenue 2",
        "SELECT 2",
        "everyone",
    )
    assert put.json()["id"] == saved["id"]
    assert client.get(url, headers=bob).json() == put.json()

    deleted = client.delete(url, headers=bob)
    assert deleted.status_code == 204
    assert deleted.content == b""
    assert client.get(url, headers=bob).status_code == 404
    assert client.get(_ENV_URL, headers=bob).json() == {"items": [], "next_cursor": None}


def test_put_by_someone_else_is_403_with_the_sentence(client: TestClient) -> None:
    """A person who sees a shared query but does not own it may not change or delete it."""
    bob = _analyst(client)
    carol = person(client, "carol", "ANALYST")
    shared = _save(client, bob, visibility="everyone")
    url = f"{_ENV_URL}/{shared['id']}"

    put = client.put(
        url, headers=carol, json={"name": "Mine", "sql": "SELECT 2", "visibility": "everyone"}
    )
    delete = client.delete(url, headers=carol)

    assert put.status_code == delete.status_code == 403
    assert put.json()["detail"] == (
        "Only the owner of a saved query can change it; save a copy under your own name instead."
    )
    assert delete.json()["detail"] == (
        "Only the owner of a saved query or an administrator can delete it."
    )
    assert client.get(url, headers=carol).json()["actions"] == []
    assert client.get(url, headers=bob).json()["sql"] == "SELECT 1"


def test_an_admin_reads_a_private_query_without_its_sql_key(client: TestClient) -> None:
    """An admin without `READ_QUERY_TEXT` sees the name and owner, and no `sql` key at all."""
    bob = _analyst(client)
    saved = _save(client, bob)

    one = client.get(f"{_ENV_URL}/{saved['id']}", headers=AUTH_HEADER)
    in_environment = client.get(_ENV_URL, headers=AUTH_HEADER)
    everywhere = client.get(_ALL_URL, headers=AUTH_HEADER)

    assert one.status_code == in_environment.status_code == everywhere.status_code == 200
    bodies = [one.json(), in_environment.json()["items"][0], everywhere.json()["items"][0]]
    for body in bodies:
        assert "sql" not in body
        assert (body["name"], body["owner"], body["environment"]) == ("Revenue", "bob", "dev")
        assert body["actions"] == ["delete"]
        assert set(body) == _OUT_KEYS - {"sql"}


def test_the_lists_show_no_sql_even_to_the_owner_and_reading_one_does(
    client: TestClient,
) -> None:
    """A list item has no `sql` key, for its owner too; `GET` of the saved query has it (#692)."""
    bob = _analyst(client)
    saved = _save(client, bob, sql="SELECT 42")

    lists = [client.get(url, headers=bob) for url in (_ENV_URL, _ALL_URL)]

    for listed in lists:
        assert listed.status_code == 200
        (item,) = listed.json()["items"]
        assert set(item) == _OUT_KEYS - {"sql"}
        assert item["actions"] == ["update", "delete"]
    assert client.get(f"{_ENV_URL}/{saved['id']}", headers=bob).json()["sql"] == "SELECT 42"


def test_an_admin_deletes_a_private_query_and_it_is_gone(client: TestClient) -> None:
    """An admin may delete someone's private query, and then the owner no longer sees it."""
    bob = _analyst(client)
    saved = _save(client, bob)
    url = f"{_ENV_URL}/{saved['id']}"

    assert (
        client.put(
            url, headers=AUTH_HEADER, json={"name": "X", "sql": "SELECT 2", "visibility": "private"}
        ).status_code
        == 403
    )
    assert client.delete(url, headers=AUTH_HEADER).status_code == 204

    assert client.get(url, headers=bob).status_code == 404
    assert client.get(url, headers=AUTH_HEADER).status_code == 404
    assert client.get(_ALL_URL, headers=bob).json()["items"] == []


def test_the_cross_environment_list_answers_items_and_next_cursor_with_environments(
    client: TestClient,
) -> None:
    """One cursor pages the saved queries of every environment, each naming its environment."""
    bob = _analyst(client)
    create_environment(client, "prod")
    client.post(
        "/api/v1/grants",
        headers=AUTH_HEADER,
        json={"principal": "bob", "role": "ANALYST", "kind": "ENVIRONMENT", "environment": "prod"},
    )
    _save(client, bob, name="b")
    _save(client, bob, name="a")
    client.post(
        "/api/v1/environments/prod/saved-queries",
        headers=bob,
        json={"name": "c", "sql": "SELECT 3"},
    )

    first = client.get(_ALL_URL, params={"limit": 2}, headers=bob)
    assert first.status_code == 200
    assert set(first.json()) == {"items", "next_cursor"}
    assert [(i["name"], i["environment"]) for i in first.json()["items"]] == [
        ("a", "dev"),
        ("b", "dev"),
    ]
    second = client.get(
        _ALL_URL, params={"limit": 2, "cursor": first.json()["next_cursor"]}, headers=bob
    )
    assert [(i["name"], i["environment"]) for i in second.json()["items"]] == [("c", "prod")]
    assert second.json()["next_cursor"] is None

    only_prod = client.get(_ALL_URL, params={"environment": "prod"}, headers=bob)
    assert [i["name"] for i in only_prod.json()["items"]] == ["c"]
    assert client.get(_ALL_URL, params={"limit": 0}, headers=bob).status_code == 422
    assert client.get(_ALL_URL, params={"limit": 201}, headers=bob).status_code == 422


def test_the_cross_environment_list_with_an_unknown_environment_is_404(
    client: TestClient,
) -> None:
    """Naming an environment that does not exist is a 404, as for the per-environment list."""
    bob = _analyst(client)

    named = client.get(_ALL_URL, params={"environment": "nowhere"}, headers=bob)

    assert named.status_code == 404
    assert "nowhere" in named.json()["detail"]
    assert client.get("/api/v1/environments/nowhere/saved-queries", headers=bob).status_code == 404
    assert (
        client.post(
            "/api/v1/environments/nowhere/saved-queries",
            headers=bob,
            json={"name": "x", "sql": "SELECT 1"},
        ).status_code
        == 404
    )


def test_a_task_token_gets_403_on_every_route(client: TestClient) -> None:
    """A task's token may neither read nor change saved queries, whoever it acts as."""
    _analyst(client)
    saved = _save(client, AUTH_HEADER, sql="SELECT 1")
    minted = client.post(
        "/api/v1/task-tokens",
        headers=AUTH_HEADER,
        json={
            "principal": "admin",
            "environment": "dev",
            "asset_scope": ["gold.*"],
            "ttl_seconds": 300,
        },
    )
    assert minted.status_code == 201, minted.text
    token = {"Authorization": f"Bearer {minted.json()['token']}"}
    item = f"{_ENV_URL}/{saved['id']}"
    body = {"name": "x", "sql": "SELECT 1", "visibility": "private"}

    answers = [
        client.get(_ENV_URL, headers=token),
        client.post(_ENV_URL, headers=token, json=body),
        client.get(item, headers=token),
        client.put(item, headers=token, json=body),
        client.delete(item, headers=token),
        client.get(_ALL_URL, headers=token),
    ]

    assert [a.status_code for a in answers] == [403] * 6
    assert {a.json()["detail"] for a in answers} == {TASK_TOKEN_REFUSED}
    assert client.get(item, headers=AUTH_HEADER).status_code == 200


def test_saved_query_routes_need_a_credential(client: TestClient) -> None:
    """Without credentials every route answers 401, whether or not the saved query exists."""
    bob = _analyst(client)
    saved = _save(client, bob)
    item = f"{_ENV_URL}/{saved['id']}"
    body = {"name": "x", "sql": "SELECT 1", "visibility": "private"}

    answers = [
        client.get(_ENV_URL),
        client.post(_ENV_URL, json=body),
        client.get(item),
        client.put(item, json=body),
        client.delete(item),
        client.get(_ALL_URL),
    ]

    assert [a.status_code for a in answers] == [401] * 6
    assert client.get(item, headers=bob).status_code == 200
