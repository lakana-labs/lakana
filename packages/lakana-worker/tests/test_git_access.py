"""`check_access`: "Test access" runs `git ls-remote` with the fetch's hardening (Task 5)."""

from __future__ import annotations

import logging
import socket
import subprocess
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest
from worker_git_helpers import GitHttpServer, bare_copy, make_repo

import lakana_worker.git as module
from lakana_core.models import ProbeOutcome, RepositoryKind
from lakana_core.projects.limits import DeployLimits
from lakana_worker.git import (
    FAILED_MESSAGE,
    HOST_KEY_CHANGED_MESSAGE,
    GitCredential,
    check_access,
)

TOKEN = "s3cret-token-value"  # nosec B105 -- the test server's token
SSH_URL = "git@git.example.com:team/analytics.git"
KNOWN_HOSTS = (
    "git.example.com ssh-ed25519 "
    "AAAAC3NzaC1lZDI1NTE5AAAAIIuE9bmUom2wGn1ig3NKAYhOQ46XwTSXMg9/uXafUgYR\n"
)
PRIVATE_KEY = (
    "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAA\n-----END OPENSSH "
    "PRIVATE KEY-----"
)


def resolves_to(*addresses: str) -> Callable[..., list[Any]]:
    """Build a stand-in for `socket.getaddrinfo` that answers with fixed addresses.

    Parameters
    ----------
    *addresses
        The addresses.

    Returns
    -------
    callable
        The resolver; it records nothing.
    """

    def resolve(host: str, port: object, *args: object, **kwargs: object) -> list[Any]:
        family = socket.AF_INET6 if ":" in addresses[0] else socket.AF_INET
        return [(family, socket.SOCK_STREAM, 6, "", (a, 0)) for a in addresses]

    return resolve


PUBLIC = resolves_to("203.0.113.7")


def _check(
    tmp_path: Path,
    *,
    kind: RepositoryKind = RepositoryKind.SSH,
    url: str = SSH_URL,
    credential: GitCredential | None = None,
    known_hosts: str | None = KNOWN_HOSTS,
    deadline: float | None = None,
    ca_bundle: Path | None = None,
    resolve: Callable[..., list[Any]] = PUBLIC,
    repo_roots: Sequence[str] = (),
    https_proxy: str | None = None,
) -> tuple[ProbeOutcome, str]:
    """Call `check_access` with test defaults.

    Parameters
    ----------
    tmp_path
        Pytest's directory; `work/` inside it is the private work directory.
    kind
        The repository's kind.
    url
        Its URL.
    credential
        Its credential.
    known_hosts
        Its known_hosts lines.
    deadline
        The monotonic deadline; 60 s from now by default.
    ca_bundle
        A CA file for HTTPS.
    resolve
        The resolver.
    repo_roots
        The directories a local repository may be under; none by default.
    https_proxy
        The proxy for HTTPS, if any.

    Returns
    -------
    tuple
        The outcome and message.
    """
    work = tmp_path / "work"
    work.mkdir(mode=0o700, exist_ok=True)
    return check_access(
        kind=kind,
        url=url,
        credential=credential,
        known_hosts=known_hosts,
        workdir=work,
        limits=DeployLimits(),
        repo_roots=list(repo_roots),
        deadline=deadline if deadline is not None else time.monotonic() + 60,
        ca_bundle=ca_bundle,
        https_proxy=https_proxy,
        resolve=resolve,
    )


def _fake_runs(
    monkeypatch: pytest.MonkeyPatch, *, returncode: int = 0, stderr: str = ""
) -> list[tuple[list[str], dict[str, str]]]:
    """Replace `_run`: `git init` succeeds, `ls-remote` answers as given.

    Parameters
    ----------
    monkeypatch
        pytest's patcher.
    returncode
        `ls-remote`'s exit status.
    stderr
        `ls-remote`'s standard error.

    Returns
    -------
    list
        ``(argv, env)`` of every call.
    """
    calls: list[tuple[list[str], dict[str, str]]] = []

    def fake(
        args: Sequence[str],
        *,
        cwd: Path,
        env: dict[str, str],
        timeout: float,
        max_bytes: int | None = None,
    ) -> subprocess.CompletedProcess[str]:
        calls.append((list(args), dict(env)))
        if args[1] == "init":
            return subprocess.CompletedProcess(list(args), 0, "", "")
        return subprocess.CompletedProcess(list(args), returncode, "", stderr)

    monkeypatch.setattr(module, "_run", fake)
    return calls


def _config(env: dict[str, str]) -> dict[str, str]:
    """Read the command-scope git config out of an environment.

    Parameters
    ----------
    env
        The environment git ran with.

    Returns
    -------
    dict
        Key to value.
    """
    return {
        env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"]
        for i in range(int(env["GIT_CONFIG_COUNT"]))
    }


