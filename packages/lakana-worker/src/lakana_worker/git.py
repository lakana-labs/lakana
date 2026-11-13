"""Fetch one commit of a registered repository with the `git` binary, hardened; or check access.

`check_access` lists the remote's branches and tags through the same setup (`_prepared`), for
"Test access"; it fetches nothing.

Lives in the worker, not in core: git, network egress and temp disk stay out
of the API server, which imports core. Every rule here is in
`docs/agent/security.md` ("Git fetch hardening"):

- git runs with a minimal environment — `PATH`, a private `HOME`, `LC_ALL`,
  `GIT_*` and, only when the operator sets a bypass list, `no_proxy` — so no
  `LAKANA_*`, database, storage or KMS setting is
  visible to it or to any helper it starts;
- no system or global config; every setting, the remote URL included, is
  passed through `GIT_CONFIG_COUNT`/`GIT_CONFIG_KEY_n`/`GIT_CONFIG_VALUE_n`,
  never as an argument; every user-supplied value follows `--end-of-options`;
- `protocol.allow=never` except the repository's own protocol; `file` only
  for `kind=local`; hooks, fsmonitor, credential helpers, submodules and
  redirects off;
- operator network settings (`LAKANA_GIT__*`) are passed explicitly for HTTPS repositories
  only — `http.proxy`, `http.sslCAInfo` and the single `no_proxy` variable — never inherited;
  SSH ignores them (no `ProxyCommand`);
- credentials only in memory and in 0600 files under a private directory
  removed in `finally`; stderr redacted before it is shown;
- the commit is fetched into a fresh bare repository and exported with
  `git archive`, so no checkout machinery runs and the uploaded tarball is
  exactly what was validated;
- a full-SHA deploy is refused unless a branch or tag of the remote contains the
  commit; every fetch is bounded in size and the ref count is checked
  first; a ref or ``HEAD`` deploy matches the remote's listing exactly.
"""

from __future__ import annotations

import base64
import contextlib
import ipaddress
import logging
import os
import re
import selectors
import shlex
import shutil
import signal
import socket
import subprocess  # nosec B404 -- git is invoked with fixed argv, no shell
import tarfile
import tempfile
import time
import unicodedata
import urllib.parse
from collections.abc import Callable, Generator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lakana_core.config import validate_proxy_url
from lakana_core.errors import ValidationError
from lakana_core.models import ProbeOutcome, RepositoryKind
from lakana_core.net.egress import is_never_reachable
from lakana_core.projects.git_input import (
    is_full_sha,
    resolve_local_path,
    validate_ref,
    validate_repo_url,
)
from lakana_core.projects.limits import DeployLimits
from lakana_core.projects.probes import ssh_endpoint

AUTH_FAILED_MESSAGE = "repository not found or credential rejected"
DEFAULT_USERNAME = "oauth2"

_AUTH_PATTERNS = re.compile(
    r"authentication failed|returned error: (401|403|404)|repository not found|"
    r"repository '.*' not found|could not read (username|password)|terminal prompts disabled|"
    r"permission denied \(publickey|access denied|does not appear to be a git repository",
    re.IGNORECASE,
)
_HOST_KEY = re.compile(r"host key verification failed|no .* host key is known", re.IGNORECASE)
_MISSING_REF = re.compile(
    r"couldn't find remote ref|not our ref|unadvertised object", re.IGNORECASE
)
_USERINFO = re.compile(r"(://)[^/@\s]+@")
_MAX_STDERR = 2000
_MAX_STDOUT = 16 * 1024 * 1024
"""Most stdout kept from one git command; the largest legitimate output is a
`ls-remote` listing of `fetch_refs` branches and tags."""
_MAX_STDERR_READ = 64 * 1024
"""Most stderr kept in memory; messages are cut to `_MAX_STDERR` when shown."""
_WATCH_SECONDS = 0.5
"""How often a size-bounded git command's repository is measured."""


@dataclass(frozen=True)
class GitCredential:
    """A resolved repository credential, held only in memory.

    Parameters
    ----------
    username
        HTTPS username; `None` sends ``oauth2``.
    token
        HTTPS token, sent as the basic-auth password.
    private_key
        SSH private key in OpenSSH format.
    """

    username: str | None = None
    token: str | None = field(default=None, repr=False)
    private_key: str | None = field(default=None, repr=False)

    def secrets(self) -> list[str]:
        """List every form of this credential that could appear in git's output.

        Returns
        -------
        list of str
            The token, its basic-auth encoding, and the key.
        """
        values: list[str] = []
        if self.token:
            values.append(self.token)
            pair = f"{self.username or DEFAULT_USERNAME}:{self.token}".encode()
            values.append(base64.b64encode(pair).decode())
        if self.private_key:
            values.append(self.private_key.strip())
        return values


class GitError(Exception):
    """A fetch failure whose message is already redacted and safe to store."""


@dataclass(frozen=True)
class FetchedCommit:
    """The fetched commit, exported.

    Parameters
    ----------
    sha
        The resolved commit SHA.
    archive
        `git archive` of the commit (tar, no `.git`).
    tree
        The archive, extracted.
    """

    sha: str
    archive: Path
    tree: Path


def git_environment(
    home: Path,
    config: Sequence[tuple[str, str]],
    extra: Mapping[str, str] | None = None,
    *,
    workdir: Path,
) -> dict[str, str]:
    """Build the complete environment for a git subprocess.

    Parameters
    ----------
    home
        A private, empty directory used as `HOME`.
    config
        ``(key, value)`` settings, passed as command-scope config.
    extra
        Further `GIT_*` variables (e.g. `GIT_ASKPASS`).
    workdir
        The caller's private work directory; its parent becomes
        `GIT_CEILING_DIRECTORIES`, so repository discovery never climbs out
        of the temp root.

    Returns
    -------
    dict
        Nothing from the worker's own environment except `PATH`.
    """
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "LC_ALL": "C",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CEILING_DIRECTORIES": str(workdir.parent),
        "GIT_PROTOCOL_FROM_USER": "0",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_CONFIG_COUNT": str(len(config)),
    }
    for index, (key, value) in enumerate(config):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    if extra:
        env.update(extra)
    return env


def proxy_secrets(proxy: str | None) -> list[str]:
    """List every form of a proxy URL's credential that could appear in git's output.

    Parameters
    ----------
    proxy
        The operator's proxy URL, possibly ``scheme://user:password@host``.

    Returns
    -------
    list of str
        The password and ``user:password``, each raw and percent-decoded, their
        basic-auth encoding, and a bare username; empty without userinfo.
    """
    if not proxy:
        return []
    parts = urllib.parse.urlsplit(proxy)
    values: list[str] = []
    if parts.password is not None:
        for password in (parts.password, urllib.parse.unquote(parts.password)):
            values.append(password)
        raw_user = parts.username or ""
        for pair in (
            f"{raw_user}:{parts.password}",
            f"{urllib.parse.unquote(raw_user)}:{urllib.parse.unquote(parts.password)}",
        ):
            values.append(pair)
            values.append(base64.b64encode(pair.encode()).decode())
    elif parts.username:
        values.append(urllib.parse.unquote(parts.username))
    return list(dict.fromkeys(value for value in values if value))


