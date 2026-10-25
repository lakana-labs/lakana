"""`DockerRunner`: the task through the launcher, and how each end maps to today's outcomes."""

from __future__ import annotations

import itertools
import json
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest
from fake_launcher import FakeLauncher, refusing

from lakana_core.runs.failures import FailureCode
from lakana_worker.launcher.requests import EGRESS_MISSING, EGRESS_TOKEN_TOO_LONG
from lakana_worker.launcher_client import (
    LauncherClient,
    LauncherTimedOut,
    LauncherUnavailable,
    LogSinkFailed,
)
from lakana_worker.runners import (
    DockerRunner,
    PythonTask,
    RunOutcome,
    StopReason,
    StopSignal,
    memory_message,
    stop_retry_gap,
)

TOKEN = "lakana_task_key01_" + "s" * 40


def _task(tmp_path: Path, **fields: object) -> PythonTask:
    """Build a task whose attempt directory is private.

    Parameters
    ----------
    tmp_path
        The test's directory.
    **fields
        Fields to replace.

    Returns
    -------
    PythonTask
        The task.
    """
    attempt = tmp_path / "attempt-abc_12"
    attempt.mkdir(mode=0o700, parents=True, exist_ok=True)
    attempt.chmod(0o700)
    values: dict[str, object] = {
        "code_dir": attempt / "code",
        "asset_path": "assets/gold/scores.py",
        "inputs": {},
        "table": "gold.scores",
        "environment": "dev",
        "server_url": "http://server:8000",
        "token": TOKEN,
        "timeout_seconds": 30.0,
        "venv": Path("/env"),
        "expected_snapshot_id": "none",
        "attempt_dir": attempt,
        "environment_key": "e" * 64,
        "python": "3.13",
    }
    values.update(fields)
    return PythonTask(**values)  # ty: ignore[invalid-argument-type]


def _runner(launcher: FakeLauncher, **options: object) -> DockerRunner:
    """Build the runner around the fake.

    Parameters
    ----------
    launcher
        The fake.
    **options
        Keyword options to replace.

    Returns
    -------
    DockerRunner
        The runner.
    """
    values: dict[str, object] = {
        "owner": "worker-1",
        "memory_mb": 2048,
        "cpus": 1.0,
        "pids": 256,
        "kill_grace": 3.0,
        "poll_seconds": 0.01,
    }
    values.update(options)
    return DockerRunner(launcher, **values)  # ty: ignore[invalid-argument-type]


def _writes(
    record: Mapping[str, object] | None,
    result: Mapping[str, object],
    logs: tuple[bytes, ...] = (),
) -> Callable[..., dict[str, object]]:
    """Build a run script that logs, writes the harness's record and ends.

    Parameters
    ----------
    record
        The result record to write into the work directory, or `None`.
    result
        The launcher's result.
    logs
        Output chunks.

    Returns
    -------
    callable
        The script.
    """

    def script(request: dict[str, Any], on_log: Callable[[bytes], None]) -> dict[str, object]:
        """Log, write the record, return the result.

        Parameters
        ----------
        request
            The request, with the test's ``_attempt_dir``.
        on_log
            The log sink.

        Returns
        -------
        dict
            The launcher's result.
        """
        for chunk in logs:
            on_log(chunk)
        if record is not None:
            work = Path(request["_attempt_dir"]) / "task"
            (work / "result.json").write_text(json.dumps(record))
        return dict(result)

    return script


def _run(
    tmp_path: Path,
    launcher: FakeLauncher,
    runner: dict[str, object] | None = None,
    /,
    **task: object,
) -> tuple[RunOutcome, list[bytes], StopSignal]:
    """Run one task, with the attempt directory passed to the script.

    Parameters
    ----------
    tmp_path
        The test's directory.
    launcher
        The fake.
    runner
        Options of the runner to replace.
    **task
        Fields of the task.

    Returns
    -------
    tuple
        The outcome, the log chunks and the stop signal.
    """
    job = _task(tmp_path, **task)
    original = launcher.call

    def call(
        request: dict[str, Any],
        on_log: Callable[[bytes], None] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, object]:
        """Pass the attempt directory to the script.

        Parameters
        ----------
        request
            The request.
        on_log
            The log sink.
        timeout
            The runner's deadline.

        Returns
        -------
        dict
            The script's result.
        """
        return original({**request, "_attempt_dir": str(job.attempt_dir)}, on_log, timeout=timeout)

    launcher.call = call  # ty: ignore[invalid-assignment]
    logs: list[bytes] = []
    stop = StopSignal()
    return _runner(launcher, **(runner or {})).run(job, logs.append, stop), logs, stop


_DONE = {"exit": 0, "oom_killed": False, "timed_out": False, "stopped": False}


def test_a_run_reads_the_record_from_the_work_directory(tmp_path: Path) -> None:
    """The harness writes `/work/result.json`, which is `<attempt>/task/result.json` here."""
    launcher = FakeLauncher(scripts={"run": _writes({"status": "succeeded", "rows": 3}, _DONE)})

    outcome, _, _ = _run(tmp_path, launcher)

    assert (outcome.succeeded, outcome.rows, outcome.failure_code) == (True, 3, None)


