"""Unit tests for the SDK's data-plane resources: environments, namespaces, tables, queries."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime
from typing import Any, cast

import httpx
import polars as pl
import pyarrow as pa
import pyarrow.ipc
import pytest

from lakana._arrow import (
    _arrow_type_to_json,
    cast_timestamp_ns_columns_to_us,
    json_to_arrow_type,
    schema_to_columns,
)
from lakana._config import ResolvedConfig
from lakana._http import HttpTransport
from lakana._models import (
    AppendResult,
    Column,
    DroppedTable,
    Environment,
    ExternalTable,
    Failure,
    Lineage,
    LineageEdge,
    LineageNode,
    LoadResult,
    MergeResult,
    Namespace,
    QueryStatus,
    Table,
    TableReference,
    WaitingFor,
    WriteResult,
)
from lakana._routes import ROUTES, Route
from lakana.client import Client
from lakana.errors import Conflict, LakanaConnectionError, QueryFailed, ValidationError
from lakana.resources.environments import Environments
from lakana.resources.namespaces import Namespaces
from lakana.resources.queries import Queries, QueryRun, waiting_for_from_json
from lakana.resources.runs import failure_from_json, run_from_json
from lakana.resources.tables import Tables, _split_namespace_table

_CONFIG = ResolvedConfig(url="http://api.example", token="lakana_pat_abc_supersecret")


def _transport(handler: Callable[[httpx.Request], httpx.Response]) -> HttpTransport:
    """Build an `HttpTransport` whose own `httpx.Client` uses a mock transport.

    Parameters
    ----------
    handler
        The mock transport's request handler.

    Returns
    -------
    HttpTransport
        A transport backed by `httpx.MockTransport(handler)`.
    """
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url=_CONFIG.url)
    return HttpTransport(_CONFIG, http_client=client)


# --- Environments ------------------------------------------------------


def test_environments_create_sends_the_right_method_path_and_body() -> None:
    """`environments.create` POSTs the name and storage config."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["json"] = request.read()
        return httpx.Response(
            201,
            json={
                "name": "dev",
                "storage_root": "file:///tmp/dev",
                "storage_secret_ref": None,
                "is_protected": False,
                "properties": {},
            },
        )

    environments = Environments(_transport(handler))
    storage = {"scheme": "file", "root": "file:///tmp/dev", "options": {}}
    result = environments.create("dev", storage)

    assert seen["method"] == "POST"
    assert seen["path"] == "/api/v1/environments"
    assert result == Environment(
        name="dev",
        storage_root="file:///tmp/dev",
        storage_secret_ref=None,
        is_protected=False,
        properties={},
    )


def test_environments_get_sends_a_get_to_the_named_environment() -> None:
    """`environments.get` GETs `/api/v1/environments/{name}`."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        return httpx.Response(
            200,
            json={
                "name": "dev",
                "storage_root": "file:///tmp/dev",
                "storage_secret_ref": "s1",
                "is_protected": True,
                "properties": {"a": "b"},
            },
        )

    environments = Environments(_transport(handler))
    result = environments.get("dev")

    assert seen["method"] == "GET"
    assert seen["path"] == "/api/v1/environments/dev"
    assert result.storage_secret_ref == "s1"
    assert result.is_protected is True


def test_environments_list_parses_every_item() -> None:
    """`environments.list` GETs the collection and parses each item."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/environments"
        return httpx.Response(
            200,
            json=[
                {
                    "name": "dev",
                    "storage_root": "file:///tmp/dev",
                    "storage_secret_ref": None,
                    "is_protected": False,
                    "properties": {},
                }
            ],
        )

    environments = Environments(_transport(handler))
    result = environments.list()

    assert result == [
        Environment(
            name="dev",
            storage_root="file:///tmp/dev",
            storage_secret_ref=None,
            is_protected=False,
            properties={},
        )
    ]


_DELETED_ENV = {
    "name": "dev",
    "storage_root": "file:///tmp/dev",
    "storage_secret_ref": None,
    "is_protected": False,
    "properties": {},
    "retention_days": None,
    "deleted_at": "2026-10-20T10:00:00+00:00",
    "purge_after": "2026-10-27T10:00:00+00:00",
}


def test_environments_delete_sends_a_delete_and_returns_the_undo_period() -> None:
    """`environments.delete` DELETEs `/api/v1/environments/{name}`, `purge` as a parameter."""
    seen: list[tuple[str, str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, str(request.url.params)))
        return httpx.Response(200, json=_DELETED_ENV)

    environments = Environments(_transport(handler))
    deleted = environments.delete("dev")
    environments.delete("dev", purge=True)

    assert seen == [
        ("DELETE", "/api/v1/environments/dev", ""),
        ("DELETE", "/api/v1/environments/dev", "purge=true"),
    ]
    assert deleted is not None
    assert deleted.purge_after == datetime(2026, 10, 27, 10, tzinfo=UTC)
    assert deleted.deleted_at == datetime(2026, 10, 20, 10, tzinfo=UTC)


def test_environments_delete_accepts_an_older_servers_empty_answer() -> None:
    """A server from before the undo period answers 204 without a body: `None`."""
    environments = Environments(_transport(lambda request: httpx.Response(204)))
    assert environments.delete("dev") is None


def test_environments_restore_posts_and_list_deleted_asks_for_them() -> None:
    """`restore` POSTs `/restore`; `list(deleted=True)` sends `deleted=true`."""
    seen: list[tuple[str, str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, str(request.url.params)))
        if request.method == "POST":
            return httpx.Response(
                200, json={**_DELETED_ENV, "deleted_at": None, "purge_after": None}
            )
        return httpx.Response(200, json=[_DELETED_ENV])

    environments = Environments(_transport(handler))
    restored = environments.restore("dev")
    (listed,) = environments.list(deleted=True)

    assert seen == [
        ("POST", "/api/v1/environments/dev/restore", ""),
        ("GET", "/api/v1/environments", "deleted=true"),
    ]
    assert (restored.deleted_at, restored.purge_after) == (None, None)
    assert listed.purge_after == datetime(2026, 10, 27, 10, tzinfo=UTC)


def test_environments_update_storage_sends_a_patch_with_the_given_fields() -> None:
    """`environments.update_storage` PATCHes `/storage` with options/secret_ref."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["body"] = request.read()
        return httpx.Response(
            200,
            json={
                "name": "dev",
                "storage_root": "file:///tmp/dev",
                "storage_secret_ref": "new-secret",
                "is_protected": False,
                "properties": {},
            },
        )

    environments = Environments(_transport(handler))
    result = environments.update_storage("dev", options={"k": "v"}, secret_ref="new-secret")

    assert seen["method"] == "PATCH"
    assert seen["path"] == "/api/v1/environments/dev/storage"
    assert b'"options"' in seen["body"]
    assert result.storage_secret_ref == "new-secret"


def test_environments_set_protection_sends_a_patch_with_is_protected() -> None:
    """`environments.set_protection` PATCHes `/api/v1/environments/{name}`."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["body"] = request.read()
        return httpx.Response(
            200,
            json={
                "name": "dev",
                "storage_root": "file:///tmp/dev",
                "storage_secret_ref": None,
                "is_protected": True,
                "properties": {},
            },
        )

    environments = Environments(_transport(handler))
    result = environments.set_protection("dev", True)

    assert seen["method"] == "PATCH"
    assert seen["path"] == "/api/v1/environments/dev"
    assert seen["body"] == b'{"is_protected":true}'
    assert result.is_protected is True


# --- Namespaces ----------------------------------------------------------


def test_namespaces_create_sends_the_dotted_path() -> None:
    """`namespaces.create` POSTs the dotted path and properties."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["body"] = request.read()
        return httpx.Response(201, json={"path": "gold.sales", "properties": {}})

    namespaces = Namespaces(_transport(handler))
    result = namespaces.create("dev", "gold.sales")

    assert seen["method"] == "POST"
    assert seen["path"] == "/api/v1/environments/dev/namespaces"
    assert seen["body"] == b'{"path":"gold.sales","properties":{}}'
    assert result == Namespace(path="gold.sales", properties={})


def test_namespaces_create_passes_properties_through() -> None:
    """`namespaces.create` includes given properties in the body."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.read() == b'{"path":"gold.sales","properties":{"owner":"alice"}}'
        return httpx.Response(201, json={"path": "gold.sales", "properties": {"owner": "alice"}})

    namespaces = Namespaces(_transport(handler))
    namespaces.create("dev", "gold.sales", properties={"owner": "alice"})


def test_namespaces_get_sends_the_dotted_path_as_a_single_segment() -> None:
    """`namespaces.get` GETs `.../namespaces/{path}` with dots preserved."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        return httpx.Response(200, json={"path": "gold.sales", "properties": {}})

    namespaces = Namespaces(_transport(handler))
    result = namespaces.get("dev", "gold.sales")

    assert seen["path"] == "/api/v1/environments/dev/namespaces/gold.sales"
    assert result.path == "gold.sales"


def test_namespaces_list_parses_every_item() -> None:
    """`namespaces.list` GETs the collection and parses each item."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/environments/dev/namespaces"
        return httpx.Response(200, json=[{"path": "gold", "properties": {}}])

    namespaces = Namespaces(_transport(handler))
    result = namespaces.list("dev")

    assert result == [Namespace(path="gold", properties={})]


def test_namespaces_delete_sends_a_delete() -> None:
    """`namespaces.delete` DELETEs `.../namespaces/{path}`."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        return httpx.Response(204)

    namespaces = Namespaces(_transport(handler))
    namespaces.delete("dev", "gold.sales")

    assert seen["method"] == "DELETE"
    assert seen["path"] == "/api/v1/environments/dev/namespaces/gold.sales"


# --- Tables: namespace/table splitting -----------------------------------


@pytest.mark.parametrize(
    ("qualified_name", "expected"),
    [
        ("bronze.sales", ("bronze", "sales")),
        ("gold.finance.sales", ("gold.finance", "sales")),
    ],
)
def test_split_namespace_table_splits_on_the_last_dot(
    qualified_name: str, expected: tuple[str, str]
) -> None:
    """The namespace and table split on the final `.`, keeping a dotted namespace intact."""
    assert _split_namespace_table(qualified_name) == expected


def test_split_namespace_table_without_a_dot_is_a_validation_error() -> None:
    """A name with no `.` at all cannot be split into namespace and table."""
    with pytest.raises(ValidationError):
        _split_namespace_table("sales")


# --- Tables: list/get/delete ----------------------------------------------


def test_tables_list_sends_the_namespace_path() -> None:
    """`tables.list` GETs `.../namespaces/{path}/tables`."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/environments/dev/namespaces/gold/tables"
        return httpx.Response(
            200,
            json=[
                {
                    "name": "sales",
                    "namespace": "gold",
                    "uuid": "u1",
                    "metadata_location": "loc1",
                    "schema": [],
                }
            ],
        )

    tables = Tables(_transport(handler))
    result = tables.list("dev", "gold")

    assert result == [Table(name="sales", namespace="gold", uuid="u1", metadata_location="loc1")]


