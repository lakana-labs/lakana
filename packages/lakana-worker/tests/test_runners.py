"""Unpacking a code tarball and running one task process."""

from __future__ import annotations

import contextlib
import dataclasses
import errno
import gzip
import hashlib
import io
import json
import os
import signal
import stat
import subprocess
import sys
import tarfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import lakana_worker.runners as runners_module
from lakana.runner import TaskDescription, parse_description
from lakana_core.runs.failures import FailureCode
from lakana_core.secrets.forms import secret_forms
from lakana_worker.processes import remove_private_tree
from lakana_worker.runners import (
    MAX_ERROR_MESSAGE_CHARS,
    RESULT_FILE,
    CodeUnpackError,
    CodeUnpackFailed,
    LocalProcessRunner,
    PythonTask,
    RunningTask,
    RunOutcome,
    StopReason,
    StopSignal,
    TaskRegistry,
    child_environment,
    token_secrets,
    unpack_code,
)

_TOKEN = "lakana_task_k1abc_s3cr3t_part-x"
_RUN_BOUND_SECONDS = 45


def _tarball(*members: tuple[tarfile.TarInfo, bytes | None]) -> bytes:
    """Build a gzipped tar from explicit members.

    Parameters
    ----------
    *members
        ``(info, content)`` pairs; `content` is `None` for non-files.

    Returns
    -------
    bytes
        The gzipped archive.
    """
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as archive:
        for info, content in members:
            archive.addfile(info, io.BytesIO(content) if content is not None else None)
    return gzip.compress(raw.getvalue(), mtime=0)


def _file(
    name: str, content: bytes = b"x = 1\n", mode: int = 0o644
) -> tuple[tarfile.TarInfo, bytes]:
    """Build a regular-file member.

    Parameters
    ----------
    name
        Its path.
    content
        Its bytes.
    mode
        Its permission bits.

    Returns
    -------
    tuple
        The member and its content.
    """
    info = tarfile.TarInfo(name)
    info.size = len(content)
    info.mode = mode
    return info, content


def _other(name: str, kind: bytes, linkname: str = "") -> tuple[tarfile.TarInfo, None]:
    """Build a member that is not a regular file.

    Parameters
    ----------
    name
        Its path.
    kind
        A `tarfile` type constant.
    linkname
        The link target.

    Returns
    -------
    tuple
        The member, without content.
    """
    info = tarfile.TarInfo(name)
    info.type = kind
    info.linkname = linkname
    return info, None


def _attempt(root: Path, name: str = "attempt") -> Path:
    """Create a private attempt directory.

    Parameters
    ----------
    root
        Where to create it.
    name
        Its name.

    Returns
    -------
    Path
        The directory, mode 0700.
    """
    path = root / name
    path.mkdir(mode=0o700)
    path.chmod(0o700)
    return path


def _unpack(tarball: bytes, attempt: Path, **limits: int) -> Path:
    """Unpack with the right digest and generous limits.

    Parameters
    ----------
    tarball
        The archive.
    attempt
        The private attempt directory; the tree goes to ``<attempt>/code``.
    **limits
        `max_bytes` / `max_members` overrides.

    Returns
    -------
    Path
        The code directory.
    """
    options = {"max_bytes": 1024 * 1024, "max_members": 100}
    options.update(limits)
    return unpack_code(tarball, hashlib.sha256(tarball).hexdigest(), attempt / "code", **options)


def test_unpack_creates_a_read_only_tree(tmp_path: Path) -> None:
    tarball = _tarball(
        _other("assets", tarfile.DIRTYPE),
        _file("assets/gold/scores.py"),
        _file("src/helper.py"),
        _file("run.sh", b"#!/bin/sh\n", mode=0o755),
        _file("pyproject.toml", b"[project]\n"),
    )
    attempt = _attempt(tmp_path)
    tree = _unpack(tarball, attempt)
    assert tree == attempt / "code"
    assert (tree / "assets" / "gold" / "scores.py").read_bytes() == b"x = 1\n"
    assert stat.S_IMODE((tree / "src" / "helper.py").stat().st_mode) == 0o444
    assert stat.S_IMODE((tree / "run.sh").stat().st_mode) == 0o555
    assert stat.S_IMODE((tree / "assets").stat().st_mode) == 0o555
    assert stat.S_IMODE(tree.stat().st_mode) == 0o555
    assert [p.name for p in attempt.iterdir()] == ["code"]
    assert remove_private_tree(tree) is True
    assert list(attempt.iterdir()) == []


def test_two_attempts_unpack_independently(tmp_path: Path) -> None:
    """Nothing is shared between attempts: code that poisons its tree harms only itself."""
    tarball = _tarball(_file("a.py"))
    first = _unpack(tarball, _attempt(tmp_path, "one"))
    first.chmod(0o700)
    (first / "a.py").chmod(0o600)
    (first / "a.py").write_text("poisoned\n")
    (first / "extra.py").write_text("planted\n")
    second = _unpack(tarball, _attempt(tmp_path, "two"))
    assert second != first
    assert (second / "a.py").read_text() == "x = 1\n"
    assert not (second / "extra.py").exists()


def test_unpack_refuses_an_existing_target(tmp_path: Path) -> None:
    attempt = _attempt(tmp_path)
    (attempt / "code").mkdir()
    (attempt / "code" / "planted.py").write_text("x\n")
    with pytest.raises(CodeUnpackFailed, match="already exists"):
        _unpack(_tarball(_file("a.py")), attempt)
    assert (attempt / "code" / "planted.py").exists()


def test_unpack_refuses_an_attempt_directory_that_is_not_private(tmp_path: Path) -> None:
    attempt = _attempt(tmp_path)
    attempt.chmod(0o750)
    with pytest.raises(CodeUnpackFailed, match="not private"):
        _unpack(_tarball(_file("a.py")), attempt)
    assert list(attempt.iterdir()) == []


@pytest.mark.parametrize(
    ("member", "fragment"),
    [
        (_file("/etc/passwd"), "unsafe path"),
        (_file("../outside.py"), "unsafe path"),
        (_file("a/../../outside.py"), "unsafe path"),
        (_file("zero\u200bwidth\u200c.py"), "unsafe path"),
        (_other("link", tarfile.SYMTYPE, "target"), "links and special files are not allowed"),
        (_other("link", tarfile.SYMTYPE, "/etc/passwd"), "links and special files are not allowed"),
        (_other("hard", tarfile.LNKTYPE, "a.py"), "links and special files are not allowed"),
        (_other("fifo", tarfile.FIFOTYPE), "links and special files are not allowed"),
        (_other("dev", tarfile.CHRTYPE), "links and special files are not allowed"),
        (_other("blk", tarfile.BLKTYPE), "links and special files are not allowed"),
    ],
)
def test_unpack_refuses_unsafe_members(
    tmp_path: Path, member: tuple[tarfile.TarInfo, bytes | None], fragment: str
) -> None:
    attempt = _attempt(tmp_path)
    tarball = _tarball(_file("a.py"), member)
    with pytest.raises(CodeUnpackError, match=fragment):
        _unpack(tarball, attempt)
    assert list(attempt.iterdir()) == []


def test_unpack_refuses_a_duplicate_member(tmp_path: Path) -> None:
    """A second member must not replace the first, byte for byte or by case folding."""
    cases = [
        [_file("a.py"), _file("a.py")],
        [_file("a.py"), _file("A.PY")],
        [_file("d/x.py"), _file("d/./x.py")],
        [_other("d", tarfile.DIRTYPE), _file("d")],
    ]
    for members in cases:
        attempt = _attempt(tmp_path, f"a{len(list(tmp_path.iterdir()))}")
        with pytest.raises(CodeUnpackError):
            _unpack(_tarball(*members), attempt)
        assert list(attempt.iterdir()) == []


def test_unpack_accepts_a_directory_listed_twice(tmp_path: Path) -> None:
    tree = _unpack(
        _tarball(
            _other("d", tarfile.DIRTYPE),
            _file("d/a.py"),
            _other("d", tarfile.DIRTYPE),
        ),
        _attempt(tmp_path),
    )
    assert (tree / "d" / "a.py").is_file()


def test_repeated_directory_entries_count_toward_the_member_limit(tmp_path: Path) -> None:
    tarball = _tarball(*[_other("d", tarfile.DIRTYPE) for _ in range(10)])
    with pytest.raises(CodeUnpackError, match="more than 5 files"):
        _unpack(tarball, _attempt(tmp_path), max_members=5)


