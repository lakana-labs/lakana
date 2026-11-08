"""Every action that hands out access asks a browser session for a fresh passkey touch.

The spec's core set (decided rules, "Step-up in part 1b"): adding or removing a passkey, issuing
invites and recovery links, creating API tokens, granting or changing rights, and ending other
people's sessions. `STEP_UP_ROUTES` lists each operation with a request that succeeds; the
dynamic test sends it with a session whose last touch is 16 minutes old (403
`StepUpRequired`), with the same session after a touch, and with a token, which has no session
and is never asked. `NO_STEP_UP` lists, with the reason, every other write below the prefixes
that own identities and rights, so a new route there cannot skip the decision unnoticed.
"""

from __future__ import annotations

import itertools
import shutil
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

import lakana_server.ui
from lakana_core.auth import AllowAllAuthorization
from lakana_core.ci_oidc.config import CiOidcConfig
from lakana_core.config import DatabaseConfig, SecretsConfig
from lakana_core.identity.sessions import SessionService
from lakana_core.models import Passkey, Principal
from lakana_server.app import create_app
from lakana_server.settings import Settings, UiConfig
from tests.conftest import _TEST_MASTER_KEY, AUTH_HEADER, PUBLIC, write_ui_dist
from tests.server_test_helpers import create_environment, create_table, session_headers, signed_in
from tests.test_session_parity import _all_operations, _route_app

Request = tuple[str, dict[str, Any] | None]
"""A path and a JSON body (`None` for none)."""

_NAMES = itertools.count()


def _unique(prefix: str) -> str:
    """Name a new object so cases sharing one app never collide.

    Parameters
    ----------
    prefix
        The start of the name.

    Returns
    -------
    str
        `prefix` and a running number.
    """
    return f"{prefix}{next(_NAMES)}"


def _ok(response: Any, status: int = 201) -> dict[str, Any]:  # ruff: ignore[any-type]
    """Assert a setup request succeeded and read its body.

    Parameters
    ----------
    response
        The response.
    status
        The expected status.

    Returns
    -------
    dict
        The JSON body, empty for a 204.
    """
    assert response.status_code == status, response.text
    return {} if status == 204 else dict(response.json())


def _principal(client: TestClient, kind: str = "USER") -> str:
    """Create a principal with no rights.

    Parameters
    ----------
    client
        The client.
    kind
        `USER` or `SERVICE_ACCOUNT`.

    Returns
    -------
    str
        Its name.
    """
    name = _unique("p")
    _ok(client.post("/api/v1/principals", headers=AUTH_HEADER, json={"name": name, "type": kind}))
    return name


def _group(client: TestClient) -> str:
    """Create an empty group.

    Parameters
    ----------
    client
        The client.

    Returns
    -------
    str
        Its name.
    """
    name = _unique("g")
    _ok(client.post("/api/v1/groups", headers=AUTH_HEADER, json={"name": name}))
    return name


def _rule(client: TestClient) -> str:
    """Create a CI trust rule for a new service account.

    Parameters
    ----------
    client
        The client.

    Returns
    -------
    str
        The rule's path.
    """
    body = {
        "issuer": "github",
        "principal": _principal(client, "SERVICE_ACCOUNT"),
        "environment": "dev",
        "repository": "analytics",
        "conditions": {"repository_id": "1", "ref": "refs/heads/main", "event_name": "push"},
    }
    created = _ok(client.post("/api/v1/ci-trust-rules", headers=AUTH_HEADER, json=body))
    return f"/api/v1/ci-trust-rules/{created['id']}"


def _policy_body(client: TestClient, **extra: str) -> dict[str, Any]:
    """Build a row filter's or column mask's body for a new person on `dev.bronze.events`.

    Parameters
    ----------
    client
        The client.
    **extra
        The kind's own fields.

    Returns
    -------
    dict
        The body.
    """
    target = {"environment": "dev", "namespace": "bronze", "table": "events"}
    return {"principal": _principal(client), **target, **extra}


