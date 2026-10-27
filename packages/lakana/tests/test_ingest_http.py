"""`lakana.ingest._http`: auth bound to the connection's host, OAuth2 client credentials."""

from __future__ import annotations

import base64
import itertools
import json
import secrets
import ssl
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
from sdk_tls_fixture import Answer, Authority, Exchange, make_authority, scripted

from lakana import context
from lakana.ingest import (
    SourceError,
    SourceRefused,
    UsageError,
    _connections,
    _http,
    files,
)
from lakana.ingest.connections import Connection, MaskedMapping

# Generated per run: no literal credential in the tree (gitleaks).
TOKEN = secrets.token_urlsafe(16)
CLIENT_SECRET = secrets.token_urlsafe(16)
ACCESS = secrets.token_urlsafe(16)


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Remove the variables the client could read; leave no task state behind.

    Parameters
    ----------
    monkeypatch
        Pytest's patcher.

    Yields
    ------
    None
        While the test runs.
    """
    for name in ("LAKANA_EGRESS_PROXY", "LAKANA_CA_BUNDLE", "HTTPS_PROXY", "https_proxy"):
        monkeypatch.delenv(name, raising=False)
    yield
    context._set(None)
    _connections.clear()


@pytest.fixture
def authority(tmp_path: Path) -> Authority:
    """Make the authority that signs the test servers.

    Parameters
    ----------
    tmp_path
        Pytest's directory.

    Returns
    -------
    Authority
        The authority.
    """
    return make_authority(tmp_path)


def _state() -> context._TaskState:
    """Set the state of a running task, so registered secrets can be read back.

    Returns
    -------
    context._TaskState
        The state.
    """
    state = context._TaskState(
        client=None,  # ty: ignore[invalid-argument-type]
        environment="dev",
        asset="bronze.x",
        declared=frozenset(),
        connections={},
    )
    context._set(state)
    return state


def _conn(
    base: str,
    authority: Authority,
    settings: Mapping[str, object],
    secret: Mapping[str, str] | None = None,
    proxy_url: str | None = None,
) -> Connection:
    """Build an ``http`` connection as `connections.get` returns it.

    Parameters
    ----------
    base
        The base URL.
    authority
        Whose CA the connection trusts.
    settings
        Its settings besides ``ca_certificate``.
    secret
        Its secret.
    proxy_url
        The task's proxy.

    Returns
    -------
    Connection
        The connection.
    """
    return Connection(
        name="api",
        kind="http",
        address={"base_url": base},
        settings={**settings, "ca_certificate": authority.ca_file.read_text()},
        endpoints=(),
        secret=None if secret is None else MaskedMapping(secret),
        proxy_url=proxy_url,
    )


def _ok(exchange: Exchange) -> Answer:
    """Answer every request with an empty JSON object.

    Parameters
    ----------
    exchange
        The request.

    Returns
    -------
    Answer
        200 and ``{}``.
    """
    return 200, {"Content-Type": "application/json"}, b"{}"


@pytest.mark.parametrize(
    ("settings", "secret", "header", "value"),
    [
        ({"auth": "bearer"}, {"token": TOKEN}, "authorization", f"Bearer {TOKEN}"),
        (
            {"auth": "basic", "username": "reader"},
            {"password": TOKEN},
            "authorization",
            "Basic " + base64.b64encode(f"reader:{TOKEN}".encode()).decode(),
        ),
        (
            {"auth": "api_key", "api_key_name": "X-Key", "api_key_in": "header"},
            {"api_key": TOKEN},
            "x-key",
            TOKEN,
        ),
    ],
    ids=["bearer", "basic", "api-key-header"],
)
def test_each_header_auth_reaches_the_server(
    authority: Authority,
    tmp_path: Path,
    settings: dict[str, object],
    secret: dict[str, str],
    header: str,
    value: str,
) -> None:
    with (
        scripted(authority, tmp_path, _ok) as server,
        _http.client_for(_conn(server.base, authority, settings, secret)) as client,
    ):
        assert client.get("items").status_code == 200

    (seen,) = server.seen
    assert seen.headers[header] == value


def test_an_api_key_in_the_query_reaches_the_server_and_nothing_else(
    authority: Authority, tmp_path: Path
) -> None:
    settings = {"auth": "api_key", "api_key_name": "key", "api_key_in": "query"}
    with (
        scripted(authority, tmp_path, _ok) as server,
        _http.client_for(_conn(server.base, authority, settings, {"api_key": TOKEN})) as client,
    ):
        client.get("items", params={"page": "2"})

    (seen,) = server.seen
    assert parse_qs(seen.target.split("?", 1)[1]) == {"page": ["2"], "key": [TOKEN]}
    assert TOKEN not in str(client.params)


@pytest.mark.parametrize(
    "url",
    [
        "https://other.test/items",
        "https://127.0.0.2:{port}/items",
        "https://127.0.0.1:1/items",
        "http://127.0.0.1:{port}/items",
        "https://user:pw@127.0.0.1:{port}/items",
    ],
    ids=["host", "another-loopback", "port", "scheme", "userinfo"],
)
@pytest.mark.parametrize("auth", ["none", "bearer", "api_key", "oauth2"])
def test_no_request_leaves_the_base_urls_origin(
    authority: Authority, tmp_path: Path, url: str, auth: str
) -> None:
    """Whatever URL a caller builds, the credential is not sent to another origin: nothing is.

    For ``oauth2`` the token endpoint is on the same server: a refused request fetches no token.
    """
    _state()
    with scripted(authority, tmp_path, Provider()) as server:
        settings, secret = _auth(auth, server.base)
        with _http.client_for(_conn(server.base, authority, settings, secret)) as client:
            port = server.base.rsplit(":", 1)[1]
            with pytest.raises(
                SourceRefused, match=r"would carry the credential of `127\.0\.0\.1`"
            ):
                client.get(url.format(port=port))

    assert server.seen == []


def _auth(kind: str, base: str) -> tuple[dict[str, object], dict[str, str] | None]:
    """Build the settings and secret of one auth kind.

    Parameters
    ----------
    kind
        ``none``, ``bearer``, ``basic``, ``api_key`` (in the query), ``api_key_header`` or
        ``oauth2`` (its token endpoint ``/token`` on `base`).
    base
        The server's base URL.

    Returns
    -------
    tuple
        The settings and the secret (`None` for ``none``).
    """
    if kind == "bearer":
        return {"auth": "bearer"}, {"token": TOKEN}
    if kind == "basic":
        return {"auth": "basic", "username": "reader"}, {"password": TOKEN}
    if kind == "api_key":
        return {"auth": "api_key", "api_key_name": "key", "api_key_in": "query"}, {"api_key": TOKEN}
    if kind == "api_key_header":
        settings = {"auth": "api_key", "api_key_name": "X-Key", "api_key_in": "header"}
        return settings, {"api_key": TOKEN}
    if kind == "oauth2":
        return {**_OAUTH, "token_url": f"{base}/token"}, {"client_secret": CLIENT_SECRET}
    return {"auth": "none"}, None


@pytest.mark.parametrize("auth", ["basic", "bearer", "api_key_header", "api_key", "oauth2"])
def test_a_followed_redirect_to_another_origin_is_refused_before_it_is_sent(
    authority: Authority, tmp_path: Path, auth: str
) -> None:
    """The origin is checked on every request sent, also a redirect a caller lets httpx follow.

    httpx strips only ``Authorization`` from a redirect to another origin; an API key header
    would go with it. The client refuses the hop instead: the server, also ``localhost`` (another
    origin), never sees ``/stolen``.
    """
    _state()

    def answer(exchange: Exchange) -> Answer:
        if exchange.method == "POST":
            return 200, {}, json.dumps({"access_token": ACCESS}).encode()
        if exchange.target == "/stolen":
            return 200, {}, b"{}"
        port = exchange.headers["host"].rsplit(":", 1)[1]
        return 302, {"Location": f"https://localhost:{port}/stolen"}, b""

    with scripted(authority, tmp_path, answer) as server:
        settings, secret = _auth(auth, server.base)
        with (
            _http.client_for(_conn(server.base, authority, settings, secret)) as client,
            pytest.raises(
                SourceRefused, match=r"a request to `localhost` would carry the credential"
            ),
        ):
            client.get("items", follow_redirects=True)

    assert [s.target.split("?")[0] for s in server.seen if s.method == "GET"] == ["/items"]


@pytest.mark.parametrize(
    ("url", "same"),
    [
        ("https://API.test/x", True),
        ("https://api.test:443/x", True),
        ("https://api.test./x", False),
        ("https://api.test:8443/x", False),
        ("https://xn--pi-7ba.test/x", False),
        ("https://\u0430pi.test/x", False),
        ("https://u@api.test/x", False),
        ("not a url\x00", False),
    ],
)
def test_same_origin_compares_scheme_normalised_host_and_port(url: str, same: bool) -> None:
    assert _http.same_origin(url, "https://api.test/v1") is same


@pytest.mark.parametrize("bad", ["a\r\nX-Evil: 1", "tab\there", "naïve", "nul\x00"])
@pytest.mark.parametrize(
    ("settings", "key"),
    [
        ({"auth": "bearer"}, "token"),
        ({"auth": "api_key", "api_key_name": "X-Key", "api_key_in": "header"}, "api_key"),
    ],
)
def test_a_secret_no_header_can_carry_is_refused_without_showing_it(
    authority: Authority, settings: dict[str, object], key: str, bad: str
) -> None:
    conn = _conn("https://api.test", authority, settings, {key: bad})

    with pytest.raises(UsageError) as caught:
        _http.client_for(conn)

    assert f"the secret's `{key}` of `api` holds a line break" in str(caught.value)
    assert bad not in str(caught.value)
    assert caught.value.retryable is False


def test_the_proxy_credential_httpx_derives_is_registered(authority: Authority) -> None:
    state = _state()
    proxy = f"http://lakana:{TOKEN}@egress:3128"

    _http.client_for(_conn("https://api.test", authority, {"auth": "none"}, None, proxy)).close()

    assert base64.b64encode(f"lakana:{TOKEN}".encode()).decode() in state.secrets


def test_the_client_ignores_proxy_variables(
    authority: Authority, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``HTTPS_PROXY`` adds no proxy and ``NO_PROXY`` removes none: the task's proxy only."""
    monkeypatch.setenv("HTTPS_PROXY", "http://wrong.example:1")
    monkeypatch.setenv("NO_PROXY", "*")
    proxy = f"http://lakana:{TOKEN}@egress:3128"

    with (
        _http.client_for(_conn("https://api.test", authority, {"auth": "none"})) as direct,
        _http.client_for(_conn("https://api.test", authority, {}, None, proxy)) as proxied,
    ):
        assert direct._mounts == {} and proxied._mounts == {}
        assert type(direct._transport._inner._pool).__name__ == "ConnectionPool"  # ty: ignore[unresolved-attribute]
        assert type(proxied._transport._inner._pool).__name__ == "HTTPProxy"  # ty: ignore[unresolved-attribute]