def test_a_stale_record_from_an_earlier_run_is_removed_first(tmp_path: Path) -> None:
    """A record left in the work directory must not count for this run."""
    work = tmp_path / "attempt-abc_12" / "task"
    work.mkdir(parents=True, mode=0o700)
    (tmp_path / "attempt-abc_12").chmod(0o700)
    (work / "result.json").write_text(json.dumps({"status": "succeeded", "rows": 3}))

    outcome, _, _ = _run(tmp_path, FakeLauncher(scripts={"run": _writes(None, _DONE)}))

    assert outcome.succeeded is False
    assert outcome.error_message == "the task process exited with status 0 without a result"


def test_the_request_carries_container_paths_and_no_parent_pid(tmp_path: Path) -> None:
    """The description is the local one with the container's paths; the token is on stdin only."""
    launcher = FakeLauncher(scripts={"run": _writes(None, _DONE)})

    _run(tmp_path, launcher)

    (request,) = launcher.requests
    description = json.loads(request["stdin"])
    assert (description["result_path"], description["work_dir"], description["parent_pid"]) == (
        "/work/result.json",
        "/work",
        None,
    )
    assert description["token"] == TOKEN
    assert TOKEN not in json.dumps({k: v for k, v in request.items() if k != "stdin"})
    assert {
        k: request[k]
        for k in (
            "op",
            "owner",
            "python",
            "environment",
            "attempt",
            "memory_mb",
            "cpus",
            "pids",
            "timeout_seconds",
            "kill_grace_seconds",
            "roots",
        )
    } == {
        "op": "run",
        "owner": "worker-1",
        "python": "3.13",
        "environment": "e" * 64,
        "attempt": "attempt-abc_12",
        "memory_mb": 2048,
        "cpus": 1.0,
        "pids": 256,
        "timeout_seconds": 30,
        "kill_grace_seconds": 3,
        "roots": [],
    }
    assert set(request) - {"_attempt_dir"} == {
        "op", "task", "owner", "python", "environment", "attempt", "memory_mb", "cpus",
        "pids", "timeout_seconds", "kill_grace_seconds", "stdin", "roots", "trust",
        "egress", "relay",
    }  # fmt: skip
    work = tmp_path / "attempt-abc_12" / "task"
    assert all((work / d).is_dir() for d in ("home", "tmp", "tmp/polars-spill"))
    assert oct(work.stat().st_mode & 0o777) == "0o700"


@pytest.mark.parametrize(
    ("timeout", "grace", "sent_timeout", "sent_grace"),
    [
        (0.2, -1.0, 1, 0),
        (30.5, 2.5, 31, 3),
        (10.0, 1000.0, 10, 300),
        (10.0, float("inf"), 10, 300),
        (10.0, float("nan"), 10, 0),
    ],
)
def test_the_limits_are_whole_seconds_within_the_launcher_rules(
    tmp_path: Path, timeout: float, grace: float, sent_timeout: int, sent_grace: int
) -> None:
    """The launcher takes integers: a timeout of at least 1, a grace from 0 to 300.

    Parameters
    ----------
    tmp_path
        The test's directory.
    timeout
        The asset's timeout.
    grace
        `LAKANA_RUN__KILL_GRACE`.
    sent_timeout
        What the launcher gets.
    sent_grace
        What the launcher gets.
    """
    launcher = FakeLauncher(scripts={"run": _writes(None, _DONE)})

    _run(tmp_path, launcher, {"kill_grace": grace}, timeout_seconds=timeout)

    request = launcher.requests[0]
    assert (request["timeout_seconds"], request["kill_grace_seconds"]) == (sent_timeout, sent_grace)


def test_the_ingestion_roots_are_sent(tmp_path: Path) -> None:
    """The launcher mounts only roots on its own allow-list; the worker names the task's."""
    launcher = FakeLauncher(scripts={"run": _writes(None, _DONE)})

    _run(tmp_path, launcher, roots=("landing",), root_users={"landing": "exports"})

    assert launcher.requests[0]["roots"] == ["landing"]


def test_egress_and_relay_are_sent_from_the_task(tmp_path: Path) -> None:
    """Each task gets the proxy pairs and relay routes of its own connections."""
    launcher = FakeLauncher(scripts={"run": _writes(None, _DONE)})

    _run(
        tmp_path,
        launcher,
        egress=(("api.example.com", 443),),
        relay=((15001, "db.example.com", 5432),),
    )

    request = launcher.requests[0]
    assert request["egress"] == [["api.example.com", 443]]
    assert request["relay"] == [{"listen": 15001, "host": "db.example.com", "port": 5432}]


_ROOT_TEXT = (
    "`exports` names ingestion root `shared`, which this worker does not have. Set it in "
    "`LAKANA_LAUNCHER__INGESTION_ROOTS` (Docker runner) or `LAKANA_RUN__INGESTION_ROOTS` "
    "(local runner)."
)


