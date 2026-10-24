"""Tests for the executor: every query runs in a child process of its own."""

import contextlib
import functools
import glob
import http.server
import logging
import os
import signal
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import NoReturn

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from lakana_core.config import QueryLimitsConfig
from lakana_core.errors import ValidationError
from lakana_core.models import QueryErrorCause
from lakana_core.query_rewrite import TableSource, rewrite_query
from lakana_core.runs.tasks import FailureKind
from lakana_worker import executor as executor_module
from lakana_worker import query_process
from lakana_worker.executor import (
    CANCELLED_MESSAGE,
    NOT_STARTED_MESSAGE,
    WORKER_STALE_MESSAGE,
    DuckDBInternalError,
    MemoryStopQuery,
    OutOfMemoryQuery,
    QueryCancelled,
    QueryCancelRequested,
    QueryTimeout,
    ResultTooLarge,
    TransientQueryError,
    WorkerStaleQuery,
    cancel_requests,
    cancellation,
    describe_query,
    execute_query,
    install_drain,
    install_heartbeat,
    stream_query,
)
from lakana_worker.query_child import NO_SPILL, REFUSED_LOCATION
from lakana_worker.query_process import REGISTRY, ChildFailed, StopReason
from lakana_worker.query_protocol import MAX_REQUEST_BYTES, ErrorDocument
from lakana_worker.spill import spill_prefix
from lakana_worker.task_lease import LeaseKeeper


class _ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """A throwaway HTTP server whose threads never outlive the test."""

    daemon_threads = True


@pytest.fixture
def parquet_over_http(tmp_path: Path) -> Iterator[str]:
    """Serve one parquet file over loopback HTTP and yield its URL.

    `SimpleHTTPRequestHandler` answers the HTTP `Range` requests DuckDB's
    parquet reader makes, so this stands in for a presigned URL without
    needing a container or the network.

    Parameters
    ----------
    tmp_path
        pytest's per-test temporary directory, used as the document root.

    Yields
    ------
    str
        The URL of the served `t1/probe.parquet`; its directory is the read prefix.
    """
    (tmp_path / "t1").mkdir()
    pq.write_table(pa.table({"a": [1, 2, 3]}), tmp_path / "t1" / "probe.parquet")
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(tmp_path))
    server = _ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/t1/probe.parquet"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_executes_a_plain_query() -> None:
    """A query with no external reference runs and returns an Arrow table."""
    result = execute_query("SELECT 1 AS x, 2 AS y")

    assert result.column("x").to_pylist() == [1]
    assert result.column("y").to_pylist() == [2]


def test_a_syntax_error_raises_validation_error() -> None:
    """A malformed query surfaces as ValidationError, not a raw DuckDB exception."""
    with pytest.raises(ValidationError):
        execute_query("SELEKT 1")


def test_duckdbs_line_and_caret_context_is_stripped_from_the_error(tmp_path: Path) -> None:
    """DuckDB's `LINE n:`/caret-marker echo of the query is never in the message.

    DuckDB's own binder error text quotes the offending SQL back verbatim
    under a `LINE n:` marker, followed by a caret-marker line pointing at
    it — and a rewritten query's `LINE n:` text can contain a row filter's
    or column mask's own restricted-subset expression, not just catalog
    file paths. `_MARKER_TEXT` stands in for that expression: it must
    appear in DuckDB's raw exception (proving the scenario is real) but
    never in `execute_query`'s `ValidationError`.
    """
    marker = "SENSITIVE_MASK_EXPR"
    # The marker sits in a string literal DuckDB's own diagnostic sentence
    # never has reason to quote back — only the `LINE n:` echo of the
    # query text does — unlike an unresolved column reference, whose
    # diagnostic sentence names the column directly regardless of the
    # `LINE` marker, which would make this test pass for the wrong reason.
    sql = f"SELECT * FROM (SELECT 1 AS x) WHERE x = '{marker}' !!!"
    raw_message = ""
    try:
        import duckdb

        duckdb.connect(":memory:").execute(sql)
    except duckdb.Error as exc:
        raw_message = str(exc)
    assert marker in raw_message, "the scenario must actually put the marker in DuckDB's own text"
    assert "LINE " in raw_message

    with pytest.raises(ValidationError) as exc_info:
        execute_query(sql)

    assert marker not in str(exc_info.value)
    assert "LINE " not in str(exc_info.value)
    assert "^" not in str(exc_info.value)
    assert "Query failed" in str(exc_info.value)


def test_a_local_query_reads_a_file_under_its_read_prefix() -> None:
    """Local (file://) mode reads bare local paths, under the directories it is given.

    See execute_query's `read_prefixes`.
    """
    with tempfile.TemporaryDirectory() as tmp:
        probe = Path(tmp) / "probe.parquet"
        pq.write_table(pa.table({"a": [1]}), probe)

        result = execute_query(
            f"SELECT * FROM read_parquet('{probe}')", read_prefixes=(f"{Path(tmp).resolve()}/",)
        )

        assert result.column("a").to_pylist() == [1]


def test_a_local_file_attach_is_refused_even_if_it_reached_the_executor(tmp_path: Path) -> None:
    """Belt and braces: a local-disk ATTACH fails here even if the blacklist were bypassed.

    `ATTACH ':memory:'` is deliberately not used for this: it always succeeds
    regardless of any filesystem restriction (confirmed interactively while
    writing this plan) because it opens a fresh in-memory catalog, not a
    path to real data — it is harmless, not a bypass, and asserting it fails
    would pin the wrong behaviour. A local file path is the real case this
    connection's allow-list mode exists to block.
    """
    with pytest.raises(ValidationError):
        execute_query(f"ATTACH '{tmp_path}/evil.db' AS x")


def test_reading_a_local_file_directly_is_refused_even_if_it_reached_the_executor(
    tmp_path: Path,
) -> None:
    """Belt and braces: a local read_parquet fails here even if the blacklist were bypassed."""
    probe = tmp_path / "probe.parquet"
    pq.write_table(pa.table({"a": [1]}), probe)

    with pytest.raises(ValidationError):
        execute_query(f"SELECT * FROM read_parquet('{probe}')")


def test_a_presigned_style_remote_read_succeeds_under_its_read_prefix(
    parquet_over_http: str,
) -> None:
    """The default configuration must be able to run the reads it exists for.

    DuckDB initialises its secret manager lazily, on the first httpfs read, and that
    initialisation touches local disk; `allow_persistent_secrets = false` keeps it off the disk,
    which allow-list mode would refuse.
    """
    prefix = parquet_over_http.rsplit("/", 1)[0] + "/"
    result = execute_query(
        f"SELECT * FROM read_parquet('{parquet_over_http}')", read_prefixes=(prefix,)
    )

    assert result.column("a").to_pylist() == [1, 2, 3]


def _environment_of_a_running_child() -> dict[str, str]:
    """Run a real child on a slow query and read its environment from outside, while it runs.

    A query cannot list its own process's environment (DuckDB reads no `/proc` file), so the
    test reads `/proc/<pid>/environ` of the registered child; the child is registered after
    its `exec`, so that is its own environment and not a copy of the worker's. The query ends
    by its time limit.

    Returns
    -------
    dict of str to str
        The child's environment variables.
    """
    seen: dict[str, str] = {}

    def read() -> None:
        """Read the environment of the first registered child as soon as it has one."""
        deadline = time.monotonic() + 10
        while not seen and time.monotonic() < deadline:
            for pid in REGISTRY.pids():
                try:
                    raw = Path(f"/proc/{pid}/environ").read_bytes()
                except OSError:
                    continue
                pairs = [item.decode().split("=", 1) for item in raw.split(b"\0") if item]
                seen.update({name: value for name, value in pairs})
            time.sleep(0.005)

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    limits = QueryLimitsConfig(timeout_seconds=1.0, kill_grace_seconds=0.5)
    with pytest.raises(QueryTimeout):
        execute_query(_SLOW_SQL, limits=limits)
    reader.join(5)
    assert seen, "no child was seen"
    return seen