# --- OAuth2 client credentials -----------------------------------------------------------------


@dataclass
class Provider:
    """A scripted token endpoint and API on one server.

    Attributes
    ----------
    token_status
        The token endpoint's status.
    token_body
        Its body.
    api_status
        The API's status for a request with the latest token.
    issued
        How many tokens were issued.
    """

    token_status: int = 200
    token_body: bytes | None = None
    api_status: int = 200
    issued: int = 0

    def __call__(self, exchange: Exchange) -> Answer:
        """Answer a token request or an API request.

        Parameters
        ----------
        exchange
            The request.

        Returns
        -------
        Answer
            The scripted answer.
        """
        if exchange.method == "POST":
            self.issued += 1
            body = (
                self.token_body
                or json.dumps(
                    {
                        "access_token": f"{ACCESS}{self.issued}",
                        "token_type": "Bearer",
                        "expires_in": 3600,
                    }
                ).encode()
            )
            return self.token_status, {"Content-Type": "application/json"}, body
        expected = f"Bearer {ACCESS}{self.issued}"
        ok = exchange.headers.get("authorization") == expected
        return (self.api_status if ok else 401), {}, b"{}"


_OAUTH = {"auth": "oauth2", "client_id": "lakana-reader", "scope": "read"}


def _oauth_conn(base: str, authority: Authority) -> Connection:
    """Build an OAuth2 connection whose token endpoint is ``/token`` on the same server.

    Parameters
    ----------
    base
        The server's base URL.
    authority
        Whose CA the connection trusts.

    Returns
    -------
    Connection
        The connection.
    """
    settings = {**_OAUTH, "token_url": f"{base}/token"}
    return _conn(f"{base}/v1", authority, settings, {"client_secret": CLIENT_SECRET})


