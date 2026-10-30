"""`lakana run`, `lakana runs …` and `lakana scheduler status`."""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from conftest import FakeApi, invoke, make_state
from test_checks_cmd import result_body

import lakana.cli.runs as runs_module

_ID = "00000000-0000-0000-0000-0000000000cc"
_CI = {"GITHUB_ACTIONS": "true"}
_RUN_PATH = f"/api/v1/environments/dev/runs/{_ID}"


def _plain(text: str) -> str:
    """Return CLI output without ANSI codes or Rich box borders, whitespace collapsed.

    Parameters
    ----------
    text
        Raw stdout or stderr.

    Returns
    -------
    str
        The plain, single-spaced text.
    """
    plain = re.sub(r"\x1b\[[0-9;]*m", "", text)
    for border in "│╭╮╰╯─":
        plain = plain.replace(border, " ")
    return " ".join(plain.split())


def _task(asset: str, status: str, attempt: int = 1) -> dict[str, object]:
    """Build a `TaskRunOut` body.

    Parameters
    ----------
    asset
        Dotted name.
    status
        Status.
    attempt
        Attempt.

    Returns
    -------
    dict
        The body.
    """
    return {
        "asset": asset,
        "status": status,
        "attempt": attempt,
        "max_attempts": 3,
        "timeout_seconds": 900,
        "next_attempt_at": None,
        "started_at": "2026-10-03T12:00:00+00:00",
        "finished_at": "2026-10-03T12:00:02+00:00" if status in {"succeeded", "failed"} else None,
        "error_message": "boom" if status == "failed" else None,
        "query_run_id": None,
        "rows_written": 2 if status == "succeeded" else None,
    }


def _run(status: str, *tasks: dict[str, object]) -> dict[str, object]:
    """Build a `RunOut` body.

    Parameters
    ----------
    status
        Run status.
    *tasks
        Its tasks.

    Returns
    -------
    dict
        The body.
    """
    return {
        "id": _ID,
        "environment": "dev",
        "repository": "analytics",
        "project_version_id": "00000000-0000-0000-0000-0000000000bb",
        "commit_sha": "a" * 40,
        "kind": "full",
        "status": status,
        "cancel_requested": False,
        "triggered_by": "admin",
        "run_as": "admin",
        "resumed_from_run_id": None,
        "selection": {"patterns": [], "upstream": False, "downstream": False},
        "error_message": None,
        "created_at": "2026-10-03T12:00:00+00:00",
        "started_at": None,
        "finished_at": None,
        "tasks": list(tasks),
    }


def _sequence(api: FakeApi, method: str, path: str, bodies: list[dict[str, object]]) -> None:
    """Answer successive requests with successive bodies (the last one repeats).

    Parameters
    ----------
    api
        The fake.
    method
        HTTP method.
    path
        Path.
    bodies
        Bodies in order.
    """
    pending: Iterator[dict[str, object]] = iter(bodies)
    last: dict[str, object] = bodies[-1]

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal last
        last = next(pending, last)
        return httpx.Response(200, json=last)

    api.routes[method, path] = respond


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make polling instant.

    Parameters
    ----------
    monkeypatch
        Pytest's monkeypatch.
    """
    monkeypatch.setattr("lakana.resources.runs.time.sleep", lambda _: None)
    monkeypatch.setattr(runs_module.time, "sleep", lambda _: None)


def test_run_prints_id_progress_and_summary_and_exits_0(tmp_path: Path) -> None:
    """Id first, then a line per task status change, then the summary."""
    api = FakeApi()
    api.on(
        "POST",
        "/api/v1/environments/dev/runs",
        status=202,
        json=_run("queued", _task("a.b", "pending", 0)),
    )
    _sequence(
        api,
        "GET",
        _RUN_PATH,
        [
            _run("running", _task("a.b", "running")),
            _run("running", _task("a.b", "running")),
            _run("succeeded", _task("a.b", "succeeded")),
        ],
    )
    result = invoke(make_state(tmp_path, api), "run", "analytics", "--env", "dev", env=_CI)
    assert result.exit_code == 0, result.output
    assert result.stderr.splitlines()[0] == _ID
    assert _ID not in result.stdout.splitlines()[0]
    out = _plain(result.stdout)
    assert out.count("a.b running") == 1
    assert "a.b succeeded attempt 1/3 2.0s" in out
    assert json.loads(api.requests[0].content) == {
        "repository": "analytics",
        "select": [],
        "upstream": False,
        "downstream": False,
    }


def test_run_failure_exits_1_and_shows_the_error(tmp_path: Path) -> None:
    """A failed run exits 1 with the failed task's error in the summary."""
    api = FakeApi()
    api.on("POST", "/api/v1/environments/dev/runs", status=202, json=_run("queued"))
    api.on(
        "GET",
        _RUN_PATH,
        json=_run("failed", _task("a.b", "failed"), _task("a.c", "upstream_failed", 0)),
    )
    result = invoke(make_state(tmp_path, api), "run", "analytics", "--env", "dev", env=_CI)
    assert result.exit_code == 1
    out = _plain(result.stdout)
    assert "boom" in out
    assert "upstream_failed" in out


def test_cancelled_run_exits_1(tmp_path: Path) -> None:
    """Cancel is a non-success: exit 1."""
    api = FakeApi()
    api.on("POST", "/api/v1/environments/dev/runs", status=202, json=_run("queued"))
    api.on("GET", _RUN_PATH, json=_run("cancelled", _task("a.b", "cancelled", 0)))
    assert invoke(make_state(tmp_path, api), "run", "analytics", "--env", "dev").exit_code == 1


def test_active_run_exits_7(tmp_path: Path) -> None:
    """7 (Conflict) when a run is already active."""
    api = FakeApi()
    api.on(
        "POST",
        "/api/v1/environments/dev/runs",
        status=409,
        json={"detail": f"Run {_ID} of 'analytics' is already active in 'dev'."},
    )
    result = invoke(make_state(tmp_path, api), "run", "analytics", "--env", "dev", env=_CI)
    assert result.exit_code == 7
    assert _ID in _plain(result.stderr)


def test_timeout_exits_1_with_the_follow_up_command(tmp_path: Path) -> None:
    """`--timeout` elapsing exits 1 and says how to keep watching."""
    api = FakeApi()
    api.on("POST", "/api/v1/environments/dev/runs", status=202, json=_run("queued"))
    api.on("GET", _RUN_PATH, json=_run("running", _task("a.b", "running")))
    result = invoke(
        make_state(tmp_path, api), "run", "analytics", "--env", "dev", "--timeout", "0", env=_CI
    )
    assert result.exit_code == 1
    assert f"lakana runs show {_ID} --env dev" in _plain(result.stderr)


def test_no_wait_prints_only_the_id_and_sends_the_selection(tmp_path: Path) -> None:
    """`--no-wait` never polls; `--select`/`--downstream` reach the body."""
    api = FakeApi()
    api.on("POST", "/api/v1/environments/dev/runs", status=202, json=_run("queued"))
    result = invoke(
        make_state(tmp_path, api),
        "run",
        "analytics",
        "--env",
        "dev",
        "--select",
        "gold.*",
        "--select",
        "silver.clean",
        "--downstream",
        "--no-wait",
    )
    assert result.stdout.strip() == _ID
    assert [r.method for r in api.requests] == ["POST"]
    body = json.loads(api.requests[0].content)
    assert (body["select"], body["downstream"]) == (["gold.*", "silver.clean"], True)


def test_runs_list_show_cancel_resume(tmp_path: Path) -> None:
    """The read and control commands."""
    api = FakeApi()
    api.on("GET", "/api/v1/environments/dev/runs", json=[_run("succeeded")])
    api.on("GET", _RUN_PATH, json=_run("failed", _task("a.b", "failed")))
    api.on("POST", f"{_RUN_PATH}/cancel", json={**_run("running"), "cancel_requested": True})
    api.on("POST", f"{_RUN_PATH}/resume", status=202, json={**_run("queued"), "kind": "resume"})
    state = make_state(tmp_path, api)
    assert _ID in _plain(
        invoke(state, "runs", "list", "--env", "dev", "--repo", "analytics", env=_CI).stdout
    )
    assert api.requests[-1].url.params["repository"] == "analytics"
    shown = _plain(invoke(state, "runs", "show", _ID, "--env", "dev", env=_CI).stdout)
    assert "a.b" in shown and "boom" in shown
    assert "Cancel requested" in _plain(invoke(state, "runs", "cancel", _ID, "--env", "dev").stdout)
    assert _ID in invoke(state, "runs", "resume", _ID, "--env", "dev").stdout


