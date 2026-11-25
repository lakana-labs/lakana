"""Tests for the namespaces platform API."""

import uuid
from datetime import date, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.models import Environment, Namespace, Table
from lakana_core.models.table import TABLE_NAME_MAX, TableType
from lakana_core.models.namespace import NAMESPACE_PATH_MAX
from lakana_core.pagination import NAMESPACE_CURSOR_MAX_LENGTH, TABLE_CURSOR_MAX_LENGTH
from lakana_server.api.platform.listing import NEXT_CURSOR_HEADER
from tests.conftest import AUTH_HEADER


def _create_env(client: TestClient, storage_root: str, name: str = "dev") -> None:
    """Create an environment, ignoring the response body.

    Parameters
    ----------
    client
        The test client.
    storage_root
        The environment's storage root URI.
    name
        Environment name.
    """
    response = client.post(
        "/api/v1/environments",
        headers=AUTH_HEADER,
        json={
            "name": name,
            "storage": {"scheme": "file", "root": storage_root, "options": {}},
        },
    )
    assert response.status_code == 201, response.text


def test_create_then_get_round_trips(client: TestClient, storage_root: str) -> None:
    """A created namespace can be fetched back by its dotted path."""
    _create_env(client, storage_root)
    created = client.post(
        "/api/v1/environments/dev/namespaces",
        headers=AUTH_HEADER,
        json={"path": "gold.sales", "properties": {"owner": "analytics"}},
    )
    assert created.status_code == 201, created.text
    fetched = client.get("/api/v1/environments/dev/namespaces/gold.sales", headers=AUTH_HEADER)
    assert fetched.status_code == 200
    assert fetched.json() == {"path": "gold.sales", "properties": {"owner": "analytics"}}


def test_get_unknown_is_404(client: TestClient, storage_root: str) -> None:
    """A missing namespace is a 404."""
    _create_env(client, storage_root)
    response = client.get("/api/v1/environments/dev/namespaces/nope", headers=AUTH_HEADER)
    assert response.status_code == 404


def test_list_returns_every_namespace_in_the_environment(
    client: TestClient, storage_root: str
) -> None:
    """Unfiltered listing, matching the environments router's own carry-forward.

    `create_environment` auto-creates `bronze`/`silver`/`gold`, so the
    listing includes those alongside whatever this test creates. The
    created namespaces are named outside that set (`"widgets"`,
    `"widgets.sales"`, `"raw"`) so each create call actually succeeds
    instead of silently 409-ing against the scaffold.
    """
    _create_env(client, storage_root)
    for path in ("widgets", "widgets.sales", "raw"):
        response = client.post(
            "/api/v1/environments/dev/namespaces", headers=AUTH_HEADER, json={"path": path}
        )
        assert response.status_code == 201, response.text
    response = client.get("/api/v1/environments/dev/namespaces", headers=AUTH_HEADER)
    assert response.status_code == 200
    assert {ns["path"] for ns in response.json()} == {
        "bronze",
        "silver",
        "gold",
        "widgets",
        "widgets.sales",
        "raw",
    }


def test_drop_removes_the_namespace(client: TestClient, storage_root: str) -> None:
    """A dropped namespace is gone from a subsequent get."""
    _create_env(client, storage_root)
    client.post("/api/v1/environments/dev/namespaces", headers=AUTH_HEADER, json={"path": "gold"})
    response = client.delete("/api/v1/environments/dev/namespaces/gold", headers=AUTH_HEADER)
    assert response.status_code == 204
    assert (
        client.get("/api/v1/environments/dev/namespaces/gold", headers=AUTH_HEADER).status_code
        == 404
    )


def test_drop_a_namespace_containing_tables_is_a_conflict(
    client: TestClient, storage_root: str
) -> None:
    """The ConflictError from CatalogService surfaces as 409, not 500.

    The platform API has no way to create a table; the Iceberg router does.
    """
    _create_env(client, storage_root)
    client.post("/api/v1/environments/dev/namespaces", headers=AUTH_HEADER, json={"path": "gold"})
    client.post(
        "/v1/dev/namespaces/gold/tables",
        headers=AUTH_HEADER,
        json={
            "name": "sales",
            "schema": {
                "type": "struct",
                "schema-id": 0,
                "fields": [{"id": 1, "name": "id", "type": "long", "required": True}],
            },
            "properties": {},
        },
    )
    response = client.delete("/api/v1/environments/dev/namespaces/gold", headers=AUTH_HEADER)
    assert response.status_code == 409


def test_list_filters_by_parent_pages_and_takes_depth(
    client: TestClient, storage_root: str
) -> None:
    """`parent` keeps every depth below it, `depth=1` the direct children, `limit` pages."""
    _create_env(client, storage_root)
    for path in ("a", "a.b", "a.b.c", "ab"):
        response = client.post(
            "/api/v1/environments/dev/namespaces", headers=AUTH_HEADER, json={"path": path}
        )
        assert response.status_code == 201, response.text
    url = "/api/v1/environments/dev/namespaces"

    first = client.get(f"{url}?parent=a&limit=1", headers=AUTH_HEADER)

    assert first.status_code == 200, first.text
    assert [ns["path"] for ns in first.json()] == ["a.b"]
    second = client.get(
        f"{url}?parent=a&limit=1&cursor={first.headers['x-next-cursor']}", headers=AUTH_HEADER
    )
    assert [ns["path"] for ns in second.json()] == ["a.b.c"]
    assert "x-next-cursor" not in second.headers
    direct = client.get(f"{url}?parent=a&depth=1", headers=AUTH_HEADER)
    assert [ns["path"] for ns in direct.json()] == ["a.b"]
    top = client.get(f"{url}?depth=1", headers=AUTH_HEADER)
    assert [ns["path"] for ns in top.json()] == ["a", "ab", "bronze", "gold", "silver"]
    assert client.get(f"{url}?depth=2", headers=AUTH_HEADER).status_code == 422
    assert client.get(f"{url}?cursor=forged", headers=AUTH_HEADER).status_code == 422
    assert client.get(f"{url}?parent=a%00", headers=AUTH_HEADER).status_code == 422


