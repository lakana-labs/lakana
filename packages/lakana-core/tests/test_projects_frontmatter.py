"""SQL front-matter: the `/* lakana … */` block."""

from __future__ import annotations

import pytest

from lakana_core.projects.frontmatter import (
    AssetMetadata,
    consistency_issues,
    merge_metadata,
    parse_front_matter,
    split_front_matter,
)
from lakana_core.projects.issues import ProjectValidationError
from lakana_core.projects.limits import DeployLimits
from lakana_core.projects.project_config import AssetDefaults

_PATH = "assets/silver/weather.sql"

_FULL = """\
/* lakana
description: Cleaned hourly weather observations, nulls dropped.
owner: data-eng
tags: [weather, hourly]
materialize: incremental
incremental_key: ts
evolve_schema: false
columns:
  station_id: WMO station identifier.
*/
SELECT station_id, ts, temp_c FROM bronze.weather WHERE temp_c IS NOT NULL
"""


def test_full_block_parses_and_body_follows() -> None:
    """Every documented key parses; the body starts on the line after `*/`."""
    parsed = parse_front_matter(_FULL, path=_PATH)
    assert parsed.metadata.materialize == "incremental"
    assert parsed.metadata.tags == ("weather", "hourly")
    assert parsed.metadata.columns == {"station_id": "WMO station identifier."}
    assert parsed.body.strip().startswith("SELECT station_id")
    assert parsed.block_line == 1
    assert parsed.body_line == 10


def test_leading_whitespace_before_the_block_is_allowed() -> None:
    """Whitespace may precede the block."""
    parsed = parse_front_matter("\n\n  /* lakana\nowner: x\n*/\nSELECT 1", path=_PATH)
    assert parsed.metadata.owner == "x"
    assert parsed.block_line == 3


def test_no_block_means_empty_metadata() -> None:
    """A file with no block takes the defaults."""
    parsed = parse_front_matter("SELECT 1 AS a\n", path=_PATH)
    assert parsed.metadata == AssetMetadata()
    assert parsed.block_line is None
    assert parsed.body_line == 1


def test_an_ordinary_leading_comment_is_not_front_matter() -> None:
    """`/* hello */` is SQL, not a lakana block."""
    parsed = parse_front_matter("/* hello */ SELECT 1", path=_PATH)
    assert parsed.metadata == AssetMetadata()
    assert parsed.body.startswith("/* hello */")


def test_empty_block_means_empty_metadata() -> None:
    """A `/* lakana` block with no content is valid and sets nothing."""
    parsed = parse_front_matter("/* lakana\n*/\nSELECT 1", path=_PATH)
    assert parsed.metadata == AssetMetadata()
    assert parsed.block_line == 1


def test_block_not_first_is_an_error() -> None:
    """A lakana block after SQL is refused with its line."""
    with pytest.raises(ProjectValidationError, match="must be the first thing") as info:
        parse_front_matter("SELECT 1\n/* lakana\nowner: x\n*/\n", path=_PATH)
    assert info.value.issues[0].line == 2


def test_unterminated_block_is_an_error() -> None:
    """A block with no closing `*/` is refused."""
    with pytest.raises(ProjectValidationError, match="never closed"):
        parse_front_matter("/* lakana\nowner: x\nSELECT 1\n", path=_PATH)


def test_unknown_key_is_an_error() -> None:
    """Strict: no `name` or `target` — the path decides."""
    with pytest.raises(ProjectValidationError, match="target: Extra inputs") as info:
        parse_front_matter("/* lakana\ntarget: gold.x\n*/\nSELECT 1", path=_PATH)
    assert info.value.issues[0].line == 1


def test_anchor_in_block_is_an_error_on_the_file_line() -> None:
    """An anchor inside the block is refused; the line is the file's, not the block's."""
    with pytest.raises(ProjectValidationError, match="anchors and aliases") as info:
        parse_front_matter("\n/* lakana\nowner: &o x\n*/\nSELECT 1", path=_PATH)
    assert info.value.issues[0].line == 3


