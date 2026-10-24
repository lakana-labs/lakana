"""Tests for the Iceberg REST table endpoints."""

import time
import uuid
from datetime import timedelta
from pathlib import Path

import pyarrow as pa
import pytest
from fastapi.testclient import TestClient
from pyiceberg.table.update import RemoveSnapshotRefUpdate, SetSnapshotRefUpdate
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from lakana_core.auth import AllowAllAuthorization, PrincipalRef
from lakana_core.catalog.json_schema import ColumnSpec
from lakana_core.catalog.service import CatalogService
from lakana_core.db import create_engine
from lakana_core.identifiers import TableRef
from lakana_core.models import (
    AuditEvent,
    AuditEventType,
    Principal,
    Table,
    TableLocation,
    TableLocationState,
    WriteMode,
)
from lakana_core.storage.resolver import StorageResolver
from lakana_server.settings import Settings
from tests.conftest import AUTH_HEADER


def _setup(client: TestClient, storage_root: str) -> None:
    """Create the `dev` environment and a `gold` namespace.

    Parameters
    ----------
    client
        The test client.
    storage_root
        The environment's storage root URI.
    """
    client.post(
        "/api/v1/environments",
        headers=AUTH_HEADER,
        json={
            "name": "dev",
            "storage": {"scheme": "file", "root": storage_root, "options": {}},
        },
    )
    client.post(
        "/v1/dev/namespaces", headers=AUTH_HEADER, json={"namespace": ["gold"], "properties": {}}
    )


_SCHEMA = {
    "type": "struct",
    "schema-id": 0,
    "fields": [
        {"id": 1, "name": "id", "type": "long", "required": True},
        {"id": 2, "name": "name", "type": "string", "required": False},
    ],
}


def test_create_then_load_table(client: TestClient, storage_root: str) -> None:
    """A created table loads back with the same metadata location."""
    _setup(client, storage_root)
    created = client.post(
        "/v1/dev/namespaces/gold/tables",
        headers=AUTH_HEADER,
        json={"name": "sales", "schema": _SCHEMA, "properties": {}},
    )
    assert created.status_code == 200, created.text
    assert created.json()["metadata-location"].endswith(".metadata.json")
    assert created.json()["metadata"]["table-uuid"]

    loaded = client.get("/v1/dev/namespaces/gold/tables/sales", headers=AUTH_HEADER)
    assert loaded.status_code == 200
    assert loaded.json()["metadata-location"] == created.json()["metadata-location"]


def test_create_with_a_real_partition_spec_and_write_order(
    client: TestClient, storage_root: str
) -> None:
    """A real client always sends non-empty `partition-spec`/`write-order` objects.

    `RestCatalog._create_table` never omits these fields — even an
    unpartitioned, unsorted table gets `{"spec-id": 0, "fields": []}` and
    `{"order-id": 0, "fields": []}` — so the branch that actually parses a
    populated spec is the one 100% of real traffic exercises. This test
    pins that the parsed partition spec and sort order really do reach the
    created table's metadata, not just that the endpoint accepts them.
    """
    _setup(client, storage_root)
    response = client.post(
        "/v1/dev/namespaces/gold/tables",
        headers=AUTH_HEADER,
        json={
            "name": "sales",
            "schema": _SCHEMA,
            "partition-spec": {
                "spec-id": 0,
                "fields": [
                    {"source-id": 1, "field-id": 1000, "transform": "identity", "name": "id"}
                ],
            },
            "write-order": {
                "order-id": 1,
                "fields": [
                    {
                        "source-id": 1,
                        "transform": "identity",
                        "direction": "asc",
                        "null-order": "nulls-first",
                    }
                ],
            },
            "properties": {},
        },
    )
    assert response.status_code == 200, response.text
    metadata = response.json()["metadata"]
    assert metadata["partition-specs"][0]["fields"] == [
        {"source-id": 1, "field-id": 1000, "transform": "identity", "name": "id"}
    ]
    assert metadata["default-sort-order-id"] == 1
    assert metadata["sort-orders"][0]["fields"] == [
        {"source-id": 1, "transform": "identity", "direction": "asc", "null-order": "nulls-first"}
    ]


def test_create_the_same_table_twice_is_a_conflict(client: TestClient, storage_root: str) -> None:
    """AlreadyExistsError maps to 409."""
    _setup(client, storage_root)
    body = {"name": "sales", "schema": _SCHEMA, "properties": {}}
    client.post("/v1/dev/namespaces/gold/tables", headers=AUTH_HEADER, json=body)
    response = client.post("/v1/dev/namespaces/gold/tables", headers=AUTH_HEADER, json=body)
    assert response.status_code == 409
    assert response.json()["error"]["type"] == "AlreadyExistsError"


