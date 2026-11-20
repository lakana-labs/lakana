"""The SDK's `connections` resource: what it sends and how it reads the answers."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from lakana._config import ResolvedConfig
from lakana._http import HttpTransport
from lakana._models import (
    Connection,
    ConnectionEnvironment,
    DeclaringAsset,
    LastConnectionTest,
    QueuedConnectionTest,
)
from lakana.errors import InvalidResponse, NotFound
from lakana.resources.connections import Connections
from lakana.resources.task_connections import TaskConnections

_CONFIG = ResolvedConfig(url="http://api.example", token="lakana_pat_abc_supersecret")
_VALUE = "s3cr3t-value"
_ANSWER = {
    "name": "crm-db",
    "kind": "postgresql",
    "address": {"host": "db.example.com", "port": 5432, "database": "crm"},
    "settings": {"user": "reader", "tls": "verify-full"},
    "secret": "crm-db-connection",
    "declared_by": [{"repository": "analytics", "asset": "customers"}],
    "created_by": "dana",
    "created_at": "2026-10-23T12:00:00+00:00",
    "updated_at": "2026-10-23T12:00:00+00:00",
}


def _connections(body: Any, sent: list[httpx.Request]) -> Connections:  # ruff: ignore[any-type]
    """Build the resource over a transport that records requests and answers with `body`.

    Parameters
    ----------
    body
        The JSON the fake server sends, or bytes for a body that is not JSON.
    sent
        Receives every request.

    Returns
    -------
    Connections
        The resource.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        if request.method == "DELETE":
            return httpx.Response(204)
        if isinstance(body, bytes):
            return httpx.Response(200, content=body)
        return httpx.Response(200, json=body)

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url=_CONFIG.url)
    return Connections(HttpTransport(_CONFIG, http_client=client))


def test_create_sends_the_fields_and_the_values_only_in_the_body() -> None:
    sent: list[httpx.Request] = []

    made = _connections(_ANSWER, sent).create(
        "prod",
        "crm-db",
        kind="postgresql",
        address={"host": "db.example.com", "database": "crm"},
        settings={"user": "reader"},
        secret_values={"password": _VALUE},
    )

    (request,) = sent
    assert (request.method, request.url.path) == ("POST", "/api/v1/environments/prod/connections")
    assert json.loads(request.content) == {
        "name": "crm-db",
        "kind": "postgresql",
        "address": {"host": "db.example.com", "database": "crm"},
        "settings": {"user": "reader"},
        "secret": None,
        "secret_values": {"password": _VALUE},
    }
    assert _VALUE not in str(request.url)
    assert all(_VALUE not in value for value in request.headers.values())
    assert made.declared_by == [DeclaringAsset(repository="analytics", asset="customers")]
    assert made.created_at.tzinfo is not None
    assert _VALUE not in repr(made)


def test_create_without_settings_sends_an_empty_object() -> None:
    sent: list[httpx.Request] = []
    _connections(_ANSWER, sent).create(
        "prod", "files", kind="directory", address={"root": "r", "path": "p"}
    )
    body = json.loads(sent[0].content)
    assert (body["settings"], body["secret"], body["secret_values"]) == ({}, None, None)


def test_update_sends_only_what_changes() -> None:
    sent: list[httpx.Request] = []
    resource = _connections(_ANSWER, sent)

    resource.update("prod", "crm-db", settings={"user": "loader"})
    resource.update("prod", "crm-db", secret_values={"password": _VALUE}, clear=["ca_certificate"])
    resource.update("prod", "crm-db", secret="crm-pw")

    assert [(r.method, r.url.path) for r in sent] == [
        ("PATCH", "/api/v1/environments/prod/connections/crm-db")
    ] * 3
    assert [json.loads(r.content) for r in sent] == [
        {"settings": {"user": "loader"}},
        {"secret_values": {"password": _VALUE}, "clear": ["ca_certificate"]},
        {"secret": "crm-pw"},
    ]


def test_get_list_and_delete() -> None:
    sent: list[httpx.Request] = []
    assert _connections(_ANSWER, sent).get("prod", "crm-db").secret == "crm-db-connection"
    page = _connections({"items": [_ANSWER], "next_cursor": "abc"}, sent).list(
        "prod", limit=1, cursor="xyz"
    )
    assert ([c.name for c in page.items], page.next_cursor) == (["crm-db"], "abc")
    assert dict(sent[-1].url.params) == {"limit": "1", "cursor": "xyz"}
    assert _connections(_ANSWER, sent).delete("prod", "crm-db") is None
    assert (sent[-1].method, sent[-1].url.path) == (
        "DELETE",
        "/api/v1/environments/prod/connections/crm-db",
    )


