"""Tests for the query child: in this process for every branch, and as a real process."""

from __future__ import annotations

import atexit
import contextlib
import datetime as dt
import io
import json
import os
import posixpath
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from lakana_worker import query_child
from lakana_worker.ca_bundle import query_ca_bundle
from lakana_worker.query_protocol import (
    CHECK,
    DESCRIBE,
    END_OF_STREAM,
    EXIT_FAILED,
    EXIT_OK,
    EXIT_REFUSED,
    MAX_REQUEST_BYTES,
    MEMORY_LIMIT_ADVICE,
    NO_RESULT_MESSAGE,
    READ,
    STORE_CERTIFICATE_UNVERIFIED,
    WRITE,
    ErrorDocument,
    Request,
    decode_error,
    encode_request,
)

SCRATCH = tempfile.mkdtemp(prefix="lakana-child-test-")
"""A directory that exists for every request's `temp_directory`; the profiler opens a file in it."""
atexit.register(shutil.rmtree, SCRATCH, ignore_errors=True)
MODULE = "lakana_worker.query_child"
CHILD_ARGV = [sys.executable, "-I", "-B", "-m", MODULE]
EXTENSIONS = str(Path.home() / ".duckdb" / "extensions")
SLOW_STREAM = "SELECT a.range FROM range(60000) a, range(60000) b WHERE a.range + b.range < 0"
"""A statement that streams and produces no row: the reader waits inside DuckDB for its first
batch, at a flat 90 MB, for about 9 s at two threads. Then it ends by itself, which is the
bound of every test that uses it should the stop under test fail."""
WIDE_ROWS = "SELECT i, repeat('x', 50) AS s FROM range(3000000) AS t(i)"
"""About 200 MB of stream that the child never holds in memory; no test reads it to the end."""
OVER_THE_LIMIT = (
    "SELECT max(rn) AS n FROM (SELECT row_number() OVER (ORDER BY b DESC) AS rn FROM "
    "(SELECT i, repeat('x', 100) || i::VARCHAR AS b FROM range(200000) AS r(i)))"
)
"""A sort of about 22 MB of rows, which cannot be held at a 32MB limit and must spill."""


def _request(sql: str = "SELECT 42 AS answer", **overrides: object) -> Request:
    """Build a request for the real child.

    Parameters
    ----------
    sql
        The statement.
    overrides
        Fields to replace.

    Returns
    -------
    Request
        The request.
    """
    fields: dict[str, Any] = {
        "mode": READ,
        "sql": sql,
        "parameters": {},
        "read_prefixes": (),
        "ca_bundle": query_ca_bundle(),
        "spill": False,
        "memory_limit": "200MB",
        "threads": 2,
        "temp_directory": SCRATCH,
        "max_temp_directory_size": "1GB",
        "batch_rows": 2048,
        "extension_directory": EXTENSIONS,
        "parent_pid": os.getpid(),
    }
    fields.update(overrides)
    return Request(**fields)


def _main(
    request: Request | bytes,
    asked: threading.Event | None = None,
    parent: int | None = None,
    usage_path: str | None = None,
) -> tuple[int, bytes, str]:
    """Run `query_child.main` in this process; return the status, the stream and stderr.

    Parameters
    ----------
    request
        The request, or raw bytes for standard input.
    asked
        The ask flag; a fresh one by default.
    parent
        The parent process id to report; this process by default.
    usage_path
        Where `main` writes the figures the child measured; nowhere by default.

    Returns
    -------
    tuple
        The exit status, the stream and standard error.
    """
    raw = request if isinstance(request, bytes) else encode_request(request)
    stream, stderr = io.BytesIO(), io.StringIO()
    code = query_child.main(
        io.BytesIO(raw),
        stream,
        stderr,
        asked or threading.Event(),
        parent=lambda: os.getpid() if parent is None else parent,
        usage_path=usage_path,
    )
    return code, stream.getvalue(), stderr.getvalue()


def _table(stream: bytes) -> pa.Table:
    """Read a complete Arrow IPC stream.

    Parameters
    ----------
    stream
        The bytes of the stream.

    Returns
    -------
    pa.Table
        The table.
    """
    assert stream.endswith(END_OF_STREAM)
    return pa.ipc.open_stream(stream).read_all()


def _failed(request: Request, asked: threading.Event | None = None) -> ErrorDocument:
    """Run a request that must fail and return its error document.

    Parameters
    ----------
    request
        The request.
    asked
        The ask flag.

    Returns
    -------
    ErrorDocument
        The document on standard error.
    """
    code, _stream, stderr = _main(request, asked)
    assert code == EXIT_FAILED
    error = decode_error(stderr.encode())
    assert error is not None
    return error


# --- in this process -------------------------------------------------------------------------


def test_a_read_writes_a_complete_stream_and_exits_0() -> None:
    code, stream, stderr = _main(_request())
    assert (code, stderr) == (EXIT_OK, "")
    assert _table(stream).to_pydict() == {"answer": [42]}


def test_the_statement_reads_every_value_as_a_variable() -> None:
    """Each value the platform sends arrives as a DuckDB variable of its own type."""
    moment = dt.datetime(2026, 10, 8, 12, 30, tzinfo=dt.UTC)
    parameters = {
        "urls": ["/data/t/a", "/data/t/b"],
        "n": 7,
        "d": Decimal("1.50"),
        "day": dt.date(2026, 10, 8),
        "naive": dt.datetime(2026, 10, 8, 12, 30),
        "aware": moment,
    }
    sql = (
        "SELECT len(getvariable('urls')) AS urls, getvariable('n') + 1 AS n, "
        "getvariable('d') * 2 AS d, getvariable('day') + 1 AS day, "
        "getvariable('naive') < getvariable('naive') + INTERVAL 1 HOUR AS naive, "
        "epoch(getvariable('aware')) AS aware"
    )
    code, stream, _stderr = _main(
        _request(sql, parameters=parameters, mode=WRITE, read_prefixes=("/data/t/",))
    )
    assert code == EXIT_OK
    assert _table(stream).to_pylist() == [
        {
            "urls": 2,
            "n": 8,
            "d": Decimal("3.00"),
            "day": dt.date(2026, 10, 9),
            "naive": True,
            "aware": moment.timestamp(),
        }
    ]


def test_a_variable_read_streams_in_more_than_one_batch(
    tmp_path: Path,
) -> None:
    """A million rows read through a files variable arrive as a stream of several batches."""
    path = tmp_path / "rows.parquet"
    duckdb.sql(
        "COPY (SELECT i, repeat('x', 50) || i::VARCHAR AS s FROM range(1000000) AS t(i)) "
        f"TO '{path}' (FORMAT parquet)"
    )
    request = _request(
        "SELECT * FROM read_parquet(getvariable('v_0_files'))",
        parameters={"v_0_files": [str(path)]},
        read_prefixes=(f"{tmp_path}/",),
        spill=True,
        memory_limit="32MB",
        threads=1,
    )

    code, stream, stderr = _main(request)

    assert (code, stderr) == (EXIT_OK, "")
    batches = list(pa.ipc.open_stream(stream))
    assert len(batches) > 1
    assert sum(batch.num_rows for batch in batches) == 1_000_000


def test_the_statement_runs_without_parameters(monkeypatch: pytest.MonkeyPatch) -> None:
    """`sql` is called with the statement alone: given parameters it holds the whole result.

    DuckDB does not count a materialised result against `memory_limit`, so no memory limit can
    show that regression; the call itself is what is checked.
    """
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    real_connect = duckdb.connect

    class Spy:
        """A connection that records how `sql` is called."""

        def __init__(self) -> None:
            self.inner = real_connect(":memory:")

        def sql(self, *args: object, **kwargs: object) -> object:
            calls.append((args, kwargs))
            return self.inner.sql(*args, **kwargs)  # ty: ignore[invalid-argument-type]

        def __getattr__(self, name: str) -> object:
            return getattr(self.inner, name)

    monkeypatch.setattr(query_child.duckdb, "connect", lambda *_a, **_k: Spy())
    request = _request(
        "SELECT len(getvariable('v_0_files')) AS n",
        parameters={"v_0_files": ["/data/t/a", "/data/t/b"]},
        read_prefixes=("/data/t/",),
    )

    code, stream, stderr = _main(request)

    assert (code, stderr) == (EXIT_OK, "")
    assert _table(stream).to_pylist() == [{"n": 2}]
    assert calls == [((request.sql,), {})]


def test_a_failing_statement_never_echoes_a_value_it_was_given(tmp_path: Path) -> None:
    """The URL is a variable's value, never SQL text, so DuckDB's error cannot quote it."""
    path = tmp_path / "X-Amz-Signature=SECRET123.parquet"
    duckdb.sql(f"COPY (SELECT 1 AS id) TO '{path}' (FORMAT parquet)")
    request = _request(
        "SELECT nope FROM read_parquet(getvariable('v_0_files'))",
        parameters={"v_0_files": [str(path)]},
        read_prefixes=(f"{tmp_path}/",),
        spill=True,
    )

    code, _stream, stderr = _main(request)

    error = decode_error(stderr.encode())
    assert code == EXIT_FAILED
    assert error is not None
    assert 'Referenced column "nope" not found' in error.message
    assert "SECRET123" not in stderr


def test_a_variable_name_that_is_not_an_identifier_is_never_written_into_sql() -> None:
    """Names are written into `SET VARIABLE`, where DuckDB takes no bound name."""
    connection = duckdb.connect(":memory:")
    with pytest.raises(ValueError, match="not an identifier"):
        query_child.set_variables(connection, {'x" = 1; SELECT 1; --': ["a"]})
    assert connection.execute("SELECT count(*) FROM duckdb_variables()").fetchone() == (0,)


def test_a_write_and_a_describe_run_in_utc_and_a_read_in_the_session_zone() -> None:
    sql = "SELECT TIMESTAMPTZ '2026-10-08 12:00:00+02' AS t"
    session_zone = duckdb.connect().execute("SELECT current_setting('TimeZone')").fetchall()[0]
    read = _table(_main(_request(sql))[1])
    write = _table(_main(_request(sql, mode=WRITE))[1])
    describe = _table(_main(_request(f"SELECT * FROM ({sql}) LIMIT 0", mode=DESCRIBE))[1])
    assert read.schema.field("t").type == pa.timestamp("us", tz=session_zone[0])
    assert write.schema.field("t").type == pa.timestamp("us", tz="UTC")
    assert describe.schema.field("t").type == pa.timestamp("us", tz="UTC")
    assert describe.num_rows == 0


_SETTINGS = (
    "s3_access_key_id",
    "s3_secret_access_key",
    "s3_session_token",
    "allow_persistent_secrets",
    "autoinstall_known_extensions",
    "autoload_known_extensions",
    "allow_community_extensions",
    "allow_extension_repositories",
    "extension_directories",
    "threads",
    "async_threads",
    "temp_directory",
    "max_temp_directory_size",
    "memory_limit",
    "enable_external_access",
    "allowed_directories",
    "allowed_paths",
    "ca_cert_file",
    "enable_server_cert_verification",
    "lock_configuration",
)


