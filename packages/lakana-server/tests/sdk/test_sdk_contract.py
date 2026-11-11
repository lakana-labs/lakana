"""OpenAPI contract test: the SDK's route table against the running server.

Compares `lakana._routes.ROUTES` against `lakana_server.app.create_app`'s own
OpenAPI schema in both directions — every SDK route must exist on the
server, and every platform route (outside Iceberg REST, which the SDK does
not cover) must be reachable through some SDK method. A third test checks
for dead `ROUTES` entries: every key must be referenced either by some
resource module or by `Client` itself — `whoami` is a client-level
concern with no dedicated resource, called straight from `lakana.client`.
"""

import pathlib

import pytest

import lakana.client
import lakana.resources as resources_pkg
from lakana._routes import ROUTES
from lakana_server.app import create_app
from lakana_server.settings import Settings


def _server_routes(settings: Settings) -> set[tuple[str, str]]:
    """Every (METHOD, path-template) the platform API serves.

    Parameters
    ----------
    settings
        Server settings for building the app.

    Returns
    -------
    set of tuple of (str, str)
        Platform routes under /api/v1 (Iceberg REST excluded).
    """
    spec = create_app(settings).openapi()
    return {
        (method.upper(), path)
        for path, ops in spec["paths"].items()
        if path.startswith("/api/v1/")
        for method in ops
        if method in {"get", "post", "put", "patch", "delete"}
    }


@pytest.mark.slow
def test_every_sdk_route_exists_on_the_server(settings: Settings) -> None:
    """The SDK never calls a method/path the server does not serve."""
    sdk = {(r.method, r.path) for r in ROUTES.values()}
    missing = sdk - _server_routes(settings)
    assert not missing, f"SDK routes not on the server: {sorted(missing)}"


_WEBAUTHN = "a WebAuthn ceremony: only a browser holding the passkey can answer it"
_BROWSER_SESSION = "describes or ends the browser session of the request; a token has none (409)"
_INTERFACE_STATE = "what the web interface remembers for a person: tour, Get started, update note"
_RECENT = "the objects a person opened in the web interface, for Home's Recently opened"

BROWSER_ONLY: dict[tuple[str, str], str] = {
    ("POST", "/api/v1/sign-in/links/describe"): (
        "the sign-in page reads the link's fragment, which only the browser that opened it has"
    ),
    ("POST", "/api/v1/sign-in/links/redeem"): (
        "using a link starts a browser session and sets its cookie; a program keeps its token"
    ),
    ("POST", "/api/v1/sign-in/passkey/options"): _WEBAUTHN,
    ("POST", "/api/v1/sign-in/passkey"): _WEBAUTHN,
    ("POST", "/api/v1/sign-in/step-up/options"): _WEBAUTHN,
    ("POST", "/api/v1/sign-in/step-up"): _WEBAUTHN,
    ("POST", "/api/v1/principals/{name}/passkeys/options"): _WEBAUTHN,
    ("POST", "/api/v1/principals/{name}/passkeys"): _WEBAUTHN,
    ("GET", "/api/v1/sign-in/session"): _BROWSER_SESSION,
    ("DELETE", "/api/v1/sign-in/session"): _BROWSER_SESSION,
    ("GET", "/api/v1/sign-in/notices"): (
        "notices are what the interface shows at sign-in; the same events are in the audit log "
        "(`lakana audit list`)"
    ),
    ("DELETE", "/api/v1/sign-in/notices/{notice_id}"): (
        "dismissing a notice the interface showed; the audit log keeps the event"
    ),
    ("GET", "/api/v1/principals/{name}/interface-state"): _INTERFACE_STATE,
    ("PATCH", "/api/v1/principals/{name}/interface-state"): _INTERFACE_STATE,
    ("GET", "/api/v1/principals/{name}/recent"): _RECENT,
    ("POST", "/api/v1/principals/{name}/recent"): _RECENT,
    ("GET", "/api/v1/home/get-started"): (
        "Home's list of first steps; a program reads the same facts from the routes they come "
        "from (repositories, deploys, queries)"
    ),
    ("GET", "/api/v1/home/attention"): (
        "Home's list of what failed or is late; a program reads the same facts from the routes "
        "they come from (runs, table health, checks, queries, recovery links, workers)"
    ),
    ("GET", "/api/v1/home/work"): (
        "Home's list of one's repositories and jobs; a program reads them from the repository, "
        "deploy and job routes"
    ),
}
"""Platform routes only a browser uses, so the SDK has no method for them, with the reason."""


@pytest.mark.slow
def test_every_server_route_has_an_sdk_method(settings: Settings) -> None:
    """Every platform route is reachable through the SDK, except the browser-only ones.

    No exemption is stale: each is a real route, and none is also in the SDK's table.
    """
    sdk = {(r.method, r.path) for r in ROUTES.values()}
    server = _server_routes(settings)
    uncovered = server - sdk - set(BROWSER_ONLY)
    assert not uncovered, f"Platform routes without an SDK method: {sorted(uncovered)}"
    assert set(BROWSER_ONLY) - server == set()
    assert set(BROWSER_ONLY) & sdk == set()


def test_every_route_is_used_by_a_resource_method() -> None:
    """No dead entries: each ROUTES key is referenced by a resource module or `Client`."""
    resources_dir = pathlib.Path(resources_pkg.__file__).parent
    source = "".join(p.read_text() for p in resources_dir.glob("*.py"))
    source += pathlib.Path(lakana.client.__file__).read_text()
    unused = [key for key in ROUTES if f'"{key}"' not in source]
    assert not unused, f"ROUTES entries never used: {unused}"


def test_the_rollback_phrases_of_the_cli_and_of_core_are_the_same() -> None:
    """The CLI keeps its own copy of the phrases (the SDK package may not import core)."""
    from lakana.cli.runs import _ROLLBACK_PHRASES
    from lakana_core.runs.checks import ROLLBACK_PHRASES

    assert _ROLLBACK_PHRASES == ROLLBACK_PHRASES
