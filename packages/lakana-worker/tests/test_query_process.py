"""Tests for the worker's side of a query child, against scripted children."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import signal
import sys
import threading
import time
from collections.abc import Callable
from itertools import pairwise
from pathlib import Path

import pyarrow as pa
import pytest

from lakana_worker import memory, query_process
from lakana_worker.query_process import (
    REGISTRY,
    ChildFailed,
    QueryChild,
    StopReason,
    Stops,
)
from lakana_worker.query_protocol import (
    MAX_ERROR_BYTES,
    MAX_ERROR_LINE_BYTES,
    READ,
    ErrorDocument,
    Request,
    decode_error,
    encode_error,
)

SCRIPT = Path(__file__).parent / "scripted_child.py"


def _request(tmp: str, sql: str = "SELECT 1") -> Request:
    """Build a request for a scripted child.

    Parameters
    ----------
    tmp
        The child's private temp directory.
    sql
        The statement.

    Returns
    -------
    Request
        The request.
    """
    return Request(
        mode=READ,
        sql=sql,
        parameters={},
        read_prefixes=(),
        ca_bundle="/etc/ssl/certs/ca-certificates.crt",
        spill=False,
        memory_limit="100MB",
        threads=1,
        temp_directory=tmp,
        max_temp_directory_size="1GB",
        batch_rows=2048,
        extension_directory="/nowhere",
        parent_pid=os.getpid(),
    )


def _child(behaviour: str, **options: object) -> QueryChild:
    """Build a `QueryChild` that runs the scripted child with one behaviour.

    Parameters
    ----------
    behaviour
        The scripted behaviour.
    **options
        Overrides for the `QueryChild` arguments.

    Returns
    -------
    QueryChild
        The child, not yet started.
    """
    settings: dict[str, object] = {
        "timeout_seconds": 30.0,
        "kill_grace_seconds": 30.0,
        "argv": [sys.executable, str(SCRIPT), behaviour],
        "poll_seconds": 0.02,
    }
    settings.update(options)
    return QueryChild(_request, **settings)  # ty: ignore[invalid-argument-type]


def _rows(child: QueryChild) -> int:
    """Read a started child to its checked end and return the row count.

    Parameters
    ----------
    child
        A started child.

    Returns
    -------
    int
        The rows read.
    """
    rows = 0
    try:
        reader = pa.ipc.open_stream(child)
        while True:
            try:
                rows += reader.read_next_batch().num_rows
            except StopIteration:
                break
    except (OSError, pa.ArrowInvalid) as exc:
        raise child.failed() from exc
    child.finish()
    return rows


def _failure(
    behaviour: str, after_ready: float | None = None, **options: object
) -> tuple[ChildFailed, float]:
    """Run a scripted child that must fail; return the failure and the seconds it took.

    With `after_ready`, the time limit starts that many seconds after the child's first line on
    standard error (see `_deadline_after_ready`) and `timeout_seconds` is only a backstop.

    Parameters
    ----------
    behaviour
        The scripted behaviour.
    after_ready
        Seconds of time limit after the first line, or `None` for the option as given.
    **options
        Overrides for the `QueryChild` arguments.

    Returns
    -------
    tuple of ChildFailed and float
        The failure and the seconds it took.
    """
    started = time.monotonic()
    if after_ready is not None:
        options["timeout_seconds"] = 60.0
    with pytest.raises(ChildFailed) as caught, _child(behaviour, **options) as child:
        if after_ready is not None:
            _deadline_after_ready(child, after_ready)
        _rows(child)
    return caught.value, time.monotonic() - started


def _deadline_after_ready(child: QueryChild, seconds: float) -> threading.Thread:
    """Start the child's time limit once it has written its first line to standard error.

    A scripted child writes that line after it has set up its signal handling, so a test that
    needs the child to answer the ask is not at the mercy of how fast the interpreter starts.
    The supervisor reads the limit as an absolute time; the test moves it.

    Parameters
    ----------
    child
        A started child.
    seconds
        The time limit, counted from the first line.

    Returns
    -------
    threading.Thread
        The thread that waits for the line.
    """

    def body() -> None:
        deadline = time.monotonic() + 30
        while b"\n" not in child.stderr and time.monotonic() < deadline:
            time.sleep(0.01)
        child._deadline = time.monotonic() + seconds

    thread = threading.Thread(target=body, daemon=True)
    thread.start()
    return thread


def _wait_until_ready(child: QueryChild) -> None:
    """Read the child's standard error, bounded, until its first line has arrived.

    Parameters
    ----------
    child
        A started child.
    """
    deadline = time.monotonic() + 30
    while b"\n" not in child.stderr and time.monotonic() < deadline:
        child._serve(0.01)
    assert b"\n" in child.stderr


def _wait_for_exit(child: QueryChild) -> None:
    """Wait, bounded, until the child's process has exited (it is not reaped).

    Parameters
    ----------
    child
        A started child.
    """
    deadline = time.monotonic() + 30
    while not child._exited() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert child._exited()


def _gone(pid: int, within: float = 10.0) -> bool:
    """Say whether a process disappears within a few seconds.

    Parameters
    ----------
    pid
        The process id.
    within
        The longest wait.

    Returns
    -------
    bool
        Whether it is gone.
    """
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.02)
    return False


def _kill_largest(resident: Callable[[int], int], detail: str) -> int | None:
    """End the largest registered query child the way the memory watchdog does.

    Parameters
    ----------
    resident
        Gives the size of a child's tree for a process id.
    detail
        Why, for the failure.

    Returns
    -------
    int or None
        The ended child's process id, or `None` when none was left to end.
    """
    chosen = memory.kill_largest([(child, detail) for child in REGISTRY.children()], resident)
    return None if chosen is None else chosen.pid


def test_a_complete_stream_is_delivered_and_nothing_is_left() -> None:
    with _child("ok") as child:
        pid = child.pid
        assert _rows(child) == 30
    assert pid is not None and _gone(pid)
    assert REGISTRY.pids() == []
    assert _kill_largest(lambda _pid: 1, "x") is None


@pytest.mark.parametrize(
    "behaviour",
    ["no_end_marker", "cut_mid_batch", "garbage", "trailing_bytes", "exit_7_after_a_full_stream"],
)
def test_an_incomplete_result_is_a_failure_never_a_short_result(behaviour: str) -> None:
    """Pyarrow reads a stream that simply stops as a clean end; the worker must not."""
    failure, _seconds = _failure(behaviour)
    assert failure.stop is None
    assert failure.error is None
    assert failure.returncode == (7 if behaviour == "exit_7_after_a_full_stream" else 0)


def test_output_pyarrow_cannot_read_is_a_failure_even_with_status_0_and_the_end_marker() -> None:
    with pytest.raises(ChildFailed) as caught, _child("garbage_with_end_marker") as child:
        child._feed()  # the whole request fits one write; the child answers and exits
        _wait_for_exit(child)  # with status 0, before the worker reads a byte
        _rows(child)
    assert (caught.value.stop, caught.value.error, caught.value.returncode) == (None, None, 0)


def test_the_batch_iterator_never_ends_for_an_incomplete_result() -> None:
    delivered = 0
    with (
        pytest.raises(ChildFailed),
        query_process.run(
            _request,
            timeout_seconds=30,
            kill_grace_seconds=30,
            argv=[sys.executable, str(SCRIPT), "no_end_marker"],
        ) as (_schema, batches),
    ):
        for batch in batches:
            delivered += batch.num_rows
    assert delivered == 30  # the rows arrived; what was missing is the proof that they are all


def test_run_yields_the_schema_and_the_batches_of_a_complete_result() -> None:
    with query_process.run(
        _request,
        timeout_seconds=30,
        kill_grace_seconds=30,
        argv=[sys.executable, str(SCRIPT), "ok"],
    ) as (schema, batches):
        table = pa.Table.from_batches(list(batches), schema=schema)
    assert table.num_rows == 30
    assert schema.names == ["i"]


def test_a_child_that_fails_before_the_schema_raises_from_run() -> None:
    with (
        pytest.raises(ChildFailed) as caught,
        query_process.run(
            _request,
            timeout_seconds=30,
            kill_grace_seconds=30,
            argv=[sys.executable, str(SCRIPT), "error_document"],
        ),
    ):
        pytest.fail(
            "the block must not run"
        )  # pragma: no cover -- unreachable while the test passes
    assert caught.value.error == ErrorDocument("invalid", "Query failed: x")


def test_an_error_document_is_found_behind_other_output() -> None:
    failure, _seconds = _failure("error_document")
    assert failure.error == ErrorDocument("invalid", "Query failed: x")
    assert failure.returncode == 3
    assert failure.stop is None


def _tail_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """Pick the log records of an internal error in a query process.

    Parameters
    ----------
    caplog
        The log capture of the test.

    Returns
    -------
    list of logging.LogRecord
        The records that carry the process's last output.
    """
    return [r for r in caplog.records if r.getMessage().startswith("A query process hit an")]


def test_an_internal_error_of_the_child_reaches_the_workers_log_bounded_and_escaped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    failure, _seconds = _failure("internal_document")
    assert failure.error == ErrorDocument("internal", "Query failed: x")
    (record,) = _tail_records(caplog)
    assert record.levelno == logging.WARNING
    text = record.getMessage()
    assert "\\x1b[31m" in text and "\x1b" not in text  # escaped, never raw
    assert "FIRST-LINE-OUT-OF-BOUND" not in text  # only the last 2,000 bytes
    assert len(text) < 2_400
    assert "Query failed: x" in text


def test_a_user_error_puts_nothing_of_the_childs_output_into_the_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    failure, _seconds = _failure("invalid_document_with_text")
    assert failure.error == ErrorDocument("invalid", "Query failed: x")
    assert caplog.records == []


@pytest.mark.parametrize("unit", ["x", "\x01", "\u00e4", "\U0001f600"])
def test_a_largest_error_document_behind_100_kb_of_other_output_is_still_decoded(
    unit: str,
) -> None:
    error = ErrorDocument("invalid", unit * 20_000)
    line = encode_error(error)
    assert len(line) > 100  # the document, not an empty message
    with (
        pytest.raises(ChildFailed) as caught,
        query_process.run(
            _request,
            timeout_seconds=30,
            kill_grace_seconds=30,
            argv=[sys.executable, str(SCRIPT), "stderr_line", line],
        ),
    ):
        pytest.fail("the block must not run")  # pragma: no cover -- unreachable while it passes
    assert caught.value.error == decode_error(line.encode())
    assert caught.value.error is not None


def test_the_kept_standard_error_is_at_least_twice_the_longest_error_line() -> None:
    assert query_process._MAX_STDERR_BYTES >= 2 * MAX_ERROR_LINE_BYTES > 2 * MAX_ERROR_BYTES


def test_a_megabyte_on_standard_error_neither_blocks_the_child_nor_is_kept() -> None:
    with _child("loud_stderr") as child:
        assert _rows(child) == 30
        assert len(child.stderr) <= query_process._MAX_STDERR_BYTES


_ADDED_BY_THE_CHILD_ITSELF = {"LC_CTYPE"} | (
    {"__CF_USER_TEXT_ENCODING"} if sys.platform == "darwin" else set()
)
"""Variables the child's own runtime sets after `exec`, none of them from the worker.

