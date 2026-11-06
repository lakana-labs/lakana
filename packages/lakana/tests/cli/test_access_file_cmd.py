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
            "text": "grant role VIEWER on environment prod to group:finance",
        }
    ],
    "changed": [{"op": "change", "entry": "ci_trust[0]", "text": "change rule deploy-prod"}],
    "removals": [
        {
            "op": "remove",
            "entry": None,
            "text": "grant role ANALYST on environment prod to group:finance",
        }
    ],
    "releases": ["staging"],
    "starts": ["dev"],
    "drift": [
        {
            "scope": "prod",
            "text": "grant role VIEWER on environment prod to user:bob",
            "by": "carol",
            "reason": "incident",
            "at": "2026-11-01T10:00:00+00:00",
        },
        {
            "scope": "prod",
            "text": "grant role VIEWER on environment prod to user:dan",
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
    assert "+ grant role VIEWER on environment prod to group:finance  (grants[0])" in lines
    assert "~ change rule deploy-prod  (ci_trust[0])" in lines
    assert "- grant role ANALYST on environment prod to group:finance" in lines
    assert (
        "! drift in prod: grant role VIEWER on environment prod to user:bob "
        "— carol, 2026-11-01T10:00:00+00:00: incident"
    ) in lines
    assert "! drift in prod: grant role VIEWER on environment prod to user:dan" in lines
    assert "stops managing staging" in lines
    assert "starts managing dev" in lines
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
    empty = {
        **_PLAN,
        "creates": [],
        "changed": [],
        "removals": [],
        "releases": [],
        "starts": [],
        "drift": [],
    }
    result = invoke(make_state(tmp_path, _api(empty)), "access", "plan", str(_file(tmp_path)))
    assert result.exit_code == 0
    assert "No changes." in result.stdout.splitlines()


def test_plan_that_only_starts_managing_is_not_no_changes(tmp_path: Path) -> None:
    """A plan with nothing but `starts` prints the line, not `No changes.`."""
    only = {**_PLAN, "creates": [], "changed": [], "removals": [], "releases": [], "drift": []}
    result = invoke(make_state(tmp_path, _api(only)), "access", "plan", str(_file(tmp_path)))
    assert "starts managing dev" in result.stdout.splitlines()
    assert "No changes." not in result.stdout.splitlines()


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


_APPLIED: dict[str, Any] = {
    "apply_id": "6b0f4c1e-5d7a-4a43-8d5e-0a7f1c2d3e4f",
    "file_sha256": "b" * 64,
    "source_commit": "0123abc",
    "created": 1,
    "changed": 0,
    "removed": 1,
    "released": [],
    "applied_at": "2026-11-06T09:30:00+00:00",
}

_APPLY = "/api/v1/access/apply"
_PLAN_ROUTE = "/api/v1/access/plan"


def _apply_api(plan: dict[str, Any] = _PLAN, applied: dict[str, Any] = _APPLIED) -> FakeApi:
    """Build a fake that answers plan and apply.

    Parameters
    ----------
    plan
        The plan body.
    applied
        The apply body.

    Returns
    -------
    FakeApi
        The fake.
    """
    api = FakeApi()
    api.on("POST", _PLAN_ROUTE, json=plan)
    api.on("POST", _APPLY, json=applied)
    return api


def _sent(api: FakeApi, path: str) -> list[dict[str, Any]]:
    """Return the JSON bodies posted to `path`.

    Parameters
    ----------
    api
        The fake.
    path
        The request path.

    Returns
    -------
    list of dict
        The bodies, oldest first.
    """
    return [json.loads(r.content) for r in api.requests if r.url.path == path]


def test_apply_without_a_fingerprint_plans_asks_and_applies_with_it(tmp_path: Path) -> None:
    """Spec 4: plan shown, confirmation read, apply sent with the plan's fingerprint."""
    api = _apply_api()
    result = invoke(
        make_state(tmp_path, api, interactive=True),
        "access",
        "apply",
        str(_file(tmp_path)),
        input="y\n",
    )
    assert result.exit_code == 0, result.stderr
    assert _sent(api, _APPLY)[0]["plan_fingerprint"] == "f" * 64
    assert "+ grant role VIEWER on environment prod to group:finance" in result.stdout
    assert "Apply these changes?" in result.stdout
    assert "Applied: 1 created, 0 changed, 1 removed." in result.stdout


def test_apply_declined_changes_nothing(tmp_path: Path) -> None:
    """Answering no sends no apply."""
    api = _apply_api()
    result = invoke(
        make_state(tmp_path, api, interactive=True),
        "access",
        "apply",
        str(_file(tmp_path)),
        input="n\n",
    )
    assert result.exit_code != 0
    assert _sent(api, _APPLY) == []


def test_apply_refuses_to_ask_without_a_terminal(tmp_path: Path) -> None:
    """No --yes and no terminal: exit 1, nothing applied, the message names --yes."""
    api = _apply_api()
    result = invoke(make_state(tmp_path, api), "access", "apply", str(_file(tmp_path)))
    assert result.exit_code == 1
    assert "--yes" in result.stderr
    assert "--plan-fingerprint" in result.stderr
    assert _sent(api, _APPLY) == []


def test_apply_with_yes_and_a_fingerprint_sends_at_once(tmp_path: Path) -> None:
    """CI: no plan request, no prompt; the commit and the file name go along."""
    api = _apply_api()
    result = invoke(
        make_state(tmp_path, api),
        "access",
        "apply",
        str(_file(tmp_path)),
        "--yes",
        "--plan-fingerprint",
        "e" * 64,
        "--source-commit",
        "0123abc",
        "--allow-lockout",
    )
    assert result.exit_code == 0, result.stderr
    assert _sent(api, _PLAN_ROUTE) == []
    body = _sent(api, _APPLY)[0]
    assert body["plan_fingerprint"] == "e" * 64
    assert body["source_commit"] == "0123abc"
    assert body["file_name"] == "access.yaml"
    assert body["allow_lockout"] is True


def test_apply_with_yes_alone_plans_then_applies(tmp_path: Path) -> None:
    """`--yes` skips the question, not the plan."""
    api = _apply_api()
    result = invoke(make_state(tmp_path, api), "access", "apply", str(_file(tmp_path)), "--yes")
    assert result.exit_code == 0, result.stderr
    assert _sent(api, _APPLY)[0]["plan_fingerprint"] == "f" * 64


def test_apply_with_problems_prints_them_and_applies_nothing(tmp_path: Path) -> None:
    """The plan's problems stop the apply before any prompt."""
    broken = {**_PLAN, "problems": ["access.yaml: x: bad"]}
    api = _apply_api(broken)
    result = invoke(make_state(tmp_path, api), "access", "apply", str(_file(tmp_path)), "--yes")
    assert result.exit_code == 1
    assert "access.yaml: x: bad" in result.stderr
    assert _sent(api, _APPLY) == []


def test_apply_says_what_it_stopped_managing(tmp_path: Path) -> None:
    """Released scopes are named after the counts."""
    api = _apply_api(applied={**_APPLIED, "released": ["staging"]})
    result = invoke(make_state(tmp_path, api), "access", "apply", str(_file(tmp_path)), "--yes")
    assert "Stopped managing: staging" in result.stdout


def test_a_stale_fingerprint_says_to_plan_again(tmp_path: Path) -> None:
    """The 409 text is passed through, with the CLI's conflict exit code (7)."""
    api = FakeApi()
    api.on(
        "POST",
        _APPLY,
        status=409,
        json={"detail": "access changed since your plan; nothing was applied. Run plan again"},
    )
    result = invoke(
        make_state(tmp_path, api),
        "access",
        "apply",
        str(_file(tmp_path)),
        "--yes",
        "--plan-fingerprint",
        "e" * 64,
    )
    assert result.exit_code == 7
    assert "plan again" in result.stderr


def test_apply_prints_json(tmp_path: Path) -> None:
    """`--output json` prints the result as an object."""
    result = invoke(
        make_state(tmp_path, _apply_api()),
        "--output",
        "json",
        "access",
        "apply",
        str(_file(tmp_path)),
        "--yes",
    )
    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout)["created"] == 1


def test_apply_refuses_a_file_over_one_mib_before_sending(tmp_path: Path) -> None:
    """R8 again: nothing is planned or applied."""
    big = tmp_path / "access.yaml"
    big.write_bytes(b"#" * (1024 * 1024 + 1))
    api = _apply_api()
    result = invoke(make_state(tmp_path, api), "access", "apply", str(big), "--yes")
    assert result.exit_code == 1
    assert api.requests == []


def test_status_lists_scopes_and_overrides(tmp_path: Path) -> None:
    """Scope, file, commit, who, when; then the open overrides with their reasons."""
    api = FakeApi()
    api.on(
        "GET",
        "/api/v1/access/status",
        json={
            "scopes": [
                {
                    "scope": "prod",
                    "file_name": "access.yaml",
                    "file_sha256": "c" * 64,
                    "source_commit": "0123abc",
                    "applied_by": "access-admin",
                    "applied_at": "2026-11-06T09:30:00+00:00",
                }
            ],
            "overrides": [
                {
                    "scope": "prod",
                    "change": "grant VIEWER to bob",
                    "reason": "Incident 42 hotfix",
                    "by": "carol",
                    "at": "2026-11-06T10:00:00+00:00",
                }
            ],
        },
    )
    result = invoke(make_state(tmp_path, api), "access", "status")
    assert result.exit_code == 0, result.stderr
    out = " ".join(result.stdout.split())
    header = result.stdout.splitlines()[1]
    assert [cell.strip() for cell in header.strip("┃").split("┃")] == [
        "scope",
        "file",
        "commit",
        "applied by",
        "applied at",
    ]
    for text in ("prod", "access.yaml", "0123abc", "access-admin", "2026-11-06"):
        assert text in out
    assert "Open overrides (the next apply reverts them unless the file lists them):" in out
    assert "grant VIEWER to bob" in out
    assert "Incident 42 hotfix" in out


def test_apply_help_says_a_fingerprint_applies_without_asking(tmp_path: Path) -> None:
    """The option text names the one case where the command asks."""
    result = invoke(make_state(tmp_path, FakeApi()), "access", "apply", "--help")
    text = " ".join(result.stdout.split())
    assert "asks only when it plans" in text


def test_status_without_scopes_says_how_to_start(tmp_path: Path) -> None:
    """Nothing managed: the message names the first command."""
    api = FakeApi()
    api.on("GET", "/api/v1/access/status", json={"scopes": [], "overrides": []})
    result = invoke(make_state(tmp_path, api), "access", "status")
    assert result.exit_code == 0, result.stderr
    assert "No scope is managed by an access file" in result.stdout
    assert "lakana access export" in result.stdout
    assert "Open overrides" not in result.stdout


def test_status_prints_json(tmp_path: Path) -> None:
    """`--output json` prints both lists."""
    api = FakeApi()
    api.on("GET", "/api/v1/access/status", json={"scopes": [], "overrides": []})
    result = invoke(make_state(tmp_path, api), "--output", "json", "access", "status")
    assert json.loads(result.stdout) == {"scopes": [], "overrides": []}
