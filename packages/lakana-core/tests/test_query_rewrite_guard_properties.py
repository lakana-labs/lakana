"""Property-based test: no nesting of reads around a statement that changes data is accepted."""

from __future__ import annotations

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from lakana_core.errors import ValidationError
from lakana_core.query_rewrite import check_blacklist, extract_table_refs
from lakana_core.sql_syntax import SQL_SYNTAX_ERRORS, describe_syntax_error

_STATEMENTS = (
    "DELETE FROM bronze.t RETURNING *",
    "INSERT INTO bronze.t VALUES (1) RETURNING *",
    "UPDATE bronze.t SET a = 1 RETURNING *",
    "MERGE INTO bronze.t USING bronze.s ON t.a = s.a WHEN MATCHED THEN DELETE",
    "CREATE TABLE x AS SELECT 1",
    "DROP TABLE x",
    "SELECT a INTO bronze.t2 FROM bronze.t",
    "ATTACH ':memory:' AS x",
    "COPY bronze.t TO 'x.csv'",
    "SET VARIABLE v_0_files = ['x']",
    "CHECKPOINT",
    "VACUUM",
)
"""Statements that are not a read; some parse inside a CTE only, some not at all."""

_WRAPPERS = (
    "WITH w{n} AS ({inner}) SELECT * FROM w{n}",
    "WITH w{n} AS MATERIALIZED ({inner}) SELECT * FROM w{n}",
    "WITH ok{n} AS (SELECT 1 AS a), w{n} AS ({inner}) SELECT * FROM ok{n}",
    "SELECT * FROM ({inner}) AS s{n}",
    "SELECT 1 AS a UNION ALL ({inner})",
    "SELECT * FROM bronze.x, LATERAL ({inner}) AS l{n}",
    "SELECT * FROM bronze.x WHERE a IN (SELECT a FROM ({inner}) AS i{n})",
    "VALUES ((SELECT count(*) FROM ({inner}) AS v{n}))",
    "VALUES (1), ((WITH w{n} AS ({inner}) SELECT count(*) FROM w{n}))",
)
"""Read shapes that put their inner text in a CTE body, a derived table, a set operation or a
bare `VALUES` list."""


def _flip_case(text: str, flips: list[bool]) -> str:
    """Change the letter case of a text, character by character.

    Parameters
    ----------
    text
        The text.
    flips
        Whether each character is upper-cased (cycled when shorter than `text`).

    Returns
    -------
    str
        The text with its letters in the drawn case.
    """
    return "".join(
        char.upper() if flips[index % len(flips)] else char.lower()
        for index, char in enumerate(text)
    )


@settings(max_examples=300, deadline=None)
@given(
    statement=st.sampled_from(_STATEMENTS),
    wrappers=st.lists(st.sampled_from(_WRAPPERS), min_size=1, max_size=4),
    flips=st.lists(st.booleans(), min_size=1, max_size=8),
)
@pytest.mark.slow
def test_no_nesting_of_reads_lets_a_statement_through(
    statement: str, wrappers: list[str], flips: list[bool]
) -> None:
    """Whatever the depth, the case and the shape, the guard and the parser never accept it.

    Parameters
    ----------
    statement
        The statement that is not a read.
    wrappers
        The read shapes around it, innermost first.
    flips
        The letter case drawn for the statement.
    """
    # Inside a derived table sqlglot reads `(CHECKPOINT)` and `(VACUUM)` as a table reference,
    # which is a read the catalog refuses later; they are statements only as a CTE body.
    assume(statement not in {"CHECKPOINT", "VACUUM"} or wrappers[0].startswith("WITH"))
    sql = _flip_case(statement, flips)
    for depth, wrapper in enumerate(wrappers):
        sql = wrapper.format(inner=sql, n=depth)
    for check in (check_blacklist, extract_table_refs):
        with pytest.raises((ValidationError, *SQL_SYNTAX_ERRORS)):
            check(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT id fROM goLd.v_ouELECT $1 At",
        "WITH x AS (SELCET {'id'.id,: 1} AS orders) SELECT id FROM gold.orders "
        "WHERE EXISTS (SELECT 1 FROM x WHERE gold.orders.id = 3)",
    ],
)
def test_found_by_mutation_a_half_parsed_statement_is_refused_by_both_checks(sql: str) -> None:
    """Statements a random mutation of the corpus made the parser fault on are refused (#215, #216).

    Parameters
    ----------
    sql
        A malformed statement the parser raises a non-syntax error on.
    """
    for check in (check_blacklist, extract_table_refs):
        with pytest.raises(SQL_SYNTAX_ERRORS) as caught:
            check(sql)
        assert describe_syntax_error(caught.value, sql).startswith("SQL syntax error")
