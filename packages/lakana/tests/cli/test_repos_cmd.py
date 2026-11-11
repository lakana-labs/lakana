"""`lakana repo *`."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from conftest import FakeApi, invoke, make_state

_REPO = {
    "id": "00000000-0000-0000-0000-000000000001",
    "name": "analytics",
    "kind": "https",
    "url": "https://github.com/acme/analytics.git",
    "credential_secret": "github-token",
    "credential_username": None,
    "ssh_known_hosts": None,
    "default_ref": "main",
    "created_by": None,
    "created_at": "2026-10-01T12:00:00+00:00",
    "updated_at": "2026-10-01T12:00:00+00:00",
}


def _plain(text: str) -> str:
    """Return CLI output without ANSI codes or Rich box borders, whitespace collapsed.

    Parameters
    ----------
    text
        Raw stdout or stderr.

    Returns
    -------
    str
        The plain, single-spaced text.
    """
    plain = re.sub(r"\x1b\[[0-9;]*m", "", text)
    for border in "│╭╮╰╯─":
        plain = plain.replace(border, " ")
    return " ".join(plain.split())


def test_repo_add_sends_every_option_and_reads_known_hosts_from_a_file(tmp_path: Path) -> None:
    """`--known-hosts-file` is read client-side; the body carries its text."""
    api = FakeApi()
    api.on("POST", "/api/v1/repositories", status=201, json={**_REPO, "kind": "ssh"})
    known = tmp_path / "known_hosts"
    known.write_text("github.com ssh-ed25519 AAAA\n")
    result = invoke(
        make_state(tmp_path, api),
        "repo",
        "add",
        "analytics",
        "--kind",
        "ssh",
        "--url",
        "git@github.com:acme/analytics.git",
        "--credential-secret",
        "deploy-key",
        "--known-hosts-file",
        str(known),
        "--default-ref",
        "main",
    )
    assert result.exit_code == 0, result.output
    body = json.loads(api.requests[-1].content)
    assert body == {
        "name": "analytics",
        "kind": "ssh",
        "url": "git@github.com:acme/analytics.git",
        "credential_secret": "deploy-key",
        "credential_username": None,
        "ssh_known_hosts": "github.com ssh-ed25519 AAAA\n",
        "default_ref": "main",
    }


def test_repo_add_local_makes_a_relative_path_absolute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--url .` for a local repository becomes the absolute path."""
    api = FakeApi()
    api.on("POST", "/api/v1/repositories", status=201, json={**_REPO, "kind": "local"})
    monkeypatch.chdir(tmp_path)
    invoke(make_state(tmp_path, api), "repo", "add", "demo", "--kind", "local", "--url", ".")
    assert json.loads(api.requests[-1].content)["url"] == str(tmp_path.resolve())


def test_repo_list_and_show_render_tables(tmp_path: Path) -> None:
    """List shows the key columns; show shows one repository."""
    api = FakeApi()
    api.on("GET", "/api/v1/repositories", json=[_REPO])
    api.on("GET", "/api/v1/repositories/analytics", json=_REPO)
    state = make_state(tmp_path, api)
    listed = _plain(invoke(state, "repo", "list", env={"GITHUB_ACTIONS": "true"}).stdout)
    assert "analytics" in listed and "https" in listed and "github-token" in listed
    shown = _plain(
        invoke(state, "repo", "show", "analytics", env={"GITHUB_ACTIONS": "true"}).stdout
    )
    assert "https://github.com/acme/analytics.git" in shown


def test_repo_update_sends_only_given_fields_and_clears(tmp_path: Path) -> None:
    """`--clear default_ref` sends null; untouched fields are absent."""
    api = FakeApi()
    api.on("PATCH", "/api/v1/repositories/analytics", json=_REPO)
    result = invoke(
        make_state(tmp_path, api),
        "repo",
        "update",
        "analytics",
        "--credential-username",
        "gitlab+deploy",
        "--clear",
        "default_ref",
    )
    assert result.exit_code == 0, result.output
    assert json.loads(api.requests[-1].content) == {
        "credential_username": "gitlab+deploy",
        "default_ref": None,
    }


def test_repo_update_with_nothing_to_change_is_a_usage_error(tmp_path: Path) -> None:
    """No option at all exits 2 without calling the API."""
    api = FakeApi()
    result = invoke(make_state(tmp_path, api), "repo", "update", "analytics")
    assert result.exit_code == 2
    assert api.requests == []


def test_repo_remove_needs_confirmation(tmp_path: Path) -> None:
    """Without a TTY, `--yes` is required."""
    api = FakeApi()
    api.on("DELETE", "/api/v1/repositories/analytics", status=204)
    state = make_state(tmp_path, api)
    assert invoke(state, "repo", "remove", "analytics").exit_code == 2
    assert invoke(state, "repo", "remove", "analytics", "--yes").exit_code == 0


def test_repo_add_unreadable_known_hosts_file_is_a_usage_error(tmp_path: Path) -> None:
    """A `--known-hosts-file` that cannot be read exits 2 without calling the API."""
    api = FakeApi()
    result = invoke(
        make_state(tmp_path, api),
        "repo",
        "add",
        "infra",
        "--kind",
        "ssh",
        "--url",
        "git@h:a/b.git",
        "--known-hosts-file",
        str(tmp_path / "missing"),
    )
    assert result.exit_code == 2
    assert "Can't read" in _plain(result.output)
    assert api.requests == []


def test_repo_update_clear_rejects_unknown_and_url(tmp_path: Path) -> None:
    """`--clear` accepts only clearable fields; `url` and typos exit 2."""
    api = FakeApi()
    state = make_state(tmp_path, api)
    for field in ("url", "bogus"):
        result = invoke(state, "repo", "update", "analytics", "--clear", field)
        assert result.exit_code == 2
        assert "--clear accepts" in _plain(result.output)
    assert api.requests == []


def test_repo_update_reads_known_hosts_file(tmp_path: Path) -> None:
    """`--known-hosts-file` on update sends the file's text."""
    api = FakeApi()
    api.on("PATCH", "/api/v1/repositories/infra", json=_REPO)
    known = tmp_path / "kh"
    known.write_text("h ssh-ed25519 BBBB\n")
    result = invoke(
        make_state(tmp_path, api), "repo", "update", "infra", "--known-hosts-file", str(known)
    )
    assert result.exit_code == 0, result.output
    assert json.loads(api.requests[-1].content) == {"ssh_known_hosts": "h ssh-ed25519 BBBB\n"}


def test_repo_update_refuses_clearing_and_setting_the_same_field(tmp_path: Path) -> None:
    """`--clear default_ref --default-ref x` is a usage error, not a silent clear."""
    api = FakeApi()
    result = invoke(
        make_state(tmp_path, api),
        "repo",
        "update",
        "analytics",
        "--default-ref",
        "x",
        "--clear",
        "default_ref",
        env={"GITHUB_ACTIONS": "true"},
    )
    assert result.exit_code == 2
    assert "default_ref" in _plain(result.output)
    assert "both" in _plain(result.output)
    assert api.requests == []


def test_sdk_update_refuses_an_unknown_field_before_asking(tmp_path: Path) -> None:
    """A field a repository cannot change is a `TypeError` naming it; nothing is sent."""
    api = FakeApi()

    with pytest.raises(TypeError, match="unexpected field\\(s\\): kind"):
        make_state(tmp_path, api).client().repositories.update("analytics", kind="ssh")
    assert api.requests == []
