"""Tests for SQL query rewriting and the AST blacklist."""

import re
from collections.abc import Callable
from typing import NoReturn

import pytest
import sqlglot
import sqlglot.errors
import sqlglot.expressions as exp

from lakana_core import query_rewrite
from lakana_core.errors import ValidationError
from lakana_core.query_rewrite import (
    ReadField,
    TableSource,
    ViewDefinition,
    _cte_alias,
    check_blacklist,
    expand_views,
    extract_table_refs,
    rewrite_query,
)
from lakana_core.sql_syntax import describe_syntax_error

_READER_CALL = re.compile(r"READ_PARQUET\(GETVARIABLE\('v_\d+_files'\)\)")
"""A generated reader call, reading one of the variables the query process sets."""


def _parquet(*urls: str) -> TableSource:
    """Build a parquet `TableSource` for the given URLs.

    Parameters
    ----------
    *urls
        The resolved URLs the table reads.

    Returns
    -------
    TableSource
        A parquet source over `urls`, with no declared columns (none are
        needed while there is at least one file).
    """
    return TableSource(urls=list(urls), file_format="parquet")


def test_extract_table_refs_finds_every_qualified_table() -> None:
    """Every distinct qualified table name in the query is returned."""
    refs = extract_table_refs("SELECT a.id FROM bronze.a AS a JOIN gold.b AS b ON a.id = b.id")

    assert set(refs) == {"bronze.a", "gold.b"}


def test_a_table_in_a_three_level_namespace_keeps_every_part() -> None:
    """`a.b.c.d` names the table `d` of the namespace `a.b.c`, never `a.b.d`."""
    assert extract_table_refs('SELECT * FROM "a"."b"."c"."d"') == ["a.b.c.d"]
    assert extract_table_refs("SELECT * FROM gold.orders") == ["gold.orders"]


def test_a_table_in_a_three_level_namespace_is_read_under_its_own_name() -> None:
    """The read takes the table's last part as its alias, so `orders.id` still resolves."""
    rewritten = rewrite_query(
        "SELECT orders.id FROM gold.sales.eu.orders",
        sources={"gold.sales.eu.orders": _parquet("a.parquet")},
    )

    assert re.search(r" FROM __lakana_gold_sales_eu_orders_\w+ AS orders$", rewritten.sql)


def test_extract_table_refs_ignores_a_name_the_query_defines_itself() -> None:
    """A `WITH` name is not a catalog table and must not be reported as one.

    `NamespacePath.from_parts([])` raises for an unqualified name, so
    treating a CTE name as a table reference failed the whole query.
    """
    refs = extract_table_refs("WITH x AS (SELECT id FROM raw.events) SELECT id FROM x")

    assert refs == ["raw.events"]


def test_extract_table_refs_reports_a_real_table_shadowed_by_an_inner_cte() -> None:
    """An inner-scope `WITH` name must not hide the real table of that name read outside it.

    The exclusion used to collect every CTE name anywhere in the statement
    and drop every same-named table node, which was an **authorization
    bypass**: the outer reference is a genuine read that DuckDB resolves
    scope-correctly, but it was never reported, so `pipeline.run_one` never
    authorized it.
    """
    sql = (
        "SELECT id FROM raw.events "
        'WHERE id IN (WITH "raw.events" AS (SELECT 1 AS id) SELECT id FROM "raw.events")'
    )

    assert extract_table_refs(sql) == ["raw.events"]


def test_extract_table_refs_reports_an_outer_url_shadowed_by_an_inner_cte() -> None:
    """The exploit shape: a quoted URL DuckDB reads over HTTP, shadowed by a same-named CTE.

    DuckDB reads `FROM "http://…/x.csv"` as a real HTTP fetch, so dropping
    this reference was worker-side SSRF with no authorization call made at
    all. It is reported here; the pipeline then rejects it as a name no
    catalog table has.
    """
    sql = (
        'SELECT * FROM "http://evil/x.csv" WHERE 1 IN ('
        'WITH "http://evil/x.csv" AS (SELECT 1 AS z) SELECT z FROM "http://evil/x.csv")'
    )

    assert extract_table_refs(sql) == ["http://evil/x.csv"]


def test_extract_table_refs_reports_a_qualified_table_beside_a_same_named_cte() -> None:
    """A qualified name is never a `WITH` binding, even where the scope binds that bare name.

    sqlglot keys a scope's sources by bare name, so the binding wins the key
    and the real `db.x` would be dropped again — but DuckDB binds a `WITH`
    name as a single identifier and resolves `db.x` to the real table. Both
    references are read here, the binding under its own alias so that the
    query means one thing; DuckDB runs this exact statement.
    """
    refs = extract_table_refs("WITH x AS (SELECT 1 AS id) SELECT * FROM db.x JOIN x AS y ON TRUE")

    assert refs == ["db.x"]


def test_extract_table_refs_refuses_an_unaliased_qualified_table_beside_that_cte() -> None:
    """Dropping the alias makes `x` mean two things at once, and the query is refused.

    DuckDB refuses it too — `Ambiguous reference to table "x" (use: db.x or
    .x)` — so nothing runnable is lost by declining to guess which of the two
    a bare `x` names.
    """
    with pytest.raises(ValidationError, match="unambiguously"):
        extract_table_refs("WITH x AS (SELECT 1 AS id) SELECT * FROM db.x JOIN x ON TRUE")


def test_extract_table_refs_reports_a_url_shadowed_by_a_derived_table_alias() -> None:
    """A derived table's alias is not a `WITH` binding and must not hide a real read.

    `scope.sources` maps a derived table's alias to a `Scope` too, not only a
    `WITH` binding, so excluding every scope-valued source dropped the second,
    bare reference here — a genuine HTTP read DuckDB issues — while a
    *different* `FROM` entry merely happened to be aliased to the same name.
    Only a name a `WITH` clause actually bound is a binding, which is what
    `Scope.cte_sources` holds and a derived table's alias never enters.
    """
    sql = (
        'SELECT * FROM (SELECT 1 AS z) AS "http://evil.invalid/x.csv", "http://evil.invalid/x.csv"'
    )

    assert extract_table_refs(sql) == ["http://evil.invalid/x.csv"]


def test_extract_table_refs_refuses_a_table_alias_that_reuses_a_cte_name() -> None:
    """The proven exploit: a real read *aliased* to an unrelated `WITH` name.

    The classification used to ask `scope.sources` for the reference's
    `alias_or_name`, so aliasing a genuine external read to a CTE's name
    handed back that CTE's `Scope` and dropped the read from the reported
    references entirely — an **authorization bypass** with the file still
    read for real. Two sources of one scope may not share a name, so the
    query is refused outright now rather than classified at all.
    """
    sql = (
        "WITH c AS (SELECT 1 AS id) "
        'SELECT * FROM (SELECT val FROM c, "http://evil.invalid/x.csv" AS c) AS w'
    )

    with pytest.raises(ValidationError, match="unambiguously"):
        extract_table_refs(sql)


def test_rewrite_refuses_a_table_alias_that_reuses_a_cte_name() -> None:
    """The rewrite refuses the same ambiguity on its own, not only the extraction.

    `rewrite_query` classifies references a second time, so it makes the same
    check independently: neither entry point may rely on the other having
    been called first.
    """
    sql = 'WITH c AS (SELECT 1 AS id) SELECT * FROM (SELECT val FROM c, "raw.events" AS c) AS w'

    with pytest.raises(ValidationError, match="unambiguously"):
        rewrite_query(sql, sources={"raw.events": _parquet("a.parquet")})


def test_extract_table_refs_reports_both_tables_of_an_unqualified_column_join() -> None:
    """An unqualified column over two sources is ordinary SQL, not an ambiguity to refuse.

    sqlglot's `qualify` validates *columns* as well as tables by default, and
    no schema is passed here, so leaving that validation on would refuse this
    query — which DuckDB, holding the real schemas, runs without complaint.
    The reference check is about table and alias resolution only.
    """
    refs = extract_table_refs("SELECT id FROM bronze.a JOIN gold.b ON bronze.a.x = gold.b.y")

    assert refs == ["bronze.a", "gold.b"]


def test_extract_table_refs_reports_two_same_named_tables_in_different_namespaces() -> None:
    """Two namespaces may hold a table of the same name, and both reads are reported.

    Their bare names collide, which is not an ambiguity: DuckDB runs
    `FROM bronze.t, silver.t` and tells the two apart by namespace, so this
    must be classified rather than refused.
    """
    refs = extract_table_refs("SELECT * FROM bronze.t, silver.t")

    assert refs == ["bronze.t", "silver.t"]


def test_extract_table_refs_refuses_a_recursive_cte_rather_than_classifying_it() -> None:
    """What this test used to assert is precisely the bypass, so it is inverted.

    It formerly required `extract_table_refs` to return `[]` here, on the
    reading that a recursive `WITH` name is visible inside its own body and
    so is never a table. That is true of the *recursive* term and false of
    the *base* term, where DuckDB still resolves the name against a real
    relation — and `[]` meant no authorization call at all. Replacing the
    query's shape with a quoted path turned the same assertion into a file
    read nobody checked.

    The name is now refused outright, so the distinction never has to be
    drawn. Kept at this spot, inverted, so the old expectation cannot be
    reintroduced by someone reading the gap as an oversight.
    """
    sql = (
        "WITH RECURSIVE r AS (SELECT 1 AS n UNION ALL SELECT n + 1 FROM r WHERE n < 3) "
        "SELECT n FROM r"
    )

    with pytest.raises(ValidationError, match="Recursive CTEs"):
        extract_table_refs(sql)


def test_extract_table_refs_reports_a_url_aliased_to_a_cte_name() -> None:
    """An alias borrowed from a `WITH` name never makes a real read a `WITH` binding.

    Nothing here is ambiguous — the alias shadows the binding inside the
    derived table, exactly as DuckDB resolves it — so the query is classified
    rather than refused, and the aliased read is a real one that must be
    reported and authorized.
    """
    sql = (
        "WITH c AS (SELECT 1 AS id) "
        'SELECT * FROM (SELECT val FROM "http://evil.invalid/x.csv" AS c) AS w, c'
    )

    assert extract_table_refs(sql) == ["http://evil.invalid/x.csv"]


def test_rewrite_rewrites_a_name_shadowed_by_a_derived_table_alias() -> None:
    """The real read beside a same-named derived table is rewritten to its scoped files."""
    rewritten = rewrite_query(
        'SELECT * FROM (SELECT 1 AS z) AS "raw.events", "raw.events"',
        sources={"raw.events": _parquet("a.parquet")},
    )

    assert _cte_alias("raw.events") in rewritten.sql
    assert len(_READER_CALL.findall(rewritten.sql)) == 1
    assert list(rewritten.parameters.values()) == [["a.parquet"]]


def test_rewrite_rewrites_only_the_outer_occurrence_of_a_shadowed_name() -> None:
    """The real outer read is rewritten to its scoped files; the inner CTE is left alone."""
    rewritten = rewrite_query(
        "SELECT id FROM raw.events "
        'WHERE id IN (WITH "raw.events" AS (SELECT 1 AS id) SELECT id FROM "raw.events")',
        sources={"raw.events": _parquet("a.parquet")},
    )

    assert f"SELECT id FROM {_cte_alias('raw.events')} AS events WHERE" in rewritten.sql
    assert '"raw.events" AS (SELECT 1 AS id) SELECT id FROM "raw.events"' in rewritten.sql
    assert len(_READER_CALL.findall(rewritten.sql)) == 1
    assert list(rewritten.parameters.values()) == [["a.parquet"]]


def test_rewrite_replaces_a_reference_with_a_scoped_read() -> None:
    """A qualified table name becomes a CTE reading only the given URLs."""
    rewritten = rewrite_query(
        "SELECT id FROM bronze.weather",
        sources={"bronze.weather": _parquet("https://example/a.parquet")},
    )

    assert _READER_CALL.search(rewritten.sql)
    assert list(rewritten.parameters.values()) == [["https://example/a.parquet"]]
    assert "bronze.weather" not in rewritten.sql.replace(_cte_alias("bronze.weather"), "")


def test_rewrite_never_puts_a_resolved_url_in_the_sql() -> None:
    """The resolved URL is set as a variable, never rendered — the whole injection defence.

    A storage object key is attacker-controlled text (a raw-zone landing
    area accepts arbitrary writes by design), so a key containing a single
    quote would close an interpolated string literal and run the rest of the
    key as SQL inside another principal's authorized query.
    """
    evil = "a.parquet']) UNION ALL SELECT 42 AS id FROM (SELECT ['"

    rewritten = rewrite_query("SELECT id FROM raw.events", sources={"raw.events": _parquet(evil)})

    assert evil not in rewritten.sql
    assert rewritten.sql.count("'") == 2  # only the quotes of the variable's name
    assert "UNION" not in rewritten.sql
    assert list(rewritten.parameters.values()) == [[evil]]


@pytest.mark.parametrize(
    ("sql", "rendered"),
    [
        (
            "SELECT list_transform([1, 2], x -> x + 1)",
            "SELECT LIST_TRANSFORM([1, 2], LAMBDA x : x + 1)",
        ),
        (
            "SELECT list_reduce([1, 2, 3], (x, y) -> x + y)",
            "SELECT LIST_REDUCE([1, 2, 3], LAMBDA x, y : x + y)",
        ),
        (
            "SELECT list_transform([[1]], x -> list_transform(x, y -> y * 2))",
            "SELECT LIST_TRANSFORM([[1]], LAMBDA x : LIST_TRANSFORM(x, LAMBDA y : y * 2))",
        ),
        (
            "SELECT list_transform([1, 2], lambda x: x + 1)",
            "SELECT LIST_TRANSFORM([1, 2], LAMBDA x : x + 1)",
        ),
    ],
)
def test_rewrite_renders_every_lambda_in_the_keyword_form(sql: str, rendered: str) -> None:
    """DuckDB 2 refuses `x -> …`; the rewrite renders `LAMBDA x : …`, which 1.5 runs too.

    Parameters
    ----------
    sql
        The query as the caller wrote it.
    rendered
        The SQL the rewrite hands to DuckDB.
    """
    assert rewrite_query(sql, {}).sql == rendered


