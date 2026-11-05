"""`lakana access export` and `lakana access plan` (#46)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from conftest import FakeApi, invoke, make_state

_PLAN: dict[str, Any] = {
    "file_name": "access.yaml",
    "file_sha256": "a" * 64,
    "manages": ["prod"],
    "creates": [
        {
            "op": "create",
            "entry": "grants[0]",
            "text": "grant role VIEWER on prod to group:finance",
        }
    ],
    "changed": [{"op": "change", "entry": "ci_trust[0]", "text": "change rule deploy-prod"}],
    "removals": [
        {"op": "remove", "entry": None, "text": "grant role ANALYST on prod to group:finance"}
    ],
    "releases": ["staging"],
    "drift": [
        {
            "scope": "prod",
            "text": "grant role VIEWER on prod to user:bob",
            "by": "carol",
            "reason": "incident",
            "at": "2026-11-01T10:00:00+00:00",
        },
        {
            "scope": "prod",
            "text": "grant role VIEWER on prod to user:dan",
            "by": None,
            "reason": None,
            "at": None,
        },
    ],
    "problems": [],
    "fingerprint": "f" * 64,
}


def _api(plan: dict[str, Any]) -> FakeApi:
    """Build a fake that answers the plan route with `plan`.

    Parameters
    ----------
    plan
        The `AccessPlanOut` body.

    Returns
    -------
    FakeApi
        The fake.
    """
    api = FakeApi()
    api.on("POST", "/api/v1/access/plan", json=plan)
    return api


def _file(tmp_path: Path, text: str = "version: 1\nmanages: [prod]\n") -> Path:
    """Write an access file.

    Parameters
    ----------
    tmp_path
        Pytest's per-test directory.
    text
        The content.

    Returns
    -------
    Path
        The file.
    """
    path = tmp_path / "access.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_plan_prints_each_change_and_the_fingerprint(tmp_path: Path) -> None:
    """`+`, `~`, `-` lines with their entry, then drift, releases and the fingerprint."""
    api = _api(_PLAN)
    result = invoke(make_state(tmp_path, api), "access", "plan", str(_file(tmp_path)))
    assert result.exit_code == 0, result.stderr
    lines = result.stdout.splitlines()
    assert "+ grant role VIEWER on prod to group:finance  (grants[0])" in lines
    assert "~ change rule deploy-prod  (ci_trust[0])" in lines
    assert "- grant role ANALYST on prod to group:finance" in lines
    assert (
        "! drift in prod: grant role VIEWER on prod to user:bob "
        "— carol, 2026-11-01T10:00:00+00:00: incident"
    ) in lines
    assert "! drift in prod: grant role VIEWER on prod to user:dan" in lines
    assert "stops managing staging" in lines
    assert lines[-1] == "Fingerprint: " + "f" * 64
    assert "No changes." not in lines


def test_plan_sends_the_file_name_and_text(tmp_path: Path) -> None:
    """The base name and the text go over; `--allow-lockout` sets the flag."""
    api = _api(_PLAN)
    invoke(make_state(tmp_path, api), "access", "plan", str(_file(tmp_path)), "--allow-lockout")
    body = json.loads(api.requests[-1].content)
    assert body == {
        "file_name": "access.yaml",
        "text": "version: 1\nmanages: [prod]\n",
        "allow_lockout": True,
    }


def test_plan_without_changes_says_so(tmp_path: Path) -> None:
    """An empty plan prints `No changes.`."""
    empty = {**_PLAN, "creates": [], "changed": [], "removals": [], "releases": [], "drift": []}
    result = invoke(make_state(tmp_path, _api(empty)), "access", "plan", str(_file(tmp_path)))
    assert result.exit_code == 0
    assert "No changes." in result.stdout.splitlines()


def test_plan_exits_1_on_problems_and_prints_them_all(tmp_path: Path) -> None:
    """Problems go to stderr, every line; the exit code is 1."""
    broken = {**_PLAN, "problems": ["access.yaml: grants[3]: no role", "access.yaml: x: bad"]}
    result = invoke(make_state(tmp_path, _api(broken)), "access", "plan", str(_file(tmp_path)))
    assert result.exit_code == 1
    assert "access.yaml: grants[3]: no role" in result.stderr
    assert "access.yaml: x: bad" in result.stderr


def test_plan_prints_json(tmp_path: Path) -> None:
    """`--output json` prints the plan as an object."""
    result = invoke(
        make_state(tmp_path, _api(_PLAN)),
        "--output",
        "json",
        "access",
        "plan",
        str(_file(tmp_path)),
    )
    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout)["fingerprint"] == "f" * 64


def test_plan_refuses_a_file_over_one_mib_before_sending(tmp_path: Path) -> None:
    """R8: the CLI checks the size first and names the limit."""
    big = tmp_path / "access.yaml"
    big.write_bytes(b"#" * (1024 * 1024 + 1))
    api = FakeApi()
    result = invoke(make_state(tmp_path, api), "access", "plan", str(big))
    assert result.exit_code == 1
    assert "1048576 bytes" in result.stderr
    assert api.requests == []


def test_plan_refuses_a_file_whose_request_body_exceeds_one_mib(tmp_path: Path) -> None:
    """Escaping grows the body past the server's limit, so the CLI checks the body (#46)."""
    big = tmp_path / "access.yaml"
    big.write_bytes(b"\\" * (700 * 1024))
    assert big.stat().st_size < 1024 * 1024
    api = FakeApi()
    result = invoke(make_state(tmp_path, api), "access", "plan", str(big))
    assert result.exit_code == 1
    stderr = " ".join(result.stderr.split())
    assert "1048576 bytes" in stderr
    assert "request" in stderr
    assert "--scope" in stderr
    assert api.requests == []


