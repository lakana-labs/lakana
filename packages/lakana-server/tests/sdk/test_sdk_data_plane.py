"""End-to-end tests: create/append/query round trip through the real SDK.

Every test here drives the platform API only through `lakana.Client` — the
same REST surface any external SDK user speaks — except for the handful of
test-setup calls (issuing a principal's own token, granting a role) that
have no SDK-level equivalent yet and go through the raw `client` fixture
with the bootstrap `AUTH_HEADER` instead. A query is run to completion
in-process via `lakana_worker.pipeline.run_one`, matching the pattern in
`packages/lakana-server/tests/api/platform/test_tables_append.py` and
`test_column_masks.py`.
"""

from __future__ import annotations

import io
import uuid
from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path

import polars as pl
import pyarrow as pa
import pyarrow.ipc
import pytest
from fastapi.testclient import TestClient
from pyiceberg.io import FileIO
from pyiceberg.table.metadata import TableMetadata
from sqlalchemy import update
from sqlalchemy.orm import Session

import lakana
from lakana_core.catalog import service as service_module
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.models import Table
from lakana_server.app import create_app
from lakana_server.settings import AppendConfig, Settings
from lakana_worker.pipeline import run_one
from tests.conftest import AUTH_HEADER


def _run_worker_once(client: TestClient, query_id: str) -> None:
    """Run one already-queued query run to completion, in-process.

    Parameters
    ----------
    client
        The test client, used to reach the app's database engine.
    query_id
        The submitted query's id, as returned by the SDK.
    """
    engine = client.app.state.engine
    with Session(engine) as session:
        run_one(session, uuid.UUID(query_id), GrantAuthorization(session))


def test_sdk_create_append_query_round_trip(
    lakana_client: lakana.Client, client: TestClient, tmp_path: Path
) -> None:
    """Bootstrap PAT -> environment -> table -> append -> query -> matching pyarrow.Table."""
    lakana_client.environments.create(
        "dev", storage={"scheme": "file", "root": f"file://{tmp_path}", "options": {}}
    )
    schema = pa.schema(
        [pa.field("id", pa.int64(), nullable=False), pa.field("region", pa.string())]
    )
    lakana_client.tables.create("dev", "bronze.sales", schema=schema)
    data = pa.table({"id": [1, 2, 3], "region": ["eu", "us", "eu"]}, schema=schema)

    appended = lakana_client.tables.append("dev", "bronze.sales", data)
    assert appended.rows == 3

    run = lakana_client.queries.submit(
        "SELECT id, region FROM bronze.sales ORDER BY id", environment="dev"
    )
    _run_worker_once(client, run.id)

    result = run.result()
    # Checked before any cast, so a type-mapping regression (e.g. a column
    # silently coming back as the wrong Arrow type) can't hide behind the
    # cast below.
    assert result.schema.field("id").type == pa.int64()
    assert result.schema.field("region").type == pa.string()
    # A DuckDB round-trip can change Arrow nullability metadata, so this
    # compares by value and casts to the original schema rather than using
    # a strict Table.equals.
    assert result.cast(data.schema).equals(data)


_POLARS_BEFORE_1_37 = tuple(int(part) for part in pl.__version__.split(".")[:2]) < (1, 37)
"""`pa.schema(pl.Schema)` needs Polars' Arrow schema export, which works from Polars 1.37."""


@pytest.mark.skipif(
    _POLARS_BEFORE_1_37,
    reason="a pl.Schema has the Arrow C interface only from Polars 1.37 (1.34 to 1.36 are broken)",
)
def test_sdk_polars_round_trip(
    lakana_client: lakana.Client, client: TestClient, tmp_path: Path
) -> None:
    """A table created from a polars schema accepts a polars append and reads back equal."""
    lakana_client.environments.create(
        "dev", storage={"scheme": "file", "root": f"file://{tmp_path}", "options": {}}
    )
    frame = pl.DataFrame(
        {"id": [1, 2, 3], "region": ["eu", None, "us"], "blob": [b"a", b"b", None]}
    )
    lakana_client.tables.create("dev", "bronze.sales", schema=frame.schema)

    appended = lakana_client.tables.append("dev", "bronze.sales", frame)
    assert appended.rows == 3

    run = lakana_client.queries.submit(
        "SELECT id, region, blob FROM bronze.sales ORDER BY id", environment="dev"
    )
    _run_worker_once(client, run.id)
    result = pl.DataFrame(run.result())

    assert result.to_dict(as_series=False) == frame.to_dict(as_series=False)