def test_the_model_has_no_field_for_a_secret_value() -> None:
    assert {field for field in Connection.__dataclass_fields__} == {
        "name",
        "kind",
        "address",
        "settings",
        "secret",
        "declared_by",
        "created_by",
        "created_at",
        "updated_at",
        "environment",
        "last_test",
        "actions",
        "last_run_failure",
    }


def test_an_answer_with_an_unknown_field_is_read() -> None:
    assert _connections({**_ANSWER, "added_later": 1}, []).get("prod", "crm-db").name == "crm-db"


@pytest.mark.parametrize("field", ["name", "kind", "address", "created_at", "declared_by"])
def test_an_answer_without_a_required_field_is_an_invalid_response(field: str) -> None:
    answer = dict(_ANSWER)
    del answer[field]
    with pytest.raises(InvalidResponse, match="not what a Lakana server sends"):
        _connections(answer, []).get("prod", "crm-db")
    with pytest.raises(InvalidResponse):
        _connections({"items": [answer], "next_cursor": None}, []).list("prod")


@pytest.mark.parametrize("body", [b"<html>", b'"text"', b"[1]", b'{"items": 1}'])
def test_an_answer_that_is_not_a_connection_is_an_invalid_response(body: bytes) -> None:
    with pytest.raises(InvalidResponse):
        _connections(body, []).get("prod", "crm-db")
    with pytest.raises(InvalidResponse):
        _connections(body, []).list("prod")


