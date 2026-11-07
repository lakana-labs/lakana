"""Per-asset parsing: front-matter, one SELECT, references, columns, lineage."""

from __future__ import annotations

import pytest
import sqlglot.errors

from lakana_core.projects.discovery import DiscoveredAsset
from lakana_core.projects.issues import ProjectValidationError
from lakana_core.projects.parse import normalize_sql, parse_assets
from lakana_core.projects.project_config import AssetDefaults


def _asset(text: str, table: str = "silver.weather") -> DiscoveredAsset:
    """Build a discovered asset.

    Parameters
    ----------
    text
        The file content.
    table
        The dotted table name.

    Returns
    -------
    DiscoveredAsset
        The asset, with a path derived from `table`.
    """
    return DiscoveredAsset(path=f"assets/{table.replace('.', '/')}.sql", table=table, text=text)


def test_parses_references_columns_and_lineage() -> None:
    """A plain SELECT yields its references, its explicit columns and column lineage."""
    [parsed] = parse_assets(
        [_asset("/* lakana\nowner: me\n*/\nSELECT station_id, temp_c AS t FROM bronze.weather\n")],
        defaults=AssetDefaults(),
    )
    assert parsed.table == "silver.weather"
    assert parsed.references == ("bronze.weather",)
    assert parsed.columns == ("station_id", "t")
    assert parsed.metadata.owner == "me"
    assert parsed.metadata.materialize == "table"
    assert parsed.lineage["t"] == [{"table": "bronze.weather", "column": "temp_c"}]
    assert parsed.body_line == 4
    assert parsed.sql == "SELECT station_id, temp_c AS t FROM bronze.weather"


def test_star_projection_has_no_explicit_columns() -> None:
    """`SELECT *` leaves `columns` as `None` so column checks are skipped."""
    [parsed] = parse_assets([_asset("SELECT * FROM bronze.weather")], defaults=AssetDefaults())
    assert parsed.columns is None


def test_a_values_list_is_a_query_with_no_explicit_columns() -> None:
    """A bare `VALUES` asset reads nothing; DuckDB names its columns, so none are listed."""
    [parsed] = parse_assets([_asset("VALUES (1, 'a'), (2, 'b')")], defaults=AssetDefaults())
    assert parsed.references == ()
    assert parsed.columns is None


def test_with_and_set_operations_are_single_queries() -> None:
    """`WITH … SELECT` and `UNION` are allowed; CTE names are not references."""
    sql = "WITH w AS (SELECT a FROM bronze.x) SELECT a FROM w UNION ALL SELECT a FROM bronze.y"
    [parsed] = parse_assets([_asset(sql)], defaults=AssetDefaults())
    assert parsed.references == ("bronze.x", "bronze.y")


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE TABLE t AS SELECT 1",
        "SELECT 1; SELECT 2",
        "INSERT INTO bronze.x SELECT 1",
        "SELECT * FROM read_csv('/etc/passwd')",
    ],
)
def test_non_queries_and_file_reads_are_refused(sql: str) -> None:
    """M5a's single-statement check applies to every asset."""
    with pytest.raises(ProjectValidationError) as info:
        parse_assets([_asset(sql)], defaults=AssetDefaults())
    assert info.value.issues[0].path == "assets/silver/weather.sql"
    assert info.value.issues[0].line == 1


def test_empty_body_is_refused() -> None:
    """Front-matter with no SQL is not an asset."""
    with pytest.raises(ProjectValidationError, match="has no SQL"):
        parse_assets([_asset("/* lakana\nowner: x\n*/\n\n")], defaults=AssetDefaults())


def test_unparseable_sql_reports_the_body_line() -> None:
    """A syntax error is an issue on the line where the body starts."""
    with pytest.raises(ProjectValidationError, match="does not parse") as info:
        parse_assets([_asset("/* lakana\nowner: x\n*/\nSELEC 1 FROM")], defaults=AssetDefaults())
    assert info.value.issues[0].line == 4


