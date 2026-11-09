"""Every `/api/v1` route answers a browser session as it answers the person's token.

The session is a credential of `get_principal`, so authorization, break-glass rules, body
limits and audit apply unchanged (spec, "Sessions"). Two tests hold that together:

- a static guard: no module under `lakana_server/api/` reads a principal's credential kind or
  session (`.session`, `.credential`, `.token_id`) except the modules of
  `CREDENTIAL_SPECIFIC_ROUTES`, so no handler can branch on the credential unnoticed;
- a dynamic comparison: every operation of the running app below `/api/v1/` is sent once with a
  session and once with the same person's token, against the same database state, with path
  parameters naming real objects of the person and a minimal body and query built from the
  OpenAPI document, so most cases run their handler. Status, body (after `_normalised`) and
  whether a cookie is set must be equal. The person holds the instance ADMIN role and the session
  a fresh passkey touch, so the comparison covers what an administrator may do; how many cases
  reached their handler is counted (`test_most_cases_reach_their_handler`).
"""

from __future__ import annotations

import ast
import functools
import inspect
import re
import shutil
from collections.abc import Callable, Generator, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.routing import BaseRoute

import lakana_server
import lakana_server.ui
from lakana_core.auth import AllowAllAuthorization, PrincipalRef, SecurableKind
from lakana_core.config import DatabaseConfig, SecretsConfig
from lakana_core.grants.roles import Role
from lakana_core.grants.service import GrantService
from lakana_core.identity.pats import PatService
from lakana_core.identity.sessions import SessionService
from lakana_core.models import Principal, PrincipalType
from lakana_server.app import ROUTERS, create_app
from lakana_server.browser_session import CSRF_HEADER, cookie_name
from lakana_server.deps import TOKEN_ONLY_ROUTES
from lakana_server.settings import Settings, UiConfig
from tests.conftest import _TEST_MASTER_KEY, AUTH_HEADER, PUBLIC, write_ui_dist
from tests.server_test_helpers import create_environment, create_table

PERSON = "pat_and_session"
"""The person whose token and session are compared: a user holding the instance ADMIN role."""

ENV = "dev"
"""An unprotected environment with a table `bronze.events`."""

MISSING = "parity-missing"
"""The value of a path parameter that names no object the test creates."""

CREDENTIAL_SPECIFIC_ROUTES: dict[tuple[str, str], str] = {
    ("GET", "/api/v1/sign-in/session"): "describes the browser session; a token has none (409)",
    ("DELETE", "/api/v1/sign-in/session"): "signs the browser session out; a token has none",
    ("GET", "/api/v1/principals/{name}/sessions"): (
        "marks the session the request came with as `current`; a token request has none"
    ),
    ("DELETE", "/api/v1/principals/{name}/sessions"): (
        "ending one's own sessions from a browser also clears that browser's cookie"
    ),
    ("POST", "/api/v1/sign-in/setup-link"): (
        "PAT only: the operator's / local person's own token mints the link"
    ),
    ("POST", "/api/v1/sign-in/local-link"): (
        "PAT only: the operator's / local person's own token mints the link"
    ),
    **{
        route: "session only: a ceremony binds to the browser session"
        for route in (
            ("POST", "/api/v1/sign-in/step-up/options"),
            ("POST", "/api/v1/sign-in/step-up"),
            ("POST", "/api/v1/principals/{name}/passkeys/options"),
            ("POST", "/api/v1/principals/{name}/passkeys"),
        )
    },
    **{
        route: "token-only: the cookie is refused (403 TokenOnlyRoute)"
        for route in TOKEN_ONLY_ROUTES
    },
}
"""Operations that answer differently by design, each with the reason."""

CREDENTIAL_READERS: dict[str, set[tuple[str, str]]] = {
    "api/platform/sessions.py": {
        ("GET", "/api/v1/sign-in/session"),
        ("DELETE", "/api/v1/sign-in/session"),
        ("GET", "/api/v1/principals/{name}/sessions"),
        ("DELETE", "/api/v1/principals/{name}/sessions"),
    },
}
"""The modules under `lakana_server/api/` that may read a principal's credential, and the
operations of `CREDENTIAL_SPECIFIC_ROUTES` that are the reason."""