def _task_connections(body: Any, sent: list[httpx.Request]) -> TaskConnections:  # ruff: ignore[any-type]
    """Build the task resource over a transport that records requests and answers with `body`.

    Parameters
    ----------
    body
        The JSON the fake server sends, or bytes for a body that is not JSON.
    sent
        Receives every request.

    Returns
    -------
    TaskConnections
        The resource.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        if isinstance(body, bytes):
            return httpx.Response(200, content=body)
        return httpx.Response(200, json=body)

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url=_CONFIG.url)
    return TaskConnections(HttpTransport(_CONFIG, http_client=client))


def test_a_task_fetches_its_connection_by_name_and_gets_the_body_as_sent() -> None:
    sent: list[httpx.Request] = []
    body = {"name": "crm-db", "kind": "postgresql", "secret": {"password": _VALUE}}

    answer = _task_connections(body, sent).get("crm-db")

    (request,) = sent
    assert (request.method, request.url.path) == ("GET", "/api/v1/tasks/self/connections/crm-db")
    assert request.content == b""
    assert answer == body


@pytest.mark.parametrize("body", [b"not json", ["crm-db"]], ids=["not-json", "a-list"])
def test_a_task_fetch_whose_answer_is_not_an_object_is_refused(body: Any) -> None:  # ruff: ignore[any-type]
    with pytest.raises(InvalidResponse, match="a connection"):
        _task_connections(body, []).get("crm-db")


_TEST_ID = "6f1c1f0e-0a52-4b7b-9d0e-2f3a4b5c6d7e"
_TEST_PATH = "/api/v1/environments/prod/connections/crm-db/tests"


def _test_body(state: str, outcome: str | None = None, message: str | None = None) -> dict:
    """Build a `ConnectionTestOut` body.

    Parameters
    ----------
    state
        ``queued``, ``running`` or ``done``.
    outcome
        The outcome once done.
    message
        The message once done.

    Returns
    -------
    dict
        The body.
    """
    return {
        "id": _TEST_ID,
        "state": state,
        "outcome": outcome,
        "message": message,
        "created_at": "2026-10-25T12:00:00+00:00",
        "finished_at": "2026-10-25T12:00:03+00:00" if state == "done" else None,
    }


class _Clock:
    """A clock that moves only when the code sleeps; records every sleep."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        """Read the clock.

        Returns
        -------
        float
            Seconds since the start.
        """
        return self.now

    def sleep(self, seconds: float) -> None:
        """Move the clock.

        Parameters
        ----------
        seconds
            How long to sleep.
        """
        self.sleeps.append(seconds)
        self.now += seconds


def _scripted(
    monkeypatch: pytest.MonkeyPatch, posted: dict, polled: list[dict], sent: list[httpx.Request]
) -> tuple[Connections, _Clock]:
    """Build the resource over a transport that answers a POST, then each GET in turn.

    Parameters
    ----------
    monkeypatch
        Replaces the clock.
    posted
        The POST answer.
    polled
        The GET answers; the last repeats.
    sent
        Receives every request.

    Returns
    -------
    tuple
        The resource and its clock.
    """
    clock = _Clock()
    monkeypatch.setattr("lakana.resources.connections.time.sleep", clock.sleep)
    monkeypatch.setattr("lakana.resources.connections.time.monotonic", clock.monotonic)
    answers = iter(polled)
    last = polled[-1]

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        if request.method == "POST":
            return httpx.Response(202, json=posted)
        return httpx.Response(200, json=next(answers, last))

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url=_CONFIG.url)
    return Connections(HttpTransport(_CONFIG, http_client=client)), clock


def test_test_without_waiting_returns_the_queued_test(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[httpx.Request] = []
    connections, clock = _scripted(monkeypatch, _test_body("queued"), [_test_body("queued")], sent)

    test = connections.test("prod", "crm-db", wait=False)

    assert [(r.method, r.url.path) for r in sent] == [("POST", _TEST_PATH)]
    assert (test.id, test.state, test.outcome, test.message) == (_TEST_ID, "queued", None, None)
    assert test.finished_at is None
    assert clock.sleeps == []


def test_test_polls_with_backoff_until_it_is_done(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[httpx.Request] = []
    polled = [
        _test_body("queued"),
        _test_body("running"),
        _test_body("running"),
        _test_body("running"),
        _test_body("done", "refused", "connection refused"),
    ]
    connections, clock = _scripted(monkeypatch, _test_body("queued"), polled, sent)

    test = connections.test("prod", "crm-db")

    assert (test.state, test.outcome, test.message) == ("done", "refused", "connection refused")
    assert test.finished_at is not None
    assert [(r.method, r.url.path) for r in sent] == [
        ("POST", _TEST_PATH),
        *[("GET", f"{_TEST_PATH}/{_TEST_ID}")] * 5,
    ]
    assert clock.sleeps == [0.5, 1.0, 2.0, 2.0, 2.0]


def test_a_test_nobody_took_times_out_naming_the_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    connections, _ = _scripted(monkeypatch, _test_body("queued"), [_test_body("queued")], [])

    with pytest.raises(TimeoutError) as raised:
        connections.test("prod", "crm-db", timeout=60.0)

    assert str(raised.value) == (
        "the test of `crm-db` is still queued after 60s: no worker has taken it. A worker "
        "of either runner runs connection tests: check that one is running (`docker compose "
        "ps worker`, or the `lakana local` terminal). Look again "
        f"with lakana connection test crm-db --env prod --id {_TEST_ID}"
    )


def test_a_test_a_worker_took_times_out_naming_its_log(monkeypatch: pytest.MonkeyPatch) -> None:
    connections, _ = _scripted(monkeypatch, _test_body("queued"), [_test_body("running")], [])

    with pytest.raises(TimeoutError) as raised:
        connections.test("prod", "crm-db", timeout=10.0)

    assert str(raised.value).startswith("the test of `crm-db` is still running after 10s: ")
    assert (
        "read the worker's log (`docker compose logs worker`, or the `lakana local` terminal)"
    ) in str(raised.value)
    assert str(raised.value).endswith(f"--env prod --id {_TEST_ID}")


def test_test_with_an_id_shows_that_test_without_queueing(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[httpx.Request] = []
    done = _test_body("done", "ok", "reachable")
    connections, _ = _scripted(monkeypatch, done, [done], sent)

    test = connections.test("prod", "crm-db", test_id=_TEST_ID, wait=False)

    assert [(r.method, r.url.path) for r in sent] == [("GET", f"{_TEST_PATH}/{_TEST_ID}")]
    assert test.outcome == "ok"


def test_an_odd_test_answer_is_an_invalid_response(monkeypatch: pytest.MonkeyPatch) -> None:
    connections, _ = _scripted(monkeypatch, {"id": _TEST_ID}, [{"id": _TEST_ID}], [])

    with pytest.raises(InvalidResponse):
        connections.test("prod", "crm-db", wait=False)


_LAST_TEST = {
    "state": "done",
    "outcome": "credentials",
    "finished_at": "2026-11-17T08:00:00+00:00",
    "id": "6f1c1f0e-0a52-4b7b-9d0e-2f3a4b5c6d7e",
    "created_at": "2026-11-17T07:59:58+00:00",
    "message": "password authentication failed for user reader",
    "requested_by": "dana",
}


def test_a_connection_reads_environment_last_test_and_actions() -> None:
    answer = {
        **_ANSWER,
        "environment": "prod",
        "last_test": _LAST_TEST,
        "actions": ["test", "update", "delete"],
    }

    connection = _connections(answer, []).get("prod", "crm-db")

    assert connection.environment == "prod"
    assert connection.actions == ("test", "update", "delete")
    assert connection.last_test == LastConnectionTest(
        state="done",
        outcome="credentials",
        finished_at=datetime(2026, 11, 17, 8, 0, tzinfo=UTC),
        id="6f1c1f0e-0a52-4b7b-9d0e-2f3a4b5c6d7e",
        created_at=datetime(2026, 11, 17, 7, 59, 58, tzinfo=UTC),
        message="password authentication failed for user reader",
        requested_by="dana",
    )
    queued = {**answer, "last_test": {"state": "queued", "outcome": None, "finished_at": None}}
    assert _connections(queued, []).get("prod", "crm-db").last_test == LastConnectionTest(
        state="queued", outcome=None, finished_at=None
    )


def test_an_older_servers_connection_reads_them_as_defaults() -> None:
    connection = _connections(_ANSWER, []).get("prod", "crm-db")

    assert (connection.environment, connection.last_test, connection.actions) == (None, None, ())


def test_a_readers_last_test_has_no_detail() -> None:
    reader = {key: _LAST_TEST[key] for key in ("state", "outcome", "finished_at")}
    answer = {**_ANSWER, "environment": "prod", "last_test": reader, "actions": []}

    last = _connections(answer, []).get("prod", "crm-db").last_test

    assert last is not None
    assert (last.state, last.outcome) == ("done", "credentials")
    assert last.message is None and last.requested_by is None
    assert last.id is None and last.created_at is None


@pytest.mark.parametrize(
    "last_test", [[], {"outcome": "ok", "finished_at": None}, {"state": "done", "finished_at": 1}]
)
def test_an_odd_last_test_is_an_invalid_response(last_test: Any) -> None:  # ruff: ignore[any-type]
    with pytest.raises(InvalidResponse):
        _connections({**_ANSWER, "last_test": last_test}, []).get("prod", "crm-db")


def test_list_all_sends_the_environment_and_reads_the_cursor_from_the_body() -> None:
    sent: list[httpx.Request] = []
    answer = {"items": [{**_ANSWER, "environment": "dev"}], "next_cursor": "n1"}
    resource = _connections(answer, sent)

    page = resource.list_all(limit=1, cursor="c0")
    resource.list_all(environment="dev")

    assert [(c.environment, c.name) for c in page.items] == [("dev", "crm-db")]
    assert page.next_cursor == "n1"
    assert [(r.method, r.url.path) for r in sent] == [("GET", "/api/v1/connections")] * 2
    assert [dict(r.url.params) for r in sent] == [
        {"limit": "1", "cursor": "c0"},
        {"environment": "dev"},
    ]


def test_iter_all_follows_every_page() -> None:
    pages = {
        None: {"items": [{**_ANSWER, "environment": "prod"}], "next_cursor": "c1"},
        "c1": {"items": [{**_ANSWER, "name": "files", "environment": "dev"}], "next_cursor": None},
    }
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json=pages[request.url.params.get("cursor")])

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url=_CONFIG.url)
    resource = Connections(HttpTransport(_CONFIG, http_client=client))

    found = [(c.environment, c.name) for c in resource.iter_all(environment="prod", page_size=1)]

    assert found == [("prod", "crm-db"), ("dev", "files")]
    assert [dict(r.url.params) for r in sent] == [
        {"environment": "prod", "limit": "1"},
        {"environment": "prod", "limit": "1", "cursor": "c1"},
    ]


def test_environments_reads_names_and_actions() -> None:
    sent: list[httpx.Request] = []
    answer = [
        {"name": "dev", "actions": ["create", "test"]},
        {"name": "prod", "actions": [], "future": 1},
    ]

    found = _connections(answer, sent).environments()

    assert found == [
        ConnectionEnvironment(name="dev", actions=("create", "test")),
        ConnectionEnvironment(name="prod", actions=()),
    ]
    assert [(r.method, r.url.path) for r in sent] == [("GET", "/api/v1/connections/environments")]


@pytest.mark.parametrize(
    "body",
    [b"{}", b'[{"actions": []}]', b'[{"name": "dev", "actions": "create"}]', b"not json"],
)
def test_an_odd_environments_answer_is_an_invalid_response(body: bytes) -> None:
    with pytest.raises(InvalidResponse):
        _connections(body, []).environments()


def test_an_older_server_without_the_routes_says_to_upgrade() -> None:
    """The router's bare 404 names the route; a Lakana 404 (an unknown environment) stays."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("environment") == "nowhere":
            return httpx.Response(
                404, json={"detail": "No environment named 'nowhere'.", "type": "NotFoundError"}
            )
        return httpx.Response(404, json={"detail": "Not Found"})

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url=_CONFIG.url)
    resource = Connections(HttpTransport(_CONFIG, http_client=client))

    with pytest.raises(NotFound, match=r"does not have GET /api/v1/connections; .* --env ENV"):
        resource.list_all()
    with pytest.raises(NotFound, match=r"GET /api/v1/connections/environments; .* env list"):
        resource.environments()
    with pytest.raises(NotFound, match="No environment named 'nowhere'"):
        resource.list_all(environment="nowhere")