def test_block_over_the_limit_is_an_error() -> None:
    """Each block is bounded."""
    big = "/* lakana\ndescription: " + "x" * 2048 + "\n*/\nSELECT 1"
    with pytest.raises(ProjectValidationError, match="larger than 1 KiB"):
        parse_front_matter(big, path=_PATH, limits=DeployLimits(yaml_bytes=1024))


def test_block_must_be_a_mapping() -> None:
    """A scalar block is refused."""
    with pytest.raises(ProjectValidationError, match="must be a mapping"):
        parse_front_matter("/* lakana\njust text\n*/\nSELECT 1", path=_PATH)


def test_crlf_and_bom_parse_identically() -> None:
    """Windows line endings and a BOM change nothing."""
    windows = "﻿" + _FULL.replace("\n", "\r\n")
    assert parse_front_matter(windows, path=_PATH) == parse_front_matter(_FULL, path=_PATH)


def test_merge_takes_front_matter_over_defaults_and_defaults_materialize() -> None:
    """Own values win; a missing materialize becomes `table`."""
    defaults = AssetDefaults(owner="data-eng", description="d", tags=("a",))
    merged = merge_metadata(defaults, AssetMetadata(owner="me"))
    assert merged.owner == "me"
    assert merged.description == "d"
    assert merged.tags == ("a",)
    assert merged.materialize == "table"
    assert merge_metadata(AssetDefaults(materialize="view"), AssetMetadata()).materialize == "view"


@pytest.mark.parametrize(
    ("metadata", "message"),
    [
        (AssetMetadata(materialize="incremental"), "incremental_key is required"),
        (AssetMetadata(materialize="table", incremental_key="ts"), "only with materialize"),
        (
            AssetMetadata(materialize="view", evolve_schema=True),
            "`evolve_schema` works with `write: append` only",
        ),
    ],
)
def test_inconsistent_metadata_is_reported(metadata: AssetMetadata, message: str) -> None:
    """`incremental_key` iff incremental; `evolve_schema` only with incremental."""
    issues = consistency_issues(metadata, path=_PATH, line=1)
    assert [issue.path for issue in issues] == [_PATH]
    assert message in issues[0].message


def test_consistent_incremental_metadata_has_no_issues() -> None:
    """The happy incremental case."""
    metadata = AssetMetadata(materialize="incremental", incremental_key="ts", evolve_schema=True)
    assert consistency_issues(metadata, path=_PATH, line=1) == []


@pytest.mark.parametrize("block", ["description: 2024-13-45", "tags: " + "[" * 1000 + "]" * 1000])
def test_hostile_block_is_a_project_validation_error(block: str) -> None:
    """Constructor errors and deep nesting in a block are issues on the asset file."""
    with pytest.raises(ProjectValidationError) as info:
        parse_front_matter(f"/* lakana\n{block}\n*/\nSELECT 1", path=_PATH)
    assert info.value.issues[0].path == _PATH


def test_run_settings_in_front_matter_and_their_merge() -> None:
    """Front-matter run settings win over defaults; unset ones inherit."""
    front = parse_front_matter(
        "/* lakana\nretries: 0\ntimeout: 90s\n*/\nSELECT 1\n", path=_PATH
    ).metadata
    merged = merge_metadata(AssetDefaults(retries=3, retry_delay=15, timeout=30), front)
    assert (merged.retries, merged.retry_delay, merged.timeout) == (0, 15, 90)


@pytest.mark.parametrize("value", ['"false"', "0", '"no"'])
def test_evolve_schema_must_be_a_real_boolean(value: str) -> None:
    """A quoted or numeric value is refused rather than coerced to a boolean."""
    head = "/* lakana\nmaterialize: incremental\nincremental_key: ts\n"
    block = f"{head}evolve_schema: {value}\n*/\nSELECT 1"
    with pytest.raises(ProjectValidationError, match="evolve_schema"):
        parse_front_matter(block, path=_PATH)


