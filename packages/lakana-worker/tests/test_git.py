"""The hardened git fetcher."""

from __future__ import annotations

import base64
import contextlib
import errno
import io
import os
import resource
import selectors
import stat
import string
import subprocess
import tarfile
import tempfile
import time
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path

import pytest
from worker_git_helpers import ConnectProxy, GitHttpServer, bare_copy, commit, git, make_repo

import lakana_worker.git as module
from lakana_core.models import RepositoryKind
from lakana_core.projects.limits import DeployLimits
from lakana_worker.git import (
    AUTH_FAILED_MESSAGE,
    FetchedCommit,
    GitCredential,
    GitError,
    _archive,
    _describe_limit,
    _extract,
    _ssh_command,
    classify_failure,
    fetch_commit,
    git_environment,
    redact,
)

_FILES = {
    "lakana.yaml": "repo: analytics\n",
    "assets/bronze/events.sql": "SELECT 1 AS id\n",
}


def _fetch(
    tmp_path: Path,
    *,
    kind: RepositoryKind = RepositoryKind.LOCAL,
    url: str | None = None,
    ref: str | None = None,
    credential: GitCredential | None = None,
    known_hosts: str | None = None,
    limits: DeployLimits | None = None,
    repo_roots: Sequence[str] | None = None,
    deadline: float | None = None,
    ca_bundle: Path | None = None,
    https_proxy: str | None = None,
    no_proxy: str | None = None,
) -> FetchedCommit:
    """Call `fetch_commit` with test defaults.

    Parameters
    ----------
    tmp_path
        Pytest's per-test directory; `work/` inside it is the private workdir.
    kind
        Repository kind.
    url
        Repository URL; defaults to the local `repos/analytics`.
    ref
        Ref to fetch.
    credential
        Credential to use.
    known_hosts
        ``known_hosts`` text.
    limits
        Limits; defaults to `DeployLimits()`.
    repo_roots
        Local roots; defaults to `repos/`.
    deadline
        `time.monotonic()` deadline; defaults to 60 s from now.
    ca_bundle
        Extra CA file.
    https_proxy
        Operator proxy URL.
    no_proxy
        Operator proxy bypass list.

    Returns
    -------
    FetchedCommit
        The result.
    """
    work = tmp_path / "work"
    work.mkdir(mode=0o700, exist_ok=True)
    return fetch_commit(
        kind=kind,
        url=url if url is not None else str(tmp_path / "repos" / "analytics"),
        ref=ref,
        credential=credential,
        known_hosts=known_hosts,
        workdir=work,
        limits=limits if limits is not None else DeployLimits(),
        repo_roots=repo_roots if repo_roots is not None else [str(tmp_path / "repos")],
        deadline=deadline if deadline is not None else time.monotonic() + 60,
        ca_bundle=ca_bundle,
        https_proxy=https_proxy,
        no_proxy=no_proxy,
    )


@pytest.fixture
def local_sha(tmp_path: Path) -> str:
    """Create a local repository under `tmp_path/repos` with one commit.

    Parameters
    ----------
    tmp_path
        Pytest's per-test directory.

    Returns
    -------
    str
        The commit SHA.
    """
    return make_repo(tmp_path / "repos" / "analytics", _FILES)


def test_local_fetch_of_head_extracts_the_tree_without_git(tmp_path: Path, local_sha: str) -> None:
    """The default ref is HEAD; the tree has the files and no `.git`."""
    fetched = _fetch(tmp_path)
    assert fetched.sha == local_sha
    assert (fetched.tree / "assets" / "bronze" / "events.sql").read_text() == "SELECT 1 AS id\n"
    assert not (fetched.tree / ".git").exists()
    assert fetched.archive.stat().st_size > 0


def test_local_fetch_by_branch_tag_and_sha(tmp_path: Path, local_sha: str) -> None:
    """Branches, tags and full SHAs all resolve to the commit they name."""
    repo = tmp_path / "repos" / "analytics"
    git(repo, "tag", "v1")
    newer = commit(repo, {"assets/bronze/more.sql": "SELECT 2 AS id\n"})
    assert _fetch(tmp_path, ref="main").sha == newer
    assert _fetch(tmp_path, ref="v1").sha == local_sha
    assert _fetch(tmp_path, ref=local_sha.upper()).sha == local_sha


def test_unknown_ref_is_reported_by_name(tmp_path: Path, local_sha: str) -> None:
    """A ref the remote does not have is named in the error."""
    with pytest.raises(GitError, match="ref 'nope' not found"):
        _fetch(tmp_path, ref="nope")


def test_unknown_sha_is_unreachable(tmp_path: Path, local_sha: str) -> None:
    """A SHA no branch or tag contains is refused, naming the repository."""
    with pytest.raises(
        GitError,
        match=f"commit {'d' * 40} is not reachable from any branch or tag of the repository",
    ):
        _fetch(tmp_path, ref="d" * 40)


def test_local_repo_outside_roots_is_refused_at_fetch(tmp_path: Path, local_sha: str) -> None:
    """The worker re-checks LAKANA_REPO_ROOTS; a removed root refuses the deploy."""
    with pytest.raises(GitError, match="LAKANA_REPO_ROOTS is empty"):
        _fetch(tmp_path, repo_roots=[])
    with pytest.raises(GitError, match="not under any"):
        _fetch(tmp_path, repo_roots=[str(tmp_path / "elsewhere")])


def test_local_repo_named_star_is_refused_at_fetch(tmp_path: Path) -> None:
    """The worker's re-check refuses `/…/*`, so `safe.directory` can never become a glob."""
    star = tmp_path / "repos" / "*"
    make_repo(star, _FILES)
    with pytest.raises(GitError, match=r"last component must not be '\*'"):
        _fetch(tmp_path, url=str(star))


def test_local_repo_config_never_runs(tmp_path: Path, local_sha: str) -> None:
    """Adversarial: core.fsmonitor in the source repo's .git/config must not run."""
    canary = tmp_path / "canary"
    repo = tmp_path / "repos" / "analytics"
    git(repo, "config", "core.fsmonitor", f"touch {canary}")
    git(repo, "config", "core.hooksPath", str(tmp_path / "hooks"))
    (tmp_path / "hooks").mkdir()
    hook = tmp_path / "hooks" / "post-checkout"
    hook.write_text(f"#!/bin/sh\ntouch {canary}\n")
    hook.chmod(0o755)
    _fetch(tmp_path)
    assert not canary.exists()


def test_option_shaped_ref_is_refused_before_git_runs(tmp_path: Path, local_sha: str) -> None:
    """Adversarial: `--upload-pack=…` as a ref never reaches git."""
    canary = tmp_path / "pwned"
    with pytest.raises(GitError, match="not a valid git ref"):
        _fetch(tmp_path, ref=f"--upload-pack=touch {canary}")
    assert not canary.exists()


def test_symlinks_survive_extraction_as_symlinks(tmp_path: Path) -> None:
    """A committed symlink is extracted as a link (discovery refuses it), never followed."""
    repo = tmp_path / "repos" / "analytics"
    make_repo(repo, _FILES)
    os.symlink("/etc/passwd", repo / "assets" / "bronze" / "passwd.sql")
    commit(repo, {})
    fetched = _fetch(tmp_path)
    link = fetched.tree / "assets" / "bronze" / "passwd.sql"
    assert link.is_symlink()
    assert os.readlink(link) == "/etc/passwd"


def test_checkout_over_the_size_limit_is_refused(tmp_path: Path) -> None:
    """Adversarial: the archive is cut off at the (test-lowered) limit."""
    make_repo(tmp_path / "repos" / "analytics", {**_FILES, "big.bin": "x" * 50_000})
    with pytest.raises(GitError, match="larger than"):
        _fetch(tmp_path, limits=DeployLimits(checkout_bytes=10_000))


def test_expired_deadline_stops_before_running_git(tmp_path: Path, local_sha: str) -> None:
    """The job's wall-clock limit applies to every command."""
    with pytest.raises(GitError, match="time limit"):
        _fetch(tmp_path, deadline=time.monotonic() - 1)


def test_credential_files_are_removed_after_success_and_failure(tmp_path: Path) -> None:
    """Askpass and key material live only for the duration of the fetch."""
    served = tmp_path / "served"
    served.mkdir()
    make_repo(tmp_path / "src", _FILES)
    bare_copy(tmp_path / "src", served, "analytics")
    with GitHttpServer(served, tmp_path) as server:
        _fetch(
            tmp_path,
            kind=RepositoryKind.HTTPS,
            url=server.url("analytics"),
            credential=GitCredential(token=server.token),
            ca_bundle=server.ca_file,
        )
        assert not list((tmp_path / "work").rglob("credentials"))
        with pytest.raises(GitError):
            _fetch(
                tmp_path,
                kind=RepositoryKind.HTTPS,
                url=server.url("analytics"),
                credential=GitCredential(token="wrong"),
                ca_bundle=server.ca_file,
            )
        assert not list((tmp_path / "work").rglob("credentials"))


@pytest.fixture
def served(tmp_path: Path) -> tuple[Path, str]:
    """Serve a bare `analytics.git` from `tmp_path/served`.

    Parameters
    ----------
    tmp_path
        Pytest's per-test directory.

    Returns
    -------
    tuple of (Path, str)
        The served root and the commit SHA.
    """
    sha = make_repo(tmp_path / "src", _FILES)
    root = tmp_path / "served"
    root.mkdir()
    bare = bare_copy(tmp_path / "src", root, "analytics")
    git(bare, "config", "uploadpack.allowFilter", "true")
    return root, sha


def _https(
    tmp_path: Path,
    server: GitHttpServer,
    credential: GitCredential | None,
    *,
    ref: str | None = None,
    limits: DeployLimits | None = None,
    https_proxy: str | None = None,
    no_proxy: str | None = None,
) -> FetchedCommit:
    """Fetch the served repository over HTTPS.

    Parameters
    ----------
    tmp_path
        Pytest's per-test directory.
    server
        The running server.
    credential
        The credential to send.
    ref
        Ref to fetch.
    limits
        Limits override.
    https_proxy
        Operator proxy URL.
    no_proxy
        Operator proxy bypass list.

    Returns
    -------
    FetchedCommit
        The result.
    """
    return _fetch(
        tmp_path,
        kind=RepositoryKind.HTTPS,
        url=server.url("analytics"),
        credential=credential,
        ca_bundle=server.ca_file,
        ref=ref,
        limits=limits,
        https_proxy=https_proxy,
        no_proxy=no_proxy,
    )


def test_https_any_username_mode_uses_oauth2_by_default(
    tmp_path: Path, served: tuple[Path, str]
) -> None:
    """GitHub PAT mode: any username, the token is what counts; we send `oauth2`."""
    root, sha = served
    with GitHttpServer(root, tmp_path) as server:
        assert _https(tmp_path, server, GitCredential(token=server.token)).sha == sha
        assert {user for _, user in server.requests if user} == {"oauth2"}


def test_https_fixed_username_mode(tmp_path: Path, served: tuple[Path, str]) -> None:
    """GitLab deploy-token mode: the configured username is required."""
    root, sha = served
    with GitHttpServer(root, tmp_path, username="gitlab+deploy-token-1") as server:
        good = GitCredential(username="gitlab+deploy-token-1", token=server.token)
        assert _https(tmp_path, server, good).sha == sha
        with pytest.raises(GitError, match=AUTH_FAILED_MESSAGE):
            _https(tmp_path, server, GitCredential(token=server.token))


@pytest.mark.parametrize("status", [401, 403, 404])
def test_rejected_credential_is_one_message_with_the_secret_redacted(
    tmp_path: Path, served: tuple[Path, str], status: int
) -> None:
    """401, 403 and 404 all become one message; the token never appears."""
    root, _ = served
    with (
        GitHttpServer(root, tmp_path, deny_status=status) as server,
        pytest.raises(GitError) as info,
    ):
        _https(tmp_path, server, GitCredential(token="wrong-token-123"))
    message = str(info.value)
    assert AUTH_FAILED_MESSAGE in message
    assert "wrong-token-123" not in message
    assert base64.b64encode(b"oauth2:wrong-token-123").decode() not in message


@pytest.mark.parametrize("allow_reachable", [True, False])
def test_sha_fetch_with_and_without_allow_reachable_sha1_in_want(
    tmp_path: Path, served: tuple[Path, str], allow_reachable: bool
) -> None:
    """A non-tip SHA is fetched directly when allowed, through the fallback when not (v0)."""
    root, first = served
    second = commit(tmp_path / "src", {"assets/bronze/more.sql": "SELECT 2 AS id\n"})
    git(tmp_path / "src", "push", "--quiet", str(root / "analytics.git"), "main")
    git(
        root / "analytics.git",
        "config",
        "uploadpack.allowReachableSHA1InWant",
        str(allow_reachable).lower(),
    )
    with GitHttpServer(root, tmp_path, protocol_v2=False) as server:
        assert _https(tmp_path, server, GitCredential(token=server.token), ref=first).sha == first
        assert _https(tmp_path, server, GitCredential(token=server.token), ref=second).sha == second