def test_deeply_nested_sql_is_an_issue_not_a_crash() -> None:
    """Adversarial: recursion in sqlglot becomes a validation issue."""
    sql = "SELECT " + "(" * 3000 + "1" + ")" * 3000 + " AS a"
    with pytest.raises(ProjectValidationError, match=r"nested too deeply|does not parse"):
        parse_assets([_asset(sql)], defaults=AssetDefaults())


def test_defaults_are_merged_and_consistency_checked() -> None:
    """`defaults.materialize: incremental` without a key fails every asset that inherits it."""
    with pytest.raises(ProjectValidationError, match="incremental_key is required"):
        parse_assets(
            [_asset("SELECT a FROM bronze.x")], defaults=AssetDefaults(materialize="incremental")
        )


def test_issues_across_assets_are_collected() -> None:
    """Every file's problems are reported together."""
    with pytest.raises(ProjectValidationError) as info:
        parse_assets(
            [
                _asset("DROP TABLE x", "silver.a"),
                _asset("/* lakana\nbad: 1\n*/\nSELECT 1", "silver.b"),
            ],
            defaults=AssetDefaults(),
        )
    assert {issue.path for issue in info.value.issues} == {
        "assets/silver/a.sql",
        "assets/silver/b.sql",
    }


def test_normalize_sql_ignores_formatting_comments_and_keyword_case() -> None:
    """Formatting-only changes normalize to the same text."""
    first = "SELECT a, b FROM bronze.x WHERE a > 1"
    second = "select a,\n       b  -- why\n  from bronze.x\n where a > 1"
    assert normalize_sql(first) == normalize_sql(second)
    assert normalize_sql(first) != normalize_sql("SELECT a FROM bronze.x WHERE a > 1")


def test_recursion_error_is_an_issue(monkeypatch: pytest.MonkeyPatch) -> None:
    """Adversarial: a RecursionError from sqlglot never escapes."""

    def boom(*_args: object, **_kwargs: object) -> None:
        raise RecursionError

    monkeypatch.setattr("lakana_core.projects.parse.check_blacklist", boom)
    with pytest.raises(ProjectValidationError, match="nested too deeply") as info:
        parse_assets([_asset("SELECT 1 AS a")], defaults=AssetDefaults())
    assert info.value.issues[0].line == 1


