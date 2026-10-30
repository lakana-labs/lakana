"""Tests for the tables platform API — list, read and drop.

`create_table`/`append_table_data` have their own test module,
`test_tables_append.py`; this one keeps the Iceberg-router-created-table
path — `_create_env_and_namespace` plus a raw `POST /v1/.../tables` call —
for `get_table`'s success path and the other list/drop tests below, which
predate the platform API's own JSON-schema create route and don't need it.
`_to_out` is unit-tested directly below, the same way `test_deps.py` tests
`lakana_server.deps` functions independently of any route — a stand-in that
carries only the attributes `_to_out` actually reads, not a real
`CatalogService.load_table` result.
"""

import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import cast

from fastapi.testclient import TestClient
from pyiceberg.schema import Schema
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from lakana_core.catalog.model_metadata import write_model_entry
from lakana_core.catalog.service import LoadedTable
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.models import Environment, Namespace, Table, TableType
from lakana_server.api.platform.schemas import TableOut
from lakana_server.api.platform.tables import _to_out
from lakana_server.settings import Settings
from tests.conftest import AUTH_HEADER
from tests.server_test_helpers import create_environment, create_table


def _create_env_and_namespace(
    client: TestClient,
    storage_root: str,
    env: str = "dev",
    path: str = "gold",
) -> None:
    """Create an environment and a namespace inside it.

    Parameters
    ----------
    client
        The test client.
    env
        Environment name.
    path
        Dot-joined namespace path.
    storage_root
        The environment's storage root URI.
    """
    client.post(
        "/api/v1/environments",
        headers=AUTH_HEADER,
        json={
            "name": env,
            "storage": {"scheme": "file", "root": storage_root, "options": {}},
        },
    )
    client.post(f"/api/v1/environments/{env}/namespaces", headers=AUTH_HEADER, json={"path": path})


def test_list_tables_is_empty_for_a_fresh_namespace(client: TestClient, storage_root: str) -> None:
    """Listing a namespace with no tables returns an empty list, not a 404."""
    _create_env_and_namespace(client, storage_root)
    response = client.get("/api/v1/environments/dev/namespaces/gold/tables", headers=AUTH_HEADER)
    assert response.status_code == 200
    assert response.json() == []


def test_get_unknown_table_is_404(client: TestClient, storage_root: str) -> None:
    """A missing table is a 404, not a 500 or an empty 200."""
    _create_env_and_namespace(client, storage_root)
    response = client.get(
        "/api/v1/environments/dev/namespaces/gold/tables/nope", headers=AUTH_HEADER
    )
    assert response.status_code == 404


def test_a_table_created_through_the_iceberg_router_is_visible_on_the_platform_api(
    client: TestClient, storage_root: str
) -> None:
    """Proves the two create paths land in the same catalog.

    A table created through the Iceberg router (the only way to create one
    before the platform API had its own table writes, and still the only way to commit new
    *metadata updates* to
    an existing table) is visible through the platform API's own list/get
    routes — the same catalog rows back both surfaces, whichever route
    created the row.
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

    listed = client.get("/api/v1/environments/dev/namespaces/gold/tables", headers=AUTH_HEADER)
    assert listed.status_code == 200
    assert [t["name"] for t in listed.json()] == ["sales"]

    fetched = client.get(
        "/api/v1/environments/dev/namespaces/gold/tables/sales", headers=AUTH_HEADER
    )
    assert fetched.status_code == 200
    assert fetched.json()["name"] == "sales"


def test_to_out_builds_the_response_body_from_a_loaded_table() -> None:
    """`_to_out` reads `ref.name`, `uuid`, `metadata_location` and the schema off its argument."""
    ref = TableRef(environment="dev", namespace=NamespacePath.from_parts(["gold"]), name="sales")
    table_uuid = uuid.uuid4()
    loaded = cast(
        LoadedTable,
        SimpleNamespace(
            ref=ref,
            uuid=table_uuid,
            metadata_location="file:///tmp/x.json",
            metadata=SimpleNamespace(schema=lambda: Schema(), current_snapshot_id=42),
        ),
    )
    assert _to_out("gold", loaded) == TableOut(
        name="sales",
        namespace="gold",
        uuid=str(table_uuid),
        metadata_location="file:///tmp/x.json",
        columns=[],
        snapshot_id=42,
    )


def test_get_table_reports_its_schema(client: TestClient) -> None:
    """`TableOut` carries the table's columns in the JSON column form."""
    create_environment(client)
    created = client.post(
        "/api/v1/environments/dev/namespaces/bronze/tables",
        headers=AUTH_HEADER,
        json={
            "name": "sales",
            "schema": [
                {"name": "id", "type": "long", "nullable": False},
                {"name": "amount", "type": "decimal(10,2)", "nullable": True},
            ],
        },
    )
    assert created.status_code == 201, created.text

    response = client.get(
        "/api/v1/environments/dev/namespaces/bronze/tables/sales", headers=AUTH_HEADER
    )

    assert response.status_code == 200, response.text
    assert [
        {k: v for k, v in column.items() if k in ("name", "type", "nullable")}
        for column in response.json()["schema"]
    ] == [
        {"name": "id", "type": "long", "nullable": False},
        {"name": "amount", "type": "decimal(10,2)", "nullable": True},
    ]
    assert created.json()["schema"] == response.json()["schema"]


