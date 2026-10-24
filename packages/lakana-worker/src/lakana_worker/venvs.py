"""The venv cache: one uv-built environment per project.

A venv holds a project's dependencies only (`uv sync --frozen --no-dev
--no-default-groups --no-install-project --no-install-workspace`). Its key
covers the lockfile and the project's `pyproject.toml`. uv is run with
`--no-config`: the project's `[tool.uv]` settings and any `uv.toml` are not
read, so none of them changes an install or sends a credential anywhere
(review C1). The lock and the index definitions in `[[tool.uv.index]]` are
still read. It is built
in a temporary sibling directory and renamed into place; the marker file,
written last, tells a complete venv from a leftover, so two workers never see
a half-built one. Two lock files per key: the build lock serializes builds,
the use lock is held shared by every task that uses the venv and taken
exclusively, without blocking, by eviction. Only `bin/python` is ever run from
a venv, which keeps working after the rename; console scripts with their
absolute shebangs are not used.

The build's environment is built from scratch: nothing of the worker's own
environment reaches uv except `PATH`. uv is started through a launcher that
ties it to the worker's life, so a worker killed in the middle of a build
leaves no build running behind it.

A lock that uses a registered package index with a credential is built
with that credential in an index the worker defines itself, and with
`--no-build`; see `lakana_worker.index_credentials`. The credential is asked
for only when a build runs.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import hashlib
import os
import re
import secrets
import selectors
import shutil
import signal
import subprocess  # nosec B404 -- uv is invoked with fixed argv, no shell
import sys
import sysconfig
import tempfile
import threading
import time
from collections.abc import Callable, Generator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from lakana_worker.git import proxy_secrets
from lakana_worker.index_credentials import (
    IndexAccess,
    missing_wheel,
    no_wheel_message,
    redaction_values,
    refusal_message,
    refused_index,
    uv_index_value,
    wheels_only,
)
from lakana_worker.launcher.task_build import UV_SYNC_FLAGS
from lakana_worker.processes import OutputRedactor, is_private_directory, signal_group, tree_size
from lakana_worker.trust import certificate_hint

MARKER = ".lakana-complete"
"""Written last into a venv; its mtime is when the venv was last used."""

_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")
_UNSAFE = re.compile(r"[^A-Za-z0-9_.]")
_WATCH_SECONDS = 0.5
_ATTEMPTS = 3
_FAILED = "environment build failed"
_TAIL_BYTES = 64 * 1024
"""How much of uv's output is kept, unredacted and in memory only, to tell why a build failed."""

BuildStop = Callable[[], bool]
"""Says whether a build is to stop: the task was cancelled, lost its lease or the worker drains.
Asked between the steps of a build and at least twice a second while uv runs."""
_LOCK_POLL_SECONDS = 0.1
"""How often a build waiting for another build of its venv asks whether it is to stop."""
_STOPPED = (
    f"{_FAILED}: stopped before it ended (the task was cancelled, lost its lease, or the worker "
    "is stopping); nothing of it was kept"
)

IndexResolver = Callable[[], Sequence[IndexAccess]]
"""Returns the registered indexes a build uses, with their credentials decrypted. Called only
when a build really runs, on the thread that runs it; raises `VenvBuildRefused` with a message
that is safe to show when a credential cannot be read."""


class AcquireVenv(Protocol):
    """The shape of `acquire`, which a test may replace."""

    def __call__(
        self,
        settings: VenvSettings,
        lock_sha256: str,
        project: Path,
        on_log: Callable[[bytes], None],
        indexes: IndexResolver | None = None,
        /,
        *,
        stop: BuildStop | None = None,
    ) -> VenvLease:
        """Return the venv of a lockfile, building it on first use.

        Parameters
        ----------
        settings
            The cache settings.
        lock_sha256
            The lock digest.
        project
            The unpacked code directory.
        on_log
            Receives the build's redacted output.
        indexes
            Returns the registered indexes the lock uses, with their credentials.
        stop
            Says when the task is to stop.

        Returns
        -------
        VenvLease
            The venv.
        """
        ...


UV_NOT_STARTED = 127
"""Exit status of the launcher when uv itself could not be started."""