def _configured(
    request: Request, statements: list[str] | None = None
) -> tuple[duckdb.DuckDBPyConnection, dict[str, object]]:
    """Configure a real connection as the child does and read every setting back.

    The connection is opened with `query_child.connection_config`, as `Session.open` does.
    Settings are read with `current_setting`: DuckDB 2 no longer lists the ``s3_*`` keys in
    `duckdb_settings()`.

    Parameters
    ----------
    request
        The request.
    statements
        When given, filled with every statement `configure` sent, in order.

    Returns
    -------
    tuple of (duckdb.DuckDBPyConnection, dict)
        The configured connection (the caller closes it) and each setting's value.
    """
    connection = duckdb.connect(":memory:", config=query_child.connection_config(request))
    spy = _Spy(connection)
    query_child.configure(spy, request)  # ty: ignore[invalid-argument-type]
    if statements is not None:
        statements.extend(spy.executed)
    return connection, _settings_of(connection)


def _settings_of(connection: duckdb.DuckDBPyConnection) -> dict[str, object]:
    """Read every setting of `_SETTINGS` with `current_setting`.

    Parameters
    ----------
    connection
        The connection.

    Returns
    -------
    dict
        Each setting's value.
    """
    values = connection.execute(
        "SELECT " + ", ".join(f"current_setting('{name}')" for name in _SETTINGS)
    ).fetchall()[0]
    return dict(zip(_SETTINGS, values, strict=True))


def test_the_connection_holds_no_credential_and_loads_nothing_by_itself(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAAMBIENT")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "ambient-secret")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "ambient-token")
    spill = tmp_path / "spill"
    spill.mkdir()
    statements: list[str] = []
    connection, settings = _configured(
        _request(
            read_prefixes=("/data/t1/", "http://127.0.0.1:9/b/t2/"), temp_directory=str(spill)
        ),
        statements,
    )
    connection.close()
    settings["allowed_directories"] = sorted(cast("list[str]", settings["allowed_directories"]))
    assert settings == {
        "s3_access_key_id": "",
        "s3_secret_access_key": "",
        "s3_session_token": "",
        "allow_persistent_secrets": False,
        "autoinstall_known_extensions": False,
        "autoload_known_extensions": False,
        "allow_community_extensions": False,
        "allow_extension_repositories": "forbidden",
        "extension_directories": [EXTENSIONS],
        "threads": 2,
        # Four I/O threads per processor thread, not DuckDB 2's default of one per core.
        "async_threads": 8,
        "temp_directory": str(spill),
        "max_temp_directory_size": "0 bytes",
        "memory_limit": "190.7 MiB",
        "enable_external_access": False,
        # DuckDB adds the temp directory itself when external access is switched off.
        "allowed_directories": sorted([f"{spill}/", "/data/t1/", "http://127.0.0.1:9/b/t2/"]),
        "allowed_paths": [query_ca_bundle()],
        "ca_cert_file": query_ca_bundle(),
        "enable_server_cert_verification": True,
        "lock_configuration": True,
    }
    assert statements.index("INSTALL httpfs") > statements.index("SET extension_directories = ?")
    assert (
        statements.index("LOAD httpfs")
        < statements.index("SET enable_server_cert_verification = true")
        < statements.index("SET enable_external_access = false")
    )
    assert statements[-1] == "SET lock_configuration = true"


def test_local_mode_may_spill_up_to_the_configured_size(tmp_path: Path) -> None:
    connection, settings = _configured(_request(spill=True, temp_directory=str(tmp_path)))
    connection.close()
    assert settings["max_temp_directory_size"] == "953.6 MiB"


@pytest.mark.parametrize(
    "statement",
    [
        "SET http_proxy = 'http://127.0.0.1:9'",
        "SET extra_http_headers = MAP {'a': 'b'}",
        "SET ca_cert_file = '/etc/passwd'",
        "SET enable_server_cert_verification = false",
        "SET enable_external_access = true",
        "SET allowed_directories = ['/']",
        "SET allowed_paths = ['/etc/passwd']",
        "SET temp_directory = '/'",
        "SET memory_limit = '64GB'",
        "SET threads = 64",
        "SET max_temp_directory_size = '1TB'",
        "SET lock_configuration = false",
        "RESET threads",
        "RESET memory_limit",
        "PRAGMA threads = 8",
        "PRAGMA memory_limit = '64GB'",
        "SET GLOBAL threads = 8",
        "SET SESSION memory_limit = '64GB'",
    ],
)
def test_no_setting_can_be_changed_once_configured(tmp_path: Path, statement: str) -> None:
    """The lock is the second layer behind the guard, which refuses `SET` itself."""
    connection, before = _configured(_request(temp_directory=str(tmp_path)))
    try:
        with pytest.raises(duckdb.Error):
            connection.execute(statement)
        assert _settings_of(connection) == before
    finally:
        connection.close()


def test_a_variable_can_still_be_set_once_configured(tmp_path: Path) -> None:
    connection, _settings = _configured(_request(temp_directory=str(tmp_path)))
    try:
        query_child.set_variables(connection, {"v_0_files": ["/x/a"], "n": 3})
        assert connection.execute("SELECT getvariable('n')").fetchall() == [(3,)]
    finally:
        connection.close()


def test_the_session_opens_its_connection_with_the_thread_counts_of_the_request() -> None:
    """`async_threads` exists only as a connection option: `SET` is refused once configured."""
    session = query_child.Session()
    connection = session.open(query_child.connection_config(_request(threads=3)))
    try:
        assert connection.execute(
            "SELECT current_setting('threads'), current_setting('async_threads')"
        ).fetchall() == [(3, 12)]
    finally:
        session.close()


def test_a_child_reads_back_the_settings_duckdb_2_names_differently() -> None:
    """The child's own connection, not a copy of its set-up, carries the DuckDB 2 settings."""
    sql = (
        "SELECT CAST(current_setting('async_threads') AS BIGINT) AS a, "
        "current_setting('extension_directories') AS e, "
        "current_setting('allow_extension_repositories') AS r"
    )
    code, stream, stderr = _main(_request(sql))
    assert (code, stderr) == (EXIT_OK, "")
    assert _table(stream).to_pylist() == [{"a": 8, "e": [EXTENSIONS], "r": "forbidden"}]


def test_a_check_accepts_good_sizes_with_an_empty_stream() -> None:
    code, stream, stderr = _main(_request("", mode=CHECK, extension_directory="/nowhere"))
    assert (code, stderr) == (EXIT_OK, "")
    assert _table(stream).num_columns == 0


@pytest.mark.parametrize("field", ["memory_limit", "max_temp_directory_size"])
def test_a_check_names_the_size_duckdb_cannot_read(field: str) -> None:
    error = _failed(_request("", mode=CHECK, **{field: "lots"}))
    assert error == ErrorDocument("invalid", field)


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (b"\xff not json", "query request: not a JSON document\n"),
        (b"[]", "query request: not a JSON object\n"),
    ],
)
def test_an_unreadable_request_is_refused_before_anything_runs(raw: bytes, message: str) -> None:
    assert _main(raw) == (EXIT_REFUSED, b"", message)


def test_a_child_whose_parent_is_not_the_worker_refuses_to_run() -> None:
    code, stream, stderr = _main(_request(), parent=1)
    assert (code, stream) == (EXIT_REFUSED, b"")
    assert stderr == "the worker that started this query is gone\n"


def test_a_syntax_error_is_about_the_query_and_never_echoes_the_sql() -> None:
    error = _failed(_request("SELEC secret_expression"))
    assert error.kind == "invalid"
    assert error.message.startswith("Query failed: Parser Error: ")
    assert "LINE" not in error.message
    assert "secret_expression" not in error.message


@pytest.mark.parametrize("sql", ["", "-- nothing"])
def test_sql_without_a_result_is_about_the_query(sql: str) -> None:
    assert _failed(_request(sql)) == ErrorDocument("invalid", NO_RESULT_MESSAGE)


def test_a_local_read_is_refused_without_naming_the_file() -> None:
    error = _failed(_request("SELECT * FROM read_csv('/etc/hostname')"))
    assert error == ErrorDocument("invalid", query_child.REFUSED_LOCATION)
    assert "hostname" not in error.message


@pytest.mark.parametrize("mode", [READ, DESCRIBE])
def test_a_query_over_the_limit_without_spill_is_out_of_memory_with_the_setting_to_raise(
    tmp_path: Path, mode: str
) -> None:
    error = _failed(
        _request(
            OVER_THE_LIMIT, mode=mode, memory_limit="32MB", threads=1, temp_directory=str(tmp_path)
        )
    )
    assert error == ErrorDocument(
        "out_of_memory",
        "Query failed: the query ran out of memory and cannot spill to disk. "
        f"{MEMORY_LIMIT_ADVICE}",
    )
    assert list(tmp_path.iterdir()) == []


def test_a_query_over_the_limit_with_spill_returns_its_result(tmp_path: Path) -> None:
    code, stream, stderr = _main(
        _request(
            OVER_THE_LIMIT,
            memory_limit="32MB",
            threads=1,
            temp_directory=str(tmp_path),
            spill=True,
        )
    )
    assert (code, stderr) == (EXIT_OK, "")
    assert _table(stream).to_pylist() == [{"n": 200000}]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM sniff_csv('/etc/hostname')",
        "SELECT count(*) FROM duckdb_extensions()",
        "SELECT * FROM read_csv('/etc/hostname')",
    ],
)
def test_a_refusal_at_execution_is_about_the_query_not_out_of_memory(sql: str) -> None:
    """Refused by the guard as well; here DuckDB's refusal comes while the statement runs."""
    assert _failed(_request(sql)) == ErrorDocument("invalid", query_child.REFUSED_LOCATION)


def test_temporary_files_are_listed_without_error() -> None:
    """``duckdb_temporary_files`` reads no file: refused by the guard, harmless here."""
    code, stream, _stderr = _main(_request("SELECT count(*) AS n FROM duckdb_temporary_files()"))
    assert code == EXIT_OK
    assert _table(stream).to_pylist() == [{"n": 0}]


def test_a_storage_failure_is_transient() -> None:
    error = _failed(
        _request(
            "SELECT * FROM read_parquet('http://127.0.0.1:9/bucket/t/none.parquet')",
            read_prefixes=("http://127.0.0.1:9/bucket/t/",),
        )
    )
    assert error.kind == "transient"
    assert error.message.startswith("Query failed: ")


def test_a_closed_https_port_is_a_transient_connection_error_not_the_ca_refusal() -> None:
    """Once the bundle is allowed, an HTTPS failure is the connection's, as for plain HTTP."""
    error = _failed(
        _request(
            "SELECT * FROM read_parquet('https://127.0.0.1:9/bucket/t/none.parquet')",
            read_prefixes=("https://127.0.0.1:9/bucket/t/",),
        )
    )
    assert error.kind == "transient"
    assert "certificate" not in error.message.lower()


def test_an_ask_that_arrives_before_the_statement_stops_it_from_starting() -> None:
    asked = threading.Event()
    asked.set()
    assert _failed(_request(SLOW_STREAM), asked).kind == "interrupted"


def test_an_ask_seen_after_configuring_ends_the_child_before_the_statement() -> None:
    """Without the main thread's interrupts, the check after configuring is what stops it.

    Through `main`, an ask that is already set also interrupts the connection, and that
    interrupt may land inside `configure`: which of the two paths ends the child then depends
    on timing, and so did the coverage of this one.
    """
    asked = threading.Event()
    asked.set()
    sink = io.BytesIO()
    error = query_child.serve(_request(SLOW_STREAM), sink, query_child.Session(), asked)
    assert error is not None
    assert error.kind == "interrupted"
    assert sink.getvalue() == b""


def test_an_ask_interrupts_a_statement_that_waits_inside_duckdb() -> None:
    asked = threading.Event()
    timer = threading.Timer(0.5, asked.set)
    timer.start()
    started = time.monotonic()
    try:
        error = _failed(_request(SLOW_STREAM), asked)
    finally:
        timer.cancel()
    assert error == ErrorDocument("interrupted", "The query stopped because it was asked to.")
    assert time.monotonic() - started < 5


