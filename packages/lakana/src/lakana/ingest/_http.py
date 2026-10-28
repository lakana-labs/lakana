"""The HTTPS client of an ``http`` connection and its retries, for ``files.read`` and ``rest.read``.

One client per connection: the task's proxy as the transport's own proxy (never
``HTTPS_PROXY``: ``trust_env=False`` also ignores ``NO_PROXY``, ``.netrc`` and
``SSL_CERT_FILE``), the trust bundle plus the connection's CA through
`lakana.ingest._check.trust_context` (TLS 1.2 or newer), and no redirects. The connection's
credential is added by `ConnectionAuth` to requests for the base URL's scheme, host and port
only, and the client's transport (`OwnOrigin`) refuses every request it would send anywhere else,
a redirect a caller lets httpx follow included: a credential never leaves the connection's host,
whatever URL a caller or a server names.

Every value derived from a secret that a request carries (a basic-auth header, an OAuth2 access
token, the egress proxy's ``Proxy-Authorization``) is registered with the harness, which hides it
in the task's log: the worker knows the connection's secret, not what is derived from it.
"""

from __future__ import annotations

import base64
import datetime as dt
import email.utils
import json
import math
import random
import re
import ssl
import time
import zlib
from collections.abc import Callable, Generator, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import parse_qsl, unquote, urlsplit

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
TOKEN_LIFETIME_SECONDS = 300.0
"""How long a token is kept when its answer has no ``expires_in`` that is a whole, non-negative
number of seconds."""
MAX_TOKEN_ANSWER = 64 * 1024
"""The most bytes of a token endpoint's answer read; a larger one is refused."""

SHOWN_HOST = 100
"""The most characters of a host a message shows."""
RETRIED_STATUSES = frozenset({408, 429, 500, 502, 503, 504})
"""Statuses `send` retries: a timeout, too many requests, and a server that may recover."""
MAX_DELAY_SECONDS = 60.0
"""The longest backoff between two attempts (a server's ``Retry-After`` may ask for more)."""

ACCEPT_ENCODING = "gzip, deflate"
"""The encodings a client asks for; `read_capped` decodes each in bounded steps and refuses any
other, so no answer is ever decoded faster than the cap counts it."""
DECODE_STEP = 64 * 1024
"""The most decoded bytes one step of `read_capped` produces."""
_WBITS = {"gzip": 16 + zlib.MAX_WBITS, "x-gzip": 16 + zlib.MAX_WBITS, "deflate": zlib.MAX_WBITS}
"""The zlib window of each encoding `read_capped` decodes (deflate: the zlib format first)."""


class _CutShort(Exception):
    """A gzip or deflate body that ends before its compressed stream does."""


_HEADER_SAFE = re.compile(r"[\x20-\x7e]+")
_PROXY_STATUS = re.compile(r"\A(\d{3})\b")
_sleep: Callable[[float], None] = time.sleep
_clock: Callable[[], float] = time.monotonic
_rand: Callable[[], float] = random.random


def _wall() -> dt.datetime:
    """Read the wall clock, for a ``Retry-After`` that is an HTTP date.

    Returns
    -------
    datetime
        Now, in UTC.
    """
    return dt.datetime.now(dt.UTC)


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


def _counted(raw: Iterable[bytes], limit: int) -> Generator[bytes]:
    """Pass a body's raw bytes on, refusing more than `limit` of them.

    Parameters
    ----------
    raw
        The body as it arrives.
    limit
        The most bytes.

    Yields
    ------
    bytes
        Each chunk.

    Raises
    ------
    OverflowError
        Past `limit` raw bytes (as ``Content-Length`` is checked, the encoded size).
    """
    total = 0
    for chunk in raw:
        total += len(chunk)
        if total > limit:
            raise OverflowError(limit)
        yield chunk