def test_tables_get_splits_the_qualified_name_into_the_url() -> None:
    """`tables.get` splits `"namespace.table"` across the path and `{name}`."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/environments/dev/namespaces/bronze/tables/sales"
        return httpx.Response(
            200,
            json={
                "name": "sales",
                "namespace": "bronze",
                "uuid": "u1",
                "metadata_location": "loc1",
                "schema": [],
            },
        )

    tables = Tables(_transport(handler))
    result = tables.get("dev", "bronze.sales")

    assert result.name == "sales"


def test_tables_get_parses_the_schema_into_columns() -> None:
    """`Table.columns` carries the server's `schema` list."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "name": "sales",
                "namespace": "bronze",
                "uuid": "u1",
                "metadata_location": "file:///m.json",
                "schema": [{"name": "id", "type": "long", "nullable": False}],
            },
        )

    table = Tables(_transport(handler)).get("dev", "bronze.sales")

    assert table.columns == [Column(name="id", type="long", nullable=False)]


def test_tables_get_carries_the_descriptions() -> None:
    """`Table` and `Column` hold the description and where it came from; older servers send none."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "name": "sales",
                "namespace": "bronze",
                "uuid": "u1",
                "metadata_location": "file:///m.json",
                "description": "Orders",
                "description_source": "project",
                "schema": [
                    {
                        "name": "id",
                        "type": "long",
                        "nullable": False,
                        "description": "Key",
                        "description_source": "edited",
                    },
                    {"name": "note", "type": "string", "nullable": True},
                ],
            },
        )

    table = Tables(_transport(handler)).get("dev", "bronze.sales")

    assert (table.description, table.description_source) == ("Orders", "project")
    assert [(c.description, c.description_source) for c in table.columns] == [
        ("Key", "edited"),
        (None, None),
    ]


def test_tables_describe_patches_the_table_and_resets_with_none() -> None:
    """`describe` PATCHes `{"description": text}`; `None` is sent as `null`."""
    seen: list[tuple[str, str, bytes]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.content))
        return httpx.Response(
            200,
            json={
                "name": "sales",
                "namespace": "bronze",
                "uuid": "u1",
                "metadata_location": None,
                "description": "Orders",
                "description_source": "edited",
                "schema": [],
            },
        )

    tables = Tables(_transport(handler))
    table = tables.describe("dev", "bronze.sales", "Orders")
    tables.describe("dev", "bronze.sales", None)

    assert table.description == "Orders"
    path = "/api/v1/environments/dev/namespaces/bronze/tables/sales"
    assert [(m, p) for m, p, _ in seen] == [("PATCH", path), ("PATCH", path)]
    assert [json.loads(body) for _, _, body in seen] == [
        {"description": "Orders"},
        {"description": None},
    ]


def test_tables_describe_with_a_column_puts_to_the_columns_route() -> None:
    """`describe(..., column=...)` PUTs `.../columns/{column}/description`."""
    seen: list[tuple[str, str, bytes]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.content))
        return httpx.Response(
            200,
            json={
                "name": "sales",
                "namespace": "bronze",
                "uuid": "u1",
                "metadata_location": None,
                "schema": [],
            },
        )

    Tables(_transport(handler)).describe("dev", "bronze.sales", "Key", column="id")

    method, path, body = seen[0]
    assert (method, path) == (
        "PUT",
        "/api/v1/environments/dev/namespaces/bronze/tables/sales/columns/id/description",
    )
    assert json.loads(body) == {"description": "Key"}


def test_tables_delete_sends_a_delete() -> None:
    """`tables.delete` DELETEs `.../namespaces/{path}/tables/{name}`."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        return httpx.Response(204)

    tables = Tables(_transport(handler))
    tables.delete("dev", "bronze.sales")

    assert seen["method"] == "DELETE"
    assert seen["path"] == "/api/v1/environments/dev/namespaces/bronze/tables/sales"


def test_tables_delete_with_purge_sends_it_as_a_query_parameter() -> None:
    """`purge=True` asks for `?purge=true`; without it no parameter is sent."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url.query, "ascii"))
        return httpx.Response(204)

    tables = Tables(_transport(handler))
    tables.delete("dev", "bronze.sales", purge=True)
    tables.delete("dev", "bronze.sales", purge=True, break_glass_reason="incident 1: by hand")

    assert seen == ["purge=true", "purge=true&break_glass_reason=incident+1%3A+by+hand"]


def test_tables_undrop_posts_the_new_name_and_returns_the_table() -> None:
    """`undrop` POSTs `.../tables/{name}/undrop` with `new_name` and the break-glass reason."""
    seen: list[tuple[str, str, dict[str, Any]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, json.loads(request.content)))
        return httpx.Response(
            200,
            json={
                "name": "sales_before",
                "namespace": "bronze",
                "uuid": "u1",
                "metadata_location": "file:///m.json",
                "schema": [],
            },
        )

    tables = Tables(_transport(handler))
    restored = tables.undrop("dev", "bronze.sales", new_name="sales_before")
    tables.undrop("dev", "bronze.sales")

    assert (restored.name, restored.uuid) == ("sales_before", "u1")
    path = "/api/v1/environments/dev/namespaces/bronze/tables/sales/undrop"
    assert seen == [
        ("POST", path, {"new_name": "sales_before", "break_glass_reason": None}),
        ("POST", path, {"new_name": None, "break_glass_reason": None}),
    ]


def test_tables_list_dropped_reads_the_dropped_tables_route() -> None:
    """`list_dropped` GETs `.../namespaces/{path}/dropped-tables` and parses the times."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(
            200,
            json=[
                {
                    "name": "sales",
                    "namespace": "bronze",
                    "uuid": "u1",
                    "kind": "TABLE",
                    "dropped_at": "2026-10-19T12:00:00Z",
                    "purge_after": "2026-10-26T12:00:00Z",
                    "restorable": True,
                }
            ],
        )

    (dropped,) = Tables(_transport(handler)).list_dropped("dev", "bronze")

    assert seen == ["/api/v1/environments/dev/namespaces/bronze/dropped-tables"]
    assert dropped == DroppedTable(
        name="sales",
        namespace="bronze",
        uuid="u1",
        kind="TABLE",
        dropped_at=datetime.fromisoformat("2026-10-19T12:00:00+00:00"),
        purge_after=datetime.fromisoformat("2026-10-26T12:00:00+00:00"),
        restorable=True,
    )


# --- Tables: create --------------------------------------------------------


def test_tables_create_converts_a_pyarrow_schema() -> None:
    """`tables.create` converts a `pa.Schema` via `schema_to_columns`."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = request.read()
        return httpx.Response(
            201,
            json={
                "name": "sales",
                "namespace": "bronze",
                "uuid": "u1",
                "metadata_location": "loc",
                "schema": [],
            },
        )

    tables = Tables(_transport(handler))
    schema = pa.schema([pa.field("id", pa.int64(), nullable=False)])
    result = tables.create("dev", "bronze.sales", schema=schema)

    assert seen["path"] == "/api/v1/environments/dev/namespaces/bronze/tables"
    assert seen["body"] == (
        b'{"name":"sales","schema":[{"name":"id","type":"long","nullable":false}]}'
    )
    assert result.name == "sales"


def test_tables_create_passes_a_json_shaped_schema_through_unmodified() -> None:
    """`tables.create` sends an already-JSON-shaped column list as-is."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.read() == (
            b'{"name":"sales","schema":[{"name":"id","type":"long","nullable":false}]}'
        )
        return httpx.Response(
            201,
            json={
                "name": "sales",
                "namespace": "bronze",
                "uuid": "u1",
                "metadata_location": "loc",
                "schema": [],
            },
        )

    tables = Tables(_transport(handler))
    tables.create("dev", "bronze.sales", schema=[{"name": "id", "type": "long", "nullable": False}])


def test_tables_create_external_sends_the_expected_body() -> None:
    """`tables.create_external` POSTs to `.../tables/external`."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/environments/dev/namespaces/raw/tables/external"
        assert request.read() == (
            b'{"name":"landing","location_pattern":"raw/landing/*.parquet","format":"parquet"}'
        )
        return httpx.Response(
            201,
            json={
                "name": "landing",
                "namespace": "raw",
                "location_pattern": "raw/landing/*.parquet",
                "format": "parquet",
                "schema": {"id": "long"},
            },
        )

    tables = Tables(_transport(handler))
    result = tables.create_external("dev", "raw.landing", "raw/landing/*.parquet", "parquet")

    assert result == ExternalTable(
        name="landing",
        namespace="raw",
        location_pattern="raw/landing/*.parquet",
        format="parquet",
        schema={"id": "long"},
    )


# --- Tables: append --------------------------------------------------------


def test_tables_append_sends_the_arrow_stream_content_type_and_body() -> None:
    """`tables.append` sends the exact Arrow-stream Content-Type and a decodable body."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["content_type"] = request.headers["content-type"]
        seen["method"] = request.method
        seen["path"] = request.url.path
        with pyarrow.ipc.open_stream(pa.BufferReader(request.content)) as reader:
            seen["table"] = reader.read_all()
        return httpx.Response(200, json={"rows": 2, "files": 1, "snapshot_id": 42})

    tables = Tables(_transport(handler))
    data = pa.table({"id": [1, 2]})
    result = tables.append("dev", "bronze.sales", data)

    assert seen["method"] == "POST"
    assert seen["path"] == "/api/v1/environments/dev/namespaces/bronze/tables/sales/data"
    assert seen["content_type"] == "application/vnd.apache.arrow.stream"
    assert seen["table"].equals(data)
    assert result == AppendResult(rows=2, files=1, snapshot_id=42)


def test_tables_append_accepts_a_record_batch() -> None:
    """`tables.append` accepts a bare `pa.RecordBatch`, not just a `pa.Table`."""
    batch = pa.record_batch({"id": [1]})

    def handler(request: httpx.Request) -> httpx.Response:
        with pyarrow.ipc.open_stream(pa.BufferReader(request.content)) as reader:
            assert reader.read_all().equals(pa.Table.from_batches([batch]))
        return httpx.Response(200, json={"rows": 1, "files": 1, "snapshot_id": 1})

    tables = Tables(_transport(handler))
    tables.append("dev", "bronze.sales", batch)


