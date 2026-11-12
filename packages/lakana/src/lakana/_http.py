"""HTTP transport: request construction, retries, and error mapping.

Requests are always sent with paths relative to the underlying
`httpx.Client`'s own `base_url` — never as absolute URLs built from
`config.url` — so a caller-supplied `httpx.Client` (for example Starlette's
`TestClient`, whose `base_url` is `http://testserver`) is used exactly as
given.
"""

from __future__ import annotations

import contextlib
import re
import time
from collections.abc import Callable, Generator, Iterator, Mapping, Sequence
from typing import Any, Protocol, TypeVar, cast
from urllib.parse import quote

import httpx

from lakana._config import ResolvedConfig
from lakana._routes import Route
from lakana.errors import (
    InvalidResponse,
    LakanaConnectionError,
    LakanaError,
    ValidationError,
    raise_for_response,
)

_RETRY_BACKOFFS_SECONDS: tuple[float, ...] = (0.0, 0.2, 0.4)
_FORBIDDEN_PATH_PARAM_VALUES = frozenset({"", ".", "..", "/"})

BUSY_ATTEMPTS = 5
"""Times a request is sent when the server answers 503 with its busy answer (a file upload: any
503; beginning an upload: its own busy 503; a result page: its `ResultPageLimitError`)."""

BUSY_BACKOFF_SECONDS = 1.0
"""Seconds waited after a 503 before sending again when the answer has no usable `Retry-After`."""

RETRY_AFTER_CAP_SECONDS = 10.0
"""Longest wait a `Retry-After` can make before a request is sent again (the server's
storage-unavailable answer says 5)."""


def wait(seconds: float) -> None:
    """Wait before asking again; the one place the SDK sleeps between attempts.

    Parameters
    ----------
    seconds
        How long.
    """
    time.sleep(seconds)


def retry_wait(exc: LakanaError) -> float:
    """Say how long to wait before sending a request again after a 503.

    Parameters
    ----------
    exc
        The 503 the server (or a proxy) answered.

    Returns
    -------
    float
        The answer's `Retry-After` when it is whole seconds (at most `RETRY_AFTER_CAP_SECONDS`),
        else `BUSY_BACKOFF_SECONDS`: the header is absent, not a number, negative, fractional or
        an HTTP date.
    """
    text = (exc.retry_after or "").strip()
    if not re.fullmatch(r"[0-9]+", text):
        return BUSY_BACKOFF_SECONDS
    seconds = float(int(text)) if len(text) <= 9 else RETRY_AFTER_CAP_SECONDS
    return min(seconds, RETRY_AFTER_CAP_SECONDS)


def _undecodable(url: str, exc: httpx.DecodingError) -> InvalidResponse:
    """Build the error for a body whose Content-Encoding does not match its bytes.

    Parameters
    ----------
    url
        The server's URL.
    exc
        What httpx raised while decoding.

    Returns
    -------
    InvalidResponse
        A message that names the server and the next step; it never carries the body.
    """
    return InvalidResponse(
        f"The answer from {url} could not be decoded ({type(exc).__name__}): its "
        "Content-Encoding does not match its body. Check the URL and any proxy between you "
        "and the server."
    )


def _format_path(template: str, path_params: Mapping[str, str] | None) -> str:
    """Fill a route's path template with URL-quoted path parameters.

    Parameters
    ----------
    template
        The route's path template, e.g. `"/api/v1/environments/{name}"`.
    path_params
        Values to substitute into the template's `{placeholders}`.

    Returns
    -------
    str
        The filled, quoted path.

    Raises
    ------
    ValidationError
        If a value is empty, `"."`, `".."`, `"/"`, or otherwise contains a
        `/` — none of which a real resource name ever contains, and any of
        which could otherwise turn one path segment into a dot-segment or
        an extra segment that `httpx`/the server would normalise or route
        differently than intended.
    """
    if not path_params:
        return template
    quoted = {}
    for key, value in path_params.items():
        if value in _FORBIDDEN_PATH_PARAM_VALUES or "/" in value:
            raise ValidationError(f"Invalid path parameter {key!r}: {value!r}")
        quoted[key] = quote(value, safe="")
    return template.format(**quoted)


def query_params(**params: object) -> dict[str, object]:
    """Build query parameters, omitting any left unset.

    Parameters
    ----------
    **params
        Candidate query parameter values.

    Returns
    -------
    dict of str to object
        Only the entries whose value is not `None` — `httpx` would
        otherwise send a `None` value as the literal empty string rather
        than omitting the parameter.
    """
    return {key: value for key, value in params.items() if value is not None}


NEXT_CURSOR_HEADER = "X-Next-Cursor"
"""The response header of a paged list that carries the cursor of the next page."""


_T_co = TypeVar("_T_co", covariant=True)


class _Page(Protocol[_T_co]):
    """What a page of a paged list has: its rows and the cursor of the next page."""

    @property
    def items(self) -> Sequence[_T_co]:
        """The rows of the page."""

    @property
    def next_cursor(self) -> str | None:
        """The cursor of the next page, or `None` on the last."""


