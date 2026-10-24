"""Property-based tests: no query shape leaks a masked value or a filtered row.

Runs the real `rewrite_query` output against a real DuckDB instance (via
`lakana_worker.executor.execute_query`) for a small, deliberately adversarial
set of query shapes — a self-join, a CTE built on another CTE, a UNION, a
subquery wrap — each referencing the same masked/filtered table, the exact
class of shape `docs/agent/testing.md` names as easy to miss with
hand-written examples alone.
"""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from worker_test_helpers import local_prefixes

from lakana_core.query_rewrite import TableSource, rewrite_query
from lakana_worker.executor import execute_query

_SHAPES = (
    "select_star",
    "select_star_from_cte",
    "cte_of_cte",
    "self_join",
    "union_all",
    "subquery_wrap",
)


def _build_query(shape: str) -> str:
    """Build a query of the given shape, all referencing `gold.customers`.

    Parameters
    ----------
    shape
        One of `_SHAPES`.

    Returns
    -------
    str
        A syntactically valid SQL query.

    Raises
    ------
    AssertionError
        If `shape` is not one of `_SHAPES` — a programming error in this
        test module, never a real input.
    """
    if shape == "select_star":
        return "SELECT * FROM gold.customers"
    if shape == "select_star_from_cte":
        return "WITH c AS (SELECT * FROM gold.customers) SELECT * FROM c"
    if shape == "cte_of_cte":
        return (
            "WITH c1 AS (SELECT * FROM gold.customers), c2 AS (SELECT * FROM c1) SELECT * FROM c2"
        )
    if shape == "self_join":
        return "SELECT a.* FROM gold.customers a JOIN gold.customers b ON a.id = b.id"
    if shape == "union_all":
        return (
            "SELECT * FROM gold.customers WHERE region = 'EU' "
            "UNION ALL "
            "SELECT * FROM gold.customers WHERE region = 'US'"
        )
    if shape == "subquery_wrap":
        return "SELECT * FROM (SELECT * FROM gold.customers) AS wrapped"
    raise AssertionError(f"unhandled shape: {shape}")


@pytest.fixture
def customers_parquet(tmp_path: Path) -> Path:
    """Write a small parquet file with a real, sensitive ssn column.

    Parameters
    ----------
    tmp_path
        Pytest's per-test temporary directory.

    Returns
    -------
    Path
        The written file's path.
    """
    path = tmp_path / "customers.parquet"
    pq.write_table(
        pa.table(
            {
                "id": [1, 2, 3],
                "ssn": ["111-11-1111", "222-22-2222", "333-33-3333"],
                "region": ["EU", "US", "EU"],
            }
        ),
        path,
    )
    return path


@pytest.mark.slow
@given(shape=st.sampled_from(_SHAPES))
@settings(
    deadline=None,
    max_examples=len(_SHAPES),
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_a_masked_column_never_carries_its_real_value_through_any_query_shape(
    shape: str, customers_parquet: Path
) -> None:
    """No generated query shape leaks ssn's real value once it is masked."""
    source = TableSource(
        urls=[str(customers_parquet)],
        file_format="parquet",
        columns={"id": "BIGINT", "ssn": "VARCHAR", "region": "VARCHAR"},
        column_masks={"ssn": "NULL"},
    )
    rewritten = rewrite_query(_build_query(shape), {"gold.customers": source})
    result = execute_query(
        rewritten.sql, rewritten.parameters, read_prefixes=local_prefixes(rewritten.parameters)
    )

    assert result.num_rows == 3
    assert "ssn" in result.column_names
    assert all(value is None for value in result.column("ssn").to_pylist())

    real_ssns = {"111-11-1111", "222-22-2222", "333-33-3333"}
    all_values = {
        value
        for column_name in result.column_names
        for value in result.column(column_name).to_pylist()
    }
    assert not (all_values & real_ssns)


@given(shape=st.sampled_from(_SHAPES))
@settings(
    deadline=None,
    max_examples=len(_SHAPES),
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_a_filtered_row_never_appears_through_any_query_shape(
    shape: str, customers_parquet: Path
) -> None:
    """No generated query shape returns a row the filter excludes."""
    source = TableSource(
        urls=[str(customers_parquet)],
        file_format="parquet",
        columns={"id": "BIGINT", "ssn": "VARCHAR", "region": "VARCHAR"},
        row_filters=["region = 'EU'"],
    )
    rewritten = rewrite_query(_build_query(shape), {"gold.customers": source})
    result = execute_query(
        rewritten.sql, rewritten.parameters, read_prefixes=local_prefixes(rewritten.parameters)
    )

    assert result.num_rows == 2
    assert set(result.column("region").to_pylist()) <= {"EU"}