def test_sqlglot_error_is_an_issue(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any sqlglot error is reported with the file and line."""

    def boom(*_args: object, **_kwargs: object) -> None:
        raise sqlglot.errors.ParseError("bad")

    monkeypatch.setattr("lakana_core.projects.parse.check_blacklist", boom)
    with pytest.raises(ProjectValidationError, match="does not parse: invalid SQL"):
        parse_assets([_asset("SELECT 1 AS a")], defaults=AssetDefaults())


@pytest.mark.parametrize(
    "front",
    [
        "/* lakana\nowner: &a x\nname: *a\n*/\nSELECT 1 AS a",
        "/* lakana\n!!python/object/apply:os.system [id]\n*/\nSELECT 1 AS a",
        "/* lakana\nowner: [\n*/\nSELECT 1 AS a",
    ],
)
def test_hostile_front_matter_is_an_issue_not_an_abort(front: str) -> None:
    """YAML errors in one file are collected next to another file's issues."""
    with pytest.raises(ProjectValidationError) as info:
        parse_assets(
            [_asset(front, "silver.a"), _asset("DROP TABLE x", "silver.b")],
            defaults=AssetDefaults(),
        )
    assert {issue.path for issue in info.value.issues} == {
        "assets/silver/a.sql",
        "assets/silver/b.sql",
    }


def test_references_are_sorted() -> None:
    """References come back sorted so manifests and diffs are deterministic."""
    [parsed] = parse_assets(
        [_asset("SELECT a FROM bronze.y JOIN bronze.x USING (a)")], defaults=AssetDefaults()
    )
    assert parsed.references == ("bronze.x", "bronze.y")


def test_aggregate_star_does_not_hide_the_columns() -> None:
    """`count(*)` is not a star projection; the output names are still known."""
    [parsed] = parse_assets(
        [_asset("SELECT a, count(*) AS n FROM bronze.x GROUP BY a")], defaults=AssetDefaults()
    )
    assert parsed.columns == ("a", "n")


@pytest.mark.parametrize(
    "sql",
    ["SELECT COLUMNS('a.*') FROM bronze.x", "SELECT x.*, a FROM bronze.x AS x"],
)
def test_qualified_star_and_columns_expression_are_stars(sql: str) -> None:
    """`t.*` and DuckDB's `COLUMNS(...)` leave the columns unknown."""
    [parsed] = parse_assets([_asset(sql)], defaults=AssetDefaults())
    assert parsed.columns is None


def test_parse_error_never_echoes_sql_or_escape_codes() -> None:
    """Terminal injection: the message is sqlglot's description, the line is the body's."""
    sql = "/* lakana\nowner: x\n*/\nSELECT 1 +\n+ FROM x -- \x1b[31m"
    with pytest.raises(ProjectValidationError) as info:
        parse_assets([_asset(sql)], defaults=AssetDefaults())
    issue = info.value.issues[0]
    assert "\x1b" not in issue.render()
    assert "SELECT 1" not in issue.message
    assert issue.line == 5


def test_an_unclosed_quote_names_its_line_and_column() -> None:
    """A tokenizer error is placed in the file by line and column."""
    sql = "/* lakana\nowner: x\n*/\nSELECT 1\nFROM 'abc"
    with pytest.raises(ProjectValidationError) as info:
        parse_assets([_asset(sql)], defaults=AssetDefaults())
    issue = info.value.issues[0]
    assert issue.line == 5
    assert (
        issue.message
        == "the SQL does not parse: a quote is opened here and never closed (column 6)"
    )


def test_the_parser_description_is_cleaned_and_cut() -> None:
    """No class path, token dump or escape code reaches the issue; its length is bounded."""
    sql = "SELECT 1 +\n+ FROM " + "x" * 5000
    with pytest.raises(ProjectValidationError) as info:
        parse_assets([_asset(sql)], defaults=AssetDefaults())
    message = info.value.issues[0].message
    assert "<class" not in message
    assert "<Token" not in message
    assert "\x1b" not in message
    assert len(message) < 250


def test_non_parse_sqlglot_error_gets_a_generic_message(monkeypatch: pytest.MonkeyPatch) -> None:
    """Errors without structured detail fall back to a generic description."""

    def boom(*_args: object, **_kwargs: object) -> None:
        raise sqlglot.errors.SqlglotError("\x1b[31mraw")

    monkeypatch.setattr("lakana_core.projects.parse.check_blacklist", boom)
    with pytest.raises(ProjectValidationError) as info:
        parse_assets([_asset("SELECT 1 AS a")], defaults=AssetDefaults())
    assert info.value.issues[0].message == "the SQL does not parse: invalid SQL"


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM range(10)",
        "SELECT * FROM generate_series(1, 3) AS t(x)",
        "SELECT * FROM unnest([1, 2, 3]) AS u(x)",
    ],
)
def test_generator_table_functions_are_allowed_and_read_nothing(sql: str) -> None:
    """`range`, `generate_series` and `unnest` deploy and contribute no external input."""
    [parsed] = parse_assets([_asset(sql)], defaults=AssetDefaults())
    assert parsed.references == ()


def test_a_generator_beside_a_real_table_keeps_the_table_as_the_input() -> None:
    """Only the real table is an external input when a generator joins it."""
    sql = "SELECT b.id, r.i FROM bronze.x AS b, range(3) AS r(i)"
    [parsed] = parse_assets([_asset(sql)], defaults=AssetDefaults())
    assert parsed.references == ("bronze.x",)


