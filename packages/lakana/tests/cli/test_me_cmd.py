"""`lakana me set`: the caller's own language."""

from __future__ import annotations

import json
from pathlib import Path

from conftest import FakeApi, invoke, make_state

_WHO = {"name": "admin", "type": "USER", "task_token": None, "ci_token": None}


def test_me_set_language_patches_whoami(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("PATCH", "/api/v1/whoami", json={**_WHO, "language": "fr"})
    result = invoke(make_state(tmp_path, api), "me", "set", "--language", "fr")
    assert result.exit_code == 0, result.stderr
    assert json.loads(api.requests[-1].content) == {"language": "fr"}
    assert "Your language is now fr" in result.stdout


def test_me_set_reset_sends_null(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("PATCH", "/api/v1/whoami", json={**_WHO, "language": None})
    result = invoke(make_state(tmp_path, api), "me", "set", "--reset")
    assert result.exit_code == 0, result.stderr
    assert json.loads(api.requests[-1].content) == {"language": None}
    assert "Your language is reset" in result.stdout


def test_me_set_needs_exactly_one_option(tmp_path: Path) -> None:
    for args in ((), ("--language", "fr", "--reset")):
        api = FakeApi()
        result = invoke(make_state(tmp_path, api), "me", "set", *args)
        assert result.exit_code != 0
        assert "Give exactly one of --language TAG and --reset." in result.stderr
        assert api.requests == []