class _BrokenSink(io.BytesIO):
    """A sink whose write fails from the n-th call on, as a pipe nobody reads does."""

    def __init__(self, good_writes: int, fail_flush: bool = False) -> None:
        super().__init__()
        self.left = good_writes
        self._fail_flush = fail_flush

    def write(self, data: object) -> int:
        """Fail once the allowed writes are used up.

        Parameters
        ----------
        data
            The bytes.

        Returns
        -------
        int
            The number of bytes written.

        Raises
        ------
        BrokenPipeError
            Once the allowed writes are used up.
        """
        if self.left <= 0:
            raise BrokenPipeError(32, "Broken pipe")
        self.left -= 1
        return super().write(data)  # ty: ignore[invalid-argument-type]

    def flush(self) -> None:
        """Fail if asked to.

        Raises
        ------
        BrokenPipeError
            When the sink was built to fail its flush.
        """
        if self._fail_flush:
            raise BrokenPipeError(32, "Broken pipe")


def _writes_of(sql: str) -> int:
    """Count the writes pyarrow makes to its sink for a statement that succeeds.

    Parameters
    ----------
    sql
        The statement.

    Returns
    -------
    int
        The number of `write` calls up to and including the end marker.
    """
    sink = _BrokenSink(10**9)
    assert query_child.serve(_request(sql), sink, query_child.Session(), threading.Event()) is None
    return 10**9 - sink.left


@pytest.mark.parametrize("sql", ["SELECT 1 AS i", "SELECT i FROM range(5000) AS t(i)"])
def test_a_reader_that_went_away_is_never_reported_as_a_storage_failure(sql: str) -> None:
    """A broken pipe is an `OSError`, like DuckDB's I/O errors; it must not look like one.

    The sink fails at every write there is, however many pyarrow makes for the statement.
    """
    total = _writes_of(sql)
    assert total >= 2
    for good_writes in range(total):
        error = query_child.serve(
            _request(sql, batch_rows=1000),
            _BrokenSink(good_writes),
            query_child.Session(),
            threading.Event(),
        )
        assert error == ErrorDocument("interrupted", "The worker stopped reading the result.")


def test_a_reader_that_went_away_before_the_flush_is_not_a_storage_failure() -> None:
    sink = _BrokenSink(10**9, fail_flush=True)
    request = _request("SELECT 1 AS i")
    error = query_child.serve(request, sink, query_child.Session(), threading.Event())
    assert error == ErrorDocument("interrupted", "The worker stopped reading the result.")


class _AskingSink(io.BytesIO):
    """A sink that sets an event once enough bytes were written, as an ask mid-stream does."""

    def __init__(self, asked: threading.Event, after_bytes: int) -> None:
        super().__init__()
        self._asked = asked
        self._after = after_bytes

    def write(self, data: object) -> int:
        """Write, then set the event once `after_bytes` bytes are in.

        Parameters
        ----------
        data
            The bytes.

        Returns
        -------
        int
            The number of bytes written.
        """
        written = super().write(data)  # ty: ignore[invalid-argument-type]
        if self.tell() >= self._after:
            self._asked.set()
        return written


def test_an_ask_after_the_first_batch_ends_the_stream_without_its_end_marker() -> None:
    asked = threading.Event()
    sink = _AskingSink(asked, after_bytes=8000)
    request = _request("SELECT i FROM range(10000) AS t(i)", batch_rows=1000)
    error = query_child.serve(request, sink, query_child.Session(), asked)
    assert error == ErrorDocument("interrupted", "The query stopped because it was asked to.")
    assert 8000 <= len(sink.getvalue()) < 40_000  # a batch is 8,000 bytes; the whole is 80,000
    assert not sink.getvalue().endswith(END_OF_STREAM)


def test_an_error_while_a_later_batch_is_fetched_is_classified_like_any_other() -> None:
    """Past the first batch DuckDB's error arrives as a plain `OSError` (see `_is_transient`).

    One thread: with several, DuckDB sometimes reports a sibling's interrupt instead.
    """
    sql = "SELECT CASE WHEN x > 150000 THEN error('boom') ELSE x END FROM range(200000) t(x)"
    code, stream, stderr = _main(_request(sql, threads=1))
    error = decode_error(stderr.encode())
    assert code == EXIT_FAILED
    assert error is not None and error.kind == "invalid" and "boom" in error.message
    assert not stream.endswith(END_OF_STREAM)


def _kind(exc: BaseException, asked: threading.Event | None = None) -> str:
    """Classify `exc` as the child does and return the kind.

    Parameters
    ----------
    exc
        The failure.
    asked
        The ask flag.

    Returns
    -------
    str
        The kind.
    """
    return query_child.failure(exc, asked or threading.Event()).kind


_DUCKDB_REFUSAL = (
    'Permission Error: Cannot access file "/srv/secret/s.csv" - file system operations are '
    "disabled by configuration"
)
_CA_REFUSAL = (
    "Permission Error: Cannot read a CA certificate bundle: file system access is restricted by "
    "configuration. Add one of /etc/ssl/certs/ca-certificates.crt to allowed_paths"
)


@pytest.mark.parametrize(
    ("exc", "kind"),
    [
        (OSError("IO Error: connection reset"), "transient"),
        (OSError("HTTP Error: HTTP GET error on 'x' (HTTP 503)"), "transient"),
        (OSError("connection reset by peer"), "transient"),
        (duckdb.IOException("IO Error: no such file"), "transient"),
        (duckdb.OutOfMemoryException("Out of Memory Error: failed to allocate"), "out_of_memory"),
        (OSError("Out of Memory Error: failed to allocate"), "out_of_memory"),
        (duckdb.InternalException("INTERNAL Error: assertion"), "internal"),
        (duckdb.FatalException("FATAL Error: database invalidated"), "internal"),
        (OSError("INTERNAL Error: assertion"), "internal"),
        (OSError("Invalid Input Error: x"), "invalid"),
        (OSError("Conversion Error: bad"), "invalid"),
        (OSError("Out of Range Error: bad"), "invalid"),
        (OSError("Not implemented Error: bad"), "invalid"),
        (duckdb.BinderException("Binder Error: no column"), "invalid"),
        (duckdb.CatalogException("Catalog Error: no table"), "invalid"),
        (duckdb.ParserException("Parser Error: syntax"), "invalid"),
        (duckdb.ConversionException("Conversion Error: bad"), "invalid"),
        (duckdb.ConstraintException("Constraint Error: dup"), "invalid"),
        (duckdb.InvalidInputException("Invalid Input Error: x"), "invalid"),
        (duckdb.PermissionException(_DUCKDB_REFUSAL), "invalid"),
        (OSError(_DUCKDB_REFUSAL), "invalid"),
        (duckdb.PermissionException(_CA_REFUSAL), "internal"),
        (OSError(_CA_REFUSAL), "internal"),
        (OSError("IO Error: " + _DUCKDB_REFUSAL), "transient"),
    ],
)
def test_the_kind_follows_the_duckdb_error_type(exc: BaseException, kind: str) -> None:
    assert _kind(exc) == kind


@pytest.mark.parametrize("exc", [duckdb.PermissionException, OSError])
def test_a_refused_path_or_url_is_never_named(exc: type[BaseException]) -> None:
    error = query_child.failure(exc(_DUCKDB_REFUSAL), threading.Event())
    assert error == ErrorDocument("invalid", query_child.REFUSED_LOCATION)
    assert "/srv/secret" not in error.message


def test_a_refused_ca_bundle_names_the_bundle_the_variables_and_the_page() -> None:
    error = query_child.failure(
        duckdb.PermissionException(_CA_REFUSAL), threading.Event(), ca_bundle="/etc/ca.pem"
    )
    assert error.kind == "internal"
    for part in ("/etc/ca.pem", "AWS_CA_BUNDLE", "SSL_CERT_FILE", query_child.NETWORK_SETTINGS):
        assert part in error.message


_SIGNED = "https://store.example:8333/bucket/t1/data/a.parquet?X-Amz-Signature=SECRETSIG"
_UNVERIFIED = (
    "IO Error: SSL peer certificate or SSH remote key was not OK error for HTTP HEAD to "
    f"'{_SIGNED}'"
)
"""DuckDB's text for a store certificate it cannot verify, the same on 1.5.5 and 2.0.0."""


@pytest.mark.parametrize("exc", [duckdb.IOException, OSError])
def test_an_unverifiable_store_certificate_is_internal_and_names_only_the_host(
    exc: type[BaseException],
) -> None:
    """Red before #277: classified ``transient`` (retried), with DuckDB's text and the URL."""
    error = query_child.failure(
        exc(_UNVERIFIED),
        threading.Event(),
        files=[_SIGNED],
        ca_bundle="/etc/ca.pem",
        prefixes=("https://store.example:8333/bucket/t1/",),
    )
    assert error.kind == "internal"
    assert error.message.startswith(STORE_CERTIFICATE_UNVERIFIED)
    for part in (
        "store.example:8333",
        "/etc/ca.pem",
        "AWS_CA_BUNDLE",
        "SSL_CERT_FILE",
        query_child.STORE_CERTIFICATES,
    ):
        assert part in error.message
    for secret in ("SECRETSIG", "X-Amz", "?", "/bucket", "a.parquet", "<file>"):
        assert secret not in error.message.replace(query_child.STORE_CERTIFICATES, "")


def test_an_unverifiable_certificate_of_a_host_the_request_does_not_name_is_not_named() -> None:
    """Only a host of the request's own prefixes is said; anything else is "the object store"."""
    error = query_child.failure(
        duckdb.IOException(_UNVERIFIED.replace("store.example", "evil.example")),
        threading.Event(),
        ca_bundle="/etc/ca.pem",
        prefixes=("https://store.example:8333/bucket/t1/",),
    )
    assert error.kind == "internal"
    assert "evil.example" not in error.message
    assert "the object store" in error.message


@pytest.mark.parametrize(
    ("text", "prefix"),
    [
        pytest.param(
            "IO Error: SSL peer certificate or SSH remote key was not OK",
            "https://store.example:8333/bucket/t1/",
            id="no URL quoted",
        ),
        pytest.param(
            _UNVERIFIED.replace("https://", "https://user@"),
            "https://user@store.example:8333/bucket/t1/",
            id="user information",
        ),
        pytest.param(
            "IO Error: SSL peer certificate or SSH remote key was not OK error for HTTP HEAD to "
            "'https://[::1/bucket/t1/a.parquet'",
            "https://[::1/bucket/t1/",
            id="a URL that does not parse",
        ),
    ],
)
def test_an_unverifiable_certificate_without_a_safe_host_says_only_the_object_store(
    text: str, prefix: str
) -> None:
    """A host is said only from a quoted URL that parses, without user information."""
    error = query_child.failure(
        duckdb.IOException(text), threading.Event(), ca_bundle="/etc/ca.pem", prefixes=(prefix,)
    )
    assert error.kind == "internal"
    assert error.message.startswith(f"{STORE_CERTIFICATE_UNVERIFIED} against ")
    assert "user" not in error.message
    assert "::1" not in error.message


def test_a_certificate_text_quoted_by_another_error_type_is_not_taken_for_one() -> None:
    """Only DuckDB's own ``IO Error`` counts: SQL can make an error of another type say it."""
    text = "Invalid Input Error: " + _UNVERIFIED
    assert query_child.failure(OSError(text), threading.Event()).kind == "invalid"


