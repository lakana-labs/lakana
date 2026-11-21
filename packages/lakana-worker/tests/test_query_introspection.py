"""Introspection functions must never hand a caller the rewriter's policy text.

`current_query()` returns the statement DuckDB is running: the rewritten one,
with every row-filter predicate and column-mask expression spliced into its
CTEs and the names of the bound URL parameters beside them. These tests drive
the real rewriter and the real executor, the level where that text came back
as an ordinary result value.
"""

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from worker_test_helpers import local_prefixes

from lakana_core.errors import ValidationError
from lakana_core.query_rewrite import TableSource, check_blacklist, rewrite_query
from lakana_worker.executor import execute_query

_CURRENT_QUERY = [
    pytest.param("SELECT current_query() AS q FROM bronze.customers", id="select"),
    pytest.param("SELECT current_query()", id="bare"),
    pytest.param("SELECT pg_catalog.current_query() AS q", id="pg-catalog-qualified"),
    pytest.param("SELECT system.main.current_query() AS q", id="catalog-qualified"),
    pytest.param('SELECT "current_query"() AS q FROM bronze.customers', id="quoted"),
    pytest.param("SELECT main.current_query() AS q FROM bronze.customers", id="schema-qualified"),
    pytest.param(
        "SELECT q, ssn FROM (SELECT current_query() AS q, ssn FROM bronze.customers) AS s",
        id="subquery",
    ),
    pytest.param(
        "WITH c AS (SELECT current_query() AS q) SELECT c.q, b.ssn FROM c, bronze.customers AS b",
        id="cte",
    ),
    pytest.param("SELECT upper(current_query()) AS q FROM bronze.customers", id="function-arg"),
    pytest.param("SELECT length(current_query()) AS n FROM bronze.customers", id="length"),
    pytest.param(
        "SELECT ssn FROM bronze.customers WHERE current_query() LIKE '%id > 1%'", id="predicate"
    ),
]

_INTROSPECTION = [
    "current_setting('temp_directory')",
    "(SELECT count(*) FROM duckdb_settings())",
    "(SELECT count(*) FROM duckdb_secrets())",
    "(SELECT count(*) FROM duckdb_extensions())",
    "(SELECT count(*) FROM duckdb_databases())",
    "(SELECT count(*) FROM duckdb_memory())",
    "(SELECT count(*) FROM duckdb_live_query_metrics())",
    "(SELECT count(*) FROM duckdb_temporary_files())",
    "(SELECT count(*) FROM duckdb_logs())",
    "(SELECT count(*) FROM duckdb_views())",
    "(SELECT count(*) FROM duckdb_columns())",
    "(SELECT count(*) FROM pragma_database_size())",
    "(SELECT count(*) FROM pragma_user_agent())",
    "(SELECT count(*) FROM sql_auto_complete('SELECT'))",
]


_BIND_TIME = [
    "stats(id)",
    "stats(ssn)",
    "pg_get_viewdef(1)",
    "pg_get_constraintdef(1)",
    "pg_get_expr('x', 1)",
    "get_block_size('memory')",
    "(SELECT count(*) FROM duckdb_profiling_settings())",
]


@pytest.fixture
def customers(tmp_path: Path) -> TableSource:
    """Build a masked, row-filtered `bronze.customers` backed by one local file.

    Parameters
    ----------
    tmp_path
        pytest's per-test temporary directory.

    Returns
    -------
    TableSource
        The table's resolved source, with a row filter and an `ssn` mask.
    """
    path = tmp_path / "c.parquet"
    pq.write_table(pa.table({"id": [1, 2, 3], "ssn": ["111", "222", "333"]}), path)
    return TableSource(
        urls=[str(path)],
        file_format="parquet",
        columns={"id": "BIGINT", "ssn": "VARCHAR"},
        row_filters=("id > 1",),
        column_masks={"ssn": "'***'"},
    )


def _assert_no_policy_text(message: str, source: TableSource) -> None:
    """Assert an error echoes neither policy text nor the source's path.

    Parameters
    ----------
    message
        The error message.
    source
        The source whose filter, mask and URL must not appear.
    """
    for secret in ("id > 1", "'***'", "c.parquet", source.urls[0]):
        assert secret not in message