@pytest.mark.parametrize(
    ("sql", "rendered"),
    [
        (
            "SELECT list_transform([1, 2, 3], mod(x -> x + 10, 4))",
            "SELECT LIST_TRANSFORM([1, 2, 3], (LAMBDA x : x + 10) % 4)",
        ),
        (
            "SELECT list_transform([1, 2, 3], x -> mod(y -> y + 10, 4))",
            "SELECT LIST_TRANSFORM([1, 2, 3], LAMBDA x : (LAMBDA y : y + 10) % 4)",
        ),
        (
            "SELECT json_extract((x, y) -> x + 1, '$.a')",
            "SELECT (LAMBDA x, y : x + 1) -> '$.a'",
        ),
    ],
)
def test_a_lambda_that_is_an_operand_keeps_its_grouping(sql: str, rendered: str) -> None:
    """`LAMBDA x : …` extends as far right as it can, so an operand lambda is parenthesised.

    sqlglot turns `mod(a, b)` into the operator `a % b`; rendered bare, the lambda's body would
    take the `% 4` in (`LAMBDA x : x + 10 % 4`), a grouping the guard never checked. DuckDB
    refuses a lambda as an operand either way; the parentheses make it refuse what was checked.

    Parameters
    ----------
    sql
        The query as the caller wrote it.
    rendered
        The SQL the rewrite hands to DuckDB.
    """
    assert rewrite_query(sql, {}).sql == rendered


def test_rewrite_renders_a_lambda_in_the_keyword_form_beside_a_table_read() -> None:
    """The query that reads a table goes through the same rendering as one that reads none."""
    rewritten = rewrite_query(
        "SELECT list_filter(e.tags, t -> t <> 'x') FROM raw.events AS e",
        sources={"raw.events": _parquet("a.parquet")},
    )

    assert "LIST_FILTER(e.tags, LAMBDA t : t <> 'x')" in rewritten.sql
    assert "->" not in rewritten.sql


_JSON = 'FROM (SELECT \'{"a": "b"}\'::JSON AS j) AS d'
"""A derived table with one JSON column `j`, for the JSON-arrow cases."""


@pytest.mark.parametrize(
    ("expression", "rendered"),
    [
        ("upper(j -> '$.a')", "UPPER(j -> '$.a')"),
        ("coalesce(j -> 'a', 'x')", "COALESCE(j -> 'a', 'x')"),
        ("json_extract_string(j -> 'n', '$.m')", "(j -> 'n') ->> '$.m'"),
        ("count(j -> 'a')", "COUNT(j -> 'a')"),
        ("upper(j ->> 'a')", "UPPER(j ->> '$.a')"),
        ("upper(j -> 'n' -> 'm')", "UPPER(j -> 'n' -> '$.m')"),
        ("upper(j -> 'n' ->> 'm')", "UPPER((j -> 'n') ->> '$.m')"),
        ("upper(\"j\" -> 'a')", "UPPER(\"j\" -> 'a')"),
        ("upper(j -> '$.a' || 'x')", "UPPER(j -> ('$.a' || 'x'))"),
    ],
)
def test_a_json_arrow_as_a_function_argument_stays_a_json_extract(
    expression: str, rendered: str
) -> None:
    """A JSON arrow in a function's arguments runs as DuckDB runs it: as a JSON extract.

    Only a function that takes a lambda gets one: anywhere else the arrow is turned into the JSON
    extract DuckDB runs before the guard sees the tree, and is rendered as an arrow again.

    Parameters
    ----------
    expression
        The select-list expression as the caller wrote it.
    rendered
        What the rewrite renders it as.
    """
    sql = f"SELECT {expression} AS v {_JSON}"
    check_blacklist(sql)

    rewritten = rewrite_query(sql, {}).sql

    assert "LAMBDA" not in rewritten
    assert f"{rendered} AS v" in rewritten


@pytest.mark.parametrize(
    "expression",
    [
        "list_transform(l, x -> x + 1)",
        "array_transform(l, x -> x + 1)",
        "list_apply(l, x -> x + 1)",
        "array_apply(l, x -> x + 1)",
        "list_filter(l, x -> x > 1)",
        "array_filter(l, x -> x > 1)",
        "filter(l, x -> x > 1)",
        "list_reduce(l, (x, y) -> x + y)",
        "array_reduce(l, (x, y) -> x + y)",
        "reduce(l, (x, y) -> x + y, 0)",
        "list_transform(l, x -> x -> 'a')",
    ],
)
def test_a_lambda_in_a_function_that_takes_one_stays_a_lambda(expression: str) -> None:
    """Every function DuckDB's catalog lists with a LAMBDA parameter keeps its lambda.

    Parameters
    ----------
    expression
        A call with a lambda argument.
    """
    rewritten = rewrite_query(f"SELECT {expression} AS v FROM (SELECT [1] AS l) AS d", {}).sql

    assert "LAMBDA" in rewritten
    assert "->" not in rewritten.replace("x -> 'a'", "").replace("x -> '$.a'", "")


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT COLUMNS(c -> c LIKE 'a%') FROM (SELECT 1 AS ab) AS d",
        "SELECT upper(lambda j: 'a') FROM (SELECT 1 AS j) AS d",
        "SELECT upper((j, k) -> 'a') FROM (SELECT 1 AS j) AS d",
    ],
)
def test_columns_a_keyword_lambda_and_two_parameters_stay_lambdas(sql: str) -> None:
    """`COLUMNS` takes a lambda; a keyword lambda or one of two parameters is never JSON.

    Parameters
    ----------
    sql
        A query whose lambda must stay a lambda.
    """
    assert "LAMBDA" in rewrite_query(sql, {}).sql


def test_the_lambda_functions_sqlglot_gives_a_class_are_matched_by_that_class() -> None:
    """`list_transform`, `list_filter` and `reduce` parse to classes; `COLUMNS` is one too."""
    assert set(query_rewrite._LAMBDA_FUNCTION_TYPES) == {
        exp.Columns,
        exp.Transform,
        exp.ArrayFilter,
        exp.Reduce,
    }


def test_a_refused_function_in_a_json_arrow_argument_is_still_refused() -> None:
    """Turning the arrow into a JSON extract keeps its right side in the tree the guard checks."""
    with pytest.raises(ValidationError, match="getvariable"):
        check_blacklist(f"SELECT upper(j -> getvariable('v_0_files')) {_JSON}")


def test_the_lambda_functions_are_the_ones_duckdbs_catalog_lists() -> None:
    """The union of `duckdb_functions()` with a LAMBDA parameter on 1.5.5 and 2.0.0."""
    assert (
        frozenset(
            {
                "apply",
                "array_apply",
                "array_filter",
                "array_reduce",
                "array_transform",
                "filter",
                "invoke",
                "list_apply",
                "list_filter",
                "list_reduce",
                "list_transform",
                "reduce",
            }
        )
        == query_rewrite._LAMBDA_FUNCTIONS
    )


def test_rewrite_keeps_the_original_table_alias() -> None:
    """Dropping the alias left the rest of the query unable to resolve `e.id`."""
    rewritten = rewrite_query(
        "SELECT e.id FROM raw.events AS e", sources={"raw.events": _parquet("a.parquet")}
    )

    assert f"{_cte_alias('raw.events')} AS e" in rewritten.sql
    assert "e.id" in rewritten.sql


def test_rewrite_aliases_an_unaliased_read_by_the_table_name() -> None:
    """`orders.id` must find the read, which is only known by its generated name otherwise."""
    rewritten = rewrite_query(
        "SELECT orders.id FROM gold.orders", sources={"gold.orders": _parquet("a.parquet")}
    )

    assert f"FROM {_cte_alias('gold.orders')} AS orders" in rewritten.sql
    assert rewritten.sql.endswith(
        "SELECT orders.id FROM " + _cte_alias("gold.orders") + " AS orders"
    )


@pytest.mark.parametrize(
    ("clause", "sql"),
    [
        ("PIVOT", "SELECT * FROM gold.sales PIVOT (sum(id) FOR region IN ('eu', 'us'))"),
        ("UNPIVOT", "SELECT * FROM gold.sales UNPIVOT (v FOR k IN (a, b))"),
        ("TABLESAMPLE", "SELECT * FROM gold.sales TABLESAMPLE RESERVOIR (1 ROWS)"),
        ("JOIN", "SELECT * FROM (gold.sales JOIN gold.other ON TRUE)"),
    ],
)
def test_rewrite_keeps_what_follows_a_table_reference(clause: str, sql: str) -> None:
    """A clause sqlglot attaches to the table node survives its replacement (#280)."""
    sources = {"gold.sales": _parquet("a.parquet"), "gold.other": _parquet("b.parquet")}

    rewritten = rewrite_query(sql, sources)

    assert clause in rewritten.sql
    if clause == "JOIN":
        assert _cte_alias("gold.other") + " AS other" in rewritten.sql


@pytest.mark.parametrize(
    ("sql", "named"),
    [
        ("SELECT * FROM gold.sales AT (VERSION => 1)", "AT (...)"),
        ("SELECT * FROM ONLY gold.sales", "ONLY"),
        ("SELECT * FROM gold.sales WITH ORDINALITY", "WITH ORDINALITY"),
        ("SELECT * FROM gold.sales WITH (NOLOCK)", "a table hint"),
    ],
)
def test_rewrite_refuses_a_table_clause_it_cannot_keep(sql: str, named: str) -> None:
    """A clause on a table reference the rewrite cannot carry over is refused, never dropped."""
    with pytest.raises(ValidationError) as caught:
        rewrite_query(sql, {"gold.sales": _parquet("a.parquet")})

    assert named in str(caught.value)
    assert "'gold.sales'" in str(caught.value)


def test_every_argument_of_a_table_reference_is_kept_or_refused_by_name() -> None:
    """A table argument a later sqlglot adds fails here, so it is classified, not dropped."""
    classified = {
        "this",
        "db",
        "catalog",
        "alias",
        *query_rewrite._KEPT_TABLE_ARGS,
        *query_rewrite._REFUSED_TABLE_CLAUSES,
    }

    assert set(exp.Table.arg_types) == classified


def test_rewrite_keeps_the_quoting_of_the_table_name_it_uses_as_alias() -> None:
    """A table written with quotes keeps them, so `"Orders".id` still matches."""
    rewritten = rewrite_query(
        'SELECT "Orders".id FROM gold."Orders"', sources={"gold.Orders": _parquet("a.parquet")}
    )

    assert 'AS "Orders"' in rewritten.sql


def test_rewrite_cuts_the_schema_off_a_fully_qualified_column() -> None:
    """`gold.orders.id` becomes `orders.id`; other dotted columns stay as written."""
    rewritten = rewrite_query(
        "SELECT gold.orders.id, other.orders.id, orders.address.country FROM gold.orders",
        sources={"gold.orders": _parquet("a.parquet")},
    )

    assert "SELECT orders.id, other.orders.id, orders.address.country FROM" in rewritten.sql


def test_rewrite_cuts_the_catalog_and_schema_off_a_three_part_column() -> None:
    """A table with a catalog part is reduced to its name the same way."""
    rewritten = rewrite_query(
        "SELECT a.b.c.id FROM a.b.c", sources={"a.b.c": _parquet("a.parquet")}
    )

    assert "SELECT c.id FROM" in rewritten.sql


def test_rewrite_leaves_a_qualified_column_alone_when_the_table_has_an_alias() -> None:
    """With an alias, `gold.orders.id` is not valid for the database either and stays as typed."""
    rewritten = rewrite_query(
        "SELECT gold.orders.id FROM gold.orders AS o",
        sources={"gold.orders": _parquet("a.parquet")},
    )

    assert "SELECT gold.orders.id FROM" in rewritten.sql


def test_rewrite_keeps_the_internal_name_for_two_unaliased_tables_of_one_name() -> None:
    """Both would be called `orders`, which the database refuses, so neither gets the alias."""
    rewritten = rewrite_query(
        "SELECT 1 FROM gold.orders JOIN silver.orders ON true",
        sources={"gold.orders": _parquet("a.parquet"), "silver.orders": _parquet("b.parquet")},
    )

    assert " AS orders" not in rewritten.sql


def test_rewrite_leaves_qualifiers_alone_when_two_reads_share_a_name() -> None:
    """A clash fails in the database as it always did; nothing is bound to one of the two."""
    rewritten = rewrite_query(
        "SELECT orders.id, gold.orders.id FROM gold.orders JOIN silver.orders ON true",
        sources={"gold.orders": _parquet("a.parquet"), "silver.orders": _parquet("b.parquet")},
    )

    assert "SELECT orders.id, gold.orders.id FROM" in rewritten.sql


def test_rewrite_gives_no_alias_when_the_name_is_used_in_another_scope() -> None:
    """The outer read and the inner one are both `orders`: neither takes the name."""
    rewritten = rewrite_query(
        "SELECT id FROM gold.orders WHERE EXISTS "
        "(SELECT 1 FROM silver.orders WHERE silver.orders.id = gold.orders.id)",
        sources={"gold.orders": _parquet("a.parquet"), "silver.orders": _parquet("b.parquet")},
    )

    assert " AS orders" not in rewritten.sql
    assert "silver.orders.id = gold.orders.id" in rewritten.sql


@pytest.mark.parametrize(
    "sql",
    [
        "WITH orders AS (SELECT 1 AS note) SELECT orders.note FROM orders, gold.orders",
        "SELECT orders.x FROM silver.orders AS orders JOIN gold.orders ON true",
        "SELECT 1 FROM gold.orders WHERE EXISTS (SELECT 1 FROM (SELECT 3 AS id) AS orders)",
    ],
)
def test_rewrite_gives_no_alias_when_another_source_is_called_the_same(sql: str) -> None:
    """A CTE, an explicit alias or a derived table of that name stays the only one."""
    rewritten = rewrite_query(
        sql,
        sources={"gold.orders": _parquet("a.parquet"), "silver.orders": _parquet("b.parquet")},
    )

    assert _cte_alias("gold.orders") + " AS orders" not in rewritten.sql


def test_rewrite_cuts_the_schema_only_in_the_scope_of_the_read() -> None:
    """In a subquery or a CTE body the full name stays as written; the database refuses it."""
    rewritten = rewrite_query(
        "WITH x AS (SELECT gold.orders.id FROM raw.items) "
        "SELECT gold.orders.id, (SELECT gold.orders.id) FROM gold.orders "
        "WHERE id IN (SELECT gold.orders.id)",
        sources={"gold.orders": _parquet("a.parquet")},
    )

    assert rewritten.sql.count("gold.orders.id") == 3
    assert "SELECT orders.id," in rewritten.sql


def test_rewrite_leaves_a_qualifier_alone_in_a_sibling_branch() -> None:
    """The other branch of a UNION cannot see the read, so its column is not rebound."""
    rewritten = rewrite_query(
        "SELECT id FROM gold.orders UNION ALL SELECT gold.orders.id FROM raw.other",
        sources={"gold.orders": _parquet("a.parquet")},
    )

    assert "SELECT gold.orders.id FROM raw.other" in rewritten.sql


_DATE = exp.DataType(this=exp.DType.DATE)
_TIMESTAMP = exp.DataType(this=exp.DType.TIMESTAMP)