def test_unpack_refuses_a_truncated_or_corrupt_archive(tmp_path: Path) -> None:
    good = _tarball(_file("a.py", b"y" * 5000), _file("b.py", b"z" * 5000))
    for index, broken in enumerate((good[: len(good) // 2], good[:20] + bytes(5) + good[25:])):
        attempt = _attempt(tmp_path, f"a{index}")
        with pytest.raises(CodeUnpackError, match="could not unpack the code"):
            _unpack(broken, attempt)
        assert list(attempt.iterdir()) == []


def test_unpack_stops_inflating_at_the_size_limit(tmp_path: Path) -> None:
    """A bomb is refused from its member headers, not after inflating all of it."""
    bomb = _tarball(_file("a.bin", b"\0" * 50_000_000))
    started = time.monotonic()
    with pytest.raises(CodeUnpackError, match="larger than 1000 bytes"):
        _unpack(bomb, _attempt(tmp_path), max_bytes=1000)
    assert time.monotonic() - started < 5


def test_unpack_enforces_size_count_and_digest(tmp_path: Path) -> None:
    attempt = _attempt(tmp_path)
    big = _tarball(_file("a.bin", b"x" * 2000))
    with pytest.raises(CodeUnpackError, match="larger than 1000 bytes"):
        _unpack(big, attempt, max_bytes=1000)
    many = _tarball(*[_file(f"f{i}.py") for i in range(5)])
    with pytest.raises(CodeUnpackError, match="more than 3 files"):
        _unpack(many, attempt, max_members=3)
    good = _tarball(_file("a.py"))
    target = attempt / "code"
    with pytest.raises(CodeUnpackError, match="does not match its recorded code_sha256"):
        unpack_code(good, "0" * 64, target, max_bytes=1000, max_members=10)
    with pytest.raises(CodeUnpackError, match="not a valid digest"):
        unpack_code(good, "../escape", target, max_bytes=1000, max_members=10)
    garbage = b"not a tarball"
    with pytest.raises(CodeUnpackError, match="could not unpack the code"):
        unpack_code(
            garbage, hashlib.sha256(garbage).hexdigest(), target, max_bytes=1000, max_members=10
        )
    assert list(attempt.iterdir()) == []


@pytest.mark.parametrize("code", [errno.ENOSPC, errno.EIO, errno.EMFILE, errno.EDQUOT])
def test_an_environment_failure_while_extracting_is_not_an_unsafe_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    attempt = _attempt(tmp_path)

    def extractall(*_args: object, **_kwargs: object) -> None:
        raise OSError(code, os.strerror(code), str(attempt / "code" / "a.py"))

    monkeypatch.setattr(runners_module.tarfile.TarFile, "extractall", extractall)
    with pytest.raises(CodeUnpackFailed) as caught:
        _unpack(_tarball(_file("a.py")), attempt)
    assert str(tmp_path) not in str(caught.value)
    assert list(attempt.iterdir()) == []


def test_an_archive_that_collides_on_the_filesystem_is_an_unsafe_archive(tmp_path: Path) -> None:
    """A file where a directory is needed is the archive's fault, not the disk's."""
    attempt = _attempt(tmp_path)
    tarball = _tarball(_file("a"), _file("a/b.py"))
    with pytest.raises(CodeUnpackError, match="could not unpack the code") as caught:
        _unpack(tarball, attempt)
    assert str(tmp_path) not in str(caught.value)


def test_unpack_errors_are_bounded_and_never_show_paths(tmp_path: Path) -> None:
    attempt = _attempt(tmp_path)
    name = "x" * 2000 + "\x1b[31m/../y"
    with pytest.raises(CodeUnpackError) as caught:
        _unpack(_tarball(_file(name)), attempt)
    assert len(str(caught.value)) <= 500
    assert "\x1b" not in str(caught.value)
    with pytest.raises(CodeUnpackError) as refused:
        _unpack(
            _tarball(_file("a.py"), _file("big", b"x" * 100)), _attempt(tmp_path, "b"), max_bytes=10
        )
    assert str(tmp_path) not in str(refused.value)


def test_a_name_too_long_for_the_filesystem_is_an_unsafe_archive(tmp_path: Path) -> None:
    attempt = _attempt(tmp_path)
    with pytest.raises(CodeUnpackError) as caught:
        _unpack(_tarball(_file("a" + "/b" * 3000 + ".py")), attempt)
    assert str(tmp_path) not in str(caught.value)
    assert len(str(caught.value)) <= 500


def test_unpack_reports_a_target_that_cannot_be_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempt = _attempt(tmp_path)
    real_mkdir = Path.mkdir

    def mkdir(self: Path, *args: object, **kwargs: object) -> None:
        if self.name == "code":
            raise OSError(errno.EROFS, "read-only file system", str(self))
        real_mkdir(self, *args, **kwargs)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(Path, "mkdir", mkdir)
    with pytest.raises(CodeUnpackFailed) as caught:
        _unpack(_tarball(_file("a.py")), attempt)
    assert str(tmp_path) not in str(caught.value)


def test_unpack_reports_a_chmod_that_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempt = _attempt(tmp_path)

    def chmod(*_args: object, **_kwargs: object) -> None:
        raise OSError(errno.EIO, "input/output error")

    monkeypatch.setattr(runners_module.os, "chmod", chmod)
    with pytest.raises(CodeUnpackFailed):
        _unpack(_tarball(_file("a.py")), attempt)


def test_child_environment_without_a_limit_is_five_variables_and_the_spill_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LAKANA_DATABASE__URL", "postgresql://secret")
    monkeypatch.setenv("PATH", "/custom/bin")
    env = child_environment(tmp_path / "home", tmp_path / "tmp")
    assert env == {
        "PATH": "/custom/bin",
        "HOME": str(tmp_path / "home"),
        "TMPDIR": str(tmp_path / "tmp"),
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "POLARS_OOC_SPILL_DIR": str(tmp_path / "tmp" / "polars-spill"),
    }
    monkeypatch.delenv("PATH")
    assert child_environment(tmp_path, tmp_path)["PATH"] == "/usr/bin:/bin"


def test_child_environment_bounds_polars_by_the_workers_memory_limit(tmp_path: Path) -> None:
    """Polars 2 spills from 80 % of the *machine's* memory; the worker's limit is lower."""
    env = child_environment(tmp_path / "home", tmp_path / "tmp", memory_limit=4 * 1024**3)
    assert env["POLARS_OVERRIDE_TOTAL_MEMORY_MB"] == "4294"  # Polars counts a MB as 10**6 bytes
    assert env["POLARS_OOC_DISK_BUDGET_MB"] == "4294"
    assert env["POLARS_OOC_SPILL_DIR"] == str(tmp_path / "tmp" / "polars-spill")
    assert {key for key in env if key.startswith("POLARS_")} == {
        "POLARS_OVERRIDE_TOTAL_MEMORY_MB",
        "POLARS_OOC_DISK_BUDGET_MB",
        "POLARS_OOC_SPILL_DIR",
    }
    assert (
        child_environment(tmp_path, tmp_path, memory_limit=1)["POLARS_OVERRIDE_TOTAL_MEMORY_MB"]
        == "1"
    )
    # without a limit only the directory is set: Polars' own default is a path every task shares
    off = child_environment(tmp_path / "home", tmp_path / "tmp", memory_limit=None)
    assert {key: value for key, value in off.items() if key.startswith("POLARS_")} == {
        "POLARS_OOC_SPILL_DIR": str(tmp_path / "tmp" / "polars-spill")
    }


def test_token_secrets() -> None:
    assert token_secrets(_TOKEN) == [_TOKEN, "s3cr3t_part-x"]
    assert token_secrets("opaque") == ["opaque"]


_HARNESS_HEAD = """\
import json, os, signal, subprocess, sys, time
description = json.loads(sys.stdin.read())
result = description["result_path"]

def write(record):
    with open(result, "w") as handle:
        json.dump(record, handle)
"""


class Harness:
    """A scripted stand-in for `lakana.runner`, run by the real `LocalProcessRunner`.

    Parameters
    ----------
    root
        A per-test directory.
    """

    def __init__(self, root: Path) -> None:
        self.code = root / "code"
        self.code.mkdir()
        self.attempt = _attempt(root)
        self.venv = root / "venv"
        (self.venv / "bin").mkdir(parents=True)
        os.symlink(sys.executable, self.venv / "bin" / "python")
        # `-P` keeps the working directory off sys.path, so the scripted module lives in the
        # fake venv's site-packages, as the real harness does.
        home = os.path.dirname(os.path.realpath(sys.executable))
        (self.venv / "pyvenv.cfg").write_text(
            f"home = {home}\ninclude-system-site-packages = false\n"
        )
        version = f"python{sys.version_info.major}.{sys.version_info.minor}"
        self.site = self.venv / "lib" / version / "site-packages"
        self.site.mkdir(parents=True)
        self.log: list[bytes] = []
        self.sink: Callable[[bytes], None] = self.log.append
        self.inputs = {"obs": "silver.weather"}
        self.stop = StopSignal()
        self.fields: dict[str, object] = {}
        self.memory_limit: int | None = None
        self.trust: Path | None = None
        self.registry = TaskRegistry()

    def task(self, timeout: float = 30.0, **fields: object) -> PythonTask:
        """Build the task.

        Parameters
        ----------
        timeout
            The asset timeout in seconds.
        **fields
            Fields of `PythonTask` to replace (``output``, ``input_kinds``, ...).

        Returns
        -------
        PythonTask
            The task.
        """
        return PythonTask(
            **{
                "code_dir": self.code,
                "asset_path": "assets/gold/scores.py",
                "inputs": self.inputs,
                "table": "gold.scores",
                "environment": "dev",
                "server_url": "http://server:8000",
                "token": _TOKEN,
                "timeout_seconds": timeout,
                "venv": self.venv,
                "expected_snapshot_id": "none",
                "attempt_dir": self.attempt,
                **fields,
            }  # ty: ignore[invalid-argument-type]
        )

    def run(
        self,
        body: str,
        *,
        timeout: float = 30.0,
        kill_grace: float = 0.3,
        head: str = _HARNESS_HEAD,
    ) -> RunOutcome:
        """Run a scripted harness to completion.

        Parameters
        ----------
        body
            Python appended to the script's head.
        timeout
            The asset timeout.
        kill_grace
            Seconds between SIGTERM and SIGKILL.
        head
            The script's first lines; the default reads the description from stdin.

        Returns
        -------
        RunOutcome
            The outcome.
        """
        self._timeout = timeout
        (self.site / "fake_harness.py").write_text(head + body)
        runner = LocalProcessRunner(
            kill_grace=kill_grace,
            module="fake_harness",
            poll_seconds=0.02,
            memory_limit=self.memory_limit,
            trust=self.trust,
            registry=self.registry,
        )
        return self._bounded(runner)

    def _bounded(self, runner: LocalProcessRunner) -> RunOutcome:
        """Run in a thread and fail the test, never hang it, if the runner does not return.

        Parameters
        ----------
        runner
            The runner under test.

        Returns
        -------
        RunOutcome
            The outcome.
        """
        box: list[RunOutcome] = []
        thread = threading.Thread(
            target=lambda: box.append(
                runner.run(self.task(self._timeout, **self.fields), self.sink, self.stop)
            ),
            daemon=True,
        )
        thread.start()
        thread.join(_RUN_BOUND_SECONDS)
        if thread.is_alive():
            self.stop.set(StopReason.CANCELLED)
            thread.join(10)
            pytest.fail("LocalProcessRunner.run did not return in time")
        return box[0]

    def output(self) -> str:
        """Return the captured output.

        Returns
        -------
        str
            Everything the sink received.
        """
        return b"".join(self.log).decode()


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    """Build the scripted harness.

    Parameters
    ----------
    tmp_path
        Pytest's per-test directory.

    Returns
    -------
    Harness
        The harness.
    """
    return Harness(tmp_path)


def _alive(pid: int) -> bool:
    """Say whether a process exists.

    Parameters
    ----------
    pid
        The process id.

    Returns
    -------
    bool
        Whether it can be signalled.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_a_successful_process(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LAKANA_SECRETS__MASTER_KEY", "must-not-leak")
    outcome = harness.run(
        "print(json.dumps({'argv': sys.argv[1:], 'env': sorted(os.environ), 'cwd': os.getcwd(),"
        " 'home': os.path.isdir(os.environ['HOME']),"
        " 'tmp': os.path.isdir(os.environ['TMPDIR'])}))\n"
        "print('to stderr', file=sys.stderr)\n"
        "assert sys.stdin.read() == ''\n"
        "open(result + '.description', 'w').write(json.dumps(description))\n"
        "write({'status': 'succeeded', 'snapshot_id': 7, 'rows': 3})\n"
    )
    assert outcome == RunOutcome(succeeded=True, snapshot_id=7, rows=3)
    seen = json.loads(harness.output().splitlines()[0])
    assert seen["argv"] == []
    assert set(seen["env"]) - {"LC_CTYPE", "__CF_USER_TEXT_ENCODING"} == {
        "HOME",
        "PATH",
        "POLARS_OOC_SPILL_DIR",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONUNBUFFERED",
        "TMPDIR",
    }
    assert seen["cwd"] == str(harness.code)
    assert seen["home"] and seen["tmp"]
    assert "to stderr" in harness.output()
    description = json.loads((harness.attempt / (RESULT_FILE + ".description")).read_text())
    assert description == {
        "version": 2,
        "server_url": "http://server:8000",
        "token": _TOKEN,
        "environment": "dev",
        "asset_path": "assets/gold/scores.py",
        "table": "gold.scores",
        "inputs": {"obs": "silver.weather"},
        "expected_snapshot_id": "none",
        "result_path": str(harness.attempt / RESULT_FILE),
        "request_timeout_seconds": 30.0,
        "parent_pid": os.getpid(),
        "output": "table",
        "input_kinds": {"obs": "table"},
        "input_versions": {},
        "decimals": "float",
        "work_dir": str(harness.attempt),
    }
    assert _TOKEN not in repr(harness.task())


def test_the_description_carries_the_artifact_fields(harness: Harness) -> None:
    harness.inputs = {"m": "models.churn", "obs": "silver.weather"}
    harness.fields = {
        "output": "artifact",
        "input_kinds": {"m": "artifact"},
        "input_versions": {"m": 4},
    }
    outcome = harness.run(
        "open(result + '.description', 'w').write(json.dumps(description))\n"
        "write({'status': 'succeeded', 'output': 'artifact', 'version': 4,"
        " 'manifest_sha256': 'a' * 64})\n"
    )
    assert outcome == RunOutcome(succeeded=True)
    description = json.loads((harness.attempt / (RESULT_FILE + ".description")).read_text())
    assert description["version"] == 2
    assert description["output"] == "artifact"
    assert description["input_kinds"] == {"m": "artifact", "obs": "table"}
    assert description["input_versions"] == {"m": 4}
    assert description["work_dir"] == str(harness.attempt)
    assert not {"inputs_dir", "output_dir"} & set(description)


def test_a_runner_with_a_memory_limit_gives_the_task_polars_limits_and_a_spill_directory(
    harness: Harness,
) -> None:
    harness.memory_limit = 2 * 1024**3
    outcome = harness.run(
        "keys = ('POLARS_OVERRIDE_TOTAL_MEMORY_MB', 'POLARS_OOC_DISK_BUDGET_MB',"
        " 'POLARS_OOC_SPILL_DIR')\n"
        "seen = {k: os.environ[k] for k in keys}\n"
        "seen['spill_is_dir'] = os.path.isdir(os.environ['POLARS_OOC_SPILL_DIR'])\n"
        "seen['mode'] = oct(os.stat(os.environ['POLARS_OOC_SPILL_DIR']).st_mode & 0o777)\n"
        "print(json.dumps(seen))\n"
        "write({'status': 'succeeded', 'snapshot_id': None, 'rows': 0})\n"
    )
    assert outcome.succeeded
    seen = json.loads(harness.output().splitlines()[0])
    assert seen == {
        "POLARS_OVERRIDE_TOTAL_MEMORY_MB": "2147",
        "POLARS_OOC_DISK_BUDGET_MB": "2147",
        "POLARS_OOC_SPILL_DIR": str(harness.attempt / "tmp" / "polars-spill"),
        "spill_is_dir": True,
        "mode": "0o700",
    }


def test_a_runner_without_a_memory_limit_still_gives_the_task_its_own_spill_directory(
    harness: Harness,
) -> None:
    """With the limit off the sizes are left out; the spill directory is still set."""
    outcome = harness.run(
        "seen = {k: v for k, v in os.environ.items() if k.startswith('POLARS_')}\n"
        "seen['spill_is_dir'] = os.path.isdir(os.environ['POLARS_OOC_SPILL_DIR'])\n"
        "seen['mode'] = oct(os.stat(os.environ['POLARS_OOC_SPILL_DIR']).st_mode & 0o777)\n"
        "print(json.dumps(seen))\n"
        "write({'status': 'succeeded', 'snapshot_id': None, 'rows': 0})\n"
    )
    assert outcome.succeeded
    assert json.loads(harness.output().splitlines()[0]) == {
        "POLARS_OOC_SPILL_DIR": str(harness.attempt / "tmp" / "polars-spill"),
        "spill_is_dir": True,
        "mode": "0o700",
    }


def test_the_description_carries_the_decimals_option(harness: Harness) -> None:
    harness.fields = {"decimals": "exact"}
    outcome = harness.run(
        "open(result + '.description', 'w').write(json.dumps(description))\n"
        "write({'status': 'succeeded', 'snapshot_id': None, 'rows': 0})\n"
    )
    assert outcome.succeeded
    description = json.loads((harness.attempt / (RESULT_FILE + ".description")).read_text())
    assert description["version"] == 2
    assert description["decimals"] == "exact"


_RAW_HEAD = _HARNESS_HEAD.replace(
    "description = json.loads(sys.stdin.read())",
    "raw = sys.stdin.read()\ndescription = json.loads(raw)\n"
    "open(description['result_path'] + '.raw', 'w').write(raw)",
)


def _real_parse(harness: Harness) -> TaskDescription:
    """Run the stand-in harness and parse the document it received with the SDK's parser.

    Parameters
    ----------
    harness
        The harness.

    Returns
    -------
    TaskDescription
        What `lakana.runner.parse_description` makes of the exact stdin document
        `LocalProcessRunner` wrote.
    """
    outcome = harness.run(
        "write({'status': 'failed', 'error_type': 'E', 'message': 'x', 'retryable': False})\n"
        "sys.exit(1)\n",
        head=_RAW_HEAD,
    )
    assert outcome.error_message == "x"
    return parse_description((harness.attempt / (RESULT_FILE + ".raw")).read_text())


def test_the_sdk_parses_the_description_of_a_table_task(harness: Harness) -> None:
    """I4: the two packages agree on the document, not just on their own halves of it."""
    parsed = _real_parse(harness)
    assert parsed.environment == "dev"
    assert parsed.table == "gold.scores"
    assert parsed.output == "table"
    assert parsed.inputs == {"obs": "silver.weather"}
    assert parsed.input_kinds == {"obs": "table"}
    assert parsed.input_versions == {}
    assert parsed.decimals == "float"
    assert parsed.work_dir == str(harness.attempt)
    assert parsed.result_path == str(harness.attempt / RESULT_FILE)
    assert parsed.expected_snapshot_id == "none"
    assert parsed.token == _TOKEN


def test_the_sdk_parses_the_decimals_of_a_task(harness: Harness) -> None:
    harness.fields = {"decimals": "exact"}
    assert _real_parse(harness).decimals == "exact"


def test_the_sdk_parses_the_description_of_an_artifact_task(harness: Harness) -> None:
    """I4: one artifact and one table input, with the version of the artifact."""
    harness.inputs = {"m": "models.churn", "obs": "silver.weather"}
    harness.fields = {
        "output": "artifact",
        "input_kinds": {"m": "artifact"},
        "input_versions": {"m": 4},
    }
    parsed = _real_parse(harness)
    assert parsed.output == "artifact"
    assert parsed.inputs == {"m": "models.churn", "obs": "silver.weather"}
    assert parsed.input_kinds == {"m": "artifact", "obs": "table"}
    assert parsed.input_versions == {"m": 4}
    assert parsed.work_dir == str(harness.attempt)


def _description(harness: Harness, **fields: object) -> dict[str, object]:
    """Write the description of a task directly, without a process.

    Parameters
    ----------
    harness
        The harness, for its task.
    **fields
        Fields of `PythonTask` to replace.

    Returns
    -------
    dict
        The decoded document.
    """
    document = runners_module.task_description(
        dataclasses.replace(harness.task(), **fields),
        result_path="/r/result.json",
        work_dir="/r",
        parent_pid=None,
    )
    return json.loads(document)


def test_a_replace_without_connections_keeps_description_version_2(harness: Harness) -> None:
    """A harness of the previous release reads it: no key of version 3 is sent."""
    description = _description(harness, hidden=("s3cr3t",))
    assert description["version"] == 2
    assert not {"write", "key", "watermark", "connections", "hidden", "evolve_schema"} & set(
        description
    )


def test_an_evolving_replace_says_so_and_keeps_description_version_2(harness: Harness) -> None:
    """A harness of the previous release ignores the key and refuses the changed schema."""
    description = _description(harness, evolve_schema=True)
    assert description["version"] == 2
    assert description["evolve_schema"] is True


def test_the_sdk_parses_the_evolve_schema_of_a_task(harness: Harness) -> None:
    assert _real_parse(harness).evolve_schema is False
    harness.fields = {"evolve_schema": True}
    assert _real_parse(harness).evolve_schema is True


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        (
            {
                "write": "append",
                "watermark_column": "ts",
                "watermark": "long:41",
                "table_exists": True,
            },
            {
                "write": "append",
                "key": [],
                "watermark": {"column": "ts", "value": "long:41"},
                "connections": {},
                "table_exists": True,
            },
        ),
        (
            {"write": "append", "watermark_column": "ts"},
            {
                "write": "append",
                "key": [],
                "watermark": {"column": "ts", "value": None},
                "connections": {},
                "table_exists": False,
            },
        ),
        (
            {"write": "merge", "key": ("id", "day")},
            {"write": "merge", "key": ["id", "day"], "watermark": None, "connections": {}},
        ),
        (
            {"connections": {"crm-db": {"kind": "postgresql"}}},
            {
                "write": "replace",
                "key": [],
                "watermark": None,
                "connections": {"crm-db": {"kind": "postgresql"}},
            },
        ),
    ],
    ids=["append", "append-first-run", "merge", "replace-with-connections"],
)
def test_an_asset_that_uses_its_fields_gets_description_version_3(
    harness: Harness, fields: dict[str, object], expected: dict[str, object]
) -> None:
    description = _description(harness, **fields)
    assert description["version"] == 3
    assert {key: description[key] for key in expected} == expected
    assert "hidden" not in description


def test_the_sdk_parses_a_version_3_description(harness: Harness) -> None:
    """I4 for version 3: the harness reads what the runner writes."""
    harness.fields = {
        "write": "merge",
        "key": ("id",),
        "watermark_column": "at",
        "watermark": "timestamptz:2026-10-22T08:00:00+00:00",
        "connections": {"crm-db": {"kind": "postgresql"}},
        "expected_snapshot_id": 17,
    }
    parsed = _real_parse(harness)
    assert (parsed.write, parsed.key, parsed.watermark_column) == ("merge", ("id",), "at")
    assert parsed.watermark is not None
    assert parsed.connections == {"crm-db": {"kind": "postgresql"}}
    assert parsed.expected_snapshot_id == 17


def test_the_secret_values_to_hide_stay_out_of_the_repr(harness: Harness) -> None:
    assert "s3cr3t" not in repr(harness.task(hidden=("s3cr3t",)))


def _failed(**fields: object) -> str:
    """Build the harness body that writes a failed record and exits 1.

    Parameters
    ----------
    **fields
        Overrides of the default record fields.

    Returns
    -------
    str
        Python source for the scripted harness.
    """
    record: dict[str, object] = {
        "status": "failed",
        "error_type": "E",
        "message": "boom",
        "retryable": True,
        "phase": "upload",
        "conflict": False,
    }
    record.update(fields)
    return f"write({record!r})\nsys.exit(1)\n"


def test_a_failed_record_is_passed_on(harness: Harness) -> None:
    outcome = harness.run(
        _failed(error_type="Conflict", message="changed " + "x" * 5000, conflict=True)
    )
    assert outcome.succeeded is False
    assert outcome.conflict is True
    assert outcome.error_type == "Conflict"
    assert outcome.retryable is True
    assert outcome.killed is False
    assert outcome.error_message is not None
    assert outcome.error_message.startswith("changed xxx")
    assert len(outcome.error_message) == MAX_ERROR_MESSAGE_CHARS
    not_retryable = harness.run(_failed(error_type="InvalidResult", message="bad", retryable=False))
    assert (not_retryable.error_type, not_retryable.retryable) == ("InvalidResult", False)
    assert not_retryable.conflict is False


def test_conflict_comes_only_from_the_conflict_flag(harness: Harness) -> None:
    named = harness.run(_failed(error_type="Conflict", conflict=False))
    assert named.conflict is False
    text = harness.run(_failed(error_type="E", conflict="yes"))
    assert text.conflict is False
    missing = harness.run(
        "write({'status': 'failed', 'message': 'old shape', 'retryable': True})\nsys.exit(1)\n"
    )
    assert (missing.conflict, missing.error_message, missing.retryable) == (
        False,
        "old shape",
        True,
    )


def test_record_text_is_sanitised_before_it_is_stored(harness: Harness) -> None:
    outcome = harness.run(
        _failed(
            error_type="Ty\x00pe\u202e" + "T" * 500,
            message="a\x00b\x1b[31mc\u2028d\ud800e\tf\ng " + "lakana_task_k1abc_s3cr3t_part-x",
        )
    )
    assert outcome.error_message == "a?b?[31mc?d?e\tf\ng ***"
    assert outcome.error_type is not None
    assert outcome.error_type.startswith("Ty?pe?TTT")
    assert len(outcome.error_type) == 100


def test_cleaning_never_joins_a_split_secret(harness: Harness) -> None:
    """Control characters are replaced, not deleted: deleting could assemble a secret."""
    outcome = harness.run(_failed(message="s3cr3t_part\x00-x and s3cr3t_part-x"))
    assert outcome.error_message == "s3cr3t_part?-x and ***"


def test_a_failed_record_without_a_message_still_says_something(harness: Harness) -> None:
    outcome = harness.run(_failed(message="\x00"))
    assert outcome.error_message == "?"
    empty = harness.run(_failed(message=""))
    assert empty.error_message == "the task failed without a message"
    assert empty.error_type == "E"
    no_type = harness.run(_failed(error_type=None))
    assert no_type.error_type is None


def test_the_token_is_redacted_from_the_log_and_the_message(harness: Harness) -> None:
    outcome = harness.run(
        "print('token', description['token'])\n"
        "print('secret part s3cr3t_part-x')\n"
        "write({'status': 'failed', 'error_type': 'E', 'message': 'saw ' + description['token'],"
        " 'retryable': True})\nsys.exit(1)\n"
    )
    assert "s3cr3t" not in harness.output()
    assert harness.output().startswith("token ***\nsecret part ***\n")
    assert outcome.error_message == "saw ***"


def test_a_token_straddling_the_forced_flush_boundary_is_hidden(harness: Harness) -> None:
    """User code controls line length: a token split across the redactor's forced flush."""
    outcome = harness.run(
        "token = description['token']\n"
        "for cut in (3, 12, len(token) - 1):\n"
        "    sys.stdout.write('x' * (65537 - cut))\n"
        "    sys.stdout.flush()\n"
        "    time.sleep(0.15)\n"
        "    sys.stdout.write(token[:cut])\n"
        "    sys.stdout.flush()\n"
        "    time.sleep(0.15)\n"
        "    sys.stdout.write(token[cut:] + '\\n')\n"
        "    sys.stdout.flush()\n"
        "write({'status': 'succeeded', 'snapshot_id': 1, 'rows': 1})\n"
    )
    assert outcome.succeeded
    assert "s3cr3t" not in harness.output()
    assert "lakana_task" not in harness.output()
    assert harness.output().count("***") == 3


def test_a_declared_connections_secret_is_hidden_even_split_across_two_writes(
    harness: Harness,
) -> None:
    """`PythonTask.hidden` joins the token in the redaction of the log and the message."""
    harness.fields["hidden"] = ("conn-s3cr3t-value",)
    outcome = harness.run(
        "sys.stdout.write('password conn-s3cr')\n"
        "sys.stdout.flush()\n"
        "time.sleep(0.15)\n"
        "sys.stdout.write('3t-value\\n')\n"
        "sys.stdout.flush()\n"
        "write({'status': 'failed', 'error_type': 'E', 'message': 'login conn-s3cr3t-value',"
        " 'retryable': True})\nsys.exit(1)\n"
    )
    assert "s3cr3t" not in harness.output()
    assert harness.output().startswith("password ***\n")
    assert outcome.error_message == "login ***"


_TRICKY_PASSWORD = "Tr0ub\\4dor\"&'x\x01é"


def test_every_form_of_a_declared_connections_secret_is_hidden(harness: Harness) -> None:
    """repr, JSON (both), URL quoting, bytes, a dict, a log record: none reaches the log."""
    harness.fields["hidden"] = tuple(secret_forms(_TRICKY_PASSWORD))
    outcome = harness.run(
        "import json, logging\n"
        "from urllib.parse import quote, quote_plus\n"
        f"pw = {_TRICKY_PASSWORD!r}\n"
        "print('raw', pw)\n"
        "print('repr', repr(pw))\n"
        "print('dict', {'password': pw})\n"
        "print('json', json.dumps({'password': pw}))\n"
        "print('json8', json.dumps({'password': pw}, ensure_ascii=False))\n"
        "print('quote', quote(pw), quote(pw, safe=''), quote_plus(pw))\n"
        "print('bytes', pw.encode())\n"
        "sys.stdout.flush()\n"
        "logging.warning('%r', pw)\n"
        "write({'status': 'succeeded', 'snapshot_id': 1, 'rows': 1})\n"
    )
    assert outcome.succeeded
    out = harness.output()
    for form in secret_forms(_TRICKY_PASSWORD) | {"Tr0ub", "4dor"}:
        assert form not in out, form
    assert out.count("***") == 10


@pytest.mark.parametrize("password", ["Zürich-Pass-2026", 'Zq-quote"Pass'])
def test_a_json_body_printed_again_hides_the_secret(harness: Harness, password: str) -> None:
    """A second layer: a JSON body as bytes, in a dict's repr, or JSON-encoded again."""
    harness.fields["hidden"] = tuple(secret_forms(password))
    outcome = harness.run(
        "import json\n"
        f"pw = {password!r}\n"
        "for ascii_only in (True, False):\n"
        "    body = json.dumps({'password': pw}, ensure_ascii=ascii_only)\n"
        "    print(body.encode())\n"
        "    print({'body': body})\n"
        "    print(json.dumps(body))\n"
        "    print(json.dumps(body, ensure_ascii=False))\n"
        "print(json.dumps(json.dumps(pw)))\n"
        "write({'status': 'succeeded', 'snapshot_id': 1, 'rows': 1})\n"
    )
    assert outcome.succeeded
    out = harness.output()
    assert "Pass" not in out
    assert out.count("***") == 9


def test_the_token_is_only_on_stdin(harness: Harness) -> None:
    harness.run(
        "print(json.dumps({'argv': sys.orig_argv, 'env': dict(os.environ)}))\n"
        "write({'status': 'succeeded', 'snapshot_id': 1, 'rows': 1})\n"
    )
    seen = json.loads(harness.output().splitlines()[0])
    assert "s3cr3t" not in json.dumps(seen)
    assert "lakana_task" not in json.dumps(seen)
    assert seen["argv"][1:] == ["-P", "-m", "fake_harness"]


def test_the_harness_starts_in_safe_path_mode(harness: Harness) -> None:
    harness.run(
        "print(sys.flags.safe_path, '' in sys.path, os.getcwd() in sys.path)\n"
        "write({'status': 'succeeded', 'snapshot_id': 1, 'rows': 1})\n"
    )
    assert harness.output().startswith("True False False")


def test_a_stale_record_from_an_earlier_process_is_not_trusted(harness: Harness) -> None:
    (harness.attempt / RESULT_FILE).write_text(
        '{"status": "succeeded", "snapshot_id": 9, "rows": 9}'
    )
    outcome = harness.run("pass\n")
    assert outcome.succeeded is False
    assert "without a result" in (outcome.error_message or "")


def test_a_succeeded_artifact_record_with_a_bad_exit_status_of_an_unstopped_process_is_no_success(
    harness: Harness,
) -> None:
    """As for a table, a process nobody stopped that exits non-zero did not succeed."""
    harness.fields = {"output": "artifact"}
    outcome = harness.run(
        "write({'status': 'succeeded', 'output': 'artifact', 'version': 3,"
        " 'manifest_sha256': 'a' * 64})\nsys.exit(1)\n"
    )
    assert outcome == RunOutcome(
        succeeded=False,
        error_message="the task process exited with status 1 without a result",
        failure_code=FailureCode.BUILD_FAILED,
    )


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("sys.exit(3)\n", "the task process exited with status 3 without a result"),
        ("os.kill(os.getpid(), signal.SIGKILL)\n", "the task process was killed by signal 9"),
        (
            "write({'status': 'succeeded', 'snapshot_id': 1, 'rows': 1})\nsys.exit(4)\n",
            "the task process exited with status 4 without a result",
        ),
        ("open(result, 'w').write('not json')\n", "exited with status 0 without a result"),
        ("write(['a', 'list'])\n", "exited with status 0 without a result"),
        (
            "write({'status': 'succeeded', 'rows': 'many'})\n",
            "exited with status 0 without a result",
        ),
        ("write({'status': 'succeeded', 'snapshot_id': True, 'rows': 1})\n", "without a result"),
        ("write({'status': 'failed', 'message': 7})\n", "exited with status 0 without a result"),
        ("write({'status': 'odd'})\n", "exited with status 0 without a result"),
        ("open(result, 'w').write('x' * 100000)\n", "exited with status 0 without a result"),
        (
            "forged = result + '.forged'\n"
            'open(forged, \'w\').write(\'{"status": "succeeded", "snapshot_id": 1, "rows": 1}\')\n'
            "os.symlink(forged, result)\n",
            "exited with status 0 without a result",
        ),
        ("os.mkdir(result)\n", "exited with status 0 without a result"),
        ("os.mkfifo(result)\n", "exited with status 0 without a result"),
        ("open(result, 'w').write('[' * 50000)\n", "exited with status 0 without a result"),
        ("open(result, 'wb').write(b'\\xff\\xfe')\n", "exited with status 0 without a result"),
        (
            "write({'status': 'succeeded', 'snapshot_id': 2**63, 'rows': 1})\n",
            "exited with status 0 without a result",
        ),
        (
            "write({'status': 'succeeded', 'snapshot_id': 1, 'rows': -1})\n",
            "exited with status 0 without a result",
        ),
        ("pass\n", "exited with status 0 without a result"),
        ("os._exit(0)\n", "exited with status 0 without a result"),
    ],
)
def test_a_process_without_a_usable_record_fails(harness: Harness, body: str, message: str) -> None:
    outcome = harness.run(body)
    assert outcome.succeeded is False
    assert outcome.error_message is not None and message in outcome.error_message
    assert outcome.killed is False
    assert outcome.retryable is True


def test_a_timeout_kills_the_whole_process_group(harness: Harness) -> None:
    started = time.monotonic()
    outcome = harness.run(
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "print(child.pid, flush=True)\n"
        "time.sleep(60)\n",
        timeout=0.4,
    )
    assert time.monotonic() - started < 15
    assert outcome == RunOutcome(
        succeeded=False,
        error_message="timed out after 0.4s",
        killed=True,
        timed_out=True,
        failure_code=FailureCode.TIMEOUT,
    )
    grandchild = int(harness.output().split()[0])
    deadline = time.monotonic() + 5
    while _alive(grandchild) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _alive(grandchild)


def test_a_succeeded_record_before_a_hang_is_still_a_success(harness: Harness) -> None:
    """The record is read before the timeout verdict; user code may keep the process alive."""
    outcome = harness.run(
        "write({'status': 'succeeded', 'snapshot_id': 3, 'rows': 2})\n"
        "print('written', flush=True)\ntime.sleep(60)\n",
        timeout=0.6,
    )
    assert outcome == RunOutcome(succeeded=True, snapshot_id=3, rows=2)


def test_a_failed_record_before_a_hang_loses_to_the_timeout(harness: Harness) -> None:
    outcome = harness.run(
        "write({'status': 'failed', 'message': 'late', 'retryable': True})\ntime.sleep(60)\n",
        timeout=0.4,
    )
    assert outcome.timed_out and outcome.killed


def test_closing_the_output_does_not_end_the_task_early(harness: Harness) -> None:
    started = time.monotonic()
    outcome = harness.run(
        "os.close(1)\nos.close(2)\ntime.sleep(1.0)\n"
        "write({'status': 'succeeded', 'snapshot_id': 4, 'rows': 1})\n"
    )
    assert outcome == RunOutcome(succeeded=True, snapshot_id=4, rows=1)
    assert time.monotonic() - started >= 1.0


def test_a_process_that_ignores_sigterm_is_killed_after_the_grace(harness: Harness) -> None:
    started = time.monotonic()
    outcome = harness.run(
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "print('ready', flush=True)\n"
        "time.sleep(60)\n",
        timeout=0.4,
        kill_grace=0.3,
    )
    assert outcome.timed_out and outcome.killed
    assert 0.6 < time.monotonic() - started < 15


def _stop_when_seen(harness: Harness, marker: bytes, reason: StopReason) -> None:
    """Make the log sink ask for a stop when the child prints `marker`.

    Parameters
    ----------
    harness
        The harness whose sink is replaced.
    marker
        The output that means the child is in the state the test wants.
    reason
        The stop reason to set.
    """

    def sink(data: bytes) -> None:
        harness.log.append(data)
        if marker in data:
            harness.stop.set(reason)

    harness.sink = sink


@pytest.mark.parametrize("reason", list(StopReason))
def test_the_stop_signal_terminates_the_process(harness: Harness, reason: StopReason) -> None:
    _stop_when_seen(harness, b"working", reason)
    outcome = harness.run("print('working', flush=True)\ntime.sleep(60)\n")
    assert outcome == RunOutcome(
        succeeded=False,
        error_message=f"stopped: {reason.value}",
        killed=True,
        failure_code={
            StopReason.CANCELLED: FailureCode.CANCELLED,
            StopReason.LEASE_LOST: FailureCode.WORKER_LOST,
            StopReason.DRAIN: FailureCode.WORKER_LOST,
            StopReason.MEMORY: FailureCode.MEMORY,
        }[reason],
    )
    harness.stop.set(StopReason.CANCELLED)
    assert harness.stop.reason is reason
    assert harness.stop.is_set()


def _end_for_memory_when_seen(harness: Harness, marker: bytes, detail: str) -> list[object]:
    """Have a thread of the test end the running task for memory once it prints `marker`.

    The thread plays the watchdog: it finds the task through the registry and asks it to end.

    Parameters
    ----------
    harness
        The harness whose sink is replaced.
    marker
        The output that means the task is running.
    detail
        The message the watchdog gives.

    Returns
    -------
    list
        Filled with the handle and what `kill_for_memory` answered, once it has been called.
    """
    seen = threading.Event()
    result: list[object] = []

    def sink(data: bytes) -> None:
        harness.log.append(data)
        if marker in data:
            seen.set()

    def watchdog() -> None:
        """Wait for the task to be running, then end it."""
        assert seen.wait(20)
        (handle,) = harness.registry.running()
        result.extend([handle, handle.pid, handle.kill_for_memory(detail)])

    harness.sink = sink
    threading.Thread(target=watchdog, daemon=True).start()
    return result


def test_the_memory_watchdog_ends_a_running_task_at_once_without_the_grace(
    harness: Harness,
) -> None:
    started = time.monotonic()
    result = _end_for_memory_when_seen(harness, b"working", "ended for memory")
    outcome = harness.run(
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "print('working', flush=True)\ntime.sleep(60)\n",
        kill_grace=30.0,
    )
    assert outcome == RunOutcome(
        succeeded=False,
        error_message="ended for memory",
        killed=True,
        failure_code=FailureCode.MEMORY,
    )
    assert time.monotonic() - started < 20  # no ask, no 30 s grace
    handle, pid, answered = result
    assert answered is True
    assert harness.stop.reason is StopReason.MEMORY
    assert isinstance(pid, int) and not _alive(pid)
    assert harness.registry.running() == []
    assert isinstance(handle, RunningTask) and handle.asset == "gold.scores"


def test_the_kill_and_the_reap_exclude_each_other(monkeypatch: pytest.MonkeyPatch) -> None:
    """While the lock is held (the reap), the kill neither signals nor returns."""
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True
    )
    handle = RunningTask(process, StopSignal(), "a.b", None)
    answer: list[bool] = []
    done = threading.Event()

    def kill() -> None:
        """Ask for the kill from another thread, as the watchdog does."""
        answer.append(handle.kill_for_memory("x"))
        done.set()

    thread = threading.Thread(target=kill, daemon=True)
    try:
        with handle._lock:
            thread.start()
            assert not done.wait(0.5)  # blocked on the lock
            assert process.poll() is None and _alive(process.pid)
        assert done.wait(20)
        thread.join(20)
        assert answer == [True] and handle.detail == "x"
    finally:
        # The kill has reaped nothing yet: on macOS a group of exited, unreaped processes
        # refuses signals with EPERM (#152), as `signal_group` already allows for.
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)
        handle.reap()