@pytest.mark.parametrize("sql", _CURRENT_QUERY)
def test_current_query_is_refused_before_it_can_return_the_rewritten_sql(
    sql: str, customers: TableSource
) -> None:
    """Every placement of `current_query()` fails validation, naming no policy text.

    Parameters
    ----------
    sql
        A query calling `current_query()` somewhere in its tree.
    customers
        The masked, row-filtered source fixture.
    """
    with pytest.raises(ValidationError, match="current_query may not be called") as caught:
        check_blacklist(sql)
    _assert_no_policy_text(str(caught.value), customers)


def test_the_reported_leak_no_longer_returns_the_filter_and_mask(
    customers: TableSource,
) -> None:
    """Before the fix this returned the CTE with `id > 1`, `'***'` and the URL parameter name.

    Parameters
    ----------
    customers
        The masked, row-filtered source fixture.
    """
    sql = "SELECT current_query() AS q FROM bronze.customers"
    with pytest.raises(ValidationError, match="current_query may not be called"):
        check_blacklist(sql)

    safe = rewrite_query("SELECT ssn FROM bronze.customers", {"bronze.customers": customers})
    result = execute_query(safe.sql, safe.parameters, read_prefixes=local_prefixes(safe.parameters))
    assert result.to_pylist() == [{"ssn": "***"}, {"ssn": "***"}]


@pytest.mark.parametrize("expression", _INTROSPECTION)
def test_other_introspection_is_refused(expression: str, customers: TableSource) -> None:
    """Settings, secrets, paths, logs and catalog dumps have no use in a governed query.

    Parameters
    ----------
    expression
        A select-list expression that introspects the connection.
    customers
        The masked, row-filtered source fixture.
    """
    with pytest.raises(ValidationError, match=r"may not be called|is not allowed") as caught:
        check_blacklist(f"SELECT {expression} AS x FROM bronze.customers")
    _assert_no_policy_text(str(caught.value), customers)


@pytest.fixture
def hidden_rows(tmp_path: Path) -> TableSource:
    """Build a source whose row filter hides the row holding the smallest id and a marker string.

    Parameters
    ----------
    tmp_path
        pytest's per-test temporary directory.

    Returns
    -------
    TableSource
        A source filtered by `id > 1`; row 1 carries `HIDDENVALUE`.
    """
    path = tmp_path / "h.parquet"
    pq.write_table(pa.table({"id": [1, 2, 3], "ssn": ["HIDDENVALUE", "222", "333"]}), path)
    return TableSource(
        urls=[str(path)],
        file_format="parquet",
        columns={"id": "BIGINT", "ssn": "VARCHAR"},
        row_filters=("id > 1",),
    )


@pytest.mark.parametrize("column", ["id", "ssn"])
def test_stats_would_report_values_the_row_filter_hides(
    column: str, hidden_rows: TableSource
) -> None:
    """DuckDB reads Parquet statistics below the CTE's WHERE, so `stats()` shows hidden rows.

    This pins the raw DuckDB behaviour the guard exists for, and that the guard
    refuses the call before it ever reaches the executor. DuckDB 2 returns the statistics as a
    `VARIANT`, read here as text; it shows the whole hidden string (1.5 showed eight
    characters of it).

    Parameters
    ----------
    column
        The column handed to `stats()`.
    hidden_rows
        The filtered source fixture.
    """
    sql = f"SELECT stats({column}) AS s FROM bronze.customers LIMIT 1"
    as_text = f"SELECT CAST(stats({column}) AS VARCHAR) AS s FROM bronze.customers LIMIT 1"
    rewritten = rewrite_query(as_text, {"bronze.customers": hidden_rows})
    raw = execute_query(
        rewritten.sql, rewritten.parameters, read_prefixes=local_prefixes(rewritten.parameters)
    )
    leaked = raw.column("s")[0].as_py()
    assert ("'min': 1}" if column == "id" else "'max': HIDDENVALUE,") in leaked

    with pytest.raises(ValidationError, match="stats may not be called") as caught:
        check_blacklist(sql)
    assert "id > 1" not in str(caught.value)


@pytest.mark.parametrize("expression", _BIND_TIME)
def test_functions_that_read_statistics_or_wrap_catalog_dumps_are_refused(
    expression: str, customers: TableSource
) -> None:
    """Bind-time statistics and macros that wrap blocked catalog functions are refused.

    Parameters
    ----------
    expression
        A select-list expression.
    customers
        The masked, row-filtered source fixture.
    """
    with pytest.raises(ValidationError, match=r"may not be called|is not allowed") as caught:
        check_blacklist(f"SELECT {expression} AS x FROM bronze.customers")
    _assert_no_policy_text(str(caught.value), customers)
