"""Tests for the sign-in link routes: setup, local, invites, recovery, describe, redeem, notices."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import cast

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import lakana_server.ui
from lakana_server.app import create_app
from lakana_server.settings import Settings, UiConfig
from tests.conftest import AUTH_HEADER, PUBLIC, SettableClock, write_ui_dist
from tests.server_test_helpers import session_headers, signed_in

UNKNOWN = {"type": "LinkUnusableError", "state": "unknown"}


def _parts(url: str) -> tuple[str, str]:
    """Split a link into its public id and secret.

    Parameters
    ----------
    url
        The link.

    Returns
    -------
    tuple of (str, str)
        The public id and the secret.
    """
    base, fragment = url.split("#", 1)
    assert base == f"{PUBLIC}/sign-in"
    values = dict(item.split("=", 1) for item in fragment.split("&"))
    return values["l"], values["s"]


def _person(client: TestClient, name: str) -> dict[str, str]:
    """Create a person and give them a personal access token.

    Parameters
    ----------
    client
        A started client.
    name
        The person's name.

    Returns
    -------
    dict of str to str
        The `Authorization` header of their token, plus ``key_id``.
    """
    r = client.post("/api/v1/principals", headers=AUTH_HEADER, json={"name": name, "type": "USER"})
    assert r.status_code == 201, r.text
    r = client.post(f"/api/v1/principals/{name}/tokens", headers=AUTH_HEADER, json={"name": "t"})
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['token']}", "key_id": r.json()["key_id"]}


def _bearer(person: dict[str, str]) -> dict[str, str]:
    """Keep only the `Authorization` header.

    Parameters
    ----------
    person
        What `_person` returned.

    Returns
    -------
    dict of str to str
        The header.
    """
    return {"Authorization": person["Authorization"]}


def _redeem(client: TestClient, url: str, **headers: str) -> httpx.Response:
    """Use a link the way the sign-in page does.

    Parameters
    ----------
    client
        A started client.
    url
        The link.
    **headers
        Extra request headers.

    Returns
    -------
    httpx.Response
        The answer.
    """
    public_id, secret = _parts(url)
    return client.post(
        "/api/v1/sign-in/links/redeem",
        json={"link_id": public_id, "secret": secret},
        headers=headers or None,
    )


def _invite(client: TestClient, name: str = "carol", display_name: str | None = None) -> dict:
    """Invite a person as the operator.

    Parameters
    ----------
    client
        A started client.
    name
        The name.
    display_name
        The display name.

    Returns
    -------
    dict
        The `InviteOut` body.
    """
    body: dict[str, str] = {"name": name}
    if display_name is not None:
        body["display_name"] = display_name
    r = client.post("/api/v1/invites", headers=AUTH_HEADER, json=body)
    assert r.status_code == 201, r.text
    return r.json()


def test_redeem_sets_the_cookie_and_returns_no_secret(ui_client: TestClient) -> None:
    """The setup link signs the operator's account in: a cookie, a CSRF token, no secret."""
    r = ui_client.post("/api/v1/sign-in/setup-link", headers=AUTH_HEADER)
    assert r.status_code == 200, r.text
    url = r.json()["url"]
    public_id, secret = _parts(url)
    described = ui_client.post("/api/v1/sign-in/links/describe", json={"link_id": public_id})
    assert described.status_code == 200, described.text
    assert described.json() == {
        "kind": "setup",
        "name": "admin",
        "display_name": None,
        "state": "open",
    }
    r = _redeem(ui_client, url)
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"name", "needs_passkey", "offer_passkey", "csrf_token"}
    assert (body["name"], body["needs_passkey"], body["offer_passkey"]) == ("admin", True, False)
    assert secret not in r.text
    cookie = r.headers["set-cookie"]
    assert cookie.startswith("lakana_session_8000=")
    assert "httponly" in cookie.lower() and "samesite=strict" in cookie.lower()
    value = cookie.split(";", 1)[0].split("=", 1)[1]
    me = ui_client.get(
        "/api/v1/sign-in/session", headers=session_headers("lakana_session_8000", value)
    )
    assert me.status_code == 200, me.text
    assert me.json()["needs_passkey"] is True
    assert me.json()["csrf_token"] == body["csrf_token"]