_TEST_ALL_PATH = "/api/v1/environments/prod/connection-tests"


def _queued(connection: str, test_id: str, *, queued: bool = True) -> dict[str, Any]:
    """Build one item of a `ConnectionQueuedTestPage` body.

    Parameters
    ----------
    connection
        The connection's name.
    test_id
        The test's id.
    queued
        Whether the request queued it.

    Returns
    -------
    dict
        The item.
    """
    return {"connection": connection, "test_id": test_id, "queued": queued}


def test_queue_tests_sends_limit_and_cursor() -> None:
    sent: list[httpx.Request] = []
    answer = {
        "items": [_queued("crm-db", "t-1"), {**_queued("files", "t-2", queued=False), "new": 1}],
        "next_cursor": "n1",
    }
    resource = _connections(answer, sent)

    page = resource.queue_tests("prod", limit=2, cursor="c0")
    resource.queue_tests("prod")

    assert page.items == [
        QueuedConnectionTest(connection="crm-db", test_id="t-1", queued=True),
        QueuedConnectionTest(connection="files", test_id="t-2", queued=False),
    ]
    assert page.next_cursor == "n1"
    assert [(r.method, r.url.path) for r in sent] == [("POST", _TEST_ALL_PATH)] * 2
    assert [dict(r.url.params) for r in sent] == [{"limit": "2", "cursor": "c0"}, {}]
    for odd in (b"[]", b'{"items": [{"connection": "a"}], "next_cursor": null}'):
        with pytest.raises(InvalidResponse):
            _connections(odd, []).queue_tests("prod")