@pytest.mark.parametrize(
    "function", ["read_parquet", "iceberg_scan", "sqlite_scan", "some_unknown_fn"]
)
def test_other_table_functions_are_refused_with_path_and_line(function: str) -> None:
    """A file/foreign reader or an unknown table function fails the deploy at its file."""
    with pytest.raises(ProjectValidationError) as caught:
        parse_assets(
            [_asset(f"-- c\nSELECT * FROM {function}('x')", table="silver.t")],
            defaults=AssetDefaults(),
        )
    [issue] = caught.value.issues
    assert (issue.path, issue.line) == ("assets/silver/t.sql", 1)
    assert f"table function {function} is not allowed" in issue.message
    assert "Register the data as a table and reference it" in issue.message


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM (SELECT 1 a) b, LATERAL iceberg_scan('x')",
        "SELECT * FROM (SELECT 1 a) b JOIN LATERAL duckdb_types() ON true",
    ],
)
def test_lateral_table_functions_are_refused_at_deploy(sql: str) -> None:
    """`LATERAL fn(...)` cannot smuggle a reader past the deploy check."""
    with pytest.raises(ProjectValidationError) as caught:
        parse_assets([_asset(sql, table="silver.t")], defaults=AssetDefaults())
    [issue] = caught.value.issues
    assert (issue.path, issue.line) == ("assets/silver/t.sql", 1)
    assert "is not allowed" in issue.message


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM (SELECT 1 a) b, LATERAL main.duckdb_types()",
        "SELECT * FROM (SELECT 1 a) b, LATERAL system.main.duckdb_functions()",
        'SELECT * FROM (SELECT 1 a) b, LATERAL "main"."duckdb_types"()',
        "SELECT * FROM (SELECT 1 a) b, LATERAL "
        "(SELECT * FROM (SELECT 2 c) d, LATERAL duckdb_types()) s",
    ],
)
def test_qualified_and_nested_lateral_functions_are_refused_at_deploy(sql: str) -> None:
    """A schema-qualified or nested `LATERAL fn()` is refused with its file and line."""
    with pytest.raises(ProjectValidationError) as caught:
        parse_assets([_asset(sql, table="silver.t")], defaults=AssetDefaults())
    [issue] = caught.value.issues
    assert (issue.path, issue.line) == ("assets/silver/t.sql", 1)
    assert "is not allowed" in issue.message


def test_a_qualified_lateral_generator_deploys_with_no_references() -> None:
    """`LATERAL main.range(3)` is a generator and reads nothing."""
    [parsed] = parse_assets(
        [_asset("SELECT * FROM (SELECT 1 a) b, LATERAL main.range(3)")], defaults=AssetDefaults()
    )
    assert parsed.references == ()


@pytest.mark.parametrize(
    "body",
    [
        "SELECT regexp_extract(url, '.*/(.*)') AS leaf FROM bronze.events",
        "SELECT '*/5 * * * *' AS schedule",
        "SELECT id FROM bronze.events WHERE note LIKE '%*/%'",
        "-- strips the trailing */ marker\nSELECT 1 AS a",
        "SELECT '*/' AS a /* c */",
    ],
)
def test_a_body_with_star_slash_before_any_comment_still_parses(body: str) -> None:
    """A `*/` in valid SQL is never mistaken for a cut front-matter block."""
    [parsed] = parse_assets([_asset(f"/* lakana\nowner: x\n*/\n{body}")], defaults=AssetDefaults())
    assert parsed.metadata.owner == "x"


@pytest.mark.parametrize(
    "block",
    ["description: ends */ early\nowner: x", "description: |\n  path a/*/b is odd\nowner: x"],
)
def test_a_cut_block_that_breaks_the_sql_names_the_cause(block: str) -> None:
    """When the shortened block leaves SQL that fails, the issue says why."""
    with pytest.raises(ProjectValidationError) as info:
        parse_assets([_asset(f"/* lakana\n{block}\n*/\nSELECT 1")], defaults=AssetDefaults())
    message = info.value.issues[0].message
    assert message.startswith("the SQL does not parse")
    assert "the block ends at the first `*/`" in message