def test_the_list_pages_past_the_longest_path_in_wide_letters_and_in_ascii(
    client: TestClient, storage_root: str
) -> None:
    """One namespace a page, each path as long as the catalog stores: no cursor is refused (#804).

    The cursor of a page holds the last path, so a path of 1024 four-byte characters made a
    cursor far past the old 512 and the next page answered 422 `string_too_long`.
    """
    _create_env(client, storage_root)
    paths = ["\U0001d538" * NAMESPACE_PATH_MAX, "ж" * NAMESPACE_PATH_MAX, "a" * NAMESPACE_PATH_MAX]
    with Session(client.app.state.engine) as session:
        env_id = session.scalars(select(Environment.id).where(Environment.name == "dev")).one()
        for path in paths:
            session.add(Namespace(id=uuid.uuid4(), environment_id=env_id, path=path))
        session.commit()
    seen: list[str] = []
    params: dict[str, str | int] = {"limit": 1}
    while True:
        response = client.get(
            "/api/v1/environments/dev/namespaces", headers=AUTH_HEADER, params=params
        )
        assert response.status_code == 200, response.text
        seen += [item["path"] for item in response.json()]
        cursor = response.headers.get(NEXT_CURSOR_HEADER)
        if cursor is None:
            break
        params["cursor"] = cursor
    assert set(paths) <= set(seen)
    assert len(seen) == len(set(seen))


def test_the_lists_of_paths_take_a_cursor_as_long_as_the_longest_they_issue(
    client: TestClient,
) -> None:
    """The routes whose cursor holds a namespace path or a table name take a long `cursor`."""
    paths = client.get("/openapi.json").json()["paths"]
    expected = {
        "/api/v1/environments/{env}/namespaces": NAMESPACE_CURSOR_MAX_LENGTH,
        "/api/v1/environments/{env}/artifacts": TABLE_CURSOR_MAX_LENGTH,
        "/api/v1/environments/{env}/expiring-tables": TABLE_CURSOR_MAX_LENGTH,
        "/api/v1/environments/{env}/orphans": TABLE_CURSOR_MAX_LENGTH,
    }
    for route, limit in expected.items():
        cursor = next(p for p in paths[route]["get"]["parameters"] if p["name"] == "cursor")
        assert cursor["schema"]["anyOf"][0]["maxLength"] == limit, route


def _page_through(client: TestClient, url: str, key: str) -> list[str]:
    """Read every page of a list of one row a page, following the cursor header.

    Parameters
    ----------
    client
        The test client.
    url
        The list's route.
    key
        The field that names a row.

    Returns
    -------
    list of str
        The rows' names, in the order of the pages.
    """
    seen: list[str] = []
    params: dict[str, str | int] = {"limit": 1}
    while True:
        response = client.get(url, headers=AUTH_HEADER, params=params)
        assert response.status_code == 200, response.text
        seen += [item[key] for item in response.json()]
        cursor = response.headers.get(NEXT_CURSOR_HEADER)
        if cursor is None:
            return seen
        params["cursor"] = cursor


def test_the_lists_of_artifacts_and_expiring_tables_page_past_the_longest_key(
    client: TestClient, storage_root: str
) -> None:
    """A namespace path of 1024 letters and a table name of 255 do not stop either list (#804)."""
    _create_env(client, storage_root)
    letters = ("\U0001d538", "ж", "a")
    with Session(client.app.state.engine) as session:
        env_id = session.scalars(select(Environment.id).where(Environment.name == "dev")).one()
        for letter in letters:
            namespace = Namespace(
                id=uuid.uuid4(), environment_id=env_id, path=letter * NAMESPACE_PATH_MAX
            )
            session.add(namespace)
            session.flush()
            session.add(
                Table(
                    id=uuid.uuid4(),
                    namespace_id=namespace.id,
                    name=letter * TABLE_NAME_MAX,
                    table_type=TableType.ARTIFACT,
                )
            )
            session.add(
                Table(
                    id=uuid.uuid4(),
                    namespace_id=namespace.id,
                    name=letter * (TABLE_NAME_MAX - 1),
                    table_type=TableType.ICEBERG,
                    retention_on=date.today() + timedelta(days=1),
                )
            )
        session.commit()
    base = "/api/v1/environments/dev"
    artifacts = _page_through(client, f"{base}/artifacts", "name")
    assert set(artifacts) == {letter * TABLE_NAME_MAX for letter in letters}
    expiring = _page_through(client, f"{base}/expiring-tables", "name")
    assert set(expiring) == {letter * (TABLE_NAME_MAX - 1) for letter in letters}
