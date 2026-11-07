"""A scripted stand-in for the query child: ``python scripted_child.py <behaviour>``.

It reads the request from standard input like the real child and then misbehaves in the one
way its argument names. Used by `test_query_process.py`, `test_executor.py` and the pipeline
tests; it imports nothing of the project.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time

EOS = b"\xff\xff\xff\xff\x00\x00\x00\x00"
ASKED = False


def _stream(rows: int, batches: int) -> bytes:
    """Build a complete Arrow IPC stream of `batches` batches of `rows` rows.

    Parameters
    ----------
    rows
        Rows per batch.
    batches
        Number of batches.

    Returns
    -------
    bytes
        The stream, with its end marker.
    """
    import pyarrow as pa  # imported late: the signal handling is set up before this costs time

    sink = pa.BufferOutputStream()
    schema = pa.schema([("i", pa.int64())])
    with pa.ipc.new_stream(sink, schema) as writer:
        for _ in range(batches):
            writer.write_batch(pa.record_batch([pa.array(range(rows), pa.int64())], schema=schema))
    return sink.getvalue().to_pybytes()


def _marker_stream(batches: int) -> bytes:
    """Build a stream whose first batch ends with eight bytes that equal the end marker.

    The last value of every batch is 4294967295, which little-endian is the marker.

    Parameters
    ----------
    batches
        Number of batches.

    Returns
    -------
    bytes
        The stream, with its end marker.
    """
    import pyarrow as pa

    sink = pa.BufferOutputStream()
    schema = pa.schema([("i", pa.int64())])
    with pa.ipc.new_stream(sink, schema) as writer:
        for _ in range(batches):
            writer.write_batch(
                pa.record_batch([pa.array([1, 4294967295], pa.int64())], schema=schema)
            )
    return sink.getvalue().to_pybytes()


def _write_error_document() -> None:
    """Write the error document of the query protocol to standard error."""
    sys.stderr.write(
        json.dumps({"lakana_query_error": 1, "kind": "invalid", "message": "Query failed: x"})
        + "\n"
    )
    sys.stderr.flush()


def _is_open(fd: int) -> bool:
    """Say whether a descriptor is open in this process.

    Parameters
    ----------
    fd
        The descriptor.

    Returns
    -------
    bool
        Whether it is open.
    """
    try:
        os.fstat(fd)
    except OSError:
        return False
    return True


def _note_the_ask(signum: int, frame: object) -> None:
    """Remember that the worker asked the child to stop.

    Parameters
    ----------
    signum
        The signal number.
    frame
        The interrupted frame.
    """
    global ASKED
    ASKED = True


def _set_up_signals(behaviour: str) -> None:
    """Install the one answer to ``SIGTERM`` that the behaviour names.

    Parameters
    ----------
    behaviour
        The scripted behaviour.
    """
    if behaviour == "ignores_sigterm":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    elif behaviour == "stops_when_asked":
        signal.signal(signal.SIGTERM, lambda signum, frame: os._exit(3))
    else:

        def finish(signum: int, frame: object) -> None:
            """Write a complete stream and exit with status 0.

            Parameters
            ----------
            signum
                The signal number.
            frame
                The interrupted frame.
            """
            out = sys.stdout.buffer
            out.write(_stream(10, 3))
            out.flush()
            os._exit(0)

        signal.signal(signal.SIGTERM, finish)


def main() -> None:
    """Behave as the first argument says."""
    behaviour = sys.argv[1]
    if behaviour == "blocked_when_asked":
        # Like the real child: it notes the ask and acts on it between writes.
        signal.signal(signal.SIGTERM, _note_the_ask)
        sys.stderr.write("READY\n")
        sys.stderr.flush()
    if behaviour in ("ignores_sigterm", "stops_when_asked", "finishes_when_asked"):
        # Before anything slow: a test that waits for READY knows the child answers the ask.
        _set_up_signals(behaviour)
        sys.stderr.write("READY\n")
        sys.stderr.flush()
    if behaviour == "closes_stdout_without_reading":
        os.close(1)  # the output ends and the request is never taken
        time.sleep(0.5)
        os._exit(0)
    if behaviour == "never_reads_stdin":
        time.sleep(60)
    if behaviour == "exits_without_reading":
        os._exit(5)
    request = json.loads(sys.stdin.buffer.read())
    out = sys.stdout.buffer
    if behaviour == "ok":
        out.write(_stream(10, 3))
    elif behaviour in ("usage", "usage_symlink"):
        # Like the real child's end: the figures it measured, in the file next to it (argv[2]).
        if behaviour == "usage":
            with open("usage.json", "w") as handle:
                handle.write(sys.argv[2])
        else:
            os.symlink("/etc/hostname", "usage.json")
        out.write(_stream(10, 3))
    elif behaviour == "echo_request":
        sys.stderr.write(json.dumps({"request": request, "env": dict(os.environ)}) + "\n")
        sys.stderr.write(json.dumps({"fds": [fd for fd in range(64) if _is_open(fd)]}) + "\n")
        out.write(_stream(1, 1))
    elif behaviour == "no_end_marker":
        out.write(_stream(10, 3)[: -len(EOS)])
    elif behaviour == "cut_mid_batch":
        out.write(_stream(1000, 3)[:-500])
    elif behaviour == "garbage":
        out.write(b"this is not an Arrow stream\n" * 40)
    elif behaviour == "garbage_with_end_marker":
        out.write(b"this is not an Arrow stream\n" * 40 + EOS)
    elif behaviour == "trailing_bytes":
        out.write(_stream(10, 3) + b"something after the end\n")
    elif behaviour == "exit_7_after_a_full_stream":
        out.write(_stream(10, 3))
        out.flush()
        os._exit(7)
    elif behaviour == "full_stream_then_killed":
        # Every batch and the end marker are handed over; then a signal from outside ends it.
        out.write(_stream(10, 3))
        out.flush()
        os.kill(os.getpid(), signal.SIGKILL)
    elif behaviour == "error_document":
        sys.stderr.write("noise from an extension\n")
        sys.stderr.write(
            json.dumps({"lakana_query_error": 1, "kind": "invalid", "message": "Query failed: x"})
            + "\n"
        )
        sys.stderr.flush()
        os._exit(3)
    elif behaviour in ("internal_document", "invalid_document_with_text"):
        # 5,000 bytes of traceback-like output (a control character in it), then the document.
        sys.stderr.write("FIRST-LINE-OUT-OF-BOUND\n" + ("File x.py, line 1\x1b[31m\n" * 250))
        kind = "internal" if behaviour == "internal_document" else "invalid"
        sys.stderr.write(
            json.dumps({"lakana_query_error": 1, "kind": kind, "message": "Query failed: x"}) + "\n"
        )
        sys.stderr.flush()
        os._exit(3)
    elif behaviour == "stderr_line":
        # 100 kB of other output (a traceback, say), then the line given as the next argument.
        sys.stderr.write(("a traceback line\n" * 6000)[:100_000] + "\n")
        sys.stderr.write(sys.argv[2] + "\n")
        sys.stderr.flush()
        os._exit(3)
    elif behaviour == "loud_stderr":
        sys.stderr.write("x" * (1024 * 1024))
        out.write(_stream(10, 3))
    elif behaviour in ("ignores_sigterm", "stops_when_asked", "finishes_when_asked"):
        time.sleep(60)
    elif behaviour == "marker_shaped_data":
        out.write(_marker_stream(3))
    elif behaviour == "cut_after_marker_shaped_data":
        out.write(_marker_stream(1)[: -len(EOS)])
        out.flush()
        os._exit(9)
    elif behaviour == "blocked_when_asked":
        block = b"\0" * 65536
        while not ASKED:
            out.write(block)
            out.flush()
        os._exit(3)
    elif behaviour == "clean_exit_with_a_pipe_holder":
        out.write(_stream(10, 3))
        out.flush()
        pid = os.fork()
        if pid == 0:
            os.setsid()  # out of the group, still holding the pipes
            # Lives until the test creates the release file (argv[2]); never a fixed sleep,
            # which a loaded runner can outlast. The long bound only stops a leaked holder.
            release = sys.argv[2]
            give_up = time.monotonic() + 300
            while not os.path.exists(release) and time.monotonic() < give_up:
                time.sleep(0.05)
            os._exit(0)
        sys.stderr.write(json.dumps({"escaped": pid}) + "\n")
        sys.stderr.flush()
        os._exit(0)
    elif behaviour == "error_after_closing_stdout":
        _write_error_document()
        os.close(1)  # the output ends first; the exit follows a moment later
        time.sleep(0.3)
        os._exit(3)
    elif behaviour == "error_after_closing_stdout_late":
        os.close(1)
        time.sleep(0.3)
        _write_error_document()  # written after the output ended, the exit follows at once
        os._exit(3)
    elif behaviour == "batch_then_error":
        out.write(_stream(10, 1)[: -len(EOS)])
        out.flush()
        time.sleep(0.3)
        _write_error_document()
        os._exit(3)
    elif behaviour == "closes_stdout_then_floods_stderr":
        os.close(1)
        sys.stderr.write("x" * (1024 * 1024))
        sys.stderr.flush()
    elif behaviour == "two_streams":
        out.write(_stream(10, 3) + _stream(10, 3))
    elif behaviour == "stream_then_marker":
        out.write(_stream(10, 3) + EOS)
    elif behaviour.startswith("flip_"):
        data = bytearray(_stream(10, 3))
        bit = int(behaviour.removeprefix("flip_"))
        data[bit // 8] ^= 1 << (bit % 8)
        out.write(bytes(data))
    elif behaviour == "closes_stdout_and_lingers":
        os.close(1)  # the output ends, the process does not
        time.sleep(1)
    elif behaviour == "floods":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        written = 0
        block = _stream(1000, 1)[: -len(EOS)]
        os.set_blocking(out.fileno(), False)
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            try:
                written += os.write(out.fileno(), block)
            except BlockingIOError:
                time.sleep(0.01)
        sys.stderr.write(json.dumps({"written": written}) + "\n")
        sys.stderr.flush()
        time.sleep(60)
    elif behaviour == "grows":
        # Resident memory in 10 MB steps, 300 MB at most or 20 seconds at most, then it ends by
        # itself: a test of the memory watchdog needs a child that really grows, with a bound.
        held = []
        grow_until = time.monotonic() + 20.0
        while len(held) < 30 and time.monotonic() < grow_until:
            block = bytearray(10 * 1024 * 1024)
            for offset in range(0, len(block), 4096):
                block[offset] = 1
            held.append(block)
            time.sleep(0.02)
        os._exit(0)
    elif behaviour == "leaves_a_grandchild":
        pid = os.fork()
        if pid == 0:
            time.sleep(60)
            os._exit(0)
        sys.stderr.write(json.dumps({"grandchild": pid}) + "\n")
        sys.stderr.flush()
        time.sleep(60)
    elif behaviour == "leaves_a_process_in_another_session":
        pid = os.fork()
        if pid == 0:
            os.setsid()  # out of the group the worker kills, still holding the pipes
            time.sleep(20)
            os._exit(0)
        sys.stderr.write(json.dumps({"escaped": pid}) + "\n")
        sys.stderr.flush()
        time.sleep(60)
    elif behaviour == "writes_a_temp_file":
        with open(os.path.join(os.environ["TMPDIR"], "spill.tmp"), "wb") as handle:
            handle.write(b"x" * 1000)
        sys.stderr.write(os.environ["TMPDIR"] + "\n")
        sys.stderr.flush()
        time.sleep(60)
    out.flush()


if __name__ == "__main__":
    main()
