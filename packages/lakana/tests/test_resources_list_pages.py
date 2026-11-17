"""The SDK's filters and cursors on the runs, queries and deploys lists."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest

from lakana._config import ResolvedConfig
from lakana._http import NEXT_CURSOR_HEADER, HttpTransport, csv_param
from lakana.errors import NotFound
from lakana.resources.artifacts import Artifacts
from lakana.resources.deploys import Deploys, OrphanTables
from lakana.resources.namespaces import Namespaces
from lakana.resources.queries import Queries
from lakana.resources.runs import Runs
from lakana.resources.tables import Tables

_CONFIG = ResolvedConfig(url="http://api.example", token="lakana_pat_abc_supersecret")
_RUN: dict[str, Any] = {
    "id": "00000000-0000-0000-0000-0000000000cc",
    "environment": "dev",
    "repository": "analytics",
    "project_version_id": "00000000-0000-0000-0000-0000000000bb",
    "commit_sha": "a" * 40,
    "kind": "full",
    "status": "failed",
    "cancel_requested": False,
    "triggered_by": "admin",
    "run_as": "admin",
    "resumed_from_run_id": None,
    "selection": {"patterns": [], "upstream": False, "downstream": False},
    "error_message": None,
    "created_at": "2026-10-14T12:00:00+00:00",
    "started_at": None,
    "finished_at": None,
    "tasks": [],
}
_ENTRY: dict[str, Any] = {
    "id": "00000000-0000-0000-0000-0000000000dd",
    "submitter": "alice",
    "kind": "read",
    "target": None,
    "status": "QUEUED",
    "created_at": "2026-10-14T12:00:00+00:00",
    "started_at": None,
    "finished_at": None,
    "error_cause": None,
    "waiting_for": None,
}
_DEPLOY: dict[str, Any] = {
    "id": "00000000-0000-0000-0000-0000000000ee",
    "environment": "dev",
    "repository": "analytics",
    "kind": "deploy",
    "status": "succeeded",
    "requested_ref": "main",
    "resolved_sha": "a" * 40,
    "rollback_to_project_version_id": None,
    "error_message": None,
    "diff": None,
    "project_version_id": None,
    "principal_id": None,
    "created_at": "2026-10-14T12:00:00+00:00",
    "started_at": None,
    "finished_at": None,
}


def _transport(
    pages: list[tuple[list[dict[str, Any]], str | None]], seen: list[httpx.Request]
) -> HttpTransport:
    """Build a transport that answers successive requests with successive pages.

    Parameters
    ----------
    pages
        The body and the `X-Next-Cursor` header (or `None`) of each answer; the last repeats.
    seen
        Where to record the requests.

    Returns
    -------
    HttpTransport
        The transport.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        body, cursor = pages[min(len(seen), len(pages) - 1)]
        seen.append(request)
        headers = {} if cursor is None else {NEXT_CURSOR_HEADER: cursor}
        return httpx.Response(200, json=body, headers=headers)

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url=_CONFIG.url)
    return HttpTransport(_CONFIG, http_client=client)


@pytest.mark.parametrize(
    ("values", "joined"),
    [
        (None, None),
        ("failed", "failed"),
        (["failed", "cancelled"], "failed,cancelled"),
        ([], None),
        ("", None),
    ],
)
def test_csv_param_joins_the_values_of_a_filter(
    values: str | list[str] | None, joined: str | None
) -> None:
    assert csv_param(values) == joined


def test_runs_list_sends_every_filter_and_returns_the_runs() -> None:
    seen: list[httpx.Request] = []
    runs = Runs(_transport([([_RUN], "next")], seen))
    since = datetime(2026, 10, 1, 12, 0, tzinfo=timezone(timedelta(hours=2)))
    listed = runs.list(
        "dev",
        repository="analytics",
        status=["failed", "cancelled"],
        trigger="schedule",
        job="nightly",
        since=since,
        limit=5,
        cursor="abc",
    )
    assert [r.id for r in listed] == [_RUN["id"]]
    params = seen[0].url.params
    assert dict(params) == {
        "repository": "analytics",
        "status": "failed,cancelled",
        "trigger": "schedule",
        "job": "nightly",
        "since": "2026-10-01T12:00:00+02:00",
        "limit": "5",
        "cursor": "abc",
    }


def test_runs_list_page_returns_the_cursor_of_the_header() -> None:
    seen: list[httpx.Request] = []
    page = Runs(_transport([([_RUN], "next-one")], seen)).list_page("dev")
    assert (page.next_cursor, len(page.items)) == ("next-one", 1)
    assert dict(seen[0].url.params) == {}
    last = Runs(_transport([([_RUN], None)], [])).list_page("dev")
    assert last.next_cursor is None