def test_tables_append_accepts_anything_pa_table_accepts() -> None:
    """`tables.append` accepts a plain dict of columns, coerced via `pa.table()`."""

    def handler(request: httpx.Request) -> httpx.Response:
        with pyarrow.ipc.open_stream(pa.BufferReader(request.content)) as reader:
            assert reader.read_all().column("id").to_pylist() == [1, 2]
        return httpx.Response(200, json={"rows": 2, "files": 1, "snapshot_id": 1})

    tables = Tables(_transport(handler))
    tables.append("dev", "bronze.sales", {"id": [1, 2]})


def test_tables_append_retries_exactly_once_on_conflict() -> None:
    """A single `Conflict` is retried once, and the retry's success is returned."""
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(409, json={"detail": "conflict"})
        return httpx.Response(200, json={"rows": 1, "files": 1, "snapshot_id": 7})

    tables = Tables(_transport(handler))
    result = tables.append("dev", "bronze.sales", pa.table({"id": [1]}))

    assert attempts["n"] == 2
    assert result.snapshot_id == 7


def test_tables_append_raises_conflict_if_the_retry_also_conflicts() -> None:
    """A second consecutive `Conflict` is not retried again — it propagates."""
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(409, json={"detail": "conflict"})

    tables = Tables(_transport(handler))
    with pytest.raises(Conflict):
        tables.append("dev", "bronze.sales", pa.table({"id": [1]}))

    assert attempts["n"] == 2


def test_tables_append_sends_batches_of_at_most_65536_rows() -> None:
    """A table larger than one batch is split so no IPC message exceeds 65,536 rows.

    The server bounds cumulative batches-times-
    columns; capping rows-per-batch on the SDK side keeps a normal, even
    very large, single-column upload's batch count (and thus its
    contribution to that cap) far below what would ever be a problem,
    without the caller needing to think about chunking at all.
    """
    seen_batch_sizes: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        with pyarrow.ipc.open_stream(pa.BufferReader(request.content)) as reader:
            for batch in reader:
                seen_batch_sizes.append(batch.num_rows)
        return httpx.Response(200, json={"rows": 200_000, "files": 1, "snapshot_id": 1})

    tables = Tables(_transport(handler))
    data = pa.table({"id": list(range(200_000))})

    tables.append("dev", "bronze.sales", data)

    assert seen_batch_sizes, "no batches were sent"
    assert all(size <= 65_536 for size in seen_batch_sizes)
    assert sum(seen_batch_sizes) == 200_000


def test_tables_append_sends_a_single_chunk_worth_of_data_when_small() -> None:
    """A table well under the batch-row bound is still sent as exactly one batch."""
    seen_batch_sizes: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        with pyarrow.ipc.open_stream(pa.BufferReader(request.content)) as reader:
            for batch in reader:
                seen_batch_sizes.append(batch.num_rows)
        return httpx.Response(200, json={"rows": 3, "files": 1, "snapshot_id": 1})

    tables = Tables(_transport(handler))
    tables.append("dev", "bronze.sales", pa.table({"id": [1, 2, 3]}))

    assert seen_batch_sizes == [3]


def test_tables_append_uses_its_own_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """`tables.append` sends its own 300s timeout, not the transport's general default."""
    seen_timeouts: list[float | None] = []
    real_request = HttpTransport.request

    def spying_request(
        self: HttpTransport,
        route: Route,
        *,
        path_params: Mapping[str, str] | None = None,
        params: Mapping[str, object] | None = None,
        json: object | None = None,
        content: bytes | Iterator[bytes] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> httpx.Response:
        seen_timeouts.append(timeout)
        return real_request(
            self,
            route,
            path_params=path_params,
            params=params,
            json=json,
            content=content,
            headers=headers,
            timeout=timeout,
        )

    monkeypatch.setattr(HttpTransport, "request", spying_request)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"rows": 1, "files": 1, "snapshot_id": 1})

    tables = Tables(_transport(handler))
    tables.append("dev", "bronze.sales", pa.table({"id": [1]}))

    assert seen_timeouts == [300.0]


def test_tables_replace_puts_the_arrow_stream_with_its_options() -> None:
    """`tables.replace` PUTs the stream, with `evolve_schema` and one `source` per source."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["params"] = request.url.params.multi_items()
        with pyarrow.ipc.open_stream(pa.BufferReader(request.content)) as reader:
            seen["table"] = reader.read_all()
        return httpx.Response(
            200,
            json={
                "rows": 2,
                "files": 1,
                "snapshot_id": 9,
                "schema_diff": {"added": [{"name": "n", "type": "long"}]},
            },
        )

    data = pa.table({"id": [1, 2]})
    result = Tables(_transport(handler)).replace(
        "dev", "gold.daily", data, evolve_schema=True, sources=["bronze.a", "bronze.b"]
    )

    assert seen["method"] == "PUT"
    assert seen["path"] == "/api/v1/environments/dev/namespaces/gold/tables/daily/data"
    assert seen["params"] == [
        ("evolve_schema", "true"),
        ("source", "bronze.a"),
        ("source", "bronze.b"),
    ]
    assert seen["table"].equals(data)
    assert result == WriteResult(
        rows=2, files=1, snapshot_id=9, schema_diff={"added": [{"name": "n", "type": "long"}]}
    )


def test_tables_replace_retries_exactly_once_on_conflict() -> None:
    """A `Conflict` proves nothing committed, so one retry is safe, as for `append`."""
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(409, json={"detail": "conflict"})
        return httpx.Response(200, json={"rows": 1, "files": 1, "snapshot_id": 3})

    result = Tables(_transport(handler)).replace("dev", "gold.daily", pa.table({"id": [1]}))

    assert attempts["n"] == 2
    assert result.snapshot_id == 3
    assert result.schema_diff is None


def test_tables_append_sends_one_source_parameter_per_source() -> None:
    """`sources=` becomes a repeated `source` query parameter; none when omitted."""
    seen: list[list[tuple[str, str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params.multi_items())
        return httpx.Response(200, json={"rows": 1, "files": 1, "snapshot_id": 1})

    tables = Tables(_transport(handler))
    tables.append("dev", "gold.daily", pa.table({"id": [1]}), sources=["bronze.a"])
    tables.append("dev", "gold.daily", pa.table({"id": [1]}))

    assert seen == [[("source", "bronze.a")], []]


def test_replace_and_append_send_source_versions_sorted() -> None:
    """`source_versions=` becomes a repeated `source_version` parameter, sorted by name."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"rows": 1, "files": 1, "snapshot_id": 1})

    tables = Tables(_transport(handler))
    data = pa.table({"id": [1]})
    tables.replace(
        "dev",
        "gold.scores",
        data,
        sources=["models.b", "models.a"],
        source_versions={"models.b": 2, "models.a": 7},
    )
    tables.append("dev", "gold.scores", data, source_versions={"models.a": 1})
    tables.append("dev", "gold.scores", data, source_versions={})
    assert seen[0].url.params.get_list("source_version") == ["7:models.a", "2:models.b"]
    assert seen[1].url.params.get_list("source_version") == ["1:models.a"]
    assert "source_version" not in seen[2].url.params


# --- Tables: merge ---------------------------------------------------------


def test_tables_merge_posts_the_stream_with_the_key_repeated() -> None:
    """`tables.merge` POSTs to `…/merge` with one `key` per column and returns the counts."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["params"] = request.url.params.multi_items()
        seen["content_type"] = request.headers["content-type"]
        with pyarrow.ipc.open_stream(pa.BufferReader(request.content)) as reader:
            seen["table"] = reader.read_all()
        return httpx.Response(200, json={"snapshot_id": 11, "rows_inserted": 3, "rows_updated": 2})

    data = pa.table({"id": [1, 2], "region": ["eu", "us"]})
    result = Tables(_transport(handler)).merge(
        "dev",
        "silver.customers",
        data,
        key=["id", "region"],
        sources=["bronze.raw"],
        source_versions={"models.a": 4},
        break_glass_reason="incident 4: fix two customers",
        expected_snapshot_id=10,
    )

    assert seen["method"] == "POST"
    assert seen["path"] == "/api/v1/environments/dev/namespaces/silver/tables/customers/merge"
    assert seen["content_type"] == "application/vnd.apache.arrow.stream"
    assert seen["params"] == [
        ("key", "id"),
        ("key", "region"),
        ("source", "bronze.raw"),
        ("source_version", "4:models.a"),
        ("break_glass_reason", "incident 4: fix two customers"),
        ("expected_snapshot_id", "10"),
    ]
    assert seen["table"].equals(data)
    assert result == MergeResult(snapshot_id=11, rows_inserted=3, rows_updated=2)


def test_tables_merge_accepts_a_single_key_as_a_string() -> None:
    """`key="id"` is one column, not the letters `i` and `d`."""
    seen: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params.get_list("key"))
        return httpx.Response(200, json={"snapshot_id": 1, "rows_inserted": 1, "rows_updated": 0})

    Tables(_transport(handler)).merge("dev", "silver.customers", pa.table({"id": [1]}), key="id")

    assert seen == [["id"]]


def test_tables_merge_refuses_an_empty_key_before_sending() -> None:
    """No request goes out without a key column."""
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={})

    with pytest.raises(ValueError, match="at least one key column"):
        Tables(_transport(handler)).merge("dev", "silver.customers", pa.table({"id": [1]}), key=[])

    assert sent == []


def test_tables_merge_raises_conflict_without_a_retry_when_a_snapshot_is_expected() -> None:
    """With `expected_snapshot_id` the conflict is the answer: one request, `Conflict`."""
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(409, json={"detail": "silver.customers changed; retry."})

    with pytest.raises(Conflict):
        Tables(_transport(handler)).merge(
            "dev", "silver.customers", pa.table({"id": [1]}), key=["id"], expected_snapshot_id=7
        )

    assert attempts["n"] == 1


def test_tables_merge_retries_once_on_conflict_without_an_expected_snapshot() -> None:
    """Without a precondition a conflict proves nothing committed: one retry, as `append`."""
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(409, json={"detail": "conflict"})
        return httpx.Response(200, json={"snapshot_id": 5, "rows_inserted": 0, "rows_updated": 1})

    result = Tables(_transport(handler)).merge(
        "dev", "silver.customers", pa.table({"id": [1]}), key=["id"]
    )

    assert attempts["n"] == 2
    assert result.rows_updated == 1


def test_tables_merge_route_is_registered() -> None:
    """The route table names the merge route."""
    assert ROUTES["tables.merge"] == Route(
        "POST", "/api/v1/environments/{env}/namespaces/{path}/tables/{name}/merge"
    )


# --- lakana._arrow: schema_to_columns ----------------------------------------


@pytest.mark.parametrize(
    ("arrow_type", "expected"),
    [
        (pa.int32(), "int"),
        (pa.int64(), "long"),
        (pa.float32(), "float"),
        (pa.float64(), "double"),
        (pa.bool_(), "boolean"),
        (pa.string(), "string"),
        (pa.large_string(), "string"),
        (pa.binary(), "binary"),
        (pa.large_binary(), "binary"),
        (pa.date32(), "date"),
        (pa.time64("us"), "time"),
        (pa.timestamp("us"), "timestamp"),
        (pa.timestamp("us", tz="UTC"), "timestamptz"),
        (pa.decimal128(10, 2), "decimal(10,2)"),
        (pa.uuid(), "uuid"),
    ],
)
def test_schema_to_columns_maps_every_supported_type(
    arrow_type: pa.DataType, expected: str
) -> None:
    """Every type in the brief's mapping table converts to its documented JSON name.

    `pa.uuid()` -> `"uuid"` was missing from the initial mapping: the server's create-table route,
    append allowlist, and
    core's `to_iceberg_schema` all already support a `uuid` column, so the
    SDK-side conversion refusing it was stricter than the server itself.
    """
    schema = pa.schema([pa.field("c", arrow_type, nullable=True)])

    assert schema_to_columns(schema) == [{"name": "c", "type": expected, "nullable": True}]


def test_schema_to_columns_respects_nullable_false() -> None:
    """A non-nullable field's `nullable` flag is carried through as `False`."""
    schema = pa.schema([pa.field("id", pa.int64(), nullable=False)])

    assert schema_to_columns(schema) == [{"name": "id", "type": "long", "nullable": False}]


