"""The browser session's cookie, CSRF header and the interface switch of the sign-in routes.

A browser session is the fourth credential of `lakana_server.deps.get_principal`, after a
personal access token, a task token and a CI token. The browser holds it as an HttpOnly cookie
whose value is ``<key_id>_<secret>`` (`lakana_core.identity.sessions`). This module names the
cookie, sets and clears it with the attributes the spec fixes, and reads it only where it
counts:

- only on paths below `/api/v1/` (the Iceberg REST routes, `/healthz` and pages never read it);
- never while the interface is off (`LAKANA_UI__ENABLED=false`): the cookie is then ignored;
- `HttpOnly`, `SameSite=Strict`, `Path=/`, no `Domain`; over HTTPS `Secure` and the name
  `__Host-lakana_session`; over plain HTTP (only on `localhost`, `Settings` refuses anything
  else) the name `lakana_session_<port>`, since cookies do not separate ports.

Every request with the cookie, reads too, carries the session's CSRF token in
`X-Lakana-CSRF` (`CSRF_HEADER`), checked by `get_principal`: a random secret only the page
holds, so the cookie alone, which every other `localhost` port receives, signs nobody in. The
interface marks its background polls with `X-Lakana-Background: 1` (`BACKGROUND_HEADER`),
which do not extend the idle timeout.
"""

from __future__ import annotations

from fastapi import Request, Response

from lakana_core.errors import ConflictError
from lakana_server.settings import PublicOrigin

CSRF_HEADER = "X-Lakana-CSRF"
"""The request header that carries the session's CSRF token on every cookie request."""

BACKGROUND_HEADER = "X-Lakana-Background"
"""The request header (value ``1``) that marks a background poll; it never extends a session."""

_API_V1 = "/api/v1/"

INTERFACE_OFF = (
    "The web interface is off (LAKANA_UI__ENABLED=false); turn it on to sign in with a browser."
)
"""The 409 detail of every `/api/v1/sign-in/*` route while the interface is off."""


def cookie_name(origin: PublicOrigin) -> str:
    """Name the session cookie for the interface's public address.

    Parameters
    ----------
    origin
        The public address (`Settings.public_origin`).

    Returns
    -------
    str
        `__Host-lakana_session` over HTTPS; `lakana_session_<port>` over plain HTTP.
    """
    if origin.scheme == "https":
        return "__Host-lakana_session"
    return f"lakana_session_{origin.port}"


def set_session_cookie(response: Response, origin: PublicOrigin, value: str, max_age: int) -> None:
    """Give the browser its session cookie.

    Parameters
    ----------
    response
        The response that starts the session.
    origin
        The public address, which decides the name and `Secure`.
    value
        The cookie's value, ``<key_id>_<secret>``.
    max_age
        Seconds until the browser drops it: the session's absolute lifetime.
    """
    response.set_cookie(
        cookie_name(origin),
        value,
        max_age=max_age,
        path="/",
        secure=origin.scheme == "https",
        httponly=True,
        samesite="strict",
    )


def clear_session_cookie(response: Response, origin: PublicOrigin) -> None:
    """Tell the browser to drop its session cookie, with the attributes it was set with.

    Parameters
    ----------
    response
        The response that ends the session.
    origin
        The public address.
    """
    response.delete_cookie(
        cookie_name(origin),
        path="/",
        secure=origin.scheme == "https",
        httponly=True,
        samesite="strict",
    )


def _app_path(request: Request) -> str:
    """Read the request path below the server's path prefix.

    Parameters
    ----------
    request
        The request.

    Returns
    -------
    str
        The ASGI `path` without a leading `root_path`.
    """
    path: str = request.scope.get("path", "")
    root: str = request.scope.get("root_path", "")
    if root and path.startswith(root):
        return path[len(root) :]
    return path


def read_session_cookie(request: Request) -> str | None:
    """Read the session cookie where it counts as a credential.

    Parameters
    ----------
    request
        The request.

    Returns
    -------
    str or None
        The cookie's value on a path below `/api/v1/` with the interface on; `None` otherwise,
        and when the request carries no such cookie.
    """
    settings = request.app.state.settings
    if not settings.ui.enabled or not _app_path(request).startswith(_API_V1):
        return None
    return request.cookies.get(cookie_name(settings.public_origin())) or None


def require_interface(request: Request) -> None:
    """Refuse a sign-in route while the web interface is off; a router dependency.

    Parameters
    ----------
    request
        The request, used to reach the settings.

    Raises
    ------
    ConflictError
        With `INTERFACE_OFF` (409) when `LAKANA_UI__ENABLED` is false.
    """
    if not request.app.state.settings.ui.enabled:
        raise ConflictError(INTERFACE_OFF)