def test_oauth2_fetches_one_token_for_several_requests(
    authority: Authority, tmp_path: Path
) -> None:
    state = _state()
    provider = Provider()
    with (
        scripted(authority, tmp_path, provider) as server,
        _http.client_for(_oauth_conn(server.base, authority)) as client,
    ):
        statuses = [client.get(f"items/{n}").status_code for n in range(3)]

    assert statuses == [200, 200, 200]
    token_requests = [s for s in server.seen if s.method == "POST"]
    assert len(token_requests) == 1
    form = parse_qs(token_requests[0].body.decode())
    assert form == {
        "grant_type": ["client_credentials"],
        "client_id": ["lakana-reader"],
        "client_secret": [CLIENT_SECRET],
        "scope": ["read"],
    }
    for seen in server.seen:
        if seen.method == "GET":
            assert CLIENT_SECRET not in seen.target + json.dumps(seen.headers)
    assert f"{ACCESS}1" in state.secrets


def test_the_token_host_and_the_api_host_each_get_only_their_own_credential(
    authority: Authority, tmp_path: Path
) -> None:
    """The common shape: ``token_url`` on another host (``localhost``) than the API's."""
    state = _state()
    provider = Provider()
    with scripted(authority, tmp_path, provider) as server:
        port = server.base.rsplit(":", 1)[1]
        settings = {**_OAUTH, "token_url": f"https://localhost:{port}/token"}
        conn = _conn(f"{server.base}/v1", authority, settings, {"client_secret": CLIENT_SECRET})
        with _http.client_for(conn) as client:
            statuses = [client.get(f"items/{n}").status_code for n in range(2)]

    assert statuses == [200, 200]
    login = [s for s in server.seen if s.headers["host"].startswith("localhost:")]
    api = [s for s in server.seen if s.headers["host"].startswith("127.0.0.1:")]
    assert [(s.method, s.target) for s in login] == [("POST", "/token")]
    assert "authorization" not in login[0].headers
    assert [s.method for s in api] == ["GET", "GET"]
    for seen in api:
        assert CLIENT_SECRET not in seen.target + json.dumps(seen.headers) + seen.body.decode()
    assert f"{ACCESS}1" in state.secrets


