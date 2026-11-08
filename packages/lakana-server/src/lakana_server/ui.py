"""Serving the web interface's built files at `/` (`LAKANA_UI__ENABLED`).

The interface is a single-page app built by `npm --prefix ui run build` into `ui_dist/`, which
the release puts into the lakana-server wheel. `mount_ui` serves it on the same origin as the
API and never changes an API answer: its fallback route, which answers every other GET with
`index.html` so that a deep link such as `/catalog/sales.orders` loads the app, never matches a
path whose first segment belongs to the API. No file is ever found by joining a request path
onto a directory: hashed assets go through Starlette's `StaticFiles`, which refuses a path that
resolves outside its directory, and every other file is looked up by name in a dictionary
built when the app starts. The security notes are in `docs/agent/security.md`, "The web
interface's files".
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Awaitable, Callable
from http import HTTPStatus
from pathlib import Path

from fastapi import FastAPI
from fastapi.routing import iter_route_contexts
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import FileResponse, PlainTextResponse, Response
from starlette.routing import Match, Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

logger = logging.getLogger(__name__)

UI_DIST: Path = Path(__file__).parent / "ui_dist"
"""Where the built interface lives inside the installed package (git-ignored in a checkout)."""

CSP_FILE = "csp.txt"
"""The file of the build holding its `Content-Security-Policy`, the one source of the policy.

`ui/csp.ts` defines it; the build writes it here (`ui/build/csp-file.ts`) and `vite preview`
sends the same constant, so the header always matches the interface actually served.
"""

_IMMUTABLE = "public, max-age=31536000, immutable"
_REVALIDATE = "no-cache"
_INDEX = "index.html"

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".json": "application/json",
    ".map": "application/json",
    ".webmanifest": "application/manifest+json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".webp": "image/webp",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
}
"""Content types by suffix, fixed here: `nosniff` makes a browser trust them as sent, and the
system's MIME table differs between machines (a slim container image has none)."""

_ROOT_FILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
"""A file directly in the build that gets a route of its own; nothing else does (and never one
named like a first segment of the API)."""


class UiNotBuiltError(RuntimeError):
    """`LAKANA_UI__ENABLED` is true, but the installation has no built interface."""


def content_type(path: Path) -> str:
    """Name the content type a file of the interface is sent with.

    Parameters
    ----------
    path
        The file.

    Returns
    -------
    str
        The type for its suffix, or `application/octet-stream` for one the interface does
        not use (a browser then runs or renders nothing of it).
    """
    return _CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")


def read_policy(dist: Path) -> str:
    """Read the content security policy the build wrote, refusing one it cannot send.

    Parameters
    ----------
    dist
        The build directory.

    Returns
    -------
    str
        The policy, surrounding spaces and newlines removed: one header value.

    Raises
    ------
    UiNotBuiltError
        When `csp.txt` is missing or unreadable, empty, not plain ASCII, has a control
        character (a line break would end the header), or has no `default-src`.
    """
    path = dist / CSP_FILE
    policy = ""
    try:
        policy = path.read_bytes().decode("ascii").strip(" \n")
    except FileNotFoundError:
        cause = "is missing"
    except OSError as exc:
        cause = f"cannot be read ({exc.strerror})"
    except UnicodeDecodeError:
        cause = "is not plain ASCII"
    else:
        directives = {part.strip().split(" ", 1)[0] for part in policy.split(";")}
        if not policy:
            cause = "is empty"
        elif any(ord(char) < 32 or ord(char) == 127 for char in policy):
            cause = "has a control character"
        elif "default-src" not in directives:
            cause = "has no default-src"
        else:
            return policy
    raise UiNotBuiltError(
        f"LAKANA_UI__ENABLED is true, but the built interface's content security policy "
        f"({path}) {cause}. Rebuild the interface from a source checkout with "
        "`npm --prefix ui ci && npm --prefix ui run build`, or install a released "
        "lakana-server; or set LAKANA_UI__ENABLED=false."
    )


def _headers(policy: str, cache_control: str) -> dict[str, str]:
    """Build the headers of every response of the interface.

    Parameters
    ----------
    policy
        The build's content security policy (`read_policy`).
    cache_control
        The `Cache-Control` value.

    Returns
    -------
    dict of str to str
        The policy, the security headers and the cache rule.
    """
    return {
        "Content-Security-Policy": policy,
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "X-Frame-Options": "DENY",
        "Cache-Control": cache_control,
    }


