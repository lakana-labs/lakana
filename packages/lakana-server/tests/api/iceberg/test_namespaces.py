"""Tests for the Iceberg REST namespace endpoints."""

from fastapi.testclient import TestClient

from lakana_server.settings import Settings
from tests.conftest import AUTH_HEADER


def _create_env_and_prefix(client: TestClient, storage_root: str) -> None:
    """Create the `dev` environment via the platform API.

    Parameters
    ----------
    client
        The test client.
    storage_root
        The environment's storage root URI.
    """
    response = client.post(
        "/api/v1/environments",
        headers=AUTH_HEADER,
        json={
            "name": "dev",
            "storage": {"scheme": "file", "root": storage_root, "options": {}},
        },
    )
    assert response.status_code == 201, response.text


def test_config_returns_empty_defaults_and_overrides(client: TestClient) -> None:
    """No credentials needed; matches what RestCatalog.__init__ requires unconditionally."""
    response = client.get("/v1/config")
    assert response.status_code == 200
    assert response.json() == {"defaults": {}, "overrides": {}}


def test_create_then_list_namespaces(client: TestClient, storage_root: str) -> None:
    """A created namespace appears in the listing, alongside the medallion scaffold.

    `create_environment` auto-creates `bronze`/`silver`/`gold`, so the listing
    after creating one more namespace includes all four, not just the one
    this test created — `"widgets"` is used here precisely so the explicit
    create doesn't collide with the scaffold.
    """
    _create_env_and_prefix(client, storage_root)
    created = client.post(
        "/v1/dev/namespaces",
        headers=AUTH_HEADER,
        json={"namespace": ["widgets"], "properties": {}},
    )
    assert created.status_code == 200, created.text
    listed = client.get("/v1/dev/namespaces", headers=AUTH_HEADER)
    assert listed.status_code == 200
    assert listed.json() == {"namespaces": [["bronze"], ["gold"], ["silver"], ["widgets"]]}


def test_create_the_same_namespace_twice_is_a_conflict(
    client: TestClient, storage_root: str
) -> None:
    """AlreadyExistsError maps to 409 on the Iceberg router too.

    Uses `"widgets"`, not one of the scaffolded `bronze`/`silver`/`gold`
    names, so the *first* create call actually succeeds and it's the
    second one that conflicts -- otherwise both calls would 409 against
    the environment's auto-created scaffold and the test would no longer
    prove what its name claims.
    """
    _create_env_and_prefix(client, storage_root)
    first = client.post(
        "/v1/dev/namespaces",
        headers=AUTH_HEADER,
        json={"namespace": ["widgets"], "properties": {}},
    )
    assert first.status_code == 200, first.text
    response = client.post(
        "/v1/dev/namespaces", headers=AUTH_HEADER, json={"namespace": ["widgets"], "properties": {}}
    )
    assert response.status_code == 409
    assert response.json()["error"]["type"] == "AlreadyExistsError"


def test_a_malformed_body_still_gets_the_iceberg_error_shape(
    client: TestClient, storage_root: str
) -> None:
    """FastAPI's own RequestValidationError must be reshaped too, not just LakanaError.

    A `namespace` of the wrong type is rejected by FastAPI's own body parsing
    before the endpoint ever runs, raising `fastapi.exceptions.RequestValidationError`
    -- a third class, distinct from both `lakana_core.errors.ValidationError` and
    `pydantic.ValidationError`. Without catching it too, this would leak
    FastAPI's default `{"detail": [...]}` body instead of the Iceberg shape.
    """
    _create_env_and_prefix(client, storage_root)
    response = client.post(
        "/v1/dev/namespaces",
        headers=AUTH_HEADER,
        json={"namespace": "not-a-list", "properties": {}},
    )
    assert response.status_code == 422
    body = response.json()
    assert "error" in body
    assert body["error"]["type"] == "RequestValidationError"
    assert "namespace" in body["error"]["message"]


