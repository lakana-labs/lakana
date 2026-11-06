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
