"""The SDK's `connections` resource: what it sends and how it reads the answers."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from lakana._config import ResolvedConfig
from lakana._http import HttpTransport
from lakana._models import Connection, DeclaringAsset
from lakana.errors import InvalidResponse
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
