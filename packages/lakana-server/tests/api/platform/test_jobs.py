"""Jobs over REST: list, start (201 or 202), pause and resume."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.models import (
    EnvironmentDeployment,
    JobSchedule,
    JobVersion,
    NotificationDelivery,
    NotificationState,
    Run,
)
from tests.api.platform.test_deploys import _insert_job_version, _setup, _token_for
from tests.conftest import AUTH_HEADER
from tests.server_test_helpers import create_environment

_ENV = "/api/v1/environments/dev"
_BODY = {"repository": "analytics"}


def _deploy_jobs(client: TestClient, notifying: bool = False, on_deploy: bool = False) -> None:
    """Create `dev`, the repository and a deployment whose manifest declares two jobs.

    Parameters
    ----------
    client
        The test client.
    notifying
        Add a third job, `alerting`, that notifies a webhook.
    on_deploy
        Make `daily` and `manual` start after each deploy.
    """
    _setup(client)
    jobs = {
        "daily": {
            "select": ["silver.*"],
            "upstream": False,
            "downstream": False,
            "schedule": {"cron": "0 3 * * *", "timezone": "Europe/Berlin"},
        },
        "manual": {
            "select": ["silver.*"],
            "upstream": False,
            "downstream": False,
            "schedule": None,
        },
    }
    if on_deploy:
        for job in jobs.values():
            job["on_deploy"] = True
    if notifying:
        jobs["alerting"] = {
            "select": ["silver.*"],
            "upstream": False,
            "downstream": False,
            "schedule": None,
            "notify": {"webhook": "team-alerts", "format": "slack"},
        }
    version_id = _insert_job_version(
        client,
        "dev",
        {
            "version": 6 if notifying else 5,
            "assets": [{"table": "silver.clean", "sql": "SELECT 1", "references": []}],
            "edges": [],
            "external_inputs": [],
            "jobs": jobs,
        },
    )
    with Session(client.app.state.engine) as session:
        version = session.get(JobVersion, version_id)
        assert version is not None
        session.add(
            EnvironmentDeployment(
                environment_id=version.environment_id,
                repository_id=version.repository_id,
                current_job_version_id=version.id,
            )
        )
        for name in jobs:
            session.add(
                JobSchedule(
                    id=uuid.uuid4(),
                    environment_id=version.environment_id,
                    repository_id=version.repository_id,
                    job=name,
                )
            )
        session.commit()


def _grant(client: TestClient, name: str, action: str) -> dict[str, str]:
    """Create a service account holding one action on `dev`.

    Parameters
    ----------
    client
        The test client.
    name
        The account's name.
    action
        The action to grant.

    Returns
    -------
    dict of str to str
        Its auth header.
    """
    header = _token_for(client, name)
    granted = client.post(
        "/api/v1/grants",
        headers=AUTH_HEADER,
        json={"principal": name, "action": action, "kind": "ENVIRONMENT", "environment": "dev"},
    )
    assert granted.status_code in (200, 201), granted.text
    return header


def test_list_shows_the_jobs_and_their_state(client: TestClient) -> None:
    """Each job has its selection, schedule, zone, next time in both zones and state."""
    _deploy_jobs(client)
    listed = client.get(f"{_ENV}/jobs", headers=AUTH_HEADER, params={"repository": "analytics"})
    assert listed.status_code == 200, listed.text
    by_name = {j["name"]: j for j in listed.json()}
    assert set(by_name) == {"daily", "manual"}
    daily = by_name["daily"]
    assert (daily["cron"], daily["timezone"], daily["select"]) == (
        "0 3 * * *",
        "Europe/Berlin",
        ["silver.*"],
    )
    assert daily["state"] == "no run principal"
    assert daily["waiting_for"]["code"] == "no_run_principal"
    assert by_name["manual"]["state"] == "no schedule"
    assert by_name["manual"]["next_run_at"] is None
    assert client.get(f"{_ENV}/jobs", headers=_token_for(client, "nosy")).status_code == 403
    runner = _grant(client, "runner_only", "RUN")
    assert client.get(f"{_ENV}/jobs", headers=runner).status_code == 403


def test_start_is_201_then_202_while_the_project_is_busy(client: TestClient) -> None:
    """The first start gives the run; the second waits for it and shows in the list."""
    _deploy_jobs(client)
    first = client.post(f"{_ENV}/jobs/manual/runs", headers=AUTH_HEADER, json=_BODY)
    assert first.status_code == 201, first.text
    run = first.json()["run"]
    assert first.json()["waiting"] is None
    assert (run["job"], run["trigger"], run["status"]) == ("manual", "manual", "queued")
    second = client.post(f"{_ENV}/jobs/daily/runs", headers=AUTH_HEADER, json=_BODY)
    assert second.status_code == 202, second.text
    waiting = second.json()["waiting"]
    assert second.json()["run"] is None
    assert waiting["waits_for_run_id"] == run["id"]
    assert (waiting["merged"], waiting["job"]) == (1, "daily")
    assert f"lakana runs show {run['id']} --env dev" in waiting["message"]
    listed = {j["name"]: j for j in client.get(f"{_ENV}/jobs", headers=AUTH_HEADER).json()}
    assert listed["daily"]["state"] == "waiting"
    assert listed["daily"]["waits_for_run_id"] == run["id"]
    assert listed["manual"]["last_run_id"] == run["id"]
    shown = client.get(f"{_ENV}/runs/{run['id']}", headers=AUTH_HEADER).json()
    assert (shown["job"], shown["trigger"], shown["scheduled_for"], shown["missed"]) == (
        "manual",
        "manual",
        None,
        0,
    )
    adhoc = client.post(f"{_ENV}/runs", headers=AUTH_HEADER, json=_BODY)
    assert adhoc.status_code == 409
    assert adhoc.json()["type"] == "ConflictError"


def test_a_service_account_start_has_the_trigger_api(client: TestClient) -> None:
    """The trigger follows the caller's principal type."""
    _deploy_jobs(client)
    header = _grant(client, "ci", "RUN")
    started = client.post(f"{_ENV}/jobs/manual/runs", headers=header, json=_BODY)
    assert started.status_code == 201, started.text
    assert started.json()["run"]["trigger"] == "api"


