"""`lakana repo host-keys` and `lakana repo test`: repository checks a worker runs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import FakeApi, invoke, make_state

import lakana.cli.repos as repos_module
import lakana.resources.repositories as resource_module

_PROBES = "/api/v1/repository-probes"
_ID = "5b1c2f9e-0000-4000-8000-000000000001"
_URL = "git@github.com:team/analytics.git"
_ED = "AAAAC3NzaC1lZDI1NTE5AAAAIIuE9bmUom2wGn1ig3NKAYhOQ46XwTSXMg9/uXafUgYR"
_KEYS = [
    {
        "type": "ecdsa-sha2-nistp256",
        "fingerprint": "SHA256:ZFFwnf2D9BVErjTfYgAPmeOgTTOT8PDphFmKnKcpGCs",
        "line": "github.com ecdsa-sha2-nistp256 AAAAE2VjZHNh",
    },
    {
        "type": "ssh-ed25519",
        "fingerprint": "SHA256:BbeSiQQjnjpybq7nV8S8tEVnQiZNHfceXjKvrLVxecI",
        "line": f"github.com ssh-ed25519 {_ED}",
    },
    {
        "type": "ssh-rsa",
        "fingerprint": "SHA256:uNiVztksCsDhcc0u9e8BujQXVUpKZIDTMczCvj3tD2s",
        "line": "github.com ssh-rsa AAAAB3NzaC1yc2E",
    },
]


def _probe(
    state: str,
    outcome: str | None = None,
    message: str | None = None,
    keys: list[dict[str, str]] | None = None,
    kind: str = "host_keys",
) -> dict[str, Any]:
    """Build a `RepositoryProbeOut` body.

    Parameters
    ----------
    state
        Its state.
    outcome
        Its outcome.
    message
        Its message.
    keys
        Its host keys.
    kind
        Its kind.

    Returns
    -------
    dict
        The body.
    """
    return {
        "id": _ID,
        "kind": kind,
        "state": state,
        "outcome": outcome,
        "message": message,
        "host_keys": keys,
        "created_at": "2026-11-11T12:00:00+00:00",
        "finished_at": None if state != "done" else "2026-11-11T12:00:03+00:00",
    }


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Poll without waiting.

    Parameters
    ----------
    monkeypatch
        pytest's patcher.
    """
    monkeypatch.setattr(resource_module, "_sleep", lambda seconds: None)


def test_cli_host_keys_prints_fingerprints_and_saves_lines(tmp_path: Path) -> None:
    """The fingerprints are printed; `--save` writes the ed25519 line only, with the warning."""
    api = FakeApi()
    api.on("POST", _PROBES, status=202, json=_probe("queued"))
    api.on("GET", f"{_PROBES}/{_ID}", json=_probe("done", "ok", "The host offered 3 keys.", _KEYS))
    target = tmp_path / "known_hosts"

    result = invoke(make_state(tmp_path, api), "repo", "host-keys", _URL, "--save", str(target))

    assert result.exit_code == 0, result.output
    for key in _KEYS:
        assert key["fingerprint"] in result.stdout
        assert key["type"] in result.stdout
    assert json.loads(api.requests[0].content) == {"kind": "host_keys", "url": _URL}
    assert target.read_text() == f"github.com ssh-ed25519 {_ED}\n"
    assert (
        "Compare this fingerprint with your host's published fingerprints before you use the file."
        in result.stderr
    )
    assert "--known-hosts-file" in result.stderr


