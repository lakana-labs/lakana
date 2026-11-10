"""Tests for `lakana status`: the instance's state in one line, and for administrators the bars."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from conftest import FakeApi, invoke, make_state
from test_runs_cmd import _WNOW, _worker

_STATUS = "/api/v1/status"
_GIB = 1024**3


@pytest.fixture(autouse=True)
def _worker_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the clock of the worker lines' `last_seen` column stand still.

    Parameters
    ----------
    monkeypatch
        Pytest's monkeypatch.
    """
    monkeypatch.setattr("lakana.cli.runs._now", lambda: _WNOW)


def _status(state: str, causes: list[str], detail: dict | None = None) -> dict:
    """Build an `InstanceStatusOut` body for one alive and one lost worker.

    Parameters
    ----------
    state
        `healthy`, `degraded` or `down`.
    causes
        The cause codes.
    detail
        The administrator's detail, if any.

    Returns
    -------
    dict
        The body.
    """
    return {
        "state": state,
        "causes": causes,
        "workers_alive": 1,
        "workers_total": 2,
        "detail": detail,
    }


def _run(tmp_path: Path, body: dict, *args: str) -> tuple[int, str]:
    """Run `lakana status` against a server that answers `body`.

    Parameters
    ----------
    tmp_path
        Holds the profile.
    body
        The status the server answers.
    *args
        Global options before the command.

    Returns
    -------
    tuple of (int, str)
        The exit code and standard output.
    """
    api = FakeApi()
    api.on("GET", _STATUS, json=body)
    result = invoke(make_state(tmp_path, api), *args, "status", env={"COLUMNS": "300"})
    return result.exit_code, result.stdout


def test_status_prints_the_state_and_for_admins_the_bars(tmp_path: Path) -> None:
    worker = _worker(
        "a:1:x",
        query_run_id="11111111-1111-4111-8111-111111111111",
        memory={
            "used_bytes": int(1.2 * _GIB),
            "limit_bytes": 4 * _GIB,
            "measured_at": _WNOW.isoformat(),
        },
    )
    detail = {
        "slots_busy": 1,
        "slots_total": 4,
        "memory_used_bytes": int(1.2 * _GIB),
        "memory_limit_bytes": 8 * _GIB,
        "workers": [worker, _worker("b:2:y", status="lost")],
    }
    code, out = _run(tmp_path, _status("degraded", ["worker_lost"], detail))
    assert code == 0
    lines = out.splitlines()
    assert lines[0] == "Degraded — Queries and runs may be slower: 1 of 2 workers is down."
    assert lines[1:3] == ["Compute: 1 of 4 slots busy", "Memory: 1.2 GiB of 8.0 GiB"]
    rows = [line for line in lines if line.startswith("│")]
    assert re.search(
        r"a:1:x\s+│.*│ alive\s+│ all\s+│ query 11111111\s+│ 1.2 GiB of 4.0 GiB", rows[0]
    )
    assert re.search(r"b:2:y\s+│.*│ lost\s+│", rows[1])


def test_a_reader_sees_one_line(tmp_path: Path) -> None:
    code, out = _run(tmp_path, _status("healthy", []))
    assert (code, out.strip()) == (0, "Healthy — Lakana is working normally.")


@pytest.mark.parametrize(
    ("state", "causes", "line"),
    [
        ("down", ["database"], "Down — The catalog database does not answer"),
        ("down", ["storage"], "Down — The storage of an environment does not answer"),
        ("down", ["no_worker"], "Down — No worker is running, so queries and runs wait."),
        ("degraded", ["memory_high"], "Degraded — A worker uses 90 % or more of its memory"),
        ("down", [], "Down — Lakana cannot do work right now; ask an administrator."),
        ("degraded", ["later_cause"], "Degraded — Lakana works, but slower than usual; ask an"),
    ],
)
def test_each_cause_has_its_sentence(
    tmp_path: Path, state: str, causes: list[str], line: str
) -> None:
    code, out = _run(tmp_path, _status(state, causes))
    assert code == 0
    assert out.startswith(line)


def test_two_lost_workers_are_counted(tmp_path: Path) -> None:
    body = {**_status("degraded", ["worker_lost"]), "workers_alive": 1, "workers_total": 3}
    assert "2 of 3 workers are down." in _run(tmp_path, body)[1]


def test_memory_without_a_limit_and_no_alive_worker(tmp_path: Path) -> None:
    detail = {
        "slots_busy": 0,
        "slots_total": 2,
        "memory_used_bytes": _GIB,
        "memory_limit_bytes": None,
        "workers": [_worker("a:1:x")],
    }
    out = _run(tmp_path, _status("healthy", [], detail))[1]
    assert "Memory: 1.0 GiB in use; a worker reports no limit" in out
    assert re.search(r"a:1:x\s+│.*│ -\s+│", out)
    nobody = {**_status("down", ["no_worker"]), "workers_alive": 0, "workers_total": 0}
    nobody["detail"] = {**detail, "slots_total": 0, "memory_used_bytes": 0, "workers": []}
    lines = _run(tmp_path, nobody)[1].splitlines()
    assert len(lines) == 1
    only_lost = {**nobody, "workers_total": 1}
    only_lost["detail"] = {**nobody["detail"], "workers": [_worker("l:1:x", status="lost")]}
    out = _run(tmp_path, only_lost)[1]
    assert "Compute" not in out and "Memory" not in out
    assert re.search(r"l:1:x\s+│.*│ lost\s+│", out)


def test_status_json_keeps_the_body(tmp_path: Path) -> None:
    body = _status("healthy", [])
    code, out = _run(tmp_path, body, "--output", "json")
    assert (code, json.loads(out)) == (0, body)
