"""Tests for `lakana sql` and `lakana query *`."""

from __future__ import annotations

import dataclasses
import io
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pyarrow as pa
import pyarrow.ipc
import pyarrow.parquet as pq
import pytest
import typer
from conftest import FakeApi, invoke, make_state
from typer.testing import Result

from lakana._models import QueryStatus, WaitingFor
from lakana.cli._state import CliState
from lakana.cli._waiting import WAIT_LINE_AFTER_SECONDS, WaitLine

_Q = "/api/v1/environments/dev/queries"


def _arrow_bytes(table: pa.Table) -> bytes:
    """Serialize a table as an Arrow IPC file.

    `QueryRun.result` reads the server's response with `pyarrow.ipc.open_file`,
    which needs the Arrow IPC **file** format (magic bytes and footer), not
    the stream format — a fake built with `pyarrow.ipc.new_stream` fails with
    `ArrowInvalid: Not an Arrow file`.

    Parameters
    ----------
    table
        The table.

    Returns
    -------
    bytes
        The serialized file.
    """
    sink = io.BytesIO()
    with pyarrow.ipc.new_file(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue()


def _api(result: pa.Table, status: str = "SUCCEEDED", error: str | None = None) -> FakeApi:
    """Build a fake that accepts a query, reports `status`, and serves `result`.

    Parameters
    ----------
    result
        The query result.
    status
        The final status.
    error
        The error message for a failed run.

    Returns
    -------
    FakeApi
        The fake.
    """
    api = FakeApi()
    # `Queries.submit` reads `response.json()["query_id"]`, and
    # `_status_from_json` reads `data["query_id"]` too — both keyed by
    # `query_id`, not `id` (verified against `lakana.resources.queries`).
    run = {"query_id": "q1", "status": status, "error_message": error, "lineage": None}
    api.on("POST", _Q, status=202, json=run)
    api.on("GET", f"{_Q}/q1", json=run)
    api.on("GET", f"{_Q}/q1/result", content=_arrow_bytes(result))
    return api


def test_sql_prints_a_table(tmp_path: Path) -> None:
    """`sql` submits, waits and renders the rows."""
    result = invoke(
        make_state(tmp_path, _api(pa.table({"id": [1, 2]}))), "sql", "SELECT 1", "--env", "dev"
    )

    assert result.exit_code == 0, result.stderr
    assert "2 rows" in result.stdout


def test_sql_json_prints_row_objects(tmp_path: Path) -> None:
    """`--output json sql` prints every row as an object."""
    result = invoke(
        make_state(tmp_path, _api(pa.table({"id": [1, 2]}))),
        "--output",
        "json",
        "sql",
        "SELECT 1",
        "--env",
        "dev",
    )

    assert json.loads(result.stdout) == [{"id": 1}, {"id": 2}]


def test_sql_reads_the_query_from_stdin(tmp_path: Path) -> None:
    """`-` reads the SQL text from stdin."""
    api = _api(pa.table({"id": [1]}))

    result = invoke(
        make_state(tmp_path, api), "sql", "-", "--env", "dev", input="SELECT id FROM t\n"
    )

    assert result.exit_code == 0, result.stderr
    assert json.loads(api.requests[0].content)["sql"].strip() == "SELECT id FROM t"


def test_sql_writes_the_full_result_to_a_file(tmp_path: Path) -> None:
    """`--out result.parquet` writes every row, regardless of `--max-rows`."""
    out = tmp_path / "result.parquet"
    table = pa.table({"id": list(range(100))})

    result = invoke(
        make_state(tmp_path, _api(table)),
        "sql",
        "SELECT 1",
        "--env",
        "dev",
        "--out",
        str(out),
        "--max-rows",
        "5",
    )

    assert result.exit_code == 0, result.stderr
    assert "Wrote 100 rows" in result.stdout
    assert pq.read_table(out).equals(table)


def test_sql_with_zero_rows(tmp_path: Path) -> None:
    """Review focus 3: an empty result shows its header and `0 rows`; JSON is `[]`."""
    empty = pa.table({"id": pa.array([], pa.int64())})

    table_mode = invoke(make_state(tmp_path, _api(empty)), "sql", "SELECT 1", "--env", "dev")
    json_mode = invoke(
        make_state(tmp_path, _api(empty)), "--output", "json", "sql", "SELECT 1", "--env", "dev"
    )

    assert "id" in table_mode.stdout
    assert "0 rows" in table_mode.stdout
    assert json.loads(json_mode.stdout) == []


def test_sql_table_mode_prints_a_bracketed_column_name_literally(tmp_path: Path) -> None:
    """A crafted column alias must not be interpreted as Rich markup."""
    table = pa.table({"[red]x[/red]": [1]})

    result = invoke(make_state(tmp_path, _api(table)), "sql", "SELECT 1", "--env", "dev")

    assert result.exit_code == 0, result.stderr
    assert "[red]x[/red]" in result.stdout


def test_a_failed_query_exits_9_with_the_servers_message(tmp_path: Path) -> None:
    """`QueryFailed` maps to exit 9, carrying the server's (redacted) message."""
    result = invoke(
        make_state(
            tmp_path, _api(pa.table({}), status="FAILED", error="Binder Error: no column x")
        ),
        "sql",
        "SELECT x",
        "--env",
        "dev",
    )

    assert result.exit_code == 9
    assert "Binder Error" in result.stderr


def test_query_submit_status_and_result(tmp_path: Path) -> None:
    """The step-by-step commands work with a query id."""
    api = _api(pa.table({"id": [7]}))

    submitted = invoke(
        make_state(tmp_path, api), "--output", "json", "query", "submit", "SELECT 7", "--env", "dev"
    )
    status = invoke(
        make_state(tmp_path, api), "--output", "json", "query", "status", "q1", "--env", "dev"
    )
    waited = invoke(
        make_state(tmp_path, api), "query", "wait", "q1", "--env", "dev", "--timeout", "5"
    )
    fetched = invoke(
        make_state(tmp_path, api), "--output", "json", "query", "result", "q1", "--env", "dev"
    )

    assert json.loads(submitted.stdout) == {"id": "q1", "environment": "dev"}
    assert json.loads(status.stdout)["status"] == "SUCCEEDED"
    assert waited.exit_code == 0, waited.stderr
    assert json.loads(fetched.stdout) == [{"id": 7}]


def test_query_result_of_a_write_prints_the_servers_message_and_exits_7(tmp_path: Path) -> None:
    """`query result` of a write run shows the server's 409 text unchanged."""
    message = (
        "Query q1 wrote to a table, so it has no result rows to download. "
        "See what it did with: lakana query status q1 --env dev"
    )
    api = FakeApi()
    api.on("GET", f"{_Q}/q1", json=_status_body("SUCCEEDED"))
    api.on("GET", f"{_Q}/q1/result", status=409, json={"detail": message})

    result = invoke(make_state(tmp_path, api), "query", "result", "q1", "--env", "dev")

    assert result.exit_code == 7
    assert message in result.stderr
    assert result.stdout == ""


def test_sql_refuses_an_unknown_out_extension_before_submitting(tmp_path: Path) -> None:
    """Final review minor 6: a bad `--out` extension fails (exit 2) with no request made."""
    api = _api(pa.table({"n": [1]}))

    result = invoke(
        make_state(tmp_path, api),
        "sql",
        "SELECT 1",
        "--env",
        "dev",
        "--out",
        str(tmp_path / "x.txt"),
    )

    assert result.exit_code == 2
    assert "--format" in result.stderr
    assert api.requests == []


def test_sql_refuses_an_invalid_explicit_format_before_submitting(tmp_path: Path) -> None:
    """An invalid `--format` for `--out` is also refused before the query runs."""
    api = _api(pa.table({"n": [1]}))

    result = invoke(
        make_state(tmp_path, api),
        "sql",
        "SELECT 1",
        "--env",
        "dev",
        "--out",
        str(tmp_path / "x.parquet"),
        "--format",
        "docx",
    )

    assert result.exit_code == 2
    assert api.requests == []


def test_query_result_refuses_an_unknown_out_extension_before_fetching(tmp_path: Path) -> None:
    """Final review minor 6: `query result --out x.txt` makes no request either."""
    api = _api(pa.table({"n": [1]}))

    result = invoke(
        make_state(tmp_path, api),
        "query",
        "result",
        "q1",
        "--env",
        "dev",
        "--out",
        str(tmp_path / "x.txt"),
    )

    assert result.exit_code == 2
    assert api.requests == []


_CSV = b'n,s\r\n1,"a,b"\r\n'
_XLSX = b"PK\x03\x04 pretend spreadsheet"


def _file_api(content: bytes) -> FakeApi:
    """Build a fake whose result route serves `content`, whatever the format asked for.

    Parameters
    ----------
    content
        The body of the result route.

    Returns
    -------
    FakeApi
        The fake, with a succeeded run `q1`.
    """
    api = FakeApi()
    run = _status_body("SUCCEEDED")
    api.on("POST", _Q, status=202, json=run)
    api.on("GET", f"{_Q}/q1", json=run)
    api.on("GET", f"{_Q}/q1/result", content=content)
    return api


def _asked_format(api: FakeApi) -> str | None:
    """Return the `format` of the last request to the result route.

    Parameters
    ----------
    api
        The fake.

    Returns
    -------
    str or None
        The query parameter, or `None` when none was sent.
    """
    asked = [r for r in api.requests if r.url.path.endswith("/result")]
    return asked[-1].url.params.get("format") if asked else None


def test_query_result_format_csv_saves_the_servers_bytes(tmp_path: Path) -> None:
    """`--format csv --out` saves the server's CSV as it is, asked for with `format=csv`."""
    api = _file_api(_CSV)
    out = tmp_path / "one.csv"

    result = invoke(
        make_state(tmp_path, api),
        "query",
        "result",
        "q1",
        "--env",
        "dev",
        "--format",
        "csv",
        "--out",
        str(out),
    )

    assert result.exit_code == 0, result.stderr
    assert out.read_bytes() == _CSV
    assert _asked_format(api) == "csv"
    assert f"Wrote {len(_CSV)} bytes to" in result.stdout
    assert [p.name for p in tmp_path.iterdir() if p.suffix == ".part"] == []


def test_query_result_xlsx_from_the_extension(tmp_path: Path) -> None:
    """`--out x.xlsx` means `--format xlsx` and saves the server's file."""
    api = _file_api(_XLSX)
    out = tmp_path / "one.xlsx"

    result = invoke(
        make_state(tmp_path, api),
        "query",
        "result",
        "q1",
        "--env",
        "dev",
        "--out",
        str(out),
    )

    assert result.exit_code == 0, result.stderr
    assert out.read_bytes() == _XLSX
    assert _asked_format(api) == "xlsx"


def test_query_result_format_csv_without_out_prints_the_csv(tmp_path: Path) -> None:
    """`--format csv` alone streams the server's CSV to standard output."""
    api = _file_api(_CSV)

    result = invoke(
        make_state(tmp_path, api), "query", "result", "q1", "--env", "dev", "--format", "csv"
    )

    assert result.exit_code == 0, result.stderr
    assert result.stdout_bytes == _CSV  # CRLF as the server sent it, not translated
    assert _asked_format(api) == "csv"


def test_query_result_format_xlsx_without_out_is_exit_2_with_the_sentence(tmp_path: Path) -> None:
    """A binary file is never printed to a terminal: exit 2 before any request."""
    api = _file_api(_XLSX)

    result = invoke(
        make_state(tmp_path, api), "query", "result", "q1", "--env", "dev", "--format", "xlsx"
    )

    assert result.exit_code == 2
    assert "--format xlsx writes a binary file; add --out FILE.xlsx" in result.stderr
    assert api.requests == []


def test_query_result_parquet_is_still_written_locally(tmp_path: Path) -> None:
    """`parquet` is written by the CLI from the Arrow result, not asked of the server."""
    table = pa.table({"id": [1, 2]})
    api = _api(table)
    out = tmp_path / "r.parquet"

    result = invoke(
        make_state(tmp_path, api), "query", "result", "q1", "--env", "dev", "--out", str(out)
    )

    assert result.exit_code == 0, result.stderr
    assert pq.read_table(out).equals(table)
    assert _asked_format(api) is None
    assert "Wrote 2 rows" in result.stdout


def test_sql_out_xlsx_uses_the_server(tmp_path: Path) -> None:
    """`lakana sql --out x.xlsx` waits for the run, then saves the server's spreadsheet."""
    api = _file_api(_XLSX)
    out = tmp_path / "x.xlsx"

    result = invoke(make_state(tmp_path, api), "sql", "SELECT 1", "--env", "dev", "--out", str(out))

    assert result.exit_code == 0, result.stderr
    assert out.read_bytes() == _XLSX
    assert _asked_format(api) == "xlsx"


def _write_api(mode: str = "replace") -> FakeApi:
    """Build a fake that accepts a write run and reports it finished.

    Parameters
    ----------
    mode
        The write mode to report.

    Returns
    -------
    FakeApi
        The fake.
    """
    api = FakeApi()
    status = {
        "query_id": "q1",
        "status": "SUCCEEDED",
        "error_message": None,
        "lineage": None,
        "write_mode": mode,
        "target": "gold.daily",
        "rows_written": 3,
        "snapshot_id": 7,
        "watermark": "long:42",
        "schema_diff": None,
    }
    api.on("POST", _Q, status=202, json={"query_id": "q1"})
    api.on("GET", f"{_Q}/q1", json=status)
    return api


def test_sql_into_writes_and_reports_the_outcome(tmp_path: Path) -> None:
    """`--into/--mode` send a target and print what was written, never a result."""
    api = _write_api()

    result = invoke(
        make_state(tmp_path, api),
        "sql",
        "SELECT 1",
        "--into",
        "gold.daily",
        "--mode",
        "append",
        "--incremental-key",
        "id",
        "--env",
        "dev",
    )

    assert result.exit_code == 0, result.stderr
    assert "Wrote 3 rows to dev/gold.daily (snapshot 7); watermark long:42" in result.stdout
    body = json.loads(api.requests[0].content)
    assert body["target"] == {
        "table": "gold.daily",
        "mode": "append",
        "incremental_key": "id",
        "evolve_schema": False,
    }
    assert all(not r.url.path.endswith("/result") for r in api.requests)


def test_sql_into_json_prints_the_status(tmp_path: Path) -> None:
    """`--output json` prints the run's status object."""
    result = invoke(
        make_state(tmp_path, _write_api()),
        "--output",
        "json",
        "sql",
        "SELECT 1",
        "--into",
        "gold.daily",
        "--mode",
        "replace",
        "--env",
        "dev",
    )

    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout)["rows_written"] == 3