def redact(text: str, secrets: Sequence[str]) -> str:
    """Remove every credential form and URL userinfo from `text`.

    Parameters
    ----------
    text
        git's stderr.
    secrets
        Values to remove.

    Returns
    -------
    str
        The text with each secret replaced by ``***``, truncated.
    """
    for secret in sorted((s for s in secrets if s), key=len, reverse=True):
        text = text.replace(secret, "***")
    text = _USERINFO.sub(r"\1***@", text)
    return text[:_MAX_STDERR]


def classify_failure(stderr: str, *, ref: str) -> str:
    """Turn git's (already redacted) stderr into the message a user sees.

    Parameters
    ----------
    stderr
        Redacted stderr.
    ref
        The ref being fetched, for a missing-ref message.

    Returns
    -------
    str
        One message for every host-style auth failure (401, 403, 404,
        "not found", publickey); a host-key message; a missing-ref
        message; otherwise git's own text.
    """
    if _HOST_KEY.search(stderr):
        return "SSH host key verification failed: the server's key is not in ssh_known_hosts"
    if _AUTH_PATTERNS.search(stderr):
        return AUTH_FAILED_MESSAGE
    if _MISSING_REF.search(stderr):
        return f"ref {ref!r} not found in the repository"
    return f"git failed: {stderr.strip()}"


def _timeout(limits: DeployLimits, deadline: float) -> float:
    """Return the timeout for the next git command.

    Parameters
    ----------
    limits
        Supplies the per-command limit.
    deadline
        The job's `time.monotonic()` deadline.

    Returns
    -------
    float
        Seconds.

    Raises
    ------
    GitError
        If the job's deadline has passed.
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise GitError("the deploy exceeded its time limit")
    return min(limits.git_command_seconds, remaining)


def _tree_size(path: Path) -> int:
    """Sum the sizes of the regular files under `path`, without following links.

    Parameters
    ----------
    path
        A directory (missing is fine).

    Returns
    -------
    int
        Bytes; files that vanish during the walk are skipped.
    """
    total = 0
    for directory, _dirs, files in os.walk(path):
        for name in files:
            with contextlib.suppress(FileNotFoundError):
                total += os.lstat(os.path.join(directory, name)).st_size
    return total


def _exceeded(limit: int) -> str:
    """Build the fetch-size error message.

    Parameters
    ----------
    limit
        The limit in bytes.

    Returns
    -------
    str
        ``repository fetch exceeded <limit>``.
    """
    return f"repository fetch exceeded {_describe_limit(limit)}"


def _run(
    args: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout: float,
    max_bytes: int | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run one git command, capturing output.

    Output is read as it arrives, never buffered without a bound: a
    hostile server answering `ls-remote` could otherwise grow the worker's
    memory for the whole command timeout. Stdout is cut off at `_MAX_STDOUT`
    (or `max_bytes`, if smaller) and the process group killed; stderr beyond
    `_MAX_STDERR_READ` is read and dropped.

    Parameters
    ----------
    args
        argv; user-supplied values only after ``--end-of-options``.
    cwd
        Directory to run in.
    env
        `git_environment`'s output.
    timeout
        Seconds before the process is killed.
    max_bytes
        With a value, `cwd`'s size and the captured stdout are bounded by it:
       measured every `_WATCH_SECONDS` while git runs and once after
        it exits.

    Returns
    -------
    subprocess.CompletedProcess
        The finished process; the caller checks `returncode`.

    Raises
    ------
    GitError
        If it timed out, exceeded `max_bytes` or `_MAX_STDOUT`, or could not
        be started.
    """
    try:
        process = subprocess.Popen(  # nosec B603 -- fixed argv, no shell, minimal env
            list(args),
            cwd=cwd,
            env=dict(env),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        raise GitError(f"could not run git: {exc.strerror or exc}") from exc
    out_pipe, err_pipe = process.stdout, process.stderr
    assert out_pipe is not None and err_pipe is not None  # nosec B101 -- both are PIPE
    out_fd, err_fd = out_pipe.fileno(), err_pipe.fileno()
    stdout, stderr = bytearray(), bytearray()
    # A selector, not `select.select`: that raises ValueError for a descriptor
    # numbered 1024 or above, which a busy worker process can reach.
    selector = selectors.DefaultSelector()
    selector.register(out_fd, selectors.EVENT_READ)
    selector.register(err_fd, selectors.EVENT_READ)
    end = time.monotonic() + timeout
    next_watch = time.monotonic() + _WATCH_SECONDS
    timed_out = GitError(f"git {args[1]} timed out after {int(timeout)}s")
    try:
        while selector.get_map():
            remaining = end - time.monotonic()
            if remaining <= 0:
                raise timed_out
            wait = remaining if max_bytes is None else min(remaining, _WATCH_SECONDS)
            for key, _ in selector.select(wait):
                descriptor = key.fd
                chunk = os.read(descriptor, 65536)
                if not chunk:
                    selector.unregister(descriptor)
                elif descriptor == err_fd:
                    stderr += chunk[: max(_MAX_STDERR_READ - len(stderr), 0)]
                else:
                    stdout += chunk
                    if max_bytes is not None and len(stdout) > max_bytes:
                        raise GitError(_exceeded(max_bytes))
                    if len(stdout) > _MAX_STDOUT:
                        raise GitError(
                            f"git {args[1]} output exceeded {_describe_limit(_MAX_STDOUT)}"
                        )
            if max_bytes is not None and time.monotonic() >= next_watch:
                next_watch = time.monotonic() + _WATCH_SECONDS
                if _tree_size(cwd) > max_bytes:
                    raise GitError(_exceeded(max_bytes))
        try:
            code = process.wait(timeout=max(end - time.monotonic(), 0.0))
        except subprocess.TimeoutExpired as exc:
            raise timed_out from exc
    finally:
        selector.close()
        _kill_group(process)
        process.wait()
        out_pipe.close()
        err_pipe.close()
    if max_bytes is not None and _tree_size(cwd) > max_bytes:
        raise GitError(_exceeded(max_bytes))
    return subprocess.CompletedProcess(
        list(args),
        code,
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
    )


def _kill_group(process: subprocess.Popen[str] | subprocess.Popen[bytes]) -> None:
    """Kill git and every helper it started (remote-https, ssh, askpass).

    The process leads its own session (`start_new_session`), so its process
    group id is its pid. Does nothing once the leader has been reaped, because
    its pid may then belong to an unrelated process group.

    Parameters
    ----------
    process
        A process started with `start_new_session=True`.
    """
    if process.returncode is not None:
        return
    # macOS refuses (EPERM) to signal a group whose members have all exited but are
    # not yet reaped; the group is gone either way.
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGKILL)


HFS_IGNORABLE = re.compile(r"[\u200c-\u200f\u202a-\u202e\u206a-\u206f\ufeff]")