def test_list_namespaces_filters_by_parent(client: TestClient, storage_root: str) -> None:
    """`?parent=gold` returns only gold's direct children, per the Iceberg REST spec.

    `["gold", "sales", "apac"]` is a *grandchild* of `gold` (a child of
    `gold.sales`), not a direct child. `CatalogService.list_namespaces`
    returns every strict descendant, so this pins that the router narrows
    that down to depth-one children only, excluding the grandchild.
    """
    _create_env_and_prefix(client, storage_root)
    for ns in (["gold"], ["gold", "sales"], ["gold", "sales", "apac"], ["silver"]):
        response = client.post("/v1/dev/namespaces", headers=AUTH_HEADER, json={"namespace": ns})
        assert response.status_code in (200, 409), response.text

    response = client.get("/v1/dev/namespaces", headers=AUTH_HEADER, params={"parent": "gold"})

    assert response.status_code == 200, response.text
    assert response.json()["namespaces"] == [["gold", "sales"]]


def test_list_namespaces_decodes_a_multi_level_parent(
    client: TestClient, storage_root: str
) -> None:
    """A parent in the unit-separator form (`gold%1Fsales`) is decoded like the path parameter.

    `["gold", "sales", "eu", "deep"]` is a grandchild of `gold.sales`, so it
    must be excluded too -- this pins filtering at an arbitrary parent
    depth, not just depth one.
    """
    _create_env_and_prefix(client, storage_root)
    for ns in (
        ["gold"],
        ["gold", "sales"],
        ["gold", "sales", "eu"],
        ["gold", "sales", "eu", "deep"],
    ):
        response = client.post("/v1/dev/namespaces", headers=AUTH_HEADER, json={"namespace": ns})
        assert response.status_code in (200, 409), response.text

    response = client.get(
        "/v1/dev/namespaces", headers=AUTH_HEADER, params={"parent": "gold\x1fsales"}
    )

    assert response.status_code == 200, response.text
    assert response.json()["namespaces"] == [["gold", "sales", "eu"]]


def test_list_namespaces_with_unknown_parent_is_404(client: TestClient, storage_root: str) -> None:
    """An unknown `parent` is a 404, matching the Iceberg REST OpenAPI spec.

    The spec documents `NoSuchNamespaceException` for "Namespace provided in
    the `parent` query parameter is not found", and the pinned pyiceberg
    client maps that 404 to `NoSuchNamespaceError`. `CatalogService.list_namespaces`
    itself only pattern-matches stored paths and would silently return an
    empty list for a nonexistent parent, so the router must check the
    parent's own existence first (via `load_namespace`, the same call
    `load_namespace` on a single namespace already uses) rather than let a
    missing parent look identical to an existing one with no children.
    """
    _create_env_and_prefix(client, storage_root)

    response = client.get("/v1/dev/namespaces", headers=AUTH_HEADER, params={"parent": "nope"})

    assert response.status_code == 404, response.text
    assert response.json()["error"]["type"] == "NotFoundError"


def test_list_namespaces_with_an_existing_childless_parent_is_an_empty_list(
    client: TestClient, storage_root: str
) -> None:
    """A `parent` that exists but has no children still returns 200 `[]`.

    Distinguishes "exists, no children" (200 empty) from "doesn't exist"
    (404) now that the router checks the parent's existence separately from
    listing its children.
    """
    _create_env_and_prefix(client, storage_root)

    response = client.get("/v1/dev/namespaces", headers=AUTH_HEADER, params={"parent": "gold"})

    assert response.status_code == 200, response.text
    assert response.json()["namespaces"] == []


def test_list_namespaces_with_parent_requires_read_on_the_parent(
    client: TestClient, storage_root: str
) -> None:
    """A caller without READ on the parent namespace gets 403, not 404 or 200.

    `list_namespaces`' router loads `parent` with `catalog.load_namespace`
    first, which authorizes `READ_NAMESPACE` at the parent's own scope
    before its existence is even checked -- this is what makes
    authorize-then-404 safe: a caller who can't read `gold` must be
    refused outright, never learn via 404 that it doesn't exist or via a
    200 empty list that it does. Mallory is granted VIEWER only on
    `silver`, an unrelated sibling namespace, never on `gold`.
    """
    _create_env_and_prefix(client, storage_root)
    for ns in (["gold"], ["gold", "sales"], ["silver"]):
        response = client.post("/v1/dev/namespaces", headers=AUTH_HEADER, json={"namespace": ns})
        assert response.status_code in (200, 409), response.text
    client.post("/api/v1/principals", headers=AUTH_HEADER, json={"name": "mallory", "type": "USER"})
    granted = client.post(
        "/api/v1/grants",
        headers=AUTH_HEADER,
        json={
            "principal": "mallory",
            "role": "VIEWER",
            "kind": "NAMESPACE",
            "environment": "dev",
            "namespace": "silver",
        },
    )
    assert granted.status_code == 201, granted.text
    issued = client.post(
        "/api/v1/principals/mallory/tokens", headers=AUTH_HEADER, json={"name": "mallory-pat"}
    )
    assert issued.status_code == 201, issued.text
    mallory_headers = {"Authorization": f"Bearer {issued.json()['token']}"}

    response = client.get("/v1/dev/namespaces", headers=mallory_headers, params={"parent": "gold"})

    assert response.status_code == 403, response.text
    assert response.json()["error"]["type"] == "PermissionDenied"


