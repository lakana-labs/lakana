"""Tests for the request-body limit and for authenticating before a JSON body is read."""

import asyncio
from collections.abc import Iterable, Iterator
from pathlib import Path

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.routing import BaseRoute
from starlette.types import Message, Receive, Scope, Send

from lakana_core.config import DatabaseConfig, SecretsConfig
from lakana_server.api.routing import AuthenticateFirstRoute, depends_on_get_principal
from lakana_server.app import ROUTERS, create_app
from lakana_server.body_limit import BodyLimitMiddleware, _replay
from lakana_server.settings import RequestBodyConfig, Settings
from lakana_server.sign_in_audit import SignInAuditLimiter
from tests.conftest import _TEST_MASTER_KEY, AUTH_HEADER

LIMIT = 2048
"""The small body limit these tests set; every oversized body is a few kilobytes."""

JSON = {"Content-Type": "application/json"}
"""The header a real client sends; without it FastAPI does not decode the body as JSON at all."""

JSON_ROUTE = "/api/v1/principals"
"""A JSON route whose body is a pydantic model."""

STREAMING_ROUTES = {
    ("POST", "/api/v1/environments/{env}/namespaces/{path}/tables/{name}/data"),
    ("PUT", "/api/v1/environments/{env}/namespaces/{path}/tables/{name}/data"),
    ("POST", "/api/v1/environments/{env}/namespaces/{path}/tables/{name}/merge"),
    ("POST", "/api/v1/environments/{env}/namespaces/{path}/artifacts/{name}/uploads"),
    (
        "PUT",
        "/api/v1/environments/{env}/namespaces/{path}/artifacts/{name}/uploads/{upload_id}/blobs/{sha256}",
    ),
    ("POST", "/api/v1/auth/oidc/token"),
}
"""Every route that reads its own body as a stream, with a limit of its own."""


@pytest.fixture
def small_client(settings: Settings, tmp_path: Path) -> Iterator[TestClient]:
    """Provide a client whose JSON body limit is `LIMIT` bytes.

    Parameters
    ----------
    settings
        The suite's settings.
    tmp_path
        The per-test directory.

    Yields
    ------
    TestClient
        A client for an app with the small limit.
    """
    small = settings.model_copy(update={"request_body": RequestBodyConfig(max_bytes=LIMIT)})
    with TestClient(create_app(small)) as client:
        yield client


def _oversized() -> bytes:
    """Build a body just over the test limit that is not valid JSON.

    Returns
    -------
    bytes
        `LIMIT + 1` bytes of text, so a parse before the size check would answer 422.
    """
    return b"x" * (LIMIT + 1)


def test_the_default_limit_is_one_mebibyte() -> None:
    """A team that sets nothing gets a limit that no ordinary request reaches."""
    assert RequestBodyConfig().max_bytes == 1_048_576


def test_a_limit_of_zero_is_refused_at_startup() -> None:
    """A limit that would refuse every body is a configuration error, not a surprise."""
    with pytest.raises(ValueError, match="max_bytes"):
        RequestBodyConfig(max_bytes=0)


def test_an_oversized_body_without_a_token_is_refused_before_it_is_parsed(
    small_client: TestClient,
) -> None:
    """The size check comes first: no token and no valid JSON, and still 413, never 401 or 422."""
    response = small_client.post(JSON_ROUTE, content=_oversized())
    assert response.status_code == 413
    assert response.json()["type"] == "PayloadTooLargeError"
    assert "LAKANA_REQUEST_BODY__MAX_BYTES" in response.json()["detail"]
    assert str(LIMIT) in response.json()["detail"]