The interpreter adds ``LC_CTYPE`` when the locale is C (PEP 538); macOS's libSystem adds
``__CF_USER_TEXT_ENCODING`` (the user's id and text encoding) to every process that lacks it.
"""


def test_the_child_gets_the_request_a_scrubbed_environment_and_only_its_pipes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "s3cr3t-key")
    monkeypatch.setenv("LAKANA_DATABASE__URL", "postgresql://lakana:pw@db/lakana")
    monkeypatch.setenv("LAKANA_SECRETS__MASTER_KEY", "master")
    monkeypatch.setenv("HTTPS_PROXY", "http://user:pw@proxy:3128")
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    read_end, write_end = os.pipe()
    os.set_inheritable(write_end, True)
    try:
        with _child("echo_request") as child:
            assert _rows(child) == 1
            lines = child.stderr.decode().splitlines()
    finally:
        os.close(read_end)
        os.close(write_end)
    seen = json.loads(lines[0])
    assert set(seen["env"]) - _ADDED_BY_THE_CHILD_ITSELF == {
        "PATH",
        "HOME",
        "TMPDIR",
        "TZ",
    }
    assert seen["env"]["TZ"] == "Asia/Tokyo"
    assert seen["env"]["HOME"] != os.environ["HOME"]
    assert set(seen["request"]) == {
        "mode",
        "sql",
        "parameters",
        "read_prefixes",
        "ca_bundle",
        "spill",
        "memory_limit",
        "threads",
        "temp_directory",
        "max_temp_directory_size",
        "batch_rows",
        "extension_directory",
        "parent_pid",
    }
    assert seen["request"]["temp_directory"] == seen["env"]["TMPDIR"]
    everything = lines[0]
    for secret in ("s3cr3t-key", "postgresql://", "master", "proxy:3128"):
        assert secret not in everything
    assert json.loads(lines[1])["fds"] == [0, 1, 2]


def test_the_child_starts_isolated_and_without_bytecode_files() -> None:
    """`-I` ignores the PYTHON* variables, so `-B` is what keeps the child from writing .pyc."""
    child = QueryChild(_request, timeout_seconds=1, kill_grace_seconds=1)
    assert child._argv == [sys.executable, "-I", "-B", "-m", "lakana_worker.query_child"]
    assert query_process.CHILD_MODULE == "lakana_worker.query_child"


def test_without_tz_the_child_gets_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TZ", raising=False)
    with _child("echo_request") as child:
        _rows(child)
        seen = json.loads(child.stderr.decode().splitlines()[0])
    assert "TZ" not in seen["env"]


def test_a_child_that_stops_when_asked_is_not_killed() -> None:
    failure, seconds = _failure("stops_when_asked", after_ready=0.3, kill_grace_seconds=20.0)
    assert (failure.stop, failure.killed) == (StopReason.TIMEOUT, False)
    assert seconds < 15  # the grace is 20: the child answered the ask


def test_a_child_that_ignores_the_ask_is_killed_after_the_grace() -> None:
    failure, seconds = _failure("ignores_sigterm", after_ready=0.3, kill_grace_seconds=0.5)
    assert (failure.stop, failure.killed) == (StopReason.TIMEOUT, True)
    assert failure.returncode == -signal.SIGKILL
    assert 0.8 <= seconds < 30


def test_a_task_runs_cancel_event_stops_the_child() -> None:
    event = threading.Event()
    threading.Timer(0.3, event.set).start()
    failure, seconds = _failure("stops_when_asked", stops=Stops(cancel_event=event))
    assert (failure.stop, failure.killed) == (StopReason.CANCEL_EVENT, False)
    assert seconds < 30


@pytest.mark.slow
def test_a_cancel_request_stops_the_child_and_is_read_about_once_a_second() -> None:
    calls: list[float] = []

    def requested() -> bool:
        """Record the read and say yes from the third on.

        Returns
        -------
        bool
            Whether the request was recorded.
        """
        calls.append(time.monotonic())
        return len(calls) >= 3

    failure, seconds = _failure("stops_when_asked", stops=Stops(cancel_requested=requested))
    assert (failure.stop, failure.killed) == (StopReason.CANCEL_REQUEST, False)
    assert len(calls) == 3
    assert 1.9 <= seconds < 30
    assert all(later - earlier >= 0.99 for earlier, later in pairwise(calls))


def test_marker_shaped_data_inside_a_stream_is_data_and_the_whole_result_arrives() -> None:
    with _child("marker_shaped_data") as child:
        assert _rows(child) == 6


def test_a_stream_cut_where_its_last_bytes_equal_the_marker_is_refused_by_the_status() -> None:
    failure, _seconds = _failure("cut_after_marker_shaped_data")
    assert (failure.stop, failure.error, failure.returncode) == (None, None, 9)


def test_a_clean_exit_is_not_enough_without_the_real_end_of_the_pipe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A holder of the pipe outside the group keeps it open: the end was never seen."""
    monkeypatch.setattr(query_process, "_DRAIN_SECONDS", 0.3)
    release = tmp_path / "release"  # the holder lives until this file exists, however slow the run
    escaped = 0
    try:
        with (
            pytest.raises(ChildFailed) as caught,
            _child(
                "clean_exit_with_a_pipe_holder",
                argv=[sys.executable, str(SCRIPT), "clean_exit_with_a_pipe_holder", str(release)],
            ) as child,
        ):
            try:
                _rows(child)
            finally:
                lines = child.stderr.decode().splitlines()
                escaped = json.loads(next(line for line in lines if line.startswith("{")))[
                    "escaped"
                ]
        assert (caught.value.stop, caught.value.returncode) == (None, 0)
    finally:
        release.touch()
        if escaped:
            with contextlib.suppress(ProcessLookupError):
                os.kill(escaped, signal.SIGKILL)


def test_after_the_ask_the_worker_keeps_reading_so_a_child_blocked_on_a_full_pipe_can_stop() -> (
    None
):
    started = time.monotonic()
    with _child("blocked_when_asked", timeout_seconds=60.0, kill_grace_seconds=30.0) as child:
        assert len(child.read(10)) == 10  # the child has set up its signals and is writing
        child._deadline = time.monotonic() + 0.3  # nobody reads: the pipe is full by then
        deadline = time.monotonic() + 30
        while child.reason is None and time.monotonic() < deadline:
            child._tick()
            time.sleep(0.02)
        assert child.reason is StopReason.TIMEOUT
        with pytest.raises(ChildFailed) as caught:
            child.finish()
    assert (caught.value.stop, caught.value.killed) == (StopReason.TIMEOUT, False)
    assert time.monotonic() - started < 25  # not the 30 s grace


def test_a_cancel_request_is_not_read_once_the_cancel_event_has_decided() -> None:
    event = threading.Event()
    event.set()
    asked: list[bool] = []

    def requested() -> bool:
        """Record that it was asked.

        Returns
        -------
        bool
            Always yes.
        """
        asked.append(True)
        return True

    failure, _seconds = _failure(
        "stops_when_asked", stops=Stops(cancel_event=event, cancel_requested=requested)
    )
    assert failure.stop is StopReason.CANCEL_EVENT
    assert asked == []


def test_a_child_whose_output_ends_early_gets_the_grace_to_exit_by_itself() -> None:
    failure, _seconds = _failure("closes_stdout_and_lingers", kill_grace_seconds=30.0)
    assert (failure.stop, failure.returncode, failure.own_kill) == (None, 0, False)


def test_a_child_whose_output_ends_early_and_does_not_exit_is_killed_after_the_grace() -> None:
    failure, seconds = _failure("closes_stdout_and_lingers", kill_grace_seconds=0.3)
    assert (failure.stop, failure.returncode, failure.own_kill) == (None, -signal.SIGKILL, True)
    assert failure.killed is False  # not a stop: the supervisor's own clean-up
    assert seconds < 30


def test_a_child_that_fails_after_closing_its_stream_still_delivers_its_document() -> None:
    """The child's own exit status and error document win over the worker's wish to clean up."""
    failure, _seconds = _failure("error_after_closing_stdout")
    assert failure.error == ErrorDocument("invalid", "Query failed: x")
    assert (failure.returncode, failure.own_kill) == (3, False)


def test_an_unreadable_stream_on_an_open_pipe_is_killed_at_once() -> None:
    failure, seconds = _failure("garbage", kill_grace_seconds=30.0)
    assert failure.stop is None
    assert seconds < 25


def test_the_error_document_of_a_late_failure_is_found_when_the_consumer_is_busy() -> None:
    with pytest.raises(ChildFailed) as caught, _child("batch_then_error") as child:
        reader = pa.ipc.open_stream(child)
        assert reader.read_next_batch().num_rows == 10
        _wait_for_exit(child)  # the consumer is busy until the child has failed
        with contextlib.suppress(StopIteration):  # the stream just stops: pyarrow calls it an end
            reader.read_next_batch()
        child.finish()
    assert caught.value.error == ErrorDocument("invalid", "Query failed: x")
    assert caught.value.returncode == 3


def test_standard_error_is_read_once_more_after_the_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Whatever was written just before the exit is found even when the wait read nothing."""
    monkeypatch.setattr(QueryChild, "_serve", lambda self, seconds: time.sleep(seconds))
    failure, _seconds = _failure("error_after_closing_stdout_late")
    assert failure.error == ErrorDocument("invalid", "Query failed: x")


def test_leaving_the_block_early_is_the_supervisors_own_kill() -> None:
    with _child("ignores_sigterm") as child:
        _wait_until_ready(child)
    assert child._own_kill is True


def test_standard_error_is_read_while_the_worker_waits_for_the_exit() -> None:
    started = time.monotonic()
    with pytest.raises(ChildFailed) as caught, _child("closes_stdout_then_floods_stderr") as child:
        child._deadline = time.monotonic() + 8
        _rows(child)
    assert caught.value.stop is None
    assert caught.value.returncode == 0
    assert time.monotonic() - started < 7


@pytest.mark.parametrize("behaviour", ["two_streams", "stream_then_marker"])
def test_anything_after_the_end_of_the_stream_but_its_marker_is_refused(behaviour: str) -> None:
    failure, _seconds = _failure(behaviour)
    assert (failure.stop, failure.error, failure.returncode) == (None, None, 0)


@pytest.mark.parametrize("bit", [3, 100, 867, 931, 1000, 2000, 5000])
def test_a_corrupt_stream_is_a_failure_or_a_valid_result_never_a_raw_exception(bit: int) -> None:
    """A flipped bit may still decode (data); what pyarrow rejects is a `ChildFailed`."""
    try:
        with query_process.run(
            _request,
            timeout_seconds=30,
            kill_grace_seconds=30,
            argv=[sys.executable, str(SCRIPT), f"flip_{bit}"],
        ) as (_schema, batches):
            assert sum(batch.num_rows for batch in batches) == 30
    except ChildFailed as failed:
        assert failed.stop is None


@pytest.mark.parametrize("error", [MemoryError, pa.ArrowNotImplementedError])
def test_run_turns_any_error_of_the_reader_into_child_failed(
    monkeypatch: pytest.MonkeyPatch, error: type[Exception]
) -> None:
    real = pa.ipc.open_stream

    class Reader:
        """A reader that fails on its first batch."""

        def __init__(self, source: object) -> None:
            """Wrap the real reader.

            Parameters
            ----------
            source
                What to read from.
            """
            self._real = real(source)
            self.schema = self._real.schema

        def read_next_batch(self) -> pa.RecordBatch:
            """Fail like pyarrow does on a corrupt stream.

            Raises
            ------
            Exception
                Always.
            """
            raise error("corrupt")

    monkeypatch.setattr(query_process.pa.ipc, "open_stream", Reader)
    with (
        pytest.raises(ChildFailed),
        query_process.run(
            _request,
            timeout_seconds=30,
            kill_grace_seconds=30,
            argv=[sys.executable, str(SCRIPT), "ok"],
        ) as (_schema, batches),
    ):
        with pytest.raises(ChildFailed) as caught:
            list(batches)
        assert caught.value.stop is None
        assert list(batches) == []  # a stream that failed stays finished
        raise caught.value


def test_a_failure_after_the_process_started_does_not_leak_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pids: list[int] = []

    def add(child: QueryChild) -> None:
        """Fail the registration once, as an interrupt in that window would.

        Parameters
        ----------
        child
            The child being registered.

        Raises
        ------
        RuntimeError
            Always.
        """
        assert child.pid is not None
        pids.append(child.pid)
        raise RuntimeError("registration failed")

    monkeypatch.setattr(REGISTRY, "add", add)
    child = _child("ignores_sigterm", directory_prefix=str(tmp_path / "q-"))
    with pytest.raises(RuntimeError, match="registration failed"), child:
        pytest.fail("the block must not run")  # pragma: no cover -- unreachable when it passes
    assert _gone(pids[0])
    assert child.closed
    assert list(tmp_path.iterdir()) == []


def test_a_request_the_child_never_takes_is_dropped_when_its_end_is_announced() -> None:
    def make(tmp: str) -> Request:
        """Build a request too large for the pipe.

        Parameters
        ----------
        tmp
            The child's private temp directory.

        Returns
        -------
        Request
            The request.
        """
        return _request(tmp, sql="SELECT 1 -- " + "x" * 4_000_000)

    child = QueryChild(
        make,
        timeout_seconds=30,
        kill_grace_seconds=30,
        argv=[sys.executable, str(SCRIPT), "closes_stdout_without_reading"],
        poll_seconds=0.02,
    )
    with pytest.raises(ChildFailed) as caught, child:
        _rows(child)
    assert (caught.value.stop, caught.value.returncode) == (None, 0)


def test_the_supervisors_clean_up_kill_leaves_an_exited_child_alone() -> None:
    with _child("exits_without_reading") as child:
        _wait_for_exit(child)
        child._clean_up_kill()
        assert child._own_kill is False


def test_a_start_failure_without_a_file_name_is_still_a_failure_with_the_reason() -> None:
    child = QueryChild(
        _request, timeout_seconds=30, kill_grace_seconds=30, argv=["python\0with-a-null"]
    )
    with pytest.raises(ChildFailed) as caught, child:
        pytest.fail("the block must not run")  # pragma: no cover -- unreachable when it passes
    assert caught.value.detail is not None
    assert caught.value.detail.startswith("could not start the query process: ")


def test_a_zero_grace_kills_in_the_round_after_the_ask() -> None:
    failure, seconds = _failure("ignores_sigterm", after_ready=0.2, kill_grace_seconds=0)
    assert (failure.stop, failure.killed) == (StopReason.TIMEOUT, True)
    assert failure.returncode == -signal.SIGKILL
    assert seconds < 25


def test_a_consumer_that_raises_stops_and_reaps_the_child_and_its_exception_arrives() -> None:
    pid = 0
    with (
        pytest.raises(KeyError, match="consumer"),
        query_process.run(
            _request,
            timeout_seconds=30,
            kill_grace_seconds=30,
            argv=[sys.executable, str(SCRIPT), "ok"],
        ) as (_schema, batches),
    ):
        pid = next(c.pid or 0 for c in [*_running_children()])
        next(batches)
        raise KeyError("consumer")
    assert pid and _gone(pid)
    assert REGISTRY.pids() == []


def _running_children() -> list[QueryChild]:
    """List the children the registry holds (a test reads the one it started).

    Returns
    -------
    list of QueryChild
        The registered children.
    """
    return list(REGISTRY._children)


def test_draining_is_asked_outside_the_lock_a_memory_kill_needs() -> None:
    entered, released = threading.Event(), threading.Event()

    def draining() -> bool:
        """Block like a slow read would.

        Returns
        -------
        bool
            Never a drain.
        """
        entered.set()
        released.wait(15)
        return False

    outcome: list[ChildFailed] = []
    with _child("ignores_sigterm", stops=Stops(draining=draining)) as child:

        def read() -> None:
            """Read the child to its end and keep the failure."""
            try:
                _rows(child)
            except ChildFailed as failed:
                outcome.append(failed)

        thread = threading.Thread(target=read, daemon=True)
        thread.start()
        assert entered.wait(15)
        started = time.monotonic()
        killed = child.kill_for_memory("over the limit")
        waited = time.monotonic() - started
        released.set()
        thread.join(15)
    assert killed is True
    assert waited < 5
    assert outcome[0].stop is StopReason.OUT_OF_MEMORY


def test_the_caller_can_ask_for_a_stop_reason_without_reading() -> None:
    with _child("ignores_sigterm", timeout_seconds=60.0, kill_grace_seconds=30.0) as child:
        assert child.stop_reason() is None
        child._deadline = time.monotonic() - 1
        assert child.stop_reason() is StopReason.TIMEOUT
        assert child.stop_reason() is StopReason.TIMEOUT  # the first reason stays
    assert child.stop_reason() is StopReason.TIMEOUT  # also once the child is reaped


def test_the_batch_stream_of_run_answers_the_same_question() -> None:
    event = threading.Event()
    with query_process.run(
        _request,
        timeout_seconds=30,
        kill_grace_seconds=30,
        stops=Stops(cancel_event=event),
        argv=[sys.executable, str(SCRIPT), "ok"],
    ) as (_schema, batches):
        assert batches.stop_reason() is None  # type: ignore[attr-defined]
        event.set()
        assert batches.stop_reason() is StopReason.CANCEL_EVENT  # type: ignore[attr-defined]
        with pytest.raises(ChildFailed) as caught:
            list(batches)
        assert caught.value.stop is StopReason.CANCEL_EVENT
        assert list(batches) == []  # a failed stream stays finished


@pytest.mark.slow
def test_a_cancel_read_that_raises_counts_as_no_and_is_logged_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def requested() -> bool:
        """Fail like a lost database connection.

        Raises
        ------
        RuntimeError
            Always.
        """
        raise RuntimeError("database gone")

    failure, _seconds = _failure(
        "stops_when_asked",
        timeout_seconds=2.3,
        stops=Stops(cancel_requested=requested),
    )
    assert failure.stop is StopReason.TIMEOUT
    assert [r.getMessage() for r in caplog.records].count(
        "Reading a query's cancel request failed; it is retried."
    ) == 1


def test_a_drain_stops_the_child() -> None:
    started = time.monotonic()
    failure, _seconds = _failure(
        "stops_when_asked", stops=Stops(draining=lambda: time.monotonic() - started > 0.3)
    )
    assert (failure.stop, failure.killed) == (StopReason.DRAIN, False)


def test_a_stale_worker_stops_the_child() -> None:
    started = time.monotonic()
    failure, _seconds = _failure(
        "stops_when_asked", stops=Stops(worker_stale=lambda: time.monotonic() - started > 0.3)
    )
    assert (failure.stop, failure.killed) == (StopReason.WORKER_STALE, False)


def test_worker_stale_is_asked_outside_the_lock() -> None:
    entered, released = threading.Event(), threading.Event()

    def worker_stale() -> bool:
        """Block like a slow read would.

        Returns
        -------
        bool
            Never stale.
        """
        entered.set()
        released.wait(15)
        return False

    outcome: list[ChildFailed] = []
    with _child("ignores_sigterm", stops=Stops(worker_stale=worker_stale)) as child:

        def read() -> None:
            """Read the child to its end and keep the failure."""
            try:
                _rows(child)
            except ChildFailed as failed:
                outcome.append(failed)

        thread = threading.Thread(target=read, daemon=True)
        thread.start()
        assert entered.wait(15)
        started = time.monotonic()
        killed = child.kill_for_memory("over the limit")
        waited = time.monotonic() - started
        released.set()
        thread.join(15)
    assert killed is True
    assert waited < 5


def test_a_cancel_request_wins_over_a_stale_worker() -> None:
    failure, _seconds = _failure(
        "stops_when_asked", stops=Stops(cancel_requested=lambda: True, worker_stale=lambda: True)
    )
    assert failure.stop is StopReason.CANCEL_REQUEST


def test_a_drain_wins_over_a_stale_worker() -> None:
    failure, _seconds = _failure(
        "stops_when_asked", stops=Stops(draining=lambda: True, worker_stale=lambda: True)
    )
    assert failure.stop is StopReason.DRAIN


def test_a_stale_worker_wins_over_the_time_limit() -> None:
    failure, _seconds = _failure(
        "stops_when_asked", stops=Stops(worker_stale=lambda: True), timeout_seconds=0.0
    )
    assert failure.stop is StopReason.WORKER_STALE


def test_the_first_reason_stays_when_a_second_arrives_during_the_grace() -> None:
    event = threading.Event()
    started = time.monotonic()
    with (
        pytest.raises(ChildFailed) as caught,
        _child(
            "ignores_sigterm", timeout_seconds=60.0, kill_grace_seconds=1.0, stops=Stops(event)
        ) as child,
    ):
        _deadline_after_ready(child, 0.2)

        def second_reason() -> None:
            """Set the cancel event once the time limit is the recorded reason."""
            deadline = time.monotonic() + 30
            while child.reason is None and time.monotonic() < deadline:
                time.sleep(0.01)
            event.set()

        thread = threading.Thread(target=second_reason, daemon=True)
        thread.start()
        _rows(child)
    thread.join(5)
    failure = caught.value
    assert (failure.stop, failure.killed) == (StopReason.TIMEOUT, True)
    assert event.is_set()
    assert 1.1 <= time.monotonic() - started < 30


def test_a_result_that_completes_while_the_child_is_being_stopped_is_not_a_result() -> None:
    failure, _seconds = _failure("finishes_when_asked", after_ready=0.3)
    assert (failure.stop, failure.killed) == (StopReason.TIMEOUT, False)
    assert failure.returncode == 0


def test_the_time_limit_counts_after_the_child_has_exited_until_the_result_is_judged() -> None:
    """A write's time limit covers the worker's own storage time, as it always did."""
    with pytest.raises(ChildFailed) as caught, _child("ok") as child:
        child._feed()  # the whole request fits one write
        _wait_for_exit(child)  # the child finished its stream and exited with status 0
        child._deadline = time.monotonic() - 1  # the worker was too slow
        _rows(child)
    assert (caught.value.stop, caught.value.returncode) == (StopReason.TIMEOUT, 0)


def test_a_cancel_request_is_read_without_the_lock_a_memory_kill_needs() -> None:
    """The cancel read is a database read later; a memory kill must never wait behind it."""
    entered, released = threading.Event(), threading.Event()

    def requested() -> bool:
        """Block like a slow read would.

        Returns
        -------
        bool
            Never a request.
        """
        entered.set()
        released.wait(15)
        return False

    outcome: list[ChildFailed] = []
    with _child("ignores_sigterm", stops=Stops(cancel_requested=requested)) as child:

        def read() -> None:
            """Read the child to its end and keep the failure."""
            try:
                _rows(child)
            except ChildFailed as failed:
                outcome.append(failed)

        thread = threading.Thread(target=read, daemon=True)
        thread.start()
        assert entered.wait(15)
        started = time.monotonic()
        killed = child.kill_for_memory("over the limit")
        waited = time.monotonic() - started
        released.set()
        thread.join(15)
    assert killed is True
    assert waited < 5
    assert outcome[0].stop is StopReason.OUT_OF_MEMORY


def test_a_consumer_that_is_slow_between_reads_still_gets_the_end_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The drain clock cuts off a silent pipe, never bytes that are waiting in it."""
    monkeypatch.setattr(query_process, "_DRAIN_SECONDS", 0.2)
    with _child("ok") as child:
        first = child.read(10)
        _wait_for_exit(child)
        child._tick()  # the clock starts: the child has exited cleanly
        time.sleep(1.0)  # five times the clock, while the whole stream waits in the pipe
        rest = b""
        while chunk := child.read(4096):
            rest += chunk
        child.finish()
    table = pa.ipc.open_stream(first + rest).read_all()
    assert table.num_rows == 30


def _when_ready(child: QueryChild, action: Callable[[], object]) -> threading.Thread:
    """Run `action` in a thread once the scripted child has written READY.

    Parameters
    ----------
    child
        A started child.
    action
        What to run.

    Returns
    -------
    threading.Thread
        The thread.
    """

    def body() -> None:
        deadline = time.monotonic() + 10
        while b"READY" not in child.stderr and time.monotonic() < deadline:
            time.sleep(0.01)
        action()

    thread = threading.Thread(target=body, daemon=True)
    thread.start()
    return thread


def test_the_memory_watchdog_kills_at_once_and_the_reason_is_out_of_memory() -> None:
    started = time.monotonic()
    killed: list[int | None] = []
    with pytest.raises(ChildFailed) as caught, _child("ignores_sigterm") as child:
        pid = child.pid
        thread = _when_ready(
            child, lambda: killed.append(_kill_largest(lambda _pid: 1, "over the limit"))
        )
        _rows(child)
    thread.join(5)
    failure = caught.value
    assert (failure.stop, failure.killed) == (StopReason.OUT_OF_MEMORY, True)
    assert failure.detail == "over the limit"
    assert killed == [pid]
    assert time.monotonic() - started < 25  # no ask, no grace (the scripted child sleeps 60)


def test_a_memory_kill_during_the_grace_keeps_the_first_reason_and_ends_the_wait() -> None:
    started = time.monotonic()
    with (
        pytest.raises(ChildFailed) as caught,
        _child("ignores_sigterm", timeout_seconds=60.0, kill_grace_seconds=30.0) as child,
    ):
        _deadline_after_ready(child, 0.2)

        def kill() -> None:
            """Kill for memory once the time limit is the recorded reason."""
            deadline = time.monotonic() + 30
            while child.reason is None and time.monotonic() < deadline:
                time.sleep(0.01)
            child.kill_for_memory("over the limit")

        thread = threading.Thread(target=kill, daemon=True)
        thread.start()
        _rows(child)
    thread.join(5)
    failure = caught.value
    assert (failure.stop, failure.killed, failure.detail) == (StopReason.TIMEOUT, True, None)
    assert time.monotonic() - started < 25  # not the 30 s grace


def test_nothing_is_signalled_once_the_child_is_reaped(monkeypatch: pytest.MonkeyPatch) -> None:
    """A process id is free for reuse after the reap: no signal may follow it."""
    with _child("ok") as child:
        _rows(child)
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(query_process.os, "killpg", lambda pid, signum: sent.append((pid, signum)))
    assert child.kill_for_memory("late") is False
    assert child._signal(signal.SIGKILL) is False
    child.close()
    assert _kill_largest(lambda _pid: 1, "late") is None
    assert sent == []
    assert child.reason is None


def test_the_largest_child_is_the_one_killed() -> None:
    with _child("ignores_sigterm") as small, _child("ignores_sigterm") as large:
        assert set(REGISTRY.pids()) == {small.pid, large.pid}
        sizes = {small.pid: 10, large.pid: 20}
        assert _kill_largest(lambda pid: sizes[pid], "x") == large.pid
        assert large.reason is StopReason.OUT_OF_MEMORY
        assert small.reason is None


def test_a_grandchild_dies_with_the_group() -> None:
    with (
        pytest.raises(ChildFailed),
        _child("leaves_a_grandchild", timeout_seconds=60.0, kill_grace_seconds=0.2) as child,
    ):
        _deadline_after_ready(child, 0.2)
        try:
            _rows(child)
        finally:
            lines = child.stderr.decode().splitlines()
            grandchild = json.loads(next(line for line in lines if line.startswith("{")))[
                "grandchild"
            ]
    assert _gone(grandchild)


def test_the_private_directory_is_removed_with_what_the_child_wrote() -> None:
    with (
        pytest.raises(ChildFailed),
        _child("writes_a_temp_file", timeout_seconds=60.0, kill_grace_seconds=0.2) as child,
    ):
        _deadline_after_ready(child, 0.2)
        try:
            _rows(child)
        finally:
            tmp = Path(child.stderr.decode().splitlines()[0])
    assert not tmp.exists()
    assert not tmp.parent.exists()


def test_leaving_the_block_early_kills_the_child() -> None:
    with _child("ignores_sigterm") as child:
        pid = child.pid
    assert pid is not None and _gone(pid)
    assert child.closed


def test_a_child_that_never_reads_a_large_request_does_not_block_the_worker() -> None:
    urls = ["https://store.example/" + "x" * 200] * 20_000  # about 4 MB of request

    def make(tmp: str) -> Request:
        """Build a request of about 4 MB.

        Parameters
        ----------
        tmp
            The child's private temp directory.

        Returns
        -------
        Request
            The request.
        """
        return _request(tmp, sql="SELECT 1 -- " + " ".join(urls))

    result: list[ChildFailed] = []

    def body() -> None:
        child = QueryChild(
            make,
            timeout_seconds=0.3,
            kill_grace_seconds=0.2,
            argv=[sys.executable, str(SCRIPT), "never_reads_stdin"],
            poll_seconds=0.02,
        )
        try:
            with child:
                _rows(child)
        except ChildFailed as failed:
            result.append(failed)

    thread = threading.Thread(target=body, daemon=True)
    thread.start()
    thread.join(10)
    assert not thread.is_alive()
    assert result[0].stop is StopReason.TIMEOUT


def test_a_process_that_cannot_start_is_a_failure_with_the_reason(tmp_path: Path) -> None:
    child = QueryChild(
        _request,
        timeout_seconds=30,
        kill_grace_seconds=30,
        argv=[str(tmp_path / "no-such-python")],
        directory_prefix=str(tmp_path / "q-"),
    )
    with pytest.raises(ChildFailed) as caught, child:
        pytest.fail(
            "the block must not run"
        )  # pragma: no cover -- unreachable while the test passes
    assert caught.value.detail is not None
    assert caught.value.detail.startswith("could not start the query process: ")
    assert str(tmp_path / "no-such-python") in caught.value.detail
    assert caught.value.returncode is None
    assert list(tmp_path.iterdir()) == []
    assert child.pid is None


def test_a_child_that_produces_faster_than_the_worker_reads_waits_on_its_pipe() -> None:
    """Standard output is read only when the caller asks: the pipe is the only buffer."""
    with _child("floods") as child:
        assert len(child.read(10)) == 10
        time.sleep(1.3)
        deadline = time.monotonic() + 5
        while b"written" not in child.stderr and time.monotonic() < deadline:
            child.read(1)
        written = json.loads(child.stderr.decode().splitlines()[0])["written"]
    assert written < 4 * 1024 * 1024


def test_the_extension_directory_is_duckdbs_default_for_the_workers_user() -> None:
    assert query_process.extension_directory() == str(Path.home() / ".duckdb" / "extensions")


@pytest.mark.parametrize("behaviour", ["cut_mid_batch", "garbage", "no_end_marker"])
def test_run_raises_child_failed_from_the_iterator_for_output_it_cannot_trust(
    behaviour: str,
) -> None:
    with (
        pytest.raises(ChildFailed) as caught,
        query_process.run(
            _request,
            timeout_seconds=30,
            kill_grace_seconds=30,
            argv=[sys.executable, str(SCRIPT), behaviour],
        ) as (_schema, batches),
    ):
        for _batch in batches:
            pass
    assert (caught.value.stop, caught.value.error) == (None, None)


def test_a_child_that_exits_without_reading_its_request_is_a_failure_not_a_hang() -> None:
    def make(tmp: str) -> Request:
        """Build a request of 4 MB.

        Parameters
        ----------
        tmp
            The child's private temp directory.

        Returns
        -------
        Request
            The request.
        """
        return _request(tmp, sql="SELECT 1 -- " + "x" * 4_000_000)

    child = QueryChild(
        make,
        timeout_seconds=30,
        kill_grace_seconds=30,
        argv=[sys.executable, str(SCRIPT), "exits_without_reading"],
        poll_seconds=0.02,
    )
    started = time.monotonic()
    with pytest.raises(ChildFailed) as caught, child:
        _rows(child)
    assert (caught.value.stop, caught.value.returncode) == (None, 5)
    assert time.monotonic() - started < 10


def test_a_write_that_would_block_is_tried_again(monkeypatch: pytest.MonkeyPatch) -> None:
    real, calls = os.write, []

    def write(fd: int, data: memoryview) -> int:
        """Refuse the first write as a full pipe does.

        Parameters
        ----------
        fd
            The descriptor.
        data
            The bytes.

        Returns
        -------
        int
            The bytes written.

        Raises
        ------
        BlockingIOError
            On the first call.
        """
        calls.append(fd)
        if len(calls) == 1:
            raise BlockingIOError
        return real(fd, data)

    monkeypatch.setattr(query_process.os, "write", write)
    with _child("ok") as child:
        assert _rows(child) == 30
    assert len(calls) == 2


def test_a_process_that_left_the_group_cannot_hold_the_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A descendant in a session of its own survives the group kill and keeps the pipes open."""
    monkeypatch.setattr(query_process, "_DRAIN_SECONDS", 0.3)
    started = time.monotonic()
    escaped = 0
    try:
        with (
            pytest.raises(ChildFailed) as caught,
            _child(
                "leaves_a_process_in_another_session", timeout_seconds=60.0, kill_grace_seconds=0.2
            ) as child,
        ):
            _deadline_after_ready(child, 0.2)
            try:
                _rows(child)
            finally:
                lines = child.stderr.decode().splitlines()
                escaped = json.loads(next(line for line in lines if line.startswith("{")))[
                    "escaped"
                ]
        assert caught.value.stop is StopReason.TIMEOUT
        assert time.monotonic() - started < 30
    finally:
        if escaped:
            with contextlib.suppress(ProcessLookupError):
                os.kill(escaped, signal.SIGKILL)


def test_a_child_that_already_ended_is_passed_over_for_the_next_largest() -> None:
    with _child("exits_without_reading") as ended, _child("ignores_sigterm") as running:
        deadline = time.monotonic() + 10
        while not ended._exited() and time.monotonic() < deadline:
            time.sleep(0.01)
        sizes = {ended.pid: 20, running.pid: 10}
        assert _kill_largest(lambda pid: sizes[pid], "x") == running.pid
        assert ended.reason is None


def test_a_group_that_is_already_gone_is_not_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def gone(pid: int, signum: int) -> None:
        """Say that the group does not exist.

        Parameters
        ----------
        pid
            The group.
        signum
            The signal.

        Raises
        ------
        ProcessLookupError
            Always.
        """
        raise ProcessLookupError

    with _child("ignores_sigterm") as child:
        with monkeypatch.context() as patch:
            patch.setattr(query_process.os, "killpg", gone)
            assert child.kill_for_memory("x") is False
        assert child.reason is None


def test_a_request_the_child_no_longer_takes_is_dropped_not_retried_for_ever(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def write(fd: int, data: object) -> int:
        """Fail like a pipe whose reader left.

        Parameters
        ----------
        fd
            The descriptor.
        data
            The bytes.

        Raises
        ------
        BrokenPipeError
            Always.
        """
        raise BrokenPipeError(32, "Broken pipe")

    monkeypatch.setattr(query_process.os, "write", write)
    with pytest.raises(ChildFailed) as caught, _child("ok") as child:
        _rows(child)
    assert (caught.value.stop, caught.value.error) == (None, None)
    assert caught.value.returncode not in (0, None)


def test_a_request_that_cannot_be_built_leaves_no_directory(tmp_path: Path) -> None:
    def make(tmp: str) -> Request:
        """Fail like a request that cannot be built.

        Parameters
        ----------
        tmp
            The child's private temp directory.

        Raises
        ------
        TypeError
            Always.
        """
        raise TypeError("Query parameter 'x' has a type the query process cannot take: float.")

    child = QueryChild(
        make, timeout_seconds=30, kill_grace_seconds=30, directory_prefix=str(tmp_path / "q-")
    )
    with pytest.raises(TypeError, match="cannot take"), child:
        pytest.fail(
            "the block must not run"
        )  # pragma: no cover -- unreachable while the test passes
    assert list(tmp_path.iterdir()) == []
    assert child.closed


def test_stop_reason_is_none_once_the_stream_has_ended_normally() -> None:
    """A late reason cannot un-complete a result: no check after the end can find one."""
    event = threading.Event()
    with _child("ok", stops=Stops(cancel_event=event)) as child:
        assert _rows(child) == 30
        event.set()
        child._deadline = time.monotonic() - 1
        assert child.stop_reason() is None


def test_stop_reason_of_a_child_that_never_started_is_none() -> None:
    assert _child("ok").stop_reason() is None


def test_a_child_that_exits_by_itself_is_not_marked_as_killed_by_the_supervisor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With a grace of 0 the clean-up kill is sent; if the status is not the kill, it is not one.

    The signal is replaced by one that reports success and sends nothing, so the child exits by
    itself with its own status and document.
    """
    real = QueryChild._signal

    def pretend(self: QueryChild, signum: int) -> bool:
        """Report a kill that was never sent; pass every other signal on.

        Parameters
        ----------
        self
            The child.
        signum
            The signal.

        Returns
        -------
        bool
            `True` for `SIGKILL`.
        """
        return True if signum == signal.SIGKILL else real(self, signum)

    monkeypatch.setattr(QueryChild, "_signal", pretend)
    failure, _seconds = _failure("error_after_closing_stdout", kill_grace_seconds=0.0)
    assert failure.error == ErrorDocument("invalid", "Query failed: x")
    assert (failure.returncode, failure.own_kill) == (3, False)
