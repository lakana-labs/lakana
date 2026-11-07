"""Run rewritten queries, each in a process of its own.

The worker's own process opens no DuckDB connection. Every statement runs in a query child
(`lakana_worker.query_child`), started and stopped by `lakana_worker.query_process`; this
module is the worker-facing API on top of both: what a query is given, which limits apply, and
which error each end of a child becomes.

Isolation is what remains once the query has been rewritten
(`lakana_core.query_rewrite.rewrite_query`) to read only the URLs the catalog resolved: the
child holds no credential (its request carries presigned URLs and nothing else, its environment
is built from scratch), and its connection is configured as `lakana_worker.query_child.configure`
documents: in allow-list mode, opening only the locations of the tables the query reads. A
process boundary, not a sandbox: the child runs as the worker's user.

A query is stopped for four reasons, always by "ask, wait `kill_grace_seconds`, kill": its
time limit, the cancel event of the task run it executes for (`cancellation`), a cancel request
on the query run (`cancel_requests`), and the worker's drain (`install_drain`). The memory
watchdog kills without asking (`lakana_worker.memory`). The time limit is therefore hard;
DuckDB's `memory_limit` stays what it was, a target inside the child.

A result is complete only when the batch iterator the supervisor hands out has ended normally:
the child's exit status was 0 and its stream carried the end marker. Batches arrive before
that proof, so a caller stores a read's result or commits a write only after the iterator has
ended, and never when the block is left earlier (`execute_query` and `describe_query` do so by
construction; the write path's core writer consumes the iterator before it commits).

What the time limit and the other reasons cover is the query, including the time the worker
spends between two batches: they are noticed on every read of the child's stream, and the read
that ends the stream evaluates them one last time. Once the last batch has been handed over and
the stream has ended normally, storing the result or committing the write is not interrupted.
"""

from __future__ import annotations

import logging
import os
import tempfile
import threading
import time
from collections.abc import Callable, Generator, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import cast

import pyarrow as pa

from lakana_core.config import QueryLimitsConfig
from lakana_core.errors import ValidationError
from lakana_worker import query_process
from lakana_worker.ca_bundle import query_ca_bundle
from lakana_worker.query_process import ChildFailed, StopReason, Stops
from lakana_worker.query_protocol import (
    CHECK,
    DESCRIBE,
    EXTENSION_SOURCE_DUCKDB,
    INTERNAL,
    INVALID,
    MAX_REQUEST_BYTES,
    MEMORY_LIMIT_ADVICE,
    OUT_OF_MEMORY,
    READ,
    STORE_CERTIFICATE_UNVERIFIED,
    TRANSIENT,
    WRITE,
    ParameterValue,
    Request,
    encode_request,
)
from lakana_worker.spill import spill_prefix

__all__ = ["MEMORY_LIMIT_ADVICE"]

logger = logging.getLogger(__name__)

_FETCH_BATCH_ROWS = 2048
"""Rows per fetched Arrow batch — DuckDB's own vector size.

Caps are checked by the worker on the batches as they arrive, so a result can overshoot
a cap by at most one batch before failing: at most 2048 rows over `max_result_rows`, and at
most 2048 rows' worth of the query's largest value over `max_result_bytes`.
A larger batch size would raise that residual — a single 65,536-row batch of
wide values was measured overshooting a 1,000,000-byte `max_result_bytes` by
roughly 1.3 GB of RSS before the check ran — so this stays at DuckDB's own
per-batch granularity rather than trading a bigger residual for fewer,
cheaper checks."""

_WRITE_FETCH_BATCH_ROWS = 65_536
"""Rows per fetched batch for a write stream.

No result cap is checked between a write's batches (writes are bounded by
`max_write_rows`, counted by the core writer), so the batch size only trades
per-batch Python overhead against one batch's memory."""


_CHECK_TIMEOUT_SECONDS = 60.0
"""The time limit of the start-up check's child."""

CANCELLED_MESSAGE = "The query was cancelled."
"""`QueryCancelled`'s message."""

DRAIN_MESSAGE = "The query was stopped because the worker is shutting down. Run it again."
"""The message of a query that the worker's drain stopped."""