@pytest.mark.parametrize("allowed", ["landing", "none is set"], ids=["another-root", "no-root"])
def test_a_roots_refusal_becomes_the_fixed_root_text(tmp_path: Path, allowed: str) -> None:
    """The launcher lacks a root the task needs: the text names the connection and both settings.

    Parameters
    ----------
    tmp_path
        The test's directory.
    allowed
        What the launcher's refusal lists.
    """
    launcher = FakeLauncher(
        scripts={
            "run": refusing(
                f"roots: a list of distinct names from LAKANA_LAUNCHER__INGESTION_ROOTS ({allowed})"
            )
        }
    )

    outcome, _, _ = _run(
        tmp_path,
        launcher,
        roots=("landing", "shared") if allowed == "landing" else ("shared",),
        root_users={"landing": "drops", "shared": "exports"},
    )

    assert outcome.error_message == _ROOT_TEXT
    assert outcome.retryable is False
    assert outcome.failure_code is FailureCode.NO_RUNTIME


def test_a_roots_refusal_naming_every_root_keeps_the_generic_text(tmp_path: Path) -> None:
    """A refusal the worker cannot pin on one root (too many, say) is an operator's problem."""
    launcher = FakeLauncher(
        scripts={
            "run": refusing(
                "roots: a list of distinct names from LAKANA_LAUNCHER__INGESTION_ROOTS (shared)"
            )
        }
    )

    outcome, _, _ = _run(tmp_path, launcher, roots=("shared",), root_users={"shared": "exports"})

    assert outcome.error_message is not None
    assert outcome.error_message.startswith("the launcher refused the task: roots:")
    assert outcome.retryable is False


def test_an_egress_missing_refusal_is_its_own_text(tmp_path: Path) -> None:
    """No operator suffix: the text already says to start the egress service."""
    launcher = FakeLauncher(scripts={"run": refusing(EGRESS_MISSING)})

    outcome, _, _ = _run(tmp_path, launcher, egress=(("api.example.com", 443),))

    assert outcome.error_message == EGRESS_MISSING
    assert outcome.retryable is False
    assert outcome.failure_code is FailureCode.NO_RUNTIME


def test_a_token_too_long_refusal_names_the_asset_and_the_split(tmp_path: Path) -> None:
    """Not the operator's settings: the asset's host names do not fit in one task's token."""
    refusal = (
        f"{EGRESS_TOKEN_TOO_LONG} (8858 characters of payload, at most 8192); grant fewer or "
        "shorter host names"
    )
    launcher = FakeLauncher(scripts={"run": refusing(refusal)})

    outcome, _, _ = _run(tmp_path, launcher, egress=(("api.example.com", 443),))

    assert outcome.error_message == (
        "asset `gold.scores` declares more or longer host names than one task's egress token "
        "can carry (8858 characters of payload, at most 8192); split its connections across "
        "several assets"
    )
    assert outcome.retryable is False
    assert outcome.failure_code is FailureCode.NO_RUNTIME


def test_a_probe_refusal_of_a_root_keeps_the_generic_text(tmp_path: Path) -> None:
    """A root the launcher has but refuses to mount is an operator problem, not the asset's."""
    reason = "ingestion root 'shared' holds a socket, pipe or device node; fix it"
    launcher = FakeLauncher(scripts={"run": refusing(reason)})

    outcome, _, _ = _run(tmp_path, launcher, roots=("shared",), root_users={"shared": "exports"})

    assert outcome.error_message is not None
    assert outcome.error_message.startswith(f"the launcher refused the task: {reason}. ")


def test_an_out_of_memory_kill_is_the_memory_stop_reason(tmp_path: Path) -> None:
    """Docker's `OOMKilled` replaces the worker's watchdog for task memory."""
    result = {"exit": 137, "oom_killed": True, "timed_out": False, "stopped": False}
    launcher = FakeLauncher(scripts={"run": _writes(None, result)})

    outcome, _, stop = _run(tmp_path, launcher)

    assert stop.reason is StopReason.MEMORY
    assert (outcome.succeeded, outcome.killed, outcome.retryable) == (False, True, True)
    assert outcome.error_message == memory_message(2048)
    assert outcome.failure_code is FailureCode.MEMORY
    assert "LAKANA_RUN__TASK_MEMORY" in memory_message(2048)


def test_a_timeout_is_reported_as_one(tmp_path: Path) -> None:
    """The launcher's timer stopped the container."""
    result = {"exit": 137, "oom_killed": False, "timed_out": True, "stopped": False}

    outcome, _, _ = _run(tmp_path, FakeLauncher(scripts={"run": _writes(None, result)}))

    assert (outcome.timed_out, outcome.error_message) == (True, "timed out after 30s")
    assert outcome.failure_code is FailureCode.TIMEOUT


def test_a_success_recorded_before_a_timeout_still_succeeds(tmp_path: Path) -> None:
    """The record is written after the upload returned; a hang afterwards changes nothing."""
    result = {"exit": 137, "oom_killed": False, "timed_out": True, "stopped": False}
    record = {"status": "succeeded", "rows": 1}

    outcome, _, _ = _run(tmp_path, FakeLauncher(scripts={"run": _writes(record, result)}))

    assert outcome.succeeded is True