_CREDENTIAL_ATTRIBUTES = frozenset({"session", "credential", "token_id"})
"""`PrincipalRef` fields that tell the credentials apart."""

STATEFUL_READS: dict[tuple[str, str], str] = {
    ("GET", "/api/v1/audit-events"): "the first request's `AUTHENTICATION_SUCCEEDED` row",
    ("GET", "/api/v1/principals/{name}/tokens"): (
        "a token request writes that token's own `last_used_at` before the list is read"
    ),
}
"""Reads whose body the credential's own use changes, with the reason: status only."""

OPEN_OPERATIONS: dict[tuple[str, str], str] = {
    ("POST", "/api/v1/auth/oidc/token"): "takes a CI provider's ID token, not a credential",
    ("POST", "/api/v1/sign-in/passkey"): "usernameless sign-in: the passkey is the proof",
}
"""Operations that read no credential at all; they may answer 401 both ways."""

_PARAMETER = re.compile(r"\{([^}:]+)(?::[^}]*)?\}")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}")
_TOKEN = re.compile(r"^lakana_(pat|task|ci)_")
_KEY_ID = re.compile(r"^[0-9a-f]{16}$|^[0-9a-f]{32}$")
_SIGN_IN_LINK = re.compile(r"^https?://[^#]+/sign-in#l=[\w-]+&s=[\w-]+$")

_BY_SEGMENT = {
    "environments": ENV,
    "principals": PERSON,
    "namespaces": "bronze",
    "tables": "events",
    "columns": "id",
}
"""The value of `{name}` and `{path}` by the literal segment before it."""

_BY_NAME = {
    "env": ENV,
    "environment_name": ENV,
    "principal_name": PERSON,
    "column": "id",
}
"""The value of other path parameters by their name; anything else is `MISSING`."""


def _route_app() -> FastAPI:
    """Build an app, without starting it, only to read its routes.

    Returns
    -------
    FastAPI
        The application `create_app` builds; its database is never opened.
    """
    return create_app(
        Settings(
            database=DatabaseConfig(url="sqlite://"),
            secrets=SecretsConfig(master_key=_TEST_MASTER_KEY),
        )
    )


def _flatten(routes: Iterable[BaseRoute]) -> Iterator[BaseRoute]:
    """Yield every route of an application, looking into the routers it included.

    Parameters
    ----------
    routes
        `app.routes`, or the routes of an included router.

    Yields
    ------
    BaseRoute
        Each route that is not itself an included router.
    """
    for route in routes:
        inner = getattr(route, "original_router", None)
        if inner is not None:
            yield from _flatten(inner.routes)
        else:
            yield route


def _api_routes(app: FastAPI) -> Iterator[APIRoute]:
    """Yield the app's routes below `/api/v1/`.

    Parameters
    ----------
    app
        The application.

    Yields
    ------
    APIRoute
        Each such route.
    """
    for route in _flatten(app.routes):
        if isinstance(route, APIRoute) and route.path.startswith("/api/v1/"):
            yield route


def _all_operations(app: FastAPI) -> list[tuple[str, str]]:
    """List every method and path template of the app below `/api/v1/`.

    Parameters
    ----------
    app
        The application.

    Returns
    -------
    list of tuple of (str, str)
        ``(method, path)``, sorted.
    """
    return sorted(
        (method, route.path) for route in _api_routes(app) for method in route.methods or ()
    )


_OPERATIONS = [op for op in _all_operations(_route_app()) if op not in CREDENTIAL_SPECIFIC_ROUTES]


def _fill_path(path: str, typed: dict[str, str]) -> str:
    """Fill a path template with real objects of the test, where it creates them.

    Parameters
    ----------
    path
        The route's template.
    typed
        A value of the declared type for each parameter that is not a plain string (a UUID
        or a number), from the OpenAPI document.

    Returns
    -------
    str
        The path, each parameter replaced by `_BY_SEGMENT`, `_BY_NAME`, its `typed` value or
        `MISSING`.
    """
    parts = path.split("/")
    out = []
    for i, part in enumerate(parts):
        match = _PARAMETER.fullmatch(part)
        if match is None:
            out.append(part)
            continue
        name = match.group(1)
        before = parts[i - 1] if i else ""
        if name in {"name", "path"} and before in _BY_SEGMENT:
            out.append(_BY_SEGMENT[before])
        else:
            out.append(_BY_NAME.get(name, typed.get(name, MISSING)))
    return "/".join(out)