WORKER_STALE_MESSAGE = (
    "The worker running this query lost its connection to the database and stopped the query. "
    "Run it again."
)
"""The message of a query that the worker stopped because its heartbeat was a lease old."""

KILLED_SUFFIX = (
    " The query did not stop when asked and was ended after the grace period "
    "({grace:g} s, LAKANA_QUERY_LIMITS__KILL_GRACE_SECONDS)."
)
"""Added to a time-limit or cancel message when the child had to be killed."""

INTERNAL_ADVICE = (
    "Run the query again; if it fails the same way, tell the operator: the worker's log has the "
    "details."
)
"""Added to the message of an unexpected error in the query process."""

BROKEN_MESSAGE = (
    "Query failed: the query process ended without a complete result (exit status {status}). "
    "Nothing was stored. Run the query again; if it fails the same way, tell the operator: "
    "the worker's log has the details."
)
"""A child that delivered no complete result and gave no reason."""

KILLED_FROM_OUTSIDE_MESSAGE = (
    "Query failed: the query process was ended from outside the worker (signal {signal}). "
    "Nothing was stored. If the machine or the container ran out of memory, the kernel ends "
    "the largest process: read or compute less (filter or aggregate earlier, or select fewer "
    "columns), or give the worker more memory. Otherwise run the query again."
)
"""A child that a signal ended and the worker did not send."""

NOT_STARTED_MESSAGE = (
    "Query failed: the worker could not start the query's process. This is a problem with the "
    "worker's installation, not with the query: tell the operator; the worker's log has the "
    "details."
)
"""A child that could not be started at all."""

REQUEST_TOO_LARGE_MESSAGE = (
    "Query refused: it would read {files:,} data files, and the request the query process "
    "needs for them is {size} MiB, over its limit of {limit} MiB. Write the table "
    "again with `replace` (for example `lakana sql ... --into <table> --mode replace`) so that "
    "its data lands in fewer, larger files."
)
"""A query over so many files that its request would not fit the child's limit."""

REQUEST_TOO_LARGE_NO_FILES_MESSAGE = (
    "Query refused: the request the query process needs for it is {size} MiB, over its limit of "
    "{limit} MiB. Shorten the statement; load the data it carries into a table and query that "
    "instead."
)
"""A request over the child's limit that holds no file list (a large statement)."""

CHECK_TIMEOUT_MESSAGE = (
    "The worker cannot run queries: its start-up check in a query process did not answer "
    "within {seconds:g} s. Check that the machine is not out of memory or processor time, that "
    "the worker's Python environment is complete (the duckdb and pyarrow packages) and that its "
    "temp directory is writable."
)
"""The start-up check ran into its own time limit."""

_CANCEL: ContextVar[threading.Event | None] = ContextVar("lakana_query_cancel", default=None)
"""The cancellation event of the task run executing in this context, if any."""

_CANCEL_REQUESTED: ContextVar[Callable[[], bool] | None] = ContextVar(
    "lakana_query_cancel_requested", default=None
)
"""Says whether a cancel request was recorded for the query run executing in this context."""

_DRAINING: Callable[[], bool] | None = None
"""Says whether the worker's drain grace is over; set once by `install_drain`."""

_WORKER_STALE: Callable[[], bool] | None = None
"""Says whether the worker's heartbeat is a lease old; set once by `install_heartbeat`."""

_EXTENSION_SOURCE = EXTENSION_SOURCE_DUCKDB
"""Where query children get DuckDB's ``httpfs`` from; set once by `install_extension_source`."""

_ARGV: Sequence[str] | None = None
"""The command that starts a query child; `None` is the real one. Tests start a scripted one."""


class QueryTimeout(ValidationError):
    """The time limit (`timeout_seconds`) stopped the query."""


class QueryCancelled(ValidationError):
    """A cancellation stopped the query: a cancel of its run, a lost lease or a drain."""


class QueryCancelRequested(QueryCancelled):
    """`POST .../queries/{id}/cancel` stopped the query."""


class ResultTooLarge(ValidationError):
    """The result went over `max_result_rows` or `max_result_bytes`."""


class TransientQueryError(ValidationError):
    """Storage or network failed, or the process was ended from outside: a retry may succeed."""