def test_cli_saves_only_the_ed25519_line_by_default(tmp_path: Path) -> None:
    """Without `--type` one key is saved; `--type` picks others, each named."""
    api = FakeApi()
    api.on("POST", _PROBES, status=202, json=_probe("queued"))
    api.on("GET", f"{_PROBES}/{_ID}", json=_probe("done", "ok", "found", _KEYS))
    target = tmp_path / "kh"

    chosen = invoke(
        make_state(tmp_path, api),
        "repo",
        "host-keys",
        _URL,
        "--save",
        str(target),
        "--type",
        "ssh-rsa",
        "--type",
        "ecdsa-sha2-nistp256",
    )

    assert chosen.exit_code == 0, chosen.output
    assert target.read_text().splitlines() == [_KEYS[0]["line"], _KEYS[2]["line"]]
    missing = invoke(
        make_state(tmp_path, api),
        "repo",
        "host-keys",
        _URL,
        "--save",
        str(tmp_path / "other"),
        "--type",
        "ecdsa-sha2-nistp521",
    )
    assert missing.exit_code == 2
    assert "ecdsa-sha2-nistp521" in missing.output
    assert not (tmp_path / "other").exists()


def test_cli_without_ed25519_saves_the_strongest_key_only(tmp_path: Path) -> None:
    """A host without an ed25519 key: the strongest one offered is saved, alone."""
    api = FakeApi()
    api.on("POST", _PROBES, status=202, json=_probe("queued"))
    api.on("GET", f"{_PROBES}/{_ID}", json=_probe("done", "ok", "found", [_KEYS[2], _KEYS[0]]))
    target = tmp_path / "kh"

    result = invoke(make_state(tmp_path, api), "repo", "host-keys", _URL, "--save", str(target))

    assert result.exit_code == 0, result.output
    assert target.read_text() == _KEYS[0]["line"] + "\n"


def test_cli_host_keys_that_fail_print_the_outcome_and_exit_1(tmp_path: Path) -> None:
    """A probe that did not find keys says why and what to do; nothing is saved."""
    api = FakeApi()
    api.on("POST", _PROBES, status=202, json=_probe("queued"))
    api.on(
        "GET", f"{_PROBES}/{_ID}", json=_probe("done", "refused_address", "Use the public name.")
    )
    target = tmp_path / "kh"

    result = invoke(make_state(tmp_path, api), "repo", "host-keys", _URL, "--save", str(target))

    assert result.exit_code == 1
    assert "refused_address: Use the public name." in result.stdout
    assert not target.exists()