UV_LAUNCHER = f"""\
import ctypes, os, signal, sys
parent, uv = int(sys.argv[1]), sys.argv[2]
if sys.platform.startswith("linux"):
    # prctl(PR_SET_PDEATHSIG, SIGKILL)
    if ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGKILL) != 0:
        sys.exit("could not ask the kernel to end this build with its worker")
if os.getppid() != parent:
    sys.exit("the worker that started this build is gone")
try:
    os.execv(uv, sys.argv[2:])
except OSError as exc:
    print("could not run uv (" + (exc.strerror or type(exc).__name__) + ")", flush=True)
    os._exit({UV_NOT_STARTED})
"""
"""Starts uv so that it dies with the worker (Linux).

The worker enforces the build's time and size limits; a worker that was
killed enforces nothing, and uv would run on, with nobody to publish what it
builds. The request survives the `execv`, and it fires when the worker
*thread* that started the build exits, so that thread waits for the build. A
worker that died before the request was made is caught by the parent check.
Processes uv starts itself (the build backend of a source distribution) are
not covered.
"""


_builds_lock = threading.Lock()
_builds = 0


@contextlib.contextmanager
def _counted_build() -> Generator[None, None, None]:
    """Count a build as in progress for as long as the block runs.

    Yields
    ------
    None
        Nothing; the count drops again when the block ends, however it ends.
    """
    global _builds
    with _builds_lock:
        _builds += 1
    try:
        yield
    finally:
        with _builds_lock:
            _builds -= 1


def builds_in_progress() -> int:
    """Count the environment builds this worker process is running now.

    The trim of uv's package cache (`lakana_worker.caches`) runs only while this is 0.

    Returns
    -------
    int
        How many `uv sync` builds have started and not ended.
    """
    with _builds_lock:
        return _builds


class VenvBuildError(Exception):
    """The venv could not be built; the text is safe to store and to show."""


class VenvCacheUnsafe(VenvBuildError):
    """The cache directory or a lock path is unsafe: a configuration error, never transient."""


class VenvBuildRefused(VenvBuildError):
    """The build cannot succeed until somebody changes something; retrying is pointless.

    A package index refused the credential, a credential's secret is missing, or a package
    has no wheel while a credential is in play.
    """


class VenvBuildStopped(VenvBuildError):
    """The build was stopped because its task is to stop; nothing of it was kept."""


class _UvExited(VenvBuildError):
    """uv ran to its end and reported a failure; its output may say which."""


class Tee:
    """Passes uv's output on to the redactor and keeps its last bytes for classification.

    Parameters
    ----------
    sink
        The redactor.
    """

    def __init__(self, sink: OutputRedactor) -> None:
        self._sink = sink
        self._tail = bytearray()

    def feed(self, data: bytes) -> None:
        """Keep and forward a chunk.

        Parameters
        ----------
        data
            Bytes uv wrote.
        """
        self._tail += data
        del self._tail[:-_TAIL_BYTES]
        self._sink.feed(data)

    def close(self) -> None:
        """Flush the redactor."""
        self._sink.close()

    def text(self) -> str:
        """Return what was kept.

        Returns
        -------
        str
            The last `_TAIL_BYTES` of the output, undecodable bytes replaced. Never stored
            and never shown: it is not redacted.
        """
        return self._tail.decode("utf-8", "replace")


@dataclass(frozen=True)
class VenvSettings:
    """What the venv cache needs from the worker's settings.

    Parameters
    ----------
    uv
        The `uv` executable: an absolute path, or a name looked up on the
        worker's `PATH` when a build starts.
    cache
        Directory holding the venvs and their lock files.
    uv_cache
        uv's own download cache (`UV_CACHE_DIR`).
    keep
        How many venvs to keep, most recently used first.
    build_timeout
        Seconds one `uv sync` may run.
    max_bytes
        Largest on-disk size of one venv while it is built.
    index_url
        An extra package index; may carry a password, left out of `repr`.
    https_proxy
        Proxy URL; may carry a password, left out of `repr`.
    no_proxy
        Hosts that bypass the proxy.
    ca_bundle
        The worker's trust bundle (`lakana_worker.trust`): Mozilla's roots plus
        `LAKANA_RUN__CA_BUNDLE`.
    """

    uv: str
    cache: Path
    uv_cache: Path
    keep: int = 10
    build_timeout: float = 600.0
    max_bytes: int = 5 * 1024**3
    index_url: str | None = field(default=None, repr=False)
    https_proxy: str | None = field(default=None, repr=False)
    no_proxy: str | None = None
    ca_bundle: Path | None = None