def _access_apply(client: TestClient) -> Request:
    """Plan an access file that manages only `acl`, so the other cases' targets stay unmanaged.

    Parameters
    ----------
    client
        The client.

    Returns
    -------
    tuple
        The apply path and its body.
    """
    text = "version: 1\nmanages: [acl]\n"
    plan = _ok(client.post("/api/v1/access/plan", headers=AUTH_HEADER, json={"text": text}), 200)
    return "/api/v1/access/apply", {"text": text, "plan_fingerprint": plan["fingerprint"]}


def _passkey(client: TestClient) -> str:
    """Store a passkey for a new person, as a finished registration would.

    Parameters
    ----------
    client
        The client.

    Returns
    -------
    str
        The passkey's path.
    """
    name = _principal(client)
    app = cast(FastAPI, client.app)
    with Session(app.state.engine) as session:
        owner = session.execute(select(Principal.id).where(Principal.name == name)).scalar_one()
        row = Passkey(
            id=uuid.uuid4(),
            principal_id=owner,
            credential_id=uuid.uuid4().hex,
            public_key=b"key",
            sign_count=0,
            rp_id="localhost",
            name="laptop",
            backup_eligible=False,
            created_at=datetime.now(UTC),
            created_user_agent="pytest",
        )
        session.add(row)
        session.commit()
        return f"/api/v1/principals/{name}/passkeys/{row.id}"


@dataclass(frozen=True)
class Case:
    """A request a step-up route answers with success once the session has a fresh touch.

    Parameters
    ----------
    build
        Creates what the request needs and returns its path and body; called once per
        credential, so a request that removes something has something to remove each time.
    ok
        The success status.
    token
        The status for a token; `ok` by default. Differs only for a session-only ceremony.
    """

    build: Callable[[TestClient], Request]
    ok: int
    token: int | None = None


def _grant(client: TestClient) -> dict[str, Any]:
    """Build a grant's body for a new person.

    Parameters
    ----------
    client
        The client.

    Returns
    -------
    dict
        The body.
    """
    return {
        "principal": _principal(client),
        "role": "ANALYST",
        "kind": "ENVIRONMENT",
        "environment": "dev",
    }


def _granted(client: TestClient) -> Request:
    """Create a grant to revoke.

    Parameters
    ----------
    client
        The client.

    Returns
    -------
    tuple
        Its path and no body.
    """
    created = _ok(client.post("/api/v1/grants", headers=AUTH_HEADER, json=_grant(client)))
    return f"/api/v1/grants/{created['id']}", None


def _member(client: TestClient) -> Request:
    """Make a new person a member of a new group.

    Parameters
    ----------
    client
        The client.

    Returns
    -------
    tuple
        The membership's path and no body.
    """
    path = f"/api/v1/groups/{_group(client)}/members/{_principal(client)}"
    _ok(client.put(path, headers=AUTH_HEADER), 204)
    return path, None


def _full_group(client: TestClient) -> Request:
    """Create a group with a member, whose rights deleting the group removes.

    Parameters
    ----------
    client
        The client.

    Returns
    -------
    tuple
        The group's path and no body.
    """
    group = _group(client)
    _ok(
        client.put(f"/api/v1/groups/{group}/members/{_principal(client)}", headers=AUTH_HEADER), 204
    )
    return f"/api/v1/groups/{group}", None


def _created(
    collection: str, body: Callable[[TestClient], dict[str, Any]]
) -> Callable[..., Request]:
    """Build a case that creates a policy and answers the path that revokes it.

    Parameters
    ----------
    collection
        `row-filters` or `column-masks`.
    body
        Builds the creation body.

    Returns
    -------
    Callable
        The case's `build`.
    """

    def build(client: TestClient) -> Request:
        """Create the policy.

        Parameters
        ----------
        client
            The client.

        Returns
        -------
        tuple
            Its path and no body.
        """
        url = f"/api/v1/{collection}"
        created = _ok(client.post(url, headers=AUTH_HEADER, json=body(client)))
        return f"{url}/{created['id']}", None

    return build


def _row_filter(client: TestClient) -> dict[str, Any]:
    """Build a row filter's body.

    Parameters
    ----------
    client
        The client.

    Returns
    -------
    dict
        The body.
    """
    return _policy_body(client, expression="id > 5")