@pytest.mark.parametrize(
    ("row_filter", "compiled"),
    [
        (
            "born < TIMESTAMP '2026-01-01 00:00:00'",
            "TRY_CAST(born AS TIMESTAMP) < CAST('2026-01-01 00:00:00' AS TIMESTAMP)",
        ),
        (
            "TIMESTAMP '2026-01-01 00:00:00' > Born",
            "CAST('2026-01-01 00:00:00' AS TIMESTAMP) > TRY_CAST(Born AS TIMESTAMP)",
        ),
        ("born < seen", "TRY_CAST(born AS TIMESTAMP) < seen"),
        ("NOT (seen = (born))", "NOT (seen = (TRY_CAST(born AS TIMESTAMP)))"),
        ("seen > DATE '2026-01-01'", "seen > TRY_CAST(CAST('2026-01-01' AS DATE) AS TIMESTAMP)"),
    ],
)
def test_rewrite_converts_the_date_side_of_a_date_timestamp_filter_with_try_cast(
    row_filter: str, compiled: str
) -> None:
    """A date outside the timestamp range becomes NULL in the comparison, never an error (#178).

    Parameters
    ----------
    row_filter
        A stored filter comparing a date with a timestamp.
    compiled
        How the generated read writes it.
    """
    source = TableSource(
        urls=["a.parquet"],
        file_format="parquet",
        columns={"born": "date", "seen": "timestamp"},
        row_filters=(row_filter,),
        mask_types={"born": _DATE, "seen": _TIMESTAMP},
    )

    rewritten = rewrite_query("SELECT * FROM gold.people", {"gold.people": source})

    assert f"WHERE ({compiled})" in rewritten.sql


@pytest.mark.parametrize(
    "row_filter",
    [
        "born = DATE '2026-01-01'",
        "seen > TIMESTAMP '2026-01-01 00:00:00'",
        "born = born",
        "name = 'born'",
        "has_group('x')",
    ],
)
def test_rewrite_leaves_a_filter_without_a_date_timestamp_comparison_as_written(
    row_filter: str,
) -> None:
    """Only a date compared with a timestamp is converted; every other filter is kept verbatim.

    Parameters
    ----------
    row_filter
        A stored filter with no comparison of a date with a timestamp.
    """
    source = TableSource(
        urls=["a.parquet"],
        file_format="parquet",
        columns={"born": "date", "seen": "timestamp", "name": "string"},
        row_filters=(row_filter,),
        mask_types={"born": _DATE, "seen": _TIMESTAMP},
    )

    rewritten = rewrite_query("SELECT * FROM gold.people", {"gold.people": source})

    assert "TRY_CAST" not in rewritten.sql


def test_rewrite_merges_its_ctes_ahead_of_the_querys_own() -> None:
    """A query's own CTE may read a rewritten table, so the generated CTE comes first."""
    rewritten = rewrite_query(
        "WITH x AS (SELECT id FROM raw.events) SELECT id FROM x",
        sources={"raw.events": _parquet("a.parquet")},
    )

    assert rewritten.sql.index(f"{_cte_alias('raw.events')} AS (") < rewritten.sql.index("x AS (")
    assert f"x AS (SELECT id FROM {_cte_alias('raw.events')} AS events)" in rewritten.sql
    assert list(rewritten.parameters.values()) == [["a.parquet"]]


def test_rewrite_emits_one_cte_for_a_table_referenced_twice() -> None:
    """A self-join names the same table twice; a duplicate CTE name is an error."""
    rewritten = rewrite_query(
        "SELECT a.id FROM gold.sales AS a JOIN gold.sales AS b ON a.id = b.id",
        sources={"gold.sales": _parquet("a.parquet")},
    )

    assert rewritten.sql.count(f"{_cte_alias('gold.sales')} AS (") == 1
    assert list(rewritten.parameters.values()) == [["a.parquet"]]


def test_rewrite_binds_one_parameter_per_cte_in_placeholder_order() -> None:
    """Each CTE reads its own variable, in the order the CTEs are generated."""
    rewritten = rewrite_query(
        "SELECT a.id FROM bronze.a AS a JOIN gold.b AS b ON a.id = b.id",
        sources={"bronze.a": _parquet("a.parquet"), "gold.b": _parquet("b.csv")},
    )

    first, second = rewritten.parameters.values()
    assert rewritten.sql.index(f"{_cte_alias('bronze.a')} AS (") < rewritten.sql.index(
        f"{_cte_alias('gold.b')} AS ("
    )
    assert first == ["a.parquet"]
    assert second == ["b.csv"]


def test_rewrite_reads_a_table_with_no_files_as_a_typed_empty_relation() -> None:
    """An Iceberg table before its first snapshot has no files; that is zero rows, not an error."""
    rewritten = rewrite_query(
        "SELECT id FROM gold.sales",
        sources={
            "gold.sales": TableSource(
                urls=[], file_format="parquet", columns={"id": "long", "region": "string"}
            )
        },
    )

    assert "READ_PARQUET" not in rewritten.sql
    assert 'CAST(NULL AS BIGINT) AS "id"' in rewritten.sql
    assert "WHERE FALSE" in rewritten.sql
    assert rewritten.parameters == {}


def test_rewrite_rejects_a_table_with_neither_files_nor_declared_columns() -> None:
    """Without a declared schema there is nothing to shape an empty relation from."""
    with pytest.raises(ValidationError, match="no declared columns"):
        rewrite_query(
            "SELECT id FROM gold.sales",
            sources={"gold.sales": TableSource(urls=[], file_format="parquet")},
        )


def test_rewrite_rejects_a_statement_that_is_not_a_query() -> None:
    """A DELETE naming a catalog table has nowhere to attach a CTE and no meaning here."""
    with pytest.raises(ValidationError, match="DELETE"):
        rewrite_query("DELETE FROM gold.sales", sources={"gold.sales": _parquet("a.parquet")})


@pytest.mark.parametrize(
    ("sql", "key"),
    [
        ("PIVOT gold.sales ON a IN ('x') USING sum(b)", "PIVOT"),
        ("SELECT 1 AS a; SELECT b FROM gold.sales", "BLOCK"),
    ],
)
def test_rewrite_rejects_a_catalog_table_under_a_statement_that_is_no_query(
    sql: str, key: str
) -> None:
    """Defence in depth: a top-level `PIVOT` or a `;`-separated block passes the walk.

    The statement has no scope to attach a CTE to, so the rewrite refuses it itself, naming
    what it parsed to.

    Parameters
    ----------
    sql
        A statement that is not an `exp.Query` and names a catalog table.
    key
        The statement kind the message names.
    """
    with pytest.raises(ValidationError, match=f"{key} may not reference a catalog table"):
        rewrite_query(sql, sources={"gold.sales": _parquet("a.parquet")})


def test_rewrite_leaves_a_reference_not_in_sources_untouched() -> None:
    """A table reference outside `sources` is not this call's concern and is left alone."""
    rewritten = rewrite_query(
        "SELECT a.id FROM bronze.a AS a JOIN gold.b AS b ON a.id = b.id",
        sources={"bronze.a": _parquet("https://example/a.parquet")},
    )

    assert _READER_CALL.search(rewritten.sql)
    assert "gold.b" in rewritten.sql


def test_rewrite_returns_the_original_sql_when_sources_matches_nothing() -> None:
    """When no table in the query is a key in `sources`, the SQL is returned unchanged."""
    sql = "SELECT id FROM bronze.weather"

    rewritten = rewrite_query(sql, sources={"gold.sales": _parquet("https://example/a.parquet")})

    assert rewritten.sql == sql
    assert rewritten.parameters == {}
    assert "WITH" not in rewritten.sql


def test_blacklist_rejects_a_direct_read_parquet_call() -> None:
    """A user cannot bypass the catalog by calling a file-reading function directly."""
    with pytest.raises(ValidationError, match="read_parquet"):
        check_blacklist("SELECT * FROM read_parquet('s3://other-bucket/x.parquet')")


def test_blacklist_rejects_a_direct_read_json_call() -> None:
    """read_json parses as exp.Anonymous; the blacklist must still catch it."""
    with pytest.raises(ValidationError, match="read_json"):
        check_blacklist("SELECT * FROM read_json('s3://other-bucket/x.json')")


def test_blacklist_rejects_a_direct_read_json_auto_call() -> None:
    """read_json_auto parses as exp.Anonymous; the blacklist must still catch it."""
    with pytest.raises(ValidationError, match="read_json_auto"):
        check_blacklist("SELECT * FROM read_json_auto('s3://other-bucket/x.json')")


@pytest.mark.parametrize(
    "function_name",
    [
        "read_parquet",
        "parquet_scan",
        "read_csv",
        "read_csv_auto",
        "read_json",
        "read_json_auto",
        "read_ndjson",
        "read_json_objects",
        "read_text",
        "read_blob",
        "glob",
        "query",
        "query_table",
        "sniff_csv",
        "parquet_metadata",
        "parquet_schema",
        "parquet_file_metadata",
        "parquet_kv_metadata",
        "parquet_full_metadata",
        "parquet_bloom_probe",
        "read_duckdb",
        "read_json_objects_auto",
        "read_ndjson_auto",
        "read_ndjson_objects",
        "json_execute_serialized_sql",
        "which_secret",
    ],
)
def test_blacklist_rejects_every_file_reading_function(function_name: str) -> None:
    """Every DuckDB file reader and its aliases are rejected, not just the canonical names.

    Parameters
    ----------
    function_name
        The reader function the query calls directly.
    """
    with pytest.raises(ValidationError, match=function_name):
        check_blacklist(f"SELECT * FROM {function_name}('/etc/passwd')")


def test_blacklist_rejects_a_quoted_function_name() -> None:
    """Quoted function names must be caught by the blacklist too.

    When a function name is written with quotes (e.g., `"read_text"(...)`),
    sqlglot represents it as an Identifier with quoted=True. The bare name
    extraction must handle this case to prevent bypasses.
    """
    with pytest.raises(ValidationError, match="read_text"):
        check_blacklist("SELECT * FROM \"read_text\"('/etc/passwd')")


def test_blacklist_allows_the_glob_operator() -> None:
    """`GLOB` as a predicate is not the `glob()` table function and stays allowed."""
    check_blacklist("SELECT id FROM bronze.weather WHERE name GLOB 'a*'")


@pytest.mark.parametrize(
    ("statement", "message"),
    [
        ("ATTACH 'file:///etc/passwd' AS x", "ATTACH"),
        ("COPY bronze.weather TO '/tmp/out.csv'", "COPY"),
        ("INSTALL httpfs", "INSTALL"),
        ("LOAD httpfs", "LOAD"),
        ("CREATE SECRET s (TYPE S3, PROVIDER credential_chain)", "CREATE"),
        ("SET s3_access_key_id = 'AKIA'", "SET"),
        ("PRAGMA database_list", "PRAGMA"),
        ("DESCRIBE bronze.weather", "DESCRIBE"),
        ("CREATE TABLE x AS SELECT 1", "CREATE"),
        ("EXPLAIN SELECT 1", "Only a single query"),
        ("CALL dbgen(sf = 1)", "Only a single query"),
        ("SELECT 1; ATTACH 'file:///etc/passwd' AS x", "ATTACH"),
        ("SELECT 1; LOAD httpfs", "LOAD"),
        ("SELECT id FROM bronze.weather; SET s3_access_key_id = 'AKIA'", "Only a single query"),
    ],
)
def test_blacklist_rejects_anything_that_is_not_a_single_query(
    statement: str, message: str
) -> None:
    """Only a query may be submitted: every other statement shape is refused.

    Parameters
    ----------
    statement
        SQL that is not a single plain query.
    message
        Text the resulting error must name, so the rejection is legible.
    """
    with pytest.raises(ValidationError, match=message):
        check_blacklist(statement)


def test_blacklist_allows_ordinary_table_references() -> None:
    """A normal query against catalog tables is never rejected."""
    check_blacklist("SELECT id FROM bronze.weather WHERE temp_c > 0")


def test_blacklist_allows_a_function_call_that_is_not_blacklisted() -> None:
    """A function call outside the blacklist is inspected and let through, not rejected."""
    check_blacklist("SELECT COUNT(*) FROM bronze.weather")


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT o.id, c.name FROM gold.orders AS o "
        "JOIN gold.customers AS c ON o.customer_id = c.id",
        "WITH recent AS (SELECT id FROM bronze.weather) SELECT id FROM recent",
        "SELECT id FROM bronze.a UNION SELECT id FROM bronze.b",
        "(SELECT id FROM bronze.weather)",
    ],
)
def test_blacklist_allows_every_query_shape(sql: str) -> None:
    """The structural rule admits every shape a legitimate read takes.

    Parameters
    ----------
    sql
        A query that must pass the check unchanged.
    """
    check_blacklist(sql)


_NEW_REFUSED_FUNCTIONS = (
    "setval",
    "nextval",
    "currval",
    "index_key",
    "to_aggregate_state",
    "__cast",
    "__between",
    "__internal_barrier",
    "__internal_tablefilter_dynamic",
    "__internal_compress_string_ubigint",
    "__internal_decompress_string",
    "__internal_not_yet_released",
)
"""Functions refused since DuckDB 2.0.0: five named ones and samples of the `__` namespace.

`__internal_compress_string_ubigint` and `__internal_decompress_string` already exist on
1.5.5; `__internal_not_yet_released` stands for a name a later release adds, which the
prefix rule must catch without a list entry.
"""

_FUNCTION_CALL_FORMS = {
    "lower": "SELECT {f}(1, 2)",
    "upper": "SELECT {F}(1, 2)",
    "quoted": 'SELECT "{f}"(1, 2)',
    "quoted_upper": 'SELECT "{F}"(1, 2)',
    "qualified": "SELECT main.{f}(1, 2)",
    "fully_qualified": "SELECT system.main.{f}(1, 2)",
    "quoted_qualified": 'SELECT "main"."{f}"(1, 2)',
    "method_call": "SELECT 's'.{f}(1)",
    "method_call_on_column": "SELECT x.{f}(1) FROM range(1) AS t(x)",
    "lambda": "SELECT list_transform([1], x -> {f}(x, 2))",
    "lambda_keyword": "SELECT list_transform([1], lambda x: {f}(x, 2))",
    "lambda_duckdb_keyword": "SELECT list_transform([1], LAMBDA x : {f}(x, 2))",
    "lambda_two_parameters": "SELECT list_reduce([1], (x, y) -> {f}(x, y))",
    "cte": "WITH c AS (SELECT {f}(1, 2) AS v) SELECT v FROM c",
    "subquery": "SELECT (SELECT {f}(1, 2))",
    "derived_table": "SELECT v FROM (SELECT {f}(1, 2) AS v) AS d",
    "lateral": "SELECT * FROM range(1) AS r(x), LATERAL (SELECT {f}(r.x, 2) AS v)",
    "where": "SELECT x FROM range(1) AS t(x) WHERE {f}(x, 2) > 0",
    "order_by": "SELECT x FROM range(1) AS t(x) ORDER BY {f}(x, 2)",
    "filter": "SELECT sum(x) FILTER (WHERE {f}(x, 2) > 0) FROM range(1) AS t(x)",
    "window": "SELECT {f}(x, 2) OVER () FROM range(1) AS t(x)",
    "values": "VALUES ({f}(1, 2))",
    "generator_argument": "SELECT * FROM range({f}(1, 2))",
}
"""Every way a query can spell a scalar call, each parsed by sqlglot to an `exp.Anonymous`.

`{f}` is the lowercase name and `{F}` the uppercase one; DuckDB resolves a function name
case-insensitively, quoted or not, and through any catalog or schema qualifier.
"""


