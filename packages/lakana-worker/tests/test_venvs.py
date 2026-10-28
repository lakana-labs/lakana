"""The venv cache: key, build, lock, atomic publish, eviction, limits.

`uv` is a small script here: the unit tier proves the cache's own logic; the
real `uv sync` runs in the integration tier.
"""

from __future__ import annotations

import dataclasses
import fcntl
import hashlib
import json
import os
import signal
import subprocess  # nosec B404 -- the test interpreter with fixed argv
import sys
import sysconfig
import threading
import time
from pathlib import Path
from typing import Any

import pytest

import lakana_worker.venvs as venvs_module
from lakana_worker.index_credentials import IndexAccess
from lakana_worker.venvs import (
    MARKER,
    VenvBuildBroken,
    VenvBuildError,
    VenvBuildRefused,
    VenvBuildStopped,
    VenvCacheUnsafe,
    VenvSettings,
    acquire,
    build_environment,
    builds_in_progress,
    venv_key,
)

_SHA_A, _SHA_B, _SHA_C = "a" * 64, "b" * 64, "c" * 64
_PYPROJECT = '[project]\nname = "demo"\nversion = "0"\n'


def _key(lock_sha256: str) -> str:
    """Name the venv of a lock with the fake project's `pyproject.toml`.

    Parameters
    ----------
    lock_sha256
        The lock digest.

    Returns
    -------
    str
        The cache key.
    """
    return venv_key(lock_sha256, hashlib.sha256(_PYPROJECT.encode()).hexdigest())


_FAKE_UV = """#!{python}
import json, os, sys, time
from pathlib import Path

here = Path(__file__).resolve().parent
behaviour = json.loads((here / "behaviour.json").read_text())
with (here / "calls.jsonl").open("a") as handle:
    record = {{
        "argv": sys.argv[1:], "env": dict(os.environ), "cwd": os.getcwd(), "pid": os.getpid(),
        "home": sorted(os.listdir(os.environ["HOME"])),
    }}
    handle.write(json.dumps(record) + "\\n")
if "hex" in behaviour:
    sys.stdout.buffer.write(bytes.fromhex(behaviour["hex"]))
    sys.stdout.buffer.flush()
print(behaviour.get("output", "Resolved 2 packages"), flush=True)
mode = behaviour.get("mode", "ok")
if mode == "fail":
    sys.exit(behaviour.get("code", 2))
venv = Path(os.environ["UV_PROJECT_ENVIRONMENT"])
(venv / "bin").mkdir(parents=True)
if mode == "big":
    (venv / "blob").write_bytes(b"x" * behaviour["bytes"])
if mode == "close-then-grow":
    os.close(1)
    os.close(2)
    with (venv / "blob").open("wb") as blob:
        for _ in range(600):
            blob.write(b"x" * 50_000)
            blob.flush()
            time.sleep(0.05)
if mode == "close-then-sleep":
    os.close(1)
    os.close(2)
time.sleep(behaviour.get("sleep", 0))
if mode != "no-python":
    os.symlink(sys.executable, venv / "bin" / "python")
"""


class Fake:
    """A scripted `uv` and the settings pointing at it.

    Parameters
    ----------
    root
        A per-test directory.
    """

    def __init__(self, root: Path) -> None:
        self.directory = root / "fake-uv"
        self.directory.mkdir()
        script = self.directory / "uv"
        script.write_text(_FAKE_UV.format(python=sys.executable))
        script.chmod(0o755)
        self.behave()
        self.project = root / "project"
        self.project.mkdir()
        self.pyproject = _PYPROJECT
        (self.project / "pyproject.toml").write_text(self.pyproject)
        self.settings = VenvSettings(
            uv=str(script), cache=root / "venvs", uv_cache=root / "uv-cache", build_timeout=20.0
        )
        self.log: list[bytes] = []

    def behave(self, **behaviour: object) -> None:
        """Set what the next `uv` runs do.

        Parameters
        ----------
        **behaviour
            ``mode`` (``ok``, ``fail``, ``big``, ``close-then-sleep``,
            ``no-python``, ``close-then-grow``), ``output``, ``hex``, ``code``,
            ``bytes``, ``sleep``.
        """
        (self.directory / "behaviour.json").write_text(json.dumps(behaviour))

    def calls(self) -> list[dict]:
        """Return every recorded `uv` invocation.

        Returns
        -------
        list of dict
            ``argv``, ``env`` and ``cwd`` per call.
        """
        path = self.directory / "calls.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    def acquire(
        self,
        sha: str = _SHA_A,
        indexes: venvs_module.IndexResolver | None = None,
        stop: venvs_module.BuildStop | None = None,
        **overrides: object,
    ) -> venvs_module.VenvLease:
        """Acquire a venv with this fake's settings.

        Parameters
        ----------
        sha
            The lock digest.
        indexes
            The index resolver, as the worker passes it.
        stop
            Says when the build is to stop, as the worker passes it.
        **overrides
            Fields of `VenvSettings` to replace.

        Returns
        -------
        VenvLease
            The lease.
        """
        settings = dataclasses.replace(self.settings, **overrides)  # type: ignore[arg-type]
        return acquire(settings, sha, self.project, self.log.append, indexes, stop=stop)

    def output(self) -> bytes:
        """Return everything logged so far.

        Returns
        -------
        bytes
            The redacted build output.
        """
        return b"".join(self.log)


@pytest.fixture
def fake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Fake:
    """Build the fake `uv`; the size watch runs every 50 ms.

    Parameters
    ----------
    tmp_path
        Pytest's per-test directory.
    monkeypatch
        Speeds up the watch.

    Returns
    -------
    Fake
        The fake.
    """
    monkeypatch.setattr(venvs_module, "_WATCH_SECONDS", 0.05)
    return Fake(tmp_path)


def test_venv_key_names_the_project_the_python_and_the_platform() -> None:
    key = venv_key(_SHA_A, _SHA_B)
    digest, _, rest = key.partition("-py")
    assert len(digest) == 64
    assert digest not in (_SHA_A, _SHA_B)
    assert rest == (
        f"{sys.version_info.major}.{sys.version_info.minor}-"
        f"{sysconfig.get_platform().replace('-', '_')}"
    )
    assert "/" not in key


def test_venv_key_depends_on_both_the_lock_and_the_pyproject() -> None:
    assert venv_key(_SHA_A, _SHA_B) == venv_key(_SHA_A, _SHA_B)
    assert venv_key(_SHA_A, _SHA_B) != venv_key(_SHA_A, _SHA_C)
    assert venv_key(_SHA_A, _SHA_B) != venv_key(_SHA_C, _SHA_B)
    assert venv_key(_SHA_A, _SHA_B) != venv_key(_SHA_B, _SHA_A)