def fold_name(name: str) -> str:
    """Fold a path the way a case- and normalisation-insensitive filesystem does.

    Mirrors `lakana_core.catalog.external_location._normalize_component` (NFD,
    casefold, NFD) rather than importing that private helper across packages.

    Parameters
    ----------
    name
        A normalised tar member name.

    Returns
    -------
    str
        The folded name; two names a filesystem may treat as one fold equal.
    """
    return unicodedata.normalize("NFD", unicodedata.normalize("NFD", name).casefold())


def _describe_limit(limit: int) -> str:
    """Format a byte limit for an error message.

    Parameters
    ----------
    limit
        Bytes.

    Returns
    -------
    str
        ``N GiB`` or ``N MiB`` for whole units, otherwise ``N bytes``.
    """
    gibibyte = 1024 * 1024 * 1024
    mebibyte = 1024 * 1024
    if limit >= gibibyte and limit % gibibyte == 0:
        return f"{limit // gibibyte} GiB"
    if limit >= mebibyte and limit % mebibyte == 0:
        return f"{limit // mebibyte} MiB"
    return f"{limit} bytes"


_TAR_END_SIZE = 1024
"""Tar ends with two zero-filled 512-byte blocks; `git archive` always writes them."""


def _archive(
    sha: str,
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout: float,
    target: Path,
    limit: int,
    secrets: Sequence[str] = (),
) -> None:
    """Stream `git archive` into `target`, stopping at `limit` bytes.

    Parameters
    ----------
    sha
        The commit.
    cwd
        The bare repository.
    env
        Git environment.
    timeout
        Seconds before the process is killed.
    target
        Output file.
    limit
        Largest archive accepted.
    secrets
        Values removed from git's stderr before it is shown.

    Raises
    ------
    GitError
        If the archive is larger than `limit`, git fails (stderr redacted), it
        times out (even after exiting 0, since output may be left unread), the
        output lacks tar's end-of-archive marker, or reading git's output or
        writing `target` fails.
    """
    try:
        with target.open("wb") as out, tempfile.TemporaryFile("w+") as err:
            process = subprocess.Popen(  # nosec B603 B607 -- fixed argv, no shell, git from PATH
                ["git", "archive", "--format=tar", "--end-of-options", sha],
                cwd=cwd,
                env=dict(env),
                stdout=subprocess.PIPE,
                stderr=err,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
            stdout = process.stdout
            assert stdout is not None  # nosec B101 -- stdout=PIPE
            descriptor = stdout.fileno()
            # Not `select.select`: it raises ValueError for a descriptor >= 1024.
            selector = selectors.DefaultSelector()
            selector.register(descriptor, selectors.EVENT_READ)
            end = time.monotonic() + timeout
            timed_out = False
            code: int | None = None
            written = 0
            tail = b""
            try:
                while True:
                    remaining = end - time.monotonic()
                    if remaining <= 0 or not selector.select(remaining):
                        # Bytes may still sit unread in the pipe even if git already
                        # exited 0, so expiry is a failure whatever the exit code.
                        timed_out = True
                        break
                    chunk = os.read(descriptor, 65536)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > limit:
                        raise GitError(f"the checkout is larger than {_describe_limit(limit)}")
                    out.write(chunk)
                    tail = (tail + chunk)[-_TAR_END_SIZE:]
                if not timed_out:
                    try:
                        code = process.wait(timeout=max(end - time.monotonic(), 0))
                    except subprocess.TimeoutExpired:
                        timed_out = True
            finally:
                selector.close()
                _kill_group(process)
                process.wait()
                stdout.close()
            if timed_out:
                raise GitError(f"git archive timed out after {int(timeout)}s")
            if code != 0:
                err.seek(0)
                raise GitError(f"git archive failed: {redact(err.read(), secrets).strip()}")
            if tail != bytes(_TAR_END_SIZE):
                raise GitError("git archive output is incomplete: no end-of-archive marker")
    except OSError as exc:
        raise GitError(f"git archive failed: {exc.strerror or type(exc).__name__}") from exc


def _extract(archive: Path, tree: Path, *, limit: int, max_members: int) -> None:
    """Extract the archive; only files, directories and symlinks are accepted.

    Regular files and directories go through `tarfile.data_filter`. Symlink
    members are kept verbatim — `data_filter` would reject an absolute
    target, but the link itself is harmless until followed, and discovery
    refuses every link by name without reading it. Containment is a
    *lexical* rule on names compared folded (case, Unicode normalisation; names
    with code points HFS+ ignores are refused), not a filesystem resolution
    (which `PATH_MAX` tricks can defeat) and not an exact-string match (APFS
    treats `A` and `a` as one name): the tree starts empty, so the symlinks tar creates are the only
    links in it, and any member whose name has an earlier symlink member as
    an ancestor is refused. Duplicate names are refused so one member cannot
    replace another. Device nodes, FIFOs and hard links are refused outright
    (`git archive` never emits them), because discovery opens files before
    it checks their type. The summed size of regular members and the member
    count are bounded.

    Parameters
    ----------
    archive
        The tar file.
    tree
        Destination directory, created here.
    limit
        Largest summed size of the extracted files.
    max_members
        Most members accepted.

    Raises
    ------
    GitError
        If a member's name is absolute, contains ``..`` or lies below a
        symlink member, repeats, is of an unsupported kind, or the data filter,
        `limit` or `max_members` refuses it.
    """
    total = 0
    seen: dict[str, tuple[str, bool]] = {}
    links: set[str] = set()
    tree.mkdir()

    def accept(member: tarfile.TarInfo, dest: str) -> tarfile.TarInfo:
        """Filter one member.

        Parameters
        ----------
        member
            The tar member.
        dest
            The destination directory.

        Returns
        -------
        tarfile.TarInfo
            The member to extract.

        Raises
        ------
        GitError
            For an unsafe name, an unsupported kind, a repeat, or too much data.
        """
        nonlocal total
        if member.name.startswith("/") or ".." in Path(member.name).parts:
            raise GitError(f"unsafe path in checkout: {member.name!r}")
        if HFS_IGNORABLE.search(member.name):
            raise GitError(f"unsafe path in checkout: {member.name!r}")
        name = fold_name(os.path.normpath(member.name))
        ancestors = {str(parent) for parent in Path(name).parents}
        if ancestors & links:
            raise GitError(f"unsafe path in checkout: {member.name!r}")
        if not (member.isreg() or member.isdir() or member.issym()):
            raise GitError(f"unsupported entry in checkout: {member.name!r}")
        if name in seen:
            # tarfile re-filters directories to set their attributes at the end; a
            # directory repeating a directory is harmless, anything else could replace.
            first, was_dir = seen[name]
            if not (member.isdir() and was_dir):
                if os.path.normpath(member.name) == first:
                    raise GitError(f"duplicate entry in checkout: {member.name!r}")
                raise GitError(
                    f"duplicate entry in checkout: {member.name!r} collides with {first!r} "
                    "when names are compared case- and normalisation-insensitively"
                )
            return tarfile.data_filter(member, dest)
        seen[name] = (os.path.normpath(member.name), member.isdir())
        if member.issym():
            links.add(name)
        if len(seen) > max_members:
            raise GitError(f"the checkout has more than {max_members} files")
        if member.isreg():
            total += member.size
            if total > limit:
                raise GitError(f"the checkout is larger than {_describe_limit(limit)}")
        if member.issym():
            return member.replace(uid=0, gid=0, uname="", gname="", deep=False)
        return tarfile.data_filter(member, dest)

    try:
        with tarfile.open(archive) as handle:
            handle.extractall(tree, filter=accept)  # nosec B202 -- filtered above
    except (tarfile.TarError, OSError, ValueError) as exc:
        raise GitError(f"could not extract the checkout: {exc}") from exc


def _protocol_config(kind: RepositoryKind) -> list[tuple[str, str]]:
    """Allow exactly the repository's own transport.

    Parameters
    ----------
    kind
        The repository kind.

    Returns
    -------
    list of tuple
        `protocol.*` settings.
    """
    allowed = {
        RepositoryKind.LOCAL: "file",
        RepositoryKind.HTTPS: "https",
        RepositoryKind.SSH: "ssh",
    }
    return [("protocol.allow", "never"), (f"protocol.{allowed[kind]}.allow", "always")]


def _write_private(path: Path, text: str, mode: int) -> None:
    """Create `path` with `mode` before writing, so it is never readable by others.

    Parameters
    ----------
    path
        The file.
    text
        Content.
    mode
        Permission bits.

    Raises
    ------
    GitError
        If the file cannot be created or written.
    """
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
    except OSError as exc:
        raise GitError(
            f"could not write {path.name}: {exc.strerror or type(exc).__name__}"
        ) from exc


def _trust_source(path: Path, source: str) -> Path:
    """Write a global git config that marks the local source repository as safe.

    git refuses to read a repository owned by another user (the Compose `./repos` mount is
    owned by the host user, the worker runs as 10001). The check runs in `git-upload-pack`
    inside the source, which never sees command-scope config (git scrubs `GIT_CONFIG_COUNT`
    for it) but does read `GIT_CONFIG_GLOBAL`. The entry names exactly the resolved source
    path, never a wildcard, and is written only for ``kind=local``. Both the path and its
    `.git` are listed: git checks the git directory, and which of the two it compares depends
    on its version (2.47 needs `.git`).

    Parameters
    ----------
    path
        Where to write the file (inside the fetch's private directory).
    source
        The resolved source path, already confined to `LAKANA_REPO_ROOTS`.

    Returns
    -------
    Path
        `path`.
    """
    lines = ["[safe]"]
    for trusted in (source, f"{source}/.git"):
        quoted = trusted.replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'\tdirectory = "{quoted}"')
    _write_private(path, "\n".join(lines) + "\n", 0o600)
    return path