@pytest.mark.parametrize("form", sorted(_FUNCTION_CALL_FORMS))
@pytest.mark.parametrize("function_name", _NEW_REFUSED_FUNCTIONS)
def test_blacklist_refuses_duckdb_internal_and_state_changing_functions_in_every_form(
    function_name: str, form: str
) -> None:
    """A refused function is refused however the query spells or nests the call.

    `setval` and `nextval` change a sequence and `currval` reads one, `index_key` reads a
    catalog index by name, `to_aggregate_state` picks an aggregate by a name given as data,
    and a name starting with `__` belongs to DuckDB's internal namespace.

    Parameters
    ----------
    function_name
        The function the query calls.
    form
        The key of the call's spelling in `_FUNCTION_CALL_FORMS`.
    """
    sql = _FUNCTION_CALL_FORMS[form].format(f=function_name, F=function_name.upper())

    with pytest.raises(ValidationError, match=re.escape(function_name)):
        check_blacklist(sql)


@pytest.mark.parametrize(
    ("function_name", "cause"),
    [
        ("setval", "changes or reads a sequence"),
        ("nextval", "changes or reads a sequence"),
        ("currval", "changes or reads a sequence"),
        ("to_aggregate_state", "picks an aggregate function by a name given as data"),
    ],
)
def test_blacklist_names_the_cause_and_the_next_step_for_a_state_or_dispatch_function(
    function_name: str, cause: str
) -> None:
    """A sequence function or `to_aggregate_state` is refused with its own reason.

    "Query the catalog instead" is no next step for these: the message names what the call
    does and says to remove it.

    Parameters
    ----------
    function_name
        The refused function.
    cause
        The part of the message that names what the function does.
    """
    with pytest.raises(ValidationError) as caught:
        check_blacklist(f"SELECT {function_name}('s')")

    message = str(caught.value)
    assert message.startswith(f"{function_name} ")
    assert cause in message
    assert "Remove the call; a query reads tables only." in message
    assert "catalog" not in message


def test_blacklist_tells_the_user_what_to_write_instead_of_an_internal_function() -> None:
    """The refusal of a `__` function names it and points at the SQL it stands for."""
    with pytest.raises(ValidationError) as caught:
        check_blacklist("SELECT __cast(1)")

    message = str(caught.value)
    assert "__cast" in message
    assert "internal to DuckDB" in message
    assert "CAST" in message.replace("__cast", "")


_COMPARISON_OPERATOR_NAMES = (
    "<",
    "<=",
    "=",
    ">",
    ">=",
    "!=",
    "<>",
    "IS DISTINCT FROM",
    "IS NOT DISTINCT FROM",
)
"""The comparison operators DuckDB 2.0 lists as functions (and `<>`, which it may list later)."""


@pytest.mark.parametrize("name", _COMPARISON_OPERATOR_NAMES)
@pytest.mark.parametrize(
    "template",
    [
        "SELECT \"{name}\"(id, 'a') FROM t",
        "SELECT main.\"{name}\"(id, 'a') FROM t",
        "SELECT system.main.\"{name}\"(id, 'a') FROM t",
        "SELECT id.\"{name}\"('a') FROM t",
        "SELECT * FROM t WHERE \"{name}\"(id, 'a')",
        'SELECT list_filter([1, 2], x -> "{name}"(x, 1))',
    ],
)
def test_blacklist_refuses_an_operator_called_by_name(name: str, template: str) -> None:
    """A comparison called as a function is refused, with the operator to write instead (#293).

    DuckDB 2.0 lists its comparison operators as functions; called by name with a text and a
    number, it fails its own type check with an internal error that leaves the process unusable.

    Parameters
    ----------
    name
        The operator's function name.
    template
        Where the call is written: bare, schema- or catalog-qualified, as a method, in a
        `WHERE` or in a lambda.
    """
    for spelling in (name, name.lower()):
        with pytest.raises(ValidationError) as caught:
            check_blacklist(template.format(name=spelling))

        message = str(caught.value)
        assert f'"{spelling}"' in message
        assert "id < 'a'" in message
        assert "write the operator itself" in message


@pytest.mark.parametrize("name", ["+", "||", "~~", "!__postfix", "->>", "@>"])
def test_blacklist_refuses_any_other_operator_called_by_name(name: str) -> None:
    """No query needs an operator by name, so every name that is not a word is refused.

    Parameters
    ----------
    name
        An operator's function name.
    """
    with pytest.raises(ValidationError, match="write the operator itself"):
        check_blacklist(f'SELECT "{name}"(1, 2)')


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT id < 'a', id <= 1, id = 1, id > 1, id >= 1, id != 1, id <> 1 FROM t",
        "SELECT a IS DISTINCT FROM b, a IS NOT DISTINCT FROM b FROM t",
        "SELECT * FROM t WHERE id < 3 AND name || 'x' = 'ax'",
        'SELECT "lower"(name), main.lower(name), "my_func2"(1) FROM t',
    ],
)
def test_blacklist_allows_operators_written_as_operators(sql: str) -> None:
    """Operator syntax and functions with ordinary names, quoted or not, stay allowed.

    Parameters
    ----------
    sql
        A query using operators or ordinary function names.
    """
    check_blacklist(sql)


@pytest.mark.parametrize("sql", ["SELECT 1 AS a__b", "SELECT my__func(1)", "SELECT x__ FROM t"])
def test_blacklist_allows_double_underscores_that_do_not_start_a_function_name(sql: str) -> None:
    """Only a function name that starts with `__` is internal; other names stay allowed.

    Parameters
    ----------
    sql
        A query with `__` inside a name.
    """
    check_blacklist(sql)


@pytest.mark.parametrize(
    "function_name",
    [
        "create_external_resource",
        "deregister_external_resource",
        "destroy_external_resource",
        "duckdb_available_metrics",
        "duckdb_dialects",
        "duckdb_eviction_queues",
        "duckdb_extension_repositories",
        "duckdb_external_resource_types",
        "duckdb_external_resources",
        "duckdb_grammar_extensions",
        "duckdb_secret_type_parameters",
        "duckdb_triggers",
        "python_arrow_scan",
        "read_single_csv_file",
        "read_single_json_file",
        "read_single_parquet_file",
        "register_external_resource",
        "register_external_resource_type",
    ],
)
def test_blacklist_refuses_every_table_function_new_in_duckdb_2(function_name: str) -> None:
    """The 18 table functions DuckDB 2.0.0 adds are outside the generator allow-list.

    Each is refused in `FROM` and inside a scalar subquery, by the allow-list rather than a
    list entry of its own.

    Parameters
    ----------
    function_name
        The table function the query calls.
    """
    for sql in (
        f"SELECT * FROM {function_name}('x')",
        f"SELECT (SELECT count(*) FROM {function_name}('x'))",
    ):
        with pytest.raises(ValidationError, match=f"table function {function_name} is not"):
            check_blacklist(sql)


@pytest.mark.parametrize(
    ("statement", "message"),
    [
        ("SELECT $v_0_files", "placeholders are not allowed"),
        ("SELECT getvariable('v_0_files')", "getvariable may not be called"),
        ("CONNECT other", "Only a single query"),
        ("CREATE EXTENSION REPOSITORY r AS 'https://example.com'", "CREATE is not permitted"),
        ("SELECT stats(id) FROM bronze.weather", "stats may not be called"),
    ],
)
def test_blacklist_refuses_the_new_duckdb_2_statements(statement: str, message: str) -> None:
    """Statements and functions DuckDB 2.0.0 adds or changes stay refused.

    Parameters
    ----------
    statement
        SQL a DuckDB 2.0.0 connection would accept or treat differently.
    message
        Text the refusal must contain.
    """
    with pytest.raises(ValidationError, match=message):
        check_blacklist(statement)


def test_blacklist_refuses_install_and_load_as_unparseable() -> None:
    """DuckDB 2.0.0's `INSTALL x AND LOAD` does not parse, and an unparseable query is refused."""
    with pytest.raises(sqlglot.errors.ParseError):
        check_blacklist("INSTALL httpfs AND LOAD")


_SECRET = '"/tmp/secret.csv"'
"""A quoted path DuckDB resolves to a real file by replacement scan.

Stands in for the file, URL, or catalog table a `WITH RECURSIVE` base term
reads while sqlglot believes the name is bound to the CTE being built.
"""

_RECURSIVE_BYPASS_SPELLINGS = {
    "union_all_base": (
        f"WITH RECURSIVE {_SECRET} AS ("
        f"SELECT val FROM {_SECRET} UNION ALL SELECT val FROM {_SECRET} WHERE FALSE"
        f") SELECT * FROM {_SECRET}"
    ),
    "union_distinct_base": (
        f"WITH RECURSIVE {_SECRET} AS ("
        f"SELECT val FROM {_SECRET} UNION SELECT val FROM {_SECRET} WHERE FALSE"
        f") SELECT * FROM {_SECRET}"
    ),
    "except_base": (
        f"WITH RECURSIVE {_SECRET} AS ("
        f"SELECT val FROM {_SECRET} EXCEPT SELECT val FROM {_SECRET} WHERE FALSE"
        f") SELECT * FROM {_SECRET}"
    ),
    "intersect_base": (
        f"WITH RECURSIVE {_SECRET} AS ("
        f"SELECT val FROM {_SECRET} INTERSECT SELECT val FROM {_SECRET} WHERE FALSE"
        f") SELECT * FROM {_SECRET}"
    ),
    "nested_set_operation_base": (
        f"WITH RECURSIVE {_SECRET} AS ("
        f"(SELECT val FROM {_SECRET} UNION ALL SELECT val FROM {_SECRET} WHERE FALSE) "
        f"UNION ALL SELECT val FROM {_SECRET} WHERE FALSE"
        f") SELECT * FROM {_SECRET}"
    ),
    "with_clause_inside_base": (
        f"WITH RECURSIVE {_SECRET} AS ("
        f"WITH leaked AS (SELECT val FROM {_SECRET}) SELECT val FROM leaked "
        f"UNION ALL SELECT val FROM {_SECRET} WHERE FALSE"
        f") SELECT * FROM {_SECRET}"
    ),
    "derived_table_inside_base": (
        f"WITH RECURSIVE {_SECRET} AS ("
        f"SELECT val FROM (SELECT val FROM {_SECRET}) AS d "
        f"UNION ALL SELECT val FROM {_SECRET} WHERE FALSE"
        f") SELECT * FROM {_SECRET}"
    ),
    "nested_in_a_subquery": (
        f"SELECT * FROM (WITH RECURSIVE {_SECRET} AS ("
        f"SELECT val FROM {_SECRET} UNION ALL SELECT val FROM {_SECRET} WHERE FALSE"
        f") SELECT * FROM {_SECRET}) AS q"
    ),
    "nested_in_an_in_predicate": (
        f"SELECT 1 WHERE 1 IN (WITH RECURSIVE {_SECRET} AS ("
        f"SELECT val FROM {_SECRET} UNION ALL SELECT val FROM {_SECRET} WHERE FALSE"
        f") SELECT * FROM {_SECRET})"
    ),
    "beside_a_legitimate_table": (
        f"WITH RECURSIVE {_SECRET} AS ("
        f"SELECT val FROM {_SECRET} UNION ALL SELECT val FROM {_SECRET} WHERE FALSE"
        f") SELECT * FROM {_SECRET}, bronze.weather"
    ),
}
"""Every reconstructed spelling of the base-term bypass, each rejected outright.

sqlglot's `optimizer/scope.py::_traverse_ctes` pre-seeds a recursive CTE's
own name into `Scope.cte_sources` *before* walking its body, and a set
operation inherits that binding into **both** branches. DuckDB only resolves
the name to the CTE in the recursive term; in the base term the same name
still resolves to a real relation — a file, an HTTP(S) URL, or a catalog
table. So every one of these read something real while `extract_table_refs`
reported nothing and no authorization call was made at all.
"""


@pytest.mark.parametrize("spelling", sorted(_RECURSIVE_BYPASS_SPELLINGS))
def test_blacklist_rejects_every_recursive_cte_bypass_spelling(spelling: str) -> None:
    """No spelling of the recursive-CTE base-term bypass survives the blacklist.

    Parameters
    ----------
    spelling
        Key into `_RECURSIVE_BYPASS_SPELLINGS` naming the shape under test.
    """
    with pytest.raises(ValidationError, match="Recursive CTEs"):
        check_blacklist(_RECURSIVE_BYPASS_SPELLINGS[spelling])


def test_blacklist_rejects_an_entirely_innocent_recursive_cte() -> None:
    """The feature is closed, not one malicious use of it.

    A counting query that touches no external relation at all is refused
    just the same: the rejection is structural, so there is no intent to
    judge and no second reading of DuckDB's binding rules to get wrong.
    """
    sql = (
        "WITH RECURSIVE r AS (SELECT 1 AS n UNION ALL SELECT n + 1 FROM r WHERE n < 3) "
        "SELECT n FROM r"
    )

    with pytest.raises(ValidationError, match="Recursive CTEs"):
        check_blacklist(sql)


def test_blacklist_allows_a_non_recursive_cte_named_like_a_recursive_one() -> None:
    """Only the `RECURSIVE` keyword is rejected, not CTEs or set operations.

    The flag read is `exp.With.args["recursive"]`, which is `True` only when
    the keyword was written; an ordinary `WITH` over a `UNION ALL` body is
    exactly the shape the bypass wore, and it must still run.
    """
    check_blacklist(
        "WITH recursive_totals AS (SELECT id FROM bronze.a UNION ALL SELECT id FROM bronze.b) "
        "SELECT id FROM recursive_totals"
    )