def test_ambient_aws_credentials_never_reach_the_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The worker process's own cloud credentials do not reach the query's connection.

    `duckdb.connect()` auto-populates the `s3_*` settings from `AWS_*` in
    the environment, so "we never called CREATE SECRET" is not enough: a
    worker holding instance-role credentials would sign every S3 request
    with them. Now two things keep them out, and this test reads both through a
    real child: its environment is built from scratch (see
    `test_the_child_sees_nothing_of_the_workers_environment`), and its connection clears the
    settings.
    """
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAFAKEFAKEFAKEFAKE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "fakesecretfakesecret")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "fakesessiontoken")

    result = execute_query(
        "SELECT current_setting('s3_access_key_id') AS key_id, "
        "current_setting('s3_secret_access_key') AS secret, "
        "current_setting('s3_session_token') AS token"
    )

    assert result.column("key_id").to_pylist() == [""]
    assert result.column("secret").to_pylist() == [""]
    assert result.column("token").to_pylist() == [""]


def test_ambient_aws_credentials_are_cleared_in_local_mode_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Local mode's weaker isolation does not earn a pass on ambient credentials."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAFAKEFAKEFAKEFAKE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "fakesecretfakesecret")

    result = execute_query(
        "SELECT current_setting('s3_access_key_id') AS key_id",
        spill=True,
    )

    assert result.column("key_id").to_pylist() == [""]


_READS_PROC = pytest.mark.skipif(
    sys.platform == "darwin",
    reason="reads /proc/<pid>/environ, which macOS lacks; the child's environment is checked "
    "from inside the child there by test_query_process::"
    "test_the_child_gets_the_request_a_scrubbed_environment_and_only_its_pipes",
)


@_READS_PROC
def test_ambient_aws_credentials_never_reach_a_local_mode_childs_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Seen from outside the running child: no `AWS_*` variable survives the scrub."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAFAKEFAKEFAKEFAKE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "fakesecretfakesecret")
    assert not [name for name in _environment_of_a_running_child() if name.startswith("AWS_")]


@_READS_PROC
def test_the_child_sees_nothing_of_the_workers_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec 8: the real child's environment is `PATH`, `HOME`, `TMPDIR` and the worker's `TZ`."""
    monkeypatch.setenv("LAKANA_DATABASE__URL", "postgresql://worker:worker-only-7f3a@db/lakana")
    monkeypatch.setenv("LAKANA_SECRETS__MASTER_KEY", "worker-only-key-91c2")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "worker-only-secret-5d1e")
    monkeypatch.setenv("TZ", "Europe/Berlin")

    environment = _environment_of_a_running_child()

    assert set(environment) <= {"PATH", "HOME", "TMPDIR", "TZ", "LC_CTYPE"}
    assert {"PATH", "HOME", "TMPDIR"} <= set(environment)
    assert environment["TZ"] == "Europe/Berlin"
    assert "worker-only" not in "".join(environment.values())
    assert Path(environment["HOME"]).name == "home"
    assert Path(environment["HOME"]).parent.name.startswith("lakana-spill-")
    assert Path(environment["TMPDIR"]).parent == Path(environment["HOME"]).parent


@pytest.mark.parametrize("sql", ["", "-- comment only", ";"])
def test_sql_that_produces_no_result_raises_validation_error(sql: str) -> None:
    """DuckDB returns None for these, which must not surface as an AttributeError."""
    with pytest.raises(ValidationError):
        execute_query(sql)


def test_a_quote_in_a_resolved_url_cannot_inject_sql(tmp_path: Path) -> None:
    """A crafted storage object key is a filename to look for, never SQL to run.

    A raw-zone landing area accepts arbitrary writes by design, so an object
    key is attacker-controlled text. Interpolating the resolved URL into a
    list literal let a key close the string and append its own SQL to
    another principal's authorized query — including folding in files from
    outside the resolved set. The first half of this test runs that exact
    construction to show it really executed the payload; the second half
    runs the same URL through the real rewrite, where it is bound as a
    parameter and fails as a missing file instead.
    """
    probe = tmp_path / "probe.parquet"
    pq.write_table(pa.table({"id": [1, 2, 3]}), probe)
    evil = f"{probe}']) UNION ALL SELECT 42 AS id FROM (SELECT ['"

    interpolated = f"WITH a AS (SELECT * FROM read_parquet(['{evil}'])) SELECT id FROM a"
    assert (
        42 in execute_query(interpolated, read_prefixes=(f"{tmp_path}/",)).column("id").to_pylist()
    )

    rewritten = rewrite_query(
        "SELECT id FROM raw.evil",
        sources={"raw.evil": TableSource(urls=[evil], file_format="parquet")},
    )

    assert evil not in rewritten.sql
    with pytest.raises(ValidationError, match="No files found that match the pattern"):
        execute_query(rewritten.sql, rewritten.parameters, read_prefixes=(f"{tmp_path}/",))


def test_result_exactly_at_the_row_cap_succeeds() -> None:
    """A result of exactly `max_result_rows` rows is returned whole."""
    result = execute_query(
        "SELECT * FROM range(5) t(x)", limits=QueryLimitsConfig(max_result_rows=5)
    )
    assert result.num_rows == 5


def test_result_over_the_row_cap_fails() -> None:
    """One row over the cap fails, naming the limit and the remedy; nothing is truncated."""
    with pytest.raises(ValidationError, match=r"max_result_rows.*5.*LIMIT"):
        execute_query("SELECT * FROM range(6) t(x)", limits=QueryLimitsConfig(max_result_rows=5))


def test_result_over_the_byte_cap_fails() -> None:
    """A result whose Arrow buffers exceed `max_result_bytes` fails the same way."""
    with pytest.raises(ValidationError, match=r"max_result_bytes.*LIMIT"):
        execute_query(
            "SELECT repeat('x', 1000) AS s FROM range(100)",
            limits=QueryLimitsConfig(max_result_bytes=10_000),
        )


def test_row_cap_is_enforced_across_batches() -> None:
    """The cap counts rows across every fetched batch, not per batch."""
    with pytest.raises(ValidationError, match="max_result_rows"):
        execute_query(
            "SELECT * FROM range(200000) t(x)",
            limits=QueryLimitsConfig(max_result_rows=150_000),
        )


def test_a_masked_columns_rewritten_expression_never_leaks_through_a_line_echo(
    tmp_path: Path,
) -> None:
    """A column mask's own replacement expression must never reach the error.

    Reproduces the scenario the `LINE n:` stripping fix exists for: a
    column mask's restricted-subset expression is spliced directly into
    the generated CTE (`query_rewrite._reader_select_sql`), and DuckDB's
    own error text quotes the *whole* rewritten statement back — including
    that expression — under a `LINE n:` marker whenever the query, not the
    mask itself, is what fails (here, a missing source file plus an
    unrelated unresolved column in the same statement).
    """
    marker = "MASK_SECRET_TXT"
    source = TableSource(
        urls=[str(tmp_path / "does-not-exist.parquet")],
        file_format="parquet",
        columns={"id": "long", "ssn": "string"},
        column_masks={"ssn": f"'{marker}'"},
    )
    rewritten = rewrite_query(
        "SELECT id, ssn, nonexistent_col FROM gold.customers", {"gold.customers": source}
    )
    assert marker in rewritten.sql, "the mask's own expression must actually be in the CTE"

    with pytest.raises(ValidationError) as exc_info:
        execute_query(rewritten.sql, rewritten.parameters, read_prefixes=(f"{tmp_path}/",))

    assert marker not in str(exc_info.value)