def test_schema_to_columns_refuses_an_unsupported_type_naming_the_column() -> None:
    """An unsupported Arrow type raises a local `ValidationError` naming the column."""
    schema = pa.schema([pa.field("bad", pa.list_(pa.int64()))])

    with pytest.raises(ValidationError, match="bad"):
        schema_to_columns(schema)


def test_schema_to_columns_refuses_wrong_unit_time64() -> None:
    """A `time64[ns]` field (wrong unit) is refused, not silently mapped to `time`."""
    schema = pa.schema([pa.field("t", pa.time64("ns"))])

    with pytest.raises(ValidationError, match="t"):
        schema_to_columns(schema)


def test_schema_to_columns_refuses_wrong_unit_timestamp() -> None:
    """A `timestamp[ns]` field (wrong unit) is refused, not silently mapped to `timestamp`."""
    schema = pa.schema([pa.field("t", pa.timestamp("ns"))])

    with pytest.raises(ValidationError, match="t"):
        schema_to_columns(schema)


def test_schema_to_columns_refuses_a_non_utc_timezone_naming_the_column() -> None:
    """A timestamp with a non-UTC timezone is refused, not silently treated as `timestamptz`.

    Only the UTC-alias spellings pyiceberg itself
    recognises (`UTC`, `+00:00`, `Etc/UTC`, `Z`) may become `timestamptz` --
    Iceberg's `timestamptz` always means UTC, so silently mapping e.g.
    `America/New_York` would reinterpret every value at the wrong instant.
    """
    schema = pa.schema([pa.field("t", pa.timestamp("us", tz="America/New_York"))])

    with pytest.raises(ValidationError, match="t"):
        schema_to_columns(schema)


@pytest.mark.parametrize("utc_alias", ["UTC", "+00:00", "Etc/UTC", "Z"])
def test_schema_to_columns_accepts_every_utc_alias(utc_alias: str) -> None:
    """Every UTC-alias spelling pyiceberg recognises maps to `timestamptz`."""
    schema = pa.schema([pa.field("t", pa.timestamp("us", tz=utc_alias))])

    assert schema_to_columns(schema) == [{"name": "t", "type": "timestamptz", "nullable": True}]


# --- lakana._arrow: json_to_arrow_type -----------------------------------


@pytest.mark.parametrize(
    "type_name",
    [
        "boolean",
        "int",
        "long",
        "float",
        "double",
        "decimal(10,2)",
        "date",
        "time",
        "timestamp",
        "timestamptz",
        "string",
        "uuid",
        "binary",
    ],
)
def test_json_to_arrow_type_inverts_the_sdk_mapping(type_name: str) -> None:
    """Every JSON column type maps to an Arrow type that maps straight back."""
    assert _arrow_type_to_json(json_to_arrow_type(type_name), "c") == type_name


def test_json_to_arrow_type_refuses_an_unknown_type() -> None:
    """A nested or misspelled type is a `ValidationError` naming it."""
    with pytest.raises(ValidationError, match="list<string>"):
        json_to_arrow_type("list<string>")


# --- lakana._arrow: cast_timestamp_ns_columns_to_us ---------------------------


def test_cast_timestamp_ns_columns_to_us_casts_whole_microseconds() -> None:
    """A `timestamp[ns]` column holding whole microseconds is cast without error."""
    schema = pa.schema([pa.field("t", pa.timestamp("ns"))])
    table = pa.table({"t": pa.array([1_000, 2_000_000], type=pa.timestamp("ns"))}, schema=schema)

    cast = cast_timestamp_ns_columns_to_us(table)

    assert cast.schema.field("t").type == pa.timestamp("us")
    assert cast.column("t").to_pylist() == table.column("t").cast(pa.timestamp("us")).to_pylist()


def test_cast_timestamp_ns_columns_to_us_preserves_timezone() -> None:
    """A tz-aware `timestamp[ns]` column keeps its timezone after casting."""
    schema = pa.schema([pa.field("t", pa.timestamp("ns", tz="UTC"))])
    table = pa.table({"t": pa.array([1_000], type=pa.timestamp("ns", tz="UTC"))}, schema=schema)

    cast = cast_timestamp_ns_columns_to_us(table)

    assert cast.schema.field("t").type == pa.timestamp("us", tz="UTC")


def test_cast_timestamp_ns_columns_to_us_leaves_other_columns_untouched() -> None:
    """A column that isn't a nanosecond timestamp is returned unchanged."""
    table = pa.table({"id": [1, 2]}, schema=pa.schema([pa.field("id", pa.int64())]))

    cast = cast_timestamp_ns_columns_to_us(table)

    assert cast.equals(table)


def test_cast_timestamp_ns_columns_to_us_refuses_truncation_naming_the_column() -> None:
    """A sub-microsecond value would be silently truncated -- refused instead, naming the column.

    `safe=True` raises rather than losing precision
    silently.
    """
    schema = pa.schema([pa.field("t", pa.timestamp("ns"))])
    table = pa.table({"t": pa.array([1], type=pa.timestamp("ns"))}, schema=schema)

    with pytest.raises(ValidationError, match="t"):
        cast_timestamp_ns_columns_to_us(table)


# --- Queries: submit/get ----------------------------------------------------


def test_queries_submit_sends_the_sql_and_returns_a_query_run() -> None:
    """`queries.submit` POSTs the SQL text and returns a handle for the returned id."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["body"] = request.read()
        return httpx.Response(202, json={"query_id": "q1"})

    queries = Queries(_transport(handler))
    run = queries.submit("SELECT 1", "dev")

    assert seen["method"] == "POST"
    assert seen["path"] == "/api/v1/environments/dev/queries"
    assert seen["body"] == b'{"sql":"SELECT 1"}'
    assert isinstance(run, QueryRun)
    assert run.id == "q1"
    assert run.environment == "dev"


def test_queries_get_builds_a_handle_without_sending_a_request() -> None:
    """`queries.get` builds a handle for an id the caller already has, with no HTTP call."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("queries.get must not send a request")

    queries = Queries(_transport(handler))
    run = queries.get("q1", "dev")

    assert run.id == "q1"
    assert run.environment == "dev"


# --- QueryRun.status/wait/result --------------------------------------------


def test_query_run_status_parses_the_status_body_including_lineage() -> None:
    """`QueryRun.status` parses every field, including lineage when present."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/environments/dev/queries/q1"
        return httpx.Response(
            200,
            json={
                "query_id": "q1",
                "status": "SUCCEEDED",
                "error_message": None,
                "lineage": {"out": [{"column": "id"}]},
            },
        )

    run = QueryRun(_transport(handler), id="q1", environment="dev")
    status = run.status()

    assert status.status == "SUCCEEDED"
    assert status.lineage == {"out": [{"column": "id"}]}


def test_query_run_wait_returns_immediately_when_already_succeeded() -> None:
    """`wait` returns on the first poll if the query has already succeeded."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"query_id": "q1", "status": "SUCCEEDED", "error_message": None, "lineage": None},
        )

    run = QueryRun(_transport(handler), id="q1", environment="dev")
    status = run.wait()

    assert status.status == "SUCCEEDED"