def _grant(
    answer: dict[str, object] | bytes, now: list[float], status: int = 200
) -> tuple[_http.ClientCredentials, list[httpx.Request]]:
    """Build a grant whose token endpoint is an in-process transport answering `answer`.

    Parameters
    ----------
    answer
        The token endpoint's JSON answer, or its raw body.
    now
        The fake clock: its last value is the time, 0 while empty.
    status
        The token endpoint's status.

    Returns
    -------
    tuple
        The grant and the token requests it sent.
    """
    sent: list[httpx.Request] = []
    body = answer if isinstance(answer, bytes) else json.dumps(answer).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(status, content=body)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    grant = _http.ClientCredentials(
        "api",
        "https://login.test/token",
        "c",
        CLIENT_SECRET,
        None,
        client,
        now=lambda: now[-1] if now else 0.0,
    )
    return grant, sent


@pytest.mark.parametrize("status", [101, 201, 202, 204])
def test_a_token_endpoint_answering_neither_200_nor_a_retried_status_is_refused(
    status: int,
) -> None:
    """A 1xx or a 2xx other than 200 is no token answer; another attempt would get the same."""
    _state()
    grant, _ = _grant({"access_token": ACCESS}, [], status)

    with pytest.raises(SourceRefused) as caught:
        grant.header()

    assert f"the token endpoint `login.test:443` answered HTTP {status}" in str(caught.value)
    assert "check the connection's token_url" in str(caught.value)
    assert caught.value.retryable is False