def test_sql_into_a_view_says_so(tmp_path: Path) -> None:
    """A view run reports the view, not rows."""
    result = invoke(
        make_state(tmp_path, _write_api("view")),
        "sql",
        "SELECT 1",
        "--into",
        "gold.daily",
        "--mode",
        "view",
        "--env",
        "dev",
    )

    assert result.exit_code == 0, result.stderr
    assert "Created view dev/gold.daily" in result.stdout


@pytest.mark.parametrize(
    "flags",
    [
        ["--mode", "replace"],
        ["--into", "gold.daily"],
        ["--incremental-key", "id"],
        ["--evolve-schema"],
        ["--into", "gold.daily", "--mode", "merge"],
        ["--into", "gold.daily", "--mode", "replace", "--incremental-key", "id"],
        ["--into", "gold.daily", "--mode", "create", "--evolve-schema"],
        ["--into", "gold.daily", "--mode", "replace", "--out", "x.parquet"],
    ],
)
def test_sql_refuses_write_flags_that_dont_go_together(tmp_path: Path, flags: list[str]) -> None:
    """Every invalid combination is a usage error (exit 2) before anything is sent."""
    api = FakeApi()

    result = invoke(make_state(tmp_path, api), "sql", "SELECT 1", *flags, "--env", "dev")

    assert result.exit_code == 2
    assert api.requests == []


def test_sql_into_a_zero_row_append_reports_no_snapshot(tmp_path: Path) -> None:
    """An incremental append that finds nothing new commits no snapshot."""
    api = FakeApi()
    status = {
        "query_id": "q1",
        "status": "SUCCEEDED",
        "error_message": None,
        "lineage": None,
        "write_mode": "append",
        "target": "gold.daily",
        "rows_written": 0,
        "snapshot_id": None,
        "watermark": None,
        "schema_diff": None,
    }
    api.on("POST", _Q, status=202, json={"query_id": "q1"})
    api.on("GET", f"{_Q}/q1", json=status)

    result = invoke(
        make_state(tmp_path, api),
        "sql",
        "SELECT 1",
        "--into",
        "gold.daily",
        "--mode",
        "append",
        "--incremental-key",
        "id",
        "--env",
        "dev",
    )

    assert result.exit_code == 0, result.stderr
    assert result.stdout.strip() == "Wrote 0 rows to dev/gold.daily"