def test_the_same_lock_with_another_pyproject_is_another_venv(fake: Fake) -> None:
    first = fake.acquire()
    first.release()
    (fake.project / "pyproject.toml").write_text(
        fake.pyproject + "\n[tool.uv]\ndefault-groups = []\n"
    )
    second = fake.acquire()
    second.release()
    assert first.path != second.path
    assert len(fake.calls()) == 2


def test_an_identical_lock_and_pyproject_reuse_the_venv(fake: Fake) -> None:
    first = fake.acquire()
    first.release()
    second = fake.acquire()
    second.release()
    assert first.path == second.path
    assert len(fake.calls()) == 1


def test_a_project_without_a_readable_pyproject_is_refused(fake: Fake) -> None:
    (fake.project / "pyproject.toml").unlink()
    with pytest.raises(VenvBuildError, match=r"pyproject\.toml"):
        fake.acquire()


def test_the_first_acquire_builds_with_the_exact_command_and_a_scrubbed_environment(
    fake: Fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "must-not-leak")
    monkeypatch.setenv("HTTPS_PROXY", "http://ambient:3128")
    lease = fake.acquire()
    try:
        target = fake.settings.cache / _key(_SHA_A)
        assert lease.path == target
        assert lease.python == target / "bin" / "python"
        assert lease.python.exists()
        assert (target / MARKER).is_file()
        (call,) = fake.calls()
        assert call["argv"] == [
            "sync",
            "--frozen",
            "--no-config",
            "--no-dev",
            "--no-default-groups",
            "--no-install-project",
            "--no-install-workspace",
            "--python",
            sys.executable,
            "--project",
            str(fake.project),
        ]
        env = call["env"]
        assert set(env) - {"LC_CTYPE", "__CF_USER_TEXT_ENCODING"} == {
            "PATH",
            "HOME",
            "TMPDIR",
            "UV_PROJECT_ENVIRONMENT",
            "UV_PYTHON_DOWNLOADS",
            "UV_CACHE_DIR",
            "UV_NO_PROGRESS",
        }
        assert env["UV_PYTHON_DOWNLOADS"] == "never"
        assert env["UV_CACHE_DIR"] == str(fake.settings.uv_cache)
        build = Path(env["UV_PROJECT_ENVIRONMENT"])
        assert build.parent == fake.settings.cache and build.name.startswith(".build-")
        assert not build.exists()
        assert env["HOME"] == env["TMPDIR"] == call["cwd"]
        assert not Path(env["HOME"]).exists()
        assert fake.output() == b"Resolved 2 packages\n"
    finally:
        lease.release()
        lease.release()


def test_network_settings_reach_uv_and_secrets_are_redacted_and_hidden(fake: Fake) -> None:
    fake.behave(output="GET https://svc:hunter2@pypi.corp/simple via http://px:s3cr3t@proxy:3128")
    bundle = fake.directory / "ca.pem"
    lease = fake.acquire(
        index_url="https://svc:hunter2@pypi.corp/simple",
        https_proxy="http://px:s3cr3t@proxy:3128",
        no_proxy="pypi.corp",
        ca_bundle=bundle,
    )
    lease.release()
    env = fake.calls()[0]["env"]
    assert env["UV_INDEX"] == "https://svc:hunter2@pypi.corp/simple"
    assert env["HTTPS_PROXY"] == env["https_proxy"] == "http://px:s3cr3t@proxy:3128"
    assert env["NO_PROXY"] == env["no_proxy"] == "pypi.corp"
    assert env["SSL_CERT_FILE"] == env["LAKANA_CA_BUNDLE"] == str(bundle)
    assert b"hunter2" not in fake.output() and b"s3cr3t" not in fake.output()
    settings = VenvSettings(
        uv="uv",
        cache=Path("/c"),
        uv_cache=Path("/u"),
        index_url="https://svc:hunter2@pypi.corp/simple",
        https_proxy="http://px:s3cr3t@proxy:3128",
    )
    assert "hunter2" not in repr(settings) and "s3cr3t" not in repr(settings)
    plain = build_environment(fake.settings, Path("/h"), Path("/b"))
    assert not {"UV_INDEX", "HTTPS_PROXY", "NO_PROXY", "SSL_CERT_FILE", "LAKANA_CA_BUNDLE"} & set(
        plain
    )


def test_a_second_acquire_reuses_the_venv_and_records_the_use(fake: Fake) -> None:
    fake.acquire().release()
    marker = fake.settings.cache / _key(_SHA_A) / MARKER
    os.utime(marker, (1_000_000, 1_000_000))
    lease = fake.acquire()
    lease.release()
    assert len(fake.calls()) == 1
    assert marker.stat().st_mtime > 1_000_000


