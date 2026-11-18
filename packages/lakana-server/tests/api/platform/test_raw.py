"""Tests for `GET /api/v1/environments/{env}/raw`, the listing of `raw/` (#672)."""

from pathlib import Path

from fastapi.testclient import TestClient

from tests.api.platform.test_environments import _create
from tests.conftest import AUTH_HEADER
from tests.server_test_helpers import person

RAW = "/api/v1/environments/dev/raw"


def _lake(client: TestClient, tmp_path: Path) -> None:
    """Make `dev` with files under `raw/` and the external table `bronze.orders` on one folder.

    Parameters
    ----------
    client
        The test client.
    tmp_path
        Pytest's directory; `dev`'s storage is `tmp_path / "dev"`.
    """
    _create(client, tmp_path)
    for path, body in {
        "raw/orders/2026-11-01.csv": "id,amount\n1,2\n",
        "raw/returns/2026-11-01.csv": "id\n1\n",
    }.items():
        (tmp_path / "dev" / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / "dev" / path).write_text(body)
    registered = client.post(
        "/api/v1/environments/dev/namespaces/bronze/tables/external",
        json={"name": "orders", "location_pattern": "raw/orders/*.csv", "format": "csv"},
        headers=AUTH_HEADER,
    )
    assert registered.status_code == 201, registered.text


def test_lists_one_folder_with_claims_and_a_cursor(client: TestClient, tmp_path: Path) -> None:
    _lake(client, tmp_path)

    first = client.get(RAW, params={"limit": 1}, headers=AUTH_HEADER)

    assert first.status_code == 200, first.text
    body = first.json()
    assert body["path"] == "raw"
    assert body["items"] == [
        {"name": "orders", "kind": "folder", "size": None, "claimed_by": {"table": "bronze.orders"}}
    ]
    rest = client.get(RAW, params={"cursor": body["next_cursor"]}, headers=AUTH_HEADER).json()
    assert rest == {
        "path": "raw",
        "items": [{"name": "returns", "kind": "folder", "size": None, "claimed_by": None}],
        "next_cursor": None,
    }
    inside = client.get(RAW, params={"path": "raw/returns"}, headers=AUTH_HEADER).json()
    assert inside["items"] == [
        {"name": "2026-11-01.csv", "kind": "file", "size": 5, "claimed_by": None}
    ]


def test_who_may_not_register_an_external_table_is_refused(
    client: TestClient, tmp_path: Path
) -> None:
    _lake(client, tmp_path)
    viewer = person(client, "vic", "VIEWER")
    assert client.get(RAW, headers=viewer).status_code == 403
    engineer = person(client, "eve", "DATA_ENGINEER")
    assert client.get(RAW, headers=engineer).status_code == 200


def test_a_path_outside_raw_is_422_and_a_missing_environment_404(
    client: TestClient, tmp_path: Path
) -> None:
    _lake(client, tmp_path)
    refused = client.get(RAW, params={"path": "raw/../managed"}, headers=AUTH_HEADER)
    assert refused.status_code == 422
    assert "has an invalid part '..'" in refused.text
    assert client.get(RAW, params={"limit": 201}, headers=AUTH_HEADER).status_code == 422
    missing = client.get("/api/v1/environments/nowhere/raw", headers=AUTH_HEADER)
    assert missing.status_code == 404