def test_a_late_execution_error_raises_validation_error() -> None:
    """A DuckDB error raised while fetching a later batch is translated too.

    Past the first batch, DuckDB's execution failure reaches Python as a
    plain `OSError` (`pyarrow.ArrowIOError` *is* `OSError` — not an
    `ArrowException` subclass, and not a `duckdb.Error`), raised from `for
    batch in reader` itself rather than from `connection.execute`. A single
    small batch that hits `error()` surfaces through DuckDB's own Python
    exception translation instead (already covered by
    `test_a_syntax_error_raises_validation_error`'s kind of case), so this
    needs enough rows to force more than one fetch from the reader. DuckDB streams
    a result of more than about 120,000 rows (at 100,000 the error is raised from
    `execute`): with the error after row 150,000 of 200,000 it delivers some 22,000
    rows first, measured on 1.5.5 at one and at four threads; the test holds on 2.0.0.
    The test runs at one thread: with several, DuckDB sometimes reports a cancelled sibling
    task's `INTERRUPT Error` instead of the error (about 1 in 2,000 runs at four threads on
    1.5.5; `query_child.failure` words that case).
    """
    with pytest.raises(ValidationError, match="Query failed") as info:
        execute_query(
            "SELECT CASE WHEN x > 150000 THEN error('boom') ELSE x END FROM range(200000) t(x)",
            limits=QueryLimitsConfig(threads=1),
        )
    assert "boom" in str(info.value)


def test_empty_result_keeps_its_schema() -> None:
    """Zero rows still returns a table with the query's columns and types."""
    result = execute_query("SELECT 1::BIGINT AS x WHERE false")
    assert result.num_rows == 0
    assert result.schema.names == ["x"]
    assert result.schema.field("x").type == pa.int64()


def test_memory_limit_is_applied() -> None:
    """The configured `memory_limit` is set on the connection the query runs on."""
    result = execute_query(
        "SELECT current_setting('memory_limit') AS m",
        limits=QueryLimitsConfig(memory_limit="512MiB"),
    )
    assert result.column("m").to_pylist() == ["512.0 MiB"]


def test_an_invalid_memory_limit_is_a_validation_error() -> None:
    """A malformed operator setting surfaces as the usual error, never a raw DuckDB one."""
    with pytest.raises(ValidationError, match="Query failed"):
        execute_query("SELECT 1", limits=QueryLimitsConfig(memory_limit="lots"))


def test_stream_query_yields_the_schema_then_batches() -> None:
    """A write stream exposes the result schema before any batch is read."""
    with stream_query("SELECT 1 AS a, 'x' AS b") as (
        schema,
        batches,
    ):
        assert schema.names == ["a", "b"]
        assert sum(batch.num_rows for batch in batches) == 1


def test_stream_query_exports_timestamptz_in_utc_whatever_the_session_zone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DuckDB exports TIMESTAMPTZ in its session zone; writes pin UTC.

    The child takes its session zone from the worker's `TZ`, so a read comes back in Berlin's
    zone (which shows the zone really arrived), while a write and a describe still say UTC.
    """
    monkeypatch.setenv("TZ", "Europe/Berlin")
    sql = "SELECT TIMESTAMPTZ '2026-01-01 12:00:00+00' AS ts"

    assert execute_query(sql).schema.field("ts").type == (pa.timestamp("us", tz="Europe/Berlin"))
    with stream_query(sql) as (schema, _):
        assert schema.field("ts").type == pa.timestamp("us", tz="UTC")
    assert describe_query(sql).field("ts").type == pa.timestamp("us", tz="UTC")


def test_a_failure_mid_stream_surfaces_as_a_validation_error() -> None:
    """A DuckDB error after the first batch is a `ValidationError`, never an `OSError`.

    One thread: with several, DuckDB sometimes reports a sibling's interrupt instead.
    """
    sql = (
        "SELECT CASE WHEN range > 300000 THEN error('boom') ELSE range END AS x FROM range(1000000)"
    )
    with (
        pytest.raises(ValidationError, match="Query failed"),
        stream_query(sql, limits=_ONE_THREAD) as (_, batches),
    ):
        for _ in batches:
            pass


def test_describe_query_plans_without_returning_rows() -> None:
    """`describe_query` returns the result schema."""
    schema = describe_query("SELECT 1 AS a, 2.5 AS b")

    assert schema.names == ["a", "b"]


def test_describe_query_reports_a_bad_query_as_a_validation_error() -> None:
    """A query that doesn't plan is a `ValidationError`."""
    with pytest.raises(ValidationError, match="Query failed"):
        describe_query("SELECT missing_column FROM (SELECT 1 AS a)")


@pytest.mark.parametrize("sql", ["", "-- comment only", ";"])
def test_stream_query_on_sql_that_produces_no_result_raises_validation_error(sql: str) -> None:
    """`stream_query` mirrors `execute_query`'s handling of DuckDB's `None` result."""
    with pytest.raises(ValidationError), stream_query(sql):
        pass


def test_stream_query_reports_a_bad_query_as_a_validation_error() -> None:
    """A query that fails to parse before it can start streaming is a `ValidationError`."""
    with (
        pytest.raises(ValidationError, match="Query failed"),
        stream_query("SELECT FROM"),
    ):
        pass


@pytest.mark.parametrize("run", ["execute", "stream", "describe"])
def test_parameters_bind_by_name_only(run: str) -> None:
    """Nothing binds positionally: a sequence is refused before DuckDB sees it.

    A positional binding is what let a user placeholder reach a URL list; a
    mapping is the only shape the rewriter produces.
    """
    positional: object = [1]
    with pytest.raises(TypeError, match="by name"):
        if run == "execute":
            execute_query("SELECT ?::INT AS x", positional)  # ty: ignore[invalid-argument-type]
        elif run == "stream":
            with stream_query(
                "SELECT ?::INT AS x",
                positional,  # ty: ignore[invalid-argument-type]
            ):
                pass
        else:
            describe_query(
                "SELECT ?::INT AS x",
                positional,  # ty: ignore[invalid-argument-type]
            )


def test_a_variable_is_set_in_every_entry_point() -> None:
    """A mapping is set as variables for a read, a write stream and a plan alike."""
    sql = "SELECT getvariable('v_test')::INT AS x"

    assert execute_query(sql, {"v_test": 7}).column("x").to_pylist() == [7]
    with stream_query(sql, {"v_test": 7}) as (_, batches):
        assert [row["x"] for batch in batches for row in batch.to_pylist()] == [7]
    assert describe_query(sql, {"v_test": 7}).names == ["x"]


_SLOW_SQL = "SELECT sum(hash(a.range + b.range)) AS n FROM range(40000) a, range(40000) b"
"""A query that runs for seconds, far longer than any limit a test sets.

A cross join of two 40,000-row ranges aggregated to one value: 1.6 billion hashes, single
threaded (9.4 to 9.6 s at 1, 2 and 4 threads; 5.5 s at 30,000 rows, 21.7 s at 60,000), and a flat
59 MB of resident memory, nothing the join retains. If the limit under test fails
the query still ends by itself within a minute (it was `range(1000000)` twice, hours)."""

_OVER_THE_LIMIT_ROWS = "SELECT i, repeat('x', 100) || i::VARCHAR AS b FROM range(200000) AS r(i)"
"""About 22 MB of rows, which a sort or an aggregation cannot hold at a 32MB limit."""

_SMALL_LIMIT = QueryLimitsConfig(memory_limit="32MB", threads=1)
"""A limit the rows above exceed; one thread, because the spilling sort fails at two."""

_SPILLING_SQL = (
    "SELECT max(rn) AS n FROM "
    f"(SELECT row_number() OVER (ORDER BY b DESC) AS rn FROM ({_OVER_THE_LIMIT_ROWS}))"
)
"""A sort too large for a 32MB memory limit at one thread, so it must spill to disk.

It needs about 160 MB of resident memory in the test process even with no limit at all, and
finishes in under 0.1 s (it was a 30-million-row `ORDER BY random()` at a 200 MB limit). The
result is one row, `n == 200000`. Spilling works at one thread only: at two the same query
fails with an out-of-memory error."""


def test_a_query_past_the_time_limit_fails_with_an_actionable_message() -> None:
    """The watchdog interrupts a slow query, which ends as a `ValidationError`."""
    started = time.monotonic()
    with pytest.raises(ValidationError, match=r"Query exceeded the time limit \(0\.5 s\)") as info:
        execute_query(_SLOW_SQL, limits=QueryLimitsConfig(timeout_seconds=0.5))
    assert time.monotonic() - started < 5
    assert "LIMIT" in str(info.value)