def test_sdk_create_and_append_a_uuid_column(lakana_client: lakana.Client, tmp_path: Path) -> None:
    """A `pa.uuid()` column round-trips through schema conversion, creation, and append.

    Fix round 1, Important: `lakana._arrow.schema_to_columns` had no mapping
    for `pa.uuid()`, even though the server's create-table route, append
    allowlist, and `to_iceberg_schema` all already support a `uuid` column
    — the SDK would have raised a local `ValidationError` for a type the
    server accepts.
    """
    lakana_client.environments.create(
        "dev", storage={"scheme": "file", "root": f"file://{tmp_path}", "options": {}}
    )
    schema = pa.schema([pa.field("id", pa.uuid(), nullable=True)])
    lakana_client.tables.create("dev", "bronze.identifiers", schema=schema)

    data = pa.table({"id": pa.array([uuid.uuid4().bytes], pa.uuid())})
    appended = lakana_client.tables.append("dev", "bronze.identifiers", data)

    assert appended.rows == 1
    assert appended.files == 1
    assert appended.snapshot_id is not None


def test_sdk_denied_append_is_permission_denied(
    lakana_client: lakana.Client, client: TestClient, tmp_path: Path
) -> None:
    """A VIEWER-only principal's append is refused with `PermissionDenied`."""
    lakana_client.environments.create(
        "dev", storage={"scheme": "file", "root": f"file://{tmp_path}", "options": {}}
    )
    schema = pa.schema([pa.field("id", pa.int64(), nullable=False)])
    lakana_client.tables.create("dev", "bronze.sales", schema=schema)

    client.post("/api/v1/principals", headers=AUTH_HEADER, json={"name": "bob", "type": "USER"})
    client.post(
        "/api/v1/grants",
        headers=AUTH_HEADER,
        json={"principal": "bob", "role": "VIEWER", "kind": "ENVIRONMENT", "environment": "dev"},
    )
    issued = client.post("/api/v1/principals/bob/tokens", headers=AUTH_HEADER, json={"name": "bob"})
    bob_token = issued.json()["token"]

    with (
        lakana.Client(url="http://testserver", token=bob_token, http_client=client) as bob_client,
        pytest.raises(lakana.PermissionDenied),
    ):
        bob_client.tables.append("dev", "bronze.sales", pa.table({"id": [1]}, schema=schema))


def test_sdk_task_token_outside_its_scope_append_is_permission_denied(
    lakana_client: lakana.Client, client: TestClient, tmp_path: Path
) -> None:
    """A task token scoped to `gold.sales` cannot append to `bronze.sales`.

    Minted for the bootstrap admin itself, whose underlying grants would
    otherwise allow this — task-token scope narrowing applies regardless
    of what the principal could do via a PAT.
    """
    lakana_client.environments.create(
        "dev", storage={"scheme": "file", "root": f"file://{tmp_path}", "options": {}}
    )
    schema = pa.schema([pa.field("id", pa.int64(), nullable=False)])
    lakana_client.tables.create("dev", "bronze.sales", schema=schema)

    minted = lakana_client.task_tokens.mint("admin", "dev", ["gold.sales"], 300)

    with (
        lakana.Client(
            url="http://testserver", token=minted.token, http_client=client
        ) as task_client,
        pytest.raises(lakana.PermissionDenied),
    ):
        task_client.tables.append("dev", "bronze.sales", pa.table({"id": [1]}, schema=schema))


def test_sdk_append_over_the_byte_cap_is_payload_too_large(
    settings: Settings, tmp_path: Path
) -> None:
    """A body over a small, configured byte cap raises `PayloadTooLarge` through the SDK."""
    capped = settings.model_copy(update={"append": AppendConfig(max_bytes=1024)})
    with TestClient(create_app(capped)) as client:
        sdk_client = lakana.Client(
            url="http://testserver", token="lakana_pat_testkey_testsecret", http_client=client
        )
        sdk_client.environments.create(
            "dev", storage={"scheme": "file", "root": f"file://{tmp_path}", "options": {}}
        )
        schema = pa.schema(
            [pa.field("id", pa.int64(), nullable=False), pa.field("note", pa.string())]
        )
        sdk_client.tables.create("dev", "bronze.sales", schema=schema)
        big_data = pa.table({"id": list(range(2000)), "note": ["x" * 100] * 2000}, schema=schema)

        with pytest.raises(lakana.PayloadTooLarge):
            sdk_client.tables.append("dev", "bronze.sales", big_data)
        sdk_client.close()