def _error(policy: str, status_code: int = 404) -> Response:
    """Answer a request for a file of the interface that cannot be served.

    Parameters
    ----------
    policy
        The build's content security policy.
    status_code
        The status: 404, or what `StaticFiles` refused with (405 for a wrong method).

    Returns
    -------
    Response
        The status's own phrase as plain text with the interface's headers, never cached; a
        405 also names the methods that work (`Allow`).
    """
    headers = _headers(policy, _REVALIDATE)
    if status_code == 405:
        headers["Allow"] = "GET, HEAD"
    return PlainTextResponse(
        HTTPStatus(status_code).phrase, status_code=status_code, headers=headers
    )


class _Assets(StaticFiles):
    """The hashed files under `assets/`: cached for good, typed by `content_type`.

    `StaticFiles` normalises the path, refuses an absolute one, a NUL byte and one whose real
    path (symlinks resolved) is outside the directory.
    """

    def __init__(self, directory: Path, policy: str) -> None:
        """Serve `directory` with `policy` on every answer.

        Parameters
        ----------
        directory
            The build's `assets/`.
        policy
            The build's content security policy.
        """
        super().__init__(directory=directory)
        self.policy = policy

    async def get_response(self, path: str, scope: Scope) -> Response:
        """Serve one asset, or a plain 404 with the interface's headers.

        Parameters
        ----------
        path
            The path below `assets/`, as `StaticFiles` normalised it.
        scope
            The ASGI scope of the request.

        Returns
        -------
        Response
            The file with a cache rule for good, or the not-found answer.
        """
        try:
            response = await super().get_response(path, scope)
        except HTTPException as exc:
            return _error(self.policy, exc.status_code)
        if response.status_code in {200, 206}:
            response.headers["Content-Type"] = content_type(Path(path))
        cached = response.status_code in {200, 206, 304}
        response.headers.update(_headers(self.policy, _IMMUTABLE if cached else _REVALIDATE))
        return response


def _files(directory: Path, dist: Path, *, recursive: bool) -> dict[str, Path]:
    """List the regular files of a directory of the build, by their path below it.

    Parameters
    ----------
    directory
        The directory to list.
    dist
        The build's root: a file whose real path (symlinks resolved) is outside it is left out.
    recursive
        Whether to list the subdirectories' files too (`os.walk` enters no symlinked
        directory).

    Returns
    -------
    dict of str to Path
        The `/`-separated path below `directory` of each file, to its real path.
    """
    if not directory.is_dir():
        return {}
    root = dist.resolve()
    found: dict[str, Path] = {}
    paths = (
        (Path(parent) / name for parent, _, names in os.walk(directory) for name in names)
        if recursive
        else directory.iterdir()
    )
    for path in paths:
        real = path.resolve()
        if real.is_file() and real.is_relative_to(root):
            found[path.relative_to(directory).as_posix()] = real
    return found


def _file_endpoint(
    path: Path, cache_control: str, policy: str
) -> Callable[[Request], Awaitable[Response]]:
    """Make the endpoint that sends one fixed file of the build.

    Parameters
    ----------
    path
        The file; it never comes from the request.
    cache_control
        Its `Cache-Control` value.
    policy
        The build's content security policy.

    Returns
    -------
    callable
        The endpoint.
    """

    async def endpoint(request: Request) -> Response:  # ruff: ignore[unused-async]
        """Send the file.

        Parameters
        ----------
        request
            The request (unused: the file is fixed).

        Returns
        -------
        Response
            The file, typed by `content_type`, with the interface's headers.
        """
        return FileResponse(
            path, media_type=content_type(path), headers=_headers(policy, cache_control)
        )

    return endpoint


def _licence_endpoint(
    licences: dict[str, Path], policy: str
) -> Callable[[Request], Awaitable[Response]]:
    """Make the endpoint for `/licenses/...`, which looks a name up and never joins it.

    Parameters
    ----------
    licences
        The licence files by their path below `licenses/`.
    policy
        The build's content security policy.

    Returns
    -------
    callable
        The endpoint.
    """

    async def endpoint(request: Request) -> Response:  # ruff: ignore[unused-async]
        """Send the named licence, or 404.

        Parameters
        ----------
        request
            The request; its `name` path parameter is a dictionary key, nothing else.

        Returns
        -------
        Response
            The licence file, or the not-found answer.
        """
        path = licences.get(request.path_params["name"])
        if path is None:
            return _error(policy)
        return FileResponse(
            path, media_type=content_type(path), headers=_headers(policy, _REVALIDATE)
        )

    return endpoint


