"""Checks of a project: discovery, the deploy rules and the manifest."""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path
from typing import Any, cast

import pytest
from test_projects_dag import make_asset

from lakana_core.errors import ValidationError
from lakana_core.projects.checks import (
    DiscoveredCheck,
    GeneratedFrom,
    ParsedCheck,
    check_issues,
    checks_listing,
    checks_of,
    count_query,
    discover_checks,
    kept_check_tables,
    manifest_checks,
    parse_checks,
    read_manifest_checks,
    source_check_tables,
    view_readers,
)
from lakana_core.projects.dag import build_graph
from lakana_core.projects.discovery import DiscoveredAsset
from lakana_core.projects.frontmatter import AssetMetadata
from lakana_core.projects.issues import ProjectValidationError
from lakana_core.projects.limits import DeployLimits
from lakana_core.projects.manifest import build_manifest
from lakana_core.projects.parse import parse_assets
from lakana_core.projects.project_config import (
    Materialize,
    ResolvedProjectConfig,
    parse_project_config,
)
from lakana_core.projects.run_policy import RunCaps

_TABLE = "silver.station_temperatures"
_PATH = "checks/silver/station_temperatures/plausible_range.sql"


def _check(text: str, check: str = "plausible_range", table: str = _TABLE) -> DiscoveredCheck:
    """Build a discovered check of `table`.

    Parameters
    ----------
    text
        The file's content.
    check
        The check's name.
    table
        The checked table.

    Returns
    -------
    DiscoveredCheck
        The check, at the path its table and name decide.
    """
    namespace, name = table.rsplit(".", 1)
    return DiscoveredCheck(
        f"checks/{namespace.replace('.', '/')}/{name}/{check}.sql", table, check, text
    )


def _parsed(
    check: str = "plausible_range", table: str = _TABLE, *, on_failure: str = "block"
) -> ParsedCheck:
    """Build a parsed check without going through the parser.

    Parameters
    ----------
    check
        The check's name.
    table
        The checked table.
    on_failure
        ``block`` or ``warn``.

    Returns
    -------
    ParsedCheck
        The check, reading its own table, its SQL on line 1.
    """
    discovered = _check(f"SELECT 1 FROM {table}", check, table)
    return ParsedCheck(
        name=f"{table}/{check}",
        table=table,
        check=check,
        path=discovered.path,
        on_failure=on_failure,
        description=None,
        sql=f"SELECT 1 FROM {table}",
        references=(table,),
        body_line=1,
    )


def _write(root: Path, relative: str, text: str = "SELECT 1 FROM silver.t\n") -> Path:
    """Write a file under `root`, creating parents.

    Parameters
    ----------
    root
        The checkout root.
    relative
        POSIX path below `root`.
    text
        File content.

    Returns
    -------
    Path
        The written file.
    """
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _messages(issues: list) -> list[str]:
    """Render every issue.

    Parameters
    ----------
    issues
        The issues.

    Returns
    -------
    list of str
        One rendered line per issue.
    """
    return [issue.render() for issue in issues]


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        (
            "SELECT station_id FROM silver.station_temperatures WHERE temp_c > 60 -- a fault",
            "SELECT COUNT(*) AS failing FROM (SELECT station_id FROM silver.station_temperatures "
            "WHERE temp_c > 60 /* a fault */) AS lakana_check",
        ),
        (
            "WITH d AS (SELECT id, count(*) n FROM silver.t GROUP BY id) "
            "SELECT * FROM d WHERE n > 1;",
            "SELECT COUNT(*) AS failing FROM (WITH d AS (SELECT id, COUNT(*) AS n FROM silver.t "
            "GROUP BY id) SELECT * FROM d WHERE n > 1) AS lakana_check",
        ),
        (
            "SELECT id FROM silver.t EXCEPT SELECT id FROM dim.keys",
            "SELECT COUNT(*) AS failing FROM (SELECT id FROM silver.t EXCEPT SELECT id FROM "
            "dim.keys) AS lakana_check",
        ),
    ],
)
def test_the_count_query_wraps_one_select(sql: str, expected: str) -> None:
    assert count_query(sql) == expected


@pytest.mark.parametrize(
    ("sql", "message"),
    [
        ("SELECT 1; SELECT 2", "this file has 2 statements"),
        ("INSERT INTO silver.t VALUES (1)", "this file is a INSERT statement"),
        ("", "the check has no SQL"),
    ],
)
def test_the_count_query_refuses_what_is_not_one_select(sql: str, message: str) -> None:
    with pytest.raises(ValidationError) as caught:
        count_query(sql)
    assert message in str(caught.value)


def test_the_count_query_names_the_rule_it_enforces() -> None:
    with pytest.raises(ValidationError) as caught:
        count_query("SELECT 1; SELECT 2")
    assert str(caught.value) == (
        "a check is one SELECT that returns the rows that are wrong; this file has 2 statements"
    )


def test_a_check_without_a_block_blocks() -> None:
    (parsed,) = parse_checks([_check("SELECT station_id FROM silver.station_temperatures")])
    assert (parsed.name, parsed.on_failure, parsed.description) == (
        "silver.station_temperatures/plausible_range",
        "block",
        None,
    )
    assert parsed.references == (_TABLE,)


def test_a_block_sets_warn_and_a_description() -> None:
    text = (
        "/* lakana\ndescription: A sensor fault.\non_failure: warn\n*/\n"
        "SELECT station_id FROM silver.station_temperatures WHERE temp_c > 60\n"
    )
    (parsed,) = parse_checks([_check(text)])
    assert (parsed.on_failure, parsed.description, parsed.body_line) == (
        "warn",
        "A sensor fault.",
        5,
    )


def test_a_block_sets_keep() -> None:
    text = "/* lakana\non_failure: keep\n*/\nSELECT station_id FROM silver.station_temperatures"
    (parsed,) = parse_checks([_check(text)])
    assert parsed.on_failure == "keep"


def test_a_wrong_on_failure_names_the_three_values() -> None:
    text = "/* lakana\non_failure: stop\n*/\nSELECT station_id FROM silver.station_temperatures"
    with pytest.raises(ProjectValidationError) as caught:
        parse_checks([_check(text)])
    assert [issue.message for issue in caught.value.issues] == [
        "on_failure must be block, keep or warn (got 'stop')"
    ]


@pytest.mark.parametrize(
    ("text", "line", "message"),
    [
        (
            "/* lakana\nseverity: high\n*/\nSELECT 1 FROM silver.station_temperatures",
            1,
            "unknown key 'severity'; a check's /* lakana */ block takes: description, on_failure",
        ),
        (
            "/* lakana\non_failure: error\n*/\nSELECT 1 FROM silver.station_temperatures",
            1,
            "on_failure must be block, keep or warn (got 'error')",
        ),
        (
            "SELECT 1 FROM silver.other",
            1,
            "the check does not read 'silver.station_temperatures', the table its path names. "
            "Select from that table, or move the file into the directory of the table it checks.",
        ),
        (
            "SELECT 1; SELECT 2",
            1,
            "a check is one SELECT that returns the rows that are wrong; this file has 2 "
            "statements",
        ),
        (
            "INSERT INTO silver.station_temperatures VALUES (1)",
            1,
            "a check is one SELECT that returns the rows that are wrong; this file is a INSERT "
            "statement",
        ),
        (
            "SELECT * FROM read_csv('x.csv')",
            1,
            "table function read_csv is not allowed",
        ),
        ("\n\n", 1, "the check has no SQL"),
    ],
)
def test_parsing_refuses_with_file_and_line(text: str, line: int, message: str) -> None:
    with pytest.raises(ProjectValidationError) as caught:
        parse_checks([_check(text)])
    (issue,) = caught.value.issues
    assert issue.path == _PATH
    assert issue.line == line
    assert message in issue.message