def test_an_oversized_chunked_body_is_refused_too(small_client: TestClient) -> None:
    """A body with no declared length is counted while it is read."""

    def chunks() -> Iterator[bytes]:
        for _ in range(3):
            yield b"x" * (LIMIT // 2)

    response = small_client.post(JSON_ROUTE, content=chunks())
    assert response.status_code == 413


def test_a_body_of_exactly_the_limit_is_read(small_client: TestClient) -> None:
    """The limit is inclusive: a body of that many bytes reaches the route."""
    padding = " " * (LIMIT - len('{"name": "x"}'))
    response = small_client.post(
        JSON_ROUTE, headers=AUTH_HEADER, content=('{"name": "x"}' + padding).encode()
    )
    assert response.status_code != 413


def test_an_oversized_body_to_the_iceberg_catalog_is_refused_in_its_own_shape(
    small_client: TestClient,
) -> None:
    """The Iceberg REST routes answer errors in the shape their specification gives."""
    response = small_client.post("/v1/dev/namespaces", content=_oversized())
    assert response.status_code == 413
    assert response.json()["error"]["code"] == 413


def test_the_iceberg_shape_is_chosen_under_a_root_path(settings: Settings) -> None:
    """Behind a proxy that mounts the server under a prefix the Iceberg routes still match."""
    small = settings.model_copy(update={"request_body": RequestBodyConfig(max_bytes=LIMIT)})
    with TestClient(create_app(small), root_path="/lakana") as client:
        response = client.post("/lakana/v1/dev/namespaces", content=_oversized())
    assert response.status_code == 413
    assert response.json()["error"]["code"] == 413


def test_a_malformed_body_without_a_token_answers_401(client: TestClient) -> None:
    """Authentication comes before the body is parsed: no token, broken JSON, still 401."""
    response = client.post(JSON_ROUTE, headers=JSON, content=b"{not json")
    assert response.status_code == 401


def test_a_malformed_body_with_a_wrong_secret_answers_401(client: TestClient) -> None:
    """A well-formed header with the wrong secret is not authenticated either."""
    response = client.post(
        JSON_ROUTE,
        headers={**JSON, "Authorization": "Bearer lakana_pat_testkey_wrongsecret"},
        content=b"{not json",
    )
    assert response.status_code == 401


def test_a_malformed_body_with_a_valid_token_is_still_422(client: TestClient) -> None:
    """An authenticated caller learns what is wrong with the body."""
    response = client.post(JSON_ROUTE, headers={**JSON, **AUTH_HEADER}, content=b"{not json")
    assert response.status_code == 422


def test_authenticating_first_records_one_success_not_two(client: TestClient) -> None:
    """Pre-authentication hands its result on: a request is one audit event, not two.

    The sign-in window is set to 0 here, so that every request would write its event.
    """
    client.app.state.sign_in_audit_limiter = SignInAuditLimiter(0)
    client.post(JSON_ROUTE, headers=AUTH_HEADER, json={"name": "alice"})
    events = client.get("/api/v1/audit-events", headers=AUTH_HEADER).json()["items"]
    succeeded = [e for e in events if e["event_type"] == "AUTHENTICATION_SUCCEEDED"]
    first = len(succeeded)
    client.post(JSON_ROUTE, headers=AUTH_HEADER, json={"name": "bob"})
    events = client.get("/api/v1/audit-events", headers=AUTH_HEADER).json()["items"]
    after = len([e for e in events if e["event_type"] == "AUTHENTICATION_SUCCEEDED"])
    # the second POST and the second listing: two requests, two events.
    assert after - first == 2


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/v1/environments/dev/namespaces/bronze/tables/t/data"),
        ("PUT", "/api/v1/environments/dev/namespaces/bronze/tables/t/data"),
        ("POST", "/api/v1/environments/dev/namespaces/bronze/tables/t/merge?key=id"),
        ("POST", "/api/v1/environments/dev/namespaces/models/artifacts/a/uploads"),
        (
            "PUT",
            "/api/v1/environments/dev/namespaces/models/artifacts/a/uploads/u/blobs/" + "0" * 64,
        ),
    ],
)
def test_a_streaming_route_keeps_its_own_limit(
    small_client: TestClient, method: str, path: str
) -> None:
    """A body over the JSON limit reaches a route that streams it and has a limit of its own."""
    response = small_client.request(method, path, headers=AUTH_HEADER, content=_oversized())
    assert "LAKANA_REQUEST_BODY__MAX_BYTES" not in response.text