def test_extract_table_refs_rejects_a_recursive_cte() -> None:
    """Defence in depth: the classifier refuses what it cannot classify soundly.

    `check_blacklist` runs first in `lakana_worker.pipeline.run_one`, so this
    is unreachable through the real pipeline today. It is asserted anyway
    because the guarantee this function owes its caller is that every name
    it leaves out is genuinely not a table — and against a recursive CTE it
    cannot keep that promise.
    """
    with pytest.raises(ValidationError, match="Recursive CTEs"):
        extract_table_refs(_RECURSIVE_BYPASS_SPELLINGS["union_all_base"])


def test_rewrite_query_rejects_a_recursive_cte() -> None:
    """The rewriter refuses a recursive CTE independently of the other two checks."""
    with pytest.raises(ValidationError, match="Recursive CTEs"):
        rewrite_query(
            _RECURSIVE_BYPASS_SPELLINGS["union_all_base"],
            {"/tmp/secret.csv": _parquet("s3://bucket/part-0.parquet")},
        )


_CONNECT_BY_SPELLINGS = {
    "start_with": (
        "SELECT id FROM bronze.org START WITH parent IS NULL CONNECT BY PRIOR id = parent"
    ),
    "no_start_with": "SELECT id FROM bronze.org CONNECT BY PRIOR id = parent",
    "nocycle": "SELECT id FROM bronze.org CONNECT BY NOCYCLE PRIOR id = parent",
    "root_and_level": (
        "SELECT CONNECT_BY_ROOT id AS root, LEVEL FROM bronze.org CONNECT BY PRIOR id = parent"
    ),
    "level_only": "SELECT LEVEL FROM (SELECT 1) CONNECT BY LEVEL < 10",
    "root_without_connect_by": "SELECT CONNECT_BY_ROOT id AS root FROM bronze.org",
    "in_a_cte": (
        "WITH h AS (SELECT id FROM bronze.org CONNECT BY PRIOR id = parent) SELECT id FROM h"
    ),
    "in_a_subquery": (
        "SELECT * FROM (SELECT id FROM bronze.org CONNECT BY PRIOR id = parent) AS h"
    ),
    "in_an_in_predicate": (
        "SELECT 1 WHERE 1 IN (SELECT id FROM bronze.org CONNECT BY PRIOR id = parent)"
    ),
}
"""Hierarchical queries in Oracle's syntax, which sqlglot renders as `WITH RECURSIVE`.

sqlglot parses `CONNECT BY` to an `exp.Connect` on a plain `SELECT` and only writes the
recursive CTE when it renders the tree, so a check for the `RECURSIVE` keyword on the parsed
tree never sees one.
"""


@pytest.mark.parametrize("spelling", sorted(_CONNECT_BY_SPELLINGS))
def test_blacklist_refuses_every_connect_by_spelling(spelling: str) -> None:
    """A hierarchical query is refused like the recursive query it renders to.

    Parameters
    ----------
    spelling
        Key into `_CONNECT_BY_SPELLINGS` naming the shape under test.
    """
    with pytest.raises(ValidationError, match="CONNECT BY is not supported"):
        check_blacklist(_CONNECT_BY_SPELLINGS[spelling])


@pytest.mark.parametrize("spelling", ["start_with", "in_a_cte", "root_and_level"])
def test_the_classifier_and_the_rewrite_refuse_connect_by_on_their_own(spelling: str) -> None:
    """Defence in depth: a caller that skipped `check_blacklist` is refused all the same.

    Parameters
    ----------
    spelling
        Key into `_CONNECT_BY_SPELLINGS` naming the shape under test.
    """
    sql = _CONNECT_BY_SPELLINGS[spelling]

    with pytest.raises(ValidationError, match="CONNECT BY is not supported"):
        extract_table_refs(sql)
    with pytest.raises(ValidationError, match="CONNECT BY is not supported"):
        rewrite_query(sql, {"bronze.org": _parquet("s3://bucket/part-0.parquet")})


def test_a_views_stored_sql_cannot_use_connect_by() -> None:
    """A view's stored SQL is refused for `CONNECT BY` too, at the top and nested."""
    view = ViewDefinition(name="gold.v", sql=_CONNECT_BY_SPELLINGS["start_with"])
    outer = ViewDefinition(name="gold.wrapper", sql="SELECT id FROM gold.v")
    views = {"gold.v": view, "gold.wrapper": outer}

    for sql in ("SELECT * FROM gold.v", "SELECT * FROM gold.wrapper"):
        with pytest.raises(ValidationError, match="CONNECT BY is not supported"):
            expand_views(sql, views.get)


def test_a_column_or_table_named_like_a_connect_by_keyword_still_runs() -> None:
    """Only the clause is refused: a column named `level` or `prior` is an ordinary name."""
    check_blacklist("SELECT level, prior FROM bronze.connect_by WHERE level > 1")


def test_a_table_with_no_policy_still_gets_a_bare_select_star() -> None:
    """No row_filters/column_masks means today's SELECT * FROM reader($name), byte for byte."""
    source = TableSource(urls=["s3://bucket/a.parquet"], file_format="parquet")

    rewritten = rewrite_query("SELECT * FROM gold.customers", {"gold.customers": source})

    assert re.search(r"SELECT \* FROM " + _READER_CALL.pattern, rewritten.sql)


def test_a_column_mask_replaces_the_column_in_the_generated_cte() -> None:
    """A masked column's real value never appears in the generated SQL's projection list."""
    source = TableSource(
        urls=["s3://bucket/a.parquet"],
        file_format="parquet",
        columns={"id": "BIGINT", "ssn": "VARCHAR"},
        column_masks={"ssn": "NULL"},
    )

    rewritten = rewrite_query("SELECT * FROM gold.customers", {"gold.customers": source})

    assert "NULL AS" in rewritten.sql
    assert '"ssn"' in rewritten.sql
    # The outer query's own SELECT * (now reading the masked CTE's output,
    # not the raw file) is untouched and expected; what must never appear
    # is the generated CTE's own body still being a blind SELECT * against
    # the reader.
    assert "* FROM READ_PARQUET" not in rewritten.sql


def test_a_row_filter_adds_a_where_clause_to_the_generated_cte() -> None:
    """A row filter's predicate is ANDed into the generated CTE's own WHERE clause."""
    source = TableSource(
        urls=["s3://bucket/a.parquet"],
        file_format="parquet",
        columns={"id": "BIGINT", "region": "VARCHAR"},
        row_filters=["region = 'EU'"],
    )

    rewritten = rewrite_query("SELECT * FROM gold.customers", {"gold.customers": source})

    assert "WHERE (region = 'EU')" in rewritten.sql


def test_a_generated_statement_that_does_not_parse_names_no_policy_text() -> None:
    """N3: a stored filter that stops parsing fails the rewrite without quoting it."""
    source = TableSource(
        urls=["s3://bucket/a.parquet"],
        file_format="parquet",
        columns={"id": "BIGINT"},
        row_filters=["SECRETWORD ((( +"],
    )

    with pytest.raises(ValidationError, match="Ask an administrator") as info:
        rewrite_query("SELECT * FROM gold.customers", {"gold.customers": source})
    assert "SECRETWORD" not in str(info.value)


def test_multiple_row_filters_are_anded_together() -> None:
    """Two applicable row filters both appear, ANDed."""
    source = TableSource(
        urls=["s3://bucket/a.parquet"],
        file_format="parquet",
        columns={"id": "BIGINT", "region": "VARCHAR"},
        row_filters=["region = 'EU'", "id > 0"],
    )

    rewritten = rewrite_query("SELECT * FROM gold.customers", {"gold.customers": source})

    assert "WHERE (region = 'EU') AND (id > 0)" in rewritten.sql


def test_a_masked_column_survives_a_join_through_a_nested_cte() -> None:
    """Masking survives nesting: the case that made the rewriter wrap every base-table reference."""
    customers = TableSource(
        urls=["s3://bucket/customers.parquet"],
        file_format="parquet",
        columns={"id": "BIGINT", "name": "VARCHAR", "ssn": "VARCHAR", "region": "VARCHAR"},
        column_masks={"ssn": "NULL"},
    )
    orders = TableSource(urls=["s3://bucket/orders.parquet"], file_format="parquet")
    sql = (
        "WITH enriched AS ("
        "  SELECT o.id, o.amount, c.name, c.ssn, c.region"
        "  FROM gold.orders o JOIN gold.customers c ON o.customer_id = c.id"
        ") SELECT * FROM enriched WHERE region = 'EU'"
    )

    rewritten = rewrite_query(sql, {"gold.orders": orders, "gold.customers": customers})

    assert "NULL AS" in rewritten.sql
    assert '"ssn"' in rewritten.sql
    # The outer query's own structure — the enriched CTE, the join, the
    # final WHERE — is untouched; only the customers CTE's own body changed.
    assert "WHERE region = 'EU'" in rewritten.sql


def test_a_policy_leaves_the_variables_unaffected() -> None:
    """A policy changes the generated CTE's body, never the variables set for it."""
    source = TableSource(
        urls=["s3://bucket/a.parquet"],
        file_format="parquet",
        columns={"id": "BIGINT", "ssn": "VARCHAR"},
        row_filters=["id > 0"],
        column_masks={"ssn": "NULL"},
    )

    rewritten = rewrite_query("SELECT * FROM gold.customers", {"gold.customers": source})

    assert list(rewritten.parameters.values()) == [["s3://bucket/a.parquet"]]
    assert rewritten.sql.count("GETVARIABLE") == 1
    assert "?" not in rewritten.sql


def test_a_column_mask_on_an_unknown_column_raises() -> None:
    """A mask keyed to a column absent from the schema fails closed, not silently unmasked.

    Without this check, a typo or case mismatch in a `ColumnMask.column`
    value (`ColumnMaskService.create` does no schema validation) would
    silently select the real, unmasked value with no diagnostic anywhere.
    """
    source = TableSource(
        urls=["s3://bucket/a.parquet"],
        file_format="parquet",
        columns={"id": "BIGINT", "ssn": "VARCHAR"},
        column_masks={"SSN": "NULL"},
    )

    with pytest.raises(ValidationError, match="unknown column 'SSN'"):
        rewrite_query("SELECT * FROM gold.customers", {"gold.customers": source})


def test_a_policy_on_a_table_with_no_declared_columns_raises() -> None:
    """Without a declared column list there is nothing to build an explicit projection from.

    Falling through silently would render `SELECT  FROM read_parquet($name)` —
    syntactically invalid SQL DuckDB would reject with an opaque parser
    error far from the real cause.
    """
    source = TableSource(
        urls=["s3://bucket/a.parquet"], file_format="parquet", row_filters=["id > 0"]
    )

    with pytest.raises(ValidationError, match="no declared column list"):
        rewrite_query("SELECT * FROM gold.customers", {"gold.customers": source})


def test_cte_alias_distinguishes_names_that_sanitize_identically() -> None:
    """`a.b_c` and `a_b.c` sanitize to the same text but get different aliases."""
    assert _cte_alias("a.b_c") != _cte_alias("a_b.c")
    assert _cte_alias("a.b_c").startswith("__lakana_a_b_c_")
    assert _cte_alias("a.b_c") == _cte_alias("a.b_c")