@pytest.mark.parametrize("exc", [duckdb.InvalidInputException, duckdb.BinderException])
def test_a_certificate_text_on_an_error_that_is_not_an_io_error_is_not_taken_for_one(
    exc: type[BaseException],
) -> None:
    """Only an `IOException` or an `OSError` counts, even when the text starts with ``IO Error``.

    Pins the class check beside the text match: SQL can make an error of another class carry
    any text, and without the check this text would be reported as the store's certificate.

    Parameters
    ----------
    exc
        The DuckDB error class that carries the text.
    """
    error = query_child.failure(
        exc(_UNVERIFIED),
        threading.Event(),
        files=[_SIGNED],
        ca_bundle="/etc/ca.pem",
        prefixes=("https://store.example:8333/bucket/t1/",),
    )

    assert error.kind == "invalid"
    assert not error.message.startswith(STORE_CERTIFICATE_UNVERIFIED)


def test_an_out_of_memory_message_names_the_setting_and_drops_duckdbs_own_advice() -> None:
    exc = duckdb.OutOfMemoryException(
        "Out of Memory Error: failed to allocate data of size 256.0 MiB (1/2 used)\n\n"
        "Possible solutions:\n* Reducing the number of threads (SET threads=X)"
    )
    error = query_child.failure(exc, threading.Event(), spill=True)
    assert error.message == (
        "Query failed: Out of Memory Error: failed to allocate data of size 256.0 MiB "
        f"(1/2 used) {MEMORY_LIMIT_ADVICE}"
    )


@pytest.mark.parametrize(
    "text",
    [
        "Out of Memory Error: failed to offload data block of size 192.0 KiB (0 bytes/0 used)",
        "Out of Memory Error: failed to allocate data of size 256.0 MiB (1/2 used)",
    ],
)
def test_without_spill_running_out_of_memory_is_decided_by_the_request_not_the_text(
    text: str,
) -> None:
    error = query_child.failure(duckdb.OutOfMemoryException(text), threading.Event())
    assert error == ErrorDocument("out_of_memory", query_child.NO_SPILL)