def _test_all_api(
    monkeypatch: pytest.MonkeyPatch,
    pages: dict[str | None, dict[str, Any]],
    reads: dict[str, list[dict[str, Any]]],
    sent: list[httpx.Request],
) -> tuple[Connections, _Clock]:
    """Build the resource over a fake Test all: pages of queued tests, then each test's reads.

    Parameters
    ----------
    monkeypatch
        Replaces the clock.
    pages
        The POST answer for each cursor.
    reads
        Each connection's GET answers in turn; the last repeats.
    sent
        Receives every request.

    Returns
    -------
    tuple
        The resource and its clock.
    """
    clock = _Clock()
    monkeypatch.setattr("lakana.resources.connections.time.sleep", clock.sleep)
    monkeypatch.setattr("lakana.resources.connections.time.monotonic", clock.monotonic)
    left = {name: list(answers) for name, answers in reads.items()}

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        if request.method == "POST":
            return httpx.Response(202, json=pages[request.url.params.get("cursor")])
        name = request.url.path.split("/")[6]
        answers = left[name]
        return httpx.Response(200, json=answers.pop(0) if len(answers) > 1 else answers[0])

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url=_CONFIG.url)
    return Connections(HttpTransport(_CONFIG, http_client=client)), clock


def _read(state: str, test_id: str, outcome: str | None = None) -> dict[str, Any]:
    """Build a `ConnectionTestOut` body of a given id.

    Parameters
    ----------
    state
        ``queued``, ``running`` or ``done``.
    test_id
        The test's id.
    outcome
        The outcome once done.

    Returns
    -------
    dict
        The body.
    """
    return {**_test_body(state, outcome, outcome and f"{outcome}!"), "id": test_id}


