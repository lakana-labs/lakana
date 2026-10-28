"""Tests for governed table creation and Arrow-append routes.

`create_table` and `append_table_data` are the platform API's own write path
for a table created from a caller-declared JSON schema and appended to with
an Arrow IPC stream — distinct from the Iceberg-router path (`/v1/...`)
`test_tables.py` exercises, which speaks the full Iceberg REST protocol. The
SDK needs this simpler surface so "create a table -> query it" works without
a client embedding pyiceberg.
"""

from __future__ import annotations

import importlib
import io
import logging
import random
import socket
import struct
import subprocess
import sys
import threading
import time
import uuid
import warnings
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import httpx
import pyarrow as pa
import pyarrow.ipc
import pytest
import requests
import uvicorn
from fastapi.testclient import TestClient
from pyiceberg.io.pyarrow import schema_to_pyarrow
from pyiceberg.table import StaticTable
from sqlalchemy.orm import Session

from lakana_core.catalog.json_schema import _PRIMITIVE_TYPES, ColumnSpec, to_iceberg_schema
from lakana_core.errors import ValidationError
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.models import QueryRun, QueryRunStatus
from lakana_core.storage.resolver import StorageResolver
from lakana_server.api.platform import schemas
from lakana_server.api.platform.tables import (
    _decode_arrow_stream,
    _dereference_table_offset,
    _is_allowed_arrow_type,
    _media_type,
    _message_declares_compression,
    _validate_batch_before_use,
    _vtable_field_offset,
)
from lakana_server.app import create_app
from lakana_server.errors import PayloadTooLargeError
from lakana_server.settings import AppendConfig, Settings
from lakana_worker.pipeline import run_one
from tests.conftest import AUTH_HEADER
from tests.server_test_helpers import create_environment

_ARROW_STREAM_HEADERS = {**AUTH_HEADER, "Content-Type": "application/vnd.apache.arrow.stream"}
_ID_ONLY_SCHEMA = [{"name": "id", "type": "long", "nullable": False}]


def _ipc_stream_bytes(table: pa.Table) -> bytes:
    """Serialize a pyarrow Table to Arrow IPC **stream** format bytes.

    Parameters
    ----------
    table
        The table to serialize.

    Returns
    -------
    bytes
        The IPC stream bytes, as a governed append request body expects.
    """
    sink = io.BytesIO()
    with pyarrow.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue()


def _ipc_stream_bytes_multi_batch(schema: pa.Schema, batches: list[pa.RecordBatch]) -> bytes:
    """Serialize a list of record batches to an Arrow IPC stream, one message per batch.

    Unlike `_ipc_stream_bytes`, which lets pyarrow choose how to chunk a
    `Table`, this writes exactly the batches given -- needed to build a
    stream with many small batches (to exceed `AppendConfig.max_arrays`) or
    with zero batches at all (to exercise a schema-only check).

    Parameters
    ----------
    schema
        The stream's schema.
    batches
        The batches to write, in order. May be empty.

    Returns
    -------
    bytes
        The IPC stream bytes.
    """
    sink = io.BytesIO()
    with pyarrow.ipc.new_stream(sink, schema) as writer:
        for batch in batches:
            writer.write_batch(batch)
    return sink.getvalue()


def _ipc_stream_bytes_compressed(table: pa.Table, codec: str) -> bytes:
    """Serialize a pyarrow Table to a **compressed** Arrow IPC stream.

    Parameters
    ----------
    table
        The table to serialize.
    codec
        The Arrow IPC body-compression codec, e.g. `"zstd"` or `"lz4"`.

    Returns
    -------
    bytes
        The compressed IPC stream bytes.
    """
    sink = io.BytesIO()
    options = pyarrow.ipc.IpcWriteOptions(compression=codec)
    with pyarrow.ipc.new_stream(sink, table.schema, options=options) as writer:
        writer.write_table(table)
    return sink.getvalue()


class _FakeMessage:
    """A stand-in for `pyarrow.ipc.Message`, carrying only what these tests need.

    Matches this test suite's existing convention (`test_tables.py`'s
    `_to_out` test, `conftest.py`'s `fake_request`) of a lightweight,
    hand-built stand-in for exercising a private helper directly, rather
    than fighting pyarrow's C++ implementation to produce a real `Message`
    with a deliberately malformed internal layout.

    Parameters
    ----------
    metadata
        Raw FlatBuffers-encoded bytes, standing in for `Message.metadata`.
    type_
        The value `Message.type` should report.
    """

    def __init__(self, metadata: bytes, type_: str) -> None:
        self.metadata = pa.py_buffer(metadata)
        self.type = type_


def _create_table(
    client: TestClient, environment: str, namespace: str, name: str, schema: list[dict]
) -> httpx.Response:
    """Create a managed table via the new JSON-schema route.

    Parameters
    ----------
    client
        The test client.
    environment
        Environment name.
    namespace
        Dot-joined namespace path.
    name
        Table name.
    schema
        The `ColumnIn`-shaped column list.

    Returns
    -------
    httpx.Response
        The raw response, so callers can assert on status/body themselves.
    """
    return client.post(
        f"/api/v1/environments/{environment}/namespaces/{namespace}/tables",
        headers=AUTH_HEADER,
        json={"name": name, "schema": schema},
    )