class WorkerStaleQuery(TransientQueryError):
    """The worker could not write its heartbeat for a lease and stopped the query.

    Recorded with the cause `worker_lost`, the cause the scheduler gives a run whose worker is
    gone: one event, one cause, whoever writes first. A task's query is tried again.
    """


class OutOfMemoryQuery(ValidationError):
    """DuckDB ran out of memory, or the memory watchdog stopped the query."""


class MemoryStopQuery(OutOfMemoryQuery):
    """The memory watchdog stopped the query: Lakana's own wording, with no engine output."""


class DuckDBInternalError(ValidationError):
    """DuckDB or the query process failed on its own, not on the query."""


_KINDS: dict[str, type[ValidationError]] = {
    INVALID: ValidationError,
    TRANSIENT: TransientQueryError,
    OUT_OF_MEMORY: OutOfMemoryQuery,
    INTERNAL: DuckDBInternalError,
}
"""The error type of each kind of error document a child writes (`interrupted` has none)."""


@contextmanager
def cancellation(event: threading.Event) -> Generator[None, None, None]:
    """Stop every query started in this block once `event` is set.

    The event is read when a query starts, from a context variable, so the task runner sets it
    around `run_one` without threading it through the pipeline. The query child is asked to
    stop and, `kill_grace_seconds` later, killed.

    Parameters
    ----------
    event
        Set by the lease keeper on cancel, lost lease or drain.

    Yields
    ------
    None
        Nothing; the block runs with the event installed.
    """
    token = _CANCEL.set(event)
    try:
        yield
    finally:
        _CANCEL.reset(token)


@contextmanager
def cancel_requests(requested: Callable[[], bool]) -> Generator[None, None, None]:
    """Stop every query started in this block once `requested` says so.

    Parameters
    ----------
    requested
        Says whether a cancel request was recorded for the query run; asked about once a
        second while the query runs.

    Yields
    ------
    None
        Nothing; the block runs with the callable installed.
    """
    token = _CANCEL_REQUESTED.set(requested)
    try:
        yield
    finally:
        _CANCEL_REQUESTED.reset(token)


def install_extension_source(source: str) -> None:
    """Tell every query child where DuckDB may get its ``httpfs`` extension from.

    Parameters
    ----------
    source
        ``duckdb`` (DuckDB's own extension server, the default) or ``none`` (never download), as
        `lakana_core.config.parse_extension_source` returns it. A mirror URL is not supported.
    """
    global _EXTENSION_SOURCE
    _EXTENSION_SOURCE = source


def install_drain(event: threading.Event, grace_seconds: float) -> None:
    """Stop running queries once the worker has been draining for `grace_seconds`.

    Parameters
    ----------
    event
        Set when the worker is told to shut down.
    grace_seconds
        How long a running query may go on after that; then it is asked to stop and, after
        `kill_grace_seconds`, killed.

    Notes
    -----
    Only an ad-hoc query is stopped this way. A query that runs for a task run (inside
    `cancellation`) is stopped by the task's lease keeper, which releases the task after the
    same grace; stopping the query first would cancel the task instead of releasing it.
    """
    since: list[float] = []

    def draining() -> bool:
        """Say whether the drain grace is over.

        Returns
        -------
        bool
            Whether the worker is draining and has been for `grace_seconds`.
        """
        if not event.is_set():
            return False
        if not since:
            since.append(time.monotonic())
        return time.monotonic() - since[0] >= grace_seconds

    global _DRAINING
    _DRAINING = draining


def install_heartbeat(stale: Callable[[], bool]) -> None:
    """Stop running queries once the worker's heartbeat is a lease old.

    Parameters
    ----------
    stale
        Says whether the worker could not write its heartbeat for a lease
        (`WorkerHeartbeat.stale`); answered from memory, because the supervisor asks it while it
        reads a result.

    Notes
    -----
    Every query is stopped this way, a task's too (unlike the drain): a worker that cannot
    reach the database can no longer renew the task's lease either, so the task is going to
    be reclaimed.
    """
    global _WORKER_STALE
    _WORKER_STALE = stale