def test_runs_iter_follows_the_cursor_until_the_last_page() -> None:
    seen: list[httpx.Request] = []
    runs = Runs(_transport([([_RUN], "c1"), ([_RUN], "c2"), ([_RUN], None)], seen))
    assert len(list(runs.iter("dev", status="failed", page_size=1))) == 3
    assert [r.url.params.get("cursor") for r in seen] == [None, "c1", "c2"]
    assert all(r.url.params["status"] == "failed" for r in seen)
    assert all(r.url.params["limit"] == "1" for r in seen)


def test_queries_list_sends_the_filters_and_page_returns_the_cursor() -> None:
    seen: list[httpx.Request] = []
    queries = Queries(_transport([([_ENTRY], "q-next")], seen))
    entries = queries.list("dev", status="failed", submitter="bob", limit=7, cursor="q0")
    assert [e.id for e in entries] == [_ENTRY["id"]]
    assert dict(seen[0].url.params) == {
        "include_ended": "false",
        "status": "failed",
        "submitter": "bob",
        "limit": "7",
        "cursor": "q0",
    }
    page = queries.list_page("dev", include_ended=True)
    assert (page.next_cursor, seen[1].url.params["include_ended"]) == ("q-next", "true")
    assert "status" not in seen[1].url.params


def test_queries_list_sends_origin_and_since() -> None:
    seen: list[httpx.Request] = []
    queries = Queries(_transport([([_ENTRY], None)], seen))
    since = datetime(2026, 10, 1, 12, 0, tzinfo=timezone(timedelta(hours=2)))
    queries.list("dev", include_ended=True, origin=["written", "task"], since=since)
    params = seen[0].url.params
    assert (params["origin"], params["since"]) == ("written,task", "2026-10-01T12:00:00+02:00")
    entry = queries.list_page("dev", origin="preview").items[0]
    assert seen[1].url.params["origin"] == "preview"
    # an older server sends neither field
    assert (entry.environment, entry.origin) == (None, None)


def test_list_all_sends_the_filters_and_reads_the_cursor_from_the_body() -> None:
    seen: list[httpx.Request] = []
    row = {**_ENTRY, "environment": "prod", "origin": "task"}
    queries = Queries(_object_transport([{"items": [row], "next_cursor": "a1"}], seen))
    since = datetime(2026, 10, 1, 12, 0, tzinfo=timezone(timedelta(hours=2)))
    page = queries.list_all(
        environment="prod",
        include_ended=True,
        status=["failed", "cancelled"],
        submitter="bob",
        origin="task",
        since=since,
        limit=5,
        cursor="a0",
    )
    assert seen[0].url.path == "/api/v1/queries"
    assert dict(seen[0].url.params) == {
        "environment": "prod",
        "include_ended": "true",
        "status": "failed,cancelled",
        "submitter": "bob",
        "origin": "task",
        "since": "2026-10-01T12:00:00+02:00",
        "limit": "5",
        "cursor": "a0",
    }
    assert page.next_cursor == "a1"
    assert [(e.id, e.environment, e.origin) for e in page.items] == [(_ENTRY["id"], "prod", "task")]
    queries.list_all()
    assert dict(seen[1].url.params) == {"include_ended": "false", "limit": "100"}


def test_iter_all_follows_every_page() -> None:
    seen: list[httpx.Request] = []
    bodies = [
        {"items": [_ENTRY], "next_cursor": "a1"},
        {"items": [_ENTRY], "next_cursor": "a2"},
        {"items": [_ENTRY], "next_cursor": None},
    ]
    queries = Queries(_object_transport(bodies, seen))
    assert len(list(queries.iter_all(origin=["written"], page_size=1))) == 3
    assert [r.url.params.get("cursor") for r in seen] == [None, "a1", "a2"]
    assert all(r.url.params["origin"] == "written" for r in seen)
    assert all(r.url.params["limit"] == "1" for r in seen)


def test_query_environments_reads_name_and_protection() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=[{"name": "prod", "protected": True}])

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url=_CONFIG.url)
    found = Queries(HttpTransport(_CONFIG, http_client=client)).environments()
    assert seen[0].url.path == "/api/v1/queries/environments"
    assert [(e.name, e.protected) for e in found] == [("prod", True)]


def _answering(status: int, body: dict[str, Any]) -> Queries:
    """Build the queries resource over a server that answers every request the same.

    Parameters
    ----------
    status
        The status of every answer.
    body
        The JSON body of every answer.

    Returns
    -------
    Queries
        The resource.
    """
    client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(status, json=body)),
        base_url=_CONFIG.url,
    )
    return Queries(HttpTransport(_CONFIG, http_client=client))