def test_sha_reachable_only_from_a_tag_after_a_force_push(
    tmp_path: Path, served: tuple[Path, str]
) -> None:
    """The fallback fetches tags, so a tag-only commit still deploys."""
    root, first = served
    bare = root / "analytics.git"
    git(bare, "tag", "v1", first)
    replacement = make_repo(tmp_path / "other", {"assets/bronze/x.sql": "SELECT 3 AS id\n"})
    git(tmp_path / "other", "push", "--quiet", "--force", str(bare), "main")
    git(bare, "config", "uploadpack.allowReachableSHA1InWant", "false")
    with GitHttpServer(root, tmp_path, protocol_v2=False) as server:
        assert _https(tmp_path, server, GitCredential(token=server.token), ref=first).sha == first
        assert _https(tmp_path, server, GitCredential(token=server.token)).sha == replacement


def test_slow_server_times_out(tmp_path: Path, served: tuple[Path, str]) -> None:
    """Each git command is bounded by `git_command_seconds`."""
    root, _ = served
    with (
        GitHttpServer(root, tmp_path, delay_seconds=3) as server,
        pytest.raises(GitError, match="timed out"),
    ):
        _https(
            tmp_path,
            server,
            GitCredential(token=server.token),
            limits=DeployLimits(git_command_seconds=1),
        )


def test_git_environment_is_minimal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No LAKANA_*, database, storage or KMS settings reach git; config goes via GIT_CONFIG_*."""
    monkeypatch.setenv("LAKANA_DATABASE__URL", "postgresql://u:p@db/lakana")
    monkeypatch.setenv("LAKANA_SECRETS__MASTER_KEY", "k")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "worker-secret")
    monkeypatch.setenv("GIT_DIR", "/elsewhere")
    env = git_environment(
        tmp_path / "home",
        [("protocol.allow", "never"), ("remote.origin.url", "u")],
        workdir=tmp_path,
    )
    assert not any(name.startswith(("LAKANA_", "AWS_")) for name in env)
    assert "GIT_DIR" not in env
    assert env["HOME"] == str(tmp_path / "home")
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_NO_LAZY_FETCH"] == "1"
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull
    assert env["GIT_CONFIG_COUNT"] == "2"
    assert (env["GIT_CONFIG_KEY_1"], env["GIT_CONFIG_VALUE_1"]) == ("remote.origin.url", "u")
    assert set(env) <= {
        "PATH",
        "HOME",
        "LC_ALL",
        "GIT_TERMINAL_PROMPT",
        "GIT_CONFIG_NOSYSTEM",
        "GIT_CONFIG_GLOBAL",
        "GIT_CEILING_DIRECTORIES",
        "GIT_PROTOCOL_FROM_USER",
        "GIT_NO_LAZY_FETCH",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_KEY_0",
        "GIT_CONFIG_VALUE_0",
        "GIT_CONFIG_KEY_1",
        "GIT_CONFIG_VALUE_1",
    }


def test_redact_removes_tokens_their_basic_auth_form_and_userinfo() -> None:
    """Every form a credential can take in git's stderr is removed."""
    token = "tok-abc"
    encoded = base64.b64encode(b"oauth2:tok-abc").decode()
    text = f"fatal: https://oauth2:{token}@host/x {encoded} {token}"
    cleaned = redact(text, [token, encoded])
    assert token not in cleaned and encoded not in cleaned
    assert "https://***@host/x" in cleaned


@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        ("fatal: Authentication failed for 'https://h/x'", AUTH_FAILED_MESSAGE),
        (
            "fatal: unable to access 'https://h/x/': The requested URL returned error: 403",
            AUTH_FAILED_MESSAGE,
        ),
        (
            "remote: Repository not found.\nfatal: repository 'https://h/x/' not found",
            AUTH_FAILED_MESSAGE,
        ),
        ("git@h: Permission denied (publickey).", AUTH_FAILED_MESSAGE),
        ("Host key verification failed.", "SSH host key verification failed"),
        ("fatal: couldn't find remote ref nope", "ref 'nope' not found"),
        ("fatal: something odd", "git failed: fatal: something odd"),
    ],
)
def test_classify_failure(stderr: str, expected: str) -> None:
    """Host-style failures collapse to one message; others keep their (redacted) text."""
    assert expected in classify_failure(stderr, ref="nope")


def test_credential_repr_hides_values() -> None:
    """A credential never prints its token or key."""
    text = repr(GitCredential(username="u", token="tok", private_key="KEY"))
    assert "tok" not in text and "KEY" not in text


def test_https_credential_files_are_private(
    tmp_path: Path, served: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The askpass script is 0700 and the token file 0600 while git runs."""
    seen: dict[str, int] = {}
    original = module._run

    def spy(
        args: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout: float,
        max_bytes: int | None = None,
    ) -> subprocess.CompletedProcess[str]:
        for path in (tmp_path / "work").rglob("credentials/*"):
            seen[path.name] = stat.S_IMODE(path.stat().st_mode)
        return original(args, cwd=cwd, env=env, timeout=timeout, max_bytes=max_bytes)

    monkeypatch.setattr(module, "_run", spy)
    root, _ = served
    with GitHttpServer(root, tmp_path) as server:
        _https(tmp_path, server, GitCredential(token=server.token))
    assert seen == {"askpass": 0o700, "username": 0o600, "token": 0o600}


# --- argv, environment and credential placement ---------------------------------------


def _spy_runs(monkeypatch: pytest.MonkeyPatch) -> list[tuple[list[str], dict[str, str]]]:
    """Record the argv and environment of every `_run` call.

    Parameters
    ----------
    monkeypatch
        Pytest's monkeypatch fixture.

    Returns
    -------
    list
        ``(argv, env)`` pairs, appended as git commands run.
    """
    calls: list[tuple[list[str], dict[str, str]]] = []
    original = module._run

    def spy(
        args: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        timeout: float,
        max_bytes: int | None = None,
    ) -> subprocess.CompletedProcess[str]:
        calls.append((list(args), dict(env)))
        return original(args, cwd=cwd, env=env, timeout=timeout, max_bytes=max_bytes)

    monkeypatch.setattr(module, "_run", spy)
    return calls


def test_url_is_config_not_argv_and_values_follow_end_of_options(
    tmp_path: Path, served: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Adversarial: no argv carries the URL or token; user values come after `--end-of-options`."""
    monkeypatch.setenv("LAKANA_DATABASE__URL", "postgresql://u:p@db/lakana")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "worker-secret")
    calls = _spy_runs(monkeypatch)
    root, sha = served
    with GitHttpServer(root, tmp_path) as server:
        _https(tmp_path, server, GitCredential(token=server.token), ref="main")
        url = server.url("analytics")
    assert calls
    for argv, env in calls:
        assert not any(url in part or "s3cret" in part for part in argv)
        assert not any(name.startswith(("LAKANA_", "AWS_")) for name in env)
        assert "s3cret-token-value" not in "".join(env.values())
        assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    fetches = [argv for argv, _ in calls if argv[1] == "fetch"]
    assert fetches
    for argv in fetches:
        assert argv.index("--end-of-options") < argv.index("origin")
    assert fetches[0][-1] == "refs/heads/main"
    config = {
        calls[0][1][f"GIT_CONFIG_KEY_{i}"]: calls[0][1][f"GIT_CONFIG_VALUE_{i}"]
        for i in range(int(calls[0][1]["GIT_CONFIG_COUNT"]))
    }
    assert config["remote.origin.url"] == url
    assert config["protocol.allow"] == "never"
    assert config["protocol.https.allow"] == "always"
    assert config["transfer.fsckObjects"] == "true"
    assert config["http.sslCAInfo"].endswith("cert.pem")
    assert sha