def test_a_fast_query_is_untouched_by_the_time_limit() -> None:
    """A query that finishes before the deadline is unaffected, and the timer is gone."""
    before = threading.active_count()
    table = execute_query("SELECT 1 AS x", limits=QueryLimitsConfig(timeout_seconds=30))
    assert table.to_pylist() == [{"x": 1}]
    assert threading.active_count() == before


def test_a_stream_past_the_time_limit_fails_mid_iteration() -> None:
    """For a write stream the deadline covers the whole iteration, not just `execute`."""
    sql = (
        "SELECT range AS x FROM range(1000) UNION ALL "
        f"SELECT CAST(n % 7 AS BIGINT) FROM ({_SLOW_SQL})"
    )
    with (
        pytest.raises(ValidationError, match="Query exceeded the time limit"),
        stream_query(sql, limits=QueryLimitsConfig(timeout_seconds=0.5)) as (_, batches),
    ):
        for _ in batches:
            pass


def test_a_stream_that_outlives_the_deadline_between_batches_fails_on_the_next() -> None:
    """The deadline is the generator's lifetime: time spent by the consumer counts."""
    sql = "SELECT range AS x FROM range(1000000)"
    with (
        pytest.raises(ValidationError, match="Query exceeded the time limit"),
        stream_query(sql, limits=QueryLimitsConfig(timeout_seconds=0.3)) as (_, batches),
    ):
        iterator = iter(batches)
        next(iterator)
        time.sleep(0.6)
        for _ in iterator:
            pass


def test_threads_and_max_temp_directory_size_are_applied() -> None:
    """The configured `threads` and `max_temp_directory_size` reach the connection."""
    table = execute_query(
        "SELECT current_setting('threads') AS t, current_setting('max_temp_directory_size') AS s",
        spill=True,
        limits=QueryLimitsConfig(threads=2, max_temp_directory_size="3GB"),
    )
    row = table.to_pylist()[0]
    assert row["t"] == 2
    assert row["s"] == "2.7 GiB"


def test_every_run_gets_its_own_temp_directory_that_is_removed_afterwards() -> None:
    """The spill directory exists during the query, is fresh per run, and is gone after.

    It is the `tmp` directory inside the child's private directory, which goes with it.
    """
    sql = "SELECT current_setting('temp_directory') AS d"
    first = execute_query(sql).to_pylist()[0]["d"]
    second = execute_query(sql).to_pylist()[0]["d"]
    assert first != second
    for spill in (first, second):
        assert Path(spill).name == "tmp"
        assert Path(spill).parent.name.startswith("lakana-spill-")
        assert not Path(spill).parent.exists()


def test_the_temp_directory_is_removed_when_the_query_fails() -> None:
    """A failing query leaves no spill directory behind, in every entry point."""
    directories: list[str] = []
    real = tempfile.mkdtemp

    def record(prefix: str) -> str:
        path = real(prefix=prefix)
        directories.append(path)
        return path

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(query_process.tempfile, "mkdtemp", record)
        with pytest.raises(ValidationError):
            execute_query("SELEKT 1")
        with pytest.raises(ValidationError), stream_query("SELEKT 1"):
            pass
        with pytest.raises(ValidationError):
            describe_query("SELEKT 1")
        with stream_query("SELECT 1 AS x") as (_, batches):
            list(batches)
        describe_query("SELECT 1 AS x")
    assert len(directories) == 5
    assert not any(Path(directory).exists() for directory in directories)


def test_a_spilling_query_works_in_local_mode_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the local filesystem enabled, DuckDB really spills into the per-run directory."""
    directory: list[str] = []
    real_mkdtemp = tempfile.mkdtemp

    def record(prefix: str) -> str:
        path = real_mkdtemp(prefix=prefix)
        directory.append(path)
        return path

    monkeypatch.setattr(query_process.tempfile, "mkdtemp", record)
    finished = threading.Event()
    seen: list[str] = []

    def poll() -> None:
        while not finished.is_set():
            if directory:
                with contextlib.suppress(OSError):  # the child's directory goes when it ends
                    seen.extend(os.listdir(Path(directory[0]) / "tmp"))
            time.sleep(0.001)

    poller = threading.Thread(target=poll)
    poller.start()
    try:
        table = execute_query(
            _SPILLING_SQL,
            spill=True,
            limits=_SMALL_LIMIT,
        )
    finally:
        finished.set()
        poller.join()
    assert table.to_pylist() == [{"n": 200_000}]
    assert seen, "no spill file ever appeared in the child's temp directory"
    assert not Path(directory[0]).exists()


def test_a_write_stream_timeout_says_storage_time_counts() -> None:
    """The write message mentions that writing to storage counts toward the limit."""
    with (
        pytest.raises(ValidationError, match="Writing to storage counts") as info,
        stream_query(
            "SELECT range AS x FROM range(1000000)", limits=QueryLimitsConfig(timeout_seconds=0.3)
        ) as (_, batches),
    ):
        iterator = iter(batches)
        next(iterator)
        time.sleep(0.6)
        for _ in iterator:
            pass
    assert "time limit" in str(info.value)


_OVER_THE_LIMIT = [
    pytest.param(f"SELECT * FROM ({_OVER_THE_LIMIT_ROWS}) ORDER BY b DESC", id="sort"),
    pytest.param(f"SELECT b, count(*) FROM ({_OVER_THE_LIMIT_ROWS}) GROUP BY b", id="aggregate"),
]
"""Queries whose operators spill once they go over a 32MB `memory_limit`."""


@pytest.mark.parametrize("sql", _OVER_THE_LIMIT)
def test_a_query_that_may_not_spill_is_reported_as_out_of_memory(sql: str) -> None:
    """Without spill a sort or aggregation over the limit is out of memory, with the advice."""
    with pytest.raises(OutOfMemoryQuery, match="LAKANA_QUERY_LIMITS__MEMORY_LIMIT") as info:
        execute_query(sql, limits=_SMALL_LIMIT)
    assert str(info.value) == NO_SPILL


@pytest.mark.parametrize("sql", _OVER_THE_LIMIT)
def test_a_write_stream_that_may_not_spill_is_reported_as_out_of_memory(sql: str) -> None:
    """The same holds for a write stream."""
    with (
        pytest.raises(OutOfMemoryQuery, match="LAKANA_QUERY_LIMITS__MEMORY_LIMIT") as info,
        stream_query(sql, limits=_SMALL_LIMIT) as (_, batches),
    ):
        for _ in batches:
            pass
    assert str(info.value) == NO_SPILL


def test_a_query_that_may_not_spill_writes_no_spill_file_and_reads_no_local_file() -> None:
    """No spill space is given to a query that may not spill, and local reads stay refused."""
    with pytest.raises(OutOfMemoryQuery):
        execute_query(f"SELECT * FROM ({_OVER_THE_LIMIT_ROWS}) ORDER BY b", limits=_SMALL_LIMIT)
    with pytest.raises(ValidationError, match="not one of the tables it reads"):
        execute_query("SELECT * FROM read_csv('/etc/hostname')", limits=_SMALL_LIMIT)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM read_csv('/etc/hostname')",
        "SELECT * FROM '/etc/hostname.csv'",
        "SELECT * FROM range(3) WHERE range::VARCHAR IN (SELECT file FROM glob('/etc/*'))",
        f"SELECT * FROM (SELECT * FROM ({_OVER_THE_LIMIT_ROWS}) ORDER BY b), "
        "read_csv('/etc/hostname')",
    ],
    ids=["read_csv", "replacement_scan", "glob", "read_csv_after_a_sort"],
)
def test_a_refused_local_read_is_an_error_about_the_query(sql: str) -> None:
    """A real local read is refused with the fixed text, which names no file, even after a sort."""
    with pytest.raises(ValidationError) as info:
        execute_query(sql, limits=_SMALL_LIMIT)
    assert type(info.value) is ValidationError
    assert str(info.value) == REFUSED_LOCATION
    assert "hostname" not in str(info.value)


def test_a_refused_local_read_in_a_write_stream_is_still_an_error_about_the_query() -> None:
    """Also for a write stream."""
    with (
        pytest.raises(ValidationError, match="not one of the tables it reads") as info,
        stream_query("SELECT * FROM read_csv('/etc/hostname')") as (_, batches),
    ):
        for _ in batches:
            pass
    assert type(info.value) is ValidationError


def test_a_cancellation_also_stops_a_write_stream() -> None:
    """A stream is stopped as well: the child is asked, then killed.

    The child writes its schema only when the aggregate ends, so entering `stream_query`
    waits; the timer starts before the block, and the cancellation surfaces from entering it.
    """
    event = threading.Event()
    timer = threading.Timer(0.3, event.set)
    timer.start()
    started = time.monotonic()
    with (
        cancellation(event),
        pytest.raises(QueryCancelled),
        stream_query(_SLOW_SQL) as (_, batches),
    ):
        list(batches)
    timer.join()
    assert time.monotonic() - started < 5


def test_timeouts_are_query_timeouts() -> None:
    """The deadline error is a `QueryTimeout` (still a `ValidationError`, same message)."""
    with pytest.raises(QueryTimeout, match="Query exceeded the time limit"):
        execute_query(_SLOW_SQL, limits=QueryLimitsConfig(timeout_seconds=0.3))


def test_io_failures_are_transient(tmp_path: Path) -> None:
    """A DuckDB I/O error (a missing file, an unreachable store) is retryable."""
    missing = tmp_path / "nope.parquet"
    with pytest.raises(TransientQueryError, match="Query failed"):
        execute_query(f"SELECT * FROM read_parquet('{missing}')", read_prefixes=(f"{tmp_path}/",))


def test_other_failures_stay_plain_validation_errors() -> None:
    """A SQL error is not transient."""
    with pytest.raises(ValidationError) as info:
        execute_query("SELECT error('boom')")
    assert type(info.value) is ValidationError


_MID_STREAM_ERRORS = [
    "SELECT CASE WHEN range > 300000 THEN error('boom') ELSE range END AS x FROM range(1000000)",
    "SELECT CAST(CASE WHEN range > 300000 THEN 'a' ELSE '1' END AS INT) AS x FROM range(1000000)",
]


_ONE_THREAD = QueryLimitsConfig(threads=1)
"""Statements that fail late run at one thread: with several, DuckDB sometimes reports a cancelled
sibling's interrupt instead of the error."""