@pytest.mark.parametrize(
    "expires",
    [
        None,
        "3599.5",
        3599.5,
        -1,
        "-1",
        "abc",
        True,
        float("nan"),
        float("inf"),
        10**400,
        "1" + "0" * 400,
    ],
    ids=[
        "missing",
        "decimal-text",
        "decimal",
        "negative",
        "negative-text",
        "words",
        "bool",
        "nan",
        "infinity",
        "beyond-a-float",
        "beyond-a-float-text",
    ],
)
def test_a_token_without_a_usable_expires_in_is_kept_a_short_while(expires: object) -> None:
    """An unusable ``expires_in`` counts as missing: the token is kept TOKEN_LIFETIME_SECONDS."""
    _state()
    said: dict[str, object] = {"access_token": ACCESS}
    if expires is not None:
        said["expires_in"] = expires
    now: list[float] = []
    grant, sent = _grant(said, now)

    grant.header()
    now.append(_http.TOKEN_LIFETIME_SECONDS - 1)
    grant.header()
    assert len(sent) == 1
    now.append(_http.TOKEN_LIFETIME_SECONDS)
    grant.header()
    assert len(sent) == 2


def test_a_token_that_expires_within_the_leeway_is_fetched_for_each_request() -> None:
    _state()
    grant, sent = _grant({"access_token": ACCESS, "expires_in": 20}, [])

    grant.header()
    grant.header()

    assert len(sent) == 2


def test_oauth2_fetches_again_before_expiry_and_after_invalidate(
    authority: Authority, tmp_path: Path
) -> None:
    times: list[float] = []

    def now() -> float:
        return times[-1] if times else 0.0

    provider = Provider(
        token_body=json.dumps({"access_token": ACCESS, "expires_in": "100"}).encode()
    )
    with (
        scripted(authority, tmp_path, provider) as server,
        httpx.Client(verify=_trust(authority), trust_env=False) as token_client,
    ):
        grant = _http.ClientCredentials(
            "api", f"{server.base}/token", "c", CLIENT_SECRET, None, token_client, now=now
        )
        assert grant.header() == f"Bearer {ACCESS}"
        times.append(69.0)
        grant.header()
        assert provider.issued == 1
        times.append(70.0)
        grant.header()
        assert provider.issued == 2
        grant.invalidate()
        grant.header()
        assert provider.issued == 3
    assert CLIENT_SECRET not in repr(grant)


def _trust(authority: Authority) -> ssl.SSLContext:
    """Trust the test authority, TLS 1.2 or newer.

    Parameters
    ----------
    authority
        The authority.

    Returns
    -------
    ssl.SSLContext
        The context.
    """
    trust = ssl.create_default_context(cafile=str(authority.ca_file))
    trust.minimum_version = ssl.TLSVersion.TLSv1_2
    return trust