def test_duplicate_key_in_block_is_an_error_on_the_file_line() -> None:
    """The line of the second key is the file's, not the block's."""
    with pytest.raises(ProjectValidationError, match="duplicate key 'owner'") as info:
        parse_front_matter("\n/* lakana\nowner: a\nowner: b\n*/\nSELECT 1", path=_PATH)
    assert info.value.issues[0].line == 4


def test_merge_key_in_block_is_an_error() -> None:
    """`<<` is refused in front-matter too."""
    with pytest.raises(ProjectValidationError, match="merge keys"):
        parse_front_matter("/* lakana\n<<: {owner: x}\n*/\nSELECT 1", path=_PATH)


def test_a_star_slash_inside_a_string_names_the_cause() -> None:
    """The block ends at the first `*/`; the YAML error says so instead of just failing."""
    text = '/* lakana\ndescription: "ends */ early"\nowner: x\n*/\nSELECT 1'
    with pytest.raises(ProjectValidationError, match=r"the block ends at the first `\*/`") as info:
        parse_front_matter(text, path=_PATH)
    assert "line 2" in str(info.value)


_HINT = r"the block ends at the first `\*/`"


def test_an_unquoted_star_slash_leaves_a_hint_on_the_front_matter() -> None:
    """The block parses (shortened); the hint is carried for the SQL error, never raised."""
    text = "/* lakana\ndescription: ends */ early\nowner: x\n*/\nSELECT 1"
    front = parse_front_matter(text, path=_PATH)
    assert front.cut_hint is not None
    assert "line 2" in front.cut_hint and "first `*/`" in front.cut_hint


def test_a_star_slash_inside_a_block_scalar_leaves_a_hint() -> None:
    """A `|` scalar containing `a/*/b` is cut at the `*/` in the middle of it."""
    text = "/* lakana\ndescription: |\n  path a/*/b is odd\nowner: x\n*/\nSELECT 1"
    assert parse_front_matter(text, path=_PATH).cut_hint is not None


def test_an_unrelated_yaml_error_carries_no_star_slash_hint() -> None:
    """The hint is only for a block cut short, not for every YAML syntax error."""
    with pytest.raises(ProjectValidationError, match="not valid YAML") as info:
        parse_front_matter("/* lakana\nowner: [x\n*/\nSELECT 1", path=_PATH)
    assert "`*/`" not in str(info.value)


def test_a_comment_in_the_body_carries_no_hint() -> None:
    """`/* … */` after the block is ordinary SQL."""
    parsed = parse_front_matter("/* lakana\nowner: x\n*/\nSELECT 1 /* note */", path=_PATH)
    assert parsed.metadata.owner == "x"
    assert parsed.cut_hint is None


@pytest.mark.parametrize(
    ("key", "value", "declaration"),
    [
        ("output", "artifact", '@asset(output="artifact")'),
        ("output", "model", '@asset(output="artifact")'),
        ("output", "3", '@asset(output="artifact")'),
        ("keep_versions", "2", '@asset(output="artifact", keep_versions=N)'),
        ("keep_versions", "[1, 2]", '@asset(output="artifact", keep_versions=N)'),
        ("keep_versions", "{a: b}", '@asset(output="artifact", keep_versions=N)'),
        ("decimals", "exact", '@asset(decimals="exact")'),
        ("decimals", "3", '@asset(decimals="exact")'),
        ("connections", "[crm-db]", '@asset(connections=["crm-db"])'),
        ("connections", "crm-db", '@asset(connections=["crm-db"])'),
    ],
    ids=[
        "str",
        "other-str",
        "int",
        "keep-int",
        "keep-list",
        "keep-map",
        "decimals",
        "dec-int",
        "connections",
        "connections-str",
    ],
)
def test_a_python_only_key_in_sql_front_matter_names_the_next_step(
    key: str, value: str, declaration: str
) -> None:
    """`output` and `keep_versions` are Python decorator arguments; the issue says so."""
    text = f"/* lakana\n{key}: {value}\n*/\nSELECT 1"
    with pytest.raises(ProjectValidationError) as info:
        parse_front_matter(text, path=_PATH, limits=DeployLimits(yaml_bytes=1 << 20))
    [issue] = info.value.issues
    assert issue.line == 1
    assert issue.message == (
        f"`{key}` applies only to a Python asset, not to a SQL file; declare the asset in a "
        f"`.py` file with `{declaration}`, or remove `{key}` from this block"
    )