def test_the_streaming_routes_are_exactly_the_ones_listed() -> None:
    """A route that reads its own body is exempt; adding one means adding it here."""
    from lakana_server.body_limit import streaming_routes

    found = {
        (method, route.path)
        for route in streaming_routes(ROUTERS)
        for method in route.methods or ()
    }
    assert found == STREAMING_ROUTES


_OPEN_ROUTES = {"/healthz", "/readyz", "/v1/config", "/api/v1/auth/oidc/token"}
"""The routes that answer without a credential, by design: the CI token exchange takes an ID
token, not a credential, and reads at most 16 KiB of body; `/readyz` has no body and answers
with check names only, from a cache (`lakana_server.readiness`)."""

_DOCS_ROUTES = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}
"""FastAPI's own documentation routes: not `APIRoute`s, no body."""


_ORIGINAL_ROUTER_MOVED = (
    "FastAPI no longer gives an included router's route the private attribute `original_router`, "
    "which `_flatten` reads. After a FastAPI upgrade: find where the version keeps the router "
    "that `include_router` was given, and read that in `_flatten`; do not skip this guard."
)


def _flatten(routes: Iterable[BaseRoute]) -> Iterator[BaseRoute]:
    """Yield every route an application serves, looking into the routers it included.

    FastAPI keeps an included router as one entry that points at the router itself, so a router
    given to `include_router` before or after `create_app` is found either way.

    Parameters
    ----------
    routes
        `app.routes`, or the routes of an included router.

    Yields
    ------
    BaseRoute
        Each route that is not itself an included router.
    """
    for route in routes:
        inner = getattr(route, "original_router", None)
        if inner is not None:
            yield from _flatten(inner.routes)
        else:
            yield route


def _unguarded(app: FastAPI) -> list[str]:
    """List what in the running app breaks either rule of this module.

    The rules: a route with a JSON body is an `AuthenticateFirstRoute` that depends on
    `get_principal` and is not marked as streaming; and every route outside `_OPEN_ROUTES`
    depends on `get_principal`.

    Parameters
    ----------
    app
        The application, as `create_app` builds it, whatever put its routes there.

    Returns
    -------
    list of str
        One line per violation; empty when the app is guarded.
    """
    found: list[str] = []
    for route in _flatten(app.routes):
        if not isinstance(route, APIRoute):
            if route.path not in _DOCS_ROUTES:  # ty: ignore[unresolved-attribute]
                found.append(f"unknown route kind {route!r}")
            continue
        if route.path not in _OPEN_ROUTES and not depends_on_get_principal(route.dependant):
            found.append(f"{route.path} does not depend on get_principal")
        if route.body_field is not None:
            if not isinstance(route, AuthenticateFirstRoute):
                found.append(f"{route.path} has a body and is not an AuthenticateFirstRoute")
            if getattr(route.endpoint, "streams_its_own_body", False):
                found.append(f"{route.path} has a JSON body and is exempt from the limit")
    return found


def test_fastapi_still_keeps_the_included_router(settings: Settings) -> None:
    """`_flatten` reads a private FastAPI attribute; fail here, with the cause, if it is gone."""
    app = create_app(settings)
    assert any(hasattr(route, "original_router") for route in app.routes), _ORIGINAL_ROUTER_MOVED


def test_every_route_of_the_running_app_authenticates_and_every_json_body_is_limited(
    settings: Settings,
) -> None:
    """No route escapes either rule, however it got into the app (router, tuple or directly)."""
    app = create_app(settings)
    routes = [r for r in _flatten(app.routes) if isinstance(r, APIRoute)]
    assert sum(r.body_field is not None for r in routes) > 10
    assert _unguarded(app) == []