def _inflated(raw: Iterable[bytes], encoding: str) -> Generator[bytes]:
    """Decode a gzip or deflate body in steps of at most `DECODE_STEP` bytes.

    A gzip body may hold several members, each decoded by a fresh decoder; any other byte after
    the end of the stream is refused, so nothing is kept beside the decoder uncounted. A
    ``deflate`` body is the zlib format, or raw deflate when its first bytes are no zlib header
    (as httpx read it).

    Parameters
    ----------
    raw
        The body as it arrives.
    encoding
        ``gzip``, ``x-gzip`` or ``deflate``.

    Yields
    ------
    bytes
        The next decoded bytes, possibly none.

    Raises
    ------
    zlib.error
        When the bytes are not of the encoding, or bytes follow a deflate stream.
    _CutShort
        When the body ends inside a stream.
    """
    wbits = _WBITS[encoding]
    inflater = zlib.decompressobj(wbits)
    first = True
    for data in raw:
        decoded = b""
        while data or (len(decoded) == DECODE_STEP and not inflater.eof):
            if data and inflater.eof:
                if encoding == "deflate":
                    raise zlib.error("bytes after the end of the compressed stream")
                inflater = zlib.decompressobj(wbits)  # the next gzip member
            try:
                decoded = inflater.decompress(data, DECODE_STEP)
            except zlib.error:
                if not (first and encoding == "deflate"):
                    raise
                wbits = -zlib.MAX_WBITS
                inflater = zlib.decompressobj(wbits)
                decoded = inflater.decompress(data, DECODE_STEP)
            first = False
            yield decoded
            data = inflater.unconsumed_tail or inflater.unused_data
    if not first and not inflater.eof:  # no byte at all is an empty body, not a cut one
        raise _CutShort(encoding)


def _within(body: bytes, limit: int) -> bytes:
    """Return a body already in memory, unless it is larger than `limit`.

    Parameters
    ----------
    body
        The body.
    limit
        The most bytes.

    Returns
    -------
    bytes
        `body`.

    Raises
    ------
    OverflowError
        When it is larger.
    """
    if len(body) > limit:
        raise OverflowError(limit)
    return body


def read_capped(response: httpx.Response, limit: int, name: str) -> bytes:
    """Read a streamed answer's body, refusing one larger than `limit` before and while reading.

    ``Content-Length`` is checked first (with a content encoding it is the encoded size). Then
    the raw bytes are counted against `limit` as they arrive, and decoded here, not by httpx, in
    steps of at most `DECODE_STEP` bytes, each counted against `limit` as it is produced: a small
    compressed body that decodes to far more stops at the limit holding at most one step more,
    and bytes after the end of a compressed stream are never piled up uncounted. Only the
    encodings of `ACCEPT_ENCODING` are decoded; any other is refused.

    Parameters
    ----------
    response
        A response opened with ``stream=True``.
    limit
        The most bytes.
    name
        The connection, for the refusal.

    Returns
    -------
    bytes
        The body, decoded.

    Raises
    ------
    OverflowError
        When the body is larger than `limit`; the caller names its own cap.
    SourceRefused
        When the answer is in a ``Content-Encoding`` Lakana did not ask for (``zstd``, ``br``,
        or more than one), or its body is not of its encoding (bytes after a deflate stream or
        that start no gzip member included): every attempt would read the same.
    httpx.DecodingError
        When the body ends inside its compressed stream (retried: a cut is usually transient).
    """
    length = response.headers.get("content-length", "")
    if length.isdigit() and int(length) > limit:
        raise OverflowError(limit)
    encoding = response.headers.get("content-encoding", "").strip().lower()
    if encoding not in ("", "identity") and encoding not in _WBITS:
        raise SourceRefused(
            f"`{name}`: the server answered in Content-Encoding "
            f"`{safe_name(encoding)[:SHOWN_HOST]}`, which Lakana does not ask for (it asks for "
            "gzip or deflate, or none); the server or a proxy before it ignores Accept-Encoding: "
            "have it send one of those"
        )
    if response.is_stream_consumed:
        # An answer built in memory (httpx's MockTransport) was decoded whole when it was built;
        # an answer from the network is a stream, decoded only here.
        return _within(response.content, limit)
    raw = _counted(response.iter_raw(), limit)
    chunks = _inflated(raw, encoding) if encoding in _WBITS else raw
    kept: list[bytes] = []
    total = 0
    try:
        for chunk in chunks:
            total += len(chunk)
            if total > limit:
                raise OverflowError(limit)
            kept.append(chunk)
    except zlib.error as exc:
        raise SourceRefused(
            f"`{name}`: the answer's body is not valid {encoding} ({exc}); every attempt would "
            "read the same bytes, so it is not retried: have the server send a well-formed body, "
            "or none encoded"
        ) from None
    except _CutShort:
        raise httpx.DecodingError(
            f"the {encoding} body ends before its compressed stream does", request=response.request
        ) from None
    return b"".join(kept)


