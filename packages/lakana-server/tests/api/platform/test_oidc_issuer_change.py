"""Changing the identity provider's issuer, or turning OIDC sign-in off, unlinks people (#809).

The sign-in design ("Settings"): "Changing the issuer leaves the linked people unlinked until they
link again." A link whose issuer is not `LAKANA_OIDC__ISSUER` counts as no link, and with OIDC
sign-in off nobody is linked: the person's passkeys work again, step-up asks for a passkey, and
they may link to the new provider from their profile. Their tokens keep the token window
(maintainer decision on #809) until they do. A stale link never signs anyone in at the new
provider.
"""

from __future__ import annotations

import logging
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
from lakana_core.identity.provider_issuer import record_oidc_issuer
from lakana_core.models import AuditEvent, AuditEventType, Principal
from lakana_server.app import create_app
from lakana_server.settings import Settings, UiConfig
from tests.api.platform.test_oidc_sign_in import (
    CONFIRMED,
    FINISH,
    FINISH_IN_SESSION,
    LINK_START,
    STEP_UP_START,
    _callback_to,
    _events,
    _now,
    _person,
    _refused,
    _signed_in_at_provider,
)
from tests.api.platform.test_passkeys import _answer, _register
from tests.conftest import AUTH_HEADER, PUBLIC, write_ui_dist
from tests.oidc_fake import ISSUER, FakeProvider, config
from tests.server_test_helpers import signed_in

OLD_ISSUER = "https://old-idp.example.com/realms/lakana"
"""The issuer the people were linked to before the provider moved."""

SUBJECT = "248289761001"
"""The ``sub`` the fake provider answers by default."""


def _app(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **oidc: object
) -> FastAPI:
    """Build an app with the interface on at `PUBLIC`.

    Parameters
    ----------
    settings
        Configuration pointed at an isolated database.
    tmp_path
        For the stand-in build.
    monkeypatch
        Points the interface at the stand-in.
    **oidc
        ``on=False`` turns OIDC sign-in off; other keys change the fake provider's settings.

    Returns
    -------
    FastAPI
        The app, not started.
    """
    monkeypatch.setattr(lakana_server.ui, "UI_DIST", write_ui_dist(tmp_path))
    update: dict[str, object] = {"ui": UiConfig(enabled=True, public_url=PUBLIC)}
    if oidc.pop("on", True):
        update["oidc"] = config(**oidc)
    return create_app(settings.model_copy(update=update))


def _started(app: FastAPI, fake: FakeProvider | None) -> TestClient:
    """Start a browser of the app.

    Parameters
    ----------
    app
        The app.
    fake
        The fake provider to answer for the configured one; `None` with OIDC sign-in off.

    Returns
    -------
    TestClient
        The browser, entered: the caller exits it.
    """
    c = TestClient(app, base_url=PUBLIC, headers={"Origin": PUBLIC}).__enter__()
    if fake is not None:
        app.state.oidc_provider = fake.provider()
        app.state.fake_provider = fake
    return c