@dataclass
class VenvLease:
    """A venv a task may use until it releases it.

    Parameters
    ----------
    path
        The venv directory.
    python
        Its interpreter, ``<path>/bin/python``.
    descriptor
        The open lock file holding the shared lock; `None` once released.
    key
        The environment key under DockerRunner (the launcher's volume); `None` for a local venv.
    """

    path: Path
    python: Path
    descriptor: int | None = field(default=None, repr=False)
    key: str | None = None

    def release(self) -> None:
        """Give the venv up; it may be evicted afterwards. Safe to call twice."""
        descriptor, self.descriptor = self.descriptor, None
        if descriptor is not None:
            os.close(descriptor)


def venv_key(lock_sha256: str, pyproject_sha256: str) -> str:
    """Name the venv of a project on this interpreter and platform.

    Parameters
    ----------
    lock_sha256
        SHA-256 of `uv.lock`, hex.
    pyproject_sha256
        SHA-256 of the project's `pyproject.toml`, hex.

    Returns
    -------
    str
        ``<sha256 of both>-py<major>.<minor>-<platform tag>``, safe as a
        directory name.
    """
    platform = _UNSAFE.sub("_", sysconfig.get_platform())
    digest = hashlib.sha256(f"{lock_sha256}:{pyproject_sha256}".encode()).hexdigest()
    return f"{digest}-py{sys.version_info.major}.{sys.version_info.minor}-{platform}"


def build_environment(
    settings: VenvSettings, home: Path, build: Path, accesses: Sequence[IndexAccess] = ()
) -> dict[str, str]:
    """Build the complete environment of one `uv sync`.

    Parameters
    ----------
    settings
        The cache settings.
    home
        A private, empty directory used as `HOME` and `TMPDIR`.
    build
        The directory uv creates the venv in.
    accesses
        The registered indexes the build uses. Each one with a credential
        becomes an index the worker itself defines in `UV_INDEX`, the
        credential inside its URL (see `lakana_worker.index_credentials`).

    Returns
    -------
    dict
        Nothing from the worker's own environment except `PATH`; the proxy,
        bypass list, CA file and indexes only when there are any.

    Raises
    ------
    ValueError
        If a credential cannot be encoded; `resolve_index_access` refuses such a
        secret first.
    """
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "TMPDIR": str(home),
        "UV_PROJECT_ENVIRONMENT": str(build),
        "UV_PYTHON_DOWNLOADS": "never",
        "UV_CACHE_DIR": str(settings.uv_cache),
        "UV_NO_PROGRESS": "1",
    }
    if settings.https_proxy is not None:
        env["HTTPS_PROXY"] = env["https_proxy"] = settings.https_proxy
    if settings.no_proxy is not None:
        env["NO_PROXY"] = env["no_proxy"] = settings.no_proxy
    if settings.ca_bundle is not None:
        env["SSL_CERT_FILE"] = env["LAKANA_CA_BUNDLE"] = str(settings.ca_bundle)
    indexes = uv_index_value(settings.index_url, accesses)
    if indexes is not None:
        env["UV_INDEX"] = indexes
    return env


def _refuse_link(path: Path, what: str) -> None:
    """Refuse a path that is a symbolic link.

    Parameters
    ----------
    path
        The path to check.
    what
        What the path is, for the message.

    Raises
    ------
    VenvCacheUnsafe
        If `path` is a symbolic link.
    """
    if path.is_symlink():
        raise VenvCacheUnsafe(f"{_FAILED}: the {what} is a symbolic link")


def _open_lock(path: Path) -> int:
    """Open (creating, mode 0600) a lock file without following a link.

    Parameters
    ----------
    path
        The lock file.

    Returns
    -------
    int
        The descriptor.

    Raises
    ------
    VenvCacheUnsafe
        If `path` is a symbolic link.
    VenvBuildError
        If `path` cannot be opened.
    """
    try:
        return os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise VenvCacheUnsafe(f"{_FAILED}: a lock file is a symbolic link") from exc
        raise VenvBuildError(f"{_FAILED}: {exc.strerror or 'filesystem error'}") from exc


def _prepare_cache(cache: Path) -> None:
    """Create the cache directory privately and check that it is safe.

    Missing parents are created with mode 0700 too. An existing directory must
    be a real directory owned by the worker's user that no one else can write to.

    Parameters
    ----------
    cache
        The cache directory.

    Raises
    ------
    VenvCacheUnsafe
        If the directory is not private.
    VenvBuildError
        If the directory cannot be created.
    """
    try:
        missing = [path for path in (cache, *cache.parents) if not os.path.lexists(path)]
        for path in reversed(missing):
            with contextlib.suppress(FileExistsError):
                os.mkdir(path, 0o700)
    except OSError as exc:
        raise VenvBuildError(f"{_FAILED}: {exc.strerror or 'filesystem error'}") from exc
    if not is_private_directory(cache, 0o077):
        raise VenvCacheUnsafe(
            f"{_FAILED}: the venv cache directory is not private (it must be a real "
            "directory owned by the worker's user, mode 0700; an operator must fix it)"
        )