def test_a_python_only_key_is_reported_with_the_other_problems_of_the_block() -> None:
    """One deploy shows every problem of the block, in one pass."""
    text = "/* lakana\noutput: artifact\nkeep_versions: 2\ntarget: x\n*/\nSELECT 1"
    with pytest.raises(ProjectValidationError) as info:
        parse_front_matter(text, path=_PATH)
    assert [issue.message.split(" ")[0] for issue in info.value.issues] == [
        "`output`",
        "`keep_versions`",
        "target:",
    ]


@pytest.mark.parametrize("key", ["output", "keep_versions"])
def test_a_huge_number_in_the_block_is_an_issue_and_is_never_printed(key: str) -> None:
    """The YAML loader refuses it first; no exception, and the digits stay out of the text."""
    text = f"/* lakana\n{key}: {'9' * 5000}\n*/\nSELECT 1"
    with pytest.raises(ProjectValidationError) as info:
        parse_front_matter(text, path=_PATH, limits=DeployLimits(yaml_bytes=1 << 20))
    [issue] = info.value.issues
    assert issue.path == _PATH
    assert "9999" not in issue.message


def test_split_front_matter_returns_the_raw_mapping() -> None:
    """The mapping is returned as written, unvalidated; the body follows the block."""
    text = "/* lakana\nseverity: high\non_failure: warn\n*/\nSELECT 1\n"
    split = split_front_matter(text, path=_PATH)
    assert split.raw == {"severity": "high", "on_failure": "warn"}
    assert (split.body, split.body_line, split.block_line, split.cut_hint) == (
        "\nSELECT 1\n",
        4,
        1,
        None,
    )


def test_split_front_matter_without_a_block_returns_the_whole_text() -> None:
    split = split_front_matter("SELECT 1\n", path=_PATH)
    assert (split.raw, split.body, split.body_line, split.block_line) == ({}, "SELECT 1\n", 1, None)


def _merged(block: str) -> AssetMetadata:
    """Parse a block and apply empty defaults, as deploy does.

    Parameters
    ----------
    block
        The block's YAML lines.

    Returns
    -------
    AssetMetadata
        The merged metadata.
    """
    front = parse_front_matter(f"/* lakana\n{block}*/\nSELECT 1 AS id", path=_PATH)
    return merge_metadata(AssetDefaults(), front.metadata)


def test_a_merge_block_parses() -> None:
    metadata = _merged("write: merge\nkey: [id, region]\nwatermark: updated_at\n")
    assert (metadata.write, metadata.key, metadata.watermark) == (
        "merge",
        ("id", "region"),
        "updated_at",
    )
    assert consistency_issues(metadata, path=_PATH, line=1) == []
    appended = _merged("write: append\nwatermark: ts\nevolve_schema: true\n")
    assert consistency_issues(appended, path=_PATH, line=1) == []


@pytest.mark.parametrize(
    ("block", "message"),
    [
        ("materialize: view\nwrite: replace\n", "`materialize: view` cannot take `write:`"),
        (
            "materialize: incremental\nincremental_key: ts\nwrite: append\nwatermark: ts\n",
            "use one: `write: append` with `watermark:` replaces `materialize: incremental`",
        ),
        (
            "write: merge\nkey: [id]\nevolve_schema: true\n",
            "`evolve_schema` works with `write: append` only",
        ),
        (
            "write: append\n",
            "`write: append` needs `watermark:` — the column whose largest value the next run "
            "starts after",
        ),
        ("write: merge\n", "`write: merge` needs `key:` — the columns that identify a row"),
        (
            "watermark: ts\n",
            "`write: replace` takes no `watermark:`; to add only new rows use `write: append`",
        ),
        ("write: merge\nkey: [id, id]\n", "key column `id` is listed twice"),
    ],
)
def test_a_wrong_write_model_in_a_block_is_reported(block: str, message: str) -> None:
    issues = consistency_issues(_merged(block), path=_PATH, line=1)
    assert [(issue.path, issue.line, issue.message) for issue in issues] == [(_PATH, 1, message)]