def test_a_build_counts_as_in_progress_while_it_runs_and_not_after(
    fake: Fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[int] = []
    real = venvs_module._build

    def counting(*args: Any, **kwargs: Any) -> None:  # ruff: ignore[any-type]
        """Record the count during the build, then build.

        Parameters
        ----------
        *args
            Passed through.
        **kwargs
            Passed through.
        """
        seen.append(builds_in_progress())
        real(*args, **kwargs)

    monkeypatch.setattr(venvs_module, "_build", counting)
    assert builds_in_progress() == 0
    fake.acquire().release()
    fake.behave(mode="fail")
    with pytest.raises(VenvBuildError):
        fake.acquire(_SHA_B)
    assert seen == [1, 1]
    assert builds_in_progress() == 0


def test_a_failed_build_surfaces_uv_output_redacted_and_leaves_nothing(fake: Fake) -> None:
    fake.behave(mode="fail", code=3, output="error: no wheel for x; proxy http://u:pw123@p:1")
    with pytest.raises(VenvBuildError) as caught:
        fake.acquire(https_proxy="http://u:pw123@p:1")
    assert str(caught.value) == "environment build failed: uv exited with status 3"
    assert b"error: no wheel for x" in fake.output()
    assert b"pw123" not in fake.output()
    assert [p.name for p in fake.settings.cache.iterdir() if not p.name.endswith(".lock")] == []
    fake.behave()
    fake.acquire().release()
    assert len(fake.calls()) == 2


def test_a_build_without_an_interpreter_fails(fake: Fake) -> None:
    fake.behave(mode="no-python")
    with pytest.raises(VenvBuildError, match="environment build failed: uv did not create"):
        fake.acquire()


def test_a_missing_uv_fails_cleanly(fake: Fake) -> None:
    with pytest.raises(VenvBuildError, match="environment build failed: could not run uv"):
        fake.acquire(uv=str(fake.directory / "absent"))
    assert b"could not run uv (No such file or directory)" in fake.output()


def test_a_build_process_that_cannot_be_started_fails_cleanly(
    fake: Fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise OSError(24, "Too many open files")

    monkeypatch.setattr(venvs_module.subprocess, "Popen", refuse)
    with pytest.raises(VenvBuildError) as caught:
        fake.acquire()
    assert str(caught.value) == (
        "environment build failed: could not start the build process (Too many open files)"
    )


def test_a_bare_uv_name_is_resolved_on_the_path(
    fake: Fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The launcher `execv`s uv, which does not search `PATH`: the worker resolves the name."""
    monkeypatch.setenv("PATH", f"{fake.directory}{os.pathsep}{os.environ['PATH']}")
    fake.acquire(uv="uv").release()
    assert len(fake.calls()) == 1


def test_a_uv_found_through_a_relative_path_entry_is_made_absolute(
    fake: Fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The child runs elsewhere, so a relative `PATH` entry must not leak into its argv."""
    monkeypatch.chdir(fake.directory.parent)
    monkeypatch.setenv("PATH", fake.directory.name)
    started: list[list[str]] = []
    real = venvs_module.subprocess.Popen

    def record(argv: list[str], **kwargs: object) -> object:
        started.append(argv)
        return real(argv, **kwargs)  # ty: ignore[no-matching-overload]

    monkeypatch.setattr(venvs_module.subprocess, "Popen", record)
    fake.acquire(uv="uv").release()
    (argv,) = started
    assert argv[5] == str(fake.directory / "uv")


def test_a_uv_name_that_is_nowhere_on_the_path_says_so(fake: Fake) -> None:
    with pytest.raises(VenvBuildError) as caught:
        fake.acquire(uv="lakana-no-such-uv")
    assert str(caught.value) == (
        "environment build failed: uv ('lakana-no-such-uv') was not found on the worker's "
        "PATH; install uv where the worker runs"
    )
    assert fake.calls() == []


def test_uv_is_started_so_that_it_dies_with_the_worker(
    fake: Fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The launcher gets the worker's pid, then uv's own command line, unchanged."""
    started: list[list[str]] = []
    real = venvs_module.subprocess.Popen

    def record(argv: list[str], **kwargs: object) -> object:
        started.append(argv)
        return real(argv, **kwargs)  # ty: ignore[no-matching-overload]

    monkeypatch.setattr(venvs_module.subprocess, "Popen", record)
    fake.acquire().release()
    (argv,) = started
    assert argv[:3] == [sys.executable, "-I", "-c"]
    assert argv[3] == venvs_module.UV_LAUNCHER
    assert argv[4:7] == [str(os.getpid()), fake.settings.uv, "sync"]
    assert fake.calls()[0]["argv"] == argv[6:]


def test_a_malformed_lock_digest_is_refused(fake: Fake) -> None:
    for digest in ("", "short", "A" * 64, "../" + "a" * 61):
        with pytest.raises(VenvBuildError, match="no valid lock digest"):
            fake.acquire(digest)
    assert not fake.settings.cache.exists()


def test_the_build_is_killed_at_its_time_limit(fake: Fake) -> None:
    fake.behave(sleep=30)
    started = time.monotonic()
    with pytest.raises(VenvBuildError) as caught:
        fake.acquire(build_timeout=0.3)
    assert str(caught.value) == "environment build failed: timed out after 0s"
    assert time.monotonic() - started < 10


def _leftovers(fake: Fake) -> list[str]:
    """List what a build left in the venv cache besides its lock files.

    Parameters
    ----------
    fake
        The fake.

    Returns
    -------
    list of str
        Venv directories and build directories, by name.
    """
    return sorted(p.name for p in fake.settings.cache.iterdir() if not p.name.endswith(".lock"))


def test_a_stop_during_the_build_kills_it_and_caches_nothing(fake: Fake) -> None:
    """#291: a cancel, a lost lease or a drain ends the build within a second or so."""
    fake.behave(sleep=30)
    stop = threading.Event()
    threading.Timer(0.3, stop.set).start()
    started = time.monotonic()

    with pytest.raises(VenvBuildStopped, match="stopped"):
        fake.acquire(stop=stop.is_set)

    assert time.monotonic() - started < 5
    assert not _alive(fake.calls()[0]["pid"])
    assert _leftovers(fake) == []
    fake.behave()
    fake.acquire().release()
    assert len(fake.calls()) == 2, "the next acquire builds again: nothing was cached"


def test_a_stop_before_the_build_runs_no_uv(fake: Fake) -> None:
    """#291: a task stopped before its build starts never starts one."""
    with pytest.raises(VenvBuildStopped):
        fake.acquire(stop=lambda: True)

    assert fake.calls() == []


@pytest.mark.slow
def test_a_stop_while_another_task_builds_the_same_venv_ends_the_wait(fake: Fake) -> None:
    """#291: a stopped task does not wait for another task's build of its venv."""
    fake.behave(sleep=3)
    first: list[object] = []
    builder = threading.Thread(target=lambda: first.append(fake.acquire()))
    builder.start()
    deadline = time.monotonic() + 5
    while not fake.calls():
        assert time.monotonic() < deadline, "the first build never started"
        time.sleep(0.01)
    stop = threading.Event()
    threading.Timer(0.3, stop.set).start()
    started = time.monotonic()

    with pytest.raises(VenvBuildStopped):
        fake.acquire(stop=stop.is_set)

    assert time.monotonic() - started < 2
    builder.join(10)
    assert isinstance(first[0], venvs_module.VenvLease)
    first[0].release()


def test_a_build_that_closes_its_output_and_hangs_is_still_killed(fake: Fake) -> None:
    fake.behave(mode="close-then-sleep", sleep=30)
    with pytest.raises(VenvBuildError, match="timed out"):
        fake.acquire(build_timeout=0.3)


def test_the_build_is_killed_when_the_venv_grows_too_large(fake: Fake) -> None:
    fake.behave(mode="big", bytes=200_000, sleep=30)
    started = time.monotonic()
    with pytest.raises(VenvBuildError) as caught:
        fake.acquire(max_bytes=100_000)
    assert str(caught.value) == "environment build failed: the venv exceeded 100000 bytes"
    assert time.monotonic() - started < 10


def test_the_size_is_checked_once_more_after_uv_exits(
    fake: Fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No periodic check gets in first, however slowly the two interpreters start.
    monkeypatch.setattr(venvs_module, "_WATCH_SECONDS", 30.0)
    fake.behave(mode="big", bytes=200_000)
    with pytest.raises(VenvBuildError, match="the venv exceeded 100000 bytes"):
        fake.acquire(max_bytes=100_000)


def test_a_leftover_directory_without_the_marker_is_rebuilt(fake: Fake) -> None:
    leftover = fake.settings.cache / _key(_SHA_A)
    fake.settings.cache.mkdir(mode=0o700)
    (leftover / "bin").mkdir(parents=True)
    (leftover / "junk").write_text("x")
    fake.acquire().release()
    assert (leftover / MARKER).is_file()
    assert not (leftover / "junk").exists()


def test_two_tasks_wanting_one_venv_build_it_once(fake: Fake) -> None:
    fake.behave(sleep=0.5)
    leases: list[venvs_module.VenvLease] = []
    errors: list[BaseException] = []

    def work() -> None:
        try:
            leases.append(fake.acquire())
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=work) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert errors == []
    assert len(leases) == 3
    assert len(fake.calls()) == 1
    for lease in leases:
        assert lease.python.exists()
        lease.release()


def _built(fake: Fake, sha: str, used_at: float) -> Path:
    """Build a venv and set when it was last used.

    Parameters
    ----------
    fake
        The fake.
    sha
        The lock digest.
    used_at
        The marker's mtime.

    Returns
    -------
    Path
        The venv directory.
    """
    fake.acquire(sha).release()
    path = fake.settings.cache / _key(sha)
    os.utime(path / MARKER, (used_at, used_at))
    return path


def test_eviction_keeps_the_most_recently_used(fake: Fake) -> None:
    oldest = _built(fake, _SHA_A, 1_000)
    newer = _built(fake, _SHA_B, 2_000)
    fake.acquire(_SHA_C, keep=2).release()
    assert not oldest.exists()
    assert newer.exists()
    assert (fake.settings.cache / _key(_SHA_C) / MARKER).is_file()


def test_a_venv_in_use_is_not_evicted(fake: Fake) -> None:
    held = fake.acquire(_SHA_A)
    oldest = fake.settings.cache / _key(_SHA_A)
    os.utime(oldest / MARKER, (1_000, 1_000))
    _built(fake, _SHA_B, 2_000)
    fake.acquire(_SHA_C, keep=1).release()
    assert held.python.exists()
    assert not (fake.settings.cache / _key(_SHA_B)).exists()
    held.release()
    fake.acquire("d" * 64, keep=1).release()
    assert not oldest.exists()


def test_stale_build_directories_are_swept(fake: Fake) -> None:
    fake.settings.cache.mkdir(mode=0o700, parents=True)
    stale = fake.settings.cache / ".build-old-1-abcd"
    fresh = fake.settings.cache / ".build-new-2-abcd"
    for path in (stale, fresh):
        path.mkdir()
    long_ago = time.time() - 10 * fake.settings.build_timeout
    os.utime(stale, (long_ago, long_ago))
    (fake.settings.cache / "stray-file").write_text("x")
    fake.acquire().release()
    assert not stale.exists()
    assert fresh.exists()


def test_a_venv_removed_between_build_and_use_is_rebuilt(
    fake: Fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lock conversion is not atomic; the marker is checked again."""
    real = venvs_module._evict
    removed: list[int] = []

    def evict_and_remove(settings: VenvSettings, key: str) -> None:
        real(settings, key)
        if not removed:
            removed.append(1)
            (settings.cache / key / MARKER).unlink()

    monkeypatch.setattr(venvs_module, "_evict", evict_and_remove)
    fake.acquire().release()
    assert len(fake.calls()) == 2


def test_a_venv_that_keeps_disappearing_gives_up(
    fake: Fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    def always_remove(settings: VenvSettings, key: str) -> None:
        (settings.cache / key / MARKER).unlink()

    monkeypatch.setattr(venvs_module, "_evict", always_remove)
    with pytest.raises(VenvBuildError, match="removed while it was being acquired"):
        fake.acquire()
    lock = fake.settings.cache / f"{_key(_SHA_A)}.lock"
    descriptor = os.open(lock, os.O_RDWR)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(descriptor)


def test_the_venv_just_built_survives_eviction_even_if_it_is_the_oldest(fake: Fake) -> None:
    """Another venv with a future marker mtime sorts first; the new one is outside `keep`."""
    other = _built(fake, _SHA_A, time.time() + 10_000)
    fake.acquire(_SHA_B, keep=1).release()
    assert other.exists()
    assert (fake.settings.cache / _key(_SHA_B) / MARKER).is_file()


def test_the_argv_carries_no_credential_and_the_env_only_the_network_settings(fake: Fake) -> None:
    fake.acquire(
        index_url="https://svc:hunter2@pypi.corp/simple",
        https_proxy="http://px:s3cr3t@proxy:3128",
        no_proxy="pypi.corp",
        ca_bundle=fake.directory / "ca.pem",
    ).release()
    (call,) = fake.calls()
    assert "hunter2" not in " ".join(call["argv"]) and "s3cr3t" not in " ".join(call["argv"])
    assert set(call["env"]) - {"LC_CTYPE", "__CF_USER_TEXT_ENCODING"} == {
        "PATH", "HOME", "TMPDIR", "UV_PROJECT_ENVIRONMENT", "UV_PYTHON_DOWNLOADS",
        "UV_CACHE_DIR", "UV_NO_PROGRESS", "UV_INDEX", "HTTPS_PROXY", "https_proxy",
        "NO_PROXY", "no_proxy", "SSL_CERT_FILE", "LAKANA_CA_BUNDLE",
    }  # fmt: skip


def test_decoded_and_basic_auth_forms_are_redacted_through_acquire(fake: Fake) -> None:
    import base64

    pair = "u@x:p@ss"
    encoded = base64.b64encode(pair.encode()).decode()
    fake.behave(output=f"decoded p@ss and user u@x:p@ss and basic {encoded}")
    fake.acquire(https_proxy="http://u%40x:p%40ss@proxy:1").release()
    out = fake.output()
    assert b"p@ss" not in out and encoded.encode() not in out and b"u@x" not in out


def test_invalid_utf8_output_is_forwarded_and_still_redacted(fake: Fake) -> None:
    fake.behave(hex="fffe80", output="after hunter2")
    fake.acquire(index_url="https://svc:hunter2@pypi.corp/simple").release()
    assert fake.output().startswith(b"\xff\xfe\x80") and b"hunter2" not in fake.output()


def test_descriptors_above_1024_do_not_break_the_build(fake: Fake) -> None:
    import resource

    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    wanted = 1300
    if hard != resource.RLIM_INFINITY and hard < wanted:
        pytest.skip("RLIMIT_NOFILE cannot be raised")
    held: list[int] = []
    try:
        if soft != resource.RLIM_INFINITY and soft < wanted:
            resource.setrlimit(resource.RLIMIT_NOFILE, (wanted, hard))
        while len(held) < 1100:
            held.append(os.dup(0))
        assert max(held) >= 1024
        fake.acquire().release()
    finally:
        for descriptor in held:
            os.close(descriptor)
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))


def test_the_size_cap_still_applies_after_uv_closes_its_output(fake: Fake) -> None:
    fake.behave(mode="close-then-grow")
    started = time.monotonic()
    with pytest.raises(VenvBuildError) as caught:
        fake.acquire(max_bytes=100_000, build_timeout=15.0)
    assert str(caught.value) == "environment build failed: the venv exceeded 100000 bytes"
    assert time.monotonic() - started < 10


def test_a_venv_whose_interpreter_is_gone_is_rebuilt(fake: Fake) -> None:
    fake.acquire().release()
    python = fake.settings.cache / _key(_SHA_A) / "bin" / "python"
    python.unlink()
    python.symlink_to("/nonexistent/python")
    lease = fake.acquire()
    lease.release()
    assert len(fake.calls()) == 2
    assert lease.python.exists()


def test_a_private_cache_with_a_planted_marker_is_the_normal_reuse_path(fake: Fake) -> None:
    fake.settings.cache.mkdir(mode=0o700)
    target = fake.settings.cache / _key(_SHA_A)
    (target / "bin").mkdir(parents=True)
    (target / "bin" / "python").symlink_to(sys.executable)
    (target / MARKER).write_text("x")
    fake.acquire().release()
    assert fake.calls() == []


def test_the_cache_directory_is_created_private_with_its_parents(fake: Fake) -> None:
    nested = fake.settings.cache.parent / "a" / "b" / "venvs"
    fake.acquire(cache=nested).release()
    for directory in (nested, nested.parent, nested.parent.parent):
        assert directory.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize("mode", [0o770, 0o707, 0o777, 0o772, 0o755, 0o750, 0o705])
def test_a_cache_directory_open_to_others_is_refused(fake: Fake, mode: int) -> None:
    fake.settings.cache.mkdir()
    fake.settings.cache.chmod(mode)
    with pytest.raises(VenvBuildError, match=r"environment build failed: .*not private"):
        fake.acquire()
    assert fake.calls() == []


def test_a_cache_directory_owned_by_someone_else_is_refused(
    fake: Fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake.settings.cache.mkdir(mode=0o700)
    monkeypatch.setattr(os, "geteuid", lambda: os.stat(fake.settings.cache).st_uid + 1)
    with pytest.raises(VenvBuildError, match="not private"):
        fake.acquire()


def test_a_symlinked_cache_directory_is_refused(fake: Fake, tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    fake.settings.cache.symlink_to(real)
    with pytest.raises(VenvBuildError, match="not private"):
        fake.acquire()
    assert list(real.iterdir()) == []


@pytest.mark.parametrize("suffix", [".lock", ".build.lock"])
def test_a_symlinked_lock_file_is_refused_and_its_target_untouched(
    fake: Fake, tmp_path: Path, suffix: str
) -> None:
    fake.settings.cache.mkdir(mode=0o700)
    victim = tmp_path / "victim"
    (fake.settings.cache / f"{_key(_SHA_A)}{suffix}").symlink_to(victim)
    if suffix == ".build.lock":
        fake.settings.cache.joinpath(f"{_key(_SHA_A)}.lock").touch()
    with pytest.raises(VenvBuildError, match="symbolic link"):
        fake.acquire()
    assert not victim.exists()
    assert fake.calls() == []


def test_a_symlinked_venv_directory_is_refused(fake: Fake, tmp_path: Path) -> None:
    fake.settings.cache.mkdir(mode=0o700)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (fake.settings.cache / _key(_SHA_A)).symlink_to(elsewhere)
    with pytest.raises(VenvBuildError, match="symbolic link"):
        fake.acquire()
    assert elsewhere.exists() and fake.calls() == []


def test_eviction_skips_a_venv_whose_lock_cannot_be_opened(fake: Fake, tmp_path: Path) -> None:
    old = _built(fake, _SHA_A, 1_000)
    lock = fake.settings.cache / f"{_key(_SHA_A)}.lock"
    lock.unlink()
    lock.symlink_to(tmp_path / "victim")
    fake.acquire(_SHA_B, keep=1).release()
    assert old.exists() and not (tmp_path / "victim").exists()


def test_an_eviction_error_never_fails_an_acquire(
    fake: Fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(settings: VenvSettings, key: str) -> None:
        raise FileNotFoundError("vanished")

    monkeypatch.setattr(venvs_module, "_evict", broken)
    lease = fake.acquire()
    lease.release()
    assert lease.python.exists()


def test_a_cache_entry_that_vanishes_during_eviction_is_skipped(
    fake: Fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake.acquire(_SHA_A).release()
    real = Path.stat

    def stat(self: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        if self.name == MARKER and _key(_SHA_A) in str(self):
            raise FileNotFoundError(str(self))
        return real(self, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "stat", stat)
    fake.acquire(_SHA_B, keep=1).release()
    assert (fake.settings.cache / _key(_SHA_A)).exists()


def test_a_stale_build_directory_that_vanishes_is_skipped(
    fake: Fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake.settings.cache.mkdir(mode=0o700)
    (fake.settings.cache / ".build-gone-1-abcd").mkdir()
    real = Path.stat

    def stat(self: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        if self.name.startswith(".build-gone"):
            raise FileNotFoundError(str(self))
        return real(self, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "stat", stat)
    fake.acquire().release()


def test_a_temporary_directory_that_cannot_be_made_is_a_build_error(
    fake: Fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*args: object, **kwargs: object) -> str:
        raise OSError(28, "No space left on device", "/secret/path")

    monkeypatch.setattr(venvs_module.tempfile, "mkdtemp", refuse)
    with pytest.raises(VenvBuildError) as caught:
        fake.acquire()
    assert str(caught.value) == "environment build failed: No space left on device"
    assert fake.calls() == []


def test_an_uv_cache_that_cannot_be_made_is_a_build_error_and_leaks_no_directory(
    fake: Fake, tmp_path: Path
) -> None:
    blocker = tmp_path / "uv-cache-file"
    blocker.write_text("x")
    leaked_before = set(Path(tempfile_dir()).glob("lakana-venv-*"))
    with pytest.raises(VenvBuildError, match="environment build failed: File exists"):
        fake.acquire(uv_cache=blocker)
    assert set(Path(tempfile_dir()).glob("lakana-venv-*")) == leaked_before


def tempfile_dir() -> str:
    """Return the directory `tempfile.mkdtemp` uses.

    Returns
    -------
    str
        The temporary directory.
    """
    import tempfile

    return tempfile.gettempdir()


def test_a_failed_rename_is_a_build_error_without_paths(
    fake: Fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise OSError(28, "No space left on device", "/secret/path")

    monkeypatch.setattr(venvs_module.os, "rename", refuse)
    with pytest.raises(VenvBuildError) as caught:
        fake.acquire()
    assert str(caught.value) == "environment build failed: No space left on device"
    assert [p.name for p in fake.settings.cache.iterdir() if not p.name.endswith(".lock")] == []


def test_an_oserror_without_a_message_is_still_a_build_error(
    fake: Fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise OSError()

    monkeypatch.setattr(venvs_module.os, "rename", refuse)
    with pytest.raises(VenvBuildError, match="environment build failed: filesystem error"):
        fake.acquire()


def test_a_lock_path_that_cannot_be_opened_is_a_build_error(fake: Fake) -> None:
    fake.settings.cache.mkdir(mode=0o700)
    (fake.settings.cache / f"{_key(_SHA_A)}.lock").mkdir()
    with pytest.raises(VenvBuildError, match="environment build failed: Is a directory"):
        fake.acquire()


def test_a_cache_directory_that_cannot_be_created_is_a_build_error(
    fake: Fake, tmp_path: Path
) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x")
    with pytest.raises(VenvBuildError, match="environment build failed: Not a directory"):
        fake.acquire(cache=blocker / "venvs")


def test_a_venv_in_use_with_a_broken_interpreter_is_not_deleted(fake: Fake) -> None:
    holder = fake.acquire()
    python = holder.path / "bin" / "python"
    python.unlink()
    python.symlink_to("/nonexistent/python")
    with pytest.raises(VenvBuildError, match=r"environment build failed: .*in use"):
        fake.acquire()
    assert (holder.path / MARKER).is_file() and len(fake.calls()) == 1
    holder.release()
    fake.acquire().release()
    assert len(fake.calls()) == 2


def test_a_cache_directory_the_worker_creates_itself_passes_the_check(fake: Fake) -> None:
    fake.acquire().release()
    assert fake.settings.cache.stat().st_mode & 0o777 == 0o700
    fake.acquire().release()


def test_unsafe_cache_and_lock_paths_raise_the_distinct_error(fake: Fake, tmp_path: Path) -> None:
    assert issubclass(VenvCacheUnsafe, VenvBuildError)
    fake.settings.cache.mkdir()
    fake.settings.cache.chmod(0o777)
    with pytest.raises(VenvCacheUnsafe):
        fake.acquire()
    fake.settings.cache.chmod(0o700)
    (fake.settings.cache / f"{_key(_SHA_A)}.lock").symlink_to(tmp_path / "v")
    with pytest.raises(VenvCacheUnsafe):
        fake.acquire()
    (fake.settings.cache / f"{_key(_SHA_A)}.lock").unlink()
    (fake.settings.cache / _key(_SHA_A)).symlink_to(tmp_path)
    with pytest.raises(VenvCacheUnsafe):
        fake.acquire()


def test_a_build_failure_is_not_the_unsafe_error(fake: Fake) -> None:
    fake.behave(mode="fail")
    with pytest.raises(VenvBuildError) as caught:
        fake.acquire()
    assert not isinstance(caught.value, VenvCacheUnsafe)


_BUILD_IN_ANOTHER_PROCESS = """\
import sys
from pathlib import Path

from lakana_worker.venvs import VenvSettings, acquire

uv, cache, uv_cache, project = sys.argv[1:]
settings = VenvSettings(uv=uv, cache=Path(cache), uv_cache=Path(uv_cache), build_timeout=60.0)
acquire(settings, "a" * 64, Path(project), lambda data: None)
"""


def _alive(pid: int) -> bool:
    """Say whether a process still exists.

    Parameters
    ----------
    pid
        The process id.

    Returns
    -------
    bool
        `False` once the process is gone.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="PR_SET_PDEATHSIG is Linux")
def test_a_build_does_not_outlive_a_worker_that_was_killed(fake: Fake) -> None:
    """SIGKILL the process that builds: nothing is left to enforce the build's time limit."""
    fake.behave(sleep=60)
    worker = subprocess.Popen(  # nosec B603 -- the test interpreter, fixed argv
        [
            sys.executable,
            "-c",
            _BUILD_IN_ANOTHER_PROCESS,
            fake.settings.uv,
            str(fake.settings.cache),
            str(fake.settings.uv_cache),
            str(fake.project),
        ]
    )
    pid = None
    try:
        deadline = time.monotonic() + 20
        while not fake.calls() and time.monotonic() < deadline:
            assert worker.poll() is None, "the building process ended before uv started"
            time.sleep(0.05)
        (call,) = fake.calls()
        pid = call["pid"]
        assert _alive(pid)
        worker.kill()
        worker.wait(timeout=10)
        deadline = time.monotonic() + 5
        while _alive(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _alive(pid), "uv outlived the worker that started it"
    finally:
        worker.kill()
        worker.wait(timeout=10)
        if pid is not None and _alive(pid):
            os.kill(pid, signal.SIGKILL)


def test_a_shared_lock_failure_is_a_build_error(
    fake: Fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(descriptor: int, operation: int) -> None:
        raise OSError(5, "Input/output error", "/secret")

    monkeypatch.setattr(venvs_module.fcntl, "flock", refuse)
    with pytest.raises(VenvBuildError) as caught:
        fake.acquire()
    assert str(caught.value) == "environment build failed: Input/output error"


def test_a_failed_use_record_is_a_build_error_and_releases_the_lock(
    fake: Fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake.acquire().release()

    def refuse(*args: object, **kwargs: object) -> None:
        raise OSError(30, "Read-only file system", "/secret")

    monkeypatch.setattr(venvs_module.os, "utime", refuse)
    with pytest.raises(VenvBuildError, match="environment build failed: Read-only file system"):
        fake.acquire()
    descriptor = os.open(fake.settings.cache / f"{_key(_SHA_A)}.lock", os.O_RDWR)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(descriptor)


def test_an_evictor_paused_before_locking_cannot_delete_a_venv_another_task_leased(
    fake: Fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review R1: lock files are never unlinked, so a paused evictor still contends on the lease.

    Evictor E2 has opened the oldest venv's use lock but not yet locked it. Meanwhile
    another evictor removes that venv and a task rebuilds it and holds a lease. E2 must
    then fail to lock (the task holds it shared) and leave the venv alone.
    """
    old = _built(fake, _SHA_A, 1_000)
    _built(fake, _SHA_B, 2_000)
    reached, resume = threading.Event(), threading.Event()
    paused: list[bool] = []
    real = fcntl.flock

    def flock(descriptor: int, operation: int) -> None:
        if (
            threading.current_thread().name == "E2"
            and operation == fcntl.LOCK_EX | fcntl.LOCK_NB
            and not paused
        ):
            paused.append(True)
            reached.set()
            resume.wait(10)
        real(descriptor, operation)

    monkeypatch.setattr(venvs_module.fcntl, "flock", flock)
    keep_one = dataclasses.replace(fake.settings, keep=1)
    e2 = threading.Thread(target=venvs_module._evict, args=(keep_one, _key(_SHA_C)), name="E2")
    e2.start()
    assert reached.wait(10)
    venvs_module._evict(keep_one, _key(_SHA_C))
    assert not old.exists()
    monkeypatch.setattr(venvs_module, "_evict", lambda settings, key: None)
    lease = fake.acquire(_SHA_A)
    try:
        resume.set()
        e2.join(10)
        assert lease.python.exists()
    finally:
        lease.release()


_TEAM = IndexAccess(
    "team", "https://pkgs.example.org/simple", "dev", "team-index", "ci", "p@ss word"
)
_OPEN = IndexAccess("open", "https://open.example.org/simple", "dev")
_REFUSAL = (
    "  \N{MULTIPLICATION SIGN} Failed to download `team-utils==1.4.0`\n"
    "  ├─▶ Failed to fetch:\n"
    "  │   `https://pkgs.example.org/files/team_utils-1.4.0-py3-none-any.whl`\n"
    "  ╰─▶ HTTP status client error (401 Unauthorized) for url\n"
    "      (https://pkgs.example.org/files/team_utils-1.4.0-py3-none-any.whl)\n"
)
_NO_WHEEL = (
    "error: Distribution `sdonly==1.0.0 @ registry+https://pkgs.example.org/simple` can't be "
    "installed because it is marked as `--no-build` but has no binary distribution"
)


def test_a_credential_adds_the_index_and_no_build_and_only_then(fake: Fake) -> None:
    fake.acquire(
        _SHA_A, lambda: [_OPEN, _TEAM], index_url="https://op:pw@mirror.corp/simple"
    ).release()
    fake.acquire(_SHA_B, lambda: [_OPEN]).release()
    fake.acquire(_SHA_C).release()
    with_credential, without, plain = fake.calls()
    assert with_credential["env"]["UV_INDEX"] == (
        "https://op:pw@mirror.corp/simple "
        "lakana-registered-0=https://ci:p%40ss%20word@pkgs.example.org/simple"
    )
    argv = with_credential["argv"]
    assert "--no-config" in argv
    assert argv[argv.index("--no-install-workspace") + 1] == "--no-build"
    assert argv.count("--no-build") == 1
    for call in (without, plain):
        assert "UV_INDEX" not in call["env"]
        assert "--no-build" not in call["argv"]
        assert "--no-config" in call["argv"]
    assert not Path(with_credential["env"]["HOME"]).exists()


def test_only_uv_index_carries_the_credential_and_no_file_does(fake: Fake) -> None:
    """Review R5.7: uv's per-index variables and a netrc follow the host name, not the URL."""
    fake.acquire(_SHA_A, lambda: [_TEAM]).release()
    (call,) = fake.calls()
    carrying = [
        name
        for name, value in call["env"].items()
        if any(part in value for part in ("p@ss word", "p%40ss%20word", "p@ss"))
    ]
    assert carrying == ["UV_INDEX"]
    assert not any(name.startswith("UV_INDEX_") for name in call["env"])
    assert not any(name.upper().endswith(("_USERNAME", "_PASSWORD")) for name in call["env"])
    assert not any("p%40ss" in part or "p@ss" in part for part in call["argv"])
    assert call["home"] == []
    assert "NETRC" not in call["env"]
    assert call["cwd"] == call["env"]["HOME"]


def test_the_resolver_runs_once_for_a_build_and_never_for_a_cached_venv(fake: Fake) -> None:
    calls: list[int] = []

    def resolver() -> list[IndexAccess]:
        """Count the calls.

        Returns
        -------
        list of IndexAccess
            The credentialed index.
        """
        calls.append(1)
        return [_TEAM]

    fake.acquire(_SHA_A, resolver).release()
    fake.acquire(_SHA_A, resolver).release()
    assert calls == [1]
    assert len(fake.calls()) == 1


def test_every_form_of_the_credential_is_redacted_from_the_build_output(fake: Fake) -> None:
    fake.behave(
        output="a p@ss word b ci:p@ss word c p%40ss%20word d ci:p%40ss%20word e "
        "Y2k6cEBzcyB3b3Jk f https://ci:p%40ss%20word@pkgs.example.org/simple"
    )
    fake.acquire(_SHA_A, lambda: [_TEAM]).release()
    assert fake.output() == b"a *** b *** c *** d *** e *** f https://***@pkgs.example.org/simple\n"


def test_a_refused_credential_is_a_refusal_that_names_index_secret_and_command(fake: Fake) -> None:
    fake.behave(mode="fail", code=1, output=_REFUSAL)
    with pytest.raises(VenvBuildRefused) as caught:
        fake.acquire(_SHA_A, lambda: [_OPEN, _TEAM])
    assert str(caught.value) == (
        "environment build failed: The package index 'team' refused the credential in secret "
        "'team-index' of environment 'dev'. Set it again with `lakana secret rotate team-index "
        "--env dev --values-stdin`. If the password is right, check that its account may read "
        "the package: the index also answers 403 when the account may not read a package or "
        "file."
    )
    assert "p@ss word" not in str(caught.value)
    assert list(fake.settings.cache.glob(".build-*")) == []


def test_a_refusal_names_the_index_of_the_refused_path_when_two_share_a_host(fake: Fake) -> None:
    """Review R5.8: two indexes on one host and port, each with its own secret."""
    alpha = IndexAccess(
        "alpha", "https://pkgs.example.org/alpha/simple", "dev", "a-secret", "", "x"
    )
    beta = IndexAccess("beta", "https://pkgs.example.org/beta/simple", "dev", "b-secret", "", "y")
    refusal = _REFUSAL.replace("/files/", "/beta/files/")
    fake.behave(mode="fail", code=1, output=refusal)
    with pytest.raises(VenvBuildRefused) as caught:
        fake.acquire(_SHA_A, lambda: [alpha, beta])
    assert "'beta' refused the credential in secret 'b-secret'" in str(caught.value)
    assert "alpha" not in str(caught.value)


def test_a_package_without_a_wheel_is_a_refusal_only_when_a_credential_is_in_play(
    fake: Fake,
) -> None:
    fake.behave(mode="fail", code=2, output=_NO_WHEEL)
    with pytest.raises(VenvBuildRefused) as caught:
        fake.acquire(_SHA_A, lambda: [_TEAM])
    assert str(caught.value) == (
        "environment build failed: sdonly has no wheel for this platform and Python; publish a "
        "wheel to your index, or remove the dependency. A project that uses a package index "
        "with a credential installs wheels only."
    )
    for resolver in (None, lambda: [_OPEN]):
        with pytest.raises(VenvBuildError) as plain:
            fake.acquire(_SHA_B, resolver)
        assert not isinstance(plain.value, VenvBuildRefused)
        assert str(plain.value) == "environment build failed: uv exited with status 2"


def test_other_failures_stay_ordinary_build_errors(fake: Fake) -> None:
    """A refusal from another host, a 5xx and a plain failure are retried as today."""
    elsewhere = _REFUSAL.replace("pkgs.example.org", "files.other.example")
    unavailable = _REFUSAL.replace(
        "client error (401 Unauthorized)", "server error (503 Service Unavailable)"
    )
    for output in (elsewhere, unavailable, "error: something else"):
        fake.behave(mode="fail", code=1, output=output)
        with pytest.raises(VenvBuildError) as caught:
            fake.acquire(_SHA_A, lambda: [_TEAM])
        assert not isinstance(caught.value, VenvBuildRefused)
        assert str(caught.value) == "environment build failed: uv exited with status 1"


def test_only_the_end_of_a_long_output_is_looked_at(fake: Fake) -> None:
    filler = "x" * 1000 + "\n"
    fake.behave(mode="fail", code=1, output=filler * 80 + _REFUSAL)
    with pytest.raises(VenvBuildRefused):
        fake.acquire(_SHA_A, lambda: [_TEAM])
    fake.behave(mode="fail", code=1, output=_REFUSAL + filler * 80)
    with pytest.raises(VenvBuildError) as caught:
        fake.acquire(_SHA_B, lambda: [_TEAM])
    assert not isinstance(caught.value, VenvBuildRefused)


def test_a_resolver_that_refuses_stops_the_build_before_uv_runs(fake: Fake) -> None:
    def resolver() -> list[IndexAccess]:
        """Refuse as `python_tasks` does for a missing secret.

        Raises
        ------
        VenvBuildRefused
            Always.
        """
        raise VenvBuildRefused("environment build failed: the secret is missing")

    with pytest.raises(VenvBuildRefused, match="the secret is missing"):
        fake.acquire(_SHA_A, resolver)
    assert fake.calls() == []
    fake.acquire(_SHA_A).release()
    assert len(fake.calls()) == 1


def test_an_index_without_a_credential_that_asks_for_one_is_a_refusal(fake: Fake) -> None:
    """The index was registered without a secret, uv gets a 401: no retry will help."""
    bare = IndexAccess("team", "https://pkgs.example.org/simple", "dev")
    fake.behave(mode="fail", code=1, output=_REFUSAL)
    with pytest.raises(VenvBuildRefused) as caught:
        fake.acquire(_SHA_A, lambda: [bare])
    assert str(caught.value) == (
        "environment build failed: The package index 'team' asked for a credential, and none is "
        "registered for it. An administrator binds one with `lakana index update team "
        "--credential-secret NAME` and creates the secret NAME in environment 'dev'."
    )


def test_the_workers_own_environment_is_not_touched_by_a_build(
    fake: Fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("UV_INDEX", raising=False)
    before = dict(os.environ)
    fake.acquire(_SHA_A, lambda: [_TEAM]).release()
    assert dict(os.environ) == before
    fake.behave(mode="fail", code=1, output=_REFUSAL)
    with pytest.raises(VenvBuildRefused):
        fake.acquire(_SHA_B, lambda: [_TEAM])
    fake.behave(mode="fail", code=2, output="error: something else")
    with pytest.raises(VenvBuildError):
        fake.acquire(_SHA_C, lambda: [_TEAM])
    assert dict(os.environ) == before
    assert "UV_INDEX" not in os.environ


def test_a_local_build_failing_on_an_untrusted_certificate_names_the_setting(fake: Fake) -> None:
    """The local runner's build says the same as a container build: set LAKANA_RUN__CA_BUNDLE."""
    fake.behave(mode="fail", code=2, output="error: invalid peer certificate: UnknownIssuer")
    with pytest.raises(VenvBuildError) as caught:
        fake.acquire()
    assert str(caught.value).startswith("environment build failed: uv exited with status 2: ")
    assert "LAKANA_RUN__CA_BUNDLE" in str(caught.value)


_BACKEND_FAILED = (
    "Resolved 2 packages in 3ms\n"
    "   Building brokenpkg==1.0\n"
    "  \N{MULTIPLICATION SIGN} Failed to build `brokenpkg==1.0`\n"
    "  ├─▶ The build backend returned an error\n"
    "  ╰─▶ Call to `backend.build_wheel` failed (exit status: 1)\n"
    "\n"
    "      [stderr]\n"
    "      RuntimeError: this package cannot be built here\n"
    "\n"
    "hint: Build failures usually indicate a problem with the package or the build environment\n"
)
"""What uv 0.12 prints when a source distribution's own build fails."""
_NO_PLATFORM = (
    "error: Distribution `macpkg==1.0 @ registry+https://pypi.org/simple` can't be installed "
    "because it doesn't have a source distribution or wheel for the current platform\n"
    "\n"
    "hint: You're on Linux (`manylinux_2_43_x86_64`), but `macpkg` (v1.0) only has wheels for "
    "the following platform: `macosx_11_0_arm64`\n"
)
"""What uv 0.12 prints for a locked package with no file the worker can install."""
_BUILD_REQUIRES_UNREACHABLE = (
    "  \N{MULTIPLICATION SIGN} Failed to build `brokenpkg==1.0`\n"
    "  ├─▶ Failed to resolve requirements from `build-system.requires`\n"
    "  ├─▶ Failed to fetch: `https://pypi.org/simple/setuptools/`\n"
    "  ├─▶ error sending request for url (https://pypi.org/simple/setuptools/)\n"
    "  ╰─▶ Connection refused (os error 111)\n"
)
"""A build that failed because the network did: a retry can succeed."""


def test_a_package_whose_own_build_fails_is_not_retried(fake: Fake) -> None:
    """Issue #48: the same sources build the same way on every attempt."""
    fake.behave(mode="fail", code=1, output=_BACKEND_FAILED)
    with pytest.raises(VenvBuildBroken) as caught:
        fake.acquire()
    assert not isinstance(caught.value, VenvBuildRefused)
    assert str(caught.value) == (
        "environment build failed: the package brokenpkg could not be built from its source "
        "(the task's log has the build's output), and every attempt builds it the same way. "
        "Depend on a version of brokenpkg that publishes a wheel for the worker's platform and "
        "the task's Python, or on one whose build works there, then run `uv lock`, commit and "
        "deploy again."
    )
    assert b"this package cannot be built here" in fake.output()


def test_a_package_without_any_file_for_the_platform_is_not_retried(fake: Fake) -> None:
    """Issue #48: no wheel and no source distribution stays so until the lock changes."""
    fake.behave(mode="fail", code=2, output=_NO_PLATFORM)
    with pytest.raises(VenvBuildBroken) as caught:
        fake.acquire()
    assert str(caught.value) == (
        "environment build failed: the package macpkg has no wheel for the worker's platform "
        "and the task's Python, and no source distribution, so it cannot be installed. Depend "
        "on a version of macpkg that publishes such a wheel (the task's log names the "
        "platforms it has), then run `uv lock`, commit and deploy again."
    )


def test_a_build_that_failed_on_the_network_is_still_retried(fake: Fake) -> None:
    """A source build whose build requirements could not be fetched may work next time."""
    for output in (
        _BUILD_REQUIRES_UNREACHABLE,
        _BACKEND_FAILED + "      urllib.error.URLError: Connection refused (os error 111)\n",
        _NO_PLATFORM + _REFUSAL.replace("client error (401 Unauthorized)", "server error (503)"),
    ):
        fake.behave(mode="fail", code=1, output=output)
        with pytest.raises(VenvBuildError) as caught:
            fake.acquire()
        assert not isinstance(caught.value, VenvBuildBroken)
        assert str(caught.value) == "environment build failed: uv exited with status 1"


def test_a_refusal_wins_over_a_broken_build(fake: Fake) -> None:
    """A refused credential says what to fix first; it is the more specific message."""
    fake.behave(mode="fail", code=1, output=_REFUSAL + _BACKEND_FAILED)
    with pytest.raises(VenvBuildRefused):
        fake.acquire(_SHA_A, lambda: [_TEAM])