def test_drop_table_removes_it(client: TestClient, storage_root: str) -> None:
    """The contract's `DELETE .../tables/{name}` row, exercised end-to-end.

    Creation still has to go through the Iceberg router — that is the one
    write path for table *content*. Deletion is a catalog-lifecycle act, the
    same kind the platform API already exposes for environments and
    namespaces, so it lives here too.
    """
    _create_env_and_namespace(client, storage_root)
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

    dropped = client.delete(
        "/api/v1/environments/dev/namespaces/gold/tables/sales", headers=AUTH_HEADER
    )
    assert dropped.status_code == 204, dropped.text

    assert (
        client.get(
            "/api/v1/environments/dev/namespaces/gold/tables/sales", headers=AUTH_HEADER
        ).status_code
        == 404
    )
    # The Iceberg router reads the same catalog rows, so a table dropped on
    # one surface must be gone on the other — the two are one catalog, not
    # two views that can drift.
    assert (
        client.get("/v1/dev/namespaces/gold/tables/sales", headers=AUTH_HEADER).status_code == 404
    )


def test_drop_unknown_table_is_404(client: TestClient, storage_root: str) -> None:
    """Dropping a table that never existed is a 404, not a silent 204."""
    _create_env_and_namespace(client, storage_root)
    response = client.delete(
        "/api/v1/environments/dev/namespaces/gold/tables/nope", headers=AUTH_HEADER
    )
    assert response.status_code == 404


def test_register_external_table_infers_and_returns_the_schema(
    client: TestClient, tmp_path: Path
) -> None:
    """Registering a raw-zone table returns its inferred schema."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    _create_env_and_namespace(client, path="raw", storage_root=f"file://{tmp_path}")
    (tmp_path / "raw" / "events").mkdir(parents=True)
    pq.write_table(pa.table({"id": [1]}), tmp_path / "raw" / "events" / "a.parquet")

    response = client.post(
        "/api/v1/environments/dev/namespaces/raw/tables/external",
        json={"name": "events", "location_pattern": "raw/events/*.parquet", "format": "parquet"},
        headers=AUTH_HEADER,
    )

    assert response.status_code == 201, response.text
    assert response.json() == {
        "name": "events",
        "namespace": "raw",
        "location_pattern": "raw/events/*.parquet",
        "format": "parquet",
        "schema": {"id": "int64"},
    }


def test_register_external_table_with_no_matching_files_is_404(
    client: TestClient, tmp_path: Path
) -> None:
    """No file matching the pattern is a 404, not a 500."""
    _create_env_and_namespace(client, path="raw", storage_root=f"file://{tmp_path}")
    (tmp_path / "raw" / "events").mkdir(parents=True)

    response = client.post(
        "/api/v1/environments/dev/namespaces/raw/tables/external",
        json={"name": "events", "location_pattern": "raw/events/*.parquet", "format": "parquet"},
        headers=AUTH_HEADER,
    )

    assert response.status_code == 404


def test_register_external_table_with_a_path_traversal_pattern_is_422(
    client: TestClient, tmp_path: Path
) -> None:
    """A location pattern escaping the environment's storage root is a 422."""
    _create_env_and_namespace(client, path="raw", storage_root=f"file://{tmp_path}")

    response = client.post(
        "/api/v1/environments/dev/namespaces/raw/tables/external",
        json={"name": "evil", "location_pattern": "../../etc/*.parquet", "format": "parquet"},
        headers=AUTH_HEADER,
    )

    assert response.status_code == 422