def test_query_run_wait_backs_off_and_raises_query_failed_with_the_server_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`wait` doubles its poll interval up to `max_interval` and surfaces the server's error."""
    sleeps: list[float] = []
    monkeypatch.setattr("lakana.resources.queries.time.sleep", sleeps.append)
    bodies = iter(
        [
            {"query_id": "q1", "status": "QUEUED", "error_message": None, "lineage": None},
            {"query_id": "q1", "status": "RUNNING", "error_message": None, "lineage": None},
            {
                "query_id": "q1",
                "status": "FAILED",
                "error_message": "column not found",
                "lineage": None,
            },
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=next(bodies))

    run = QueryRun(_transport(handler), id="q1", environment="dev")

    with pytest.raises(QueryFailed, match="column not found"):
        run.wait(poll_interval=0.1, max_interval=1.0)

    assert sleeps == [0.1, 0.2]


def test_query_failed_carries_the_servers_cause_and_none_from_an_older_server() -> None:
    """`QueryFailed.cause` is the status's `error_cause`; a missing field is `None`."""
    failed = {"query_id": "q1", "status": "FAILED", "error_message": "gone", "lineage": None}
    for extra, expected in (({"error_cause": "not_found"}, "not_found"), ({}, None)):

        def handler(request: httpx.Request, extra: dict = extra) -> httpx.Response:
            return httpx.Response(200, json={**failed, **extra})

        run = QueryRun(_transport(handler), id="q1", environment="dev")
        with pytest.raises(QueryFailed, match="gone") as caught:
            run.wait()
        assert caught.value.cause == expected
        assert run.status().error_cause == expected


# --- QueryRun.cancel and a cancelled run ------------------------------


def _cancel_handler(
    seen: list[httpx.Request], status: str = "CANCELLED"
) -> Callable[[httpx.Request], httpx.Response]:
    """Build a transport handler that records requests and answers a cancel with `status`.

    Parameters
    ----------
    seen
        Receives every request.
    status
        The status the fake server answers with.

    Returns
    -------
    Callable
        The handler.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "query_id": "q1",
                "status": status,
                "error_message": "The query was cancelled.",
                "error_cause": "cancelled",
            },
        )

    return handler


def test_cancel_posts_to_the_cancel_route_and_returns_the_status() -> None:
    """`QueryRun.cancel` sends `queries.cancel` with the path parameters, no body."""
    seen: list[httpx.Request] = []
    run = QueryRun(_transport(_cancel_handler(seen)), id="q1", environment="dev")

    status = run.cancel()

    (request,) = seen
    assert (request.method, request.url.path) == (
        ROUTES["queries.cancel"].method,
        "/api/v1/environments/dev/queries/q1/cancel",
    )
    assert request.read() == b""
    assert (status.id, status.status, status.error_cause) == ("q1", "CANCELLED", "cancelled")


def test_queries_cancel_is_the_same_call_by_id() -> None:
    """`Queries.cancel("id", "dev")` sends the same request, and returns the status."""
    seen: list[httpx.Request] = []

    status = Queries(_transport(_cancel_handler(seen, "RUNNING"))).cancel("q1", "dev")

    (request,) = seen
    assert (request.method, request.url.path) == (
        "POST",
        "/api/v1/environments/dev/queries/q1/cancel",
    )
    assert status.status == "RUNNING"


def test_wait_ends_on_a_cancelled_run_with_the_cause_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `CANCELLED` status ends `wait` with `QueryFailed` after one poll, without a sleep."""
    sleeps: list[float] = []
    monkeypatch.setattr("lakana.resources.queries.time.sleep", sleeps.append)
    seen: list[httpx.Request] = []
    run = QueryRun(_transport(_cancel_handler(seen)), id="q1", environment="dev")

    with pytest.raises(QueryFailed, match=r"The query was cancelled\.") as caught:
        run.wait(timeout=0)

    assert caught.value.cause == "cancelled"
    assert len(seen) == 1
    assert sleeps == []


def test_wait_on_a_cancelled_run_without_a_message_still_says_so() -> None:
    """A server that sends neither message nor cause still gives `cancelled`."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "query_id": "q1",
                "status": "CANCELLED",
                "error_message": None,
                "error_cause": None,
            },
        )

    run = QueryRun(_transport(handler), id="q1", environment="dev")

    with pytest.raises(QueryFailed) as caught:
        run.wait(timeout=0)

    assert str(caught.value) == "The query was cancelled."
    assert caught.value.cause == "cancelled"


def test_sql_timeout_names_the_query_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """`Client.sql`'s `TimeoutError` names the query id it was polling."""
    monkeypatch.setattr("lakana.resources.queries.time.sleep", lambda _seconds: None)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(202, json={"query_id": "abc123"})
        return httpx.Response(
            200,
            json={
                "query_id": "abc123",
                "status": "RUNNING",
                "error_message": None,
                "lineage": None,
            },
        )

    http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url=_CONFIG.url)
    client = Client(url=_CONFIG.url, token=_CONFIG.token, http_client=http_client)

    with pytest.raises(TimeoutError, match="abc123"):
        client.sql("SELECT 1", environment="dev", timeout=0)


def test_query_run_result_reads_the_arrow_ipc_file_response() -> None:
    """`QueryRun.result` decodes the server's Arrow IPC **file** response."""
    table = pa.table({"id": [1, 2, 3]})
    sink = pa.BufferOutputStream()
    with pyarrow.ipc.new_file(sink, table.schema) as writer:
        writer.write_table(table)
    file_bytes = sink.getvalue().to_pybytes()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/environments/dev/queries/q1/result"
        return httpx.Response(200, content=file_bytes)

    run = QueryRun(_transport(handler), id="q1", environment="dev")
    result = run.result()

    assert result.equals(table)


# --- Client wiring -----------------------------------------------------


def test_client_exposes_the_data_plane_resources() -> None:
    """`Client` wires up `environments`/`namespaces`/`tables`/`queries` attributes."""
    http_client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    )
    client = Client(url="http://api.example", token="lakana_pat_a_s", http_client=http_client)

    assert isinstance(client.environments, Environments)
    assert isinstance(client.namespaces, Namespaces)
    assert isinstance(client.tables, Tables)
    assert isinstance(client.queries, Queries)


def test_client_sql_submits_waits_and_returns_the_result() -> None:
    """`Client.sql` submits, waits for success, and returns the decoded result."""
    table = pa.table({"id": [1, 2]})
    sink = pa.BufferOutputStream()
    with pyarrow.ipc.new_file(sink, table.schema) as writer:
        writer.write_table(table)
    file_bytes = sink.getvalue().to_pybytes()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(202, json={"query_id": "q1"})
        if request.url.path.endswith("/result"):
            return httpx.Response(200, content=file_bytes)
        return httpx.Response(
            200,
            json={"query_id": "q1", "status": "SUCCEEDED", "error_message": None, "lineage": None},
        )

    http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url=_CONFIG.url)
    client = Client(url=_CONFIG.url, token=_CONFIG.token, http_client=http_client)

    result = client.sql("SELECT * FROM t", environment="dev")

    assert result.equals(table)


def test_tables_append_normalizes_polars_view_types_to_plain_arrow_types() -> None:
    """Polars `string_view`/`binary_view` columns are sent as `string`/`binary`.

    The append endpoint refuses view types by design (CPU-amplification guard),
    so the SDK normalizes them client-side; the values are unchanged.
    """
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        with pyarrow.ipc.open_stream(pa.BufferReader(request.content)) as reader:
            seen["table"] = reader.read_all()
        return httpx.Response(200, json={"rows": 2, "files": 1, "snapshot_id": 1})

    frame = pl.DataFrame({"region": ["eu", None], "blob": [b"x", None], "id": [1, 2]})

    Tables(_transport(handler)).append("dev", "bronze.sales", frame)

    sent = seen["table"]
    assert sent.schema.field("region").type == pa.string()
    assert sent.schema.field("blob").type == pa.binary()
    assert sent.column("region").to_pylist() == ["eu", None]
    assert sent.column("blob").to_pylist() == [b"x", None]


_POLARS_BEFORE_1_37 = tuple(int(part) for part in pl.__version__.split(".")[:2]) < (1, 37)
"""`pa.schema(pl.Schema)` needs Polars' Arrow schema export, which works from Polars 1.37."""


@pytest.mark.skipif(
    _POLARS_BEFORE_1_37,
    reason="a pl.Schema has the Arrow C interface only from Polars 1.37 (1.34 to 1.36 are broken)",
)
def test_tables_create_accepts_a_polars_schema() -> None:
    """A polars `Schema` converts through the Arrow C interface, with view types mapped."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            201,
            json={
                "name": "sales",
                "namespace": "bronze",
                "uuid": "u1",
                "metadata_location": "file:///m.json",
                "schema": [],
            },
        )

    schema = pl.Schema({"id": pl.Int64, "region": pl.String, "blob": pl.Binary})

    Tables(_transport(handler)).create("dev", "bronze.sales", schema=schema)

    assert seen["body"]["schema"] == [
        {"name": "id", "type": "long", "nullable": True},
        {"name": "region", "type": "string", "nullable": True},
        {"name": "blob", "type": "binary", "nullable": True},
    ]


def test_tables_create_names_the_polars_version_a_polars_schema_needs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Below Polars 1.37 `pa.schema(pl.Schema)` raises a raw `TypeError`; say what to do."""

    def broken(self: object, *args: object) -> object:
        raise TypeError("DataType expected, got Int64")  # what pyarrow made of it on Polars 1.30

    monkeypatch.setattr(pl.Schema, "__arrow_c_schema__", broken, raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("nothing is sent")

    with pytest.raises(ValidationError, match=r"Polars 1\.37 or newer.*pyarrow\.schema.*list of"):
        Tables(_transport(handler)).create("dev", "bronze.sales", schema=pl.Schema({"a": pl.Int64}))


def test_tables_create_does_not_hide_another_type_error() -> None:
    """Only a Polars `Schema` gets that message; any other input keeps pyarrow's own error."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("nothing is sent")

    with pytest.raises(TypeError):
        Tables(_transport(handler)).create("dev", "bronze.sales", schema=42)


def test_tables_get_tolerates_a_server_without_the_schema_field() -> None:
    """An older server sends no `schema`; the SDK returns empty `columns`."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "name": "sales",
                "namespace": "bronze",
                "uuid": "u1",
                "metadata_location": "file:///m.json",
            },
        )

    table = Tables(_transport(handler)).get("dev", "bronze.sales")

    assert table.columns == []


def test_queries_submit_sends_a_write_target() -> None:
    """`into`/`mode` become the request's `target` object."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["json"] = json.loads(request.content)
        return httpx.Response(202, json={"query_id": "q1"})

    Queries(_transport(handler)).submit(
        "SELECT 1", "dev", into="gold.daily", mode="append", incremental_key="ts"
    )

    assert seen["json"] == {
        "sql": "SELECT 1",
        "target": {
            "table": "gold.daily",
            "mode": "append",
            "incremental_key": "ts",
            "evolve_schema": False,
        },
    }


@pytest.mark.parametrize(
    "options",
    [
        {"mode": "replace"},
        {"into": "gold.daily"},
        {"incremental_key": "ts"},
        {"evolve_schema": True},
    ],
)
def test_write_options_without_their_partner_fail_before_any_request(
    options: dict[str, Any],
) -> None:
    """`into` requires `mode`, and the other options require `into`."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request should be sent")

    with pytest.raises(ValidationError):
        Queries(_transport(handler)).submit("SELECT 1", "dev", **options)