def test_rewrite_refuses_an_alias_claimed_by_two_tables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the derivation ever collides again, the rewrite fails loudly instead of reusing."""
    import lakana_core.query_rewrite as query_rewrite

    monkeypatch.setattr(query_rewrite, "_cte_alias", lambda dotted: "__lakana_same")
    with pytest.raises(ValidationError, match="__lakana_same"):
        rewrite_query(
            "SELECT * FROM a.b UNION ALL SELECT * FROM c.d",
            sources={"a.b": _parquet("x.parquet"), "c.d": _parquet("y.parquet")},
        )


def test_extract_table_refs_skips_table_functions() -> None:
    """A table function is not a table reference; a real table beside it still is."""
    sql = "SELECT * FROM bronze.x AS b, range(3) AS r(i), generate_series(1, 2) AS g(j)"
    assert extract_table_refs(sql) == ["bronze.x"]


@pytest.mark.parametrize("sql", ["SELECT * FROM range(3)", "SELECT * FROM unnest([1])"])
def test_blacklist_allows_generator_table_functions(sql: str) -> None:
    """Pure generators read nothing, so the guard lets them through."""
    check_blacklist(sql)


@pytest.mark.parametrize(
    "function", ["read_parquet", "read_csv_auto", "iceberg_scan", "postgres_scan", "mystery"]
)
def test_blacklist_refuses_other_table_functions(function: str) -> None:
    """Every table function outside the generator allow-list is refused with guidance."""
    with pytest.raises(ValidationError, match=f"table function {function} is not allowed"):
        check_blacklist(f"SELECT * FROM {function}('x')")


_NAMED_READERS = [
    # Parquet, Avro, Delta and Iceberg metadata: each reads the file or the table at a path.
    "parquet_metadata",
    "parquet_schema",
    "parquet_file_metadata",
    "parquet_kv_metadata",
    "parquet_full_metadata",
    "parquet_bloom_probe",
    "avro_metadata",
    "delta_domain_metadata",
    "delta_get_transaction_version",
    "delta_list_files",
    "iceberg_column_stats",
    "iceberg_files",
    "iceberg_history",
    "iceberg_load_table_response",
    "iceberg_manifests",
    "iceberg_metadata",
    "iceberg_partition_specs",
    "iceberg_partition_stats",
    "iceberg_refs",
    "iceberg_snapshots",
    # Another database.
    "sqlite_attach",
    "sqlite_query",
    "postgres_query",
    "postgres_execute",
    "mysql_query",
    "mysql_execute",
    # Already named before #487.
    "glob",
    "sniff_csv",
]
"""DuckDB 2.0.0's table functions that read a file, a URL or another database by a name that
neither starts with ``read_`` nor ends with ``_scan`` (``duckdb_functions()``, with the parquet,
json, httpfs, iceberg, delta, sqlite, postgres, mysql, avro and ducklake extensions loaded)."""


@pytest.mark.parametrize("function", _NAMED_READERS)
def test_a_reader_by_another_name_is_refused_as_a_reader(function: str) -> None:
    """The refusal names the cause, not a typo (#487); the allow-list is unchanged.

    Parameters
    ----------
    function
        The table function the query calls.
    """
    with pytest.raises(ValidationError, match=f"table function {function} is not allowed") as info:
        check_blacklist(f"SELECT * FROM {function}('x')")
    assert "it reads data directly" in str(info.value)
    assert "check the name" not in str(info.value)


@pytest.mark.parametrize("function", ["parquet_metadat", "iceberg_snapshot", "mystery"])
def test_a_misspelt_name_is_asked_to_be_checked(function: str) -> None:
    """A name that is not a known reader keeps the line that asks to check it.

    Parameters
    ----------
    function
        A name DuckDB does not have.
    """
    with pytest.raises(ValidationError, match="check the name"):
        check_blacklist(f"SELECT * FROM {function}('x')")


_LATERAL_BYPASSES = [
    ("SELECT * FROM (SELECT 1 a) b, LATERAL iceberg_scan('x')", "iceberg_scan"),
    ("SELECT * FROM (SELECT 1 a) b, LATERAL delta_scan('x')", "delta_scan"),
    ("SELECT * FROM (SELECT 1 a) b, LATERAL sqlite_scan('x', 't')", "sqlite_scan"),
    ("SELECT * FROM (SELECT 1 a) b JOIN LATERAL duckdb_types() ON true", "duckdb_types"),
    ("SELECT * FROM (SELECT 1 a) b, LATERAL duckdb_functions()", "duckdb_functions"),
    ("SELECT * FROM (SELECT 1 a) b, LATERAL test_all_types()", "test_all_types"),
    ("SELECT * FROM (SELECT 1 a) b, LATERAL json_each('[1]')", "json_each"),
    ("SELECT * FROM (SELECT 1 a) b, LATERAL repeat_row(1, 2)", "repeat_row"),
    ("SELECT * FROM table(iceberg_scan('x'))", "iceberg_scan"),
    ("SELECT * FROM table(mystery('x'))", "mystery"),
    ("SELECT * FROM table(1)", "table"),
]


@pytest.mark.parametrize(("sql", "function"), _LATERAL_BYPASSES)
def test_blacklist_refuses_table_functions_in_any_from_position(sql: str, function: str) -> None:
    """`LATERAL fn(...)` and `TABLE(fn(...))` are held to the same allow-list as `FROM fn(...)`."""
    with pytest.raises(ValidationError, match=f"table function {function} is not allowed"):
        check_blacklist(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM (SELECT 1 a) b, LATERAL range(3)",
        "SELECT * FROM (SELECT [1] a) b, LATERAL unnest(b.a)",
        "SELECT * FROM (SELECT 1 a) b JOIN LATERAL generate_series(1, 2) ON true",
    ],
)
def test_lateral_generators_stay_allowed_and_are_no_references(sql: str) -> None:
    """A lateral generator is allowed and contributes no table reference."""
    check_blacklist(sql)
    assert extract_table_refs(sql) == []


def test_extract_table_refs_never_returns_an_empty_name() -> None:
    """`ROWS FROM (...)` yields a nameless table node that must not become a reference."""
    assert extract_table_refs("SELECT * FROM ROWS FROM (range(3))") == []


def test_blacklist_refuses_rows_from() -> None:
    """`ROWS FROM (...)` is refused rather than half-understood."""
    with pytest.raises(ValidationError, match="table function rows from is not allowed"):
        check_blacklist("SELECT * FROM ROWS FROM (iceberg_scan('x'))")


@pytest.mark.parametrize(
    "function",
    [
        "iceberg_scan",
        "delta_scan",
        "sqlite_scan",
        "postgres_scan",
        "mysql_scan",
        "postgres_query",
        "mysql_query",
        "sqlite_attach",
    ],
)
def test_foreign_scanners_are_blacklisted_by_name_in_any_position(function: str) -> None:
    """Foreign scanners are refused even as a scalar-position call."""
    with pytest.raises(ValidationError, match=function):
        check_blacklist(f"SELECT {function}('x')")


_QUALIFIED_LATERAL = [
    ("SELECT * FROM (SELECT 1 a) b, LATERAL main.duckdb_types()", "duckdb_types"),
    ("SELECT * FROM (SELECT 1 a) b, LATERAL system.main.duckdb_functions()", "duckdb_functions"),
    ('SELECT * FROM (SELECT 1 a) b, LATERAL "main"."duckdb_types"()', "duckdb_types"),
    ("SELECT * FROM (SELECT 1 a) b, LATERAL main.test_all_types()", "test_all_types"),
    ("SELECT * FROM (SELECT 1 a) b, LATERAL main.json_each('[1]')", "json_each"),
    (
        "SELECT * FROM (SELECT 1 a) b, LATERAL "
        "(SELECT * FROM (SELECT 2 c) d, LATERAL duckdb_types()) s",
        "duckdb_types",
    ),
]


@pytest.mark.parametrize(("sql", "function"), _QUALIFIED_LATERAL)
def test_blacklist_refuses_schema_qualified_and_nested_lateral_functions(
    sql: str, function: str
) -> None:
    """A schema-qualified or nested `LATERAL fn()` meets the same allow-list."""
    with pytest.raises(ValidationError, match=f"table function {function} is not allowed"):
        check_blacklist(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM (SELECT 1 a) b, LATERAL main.range(3)",
        "SELECT * FROM (SELECT 1 a) b, LATERAL range(3) AS r(i)",
        "SELECT * FROM (SELECT 1 a) b, LATERAL (SELECT 2 c) s",
    ],
)
def test_qualified_lateral_generators_and_subqueries_stay_allowed(sql: str) -> None:
    """Generators (qualified or aliased) and lateral subqueries are allowed, no references."""
    check_blacklist(sql)
    assert extract_table_refs(sql) == []


def test_an_unrecognised_lateral_shape_is_refused() -> None:
    """A `LATERAL` that is neither a subquery nor a function call fails closed."""
    node = sqlglot.parse_one("SELECT * FROM t, LATERAL (SELECT 1) s", dialect="duckdb").find(
        exp.Lateral
    )
    assert node is not None
    node.set("this", exp.column("x"))
    assert query_rewrite._from_item_function_name(node) == "lateral"


def test_a_source_that_lists_only_its_declared_columns_never_reads_with_a_star() -> None:
    """Without any policy the files are still read through the explicit column list.

    A read at an older snapshot must not show a column that its files have and the source
    does not declare; ``SELECT *`` over the files would.
    """
    source = TableSource(
        urls=["s3://b/f.parquet"],
        file_format="parquet",
        columns={"id": "long", "name": "string"},
        declared_columns_only=True,
    )

    rewritten = rewrite_query("SELECT * FROM gold.t", {"gold.t": source})

    assert 'SELECT "id", "name" FROM READ_PARQUET(' in rewritten.sql
    assert "* FROM READ_PARQUET" not in rewritten.sql
    assert " WHERE " not in rewritten.sql
    plain = TableSource(urls=["s3://b/f.parquet"], file_format="parquet", columns={"id": "long"})
    assert "* FROM READ_PARQUET" in rewrite_query("SELECT * FROM gold.t", {"gold.t": plain}).sql


def test_a_source_that_lists_only_its_declared_columns_needs_some() -> None:
    """No column to list is refused, never rendered as an empty select list."""
    source = TableSource(
        urls=["s3://b/f.parquet"], file_format="parquet", declared_columns_only=True
    )

    with pytest.raises(ValidationError, match="no declared column list"):
        rewrite_query("SELECT * FROM gold.t", {"gold.t": source})


def test_named_columns_counts_the_column_of_a_json_arrow_argument() -> None:
    """`upper(j -> 'a')` names the column `j`, as DuckDB reads it; a lambda's parameter is none."""
    named = query_rewrite.named_columns(
        "SELECT upper(j -> 'a'), list_transform(l, x -> x) FROM bronze.t"
    )

    assert named == frozenset({"j", "l"})


def test_named_columns_of_a_statement_that_does_not_parse_is_empty() -> None:
    """A statement sqlglot cannot parse names no column, so no error is blamed on the reader."""
    assert query_rewrite.named_columns("SELECT tier FROM (") == frozenset()


_NOT_A_READ = [
    pytest.param("DELETE FROM bronze.t RETURNING *", "DELETE", id="delete"),
    pytest.param("dElEtE fRoM bronze.t ReTuRnInG *", "DELETE", id="delete-mixed-case"),
    pytest.param("INSERT INTO bronze.t VALUES (1) RETURNING *", "INSERT", id="insert"),
    pytest.param(
        "INSERT OR REPLACE INTO bronze.t VALUES (1) RETURNING *", "INSERT", id="insert-or-replace"
    ),
    pytest.param("UPDATE bronze.t SET a = 1 RETURNING *", "UPDATE", id="update"),
    pytest.param(
        "MERGE INTO bronze.t USING bronze.s ON t.a = s.a WHEN MATCHED THEN DELETE",
        "MERGE",
        id="merge",
    ),
    pytest.param("CREATE TABLE x AS SELECT 1", "CREATE", id="create"),
    pytest.param("DROP TABLE x", "DROP", id="drop"),
    pytest.param("ATTACH ':memory:' AS x", "ATTACH", id="attach"),
    pytest.param("DETACH x", "DETACH", id="detach"),
    pytest.param("USE x", "USE", id="use"),
    pytest.param("PRAGMA version", "PRAGMA", id="pragma"),
    pytest.param("BEGIN TRANSACTION", "BEGIN", id="transaction"),
    pytest.param("INSTALL httpfs", "INSTALL", id="install"),
    pytest.param("UPDATE EXTENSIONS", "UPDATE", id="update-extensions"),
    pytest.param("COMMENT ON TABLE bronze.t IS 'x'", "COMMENT", id="comment"),
    pytest.param("DESCRIBE bronze.t", "DESCRIBE", id="describe"),
    pytest.param("SHOW TABLES", "SHOW", id="show"),
    pytest.param("SUMMARIZE SELECT 1", "SUMMARIZE", id="summarize"),
    pytest.param("SELECT 1 AS a INTO bronze.t2", "INTO", id="select-into"),
]
"""Statements that are not a read, each with the keyword its refusal names."""

_STATEMENT_POSITIONS = [
    pytest.param("WITH m AS ({s}) SELECT * FROM m", id="cte"),
    pytest.param("WITH m AS MATERIALIZED ({s}) SELECT * FROM m", id="materialized-cte"),
    pytest.param("WITH ok AS (SELECT 1 AS a), m AS ({s}) SELECT * FROM ok", id="second-cte"),
    pytest.param("WITH a AS (WITH b AS ({s}) SELECT * FROM b) SELECT * FROM a", id="nested-cte"),
    pytest.param("SELECT * FROM (WITH b AS ({s}) SELECT * FROM b) AS q", id="cte-in-subquery"),
    pytest.param(
        "SELECT 1 AS a UNION ALL (WITH b AS ({s}) SELECT * FROM b)", id="cte-in-set-operation"
    ),
    pytest.param(
        "SELECT * FROM bronze.x, LATERAL (WITH b AS ({s}) SELECT * FROM b) AS l",
        id="cte-in-lateral",
    ),
    pytest.param("VALUES ((WITH b AS ({s}) SELECT count(*) FROM b))", id="cte-in-bare-values"),
    pytest.param(
        "VALUES (1), ((SELECT count(*) FROM (WITH b AS ({s}) SELECT * FROM b) AS q))",
        id="cte-in-subquery-in-bare-values",
    ),
]
"""Every place sqlglot's duckdb dialect parses a statement inside a query (verified 30.22)."""


@pytest.mark.parametrize("position", _STATEMENT_POSITIONS)
@pytest.mark.parametrize(("statement", "keyword"), _NOT_A_READ)
def test_a_statement_that_is_not_a_read_is_refused_wherever_it_sits(
    statement: str, keyword: str, position: str
) -> None:
    """DuckDB 2 runs DML inside a CTE and reports the whole text as a SELECT.

    Parameters
    ----------
    statement
        The statement that is not a read.
    keyword
        The keyword the refusal names.
    position
        Where in the query the statement sits.
    """
    with pytest.raises(ValidationError, match=keyword):
        check_blacklist(position.format(s=statement))


def test_dml_inside_a_cte_is_refused_with_the_reason() -> None:
    """The message says what a query may do and that depth does not matter."""
    with pytest.raises(ValidationError) as caught:
        check_blacklist("WITH m AS (DELETE FROM bronze.t RETURNING *) SELECT * FROM m")

    assert str(caught.value) == (
        "A query reads data and cannot change it: `DELETE` is not allowed, also not inside a "
        "CTE or subquery."
    )


@pytest.mark.parametrize(
    ("sql", "keyword"),
    [
        ("WITH m AS (CHECKPOINT) SELECT 1", "CHECKPOINT"),
        ("WITH m AS (VACUUM) SELECT 1", "VACUUM"),
    ],
)
def test_a_cte_body_sqlglot_reads_as_something_else_is_refused(sql: str, keyword: str) -> None:
    """The parser reads `CHECKPOINT` as a column and `VACUUM` as a command; neither is a query.

    Parameters
    ----------
    sql
        A CTE whose body is a statement sqlglot has no class for.
    keyword
        The keyword the refusal names.
    """
    with pytest.raises(ValidationError, match=f"`{keyword}` is not allowed"):
        check_blacklist(sql)


def test_the_rewrite_refuses_dml_inside_a_cte_on_its_own() -> None:
    """Defence in depth: a caller that skipped `check_blacklist` is refused all the same."""
    with pytest.raises(ValidationError, match="`DELETE` is not allowed"):
        rewrite_query(
            "WITH m AS (DELETE FROM gold.sales RETURNING *) SELECT * FROM m",
            sources={"gold.sales": _parquet("a.parquet")},
        )
    with pytest.raises(ValidationError, match="`DELETE` is not allowed"):
        extract_table_refs("WITH m AS (DELETE FROM gold.sales RETURNING *) SELECT * FROM m")


@pytest.mark.parametrize(
    "sql",
    [
        "WITH m AS (VALUES (1)) SELECT * FROM m",
        "WITH m AS (PIVOT bronze.t ON a IN ('x') USING sum(b)) SELECT * FROM m",
        "WITH m AS (FROM bronze.t) SELECT * FROM m",
        "SELECT * FROM (FROM bronze.t SELECT a) AS s",
        "SELECT * FROM (bronze.v JOIN bronze.x ON v.a = x.a)",
        "SELECT 'DELETE FROM t' AS s",
        'SELECT 1 AS "delete"',
        "SELECT update FROM (SELECT 1 AS update)",
    ],
)
def test_a_read_that_merely_names_a_statement_keyword_passes(sql: str) -> None:
    """Over-refusal guard: reads in DuckDB's own spellings stay allowed.

    Parameters
    ----------
    sql
        A read.
    """
    check_blacklist(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT getvariable('v_0_files') AS u",
        "SELECT GETVARIABLE('v_0_files') AS u",
        "SELECT \"getvariable\"('v_0_files') AS u",
        "SELECT main.getvariable('v_0_files') AS u",
        "SELECT system.main.getvariable('v_0_files') AS u",
        "SELECT list_transform([1], lambda x: getvariable('v_0_files')) AS u",
        "SELECT id FROM bronze.t WHERE getvariable('v_0_files') IS NOT NULL",
        "WITH c AS (SELECT getvariable('v_0_schema') AS s) SELECT * FROM c",
    ],
)
def test_user_sql_cannot_read_a_variable_of_the_connection(sql: str) -> None:
    """The query path sets values as DuckDB variables under predictable names.

    Parameters
    ----------
    sql
        A query that reads a variable.
    """
    with pytest.raises(ValidationError, match="getvariable may not be called directly"):
        check_blacklist(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT json_serialize_plan('SELECT getvariable(''v_0_files'')') AS p",
        "SELECT JSON_SERIALIZE_PLAN('SELECT 1') AS p",
        "SELECT main.json_serialize_plan('SELECT 1') AS p",
        "SELECT id FROM bronze.t WHERE json_serialize_plan('SELECT 1') IS NOT NULL",
        "SELECT list_transform([1], lambda x: json_serialize_plan('SELECT 1')) AS p",
        "WITH c AS (SELECT json_serialize_plan('SELECT 1') AS p) SELECT * FROM c",
    ],
)
def test_user_sql_cannot_plan_sql_given_as_a_value(sql: str) -> None:
    """A scalar that plans the SQL text it is given is refused wherever a scalar may sit.

    Parameters
    ----------
    sql
        A query that calls the scalar.
    """
    with pytest.raises(ValidationError, match="json_serialize_plan may not be called directly"):
        check_blacklist(sql)


