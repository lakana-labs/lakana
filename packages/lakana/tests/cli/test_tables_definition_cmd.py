r"""`lakana table definition`: where a table is defined and, with `--show`, its file.

Every invisible character is written as a `\u` or `\x` escape, so this file stays ASCII.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from conftest import FakeApi, invoke, make_state
from typer.testing import Result

_DEFINITION = "/api/v1/environments/dev/namespaces/silver/tables/orders/definition"
_COMMIT = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
_LINK = "https://github.com/acme/analytics/blob/" + _COMMIT + "/assets/silver/orders.sql#L1"

LOCATION: dict[str, Any] = {
    "defined": True,
    "repository": "analytics",
    "path": "assets/silver/orders.sql",
    "line": 1,
    "commit": _COMMIT,
    "project_version_id": "6f1c1d1e-0000-4000-8000-000000000001",
    "deployed_at": "2026-11-12T10:00:00Z",
    "browse_url": _LINK,
}
"""A table a deployed project defines, as the REST API answers it."""

NOT_DEFINED: dict[str, Any] = {"defined": False, **{k: None for k in LOCATION if k != "defined"}}
"""A table no current deployment defines."""

LINES = (
    "Defined in assets/silver/orders.sql, line 1\n"
    "Repository  analytics\n"
    "Commit      a1b2c3d (deployed 2026-11-12 10:00 UTC)\n"
)
"""The location lines without the link."""

RESTRICTED = (
    "The file is shown only to people who may read this environment and every table the file "
    "uses, without a row filter or column mask. Open it in your git host if you have access "
    "there, or ask an administrator."
)
NOT_RECORDED = (
    "Lakana has not recorded this file for the deployed version: it was deployed before Lakana "
    "kept project files, or its code no longer matches what this release records. Deploy a new "
    "commit to record the file."
)
TOO_LARGE = "The file is larger than 1 MiB, so it is not shown here. Open it in your git host."


def _invoke(
    tmp_path: Path, location: dict[str, Any], *args: str, file: dict[str, Any] | None = None
) -> Result:
    """Fake both definition routes and run `table definition silver.orders --env dev`.

    Parameters
    ----------
    tmp_path
        Pytest's per-test directory.
    location
        The answer of the location route, and the location part of the file route's.
    *args
        More arguments, such as `--show`; `--output json` goes first when given.
    file
        The file part of the file route's answer; no content by default.

    Returns
    -------
    Result
        The CLI's result.
    """
    api = FakeApi()
    api.on("GET", _DEFINITION, json=location)
    blank = {"content": None, "bytes": None, "reason": None, "message": None}
    api.on("GET", _DEFINITION + "/file", json={**location, **blank, **(file or {})})
    head = [a for a in args if a in ("--output", "json")]
    tail = [a for a in args if a not in head]
    return invoke(
        make_state(tmp_path, api),
        *head,
        "table",
        "definition",
        "silver.orders",
        *tail,
        "--env",
        "dev",
    )


def invoke_show(tmp_path: Path, **file: Any) -> Result:  # ruff: ignore[any-type]
    """Run `table definition silver.orders --show --env dev` against a faked file answer.

    Parameters
    ----------
    tmp_path
        Pytest's per-test directory.
    **file
        The file part of the answer: `content`, and `reason` and `message` for none.

    Returns
    -------
    Result
        The CLI's result.
    """
    content = file.get("content")
    shown = {"bytes": None if content is None else len(content.encode()), **file}
    return _invoke(tmp_path, LOCATION, "--show", file=shown)


def test_definition_prints_the_location_and_link(tmp_path: Path) -> None:
    result = _invoke(tmp_path, LOCATION)
    assert result.exit_code == 0, result.stderr
    assert result.stdout == LINES + f"Open        {_LINK}\n"
    assert result.stderr == ""


def test_definition_leaves_out_the_link_when_there_is_none(tmp_path: Path) -> None:
    result = _invoke(tmp_path, {**LOCATION, "browse_url": None})
    assert result.exit_code == 0, result.stderr
    assert result.stdout == LINES
    assert "Open" not in result.stdout + result.stderr


def test_definition_without_a_deploy_time_leaves_out_the_parenthesis(tmp_path: Path) -> None:
    result = _invoke(tmp_path, {**LOCATION, "deployed_at": None, "browse_url": None})
    assert "Commit      a1b2c3d\n" in result.stdout


def test_definition_of_an_undefined_table_says_so_with_quoted_names(tmp_path: Path) -> None:
    result = _invoke(tmp_path, NOT_DEFINED)
    assert result.exit_code == 0, result.stderr
    assert result.stdout == (
        "'silver.orders' is not defined by a project deployed to 'dev'; it was created or "
        "renamed outside a project.\n"
    )


def test_definition_show_of_an_undefined_table_says_so_on_stderr(tmp_path: Path) -> None:
    result = _invoke(tmp_path, NOT_DEFINED, "--show")
    assert result.exit_code == 0, result.stderr
    assert result.stdout == ""
    assert "'silver.orders' is not defined by a project deployed to 'dev'" in result.stderr


def test_definition_location_escapes_a_forged_path(tmp_path: Path) -> None:
    forged = {
        **LOCATION,
        "path": "a\x1b[31m.sql\nOpen        https://evil.example",
        "repository": "r\u202eepo",
        "browse_url": "https://h.example/\x1b]8;;https://evil.example\x07x",
    }
    result = _invoke(tmp_path, forged)
    assert result.stdout.splitlines() == [
        "Defined in a\\x1b[31m.sql\\nOpen        https://evil.example, line 1",
        "Repository  r\\u202eepo",
        "Commit      a1b2c3d (deployed 2026-11-12 10:00 UTC)",
        "Open        https://h.example/\\x1b]8;;https://evil.example\\x07x",
    ]


def test_definition_show_writes_the_location_to_stderr_and_the_file_to_stdout(
    tmp_path: Path,
) -> None:
    result = invoke_show(tmp_path, content="SELECT 1 AS id\n")
    assert result.exit_code == 0, result.stderr
    assert result.stdout == "SELECT 1 AS id\n"
    assert result.stderr == LINES + f"Open        {_LINK}\n"


def test_definition_show_escapes_control_characters(tmp_path: Path) -> None:
    out = invoke_show(tmp_path, content="SELECT 1\x1b[2J\r\n\x00\x85\tok\n")
    assert out.stdout == "SELECT 1\\x1b[2J\\x0d\n\\x00\\x85\tok\n"


def test_definition_show_escapes_terminal_sequences(tmp_path: Path) -> None:
    content = (
        "-- \x1b]8;;https://evil.example\x1b\\click\x1b]8;;\x1b\\\n"
        "-- \x07\x9b31m\x1b[1A\x1b[2K\rSELECT 2\n"
    )
    out = invoke_show(tmp_path, content=content)
    assert out.stdout == (
        "-- \\x1b]8;;https://evil.example\\x1b\\click\\x1b]8;;\\x1b\\\n"
        "-- \\x07\\x9b31m\\x1b[1A\\x1b[2K\\x0dSELECT 2\n"
    )


def test_definition_show_escapes_bidi_and_format_characters(tmp_path: Path) -> None:
    out = invoke_show(tmp_path, content="a\u202eb\u2066c\u200bd\u2028e\u2029f\ufeff\n")
    assert out.stdout == "a\\u202eb\\u2066c\\u200bd\\u2028e\\u2029f\\ufeff\n"


def test_definition_show_keeps_printable_unicode(tmp_path: Path) -> None:
    text = "-- \u00e9\u20ac\u65e5\u672c \U0001f469\n"
    assert invoke_show(tmp_path, content=text).stdout == text


def test_definition_show_never_reads_markup_or_expands_tabs(tmp_path: Path) -> None:
    text = "SELECT '[bold]x[/bold]'\t-- [link=https://evil.example]y[/link]\n"
    assert invoke_show(tmp_path, content=text).stdout == text


@pytest.mark.parametrize(
    ("reason", "message"),
    [("restricted", RESTRICTED), ("not_recorded", NOT_RECORDED), ("too_large", TOO_LARGE)],
)
def test_definition_show_prints_the_reason_when_no_content(
    tmp_path: Path, reason: str, message: str
) -> None:
    out = invoke_show(tmp_path, content=None, reason=reason, message=message)
    assert out.exit_code == 0 and out.stdout == ""
    assert out.stderr == LINES + f"Open        {_LINK}\n" + message + "\n"


def test_definition_show_escapes_the_message(tmp_path: Path) -> None:
    out = invoke_show(tmp_path, content=None, reason="restricted", message="no\x1b[2J")
    assert out.stderr.endswith("no\\x1b[2J\n")


def test_definition_json_output_is_the_location(tmp_path: Path) -> None:
    result = _invoke(tmp_path, LOCATION, "--output", "json")
    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout) == {**LOCATION, "deployed_at": "2026-11-12T10:00:00+00:00"}


def test_definition_show_json_output_has_the_file(tmp_path: Path) -> None:
    result = _invoke(tmp_path, LOCATION, "--output", "json", "--show", file={"content": "x\x1b"})
    body = json.loads(result.stdout)
    assert (body["path"], body["content"], body["reason"]) == (
        "assets/silver/orders.sql",
        "x\x1b",
        None,
    )