def test_client_sql_with_into_waits_and_returns_the_write_status() -> None:
    """A write has no result: `client.sql(into=...)` returns the finished status."""
    status = {
        "query_id": "q1",
        "status": "SUCCEEDED",
        "error_message": None,
        "lineage": None,
        "write_mode": "replace",
        "target": "gold.daily",
        "rows_written": 3,
        "snapshot_id": 11,
        "watermark": None,
        "schema_diff": None,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert not request.url.path.endswith("/result")
        if request.method == "POST":
            return httpx.Response(202, json={"query_id": "q1"})
        return httpx.Response(200, json=status)

    http_client = httpx.Client(transport=httpx.MockTransport(handler), base_url=_CONFIG.url)
    client = Client(url=_CONFIG.url, token=_CONFIG.token, http_client=http_client)

    result = client.sql("SELECT 1", "dev", into="gold.daily", mode="replace")

    assert result == QueryStatus(
        id="q1",
        status="SUCCEEDED",
        error_message=None,
        lineage=None,
        write_mode="replace",
        target="gold.daily",
        rows_written=3,
        snapshot_id=11,
    )


def test_tables_get_parses_a_view() -> None:
    """A view carries its kind, SQL and references; it has no metadata location."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "name": "v",
                "namespace": "gold",
                "uuid": "u1",
                "kind": "VIEW",
                "metadata_location": None,
                "schema": [{"name": "id", "type": "long", "nullable": True}],
                "sql": "SELECT id FROM bronze.src",
                "references": [{"id": "u0", "name": "bronze.src"}],
            },
        )

    table = Tables(_transport(handler)).get("dev", "gold.v")

    assert table == Table(
        name="v",
        namespace="gold",
        uuid="u1",
        metadata_location=None,
        columns=[Column(name="id", type="long", nullable=True)],
        kind="VIEW",
        sql="SELECT id FROM bronze.src",
        references=[TableReference(id="u0", name="bronze.src")],
    )


def test_tables_get_parses_a_view_with_hidden_references() -> None:
    """A reader who can't read every reference gets a count and no SQL."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "name": "v",
                "namespace": "gold",
                "uuid": "u1",
                "kind": "VIEW",
                "metadata_location": None,
                "schema": [],
                "sql": None,
                "references": [],
                "hidden_references": 2,
            },
        )

    table = Tables(_transport(handler)).get("dev", "gold.v")

    assert (table.sql, table.references, table.hidden_references) == (None, [], 2)


def test_tables_get_defaults_hidden_references_to_zero() -> None:
    """An older server sends no `hidden_references`."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"name": "s", "namespace": "bronze", "uuid": "u", "metadata_location": "m"},
        )

    assert Tables(_transport(handler)).get("dev", "bronze.s").hidden_references == 0


def test_tables_get_parses_the_state_of_a_table_that_is_not_published() -> None:
    """`state` and `state_message` are typed fields of the table."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "name": "s",
                "namespace": "bronze",
                "uuid": "u",
                "metadata_location": "m",
                "state": "not_published",
                "state_message": "`bronze.s` has not been published.",
            },
        )

    table = Tables(_transport(handler)).get("dev", "bronze.s")

    assert (table.state, table.state_message) == (
        "not_published",
        "`bronze.s` has not been published.",
    )


def test_tables_get_parses_the_state_of_a_kept_table() -> None:
    """A table kept after a failed check reads as `kept`, with the message."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "name": "s",
                "namespace": "bronze",
                "uuid": "u",
                "metadata_location": "m",
                "state": "kept",
                "state_message": "Kept after a failed check.",
            },
        )

    table = Tables(_transport(handler)).get("dev", "bronze.s")

    assert (table.state, table.state_message) == ("kept", "Kept after a failed check.")


def test_tables_get_defaults_the_state_to_published() -> None:
    """An older server sends neither field: the table reads as published, with no message."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"name": "s", "namespace": "bronze", "uuid": "u", "metadata_location": "m"},
        )

    table = Tables(_transport(handler)).get("dev", "bronze.s")

    assert (table.state, table.state_message) == ("published", None)


def test_tables_lineage_sends_direction_and_depth_and_parses_the_graph() -> None:
    """`tables.lineage` GETs the lineage route and parses nodes, edges and hidden count."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        return httpx.Response(
            200,
            json={
                "root_id": "t",
                "direction": "downstream",
                "depth": 5,
                "nodes": [
                    {"id": "t", "name": "gold.t", "kind": "TABLE", "dropped": False},
                    {"id": "x", "name": "gold.x", "kind": None, "dropped": True},
                ],
                "edges": [
                    {
                        "source_id": "t",
                        "target_id": "x",
                        "source_snapshot_id": 1,
                        "target_snapshot_id": 2,
                        "query_run_id": None,
                        "created_at": "2026-09-29T10:00:00Z",
                    }
                ],
                "hidden_nodes": 1,
                "truncated": True,
            },
        )

    lineage = Tables(_transport(handler)).lineage("dev", "gold.t", direction="downstream", depth=5)

    assert seen["path"] == "/api/v1/environments/dev/namespaces/gold/tables/t/lineage"
    assert seen["params"] == {"direction": "downstream", "depth": "5"}
    assert lineage == Lineage(
        root_id="t",
        direction="downstream",
        depth=5,
        nodes=[
            LineageNode(id="t", name="gold.t", kind="TABLE", dropped=False),
            LineageNode(id="x", name="gold.x", kind=None, dropped=True),
        ],
        edges=[
            LineageEdge(
                source_id="t",
                target_id="x",
                source_snapshot_id=1,
                target_snapshot_id=2,
                query_run_id=None,
                created_at=datetime.fromisoformat("2026-09-29T10:00:00Z"),
            )
        ],
        hidden_nodes=1,
        truncated=True,
    )


def test_tables_lineage_defaults_truncated_to_false() -> None:
    """An older server sends no `truncated`."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "root_id": "t",
                "direction": "upstream",
                "depth": 3,
                "nodes": [{"id": "t", "name": "gold.t", "kind": "TABLE", "dropped": False}],
                "edges": [],
                "hidden_nodes": 0,
            },
        )

    assert Tables(_transport(handler)).lineage("dev", "gold.t").truncated is False


def test_tables_lineage_parses_a_connection_node_and_its_kind() -> None:
    """A connection source carries its kind; an older server's nodes read as having none."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "root_id": "t",
                "direction": "upstream",
                "depth": 3,
                "nodes": [
                    {"id": "t", "name": "gold.t", "kind": "TABLE", "dropped": False},
                    {
                        "id": "c",
                        "name": "crm-db",
                        "kind": "CONNECTION",
                        "dropped": False,
                        "connection_kind": "postgresql",
                    },
                ],
                "edges": [],
                "hidden_nodes": 0,
            },
        )

    nodes = Tables(_transport(handler)).lineage("dev", "gold.t").nodes

    assert nodes == [
        LineageNode(id="t", name="gold.t", kind="TABLE", dropped=False),
        LineageNode(
            id="c", name="crm-db", kind="CONNECTION", dropped=False, connection_kind="postgresql"
        ),
    ]
    assert nodes[0].connection_kind is None


def test_tables_maintain_posts_the_request_and_returns_a_run_handle() -> None:
    """`tables.maintain` returns a `QueryRun`, polled like any query."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["json"] = json.loads(request.content)
        return httpx.Response(202, json={"query_id": "m1"})

    run = Tables(_transport(handler)).maintain("dev", "gold.daily", keep_last=2, dry_run=True)

    assert seen["method"] == "POST"
    assert seen["path"] == "/api/v1/environments/dev/namespaces/gold/tables/daily/maintenance"
    assert seen["json"] == {
        "keep_days": 7,
        "keep_last": 2,
        "dry_run": True,
        "older_than_hours": 72,
    }
    assert isinstance(run, QueryRun)
    assert (run.id, run.environment) == ("m1", "dev")


def test_tables_maintain_sends_a_custom_older_than_hours() -> None:
    """`older_than_hours` goes out in the body."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["json"] = json.loads(request.content)
        return httpx.Response(202, json={"query_id": "m1"})

    Tables(_transport(handler)).maintain("dev", "gold.daily", older_than_hours=24)

    assert seen["json"]["older_than_hours"] == 24


def test_tables_maintain_surfaces_a_rejected_older_than_hours() -> None:
    """The server's 422 for `older_than_hours=0` is a `ValidationError`."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"detail": "older_than_hours must be at least 1"})

    with pytest.raises(ValidationError):
        Tables(_transport(handler)).maintain("dev", "gold.daily", older_than_hours=0)


def test_query_status_parses_a_maintenance_report() -> None:
    """A maintenance run's status carries its request and report, note included."""
    report = {
        "request": {"keep_days": 7},
        "report": {"files_deleted": 2, "note": "table changed during maintenance, retry"},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "query_id": "m1",
                "status": "SUCCEEDED",
                "error_message": None,
                "lineage": None,
                "write_mode": "maintain",
                "maintenance": report,
            },
        )

    status = QueryRun(_transport(handler), id="m1", environment="dev").status()

    assert status.write_mode == "maintain"
    assert status.maintenance == report


@pytest.mark.parametrize(
    ("method", "expected", "sent"),
    [
        ("append", 7, "7"),
        ("append", "none", "none"),
        ("replace", 7, "7"),
        ("replace", "none", "none"),
    ],
)
def test_tables_writes_send_expected_snapshot_id_and_do_not_retry_a_conflict(
    method: str, expected: int | str, sent: str
) -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        assert request.url.params["expected_snapshot_id"] == sent
        return httpx.Response(409, json={"detail": "gold.t changed during the write; retry."})

    tables = Tables(_transport(handler))
    with pytest.raises(Conflict):
        getattr(tables, method)(
            "dev", "gold.t", pa.table({"id": [1]}), expected_snapshot_id=expected
        )
    assert attempts["n"] == 1


def test_tables_writes_omit_expected_snapshot_id_by_default() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "expected_snapshot_id" not in request.url.params
        return httpx.Response(200, json={"rows": 1, "files": 1, "snapshot_id": 7})

    tables = Tables(_transport(handler))
    tables.append("dev", "gold.t", pa.table({"id": [1]}))
    tables.replace("dev", "gold.t", pa.table({"id": [1]}))


def test_tables_get_exposes_the_snapshot_id() -> None:
    body = {"name": "t", "namespace": "gold", "uuid": "u1", "metadata_location": "m", "schema": []}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    assert Tables(_transport(handler)).get("dev", "gold.t").snapshot_id is None
    body["snapshot_id"] = 42
    assert Tables(_transport(handler)).get("dev", "gold.t").snapshot_id == 42


@pytest.mark.parametrize(("method", "route"), [("append", "POST"), ("replace", "PUT")])
def test_tables_writes_hit_their_own_route(method: str, route: str) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url.path}")
        return httpx.Response(200, json={"rows": 1, "files": 1, "snapshot_id": 7})

    getattr(Tables(_transport(handler)), method)("dev", "gold.t", pa.table({"id": [1]}))
    assert seen == [f"{route} /api/v1/environments/dev/namespaces/gold/tables/t/data"]


# -- wait reasons ----------------------------------------------------

_QUEUED_BODY: dict[str, Any] = {
    "query_id": "q1",
    "status": "QUEUED",
    "error_message": None,
    "lineage": None,
}


