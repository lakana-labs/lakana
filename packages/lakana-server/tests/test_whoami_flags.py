"""Tests for the access flags of `GET /api/v1/whoami`.

`may_deploy`, `secret_environments` and `manage_policy`.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.api.platform.test_whoami import _new_person, _task_token_header
from tests.conftest import AUTH_HEADER
from tests.server_test_helpers import create_environment


def _grant(client: TestClient, body: dict[str, str]) -> None:
    """Create a grant for kim in `dev`.

    Parameters
    ----------
    client
        The test client.
    body
        The role or action of the grant.
    """
    granted = client.post(
        "/api/v1/grants",
        headers=AUTH_HEADER,
        json={"principal": "kim", "kind": "ENVIRONMENT", "environment": "dev", **body},
    )
    assert granted.status_code == 201, granted.text


def test_whoami_says_may_deploy_for_a_deploy_grant(client: TestClient) -> None:
    create_environment(client, "dev")
    header = _new_person(client, "kim")
    assert client.get("/api/v1/whoami", headers=header).json()["may_deploy"] is False
    _grant(client, {"role": "DATA_ENGINEER"})
    assert client.get("/api/v1/whoami", headers=header).json()["may_deploy"] is False
    _grant(client, {"action": "DEPLOY"})
    body = client.get("/api/v1/whoami", headers=header).json()
    assert (body["admin"], body["has_access"], body["may_deploy"]) == (False, True, True)


def test_an_administrator_may_deploy_once_an_environment_exists(client: TestClient) -> None:
    assert client.get("/api/v1/whoami", headers=AUTH_HEADER).json()["may_deploy"] is False
    create_environment(client, "dev")
    assert client.get("/api/v1/whoami", headers=AUTH_HEADER).json()["may_deploy"] is True


def _secret_environments(client: TestClient, header: dict[str, str]) -> list[str]:
    """Read `secret_environments` of whoami.

    Parameters
    ----------
    client
        The test client.
    header
        The caller's Authorization header.

    Returns
    -------
    list of str
        The environments where the caller manages secrets.
    """
    return client.get("/api/v1/whoami", headers=header).json()["secret_environments"]


def test_whoami_names_the_environments_where_one_manages_secrets(client: TestClient) -> None:
    create_environment(client, "dev")
    create_environment(client, "prod")
    header = _new_person(client, "kim")
    assert _secret_environments(client, header) == []
    _grant(client, {"action": "MANAGE_SECRET"})
    granted = client.post(
        "/api/v1/grants",
        headers=AUTH_HEADER,
        json={
            "principal": "kim",
            "kind": "ENVIRONMENT",
            "environment": "prod",
            "role": "DATA_ENGINEER",
        },
    )
    assert granted.status_code == 201, granted.text
    assert _secret_environments(client, header) == ["dev"]
    assert _secret_environments(client, AUTH_HEADER) == ["dev", "prod"]
    deleted = client.delete("/api/v1/environments/dev", headers=AUTH_HEADER)
    assert deleted.status_code in {200, 204}, deleted.text
    assert _secret_environments(client, AUTH_HEADER) == ["prod"]


def test_whoami_records_no_audit_event_for_the_flags(client: TestClient) -> None:
    create_environment(client, "dev")
    before = client.get("/api/v1/audit-events", headers=AUTH_HEADER).json()["items"]
    assert _secret_environments(client, AUTH_HEADER) == ["dev"]
    after = client.get("/api/v1/audit-events", headers=AUTH_HEADER).json()["items"]
    assert after == before


def _manage_policy(client: TestClient, header: dict[str, str]) -> bool:
    """Read `manage_policy` of whoami.

    Parameters
    ----------
    client
        The test client.
    header
        The caller's Authorization header.

    Returns
    -------
    bool
        Whether the caller manages row filters and column masks.
    """
    return client.get("/api/v1/whoami", headers=header).json()["manage_policy"]


def test_whoami_says_who_manages_policies(client: TestClient) -> None:
    """MANAGE_POLICY or ADMIN on the instance says yes; a role without it or dev alone says no."""
    create_environment(client, "dev")
    grants = {
        "pat": {"action": "MANAGE_POLICY", "kind": "INSTANCE"},
        "lee": {"role": "DATA_ENGINEER", "kind": "INSTANCE"},
        "max": {"action": "MANAGE_POLICY", "kind": "ENVIRONMENT", "environment": "dev"},
    }
    headers = {}
    for name, grant in grants.items():
        headers[name] = _new_person(client, name)
        assert _manage_policy(client, headers[name]) is False
        granted = client.post(
            "/api/v1/grants", headers=AUTH_HEADER, json={"principal": name, **grant}
        )
        assert granted.status_code == 201, granted.text

    assert _manage_policy(client, headers["pat"]) is True
    assert _manage_policy(client, AUTH_HEADER) is True
    assert _manage_policy(client, headers["lee"]) is False
    assert _manage_policy(client, headers["max"]) is False


def test_a_task_token_manages_no_policy(client: TestClient) -> None:
    """An administrator's task token manages no policy; its personal token does."""
    create_environment(client, "dev")

    assert _manage_policy(client, _task_token_header(client)) is False
    assert _manage_policy(client, AUTH_HEADER) is True


def test_the_policy_flag_records_no_audit_event(client: TestClient) -> None:
    """Asking whoami about policies writes nothing, for a yes and for a no.

    Reading the audit log records that it was read (`ACTIVITY_VIEWED`), so those are left out.
    """
    header = _new_person(client, "kim")
    assert _manage_policy(client, header) is False  # kim's first sign-in is recorded here

    def events() -> list[dict[str, object]]:
        items = client.get("/api/v1/audit-events", headers=AUTH_HEADER).json()["items"]
        return [item for item in items if item["event_type"] != "ACTIVITY_VIEWED"]

    before = events()
    assert _manage_policy(client, AUTH_HEADER) is True
    assert _manage_policy(client, header) is False
    assert events() == before