def test_a_body_route_put_directly_on_the_app_is_found_by_the_guard(settings: Settings) -> None:
    """The guard reads the app, so a route outside `ROUTERS` fails it."""
    app = create_app(settings)

    @app.post("/api/v1/sneaky")
    def sneaky(body: dict[str, str]) -> dict[str, str]:
        """Answer without authenticating.

        Parameters
        ----------
        body
            Anything.

        Returns
        -------
        dict
            Empty.
        """
        return {}

    assert _unguarded(app) == [
        "/api/v1/sneaky does not depend on get_principal",
        "/api/v1/sneaky has a body and is not an AuthenticateFirstRoute",
    ]


def test_a_router_included_outside_the_tuple_is_found_by_the_guard(settings: Settings) -> None:
    """A router added with `app.include_router` after `create_app` is checked too."""
    app = create_app(settings)
    extra = APIRouter()

    @extra.get("/api/v1/extra")
    def extra_route() -> dict[str, str]:
        """Answer without authenticating.

        Returns
        -------
        dict
            Empty.
        """
        return {}

    app.include_router(extra)
    assert _unguarded(app) == ["/api/v1/extra does not depend on get_principal"]


def test_settings_read_the_limit_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`LAKANA_REQUEST_BODY__MAX_BYTES` sets the limit."""
    monkeypatch.setenv("LAKANA_REQUEST_BODY__MAX_BYTES", "4096")
    loaded = Settings(
        database=DatabaseConfig(url=f"sqlite:///{tmp_path}/x.db"),
        secrets=SecretsConfig(master_key=_TEST_MASTER_KEY),
    )
    assert loaded.request_body.max_bytes == 4096


async def _drive(
    messages: list[Message], headers: list[tuple[bytes, bytes]], scope_type: str = "http"
) -> tuple[list[Message], list[Message]]:
    """Run the middleware over a hand-built ASGI exchange with a stub application.

    Parameters
    ----------
    messages
        What the client sends, in order.
    headers
        The request headers.
    scope_type
        The ASGI scope type.

    Returns
    -------
    tuple
        What the stub application received, and what was sent back.
    """
    received: list[Message] = []
    sent: list[Message] = []
    pending = list(messages)

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        while True:
            message = await receive()
            received.append(message)
            if message["type"] != "http.request" or not message.get("more_body"):
                break

    async def receive() -> Message:  # ruff: ignore[unused-async] -- the ASGI signature
        return pending.pop(0) if pending else {"type": "http.disconnect"}

    async def send(message: Message) -> None:  # ruff: ignore[unused-async] -- the ASGI signature
        sent.append(message)

    middleware = BodyLimitMiddleware(inner, max_bytes=10, exempt=[])
    scope: Scope = {"type": scope_type, "path": "/x", "method": "POST", "headers": headers}
    await middleware(scope, receive, send)
    return received, sent


def test_a_scope_that_is_not_http_goes_straight_through() -> None:
    """The lifespan exchange is none of the middleware's business."""
    received, sent = asyncio.run(_drive([{"type": "lifespan.startup"}], [], scope_type="lifespan"))
    assert received == [{"type": "lifespan.startup"}]
    assert sent == []


def test_a_content_length_that_is_not_a_number_is_counted_like_none() -> None:
    """A length that is not a number declares nothing; the bytes that arrive are counted."""
    received, sent = asyncio.run(
        _drive([{"type": "http.request", "body": b"hi"}], [(b"content-length", b"abc")])
    )
    assert received[0]["body"] == b"hi"
    assert sent == []
    over, sent = asyncio.run(
        _drive([{"type": "http.request", "body": b"x" * 11}], [(b"content-length", b"abc")])
    )
    assert over == []
    assert sent[0]["status"] == 413