_LIST_ALL_TOO_OLD = (
    "This server does not have GET /api/v1/queries; it is older than this lakana package. "
    "Upgrade the server, or list one environment with `lakana query list --env ENV`."
)
_ENVIRONMENTS_TOO_OLD = (
    "This server does not have GET /api/v1/queries/environments; it is older than this lakana "
    "package. Upgrade the server, or see the environments with `lakana env list`."
)


@pytest.mark.parametrize(
    ("call", "message"),
    [
        (lambda queries: queries.list_all(), _LIST_ALL_TOO_OLD),
        (lambda queries: queries.list_all(environment="dev"), _LIST_ALL_TOO_OLD),
        (lambda queries: list(queries.iter_all()), _LIST_ALL_TOO_OLD),
        (lambda queries: queries.environments(), _ENVIRONMENTS_TOO_OLD),
    ],
)
def test_an_older_server_without_the_route_is_named_as_too_old(
    call: Callable[[Queries], object], message: str
) -> None:
    """The router's bare 404 has no Lakana error type: the route is missing, not the object."""
    with pytest.raises(NotFound) as caught:
        call(_answering(404, {"detail": "Not Found"}))
    assert (str(caught.value), caught.value.status, caught.value.error_type) == (
        message,
        404,
        None,
    )


def test_a_lakana_404_of_an_unknown_environment_stays_as_it_is() -> None:
    unknown = {"detail": "Environment 'nope' not found.", "type": "NotFoundError"}
    with pytest.raises(NotFound) as caught:
        _answering(404, unknown).list_all(environment="nope")
    assert (str(caught.value), caught.value.error_type) == (
        "Environment 'nope' not found.",
        "NotFoundError",
    )


def test_deploys_list_sends_the_filter_and_page_returns_the_cursor() -> None:
    seen: list[httpx.Request] = []
    deploys = Deploys(_transport([([_DEPLOY], "d-next")], seen))
    listed = deploys.list("dev", repository="analytics", limit=3, cursor="d0")
    assert [d.id for d in listed] == [_DEPLOY["id"]]
    assert dict(seen[0].url.params) == {"repository": "analytics", "limit": "3", "cursor": "d0"}
    page = deploys.list_page("dev")
    assert (page.next_cursor, dict(seen[1].url.params)) == ("d-next", {})


_DROPPED: dict[str, Any] = {
    "name": "sales",
    "namespace": "gold",
    "uuid": "00000000-0000-0000-0000-0000000000a1",
    "kind": "TABLE",
    "dropped_at": "2026-10-14T12:00:00+00:00",
    "purge_after": "2026-10-21T12:00:00+00:00",
    "restorable": True,
}
_EXPIRING: dict[str, Any] = {
    "name": "t",
    "namespace": "silver",
    "uuid": "00000000-0000-0000-0000-0000000000a2",
    "source": "table",
    "days": 2,
    "on": None,
    "never": False,
    "set_by": "admin",
    "set_at": "2026-10-14T12:00:00+00:00",
    "last_activity_at": "2026-10-14T12:00:00+00:00",
    "expires_at": "2026-10-16T12:00:00+00:00",
    "kept": None,
}