@pytest.mark.parametrize("sql", _MID_STREAM_ERRORS)
def test_a_mid_stream_sql_error_is_permanent_not_transient(sql: str) -> None:
    """DuckDB reports a mid-stream SQL error as a plain `OSError`; it must not retry."""
    with (
        pytest.raises(ValidationError, match="Query failed") as info,
        stream_query(sql, limits=_ONE_THREAD) as (_, batches),
    ):
        for _ in batches:
            pass
    assert type(info.value) is ValidationError


@pytest.mark.parametrize("sql", _MID_STREAM_ERRORS)
def test_a_mid_stream_sql_error_in_a_buffered_query_is_permanent(sql: str) -> None:
    """The same holds for `execute_query`'s fetch loop."""
    with pytest.raises(ValidationError, match="Query failed") as info:
        execute_query(sql, limits=_ONE_THREAD)
    assert type(info.value) is ValidationError


def test_running_out_of_memory_with_variables_set_is_out_of_memory() -> None:
    """Every production query reads variables; they change nothing about the classification."""
    sql = (
        "SELECT * FROM (SELECT i, repeat('x', 100) || i::VARCHAR AS b "
        "FROM range(getvariable('n')) AS r(i)) ORDER BY b DESC"
    )
    with pytest.raises(OutOfMemoryQuery, match="LAKANA_QUERY_LIMITS__MEMORY_LIMIT"):
        execute_query(sql, {"n": 200000}, limits=_SMALL_LIMIT)


@pytest.mark.parametrize(
    "sql",
    ["SELECT * FROM sniff_csv('/etc/hostname')", "SELECT count(*) FROM duckdb_extensions()"],
)
def test_catalog_functions_refused_only_at_execution_are_errors_about_the_query(sql: str) -> None:
    """Two functions DuckDB plans without the disk and refuses while running.

    They read as an error about the query (they once read as out of memory, when a refusal
    while running meant a refused spill). The guard refuses both before the worker.
    """
    with pytest.raises(ValidationError) as info:
        execute_query(sql, limits=_SMALL_LIMIT)
    assert type(info.value) is ValidationError
    assert str(info.value) == REFUSED_LOCATION


def test_listing_temporary_files_reads_no_file() -> None:
    """``duckdb_temporary_files`` lists DuckDB's own spill files; the guard refuses it too."""
    assert execute_query("SELECT count(*) AS n FROM duckdb_temporary_files()").to_pylist() == [
        {"n": 0}
    ]


def _cross_join_of_literals(rows: int = 3000) -> str:
    """Build a query that needs more memory than a small limit from a literal `VALUES` list.

    Parameters
    ----------
    rows
        Literal rows; the query pairs every row with every other one (9 million strings at 3000).

    Returns
    -------
    str
        The SQL. No generating function: the data is in the text.
    """
    values = ",".join(f"({i},'value-{i:06d}')" for i in range(rows))
    return f"WITH a(x, s) AS (VALUES {values}) SELECT list(a.s || b.s) AS l FROM a, a b"


def test_a_real_out_of_memory_query_fails_with_the_out_of_memory_type() -> None:
    """Against real DuckDB: an aggregate that cannot spill exceeds a small limit.

    The query is a self cross join of literal rows, not a generated series: it fails
    in about 0.1 s at 100MB. Spilling is allowed, so the failure is DuckDB's own text, not the
    fixed "cannot spill" wording of a query that may not spill.
    """
    with pytest.raises(OutOfMemoryQuery, match="LAKANA_QUERY_LIMITS__MEMORY_LIMIT"):
        execute_query(
            _cross_join_of_literals(),
            spill=True,
            limits=QueryLimitsConfig(memory_limit="100MB", threads=2),
        )


@pytest.mark.parametrize("size", ["1GB", "512MiB", "100MB"])
def test_check_limits_accepts_the_sizes_duckdb_accepts(size: str) -> None:
    """A valid size passes the start-up probe, which runs no query."""
    executor_module.check_limits(QueryLimitsConfig(memory_limit=size, max_temp_directory_size=size))


@pytest.mark.parametrize(
    ("field", "variable"),
    [
        ("memory_limit", "LAKANA_QUERY_LIMITS__MEMORY_LIMIT"),
        ("max_temp_directory_size", "LAKANA_QUERY_LIMITS__MAX_TEMP_DIRECTORY_SIZE"),
    ],
)
def test_check_limits_names_the_setting_a_size_it_cannot_use(field: str, variable: str) -> None:
    """A malformed size fails at start-up with the variable's name, not on every query."""
    limits = QueryLimitsConfig(**{field: "lots\x1b[31m" + "x" * 500})
    with pytest.raises(ValueError, match=variable) as caught:
        executor_module.check_limits(limits)
    message = str(caught.value)
    assert "\x1b" not in message
    assert len(message) < 300
    assert "1GB" in message


SCRIPT = Path(__file__).parent / "scripted_child.py"
_LIMITS = QueryLimitsConfig(timeout_seconds=30)
_TIME_LIMIT = (
    "Query exceeded the time limit (30 s). Aggregate or filter earlier, add a LIMIT, or ask the "
    "operator to raise LAKANA_QUERY_LIMITS__TIMEOUT_SECONDS."
)
_WRITE_TIME_LIMIT = (
    "Query exceeded the time limit (30 s). Writing to storage counts toward the limit, so a slow "
    "store can trip it; reduce the data written, or ask the operator to raise "
    "LAKANA_QUERY_LIMITS__TIMEOUT_SECONDS."
)
_KILLED = (
    " The query did not stop when asked and was ended after the grace period "
    "(10 s, LAKANA_QUERY_LIMITS__KILL_GRACE_SECONDS)."
)
_BROKEN = (
    "Query failed: the query process ended without a complete result (exit status {status}). "
    "Nothing was stored. Run the query again; if it fails the same way, tell the operator: "
    "the worker's log has the details."
)
_FROM_OUTSIDE = (
    "Query failed: the query process was ended from outside the worker (signal 9). "
    "Nothing was stored. If the machine or the container ran out of memory, the kernel "
    "ends the largest process: read or compute less (filter or aggregate earlier, or "
    "select fewer columns), or give the worker more memory. Otherwise run the query again."
)
_STRING_AGG = "SELECT length(string_agg('', '')) AS n FROM range(40000) a, range(40000) b"
"""The second shape: about 5 s by itself at a flat 88 MB, which bounds its test."""
_WIDE_ROWS = "SELECT i, repeat('x', 50) AS s FROM range(3000000) AS t(i)"
"""About 200 MB of result that the child never holds in memory; no test reads it to the end."""


