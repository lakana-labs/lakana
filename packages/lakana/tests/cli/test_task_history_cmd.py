"""`lakana task history`."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from conftest import FakeApi, invoke, make_state
from test_jobs_cmd import _plain

_URL = "/api/v1/environments/dev/jobs/nightly/tasks/orders/attempts"
_RUN = "11111111-1111-4111-8111-111111111111"


def _entry(attempt: int, **changes: Any) -> dict[str, Any]:  # ruff: ignore[any-type]
    """Build a `TaskHistoryEntryOut` body.

    Parameters
    ----------
    attempt
        The attempt number.
    **changes
        Fields to change.

    Returns
    -------
    dict
        The body.
    """
    body: dict[str, Any] = {
        "run_id": _RUN,
        "attempt": attempt,
        "status": "succeeded",
        "started_at": "2026-11-01T12:00:00+00:00",
        "finished_at": "2026-11-01T12:00:12+00:00",
        "duration_seconds": 12.0,
        "peak_memory_bytes": 432013312,
        "cpu_seconds": 3.1,
        "bytes_read": 1288490188,
        "failure_code": None,
    }
    return {**body, **changes}


def _api(*entries: dict[str, Any]) -> FakeApi:
    """Answer the history route with `entries`.

    Parameters
    ----------
    *entries
        The attempts.

    Returns
    -------
    FakeApi
        The fake API.
    """
    api = FakeApi()
    api.on("GET", _URL, json={"attempts": list(entries), "next_cursor": None})
    return api


def test_task_history_prints_a_row_per_attempt(tmp_path: Path) -> None:
    """Header, a failed row with its failure code, a not-measured row with a dash."""
    api = _api(
        _entry(3),
        _entry(2, status="failed", failure_code="memory", bytes_read=None),
        _entry(
            1,
            peak_memory_bytes=None,
            cpu_seconds=None,
            bytes_read=None,
            finished_at=None,
            duration_seconds=None,
            status="running",
        ),
    )
    result = invoke(
        make_state(tmp_path, api), "task", "history", "nightly", "orders", "--env", "dev"
    )
    assert result.exit_code == 0, result.output
    out = _plain(result.stdout)
    for header in ("run", "attempt", "status", "duration", "resources"):
        assert header in out.lower()
    assert f"{_RUN} 3 succeeded 12.0s 412 MiB · 3.1 s CPU · 1.2 GiB read" in out
    assert f"{_RUN} 2 failed (memory) 12.0s 412 MiB · 3.1 s CPU" in out
    assert f"{_RUN} 1 running —" in out


def test_task_history_default_is_20_and_last_and_repo_are_passed(tmp_path: Path) -> None:
    """No `--last` asks for 20; `--last 5 --repo r` asks for 5 of that repository."""
    api = _api(_entry(1))
    invoke(make_state(tmp_path, api), "task", "history", "nightly", "orders", "--env", "dev")
    assert api.requests[0].url.params["limit"] == "20"
    invoke(
        make_state(tmp_path, api),
        *("task", "history", "nightly", "orders", "--env", "dev", "--last", "5", "--repo", "r"),
    )
    assert dict(api.requests[1].url.params) == {"limit": "5", "repository": "r"}


def test_task_history_refuses_last_out_of_range(tmp_path: Path) -> None:
    """Exit 2 and the range in the message, before any request."""
    for last in ("0", "201"):
        api = _api()
        result = invoke(
            make_state(tmp_path, api),
            *("task", "history", "nightly", "orders", "--env", "dev", "--last", last),
        )
        assert result.exit_code == 2
        assert "--last must be between 1 and 200" in _plain(result.stderr)
        assert api.requests == []


def test_task_history_json_keeps_the_cursor_so_a_script_can_page(tmp_path: Path) -> None:
    """`--output json` prints the entries and `next_cursor`; `--cursor` is sent back."""
    api = FakeApi()
    api.on("GET", _URL, json={"attempts": [_entry(1)], "next_cursor": "abc"})
    result = invoke(
        make_state(tmp_path, api),
        *("--output", "json", "task", "history", "nightly", "orders", "--env", "dev"),
    )
    body = json.loads(result.stdout)
    assert body["next_cursor"] == "abc"
    (entry,) = body["attempts"]
    assert (entry["attempt"], entry["peak_memory_bytes"]) == (1, 432013312)
    invoke(
        make_state(tmp_path, api),
        *("task", "history", "nightly", "orders", "--env", "dev", "--cursor", "abc"),
    )
    assert api.requests[1].url.params["cursor"] == "abc"


def test_task_history_points_at_last_below_200_and_at_the_cursor_at_200(tmp_path: Path) -> None:
    """More attempts exist: a larger `--last` helps below 200, only `--cursor` pages at 200."""
    api = FakeApi()
    api.on("GET", _URL, json={"attempts": [_entry(1)], "next_cursor": "abc"})
    below = _plain(
        invoke(
            make_state(tmp_path, api), "task", "history", "nightly", "orders", "--env", "dev"
        ).stdout
    )
    assert "Older attempts exist" in below
    assert "larger --last" in below
    at_200 = _plain(
        invoke(
            make_state(tmp_path, api),
            *("task", "history", "nightly", "orders", "--env", "dev", "--last", "200"),
        ).stdout
    )
    assert "--cursor abc" in at_200
    assert "larger --last" not in at_200


def test_task_history_of_a_task_that_never_ran_says_so(tmp_path: Path) -> None:
    """An empty list is explained, with the next step."""
    api = _api()
    result = invoke(
        make_state(tmp_path, api), "task", "history", "nightly", "orders", "--env", "dev"
    )
    assert result.exit_code == 0, result.output
    assert "No attempts yet: the task has not run." in _plain(result.stdout)
    assert "lakana job run nightly" in _plain(result.stdout)
    assert "(none)" not in result.stdout
