"""Tests for the web interface's Host and Origin guard (`lakana_server.browser_guard`)."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.types import Message, Receive, Scope, Send

import lakana_server.ui
from lakana_server.app import create_app
from lakana_server.browser_guard import LOOPBACK_NAMES, BrowserGuard
from lakana_server.settings import Settings, UiConfig
from lakana_server.ui import CSP_FILE
from tests.conftest import AUTH_HEADER

MARKER = "<!-- guard-index -->"
"""In the stand-in page only, so a test can tell the page from any other answer."""

PUBLIC = "http://localhost:8000"
"""The public origin of the test settings: `LAKANA_UI__PUBLIC_URL` unset, port 8000."""

COOKIE = {"Cookie": "lakana_session_8000=abc"}
"""A session cookie as the interface on loopback port 8000 carries it."""

DESCRIBE = "/api/v1/sign-in/links/describe"
"""A sign-in route: browser traffic whatever its credential."""


@pytest.fixture
def ui_dist(tmp_path: Path) -> Path:
    """Write the smallest build the server accepts.

    Parameters
    ----------
    tmp_path
        Pytest's per-test temporary directory.

    Returns
    -------
    Path
        The stand-in build directory.
    """
    dist = tmp_path / "ui_dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(f"<!doctype html><html><body>{MARKER}</body></html>")
    (dist / CSP_FILE).write_text("default-src 'self'")
    (dist / "assets" / "app-1a2b.js").write_text("console.log('app');")
    return dist


@pytest.fixture
def client(
    ui_dist: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    """Provide a client for an app with the interface on, reached at the public address.

    Parameters
    ----------
    ui_dist
        The stand-in build directory.
    settings
        The test settings.
    monkeypatch
        Points `lakana_server.ui.UI_DIST` at the stand-in.

    Yields
    ------
    TestClient
        A started client whose requests carry `Host: localhost:8000` unless a test sets one.
    """
    monkeypatch.setattr(lakana_server.ui, "UI_DIST", ui_dist)
    app = create_app(settings.model_copy(update={"ui": UiConfig(enabled=True)}))
    with TestClient(app, base_url=PUBLIC) as c:
        yield c


def test_the_public_address_serves_the_page(client: TestClient) -> None:
    """The guard lets the page through at `localhost:<port>`."""
    response = client.get("/")
    assert response.status_code == 200
    assert MARKER in response.text


def test_unknown_host_is_refused_with_the_setting_named(client: TestClient) -> None:
    """A rebinding name reaching a page gets 421, naming the setting to fix."""
    r = client.get("/", headers={"Host": "evil.example:8000"})
    assert r.status_code == 421
    assert r.json()["type"] == "HostNotAllowed"
    assert "LAKANA_UI__PUBLIC_URL" in r.json()["detail"]
    assert "evil" not in r.text


@pytest.mark.parametrize(
    "host",
    [
        "localhost:9999",
        "localhost",
        "localhost.:8000",
        "localhost:08000",
        "localhost:8000.",
        "127.0.0.2:8000",
        "[0:0:0:0:0:0:0:1]:8000",
        "[::ffff:127.0.0.1]:8000",
        "evil.localhost:8000",
        "localhost:8000@evil.example",
        "",
    ],
)
def test_host_variants_are_refused(client: TestClient, host: str) -> None:
    """Exact name and port only: no other port, trailing dot, other loopback spelling."""
    r = client.get("/catalog", headers={"Host": host})
    assert r.status_code == 421
    assert MARKER not in r.text


@pytest.mark.parametrize("host", ["LOCALHOST:8000", "LocalHost:8000"])
def test_host_letter_case_does_not_matter(client: TestClient, host: str) -> None:
    """Host names compare without letter case, as DNS does."""
    assert MARKER in client.get("/", headers={"Host": host}).text


def test_cookie_request_with_an_unknown_host_is_refused(client: TestClient) -> None:
    """An API call carrying the session cookie is browser traffic."""
    r = client.get("/api/v1/whoami", headers={**COOKIE, "Host": "evil.example:8000"})
    assert r.status_code == 421
    assert r.json()["type"] == "HostNotAllowed"


@pytest.mark.parametrize(
    "cookie",
    [
        "lakana_session_8000=abc",
        "other=1; __Host-lakana_session=abc",
        "lakana_session=abc",
        "LAKANA_SESSION_8000=abc",
        " lakana_session_9000 = abc ;x=1",
    ],
)
def test_every_session_cookie_name_makes_browser_traffic(client: TestClient, cookie: str) -> None:
    """Any `lakana_session*` or `__Host-lakana_session` name, in any letter case."""
    r = client.get("/api/v1/whoami", headers={"Cookie": cookie, "Host": "evil.example:8000"})
    assert r.status_code == 421


def test_sign_in_route_with_an_unknown_host_is_refused(client: TestClient) -> None:
    """A sign-in route is browser traffic even without a cookie."""
    r = client.post(DESCRIBE, json={}, headers={"Host": "evil.example", "Origin": PUBLIC})
    assert r.status_code == 421
    assert r.json()["type"] == "HostNotAllowed"


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/v1/config"),
        ("POST", "/api/v1/auth/oidc/token"),
        ("GET", "/healthz"),
        ("GET", "/readyz"),
        ("GET", "/api/v1/whoami"),
    ],
)
def test_non_browser_traffic_skips_the_host_check(
    client: TestClient, method: str, path: str
) -> None:
    """Engines, CI and the worker reach open routes by internal names, without a cookie."""
    r = client.request(method, path, headers={"Host": "server:8000"})
    assert r.status_code != 421
    assert r.status_code != 403 or r.json().get("type") != "OriginNotAllowed"


def test_bearer_request_skips_the_host_check(client: TestClient) -> None:
    """An ordinary API route with a Bearer passes at any name, as the Compose worker calls it."""
    r = client.get("/api/v1/whoami", headers={**AUTH_HEADER, "Host": "server:8000"})
    assert r.status_code == 200


@pytest.mark.parametrize(
    ("method", "path"), [("POST", DESCRIBE), ("GET", "/"), ("GET", "/catalog")]
)
def test_a_fake_bearer_does_not_skip_the_host_check(
    client: TestClient, method: str, path: str
) -> None:
    """A rebinding page can add any `Authorization`: sign-in routes and pages still check Host."""
    r = client.request(
        method, path, headers={"Authorization": "Bearer x", "Host": "evil.example:8000"}
    )
    assert r.status_code == 421
    assert r.json()["type"] == "HostNotAllowed"


def test_a_bearer_sign_in_request_needs_no_origin(client: TestClient) -> None:
    """At an allowed name, a Bearer request (the CLI's local sign-in) skips the Origin rule."""
    r = client.post(DESCRIBE, json={}, headers=AUTH_HEADER)
    assert r.json().get("type") != "OriginNotAllowed"


def test_bearer_request_with_a_cookie_skips_the_guard(client: TestClient) -> None:
    """The Bearer header wins over the cookie, so the cookie does not make it browser traffic."""
    r = client.get("/api/v1/whoami", headers={**AUTH_HEADER, **COOKIE, "Host": "evil.example"})
    assert r.status_code == 200


@pytest.mark.parametrize("authorization", ["Basic dXNlcjpwdw==", "Bearer", "bearerx y", ""])
def test_an_authorization_header_that_is_not_bearer_does_not_skip(
    client: TestClient, authorization: str
) -> None:
    """Only a Bearer credential skips the guard; any other header is checked like none."""
    r = client.get(
        "/api/v1/whoami",
        headers={**COOKIE, "Authorization": authorization, "Host": "evil.example"},
    )
    assert r.status_code == 421


@pytest.mark.parametrize("alias", ["127.0.0.1:8000", "[::1]:8000"])
def test_loopback_alias_redirects_pages_to_localhost(client: TestClient, alias: str) -> None:
    """A page opened at a loopback address moves to `localhost`, where passkeys work."""
    r = client.get("/catalog?x=1", headers={"Host": alias}, follow_redirects=False)
    assert r.status_code == 308
    assert r.headers["location"] == "http://localhost:8000/catalog?x=1"


def test_loopback_alias_head_is_redirected_and_post_is_not(client: TestClient) -> None:
    """Only GET and HEAD move; a POST at an alias meets the Origin check instead."""
    assert (
        client.head("/", headers={"Host": "127.0.0.1:8000"}, follow_redirects=False).status_code
        == 308
    )
    r = client.post("/", headers={"Host": "127.0.0.1:8000"}, follow_redirects=False)
    assert r.status_code == 403


def test_loopback_alias_is_not_redirected_for_the_api(client: TestClient) -> None:
    """An API call with the cookie at a loopback alias is answered, not moved."""
    r = client.get(
        "/api/v1/whoami", headers={**COOKIE, "Host": "127.0.0.1:8000"}, follow_redirects=False
    )
    assert r.status_code != 308
    assert r.status_code == 401


def test_unsafe_cookie_request_without_origin_is_refused(client: TestClient) -> None:
    """A state change with the cookie and no Origin is refused, naming `Origin`."""
    r = client.post(DESCRIBE, json={}, headers=COOKIE)
    assert r.status_code == 403
    assert r.json()["type"] == "OriginNotAllowed"
    assert "Origin" in r.json()["detail"]


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "PROPFIND"])
def test_every_unsafe_method_needs_the_origin(client: TestClient, method: str) -> None:
    """Every method but GET, HEAD and OPTIONS is a state change here."""
    r = client.request(method, "/api/v1/whoami", headers=COOKIE)
    assert r.status_code == 403


