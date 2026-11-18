"""Tests for the secrets resource of the platform API."""

import logging

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.models import AuditEvent, AuditEventType
from tests.conftest import AUTH_HEADER


def _create_dev_environment(client: TestClient, storage_root: str) -> None:
    """Create a bare `dev` environment for a test to attach secrets to.

    Parameters
    ----------
    client
        The test client.
    storage_root
        The environment's storage root URI.
    """
    client.post(
        "/api/v1/environments",
        headers=AUTH_HEADER,
        json={
            "name": "dev",
            "storage": {"scheme": "file", "root": storage_root},
        },
    )


def test_create_a_secret_and_list_it(client: TestClient, storage_root: str) -> None:
    """A created secret shows up when listing its environment's secrets, with no value field."""
    _create_dev_environment(client, storage_root)

    created = client.post(
        "/api/v1/environments/dev/secrets",
        headers=AUTH_HEADER,
        json={"name": "s3-creds", "values": {"access_key_id": "AKIA123"}},
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["name"] == "s3-creds"
    assert "values" not in body
    assert "ciphertext" not in body

    listed = client.get("/api/v1/environments/dev/secrets", headers=AUTH_HEADER)
    assert listed.status_code == 200
    assert [s["name"] for s in listed.json()] == ["s3-creds"]
    assert "values" not in listed.json()[0]
    assert listed.json()[0]["format_version"] == created.json()["format_version"]
    assert listed.json()[0]["key_id"].startswith("env:")


def test_create_raises_not_found_for_an_unknown_environment(client: TestClient) -> None:
    """Creating a secret in an environment that doesn't exist is a 404."""
    response = client.post(
        "/api/v1/environments/nope/secrets",
        headers=AUTH_HEADER,
        json={"name": "s3-creds", "values": {"k": "v"}},
    )
    assert response.status_code == 404


def test_rotate_a_secret(client: TestClient, storage_root: str) -> None:
    """PATCH re-encrypts a secret's value under the same name."""
    _create_dev_environment(client, storage_root)
    client.post(
        "/api/v1/environments/dev/secrets",
        headers=AUTH_HEADER,
        json={"name": "s3-creds", "values": {"k": "old"}},
    )

    rotated = client.patch(
        "/api/v1/environments/dev/secrets/s3-creds",
        headers=AUTH_HEADER,
        json={"values": {"k": "new"}},
    )
    assert rotated.status_code == 200, rotated.text
    assert "values" not in rotated.json()


def test_list_shows_when_a_secret_was_rotated(client: TestClient, storage_root: str) -> None:
    _create_dev_environment(client, storage_root)
    created = client.post(
        "/api/v1/environments/dev/secrets",
        headers=AUTH_HEADER,
        json={"name": "s3-creds", "values": {"k": "old"}},
    )
    assert created.status_code == 201, created.text
    assert created.json()["rotated_at"] is None
    rotated = client.patch(
        "/api/v1/environments/dev/secrets/s3-creds",
        headers=AUTH_HEADER,
        json={"values": {"k": "new"}},
    )
    assert rotated.json()["rotated_at"] is not None
    listed = client.get("/api/v1/environments/dev/secrets", headers=AUTH_HEADER).json()
    assert [(s["name"], s["rotated_at"]) for s in listed] == [
        ("s3-creds", rotated.json()["rotated_at"])
    ]
    assert listed[0]["created_at"] == created.json()["created_at"]


def test_rotate_raises_not_found_for_an_unknown_secret_name(
    client: TestClient, storage_root: str
) -> None:
    """Rotating a secret name that doesn't exist in that environment is a 404."""
    _create_dev_environment(client, storage_root)

    response = client.patch(
        "/api/v1/environments/dev/secrets/nope", headers=AUTH_HEADER, json={"values": {"k": "v"}}
    )
    assert response.status_code == 404


def test_revoke_a_secret(client: TestClient, storage_root: str) -> None:
    """DELETE revokes a secret; it no longer appears when listing."""
    _create_dev_environment(client, storage_root)
    client.post(
        "/api/v1/environments/dev/secrets",
        headers=AUTH_HEADER,
        json={"name": "s3-creds", "values": {"k": "v"}},
    )

    revoked = client.delete("/api/v1/environments/dev/secrets/s3-creds", headers=AUTH_HEADER)
    assert revoked.status_code == 204

    listed = client.get("/api/v1/environments/dev/secrets", headers=AUTH_HEADER)
    assert listed.json() == []


def test_revoke_raises_not_found_for_an_unknown_secret_name(
    client: TestClient, storage_root: str
) -> None:
    """Revoking a secret name that doesn't exist in that environment is a 404."""
    _create_dev_environment(client, storage_root)

    response = client.delete("/api/v1/environments/dev/secrets/nope", headers=AUTH_HEADER)
    assert response.status_code == 404


def test_rotate_raises_not_found_when_other_secrets_exist(
    client: TestClient, storage_root: str
) -> None:
    """Looking up an unknown name still 404s when it must skip past other secrets first."""
    _create_dev_environment(client, storage_root)
    client.post(
        "/api/v1/environments/dev/secrets",
        headers=AUTH_HEADER,
        json={"name": "a", "values": {"k": "v"}},
    )
    client.post(
        "/api/v1/environments/dev/secrets",
        headers=AUTH_HEADER,
        json={"name": "b", "values": {"k": "v"}},
    )

    response = client.patch(
        "/api/v1/environments/dev/secrets/nope", headers=AUTH_HEADER, json={"values": {"k": "v"}}
    )
    assert response.status_code == 404


_SENTINEL = "hunter2-sentinel-value"

_BAD_VALUES = [
    pytest.param({"k": 7, "other": _SENTINEL}, id="wrong-type-of-a-value"),
    pytest.param({_SENTINEL: 7}, id="secret-typed-into-a-key"),
    pytest.param({"k": [_SENTINEL]}, id="list-instead-of-string"),
    pytest.param({"k": {"inner": _SENTINEL}}, id="object-instead-of-string"),
    pytest.param([_SENTINEL], id="values-is-a-list"),
    pytest.param(_SENTINEL, id="values-is-a-string"),
]


def _assert_no_sentinel(response: Response) -> None:
    """Assert a 422 whose whole body carries none of the submitted secret text.

    Parameters
    ----------
    response
        The response to check.
    """
    assert response.status_code == 422, response.text
    assert _SENTINEL not in response.text


@pytest.mark.parametrize("values", _BAD_VALUES)
def test_a_rejected_create_does_not_echo_what_was_submitted(
    client: TestClient, storage_root: str, values: object
) -> None:
    """A create that fails validation answers 422 without any submitted value."""
    _create_dev_environment(client, storage_root)
    _assert_no_sentinel(
        client.post(
            "/api/v1/environments/dev/secrets",
            headers=AUTH_HEADER,
            json={"name": "s", "values": values},
        )
    )


@pytest.mark.parametrize("name", [[_SENTINEL], {_SENTINEL: 1}, None])
def test_a_rejected_secret_name_does_not_echo_the_values_beside_it(
    client: TestClient, storage_root: str, name: object
) -> None:
    """A create whose name is wrong answers 422 without the valid secret values sent with it."""
    _create_dev_environment(client, storage_root)
    _assert_no_sentinel(
        client.post(
            "/api/v1/environments/dev/secrets",
            headers=AUTH_HEADER,
            json={"name": name, "values": {"k": _SENTINEL}},
        )
    )


@pytest.mark.parametrize("values", _BAD_VALUES)
def test_a_rejected_rotate_does_not_echo_what_was_submitted(
    client: TestClient, storage_root: str, values: object
) -> None:
    """A rotate that fails validation answers 422 without any submitted value."""
    _create_dev_environment(client, storage_root)
    client.post(
        "/api/v1/environments/dev/secrets",
        headers=AUTH_HEADER,
        json={"name": "s", "values": {"k": "v"}},
    )
    _assert_no_sentinel(
        client.patch(
            "/api/v1/environments/dev/secrets/s", headers=AUTH_HEADER, json={"values": values}
        )
    )


def test_a_rejected_secret_request_writes_nothing_of_it_to_the_log(
    client: TestClient, storage_root: str, caplog: pytest.LogCaptureFixture
) -> None:
    """The application's own log carries none of a rejected secret body."""
    _create_dev_environment(client, storage_root)
    with caplog.at_level(logging.DEBUG):
        client.post(
            "/api/v1/environments/dev/secrets",
            headers=AUTH_HEADER,
            json={"name": "s", "values": {_SENTINEL: 7}},
        )
    assert _SENTINEL not in caplog.text


def test_a_malformed_json_body_does_not_echo_what_was_submitted(
    client: TestClient, storage_root: str
) -> None:
    """A body that is not JSON at all answers 422 without the text that was sent."""
    _create_dev_environment(client, storage_root)
    _assert_no_sentinel(
        client.post(
            "/api/v1/environments/dev/secrets",
            headers={**AUTH_HEADER, "content-type": "application/json"},
            content=f'{{"name": "s", "values": {{"k": {_SENTINEL}}}}}'.encode(),
        )
    )


_BAD_OPTIONS = [
    pytest.param({_SENTINEL: 1}, id="secret-typed-into-a-key"),
    pytest.param({"k": [_SENTINEL]}, id="list-instead-of-string"),
    pytest.param({"k": {"inner": _SENTINEL}}, id="object-instead-of-string"),
    pytest.param([_SENTINEL], id="options-is-a-list"),
    pytest.param(_SENTINEL, id="options-is-a-string"),
]


@pytest.mark.parametrize("options", _BAD_OPTIONS)
def test_a_rejected_environment_create_does_not_echo_the_storage_options(
    client: TestClient, storage_root: str, options: object
) -> None:
    """Storage options can carry inline S3 credentials, so a refusal names no key and no value."""
    response = client.post(
        "/api/v1/environments",
        headers=AUTH_HEADER,
        json={
            "name": "dev",
            "storage": {"scheme": "file", "root": storage_root, "options": options},
        },
    )
    _assert_no_sentinel(response)
    assert "options" in response.text


@pytest.mark.parametrize("options", _BAD_OPTIONS)
def test_a_rejected_storage_update_does_not_echo_the_options(
    client: TestClient, storage_root: str, options: object
) -> None:
    """A PATCH of the storage options answers 422 without any submitted key or value."""
    _create_dev_environment(client, storage_root)
    _assert_no_sentinel(
        client.patch(
            "/api/v1/environments/dev/storage", headers=AUTH_HEADER, json={"options": options}
        )
    )


def test_a_storage_update_without_options_is_still_accepted(
    client: TestClient, storage_root: str
) -> None:
    """`options` stays optional on a PATCH: leaving it out, or null, changes nothing."""
    _create_dev_environment(client, storage_root)
    for body in ({}, {"options": None}, {"options": {"endpoint": "http://s3.corp"}}):
        response = client.patch("/api/v1/environments/dev/storage", headers=AUTH_HEADER, json=body)
        assert response.status_code == 200, response.text


def test_a_secret_is_bound_through_the_api_and_shows_its_use(
    client: TestClient, storage_root: str
) -> None:
    """PUT .../use binds; every listing shows the use and the target's name."""
    _create_dev_environment(client, storage_root)
    created = client.post(
        "/api/v1/environments/dev/secrets",
        headers=AUTH_HEADER,
        json={"name": "pypi", "values": {"password": "pw"}},
    )
    assert created.status_code == 201
    assert (created.json()["use"], created.json()["use_target"]) == (None, None)
    assert created.json()["use_target_url"] is None
    client.post(
        "/api/v1/package-indexes",
        headers=AUTH_HEADER,
        json={"name": "corp", "url": "https://corp.example.org/simple"},
    )

    bound = client.put(
        "/api/v1/environments/dev/secrets/pypi/use",
        headers=AUTH_HEADER,
        json={"kind": "package_index", "target": "corp"},
    )

    assert bound.status_code == 200, bound.text
    assert (bound.json()["use"], bound.json()["use_target"]) == ("package_index", "corp")
    assert bound.json()["use_target_url"] == "https://corp.example.org/simple"
    listed = client.get("/api/v1/environments/dev/secrets", headers=AUTH_HEADER).json()
    assert [(s["name"], s["use"], s["use_target"]) for s in listed] == [
        ("pypi", "package_index", "corp")
    ]


def test_naming_an_unbound_secret_as_an_administrator_binds_it_and_the_listing_shows_it(
    client: TestClient, storage_root: str
) -> None:
    """The one-step flow: create the secret, name it on an index, read its use back."""
    _create_dev_environment(client, storage_root)
    client.post(
        "/api/v1/environments/dev/secrets",
        headers=AUTH_HEADER,
        json={"name": "pypi", "values": {"password": "pw"}},
    )

    added = client.post(
        "/api/v1/package-indexes",
        headers=AUTH_HEADER,
        json={"name": "corp", "url": "https://corp.example.org/simple"},
    )
    assert added.status_code == 201, added.text
    updated = client.patch(
        "/api/v1/package-indexes/corp",
        headers=AUTH_HEADER,
        json={"credential_secret": "pypi"},
    )
    assert updated.status_code == 200, updated.text

    listed = client.get("/api/v1/environments/dev/secrets", headers=AUTH_HEADER).json()
    assert [(s["use"], s["use_target"]) for s in listed] == [("package_index", "corp")]


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({"kind": "package_index", "target": "nope"}, 404),
        ({"kind": "environment", "target": "corp"}, 422),
        ({"kind": "elsewhere", "target": None}, 422),
        ({"kind": "repository"}, 422),
    ],
)
def test_a_bind_to_something_that_is_not_there_is_refused(
    client: TestClient, storage_root: str, body: dict[str, object], status: int
) -> None:
    """An unknown target is 404; a malformed use is 422.

    Parameters
    ----------
    client
        The administrator's client.
    storage_root
        The environment's storage root.
    body
        The request body.
    status
        The expected status.
    """
    _create_dev_environment(client, storage_root)
    client.post(
        "/api/v1/environments/dev/secrets",
        headers=AUTH_HEADER,
        json={"name": "s", "values": {"k": "v"}},
    )

    response = client.put("/api/v1/environments/dev/secrets/s/use", headers=AUTH_HEADER, json=body)

    assert response.status_code == status


