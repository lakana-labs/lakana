"""The generated check SQL means what the spec says, run in DuckDB on tiny tables."""

from __future__ import annotations

import duckdb

from lakana_core.projects.checks import count_query
from lakana_core.projects.model_checks import (
    key_not_null_sql,
    key_unique_sql,
    measures_sql,
    reference_sql,
)


def _connection() -> duckdb.DuckDBPyConnection:
    """Open DuckDB in memory with a fixed fact and dimension.

    Returns
    -------
    duckdb.DuckDBPyConnection
        The connection, holding `gold.f` and `gold.d`.
    """
    con = duckdb.connect()
    con.execute("CREATE SCHEMA gold")
    con.execute('CREATE TABLE gold.f (id INT, "store id" INT, "order" INT, "we""ird" INT)')
    con.execute(
        "INSERT INTO gold.f VALUES "
        "(1, 10, 1, 5), (1, 11, 2, 5), (2, NULL, 3, NULL), (NULL, 99, 4, 6)"
    )
    con.execute('CREATE TABLE gold.d ("store id" INT)')
    con.execute("INSERT INTO gold.d VALUES (10), (11)")
    return con


def _rows(sql: str) -> list[tuple]:
    """Run a check against a fixed fact and dimension.

    Parameters
    ----------
    sql
        The check.

    Returns
    -------
    list of tuple
        The rows it returns, sorted.
    """
    return sorted(_connection().execute(sql).fetchall(), key=repr)


def test_key_unique_returns_the_rows_of_a_repeated_key() -> None:
    assert _rows(key_unique_sql("gold.f", ["id"])) == [(1, 10, 1, 5), (1, 11, 2, 5)]


def test_key_not_null_returns_the_rows_with_a_null_key() -> None:
    assert _rows(key_not_null_sql("gold.f", ["id"])) == [(None, 99, 4, 6)]


def test_a_reference_returns_orphans_and_skips_nulls() -> None:
    assert _rows(reference_sql("gold.f", "store id", "gold.d", "store id")) == [(None, 99, 4, 6)]


def test_generated_sql_quotes_unusual_column_names() -> None:
    """A reserved word, a space and a double quote in a column name all run."""
    assert _rows(key_unique_sql("gold.f", ["order"])) == []
    assert _rows(key_unique_sql("gold.f", ['we"ird'])) == [(1, 10, 1, 5), (1, 11, 2, 5)]
    assert _rows(key_not_null_sql("gold.f", ['we"ird', "store id"])) == [(2, None, 3, None)]


def test_the_run_counts_the_generated_sql_as_it_counts_a_hand_written_check() -> None:
    """The worker wraps every check in `count_query`; the generated SQL survives the rewrite."""
    con = _connection()
    for sql, failing in (
        (key_unique_sql("gold.f", ["id"]), 2),
        (key_not_null_sql("gold.f", ["id"]), 1),
        (reference_sql("gold.f", "store id", "gold.d", "store id"), 1),
    ):
        assert con.execute(count_query(sql)).fetchone() == (failing,)


def test_measures_sql_returns_no_rows_and_quotes_the_table() -> None:
    con = _connection()
    con.execute('CREATE SCHEMA "we""ird"')
    con.execute('CREATE TABLE "we""ird"."order" (amount DECIMAL(10, 2))')
    con.execute('INSERT INTO "we""ird"."order" VALUES (1.50)')
    assert con.execute(measures_sql('we"ird.order')).fetchall() == []
    assert _rows(measures_sql("gold.f")) == []
