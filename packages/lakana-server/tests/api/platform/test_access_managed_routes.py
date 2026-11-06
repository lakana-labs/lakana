"""The routes that change access refuse a scope an access file manages, and the override (#46)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from lakana_core.ci_oidc.config import CiOidcConfig
from lakana_core.models import AccessManagedScope
from lakana_server.app import create_app
from lakana_server.settings import Settings
from tests.conftest import AUTH_HEADER
from tests.server_test_helpers import create_environment, create_table

_OVERRIDE = {"override_managed": "true", "override_reason": "Incident 42 hotfix"}
_RULES = "/api/v1/ci-trust-rules"
_RULE: dict[str, Any] = {
    "issuer": "github",
    "principal": "deployer",
    "environment": "prod",
    "repository": "analytics",
    "conditions": {"repository_id": "123456", "ref": "refs/heads/main", "event_name": "push"},
}
_TABLE = {"environment": "prod", "namespace": "gold", "table": "orders"}

# (method, path, body): the path's `{...}` are filled from `_seed`'s ids.
_ROUTES: dict[str, tuple[str, str, dict[str, Any] | None]] = {
    "grant": (
        "POST",
        "/api/v1/grants",
        {"principal": "bob", "role": "VIEWER", "kind": "ENVIRONMENT", "environment": "prod"},
    ),
    "revoke": ("DELETE", "/api/v1/grants/{grant}", None),
    "create_group": ("POST", "/api/v1/groups", {"name": "ops"}),
    "delete_group": ("DELETE", "/api/v1/groups/empty", None),
    "add_member": ("PUT", "/api/v1/groups/team/members/bob", None),
    "remove_member": ("DELETE", "/api/v1/groups/team/members/alice", None),
    "create_service_account": (
        "POST",
        "/api/v1/principals",
        {"name": "bot2", "type": "SERVICE_ACCOUNT"},
    ),
    "deactivate_service_account": ("DELETE", "/api/v1/principals/deployer", None),
    "create_row_filter": (
        "POST",
        "/api/v1/row-filters",
        {**_TABLE, "principal": "bob", "expression": "id > 5"},
    ),
    "revoke_row_filter": ("DELETE", "/api/v1/row-filters/{row_filter}", None),
    "create_column_mask": (
        "POST",
        "/api/v1/column-masks",
        {**_TABLE, "principal": "bob", "column": "id", "expression": "NULL"},
    ),
    "revoke_column_mask": ("DELETE", "/api/v1/column-masks/{column_mask}", None),
    "create_ci_rule": (
        "POST",
        _RULES,
        {**_RULE, "conditions": {**_RULE["conditions"], "ref": "refs/heads/release"}},
    ),
    "update_ci_rule": ("PATCH", _RULES + "/{rule}", {"kinds": ["deploy", "dry_run"]}),
    "enable_ci_rule": ("POST", _RULES + "/{rule_off}/enable", None),
    "disable_ci_rule": ("POST", _RULES + "/{rule}/disable", None),
    "delete_ci_rule": ("DELETE", _RULES + "/{rule}", None),
}


@pytest.fixture
def managed_client(settings: Settings) -> Iterator[TestClient]:
    """Provide a client of a server that accepts GitHub's ID tokens.

    Parameters
    ----------
    settings
        The base settings.

    Yields
    ------
    TestClient
        The client.
    """
    configured = settings.model_copy(
        update={"ci_oidc": CiOidcConfig(issuers=["github"], audience="https://l.example")}
    )
    with TestClient(create_app(configured)) as client:
        yield client


def _post(client: TestClient, path: str, body: dict[str, Any]) -> dict[str, Any]:
    """Make something as the administrator.

    Parameters
    ----------
    client
        The client.
    path
        The route.
    body
        The JSON body.

    Returns
    -------
    dict
        The answer's body, or an empty one.
    """
    response = client.post(path, headers=AUTH_HEADER, json=body)
    assert response.status_code in (200, 201, 204), response.text
    return response.json() if response.content else {}


def _seed(client: TestClient) -> dict[str, str]:
    """Make one of each kind of access, before any file manages a scope.

    Parameters
    ----------
    client
        The client.

    Returns
    -------
    dict of str to str
        The ids the route paths name.
    """
    create_environment(client, "prod")
    create_table(client, "prod", "gold", "orders")
    _post(
        client,
        "/api/v1/repositories",
        {"name": "analytics", "kind": "https", "url": "https://github.com/o/a.git"},
    )
    for name, kind in (("alice", "USER"), ("bob", "USER"), ("deployer", "SERVICE_ACCOUNT")):
        _post(client, "/api/v1/principals", {"name": name, "type": kind})
    for group in ("team", "empty"):
        _post(client, "/api/v1/groups", {"name": group})
    client.put("/api/v1/groups/team/members/alice", headers=AUTH_HEADER)
    grant = _post(
        client,
        "/api/v1/grants",
        {"group": "team", "role": "VIEWER", "kind": "ENVIRONMENT", "environment": "prod"},
    )
    row_filter = _post(
        client, "/api/v1/row-filters", {**_TABLE, "principal": "alice", "expression": "id > 1"}
    )
    mask = _post(
        client,
        "/api/v1/column-masks",
        {**_TABLE, "principal": "alice", "column": "id", "expression": "NULL"},
    )
    rule = _post(client, _RULES, _RULE)
    off = _post(
        client, _RULES, {**_RULE, "conditions": {**_RULE["conditions"], "ref": "refs/heads/dev"}}
    )
    _post(client, f"{_RULES}/{off['id']}/disable", {})
    return {
        "grant": grant["id"],
        "row_filter": row_filter["id"],
        "column_mask": mask["id"],
        "rule": rule["id"],
        "rule_off": off["id"],
    }


def _manage(client: TestClient, scope: str) -> None:
    """Record a scope as managed, as an apply leaves it.

    Parameters
    ----------
    client
        The client, whose server's database is written.
    scope
        ``instance`` or an environment.
    """
    with Session(client.app.state.engine) as session:  # type: ignore[attr-defined]
        session.add(
            AccessManagedScope(
                scope=scope,
                file_sha256="0" * 64,
                file_name="access.yaml",
                apply_id=uuid.uuid4(),
                applied_at=datetime(2026, 11, 6, 9, 30, tzinfo=UTC),
            )
        )
        session.commit()


@pytest.mark.parametrize("name", sorted(_ROUTES))
def test_a_route_refuses_a_managed_scope_and_takes_the_override(
    managed_client: TestClient, name: str
) -> None:
    """Review focus 4: 409 with the message; 422 for half an override; 2xx and audited with it."""
    ids = _seed(managed_client)
    _manage(managed_client, "instance")
    method, path, body = _ROUTES[name]
    url = path.format(**ids)

    refused = managed_client.request(method, url, headers=AUTH_HEADER, json=body)
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"] == (
        "`instance` is managed by an access file (last applied 2026-11-06T09:30Z); change the "
        "file and run `lakana access apply`, or, in an emergency, repeat with "
        "`--override-managed --reason …`"
    )
    half = managed_client.request(
        method, url, headers=AUTH_HEADER, json=body, params={"override_managed": "true"}
    )
    assert half.status_code == 422
    assert "`--override-managed` needs `--reason`" in half.text

    allowed = managed_client.request(method, url, headers=AUTH_HEADER, json=body, params=_OVERRIDE)
    assert allowed.status_code in (200, 201, 204), allowed.text
    events = managed_client.get(
        "/api/v1/audit-events",
        headers=AUTH_HEADER,
        params={"event_type": "MANAGED_SCOPE_OVERRIDDEN"},
    )
    [event] = events.json()["items"]
    assert event["detail"]["scope"] == "instance"
    assert event["detail"]["reason"] == "Incident 42 hotfix"


def test_a_reason_without_the_flag_is_refused(managed_client: TestClient) -> None:
    """The other half: `override_reason` alone answers 422 and names the flag."""
    response = managed_client.post(
        "/api/v1/groups",
        headers=AUTH_HEADER,
        json={"name": "ops"},
        params={"override_reason": "Incident 42 hotfix"},
    )
    assert response.status_code == 422
    assert "`--reason` goes with `--override-managed`" in response.text


def test_outside_the_managed_scope_a_route_works_and_an_override_is_refused(
    managed_client: TestClient,
) -> None:
    """With only `prod` managed, a group is created; an override there is refused (R11)."""
    _manage(managed_client, "prod")
    created = managed_client.post("/api/v1/groups", headers=AUTH_HEADER, json={"name": "ops"})
    assert created.status_code == 201, created.text
    stale = managed_client.post(
        "/api/v1/groups", headers=AUTH_HEADER, json={"name": "ops2"}, params=_OVERRIDE
    )
    assert stale.status_code == 422
    assert "repeat without `--override-managed`" in stale.text