def _nobody(client: TestClient) -> dict[str, str]:
    """Create a service account that holds no grant and return its auth header.

    Parameters
    ----------
    client
        The administrator's client.

    Returns
    -------
    dict of str to str
        An Authorization header.
    """
    client.post(
        "/api/v1/principals",
        headers=AUTH_HEADER,
        json={"name": "nobody", "type": "SERVICE_ACCOUNT"},
    )
    issued = client.post(
        "/api/v1/principals/nobody/tokens", headers=AUTH_HEADER, json={"name": "nobody"}
    )
    return {"Authorization": f"Bearer {issued.json()['token']}"}


def _events(client: TestClient, kind: AuditEventType) -> list[AuditEvent]:
    """Read the audit events of one type.

    Parameters
    ----------
    client
        The test client, whose app holds the database.
    kind
        The event type.

    Returns
    -------
    list of AuditEvent
        The events, oldest first.
    """
    with Session(client.app.state.engine) as session:
        return list(
            session.execute(
                select(AuditEvent)
                .where(AuditEvent.event_type == kind)
                .order_by(AuditEvent.occurred_at)
            ).scalars()
        )


def test_a_caller_without_the_right_gets_403_before_the_target_is_looked_at(
    client: TestClient, storage_root: str
) -> None:
    """A bad or a good target answers 403 alike, and the denial is audited."""
    _create_dev_environment(client, storage_root)
    client.post(
        "/api/v1/environments/dev/secrets",
        headers=AUTH_HEADER,
        json={"name": "s", "values": {"k": "v"}},
    )
    client.post(
        "/api/v1/package-indexes",
        headers=AUTH_HEADER,
        json={"name": "corp", "url": "https://corp.example.org/simple"},
    )
    nobody = _nobody(client)

    statuses = [
        client.put(
            "/api/v1/environments/dev/secrets/s/use",
            headers=nobody,
            json={"kind": "package_index", "target": target},
        ).status_code
        for target in ("nope", "corp")
    ]
    created = client.post(
        "/api/v1/environments/dev/secrets",
        headers=nobody,
        json={
            "name": "t",
            "values": {"k": "v"},
            "use": {"kind": "package_index", "target": "nope"},
        },
    )

    assert statuses == [403, 403]
    assert created.status_code == 403
    assert _events(client, AuditEventType.AUTHORIZATION_DENIED)
    unbound = client.get("/api/v1/environments/dev/secrets", headers=AUTH_HEADER).json()
    assert [(s["name"], s["use"]) for s in unbound] == [("s", None)]