def _askpass(credentials_dir: Path, credential: GitCredential, url: str) -> Path:
    """Write the username, token and askpass helper for an HTTPS fetch.

    The username may contain any character (``@ / % #``); it reaches git
    only through this helper, never inside the URL. The helper answers only
    git's two exact prompts for the repository's own origin (``Username for
    'https://<host>': `` and ``Password for 'https://<encoded username>@<host>': ``, no
    wildcards) and exits 1 for any other
    (notably the operator proxy's), so the token is never offered to a proxy.

    Parameters
    ----------
    credentials_dir
        A private directory created by the caller.
    credential
        A credential with a token.
    url
        The repository URL; its host (and port) names the origin answered for.

    Returns
    -------
    Path
        The askpass script.
    """
    assert credential.token is not None  # nosec B101 -- the caller checked
    _write_private(credentials_dir / "username", credential.username or DEFAULT_USERNAME, 0o600)
    _write_private(credentials_dir / "token", credential.token, 0o600)
    quoted = shlex.quote(str(credentials_dir))
    origin = urllib.parse.urlsplit(url).netloc
    variants = list(dict.fromkeys((origin, origin.lower())))
    # git prints the username percent-encoded in its password prompt.
    username = urllib.parse.quote(credential.username or DEFAULT_USERNAME, safe="")
    username_patterns = [_sh_literal(f"Username for 'https://{v}': ") for v in variants]
    password_patterns = [_sh_literal(f"Password for 'https://{username}@{v}': ") for v in variants]
    script = (
        "#!/bin/sh\n"
        f'case "$1" in\n  {" | ".join(username_patterns)}) exec cat {quoted}/username ;;\n'
        f"  {' | '.join(password_patterns)}) exec cat {quoted}/token ;;\n"
        "  *) exit 1 ;;\nesac\n"
    )
    path = credentials_dir / "askpass"
    _write_private(path, script, 0o700)
    return path


def _sh_literal(text: str) -> str:
    r"""Quote `text` as a double-quoted shell string with nothing expanded.

    Parameters
    ----------
    text
        Literal text.

    Returns
    -------
    str
        `text` in double quotes, ``\\``, ``"``, ``$`` and the backtick escaped.
    """
    return '"' + re.sub(r'([\\"$`])', r"\\\1", text) + '"'


def _ssh_command(
    credentials_dir: Path, credential: GitCredential | None, known_hosts: str | None
) -> str:
    """Write the SSH key material and build the `core.sshCommand` value.

    Parameters
    ----------
    credentials_dir
        A private directory created by the caller.
    credential
        The credential; its `private_key`, if any, becomes the only identity.
    known_hosts
        ``known_hosts`` lines; `None` writes an empty file so verification
        fails closed.

    Returns
    -------
    str
        A shell-quoted ssh command line.
    """
    known_hosts_file = credentials_dir / "known_hosts"
    _write_private(known_hosts_file, known_hosts or "", 0o600)
    command = [
        "ssh",
        "-F",
        os.devnull,
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={known_hosts_file}",
        "-o",
        f"GlobalKnownHostsFile={os.devnull}",
        "-o",
        "IdentityAgent=none",
        "-o",
        "ConnectTimeout=30",
        "-o",
        "IdentitiesOnly=yes",
    ]
    if credential is not None and credential.private_key:
        key_file = credentials_dir / "id"
        _write_private(key_file, credential.private_key.strip() + "\n", 0o600)
        command += ["-i", str(key_file)]
    else:
        # ssh resolves ~ from the passwd entry, not $HOME: without this it would
        # offer the worker user's own ~/.ssh keys.
        command += ["-o", "IdentityFile=none"]
    return shlex.join(command)