def test_describe_of_an_unknown_link_is_410(ui_client: TestClient) -> None:
    """An unknown public id answers 410 with state `unknown`."""
    r = ui_client.post("/api/v1/sign-in/links/describe", json={"link_id": "nope"})
    assert r.status_code == 410, r.text
    assert {k: r.json()[k] for k in UNKNOWN} == UNKNOWN
    assert "new link" in r.json()["detail"]


def test_a_wrong_secret_answers_exactly_like_an_unknown_link(ui_client: TestClient) -> None:
    """A real public id with a wrong secret gets the body of an unknown id."""
    public_id, _ = _parts(_invite(ui_client)["url"])
    wrong = ui_client.post(
        "/api/v1/sign-in/links/redeem", json={"link_id": public_id, "secret": "wrong"}
    )
    unknown = ui_client.post(
        "/api/v1/sign-in/links/redeem", json={"link_id": "nope", "secret": "wrong"}
    )
    assert wrong.status_code == unknown.status_code == 410
    assert wrong.json() == unknown.json()
    assert wrong.json()["state"] == "unknown"


def _make(client: TestClient, state: str) -> str:
    """Make a link in a given state.

    Parameters
    ----------
    client
        A started client of an app whose clock is a `SettableClock`.
    state
        ``used``, ``expired`` or ``withdrawn``.

    Returns
    -------
    str
        The link.
    """
    invite = _invite(client)
    if state == "used":
        assert _redeem(client, invite["url"]).status_code == 200
    elif state == "withdrawn":
        r = client.delete(f"/api/v1/invites/{invite['id']}", headers=AUTH_HEADER)
        assert r.status_code == 204, r.text
    else:
        cast(FastAPI, client.app).state.clock.advance(timedelta(days=7))
    return invite["url"]


@pytest.mark.parametrize(
    ("state", "next_step"),
    [
        ("used", "sign in with your passkey"),
        ("expired", "new link"),
        ("withdrawn", "new link"),
    ],
)
def test_an_unusable_link_says_why_and_what_next(
    ui_client: TestClient, clock: SettableClock, state: str, next_step: str
) -> None:
    """Describe and redeem of a used, expired or withdrawn link answer 410 with its state."""
    cast(FastAPI, ui_client.app).state.clock = clock
    url = _make(ui_client, state)
    public_id, _ = _parts(url)
    for r in (
        ui_client.post("/api/v1/sign-in/links/describe", json={"link_id": public_id}),
        _redeem(ui_client, url),
    ):
        assert r.status_code == 410, r.text
        assert r.json()["state"] == state
        assert r.json()["type"] == "LinkUnusableError"
        assert next_step in r.json()["detail"].lower()


def test_a_cross_origin_redeem_is_refused(ui_client: TestClient) -> None:
    """The guard refuses a redeem from another origin before the route runs."""
    url = _invite(ui_client)["url"]
    r = _redeem(ui_client, url, Origin="https://evil.example")
    assert r.status_code == 403, r.text
    assert _redeem(ui_client, url).status_code == 200


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "/api/v1/sign-in/links/describe", {"link_id": "x"}),
        ("POST", "/api/v1/sign-in/links/redeem", {"link_id": "x", "secret": "y"}),
        ("POST", "/api/v1/sign-in/setup-link", None),
        ("POST", "/api/v1/sign-in/local-link", None),
        ("GET", "/api/v1/sign-in/notices", None),
        ("POST", "/api/v1/invites", {"name": "carol"}),
        ("POST", "/api/v1/principals/admin/recovery-link", None),
    ],
)
def test_routes_answer_409_with_the_interface_off(
    client: TestClient, method: str, path: str, body: dict | None
) -> None:
    """With `LAKANA_UI__ENABLED=false` every link route says how to turn the interface on."""
    r = client.request(method, path, headers=AUTH_HEADER, json=body)
    assert r.status_code == 409, r.text
    assert "LAKANA_UI__ENABLED" in r.json()["detail"]


def test_invite_creation_without_step_up_is_403(ui_client: TestClient) -> None:
    """An administrator's session without a fresh passkey touch cannot invite."""
    name, value, csrf = signed_in(ui_client, "admin")
    r = ui_client.post(
        "/api/v1/invites",
        headers=session_headers(name, value, csrf),
        json={"name": "carol"},
    )
    assert r.status_code == 403, r.text
    assert r.json()["type"] == "StepUpRequired"


