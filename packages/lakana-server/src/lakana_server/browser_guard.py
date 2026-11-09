"""The web interface's Host and Origin guard (`LAKANA_UI__ENABLED`).

`BrowserGuard` is a pure ASGI middleware in front of every route while the interface is on. It
defends browser traffic against DNS rebinding (a page whose name an attacker points at the
server) and cross-site requests, before any route, credential or body is looked at:

1. a Bearer `Authorization` header (first header, `Bearer <credential>`) skips only the
   session-cookie test of rule 2 and the Origin rule 5: the Bearer header wins over the cookie,
   and a cross-site page cannot send one without a CORS preflight, which no route answers. It
   does not skip the Host check of pages and sign-in routes, because a DNS-rebinding page is
   same-origin with its own name and can add a fake Bearer header freely;
2. a request is browser traffic when its path is a page or file of the interface (not below
   `/api/` or `/v1/`, not `/healthz` or `/readyz`), when it carries a session cookie
   (`lakana_session*`, `__Host-lakana_session`; not with a Bearer), or when it is a sign-in route
   (`/api/v1/sign-in/...`); everything else passes untouched, so engines, CI and the worker
   keep reaching open routes such as `/v1/config` by internal names;
3. browser traffic must name an allowed host in `Host` (`Settings.allowed_host_headers`), or it
   is refused with 421;
4. a GET or HEAD of a page at `127.0.0.1` or `[::1]`, while the public address is `localhost`,
   is sent on (308) to the same path at the public address, where passkeys work;
5. every other method of browser traffic without a Bearer needs `Origin` equal to the public
   origin, or no `Origin` and `Sec-Fetch-Site: same-origin`, or it is refused with 403.

Every address the guard sends a browser to is built from `Settings.public_origin`, never from
the request's `Host`. The security notes are in `docs/agent/security.md`, "The web interface's
files", "Host and Origin".
"""

from __future__ import annotations

import logging
from urllib.parse import quote, quote_from_bytes

from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from lakana_server.settings import Settings

LOOPBACK_NAMES = frozenset({"localhost", "127.0.0.1", "::1"})
"""The names a local server is reached at: `localhost` and the two loopback addresses."""

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_API_PREFIXES = ("/api/", "/v1/")
_API_EXACT = frozenset({"/api", "/v1", "/healthz", "/readyz"})
_SIGN_IN = "/api/v1/sign-in"
_SESSION_COOKIES = ("lakana_session", "__host-lakana_session")
_PATH_SAFE = "/!$&'()*+,;=:@-._~"
_QUERY_SAFE = "/?!$&'()*+,;=:@-._~%"

HOST_NOT_ALLOWED = (
    "This server answers browsers only at its own addresses, and this request named another "
    "in its Host header. Open the interface at the address in LAKANA_UI__PUBLIC_URL, or add "
    "this name to LAKANA_UI__ALLOWED_HOSTS. Behind a reverse proxy, pass the browser's Host "
    "header through (nginx: proxy_set_header Host $http_host;)."
)
"""The 421 detail; it never repeats the request's `Host`."""

ORIGIN_NOT_ALLOWED = (
    "This change did not come from the interface: its Origin header is missing or is not the "
    "server's public address (LAKANA_UI__PUBLIC_URL). Make changes in the interface opened at "
    "that address; a script sends a token instead (Authorization: Bearer)."
)
"""The 403 detail."""

logger = logging.getLogger(__name__)


def _values(scope: Scope, name: bytes) -> list[str]:
    """Collect every value of one request header.

    Parameters
    ----------
    scope
        The ASGI scope.
    name
        The header name, lower case.

    Returns
    -------
    list of str
        The values in order, decoded as Latin-1.
    """
    return [value.decode("latin-1") for key, value in scope.get("headers", ()) if key == name]


def _single(scope: Scope, name: bytes) -> str | None:
    """Read a header that must appear at most once.

    Parameters
    ----------
    scope
        The ASGI scope.
    name
        The header name, lower case.

    Returns
    -------
    str or None
        Its value when it appears exactly once; None when it is missing or repeated.
    """
    values = _values(scope, name)
    return values[0] if len(values) == 1 else None


def _has_bearer(scope: Scope) -> bool:
    """Tell whether the request carries a Bearer credential, which wins over the cookie.

    Parameters
    ----------
    scope
        The ASGI scope.

    Returns
    -------
    bool
        True when the first `Authorization` header is `Bearer <something>`, as
        `get_principal` reads it.
    """
    values = _values(scope, b"authorization")
    if not values:
        return False
    scheme, _, credential = values[0].strip().partition(" ")
    return scheme.lower() == "bearer" and bool(credential.strip())


def _has_session_cookie(scope: Scope) -> bool:
    """Tell whether the request carries a cookie named like the interface's session.

    Parameters
    ----------
    scope
        The ASGI scope.

    Returns
    -------
    bool
        True for any cookie whose name, in any letter case, starts with `lakana_session` or
        `__Host-lakana_session`.
    """
    for header in _values(scope, b"cookie"):
        for pair in header.split(";"):
            name = pair.split("=", 1)[0].strip().lower()
            if name.startswith(_SESSION_COOKIES):
                return True
    return False