def test_get_namespace_returns_its_properties(client: TestClient, storage_root: str) -> None:
    """GET on a namespace returns the NamespaceResponse shape.

    Uses `"widgets"` rather than `"gold"`: `"gold"` now comes pre-created
    (empty properties) by the environment's medallion scaffold, so an
    explicit create with `{"owner": "analytics"}` on it would silently 409
    and this test would end up reading back the scaffold's empty properties
    instead of proving anything about properties set at creation time.
    """
    _create_env_and_prefix(client, storage_root)
    created = client.post(
        "/v1/dev/namespaces",
        headers=AUTH_HEADER,
        json={"namespace": ["widgets"], "properties": {"owner": "analytics"}},
    )
    assert created.status_code == 200, created.text
    response = client.get("/v1/dev/namespaces/widgets", headers=AUTH_HEADER)
    assert response.status_code == 200
    assert response.json() == {"namespace": ["widgets"], "properties": {"owner": "analytics"}}


def test_get_unknown_namespace_is_404(client: TestClient, storage_root: str) -> None:
    """NotFoundError maps to 404 with the spec's error shape."""
    _create_env_and_prefix(client, storage_root)
    response = client.get("/v1/dev/namespaces/nope", headers=AUTH_HEADER)
    assert response.status_code == 404
    assert response.json()["error"]["type"] == "NotFoundError"