def test_cli_host_keys_says_it_waits_after_a_second(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A probe still queued after a second prints one status line on standard error."""
    ticks = iter([0.0, 0.2, 1.5, 1.6, 1.7, 1.8])
    monkeypatch.setattr(repos_module, "_clock", lambda: next(ticks))
    answers = iter([_probe("queued"), _probe("running"), _probe("done", "ok", "found", _KEYS)])
    api = FakeApi()
    api.on("POST", _PROBES, status=202, json=_probe("queued"))
    api.routes["GET", f"{_PROBES}/{_ID}"] = lambda request: httpx.Response(200, json=next(answers))

    result = invoke(make_state(tmp_path, api), "repo", "host-keys", _URL)

    assert result.exit_code == 0, result.output
    assert result.stderr.count("Waiting for a worker") == 1


def test_cli_host_keys_that_time_out_say_how_to_look_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A probe never done within 60 s exits 1 naming the wait."""
    api = FakeApi()
    api.on("POST", _PROBES, status=202, json=_probe("queued"))
    api.on("GET", f"{_PROBES}/{_ID}", json=_probe("queued"))
    clock = iter(float(i) for i in range(0, 1000, 10))
    monkeypatch.setattr(resource_module, "_monotonic", lambda: next(clock))

    result = invoke(make_state(tmp_path, api), "repo", "host-keys", _URL)

    assert result.exit_code == 1
    assert "worker" in result.output
    assert len(api.requests) < 20


def test_cli_repo_test_prints_the_outcome(tmp_path: Path) -> None:
    """`repo test` queues an access check of NAME in ENV and prints what it found."""
    api = FakeApi()
    api.on("POST", _PROBES, status=202, json=_probe("queued", kind="access"))
    api.on(
        "GET",
        f"{_PROBES}/{_ID}",
        json=_probe("done", "auth_refused", "Add the public key.", kind="access"),
    )

    result = invoke(make_state(tmp_path, api), "repo", "test", "analytics", "--env", "prod")

    assert result.exit_code == 1
    assert "auth_refused: Add the public key." in result.stdout
    assert json.loads(api.requests[0].content) == {
        "kind": "access",
        "repository": "analytics",
        "environment": "prod",
    }


def test_cli_repo_test_ok_exits_0(tmp_path: Path) -> None:
    """An access check that read the repository prints ok."""
    api = FakeApi()
    api.on("POST", _PROBES, status=202, json=_probe("queued", kind="access"))
    api.on("GET", f"{_PROBES}/{_ID}", json=_probe("done", "ok", "Read it.", kind="access"))

    result = invoke(make_state(tmp_path, api), "repo", "test", "analytics", "--env", "prod")

    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == "ok: Read it."


def _ok_api() -> FakeApi:
    """Build an API whose host-key check finds the three keys.

    Returns
    -------
    FakeApi
        The API.
    """
    api = FakeApi()
    api.on("POST", _PROBES, status=202, json=_probe("queued"))
    api.on("GET", f"{_PROBES}/{_ID}", json=_probe("done", "ok", "found", _KEYS))
    return api


def test_save_refuses_an_existing_file_unless_forced(tmp_path: Path) -> None:
    """An existing FILE (your own known_hosts, say) is kept; `--force` replaces it."""
    target = tmp_path / "known_hosts"
    target.write_text("mine\n")
    api = _ok_api()

    refused = invoke(make_state(tmp_path, api), "repo", "host-keys", _URL, "--save", str(target))

    assert refused.exit_code == 2
    assert "already exists" in refused.output and "--force" in refused.output
    assert target.read_text() == "mine\n"
    assert api.requests == [], "refused before a worker is asked"
    forced = invoke(
        make_state(tmp_path, api), "repo", "host-keys", _URL, "--save", str(target), "--force"
    )
    assert forced.exit_code == 0, forced.output
    assert target.read_text() == f"github.com ssh-ed25519 {_ED}\n"


@pytest.mark.parametrize("force", [False, True])
def test_save_never_follows_a_symlink(tmp_path: Path, force: bool) -> None:
    """A symlink is refused even with `--force`; neither it nor its target changes.

    Parameters
    ----------
    tmp_path
        Pytest's directory.
    force
        Whether `--force` is given.
    """
    real = tmp_path / "real"
    real.write_text("mine\n")
    link = tmp_path / "link"
    link.symlink_to(real)
    dangling = tmp_path / "dangling"
    dangling.symlink_to(tmp_path / "nowhere")
    for path in (link, dangling):
        args = ["repo", "host-keys", _URL, "--save", str(path), *(["--force"] if force else [])]
        result = invoke(make_state(tmp_path, _ok_api()), *args)
        assert result.exit_code == 2
        assert "symbolic link" in result.output
    assert real.read_text() == "mine\n"
    assert not (tmp_path / "nowhere").exists()


def test_type_without_save_is_refused(tmp_path: Path) -> None:
    """`--type` picks what `--save` writes; alone it is refused, naming `--save`."""
    api = _ok_api()

    result = invoke(make_state(tmp_path, api), "repo", "host-keys", _URL, "--type", "ssh-rsa")

    assert result.exit_code == 2
    assert "--save" in result.output
    assert api.requests == []


def test_force_without_save_is_refused(tmp_path: Path) -> None:
    """`--force` alone is refused too."""
    result = invoke(make_state(tmp_path, _ok_api()), "repo", "host-keys", _URL, "--force")

    assert result.exit_code == 2
    assert "--save" in result.output


def test_cli_json_output_is_the_probe(tmp_path: Path) -> None:
    """With `--output json` the probe is printed as JSON."""
    api = FakeApi()
    api.on("POST", _PROBES, status=202, json=_probe("queued"))
    api.on("GET", f"{_PROBES}/{_ID}", json=_probe("done", "ok", "found", _KEYS))

    result = invoke(make_state(tmp_path, api), "--output", "json", "repo", "host-keys", _URL)

    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert body["outcome"] == "ok"
    assert [k["type"] for k in body["host_keys"]] == [k["type"] for k in _KEYS]