def test_a_stop_asks_the_launcher_to_stop_the_container(tmp_path: Path) -> None:
    """A cancel, a lost lease or the drain reaches the container through `stop`."""
    launcher = FakeLauncher()

    def blocks(request: dict[str, Any], on_log: Callable[[bytes], None]) -> dict[str, object]:
        """Run until stopped.

        Parameters
        ----------
        request
            The request.
        on_log
            The log sink.

        Returns
        -------
        dict
            A stopped container.
        """
        launcher.released.wait(10)
        return {"exit": 137, "oom_killed": False, "timed_out": False, "stopped": True}

    launcher.scripts["run"] = blocks
    job = _task(tmp_path)
    stop = StopSignal()
    threading.Timer(0.1, stop.set, (StopReason.CANCELLED,)).start()

    outcome = _runner(launcher).run(job, lambda chunk: None, stop)

    assert launcher.stopped == [launcher.requests[0]["task"]]
    assert launcher.owners == [launcher.requests[0]["owner"]], "#305: a stop names its owner"
    assert outcome.error_message == "stopped: cancelled"
    assert outcome.failure_code is FailureCode.CANCELLED


def test_a_stop_the_launcher_cannot_take_is_not_raised(tmp_path: Path) -> None:
    """The launcher's own deadline still ends the container; the run waits for its answer."""
    launcher = FakeLauncher()
    stop = StopSignal()

    def down(ident: str, owner: str) -> bool:
        """Fail like an unreachable launcher.

        Parameters
        ----------
        ident
            The id.
        owner
            The worker that asks.

        Raises
        ------
        LauncherUnavailable
            Always.
        """
        raise LauncherUnavailable("runner unavailable: gone")

    def slow(request: dict[str, Any], on_log: Callable[[bytes], None]) -> dict[str, object]:
        """End stopped after a moment.

        Parameters
        ----------
        request
            The request.
        on_log
            The log sink.

        Returns
        -------
        dict
            A stopped container.
        """
        stop.set(StopReason.DRAIN)
        threading.Event().wait(0.1)
        return {"exit": 137, "stopped": True}

    launcher.stop = down  # ty: ignore[invalid-assignment]
    launcher.scripts["run"] = slow

    outcome = _runner(launcher).run(_task(tmp_path), lambda chunk: None, stop)

    assert outcome.error_message == "stopped: worker draining"
    assert outcome.failure_code is FailureCode.WORKER_LOST


def test_a_cancel_wins_over_an_out_of_memory_kill_after_it(tmp_path: Path) -> None:
    """The first reason wins, as with the watchdog."""
    stop = StopSignal()

    def cancelled(request: dict[str, Any], on_log: Callable[[bytes], None]) -> dict[str, object]:
        """Be cancelled while running, then end out of memory.

        Parameters
        ----------
        request
            The request.
        on_log
            The log sink.

        Returns
        -------
        dict
            An out-of-memory end.
        """
        stop.set(StopReason.CANCELLED)
        return {"exit": 137, "oom_killed": True, "stopped": True}

    launcher = FakeLauncher(scripts={"run": cancelled})
    job = _task(tmp_path)

    _runner(launcher).run(job, lambda chunk: None, stop)

    assert stop.reason is StopReason.CANCELLED


def test_an_unreachable_launcher_is_runner_unavailable_and_transient(tmp_path: Path) -> None:
    """The message names the service and the setting (`unavailable_message`)."""
    launcher = FakeLauncher()

    outcome, _, _ = _run(tmp_path, launcher)

    assert outcome.succeeded is False and outcome.retryable is True
    assert outcome.error_message == "runner unavailable: no script for run"
    assert outcome.failure_code is FailureCode.NO_RUNTIME


def test_a_refused_run_is_permanent_and_names_the_settings(tmp_path: Path) -> None:
    """A refusal is a configuration mismatch between worker and launcher; a retry cannot help."""
    launcher = FakeLauncher(
        scripts={"run": refusing("python must be one of 3.14 (LAKANA_LAUNCHER__IMAGES)")}
    )

    outcome, _, _ = _run(tmp_path, launcher)

    assert outcome.retryable is False
    assert outcome.failure_code is FailureCode.NO_RUNTIME
    assert outcome.error_message == (
        "the launcher refused the task: python must be one of 3.14 (LAKANA_LAUNCHER__IMAGES). "
        "An operator checks LAKANA_LAUNCHER__IMAGES and the LAKANA_RUN__TASK_* settings against "
        "the launcher's ceilings, or the project's python: in lakana.yaml"
    )


def test_a_run_refused_for_its_fields_names_the_release_skew(tmp_path: Path) -> None:
    """A worker and a launcher of different releases disagree on the fields, not the settings."""
    launcher = FakeLauncher(
        scripts={"run": refusing("run takes exactly the fields attempt, cpus, environment")}
    )

    outcome, _, _ = _run(tmp_path, launcher)

    assert outcome.retryable is False
    assert outcome.failure_code is FailureCode.NO_RUNTIME
    assert outcome.error_message is not None
    assert "different releases" in outcome.error_message
    assert "upgrade" in outcome.error_message
    assert "LAKANA_RUN__TASK_" not in outcome.error_message


