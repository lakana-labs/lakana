"""The filters, the cursor and the next-page line of `runs list`, `deploys list` and `--since`."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from conftest import FakeApi, invoke, make_state
from test_deploys_cmd import _deploy
from test_runs_cmd import _run

from lakana.cli._paging import parse_since
from lakana.errors import ValidationError

_RUNS = "/api/v1/environments/dev/runs"
_DEPLOYS = "/api/v1/environments/dev/deploys"


def _paged(path: str, body: Sequence[Mapping[str, object]], cursor: str | None) -> FakeApi:
    """Build a fake whose list route answers with `body` and, if given, a next cursor.

    Parameters
    ----------
    path
        The route.
    body
        The rows.
    cursor
        The `X-Next-Cursor` header, or `None` for none.

    Returns
    -------
    FakeApi
        The fake.
    """
    api = FakeApi()
    headers = {} if cursor is None else {"X-Next-Cursor": cursor}
    api.routes["GET", path] = lambda request: httpx.Response(200, json=body, headers=headers)
    return api


def test_runs_list_sends_every_filter(tmp_path: Path) -> None:
    api = _paged(_RUNS, [_run("failed")], None)
    result = invoke(
        make_state(tmp_path, api),
        "runs",
        "list",
        "--env",
        "dev",
        "--repo",
        "analytics",
        "--status",
        "failed",
        "--status",
        "cancelled,queued",
        "--trigger",
        "schedule",
        "--job",
        "nightly",
        "--since",
        "2026-10-18T09:30:00Z",
        "--limit",
        "20",
        "--cursor",
        "abc",
    )
    assert result.exit_code == 0, result.stderr
    assert dict(api.requests[-1].url.params) == {
        "repository": "analytics",
        "status": "failed,cancelled,queued",
        "trigger": "schedule",
        "job": "nightly",
        "since": "2026-10-18T09:30:00+00:00",
        "limit": "20",
        "cursor": "abc",
    }
    assert "More results" not in result.stdout


def test_runs_list_names_the_command_for_the_next_page(tmp_path: Path) -> None:
    api = _paged(_RUNS, [_run("failed")], "next-1")
    result = invoke(
        make_state(tmp_path, api),
        "runs",
        "list",
        "--env",
        "dev",
        "--status",
        "failed",
        "--since",
        "2026-10-18",
        "--limit",
        "1",
    )
    assert result.exit_code == 0, result.stderr
    assert (
        "More results: lakana runs list --env dev --status failed --since 2026-10-18 "
        "--limit 1 --cursor next-1" in result.stdout
    )


def test_runs_list_json_keeps_stdout_a_plain_list(tmp_path: Path) -> None:
    api = _paged(_RUNS, [_run("failed")], "next-1")
    result = invoke(make_state(tmp_path, api), "--output", "json", "runs", "list", "--env", "dev")
    assert result.exit_code == 0, result.stderr
    assert len(json.loads(result.stdout)) == 1
    assert "More results: lakana runs list --env dev --cursor next-1" in result.stderr


def test_runs_list_refuses_a_since_it_cannot_read(tmp_path: Path) -> None:
    api = _paged(_RUNS, [], None)
    result = invoke(
        make_state(tmp_path, api), "runs", "list", "--env", "dev", "--since", "yesterday"
    )
    assert result.exit_code == 2
    assert "--since 'yesterday' is not a date or time" in result.stderr
    assert "2026-10-18T09:30:00Z" in result.stderr
    assert api.requests == []


def test_a_server_refusal_of_a_filter_is_printed(tmp_path: Path) -> None:
    api = FakeApi()
    api.on(
        "GET",
        _RUNS,
        status=422,
        json={
            "detail": "Unknown status 'done': `status` must be one of queued, running.",
            "type": "ValidationError",
        },
    )
    result = invoke(make_state(tmp_path, api), "runs", "list", "--env", "dev", "--status", "done")
    assert result.exit_code != 0
    assert "`status` must be one of queued, running" in result.stderr


def test_deploys_list_sends_its_filters_and_names_the_next_page(tmp_path: Path) -> None:
    api = _paged(_DEPLOYS, [_deploy("succeeded")], "d-next")
    result = invoke(
        make_state(tmp_path, api),
        "deploys",
        "list",
        "--env",
        "dev",
        "--repo",
        "analytics",
        "--limit",
        "1",
        "--cursor",
        "d0",
    )
    assert result.exit_code == 0, result.stderr
    assert dict(api.requests[-1].url.params) == {
        "repository": "analytics",
        "limit": "1",
        "cursor": "d0",
    }
    assert (
        "More results: lakana deploys list --env dev --repo analytics --limit 1 "
        "--cursor d-next" in result.stdout
    )


def test_deploys_list_without_a_next_page_prints_no_line(tmp_path: Path) -> None:
    api = _paged(_DEPLOYS, [_deploy("succeeded")], None)
    result = invoke(make_state(tmp_path, api), "deploys", "list", "--env", "dev")
    assert result.exit_code == 0, result.stderr
    assert "More results" not in result.stdout
    assert dict(api.requests[-1].url.params) == {}


def test_a_value_in_the_next_page_line_is_quoted_and_made_safe(tmp_path: Path) -> None:
    api = _paged(_RUNS, [_run("failed")], "next-1")
    result = invoke(
        make_state(tmp_path, api), "runs", "list", "--env", "dev", "--job", "two words; rm\x1b[2J"
    )
    assert result.exit_code == 0, result.stderr
    assert "\x1b" not in result.stdout
    assert "--job 'two words; rm" in result.stdout


def test_since_is_read_in_the_local_time_zone_unless_it_has_one() -> None:
    assert parse_since(None) is None
    aware = parse_since("2026-10-18T09:30:00+02:00")
    assert aware == datetime(2026, 10, 18, 7, 30, tzinfo=UTC)
    naive = parse_since("2026-10-18")
    assert naive is not None
    assert naive.tzinfo is not None
    assert naive.replace(tzinfo=None) == datetime(2026, 10, 18)


def test_a_refused_filter_in_the_sdk_is_a_validation_error(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("GET", _RUNS, status=422, json={"detail": "bad", "type": "ValidationError"})
    with pytest.raises(ValidationError):
        api.client().runs.list("dev", status="done")