# --- Cancel and Ctrl-C ----------------------------------------------


def _outcome(result: Result) -> str:
    """Return what the CLI said after it announced the cancel, asserting the announcement.

    Parameters
    ----------
    result
        The invocation.

    Returns
    -------
    str
        Standard error without its first line.
    """
    first, _, rest = result.stderr.partition("\n")
    assert first == ("Interrupted. Cancelling query q1; press Ctrl-C again to stop waiting for it.")
    return rest.strip()


def _status_body(status: str, query_id: str = "q1") -> dict[str, object]:
    """Build the JSON body of a query status.

    Parameters
    ----------
    status
        The status.
    query_id
        The query id.

    Returns
    -------
    dict of str to object
        A `QueryStatusOut` body.
    """
    cancelled = status == "CANCELLED"
    return {
        "query_id": query_id,
        "status": status,
        "error_message": "The query was cancelled." if cancelled else None,
        "error_cause": "cancelled" if cancelled else None,
        "lineage": None,
    }


def _interrupt(request: httpx.Request) -> httpx.Response:
    """Stand in for the user pressing Ctrl-C while the request is on its way.

    Parameters
    ----------
    request
        The request, unused.

    Raises
    ------
    KeyboardInterrupt
        Always.
    """
    raise KeyboardInterrupt


def _cancelling_api(answer: str = "CANCELLED") -> FakeApi:
    """Build a fake whose status polls are interrupted and whose cancel answers `answer`.

    Parameters
    ----------
    answer
        The status the cancel route answers with.

    Returns
    -------
    FakeApi
        The fake; `POST .../cancel` is the only route that answers.
    """
    api = FakeApi()
    api.on("POST", _Q, status=202, json={"query_id": "q1"})
    api.routes["GET", f"{_Q}/q1"] = _interrupt
    api.on("POST", f"{_Q}/q1/cancel", json=_status_body(answer))
    return api


def _cancel_requests(api: FakeApi) -> list[httpx.Request]:
    """List the cancel requests the fake received.

    Parameters
    ----------
    api
        The fake.

    Returns
    -------
    list of httpx.Request
        Every request to a cancel route.
    """
    return [r for r in api.requests if r.url.path.endswith("/cancel")]


@pytest.mark.parametrize(
    ("answer", "message"),
    [
        ("CANCELLED", "Cancelled query q1."),
        (
            "RUNNING",
            "Asked query q1 to stop. A running query ends within the grace period (10 s unless "
            "the operator changed it); check with 'lakana query status q1 --env dev'.",
        ),
        ("SUCCEEDED", "Query q1 had already ended (SUCCEEDED); nothing was changed."),
    ],
    ids=["queued", "running", "ended"],
)
def test_query_cancel_says_what_the_server_did(tmp_path: Path, answer: str, message: str) -> None:
    """`query cancel` names the outcome by the status the server answered with."""
    api = FakeApi()
    api.on("POST", f"{_Q}/q1/cancel", json=_status_body(answer))

    result = invoke(make_state(tmp_path, api), "query", "cancel", "q1", "--env", "dev")

    assert result.exit_code == 0, result.stderr
    assert result.stdout.strip() == message


def test_query_cancel_prints_the_status_in_json_mode(tmp_path: Path) -> None:
    """With `--output json` the status object is printed."""
    api = FakeApi()
    api.on("POST", f"{_Q}/q1/cancel", json=_status_body("CANCELLED"))

    result = invoke(
        make_state(tmp_path, api), "--output", "json", "query", "cancel", "q1", "--env", "dev"
    )

    body = json.loads(result.stdout)
    assert (body["id"], body["status"], body["error_cause"]) == ("q1", "CANCELLED", "cancelled")


def test_query_cancel_of_a_query_the_caller_may_not_cancel_is_the_not_found_error(
    tmp_path: Path,
) -> None:
    """A refusal is the server's 404, exit 6; the CLI adds nothing that tells the cases apart."""
    api = FakeApi()
    api.on("POST", f"{_Q}/q1/cancel", status=404, json={"detail": "No query run q1 in 'dev'."})

    result = invoke(make_state(tmp_path, api), "query", "cancel", "q1", "--env", "dev")

    assert result.exit_code == 6
    assert "No query run q1" in result.stderr


_BUSY_STARTING_OR_ENDING = (
    "Query q1 is being started or is finishing right now, so it cannot be cancelled at this "
    "moment. Check it with: lakana query status q1 --env dev. If it is still running, cancel it "
    "with: lakana query cancel q1 --env dev"
)


def test_query_cancel_of_a_query_that_is_starting_or_ending_prints_the_servers_conflict(
    tmp_path: Path,
) -> None:
    """The server's 409 is the message and the exit status is a conflict's, 7."""
    api = FakeApi()
    api.on("POST", f"{_Q}/q1/cancel", status=409, json={"detail": _BUSY_STARTING_OR_ENDING})

    result = invoke(make_state(tmp_path, api), "query", "cancel", "q1", "--env", "dev")

    assert result.exit_code == 7
    assert _BUSY_STARTING_OR_ENDING in " ".join(result.stderr.split())


def test_ctrl_c_with_a_query_that_is_starting_or_ending_says_it_was_not_cancelled(
    tmp_path: Path,
) -> None:
    """A cancel refused with a conflict changed nothing; the line says so and repeats the advice."""
    api = _cancelling_api()
    api.on("POST", f"{_Q}/q1/cancel", status=409, json={"detail": _BUSY_STARTING_OR_ENDING})

    result = invoke(make_state(tmp_path, api), "sql", "SELECT 1", "--env", "dev")

    assert result.exit_code == 130
    assert _outcome(result) == (
        "Interrupted. Query q1 was not cancelled: it is being started or is finishing. "
        "Check it with 'lakana query status q1 --env dev'; if it is still running, "
        "cancel it with 'lakana query cancel q1 --env dev'."
    )


def test_ctrl_c_in_sql_cancels_the_query_and_exits_130(tmp_path: Path) -> None:
    """Ctrl-C while `sql` waits sends one cancel, says so, and fetches no result."""
    api = _cancelling_api()

    result = invoke(make_state(tmp_path, api), "sql", "SELECT 1", "--env", "dev")

    assert result.exit_code == 130
    assert len(_cancel_requests(api)) == 1
    assert _outcome(result) == "Interrupted. Cancelled query q1."
    assert result.stdout == ""
    assert all(not r.url.path.endswith("/result") for r in api.requests)


def test_ctrl_c_in_sql_with_a_running_query_says_it_was_asked(tmp_path: Path) -> None:
    """A cancel the worker has yet to carry out is reported as a request."""
    result = invoke(
        make_state(tmp_path, _cancelling_api("RUNNING")), "sql", "SELECT 1", "--env", "dev"
    )

    assert result.exit_code == 130
    assert _outcome(result) == (
        "Interrupted. Asked query q1 to stop; check with 'lakana query status q1 --env dev'."
    )


def test_ctrl_c_in_a_write_cancels_it_too(tmp_path: Path) -> None:
    """The `--into` path cancels as well, and prints no write summary."""
    api = _cancelling_api()

    result = invoke(
        make_state(tmp_path, api),
        "sql",
        "SELECT 1",
        "--into",
        "gold.daily",
        "--mode",
        "replace",
        "--env",
        "dev",
    )

    assert result.exit_code == 130
    assert len(_cancel_requests(api)) == 1
    assert _outcome(result) == "Interrupted. Cancelled query q1."
    assert result.stdout == ""


def test_ctrl_c_in_query_wait_cancels_the_query_and_exits_130(tmp_path: Path) -> None:
    """`query wait` cancels the query it waits for, as `sql` does."""
    api = _cancelling_api()

    result = invoke(make_state(tmp_path, api), "query", "wait", "q1", "--env", "dev")

    assert result.exit_code == 130
    assert len(_cancel_requests(api)) == 1
    assert _outcome(result) == "Interrupted. Cancelled query q1."


def test_ctrl_c_after_the_query_ended_says_so(tmp_path: Path) -> None:
    """A query that finished in the instant of the Ctrl-C is not reported as cancelled."""
    result = invoke(
        make_state(tmp_path, _cancelling_api("SUCCEEDED")), "sql", "SELECT 1", "--env", "dev"
    )

    assert result.exit_code == 130
    assert _outcome(result) == "Interrupted. Query q1 had already ended (SUCCEEDED)."