def test_python_and_sql_assets_are_parsed_together_and_issues_collected() -> None:
    good_sql = _asset("SELECT 1 AS a")
    good_py = DiscoveredAsset(
        path="assets/gold/scores.py",
        table="gold.scores",
        text=(
            "from lakana import asset\n\n@asset(inputs={'w': 'silver.weather'})\n"
            "def f(w):\n    return w\n"
        ),
        kind="python",
    )
    parsed = parse_assets([good_sql, good_py], defaults=AssetDefaults())
    assert [(a.table, a.kind) for a in parsed] == [
        ("silver.weather", "sql"),
        ("gold.scores", "python"),
    ]
    bad_py = DiscoveredAsset(
        path="assets/gold/bad.py", table="gold.bad", text="x = 1\n", kind="python"
    )
    bad_sql = _asset("DROP TABLE x", table="silver.bad")
    with pytest.raises(ProjectValidationError) as info:
        parse_assets([bad_sql, bad_py, good_py], defaults=AssetDefaults())
    assert {issue.path for issue in info.value.issues} == {
        "assets/silver/bad.sql",
        "assets/gold/bad.py",
    }


def test_an_asset_longer_than_the_statement_limit_is_an_issue_that_names_the_setting() -> None:
    """A SQL asset is a statement too: it gets the limit a submitted query gets."""
    from lakana_core.projects.limits import DeployLimits

    sql = "SELECT 'aaaaaaaa' AS a FROM bronze.weather"
    text = "/* lakana\nowner: me\n*/\n" + sql + "\n"
    limit = len(sql) - 1
    with pytest.raises(ProjectValidationError) as caught:
        parse_assets(
            [_asset(text)], defaults=AssetDefaults(), limits=DeployLimits(statement_chars=limit)
        )
    [issue] = caught.value.issues
    assert issue.line == 4
    assert "LAKANA_QUERY_LIMITS__MAX_STATEMENT_CHARS" in issue.message
    assert f"the most Lakana accepts is {limit}" in issue.message
    assert "aaaa" not in issue.message


def test_an_asset_of_exactly_the_statement_limit_parses() -> None:
    """The limit is inclusive."""
    from lakana_core.projects.limits import DeployLimits

    sql = "SELECT a FROM bronze.weather"
    [parsed] = parse_assets(
        [_asset(sql)], defaults=AssetDefaults(), limits=DeployLimits(statement_chars=len(sql))
    )
    assert parsed.sql == sql


def test_a_statement_that_changes_data_inside_a_cte_fails_the_deploy_at_its_file() -> None:
    """The deploy names the file, the line and the reason."""
    sql = "WITH gone AS (DELETE FROM bronze.x RETURNING *) SELECT * FROM gone"

    with pytest.raises(ProjectValidationError) as caught:
        parse_assets([_asset(sql)], defaults=AssetDefaults())

    [issue] = caught.value.issues
    assert (issue.path, issue.line) == ("assets/silver/weather.sql", 1)
    assert issue.message == (
        "A query reads data and cannot change it: `DELETE` is not allowed, also not inside a "
        "CTE or subquery."
    )


def test_a_generator_table_function_is_not_a_file_reference() -> None:
    from lakana_core.projects.parse import sql_file_references

    for sql in (
        "SELECT i FROM range(1, 13) AS r(i)",
        "SELECT i FROM generate_series(1, 3) AS g(i)",
        "SELECT i FROM unnest([1, 2]) AS u(i)",
    ):
        assert sql_file_references(sql, []) == set(), sql


def test_a_reading_table_function_is_still_refused_and_named() -> None:
    from lakana_core.projects.parse import sql_file_references

    found = sql_file_references("SELECT * FROM read_parquet('x.parquet')", [])
    assert found == {"read_parquet()"}
    assert "" not in sql_file_references("SELECT * FROM read_csv('a.csv') AS c", [])
