"""Tests for `GET /api/v1/info`."""

from importlib.metadata import PackageNotFoundError, version

import pytest
from fastapi.testclient import TestClient

from lakana_server.api.platform import info as info_module
from lakana_server.app import create_app
from lakana_server.settings import AppendConfig, Settings
from tests.conftest import AUTH_HEADER


def test_info_reports_the_platform_version_from_package_metadata(client: TestClient) -> None:
    """An authenticated caller gets the version the server package was built with."""
    response = client.get("/api/v1/info", headers=AUTH_HEADER)

    assert response.status_code == 200, response.text
    assert response.json() == {
        "version": version("lakana-server"),
        "artifact_limits": {
            "max_files": 10_000,
            "max_file_bytes": 2 * 1024**3,
            "max_bytes": 10 * 1024**3,
        },
        "upload_max_bytes": 256 * 1024 * 1024,
    }


def test_info_is_open_to_a_task_token_caller_too(client: TestClient) -> None:
    """Any principal may ask, including the task token a Python task runs with."""
    minted = client.post(
        "/api/v1/task-tokens",
        headers=AUTH_HEADER,
        json={"principal": "admin", "environment": "dev", "asset_scope": [], "ttl_seconds": 300},
    )
    assert minted.status_code == 201, minted.text

    response = client.get(
        "/api/v1/info", headers={"Authorization": f"Bearer {minted.json()['token']}"}
    )

    assert response.status_code == 200, response.text


def test_info_requires_authentication(client: TestClient) -> None:
    """No credential is a 401: the version is not for anonymous callers."""
    assert client.get("/api/v1/info").status_code == 401


def test_healthz_still_reveals_nothing_about_the_version(client: TestClient) -> None:
    """The unauthenticated health check stays a bare status."""
    response = client.get("/healthz")

    assert response.json() == {"status": "ok"}
    assert version("lakana-server") not in response.text


def test_a_source_checkout_without_package_metadata_reports_an_unknown_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing(name: str) -> str:
        raise PackageNotFoundError(name)

    monkeypatch.setattr(info_module, "version", missing)
    assert info_module.platform_version() == "0+unknown"


def test_info_reports_the_upload_limit_the_operator_set(settings: Settings) -> None:
    """`LAKANA_APPEND__MAX_BYTES` reaches the client, so `lakana load` can check a file first."""
    capped = settings.model_copy(update={"append": AppendConfig(max_bytes=4096)})
    with TestClient(create_app(capped)) as client:
        response = client.get("/api/v1/info", headers=AUTH_HEADER)

    assert response.json()["upload_max_bytes"] == 4096