def test_a_task_token_split_across_log_lines_is_redacted(tmp_path: Path) -> None:
    """Review focus: Docker frames and base64 lines cut the token anywhere."""
    middle = len(TOKEN) // 2
    chunks = (b"token=" + TOKEN[:middle].encode(), TOKEN[middle:].encode() + b"\n")
    launcher = FakeLauncher(scripts={"run": _writes(None, _DONE, chunks)})

    _, logs, _ = _run(tmp_path, launcher)

    joined = b"".join(logs)
    assert TOKEN.encode() not in joined and b"s" * 40 not in joined
    assert joined.startswith(b"token=")


def test_a_declared_connections_secret_split_across_log_lines_is_redacted(
    tmp_path: Path,
) -> None:
    """`PythonTask.hidden` is masked in the container's log as the token is."""
    chunks = (b"password=conn-s3c", b"r3t-value\n")
    launcher = FakeLauncher(scripts={"run": _writes(None, _DONE, chunks)})

    _, logs, _ = _run(tmp_path, launcher, None, hidden=("conn-s3cr3t-value",))

    assert b"".join(logs) == b"password=***\n"


def test_a_failing_log_sink_stops_the_container_and_is_permanent(tmp_path: Path) -> None:
    """The client reports the sink's failure; the runner stops the container."""
    launcher = FakeLauncher()

    def sink_fails(request: dict[str, Any], on_log: Callable[[bytes], None]) -> dict[str, object]:
        """Fail as the client does when the sink raised.

        Parameters
        ----------
        request
            The request.
        on_log
            The log sink.

        Raises
        ------
        LogSinkFailed
            Always.
        """
        raise LogSinkFailed("the log sink failed")

    launcher.scripts["run"] = sink_fails

    outcome, _, _ = _run(tmp_path, launcher)

    assert (outcome.error_message, outcome.retryable) == ("the log sink failed", False)
    assert outcome.failure_code is FailureCode.INTERNAL
    assert launcher.stopped == [launcher.requests[0]["task"]]


def test_a_sink_failing_on_the_last_flush_fails_an_unsuccessful_run(tmp_path: Path) -> None:
    """The redactor flushes what it kept when the run ends; that flush may fail too."""
    launcher = FakeLauncher(scripts={"run": _writes(None, {"exit": 1}, (b"no newline",))})

    def broken(chunk: bytes) -> None:
        """Fail on every write.

        Parameters
        ----------
        chunk
            The output.

        Raises
        ------
        OSError
            Always.
        """
        raise OSError("disk full")

    outcome = _runner(launcher).run(_task(tmp_path), broken, StopSignal())

    assert (outcome.error_message, outcome.retryable) == ("the log sink failed", False)
    assert outcome.failure_code is FailureCode.INTERNAL


@pytest.mark.parametrize(
    "fields",
    [
        {"environment_key": None},
        {"python": None},
        {"timeout_seconds": float("inf")},
        {"attempt_dir": Path("relative/attempt-abc_12")},
    ],
    ids=["no-key", "no-python", "infinite-timeout", "relative-dir"],
)
def test_an_invalid_task_is_a_worker_defect(tmp_path: Path, fields: dict[str, object]) -> None:
    """Nothing is sent: the worker built the task wrong.

    Parameters
    ----------
    tmp_path
        The test's directory.
    fields
        The wrong fields.
    """
    launcher = FakeLauncher()

    outcome, _, _ = _run(tmp_path, launcher, **fields)

    assert launcher.requests == []
    assert outcome.retryable is False and "worker defect" in (outcome.error_message or "")
    assert outcome.failure_code is FailureCode.INTERNAL


def test_an_attempt_directory_others_can_read_is_refused(tmp_path: Path) -> None:
    """The work directory holds the result record; nobody else may plant one."""
    launcher = FakeLauncher()
    job = _task(tmp_path)
    job.attempt_dir.chmod(0o755)

    outcome = _runner(launcher).run(job, lambda chunk: None, StopSignal())

    assert launcher.requests == []
    assert "not private" in (outcome.error_message or "")
    assert outcome.failure_code is FailureCode.INTERNAL


