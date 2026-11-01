"""The generated check SQL means what the spec says, run in DuckDB on tiny tables."""

from __future__ import annotations

import duckdb

from lakana_core.projects.checks import count_query
from lakana_core.projects.model_checks import (
    history_key_unique_sql,
    key_not_null_sql,
    key_unique_sql,
    measures_sql,
    reference_sql,
    valid_at_reference_sql,
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


def _versions() -> duckdb.DuckDBPyConnection:
    """Open DuckDB in memory, in UTC as a check runs, with a history dimension `gold.d`.

    Returns
    -------
    duckdb.DuckDBPyConnection
        The connection. `A` is fine; `B` has two open versions; `C` overlaps; `D` says
        `is_current` without being open; `E` has a version of zero length; `F` repeats a
        surrogate key.
    """
    con = duckdb.connect()
    con.execute("SET TimeZone = 'UTC'")
    con.execute("CREATE SCHEMA gold")
    con.execute(
        "CREATE TABLE gold.d (customer_id VARCHAR, d_key VARCHAR, valid_from TIMESTAMPTZ, "
        "valid_to TIMESTAMPTZ, is_current BOOLEAN)"
    )
    con.execute(
        "INSERT INTO gold.d VALUES "
        "('A', 'a1', '2026-01-01', '2026-02-01', false), ('A', 'a2', '2026-02-01', NULL, true), "
        "('B', 'b1', '2026-01-01', NULL, true), ('B', 'b2', '2026-01-15', NULL, true), "
        "('C', 'c1', '2026-01-01', '2026-03-01', false), ('C', 'c2', '2026-02-01', NULL, true), "
        "('D', 'd1', '2026-01-01', NULL, false), "
        "('E', 'e1', '2026-03-01', '2026-03-01', false), "
        "('F', 'f1', '2026-01-01', NULL, true), ('G', 'f1', '2026-01-01', NULL, true)"
    )
    return con


def _keys(con: duckdb.DuckDBPyConnection, sql: str) -> list[tuple[str, str]]:
    """Run a check and keep the key columns of what it returns (timestamps need pytz to fetch).

    Parameters
    ----------
    con
        The connection.
    sql
        The check.

    Returns
    -------
    list of tuple
        ``(customer_id, d_key)``, sorted.
    """
    return sorted(con.execute(f"SELECT customer_id, d_key FROM ({sql})").fetchall())  # nosec B608


def test_the_history_key_check_finds_every_broken_version_and_nothing_else() -> None:
    found = _keys(_versions(), history_key_unique_sql("gold.d", ["customer_id"]))
    assert found == [("B", "b1"), ("C", "c1"), ("D", "d1"), ("E", "e1")]


def test_the_surrogate_key_check_finds_a_repeated_version_key() -> None:
    con = _versions()
    sql = key_unique_sql("gold.d", ["d_key"])  # the shape `surrogate_key_unique_sql` writes
    assert _keys(con, sql) == [("F", "f1"), ("G", "f1")]
    assert con.execute(
        count_query(history_key_unique_sql("gold.d", ["customer_id"]))
    ).fetchone() == (4,)


def test_the_history_key_check_accepts_a_gap_between_versions() -> None:
    """A key may be absent for a while: a version that ends before the next starts is legal."""
    con = _versions()
    con.execute(
        "INSERT INTO gold.d VALUES "
        "('H', 'h1', '2026-01-01', '2026-01-15', false), ('H', 'h2', '2026-02-01', NULL, true)"
    )
    found = _keys(con, history_key_unique_sql("gold.d", ["customer_id"]))
    assert [row for row in found if row[0] == "H"] == []


def test_the_history_key_check_fails_two_versions_that_start_together() -> None:
    """Two versions of one key with the same `valid_from` overlap; exactly one row is returned.

    Which of the two depends on the order of the tie, so the test pins the count only.
    """
    con = _versions()
    con.execute(
        "INSERT INTO gold.d VALUES "
        "('I', 'i1', '2026-01-01', '2026-02-01', false), ('I', 'i2', '2026-01-01', NULL, true)"
    )
    found = _keys(con, history_key_unique_sql("gold.d", ["customer_id"]))
    assert len([row for row in found if row[0] == "I"]) == 1


def test_the_valid_at_reference_reads_half_open_intervals_dates_and_nulls() -> None:
    con = _versions()
    con.execute("CREATE TABLE gold.f (order_id INT, customer_id VARCHAR, ordered_at DATE)")
    con.execute(
        "INSERT INTO gold.f VALUES "
        "(1, 'A', '2026-01-10'), "  # inside A's first version
        "(2, 'A', '2026-02-01'), "  # the instant A moved: the new version, not a gap
        "(3, 'A', '2025-12-31'), "  # before A's first version: fails
        "(4, 'A', NULL), "  # a key without a time cannot be placed: fails
        "(5, NULL, '2026-01-01'), "  # no key: not checked
        "(6, 'Z', '2026-01-01')"  # no such key: fails
    )
    sql = valid_at_reference_sql("gold.f", "customer_id", "gold.d", "customer_id", "ordered_at")
    failing = sorted(con.execute(f"SELECT order_id FROM ({sql})").fetchall())  # nosec B608
    assert failing == [(3,), (4,), (6,)]
