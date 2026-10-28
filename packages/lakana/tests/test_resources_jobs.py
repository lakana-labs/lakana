"""The SDK's jobs resource and the job fields of a run."""

from __future__ import annotations

import json
from typing import Any

import httpx

from lakana._config import ResolvedConfig
from lakana._http import HttpTransport
from lakana.resources.jobs import Jobs
from lakana.resources.runs import run_from_json

_CONFIG = ResolvedConfig(url="http://api.example", token="lakana_pat_abc_supersecret")
_RUN: dict[str, Any] = {
    "id": "00000000-0000-0000-0000-0000000000cc",
    "environment": "dev",
    "repository": "analytics",
    "project_version_id": "00000000-0000-0000-0000-0000000000bb",
    "commit_sha": "a" * 40,
    "kind": "selection",
    "status": "queued",
    "cancel_requested": False,
    "triggered_by": "admin",
    "run_as": "admin",
    "resumed_from_run_id": None,
    "selection": {"patterns": ["silver.*"], "upstream": False, "downstream": False},
    "error_message": None,
    "created_at": "2026-10-14T12:00:00+00:00",
    "started_at": None,
    "finished_at": None,
    "tasks": [],
}
_WAITING = {
    "job": "daily",
    "waits_for_run_id": _RUN["id"],
    "trigger": "manual",
    "requested_at": "2026-10-14T12:00:01+00:00",
    "requested_by": "admin",
    "scheduled_for": None,
    "missed": 0,
    "merged": 2,
    "message": "`daily` waits for run X to finish; it starts then.",
}
_JOB: dict[str, Any] = {
    "name": "daily",
    "environment": "dev",
    "repository": "analytics",
    "select": ["silver.*"],
    "upstream": False,
    "downstream": False,
    "cron": "0 3 * * *",
    "timezone": "Europe/Berlin",
    "state": "waiting",
    "paused": False,
    "next_run_at": "2026-10-15T01:00:00+00:00",
    "next_run_local": "2026-10-15T03:00:00+02:00",
    "waiting_for": {"code": "job_waits", "message": "A start waits."},
    "waits_for_run_id": _RUN["id"],
    "last_run_id": None,
    "last_run_status": None,
    "last_run_trigger": None,
    "last_run_at": None,
}