def test_a_work_directory_that_is_a_link_is_refused(tmp_path: Path) -> None:
    """The container writes into `<attempt>/task`; a link there would point it elsewhere."""
    launcher = FakeLauncher()
    job = _task(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    (job.attempt_dir / "task").symlink_to(elsewhere)

    outcome = _runner(launcher).run(job, lambda chunk: None, StopSignal())

    assert launcher.requests == []
    assert "work directory is not private" in (outcome.error_message or "")
    assert outcome.failure_code is FailureCode.INTERNAL
    assert list(elsewhere.iterdir()) == []


def test_a_work_directory_that_cannot_be_made_fails_before_the_launcher(tmp_path: Path) -> None:
    """A file where the work directory belongs is reported, not raised."""
    launcher = FakeLauncher()
    job = _task(tmp_path)
    (job.attempt_dir / "task").write_text("in the way")
    second = _task(tmp_path / "second")
    (second.attempt_dir / "task").mkdir(mode=0o700)
    (second.attempt_dir / "task" / "home").write_text("in the way")

    for attempt in (job, second):
        outcome = _runner(launcher).run(attempt, lambda chunk: None, StopSignal())

        assert (outcome.error_message or "").startswith("could not prepare the task directory")
        assert outcome.failure_code is FailureCode.INTERNAL
    assert launcher.requests == []


def test_an_exit_by_signal_reads_as_the_signal(tmp_path: Path) -> None:
    """The container's init reports 128 + the signal; the message names the signal."""
    result = {"exit": 137, "oom_killed": False, "timed_out": False, "stopped": False}

    outcome, _, _ = _run(tmp_path, FakeLauncher(scripts={"run": _writes(None, result)}))

    assert outcome.error_message == "the task process was killed by signal 9"


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (None, "ended without an exit status from Docker"),
        (True, "ended without an exit status from Docker"),
        (2, "the harness refuses a task description"),
        (160, "exited with status 160"),
        (128, "exited with status 128"),
    ],
)
def test_an_exit_status_docker_gave_oddly_is_read_safely(
    tmp_path: Path, value: object, message: str
) -> None:
    """No status, a boolean, or a status outside the signal range is a plain status.

    Parameters
    ----------
    tmp_path
        The test's directory.
    value
        Docker's status.
    message
        Part of the failure's text.
    """
    launcher = FakeLauncher(scripts={"run": _writes(None, {"exit": value})})

    outcome, _, _ = _run(tmp_path, launcher)

    assert message in (outcome.error_message or "")
    assert outcome.succeeded is False and outcome.failure_code is not None


def test_a_stopped_container_without_a_status_reads_as_stopped(tmp_path: Path) -> None:
    """A stop explains the end; Docker's missing status does not matter then."""
    launcher = FakeLauncher(scripts={"run": _writes(None, {"stopped": True})})

    outcome, _, _ = _run(tmp_path, launcher)

    assert (outcome.error_message or "").startswith("the launcher stopped the task's container")
    assert "docker compose logs launcher" in (outcome.error_message or "")
    assert (outcome.killed, outcome.retryable, outcome.failure_code) == (
        True,
        True,
        FailureCode.INTERNAL,
    )


def test_an_unavailable_launcher_raised_by_the_client_is_kept_as_its_text(tmp_path: Path) -> None:
    """`LauncherUnavailable`'s text is already the task's message."""
    launcher = FakeLauncher()

    def down(request: dict[str, Any], on_log: Callable[[bytes], None]) -> dict[str, object]:
        """Fail like a missing socket.

        Parameters
        ----------
        request
            The request.
        on_log
            The log sink.

        Raises
        ------
        LauncherUnavailable
            Always.
        """
        raise LauncherUnavailable(
            "runner unavailable: the launcher's socket cannot be reached (No such file or "
            "directory). Check ..."
        )

    launcher.scripts["run"] = down

    outcome, _, _ = _run(tmp_path, launcher)

    assert (outcome.error_message or "").startswith(
        "runner unavailable: the launcher's socket cannot be reached"
    )


def test_an_unexpected_error_of_the_client_is_raised(tmp_path: Path) -> None:
    """A bug is not turned into a task failure that hides it."""
    launcher = FakeLauncher()

    def bug(request: dict[str, Any], on_log: Callable[[bytes], None]) -> dict[str, object]:
        """Fail like a bug.

        Parameters
        ----------
        request
            The request.
        on_log
            The log sink.

        Raises
        ------
        KeyError
            Always.
        """
        raise KeyError("bug")

    launcher.scripts["run"] = bug

    with pytest.raises(KeyError):
        _run(tmp_path, launcher)


def test_a_stop_set_before_the_run_sends_nothing(tmp_path: Path) -> None:
    """A cancel during the environment build must not start a container."""
    launcher = FakeLauncher(scripts={"run": _writes(None, _DONE)})
    stop = StopSignal()
    stop.set(StopReason.CANCELLED)

    outcome = _runner(launcher).run(_task(tmp_path), lambda chunk: None, stop)

    assert launcher.requests == []
    assert (outcome.error_message, outcome.failure_code) == (
        "stopped: cancelled",
        FailureCode.CANCELLED,
    )


def test_a_stop_the_launcher_has_not_registered_yet_is_asked_again(tmp_path: Path) -> None:
    """Before the launcher registers the run, `stop` answers False; the runner keeps asking."""
    launcher = FakeLauncher()
    answers = [False, False, True]
    asked: list[str] = []

    def late(ident: str, owner: str) -> bool:
        """Answer not running twice, then stop the container.

        Parameters
        ----------
        ident
            The task's id.
        owner
            The worker that asks.

        Returns
        -------
        bool
            Whether it was running.
        """
        asked.append(ident)
        answer = answers.pop(0) if answers else True
        if answer:
            launcher.released.set()
        return answer

    def blocks(request: dict[str, Any], on_log: Callable[[bytes], None]) -> dict[str, object]:
        """Run until stopped.

        Parameters
        ----------
        request
            The request.
        on_log
            The log sink.

        Returns
        -------
        dict
            A stopped container.
        """
        launcher.released.wait(10)
        return {"exit": 137, "stopped": True}

    launcher.stop = late  # ty: ignore[invalid-assignment]
    launcher.scripts["run"] = blocks
    stop = StopSignal()
    threading.Timer(0.05, stop.set, (StopReason.LEASE_LOST,)).start()

    outcome = _runner(launcher).run(_task(tmp_path), lambda chunk: None, stop)

    assert len(asked) == 3 and set(asked) == {launcher.requests[0]["task"]}
    assert outcome.failure_code is FailureCode.WORKER_LOST