def test_check_access_uses_the_fetch_hardening(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One protocol, the URL as config, strict host keys from the repository only, a minimal env."""
    monkeypatch.setenv("LAKANA_DATABASE__URL", "postgresql://u:p@db/lakana")
    calls = _fake_runs(monkeypatch)
    outcome, message = _check(tmp_path, credential=GitCredential(private_key=PRIVATE_KEY))
    assert outcome is ProbeOutcome.OK
    assert "read" in message
    listed = [(argv, env) for argv, env in calls if argv[1] == "ls-remote"]
    assert len(listed) == 1
    argv, env = listed[0]
    assert argv == ["git", "ls-remote", "--heads", "--tags", "--end-of-options", "origin"]
    assert not any(name.startswith("LAKANA_") for name in env)
    config = _config(env)
    assert config["remote.origin.url"] == SSH_URL
    assert config["protocol.allow"] == "never"
    assert config["protocol.ssh.allow"] == "always"
    assert config["credential.helper"] == ""
    assert config["core.hooksPath"] == "/dev/null"
    ssh = config["core.sshCommand"]
    assert "StrictHostKeyChecking=yes" in ssh and "BatchMode=yes" in ssh
    assert f"GlobalKnownHostsFile={'/dev/null'}" in ssh
    assert PRIVATE_KEY not in ssh and PRIVATE_KEY not in "".join(env.values())
    assert not list((tmp_path / "work").rglob("id")), "the key file is removed afterwards"


def test_check_access_revalidates_the_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A URL a worker would refuse to fetch is refused before git runs."""
    calls = _fake_runs(monkeypatch)
    outcome, message = _check(tmp_path, url="-oProxyCommand=x:r")
    assert outcome is ProbeOutcome.FAILED
    assert "URL" in message
    assert calls == []


def test_check_access_refuses_a_never_reachable_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A name that resolves to the metadata address is refused before git runs (M5)."""
    calls = _fake_runs(monkeypatch)
    outcome, message = _check(tmp_path, resolve=resolves_to("10.0.0.5", "169.254.169.254"))
    assert outcome is ProbeOutcome.REFUSED_ADDRESS
    assert "address" in message
    assert calls == []


def test_a_name_the_worker_cannot_resolve_is_left_to_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Behind a proxy a worker may not resolve names itself; git then says what happens."""

    def fails(*args: object, **kwargs: object) -> list[Any]:
        raise socket.gaierror("no")

    calls = _fake_runs(
        monkeypatch,
        returncode=128,
        stderr="ssh: Could not resolve hostname git.example.com: Name or service not known\n",
    )
    outcome, _ = _check(tmp_path, resolve=fails)
    assert outcome is ProbeOutcome.UNREACHABLE
    assert calls


AUTH = "Add the public key as a read-only deploy key at your git host, then test again."


@pytest.mark.parametrize(
    ("stderr", "outcome"),
    [
        (
            "git@git.example.com: Permission denied (publickey).\r\nfatal: Could not read from "
            "remote repository.\n",
            ProbeOutcome.AUTH_REFUSED,
        ),
        (
            "fatal: Authentication failed for 'https://git.example.com/r.git/'\n",
            ProbeOutcome.AUTH_REFUSED,
        ),
        (
            "fatal: could not read Username for 'https://git.example.com': terminal prompts "
            "disabled\n",
            ProbeOutcome.AUTH_REFUSED,
        ),
        (
            "@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@\n"
            "@    WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!     @\n"
            "IT IS POSSIBLE THAT SOMEONE IS DOING SOMETHING NASTY!\n"
            "Host key verification failed.\n",
            ProbeOutcome.HOST_KEY_CHANGED,
        ),
        (
            "No ED25519 host key is known for git.example.com and you have requested strict "
            "checking.\n"
            "Host key verification failed.\n",
            ProbeOutcome.HOST_KEY_UNKNOWN,
        ),
        ("Host key verification failed.\n", ProbeOutcome.HOST_KEY_UNKNOWN),
        (
            "ERROR: Repository not found.\nfatal: Could not read from remote repository.\n",
            ProbeOutcome.NOT_FOUND,
        ),
        (
            "remote: Repository not found.\nfatal: repository 'https://git.example.com/r.git/' "
            "not found\n",
            ProbeOutcome.NOT_FOUND,
        ),
        ("fatal: 'team/r.git' does not appear to be a git repository\n", ProbeOutcome.NOT_FOUND),
        (
            "ssh: connect to host git.example.com port 22: Connection refused\n",
            ProbeOutcome.UNREACHABLE,
        ),
        (
            "fatal: unable to access 'https://git.example.com/r.git/': Could not resolve host: "
            "git.example.com\n",
            ProbeOutcome.UNREACHABLE,
        ),
        (
            "ssh: connect to host git.example.com port 22: Connection timed out\n",
            ProbeOutcome.TIMEOUT,
        ),
        ("fatal: protocol error: bad line length character: SSH-\n", ProbeOutcome.FAILED),
    ],
)
def test_check_access_names_the_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stderr: str, outcome: ProbeOutcome
) -> None:
    """Git's stderr maps to an outcome and a fixed sentence with the next step.

    Parameters
    ----------
    tmp_path
        Pytest's directory.
    monkeypatch
        pytest's patcher.
    stderr
        What git printed.
    outcome
        The outcome it means.
    """
    _fake_runs(monkeypatch, returncode=128, stderr=stderr)
    found, message = _check(tmp_path)
    assert found is outcome
    assert stderr.strip() not in message
    assert "git.example.com" not in message
    if outcome is ProbeOutcome.AUTH_REFUSED:
        assert message == AUTH
    if outcome is ProbeOutcome.HOST_KEY_CHANGED:
        assert message == HOST_KEY_CHANGED_MESSAGE
        assert message == (
            "The host's key is not the one you confirmed. Check the host's published "
            "fingerprints before you connect again."
        )
    if outcome is ProbeOutcome.HOST_KEY_UNKNOWN:
        assert "lakana repo host-keys" in message
    if outcome is ProbeOutcome.FAILED:
        assert message == FAILED_MESSAGE == "The check failed; the worker's log has the details."


