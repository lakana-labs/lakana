"""`lakana check list`: the results of the data quality checks a run made."""

from __future__ import annotations

import io
import json
import re
from pathlib import Path

import pyarrow as pa
import pyarrow.ipc
import pyarrow.parquet as pq
from conftest import FakeApi, invoke, make_state

_RUN = "3f2c9d1e-8b7a-4c55-9e0a-1b2c3d4e5f60"
_CHECKS = "/api/v1/environments/dev/checks"
_RUN_CHECKS = f"/api/v1/environments/dev/runs/{_RUN}/checks"


def result_body(check: str = "unique_reading", **more: object) -> dict[str, object]:
    """Build a `CheckResultOut` body for a passed blocking check of one table.

    Parameters
    ----------
    check
        The check's short name.
    **more
        Fields that differ.

    Returns
    -------
    dict
        The body.
    """
    table = str(more.get("table", "silver.station_temperatures"))
    return {
        "run_id": _RUN,
        "task": table,
        "name": f"{table}/{check}",
        "check": check,
        "path": f"checks/{table.replace('.', '/')}/{check}.sql",
        "table": table,
        "table_id": None,
        "on_failure": "block",
        "outcome": "passed",
        "failing_rows": 0,
        "error": None,
        "checked_snapshot_id": 7,
        "rollback_outcome": None,
        "policy_applied": False,
        "policy_note": None,
        "query_run_id": None,
        "started_at": "2026-10-11T12:00:00+00:00",
        "finished_at": "2026-10-11T12:00:01+00:00",
        "generated": False,
        **more,
    }


def _page(*items: dict[str, object]) -> dict[str, object]:
    """Build a `CheckResultPage` body without a next page.

    Parameters
    ----------
    *items
        The results.

    Returns
    -------
    dict
        The body.
    """
    return {"items": list(items), "next_cursor": None}


def test_check_list_shows_results_newest_first(tmp_path: Path) -> None:
    api = FakeApi()
    api.on(
        "GET",
        _CHECKS,
        json=_page(
            result_body("unique_reading", outcome="failed", failing_rows=3),
            result_body("plausible_range", outcome="warned", on_failure="warn", failing_rows=12),
        ),
    )
    result = invoke(
        make_state(tmp_path, api), "check", "list", "--env", "dev", env={"COLUMNS": "200"}
    )
    assert result.exit_code == 0, result.stderr
    header = next(line for line in result.stdout.splitlines() if "outcome" in line)
    cells = [cell.strip() for cell in header.split("┃")[1:-1]]
    assert cells == ["run", "table", "check", "outcome", "rows", "on_failure", "finished_at"]
    rows = [line for line in result.stdout.splitlines() if line.startswith("│")]
    assert "unique_reading" in rows[0] and "plausible_range" in rows[1]
    assert _RUN[:8] in rows[0]
    assert _RUN not in result.stdout
    assert re.search(r"failed\s+│ 3\s+│ block", rows[0])
    assert re.search(r"warned\s+│ 12\s+│ warn", rows[1])
    assert api.requests[-1].url.params["limit"] == "50"


def test_check_list_marks_a_generated_check(tmp_path: Path) -> None:
    api = FakeApi()
    api.on(
        "GET",
        _CHECKS,
        json=_page(result_body("key_unique", generated=True), result_body("unique_reading")),
    )
    result = invoke(
        make_state(tmp_path, api), "check", "list", "--env", "dev", env={"COLUMNS": "200"}
    )
    assert result.exit_code == 0, result.stderr
    rows = [line for line in result.stdout.splitlines() if line.startswith("│")]
    assert "key_unique (generated)" in rows[0]
    assert "(generated)" not in rows[1]


def test_a_result_from_an_older_server_is_not_marked_generated(tmp_path: Path) -> None:
    body = result_body("key_unique")
    del body["generated"]
    api = FakeApi()
    api.on("GET", _CHECKS, json=_page(body))
    result = invoke(make_state(tmp_path, api), "--output", "json", "check", "list", "--env", "dev")
    (row,) = json.loads(result.stdout)
    assert row["generated"] is False