def fetch_commit(
    *,
    kind: RepositoryKind,
    url: str,
    ref: str | None,
    credential: GitCredential | None,
    known_hosts: str | None,
    workdir: Path,
    limits: DeployLimits,
    repo_roots: Sequence[str],
    deadline: float,
    ca_bundle: Path | None = None,
    https_proxy: str | None = None,
    no_proxy: str | None = None,
    repository_name: str = "the repository",
) -> FetchedCommit:
    """Fetch one commit into a fresh repository and export it.

    Parameters
    ----------
    kind
        Repository kind.
    url
        Registered URL (re-validated here).
    ref
        Ref or full SHA; `None` fetches the remote's ``HEAD``.
    credential
        Resolved credential, if the repository has one.
    known_hosts
        ``known_hosts`` lines for ``ssh``.
    workdir
        A private (0700) directory the caller removes afterwards; each call
        creates its own subdirectory in it, so it may be reused.
    limits
        Time and size limits.
    repo_roots
        `LAKANA_REPO_ROOTS`, re-checked for ``local``.
    deadline
        The job's `time.monotonic()` deadline.
    ca_bundle
        Extra CA file for HTTPS (`LAKANA_GIT__CA_BUNDLE`), passed as
        ``http.sslCAInfo``; `None` uses the system store.
    https_proxy
        Proxy URL for HTTPS repositories (`LAKANA_GIT__HTTPS_PROXY`), passed as
        ``http.proxy``; `None` means no proxy, whatever the worker's own
        environment says. Its credentials are redacted from errors.
    no_proxy
        Comma-separated hosts that bypass the proxy (`LAKANA_GIT__NO_PROXY`), passed
        as the one `no_proxy` variable; `None` sets none.
    repository_name
        Named in the unreachable-commit error.

    Returns
    -------
    FetchedCommit
        SHA, archive and extracted tree inside `workdir`.

    Raises
    ------
    GitError
        For every failure, with a redacted message.
    """
    try:
        if https_proxy is not None:
            validate_proxy_url(https_proxy)
        wanted = validate_ref(ref) if ref is not None else "HEAD"
    except (ValidationError, ValueError) as exc:
        raise GitError(str(exc)) from exc
    with _prepared(
        kind=kind,
        url=url,
        credential=credential,
        known_hosts=known_hosts,
        workdir=workdir,
        repo_roots=repo_roots,
        ca_bundle=ca_bundle,
        https_proxy=https_proxy,
        no_proxy=no_proxy,
        prefix="fetch-",
    ) as (run, env, secrets):
        repo = run / "repo.git"
        _init(repo, run=run, env=env, limits=limits, deadline=deadline, secrets=secrets)
        if is_full_sha(wanted):
            wanted = _verify_reachable(
                wanted,
                run=run,
                env=env,
                limits=limits,
                deadline=deadline,
                secrets=secrets,
                repository_name=repository_name,
            )
        sha = _fetch(wanted, repo=repo, env=env, limits=limits, deadline=deadline, secrets=secrets)
        _neutralise_export_subst(repo)
        archive = run / "code.tar"
        _archive(
            sha,
            cwd=repo,
            env=env,
            timeout=_timeout(limits, deadline),
            target=archive,
            limit=limits.checkout_bytes,
            secrets=secrets,
        )
        tree = run / "tree"
        _extract(archive, tree, limit=limits.checkout_bytes, max_members=limits.checkout_members)
        return FetchedCommit(sha=sha, archive=archive, tree=tree)


def _neutralise_export_subst(repo: Path) -> None:
    """Make `git archive` leave every `$Format:…$` placeholder unexpanded.

    `$GIT_DIR/info/attributes` takes precedence over the commit's own `.gitattributes`, so
    `* -export-subst` turns the expansion off: one commit always gives the same text, for what
    runs and for the source map. `export-ignore` stays in force, and so do the attributes that
    convert a file's text on checkout (`ident`, `eol`, `working-tree-encoding`), which depend
    only on the commit.

    Parameters
    ----------
    repo
        The temporary repository's git directory.
    """
    info = repo / "info"
    info.mkdir(exist_ok=True)
    (info / "attributes").write_text("* -export-subst\n", encoding="utf-8")


def _init(
    repo: Path,
    *,
    run: Path,
    env: Mapping[str, str],
    limits: DeployLimits,
    deadline: float,
    secrets: Sequence[str],
) -> None:
    """Create the fresh bare repository git fetches into or lists from.

    Parameters
    ----------
    repo
        Where to create it.
    run
        The directory `_prepared` made.
    env
        Git environment.
    limits
        Limits.
    deadline
        Job deadline.
    secrets
        Values to redact.

    Raises
    ------
    GitError
        If `git init` fails, or the deadline has passed.
    """
    init = _run(
        ["git", "init", "--quiet", "--bare", str(repo)],
        cwd=run,
        env=env,
        timeout=_timeout(limits, deadline),
    )
    if init.returncode != 0:
        raise GitError(f"git init failed: {redact(init.stderr, secrets)}")


@contextlib.contextmanager
def _prepared(
    *,
    kind: RepositoryKind,
    url: str,
    credential: GitCredential | None,
    known_hosts: str | None,
    workdir: Path,
    repo_roots: Sequence[str],
    ca_bundle: Path | None,
    https_proxy: str | None,
    no_proxy: str | None,
    prefix: str,
) -> Generator[tuple[Path, dict[str, str], list[str]]]:
    """Validate a repository's settings and build the hardened git environment for it.

    Shared by `fetch_commit` and `check_access`, so a check of a repository runs git exactly as
    a deploy does: one protocol, the URL as command-scope config, no hooks, helpers or
    redirects, the operator's HTTPS settings only for ``https``, and the credential in private
    files that are removed when the block ends.

    Parameters
    ----------
    kind
        Repository kind.
    url
        Registered URL (re-validated here).
    credential
        Resolved credential, if the repository has one.
    known_hosts
        ``known_hosts`` lines for ``ssh``.
    workdir
        The caller's private work directory; a new subdirectory is made in it.
    repo_roots
        `LAKANA_REPO_ROOTS`, re-checked for ``local``.
    ca_bundle
        Extra CA file for HTTPS.
    https_proxy
        Proxy URL for HTTPS repositories, already validated.
    no_proxy
        Hosts that bypass the proxy.
    prefix
        Starts the name of the new subdirectory.

    Yields
    ------
    tuple
        The new directory, git's environment, and every credential form to redact.

    Raises
    ------
    GitError
        If the URL or the local path is refused, with the reason.
    """
    secrets = credential.secrets() if credential is not None else []
    if kind is RepositoryKind.HTTPS:
        secrets = [*secrets, *proxy_secrets(https_proxy)]
    try:
        validate_repo_url(kind, url)
        remote = resolve_local_path(url, repo_roots) if kind is RepositoryKind.LOCAL else url
    except (ValidationError, ValueError) as exc:
        raise GitError(str(exc)) from exc
    run = Path(tempfile.mkdtemp(prefix=prefix, dir=workdir))
    credentials_dir = run / "credentials"
    home = run / "home"
    home.mkdir(mode=0o700)
    config = [
        *_protocol_config(kind),
        ("core.hooksPath", os.devnull),
        ("core.fsmonitor", "false"),
        ("credential.helper", ""),
        ("fetch.recurseSubmodules", "false"),
        ("submodule.recurse", "false"),
        ("http.followRedirects", "false"),
        ("transfer.fsckObjects", "true"),
        ("remote.origin.url", remote),
    ]
    extra: dict[str, str] = {}
    try:
        if kind is RepositoryKind.LOCAL:
            extra["GIT_CONFIG_GLOBAL"] = str(_trust_source(run / "gitconfig", remote))
        if kind is RepositoryKind.HTTPS:
            if ca_bundle is not None:
                config.append(("http.sslCAInfo", str(ca_bundle)))
            if https_proxy is not None:
                config.append(("http.proxy", https_proxy))
                if ca_bundle is not None and https_proxy.startswith("https://"):
                    config.append(("http.proxySSLCAInfo", str(ca_bundle)))
            if no_proxy is not None:
                # git has no `http.noProxy`; libcurl reads the bypass list from this variable.
                extra["no_proxy"] = no_proxy
            if credential is not None and credential.token:
                credentials_dir.mkdir(mode=0o700)
                extra["GIT_ASKPASS"] = str(_askpass(credentials_dir, credential, remote))
        if kind is RepositoryKind.SSH:
            credentials_dir.mkdir(mode=0o700)
            config.append(
                ("core.sshCommand", _ssh_command(credentials_dir, credential, known_hosts))
            )
        yield run, git_environment(home, config, extra, workdir=workdir), secrets
    finally:
        shutil.rmtree(credentials_dir, ignore_errors=True)