def test_an_unexpected_exception_becomes_a_document_without_its_text(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def explode(connection: object, request: object) -> None:
        raise RuntimeError("s3://bucket/" + "secret" + "-path")

    monkeypatch.setattr(query_child, "configure", explode)
    error = _failed(_request())
    assert error == ErrorDocument(
        "internal", "Query failed: the query process hit an unexpected error (RuntimeError)."
    )
    printed = capsys.readouterr().err
    assert "in explode" in printed and printed.rstrip().endswith("RuntimeError")
    assert "secret-path" not in printed


def test_a_connection_that_fails_to_close_does_not_stop_serve_from_answering() -> None:
    class Stubborn(query_child.Session):
        def close(self) -> None:
            """Fail as a broken connection might.

            Raises
            ------
            RuntimeError
                Always.
            """
            raise RuntimeError("cannot close")

    sink = io.BytesIO()
    assert query_child.serve(_request(), sink, Stubborn(), threading.Event()) is None
    assert _table(sink.getvalue()).to_pydict() == {"answer": [42]}


def test_a_request_over_the_limit_is_refused_and_never_read_beyond_it() -> None:
    reads: list[int] = []

    class Stdin(io.BytesIO):
        def read(self, size: int | None = -1) -> bytes:
            """Record the size asked for.

            Parameters
            ----------
            size
                How many bytes.

            Returns
            -------
            bytes
                More than the limit.
            """
            reads.append(-1 if size is None else size)
            return b" " * (MAX_REQUEST_BYTES + 1)

    stream, stderr = io.BytesIO(), io.StringIO()
    code = query_child.main(Stdin(), stream, stderr, threading.Event(), parent=os.getpid)
    assert (code, stderr.getvalue()) == (EXIT_REFUSED, "query request: larger than 64 MiB\n")
    assert reads == [MAX_REQUEST_BYTES + 1]


def test_interrupting_a_closed_session_does_nothing() -> None:
    session = query_child.Session()
    session.interrupt()
    session.open(query_child.connection_config(_request()))
    session.interrupt()
    session.close()
    session.close()
    session.interrupt()


def test_die_with_parent_asks_the_kernel_for_sigkill(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[int, int]] = []

    class Libc:
        def prctl(self, option: int, value: int) -> int:
            """Record the request.

            Parameters
            ----------
            option
                The prctl option.
            value
                Its value.

            Returns
            -------
            int
                0.
            """
            calls.append((option, value))
            return 0

    monkeypatch.setattr(query_child.sys, "platform", "linux")
    monkeypatch.setattr(query_child.ctypes, "CDLL", lambda name, use_errno: Libc())
    query_child.die_with_parent()
    assert calls == [(1, signal.SIGKILL)]


def test_die_with_parent_exits_when_the_kernel_refuses(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class Libc:
        def prctl(self, option: int, value: int) -> int:
            """Refuse.

            Parameters
            ----------
            option
                The prctl option.
            value
                Its value.

            Returns
            -------
            int
                -1.
            """
            return -1

    monkeypatch.setattr(query_child.sys, "platform", "linux")
    monkeypatch.setattr(query_child.ctypes, "CDLL", lambda name, use_errno: Libc())
    with pytest.raises(SystemExit) as caught:
        query_child.die_with_parent()
    assert caught.value.code == EXIT_REFUSED
    assert "could not ask the kernel" in capsys.readouterr().err


def test_die_with_parent_does_nothing_off_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(query_child.sys, "platform", "darwin")
    monkeypatch.setattr(query_child.ctypes, "CDLL", lambda name, use_errno: pytest.fail("called"))
    query_child.die_with_parent()


class _Exit(Exception):
    """Stands in for `os._exit` in `run_cli` tests."""


def _run_cli(monkeypatch: pytest.MonkeyPatch, main: object) -> tuple[int, list[str]]:
    """Run `run_cli` with the process-level calls recorded instead of made.

    Parameters
    ----------
    monkeypatch
        The fixture.
    main
        Stands in for `query_child.main`.

    Returns
    -------
    tuple
        The status passed to `os._exit` and the steps taken.
    """
    steps: list[str] = []

    def leave(code: int) -> None:
        raise _Exit(code)

    monkeypatch.setattr(query_child, "die_with_parent", lambda: steps.append("pdeathsig"))
    monkeypatch.setattr(
        query_child.signal, "signal", lambda signum, handler: steps.append(f"signal {signum}")
    )
    monkeypatch.setattr(query_child.os, "dup", lambda fd: steps.append(f"dup {fd}") or 99)
    monkeypatch.setattr(query_child.os, "dup2", lambda a, b: steps.append(f"dup2 {a} {b}"))
    monkeypatch.setattr(query_child.os, "fdopen", lambda fd, mode: io.BytesIO())
    monkeypatch.setattr(query_child.os, "_exit", leave)
    monkeypatch.setattr(query_child, "main", main)
    with pytest.raises(_Exit) as caught:
        query_child.run_cli()
    return caught.value.args[0], steps


def test_run_cli_prepares_the_process_in_order_and_exits_with_mains_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    given: dict[str, object] = {}

    def main(
        stdin: object, stream: object, stderr: object, asked: object, **options: object
    ) -> int:
        given.update(options)
        return 3

    code, steps = _run_cli(monkeypatch, main)
    assert code == 3
    assert given == {"usage_path": query_child.USAGE_FILE}
    assert steps == ["pdeathsig", f"signal {int(signal.SIGTERM)}", "dup 1", "dup2 2 1"]


def test_run_cli_reports_a_crash_and_still_ends_the_process(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def crash(stdin: object, stream: object, stderr: object, asked: object, **_: object) -> int:
        raise RuntimeError("boom")

    code, _steps = _run_cli(monkeypatch, crash)
    assert code == EXIT_REFUSED
    assert "RuntimeError: boom" in capsys.readouterr().err


# --- as a real process -----------------------------------------------------------------------


class _Real:
    """A real child started the way the worker starts it, always killed and reaped at the end."""

    def __init__(
        self,
        request_fields: dict[str, object],
        tmp_path: Path,
        code: str | None = None,
        zone: str | None = None,
        send: bool = True,
    ) -> None:
        self._request_fields = request_fields
        self.home, self.tmp = tmp_path / "home", tmp_path / "tmp"
        self.home.mkdir()
        self.tmp.mkdir()
        argv = CHILD_ARGV if code is None else [sys.executable, "-I", "-B", "-c", code]
        self.process = subprocess.Popen(
            argv,
            cwd=tmp_path,
            env={
                "PATH": os.environ["PATH"],
                "HOME": str(self.home),
                "TMPDIR": str(self.tmp),
                **({} if zone is None else {"TZ": zone}),
            },
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        if send:
            self.send()

    def send(self) -> None:
        """Write the request to the child and close its standard input.

        A child that cannot be given its request is killed and reaped before the error goes on,
        because the object is never entered and so never exited.
        """
        try:
            request = _request(
                temp_directory=str(self.tmp),
                **self._request_fields,  # ty: ignore[invalid-argument-type]
            )
            assert self.process.stdin is not None
            self.process.stdin.write(encode_request(request))
            self.process.stdin.close()
            # Python 3.11's communicate() flushes stdin even when it is closed, and raises.
            self.process.stdin = None
        except BaseException:
            self.__exit__()
            raise

    def read_at_least(self, size: int, timeout: float = 30.0) -> bytes:
        """Read from the child's standard output until `size` bytes are in, with a bound.

        Parameters
        ----------
        size
            How many bytes to read at least.
        timeout
            Seconds to wait at most.

        Returns
        -------
        bytes
            What was read, at least `size` bytes.
        """
        assert self.process.stdout is not None
        fd = self.process.stdout.fileno()
        data = b""
        deadline = time.monotonic() + timeout
        with selectors.DefaultSelector() as selector:
            selector.register(fd, selectors.EVENT_READ)
            while len(data) < size:
                left = deadline - time.monotonic()
                if left <= 0 or not selector.select(left):
                    pytest.fail(f"the child wrote {len(data)} of {size} bytes in {timeout} s")
                chunk = os.read(fd, 1 << 20)
                assert chunk, "the child closed its standard output early"
                data += chunk
        return data

    def __enter__(self) -> _Real:
        """Hand out the running child.

        Returns
        -------
        _Real
            The child.
        """
        return self

    def __exit__(self, *exc: object) -> None:
        """Kill the group and reap, whatever the test did."""
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(self.process.pid, signal.SIGKILL)
        self.process.wait(timeout=20)
        for pipe in (self.process.stdout, self.process.stderr):
            if pipe is not None:
                pipe.close()

    def end(self, timeout: float = 30.0) -> tuple[int, bytes, bytes]:
        """Wait for the child's end; return its status, standard output and standard error.

        Parameters
        ----------
        timeout
            Seconds to wait.

        Returns
        -------
        tuple
            The status, standard output and standard error.
        """
        out, err = self.process.communicate(timeout=timeout)
        return self.process.returncode, out, err


def test_the_real_child_answers_with_a_complete_stream(tmp_path: Path) -> None:
    sql = "SELECT i % 10 AS k, sum(i) AS s FROM range(5000) AS t(i) GROUP BY 1 ORDER BY 1"
    with _Real({"sql": sql}, tmp_path) as child:
        code, out, err = child.end()
    assert (code, err) == (EXIT_OK, b"")
    assert _table(out).num_rows == 10


def test_a_child_with_an_empty_home_loads_httpfs_from_the_workers_directory(
    tmp_path: Path,
) -> None:
    """Without `extension_directories` DuckDB looks under the empty HOME and tries to download.

    ``duckdb_extensions()`` lists the extension directory, which allow-list mode refuses; httpfs
    shows by the settings it adds.
    """
    sql = "SELECT count(*) > 0 AS n FROM duckdb_settings() WHERE name LIKE 's3_%'"
    with _Real({"sql": sql, "read_prefixes": (f"{tmp_path}/",)}, tmp_path) as child:
        code, out, err = child.end()
        assert (code, err) == (EXIT_OK, b"")
        assert _table(out).to_pydict() == {"n": [True]}
        assert list(child.home.iterdir()) == []


def test_a_read_is_exported_in_the_zone_of_the_childs_tz_variable(tmp_path: Path) -> None:
    """DuckDB takes its session zone from TZ, which is why the worker passes its own on."""
    sql = "SELECT TIMESTAMPTZ '2026-10-08 12:00:00+02' AS t"
    with _Real({"sql": sql}, tmp_path, zone="Asia/Tokyo") as child:
        code, out, _err = child.end()
    assert code == EXIT_OK
    assert _table(out).schema.field("t").type == pa.timestamp("us", tz="Asia/Tokyo")


LINUX = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc")


def test_the_real_child_stops_within_seconds_of_sigterm_while_it_waits_for_a_batch(
    tmp_path: Path,
) -> None:
    """The probe's finding: with the statement on the main thread this never returns.

    The child runs one statement only, so the test cannot have it write a marker first. It
    waits until the child has used half a second of processor time (`/proc`), which it spends
    inside DuckDB on the slow statement (start-up costs about 0.1 s); the statement runs about
    9 s, so the signal arrives while the child waits for its first batch.
    """
    with _Real({"sql": SLOW_STREAM}, tmp_path) as child:
        _wait_for_cpu(child.process.pid, seconds=0.5)
        asked = time.monotonic()
        os.killpg(child.process.pid, signal.SIGTERM)
        code, _out, err = child.end(timeout=20)
        seconds = time.monotonic() - asked
    assert code == EXIT_FAILED
    assert decode_error(err) == ErrorDocument(
        "interrupted", "The query stopped because it was asked to."
    )
    assert seconds < 5


def _wait_for_cpu(pid: int, seconds: float, timeout: float = 30.0) -> None:
    """Wait until a process has used `seconds` of processor time, or fail the test.

    Parameters
    ----------
    pid
        The process.
    seconds
        User plus system time to wait for.
    timeout
        Seconds to wait at most.

    Where there is no ``/proc`` (macOS) it waits 1.5 s instead, well inside the statement's 9 s.
    """
    if not Path(f"/proc/{pid}/stat").exists():
        time.sleep(1.5)
        return
    ticks = os.sysconf("SC_CLK_TCK")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        if (int(fields[11]) + int(fields[12])) / ticks >= seconds:
            return
        time.sleep(0.02)
    pytest.fail(f"the child did not use {seconds} s of processor time within {timeout} s")


def test_killing_the_group_mid_stream_leaves_no_process_no_end_marker_and_no_file(
    tmp_path: Path,
) -> None:
    with _Real({"sql": WIDE_ROWS}, tmp_path) as child:
        first = child.read_at_least(1024 * 1024)
        os.killpg(child.process.pid, signal.SIGKILL)
        code, rest, _err = child.end(timeout=20)
        members = subprocess.run(
            ["pgrep", "-g", str(child.process.pid)],
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        ).stdout.split()
    assert code == -signal.SIGKILL
    assert len(first) >= 1024 * 1024
    assert not (first + rest).endswith(END_OF_STREAM)
    assert members == []
    assert sorted(p.name for p in tmp_path.iterdir()) == ["home", "tmp"]
    assert list(child.tmp.iterdir()) == [] and list(child.home.iterdir()) == []


def _closed_early(tmp_path: Path, sql: str, read_first: int) -> tuple[int, bytes]:
    """Run a real child whose reader closes its end of standard output early.

    Parameters
    ----------
    tmp_path
        The test's directory.
    sql
        The statement.
    read_first
        Bytes to read before the close; 0 closes before the request is even sent.

    Returns
    -------
    tuple
        The child's exit status and its standard error.
    """
    with _Real({"sql": sql}, tmp_path, send=read_first > 0) as child:
        if read_first:
            child.read_at_least(read_first)
        assert child.process.stdout is not None
        child.process.stdout.close()
        child.process.stdout = None  # so that `communicate` reads standard error only
        if not read_first:
            child.send()
        _out, err = child.process.communicate(timeout=30)
    return child.process.returncode, err


class _Spy:
    """A real connection that records every statement string handed to `execute`.

    Parameters
    ----------
    real
        The connection to wrap.
    """

    def __init__(self, real: duckdb.DuckDBPyConnection) -> None:
        self._real = real
        self.executed: list[str] = []

    def execute(self, sql: str, parameters: object = None) -> duckdb.DuckDBPyConnection:
        """Record `sql`, then run it.

        Parameters
        ----------
        sql
            The statement text.
        parameters
            The bound values.

        Returns
        -------
        duckdb.DuckDBPyConnection
            The real connection, with the result.
        """
        self.executed.append(sql)
        return self._real.execute(sql, parameters)  # type: ignore[arg-type]

    def __getattr__(self, name: str) -> object:
        """Hand every other attribute to the real connection.

        Parameters
        ----------
        name
            The attribute.

        Returns
        -------
        object
            The real connection's attribute.
        """
        return getattr(self._real, name)


def test_a_statement_that_runs_out_of_memory_is_never_sent_again(tmp_path: Path) -> None:
    """Against real DuckDB: after the failure nothing more is sent, no ``EXPLAIN`` either.

    Planning a text again would run each statement of it a second time (``EXPLAIN a; b`` runs
    ``b``); the failure is classified from the request alone.
    """
    sql = (
        "CREATE TABLE c(i INT); INSERT INTO c VALUES (1); "
        "SELECT * FROM (SELECT i, repeat('x', 100) || i::VARCHAR AS b "
        "FROM range(200000) AS r(i)) ORDER BY b DESC"
    )
    request = _request(sql, memory_limit="32MB", threads=1, temp_directory=str(tmp_path))
    real = duckdb.connect(":memory:")
    spy = _Spy(real)
    try:
        query_child.configure(spy, request)  # ty: ignore[invalid-argument-type]
        sent = len(spy.executed)
        with pytest.raises(duckdb.OutOfMemoryException) as caught:
            reader = spy.execute(sql, None).to_arrow_reader(request.batch_rows)
            assert reader is not None
            for _batch in reader:
                pass
        document = query_child.failure(caught.value, threading.Event())
    finally:
        real.close()
    assert document == ErrorDocument("out_of_memory", query_child.NO_SPILL)
    assert spy.executed[sent:] == [sql]


@pytest.mark.parametrize(
    ("sql", "read_first", "document"),
    [
        (
            WIDE_ROWS,
            64 * 1024,
            ErrorDocument("interrupted", "The worker stopped reading the result."),
        ),
        (
            "SELECT 1 AS i",
            0,
            ErrorDocument("interrupted", "The worker stopped reading the result."),
        ),
        ("SELEC 1", 0, None),
    ],
)
def test_a_reader_that_closes_the_pipe_leaves_the_error_document_as_the_last_line(
    tmp_path: Path, sql: str, read_first: int, document: ErrorDocument | None
) -> None:
    """Closing the output stream would flush again, raise, and print a traceback behind it."""
    code, err = _closed_early(tmp_path, sql, read_first)
    assert code == EXIT_FAILED
    found = decode_error(err)
    assert found is not None, err.decode(errors="replace")
    if document is None:
        assert found.kind == "invalid"
    else:
        assert found == document
    assert b"Traceback" not in err


def test_what_is_written_to_descriptor_1_lands_on_standard_error_not_in_the_stream(
    tmp_path: Path,
) -> None:
    code_text = (
        f"import os, {MODULE} as child\n"
        "real = child.main\n"
        "def main(*args, **kwargs):\n"
        "    os.write(1, b'an extension prints here\\n')\n"
        "    print('and so does a stray print', flush=True)\n"
        "    return real(*args, **kwargs)\n"
        "child.main = main\n"
        "child.run_cli()\n"
    )
    with _Real({}, tmp_path, code=code_text) as child:
        code, out, err = child.end()
    assert code == EXIT_OK
    assert _table(out).to_pydict() == {"answer": [42]}
    assert err == b"an extension prints here\nand so does a stray print\n"


def test_importing_the_child_loads_nothing_of_the_core(tmp_path: Path) -> None:
    """Every import is paid by every query: `lakana_core.config` alone costs 130 ms."""
    code = (
        f"import sys, {MODULE}\n"
        "heavy = {'lakana_core', 'sqlalchemy', 'pydantic', 'pydantic_settings', 'pyiceberg'}\n"
        "print(sorted(heavy & {name.split('.')[0] for name in sys.modules}))\n"
    )
    done = subprocess.run(
        [sys.executable, "-I", "-B", "-c", code],
        capture_output=True,
        text=True,
        check=True,
        cwd=tmp_path,
        timeout=60,
    )
    assert done.stdout.strip() == "[]"


def _handles_sigterm(pid: int) -> bool:
    """Say whether a process has installed a handler for SIGTERM (Linux).

    Parameters
    ----------
    pid
        The process id.

    Returns
    -------
    bool
        Whether the signal is caught.
    """
    status = Path(f"/proc/{pid}/status").read_text()
    caught = next(line for line in status.splitlines() if line.startswith("SigCgt:"))
    return bool(int(caught.split()[1], 16) >> (signal.SIGTERM - 1) & 1)


def _running(pid: int) -> bool:
    """Say whether a process exists and is not a zombie.

    Parameters
    ----------
    pid
        The process id.

    Returns
    -------
    bool
        Whether it runs.
    """
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()[0]
    except OSError:
        return False
    return state != "Z"


LONG_STREAM = "SELECT a.range FROM range(200000) a, range(200000) b WHERE a.range + b.range < 0"
"""The same shape as `SLOW_STREAM`, long enough by construction (minutes) that the statement is
still running whenever the test looks; the test ends it, and `finally` kills the group."""


@LINUX
def test_the_child_dies_when_its_worker_is_killed() -> None:
    """A stand-in worker starts the real child and is then killed with SIGKILL."""
    worker = (
        "import json, os, subprocess, sys, time\n"
        "request = json.loads(sys.argv[1]); request['parent_pid'] = os.getpid()\n"
        "child = subprocess.Popen(json.loads(sys.argv[2]), stdin=subprocess.PIPE,\n"
        "    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)\n"
        "child.stdin.write(json.dumps(request).encode()); child.stdin.close()\n"
        "print(child.pid, flush=True)\n"
        "time.sleep(60)\n"
    )
    request = json.loads(encode_request(_request(LONG_STREAM)))
    process = subprocess.Popen(
        [sys.executable, "-c", worker, json.dumps(request), json.dumps(CHILD_ARGV)],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    child_pid = int(process.stdout.readline())
    try:
        deadline = time.monotonic() + 30
        while not _handles_sigterm(child_pid):  # `run_cli` has prepared the process
            assert _running(child_pid), "the child ended before it was ready"
            assert time.monotonic() < deadline, "the child never became ready"
            time.sleep(0.01)
        process.kill()
        process.wait(timeout=10)
        deadline = time.monotonic() + 2
        while _running(child_pid) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not _running(child_pid)
    finally:
        process.kill()
        process.wait(timeout=10)
        process.stdout.close()
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(child_pid, signal.SIGKILL)


def test_a_child_that_cannot_be_given_its_request_is_reaped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[subprocess.Popen[bytes]] = []
    leave = _Real.__exit__

    def spy(self: _Real, *exc: object) -> None:
        seen.append(self.process)
        leave(self, *exc)

    def refuse(request: object) -> bytes:
        raise RuntimeError("no request today")

    monkeypatch.setattr(_Real, "__exit__", spy)
    monkeypatch.setattr(sys.modules[__name__], "encode_request", refuse)
    with pytest.raises(RuntimeError, match="no request today"):
        _Real({}, tmp_path)
    assert len(seen) == 1 and seen[0].poll() is not None


_LOST_CAUSE = (
    "The statement failed, but DuckDB reported an interruption instead of the cause. "
    "Run it again to see the error."
)


@pytest.mark.parametrize(
    "exc",
    [OSError("INTERRUPT Error: Interrupted!"), duckdb.InterruptException("Interrupted!")],
    ids=["from the fetch", "from execute"],
)
def test_an_interrupt_nobody_asked_for_says_that_the_cause_was_lost(exc: BaseException) -> None:
    """DuckDB can report a sibling thread's interrupt instead of the error."""
    error = query_child.failure(exc, threading.Event())
    assert (error.kind, error.message) == ("invalid", _LOST_CAUSE)


def test_an_interrupt_that_was_asked_for_keeps_its_wording() -> None:
    asked = threading.Event()
    asked.set()
    error = query_child.failure(OSError("INTERRUPT Error: Interrupted!"), asked)
    assert error == ErrorDocument("interrupted", "The query stopped because it was asked to.")


def test_other_errors_are_not_taken_for_a_lost_cause() -> None:
    error = query_child.failure(OSError("Conversion Error: boom"), threading.Event())
    assert error.message == "Query failed: Conversion Error: boom"


def test_a_field_id_map_is_set_as_a_duckdb_map() -> None:
    """The map has the shape `read_parquet(schema = ...)` takes on DuckDB 1.5."""
    sql = (
        "SELECT map_keys(getvariable('v_0_schema')) AS k, "
        "getvariable('v_0_schema')[2].type AS t, "
        "getvariable('v_0_schema')[2].default_value IS NULL AS d"
    )
    request = _request(sql, parameters={"v_0_schema": ((1, "id", "BIGINT"), (2, "n", "VARCHAR"))})

    code, stream, _stderr = _main(request)

    assert code == EXIT_OK
    assert _table(stream).to_pylist() == [{"k": [1, 2], "t": "VARCHAR", "d": True}]


@pytest.mark.parametrize(
    "fields",
    [
        pytest.param(((1, "id", "NOT A TYPE"),), id="a type DuckDB cannot parse"),
        pytest.param(((1, "id", "STRUCT(a"),), id="a type that is cut off"),
    ],
)
def test_a_schema_map_duckdb_cannot_parse_fails_closed(
    tmp_path: Path, fields: tuple[tuple[int, str, str], ...]
) -> None:
    """DuckDB's refusal of the map ends the query as an ordinary failure naming no file."""
    path = _field_id_file(tmp_path, "X-Amz-Signature=SECRET123.parquet", "abc")
    request = _request(
        "SELECT * FROM read_parquet(getvariable('v_0_files'), schema = getvariable('v_0_schema'))",
        parameters={"v_0_files": [str(path)], "v_0_schema": fields},
        read_prefixes=(f"{tmp_path}/",),
        spill=True,
    )

    code, stream, stderr = _main(request)

    error = decode_error(stderr.encode())
    assert code == EXIT_FAILED
    assert error is not None
    assert error.kind == "invalid"
    assert error.message.startswith("Query failed: ")
    assert stream == b"" or not list(pa.ipc.open_stream(stream))
    for text in (error.message, stderr, repr(stream)):
        assert str(path) not in text
        assert "SECRET123" not in text


_HIDDEN_CONVERSION = (
    "Query failed: a value could not be converted (the value is not shown, because it may come "
    "from data you cannot see). Check the casts in the query; otherwise a file of this table "
    "stores a column in a type the read cannot convert it to."
)


def _field_id_file(directory: Path, name: str, value: str) -> Path:
    """Write a one-row Parquet file whose column ``ssn`` is text and has field id 1.

    Parameters
    ----------
    directory
        Where the file goes.
    name
        The file's name.
    value
        The cell value.

    Returns
    -------
    Path
        The file.
    """
    path = directory / name
    field = pa.field("ssn", pa.string(), metadata={b"PARQUET:field_id": b"1"})
    pq.write_table(pa.table({"ssn": [value]}, schema=pa.schema([field])), path)
    return path


@pytest.mark.parametrize(
    "select",
    [
        pytest.param("SELECT * FROM {reader}", id="a plain read"),
        pytest.param('SELECT "ssn" % 10 AS "ssn" FROM {reader}', id="a partial mask"),
        pytest.param('SELECT "ssn" FROM {reader} WHERE ("ssn" > 0)', id="a row filter"),
    ],
)
def test_a_cast_the_map_asks_for_names_no_cell_value(tmp_path: Path, select: str) -> None:
    """A file that stores text where the map says INTEGER fails without quoting the cell."""
    path = _field_id_file(tmp_path, "X-Amz-Signature=SECRET123.parquet", "123-45-6789")
    reader = "read_parquet(getvariable('v_0_files'), schema = getvariable('v_0_schema'))"
    request = _request(
        select.format(reader=reader),
        parameters={"v_0_files": [str(path)], "v_0_schema": ((1, "ssn", "INTEGER"),)},
        read_prefixes=(f"{tmp_path}/",),
        spill=True,
    )

    code, stream, stderr = _main(request)

    error = decode_error(stderr.encode())
    assert code == EXIT_FAILED
    assert error is not None
    assert error.kind == "invalid"
    assert error.message in {
        'Query failed: a file of a table this query reads stores column "ssn" as VARCHAR, '
        "which cannot be read as INTEGER. Ask the owner of that table to write its rows again, "
        "so that every file stores the column in one type.",
        _HIDDEN_CONVERSION,
    }
    assert stream == b"" or not list(pa.ipc.open_stream(stream))
    for text in (error.message, stderr, repr(stream)):
        for secret in (str(path), "SECRET123", "123-45-6789", "6789"):
            assert secret not in text


def test_a_cast_of_the_querys_own_on_a_mapped_read_names_the_next_step(tmp_path: Path) -> None:
    """The user's own failing cast is worded without its value and points at the query."""
    path = _field_id_file(tmp_path, "rows.parquet", "123-45-6789")
    request = _request(
        "SELECT CAST('abc' AS INT) AS n FROM read_parquet(getvariable('v_0_files'), "
        "schema = getvariable('v_0_schema'))",
        parameters={"v_0_files": [str(path)], "v_0_schema": ((1, "ssn", "VARCHAR"),)},
        read_prefixes=(f"{tmp_path}/",),
        spill=True,
    )

    code, stream, stderr = _main(request)

    assert code == EXIT_FAILED
    assert decode_error(stderr.encode()) == ErrorDocument("invalid", _HIDDEN_CONVERSION)
    assert "abc" not in stderr
    assert stream == b"" or not list(pa.ipc.open_stream(stream))


# --- the files against the prefixes ------------------------------------------------------------

_REMOTE = "http://store:8333/bucket/root/managed/ns/t1-ab/"
_LOCAL = "/srv/lakana/managed/ns/t1-ab/"


def _files(*urls: str, prefixes: tuple[str, ...] = (_REMOTE, _LOCAL)) -> Request:
    """Build a request whose one file list holds `urls`.

    Parameters
    ----------
    *urls
        The files.
    prefixes
        The request's prefixes.

    Returns
    -------
    Request
        The request.
    """
    return _request(
        "SELECT * FROM read_parquet(getvariable('v_0_files'))",
        parameters={"v_0_files": list(urls)},
        read_prefixes=prefixes,
    )


@pytest.mark.parametrize(
    "url",
    [
        f"{_REMOTE}data/a.parquet?X-Amz-Signature=abc&X-Amz-Credential=k%2F2026",
        f"{_REMOTE}data/x%20y.parquet",
        f"{_REMOTE}a..b.parquet",
        f"{_LOCAL}data/a.parquet",
        f"{_LOCAL}data/a..b.parquet",
        f"{_LOCAL}data/.hidden.parquet",
    ],
)
def test_a_file_under_a_prefix_passes(url: str) -> None:
    assert not query_child.file_outside_prefixes(_files(url))


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("http://store:8333/bucket/root/managed/ns/t10-ab/a.parquet", id="sibling"),
        pytest.param("http://other:8333/bucket/root/managed/ns/t1-ab/a.parquet", id="other host"),
        pytest.param("https://store:8333/bucket/root/managed/ns/t1-ab/a.parquet", id="https"),
        pytest.param("s3://bucket/root/managed/ns/t1-ab/a.parquet", id="s3 scheme"),
        pytest.param("http://169.254.169.254/latest/meta-data/", id="metadata"),
        pytest.param(f"{_REMOTE}../t2/a.parquet", id="dotdot"),
        pytest.param(f"{_REMOTE}data/./../../t2/a.parquet", id="dot then dotdot"),
        pytest.param(f"{_REMOTE}%2e%2e/t2/a.parquet", id="escaped dotdot"),
        pytest.param(f"{_REMOTE}%2E./t2/a.parquet", id="escaped dot upper"),
        pytest.param(f"{_REMOTE}..%2ft2/a.parquet", id="escaped slash"),
        pytest.param(f"{_REMOTE}..%5ct2/a.parquet", id="escaped backslash"),
        pytest.param(f"{_REMOTE}..\\t2/a.parquet", id="backslash"),
        pytest.param(f"{_REMOTE}..?x=1", id="dotdot before query"),
        pytest.param(f"{_LOCAL}../t2/a.parquet", id="local dotdot"),
        pytest.param(f"{_LOCAL}data/../../t2/a.parquet", id="local deep dotdot"),
        pytest.param(f"{_LOCAL}%2e%2e/t2/a.parquet", id="local escaped"),
        pytest.param("/srv/lakana/managed/ns/t1-ab", id="the directory without slash"),
        pytest.param("/etc/passwd", id="local elsewhere"),
        pytest.param("", id="empty"),
        pytest.param(f"{_LOCAL}a\x00.parquet", id="NUL"),
        pytest.param("relative/a.parquet", id="relative"),
    ],
)
def test_a_file_outside_the_prefixes_is_refused_before_anything_runs(
    url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert query_child.file_outside_prefixes(_files(f"{_LOCAL}ok.parquet", url))
    monkeypatch.setattr(query_child.duckdb, "connect", lambda *_a, **_k: pytest.fail("ran"))
    assert _failed(_files(url)) == ErrorDocument("internal", query_child.OUTSIDE_PREFIXES)


@pytest.mark.parametrize(
    "prefix",
    [
        pytest.param("/", id="the disk"),
        pytest.param("/srv/lakana", id="no trailing slash"),
        pytest.param("srv/lakana/", id="relative"),
        pytest.param("/srv/./lakana/", id="dot"),
        pytest.param("/srv/../etc/", id="dotdot"),
        pytest.param("/srv//lakana/", id="double slash"),
        pytest.param("//srv/lakana/", id="leading double slash"),
        pytest.param("/srv/lakana\x00/", id="NUL"),
        pytest.param("http://store:8333/", id="host only"),
        pytest.param("http://store:8333", id="host without slash"),
        pytest.param("http://store:8333/bucket", id="remote without slash"),
        pytest.param("http://user@store:8333/bucket/", id="user information"),
        pytest.param("http://store:8333@evil/bucket/", id="userinfo trick"),
        pytest.param("http://store:8333/bucket/?x=/", id="query"),
        pytest.param("http://store:8333/bucket/#/", id="fragment"),
        pytest.param("http://store:8333/bucket//t/", id="empty segment"),
        pytest.param("http://store:8333/bucket/../t/", id="remote dotdot"),
        pytest.param("http://store:8333/bucket/%2e%2e/t/", id="remote escaped dotdot"),
        pytest.param("http://store:8333/b\\x/", id="backslash"),
        pytest.param("http://-store/b/", id="bad host"),
        pytest.param("s3://bucket/root/", id="s3"),
        pytest.param("file:///srv/lakana/", id="file URL"),
        pytest.param("", id="empty"),
    ],
)
def test_a_prefix_of_another_shape_refuses_the_request(prefix: str) -> None:
    assert not query_child.valid_prefix(prefix)
    request = _request(read_prefixes=(prefix,))
    assert query_child.file_outside_prefixes(request)
    assert _failed(request) == ErrorDocument("internal", query_child.OUTSIDE_PREFIXES)


@pytest.mark.parametrize(
    "prefix",
    [
        "/srv/lakana/managed/ns/t1-ab/",
        "http://127.0.0.1:8333/bucket/root/t/",
        "https://bucket.s3.eu-central-1.amazonaws.com/root/t/",
        "https://acct.blob.core.windows.net/container/root/t/",
        "http://[::1]:8333/bucket/t/",
    ],
)
def test_a_prefix_of_a_known_shape_is_accepted(prefix: str) -> None:
    assert query_child.valid_prefix(prefix)


def test_a_relative_ca_bundle_refuses_the_request() -> None:
    assert _failed(_request(ca_bundle="ca.pem")) == ErrorDocument(
        "internal", query_child.OUTSIDE_PREFIXES
    )


def test_a_check_is_not_refused_for_its_prefixes() -> None:
    """The start-up check runs no statement and reads no file."""
    code, _stream, stderr = _main(_request("", mode=CHECK, read_prefixes=("/",)))
    assert (code, stderr) == (EXIT_OK, "")


_SEGMENT = st.text(alphabet="ab.%2eE/\\?", min_size=0, max_size=12)


@settings(max_examples=400, deadline=None)
@given(rest=_SEGMENT, remote=st.booleans())
def test_no_generated_file_that_passes_reaches_outside_its_prefix(rest: str, remote: bool) -> None:
    """Property: whatever passes, normalised as a server or the kernel would, stays inside.

    The rest after the prefix is percent-decoded (as a server might), backslashes are read as
    slashes, and the path is normalised; it must still lie under the prefix's directory.
    """
    prefix = _REMOTE if remote else _LOCAL
    url = prefix + rest
    if query_child.file_outside_prefixes(_files(url, prefixes=(prefix,))):
        return
    path = url.split("://", 1)[-1].split("/", 1)[-1] if remote else url
    path = "/" + path.lstrip("/")
    if remote:
        path = path.split("?", 1)[0]
        path = urllib.parse.unquote(path).replace("\\", "/")
    base = prefix.split("://", 1)[-1].split("/", 1)[-1] if remote else prefix
    base = "/" + base.lstrip("/")
    assert (posixpath.normpath(path) + "/").startswith(base) or posixpath.normpath(
        path
    ) + "/" == base


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE SECRET p (TYPE http, HTTP_PROXY '127.0.0.1:9'); SELECT 1",
        "SELECT 1; SELECT 2",
        "SET threads = 1",
        "ATTACH ':memory:' AS m",
    ],
)
def test_anything_but_one_select_is_refused_before_the_connection_is_configured(
    sql: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In this process, for coverage; `test_query_allow_list.py` runs the real child."""
    monkeypatch.setattr(query_child, "configure", lambda *_a: pytest.fail("configured"))
    assert _failed(_request(sql)) == ErrorDocument("internal", query_child.NOT_ONE_SELECT)


_COPY = "COPY (SELECT 1 AS a) TO '/nonexistent/lakana-test-copy.csv'"
NESTED_WRITES = [
    pytest.param(f"WITH x AS ({_COPY}) SELECT 1", id="COPY in a CTE"),
    pytest.param(f"WITH x AS MATERIALIZED ({_COPY}) SELECT 1", id="COPY in a materialized CTE"),
    pytest.param(f"SELECT * FROM (WITH x AS ({_COPY}) SELECT 1)", id="COPY in a subquery"),
    pytest.param(f"SELECT 1 UNION ALL (WITH x AS ({_COPY}) SELECT 1)", id="COPY in a UNION"),
    pytest.param(f"SELECT EXISTS (WITH x AS ({_COPY}) SELECT 1)", id="COPY in EXISTS"),
    pytest.param(
        f"SELECT list_transform([1], LAMBDA y : (WITH x AS ({_COPY}) SELECT 1))",
        id="COPY in a lambda",
    ),
    pytest.param("WITH x AS (DELETE FROM t RETURNING *) SELECT * FROM x", id="DELETE in a CTE"),
    pytest.param(
        "SELECT * FROM (WITH x AS (UPDATE t SET a = 1 RETURNING *) SELECT * FROM x)",
        id="UPDATE in a subquery",
    ),
    pytest.param(
        "SELECT 1 UNION ALL (WITH x AS (INSERT INTO t VALUES (1) RETURNING *) SELECT 1)",
        id="INSERT in a UNION",
    ),
    pytest.param(
        "SELECT * FROM json_execute_serialized_sql(json_serialize_sql("
        f"'WITH x AS ({_COPY.replace(chr(39), chr(39) * 2)}) SELECT 1'))",
        id="COPY run from a serialised statement",
    ),
    pytest.param("SELECT * FROM main.JSON_EXECUTE_SERIALIZED_SQL('{}')", id="a qualified spelling"),
    pytest.param("SELECT * FROM query('SELECT 1')", id="query()"),
    pytest.param("SELECT * FROM query_table('t')", id="query_table()"),
    pytest.param("SELECT ('SELECT 1').query()", id="query() as a method"),
    pytest.param("SELECT 1 WHERE EXISTS (SELECT * FROM query('SELECT 1'))", id="query() in EXISTS"),
    pytest.param("SELECT json_serialize_plan('SELECT 1')", id="json_serialize_plan()"),
]
"""Writes that DuckDB 2's parser reports as one ``SELECT``, and calls of functions that run (or
plan) SQL given as a value, which the tree cannot show. The guard refuses every one first; these
pin the second layer."""


def _serialised_execute(target: Path) -> str:
    """Build a SELECT that runs a nested ``COPY`` to `target` from a serialised statement.

    Parameters
    ----------
    target
        The file the inner ``COPY`` writes.

    Returns
    -------
    str
        One SELECT whose own tree holds only a table function call.
    """
    inner = f"WITH x AS (COPY (SELECT 9 AS a) TO ''{target}'') SELECT 1"
    return f"SELECT * FROM json_execute_serialized_sql(json_serialize_sql('{inner}'))"


@pytest.mark.parametrize("sql", NESTED_WRITES)
def test_a_write_nested_in_a_select_is_refused_before_the_connection_is_configured(
    sql: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Red on 2.0.0 before the tree check: each one parsed as one SELECT and reached DuckDB."""
    monkeypatch.setattr(query_child, "configure", lambda *_a: pytest.fail("configured"))
    assert _failed(_request(sql)) == ErrorDocument("internal", query_child.WRITES_IN_SELECT)


@pytest.mark.parametrize("serialised", [False, True], ids=["nested COPY", "serialised COPY"])
def test_a_copy_nested_in_a_select_writes_nothing_into_the_temp_directory(
    tmp_path: Path, serialised: bool
) -> None:
    """Red on 2.0.0 before the second layer: the file appeared in the query's temp directory.

    ``max_temp_directory_size`` limits spilling only, so in remote mode such a write could fill
    the worker's temp file system until the time limit. The serialised form was red before the
    function check too: its own tree shows only a table function. The guard refuses both first.
    """
    spill = tmp_path / "spill"
    spill.mkdir()
    target = spill / "f.csv"
    sql = (
        _serialised_execute(target)
        if serialised
        else f"WITH x AS (COPY (SELECT range AS a FROM range(10)) TO '{target}') SELECT 1 AS one"
    )
    request = _request(sql, temp_directory=str(spill), read_prefixes=("http://127.0.0.1:9/b/t1/",))
    assert _failed(request) == ErrorDocument("internal", query_child.WRITES_IN_SELECT)
    assert not target.exists()


@pytest.mark.parametrize(
    "document",
    [
        pytest.param("not json", id="not JSON"),
        pytest.param("[]", id="not an object"),
        pytest.param('{"error": true, "error_message": "x"}', id="DuckDB could not serialise"),
        pytest.param('{"error": false}', id="no statements"),
        pytest.param('{"error": false, "statements": []}', id="zero statements"),
        pytest.param(
            '{"error": false, "statements": [{"node": {"type": "SELECT_NODE"}}, '
            '{"node": {"type": "SELECT_NODE"}}]}',
            id="two statements",
        ),
        pytest.param(
            '{"error": false, "statements": [{"node": {"type": "MERGE_INTO_QUERY_NODE"}}]}',
            id="a write node",
        ),
        pytest.param(
            '{"error": false, "statements": [{"node": {"type": "STATEMENT_NODE"}}]}',
            id="a node type not on the list",
        ),
        pytest.param(
            '{"error": false, "statements": [{"node": {"type": "SELECT_NODE", "x": '
            '{"type": "FUTURE_WRITE", "modifiers": [], "cte_map": {}}}}]}',
            id="a query node whose type does not end in _NODE",
        ),
        pytest.param(
            '{"error": false, "statements": [{"node": {"type": 7, "modifiers": [], '
            '"cte_map": {}}}]}',
            id="a query node whose type is not a string",
        ),
        pytest.param(
            '{"error": false, "statements": [{"node": {"type": "SELECT_NODE", "from_table": '
            '{"type": "TABLE_FUNCTION", "function": {"class": "FUNCTION", '
            '"function_name": "Query", "schema": "other"}}}}]}',
            id="a function that runs SQL, in any case and schema",
        ),
        pytest.param(
            '{"error": false, "statements": [{"node": {"type": "SELECT_NODE", "x": '
            '{"function_name": ["query"]}}}]}',
            id="a function name that is not a string",
        ),
    ],
)
def test_a_serialised_tree_that_is_not_plainly_a_read_is_refused(document: str) -> None:
    """Fail closed: anything but one statement made only of reading query nodes is refused."""
    assert not query_child.reads_only(document)


def test_a_serialised_read_is_accepted() -> None:
    connection = duckdb.connect()
    sql = "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r WHERE n < 3) FROM r"
    (document,) = connection.execute("SELECT json_serialize_sql(?)", [sql]).fetchone() or ("",)
    assert query_child.reads_only(document)


def test_the_serialiser_gives_an_empty_text_when_duckdb_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An error from the serialiser becomes the empty text that `reads_only` refuses."""
    monkeypatch.setattr(query_child, "SERIALISE", "SELECT CAST(? AS INTEGER)")
    assert query_child._serialised(duckdb.connect(), "SELECT 1") == ""
    assert not query_child.reads_only("")


def test_a_statement_duckdb_cannot_serialise_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail closed when the serialiser reports an error for SQL the statement check accepted."""
    monkeypatch.setattr(query_child, "SERIALISE", "SELECT error('cannot serialise') || ?")
    monkeypatch.setattr(query_child, "configure", lambda *_a: pytest.fail("configured"))
    assert _failed(_request("SELECT 1")) == ErrorDocument("internal", query_child.WRITES_IN_SELECT)


# --- the figures the child measures about itself ---------------------------------------------


PARQUET = str(Path(SCRATCH) / "rows.parquet")
duckdb.sql(
    "COPY (SELECT i, repeat('x', 20) || i::VARCHAR AS s FROM range(2000) AS t(i)) "
    f"TO '{PARQUET}' (FORMAT parquet)"
)
"""A Parquet file of 2000 rows that the real-child test reads."""


def _usage(request: Request, tmp_path: Path) -> tuple[int, bytes, str, dict[str, object]]:
    """Run a request and read back the usage file it wrote.

    Parameters
    ----------
    request
        The request.
    tmp_path
        A directory for the usage file.

    Returns
    -------
    tuple
        The status, the stream, standard error and the usage document.
    """
    path = tmp_path / query_child.USAGE_FILE
    code, stream, stderr = _main(request, usage_path=str(path))
    return code, stream, stderr, json.loads(path.read_text())


def _parquet(tmp_path: Path) -> tuple[str, Path]:
    """Write a Parquet file of 2000 rows and name the request's statement that reads it.

    Parameters
    ----------
    tmp_path
        The directory.

    Returns
    -------
    tuple of (str, Path)
        The variable's name and the file.
    """
    path = tmp_path / "rows.parquet"
    duckdb.sql(
        "COPY (SELECT i, repeat('x', 20) || i::VARCHAR AS s FROM range(2000) AS t(i)) "
        f"TO '{path}' (FORMAT parquet)"
    )
    return "v_0_files", path


def _read_request(tmp_path: Path, sql: str) -> Request:
    """Build a request whose statement may read the Parquet file of `_parquet`.

    Parameters
    ----------
    tmp_path
        The directory.
    sql
        The statement.

    Returns
    -------
    Request
        The request.
    """
    name, path = _parquet(tmp_path)
    return _request(sql, parameters={name: [str(path)]}, read_prefixes=(f"{tmp_path}/",))


def test_the_child_tracks_only_the_bytes_read_and_profiles_into_a_file() -> None:
    connection, _settings = _configured(_request())
    try:
        rows = connection.execute(
            "SELECT name, value FROM duckdb_settings() "
            "WHERE name IN ('tracked_metrics', 'enable_profiling', 'profiling_output')"
        ).fetchall()
    finally:
        connection.close()
    found = dict(rows)
    assert found["tracked_metrics"] == "[io.total_bytes_read]"
    assert found["enable_profiling"] == "json"
    assert found["profiling_output"] == os.path.join(SCRATCH, query_child.PROFILE_FILE)


def test_a_parquet_read_reports_the_bytes_it_read_and_the_childs_own_peak(tmp_path: Path) -> None:
    sql = "SELECT sum(i) AS n FROM read_parquet(getvariable('v_0_files'))"
    code, stream, stderr, usage = _usage(_read_request(tmp_path, sql), tmp_path)
    assert (code, stderr) == (EXIT_OK, "")
    assert _table(stream).to_pylist() == [{"n": sum(range(2000))}]
    assert set(usage) == {"bytes_read", "peak_memory_bytes"}
    assert isinstance(usage["bytes_read"], int) and usage["bytes_read"] > 0


def test_a_statement_that_reads_nothing_reports_zero_bytes(tmp_path: Path) -> None:
    *_rest, usage = _usage(_request("SELECT 1 AS one"), tmp_path)
    assert usage["bytes_read"] == 0


def test_a_statement_answered_from_metadata_reports_null(tmp_path: Path) -> None:
    sql = "SELECT count(*) AS n FROM read_parquet(getvariable('v_0_files'))"
    code, stream, _stderr, usage = _usage(_read_request(tmp_path, sql), tmp_path)
    assert code == EXIT_OK
    assert _table(stream).to_pylist() == [{"n": 2000}]
    assert usage["bytes_read"] is None


def test_a_failed_statement_reports_null_and_its_error_document_still_decodes(
    tmp_path: Path,
) -> None:
    # The statement before the user's is `set_variables`, whose profile counts 0 bytes: measuring
    # after the failure would give 0 and not null.
    request = _request("SELECT * FROM no_such_table", read_prefixes=(f"{tmp_path}/",))
    code, _stream, stderr, usage = _usage(request, tmp_path)
    assert code == EXIT_FAILED
    assert decode_error(stderr.encode()) is not None
    assert usage["bytes_read"] is None


def test_an_unwritable_usage_file_only_loses_the_figures(tmp_path: Path) -> None:
    path = tmp_path / query_child.USAGE_FILE
    path.write_text("taken")
    code, _stream, stderr = _main(_request(), usage_path=str(path))
    assert (code, stderr) == (EXIT_OK, "")
    assert path.read_text() == "taken"


def test_a_usage_file_is_not_written_through_a_symbolic_link(tmp_path: Path) -> None:
    target = tmp_path / "target"
    link = tmp_path / query_child.USAGE_FILE
    link.symlink_to(target)
    _main(_request(), usage_path=str(link))
    assert not target.exists()


def test_a_session_without_a_connection_measures_nothing() -> None:
    session = query_child.Session()
    session.measure()
    assert session.bytes_read is None


@pytest.mark.parametrize(
    "answer",
    [
        '{"result": "error"}',
        "[]",
        '{"io": 3}',
        '{"io": {"total_bytes_read": -1}}',
        '{"io": {"total_bytes_read": true}}',
        '{"io": {"total_bytes_read": "7"}}',
        "not json",
        5,
    ],
)
def test_a_profile_that_is_not_a_count_measures_nothing(answer: object) -> None:
    class Fake:
        def get_profiling_information(self, _format: str) -> object:
            return answer

    session = query_child.Session()
    session._connection = Fake()  # ty: ignore[invalid-assignment]
    session.measure()
    assert session.bytes_read is None


def test_a_profile_the_connection_cannot_give_measures_nothing() -> None:
    class Broken:
        def get_profiling_information(self, _format: str) -> str:
            raise duckdb.Error("closed")

    session = query_child.Session()
    session._connection = Broken()  # ty: ignore[invalid-assignment]
    session.measure()
    assert session.bytes_read is None


def test_a_measurement_that_fails_never_fails_the_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(self: query_child.Session) -> None:
        raise RuntimeError("the binding broke")

    monkeypatch.setattr(query_child.Session, "measure", broken)
    code, stream, stderr, usage = _usage(_request(), tmp_path)
    assert (code, stderr) == (EXIT_OK, "")
    assert _table(stream).to_pylist() == [{"answer": 42}]
    assert usage["bytes_read"] is None


def test_configure_sends_the_profiling_statements_the_overhead_script_measures(
    tmp_path: Path,
) -> None:
    sent: list[str] = []
    connection, _settings = _configured(_request(), sent)
    connection.close()
    expected = query_child.profiling_statements(os.path.join(SCRATCH, query_child.PROFILE_FILE))
    assert [statement for statement in sent if statement in expected] == expected


def test_a_real_child_through_the_workers_query_child_reports_its_figures() -> None:
    """The child writes `usage.json` where the worker looks: its working directory."""
    from lakana_worker.query_process import QueryChild

    def make(tmp: str) -> Request:
        return _request(
            "SELECT sum(i) AS n FROM read_parquet(getvariable('v_0_files'))",
            parameters={"v_0_files": [PARQUET]},
            read_prefixes=(f"{Path(PARQUET).parent}/",),
            temp_directory=tmp,
        )

    with QueryChild(make, timeout_seconds=30, kill_grace_seconds=5) as child:
        reader = pa.ipc.open_stream(child)
        assert reader.read_all().to_pylist() == [{"n": sum(range(2000))}]
        child.finish()
    assert child.stderr == b""
    figures = child.resources
    assert figures.bytes_read is not None and figures.bytes_read > 0
    if sys.platform.startswith("linux"):
        assert figures.peak_memory_bytes is not None and figures.peak_memory_bytes > 0
    assert figures.cpu_seconds is not None


class _FakeConnection:
    """Records the statements `load_httpfs` runs; `LOAD` fails while `installed` is false."""

    def __init__(self, *, installed: bool) -> None:
        self.statements: list[str] = []
        self.installed = installed

    def execute(self, statement: str) -> None:
        self.statements.append(statement)
        if statement == "LOAD httpfs" and not self.installed:
            raise duckdb.IOException(
                'IO Error: Extension "/ext/v2.0.0/linux_amd64/httpfs.duckdb_extension" not found.'
            )


def _load(connection: _FakeConnection, source: str) -> None:
    query_child.load_httpfs(
        cast("duckdb.DuckDBPyConnection", connection),
        _request(extension_source=source, extension_directory="/ext"),
    )


def test_by_default_httpfs_is_installed_from_duckdbs_own_server_as_before() -> None:
    connection = _FakeConnection(installed=True)
    _load(connection, "duckdb")
    assert connection.statements == ["INSTALL httpfs", "LOAD httpfs"]


def test_none_never_installs_and_loads_what_is_there() -> None:
    present = _FakeConnection(installed=True)
    _load(present, "none")
    assert "INSTALL httpfs" not in present.statements
    assert present.statements[-1] == "LOAD httpfs"
    missing = _FakeConnection(installed=False)
    with pytest.raises(query_child.ExtensionUnavailable) as caught:
        _load(missing, "none")
    assert not any(s.startswith("INSTALL") for s in missing.statements)
    assert str(caught.value) == (
        "Query failed: the worker cannot load DuckDB's httpfs extension: "
        "/ext/v2.0.0/linux_amd64/httpfs.duckdb_extension is missing and "
        "LAKANA_WORKER__EXTENSION_SOURCE=none forbids downloading it. Tell the operator: run "
        "the worker from the Lakana container image, which bundles it, or copy "
        "httpfs.duckdb_extension to /ext/v2.0.0/linux_amd64/httpfs.duckdb_extension (inside /ext), "
        "or unset LAKANA_WORKER__EXTENSION_SOURCE to let DuckDB download it."
    )


def test_a_real_child_with_none_and_an_empty_extension_directory_answers_with_the_message(
    tmp_path: Path,
) -> None:
    error = _failed(_request(extension_directory=str(tmp_path), extension_source="none"))
    assert error.kind == "invalid"
    assert str(tmp_path) in error.message
    assert "httpfs.duckdb_extension is missing" in error.message
    assert "LAKANA_WORKER__EXTENSION_SOURCE=none" in error.message


def test_a_check_with_none_and_an_empty_extension_directory_answers_with_the_message(
    tmp_path: Path,
) -> None:
    error = _failed(
        _request("", mode=CHECK, extension_directory=str(tmp_path), extension_source="none")
    )
    assert error.kind == "invalid"
    assert str(tmp_path) in error.message
    assert "httpfs.duckdb_extension is missing" in error.message
    assert "LAKANA_WORKER__EXTENSION_SOURCE=none" in error.message


def test_a_check_with_none_passes_when_httpfs_is_in_the_directory() -> None:
    code, stream, stderr = _main(_request("", mode=CHECK, extension_source="none"))
    assert (code, stderr) == (EXIT_OK, "")
    assert _table(stream).num_columns == 0