def test_sec_fetch_site_same_site_is_not_enough(client: TestClient) -> None:
    """On loopback, other ports are the same site: only same-origin counts."""
    r = client.post(DESCRIBE, json={}, headers={**COOKIE, "Sec-Fetch-Site": "same-site"})
    assert r.status_code == 403


def test_sec_fetch_site_same_origin_is_accepted(client: TestClient) -> None:
    """A browser that sends no Origin but `Sec-Fetch-Site: same-origin` passes."""
    r = client.post(DESCRIBE, json={}, headers={**COOKIE, "Sec-Fetch-Site": "same-origin"})
    assert r.json().get("type") != "OriginNotAllowed"


def test_the_public_origin_passes(client: TestClient) -> None:
    """The exact public origin passes the guard (the route itself answers)."""
    r = client.post(DESCRIBE, json={}, headers={**COOKIE, "Origin": PUBLIC})
    assert r.json().get("type") != "OriginNotAllowed"


@pytest.mark.parametrize(
    "origin",
    [
        "http://localhost:9999",
        "null",
        "https://localhost:8000",
        "http://127.0.0.1:8000",
        "http://localhost:8000/",
        "http://LOCALHOST:8000",
        "http://localhost:8000.evil.example",
        "",
    ],
)
def test_origin_must_match_scheme_host_and_port(client: TestClient, origin: str) -> None:
    """Exactly the public origin: another port on localhost is another origin."""
    r = client.post(
        DESCRIBE, json={}, headers={**COOKIE, "Origin": origin, "Sec-Fetch-Site": "same-origin"}
    )
    assert r.status_code == 403
    assert r.json()["type"] == "OriginNotAllowed"