@pytest.fixture
def moved(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    """Provide a browser of a server whose provider is now `ISSUER`.

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
        A started client whose requests carry the public `Origin`.
    """
    c = _started(_app(settings, tmp_path, monkeypatch), FakeProvider())
    try:
        yield c
    finally:
        c.__exit__(None, None, None)


@pytest.fixture
def off(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    """Provide a browser of a server with OIDC sign-in off.

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
        A started client whose requests carry the public `Origin`.
    """
    c = _started(_app(settings, tmp_path, monkeypatch, on=False), None)
    try:
        yield c
    finally:
        c.__exit__(None, None, None)


def _stale(client: TestClient, name: str, *, issuer: str = OLD_ISSUER) -> str:
    """Create a person linked to another issuer, 90 days since their last provider sign-in.

    Parameters
    ----------
    client
        The browser.
    name
        The name.
    issuer
        The issuer of their stored link.

    Returns
    -------
    str
        Their personal token, issued before they were linked.
    """
    r = client.post("/api/v1/principals", headers=AUTH_HEADER, json={"name": name, "type": "USER"})
    assert r.status_code == 201, r.text
    r = client.post(f"/api/v1/principals/{name}/tokens", headers=AUTH_HEADER, json={"name": "t"})
    assert r.status_code == 201, r.text
    app = cast(FastAPI, client.app)
    with Session(app.state.engine) as session:
        row = session.execute(select(Principal).where(Principal.name == name)).scalar_one()
        row.oidc_issuer, row.oidc_subject = issuer, SUBJECT
        row.oidc_email = f"{name}@example.com"
        row.last_provider_sign_in_at = datetime.now(UTC) - timedelta(days=90)
        session.commit()
    return str(r.json()["token"])


def _session(client: TestClient, name: str, *, touched: bool) -> dict[str, str]:
    """Sign a person in directly and put the session cookie into the browser.

    Parameters
    ----------
    client
        The browser.
    name
        The person.
    touched
        Whether the session has a fresh step-up touch.

    Returns
    -------
    dict of str to str
        The CSRF header the page sends; the cookie is in the browser.
    """
    clock = cast(FastAPI, client.app).state.clock
    cookie, value, csrf = signed_in(client, name, touched_at=clock() if touched else None)
    client.cookies.set(cookie, value)
    return {"X-Lakana-CSRF": csrf}


def _the_person_is_unlinked(client: TestClient, name: str, token: str, *, oidc_on: bool) -> None:
    """Check every place that says whether a person is linked says they are not.

    Parameters
    ----------
    client
        The browser.
    name
        The person, with a stale link and a provider sign-in 90 days ago.
    token
        Their personal token.
    oidc_on
        Whether the server runs with an issuer, which decides the token's refusal sentence.
    """
    bearer = {"Authorization": f"Bearer {token}"}
    # The token window stays: 90 days without a provider sign-in, so the token stops, and the
    # sentence says how to get it back.
    r = client.get("/api/v1/whoami", headers=bearer)
    assert r.status_code == 401, r.text
    detail = r.json()["detail"]
    assert ("current provider" in detail) is oidc_on and ("unlink you" in detail) is not oidc_on
    # Access > a person.
    r = client.get(f"/api/v1/principals/{name}", headers=AUTH_HEADER)
    # Access > a person: the link is shown, marked stale, with the end of the token window.
    link = r.json()["provider_link"]
    assert r.status_code == 200 and link["stale"] is True, r.text
    assert link["token_window_ends_at"] is not None
    # The profile: the browser session tells the interface to offer a passkey, not the provider.
    headers = _session(client, name, touched=True)
    r = client.get("/api/v1/sign-in/session", headers=headers)
    assert r.json()["provider_linked"] is False
    # A passkey may be added; step-up asks for it, and it signs the person in.
    auth, _ = _register(client, name, headers)
    r = client.post("/api/v1/sign-in/step-up/options", headers=headers)
    assert r.status_code == 200, r.text
    client.cookies.clear()
    options = client.post("/api/v1/sign-in/passkey/options").json()
    r = client.post("/api/v1/sign-in/passkey", json=_answer(auth, options))
    assert r.status_code == 200, r.text
    assert r.json()["name"] == name


def test_a_person_linked_to_another_issuer_counts_as_unlinked(moved: TestClient) -> None:
    token = _stale(moved, "alice")
    _the_person_is_unlinked(moved, "alice", token, oidc_on=True)
    r = moved.get("/api/v1/settings/sign-in", headers=AUTH_HEADER)
    assert r.json()["provider"]["linked_people"] == 0


def test_with_oidc_off_a_linked_person_counts_as_unlinked(off: TestClient) -> None:
    token = _stale(off, "alice", issuer=ISSUER)
    _the_person_is_unlinked(off, "alice", token, oidc_on=False)


def test_a_stale_link_signs_nobody_in_at_the_new_provider(moved: TestClient) -> None:
    """Someone at the new provider with the stale link's ``sub`` is not that person."""
    _stale(moved, "alice")
    _signed_in_at_provider(moved, cast(FastAPI, moved.app).state.fake_provider, sub=SUBJECT)
    r = moved.post(FINISH, json={})
    _refused(r, "not_linked")


def test_provider_step_up_is_not_offered_for_a_stale_link(moved: TestClient) -> None:
    """Step-up stays a passkey touch: the provider's answer for the stale ``sub`` is no touch."""
    _stale(moved, "alice")
    headers = _session(moved, "alice", touched=False)
    r = moved.post(STEP_UP_START, headers=headers, json={})
    assert r.status_code == 409, r.text
    assert "passkey" in r.json()["detail"]


def test_a_person_with_a_stale_link_links_to_the_new_provider(moved: TestClient) -> None:
    token = _stale(moved, "alice")
    headers = _session(moved, "alice", touched=True)
    r = moved.post(LINK_START, headers=headers, json={})
    assert r.status_code == 200, r.text
    url = r.json()["authorization_url"]
    assert _callback_to(moved, url, sub="alice-new", auth_time=_now()) == CONFIRMED
    r = moved.post(FINISH_IN_SESSION, headers=headers, json={})
    assert r.status_code == 200, r.text
    alice = _person(moved, "alice")
    assert (alice.oidc_issuer, alice.oidc_subject) == (ISSUER, "alice-new")
    [linked] = _events(moved, AuditEventType.PRINCIPAL_OIDC_LINKED)
    assert linked["replaces"] == {"iss": OLD_ISSUER, "sub": SUBJECT}
    r = moved.get("/api/v1/sign-in/session", headers=headers)
    assert r.json()["provider_linked"] is True
    # Linking again opens the current window: the token from before works again.
    r = moved.get("/api/v1/whoami", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    assert r.json()["token_window_ends_at"] is not None


def test_a_provider_step_up_started_before_the_issuer_moved_is_refused(moved: TestClient) -> None:
    """The step-up finish asks `is_linked` too: a link that stopped counting confirms nothing."""
    _stale(moved, "alice", issuer=ISSUER)
    headers = _session(moved, "alice", touched=False)
    r = moved.post(STEP_UP_START, headers=headers, json={})
    assert r.status_code == 200, r.text
    # Another server of this database started with another issuer meanwhile.
    app = cast(FastAPI, moved.app)
    with Session(app.state.engine) as session:
        record_oidc_issuer(session, OLD_ISSUER)
    url = r.json()["authorization_url"]
    assert _callback_to(moved, url, sub=SUBJECT, auth_time=_now()) == CONFIRMED
    _refused(moved.post(FINISH_IN_SESSION, headers=headers, json={}), "identity_mismatch")
    r = moved.get("/api/v1/sign-in/session", headers=headers)
    assert r.json()["step_up_until"] is None


def test_starting_with_another_issuer_is_audited_and_logged(
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    first = _started(_app(settings, tmp_path / "1", monkeypatch, issuer=OLD_ISSUER), None)
    try:
        _stale(first, "alice")
    finally:
        first.__exit__(None, None, None)
    with caplog.at_level(logging.WARNING, logger="lakana_server.app"):
        second = _started(_app(settings, tmp_path / "2", monkeypatch), None)
        second.__exit__(None, None, None)
    assert f"issuer is now {ISSUER}" in caplog.text
    assert "1 people linked to another issuer now count as unlinked (alice)" in caplog.text
    assert "Deactivate in Lakana anyone of them who left at the old provider" in caplog.text
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="lakana_server.app"):
        third = _started(_app(settings, tmp_path / "3", monkeypatch, on=False), None)
    try:
        app = cast(FastAPI, third.app)
        with Session(app.state.engine) as session:
            details = [
                dict(d)
                for d in session.execute(
                    select(AuditEvent.detail)
                    .where(AuditEvent.event_type == AuditEventType.SIGN_IN_SETTINGS_CHANGED)
                    .order_by(AuditEvent.occurred_at)
                ).scalars()
            ]
    finally:
        third.__exit__(None, None, None)
    assert "LAKANA_OIDC__ISSUER" in caplog.text
    assert [(d["before"], d["after"], d["links_not_counted"]) for d in details] == [
        ({"oidc_issuer": OLD_ISSUER}, {"oidc_issuer": ISSUER}, 1),
        ({"oidc_issuer": ISSUER}, {"oidc_issuer": None}, 1),
    ]
