"""The Arrow replace route and Arrow `sources`."""

from __future__ import annotations

import io
import uuid

import pyarrow as pa
import pyarrow.ipc
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.models import AuditEvent, AuditEventType, LineageEdge
from lakana_worker.pipeline import run_one
from tests.conftest import AUTH_HEADER
from tests.server_test_helpers import create_environment

_HEADERS = {**AUTH_HEADER, "Content-Type": "application/vnd.apache.arrow.stream"}
_TABLES = "/api/v1/environments/dev/namespaces/gold/tables"


def _ipc(table: pa.Table) -> bytes:
    """Serialize a table as an uncompressed Arrow IPC stream.

    Parameters
    ----------
    table
        The rows.

    Returns
    -------
    bytes
        The stream.
    """
    sink = io.BytesIO()
    with pyarrow.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue()


def _ids(ids: list[int]) -> pa.Table:
    """Build a non-nullable `id` column, matching the table the tests create.

    Parameters
    ----------
    ids
        The ids.

    Returns
    -------
    pa.Table
        The rows.
    """
    return pa.table(
        {"id": pa.array(ids, pa.int64())},
        schema=pa.schema([pa.field("id", pa.int64(), nullable=False)]),
    )


def _setup(client: TestClient) -> None:
    """Create `dev` with `gold.t` and `gold.src`, each holding one row.

    Parameters
    ----------
    client
        The test client.
    """
    create_environment(client)
    for name in ("t", "src"):
        created = client.post(
            _TABLES,
            headers=AUTH_HEADER,
            json={"name": name, "schema": [{"name": "id", "type": "long", "nullable": False}]},
        )
        assert created.status_code == 201, created.text
        appended = client.post(f"{_TABLES}/{name}/data", headers=_HEADERS, content=_ipc(_ids([1])))
        assert appended.status_code == 200, appended.text


def _ids_in(client: TestClient, table: str) -> list[int]:
    """Query a table's ids through the queries API and an in-process worker.

    Parameters
    ----------
    client
        The test client.
    table
        The dotted table.

    Returns
    -------
    list of int
        The ids, sorted.
    """
    submitted = client.post(
        "/api/v1/environments/dev/queries",
        headers=AUTH_HEADER,
        json={"sql": f"SELECT id FROM {table} ORDER BY id"},
    )
    query_id = submitted.json()["query_id"]
    with Session(client.app.state.engine) as session:
        run_one(session, uuid.UUID(query_id), GrantAuthorization(session))
    result = client.get(f"/api/v1/environments/dev/queries/{query_id}/result", headers=AUTH_HEADER)
    with pyarrow.ipc.open_file(pa.BufferReader(result.content)) as reader:
        return reader.read_all().column("id").to_pylist()


def test_put_replaces_every_row(client: TestClient) -> None:
    """`PUT …/data` is a full refresh from Arrow."""
    _setup(client)

    response = client.put(f"{_TABLES}/t/data", headers=_HEADERS, content=_ipc(_ids([7, 8])))

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["rows"], body["files"], body["schema_diff"]) == (2, 1, None)
    assert body["snapshot_id"] is not None
    assert _ids_in(client, "gold.t") == [7, 8]


def test_put_with_evolve_schema_adopts_the_uploads_schema(client: TestClient) -> None:
    """`?evolve_schema=true` lets the table take the upload's schema."""
    _setup(client)
    wider = _ids([5]).append_column("n", pa.array([3], pa.int64()))

    response = client.put(
        f"{_TABLES}/t/data", headers=_HEADERS, params={"evolve_schema": "true"}, content=_ipc(wider)
    )

    assert response.status_code == 200, response.text
    assert response.json()["schema_diff"]["added"] == [{"name": "n", "type": "long"}]


def test_put_refuses_an_incompatible_upload_with_the_diff(client: TestClient) -> None:
    """Without `evolve_schema`, a renamed column is a 422 naming both names."""
    _setup(client)
    renamed = pa.table({"key": pa.array([5], pa.int64())})

    response = client.put(f"{_TABLES}/t/data", headers=_HEADERS, content=_ipc(renamed))

    assert response.status_code == 422
    assert "renamed-looking id -> key" in response.text
    assert _ids_in(client, "gold.t") == [1]