def test_a_page_post_without_a_cookie_needs_the_origin(client: TestClient) -> None:
    """A page path is browser traffic whatever its credential."""
    assert client.post("/catalog").status_code == 403


def test_guard_is_absent_with_the_interface_off(settings: Settings) -> None:
    """With `LAKANA_UI__ENABLED=false` no Host or Origin is checked."""
    with TestClient(create_app(settings)) as c:
        assert c.get("/", headers={"Host": "evil.example:8000"}).status_code == 404
        r = c.post(DESCRIBE, json={}, headers={**COOKIE, "Host": "evil.example"})
        assert r.status_code not in {403, 421}


def test_api_answers_carry_no_cors_headers(client: TestClient) -> None:
    """No CORS on the API: a cross-origin page gets nothing it may read."""
    r = client.options(
        "/api/v1/whoami",
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"},
    )
    assert not any(h.lower().startswith("access-control-") for h in r.headers)
    r = client.get("/api/v1/whoami", headers={**AUTH_HEADER, "Origin": "https://evil.example"})
    assert not any(h.lower().startswith("access-control-") for h in r.headers)


def test_loopback_names() -> None:
    """The names a local server answers at, besides the configured ones."""
    assert frozenset({"localhost", "127.0.0.1", "::1"}) == LOOPBACK_NAMES


