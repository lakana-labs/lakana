"""`lakana job list|run|pause|resume`."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import FakeApi, invoke, make_state
from test_runs_cmd import _ID, _run, _task

import lakana.cli.runs as runs_module

_JOBS = "/api/v1/environments/dev/jobs"
_CI = {"GITHUB_ACTIONS": "true"}


def _plain(text: str) -> str:
    """Return CLI output without ANSI codes or Rich borders, whitespace collapsed.

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


def _job(name: str = "daily", **changes: Any) -> dict[str, Any]:  # ruff: ignore[any-type]
    """Build a `JobOut` body.

    Parameters
    ----------
    name
        The job.
    **changes
        Fields to change.

    Returns
    -------
    dict
        The body.
    """
    body: dict[str, Any] = {
        "name": name,
        "environment": "dev",
        "repository": "analytics",
        "select": ["silver.*"],
        "upstream": False,
        "downstream": False,
        "cron": "0 3 * * *",
        "timezone": "Europe/Berlin",
        "state": "scheduled",
        "paused": False,
        "next_run_at": "2026-07-01T01:00:00+00:00",
        "next_run_local": "2026-07-01T03:00:00+02:00",
        "waiting_for": None,
        "waits_for_run_id": None,
        "last_run_id": _ID,
        "last_run_status": "succeeded",
        "last_run_trigger": "schedule",
        "last_run_at": "2026-06-30T01:00:00+00:00",
        "triggers": [
            {
                "kind": "schedule",
                "cron": "0 3 * * *",
                "timezone": "Europe/Berlin",
                "paused": False,
                "next_run_at": "2026-07-01T01:00:00+00:00",
            },
            {"kind": "on_demand"},
        ],
    }
    return {**body, **changes}


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


def test_list_shows_state_schedule_next_run_in_both_zones_and_the_last_run(
    tmp_path: Path,
) -> None:
    api = FakeApi()
    api.on(
        "GET",
        _JOBS,
        json=[
            _job(),
            _job(
                "manual",
                cron=None,
                timezone=None,
                state="no schedule",
                next_run_at=None,
                next_run_local=None,
                last_run_id=None,
                last_run_status=None,
                last_run_trigger=None,
                last_run_at=None,
            ),
            _job(
                "busy",
                state="waiting",
                waiting_for={"code": "job_waits", "message": "A start waits for run X."},
            ),
        ],
    )
    result = invoke(make_state(tmp_path, api), "job", "list", "--env", "dev", "--repo", "analytics")
    assert result.exit_code == 0, result.output
    assert api.requests[0].url.params["repository"] == "analytics"
    out = _plain(result.stdout)
    assert "daily" in out and "scheduled" in out and "no schedule" in out
    assert "2026-07-01 03:00 Europe/Berlin (01:00 UTC)" in out
    assert "succeeded (schedule)" in out
    assert "busy: A start waits for run X." in out


def test_list_shows_the_triggers_and_marks_a_paused_schedule(tmp_path: Path) -> None:
    api = FakeApi()
    paused = {
        "kind": "schedule",
        "cron": "0 6 * * *",
        "timezone": "Europe/Berlin",
        "paused": True,
        "next_run_at": None,
    }
    api.on(
        "GET",
        _JOBS,
        json=[
            _job(),
            _job("sleepy", triggers=[paused, {"kind": "on_demand"}]),
            _job("manual", triggers=[{"kind": "on_demand"}, {"kind": "after_deploy"}]),
        ],
    )
    result = invoke(
        make_state(tmp_path, api), "job", "list", "--env", "dev", env={"COLUMNS": "200"}
    )
    out = _plain(result.stdout)
    assert "triggers" in out
    assert "schedule 0 3 * * * Europe/Berlin" in out and "on demand" in out
    assert "schedule 0 6 * * * Europe/Berlin (paused)" in out
    assert "after_deploy" not in out