def timeout_error(limits: QueryLimitsConfig, *, writing: bool, killed: bool) -> QueryTimeout:
    """Build the time-limit error.

    Parameters
    ----------
    limits
        Supplies `timeout_seconds` and `kill_grace_seconds`.
    writing
        The run streams a write, so the message says storage time counts.
    killed
        The child did not stop when asked and was killed after the grace period.

    Returns
    -------
    QueryTimeout
        The error, with a hint fitting the kind of run.
    """
    hint = (
        "Writing to storage counts toward the limit, so a slow store can trip it; "
        "reduce the data written, or ask the operator to raise "
        if writing
        else "Aggregate or filter earlier, add a LIMIT, or ask the operator to raise "
    )
    message = (
        f"Query exceeded the time limit ({limits.timeout_seconds:g} s). "
        f"{hint}LAKANA_QUERY_LIMITS__TIMEOUT_SECONDS."
    )
    if killed:
        message += KILLED_SUFFIX.format(grace=limits.kill_grace_seconds)
    return QueryTimeout(message)


def _translate(failed: ChildFailed, limits: QueryLimitsConfig, writing: bool) -> ValidationError:
    """Turn the end of a query child into the error the pipeline records.

    The worker's stop reason wins over whatever the child reported. A child that did not
    stop when asked is named in the worker's log, and in the message where the user asked
    for the stop or set the limit (a time limit, a cancel request). A signal ends a child
    "from outside" only when the supervisor did not send it itself.

    Parameters
    ----------
    failed
        How the child ended.
    limits
        Supplies `timeout_seconds` and `kill_grace_seconds` for the messages.
    writing
        The run streams a write.

    Returns
    -------
    ValidationError
        The error to raise: one of this module's types, or a plain `ValidationError` for an
        error about the query.
    """
    grace = limits.kill_grace_seconds
    if failed.killed and failed.stop is not None and failed.stop is not StopReason.OUT_OF_MEMORY:
        logger.warning(
            "A query did not stop when asked (%s) and was ended after the grace period of "
            "%g s (LAKANA_QUERY_LIMITS__KILL_GRACE_SECONDS).",
            failed.stop.value,
            grace,
        )
    if failed.stop is StopReason.TIMEOUT:
        return timeout_error(limits, writing=writing, killed=failed.killed)
    if failed.stop is StopReason.CANCEL_REQUEST:
        suffix = KILLED_SUFFIX.format(grace=grace) if failed.killed else ""
        return QueryCancelRequested(CANCELLED_MESSAGE + suffix)
    if failed.stop is StopReason.CANCEL_EVENT:
        return QueryCancelled(CANCELLED_MESSAGE)
    if failed.stop is StopReason.DRAIN:
        return QueryCancelled(DRAIN_MESSAGE)
    if failed.stop is StopReason.WORKER_STALE:
        return WorkerStaleQuery(WORKER_STALE_MESSAGE)
    if failed.stop is StopReason.OUT_OF_MEMORY:
        return MemoryStopQuery(
            failed.detail or "Query failed: the worker ran out of memory and stopped the query."
        )
    if failed.error is not None and failed.error.kind in _KINDS:
        message = failed.error.message
        if failed.error.kind == INTERNAL and not message.startswith(STORE_CERTIFICATE_UNVERIFIED):
            message = f"{message} {INTERNAL_ADVICE}"
        return _KINDS[failed.error.kind](message)
    if failed.returncode is None:
        logger.error("The worker could not start a query process: %a", failed.detail)
        return DuckDBInternalError(NOT_STARTED_MESSAGE)
    if failed.returncode < 0 and not failed.own_kill:
        return TransientQueryError(KILLED_FROM_OUTSIDE_MESSAGE.format(signal=-failed.returncode))
    return DuckDBInternalError(BROKEN_MESSAGE.format(status=failed.returncode))