@pytest.fixture
def scripted(monkeypatch: pytest.MonkeyPatch) -> Callable[[str], None]:
    """Make the executor start the scripted child with one behaviour.

    Parameters
    ----------
    monkeypatch
        Replaces the executor's command for the test.

    Returns
    -------
    Callable
        Takes the behaviour's name.
    """

    def use(behaviour: str) -> None:
        """Choose the behaviour.

        Parameters
        ----------
        behaviour
            One of `scripted_child.py`'s behaviours.
        """
        monkeypatch.setattr(executor_module, "_ARGV", [sys.executable, str(SCRIPT), behaviour])

    return use


@pytest.mark.parametrize(
    ("failed", "writing", "kind", "message"),
    [
        (ChildFailed(stop=StopReason.TIMEOUT), False, QueryTimeout, _TIME_LIMIT),
        (
            ChildFailed(stop=StopReason.TIMEOUT, killed=True),
            False,
            QueryTimeout,
            _TIME_LIMIT + _KILLED,
        ),
        (ChildFailed(stop=StopReason.TIMEOUT), True, QueryTimeout, _WRITE_TIME_LIMIT),
        (
            ChildFailed(stop=StopReason.CANCEL_EVENT, killed=True),
            False,
            QueryCancelled,
            CANCELLED_MESSAGE,
        ),
        (
            ChildFailed(stop=StopReason.CANCEL_REQUEST),
            False,
            QueryCancelRequested,
            CANCELLED_MESSAGE,
        ),
        (
            ChildFailed(stop=StopReason.CANCEL_REQUEST, killed=True),
            False,
            QueryCancelRequested,
            CANCELLED_MESSAGE + _KILLED,
        ),
        (
            ChildFailed(stop=StopReason.DRAIN),
            False,
            QueryCancelled,
            "The query was stopped because the worker is shutting down. Run it again.",
        ),
        (
            ChildFailed(stop=StopReason.OUT_OF_MEMORY, killed=True, detail="over"),
            False,
            MemoryStopQuery,
            "over",
        ),
        (
            ChildFailed(stop=StopReason.OUT_OF_MEMORY, killed=True),
            False,
            MemoryStopQuery,
            "Query failed: the worker ran out of memory and stopped the query.",
        ),
        (
            ChildFailed(stop=StopReason.TIMEOUT, error=ErrorDocument("invalid", "x"), returncode=3),
            False,
            QueryTimeout,
            _TIME_LIMIT,
        ),
        (
            ChildFailed(error=ErrorDocument("invalid", "Query failed: a"), returncode=3),
            False,
            ValidationError,
            "Query failed: a",
        ),
        (
            ChildFailed(error=ErrorDocument("transient", "Query failed: b"), returncode=3),
            False,
            TransientQueryError,
            "Query failed: b",
        ),
        (
            ChildFailed(error=ErrorDocument("out_of_memory", "Query failed: c"), returncode=3),
            False,
            OutOfMemoryQuery,
            "Query failed: c",
        ),
        (
            ChildFailed(error=ErrorDocument("internal", "Query failed: d"), returncode=3),
            False,
            DuckDBInternalError,
            "Query failed: d Run the query again; if it fails the same way, tell the operator: "
            "the worker's log has the details.",
        ),
        (
            ChildFailed(error=ErrorDocument("interrupted", "asked"), returncode=3),
            False,
            DuckDBInternalError,
            _BROKEN.format(status=3),
        ),
        (
            ChildFailed(
                stop=StopReason.CANCEL_EVENT, error=ErrorDocument("internal", "x"), returncode=3
            ),
            False,
            QueryCancelled,
            CANCELLED_MESSAGE,
        ),
        (
            ChildFailed(
                stop=StopReason.CANCEL_REQUEST, error=ErrorDocument("internal", "x"), returncode=3
            ),
            False,
            QueryCancelRequested,
            CANCELLED_MESSAGE,
        ),
        (
            ChildFailed(stop=StopReason.DRAIN, error=ErrorDocument("internal", "x"), returncode=3),
            False,
            QueryCancelled,
            "The query was stopped because the worker is shutting down. Run it again.",
        ),
        (
            ChildFailed(
                stop=StopReason.WORKER_STALE, error=ErrorDocument("internal", "x"), returncode=3
            ),
            False,
            WorkerStaleQuery,
            WORKER_STALE_MESSAGE,
        ),
        (ChildFailed(returncode=0), False, DuckDBInternalError, _BROKEN.format(status=0)),
        (ChildFailed(returncode=7), False, DuckDBInternalError, _BROKEN.format(status=7)),
        (ChildFailed(returncode=-9), False, TransientQueryError, _FROM_OUTSIDE),
        (
            ChildFailed(returncode=-9, own_kill=True),
            False,
            DuckDBInternalError,
            _BROKEN.format(status=-9),
        ),
        (
            ChildFailed(
                returncode=-9, own_kill=True, error=ErrorDocument("internal", "Query failed: e")
            ),
            False,
            DuckDBInternalError,
            "Query failed: e Run the query again; if it fails the same way, tell the operator: "
            "the worker's log has the details.",
        ),
        (
            ChildFailed(detail="could not start the query process: No such file or directory"),
            False,
            DuckDBInternalError,
            "Query failed: the worker could not start the query's process. This is a problem "
            "with the worker's installation, not with the query: tell the operator; the "
            "worker's log has the details.",
        ),
    ],
)
def test_each_end_of_a_child_becomes_the_error_the_pipeline_records(
    failed: ChildFailed, writing: bool, kind: type[Exception], message: str
) -> None:
    """The stop reason wins over the child's own document; the exact type decides the cause.

    A signal the worker did not send is transient; one it sent itself while cleaning up is not.
    """
    error = executor_module._translate(failed, _LIMITS, writing)
    assert type(error) is kind
    assert str(error) == message