def test_show_lists_each_trigger_with_its_next_run(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("GET", f"{_JOBS}/daily", json=_job())
    result = invoke(
        make_state(tmp_path, api), "job", "show", "daily", "--env", "dev", "--repo", "analytics"
    )
    assert result.exit_code == 0, result.output
    assert api.requests[0].url.params["repository"] == "analytics"
    out = _plain(result.stdout)
    assert "schedule 0 3 * * * Europe/Berlin" in out
    assert "next run 2026-07-01 03:00 Europe/Berlin (01:00 UTC)" in out
    assert "on demand" in out and "lakana job run daily" in out


def test_list_and_show_name_the_deploy_trigger(tmp_path: Path) -> None:
    api = FakeApi()
    triggers = [{"kind": "deploy"}, {"kind": "on_demand"}]
    api.on("GET", _JOBS, json=[_job("refresh", triggers=triggers)])
    api.on("GET", f"{_JOBS}/refresh", json=_job("refresh", triggers=triggers))
    listed = invoke(
        make_state(tmp_path, api), "job", "list", "--env", "dev", env={"COLUMNS": "200"}
    )
    assert "after each deploy" in _plain(listed.stdout)
    shown = invoke(make_state(tmp_path, api), "job", "show", "refresh", "--env", "dev")
    assert shown.exit_code == 0, shown.output
    assert "after each deploy: starts as the deployer" in _plain(shown.stdout)


def test_show_of_a_waiting_job_says_why_it_waits(tmp_path: Path) -> None:
    api = FakeApi()
    waiting = {"code": "job_waits", "message": "A start waits for run X."}
    api.on("GET", f"{_JOBS}/busy", json=_job("busy", state="waiting", waiting_for=waiting))
    result = invoke(make_state(tmp_path, api), "job", "show", "busy", "--env", "dev")
    assert result.exit_code == 0, result.output
    assert "A start waits for run X." in _plain(result.stdout)


def test_show_of_a_paused_schedule_and_of_a_job_without_one(tmp_path: Path) -> None:
    api = FakeApi()
    paused = {
        "kind": "schedule",
        "cron": "0 6 * * *",
        "timezone": "UTC",
        "paused": True,
        "next_run_at": None,
    }
    api.on("GET", f"{_JOBS}/sleepy", json=_job("sleepy", triggers=[paused, {"kind": "on_demand"}]))
    api.on("GET", f"{_JOBS}/manual", json=_job("manual", triggers=[{"kind": "on_demand"}]))
    sleepy = _plain(
        invoke(make_state(tmp_path, api), "job", "show", "sleepy", "--env", "dev").stdout
    )
    assert "paused" in sleepy and "next run" not in sleepy
    manual = _plain(
        invoke(make_state(tmp_path, api), "job", "show", "manual", "--env", "dev").stdout
    )
    assert "schedule 0" not in manual and "on demand" in manual


def test_show_as_json_is_the_whole_object(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("GET", f"{_JOBS}/daily", json=_job())
    result = invoke(
        make_state(tmp_path, api), "--output", "json", "job", "show", "daily", "--env", "dev"
    )
    body = json.loads(result.stdout)
    assert [t["kind"] for t in body["triggers"]] == ["schedule", "on_demand"]


def test_show_of_an_unknown_job_exits_6_with_the_names(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("GET", f"{_JOBS}/nope", status=404, json={"detail": "No job 'nope'. Its jobs: daily."})
    result = invoke(make_state(tmp_path, api), "job", "show", "nope", "--env", "dev")
    assert result.exit_code == 6
    assert "daily" in _plain(result.stderr)


def test_a_job_in_utc_shows_the_time_in_utc_only(tmp_path: Path) -> None:
    api = FakeApi()
    api.on(
        "GET",
        _JOBS,
        json=[_job(timezone="UTC", next_run_local="2026-07-01T01:00:00+00:00")],
    )
    result = invoke(make_state(tmp_path, api), "job", "list", "--env", "dev")
    out = _plain(result.stdout)
    assert "2026-07-01 01:00 UTC" in out and "(01:00 UTC)" not in out


def test_list_fits_80_columns(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("GET", _JOBS, json=[_job(), _job("nightly_export")])
    result = invoke(make_state(tmp_path, api), "job", "list", "--env", "dev", env={"COLUMNS": "80"})
    assert result.exit_code == 0
    assert all(len(line) <= 80 for line in result.stdout.splitlines())
    assert "daily" in result.stdout and "state" in result.stdout


def test_list_as_json_is_the_whole_object(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("GET", _JOBS, json=[_job()])
    result = invoke(make_state(tmp_path, api), "--output", "json", "job", "list", "--env", "dev")
    body = json.loads(result.stdout)
    assert body[0]["name"] == "daily" and body[0]["next_run_local"].endswith("+02:00")


def test_run_follows_the_queued_run_like_lakana_run(tmp_path: Path) -> None:
    api = FakeApi()
    queued = {
        "run": {**_run("queued", _task("a.b", "pending", 0)), "job": "daily"},
        "waiting": None,
    }
    api.on("POST", f"{_JOBS}/daily/runs", status=201, json=queued)
    api.on(
        "GET",
        f"/api/v1/environments/dev/runs/{_ID}",
        json=_run("succeeded", _task("a.b", "succeeded")),
    )
    result = invoke(
        make_state(tmp_path, api),
        "job",
        "run",
        "daily",
        "--env",
        "dev",
        "--repo",
        "analytics",
        env=_CI,
    )
    assert result.exit_code == 0, result.output
    assert result.stderr.splitlines()[0] == _ID
    assert json.loads(api.requests[0].content) == {"repository": "analytics"}
    assert "a.b succeeded" in _plain(result.stdout)


def test_run_no_wait_prints_only_the_id(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("POST", f"{_JOBS}/daily/runs", status=201, json={"run": _run("queued"), "waiting": None})
    result = invoke(
        make_state(tmp_path, api),
        "job",
        "run",
        "daily",
        "--env",
        "dev",
        "--repo",
        "analytics",
        "--no-wait",
    )
    assert result.stdout.strip() == _ID
    assert [r.method for r in api.requests] == ["POST"]


def test_a_waiting_start_exits_0_and_says_what_it_waits_for(tmp_path: Path) -> None:
    api = FakeApi()
    waiting = {
        "job": "daily",
        "waits_for_run_id": _ID,
        "trigger": "manual",
        "requested_at": "2026-10-14T12:00:00+00:00",
        "requested_by": "admin",
        "scheduled_for": None,
        "missed": 0,
        "merged": 1,
        "message": f"`daily` waits for run {_ID} to finish; it starts then.",
    }
    api.on("POST", f"{_JOBS}/daily/runs", status=202, json={"run": None, "waiting": waiting})
    result = invoke(
        make_state(tmp_path, api), "job", "run", "daily", "--env", "dev", "--repo", "analytics"
    )
    assert result.exit_code == 0, result.output
    assert f"waits for run {_ID} to finish; it starts then." in _plain(result.stdout)
    assert [r.method for r in api.requests] == ["POST"]
    as_json = invoke(
        make_state(tmp_path, api),
        "--output",
        "json",
        "job",
        "run",
        "daily",
        "--env",
        "dev",
        "--repo",
        "analytics",
    )
    assert json.loads(as_json.stdout)["waiting"]["merged"] == 1


def test_a_reply_with_neither_run_nor_waiting_start_exits_0_quietly(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("POST", f"{_JOBS}/daily/runs", status=202, json={"run": None, "waiting": None})
    result = invoke(
        make_state(tmp_path, api), "job", "run", "daily", "--env", "dev", "--repo", "analytics"
    )
    assert result.exit_code == 0, result.output
    assert [r.method for r in api.requests] == ["POST"]


def test_run_of_an_unknown_job_exits_6_with_the_names(tmp_path: Path) -> None:
    api = FakeApi()
    api.on(
        "POST",
        f"{_JOBS}/nope/runs",
        status=404,
        json={"detail": "No job 'nope' in 'analytics' in 'dev'. Its jobs: daily, manual."},
    )
    result = invoke(
        make_state(tmp_path, api), "job", "run", "nope", "--env", "dev", "--repo", "analytics"
    )
    assert result.exit_code == 6
    assert "daily, manual" in _plain(result.stderr)


def test_pause_and_resume(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("POST", f"{_JOBS}/daily/pause", json=_job(paused=True, state="paused"))
    api.on("POST", f"{_JOBS}/daily/resume", json=_job())
    paused = invoke(
        make_state(tmp_path, api), "job", "pause", "daily", "--env", "dev", "--repo", "analytics"
    )
    assert paused.exit_code == 0, paused.output
    assert "Paused daily in dev" in _plain(paused.stdout)
    assert "lakana job resume daily --env dev" in _plain(paused.stdout)
    resumed = invoke(
        make_state(tmp_path, api), "job", "resume", "daily", "--env", "dev", "--repo", "analytics"
    )
    assert resumed.exit_code == 0, resumed.output
    assert "Resumed daily in dev; next run 2026-07-01 03:00 Europe/Berlin (01:00 UTC)." in _plain(
        resumed.stdout
    )
    assert json.loads(api.requests[0].content) == {"repository": "analytics"}


def test_pause_without_deploy_exits_5(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("POST", f"{_JOBS}/daily/pause", status=403, json={"detail": "Not permitted."})
    result = invoke(
        make_state(tmp_path, api), "job", "pause", "daily", "--env", "dev", "--repo", "analytics"
    )
    assert result.exit_code == 5


def test_runs_list_and_show_name_the_job_and_the_trigger(tmp_path: Path) -> None:
    api = FakeApi()
    scheduled = {
        **_run("succeeded"),
        "job": "daily",
        "trigger": "schedule",
        "scheduled_for": "2026-10-14T03:00:00+00:00",
        "missed": 2,
    }
    api.on("GET", "/api/v1/environments/dev/runs", json=[scheduled])
    api.on("GET", f"/api/v1/environments/dev/runs/{_ID}", json=scheduled)
    listed = invoke(make_state(tmp_path, api), "runs", "list", "--env", "dev")
    assert "daily" in listed.stdout and "schedule" in listed.stdout
    shown = invoke(make_state(tmp_path, api), "runs", "show", _ID, "--env", "dev")
    out = _plain(shown.stdout)
    assert "job daily" in out and "trigger schedule" in out and "missed 2" in out


_DELIVERY = "00000000-0000-0000-0000-0000000000dd"
_NOTIFICATIONS = f"/api/v1/environments/dev/notifications/{_DELIVERY}"


def _delivery(state: str = "pending", **changes: Any) -> dict[str, Any]:  # ruff: ignore[any-type]
    """Build a `DeliveryOut` body.

    Parameters
    ----------
    state
        ``pending``, ``delivered`` or ``failed``.
    **changes
        Fields to change.

    Returns
    -------
    dict
        The body.
    """
    body: dict[str, Any] = {
        "id": _DELIVERY,
        "job": "daily",
        "repository": "analytics",
        "event": "test",
        "state": state,
        "attempts": 0,
        "max_attempts": 5,
        "last_result": None,
        "delivered_at": None,
        "next_attempt_at": "2026-10-14T12:00:00+00:00",
    }
    return {**body, **changes}


def _api_with_answers(*answers: dict[str, Any]) -> FakeApi:
    """Build a fake server that queues a test and answers each poll with the next body.

    Parameters
    ----------
    *answers
        The delivery bodies, one per poll; the last one repeats.

    Returns
    -------
    FakeApi
        The fake server.
    """
    api = FakeApi()
    api.on("POST", f"{_JOBS}/daily/notify-test", status=202, json={"delivery_id": _DELIVERY})
    polled = iter(answers)
    last: list[dict[str, Any]] = [answers[-1]]

    def poll(request: httpx.Request) -> httpx.Response:
        last[0] = next(polled, last[0])
        return httpx.Response(200, json=last[0])

    api.routes["GET", _NOTIFICATIONS] = poll
    return api


def _notify_test(tmp_path: Path, api: FakeApi, *extra: str) -> Any:  # ruff: ignore[any-type]
    """Run `lakana job notify-test daily`.

    Parameters
    ----------
    tmp_path
        The profile directory.
    api
        The fake server.
    *extra
        More arguments for the command.

    Returns
    -------
    Any
        The invocation result.
    """
    return invoke(
        make_state(tmp_path, api),
        *extra,
        "job",
        "notify-test",
        "daily",
        "--env",
        "dev",
        "--repo",
        "analytics",
    )


def test_notify_test_waits_for_the_delivery_and_exits_0_when_it_arrives(tmp_path: Path) -> None:
    api = _api_with_answers(_delivery(), _delivery(attempts=1), _delivery("delivered", attempts=1))
    result = _notify_test(tmp_path, api)
    assert result.exit_code == 0, result.output
    assert _plain(result.stdout) == "delivered"
    assert json.loads(api.requests[0].content) == {"repository": "analytics"}
    assert [r.method for r in api.requests] == ["POST", "GET", "GET", "GET"]


def test_notify_test_prints_the_refusal_and_the_next_step_and_exits_1(tmp_path: Path) -> None:
    reason = "the address is private; allow it with LAKANA_WEBHOOK__ALLOW_HOSTS"
    api = _api_with_answers(_delivery("failed", attempts=1, last_result=reason))
    result = _notify_test(tmp_path, api)
    assert result.exit_code == 1
    err = _plain(result.stderr)
    assert f"failed: {reason}" in err
    assert "lakana job notify-test daily --env dev" in err


def test_notify_test_that_is_still_trying_names_the_command_to_look_later(
    tmp_path: Path,
) -> None:
    api = _api_with_answers(
        _delivery(attempts=2, last_result="the server answered 503, will try again")
    )
    result = _notify_test(tmp_path, api)
    assert result.exit_code == 1
    err = _plain(result.stderr)
    assert "still trying (attempt 2 of 5)" in err
    assert f"lakana job notification {_DELIVERY} --env dev" in err
    assert len([r for r in api.requests if r.method == "GET"]) == 30


def test_notify_test_before_any_attempt_says_a_worker_has_not_picked_it_up(
    tmp_path: Path,
) -> None:
    result = _notify_test(tmp_path, _api_with_answers(_delivery()))
    assert result.exit_code == 1
    assert "no attempt yet" in _plain(result.stderr)


def test_notify_test_as_json_carries_the_delivery(tmp_path: Path) -> None:
    api = _api_with_answers(_delivery("delivered", attempts=1))
    result = _notify_test(tmp_path, api, "--output", "json")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["state"] == "delivered"


def test_notify_test_without_notify_exits_7_and_without_deploy_exits_5(tmp_path: Path) -> None:
    api = FakeApi()
    message = (
        "Job `daily` has no usable `notify:` in `dev`; add or fix it in lakana.yaml and deploy."
    )
    api.on("POST", f"{_JOBS}/daily/notify-test", status=409, json={"detail": message})
    refused = _notify_test(tmp_path, api)
    assert refused.exit_code == 7
    assert message in _plain(refused.stderr)
    api.on("POST", f"{_JOBS}/daily/notify-test", status=403, json={"detail": "Not permitted."})
    assert _notify_test(tmp_path, api).exit_code == 5


def test_notification_shows_one_delivery(tmp_path: Path) -> None:
    api = FakeApi()
    api.on(
        "GET",
        _NOTIFICATIONS,
        json=_delivery("pending", attempts=3, last_result="the server answered 503"),
    )
    result = invoke(make_state(tmp_path, api), "job", "notification", _DELIVERY, "--env", "dev")
    assert result.exit_code == 0, result.output
    out = _plain(result.stdout)
    assert "daily" in out and "test" in out and "retrying (attempt 3 of 5)" in out


def test_runs_show_lists_the_notifications_of_the_run(tmp_path: Path) -> None:
    api = FakeApi()
    ids = [f"00000000-0000-0000-0000-0000000000b{n}" for n in range(1, 5)]
    notifications = [
        {
            "id": ids[0],
            "event": "failed",
            "state": "failed",
            "attempts": 1,
            "max_attempts": 5,
            "last_result": "the server answered 404",
            "delivered_at": None,
        },
        {
            "id": ids[1],
            "event": "recovered",
            "state": "pending",
            "attempts": 2,
            "max_attempts": 5,
            "last_result": "timeout",
            "delivered_at": None,
        },
        {
            "id": ids[2],
            "event": "failed",
            "state": "delivered",
            "attempts": 1,
            "max_attempts": 5,
            "last_result": None,
            "delivered_at": "2026-10-14T12:05:00+00:00",
        },
        {
            "id": ids[3],
            "event": "failed",
            "state": "pending",
            "attempts": 1,
            "max_attempts": 5,
            "last_result": None,
            "delivered_at": None,
        },
    ]
    api.on(
        "GET",
        f"/api/v1/environments/dev/runs/{_ID}",
        json={**_run("failed"), "job": "daily", "notifications": notifications},
    )
    out = _plain(invoke(make_state(tmp_path, api), "runs", "show", _ID, "--env", "dev").stdout)
    assert "Notifications" in out
    assert f"failed {ids[0]} failed: the server answered 404" in out
    assert f"recovered {ids[1]} retrying (attempt 2 of 5); last: timeout" in out
    assert f"failed {ids[2]} delivered" in out
    assert f"failed {ids[3]} sending (attempt 1 of 5)" in out


def test_runs_show_of_a_run_without_notifications_prints_no_section(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("GET", f"/api/v1/environments/dev/runs/{_ID}", json=_run("succeeded"))
    out = invoke(make_state(tmp_path, api), "runs", "show", _ID, "--env", "dev").stdout
    assert "Notifications" not in out


def test_notify_test_as_json_that_failed_still_exits_1_and_prints_only_json(
    tmp_path: Path,
) -> None:
    api = _api_with_answers(_delivery("failed", attempts=1, last_result="the server answered 404"))
    result = _notify_test(tmp_path, api, "--output", "json")
    assert result.exit_code == 1
    assert json.loads(result.stdout)["last_result"] == "the server answered 404"
    assert result.stderr == ""


def test_notification_that_no_worker_has_tried_yet_reads_queued(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("GET", _NOTIFICATIONS, json=_delivery("pending"))
    result = invoke(make_state(tmp_path, api), "job", "notification", _DELIVERY, "--env", "dev")
    assert "queued" in _plain(result.stdout)


@pytest.mark.parametrize("width", ["80", "120"])
def test_a_trigger_never_splits_across_lines(tmp_path: Path, width: str) -> None:
    api = FakeApi()
    api.on("GET", _JOBS, json=[_job(), _job("nightly_export")])
    result = invoke(
        make_state(tmp_path, api), "job", "list", "--env", "dev", env={"COLUMNS": width}
    )
    lines = result.stdout.splitlines()
    assert all(len(line) <= int(width) for line in lines)
    cells = [part.strip() for line in lines for part in line.split("│")]
    if width == "120":
        assert "on demand" in cells
        assert "schedule 0 3 * * * Europe/Berlin" in cells
    else:
        assert "Not shown: repository, last_run, triggers." in result.stdout
