"""Unit tests for `client.tables.preview` and `QueryRun.page`."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest

from lakana import _http
from lakana._config import ResolvedConfig
from lakana._http import BUSY_ATTEMPTS, HttpTransport
from lakana._models import MaskedColumn, ResultColumn
from lakana.errors import Conflict, InvalidResponse, LakanaError
from lakana.resources.queries import QueryRun
from lakana.resources.tables import Tables

_CONFIG = ResolvedConfig(url="http://api.example", token="lakana_pat_abc_supersecret")

PAGE_BODY: dict[str, Any] = {
    "columns": [{"name": "id", "type": "int64"}, {"name": "email", "type": "string"}],
    "columns_cut": 0,
    "masked_columns": [{"table": "gold.orders", "column": "email"}],
    "masked_columns_cut": 0,
    "rows": [["1", "***"], ["2", "***"], ["3", {"cut": True, "text": "abc"}]],
    "row_cut": [2],
    "row_count": 3,
    "next_offset": None,
    "duration_ms": 12,
    "preview": {"table": "gold.orders"},
}


def _transport(handler: Callable[[httpx.Request], httpx.Response]) -> HttpTransport:
    """Build an `HttpTransport` over a mock transport.

    Parameters
    ----------
    handler
        The mock transport's request handler.

    Returns
    -------
    HttpTransport
        A transport backed by `httpx.MockTransport(handler)`.
    """
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url=_CONFIG.url)
    return HttpTransport(_CONFIG, http_client=client)


def ok(body: dict[str, Any]) -> httpx.Response:
    """Answer 200 with a JSON body.

    Parameters
    ----------
    body
        The body.

    Returns
    -------
    httpx.Response
        The answer.
    """
    return httpx.Response(200, json=body)


def answer(body: dict[str, Any]) -> Callable[[httpx.Request], httpx.Response]:
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
    return lambda _request: ok(body)


def busy(retry_after: str | None = None) -> httpx.Response:
    """Answer the server's busy 503 for a result page.

    Parameters
    ----------
    retry_after
        The `Retry-After` header, or `None` for none.

    Returns
    -------
    httpx.Response
        The answer.
    """
    headers = {} if retry_after is None else {"Retry-After": retry_after}
    return httpx.Response(
        503, json={"detail": "busy", "type": "ResultPageLimitError"}, headers=headers
    )


def _run(handler: Callable[[httpx.Request], httpx.Response]) -> QueryRun:
    return QueryRun(_transport(handler), id="q1", environment="dev")


def test_preview_posts_no_body_and_returns_a_handle() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(202, json={"query_id": "q1"})

    run = Tables(_transport(handler)).preview("dev", "gold.orders")
    assert (seen[0].method, seen[0].url.path, seen[0].content, run.id) == (
        "POST",
        "/api/v1/environments/dev/namespaces/gold/tables/orders/preview",
        b"",
        "q1",
    )


def test_page_asks_for_json_with_the_limit_and_offset() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return ok(PAGE_BODY)

    _run(handler).page(limit=5, offset=10)
    assert (seen[0].method, seen[0].url.path, dict(seen[0].url.params)) == (
        "GET",
        "/api/v1/environments/dev/queries/q1/result",
        {"format": "json", "limit": "5", "offset": "10"},
    )


def test_page_maps_the_body() -> None:
    page = _run(answer(PAGE_BODY)).page()
    assert page.preview_table == "gold.orders"
    assert page.masked_columns == (MaskedColumn("gold.orders", "email"),)
    assert page.columns[0] == ResultColumn("id", "int64", False)
    assert page.columns[0].type_cut is False
    assert page.rows[2] == ("3", {"cut": True, "text": "abc"})
    assert (page.row_cut, page.row_count, page.next_offset, page.duration_ms) == ((2,), 3, None, 12)


def test_page_reads_the_cut_markers() -> None:
    body = {
        **PAGE_BODY,
        "columns": [{"name": "c", "type": "x", "type_cut": True}],
        "columns_cut": 4,
        "masked_columns_cut": 2,
    }
    page = _run(answer(body)).page()
    assert (page.columns[0].type_cut, page.columns_cut, page.masked_columns_cut) == (True, 4, 2)


def test_page_maps_masked_columns_null_to_none() -> None:
    body = {**PAGE_BODY, "masked_columns": None, "preview": None}
    page = _run(answer(body)).page()
    assert (page.masked_columns, page.preview_table) == (None, None)


def test_page_asks_again_when_busy_and_never_sleeps(monkeypatch: pytest.MonkeyPatch) -> None:
    waits: list[float] = []
    monkeypatch.setattr(_http, "wait", waits.append)
    answers = iter([busy("1"), busy("1"), ok(PAGE_BODY)])
    assert _run(lambda _r: next(answers)).page().row_count == 3
    assert waits == [1.0, 1.0]


def test_page_gives_up_after_the_busy_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    waits: list[float] = []
    monkeypatch.setattr(_http, "wait", waits.append)
    with pytest.raises(LakanaError) as caught:
        _run(lambda _r: busy()).page()
    assert caught.value.status == 503
    assert len(waits) == BUSY_ATTEMPTS - 1


def test_page_raises_at_once_on_a_503_that_is_not_the_busy_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    waits: list[float] = []
    monkeypatch.setattr(_http, "wait", waits.append)
    with pytest.raises(LakanaError) as caught:
        _run(lambda _r: httpx.Response(503, json={"detail": "down"})).page()
    assert caught.value.status == 503
    assert waits == []


def test_page_raises_at_once_on_another_error() -> None:
    with pytest.raises(LakanaError) as caught:
        _run(lambda _r: httpx.Response(409, json={"detail": "not done"})).page()
    assert caught.value.status == 409


def test_wait_really_sleeps(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []
    monkeypatch.setattr(_http.time, "sleep", slept.append)
    _http.wait(1.5)
    assert slept == [1.5]


def test_preview_of_an_artifact_is_a_conflict_the_sdk_raises() -> None:
    message = "An artifact holds files, not rows; download it with `lakana artifact download a.b`."

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": message, "type": "ConflictError"})

    with pytest.raises(Conflict, match="holds files, not rows"):
        Tables(_transport(handler)).preview("dev", "a.b")


def test_page_of_a_body_that_is_not_json_names_the_cause() -> None:
    with pytest.raises(InvalidResponse, match="not what a Lakana server sends"):
        _run(lambda _r: httpx.Response(200, content=b"<html>proxy</html>")).page()


def test_page_of_a_json_body_that_is_not_a_page_names_the_cause() -> None:
    with pytest.raises(InvalidResponse, match="not what a Lakana server sends"):
        _run(lambda _r: httpx.Response(200, json=["not", "a", "page"])).page()


def test_page_reads_next_offset() -> None:
    page = _run(answer({**PAGE_BODY, "next_offset": 3})).page()
    assert page.next_offset == 3


def test_page_waits_as_long_as_the_retry_after_says(monkeypatch: pytest.MonkeyPatch) -> None:
    waits: list[float] = []
    monkeypatch.setattr(_http, "wait", waits.append)
    answers = iter([busy("3"), ok(PAGE_BODY)])
    _run(lambda _r: next(answers)).page()
    assert waits == [3.0]


def test_page_raises_at_once_on_the_busy_type_with_another_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    waits: list[float] = []
    monkeypatch.setattr(_http, "wait", waits.append)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, json={"detail": "no", "type": "ResultPageLimitError"})

    with pytest.raises(LakanaError) as caught:
        _run(handler).page()
    assert caught.value.status == 502
    assert waits == []
