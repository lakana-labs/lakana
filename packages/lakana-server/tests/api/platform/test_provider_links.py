"""The provider window of linked people's tokens, and where it is shown (#32, PR 2).

`whoami`, the session, a person and the sign-in settings describe a person's link to the identity
provider and when their tokens' window ends; an administrator changes the window with the
session lengths.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

import lakana_server.ui
from lakana_core.models import AuditEvent, AuditEventType, Principal
from lakana_server.app import create_app
from lakana_server.settings import Settings, UiConfig
from tests.conftest import AUTH_HEADER, PUBLIC, write_ui_dist
from tests.oidc_fake import ISSUER, FakeProvider, config
from tests.server_test_helpers import session_headers, signed_in

SETTINGS = "/api/v1/settings/sign-in"


@pytest.fixture
def client(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    """Provide a browser at the public address of an app with OIDC sign-in on.

    Parameters
    ----------
    settings
        Configuration pointed at an isolated database.
    tmp_path
        For the stand-in build.
    monkeypatch
        Points the interface at the stand-in.

    Yields
    ------
    TestClient
        A started client.
    """
    monkeypatch.setattr(lakana_server.ui, "UI_DIST", write_ui_dist(tmp_path))
    app = create_app(
        settings.model_copy(
            update={"ui": UiConfig(enabled=True, public_url=PUBLIC), "oidc": config()}
        )
    )
    with TestClient(app, base_url=PUBLIC, headers={"Origin": PUBLIC}) as c:
        app.state.oidc_provider = FakeProvider().provider()
        yield c


def _person(
    client: TestClient, name: str, *, linked_ago: timedelta | None = None, admin: bool = False
) -> str:
    """Create a person, optionally linked, and issue them a token as the operator.

    Parameters
    ----------
    client
        The client.
    name
        The name.
    linked_ago
        How long ago they last signed in at the provider; `None` leaves them unlinked.
    admin
        Whether to grant them ADMIN on the instance.

    Returns
    -------
    str
        Their personal token.
    """
    r = client.post("/api/v1/principals", headers=AUTH_HEADER, json={"name": name, "type": "USER"})
    assert r.status_code == 201, r.text
    if admin:
        grant = {"principal": name, "role": "ADMIN", "kind": "INSTANCE"}
        assert client.post("/api/v1/grants", headers=AUTH_HEADER, json=grant).status_code == 201
    r = client.post(f"/api/v1/principals/{name}/tokens", headers=AUTH_HEADER, json={"name": "t"})
    assert r.status_code == 201, r.text
    if linked_ago is not None:
        app = cast(FastAPI, client.app)
        with Session(app.state.engine) as session:
            row = session.execute(select(Principal).where(Principal.name == name)).scalar_one()
            row.oidc_issuer, row.oidc_subject = ISSUER, name
            row.oidc_email = f"{name}@example.com"
            row.last_provider_sign_in_at = datetime.now(UTC) - linked_ago
            session.commit()
    return str(r.json()["token"])


def _bearer(token: str) -> dict[str, str]:
    """Build the header of a token.

    Parameters
    ----------
    token
        The token.

    Returns
    -------
    dict of str to str
        ``Authorization``.
    """
    return {"Authorization": f"Bearer {token}"}


def test_token_outside_the_window_is_401_with_the_next_step(client: TestClient) -> None:
    token = _person(client, "carol", linked_ago=timedelta(days=31))
    r = client.get("/api/v1/whoami", headers=_bearer(token))
    assert r.status_code == 401, r.text
    assert "Sign in to Lakana in the browser" in r.json()["detail"]
    listed = client.get("/api/v1/principals/carol/tokens", headers=AUTH_HEADER).json()
    assert [t["revoked_at"] for t in listed] == [None]


def test_whoami_shows_when_the_window_ends(client: TestClient) -> None:
    token = _person(client, "carol", linked_ago=timedelta(days=1))
    r = client.get("/api/v1/whoami", headers=_bearer(token))
    assert r.status_code == 200, r.text
    ends = datetime.fromisoformat(r.json()["token_window_ends_at"])
    assert timedelta(days=28, hours=23) < ends - datetime.now(UTC) < timedelta(days=29, hours=1)


def test_unlinked_person_has_no_window(client: TestClient) -> None:
    token = _person(client, "dave")
    assert (
        client.get("/api/v1/whoami", headers=_bearer(token)).json()["token_window_ends_at"] is None
    )


def test_the_session_says_whether_the_person_is_linked(client: TestClient) -> None:
    _person(client, "carol", linked_ago=timedelta(days=1))
    _person(client, "dave")
    for name, linked in (("carol", True), ("dave", False)):
        headers = session_headers(*signed_in(client, name))
        r = client.get("/api/v1/sign-in/session", headers=headers)
        assert r.json()["provider_linked"] is linked, name


def test_settings_show_the_provider_and_counts(client: TestClient) -> None:
    _person(client, "carol", linked_ago=timedelta(days=1))
    r = client.get(SETTINGS, headers=AUTH_HEADER)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["token_provider_window_days"] == 30
    assert (
        body["bounds"]["token_provider_window_days_min"],
        body["bounds"]["token_provider_window_days_max"],
    ) == (1, 365)
    assert body["provider"] == {
        "display_name": "idp.example.com",
        "issuer": ISSUER,
        "linked_people": 1,
        "unlinked_admins": 0,
    }
    # Somebody who is no administrator reads the lengths, not the counts.
    dave = _person(client, "dave")
    r = client.get(SETTINGS, headers=_bearer(dave))
    assert (r.json()["token_provider_window_days"], r.json()["provider"]) == (30, None)
    cast(FastAPI, client.app).state.oidc_provider = None
    assert client.get(SETTINGS, headers=AUTH_HEADER).json()["provider"] is None


def test_window_change_is_audited_and_needs_step_up(client: TestClient) -> None:
    token = _person(client, "carol", linked_ago=timedelta(days=8))
    assert client.get("/api/v1/whoami", headers=_bearer(token)).status_code == 200
    stale = session_headers(*signed_in(client, "admin"))
    r = client.patch(SETTINGS, headers=stale, json={"token_provider_window_days": 7})
    assert (r.status_code, r.json()["type"]) == (403, "StepUpRequired")
    r = client.patch(SETTINGS, headers=AUTH_HEADER, json={"token_provider_window_days": 7})
    assert (r.status_code, r.json()["token_provider_window_days"]) == (200, 7), r.text
    # The next request already counts the new window.
    assert client.get("/api/v1/whoami", headers=_bearer(token)).status_code == 401
    app = cast(FastAPI, client.app)
    with Session(app.state.engine) as session:
        [detail] = session.execute(
            select(AuditEvent.detail).where(
                AuditEvent.event_type == AuditEventType.SIGN_IN_SETTINGS_CHANGED
            )
        ).scalars()
    assert (
        detail["before"]["token_provider_window_days"],
        detail["after"]["token_provider_window_days"],
    ) == (30, 7)


@pytest.mark.parametrize(("days", "bound"), [(0, "at least 1 day"), (366, "at most 365 days")])
def test_window_bounds_are_1_and_365(client: TestClient, days: int, bound: str) -> None:
    r = client.patch(SETTINGS, headers=AUTH_HEADER, json={"token_provider_window_days": days})
    assert r.status_code == 422, r.text
    assert bound in r.text


def test_person_page_shows_the_link_to_admins_only(client: TestClient) -> None:
    _person(client, "carol", linked_ago=timedelta(days=1))
    r = client.get("/api/v1/principals/carol", headers=AUTH_HEADER)
    link = r.json()["provider_link"]
    assert (link["issuer"], link["email"], link["name"]) == (ISSUER, "carol@example.com", None)
    assert link["last_sign_in_at"] is not None and link["token_window_ends_at"] is not None
    dave = _person(client, "dave")
    assert (
        client.get("/api/v1/principals/dave", headers=AUTH_HEADER).json()["provider_link"] is None
    )
    # Someone who may manage principals but is no administrator does not see the link.
    grant = {"principal": "dave", "action": "MANAGE_PRINCIPAL", "kind": "INSTANCE"}
    assert client.post("/api/v1/grants", headers=AUTH_HEADER, json=grant).status_code == 201
    r = client.get("/api/v1/principals/carol", headers=_bearer(dave))
    assert (r.status_code, r.json()["provider_link"]) == (200, None), r.text
    listed = client.get("/api/v1/principals", headers=AUTH_HEADER).json()
    assert {p["name"]: p["provider_link"] is not None for p in listed}["carol"] is True


# -- an administrator unlinks a person (#32, PR 2) ----------------------------------------------


def test_unlink_route_unlinks(client: TestClient) -> None:
    token = _person(client, "carol", linked_ago=timedelta(days=1))
    r = client.delete("/api/v1/principals/carol/oidc-link", headers=AUTH_HEADER)
    assert r.status_code == 204, r.text
    assert (
        client.get("/api/v1/principals/carol", headers=AUTH_HEADER).json()["provider_link"] is None
    )
    # Unlinking revokes every personal token: the person comes back with a recovery link.
    assert client.get("/api/v1/whoami", headers=_bearer(token)).status_code == 401


def test_unlink_route_404_for_unknown_and_409_for_not_linked(client: TestClient) -> None:
    r = client.delete("/api/v1/principals/nobody/oidc-link", headers=AUTH_HEADER)
    assert r.status_code == 404, r.text
    _person(client, "dave")
    r = client.delete("/api/v1/principals/dave/oidc-link", headers=AUTH_HEADER)
    assert r.status_code == 409, r.text
    dave = _person(client, "erin")
    r = client.delete("/api/v1/principals/dave/oidc-link", headers=_bearer(dave))
    assert r.status_code == 403, r.text


def test_the_server_records_the_relying_party_for_the_break_glass_count(
    client: TestClient,
) -> None:
    from lakana_core.grants.break_glass import current_rp_id

    app = cast(FastAPI, client.app)
    with Session(app.state.engine) as session:
        assert current_rp_id(session) == "localhost"