def test_the_secret_appears_in_no_log_record(
    ui_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """Issuing, describing and using a link never logs its secret."""
    caplog.set_level(logging.DEBUG)
    url = _invite(ui_client)["url"]
    public_id, secret = _parts(url)
    ui_client.post("/api/v1/sign-in/links/describe", json={"link_id": public_id})
    assert _redeem(ui_client, url).status_code == 200
    ui_client.post("/api/v1/sign-in/links/redeem", json={"link_id": public_id, "secret": secret})
    assert caplog.records
    for record in caplog.records:
        assert secret not in record.getMessage()
        assert secret not in repr(record.args)


def test_invites_are_listed_without_their_url_and_withdrawn(ui_client: TestClient) -> None:
    """The list shows open invites without a secret; withdrawing frees the name."""
    invite = _invite(ui_client, "carol", "Carol C.")
    assert set(invite) == {"id", "name", "display_name", "url", "expires_at"}
    r = ui_client.get("/api/v1/invites", headers=AUTH_HEADER)
    assert r.status_code == 200, r.text
    assert [(i["name"], i["display_name"]) for i in r.json()] == [("carol", "Carol C.")]
    assert "url" not in r.json()[0]
    assert _parts(invite["url"])[1] not in r.text
    again = ui_client.post("/api/v1/invites", headers=AUTH_HEADER, json={"name": "Carol"})
    assert again.status_code == 409, again.text
    assert (
        ui_client.delete(f"/api/v1/invites/{invite['id']}", headers=AUTH_HEADER).status_code == 204
    )
    assert ui_client.get("/api/v1/invites", headers=AUTH_HEADER).json() == []


def test_principal_create_refuses_a_case_variant_over_rest(ui_client: TestClient) -> None:
    """`POST /api/v1/principals` refuses `Admin` while `admin` exists, naming letter case."""
    r = ui_client.post(
        "/api/v1/principals", headers=AUTH_HEADER, json={"name": "Admin", "type": "USER"}
    )
    assert r.status_code == 409, r.text
    assert "letter case" in r.json()["detail"]


def test_an_invite_signs_in_a_new_person_with_their_display_name(ui_client: TestClient) -> None:
    """The session of an accepted invite shows its display name and asks for a passkey."""
    r = _redeem(ui_client, _invite(ui_client, "carol", "Carol C.")["url"])
    assert r.status_code == 200, r.text
    assert r.json()["needs_passkey"] is True
    value = r.headers["set-cookie"].split(";", 1)[0].split("=", 1)[1]
    me = ui_client.get(
        "/api/v1/sign-in/session", headers=session_headers("lakana_session_8000", value)
    )
    assert (me.json()["name"], me.json()["display_name"]) == ("carol", "Carol C.")


def test_recovery_links_notify_and_can_be_listed_and_withdrawn(ui_client: TestClient) -> None:
    """The operator recovers a person, who and the administrators then see a notice."""
    _person(ui_client, "alice")
    r = ui_client.post("/api/v1/principals/alice/recovery-link", headers=AUTH_HEADER)
    assert r.status_code == 201, r.text
    assert set(r.json()) == {"id", "name", "url", "expires_at"}
    listed = ui_client.get("/api/v1/recovery-links", headers=AUTH_HEADER)
    assert [(i["id"], i["name"]) for i in listed.json()] == [(r.json()["id"], "alice")]
    assert "url" not in listed.json()[0]
    redeemed = _redeem(ui_client, r.json()["url"])
    assert redeemed.status_code == 200, redeemed.text
    value = redeemed.headers["set-cookie"].split(";", 1)[0].split("=", 1)[1]
    headers = session_headers("lakana_session_8000", value)
    notices = ui_client.get("/api/v1/sign-in/notices", headers=headers)
    assert notices.status_code == 200, notices.text
    assert [(n["kind"], n["subject"]) for n in notices.json()] == [("recovery_used", "alice")]
    operator = ui_client.get("/api/v1/sign-in/notices", headers=AUTH_HEADER).json()
    assert len(operator) == 1
    notice_id = notices.json()[0]["id"]
    csrf = redeemed.json()["csrf_token"]
    gone = ui_client.delete(
        f"/api/v1/sign-in/notices/{notice_id}",
        headers=session_headers("lakana_session_8000", value, csrf),
    )
    assert gone.status_code == 204, gone.text
    assert ui_client.get("/api/v1/sign-in/notices", headers=headers).json() == []
    second = ui_client.post("/api/v1/principals/alice/recovery-link", headers=AUTH_HEADER)
    r = ui_client.delete(f"/api/v1/recovery-links/{second.json()['id']}", headers=AUTH_HEADER)
    assert r.status_code == 204, r.text


def test_recovery_of_an_administrator_by_another_admin_is_403(ui_client: TestClient) -> None:
    """An ordinary administrator's token cannot recover an administrator."""
    boss = _person(ui_client, "boss")
    r = ui_client.post(
        "/api/v1/grants",
        headers=AUTH_HEADER,
        json={"principal": "boss", "role": "ADMIN", "kind": "INSTANCE"},
    )
    assert r.status_code in (200, 201), r.text
    r = ui_client.post("/api/v1/principals/admin/recovery-link", headers=_bearer(boss))
    assert r.status_code == 403, r.text
    assert "server operator" in r.json()["detail"]


def test_local_mode_comes_from_the_setting(ui_client: TestClient) -> None:
    """With `LAKANA_UI__LOCAL_SIGN_IN` unset a local link is 404, naming the next step."""
    alice = _person(ui_client, "alice")
    r = ui_client.post("/api/v1/sign-in/local-link", headers=_bearer(alice))
    assert r.status_code == 404, r.text
    assert "recovery link" in r.json()["detail"]


@pytest.fixture
def local_client(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    """Provide a client of an app with the interface and local sign-in on.

    Parameters
    ----------
    settings
        Configuration pointed at an isolated database.
    tmp_path
        Pytest's per-test directory, for the stand-in build.
    monkeypatch
        Points `lakana_server.ui.UI_DIST` at the stand-in.

    Yields
    ------
    TestClient
        A started client at the public address.
    """
    monkeypatch.setattr(lakana_server.ui, "UI_DIST", write_ui_dist(tmp_path))
    local = settings.model_copy(update={"ui": UiConfig(enabled=True, local_sign_in=True)})
    with TestClient(create_app(local), base_url=PUBLIC, headers={"Origin": PUBLIC}) as c:
        yield c


def test_a_local_link_signs_in_without_a_passkey_and_ends_with_its_token(
    local_client: TestClient,
) -> None:
    """A local session offers a passkey, and ends (`token_revoked`) when its token is revoked."""
    alice = _person(local_client, "alice")
    r = local_client.post("/api/v1/sign-in/local-link", headers=_bearer(alice))
    assert r.status_code == 200, r.text
    redeemed = _redeem(local_client, r.json()["url"])
    assert redeemed.status_code == 200, redeemed.text
    assert (redeemed.json()["needs_passkey"], redeemed.json()["offer_passkey"]) == (False, True)
    value = redeemed.headers["set-cookie"].split(";", 1)[0].split("=", 1)[1]
    headers = session_headers("lakana_session_8000", value)
    assert local_client.get("/api/v1/sign-in/session", headers=headers).status_code == 200
    revoked = local_client.delete(
        f"/api/v1/principals/alice/tokens/{alice['key_id']}", headers=AUTH_HEADER
    )
    assert revoked.status_code == 204, revoked.text
    ended = local_client.get("/api/v1/sign-in/session", headers=headers)
    assert ended.status_code == 401, ended.text
    assert ended.json()["reason"] == "token_revoked"


def test_a_local_link_is_not_used_over_an_open_session(local_client: TestClient) -> None:
    """A browser that is signed in gets 409, and the link stays usable."""
    alice = _person(local_client, "alice")
    url = local_client.post("/api/v1/sign-in/local-link", headers=_bearer(alice)).json()["url"]
    name, value, _ = signed_in(local_client, "admin")
    r = _redeem(local_client, url, Cookie=f"{name}={value}")
    assert r.status_code == 409, r.text
    assert "already signed in" in r.json()["detail"]
    assert _redeem(local_client, url).status_code == 200


def test_a_session_cannot_mint_a_local_link(local_client: TestClient) -> None:
    """Only a person's own token mints a local link."""
    _person(local_client, "alice")
    name, value, csrf = signed_in(local_client, "alice")
    r = local_client.post("/api/v1/sign-in/local-link", headers=session_headers(name, value, csrf))
    assert r.status_code == 403, r.text
    assert "personal access token" in r.json()["detail"]