def test_external_table_outside_raw_is_refused(client: TestClient, tmp_path: Path) -> None:
    """A pattern not shaped `raw/<dir…>/<glob>` is refused, even inside the storage root."""
    _create_env_and_namespace(client, path="gold", storage_root=f"file://{tmp_path}")

    response = client.post(
        "/api/v1/environments/dev/namespaces/gold/tables/external",
        json={
            "name": "customers",
            "location_pattern": "managed/gold/customers/*.parquet",
            "format": "parquet",
        },
        headers=AUTH_HEADER,
    )

    assert response.status_code == 422


def test_disjoint_claims_are_both_allowed(client: TestClient, tmp_path: Path) -> None:
    """Two external tables over non-overlapping `raw/` directories both register cleanly."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    _create_env_and_namespace(client, path="raw", storage_root=f"file://{tmp_path}")
    (tmp_path / "raw" / "customers").mkdir(parents=True)
    pq.write_table(pa.table({"id": [1]}), tmp_path / "raw" / "customers" / "a.parquet")
    (tmp_path / "raw" / "orders").mkdir(parents=True)
    pq.write_table(pa.table({"id": [1]}), tmp_path / "raw" / "orders" / "a.parquet")

    first = client.post(
        "/api/v1/environments/dev/namespaces/raw/tables/external",
        json={
            "name": "customers",
            "location_pattern": "raw/customers/*.parquet",
            "format": "parquet",
        },
        headers=AUTH_HEADER,
    )
    assert first.status_code == 201, first.text

    second = client.post(
        "/api/v1/environments/dev/namespaces/raw/tables/external",
        json={"name": "orders", "location_pattern": "raw/orders/*.parquet", "format": "parquet"},
        headers=AUTH_HEADER,
    )
    assert second.status_code == 201, second.text


def test_103_exploit_a_second_claim_over_another_tables_raw_directory_is_refused(
    client: TestClient, tmp_path: Path
) -> None:
    """The original exploit: a low-privileged principal cannot claim another table's files.

    `raw.customers` claims `raw/customers`, with an `ssn` column a
    lower-privileged principal (mallory, granted only `DATA_SCIENTIST` on
    an unrelated `sandbox` namespace) must never read. Before this fix,
    mallory could register her own external table, `sandbox.loot`, over
    the exact same `raw/customers/*.parquet` pattern (or a sub-claim
    beneath it, or any other zone under the storage root) and read the
    file under her own table's grants — a table she was never granted
    `READ_TABLE` on. `parse_external_location`'s `raw/<dir>/<glob>` shape
    plus `check_claim_free`'s overlap check close this off at create time.
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    _create_env_and_namespace(client, path="raw", storage_root=f"file://{tmp_path}")
    (tmp_path / "raw" / "customers" / "eu").mkdir(parents=True)
    pq.write_table(
        pa.table({"id": [1], "ssn": ["123-45-6789"]}), tmp_path / "raw" / "customers" / "a.parquet"
    )
    # A real file also sits under the `eu` sub-claim below, so the
    # sub-claim attempt further down is refused by the overlap check
    # itself (`check_claim_free`), not incidentally by "no file matches" —
    # a sub-claim must be refused precisely because it overlaps, whether
    # or not it happens to have its own files.
    pq.write_table(
        pa.table({"id": [1], "ssn": ["987-65-4321"]}),
        tmp_path / "raw" / "customers" / "eu" / "a.parquet",
    )
    registered = client.post(
        "/api/v1/environments/dev/namespaces/raw/tables/external",
        json={
            "name": "customers",
            "location_pattern": "raw/customers/*.parquet",
            "format": "parquet",
        },
        headers=AUTH_HEADER,
    )
    assert registered.status_code == 201, registered.text

    client.post(
        "/api/v1/environments/dev/namespaces", headers=AUTH_HEADER, json={"path": "sandbox"}
    )
    client.post("/api/v1/principals", headers=AUTH_HEADER, json={"name": "mallory", "type": "USER"})
    client.post(
        "/api/v1/grants",
        headers=AUTH_HEADER,
        json={
            "principal": "mallory",
            "role": "DATA_SCIENTIST",
            "kind": "NAMESPACE",
            "environment": "dev",
            "namespace": "sandbox",
        },
    )
    issued = client.post(
        "/api/v1/principals/mallory/tokens", headers=AUTH_HEADER, json={"name": "mallory-pat"}
    )
    assert issued.status_code == 201, issued.text
    mallory_headers = {"Authorization": f"Bearer {issued.json()['token']}"}

    same_claim = client.post(
        "/api/v1/environments/dev/namespaces/sandbox/tables/external",
        json={"name": "loot", "location_pattern": "raw/customers/*.parquet", "format": "parquet"},
        headers=mallory_headers,
    )
    assert same_claim.status_code == 422, same_claim.text
    # The message must not confirm that a table named "customers" exists
    # -- mallory has no grant to see it.
    assert "customers" not in same_claim.json()["detail"]

    outside_raw = client.post(
        "/api/v1/environments/dev/namespaces/sandbox/tables/external",
        json={
            "name": "loot2",
            "location_pattern": "managed/gold/customers/*.parquet",
            "format": "parquet",
        },
        headers=mallory_headers,
    )
    assert outside_raw.status_code == 422, outside_raw.text

    sub_claim = client.post(
        "/api/v1/environments/dev/namespaces/sandbox/tables/external",
        json={
            "name": "loot3",
            "location_pattern": "raw/customers/eu/*.parquet",
            "format": "parquet",
        },
        headers=mallory_headers,
    )
    assert sub_claim.status_code == 422, sub_claim.text