def test_a_kill_after_the_grace_is_named_in_the_workers_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Spec 5: whether the query stopped when asked or was killed is recorded."""
    with caplog.at_level("WARNING", logger="lakana_worker.executor"):
        executor_module._translate(
            ChildFailed(stop=StopReason.CANCEL_EVENT, killed=True), _LIMITS, False
        )
        executor_module._translate(ChildFailed(stop=StopReason.TIMEOUT), _LIMITS, False)
        executor_module._translate(
            ChildFailed(stop=StopReason.OUT_OF_MEMORY, killed=True), _LIMITS, False
        )
    assert caplog.messages == [
        "A query did not stop when asked (cancel_event) and was ended after the grace period "
        "of 10 s (LAKANA_QUERY_LIMITS__KILL_GRACE_SECONDS)."
    ]


@pytest.mark.slow
def test_a_statement_that_ignores_the_ask_is_killed_and_the_next_query_runs(
    scripted: Callable[[str], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    scripted("ignores_sigterm")
    limits = QueryLimitsConfig(timeout_seconds=2.0, kill_grace_seconds=0.3)
    started = time.monotonic()
    with pytest.raises(QueryTimeout) as caught:
        execute_query("SELECT 1", limits=limits)
    assert time.monotonic() - started < 10
    assert str(caught.value).endswith(
        " The query did not stop when asked and was ended after the grace period "
        "(0.3 s, LAKANA_QUERY_LIMITS__KILL_GRACE_SECONDS)."
    )
    monkeypatch.setattr(executor_module, "_ARGV", None)
    assert execute_query("SELECT 42 AS a").to_pydict() == {"a": [42]}
    assert REGISTRY.pids() == []


@pytest.mark.parametrize("sql", [_SLOW_SQL, _STRING_AGG], ids=["cross_join", "string_agg"])
def test_the_shapes_of_292_end_within_the_time_limit_and_the_grace(sql: str) -> None:
    """On DuckDB 1.5.5 and 2.0.0 both stop when asked; either way they end, the worker goes on."""
    limits = QueryLimitsConfig(timeout_seconds=0.5, kill_grace_seconds=1.0, threads=2)
    started = time.monotonic()
    with pytest.raises(QueryTimeout, match="LAKANA_QUERY_LIMITS__TIMEOUT_SECONDS"):
        execute_query(sql, limits=limits)
    assert time.monotonic() - started < 6
    assert REGISTRY.pids() == []


def test_a_result_over_a_cap_ends_the_child_at_once() -> None:
    """The cap stops the query after one megabyte, not after 200."""
    started = time.monotonic()
    with pytest.raises(ResultTooLarge, match="max_result_bytes"):
        execute_query(_WIDE_ROWS, limits=QueryLimitsConfig(max_result_bytes=1_000_000))
    assert time.monotonic() - started < 10
    assert REGISTRY.pids() == []
    assert glob.glob(str(Path(tempfile.gettempdir()) / (spill_prefix() + "*"))) == []


def _kill_the_child_once_started() -> threading.Thread:
    """Start a thread that kills the first registered child's group with SIGKILL.

    Returns
    -------
    threading.Thread
        The started thread; the caller joins it.
    """

    def kill_when_started() -> None:
        """Wait for a child to be registered, give it a moment, kill its group."""
        deadline = time.monotonic() + 10
        while not REGISTRY.pids() and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(0.3)
        for pid in REGISTRY.pids():
            os.killpg(pid, signal.SIGKILL)

    killer = threading.Thread(target=kill_when_started, daemon=True)
    killer.start()
    return killer


def test_a_child_killed_from_outside_fails_the_query_as_one_a_retry_may_cure() -> None:
    """Spec 8: the kernel's out-of-memory killer, or an operator, ends the process."""
    killer = _kill_the_child_once_started()
    with pytest.raises(TransientQueryError) as caught:
        execute_query(_SLOW_SQL)
    killer.join(5)
    assert str(caught.value) == _FROM_OUTSIDE
    assert REGISTRY.pids() == []


_ENDS_AFTER_THE_LAST_BATCH = pytest.mark.parametrize(
    ("behaviour", "kind", "message"),
    [
        ("full_stream_then_killed", TransientQueryError, _FROM_OUTSIDE),
        ("exit_7_after_a_full_stream", DuckDBInternalError, _BROKEN.format(status=7)),
    ],
)
"""R3.8: the scripted child hands over all three batches and the end marker; then a signal from
outside kills it, or it exits with a failing status. The batches reached the caller before the
proof, so nothing may be returned, stored or committed on the strength of them."""


@_ENDS_AFTER_THE_LAST_BATCH
def test_a_read_returns_no_table_when_the_child_ends_after_its_last_batch(
    scripted: Callable[[str], None], behaviour: str, kind: type[Exception], message: str
) -> None:
    scripted(behaviour)
    with pytest.raises(kind) as caught:
        execute_query("SELECT 1")
    assert str(caught.value) == message


@_ENDS_AFTER_THE_LAST_BATCH
def test_a_write_stream_reaches_its_commit_point_only_through_the_end_of_the_iterator(
    scripted: Callable[[str], None], behaviour: str, kind: type[Exception], message: str
) -> None:
    scripted(behaviour)
    received: list[int] = []
    committed: list[bool] = []
    with (
        pytest.raises(kind) as caught,
        stream_query("SELECT 1") as (_, batches),
    ):
        for batch in batches:
            received.append(batch.num_rows)
        committed.append(True)  # stands in for the commit that follows the last batch
    assert received == [10, 10, 10]
    assert committed == []
    assert str(caught.value) == message


@_ENDS_AFTER_THE_LAST_BATCH
def test_a_describe_returns_no_schema_when_the_child_ends_after_its_last_batch(
    scripted: Callable[[str], None], behaviour: str, kind: type[Exception], message: str
) -> None:
    scripted(behaviour)
    with pytest.raises(kind) as caught:
        describe_query("SELECT 1")
    assert str(caught.value) == message


def test_a_failed_stream_raises_its_error_again_and_never_ends_normally(
    scripted: Callable[[str], None],
) -> None:
    """A caller that caught the error and reads on must not see a stream that just ended."""
    scripted("exit_7_after_a_full_stream")
    with stream_query("SELECT 1") as (_, batches):
        with pytest.raises(DuckDBInternalError) as first:
            list(batches)
        with pytest.raises(DuckDBInternalError) as again:
            next(iter(batches))
        assert again.value is first.value


def test_a_time_limit_that_passes_after_the_last_batch_is_noticed_when_the_stream_ends() -> None:
    """R3.9: the consumer's own work between the last batch and the end counts, and is noticed.

    One batch holds the whole result and the child has long since exited; the consumer then
    takes longer than the limit before it asks for the end. The end is a read like any other,
    so the commit that follows it is never reached.
    """
    committed: list[bool] = []
    with (
        pytest.raises(QueryTimeout, match="Writing to storage counts"),
        stream_query(
            "SELECT range AS x FROM range(1000)", limits=QueryLimitsConfig(timeout_seconds=0.5)
        ) as (_, batches),
    ):
        iterator = iter(batches)
        assert next(iterator).num_rows == 1000
        time.sleep(0.8)
        for _ in iterator:
            pass
        committed.append(True)
    assert committed == []


def test_a_set_cancellation_interrupts_a_running_query() -> None:
    """A cancellation event stops the query child; the run fails with `QueryCancelled`."""
    event = threading.Event()
    timer = threading.Timer(0.3, event.set)
    timer.start()
    started = time.monotonic()
    with cancellation(event), pytest.raises(QueryCancelled, match=CANCELLED_MESSAGE) as caught:
        execute_query(_SLOW_SQL, limits=QueryLimitsConfig(timeout_seconds=30))
    assert str(caught.value) == CANCELLED_MESSAGE
    assert time.monotonic() - started < 5
    timer.join()


