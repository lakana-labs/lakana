"""The CI trust rules resource of the platform API."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from lakana_core.ci_oidc.config import GITHUB_ISSUER, CiOidcConfig
from lakana_server.app import create_app
from lakana_server.settings import Settings
from tests.api.platform.test_package_indexes import _token_for
from tests.conftest import AUTH_HEADER
from tests.server_test_helpers import create_environment

_RULES = "/api/v1/ci-trust-rules"
_RULE = {
    "issuer": "github",
    "principal": "deployer",
    "environment": "prod",
    "repository": "analytics",
    "conditions": {"repository_id": "123456", "ref": "refs/heads/main", "event_name": "push"},
}


@pytest.fixture
def rules_client(settings: Settings) -> Iterator[TestClient]:
    """Provide a client of a server that accepts GitHub's ID tokens, with the targets of a rule.

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
        create_environment(client, "prod")
        client.post(
            "/api/v1/repositories",
            headers=AUTH_HEADER,
            json={"name": "analytics", "kind": "https", "url": "https://github.com/o/a.git"},
        )
        client.post(
            "/api/v1/principals",
            headers=AUTH_HEADER,
            json={"name": "deployer", "type": "SERVICE_ACCOUNT"},
        )
        yield client


def test_create_list_show_update_enable_disable_delete(rules_client: TestClient) -> None:
    created = rules_client.post(_RULES, headers=AUTH_HEADER, json=_RULE)
    assert created.status_code == 201, created.text
    body = created.json()
    assert (body["issuer"], body["kinds"], body["enabled"], body["created_by"]) == (
        GITHUB_ISSUER,
        ["deploy"],
        True,
        "admin",
    )
    rule = f"{_RULES}/{body['id']}"
    assert [r["id"] for r in rules_client.get(_RULES, headers=AUTH_HEADER).json()] == [body["id"]]
    assert rules_client.get(rule, headers=AUTH_HEADER).json()["principal"] == "deployer"
    patched = rules_client.patch(rule, headers=AUTH_HEADER, json={"kinds": ["deploy", "dry_run"]})
    assert patched.status_code == 200, patched.text
    assert patched.json()["kinds"] == ["deploy", "dry_run"]
    assert patched.json()["updated_by"] == "admin"
    assert rules_client.post(f"{rule}/disable", headers=AUTH_HEADER).json()["enabled"] is False
    assert rules_client.post(f"{rule}/enable", headers=AUTH_HEADER).json()["enabled"] is True
    assert rules_client.delete(rule, headers=AUTH_HEADER).status_code == 204
    assert rules_client.get(rule, headers=AUTH_HEADER).status_code == 404


def test_start_on_deploy_jobs_is_off_by_default_and_set_by_an_administrator(
    rules_client: TestClient,
) -> None:
    created = rules_client.post(_RULES, headers=AUTH_HEADER, json=_RULE).json()
    assert created["start_on_deploy_jobs"] is False
    rule = f"{_RULES}/{created['id']}"
    other = _token_for(rules_client, "someone")
    refused = rules_client.patch(rule, headers=other, json={"start_on_deploy_jobs": True})
    assert refused.status_code == 403
    patched = rules_client.patch(rule, headers=AUTH_HEADER, json={"start_on_deploy_jobs": True})
    assert patched.status_code == 200, patched.text
    assert patched.json()["start_on_deploy_jobs"] is True
    opted = rules_client.post(
        _RULES, headers=AUTH_HEADER, json={**_RULE, "start_on_deploy_jobs": True}
    )
    assert opted.json()["start_on_deploy_jobs"] is True


def test_a_rule_without_the_numeric_id_or_a_ref_is_a_422(rules_client: TestClient) -> None:
    for conditions, needle in (
        ({"ref": "refs/heads/main", "event_name": "push"}, "repository_id"),
        ({"repository_id": "1"}, "ref or environment"),
        ({"repository_id": "1", "ref": "refs/heads/main"}, "--claim event_name=push"),
        ({"repository_id": 1, "ref": "refs/heads/main", "event_name": "push"}, "string"),
    ):
        response = rules_client.post(
            _RULES, headers=AUTH_HEADER, json={**_RULE, "conditions": conditions}
        )
        assert response.status_code == 422, response.text
        assert needle in response.text
    rollback = rules_client.post(_RULES, headers=AUTH_HEADER, json={**_RULE, "kinds": ["rollback"]})
    assert rollback.status_code == 422
    assert "rollback" in rollback.json()["detail"]


def test_only_an_instance_administrator_may_touch_rules(rules_client: TestClient) -> None:
    created = rules_client.post(_RULES, headers=AUTH_HEADER, json=_RULE).json()
    header = _token_for(rules_client, "someone")
    rule = f"{_RULES}/{created['id']}"
    for method, path, body in (
        ("POST", _RULES, _RULE),
        ("GET", _RULES, None),
        ("GET", rule, None),
        ("GET", f"{_RULES}/{uuid.uuid4()}", None),
        ("PATCH", rule, {"kinds": ["dry_run"]}),
        ("POST", f"{rule}/enable", None),
        ("POST", f"{rule}/disable", None),
        ("DELETE", rule, None),
        ("POST", f"{_RULES}/explain", {"claims": {}, "environment": "p", "repository": "r"}),
    ):
        response = rules_client.request(method, path, headers=header, json=body)
        assert response.status_code == 403, (method, path, response.text)
    assert rules_client.get(rule, headers=AUTH_HEADER).json()["enabled"] is True


def test_explain_answers_without_a_token(rules_client: TestClient) -> None:
    created = rules_client.post(_RULES, headers=AUTH_HEADER, json=_RULE).json()
    claims = {
        "iss": GITHUB_ISSUER,
        "repository_id": "123456",
        "ref": "refs/heads/feature",
        "event_name": "push",
        "sha": "0" * 40,
    }
    report = rules_client.post(
        f"{_RULES}/explain",
        headers=AUTH_HEADER,
        json={"claims": claims, "environment": "prod", "repository": "analytics"},
    )
    assert report.status_code == 200, report.text
    body = report.json()
    assert body["matching"] == []
    assert body["rules"] == [
        {
            "rule_id": created["id"],
            "matches": False,
            "reasons": ["claim ref is 'refs/heads/feature'; the rule needs 'refs/heads/main'"],
        }
    ]
    claims["ref"] = "refs/heads/main"
    accepted = rules_client.post(
        f"{_RULES}/explain",
        headers=AUTH_HEADER,
        json={"claims": claims, "environment": "prod", "repository": "analytics"},
    ).json()
    assert accepted["verdict"] == f"accepted by rule {created['id']}"


def test_a_rule_is_refused_while_ci_oidc_is_off(client: TestClient) -> None:
    response = client.post(_RULES, headers=AUTH_HEADER, json=_RULE)
    assert response.status_code == 422
    assert "LAKANA_CI_OIDC__ISSUERS" in response.json()["detail"]