@pytest.mark.parametrize(
    ("status", "body", "error", "text"),
    [
        (401, b'{"error": "invalid_client"}', SourceRefused, "refused the client credentials"),
        (400, b'{"error": "invalid_scope"}', SourceRefused, "refused the client credentials"),
        (302, b"", SourceRefused, "answered HTTP 302, which Lakana does not follow or retry"),
        (404, b"", SourceRefused, "answered HTTP 404, which Lakana does not follow or retry"),
        (503, b"", SourceError, "answered HTTP 503"),
        (200, b'{"token": "x"}', UsageError, "answered without an access_token"),
        (200, b"<html>", UsageError, "answered without an access_token"),
        (200, json.dumps({"access_token": "a\r\nb"}).encode(), UsageError, "without an access"),
        (200, b'{"access_token": "abcd", "token_type": "mac"}', UsageError, "of type 'mac'"),
    ],
)
def test_a_token_endpoints_answer_is_classified_by_status_and_shape(
    authority: Authority,
    tmp_path: Path,
    status: int,
    body: bytes,
    error: type[Exception],
    text: str,
) -> None:
    _state()
    provider = Provider(token_status=status, token_body=body or b"{}")
    with (
        scripted(authority, tmp_path, provider) as server,
        _http.client_for(_oauth_conn(server.base, authority)) as client,
    ):
        with pytest.raises(error) as caught:
            client.get("items")
        api_requests = [s for s in server.seen if s.method == "GET"]

    assert text in str(caught.value)
    assert "`api`: the token endpoint `127.0.0.1:" in str(caught.value)
    assert CLIENT_SECRET not in str(caught.value)
    assert "/token" not in str(caught.value)
    assert api_requests == []


def test_a_token_answer_without_a_length_stops_at_its_cap(
    authority: Authority, tmp_path: Path
) -> None:
    """A chunked or unterminated answer is read only up to 64 KiB, never to its end."""

    def endless(exchange: Exchange) -> Answer:
        return 200, {"Content-Type": "application/json"}, itertools.repeat(b" " * 16384)

    _state()
    with (
        scripted(authority, tmp_path, endless) as server,
        _http.client_for(_oauth_conn(server.base, authority)) as client,
        pytest.raises(UsageError, match=r"answered more than 65536 bytes"),
    ):
        client.get("items")


def test_files_read_reads_with_an_oauth2_token(authority: Authority, tmp_path: Path) -> None:
    def answer(exchange: Exchange) -> Answer:
        if exchange.method == "POST":
            return 200, {}, json.dumps({"access_token": ACCESS}).encode()
        if exchange.headers.get("authorization") != f"Bearer {ACCESS}":
            return 401, {}, b""
        return 200, {}, b"id\n1\n2\n"

    with scripted(authority, tmp_path, answer) as server:
        settings = {
            **_OAUTH,
            "token_url": f"{server.base}/token",
            "ca_certificate": authority.ca_file.read_text(),
        }
        body = {
            "name": "api",
            "kind": "http",
            "address": {"base_url": f"{server.base}/v1"},
            "settings": settings,
            "endpoints": [],
            "secret": {"client_secret": CLIENT_SECRET},
        }
        context._set(
            context._TaskState(
                client=_Client(body),  # ty: ignore[invalid-argument-type]
                environment="dev",
                asset="bronze.x",
                declared=frozenset({"api"}),
                connections={"api": {"kind": "http"}},
            )
        )
        table = files.read("api", "export.csv")

    assert table.column("id").to_pylist() == [1, 2]
    assert ACCESS in context.current().secrets


@dataclass(frozen=True)
class _Resource:
    """A fake `client.task_connections` answering one body.

    Attributes
    ----------
    body
        The body.
    """

    body: dict[str, object]

    def get(self, name: str) -> dict[str, object]:
        """Answer the fetch.

        Parameters
        ----------
        name
            The connection's name.

        Returns
        -------
        dict
            The body.
        """
        return self.body


class _Client:
    """A fake SDK client whose `task_connections` answers one body.

    Parameters
    ----------
    body
        The body.
    """

    def __init__(self, body: dict[str, object]) -> None:
        self.task_connections = _Resource(body)


# --- classifying transport failures ------------------------------------------------------------


def _verify_failure() -> httpx.ConnectError:
    """Build httpx's error for a certificate that did not verify.

    Returns
    -------
    httpx.ConnectError
        With the `ssl.SSLCertVerificationError` as its cause.
    """
    error = httpx.ConnectError("tls")
    error.__cause__ = ssl.SSLCertVerificationError("certificate verify failed")
    return error


