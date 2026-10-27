"""The HTTPS client of an ``http`` connection, for ``files.read`` (and later ``rest.read``).

One client per connection: the task's proxy as the transport's own proxy (never
``HTTPS_PROXY``: ``trust_env=False`` also ignores ``NO_PROXY``, ``.netrc`` and
``SSL_CERT_FILE``), the trust bundle plus the connection's CA through
`lakana.ingest._check.trust_context` (TLS 1.2 or newer), and no redirects. The connection's
credential is added by `ConnectionAuth` to requests for the base URL's scheme, host and port
only, so a credential never leaves the connection's host, whatever URL a caller builds.

Every value derived from a secret that a request carries (a basic-auth header, an OAuth2 access
token, the egress proxy's ``Proxy-Authorization``) is registered with the harness, which hides it
in the task's log: the worker knows the connection's secret, not what is derived from it.
"""

from __future__ import annotations

import base64
import json
import re
import ssl
import time
from collections.abc import Callable, Generator
from typing import TYPE_CHECKING
from urllib.parse import unquote, urlsplit

import httpx

from lakana import context
from lakana.ingest._check import trust_context
from lakana.ingest._errors import (
    IngestError,
    SourceError,
    SourceRefused,
    UsageError,
    missing_secret,
    safe_name,
    task_environment,
)

if TYPE_CHECKING:
    from lakana.ingest.connections import Connection

TIMEOUT_SECONDS = 60.0
"""Seconds per connect, read and write of one request."""
TOKEN_LEEWAY_SECONDS = 30.0
"""An OAuth2 access token is fetched again this long before its ``expires_in`` ends."""
MAX_TOKEN_ANSWER = 64 * 1024
"""The most bytes of a token endpoint's answer read; a larger one is refused."""

_HEADER_SAFE = re.compile(r"[\x20-\x7e]+")
_PROXY_STATUS = re.compile(r"\A(\d{3})\b")


def origin(url: httpx.URL | str) -> tuple[str, bytes, int]:
    """Name the origin of a URL: scheme, host and port, as httpx will connect to them.

    The host is httpx's normalised form (lower case, IDNA-encoded), so ``API.test``,
    ``api.test:443`` and ``api.test`` are one origin, and a Unicode look-alike is not.

    Parameters
    ----------
    url
        An absolute URL.

    Returns
    -------
    tuple
        The scheme, the host as ASCII bytes, the port (443 for ``https`` without one).
    """
    parsed = httpx.URL(url)
    default = 443 if parsed.scheme == "https" else 80
    return parsed.scheme, parsed.raw_host.lower(), parsed.port or default


def same_origin(url: httpx.URL | str, base_url: httpx.URL | str) -> bool:
    """Say whether a URL is on the base URL's scheme, host and port.

    Parameters
    ----------
    url
        The URL a request would go to.
    base_url
        The connection's base URL.

    Returns
    -------
    bool
        True for the same origin; False for another, and for a URL with a user or a password,
        or one httpx cannot read.
    """
    try:
        parsed = httpx.URL(url)
        if parsed.userinfo:
            return False
        return origin(parsed) == origin(base_url)
    except (httpx.InvalidURL, TypeError, ValueError):
        return False


def header_value(name: str, key: str, value: str) -> str:
    """Check that a secret value can be sent in an HTTP header, unchanged.

    Parameters
    ----------
    name
        The connection.
    key
        The secret's key, such as ``token``.
    value
        The value.

    Returns
    -------
    str
        `value`.

    Raises
    ------
    UsageError
        For a value with a line break, another control character or a character outside
        ASCII, which no header can carry (a line break would add a header of its own); the
        value is never shown.
    """
    if _HEADER_SAFE.fullmatch(value) is None:
        raise UsageError(
            f"the secret's `{key}` of `{name}` holds a line break, another control character or "
            "a character outside ASCII, which an HTTP header cannot carry; store it again: "
            f"`lakana connection update {name} --env {task_environment()} --password-stdin`"
        )
    return value


