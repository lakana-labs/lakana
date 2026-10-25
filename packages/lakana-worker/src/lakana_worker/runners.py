"""Running one Python task: the `Runner` protocol, code unpacking and the two runners.

`LocalProcessRunner` and `DockerRunner` implement the protocol.

A Python task is trusted code from a reviewed repository, and
`LocalProcessRunner` is **not a sandbox**: the process runs as the worker's
OS user. Its scrubbed environment and private temporary directory are
hygiene. What bounds the task is its token (bound to the attempt, scoped to
its inputs and its own table) and the process group the supervisor can kill.

`DockerRunner` is isolation between projects: each task runs in a container of its own that the
launcher starts (`lakana_worker.launcher`). Containers share the host's kernel, so it is not a
sandbox for hostile code.

The token is written to the child's stdin and nowhere else: never on a
command line, never in the environment. Everything the child writes to
stdout or stderr is redacted before it reaches the task log, and the result
file the child writes is read as untrusted input: it may be missing, a link,
a FIFO, huge or forged, and what it says about the table is a claim the
caller checks, never a fact.
"""

from __future__ import annotations

import contextlib
import enum
import errno
import hashlib
import io
import json
import math
import os
import re
import selectors
import signal
import stat
import subprocess  # nosec B404 -- starts the task's own interpreter, fixed argv, no shell
import tarfile
import threading
import time
import uuid
import zlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from lakana_core.runs.failures import FailureCode
from lakana_worker.git import HFS_IGNORABLE, fold_name
from lakana_worker.launcher.requests import EGRESS_MISSING, allowed_roots
from lakana_worker.launcher_client import (
    RELEASE_SKEW,
    LauncherClient,
    LauncherRefused,
    LauncherTimedOut,
    LauncherUnavailable,
    LogSinkFailed,
    release_skew,
)
from lakana_worker.processes import (
    OutputRedactor,
    is_private_directory,
    remove_private_tree,
    signal_group,
)
from lakana_worker.trust import install_trust

MAX_ERROR_MESSAGE_CHARS = 2000
"""Longest task error message stored; the value `lakana.runner` truncates to as well."""

MAX_TASK_OUTPUT_BYTES = 1024 * 1024
"""Most process output (venv build and task) kept in the log per attempt.

The worker wraps its log sink in one `CappedSink` of this size and passes it
both to the venv build and to the runner, so one cap covers everything.
"""

RESULT_FILE = "result.json"

_MAX_RESULT_BYTES = 64 * 1024
_MAX_ERROR_TYPE_CHARS = 100
_MAX_INT64 = 2**63 - 1
_STDIN_CHUNK = 65536
_MAX_UNPACK_MESSAGE = 500
_ENVIRONMENT_ERRNOS = frozenset(
    {errno.ENOSPC, errno.EIO, errno.EMFILE, errno.ENFILE, errno.EDQUOT, errno.ENOMEM, errno.EROFS}
)
"""Errors while unpacking that say the machine, not the archive, is at fault."""
_DRAIN_SECONDS = 2.0
"""How long output is still read after the process group was killed or its leader exited."""

_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")

_DESCRIPTION_VERSION = 2
"""The task description's version for an asset that replaces and declares no connection: a
harness of the release before the write modes reads it."""
_WRITES_DESCRIPTION_VERSION = 3
"""The version that adds ``write``, ``key``, ``watermark`` and ``connections``; sent only to an
asset that uses one of them (`lakana.runner.parse_description` reads 2 and 3)."""
_DESCRIPTION_REFUSED = 2
"""The exit status with which the harness refuses a task description it cannot use."""
_DESCRIPTION_MESSAGE = (
    "the task process exited with status 2 without a result: the harness refuses a task "
    "description it does not understand, so the worker and the lakana package in the task's "
    "environment disagree about it (the output above says which field). Check that the "
    "project's uv.lock pins the same lakana version as this worker, then deploy again. If the "
    "asset's own code exits with status 2, use another status"
)


class StopReason(enum.StrEnum):
    """Why the supervisor stops a running task process."""

    CANCELLED = "cancelled"
    LEASE_LOST = "lease lost"
    DRAIN = "worker draining"
    MEMORY = "memory limit"


_STOP_CODES: Mapping[StopReason | None, FailureCode] = {
    StopReason.CANCELLED: FailureCode.CANCELLED,
    StopReason.LEASE_LOST: FailureCode.WORKER_LOST,
    StopReason.DRAIN: FailureCode.WORKER_LOST,
    StopReason.MEMORY: FailureCode.MEMORY,
}
"""The failure code of a process the supervisor stopped, by why; a stop for no reason is
``internal``."""


