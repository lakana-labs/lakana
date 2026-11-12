"""`lakana table preview`: the first rows of a table, read as you."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from conftest import FakeApi, invoke, make_state

_PREVIEW = "/api/v1/environments/dev/namespaces/gold/tables/orders/preview"
_Q = "/api/v1/environments/dev/queries"


def _page(**more: Any) -> dict[str, Any]:  # ruff: ignore[any-type]
    """Build a three-row JSON page whose email column was masked.

    Parameters
    ----------
    **more
        Keys that differ.

    Returns
    -------
    dict
        The page.
    """
    return {
        "columns": [{"name": "id", "type": "int64"}, {"name": "email", "type": "string"}],
        "columns_cut": 0,
        "masked_columns": [{"table": "gold.orders", "column": "email"}],
        "masked_columns_cut": 0,
        "rows": [["1", "***"], ["2", "***"], ["3", "***"]],
        "row_cut": [],
        "row_count": 3,
        "next_offset": None,
        "duration_ms": 800,
        "preview": {"table": "gold.orders"},
        **more,
    }


def _api(
    page: dict[str, Any] | None = None, *, status: str = "SUCCEEDED", error: str = ""
) -> FakeApi:
    """Fake the preview route, the run's status and its JSON page.

    Parameters
    ----------
    page
        The JSON page the result route answers.
    status
        The run's final status.
    error
        The error message of a failed run.

    Returns
    -------
    FakeApi
        The fake.
    """
    api = FakeApi()
    run = {"query_id": "q1", "status": status, "error_message": error or None, "lineage": None}
    api.on("POST", _PREVIEW, status=202, json={"query_id": "q1"})
    api.on("GET", f"{_Q}/q1", json=run)
    api.on("GET", f"{_Q}/q1/result", json=page if page is not None else _page())
    return api


def test_preview_prints_rows_and_names_the_masked_columns(tmp_path: Path) -> None:
    result = invoke(make_state(tmp_path, _api()), "table", "preview", "gold.orders", "--env", "dev")
    assert result.exit_code == 0, result.stderr
    assert "Masked for you: email" in result.stderr
    assert "First 3 rows" in result.stderr and "read as you" in result.stderr
    assert "0.8 s" in result.stderr
    assert "***" in result.stdout


def test_preview_names_a_mask_behind_a_view_with_its_table(tmp_path: Path) -> None:
    page = _page(
        masked_columns=[
            {"table": "gold.orders", "column": "email"},
            {"table": "gold.customers", "column": "phone"},
        ]
    )
    result = invoke(
        make_state(tmp_path, _api(page)), "table", "preview", "gold.orders", "--env", "dev"
    )
    assert "Masked for you: email, gold.customers.phone" in result.stderr


def test_preview_says_so_when_masked_columns_were_left_out(tmp_path: Path) -> None:
    result = invoke(
        make_state(tmp_path, _api(_page(masked_columns_cut=2))),
        "table",
        "preview",
        "gold.orders",
        "--env",
        "dev",
    )
    assert "Masked for you: email, and 2 more" in result.stderr


def test_preview_without_a_mask_prints_no_masked_line(tmp_path: Path) -> None:
    for masked in ([], None):
        result = invoke(
            make_state(tmp_path, _api(_page(masked_columns=masked))),
            "table",
            "preview",
            "gold.orders",
            "--env",
            "dev",
        )
        assert result.exit_code == 0, result.stderr
        assert "Masked" not in result.stderr


def test_preview_says_when_columns_were_left_out(tmp_path: Path) -> None:
    result = invoke(
        make_state(tmp_path, _api(_page(columns_cut=7))),
        "table",
        "preview",
        "gold.orders",
        "--env",
        "dev",
    )
    assert "7 more columns not shown" in result.stderr


def test_preview_marks_cut_and_binary_cells(tmp_path: Path) -> None:
    page = _page(
        rows=[
            ["1", {"cut": True, "text": "abc"}],
            ["2", {"binary": 3}],
            ["3", [1, {"cut": True}]],
            ["4", {"a": 1}],
            ["5", {"cut": True}],
            ["6", None],
        ],
        row_count=6,
    )
    result = invoke(
        make_state(tmp_path, _api(page)),
        "table",
        "preview",
        "gold.orders",
        "--env",
        "dev",
        env={"COLUMNS": "200"},
    )
    assert "abc…" in result.stdout and "<3 bytes>" in result.stdout
    assert '[1,{"cut":true}]' in result.stdout.replace(" ", "")
    assert '{"a":1}' in result.stdout.replace(" ", "")
    assert "…" in result.stdout


def test_preview_escapes_control_characters_in_cells(tmp_path: Path) -> None:
    page = _page(rows=[["1", "a\x1b[31mred"]], row_count=1)
    result = invoke(
        make_state(tmp_path, _api(page)), "table", "preview", "gold.orders", "--env", "dev"
    )
    assert "\x1b" not in result.stdout
    assert "\\x1b" in result.stdout


def test_preview_does_not_read_markup_in_cells(tmp_path: Path) -> None:
    page = _page(rows=[["1", "[bold]x[/bold]"]], row_count=1)
    result = invoke(
        make_state(tmp_path, _api(page)), "table", "preview", "gold.orders", "--env", "dev"
    )
    assert "[bold]x[/bold]" in result.stdout


def test_preview_json_output_is_the_page(tmp_path: Path) -> None:
    result = invoke(
        make_state(tmp_path, _api()),
        "--output",
        "json",
        "table",
        "preview",
        "gold.orders",
        "--env",
        "dev",
    )
    body = json.loads(result.stdout)
    assert body["row_count"] == 3
    assert body["preview_table"] == "gold.orders"
    assert body["masked_columns"] == [{"table": "gold.orders", "column": "email"}]


def test_preview_prints_the_failure_of_the_run(tmp_path: Path) -> None:
    message = "The table changed since the preview was asked for; refresh."
    result = invoke(
        make_state(tmp_path, _api(status="FAILED", error=message)),
        "table",
        "preview",
        "gold.orders",
        "--env",
        "dev",
    )
    assert result.exit_code == 9  # a failed query, as for `lakana sql`
    assert message in result.stderr


def test_preview_of_an_empty_table_says_so(tmp_path: Path) -> None:
    page = _page(rows=[], row_count=0, masked_columns=[])
    result = invoke(
        make_state(tmp_path, _api(page)), "table", "preview", "gold.orders", "--env", "dev"
    )
    assert result.exit_code == 0, result.stderr
    assert "No rows." in result.stdout


def test_preview_refused_for_a_viewer_names_the_next_step(tmp_path: Path) -> None:
    api = _api()
    refusal = (
        "Preview runs a query as you, which needs the right to run queries in dev. "
        "Ask an administrator for the Analyst role there."
    )
    api.on("POST", _PREVIEW, status=403, json={"detail": refusal, "type": "PermissionDeniedError"})
    result = invoke(make_state(tmp_path, api), "table", "preview", "gold.orders", "--env", "dev")
    assert result.exit_code != 0
    assert "Ask an administrator for the Analyst role there." in result.stderr


def test_preview_passes_a_timeout_and_asks_for_the_first_page_only(tmp_path: Path) -> None:
    api = _api()
    result = invoke(
        make_state(tmp_path, api),
        "table",
        "preview",
        "gold.orders",
        "--env",
        "dev",
        "--timeout",
        "30",
    )
    assert result.exit_code == 0, result.stderr
    asked = api.requests[-1]
    assert dict(asked.url.params) == {"format": "json", "limit": "100", "offset": "0"}


def test_preview_leaves_out_the_time_when_the_server_does_not_know_it(tmp_path: Path) -> None:
    result = invoke(
        make_state(tmp_path, _api(_page(duration_ms=None))),
        "table",
        "preview",
        "gold.orders",
        "--env",
        "dev",
    )
    assert result.exit_code == 0, result.stderr
    assert "First 3 rows · read as you" in result.stderr
    assert " s\n" not in result.stderr.replace("\r", "").split("Masked")[0]