def _string_for(name: str, schema: dict[str, Any]) -> str:
    """Choose a string a minimal request would send.

    Parameters
    ----------
    name
        The property's or parameter's name.
    schema
        Its JSON schema.

    Returns
    -------
    str
        A value of its format, or one named after what the property holds.
    """
    fmt = schema.get("format")
    if fmt == "date-time":
        return "2026-01-01T00:00:00Z"
    if fmt == "date":
        return "2026-01-01"
    if fmt == "uuid":
        return "00000000-0000-0000-0000-000000000000"
    by_name = {
        "environment": ENV,
        "env": ENV,
        "principal": PERSON,
        "namespace": "bronze",
        "table": "events",
        "sql": "select 1",
        "url": "https://example.com/parity",
    }
    return by_name.get(name, "parity")


def _minimal(schema: dict[str, Any], components: dict[str, Any], name: str = "") -> Any:  # ruff: ignore[any-type]
    """Build the smallest value a JSON schema accepts: required properties only.

    Parameters
    ----------
    schema
        The schema.
    components
        The OpenAPI document's `components.schemas`, for references.
    name
        The property's name, for a string's value.

    Returns
    -------
    Any
        The value.
    """
    if "$ref" in schema:
        return _minimal(components[schema["$ref"].rsplit("/", 1)[1]], components, name)
    for key in ("anyOf", "oneOf", "allOf"):
        if key in schema:
            options = [s for s in schema[key] if s.get("type") != "null"]
            return _minimal(options[0], components, name) if options else None
    if "const" in schema:
        return schema["const"]
    if "enum" in schema:
        return schema["enum"][0]
    if "default" in schema:
        return schema["default"]
    kind = schema.get("type")
    if kind == "object" or "properties" in schema:
        props = schema.get("properties", {})
        return {k: _minimal(props[k], components, k) for k in schema.get("required", [])}
    if kind == "array":
        item = _minimal(schema.get("items", {}), components, name)
        return [item] * schema.get("minItems", 0)
    if kind == "integer":
        return max(1, int(schema.get("minimum", 1)))
    if kind == "number":
        return max(1.0, float(schema.get("minimum", 1.0)))
    if kind == "boolean":
        return False
    return _string_for(name, schema)


@dataclass
class _Request:
    """One case's request, as both credentials send it.

    Parameters
    ----------
    method
        The HTTP method.
    url
        The path with its query.
    body
        The JSON body, or `None` for none.
    """

    method: str
    url: str
    body: Any = None


def _request_for(document: dict[str, Any], method: str, path: str) -> _Request:
    """Build a case's request from the OpenAPI document.

    Parameters
    ----------
    document
        The app's OpenAPI document.
    method
        The HTTP method.
    path
        The route's template.

    Returns
    -------
    _Request
        Path filled by `_fill_path`, every required query parameter and a minimal body.
    """
    operation = document["paths"][path][method.lower()]
    components = document.get("components", {}).get("schemas", {})
    query = [
        f"{p['name']}={_minimal(p.get('schema', {}), components, p['name'])}"
        for p in operation.get("parameters", [])
        if p["in"] == "query" and p.get("required")
    ]
    body = None
    content = operation.get("requestBody", {}).get("content", {})
    if "application/json" in content:
        body = _minimal(content["application/json"].get("schema", {}), components)
    elif method not in {"GET", "HEAD", "OPTIONS"}:
        body = {}
    typed = {
        p["name"]: str(_minimal(p["schema"], components, p["name"]))
        for p in operation.get("parameters", [])
        if p["in"] == "path"
        and (p.get("schema", {}).get("format") or p.get("schema", {}).get("type") != "string")
    }
    url = _fill_path(path, typed) + ("?" + "&".join(query) if query else "")
    return _Request(method, url, body)