def test_a_declared_length_over_the_limit_is_refused_without_reading() -> None:
    """A length over the limit is refused at once: the client's body is never asked for."""
    received, sent = asyncio.run(
        _drive([{"type": "http.request", "body": b"x" * 11}], [(b"content-length", b"11")])
    )
    assert received == []
    assert sent[0]["status"] == 413


def test_a_content_length_cannot_hide_a_longer_chunked_body() -> None:
    """Both headers, a short declared length and a long body: the bytes decide, not the header."""
    pieces: list[Message] = [
        {"type": "http.request", "body": b"123456", "more_body": True},
        {"type": "http.request", "body": b"123456"},
    ]
    received, sent = asyncio.run(
        _drive(pieces, [(b"content-length", b"5"), (b"transfer-encoding", b"chunked")])
    )
    assert received == []
    assert sent[0]["status"] == 413


def test_a_body_with_neither_length_header_is_counted_too() -> None:
    """No header at all says nothing about the body; the bytes that arrive are counted."""
    pieces: list[Message] = [
        {"type": "http.request", "body": b"123456", "more_body": True},
        {"type": "http.request", "body": b"123456"},
    ]
    received, sent = asyncio.run(_drive(pieces, []))
    assert received == []
    assert sent[0]["status"] == 413


def test_a_body_of_exactly_the_limit_reaches_the_application_byte_for_byte() -> None:
    """The limit is inclusive, with a header that agrees or with none."""
    for headers in ([(b"content-length", b"10")], []):
        received, sent = asyncio.run(
            _drive([{"type": "http.request", "body": b"0123456789"}], headers)
        )
        assert received == [{"type": "http.request", "body": b"0123456789"}]
        assert sent == []


def test_a_client_that_leaves_in_the_middle_of_a_declared_body_is_passed_on() -> None:
    """A disconnect after part of a declared body ends the wait: the application is told."""
    pieces: list[Message] = [{"type": "http.request", "body": b"123", "more_body": True}]
    received, _ = asyncio.run(_drive(pieces, [(b"content-length", b"8")]))
    assert received == [{"type": "http.disconnect"}]


def test_a_request_without_a_body_is_not_held_up() -> None:
    """An empty request reaches the application with the one empty message it was sent."""
    received, _ = asyncio.run(_drive([{"type": "http.request", "body": b""}], []))
    assert received == [{"type": "http.request", "body": b""}]


def test_a_chunked_body_within_the_limit_is_handed_on_whole() -> None:
    """Chunks that add up to the limit or less reach the application as one body."""
    pieces: list[Message] = [
        {"type": "http.request", "body": b"12345", "more_body": True},
        {"type": "http.request", "body": b"12345"},
    ]
    received, sent = asyncio.run(_drive(pieces, [(b"transfer-encoding", b"chunked")]))
    assert received == [{"type": "http.request", "body": b"1234512345"}]
    assert sent == []


def test_a_client_that_leaves_in_the_middle_of_a_chunked_body_is_passed_on() -> None:
    """A disconnect is not a body; the application is told, as it would be without the limit."""
    pieces: list[Message] = [{"type": "http.request", "body": b"123", "more_body": True}]
    received, _ = asyncio.run(_drive(pieces, [(b"transfer-encoding", b"chunked")]))
    assert received == [{"type": "http.disconnect"}]


def test_what_the_client_sends_after_the_body_is_still_delivered() -> None:
    """After the held body, the application's next read is the client's own (a disconnect)."""

    async def client_side() -> Message:  # ruff: ignore[unused-async] -- the ASGI signature
        return {"type": "http.disconnect"}

    async def read_twice() -> list[Message]:
        receive = _replay([{"type": "http.request", "body": b"x"}], client_side)
        return [await receive(), await receive()]

    assert asyncio.run(read_twice()) == [
        {"type": "http.request", "body": b"x"},
        {"type": "http.disconnect"},
    ]