def test_post_with_sources_records_lineage(client: TestClient) -> None:
    """`?source=` on an append records one edge per source."""
    _setup(client)

    response = client.post(
        f"{_TABLES}/t/data",
        headers=_HEADERS,
        params={"source": ["gold.src"]},
        content=_ipc(_ids([2])),
    )

    assert response.status_code == 200, response.text
    with Session(client.app.state.engine) as session:
        edge = session.execute(select(LineageEdge)).scalar_one()
    assert (edge.source_name, edge.target_name) == ("gold.src", "gold.t")


def test_a_missing_source_is_a_404_and_writes_nothing(client: TestClient) -> None:
    """Every declared source must exist before a file is written."""
    _setup(client)

    response = client.put(
        f"{_TABLES}/t/data",
        headers=_HEADERS,
        params={"source": ["gold.nope"]},
        content=_ipc(_ids([9])),
    )

    assert response.status_code == 404
    assert _ids_in(client, "gold.t") == [1]


def test_put_refuses_a_non_arrow_body(client: TestClient) -> None:
    """The replace route shares the append route's body checks."""
    _setup(client)

    response = client.put(
        f"{_TABLES}/t/data",
        headers={**AUTH_HEADER, "Content-Type": "application/json"},
        content=b"[]",
    )

    assert response.status_code == 422


def _snapshot(client: TestClient, table: str) -> int | None:
    """Read a table's current snapshot id through `GET …/tables/{name}`.

    Parameters
    ----------
    client
        The test client.
    table
        The table's name in `gold`.

    Returns
    -------
    int or None
        The id, or `None` without a snapshot.
    """
    response = client.get(f"{_TABLES}/{table}", headers=AUTH_HEADER)
    assert response.status_code == 200, response.text
    return response.json()["snapshot_id"]


def test_get_table_reports_the_current_snapshot(client: TestClient) -> None:
    _setup(client)
    assert isinstance(_snapshot(client, "t"), int)
    created = client.post(
        _TABLES,
        headers=AUTH_HEADER,
        json={"name": "empty", "schema": [{"name": "id", "type": "long", "nullable": False}]},
    )
    assert created.status_code == 201, created.text
    assert _snapshot(client, "empty") is None


