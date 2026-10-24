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


@pytest.mark.slow
def test_every_server_route_has_an_sdk_method(settings: Settings) -> None:
    """Every platform route is reachable through the SDK."""
    sdk = {(r.method, r.path) for r in ROUTES.values()}
    uncovered = _server_routes(settings) - sdk
    assert not uncovered, f"Platform routes without an SDK method: {sorted(uncovered)}"


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