@pytest.mark.parametrize("function_name", ["postgres_execute", "mysql_execute"])
@pytest.mark.parametrize(
    "template",
    [
        "SELECT {f}('db', 'SELECT 1') AS v",
        "SELECT id FROM bronze.t WHERE {f}('db', 'SELECT 1') IS NOT NULL",
        "SELECT * FROM {f}('db', 'SELECT 1')",
    ],
)
def test_user_sql_cannot_call_an_extensions_sql_executor(function_name: str, template: str) -> None:
    """An extension function that runs SQL on another database is refused in every position.

    Parameters
    ----------
    function_name
        The function, from an extension the query process never loads.
    template
        A query with a placeholder for the function's name.
    """
    with pytest.raises(ValidationError, match=function_name):
        check_blacklist(template.format(f=function_name))


@pytest.mark.parametrize(
    ("sql", "message"),
    [
        ("SET VARIABLE v_0_files = ['x']", "not SET"),
        ("RESET VARIABLE v_0_files", "Only a single query"),
    ],
)
def test_user_sql_cannot_set_a_variable_of_the_connection(sql: str, message: str) -> None:
    """Pin: a variable can be neither set nor reset by a submitted statement.

    Parameters
    ----------
    sql
        A statement that changes a variable.
    message
        Text the refusal must contain.
    """
    with pytest.raises(ValidationError, match=message):
        check_blacklist(sql)


def test_a_views_stored_sql_cannot_read_a_variable_either() -> None:
    """A view expands into the reader's query, so its stored SQL is checked the same way."""
    view = ViewDefinition(name="gold.v", sql="SELECT getvariable('v_0_files') AS u")

    with pytest.raises(ValidationError, match="getvariable may not be called directly"):
        expand_views("SELECT * FROM gold.v", lambda dotted: view if dotted == "gold.v" else None)


def test_select_into_is_refused_at_the_top_level() -> None:
    """The rewrite would turn `SELECT ... INTO` into `CREATE TABLE ... AS`, so it is refused."""
    with pytest.raises(ValidationError, match="`INTO` is not allowed"):
        check_blacklist("SELECT x.a INTO main.u FROM bronze.t AS x")


def test_a_statement_with_its_own_with_clause_is_named_by_its_keyword() -> None:
    """The message names `DELETE`, not the `WITH` the statement starts with."""
    sql = "WITH m AS (WITH x AS (SELECT 1 AS a) DELETE FROM bronze.t RETURNING *) SELECT * FROM m"

    with pytest.raises(ValidationError, match="`DELETE` is not allowed"):
        check_blacklist(sql)


def test_rewrite_reads_each_file_list_through_a_variable_of_its_own() -> None:
    """Two reads, two variables, numbered in the order the CTEs are generated; no placeholder."""
    rewritten = rewrite_query(
        "SELECT a.id FROM bronze.a AS a JOIN gold.b AS b ON a.id = b.id",
        sources={"bronze.a": _parquet("a.parquet"), "gold.b": _parquet("b.parquet")},
    )

    assert "READ_PARQUET(GETVARIABLE('v_0_files'))" in rewritten.sql
    assert "READ_PARQUET(GETVARIABLE('v_1_files'))" in rewritten.sql
    assert rewritten.parameters == {"v_0_files": ["a.parquet"], "v_1_files": ["b.parquet"]}
    assert "$" not in rewritten.sql
    assert "?" not in rewritten.sql


def test_a_table_without_files_takes_no_variable_number() -> None:
    """An empty relation reads nothing, so the next read is still `v_1_files`."""
    rewritten = rewrite_query(
        "SELECT a.id FROM bronze.a AS a, gold.empty AS e, gold.c AS c",
        sources={
            "bronze.a": _parquet("a.parquet"),
            "gold.empty": TableSource(urls=[], file_format="parquet", columns={"id": "long"}),
            "gold.c": TableSource(urls=["c.csv"], file_format="csv"),
        },
    )

    assert rewritten.parameters == {"v_0_files": ["a.parquet"], "v_1_files": ["c.csv"]}
    assert "READ_CSV(GETVARIABLE('v_1_files'))" in rewritten.sql


_FIELDS = (ReadField(1, "id", "BIGINT"), ReadField(2, "name", "VARCHAR"))


def test_an_iceberg_read_reads_its_files_by_field_id() -> None:
    """The map is a second variable of the same read, never SQL text."""
    source = TableSource(
        urls=["a.parquet"],
        file_format="parquet",
        columns={"id": "long", "name": "string"},
        field_ids=_FIELDS,
    )

    rewritten = rewrite_query("SELECT * FROM gold.t", {"gold.t": source})

    assert (
        "READ_PARQUET(GETVARIABLE('v_0_files'), schema = GETVARIABLE('v_0_schema'))"
        in rewritten.sql
    )
    assert rewritten.parameters == {"v_0_files": ["a.parquet"], "v_0_schema": _FIELDS}


def test_a_projected_read_by_field_id_keeps_its_policy() -> None:
    """Filters and masks still apply in the CTE over the map's columns."""
    source = TableSource(
        urls=["a.parquet"],
        file_format="parquet",
        columns={"id": "long", "name": "string"},
        row_filters=("id > 1",),
        column_masks={"name": "NULL"},
        field_ids=_FIELDS,
    )

    rewritten = rewrite_query("SELECT * FROM gold.t", {"gold.t": source})

    assert (
        'SELECT "id", NULL AS "name" FROM READ_PARQUET(GETVARIABLE(\'v_0_files\'), '
        "schema = GETVARIABLE('v_0_schema')) WHERE (id > 1)"
    ) in rewritten.sql


def test_a_read_by_field_id_of_files_that_are_not_parquet_is_refused() -> None:
    """Field ids belong to Parquet; any other format cannot be read by them."""
    source = TableSource(
        urls=["a.orc"],
        file_format="orc",
        columns={"id": "long", "name": "string"},
        field_ids=_FIELDS,
    )

    with pytest.raises(ValidationError, match="A read by field id needs Parquet files"):
        rewrite_query("SELECT * FROM gold.t", {"gold.t": source})


def test_a_field_id_map_that_misses_a_declared_column_is_refused() -> None:
    """A column outside the map would not be read at all; that is a defect, refused."""
    source = TableSource(
        urls=["a.parquet"],
        file_format="parquet",
        columns={"id": "long", "name": "string"},
        field_ids=_FIELDS[:1],
    )

    with pytest.raises(ValidationError, match="has no field id for its column 'name'"):
        rewrite_query("SELECT * FROM gold.t", {"gold.t": source})


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        pytest.param(
            (*_FIELDS, ReadField(3, "extra", "BIGINT")),
            "names the column 'extra', which the table does not declare",
            id="a name the table does not declare",
        ),
        pytest.param(
            (ReadField(1, "id", "BIGINT"), ReadField(1, "name", "VARCHAR")),
            "field id 1 twice",
            id="a field id twice",
        ),
        pytest.param(
            (
                ReadField(1, "id", "BIGINT"),
                ReadField(2, "id", "VARCHAR"),
                ReadField(3, "name", "X"),
            ),
            "column 'id' twice",
            id="a name twice",
        ),
    ],
)
def test_a_field_id_map_that_is_not_the_declared_columns_is_refused(
    fields: tuple[ReadField, ...], message: str
) -> None:
    source = TableSource(
        urls=["a.parquet"],
        file_format="parquet",
        columns={"id": "long", "name": "string"},
        field_ids=fields,
    )

    with pytest.raises(ValidationError, match=message):
        rewrite_query("SELECT * FROM gold.t", {"gold.t": source})


def test_only_a_type_checked_mask_is_cast_to_the_columns_structured_type() -> None:
    """The cast is built from the structured type given; an unchecked mask is spelled as stored."""
    target = exp.DataType(this=exp.DType.BIGINT)
    columns = {"id": "BIGINT", "ssn": "VARCHAR"}
    masks = {"id": "0", "ssn": "'***'"}
    checked = TableSource(
        urls=["a"],
        file_format="parquet",
        columns=columns,
        column_masks=masks,
        mask_types={"id": target},
        typed_masks=frozenset({"id", "ssn"}),
    )
    legacy = TableSource(
        urls=["a"],
        file_format="parquet",
        columns=columns,
        column_masks=masks,
        mask_types={"id": target},
    )

    assert (
        'TRY_CAST(0 AS BIGINT) AS "id", \'***\' AS "ssn"'
        in rewrite_query("SELECT * FROM gold.t", {"gold.t": checked}).sql
    )
    assert (
        '0 AS "id", \'***\' AS "ssn"'
        in rewrite_query("SELECT * FROM gold.t", {"gold.t": legacy}).sql
    )


_BARE_VALUES = [
    pytest.param("VALUES (1)", id="one-row"),
    pytest.param("VALUES (1, 'a'), (2, 'b')", id="two-rows"),
    pytest.param("values (2), (1) order by 1 limit 1 offset 0", id="ordered-and-limited"),
]
"""Queries that are only a `VALUES` list."""


@pytest.mark.parametrize("sql", _BARE_VALUES)
def test_a_query_that_is_only_a_values_list_is_a_read(sql: str) -> None:
    """A bare `VALUES` reads nothing and changes nothing, so the guard lets it through.

    Parameters
    ----------
    sql
        A bare `VALUES` list.
    """
    check_blacklist(sql)

    assert extract_table_refs(sql) == []


def test_the_rewrite_turns_a_bare_values_list_into_a_select_of_it() -> None:
    """Every later step sees the `SELECT` it already handles; the order and limit stay outside."""
    rewritten = rewrite_query("VALUES (2), (1) ORDER BY 1 LIMIT 1 OFFSET 0", {})

    assert rewritten.sql == (
        "SELECT * FROM (VALUES (2), (1)) AS __lakana_values ORDER BY 1 LIMIT 1 OFFSET 0"
    )
    assert rewritten.parameters == {}


_IN_A_VALUES_LIST = [
    pytest.param("(SELECT * FROM read_csv('x'))", id="file-reading-table-function"),
    pytest.param("(SELECT * FROM table(mystery('x')))", id="unknown-table-function"),
    pytest.param("(SELECT * FROM (SELECT 1 AS a) AS b, LATERAL iceberg_scan('x'))", id="lateral"),
    pytest.param("read_csv('x')", id="file-reading-function"),
    pytest.param("getvariable('v_0_files')", id="getvariable"),
    pytest.param("main.getvariable('v_0_files')", id="qualified-getvariable"),
    pytest.param("query('SELECT 1')", id="query"),
    pytest.param("current_query()", id="current-query"),
    pytest.param("json_execute_serialized_sql('x')", id="serialized-sql"),
    pytest.param("json_serialize_plan('SELECT getvariable(''v_0_files'')')", id="serialize-plan"),
    pytest.param("?", id="placeholder"),
    pytest.param("$name", id="named-placeholder"),
    pytest.param(
        "(WITH m AS (DELETE FROM bronze.t RETURNING *) SELECT count(*) FROM m)",
        id="statement-in-a-cte",
    ),
    pytest.param("(WITH m AS (CHECKPOINT) SELECT 1)", id="cte-body-not-a-query"),
    pytest.param(
        "(WITH RECURSIVE r AS (SELECT 1 AS n UNION ALL SELECT n + 1 FROM r WHERE n < 3) "
        "SELECT max(n) FROM r)",
        id="recursive-cte",
    ),
]
"""Expressions the guard refuses inside a `SELECT` list, each to be put in a bare `VALUES`."""


def _refusal(sql: str) -> str:
    """Say why `check_blacklist` refuses a query.

    Parameters
    ----------
    sql
        A query the guard must refuse.

    Returns
    -------
    str
        The refusal's message.
    """
    with pytest.raises(ValidationError) as caught:
        check_blacklist(sql)
    return str(caught.value)


@pytest.mark.parametrize("expression", _IN_A_VALUES_LIST)
def test_a_bare_values_list_is_refused_for_what_a_select_is_refused_for(expression: str) -> None:
    """`VALUES` holds expressions; each is checked as it is in a `SELECT` list, message and all.

    Parameters
    ----------
    expression
        An expression the guard refuses in a `SELECT` list.
    """
    in_a_select = _refusal(f"SELECT {expression} AS a")

    assert _refusal(f"VALUES ({expression})") == in_a_select
    assert _refusal(f"VALUES (1), (2, {expression})") == in_a_select


@pytest.mark.parametrize(
    "expression",
    [
        "?",
        "(WITH m AS (DELETE FROM bronze.t RETURNING *) SELECT count(*) FROM m)",
        "(WITH RECURSIVE r AS (SELECT 1 AS n UNION ALL SELECT n FROM r) SELECT max(n) FROM r)",
    ],
)
def test_the_rewrite_refuses_a_bare_values_list_on_its_own(expression: str) -> None:
    """Defence in depth: a caller that skipped `check_blacklist` is refused all the same.

    Parameters
    ----------
    expression
        An expression `rewrite_query` and `extract_table_refs` refuse themselves.
    """
    sql = f"VALUES ({expression})"
    with pytest.raises(ValidationError):
        extract_table_refs(sql)
    with pytest.raises(ValidationError):
        rewrite_query(sql, {})
    with pytest.raises(ValidationError):
        expand_views(sql, lambda _dotted: None)


