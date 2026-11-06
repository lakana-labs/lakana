"""The SDK's runs resource: the figures an attempt used."""

from __future__ import annotations

from typing import Any

import httpx
from test_resources_jobs import _RUN

from lakana._config import ResolvedConfig
from lakana._http import HttpTransport
from lakana.resources.queries import _status_from_json
from lakana.resources.runs import Runs, run_from_json

_CONFIG = ResolvedConfig(url="http://api.example", token="lakana_pat_abc_supersecret")
_TASK: dict[str, Any] = {
    "asset": "silver.clean",
    "status": "succeeded",
    "attempt": 2,
    "max_attempts": 3,
    "timeout_seconds": 900,
    "next_attempt_at": None,
    "started_at": "2026-10-14T12:00:00+00:00",
    "finished_at": "2026-10-14T12:00:02+00:00",
    "error_message": None,
    "query_run_id": None,
    "rows_written": 2,
}
_ATTEMPT: dict[str, Any] = {
    "attempt": 1,
    "worker": "w1",
    "started_at": "2026-10-14T12:00:00+00:00",
    "finished_at": "2026-10-14T12:00:02+00:00",
    "outcome": "succeeded",
    "rows_written": 2,
    "error_message": None,
}


def test_a_task_carries_the_figures_of_each_attempt() -> None:
    """The run detail's `attempts` become `AttemptResources`, unknown figures `None`."""
    attempts = [
        {"attempt": 1, "peak_memory_bytes": None, "cpu_seconds": None, "bytes_read": None},
        {
            "attempt": 2,
            "peak_memory_bytes": 432013312,
            "cpu_seconds": 3.1,
            "bytes_read": 1288490188,
        },
    ]
    run = run_from_json({**_RUN, "tasks": [{**_TASK, "attempts": attempts}]})
    first, second = run.tasks[0].attempts
    assert (first.attempt, first.peak_memory_bytes, first.cpu_seconds, first.bytes_read) == (
        1,
        None,
        None,
        None,
    )
    assert (second.peak_memory_bytes, second.cpu_seconds, second.bytes_read) == (
        432013312,
        3.1,
        1288490188,
    )


def test_a_task_from_an_older_server_has_no_attempts() -> None:
    """A body without the key reads as an empty tuple."""
    assert run_from_json({**_RUN, "tasks": [_TASK]}).tasks[0].attempts == ()


def _log(attempt: dict[str, Any]) -> Any:  # ruff: ignore[any-type]
    """Read a task log whose only attempt is `attempt`.

    Parameters
    ----------
    attempt
        The attempt body.

    Returns
    -------
    TaskLog
        The parsed log.
    """
    body = {
        "run_id": _RUN["id"],
        "asset": "silver.clean",
        "text": "",
        "attempts": [attempt],
        "next_offset": 0,
        "more": False,
        "complete": True,
        "reset": False,
    }
    client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body)),
        base_url=_CONFIG.url,
    )
    return Runs(HttpTransport(_CONFIG, http_client=client)).log("dev", _RUN["id"], "silver.clean")


def test_a_logged_attempt_parses_the_figures_and_defaults_missing_keys_to_none() -> None:
    """`TaskAttempt` has the figures; an older server's attempt has `None`."""
    measured = _log(
        {**_ATTEMPT, "peak_memory_bytes": 5, "cpu_seconds": 0.5, "bytes_read": 0}
    ).attempts[0]
    assert (measured.peak_memory_bytes, measured.cpu_seconds, measured.bytes_read) == (5, 0.5, 0)
    older = _log(_ATTEMPT).attempts[0]
    assert (older.peak_memory_bytes, older.cpu_seconds, older.bytes_read) == (None, None, None)


def test_a_query_status_parses_the_figures() -> None:
    """`QueryStatus` carries the three figures; an older server's status has `None`."""
    base = {"query_id": "q1", "status": "SUCCEEDED"}
    older = _status_from_json(base)
    assert (older.peak_memory_bytes, older.cpu_seconds, older.bytes_read) == (None, None, None)
    status = _status_from_json(
        {**base, "peak_memory_bytes": 7, "cpu_seconds": 1.5, "bytes_read": 9}
    )
    assert (status.peak_memory_bytes, status.cpu_seconds, status.bytes_read) == (7, 1.5, 9)


def test_task_history_asks_for_a_page_and_parses_the_entries() -> None:
    """The route, the query and the entries (`None` for a figure not measured)."""
    seen: list[httpx.Request] = []
    entry = {
        "run_id": _RUN["id"],
        "attempt": 2,
        "status": "failed",
        "started_at": "2026-11-01T12:00:00+00:00",
        "finished_at": "2026-11-01T12:00:12+00:00",
        "duration_seconds": 12.0,
        "peak_memory_bytes": 432013312,
        "cpu_seconds": 3.1,
        "bytes_read": None,
        "failure_code": "out_of_memory",
    }
    running = {**entry, "attempt": 3, "status": "running", "finished_at": None}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"attempts": [running, entry], "next_cursor": "abc"})

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url=_CONFIG.url)
    page = Runs(HttpTransport(_CONFIG, http_client=client)).task_history(
        "dev", "nightly", "silver.clean", repository="analytics", limit=2, cursor="c0"
    )
    (request,) = seen
    assert request.url.path == "/api/v1/environments/dev/jobs/nightly/tasks/silver.clean/attempts"
    assert dict(request.url.params) == {"repository": "analytics", "limit": "2", "cursor": "c0"}
    assert page.next_cursor == "abc"
    newest, older = page.items
    assert (newest.status, newest.finished_at, newest.duration_seconds) == ("running", None, 12.0)
    assert (older.run_id, older.attempt, older.failure_code) == (_RUN["id"], 2, "out_of_memory")
    assert (older.peak_memory_bytes, older.cpu_seconds, older.bytes_read) == (
        432013312,
        3.1,
        None,
    )
    assert older.started_at.isoformat() == "2026-11-01T12:00:00+00:00"