def _jobs(status: int, body: Any, seen: list[httpx.Request] | None = None) -> Jobs:  # ruff: ignore[any-type]
    """Build the resource over a transport that always answers the same.

    Parameters
    ----------
    status
        The status code.
    body
        The JSON the fake server sends.
    seen
        Where to record the requests.

    Returns
    -------
    Jobs
        The resource.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(status, json=body)

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url=_CONFIG.url)
    return Jobs(HttpTransport(_CONFIG, http_client=client))


def test_a_run_from_an_older_server_has_the_job_fields_at_their_defaults() -> None:
    run = run_from_json(_RUN)
    assert (run.job, run.trigger, run.scheduled_for, run.missed) == (None, None, None, 0)
    scheduled = run_from_json(
        {
            **_RUN,
            "job": "daily",
            "trigger": "schedule",
            "scheduled_for": "2026-10-14T03:00:00+00:00",
            "missed": 2,
        }
    )
    assert (scheduled.job, scheduled.trigger, scheduled.missed) == ("daily", "schedule", 2)
    assert scheduled.scheduled_for is not None and scheduled.scheduled_for.hour == 3


def test_start_reads_a_queued_run_and_a_waiting_start() -> None:
    seen: list[httpx.Request] = []
    queued = _jobs(201, {"run": {**_RUN, "job": "daily"}, "waiting": None}, seen).start(
        "dev", "analytics", "daily"
    )
    assert queued.waiting is None and queued.run is not None and queued.run.job == "daily"
    assert (seen[0].method, seen[0].url.path) == (
        "POST",
        "/api/v1/environments/dev/jobs/daily/runs",
    )
    assert json.loads(seen[0].content) == {"repository": "analytics"}
    waiting = _jobs(202, {"run": None, "waiting": _WAITING}).start("dev", "analytics", "daily")
    assert waiting.run is None and waiting.waiting is not None
    assert (waiting.waiting.waits_for_run_id, waiting.waiting.merged) == (_RUN["id"], 2)


def test_list_pause_and_resume_read_a_job() -> None:
    seen: list[httpx.Request] = []
    jobs = _jobs(200, [_JOB], seen).list("dev", repository="analytics")
    assert jobs[0].waiting_for is not None and jobs[0].waiting_for.code == "job_waits"
    assert jobs[0].select == ("silver.*",)
    assert seen[0].url.params["repository"] == "analytics"
    paused = _jobs(200, {**_JOB, "paused": True, "state": "paused"}, seen).pause(
        "dev", "analytics", "daily"
    )
    assert paused.paused and seen[-1].url.path.endswith("/jobs/daily/pause")
    resumed = _jobs(200, _JOB, seen).resume("dev", "analytics", "daily")
    assert resumed.name == "daily" and seen[-1].url.path.endswith("/jobs/daily/resume")


def test_a_run_from_an_older_server_has_no_notifications_and_a_newer_one_has_them() -> None:
    assert run_from_json(_RUN).notifications == []
    shown = run_from_json(
        {
            **_RUN,
            "notifications": [
                {
                    "id": "00000000-0000-0000-0000-0000000000a1",
                    "event": "failed",
                    "state": "pending",
                    "attempts": 2,
                    "max_attempts": 5,
                    "last_result": "the server answered 503",
                    "delivered_at": None,
                },
                {
                    "id": "00000000-0000-0000-0000-0000000000a2",
                    "event": "recovered",
                    "state": "delivered",
                    "attempts": 1,
                    "max_attempts": 5,
                    "last_result": None,
                    "delivered_at": "2026-10-14T12:05:00+00:00",
                },
            ],
        }
    )
    first, second = shown.notifications
    assert (first.event, first.state, first.attempts, first.last_result) == (
        "failed",
        "pending",
        2,
        "the server answered 503",
    )
    assert (first.id, second.id) == (
        "00000000-0000-0000-0000-0000000000a1",
        "00000000-0000-0000-0000-0000000000a2",
    )
    assert first.delivered_at is None and second.delivered_at is not None
    assert second.max_attempts == 5


def test_notify_test_returns_the_delivery_and_delivery_reads_it() -> None:
    seen: list[httpx.Request] = []
    delivery_id = "00000000-0000-0000-0000-0000000000dd"
    queued = _jobs(202, {"delivery_id": delivery_id}, seen).notify_test("dev", "analytics", "daily")
    assert queued == delivery_id
    assert (seen[0].method, seen[0].url.path) == (
        "POST",
        "/api/v1/environments/dev/jobs/daily/notify-test",
    )
    assert json.loads(seen[0].content) == {"repository": "analytics"}
    body = {
        "id": delivery_id,
        "job": "daily",
        "repository": "analytics",
        "event": "test",
        "state": "failed",
        "attempts": 1,
        "max_attempts": 5,
        "last_result": "the address is private",
        "delivered_at": None,
        "next_attempt_at": None,
    }
    shown = _jobs(200, body, seen).delivery("dev", delivery_id)
    assert (shown.id, shown.state, shown.last_result) == (
        delivery_id,
        "failed",
        "the address is private",
    )
    assert shown.next_attempt_at is None
    assert (seen[1].method, seen[1].url.path) == (
        "GET",
        f"/api/v1/environments/dev/notifications/{delivery_id}",
    )


_TRIGGERS = [
    {
        "kind": "schedule",
        "cron": "0 3 * * *",
        "timezone": "Europe/Berlin",
        "paused": False,
        "next_run_at": "2026-10-15T01:00:00+00:00",
    },
    {"kind": "on_demand"},
]


def test_a_job_reads_its_triggers_and_ignores_a_kind_it_does_not_know() -> None:
    body = {**_JOB, "triggers": [*_TRIGGERS, {"kind": "after_deploy", "repository": "x"}]}
    job = _jobs(200, [body]).list("dev")[0]
    schedule, on_demand = job.triggers
    assert (schedule.kind, schedule.cron, schedule.timezone, schedule.paused) == (
        "schedule",
        "0 3 * * *",
        "Europe/Berlin",
        False,
    )
    assert schedule.next_run_at is not None and schedule.next_run_at.hour == 1
    assert (on_demand.kind, on_demand.cron, on_demand.next_run_at) == ("on_demand", None, None)
    assert len(job.triggers) == 2


def test_a_job_reads_the_deploy_trigger() -> None:
    body = {**_JOB, "triggers": [_TRIGGERS[0], {"kind": "deploy"}, _TRIGGERS[1]]}
    job = _jobs(200, [body]).list("dev")[0]
    assert [trigger.kind for trigger in job.triggers] == ["schedule", "deploy", "on_demand"]
    assert (job.triggers[1].cron, job.triggers[1].paused) == (None, None)


def test_a_job_from_an_older_server_has_no_triggers() -> None:
    assert _jobs(200, [_JOB]).list("dev")[0].triggers == ()


def test_get_reads_one_job() -> None:
    seen: list[httpx.Request] = []
    job = _jobs(200, {**_JOB, "triggers": _TRIGGERS}, seen).get("dev", "daily")
    assert job.triggers[1].kind == "on_demand"
    assert seen[0].url.path == "/api/v1/environments/dev/jobs/daily"
    assert "repository" not in seen[0].url.params
    _jobs(200, _JOB, seen).get("dev", "daily", repository="analytics")
    assert seen[-1].url.params["repository"] == "analytics"
