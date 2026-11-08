"""Every `/api/v1` route answers a browser session exactly as it answers the person's token.

The session is a credential of `get_principal`, so authorization, break-glass rules, body
limits and audit apply unchanged (spec, "Sessions"). This test holds that by construction: it
walks the running app's routes, so a new route is covered the day it is added.
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.routing import BaseRoute

import lakana_server.ui
from lakana_core.auth import AllowAllAuthorization, PrincipalRef, SecurableKind
from lakana_core.config import DatabaseConfig, SecretsConfig
from lakana_core.grants.roles import Role
from lakana_core.grants.service import GrantService
from lakana_core.identity.pats import PatService
from lakana_core.identity.sessions import SessionService
from lakana_core.models import Principal, PrincipalType
from lakana_server.app import create_app
from lakana_server.browser_session import CSRF_HEADER, cookie_name
from lakana_server.deps import TOKEN_ONLY_ROUTES
from lakana_server.settings import Settings, UiConfig
from tests.conftest import _TEST_MASTER_KEY, AUTH_HEADER, PUBLIC, write_ui_dist
from tests.server_test_helpers import create_environment

PERSON = "pat_and_session"
"""The person whose token and session are compared: a user holding the instance ADMIN role."""

MISSING = "parity-missing"
"""The value of every path parameter."""

CREDENTIAL_SPECIFIC_ROUTES: dict[tuple[str, str], str] = {
    ("GET", "/api/v1/sign-in/session"): "describes the browser session; a token has none (409)",
    ("DELETE", "/api/v1/sign-in/session"): "signs the browser session out; a token has none",
    **{
        route: "token-only: the cookie is refused (403 TokenOnlyRoute)"
        for route in TOKEN_ONLY_ROUTES
    },
}
"""Operations that answer differently by design, each with the reason."""

STATEFUL_READS = {("GET", "/api/v1/audit-events")}
"""Reads whose body the first request changes (its `AUTHENTICATION_SUCCEEDED` row): status only."""

VOLATILE_KEYS = {"request_id"}
"""Keys dropped from both bodies before they are compared."""

_PARAMETER = re.compile(r"\{[^}]+\}")


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
        (method, route.path)
        for route in _flatten(app.routes)
        if isinstance(route, APIRoute) and route.path.startswith("/api/v1/")
        for method in route.methods or ()
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


def _api_v1_operations() -> list[tuple[str, str]]:
    """List the operations whose answers must not depend on the credential.

    Returns
    -------
    list of tuple of (str, str)
        Every operation below `/api/v1/` except `CREDENTIAL_SPECIFIC_ROUTES`.
    """
    return [op for op in _all_operations(_route_app()) if op not in CREDENTIAL_SPECIFIC_ROUTES]


def _ids(operation: tuple[str, str]) -> str:
    """Name a test case.

    Parameters
    ----------
    operation
        ``(method, path)``.

    Returns
    -------
    str
        ``METHOD path``.
    """
    return f"{operation[0]} {operation[1]}"


def _without_volatile(value: Any) -> Any:  # ruff: ignore[any-type]
    """Drop `VOLATILE_KEYS` at every depth of a JSON value.

    Parameters
    ----------
    value
        A decoded JSON value.

    Returns
    -------
    Any
        The same value without those keys.
    """
    if isinstance(value, dict):
        return {k: _without_volatile(v) for k, v in value.items() if k not in VOLATILE_KEYS}
    if isinstance(value, list):
        return [_without_volatile(v) for v in value]
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
        The decoded JSON without volatile keys, or the text when it is not JSON.
    """
    try:
        return _without_volatile(response.json())
    except ValueError:
        return response.text


@pytest.fixture(scope="module")
def app_client(
    tmp_path_factory: pytest.TempPathFactory, catalog_template: Path
) -> Iterator[tuple[TestClient, dict[str, str], dict[str, str]]]:
    """Start one app with the interface on, and sign the same person in both ways.

    Parameters
    ----------
    tmp_path_factory
        Pytest's session-wide temporary directory factory.
    catalog_template
        The read-only database with the schema already created.

    Yields
    ------
    tuple of (TestClient, dict, dict)
        The client, the session's headers (cookie, CSRF token) and the token's headers.
    """
    root = tmp_path_factory.mktemp("parity")
    database = root / "lakana.db"
    shutil.copyfile(catalog_template, database)
    settings = Settings(
        database=DatabaseConfig(url=f"sqlite:///{database}"),
        bootstrap_pat_key_id="testkey",
        bootstrap_pat_secret="testsecret",
        secrets=SecretsConfig(master_key=_TEST_MASTER_KEY),
        ui=UiConfig(enabled=True),
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(lakana_server.ui, "UI_DIST", write_ui_dist(root))
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
            create_environment(client, "prod")
            r = client.patch(
                "/api/v1/environments/prod", headers=AUTH_HEADER, json={"is_protected": True}
            )
            assert r.status_code == 200, r.text
            name = cookie_name(settings.public_origin())
            cookie = {"Cookie": f"{name}={started.cookie_value}", CSRF_HEADER: started.csrf_token}
            yield client, cookie, {"Authorization": f"Bearer {token}"}


def _both(
    app_client: tuple[TestClient, dict[str, str], dict[str, str]], method: str, url: str
) -> tuple[Any, Any]:
    """Send one request with the session, then the same with the token.

    Parameters
    ----------
    app_client
        The client and both credentials' headers.
    method
        The HTTP method.
    url
        The path, with its query.

    Returns
    -------
    tuple
        The session's response and the token's.
    """
    client, cookie, bearer = app_client
    body = None if method in {"GET", "HEAD", "OPTIONS"} else {}
    by_session = client.request(method, url, headers=cookie, json=body)
    by_token = client.request(method, url, headers=bearer, json=body)
    return by_session, by_token


_OPERATIONS = _api_v1_operations()


@pytest.mark.parametrize(("method", "path"), _OPERATIONS, ids=[_ids(op) for op in _OPERATIONS])
def test_a_session_answers_exactly_as_the_persons_token(
    app_client: tuple[TestClient, dict[str, str], dict[str, str]], method: str, path: str
) -> None:
    """Same status and, but for `VOLATILE_KEYS` and `STATEFUL_READS`, the same body."""
    url = _PARAMETER.sub(MISSING, path)
    by_session, by_token = _both(app_client, method, url)
    assert by_session.status_code != 401, by_session.text
    assert by_session.status_code == by_token.status_code, (by_session.text, by_token.text)
    if (method, path) not in STATEFUL_READS:
        assert _body(by_session) == _body(by_token)


def test_break_glass_answers_a_session_as_the_token(
    app_client: tuple[TestClient, dict[str, str], dict[str, str]],
) -> None:
    """A protected write is refused without a reason and handled alike with one, both ways."""
    url = f"/api/v1/environments/prod/namespaces/bronze.{MISSING}"
    refused = _both(app_client, "DELETE", url)
    assert refused[0].status_code == refused[1].status_code == 403
    assert _body(refused[0]) == _body(refused[1])
    broken = _both(app_client, "DELETE", f"{url}?break_glass_reason=parity")
    assert broken[0].status_code == broken[1].status_code != 403, broken[0].text
    assert _body(broken[0]) == _body(broken[1])


def test_credential_specific_routes_exist() -> None:
    """Each exception names an operation the app has, so the list cannot go stale."""
    operations = set(_all_operations(_route_app()))
    assert set(CREDENTIAL_SPECIFIC_ROUTES) <= operations
    assert set(STATEFUL_READS) <= operations