def _normalised(value: Any) -> Any:  # ruff: ignore[any-type]
    """Replace what two identical requests made at different moments answer differently.

    A created object's id, a timestamp, a newly issued token's text, a new sign-in link's
    random halves and a WebAuthn challenge differ between the two worlds even when the
    credential plays no part; everything else is compared as it is.

    Parameters
    ----------
    value
        A decoded JSON value.

    Returns
    -------
    Any
        The value with ids, key ids, times, token texts, sign-in links and challenges replaced
        by placeholders, and
        `request_id` dropped.
    """
    if isinstance(value, dict):
        return {
            k: "<challenge>" if k == "challenge" else _normalised(v)
            for k, v in value.items()
            if k != "request_id"
        }
    if isinstance(value, list):
        return [_normalised(v) for v in value]
    if isinstance(value, str):
        if _UUID.match(value):
            return "<uuid>"
        if _TIME.match(value):
            return "<time>"
        if _TOKEN.match(value):
            return "<token>"
        if _SIGN_IN_LINK.match(value):
            return "<sign-in link>"
        if _KEY_ID.match(value):
            return "<key-id>"
    return value


def _body(response: Any) -> Any:  # ruff: ignore[any-type]
    """Read a response's body for comparison.

    Parameters
    ----------
    response
        An httpx response.

    Returns
    -------
    Any
        The decoded, normalised JSON, or the text when it is not JSON.
    """
    try:
        return _normalised(response.json())
    except ValueError:
        return response.text


@dataclass
class _World:
    """The running app, both credentials, and a way back to one database state.

    Parameters
    ----------
    client
        The started client.
    session_headers
        Cookie and CSRF token of the person's session.
    token_headers
        The person's token.
    database
        The SQLite file.
    reached
        The handlers that ran during the current request, by operation.
    """

    client: TestClient
    session_headers: dict[str, str]
    token_headers: dict[str, str]
    database: Path
    reached: set[tuple[str, str]] = field(default_factory=set)

    def snapshot(self) -> Path:
        """Copy the database as it is now.

        Returns
        -------
        Path
            The copy.
        """
        self.client.app.state.engine.dispose()
        copy = self.database.with_suffix(".snapshot")
        shutil.copyfile(self.database, copy)
        return copy

    def restore(self, copy: Path) -> None:
        """Put the database back as `snapshot` copied it.

        Parameters
        ----------
        copy
            The snapshot.
        """
        self.client.app.state.engine.dispose()
        for extra in ("-wal", "-shm"):
            Path(f"{self.database}{extra}").unlink(missing_ok=True)
        shutil.copyfile(copy, self.database)


_REACHED: dict[tuple[str, str], bool] = {}
"""Whether each case's handler ran, for `test_most_cases_reach_their_handler`."""