def test_local_fetch_trusts_only_the_resolved_source_through_a_private_global_config(
    tmp_path: Path, local_sha: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repo owned by another user (the Compose mount) needs `safe.directory` for upload-pack.

    upload-pack runs in the source and never sees command-scope config (git scrubs
    `GIT_CONFIG_COUNT` for it), so the entry travels in a private global config file naming
    exactly the resolved source path and its `.git`.
    """
    calls = _spy_runs(monkeypatch)
    assert _fetch(tmp_path).sha == local_sha
    source = str((tmp_path / "repos" / "analytics").resolve())
    fetch_env = next(env for argv, env in calls if argv[1] == "fetch")
    config = Path(fetch_env["GIT_CONFIG_GLOBAL"])
    assert config != Path(os.devnull)
    assert config.read_text() == (
        f'[safe]\n\tdirectory = "{source}"\n\tdirectory = "{source}/.git"\n'
    )
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    assert fetch_env["GIT_CONFIG_NOSYSTEM"] == "1"


def test_trusted_path_is_escaped_inside_the_global_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A quote or backslash in the path cannot add a config entry of its own."""
    hostile = tmp_path / "repos" / 'x"\\y'
    sha = make_repo(hostile, _FILES)
    calls = _spy_runs(monkeypatch)
    assert _fetch(tmp_path, url=str(hostile)).sha == sha
    fetch_env = next(env for argv, env in calls if argv[1] == "fetch")
    escaped = str(hostile.resolve()).replace("\\", "\\\\").replace('"', '\\"')
    assert Path(fetch_env["GIT_CONFIG_GLOBAL"]).read_text() == (
        f'[safe]\n\tdirectory = "{escaped}"\n\tdirectory = "{escaped}/.git"\n'
    )
    parsed = subprocess.run(
        ["git", "config", "--file", fetch_env["GIT_CONFIG_GLOBAL"], "--get-all", "safe.directory"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    assert parsed == [str(hostile.resolve()), f"{hostile.resolve()}/.git"]


def test_https_fetch_keeps_the_global_config_empty(
    tmp_path: Path, served: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a local fetch gets a trusted directory."""
    calls = _spy_runs(monkeypatch)
    root, _ = served
    with GitHttpServer(root, tmp_path) as server:
        _https(tmp_path, server, GitCredential(token=server.token), ref="main")
    assert calls
    assert all(env["GIT_CONFIG_GLOBAL"] == os.devnull for _, env in calls)


def test_each_fetch_gets_its_own_directory_inside_the_workdir(
    tmp_path: Path, local_sha: str
) -> None:
    """Two fetches into one workdir never collide, and results stay under it."""
    first, second = _fetch(tmp_path), _fetch(tmp_path)
    assert first.tree != second.tree
    work = (tmp_path / "work").resolve()
    for fetched in (first, second):
        assert work in fetched.tree.resolve().parents
        assert work in fetched.archive.resolve().parents


def test_username_with_url_metacharacters_reaches_git_only_through_askpass(
    tmp_path: Path, served: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A username with `@ / % #` is sent as basic-auth user and appears in no argv or URL."""
    name = "dep@loy/to%ken#1"
    calls = _spy_runs(monkeypatch)
    root, sha = served
    with GitHttpServer(root, tmp_path, username=name) as server:
        assert _https(tmp_path, server, GitCredential(username=name, token=server.token)).sha == sha
    for argv, env in calls:
        assert not any(name in part for part in argv)
        assert not any(name in value for value in env.values())


def test_https_without_a_credential_is_the_auth_message(
    tmp_path: Path, served: tuple[Path, str]
) -> None:
    """No credential against a server that wants one: prompts are off, so one clear message."""
    root, _ = served
    with (
        GitHttpServer(root, tmp_path) as server,
        pytest.raises(GitError, match=AUTH_FAILED_MESSAGE),
    ):
        _https(tmp_path, server, None)


def test_https_url_is_revalidated_in_the_worker(tmp_path: Path) -> None:
    """A row with credentials in the URL never reaches git."""
    with pytest.raises(GitError, match="must not contain credentials"):
        _fetch(tmp_path, kind=RepositoryKind.HTTPS, url="https://u:p@host/x.git")


def test_missing_git_binary_is_a_git_error(
    tmp_path: Path, local_sha: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If git cannot be started the failure is a GitError, not an OSError."""
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    with pytest.raises(GitError, match="could not run git"):
        _fetch(tmp_path)


def _completed(returncode: int, stderr: str = "") -> subprocess.CompletedProcess[str]:
    """Build a finished process for stubbing `_run`.

    Parameters
    ----------
    returncode
        Exit status.
    stderr
        Captured stderr.

    Returns
    -------
    subprocess.CompletedProcess
        A result with empty stdout.
    """
    return subprocess.CompletedProcess([], returncode, stdout="", stderr=stderr)


def test_failed_init_is_reported(
    tmp_path: Path, local_sha: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing `git init` stops the fetch with git's text."""
    monkeypatch.setattr(module, "_run", lambda args, **_: _completed(1, "fatal: no disk"))
    with pytest.raises(GitError, match="git init failed: fatal: no disk"):
        _fetch(tmp_path)


def test_failed_fallback_fetch_is_classified(
    tmp_path: Path, local_sha: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the direct SHA fetch and the all-refs fallback both fail, the latter's error shows."""
    original = module._run

    def fake(
        args: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout: float,
        max_bytes: int | None = None,
    ) -> subprocess.CompletedProcess[str]:
        if args[1] == "fetch" and "--filter=tree:0" in args:
            return original(args, cwd=cwd, env=env, timeout=timeout, max_bytes=max_bytes)
        if args[1] == "fetch" and "--depth" in args:
            return _completed(128, "fatal: not our ref")
        if args[1] == "fetch":
            return _completed(128, "fatal: unable to access: The requested URL returned error: 403")
        return original(args, cwd=cwd, env=env, timeout=timeout, max_bytes=max_bytes)

    monkeypatch.setattr(module, "_run", fake)
    with pytest.raises(GitError, match=AUTH_FAILED_MESSAGE):
        _fetch(tmp_path, ref=local_sha)


def test_auth_failure_on_a_sha_does_not_trigger_the_fallback(
    tmp_path: Path, local_sha: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rejected credential is final; fetching every ref would only repeat the failure."""
    seen: list[Sequence[str]] = []
    original = module._run

    def fake(
        args: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout: float,
        max_bytes: int | None = None,
    ) -> subprocess.CompletedProcess[str]:
        seen.append(args)
        if args[1] == "fetch":
            return _completed(128, "fatal: Authentication failed for 'https://h/x'")
        return original(args, cwd=cwd, env=env, timeout=timeout, max_bytes=max_bytes)

    monkeypatch.setattr(module, "_run", fake)
    with pytest.raises(GitError, match=AUTH_FAILED_MESSAGE):
        _fetch(tmp_path, ref="a" * 40)
    assert sum(1 for args in seen if args[1] == "fetch") == 1


def test_protocol_v2_ignores_allow_reachable_sha1_in_want(
    tmp_path: Path, served: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """With protocol v2 a non-tip SHA is fetched directly, no fallback."""
    root, first = served
    commit(tmp_path / "src", {"assets/bronze/more.sql": "SELECT 2 AS id\n"})
    git(tmp_path / "src", "push", "--quiet", str(root / "analytics.git"), "main")
    git(root / "analytics.git", "config", "uploadpack.allowReachableSHA1InWant", "false")
    calls = _spy_runs(monkeypatch)
    with GitHttpServer(root, tmp_path, protocol_v2=True) as server:
        assert _https(tmp_path, server, GitCredential(token=server.token), ref=first).sha == first
    fetches = [argv for argv, _ in calls if argv[1] == "fetch"]
    assert len(fetches) == 2
    assert "--filter=tree:0" in fetches[0]
    assert "--depth" in fetches[1]


def test_the_all_refs_fallback_runs_when_v0_refuses_a_non_tip_sha(
    tmp_path: Path, served: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Protocol v0 without allowReachableSHA1InWant: shallow fetch fails, fallback succeeds."""
    root, first = served
    commit(tmp_path / "src", {"assets/bronze/more.sql": "SELECT 2 AS id\n"})
    git(tmp_path / "src", "push", "--quiet", str(root / "analytics.git"), "main")
    git(root / "analytics.git", "config", "uploadpack.allowReachableSHA1InWant", "false")
    calls = _spy_runs(monkeypatch)
    with GitHttpServer(root, tmp_path, protocol_v2=False) as server:
        assert _https(tmp_path, server, GitCredential(token=server.token), ref=first).sha == first
    fetches = [argv for argv, _ in calls if argv[1] == "fetch"]
    assert len(fetches) == 3
    assert "--filter=tree:0" in fetches[0]
    assert "--depth" in fetches[1]
    assert "+refs/tags/*:refs/tags/*" in fetches[2]
    assert "--filter=tree:0" not in fetches[2]


# --- archive and extraction -----------------------------------------------------------


def _tar(path: Path, *members: tuple[tarfile.TarInfo, bytes | None]) -> Path:
    """Write a tar file with exactly the given members.

    Parameters
    ----------
    path
        Output file.
    *members
        ``(info, content)`` pairs; content is `None` for non-regular members.

    Returns
    -------
    Path
        `path`.
    """
    with tarfile.open(path, "w") as handle:
        for info, data in members:
            handle.addfile(info, io.BytesIO(data) if data is not None else None)
    return path


def _info(
    name: str, kind: bytes = tarfile.REGTYPE, *, size: int = 0, link: str = ""
) -> tarfile.TarInfo:
    """Build a tar member header.

    Parameters
    ----------
    name
        Member name.
    kind
        Tar type flag.
    size
        Content size (regular files).
    link
        Link target.

    Returns
    -------
    tarfile.TarInfo
        The header.
    """
    info = tarfile.TarInfo(name)
    info.type = kind
    info.size = size
    info.linkname = link
    info.mode = 0o644
    return info


def test_extract_keeps_regular_files_directories_and_symlinks(tmp_path: Path) -> None:
    """The three member kinds git archive emits are extracted; links stay links."""
    archive = _tar(
        tmp_path / "a.tar",
        (_info("d", tarfile.DIRTYPE), None),
        (_info("d/f.sql", size=3), b"abc"),
        (_info("d/link", tarfile.SYMTYPE, link="/etc/passwd"), None),
    )
    _extract(archive, tmp_path / "tree", limit=1000, max_members=100)
    assert (tmp_path / "tree" / "d" / "f.sql").read_text() == "abc"
    assert os.readlink(tmp_path / "tree" / "d" / "link") == "/etc/passwd"


@pytest.mark.parametrize(
    "member",
    [
        _info("dev", tarfile.CHRTYPE),
        _info("blk", tarfile.BLKTYPE),
        _info("fifo", tarfile.FIFOTYPE),
        _info("hard", tarfile.LNKTYPE, link="/etc/passwd"),
        _info("hard2", tarfile.LNKTYPE, link="../outside"),
        _info("hard3", tarfile.LNKTYPE, link="f"),
    ],
    ids=["char-device", "block-device", "fifo", "hardlink-abs", "hardlink-out", "hardlink-in"],
)
def test_extract_refuses_anything_that_is_not_a_file_directory_or_symlink(
    tmp_path: Path, member: tarfile.TarInfo
) -> None:
    """Adversarial: no device, FIFO or hard link reaches the checkout."""
    archive = _tar(tmp_path / "a.tar", (_info("f", size=1), b"x"), (member, None))
    with pytest.raises(GitError, match="unsupported entry"):
        _extract(archive, tmp_path / "tree", limit=1000, max_members=100)
    assert not (tmp_path / "tree" / member.name).exists()


@pytest.mark.parametrize("name", ["../escape", "/abs", "a/../../escape"])
@pytest.mark.parametrize("kind", [tarfile.REGTYPE, tarfile.SYMTYPE])
def test_extract_refuses_names_that_leave_the_tree(tmp_path: Path, name: str, kind: bytes) -> None:
    """Adversarial: absolute and `..` names are refused for files and for symlinks alike."""
    archive = _tar(tmp_path / "a.tar", (_info(name, kind, link="x"), None))
    with pytest.raises(GitError, match="unsafe path"):
        _extract(archive, tmp_path / "tree", limit=1000, max_members=100)
    assert not (tmp_path / "escape").exists()


def test_extract_refuses_a_file_written_through_an_outside_symlink(tmp_path: Path) -> None:
    """Adversarial: a file below a symlink that points outside is refused."""
    outside = tmp_path / "outside"
    outside.mkdir()
    archive = _tar(
        tmp_path / "a.tar",
        (_info("l", tarfile.SYMTYPE, link=str(outside)), None),
        (_info("l/pwn", size=1), b"x"),
    )
    with pytest.raises(GitError, match="unsafe path"):
        _extract(archive, tmp_path / "tree", limit=1000, max_members=100)
    assert not (outside / "pwn").exists()


def test_extract_stops_when_member_sizes_pass_the_limit(tmp_path: Path) -> None:
    """The summed size of regular members is bounded, not just the archive."""
    archive = _tar(
        tmp_path / "a.tar",
        (_info("a", size=600), b"x" * 600),
        (_info("b", size=600), b"y" * 600),
    )
    with pytest.raises(GitError, match="larger than 1000 bytes"):
        _extract(archive, tmp_path / "tree", limit=1000, max_members=100)
    _extract(archive, tmp_path / "ok", limit=1200, max_members=100)


def test_archive_failure_is_reported(tmp_path: Path, local_sha: str) -> None:
    """`git archive` of an object that does not exist fails with its message."""
    repo = tmp_path / "bare.git"
    git(tmp_path, "init", "--quiet", "--bare", str(repo))
    env = git_environment(tmp_path / "home", [], workdir=tmp_path)
    with pytest.raises(GitError, match="git archive failed"):
        _archive("d" * 40, cwd=repo, env=env, timeout=30, target=tmp_path / "x.tar", limit=1000)


def test_archive_timeout_kills_git(tmp_path: Path) -> None:
    """A hung `git archive` is killed at its timeout."""
    stub = tmp_path / "bin"
    stub.mkdir()
    script = stub / "git"
    script.write_text("#!/bin/sh\nexec /bin/sleep 30\n")
    script.chmod(0o755)
    started = time.monotonic()
    with pytest.raises(GitError, match="git archive timed out"):
        _archive(
            "a" * 40,
            cwd=tmp_path,
            env={"PATH": str(stub)},
            timeout=0.5,
            target=tmp_path / "x.tar",
            limit=1000,
        )
    assert time.monotonic() - started < 10


# --- SSH, without a server ------------------------------------------------


def test_ssh_command_pins_host_keys_and_the_deploy_key(tmp_path: Path) -> None:
    """Strict host keys against the repository's file only, batch mode, no agent, the key only."""
    directory = tmp_path / "credentials"
    directory.mkdir(mode=0o700)
    command = _ssh_command(
        directory,
        GitCredential(private_key="-----BEGIN KEY-----\nabc\n-----END KEY-----"),
        "h ssh-ed25519 AAAA\n",
    )
    words = command.split()
    assert words[0] == "ssh"
    for option in (
        "BatchMode=yes",
        "StrictHostKeyChecking=yes",
        f"UserKnownHostsFile={directory}/known_hosts",
        f"GlobalKnownHostsFile={os.devnull}",
        "IdentityAgent=none",
        "IdentitiesOnly=yes",
    ):
        assert option in words
    assert words[words.index("-F") + 1] == os.devnull
    assert words[words.index("-i") + 1] == f"{directory}/id"
    assert (directory / "known_hosts").read_text() == "h ssh-ed25519 AAAA\n"
    assert (directory / "id").read_text().endswith("-----END KEY-----\n")
    for name in ("known_hosts", "id"):
        assert stat.S_IMODE((directory / name).stat().st_mode) == 0o600


def test_ssh_command_without_a_key_or_known_hosts(tmp_path: Path) -> None:
    """No credential: no identity options, and an empty known_hosts so verification fails closed."""
    directory = tmp_path / "credentials"
    directory.mkdir(mode=0o700)
    command = _ssh_command(directory, None, None)
    words = command.split()
    assert "-i" not in words
    assert "IdentitiesOnly=yes" in words
    assert "IdentityFile=none" in words
    assert (directory / "known_hosts").read_text() == ""
    assert not (directory / "id").exists()


def test_ssh_fetch_wires_the_command_through_config_and_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """For kind=ssh the command is `core.sshCommand`; key files exist only while git runs."""
    seen: dict[str, object] = {}

    def fake(
        args: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout: float,
        max_bytes: int | None = None,
    ) -> subprocess.CompletedProcess[str]:
        if args[1] == "init":
            values = {
                env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"]
                for i in range(int(env["GIT_CONFIG_COUNT"]))
            }
            seen["command"] = values["core.sshCommand"]
            seen["protocol"] = values["protocol.ssh.allow"]
            seen["files"] = sorted(p.name for p in (tmp_path / "work").rglob("credentials/*"))
            return _completed(1, "stop here")
        raise AssertionError(args)

    monkeypatch.setattr(module, "_run", fake)
    with pytest.raises(GitError, match="git init failed"):
        _fetch(
            tmp_path,
            kind=RepositoryKind.SSH,
            url="ssh://git@host.example/analytics.git",
            credential=GitCredential(private_key="KEYDATA"),
            known_hosts="host.example ssh-ed25519 AAAA\n",
        )
    assert "StrictHostKeyChecking=yes" in str(seen["command"])
    assert seen["protocol"] == "always"
    assert seen["files"] == ["id", "known_hosts"]
    assert not list((tmp_path / "work").rglob("credentials"))
    assert "KEYDATA" not in str(seen["command"])


def test_git_environment_ceiling_is_the_parent_of_the_work_directory(tmp_path: Path) -> None:
    """Discovery may not climb above the private temp root."""
    work = tmp_path / "work"
    env = git_environment(work / "fetch-1" / "home", [], workdir=work)
    assert env["GIT_CEILING_DIRECTORIES"] == str(tmp_path)


def test_local_upload_pack_hooks_in_the_source_config_never_run(
    tmp_path: Path, local_sha: str
) -> None:
    """Adversarial: the source config is read by upload-pack, but git refuses these hooks there."""
    canary = tmp_path / "canary"
    repo = tmp_path / "repos" / "analytics"
    other = make_repo(tmp_path / "other", {"x": "1"})
    assert other
    (repo / ".git" / "objects" / "info" / "alternates").write_text(
        f"{tmp_path / 'other' / '.git' / 'objects'}\n"
    )
    git(repo, "config", "uploadpack.packObjectsHook", f"touch {canary}")
    git(repo, "config", "core.alternateRefsCommand", f"touch {canary}")
    assert _fetch(tmp_path).sha == local_sha
    assert not canary.exists()


def test_describe_limit_uses_mebibytes_when_whole() -> None:
    """The default 200 MiB limit reads as such; odd limits read in bytes."""
    assert _describe_limit(200 * 1024 * 1024) == "200 MiB"
    assert _describe_limit(10_000) == "10000 bytes"


def test_https_without_a_ca_bundle_uses_the_system_trust_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Production passes no `ca_bundle`: `http.sslCAInfo` is not set at all."""
    calls = _spy_runs(monkeypatch)
    with pytest.raises(GitError):
        _fetch(
            tmp_path,
            kind=RepositoryKind.HTTPS,
            url="https://127.0.0.1:1/analytics.git",
            credential=GitCredential(token="t"),
        )
    env = calls[0][1]
    keys = [env[f"GIT_CONFIG_KEY_{i}"] for i in range(int(env["GIT_CONFIG_COUNT"]))]
    assert "http.sslCAInfo" not in keys


# --- more extraction cases ----------------------------------------------------------------------


def test_extract_refuses_a_symlink_written_through_a_symlink(tmp_path: Path) -> None:
    """Adversarial: `l -> outside` then symlink `l/victim` must not replace `outside/victim`."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "victim").write_text("original")
    archive = _tar(
        tmp_path / "a.tar",
        (_info("l", tarfile.SYMTYPE, link=str(outside)), None),
        (_info("l/victim", tarfile.SYMTYPE, link="/etc/passwd"), None),
    )
    with pytest.raises(GitError, match="unsafe path"):
        _extract(archive, tmp_path / "tree", limit=1000, max_members=100)
    assert (outside / "victim").read_text() == "original"


def test_extract_refuses_duplicate_names(tmp_path: Path) -> None:
    """Adversarial: a second member of the same name could replace the first (e.g. file by link)."""
    archive = _tar(
        tmp_path / "a.tar",
        (_info("a", size=1), b"x"),
        (_info("a", tarfile.SYMTYPE, link="/etc/passwd"), None),
    )
    with pytest.raises(GitError, match="duplicate entry"):
        _extract(archive, tmp_path / "tree", limit=1000, max_members=100)


def test_extract_refuses_a_directory_replacing_a_symlink(tmp_path: Path) -> None:
    """Adversarial: a directory member named like an earlier link would chmod through it."""
    archive = _tar(
        tmp_path / "a.tar",
        (_info("l", tarfile.SYMTYPE, link=str(tmp_path)), None),
        (_info("l", tarfile.DIRTYPE), None),
    )
    with pytest.raises(GitError, match="duplicate entry"):
        _extract(archive, tmp_path / "tree", limit=1000, max_members=100)


def test_extract_refuses_members_below_a_symlink_lexically_even_past_path_max(
    tmp_path: Path,
) -> None:
    """Adversarial (CVE-2025-4517 shape): resolution via realpath fails past PATH_MAX; names decide.

    Deep directories with short symlinks, a link pointing 16 levels up, then
    `escape -> <link>/../outside` and a symlink `escape/victim`. A resolver
    that treats an over-long component as a plain name is fooled; the rule
    "no member below a symlink member" is not.
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "victim").write_text("orig")
    tree = tmp_path / "tree"
    component = "d" * 247
    # As many levels as fit below the platform's PATH_MAX (16 on Linux's 4096, 3 on macOS's
    # 1024), so the directories can be created and only the resolved link runs past it.
    path_max = os.pathconf(tmp_path, "PC_PATH_MAX")
    room = path_max - len(str(tree)) - 2
    steps = string.ascii_lowercase[: min(16, room // (len(component) + 1))]
    assert len(str(tree)) + 1 + len(steps) * (len(component) + 1) + 254 > path_max
    members: list[tuple[tarfile.TarInfo, bytes | None]] = []
    path = ""
    for step in steps:
        members.append((_info(os.path.join(path, component), tarfile.DIRTYPE), None))
        members.append((_info(os.path.join(path, step), tarfile.SYMTYPE, link=component), None))
        path = os.path.join(path, component)
    link_path = os.path.join(*steps, "l" * 254)
    up = os.path.join(*[".."] * len(steps))
    members.append((_info(link_path, tarfile.SYMTYPE, link=up), None))
    members.append((_info("escape", tarfile.SYMTYPE, link=f"{link_path}/../outside"), None))
    members.append((_info("escape/victim", tarfile.SYMTYPE, link="/etc/passwd"), None))
    archive = tmp_path / "a.tar"
    with tarfile.open(archive, "w", format=tarfile.PAX_FORMAT) as handle:
        for info, _ in members:
            handle.addfile(info)
    with pytest.raises(GitError, match="unsafe path"):
        _extract(archive, tree, limit=10**6, max_members=1000)
    assert (outside / "victim").read_text() == "orig"


def test_extract_maps_a_file_colliding_with_an_implicit_directory_to_git_error(
    tmp_path: Path,
) -> None:
    """The tarfile module's own OSError (`a/b` creates `a/`, then a file `a`) becomes a GitError."""
    archive = _tar(
        tmp_path / "a.tar",
        (_info("a/b", size=1), b"x"),
        (_info("a", size=1), b"y"),
    )
    with pytest.raises(GitError, match="could not extract"):
        _extract(archive, tmp_path / "tree", limit=1000, max_members=100)


def test_killing_a_reaped_process_group_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After the leader is reaped its pid may be reused, so `_kill_group` must not signal it."""
    process = subprocess.Popen(["true"], start_new_session=True)
    process.wait()
    calls: list[int] = []
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: calls.append(pgid))
    module._kill_group(process)
    assert calls == []


def test_killing_a_group_of_exited_processes_is_not_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On macOS, signalling a group whose members have all exited but are not yet reaped fails."""
    process = subprocess.Popen(["true"], start_new_session=True)
    process.wait()
    process.returncode = None

    def refuse(pgid: int, signum: int) -> None:
        """Fail as macOS's killpg does for a group of zombies.

        Parameters
        ----------
        pgid
            The process group.
        signum
            The signal.

        Raises
        ------
        PermissionError
            Always.
        """
        raise PermissionError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(os, "killpg", refuse)
    module._kill_group(process)
    process.returncode = 0


@pytest.mark.parametrize(
    ("link", "child"),
    [
        ("A", "a/x"),
        ("\u00e9", "e\u0301/x"),
        ("e\u0301", "\u00e9/x"),
        ("a", "a\u200c/x"),
        ("a", "a\ufeff/x"),
        ("dir/L", "dir/l/x"),
    ],
    ids=["case", "nfc-vs-nfd", "nfd-vs-nfc", "zero-width-non-joiner", "bom", "nested-case"],
)
def test_extract_compares_names_folded_for_case_and_unicode_insensitive_filesystems(
    tmp_path: Path, link: str, child: str
) -> None:
    """Adversarial: on APFS/HFS+ `A` and `a` are one name, so a child of `a` is below link `A`."""
    outside = tmp_path / "outside"
    outside.mkdir()
    members = [(_info(link, tarfile.SYMTYPE, link=str(outside)), None)]
    members.append((_info(child, tarfile.SYMTYPE, link="/etc/passwd"), None))
    archive = _tar(tmp_path / "a.tar", *members)
    with pytest.raises(GitError, match="unsafe path"):
        _extract(archive, tmp_path / "tree", limit=1000, max_members=100)
    assert not (outside / "x").exists()


def test_extract_refuses_case_colliding_duplicates(tmp_path: Path) -> None:
    """`a` then `A` is one entry on a case-insensitive filesystem."""
    archive = _tar(
        tmp_path / "a.tar",
        (_info("a", size=1), b"x"),
        (_info("A", tarfile.SYMTYPE, link="/etc/passwd"), None),
    )
    with pytest.raises(
        GitError,
        match="'A' collides with 'a' when names are compared case- and normalisation-insensitively",
    ):
        _extract(archive, tmp_path / "tree", limit=1000, max_members=100)


def test_extract_maps_a_nul_in_a_pax_name_to_git_error(tmp_path: Path) -> None:
    """A NUL smuggled through a PAX path record is a GitError, not a ValueError."""
    info = _info("ab", size=1)
    info.pax_headers = {"path": "a\0b"}
    archive = tmp_path / "a.tar"
    with tarfile.open(archive, "w", format=tarfile.PAX_FORMAT) as handle:
        handle.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(GitError, match="could not extract"):
        _extract(archive, tmp_path / "tree", limit=1000, max_members=100)


def test_extract_caps_the_number_of_members(tmp_path: Path) -> None:
    """Many empty files cannot exhaust inodes: the member count is bounded."""
    archive = _tar(tmp_path / "a.tar", *((_info(f"f{i}"), b"") for i in range(3)))
    with pytest.raises(GitError, match="more than 2 files"):
        _extract(archive, tmp_path / "tree", limit=1000, max_members=2)
    _extract(archive, tmp_path / "ok", limit=1000, max_members=3)


def test_crafted_git_tree_cannot_write_outside_the_checkout(tmp_path: Path) -> None:
    """Adversarial, end to end: tree entries `l` and `l/victim.txt` (both symlinks) are refused."""
    outside = tmp_path / "outside"
    outside.mkdir()
    repo = tmp_path / "repos" / "analytics"
    make_repo(repo, _FILES)

    def blob(content: str) -> bytes:
        return bytes.fromhex(
            subprocess.run(
                ["git", "hash-object", "-w", "--stdin"],
                cwd=repo,
                input=content,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
        )

    entries = [(b"l", blob(str(outside))), (b"l/victim.txt", blob("/etc/passwd"))]
    body = b"".join(b"120000 " + name + b"\0" + sha for name, sha in entries)
    tree = (
        subprocess.run(
            ["git", "hash-object", "-w", "--literally", "-t", "tree", "--stdin"],
            cwd=repo,
            input=body,
            capture_output=True,
            check=True,
        )
        .stdout.decode()
        .strip()
    )
    evil = git(repo, "commit-tree", tree, "-m", "evil")
    git(repo, "update-ref", "refs/heads/main", evil)
    with pytest.raises(GitError):
        _fetch(tmp_path)
    assert not (outside / "victim.txt").exists()


def _process_with(marker: str) -> list[str]:
    """List command lines of live processes containing `marker`.

    Parameters
    ----------
    marker
        Text to look for in `/proc/*/cmdline`, or in what ``ps`` lists where there is no
        `/proc` (macOS).

    Returns
    -------
    list of str
        Matching command lines.
    """
    if not Path("/proc/self").exists():
        listing = subprocess.run(
            ["ps", "-A", "-ww", "-o", "args="], capture_output=True, text=True, check=True
        ).stdout.splitlines()
    else:
        listing = []
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                listing.append((entry / "cmdline").read_bytes().replace(b"\0", b" ").decode())
            except OSError:
                continue
    return [line for line in listing if marker in line and "pytest" not in line]


def _stub_git_leaving_a_child(tmp_path: Path, marker: str) -> Path:
    """Create a fake `git` that starts a long-lived child and waits for it.

    Parameters
    ----------
    tmp_path
        Pytest's per-test directory.
    marker
        Unique sleep duration identifying the child.

    Returns
    -------
    Path
        The directory to use as `PATH`.
    """
    stub = tmp_path / "bin"
    stub.mkdir()
    script = stub / "git"
    script.write_text(f"#!/bin/sh\n/bin/sleep {marker} &\nwait\n")
    script.chmod(0o755)
    return stub


def test_run_timeout_kills_the_whole_process_group(tmp_path: Path) -> None:
    """Adversarial: a helper started by git (git-remote-https, ssh) dies with it on timeout."""
    marker = "31.111"
    stub = _stub_git_leaving_a_child(tmp_path, marker)
    with pytest.raises(GitError, match="timed out"):
        module._run(["git", "fetch"], cwd=tmp_path, env={"PATH": str(stub)}, timeout=0.5)
    time.sleep(0.2)
    assert not _process_with(f"sleep {marker}")


def test_archive_timeout_kills_the_whole_process_group(tmp_path: Path) -> None:
    """The same for `git archive` and anything it spawned."""
    marker = "31.222"
    stub = _stub_git_leaving_a_child(tmp_path, marker)
    started = time.monotonic()
    with pytest.raises(GitError, match="timed out"):
        _archive(
            "a" * 40,
            cwd=tmp_path,
            env={"PATH": str(stub)},
            timeout=0.5,
            target=tmp_path / "x.tar",
            limit=1000,
        )
    assert time.monotonic() - started < 10
    time.sleep(0.2)
    assert not _process_with(f"sleep {marker}")


def _stub_git_script(tmp_path: Path, body: str) -> Path:
    """Create a fake `git` running a shell script body.

    Parameters
    ----------
    tmp_path
        Pytest's per-test directory.
    body
        Shell commands the fake runs.

    Returns
    -------
    Path
        The directory to put on ``PATH``.
    """
    stub = tmp_path / "bin"
    stub.mkdir()
    script = stub / "git"
    script.write_text(f"#!/bin/sh\n{body}\n")
    script.chmod(0o755)
    return stub


def _two_member_archive(tmp_path: Path) -> Path:
    """Write a tar whose second member starts at offset 1024.

    Parameters
    ----------
    tmp_path
        Pytest's per-test directory.

    Returns
    -------
    Path
        The archive.
    """
    return _tar(
        tmp_path / "source.tar",
        (_info("first.txt", size=1), b"x"),
        (_info("second.txt", size=1), b"y"),
    )


def test_archive_timeout_at_exit_zero_is_still_a_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Adversarial: the deadline hitting after git exited 0 must not pass a cut-off archive.

    The fake writes the first member, pauses, writes the rest and exits 0. The
    second wait for output "expires" only after that, as when the deadline
    falls right after a read: the bytes still in the pipe were never read.
    """
    source = _two_member_archive(tmp_path)
    stub = _stub_git_script(
        tmp_path, f"head -c 1024 {source}\nsleep 0.3\ntail -c +1025 {source}\nexit 0"
    )
    selector_class = module.selectors.DefaultSelector
    real_select = selector_class.select
    calls = 0

    def expire_once_after_exit(
        self: selectors.DefaultSelector, timeout: float | None = None
    ) -> list[tuple[selectors.SelectorKey, int]]:
        nonlocal calls
        calls += 1
        if calls == 2:
            time.sleep(0.8)
            return []
        return real_select(self, timeout)

    monkeypatch.setattr(selector_class, "select", expire_once_after_exit)
    with pytest.raises(GitError, match="git archive timed out"):
        _archive(
            "a" * 40,
            cwd=tmp_path,
            env={"PATH": f"{stub}:/usr/bin:/bin"},
            timeout=5,
            target=tmp_path / "x.tar",
            limit=100_000,
        )


def test_archive_without_the_end_marker_is_refused(tmp_path: Path) -> None:
    """Defence in depth: output lacking tar's two zero end blocks is incomplete, even at exit 0."""
    source = _two_member_archive(tmp_path)
    stub = _stub_git_script(tmp_path, f"head -c 1024 {source}\nexit 0")
    with pytest.raises(GitError, match="incomplete"):
        _archive(
            "a" * 40,
            cwd=tmp_path,
            env={"PATH": f"{stub}:/usr/bin:/bin"},
            timeout=30,
            target=tmp_path / "x.tar",
            limit=100_000,
        )


def test_archive_with_the_end_marker_is_accepted(tmp_path: Path) -> None:
    """A whole tar written by a fake git at exit 0 passes the end-marker check."""
    source = _two_member_archive(tmp_path)
    stub = _stub_git_script(tmp_path, f"cat {source}")
    target = tmp_path / "x.tar"
    _archive(
        "a" * 40,
        cwd=tmp_path,
        env={"PATH": f"{stub}:/usr/bin:/bin"},
        timeout=30,
        target=target,
        limit=100_000,
    )
    assert target.read_bytes() == source.read_bytes()


def test_archive_git_closing_stdout_without_exiting_times_out(tmp_path: Path) -> None:
    """A git that closes its output but never exits is killed at the deadline, not waited on."""
    stub = _stub_git_script(tmp_path, "exec >&-\nexec /bin/sleep 30")
    started = time.monotonic()
    with pytest.raises(GitError, match="git archive timed out"):
        _archive(
            "a" * 40,
            cwd=tmp_path,
            env={"PATH": f"{stub}:/usr/bin:/bin"},
            timeout=0.5,
            target=tmp_path / "x.tar",
            limit=1000,
        )
    assert time.monotonic() - started < 10


@pytest.mark.slow
def test_slow_https_server_leaves_no_git_process_behind(
    tmp_path: Path, served: tuple[Path, str]
) -> None:
    """After a fetch timeout no process still names the repository URL."""
    root, _ = served
    with GitHttpServer(root, tmp_path, delay_seconds=3) as server:
        url = server.url("analytics")
        with pytest.raises(GitError, match="timed out"):
            _https(
                tmp_path,
                server,
                GitCredential(token=server.token),
                limits=DeployLimits(git_command_seconds=1),
            )
        time.sleep(0.3)
        assert not _process_with(url)


def test_a_ref_naming_a_tree_is_not_a_commit(tmp_path: Path, local_sha: str) -> None:
    """A tag on a tree fetches fine but names no commit."""
    repo = tmp_path / "repos" / "analytics"
    git(repo, "tag", "treetag", "HEAD^{tree}")
    with pytest.raises(GitError, match="'treetag' does not name a commit"):
        _fetch(tmp_path, ref="treetag")


def test_a_commit_missing_after_the_fallback_fetch_is_reported(
    tmp_path: Path, served: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the remote moved between probe and fetch, the missing commit is named."""
    root, first = served
    commit(tmp_path / "src", {"assets/bronze/more.sql": "SELECT 2 AS id\n"})
    git(tmp_path / "src", "push", "--quiet", str(root / "analytics.git"), "main")
    git(root / "analytics.git", "config", "uploadpack.allowReachableSHA1InWant", "false")
    original = module._commit_of

    def vanished(
        name: str, *, repo: Path, env: Mapping[str, str], limits: DeployLimits, deadline: float
    ) -> str | None:
        if name == first and repo.name == "repo.git":
            return None
        return original(name, repo=repo, env=env, limits=limits, deadline=deadline)

    monkeypatch.setattr(module, "_commit_of", vanished)
    with (
        GitHttpServer(root, tmp_path, protocol_v2=False) as server,
        pytest.raises(GitError, match=f"commit {first} not found in the repository"),
    ):
        _https(tmp_path, server, GitCredential(token=server.token), ref=first)


def test_a_tree_sha_is_not_a_commit(tmp_path: Path, local_sha: str) -> None:
    """A SHA that names a tree is refused as not naming a commit."""
    tree = git(tmp_path / "repos" / "analytics", "rev-parse", "HEAD^{tree}")
    with pytest.raises(GitError, match=f"{tree} does not name a commit"):
        _fetch(tmp_path, ref=tree)


def test_annotated_tag_sha_is_peeled_after_the_fallback(
    tmp_path: Path, served: tuple[Path, str]
) -> None:
    """Protocol v0 fallback: the tag object's SHA resolves to the commit it points at."""
    root, first = served
    bare = root / "analytics.git"
    git(bare, "tag", "-a", "-m", "release", "v1", first)
    tag_object = git(bare, "rev-parse", "v1")
    git(bare, "tag", "-a", "-m", "outer", "v2", tag_object)
    git(bare, "tag", "-d", "v1")
    assert tag_object != first
    commit(tmp_path / "src", {"assets/bronze/more.sql": "SELECT 2 AS id\n"})
    git(tmp_path / "src", "push", "--quiet", str(bare), "main")
    git(bare, "config", "uploadpack.allowReachableSHA1InWant", "false")
    with GitHttpServer(root, tmp_path, protocol_v2=False) as server:
        fetched = _https(tmp_path, server, GitCredential(token=server.token), ref=tag_object)
    assert fetched.sha == first


def test_partial_clone_promisor_remote_in_the_source_is_never_contacted(tmp_path: Path) -> None:
    """Adversarial: a real partial clone missing blobs must not lazy-fetch from its promisor.

    The source is a `--filter=tree:0` clone, so serving HEAD needs blobs it
    lacks; a lazy fetch would run the promisor URL, which is re-pointed at an
    `ext::` command allowed by the source's own config. Nothing may run. Note:
    git 2.53's upload-pack disables lazy fetching itself, so this guards
    against a regression on older patched lines rather than failing without
    the worker's `GIT_NO_LAZY_FETCH=1` here (checked by experiment).
    """
    canary = tmp_path / "canary"
    origin = tmp_path / "origin"
    make_repo(origin, _FILES)
    git(origin, "config", "uploadpack.allowFilter", "true")
    repo = tmp_path / "repos" / "analytics"
    repo.parent.mkdir()
    git(
        tmp_path,
        "clone",
        "--quiet",
        "--no-checkout",
        "--filter=tree:0",
        f"file://{origin}",
        str(repo),
    )
    assert git(repo, "config", "remote.origin.promisor") == "true"
    git(repo, "config", "remote.origin.url", f"ext::sh -c 'touch {canary}' %S")
    git(repo, "config", "protocol.ext.allow", "always")
    git(repo, "config", "core.sshCommand", f"touch {canary}")
    with contextlib.suppress(GitError):
        _fetch(tmp_path)
    assert not canary.exists()


def test_a_dangling_commit_is_refused_even_where_protocol_v2_would_serve_it(
    tmp_path: Path, served: tuple[Path, str]
) -> None:
    """A force-pushed-away commit still in the server's object store is not deployable."""
    root, first = served
    bare = root / "analytics.git"
    dangling = commit(tmp_path / "src", {"assets/bronze/secret.sql": "SELECT 9 AS id\n"})
    git(tmp_path / "src", "push", "--quiet", str(bare), "main")
    git(tmp_path / "src", "push", "--quiet", "--force", str(bare), f"{first}:refs/heads/main")
    with (
        GitHttpServer(root, tmp_path, protocol_v2=True) as server,
        pytest.raises(GitError, match=f"commit {dangling} is not reachable"),
    ):
        _https(tmp_path, server, GitCredential(token=server.token), ref=dangling)


def test_looking_up_an_unreachable_sha_in_the_probe_never_lazily_fetches_it(
    tmp_path: Path, served: tuple[Path, str]
) -> None:
    """D15: the partial-clone probe must not download a dangling commit on demand."""
    root, first = served
    bare = root / "analytics.git"
    dangling = commit(tmp_path / "src", {"assets/bronze/secret.sql": "SELECT 9 AS id\n"})
    git(tmp_path / "src", "push", "--quiet", str(bare), "main")
    git(tmp_path / "src", "push", "--quiet", "--force", str(bare), f"{first}:refs/heads/main")
    with (
        GitHttpServer(root, tmp_path, protocol_v2=True) as server,
        pytest.raises(GitError, match=f"commit {dangling} is not reachable"),
    ):
        _https(tmp_path, server, GitCredential(token=server.token), ref=dangling)
    (probe,) = (tmp_path / "work").glob("fetch-*/probe.git")
    present = subprocess.run(  # nosec B603 B607 -- fixed argv in tests
        ["git", "cat-file", "-e", dangling],
        cwd=probe,
        env={**os.environ, "GIT_NO_LAZY_FETCH": "1"},
        capture_output=True,
        check=False,
    )
    assert present.returncode != 0
    assert module._tree_size(probe) < 50_000


@pytest.mark.parametrize("allow_filter", [True, False])
def test_sha_deploys_work_with_and_without_filter_support(
    tmp_path: Path, served: tuple[Path, str], monkeypatch: pytest.MonkeyPatch, allow_filter: bool
) -> None:
    """The probe asks for `--filter=tree:0`; a server without filter support sends everything."""
    root, first = served
    git(root / "analytics.git", "config", "uploadpack.allowFilter", str(allow_filter).lower())
    calls = _spy_runs(monkeypatch)
    with GitHttpServer(root, tmp_path) as server:
        assert _https(tmp_path, server, GitCredential(token=server.token), ref=first).sha == first
    assert any("--filter=tree:0" in argv for argv, _ in calls)


def test_a_commit_reachable_only_from_a_tag_deploys(tmp_path: Path, local_sha: str) -> None:
    """Tags count as reachability roots."""
    repo = tmp_path / "repos" / "analytics"
    git(repo, "tag", "v1")
    git(repo, "commit", "--quiet", "--amend", "-m", "rewritten")
    assert _fetch(tmp_path, ref=local_sha).sha == local_sha


def test_an_annotated_tag_sha_is_peeled_and_checked(tmp_path: Path, local_sha: str) -> None:
    """A tag object's SHA resolves to its commit, which a ref contains."""
    repo = tmp_path / "repos" / "analytics"
    git(repo, "tag", "-a", "v2", "-m", "release")
    tag_object = git(repo, "rev-parse", "v2")
    assert _fetch(tmp_path, ref=tag_object).sha == local_sha


def test_too_many_refs_are_refused_before_fetching(tmp_path: Path, local_sha: str) -> None:
    """`ls-remote` counts branches and tags first."""
    repo = tmp_path / "repos" / "analytics"
    for name in ("a", "b", "c"):
        git(repo, "tag", name)
    with pytest.raises(GitError, match="more than 2 branches and tags"):
        _fetch(tmp_path, ref=local_sha, limits=DeployLimits(fetch_refs=2))


def test_a_fetch_past_the_size_limit_is_refused(tmp_path: Path) -> None:
    """The temp repository's size is bounded (checked again after git exits)."""
    repo = tmp_path / "repos" / "analytics"
    make_repo(
        repo,
        {**_FILES, "assets/bronze/big.sql": "-- " + os.urandom(150_000).hex() + "\nSELECT 1\n"},
    )
    with pytest.raises(GitError, match="repository fetch exceeded 65536 bytes"):
        _fetch(tmp_path, ref="main", limits=DeployLimits(fetch_bytes=65_536))


def test_a_growing_fetch_is_killed_while_it_runs(
    tmp_path: Path, served: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The size is polled during the fetch and the process group killed past the limit."""
    root, _ = served
    monkeypatch.setattr(module, "_WATCH_SECONDS", 0.05)
    monkeypatch.setattr(module, "_tree_size", lambda path: 10**12)
    started = time.monotonic()
    with GitHttpServer(root, tmp_path, delay_seconds=1) as server:
        url = server.url("analytics")
        with pytest.raises(GitError, match="repository fetch exceeded 1 GiB"):
            _https(tmp_path, server, GitCredential(token=server.token))
        assert time.monotonic() - started < 5
        time.sleep(0.3)
        assert not _process_with(url)


def test_a_watched_command_that_never_finishes_times_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A size-bounded run still honours its timeout."""
    monkeypatch.setattr(module, "_WATCH_SECONDS", 0.05)
    stub = _stub_git_script(tmp_path, "exec /bin/sleep 30")
    started = time.monotonic()
    with pytest.raises(GitError, match="timed out"):
        module._run(
            ["git", "fetch"], cwd=tmp_path, env={"PATH": str(stub)}, timeout=0.3, max_bytes=10**9
        )
    assert time.monotonic() - started < 5


def test_a_watched_command_with_oversized_stdout_is_refused(tmp_path: Path) -> None:
    """Captured stdout counts against the bound."""
    stub = _stub_git_script(tmp_path, "/usr/bin/head -c 5000 /dev/zero | /usr/bin/tr '\\0' x")
    with pytest.raises(GitError, match="exceeded 1000 bytes"):
        module._run(
            ["git", "ls-remote"], cwd=tmp_path, env={"PATH": str(stub)}, timeout=10, max_bytes=1000
        )


@pytest.mark.parametrize("failing", ["ls-remote", "fetch"])
def test_failed_probe_ls_remote_and_fetch_are_classified(
    tmp_path: Path, local_sha: str, monkeypatch: pytest.MonkeyPatch, failing: str
) -> None:
    """A probe step's failure surfaces as the classified, redacted message."""
    original = module._run

    def fake(
        args: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout: float,
        max_bytes: int | None = None,
    ) -> subprocess.CompletedProcess[str]:
        if args[1] == failing:
            return _completed(128, "fatal: Authentication failed for 'https://h/x'")
        return original(args, cwd=cwd, env=env, timeout=timeout, max_bytes=max_bytes)

    monkeypatch.setattr(module, "_run", fake)
    with pytest.raises(GitError, match=AUTH_FAILED_MESSAGE):
        _fetch(tmp_path, ref=local_sha)


def test_failed_probe_init_is_reported(
    tmp_path: Path, local_sha: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A probe repository that cannot be created stops the deploy with git's text."""
    original = module._run
    inits = 0

    def fake(
        args: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout: float,
        max_bytes: int | None = None,
    ) -> subprocess.CompletedProcess[str]:
        nonlocal inits
        if args[1] == "init":
            inits += 1
            if inits == 2:
                return _completed(1, "fatal: probe disk")
        return original(args, cwd=cwd, env=env, timeout=timeout, max_bytes=max_bytes)

    monkeypatch.setattr(module, "_run", fake)
    with pytest.raises(GitError, match="git init failed: fatal: probe disk"):
        _fetch(tmp_path, ref=local_sha)


def test_a_failing_reachability_query_is_unreachable(
    tmp_path: Path, local_sha: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If `for-each-ref` itself fails the commit is not treated as reachable."""
    original = module._run

    def fake(
        args: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout: float,
        max_bytes: int | None = None,
    ) -> subprocess.CompletedProcess[str]:
        if args[1] == "for-each-ref":
            return _completed(128, "fatal: boom")
        return original(args, cwd=cwd, env=env, timeout=timeout, max_bytes=max_bytes)

    monkeypatch.setattr(module, "_run", fake)
    with pytest.raises(GitError, match="is not reachable"):
        _fetch(tmp_path, ref=local_sha)


def test_tree_size_counts_files_and_tolerates_vanishing_ones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sizes add up; a file deleted mid-walk is skipped; a missing root is zero."""
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "a").write_bytes(b"x" * 10)
    (tmp_path / "b").write_bytes(b"y" * 5)
    (tmp_path / "link").symlink_to(tmp_path / "b")
    assert module._tree_size(tmp_path) == 10 + 5 + len(str(tmp_path / "b"))
    assert module._tree_size(tmp_path / "missing") == 0
    real = os.lstat

    def vanishing(path: str, **kwargs: object) -> os.stat_result:
        if str(path).endswith("/b"):
            raise FileNotFoundError(path)
        return real(path, **kwargs)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(module.os, "lstat", vanishing)
    assert module._tree_size(tmp_path) == 10 + len(str(tmp_path / "b"))


def test_describe_limit_uses_gibibytes_when_whole() -> None:
    """The default 1 GiB fetch limit reads as such."""
    assert _describe_limit(1024**3) == "1 GiB"
    assert _describe_limit(3 * 1024**3) == "3 GiB"


def test_stdout_is_measured_in_bytes_not_characters(tmp_path: Path) -> None:
    """400 two-byte characters (800 bytes) exceed a 500-byte bound."""
    stub = _stub_git_script(
        tmp_path,
        "i=0\nwhile [ $i -lt 400 ]; do printf '\\303\\251'; i=$((i+1)); done",
    )
    with pytest.raises(GitError, match="exceeded 500 bytes"):
        module._run(
            ["git", "ls-remote"], cwd=tmp_path, env={"PATH": str(stub)}, timeout=10, max_bytes=500
        )


def test_many_pull_and_review_refs_do_not_count_against_the_ref_limit(
    tmp_path: Path, local_sha: str
) -> None:
    """Only branches and tags are counted; refs/pull/* and refs/changes/* are not."""
    repo = tmp_path / "repos" / "analytics"
    for number in range(8):
        git(repo, "update-ref", f"refs/pull/{number}/head", local_sha)
        git(repo, "update-ref", f"refs/changes/{number}/1", local_sha)
    assert _fetch(tmp_path, ref=local_sha, limits=DeployLimits(fetch_refs=3)).sha == local_sha


@pytest.mark.parametrize(
    "ref", ["refs/pull/1/head", "refs/changes/1/1", "refs/merge-requests/1/head"]
)
def test_ref_deploys_refuse_pull_and_review_namespaces(
    tmp_path: Path, local_sha: str, ref: str
) -> None:
    """The ref is refused before git runs, even if the remote has it."""
    repo = tmp_path / "repos" / "analytics"
    git(repo, "update-ref", "refs/pull/1/head", local_sha)
    with pytest.raises(GitError, match="is not a branch, a tag or HEAD"):
        _fetch(tmp_path, ref=ref)


def test_a_short_name_resolves_only_to_a_branch_or_tag(tmp_path: Path, local_sha: str) -> None:
    """`custom/topic` would resolve to `refs/custom/topic` in git; here it is not found."""
    repo = tmp_path / "repos" / "analytics"
    git(repo, "update-ref", "refs/custom/topic", local_sha)
    with pytest.raises(GitError, match="ref 'custom/topic' not found"):
        _fetch(tmp_path, ref="custom/topic")


def test_fully_qualified_heads_and_tags_deploy(tmp_path: Path, local_sha: str) -> None:
    """`refs/heads/X` and `refs/tags/X` are fetched as given."""
    repo = tmp_path / "repos" / "analytics"
    git(repo, "tag", "v1")
    assert _fetch(tmp_path, ref="refs/heads/main").sha == local_sha
    assert _fetch(tmp_path, ref="refs/tags/v1").sha == local_sha


def test_a_tag_wins_over_a_branch_of_the_same_short_name(tmp_path: Path, local_sha: str) -> None:
    """As in git's own rules, an ambiguous short name resolves to the tag."""
    repo = tmp_path / "repos" / "analytics"
    git(repo, "tag", "dual")
    newer = commit(repo, {"assets/bronze/more.sql": "SELECT 2 AS id\n"})
    git(repo, "branch", "dual", newer)
    assert _fetch(tmp_path, ref="dual").sha == local_sha


def test_an_annotated_tag_on_a_tree_does_not_name_a_commit(tmp_path: Path, local_sha: str) -> None:
    """The tag object is present in the probe but peels to no commit."""
    repo = tmp_path / "repos" / "analytics"
    git(repo, "tag", "-a", "treetag2", "-m", "t", "HEAD^{tree}")
    tag_object = git(repo, "rev-parse", "treetag2")
    with pytest.raises(GitError, match=f"{tag_object} does not name a commit"):
        _fetch(tmp_path, ref=tag_object)


def test_failed_ref_listing_is_classified(
    tmp_path: Path, local_sha: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rejected credential while resolving a short name is the auth message."""
    original = module._run

    def fake(
        args: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout: float,
        max_bytes: int | None = None,
    ) -> subprocess.CompletedProcess[str]:
        if args[1] == "ls-remote":
            return _completed(128, "fatal: Authentication failed for 'https://h/x'")
        return original(args, cwd=cwd, env=env, timeout=timeout, max_bytes=max_bytes)

    monkeypatch.setattr(module, "_run", fake)
    with pytest.raises(GitError, match=AUTH_FAILED_MESSAGE):
        _fetch(tmp_path, ref="main")


def test_a_short_name_that_only_exists_as_a_pull_ref_is_not_found(
    tmp_path: Path, local_sha: str
) -> None:
    """`pull/1/head` is a legal short name; resolution finds no branch or tag of that name."""
    repo = tmp_path / "repos" / "analytics"
    git(repo, "update-ref", "refs/pull/1/head", local_sha)
    with pytest.raises(GitError, match="ref 'pull/1/head' not found"):
        _fetch(tmp_path, ref="pull/1/head")


def test_a_branch_named_like_a_review_namespace_deploys(tmp_path: Path, local_sha: str) -> None:
    """`environments/prod` is a real branch name and must not be refused."""
    repo = tmp_path / "repos" / "analytics"
    git(repo, "branch", "environments/prod")
    assert _fetch(tmp_path, ref="environments/prod").sha == local_sha


@pytest.mark.parametrize(
    ("ref", "decoy"),
    [
        ("refs/heads/foo", "refs/refs/heads/foo"),
        ("refs/tags/bar", "refs/remotes/refs/tags/bar"),
    ],
)
def test_qualified_refs_are_matched_exactly_not_by_git_dwim(
    tmp_path: Path, local_sha: str, ref: str, decoy: str
) -> None:
    """A decoy in another namespace must not satisfy `refs/heads/foo` / `refs/tags/bar`."""
    repo = tmp_path / "repos" / "analytics"
    decoy_sha = commit(repo, {"assets/bronze/decoy.sql": "SELECT 2 AS id\n"})
    git(repo, "update-ref", decoy, decoy_sha)
    git(repo, "reset", "--quiet", "--hard", local_sha)
    with pytest.raises(GitError, match=f"ref '{ref}' not found"):
        _fetch(tmp_path, ref=ref)


def test_a_branch_deleted_between_listing_and_fetch_is_refused(
    tmp_path: Path, local_sha: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The listing found `refs/heads/foo`; it is gone at fetch time: not found, no fallback."""
    repo = tmp_path / "repos" / "analytics"
    git(repo, "branch", "foo")
    original = module._run

    def racing(
        args: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout: float,
        max_bytes: int | None = None,
    ) -> subprocess.CompletedProcess[str]:
        done = original(args, cwd=cwd, env=env, timeout=timeout, max_bytes=max_bytes)
        if args[1] == "ls-remote":
            git(repo, "branch", "-D", "foo")
        return done

    monkeypatch.setattr(module, "_run", racing)
    with pytest.raises(GitError, match="ref 'foo' not found"):
        _fetch(tmp_path, ref="foo")


def test_a_branch_swapped_for_a_decoy_between_listing_and_fetch_is_refused(
    tmp_path: Path, local_sha: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deleting `foo` and planting `refs/refs/heads/foo` must not make git DWIM to the decoy."""
    repo = tmp_path / "repos" / "analytics"
    git(repo, "branch", "foo")
    decoy_sha = commit(repo, {"assets/bronze/decoy.sql": "SELECT 2 AS id\n"})
    git(repo, "reset", "--quiet", "--hard", local_sha)
    original = module._run

    def racing(
        args: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout: float,
        max_bytes: int | None = None,
    ) -> subprocess.CompletedProcess[str]:
        done = original(args, cwd=cwd, env=env, timeout=timeout, max_bytes=max_bytes)
        if args[1] == "ls-remote":
            git(repo, "branch", "-D", "foo")
            git(repo, "update-ref", "refs/refs/heads/foo", decoy_sha)
        return done

    monkeypatch.setattr(module, "_run", racing)
    with pytest.raises(GitError, match="'refs/heads/foo' changed while it was being fetched"):
        _fetch(tmp_path, ref="refs/heads/foo")


def test_non_utf8_output_from_git_is_decoded_leniently(tmp_path: Path) -> None:
    """Hostile bytes in a server's output are replaced, not raised as UnicodeDecodeError."""
    stub = _stub_git_script(tmp_path, "printf '\\377\\376 ok'")
    done = module._run(
        ["git", "ls-remote"], cwd=tmp_path, env={"PATH": str(stub)}, timeout=10, max_bytes=1000
    )
    assert done.stdout.endswith(" ok")


def test_an_error_while_watching_kills_the_process_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Any exception inside the polling loop still kills git and its children."""
    marker = str(os.getpid() + 7777)
    stub = _stub_git_script(tmp_path, f"/bin/sleep {marker} &\nwait")
    monkeypatch.setattr(module, "_WATCH_SECONDS", 0.05)

    def boom(path: Path) -> int:
        raise RuntimeError("walk failed")

    monkeypatch.setattr(module, "_tree_size", boom)
    with pytest.raises(RuntimeError, match="walk failed"):
        module._run(
            ["git", "fetch"], cwd=tmp_path, env={"PATH": str(stub)}, timeout=10, max_bytes=10**9
        )
    time.sleep(0.2)
    assert not _process_with(f"sleep {marker}")


# --- HEAD deploys resolve exactly ------------------------------------------------

_UNBORN = "the repository's HEAD does not point to a commit; deploy a branch, tag or commit"


def _unborn_head_with_decoy(repo: Path, sha: str, decoy: str) -> str:
    """Point the source's HEAD at a missing branch and plant `decoy` at another commit.

    Parameters
    ----------
    repo
        The source repository.
    sha
        Its current commit.
    decoy
        The ref git's DWIM would try for ``HEAD``.

    Returns
    -------
    str
        The decoy's commit.
    """
    decoy_sha = commit(repo, {"assets/bronze/decoy.sql": "SELECT 2 AS id\n"})
    git(repo, "reset", "--quiet", "--hard", sha)
    git(repo, "update-ref", decoy, decoy_sha)
    git(repo, "symbolic-ref", "HEAD", "refs/heads/missing")
    return decoy_sha


@pytest.mark.parametrize("decoy", ["refs/remotes/HEAD", "refs/HEAD"])
def test_an_unborn_head_is_refused_even_with_a_dwim_decoy(
    tmp_path: Path, local_sha: str, decoy: str
) -> None:
    """`git fetch origin HEAD` would DWIM to the decoy; the exact listing refuses instead."""
    _unborn_head_with_decoy(tmp_path / "repos" / "analytics", local_sha, decoy)
    with pytest.raises(GitError, match=_UNBORN):
        _fetch(tmp_path)
    with pytest.raises(GitError, match=_UNBORN):
        _fetch(tmp_path, ref="HEAD")


def test_an_unborn_head_without_a_decoy_is_refused(tmp_path: Path, local_sha: str) -> None:
    """An unborn HEAD is the clear message, not git's `couldn't find remote ref`."""
    git(tmp_path / "repos" / "analytics", "symbolic-ref", "HEAD", "refs/heads/missing")
    with pytest.raises(GitError, match=_UNBORN):
        _fetch(tmp_path)


def test_head_follows_a_symbolic_head_to_another_branch(tmp_path: Path, local_sha: str) -> None:
    """HEAD names whatever commit the server's HEAD points at."""
    repo = tmp_path / "repos" / "analytics"
    git(repo, "checkout", "--quiet", "-b", "trunk")
    newer = commit(repo, {"assets/bronze/more.sql": "SELECT 2 AS id\n"})
    assert _fetch(tmp_path).sha == newer


def test_head_moved_to_a_decoy_between_listing_and_fetch_is_refused(
    tmp_path: Path, local_sha: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HEAD goes unborn and a decoy appears after the listing: the OID check refuses."""
    repo = tmp_path / "repos" / "analytics"
    decoy_sha = commit(repo, {"assets/bronze/decoy.sql": "SELECT 2 AS id\n"})
    git(repo, "reset", "--quiet", "--hard", local_sha)
    original = module._run

    def racing(
        args: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout: float,
        max_bytes: int | None = None,
    ) -> subprocess.CompletedProcess[str]:
        done = original(args, cwd=cwd, env=env, timeout=timeout, max_bytes=max_bytes)
        if args[1] == "ls-remote":
            git(repo, "update-ref", "refs/remotes/HEAD", decoy_sha)
            git(repo, "symbolic-ref", "HEAD", "refs/heads/missing")
        return done

    monkeypatch.setattr(module, "_run", racing)
    with pytest.raises(GitError, match="'HEAD' changed while it was being fetched"):
        _fetch(tmp_path)


def test_head_moving_to_another_commit_between_listing_and_fetch_is_refused(
    tmp_path: Path, local_sha: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A push between the listing and the fetch changes HEAD's commit: try again."""
    repo = tmp_path / "repos" / "analytics"
    original = module._run

    def racing(
        args: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout: float,
        max_bytes: int | None = None,
    ) -> subprocess.CompletedProcess[str]:
        done = original(args, cwd=cwd, env=env, timeout=timeout, max_bytes=max_bytes)
        if args[1] == "ls-remote":
            commit(repo, {"assets/bronze/more.sql": "SELECT 2 AS id\n"})
        return done

    monkeypatch.setattr(module, "_run", racing)
    with pytest.raises(GitError, match="'HEAD' changed while it was being fetched"):
        _fetch(tmp_path)


def test_https_head_is_resolved_and_an_unborn_head_refused(
    tmp_path: Path, served: tuple[Path, str]
) -> None:
    """Over the smart-HTTP protocol HEAD resolves, and an unborn one is refused."""
    root, sha = served
    with GitHttpServer(project_root=root, certificate_dir=tmp_path) as server:
        assert _https(tmp_path, server, GitCredential(token=server.token)).sha == sha
        bare = root / "analytics.git"
        git(bare, "update-ref", "refs/remotes/HEAD", sha)
        git(bare, "symbolic-ref", "HEAD", "refs/heads/missing")
        with pytest.raises(GitError, match=_UNBORN):
            _https(tmp_path, server, GitCredential(token=server.token))


def test_failed_head_listing_is_classified(
    tmp_path: Path, local_sha: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rejected credential while listing HEAD is the auth message."""
    original = module._run

    def fake(
        args: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout: float,
        max_bytes: int | None = None,
    ) -> subprocess.CompletedProcess[str]:
        if args[1] == "ls-remote":
            return _completed(128, "fatal: Authentication failed for 'https://h/x'")
        return original(args, cwd=cwd, env=env, timeout=timeout, max_bytes=max_bytes)

    monkeypatch.setattr(module, "_run", fake)
    with pytest.raises(GitError, match=AUTH_FAILED_MESSAGE):
        _fetch(tmp_path)


# --- operator proxies and CA bundles ---------------------------------------------


def _config_of(env: Mapping[str, str]) -> dict[str, str]:
    """Collect the ``GIT_CONFIG_KEY_n``/``VALUE_n`` pairs of a git environment.

    Parameters
    ----------
    env
        A `git_environment` result.

    Returns
    -------
    dict
        Setting name to value (the last one wins, as in git).
    """
    return {
        env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"]
        for i in range(int(env["GIT_CONFIG_COUNT"]))
    }


_PROXY_ENV = ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY", "https_proxy", "http_proxy")


def test_https_fetch_goes_through_the_operator_proxy(
    tmp_path: Path, served: tuple[Path, str]
) -> None:
    """`http.proxy` makes git tunnel through the configured proxy with CONNECT."""
    root, sha = served
    with GitHttpServer(root, tmp_path) as server, ConnectProxy() as proxy:
        fetched = _https(
            tmp_path, server, GitCredential(token=server.token), https_proxy=proxy.url()
        )
    assert fetched.sha == sha
    assert proxy.connects
    assert set(proxy.connects) == {server.url("x").split("/")[2]}


def test_no_proxy_bypasses_the_proxy(tmp_path: Path, served: tuple[Path, str]) -> None:
    """A host in the operator's `no_proxy` list is fetched directly."""
    root, sha = served
    with GitHttpServer(root, tmp_path) as server, ConnectProxy() as proxy:
        fetched = _https(
            tmp_path,
            server,
            GitCredential(token=server.token),
            https_proxy=proxy.url(),
            no_proxy="127.0.0.1",
        )
    assert fetched.sha == sha
    assert proxy.connects == []


@pytest.mark.slow
def test_proxy_credentials_are_sent_and_never_shown(
    tmp_path: Path, served: tuple[Path, str]
) -> None:
    """User and password in the proxy URL authenticate; a rejected one is redacted in the error."""
    root, sha = served
    with GitHttpServer(root, tmp_path) as server, ConnectProxy(credentials="svc:p@ss") as proxy:
        credential = GitCredential(token=server.token)
        assert _https(tmp_path, server, credential, https_proxy=proxy.url("svc:p%40ss")).sha == sha
        with pytest.raises(GitError) as raised:
            _https(tmp_path, server, credential, https_proxy=proxy.url("svc:wr0ngpw"))
    assert "wr0ngpw" not in str(raised.value)
    assert "svc:" not in str(raised.value)


def test_proxy_settings_are_passed_explicitly_and_nothing_is_inherited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the configured proxy, bypass list and CA reach git, nothing the worker inherited."""
    for name in _PROXY_ENV:
        monkeypatch.setenv(name, "http://inherited.invalid:3128")
    calls = _spy_runs(monkeypatch)
    ca = tmp_path / "ca.pem"
    ca.write_text("x")
    with pytest.raises(GitError):
        _fetch(
            tmp_path,
            kind=RepositoryKind.HTTPS,
            url="https://127.0.0.1:1/analytics.git",
            credential=GitCredential(token="t"),
            https_proxy="http://proxy.example:3128",
            no_proxy="internal.example,.corp",
            ca_bundle=ca,
        )
    env = calls[0][1]
    config = _config_of(env)
    assert config["http.proxy"] == "http://proxy.example:3128"
    assert config["http.sslCAInfo"] == str(ca)
    assert env["no_proxy"] == "internal.example,.corp"
    assert not any(name in env for name in _PROXY_ENV if name != "no_proxy")
    assert "inherited.invalid" not in repr(env)


def test_unconfigured_proxy_sets_nothing_and_ignores_the_workers_environment(
    tmp_path: Path, served: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without operator settings git gets no proxy, whatever the worker's environment says."""
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("https_proxy", "http://127.0.0.1:1")
    calls = _spy_runs(monkeypatch)
    root, sha = served
    with GitHttpServer(root, tmp_path) as server:
        assert _https(tmp_path, server, GitCredential(token=server.token)).sha == sha
    env = calls[0][1]
    assert "http.proxy" not in _config_of(env)
    assert not any(name in env for name in (*_PROXY_ENV, "no_proxy"))


@pytest.mark.parametrize("kind", [RepositoryKind.LOCAL, RepositoryKind.SSH])
def test_proxy_settings_apply_to_https_repositories_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: RepositoryKind
) -> None:
    """Local and SSH fetches never see the HTTPS proxy settings."""
    make_repo(tmp_path / "repos" / "analytics", _FILES)
    calls = _spy_runs(monkeypatch)
    with contextlib.suppress(GitError):
        _fetch(
            tmp_path,
            kind=kind,
            url=str(tmp_path / "repos" / "analytics")
            if kind is RepositoryKind.LOCAL
            else "ssh://git@127.0.0.1:1/analytics.git",
            https_proxy="http://proxy.example:3128",
            no_proxy="x",
        )
    env = calls[0][1]
    assert "http.proxy" not in _config_of(env)
    assert "no_proxy" not in env


def test_proxy_secrets_cover_every_form_of_the_proxy_credential() -> None:
    """The password, `user:password`, their encodings and base64 are all redacted."""
    secrets = module.proxy_secrets("http://svc:p%40ss@proxy.example:3128")
    assert {"p@ss", "p%40ss", "svc:p@ss", "svc:p%40ss"} <= set(secrets)
    assert base64.b64encode(b"svc:p@ss").decode() in secrets
    text = redact("proxy svc:p@ss and Basic " + base64.b64encode(b"svc:p@ss").decode(), secrets)
    assert "p@ss" not in text
    assert "c3ZjOnBAc3M" not in text
    assert module.proxy_secrets("http://proxy.example:3128") == []
    assert module.proxy_secrets(None) == []
    assert module.proxy_secrets("http://svc@proxy.example") == ["svc"]


def test_an_https_fetch_with_a_custom_ca_bundle_still_verifies_the_server(
    tmp_path: Path, served: tuple[Path, str]
) -> None:
    """A wrong CA bundle is refused: operator-provided trust replaces, it never disables, TLS."""
    root, _ = served
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    with (
        GitHttpServer(root, tmp_path) as server,
        GitHttpServer(root, other_dir) as unrelated,
        pytest.raises(GitError),
    ):
        _fetch(
            tmp_path,
            kind=RepositoryKind.HTTPS,
            url=server.url("analytics"),
            credential=GitCredential(token=server.token),
            ca_bundle=unrelated.ca_file,
        )


def test_proxy_credentials_in_git_stderr_are_redacted_from_the_error(
    tmp_path: Path, local_sha: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A proxy URL echoed by git never reaches the stored message with its password."""
    original = module._run

    def fake(
        args: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout: float,
        max_bytes: int | None = None,
    ) -> subprocess.CompletedProcess[str]:
        if args[1] == "ls-remote":
            return _completed(
                128, "fatal: proxy http://svc:p%40ss@proxy.example:3128 said svc:p@ss"
            )
        return original(args, cwd=cwd, env=env, timeout=timeout, max_bytes=max_bytes)

    monkeypatch.setattr(module, "_run", fake)
    with pytest.raises(GitError) as raised:
        _fetch(
            tmp_path,
            kind=RepositoryKind.HTTPS,
            url="https://127.0.0.1:1/analytics.git",
            ref="main",
            credential=GitCredential(token="t"),
            https_proxy="http://svc:p%40ss@proxy.example:3128",
        )
    assert "p@ss" not in str(raised.value)
    assert "p%40ss" not in str(raised.value)
    assert "svc:" not in str(raised.value)


def _run_askpass(
    tmp_path: Path, prompt: str, url: str = "https://git.corp:8443/a.git"
) -> tuple[int, str]:
    """Run the askpass helper for `url`'s credential with `prompt`.

    Parameters
    ----------
    tmp_path
        Pytest's per-test directory.
    prompt
        The prompt git passes as ``$1``.
    url
        The repository URL the helper is built for.

    Returns
    -------
    tuple of (int, str)
        Exit status and stdout.
    """
    directory = Path(tempfile.mkdtemp(dir=tmp_path))
    script = module._askpass(directory, GitCredential(username="bob", token="tok-123"), url)
    done = subprocess.run(  # nosec B603 -- test runs the generated helper
        [str(script), prompt], capture_output=True, text=True, check=False
    )
    return done.returncode, done.stdout


def test_askpass_matches_a_percent_encoded_username_exactly(tmp_path: Path) -> None:
    """Git prints `dep@loy` as `dep%40loy`; only that exact form is answered."""
    directory = Path(tempfile.mkdtemp(dir=tmp_path))
    credential = GitCredential(username="dep@loy/x", token="tok-123")
    script = module._askpass(directory, credential, "https://git.corp/a.git")

    def ask(prompt: str) -> tuple[int, str]:
        done = subprocess.run(  # nosec B603 -- test runs the generated helper
            [str(script), prompt], capture_output=True, text=True, check=False
        )
        return done.returncode, done.stdout

    assert ask("Password for 'https://dep%40loy%2Fx@git.corp': ") == (0, "tok-123")
    assert ask("Password for 'https://dep@loy/x@git.corp': ")[0] == 1


def test_askpass_answers_only_prompts_for_the_repository_origin(tmp_path: Path) -> None:
    """The repository token is given to git's prompt for the repository and to nothing else."""
    assert _run_askpass(tmp_path, "Username for 'https://git.corp:8443': ") == (0, "bob")
    assert _run_askpass(tmp_path, "Password for 'https://bob@git.corp:8443': ") == (0, "tok-123")
    for prompt in (
        "Password for 'https://x@git.corp:8443'@proxy.evil:3128': ",
        "Password for 'https://bob@git.corp:8443'@proxy.evil:3128': ",
        "Password for 'https://x@y@git.corp:8443': ",
        "Password for 'https://other@git.corp:8443': ",
        "Password for 'https://git.corp:8443': ",
        "Password for 'https://bob@git.corp:9999': ",
        "Password for 'http://bob@git.corp:8443': ",
        "Password for 'https://bob@git.corp:8443.evil': ",
        "Password for 'https://bob@git.corp:8443/path': ",
        "Username for 'http://git.corp:8443': ",
        "Username for 'https://git.corp:8443.evil': ",
        "Username for 'https://git.corp:8443'@proxy.evil:3128': ",
        "Password for 'http://svc@proxy.corp:3128': ",
        "Username for 'http://proxy.corp:3128': ",
        "Password for 'https://git.corp.evil:8443': ",
        "Password for 'https://git.corp:9999': ",
        "Enter passphrase for key '/x': ",
        "",
    ):
        assert _run_askpass(tmp_path, prompt) == (1, ""), prompt


def test_the_token_never_reaches_a_proxy_that_asks_for_a_password(
    tmp_path: Path, served: tuple[Path, str]
) -> None:
    """A username-only proxy URL is refused up front; the proxy sees no repository token."""
    root, _ = served
    with (
        GitHttpServer(root, tmp_path) as server,
        ConnectProxy(credentials="svc:pw") as proxy,
        pytest.raises(GitError, match="proxy URL must include a password"),
    ):
        _https(tmp_path, server, GitCredential(token=server.token), https_proxy=proxy.url("svc"))
    assert proxy.authorizations == []


def test_the_askpass_guard_alone_keeps_the_token_from_a_username_only_proxy(
    tmp_path: Path, served: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even if the URL check were bypassed, git's proxy prompt gets no token."""
    monkeypatch.setattr(module, "validate_proxy_url", lambda value: None)
    root, _ = served
    with (
        GitHttpServer(root, tmp_path) as server,
        ConnectProxy(credentials="svc:pw") as proxy,
        pytest.raises(GitError),
    ):
        _https(tmp_path, server, GitCredential(token=server.token), https_proxy=proxy.url("svc"))
    token_forms = {
        "Basic " + base64.b64encode(f"svc:{server.token}".encode()).decode(),
        "Basic " + base64.b64encode(f"oauth2:{server.token}".encode()).decode(),
    }
    assert not token_forms & {a for a in proxy.authorizations if a}


def test_an_https_proxy_trusts_the_ca_bundle_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A TLS proxy behind the internal CA is verified with the same bundle."""
    calls = _spy_runs(monkeypatch)
    ca = tmp_path / "ca.pem"
    ca.write_text("x")
    for proxy, expected in (("https://proxy.corp:3128", True), ("http://proxy.corp:3128", False)):
        with pytest.raises(GitError):
            _fetch(
                tmp_path,
                kind=RepositoryKind.HTTPS,
                url="https://127.0.0.1:1/analytics.git",
                credential=GitCredential(token="t"),
                https_proxy=proxy,
                ca_bundle=ca,
            )
        config = _config_of(calls[-1][1])
        assert (config.get("http.proxySSLCAInfo") == str(ca)) is expected
        calls.clear()


def test_fetch_refuses_a_malformed_proxy_url_even_without_the_settings_validator(
    tmp_path: Path, local_sha: str
) -> None:
    """`fetch_commit` re-validates the proxy: unencoded '@' and a missing password are refused."""
    for proxy in ("http://svc:p@ss@proxy.corp:3128", "http://svc@proxy.corp:3128", "ftp://x"):
        with pytest.raises(GitError) as raised:
            _fetch(
                tmp_path,
                kind=RepositoryKind.HTTPS,
                url="https://127.0.0.1:1/analytics.git",
                https_proxy=proxy,
            )
        assert "ss@" not in str(raised.value)
        assert "svc" not in str(raised.value)


def test_archive_failure_message_is_redacted(tmp_path: Path) -> None:
    """A secret or URL userinfo in `git archive`'s stderr never reaches the stored error."""
    stub = _stub_git_script(
        tmp_path, "echo 'fatal: tok-9f3 at https://bob:tok-9f3@git.corp/a.git' >&2\nexit 128"
    )
    with pytest.raises(GitError, match="git archive failed") as info:
        _archive(
            "a" * 40,
            cwd=tmp_path,
            env={"PATH": f"{stub}:/usr/bin:/bin"},
            timeout=30,
            target=tmp_path / "x.tar",
            limit=1000,
            secrets=["tok-9f3"],
        )
    assert "tok-9f3" not in str(info.value)
    assert "bob" not in str(info.value)
    assert "***" in str(info.value)


def test_archive_read_error_is_a_git_error_and_git_is_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An `OSError` while reading git's output is a `GitError`, not an escaping `OSError`."""
    stub = _stub_git_script(tmp_path, "sleep 30")

    def broken(descriptor: int, size: int) -> bytes:
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(module.os, "read", broken)
    started = time.monotonic()
    with pytest.raises(GitError, match=r"git archive failed: .*Input/output error"):
        _archive(
            "a" * 40,
            cwd=tmp_path,
            env={"PATH": f"{stub}:/usr/bin:/bin"},
            timeout=30,
            target=tmp_path / "x.tar",
            limit=1000,
        )
    assert time.monotonic() - started < 10


def test_archive_target_that_cannot_be_written_is_a_git_error(tmp_path: Path) -> None:
    """The output file failing to open (or fill up) is a `GitError` too."""
    with pytest.raises(GitError, match="git archive failed"):
        _archive(
            "a" * 40,
            cwd=tmp_path,
            env={"PATH": "/usr/bin:/bin"},
            timeout=30,
            target=tmp_path / "missing-dir" / "x.tar",
            limit=1000,
        )


def test_credential_files_that_cannot_be_written_are_git_errors(tmp_path: Path) -> None:
    """`_trust_source`, `_askpass` and `_ssh_command` raise `GitError`, never a bare `OSError`."""
    missing = tmp_path / "gone"
    with pytest.raises(GitError, match="could not write"):
        module._trust_source(missing / "gitconfig", "/repos/a")
    with pytest.raises(GitError, match="could not write"):
        module._askpass(missing, GitCredential(token="t"), "https://git.corp/a.git")
    with pytest.raises(GitError, match="could not write"):
        _ssh_command(missing, GitCredential(private_key="k"), "h ssh-ed25519 AAAA\n")


def test_a_hostile_ls_remote_listing_is_cut_off_at_the_stdout_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ls-remote's stdout is streamed and capped, not buffered up to `fetch_bytes`."""
    monkeypatch.setattr(module, "_MAX_STDOUT", 10_240)
    stub = _stub_git_script(tmp_path, f"exec /usr/bin/yes '{'0' * 40}\trefs/heads/x'")
    started = time.monotonic()
    with pytest.raises(GitError, match=r"git ls-remote output exceeded 10240 bytes"):
        module._resolve_ref(
            "main",
            repo=tmp_path,
            env={"PATH": str(stub)},
            limits=DeployLimits(git_command_seconds=20),
            deadline=time.monotonic() + 30,
            secrets=[],
        )
    assert time.monotonic() - started < 10


def test_an_unbounded_command_is_capped_too_and_stderr_is_not_kept_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every git command's stdout has the cap; stderr past its own bound is dropped."""
    monkeypatch.setattr(module, "_MAX_STDOUT", 100)
    monkeypatch.setattr(module, "_MAX_STDERR_READ", 10)
    loud = _stub_git_script(tmp_path, "/usr/bin/head -c 5000 /dev/zero >&2; exit 3")
    done = module._run(["git", "rev-parse"], cwd=tmp_path, env={"PATH": str(loud)}, timeout=10)
    assert (done.returncode, done.stdout, len(done.stderr)) == (3, "", 10)
    (loud / "git").write_text("#!/bin/sh\nexec /usr/bin/yes\n")
    with pytest.raises(GitError, match="git rev-parse output exceeded 100 bytes"):
        module._run(["git", "rev-parse"], cwd=tmp_path, env={"PATH": str(loud)}, timeout=10)


def test_a_command_keeps_its_stdout_stderr_and_exit_code(tmp_path: Path) -> None:
    """Within the cap, the streamed read returns what `communicate` did."""
    stub = _stub_git_script(tmp_path, "printf 'a\\tb\\n'; printf 'oops' >&2; exit 3")
    done = module._run(["git", "ls-remote"], cwd=tmp_path, env={"PATH": str(stub)}, timeout=10)
    assert (done.returncode, done.stdout, done.stderr) == (3, "a\tb\n", "oops")


def test_a_command_whose_pipes_close_but_which_never_exits_times_out(tmp_path: Path) -> None:
    """Closed pipes are not an exit: the wait is bounded and the group killed."""
    stub = _stub_git_script(tmp_path, "exec >/dev/null 2>&1\nexec /bin/sleep 30")
    started = time.monotonic()
    with pytest.raises(GitError, match="timed out"):
        module._run(["git", "fetch"], cwd=tmp_path, env={"PATH": str(stub)}, timeout=0.3)
    assert time.monotonic() - started < 5


@pytest.mark.parametrize("graph", ["written", "exits-nonzero", "raises"])
def test_the_probe_writes_a_commit_graph_before_the_reachability_check(
    tmp_path: Path, local_sha: str, monkeypatch: pytest.MonkeyPatch, graph: str
) -> None:
    """`for-each-ref --contains` walks a commit-graph; writing it is best effort."""
    original = module._run
    seen: list[str] = []

    def recording(
        args: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout: float,
        max_bytes: int | None = None,
    ) -> subprocess.CompletedProcess[str]:
        seen.append(args[1])
        if args[1] == "commit-graph":
            assert list(args[2:]) == ["write", "--reachable"]
            assert cwd.name == "probe.git"
            if graph == "exits-nonzero":
                return _completed(1, "error: cannot write")
            if graph == "raises":
                raise GitError("git commit-graph timed out after 120s")
        return original(args, cwd=cwd, env=env, timeout=timeout, max_bytes=max_bytes)

    monkeypatch.setattr(module, "_run", recording)
    assert _fetch(tmp_path, ref=local_sha).sha == local_sha
    assert seen.index("commit-graph") + 1 == seen.index("for-each-ref")


def test_the_commit_graph_does_not_count_against_the_fetch_size_limit(
    tmp_path: Path, local_sha: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A probe just under `fetch_bytes` still deploys: the graph file is not measured."""
    real = module._tree_size

    def over_the_limit_once_the_graph_exists(path: Path) -> int:
        info = path / "objects" / "info"
        if (info / "commit-graph").exists() or (info / "commit-graphs").exists():
            return 10**12
        return real(path)

    monkeypatch.setattr(module, "_tree_size", over_the_limit_once_the_graph_exists)
    assert _fetch(tmp_path, ref=local_sha).sha == local_sha


@pytest.fixture
def many_open_files() -> Iterator[None]:
    """Hold more than 1024 descriptors open, so the next pipe gets a number `select` rejects.

    Yields
    ------
    None
        While the descriptors are open.
    """
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    wanted = 1200
    if hard != resource.RLIM_INFINITY and hard < wanted:
        pytest.skip("RLIMIT_NOFILE's hard limit is too low to open 1024 descriptors")
    if soft != resource.RLIM_INFINITY and soft < wanted:
        resource.setrlimit(resource.RLIMIT_NOFILE, (wanted, hard))
    held: list[int] = []
    try:
        while not held or held[-1] < 1030:
            held.append(os.open(os.devnull, os.O_RDONLY))
        yield
    finally:
        for descriptor in held:
            os.close(descriptor)
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))


@pytest.mark.usefixtures("many_open_files")
def test_git_commands_work_in_a_process_with_more_than_1024_open_files(
    tmp_path: Path, local_sha: str
) -> None:
    """`select.select` raises ValueError for a descriptor >= 1024; a selector does not."""
    repo = tmp_path / "repos" / "analytics"
    env = {"PATH": os.environ["PATH"]}
    done = module._run(["git", "rev-parse", "HEAD"], cwd=repo, env=env, timeout=30)
    assert done.stdout.strip() == local_sha
    target = tmp_path / "tree.tar"
    _archive(local_sha, cwd=repo, env=env, timeout=30, target=target, limit=10**7)
    assert tarfile.is_tarfile(target)


def test_git_userinfo_pattern_is_bounded_and_fast_on_a_huge_word_run() -> None:
    import time

    from lakana_worker.git import _USERINFO, redact

    assert b"\\w+".decode() not in _USERINFO.pattern
    text = "A" * (256 * 1024)
    started = time.monotonic()
    assert redact(text, []).startswith("AAAA")
    assert time.monotonic() - started < 5
    assert redact("see https://u:p@host/x", []) == "see https://***@host/x"