def certificate_refused(error: BaseException) -> bool:
    """Say whether httpx failed because a server's certificate did not verify.

    Parameters
    ----------
    error
        What httpx raised.

    Returns
    -------
    bool
        Whether an `ssl.SSLCertVerificationError` is among its causes.
    """
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        if isinstance(current, ssl.SSLCertVerificationError):
            return True
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return False


def proxy_status(error: httpx.ProxyError) -> int | None:
    """Read the status of the egress proxy's refusal of a tunnel.

    httpcore formats it as ``"<status> <reason>"``; only the leading number, which httpcore
    writes from the status line's code, is read, never the reason's words.

    Parameters
    ----------
    error
        httpx's `ProxyError`.

    Returns
    -------
    int or None
        The status, or `None` when the error carries none.
    """
    found = _PROXY_STATUS.match(str(error))
    return int(found.group(1)) if found else None


def transport_failure(name: str, host: str, error: httpx.HTTPError) -> IngestError:
    """Turn what httpx raised for a request into the error that names the cause and next step.

    Classified by the error's type and the proxy's status code only: a server's or a proxy's
    words decide nothing.

    Parameters
    ----------
    name
        The connection.
    host
        The host the request went to, as the connection names it.
    error
        What httpx raised.

    Returns
    -------
    IngestError
        `SourceRefused` for a certificate that does not verify and for the egress proxy's 403
        (not a destination of this task) and 407 (the task's token refused); `UsageError` for a
        request httpx refused to send; `SourceError` (retried) otherwise.
    """
    environment = task_environment()
    shown = safe_name(host)
    if certificate_refused(error):
        return SourceRefused(
            f"`{name}`: the server's certificate is not trusted for `{shown}`. Give the "
            f"connection your CA: `lakana connection update {name} --env {environment} "
            "--ca-file ca.pem`, or add it to the workers' trust bundle (LAKANA_RUN__CA_BUNDLE)."
        )
    if isinstance(error, httpx.ProxyError):
        status = proxy_status(error)
        if status == 403:
            return SourceRefused(
                f"`{shown}` is not a connection of this asset: declare it in "
                "@asset(connections=[...]) and create it with `lakana connection create`. (An "
                "address inside the Lakana stack or on the Docker host is never reachable from "
                "a task.)"
            )
        if status == 407:
            return SourceRefused(
                f"`{name}`: the task's egress proxy refused the task's token for `{shown}`; the "
                "token expires with the task, so this attempt ran past its time limit"
            )
        return SourceError(
            f"`{name}`: the task's egress proxy could not open a tunnel to `{shown}` (HTTP "
            f"{status or '?'})"
        )
    if isinstance(error, httpx.LocalProtocolError | httpx.UnsupportedProtocol):
        return UsageError(
            f"`{name}`: httpx refused to send a request to `{shown}` "
            f"({type(error).__name__}); check the connection's settings"
        )
    return SourceError(f"`{name}`: the request to `{shown}` failed ({type(error).__name__})")


def read_capped(response: httpx.Response, limit: int) -> bytes:
    """Read a streamed answer's body, refusing one larger than `limit` before and while reading.

    ``Content-Length`` is checked first (with a content encoding it is the encoded size, which
    is never larger than the decoded one); then the decoded bytes are counted as they arrive, so
    an answer without a length, or one that decodes to more, stops at the limit.

    Parameters
    ----------
    response
        A response opened with ``stream=True``.
    limit
        The most bytes.

    Returns
    -------
    bytes
        The body, decoded.

    Raises
    ------
    OverflowError
        When the body is larger than `limit`; the caller names its own cap.
    """
    length = response.headers.get("content-length", "")
    if length.isdigit() and int(length) > limit:
        raise OverflowError(limit)
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_bytes():
        total += len(chunk)
        if total > limit:
            raise OverflowError(limit)
        chunks.append(chunk)
    return b"".join(chunks)