def _call(settings: Settings, scope_update: Mapping[str, object]) -> tuple[int, dict[str, str]]:
    """Run the guard alone over one raw ASGI request.

    Parameters
    ----------
    settings
        The settings the guard reads.
    scope_update
        Keys that replace those of a plain `GET /` at the public address.

    Returns
    -------
    tuple of (int, dict of str to str)
        The status sent, and the response headers; 200 when the request reached the app.
    """
    sent: list[Message] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        """Answer 200, standing in for the routes.

        Parameters
        ----------
        scope
            The ASGI scope.
        receive
            The ASGI receive channel.
        send
            The ASGI send channel.
        """
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive() -> Message:  # ruff: ignore[unused-async]
        """Hand over an empty body.

        Returns
        -------
        dict
            The one `http.request` message.
        """
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:  # ruff: ignore[unused-async]
        """Keep a message the guard sends.

        Parameters
        ----------
        message
            The ASGI message.
        """
        sent.append(message)

    scope: dict[str, object] = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/",
        "raw_path": b"/",
        "root_path": "",
        "query_string": b"",
        "headers": [(b"host", b"localhost:8000")],
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 8000),
    }
    scope.update(scope_update)
    asyncio.run(BrowserGuard(app, settings=settings)(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    headers = {k.decode().lower(): v.decode() for k, v in start["headers"]}
    return start["status"], headers


def test_a_page_request_without_host_is_refused(settings: Settings) -> None:
    """An HTTP/1.0 request without `Host` is not a request to the public address."""
    assert _call(settings, {"headers": [], "http_version": "1.0"})[0] == 421


def test_two_host_headers_are_refused(settings: Settings) -> None:
    """Two `Host` headers are ambiguous: neither is believed."""
    headers = [(b"host", b"localhost:8000"), (b"host", b"evil.example")]
    assert _call(settings, {"headers": headers})[0] == 421


def test_two_origin_headers_are_refused(settings: Settings) -> None:
    """Two `Origin` headers are ambiguous: neither is believed."""
    headers = [
        (b"host", b"localhost:8000"),
        (b"origin", PUBLIC.encode()),
        (b"origin", b"https://evil.example"),
    ]
    assert _call(settings, {"method": "POST", "headers": headers})[0] == 403


def test_an_absolute_form_target_is_judged_by_its_host_header(settings: Settings) -> None:
    """`GET http://localhost:8000/ HTTP/1.1` with `Host: evil` is refused: the header counts."""
    scope = {"raw_path": b"http://localhost:8000/", "headers": [(b"host", b"evil.example")]}
    assert _call(settings, scope)[0] == 421


def test_an_absolute_form_target_never_reaches_the_location(settings: Settings) -> None:
    """The redirect at an alias is built from the decoded path, never from the raw target."""
    scope = {
        "path": "/catalog",
        "raw_path": b"http://evil.example/catalog",
        "headers": [(b"host", b"127.0.0.1:8000")],
    }
    status, headers = _call(settings, scope)
    assert status == 308
    assert headers["location"] == "http://localhost:8000/catalog"


def test_a_path_that_looks_like_an_authority_stays_a_path(settings: Settings) -> None:
    """At an alias, `//evil.example/...` moves to a path below the public origin, re-encoded."""
    scope = {"path": "//evil.example/../a b", "headers": [(b"host", b"127.0.0.1:8000")]}
    status, headers = _call(settings, scope)
    assert status == 308
    assert headers["location"] == "http://localhost:8000//evil.example/../a%20b"


def test_a_path_prefix_is_removed_before_the_api_is_recognised(settings: Settings) -> None:
    """Behind a path prefix (`root_path`), `/lakana/v1/config` is still not browser traffic."""
    scope = {"path": "/lakana/v1/config", "root_path": "/lakana", "headers": [(b"host", b"server")]}
    assert _call(settings, scope)[0] == 200
    scope = {"path": "/lakana/catalog", "root_path": "/lakana", "headers": [(b"host", b"server")]}
    assert _call(settings, scope)[0] == 421


@pytest.mark.parametrize(
    "path", ["/apix", "/API/v1/whoami", "/v1x", "/healthzz", "//api/v1/whoami"]
)
def test_a_path_only_resembling_the_api_is_browser_traffic(settings: Settings, path: str) -> None:
    """Only `/api/`, `/v1/`, `/healthz` and `/readyz` themselves skip the Host check."""
    assert _call(settings, {"path": path, "headers": [(b"host", b"server")]})[0] == 421


def test_lifespan_and_other_scopes_pass(settings: Settings) -> None:
    """The guard looks only at HTTP and WebSocket requests."""
    seen: list[str] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:  # ruff: ignore[unused-async]
        """Record the scope type.

        Parameters
        ----------
        scope
            The ASGI scope.
        receive
            The ASGI receive channel.
        send
            The ASGI send channel.
        """
        seen.append(scope["type"])

    async def nothing() -> Message:  # ruff: ignore[unused-async]
        """Stand in for the receive channel.

        Returns
        -------
        dict
            An empty message.
        """
        return {}

    async def drop(message: Message) -> None:
        """Stand in for the send channel.

        Parameters
        ----------
        message
            The ASGI message, dropped.
        """

    asyncio.run(BrowserGuard(app, settings=settings)({"type": "lifespan"}, nothing, drop))
    assert seen == ["lifespan"]


def test_a_websocket_at_an_unknown_host_is_closed(settings: Settings) -> None:
    """A WebSocket to a page path is browser traffic too: it is closed before the app sees it."""
    sent: list[Message] = []
    reached: list[bool] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:  # ruff: ignore[unused-async]
        """Record that the app was reached.

        Parameters
        ----------
        scope
            The ASGI scope.
        receive
            The ASGI receive channel.
        send
            The ASGI send channel.
        """
        reached.append(True)

    async def receive() -> Message:  # ruff: ignore[unused-async]
        """Hand over the connect message.

        Returns
        -------
        dict
            `websocket.connect`.
        """
        return {"type": "websocket.connect"}

    async def send(message: Message) -> None:  # ruff: ignore[unused-async]
        """Keep a message the guard sends.

        Parameters
        ----------
        message
            The ASGI message.
        """
        sent.append(message)

    scope = {
        "type": "websocket",
        "path": "/ws",
        "root_path": "",
        "headers": [(b"host", b"evil.example")],
    }
    asyncio.run(BrowserGuard(app, settings=settings)(scope, receive, send))
    assert reached == []
    assert sent[0]["type"] == "websocket.close"