class _AppPages(Route):
    """Every other GET or HEAD path: the app's page, so a deep link loads the app.

    It never matches a path whose first segment is the API's, in any letter case, so an
    unknown API path or a wrong method there answers exactly as it does without the
    interface. Nor a path with a `.` or `..` segment or a backslash, which no browser sends
    and which could otherwise dress an API path up as a page (`/./api/...`).
    """

    def __init__(self, index: Path, reserved: frozenset[str], policy: str) -> None:
        """Answer with `index` everywhere outside `reserved`.

        Parameters
        ----------
        index
            The build's `index.html`.
        reserved
            The first path segments that stay the API's (`reserved_segments`).
        policy
            The build's content security policy.
        """
        super().__init__(
            "/{path:path}",
            _file_endpoint(index, _REVALIDATE, policy),
            methods=["GET", "HEAD"],
            include_in_schema=False,
        )
        self.reserved = reserved
        self._folded = frozenset(segment.casefold() for segment in reserved)

    def matches(self, scope: Scope) -> tuple[Match, Scope]:
        """Match like `Route`, except never in a reserved first segment or an odd path.

        Parameters
        ----------
        scope
            The ASGI scope of the request.

        Returns
        -------
        tuple of Match and dict
            `Match.NONE` for a reserved first segment, a dot segment or a backslash,
            otherwise what `Route` says.
        """
        match, child = super().matches(scope)
        # `Route` has already removed the `root_path` (at a segment boundary); no match, no path.
        path = child.get("path_params", {}).get("path", "")
        segments = path.split("/")
        if (
            segments[0].casefold() in self._folded
            or "\\" in path
            or any(segment in {".", ".."} for segment in segments)
        ):
            return Match.NONE, {}
        return match, child


def reserved_segments(app: FastAPI) -> frozenset[str]:
    """Name the first path segments the API uses, which the app's pages never answer.

    Parameters
    ----------
    app
        The application, with every API route already registered.

    Returns
    -------
    frozenset of str
        The first segment of every route registered so far (`api`, `v1`, `healthz`, `docs`,
        `openapi.json`, ...), plus `api` itself.

    Raises
    ------
    RuntimeError
        When a route's first segment is a path parameter (`/{tenant}/...`): no segment could
        keep the pages off it.
    """
    segments = {"api"}
    # Through FastAPI's iterator: an included router is one entry of `app.router.routes`
    # (`_IncludedRouter`, no `path`) whose routes only the iterator reaches.
    for route in iter_route_contexts(app.router.routes):
        path = route.path or ""
        first = path.lstrip("/").split("/", 1)[0]
        if first.startswith("{"):
            raise RuntimeError(
                f"The route {path} starts with a path parameter, so the web interface's pages "
                "cannot be kept off it; give it a fixed first segment."
            )
        segments.add(first)
    segments.discard("")
    return frozenset(segments)


def mount_ui(app: FastAPI, dist: Path) -> None:
    """Serve the built interface in `dist` at `/`, after every API route.

    Parameters
    ----------
    app
        The application, with every API route already registered.
    dist
        The build directory (`UI_DIST` in an installation).

    Raises
    ------
    UiNotBuiltError
        When `dist` has no `index.html` or no `assets/`, or its policy is missing or
        malformed (`read_policy`).

    Notes
    -----
    FastAPI 0.141's `app.frontend()` registers low-priority routes tried after every other
    one, which would remove the "mount last" rule, but it is no drop-in: its fallback depends
    on `Accept: text/html` and it has no reserved segments (a browser opening `/api/v1/nope`
    would get the page). Revisit at the next FastAPI-related change.
    """
    index = dist / _INDEX
    if not index.is_file():
        raise UiNotBuiltError(
            f"LAKANA_UI__ENABLED is true, but this installation has no built interface ({dist}). "
            "Install a released lakana-server, or build it from a source checkout with "
            "`npm --prefix ui ci && npm --prefix ui run build`; or set LAKANA_UI__ENABLED=false."
        )
    if not (dist / "assets").is_dir():
        raise UiNotBuiltError(
            f"LAKANA_UI__ENABLED is true, but the built interface ({dist}) has no assets/ "
            "directory: the build is incomplete. Rebuild it from a source checkout with "
            "`npm --prefix ui ci && npm --prefix ui run build`, or install a released "
            "lakana-server; or set LAKANA_UI__ENABLED=false."
        )
    policy = read_policy(dist)
    reserved = reserved_segments(app)
    routes = app.router.routes
    routes.append(Mount("/assets", app=_Assets(dist / "assets", policy)))
    routes.append(
        Route(
            "/licenses/{name:path}",
            _licence_endpoint(_files(dist / "licenses", dist, recursive=True), policy),
            methods=["GET", "HEAD"],
            include_in_schema=False,
        )
    )
    for name, path in sorted(_files(dist, dist, recursive=False).items()):
        if name not in {_INDEX, CSP_FILE, *reserved} and _ROOT_FILE_NAME.fullmatch(name):
            routes.append(
                Route(
                    f"/{name}",
                    _file_endpoint(path, _REVALIDATE, policy),
                    methods=["GET", "HEAD"],
                    include_in_schema=False,
                )
            )
    routes.append(_AppPages(index, reserved, policy))
    logger.info("The web interface is served at /.")