def test_put_with_the_current_snapshot_commits_and_a_stale_one_conflicts(
    client: TestClient,
) -> None:
    _setup(client)
    first = _snapshot(client, "t")
    ok = client.put(
        f"{_TABLES}/t/data?expected_snapshot_id={first}", headers=_HEADERS, content=_ipc(_ids([2]))
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["snapshot_id"] == _snapshot(client, "t")
    stale = client.put(
        f"{_TABLES}/t/data?expected_snapshot_id={first}", headers=_HEADERS, content=_ipc(_ids([3]))
    )
    assert stale.status_code == 409, stale.text
    assert "gold.t changed during the write; retry." in stale.text
    assert _ids_in(client, "gold.t") == [2]


def test_post_with_the_current_snapshot_commits_and_a_stale_one_conflicts(
    client: TestClient,
) -> None:
    _setup(client)
    first = _snapshot(client, "t")
    ok = client.post(
        f"{_TABLES}/t/data?expected_snapshot_id={first}", headers=_HEADERS, content=_ipc(_ids([2]))
    )
    assert ok.status_code == 200, ok.text
    stale = client.post(
        f"{_TABLES}/t/data?expected_snapshot_id={first}", headers=_HEADERS, content=_ipc(_ids([3]))
    )
    assert stale.status_code == 409, stale.text
    assert _ids_in(client, "gold.t") == [1, 2]


def test_a_stale_snapshot_conflicts_even_for_an_empty_append(client: TestClient) -> None:
    _setup(client)
    stale = client.post(
        f"{_TABLES}/t/data?expected_snapshot_id=1", headers=_HEADERS, content=_ipc(_ids([]))
    )
    assert stale.status_code == 409, stale.text


def test_none_means_no_snapshot_yet(client: TestClient) -> None:
    _setup(client)
    for method in (client.post, client.put):
        refused = method(
            f"{_TABLES}/t/data?expected_snapshot_id=none", headers=_HEADERS, content=_ipc(_ids([9]))
        )
        assert refused.status_code == 409, refused.text
    client.post(
        _TABLES,
        headers=AUTH_HEADER,
        json={"name": "empty", "schema": [{"name": "id", "type": "long", "nullable": False}]},
    )
    first = client.post(
        f"{_TABLES}/empty/data?expected_snapshot_id=none", headers=_HEADERS, content=_ipc(_ids([1]))
    )
    assert first.status_code == 200, first.text
    second = client.put(
        f"{_TABLES}/empty/data?expected_snapshot_id=none", headers=_HEADERS, content=_ipc(_ids([2]))
    )
    assert second.status_code == 409, second.text


def test_put_none_creates_a_missing_table_from_the_upload(client: TestClient) -> None:
    _setup(client)
    created = client.put(
        f"{_TABLES}/fresh/data?expected_snapshot_id=none&source=gold.src",
        headers=_HEADERS,
        content=_ipc(_ids([4, 5])),
    )
    assert created.status_code == 200, created.text
    assert created.json()["rows"] == 2
    assert created.json()["snapshot_id"] == _snapshot(client, "fresh")
    assert _ids_in(client, "gold.fresh") == [4, 5]
    with Session(client.app.state.engine) as session:
        assert session.execute(select(LineageEdge)).scalars().all()
    again = client.put(
        f"{_TABLES}/fresh/data?expected_snapshot_id=none", headers=_HEADERS, content=_ipc(_ids([6]))
    )
    assert again.status_code == 409, again.text


def test_put_none_creates_an_empty_table_then_none_still_holds(client: TestClient) -> None:
    """Zero rows with columns is valid: the table exists without a snapshot."""
    _setup(client)
    created = client.put(
        f"{_TABLES}/fresh/data?expected_snapshot_id=none", headers=_HEADERS, content=_ipc(_ids([]))
    )
    assert created.status_code == 200, created.text
    assert created.json()["snapshot_id"] is None
    assert _snapshot(client, "fresh") is None
    filled = client.put(
        f"{_TABLES}/fresh/data?expected_snapshot_id=none", headers=_HEADERS, content=_ipc(_ids([1]))
    )
    assert filled.status_code == 200, filled.text
    assert _ids_in(client, "gold.fresh") == [1]


def test_missing_tables_without_none(client: TestClient) -> None:
    _setup(client)
    plain = client.put(f"{_TABLES}/absent/data", headers=_HEADERS, content=_ipc(_ids([1])))
    assert plain.status_code == 404, plain.text
    pinned = client.put(
        f"{_TABLES}/absent/data?expected_snapshot_id=7", headers=_HEADERS, content=_ipc(_ids([1]))
    )
    assert pinned.status_code == 409, pinned.text
    appended = client.post(
        f"{_TABLES}/absent/data?expected_snapshot_id=none",
        headers=_HEADERS,
        content=_ipc(_ids([1])),
    )
    assert appended.status_code == 404, appended.text
    missing_namespace = client.put(
        "/api/v1/environments/dev/namespaces/nowhere/tables/x/data?expected_snapshot_id=none",
        headers=_HEADERS,
        content=_ipc(_ids([1])),
    )
    assert missing_namespace.status_code == 404, missing_namespace.text


@pytest.mark.parametrize("value", ["", "latest", "-1", "1.5", "None", "9" * 30, "١٢"])
def test_a_malformed_expected_snapshot_is_422(client: TestClient, value: str) -> None:
    _setup(client)
    for method in (client.post, client.put):
        response = method(
            f"{_TABLES}/t/data",
            params={"expected_snapshot_id": value},
            headers=_HEADERS,
            content=_ipc(_ids([1])),
        )
        assert response.status_code == 422, response.text
        assert "expected_snapshot_id must be a snapshot id or 'none'" in response.text


def _restricted(client: TestClient, name: str, actions: list[str]) -> dict[str, str]:
    """Create a user holding only `actions` on `dev` and return its Arrow request headers.

    Parameters
    ----------
    client
        The test client.
    name
        The principal's name.
    actions
        Actions granted at environment scope.

    Returns
    -------
    dict
        `Authorization` and the Arrow `Content-Type`.
    """
    client.post("/api/v1/principals", headers=AUTH_HEADER, json={"name": name, "type": "USER"})
    for action in actions:
        granted = client.post(
            "/api/v1/grants",
            headers=AUTH_HEADER,
            json={"principal": name, "action": action, "kind": "ENVIRONMENT", "environment": "dev"},
        )
        assert granted.status_code < 300, granted.text
    issued = client.post(
        f"/api/v1/principals/{name}/tokens", headers=AUTH_HEADER, json={"name": name}
    )
    return {
        "Authorization": f"Bearer {issued.json()['token']}",
        "Content-Type": "application/vnd.apache.arrow.stream",
    }


def test_put_none_on_a_missing_table_needs_create_table(client: TestClient) -> None:
    _setup(client)
    url = f"{_TABLES}/fresh/data?expected_snapshot_id=none"
    writer = _restricted(client, "writer", ["WRITE_TABLE"])
    refused = client.put(url, headers=writer, content=_ipc(_ids([1])))
    assert refused.status_code == 403, refused.text
    assert "CREATE_TABLE" in refused.text
    assert client.get(f"{_TABLES}/fresh", headers=AUTH_HEADER).status_code == 404
    with Session(client.app.state.engine) as session:
        denied = (
            session.execute(
                select(AuditEvent).where(
                    AuditEvent.event_type == AuditEventType.AUTHORIZATION_DENIED
                )
            )
            .scalars()
            .all()
        )
    assert any("CREATE_TABLE" in str(event.detail) for event in denied)
    creator = _restricted(client, "creator", ["WRITE_TABLE", "CREATE_TABLE"])
    created = client.put(url, headers=creator, content=_ipc(_ids([1])))
    assert created.status_code == 200, created.text


def test_a_write_without_write_table_does_not_disclose_existence(client: TestClient) -> None:
    _setup(client)
    reader = _restricted(client, "reader", ["READ_TABLE"])
    for method in (client.post, client.put):
        for query in ("?expected_snapshot_id=none", "?expected_snapshot_id=7", ""):
            for name in ("t", "missing"):
                refused = method(
                    f"{_TABLES}/{name}/data{query}", headers=reader, content=_ipc(_ids([1]))
                )
                assert refused.status_code == 403, refused.text
                assert "WRITE_TABLE" in refused.text


def test_a_repeated_expected_snapshot_is_422_on_both_routes(client: TestClient) -> None:
    _setup(client)
    for method in (client.post, client.put):
        response = method(
            f"{_TABLES}/absent/data?expected_snapshot_id=x&expected_snapshot_id=none",
            headers=_HEADERS,
            content=_ipc(_ids([1])),
        )
        assert response.status_code == 422, response.text
        assert "at most once" in response.text
    assert client.get(f"{_TABLES}/absent", headers=AUTH_HEADER).status_code == 404


_SOURCE_VERSION_422 = "source_version must be '<version>:<namespace.name>'"


@pytest.mark.parametrize("method", ["POST", "PUT"])
@pytest.mark.parametrize(
    "value",
    [
        "models.churn",
        "0:models.churn",
        "01:models.churn",
        "-1:models.churn",
        "\uff11:models.churn",
        "2147483648:models.churn",
        f"{'9' * 5000}:models.churn",
        "1:nonamespace",
    ],
)
def test_a_malformed_source_version_is_422_before_the_body_is_read(
    client: TestClient, method: str, value: str
) -> None:
    """Both Arrow routes answer 422, not 500, and never read the (here invalid) body."""
    _setup(client)
    response = client.request(
        method,
        f"{_TABLES}/t/data",
        params={"source_version": value},
        content=b"not arrow",
        headers=_HEADERS,
    )
    assert response.status_code == 422
    assert _SOURCE_VERSION_422 in response.json()["detail"]


def test_a_source_version_for_a_table_is_422_and_writes_nothing(client: TestClient) -> None:
    """A version may be named only for an artifact among the sources."""
    _setup(client)
    response = client.put(
        f"{_TABLES}/t/data",
        params={"source": ["gold.src"], "source_version": "1:gold.src"},
        content=_ipc(_ids([9])),
        headers=_HEADERS,
    )
    assert response.status_code == 422
    assert "is not an artifact among the sources" in response.json()["detail"]
    assert _ids_in(client, "gold.t") == [1]


@pytest.mark.parametrize("name", ["gold.src\nx.y", f"{'a' * 5000}.b", "gold.src‮"])
def test_an_unprintable_or_oversized_source_version_name_is_not_echoed(
    client: TestClient, name: str
) -> None:
    """The 422 is short and carries no line break, whatever name was sent."""
    _setup(client)
    response = client.put(
        f"{_TABLES}/t/data",
        params={"source_version": f"1:{name}"},
        content=b"not arrow",
        headers=_HEADERS,
    )
    assert response.status_code == 422
    assert len(response.content) < 1024
    detail = response.json()["detail"]
    assert "\n" not in detail
    assert name not in detail
    assert detail.startswith(_SOURCE_VERSION_422)


_TOO_MANY_SOURCES_422 = "source may be given at most 64 times"


@pytest.mark.parametrize("method", ["POST", "PUT"])
def test_more_than_64_sources_is_422_before_the_body_is_read(
    client: TestClient, method: str
) -> None:
    """The count of `source` values is bounded as `source_version`'s is."""
    _setup(client)
    response = client.request(
        method,
        f"{_TABLES}/t/data",
        params={"source": ["gold.src"] * 65},
        content=b"not arrow",
        headers=_HEADERS,
    )
    assert response.status_code == 422
    assert _TOO_MANY_SOURCES_422 in response.json()["detail"]


def test_64_sources_are_accepted(client: TestClient) -> None:
    """The bound is inclusive: 64 values reach the write."""
    _setup(client)
    response = client.post(
        f"{_TABLES}/t/data",
        params={"source": ["gold.src"] * 64},
        content=_ipc(_ids([2])),
        headers=_HEADERS,
    )
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("method", ["POST", "PUT"])
@pytest.mark.parametrize("name", ["x" * 60_000, "gold." + "x" * 60_000, "gold.\n" + "x" * 5000])
def test_a_source_name_longer_than_any_table_name_is_422_and_small(
    client: TestClient, method: str, name: str
) -> None:
    """A 60 KB `source` is refused without being echoed, before the body is read."""
    # Only the target table exists, so a 404 would show the name was looked up first; no
    # source table and no rows: they are never reached, and each write is an Iceberg commit.
    create_environment(client)
    created = client.post(
        _TABLES,
        headers=AUTH_HEADER,
        json={"name": "t", "schema": [{"name": "id", "type": "long", "nullable": False}]},
    )
    assert created.status_code == 201, created.text
    response = client.request(
        method,
        f"{_TABLES}/t/data",
        params={"source": name},
        content=b"not arrow",
        headers=_HEADERS,
    )
    assert response.status_code == 422
    assert len(response.text) < 600
    assert "x" * 50 not in response.text


@pytest.mark.parametrize(
    "name", ["gold." + "y" * 1000, "z" * 1000, "gold.sub." + "y" * 1000, "gold.a b" + "y" * 1000]
)
def test_a_legal_length_source_that_does_not_exist_is_answered_with_a_bounded_name(
    client: TestClient, name: str
) -> None:
    """A name of a legal length, found nowhere, is echoed at most 200 characters long."""
    _setup(client)
    response = client.post(
        f"{_TABLES}/t/data",
        params={"source": name},
        content=_ipc(_ids([2])),
        headers=_HEADERS,
    )
    assert response.status_code in (404, 422)
    assert len(response.text) < 700
    assert "y" * 250 not in response.text and "z" * 250 not in response.text


def test_an_unknown_long_table_or_namespace_is_a_bounded_404(client: TestClient) -> None:
    """The lookups that name what they could not find cut a long name short."""
    _setup(client)
    long_table = client.get(
        "/api/v1/environments/dev/namespaces/gold/tables/" + "t" * 5000, headers=AUTH_HEADER
    )
    long_namespace = client.get(
        "/api/v1/environments/dev/namespaces/gold." + "n" * 5000 + "/tables", headers=AUTH_HEADER
    )
    assert long_table.status_code == 404
    assert long_namespace.status_code == 404
    assert len(long_table.text) < 700
    assert len(long_namespace.text) < 700