def test_a_query_status_parses_waiting_for() -> None:
    """Every optional field of the reason is parsed; times are aware, names a tuple."""
    body = {
        **_QUEUED_BODY,
        "waiting_for": {
            "code": "workers_busy",
            "message": "All 2 workers are busy, and about 4 queries or deploys are ahead.",
            "workers": 2,
            "ahead": 4,
            "upstream": ["a", "b"],
            "upstream_count": 2,
            "attempt": 2,
            "max_attempts": 3,
            "next_attempt_at": "2026-10-09T14:03:20+00:00",
        },
    }
    run = QueryRun(
        _transport(lambda request: httpx.Response(200, json=body)), id="q1", environment="dev"
    )
    waiting = run.status().waiting_for
    assert waiting == WaitingFor(
        code="workers_busy",
        message="All 2 workers are busy, and about 4 queries or deploys are ahead.",
        workers=2,
        ahead=4,
        upstream=("a", "b"),
        upstream_count=2,
        attempt=2,
        max_attempts=3,
        next_attempt_at=datetime.fromisoformat("2026-10-09T14:03:20+00:00"),
    )
    assert waiting.next_attempt_at is not None and waiting.next_attempt_at.tzinfo is not None


def test_new_fields_of_a_reason_are_parsed() -> None:
    """The two newer codes carry `other_workers`, `running` and `limit`."""
    kind = waiting_for_from_json(
        {"code": "no_worker_of_kind", "message": "m", "workers": 0, "other_workers": 2}
    )
    assert kind == WaitingFor(code="no_worker_of_kind", message="m", workers=0, other_workers=2)
    own = waiting_for_from_json({"code": "user_limit", "message": "m", "running": 2, "limit": 2})
    assert own == WaitingFor(code="user_limit", message="m", running=2, limit=2)
    manager = waiting_for_from_json({"code": "user_limit", "message": "m", "limit": 2})
    assert manager == WaitingFor(code="user_limit", message="m", limit=2)
    assert manager is not None and manager.running is None


def test_a_reason_from_an_older_server_has_no_new_fields() -> None:
    """A body without the three fields parses, with each of them `None`."""
    waiting = waiting_for_from_json({"code": "workers_busy", "message": "m", "workers": 2})
    assert waiting == WaitingFor(code="workers_busy", message="m", workers=2)
    assert waiting is not None
    assert (waiting.other_workers, waiting.running, waiting.limit) == (None, None, None)


@pytest.mark.parametrize(
    "extra",
    [
        {"limit": "two"},
        {"running": True},
        {"other_workers": None},
        {"limit": 2.5, "running": [], "other_workers": {}},
    ],
)
def test_malformed_new_fields_are_left_out(extra: dict) -> None:
    """The reason is kept; the field of the wrong shape is `None`."""
    waiting = waiting_for_from_json({"code": "user_limit", "message": "m", **extra})
    assert waiting is not None
    assert (waiting.code, waiting.message) == ("user_limit", "m")
    assert (waiting.other_workers, waiting.running, waiting.limit) == (None, None, None)


def test_a_status_without_the_field_parses_as_none() -> None:
    """An older server sends no `waiting_for`; a finished query sends `null`."""
    for body in (_QUEUED_BODY, {**_QUEUED_BODY, "waiting_for": None}):
        run = QueryRun(
            _transport(lambda request, body=body: httpx.Response(200, json=body)),
            id="q1",
            environment="dev",
        )
        assert run.status().waiting_for is None


def test_a_code_from_a_newer_server_is_kept_as_an_opaque_string() -> None:
    """An unknown code, and fields this SDK does not know, neither break the parse nor vanish."""
    body = {
        **_QUEUED_BODY,
        "waiting_for": {"code": "quota_full", "message": "Quota reached.", "quota": 7},
    }
    run = QueryRun(
        _transport(lambda request: httpx.Response(200, json=body)), id="q1", environment="dev"
    )
    waiting = run.status().waiting_for
    assert waiting == WaitingFor(code="quota_full", message="Quota reached.")


def test_a_run_task_parses_waiting_for() -> None:
    """Each task of a run carries its reason; a server that predates it sends none."""
    task = {
        "asset": "gold.daily",
        "status": "pending",
        "attempt": 0,
        "max_attempts": 3,
        "timeout_seconds": 600,
        "next_attempt_at": None,
        "started_at": None,
        "finished_at": None,
        "error_message": None,
        "query_run_id": None,
        "rows_written": None,
    }
    body = {
        "id": "r1",
        "environment": "dev",
        "repository": "analytics",
        "project_version_id": "v1",
        "commit_sha": "a" * 40,
        "kind": "full",
        "status": "running",
        "cancel_requested": False,
        "triggered_by": "admin",
        "run_as": "admin",
        "resumed_from_run_id": None,
        "selection": {},
        "error_message": None,
        "created_at": "2026-10-09T14:00:00+00:00",
        "started_at": None,
        "finished_at": None,
        "tasks": [
            {
                **task,
                "waiting_for": {
                    "code": "upstream",
                    "message": "Waiting for upstream tasks: silver.clean.",
                    "upstream": ["silver.clean"],
                },
            },
            {**task, "asset": "gold.weekly"},
        ],
    }
    waiting, old = run_from_json(body).tasks
    assert waiting.waiting_for == WaitingFor(
        code="upstream",
        message="Waiting for upstream tasks: silver.clean.",
        upstream=("silver.clean",),
    )
    assert old.waiting_for is None


def test_a_run_task_parses_setting_back_and_defaults_it_for_an_older_server() -> None:
    task = {
        "asset": "gold.weekly",
        "status": "running",
        "attempt": 1,
        "max_attempts": 3,
        "timeout_seconds": 60,
        "next_attempt_at": None,
        "started_at": None,
        "finished_at": None,
        "error_message": None,
        "query_run_id": None,
        "rows_written": None,
    }
    body = {
        "id": "r1",
        "environment": "dev",
        "repository": "analytics",
        "project_version_id": "v1",
        "commit_sha": "a" * 40,
        "kind": "full",
        "status": "running",
        "cancel_requested": False,
        "triggered_by": "admin",
        "run_as": "admin",
        "resumed_from_run_id": None,
        "selection": {},
        "error_message": None,
        "created_at": "2026-10-09T14:00:00+00:00",
        "started_at": None,
        "finished_at": None,
        "tasks": [{**task, "setting_back": True}, task],
    }
    setting, old = run_from_json(body).tasks
    assert (setting.setting_back, old.setting_back) == (True, False)


def _sequence_run(statuses: list[dict[str, Any]]) -> QueryRun:
    """Build a handle whose status answers are the given bodies, in order.

    Parameters
    ----------
    statuses
        The bodies, one per poll.

    Returns
    -------
    QueryRun
        The handle.
    """
    bodies = iter(statuses)
    return QueryRun(
        _transport(lambda request: httpx.Response(200, json=next(bodies))),
        id="q1",
        environment="dev",
    )


def test_wait_calls_on_update_with_every_polled_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two `QUEUED` answers and one `SUCCEEDED`: three calls, in order, the last before return."""
    monkeypatch.setattr("lakana.resources.queries.time.sleep", lambda seconds: None)
    waiting = {"code": "no_worker", "message": "No worker is running."}
    run = _sequence_run(
        [
            {**_QUEUED_BODY, "waiting_for": waiting},
            {**_QUEUED_BODY, "waiting_for": {"code": "starting", "message": "Soon."}},
            {**_QUEUED_BODY, "status": "SUCCEEDED"},
        ]
    )
    seen: list[tuple[str, str | None]] = []

    def on_update(status: QueryStatus) -> None:
        """Record the status and the code.

        Parameters
        ----------
        status
            The polled status.
        """
        seen.append((status.status, status.waiting_for.code if status.waiting_for else None))

    result = run.wait(on_update=on_update)

    assert result.status == "SUCCEEDED"
    assert seen == [("QUEUED", "no_worker"), ("QUEUED", "starting"), ("SUCCEEDED", None)]


def test_wait_calls_on_update_before_it_raises_for_a_failed_query() -> None:
    """The callback sees the failing status first; the `QueryFailed` follows."""
    run = _sequence_run([{**_QUEUED_BODY, "status": "FAILED", "error_message": "boom"}])
    seen: list[str] = []
    with pytest.raises(QueryFailed, match="boom"):
        run.wait(on_update=lambda status: seen.append(status.status))
    assert seen == ["FAILED"]


def test_an_exception_in_on_update_ends_the_wait() -> None:
    """The callback's exception propagates: the caller's bug is not swallowed."""
    run = _sequence_run([_QUEUED_BODY])

    def on_update(status: QueryStatus) -> None:
        """Refuse every status.

        Parameters
        ----------
        status
            The polled status.

        Raises
        ------
        RuntimeError
            Always.
        """
        raise RuntimeError("display broke")

    with pytest.raises(RuntimeError, match="display broke"):
        run.wait(on_update=on_update)


_MALFORMED_REASONS: list[object] = [
    "a string",
    ["code", "message"],
    7,
    True,
    {},
    {"code": "x"},
    {"message": "m"},
    {"code": "x", "message": None},
    {"code": "x", "message": 5},
    {"code": None, "message": "m"},
    {"code": 3, "message": "m"},
]


@pytest.mark.parametrize("reason", _MALFORMED_REASONS)
def test_a_waiting_for_that_is_not_a_well_formed_object_parses_as_none(reason: object) -> None:
    """A status, a task and a list row with a malformed reason behave as if nothing waits."""
    assert waiting_for_from_json(reason) is None
    body = {**_QUEUED_BODY, "waiting_for": reason}
    run = QueryRun(
        _transport(lambda request: httpx.Response(200, json=body)), id="q1", environment="dev"
    )
    assert run.status().waiting_for is None


@pytest.mark.parametrize(
    "extra",
    [
        {"upstream": "abc"},
        {"upstream": 5},
        {"upstream": [1, None]},
        {"next_attempt_at": "not a time"},
        {"next_attempt_at": 12},
        {"workers": "two", "ahead": [], "attempt": {}},
    ],
)
def test_optional_fields_of_the_wrong_shape_are_left_out_not_fatal(extra: dict) -> None:
    """The code and message are enough; a malformed optional field is dropped."""
    waiting = waiting_for_from_json({"code": "x", "message": "m", **extra})
    assert waiting is not None
    assert (waiting.code, waiting.message) == ("x", "m")
    assert waiting.upstream is None
    assert waiting.next_attempt_at is None
    assert waiting.workers is None and waiting.ahead is None and waiting.attempt is None


def test_tables_get_carries_the_numbers_of_the_snapshot() -> None:
    """`Table` holds the snapshot's numbers; an older server sends none."""
    bodies = [
        {
            "last_write_at": "2026-10-11T12:00:00Z",
            "row_count": 3,
            "size_bytes": 2048,
            "file_count": 1,
        },
        {},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "name": "sales",
                "namespace": "bronze",
                "uuid": "u1",
                "metadata_location": None,
                "schema": [],
                **bodies.pop(0),
            },
        )

    tables = Tables(_transport(handler))
    full, bare = tables.get("dev", "bronze.sales"), tables.get("dev", "bronze.sales")

    assert full.last_write_at == datetime.fromisoformat("2026-10-11T12:00:00+00:00")
    assert (full.row_count, full.size_bytes, full.file_count) == (3, 2048, 1)
    assert (bare.last_write_at, bare.row_count, bare.size_bytes, bare.file_count) == (
        None,
        None,
        None,
        None,
    )