class StopSignal:
    """Set by the supervisor on cancellation, a lost lease, or worker drain."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self.reason: StopReason | None = None

    def set(self, reason: StopReason) -> None:
        """Ask the runner to stop the process; the first reason wins.

        Parameters
        ----------
        reason
            Why.
        """
        if self.reason is None:
            self.reason = reason
        self._event.set()

    def is_set(self) -> bool:
        """Say whether a stop was requested.

        Returns
        -------
        bool
            Whether `set` was called.
        """
        return self._event.is_set()


@dataclass(frozen=True)
class PythonTask:
    """Everything a runner needs to run one attempt of a Python asset.

    Parameters
    ----------
    code_dir
        The unpacked, read-only repository; the process's working directory.
    asset_path
        The asset file, relative to `code_dir`.
    inputs
        Parameter name → dotted table name.
    table
        The asset's own table.
    environment
        Environment name.
    server_url
        Base URL of the Lakana server as the process reaches it.
    token
        The attempt's bound task token. Left out of `repr`.
    timeout_seconds
        The asset's timeout, counted from process start.
    venv
        The venv whose `bin/python` runs the harness.
    expected_snapshot_id
        The target's current snapshot id as the worker read it, or ``"none"``.
    attempt_dir
        A private per-attempt directory: `HOME`, `TMPDIR` and the result file;
        sent to the harness as `work_dir`, where it makes `inputs` and `output`.
        Must be absolute.
    output
        ``table`` or ``artifact``: what the asset produces.
    input_kinds
        Parameter name to ``table`` or ``artifact``; a parameter missing here is
        sent as ``table``.
    input_versions
        Parameter name to the version of an artifact input, resolved by the
        worker before the process starts; exactly the artifact inputs.
    decimals
        ``float`` or ``exact``: how a SQL decimal reaches the function, from the
        manifest entry (an entry without the key means ``float``).
    task_run_id
        The task run this attempt belongs to, for the memory watchdog's log and record;
        `None` outside the worker (tests).
    environment_key
        The environment under DockerRunner (`VenvLease.key`); `None` for the local runner.
    python
        The Python version the task runs on, ``3.11`` to ``3.14``; DockerRunner picks the image
        by it.
    write
        ``replace``, ``append`` or ``merge``: how the harness writes the table.
    key
        The merge key's columns; empty unless `write` is ``merge``.
    watermark_column
        The asset's `watermark` column, if it has one.
    watermark
        The encoded watermark the worker read (``<type>:<value>``), or `None` when the table
        has none yet.
    connections
        Each connection the asset declares to its routing, as the harness gets it: its ``kind``,
        and the ``grant`` (the ``[host, port]`` pairs the attempt may reach) with a
        ``relay_port`` under the Docker runner, or a mounted root's ``root``, ``root_dir``,
        ``path`` and ``root_path``.
    table_exists
        Whether the asset's table existed when the worker read it (one created by a first run
        of zero rows has no snapshot and still exists).
    hidden
        Secret values the log redactor masks in the task's output. Left out of `repr` and never
        sent to the harness.
    egress
        The ``(host, port)`` pairs the task may reach through the egress proxy (Docker runner).
    relay
        The relay's routes, ``(listen port, host, port)`` each (Docker runner).
    roots
        The ingestion roots the task reads, in declaration order.
    root_users
        Each of `roots` to the first connection naming it, for the message when a root is
        missing.
    """

    code_dir: Path
    asset_path: str
    inputs: Mapping[str, str]
    table: str
    environment: str
    server_url: str
    token: str = field(repr=False)
    timeout_seconds: float
    venv: Path
    expected_snapshot_id: int | str
    attempt_dir: Path
    output: str = "table"
    input_kinds: Mapping[str, str] = field(default_factory=dict)
    input_versions: Mapping[str, int] = field(default_factory=dict)
    decimals: str = "float"
    task_run_id: uuid.UUID | None = None
    environment_key: str | None = None
    python: str | None = None
    write: str = "replace"
    key: tuple[str, ...] = ()
    watermark_column: str | None = None
    watermark: str | None = None
    connections: Mapping[str, Mapping[str, object]] = field(default_factory=dict)
    table_exists: bool = False
    hidden: tuple[str, ...] = field(default=(), repr=False)
    egress: tuple[tuple[str, int], ...] = ()
    relay: tuple[tuple[int, str, int], ...] = ()
    roots: tuple[str, ...] = ()
    root_users: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class RunOutcome:
    """How a task process ended.

    `snapshot_id` and `rows` are what the process *claimed* in a
    file that user code can write itself. They are display hints at most: the
    caller decides success from the table, or from the artifact version this
    attempt committed, never from them.

    Parameters
    ----------
    succeeded
        A ``succeeded`` result record from the process (and, unless the
        runner had to stop it, exit code 0). Not proof that the table changed.
    snapshot_id
        The snapshot the process claims it wrote, if any.
    rows
        The row count it claims.
    error_message
        Safe, redacted, truncated text for a failure.
    killed
        Whether the runner stopped the process (timeout or stop signal).
    timed_out
        Whether it was stopped for its timeout.
    error_type
        The harness's exception class name, sanitised; display text only.
    retryable
        `False` when the harness says a retry cannot help.
    conflict
        Whether the harness's explicit ``conflict`` flag was ``true``: the
        upload lost a compare-and-swap on the table's snapshot, or an artifact
        upload was refused with a conflict. The only conflict signal;
        `error_type` is never consulted.
    failure_code
        Why the process failed; `None` for a success. Every failure sets one.
    """

    succeeded: bool
    snapshot_id: int | None = None
    rows: int | None = None
    error_message: str | None = None
    killed: bool = False
    timed_out: bool = False
    error_type: str | None = None
    retryable: bool = True
    conflict: bool = False
    failure_code: FailureCode | None = None


class Runner(Protocol):
    """Runs one Python task to completion: `LocalProcessRunner` or `DockerRunner`."""

    @property
    def isolated(self) -> bool:
        """Whether each task runs in a container of its own, isolated from the worker.

        Returns
        -------
        bool
            `True` for a container runner; a protected environment runs Python tasks only on
            one.
        """
        ...

    def run(
        self, task: PythonTask, on_log: Callable[[bytes], None], stop: StopSignal
    ) -> RunOutcome:
        """Run the task and report how it ended; never raises for the task's own failure.

        Parameters
        ----------
        task
            What to run.
        on_log
            Receives the process's redacted output as it arrives.
        stop
            Set by the supervisor to have the process stopped.

        Returns
        -------
        RunOutcome
            The outcome.
        """
        ...


POLARS_SPILL_DIRECTORY = "polars-spill"
"""The directory in a task's `TMPDIR` that Polars 2 may spill into; the runner makes it."""