@contextmanager
def _recording_calls(reached: set[tuple[str, str]]) -> Generator[None]:
    """Make every `/api/v1` handler note that it ran, for apps built inside the block.

    FastAPI builds an included route's handler from the route's `endpoint` when the app
    includes the router, so the endpoints of `lakana_server.app.ROUTERS` are wrapped before
    `create_app` and put back afterwards.

    Parameters
    ----------
    reached
        Where the handlers note ``(method, path)``.

    Yields
    ------
    None
        While the handlers are wrapped.
    """
    originals: list[tuple[APIRoute, Callable[..., Any]]] = []
    routes = [r for router in ROUTERS for r in _flatten(router.routes)]
    for route in routes:
        if not (isinstance(route, APIRoute) and route.path.startswith("/api/v1/")):
            continue
        call = route.endpoint
        key = (sorted(route.methods or ())[0], route.path)

        def wrap(original: Callable[..., Any], key: tuple[str, str]) -> Callable[..., Any]:
            """Wrap one handler.

            Parameters
            ----------
            original
                The handler.
            key
                Its operation.

            Returns
            -------
            Callable
                The handler, noting each call first.
            """
            if inspect.iscoroutinefunction(original):

                @functools.wraps(original)
                async def noted_async(*args: Any, **kwargs: Any) -> Any:  # ruff: ignore[any-type]
                    """Note the call, then run the handler.

                    Parameters
                    ----------
                    *args
                        Passed on.
                    **kwargs
                        Passed on.

                    Returns
                    -------
                    Any
                        The handler's answer.
                    """
                    reached.add(key)
                    return await original(*args, **kwargs)

                return noted_async

            @functools.wraps(original)
            def noted(*args: Any, **kwargs: Any) -> Any:  # ruff: ignore[any-type]
                """Note the call, then run the handler.

                Parameters
                ----------
                *args
                    Passed on.
                **kwargs
                    Passed on.

                Returns
                -------
                Any
                    The handler's answer.
                """
                reached.add(key)
                return original(*args, **kwargs)

            return noted

        originals.append((route, call))
        noted_call = wrap(call, key)
        route.dependant.call = noted_call
        route.endpoint = noted_call
    try:
        yield
    finally:
        for route, call in originals:
            route.dependant.call = call
            route.endpoint = call


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory, catalog_template: Path) -> Iterator[_World]:
    """Start one app with the interface on, and sign the same person in both ways.

    Creates `dev` with `bronze.events`, and a protected `prod`.

    Parameters
    ----------
    tmp_path_factory
        Pytest's session-wide temporary directory factory.
    catalog_template
        The read-only database with the schema already created.

    Yields
    ------
    _World
        The client and both credentials.
    """
    root = tmp_path_factory.mktemp("parity")
    database = root / "lakana.db"
    shutil.copyfile(catalog_template, database)
    database.chmod(0o644)
    settings = Settings(
        database=DatabaseConfig(url=f"sqlite:///{database}"),
        bootstrap_pat_key_id="testkey",
        bootstrap_pat_secret="testsecret",
        secrets=SecretsConfig(master_key=_TEST_MASTER_KEY),
        ui=UiConfig(enabled=True),
    )
    reached: set[tuple[str, str]] = set()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(lakana_server.ui, "UI_DIST", write_ui_dist(root))
        with _recording_calls(reached):
            app = create_app(settings)
        with TestClient(app, base_url=PUBLIC, headers={"Origin": PUBLIC}) as client:
            r = client.post(
                "/api/v1/principals", headers=AUTH_HEADER, json={"name": PERSON, "type": "USER"}
            )
            assert r.status_code == 201, r.text
            with Session(app.state.engine) as session:
                row = session.execute(
                    select(Principal).where(Principal.name == PERSON)
                ).scalar_one()
                admin = session.execute(
                    select(Principal).where(Principal.name == "admin")
                ).scalar_one()
                acting = PrincipalRef(id=admin.id, name=admin.name, type=PrincipalType.USER)
                GrantService(session, AllowAllAuthorization()).grant(
                    acting, principal=PERSON, role=Role.ADMIN, kind=SecurableKind.INSTANCE
                )
                person = PrincipalRef(id=row.id, name=row.name, type=row.type)
                token = (
                    PatService(session, AllowAllAuthorization())
                    .issue(person, PERSON, name="p")
                    .token
                )
                started = SessionService(session, AllowAllAuthorization()).start(
                    row.id, via="passkey", replacing=None, peer="t", user_agent="t", touched=True
                )
                session.commit()
            create_environment(client, ENV)
            create_table(client, ENV, "bronze", "events")
            create_environment(client, "prod")
            r = client.patch(
                "/api/v1/environments/prod", headers=AUTH_HEADER, json={"is_protected": True}
            )
            assert r.status_code == 200, r.text
            name = cookie_name(settings.public_origin())
            cookie = {"Cookie": f"{name}={started.cookie_value}", CSRF_HEADER: started.csrf_token}
            yield _World(client, cookie, {"Authorization": f"Bearer {token}"}, database, reached)


def _both(world: _World, request: _Request) -> tuple[Any, Any]:
    """Send one request with the session and the same with the token, from one database state.

    The token's request runs against the state the session's request started from, and the
    state is put back afterwards, so a write is compared with the same write and later cases see
    the objects `world` created.

    Parameters
    ----------
    world
        The app and both credentials.
    request
        The request.

    Returns
    -------
    tuple
        The session's response and the token's.
    """
    client = world.client
    copy = world.snapshot()
    try:
        by_session = client.request(
            request.method, request.url, headers=world.session_headers, json=request.body
        )
        world.restore(copy)
        by_token = client.request(
            request.method, request.url, headers=world.token_headers, json=request.body
        )
    finally:
        world.restore(copy)
    return by_session, by_token