def test_update_properties_sets_and_removes(client: TestClient, storage_root: str) -> None:
    """The UpdateNamespacePropertiesResponse shape matches PropertyUpdateSummary.

    Uses `"widgets"` rather than `"gold"`: `"gold"` now comes pre-created
    with no properties by the environment's medallion scaffold, so an
    explicit create seeding `keep`/`drop` on it would silently 409 and
    leave nothing for this test's removal/update assertions to act on.
    """
    _create_env_and_prefix(client, storage_root)
    created = client.post(
        "/v1/dev/namespaces",
        headers=AUTH_HEADER,
        json={"namespace": ["widgets"], "properties": {"keep": "1", "drop": "2"}},
    )
    assert created.status_code == 200, created.text
    response = client.post(
        "/v1/dev/namespaces/widgets/properties",
        headers=AUTH_HEADER,
        json={"removals": ["drop", "ghost"], "updates": {"added": "3"}},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["updated"] == ["added"]
    assert body["removed"] == ["drop"]
    assert body["missing"] == ["ghost"]


def test_drop_namespace(client: TestClient, storage_root: str) -> None:
    """DELETE removes the namespace; a subsequent GET is a 404."""
    _create_env_and_prefix(client, storage_root)
    client.post(
        "/v1/dev/namespaces", headers=AUTH_HEADER, json={"namespace": ["gold"], "properties": {}}
    )
    response = client.delete("/v1/dev/namespaces/gold", headers=AUTH_HEADER)
    assert response.status_code == 204
    assert client.get("/v1/dev/namespaces/gold", headers=AUTH_HEADER).status_code == 404


def test_iceberg_namespace_create_and_drop_write_the_audit_events(
    client: TestClient, storage_root: str, settings: Settings
) -> None:
    """A namespace made and dropped through the Iceberg REST catalog is audited like the API's."""
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session

    from lakana_core.models import AuditEvent, AuditEventType

    _create_env_and_prefix(client, storage_root)
    created = client.post(
        "/v1/dev/namespaces", headers=AUTH_HEADER, json={"namespace": ["widgets"], "properties": {}}
    )
    assert created.status_code == 200, created.text
    assert client.delete("/v1/dev/namespaces/widgets", headers=AUTH_HEADER).status_code == 204

    engine = create_engine(settings.database.url)
    with Session(engine) as session:
        rows = session.execute(
            select(AuditEvent.event_type, AuditEvent.detail).where(
                AuditEvent.event_type.in_(
                    [AuditEventType.NAMESPACE_CREATED, AuditEventType.NAMESPACE_DROPPED]
                ),
                AuditEvent.securable_namespace == "widgets",
            )
        ).all()
    engine.dispose()
    assert sorted((row.event_type, row.detail["namespace"]) for row in rows) == [
        (AuditEventType.NAMESPACE_CREATED, "widgets"),
        (AuditEventType.NAMESPACE_DROPPED, "widgets"),
    ]


def test_a_multi_part_namespace_round_trips_through_real_http(
    client: TestClient, storage_root: str
) -> None:
    """The %1F-joined wire encoding survives a real request, not just unit-level encode/decode."""
    from urllib.parse import quote

    from lakana_core.identifiers import NAMESPACE_SEPARATOR

    _create_env_and_prefix(client, storage_root)
    client.post(
        "/v1/dev/namespaces",
        headers=AUTH_HEADER,
        json={"namespace": ["gold", "sales"], "properties": {}},
    )
    encoded = quote("gold" + NAMESPACE_SEPARATOR + "sales", safe="")
    response = client.get(f"/v1/dev/namespaces/{encoded}", headers=AUTH_HEADER)
    assert response.status_code == 200
    assert response.json()["namespace"] == ["gold", "sales"]


def test_create_namespace_with_a_dotted_component_is_422(
    client: TestClient, storage_root: str
) -> None:
    """The Iceberg route refuses a dotted namespace component, naming it."""
    _create_env_and_prefix(client, storage_root)
    response = client.post(
        "/v1/dev/namespaces",
        headers=AUTH_HEADER,
        json={"namespace": ["gold", "a.b"], "properties": {}},
    )
    assert response.status_code == 422, response.text
    assert "'a.b'" in response.text
    assert ["gold", "a.b"] not in client.get(
        "/v1/dev/namespaces", headers=AUTH_HEADER, params={"parent": "gold"}
    ).json()["namespaces"]


def _insert_namespace(settings: Settings, joined: str) -> None:
    """Insert a namespace row directly, bypassing validation.

    Parameters
    ----------
    settings
        Settings naming the test database.
    joined
        The stored (separator-joined) path.
    """
    import uuid

    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session

    from lakana_core.models import Environment, Namespace

    engine = create_engine(settings.database.url)
    with Session(engine) as session:
        env = session.execute(select(Environment).where(Environment.name == "dev")).scalar_one()
        session.add(Namespace(id=uuid.uuid4(), environment_id=env.id, path=joined, properties={}))
        session.commit()
    engine.dispose()


def test_a_dotted_parent_that_does_not_exist_is_422(client: TestClient, storage_root: str) -> None:
    """A new dotted component is refused at any depth, not just the last."""
    _create_env_and_prefix(client, storage_root)
    response = client.post(
        "/v1/dev/namespaces",
        headers=AUTH_HEADER,
        json={"namespace": ["a.b", "c"], "properties": {}},
    )
    assert response.status_code == 422, response.text
    assert "'a.b'" in response.text


def test_a_new_child_under_an_existing_dotted_parent_is_created(
    client: TestClient, storage_root: str, settings: Settings
) -> None:
    """A legacy dotted parent stays usable as a parent."""
    _create_env_and_prefix(client, storage_root)
    _insert_namespace(settings, "a.b")
    response = client.post(
        "/v1/dev/namespaces", headers=AUTH_HEADER, json={"namespace": ["a.b", "c"]}
    )
    assert response.status_code == 200, response.text


def test_a_new_dotted_child_under_an_existing_dotted_parent_is_422(
    client: TestClient, storage_root: str, settings: Settings
) -> None:
    """Only the existing prefix is exempt; the new dotted child is refused."""
    _create_env_and_prefix(client, storage_root)
    _insert_namespace(settings, "a.b")
    response = client.post(
        "/v1/dev/namespaces", headers=AUTH_HEADER, json={"namespace": ["a.b", "c.d"]}
    )
    assert response.status_code == 422, response.text
    assert "'c.d'" in response.text
