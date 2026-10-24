"""Merge through a real server on Postgres and SeaweedFS (S3), with the SDK.

The first merge creates the table, the next updates and inserts in one snapshot. A second test
prints (does not assert) the server process's peak resident memory and the time of a merge of
100,000 rows into a 1,000,000-row table, and of a merge on a key of two columns; the numbers go
on issue #23.
"""

from __future__ import annotations

import resource
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import pyarrow as pa
import pytest
from pyiceberg.catalog.rest import RestCatalog

import lakana
from lakana_server.app import create_app
from tests.integration_helpers import unique_name
from tests.python_asset_helpers import TOKEN, Services, running_services, serving

pytestmark = pytest.mark.integration

T0 = datetime(2026, 10, 1, 8, 0)
_SCHEMA = pa.schema(
    [
        pa.field("id", pa.int64(), nullable=False),
        pa.field("name", pa.string()),
        pa.field("updated_at", pa.timestamp("us")),
    ]
)


@dataclass(frozen=True)
class _Stack:
    """The services, a running server and an admin client.

    Parameters
    ----------
    services
        Postgres and SeaweedFS.
    url
        The server's base URL.
    admin
        An SDK client with the bootstrap token.
    """

    services: Services
    url: str
    admin: lakana.Client

    def catalog(self, environment: str) -> RestCatalog:
        """Open a pyiceberg REST catalog on an environment, with the store's own credentials.

        Parameters
        ----------
        environment
            The environment.

        Returns
        -------
        RestCatalog
            The catalog.
        """
        options: dict[str, Any] = self.services.storage["options"]  # ty: ignore[invalid-assignment]
        return RestCatalog(name="lakana", uri=self.url, prefix=environment, token=TOKEN, **options)


@pytest.fixture(scope="module")
def stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[_Stack]:
    """Start Postgres, SeaweedFS and a real HTTP server.

    Parameters
    ----------
    tmp_path_factory
        For the repo root.

    Yields
    ------
    _Stack
        The stack.
    """
    with (
        running_services(tmp_path_factory) as services,
        serving(create_app(services.settings)) as url,
        lakana.Client(url, TOKEN, timeout=120.0) as admin,
    ):
        yield _Stack(services=services, url=url, admin=admin)


def _rows(ids: range | list[int], name: str, *, at: datetime = T0) -> pa.Table:
    """Build rows of `bronze.customers`.

    Parameters
    ----------
    ids
        The ids.
    name
        The name of every row.
    at
        The `updated_at` of every row.

    Returns
    -------
    pa.Table
        The rows.
    """
    count = len(ids)
    return pa.table(
        {
            "id": pa.array(ids, pa.int64()),
            "name": pa.array([name] * count, pa.string()),
            "updated_at": pa.array([at] * count, pa.timestamp("us")),
        },
        schema=_SCHEMA,
    )


def _environment(stack: _Stack) -> str:
    """Create a fresh environment (it comes with a `bronze` namespace).

    Parameters
    ----------
    stack
        The stack.

    Returns
    -------
    str
        Its name.
    """
    environment = unique_name()
    stack.services.environment(stack.admin, environment)
    return environment


def test_the_first_merge_creates_the_table_and_the_next_commits_one_snapshot(
    stack: _Stack,
) -> None:
    """Create by merge on S3, then update id 2 and insert id 3 in exactly one snapshot."""
    environment = _environment(stack)

    created = stack.admin.tables.merge(
        environment,
        "bronze.customers",
        _rows([1, 2], "a"),
        key="id",
        expected_snapshot_id="none",
    )
    merged = stack.admin.tables.merge(
        environment, "bronze.customers", _rows([2, 3], "b", at=T0 + timedelta(days=1)), key="id"
    )

    assert (created.rows_inserted, created.rows_updated) == (2, 0)
    assert (merged.rows_inserted, merged.rows_updated) == (1, 1)
    table = stack.catalog(environment).load_table("bronze.customers")
    assert [s.snapshot_id for s in table.metadata.snapshots] == [
        created.snapshot_id,
        merged.snapshot_id,
    ]
    assert [entry.snapshot_id for entry in table.metadata.snapshot_log] == [
        created.snapshot_id,
        merged.snapshot_id,
    ]
    rows = table.scan().to_arrow().sort_by("id")
    assert rows.column("id").to_pylist() == [1, 2, 3]
    assert rows.column("name").to_pylist() == ["a", "b", "b"]


def _peak_mib() -> float:
    """Read this process's peak resident memory.

    Returns
    -------
    float
        MiB (`ru_maxrss` is KiB on Linux).
    """
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def test_measure_a_large_merge(stack: _Stack) -> None:
    """Print peak memory and time; the server runs in this process, so its peak is ours."""
    environment = _environment(stack)
    table = "bronze.customers"
    for start in range(0, 1_000_000, 250_000):
        if start == 0:
            stack.admin.tables.replace(
                environment, table, _rows(range(250_000), "a"), expected_snapshot_id="none"
            )
        else:
            stack.admin.tables.append(environment, table, _rows(range(start, start + 250_000), "a"))
    batch = _rows(range(950_000, 1_050_000), "b")

    before = _peak_mib()
    started = time.monotonic()
    result = stack.admin.tables.merge(environment, table, batch, key="id")
    seconds = time.monotonic() - started
    after = _peak_mib()
    print(
        f"\nmerge of 100,000 rows into 1,000,000 on one key column: {seconds:.1f} s, "
        f"peak RSS {before:.0f} MiB before, {after:.0f} MiB after "
        f"(+{after - before:.0f} MiB); inserted {result.rows_inserted}, "
        f"updated {result.rows_updated}"
    )
    assert (result.rows_inserted, result.rows_updated) == (50_000, 50_000)

    composite = _rows(range(990_000, 995_000), "b", at=T0 + timedelta(days=1))
    before = _peak_mib()
    started = time.monotonic()
    result = stack.admin.tables.merge(environment, table, composite, key=["id", "name"])
    seconds = time.monotonic() - started
    after = _peak_mib()
    print(
        f"merge of 5,000 rows into 1,050,000 on a key of two columns: {seconds:.1f} s, "
        f"peak RSS {before:.0f} MiB before, {after:.0f} MiB after "
        f"(+{after - before:.0f} MiB); inserted {result.rows_inserted}, "
        f"updated {result.rows_updated}"
    )
    assert (result.rows_inserted, result.rows_updated) == (0, 5_000)
