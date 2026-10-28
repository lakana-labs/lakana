"""`lakana ci-trust *`."""

from __future__ import annotations

import json
from pathlib import Path

from conftest import FakeApi, invoke, make_state

_BASE = "/api/v1/ci-trust-rules"
_ID = "00000000-0000-0000-0000-000000000001"
_RULE = {
    "id": _ID,
    "issuer": "https://token.actions.githubusercontent.com",
    "principal": "deployer",
    "environment": "prod",
    "repository": "analytics",
    "kinds": ["deploy"],
    "conditions": {"repository_id": "123456", "ref": "refs/heads/main"},
    "enabled": True,
    "created_by": "admin",
    "created_at": "2026-10-20T12:00:00+00:00",
    "updated_by": None,
    "updated_at": "2026-10-20T12:00:00+00:00",
}
_REPORT = {
    "issuer": _RULE["issuer"],
    "environment": "prod",
    "repository": "analytics",
    "verdict": "refused: no enabled rule for this issuer, environment and repository matches",
    "matching": [],
    "rules": [
        {
            "rule_id": _ID,
            "matches": False,
            "reasons": ["claim ref is 'refs/heads/dev'; the rule needs 'refs/heads/main'"],
        }
    ],
}


def test_create_sends_the_rule_and_says_what_to_check(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("POST", _BASE, status=201, json=_RULE)
    result = invoke(
        make_state(tmp_path, api),
        "ci-trust",
        "create",
        "--issuer",
        "github",
        "--env",
        "prod",
        "--repo",
        "analytics",
        "--principal",
        "deployer",
        "--claim",
        "repository_id=123456",
        "--claim",
        "ref=refs/heads/main",
    )
    assert result.exit_code == 0, result.output
    (posted,) = api.requests
    assert json.loads(posted.content) == {
        "issuer": "github",
        "principal": "deployer",
        "environment": "prod",
        "repository": "analytics",
        "kinds": ["deploy"],
        "conditions": {"repository_id": "123456", "ref": "refs/heads/main"},
    }
    assert f"Created CI trust rule {_ID}" in result.stdout
    assert "lakana access explain deployer --scope prod" in result.stdout


def test_create_takes_both_kinds_and_refuses_a_claim_without_a_value(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("POST", _BASE, status=201, json={**_RULE, "kinds": ["deploy", "dry_run"]})
    args = ["ci-trust", "create", "--issuer", "github", "--env", "prod", "--repo", "analytics"]
    args += ["--principal", "deployer", "--claim", "repository_id=1", "--claim", "ref=x"]
    result = invoke(make_state(tmp_path, api), *args, "--kind", "deploy", "--kind", "dry_run")
    assert result.exit_code == 0, result.output
    assert json.loads(api.requests[0].content)["kinds"] == ["deploy", "dry_run"]
    refused = invoke(make_state(tmp_path, FakeApi()), *args[:-2], "--claim", "ref")
    assert refused.exit_code == 2
    assert "--claim expects key=value" in refused.output


def test_list_show_enable_disable_delete(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("GET", _BASE, json=[_RULE])
    api.on("GET", f"{_BASE}/{_ID}", json=_RULE)
    api.on("POST", f"{_BASE}/{_ID}/enable", json=_RULE)
    api.on("POST", f"{_BASE}/{_ID}/disable", json={**_RULE, "enabled": False})
    api.on("DELETE", f"{_BASE}/{_ID}", status=204)
    state = make_state(tmp_path, api)
    listed = invoke(state, "ci-trust", "list")
    assert listed.exit_code == 0, listed.output
    assert "analytics" in listed.stdout
    shown = invoke(state, "ci-trust", "show", _ID)
    assert "refs/heads/main" in shown.stdout
    assert "Enabled CI trust rule" in invoke(state, "ci-trust", "enable", _ID).stdout
    assert "no longer work" in invoke(state, "ci-trust", "disable", _ID).stdout
    deleted = invoke(state, "ci-trust", "delete", _ID, "--yes")
    assert deleted.exit_code == 0, deleted.output
    assert "Deleted CI trust rule" in deleted.stdout
    refused = invoke(make_state(tmp_path, FakeApi()), "ci-trust", "delete", _ID)
    assert refused.exit_code == 2
    assert "--yes" in refused.output


def test_explain_reads_claims_from_a_file_or_stdin(tmp_path: Path) -> None:
    claims = {"iss": _RULE["issuer"], "repository_id": "123456", "ref": "refs/heads/dev"}
    path = tmp_path / "claims.json"
    path.write_text(json.dumps(claims), encoding="utf-8")
    api = FakeApi()
    api.on("POST", f"{_BASE}/explain", json=_REPORT)
    state = make_state(tmp_path, api)
    result = invoke(
        state, "ci-trust", "explain", "--claims", str(path), "--env", "prod", "--repo", "analytics"
    )
    assert result.exit_code == 0, result.output
    assert json.loads(api.requests[0].content) == {
        "claims": claims,
        "environment": "prod",
        "repository": "analytics",
    }
    assert "Verdict: refused" in result.stdout
    assert "claim ref is 'refs/heads/dev'" in result.stdout
    piped = invoke(
        state,
        "ci-trust",
        "explain",
        "--claims",
        "-",
        "--env",
        "prod",
        "--repo",
        "analytics",
        "--issuer",
        "github",
        input=json.dumps(claims),
    )
    assert piped.exit_code == 0, piped.output
    assert json.loads(api.requests[1].content)["issuer"] == "github"


def test_explain_shows_a_match_json_and_no_rules(tmp_path: Path) -> None:
    accepted = {
        **_REPORT,
        "verdict": f"accepted by rule {_ID}",
        "matching": [_ID],
        "rules": [{"rule_id": _ID, "matches": True, "reasons": []}],
    }
    api = FakeApi()
    api.on("POST", f"{_BASE}/explain", json=accepted)
    path = tmp_path / "claims.json"
    path.write_text("{}", encoding="utf-8")
    args = ["ci-trust", "explain", "--claims", str(path), "--env", "prod", "--repo", "analytics"]
    state = make_state(tmp_path, api)
    assert f"{_ID}: matches" in invoke(state, *args).stdout
    as_json = invoke(state, "--output", "json", *args)
    assert json.loads(as_json.stdout)["matching"] == [_ID]
    empty = FakeApi()
    empty.on("POST", f"{_BASE}/explain", json={**_REPORT, "rules": []})
    assert "no rules" in invoke(make_state(tmp_path, empty), *args).stdout


def test_explain_refuses_claims_that_are_not_an_object(tmp_path: Path) -> None:
    for content in ("eyJhbGciOi.payload.sig", "[1]", "x" * (64 * 1024 + 1)):
        path = tmp_path / "claims.json"
        path.write_text(content, encoding="utf-8")
        result = invoke(
            make_state(tmp_path, FakeApi()),
            "ci-trust",
            "explain",
            "--claims",
            str(path),
            "--env",
            "prod",
            "--repo",
            "analytics",
        )
        assert result.exit_code == 2
    missing = invoke(
        make_state(tmp_path, FakeApi()),
        "ci-trust",
        "explain",
        "--claims",
        str(tmp_path / "nope.json"),
        "--env",
        "prod",
        "--repo",
        "analytics",
    )
    assert missing.exit_code == 2
    assert "Cannot read" in missing.output


def test_update_sends_only_what_changes(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("PATCH", f"{_BASE}/{_ID}", json=_RULE)
    state = make_state(tmp_path, api)
    result = invoke(
        state,
        "ci-trust",
        "update",
        _ID,
        "--claim",
        "repository_id=1",
        "--claim",
        "environment=prod",
    )
    assert result.exit_code == 0, result.output
    assert json.loads(api.requests[0].content) == {
        "conditions": {"repository_id": "1", "environment": "prod"}
    }
    invoke(state, "ci-trust", "update", _ID, "--principal", "other", "--kind", "dry_run")
    assert json.loads(api.requests[1].content) == {"principal": "other", "kinds": ["dry_run"]}
    nothing = invoke(make_state(tmp_path, FakeApi()), "ci-trust", "update", _ID)
    assert nothing.exit_code == 2
    assert "Nothing to change" in nothing.output


def test_start_on_deploy_jobs_is_sent_only_when_asked_for(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("POST", _BASE, status=201, json={**_RULE, "start_on_deploy_jobs": True})
    api.on("PATCH", f"{_BASE}/{_ID}", json=_RULE)
    state = make_state(tmp_path, api)
    created = invoke(
        state,
        "ci-trust",
        "create",
        "--issuer",
        "github",
        "--env",
        "prod",
        "--repo",
        "analytics",
        "--principal",
        "deployer",
        "--claim",
        "repository_id=123456",
        "--claim",
        "ref=refs/heads/main",
        "--start-on-deploy-jobs",
    )
    assert created.exit_code == 0, created.output
    assert json.loads(api.requests[0].content)["start_on_deploy_jobs"] is True
    assert "starts the deploy's on_deploy jobs" in created.stdout.replace("\n", " ")
    invoke(state, "ci-trust", "update", _ID, "--no-start-on-deploy-jobs")
    assert json.loads(api.requests[1].content) == {"start_on_deploy_jobs": False}
    invoke(state, "ci-trust", "update", _ID, "--start-on-deploy-jobs")
    assert json.loads(api.requests[2].content) == {"start_on_deploy_jobs": True}