def test_an_unexpected_error_stops_the_container_before_it_is_raised(tmp_path: Path) -> None:
    """A defect must not leave the container running."""
    launcher = FakeLauncher()

    def bug(request: dict[str, Any], on_log: Callable[[bytes], None]) -> dict[str, object]:
        """Fail like a bug.

        Parameters
        ----------
        request
            The request.
        on_log
            The log sink.

        Raises
        ------
        KeyError
            Always.
        """
        raise KeyError("bug")

    launcher.scripts["run"] = bug

    with pytest.raises(KeyError):
        _run(tmp_path, launcher)
    assert launcher.stopped == [launcher.requests[0]["task"]]


def test_the_invalid_task_message_names_every_cause(tmp_path: Path) -> None:
    """A missing environment key or Python is named, not only the timeout and the directory."""
    outcome, _, _ = _run(tmp_path, FakeLauncher(), python=None)

    assert "environment key" in (outcome.error_message or "")
    assert "Python version" in (outcome.error_message or "")


def test_the_launcher_call_has_a_deadline_past_the_launchers_own_end(tmp_path: Path) -> None:
    """Timeout, kill grace and a margin for the launcher's image pull and wait: then give up."""
    launcher = FakeLauncher(scripts={"run": _writes({"status": "succeeded"}, _DONE)})

    _run(tmp_path, launcher, {"deadline_margin": 7.5}, timeout_seconds=29.5)

    assert launcher.timeouts == [30 + 3 + 7.5]


def test_the_default_deadline_margin_covers_an_image_pull() -> None:
    """The launcher pulls a missing image (up to 600 s) before its own timer starts."""
    runner = DockerRunner(LauncherClient(Path("l.sock")), owner="w", memory_mb=64, cpus=1, pids=16)

    assert runner._deadline_margin >= 600 + 60


def _hangs(launcher: FakeLauncher) -> Callable[..., dict[str, object]]:
    """Build a run script that behaves like the client reaching its deadline.

    Parameters
    ----------
    launcher
        The fake, whose recorded deadline the script checks.

    Returns
    -------
    callable
        The script.
    """

    def script(request: dict[str, Any], on_log: Callable[[bytes], None]) -> dict[str, object]:
        """Raise what the client raises when no result came in time.

        Parameters
        ----------
        request
            The request.
        on_log
            The log sink.

        Raises
        ------
        LauncherTimedOut
            Always.
        """
        assert launcher.timeouts[-1] is not None
        raise LauncherTimedOut("the launcher sent no result within 993s; the connection is closed")

    return script


def test_a_launcher_that_never_answers_ends_the_run_at_the_worker_deadline(tmp_path: Path) -> None:
    """Before, only the launcher's timer and reaper ended an overrunning container."""
    launcher = FakeLauncher()
    launcher.scripts["run"] = _hangs(launcher)

    outcome, _, _ = _run(tmp_path, launcher)

    assert (outcome.succeeded, outcome.timed_out, outcome.killed) == (False, True, True)
    assert outcome.failure_code is FailureCode.TIMEOUT
    message = outcome.error_message or ""
    assert "launcher" in message and "docker compose logs launcher" in message
    assert launcher.stopped == [launcher.requests[0]["task"]]


def test_a_cancel_the_launcher_never_confirmed_is_still_a_cancel(tmp_path: Path) -> None:
    """The deadline does not turn a stop the worker asked for into a timeout."""
    launcher = FakeLauncher()
    hangs = _hangs(launcher)
    stop = StopSignal()

    def cancelled(request: dict[str, Any], on_log: Callable[[bytes], None]) -> dict[str, object]:
        """Cancel, then reach the deadline.

        Parameters
        ----------
        request
            The request.
        on_log
            The log sink.

        Returns
        -------
        dict
            Never; the deadline is raised.
        """
        stop.set(StopReason.CANCELLED)
        return hangs(request, on_log)

    launcher.scripts["run"] = cancelled

    outcome = _runner(launcher).run(_task(tmp_path), lambda chunk: None, stop)

    assert (outcome.error_message, outcome.failure_code) == (
        "stopped: cancelled",
        FailureCode.CANCELLED,
    )


def test_a_success_recorded_before_the_launcher_hung_still_succeeds(tmp_path: Path) -> None:
    """The record is read as after a timeout: written after the upload returned."""
    launcher = FakeLauncher()
    hangs = _hangs(launcher)

    def recorded(request: dict[str, Any], on_log: Callable[[bytes], None]) -> dict[str, object]:
        """Write a success record, then reach the deadline.

        Parameters
        ----------
        request
            The request, with the test's ``_attempt_dir``.
        on_log
            The log sink.

        Returns
        -------
        dict
            Never; the deadline is raised.
        """
        work = Path(request["_attempt_dir"]) / "task"
        (work / "result.json").write_text(json.dumps({"status": "succeeded", "rows": 1}))
        return hangs(request, on_log)

    launcher.scripts["run"] = recorded

    outcome, _, _ = _run(tmp_path, launcher)

    assert outcome.succeeded is True