def test_every_issue_of_every_check_is_reported_together() -> None:
    with pytest.raises(ProjectValidationError) as caught:
        parse_checks(
            [
                _check("SELECT 1 FROM silver.other", "a_bad"),
                _check("SELECT 1; SELECT 2", "b_bad"),
            ]
        )
    assert [issue.path for issue in caught.value.issues] == [
        "checks/silver/station_temperatures/a_bad.sql",
        "checks/silver/station_temperatures/b_bad.sql",
    ]


def test_a_syntax_error_names_its_line() -> None:
    text = (
        "/* lakana\ndescription: x\n*/\nSELECT a\nFROM silver.station_temperatures\n"
        "WHERE a = 'oops\n"
    )
    with pytest.raises(ProjectValidationError) as caught:
        parse_checks([_check(text)])
    (issue,) = caught.value.issues
    assert issue.line == 6
    assert issue.message.startswith("the SQL does not parse:")


def test_a_statement_over_the_limit_is_refused() -> None:
    with pytest.raises(ProjectValidationError) as caught:
        parse_checks(
            [_check("SELECT station_id FROM silver.station_temperatures")],
            limits=DeployLimits(statement_chars=20),
        )
    (issue,) = caught.value.issues
    assert issue.message.startswith("the SQL is ")
    assert "20" in issue.message


def test_a_block_that_is_not_first_is_refused() -> None:
    text = "SELECT 1 FROM silver.station_temperatures\n/* lakana\non_failure: warn\n*/\n"
    with pytest.raises(ProjectValidationError) as caught:
        parse_checks([_check(text)])
    (issue,) = caught.value.issues
    assert (issue.line, issue.message) == (
        2,
        "the /* lakana */ block must be the first thing in the file",
    )


def test_an_unsafe_description_is_refused() -> None:
    text = '/* lakana\ndescription: "a\\u2028b"\n*/\nSELECT 1 FROM silver.station_temperatures'
    with pytest.raises(ProjectValidationError) as caught:
        parse_checks([_check(text)])
    (issue,) = caught.value.issues
    assert issue.message.startswith("description:")


def test_a_deeply_nested_check_is_refused_not_crashed() -> None:
    text = "SELECT " + "(" * 3000 + "1" + ")" * 3000 + " FROM silver.station_temperatures"
    with pytest.raises(ProjectValidationError) as caught:
        parse_checks([_check(text)])
    (issue,) = caught.value.issues
    assert issue.path == _PATH


# -- discovery ---------------------------------------------------------------------------------


def test_discovery_finds_checks_sorted_by_path(tmp_path: Path) -> None:
    _write(tmp_path, "checks/silver/b/two.sql", "﻿SELECT 2 FROM silver.b\r\n")
    _write(tmp_path, "checks/silver/a/one.sql", "SELECT 1 FROM silver.a\n")
    _write(tmp_path, "checks/silver/a/zero.sql", "SELECT 0 FROM silver.a\n")
    found, issues = discover_checks(tmp_path, limits=DeployLimits())
    assert issues == []
    assert found == [
        DiscoveredCheck("checks/silver/a/one.sql", "silver.a", "one", "SELECT 1 FROM silver.a\n"),
        DiscoveredCheck("checks/silver/a/zero.sql", "silver.a", "zero", "SELECT 0 FROM silver.a\n"),
        DiscoveredCheck("checks/silver/b/two.sql", "silver.b", "two", "SELECT 2 FROM silver.b\n"),
    ]


def test_a_project_without_checks_has_none(tmp_path: Path) -> None:
    _write(tmp_path, "assets/silver/a.sql")
    assert discover_checks(tmp_path, limits=DeployLimits()) == ([], [])


@pytest.mark.parametrize("relative", ["checks/x.sql", "checks/silver/x.sql"])
def test_a_file_outside_the_table_directory_is_refused(tmp_path: Path, relative: str) -> None:
    _write(tmp_path, relative)
    found, issues = discover_checks(tmp_path, limits=DeployLimits())
    assert found == []
    assert _messages(issues) == [
        f"{relative}: a check must live in a directory named after its table: "
        "checks/<namespace>/<table>/<name>.sql"
    ]


def test_a_deep_path_makes_a_nested_namespace(tmp_path: Path) -> None:
    _write(tmp_path, "checks/a/b/c/d.sql")
    ((found,), issues) = discover_checks(tmp_path, limits=DeployLimits())
    assert (found.table, found.check) == ("a.b.c", "d")
    assert issues == []


def test_a_python_file_under_checks_is_refused(tmp_path: Path) -> None:
    _write(tmp_path, "checks/silver/t/x.py", "print(1)\n")
    found, issues = discover_checks(tmp_path, limits=DeployLimits())
    assert found == []
    assert _messages(issues) == [
        "checks/silver/t/x.py: a check is a SQL file; a Python file under checks/ is not run. "
        "Write the check as a SELECT that returns the rows that are wrong"
    ]


def test_another_suffix_is_ignored(tmp_path: Path) -> None:
    _write(tmp_path, "checks/README.md", "# notes\n")
    _write(tmp_path, "checks/silver/t/notes.txt", "notes\n")
    assert discover_checks(tmp_path, limits=DeployLimits()) == ([], [])


def test_a_name_that_is_no_name_is_refused(tmp_path: Path) -> None:
    _write(tmp_path, "checks/silver/t/a.b.sql")
    found, issues = discover_checks(tmp_path, limits=DeployLimits())
    assert found == []
    (issue,) = issues
    assert issue.path == "checks/silver/t/a.b.sql"
    assert issue.message.startswith("the check's name must be a valid name: Check name ")


def test_a_namespace_or_table_that_is_no_name_is_refused(tmp_path: Path) -> None:
    _write(tmp_path, "checks/si.lver/t/x.sql")
    _write(tmp_path, "checks/silver/t.t/x.sql")
    _, issues = discover_checks(tmp_path, limits=DeployLimits())
    assert [issue.message.split(" ")[:2] for issue in issues] == [
        ["Namespace", "component"],
        ["Table", "name"],
    ]


def test_two_spellings_of_one_check_are_refused(tmp_path: Path) -> None:
    probe = tmp_path / "Probe"
    probe.write_text("x")
    if (tmp_path / "probe").exists():
        pytest.skip("the file system folds case, so two spellings cannot coexist")
    _write(tmp_path, "checks/silver/t/Unique.sql")
    _write(tmp_path, "checks/silver/t/unique.sql")
    found, issues = discover_checks(tmp_path, limits=DeployLimits())
    assert [check.check for check in found] == ["Unique"]
    assert _messages(issues) == [
        "checks/silver/t/unique.sql: checks/silver/t/Unique.sql and checks/silver/t/unique.sql "
        "are both the check 'silver.t/unique'"
    ]