FAILED_MESSAGE = "The check failed; the worker's log has the details."
"""The message of an access check whose failure Lakana does not recognise (M3: never git's text)."""

HOST_KEY_CHANGED_MESSAGE = (
    "The host's key is not the one you confirmed. Check the host's published fingerprints "
    "before you connect again."
)
"""The message of an access check whose host offered a key other than the confirmed one."""

REFUSED_ADDRESS_MESSAGE = (
    "The host's name resolves to an address no Lakana worker may reach (loopback, "
    "link-local or a cloud metadata address). Use the git host's public name."
)
"""The message of an access check or a host-key scan refused for its address."""

_ACCESS_MESSAGES = {
    ProbeOutcome.OK: "The worker reached the repository and could read its branches and tags.",
    ProbeOutcome.HOST_KEY_CHANGED: HOST_KEY_CHANGED_MESSAGE,
    ProbeOutcome.HOST_KEY_UNKNOWN: (
        "The host offered no key this repository trusts. Fetch the host's keys again "
        "(`lakana repo host-keys URL`), compare them with your host's published fingerprints, "
        "and update the repository's known hosts."
    ),
    ProbeOutcome.AUTH_REFUSED: (
        "Add the public key as a read-only deploy key at your git host, then test again."
    ),
    ProbeOutcome.NOT_FOUND: (
        "The host says there is no such repository, or it hides one the credential may not "
        "read. Check the URL, and that the deploy key or token is given access to this "
        "repository at your git host."
    ),
    ProbeOutcome.UNREACHABLE: (
        "The worker could not reach the host. Check the URL's host and port, and that the "
        "worker's network may connect to it."
    ),
    ProbeOutcome.REFUSED_ADDRESS: REFUSED_ADDRESS_MESSAGE,
    ProbeOutcome.TIMEOUT: (
        "The host did not answer in time. Check the URL's host and port and the network path "
        "from the worker to it, then test again."
    ),
    ProbeOutcome.FAILED: FAILED_MESSAGE,
}
"""The fixed sentence of each outcome of an access check, with its next step."""

_HOST_KEY_CHANGED = re.compile(r"REMOTE HOST IDENTIFICATION HAS CHANGED")
_NOT_FOUND = re.compile(
    r"repository not found|repository '.*' not found|returned error: 404|"
    r"does not appear to be a git repository",
    re.IGNORECASE,
)
_UNREACHABLE = re.compile(
    r"could not resolve host|could not resolve hostname|name or service not known|"
    r"connection refused|no route to host|network is unreachable|failed to connect",
    re.IGNORECASE,
)
_TIMED_OUT = re.compile(r"timed out|timeout", re.IGNORECASE)

logger = logging.getLogger(__name__)


def access_outcome(stderr: str) -> ProbeOutcome:
    """Map git's (redacted) stderr of a failed `ls-remote` to an access check's outcome.

    The rules are `classify_failure`'s, split finer: a changed host key only on ssh's "REMOTE
    HOST IDENTIFICATION HAS CHANGED"; any other host-key failure is an unknown key; a host's
    "not found" apart from a refused credential.

    Parameters
    ----------
    stderr
        Redacted stderr.

    Returns
    -------
    ProbeOutcome
        The outcome; ``failed`` for anything not recognised.
    """
    if _HOST_KEY_CHANGED.search(stderr):
        return ProbeOutcome.HOST_KEY_CHANGED
    if _HOST_KEY.search(stderr):
        return ProbeOutcome.HOST_KEY_UNKNOWN
    if _NOT_FOUND.search(stderr):
        return ProbeOutcome.NOT_FOUND
    if _AUTH_PATTERNS.search(stderr):
        return ProbeOutcome.AUTH_REFUSED
    if _TIMED_OUT.search(stderr):
        return ProbeOutcome.TIMEOUT
    if _UNREACHABLE.search(stderr):
        return ProbeOutcome.UNREACHABLE
    return ProbeOutcome.FAILED


def _network_host(kind: RepositoryKind, url: str) -> tuple[str, int] | None:
    """Read the host and port a remote repository URL connects to.

    Parameters
    ----------
    kind
        The repository's kind.
    url
        Its validated URL.

    Returns
    -------
    tuple or None
        The host and port; `None` for ``local``.

    Raises
    ------
    ValidationError
        For an SSH URL `ssh_endpoint` refuses.
    """
    if kind is RepositoryKind.SSH:
        return ssh_endpoint(url)
    if kind is RepositoryKind.HTTPS:
        parts = urllib.parse.urlsplit(url)
        return parts.hostname or "", parts.port or 443
    return None


