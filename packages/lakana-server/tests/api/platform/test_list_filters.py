"""Filters and cursors of `GET .../runs`, `.../queries` and `.../deploys`."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from lakana_core.models import DeployRun, QueryRun, QueryRunStatus, Run, RunStatus, RunTrigger
from lakana_server.api.platform.listing import NEXT_CURSOR_HEADER
from tests.api.platform.test_deployments_run_as import _deploy
from tests.api.platform.test_deploys import _setup as _deploys_setup
from tests.api.platform.test_queries import _environment, _person, _submit
from tests.conftest import AUTH_HEADER

_RUNS = "/api/v1/environments/dev/runs"
_QUERIES = "/api/v1/environments/dev/queries"
_DEPLOYS = "/api/v1/environments/dev/deploys"
_T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


@dataclass(frozen=True)
class _Spec:
    """What a test run should look like.

    Parameters
    ----------
    minutes
        Minutes after `_T0` it was created.
    status
        Its status.
    trigger
        What started it.
    job
        The job it belongs to.
    """

    minutes: int = 0
    status: RunStatus = RunStatus.SUCCEEDED
    trigger: RunTrigger | None = None
    job: str | None = None


def _make_runs(client: TestClient, specs: list[_Spec]) -> list[str]:
    """Start one run per spec and give each its status, time, trigger and job.

    Parameters
    ----------
    client
        The test client.
    specs
        One per run.

    Returns
    -------
    list of str
        The run ids, in the order of `specs`.
    """
    ids: list[str] = []
    for spec in specs:
        started = client.post(_RUNS, headers=AUTH_HEADER, json={"repository": "analytics"})
        assert started.status_code == 202, started.text
        run_id = started.json()["id"]
        with Session(client.app.state.engine) as session:
            run = session.get(Run, uuid.UUID(run_id))
            assert run is not None
            run.created_at = _T0 + timedelta(minutes=spec.minutes)
            run.status = spec.status
            run.trigger = spec.trigger
            run.job = spec.job
            session.commit()
        ids.append(run_id)
    return ids


def test_runs_filter_by_status_trigger_job_and_since(client: TestClient) -> None:
    _deploy(client)
    old, bad, gone = _make_runs(
        client,
        [
            _Spec(minutes=1, trigger=RunTrigger.SCHEDULE, job="nightly"),
            _Spec(minutes=60, status=RunStatus.FAILED, trigger=RunTrigger.API, job="x"),
            _Spec(minutes=120, status=RunStatus.CANCELLED),
        ],
    )

    def ids(**params: object) -> list[str]:
        response = client.get(_RUNS, headers=AUTH_HEADER, params=params)
        assert response.status_code == 200, response.text
        return [r["id"] for r in response.json()]

    assert ids(status="failed") == [bad]
    assert ids(status="failed,cancelled") == [gone, bad]
    assert ids(status=["failed", "cancelled"]) == [gone, bad]
    assert ids(trigger="schedule") == [old]
    assert ids(job="nightly") == [old]
    assert ids(since="2026-10-01T13:00:00Z") == [gone, bad]
    assert ids(since="2026-10-01T15:00:00+02:00") == [gone, bad]
    assert ids(status="failed", trigger="api", job="x", since="2026-10-01T00:00:00Z") == [bad]
    assert ids(status="failed", job="nightly") == []


def test_runs_filter_by_the_trigger_deploy(client: TestClient) -> None:
    _deploy(client)
    by_deploy, _ = _make_runs(
        client,
        [
            _Spec(minutes=1, trigger=RunTrigger.DEPLOY, job="refresh"),
            _Spec(minutes=2, trigger=RunTrigger.MANUAL, job="refresh"),
        ],
    )
    response = client.get(_RUNS, headers=AUTH_HEADER, params={"trigger": "deploy"})
    assert response.status_code == 200, response.text
    assert [(r["id"], r["trigger"]) for r in response.json()] == [(by_deploy, "deploy")]


def test_runs_page_with_a_header_cursor(client: TestClient) -> None:
    _deploy(client)
    made = _make_runs(client, [_Spec(minutes=5)] * 3 + [_Spec(minutes=1), _Spec(minutes=9)])
    seen: list[str] = []
    headers = []
    params: dict[str, object] = {"limit": 2}
    while True:
        response = client.get(_RUNS, headers=AUTH_HEADER, params=params)
        assert response.status_code == 200, response.text
        seen += [r["id"] for r in response.json()]
        cursor = response.headers.get(NEXT_CURSOR_HEADER)
        headers.append(cursor)
        if cursor is None:
            break
        params = {"limit": 2, "cursor": cursor}
    assert sorted(seen) == sorted(made)
    assert seen[0] == made[4]
    assert seen[-1] == made[3]
    assert len(headers) == 3
    assert headers[-1] is None


def test_the_last_page_and_an_unpaged_list_have_no_cursor_header(client: TestClient) -> None:
    _deploy(client)
    _make_runs(client, [_Spec(minutes=1)])
    response = client.get(_RUNS, headers=AUTH_HEADER)
    assert NEXT_CURSOR_HEADER not in response.headers


def test_invalid_run_filters_are_422_and_name_the_parameter(client: TestClient) -> None:
    _deploy(client)
    status = client.get(_RUNS, headers=AUTH_HEADER, params={"status": "done"})
    assert status.status_code == 422
    assert (
        "`status` must be one of queued, running, succeeded, failed, cancelled"
        in (status.json()["detail"])
    )
    trigger = client.get(_RUNS, headers=AUTH_HEADER, params={"trigger": "cron"})
    assert trigger.status_code == 422
    assert "`trigger` must be one of schedule, api, manual" in trigger.json()["detail"]
    naive = client.get(_RUNS, headers=AUTH_HEADER, params={"since": "2026-10-01T00:00:00"})
    assert naive.status_code == 422
    assert "`since` has no time zone" in naive.json()["detail"]
    garbled = client.get(_RUNS, headers=AUTH_HEADER, params={"since": "yesterday"})
    assert garbled.status_code == 422
    assert "since" in str(garbled.json()["detail"])
    cursor = client.get(_RUNS, headers=AUTH_HEADER, params={"cursor": "nonsense"})
    assert cursor.status_code == 422
    assert "Start the listing again" in cursor.json()["detail"]


def test_a_cursor_of_another_list_is_refused_on_every_list(client: TestClient) -> None:
    _deploy(client)
    _make_runs(client, [_Spec(minutes=1), _Spec(minutes=2)])
    runs = client.get(_RUNS, headers=AUTH_HEADER, params={"limit": 1})
    cursor = runs.headers[NEXT_CURSOR_HEADER]
    for url in (_QUERIES, _DEPLOYS):
        refused = client.get(url, headers=AUTH_HEADER, params={"cursor": cursor})
        assert refused.status_code == 422, url
        assert "came from the `runs` list" in refused.json()["detail"]
    checks = client.get(
        "/api/v1/environments/dev/checks", headers=AUTH_HEADER, params={"cursor": cursor}
    )
    assert checks.status_code == 422


def test_queries_filter_by_status_and_submitter_and_page(client: TestClient) -> None:
    _environment(client)
    bob = _person(client, "bob", "ANALYST")
    mine = _submit(client)
    theirs = _submit(client, bob)
    done = _submit(client)
    with Session(client.app.state.engine) as session:
        run = session.get(QueryRun, uuid.UUID(done))
        assert run is not None
        run.status = QueryRunStatus.FAILED
        run.finished_at = datetime.now(UTC)
        session.commit()

    def ids(headers: dict[str, str] = AUTH_HEADER, **params: object) -> list[str]:
        response = client.get(_QUERIES, headers=headers, params=params)
        assert response.status_code == 200, response.text
        return [r["id"] for r in response.json()]

    assert sorted(ids(status="queued")) == sorted([mine, theirs])
    assert ids(status="failed") == [done]
    assert ids(submitter="bob") == [theirs]
    assert ids(submitter="bob", status="failed") == []
    # bob is not a manager: the filter never shows the admin's queries
    assert ids(bob, submitter="admin") == []
    assert ids(bob, submitter="bob") == [theirs]
    paged: list[str] = []
    params: dict[str, object] = {"limit": 1}
    while True:
        response = client.get(_QUERIES, headers=AUTH_HEADER, params=params)
        paged += [r["id"] for r in response.json()]
        cursor = response.headers.get(NEXT_CURSOR_HEADER)
        if cursor is None:
            break
        params = {"limit": 1, "cursor": cursor}
    assert sorted(paged) == sorted([mine, theirs])
    bad = client.get(_QUERIES, headers=AUTH_HEADER, params={"status": "stuck"})
    assert bad.status_code == 422
    assert (
        "`status` must be one of QUEUED, RUNNING, SUCCEEDED, FAILED, CANCELLED"
        in (bad.json()["detail"])
    )


def test_deploys_filter_by_repository_and_page(client: TestClient) -> None:
    _deploys_setup(client)
    client.post(
        "/api/v1/repositories",
        headers=AUTH_HEADER,
        json={"name": "ml", "kind": "https", "url": "https://github.com/acme/ml.git"},
    )
    made: dict[str, list[str]] = {"analytics": [], "ml": []}
    for n, repository in enumerate(["analytics", "ml", "analytics", "analytics"]):
        accepted = client.post(_DEPLOYS, headers=AUTH_HEADER, json={"repository": repository})
        assert accepted.status_code == 202, accepted.text
        deploy_id = accepted.json()["id"]
        with Session(client.app.state.engine) as session:
            row = session.get(DeployRun, uuid.UUID(deploy_id))
            assert row is not None
            row.created_at = _T0 + timedelta(minutes=n)
            session.commit()
        made[repository].append(deploy_id)

    only = client.get(_DEPLOYS, headers=AUTH_HEADER, params={"repository": "ml"}).json()
    assert [d["id"] for d in only] == made["ml"]
    seen: list[str] = []
    params: dict[str, object] = {"repository": "analytics", "limit": 2}
    while True:
        response = client.get(_DEPLOYS, headers=AUTH_HEADER, params=params)
        seen += [d["id"] for d in response.json()]
        cursor = response.headers.get(NEXT_CURSOR_HEADER)
        if cursor is None:
            break
        params["cursor"] = cursor
    assert seen == list(reversed(made["analytics"]))
    ghost = client.get(_DEPLOYS, headers=AUTH_HEADER, params={"repository": "ghost"})
    assert ghost.status_code == 404
    assert client.get(_DEPLOYS, headers=AUTH_HEADER, params={"limit": "201"}).status_code == 422