def test_request_errors(client: TestClient) -> None:
    """Unknown job 404 with the names, bad body 422, no RUN 403, nothing to pause 409."""
    _deploy_jobs(client)
    unknown = client.post(f"{_ENV}/jobs/nope/runs", headers=AUTH_HEADER, json=_BODY)
    assert unknown.status_code == 404
    assert "daily, manual" in unknown.json()["detail"]
    for body in ({}, {"repository": ""}, {"repository": "analytics", "extra": 1}):
        assert (
            client.post(f"{_ENV}/jobs/manual/runs", headers=AUTH_HEADER, json=body).status_code
            == 422
        )
    outsider = _token_for(client, "outsider")
    assert client.post(f"{_ENV}/jobs/manual/runs", headers=outsider, json=_BODY).status_code == 403
    assert client.post(f"{_ENV}/jobs/manual/runs", json=_BODY).status_code == 401
    assert (
        client.post(f"{_ENV}/jobs/manual/pause", headers=AUTH_HEADER, json=_BODY).status_code == 409
    )


def test_pause_and_resume_need_deploy(client: TestClient) -> None:
    """RUN alone cannot pause; DEPLOY can; both show in the list and the audit log."""
    _deploy_jobs(client)
    runner = _grant(client, "runner", "RUN")
    assert client.post(f"{_ENV}/jobs/daily/pause", headers=runner, json=_BODY).status_code == 403
    assert client.post(f"{_ENV}/jobs/daily/resume", headers=runner, json=_BODY).status_code == 403
    deployer = _grant(client, "deployer", "DEPLOY")
    paused = client.post(f"{_ENV}/jobs/daily/pause", headers=deployer, json=_BODY)
    assert paused.status_code == 200, paused.text
    assert (paused.json()["paused"], paused.json()["state"]) == (True, "paused")
    with Session(client.app.state.engine) as session:
        row = session.scalars(select(JobSchedule).where(JobSchedule.job == "daily")).one()
        assert row.paused and row.paused_by is not None
    resumed = client.post(f"{_ENV}/jobs/daily/resume", headers=deployer, json=_BODY)
    assert resumed.status_code == 200
    assert resumed.json()["paused"] is False
    assert resumed.json()["next_run_at"] is not None
    events = client.get("/api/v1/audit-events", headers=AUTH_HEADER, params={"limit": 50})
    assert events.status_code == 200, events.text
    types = {e["event_type"] for e in events.json()["items"]}
    assert {"JOB_PAUSED", "JOB_RESUMED"} <= types


