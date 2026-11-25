"""The commands of the how-to "Bring your team onto single sign-on", run against the real app.

`docs/examples/oidc-sign-in.sh` holds what the page shows. Each region runs here with the CLI,
against an app with OIDC sign-in on, `sam` linked to the identity provider and `kim` a linked
administrator, and the test checks that the command did what the page says it does.
"""

from __future__ import annotations

import json
import shlex
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from scripts.docs_blocks import read_source
from sqlalchemy import select
from sqlalchemy.orm import Session

import lakana_server.ui
from lakana_core.models import Principal
from lakana_server.app import create_app
from lakana_server.settings import Settings, UiConfig
from tests.cli.conftest import make_cli_state, run_cli
from tests.conftest import AUTH_HEADER, PUBLIC, write_ui_dist
from tests.oidc_fake import ISSUER, FakeProvider, config

_ROOT = Path(__file__).resolve().parents[4]
_SCRIPT = "docs/examples/oidc-sign-in.sh"


@pytest.fixture
def app(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    """Run an app with OIDC sign-in on, `sam` linked and `kim` a linked administrator.

    Parameters
    ----------
    settings
        The base settings.
    tmp_path
        For the stand-in build of the interface.
    monkeypatch
        Points the interface at the stand-in.

    Yields
    ------
    TestClient
        The client, at the public address.
    """
    monkeypatch.setattr(lakana_server.ui, "UI_DIST", write_ui_dist(tmp_path))
    application = create_app(
        settings.model_copy(
            update={"ui": UiConfig(enabled=True, public_url=PUBLIC), "oidc": config()}
        )
    )
    with TestClient(application, base_url=PUBLIC, headers={"Origin": PUBLIC}) as client:
        application.state.oidc_provider = FakeProvider().provider()
        _linked_person(client, "sam")
        _linked_person(client, "kim", admin=True)
        yield client


def _linked_person(client: TestClient, name: str, *, admin: bool = False) -> None:
    """Create a person linked to the provider a day ago, with a personal token.

    Parameters
    ----------
    client
        The client.
    name
        The name.
    admin
        Whether to grant them ADMIN on the instance.
    """
    made = client.post(
        "/api/v1/principals", headers=AUTH_HEADER, json={"name": name, "type": "USER"}
    )
    assert made.status_code == 201, made.text
    if admin:
        grant = {"principal": name, "role": "ADMIN", "kind": "INSTANCE"}
        assert client.post("/api/v1/grants", headers=AUTH_HEADER, json=grant).status_code == 201
    token = client.post(
        f"/api/v1/principals/{name}/tokens", headers=AUTH_HEADER, json={"name": "laptop"}
    )
    assert token.status_code == 201, token.text
    with Session(cast(FastAPI, client.app).state.engine) as session:
        row = session.execute(select(Principal).where(Principal.name == name)).scalar_one()
        row.oidc_issuer, row.oidc_subject = ISSUER, f"{name}-at-the-provider"
        row.last_provider_sign_in_at = datetime.now(UTC) - timedelta(days=1)
        session.commit()


def _run_region(app: TestClient, tmp_path: Path, region: str) -> str:
    """Run every command of a region of the page's script as the server operator.

    Parameters
    ----------
    app
        The client.
    tmp_path
        Pytest's per-test directory.
    region
        The region's name.

    Returns
    -------
    str
        What the last command printed on stdout.
    """
    state = make_cli_state(app, tmp_path)
    out = ""
    for line in read_source(_ROOT, f"{_SCRIPT}#{region}").splitlines():
        words = shlex.split(line)
        assert words[0] == "lakana", line
        result = run_cli(state, *words[1:])
        assert result.exit_code == 0, (words, result.stderr)
        out = result.stdout
    return out


def _provider_link(app: TestClient, name: str) -> object:
    """Read a person's link to the provider as an administrator sees it.

    Parameters
    ----------
    app
        The client.
    name
        The person.

    Returns
    -------
    object
        `provider_link` of the principal; `None` when they are not linked.
    """
    return app.get(f"/api/v1/principals/{name}", headers=AUTH_HEADER).json()["provider_link"]


def _active_tokens(app: TestClient, name: str) -> list[object]:
    """List the person's tokens that were not revoked.

    Parameters
    ----------
    app
        The client.
    name
        The person.

    Returns
    -------
    list
        The tokens whose `revoked_at` is empty.
    """
    listed = app.get(f"/api/v1/principals/{name}/tokens", headers=AUTH_HEADER).json()
    return [t for t in listed if t["revoked_at"] is None]


def test_the_window_command_sets_the_token_window(app: TestClient, tmp_path: Path) -> None:
    _run_region(app, tmp_path, "window")
    body = app.get("/api/v1/settings/sign-in", headers=AUTH_HEADER).json()
    assert body["token_provider_window_days"] == 14


def test_offboarding_deactivates_and_keeps_the_link(app: TestClient, tmp_path: Path) -> None:
    _run_region(app, tmp_path, "offboard")
    sam = app.get("/api/v1/principals/sam", headers=AUTH_HEADER).json()
    assert sam["is_active"] is False
    # Deactivating is not unlinking: reactivated, the person still signs in through the provider.
    assert _provider_link(app, "sam") is not None


def test_unlinking_revokes_the_tokens(app: TestClient, tmp_path: Path) -> None:
    _run_region(app, tmp_path, "unlink")
    assert _provider_link(app, "sam") is None
    assert _active_tokens(app, "sam") == []


def test_the_operator_s_recovery_link_unlinks_a_linked_administrator(
    app: TestClient, tmp_path: Path
) -> None:
    url = _run_region(app, tmp_path, "operator-recovery").strip()
    base, fragment = url.split("#", 1)
    assert base == f"{PUBLIC}/sign-in"
    values = dict(item.split("=", 1) for item in fragment.split("&"))
    assert _provider_link(app, "kim") is not None
    redeemed = app.post(
        "/api/v1/sign-in/links/redeem", json={"link_id": values["l"], "secret": values["s"]}
    )
    assert redeemed.status_code == 200, redeemed.text
    assert _provider_link(app, "kim") is None
    assert _active_tokens(app, "kim") == []


def test_the_refusals_command_lists_a_refused_sign_in_with_its_reason(
    app: TestClient, tmp_path: Path
) -> None:
    # A callback that names no open flow is refused as `flow_expired` and audited.
    answer = app.get(
        "/api/v1/sign-in/oidc/callback", params={"code": "c", "state": "s"}, follow_redirects=False
    )
    assert answer.status_code == 303, answer.text
    events = json.loads(_run_region(app, tmp_path, "refusals"))
    reasons = [event["detail"]["reason"] for event in events["items"]]
    assert reasons == ["flow_expired"], events