@pytest.mark.parametrize(("method", "path"), _OPERATIONS, ids=[f"{m} {p}" for m, p in _OPERATIONS])
def test_a_session_answers_exactly_as_the_persons_token(
    world: _World, method: str, path: str
) -> None:
    """Same status, same normalised body (status only for `STATEFUL_READS`), same cookies."""
    document = world.client.app.openapi()
    request = _request_for(document, method, path)
    world.reached.clear()
    by_session, by_token = _both(world, request)
    _REACHED[method, path] = bool(world.reached)
    if (method, path) not in OPEN_OPERATIONS:
        assert by_session.status_code != 401, by_session.text
    assert by_session.status_code == by_token.status_code, (by_session.text, by_token.text)
    if (method, path) not in STATEFUL_READS:
        assert _body(by_session) == _body(by_token)
    assert by_session.headers.get("set-cookie") == by_token.headers.get("set-cookie")


def test_most_cases_reach_their_handler() -> None:
    """At least nine in ten cases run their handler, so the comparison sees real answers."""
    if len(_REACHED) < len(_OPERATIONS):
        pytest.skip("runs after the whole parametrized comparison only")
    reached = sum(_REACHED.values())
    print(f"parity: {reached} of {len(_REACHED)} cases reached their handler")
    print("not reached:", sorted(k for k, v in _REACHED.items() if not v))
    assert reached >= 0.9 * len(_REACHED), sorted(k for k, v in _REACHED.items() if not v)


def test_break_glass_answers_a_session_as_the_token(world: _World) -> None:
    """A protected write is refused without a reason and handled alike with one, both ways."""
    url = "/api/v1/environments/prod/namespaces/bronze"
    refused = _both(world, _Request("DELETE", url))
    assert refused[0].status_code == refused[1].status_code == 403
    assert _body(refused[0]) == _body(refused[1])
    broken = _both(world, _Request("DELETE", f"{url}?break_glass_reason=parity"))
    assert broken[0].status_code == broken[1].status_code != 403, broken[0].text
    assert _body(broken[0]) == _body(broken[1])


def test_credential_specific_routes_exist() -> None:
    """Each exception names an operation the app has, so the list cannot go stale."""
    operations = set(_all_operations(_route_app()))
    assert set(CREDENTIAL_SPECIFIC_ROUTES) <= operations
    assert set(STATEFUL_READS) <= operations
    assert set(OPEN_OPERATIONS) <= operations
    for reasons in CREDENTIAL_READERS.values():
        assert reasons <= set(CREDENTIAL_SPECIFIC_ROUTES)


def _credential_reads(source: str) -> list[int]:
    """Find the reads of a credential-telling attribute in a module.

    Parameters
    ----------
    source
        The module's text.

    Returns
    -------
    list of int
        The line of each `x.session`, `x.credential` or `x.token_id`, and of each `getattr` with
        one of those names.
    """
    lines = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Attribute) and node.attr in _CREDENTIAL_ATTRIBUTES:
            lines.append(node.lineno)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in {"getattr", "hasattr"}
            and len(node.args) > 1
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value in _CREDENTIAL_ATTRIBUTES
        ):
            lines.append(node.lineno)
    return lines


def test_no_route_module_branches_on_the_credential() -> None:
    """Only the modules of `CREDENTIAL_READERS` read a principal's session or credential kind."""
    api = Path(lakana_server.__file__).parent / "api"
    found = {
        str(path.relative_to(api.parent)): lines
        for path in sorted(api.rglob("*.py"))
        if (lines := _credential_reads(path.read_text()))
    }
    assert set(found) <= set(CREDENTIAL_READERS), found


def test_the_guard_finds_a_credential_read() -> None:
    """The guard sees an attribute read and a `getattr` alike."""
    assert _credential_reads("p.session\nx = getattr(p, 'credential')\np.name\n") == [1, 2]