def test_103_round_2_a_legacy_row_with_a_null_claim_still_blocks_a_new_overlapping_claim(
    client: TestClient, settings: Settings, tmp_path: Path
) -> None:
    """A older `EXTERNAL` row with `external_claim=None` still fails closed.

    `tables.external_claim` is never backfilled for a row created before
    the id-binding migration (or the migration's backfill couldn't parse its
    pattern) — it stays `NULL`. Such a row must not read as "claims
    nothing": `check_claim_free` re-parses its stored `location_pattern`
    and still treats an overlapping new claim as a conflict.
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    _create_env_and_namespace(client, path="raw", storage_root=f"file://{tmp_path}")
    client.post(
        "/api/v1/environments/dev/namespaces", headers=AUTH_HEADER, json={"path": "sandbox"}
    )
    (tmp_path / "raw" / "customers").mkdir(parents=True)
    pq.write_table(
        pa.table({"id": [1], "ssn": ["123-45-6789"]}), tmp_path / "raw" / "customers" / "a.parquet"
    )

    engine = create_engine(settings.database.url)
    with Session(engine) as session:
        env = session.execute(select(Environment).where(Environment.name == "dev")).scalar_one()
        raw_ns = session.execute(
            select(Namespace).where(Namespace.environment_id == env.id, Namespace.path == "raw")
        ).scalar_one()
        session.add(
            Table(
                id=uuid.uuid4(),
                namespace_id=raw_ns.id,
                name="customers",
                table_type=TableType.EXTERNAL,
                external_config={
                    "location_pattern": "raw/customers/*.parquet",
                    "format": "parquet",
                    "schema": {"id": "int64"},
                },
                external_claim=None,
            )
        )
        session.commit()
    engine.dispose()

    client.post("/api/v1/principals", headers=AUTH_HEADER, json={"name": "mallory", "type": "USER"})
    client.post(
        "/api/v1/grants",
        headers=AUTH_HEADER,
        json={
            "principal": "mallory",
            "role": "DATA_SCIENTIST",
            "kind": "NAMESPACE",
            "environment": "dev",
            "namespace": "sandbox",
        },
    )
    issued = client.post(
        "/api/v1/principals/mallory/tokens", headers=AUTH_HEADER, json={"name": "mallory-pat"}
    )
    assert issued.status_code == 201, issued.text
    mallory_headers = {"Authorization": f"Bearer {issued.json()['token']}"}

    response = client.post(
        "/api/v1/environments/dev/namespaces/sandbox/tables/external",
        json={"name": "loot", "location_pattern": "raw/customers/*.parquet", "format": "parquet"},
        headers=mallory_headers,
    )
    assert response.status_code == 422, response.text


def test_register_external_table_with_a_dotted_name_is_422(
    client: TestClient, tmp_path: Path
) -> None:
    """A dotted table name is refused with a message naming it."""
    _create_env_and_namespace(client, path="raw", storage_root=f"file://{tmp_path}")

    response = client.post(
        "/api/v1/environments/dev/namespaces/raw/tables/external",
        json={"name": "a.b", "location_pattern": "raw/events/*.parquet", "format": "parquet"},
        headers=AUTH_HEADER,
    )

    assert response.status_code == 422, response.text
    assert "'a.b'" in response.text


def test_an_artifact_is_listed_and_described_with_its_kind(
    client: TestClient, settings: Settings
) -> None:
    create_environment(client)
    create_table(client, "dev", "gold", "churn")
    engine = create_engine(settings.database.url)
    try:
        with Session(engine) as session:
            row = session.scalars(select(Table).where(Table.name == "churn")).one()
            row.table_type = TableType.ARTIFACT
            row.metadata_location = None
            session.commit()
    finally:
        engine.dispose()
    base = "/api/v1/environments/dev/namespaces/gold/tables"
    listed = client.get(base, headers=AUTH_HEADER)
    assert listed.status_code == 200
    assert [(t["name"], t["kind"], t["schema"]) for t in listed.json()] == [
        ("churn", "ARTIFACT", [])
    ]
    one = client.get(f"{base}/churn", headers=AUTH_HEADER)
    assert (one.status_code, one.json()["kind"], one.json()["metadata_location"]) == (
        200,
        "ARTIFACT",
        None,
    )
    refused = client.delete(f"{base}/churn", headers=AUTH_HEADER)
    assert refused.status_code == 422
    assert "drop it with `lakana artifact drop gold.churn`" in refused.json()["detail"]


def _mark_not_published(settings: Settings, name: str, run_id: uuid.UUID) -> None:
    """Mark a table as not published, as the rollback of a failed first build does.

    Parameters
    ----------
    settings
        The app's settings, which name its database.
    name
        The table's name.
    run_id
        The run whose checks failed.
    """
    engine = create_engine(settings.database.url)
    try:
        with Session(engine) as session:
            row = session.scalars(select(Table).where(Table.name == name)).one()
            row.unpublished_run_id = run_id
            session.commit()
    finally:
        engine.dispose()


def _not_published_text(dotted: str, run_id: uuid.UUID) -> str:
    """Build the text of a table that is not published, with no result row left.

    Parameters
    ----------
    dotted
        The table.
    run_id
        The run.

    Returns
    -------
    str
        The text.
    """
    return (
        f"`{dotted}` has not been published: its first build did not finish, "
        f"or failed a check (run {run_id}). It is published by the next run that builds it "
        "and passes its checks."
    )


def test_table_get_shows_a_table_that_is_not_published(
    client: TestClient, settings: Settings
) -> None:
    create_environment(client)
    create_table(client, "dev", "gold", "sales")
    run_id = uuid.uuid4()
    _mark_not_published(settings, "sales", run_id)

    response = client.get(
        "/api/v1/environments/dev/namespaces/gold/tables/sales", headers=AUTH_HEADER
    )

    assert response.status_code == 200, response.text
    assert response.json()["state"] == "not_published"
    assert response.json()["state_message"] == _not_published_text("gold.sales", run_id)


def test_table_list_shows_the_state(client: TestClient, settings: Settings) -> None:
    create_environment(client)
    create_table(client, "dev", "gold", "sales")
    create_table(client, "dev", "gold", "fine")
    run_id = uuid.uuid4()
    _mark_not_published(settings, "sales", run_id)

    response = client.get("/api/v1/environments/dev/namespaces/gold/tables", headers=AUTH_HEADER)

    assert response.status_code == 200, response.text
    assert {t["name"]: (t["state"], t["state_message"]) for t in response.json()} == {
        "fine": ("published", None),
        "sales": ("not_published", _not_published_text("gold.sales", run_id)),
    }


def _mark_kept(settings: Settings, name: str, run_id: uuid.UUID, check: str) -> None:
    """Put the note of a table that was kept after a failed check, as the worker does.

    Parameters
    ----------
    settings
        The app's settings, which name its database.
    name
        The table's name.
    run_id
        The run whose check failed.
    check
        The text the state message shows for the check.
    """
    engine = create_engine(settings.database.url)
    try:
        with Session(engine) as session:
            row = session.scalars(select(Table).where(Table.name == name)).one()
            row.kept_run_id = run_id
            row.kept_task_run_id = uuid.uuid4()
            row.kept_check = check
            session.commit()
    finally:
        engine.dispose()


def _kept_text(check: str, run_id: uuid.UUID) -> str:
    """Build the state message of a kept table.

    Parameters
    ----------
    check
        The check as the message shows it.
    run_id
        The run.

    Returns
    -------
    str
        The text.
    """
    return (
        f"Kept after a failed check (`{check}`, run {run_id}): the table holds the data "
        "that failed. Tasks do not read it until its checks pass."
    )


def test_table_get_and_list_show_a_kept_table(client: TestClient, settings: Settings) -> None:
    create_environment(client)
    create_table(client, "dev", "gold", "sales")
    create_table(client, "dev", "gold", "fine")
    run_id = uuid.uuid4()
    _mark_kept(settings, "sales", run_id, "gold.sales/unique_id and 1 more")
    # The note's run left no results, so nothing tells what "and 1 more" counts (#385).
    expected = _kept_text("gold.sales/unique_id", run_id)

    got = client.get("/api/v1/environments/dev/namespaces/gold/tables/sales", headers=AUTH_HEADER)
    listed = client.get("/api/v1/environments/dev/namespaces/gold/tables", headers=AUTH_HEADER)

    assert got.status_code == 200, got.text
    assert (got.json()["state"], got.json()["state_message"]) == ("kept", expected)
    assert {t["name"]: (t["state"], t["state_message"]) for t in listed.json()} == {
        "fine": ("published", None),
        "sales": ("kept", expected),
    }


_GOLD_TABLES = "/api/v1/environments/dev/namespaces/gold/tables"


def _star(client: TestClient) -> None:
    """Create `gold.dim_store`, `gold.fact_sales` that references it, and a plain `gold.notes`.

    The model is written onto the rows as a deploy writes it, with one generated result.

    Parameters
    ----------
    client
        The test client.
    """
    create_environment(client)
    for name in ("dim_store", "fact_sales", "notes"):
        create_table(client, "dev", "gold", name)
    with Session(client.app.state.engine) as session:
        rows = {r.name: r for r in session.scalars(select(Table))}
        write_model_entry(
            session,
            rows["dim_store"],
            {
                "write": "replace",
                "key": ["store_id"],
                "model": {"kind": "dimension", "grain": "One row per store."},
            },
        )
        write_model_entry(
            session,
            rows["fact_sales"],
            {
                "write": "replace",
                "key": ["date", "store_id"],
                "model": {
                    "kind": "fact",
                    "grain": "One row per store and day.",
                    "references": [
                        {"column": "store_id", "target": "gold.dim_store", "target_column": "id"}
                    ],
                },
            },
        )
        session.commit()


def test_a_table_carries_its_model_and_a_plain_table_none(client: TestClient) -> None:
    _star(client)
    fact = client.get(f"{_GOLD_TABLES}/fact_sales", headers=AUTH_HEADER)
    plain = client.get(f"{_GOLD_TABLES}/notes", headers=AUTH_HEADER)
    assert fact.status_code == 200, fact.text
    assert fact.json()["model"] == {
        "kind": "fact",
        "grain": "One row per store and day.",
        "key": ["date", "store_id"],
        "key_none": False,
        "references": [
            {
                "id": "gold.fact_sales/ref_store_id",
                "table": "gold.fact_sales",
                "column": "store_id",
                "target": "gold.dim_store",
                "target_column": "id",
                "target_exists": True,
            }
        ],
        "hidden_references": 0,
        "checks": [],
    }
    assert plain.json()["model"] is None


def test_the_listing_filters_on_the_model_kind(client: TestClient) -> None:
    _star(client)
    facts = client.get(_GOLD_TABLES, headers=AUTH_HEADER, params={"model_kind": "fact"})
    every = client.get(_GOLD_TABLES, headers=AUTH_HEADER)
    assert facts.status_code == 200, facts.text
    assert [t["name"] for t in facts.json()] == ["fact_sales"]
    assert sorted(t["name"] for t in every.json()) == ["dim_store", "fact_sales", "notes"]


def test_an_unknown_model_kind_is_refused_naming_the_kinds(client: TestClient) -> None:
    _star(client)
    refused = client.get(_GOLD_TABLES, headers=AUTH_HEADER, params={"model_kind": "cube"})
    assert refused.status_code == 422
    assert refused.json()["detail"] == (
        "model_kind must be one of fact, dimension, wide, staging; got 'cube'"
    )


def test_the_references_route_lists_both_directions(client: TestClient) -> None:
    _star(client)
    found = client.get(f"{_GOLD_TABLES}/dim_store/references", headers=AUTH_HEADER)
    assert found.status_code == 200, found.text
    assert found.json() == {
        "table": "gold.dim_store",
        "outgoing": [],
        "hidden_outgoing": 0,
        "incoming": [
            {
                "id": "gold.fact_sales/ref_store_id",
                "table": "gold.fact_sales",
                "column": "store_id",
                "target": "gold.dim_store",
                "target_column": "id",
                "target_exists": True,
            }
        ],
        "hidden_incoming": 0,
    }


def _table_reader(client: TestClient, name: str, table: str) -> dict[str, str]:
    """Create a user holding the viewer role on one table of `gold` only.

    Parameters
    ----------
    client
        The test client.
    name
        The user's name.
    table
        The table it may read.

    Returns
    -------
    dict
        Its `Authorization` header.
    """
    client.post("/api/v1/principals", headers=AUTH_HEADER, json={"name": name, "type": "USER"})
    granted = client.post(
        "/api/v1/grants",
        headers=AUTH_HEADER,
        json={
            "principal": name,
            "role": "VIEWER",
            "kind": "TABLE",
            "environment": "dev",
            "namespace": "gold",
            "table": table,
        },
    )
    assert granted.status_code < 300, granted.text
    issued = client.post(
        f"/api/v1/principals/{name}/tokens", headers=AUTH_HEADER, json={"name": name}
    )
    return {"Authorization": f"Bearer {issued.json()['token']}"}


def test_the_references_route_needs_read_table(client: TestClient) -> None:
    _star(client)
    reader = _table_reader(client, "rita", "fact_sales")
    refused = client.get(f"{_GOLD_TABLES}/dim_store/references", headers=reader)
    assert refused.status_code == 403


def test_a_reader_of_one_table_sees_only_counts_of_the_others(client: TestClient) -> None:
    _star(client)
    of_dim = _table_reader(client, "dora", "dim_store")
    of_fact = _table_reader(client, "fred", "fact_sales")
    dim = client.get(f"{_GOLD_TABLES}/dim_store/references", headers=of_dim).json()
    fact = client.get(f"{_GOLD_TABLES}/fact_sales", headers=of_fact).json()
    assert (dim["incoming"], dim["hidden_incoming"]) == ([], 1)
    assert (fact["model"]["references"], fact["model"]["hidden_references"]) == ([], 1)
    assert "dim_store" not in str(fact)