def test_check_access_redacts_the_credential_in_its_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A failure's stderr goes to the worker's log, redacted, and never into the message (T8)."""
    _fake_runs(
        monkeypatch,
        returncode=128,
        stderr=f"fatal: odd failure near {TOKEN} and https://u:{TOKEN}@h/\n",
    )
    with caplog.at_level(logging.WARNING, logger="lakana_worker.git"):
        outcome, message = _check(
            tmp_path,
            kind=RepositoryKind.HTTPS,
            url="https://git.example.com/team/r.git",
            credential=GitCredential(token=TOKEN),
            known_hosts=None,
        )
    assert outcome is ProbeOutcome.FAILED
    assert TOKEN not in message
    assert TOKEN not in caplog.text
    assert "odd failure" in caplog.text


def test_check_access_times_out_at_the_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deadline already passed is a timeout, not a crash."""
    _fake_runs(monkeypatch)
    outcome, message = _check(tmp_path, deadline=time.monotonic() - 1)
    assert outcome is ProbeOutcome.TIMEOUT
    assert "time" in message


def test_check_access_reads_a_real_repository_over_https(tmp_path: Path) -> None:
    """Against a real git server the right token is ok and a wrong one is refused, redacted."""
    make_repo(tmp_path / "src", {"lakana.yaml": "repo: analytics\n"})
    root = tmp_path / "served"
    root.mkdir()
    bare_copy(tmp_path / "src", root, "analytics")
    with GitHttpServer(root, tmp_path) as server:
        url = server.url("analytics")
        good = _check(
            tmp_path,
            kind=RepositoryKind.HTTPS,
            url=url,
            credential=GitCredential(token=server.token),
            known_hosts=None,
            ca_bundle=server.ca_file,
        )
        bad = _check(
            tmp_path,
            kind=RepositoryKind.HTTPS,
            url=url,
            credential=GitCredential(token="wrong-token-value"),
            known_hosts=None,
            ca_bundle=server.ca_file,
        )
    assert good[0] is ProbeOutcome.OK
    assert bad[0] is ProbeOutcome.AUTH_REFUSED
    assert "wrong-token-value" not in bad[1]


def test_check_access_refuses_a_proxy_that_is_not_an_http_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A malformed proxy setting is refused before git runs, naming the form it needs."""
    calls = _fake_runs(monkeypatch)
    outcome, message = _check(
        tmp_path,
        kind=RepositoryKind.HTTPS,
        url="https://git.example.com/team/r.git",
        known_hosts=None,
        https_proxy="ftp://proxy.corp:3128",
    )
    assert outcome is ProbeOutcome.FAILED
    assert "http://proxy.corp:3128" in message
    assert calls == []


def test_check_access_reads_a_local_repository_without_resolving_a_host(tmp_path: Path) -> None:
    """A local repository under the roots has no network host to check; git reads it."""
    make_repo(tmp_path / "src", {"lakana.yaml": "repo: analytics\n"})

    def never(*args: object, **kwargs: object) -> list[Any]:
        raise AssertionError("a local repository is not resolved")

    outcome, _ = _check(
        tmp_path,
        kind=RepositoryKind.LOCAL,
        url=str(tmp_path / "src"),
        known_hosts=None,
        resolve=never,
        repo_roots=[str(tmp_path)],
    )
    assert outcome is ProbeOutcome.OK


def test_a_git_error_that_is_no_timeout_is_failed_and_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Git refused before it ran (no local roots): a fixed sentence, the cause in the log."""
    with caplog.at_level(logging.WARNING, logger="lakana_worker.git"):
        outcome, message = _check(
            tmp_path, kind=RepositoryKind.LOCAL, url=str(tmp_path / "src"), known_hosts=None
        )
    assert (outcome, message) == (ProbeOutcome.FAILED, FAILED_MESSAGE)
    assert "LAKANA_REPO_ROOTS is empty" in caplog.text
