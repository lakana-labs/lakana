"""Filters and cursors of `GET .../runs`, `.../queries` and `.../deploys`."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from lakana_core.models import (
    DeployRun,
    Principal,
    PrincipalType,
    QueryRun,
    QueryRunStatus,
    Run,
    RunStatus,
    RunTrigger,
)
from lakana_server.api.platform.listing import NEXT_CURSOR_HEADER
from tests.api.platform.test_deployments_run_as import _deploy
from tests.api.platform.test_deploys import _setup as _deploys_setup
from tests.api.platform.test_queries import _environment, _submit
from tests.conftest import AUTH_HEADER
from tests.server_test_helpers import person

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
    bob = person(client, "bob", "ANALYST")
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


_SORTABLE = {
    _RUNS: "created, duration, environment, name, status, tasks, trigger",
    "/api/v1/runs": "created, duration, environment, name, status, tasks, trigger",
    _DEPLOYS: "created, kind, repository, status, who",
    "/api/v1/deploys": "created, kind, repository, status, who",
    "/api/v1/environments/dev/jobs": ("environment, last_run, name, next_run, repository, status"),
    "/api/v1/environments/dev/connections": "created, environment, kind, name, status",
    "/api/v1/connections": "created, environment, kind, name, status",
    "/api/v1/environments/dev/saved-queries": "created, name, updated",
    "/api/v1/saved-queries": "created, name, updated",
    "/api/v1/principals": "created, name, status",
    "/api/v1/audit-events": "event, occurred, on, who",
    "/api/v1/grants": "granted, on, what, who",
    "/api/v1/groups": "created, name",
    "/api/v1/row-filters": "created, on, who",
    "/api/v1/column-masks": "column, created, on, who",
    "/api/v1/tokens": "created, expires, last_used, name, owner",
    "/api/v1/task-tokens": "acts_as, environment, expires, issued, purpose",
    "/api/v1/environments/dev/dropped-tables": "dropped, name, purge",
    "/api/v1/environments/dev/expiring-tables": "expires, name",
    "/api/v1/environments/dev/orphans": "kind, name, repository",
    "/api/v1/environments/dev/queries": "created, status",
    "/api/v1/queries": "created, status",
}
"""Every list a person can sort by a column, with the fields it sorts by."""


@pytest.mark.parametrize(("url", "fields"), list(_SORTABLE.items()), ids=list(_SORTABLE))
def test_every_sortable_list_takes_its_fields_and_refuses_any_other(
    client: TestClient, url: str, fields: str
) -> None:
    """One list a test, so each stays fast: every field both ways, any other field refused.

    Parameters
    ----------
    client
        The test client.
    url
        The list's route.
    fields
        The fields it sorts by, as its refusal names them.
    """
    _deploy(client)
    for field in fields.split(", "):
        for order in ("asc", "desc"):
            ok = client.get(url, headers=AUTH_HEADER, params={"sort": field, "order": order})
            assert ok.status_code == 200, (url, field, ok.text)
    unknown = client.get(url, headers=AUTH_HEADER, params={"sort": "secret_text"})
    assert unknown.status_code == 422, url
    assert unknown.json()["detail"] == (f"`sort` must be one of: {fields}; got 'secret_text'."), url
    direction = client.get(url, headers=AUTH_HEADER, params={"order": "sideways"})
    assert direction.status_code == 422, url
    assert direction.json()["detail"] == "`order` must be asc or desc; got 'sideways'.", url


def test_runs_sorted_by_a_column_page_with_the_header_cursor(client: TestClient) -> None:
    _deploy(client)
    made = _make_runs(
        client,
        [
            _Spec(minutes=1, job="b"),
            _Spec(minutes=2, status=RunStatus.FAILED, job="a"),
            _Spec(minutes=3, job="c"),
        ],
    )
    for url in (_RUNS, "/api/v1/runs"):
        for sort, order, expected in (
            ("created", "asc", made),
            ("name", "desc", [made[2], made[0], made[1]]),
            ("status", "asc", [made[1], made[2], made[0]]),
        ):
            seen: list[str] = []
            params: dict[str, object] = {"limit": 1, "sort": sort, "order": order}
            while True:
                response = client.get(url, headers=AUTH_HEADER, params=params)
                assert response.status_code == 200, response.text
                body = response.json()
                items = body if isinstance(body, list) else body["items"]
                seen += [r["id"] for r in items]
                cursor = (
                    response.headers.get(NEXT_CURSOR_HEADER)
                    if isinstance(body, list)
                    else body["next_cursor"]
                )
                if cursor is None:
                    break
                params["cursor"] = cursor
            assert seen == expected, (url, sort, order)
    first = client.get(_RUNS, headers=AUTH_HEADER, params={"limit": 1, "sort": "name"})
    other = client.get(
        _RUNS,
        headers=AUTH_HEADER,
        params={"cursor": first.headers[NEXT_CURSOR_HEADER], "sort": "status"},
    )
    assert other.status_code == 422
    assert "came from the list sorted by `name:asc`" in other.json()["detail"]


_WIDE = "\U0001d538"
"""A letter of four bytes in UTF-8, the widest a name can hold."""


def _walk_ids(client: TestClient, url: str, headers: dict[str, str], **params: object) -> list[str]:
    """Read every page of a list, one row a page, following its cursor.

    Parameters
    ----------
    client
        The test client.
    url
        The list's route.
    headers
        The caller's Authorization header.
    **params
        The sort and the filters.

    Returns
    -------
    list of str
        Each row's id (a principal's name), in the order of the pages.
    """
    seen: list[str] = []
    query: dict[str, object] = {"limit": 1, **params}
    while True:
        response = client.get(url, headers=headers, params=query)
        assert response.status_code == 200, (url, response.text)
        body = response.json()
        items = body if isinstance(body, list) else body["items"]
        seen += [str(item.get("id", item.get("name"))) for item in items]
        cursor = (
            response.headers.get(NEXT_CURSOR_HEADER)
            if isinstance(body, list)
            else body["next_cursor"]
        )
        if cursor is None:
            return seen
        query["cursor"] = cursor


def test_lists_page_past_names_of_the_longest_length_in_wide_letters(client: TestClient) -> None:
    """Every cursor of a name fits the route's `cursor`: no list stops at a long name (#803)."""
    _deploy(client)
    names = [_WIDE * 255, "ж" * 255, "a" * 255]
    made = _make_runs(client, [_Spec(minutes=m, job=name) for m, name in enumerate(names)])
    for url in (_RUNS, "/api/v1/runs"):
        for sort in ("name", "status", "created"):
            for order in ("asc", "desc"):
                got = _walk_ids(client, url, AUTH_HEADER, sort=sort, order=order)
                assert sorted(got) == sorted(made), (url, sort, order)

    with Session(client.app.state.engine) as session:
        for name in names:
            session.add(Principal(id=uuid.uuid4(), type=PrincipalType.USER, name=name))
        session.commit()
    people = _walk_ids(client, "/api/v1/principals", AUTH_HEADER, sort="name", order="desc")
    assert set(names) <= set(people)

    saved = [_WIDE * 200, "ж" * 200, "S" * 200]
    for name in saved:
        created = client.post(
            "/api/v1/environments/dev/saved-queries",
            headers=AUTH_HEADER,
            json={"name": name, "sql": "SELECT 1"},
        )
        assert created.status_code == 201, created.text
    for url in ("/api/v1/environments/dev/saved-queries", "/api/v1/saved-queries"):
        for params in ({}, {"sort": "name", "order": "desc"}, {"sort": "updated"}):
            assert len(_walk_ids(client, url, AUTH_HEADER, **params)) == 3, (url, params)