@pytest.mark.parametrize(
    ("block", "message"),
    [
        ("write: upsert\n", "write: must be one of replace, append, merge; got 'upsert'"),
        (
            'write: merge\nkey: ["i\\u200bd"]\n',
            "key: a key column must not contain a control, format or surrogate character or a "
            "line separator",
        ),
        (
            'write: append\nwatermark: "t\\u2028s"\n',
            "watermark: watermark must not contain a control, format or surrogate character or "
            "a line separator",
        ),
    ],
)
def test_a_wrong_write_value_in_a_block_is_refused(block: str, message: str) -> None:
    with pytest.raises(ProjectValidationError) as caught:
        _merged(block)
    assert [issue.message for issue in caught.value.issues] == [message]


def _meta(block: str) -> AssetMetadata:
    """Parse a block and return its metadata.

    Parameters
    ----------
    block
        The YAML between the markers.

    Returns
    -------
    AssetMetadata
        The parsed metadata.
    """
    return parse_front_matter(f"/* lakana\n{block}\n*/\nSELECT 1 AS id", path=_PATH).metadata


def test_the_model_fields_parse() -> None:
    """kind, grain, key, references and checks are read from the block."""
    meta = _meta(
        "kind: fact\ngrain: One row per store and calendar day.\nkey: [date, store_id]\n"
        "references:\n  store_id: gold.dim_store\n  sku: gold.dim_product.sku\n"
        "checks:\n  key: block\n  references: warn"
    )
    assert meta.kind == "fact"
    assert meta.grain == "One row per store and calendar day."
    assert meta.key == ("date", "store_id")
    assert meta.references == {"store_id": "gold.dim_store", "sku": "gold.dim_product.sku"}
    assert meta.checks == {"key": "block", "references": "warn"}


def test_key_none_is_the_escape_and_a_listed_none_is_a_column() -> None:
    """`key: none` is the escape; `key: [none]` names a column; `key: id` is one column."""
    assert _meta("key: none").key == "none"
    assert _meta("key: [none]").key == ("none",)
    assert _meta("key: id").key == ("id",)


@pytest.mark.parametrize(
    ("block", "message"),
    [
        ("kind: cube", "kind: must be one of fact, dimension, wide, staging; got 'cube'"),
        ("grain: ''", "grain: must not be empty"),
        ("grain: |\n  two\n  lines", "grain: must be one line"),
        ("grain: " + "x" * 301, "grain: must be at most 300 characters"),
        ("checks:\n  rows: warn", "checks: takes only key and references"),
        ("checks:\n  key: loud", "checks: key must be block, keep or warn"),
        ("references:\n  store_id: dim_store", "references: reference `store_id: dim_store`"),
        ('grain: "a\\u202eb"', "grain: must not contain a surrogate"),
        ('references:\n  "a\\u200bb": gold.dim_store', "references: a reference column must not"),
    ],
)
def test_bad_model_values_are_refused(block: str, message: str) -> None:
    """Each refused value names the field and the fix, as `<field>: <message>`."""
    with pytest.raises(ProjectValidationError) as info:
        _meta(block)
    assert message in info.value.issues[0].message


def test_a_one_line_grain_written_as_a_block_scalar_is_one_line() -> None:
    """`grain: |` with one line ends in a newline; the rule reads the stripped text."""
    assert _meta("grain: |\n  One row per order.").grain is not None


def test_a_reference_column_that_cannot_name_its_check_is_refused() -> None:
    """A reference column must make a valid check name `ref_<column>`."""
    with pytest.raises(ProjectValidationError) as info:
        _meta("references:\n  a/b: gold.dim_store")
    assert "reference column `a/b` cannot name its check `ref_a/b`" in info.value.issues[0].message


def test_an_explicit_null_grain_is_no_grain() -> None:
    """`grain: null` reads as no grain."""
    assert _meta("grain: null").grain is None