_HEALTH_BODY = {
    "days": 30,
    "last_maintenance": {
        "run_id": "r1",
        "finished_at": "2026-10-11T12:00:00Z",
        "status": "stopped",
        "snapshots_expired": 3,
        "files_deleted": 4,
        "bytes_deleted": 2048,
        "tags_removed": 1,
    },
    "checks": [
        {
            "name": "bronze.sales/unique_id",
            "check": "unique_id",
            "run_id": "r2",
            "outcome": "failed",
            "on_failure": "keep",
            "failing_rows": 2,
            "error": None,
            "rollback_outcome": None,
            "kept": True,
            "not_published": False,
            "finished_at": "2026-10-11T13:00:00Z",
        }
    ],
    "readers": [{"principal": "ada", "read_count": 5, "last_read_at": "2026-10-11T14:00:00Z"}],
}


def test_tables_health_gets_the_health_route_and_parses_it() -> None:
    """`health` GETs `.../tables/{name}/health` with `days` only when given."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_HEALTH_BODY)

    tables = Tables(_transport(handler))
    health = tables.health("dev", "bronze.sales")
    tables.health("dev", "bronze.sales", days=7)

    path = "/api/v1/environments/dev/namespaces/bronze/tables/sales/health"
    assert [(r.method, r.url.path, dict(r.url.params)) for r in seen] == [
        ("GET", path, {}),
        ("GET", path, {"days": "7"}),
    ]
    assert health.days == 30
    assert health.last_maintenance is not None
    assert (health.last_maintenance.status, health.last_maintenance.snapshots_expired) == (
        "stopped",
        3,
    )
    assert health.last_maintenance.finished_at == datetime.fromisoformat(
        "2026-10-11T12:00:00+00:00"
    )
    (check,) = health.checks
    assert (check.check, check.outcome, check.kept, check.not_published) == (
        "unique_id",
        "failed",
        True,
        False,
    )
    assert check.finished_at == datetime.fromisoformat("2026-10-11T13:00:00+00:00")
    assert check.generated is False
    assert health.readers is not None
    assert [(r.principal, r.read_count) for r in health.readers] == [("ada", 5)]


def test_tables_health_reads_the_generated_mark_of_a_check() -> None:
    """A check from the asset's header carries `generated`; an older server's body has none."""
    (check,) = cast("list[dict[str, object]]", _HEALTH_BODY["checks"])
    body = {**_HEALTH_BODY, "checks": [{**check, "generated": True}]}
    health = Tables(_transport(lambda _: httpx.Response(200, json=body))).health(
        "dev", "bronze.sales"
    )
    assert [c.generated for c in health.checks] == [True]


def test_tables_health_with_nothing_to_show() -> None:
    """No maintenance and no right to see readers parse as `None`."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"days": 30, "last_maintenance": None, "checks": [], "readers": None}
        )

    health = Tables(_transport(handler)).health("dev", "bronze.sales")

    assert (health.last_maintenance, health.checks, health.readers) == (None, [], None)


def _failed_task(**extra: object) -> dict[str, object]:
    """Build the body of a task run as the server sends it.

    Parameters
    ----------
    **extra
        Fields to add or replace.

    Returns
    -------
    dict
        The task body.
    """
    return {
        "asset": "gold.daily",
        "status": "failed",
        "attempt": 1,
        "max_attempts": 3,
        "timeout_seconds": 600,
        "next_attempt_at": None,
        "started_at": None,
        "finished_at": None,
        "error_message": "timed out after 600 s",
        "query_run_id": None,
        "rows_written": None,
        **extra,
    }


def _run_body(*tasks: dict[str, object], **extra: object) -> dict[str, object]:
    """Build the body of a run as the server sends it.

    Parameters
    ----------
    *tasks
        Its tasks.
    **extra
        Fields to add or replace.

    Returns
    -------
    dict
        The run body.
    """
    return {
        "id": "r1",
        "environment": "dev",
        "repository": "analytics",
        "project_version_id": "v1",
        "commit_sha": "a" * 40,
        "kind": "full",
        "status": "failed",
        "cancel_requested": False,
        "triggered_by": "admin",
        "run_as": "admin",
        "resumed_from_run_id": None,
        "selection": {},
        "error_message": None,
        "created_at": "2026-10-09T14:00:00+00:00",
        "started_at": None,
        "finished_at": None,
        "tasks": list(tasks),
        **extra,
    }


def test_a_run_task_parses_its_failure_and_a_run_its_failed_task_count() -> None:
    """`failure` is the code, the cause and the next step; `error_message` stays the detail."""
    failure = {"code": "timeout", "cause": "It ran too long.", "next_step": "Make it faster."}
    run = run_from_json(_run_body(_failed_task(failure=failure), failed_tasks=1))

    (task,) = run.tasks
    assert task.failure == Failure("timeout", "It ran too long.", "Make it faster.")
    assert task.error_message == "timed out after 600 s"
    assert run.failed_tasks == 1


def test_a_run_task_without_a_failure_and_a_run_of_an_older_server_default() -> None:
    """A task with no error has none, and an older server sends neither field."""
    run = run_from_json(_run_body(_failed_task(status="succeeded", error_message=None)))

    assert run.tasks[0].failure is None
    assert run.failed_tasks == 0


@pytest.mark.parametrize(
    "bad",
    [None, "timeout", 3, {"code": "timeout"}, {"code": 1, "cause": "c", "next_step": "n"}],
)
def test_a_failure_that_cannot_be_read_never_fails_the_command(bad: object) -> None:
    """The failure only helps the reader, so a malformed one is no failure at all."""
    assert failure_from_json(bad) is None


_ORPHANED = {
    "environment": "dev",
    "older_than_days": 30,
    "complete": True,
    "locations": [
        {
            "location": "managed/gold/orders-0123",
            "files": 2,
            "bytes": 10,
            "newest_file_at": "2026-09-01T10:00:00Z",
            "recent": False,
            "deleted": True,
            "files_deleted": 2,
            "bytes_deleted": 10,
            "kept_because": None,
        },
        {
            "location": "managed/gold/fresh-0123",
            "files": 1,
            "bytes": 5,
            "newest_file_at": None,
            "recent": True,
            "deleted": False,
            "files_deleted": 0,
            "bytes_deleted": 0,
            "kept_because": "recent",
        },
    ],
}


def test_environments_orphaned_locations_lists_and_deletes() -> None:
    """The listing GETs with `older_than_days`; the deletion POSTs it in the body."""
    seen: list[tuple[str, str, str, bytes]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, str(request.url.params), request.content))
        return httpx.Response(200, json=_ORPHANED)

    environments = Environments(_transport(handler))
    listed = environments.orphaned_locations("dev", older_than_days=7)
    deleted = environments.delete_orphaned_locations("dev")

    assert seen == [
        ("GET", "/api/v1/environments/dev/orphaned-locations", "older_than_days=7", b""),
        (
            "POST",
            "/api/v1/environments/dev/orphaned-locations/delete",
            "",
            b'{"older_than_days":30}',
        ),
    ]
    assert listed.environment == "dev" and listed.complete
    first, second = deleted.locations
    assert (first.location, first.files, first.bytes, first.deleted) == (
        "managed/gold/orders-0123",
        2,
        10,
        True,
    )
    assert first.newest_file_at == datetime(2026, 9, 1, 10, tzinfo=UTC)
    assert (second.newest_file_at, second.recent, second.kept_because) == (None, True, "recent")


def test_tables_load_posts_the_stream_with_its_options() -> None:
    """`tables.load` POSTs to `…/load` with `write`, `dry_run`, the pin and the reason."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["params"] = request.url.params.multi_items()
        with pyarrow.ipc.open_stream(pa.BufferReader(request.content)) as reader:
            seen["table"] = reader.read_all()
        return httpx.Response(
            200, json={"snapshot_id": 7, "rows": 2, "created": False, "dry_run": False}
        )

    data = pa.table({"id": [1, 2]})
    result = Tables(_transport(handler)).load(
        "dev",
        "bronze.customers",
        data,
        write="append",
        expected_snapshot_id=6,
        break_glass_reason="incident 4: load the export",
    )

    assert seen["method"] == "POST"
    assert seen["path"] == "/api/v1/environments/dev/namespaces/bronze/tables/customers/load"
    assert seen["params"] == [
        ("write", "append"),
        ("dry_run", "false"),
        ("expected_snapshot_id", "6"),
        ("break_glass_reason", "incident 4: load the export"),
    ]
    assert seen["table"].equals(data)
    assert result == LoadResult(snapshot_id=7, rows=2, created=False, dry_run=False)


def test_tables_load_shows_the_servers_bronze_refusal_and_never_retries() -> None:
    """The 422 arrives as `ValidationError` with the server's text; a 409 is not retried."""
    text = (
        "`lakana load` loads into bronze only: load into `bronze.customers` and transform it "
        "into `silver.customers` with an asset."
    )
    calls: list[str] = []

    def refuse(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(422, json={"detail": text})

    with pytest.raises(ValidationError) as caught:
        Tables(_transport(refuse)).load(
            "dev", "silver.customers", pa.table({"id": [1]}), expected_snapshot_id="none"
        )
    assert text in str(caught.value)

    def conflict(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(409, json={"detail": "changed"})

    with pytest.raises(Conflict):
        Tables(_transport(conflict)).load("dev", "bronze.customers", pa.table({"id": [1]}))
    assert len(calls) == 2


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(200, json={"version": "1", "upload_max_bytes": 1024}),
        httpx.Response(200, json={"version": "1"}),
        httpx.Response(200, json={"version": "1", "upload_max_bytes": True}),
        httpx.Response(200, json={"version": "1", "upload_max_bytes": 0}),
        httpx.Response(200, content=b"not json"),
        httpx.Response(404, json={"detail": "no such route"}),
    ],
)
def test_tables_upload_limit_reads_the_info_route_or_says_nothing(answer: httpx.Response) -> None:
    """Only a positive integer counts; an older or odd answer leaves the check to the server."""
    limit = Tables(_transport(lambda request: answer)).upload_limit()

    expected = 1024 if answer.status_code == 200 and b"1024" in answer.content else None
    assert limit == expected


def test_tables_upload_limit_lets_an_unreachable_server_say_so() -> None:
    """A connection failure is not "no limit": it is raised."""

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(LakanaConnectionError):
        Tables(_transport(down)).upload_limit()
