"""`GET /api/v1/settings/configuration`: the server's settings, never a secret's value."""

from __future__ import annotations

import base64
import logging
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from lakana_core.config import DatabaseConfig, RunConfig, SecretsConfig
from lakana_server.settings import Settings
from tests.api.platform.test_ci_oidc import _bearer, _exchange, _setup, ci_client  # ruff: ignore[unused-import]
from tests.conftest import AUTH_HEADER
from tests.server_test_helpers import session_headers, signed_in

_URL = "/api/v1/settings/configuration"
_MASTER = base64.b64encode(b"master-sentinel-0123456789abcdef").decode()


def _entries(response: httpx.Response) -> dict[str, dict[str, Any]]:
    return {item["name"]: item for item in response.json()}


def test_no_secret_value_reaches_the_answer(
    client: TestClient, settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    client.app.state.settings = Settings(
        database=DatabaseConfig(url="postgresql://lakana:pw-sentinel@db.example/lakana"),
        bootstrap_pat_key_id="testkey",
        bootstrap_pat_secret="bootstrap-sentinel",
        secrets=SecretsConfig(master_key=_MASTER),
        run=RunConfig(
            index_url="https://u:idx-sentinel@pypi.example/simple",
            https_proxy="http://proxy:px-sentinel@proxy.example:3128",
        ),
    )
    caplog.set_level(logging.DEBUG)
    response = client.get(_URL, headers=AUTH_HEADER)
    assert response.status_code == 200, response.text
    for sentinel in ("pw-sentinel", "bootstrap-sentinel", "idx-sentinel", "px-sentinel", _MASTER):
        assert sentinel not in response.text
        assert sentinel not in caplog.text
    entries = _entries(response)
    for name in (
        "LAKANA_DATABASE__URL",
        "LAKANA_SECRETS__MASTER_KEY",
        "LAKANA_BOOTSTRAP_PAT_SECRET",
        "LAKANA_RUN__INDEX_URL",
        "LAKANA_RUN__HTTPS_PROXY",
    ):
        assert entries[name]["secret"] is True, name
        assert entries[name]["value"] is None, name
        assert entries[name]["has_value"] is True, name


def test_an_unset_secret_says_it_has_no_value(client: TestClient) -> None:
    entries = _entries(client.get(_URL, headers=AUTH_HEADER))
    unset = entries["LAKANA_RUN__HTTPS_PROXY"]
    assert (unset["secret"], unset["value"], unset["has_value"]) == (True, None, False)
    assert entries["LAKANA_PORT"]["source"] == "default"
    assert entries["LAKANA_PORT"]["secret"] is False


def test_configuration_is_403_for_a_person_a_task_token_and_a_ci_token(
    ci_client: TestClient,  # ruff: ignore[redefined-while-unused]
) -> None:
    _setup(ci_client)
    created = ci_client.post(
        "/api/v1/principals", headers=AUTH_HEADER, json={"name": "bob", "type": "USER"}
    )
    assert created.status_code == 201, created.text
    issued = ci_client.post(
        "/api/v1/principals/bob/tokens", headers=AUTH_HEADER, json={"name": "t"}
    )
    assert issued.status_code == 201, issued.text
    minted = ci_client.post(
        "/api/v1/task-tokens",
        headers=AUTH_HEADER,
        json={
            "principal": "admin",
            "environment": "dev",
            "asset_scope": ["gold.x"],
            "ttl_seconds": 60,
        },
    )
    assert minted.status_code == 201, minted.text
    ci_token = _exchange(ci_client).json()["token"]
    for token in (issued.json()["token"], minted.json()["token"], ci_token):
        response = ci_client.get(_URL, headers=_bearer(token))
        assert response.status_code == 403, response.text
    assert ci_client.get(_URL, headers=AUTH_HEADER).status_code == 200


def test_configuration_from_a_browser_session(browser_client: TestClient) -> None:
    name, value, csrf = signed_in(browser_client, "admin", touched_at=datetime.now(UTC))
    response = browser_client.get(_URL, headers=session_headers(name, value, csrf))
    assert response.status_code == 200, response.text
    assert response.json()[0]["name"] == "LAKANA_DATABASE__URL"
    browser_client.post(
        "/api/v1/principals", headers=AUTH_HEADER, json={"name": "bob", "type": "USER"}
    )
    name, value, csrf = signed_in(browser_client, "bob")
    denied = browser_client.get(_URL, headers=session_headers(name, value, csrf))
    assert denied.status_code == 403
