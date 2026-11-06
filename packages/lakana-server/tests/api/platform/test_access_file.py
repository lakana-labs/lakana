"""Export and plan an access file over REST (#46)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import AUTH_HEADER
from tests.server_test_helpers import create_environment

_APPLY_BODY = {"text": "", "plan_fingerprint": "0" * 64}


def _user_header(client: TestClient, name: str) -> dict[str, str]:
    """Create a user with a token and return its auth header.

    Parameters
    ----------
    client
        The test client.
    name
        The user's name.

    Returns
    -------
    dict of str to str
        An Authorization header.
    """
    created = client.post(
        "/api/v1/principals", headers=AUTH_HEADER, json={"name": name, "type": "USER"}
    )
    assert created.status_code == 201, created.text
    issued = client.post(
        f"/api/v1/principals/{name}/tokens", headers=AUTH_HEADER, json={"name": name}
    )
    return {"Authorization": f"Bearer {issued.json()['token']}"}


def test_export_then_plan_is_empty_over_rest(client: TestClient) -> None:
    """The bootstrap admin's export plans to nothing."""
    exported = client.get("/api/v1/access/export", headers=AUTH_HEADER)
    assert exported.status_code == 200, exported.text
    plan = client.post(
        "/api/v1/access/plan", headers=AUTH_HEADER, json={"text": exported.json()["text"]}
    )
    assert plan.status_code == 200, plan.text
    body = plan.json()
    assert (body["creates"], body["changed"], body["removals"], body["problems"]) == (
        [],
        [],
        [],
        [],
    )
    assert body["file_name"] == "access.yaml"
    assert body["manages"] == ["instance"]
    assert len(body["fingerprint"]) == 64
    assert len(body["file_sha256"]) == 64


def test_plan_names_a_creation_with_its_entry_path(client: TestClient) -> None:
    """A new group in the file is a create with the path of its entry."""
    text = client.get("/api/v1/access/export", headers=AUTH_HEADER).json()["text"]
    text += "groups:\n  finance:\n    members: []\n"
    body = client.post("/api/v1/access/plan", headers=AUTH_HEADER, json={"text": text}).json()
    assert body["problems"] == []
    assert [c["op"] for c in body["creates"]] == ["create"]
    assert body["creates"][0]["entry"] == "groups.finance"
    assert "finance" in body["creates"][0]["text"]


def test_export_names_the_scopes_it_is_given(client: TestClient) -> None:
    """Scopes repeat in the query; an unknown environment is a 422 naming the next step."""
    refused = client.get("/api/v1/access/export?scope=nowhere", headers=AUTH_HEADER)
    assert refused.status_code == 422
    assert "lakana env list" in refused.text


def test_plan_returns_every_problem_with_200(client: TestClient) -> None:
    """A broken file is a plan with problems, not an error: CI shows all of them."""
    response = client.post(
        "/api/v1/access/plan",
        headers=AUTH_HEADER,
        json={"file_name": "team.yaml", "text": "version: 1\nmanages: [instance]\nx: 1\ny: 2\n"},
    )
    assert response.status_code == 200
    assert [p[:15] for p in response.json()["problems"]] == ["team.yaml: x (l", "team.yaml: y (l"]


def test_plan_of_a_file_over_the_body_limit_names_the_limit(client: TestClient) -> None:
    """R8: the request body limit refuses it with 413 and names the limit; no field cap."""
    response = client.post(
        "/api/v1/access/plan", headers=AUTH_HEADER, json={"text": "#" * (1024 * 1024 + 1)}
    )
    assert response.status_code == 413
    assert "1048576 bytes" in response.text


def test_access_routes_refuse_a_non_admin(client: TestClient) -> None:
    """403 for export and plan without the ADMIN role on the instance."""
    header = _user_header(client, "alice")
    assert client.get("/api/v1/access/export", headers=header).status_code == 403
    assert client.post("/api/v1/access/plan", headers=header, json={"text": ""}).status_code == 403
    assert client.post("/api/v1/access/apply", headers=header, json=_APPLY_BODY).status_code == 403
    assert client.get("/api/v1/access/status", headers=header).status_code == 403


def test_a_file_name_with_a_path_is_refused(client: TestClient) -> None:
    """422 naming file_name."""
    response = client.post(
        "/api/v1/access/plan", headers=AUTH_HEADER, json={"file_name": "../a.yaml", "text": ""}
    )
    assert response.status_code == 422
    assert "file_name" in response.text