def child_environment(
    home: Path, tmp: Path, memory_limit: int | None = None, trust: Path | None = None
) -> dict[str, str]:
    """Build the complete environment of a task process.

    Polars 2 spills to disk from 80 % of the memory it believes the machine has. It
    reads the machine's total (verified inside a 1 GiB cgroup: it still reports the
    host's), not the container's or the worker's limit, so on a large host the watchdog
    (`LAKANA_WORKER__MEMORY_LIMIT`) would stop the task before Polars ever spills. With a
    limit, the task is told that the machine has that much memory and may spill at most
    that many megabytes. With or without one it is told to spill into a directory of its own
    in `tmp`: Polars' default is a path under `/var/tmp` that every task and every attempt shares
    and nothing removes.

    Parameters
    ----------
    home
        A private directory used as `HOME`.
    tmp
        A private directory used as `TMPDIR`.
    memory_limit
        The worker's memory limit in bytes, or `None` when it has none (the two Polars
        sizes are then not set; the spill directory is).
    trust
        The worker's trust bundle, or `None` without `LAKANA_RUN__CA_BUNDLE`.

    Returns
    -------
    dict
        `PATH`, `HOME`, `TMPDIR`, `PYTHONUNBUFFERED`, `PYTHONDONTWRITEBYTECODE`,
        `POLARS_OOC_SPILL_DIR` and, with `memory_limit`, `POLARS_OVERRIDE_TOTAL_MEMORY_MB`
        and `POLARS_OOC_DISK_BUDGET_MB` (the limit in megabytes of 10**6 bytes, as Polars
        counts them, at least 1), with `trust` `SSL_CERT_FILE` and `LAKANA_CA_BUNDLE` (both
        naming it); nothing else of the worker's own environment.
    """
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "TMPDIR": str(tmp),
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "POLARS_OOC_SPILL_DIR": str(tmp / POLARS_SPILL_DIRECTORY),
    }
    if memory_limit is not None:
        megabytes = str(max(memory_limit // 1_000_000, 1))
        env["POLARS_OVERRIDE_TOTAL_MEMORY_MB"] = megabytes
        env["POLARS_OOC_DISK_BUDGET_MB"] = megabytes
    if trust is not None:
        env["SSL_CERT_FILE"] = env["LAKANA_CA_BUNDLE"] = str(trust)
    return env


def token_secrets(token: str) -> list[str]:
    """List the forms of a task token to redact from output.

    Parameters
    ----------
    token
        ``lakana_task_<key_id>_<secret>``.

    Returns
    -------
    list of str
        The whole token and its secret part.
    """
    parts = token.split("_", 3)
    return [token, parts[3]] if len(parts) == 4 else [token]


def _is_count(value: object) -> bool:
    """Say whether a JSON value is an integer in the range of a bigint column.

    Parameters
    ----------
    value
        A decoded JSON value.

    Returns
    -------
    bool
        Whether it is a non-negative `int` (not a `bool`) below 2**63.
    """
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= _MAX_INT64


def _read_record(path: Path, expected_output: str) -> dict[str, object] | None:
    """Read the harness's result record as untrusted input.

    Parameters
    ----------
    path
        The result file.
    expected_output
        What the task produces, ``table`` or ``artifact``: a ``succeeded`` record
        of the other shape is not a record of this task.

    Returns
    -------
    dict or None
        A well-formed ``succeeded`` or ``failed`` record; `None` for a
        missing file, a link, a non-file, an oversized file, or anything
        that is not such a record. A table record has `rows` (and maybe
        `snapshot_id`); an artifact record has ``output: "artifact"``, a
        positive `version` and a `manifest_sha256` of 64 lower-case hex digits.
        A record that has `output` but not that shape is refused, and so is a
        ``succeeded`` record whose shape is not that of `expected_output`.
    """
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_RESULT_BYTES:
            return None
        data = os.read(descriptor, _MAX_RESULT_BYTES)
    except OSError:
        return None
    finally:
        os.close(descriptor)
    try:
        record = json.loads(data)
    except (ValueError, RecursionError):
        return None
    if not isinstance(record, dict):
        return None
    if record.get("status") == "succeeded" and "output" in record:
        version, digest = record.get("version"), record.get("manifest_sha256")
        if (
            expected_output == "artifact"
            and record["output"] == "artifact"
            and _is_count(version)
            and version != 0
            and isinstance(digest, str)
            and _SHA256.match(digest) is not None
        ):
            return record
        return None
    if record.get("status") == "succeeded":
        snapshot = record.get("snapshot_id")
        if (
            expected_output == "table"
            and (snapshot is None or _is_count(snapshot))
            and _is_count(record.get("rows"))
        ):
            return record
        return None
    if record.get("status") == "failed" and isinstance(record.get("message"), str):
        return record
    return None


def _clean(text: str, secrets_to_hide: list[str], limit: int) -> str:
    """Make record text safe to store: redacted, printable, bounded.

    The text is redacted as it was written first, so a secret that holds a character that is
    not printable (a no-break space, a zero-width space, an escape) is still found. Control,
    format, surrogate and private-use characters then become ``?`` (never deleted, which could
    join the halves of a secret), and the text is redacted again and only then cut.

    Parameters
    ----------
    text
        Text from the result record.
    secrets_to_hide
        The task token, its secret part and the forms of the connections' secrets.
    limit
        Most characters kept.

    Returns
    -------
    str
        The cleaned text.
    """
    raw = _redacted(text.encode("utf-8", "surrogatepass"), secrets_to_hide)
    written = raw.decode("utf-8", "surrogatepass")
    printable = "".join(c if c.isprintable() or c in "\n\t" else "?" for c in written)
    return _redacted(printable.encode("utf-8"), secrets_to_hide).decode("utf-8")[:limit]


def _redacted(data: bytes, secrets_to_hide: list[str]) -> bytes:
    """Mask every secret in one piece of text, as the log stream masks it.

    Parameters
    ----------
    data
        The text as bytes.
    secrets_to_hide
        The values to mask.

    Returns
    -------
    bytes
        The text with each value replaced by ``***``.
    """
    redacted: list[bytes] = []
    redactor = OutputRedactor(secrets_to_hide, redacted.append, max_line=len(data) + 1)
    redactor.feed(data)
    redactor.close()
    return b"".join(redacted)


def _leader_exited(process: subprocess.Popen[bytes]) -> bool:
    """Say whether the process has exited, without reaping it.

    Leaving the zombie in place keeps its pid, and so its process group id,
    reserved: the group can still be signalled safely.

    Parameters
    ----------
    process
        The task process.

    Returns
    -------
    bool
        Whether it has exited.
    """
    return os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None


def _seconds(value: float) -> str:
    """Write a duration for a message without losing its fraction.

    Parameters
    ----------
    value
        Seconds.

    Returns
    -------
    str
        ``600s`` for a whole number, ``0.4s`` for a fraction.
    """
    return f"{int(value)}s" if value == int(value) else f"{value:g}s"


def _outcome_from(
    record: dict[str, object] | None,
    *,
    code: int,
    stopped: bool,
    timed_out: bool,
    timeout_seconds: float,
    stop: StopSignal,
    secrets_to_hide: list[str],
    memory_detail: str | None = None,
) -> RunOutcome:
    """Decide how an attempt ended from its record, exit code and how it was stopped.

    A ``succeeded`` record wins over everything but a bad exit status of a
    process nobody stopped: the record is written only after the upload
    returned, so a process that then hung and was killed still succeeded. Then
    a timeout, the memory watchdog's kill, a stop, a ``failed`` record, and the exit status.

    Parameters
    ----------
    record
        The validated record, or `None`.
    code
        The exit status (negative for a signal).
    stopped
        Whether the runner stopped the process.
    timed_out
        Whether it stopped it for the timeout.
    timeout_seconds
        The asset timeout, for the message.
    stop
        The supervisor's signal, for the reason.
    secrets_to_hide
        Forms of the token to redact from record text.
    memory_detail
        The message with which the memory watchdog ended the process, or `None`.

    Returns
    -------
    RunOutcome
        The outcome.
    """
    if record is not None and record["status"] == "succeeded" and (code == 0 or stopped):
        if record.get("output") == "artifact":
            return RunOutcome(succeeded=True)
        snapshot, rows = record.get("snapshot_id"), record["rows"]
        return RunOutcome(
            succeeded=True,
            snapshot_id=snapshot if isinstance(snapshot, int) else None,
            rows=rows if isinstance(rows, int) else None,
        )
    if timed_out:
        return RunOutcome(
            succeeded=False,
            error_message=f"timed out after {_seconds(timeout_seconds)}",
            killed=True,
            timed_out=True,
            failure_code=FailureCode.TIMEOUT,
        )
    if memory_detail is not None:
        return RunOutcome(
            succeeded=False,
            error_message=memory_detail,
            killed=True,
            failure_code=FailureCode.MEMORY,
        )
    if stopped:
        reason = stop.reason.value if stop.reason is not None else "stopped"
        return RunOutcome(
            succeeded=False,
            error_message=f"stopped: {reason}",
            killed=True,
            failure_code=_STOP_CODES.get(stop.reason, FailureCode.INTERNAL),
        )
    if record is not None and record["status"] == "failed":
        kind = record.get("error_type")
        message = _clean(str(record["message"]), secrets_to_hide, MAX_ERROR_MESSAGE_CHARS)
        return RunOutcome(
            succeeded=False,
            error_message=message or "the task failed without a message",
            error_type=(
                _clean(kind, secrets_to_hide, _MAX_ERROR_TYPE_CHARS)
                if isinstance(kind, str)
                else None
            ),
            retryable=record.get("retryable") is not False,
            conflict=record.get("conflict") is True,
            failure_code=(
                FailureCode.CONFLICT if record.get("conflict") is True else FailureCode.BUILD_FAILED
            ),
        )
    if code < 0:
        return RunOutcome(
            succeeded=False,
            error_message=f"the task process was killed by signal {-code}",
            failure_code=FailureCode.BUILD_FAILED,
        )
    if code == _DESCRIPTION_REFUSED:
        return RunOutcome(
            succeeded=False,
            error_message=_DESCRIPTION_MESSAGE,
            retryable=False,
            failure_code=FailureCode.BUILD_FAILED,
        )
    return RunOutcome(
        succeeded=False,
        error_message=f"the task process exited with status {code} without a result",
        failure_code=FailureCode.BUILD_FAILED,
    )


NO_SDK_MESSAGE = (
    "the task's environment does not contain the lakana package, so nothing can run the asset. "
    "A package in a dependency group or an extra is not installed for a task: add "
    "`lakana[assets]` to `[project] dependencies` in pyproject.toml, run `uv lock`, commit and "
    "deploy again"
)


_INVALID_TASK = (
    "the worker built an invalid task (a timeout that is not a finite number, an attempt "
    "directory that is not an absolute path, or, for a container, no environment key or no "
    "Python version), which the harness or the launcher would refuse; this is a worker "
    "defect: report it, with the worker's version"
)
_LAUNCHER_STOPPED = (
    "the launcher stopped the task's container without the worker asking (its deadline passed, "
    "or the launcher restarted); run the task again, and if it happens again, docker compose "
    "logs launcher has the reason"
)


def task_description(
    task: PythonTask, *, result_path: str, work_dir: str, parent_pid: int | None
) -> bytes:
    """Write the task description the harness reads from its stdin.

    Parameters
    ----------
    task
        The task.
    result_path
        Where the harness writes its record, as the process sees it.
    work_dir
        The attempt's private directory, as the process sees it.
    parent_pid
        The worker's pid for the harness's parent check; `None` in a container, where the
        launcher, not the worker, is the parent.

    Returns
    -------
    bytes
        The JSON document: version `_WRITES_DESCRIPTION_VERSION` for an asset that appends,
        merges or declares a connection, else version `_DESCRIPTION_VERSION`, unchanged, so a
        harness of the previous release still runs every other asset.
    """
    document: dict[str, object] = {
        "version": _DESCRIPTION_VERSION,
        "server_url": task.server_url,
        "token": task.token,
        "environment": task.environment,
        "asset_path": task.asset_path,
        "table": task.table,
        "inputs": dict(task.inputs),
        "expected_snapshot_id": task.expected_snapshot_id,
        "result_path": result_path,
        "request_timeout_seconds": float(task.timeout_seconds),
        "parent_pid": parent_pid,
        "output": task.output,
        "input_kinds": {p: task.input_kinds.get(p, "table") for p in task.inputs},
        "input_versions": dict(task.input_versions),
        "decimals": task.decimals,
        "work_dir": work_dir,
    }
    if task.write != "replace" or task.connections:
        document["version"] = _WRITES_DESCRIPTION_VERSION
        document["write"] = task.write
        document["key"] = list(task.key)
        document["watermark"] = (
            None
            if task.watermark_column is None
            else {"column": task.watermark_column, "value": task.watermark}
        )
        document["table_exists"] = task.table_exists
        document["connections"] = {
            name: dict(routing) for name, routing in task.connections.items()
        }
    return json.dumps(document).encode("utf-8")


def _has_module(venv: Path, module: str) -> bool:
    """Say whether a venv's site-packages holds a module's file; nothing is imported.

    Parameters
    ----------
    venv
        The venv directory.
    module
        The dotted module name run with ``-m``.

    Returns
    -------
    bool
        Whether ``<venv>/lib*/python*/site-packages/<module path>.py`` exists.
    """
    relative = module.replace(".", "/") + ".py"
    return any(venv.glob(f"lib*/python*/site-packages/{relative}"))


class RunningTask:
    """A running task process, as the memory watchdog reaches it.

    The watchdog measures `pid` and calls `kill_for_memory`; it never signals a number it
    measured earlier. The kill and the reaping of the process take the same lock, so a kill
    never lands after the leader was reaped (when its pid may belong to someone else).

    Parameters
    ----------
    process
        The task process, started with ``start_new_session=True``.
    stop
        The attempt's stop signal, which gets the reason `StopReason.MEMORY`.
    asset
        The asset's name, for the log.
    task_run_id
        The task run, for the log and for recording what became of it.
    """

    def __init__(
        self,
        process: subprocess.Popen[bytes],
        stop: StopSignal,
        asset: str,
        task_run_id: uuid.UUID | None,
    ) -> None:
        self._process = process
        self._stop = stop
        self._lock = threading.Lock()
        self._reaped = False
        self.asset = asset
        self.task_run_id = task_run_id
        self.detail: str | None = None
        """Why the watchdog ended the process; `None` when it did not."""

    @property
    def pid(self) -> int:
        """Give the process id of the task's leader, to measure the tree under it.

        Returns
        -------
        int
            The id.
        """
        return self._process.pid

    def kill_for_memory(self, detail: str) -> bool:
        """Kill the task's process group at once because the worker is over its memory limit.

        No ask and no grace: memory cannot wait. Called from the watchdog's thread. Does
        nothing for a task that has ended: the leader exited (the runner kills what is left of its
        group within its poll interval, and the watchdog moves on to the next largest) or was
        reaped.

        Parameters
        ----------
        detail
            Why, as the attempt's failure message reads.

        Returns
        -------
        bool
            Whether a live task was killed.
        """
        with self._lock:
            if self._reaped or _leader_exited(self._process):
                return False
            signal_group(self._process, signal.SIGKILL)
            self.detail = detail
            self._stop.set(StopReason.MEMORY)
            return True

    def reap(self) -> int:
        """Wait for the leader to end and reap it, under the lock the kill takes.

        Returns
        -------
        int
            The exit status (negative for a signal).
        """
        with contextlib.suppress(ChildProcessError):
            os.waitid(os.P_PID, self._process.pid, os.WEXITED | os.WNOWAIT)
        with self._lock:
            code = self._process.wait()
            self._reaped = True
        return code


class TaskRegistry:
    """The task processes that are running, for the memory watchdog."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._running: list[RunningTask] = []

    def add(self, task: RunningTask) -> None:
        """Register a started task process.

        Parameters
        ----------
        task
            The task.
        """
        with self._lock:
            self._running.append(task)

    def remove(self, task: RunningTask) -> None:
        """Forget a reaped task process.

        Parameters
        ----------
        task
            The task.
        """
        with self._lock, contextlib.suppress(ValueError):
            self._running.remove(task)

    def running(self) -> list[RunningTask]:
        """List the task processes that are running.

        Returns
        -------
        list of RunningTask
            A copy.
        """
        with self._lock:
            return list(self._running)


TASKS = TaskRegistry()
"""The process-wide registry `LocalProcessRunner` reports to and the memory watchdog reads."""


class LocalProcessRunner:
    """Run a task as a subprocess of the worker, in the task's venv.

    The thread that calls `run` must outlive the process: on Linux the
    harness asks for `PR_SET_PDEATHSIG`, which fires when that *thread*
    exits. `run` returns only after the process is reaped.

    Parameters
    ----------
    kill_grace
        Seconds between SIGTERM and SIGKILL (`LAKANA_RUN__KILL_GRACE`).
    module
        The module run with ``python -P -m``; tests substitute a scripted one.
    poll_seconds
        How often the stop signal and the timeout are checked.
    memory_limit
        The worker's memory limit in bytes, which the task's Polars is told about
        (`child_environment`); `None` when the worker has none.
    trust
        The worker's trust bundle; `None` without `LAKANA_RUN__CA_BUNDLE`.
    registry
        Where the running process is listed for the memory watchdog; `TASKS` by default.
    """

    def __init__(
        self,
        kill_grace: float = 10.0,
        module: str = "lakana.runner",
        poll_seconds: float = 0.2,
        memory_limit: int | None = None,
        trust: Path | None = None,
        registry: TaskRegistry | None = None,
    ) -> None:
        self._registry = registry if registry is not None else TASKS
        self._kill_grace = kill_grace
        self._module = module
        self._poll_seconds = poll_seconds
        self._memory_limit = memory_limit
        self._trust = trust

    @property
    def isolated(self) -> bool:
        """Not isolated: the task is a process of the worker.

        Returns
        -------
        bool
            `False`.
        """
        return False

    def run(
        self, task: PythonTask, on_log: Callable[[bytes], None], stop: StopSignal
    ) -> RunOutcome:
        """Start the harness, feed it the task on stdin, and supervise it to its end.

        Parameters
        ----------
        task
            What to run.
        on_log
            Receives the process's redacted output.
        stop
            Set to have the process group terminated, then killed after the grace.

        Returns
        -------
        RunOutcome
            Success for a ``succeeded`` record (and exit code 0 unless the
            process was stopped); otherwise why it failed. Never raises for
            anything the process does or writes, or for a failing `on_log`
            (that is a failed outcome, ``retryable=False``, process killed).
        """
        if not math.isfinite(task.timeout_seconds) or not task.attempt_dir.is_absolute():
            return RunOutcome(
                succeeded=False,
                error_message=_INVALID_TASK,
                retryable=False,
                failure_code=FailureCode.INTERNAL,
            )
        home, tmp = task.attempt_dir / "home", task.attempt_dir / "tmp"
        result_path = task.attempt_dir / RESULT_FILE
        if not is_private_directory(task.attempt_dir, 0o077):
            return RunOutcome(
                succeeded=False,
                error_message="could not prepare the task directory: the attempt directory "
                "is not private (it must exist, be owned by the worker and have no group "
                "or other access)",
                failure_code=FailureCode.INTERNAL,
            )
        try:
            home.mkdir(mode=0o700, exist_ok=True)
            tmp.mkdir(mode=0o700, exist_ok=True)
            (tmp / POLARS_SPILL_DIRECTORY).mkdir(mode=0o700, exist_ok=True)
            with contextlib.suppress(FileNotFoundError):
                result_path.unlink()
        except OSError as exc:
            return RunOutcome(
                succeeded=False,
                error_message=f"could not prepare the task directory: {exc.strerror or exc}",
                failure_code=FailureCode.INTERNAL,
            )
        if not _has_module(task.venv, self._module):
            return RunOutcome(
                succeeded=False,
                error_message=NO_SDK_MESSAGE,
                retryable=False,
                failure_code=FailureCode.BUILD_FAILED,
            )
        secrets_to_hide = token_secrets(task.token) + list(task.hidden)
        document = task_description(
            task,
            result_path=str(result_path),
            work_dir=str(task.attempt_dir),
            parent_pid=os.getpid(),
        )
        try:
            process = subprocess.Popen(  # nosec B603 -- fixed argv, no shell, minimal env
                [str(task.venv / "bin" / "python"), "-P", "-m", self._module],
                cwd=task.code_dir,
                env=child_environment(home, tmp, self._memory_limit, self._trust),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except (OSError, ValueError) as exc:
            reason = getattr(exc, "strerror", None) or exc
            return RunOutcome(
                succeeded=False,
                error_message=f"could not start the task process: {reason}",
                failure_code=FailureCode.INTERNAL,
            )
        running = RunningTask(process, stop, task.table, task.task_run_id)
        self._registry.add(running)
        stdin, pipe = process.stdin, process.stdout
        assert stdin is not None and pipe is not None  # nosec B101 -- both are PIPE
        stdin_fd = stdin.fileno()
        pending = memoryview(document)
        sink = OutputRedactor(secrets_to_hide, on_log)
        selector: selectors.BaseSelector | None = None
        supervise_error: str | None = None
        deadline = time.monotonic() + task.timeout_seconds
        stopped = timed_out = sink_failed = sink_killed = False
        kill_at: float | None = None
        drain_until: float | None = None
        try:
            os.set_blocking(stdin_fd, False)
            selector = selectors.DefaultSelector()
            selector.register(pipe, selectors.EVENT_READ)
            selector.register(stdin_fd, selectors.EVENT_WRITE)
            while True:
                now = time.monotonic()
                exited = _leader_exited(process)
                if not exited and not stopped and (stop.is_set() or now >= deadline):
                    stopped, timed_out = True, not stop.is_set()
                    signal_group(process, signal.SIGTERM)
                    kill_at = now + self._kill_grace
                if kill_at is not None and now >= kill_at:
                    signal_group(process, signal.SIGKILL)
                    kill_at = None
                    drain_until = now + _DRAIN_SECONDS
                if exited and drain_until is None:
                    signal_group(process, signal.SIGKILL)
                    drain_until = now + _DRAIN_SECONDS
                if not selector.get_map() and exited:
                    break
                if drain_until is not None and now >= drain_until:
                    break
                for key, _events in selector.select(self._poll_seconds):
                    if key.fd == stdin_fd:
                        try:
                            sent = os.write(stdin_fd, pending[:_STDIN_CHUNK])
                        except BlockingIOError:
                            sent = 0
                        except OSError:  # the child closed its stdin or died
                            sent = len(pending)
                        pending = pending[sent:]
                        if not pending:
                            selector.unregister(stdin_fd)
                            with contextlib.suppress(OSError):
                                stdin.close()
                        continue
                    try:
                        chunk = os.read(pipe.fileno(), 65536)
                    except OSError:
                        chunk = b""
                    if not chunk:
                        selector.unregister(pipe)
                        continue
                    try:
                        sink.feed(chunk)
                    except Exception:  # the caller's sink; nothing it raises may escape
                        sink_failed = sink_killed = True
                        signal_group(process, signal.SIGKILL)
                        break
                if sink_failed:
                    break
        except OSError as exc:  # EMFILE and the like: stop the child, report, never raise
            supervise_error = exc.strerror or "system error"
        finally:
            if selector is not None:
                selector.close()
            signal_group(process, signal.SIGKILL)
            code = running.reap()
            self._registry.remove(running)
            with contextlib.suppress(OSError):
                stdin.close()
            pipe.close()
            try:
                sink.close()
            except Exception:  # as above
                sink_failed = True
        if supervise_error is not None:
            return RunOutcome(
                succeeded=False,
                error_message=f"could not supervise the task process: {supervise_error}",
                killed=True,
                failure_code=FailureCode.INTERNAL,
            )
        outcome = _outcome_from(
            _read_record(result_path, task.output),
            code=code,
            stopped=stopped or sink_killed or running.detail is not None,
            timed_out=timed_out,
            timeout_seconds=task.timeout_seconds,
            stop=stop,
            secrets_to_hide=secrets_to_hide,
            memory_detail=running.detail,
        )
        if sink_failed and not outcome.succeeded:
            return RunOutcome(
                succeeded=False,
                error_message="the log sink failed",
                killed=sink_killed,
                retryable=False,
                failure_code=FailureCode.INTERNAL,
            )
        return outcome


_MAX_KILL_GRACE_SECONDS = 300
"""The longest grace the launcher accepts (`lakana_worker.launcher.requests.MAX_KILL_GRACE`)."""
DEADLINE_MARGIN_SECONDS = 900.0
"""Seconds past a task's timeout and kill grace after which the worker stops waiting for the
launcher: its image pull before the timer starts (up to 600 s), its wait margin (60 s), slack."""
STOP_RETRY_CAP_SECONDS = 5.0
"""The longest gap between two requests to stop a container the launcher has not stopped."""
_clock = time.monotonic
"""The clock the stop retries are timed by; a test replaces it with one it moves itself."""


def stop_retry_gap(gap: float, poll_seconds: float) -> float:
    """Say how long to wait before the next request to stop a container, after one failed.

    Parameters
    ----------
    gap
        The gap before the request that just failed; 0 after the first, so that the first
        retry comes at the next poll.
    poll_seconds
        How often the runner checks.

    Returns
    -------
    float
        The poll interval after the first retry, then double the last gap, at most
        `STOP_RETRY_CAP_SECONDS`.
    """
    return min(STOP_RETRY_CAP_SECONDS, gap * 2 if gap else poll_seconds)


def _deadline_message(seconds: float, margin: float) -> str:
    """Write the failure of a task whose launcher sent no result in time.

    Parameters
    ----------
    seconds
        How long the worker waited.
    margin
        The part of it past the timeout and the kill grace.

    Returns
    -------
    str
        The cause and the next step.
    """
    return (
        f"timed out: the launcher reported no end of the task's container within "
        f"{_seconds(seconds)} (its timeout, the kill grace and {_seconds(margin)} "
        "for the launcher). The worker stopped waiting and closed its connection, which makes "
        "the launcher remove the container; docker compose logs launcher has the reason"
    )


def memory_message(memory_mb: int) -> str:
    """Write the failure of a task its container's memory limit ended.

    Parameters
    ----------
    memory_mb
        `LAKANA_RUN__TASK_MEMORY` in MiB.

    Returns
    -------
    str
        The cause and the next step.
    """
    return (
        f"this task was ended because it used more than its container's memory limit "
        f"({memory_mb} MB, LAKANA_RUN__TASK_MEMORY). Use less data in the task (narrow its "
        "inputs with an upstream SQL asset), or ask the operator to raise LAKANA_RUN__TASK_MEMORY."
    )


def _exit_status(value: object) -> int | None:
    """Turn a container's exit status into the local runner's convention.

    Parameters
    ----------
    value
        Docker's ``StatusCode``; the container's init reports 128 plus the signal of a process
        killed by one.

    Returns
    -------
    int or None
        The status, negative for a signal; `None` when Docker gave none.
    """
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    return -(value - 128) if 128 < value < 160 else value


def _grace_seconds(kill_grace: float) -> int:
    """Turn `LAKANA_RUN__KILL_GRACE` into the whole seconds the launcher accepts.

    Parameters
    ----------
    kill_grace
        Seconds between SIGTERM and SIGKILL.

    Returns
    -------
    int
        Rounded up, from 0 to `_MAX_KILL_GRACE_SECONDS`.
    """
    if not math.isfinite(kill_grace):
        return _MAX_KILL_GRACE_SECONDS if kill_grace > 0 else 0
    return min(_MAX_KILL_GRACE_SECONDS, max(0, math.ceil(kill_grace)))


MISSING_ROOT = (
    "`{connection}` names ingestion root `{root}`, which this worker does not have. Set it in "
    "`LAKANA_LAUNCHER__INGESTION_ROOTS` (Docker runner) or `LAKANA_RUN__INGESTION_ROOTS` "
    "(local runner)."
)
"""The failure of a task whose connection names a root the worker cannot give it."""


def _own_refusal(refusal: str, task: PythonTask) -> str | None:
    """Turn a launcher refusal the asset's connections caused into its fixed text.

    Parameters
    ----------
    refusal
        The launcher's refusal.
    task
        The refused task, with its roots and the connections that named them.

    Returns
    -------
    str or None
        `EGRESS_MISSING` as it is; `MISSING_ROOT` for the first of the task's roots the
        launcher does not list; `None` for every other refusal (the operator's problem, given
        the generic text).
    """
    if refusal == EGRESS_MISSING:
        return EGRESS_MISSING
    allowed = allowed_roots(refusal)
    if allowed is None:
        return None
    for root in task.roots:
        if root not in allowed:
            return MISSING_ROOT.format(connection=task.root_users[root], root=root)
    return None


class DockerRunner:
    """Run a task in a container of its own, through the launcher.

    The container sees the attempt's code read-only at `/code`, its environment read-only at
    `/env`, a private `/work` (the attempt's ``task`` directory), with a trust bundle the
    attempt's ``trust`` directory read-only at `/trust`, and `/tmp`, and a network on
    which the server is the only other member. It runs as the worker's uid with no
    capabilities, a read-only root filesystem and the limits below, enforced by the runtime;
    when it ends, the launcher removes it with every process in it. The token travels on stdin
    and nowhere else, as with `LocalProcessRunner`; the result record the container writes is
    read with the same distrust.

    Parameters
    ----------
    client
        The launcher.
    owner
        This worker, for the launcher's labels.
    memory_mb
        `LAKANA_RUN__TASK_MEMORY` in MiB.
    cpus
        `LAKANA_RUN__TASK_CPUS`.
    pids
        `LAKANA_RUN__TASK_PIDS`.
    kill_grace
        Seconds between SIGTERM and SIGKILL on a stop (`LAKANA_RUN__KILL_GRACE`).
    poll_seconds
        How often the stop signal is checked.
    trust
        The worker's trust bundle as bytes (`lakana_worker.trust`); `None` without
        `LAKANA_RUN__CA_BUNDLE`.
    deadline_margin
        Seconds past the timeout and the kill grace after which the worker gives up on the
        launcher's answer and closes the connection, so the launcher removes the container.
    """

    def __init__(
        self,
        client: LauncherClient,
        *,
        owner: str,
        memory_mb: int,
        cpus: float,
        pids: int,
        kill_grace: float = 10.0,
        poll_seconds: float = 0.2,
        trust: bytes | None = None,
        deadline_margin: float = DEADLINE_MARGIN_SECONDS,
    ) -> None:
        self._client = client
        self._owner = owner
        self._memory_mb = memory_mb
        self._cpus = cpus
        self._pids = pids
        self._kill_grace = kill_grace
        self._poll_seconds = poll_seconds
        self._trust = trust
        self._deadline_margin = deadline_margin

    def _stop(self, ident: str) -> bool:
        """Ask the launcher to stop the container; a launcher that cannot is not an error here.

        The launcher's own timer and the deadline label still end the container.

        Parameters
        ----------
        ident
            The task's id.

        Returns
        -------
        bool
            Whether the launcher stopped a running container. `False` also before it has
            registered the run, so the caller asks again.
        """
        with contextlib.suppress(LauncherUnavailable, LauncherRefused):
            return self._client.stop(ident, self._owner)
        return False

    @property
    def isolated(self) -> bool:
        """Isolated: each task runs in a container of its own.

        Returns
        -------
        bool
            `True`.
        """
        return True

    def run(
        self, task: PythonTask, on_log: Callable[[bytes], None], stop: StopSignal
    ) -> RunOutcome:
        """Ask the launcher to run the task and supervise it to its end.

        Parameters
        ----------
        task
            What to run; needs `environment_key` and `python`.
        on_log
            Receives the container's redacted output.
        stop
            Set to have the container stopped (SIGTERM, SIGKILL after the grace).

        Returns
        -------
        RunOutcome
            As `LocalProcessRunner` reports it; "runner unavailable" (transient) when the
            launcher cannot be reached; an out-of-memory kill sets `StopReason.MEMORY`.

        Raises
        ------
        Exception
            Whatever the launcher client raised that is not one of its own failures: a defect,
            not a task's failure.
        """
        if (
            task.environment_key is None
            or task.python is None
            or not math.isfinite(task.timeout_seconds)
            or not task.attempt_dir.is_absolute()
        ):
            return RunOutcome(
                succeeded=False,
                error_message=_INVALID_TASK,
                retryable=False,
                failure_code=FailureCode.INTERNAL,
            )
        if not is_private_directory(task.attempt_dir, 0o077):
            return RunOutcome(
                succeeded=False,
                error_message="could not prepare the task directory: the attempt directory "
                "is not private (it must exist, be owned by the worker and have no group "
                "or other access)",
                failure_code=FailureCode.INTERNAL,
            )
        work = task.attempt_dir / "task"
        try:
            work.mkdir(mode=0o700, exist_ok=True)
        except OSError as exc:
            return RunOutcome(
                succeeded=False,
                error_message=f"could not prepare the task directory: {exc.strerror or exc}",
                failure_code=FailureCode.INTERNAL,
            )
        if not is_private_directory(work, 0o077):
            return RunOutcome(
                succeeded=False,
                error_message="could not prepare the task directory: the work directory is not "
                "private (a link, or a directory others can reach, where the worker makes its own)",
                failure_code=FailureCode.INTERNAL,
            )
        try:
            for directory in (
                work / "home",
                work / "tmp",
                work / "tmp" / POLARS_SPILL_DIRECTORY,
            ):
                directory.mkdir(mode=0o700, exist_ok=True)
            with contextlib.suppress(FileNotFoundError):
                (work / RESULT_FILE).unlink()
            if self._trust is not None:
                install_trust(self._trust, task.attempt_dir)
        except OSError as exc:
            return RunOutcome(
                succeeded=False,
                error_message=f"could not prepare the task directory: {exc.strerror or exc}",
                failure_code=FailureCode.INTERNAL,
            )
        secrets_to_hide = token_secrets(task.token) + list(task.hidden)
        if stop.is_set():  # a cancel, lost lease or drain before the container exists
            return _outcome_from(
                None,
                code=-1,
                stopped=True,
                timed_out=False,
                timeout_seconds=task.timeout_seconds,
                stop=stop,
                secrets_to_hide=secrets_to_hide,
            )
        ident = uuid.uuid4().hex
        timeout_seconds = max(1, math.ceil(task.timeout_seconds))
        grace_seconds = _grace_seconds(self._kill_grace)
        request: dict[str, object] = {
            "op": "run",
            "task": ident,
            "owner": self._owner,
            "python": task.python,
            "environment": task.environment_key,
            "attempt": task.attempt_dir.name,
            "memory_mb": self._memory_mb,
            "cpus": self._cpus,
            "pids": self._pids,
            "timeout_seconds": timeout_seconds,
            "kill_grace_seconds": grace_seconds,
            "stdin": task_description(
                task, result_path=f"/work/{RESULT_FILE}", work_dir="/work", parent_pid=None
            ).decode("utf-8"),
            "roots": list(task.roots),
            "trust": self._trust is not None,
            "egress": [[host, port] for host, port in task.egress],
            "relay": [
                {"listen": listen, "host": host, "port": port} for listen, host, port in task.relay
            ],
        }
        sink = OutputRedactor(secrets_to_hide, on_log)
        box: dict[str, object] = {}
        deadline = timeout_seconds + grace_seconds + self._deadline_margin

        def call() -> None:
            """Run the launcher call on its own thread, keeping what it returned or raised."""
            try:
                box["result"] = self._client.call(request, sink.feed, timeout=deadline)
            except Exception as exc:  # handed back to the supervising thread below
                box["error"] = exc

        thread = threading.Thread(target=call, name="lakana-docker-task", daemon=True)
        thread.start()
        stopped_by_us = False
        gap = 0.0
        next_stop = 0.0
        while thread.is_alive():
            thread.join(self._poll_seconds)
            now = _clock()
            if stop.is_set() and not stopped_by_us and thread.is_alive() and now >= next_stop:
                # The launcher answers False until it registered the run, and when Docker did
                # not stop the container: asked again, less often each time.
                stopped_by_us = self._stop(ident)
                next_stop = now + gap
                gap = stop_retry_gap(gap, self._poll_seconds)
        sink_failed = False
        try:
            sink.close()
        except Exception:  # the caller's sink; nothing it raises may escape
            sink_failed = True
        error = box.get("error")
        if isinstance(error, LogSinkFailed):
            self._stop(ident)
            return RunOutcome(
                succeeded=False,
                error_message="the log sink failed",
                killed=True,
                retryable=False,
                failure_code=FailureCode.INTERNAL,
            )
        if isinstance(error, LauncherTimedOut):
            self._stop(ident)
            outcome = _outcome_from(
                _read_record(work / RESULT_FILE, task.output),
                code=-1,
                stopped=True,
                timed_out=stop.reason is None,
                timeout_seconds=task.timeout_seconds,
                stop=stop,
                secrets_to_hide=secrets_to_hide,
            )
            if outcome.succeeded or stop.reason is not None:
                return outcome
            return RunOutcome(
                succeeded=False,
                error_message=_deadline_message(deadline, self._deadline_margin),
                killed=True,
                timed_out=True,
                failure_code=FailureCode.TIMEOUT,
            )
        if isinstance(error, LauncherUnavailable):
            return RunOutcome(
                succeeded=False, error_message=str(error), failure_code=FailureCode.NO_RUNTIME
            )
        if isinstance(error, LauncherRefused):
            own = _own_refusal(str(error), task)
            if own is not None:
                return RunOutcome(
                    succeeded=False,
                    error_message=own,
                    retryable=False,
                    failure_code=FailureCode.NO_RUNTIME,
                )
            next_step = (
                RELEASE_SKEW
                if release_skew(str(error))
                else "An operator checks LAKANA_LAUNCHER__IMAGES and the LAKANA_RUN__TASK_* "
                "settings against the launcher's ceilings, or the project's python: in lakana.yaml"
            )
            return RunOutcome(
                succeeded=False,
                error_message=f"the launcher refused the task: {error}. {next_step}",
                retryable=False,
                failure_code=FailureCode.NO_RUNTIME,
            )
        if isinstance(error, BaseException):
            self._stop(ident)
            raise error
        result = box.get("result")
        assert isinstance(result, dict)  # nosec B101 -- set by `call` when nothing was raised
        oom = result.get("oom_killed") is True
        if oom:
            stop.set(StopReason.MEMORY)
        timed_out = result.get("timed_out") is True
        stopped = result.get("stopped") is True or timed_out or oom
        code = _exit_status(result.get("exit"))
        if code is None and not stopped:
            return RunOutcome(
                succeeded=False,
                error_message="the task's container ended without an exit status from Docker; "
                "the launcher's log has the reason (docker compose logs launcher)",
                failure_code=FailureCode.INTERNAL,
            )
        outcome = _outcome_from(
            _read_record(work / RESULT_FILE, task.output),
            code=-1 if code is None else code,
            stopped=stopped,
            timed_out=timed_out,
            timeout_seconds=task.timeout_seconds,
            stop=stop,
            secrets_to_hide=secrets_to_hide,
            memory_detail=memory_message(self._memory_mb) if oom else None,
        )
        if sink_failed and not outcome.succeeded:
            return RunOutcome(
                succeeded=False,
                error_message="the log sink failed",
                retryable=False,
                failure_code=FailureCode.INTERNAL,
            )
        if not outcome.succeeded and stopped and not timed_out and not oom and stop.reason is None:
            return RunOutcome(
                succeeded=False,
                error_message=_LAUNCHER_STOPPED,
                killed=True,
                failure_code=FailureCode.INTERNAL,
            )
        return outcome


class CodeUnpackError(Exception):
    """A code tarball is unsafe or malformed; retrying cannot help (permanent).

    The text is bounded, escaped and free of the worker's paths, so it is safe to store.
    """


class CodeUnpackFailed(Exception):
    """The machine could not unpack the code (disk full, I/O error, ...); a retry may work.

    The text is bounded and free of the worker's paths, so it is safe to store.
    """


def _make_read_only(tree: Path) -> None:
    """Remove every write bit under `tree`.

    Parameters
    ----------
    tree
        The unpacked directory.
    """
    for directory, _dirs, files in os.walk(tree):
        for name in files:
            path = os.path.join(directory, name)
            executable = os.stat(path).st_mode & stat.S_IXUSR
            os.chmod(path, 0o555 if executable else 0o444)
        os.chmod(directory, 0o555)  # nosec B103 -- removes write bits; the attempt dir is 0700


def _show(name: str) -> str:
    """Escape and cut a member name for a message.

    Parameters
    ----------
    name
        A tar member name.

    Returns
    -------
    str
        An ASCII-only, bounded rendering.
    """
    return ascii(name)[:100]


def unpack_code(
    tarball: bytes, code_sha256: str, target: Path, *, max_bytes: int, max_members: int
) -> Path:
    """Unpack a project version's code tarball into a fresh, read-only directory of one attempt.

    Nothing is cached or shared: every attempt unpacks its own tree under its own
    private directory, so code that rewrites its tree (read-only is mode bits only,
    and the task runs as the worker's user) can only poison itself. The caller
    removes the whole attempt directory with `remove_private_tree` when the attempt
    ends. Only regular files and directories are accepted: no links of either kind, no devices, no
    FIFOs, no member that repeats an earlier name (compared folded, as a
    case-insensitive filesystem would). Every member counts toward the limit. The
    archive is inflated as a stream, so the limits stop a bomb before it is expanded.
    (Deploy refuses a repository with a symlink once it has a Python asset.)

    Parameters
    ----------
    tarball
        The gzipped tar as read from storage.
    code_sha256
        The project version's recorded digest of `tarball`.
    target
        The directory to create; it must not exist, and its parent must be a
        private directory (owned by the worker, no group or other access).
    max_bytes
        Largest summed size of the unpacked files.
    max_members
        Most members accepted.

    Returns
    -------
    Path
        `target`.

    Raises
    ------
    CodeUnpackError
        The digest is malformed or does not match, the archive is not a gzipped
        tar, a member is unsafe, of an unsupported kind or repeated, or a limit is
        exceeded. Permanent.
    CodeUnpackFailed
        The parent is not private, `target` exists or cannot be created, or the
        machine failed while unpacking (no space, I/O error, too many open files).
        Transient.
    """
    if _SHA256.match(code_sha256) is None:
        raise CodeUnpackError("the project version's code_sha256 is not a valid digest")
    if hashlib.sha256(tarball).hexdigest() != code_sha256:
        raise CodeUnpackError("the code tarball does not match its recorded code_sha256")
    if not is_private_directory(target.parent, 0o077):
        raise CodeUnpackFailed("the attempt directory is not private")
    try:
        target.mkdir(mode=0o700)
    except FileExistsError:
        raise CodeUnpackFailed("the code directory already exists") from None
    except OSError as exc:
        raise CodeUnpackFailed(
            f"could not create the code directory: {exc.strerror or 'filesystem error'}"
        ) from None
    total = count = 0
    seen: dict[str, bool] = {}

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
        CodeUnpackError
            For an unsafe name, a link or special file, a repeat, or too much data.
        """
        nonlocal total, count
        shown = _show(member.name)
        if (
            member.name.startswith("/")
            or ".." in Path(member.name).parts
            or HFS_IGNORABLE.search(member.name)
        ):
            raise CodeUnpackError(f"unsafe path in the code tarball: {shown}")
        if not (member.isreg() or member.isdir()):
            raise CodeUnpackError(
                f"links and special files are not allowed in a Python project: {shown}"
            )
        count += 1
        if count > max_members:
            raise CodeUnpackError(f"the code tarball has more than {max_members} files")
        name = fold_name(os.path.normpath(member.name))
        if name in seen and not (member.isdir() and seen[name]):
            raise CodeUnpackError(f"duplicate entry in the code tarball: {shown}")
        seen[name] = member.isdir()
        if member.isreg():
            total += member.size
            if total > max_bytes:
                raise CodeUnpackError(f"the code is larger than {max_bytes} bytes")
        return tarfile.data_filter(member, dest)

    done = False
    try:
        try:
            with tarfile.open(fileobj=io.BytesIO(tarball), mode="r|gz") as handle:
                handle.extractall(target, filter=accept)  # nosec B202 -- filtered above
        except OSError as exc:
            reason = exc.strerror or "filesystem error"
            if exc.errno in _ENVIRONMENT_ERRNOS:
                raise CodeUnpackFailed(f"could not unpack the code: {reason}") from None
            raise CodeUnpackError(f"could not unpack the code: {reason}") from None
        except (tarfile.TarError, ValueError, EOFError, zlib.error) as exc:
            raise CodeUnpackError(f"could not unpack the code: {type(exc).__name__}") from None
        try:
            _make_read_only(target)
        except OSError as exc:
            raise CodeUnpackFailed(
                f"could not unpack the code: {exc.strerror or 'filesystem error'}"
            ) from None
        done = True
    finally:
        if not done:
            remove_private_tree(target)
    return target