def test_a_table_read_inside_a_bare_values_list_is_authorized_and_rewritten() -> None:
    """A subquery in `VALUES` reads a catalog table: it is listed and reads its granted files."""
    sql = "VALUES ((SELECT count(*) FROM gold.sales), 1)"

    assert extract_table_refs(sql) == ["gold.sales"]
    rewritten = rewrite_query(sql, {"gold.sales": _parquet("a.parquet")})

    assert "gold.sales" not in rewritten.sql
    assert rewritten.sql.startswith(f"WITH {_cte_alias('gold.sales')} AS (SELECT * FROM ")
    assert _READER_CALL.search(rewritten.sql)
    assert rewritten.parameters == {"v_0_files": ["a.parquet"]}


def test_a_column_mask_holds_for_a_read_inside_a_bare_values_list() -> None:
    """The masked projection replaces the read wherever the read sits, a `VALUES` row included."""
    source = TableSource(
        urls=["a.parquet"],
        file_format="parquet",
        columns={"id": "BIGINT", "ssn": "VARCHAR"},
        column_masks={"ssn": "NULL"},
        row_filters=("id > 1",),
    )

    sql = rewrite_query(
        "VALUES ((SELECT max(ssn) FROM gold.customers))", {"gold.customers": source}
    ).sql

    assert 'NULL AS "ssn"' in sql
    assert "WHERE (id > 1)" in sql


@pytest.mark.parametrize(
    ("sql", "refs"),
    [
        ("VALUES ((WITH t AS (SELECT 1 AS a) SELECT a FROM t))", []),
        ("VALUES ((WITH t AS (SELECT 1 AS a) SELECT a FROM bronze.t))", ["bronze.t"]),
        ("VALUES ((SELECT a FROM bronze.x WHERE a IN (SELECT a FROM silver.y)))", None),
    ],
)
def test_a_bare_values_list_resolves_its_references_as_a_select_does(
    sql: str, refs: list[str] | None
) -> None:
    """A `WITH` name inside `VALUES` is a binding; a qualified name beside it is a real table.

    Parameters
    ----------
    sql
        A bare `VALUES` list reading through a subquery.
    refs
        The references expected, or `None` for the same as `SELECT * FROM (<sql>) AS v`.
    """
    expected = refs if refs is not None else extract_table_refs(f"SELECT * FROM ({sql}) AS v")

    assert extract_table_refs(sql) == expected


def test_an_ambiguous_reference_inside_a_bare_values_list_is_refused() -> None:
    """The scope gate sees into `VALUES`: a name that means two things is refused, as in SELECT."""
    inner = "(WITH t AS (SELECT 1 AS a) SELECT count(*) FROM bronze.t JOIN t ON true)"
    with pytest.raises(ValidationError, match="cannot be resolved unambiguously"):
        extract_table_refs(f"SELECT {inner} AS a")
    with pytest.raises(ValidationError, match="cannot be resolved unambiguously"):
        extract_table_refs(f"VALUES ({inner})")


def test_a_view_stored_as_a_bare_values_list_expands() -> None:
    """A view whose stored SQL is a `VALUES` list expands like any other view."""
    views = {"gold.v": ViewDefinition(name="gold.v", sql="VALUES (1, 'a')")}

    expanded = expand_views("SELECT * FROM gold.v", views.get)

    assert expanded.views == ("gold.v",)
    assert expanded.sql == "SELECT * FROM (SELECT * FROM (VALUES (1, 'a')) AS __lakana_values) AS v"


@pytest.mark.parametrize("sql", ["VALUES (1) AS t", "VALUES (1, 2) AS t(a, b)", "VALUES (1) t"])
def test_a_bare_values_list_with_an_alias_is_refused_as_duckdb_refuses_it(sql: str) -> None:
    """An alias on a bare `VALUES` is read by sqlglot, rejected by DuckDB; the guard names the fix.

    Parameters
    ----------
    sql
        A bare `VALUES` list with an alias.
    """
    message = "A VALUES list on its own takes no alias"
    with pytest.raises(ValidationError, match=message):
        check_blacklist(sql)
    with pytest.raises(ValidationError, match=message):
        extract_table_refs(sql)
    with pytest.raises(ValidationError, match=message):
        rewrite_query(sql, {})


_SELECT_FORM = (
    "A VALUES list on its own takes no {c}; write SELECT * FROM (VALUES ...) AS t(a, b) {c} ... "
    "instead."
)
"""The refusal for a clause DuckDB takes in a `SELECT` but not on a bare `VALUES`."""

_VALUES_CLAUSES_DUCKDB_REFUSES = [
    pytest.param("SORT BY 1", "DuckDB has no SORT BY; use ORDER BY.", id="sort-by"),
    pytest.param("CLUSTER BY 1", "DuckDB has no CLUSTER BY; use ORDER BY.", id="cluster-by"),
    pytest.param("DISTRIBUTE BY 1", "DuckDB has no DISTRIBUTE BY; remove it.", id="distribute-by"),
    pytest.param(
        "FOR UPDATE", "DuckDB has no FOR UPDATE; remove it, a query takes no lock.", id="for-update"
    ),
    pytest.param("WHERE TRUE", _SELECT_FORM.format(c="WHERE"), id="where"),
    pytest.param("QUALIFY TRUE", _SELECT_FORM.format(c="QUALIFY"), id="qualify"),
    pytest.param("HAVING TRUE", _SELECT_FORM.format(c="HAVING"), id="having"),
    pytest.param("WINDOW w AS (ORDER BY 1)", _SELECT_FORM.format(c="WINDOW"), id="window"),
    pytest.param("TABLESAMPLE (10 ROWS)", _SELECT_FORM.format(c="TABLESAMPLE"), id="tablesample"),
    pytest.param("USING SAMPLE 10", _SELECT_FORM.format(c="TABLESAMPLE"), id="using-sample"),
    pytest.param("GROUP BY 1", _SELECT_FORM.format(c="GROUP BY"), id="group-by"),
    pytest.param("JOIN bronze.x ON TRUE", _SELECT_FORM.format(c="JOIN"), id="join"),
]
"""Clauses sqlglot attaches to a bare `VALUES` and DuckDB refuses there, with the refusal.

For a clause DuckDB never takes, the refusal says so; for the others it shows the `SELECT` form
DuckDB runs."""


@pytest.mark.parametrize(("clause", "message"), _VALUES_CLAUSES_DUCKDB_REFUSES)
def test_a_clause_duckdb_does_not_take_on_a_bare_values_list_is_refused(
    clause: str, message: str
) -> None:
    """The rewrite would drop the clause or render it in the wrong place; it is refused instead.

    Parameters
    ----------
    clause
        The clause after the `VALUES` list.
    message
        The refusal, word for word.
    """
    sql = f"VALUES (1) {clause}"
    for check in (check_blacklist, extract_table_refs, lambda text: rewrite_query(text, {})):
        with pytest.raises(ValidationError) as caught:
            check(sql)
        assert str(caught.value) == message


def test_every_argument_sqlglot_can_set_on_values_is_allowed_or_refused_by_name() -> None:
    """A sqlglot upgrade that gives `exp.Values` a new argument fails here, not in production.

    sqlglot sets the query modifiers (`WHERE`, `JOIN`, ...) on any expression a query parses to,
    so they are listed beside `exp.Values.arg_types`.
    """
    known = set(exp.Values.arg_types) | set(exp.QUERY_MODIFIERS)

    assert known <= query_rewrite._VALUES_ARGS | set(query_rewrite._VALUES_CLAUSES)
    assert not query_rewrite._VALUES_ARGS & set(query_rewrite._VALUES_CLAUSES)
    assert set(query_rewrite._CLAUSES_DUCKDB_LACKS) <= set(query_rewrite._VALUES_CLAUSES.values())


def test_an_argument_on_values_with_no_name_yet_is_refused_all_the_same() -> None:
    """Allow-list, not deny-list: an argument nobody named is refused under its own key."""
    values = sqlglot.parse_one("VALUES (1)", dialect="duckdb")
    assert isinstance(values, exp.Values)
    values.set("novel", exp.Literal.number(1))

    with pytest.raises(ValidationError, match="takes no NOVEL"):
        query_rewrite._reject_values_clauses(values)


_WITNESS_PLACEHOLDER_NAME = "SELECT id fROM goLd.v_ouELECT $1 At"
"""A half-parsed table alias: sqlglot's parser raises `ValueError` on the placeholder (#215)."""

_WITNESS_EMPTY_NODE = (
    "WITH x AS (SELCET {'id'.id,: 1} AS orders) SELECT id FROM gold.orders "
    "WHERE EXISTS (SELECT 1 FROM x WHERE gold.orders.id = 3)"
)
"""A half-parsed struct literal: sqlglot's parser raises `AttributeError` on `None` (#216)."""

_SYNTAX_MESSAGE = "SQL syntax error: the query could not be read"


@pytest.mark.parametrize(
    "check",
    [query_rewrite.check_blacklist, query_rewrite.extract_table_refs, query_rewrite.rewrite_query],
)
def test_a_parser_value_error_is_refused_with_the_syntax_message(
    check: Callable[..., object],
) -> None:
    """A placeholder where the parser expects a name is refused, not raised as `ValueError` (#215).

    Parameters
    ----------
    check
        A public entry point of the guard.
    """
    args = (
        (_WITNESS_PLACEHOLDER_NAME, {})
        if check is query_rewrite.rewrite_query
        else (_WITNESS_PLACEHOLDER_NAME,)
    )
    with pytest.raises(sqlglot.errors.ParseError) as caught:
        check(*args)
    assert describe_syntax_error(caught.value, args[0]).startswith(_SYNTAX_MESSAGE)


@pytest.mark.parametrize("entry", ["check_blacklist", "extract_table_refs", "rewrite_query"])
def test_an_unexpected_error_in_the_walk_is_refused_and_logged_without_the_sql(
    entry: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Whatever else breaks while the guard reads a query refuses it; the SQL is not logged.

    Parameters
    ----------
    entry
        The name of the public entry point to call.
    monkeypatch
        Makes the first internal step raise.
    caplog
        Captures the warning.
    """

    def explode(*_args: object, **_kwargs: object) -> NoReturn:
        raise KeyError("secret-table-name")

    monkeypatch.setattr(query_rewrite, "_reject_placeholders", explode)
    monkeypatch.setattr(query_rewrite, "_cte_reference_ids", explode)
    args = ("SELECT 1 AS a FROM gold.t",) + (({},) if entry == "rewrite_query" else ())
    with (
        caplog.at_level("WARNING", logger="lakana_core.query_rewrite"),
        pytest.raises(ValidationError, match="could not parse your SQL"),
    ):
        getattr(query_rewrite, entry)(*args)
    assert "KeyError" in caplog.text
    assert "secret-table-name" not in caplog.text
    assert "gold.t" not in caplog.text


def test_expected_errors_pass_through_the_safety_net() -> None:
    """A syntax error keeps its own type; the net does not rewrite it."""
    with pytest.raises(sqlglot.errors.ParseError):
        query_rewrite.check_blacklist("SELECT FROM WHERE (")


@pytest.mark.parametrize(
    "check",
    [query_rewrite.check_blacklist, query_rewrite.extract_table_refs, query_rewrite.rewrite_query],
)
def test_a_parser_attribute_error_is_refused_with_the_syntax_message(
    check: Callable[..., object],
) -> None:
    """A half-parsed struct literal is refused, not raised as `AttributeError` (#216).

    Parameters
    ----------
    check
        A public entry point of the guard.
    """
    args = (
        (_WITNESS_EMPTY_NODE, {})
        if check is query_rewrite.rewrite_query
        else (_WITNESS_EMPTY_NODE,)
    )
    with pytest.raises(sqlglot.errors.ParseError) as caught:
        check(*args)
    assert describe_syntax_error(caught.value, args[0]).startswith(_SYNTAX_MESSAGE)


@pytest.mark.parametrize("witness", [_WITNESS_PLACEHOLDER_NAME, _WITNESS_EMPTY_NODE])
def test_a_stored_view_that_trips_a_parser_fault_is_named(witness: str) -> None:
    """The reader's valid query is not blamed for a stored view the parser faults on.

    Parameters
    ----------
    witness
        The stored view's SQL.
    """
    with pytest.raises(ValidationError, match=r"View 'gold\.v' could not be read"):
        expand_views(
            "SELECT * FROM gold.v",
            lambda dotted: ViewDefinition(name=dotted, sql=witness) if dotted == "gold.v" else None,
        )


_PIVOT_WITHOUT_VALUES = [
    "SELECT * FROM (PIVOT gold.sales ON a USING sum(b))",
    "SELECT * FROM (PIVOT gold.sales ON a USING sum(b) GROUP BY c)",
    "SELECT * FROM (PIVOT gold.sales ON a IN ('x'), c USING sum(b))",
    "SELECT * FROM (PIVOT gold.sales ON a || c USING sum(b))",
    "SELECT * FROM (PIVOT gold.sales ON a IN (SELECT a FROM gold.sales) USING sum(b))",
    "WITH p AS (PIVOT gold.sales ON a USING sum(b)) SELECT * FROM p",
    "SELECT * FROM gold.sales WHERE b IN (SELECT 1 FROM (PIVOT gold.sales ON a USING sum(b)))",
    "SELECT * FROM gold.sales PIVOT (sum(b) FOR a IN (SELECT a FROM gold.sales))",
]


@pytest.mark.parametrize("sql", _PIVOT_WITHOUT_VALUES)
def test_a_pivot_without_the_values_of_its_on_column_is_refused(sql: str) -> None:
    """DuckDB runs such a PIVOT as two statements (it creates a type first).

    The query process runs exactly one SELECT, so the guard says what to write instead of
    letting it fail there as a defect.
    """
    with pytest.raises(ValidationError) as caught:
        check_blacklist(sql)
    assert str(caught.value) == query_rewrite.PIVOT_VALUES_MESSAGE
    with pytest.raises(ValidationError):
        extract_table_refs(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM (PIVOT gold.sales ON a IN ('x', 'y') USING sum(b))",
        "SELECT * FROM (PIVOT gold.sales ON a IN ('x'), c IN (1, 2) USING sum(b) GROUP BY d)",
        "SELECT * FROM gold.sales PIVOT (sum(b) FOR a IN ('x', 'y'))",
        "SELECT * FROM gold.sales UNPIVOT (v FOR k IN (a, b))",
        "SELECT * FROM (UNPIVOT gold.sales ON a, b INTO NAME k VALUE v)",
    ],
)
def test_a_pivot_with_its_values_listed_and_an_unpivot_pass(sql: str) -> None:
    check_blacklist(sql)
    assert extract_table_refs(sql) == ["gold.sales"]