def test_ctrl_c_in_the_instant_of_the_announcement_still_cancels_the_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Ctrl-C that lands while the announcement is written does not skip the cancel."""
    real_echo = typer.echo
    raised: list[bool] = []

    def echo(message: object = None, *args: Any, **kwargs: Any) -> None:  # ruff: ignore[any-type]
        """Raise a Ctrl-C once, on the announcement; write everything else.

        Parameters
        ----------
        message
            The text.
        *args
            Passed on.
        **kwargs
            Passed on.

        Raises
        ------
        KeyboardInterrupt
            The first time the announcement is written.
        """
        if not raised and "Cancelling query" in str(message):
            raised.append(True)
            raise KeyboardInterrupt
        real_echo(message, *args, **kwargs)

    monkeypatch.setattr(typer, "echo", echo)
    api = _cancelling_api()

    result = invoke(make_state(tmp_path, api), "sql", "SELECT 1", "--env", "dev")

    assert raised
    assert result.exit_code == 130
    assert len(_cancel_requests(api)) == 1
    assert result.stderr.strip() == "Interrupted. Cancelled query q1."


def test_ctrl_c_message_inlines_the_status_the_server_sent(tmp_path: Path) -> None:
    """The status is the server's text: one line, control characters escaped."""
    result = invoke(
        make_state(tmp_path, _cancelling_api("DONE\x1b[31m\nX")), "sql", "SELECT 1", "--env", "dev"
    )

    assert result.exit_code == 130
    assert _outcome(result) == "Interrupted. Query q1 had already ended (DONE\\x1b[31m\\nX)."


def test_ctrl_c_when_the_cancel_fails_names_the_command_to_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreachable server: the message names the query and the command to run later."""
    monkeypatch.setattr("lakana._http.time.sleep", lambda _seconds: None)
    api = _cancelling_api()

    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    api.routes["POST", f"{_Q}/q1/cancel"] = unreachable

    result = invoke(make_state(tmp_path, api), "sql", "SELECT 1", "--env", "dev")

    assert result.exit_code == 130
    assert _outcome(result) == (
        "Interrupted. The query could not be cancelled (Could not reach http://lakana.test: "
        "ConnectError) and may still be running; cancel it with "
        "'lakana query cancel q1 --env dev'."
    )


def test_a_second_ctrl_c_while_the_cancel_is_sent_exits_130_at_once(tmp_path: Path) -> None:
    """The user can always get out: the second Ctrl-C ends the attempt and says what is left."""
    api = _cancelling_api()
    api.routes["POST", f"{_Q}/q1/cancel"] = _interrupt

    result = invoke(make_state(tmp_path, api), "query", "wait", "q1", "--env", "dev")

    assert result.exit_code == 130
    assert _outcome(result) == (
        "Interrupted again. Query q1 may still be running; cancel it with "
        "'lakana query cancel q1 --env dev'."
    )


def test_ctrl_c_before_the_query_is_submitted_sends_no_cancel(tmp_path: Path) -> None:
    """No query id is known, so there is nothing to cancel and the exit status is 130."""
    api = FakeApi()
    api.routes["POST", _Q] = _interrupt

    result = invoke(make_state(tmp_path, api), "sql", "SELECT 1", "--env", "dev")

    assert result.exit_code == 130
    assert _cancel_requests(api) == []
    assert result.stderr.strip() == (
        "Interrupted before the server confirmed the query. No query id is known, so nothing "
        "was cancelled. If the request had already reached the server, the query may still be "
        "queued or running: list your queries with 'lakana query list --env dev' (add --all "
        "for the ones that just ended) and cancel it with 'lakana query cancel ID --env dev'."
    )


def test_ctrl_c_before_a_write_is_submitted_sends_no_cancel(tmp_path: Path) -> None:
    """The `--into` path behaves the same before the submission is confirmed."""
    api = FakeApi()
    api.routes["POST", _Q] = _interrupt

    result = invoke(
        make_state(tmp_path, api),
        "sql",
        "SELECT 1",
        "--into",
        "gold.daily",
        "--mode",
        "replace",
        "--env",
        "dev",
    )

    assert result.exit_code == 130
    assert _cancel_requests(api) == []
    assert "No query id is known" in result.stderr
    assert "'lakana query list --env dev'" in result.stderr


@pytest.mark.parametrize(
    ("status", "detail"),
    [(404, "No query run q1 in 'dev'."), (403, "Not allowed.")],
    ids=["someone-elses", "forbidden"],
)
def test_ctrl_c_in_query_wait_for_a_query_the_user_may_not_cancel(
    tmp_path: Path, status: int, detail: str
) -> None:
    """The wait ends, the refusal is shown plainly, and the exit status is 130."""
    api = _cancelling_api()
    api.on("POST", f"{_Q}/q1/cancel", status=status, json={"detail": detail})

    result = invoke(make_state(tmp_path, api), "query", "wait", "q1", "--env", "dev")

    assert result.exit_code == 130
    assert _outcome(result) == f"Interrupted. Query q1 was not cancelled: {detail}"


def test_ctrl_c_message_cannot_inject_terminal_control_characters(tmp_path: Path) -> None:
    """The refusal text comes from the server: its control characters are written as escapes."""
    api = _cancelling_api()
    api.on("POST", f"{_Q}/q1/cancel", status=404, json={"detail": "gone\x1b[31m bell\x07 cr\rX"})

    result = invoke(make_state(tmp_path, api), "query", "wait", "q1", "--env", "dev")

    assert result.exit_code == 130
    assert "\x07" not in result.stderr
    assert "\r" not in result.stderr
    assert _outcome(result) == (
        "Interrupted. Query q1 was not cancelled: gone\\x1b[31m bell\\x07 cr\\nX"
    )


def test_a_waiting_client_sees_a_cancelled_query_as_a_failed_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Someone else cancelled it: `sql` and `query wait` print the message and exit 9.

    The poll does not sleep and the wait has a bound, so a broken `CANCELLED` branch of the SDK
    fails in seconds (exit 9 with another message) instead of waiting for ever.
    """
    monkeypatch.setattr("lakana.resources.queries.time.sleep", lambda _seconds: None)
    api = _api(pa.table({}), status="CANCELLED", error="The query was cancelled.")

    sql = invoke(make_state(tmp_path, api), "sql", "SELECT 1", "--env", "dev", "--timeout", "2")
    waited = invoke(
        make_state(tmp_path, api), "query", "wait", "q1", "--env", "dev", "--timeout", "2"
    )

    for result in (sql, waited):
        assert result.exit_code == 9
        assert result.stderr.strip() == "Error: The query was cancelled."
    assert all(not r.url.path.endswith("/result") for r in api.requests)


def test_query_status_shows_a_cancelled_query(tmp_path: Path) -> None:
    """`query status` shows the status `CANCELLED` and its message."""
    api = _api(pa.table({}), status="CANCELLED", error="The query was cancelled.")

    result = invoke(make_state(tmp_path, api), "query", "status", "q1", "--env", "dev")

    assert result.exit_code == 0, result.stderr
    assert "CANCELLED" in result.stdout
    assert "The query was cancelled." in result.stdout


def test_ctrl_c_while_the_result_is_fetched_names_the_query(tmp_path: Path) -> None:
    """The query had finished: nothing is cancelled, and the message says how to fetch it."""
    api = _api(pa.table({"id": [1]}))
    api.routes["GET", f"{_Q}/q1/result"] = _interrupt

    result = invoke(make_state(tmp_path, api), "sql", "SELECT 1", "--env", "dev")

    assert result.exit_code == 130
    assert _cancel_requests(api) == []
    assert result.stderr.strip() == (
        "Interrupted. Query q1 had finished; fetch its result with "
        "'lakana query result q1 --env dev'."
    )


# -- `lakana query list` --------------------------------------

_LIST_ID = "3f2c9d1e-8b7a-4c55-9e0a-1b2c3d4e5f60"
_NOW = datetime(2026, 10, 9, 12, 0, 0, tzinfo=UTC)


def _entry(**more: object) -> dict[str, object]:
    """Build a `QueryListOut` row.

    Parameters
    ----------
    **more
        Fields that differ from a queued read of `alice` submitted 12 seconds ago.

    Returns
    -------
    dict
        The row.
    """
    return {
        "id": _LIST_ID,
        "environment": "dev",
        "origin": "written",
        "submitter": "alice",
        "kind": "read",
        "target": None,
        "status": "QUEUED",
        "created_at": (_NOW - timedelta(seconds=12)).isoformat(),
        "started_at": None,
        "finished_at": None,
        "error_cause": None,
        "waiting_for": None,
        **more,
    }