def _remove_unused(settings: VenvSettings, key: str, target: Path) -> None:
    """Delete a venv directory nobody holds a lease on.

    Parameters
    ----------
    settings
        The cache settings.
    key
        The venv's key.
    target
        The venv directory.

    Raises
    ------
    VenvBuildError
        If another task holds the venv's use lock.
    """
    descriptor = _open_lock(settings.cache / f"{key}.lock")
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise VenvBuildError(f"{_FAILED}: the venv is in use by another task") from exc
        shutil.rmtree(target)
    finally:
        os.close(descriptor)


def _run_uv(
    argv: list[str],
    env: dict[str, str],
    cwd: Path,
    *,
    timeout: float,
    max_bytes: int,
    watch: Path,
    sink: Tee,
    stop: BuildStop | None = None,
) -> None:
    """Run uv to completion, streaming its output and enforcing both limits and the stop.

    uv is started through `UV_LAUNCHER`, so that it does not outlive a worker
    that is killed while it builds.

    Parameters
    ----------
    argv
        The command.
    env
        `build_environment`'s output.
    cwd
        Directory to run in.
    timeout
        Seconds before the process group is killed.
    max_bytes
        Largest size of `watch`.
    watch
        The directory uv builds the venv in.
    sink
        Receives stdout and stderr, merged.
    stop
        Asked at every step; when it says so, the process group is killed.

    Raises
    ------
    VenvBuildStopped
        If `stop` said so before uv ended.
    VenvBuildError
        If uv could not be started, timed out, or grew `watch` past `max_bytes`.
    _UvExited
        If uv exited with a non-zero status.
    """
    try:
        process = subprocess.Popen(  # nosec B603 -- fixed argv, no shell, minimal env
            [sys.executable, "-I", "-c", UV_LAUNCHER, str(os.getpid()), *argv],
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError as exc:
        raise VenvBuildError(
            f"{_FAILED}: could not start the build process ({exc.strerror or exc})"
        ) from exc
    pipe = process.stdout
    assert pipe is not None  # nosec B101 -- stdout is PIPE
    selector = selectors.DefaultSelector()
    selector.register(pipe, selectors.EVENT_READ)
    end = time.monotonic() + timeout
    next_watch = time.monotonic() + _WATCH_SECONDS
    timed_out = f"{_FAILED}: timed out after {int(timeout)}s"
    too_large = f"{_FAILED}: the venv exceeded {max_bytes} bytes"
    failure: str | None = None
    code = -1
    try:
        while failure is None:
            if stop is not None and stop():
                failure = _STOPPED
                break
            remaining = end - time.monotonic()
            if remaining <= 0:
                failure = timed_out
                break
            step = min(remaining, _WATCH_SECONDS)
            if selector.get_map():
                if selector.select(step):
                    chunk = os.read(pipe.fileno(), 65536)
                    if chunk:
                        sink.feed(chunk)
                    else:
                        selector.unregister(pipe)
            else:
                try:
                    code = process.wait(timeout=step)
                except subprocess.TimeoutExpired:
                    pass
                else:
                    break
            if time.monotonic() >= next_watch:
                next_watch = time.monotonic() + _WATCH_SECONDS
                if tree_size(watch) > max_bytes:
                    failure = too_large
    finally:
        selector.close()
        signal_group(process, signal.SIGKILL)
        process.wait()
        pipe.close()
        sink.close()
    if failure == _STOPPED:
        raise VenvBuildStopped(failure)
    if failure is None and tree_size(watch) > max_bytes:
        failure = too_large
    if failure is not None:
        raise VenvBuildError(failure)
    if code == UV_NOT_STARTED:
        raise VenvBuildError(f"{_FAILED}: could not run uv (the task log has the reason)")
    if code != 0:
        raise _UvExited(f"{_FAILED}: uv exited with status {code}")


def _build(
    settings: VenvSettings,
    key: str,
    project: Path,
    on_log: Callable[[bytes], None],
    indexes: IndexResolver | None = None,
    stop: BuildStop | None = None,
) -> None:
    """Build the venv of `key` and publish it atomically.

    The caller holds the key's build lock.

    Parameters
    ----------
    settings
        The cache settings.
    key
        The venv's key.
    project
        The unpacked code directory holding `pyproject.toml` and `uv.lock`.
    on_log
        Receives uv's redacted output.
    indexes
        Returns the registered indexes the lock uses, with their credentials;
        `None` for a lock that uses none.
    stop
        Says when to stop; the build directory is removed and nothing is published.

    Raises
    ------
    VenvBuildStopped
        If `stop` said so before the venv was published.
    VenvBuildRefused
        If a credential cannot be read (raised by `indexes`), a registered
        index answered 401 or 403, or a package has no wheel while a
        credential is in play.
    VenvBuildError
        If uv cannot be found, or the build fails or exceeds a limit.
    _UvExited
        If uv failed for a reason that is neither a refusal nor a missing wheel.
    """
    target = settings.cache / key
    build = settings.cache / f".build-{key}-{os.getpid()}-{secrets.token_hex(4)}"
    accesses = list(indexes()) if indexes is not None else []
    sink = Tee(
        OutputRedactor(
            [
                *proxy_secrets(settings.https_proxy),
                *proxy_secrets(settings.index_url),
                *redaction_values(accesses),
            ],
            on_log,
        )
    )
    uv: str | None = settings.uv
    if not os.path.isabs(settings.uv):
        found = shutil.which(settings.uv)
        # A relative `PATH` entry yields a relative result; the build runs from another directory.
        uv = None if found is None else os.path.abspath(found)
    if uv is None:
        # The launcher starts uv with `execv`, which does not search `PATH`.
        raise VenvBuildError(
            f"{_FAILED}: uv ({settings.uv!r}) was not found on the worker's PATH; install uv "
            "where the worker runs"
        )
    argv = [
        uv,
        *UV_SYNC_FLAGS,
        # A source distribution's build runs that package's code where the credential is.
        *(["--no-build"] if wheels_only(accesses) else []),
        "--python",
        sys.executable,
        "--project",
        str(project),
    ]
    try:
        if target.exists():
            _remove_unused(settings, key, target)
        home = Path(tempfile.mkdtemp(prefix="lakana-venv-"))
        try:
            settings.uv_cache.mkdir(mode=0o700, parents=True, exist_ok=True)
            try:
                _run_uv(
                    argv,
                    build_environment(settings, home, build, accesses),
                    home,
                    timeout=settings.build_timeout,
                    max_bytes=settings.max_bytes,
                    watch=build,
                    sink=sink,
                    stop=stop,
                )
            except _UvExited as exc:
                output = sink.text()
                refused = refused_index(output, accesses)
                if refused is not None:
                    raise VenvBuildRefused(f"{_FAILED}: {refusal_message(refused)}") from exc
                package = missing_wheel(output) if wheels_only(accesses) else None
                if package is not None:
                    raise VenvBuildRefused(f"{_FAILED}: {no_wheel_message(package)}") from exc
                hint = certificate_hint(output)
                if hint is not None:
                    raise VenvBuildError(f"{exc}: {hint}") from exc
                raise
            if not (build / "bin" / "python").exists():
                raise VenvBuildError(f"{_FAILED}: uv did not create an interpreter")
            (build / MARKER).write_text(key + "\n", encoding="utf-8")
            os.rename(build, target)
        finally:
            shutil.rmtree(home, ignore_errors=True)
    except OSError as exc:
        raise VenvBuildError(f"{_FAILED}: {exc.strerror or 'filesystem error'}") from exc
    finally:
        shutil.rmtree(build, ignore_errors=True)


def _last_used(path: Path) -> float | None:
    """Read when a venv was last used.

    Parameters
    ----------
    path
        A cache entry.

    Returns
    -------
    float or None
        The marker's mtime; `None` if the entry has no marker or vanished.
    """
    try:
        return (path / MARKER).stat().st_mtime
    except OSError:
        return None


def _evict(settings: VenvSettings, key: str) -> None:
    """Keep the most recently used venvs and sweep stale build directories.

    Called after a build, holding `key`'s build lock. A venv another
    task holds its use lock on is skipped.

    Parameters
    ----------
    settings
        The cache settings.
    key
        The venv just built; never evicted here.
    """
    stale_before = time.time() - 2 * settings.build_timeout
    used: list[tuple[float, Path]] = []
    for child in settings.cache.iterdir():
        if child.name.startswith(".build-"):
            with contextlib.suppress(OSError):
                if child.stat().st_mtime < stale_before:
                    shutil.rmtree(child, ignore_errors=True)
            continue
        last_used = _last_used(child)
        if last_used is not None:
            used.append((last_used, child))
    used.sort(reverse=True)
    for _, path in used[settings.keep :]:
        if path.name == key:
            continue
        try:
            descriptor = os.open(
                settings.cache / f"{path.name}.lock",
                os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
            )
        except OSError:
            continue
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                continue
            (path / MARKER).unlink(missing_ok=True)
            shutil.rmtree(path, ignore_errors=True)
        finally:
            os.close(descriptor)


def _lock_building(descriptor: int, stop: BuildStop | None) -> None:
    """Take a venv's build lock, giving up when the task is to stop.

    Parameters
    ----------
    descriptor
        The build lock file.
    stop
        Asked while another build holds the lock; `None` waits as long as it takes.

    Raises
    ------
    VenvBuildStopped
        If `stop` said so while the lock was held by another build.
    """
    if stop is None:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        return
    while True:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if stop():
                raise VenvBuildStopped(_STOPPED) from None
            time.sleep(_LOCK_POLL_SECONDS)
        else:
            return


def _usable(target: Path) -> bool:
    """Tell whether a cached venv is complete and its interpreter still resolves.

    Parameters
    ----------
    target
        The venv directory.

    Returns
    -------
    bool
        `True` if the marker is a file and ``bin/python`` resolves.
    """
    return (target / MARKER).is_file() and (target / "bin" / "python").exists()


def acquire(
    settings: VenvSettings,
    lock_sha256: str,
    project: Path,
    on_log: Callable[[bytes], None],
    indexes: IndexResolver | None = None,
    *,
    stop: BuildStop | None = None,
) -> VenvLease:
    """Return the venv of a lockfile, building it if this is its first use.

    Parameters
    ----------
    settings
        The cache settings.
    lock_sha256
        The project version's `lock_sha256`; the key also covers the project's
        `pyproject.toml`.
    project
        The unpacked code directory.
    on_log
        Receives uv's redacted output if a build runs.
    indexes
        Returns the registered indexes the lock uses, with their credentials.
        Called only if a build runs: a cached venv is used without reading a
        secret or contacting an index.
    stop
        Says when the task is to stop (#291): a wait for another task's build of
        the venv ends, and a running build is killed and leaves nothing behind.

    Returns
    -------
    VenvLease
        The venv, protected from eviction until released.

    Raises
    ------
    VenvBuildStopped
        If `stop` said so before the venv was there.
    VenvCacheUnsafe
        If the cache directory, a lock file or the venv directory is unsafe.
    VenvBuildRefused
        If the build cannot succeed until a credential, an index or the lock changes.
    VenvBuildError
        If `lock_sha256` is not a SHA-256 hex digest, `pyproject.toml` cannot be
        read, the build fails, or the
        venv keeps being removed while it is acquired.
    """
    if _SHA256.match(lock_sha256) is None:
        raise VenvBuildError(f"{_FAILED}: the project version has no valid lock digest")
    try:
        pyproject_sha256 = hashlib.sha256((project / "pyproject.toml").read_bytes()).hexdigest()
    except OSError as exc:
        raise VenvBuildError(
            f"{_FAILED}: the deployed code has no readable pyproject.toml; redeploy"
        ) from exc
    key = venv_key(lock_sha256, pyproject_sha256)
    _prepare_cache(settings.cache)
    target = settings.cache / key
    _refuse_link(target, "venv directory")
    use = _open_lock(settings.cache / f"{key}.lock")
    try:
        try:
            for _ in range(_ATTEMPTS):
                fcntl.flock(use, fcntl.LOCK_SH)
                if _usable(target):
                    os.utime(target / MARKER)
                    return VenvLease(path=target, python=target / "bin" / "python", descriptor=use)
                fcntl.flock(use, fcntl.LOCK_UN)
                building = _open_lock(settings.cache / f"{key}.build.lock")
                try:
                    _lock_building(building, stop)
                    if not _usable(target):
                        if stop is not None and stop():
                            raise VenvBuildStopped(_STOPPED)
                        with _counted_build():
                            _build(settings, key, project, on_log, indexes, stop)
                        with contextlib.suppress(OSError):
                            _evict(settings, key)
                finally:
                    os.close(building)
        except OSError as exc:
            raise VenvBuildError(f"{_FAILED}: {exc.strerror or 'filesystem error'}") from exc
        raise VenvBuildError(f"{_FAILED}: the venv was removed while it was being acquired")
    except BaseException:
        os.close(use)
        raise