@pytest.mark.slow
def test_a_drain_during_a_tasks_query_is_left_to_its_lease_keeper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Plan decision 7: a task's query is released at drain, as before, never cancelled.

    The lease keeper owns the drain of a task (`LAKANA_RUN__DRAIN_GRACE_SECONDS`, then
    `RELEASED`). The child's drain clock must not run for a query that runs for a task run, or
    it would fire first and the task would end `CANCELLED`.
    """
    monkeypatch.setattr(executor_module, "_DRAINING", None)
    drain = threading.Event()
    install_drain(drain, 0.2)

    def no_session() -> NoReturn:
        raise AssertionError("the keeper renews nothing in this test")

    keeper = LeaseKeeper(
        no_session,
        uuid.uuid4(),
        "w1",
        1,
        lease_seconds=60,
        renew_seconds=3600,
        drain=drain,
        drain_grace_seconds=0.2,
    )
    keeper.start()
    timer = threading.Timer(0.3, drain.set)
    timer.start()
    try:
        with cancellation(keeper.cancel), pytest.raises(QueryCancelled) as caught:
            execute_query(_SLOW_SQL, limits=QueryLimitsConfig(timeout_seconds=30))
    finally:
        keeper.stop()
        timer.cancel()
    assert str(caught.value) == CANCELLED_MESSAGE
    assert keeper.reason is FailureKind.RELEASED


def test_a_cancel_request_is_its_own_error_type() -> None:
    started = time.monotonic()
    with cancel_requests(lambda: True), pytest.raises(QueryCancelRequested) as caught:
        execute_query(_SLOW_SQL)
    assert str(caught.value) == CANCELLED_MESSAGE
    assert time.monotonic() - started < 5


def test_a_draining_worker_stops_a_query_after_the_grace(
    scripted: Callable[[str], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """SIGTERM sets the drain event; a running query gets the drain grace."""
    scripted("stops_when_asked")
    monkeypatch.setattr(executor_module, "_DRAINING", None)  # restored after the test
    drain = threading.Event()
    install_drain(drain, 0.4)
    drain.set()
    started = time.monotonic()
    with pytest.raises(QueryCancelled) as caught:
        execute_query("SELECT 1")
    assert type(caught.value) is QueryCancelled
    assert str(caught.value) == (
        "The query was stopped because the worker is shutting down. Run it again."
    )
    assert 0.4 <= time.monotonic() - started < 10


def test_a_query_stopped_for_a_stale_heartbeat_fails_as_transient_with_its_message(
    scripted: Callable[[str], None],
) -> None:
    from lakana_worker.pipeline import failure_cause
    from lakana_worker.task_outcome import classify

    scripted("stops_when_asked")
    install_heartbeat(lambda: True)
    started = time.monotonic()
    with pytest.raises(TransientQueryError) as caught:
        execute_query("SELECT 1")
    assert str(caught.value) == WORKER_STALE_MESSAGE
    assert WORKER_STALE_MESSAGE == (
        "The worker running this query lost its connection to the database and stopped the "
        "query. Run it again."
    )
    assert failure_cause(caught.value) is QueryErrorCause.WORKER_LOST
    assert classify(caught.value) is FailureKind.TRANSIENT
    assert time.monotonic() - started < 10


def test_a_tasks_query_is_stopped_too(scripted: Callable[[str], None]) -> None:
    """Unlike the drain, the stale heartbeat applies inside `cancellation`."""
    scripted("stops_when_asked")
    install_heartbeat(lambda: True)
    with cancellation(threading.Event()), pytest.raises(TransientQueryError) as caught:
        execute_query("SELECT 1")
    assert str(caught.value) == WORKER_STALE_MESSAGE


def test_a_worker_with_a_fresh_heartbeat_stops_nothing() -> None:
    install_heartbeat(lambda: False)
    assert execute_query("SELECT 1 AS a").to_pydict() == {"a": [1]}


def test_a_worker_that_is_not_draining_stops_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(executor_module, "_DRAINING", None)
    install_drain(threading.Event(), 0.0)
    assert execute_query("SELECT 1 AS a").to_pydict() == {"a": [1]}


def test_the_request_limit_is_64_mib() -> None:
    """A table of tens of thousands of files must still be readable."""
    assert MAX_REQUEST_BYTES == 64 * 1024 * 1024


def test_a_query_over_too_many_files_is_refused_before_a_process_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """I2: the user is told how many files and what to do; nothing is started."""
    monkeypatch.setattr(executor_module, "MAX_REQUEST_BYTES", 2 * 1024 * 1024)

    def not_started(*_: object, **__: object) -> None:
        raise AssertionError("no process may be started for a request the child would refuse")

    monkeypatch.setattr(executor_module.query_process, "run", not_started)
    urls = [f"https://store.example/data/{'x' * 200}-{i}.parquet" for i in range(10_000)]
    with pytest.raises(ValidationError) as caught:
        execute_query("SELECT len(getvariable('u')) AS n", {"u": urls})
    message = str(caught.value)
    assert type(caught.value) is ValidationError
    assert message.startswith("Query refused: it would read 10,000 data files")
    assert "over its limit of 2 MiB" in message
    assert "maintain" not in message  # maintenance does not merge files, so it cannot help
    assert "partition" not in message  # the worker binds every live file, whatever the WHERE says
    assert "filter" not in message
    assert "`replace`" in message and "fewer, larger files" in message
    assert "Run the query again" not in message
    assert "store.example" not in message


def test_a_request_over_the_limit_without_a_file_list_does_not_talk_about_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A long statement, no file list: the message does not talk about "0 data files"."""
    monkeypatch.setattr(executor_module, "MAX_REQUEST_BYTES", 1024 * 1024)

    def not_started(*_: object, **__: object) -> None:
        raise AssertionError("no process may be started for a request the child would refuse")

    monkeypatch.setattr(executor_module.query_process, "run", not_started)
    with pytest.raises(ValidationError) as caught:
        execute_query("SELECT '" + "x" * (2 * 1024 * 1024) + "' AS v")
    assert str(caught.value) == (
        "Query refused: the request the query process needs for it is 3 MiB, over its limit of "
        "1 MiB. Shorten the statement; load the data it carries into a table and query that "
        "instead."
    )


def test_a_request_of_a_thousand_files_is_sent() -> None:
    """An ordinary many-file read still goes through the check."""
    urls = [f"https://store.example/data/{'x' * 200}-{i}.parquet" for i in range(1000)]
    result = execute_query(
        "SELECT len(getvariable('u')) AS n",
        {"u": urls},
        read_prefixes=("https://store.example/data/",),
    )
    assert result.to_pydict() == {"n": [1000]}


def test_the_start_up_checks_timeout_names_the_check_not_the_query_time_limit(
    scripted: Callable[[str], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A check that does not answer says so and what to look at."""
    scripted("ignores_sigterm")
    monkeypatch.setattr(executor_module, "_CHECK_TIMEOUT_SECONDS", 1.0)
    with pytest.raises(ValueError, match="start-up check") as refused:
        executor_module.check_limits(QueryLimitsConfig(kill_grace_seconds=0.3))
    assert str(refused.value) == (
        "The worker cannot run queries: its start-up check in a query process did not answer "
        "within 1 s. Check that the machine is not out of memory or processor time, that the "
        "worker's Python environment is complete (the duckdb and pyarrow packages) and that its "
        "temp directory is writable."
    )


def test_a_child_that_cannot_start_fails_the_query_and_the_start_up_check(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The query fails with a message for the operator; the worker goes on."""
    missing = tmp_path / "no-such-python"
    monkeypatch.setattr(executor_module, "_ARGV", [str(missing)])
    reason = f"could not start the query process: No such file or directory: {missing}"
    with pytest.raises(DuckDBInternalError) as caught:
        execute_query("SELECT 1")
    assert str(caught.value) == NOT_STARTED_MESSAGE
    assert str(missing) not in str(caught.value)  # the path is for the worker's log only
    logged = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert reason in logged[0].getMessage()
    with pytest.raises(ValueError, match="start-up check") as refused:
        executor_module.check_limits(QueryLimitsConfig())
    assert str(refused.value).startswith(
        "The worker cannot run queries: its start-up check in a query process failed (Query "
        "failed: the worker could not start the query's process."
    )
    assert str(refused.value).endswith(
        "Check that the worker's Python environment is complete (the duckdb and pyarrow "
        "packages) and that its temp directory is writable."
    )
    monkeypatch.setattr(executor_module, "_ARGV", None)
    assert execute_query("SELECT 42 AS a").to_pydict() == {"a": [42]}


def test_a_value_the_child_cannot_take_is_refused_before_a_process_starts() -> None:
    with pytest.raises(ValidationError) as caught:
        execute_query("SELECT getvariable('x') AS x", {"x": 1.5})
    assert str(caught.value) == (
        "Query parameter 'x' has a type the query process cannot take: float."
    )
    assert REGISTRY.pids() == []


def test_a_type_error_that_is_not_about_a_parameter_is_not_turned_into_a_query_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the protocol's own refusal of a value becomes a `ValidationError`; a bug is a bug."""

    def broken(*_: object, **__: object) -> None:
        raise TypeError("an unrelated bug")

    monkeypatch.setattr(executor_module.query_process, "run", broken)
    with pytest.raises(TypeError, match="an unrelated bug"):
        execute_query("SELECT 1")


@pytest.mark.slow
def test_the_worker_process_imports_no_duckdb() -> None:
    """Spec 4.1: the worker's own process no longer opens a DuckDB connection."""
    code = (
        "import sys, lakana_worker.__main__, lakana_worker.pipeline, lakana_worker.tasks\n"
        "print('duckdb' in sys.modules)\n"
    )
    done = subprocess.run(  # nosec B603 -- fixed argv, no shell
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, check=True, timeout=120
    )
    assert done.stdout.strip() == "False"