def test_check_list_of_one_run(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("GET", _RUN_CHECKS, json=[result_body("a"), result_body("b")])
    result = invoke(
        make_state(tmp_path, api),
        "check",
        "list",
        "--env",
        "dev",
        "--run",
        _RUN,
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 0, result.stderr
    rows = [line for line in result.stdout.splitlines() if line.startswith("│")]
    assert len(rows) == 2
    assert [r.url.path for r in api.requests] == [_RUN_CHECKS]


def test_check_list_filters_by_table(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("GET", _CHECKS, json=_page(result_body()))
    invoke(make_state(tmp_path, api), "check", "list", "--env", "dev", "--table", "silver.a")
    assert api.requests[-1].url.params["table"] == "silver.a"
    api.on(
        "GET",
        _RUN_CHECKS,
        json=[result_body("a"), result_body("b", table="gold.other")],
    )
    result = invoke(
        make_state(tmp_path, api),
        "check",
        "list",
        "--env",
        "dev",
        "--run",
        _RUN,
        "--table",
        "gold.other",
        env={"COLUMNS": "200"},
    )
    rows = [line for line in result.stdout.splitlines() if line.startswith("│")]
    assert len(rows) == 1 and "gold.other" in rows[0]


def test_check_list_follows_pages_up_to_the_limit(tmp_path: Path) -> None:
    api = FakeApi()
    api.on(
        "GET",
        _CHECKS,
        json={"items": [result_body("a"), result_body("b")], "next_cursor": "more"},
    )
    result = invoke(
        make_state(tmp_path, api),
        "check",
        "list",
        "--env",
        "dev",
        "--limit",
        "3",
        env={"COLUMNS": "200"},
    )
    rows = [line for line in result.stdout.splitlines() if line.startswith("│")]
    assert len(rows) == 3
    assert api.requests[1].url.params["cursor"] == "more"


def test_check_list_says_when_there_is_nothing(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("GET", _CHECKS, json=_page())
    api.on("GET", _RUN_CHECKS, json=[])
    state = make_state(tmp_path, api)
    listed = invoke(state, "check", "list", "--env", "dev")
    assert (listed.exit_code, listed.stdout.strip()) == (0, "No check results in dev.")
    one = invoke(state, "check", "list", "--env", "dev", "--run", _RUN)
    assert (one.exit_code, one.stdout.strip()) == (0, f"Run {_RUN} has no check results.")


def test_check_list_json_keeps_the_results_as_they_are(tmp_path: Path) -> None:
    body = result_body("a", outcome="failed", failing_rows=3)
    api = FakeApi()
    api.on("GET", _CHECKS, json=_page(body))
    result = invoke(make_state(tmp_path, api), "--output", "json", "check", "list", "--env", "dev")
    (row,) = json.loads(result.stdout)
    assert set(row) == set(body)
    assert row["run_id"] == _RUN
    empty = FakeApi()
    empty.on("GET", _CHECKS, json=_page())
    nothing = invoke(
        make_state(tmp_path, empty), "--output", "json", "check", "list", "--env", "dev"
    )
    assert json.loads(nothing.stdout) == []


def test_a_check_text_cannot_inject_terminal_controls(tmp_path: Path) -> None:
    api = FakeApi()
    api.on(
        "GET",
        _CHECKS,
        json=_page(result_body("a\x1b[2Jb", table="s.t\x07x", outcome="x\x1b[31my")),
    )
    result = invoke(
        make_state(tmp_path, api), "check", "list", "--env", "dev", env={"COLUMNS": "200"}
    )
    assert "\x1b" not in result.stdout
    assert "\x07" not in result.stdout
    api.on("GET", _CHECKS, json=_page())
    odd = invoke(make_state(tmp_path, api), "check", "list", "--env", "d\x1b[2Jev")
    assert "\x1b" not in odd.stdout


_ROWS = f"/api/v1/environments/dev/runs/{_RUN}/checks/rows"
_QUERIES = "/api/v1/environments/dev/queries"
_CHECK = "silver.orders/unique_id"


def _arrow(table: pa.Table) -> bytes:
    """Serialize a table as an Arrow IPC file, as the server's result route sends it.

    Parameters
    ----------
    table
        The table.

    Returns
    -------
    bytes
        The file.
    """
    sink = io.BytesIO()
    with pyarrow.ipc.new_file(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue()


def _rows_api(table: pa.Table, *, status: str = "SUCCEEDED", error: str | None = None) -> FakeApi:
    """Build a fake that queues the rows of a check, reports `status` and serves `table`.

    Parameters
    ----------
    table
        The rows the query found.
    status
        The final status of the query.
    error
        The error message of a failed query.

    Returns
    -------
    FakeApi
        The fake.
    """
    api = FakeApi()
    run = {"query_id": "q1", "status": status, "error_message": error, "lineage": None}
    api.on("POST", _ROWS, status=202, json={"query_id": "q1"})
    api.on("GET", f"{_QUERIES}/q1", json=run)
    api.on("GET", f"{_QUERIES}/q1/result", content=_arrow(table))
    return api


def test_check_rows_queues_the_query_waits_and_prints_the_rows(tmp_path: Path) -> None:
    api = _rows_api(pa.table({"id": [1, 2]}))

    result = invoke(make_state(tmp_path, api), "check", "rows", _RUN, _CHECK, "--env", "dev")

    assert result.exit_code == 0, result.stderr
    assert "2 rows" in result.stdout
    post = api.requests[0]
    assert (post.method, post.url.path, json.loads(post.content)) == (
        "POST",
        _ROWS,
        {"check": _CHECK},
    )


def test_check_rows_prints_json_and_writes_a_file_like_sql(tmp_path: Path) -> None:
    table = pa.table({"id": list(range(30))})
    as_json = invoke(
        make_state(tmp_path, _rows_api(table)),
        "--output",
        "json",
        "check",
        "rows",
        _RUN,
        _CHECK,
        "--env",
        "dev",
    )
    assert json.loads(as_json.stdout) == [{"id": n} for n in range(30)]
    out = tmp_path / "rows.parquet"
    written = invoke(
        make_state(tmp_path, _rows_api(table)),
        "check",
        "rows",
        _RUN,
        _CHECK,
        "--env",
        "dev",
        "--out",
        str(out),
        "--max-rows",
        "5",
    )
    assert written.exit_code == 0, written.stderr
    assert "Wrote 30 rows" in written.stdout
    assert pq.read_table(out).equals(table)


def test_check_rows_refuses_an_unknown_out_extension_before_it_queues_anything(
    tmp_path: Path,
) -> None:
    api = _rows_api(pa.table({"id": [1]}))

    result = invoke(
        make_state(tmp_path, api),
        "check",
        "rows",
        _RUN,
        _CHECK,
        "--env",
        "dev",
        "--out",
        str(tmp_path / "rows.txt"),
    )

    assert result.exit_code == 2
    assert api.requests == []


def test_check_rows_without_any_environment_is_a_usage_error(tmp_path: Path) -> None:
    api = _rows_api(pa.table({"id": [1]}))

    result = invoke(make_state(tmp_path, api), "check", "rows", _RUN, _CHECK)

    assert result.exit_code == 2
    assert "No environment" in result.stderr
    assert api.requests == []


def test_check_rows_prints_the_servers_refusals_as_errors(tmp_path: Path) -> None:
    for status, code in ((404, 6), (409, 7), (403, 5)):
        message = f"the server says {status}"
        api = FakeApi()
        api.on("POST", _ROWS, status=status, json={"detail": message})

        result = invoke(make_state(tmp_path, api), "check", "rows", _RUN, _CHECK, "--env", "dev")

        assert result.exit_code == code, (status, result.stderr)
        assert message in result.stderr
        assert result.stdout == ""


def test_check_rows_prints_the_message_of_a_failed_query_with_exit_9(tmp_path: Path) -> None:
    message = (
        "The rows of this check can no longer be read: the snapshot it checked is no longer "
        "in `silver.orders`."
    )
    api = _rows_api(pa.table({}), status="FAILED", error=message)

    result = invoke(make_state(tmp_path, api), "check", "rows", _RUN, _CHECK, "--env", "dev")

    assert result.exit_code == 9
    assert message in result.stderr


def test_check_rows_help_says_what_it_shows_and_what_it_needs(tmp_path: Path) -> None:
    result = invoke(make_state(tmp_path, FakeApi()), "check", "rows", "--help")
    text = " ".join(result.output.split())
    description = text.split("╭─ Arguments")[0].strip()

    assert result.exit_code == 0
    assert description == (
        "Usage: root check rows [OPTIONS] {RUN} {CHECK} Show the rows a check found in a run. "
        "These are the rows this check found in that run, as they were then, also after the "
        "table was set back. It runs as a query of yours, under your own rights, row filters "
        "and column masks: you see the rows the check found that you may read, and every other "
        "table the check's SQL reads is read as it is now. It needs the right to read the run "
        "and to submit queries, and the right to read every table the check's SQL reads: "
        "without it, the command refuses and shows neither rows nor SQL. The rows stay "
        "readable for seven days after a failed blocking check; then `lakana table maintain` "
        "removes the snapshot, and the number of rows remains in `lakana check list`. A check "
        "that read no snapshot (a view, or SQL that does not read its table) has no rows to "
        "show; the message says where to run its SQL yourself. Prints a table like "
        "`lakana sql`; --out writes every row to a file instead."
    )
    options = re.findall(r"--[a-z-]+", text.split("╭─ Options")[1])
    assert options == ["--env", "--out", "--format", "--max-rows", "--timeout", "--help"]