def _log_body(
    text: str, next_offset: int, *, more: bool = False, complete: bool = False, reset: bool = False
) -> dict[str, object]:
    """Build a `TaskLogOut` body.

    Parameters
    ----------
    text
        The text.
    next_offset
        Where it ends.
    more
        Whether stored bytes remain.
    complete
        Whether the task is finished and nothing more comes.
    reset
        Whether the log shrank.

    Returns
    -------
    dict
        The body.
    """
    return {
        "run_id": _ID,
        "asset": "a.b",
        "text": text,
        "attempts": [],
        "next_offset": next_offset,
        "more": more,
        "complete": complete,
        "reset": reset,
    }


def _serve_log(api: FakeApi, stored: list[bytes], *, chunk: int = 1 << 20) -> list[int]:
    """Serve ``a.b``'s log from `stored` like the server: from `offset`, at most `chunk` bytes.

    Parameters
    ----------
    api
        The fake.
    stored
        The log's bytes; the test appends to it between polls (its only element).
    chunk
        The most bytes one response carries.

    Returns
    -------
    list of int
        The byte count of each response's text, in order.
    """
    sent: list[int] = []

    def respond(request: httpx.Request) -> httpx.Response:
        data = stored[0]
        start = int(request.url.params.get("offset", 0))
        end = min(len(data), start + chunk)
        text = data[start:end]
        sent.append(len(text))
        return httpx.Response(
            200, json=_log_body(text.decode(), end, more=end < len(data), reset=start > len(data))
        )

    api.routes["GET", f"{_RUN_PATH}/tasks/a.b/log"] = respond
    return sent


def test_runs_logs_for_one_task(tmp_path: Path) -> None:
    """`--task` prints exactly that task's log, without a header, and sends no offset."""
    api = FakeApi()
    api.on("GET", _RUN_PATH, json=_run("succeeded", _task("a.b", "succeeded")))
    api.on("GET", f"{_RUN_PATH}/tasks/a.b/log", json=_log_body("line 1\n", 7))
    single = invoke(make_state(tmp_path, api), "runs", "logs", _ID, "--env", "dev", "--task", "a.b")
    assert single.stdout == "line 1\n"
    assert "offset" not in api.requests[-1].url.params