def test_notify_test_needs_deploy_and_answers_202_with_the_delivery(client: TestClient) -> None:
    """RUN alone cannot test a webhook; DEPLOY queues a row and can read its state."""
    _deploy_jobs(client, notifying=True)
    url = f"{_ENV}/jobs/alerting/notify-test"
    assert client.post(url, json=_BODY).status_code == 401
    runner = _grant(client, "runner", "RUN")
    assert client.post(url, headers=runner, json=_BODY).status_code == 403
    deployer = _grant(client, "deployer", "DEPLOY")
    queued = client.post(url, headers=deployer, json=_BODY)
    assert queued.status_code == 202, queued.text
    delivery_id = queued.json()["delivery_id"]
    shown = client.get(f"{_ENV}/notifications/{delivery_id}", headers=deployer)
    assert shown.status_code == 200, shown.text
    assert shown.json() == {
        "id": delivery_id,
        "job": "alerting",
        "repository": "analytics",
        "event": "test",
        "state": "pending",
        "attempts": 0,
        "max_attempts": 5,
        "last_result": None,
        "delivered_at": None,
        "next_attempt_at": shown.json()["next_attempt_at"],
    }
    assert shown.json()["next_attempt_at"] is not None
    reader = _grant(client, "reader", "READ_ENVIRONMENT")
    assert client.get(f"{_ENV}/notifications/{delivery_id}", headers=reader).status_code == 200
    outsider = _token_for(client, "outsider")
    assert client.get(f"{_ENV}/notifications/{delivery_id}", headers=outsider).status_code == 403
    missing = client.get(f"{_ENV}/notifications/{uuid.uuid4()}", headers=deployer)
    assert missing.status_code == 404


def test_a_delivery_is_not_readable_through_another_environment(client: TestClient) -> None:
    """A reader of `prod` alone gets a 404 for a delivery queued in `dev`, not its state."""
    _deploy_jobs(client, notifying=True)
    queued = client.post(f"{_ENV}/jobs/alerting/notify-test", headers=AUTH_HEADER, json=_BODY)
    delivery_id = queued.json()["delivery_id"]
    create_environment(client, "prod")
    reader = _token_for(client, "prod_reader")
    granted = client.post(
        "/api/v1/grants",
        headers=AUTH_HEADER,
        json={
            "principal": "prod_reader",
            "action": "READ_ENVIRONMENT",
            "kind": "ENVIRONMENT",
            "environment": "prod",
        },
    )
    assert granted.status_code in (200, 201), granted.text
    other = f"/api/v1/environments/prod/notifications/{delivery_id}"
    for headers in (reader, AUTH_HEADER):
        refused = client.get(other, headers=headers)
        assert refused.status_code == 404, refused.text
        assert "alerting" not in refused.text
    assert client.get(f"{_ENV}/notifications/{delivery_id}", headers=AUTH_HEADER).status_code == 200


def test_notify_test_errors_name_the_next_step(client: TestClient) -> None:
    """An unknown job lists the jobs; a job without `notify:` is a 409 with the fix."""
    _deploy_jobs(client, notifying=True)
    unknown = client.post(f"{_ENV}/jobs/nope/notify-test", headers=AUTH_HEADER, json=_BODY)
    assert unknown.status_code == 404
    assert "alerting, daily, manual" in unknown.json()["detail"]
    silent = client.post(f"{_ENV}/jobs/daily/notify-test", headers=AUTH_HEADER, json=_BODY)
    assert silent.status_code == 409
    assert (
        "Job `daily` has no usable `notify:` in `dev`; add or fix it in lakana.yaml and deploy."
        in silent.json()["detail"]
    )
    bad = client.post(f"{_ENV}/jobs/alerting/notify-test", headers=AUTH_HEADER, json={})
    assert bad.status_code == 422


def test_a_run_shows_its_notifications_to_whoever_may_read_it(client: TestClient) -> None:
    """`notifications` is on the run; a caller who may not read the run gets a 403, no rows."""
    _deploy_jobs(client, notifying=True)
    started = client.post(f"{_ENV}/jobs/alerting/runs", headers=AUTH_HEADER, json=_BODY)
    run = started.json()["run"]
    assert client.get(f"{_ENV}/runs/{run['id']}", headers=AUTH_HEADER).json()["notifications"] == []
    run_delivery_id = uuid.uuid4()
    with Session(client.app.state.engine) as session:
        row = session.get(Run, uuid.UUID(run["id"]))
        assert row is not None
        session.add(
            NotificationDelivery(
                id=run_delivery_id,
                run_id=row.id,
                environment_id=row.environment_id,
                repository_id=row.repository_id,
                job="alerting",
                event="failed",
                format="slack",
                secret_name="team-alerts",
                state=NotificationState.PENDING,
                attempts=2,
                last_result="the server answered 503",
            )
        )
        session.commit()
    shown = client.get(f"{_ENV}/runs/{run['id']}", headers=AUTH_HEADER).json()
    assert shown["notifications"] == [
        {
            "id": str(run_delivery_id),
            "event": "failed",
            "state": "pending",
            "attempts": 2,
            "max_attempts": 5,
            "last_result": "the server answered 503",
            "delivered_at": None,
        }
    ]
    outsider = _token_for(client, "outsider")
    stranger = client.get(f"{_ENV}/runs/{run['id']}", headers=outsider)
    assert stranger.status_code == 403 and "notifications" not in stranger.text