@pytest.fixture(autouse=True)
def _frozen_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the clock of the `age` column stand still.

    Parameters
    ----------
    monkeypatch
        Pytest's monkeypatch.
    """
    monkeypatch.setattr("lakana.cli.queries._now", lambda: _NOW)


def _list_api(*rows: dict[str, object]) -> FakeApi:
    """Build a fake that answers the list route with the given rows.

    Parameters
    ----------
    *rows
        The rows.

    Returns
    -------
    FakeApi
        The fake.
    """
    api = FakeApi()
    api.on("GET", _Q, json=list(rows))
    return api


def test_query_list_shows_the_table(tmp_path: Path) -> None:
    waiting = {
        "code": "workers_busy",
        "message": "All 2 workers are busy, and about 4 queries or deploys are ahead of this one.",
        "workers": 2,
        "ahead": 4,
    }
    api = _list_api(
        _entry(
            id="a" * 8 + _LIST_ID[8:],
            status="RUNNING",
            started_at=_NOW.isoformat(),
            created_at=(_NOW - timedelta(minutes=5)).isoformat(),
            kind="append",
            target="sales.orders",
            submitter="bob",
        ),
        _entry(waiting_for=waiting),
    )
    result = invoke(
        make_state(tmp_path, api), "query", "list", "--env", "dev", env={"COLUMNS": "200"}
    )
    assert result.exit_code == 0, result.stderr
    out = result.stdout
    assert "append to sales.orders" in out
    assert "5m" in out
    assert "12s" in out
    assert "All 2 workers are busy" in out
    assert "alice" in out
    assert "bob" in out
    request = api.requests[-1]
    assert request.url.params["include_ended"] == "false"
    assert request.url.params["limit"] == "100"


def test_query_list_all_asks_for_ended_runs(tmp_path: Path) -> None:
    api = _list_api(_entry(status="SUCCEEDED", finished_at=_NOW.isoformat()))
    result = invoke(
        make_state(tmp_path, api), "query", "list", "--env", "dev", "--all", "--limit", "7"
    )
    assert result.exit_code == 0, result.stderr
    assert api.requests[-1].url.params["include_ended"] == "true"
    assert api.requests[-1].url.params["limit"] == "7"
    assert "SUCCEEDED" in result.stdout


def test_query_list_json_has_the_whole_reason_and_no_sql(tmp_path: Path) -> None:
    waiting = {"code": "starting", "message": "A worker is free.", "workers": 1, "ahead": 0}
    rows = [_entry(waiting_for=waiting)]
    result = invoke(
        make_state(tmp_path, _list_api(*rows)), "--output", "json", "query", "list", "--env", "dev"
    )
    assert result.exit_code == 0, result.stderr
    (row,) = json.loads(result.stdout)
    assert set(row) == set(rows[0])
    assert row["waiting_for"]["code"] == "starting"
    assert row["waiting_for"]["ahead"] == 0
    assert not {"sql_text", "error_message", "lineage"} & set(row)


@pytest.mark.parametrize(
    ("kind", "target", "shown"),
    [
        ("read", None, "read"),
        ("create", "gold.daily", "create gold.daily"),
        ("replace", "gold.daily", "replace gold.daily"),
        ("append", "gold.daily", "append to gold.daily"),
        ("view", "gold.daily", "view gold.daily"),
        ("maintain", "gold.daily", "maintenance of gold.daily"),
        ("future-kind", None, "future-kind"),
    ],
)
def test_kind_and_age_are_written_as_documented(
    tmp_path: Path, kind: str, target: str | None, shown: str
) -> None:
    api = _list_api(_entry(kind=kind, target=target))
    result = invoke(
        make_state(tmp_path, api), "query", "list", "--env", "dev", env={"COLUMNS": "200"}
    )
    assert result.exit_code == 0, result.stderr
    assert re.search(rf"│ {re.escape(shown)}\s+│", result.stdout), result.stdout


@pytest.mark.parametrize(
    ("seconds", "shown"),
    [
        (-5, "0s"),
        (0, "0s"),
        (12, "12s"),
        (59, "59s"),
        (60, "1m"),
        (179, "2m"),
        (3599, "59m"),
        (3600, "1h"),
        (7300, "2h"),
        (86_399, "23h"),
        (86_400, "1d"),
        (3 * 86_400 + 5, "3d"),
    ],
)
def test_age_uses_the_largest_unit_that_is_at_least_one(
    tmp_path: Path, seconds: int, shown: str
) -> None:
    api = _list_api(_entry(created_at=(_NOW - timedelta(seconds=seconds)).isoformat()))
    result = invoke(
        make_state(tmp_path, api), "query", "list", "--env", "dev", env={"COLUMNS": "200"}
    )
    assert re.search(rf"│ {shown}\s+│", result.stdout), result.stdout


def test_an_empty_list_says_so(tmp_path: Path) -> None:
    state = make_state(tmp_path, _list_api())
    queued = invoke(state, "query", "list", "--env", "dev")
    assert queued.stdout.strip() == "No queued or running queries in dev."
    ended = invoke(state, "query", "list", "--env", "dev", "--all")
    assert ended.stdout.strip() == "No queries in dev in the last 24 hours."
    as_json = invoke(state, "--output", "json", "query", "list", "--env", "dev")
    assert json.loads(as_json.stdout) == []


def _limited_api(count: int) -> FakeApi:
    """Build a fake whose list route returns at most the `limit` it is sent of `count` rows.

    It sends the cursor `cursor-<limit>` when it holds more.

    Parameters
    ----------
    count
        How many queries the environment has.

    Returns
    -------
    FakeApi
        The fake.
    """
    rows = [_entry(id=f"{n:08x}" + _LIST_ID[8:]) for n in range(count)]
    api = FakeApi()

    def respond(request: httpx.Request) -> httpx.Response:
        limit = int(request.url.params["limit"])
        headers = {"X-Next-Cursor": f"cursor-{limit}"} if count > limit else {}
        return httpx.Response(200, json=rows[:limit], headers=headers)

    api.routes["GET", _Q] = respond
    return api


def test_a_cut_list_names_the_command_for_the_next_page(tmp_path: Path) -> None:
    wide = {"COLUMNS": "200"}
    cut = invoke(
        make_state(tmp_path, _limited_api(5)),
        "query",
        "list",
        "--env",
        "dev",
        "--limit",
        "3",
        "--all",
        "--status",
        "queued,failed",
        "--submitter",
        "bob",
        env=wide,
    )
    assert cut.stdout.count("-8b7a-4c55") == 3
    assert (
        "More results: lakana query list --env dev --all --status queued,failed "
        "--submitter bob --limit 3 --cursor cursor-3" in cut.stdout
    )
    exact = invoke(
        make_state(tmp_path, _limited_api(3)),
        "query",
        "list",
        "--env",
        "dev",
        "--limit",
        "3",
        env=wide,
    )
    assert exact.stdout.count("-8b7a-4c55") == 3
    assert "More results" not in exact.stdout


def test_the_next_page_hint_of_a_json_listing_goes_to_stderr(tmp_path: Path) -> None:
    as_json = invoke(
        make_state(tmp_path, _limited_api(5)),
        "--output",
        "json",
        "query",
        "list",
        "--env",
        "dev",
        "--limit",
        "3",
    )
    assert "More results" not in as_json.stdout
    assert "--cursor cursor-3" in as_json.stderr
    assert len(json.loads(as_json.stdout)) == 3


def test_a_cursor_is_sent_and_a_filter_that_finds_nothing_says_so(tmp_path: Path) -> None:
    api = _list_api()
    state = make_state(tmp_path, api)
    result = invoke(state, "query", "list", "--env", "dev", "--status", "failed", "--cursor", "abc")
    assert "No queries in dev match." in result.stdout
    params = api.requests[-1].url.params
    assert (params["status"], params["cursor"]) == ("failed", "abc")
    assert "submitter" not in params
    named = invoke(state, "query", "list", "--env", "dev", "--submitter", "bob")
    assert "No queries in dev match." in named.stdout
    assert api.requests[-1].url.params["submitter"] == "bob"


def test_query_list_all_environments_adds_the_environment_column(tmp_path: Path) -> None:
    api = FakeApi()
    rows = [
        _entry(environment="prod", origin="written"),
        _entry(id="b" * 8 + _LIST_ID[8:], environment="stage", origin="task"),
    ]
    api.on("GET", "/api/v1/queries", json={"items": rows, "next_cursor": "all-1"})
    result = invoke(
        make_state(tmp_path, api),
        "query",
        "list",
        "--all-environments",
        "--all",
        "--since",
        "2026-10-01T00:00:00Z",
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 0, result.stderr
    header = next(line for line in result.stdout.splitlines() if " id " in line)
    assert header.index("environment") < header.index("submitter")
    assert header.index("id") < header.index("environment")
    assert "prod" in result.stdout
    assert "stage" in result.stdout
    params = api.requests[-1].url.params
    assert "environment" not in params
    assert (params["include_ended"], params["since"]) == ("true", "2026-10-01T00:00:00+00:00")
    assert (
        "More results: lakana query list --all-environments --all --since 2026-10-01T00:00:00Z "
        "--cursor all-1" in result.stdout
    )
    both = invoke(make_state(tmp_path, api), "query", "list", "--all-environments", "--env", "dev")
    assert both.exit_code == 2
    assert "--all-environments" in both.stderr
    empty = FakeApi()
    empty.on("GET", "/api/v1/queries", json={"items": [], "next_cursor": None})
    nothing = invoke(make_state(tmp_path, empty), "query", "list", "--all-environments")
    assert "No queued or running queries in any environment." in nothing.stdout


def test_query_list_origin_is_repeatable(tmp_path: Path) -> None:
    api = _list_api(_entry(origin="task", environment="dev"))
    result = invoke(
        make_state(tmp_path, api),
        "query",
        "list",
        "--env",
        "dev",
        "--origin",
        "written",
        "--origin",
        "task",
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 0, result.stderr
    assert api.requests[-1].url.params["origin"] == "written,task"
    header = next(line for line in result.stdout.splitlines() if " id " in line)
    assert "environment" not in header


def test_query_list_limit_is_bounded_before_a_request(tmp_path: Path) -> None:
    api = _list_api()
    state = make_state(tmp_path, api)
    for bad in ("0", "201"):
        assert invoke(state, "query", "list", "--env", "dev", "--limit", bad).exit_code == 2
    assert api.requests == []


def test_text_the_server_sent_cannot_inject_terminal_controls_into_the_list(tmp_path: Path) -> None:
    api = _list_api(
        _entry(
            submitter="eve\x1b[31m",
            kind="create",
            target="t\x1b[2J\nx",
            waiting_for={"code": "starting", "message": "hi\x1b]0;owned\x07"},
        )
    )
    result = invoke(
        make_state(tmp_path, api), "query", "list", "--env", "dev", env={"COLUMNS": "200"}
    )
    assert "\x1b" not in result.stdout
    assert "\x07" not in result.stdout
    assert "\\x1b" in result.stdout


# --- The reason a query waits --------------------------------

_BUSY = "All 2 workers are busy, and about 4 queries or deploys are ahead of this one."
_NONE = "No worker is running, so this query cannot start. Ask the operator to start a worker."
_STATUS = QueryStatus(id="q1", status="RUNNING", error_message=None, lineage=None)


def _queued(code: str, message: str) -> QueryStatus:
    """Build a QUEUED status with a wait reason.

    Parameters
    ----------
    code
        The reason's code.
    message
        The reason's sentence.

    Returns
    -------
    QueryStatus
        The status.
    """
    return dataclasses.replace(_STATUS, status="QUEUED", waiting_for=WaitingFor(code, message))


class _Clock:
    """A clock moved by hand.

    Parameters
    ----------
    now
        The time it shows.
    """

    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        """Read the clock.

        Returns
        -------
        float
            The time.
        """
        return self.now


def _line(tmp_path: Path, clock: _Clock, output: str = "table") -> WaitLine:
    """Build a wait line on a state with the given output mode.

    Parameters
    ----------
    tmp_path
        Pytest's per-test directory.
    clock
        The clock.
    output
        The output mode.

    Returns
    -------
    WaitLine
        The callable under test.
    """
    state: CliState = make_state(tmp_path, FakeApi())
    state.output = output
    return WaitLine(state, monotonic=clock)


def _primed(tmp_path: Path) -> tuple[WaitLine, _Clock]:
    """Build a wait line that saw a QUEUED status without a reason at 0 s and is now at 5 s.

    Parameters
    ----------
    tmp_path
        Pytest's per-test directory.

    Returns
    -------
    tuple of (WaitLine, _Clock)
        The line and its clock.
    """
    clock = _Clock()
    line = _line(tmp_path, clock)
    line(dataclasses.replace(_STATUS, status="QUEUED"))
    clock.now = 5.0
    return line, clock


def test_the_wait_line_comes_after_three_seconds_and_again_only_when_the_code_changes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    clock = _Clock()
    line = _line(tmp_path, clock)
    line(_queued("workers_busy", _BUSY))
    clock.now = WAIT_LINE_AFTER_SECONDS - 0.1
    line(_queued("workers_busy", _BUSY))
    assert capsys.readouterr() == ("", "")
    clock.now = WAIT_LINE_AFTER_SECONDS
    line(_queued("workers_busy", _BUSY))
    line(_queued("workers_busy", _BUSY.replace("about 4", "about 3")))  # only a number changed
    line(_queued("no_worker", _NONE))
    out, err = capsys.readouterr()
    assert out == ""
    assert err.splitlines() == [f"Waiting: {_BUSY}", f"Waiting: {_NONE}"]


def test_a_code_that_comes_back_is_printed_again(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    clock = _Clock()
    line = _line(tmp_path, clock)
    line(_queued("no_worker", _NONE))
    clock.now = 5.0
    for code, message in (
        ("no_worker", _NONE),
        ("starting", "A worker is free."),
        ("no_worker", _NONE),
    ):
        line(_queued(code, message))
    assert capsys.readouterr().err.splitlines() == [
        f"Waiting: {_NONE}",
        "Waiting: A worker is free.",
        f"Waiting: {_NONE}",
    ]


def test_no_wait_line_with_json_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    clock = _Clock(10.0)
    line = _line(tmp_path, clock, output="json")
    line(_queued("workers_busy", _BUSY))
    clock.now = 20.0
    line(_queued("workers_busy", _BUSY))
    assert capsys.readouterr() == ("", "")


def test_a_running_or_finished_status_prints_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    clock = _Clock()
    line = _line(tmp_path, clock)
    clock.now = 10.0
    for status in ("RUNNING", "SUCCEEDED", "FAILED", "CANCELLED"):
        line(dataclasses.replace(_queued("workers_busy", _BUSY), status=status))
    assert capsys.readouterr() == ("", "")


def test_a_status_without_a_reason_prints_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    clock = _Clock()
    line = _line(tmp_path, clock)
    line(dataclasses.replace(_STATUS, status="QUEUED"))
    clock.now = 10.0
    line(dataclasses.replace(_STATUS, status="QUEUED"))
    assert capsys.readouterr() == ("", "")


def test_a_query_that_starts_within_three_seconds_prints_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    clock = _Clock()
    line = _line(tmp_path, clock)
    line(_queued("starting", "A worker is free; the query starts within about a second."))
    clock.now = 1.5
    line(dataclasses.replace(_STATUS, status="RUNNING"))
    clock.now = 9.0
    line(dataclasses.replace(_STATUS, status="SUCCEEDED"))
    assert capsys.readouterr() == ("", "")


def test_the_three_seconds_count_from_the_first_queued_status_this_command_saw(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    clock = _Clock(1000.0)
    line = _line(tmp_path, clock)
    line(_queued("workers_busy", _BUSY))
    clock.now = 1002.9
    line(_queued("workers_busy", _BUSY))
    assert capsys.readouterr() == ("", "")
    clock.now = 1003.0
    line(_queued("workers_busy", _BUSY))
    assert capsys.readouterr().err == f"Waiting: {_BUSY}\n"


def test_control_characters_in_a_reason_are_escaped(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    line, clock = _primed(tmp_path)
    line(_queued("upstream", "Waiting for upstream tasks: a\x1b[31m, b\nFAKE\x07."))
    clock.now = 10.0
    line(_queued("upstream", "x"))
    err = capsys.readouterr().err
    assert "\x1b" not in err and "\x07" not in err
    assert err == "Waiting: Waiting for upstream tasks: a\\x1b[31m, b\\nFAKE\\x07.\n"


def test_a_name_the_server_already_escaped_is_not_escaped_twice(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    line, clock = _primed(tmp_path)
    sent = "Waiting for upstream tasks: a\\x1b[31m."  # a literal backslash and x1b
    line(_queued("upstream", sent))
    clock.now = 10.0
    line(_queued("upstream", sent))
    assert capsys.readouterr().err == f"Waiting: {sent}\n"


def test_a_long_reason_is_cut(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    line, clock = _primed(tmp_path)
    line(_queued("upstream", "n" * 5000))
    clock.now = 10.0
    line(_queued("upstream", "n" * 5000))
    err = capsys.readouterr().err
    assert err.endswith("…\n")
    assert len(err) < 600


def test_a_code_the_cli_does_not_know_is_printed_with_its_message(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    line, clock = _primed(tmp_path)
    line(_queued("from_the_future", "Something newer."))
    clock.now = 10.0
    line(_queued("from_the_future", "Something newer, again."))
    assert capsys.readouterr().err == "Waiting: Something newer.\n"


def test_a_reason_without_a_message_prints_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    line, clock = _primed(tmp_path)
    clock.now = 10.0
    line(_queued("workers_busy", ""))
    line(_queued("workers_busy", "   "))
    assert capsys.readouterr() == ("", "")
    line(_queued("workers_busy", _BUSY))  # the same code, now with a sentence
    assert capsys.readouterr().err == f"Waiting: {_BUSY}\n"


_USER_LIMIT = (
    "You have 2 queries running, which is the limit per user here; this one can start when one "
    "of them ends. 'lakana query list' shows them."
)
_NO_KIND = (
    "No worker that takes interactive work is running (2 workers take pipeline work only), so "
    "this query cannot start. Ask the operator to start a worker with "
    "LAKANA_WORKER__TAKES=interactive or all."
)
_NEW_REASONS = [
    ("user_limit", _USER_LIMIT, {"running": 2, "limit": 2}),
    ("no_worker_of_kind", _NO_KIND, {"workers": 0, "other_workers": 2}),
]


@pytest.mark.parametrize(("code", "message", "more"), _NEW_REASONS)
def test_the_wait_line_prints_a_new_reason_once_and_again_when_the_code_changes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], code: str, message: str, more: dict
) -> None:
    line, clock = _primed(tmp_path)
    line(_queued(code, message))
    clock.now = 10.0
    line(_queued(code, message.replace("2", "3")))  # the sentence changed, the code did not
    line(_queued("workers_busy", _BUSY))
    line(_queued(code, message))
    assert capsys.readouterr().err.splitlines() == [
        f"Waiting: {message}",
        f"Waiting: {_BUSY}",
        f"Waiting: {message}",
    ]


def test_the_wait_line_prints_a_user_limit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    line, clock = _primed(tmp_path)
    line(_queued("user_limit", _USER_LIMIT))
    clock.now = 20.0
    line(_queued("user_limit", _USER_LIMIT))
    assert capsys.readouterr().err == f"Waiting: {_USER_LIMIT}\n"


@pytest.mark.parametrize(("code", "message", "more"), _NEW_REASONS)
def test_a_new_reason_is_escaped_and_cut_in_the_wait_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], code: str, message: str, more: dict
) -> None:
    line, _clock = _primed(tmp_path)
    line(_queued(code, "x\x1b[31m" + "n" * 5000))
    err = capsys.readouterr().err
    assert err.startswith("Waiting: x\\x1b[31m")
    assert "\x1b" not in err
    assert err.endswith("…\n")
    assert len(err) < 600


@pytest.mark.usefixtures("_slow_clock")
@pytest.mark.parametrize(("code", "message", "more"), _NEW_REASONS)
@pytest.mark.parametrize("command", ["sql", "wait"])
def test_sql_and_query_wait_print_a_new_reason(
    tmp_path: Path, code: str, message: str, more: dict, command: str
) -> None:
    polls = [
        _queued_body(code, message, **more),
        _queued_body(code, message, **more),
        _queued_body(code, message, **more),
        _status_body("SUCCEEDED"),
    ]
    args = (
        ["sql", "SELECT 1", "--env", "dev"]
        if command == "sql"
        else ["query", "wait", "q1", "--env", "dev"]
    )

    result = invoke(make_state(tmp_path, _waiting_api(polls)), *args)

    assert result.exit_code == 0, result.stderr
    assert result.stderr == f"Waiting: {message}\n"


@pytest.mark.parametrize(("code", "message", "more"), _NEW_REASONS)
def test_query_status_and_query_list_show_a_new_reason(
    tmp_path: Path, code: str, message: str, more: dict
) -> None:
    status = invoke(
        make_state(tmp_path, _waiting_api([_queued_body(code, message, **more)])),
        "--output",
        "json",
        "query",
        "status",
        "q1",
        "--env",
        "dev",
    )
    assert json.loads(status.stdout)["waiting_for"]["code"] == code
    reason = {"code": code, "message": message, **more}
    listed = invoke(
        make_state(tmp_path, _list_api(_entry(waiting_for=reason))),
        "query",
        "list",
        "--env",
        "dev",
        env={"COLUMNS": "400"},
    )
    assert listed.exit_code == 0, listed.stderr
    assert " ".join(listed.stdout.split()).count(message.split(",")[0]) == 1
    table = invoke(
        make_state(tmp_path, _waiting_api([_queued_body(code, "x\x1b[31m" + "n" * 5000, **more)])),
        "query",
        "status",
        "q1",
        "--env",
        "dev",
    )
    assert "\x1b" not in table.stdout
    assert "…" in table.stdout
    assert len(table.stdout) < 3000


def test_an_unknown_code_from_a_newer_server_is_printed_as_its_message(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    line, _clock = _primed(tmp_path)
    line(_queued("a_reason_of_a_later_release", "Something newer happens."))
    assert capsys.readouterr().err == "Waiting: Something newer happens.\n"


def _queued_body(code: str, message: str, **more: object) -> dict[str, object]:
    """Build the JSON body of a QUEUED query that waits.

    Parameters
    ----------
    code
        The reason's code.
    message
        The reason's sentence.
    **more
        Further fields of the reason.

    Returns
    -------
    dict of str to object
        A `QueryStatusOut` body.
    """
    body = _status_body("QUEUED")
    body["waiting_for"] = {"code": code, "message": message, **more}
    return body


def _waiting_api(polls: list[dict[str, object]]) -> FakeApi:
    """Build a fake whose status polls answer `polls` in order, the last one repeating.

    Parameters
    ----------
    polls
        The bodies.

    Returns
    -------
    FakeApi
        The fake, with a result for the last poll.
    """
    api = _api(pa.table({"id": [1]}))
    answers = iter(polls)
    last = polls[-1]

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal last
        last = next(answers, last)
        return httpx.Response(200, json=last)

    api.routes["GET", f"{_Q}/q1"] = respond
    return api


@pytest.fixture
def _slow_clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    """Make every sleep of a poll five seconds on the clock of the wait line.

    Parameters
    ----------
    monkeypatch
        Pytest's monkeypatch.

    Returns
    -------
    _Clock
        The clock.
    """
    clock = _Clock()
    monkeypatch.setattr("lakana.cli.queries.clock", clock)
    monkeypatch.setattr(
        "lakana.resources.queries.time.sleep",
        lambda _seconds: setattr(clock, "now", clock.now + 5.0),
    )
    return clock


def _three_polls() -> list[dict[str, object]]:
    """Build the answers of a query that waits behind a busy worker and then runs.

    Returns
    -------
    list of dict
        QUEUED at 0 s, 5 s and 10 s (the third with another number), then SUCCEEDED.
    """
    return [
        _queued_body("workers_busy", _BUSY, workers=2, ahead=4),
        _queued_body("workers_busy", _BUSY, workers=2, ahead=4),
        _queued_body("workers_busy", _BUSY.replace("about 4", "about 1"), workers=2, ahead=1),
        _status_body("SUCCEEDED"),
    ]


@pytest.mark.usefixtures("_slow_clock")
def test_sql_prints_the_wait_line_and_its_result_and_exit_code_are_unchanged(
    tmp_path: Path,
) -> None:
    waiting = _waiting_api(_three_polls())
    quick = _api(pa.table({"id": [1]}))

    slow = invoke(make_state(tmp_path, waiting), "sql", "SELECT 1", "--env", "dev")
    plain = invoke(make_state(tmp_path, quick), "sql", "SELECT 1", "--env", "dev")

    assert slow.exit_code == 0, slow.stderr
    assert slow.stdout == plain.stdout
    assert slow.stderr == f"Waiting: {_BUSY}\n"


@pytest.mark.usefixtures("_slow_clock")
def test_sql_prints_nothing_about_waiting_with_json_output(tmp_path: Path) -> None:
    waiting = _waiting_api(_three_polls())

    result = invoke(
        make_state(tmp_path, waiting), "--output", "json", "sql", "SELECT 1", "--env", "dev"
    )

    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout) == [{"id": 1}]
    assert result.stderr == ""


@pytest.mark.usefixtures("_slow_clock")
def test_sql_into_prints_the_wait_line(tmp_path: Path) -> None:
    waiting = _waiting_api(_three_polls())

    result = invoke(
        make_state(tmp_path, waiting),
        "sql",
        "SELECT 1",
        "--into",
        "gold.daily",
        "--mode",
        "append",
        "--env",
        "dev",
    )

    assert result.exit_code == 0, result.stderr
    assert result.stderr == f"Waiting: {_BUSY}\n"
    assert "Wrote" in result.stdout


@pytest.mark.usefixtures("_slow_clock")
def test_query_wait_prints_the_wait_line(tmp_path: Path) -> None:
    waiting = _waiting_api(_three_polls())
    quick = _api(pa.table({"id": [1]}))

    slow = invoke(make_state(tmp_path, waiting), "query", "wait", "q1", "--env", "dev")
    plain = invoke(make_state(tmp_path, quick), "query", "wait", "q1", "--env", "dev")

    assert slow.exit_code == 0, slow.stderr
    assert slow.stdout == plain.stdout
    assert slow.stderr == f"Waiting: {_BUSY}\n"


@pytest.mark.usefixtures("_slow_clock")
def test_ctrl_c_after_a_wait_line_cancels_as_before(tmp_path: Path) -> None:
    polls = _three_polls()[:2]
    api = _waiting_api(polls)
    api.routes["POST", f"{_Q}/q1/cancel"] = lambda request: httpx.Response(
        200, json=_status_body("CANCELLED")
    )
    first = api.routes["GET", f"{_Q}/q1"]
    seen: list[int] = []

    def interrupted(request: httpx.Request) -> httpx.Response:
        seen.append(1)
        if len(seen) > 2:
            raise KeyboardInterrupt
        return first(request)

    api.routes["GET", f"{_Q}/q1"] = interrupted

    result = invoke(make_state(tmp_path, api), "sql", "SELECT 1", "--env", "dev")

    assert result.exit_code == 130
    assert result.stderr.splitlines() == [
        f"Waiting: {_BUSY}",
        "Interrupted. Cancelling query q1; press Ctrl-C again to stop waiting for it.",
        "Interrupted. Cancelled query q1.",
    ]


def test_query_status_has_a_waiting_for_column(tmp_path: Path) -> None:
    api = _waiting_api([_queued_body("no_worker", _NONE, workers=0, ahead=0)])
    state = make_state(tmp_path, api)

    table = invoke(state, "query", "status", "q1", "--env", "dev")
    as_json = invoke(state, "--output", "json", "query", "status", "q1", "--env", "dev")

    assert table.exit_code == 0, table.stderr
    cells = [" ".join(line.split()) for line in table.stdout.splitlines()]
    labels = [line.split("│")[1].strip() for line in table.stdout.splitlines() if "│" in line]
    assert labels == ["id", "status", "waiting_for", "error_message"]
    assert _NONE.split(",")[0] in " ".join(cells)
    body = json.loads(as_json.stdout)
    assert body["waiting_for"]["code"] == "no_worker"
    assert body["waiting_for"]["message"] == _NONE


def test_query_status_of_a_query_that_is_not_waiting_has_an_empty_cell(tmp_path: Path) -> None:
    api = _api(pa.table({}), status="RUNNING")

    result = invoke(make_state(tmp_path, api), "query", "status", "q1", "--env", "dev")

    assert result.exit_code == 0, result.stderr
    assert "waiting_for" in result.stdout
    assert "Waiting" not in result.stdout


# --- malformed responses -------------------------------------------

_MALFORMED: list[object] = [
    "a string",
    [1],
    7,
    {"code": "x"},
    {"code": "x", "message": None},
    {"code": "x", "message": 5},
]


def _nothing_waits(result: Result) -> None:
    """Assert that a command ran as if nothing waited: no error, no traceback, no wait line.

    Parameters
    ----------
    result
        The invocation.
    """
    assert result.exception is None
    assert result.exit_code == 0, result.stderr
    assert result.stderr == ""
    assert "Error" not in result.stdout and "Traceback" not in result.stdout


@pytest.mark.usefixtures("_slow_clock")
@pytest.mark.parametrize("reason", _MALFORMED)
def test_a_malformed_reason_does_not_break_sql_or_query_wait(
    tmp_path: Path, reason: object
) -> None:
    def polls() -> list[dict[str, object]]:
        queued = {**_status_body("QUEUED"), "waiting_for": reason}
        return [queued, queued, queued, _status_body("SUCCEEDED")]

    sql = invoke(make_state(tmp_path, _waiting_api(polls())), "sql", "SELECT 1", "--env", "dev")
    waited = invoke(
        make_state(tmp_path, _waiting_api(polls())), "query", "wait", "q1", "--env", "dev"
    )

    _nothing_waits(sql)
    _nothing_waits(waited)


@pytest.mark.parametrize("reason", _MALFORMED)
def test_a_malformed_reason_does_not_break_query_status_or_query_list(
    tmp_path: Path, reason: object
) -> None:
    queued = {**_status_body("QUEUED"), "waiting_for": reason}
    status = invoke(
        make_state(tmp_path, _waiting_api([queued])), "query", "status", "q1", "--env", "dev"
    )
    listed = invoke(
        make_state(tmp_path, _list_api(_entry(waiting_for=reason))), "query", "list", "--env", "dev"
    )

    _nothing_waits(status)
    assert "QUEUED" in status.stdout
    _nothing_waits(listed)
    assert "QUEUED" in listed.stdout


def test_the_clock_starts_at_the_first_queued_status_not_at_construction(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    clock = _Clock(0.0)
    line = _line(tmp_path, clock)
    clock.now = 1000.0
    line(_queued("workers_busy", _BUSY))
    clock.now = 1002.9
    line(_queued("workers_busy", _BUSY))
    assert capsys.readouterr() == ("", "")
    clock.now = 1003.0
    line(_queued("workers_busy", _BUSY))
    assert capsys.readouterr().err == f"Waiting: {_BUSY}\n"


@pytest.mark.usefixtures("_slow_clock")
@pytest.mark.parametrize("output", ["table", "json"])
@pytest.mark.parametrize("command", ["sql", "wait"])
def test_standard_output_is_byte_for_byte_what_it_is_without_waiting(
    tmp_path: Path, output: str, command: str
) -> None:
    args = (
        ["sql", "SELECT 1", "--env", "dev"]
        if command == "sql"
        else ["query", "wait", "q1", "--env", "dev"]
    )
    slow = invoke(make_state(tmp_path, _waiting_api(_three_polls())), "--output", output, *args)
    quick = invoke(make_state(tmp_path, _api(pa.table({"id": [1]}))), "--output", output, *args)

    assert slow.exit_code == quick.exit_code == 0, slow.stderr
    assert slow.stdout == quick.stdout
    assert slow.stderr == ("" if output == "json" else f"Waiting: {_BUSY}\n")


@pytest.mark.usefixtures("_slow_clock")
def test_ctrl_c_raised_while_the_wait_line_is_printed_still_cancels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = _waiting_api(_three_polls())
    api.routes["POST", f"{_Q}/q1/cancel"] = lambda request: httpx.Response(
        200, json=_status_body("CANCELLED")
    )
    echo = typer.echo

    def interrupting(message: str, err: bool = False) -> None:
        if message.startswith("Waiting:"):
            raise KeyboardInterrupt
        echo(message, err=err)

    monkeypatch.setattr(typer, "echo", interrupting)

    result = invoke(make_state(tmp_path, api), "sql", "SELECT 1", "--env", "dev")

    assert result.exit_code == 130
    assert result.stderr.splitlines() == [
        "Interrupted. Cancelling query q1; press Ctrl-C again to stop waiting for it.",
        "Interrupted. Cancelled query q1.",
    ]


def test_a_long_reason_is_cut_in_the_status_cell(tmp_path: Path) -> None:
    api = _waiting_api([_queued_body("workers_busy", "word " * 1000)])

    result = invoke(make_state(tmp_path, api), "query", "status", "q1", "--env", "dev")

    assert result.exit_code == 0, result.stderr
    assert "…" in result.stdout
    assert len(result.stdout) < 3000


def test_a_long_reason_is_cut_in_the_list_cell(tmp_path: Path) -> None:
    waiting = {"code": "workers_busy", "message": "word " * 1000}

    result = invoke(
        make_state(tmp_path, _list_api(_entry(waiting_for=waiting))),
        "query",
        "list",
        "--env",
        "dev",
    )

    assert result.exit_code == 0, result.stderr
    assert "…" in result.stdout
    assert len(result.stdout) < 3000