def _column_mask(client: TestClient) -> dict[str, Any]:
    """Build a column mask's body.

    Parameters
    ----------
    client
        The client.

    Returns
    -------
    dict
        The body.
    """
    return _policy_body(client, column="id", expression="NULL")


def _disabled_rule(client: TestClient) -> Request:
    """Create a disabled CI trust rule to enable.

    Parameters
    ----------
    client
        The client.

    Returns
    -------
    tuple
        The enable path and no body.
    """
    rule = _rule(client)
    _ok(client.post(f"{rule}/disable", headers=AUTH_HEADER), 200)
    return f"{rule}/enable", None


STEP_UP_ROUTES: dict[tuple[str, str], Case] = {
    ("POST", "/api/v1/principals/{name}/tokens"): Case(
        lambda c: (f"/api/v1/principals/{_principal(c, 'SERVICE_ACCOUNT')}/tokens", {"name": "t"}),
        201,
    ),
    ("POST", "/api/v1/principals"): Case(
        lambda c: ("/api/v1/principals", {"name": _unique("s"), "type": "SERVICE_ACCOUNT"}), 201
    ),
    ("DELETE", "/api/v1/principals/{name}"): Case(
        lambda c: (f"/api/v1/principals/{_principal(c)}", None), 204
    ),
    ("POST", "/api/v1/grants"): Case(lambda c: ("/api/v1/grants", _grant(c)), 201),
    ("DELETE", "/api/v1/grants/{grant_id}"): Case(_granted, 204),
    ("PUT", "/api/v1/groups/{name}/members/{principal_name}"): Case(
        lambda c: (f"/api/v1/groups/{_group(c)}/members/{_principal(c)}", None), 204
    ),
    ("DELETE", "/api/v1/groups/{name}/members/{principal_name}"): Case(_member, 204),
    ("DELETE", "/api/v1/groups/{name}"): Case(_full_group, 204),
    ("POST", "/api/v1/access/apply"): Case(_access_apply, 200),
    ("POST", "/api/v1/row-filters"): Case(lambda c: ("/api/v1/row-filters", _row_filter(c)), 201),
    ("DELETE", "/api/v1/row-filters/{row_filter_id}"): Case(
        _created("row-filters", _row_filter), 204
    ),
    ("POST", "/api/v1/column-masks"): Case(
        lambda c: ("/api/v1/column-masks", _column_mask(c)), 201
    ),
    ("DELETE", "/api/v1/column-masks/{column_mask_id}"): Case(
        _created("column-masks", _column_mask), 204
    ),
    ("POST", "/api/v1/ci-trust-rules"): Case(
        lambda c: (
            "/api/v1/ci-trust-rules",
            {
                "issuer": "github",
                "principal": _principal(c, "SERVICE_ACCOUNT"),
                "environment": "dev",
                "repository": "analytics",
                "conditions": {
                    "repository_id": "1",
                    "ref": "refs/heads/main",
                    "event_name": "push",
                },
            },
        ),
        201,
    ),
    ("PATCH", "/api/v1/ci-trust-rules/{rule_id}"): Case(
        lambda c: (_rule(c), {"kinds": ["deploy", "dry_run"]}), 200
    ),
    ("POST", "/api/v1/ci-trust-rules/{rule_id}/enable"): Case(_disabled_rule, 200),
    ("POST", "/api/v1/ci-trust-rules/{rule_id}/disable"): Case(
        lambda c: (f"{_rule(c)}/disable", None), 200
    ),
    ("DELETE", "/api/v1/ci-trust-rules/{rule_id}"): Case(lambda c: (_rule(c), None), 204),
    ("DELETE", "/api/v1/principals/{name}/sessions"): Case(
        lambda c: (f"/api/v1/principals/{_principal(c)}/sessions", None), 204
    ),
    ("PATCH", "/api/v1/settings/sign-in"): Case(
        lambda c: (
            "/api/v1/settings/sign-in",
            {"idle_timeout_minutes": 720, "absolute_lifetime_minutes": 10080},
        ),
        200,
    ),
    ("POST", "/api/v1/invites"): Case(lambda c: ("/api/v1/invites", {"name": _unique("i")}), 201),
    ("POST", "/api/v1/principals/{name}/recovery-link"): Case(
        lambda c: (f"/api/v1/principals/{_principal(c)}/recovery-link", None), 201
    ),
    ("POST", "/api/v1/principals/{name}/passkeys/options"): Case(
        lambda c: ("/api/v1/principals/admin/passkeys/options", None), 200, token=409
    ),
    ("DELETE", "/api/v1/principals/{name}/passkeys/{passkey_id}"): Case(
        lambda c: (_passkey(c), None), 204
    ),
}
"""Each operation that needs a fresh passkey touch in a browser session (`require_step_up`),
with a request it answers with success. `DELETE .../sessions` is another person's (one's own
sessions end without step-up). Adding a passkey is a session-only ceremony: a token gets 409
there for that reason, not for step-up; its second half (`POST .../passkeys`) needs no touch of
its own because it answers a challenge only the options route hands out."""