@pytest.mark.parametrize(
    ("error", "kind", "text"),
    [
        (httpx.ProxyError("403 Forbidden"), SourceRefused, "`api.test` is not a connection of"),
        (httpx.ProxyError("407 Proxy Authentication Required"), SourceRefused, "token expires"),
        (httpx.ProxyError("502 403 Forbidden"), SourceError, "could not open a tunnel"),
        (httpx.ProxyError("Forbidden 403"), SourceError, "(HTTP ?)"),
        (_verify_failure(), SourceRefused, "certificate is not trusted for `api.test`"),
        (httpx.ConnectError("certificate verify failed"), SourceError, "(ConnectError)"),
        (httpx.ReadTimeout("t"), SourceError, "(ReadTimeout)"),
        (httpx.LocalProtocolError("bad header"), UsageError, "refused to send"),
    ],
    ids=[
        "proxy-403",
        "proxy-407",
        "proxy-502-with-403-in-its-words",
        "proxy-words-only",
        "certificate",
        "certificate-only-in-words",
        "timeout",
        "local-protocol",
    ],
)
def test_transport_failures_are_classified_by_type_and_status_not_words(
    error: httpx.HTTPError, kind: type[Exception], text: str
) -> None:
    found = _http.transport_failure("api", "api.test", error)

    assert type(found) is kind
    assert text in str(found)


def test_a_token_endpoint_that_cannot_be_reached_or_trusted_is_named(
    authority: Authority, tmp_path: Path
) -> None:
    _state()
    (tmp_path / "other").mkdir()
    other = make_authority(tmp_path / "other", "Other CA")
    with scripted(other, tmp_path / "other", Provider()) as untrusted:
        conn = _oauth_conn(untrusted.base, authority)
        with _http.client_for(conn) as client, pytest.raises(SourceRefused) as caught:
            client.get("items")
    assert "the server's certificate is not trusted for `127.0.0.1`" in str(caught.value)

    with scripted(authority, tmp_path, _ok) as server:
        base = server.base
    with (
        _http.client_for(_oauth_conn(base, authority)) as client,
        pytest.raises(SourceError) as gone,
    ):
        client.get("items")
    assert type(gone.value) is SourceError
    assert "the request to `127.0.0.1` failed (ConnectError)" in str(gone.value)


@pytest.mark.parametrize(
    ("headers", "body", "error", "text"),
    [
        ({"Content-Length": "70000"}, b"{}", UsageError, "answered more than 65536 bytes"),
        ({"Content-Length": "100"}, b"{}", SourceError, "failed (RemoteProtocolError)"),
    ],
    ids=["announced-too-long", "cut-short"],
)
def test_a_token_answer_too_long_or_cut_short_is_named(
    authority: Authority,
    tmp_path: Path,
    headers: dict[str, str],
    body: bytes,
    error: type[Exception],
    text: str,
) -> None:
    def answer(exchange: Exchange) -> Answer:
        return 200, headers, body

    _state()
    with (
        scripted(authority, tmp_path, answer) as server,
        _http.client_for(_oauth_conn(server.base, authority)) as client,
        pytest.raises(error) as caught,
    ):
        client.get("items")

    assert text in str(caught.value)


def test_the_auth_and_the_grant_show_no_credential(authority: Authority) -> None:
    conn = _oauth_conn("https://api.test", authority)

    with _http.client_for(conn) as client:
        auth = client.auth
        assert isinstance(auth, _http.ConnectionAuth)
        assert repr(auth) == "ConnectionAuth('api')"
        assert repr(auth.oauth) == "ClientCredentials('api', 'https://api.test/token')"


def test_a_proxy_without_a_password_registers_nothing(authority: Authority) -> None:
    state = _state()

    _http.client_for(_conn("https://api.test", authority, {}, None, "http://egress:3128")).close()

    assert state.secrets == set()