def test_create_table_from_json_schema(client: TestClient) -> None:
    """A table created from a JSON schema is a real Iceberg table with that schema."""
    create_environment(client)
    created = _create_table(
        client,
        "dev",
        "gold",
        "sales",
        [
            {"name": "id", "type": "long", "nullable": False},
            {"name": "amount", "type": "double", "nullable": True},
        ],
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["name"] == "sales"
    assert body["namespace"] == "gold"

    fetched = client.get(
        "/api/v1/environments/dev/namespaces/gold/tables/sales", headers=AUTH_HEADER
    )
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["metadata_location"] == body["metadata_location"]

    schema = StaticTable.from_metadata(body["metadata_location"]).schema()
    assert [f.name for f in schema.fields] == ["id", "amount"]
    assert schema.find_field("id").required is True
    assert schema.find_field("amount").required is False


def test_create_table_outside_medallion_is_422(client: TestClient) -> None:
    """A table in a namespace not rooted at bronze/silver/gold is refused."""
    create_environment(client)
    client.post("/api/v1/environments/dev/namespaces", headers=AUTH_HEADER, json={"path": "raw"})

    response = _create_table(
        client, "dev", "raw", "x", [{"name": "id", "type": "long", "nullable": False}]
    )

    assert response.status_code == 422, response.text


def test_create_existing_table_is_409(client: TestClient) -> None:
    """Creating a table twice with the same name is a 409, not a 500."""
    create_environment(client)
    schema = [{"name": "id", "type": "long", "nullable": False}]

    first = _create_table(client, "dev", "gold", "sales", schema)
    assert first.status_code == 201, first.text

    second = _create_table(client, "dev", "gold", "sales", schema)
    assert second.status_code == 409, second.text


def test_append_then_query_returns_the_rows(client: TestClient) -> None:
    """Appended rows are readable, unchanged, through a query run in-process."""
    create_environment(client)
    _create_table(
        client,
        "dev",
        "gold",
        "sales",
        [
            {"name": "id", "type": "long", "nullable": False},
            {"name": "amount", "type": "double", "nullable": True},
        ],
    )
    data = pa.table(
        {"id": [1, 2, 3], "amount": [1.5, 2.5, None]},
        schema=pa.schema(
            [pa.field("id", pa.int64(), nullable=False), pa.field("amount", pa.float64())]
        ),
    )

    appended = client.post(
        "/api/v1/environments/dev/namespaces/gold/tables/sales/data",
        headers=_ARROW_STREAM_HEADERS,
        content=_ipc_stream_bytes(data),
    )
    assert appended.status_code == 200, appended.text
    result_body = appended.json()
    assert result_body == {"rows": 3, "files": 1, "snapshot_id": result_body["snapshot_id"]}
    assert result_body["snapshot_id"] is not None

    submitted = client.post(
        "/api/v1/environments/dev/queries",
        headers=AUTH_HEADER,
        json={"sql": "SELECT * FROM gold.sales ORDER BY id"},
    )
    assert submitted.status_code == 202, submitted.text
    query_id = uuid.UUID(submitted.json()["query_id"])

    engine = client.app.state.engine
    with Session(engine) as session:
        run_one(session, query_id, GrantAuthorization(session))
        run = session.get(QueryRun, query_id)
        assert run is not None
        assert run.status == QueryRunStatus.SUCCEEDED, run.error_message
        assert run.result_location is not None
        raw = StorageResolver(session).for_environment("dev").storage.read(run.result_location)

    with pyarrow.ipc.open_file(io.BytesIO(raw)) as reader:
        result = reader.read_all()

    assert result.column("id").to_pylist() == [1, 2, 3]
    assert result.column("amount").to_pylist() == [1.5, 2.5, None]
    assert result.schema.field("id").type == pa.int64()
    assert result.schema.field("amount").type == pa.float64()


def _ids(values: list[int]) -> pa.Table:
    """Build rows for a table with one required `id` column.

    Parameters
    ----------
    values
        The ids.

    Returns
    -------
    pa.Table
        The rows.
    """
    return pa.table({"id": values}, schema=pa.schema([pa.field("id", pa.int64(), nullable=False)]))


@pytest.mark.parametrize("method", ["POST", "PUT"])
def test_a_create_pin_on_a_table_with_data_is_refused_before_the_body_is_read(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    """`expected_snapshot_id=none` on a table that has a snapshot is a 409 with no body decoded."""
    create_environment(client)
    _create_table(client, "dev", "gold", "sales", _ID_ONLY_SCHEMA)
    url = "/api/v1/environments/dev/namespaces/gold/tables/sales/data"
    first = client.post(url, headers=_ARROW_STREAM_HEADERS, content=_ipc_stream_bytes(_ids([1])))
    assert first.status_code == 200, first.text

    def body_was_read(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("the body was decoded")

    monkeypatch.setattr("lakana_server.api.platform.tables._decode_arrow_stream", body_was_read)
    refused = client.request(
        method,
        url,
        params={"expected_snapshot_id": "none"},
        headers=_ARROW_STREAM_HEADERS,
        content=_ipc_stream_bytes(_ids([2])),
    )

    assert refused.status_code == 409, refused.text
    assert "already has data" in refused.text


def test_append_without_write_table_is_403(client: TestClient) -> None:
    """A VIEWER-only principal cannot append."""
    create_environment(client)
    _create_table(client, "dev", "gold", "sales", _ID_ONLY_SCHEMA)
    client.post("/api/v1/principals", headers=AUTH_HEADER, json={"name": "bob", "type": "USER"})
    client.post(
        "/api/v1/grants",
        headers=AUTH_HEADER,
        json={"principal": "bob", "role": "VIEWER", "kind": "ENVIRONMENT", "environment": "dev"},
    )
    issued = client.post("/api/v1/principals/bob/tokens", headers=AUTH_HEADER, json={"name": "bob"})
    bob_headers = {
        "Authorization": f"Bearer {issued.json()['token']}",
        "Content-Type": "application/vnd.apache.arrow.stream",
    }
    data = pa.table({"id": [1]}, schema=pa.schema([pa.field("id", pa.int64(), nullable=False)]))

    response = client.post(
        "/api/v1/environments/dev/namespaces/gold/tables/sales/data",
        headers=bob_headers,
        content=_ipc_stream_bytes(data),
    )

    assert response.status_code == 403, response.text


def test_append_wrong_content_type_is_422(client: TestClient) -> None:
    """A body sent with the wrong Content-Type is refused before being decoded."""
    create_environment(client)
    _create_table(client, "dev", "gold", "sales", _ID_ONLY_SCHEMA)
    data = pa.table({"id": [1]}, schema=pa.schema([pa.field("id", pa.int64(), nullable=False)]))

    response = client.post(
        "/api/v1/environments/dev/namespaces/gold/tables/sales/data",
        headers={**AUTH_HEADER, "Content-Type": "application/json"},
        content=_ipc_stream_bytes(data),
    )

    assert response.status_code == 422, response.text


def test_append_malformed_stream_is_422(client: TestClient) -> None:
    """Random bytes with the right Content-Type are a 422, not a 500."""
    create_environment(client)
    _create_table(client, "dev", "gold", "sales", _ID_ONLY_SCHEMA)

    response = client.post(
        "/api/v1/environments/dev/namespaces/gold/tables/sales/data",
        headers=_ARROW_STREAM_HEADERS,
        content=b"not an arrow ipc stream, just filler bytes" * 5,
    )

    assert response.status_code == 422, response.text


def test_append_over_cap_is_413_and_writes_nothing(settings: Settings, tmp_path: Path) -> None:
    """A body over the configured byte cap is refused, and nothing is written."""
    capped = settings.model_copy(update={"append": AppendConfig(max_bytes=1024)})
    with TestClient(create_app(capped)) as client:
        client.post(
            "/api/v1/environments",
            headers=AUTH_HEADER,
            json={
                "name": "dev",
                "storage": {"scheme": "file", "root": f"file://{tmp_path}", "options": {}},
            },
        )
        _create_table(
            client,
            "dev",
            "gold",
            "sales",
            [
                {"name": "id", "type": "long", "nullable": False},
                {"name": "note", "type": "string", "nullable": True},
            ],
        )
        before = client.get(
            "/api/v1/environments/dev/namespaces/gold/tables/sales", headers=AUTH_HEADER
        ).json()

        big_data = pa.table(
            {"id": list(range(2000)), "note": ["x" * 100] * 2000},
            schema=pa.schema(
                [pa.field("id", pa.int64(), nullable=False), pa.field("note", pa.string())]
            ),
        )

        response = client.post(
            "/api/v1/environments/dev/namespaces/gold/tables/sales/data",
            headers=_ARROW_STREAM_HEADERS,
            content=_ipc_stream_bytes(big_data),
        )

        assert response.status_code == 413, response.text

        after = client.get(
            "/api/v1/environments/dev/namespaces/gold/tables/sales", headers=AUTH_HEADER
        ).json()
        assert after["metadata_location"] == before["metadata_location"]

    assert list(tmp_path.rglob("*.parquet")) == []


def test_append_chunked_body_over_cap_is_413(settings: Settings, tmp_path: Path) -> None:
    """The cap is enforced from streamed chunks alone, with no Content-Length header."""
    capped = settings.model_copy(update={"append": AppendConfig(max_bytes=1024)})
    with TestClient(create_app(capped)) as client:
        client.post(
            "/api/v1/environments",
            headers=AUTH_HEADER,
            json={
                "name": "dev",
                "storage": {"scheme": "file", "root": f"file://{tmp_path}", "options": {}},
            },
        )
        _create_table(client, "dev", "gold", "sales", _ID_ONLY_SCHEMA)

        data = pa.table(
            {"id": list(range(2000))},
            schema=pa.schema([pa.field("id", pa.int64(), nullable=False)]),
        )
        body = _ipc_stream_bytes(data)
        chunks = [body[i : i + 100] for i in range(0, len(body), 100)]

        response = client.post(
            "/api/v1/environments/dev/namespaces/gold/tables/sales/data",
            headers=_ARROW_STREAM_HEADERS,
            content=iter(chunks),
        )

        assert response.status_code == 413, response.text

    assert list(tmp_path.rglob("*.parquet")) == []


def test_append_schema_mismatch_is_422_naming_column(client: TestClient) -> None:
    """A type mismatch on a named column is refused, naming that column."""
    create_environment(client)
    _create_table(client, "dev", "gold", "sales", _ID_ONLY_SCHEMA)
    data = pa.table({"id": ["not-a-long"]}, schema=pa.schema([pa.field("id", pa.string())]))

    response = client.post(
        "/api/v1/environments/dev/namespaces/gold/tables/sales/data",
        headers=_ARROW_STREAM_HEADERS,
        content=_ipc_stream_bytes(data),
    )

    assert response.status_code == 422, response.text
    assert "id" in response.json()["detail"]


@pytest.mark.slow
def test_append_records_audit_event(client: TestClient) -> None:
    """A successful append records TABLE_APPENDED with no data values in its detail."""
    create_environment(client)
    _create_table(client, "dev", "gold", "sales", _ID_ONLY_SCHEMA)
    data = pa.table({"id": [1, 2]}, schema=pa.schema([pa.field("id", pa.int64(), nullable=False)]))

    appended = client.post(
        "/api/v1/environments/dev/namespaces/gold/tables/sales/data",
        headers=_ARROW_STREAM_HEADERS,
        content=_ipc_stream_bytes(data),
    )
    assert appended.status_code == 200, appended.text

    events = client.get(
        "/api/v1/audit-events",
        headers=AUTH_HEADER,
        params={"event_type": "TABLE_APPENDED"},
    )
    assert events.status_code == 200, events.text
    listed = events.json()["items"]
    assert len(listed) == 1
    detail = listed[0]["detail"]
    # Exactly {rows, files, snapshot_id} -- no actual column values (the "1"/"2"
    # appended above never appear anywhere in the detail).
    assert detail == {"rows": 2, "files": 1, "snapshot_id": appended.json()["snapshot_id"]}


# --- compression, authorization order, decode failures
# -----------------------------------------------------
#
# Compression defeats the wire byte cap (2,288 bytes of zstd
# decode to 64 MB -- a ~28,000:1 ratio). Authorization ran only
# after the whole body was read and decoded. Decode failures
# beyond the original (pa.ArrowInvalid, OSError) pair -- ArrowNotImplementedError,
# and corrupted-but-readable offsets only caught by full validation -- could
# reach the route unhandled. Minors: schema bounds, extra-field rejection,
# case-insensitive Content-Type parsing.


@pytest.mark.parametrize("codec", ["zstd", "lz4"])
def test_append_compressed_ipc_is_422_without_large_allocation(
    client: TestClient, codec: str
) -> None:
    """A compression bomb is refused outright, never decoded.

    8,000,000 zeros compress to a few KB on the wire but would decode to
    64 MB -- the point of this test is that the request is refused before
    that decode is ever attempted, based on a cheap metadata-only scan.
    """
    create_environment(client)
    _create_table(client, "dev", "gold", "sales", _ID_ONLY_SCHEMA)
    huge = pa.table({"id": pa.array([0] * 8_000_000, pa.int64())})
    body = _ipc_stream_bytes_compressed(huge, codec)
    assert len(body) < 1_000_000  # both codecs land far below the 64 MB decoded size

    response = client.post(
        "/api/v1/environments/dev/namespaces/gold/tables/sales/data",
        headers=_ARROW_STREAM_HEADERS,
        content=body,
    )

    assert response.status_code == 422, response.text
    assert "compressed" in response.json()["detail"].lower()


def test_append_over_cap_body_from_a_viewer_is_403_not_413(settings: Settings) -> None:
    """Authorization is checked before the body is read: a VIEWER gets 403, never 413."""
    capped = settings.model_copy(update={"append": AppendConfig(max_bytes=64)})
    with TestClient(create_app(capped)) as client:
        create_environment(client)
        _create_table(client, "dev", "gold", "sales", _ID_ONLY_SCHEMA)
        client.post("/api/v1/principals", headers=AUTH_HEADER, json={"name": "bob", "type": "USER"})
        client.post(
            "/api/v1/grants",
            headers=AUTH_HEADER,
            json={
                "principal": "bob",
                "role": "VIEWER",
                "kind": "ENVIRONMENT",
                "environment": "dev",
            },
        )
        issued = client.post(
            "/api/v1/principals/bob/tokens", headers=AUTH_HEADER, json={"name": "bob"}
        )
        bob_headers = {
            "Authorization": f"Bearer {issued.json()['token']}",
            "Content-Type": "application/vnd.apache.arrow.stream",
        }
        data = pa.table(
            {"id": list(range(200))},
            schema=pa.schema([pa.field("id", pa.int64(), nullable=False)]),
        )
        body = _ipc_stream_bytes(data)
        assert len(body) > 64

        response = client.post(
            "/api/v1/environments/dev/namespaces/gold/tables/sales/data",
            headers=bob_headers,
            content=body,
        )

    assert response.status_code == 403, response.text


def test_append_to_a_missing_table_is_404_before_body_is_read(client: TestClient) -> None:
    """A nonexistent table is a 404, even for a malformed body -- proving the check runs first."""
    create_environment(client)

    response = client.post(
        "/api/v1/environments/dev/namespaces/gold/tables/nope/data",
        headers=_ARROW_STREAM_HEADERS,
        content=b"not a valid arrow ipc stream at all",
    )

    assert response.status_code == 404, response.text


def test_append_missing_content_type_header_is_422(client: TestClient) -> None:
    """No Content-Type header at all is refused the same as the wrong one."""
    create_environment(client)
    _create_table(client, "dev", "gold", "sales", _ID_ONLY_SCHEMA)
    data = pa.table({"id": [1]}, schema=pa.schema([pa.field("id", pa.int64(), nullable=False)]))

    response = client.post(
        "/api/v1/environments/dev/namespaces/gold/tables/sales/data",
        headers=AUTH_HEADER,
        content=_ipc_stream_bytes(data),
    )

    assert response.status_code == 422, response.text


def test_append_content_type_with_parameters_and_mixed_case_is_accepted(
    client: TestClient,
) -> None:
    """Content-Type is matched on the media type alone, case-insensitively."""
    create_environment(client)
    _create_table(client, "dev", "gold", "sales", _ID_ONLY_SCHEMA)
    data = pa.table({"id": [1]}, schema=pa.schema([pa.field("id", pa.int64(), nullable=False)]))

    response = client.post(
        "/api/v1/environments/dev/namespaces/gold/tables/sales/data",
        headers={
            **AUTH_HEADER,
            "Content-Type": "Application/Vnd.Apache.Arrow.Stream; charset=utf-8",
        },
        content=_ipc_stream_bytes(data),
    )

    assert response.status_code == 200, response.text


def test_create_table_with_extra_field_is_422(client: TestClient) -> None:
    """A caller-sent `location` is refused outright, never silently dropped."""
    create_environment(client)

    response = client.post(
        "/api/v1/environments/dev/namespaces/gold/tables",
        headers=AUTH_HEADER,
        json={"name": "sales", "schema": _ID_ONLY_SCHEMA, "location": "file:///tmp/evil"},
    )

    assert response.status_code == 422, response.text


def test_create_table_too_many_columns_is_422(client: TestClient) -> None:
    """More than 1000 columns is refused."""
    create_environment(client)
    schema = [{"name": f"c{i}", "type": "long", "nullable": True} for i in range(1001)]

    response = _create_table(client, "dev", "gold", "sales", schema)

    assert response.status_code == 422, response.text


def test_create_table_column_name_too_long_is_422(client: TestClient) -> None:
    """A column name over 255 characters is refused."""
    create_environment(client)

    response = _create_table(
        client, "dev", "gold", "sales", [{"name": "c" * 256, "type": "long", "nullable": True}]
    )

    assert response.status_code == 422, response.text


def test_create_table_column_type_too_long_is_422(client: TestClient) -> None:
    """A column type name over 64 characters is refused."""
    create_environment(client)

    response = _create_table(
        client, "dev", "gold", "sales", [{"name": "id", "type": "x" * 65, "nullable": True}]
    )

    assert response.status_code == 422, response.text


def test_create_table_column_with_extra_field_is_422(client: TestClient) -> None:
    """`ColumnIn` also refuses an unrecognised field."""
    create_environment(client)

    response = _create_table(
        client,
        "dev",
        "gold",
        "sales",
        [{"name": "id", "type": "long", "nullable": True, "location": "file:///tmp/evil"}],
    )

    assert response.status_code == 422, response.text


def test_table_create_no_longer_triggers_the_schema_shadow_warning() -> None:
    """Neither `TableCreate` nor `ExternalTableOut` shadow `BaseModel.schema`.

    `pydantic` warns at class-definition time when a field is named
    `schema`, since `BaseModel` itself defines a (deprecated, v1-compat)
    `schema()` method. `TableCreate` renamed its attribute to `columns`
    with `Field(alias="schema")` so the wire format is unchanged; `ExternalTableOut`
    got the same treatment (`table_schema`, aliased to
    `schema`), so the warning is gone for both.
    """
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        importlib.reload(schemas)

    messages = [str(w.message) for w in caught if issubclass(w.category, UserWarning)]
    assert not any("TableCreate" in m for m in messages), messages
    assert not any("ExternalTableOut" in m for m in messages), messages


# --- the uuid type -------------------------------------------------------
#
# The first type allowlist
# omitted pa.uuid(), so a table's uuid column -- a type to_iceberg_schema
# already accepts, that pyiceberg's schema_to_pyarrow already maps to
# pa.uuid(), and that CatalogService.append's own schema check already
# accepted -- was refused (422) by the allowlist alone.


def test_append_uuid_column_is_accepted(client: TestClient) -> None:
    """A `uuid` column round-trips through create + append end to end.

    Before this fix, `_is_allowed_arrow_type` refused `pa.uuid()` outright
    (missing from `_ALLOWED_SCALAR_ARROW_TYPES`), even though every other
    layer of the append path already accepted it: `to_iceberg_schema`
    supports `"uuid"` in a caller's JSON column list, pyiceberg's
    `schema_to_pyarrow` maps `UUIDType()` to `pa.uuid()`, and
    `CatalogService.append`'s own Arrow-vs-Iceberg schema check accepts
    `pa.uuid()` data unmodified -- the allowlist alone was stricter than
    core itself, not just stricter than necessary.
    """
    create_environment(client)
    created = _create_table(
        client, "dev", "gold", "sales", [{"name": "id", "type": "uuid", "nullable": True}]
    )
    assert created.status_code == 201, created.text

    data = pa.table({"id": pa.array([uuid.uuid4().bytes], pa.uuid())})

    response = client.post(
        "/api/v1/environments/dev/namespaces/gold/tables/sales/data",
        headers=_ARROW_STREAM_HEADERS,
        content=_ipc_stream_bytes(data),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["rows"] == 1
    assert body["files"] == 1
    assert body["snapshot_id"] is not None


# --- Unit tests for the decode/compression-detection helpers directly ----


def test_decode_arrow_stream_rejects_compressed_messages() -> None:
    """Compressed IPC (zstd or lz4) is refused before any batch is decoded."""
    huge = pa.table({"id": pa.array([0] * 8_000_000, pa.int64())})
    for codec in ("zstd", "lz4"):
        body = _ipc_stream_bytes_compressed(huge, codec)
        with pytest.raises(ValidationError, match=r"[Cc]ompressed"):
            _decode_arrow_stream(pa.py_buffer(body), max_bytes=10**9, max_arrays=10**9)


def test_decode_arrow_stream_rejects_a_compressed_dictionary_batch() -> None:
    """A compressed dictionary-encoded column is refused, and its batch flagged as compressed.

    The decoder refuses it on the dictionary type before the compression scan
    runs; the scanner itself must still recognise a compressed
    `DictionaryBatch` message, not only a compressed `RecordBatch`.
    """
    dict_array = pa.array(["a" * 200] * 3000).dictionary_encode()
    table = pa.table({"d": dict_array})
    body = _ipc_stream_bytes_compressed(table, "zstd")

    with pytest.raises(ValidationError, match="unsupported Arrow type"):
        _decode_arrow_stream(pa.py_buffer(body), max_bytes=10**9, max_arrays=10**9)

    messages = list(pyarrow.ipc.MessageReader.open_stream(pa.BufferReader(body)))
    dictionary_batches = [m for m in messages if m.type == "dictionary"]
    assert dictionary_batches
    assert all(_message_declares_compression(m) for m in dictionary_batches)


def test_decode_arrow_stream_enforces_a_decode_time_byte_cap() -> None:
    """Decoded size is capped independently of the request's own streaming byte cap."""
    data = pa.table(
        {"id": list(range(2000))}, schema=pa.schema([pa.field("id", pa.int64(), nullable=False)])
    )
    body = _ipc_stream_bytes(data)

    with pytest.raises(PayloadTooLargeError):
        _decode_arrow_stream(pa.py_buffer(body), max_bytes=10, max_arrays=10**9)


def test_decode_arrow_stream_accepts_a_normal_uncompressed_stream() -> None:
    """The happy path still works: uncompressed, under the cap, decodes cleanly."""
    data = pa.table(
        {"id": list(range(10))}, schema=pa.schema([pa.field("id", pa.int64(), nullable=False)])
    )
    body = _ipc_stream_bytes(data)

    result = _decode_arrow_stream(pa.py_buffer(body), max_bytes=10**9, max_arrays=10**9)

    assert result.column("id").to_pylist() == list(range(10))


# --- validation order, CPU-bound decode -------------------------------------------------------
#
# Calling `batch.nbytes` on an unvalidated batch segfaults the
# process for a corrupted dictionary batch. Decode and full
# validation are CPU-bound and ran synchronously on the event loop, and full
# validation's cost is unbounded for a type like `string_view` (many short
# views can reference one large shared buffer). Minor: ColumnIn also gets
# extra="forbid".


def test_decode_arrow_stream_rejects_a_string_view_column() -> None:
    """A `string_view` column is refused by type, before any batch is validated.

    `string_view`'s validation cost is not proportional to wire size: many short views can
    reference one shared,
    large data buffer, so `Table.validate(full=True)`'s UTF-8 check can
    revisit the same underlying bytes once per view with no bound tied to
    a byte cap -- the reviewer's `views.py` probe demonstrated this
    reaching several GiB of logical content from a single-digit-MiB wire
    body. Rejecting the type up front, before any batch reaches full
    validation, closes this regardless of the shared buffer's actual size;
    this test uses a 16-byte one to keep the fixture cheap; the error
    message ("unsupported Arrow type") is one only this up-front schema
    check can produce, not a `Table.validate(full=True)` failure (which
    would name a buffer/offset problem instead), so the assertion below
    also confirms which check actually caught it.
    """
    data = pa.py_buffer(b"a" * 16)
    view = struct.pack("<i4sii", 16, b"aaaa", 0, 0)
    arr = pa.Array.from_buffers(pa.string_view(), 1, [None, pa.py_buffer(view), data])
    t = pa.table({"s": arr})
    sink = io.BytesIO()
    with pyarrow.ipc.new_stream(sink, t.schema) as writer:
        writer.write_table(t)

    with pytest.raises(ValidationError, match=r"unsupported Arrow type"):
        _decode_arrow_stream(pa.py_buffer(sink.getvalue()), max_bytes=10**9, max_arrays=10**9)


def test_decode_arrow_stream_rejects_a_dictionary_typed_column() -> None:
    """A dictionary-encoded column is refused by type, before any batch is decoded."""
    t = pa.table({"k": pa.array(["a", "b"] * 25).dictionary_encode()})
    body = _ipc_stream_bytes(t)

    with pytest.raises(ValidationError, match=r"unsupported Arrow type"):
        _decode_arrow_stream(pa.py_buffer(body), max_bytes=10**9, max_arrays=10**9)


def test_decode_arrow_stream_rejects_a_list_typed_column() -> None:
    """A list column is refused by type, before any batch is decoded."""
    t = pa.table({"l": pa.array([[1, 2], [3, 4]])})
    body = _ipc_stream_bytes(t)

    with pytest.raises(ValidationError, match=r"unsupported Arrow type"):
        _decode_arrow_stream(pa.py_buffer(body), max_bytes=10**9, max_arrays=10**9)


def test_decode_arrow_stream_accepts_every_allowlisted_type() -> None:
    """Every type `_is_allowed_arrow_type` accepts round-trips through the real decoder."""
    fields = {
        "a_int32": pa.array([1], pa.int32()),
        "a_int64": pa.array([1], pa.int64()),
        "a_float32": pa.array([1.0], pa.float32()),
        "a_float64": pa.array([1.0], pa.float64()),
        "a_bool": pa.array([True], pa.bool_()),
        "a_string": pa.array(["x"], pa.string()),
        "a_large_string": pa.array(["x"], pa.large_string()),
        "a_binary": pa.array([b"x"], pa.binary()),
        "a_large_binary": pa.array([b"x"], pa.large_binary()),
        "a_decimal": pa.array([1], pa.decimal128(10, 2)),
        "a_date32": pa.array([1], pa.date32()),
        "a_time64": pa.array([1], pa.time64("us")),
        "a_timestamp": pa.array([1], pa.timestamp("us")),
        "a_timestamp_tz": pa.array([1], pa.timestamp("us", tz="UTC")),
        "a_uuid": pa.array([uuid.uuid4().bytes], pa.uuid()),
    }
    t = pa.table(fields)
    body = _ipc_stream_bytes(t)

    result = _decode_arrow_stream(pa.py_buffer(body), max_bytes=10**9, max_arrays=10**9)

    assert result.column_names == list(fields)
    assert result.num_rows == 1


@pytest.mark.parametrize(
    ("arrow_type", "expected"),
    [
        pytest.param(pa.int32(), True, id="int32"),
        pytest.param(pa.int64(), True, id="int64"),
        pytest.param(pa.float32(), True, id="float32"),
        pytest.param(pa.float64(), True, id="float64"),
        pytest.param(pa.bool_(), True, id="bool"),
        pytest.param(pa.string(), True, id="string"),
        pytest.param(pa.large_string(), True, id="large_string"),
        pytest.param(pa.binary(), True, id="binary"),
        pytest.param(pa.large_binary(), True, id="large_binary"),
        pytest.param(pa.decimal128(10, 2), True, id="decimal128"),
        pytest.param(pa.date32(), True, id="date32"),
        pytest.param(pa.time64("us"), True, id="time64_us"),
        pytest.param(pa.timestamp("us"), True, id="timestamp_us_naive"),
        pytest.param(pa.timestamp("us", tz="UTC"), True, id="timestamp_us_utc"),
        pytest.param(pa.uuid(), True, id="uuid"),
        pytest.param(pa.time64("ns"), False, id="time64_ns_wrong_unit"),
        pytest.param(pa.timestamp("ns"), False, id="timestamp_ns_wrong_unit"),
        pytest.param(pa.int16(), False, id="int16_too_narrow"),
        pytest.param(pa.string_view(), False, id="string_view"),
        pytest.param(pa.binary_view(), False, id="binary_view"),
        pytest.param(pa.list_(pa.int64()), False, id="list"),
        pytest.param(pa.dictionary(pa.int32(), pa.string()), False, id="dictionary"),
        pytest.param(pa.struct([("a", pa.int64())]), False, id="struct"),
    ],
)
def test_is_allowed_arrow_type(arrow_type: pa.DataType, expected: bool) -> None:
    """Direct unit coverage of the allowlist for every branch (parametric and scalar)."""
    assert _is_allowed_arrow_type(arrow_type) is expected


def _build_dictionary_length_corrupted_stream() -> bytes:
    """Deterministically reproduce the reviewer's `crasher.bin` (2,120 bytes).

    Builds the same base table `crash.py` used (`id`/`s`/`d`/`k`, `k` being
    a dictionary-encoded string column), then patches the dictionary
    batch's inner `RecordBatch`'s first `FieldNode.length` -- not the
    `RecordBatch`'s own top-level `length` field, which alone does not
    reproduce the crash, since array construction reads the per-field
    `FieldNode` length, not the batch-level summary -- from 2 to
    2,231,369,730, matching the reviewer's report exactly (confirmed by
    the identical `pyarrow.lib.ArrowInvalid` message this produces:
    "Buffer #1 too small in array of type string and length
    2231369730: expected at least 8925478920 byte(s), got 12").

    Returns
    -------
    bytes
        A 2,120-byte uncompressed Arrow IPC stream with the corrupted
        dictionary length, byte-for-byte equivalent in effect to
        `crasher.bin`.
    """
    t = pa.table(
        {
            "id": pa.array(range(50), pa.int64()),
            "s": pa.array([str(i) for i in range(50)]),
            "d": pa.array([1.5] * 50),
            "k": pa.array(["a", "b"] * 25).dictionary_encode(),
        }
    )
    sink = io.BytesIO()
    with pyarrow.ipc.new_stream(sink, t.schema) as writer:
        writer.write_table(t)
    body = sink.getvalue()

    messages = list(pyarrow.ipc.MessageReader.open_stream(pa.BufferReader(body)))
    dictionary_message = next(m for m in messages if m.type == "dictionary")
    meta = dictionary_message.metadata.to_pybytes()

    # Message.header (field 2) -> DictionaryBatch table.
    root = struct.unpack_from("<I", meta, 0)[0]
    header_field = _vtable_field_offset(meta, root, 2)
    assert header_field is not None
    header_table = _dereference_table_offset(meta, header_field)
    # DictionaryBatch.data (field 1) -> the nested RecordBatch table.
    data_field = _vtable_field_offset(meta, header_table, 1)
    assert data_field is not None
    record_batch_table = _dereference_table_offset(meta, data_field)
    # RecordBatch.nodes (field 1) -> a vector of inline FieldNode structs
    # (length: int64, null_count: int64), 16 bytes each; the vector itself
    # is prefixed by a 4-byte element count.
    nodes_field = _vtable_field_offset(meta, record_batch_table, 1)
    assert nodes_field is not None
    nodes_vector_pos = _dereference_table_offset(meta, nodes_field)
    first_node_length_pos = nodes_vector_pos + 4

    assert meta in body and body.count(meta) == 1, "dictionary message metadata must be unique"
    absolute_pos = body.index(meta) + first_node_length_pos
    corrupted = bytearray(body)
    corrupted[absolute_pos : absolute_pos + 8] = struct.pack("<q", 2_231_369_730)
    return bytes(corrupted)


@pytest.mark.slow
def test_decode_arrow_stream_survives_a_deterministic_dictionary_length_corruption(
    tmp_path: Path,
) -> None:
    """A byte-patched dictionary length is refused cleanly, never crashing the process.

    This is an allowlist-plus-crash regression, not a proof of the
    validate-before-nbytes ordering specifically: `k`'s dictionary type is
    refused by `_is_allowed_arrow_type`
    before any batch is ever decoded, so *this* payload no longer reaches
    the per-batch loop `_validate_batch_before_use` guards (see
    `test_validate_batch_before_use_calls_validate_before_nbytes`
    below for a direct, type-independent test of that ordering itself).
    This test still earns its place as a regression guard on the whole
    pipeline: before validation was moved ahead of it, calling `.nbytes`
    on a batch without validating it first segfaulted the process outright
    on exactly this corruption (confirmed separately: the pre-fix ordering
    reproducibly exits with SIGSEGV, returncode -11, on this same payload).
    A segfault is not a Python exception -- no `except` clause anywhere in
    this process could ever catch it -- so this runs the decode in a
    *subprocess* specifically so that a future regression (in either the
    allowlist or the ordering) shows up as a failed assertion here, rather
    than killing the entire pytest run without a traceback.
    """
    payload_path = tmp_path / "corrupted.arrow"
    payload_path.write_bytes(_build_dictionary_length_corrupted_stream())

    script = (
        "import pyarrow as pa\n"
        "from lakana_server.api.platform.tables import _decode_arrow_stream\n"
        "from lakana_core.errors import ValidationError\n"
        f"body = open({str(payload_path)!r}, 'rb').read()\n"
        "try:\n"
        "    _decode_arrow_stream(pa.py_buffer(body), 1 << 28, 1 << 28)\n"
        "    print('ACCEPTED')\n"
        "except ValidationError as exc:\n"
        "    print('REFUSED:', exc)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=30
    )

    assert result.returncode == 0, (
        f"subprocess did not exit cleanly (returncode={result.returncode}, a negative "
        f"value means it was killed by a signal -- e.g. -11 is SIGSEGV); "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "REFUSED" in result.stdout, result.stdout


def test_validate_batch_before_use_calls_validate_before_nbytes() -> None:
    """`_validate_batch_before_use` calls `.validate(full=True)` strictly before `.nbytes`.

    This is the actual fix, tested
    directly and unconditionally -- independent of the type allowlist,
    which (per its own reproduction) now also happens to catch
    the one PoC that originally motivated this fix. `pyarrow.RecordBatch` is an immutable extension
    type
    (`TypeError: cannot set 'validate' attribute of immutable type
    'pyarrow.lib.RecordBatch'`), so it cannot be monkeypatched to record
    call order; a plain recording stand-in exercises the exact same
    attribute-access sequence `_validate_batch_before_use` performs on a
    real batch, without needing one.
    """
    calls: list[str] = []

    class _RecordingBatch:
        """A minimal stand-in recording `validate`/`nbytes` access order."""

        def validate(self, full: bool = False) -> None:
            assert full is True
            calls.append("validate")

        @property
        def nbytes(self) -> int:
            calls.append("nbytes")
            return 123

    result = _validate_batch_before_use(_RecordingBatch())

    assert calls == ["validate", "nbytes"]
    assert result == 123


def test_decode_arrow_stream_catches_arrow_not_implemented_error() -> None:
    """A schema pyarrow itself can't represent is a ValidationError, not a crash.

    Two byte flips inside this stream's schema message make pyarrow read one
    field as a >64-bit integer type, which it explicitly does not support.
    Deterministic: found by targeted fuzzing of this exact stream.
    """
    t = pa.table(
        {
            "id": pa.array(range(50), pa.int64()),
            "s": pa.array([str(i) for i in range(50)]),
            "d": pa.array([1.5] * 50),
        }
    )
    sink = io.BytesIO()
    with pyarrow.ipc.new_stream(sink, t.schema) as writer:
        writer.write_table(t)
    corrupted = bytearray(sink.getvalue())
    corrupted[1167] = 127
    corrupted[216] = 184

    with pytest.raises(ValidationError):
        _decode_arrow_stream(pa.py_buffer(bytes(corrupted)), max_bytes=10**9, max_arrays=10**9)


@pytest.mark.parametrize(
    "patched_offsets",
    [
        pytest.param(struct.pack("<4i", 0, 12, 0, 12), id="non_monotonic"),
        pytest.param(struct.pack("<4i", 0, 4, 8, 4096), id="out_of_bounds_last"),
    ],
)
def test_decode_arrow_stream_full_validates_corrupted_offsets(patched_offsets: bytes) -> None:
    """Corrupted string offsets are caught by full validation, not left for a later crash.

    Both shapes decode "successfully" under pyarrow's lighter default
    validation and only fail unpredictably the first time something
    downstream touches the bad bytes (observed as a bare `SystemError` from
    a `.cast()` on the corrupted array) unless full validation catches them
    here first.
    """
    t = pa.table({"s": pa.array(["aaaa", "bbbb", "cccc"], pa.string())})
    sink = io.BytesIO()
    with pyarrow.ipc.new_stream(sink, t.schema) as writer:
        writer.write_table(t)
    body = sink.getvalue()
    good_offsets = struct.pack("<4i", 0, 4, 8, 12)
    assert body.count(good_offsets) == 1
    corrupted = body.replace(good_offsets, patched_offsets)

    with pytest.raises(ValidationError):
        _decode_arrow_stream(pa.py_buffer(corrupted), max_bytes=10**9, max_arrays=10**9)


def _fuzz_base_table() -> pa.Table:
    """Build the base table the random-corruption fuzz tests mutate.

    Includes a dictionary column (`k`) and a list column (`l`) alongside
    plain scalar/string columns (a regression campaign added these two to the base after finding
    that an
    unvalidated dictionary batch's `.nbytes` could segfault the process;
    keeping them in the committed fuzz base means future fuzzing keeps
    exercising that exact structural shape, even though today's fixed code
    now refuses both types outright via `_is_allowed_arrow_type` before a
    batch is ever decoded).

    Returns
    -------
    pa.Table
        A five-column table: `id` (int64), `s` (string), `d` (double), `k`
        (dictionary-encoded string), `l` (list of int64).
    """
    return pa.table(
        {
            "id": pa.array(range(50), pa.int64()),
            "s": pa.array([str(i) for i in range(50)]),
            "d": pa.array([1.5] * 50),
            "k": pa.array(["a", "b"] * 25).dictionary_encode(),
            "l": pa.array([[1, 2]] * 50),
        }
    )


def test_decode_arrow_stream_never_leaks_a_raw_exception_under_random_corruption() -> None:
    """Reuses the reviewer's fuzz approach: random 1-4 byte flips never escape unhandled.

    Every outcome must be a clean decode or one of this module's own
    handled exceptions -- never some other exception type (an uncaught
    Python exception) and never a process crash (which no Python-level
    assertion here could even observe -- see
    `test_decode_arrow_stream_survives_a_deterministic_dictionary_length_corruption`
    below for why that specific regression runs in a subprocess instead).
    This loop is exactly how the bare `UnicodeDecodeError` gap (`Table.validate(full=True)` on a
    corrupted
    string array can raise it directly, and it is not a
    `pyarrow.lib.ArrowException`) was originally found.
    """
    sink = io.BytesIO()
    with pyarrow.ipc.new_stream(sink, _fuzz_base_table().schema) as writer:
        writer.write_table(_fuzz_base_table())
    base = sink.getvalue()
    rnd = random.Random(0)
    saw_a_handled_failure = False
    for _ in range(2000):
        mutated = bytearray(base)
        for _ in range(rnd.randint(1, 4)):
            pos = rnd.randrange(len(mutated))
            mutated[pos] = rnd.randrange(256)
        try:
            _decode_arrow_stream(pa.py_buffer(bytes(mutated)), max_bytes=10**9, max_arrays=10**9)
        except (ValidationError, PayloadTooLargeError):
            saw_a_handled_failure = True
        except Exception as exc:
            pytest.fail(f"leaked {type(exc).__name__}: {exc}")
    assert saw_a_handled_failure


def test_is_allowed_arrow_type_accepts_every_core_primitive_type() -> None:
    """The server's allowlist accepts every Arrow type core's own primitive map can produce.

    Three hand-kept type lists exist for the append path: the server's
    `_ALLOWED_SCALAR_ARROW_TYPES`/`_is_allowed_arrow_type`, core's
    `json_schema` (`_PRIMITIVE_TYPES` plus `decimal(p,s)`) and
    `append._types_compatible`, and the SDK's `_arrow._SIMPLE_ARROW_TO_JSON`.
    Nothing keeps them in sync except tests like this one: drive every primitive type name core can
    produce through
    the exact conversion `CatalogService.append`'s own schema check uses
    (`to_iceberg_schema` -> `schema_to_pyarrow`), then assert the server's
    predicate accepts every resulting Arrow type -- a regression here means
    a legitimate caller's column would be refused before `CatalogService.
    append` ever gets a chance to accept it.
    """
    type_names = [*_PRIMITIVE_TYPES, "decimal(10,2)"]
    columns = [ColumnSpec(name=name, type=name, nullable=True) for name in type_names]
    iceberg_schema = to_iceberg_schema(columns)
    arrow_schema = schema_to_pyarrow(iceberg_schema)

    for field in arrow_schema:
        assert _is_allowed_arrow_type(field.type), f"{field.name}: {field.type}"


def test_message_declares_compression_returns_false_when_header_field_is_absent() -> None:
    """A message whose vtable doesn't even reach the `header` field slot is treated as uncompressed.

    Hand-built minimal FlatBuffers bytes: a root table whose vtable has no
    field-2 (`header`) entry at all -- a shape a real pyarrow writer never
    produces, but one `_message_declares_compression` must still handle
    without raising, since a malformed message this shallow is caught by
    the real decode step right after, not by this compression pre-scan.
    """
    buf = struct.pack("<I", 8) + struct.pack("<HH", 4, 4) + struct.pack("<i", 4)

    assert _message_declares_compression(_FakeMessage(buf, "record batch")) is False


def test_message_declares_compression_returns_false_when_dictionary_data_field_is_absent() -> None:
    """A dictionary-batch message whose header has no `data` field is treated as uncompressed."""
    buf = bytearray(34)
    buf[0:4] = struct.pack("<I", 14)
    buf[4:14] = struct.pack("<HHHHH", 10, 12, 0, 0, 4)
    buf[14:18] = struct.pack("<i", 10)
    buf[18:22] = struct.pack("<I", 12)
    buf[22:26] = b"\x00\x00\x00\x00"
    buf[26:30] = struct.pack("<HH", 4, 4)
    buf[30:34] = struct.pack("<i", 4)

    assert _message_declares_compression(_FakeMessage(bytes(buf), "dictionary")) is False


def test_message_declares_compression_raises_arrow_invalid_on_truncated_metadata() -> None:
    """Metadata too short to hold even the root offset is a decode failure, not a Python crash."""
    with pytest.raises(pa.ArrowInvalid):
        _message_declares_compression(_FakeMessage(b"\x00\x00", "record batch"))


def test_vtable_field_offset_returns_none_for_a_field_present_but_zero() -> None:
    """A field explicitly written as offset 0 (flatc's own 'absent' marker) is `None`, not `0`."""
    vtable = struct.pack("<HH", 6, 4) + struct.pack("<H", 0)
    table = struct.pack("<i", 6)
    buf = vtable + table

    assert _vtable_field_offset(buf, table_pos=len(vtable), field_index=0) is None


def test_media_type_returns_empty_string_for_a_missing_header() -> None:
    """`None` (no header sent) maps to `''`, which never matches a real media type."""
    assert _media_type(None) == ""


# --- pool exhaustion, concurrency --
#
# `check_can_append` used to hold the request's pooled database connection for
# the whole client-paced `request.stream()` read. The reviewer reproduced 16
# stalled uploads exhausting the default connection pool (5 + 10 overflow)
# and freezing an unrelated request for the pool-checkout timeout. These
# tests need a real socket, not the in-process `TestClient`: a client-side
# blocking call inside a body generator driven through `TestClient`'s ASGI
# transport was empirically found to block the *whole* shared event loop
# (verified by timing an unrelated request alongside a `time.sleep`-based
# stalled body: it took just as long as the stall, not the few
# milliseconds a genuinely independent request should), which would make
# every one of these tests pass regardless of whether the real fix is
# present. A real `uvicorn` server on a real loopback socket does not have
# this problem: the server's event loop is free to serve other connections
# while one socket simply hasn't sent its next byte yet.


def _free_port() -> int:
    """Find a currently-unbound TCP port on loopback.

    Returns
    -------
    int
        A port not currently bound, suitable for uvicorn to listen on.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def _running_app(settings: Settings) -> Generator[tuple[str, int], None, None]:
    """Run `create_app(settings)` on a real uvicorn server in a background thread.

    Parameters
    ----------
    settings
        Configuration to build the app with.

    Yields
    ------
    tuple of (str, int)
        The server's base URL and the port it is listening on.
    """
    app = create_app(settings)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    assert server.started, "uvicorn did not start within 10 seconds"
    try:
        yield f"http://127.0.0.1:{port}", port
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def _open_stalled_append_connection(port: int, path: str) -> socket.socket:
    """Open a raw socket and send an append request whose chunked body never completes.

    Sends valid request headers and one chunk of body, but never sends the
    terminating zero-length chunk -- the server's `request.stream()` is left
    waiting for more bytes that never arrive, exactly like a real client
    that stalled mid-upload.

    Parameters
    ----------
    port
        The server's loopback port.
    path
        The append route's path, e.g. `/api/v1/environments/dev/namespaces/
        gold/tables/sales/data`.

    Returns
    -------
    socket.socket
        The open, connected socket. The caller must close it.
    """
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    request = (
        f"POST {path} HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{port}\r\n"
        f"Authorization: Bearer lakana_pat_testkey_testsecret\r\n"
        f"Content-Type: application/vnd.apache.arrow.stream\r\n"
        f"Transfer-Encoding: chunked\r\n"
        f"Connection: keep-alive\r\n"
        f"\r\n"
        f"1\r\n\x00\r\n"
    ).encode()
    sock.sendall(request)
    return sock


def _setup_dev_sales_table(base_url: str, tmp_path: Path) -> None:
    """Create the `dev` environment and a `gold.sales` table over real HTTP.

    Parameters
    ----------
    base_url
        The running server's base URL.
    tmp_path
        Where the environment's file storage root should live.
    """
    created = requests.post(
        f"{base_url}/api/v1/environments",
        headers=AUTH_HEADER,
        json={
            "name": "dev",
            "storage": {"scheme": "file", "root": f"file://{tmp_path}", "options": {}},
        },
        timeout=5,
    )
    assert created.status_code == 201, created.text
    created = requests.post(
        f"{base_url}/api/v1/environments/dev/namespaces/gold/tables",
        headers=AUTH_HEADER,
        json={"name": "sales", "schema": _ID_ONLY_SCHEMA},
        timeout=5,
    )
    assert created.status_code == 201, created.text


_APPEND_PATH = "/api/v1/environments/dev/namespaces/gold/tables/sales/data"


def test_append_stalled_uploads_do_not_exhaust_the_connection_pool(
    settings: Settings, tmp_path: Path
) -> None:
    """16 stalled uploads must not freeze an unrelated request.

    `max_concurrent` is raised well above 16 so the concurrency cap (1b)
    cannot itself be the reason the 17th request succeeds -- this test is
    isolated to the connection-release fix alone. With SQLAlchemy's default
    pool (5 + 10 overflow = 15 connections), 16 uploads whose
    `check_can_append` connection was never released would exhaust it,
    exactly the reviewer's reproduction.
    """
    capped = settings.model_copy(update={"append": AppendConfig(max_concurrent=20)})
    with _running_app(capped) as (base_url, port):
        _setup_dev_sales_table(base_url, tmp_path)

        stalled = [_open_stalled_append_connection(port, _APPEND_PATH) for _ in range(16)]
        try:
            time.sleep(0.5)  # let every upload's pre-check and rollback complete

            start = time.monotonic()
            response = requests.get(
                f"{base_url}/api/v1/environments/dev/namespaces/gold/tables",
                headers=AUTH_HEADER,
                timeout=5,
            )
            elapsed = time.monotonic() - start
        finally:
            for sock in stalled:
                sock.close()

    assert response.status_code == 200, response.text
    assert elapsed < 3.0, f"an unrelated request took {elapsed:.1f}s -- the pool was exhausted"


def test_append_over_the_concurrency_cap_is_503_with_retry_after(
    settings: Settings, tmp_path: Path
) -> None:
    """A request past `max_concurrent` is refused with 503 and `Retry-After: 1`."""
    capped = settings.model_copy(update={"append": AppendConfig(max_concurrent=1)})
    with _running_app(capped) as (base_url, port):
        _setup_dev_sales_table(base_url, tmp_path)

        holder = _open_stalled_append_connection(port, _APPEND_PATH)
        try:
            time.sleep(0.3)  # let the held connection's request actually claim its slot

            data = pa.table(
                {"id": [1]}, schema=pa.schema([pa.field("id", pa.int64(), nullable=False)])
            )
            response = requests.post(
                f"{base_url}{_APPEND_PATH}",
                headers=_ARROW_STREAM_HEADERS,
                data=_ipc_stream_bytes(data),
                timeout=5,
            )
        finally:
            holder.close()

    assert response.status_code == 503, response.text
    assert response.headers.get("Retry-After") == "1"
    assert response.json()["type"] == "AppendConcurrencyLimitError"


def test_append_body_read_deadline_is_408(settings: Settings, tmp_path: Path) -> None:
    """A client that stops sending mid-upload is refused with 408."""
    capped = settings.model_copy(update={"append": AppendConfig(read_timeout_seconds=0.2)})
    with _running_app(capped) as (base_url, port):
        _setup_dev_sales_table(base_url, tmp_path)

        sock = _open_stalled_append_connection(port, _APPEND_PATH)
        try:
            sock.settimeout(5)
            start = time.monotonic()
            raw = sock.recv(4096)
            elapsed = time.monotonic() - start
        finally:
            sock.close()

    status_line = raw.split(b"\r\n", 1)[0].decode()
    assert " 408 " in status_line, status_line
    assert elapsed < 4.0, f"the deadline took {elapsed:.1f}s to fire"


# --- many tiny batches, wide schemas --
#
# The decoded-size cap only counted buffer bytes. Each record batch costs
# roughly 1-3 KB of object overhead in decode and again in pyiceberg,
# independent of its byte size: the reviewer measured 1.7M one-row batches
# inside a 256 MiB body costing +1.46 GB RSS and 7s to decode, then +3.3 GB
# and 32s in the pyiceberg append.


def test_decode_arrow_stream_refuses_schema_over_1000_columns() -> None:
    """A schema declaring over 1000 columns is refused before any batch is decoded."""
    schema = pa.schema([pa.field(f"c{i}", pa.int64()) for i in range(1001)])
    body = _ipc_stream_bytes_multi_batch(schema, [])

    with pytest.raises(ValidationError, match="1000"):
        _decode_arrow_stream(pa.py_buffer(body), max_bytes=10**9, max_arrays=10**9)


def test_decode_arrow_stream_stops_at_max_arrays_before_decoding_every_batch() -> None:
    """Many one-row batches past `max_arrays` are refused quickly, not after decoding all."""
    schema = pa.schema([pa.field("id", pa.int64(), nullable=False)])
    batches = [pa.RecordBatch.from_pydict({"id": [i]}, schema=schema) for i in range(20_000)]
    body = _ipc_stream_bytes_multi_batch(schema, batches)

    start = time.monotonic()
    with pytest.raises(PayloadTooLargeError, match="array"):
        _decode_arrow_stream(pa.py_buffer(body), max_bytes=10**9, max_arrays=5)
    elapsed = time.monotonic() - start

    assert elapsed < 2.0, f"took {elapsed:.2f}s -- looks like every batch was still decoded"


def test_decode_arrow_stream_combines_chunks_before_returning() -> None:
    """The decoded table is combined into one chunk per column."""
    schema = pa.schema([pa.field("id", pa.int64(), nullable=False)])
    batches = [pa.RecordBatch.from_pydict({"id": [i]}, schema=schema) for i in range(5)]
    body = _ipc_stream_bytes_multi_batch(schema, batches)

    result = _decode_arrow_stream(pa.py_buffer(body), max_bytes=10**9, max_arrays=10**9)

    assert result.column(0).num_chunks == 1


def test_append_schema_over_1000_columns_is_422(client: TestClient) -> None:
    """A stream whose own schema declares over 1000 columns is refused end to end."""
    create_environment(client)
    _create_table(client, "dev", "gold", "sales", _ID_ONLY_SCHEMA)

    wide_schema = pa.schema([pa.field(f"c{i}", pa.int64()) for i in range(1001)])
    body = _ipc_stream_bytes_multi_batch(wide_schema, [])

    response = client.post(
        "/api/v1/environments/dev/namespaces/gold/tables/sales/data",
        headers=_ARROW_STREAM_HEADERS,
        content=body,
    )

    assert response.status_code == 422, response.text


def test_append_over_max_arrays_is_413(settings: Settings, tmp_path: Path) -> None:
    """Many one-row batches exceeding `max_arrays` are refused with 413, end to end."""
    capped = settings.model_copy(update={"append": AppendConfig(max_arrays=5)})
    with TestClient(create_app(capped)) as client:
        create_environment(client)
        _create_table(client, "dev", "gold", "sales", _ID_ONLY_SCHEMA)

        schema = pa.schema([pa.field("id", pa.int64(), nullable=False)])
        batches = [pa.RecordBatch.from_pydict({"id": [i]}, schema=schema) for i in range(50)]
        body = _ipc_stream_bytes_multi_batch(schema, batches)

        response = client.post(
            "/api/v1/environments/dev/namespaces/gold/tables/sales/data",
            headers=_ARROW_STREAM_HEADERS,
            content=body,
        )

    assert response.status_code == 413, response.text
    assert "array" in response.json()["detail"].lower()


# --- zero-column schemas -----------------------------------------------------
#
# A zero-column schema defeats both C2 caps -- a zero-column
# batch contributes ~0 to the byte cap and exactly 0 to the array cap, so a
# stream of arbitrarily many zero-column batches decodes unbounded, at
# +723 MB RSS for 2.5M empty batches in the reviewer's reproduction.


@pytest.mark.parametrize(
    ("schema", "message"),
    [
        (pa.schema([]), "no columns"),
        (pa.schema([pa.field(f"c{i}", pa.int8()) for i in range(1001)]), "maximum is 1000"),
        (pa.schema([pa.field("d", pa.dictionary(pa.int8(), pa.string()))]), "unsupported"),
    ],
)
def test_decode_arrow_stream_refuses_a_bad_schema_before_the_compression_prescan(
    monkeypatch: pytest.MonkeyPatch, schema: pa.Schema, message: str
) -> None:
    """A schema refused on its own is refused before any batch message is scanned.

    The compression pre-scan walks every message in the stream, so refusing
    a bad schema only after it cost seconds of CPU on a many-message stream.

    Parameters
    ----------
    monkeypatch
        Replaces `_message_declares_compression` with a spy.
    schema
        A schema the decoder refuses without looking at any batch.
    message
        A fragment of the expected refusal.
    """
    scanned: list[object] = []
    monkeypatch.setattr(
        "lakana_server.api.platform.tables._message_declares_compression",
        lambda m: scanned.append(m) or False,
    )
    sink = io.BytesIO()
    with pyarrow.ipc.new_stream(sink, schema):
        pass

    with pytest.raises(ValidationError, match=message):
        _decode_arrow_stream(pa.py_buffer(sink.getvalue()), max_bytes=10**9, max_arrays=10**9)

    assert scanned == []


def test_decode_arrow_stream_refuses_a_zero_column_schema_before_decoding_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A zero-column stream is refused once its schema is read, before any batch is touched.

    The proof is structural, not a wall-clock bound: every decoded batch goes
    through `_validate_batch_before_use`, so a spy on it shows the batch loop
    was never entered. (A timing assertion here flaked on slow CI runners.)

    Parameters
    ----------
    monkeypatch
        Replaces `_validate_batch_before_use` with a spy.
    """
    validated: list[pa.RecordBatch] = []
    monkeypatch.setattr(
        "lakana_server.api.platform.tables._validate_batch_before_use", validated.append
    )
    empty_schema = pa.schema([])
    empty_batch = pa.RecordBatch.from_arrays([], schema=empty_schema)
    body = _ipc_stream_bytes_multi_batch(empty_schema, [empty_batch] * 1_000)

    with pytest.raises(ValidationError, match="no columns"):
        _decode_arrow_stream(pa.py_buffer(body), max_bytes=10**9, max_arrays=10**9)

    assert validated == []


def test_append_zero_column_schema_is_422(client: TestClient) -> None:
    """A zero-column append stream is refused end to end."""
    create_environment(client)
    _create_table(client, "dev", "gold", "sales", _ID_ONLY_SCHEMA)
    empty_schema = pa.schema([])
    body = _ipc_stream_bytes_multi_batch(empty_schema, [])

    response = client.post(
        "/api/v1/environments/dev/namespaces/gold/tables/sales/data",
        headers=_ARROW_STREAM_HEADERS,
        content=body,
    )

    assert response.status_code == 422, response.text
    assert "no columns" in response.json()["detail"].lower()


class _RecordingHandler(logging.Handler):
    """A logging handler that just remembers every record at or above its level.

    Attached directly to the `"uvicorn.error"` logger, not relied on via
    pytest's `caplog` fixture: uvicorn's default logging configuration sets
    `propagate=False` on its own loggers, so a handler attached only to the
    root logger (as `caplog` does) never sees anything uvicorn logs —
    confirmed empirically: a first version of this test using `caplog`
    passed even with the bug (the `ClientDisconnect` traceback) still
    present, because it wasn't actually observing uvicorn's output at all.

    Parameters
    ----------
    level
        The minimum level to record.
    """

    def __init__(self, level: int) -> None:
        super().__init__(level=level)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        """Remember `record`.

        Parameters
        ----------
        record
            The log record being emitted.
        """
        self.records.append(record)


def test_append_client_disconnect_releases_the_slot_and_logs_nothing_at_error(
    settings: Settings, tmp_path: Path
) -> None:
    """A client that disconnects mid-upload releases its slot and is not logged as an error.

    Letting Starlette's `ClientDisconnect` propagate unhandled
    used to make it log "Exception in ASGI application" at `ERROR`, with a
    full traceback, for the ordinary, expected case of a client hanging up
    mid-upload -- not a bug in the application. `max_concurrent=1` makes a
    leaked slot observable: if the disconnected request's slot were never
    released, a second, complete append would be refused with 503.
    """
    capped = settings.model_copy(update={"append": AppendConfig(max_concurrent=1)})
    uvicorn_error_logger = logging.getLogger("uvicorn.error")
    handler = _RecordingHandler(logging.ERROR)
    with _running_app(capped) as (base_url, port):
        # Attached only once the server has actually started, not before: uvicorn's own
        # startup configures logging (`logging.config.dictConfig`), which replaces
        # `"uvicorn.error"`'s handler list -- a handler attached before that point is
        # silently dropped (confirmed empirically: an earlier version of this test attached
        # the handler before `_running_app`, and it never saw anything, bug present or not).
        uvicorn_error_logger.addHandler(handler)
        try:
            _setup_dev_sales_table(base_url, tmp_path)

            sock = _open_stalled_append_connection(port, _APPEND_PATH)
            time.sleep(0.3)  # let the route actually claim its slot and start reading
            sock.close()  # disconnect mid-upload, without ever completing the body
            time.sleep(0.3)  # give the server a moment to notice and release the slot

            data = pa.table(
                {"id": [1]}, schema=pa.schema([pa.field("id", pa.int64(), nullable=False)])
            )
            response = requests.post(
                f"{base_url}{_APPEND_PATH}",
                headers=_ARROW_STREAM_HEADERS,
                data=_ipc_stream_bytes(data),
                timeout=5,
            )
        finally:
            uvicorn_error_logger.removeHandler(handler)

    assert response.status_code == 200, response.text
    error_messages = [record.getMessage() for record in handler.records]
    assert not error_messages, error_messages


def test_create_table_with_a_dotted_name_is_422(client: TestClient) -> None:
    """A dotted table name is refused with a message naming it."""
    create_environment(client)

    response = _create_table(
        client, "dev", "gold", "a.b", [{"name": "id", "type": "long", "nullable": False}]
    )

    assert response.status_code == 422, response.text
    assert "'a.b'" in response.text