class ClientCredentials:
    """The OAuth2 client-credentials grant of a connection: one token, cached, fetched again.

    The client secret goes to the connection's ``token_url`` only, in the form body
    (``client_secret_post``); the token is kept until ``expires_in`` minus
    `TOKEN_LEEWAY_SECONDS` (so a token valid for 30 seconds or less is fetched for every request),
    for `TOKEN_LIFETIME_SECONDS` when the answer has no usable ``expires_in``, and fetched again
    after `invalidate`.

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
            redirect, another 4xx, a 1xx or a 2xx other than 200, or its certificate is not
            trusted.
        UsageError
            When it answers 200 without a usable bearer token: the connection's ``token_url``
            is not a token endpoint.
        SourceError
            When it cannot be reached or answers 5xx, 408 or 429, which may pass.
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
            if status != 200 and status < 500 and status not in (408, 429):
                raise SourceRefused(
                    f"`{self.name}`: the token endpoint `{where}` answered HTTP {status}, which "
                    "Lakana does not follow or retry; check the connection's token_url"
                )
            if status != 200:
                raise SourceError(
                    f"`{self.name}`: the token endpoint `{where}` answered HTTP {status}"
                )
            try:
                body = read_capped(response, MAX_TOKEN_ANSWER, self.name)
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
            # Decoded here: json.loads would guess UTF-16 or UTF-32 from bytes; JSON is UTF-8,
            # with or without the byte order mark some providers send (as rest.read accepts).
            said = json.loads(body.removeprefix(b"\xef\xbb\xbf").decode("utf-8"))
        except (ValueError, RecursionError):
            said = None
        if not isinstance(said, dict):
            said = {}
        token = said.get("access_token")
        kind = said.get("token_type", "bearer")
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
        expires = _seconds(said.get("expires_in"))
        if expires is None:
            self._until = self._now() + TOKEN_LIFETIME_SECONDS
        else:
            self._until = self._now() + expires - TOKEN_LEEWAY_SECONDS
        self._token = token


def _seconds(value: object) -> float | None:
    """Read an ``expires_in``: a whole, non-negative number of seconds, or its digits as text.

    Parameters
    ----------
    value
        What the token answer gave.

    Returns
    -------
    float or None
        The seconds; `None` for anything else (a fraction, a sign, words, a bool, NaN, a number
        no float holds).
    """
    whole = isinstance(value, int) and not isinstance(value, bool) and value >= 0
    if not whole and not (isinstance(value, str) and value.isascii() and value.isdigit()):
        return None
    try:
        seconds = float(value)
    except OverflowError:
        return None
    return seconds if math.isfinite(seconds) else None


def _elsewhere(name: str, url: httpx.URL, base_url: str) -> SourceRefused:
    """Build the refusal of a request to another origin than the connection's.

    Parameters
    ----------
    name
        The connection.
    url
        Where the request would go.
    base_url
        The connection's base URL.

    Returns
    -------
    SourceRefused
        Naming both hosts, never a credential or a path; as ``scheme://host:port`` when only
        the scheme, the port or the URL's user and password differ.
    """
    there, userinfo, here = shown_origins(url, base_url)
    return SourceRefused(
        f"`{name}`: a request to `{there}`{userinfo} would carry the credential of "
        f"`{here}`; Lakana sends it to the connection's own host only"
    )


def shown_origins(url: httpx.URL, base_url: str) -> tuple[str, str, str]:
    """Show where a refused request would go and the connection's origin, as far as they differ.

    Parameters
    ----------
    url
        Where the request would go.
    base_url
        The connection's base URL.

    Returns
    -------
    tuple
        Its host, a note (``" with a user or a password in its URL"`` or ``""``) and the base
        URL's host; both as ``scheme://host:port`` when the hosts are the same. Hosts are
        escaped and cut to `SHOWN_HOST` characters.
    """
    scheme, host, port = origin(base_url)
    there, here = safe_name(url.host)[:SHOWN_HOST], host.decode("ascii")[:SHOWN_HOST]
    note = ""
    if origin(url)[1] == host:
        there, here = _full(url.scheme, there, url.port), _full(scheme, here, port)
        note = " with a user or a password in its URL" if url.userinfo else ""
    return there, note, here


def _full(scheme: str, host: str, port: int | None) -> str:
    """Write an origin as ``scheme://host:port``.

    Parameters
    ----------
    scheme
        The scheme, escaped by the caller where it came from a URL.
    host
        The host, escaped by the caller where it came from a URL.
    port
        The port; `None` for the scheme's default.

    Returns
    -------
    str
        Such as ``https://api.test:443`` (an IPv6 host in brackets).
    """
    default = 443 if scheme == "https" else 80
    shown = f"[{host}]" if ":" in host else host
    return f"{safe_name(scheme)}://{shown}:{port or default}"


class OwnOrigin(httpx.BaseTransport):
    """The transport of a connection's client: refuses every request to another origin.

    httpx runs an `httpx.Auth` once per ``send``, and a redirect it follows inside that ``send``
    keeps every header but ``Authorization`` (an API key header would go along). This transport
    sees each request httpx sends, so a hop to another origin is refused before it is sent,
    whether or not a caller lets httpx follow redirects.

    Parameters
    ----------
    name
        The connection, for messages.
    base_url
        The connection's base URL.
    inner
        The transport that sends (the task's proxy, the trust context).
    """

    def __init__(self, name: str, base_url: str, inner: httpx.BaseTransport) -> None:
        self.name = name
        self.base_url = base_url
        self._inner = inner

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        """Send a request for the base URL's origin; refuse any other.

        Parameters
        ----------
        request
            The request httpx is about to send.

        Returns
        -------
        httpx.Response
            The inner transport's response.

        Raises
        ------
        SourceRefused
            For a request to another origin than the base URL's.
        """
        if not same_origin(request.url, self.base_url):
            refusal = _elsewhere(self.name, request.url, self.base_url)
            raise refusal
        return self._inner.handle_request(request)

    def close(self) -> None:
        """Close the inner transport."""
        self._inner.close()


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
            refusal = _elsewhere(self.name, request.url, self.base_url)
            raise refusal
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
        A client whose ``base_url`` is the connection's, whose ``auth`` is a `ConnectionAuth`
        and whose transport is an `OwnOrigin`; the caller closes it.

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
            transport=transport,
            headers={"Accept-Encoding": ACCEPT_ENCODING},
            trust_env=False,
            follow_redirects=False,
            timeout=TIMEOUT_SECONDS,
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
        transport=OwnOrigin(conn.name, base_url, transport),
        headers={"Accept-Encoding": ACCEPT_ENCODING},
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


def shown_url(url: httpx.URL | str) -> str:
    """Show a URL in a message: its user, password, fragment and query values removed.

    Parameters
    ----------
    url
        The URL.

    Returns
    -------
    str
        Such as ``https://api.test/v1/items?page=…&key=…``, control characters escaped.
    """
    try:
        # Escaped first: urlsplit drops line feeds and tabs, which would hide them.
        parts = urlsplit(safe_name(str(url)))
        host = parts.hostname or ""
        port = f":{parts.port}" if parts.port is not None else ""
    except ValueError:
        return "<a URL that cannot be read>"
    query = "&".join(f"{key}=…" for key, _ in parse_qsl(parts.query, keep_blank_values=True))
    return f"{parts.scheme}://{host}{port}{parts.path}" + (f"?{query}" if query else "")


def retry_delay(
    attempt: int, retry_after: str | None, *, rand: Callable[[], float], wall: dt.datetime
) -> float:
    """How long to wait before the next attempt.

    Parameters
    ----------
    attempt
        How many attempts failed so far, less one: 0 after the first.
    retry_after
        The server's ``Retry-After``, if any: seconds or an HTTP date.
    rand
        A number in [0, 1) (jitter); `random.random` outside tests.
    wall
        The current time, UTC, for an HTTP date.

    Returns
    -------
    float
        Seconds: the server's own value when it sent a valid one (0 for a date in the past),
        else 0.5 * 2**attempt capped at `MAX_DELAY_SECONDS`, in the upper half of that range.
    """
    if retry_after is not None:
        text = retry_after.strip()
        if text.isascii() and text.isdigit():
            return float(text)
        try:
            when = email.utils.parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError):
            when = None
        if when is not None and when.tzinfo is not None:
            return max(0.0, (when - wall).total_seconds())
    ceiling = min(MAX_DELAY_SECONDS, 0.5 * 2 ** min(attempt, 16))
    return ceiling / 2 + rand() * ceiling / 2


@dataclass(frozen=True)
class Answer:
    """An answer `send` read.

    Parameters
    ----------
    status
        The status.
    headers
        The headers.
    body
        The body, decoded; empty for a status other than 2xx (never read).
    url
        The URL the request went to.
    """

    status: int
    headers: httpx.Headers
    body: bytes
    url: httpx.URL


def _transient(error: httpx.HTTPError) -> bool:
    """Say whether another attempt of a failed request could succeed.

    Parameters
    ----------
    error
        What httpx raised.

    Returns
    -------
    bool
        True for a timeout, a network or protocol error, an answer that does not decode, and
        the egress proxy's 502, 503 or 504; never for a certificate that does not verify.
    """
    if certificate_refused(error):
        return False
    if isinstance(error, httpx.ProxyError):
        return proxy_status(error) in (502, 503, 504)
    return isinstance(
        error,
        httpx.TimeoutException
        | httpx.NetworkError
        | httpx.RemoteProtocolError
        | httpx.DecodingError,
    )


def send(
    client: httpx.Client,
    request: httpx.Request,
    *,
    name: str,
    limit: int,
    retries: int,
    budget: float,
) -> Answer:
    """Send a request, retrying what may pass within a number of attempts and a time budget.

    Retried: a transient error (`_transient`) and the statuses of `RETRIED_STATUSES`, after a
    backoff with jitter, or after the server's ``Retry-After``. A request is not retried when
    the next wait would end past `budget` seconds from the first attempt. An OAuth2 connection
    answered 401 fetches a new token and sends the request once more (not counted as a retry).
    A 2xx answer's body is read up to `limit` bytes; another status is returned without its
    body, which is never read.

    Parameters
    ----------
    client
        The connection's client (`client_for`).
    request
        The request, for the base URL's origin.
    name
        The connection, for messages.
    limit
        The most bytes of the body read.
    retries
        How many times a request may be sent again.
    budget
        Seconds from the first attempt within which another attempt may start.

    Returns
    -------
    Answer
        The answer.

    Raises
    ------
    SourceError
        When the retries or the budget are spent (retried by the harness), or for an error of
        `transport_failure` that is not transient (`SourceRefused`, `UsageError`).
    OverflowError
        When a 2xx body is larger than `limit`.
    """
    start, attempt, refreshed = _clock(), 0, False
    host = request.url.host
    auth = client.auth
    oauth = auth.oauth if isinstance(auth, ConnectionAuth) else None
    while True:
        retry_after: str | None = None
        try:
            response = client.send(request, stream=True)
        except httpx.HTTPError as exc:
            if not _transient(exc):
                failure = transport_failure(name, host, exc)
                raise failure from None
            happened = f"failed ({type(exc).__name__})"
        else:
            try:
                status = response.status_code
                if status == 401 and oauth is not None and not refreshed:
                    oauth.invalidate()
                    refreshed = True
                    continue
                if status not in RETRIED_STATUSES:
                    body = read_capped(response, limit, name) if 200 <= status < 300 else b""
                    return Answer(status, response.headers, body, request.url)
                happened = f"answered {status}"
                retry_after = response.headers.get("retry-after")
            except httpx.HTTPError as exc:
                if not _transient(exc):
                    failure = transport_failure(name, host, exc)
                    raise failure from None
                happened = f"failed while answering ({type(exc).__name__})"
            finally:
                response.close()
        attempt += 1
        delay = retry_delay(attempt - 1, retry_after, rand=_rand, wall=_wall())
        spent = _clock() - start
        if attempt > retries or spent + delay > budget:
            raise SourceError(
                f"`{name}`: {request.method} {shown_url(request.url)} {happened} after {attempt} "
                f"attempt{'' if attempt == 1 else 's'} in {round(spent)} s; the API is unavailable "
                "— try again later, or raise retries= or retry_budget_seconds="
            )
        _sleep(delay)