class ClientCredentials:
    """The OAuth2 client-credentials grant of a connection: one token, cached, fetched again.

    The client secret goes to the connection's ``token_url`` only, in the form body
    (``client_secret_post``); the token is kept until ``expires_in`` minus
    `TOKEN_LEEWAY_SECONDS` (for the attempt when the answer has no ``expires_in``) and fetched
    again after `invalidate`.

    Parameters
    ----------
    name
        The connection, for messages.
    token_url
        The connection's ``token_url``.
    client_id
        The connection's ``client_id``.
    client_secret
        The secret's ``client_secret``.
    scope
        The connection's ``scope``, or `None`.
    client
        The client the token request goes through: the connection's transport, no auth, no
        redirects.
    now
        A monotonic clock; `time.monotonic` outside tests.
    """

    def __init__(
        self,
        name: str,
        token_url: str,
        client_id: str,
        client_secret: str,
        scope: str | None,
        client: httpx.Client,
        *,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self.name = name
        self.token_url = token_url
        self._form = {"grant_type": "client_credentials", "client_id": client_id}
        self._client_secret = client_secret
        if scope:
            self._form["scope"] = scope
        self._client = client
        self._now = now
        self._token: str | None = None
        self._until = 0.0

    def __repr__(self) -> str:
        """Name the token endpoint, never the secret or the token.

        Returns
        -------
        str
            Such as ``ClientCredentials('api', 'https://login.test/token')``.
        """
        return f"ClientCredentials({self.name!r}, {self.token_url!r})"

    def header(self) -> str:
        """Return the ``Authorization`` value, fetching a token when none is valid.

        Returns
        -------
        str
            ``Bearer <token>``.
        """
        if self._token is None or self._now() >= self._until:
            self._fetch()
        return f"Bearer {self._token}"

    def invalidate(self) -> None:
        """Forget the token, so the next request fetches a new one (after a 401)."""
        self._token = None

    def _where(self) -> str:
        """Name the token endpoint's host and port, never its path or query.

        Returns
        -------
        str
            Such as ``login.test:443``.
        """
        _, host, port = origin(self.token_url)
        return safe_name(f"{host.decode('ascii')}:{port}")

    def _fetch(self) -> None:
        """Ask the token endpoint for a token and keep it.

        Raises
        ------
        SourceRefused
            When the endpoint refuses the client credentials (HTTP 400 or 401), answers with a
            redirect or another 4xx, or its certificate is not trusted.
        UsageError
            When it answers 200 without a usable bearer token: the connection's ``token_url``
            is not a token endpoint.
        SourceError
            When it cannot be reached or answers 5xx or 429, which may pass.
        """
        where = self._where()
        environment = task_environment()
        request = self._client.build_request(
            "POST",
            self.token_url,
            data={**self._form, "client_secret": self._client_secret},
            headers={"Accept": "application/json"},
        )
        try:
            response = self._client.send(request, stream=True)
        except httpx.HTTPError as exc:
            failure = transport_failure(self.name, where.rsplit(":", 1)[0], exc)
            raise failure from None
        try:
            status = response.status_code
            if status in (400, 401):
                raise SourceRefused(
                    f"`{self.name}`: the token endpoint `{where}` refused the client credentials "
                    f"(HTTP {status}). Check client_id and the secret with `lakana connection "
                    f"test {self.name} --env {environment}`."
                )
            if 300 <= status < 400 or (400 <= status < 500 and status not in (408, 429)):
                raise SourceRefused(
                    f"`{self.name}`: the token endpoint `{where}` answered HTTP {status}, which "
                    "Lakana does not follow or retry; check the connection's token_url"
                )
            if status != 200:
                raise SourceError(
                    f"`{self.name}`: the token endpoint `{where}` answered HTTP {status}"
                )
            try:
                body = read_capped(response, MAX_TOKEN_ANSWER)
            except OverflowError:
                raise UsageError(
                    f"`{self.name}`: the token endpoint `{where}` answered more than "
                    f"{MAX_TOKEN_ANSWER} bytes; check the connection's token_url"
                ) from None
            except httpx.HTTPError as exc:
                failure = transport_failure(self.name, where.rsplit(":", 1)[0], exc)
                raise failure from None
        finally:
            response.close()
        self._keep(body, where)

    def _keep(self, body: bytes, where: str) -> None:
        """Read the token answer and keep its token.

        Parameters
        ----------
        body
            The answer's body.
        where
            The endpoint's host and port, for messages.

        Raises
        ------
        UsageError
            For an answer that is no JSON object with a bearer ``access_token`` that a header
            can carry.
        """
        try:
            said = json.loads(body)
        except (ValueError, RecursionError):
            said = None
        token = said.get("access_token") if isinstance(said, dict) else None
        kind = said.get("token_type", "bearer") if isinstance(said, dict) else "bearer"
        if not isinstance(token, str) or not token or _HEADER_SAFE.fullmatch(token) is None:
            raise UsageError(
                f"`{self.name}`: the token endpoint `{where}` answered without an access_token "
                "a request can carry; check the connection's token_url"
            )
        if not isinstance(kind, str) or kind.lower() != "bearer":
            raise UsageError(
                f"`{self.name}`: the token endpoint `{where}` gave a token of type "
                f"{safe_name(str(kind))[:40]!r}; Lakana sends bearer tokens only"
            )
        context.register_secret(token)
        expires = said.get("expires_in") if isinstance(said, dict) else None
        if isinstance(expires, str) and expires.isdigit():
            expires = int(expires)
        if isinstance(expires, int | float) and not isinstance(expires, bool):
            self._until = self._now() + float(expires) - TOKEN_LEEWAY_SECONDS
        else:
            self._until = float("inf")
        self._token = token


class ConnectionAuth(httpx.Auth):
    """Adds a connection's credential to requests for its base URL's origin, and to no other.

    A request for another scheme, host or port is refused before it is sent, so neither a
    header, an API key in the query nor a token can leave the connection's host, whatever URL a
    caller or a server's next link names.

    Parameters
    ----------
    name
        The connection, for messages.
    base_url
        The connection's base URL.
    headers
        Headers to add (bearer, basic, API key).
    params
        Query parameters to add (an API key in the query).
    oauth
        The OAuth2 grant, or `None`.
    """

    def __init__(
        self,
        name: str,
        base_url: str,
        headers: dict[str, str],
        params: dict[str, str],
        oauth: ClientCredentials | None,
    ) -> None:
        self.name = name
        self.base_url = base_url
        self._headers = headers
        self.params = params
        self.oauth = oauth

    def __repr__(self) -> str:
        """Name the connection, never a credential.

        Returns
        -------
        str
            Such as ``ConnectionAuth('api')``.
        """
        return f"ConnectionAuth({self.name!r})"

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        """Add the credential and send the request, once.

        Parameters
        ----------
        request
            The request.

        Yields
        ------
        httpx.Request
            The request, with the credential.

        Raises
        ------
        SourceRefused
            For a request to another origin than the base URL's.
        """
        if not same_origin(request.url, self.base_url):
            _, host, _ = origin(self.base_url)
            raise SourceRefused(
                f"`{self.name}`: a request to `{safe_name(request.url.host)}` would carry the "
                f"credential of `{host.decode('ascii')}`; Lakana sends it to the connection's own "
                "host only"
            )
        for key, value in self._headers.items():
            request.headers[key] = value
        if self.params:
            request.url = request.url.copy_merge_params(self.params)
        if self.oauth is not None:
            request.headers["Authorization"] = self.oauth.header()
        yield request


def client_for(conn: Connection) -> httpx.Client:
    """Build the client of an ``http`` connection.

    Parameters
    ----------
    conn
        The connection, with its secret and the task's proxy.

    Returns
    -------
    httpx.Client
        A client whose ``base_url`` is the connection's and whose ``auth`` is a
        `ConnectionAuth`; the caller closes it.

    Raises
    ------
    UsageError
        For a missing secret, a secret value no header can carry, a CA certificate that is no
        PEM certificate, or a trust bundle this task cannot read.
    """
    auth_mode = str(conn.settings.get("auth", "none"))
    try:
        verify = trust_context(_ca(conn))
    except ssl.SSLError:
        raise UsageError(
            f"the CA certificate of `{conn.name}` or the workers' trust bundle holds no PEM "
            f"certificate; give the connection your CA: `lakana connection update {conn.name} "
            f"--env {task_environment()} --ca-file ca.pem`, and check LAKANA_RUN__CA_BUNDLE"
        ) from None
    except OSError as exc:
        raise UsageError(
            f"the trust bundle (LAKANA_CA_BUNDLE) cannot be read ({exc.strerror}); check the "
            "worker's LAKANA_RUN__CA_BUNDLE"
        ) from None
    base_url = str(conn.address["base_url"])
    headers: dict[str, str] = {}
    params: dict[str, str] = {}
    secret: str | None = None
    if auth_mode == "bearer":
        token = header_value(conn.name, "token", _secret(conn, "token"))
        headers["Authorization"] = f"Bearer {token}"
    elif auth_mode == "basic":
        user, password = str(conn.settings["username"]), _secret(conn, "password")
        value = base64.b64encode(f"{user}:{password}".encode()).decode()
        # The header carries a value derived from the secret, which the worker cannot know.
        context.register_secret(value)
        headers["Authorization"] = f"Basic {value}"
    elif auth_mode == "api_key":
        name, value = str(conn.settings["api_key_name"]), _secret(conn, "api_key")
        if conn.settings.get("api_key_in") == "query":
            params[name] = value
        else:
            headers[name] = header_value(conn.name, "api_key", value)
    elif auth_mode == "oauth2":
        secret = _secret(conn, "client_secret")
    transport = httpx.HTTPTransport(verify=verify, proxy=conn.proxy_url, retries=0)
    if conn.proxy_url:
        parts = urlsplit(conn.proxy_url)
        if parts.password:
            # httpx sends Basic base64("user:token") to the proxy: a form the harness must hide.
            pair = f"{unquote(parts.username or '')}:{unquote(parts.password)}"
            context.register_secret(base64.b64encode(pair.encode()).decode())
    oauth: ClientCredentials | None = None
    if secret is not None:
        token_client = httpx.Client(
            transport=transport, trust_env=False, follow_redirects=False, timeout=TIMEOUT_SECONDS
        )
        oauth = ClientCredentials(
            conn.name,
            str(conn.settings["token_url"]),
            str(conn.settings["client_id"]),
            secret,
            str(conn.settings["scope"]) if conn.settings.get("scope") else None,
            token_client,
        )
    return httpx.Client(
        base_url=base_url,
        transport=transport,
        trust_env=False,
        auth=ConnectionAuth(conn.name, base_url, headers, params, oauth),
        follow_redirects=False,
        timeout=TIMEOUT_SECONDS,
    )


def _ca(conn: Connection) -> str | None:
    """Read the connection's own CA certificates.

    Parameters
    ----------
    conn
        The connection.

    Returns
    -------
    str or None
        PEM, or `None`.
    """
    value = conn.settings.get("ca_certificate")
    return value if isinstance(value, str) and value else None


def _secret(conn: Connection, key: str) -> str:
    """Read one value of the connection's secret.

    Parameters
    ----------
    conn
        The connection.
    key
        The key its auth needs.

    Returns
    -------
    str
        The value.

    Raises
    ------
    UsageError
        When the secret is missing, naming the command that binds one.
    """
    if conn.secret is None or key not in conn.secret:
        error = missing_secret(conn.name, key, "--password-stdin")
        raise error
    return conn.secret[key]