def test_a_successful_bind_is_audited(client: TestClient, storage_root: str) -> None:
    """A bind writes a SECRET_BOUND event."""
    _create_dev_environment(client, storage_root)
    client.post(
        "/api/v1/environments/dev/secrets",
        headers=AUTH_HEADER,
        json={"name": "s", "values": {"k": "v"}},
    )
    client.post(
        "/api/v1/package-indexes",
        headers=AUTH_HEADER,
        json={"name": "corp", "url": "https://corp.example.org/simple"},
    )

    client.put(
        "/api/v1/environments/dev/secrets/s/use",
        headers=AUTH_HEADER,
        json={"kind": "package_index", "target": "corp"},
    )

    assert len(_events(client, AuditEventType.SECRET_BOUND)) == 1


def test_a_secret_is_created_with_its_use(client: TestClient, storage_root: str) -> None:
    """POST with a use creates the secret bound, and every listing shows it."""
    _create_dev_environment(client, storage_root)
    client.post(
        "/api/v1/package-indexes",
        headers=AUTH_HEADER,
        json={"name": "corp", "url": "https://corp.example.org/simple"},
    )

    created = client.post(
        "/api/v1/environments/dev/secrets",
        headers=AUTH_HEADER,
        json={
            "name": "pypi",
            "values": {"password": "pw"},
            "use": {"kind": "package_index", "target": "corp"},
        },
    )

    assert created.status_code == 201, created.text
    assert (created.json()["use"], created.json()["use_target"]) == ("package_index", "corp")
    assert created.json()["use_target_url"] == "https://corp.example.org/simple"
    listed = client.get("/api/v1/environments/dev/secrets", headers=AUTH_HEADER).json()
    assert [(s["use"], s["use_target"]) for s in listed] == [("package_index", "corp")]
    assert len(_events(client, AuditEventType.SECRET_BOUND)) == 1


@pytest.mark.parametrize(
    ("use", "status"),
    [
        ({"kind": "package_index", "target": "nope"}, 404),
        ({"kind": "environment", "target": "corp"}, 422),
        ({"kind": "elsewhere"}, 422),
        ({"kind": "repository"}, 422),
    ],
)
def test_a_create_with_a_use_that_cannot_be_bound_creates_nothing(
    client: TestClient, storage_root: str, use: dict[str, object], status: int
) -> None:
    """An unknown target is 404, a malformed use 422, and no secret is left behind.

    Parameters
    ----------
    client
        The administrator's client.
    storage_root
        The environment's storage root.
    use
        The use in the request body.
    status
        The expected status.
    """
    _create_dev_environment(client, storage_root)

    response = client.post(
        "/api/v1/environments/dev/secrets",
        headers=AUTH_HEADER,
        json={"name": "s", "values": {"k": "v"}, "use": use},
    )

    assert response.status_code == status
    assert client.get("/api/v1/environments/dev/secrets", headers=AUTH_HEADER).json() == []