SENSITIVE_PREFIXES = (
    "/api/v1/principals",
    "/api/v1/grants",
    "/api/v1/groups",
    "/api/v1/access",
    "/api/v1/ci-trust-rules",
    "/api/v1/row-filters",
    "/api/v1/column-masks",
    "/api/v1/invites",
    "/api/v1/recovery-links",
    "/api/v1/settings/sign-in",
    "/api/v1/task-tokens",
    "/api/v1/sign-in",
    "/api/v1/auth",
    "/api/v1/whoami",
)
"""The paths that own identities, credentials and rights: every write below them is either in
`STEP_UP_ROUTES` or in `NO_STEP_UP` with a reason."""

NO_STEP_UP: dict[tuple[str, str], str] = {
    ("POST", "/api/v1/groups"): "a new group is empty and grants nothing",
    ("DELETE", "/api/v1/principals/{name}/tokens/{key_id}"): "revoking a token removes access",
    ("DELETE", "/api/v1/invites/{invite_id}"): "withdrawing an invite removes access",
    ("DELETE", "/api/v1/recovery-links/{link_id}"): "withdrawing a link removes access",
    ("POST", "/api/v1/access/plan"): "a plan changes nothing",
    ("POST", "/api/v1/ci-trust-rules/explain"): "explaining a token's match changes nothing",
    ("POST", "/api/v1/principals/{name}/passkeys"): (
        "answers a registration challenge that only the step-up options route hands out"
    ),
    ("PATCH", "/api/v1/whoami"): "one's own interface language",
    ("DELETE", "/api/v1/sign-in/session"): "signing one's own browser out",
    ("DELETE", "/api/v1/sign-in/notices/{notice_id}"): "dismissing one's own notice",
    ("POST", "/api/v1/sign-in/passkey/options"): "signing in: the passkey is the proof",
    ("POST", "/api/v1/sign-in/passkey"): "signing in: the passkey is the proof",
    ("POST", "/api/v1/sign-in/step-up/options"): "the step-up ceremony itself",
    ("POST", "/api/v1/sign-in/step-up"): "the step-up ceremony itself",
    ("POST", "/api/v1/sign-in/links/describe"): "reads a link without using it",
    ("POST", "/api/v1/sign-in/links/redeem"): "signing in: the link is the proof",
    ("POST", "/api/v1/sign-in/setup-link"): "PAT only (the operator's token); no session",
    ("POST", "/api/v1/sign-in/local-link"): "PAT only (the local person's token); no session",
    ("POST", "/api/v1/task-tokens"): "token-only route: a session never reaches it",
    ("DELETE", "/api/v1/task-tokens/{key_id}"): "token-only route: a session never reaches it",
    ("POST", "/api/v1/auth/oidc/token"): "a CI provider's ID token, never a session",
}
"""Writes below `SENSITIVE_PREFIXES` that need no step-up, each with the reason."""