def test_test_all_walks_the_pages_and_waits_for_each(monkeypatch: pytest.MonkeyPatch) -> None:
    pages: dict[str | None, dict[str, Any]] = {
        None: {"items": [_queued("a", "t-a")], "next_cursor": "c1"},
        "c1": {"items": [_queued("b", "t-b", queued=False)], "next_cursor": None},
    }
    reads = {
        "a": [_read("queued", "t-a"), _read("running", "t-a"), _read("done", "t-a", "ok")],
        "b": [_read("done", "t-b", "refused")],
    }
    sent: list[httpx.Request] = []
    connections, clock = _test_all_api(monkeypatch, pages, reads, sent)

    found = connections.test_all("prod")

    assert {name: (t.id, t.state, t.outcome) for name, t in found.items()} == {
        "a": ("t-a", "done", "ok"),
        "b": ("t-b", "done", "refused"),
    }
    assert [(r.method, r.url.path) for r in sent] == [
        ("POST", _TEST_ALL_PATH),
        ("POST", _TEST_ALL_PATH),
        ("GET", "/api/v1/environments/prod/connections/a/tests/t-a"),
        ("GET", "/api/v1/environments/prod/connections/b/tests/t-b"),
        ("GET", "/api/v1/environments/prod/connections/a/tests/t-a"),
        ("GET", "/api/v1/environments/prod/connections/a/tests/t-a"),
    ]
    assert [dict(r.url.params) for r in sent[:2]] == [{}, {"cursor": "c1"}]
    assert clock.sleeps == [0.5, 1.0]

    sent.clear()
    reads["a"] = [_read("queued", "t-a")]
    connections, clock = _test_all_api(monkeypatch, pages, reads, sent)
    once = connections.test_all("prod", wait=False)
    assert {name: t.state for name, t in once.items()} == {"a": "queued", "b": "done"}
    assert [r.method for r in sent] == ["POST", "POST", "GET", "GET"]
    assert clock.sleeps == []


def test_test_all_times_out_naming_the_open_ones(monkeypatch: pytest.MonkeyPatch) -> None:
    pages: dict[str | None, dict[str, Any]] = {
        None: {
            "items": [_queued("c", "t-c"), _queued("b", "t-b"), _queued("a", "t-a")],
            "next_cursor": None,
        }
    }
    reads = {
        "a": [_read("queued", "t-a")],
        "b": [_read("running", "t-b")],
        "c": [_read("done", "t-c", "ok")],
    }
    connections, _ = _test_all_api(monkeypatch, pages, reads, [])

    with pytest.raises(TimeoutError) as raised:
        connections.test_all("prod", timeout=10.0)

    assert str(raised.value) == (
        "2 connection tests are not done after 10s: `a` is still queued (look again with "
        "lakana connection test a --env prod --id t-a), `b` is still running (look again with "
        "lakana connection test b --env prod --id t-b). No worker has taken a queued test: "
        "check that a worker of either runner is running (`docker compose ps worker`, or the "
        "`lakana local` terminal). A worker took a running test: read the worker's log "
        "(`docker compose logs worker`, or the `lakana local` terminal)."
    )

    one, _ = _test_all_api(
        monkeypatch,
        {None: {"items": [_queued("a", "t-a")], "next_cursor": None}},
        {"a": [_read("queued", "t-a")]},
        [],
    )
    with pytest.raises(TimeoutError) as alone:
        one.test_all("prod", timeout=60.0)
    assert str(alone.value).startswith("the test of `a` is still queued after 60s: no worker")
    assert str(alone.value).endswith("lakana connection test a --env prod --id t-a")


def test_test_all_with_only_running_tests_names_only_the_workers_log(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every open test was taken: no hint about a missing worker, only the log to read."""
    pages: dict[str | None, dict[str, Any]] = {
        None: {"items": [_queued("a", "t-a"), _queued("b", "t-b")], "next_cursor": None}
    }
    reads = {"a": [_read("running", "t-a")], "b": [_read("running", "t-b")]}
    connections, _ = _test_all_api(monkeypatch, pages, reads, [])

    with pytest.raises(TimeoutError) as raised:
        connections.test_all("prod", timeout=10.0)

    assert "No worker has taken" not in str(raised.value)
    assert str(raised.value).endswith(
        "A worker took a running test: read the worker's log "
        "(`docker compose logs worker`, or the `lakana local` terminal)."
    )