class _Translated(Iterator[pa.RecordBatch]):
    """Iterate a child's batches, raising the pipeline's errors instead of `ChildFailed`.

    After a failure every further read raises the same error again: an iterator that answered
    `StopIteration` once its error was caught would look like a result that ended normally.

    Parameters
    ----------
    batches
        The supervisor's batches; they end normally only for a complete result.
    limits
        As for `_translate`.
    writing
        As for `_translate`.
    """

    def __init__(
        self, batches: Iterator[pa.RecordBatch], limits: QueryLimitsConfig, writing: bool
    ) -> None:
        self._batches = batches
        self._limits = limits
        self._writing = writing
        self._error: ValidationError | None = None

    def __next__(self) -> pa.RecordBatch:
        """Return the next batch.

        Returns
        -------
        pa.RecordBatch
            The next batch.

        Raises
        ------
        StopIteration
            After the last batch of a result the supervisor proved complete.
        ValidationError
            If the child failed, as `_translate` words it; the same error on every later call.
        """
        if self._error is not None:
            raise self._error
        try:
            return next(self._batches)
        except ChildFailed as failed:
            self._error = _translate(failed, self._limits, self._writing)
            raise self._error from failed


def _refuse_a_request_the_child_cannot_read(request: Request) -> None:
    """Refuse a request over the child's limit before a process is started.

    Parameters
    ----------
    request
        The request, with a temp directory of the length the real one will have.

    Raises
    ------
    ValidationError
        If the encoded request is over `MAX_REQUEST_BYTES`. The message names the number of
        data files in it and what to do (or, without a file list, that the statement is
        large); the child's own check stays as the second line.
    TypeError
        If a bound value has a type the child cannot take (`encode_request`).
    """
    size = len(encode_request(request))
    if size > MAX_REQUEST_BYTES:
        files = sum(len(v) for v in request.parameters.values() if isinstance(v, list))
        template = REQUEST_TOO_LARGE_MESSAGE if files else REQUEST_TOO_LARGE_NO_FILES_MESSAGE
        raise ValidationError(
            template.format(files=files, size=-(-size // 2**20), limit=MAX_REQUEST_BYTES // 2**20)
        )


@contextmanager
def _child(
    mode: str,
    sql: str,
    named: dict[str, object] | None,
    *,
    read_prefixes: Sequence[str],
    spill: bool,
    limits: QueryLimitsConfig,
    batch_rows: int,
    writing: bool = False,
    timeout_seconds: float | None = None,
) -> Generator[tuple[pa.Schema, Iterator[pa.RecordBatch]], None, None]:
    """Run one statement in a query child and stream its result.

    Parameters
    ----------
    mode
        The child's mode (`READ`, `WRITE`, `DESCRIBE` or `CHECK`).
    sql
        The rewritten SQL.
    named
        The values bound by name, or `None`.
    read_prefixes
        The locations the statement may open (see `execute_query`).
    spill
        Whether DuckDB may spill to the child's private temp directory.
    limits
        Supplies the limits the child is given and the time limit and grace the worker applies.
    batch_rows
        Rows per Arrow batch.
    writing
        The run streams a write.
    timeout_seconds
        The time limit if it is not `limits.timeout_seconds`.

    Yields
    ------
    tuple of (pa.Schema, Iterator of pa.RecordBatch)
        The result's schema and its batches. The batches end normally only for a result the
        supervisor proved complete.

    Raises
    ------
    ValidationError
        If the child cannot be started or fails before its schema, or a value cannot be sent
        to it.
    TypeError
        If starting the child raises a `TypeError` that is not about a query parameter.
    """

    def make_request(temp_directory: str) -> Request:
        """Build the child's request from its private temp directory.

        Parameters
        ----------
        temp_directory
            The directory the child may spill into.

        Returns
        -------
        Request
            The request.
        """
        return Request(
            mode=mode,
            sql=sql,
            # `encode_request` refuses a value of any other type, by the parameter's name.
            parameters=cast("Mapping[str, ParameterValue]", named or {}),
            read_prefixes=tuple(read_prefixes),
            ca_bundle=query_ca_bundle(),
            spill=spill,
            memory_limit=limits.memory_limit,
            threads=limits.threads,
            temp_directory=temp_directory,
            max_temp_directory_size=limits.max_temp_directory_size,
            batch_rows=batch_rows,
            extension_directory=query_process.extension_directory(),
            parent_pid=os.getpid(),
            extension_source=_EXTENSION_SOURCE,
        )

    cancel_event = _CANCEL.get()
    stops = Stops(
        cancel_event=cancel_event,
        cancel_requested=_CANCEL_REQUESTED.get(),
        # A task's query is released at drain by its lease keeper, not stopped here.
        draining=_DRAINING if cancel_event is None else None,
        worker_stale=_WORKER_STALE,
    )
    with ExitStack() as stack:
        try:
            _refuse_a_request_the_child_cannot_read(
                make_request(str(Path(tempfile.gettempdir(), spill_prefix() + "x" * 8, "tmp")))
            )
            schema, batches = stack.enter_context(
                query_process.run(
                    make_request,
                    timeout_seconds=(
                        limits.timeout_seconds if timeout_seconds is None else timeout_seconds
                    ),
                    kill_grace_seconds=limits.kill_grace_seconds,
                    stops=stops,
                    argv=_ARGV,
                    directory_prefix=spill_prefix(),
                )
            )
        except ChildFailed as failed:
            error = _translate(failed, limits, writing)
            raise error from failed
        except TypeError as exc:
            if str(exc).startswith("Query parameter "):
                raise ValidationError(str(exc)) from exc
            raise
        yield schema, _Translated(batches, limits, writing)


def _named(parameters: Mapping[str, object] | None) -> dict[str, object] | None:
    """Refuse anything but a name-to-value mapping as query parameters.

    Every value the platform passes — each URL list and an incremental write's
    watermark — is set as a named DuckDB variable. A positional sequence is what
    once let a user placeholder reach a URL list, so it is refused here rather than
    handed to DuckDB, whatever a caller's static types said.

    Parameters
    ----------
    parameters
        The values to bind, or `None` for none.

    Returns
    -------
    dict of str to object or None
        A copy of `parameters`, or `None`.

    Raises
    ------
    TypeError
        If `parameters` is neither `None` nor a `Mapping`.
    """
    if parameters is None:
        return None
    if not isinstance(parameters, Mapping):
        raise TypeError("Query parameters must be bound by name, as a mapping.")
    return dict(parameters)


def execute_query(
    sql: str,
    parameters: Mapping[str, object] | None = None,
    *,
    read_prefixes: Sequence[str] = (),
    spill: bool = False,
    limits: QueryLimitsConfig | None = None,
) -> pa.Table:
    """Run one rewritten query in a query child and return its result.

    Parameters
    ----------
    sql
        SQL to execute — already rewritten so that every table
        reference is a `read_parquet`/`read_csv`/`read_json` call over a
        `getvariable('<name>')` variable, never a bare catalog name and never a
        file list spelled out in the SQL text.
    parameters
        Values the query process sets as variables before it runs `sql`, as
        `lakana_core.query_rewrite.RewrittenQuery.parameters` comes. Only a
        mapping is accepted: nothing binds positionally (see `_named`).
        Each resolved file list is set as one LIST value, which is the
        whole point: DuckDB treats a bound string as a filename to look for,
        so a storage object key containing a quote fails as "no files found"
        instead of executing as SQL. Only the types the platform binds can be
        sent to the child (lists of strings, integers, decimals, dates and
        timestamps).
    read_prefixes
        The locations the statement may open, each ending in ``/``: the location of every
        table the query reads, in the form its files' URLs take (the URL prefix of a
        presigned URL, ``http(s)://host[:port]/<path>/``; the absolute directory in local
        ``file://`` mode), as `lakana_core.storage.base.Storage.read_url_prefix` makes it.
        DuckDB runs in allow-list mode: nothing else is opened, besides the child's private
        temp directory and the CA bundle (`lakana_worker.ca_bundle`). Every file a variable
        names must lie under one of them, or the child refuses to run (an `internal` failure).
        The default, none, lets a statement read nothing but what it computes itself.
    spill
        Whether DuckDB may spill to the child's private temp directory, up to
        `limits.max_temp_directory_size`. Only local ``file://`` mode passes `True`;
        otherwise a query over `limits.memory_limit` fails as out of memory.
    limits
        Result and execution bounds, including the wall-clock
        `timeout_seconds` and the `kill_grace_seconds` after which a query that
        did not stop is killed. `None` uses `QueryLimitsConfig()`'s defaults.

    Returns
    -------
    pyarrow.Table
        The query's result, returned only after the child's stream was proven complete.

    Raises
    ------
    ValidationError
        If the query fails for any reason: it fails to parse; it opens a file
        or an address outside `read_prefixes`; a configuration statement of the child
        fails; the SQL produces no result at all (empty or comment-only text);
        `limits.memory_limit` is not a value DuckDB accepts; an execution error
        surfaces while a later batch is fetched; a value cannot be sent to the
        child. DuckDB's own exception classes never reach a caller: the child
        classifies the failure and this module raises the matching subclass.
    ResultTooLarge
        A `ValidationError` subclass: the result exceeds `limits.max_result_rows` or
        `limits.max_result_bytes`. The child is ended at once.
    QueryTimeout
        The query ran past `limits.timeout_seconds`.
    QueryCancelled
        A cancellation, a lost lease or the worker's drain stopped the query.
    TransientQueryError
        Storage or the network failed, or a signal from outside ended the child.
    OutOfMemoryQuery
        DuckDB ran out of memory, or the worker's memory limit stopped the query.
    DuckDBInternalError
        DuckDB failed on its own, or the child ended without a complete result.
    TypeError
        If `parameters` is not a mapping — see `_named`.
    """
    named = _named(parameters)
    limits = limits or QueryLimitsConfig()
    with _child(
        READ,
        sql,
        named,
        read_prefixes=read_prefixes,
        spill=spill,
        limits=limits,
        batch_rows=_FETCH_BATCH_ROWS,
    ) as (schema, batches):
        kept: list[pa.RecordBatch] = []
        rows = 0
        size = 0
        for batch in batches:
            rows += batch.num_rows
            size += batch.nbytes
            if rows > limits.max_result_rows:
                raise ResultTooLarge(
                    f"Query result exceeds max_result_rows ({limits.max_result_rows}). "
                    "Aggregate the result or add a LIMIT."
                )
            if size > limits.max_result_bytes:
                raise ResultTooLarge(
                    f"Query result exceeds max_result_bytes ({limits.max_result_bytes}). "
                    "Aggregate the result or add a LIMIT."
                )
            kept.append(batch)
        return pa.Table.from_batches(kept, schema=schema)


@contextmanager
def stream_query(
    sql: str,
    parameters: Mapping[str, object] | None = None,
    *,
    read_prefixes: Sequence[str] = (),
    spill: bool = False,
    limits: QueryLimitsConfig | None = None,
) -> Generator[tuple[pa.Schema, Iterator[pa.RecordBatch]], None, None]:
    """Run one rewritten query for a write and stream its batches.

    The same child `execute_query` uses, with a UTC session zone, but no result
    caps: a write streams to storage and is bounded by `max_write_rows` in the core writer
    instead. The child lives until the ``with`` block ends.

    The batches end normally only when the child's stream was proven complete; otherwise they
    raise the errors of `execute_query`, on every further read as well. A write commits only
    after they have ended, never when the block is left earlier. The time limit covers the whole
    streaming phase, storage time included, because the clock runs while the caller writes
    between batches; it is noticed at the next read, the one that ends the stream included.
    Once the stream has ended normally, writing the last file and the commit are not interrupted.

    Parameters
    ----------
    sql
        Rewritten SQL, as for `execute_query`.
    parameters
        Values set as variables for `sql`, as for `execute_query`: the
        rewriter's URL lists plus, for an incremental append, the watermark.
    read_prefixes
        As for `execute_query`.
    spill
        As for `execute_query`.
    limits
        Supplies `memory_limit`, `threads`, the spill bounds, `timeout_seconds` and
        `kill_grace_seconds`. `None` uses the defaults.

    Yields
    ------
    tuple of (pa.Schema, Iterator of pa.RecordBatch)
        The result's schema, known before any batch is read, and the batches.

    Raises
    ------
    ValidationError
        If starting the query fails, or the SQL produces no result. A failure while iterating
        the batches is raised by the iterator, also as `ValidationError` (a subclass, see
        `execute_query`).
    TypeError
        If `parameters` is not a mapping — see `_named`.
    """
    named = _named(parameters)
    limits = limits or QueryLimitsConfig()
    with _child(
        WRITE,
        sql,
        named,
        read_prefixes=read_prefixes,
        spill=spill,
        limits=limits,
        batch_rows=_WRITE_FETCH_BATCH_ROWS,
        writing=True,
    ) as (schema, batches):
        yield schema, batches


def describe_query(
    sql: str,
    parameters: Mapping[str, object] | None = None,
    *,
    read_prefixes: Sequence[str] = (),
    spill: bool = False,
    limits: QueryLimitsConfig | None = None,
) -> pa.Schema:
    """Plan one rewritten query in a query child and return its result schema without rows.

    Used to find a view's output columns and to diagnose a view
    that no longer plans. Runs `SELECT * FROM (<sql>) LIMIT 0`
    in a UTC-zoned child, as a write does; `sql` is the rewriter's regenerated
    output, never raw user text. The schema is returned only after the child's
    stream was proven complete.

    Parameters
    ----------
    sql
        Rewritten SQL.
    parameters
        Values set as variables for it, as for `execute_query`.
    read_prefixes
        As for `execute_query`.
    spill
        As for `execute_query`.
    limits
        Supplies `memory_limit`, `threads`, the spill bounds, `timeout_seconds` and
        `kill_grace_seconds`. `None` uses the defaults.

    Returns
    -------
    pa.Schema
        The result's schema.

    Raises
    ------
    ValidationError
        If the query does not plan, or its child fails (see `execute_query`).
    TypeError
        If `parameters` is not a mapping — see `_named`.
    """
    named = _named(parameters)
    limits = limits or QueryLimitsConfig()
    described = f"SELECT * FROM ({sql}) AS __lakana_described LIMIT 0"  # nosec B608
    with _child(
        DESCRIBE,
        described,
        named,
        read_prefixes=read_prefixes,
        spill=spill,
        limits=limits,
        batch_rows=_FETCH_BATCH_ROWS,
    ) as (schema, batches):
        for _batch in batches:
            pass
        return schema


def check_limits(limits: QueryLimitsConfig) -> None:
    """Check at start-up that a query child starts and that DuckDB accepts the sizes.

    Runs one child in mode ``check``, which only sets the two sizes and runs no query. Without
    this a malformed ``LAKANA_QUERY_LIMITS__MEMORY_LIMIT`` would fail every query at run time,
    and so would a worker whose Python environment cannot start a child.

    Parameters
    ----------
    limits
        The query limits whose sizes are checked (`memory_limit`, `max_temp_directory_size`).

    Raises
    ------
    ValueError
        If `httpfs` cannot be loaded under `LAKANA_WORKER__EXTENSION_SOURCE` (the message of
        the query that would fail), if DuckDB cannot read one of the sizes (the message names
        the environment variable, cuts
        and escapes the value it quotes, and shows two valid forms), or if no query child can be
        started or none answers (the message says the worker cannot run queries and what to
        check).
    """
    try:
        with _child(
            CHECK,
            "",
            None,
            read_prefixes=(),
            spill=False,
            limits=limits,
            batch_rows=_FETCH_BATCH_ROWS,
            timeout_seconds=_CHECK_TIMEOUT_SECONDS,
        ) as (_schema, batches):
            list(batches)  # an empty stream; reading it to its end is the check
    except QueryTimeout as exc:
        raise ValueError(CHECK_TIMEOUT_MESSAGE.format(seconds=_CHECK_TIMEOUT_SECONDS)) from exc
    except ValidationError as exc:
        field = str(exc)
        if field in ("memory_limit", "max_temp_directory_size"):
            raise ValueError(
                f"LAKANA_QUERY_LIMITS__{field.upper()} is not a size DuckDB accepts: "
                f"{ascii(getattr(limits, field))[:60]}. Write a number and a unit, such as 1GB "
                "or 512MiB."
            ) from exc
        if "LAKANA_WORKER__EXTENSION_SOURCE" in field:
            # The reader of a start-up refusal is the operator, not a user whose query failed.
            raise ValueError(
                field.removeprefix("Query failed: ").replace("Tell the operator: ", "To fix it: ")
            ) from exc
        raise ValueError(
            "The worker cannot run queries: its start-up check in a query process failed "
            f"({exc}). Check that the worker's Python environment is complete (the duckdb and "
            "pyarrow packages) and that its temp directory is writable."
        ) from exc