def _object_transport(bodies: list[dict[str, Any]], seen: list[httpx.Request]) -> HttpTransport:
    """Build a transport that answers successive requests with successive JSON objects.

    Parameters
    ----------
    bodies
        The body of each answer; the last repeats.
    seen
        Where to record the requests.

    Returns
    -------
    HttpTransport
        The transport.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        body = bodies[min(len(seen), len(bodies) - 1)]
        seen.append(request)
        return httpx.Response(200, json=body)

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url=_CONFIG.url)
    return HttpTransport(_CONFIG, http_client=client)


def test_list_dropped_page_reads_the_cursor_of_the_body() -> None:
    seen: list[httpx.Request] = []
    tables = Tables(_object_transport([{"items": [_DROPPED], "next_cursor": "d1"}], seen))
    page = tables.list_dropped_page("dev", limit=1, cursor="d0")
    assert (page.next_cursor, [d.name for d in page.items]) == ("d1", ["sales"])
    assert seen[0].url.path == "/api/v1/environments/dev/dropped-tables"
    assert dict(seen[0].url.params) == {"limit": "1", "cursor": "d0"}


def test_iter_dropped_walks_every_page() -> None:
    seen: list[httpx.Request] = []
    bodies = [
        {"items": [_DROPPED], "next_cursor": "d1"},
        {"items": [_DROPPED], "next_cursor": None},
    ]
    tables = Tables(_object_transport(bodies, seen))
    assert len(list(tables.iter_dropped("dev", page_size=1))) == 2
    assert [r.url.params.get("cursor") for r in seen] == [None, "d1"]


def test_expiring_sends_the_namespace_and_pages_by_header() -> None:
    seen: list[httpx.Request] = []
    tables = Tables(_transport([([_EXPIRING], "e1"), ([_EXPIRING], None)], seen))
    page = tables.expiring_page("dev", within_days=30, namespace="silver", limit=1)
    assert (page.next_cursor, len(page.items)) == ("e1", 1)
    assert dict(seen[0].url.params) == {"within_days": "30", "namespace": "silver", "limit": "1"}
    two = Tables(_transport([([_EXPIRING], "e1"), ([_EXPIRING], None)], []))
    assert len(list(two.iter_expiring("dev", namespace="silver", page_size=1))) == 2
    assert len(Tables(_transport([([_EXPIRING], None)], [])).expiring("dev")) == 1


def test_namespaces_iter_sends_parent_on_every_page() -> None:
    seen: list[httpx.Request] = []
    namespace = {"path": "a.b", "properties": {}}
    spaces = Namespaces(_transport([([namespace], "n1"), ([namespace], None)], seen))
    assert [n.path for n in spaces.iter("dev", parent="a", depth=1, page_size=1)] == ["a.b", "a.b"]
    assert all(r.url.params["parent"] == "a" and r.url.params["depth"] == "1" for r in seen)
    assert [r.url.params.get("cursor") for r in seen] == [None, "n1"]
    again = Namespaces(_transport([([namespace], "n1")], []))
    assert again.list_page("dev", parent="a", limit=1).next_cursor == "n1"
    assert again.list("dev")[0].path == "a.b"


_ORPHAN: dict[str, Any] = {"repository": "analytics", "table": "silver.old", "kind": "TABLE"}
_ARTIFACT: dict[str, Any] = {
    "name": "churn",
    "namespace": "models",
    "uuid": "u",
    "kind": "ARTIFACT",
    "current_version": 1,
    "versions": 1,
    "size_bytes": 3,
    "created_at": "2026-10-04T12:00:00Z",
    "defined_by": {"repository": "analytics", "path": "assets/models/churn.py"},
    "keep_versions": 3,
}


def test_orphans_list_says_kind_and_existence_and_reads_the_cursor_of_the_body() -> None:
    seen: list[httpx.Request] = []
    body = {
        "orphans": [{**_ORPHAN, "exists": False}],
        "hidden": 2,
        "next_cursor": "o1",
    }
    found = OrphanTables(_object_transport([body], seen)).list("dev", limit=1, cursor="o0")
    assert (found.next_cursor, found.hidden) == ("o1", 2)
    assert [(o.table, o.kind, o.exists) for o in found.orphans] == [("silver.old", "TABLE", False)]
    assert dict(seen[0].url.params) == {"limit": "1", "cursor": "o0"}


def test_orphans_iter_walks_every_page() -> None:
    seen: list[httpx.Request] = []
    bodies = [
        {"orphans": [{**_ORPHAN, "exists": True}], "hidden": 0, "next_cursor": "o1"},
        {"orphans": [{**_ORPHAN, "exists": True}], "hidden": 0, "next_cursor": None},
    ]
    assert len(list(OrphanTables(_object_transport(bodies, seen)).iter("dev", page_size=1))) == 2
    assert [r.url.params.get("cursor") for r in seen] == [None, "o1"]


def test_artifacts_list_page_sends_the_paging_and_reads_defined_by_and_keep_versions() -> None:
    seen: list[httpx.Request] = []
    artifacts = Artifacts(_transport([([_ARTIFACT], "a1"), ([_ARTIFACT], None)], seen))
    page = artifacts.list_page("dev", "models", limit=1, cursor="a0")
    assert (page.next_cursor, page.items[0].keep_versions) == ("a1", 3)
    assert page.items[0].defined_by is not None
    assert page.items[0].defined_by.path == "assets/models/churn.py"
    assert dict(seen[0].url.params) == {"namespace": "models", "limit": "1", "cursor": "a0"}
    two = Artifacts(_transport([([_ARTIFACT], "a1"), ([_ARTIFACT], None)], []))
    assert len(list(two.iter("dev", page_size=1))) == 2
    plain = {k: v for k, v in _ARTIFACT.items() if k not in ("defined_by", "keep_versions")}
    one = Artifacts(_transport([([plain], None)], [])).list("dev")[0]
    assert (one.defined_by, one.keep_versions) == (None, None)
