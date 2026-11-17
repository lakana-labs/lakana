"""`lakana configuration`: the server's settings, secrets only as set or not set."""

from __future__ import annotations

import json
from pathlib import Path

from conftest import FakeApi, invoke, make_state

_ROWS = [
    {"name": "LAKANA_PORT", "value": 8000, "secret": False, "has_value": True, "source": "set"},
    {
        "name": "LAKANA_HOST",
        "value": "127.0.0.1",
        "secret": False,
        "has_value": True,
        "source": "default",
    },
    {
        "name": "LAKANA_UI__PUBLIC_URL",
        "value": None,
        "secret": False,
        "has_value": False,
        "source": "default",
    },
    {
        "name": "LAKANA_REPO_ROOTS",
        "value": ["/a"],
        "secret": False,
        "has_value": True,
        "source": "set",
    },
    {
        "name": "LAKANA_SECRETS__MASTER_KEY",
        "value": None,
        "secret": True,
        "has_value": True,
        "source": "set",
    },
    {
        "name": "LAKANA_RUN__INDEX_URL",
        "value": None,
        "secret": True,
        "has_value": False,
        "source": "default",
    },
]


def _api() -> FakeApi:
    api = FakeApi()
    api.on("GET", "/api/v1/settings/configuration", json=_ROWS)
    return api


def test_configuration_prints_secret_set_and_not_set(tmp_path: Path) -> None:
    result = invoke(make_state(tmp_path, _api()), "configuration")
    assert result.exit_code == 0, result.stderr
    out = " ".join(result.stdout.replace("│", " ").split())
    assert "LAKANA_SECRETS__MASTER_KEY (secret, set) set" in out
    assert "LAKANA_RUN__INDEX_URL (secret, not set) default" in out
    assert "LAKANA_PORT 8000 set" in out
    assert "LAKANA_HOST 127.0.0.1 default" in out
    assert "(not set)" in out
    assert "null" not in out
    assert '["/a"]' in out


def test_configuration_ends_with_the_restart_line(tmp_path: Path) -> None:
    result = invoke(make_state(tmp_path, _api()), "configuration")
    assert result.stderr.strip().endswith(
        "Values change in the server's .env file (or its environment) and take effect after a "
        "restart."
    )


def test_configuration_json_prints_the_rows_and_no_restart_line(tmp_path: Path) -> None:
    result = invoke(make_state(tmp_path, _api()), "--output", "json", "configuration")
    assert next(row["name"] for row in json.loads(result.stdout)) == "LAKANA_PORT"
    assert "restart" not in result.stderr