def _next_cursor(cursor: str, seen: set[str]) -> str:
    """Record a page's cursor, refusing one a server has sent before.

    Parameters
    ----------
    cursor
        The `next_cursor` of the page just read.
    seen
        The cursors of the earlier pages; receives `cursor`.

    Returns
    -------
    str
        `cursor`.

    Raises
    ------
    InvalidResponse
        If the server sent this cursor before, so that paging would never end.
    """
    if cursor in seen:
        raise InvalidResponse(
            "The server sent the same page cursor twice, so the list would never end. "
            "Check the URL, or upgrade the server and the SDK to matching releases."
        )
    seen.add(cursor)
    return cursor


def every_page(fetch: Callable[[str | None], _Page[_T_co]]) -> Iterator[_T_co]:
    """Yield every row of a paged list, fetching one page after the other.

    Parameters
    ----------
    fetch
        Reads the page that starts at a cursor (`None` for the first).

    Yields
    ------
    object
        Each row, once, in the list's order.
    """
    cursor: str | None = None
    seen: set[str] = set()
    while True:
        page = fetch(cursor)
        yield from page.items
        if page.next_cursor is None:
            return
        cursor = _next_cursor(page.next_cursor, seen)


def csv_param(values: str | Sequence[str] | None) -> str | None:
    """Join the values of a filter that takes several into the comma-separated form.

    Parameters
    ----------
    values
        One value, several, or `None` for no filter.

    Returns
    -------
    str or None
        ``"failed,cancelled"``; `None` when no value is given, so the filter is left out.
    """
    if values is None:
        return None
    joined = values if isinstance(values, str) else ",".join(values)
    return joined or None