def test_runs_logs_follow_fetches_each_byte_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--follow` asks from the offset it reached: each line travels once, then it exits."""
    api = FakeApi()
    stored = [b"line 1\n"]
    sent = _serve_log(api, stored)
    appended = [b"line 1\nline 2 \xc3\xa4\n", b"line 1\nline 2 \xc3\xa4\nline 3\n"]
    _sequence_run(api, iter(["running", "running", "succeeded"]))

    def sleep(_seconds: float) -> None:
        stored[0] = appended.pop(0) if appended else stored[0]

    monkeypatch.setattr(runs_module.time, "sleep", sleep)
    followed = invoke(make_state(tmp_path, api), "runs", "logs", _ID, "--env", "dev", "--follow")
    assert followed.exit_code == 0, followed.output
    assert followed.stdout == "== a.b ==\nline 1\nline 2 \u00e4\nline 3\n"
    offsets = [r.url.params["offset"] for r in api.requests if r.url.path.endswith("/log")]
    assert offsets == ["0", "7", "17"]
    assert sum(sent) == len(stored[0])


def _sequence_run(api: FakeApi, states: Iterator[str]) -> None:
    """Answer the run request with one of `states` per request (the last repeats).

    Parameters
    ----------
    api
        The fake.
    states
        Run statuses in order.
    """
    last = ["running"]

    def respond(request: httpx.Request) -> httpx.Response:
        last[0] = next(states, last[0])
        done = last[0] == "succeeded"
        return httpx.Response(
            200, json=_run(last[0], _task("a.b", "succeeded" if done else "running"))
        )

    api.routes["GET", _RUN_PATH] = respond


def test_runs_logs_follow_continues_while_the_server_cut_the_chunk(tmp_path: Path) -> None:
    """A capped response is followed at once by the next chunk, not after a poll."""
    api = FakeApi()
    stored = [b"abcdefghij"]
    sent = _serve_log(api, stored, chunk=4)
    _sequence_run(api, iter(["succeeded"]))
    followed = invoke(make_state(tmp_path, api), "runs", "logs", _ID, "--env", "dev", "--follow")
    assert followed.stdout == "== a.b ==\nabcdefghij"
    assert sent == [4, 4, 2]


def test_runs_logs_follow_stops_when_the_task_is_complete(tmp_path: Path) -> None:
    """The log reports `complete`: no more requests, even though the run is still going."""
    api = FakeApi()
    api.routes["GET", _RUN_PATH] = lambda request: httpx.Response(
        200, json=_run("running", _task("a.b", "succeeded"))
    )
    api.on("GET", f"{_RUN_PATH}/tasks/a.b/log", json=_log_body("done\n", 5, complete=True))
    followed = invoke(
        make_state(tmp_path, api), "runs", "logs", _ID, "--env", "dev", "--task", "a.b", "--follow"
    )
    assert followed.exit_code == 0, followed.output
    assert followed.stdout == "done\n"
    assert [r.url.path.endswith("/log") for r in api.requests].count(True) == 1


def test_a_followed_log_that_is_complete_is_not_fetched_again(tmp_path: Path) -> None:
    """Two tasks: the one whose log is complete is skipped on the later polls."""
    api = FakeApi()
    runs = iter(["running", "running", "succeeded"])
    last = ["running"]

    def run_response(request: httpx.Request) -> httpx.Response:
        last[0] = next(runs, last[0])
        return httpx.Response(
            200, json=_run(last[0], _task("a.b", "succeeded"), _task("c.d", "running"))
        )

    api.routes["GET", _RUN_PATH] = run_response
    api.on("GET", f"{_RUN_PATH}/tasks/a.b/log", json=_log_body("done\n", 5, complete=True))
    api.on("GET", f"{_RUN_PATH}/tasks/c.d/log", json=_log_body("", 0))
    followed = invoke(make_state(tmp_path, api), "runs", "logs", _ID, "--env", "dev", "--follow")
    assert followed.exit_code == 0, followed.output
    paths = [r.url.path for r in api.requests if r.url.path.endswith("/log")]
    assert paths.count(f"{_RUN_PATH}/tasks/a.b/log") == 1
    assert paths.count(f"{_RUN_PATH}/tasks/c.d/log") == 3


def test_runs_logs_follow_starts_over_when_the_log_shrank(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`reset`: the client continues from the offset the server names, not its own."""
    api = FakeApi()
    stored = [b"0123456789"]
    _serve_log(api, stored)
    _sequence_run(api, iter(["running", "running", "succeeded"]))

    def sleep(_seconds: float) -> None:
        stored[0] = b"ab"

    monkeypatch.setattr(runs_module.time, "sleep", sleep)
    followed = invoke(make_state(tmp_path, api), "runs", "logs", _ID, "--env", "dev", "--follow")
    assert followed.stdout == "== a.b ==\n0123456789"
    offsets = [r.url.params["offset"] for r in api.requests if r.url.path.endswith("/log")]
    assert offsets == ["0", "10", "2"]


def test_json_output_of_run_show_and_resume(tmp_path: Path) -> None:
    """JSON mode prints bodies only: no id line, no progress, no tables."""
    api = FakeApi()
    api.on("POST", "/api/v1/environments/dev/runs", status=202, json=_run("queued"))
    api.on("GET", _RUN_PATH, json=_run("succeeded", _task("a.b", "succeeded")))
    api.on("POST", f"{_RUN_PATH}/resume", status=202, json=_run("queued"))
    state = make_state(tmp_path, api)
    waited = invoke(state, "--output", "json", "run", "analytics", "--env", "dev")
    assert json.loads(waited.stdout)["status"] == "succeeded"
    queued = invoke(state, "--output", "json", "run", "analytics", "--env", "dev", "--no-wait")
    assert json.loads(queued.stdout)["status"] == "queued"
    shown = invoke(state, "--output", "json", "runs", "show", _ID, "--env", "dev")
    assert json.loads(shown.stdout)["tasks"][0]["asset"] == "a.b"
    resumed = invoke(state, "--output", "json", "runs", "resume", _ID, "--env", "dev")
    assert json.loads(resumed.stdout)["id"] == _ID


def test_scheduler_status(tmp_path: Path) -> None:
    """`lakana scheduler status` shows the holder and counts."""
    api = FakeApi()
    api.on(
        "GET",
        "/api/v1/scheduler",
        json={
            "holder": "host:1:abc",
            "heartbeat_at": "2026-10-03T12:00:00+00:00",
            "active_runs": 1,
            "pending": 2,
            "ready": 0,
            "running": 1,
        },
    )
    out = _plain(invoke(make_state(tmp_path, api), "scheduler", "status", env=_CI).stdout)
    assert "host:1:abc" in out


def _interrupt(_seconds: float) -> None:
    """Simulate Ctrl-C during a polling sleep.

    Parameters
    ----------
    _seconds
        Ignored.

    Raises
    ------
    KeyboardInterrupt
        Always.
    """
    raise KeyboardInterrupt


def test_ctrl_c_while_waiting_exits_130_with_the_follow_up_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Interrupting `lakana run` exits 130, names `lakana runs show`, shows no traceback."""
    api = FakeApi()
    api.on("POST", "/api/v1/environments/dev/runs", status=202, json=_run("queued"))
    api.on("GET", _RUN_PATH, json=_run("running", _task("a.b", "running")))
    monkeypatch.setattr("lakana.resources.runs.time.sleep", _interrupt)
    result = invoke(make_state(tmp_path, api), "run", "analytics", "--env", "dev", env=_CI)
    assert result.exit_code == 130
    assert f"lakana runs show {_ID} --env dev" in _plain(result.stderr)
    assert "Traceback" not in result.output


def test_ctrl_c_while_following_logs_exits_130(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Interrupting `lakana runs logs --follow` exits 130 without a traceback."""
    api = FakeApi()
    api.on("GET", _RUN_PATH, json=_run("running"))
    monkeypatch.setattr(runs_module.time, "sleep", _interrupt)
    result = invoke(make_state(tmp_path, api), "runs", "logs", _ID, "--env", "dev", "--follow")
    assert result.exit_code == 130
    assert "Traceback" not in result.output
    assert "Aborted" not in result.output


def test_run_takes_the_repository_from_lakana_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without REPO, `lakana.yaml`'s `repo:` flows through `lakana run`."""
    project = tmp_path / "proj"
    project.mkdir()
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    (project / "lakana.yaml").write_text("repo: analytics\n")
    monkeypatch.chdir(project)
    api = FakeApi()
    api.on("POST", "/api/v1/environments/dev/runs", status=202, json=_run("queued"))
    result = invoke(make_state(tmp_path, api), "run", "--env", "dev", "--no-wait")
    assert result.exit_code == 0, result.output
    assert json.loads(api.requests[0].content)["repository"] == "analytics"


def test_runs_list_limit_is_sent_and_bounded(tmp_path: Path) -> None:
    """`--limit N` reaches the server; without it the server's default applies."""
    api = FakeApi()
    api.on("GET", "/api/v1/environments/dev/runs", json=[_run("succeeded")])
    state = make_state(tmp_path, api)
    assert invoke(state, "runs", "list", "--env", "dev", "--limit", "5", env=_CI).exit_code == 0
    assert api.requests[-1].url.params["limit"] == "5"
    assert invoke(state, "runs", "list", "--env", "dev", env=_CI).exit_code == 0
    assert "limit" not in api.requests[-1].url.params
    sent = len(api.requests)
    for bad in ("0", "201"):
        refused = invoke(state, "runs", "list", "--env", "dev", "--limit", bad, env=_CI)
        assert refused.exit_code == 2
    assert len(api.requests) == sent


def _many_failed(count: int) -> dict[str, object]:
    """Build a failed run whose every task failed with its own error.

    Parameters
    ----------
    count
        Number of tasks.

    Returns
    -------
    dict
        The `RunOut` body.
    """
    tasks = [
        {**_task(f"a.t{i:03d}", "failed"), "error_message": f"boom-{i:03d}"} for i in range(count)
    ]
    return _run("failed", *tasks)


def test_a_run_summary_shows_at_most_fifty_errors(tmp_path: Path) -> None:
    """The table keeps every task, but only 50 error texts, then `+N more`."""
    api = FakeApi()
    api.on("GET", _RUN_PATH, json=_many_failed(53))
    state = make_state(tmp_path, api)
    shown = _plain(invoke(state, "runs", "show", _ID, "--env", "dev", env=_CI).stdout)
    assert "boom-049" in shown and "boom-050" not in shown
    assert "a.t052" in shown
    assert "+3 more failed tasks" in shown
    api.on("GET", _RUN_PATH, json=_many_failed(50))
    exact = _plain(invoke(state, "runs", "show", _ID, "--env", "dev", env=_CI).stdout)
    assert "boom-049" in exact and "more failed tasks" not in exact


def test_the_note_of_a_succeeded_task_is_not_called_an_error_and_is_not_counted(
    tmp_path: Path,
) -> None:
    """A task found succeeded after its worker was lost shows its message as a note."""
    note = "The worker was lost after this task's write had been committed."
    tasks = [{**_task("a.ok", "succeeded"), "error_message": note}]
    tasks += [
        {**_task(f"a.t{i:03d}", "failed"), "error_message": f"boom-{i:03d}"} for i in range(50)
    ]
    api = FakeApi()
    api.on("GET", _RUN_PATH, json=_run("failed", *tasks))
    shown = _plain(
        invoke(make_state(tmp_path, api), "runs", "show", _ID, "--env", "dev", env=_CI).stdout
    )
    assert "note: The worker was lost" in shown
    assert "boom-049" in shown  # the note did not use one of the 50 places
    assert "more failed tasks" not in shown


def test_json_output_of_a_run_keeps_every_error(tmp_path: Path) -> None:
    """The cap is for the human summary only."""
    api = FakeApi()
    api.on("GET", _RUN_PATH, json=_many_failed(53))
    state = make_state(tmp_path, api)
    shown = json.loads(
        invoke(state, "--output", "json", "runs", "show", _ID, "--env", "dev").stdout
    )
    assert [t["error_message"] for t in shown["tasks"]][-1] == "boom-052"


def test_progress_lines_say_plainly_what_each_state_means(tmp_path: Path) -> None:
    """No attempt count before a task starts; a retry says so; running and finished keep it."""
    api = FakeApi()
    api.on(
        "POST",
        "/api/v1/environments/dev/runs",
        status=202,
        json=_run("queued", _task("a.b", "pending", 0)),
    )
    waiting = {**_task("a.b", "pending", 1), "next_attempt_at": "2026-10-03T12:00:30+00:00"}
    _sequence(
        api,
        "GET",
        _RUN_PATH,
        [
            _run("queued", _task("a.b", "pending", 0)),
            _run("running", _task("a.b", "running", 1)),
            _run("running", waiting),
            _run("running", _task("a.b", "running", 2)),
            _run("succeeded", _task("a.b", "succeeded", 2)),
        ],
    )
    result = invoke(make_state(tmp_path, api), "run", "analytics", "--env", "dev", env=_CI)
    assert result.exit_code == 0, result.output
    lines = [" ".join(line.split()) for line in result.stdout.splitlines()]
    progress = [line for line in lines if line.startswith("a.b ")]
    assert progress[:5] == [
        "a.b pending",
        "a.b running attempt 1/3",
        "a.b pending retrying after attempt 1/3",
        "a.b running attempt 2/3",
        "a.b succeeded attempt 2/3 2.0s",
    ]
    assert not any("0/3" in line for line in lines)


def test_summary_table_leaves_the_attempt_blank_for_tasks_that_never_started(
    tmp_path: Path,
) -> None:
    """`upstream_failed` never ran: the attempt column is empty, not `0/3`."""
    api = FakeApi()
    api.on(
        "GET",
        _RUN_PATH,
        json=_run("failed", _task("a.b", "failed", 3), _task("a.c", "upstream_failed", 0)),
    )
    result = invoke(make_state(tmp_path, api), "runs", "show", _ID, "--env", "dev")
    out = _plain(result.stdout)
    assert "0/3" not in out
    assert "3/3" in out


def test_json_output_keeps_every_attempt_number(tmp_path: Path) -> None:
    """Machine-readable output reports the raw numbers, including 0."""
    api = FakeApi()
    api.on("GET", _RUN_PATH, json=_run("queued", _task("a.b", "pending", 0)))
    result = invoke(
        make_state(tmp_path, api), "--output", "json", "runs", "show", _ID, "--env", "dev"
    )
    task = json.loads(result.stdout)["tasks"][0]
    assert (task["attempt"], task["max_attempts"]) == (0, 3)


_LONG_ERROR = (
    "TypeError: unsupported operand type(s) for *: 'float' and 'decimal.Decimal'\n"
    "This error is almost always a mistake in the asset's code, so it "
    "is not retried. Fix the function, deploy, and run again."
)


def _failed_with_a_long_error() -> dict[str, object]:
    """Build a failed run whose only task has a long, two-line error.

    Returns
    -------
    dict
        The `RunOut` body.
    """
    return _run("failed", {**_task("gold.scores", "failed"), "error_message": _LONG_ERROR})


@pytest.mark.parametrize("columns", ["80", "120"])
def test_run_and_runs_show_print_a_long_task_error_in_full(tmp_path: Path, columns: str) -> None:
    """The run view used to cut the error at 80 columns; it folds it instead."""
    api = FakeApi()
    api.on("POST", "/api/v1/environments/dev/runs", status=202, json=_run("queued"))
    api.on("GET", _RUN_PATH, json=_failed_with_a_long_error())
    state = make_state(tmp_path, api)
    env = {**_CI, "COLUMNS": columns}
    expected = " ".join(_LONG_ERROR.split())
    first, second = (sentence.split()[0] for sentence in _LONG_ERROR.split("\n"))
    ran = invoke(state, "run", "analytics", "--env", "dev", env=env)
    assert ran.exit_code == 1
    assert expected in _plain(ran.stdout)
    shown = invoke(state, "runs", "show", _ID, "--env", "dev", env=env)
    assert expected in _plain(shown.stdout)
    assert "…" not in ran.stdout + shown.stdout
    for out in (ran.stdout, shown.stdout):
        assert "\\x0a" not in out  # a line break is a line break, not an escape
        assert not any(first in line and second in line for line in out.splitlines())
        assert any(second in line for line in out.splitlines())


# -- `lakana worker list` -------------------------------------

_WORKERS = "/api/v1/workers"
_Q1 = "11111111-1111-4111-8111-111111111111"
_D1 = "22222222-2222-4222-8222-222222222222"
_T1 = "33333333-3333-4333-8333-333333333333"
_WNOW = datetime(2026, 10, 9, 12, 0, 0, tzinfo=UTC)


def _worker(worker_id: str = "box:41:ab12", **more: object) -> dict[str, object]:
    """Build a `WorkerOut` row.

    Parameters
    ----------
    worker_id
        The worker's id.
    **more
        Fields that differ from an idle, alive worker whose last beat was 5 seconds ago.

    Returns
    -------
    dict
        The row.
    """
    return {
        "id": worker_id,
        "host": "box",
        "version": "0.9.0",
        "started_at": (_WNOW - timedelta(hours=2)).isoformat(),
        "heartbeat_at": (_WNOW - timedelta(seconds=5)).isoformat(),
        "status": "alive",
        "takes": "all",
        "query_run_id": None,
        "deploy_run_id": None,
        "task_run_id": None,
        "caches": None,
        **more,
    }


@pytest.fixture
def _worker_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the clock of the `last_seen` column stand still.

    Parameters
    ----------
    monkeypatch
        Pytest's monkeypatch.
    """
    monkeypatch.setattr("lakana.cli.runs._now", lambda: _WNOW)


@pytest.mark.usefixtures("_worker_clock")
def test_worker_list_shows_alive_and_lost_workers(tmp_path: Path) -> None:
    api = FakeApi()
    api.on(
        "GET",
        _WORKERS,
        json=[
            _worker("a:1:x"),
            _worker(
                "b:2:y",
                status="lost",
                takes="pipeline",
                heartbeat_at=(_WNOW - timedelta(minutes=7)).isoformat(),
                query_run_id=_Q1,
            ),
        ],
    )
    result = invoke(make_state(tmp_path, api), "worker", "list", env={"COLUMNS": "300"})
    assert result.exit_code == 0, result.stderr
    lines = [line for line in result.stdout.splitlines() if line.startswith("│")]
    assert re.search(
        r"a:1:x\s+│ box\s+│ 0.9.0\s+│.*│ 5s ago\s+│ alive\s+│ all\s+│ idle\s+│", lines[0]
    )
    assert re.search(r"b:2:y\s+│.*│ 7m ago\s+│ lost\s+│ pipeline\s+│ query 11111111\s+│", lines[1])
    assert len(lines) == 2


@pytest.mark.usefixtures("_worker_clock")
def test_worker_list_json_keeps_the_entries_as_they_are(tmp_path: Path) -> None:
    rows = [_worker(task_run_id=_T1)]
    api = FakeApi()
    api.on("GET", _WORKERS, json=rows)
    result = invoke(make_state(tmp_path, api), "--output", "json", "worker", "list")
    assert [set(row) for row in json.loads(result.stdout)] == [set(rows[0])]
    assert json.loads(result.stdout)[0]["takes"] == "all"


@pytest.mark.usefixtures("_worker_clock")
def test_takes_stays_at_80_columns(tmp_path: Path) -> None:
    api = FakeApi()
    api.on(
        "GET",
        _WORKERS,
        json=[_worker("analytics-box:41233:3f2c9d1e", host="analytics-box", takes="interactive")],
    )
    result = invoke(make_state(tmp_path, api), "worker", "list", env={"COLUMNS": "80"})
    assert result.exit_code == 0, result.stderr
    assert "takes" in result.stdout.split("Not shown")[0]
    assert "interactive" in result.stdout
    (hint,) = [line for line in result.stdout.splitlines() if line.startswith("Not shown")]
    assert "started_at, version, host" in hint
    assert "takes" not in hint


@pytest.mark.usefixtures("_worker_clock")
def test_an_entry_without_takes_or_with_a_malformed_one_is_shown_as_all(tmp_path: Path) -> None:
    older = _worker("old:1:x")
    del older["takes"]
    api = FakeApi()
    api.on("GET", _WORKERS, json=[older, _worker("bad:2:y", takes=7)])
    result = invoke(make_state(tmp_path, api), "worker", "list", env={"COLUMNS": "300"})
    assert result.exit_code == 0, result.stderr
    lines = [line for line in result.stdout.splitlines() if line.startswith("│")]
    assert len(lines) == 2
    assert all(re.search(r"│ alive\s+│ all\s+│ idle\s+│", line) for line in lines)


@pytest.mark.usefixtures("_worker_clock")
def test_doing_folds_the_three_ids(tmp_path: Path) -> None:
    api = FakeApi()
    api.on(
        "GET",
        _WORKERS,
        json=[
            _worker("w1", query_run_id=_Q1),
            _worker("w2", deploy_run_id=_D1),
            _worker("w3", task_run_id=_T1),
            _worker("w4", query_run_id=_Q1, task_run_id=_T1),
            _worker("w5", query_run_id=_Q1, deploy_run_id=_D1, task_run_id=_T1),
        ],
    )
    result = invoke(make_state(tmp_path, api), "worker", "list", env={"COLUMNS": "400"})
    rows = [line for line in result.stdout.splitlines() if line.startswith("│")]
    doing = [re.split(r"\s+│\s*", row.strip("│ "))[-2] for row in rows]
    assert doing == [
        "query 11111111",
        "deploy 22222222",
        "task 33333333",
        "query 11111111, task 33333333",
        "query 11111111, deploy 22222222, task 33333333",
    ]
    assert "doing shows 8 characters of each run id; --output json has them whole." in result.stdout
    whole = invoke(make_state(tmp_path, api), "--output", "json", "worker", "list")
    assert json.loads(whole.stdout)[4]["deploy_run_id"] == _D1


def test_no_worker_says_what_to_do(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("GET", _WORKERS, json=[])
    state = make_state(tmp_path, api)
    result = invoke(state, "worker", "list")
    assert result.exit_code == 0, result.stderr
    assert result.stdout.strip() == (
        "No worker is registered. Start one; on a local instance, start it again with "
        "'lakana server start --local'."
    )
    assert json.loads(invoke(state, "--output", "json", "worker", "list").stdout) == []


@pytest.mark.usefixtures("_worker_clock")
def test_a_worker_text_cannot_inject_terminal_controls(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("GET", _WORKERS, json=[_worker("w\x1b[2J", host="h\x07ost", takes="x\x1b[31m\x07y")])
    result = invoke(make_state(tmp_path, api), "worker", "list", env={"COLUMNS": "300"})
    assert "\x1b" not in result.stdout
    assert "\x07" not in result.stdout


@pytest.mark.usefixtures("_worker_clock")
def test_worker_list_shows_the_size_of_the_caches(tmp_path: Path) -> None:
    measured = {
        "environments_bytes": 1024**3,
        "packages_bytes": 2 * 1024**3,
        "tasks_bytes": 0,
        "complete": True,
        "measured_at": _WNOW.isoformat(),
    }
    api = FakeApi()
    api.on(
        "GET",
        _WORKERS,
        json=[
            _worker("a:1:x", caches=measured),
            _worker("b:2:y", caches={**measured, "complete": False}),
            _worker("c:3:z", caches=None),
            {key: value for key, value in _worker("d:4:w").items() if key != "caches"},
        ],
    )
    result = invoke(make_state(tmp_path, api), "worker", "list", env={"COLUMNS": "300"})
    assert result.exit_code == 0, result.stderr
    rows = [line for line in result.stdout.splitlines() if line.startswith("│")]
    caches = [re.split(r"\s+│\s*", row.strip("│ "))[-1] for row in rows]
    assert caches == ["3.0 GiB", "3.0 GiB+", "-", "-"]
    assert "lakana env usage" in result.stdout
    whole = json.loads(
        invoke(make_state(tmp_path, api), "--output", "json", "worker", "list").stdout
    )
    assert whole[0]["caches"]["packages_bytes"] == 2 * 1024**3
    assert whole[2]["caches"] is None


def test_worker_list_is_refused_with_the_servers_message(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("GET", _WORKERS, status=403, json={"detail": "Not allowed."})
    result = invoke(make_state(tmp_path, api), "worker", "list")
    assert result.exit_code != 0
    assert "Not allowed." in result.stderr


# -- The reason a task waits -----------------------------------

_BUSY_TASK = "Ready, and all 2 workers are busy. Queued queries and deploys are served first."
_NO_WORKER_TASK = (
    "No worker is running, so this task cannot start. Ask the operator to start a worker; on a "
    "local instance, start it again with 'lakana server start --local'."
)
_RETRY = "Attempt 1 of 3 failed; the next one starts at 12:00:30 UTC."


def _waits(task: dict[str, object], code: str, message: str) -> dict[str, object]:
    """Give a task body a wait reason.

    Parameters
    ----------
    task
        The `TaskRunOut` body.
    code
        The reason's code.
    message
        The reason's sentence.

    Returns
    -------
    dict
        The body with `waiting_for`.
    """
    return {**task, "waiting_for": {"code": code, "message": message}}


def _followed(tmp_path: Path, *polls: dict[str, object]) -> list[str]:
    """Run `lakana run` over the polls and return the lines it printed on standard output.

    Parameters
    ----------
    tmp_path
        Pytest's per-test directory.
    *polls
        The bodies of successive polls; the last one repeats.

    Returns
    -------
    list of str
        The lines of standard output before the summary, whitespace collapsed.
    """
    api = FakeApi()
    api.on("POST", "/api/v1/environments/dev/runs", status=202, json=_run("queued"))
    _sequence(api, "GET", _RUN_PATH, [*polls, _run("succeeded", _task("a.b", "succeeded"))])
    result = invoke(make_state(tmp_path, api), "run", "analytics", "--env", "dev", env=_CI)
    assert result.exit_code == 0, result.output
    lines = [" ".join(line.split()) for line in result.stdout.splitlines()]
    return [
        line for line in lines[: lines.index(f"Run {_ID} succeeded")] if "succeeded" not in line
    ]


@pytest.mark.parametrize(
    ("columns", "hint"),
    [("80", "Not shown: duration."), ("60", "Not shown: duration, rows.")],
)
def test_runs_show_has_a_waiting_for_column_that_is_never_dropped(
    tmp_path: Path, columns: str, hint: str
) -> None:
    api = FakeApi()
    api.on(
        "GET",
        _RUN_PATH,
        json=_run(
            "running",
            _waits(_task("a.b", "ready", 0), "workers_busy", _BUSY_TASK),
            _task("a.c", "succeeded"),
        ),
    )

    result = invoke(
        make_state(tmp_path, api), "runs", "show", _ID, "--env", "dev", env={"COLUMNS": columns}
    )

    assert result.exit_code == 0, result.stderr
    assert _BUSY_TASK in _plain(result.stdout)
    assert "waiting_for" in result.stdout
    assert hint in result.stdout
    assert max(len(line) for line in result.stdout.splitlines()) <= int(columns)


def test_runs_show_says_a_running_task_is_setting_the_table_back(tmp_path: Path) -> None:
    api = FakeApi()
    api.on(
        "GET",
        _RUN_PATH,
        json=_run(
            "running",
            {**_task("a.b", "running"), "setting_back": True},
            _task("a.c", "running"),
        ),
    )

    result = invoke(make_state(tmp_path, api), "runs", "show", _ID, "--env", "dev")

    assert result.exit_code == 0, result.stderr
    rows = [" ".join(line.split()) for line in result.stdout.splitlines()]
    assert [r for r in rows if "setting the table back" in r] == [
        next(r for r in rows if "a.b" in r)
    ]
    assert any("a.c" in r and "running" in r for r in rows)


def test_the_progress_of_a_run_says_a_task_is_setting_the_table_back(tmp_path: Path) -> None:
    lines = _followed(
        tmp_path,
        _run("running", _task("a.b", "running")),
        _run("running", {**_task("a.b", "running"), "setting_back": True}),
    )
    assert lines[-1].startswith("a.b setting the table back")


def test_runs_show_has_no_empty_column_when_no_task_waits(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("GET", _RUN_PATH, json=_run("running", _task("a.c", "succeeded")))

    result = invoke(make_state(tmp_path, api), "runs", "show", _ID, "--env", "dev")

    assert result.exit_code == 0, result.stderr
    assert "waiting_for" not in result.stdout
    assert "Not shown" not in result.stdout


def test_runs_show_does_not_escape_a_reason_twice_and_escapes_what_is_raw(
    tmp_path: Path,
) -> None:
    api = FakeApi()
    sent = "Waiting for upstream tasks: a\\x1b[31m, b\x1b[31m."
    api.on(
        "GET", _RUN_PATH, json=_run("running", _waits(_task("a.b", "pending", 0), "upstream", sent))
    )

    result = invoke(make_state(tmp_path, api), "runs", "show", _ID, "--env", "dev")

    assert "\x1b" not in result.stdout
    assert _plain(result.stdout).count("a\\x1b[31m, b\\x1b[31m.") == 1


def test_runs_show_json_keeps_the_object(tmp_path: Path) -> None:
    api = FakeApi()
    api.on(
        "GET",
        _RUN_PATH,
        json=_run("running", _waits(_task("a.b", "ready", 0), "workers_busy", _BUSY_TASK)),
    )

    result = invoke(
        make_state(tmp_path, api), "--output", "json", "runs", "show", _ID, "--env", "dev"
    )

    waiting = json.loads(result.stdout)["tasks"][0]["waiting_for"]
    assert (waiting["code"], waiting["message"]) == ("workers_busy", _BUSY_TASK)


def test_the_follower_appends_the_reason_to_a_line_it_prints_anyway(tmp_path: Path) -> None:
    lines = _followed(
        tmp_path,
        _run("running", _waits(_task("a.b", "ready", 0), "workers_busy", _BUSY_TASK)),
    )

    assert [line for line in lines if line.startswith("a.b")] == [f"a.b ready {_BUSY_TASK}"]


def test_the_follower_prints_a_line_of_its_own_only_for_a_reason_the_user_can_act_on(
    tmp_path: Path,
) -> None:
    ready = _task("a.b", "ready", 0)

    def poll(code: str, message: str) -> dict[str, object]:
        return _run("running", _waits(ready, code, message))

    lines = _followed(
        tmp_path,
        poll("starting", "Starts within about a second."),
        poll("starting", "Starts within about a second."),
        poll("no_worker", _NO_WORKER_TASK),
        poll("no_worker", _NO_WORKER_TASK + " "),  # the sentence changed, the code did not
        poll("workers_busy", _BUSY_TASK),
        poll("starting", "Starts within about a second."),  # silent
        poll("upstream", "Waiting for upstream tasks: x."),  # silent
        poll("retry", _RETRY),
        poll("retry", _RETRY),
        poll("workers_busy", _BUSY_TASK),
    )

    assert [line for line in lines if line.startswith("a.b")] == [
        "a.b ready",
        f"a.b ready {_NO_WORKER_TASK}",
        f"a.b ready {_BUSY_TASK}",
        f"a.b ready {_RETRY}",
        f"a.b ready {_BUSY_TASK}",
    ]


_NO_KIND_TASK = (
    "No worker that takes pipeline work is running (2 workers take interactive work only), so "
    "this task cannot start. Ask the operator to start a worker with "
    "LAKANA_WORKER__TAKES=pipeline or all."
)


def test_the_follower_prints_a_line_of_its_own_for_no_worker_of_kind(tmp_path: Path) -> None:
    ready = _task("a.b", "ready", 0)

    def poll(code: str, message: str) -> dict[str, object]:
        return _run("running", _waits(ready, code, message))

    lines = _followed(
        tmp_path,
        poll("starting", "Starts within about a second."),
        poll("no_worker_of_kind", _NO_KIND_TASK),
        poll("no_worker_of_kind", _NO_KIND_TASK + " "),  # the sentence changed, the code did not
        poll("starting", "Starts within about a second."),  # silent
        poll("no_worker_of_kind", _NO_KIND_TASK),
    )

    assert [line for line in lines if line.startswith("a.b")] == [
        "a.b ready",
        f"a.b ready {_NO_KIND_TASK}",
        f"a.b ready {_NO_KIND_TASK}",
    ]


def test_a_task_that_first_appears_with_no_worker_of_kind_carries_it_on_its_line(
    tmp_path: Path,
) -> None:
    lines = _followed(
        tmp_path,
        _run("running", _waits(_task("a.b", "ready", 0), "no_worker_of_kind", _NO_KIND_TASK)),
    )

    assert [line for line in lines if line.startswith("a.b")] == [f"a.b ready {_NO_KIND_TASK}"]


def test_the_followers_no_worker_of_kind_sentence_is_escaped_and_cut(tmp_path: Path) -> None:
    lines = _followed(
        tmp_path,
        _run("running", _waits(_task("a.b", "ready", 0), "no_worker_of_kind", "x\x1b[31m")),
        _run("running", _waits(_task("a.c", "ready", 0), "no_worker_of_kind", "n" * 5000)),
    )

    assert "a.b ready x\\x1b[31m" in lines
    assert max(len(line) for line in lines) < 600


def test_many_tasks_that_wait_for_the_same_reason_print_one_line_each_not_one_per_poll(
    tmp_path: Path,
) -> None:
    tasks = [
        _waits(_task(f"a.t{n}", "pending", 0), "upstream", "Waiting for upstream tasks: a.up.")
        for n in range(30)
    ]

    lines = _followed(tmp_path, *(_run("running", *tasks) for _ in range(4)))

    assert len([line for line in lines if line.startswith("a.t")]) == 30


def test_a_retry_line_carries_the_reason_instead_of_the_retrying_text(tmp_path: Path) -> None:
    waiting = _waits(
        {**_task("a.b", "pending", 1), "next_attempt_at": "2026-10-03T12:00:30+00:00"},
        "retry",
        _RETRY,
    )

    lines = _followed(
        tmp_path, _run("running", _task("a.b", "running", 1)), _run("running", waiting)
    )

    assert [line for line in lines if line.startswith("a.b")] == [
        "a.b running attempt 1/3",
        f"a.b pending {_RETRY}",
    ]
    assert not any("retrying after attempt" in line for line in lines)


def test_the_followers_reason_is_escaped_and_cut(tmp_path: Path) -> None:
    long = "n" * 5000
    lines = _followed(
        tmp_path,
        _run("running", _waits(_task("a.b", "pending", 0), "workers_busy", "x\x1b[31m")),
        _run("running", _waits(_task("a.c", "pending", 0), "workers_busy", long)),
    )

    assert "a.b pending x\\x1b[31m" in lines
    assert max(len(line) for line in lines) < 600


def test_the_follower_prints_nothing_in_json_mode(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("POST", "/api/v1/environments/dev/runs", status=202, json=_run("queued"))
    _sequence(
        api,
        "GET",
        _RUN_PATH,
        [
            _run("running", _waits(_task("a.b", "ready", 0), "workers_busy", _BUSY_TASK)),
            _run("succeeded", _task("a.b", "succeeded")),
        ],
    )

    result = invoke(
        make_state(tmp_path, api), "--output", "json", "run", "analytics", "--env", "dev", env=_CI
    )

    assert result.exit_code == 0, result.output
    assert _BUSY_TASK not in result.stderr
    assert json.loads(result.stdout)["status"] == "succeeded"


def test_a_healthy_run_prints_no_reason_for_the_normal_course(tmp_path: Path) -> None:
    lines = _followed(
        tmp_path,
        _run(
            "running",
            _waits(_task("a.b", "ready", 0), "starting", "Starts within about a second."),
            _waits(_task("a.c", "pending", 0), "upstream", "Waiting for upstream tasks: a.b."),
        ),
    )

    assert [line for line in lines if line.startswith("a.")] == ["a.b ready", "a.c pending"]


def test_the_follower_prints_no_line_of_its_own_for_a_reason_without_a_sentence(
    tmp_path: Path,
) -> None:
    ready = _task("a.b", "ready", 0)

    lines = _followed(
        tmp_path,
        _run("running", ready),
        _run("running", _waits(ready, "workers_busy", "")),
    )

    assert [line for line in lines if line.startswith("a.b")] == ["a.b ready"]


@pytest.mark.parametrize(
    "reason",
    [
        "a string",
        [1],
        7,
        {"code": "x"},
        {"code": "x", "message": None},
        {"code": "x", "message": 5},
    ],
)
def test_a_malformed_task_reason_is_treated_as_no_reason(tmp_path: Path, reason: object) -> None:
    api = FakeApi()
    api.on(
        "GET", _RUN_PATH, json=_run("running", {**_task("a.b", "ready", 0), "waiting_for": reason})
    )

    result = invoke(make_state(tmp_path, api), "runs", "show", _ID, "--env", "dev")

    assert result.exit_code == 0, result.stderr
    assert result.exception is None
    assert "waiting_for" not in result.stdout
    assert "a.b" in result.stdout


def test_a_long_task_reason_is_cut_in_the_table(tmp_path: Path) -> None:
    api = FakeApi()
    api.on(
        "GET",
        _RUN_PATH,
        json=_run("running", _waits(_task("a.b", "ready", 0), "workers_busy", "word " * 1000)),
    )

    result = invoke(make_state(tmp_path, api), "runs", "show", _ID, "--env", "dev")

    assert result.exit_code == 0, result.stderr
    assert "…" in result.stdout
    assert len(result.stdout) < 3000


# -- the check block of `lakana runs show` ------------------------------


def _checked(
    asset: str, *checks: dict[str, object], kind: str = "asset", kept_check: bool | None = False
) -> dict[str, object]:
    """Build a succeeded task body with check results.

    Parameters
    ----------
    asset
        The task's table.
    *checks
        Its `CheckResultOut` bodies.
    kind
        ``asset`` or ``source_check``.
    kept_check
        Whether a ``source_check`` task checks a table that was kept after a failed check.

    Returns
    -------
    dict
        The body.
    """
    return {
        **_task(asset, "succeeded"),
        "kind": kind,
        "checks": list(checks),
        "kept_check": kept_check,
    }


def _words(lines: list[str]) -> list[str]:
    """Collapse the white space of each line.

    Parameters
    ----------
    lines
        Lines of output.

    Returns
    -------
    list of str
        The lines with single spaces.
    """
    return [" ".join(line.split()) for line in lines]


def _show(tmp_path: Path, *tasks: dict[str, object]) -> list[str]:
    """Run `lakana runs show` on a run with these tasks.

    Parameters
    ----------
    tmp_path
        Pytest's per-test directory.
    *tasks
        The tasks.

    Returns
    -------
    list of str
        The check block: the lines after the task table and the blank line that sets them off.
    """
    api = FakeApi()
    api.on("GET", _RUN_PATH, json=_run("succeeded", *tasks))
    result = invoke(
        make_state(tmp_path, api), "runs", "show", _ID, "--env", "dev", env={"COLUMNS": "200"}
    )
    assert result.exit_code == 0, result.stderr
    lines = result.stdout.splitlines()
    last_border = max(i for i, line in enumerate(lines) if line.startswith("└"))
    rest = lines[last_border + 1 :]
    if rest:
        assert rest[0] == ""
    return rest[1:]


def test_runs_show_lists_the_checks_under_their_table(tmp_path: Path) -> None:
    table = "silver.station_temperatures"
    block = _show(
        tmp_path,
        _checked(
            table,
            result_body(
                "unique_reading", outcome="failed", failing_rows=3, rollback_outcome="rolled_back"
            ),
            result_body("plausible_range", outcome="warned", on_failure="warn", failing_rows=12),
            result_body("no_missing_station"),
        ),
    )
    assert block == [
        "silver.station_temperatures   FAILED   2 of 3 checks passed, rolled back",
        "  no_missing_station          passed",
        "  plausible_range             warned   12 rows",
        "  unique_reading              FAILED   3 rows",
    ]


@pytest.mark.parametrize(
    ("outcome", "phrase"),
    [
        ("rolled_back", "rolled back"),
        ("unpublished", "not published"),
        ("nothing_written", "nothing was written"),
        ("not_a_table", "a view, nothing to set back"),
        ("changed_since", "left as it is, changed since"),
        ("failed", "rollback failed"),
    ],
)
def test_runs_show_states_what_the_rollback_did(tmp_path: Path, outcome: str, phrase: str) -> None:
    block = _show(
        tmp_path,
        _checked(
            "silver.t",
            result_body(
                "c",
                table="silver.t",
                outcome="failed",
                failing_rows=1,
                rollback_outcome=outcome,
            ),
        ),
    )
    assert _words(block) == [
        f"silver.t FAILED 0 of 1 checks passed, {phrase}",
        "c FAILED 1 row",
    ]


def test_runs_show_group_states(tmp_path: Path) -> None:
    passed = _show(
        tmp_path,
        _checked(
            "silver.t", result_body("a", table="silver.t"), result_body("b", table="silver.t")
        ),
    )
    assert _words(passed) == ["silver.t passed 2 of 2 checks passed", "a passed", "b passed"]
    warned = _show(
        tmp_path,
        _checked(
            "silver.t",
            result_body("a", table="silver.t", outcome="warned", on_failure="warn", failing_rows=1),
        ),
    )
    assert _words(warned) == ["silver.t warned 1 of 1 checks passed", "a warned 1 row"]
    errored = _show(
        tmp_path,
        _checked(
            "silver.t",
            result_body(
                "a",
                table="silver.t",
                outcome="errored",
                failing_rows=None,
                error="no such\ncolumn: x",
            ),
        ),
    )
    assert _words(errored) == [
        "silver.t FAILED 0 of 1 checks passed",
        "a ERRORED no such\\ncolumn: x",
    ]
    soft = _show(
        tmp_path,
        _checked(
            "silver.t",
            result_body(
                "a",
                table="silver.t",
                outcome="errored",
                on_failure="warn",
                failing_rows=None,
                error="boom",
            ),
        ),
    )
    assert _words(soft) == ["silver.t warned 0 of 1 checks passed", "a errored boom"]
    mixed = _show(
        tmp_path,
        _checked(
            "silver.t",
            result_body("a", table="silver.t"),
            result_body(
                "b",
                table="silver.t",
                outcome="errored",
                on_failure="warn",
                failing_rows=None,
                error="boom",
            ),
        ),
    )
    assert _words(mixed) == ["silver.t warned 1 of 2 checks passed", "a passed", "b errored boom"]
    source = _show(
        tmp_path,
        _checked(
            "raw.events",
            result_body("c", table="raw.events", outcome="failed", failing_rows=2),
            kind="source_check",
        ),
    )
    assert _words(source)[0] == "raw.events FAILED 0 of 1 checks passed, not built by this project"
    kept = _show(
        tmp_path,
        _checked(
            "silver.orders",
            result_body("c", table="silver.orders", outcome="failed", failing_rows=2),
            kind="source_check",
            kept_check=True,
        ),
    )
    assert _words(kept)[0] == "silver.orders FAILED 0 of 1 checks passed, kept after a failed check"


def test_runs_show_names_a_check_task_plainly_when_the_server_cannot_tell_its_kind(
    tmp_path: Path,
) -> None:
    unknown = _show(
        tmp_path,
        _checked(
            "silver.orders",
            result_body("c", table="silver.orders", outcome="failed", failing_rows=2),
            kind="source_check",
            kept_check=None,
        ),
    )
    assert _words(unknown)[0] == "silver.orders FAILED 0 of 1 checks passed, check task"


def test_runs_show_marks_a_keep_check_as_kept(tmp_path: Path) -> None:
    failed = _show(
        tmp_path,
        _checked(
            "silver.t",
            result_body("a", table="silver.t", outcome="failed", on_failure="keep", failing_rows=2),
            result_body("b", table="silver.t"),
        ),
    )
    assert _words(failed) == [
        "silver.t FAILED 1 of 2 checks passed",
        "a failed (kept) 2 rows",
        "b passed",
    ]
    errored = _show(
        tmp_path,
        _checked(
            "silver.t",
            result_body(
                "a",
                table="silver.t",
                outcome="errored",
                on_failure="keep",
                failing_rows=None,
                error="boom",
            ),
        ),
    )
    assert _words(errored) == ["silver.t FAILED 0 of 1 checks passed", "a ERRORED (kept) boom"]


def test_runs_show_adds_the_policy_note(tmp_path: Path) -> None:
    note = (
        "checked under a row filter or column mask of admin: "
        "rows and values it cannot see were not checked"
    )
    block = _show(
        tmp_path,
        _checked(
            "silver.t",
            result_body("a", table="silver.t", policy_applied=True, policy_note=note),
            result_body("b", table="silver.t"),
        ),
    )
    assert _words(block) == [
        "silver.t passed 2 of 2 checks passed",
        "a passed",
        f"note: {note}",
        "b passed",
    ]
    assert block[2] == f"    note: {note}"


def test_runs_show_without_checks_prints_no_block(tmp_path: Path) -> None:
    api = FakeApi()
    plain = _run("failed", _task("a.b", "failed"))
    api.on("GET", _RUN_PATH, json=plain)
    older = invoke(make_state(tmp_path, api), "runs", "show", _ID, "--env", "dev")
    with_empty = _run("failed", {**_task("a.b", "failed"), "kind": "asset", "checks": []})
    api.on("GET", _RUN_PATH, json=with_empty)
    newer = invoke(make_state(tmp_path, api), "runs", "show", _ID, "--env", "dev")
    assert newer.stdout == older.stdout
    assert _show(tmp_path, _task("a.b", "succeeded")) == []


def test_run_prints_the_check_block_in_its_summary(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("POST", "/api/v1/environments/dev/runs", status=202, json=_run("queued"))
    api.on(
        "GET",
        _RUN_PATH,
        json=_run(
            "failed",
            _checked(
                "silver.t", result_body("a", table="silver.t", outcome="failed", failing_rows=1)
            ),
        ),
    )
    result = invoke(make_state(tmp_path, api), "run", "analytics", "--env", "dev", env=_CI)
    assert result.exit_code == 1
    assert "silver.t   FAILED   0 of 1 checks passed" in result.stdout


def test_runs_show_json_has_the_checks(tmp_path: Path) -> None:
    api = FakeApi()
    api.on(
        "GET",
        _RUN_PATH,
        json=_run(
            "failed",
            _checked(
                "silver.t", result_body("a", table="silver.t", outcome="failed", failing_rows=1)
            ),
        ),
    )
    result = invoke(
        make_state(tmp_path, api), "--output", "json", "runs", "show", _ID, "--env", "dev"
    )
    (task,) = json.loads(result.stdout)["tasks"]
    assert task["kind"] == "asset"
    assert [(c["check"], c["outcome"], c["failing_rows"]) for c in task["checks"]] == [
        ("a", "failed", 1)
    ]


def test_a_check_text_in_runs_show_cannot_inject_terminal_controls(tmp_path: Path) -> None:
    block = _show(
        tmp_path,
        _checked(
            "silver.t\x1b[2J",
            result_body(
                "a\x07", table="silver.t", outcome="errored", failing_rows=None, error="x\x1b[31m"
            ),
        ),
    )
    assert "\x1b" not in "".join(block)
    assert "\x07" not in "".join(block)


def test_runs_show_aligns_the_tails_of_a_group_with_a_kept_state(tmp_path: Path) -> None:
    block = _show(
        tmp_path,
        _checked(
            "silver.t",
            result_body("a", table="silver.t"),
            result_body("b", table="silver.t", outcome="failed", on_failure="keep", failing_rows=2),
            result_body("c", table="silver.t", outcome="failed", failing_rows=1),
        ),
    )
    # name column 10 wide, state column 13 wide ("failed (kept)"), two spaces, then the tail
    assert block == [
        "silver.t   " + "FAILED".ljust(13) + "  1 of 3 checks passed",
        "  a        passed",
        "  b        " + "failed (kept)" + "  2 rows",
        "  c        " + "FAILED".ljust(13) + "  1 row",
    ]


def test_a_group_without_a_kept_state_prints_as_before(tmp_path: Path) -> None:
    block = _show(
        tmp_path,
        _checked(
            "silver.t",
            result_body("a", table="silver.t"),
            result_body("b", table="silver.t", outcome="failed", failing_rows=3),
        ),
    )
    assert block == [
        "silver.t   FAILED   1 of 2 checks passed",
        "  a        passed",
        "  b        FAILED   3 rows",
    ]


def test_run_shows_a_kept_check_in_the_check_block(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("POST", "/api/v1/environments/dev/runs", status=202, json=_run("queued"))
    api.on(
        "GET",
        _RUN_PATH,
        json=_run(
            "failed",
            _checked(
                "silver.t",
                result_body(
                    "a", table="silver.t", outcome="failed", on_failure="keep", failing_rows=1
                ),
            ),
        ),
    )
    result = invoke(make_state(tmp_path, api), "run", "analytics", "--env", "dev", env=_CI)
    assert "failed (kept)  1 row" in result.stdout


def test_a_run_whose_only_failure_is_a_kept_check_exits_1_with_the_message(
    tmp_path: Path,
) -> None:
    message = (
        "Check `silver.t/a` failed: 1 row (checks/silver/t/a.sql). The table keeps this run's "
        "data (`on_failure: keep`), and nothing that reads it was built. Repair the data or the "
        f"asset; after a fix, deploy and run again; if the project did not change: "
        f"`lakana runs resume {_ID} --env dev`."
    )
    api = FakeApi()
    api.on("POST", "/api/v1/environments/dev/runs", status=202, json=_run("queued"))
    task = {
        **_checked(
            "silver.t",
            result_body("a", table="silver.t", outcome="failed", on_failure="keep", failing_rows=1),
        ),
        "status": "failed",
        "error_message": message,
    }
    api.on("GET", _RUN_PATH, json=_run("failed", task))
    result = invoke(make_state(tmp_path, api), "run", "analytics", "--env", "dev", env=_CI)
    assert result.exit_code == 1
    out = " ".join(_plain(result.stdout).split())
    assert "lakana runs resume" in out and "on_failure: keep" in out
    assert "rolled back" not in out and "set back" not in out


_TIMEOUT_FAILURE = {
    "code": "timeout",
    "cause": "The task ran longer than its time limit.",
    "next_step": "Make the task faster, or raise its timeout in the project and deploy again.",
}


def _failed_with_a_code(asset: str = "a.b", **extra: object) -> dict[str, object]:
    """Build a failed task body with the `timeout` failure.

    Parameters
    ----------
    asset
        Dotted name.
    **extra
        Fields to add or replace.

    Returns
    -------
    dict
        The task body.
    """
    return {**_task(asset, "failed"), "failure": _TIMEOUT_FAILURE, **extra}


def test_runs_show_prints_the_code_the_cause_and_the_next_step_of_a_failed_task(
    tmp_path: Path,
) -> None:
    """Under the table: the code, what happened, and what to do, for each failed task."""
    api = FakeApi()
    api.on(
        "GET",
        _RUN_PATH,
        json=_run("failed", _failed_with_a_code(), _task("a.c", "upstream_failed", 0)),
    )

    shown = _plain(
        invoke(make_state(tmp_path, api), "runs", "show", _ID, "--env", "dev", env=_CI).stdout
    )

    assert "a.b failed: timeout" in shown
    assert "The task ran longer than its time limit." in shown
    assert "Next: Make the task faster, or raise its timeout in the project and deploy again." in (
        shown
    )
    assert "a.c" in shown and "failed: upstream_failed" not in shown


def test_a_task_that_failed_before_the_codes_existed_shows_unknown(tmp_path: Path) -> None:
    """The server sends `unknown` for such a task; the CLI shows it like any other code."""
    unknown = {
        "code": "unknown",
        "cause": "This failure was recorded before failure codes existed.",
        "next_step": "Read the detail and the task's log.",
    }
    api = FakeApi()
    api.on("GET", _RUN_PATH, json=_run("failed", _failed_with_a_code(failure=unknown)))

    shown = _plain(
        invoke(make_state(tmp_path, api), "runs", "show", _ID, "--env", "dev", env=_CI).stdout
    )

    assert "a.b failed: unknown" in shown
    assert "Next: Read the detail and the task's log." in shown


def test_a_failed_task_without_a_failure_from_an_older_server_prints_no_block(
    tmp_path: Path,
) -> None:
    """Nothing is made up: the error column is all there is."""
    api = FakeApi()
    api.on("GET", _RUN_PATH, json=_run("failed", _task("a.b", "failed")))

    shown = _plain(
        invoke(make_state(tmp_path, api), "runs", "show", _ID, "--env", "dev", env=_CI).stdout
    )

    assert "boom" in shown
    assert "Next:" not in shown


def test_run_prints_the_next_step_when_a_task_fails(tmp_path: Path) -> None:
    """`lakana run` says so under the task's progress line, once, and again in the summary."""
    api = FakeApi()
    api.on("POST", "/api/v1/environments/dev/runs", status=202, json=_run("queued"))
    _sequence(
        api,
        "GET",
        _RUN_PATH,
        [
            _run("running", _task("a.b", "running")),
            _run("failed", _failed_with_a_code()),
        ],
    )

    result = invoke(make_state(tmp_path, api), "run", "analytics", "--env", "dev", env=_CI)

    assert result.exit_code == 1
    lines = _plain(result.stdout)
    progress = (
        "a.b failed attempt 1/3 2.0s failed: timeout. Next: Make the task faster, or raise its "
        "timeout in the project and deploy again."
    )
    assert progress in lines


def test_run_prints_no_next_step_for_a_task_that_will_try_again(tmp_path: Path) -> None:
    """A retry is not the end of the task: no next step yet."""
    api = FakeApi()
    api.on("POST", "/api/v1/environments/dev/runs", status=202, json=_run("queued"))
    retrying = {**_task("a.b", "pending"), "failure": _TIMEOUT_FAILURE}
    _sequence(
        api,
        "GET",
        _RUN_PATH,
        [_run("running", retrying), _run("succeeded", _task("a.b", "succeeded"))],
    )

    result = invoke(make_state(tmp_path, api), "run", "analytics", "--env", "dev", env=_CI)

    assert "Next:" not in result.stdout


def test_a_failure_text_cannot_inject_terminal_controls_or_lines(tmp_path: Path) -> None:
    """The server's sentences are untrusted text: escaped and on one line."""
    hostile = {
        "code": "internal\x1b[31m",
        "cause": "line one\nline two\x07",
        "next_step": "do this\x1b]0;title\x07",
    }
    api = FakeApi()
    api.on("GET", _RUN_PATH, json=_run("failed", _failed_with_a_code(failure=hostile)))

    shown = invoke(make_state(tmp_path, api), "runs", "show", _ID, "--env", "dev", env=_CI).stdout

    assert "\x1b[31m" not in shown and "\x07" not in shown and "\x1b]" not in shown
    assert "line one\\nline two" in shown


def test_runs_show_json_keeps_the_failure_and_the_failed_task_count(tmp_path: Path) -> None:
    """JSON output is the object as the server sent it."""
    api = FakeApi()
    api.on("GET", _RUN_PATH, json={**_run("failed", _failed_with_a_code()), "failed_tasks": 1})

    shown = json.loads(
        invoke(
            make_state(tmp_path, api), "--output", "json", "runs", "show", _ID, "--env", "dev"
        ).stdout
    )

    assert shown["failed_tasks"] == 1
    assert shown["tasks"][0]["failure"] == _TIMEOUT_FAILURE


def test_runs_show_marks_a_generated_check(tmp_path: Path) -> None:
    """A check from the asset's header says so, as `lakana check list` does."""
    block = _show(
        tmp_path,
        _checked(
            "gold.fact_sales",
            result_body("key_unique", generated=True),
            result_body("positive"),
        ),
    )
    assert block == [
        "gold.fact_sales          passed   2 of 2 checks passed",
        "  key_unique (generated) passed",
        "  positive               passed",
    ]


def test_runs_show_counts_the_checks_it_does_not_show(tmp_path: Path) -> None:
    block = _show(
        tmp_path,
        {**_checked("gold.fact_sales", result_body("positive")), "hidden_checks": 2},
    )
    assert block[-1] == "  2 checks of references to tables you cannot read are not shown."


_POLICY_MESSAGE = (
    "A row filter on `gold.orders` uses column `region`, which this run removed from the table. "
    "Reads of the table by the people the filter applies to are refused until it is created "
    "again. Next: ask an administrator to create the filter again for the column it is meant "
    'for and then revoke the old one, in that order (see "After a rename" in the documentation).'
)


def _warned_task() -> dict[str, object]:
    """Build a succeeded task body with one policy warning.

    Returns
    -------
    dict
        The task body.
    """
    warning = {
        "kind": "row_filter",
        "column": "region",
        "change": "removed",
        "policy_id": None,
        "message": _POLICY_MESSAGE + "\x1b[2J",
    }
    return {**_task("gold.orders", "succeeded"), "policy_warnings": [warning]}


def test_runs_show_and_run_print_a_policy_warning_in_full_under_the_tasks(tmp_path: Path) -> None:
    """The whole message, escaped, after the task table; a run without warnings prints none."""
    api = FakeApi()
    api.on("GET", _RUN_PATH, json=_run("succeeded", _warned_task()))
    api.on("POST", "/api/v1/environments/dev/runs", status=202, json=_run("queued"))
    state = make_state(tmp_path, api)

    for args in (("runs", "show", _ID, "--env", "dev"), ("run", "analytics", "--env", "dev")):
        lines = invoke(state, *args, env=_CI).stdout.splitlines()
        at = lines.index("gold.orders warning:")
        assert lines[at + 1] == f"  {_POLICY_MESSAGE}\\x1b[2J"

    api.on("GET", _RUN_PATH, json=_run("succeeded", _task("gold.orders", "succeeded")))
    assert "warning" not in invoke(state, "runs", "show", _ID, "--env", "dev", env=_CI).stdout