def test_sdk_missing_table_is_not_found(lakana_client: lakana.Client, tmp_path: Path) -> None:
    """Fetching a table that was never created raises `NotFound`."""
    lakana_client.environments.create(
        "dev", storage={"scheme": "file", "root": f"file://{tmp_path}", "options": {}}
    )

    with pytest.raises(lakana.NotFound):
        lakana_client.tables.get("dev", "bronze.nope")


def test_sdk_shows_a_table_that_is_not_published(
    lakana_client: lakana.Client, client: TestClient, tmp_path: Path
) -> None:
    """`tables.get` and `tables.list` carry the state and why; a fresh table is published."""
    lakana_client.environments.create(
        "dev", storage={"scheme": "file", "root": f"file://{tmp_path}", "options": {}}
    )
    schema = pa.schema([pa.field("id", pa.int64(), nullable=False)])
    lakana_client.tables.create("dev", "bronze.sales", schema=schema)
    lakana_client.tables.create("dev", "bronze.fine", schema=schema)
    run_id = uuid.uuid4()
    with Session(client.app.state.engine) as session:
        session.execute(
            update(Table).where(Table.name == "sales").values(unpublished_run_id=run_id)
        )
        session.commit()

    stopped = lakana_client.tables.get("dev", "bronze.sales")
    listed = {
        t.name: (t.state, t.state_message) for t in lakana_client.tables.list("dev", "bronze")
    }

    expected = (
        "`bronze.sales` has not been published: its first build did not finish, "
        f"or failed a check (run {run_id}). It is published by the next run that builds it "
        "and passes its checks."
    )
    assert (stopped.state, stopped.state_message) == ("not_published", expected)
    assert listed == {"sales": ("not_published", expected), "fine": ("published", None)}


def test_sdk_failed_query_raises_query_failed(
    lakana_client: lakana.Client, client: TestClient, tmp_path: Path
) -> None:
    """A query against a missing column ends `FAILED`, and `wait` raises `QueryFailed`."""
    lakana_client.environments.create(
        "dev", storage={"scheme": "file", "root": f"file://{tmp_path}", "options": {}}
    )
    schema = pa.schema([pa.field("id", pa.int64(), nullable=False)])
    lakana_client.tables.create("dev", "bronze.sales", schema=schema)

    run = lakana_client.queries.submit("SELECT no_such_column FROM bronze.sales", environment="dev")
    _run_worker_once(client, run.id)

    with pytest.raises(lakana.QueryFailed):
        run.wait()


def test_sdk_sees_why_a_queued_query_waits(lakana_client: lakana.Client, tmp_path: Path) -> None:
    """With no worker running, the status says so, and `wait(on_update=)` hands it over."""
    lakana_client.environments.create(
        "dev", storage={"scheme": "file", "root": f"file://{tmp_path}", "options": {}}
    )
    run = lakana_client.queries.submit("SELECT 1", environment="dev")
    waiting = run.status().waiting_for
    assert waiting is not None
    assert (waiting.code, waiting.workers, waiting.ahead) == ("no_worker", 0, None)
    assert "Ask the operator to start a worker" in waiting.message
    seen: list[str | None] = []
    with pytest.raises(TimeoutError):
        run.wait(
            timeout=0.2,
            poll_interval=0.05,
            on_update=lambda status: seen.append(
                None if status.waiting_for is None else status.waiting_for.code
            ),
        )
    assert seen and set(seen) == {"no_worker"}


def _competing_append_ipc_bytes(table: pa.Table) -> bytes:
    """Serialize a table to an Arrow IPC stream, for a raw (non-SDK) append request.

    Parameters
    ----------
    table
        The table to serialize.

    Returns
    -------
    bytes
        The IPC stream bytes.
    """
    sink = io.BytesIO()
    with pyarrow.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue()


