"""`--override-managed --reason` on the 17 commands that change access (#46)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from conftest import FakeApi, invoke, make_state

_REASON = "Incident 42 hotfix"
_RULE_ID = "00000000-0000-0000-0000-000000000001"
_RULE = {
    "id": _RULE_ID,
    "issuer": "https://token.actions.githubusercontent.com",
    "principal": "deployer",
    "environment": "prod",
    "repository": "analytics",
    "kinds": ["deploy"],
    "conditions": {"repository_id": "123456", "ref": "refs/heads/main"},
    "enabled": True,
    "created_by": "admin",
    "created_at": "2026-10-20T12:00:00+00:00",
    "updated_by": None,
    "updated_at": "2026-10-20T12:00:00+00:00",
}
_POLICY = {
    "id": "r1",
    "principal_id": "p1",
    "group_id": None,
    "environment": "prod",
    "namespace": "gold",
    "table": "orders",
    "expression": "id > 1",
    "created_by": "a1",
    "created_at": "2026-01-01T00:00:00+00:00",
}
_GRANT = {
    "id": "g1",
    "principal_id": "p1",
    "group_id": None,
    "role": "VIEWER",
    "action": None,
    "kind": "ENVIRONMENT",
    "environment": "prod",
    "namespace": None,
    "table": None,
    "granted_by": "a1",
    "granted_at": "2026-01-01T00:00:00+00:00",
}
_RULES = "/api/v1/ci-trust-rules"

# name: (command line, method, path, status, answer)
_COMMANDS: dict[str, tuple[list[str], str, str, int, Any]] = {
    "grant create": (
        [
            "grant",
            "create",
            "--principal",
            "bob",
            "--role",
            "viewer",
            "--kind",
            "environment",
            "--env",
            "prod",
        ],
        "POST",
        "/api/v1/grants",
        201,
        _GRANT,
    ),
    "grant revoke": (["grant", "revoke", "g1", "--yes"], "DELETE", "/api/v1/grants/g1", 204, None),
    "group create": (["group", "create", "ops"], "POST", "/api/v1/groups", 201, {"name": "ops"}),
    "group delete": (
        ["group", "delete", "ops", "--yes"],
        "DELETE",
        "/api/v1/groups/ops",
        204,
        None,
    ),
    "group add-member": (
        ["group", "add-member", "ops", "bob"],
        "PUT",
        "/api/v1/groups/ops/members/bob",
        204,
        None,
    ),
    "group remove-member": (
        ["group", "remove-member", "ops", "bob", "--yes"],
        "DELETE",
        "/api/v1/groups/ops/members/bob",
        204,
        None,
    ),
    "principal create": (
        ["principal", "create", "bot", "--type", "service-account"],
        "POST",
        "/api/v1/principals",
        201,
        {"name": "bot", "type": "SERVICE_ACCOUNT", "is_active": True},
    ),
    "principal deactivate": (
        ["principal", "deactivate", "bot", "--yes"],
        "DELETE",
        "/api/v1/principals/bot",
        204,
        None,
    ),
    "row-filter create": (
        [
            "row-filter",
            "create",
            "gold.orders",
            "--principal",
            "bob",
            "--expression",
            "id > 1",
            "--env",
            "prod",
        ],
        "POST",
        "/api/v1/row-filters",
        201,
        _POLICY,
    ),
    "row-filter revoke": (
        ["row-filter", "revoke", "r1", "--yes"],
        "DELETE",
        "/api/v1/row-filters/r1",
        204,
        None,
    ),
    "column-mask create": (
        [
            "column-mask",
            "create",
            "gold.orders",
            "id",
            "--principal",
            "bob",
            "--expression",
            "NULL",
            "--env",
            "prod",
        ],
        "POST",
        "/api/v1/column-masks",
        201,
        {**_POLICY, "column": "id", "expression": "NULL"},
    ),
    "column-mask revoke": (
        ["column-mask", "revoke", "m1", "--yes"],
        "DELETE",
        "/api/v1/column-masks/m1",
        204,
        None,
    ),
    "ci-trust create": (
        [
            "ci-trust",
            "create",
            "--issuer",
            "github",
            "--env",
            "prod",
            "--repo",
            "analytics",
            "--principal",
            "deployer",
            "--claim",
            "repository_id=123456",
            "--claim",
            "ref=refs/heads/main",
        ],
        "POST",
        _RULES,
        201,
        _RULE,
    ),
    "ci-trust update": (
        ["ci-trust", "update", _RULE_ID, "--kind", "dry_run"],
        "PATCH",
        f"{_RULES}/{_RULE_ID}",
        200,
        _RULE,
    ),
    "ci-trust enable": (
        ["ci-trust", "enable", _RULE_ID],
        "POST",
        f"{_RULES}/{_RULE_ID}/enable",
        200,
        _RULE,
    ),
    "ci-trust disable": (
        ["ci-trust", "disable", _RULE_ID],
        "POST",
        f"{_RULES}/{_RULE_ID}/disable",
        200,
        _RULE,
    ),
    "ci-trust delete": (
        ["ci-trust", "delete", _RULE_ID, "--yes"],
        "DELETE",
        f"{_RULES}/{_RULE_ID}",
        204,
        None,
    ),
}


@pytest.mark.parametrize("name", sorted(_COMMANDS))
def test_the_override_sends_both_parameters_and_warns(tmp_path: Path, name: str) -> None:
    """Each command sends `override_managed=true` and the reason, and warns on stderr."""
    args, method, path, status, answer = _COMMANDS[name]
    api = FakeApi()
    api.on(method, path, status=status, json=answer)
    result = invoke(make_state(tmp_path, api), *args, "--override-managed", "--reason", _REASON)
    assert result.exit_code == 0, result.output
    [request] = [r for r in api.requests if (r.method, r.url.path) == (method, path)]
    assert dict(request.url.params) == {"override_managed": "true", "override_reason": _REASON}
    assert (
        "Overriding a scope an access file manages; the next apply reverts this unless the file "
        "lists it." in " ".join(result.stderr.split())
    )


@pytest.mark.parametrize("name", sorted(_COMMANDS))
def test_the_flag_alone_is_refused_before_anything_is_sent(tmp_path: Path, name: str) -> None:
    """`--override-managed` without `--reason` names `--reason`; nothing reaches the server."""
    args, *_ = _COMMANDS[name]
    api = FakeApi()
    result = invoke(make_state(tmp_path, api), *args, "--override-managed")
    assert result.exit_code == 2
    assert "--reason" in result.output
    assert api.requests == []


def test_without_the_flag_nothing_is_added(tmp_path: Path) -> None:
    """A plain command sends no override parameter; `--reason` alone is refused."""
    api = FakeApi()
    api.on("POST", "/api/v1/groups", status=201, json={"name": "ops"})
    state = make_state(tmp_path, api)
    assert invoke(state, "group", "create", "ops").exit_code == 0
    assert dict(api.requests[0].url.params) == {}
    refused = invoke(state, "group", "create", "ops", "--reason", _REASON)
    assert refused.exit_code == 2
    assert "--override-managed" in refused.output