def test_a_record_written_before_the_kill_for_memory_still_counts_as_succeeded(
    harness: Harness,
) -> None:
    """The attempt wrote its `succeeded` record, then the watchdog ended the process: success."""

    def sink(data: bytes) -> None:
        """End the task for memory when it says its record is written, and wait for its death.

        The kill and the wait happen on the runner's own thread, so the runner's next look finds
        the leader already exited: only the memory detail can tell that it was stopped.

        Parameters
        ----------
        data
            The task's output.
        """
        harness.log.append(data)
        if b"recorded" in data:
            (handle,) = harness.registry.running()
            assert handle.kill_for_memory("ended for memory")
            os.waitid(os.P_PID, handle.pid, os.WEXITED | os.WNOWAIT)

    harness.sink = sink
    outcome = harness.run(
        "write({'status': 'succeeded', 'snapshot_id': 1, 'rows': 1})\n"
        "print('recorded', flush=True)\ntime.sleep(60)\n",
        kill_grace=30.0,
    )
    assert outcome == RunOutcome(succeeded=True, snapshot_id=1, rows=1)
    assert harness.stop.reason is StopReason.MEMORY


def test_a_task_that_has_ended_is_never_signalled_by_the_watchdog(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After the reap the pid may belong to someone else: the handle sends nothing."""
    handles: list[RunningTask] = []
    original = harness.registry.add
    monkeypatch.setattr(
        harness.registry, "add", lambda task: (handles.append(task), original(task))
    )
    outcome = harness.run("write({'status': 'succeeded', 'snapshot_id': 1, 'rows': 1})\n")
    assert outcome.succeeded
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "killpg", lambda pgid, signum: sent.append((pgid, signum)))
    (handle,) = handles
    assert handle.kill_for_memory("late") is False
    assert sent == [] and handle.detail is None


def test_a_task_whose_leader_exited_but_was_not_reaped_is_not_signalled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
    handle = RunningTask(process, StopSignal(), "a.b", None)
    os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOWAIT)
    sent: list[int] = []
    monkeypatch.setattr(os, "killpg", lambda pgid, signum: sent.append(pgid))
    assert handle.kill_for_memory("late") is False
    assert sent == [] and handle.detail is None
    assert handle.reap() == 0


def test_the_registry_lists_what_runs_and_forgets_what_was_removed_twice() -> None:
    registry = TaskRegistry()
    process = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
    handle = RunningTask(process, StopSignal(), "a.b", None)
    registry.add(handle)
    assert registry.running() == [handle]
    registry.remove(handle)
    registry.remove(handle)
    assert registry.running() == []
    handle.reap()


def test_a_timeout_that_came_first_is_still_the_reason_when_memory_ended_the_process() -> None:
    outcome = runners_module._outcome_from(
        None,
        code=-9,
        stopped=True,
        timed_out=True,
        timeout_seconds=10.0,
        stop=StopSignal(),
        secrets_to_hide=[],
        memory_detail="ended for memory",
    )
    assert outcome.timed_out is True and outcome.error_message == "timed out after 10s"


def test_the_memory_stop_reason_reads_as_the_limit() -> None:
    assert StopReason.MEMORY.value == "memory limit"


def test_a_result_that_landed_before_the_stop_counts_as_succeeded(harness: Harness) -> None:
    """As in M5c1: a write that completed is a success whatever the supervisor saw."""
    _stop_when_seen(harness, b"written", StopReason.CANCELLED)
    outcome = harness.run(
        "signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))\n"
        "write({'status': 'succeeded', 'snapshot_id': None, 'rows': 0})\n"
        "print('written', flush=True)\n"
        "time.sleep(60)\n"
    )
    assert outcome == RunOutcome(succeeded=True, snapshot_id=None, rows=0)


def test_a_leftover_grandchild_holding_the_pipe_does_not_hang_the_runner(harness: Harness) -> None:
    started = time.monotonic()
    outcome = harness.run(
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "print(child.pid, flush=True)\n"
        "write({'status': 'succeeded', 'snapshot_id': 5, 'rows': 1})\n"
    )
    assert outcome == RunOutcome(succeeded=True, snapshot_id=5, rows=1)
    assert time.monotonic() - started < 15
    grandchild = int(harness.output().split()[0])
    deadline = time.monotonic() + 5
    while _alive(grandchild) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _alive(grandchild)


def test_a_grandchild_that_left_the_group_cannot_hold_the_runner_forever(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runners_module, "_DRAIN_SECONDS", 0.3)
    started = time.monotonic()
    outcome = harness.run(
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)'],"
        " start_new_session=True)\n"
        "print(child.pid, flush=True)\n"
        "write({'status': 'succeeded', 'snapshot_id': 5, 'rows': 1})\n"
    )
    assert outcome.succeeded
    assert time.monotonic() - started < 10
    os.kill(int(harness.output().split()[0]), signal.SIGKILL)


def _run_in(harness: Harness, attempt: Path) -> RunOutcome:
    """Run the runner against a given attempt directory.

    Parameters
    ----------
    harness
        The harness.
    attempt
        The attempt directory to hand over.

    Returns
    -------
    RunOutcome
        The outcome.
    """
    broken = dataclasses.replace(harness.task(), attempt_dir=attempt)
    return LocalProcessRunner(module="fake_harness").run(broken, harness.log.append, harness.stop)


def test_a_venv_without_the_harness_module_fails_permanently_and_says_what_to_do(
    harness: Harness,
) -> None:
    outcome = _run_in(harness, harness.attempt)
    assert outcome.succeeded is False
    assert outcome.retryable is False
    assert outcome.error_message == (
        "the task's environment does not contain the lakana package, so nothing can run the "
        "asset. A package in a dependency group or an extra is not installed for a task: add "
        "`lakana[assets]` to `[project] dependencies` in pyproject.toml, run `uv lock`, commit "
        "and deploy again"
    )
    assert harness.log == []
    assert outcome.failure_code is FailureCode.BUILD_FAILED


def test_an_unusable_attempt_directory_is_a_failure_not_an_exception(
    harness: Harness, tmp_path: Path
) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a directory")
    outcome = _run_in(harness, blocker)
    assert outcome.succeeded is False
    assert (outcome.error_message or "").startswith("could not prepare the task directory")
    assert outcome.failure_code is FailureCode.INTERNAL


def test_a_missing_attempt_directory_is_not_created(harness: Harness, tmp_path: Path) -> None:
    missing = tmp_path / "deep" / "attempt"
    outcome = _run_in(harness, missing)
    assert (outcome.error_message or "").startswith("could not prepare the task directory")
    assert not missing.exists() and not missing.parent.exists()


@pytest.mark.parametrize("mode", [0o750, 0o705, 0o770, 0o707, 0o755])
def test_an_attempt_directory_others_can_reach_is_refused(
    harness: Harness, tmp_path: Path, mode: int
) -> None:
    shared = _attempt(tmp_path, "shared")
    shared.chmod(mode)
    outcome = _run_in(harness, shared)
    assert "not private" in (outcome.error_message or "")
    assert outcome.failure_code is FailureCode.INTERNAL
    assert list(shared.iterdir()) == []


def test_a_symlinked_attempt_directory_is_refused(harness: Harness, tmp_path: Path) -> None:
    link = tmp_path / "link"
    link.symlink_to(harness.attempt)
    outcome = _run_in(harness, link)
    assert "not private" in (outcome.error_message or "")


def test_a_missing_working_directory_is_a_failure_not_an_exception(harness: Harness) -> None:
    harness.code.rmdir()
    outcome = harness.run("")
    assert outcome.succeeded is False
    assert (outcome.error_message or "").startswith("could not start the task process")
    assert outcome.failure_code is FailureCode.INTERNAL


def test_the_attempt_directories_are_private(harness: Harness) -> None:
    harness.run("write({'status': 'succeeded', 'snapshot_id': 1, 'rows': 1})\n")
    for name in ("home", "tmp"):
        assert stat.S_IMODE((harness.attempt / name).stat().st_mode) == 0o700


def test_a_missing_interpreter_is_a_failure_not_an_exception(harness: Harness) -> None:
    (harness.venv / "bin" / "python").unlink()
    outcome = harness.run("")
    assert outcome.succeeded is False
    assert outcome.error_message is not None
    assert outcome.error_message.startswith("could not start the task process")
    assert outcome.killed is False


def test_an_unreadable_record_is_no_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    record = tmp_path / RESULT_FILE
    record.write_text('{"status": "succeeded", "snapshot_id": 1, "rows": 1}')
    assert runners_module._read_record(record, "table") == {
        "status": "succeeded",
        "snapshot_id": 1,
        "rows": 1,
    }

    def refuse(_descriptor: int, _size: int) -> bytes:
        raise OSError(5, "input/output error")

    monkeypatch.setattr(runners_module.os, "read", refuse)
    assert runners_module._read_record(record, "table") is None


def test_a_failing_pipe_read_ends_the_output_without_raising(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_read = os.read
    failed: list[int] = []

    def read(descriptor: int, size: int) -> bytes:
        if size == 65536 and not failed:  # the runner's pipe read, not Popen's own
            failed.append(descriptor)
            raise OSError(5, "input/output error")
        return real_read(descriptor, size)

    monkeypatch.setattr(runners_module.os, "read", read)
    outcome = harness.run(
        "print('hello', flush=True)\n"
        "time.sleep(0.3)\n"
        "write({'status': 'succeeded', 'snapshot_id': 1, 'rows': 1})\n"
    )
    assert outcome.succeeded
    assert failed


def test_a_large_description_reaches_the_child_whole(harness: Harness) -> None:
    harness.inputs = {f"input_{i}": f"silver.table_{i}" for i in range(12000)}
    outcome = harness.run(
        "print(len(description['inputs']), flush=True)\n"
        "write({'status': 'succeeded', 'snapshot_id': 1, 'rows': 1})\n"
    )
    assert outcome.succeeded, outcome
    assert harness.output().startswith("12000\n")


def test_a_large_description_does_not_deadlock_a_child_that_writes_first(
    harness: Harness,
) -> None:
    harness.inputs = {f"input_{i}": f"silver.table_{i}" for i in range(12000)}
    outcome = harness.run(
        "sys.stdout.write('y' * 300000)\n"
        "sys.stdout.flush()\n"
        "description = json.loads(sys.stdin.read())\n"
        "write({'status': 'succeeded', 'snapshot_id': 1, 'rows': len(description['inputs'])})\n",
        head="import json, sys\nresult = " + repr(str(harness.attempt / RESULT_FILE)) + "\n"
        "def write(record):\n    open(result, 'w').write(json.dumps(record))\n",
    )
    assert outcome == RunOutcome(succeeded=True, snapshot_id=1, rows=12000)


def test_a_child_that_never_reads_its_description_does_not_hang_the_runner(
    harness: Harness,
) -> None:
    harness.inputs = {f"input_{i}": f"silver.table_{i}" for i in range(12000)}
    outcome = harness.run("sys.exit(5)\n", head="import sys\n")
    assert outcome.succeeded is False
    assert "exited with status 5" in (outcome.error_message or "")


def test_a_raising_log_sink_stops_the_child_and_fails_the_attempt(harness: Harness) -> None:
    def broken(_data: bytes) -> None:
        raise RuntimeError("sink exploded: secret-in-the-message")

    harness.sink = broken
    started = time.monotonic()
    outcome = harness.run("print('x', flush=True)\ntime.sleep(60)\n")
    assert outcome == RunOutcome(
        succeeded=False,
        error_message="the log sink failed",
        killed=True,
        retryable=False,
        failure_code=FailureCode.INTERNAL,
    )
    assert time.monotonic() - started < 20


def test_a_sink_that_fails_on_the_final_flush_fails_an_attempt_without_a_record(
    harness: Harness,
) -> None:
    def broken(_data: bytes) -> None:
        raise OSError("disk full")

    harness.sink = broken
    outcome = harness.run("sys.stdout.write('no newline')\nsys.stdout.flush()\nsys.exit(0)\n")
    assert outcome == RunOutcome(
        succeeded=False,
        error_message="the log sink failed",
        retryable=False,
        failure_code=FailureCode.INTERNAL,
    )


def test_a_succeeded_record_survives_a_failing_sink(harness: Harness) -> None:
    """The write landed; the caller still checks the table, and the log loss is its to report."""

    def broken(_data: bytes) -> None:
        raise OSError("disk full")

    harness.sink = broken
    outcome = harness.run(
        "sys.stdout.write('no newline')\n"
        "sys.stdout.flush()\n"
        "write({'status': 'succeeded', 'snapshot_id': 1, 'rows': 1})\n"
    )
    assert outcome == RunOutcome(succeeded=True, snapshot_id=1, rows=1)


def test_a_directory_where_the_result_file_goes_is_a_failure_not_an_exception(
    harness: Harness,
) -> None:
    (harness.attempt / RESULT_FILE).mkdir()
    outcome = harness.run("write({'status': 'succeeded', 'snapshot_id': 1, 'rows': 1})\n")
    assert outcome.succeeded is False
    assert (outcome.error_message or "").startswith("could not prepare the task directory")


def test_a_stdin_write_that_would_block_is_retried(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_write = os.write
    blocked: list[int] = []

    def write(descriptor: int, data: bytes | memoryview) -> int:
        if not blocked and len(data) > 1000:  # the description, not any other write
            blocked.append(descriptor)
            raise BlockingIOError(errno.EAGAIN, "would block")
        return real_write(descriptor, data)

    monkeypatch.setattr(runners_module.os, "write", write)
    harness.inputs = {f"input_{i}": f"silver.table_{i}" for i in range(200)}
    outcome = harness.run("write({'status': 'succeeded', 'snapshot_id': 1, 'rows': 1})\n")
    assert outcome.succeeded
    assert blocked


def test_a_sink_that_fails_after_the_record_was_written_still_succeeds(harness: Harness) -> None:
    """The kill the sink failure causes must not hide a record that landed first."""

    def broken(data: bytes) -> None:
        if b"after" in data:
            raise OSError("disk full")

    harness.sink = broken
    outcome = harness.run(
        "write({'status': 'succeeded', 'snapshot_id': 6, 'rows': 2})\n"
        "print('after the record', flush=True)\n"
        "time.sleep(60)\n"
    )
    assert outcome == RunOutcome(succeeded=True, snapshot_id=6, rows=2)


def test_a_failure_setting_up_the_loop_kills_and_reaps_the_child(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    pids: list[int] = []
    real_popen = subprocess.Popen

    def popen(*args: object, **kwargs: object) -> object:
        process = real_popen(*args, **kwargs)  # ty: ignore[no-matching-overload]
        pids.append(process.pid)
        return process

    def selector() -> object:
        raise OSError(errno.EMFILE, "too many open files")

    monkeypatch.setattr(runners_module.subprocess, "Popen", popen)
    monkeypatch.setattr(runners_module.selectors, "DefaultSelector", selector)
    outcome = harness.run("time.sleep(60)\n")
    assert outcome.succeeded is False
    assert "could not supervise the task process" in (outcome.error_message or "")
    assert pids
    with pytest.raises(ChildProcessError):
        os.waitpid(pids[0], os.WNOHANG)  # already reaped by the runner


_DIGEST = "a" * 64


@pytest.mark.parametrize(
    ("record", "accepted"),
    [
        (
            {"status": "succeeded", "output": "artifact", "version": 3, "manifest_sha256": _DIGEST},
            True,
        ),
        (
            {"status": "succeeded", "output": "artifact", "version": 0, "manifest_sha256": _DIGEST},
            False,
        ),
        (
            {
                "status": "succeeded",
                "output": "artifact",
                "version": -1,
                "manifest_sha256": _DIGEST,
            },
            False,
        ),
        (
            {
                "status": "succeeded",
                "output": "artifact",
                "version": True,
                "manifest_sha256": _DIGEST,
            },
            False,
        ),
        (
            {
                "status": "succeeded",
                "output": "artifact",
                "version": 3.0,
                "manifest_sha256": _DIGEST,
            },
            False,
        ),
        (
            {
                "status": "succeeded",
                "output": "artifact",
                "version": "3",
                "manifest_sha256": _DIGEST,
            },
            False,
        ),
        (
            {
                "status": "succeeded",
                "output": "artifact",
                "version": 3,
                "manifest_sha256": "A" * 64,
            },
            False,
        ),
        (
            {
                "status": "succeeded",
                "output": "artifact",
                "version": 3,
                "manifest_sha256": "a" * 63,
            },
            False,
        ),
        ({"status": "succeeded", "output": "artifact", "version": 3, "manifest_sha256": 7}, False),
        ({"status": "succeeded", "output": "artifact", "version": 3}, False),
        (
            {
                "status": "succeeded",
                "output": "artifact",
                "version": 2**63,
                "manifest_sha256": _DIGEST,
            },
            False,
        ),
        (
            {"status": "succeeded", "output": "table", "version": 3, "manifest_sha256": _DIGEST},
            False,
        ),
        (
            {
                "status": "succeeded",
                "output": ["artifact"],
                "version": 3,
                "manifest_sha256": _DIGEST,
            },
            False,
        ),
        ({"status": "succeeded", "rows": 2, "snapshot_id": 5}, True),
        (
            {
                "status": "succeeded",
                "output": "artifact",
                "version": 3,
                "manifest_sha256": _DIGEST,
                "rows": 2,
            },
            True,
        ),
        ({"status": "succeeded", "version": 3, "manifest_sha256": _DIGEST}, False),
    ],
)
def test_an_artifact_record_is_read_as_untrusted_input(
    tmp_path: Path, record: dict[str, object], accepted: bool
) -> None:
    path = tmp_path / "result.json"
    path.write_text(json.dumps(record))
    output = "artifact" if "output" in record else "table"
    assert (runners_module._read_record(path, output) is not None) is accepted


@pytest.mark.parametrize(
    ("record", "task_output"),
    [
        ({"status": "succeeded", "rows": 2, "snapshot_id": 5}, "artifact"),
        (
            {"status": "succeeded", "output": "artifact", "version": 3, "manifest_sha256": _DIGEST},
            "table",
        ),
    ],
    ids=["table-record-for-an-artifact-task", "artifact-record-for-a-table-task"],
)
def test_a_record_of_the_other_kind_of_task_is_no_usable_record(
    tmp_path: Path, record: dict[str, object], task_output: str
) -> None:
    """The record is judged against what the task produces, not only on its own shape."""
    path = tmp_path / "result.json"
    path.write_text(json.dumps(record))
    assert runners_module._read_record(path, task_output) is None


def test_a_failed_record_is_read_for_either_kind_of_task(tmp_path: Path) -> None:
    path = tmp_path / "result.json"
    path.write_text(json.dumps({"status": "failed", "message": "x"}))
    for output in ("table", "artifact"):
        assert runners_module._read_record(path, output) == {"status": "failed", "message": "x"}


def _outcome(record: dict[str, object] | None, **overrides: object) -> RunOutcome:
    """Decide an outcome from a record with the usual arguments.

    Parameters
    ----------
    record
        The record.
    **overrides
        Arguments of `_outcome_from` to replace.

    Returns
    -------
    RunOutcome
        The outcome.
    """
    arguments: dict[str, Any] = {
        "code": 0,
        "stopped": False,
        "timed_out": False,
        "timeout_seconds": 10.0,
        "stop": StopSignal(),
        "secrets_to_hide": [],
        **overrides,
    }
    return runners_module._outcome_from(record, **arguments)


def test_an_artifact_outcome_claims_no_row_count_or_snapshot() -> None:
    outcome = _outcome(
        {"status": "succeeded", "output": "artifact", "version": 3, "manifest_sha256": _DIGEST}
    )
    assert (outcome.succeeded, outcome.rows, outcome.snapshot_id) == (True, None, None)
    assert not hasattr(outcome, "version")  # the worker reads the version from the catalog


def test_a_table_record_with_artifact_fields_claims_no_version() -> None:
    outcome = _outcome(
        {
            "status": "succeeded",
            "rows": 2,
            "snapshot_id": 5,
            "version": 9,
            "manifest_sha256": _DIGEST,
        }
    )
    assert (outcome.succeeded, outcome.rows, outcome.snapshot_id) == (True, 2, 5)


def test_exit_status_two_is_a_permanent_disagreement_about_the_description() -> None:
    outcome = _outcome(None, code=2)
    assert outcome.retryable is False
    assert outcome.error_message is not None
    assert "task description" in outcome.error_message
    assert "same lakana version" in outcome.error_message
    other = _outcome(None, code=3)
    assert other.retryable is True
    assert (outcome.failure_code, other.failure_code) == (FailureCode.BUILD_FAILED,) * 2


def test_a_failed_record_is_a_build_failure_unless_it_says_conflict() -> None:
    """The process reported its own failure; only an explicit conflict flag is another code."""
    failed = _outcome({"status": "failed", "message": "boom"})
    conflicted = _outcome({"status": "failed", "message": "lost the race", "conflict": True})

    assert failed.failure_code is FailureCode.BUILD_FAILED
    assert conflicted.failure_code is FailureCode.CONFLICT


def test_a_process_ended_by_a_signal_or_without_a_result_is_a_build_failure() -> None:
    """Nothing says more than that the process did not finish."""
    assert _outcome(None, code=-9).failure_code is FailureCode.BUILD_FAILED
    assert _outcome(None, code=1).failure_code is FailureCode.BUILD_FAILED


def test_a_stop_for_no_reason_is_internal() -> None:
    """The supervisor stopped the process and cannot say why."""
    outcome = _outcome(None, stopped=True)

    assert outcome.failure_code is FailureCode.INTERNAL


def test_a_success_has_no_failure_code() -> None:
    """Only a failure carries one."""
    assert _outcome({"status": "succeeded", "snapshot_id": 1, "rows": 1}).failure_code is None


def test_a_harness_that_exits_with_status_two_fails_for_good(harness: Harness) -> None:
    outcome = harness.run("print('task description: refused', file=sys.stderr)\nsys.exit(2)\n")
    assert outcome.succeeded is False
    assert outcome.retryable is False
    assert "task description: refused" in harness.output()


@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), float("-inf")])
def test_a_timeout_that_is_not_finite_is_refused_before_a_process_starts(
    harness: Harness, timeout: float
) -> None:
    """Point 11: the harness refuses NaN and Infinity, so the worker never sends them."""
    outcome = harness.run("open(result + '.started', 'w').close()\n", timeout=timeout)
    assert outcome.succeeded is False
    assert outcome.retryable is False
    assert outcome.error_message is not None and "finite" in outcome.error_message
    assert outcome.failure_code is FailureCode.INTERNAL
    assert not (harness.attempt / (RESULT_FILE + ".started")).exists()


def test_an_attempt_directory_that_is_not_absolute_is_refused_before_a_process_starts(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(harness.attempt.parent)
    harness.fields = {"attempt_dir": Path(harness.attempt.name)}
    outcome = harness.run("open(result + '.started', 'w').close()\n")
    assert (outcome.succeeded, outcome.retryable) == (False, False)
    assert outcome.error_message is not None and "absolute" in outcome.error_message
    assert not (harness.attempt / (RESULT_FILE + ".started")).exists()


@pytest.mark.parametrize(
    ("seconds", "text"),
    [(0.4, "0.4s"), (1.5, "1.5s"), (3.0, "3s"), (900.0, "900s"), (1_000_000.0, "1000000s")],
)
def test_the_timeout_message_keeps_a_fraction_of_a_second(seconds: float, text: str) -> None:
    """A timeout under a second no longer reads "0s"."""
    outcome = _outcome(None, code=-9, stopped=True, timed_out=True, timeout_seconds=seconds)
    assert outcome.error_message == f"timed out after {text}"


@pytest.mark.parametrize(
    "value", ["pass\xa0word123", "pass​word123", "pass\x1bword123", "pässword123"]
)
def test_a_secret_with_a_character_that_is_not_printable_is_hidden_in_the_record(
    value: str,
) -> None:
    """Redaction sees the text as written; only then are such characters replaced with `?`."""
    hidden = sorted(secret_forms(value))
    cleaned = runners_module._clean(f"connect failed for {value} at host", hidden, 2000)
    assert cleaned == "connect failed for *** at host"


def test_a_lone_surrogate_in_a_record_is_still_replaced() -> None:
    assert runners_module._clean("bad \ud800 text \x00", [], 2000) == "bad ? text ?"


def test_the_local_task_process_trusts_the_workers_bundle(harness: Harness, tmp_path: Path) -> None:
    """`SSL_CERT_FILE` and `LAKANA_CA_BUNDLE` name the worker's bundle; without one, neither."""
    harness.trust = tmp_path / "trust.pem"
    body = (
        "keys = ('SSL_CERT_FILE', 'LAKANA_CA_BUNDLE')\n"
        "print(json.dumps({k: os.environ.get(k) for k in keys}))\n"
        "write({'status': 'succeeded', 'snapshot_id': None, 'rows': 0})\n"
    )
    assert harness.run(body).succeeded
    assert json.loads(harness.output().splitlines()[0]) == {
        "SSL_CERT_FILE": str(tmp_path / "trust.pem"),
        "LAKANA_CA_BUNDLE": str(tmp_path / "trust.pem"),
    }
    assert (
        child_environment(tmp_path, tmp_path)
        .keys()
        .isdisjoint({"SSL_CERT_FILE", "LAKANA_CA_BUNDLE"})
    )