def test_plan_names_a_missing_or_unreadable_file(tmp_path: Path) -> None:
    """The message names the path and the next step."""
    result = invoke(make_state(tmp_path, FakeApi()), "access", "plan", str(tmp_path / "no.yaml"))
    assert result.exit_code == 1
    assert "no.yaml" in result.stderr
    assert "lakana access export" in result.stderr


def test_plan_refuses_a_file_that_is_not_utf8(tmp_path: Path) -> None:
    """Bytes that are not UTF-8 are named, not guessed."""
    bad = tmp_path / "access.yaml"
    bad.write_bytes(b"\xff\xfe")
    result = invoke(make_state(tmp_path, FakeApi()), "access", "plan", str(bad))
    assert result.exit_code == 1
    assert "UTF-8" in result.stderr


def test_export_prints_the_text_without_o(tmp_path: Path) -> None:
    """Without `-o` the text goes to stdout; scopes repeat."""
    api = FakeApi()
    api.on("GET", "/api/v1/access/export", json={"text": "version: 1\n"})
    result = invoke(
        make_state(tmp_path, api), "access", "export", "--scope", "prod", "--scope", "staging"
    )
    assert result.exit_code == 0, result.stderr
    assert result.stdout == "version: 1\n"
    assert api.requests[-1].url.query == b"scope=prod&scope=staging"


def test_export_writes_the_file_with_o(tmp_path: Path) -> None:
    """`-o` writes the text and says where."""
    api = FakeApi()
    api.on("GET", "/api/v1/access/export", json={"text": "version: 1\n"})
    target = tmp_path / "out.yaml"
    result = invoke(make_state(tmp_path, api), "access", "export", "-o", str(target))
    assert result.exit_code == 0, result.stderr
    assert target.read_text(encoding="utf-8") == "version: 1\n"
    assert "out.yaml" in result.stdout
    assert api.requests[-1].url.query == b"scope=instance"


def test_export_names_an_unwritable_target(tmp_path: Path) -> None:
    """A directory that does not exist is named."""
    api = FakeApi()
    api.on("GET", "/api/v1/access/export", json={"text": "x"})
    result = invoke(
        make_state(tmp_path, api), "access", "export", "-o", str(tmp_path / "no" / "out.yaml")
    )
    assert result.exit_code == 1
    assert "out.yaml" in result.stderr


def test_plan_json_with_problems_exits_1(tmp_path: Path) -> None:
    """The JSON plan is printed, and the exit code still says the file has problems."""
    broken = {**_PLAN, "problems": ["access.yaml: x: bad"]}
    result = invoke(
        make_state(tmp_path, _api(broken)),
        "--output",
        "json",
        "access",
        "plan",
        str(_file(tmp_path)),
    )
    assert result.exit_code == 1
    assert json.loads(result.stdout)["problems"] == ["access.yaml: x: bad"]