def _set_schedule_row(client: TestClient, job: str, **fields: object) -> None:
    """Change a job's schedule row.

    Parameters
    ----------
    client
        The test client.
    job
        The job.
    **fields
        Columns of the row to set.
    """
    with Session(client.app.state.engine) as session:
        row = session.scalars(select(JobSchedule).where(JobSchedule.job == job)).one()
        for key, value in fields.items():
            setattr(row, key, value)
        session.commit()


def test_a_scheduled_job_lists_its_schedule_then_on_demand(client: TestClient) -> None:
    """A schedule carries its cron, zone, pause state and next time; every job can be started."""
    _deploy_jobs(client)
    _set_schedule_row(client, "daily", next_fire_at=datetime(2026, 7, 1, 1, 0, tzinfo=UTC))
    by_name = {j["name"]: j for j in client.get(f"{_ENV}/jobs", headers=AUTH_HEADER).json()}
    schedule, on_demand = by_name["daily"]["triggers"]
    assert schedule["kind"] == "schedule"
    assert (schedule["cron"], schedule["timezone"], schedule["paused"]) == (
        "0 3 * * *",
        "Europe/Berlin",
        False,
    )
    assert schedule["next_run_at"].startswith("2026-07-01T01:00:00")
    assert on_demand == {"kind": "on_demand"}


def test_a_paused_schedule_has_no_next_run(client: TestClient) -> None:
    """Paused: `paused` is true and `next_run_at` is null."""
    _deploy_jobs(client)
    _set_schedule_row(client, "daily", paused=True, next_fire_at=datetime(2026, 7, 1, tzinfo=UTC))
    daily = client.get(f"{_ENV}/jobs/daily", headers=AUTH_HEADER).json()
    schedule = daily["triggers"][0]
    assert (schedule["kind"], schedule["paused"], schedule["next_run_at"]) == (
        "schedule",
        True,
        None,
    )


def test_a_job_without_a_schedule_has_only_on_demand(client: TestClient) -> None:
    """No schedule here: the list holds just the on-demand trigger."""
    _deploy_jobs(client)
    manual = client.get(f"{_ENV}/jobs/manual", headers=AUTH_HEADER).json()
    assert manual["triggers"] == [{"kind": "on_demand"}]


def test_a_job_that_starts_after_each_deploy_lists_the_deploy_trigger(client: TestClient) -> None:
    """The deploy trigger comes after a schedule and before on demand, which is always last."""
    _deploy_jobs(client, on_deploy=True)
    by_name = {j["name"]: j for j in client.get(f"{_ENV}/jobs", headers=AUTH_HEADER).json()}
    assert [t["kind"] for t in by_name["daily"]["triggers"]] == ["schedule", "deploy", "on_demand"]
    assert by_name["manual"]["triggers"] == [{"kind": "deploy"}, {"kind": "on_demand"}]
    assert "on_deploy" not in by_name["manual"]


def test_the_list_and_the_detail_agree(client: TestClient) -> None:
    """`GET /jobs/{job}` answers the same body as its entry in `GET /jobs`."""
    _deploy_jobs(client)
    _set_schedule_row(client, "daily", next_fire_at=datetime(2026, 7, 1, 1, 0, tzinfo=UTC))
    listed = {j["name"]: j for j in client.get(f"{_ENV}/jobs", headers=AUTH_HEADER).json()}
    for name in ("daily", "manual"):
        shown = client.get(f"{_ENV}/jobs/{name}", headers=AUTH_HEADER)
        assert shown.status_code == 200, shown.text
        assert shown.json() == listed[name]


def test_the_detail_refuses_what_it_cannot_show(client: TestClient) -> None:
    """Unknown job 404 with the names, no read right 403, a wrong repository 404."""
    _deploy_jobs(client)
    unknown = client.get(f"{_ENV}/jobs/nope", headers=AUTH_HEADER)
    assert unknown.status_code == 404
    assert "daily, manual" in unknown.json()["detail"]
    other = client.get(f"{_ENV}/jobs/daily", headers=AUTH_HEADER, params={"repository": "nope"})
    assert other.status_code == 404
    assert client.get(f"{_ENV}/jobs/daily", headers=_token_for(client, "nosy2")).status_code == 403
    assert client.get(f"{_ENV}/jobs/daily").status_code == 401
    scoped = client.get(
        f"{_ENV}/jobs/daily", headers=AUTH_HEADER, params={"repository": "analytics"}
    )
    assert scoped.status_code == 200


def test_pause_answers_with_the_triggers(client: TestClient) -> None:
    """The pause body is a `JobOut` too, so it carries the triggers."""
    _deploy_jobs(client)
    deployer = _grant(client, "deployer2", "DEPLOY")
    paused = client.post(f"{_ENV}/jobs/daily/pause", headers=deployer, json=_BODY).json()
    assert paused["triggers"][0]["paused"] is True