class HttpTransport:
    """Sends authenticated HTTP requests and maps errors to SDK exceptions.

    Parameters
    ----------
    config
        The resolved URL and token.
    timeout
        Request timeout in seconds, used only when this transport builds
        its own `httpx.Client`.
    verify
        TLS verification setting, used only when this transport builds its
        own `httpx.Client`.
    http_client
        An existing `httpx.Client` to use instead of building one — for
        example Starlette's `TestClient`. Used exactly as given; requests
        are sent with paths relative to its own `base_url`. Ownership stays
        with the caller: `close()` never closes a client supplied this way.
    """

    def __init__(
        self,
        config: ResolvedConfig,
        *,
        timeout: float = 30.0,
        verify: bool | str = True,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._config = config
        self._owns_client = http_client is None
        if http_client is not None:
            self._client = http_client
        else:
            self._client = httpx.Client(base_url=config.url, timeout=timeout, verify=verify)

    def request(
        self,
        route: Route,
        *,
        path_params: Mapping[str, str] | None = None,
        params: Mapping[str, object] | None = None,
        json: object | None = None,
        content: bytes | Iterator[bytes] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
        retry: bool = True,
        authenticated: bool = True,
    ) -> httpx.Response:
        """Send one request for a route and raise on a non-2xx response.

        Parameters
        ----------
        route
            The method and path template to send.
        path_params
            Values to substitute into the route's path template.
        params
            Query parameters.
        json
            A JSON request body.
        content
            A raw request body, e.g. an Arrow IPC stream.
        headers
            Additional headers, merged over the default `Authorization`
            header (and taking precedence over it, for the same header
            name).
        timeout
            A per-request timeout override, in seconds. `None` (the
            default) uses this transport's own configured timeout
            (`Client(timeout=...)`), the same as omitting the argument to
            `httpx.Client.request` entirely — passed on only when given, so
            a caller that wants the transport's default never has to know
            what it is.
        retry
            Whether a `GET` is retried on a transport error (the default). `False`
            sends it exactly once.
        authenticated
            Whether to send the token (the default). `False` sends no `Authorization`
            header, for the one route that takes no credential (the CI token exchange).

        Returns
        -------
        httpx.Response
            The successful response.

        Raises
        ------
        ValidationError
            If a path parameter's value is unsafe to place in a URL path
            (see `_format_path`).
        LakanaError
            Mapped from a non-2xx response by `raise_for_response`.
        LakanaConnectionError
            If the server could not be reached.
        InvalidResponse
            If the body cannot be decoded (its Content-Encoding does not match its bytes).
        """
        path = _format_path(route.path, path_params)
        request_headers = {"Authorization": f"Bearer {self._config.token}"} if authenticated else {}
        if headers:
            request_headers.update(headers)
        if route.method == "GET" and retry:
            response = self._send_with_retry(
                route.method,
                path,
                params=params,
                json=json,
                content=content,
                headers=request_headers,
                timeout=timeout,
            )
        else:
            response = self._send_once(
                route.method,
                path,
                params=params,
                json=json,
                content=content,
                headers=request_headers,
                timeout=timeout,
            )
        raise_for_response(response)
        return response

    @contextlib.contextmanager
    def stream(
        self,
        route: Route,
        *,
        path_params: Mapping[str, str] | None = None,
        params: Mapping[str, object] | None = None,
        timeout: float | None = None,
    ) -> Generator[Iterator[bytes]]:
        """Send one request and offer its body in pieces, never as a whole.

        A `GET` is sent once, never retried: a body that broke half way is the caller's to
        start again, from the beginning.

        Parameters
        ----------
        route
            The method and path template.
        path_params
            Values for the path template.
        params
            Query parameters.
        timeout
            A per-request timeout override, in seconds (httpx applies it to each connect and
            read, not to the whole body).

        Yields
        ------
        Iterator of bytes
            The body's pieces. Read them inside the `with` block.

        Raises
        ------
        ValidationError
            If a path parameter's value is unsafe to place in a URL path.
        LakanaError
            Mapped from a non-2xx response, whose body is read first.
        LakanaConnectionError
            If the server could not be reached, or the connection broke before the body ended
            (httpx then says the body was incomplete): a body that was cut off is never taken
            for a short one that finished.
        InvalidResponse
            If the body cannot be decoded (its Content-Encoding does not match its bytes).
        """
        path = _format_path(route.path, path_params)
        headers = {"Authorization": f"Bearer {self._config.token}"}
        extra: dict[str, Any] = {} if timeout is None else {"timeout": timeout}
        try:
            with self._client.stream(
                route.method, path, params=cast(Any, params), headers=headers, **extra
            ) as response:
                if not response.is_success:
                    response.read()
                    raise_for_response(response)
                yield response.iter_bytes()
        except httpx.TransportError as exc:
            raise LakanaConnectionError(
                f"Could not reach {self._config.url}: {type(exc).__name__}"
            ) from exc
        except httpx.DecodingError as exc:
            problem = _undecodable(self._config.url, exc)
            raise problem from exc

    def _send_once(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, object] | None,
        json: object | None,
        content: bytes | Iterator[bytes] | None,
        headers: Mapping[str, str],
        timeout: float | None,
    ) -> httpx.Response:
        """Send a single request, wrapping a transport failure.

        Parameters
        ----------
        method
            The HTTP method.
        path
            The path, relative to the client's `base_url`.
        params
            Query parameters.
        json
            A JSON request body.
        content
            A raw request body.
        headers
            Request headers.
        timeout
            A per-request timeout override in seconds, or `None` to use
            this transport's own configured timeout.

        Returns
        -------
        httpx.Response
            The response.

        Raises
        ------
        LakanaConnectionError
            If the request could not be sent at all.
        InvalidResponse
            If the body cannot be decoded (its Content-Encoding does not match its bytes).
        """
        extra: dict[str, Any] = {} if timeout is None else {"timeout": timeout}
        try:
            return self._client.request(
                method,
                path,
                params=cast(Any, params),
                json=json,
                content=content,
                headers=headers,
                **extra,
            )
        except httpx.TransportError as exc:
            raise LakanaConnectionError(
                f"Could not reach {self._config.url}: {type(exc).__name__}"
            ) from exc
        except httpx.DecodingError as exc:
            problem = _undecodable(self._config.url, exc)
            raise problem from exc

    def _send_with_retry(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, object] | None,
        json: object | None,
        content: bytes | Iterator[bytes] | None,
        headers: Mapping[str, str],
        timeout: float | None,
    ) -> httpx.Response:
        """Send a request, retrying on transport errors with backoff.

        Parameters
        ----------
        method
            The HTTP method.
        path
            The path, relative to the client's `base_url`.
        params
            Query parameters.
        json
            A JSON request body.
        content
            A raw request body.
        headers
            Request headers.
        timeout
            A per-request timeout override in seconds, or `None` to use
            this transport's own configured timeout.

        Returns
        -------
        httpx.Response
            The response from the first attempt that succeeds.

        Raises
        ------
        LakanaConnectionError
            If every attempt fails.
        InvalidResponse
            If the body cannot be decoded (its Content-Encoding does not match its bytes).
        """
        extra: dict[str, Any] = {} if timeout is None else {"timeout": timeout}
        last_exc: httpx.TransportError | None = None
        for backoff in _RETRY_BACKOFFS_SECONDS:
            if backoff:
                time.sleep(backoff)
            try:
                return self._client.request(
                    method,
                    path,
                    params=cast(Any, params),
                    json=json,
                    content=content,
                    headers=headers,
                    **extra,
                )
            except httpx.TransportError as exc:
                last_exc = exc
            except httpx.DecodingError as exc:
                problem = _undecodable(self._config.url, exc)
                raise problem from exc
        raise LakanaConnectionError(
            f"Could not reach {self._config.url}: {type(last_exc).__name__}"
        ) from last_exc

    def close(self) -> None:
        """Close the underlying `httpx.Client`, if this transport built it.

        A caller-supplied `http_client` is never closed here — the caller
        that constructed it owns its lifecycle.
        """
        if self._owns_client:
            self._client.close()