def test_a_stop_the_launcher_keeps_refusing_is_asked_again_less_and_less_often(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first retry is immediate, then the gap doubles: a stuck launcher is not flooded.

    The runner's clock is a fake that moves one poll per reading, so the gaps are counted in
    polls whatever the machine's load.
    """
    tick = 1 / 64  # exact in binary: the gaps add up without rounding
    now = [0.0]
    launcher = FakeLauncher()
    asked: list[float] = []
    enough = threading.Event()
    stop = StopSignal()

    def clock() -> float:
        """Move the fake clock on by one poll.

        Returns
        -------
        float
            The new time.
        """
        now[0] += tick
        return now[0]

    def refuses(ident: str, owner: str) -> bool:
        """Answer that Docker did not stop the container.

        Parameters
        ----------
        ident
            The task's id.
        owner
            The worker that asks.

        Returns
        -------
        bool
            `False`.
        """
        asked.append(now[0])
        if len(asked) >= 7:
            enough.set()
        return False

    def runs_until_asked_enough(
        request: dict[str, Any], on_log: Callable[[bytes], None]
    ) -> dict[str, object]:
        """Ask for a stop, then end by itself once the runner asked seven times.

        Parameters
        ----------
        request
            The request.
        on_log
            The log sink.

        Returns
        -------
        dict
            A stopped container.
        """
        stop.set(StopReason.CANCELLED)
        enough.wait(30)
        return {"exit": 137, "stopped": True}

    monkeypatch.setattr("lakana_worker.runners._clock", clock)
    launcher.stop = refuses  # ty: ignore[invalid-assignment]
    launcher.scripts["run"] = runs_until_asked_enough

    _runner(launcher, poll_seconds=tick).run(_task(tmp_path), lambda chunk: None, stop)

    polls = [round((later - earlier) / tick) for earlier, later in itertools.pairwise(asked[:7])]
    assert polls == [1, 1, 2, 4, 8, 16]


def test_the_stop_retry_gap_doubles_from_the_poll_and_stops_growing_at_five_seconds() -> None:
    """0 (the first retry is immediate), then the poll interval, doubling, at most 5 s."""
    gaps = [0.0]
    for _ in range(7):
        gaps.append(stop_retry_gap(gaps[-1], 0.2))

    assert gaps == [0.0, 0.2, 0.4, 0.8, 1.6, 3.2, 5.0, 5.0]


def test_with_a_trust_bundle_the_attempt_gets_a_copy_and_the_run_asks_for_the_mount(
    tmp_path: Path,
) -> None:
    """`<attempt>/trust/trust.pem` (0644 in a 0755 directory, a copy) and `trust: true`."""
    bundle = b"-----BEGIN CERTIFICATE-----\nAA==\n-----END CERTIFICATE-----\n"
    launcher = FakeLauncher(scripts={"run": _writes({"status": "succeeded", "rows": 1}, _DONE)})
    outcome, _, _ = _run(tmp_path, launcher, {"trust": bundle})
    assert outcome.succeeded, outcome.error_message
    assert launcher.requests[0]["trust"] is True
    copied = tmp_path / "attempt-abc_12" / "trust" / "trust.pem"
    assert copied.read_bytes() == bundle and not copied.is_symlink()
    assert oct(copied.stat().st_mode & 0o777) == "0o644"
    assert oct(copied.parent.stat().st_mode & 0o777) == "0o755"
    assert oct((tmp_path / "attempt-abc_12").stat().st_mode & 0o777) == "0o700"


def test_without_a_trust_bundle_there_is_no_trust_directory(tmp_path: Path) -> None:
    """`trust: false`, and nothing written: today's run."""
    launcher = FakeLauncher(scripts={"run": _writes({"status": "succeeded", "rows": 1}, _DONE)})
    _run(tmp_path, launcher)
    assert launcher.requests[0]["trust"] is False
    assert not (tmp_path / "attempt-abc_12" / "trust").exists()


def test_a_planted_trust_link_fails_the_attempt_before_the_launcher(tmp_path: Path) -> None:
    """A link at `<attempt>/trust` is refused as an unprepared directory; nothing is sent."""
    bundle = b"-----BEGIN CERTIFICATE-----\nAA==\n-----END CERTIFICATE-----\n"
    launcher = FakeLauncher(scripts={"run": _writes({"status": "succeeded", "rows": 1}, _DONE)})
    attempt = tmp_path / "attempt-abc_12"
    attempt.mkdir(mode=0o700)
    (attempt / "trust").symlink_to(tmp_path)
    outcome, _, _ = _run(tmp_path, launcher, {"trust": bundle})
    assert not outcome.succeeded
    assert "could not prepare the task directory" in (outcome.error_message or "")
    assert launcher.requests == []