def test_create_with_a_caller_supplied_location_is_refused(
    client: TestClient, storage_root: str
) -> None:
    """A client-supplied `location` is refused, even one inside the environment's root.

    Lakana assigns every managed table's location itself,
    id-suffixed; a caller-supplied location could otherwise collide with
    another table's directory (for instance one named after a table
    renamed away from this name), defeating that confinement.
    """
    _setup(client, storage_root)
    response = client.post(
        "/v1/dev/namespaces/gold/tables",
        headers=AUTH_HEADER,
        json={
            "name": "customers",
            "schema": _SCHEMA,
            "location": f"{storage_root}/managed/gold/customers",
            "properties": {},
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["type"] == "ValidationError"
    assert (
        client.get("/v1/dev/namespaces/gold/tables/customers", headers=AUTH_HEADER).status_code
        == 404
    )


def test_create_with_stage_create_true_is_rejected(client: TestClient, storage_root: str) -> None:
    """Staged creation is not implemented; the request must fail, not silently commit."""
    _setup(client, storage_root)
    response = client.post(
        "/v1/dev/namespaces/gold/tables",
        headers=AUTH_HEADER,
        json={"name": "sales", "schema": _SCHEMA, "stage-create": True},
    )
    assert response.status_code == 422
    assert response.json()["error"]["type"] == "ValidationError"
    assert (
        client.get("/v1/dev/namespaces/gold/tables/sales", headers=AUTH_HEADER).status_code == 404
    )


def test_create_with_a_malformed_schema_is_a_pydantic_validation_error(
    client: TestClient, storage_root: str
) -> None:
    """The nested schema dict is validated by pyiceberg's own model, via iceberg_json."""
    _setup(client, storage_root)
    response = client.post(
        "/v1/dev/namespaces/gold/tables",
        headers=AUTH_HEADER,
        json={"name": "sales", "schema": {"type": "struct", "fields": "not-a-list"}},
    )
    assert response.status_code == 422
    assert response.json()["error"]["type"] == "ValidationError"


def test_get_unknown_table_is_404(client: TestClient, storage_root: str) -> None:
    """NotFoundError maps to 404."""
    _setup(client, storage_root)
    response = client.get("/v1/dev/namespaces/gold/tables/nope", headers=AUTH_HEADER)
    assert response.status_code == 404


def test_list_tables(client: TestClient, storage_root: str) -> None:
    """ListTablesResponse's identifiers shape matches what pyiceberg's client expects."""
    _setup(client, storage_root)
    client.post(
        "/v1/dev/namespaces/gold/tables",
        headers=AUTH_HEADER,
        json={"name": "sales", "schema": _SCHEMA, "properties": {}},
    )
    response = client.get("/v1/dev/namespaces/gold/tables", headers=AUTH_HEADER)
    assert response.status_code == 200
    assert response.json() == {"identifiers": [{"namespace": ["gold"], "name": "sales"}]}


def _tombstone(settings: Settings) -> TableLocation:
    """Read the one location record, which a drop makes a tombstone.

    Parameters
    ----------
    settings
        The app's settings, which name its database.

    Returns
    -------
    TableLocation
        The record, detached.
    """
    engine = create_engine(settings.database)
    try:
        with Session(engine) as session:
            record = session.scalars(select(TableLocation)).one()
            session.expunge(record)
            return record
    finally:
        engine.dispose()


def test_drop_table(client: TestClient, storage_root: str, settings: Settings) -> None:
    """DELETE without purge removes the catalog row and keeps the files for the undo period.

    A subsequent GET is a 404, but the file on disk must survive, and the location record is a
    tombstone due seven days after the drop (`purgeRequested=false` gives one too).
    """
    _setup(client, storage_root)
    created = client.post(
        "/v1/dev/namespaces/gold/tables",
        headers=AUTH_HEADER,
        json={"name": "sales", "schema": _SCHEMA, "properties": {}},
    )
    metadata_path = created.json()["metadata-location"].removeprefix("file://")
    assert Path(metadata_path).exists()

    response = client.delete(
        "/v1/dev/namespaces/gold/tables/sales",
        headers=AUTH_HEADER,
        params={"purgeRequested": "false"},
    )
    assert response.status_code == 204
    assert (
        client.get("/v1/dev/namespaces/gold/tables/sales", headers=AUTH_HEADER).status_code == 404
    )
    assert Path(metadata_path).exists()
    record = _tombstone(settings)
    assert record.state is TableLocationState.DROPPED
    assert record.purge_after is not None and record.dropped_at is not None
    assert record.purge_after - record.dropped_at == timedelta(days=7)
    assert _drop_reasons(settings) == ["user"]


def _drop_reasons(settings: Settings) -> list[str]:
    """Read the reasons of the `TABLE_DROPPED` events.

    Parameters
    ----------
    settings
        The app's settings.

    Returns
    -------
    list of str
        One per event.
    """
    engine = create_engine(settings.database)
    try:
        with Session(engine) as session:
            return [
                event.detail["reason"]
                for event in session.scalars(
                    select(AuditEvent).where(AuditEvent.event_type == AuditEventType.TABLE_DROPPED)
                )
            ]
    finally:
        engine.dispose()


def test_drop_table_with_purge_query_parameter(
    client: TestClient, storage_root: str, settings: Settings
) -> None:
    """The `purgeRequested` flag is a query parameter, not a body field.

    Verified against a real client. With it the tombstone is due at once: the next sweep of the
    worker deletes the files, and the drop itself deletes nothing.
    """
    _setup(client, storage_root)
    created = client.post(
        "/v1/dev/namespaces/gold/tables",
        headers=AUTH_HEADER,
        json={"name": "sales", "schema": _SCHEMA, "properties": {}},
    )
    metadata_path = created.json()["metadata-location"].removeprefix("file://")
    assert Path(metadata_path).exists()

    response = client.request(
        "DELETE",
        "/v1/dev/namespaces/gold/tables/sales",
        headers=AUTH_HEADER,
        params={"purgeRequested": "true"},
    )
    assert response.status_code == 204
    assert Path(metadata_path).exists()
    record = _tombstone(settings)
    assert record.state is TableLocationState.DROPPED
    assert record.purge_after == record.dropped_at
    assert _drop_reasons(settings) == ["purge"]


def test_drop_table_over_iceberg_rest_never_drops_a_view(
    client: TestClient, storage_root: str, settings: Settings
) -> None:
    """Iceberg DELETE must refuse a view, like HEAD and GET already do.

    Views are not created over the Iceberg REST surface, so this
    inserts one directly against the same database `client`'s app uses —
    `settings` is the same object the `client` fixture builds its app from,
    so `create_engine(settings.database)` reaches the same file.
    """
    _setup(client, storage_root)
    engine = create_engine(settings.database)
    with Session(engine) as session:
        principal_row = session.execute(select(Principal)).scalars().one()
        principal = PrincipalRef(
            id=principal_row.id, name=principal_row.name, type=principal_row.type
        )
        catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
        catalog.create_or_replace_view(
            principal,
            TableRef.from_dotted("dev", "gold.v"),
            sql="SELECT 1 AS id",
            references=[],
            columns=[ColumnSpec(name="id", type="long", nullable=True)],
        )

    response = client.delete("/v1/dev/namespaces/gold/tables/v", headers=AUTH_HEADER)

    assert response.status_code == 404
    with Session(engine) as session:
        row = session.execute(select(Table).where(Table.name == "v")).scalar_one_or_none()
        assert row is not None
    engine.dispose()


def test_rename_table(client: TestClient, storage_root: str) -> None:
    """Rename moves the table; the old name is gone, the new one loads."""
    _setup(client, storage_root)
    client.post(
        "/v1/dev/namespaces/gold/tables",
        headers=AUTH_HEADER,
        json={"name": "sales", "schema": _SCHEMA, "properties": {}},
    )
    response = client.post(
        "/v1/dev/tables/rename",
        headers=AUTH_HEADER,
        json={
            "source": {"namespace": ["gold"], "name": "sales"},
            "destination": {"namespace": ["gold"], "name": "sales_renamed"},
        },
    )
    assert response.status_code == 200
    assert (
        client.get("/v1/dev/namespaces/gold/tables/sales", headers=AUTH_HEADER).status_code == 404
    )
    assert (
        client.get("/v1/dev/namespaces/gold/tables/sales_renamed", headers=AUTH_HEADER).status_code
        == 200
    )


def test_rename_with_a_missing_name_is_a_validation_error(
    client: TestClient, storage_root: str
) -> None:
    """A malformed rename body is a 422 in the Iceberg shape, not a bare 500.

    `source`/`destination` are now a typed nested model, so FastAPI itself
    rejects a missing `name` as a `RequestValidationError` before the
    endpoint body ever indexes into it.
    """
    _setup(client, storage_root)
    response = client.post(
        "/v1/dev/tables/rename",
        headers=AUTH_HEADER,
        json={
            "source": {"namespace": ["gold"]},
            "destination": {"namespace": ["gold"], "name": "x"},
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["type"] == "RequestValidationError"


def test_rename_onto_an_existing_table_is_a_conflict(client: TestClient, storage_root: str) -> None:
    """AlreadyExistsError from CatalogService.rename_table maps to 409."""
    _setup(client, storage_root)
    for name in ("sales", "occupied"):
        client.post(
            "/v1/dev/namespaces/gold/tables",
            headers=AUTH_HEADER,
            json={"name": name, "schema": _SCHEMA, "properties": {}},
        )
    response = client.post(
        "/v1/dev/tables/rename",
        headers=AUTH_HEADER,
        json={
            "source": {"namespace": ["gold"], "name": "sales"},
            "destination": {"namespace": ["gold"], "name": "occupied"},
        },
    )
    assert response.status_code == 409


def test_commit_applies_an_update(client: TestClient, storage_root: str) -> None:
    """A committed update is visible on reload, at a new metadata location."""
    _setup(client, storage_root)
    created = client.post(
        "/v1/dev/namespaces/gold/tables",
        headers=AUTH_HEADER,
        json={"name": "sales", "schema": _SCHEMA, "properties": {}},
    )
    table_uuid = created.json()["metadata"]["table-uuid"]

    committed = client.post(
        "/v1/dev/namespaces/gold/tables/sales",
        headers=AUTH_HEADER,
        json={
            "identifier": {"namespace": ["gold"], "name": "sales"},
            "requirements": [{"type": "assert-table-uuid", "uuid": table_uuid}],
            "updates": [{"action": "set-properties", "updates": {"owner": "chris"}}],
        },
    )
    assert committed.status_code == 200, committed.text
    assert committed.json()["metadata-location"] != created.json()["metadata-location"]
    assert committed.json()["metadata"]["properties"]["owner"] == "chris"


def test_commit_with_a_failed_requirement_is_a_conflict(
    client: TestClient, storage_root: str
) -> None:
    """ConflictError from check_requirements maps to 409, and nothing is written."""
    import uuid

    _setup(client, storage_root)
    created = client.post(
        "/v1/dev/namespaces/gold/tables",
        headers=AUTH_HEADER,
        json={"name": "sales", "schema": _SCHEMA, "properties": {}},
    )
    response = client.post(
        "/v1/dev/namespaces/gold/tables/sales",
        headers=AUTH_HEADER,
        json={
            "identifier": {"namespace": ["gold"], "name": "sales"},
            "requirements": [{"type": "assert-table-uuid", "uuid": str(uuid.uuid4())}],
            "updates": [{"action": "set-properties", "updates": {"owner": "nobody"}}],
        },
    )
    assert response.status_code == 409
    assert response.json()["error"]["type"] == "ConflictError"

    reloaded = client.get("/v1/dev/namespaces/gold/tables/sales", headers=AUTH_HEADER)
    assert reloaded.json()["metadata-location"] == created.json()["metadata-location"]


def test_commit_with_no_requirements_or_updates_is_a_no_op_success(
    client: TestClient, storage_root: str
) -> None:
    """An empty commit is valid — CommitTableRequest's arrays both default to []."""
    _setup(client, storage_root)
    client.post(
        "/v1/dev/namespaces/gold/tables",
        headers=AUTH_HEADER,
        json={"name": "sales", "schema": _SCHEMA, "properties": {}},
    )
    response = client.post(
        "/v1/dev/namespaces/gold/tables/sales",
        headers=AUTH_HEADER,
        json={"identifier": {"namespace": ["gold"], "name": "sales"}},
    )
    assert response.status_code == 200


def test_commit_removing_an_unset_property_is_a_422_not_a_500(
    client: TestClient, storage_root: str
) -> None:
    """A `remove-properties` update for an absent key must not reach Starlette.

    pyiceberg pops each removal off the properties dict unguarded, raising
    `KeyError`. Untranslated, that escapes core, escapes `IcebergRoute`, and
    Starlette answers plain-text ``Internal Server Error`` with no JSON body
    at all — on the one surface whose whole purpose is a spec-mandated error
    shape. Reachable by any non-pyiceberg client and by two writers racing
    to remove the same property.
    """
    _setup(client, storage_root)
    client.post(
        "/v1/dev/namespaces/gold/tables",
        headers=AUTH_HEADER,
        json={"name": "sales", "schema": _SCHEMA, "properties": {}},
    )

    response = client.post(
        "/v1/dev/namespaces/gold/tables/sales",
        headers=AUTH_HEADER,
        json={
            "identifier": {"namespace": ["gold"], "name": "sales"},
            "updates": [{"action": "remove-properties", "removals": ["never-set"]}],
        },
    )

    assert response.status_code == 422, response.text
    assert response.json()["error"]["type"] == "ValidationError"
    assert response.json()["error"]["code"] == 422


def test_create_table_with_a_dotted_name_is_422(client: TestClient, storage_root: str) -> None:
    """The Iceberg route refuses a dotted table name, naming it."""
    _setup(client, storage_root)
    response = client.post(
        "/v1/dev/namespaces/gold/tables",
        headers=AUTH_HEADER,
        json={"name": "a.b", "schema": _SCHEMA, "properties": {}},
    )
    assert response.status_code == 422, response.text
    assert "'a.b'" in response.text


def test_rename_to_a_dotted_name_is_422(client: TestClient, storage_root: str) -> None:
    """Renaming onto a dotted name is refused; the table keeps its old name."""
    _setup(client, storage_root)
    client.post(
        "/v1/dev/namespaces/gold/tables",
        headers=AUTH_HEADER,
        json={"name": "sales", "schema": _SCHEMA, "properties": {}},
    )
    response = client.post(
        "/v1/dev/tables/rename",
        headers=AUTH_HEADER,
        json={
            "source": {"namespace": ["gold"], "name": "sales"},
            "destination": {"namespace": ["gold"], "name": "a.b"},
        },
    )
    assert response.status_code == 422, response.text
    assert "'a.b'" in response.text
    assert (
        client.get("/v1/dev/namespaces/gold/tables/sales", headers=AUTH_HEADER).status_code == 200
    )


def _rows_in_sales(settings: Settings, *, first_build_failed: bool) -> int:
    """Create `gold.sales` with one row through core, optionally as a rolled-back first build.

    The second form leaves what `CatalogService.roll_back` leaves: a tag on the written
    snapshot, no `main`, and the not-published marker (set directly, as `roll_back` is not
    reachable from a route).

    Parameters
    ----------
    settings
        The app's settings, which name its database.
    first_build_failed
        Leave the table not published.

    Returns
    -------
    int
        The written snapshot's id.
    """
    engine = create_engine(settings.database)
    try:
        with Session(engine) as session:
            row = session.execute(select(Principal)).scalars().one()
            principal = PrincipalRef(id=row.id, name=row.name, type=row.type)
            catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
            ref = TableRef.from_dotted("dev", "gold.sales")
            data = pa.table({"id": pa.array([1], pa.int64())})
            written = catalog.write_batches(
                principal, ref, mode=WriteMode.CREATE, schema=data.schema, batches=data.to_batches()
            )
            assert written.snapshot_id is not None
            if first_build_failed:
                catalog.commit_table(
                    principal,
                    ref,
                    requirements=[],
                    updates=[
                        SetSnapshotRefUpdate(
                            ref_name="lakana-check-failed-x",
                            type="tag",
                            snapshot_id=written.snapshot_id,
                        ),
                        RemoveSnapshotRefUpdate(ref_name="main"),
                    ],
                    internal_rollback=True,
                )
                table = session.execute(select(Table).where(Table.name == "sales")).scalar_one()
                table.unpublished_run_id = uuid.uuid4()
                session.commit()
            return written.snapshot_id
    finally:
        engine.dispose()


def test_the_iceberg_commit_route_refuses_to_remove_main(
    client: TestClient, storage_root: str, settings: Settings
) -> None:
    """An engine cannot empty a table by removing its `main` reference (rule C1)."""
    _setup(client, storage_root)
    snapshot = _rows_in_sales(settings, first_build_failed=False)
    before = client.get("/v1/dev/namespaces/gold/tables/sales", headers=AUTH_HEADER).json()

    response = client.post(
        "/v1/dev/namespaces/gold/tables/sales",
        headers=AUTH_HEADER,
        json={
            "identifier": {"namespace": ["gold"], "name": "sales"},
            "updates": [{"action": "remove-snapshot-ref", "ref-name": "main"}],
        },
    )

    assert response.status_code == 422, response.text
    assert response.json()["error"]["type"] == "ValidationError"
    assert response.json()["error"]["message"] == (
        "A commit cannot remove a table's current snapshot: readers would see an empty table. "
        "Drop the table, or write the rows it should hold."
    )
    after = client.get("/v1/dev/namespaces/gold/tables/sales", headers=AUTH_HEADER).json()
    assert after["metadata-location"] == before["metadata-location"]
    assert after["metadata"]["current-snapshot-id"] == snapshot


def test_the_iceberg_commit_route_cannot_publish_a_table_by_reference(
    client: TestClient, storage_root: str, settings: Settings
) -> None:
    """A not-published table gets a current snapshot only from a snapshot the commit adds (C2)."""
    _setup(client, storage_root)
    snapshot = _rows_in_sales(settings, first_build_failed=True)

    response = client.post(
        "/v1/dev/namespaces/gold/tables/sales",
        headers=AUTH_HEADER,
        json={
            "identifier": {"namespace": ["gold"], "name": "sales"},
            "updates": [
                {
                    "action": "set-snapshot-ref",
                    "ref-name": "main",
                    "type": "branch",
                    "snapshot-id": snapshot,
                }
            ],
        },
    )

    assert response.status_code == 422, response.text
    assert response.json()["error"]["type"] == "ValidationError"
    assert response.json()["error"]["message"] == (
        "gold.sales is not published: its first build did not finish, or failed a check. A "
        "commit can publish it only with a new snapshot, not by pointing at an existing one. "
        "Run its pipeline again."
    )
    engine = create_engine(settings.database)
    with Session(engine) as session:
        row = session.execute(select(Table).where(Table.name == "sales")).scalar_one()
        assert row.unpublished_run_id is not None
    engine.dispose()


def test_loading_a_table_that_is_not_published_answers_404_with_the_reason(
    client: TestClient, storage_root: str, settings: Settings
) -> None:
    _setup(client, storage_root)
    _rows_in_sales(settings, first_build_failed=True)
    run_id = uuid.uuid4()
    engine = create_engine(settings.database)
    with Session(engine) as session:
        table = session.execute(select(Table).where(Table.name == "sales")).scalar_one()
        table.unpublished_run_id = run_id
        session.commit()
    engine.dispose()
    path = "/v1/dev/namespaces/gold/tables/sales"

    loaded = client.get(path, headers=AUTH_HEADER)
    with_snapshots = client.get(f"{path}?snapshots=all", headers=AUTH_HEADER)
    listed = client.get("/v1/dev/namespaces/gold/tables", headers=AUTH_HEADER)

    expected = (
        "`gold.sales` has not been published: its first build did not finish, "
        f"or failed a check (run {run_id}). It is published by the next run that builds it "
        "and passes its checks."
    )
    for refused in (loaded, with_snapshots):
        assert refused.status_code == 404, refused.text
        assert refused.json() == {
            "error": {"message": expected, "type": "NotPublishedError", "code": 404}
        }
    assert listed.json() == {"identifiers": [{"namespace": ["gold"], "name": "sales"}]}


def test_a_caller_without_read_learns_nothing_about_publication_from_the_load(
    client: TestClient, storage_root: str, settings: Settings
) -> None:
    """Authorization comes first: 403 with the text a published table gets, not 404."""
    _setup(client, storage_root)
    _rows_in_sales(settings, first_build_failed=True)
    created = client.post(
        "/v1/dev/namespaces/gold/tables",
        headers=AUTH_HEADER,
        json={"name": "fine", "schema": _SCHEMA, "properties": {}},
    )
    assert created.status_code == 200, created.text
    client.post("/api/v1/principals", headers=AUTH_HEADER, json={"name": "mallory", "type": "USER"})
    client.post("/v1/dev/namespaces", headers=AUTH_HEADER, json={"namespace": ["silver"]})
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
    mallory = {"Authorization": f"Bearer {issued.json()['token']}"}

    stopped = client.get("/v1/dev/namespaces/gold/tables/sales", headers=mallory)
    fine = client.get("/v1/dev/namespaces/gold/tables/fine", headers=mallory)

    assert (stopped.status_code, fine.status_code) == (403, 403)
    assert stopped.json()["error"]["type"] == "PermissionDenied"
    assert "published" not in stopped.text
    assert stopped.json() == {
        "error": {
            **fine.json()["error"],
            "message": fine.json()["error"]["message"].replace("fine", "sales"),
        }
    }


_HISTORY = ("snapshots", "refs", "current-snapshot-id", "snapshot-log", "metadata-log")


def _holder(client: TestClient, name: str, *actions: str) -> dict[str, str]:
    """Create a user holding some actions on `dev` and return their header.

    Parameters
    ----------
    client
        The test client.
    name
        The user's name.
    *actions
        The actions granted, each on the whole environment.

    Returns
    -------
    dict
        The `Authorization` header.
    """
    client.post("/api/v1/principals", headers=AUTH_HEADER, json={"name": name, "type": "USER"})
    for action in actions:
        granted = client.post(
            "/api/v1/grants",
            headers=AUTH_HEADER,
            json={"principal": name, "action": action, "kind": "ENVIRONMENT", "environment": "dev"},
        )
        assert granted.status_code == 201, granted.text
    issued = client.post(
        f"/api/v1/principals/{name}/tokens", headers=AUTH_HEADER, json={"name": f"{name}-pat"}
    )
    return {"Authorization": f"Bearer {issued.json()['token']}"}


def _without_history(metadata: dict[str, object]) -> None:
    """Assert that an answer's metadata shows nothing of the table's history.

    Parameters
    ----------
    metadata
        The answer's `metadata`.
    """
    for key in (*_HISTORY, "properties", "statistics", "partition-statistics"):
        assert key not in metadata, key
    assert "lakana-check-failed" not in str(metadata)
    assert metadata["table-uuid"] and metadata["schemas"] and metadata["location"]


def _tag_a_failed_check(settings: Settings, snapshot_id: int) -> None:
    """Put a failed check's tag on a snapshot of `gold.sales`, keeping `main`.

    Parameters
    ----------
    settings
        The app's settings, which name its database.
    snapshot_id
        The snapshot to tag.
    """
    engine = create_engine(settings.database)
    try:
        with Session(engine) as session:
            row = session.execute(select(Principal)).scalars().one()
            principal = PrincipalRef(id=row.id, name=row.name, type=row.type)
            CatalogService(session, StorageResolver(session), AllowAllAuthorization()).commit_table(
                principal,
                TableRef.from_dotted("dev", "gold.sales"),
                requirements=[],
                updates=[
                    SetSnapshotRefUpdate(
                        ref_name="lakana-check-failed-x", type="tag", snapshot_id=snapshot_id
                    )
                ],
                internal_rollback=True,  # the rollback is the one setter of such a tag
            )
    finally:
        engine.dispose()


def test_a_writer_without_read_gets_an_answer_without_the_tables_history(
    client: TestClient, storage_root: str, settings: Settings
) -> None:
    """The commit lands; the answer names no snapshot, ref, property or failed check."""
    _setup(client, storage_root)
    _tag_a_failed_check(settings, _rows_in_sales(settings, first_build_failed=False))
    writer = _holder(client, "wanda", "READ_ENVIRONMENT", "WRITE_TABLE")

    committed = client.post(
        "/v1/dev/namespaces/gold/tables/sales",
        headers=writer,
        json={"updates": [{"action": "set-properties", "updates": {"owner": "wanda"}}]},
    )

    assert committed.status_code == 200, committed.text
    _without_history(committed.json()["metadata"])
    reloaded = client.get("/v1/dev/namespaces/gold/tables/sales", headers=AUTH_HEADER).json()
    assert reloaded["metadata-location"] == committed.json()["metadata-location"]
    assert reloaded["metadata"]["properties"]["owner"] == "wanda"
    assert "lakana-check-failed-x" in reloaded["metadata"]["refs"]


def test_the_no_op_probe_tells_a_writer_without_read_nothing(
    client: TestClient, storage_root: str, settings: Settings
) -> None:
    """A commit with no requirements and no updates used to answer every snapshot."""
    _setup(client, storage_root)
    _tag_a_failed_check(settings, _rows_in_sales(settings, first_build_failed=False))
    writer = _holder(client, "wanda", "READ_ENVIRONMENT", "WRITE_TABLE")

    probe = client.post("/v1/dev/namespaces/gold/tables/sales", headers=writer, json={})

    assert probe.status_code == 200, probe.text
    _without_history(probe.json()["metadata"])
    assert probe.json()["metadata-location"].endswith(".metadata.json")


def test_a_reader_gets_the_whole_answer(
    client: TestClient, storage_root: str, settings: Settings
) -> None:
    _setup(client, storage_root)
    _rows_in_sales(settings, first_build_failed=False)
    reader = _holder(client, "rita", "READ_ENVIRONMENT", "READ_TABLE", "WRITE_TABLE")

    probe = client.post("/v1/dev/namespaces/gold/tables/sales", headers=reader, json={})

    assert probe.status_code == 200, probe.text
    assert len(probe.json()["metadata"]["snapshots"]) == 1
    assert "main" in probe.json()["metadata"]["refs"]


def test_a_reader_of_a_table_that_is_not_published_gets_no_history(
    client: TestClient, storage_root: str, settings: Settings
) -> None:
    """The load refuses such a table, so the commit's answer shows no more than the load."""
    _setup(client, storage_root)
    _rows_in_sales(settings, first_build_failed=True)

    probe = client.post("/v1/dev/namespaces/gold/tables/sales", headers=AUTH_HEADER, json={})

    assert probe.status_code == 200, probe.text
    _without_history(probe.json()["metadata"])


def test_a_creator_without_read_gets_an_answer_without_the_tables_history(
    client: TestClient, storage_root: str
) -> None:
    _setup(client, storage_root)
    creator = _holder(client, "carl", "READ_ENVIRONMENT", "CREATE_TABLE")

    created = client.post(
        "/v1/dev/namespaces/gold/tables",
        headers=creator,
        json={"name": "fresh", "schema": _SCHEMA, "properties": {"owner": "carl"}},
    )

    assert created.status_code == 200, created.text
    _without_history(created.json()["metadata"])


def _probe(client: TestClient, headers: dict[str, str], ref: str, snapshot_id: int) -> str:
    """Assert something about a ref of `gold.sales` and return the 409's message.

    Parameters
    ----------
    client
        The test client.
    headers
        The caller's header.
    ref
        The ref asserted.
    snapshot_id
        The snapshot id asserted, which is wrong.

    Returns
    -------
    str
        The error message.
    """
    refused = client.post(
        "/v1/dev/namespaces/gold/tables/sales",
        headers=headers,
        json={
            "requirements": [
                {"type": "assert-ref-snapshot-id", "ref": ref, "snapshot-id": snapshot_id}
            ]
        },
    )
    assert refused.status_code == 409, refused.text
    return refused.json()["error"]["message"]


def test_a_failed_requirement_tells_a_writer_without_read_nothing_of_the_table(
    client: TestClient, storage_root: str, settings: Settings
) -> None:
    """Neither the head's snapshot id nor whether a failed check's tag exists."""
    _setup(client, storage_root)
    head = _rows_in_sales(settings, first_build_failed=False)
    _tag_a_failed_check(settings, head)
    writer = _holder(client, "wanda", "READ_ENVIRONMENT", "WRITE_TABLE")

    messages = {
        _probe(client, writer, "main", 1),
        _probe(client, writer, "lakana-check-failed-x", 1),
        _probe(client, writer, "lakana-check-failed-y", 1),
    }

    assert len(messages) == 1
    [message] = messages
    assert str(head) not in message
    assert "lakana-check-failed" not in message and "missing" not in message
    assert "changed while this write was in progress" in message and "READ" in message


def test_a_failed_requirement_tells_a_reader_what_failed(
    client: TestClient, storage_root: str, settings: Settings
) -> None:
    _setup(client, storage_root)
    head = _rows_in_sales(settings, first_build_failed=False)

    message = _probe(client, AUTH_HEADER, "main", 1)

    assert str(head) in message


def test_an_update_that_does_not_fit_tells_a_writer_without_read_nothing(
    client: TestClient, storage_root: str, settings: Settings
) -> None:
    """The 422 of pyiceberg for a stale snapshot prints a stored number; without READ, not."""
    _setup(client, storage_root)
    head = _rows_in_sales(settings, first_build_failed=False)
    writer = _holder(client, "wanda", "READ_ENVIRONMENT", "WRITE_TABLE")
    loaded = client.get("/v1/dev/namespaces/gold/tables/sales", headers=AUTH_HEADER).json()[
        "metadata"
    ]
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
    body = {"updates": [stale]}

    refused = client.post("/v1/dev/namespaces/gold/tables/sales", headers=writer, json=body)
    detailed = client.post("/v1/dev/namespaces/gold/tables/sales", headers=AUTH_HEADER, json=body)

    assert (refused.status_code, detailed.status_code) == (422, 422)
    assert str(head) not in refused.text
    assert f"older than last sequence number {stored}" in detailed.text
    assert stored not in refused.text
    for leak in ("manifest", "Summary", "file://", "snap-"):
        assert leak not in refused.text, leak
    assert "READ" in refused.json()["error"]["message"]


def test_an_iceberg_commit_cannot_remove_snapshots_or_the_tag_of_a_failed_check(
    client: TestClient, storage_root: str, settings: Settings
) -> None:
    """403 with the way out, for a writer and for an administrator; a user's own ref goes."""
    _setup(client, storage_root)
    _rows_in_sales(settings, first_build_failed=False)
    writer = _holder(client, "wanda", "READ_ENVIRONMENT", "WRITE_TABLE")
    route = "/v1/dev/namespaces/gold/tables/sales"
    removals = (
        {"action": "remove-schemas", "schema-ids": [5]},
        {"action": "remove-partition-specs", "spec-ids": [5]},
        {
            "action": "set-snapshot-ref",
            "ref-name": "lakana-check-failed-x",
            "type": "tag",
            "snapshot-id": 1,
        },
        {"action": "remove-snapshots", "snapshot-ids": [1]},
        {"action": "remove-snapshot-ref", "ref-name": "lakana-check-failed-x"},
    )

    for headers in (writer, AUTH_HEADER):
        for removal in removals:
            refused = client.post(route, headers=headers, json={"updates": [removal]})
            assert refused.status_code == 403, refused.text
            assert refused.json()["error"]["message"]
    head = _head_of_sales(settings)
    made = client.post(
        route,
        headers=AUTH_HEADER,
        json={
            "updates": [
                {
                    "action": "set-snapshot-ref",
                    "ref-name": "mine",
                    "type": "branch",
                    "snapshot-id": head,
                }
            ]
        },
    )
    assert made.status_code == 200, made.text
    other = client.post(
        route,
        headers=AUTH_HEADER,
        json={"updates": [{"action": "remove-snapshot-ref", "ref-name": "mine"}]},
    )
    assert other.status_code == 200, other.text
    assert "mine" not in other.json()["metadata"]["refs"]


def test_removing_a_property_tells_a_writer_without_read_nothing_of_whether_it_was_there(
    client: TestClient, storage_root: str, settings: Settings
) -> None:
    _setup(client, storage_root)
    _rows_in_sales(settings, first_build_failed=False)
    set_owner = {"updates": [{"action": "set-properties", "updates": {"owner": "x"}}]}
    assert (
        client.post(
            "/v1/dev/namespaces/gold/tables/sales", headers=AUTH_HEADER, json=set_owner
        ).status_code
        == 200
    )
    writer = _holder(client, "wanda", "READ_ENVIRONMENT", "WRITE_TABLE")

    answers = []
    # The absent key first: its removal changes nothing, so a time of the last change in its
    # answer would be the earlier one. The two requests are apart by more than a millisecond.
    for key in ("nobody-set-this", "owner"):
        time.sleep(0.01)
        answers.append(
            client.post(
                "/v1/dev/namespaces/gold/tables/sales",
                headers=writer,
                json={"updates": [{"action": "remove-properties", "removals": [key]}]},
            )
        )

    assert [a.status_code for a in answers] == [200, 200]
    first, second = (a.json()["metadata"] for a in answers)
    assert first == second


def _head_of_sales(settings: Settings) -> int:
    """Read the current snapshot id of `gold.sales` from its catalog row and metadata.

    Parameters
    ----------
    settings
        The app's settings, which name its database.

    Returns
    -------
    int
        The id.
    """
    engine = create_engine(settings.database)
    try:
        with Session(engine) as session:
            row = session.execute(select(Principal).where(Principal.name == "admin")).scalar_one()
            loaded = CatalogService(
                session, StorageResolver(session), AllowAllAuthorization()
            ).load_table(
                PrincipalRef(id=row.id, name=row.name, type=row.type),
                TableRef.from_dotted("dev", "gold.sales"),
            )
            assert loaded.metadata.current_snapshot_id is not None
            return loaded.metadata.current_snapshot_id
    finally:
        engine.dispose()


@pytest.mark.slow
def test_an_unbound_task_token_is_refused_a_kept_table_over_the_iceberg_load(
    client: TestClient, storage_root: str, settings: Settings
) -> None:
    """A task token minted over REST is a task with no run: 404 with the refusal."""
    _setup(client, storage_root)
    _rows_in_sales(settings, first_build_failed=False)
    engine = create_engine(settings.database)
    try:
        with Session(engine) as session:
            session.execute(
                update(Table).values(
                    kept_run_id=uuid.uuid4(), kept_task_run_id=uuid.uuid4(), kept_check="unique_id"
                )
            )
            session.commit()
    finally:
        engine.dispose()
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
    task = {"Authorization": f"Bearer {minted.json()['token']}"}
    route = "/v1/dev/namespaces/gold/tables/sales"

    refused = client.get(route, headers=task)
    person = client.get(route, headers=AUTH_HEADER)

    assert refused.status_code == 404, refused.text
    assert refused.json()["error"]["type"] == "KeptAfterCheckError"
    assert person.status_code == 200, person.text


def test_an_iceberg_rest_create_claims_its_location_and_publishes_it(
    client: TestClient, storage_root: str, settings: Settings
) -> None:
    """The create writes its metadata before the row exists, so the location is claimed first.

    After the create the claim is LIVE and names the table, which is what keeps the location
    sweep away from it.
    """
    _setup(client, storage_root)
    created = client.post(
        "/v1/dev/namespaces/gold/tables",
        headers=AUTH_HEADER,
        json={"name": "sales", "schema": _SCHEMA, "properties": {}},
    )
    assert created.status_code == 200, created.text
    location = created.json()["metadata"]["location"]
    engine = create_engine(settings.database)
    with Session(engine) as session:
        record = session.execute(select(TableLocation)).scalars().one()
        table_id = session.execute(select(Table.id).where(Table.name == "sales")).scalar_one()
    engine.dispose()
    assert (record.location, record.state, record.table_id) == (
        location,
        TableLocationState.LIVE,
        table_id,
    )