def test_a_task_token_is_refused_on_the_access_routes(client: TestClient) -> None:
    """R10: a task token, even the administrator's own, gets 403 on export and plan."""
    create_environment(client)
    minted = client.post(
        "/api/v1/task-tokens",
        headers=AUTH_HEADER,
        json={
            "principal": "admin",
            "environment": "dev",
            "asset_scope": ["gold.a"],
            "ttl_seconds": 300,
        },
    )
    assert minted.status_code == 201, minted.text
    header = {"Authorization": f"Bearer {minted.json()['token']}"}
    assert client.get("/api/v1/access/export", headers=header).status_code == 403
    assert client.post("/api/v1/access/plan", headers=header, json={"text": ""}).status_code == 403
    assert client.post("/api/v1/access/apply", headers=header, json=_APPLY_BODY).status_code == 403
    assert client.get("/api/v1/access/status", headers=header).status_code == 403


def _plan(client: TestClient, text: str) -> dict[str, object]:
    """Plan a file as the bootstrap administrator.

    Parameters
    ----------
    client
        The test client.
    text
        The file's content.

    Returns
    -------
    dict of str to object
        The plan body.
    """
    response = client.post("/api/v1/access/plan", headers=AUTH_HEADER, json={"text": text})
    assert response.status_code == 200, response.text
    return dict(response.json())


def test_apply_makes_the_planned_changes_and_the_next_plan_is_empty(client: TestClient) -> None:
    """Export, add a group, plan, apply with the fingerprint, plan again: nothing left."""
    text = client.get("/api/v1/access/export", headers=AUTH_HEADER).json()["text"]
    text += "groups:\n  finance:\n    members: []\n"
    plan = _plan(client, text)
    applied = client.post(
        "/api/v1/access/apply",
        headers=AUTH_HEADER,
        json={
            "text": text,
            "plan_fingerprint": plan["fingerprint"],
            "source_commit": "0123abc",
        },
    )
    assert applied.status_code == 200, applied.text
    body = applied.json()
    assert (body["created"], body["changed"], body["removed"], body["released"]) == (1, 0, 0, [])
    assert body["source_commit"] == "0123abc"
    assert len(body["apply_id"]) == 36
    assert len(body["file_sha256"]) == 64
    assert body["applied_at"]
    again = _plan(client, text)
    assert (again["creates"], again["changed"], again["removals"]) == ([], [], [])


def test_apply_with_a_stale_fingerprint_is_409_and_changes_nothing(client: TestClient) -> None:
    """The text is not applied; the answer says to plan again."""
    text = client.get("/api/v1/access/export", headers=AUTH_HEADER).json()["text"]
    text += "groups:\n  finance:\n    members: []\n"
    response = client.post(
        "/api/v1/access/apply",
        headers=AUTH_HEADER,
        json={"text": text, "plan_fingerprint": "0" * 64},
    )
    assert response.status_code == 409
    assert "plan again" in response.text
    assert _plan(client, text)["creates"]


def test_apply_lists_every_problem_with_422(client: TestClient) -> None:
    """A file with problems is not applied; each problem is in the answer."""
    text = "version: 1\nmanages: [instance]\nx: 1\ny: 2\n"
    fingerprint = _plan(client, text)["fingerprint"]
    response = client.post(
        "/api/v1/access/apply",
        headers=AUTH_HEADER,
        json={"text": text, "plan_fingerprint": fingerprint},
    )
    assert response.status_code == 422, response.text
    assert "access.yaml: x" in response.text
    assert "access.yaml: y" in response.text


def test_apply_rejects_a_fingerprint_that_is_not_64_hex_digits(client: TestClient) -> None:
    """422 naming plan_fingerprint."""
    response = client.post(
        "/api/v1/access/apply", headers=AUTH_HEADER, json={"text": "", "plan_fingerprint": "abc"}
    )
    assert response.status_code == 422
    assert "plan_fingerprint" in response.text


def test_status_has_the_shape_the_sdk_reads(client: TestClient) -> None:
    """Scopes after an apply, no overrides yet."""
    assert client.get("/api/v1/access/status", headers=AUTH_HEADER).json() == {
        "scopes": [],
        "overrides": [],
    }
    text = client.get("/api/v1/access/export", headers=AUTH_HEADER).json()["text"]
    fingerprint = _plan(client, text)["fingerprint"]
    applied = client.post(
        "/api/v1/access/apply",
        headers=AUTH_HEADER,
        json={"text": text, "plan_fingerprint": fingerprint, "source_commit": "0123abc"},
    )
    assert applied.status_code == 200, applied.text
    scope = client.get("/api/v1/access/status", headers=AUTH_HEADER).json()["scopes"][0]
    assert scope["scope"] == "instance"
    assert scope["file_name"] == "access.yaml"
    assert scope["source_commit"] == "0123abc"
    assert scope["applied_by"] == "admin"
    assert set(scope) == {
        "scope",
        "file_name",
        "file_sha256",
        "source_commit",
        "applied_by",
        "applied_at",
    }