def test_symlinks_under_checks_are_refused(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    _write(outside, "secret.sql", "SELECT 1 FROM silver.t\n")
    root = tmp_path / "repo"
    _write(root, "checks/silver/t/real.sql")
    (root / "checks/silver/t/linked.sql").symlink_to(outside / "secret.sql")
    (root / "checks/silver/other").symlink_to(outside, target_is_directory=True)
    found, issues = discover_checks(root, limits=DeployLimits())
    assert [check.path for check in found] == ["checks/silver/t/real.sql"]
    assert _messages(issues) == [
        "checks/silver/other: symlinks are not allowed under checks/",
        "checks/silver/t/linked.sql: symlinks are not allowed under checks/",
    ]


def test_a_symlinked_checks_root_is_refused(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    _write(outside, "silver/t/x.sql")
    root = tmp_path / "repo"
    root.mkdir()
    (root / "checks").symlink_to(outside, target_is_directory=True)
    found, issues = discover_checks(root, limits=DeployLimits())
    assert found == []
    assert _messages(issues) == ["checks: symlinks are not allowed under checks/"]


def test_an_oversized_or_non_utf8_check_is_refused(tmp_path: Path) -> None:
    _write(tmp_path, "checks/silver/t/big.sql", "SELECT 1 FROM silver.t -- " + "x" * 2048)
    (tmp_path / "checks/silver/t/bad.sql").write_bytes(b"SELECT '\xff' FROM silver.t")
    found, issues = discover_checks(tmp_path, limits=DeployLimits(sql_file_bytes=1024))
    assert found == []
    assert _messages(issues) == [
        "checks/silver/t/bad.sql: checks/silver/t/bad.sql is not valid UTF-8",
        "checks/silver/t/big.sql: larger than 1 KiB",
    ]


def test_a_path_with_a_control_character_is_refused_and_escaped(tmp_path: Path) -> None:
    _write(tmp_path, "checks/silver/t/a\x1bb.sql")
    found, issues = discover_checks(tmp_path, limits=DeployLimits())
    assert found == []
    assert _messages(issues) == [
        "checks/silver/t/a\\x1bb.sql: the path has a non-UTF-8 or control character"
    ]


def test_a_directory_that_cannot_be_listed_is_an_issue(tmp_path: Path) -> None:
    _write(tmp_path, "checks/silver/t/x.sql")
    locked = tmp_path / "checks/silver/t"
    locked.chmod(0)
    try:
        if os.access(locked, os.R_OK):
            pytest.skip("the user can read a mode-0 directory (root)")
        _, issues = discover_checks(tmp_path, limits=DeployLimits())
    finally:
        locked.chmod(0o755)
    assert _messages(issues) == ["checks/silver/t: cannot be read: Permission denied"]


# -- the deploy rules ----------------------------------------------------------------------------


def _assets(*tables: str, reads: tuple[str, ...] = ()) -> list:
    """Build assets that produce `tables` and read `reads`.

    Parameters
    ----------
    *tables
        The tables the assets produce.
    reads
        The tables the first asset reads.

    Returns
    -------
    list of ParsedAsset
        One asset per table.
    """
    return [make_asset(table, reads if i == 0 else ()) for i, table in enumerate(tables)]


def _issues(
    checks: list[ParsedCheck], assets: list, *, limits: DeployLimits | None = None
) -> list[str]:
    """Run `check_issues` and render what it finds.

    Parameters
    ----------
    checks
        The checks.
    assets
        The project's assets.
    limits
        Limits, or the defaults.

    Returns
    -------
    list of str
        The rendered issues.
    """
    return _messages(check_issues(checks, assets, build_graph(assets), limits=limits))


def test_a_check_of_a_table_the_project_neither_builds_nor_reads_is_refused() -> None:
    assets = _assets("bronze.readings", "silver.station_temperatures", reads=("raw.events",))
    assert _issues([_parsed(table="gold.other")], assets) == [
        "checks/gold/other/plausible_range.sql:1: table 'gold.other' is neither built nor read by "
        "this project. A check belongs to a table that an asset of this project produces or "
        "selects from. This project builds: bronze.readings, silver.station_temperatures. "
        "It reads: raw.events."
    ]


def test_a_long_list_of_built_tables_is_cut_after_ten() -> None:
    assets = _assets(*[f"silver.t{i:02d}" for i in range(12)], reads=("raw.events",))
    (message,) = _issues([_parsed(table="gold.other")], assets)
    assert message.endswith(
        "This project builds: silver.t00, silver.t01, silver.t02, silver.t03, silver.t04, "
        "silver.t05, silver.t06, silver.t07, silver.t08, silver.t09 and 2 more. "
        "It reads: raw.events."
    )


def test_a_project_with_no_external_input_reads_nothing() -> None:
    (message,) = _issues([_parsed(table="gold.other")], _assets("silver.a"))
    assert message.endswith("This project builds: silver.a. It reads: nothing.")


def test_a_check_of_a_built_table_and_of_a_read_table_pass() -> None:
    assets = _assets(_TABLE, reads=("raw.events",))
    assert _issues([_parsed(), _parsed("fresh", "raw.events")], assets) == []


def test_a_check_of_an_artifact_is_refused() -> None:
    artifact = dataclasses.replace(make_asset("ml.model"), output="artifact", kind="python")
    assert _issues([_parsed(table="ml.model")], [artifact]) == [
        "checks/ml/model/plausible_range.sql:1: 'ml.model' is an artifact; a check needs a table. "
        "Check a table the artifact is built from instead."
    ]


def test_a_check_of_a_view_is_allowed() -> None:
    view = make_asset(_TABLE, metadata=AssetMetadata(materialize="view"))
    assert _issues([_parsed()], [view]) == []


def _asset_of(materialize: Materialize) -> list:
    """Build the one asset of `_TABLE` with a way of materializing it.

    Parameters
    ----------
    materialize
        ``table``, ``incremental`` or ``view``.

    Returns
    -------
    list of ParsedAsset
        The asset, which reads ``raw.events``.
    """
    return [make_asset(_TABLE, ("raw.events",), metadata=AssetMetadata(materialize=materialize))]


def test_keep_on_a_check_of_an_incremental_asset_is_refused() -> None:
    assets = _asset_of("incremental")
    issues = check_issues([_parsed(on_failure="keep")], assets, build_graph(assets))
    assert [(issue.path, issue.line, issue.message) for issue in issues] == [
        (
            _PATH,
            None,
            "on_failure: keep cannot be used on `silver.station_temperatures`: it is an "
            "incremental asset, and building it again only adds rows, so the rows that failed "
            "would stay. Use `on_failure: block`: it takes the failed increment out again.",
        )
    ]


def test_the_refusal_of_keep_points_at_the_settings_block_of_the_check() -> None:
    text = "\n/* lakana\non_failure: keep\n*/\nSELECT station_id FROM silver.station_temperatures"
    (parsed,) = parse_checks([_check(text)])
    assets = _asset_of("incremental")

    (issue,) = check_issues([parsed], assets, build_graph(assets))

    assert parsed.block_line == 2
    assert (issue.path, issue.line) == (parsed.path, 2)


def test_keep_on_a_check_of_a_view_asset_is_refused() -> None:
    assert _issues([_parsed(on_failure="keep")], _asset_of("view")) == [
        f"{_PATH}: on_failure: keep cannot be used on `silver.station_temperatures`: it is a "
        "view and holds no data to keep. Use `on_failure: block`: for a view it stops what "
        "reads it in that run."
    ]


def test_every_keep_check_of_an_incremental_asset_gets_its_own_issue() -> None:
    checks = [_parsed("a", on_failure="keep"), _parsed("b"), _parsed("c", on_failure="keep")]
    assets = _asset_of("incremental")
    assert [issue.path for issue in check_issues(checks, assets, build_graph(assets))] == [
        "checks/silver/station_temperatures/a.sql",
        "checks/silver/station_temperatures/c.sql",
    ]


def test_the_refusal_of_keep_escapes_the_table_name() -> None:
    odd = dataclasses.replace(_asset_of("view")[0], table="silver.caf\u00e9")
    check = dataclasses.replace(_parsed(on_failure="keep"), table="silver.caf\u00e9")
    (issue,) = check_issues([check], [odd], build_graph([odd]))
    assert "`silver.caf\\xe9`" in issue.message


def test_keep_on_a_table_asset_a_python_asset_and_a_source_table_is_accepted() -> None:
    python = dataclasses.replace(make_asset("silver.scored"), kind="python")
    assets = [*_asset_of("table"), python]
    checks = [
        _parsed(on_failure="keep"),
        _parsed(table="silver.scored", on_failure="keep"),
        _parsed(table="raw.events", on_failure="keep"),
    ]
    assert _issues(checks, assets) == []


def test_an_asset_that_states_no_materialize_is_a_table_and_takes_keep() -> None:
    plain = [make_asset(_TABLE, metadata=AssetMetadata())]
    assert _issues([_parsed(on_failure="keep")], plain) == []


@pytest.mark.parametrize("materialize", ["incremental", "view"])
@pytest.mark.parametrize("on_failure", ["block", "warn"])
def test_block_and_warn_on_an_incremental_or_view_asset_are_accepted(
    materialize: Materialize, on_failure: str
) -> None:
    assert _issues([_parsed(on_failure=on_failure)], _asset_of(materialize)) == []


def test_the_fifty_first_check_of_a_table_is_refused() -> None:
    assets = _assets(_TABLE)
    many = [_parsed(f"c{i:02d}") for i in range(51)]
    (issue,) = check_issues(many, assets, build_graph(assets))
    assert (issue.path, issue.line, issue.message) == (
        "checks/silver/station_temperatures",
        None,
        "more than 50 checks of table 'silver.station_temperatures'; combine checks or split "
        "the table",
    )
    assert check_issues(many[:50], assets, build_graph(assets)) == []


def test_the_limit_of_checks_per_table_is_a_setting() -> None:
    assets = _assets(_TABLE)
    three = [_parsed(f"c{i}") for i in range(3)]
    (issue,) = check_issues(
        three, assets, build_graph(assets), limits=DeployLimits(max_checks_per_table=2)
    )
    assert issue.message.startswith("more than 2 checks of table")


# -- the manifest ----------------------------------------------------------------------------------


def _manifest_with(*checks: ParsedCheck) -> dict:
    """Build a manifest of one asset and the checks.

    Parameters
    ----------
    *checks
        The checks.

    Returns
    -------
    dict
        The manifest.
    """
    assets = [make_asset(_TABLE, ("raw.events",))]
    return build_manifest(assets, build_graph(assets), ResolvedProjectConfig(), checks=checks)


def test_the_manifest_lists_its_checks() -> None:
    manifest = _manifest_with(
        _parsed("z_last"), dataclasses.replace(_parsed("a_first"), description="Why.")
    )
    assert manifest["version"] == 4
    assert [entry["name"] for entry in manifest["checks"]] == [
        f"{_TABLE}/a_first",
        f"{_TABLE}/z_last",
    ]
    assert set(manifest["checks"][0]) == {
        "name",
        "table",
        "check",
        "path",
        "on_failure",
        "description",
        "sql",
        "references",
    }
    assert manifest["checks"][0]["description"] == "Why."
    assert manifest["checks"][0]["references"] == [_TABLE]


def test_a_manifest_without_checks_is_unchanged() -> None:
    """Version 3 and no `checks` key: the literal of `test_manifest_pins_every_field`."""
    asset = make_asset(
        "silver.clean",
        ("bronze.raw",),
        metadata=AssetMetadata(
            materialize="incremental",
            incremental_key="ts",
            evolve_schema=True,
            description="d",
            owner="me",
            tags=("a", "b"),
            columns={"a": "the a"},
            retries=1,
            retry_delay=10,
            timeout=60,
        ),
    )
    config = ResolvedProjectConfig(repo="analytics")
    manifest = build_manifest([asset], build_graph([asset]), config, caps=RunCaps(), checks=())
    assert manifest == {
        "version": 3,
        "assets": [
            {
                "table": "silver.clean",
                "path": "assets/silver/clean.sql",
                "kind": "sql",
                "materialize": "incremental",
                "write_mode": "append",
                "first_write_mode": "create",
                "incremental_key": "ts",
                "evolve_schema": True,
                "write": "append",
                "key": [],
                "watermark": "ts",
                "retries": 1,
                "retry_delay_seconds": 10,
                "timeout_seconds": 60,
                "metadata": {
                    "description": "d",
                    "owner": "me",
                    "tags": ["a", "b"],
                    "columns": {"a": "the a"},
                },
                "sql": asset.sql,
                "normalized_sql": asset.normalized_sql,
                "references": ["bronze.raw"],
                "columns": ["a"],
                "lineage": {},
            }
        ],
        "edges": [],
        "external_inputs": ["bronze.raw"],
        "config": config.model_dump(mode="json", exclude={"jobs"}),
    }


def test_checks_of_reads_one_tables_checks_sorted() -> None:
    manifest = _manifest_with(
        _parsed("b", on_failure="warn"), _parsed("a"), _parsed("c", "raw.events")
    )
    assert [(spec.check, spec.on_failure) for spec in checks_of(manifest, _TABLE)] == [
        ("a", "block"),
        ("b", "warn"),
    ]
    assert [spec.name for spec in manifest_checks(manifest)] == [
        "raw.events/c",
        f"{_TABLE}/a",
        f"{_TABLE}/b",
    ]
    assert checks_of(manifest, "gold.none") == ()


@pytest.mark.parametrize(
    "manifest", [{"checks": "x"}, {"checks": [1, {"name": 3}]}, None, {"assets": []}]
)
def test_a_damaged_checks_list_reads_as_no_checks(manifest: object) -> None:
    assert manifest_checks(manifest) == ()


@pytest.mark.parametrize(
    ("stored", "read"),
    [("keep", "keep"), ("warn", "warn"), ("block", "block"), ("KEEP", "block"), (7, "block")],
)
def test_a_stored_on_failure_reads_as_itself_or_as_blocking(stored: object, read: str) -> None:
    manifest = {"assets": [{"table": "a.b"}], "checks": [_stored(on_failure=stored)]}
    (spec,) = read_manifest_checks(manifest).specs
    assert spec.on_failure == read


def test_a_damaged_on_failure_reads_as_blocking() -> None:
    entry = {
        "name": "a.b/c",
        "table": "a.b",
        "check": "c",
        "path": "checks/a/b/c.sql",
        "sql": "SELECT 1 FROM a.b",
    }
    manifest = {
        "checks": [
            {**entry, "on_failure": 7, "description": 5},
            {**entry, "name": "a.b/d", "check": "d", "on_failure": "warn", "description": "x"},
        ]
    }
    first, second = manifest_checks(manifest)
    assert (first.on_failure, first.description) == ("block", None)
    assert (second.on_failure, second.description) == ("warn", "x")


def _stored(**override: object) -> dict[str, object]:
    """Build one stored check entry of `a.b`, with some keys replaced.

    Parameters
    ----------
    **override
        Keys to replace or add; a value of `None` removes the key.

    Returns
    -------
    dict
        The entry.
    """
    entry: dict[str, object] = {
        "name": "a.b/c",
        "table": "a.b",
        "check": "c",
        "path": "checks/a/b/c.sql",
        "sql": "SELECT 1 FROM a.b",
        "on_failure": "block",
    }
    entry.update(override)
    return {key: value for key, value in entry.items() if value is not None}


@pytest.mark.parametrize(
    "damaged",
    [_stored(sql=None), _stored(name=3), _stored(check=None), _stored(path=["x"])],
    ids=["no sql", "name not a string", "no check", "path not a string"],
)
def test_an_entry_that_names_its_table_and_cannot_be_read_is_noted_for_that_table(
    damaged: dict[str, object],
) -> None:
    """The other checks are still read; the table is told apart from a table with none."""
    good = _stored(name="a.b/d", check="d")
    read = read_manifest_checks({"assets": [{"table": "a.b"}], "checks": [damaged, good]})
    assert [spec.name for spec in read.specs] == ["a.b/d"]
    assert read.unreadable_tables == {"a.b"}
    assert read.unplaced is False
    assert (read.unreadable_for("a.b"), read.unreadable_for("x.y")) == (True, False)
    assert [spec.name for spec in read.of("a.b")] == ["a.b/d"]


@pytest.mark.parametrize(
    "manifest",
    [
        {"checks": [5]},
        {"checks": ["a.b/c"]},
        {"checks": [_stored(table=3)]},
        {"checks": [_stored(table=None)]},
        {"checks": "x"},
        {"checks": {"a.b/c": _stored()}},
        {"checks": None},
        {"version": 4},
        {"version": 4, "checks": []},
        {"assets": [{"table": "a.b"}], "checks": [_stored(table="", sql=None)]},
        {"assets": [{"table": "a.b"}], "checks": [_stored(table="A.B", sql=None)]},
    ],
    ids=[
        "int",
        "string",
        "table not a string",
        "no table",
        "list is a string",
        "list is a dict",
        "null",
        "version 4 without the key",
        "version 4 with an empty list",
        "table is empty",
        "table matches nothing",
    ],
)
def test_an_entry_that_names_no_table_counts_against_every_table(manifest: object) -> None:
    """It may be any table's, so every table is told it has an entry it cannot read."""
    read = read_manifest_checks(manifest)
    assert read.specs == ()
    assert read.unplaced is True
    assert (read.unreadable_for("a.b"), read.unreadable_for("x.y")) == (True, True)


def test_a_readable_check_of_a_table_the_manifest_does_not_have_fails_closed() -> None:
    """No task would run it, so the table it was meant for would be published unchecked.

    ``A.B`` is not ``a.b``: the entry is whole and consistent, and belongs to no task.
    """
    manifest = {"assets": [{"table": "a.b"}], "checks": [_stored(table="A.B", name="A.B/c")]}
    read = read_manifest_checks(manifest)
    assert read.unplaced is True
    assert read.unreadable_for("a.b") is True


def test_a_readable_check_of_a_table_an_asset_reads_is_placed() -> None:
    """A check of a source table is a table of the manifest through ``references``."""
    manifest = {
        "assets": [{"table": "a.b", "references": ["raw.src"]}],
        "checks": [_stored(table="raw.src", name="raw.src/c")],
    }
    read = read_manifest_checks(manifest)
    assert [spec.table for spec in read.specs] == ["raw.src"]
    assert (read.unreadable_tables, read.unplaced) == (frozenset(), False)


@pytest.mark.parametrize(
    "manifest",
    [
        None,
        "x",
        {"assets": []},
        {"version": 3, "assets": []},
        {"checks": []},
        {"assets": [{"table": "a.b"}], "checks": [_stored()]},
        {"version": 4, "assets": [{"table": "a.b"}], "checks": [_stored()]},
    ],
    ids=[
        "none",
        "not a mapping",
        "no checks key",
        "version 3 without the key",
        "empty",
        "one good check",
        "version 4 with a check",
    ],
)
def test_a_manifest_without_damage_notes_nothing(manifest: object) -> None:
    """No entry that cannot be read, no note."""
    read = read_manifest_checks(manifest)
    assert (read.unreadable_tables, read.unplaced) == (frozenset(), False)
    assert read.unreadable_for("a.b") is False


def test_two_entries_of_one_name_are_both_unreadable() -> None:
    """A result is recorded by name, so the second would replace the first's failure."""
    manifest = {
        "assets": [{"table": "a.b"}],
        "checks": [
            _stored(),
            _stored(sql="SELECT 1 FROM a.b WHERE false"),
            _stored(name="a.b/d", check="d"),
        ],
    }
    read = read_manifest_checks(manifest)
    assert [spec.name for spec in read.specs] == ["a.b/d"]
    assert (read.unreadable_tables, read.unplaced) == ({"a.b"}, False)


@pytest.mark.parametrize(
    "entry",
    [_stored(name="a.b/other"), _stored(name="x.y/c"), _stored(name="c")],
    ids=["other check", "other table", "no table"],
)
def test_an_entry_whose_name_is_not_its_table_and_check_is_unreadable(
    entry: dict[str, object],
) -> None:
    """The name is what a result is recorded and shown under."""
    read = read_manifest_checks({"assets": [{"table": "a.b"}], "checks": [entry]})
    assert read.specs == ()
    assert (read.unreadable_tables, read.unplaced) == ({"a.b"}, False)


@pytest.mark.parametrize("name", ["(none)", "(unreadable)"])
def test_a_check_named_like_a_stand_in_is_refused(tmp_path: Path, name: str) -> None:
    """The two names are what a task's result is called when a check cannot be given."""
    _write(tmp_path, f"checks/silver/t/{name}.sql")
    found, issues = discover_checks(tmp_path, limits=DeployLimits())
    assert found == []
    (issue,) = issues
    assert issue.path == f"checks/silver/t/{name}.sql"
    assert issue.message == (
        f"the check's name '{name}' is reserved: it names the result of a check that cannot be "
        "read. Rename the file."
    )


def test_a_damaged_description_is_dropped_and_is_not_damage() -> None:
    read = read_manifest_checks(
        {"assets": [{"table": "a.b"}], "checks": [_stored(description=5, on_failure=7)]}
    )
    (spec,) = read.specs
    assert (spec.description, spec.on_failure) == (None, "block")
    assert read.unreadable_for("a.b") is False


def test_a_source_check_task_is_made_for_a_table_whose_only_entry_cannot_be_read() -> None:
    """The reader waits for a task that fails, not for a check that is missing."""
    manifest = {
        "assets": [{"table": "silver.a", "references": ["raw.events", "raw.other"]}],
        "checks": [_stored(table="raw.events", sql=None), _stored(table="raw.other")],
    }
    assert source_check_tables(manifest, ["silver.a"]) == {
        "raw.events": ("silver.a",),
        "raw.other": ("silver.a",),
    }


def test_source_check_tables_maps_a_read_table_to_its_selected_readers() -> None:
    assets = [
        make_asset("silver.a", ("raw.events",)),
        make_asset("silver.b", ("raw.events", "raw.other")),
    ]
    assets.append(make_asset("silver.c", ("silver.a",)))
    manifest = build_manifest(
        assets,
        build_graph(assets),
        ResolvedProjectConfig(),
        checks=[_parsed("c1", "raw.events"), _parsed("c2", "silver.a")],
    )
    assert source_check_tables(manifest, ["silver.a", "silver.b"]) == {
        "raw.events": ("silver.a", "silver.b")
    }
    assert source_check_tables(manifest, ["silver.b"]) == {"raw.events": ("silver.b",)}
    assert source_check_tables(manifest, ["silver.c"]) == {}
    assert source_check_tables(manifest, ["not.an.asset"]) == {}


def test_a_damaged_reference_list_reads_as_no_reader() -> None:
    manifest = {
        "assets": [{"table": "silver.a", "references": "raw.events"}],
        "checks": [
            {
                "name": "raw.events/c",
                "table": "raw.events",
                "check": "c",
                "path": "p",
                "sql": "s",
            }
        ],
    }
    assert source_check_tables(manifest, ["silver.a"]) == {}


def test_the_listing_groups_checks_by_table_sorted() -> None:
    listing = checks_listing(
        [_parsed("b"), _parsed("a", on_failure="warn"), _parsed("z", "raw.events")]
    )
    assert listing == {
        "raw.events": [{"check": "z", "on_failure": "block"}],
        _TABLE: [
            {"check": "a", "on_failure": "warn"},
            {"check": "b", "on_failure": "block"},
        ],
    }
    assert list(listing) == ["raw.events", _TABLE]
    assert checks_listing([]) == {}


def test_a_syntax_error_after_a_cut_short_block_carries_the_hint() -> None:
    """A `*/` inside the YAML ends the block early; the hint explains the parse error after it."""
    text = (
        "/* lakana\ndescription: ends with */ here\n*/\nSELECT 1 FROM silver.station_temperatures\n"
    )
    with pytest.raises(ProjectValidationError) as caught:
        parse_checks([_check(text)])
    (issue,) = caught.value.issues
    assert issue.message.startswith("the SQL does not parse:")
    assert "the block ends at the first `*/`, on line 2" in issue.message


def _kept_manifest(*, with_check: bool = True) -> dict[str, Any]:
    """Build a manifest in which `gold.report` reads the built table `silver.orders`.

    Parameters
    ----------
    with_check
        Whether `silver.orders` has a check entry.

    Returns
    -------
    dict
        The manifest.
    """
    return {
        "assets": [
            {"table": "silver.orders", "references": ["raw.src"]},
            {"table": "gold.report", "references": ["silver.orders"]},
            {"table": "gold.other", "references": ["raw.src"]},
        ],
        "checks": (
            [_stored(name="silver.orders/unique_id", table="silver.orders", check="unique_id")]
            if with_check
            else []
        ),
    }


def test_a_kept_table_that_a_selected_asset_reads_gets_a_check_task() -> None:
    assert kept_check_tables(_kept_manifest(), ["gold.report"], {"silver.orders"}) == {
        "silver.orders": ("gold.report",)
    }


def test_a_kept_table_without_a_check_entry_is_included() -> None:
    manifest = _kept_manifest(with_check=False)
    assert kept_check_tables(manifest, ["gold.report"], ["silver.orders"]) == {
        "silver.orders": ("gold.report",)
    }


def test_the_readers_of_a_kept_table_are_sorted() -> None:
    manifest = _kept_manifest()
    manifest["assets"].append({"table": "gold.a_report", "references": ["silver.orders"]})
    found = kept_check_tables(manifest, ["gold.report", "gold.a_report"], {"silver.orders"})
    assert found == {"silver.orders": ("gold.a_report", "gold.report")}


@pytest.mark.parametrize(
    ("selected", "kept"),
    [
        (["silver.orders", "gold.report"], {"silver.orders"}),  # rebuilt in this run
        (["gold.report"], set()),  # no note
        (["gold.other"], {"silver.orders"}),  # no selected reader
        (["gold.other"], {"raw.src"}),  # not an asset: a source table that a selected asset reads
    ],
)
def test_no_check_task_without_need(selected: list[str], kept: set[str]) -> None:
    assert kept_check_tables(_kept_manifest(), selected, kept) == {}


def test_a_damaged_reference_list_reads_as_no_reader_of_a_kept_table() -> None:
    manifest = {
        "assets": [
            {"table": "silver.orders"},
            {"table": "gold.report", "references": "silver.orders"},
        ]
    }
    assert kept_check_tables(manifest, ["gold.report"], {"silver.orders"}) == {}


def _chain(**materialize: str) -> dict[str, Any]:
    """Build a manifest whose assets read each other in a line, with given materializations.

    `silver.orders` <- `silver.v1` <- `silver.v2` <- `gold.report`; `materialize` maps a table to
    its materialization (default `table`), and `silver.orders` has a check.

    Parameters
    ----------
    **materialize
        Materialization by table name with dots written as ``__``.

    Returns
    -------
    dict
        The manifest.
    """
    how = {name.replace("__", "."): value for name, value in materialize.items()}
    line = [("silver.orders", []), ("silver.v1", ["silver.orders"]), ("silver.v2", ["silver.v1"])]
    line.append(("gold.report", ["silver.v2"]))
    return {
        "assets": [
            {"table": t, "references": refs, "materialize": how.get(t, "table")} for t, refs in line
        ],
        "checks": [_stored(name="silver.orders/c", table="silver.orders", check="c")],
    }


def test_a_kept_table_read_through_an_unselected_view_is_checked_for_the_reader() -> None:
    manifest = _chain(silver__v1="view", silver__v2="view")
    found = kept_check_tables(manifest, ["gold.report"], {"silver.orders"})
    assert found == {"silver.orders": ("gold.report",)}  # through two views
    manifest["assets"] = [a for a in manifest["assets"] if a["table"] != "silver.v2"]
    manifest["assets"][-1]["references"] = ["silver.v1"]
    assert kept_check_tables(manifest, ["gold.report"], {"silver.orders"}) == {
        "silver.orders": ("gold.report",)
    }  # through one


def test_a_kept_table_is_not_followed_through_an_unselected_table_asset() -> None:
    manifest = _chain(silver__v2="view")  # silver.v1 is a table: it holds older, checked data
    assert kept_check_tables(manifest, ["gold.report"], {"silver.orders"}) == {}
    incremental = _chain(silver__v1="incremental", silver__v2="view")
    assert kept_check_tables(incremental, ["gold.report"], {"silver.orders"}) == {}


def test_a_selected_view_between_is_a_reader_like_any_other() -> None:
    manifest = _chain(silver__v1="view", silver__v2="view")
    found = kept_check_tables(manifest, ["silver.v1", "gold.report"], {"silver.orders"})
    assert found == {"silver.orders": ("silver.v1",)}  # gold.report waits for silver.v1


def test_a_reference_cycle_of_views_ends() -> None:
    manifest = _chain(silver__v1="view", silver__v2="view")
    manifest["assets"][1]["references"] = ["silver.orders", "silver.v2"]  # v1 <-> v2
    assert kept_check_tables(manifest, ["gold.report"], {"silver.orders"}) == {
        "silver.orders": ("gold.report",)
    }
    lone = {
        "assets": [
            {"table": "silver.orders", "references": []},
            {"table": "silver.v", "references": ["silver.v"], "materialize": "view"},
            {"table": "gold.report", "references": ["silver.v"]},
        ]
    }
    assert kept_check_tables(lone, ["gold.report"], {"silver.orders"}) == {}


def test_a_selected_asset_is_an_upstream_of_what_reads_it_through_an_unselected_view() -> None:
    """`--select silver.orders gold.report`; the reader must come after the rebuild."""
    manifest = _chain(silver__v1="view", silver__v2="view")
    selected = ["silver.orders", "gold.report"]

    assert view_readers(manifest, selected) == {"silver.orders": ("gold.report",)}
    assert kept_check_tables(manifest, selected, {"silver.orders"}) == {}


def test_a_selected_asset_orders_every_reader_sorted_and_never_itself() -> None:
    manifest = _chain(silver__v1="view", silver__v2="view")
    manifest["assets"].append({"table": "gold.a_report", "references": ["silver.v1"]})
    manifest["assets"][0]["references"] = ["silver.orders"]  # an incremental reads itself
    selected = ["gold.report", "silver.orders", "gold.a_report"]

    assert view_readers(manifest, selected) == {"silver.orders": ("gold.a_report", "gold.report")}


@pytest.mark.parametrize(
    ("materialize", "selected"),
    [
        ({"silver__v1": "view", "silver__v2": "view"}, ["gold.report"]),
        ({"silver__v1": "view", "silver__v2": "view"}, ["silver.orders"]),
        ({"silver__v2": "view"}, ["silver.orders", "gold.report"]),
    ],
    ids=["reader alone", "table alone", "behind a table"],
)
def test_no_extra_upstream_without_need(materialize: dict[str, str], selected: list[str]) -> None:
    """Only a selected asset reached through unselected views alone; an unselected table stops."""
    assert view_readers(_chain(**materialize), selected) == {}


def test_a_selected_view_between_them_is_the_reader_and_the_walk_stops_at_it() -> None:
    """`silver.v2` is a task of its own: it comes after the table, and its edge orders the rest."""
    manifest = _chain(silver__v1="view", silver__v2="view")
    selected = ["silver.orders", "silver.v2", "gold.report"]

    assert view_readers(manifest, selected) == {
        "silver.orders": ("silver.v2",),
        "silver.v2": ("gold.report",),
    }


def test_a_direct_reader_of_a_selected_asset_is_named_too() -> None:
    """Its edge orders it already; naming it again costs nothing and keeps the walk one walk."""
    assert view_readers(_kept_manifest(), ["silver.orders", "gold.report"]) == {
        "silver.orders": ("gold.report",)
    }


def test_a_reference_cycle_through_views_ends_the_walk_for_a_selected_asset() -> None:
    manifest = _chain(silver__v1="view", silver__v2="view")
    manifest["assets"][1]["references"] = ["silver.orders", "silver.v2"]

    assert view_readers(manifest, ["silver.orders", "gold.report"]) == {
        "silver.orders": ("gold.report",)
    }


def test_a_malformed_manifest_names_no_reader() -> None:
    assert view_readers([], ["gold.report"]) == {}
    broken = {"assets": [{"table": "silver.orders"}, {"table": "gold.report", "references": 7}]}
    assert view_readers(broken, ["silver.orders", "gold.report"]) == {}


def _two_checks(own: str) -> list[DiscoveredCheck]:
    """Build two checks of one table, one stating `on_failure` and one stating nothing.

    Parameters
    ----------
    own
        The value the first check states.

    Returns
    -------
    list of DiscoveredCheck
        ``own`` (stating `own`) and ``plain`` (no block).
    """
    return [
        _check(f"/* lakana\non_failure: {own}\n*/\nSELECT 1 FROM {_TABLE}", "own"),
        _check(f"SELECT 1 FROM {_TABLE}", "plain"),
    ]


def test_a_check_takes_the_default_and_its_own_value_wins() -> None:
    parsed = parse_checks(_two_checks(own="block"), default_on_failure="warn")
    assert [(c.name, c.on_failure, c.from_default) for c in parsed] == [
        (f"{_TABLE}/own", "block", False),
        (f"{_TABLE}/plain", "warn", True),
    ]


def test_a_block_without_on_failure_takes_the_default() -> None:
    text = f"/* lakana\ndescription: d\n*/\nSELECT 1 FROM {_TABLE}"
    (parsed,) = parse_checks([_check(text)], default_on_failure="warn")
    assert (parsed.on_failure, parsed.from_default) == ("warn", True)


def test_a_default_of_block_marks_nothing() -> None:
    parsed = parse_checks(_two_checks(own="keep"))
    assert [(c.on_failure, c.from_default) for c in parsed] == [("keep", False), ("block", False)]


def test_the_listing_names_lakana_yaml_only_for_a_default() -> None:
    plain = parse_checks(_two_checks(own="warn"))
    assert checks_listing(plain) == {
        _TABLE: [
            {"check": "own", "on_failure": "warn"},
            {"check": "plain", "on_failure": "block"},
        ]
    }
    defaulted = parse_checks(_two_checks(own="warn"), default_on_failure="warn")
    assert checks_listing(defaulted) == {
        _TABLE: [
            {"check": "own", "on_failure": "warn"},
            {"check": "plain", "on_failure": "warn", "from": "lakana.yaml"},
        ]
    }


def test_the_listing_names_where_a_generated_check_comes_from() -> None:
    generated = ParsedCheck(
        name="gold.fact_sales/key_unique",
        table="gold.fact_sales",
        check="key_unique",
        path="models/gold/fact_sales.sql:3",
        on_failure="block",
        description=None,
        sql="SELECT 1",
        references=("gold.fact_sales",),
        body_line=1,
        generated=GeneratedFrom("key", ("id",)),
    )
    assert checks_listing([generated]) == {
        "gold.fact_sales": [{"check": "key_unique", "on_failure": "block", "generated": "key"}]
    }


@pytest.mark.parametrize(
    ("yaml_text", "environment"),
    [
        ("defaults:\n  materialize: view\n", "dev"),
        (
            "defaults:\n  materialize: table\n"
            "environments:\n  dev:\n    defaults:\n      materialize: view\n",
            "dev",
        ),
    ],
    ids=["project default", "environment override"],
)
def test_keep_is_refused_for_a_materialize_that_comes_from_lakana_yaml(
    yaml_text: str, environment: str
) -> None:
    """The deploy resolves the file's defaults and the environment's override first."""
    config = parse_project_config(yaml_text).resolved_for(environment)
    assets = parse_assets(
        [DiscoveredAsset("assets/silver/station_temperatures.sql", _TABLE, "SELECT 1 FROM raw.e")],
        defaults=config.defaults,
    )
    (check,) = parse_checks(
        [_check(f"/* lakana\non_failure: keep\n*/\nSELECT 1 FROM {_TABLE}")],
        default_on_failure=config.checks.on_failure,
    )

    (issue,) = check_issues([check], assets, build_graph(assets))

    assert "it is a view" in issue.message


def test_keep_on_a_check_of_an_append_asset_is_refused_as_for_the_old_spelling() -> None:
    metadata = AssetMetadata(materialize="table", write="append", watermark="ts")
    assets = [make_asset(_TABLE, ("raw.events",), metadata=metadata)]
    assert _issues([_parsed(on_failure="keep")], assets) == [
        f"{_PATH}: on_failure: keep cannot be used on `silver.station_temperatures`: it is an "
        "incremental asset, and building it again only adds rows, so the rows that failed "
        "would stay. Use `on_failure: block`: it takes the failed increment out again."
    ]


def test_keep_on_a_check_of_a_merge_asset_is_refused() -> None:
    metadata = AssetMetadata(materialize="table", write="merge", key=("id",))
    assets = [make_asset(_TABLE, ("raw.events",), metadata=metadata)]
    assert _issues([_parsed(on_failure="keep")], assets) == [
        f"{_PATH}: on_failure: keep cannot be used on `silver.station_temperatures`: it is "
        "written with `write: merge`, and building it again never deletes a row, so the rows "
        "that failed would stay. Use `on_failure: block`: it takes the failed merge out again."
    ]


# -- generated checks in the manifest -------------------------------------------------------------

_KEY_FROM = {"family": "key", "columns": ["id"]}
_REF_FROM = {"family": "references", "columns": ["s"], "target": "x.y", "target_column": "id"}


def test_a_generated_check_records_where_it_came_from_and_a_hand_written_one_does_not() -> None:
    """Only a generated entry gets the `generated` key, so every older entry keeps its shape."""
    generated = dataclasses.replace(
        _parsed("ref_s"), generated=GeneratedFrom("references", ("s",), "x.y", "id")
    )
    manifest = _manifest_with(_parsed("hand"), generated)
    entries = {entry["check"]: entry for entry in manifest["checks"]}
    assert "generated" not in entries["hand"]
    assert entries["ref_s"]["generated"] == _REF_FROM
    keyed = dataclasses.replace(_parsed("key_unique"), generated=GeneratedFrom("key", ("id",)))
    (entry,) = _manifest_with(keyed)["checks"]
    assert entry["generated"] == {**_KEY_FROM, "target": None, "target_column": None}


@pytest.mark.parametrize("generated", [_KEY_FROM, _REF_FROM, {**_KEY_FROM, "target": None}])
def test_a_well_formed_generated_entry_reads_back(generated: dict[str, object]) -> None:
    manifest = {"assets": [{"table": "a.b"}], "checks": [_stored(generated=generated)]}
    (spec,) = read_manifest_checks(manifest).specs
    assert spec.generated == GeneratedFrom(
        cast("str", generated["family"]),
        tuple(cast("list[str]", generated["columns"])),
        cast("str | None", generated.get("target")),
        cast("str | None", generated.get("target_column")),
    )
    assert (
        read_manifest_checks({"assets": [{"table": "a.b"}], "checks": [_stored()]})
        .specs[0]
        .generated
        is None
    )


@pytest.mark.parametrize(
    "generated",
    [
        "key",
        None,
        {**_KEY_FROM, "family": "rows"},
        {**_KEY_FROM, "columns": []},
        {**_KEY_FROM, "columns": "id"},
        {**_KEY_FROM, "columns": [1]},
        {"columns": ["id"]},
        {**_KEY_FROM, "target": "x.y", "target_column": "id"},
        {**_REF_FROM, "target": None},
        {**_REF_FROM, "target_column": 3},
    ],
    ids=[
        "a string",
        "null",
        "unknown family",
        "no columns",
        "columns a string",
        "column not a string",
        "no family",
        "key with a target",
        "reference without a target",
        "target column not a string",
    ],
)
def test_a_damaged_generated_entry_is_unreadable_for_its_table(generated: object) -> None:
    entry: dict[str, object] = {**_stored(), "generated": generated}
    read = read_manifest_checks({"assets": [{"table": "a.b"}], "checks": [entry]})
    assert read.specs == ()
    assert read.unreadable_tables == {"a.b"}


_MEASURES_FROM = {"family": "measures", "columns": ["a", "b"], "aggregations": ["sum", "max"]}


@pytest.mark.parametrize(
    ("generated", "expected"),
    [
        (
            {**_MEASURES_FROM, "not_over": ["d"]},
            GeneratedFrom("measures", ("a", "b"), not_over=("d",), aggregations=("sum", "max")),
        ),
        (_MEASURES_FROM, GeneratedFrom("measures", ("a", "b"), aggregations=("sum", "max"))),
        ({**_MEASURES_FROM, "target": "gold.x", "target_column": "id"}, None),
        ({**_MEASURES_FROM, "not_over": "d"}, None),
        ({**_MEASURES_FROM, "aggregations": None}, None),
        ({**_MEASURES_FROM, "aggregations": ["sum"]}, None),
        ({**_MEASURES_FROM, "aggregations": ["sum", "median"]}, None),
        ({**_MEASURES_FROM, "aggregations": "sum"}, None),
        ({"family": "key", "columns": ["a"], "not_over": ["d"]}, None),
        ({"family": "key", "columns": ["a"], "aggregations": ["sum"]}, None),
    ],
)
def test_a_stored_measures_entry_reads_back_or_is_unreadable(
    generated: dict[str, object], expected: GeneratedFrom | None
) -> None:
    read = read_manifest_checks(
        {"assets": [{"table": "a.b"}], "checks": [_stored(generated=generated)]}
    )
    assert [spec.generated for spec in read.specs] == ([] if expected is None else [expected])
    assert read.unreadable_tables == (set() if expected is not None else {"a.b"})