def refuses_address(
    host: str, port: int, *, resolve: Callable[..., Sequence[Any]] = socket.getaddrinfo
) -> bool:
    """Say whether a host resolves to any address no Lakana worker may reach.

    Parameters
    ----------
    host
        The host.
    port
        Its port.
    resolve
        `socket.getaddrinfo` or a stand-in.

    Returns
    -------
    bool
        `True` when any address `is_never_reachable`; `False` when none is, or the name does not
        resolve here (behind a proxy the worker may not resolve names itself, and git then says
        what happened).
    """
    try:
        answers = resolve(host, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        return False
    return any(
        is_never_reachable(ipaddress.ip_address(str(answer[4][0]).split("%", 1)[0]))
        for answer in answers
    )


def check_access(
    *,
    kind: RepositoryKind,
    url: str,
    credential: GitCredential | None,
    known_hosts: str | None,
    workdir: Path,
    limits: DeployLimits,
    repo_roots: Sequence[str],
    deadline: float,
    ca_bundle: Path | None = None,
    https_proxy: str | None = None,
    no_proxy: str | None = None,
    resolve: Callable[..., Sequence[Any]] = socket.getaddrinfo,
) -> tuple[ProbeOutcome, str]:
    """Check that a worker can read a repository: `git ls-remote --heads --tags`.

    Runs through the same environment, protocol allow-list, `core.sshCommand` (strict host keys
    against `known_hosts` only), askpass, limits and redaction as `fetch_commit`, after the URL
    is validated again and its host is resolved once: any address that `is_never_reachable`
    refuses the check before git runs (git then resolves the name again, as a deploy does).
    Nothing is fetched.

    Parameters
    ----------
    kind
        Repository kind.
    url
        Registered URL.
    credential
        Resolved credential, if the repository has one.
    known_hosts
        The repository's ``known_hosts`` lines for ``ssh``.
    workdir
        A private (0700) directory the caller removes afterwards.
    limits
        Time and size limits.
    repo_roots
        `LAKANA_REPO_ROOTS`, re-checked for ``local``.
    deadline
        The check's `time.monotonic()` deadline.
    ca_bundle
        Extra CA file for HTTPS (`LAKANA_GIT__CA_BUNDLE`).
    https_proxy
        Proxy URL for HTTPS repositories (`LAKANA_GIT__HTTPS_PROXY`).
    no_proxy
        Hosts that bypass the proxy (`LAKANA_GIT__NO_PROXY`).
    resolve
        `socket.getaddrinfo` or a stand-in.

    Returns
    -------
    tuple of (ProbeOutcome, str)
        The outcome and a fixed sentence with the next step; never git's own text, which goes
        to the worker's log, redacted, for a failure Lakana does not recognise.
    """
    try:
        if https_proxy is not None:
            validate_proxy_url(https_proxy)
        validate_repo_url(kind, url)
        endpoint = _network_host(kind, url)
    except (ValidationError, ValueError) as exc:
        return ProbeOutcome.FAILED, f"The repository URL is refused: {exc}"
    if endpoint is not None and refuses_address(*endpoint, resolve=resolve):
        return ProbeOutcome.REFUSED_ADDRESS, _ACCESS_MESSAGES[ProbeOutcome.REFUSED_ADDRESS]
    try:
        with _prepared(
            kind=kind,
            url=url,
            credential=credential,
            known_hosts=known_hosts,
            workdir=workdir,
            repo_roots=repo_roots,
            ca_bundle=ca_bundle,
            https_proxy=https_proxy,
            no_proxy=no_proxy,
            prefix="access-",
        ) as (run, env, secrets):
            repo = run / "repo.git"
            _init(repo, run=run, env=env, limits=limits, deadline=deadline, secrets=secrets)
            listed = _run(
                ["git", "ls-remote", "--heads", "--tags", "--end-of-options", "origin"],
                cwd=repo,
                env=env,
                timeout=_timeout(limits, deadline),
                max_bytes=limits.fetch_bytes,
            )
            stderr = redact(listed.stderr, secrets)
    except GitError as exc:
        text = str(exc)
        if "timed out" in text or "time limit" in text:
            return ProbeOutcome.TIMEOUT, (
                "The check ran out of time before the host answered. Check the URL's host and "
                "port and the network path from the worker to it, then test again."
            )
        logger.warning("A repository access check could not run git: %s", text)
        return ProbeOutcome.FAILED, FAILED_MESSAGE
    if listed.returncode == 0:
        return ProbeOutcome.OK, _ACCESS_MESSAGES[ProbeOutcome.OK]
    outcome = access_outcome(stderr)
    if outcome is ProbeOutcome.FAILED:
        logger.warning("A repository access check failed: %s", stderr.strip())
    return outcome, _ACCESS_MESSAGES[outcome]


def _fetch(
    wanted: str,
    *,
    repo: Path,
    env: Mapping[str, str],
    limits: DeployLimits,
    deadline: float,
    secrets: Sequence[str],
) -> str:
    """Fetch `wanted` and return the commit SHA it resolves to.

    Parameters
    ----------
    wanted
        A validated ref or full SHA.
    repo
        The bare repository.
    env
        Git environment.
    limits
        Limits.
    deadline
        Job deadline.
    secrets
        Values to redact.

    Returns
    -------
    str
        The resolved SHA.

    Raises
    ------
    GitError
        If the ref or commit cannot be fetched.
    """
    base = ["git", "fetch", "--quiet", "--no-tags", "--no-recurse-submodules"]
    target = wanted
    listed: str | None = None
    if not is_full_sha(wanted):
        target, listed = _resolve_ref(
            wanted, repo=repo, env=env, limits=limits, deadline=deadline, secrets=secrets
        )
    shallow = _run(
        [*base, "--depth", "1", "--end-of-options", "origin", target],
        cwd=repo,
        env=env,
        timeout=_timeout(limits, deadline),
        max_bytes=limits.fetch_bytes,
    )
    if shallow.returncode == 0:
        if listed is not None and _object_of("FETCH_HEAD", repo, env, limits, deadline) != listed:
            raise GitError(f"{wanted!r} changed while it was being fetched; try again")
        sha = _commit_of("FETCH_HEAD", repo=repo, env=env, limits=limits, deadline=deadline)
        if sha is None:
            raise GitError(f"{wanted!r} does not name a commit")
        return sha
    message = classify_failure(redact(shallow.stderr, secrets), ref=wanted)
    if not is_full_sha(wanted) or message == AUTH_FAILED_MESSAGE or "host key" in message:
        raise GitError(message)
    everything = _run(
        [
            *base,
            "--end-of-options",
            "origin",
            "+refs/heads/*:refs/remotes/origin/*",
            "+refs/tags/*:refs/tags/*",
        ],
        cwd=repo,
        env=env,
        timeout=_timeout(limits, deadline),
        max_bytes=limits.fetch_bytes,
    )
    if everything.returncode != 0:
        raise GitError(classify_failure(redact(everything.stderr, secrets), ref=wanted))
    sha = _commit_of(wanted, repo=repo, env=env, limits=limits, deadline=deadline)
    if sha is None:
        raise GitError(f"commit {wanted} not found in the repository")
    return sha


def _resolve_ref(
    name: str,
    *,
    repo: Path,
    env: Mapping[str, str],
    limits: DeployLimits,
    deadline: float,
    secrets: Sequence[str],
) -> tuple[str, str]:
    """Resolve a ref to ``HEAD``, a branch or a tag of the remote, never another namespace.

    Git itself would also try ``refs/<name>``, ``refs/remotes/<name>`` and so
    on (``refs/pull/…``, ``refs/changes/…``, ``refs/refs/heads/<name>``), for
    qualified names too. So the remote's branches and tags are listed and the
    name matched exactly (a tag wins over a branch, as in git's own rules); the
    caller then checks that what it fetched is the listed object. ``HEAD`` is
    listed the same way and must appear as an exact ``HEAD`` line:
    ``ls-remote`` patterns match by suffix, so ``refs/remotes/HEAD`` is
    listed too, and an unborn HEAD would otherwise let git's DWIM fetch it.

    Parameters
    ----------
    name
        A validated short name, ``HEAD``, or ``refs/heads/…`` / ``refs/tags/…`` name.
    repo
        The bare repository.
    env
        Git environment.
    limits
        Limits.
    deadline
        Job deadline.
    secrets
        Values to redact.

    Returns
    -------
    tuple of (str, str)
        The full ref name and the object id the remote lists for it.

    Raises
    ------
    GitError
        If the listing fails, the remote has neither a tag nor a branch of that
        name, or (for ``HEAD``) its HEAD does not point to a commit.
    """
    if name == "HEAD":
        candidates = ["HEAD"]
        selection: list[str] = []
    else:
        candidates = (
            [name] if name.startswith("refs/") else [f"refs/tags/{name}", f"refs/heads/{name}"]
        )
        selection = ["--refs", "--heads", "--tags"]
    listed = _run(
        ["git", "ls-remote", *selection, "--end-of-options", "origin", *candidates],
        cwd=repo,
        env=env,
        timeout=_timeout(limits, deadline),
        max_bytes=limits.fetch_bytes,
    )
    if listed.returncode != 0:
        raise GitError(classify_failure(redact(listed.stderr, secrets), ref=name))
    found: dict[str, str] = {}
    for line in listed.stdout.splitlines():
        oid, _, refname = line.partition("\t")
        found[refname.strip()] = oid.strip()
    for candidate in candidates:
        if candidate in found:
            return candidate, found[candidate]
    if name == "HEAD":
        raise GitError(
            "the repository's HEAD does not point to a commit; deploy a branch, tag or commit"
        )
    raise GitError(classify_failure("fatal: couldn't find remote ref", ref=name))


def _object_of(
    name: str, repo: Path, env: Mapping[str, str], limits: DeployLimits, deadline: float
) -> str:
    """Return the object id `name` names, without peeling it.

    Parameters
    ----------
    name
        ``FETCH_HEAD``.
    repo
        The bare repository.
    env
        Git environment.
    limits
        Limits.
    deadline
        Job deadline.

    Returns
    -------
    str
        The object id, or an empty string if it does not resolve.
    """
    resolved = _run(
        ["git", "rev-parse", "--verify", "--quiet", "--end-of-options", name],
        cwd=repo,
        env=env,
        timeout=_timeout(limits, deadline),
    )
    return resolved.stdout.strip()


def _verify_reachable(
    sha: str,
    *,
    run: Path,
    env: Mapping[str, str],
    limits: DeployLimits,
    deadline: float,
    secrets: Sequence[str],
    repository_name: str,
) -> str:
    """Require a full SHA to be reachable from a branch or tag of the remote.

    Fetches every branch and tag without trees or blobs (`--filter=tree:0`) into a throwaway probe
    repository (a server without filter support sends everything, bounded
    by `fetch_bytes`), after counting the refs with `ls-remote`, and writes a
    commit-graph so the reachability check stays fast at many refs. Every probe
    command runs with `env` (`GIT_NO_LAZY_FETCH=1`), so looking up an unknown
    SHA in the partial clone cannot download it on demand.

    Parameters
    ----------
    sha
        A validated full SHA (a commit or an annotated tag object).
    run
        This fetch's private directory.
    env
        Git environment.
    limits
        Limits.
    deadline
        Job deadline.
    secrets
        Values to redact.
    repository_name
        For the error message.

    Returns
    -------
    str
        The commit `sha` peels to.

    Raises
    ------
    GitError
        If the remote has too many refs, the probe fetch fails, or no branch
        or tag contains the commit.
    """
    probe = run / "probe.git"
    init = _run(
        ["git", "init", "--quiet", "--bare", str(probe)],
        cwd=run,
        env=env,
        timeout=_timeout(limits, deadline),
    )
    if init.returncode != 0:
        raise GitError(f"git init failed: {redact(init.stderr, secrets)}")
    listed = _run(
        ["git", "ls-remote", "--refs", "--heads", "--tags", "--end-of-options", "origin"],
        cwd=probe,
        env=env,
        timeout=_timeout(limits, deadline),
        max_bytes=limits.fetch_bytes,
    )
    if listed.returncode != 0:
        raise GitError(classify_failure(redact(listed.stderr, secrets), ref=sha))
    if sum(1 for line in listed.stdout.splitlines() if line.strip()) > limits.fetch_refs:
        raise GitError(f"the repository has more than {limits.fetch_refs} branches and tags")
    fetched = _run(
        [
            "git",
            "fetch",
            "--quiet",
            "--no-tags",
            "--no-recurse-submodules",
            "--filter=tree:0",
            "--end-of-options",
            "origin",
            "+refs/heads/*:refs/remotes/origin/*",
            "+refs/tags/*:refs/tags/*",
        ],
        cwd=probe,
        env=env,
        timeout=_timeout(limits, deadline),
        max_bytes=limits.fetch_bytes,
    )
    if fetched.returncode != 0:
        raise GitError(classify_failure(redact(fetched.stderr, secrets), ref=sha))
    commit = _commit_of(sha, repo=probe, env=env, limits=limits, deadline=deadline)
    unreachable = GitError(
        f"commit {sha} is not reachable from any branch or tag of {repository_name}"
    )
    if commit is None:
        kind = _run(
            ["git", "cat-file", "-t", sha],
            cwd=probe,
            env=env,
            timeout=_timeout(limits, deadline),
        )
        if kind.returncode == 0:
            raise GitError(f"{sha} does not name a commit")
        raise unreachable
    # Best effort: with a commit-graph, `--contains` over thousands of
    # refs and a deep history is a generation-number lookup instead of a walk
    # per ref (measured: 5000 refs x 20000 commits, 0.65 s -> 0.02 s, plus
    # 0.06 s to write). Nothing it does may fail the deploy: a timeout or a
    # git that cannot start is swallowed, and it runs without `max_bytes`, so
    # the graph file (a few dozen bytes per already-fetched commit) cannot
    # push a probe that fitted `fetch_bytes` over the limit. The check below
    # still runs, bounded by the timeout, and fails closed.
    with contextlib.suppress(GitError):
        _run(
            ["git", "commit-graph", "write", "--reachable"],
            cwd=probe,
            env=env,
            timeout=_timeout(limits, deadline),
        )
    contains = _run(
        ["git", "for-each-ref", "--count=1", "--format=%(refname)", f"--contains={commit}"],
        cwd=probe,
        env=env,
        timeout=_timeout(limits, deadline),
    )
    if contains.returncode != 0 or not contains.stdout.strip():
        raise unreachable
    shutil.rmtree(probe, ignore_errors=True)
    return commit


def _commit_of(
    name: str, *, repo: Path, env: Mapping[str, str], limits: DeployLimits, deadline: float
) -> str | None:
    """Peel `name` to a commit SHA.

    Parameters
    ----------
    name
        ``FETCH_HEAD`` or a full object SHA (an annotated tag's SHA peels to its commit).
    repo
        The bare repository.
    env
        Git environment.
    limits
        Limits.
    deadline
        Job deadline.

    Returns
    -------
    str or None
        The commit SHA, or `None` if `name` is missing or not a commit.
    """
    resolved = _run(
        ["git", "rev-parse", "--verify", "--quiet", "--end-of-options", f"{name}^{{commit}}"],
        cwd=repo,
        env=env,
        timeout=_timeout(limits, deadline),
    )
    sha = resolved.stdout.strip()
    if resolved.returncode != 0 or not is_full_sha(sha):
        return None
    return sha