@pytest.fixture(scope="module")
def client(
    tmp_path_factory: pytest.TempPathFactory, catalog_template: Path
) -> Iterator[TestClient]:
    """Start one app with the interface on, CI tokens, `dev.bronze.events`, `acl` and a repository.

    Parameters
    ----------
    tmp_path_factory
        Pytest's session-wide temporary directory factory.
    catalog_template
        The read-only database with the schema already created.

    Yields
    ------
    TestClient
        A client at the public address.
    """
    root = tmp_path_factory.mktemp("step-up")
    database = root / "lakana.db"
    shutil.copyfile(catalog_template, database)
    database.chmod(0o644)
    settings = Settings(
        database=DatabaseConfig(url=f"sqlite:///{database}"),
        bootstrap_pat_key_id="testkey",
        bootstrap_pat_secret="testsecret",
        secrets=SecretsConfig(master_key=_TEST_MASTER_KEY),
        ui=UiConfig(enabled=True),
        ci_oidc=CiOidcConfig(issuers=["github"], audience="https://l.example"),
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(lakana_server.ui, "UI_DIST", write_ui_dist(root))
        with TestClient(create_app(settings), base_url=PUBLIC, headers={"Origin": PUBLIC}) as c:
            create_environment(c, "dev", root=f"file://{root}/dev")
            create_table(c, "dev", "bronze", "events")
            create_environment(c, "acl", root=f"file://{root}/acl")
            _ok(
                c.post(
                    "/api/v1/repositories",
                    headers=AUTH_HEADER,
                    json={
                        "name": "analytics",
                        "kind": "https",
                        "url": "https://github.com/o/a.git",
                    },
                )
            )
            yield c


def _send(client: TestClient, method: str, request: Request, headers: dict[str, str]) -> Any:  # ruff: ignore[any-type]
    """Send one request.

    Parameters
    ----------
    client
        The client.
    method
        The HTTP method.
    request
        The path and body.
    headers
        The credential's headers.

    Returns
    -------
    Any
        The response.
    """
    path, body = request
    return client.request(method, path, headers=headers, json=body)


def _touch(client: TestClient, cookie_value: str) -> None:
    """Record a passkey touch now, as `POST /api/v1/sign-in/step-up` does.

    Parameters
    ----------
    client
        The client.
    cookie_value
        The session's cookie value.
    """
    app = cast(FastAPI, client.app)
    with Session(app.state.engine) as session:
        SessionService(session, AllowAllAuthorization()).touch(cookie_value.split("_", 1)[0])
        session.commit()


@pytest.mark.parametrize(("method", "path"), list(STEP_UP_ROUTES))
def test_a_stale_session_is_asked_for_a_touch_and_a_token_never(
    client: TestClient, method: str, path: str
) -> None:
    """Touched 16 minutes ago: 403 naming the next step; after a touch, or by token: done."""
    case = STEP_UP_ROUTES[method, path]
    stale = datetime.now(UTC) - timedelta(minutes=16)
    name, value, csrf = signed_in(client, "admin", touched_at=stale)
    headers = session_headers(name, value, csrf)
    request = case.build(client)
    refused = _send(client, method, request, headers)
    assert refused.status_code == 403, refused.text
    assert refused.json()["type"] == "StepUpRequired"
    assert "passkey" in refused.json()["detail"]
    _touch(client, value)
    done = _send(client, method, request, headers)
    assert done.status_code == case.ok, done.text
    by_token = _send(client, method, case.build(client), AUTH_HEADER)
    assert by_token.status_code == (case.token or case.ok), by_token.text


def test_every_listed_route_exists() -> None:
    """A renamed or removed route cannot leave a stale entry behind."""
    operations = set(_all_operations(_route_app()))
    assert set(STEP_UP_ROUTES) - operations == set()
    assert set(NO_STEP_UP) - operations == set()
    assert set(STEP_UP_ROUTES) & set(NO_STEP_UP) == set()


def test_every_write_on_identities_and_rights_decides_on_step_up() -> None:
    """A new write below the identity and rights prefixes must join one of the two lists."""
    writes = {
        (method, path)
        for method, path in _all_operations(_route_app())
        if method != "GET" and path.startswith(SENSITIVE_PREFIXES)
    }
    assert writes - set(STEP_UP_ROUTES) - set(NO_STEP_UP) == set()
