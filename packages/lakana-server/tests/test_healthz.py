"""Tests for the unauthenticated health endpoint."""

from typing import Any

from fastapi.testclient import TestClient


def test_healthz_needs_no_credentials(client: TestClient) -> None:
    """`GET /healthz` answers 200 with no Authorization header."""
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_healthz_is_not_part_of_the_platform_api(openapi_schema: dict[str, Any]) -> None:
    """It lives outside /api/v1, so the SDK route contract doesn't cover it."""
    paths = openapi_schema["paths"]

    assert "/healthz" not in paths