@pytest.mark.slow
def test_sdk_two_concurrent_appends_one_conflicts_and_the_sdk_retry_succeeds(
    lakana_client: lakana.Client,
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A competing append that commits mid-flight causes a 409 the SDK retries once.

    `service_module.build_transaction` is patched to also perform a second,
    independent append -- through the raw `client`, not the SDK -- right
    after the SDK's own append has staged its transaction but before it
    reaches `commit_table`'s compare-and-swap. That race is real: the SDK's
    append loses it, gets a genuine 409 from the server, and its built-in
    single retry succeeds against the table's now-current state. Both
    appends' rows must land, and each commit gets its own `TABLE_APPENDED`
    event -- the SDK's first, failed attempt is rolled back and audits
    nothing.
    """
    lakana_client.environments.create(
        "dev", storage={"scheme": "file", "root": f"file://{tmp_path}", "options": {}}
    )
    schema = pa.schema([pa.field("id", pa.int64(), nullable=False)])
    lakana_client.tables.create("dev", "bronze.sales", schema=schema)

    real_build_transaction = service_module.build_transaction
    already_raced = False

    def racing_build_transaction(
        metadata: TableMetadata, metadata_location: str, file_io: FileIO, data: pa.Table
    ) -> tuple[object, object]:
        result = real_build_transaction(metadata, metadata_location, file_io, data)
        nonlocal already_raced
        if not already_raced:
            already_raced = True
            competing = client.post(
                "/api/v1/environments/dev/namespaces/bronze/tables/sales/data",
                headers={
                    **AUTH_HEADER,
                    "Content-Type": "application/vnd.apache.arrow.stream",
                },
                content=_competing_append_ipc_bytes(pa.table({"id": [999]}, schema=schema)),
            )
            assert competing.status_code == 200, competing.text
        return result

    monkeypatch.setattr(service_module, "build_transaction", racing_build_transaction)

    data = pa.table({"id": [1, 2]}, schema=schema)
    appended = lakana_client.tables.append("dev", "bronze.sales", data)
    assert appended.rows == 2

    run = lakana_client.queries.submit("SELECT id FROM bronze.sales ORDER BY id", environment="dev")
    _run_worker_once(client, run.id)
    result = run.result()
    assert result.column("id").to_pylist() == [1, 2, 999]

    page = lakana_client.audit_events.list(event_type="TABLE_APPENDED", limit=1)
    assert len(page.items) == 1
    assert page.next_cursor is not None
    events = list(lakana_client.audit_events.iter(event_type="TABLE_APPENDED", page_size=1))
    assert len(events) == 2


# --- no drift check across the three hand-kept -----
# allowlists (server's _ALLOWED_SCALAR_ARROW_TYPES, core's json_schema plus
# _types_compatible, and the SDK's _arrow._SIMPLE_ARROW_TO_JSON). This
# end-to-end sweep drives every type the SDK can map through the real
# create-table-from-schema -> append round trip. A UTC-alias timestamp is
# included, covering minor 1 (only UTC aliases become timestamptz) too.


@pytest.mark.parametrize(
    ("type_id", "arrow_type", "value"),
    [
        ("int32", pa.int32(), 1),
        ("int64", pa.int64(), 1),
        ("float32", pa.float32(), 1.5),
        ("float64", pa.float64(), 1.5),
        ("boolean", pa.bool_(), True),
        ("string", pa.string(), "x"),
        ("large_string", pa.large_string(), "x"),
        ("binary", pa.binary(), b"x"),
        ("large_binary", pa.large_binary(), b"x"),
        ("date32", pa.date32(), date(2024, 1, 1)),
        ("uuid", pa.uuid(), uuid.UUID("12345678-1234-5678-1234-567812345678").bytes),
        ("decimal", pa.decimal128(10, 2), Decimal("1.23")),
        ("time64_us", pa.time64("us"), time(1, 2, 3)),
        ("timestamp_naive", pa.timestamp("us"), datetime(2024, 1, 1)),
        ("timestamp_utc", pa.timestamp("us", tz="UTC"), datetime(2024, 1, 1, tzinfo=UTC)),
        ("timestamp_utc_alias", pa.timestamp("us", tz="+00:00"), datetime(2024, 1, 1, tzinfo=UTC)),
    ],
)
def test_sdk_create_and_append_every_sdk_mapped_type(
    lakana_client: lakana.Client,
    tmp_path: Path,
    type_id: str,
    arrow_type: pa.DataType,
    value: object,
) -> None:
    """Every type the SDK can map round-trips through create + append."""
    lakana_client.environments.create(
        "dev", storage={"scheme": "file", "root": f"file://{tmp_path}", "options": {}}
    )
    schema = pa.schema([pa.field("v", arrow_type, nullable=True)])
    qualified_name = f"bronze.t_{type_id}"
    lakana_client.tables.create("dev", qualified_name, schema=schema)
    data = pa.table({"v": pa.array([value], type=arrow_type)})

    appended = lakana_client.tables.append("dev", qualified_name, data)

    assert appended.rows == 1