def _app_path(scope: Scope) -> str:
    """Read the request path below the server's path prefix.

    Parameters
    ----------
    scope
        The ASGI scope.

    Returns
    -------
    str
        `path` without a leading `root_path`.
    """
    path: str = scope.get("path", "")
    root: str = scope.get("root_path", "")
    if root and path.startswith(root):
        return path[len(root) :]
    return path


def _is_page(path: str) -> bool:
    """Tell whether a path is the interface's (a page or file), not the API's.

    Parameters
    ----------
    path
        The path below the server's prefix.

    Returns
    -------
    bool
        False only for `/api`, `/v1` and what lies below them, and for `/healthz` and
        `/readyz`; any other spelling counts as the interface's.
    """
    return not (path in _API_EXACT or path.startswith(_API_PREFIXES))


class BrowserGuard:
    """Check `Host` and `Origin` of browser traffic before any route sees it.

    Parameters
    ----------
    app
        The application to wrap.
    settings
        The server settings: the public origin and the allowed `Host` headers are read once.
    """

    def __init__(self, app: ASGIApp, *, settings: Settings) -> None:
        self.app = app
        self.public = settings.public_origin()
        self.allowed_hosts = settings.allowed_host_headers()
        port = self.public.host_header.removeprefix(self.public.host)
        self.aliases = (
            frozenset({f"127.0.0.1{port}", f"[::1]{port}"}) if self.public.loopback else frozenset()
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Pass the request on, or answer it with a refusal or a redirect.

        Parameters
        ----------
        scope
            The ASGI scope.
        receive
            The request's receive channel.
        send
            The response's send channel.
        """
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        bearer = _has_bearer(scope)
        path = _app_path(scope)
        page = _is_page(path)
        cookie = not bearer and _has_session_cookie(scope)
        if not (page or cookie or self._is_sign_in(path)):
            await self.app(scope, receive, send)
            return
        host = (_single(scope, b"host") or "").lower()
        if host not in self.allowed_hosts:
            logger.debug("Refused browser traffic at an unknown host: %.100r", host)
            await self._refuse(scope, receive, send, 421, HOST_NOT_ALLOWED, "HostNotAllowed")
            return
        websocket = scope["type"] == "websocket"
        method = "GET" if websocket else scope["method"]
        if page and not websocket and method in {"GET", "HEAD"} and host in self.aliases:
            await self._redirect(scope, receive, send)
            return
        unsafe = websocket or method not in _SAFE_METHODS
        if unsafe and not bearer and not self._same_origin(scope):
            await self._refuse(scope, receive, send, 403, ORIGIN_NOT_ALLOWED, "OriginNotAllowed")
            return
        await self.app(scope, receive, send)

    @staticmethod
    def _is_sign_in(path: str) -> bool:
        """Tell whether a path is a sign-in route.

        Parameters
        ----------
        path
            The path below the server's prefix.

        Returns
        -------
        bool
            True for `/api/v1/sign-in` and everything below it.
        """
        return path == _SIGN_IN or path.startswith(_SIGN_IN + "/")

    def _same_origin(self, scope: Scope) -> bool:
        """Tell whether a state-changing request comes from the interface itself.

        Parameters
        ----------
        scope
            The ASGI scope.

        Returns
        -------
        bool
            True when the one `Origin` is the public origin exactly, or when there is no
            `Origin` and the one `Sec-Fetch-Site` is `same-origin`.
        """
        origins = _values(scope, b"origin")
        if origins:
            return len(origins) == 1 and origins[0] == self.public.origin
        return _single(scope, b"sec-fetch-site") == "same-origin"

    async def _redirect(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Send a page opened at a loopback address on to the public address.

        Parameters
        ----------
        scope
            The ASGI scope; only its decoded path and query are used.
        receive
            The request's receive channel.
        send
            The response's send channel.
        """
        path: str = scope.get("path", "")
        if not path.startswith("/"):
            path = "/" + path
        location = self.public.origin + quote(path, safe=_PATH_SAFE)
        query: bytes = scope.get("query_string", b"")
        if query:
            location += "?" + quote_from_bytes(query, safe=_QUERY_SAFE)
        response = Response(
            status_code=308, headers={"Location": location, "Cache-Control": "no-store"}
        )
        await response(scope, receive, send)

    @staticmethod
    async def _refuse(
        scope: Scope, receive: Receive, send: Send, status: int, detail: str, kind: str
    ) -> None:
        """Answer with a refusal in the API's error shape, or close a WebSocket.

        Parameters
        ----------
        scope
            The ASGI scope.
        receive
            The request's receive channel.
        send
            The response's send channel.
        status
            421 or 403.
        detail
            The cause and the next step.
        kind
            `HostNotAllowed` or `OriginNotAllowed`.
        """
        if scope["type"] == "websocket":
            await receive()
            await send({"type": "websocket.close", "code": 1008})
            return
        response = JSONResponse(
            {"detail": detail, "type": kind},
            status_code=status,
            headers={"Cache-Control": "no-store"},
        )
        await response(scope, receive, send)
