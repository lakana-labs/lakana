"""Unit tests for `client.tables.definition` and `client.tables.definition_file`."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from lakana._config import ResolvedConfig
from lakana._http import HttpTransport
from lakana._models import TableDefinition, TableDefinitionFile
from lakana.errors import ServerError
from lakana.resources.tables import Tables

_CONFIG = ResolvedConfig(url="http://api.example", token="lakana_pat_abc_supersecret")
_DEFINITION = "/api/v1/environments/dev/namespaces/silver/tables/orders/definition"

LOCATION: dict[str, Any] = {
    "defined": True,
    "repository": "analytics",
    "path": "assets/silver/orders.sql",
    "line": 1,
    "commit": "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678",
    "project_version_id": "6f1c1d1e-0000-4000-8000-000000000001",
    "deployed_at": "2026-11-12T10:00:00Z",
    "browse_url": "https://github.com/acme/analytics/blob/a1b2c3d/assets/silver/orders.sql#L1",
}
"""A table a deployed project defines, as the REST API answers it."""


def _transport(
    handler: Callable[[httpx.Request], httpx.Response], seen: list[httpx.Request]
) -> HttpTransport:
    """Build an `HttpTransport` over a mock transport that remembers each request.

    Parameters
    ----------
    handler
        The mock transport's request handler.
    seen
        Each request is appended here.

    Returns
    -------
    HttpTransport
        A transport backed by `httpx.MockTransport`.
    """

    def remember(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    client = httpx.Client(transport=httpx.MockTransport(remember), base_url=_CONFIG.url)
    return HttpTransport(_CONFIG, http_client=client)


def _answer(body: dict[str, Any]) -> Callable[[httpx.Request], httpx.Response]:
    """Build a handler that answers every request with 200 and a body.

    Parameters
    ----------
    body
        The body.

    Returns
    -------
    Callable
        The handler.
    """
    return lambda _request: httpx.Response(200, json=body)


def test_definition_reads_the_location() -> None:
    seen: list[httpx.Request] = []
    found = Tables(_transport(_answer(LOCATION), seen)).definition("dev", "silver.orders")
    assert (seen[0].method, seen[0].url.path, seen[0].url.query, seen[0].content) == (
        "GET",
        _DEFINITION,
        b"",
        b"",
    )
    assert found == TableDefinition(
        defined=True,
        repository="analytics",
        path="assets/silver/orders.sql",
        line=1,
        commit=LOCATION["commit"],
        project_version_id=LOCATION["project_version_id"],
        deployed_at=datetime(2026, 11, 12, 10, 0, tzinfo=UTC),
        browse_url=LOCATION["browse_url"],
    )


def test_definition_of_a_table_no_project_defines_is_all_none() -> None:
    body = {"defined": False, **{k: None for k in LOCATION if k != "defined"}}
    found = Tables(_transport(_answer(body), [])).definition("dev", "silver.orders")
    assert found == TableDefinition(False, None, None, None, None, None, None, None)


def test_definition_ignores_a_field_it_does_not_know() -> None:
    found = Tables(_transport(_answer({**LOCATION, "added_later": 1}), [])).definition(
        "dev", "silver.orders"
    )
    assert found.path == "assets/silver/orders.sql"


def test_definition_file_reads_content_or_reason() -> None:
    seen: list[httpx.Request] = []
    shown = {**LOCATION, "content": "SELECT 1\n", "bytes": 9, "reason": None, "message": None}
    found = Tables(_transport(_answer(shown), seen)).definition_file("dev", "silver.orders")
    assert (seen[0].method, seen[0].url.path) == ("GET", _DEFINITION + "/file")
    assert isinstance(found, TableDefinitionFile)
    assert (found.content, found.bytes, found.reason, found.message) == (
        "SELECT 1\n",
        9,
        None,
        None,
    )
    assert (found.path, found.deployed_at) == (
        "assets/silver/orders.sql",
        datetime(2026, 11, 12, 10, 0, tzinfo=UTC),
    )
    refused = {**LOCATION, "content": None, "bytes": None, "reason": "restricted", "message": "M"}
    found = Tables(_transport(_answer(refused), [])).definition_file("dev", "silver.orders")
    assert (found.content, found.bytes, found.reason, found.message) == (
        None,
        None,
        "restricted",
        "M",
    )


def test_definition_file_raises_server_error_when_the_read_cannot_be_recorded() -> None:
    def refuse(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "try again"}, headers={"Retry-After": "1"})

    with pytest.raises(ServerError):
        Tables(_transport(refuse, [])).definition_file("dev", "silver.orders")
