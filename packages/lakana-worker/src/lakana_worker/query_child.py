"""One query in a process of its own: ``python -I -B -m lakana_worker.query_child``.

The worker starts this module once per query, with its own interpreter (``-I``: the working
directory and the environment's ``PYTHON*`` variables are ignored; ``-B``: no bytecode files, which
``PYTHONDONTWRITEBYTECODE`` would not achieve under ``-I``), in a new session, with an environment
built from scratch and a private ``HOME`` and ``TMPDIR``. The module reads one
request from standard input (`lakana_worker.query_protocol.Request`), opens one in-memory DuckDB
connection, configures it, runs the one statement and writes the result to standard output as an
Arrow IPC stream. It holds no credential: the statement reads presigned URLs.

**Allow-list mode.** DuckDB opens only what the request names: the locations of the tables the
query reads (`read_prefixes`: a directory in local mode, a URL prefix otherwise), the query's
private temp directory (DuckDB allows its own ``temp_directory``) and the CA bundle. Anything
else, another host, another scheme, a sibling directory, a local file, a symlink out of a table
directory, is refused while the statement is planned. DuckDB compares URLs as strings and sends a
``..`` segment to the server unchanged, so before anything runs the child checks every file the
variables name against the prefixes (`file_outside_prefixes`) and refuses the request when one
lies outside.

**One SELECT, checked by DuckDB's parser.** The lock holds settings, not secrets: ``CREATE SECRET
(TYPE http, HTTP_PROXY ...)`` or ``EXTRA_HTTP_HEADERS`` would still route the reads, presigned URLs
included, through a proxy of the statement's choosing. So before anything is configured the child
splits the text with `duckdb.DuckDBPyConnection.extract_statements` and runs it only if that is
exactly one ``SELECT`` (`NOT_ONE_SELECT` otherwise, an ``internal`` failure). DuckDB 2 reports a
write inside a CTE (``WITH x AS (COPY ... TO ...) SELECT 1``, or ``INSERT``, ``UPDATE`` and
``DELETE ... RETURNING``) as one ``SELECT``, at any depth, so the child then asks DuckDB for the
statement's parse tree (`SERIALISE`, still on the unconfigured connection) and runs it only if
every query node in it reads (`reads_only`, `READING_QUERY_NODES`; `WRITES_IN_SELECT` otherwise,
also when the tree cannot be had). The same check refuses a call of a function that runs SQL
given as a value (`SQL_RUNNING_FUNCTIONS`, e.g. ``json_execute_serialized_sql``), whose SQL the
tree shows only as text. Unchecked, such a ``COPY`` wrote into the query's temp
directory, which ``max_temp_directory_size`` does not bound, or in local mode into a table
directory. The guard refuses all of these first; this is the second layer.

Why each configuration statement is there, and why their order matters:

0. The connection is opened with ``threads`` and ``async_threads`` as options
   (`connection_config`). DuckDB 2 starts a pool of I/O threads with every connection, one per
   core by default (80 on a 20-core machine); the child asks for `ASYNC_THREADS_PER_THREAD`
   per processor thread instead. Measured on remote reads with 5 ms per request, 4 threads:
   four per thread was within 10 % of the default on every query, two per thread was not.
   ``async_threads`` can only be given here: ``SET async_threads`` is refused once external
   access is off.
1. ``extension_directories`` comes first. The child's ``HOME`` is empty, and DuckDB looks for
   installed extensions under ``HOME``; without the worker's directory ``INSTALL httpfs`` would
   download the extension for every query, and fail without network access. DuckDB 2 takes a
   list here (a string fails the cast to ``VARCHAR[]``).
2. ``allow_community_extensions = false`` and ``allow_extension_repositories = 'forbidden'``
   (``undecided``, the default, may still change to ``allowed`` at run time; ``forbidden`` may
   not, and refuses ``CREATE EXTENSION REPOSITORY``); then ``INSTALL``/``LOAD httpfs``, which
   need the extension directory, before external access is switched off.
3. ``s3_access_key_id``, ``s3_secret_access_key`` and ``s3_session_token`` are cleared.
   `duckdb.connect()` fills them from ``AWS_*`` environment variables. The child's environment
   has none, and the statements stay as a second layer.
4. ``allow_persistent_secrets = false`` keeps DuckDB's disk-backed secret store out (the secret
   manager would read local disk on the first httpfs read);
   ``autoinstall_known_extensions`` and ``autoload_known_extensions`` are off, so that no
   function loads an extension the module did not load on purpose.
5. ``ca_cert_file`` is the request's bundle. DuckDB 2 reads the bundle lazily, through the local
   file system, on the first HTTPS request; it must be named and allowed, or every HTTPS read
   fails with ``Cannot read a CA certificate bundle``. Then
   ``enable_server_cert_verification = true``: DuckDB (1.5.5 and 2.0.0 alike) verifies the
   store's certificate whatever it says, but its default reads as off, and the behaviour must
   not rest on that. A certificate the bundle cannot verify fails the query as ``internal``, not
   retried, with a message naming the store's host and port, never the presigned URL.
6. ``temp_directory`` before external access is switched off: afterwards DuckDB refuses to
   change it (``Modifying the temp_directory has been disabled``).
7. ``allowed_paths`` (the bundle) and ``allowed_directories`` (the prefixes), before external
   access is switched off: afterwards DuckDB refuses to change them.
8. ``enable_external_access = false``: from here on DuckDB opens only the allowed paths and
   directories, and the temp directory.
9. The limits: ``memory_limit``, ``threads``, ``max_temp_directory_size`` (``0KB`` unless the
   request allows spilling, which only local mode does: a query over ``memory_limit`` then fails
   with DuckDB's ``Out of Memory Error``) and, for a mode other than ``read``, ``TimeZone``. All
   are still settable after step 8.
10. ``lock_configuration = true`` last: no ``SET``, ``RESET`` or ``PRAGMA`` of any setting
    succeeds after it (``SET VARIABLE`` still does). A second layer behind the guard, which
    refuses ``SET`` itself, against ``http_proxy`` (a proxy would receive the presigned URLs),
    ``extra_http_headers`` and ``ca_cert_file`` as settings (not as a secret: see above), and
    against ``lambda_syntax`` and ``error_on_division_by_zero``. ``http_proxy`` defaults to the
    ``HTTP_PROXY`` environment variable on DuckDB 2; the child's environment has none.

**Values are variables, not parameters.** Every value the statement reads (a file list, a
field-id map, a watermark) is set with ``SET VARIABLE "<name>" = ?`` before the statement, and
the statement runs with no parameters through ``connection.sql(sql).to_arrow_reader(n)``. With
parameters, ``sql()`` holds the whole result in memory before the first batch, and DuckDB 2
removed the Arrow export from ``execute()`` results.

**Being asked to stop.** The statement runs in a second thread. The main thread stays in Python,
so the handler of ``SIGTERM`` runs at once; it sets a flag, and the main thread then calls
`connection.interrupt()` ten times a second until the statement's thread ends. DuckDB clears the
interrupt flag when a statement starts, hence the repetition. With the statement on the main
thread the handler would not run while pyarrow waits for the next batch (measured: never within
60 s); with this arrangement a statement stopped 10 to 20 ms after the signal. The ask reaches
a child that is working in DuckDB or between two batches. A child blocked in a write to a pipe
nobody reads, or in reading a standard input nobody closed, does not react to it until the
blocking call returns: the worker reads the pipe, closes the input, or kills the group.

**Standard output is the result and nothing else.** Before anything runs, descriptor 1 is
duplicated for the Arrow stream and then pointed at standard error, so whatever DuckDB, an
extension or a stray ``print`` writes to "standard output" cannot corrupt the stream.

**The worker's death.** On Linux ``PR_SET_PDEATHSIG`` kills the child when the worker's thread
that started it ends. macOS has no such request: a child there can outlive a worker that is
killed, until it writes to the closed pipe or finishes.

A process boundary, not a sandbox: the child runs as the worker's user and sees its file system.
"""

from __future__ import annotations

import contextlib
import ctypes
import functools
import json
import os
import re
import signal
import sys
import threading
import traceback
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from typing import BinaryIO, NoReturn, TextIO

import duckdb
import pyarrow as pa

from lakana_worker.own_peak import own_peak_bytes
from lakana_worker.query_protocol import (
    CHECK,
    EXIT_FAILED,
    EXIT_OK,
    EXIT_REFUSED,
    EXTENSION_SOURCE_DUCKDB,
    EXTENSION_SOURCE_NONE,
    INTERNAL,
    INTERRUPTED,
    INVALID,
    MAX_REQUEST_BYTES,
    MEMORY_LIMIT_ADVICE,
    NO_RESULT_MESSAGE,
    OUT_OF_MEMORY,
    READ,
    STORE_CERTIFICATE_UNVERIFIED,
    TRANSIENT,
    UNCONVERTED_VALUE,
    USAGE_FILE,
    ErrorDocument,
    ParameterValue,
    Request,
    decode_request,
    encode_error,
)

PROFILE_METRIC = "io.total_bytes_read"
"""The one metric DuckDB tracks while profiling: the bytes a statement read."""
PROFILE_FILE = "profile.json"
"""Where DuckDB's profiler writes, inside the child's temp directory; it is never read."""

_DUCKDB_LINE_MARKER = re.compile(r"^LINE \d+:")
"""Matches DuckDB's own echo of the offending line, e.g. ``LINE 1: SELECT ...``."""

_DUCKDB_CARET_MARKER = re.compile(r"^\s*\^+\s*$")
"""Matches the caret-only line DuckDB prints under a ``LINE n:`` echo."""

_DUCKDB_ERROR_TYPE = re.compile(r"^([A-Z][A-Za-z]*(?: [A-Za-z]+)*) Error:")
"""Matches the error-type prefix DuckDB puts on its messages, e.g. ``Binder Error:``."""

_TRANSIENT_ERROR_TYPES = frozenset({"IO", "HTTP"})
_OUT_OF_MEMORY_ERROR_TYPES = frozenset({"Out of Memory"})
_INTERNAL_ERROR_TYPES = frozenset({"INTERNAL", "FATAL"})
_UNVERIFIED_CERTIFICATE = re.compile(
    r"^IO Error: SSL peer certificate or SSH remote key was not OK\b"
    r"(?:.*?'(?P<url>https://[^'\s]+)')?"
)
"""DuckDB's report of a server certificate it cannot verify against ``ca_cert_file``: the same
text on DuckDB 1.5.5 and 2.0.0, as a `duckdb.IOException` or, from a later batch, an `OSError`. It
quotes the URL it asked for, a presigned one; only its host and port are ever used."""
_CA_REFUSAL = "Cannot read a CA certificate bundle"
"""DuckDB 2's refusal to load the CA bundle under the restriction. It should not occur once
``ca_cert_file`` and ``allowed_paths`` name the bundle; pinned so that a DuckDB change shows."""

REFUSED_LOCATION = (
    "Query failed: the query tried to open a file or an address that is not one of the tables "
    "it reads."
)
"""What a child says of DuckDB's refusal to open a path or URL outside the allowed ones. DuckDB's
own text quotes the path, so it is never passed on."""

OUTSIDE_PREFIXES = (
    "Query failed: a file of the query lies outside the locations it was given. This is a "
    "defect of Lakana; please report it."
)
"""What a child says, without running anything, when a file of a variable lies outside the
request's prefixes, or a prefix has a shape the child does not accept."""

NOT_ONE_SELECT = (
    "Query failed: the query process was given something other than one SELECT statement. "
    "This is a defect of Lakana; please report it."
)
"""What a child says, without running anything, of SQL that DuckDB's parser does not read as
exactly one ``SELECT``. Every mode sends one (a describe wraps it in ``SELECT * FROM (...)
LIMIT 0``); anything else at the top level (a second statement, ``CREATE SECRET``, ``SET``,
``PRAGMA``, ``ATTACH``, ``COPY``, ``INSERT``, ``MERGE``, ``LOAD``, ``CALL``) is refused here as
the layer behind the guard. A write nested inside a ``SELECT`` passes this check on DuckDB 2 and
is refused by `WRITES_IN_SELECT`'s check."""

WRITES_IN_SELECT = (
    "Query failed: the query process was given a SELECT that may write (a COPY, INSERT, UPDATE, "
    "DELETE or MERGE inside it, or a call of a function that runs SQL given as a value), or one "
    "whose parse tree it could not check. This is a defect of Lakana; please report it."
)
"""What a child says, without running anything, of one ``SELECT`` whose parse tree is not made of
reading query nodes only, or that calls a function in `SQL_RUNNING_FUNCTIONS` (`reads_only`).
DuckDB 2's parser reports ``WITH x AS (COPY ... TO ...) SELECT 1`` as one ``SELECT``, and the same
holds for ``INSERT``, ``UPDATE`` and ``DELETE ... RETURNING`` in a CTE, at any depth: in a
subquery, a ``UNION`` branch, ``EXISTS`` or a lambda. The guard refuses all of these first."""

SERIALISE = "SELECT json_serialize_sql(?)"
"""How the child asks DuckDB's own parser for the statement's tree, as JSON. Nothing runs: the
function parses its argument and returns the tree; it fails, or reports ``"error": true``, for
anything it cannot serialise."""

READING_QUERY_NODES = frozenset(
    {"SELECT_NODE", "SET_OPERATION_NODE", "RECURSIVE_CTE_NODE", "CTE_NODE"}
)
"""The query node types a statement the child runs may contain. Every other node type is refused,
the writing ones (``COPY_QUERY_NODE``, ``INSERT_QUERY_NODE``, ``UPDATE_QUERY_NODE``,
``DELETE_QUERY_NODE`` on DuckDB 2.0.0) and any a later DuckDB adds."""

SQL_RUNNING_FUNCTIONS = frozenset(
    {
        "json_execute_serialized_sql",
        "query",
        "query_table",
        "json_serialize_plan",
        "postgres_query",
        "postgres_execute",
        "mysql_query",
        "mysql_execute",
    }
)
"""Functions that run, or bind and plan, SQL given as a value: SQL the parse tree shows only as a
text or a serialised statement, so `READING_QUERY_NODES` cannot judge it. A call of any of them,
in ``FROM`` or as an expression, by any qualified or quoted spelling or as a method, is refused
(the serialised name is lower-cased and compared without its schema or catalog).
``SELECT * FROM json_execute_serialized_sql(json_serialize_sql('WITH x AS (COPY ... TO ...)
SELECT 1'))`` ran the inner ``COPY`` on 2.0.0 and wrote the file.

Derived like the guard's blacklist (`lakana_core.query_rewrite`, which refuses all of these
first): every function of DuckDB 2.0.0's ``duckdb_functions()`` whose name or description matches
``sql``, ``query``, ``exec``, ``serial``, ``pragma``, ``eval``, ``parse``, ``prepare``,
``statement`` or ``plan``, read one by one. ``json_execute_serialized_sql`` (table and pragma),
``query`` and ``query_table`` execute; ``json_serialize_plan`` binds and plans (it does not write,
but binding opens what the SQL names); ``postgres_*``/``mysql_*`` belong to extensions the child
never loads (autoloading is off) and are listed should one ever be loaded. Left out:
``json_serialize_sql`` and ``json_deserialize_sql`` only turn text into a tree and back;
``aggregate``/``list_aggregate`` call an aggregate by name, not SQL; the ``pragma_*`` table
functions read settings and metadata, which the guard governs."""

NO_SPILL = (
    f"Query failed: the query ran out of memory and cannot spill to disk. {MEMORY_LIMIT_ADVICE}"
)
"""What a child says of running out of memory when the request does not allow spilling."""

NETWORK_SETTINGS = "https://lakana-labs.github.io/lakana/reference/network-settings/"
"""The page that says how to trust a private certificate authority."""

STORE_CERTIFICATES = f"{NETWORK_SETTINGS}#object-store-certificates-for-queries"
"""The section that says which certificate authorities a query process trusts."""

_REMOTE_PREFIX = re.compile(
    r"https?://(?:[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?|\[[0-9A-Fa-f:.]+\])(?::[0-9]{1,5})?"
    r"/[^?#\s\\]+/"
)
"""A remote prefix: ``http(s)://host[:port]/`` and a non-empty path ending in ``/``. No user
information, query, fragment, white space or backslash."""

_DOT_ESCAPE = re.compile(r"%2e", re.IGNORECASE)
"""A percent-escaped dot, which a server may decode into a ``..`` segment."""

ASYNC_THREADS_PER_THREAD = 4
"""DuckDB I/O threads per processor thread of a query (``async_threads``); see the module's
docstring, step 0, for the measurement."""

_DIVISION_ADVICE = re.compile(
    r", or SET error_on_division_by_zero=false to return NULL for all divisions by zero\."
)
"""The part of DuckDB 2's division-by-zero message that suggests a setting. A query cannot run
``SET`` (the guard refuses it, and the lock after it), so the message keeps only ``TRY(...)``."""

_INTERRUPT_SECONDS = 0.1
"""How often the main thread interrupts the connection once it was asked to stop."""

_ASKED = "The query stopped because it was asked to."
_LOST_CAUSE = (
    "The statement failed, but DuckDB reported an interruption instead of the cause. "
    "Run it again to see the error."
)
"""With several threads DuckDB can cancel a sibling task of a failing statement and report that
interruption in place of the statement's own error (about 1 failure in 2,000 at 4 threads in
DuckDB 1.5.5; not reproduced locally on 2.0.0, kept until a CI run shows it gone). Nothing here
interrupted the connection, so the cause is lost."""
_SINK_CLOSED = "The worker stopped reading the result."
_PR_SET_PDEATHSIG = 1
_VARIABLE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
"""A name the child sets a variable under. DuckDB binds no name in `SET VARIABLE`, so the name
is written into the statement, double-quoted, and must be an identifier."""

_READ_CAST = re.compile(
    r'failed to cast column "(?P<column>[^"]*)" from type (?P<stored>.+?) to (?P<wanted>.+?): :'
)
"""DuckDB's report of a column a file stores in a type the read cannot convert to."""


def strip_duckdb_context(message: str) -> str:
    """Remove DuckDB's `LINE n:`/caret echo of the query text from an error message.

    DuckDB's own binder/parser errors quote the offending SQL back verbatim
    under a `LINE n:` marker, followed by a caret-marker line pointing at
    the token — and the SQL this module executes is already-rewritten,
    catalog-generated SQL (`lakana_core.query_rewrite.rewrite_query`),
    which can embed a row filter's or column mask's own restricted-subset
    expression directly in the CTE it builds. Neither must ever reach a
    caller: this truncates the message at the first such line, since
    everything from there on is DuckDB echoing text this module itself
    supplied, never new diagnostic information.

    Parameters
    ----------
    message
        The exception text as `str(exc)` produced it.

    Returns
    -------
    str
        `message` with the first `LINE n:` line (or caret-marker line, if
        that comes first) and everything after it removed. Returned
        unchanged if neither marker appears.
    """
    lines = message.splitlines()
    for index, line in enumerate(lines):
        if _DUCKDB_LINE_MARKER.match(line) or _DUCKDB_CARET_MARKER.match(line):
            return "\n".join(lines[:index]).rstrip()
    return message


def _is_transient(exc: BaseException) -> bool:
    """Tell a retryable I/O failure from a permanent SQL failure.

    DuckDB raises `IOException` for storage and network trouble, but an
    error surfacing while a later batch is fetched arrives as a plain
    `OSError` whatever its cause — a failed `CAST` as much as a dropped
    connection. Those are told apart by the error-type prefix DuckDB puts on
    its message: any type but `IO`/`HTTP` is a SQL error that a retry would
    only repeat.

    Parameters
    ----------
    exc
        The `duckdb.Error` or `OSError` that ended the query.

    Returns
    -------
    bool
        Whether a retry may succeed.
    """
    if isinstance(exc, duckdb.IOException):
        return True
    if not isinstance(exc, OSError):
        return False
    matched = _DUCKDB_ERROR_TYPE.match(str(exc))
    return matched is None or matched.group(1) in _TRANSIENT_ERROR_TYPES


def _error_type(exc: BaseException) -> str | None:
    """Read the error type DuckDB put on a plain `OSError`'s message.

    Parameters
    ----------
    exc
        The exception.

    Returns
    -------
    str or None
        The prefix (``Out of Memory``, ``INTERNAL``, ...) of an `OSError`
        carrying one; `None` for any other exception, whose class says it.
    """
    if not isinstance(exc, OSError):
        return None
    matched = _DUCKDB_ERROR_TYPE.match(str(exc))
    return None if matched is None else matched.group(1)


def _is_interrupt(exc: BaseException) -> bool:
    """Tell DuckDB's interrupt error from any other.

    Parameters
    ----------
    exc
        The `duckdb.Error` or `OSError` that ended the query.

    Returns
    -------
    bool
        Whether DuckDB reports an interruption (``INTERRUPT Error``).
    """
    return isinstance(exc, duckdb.InterruptException) or _error_type(exc) == "INTERRUPT"


def _is_out_of_memory(exc: BaseException) -> bool:
    """Tell an out-of-memory failure from any other DuckDB error.

    Parameters
    ----------
    exc
        The `duckdb.Error` or `OSError` that ended the query.

    Returns
    -------
    bool
        Whether DuckDB ran out of memory.
    """
    return isinstance(exc, duckdb.OutOfMemoryException) or (
        _error_type(exc) in _OUT_OF_MEMORY_ERROR_TYPES
    )


def _is_permission(exc: BaseException) -> bool:
    """Tell DuckDB's refusal to open a path or URL from any other error.

    Parameters
    ----------
    exc
        The `duckdb.Error` or `OSError` that ended the query.

    Returns
    -------
    bool
        Whether DuckDB reports a ``Permission Error``, by the class or by the message's prefix.
    """
    return isinstance(exc, duckdb.PermissionException) or _error_type(exc) == "Permission"


def _is_internal(exc: BaseException) -> bool:
    """Tell a DuckDB internal or fatal error from an error about the query.

    Parameters
    ----------
    exc
        The `duckdb.Error` or `OSError` that ended the query.

    Returns
    -------
    bool
        Whether DuckDB failed on its own bug or state, not on the SQL.
    """
    return isinstance(exc, (duckdb.InternalException, duckdb.FatalException)) or (
        _error_type(exc) in _INTERNAL_ERROR_TYPES
    )


def _has_dot_segment(path: str, separators: str) -> bool:
    """Tell whether a path holds a ``.`` or ``..`` segment.

    Parameters
    ----------
    path
        The path, or the part of a URL after its prefix.
    separators
        The characters that separate segments.

    Returns
    -------
    bool
        Whether a segment is ``.`` or ``..``.
    """
    return any(segment in (".", "..") for segment in re.split(f"[{re.escape(separators)}]", path))


def valid_prefix(prefix: str) -> bool:
    """Tell whether a prefix has a shape the child gives DuckDB as an allowed location.

    Parameters
    ----------
    prefix
        One of the request's `read_prefixes`.

    Returns
    -------
    bool
        For a remote prefix: ``http(s)://host[:port]/`` and a non-empty path ending in ``/``
        whose segments are neither empty nor ``.``/``..`` and that holds no ``%2e``. For a local
        one: absolute, ending in ``/``, `os.path.normpath`-canonical before that slash, and not
        ``/`` itself. Nothing else (no NUL byte either).
    """
    if "\x00" in prefix:
        return False
    if prefix.startswith(("http://", "https://")):
        if _REMOTE_PREFIX.fullmatch(prefix) is None:
            return False
        path = prefix.split("/", 3)[3][:-1]
        return not _DOT_ESCAPE.search(path) and all(
            segment not in ("", ".", "..") for segment in path.split("/")
        )
    directory = prefix[:-1]
    return (
        prefix.startswith("/")
        and not prefix.startswith("//")
        and prefix.endswith("/")
        and directory != ""
        and os.path.normpath(directory) == directory
    )


def _inside(url: str, prefix: str) -> bool:
    """Tell whether a file lies under one prefix, without a way out of it.

    Parameters
    ----------
    url
        A file the statement reads: a presigned URL, or a local path.
    prefix
        A prefix `valid_prefix` accepted.

    Returns
    -------
    bool
        Whether `url` starts with `prefix` and the rest (up to ``?`` for a URL) holds no
        ``.``/``..`` segment and no ``%2e``. A URL's rest is also checked once percent-decoded,
        split at ``/`` and at a backslash, as a server might read it.
    """
    if "\x00" in url or not url.startswith(prefix):
        return False
    rest = url[len(prefix) :]
    if not prefix.startswith(("http://", "https://")):
        return not _DOT_ESCAPE.search(rest) and not _has_dot_segment(rest, "/")
    rest = rest.split("?", 1)[0]
    return not (
        _DOT_ESCAPE.search(rest)
        or _has_dot_segment(rest, "/")
        or _has_dot_segment(urllib.parse.unquote(rest), "/\\")
    )


def file_outside_prefixes(request: Request) -> bool:
    """Tell whether the request must be refused before anything runs (fail closed).

    DuckDB checks only what it opens, and sends a URL's ``..`` to the server unchanged
    (``.../t1/../t2/a.parquet`` passes an allowed ``.../t1/``), so the child checks the files
    itself.

    Parameters
    ----------
    request
        The request: its prefixes and the file lists among its variables.

    Returns
    -------
    bool
        Whether a prefix has a shape `valid_prefix` refuses, the CA bundle is not an absolute
        path, or a string of a list-valued variable does not lie inside one of the prefixes.
    """
    if not all(valid_prefix(prefix) for prefix in request.read_prefixes):
        return True
    if not os.path.isabs(request.ca_bundle):
        return True
    return any(
        not any(_inside(url, prefix) for prefix in request.read_prefixes)
        for value in request.parameters.values()
        if isinstance(value, list)
        for url in value
    )


ConnectionConfig = dict[str, str | bool | int | float | list[str]]
"""The options `duckdb.connect` takes as ``config``."""


def connection_config(request: Request) -> ConnectionConfig:
    """Give the options the connection is opened with.

    Parameters
    ----------
    request
        Supplies ``threads``.

    Returns
    -------
    dict
        ``threads`` and ``async_threads`` (`ASYNC_THREADS_PER_THREAD` per thread), for
        `duckdb.connect`'s ``config``.
    """
    return {
        "threads": request.threads,
        "async_threads": ASYNC_THREADS_PER_THREAD * request.threads,
    }


def profiling_statements(output: str) -> list[str]:
    """Give the statements that switch profiling on, in the order `configure` runs them.

    Parameters
    ----------
    output
        The file DuckDB's profiler writes to.

    Returns
    -------
    list of str
        The statements. ``tracked_metrics`` needs profiling on first, and without an output file
        DuckDB 2 prints every statement's profile to standard error.
    """
    return [
        "PRAGMA enable_profiling = 'json'",
        "SET profiling_output = '" + output.replace("'", "''") + "'",
        f"SET tracked_metrics = ['{PROFILE_METRIC}']",
    ]


class ExtensionUnavailable(Exception):
    """DuckDB's ``httpfs`` extension is neither installed nor allowed to be downloaded."""


def load_httpfs(connection: duckdb.DuckDBPyConnection, request: Request) -> None:
    """Load ``httpfs``, installing it first where the request's extension source allows.

    With the default source the extension is installed from DuckDB's own server (a no-op when
    it is in the extension directory). With ``none`` or a mirror it is loaded from the
    directory first, so an extension the container image bundled keeps working whatever the
    setting says; only a missing one is installed, from the mirror (``INSTALL ... FROM`` applies
    to this install alone and works under ``allow_extension_repositories = 'forbidden'``).

    Parameters
    ----------
    connection
        A fresh in-memory connection with its extension directory set.
    request
        Supplies ``extension_source`` and ``extension_directory``.

    Raises
    ------
    ExtensionUnavailable
        If the extension is missing and the source is ``none`` or its mirror does not have it.
        The message names the extension, the directory and the next step.
    """
    source = request.extension_source
    if source == EXTENSION_SOURCE_DUCKDB:
        connection.execute("INSTALL httpfs")
        connection.execute("LOAD httpfs")
        return
    try:
        connection.execute("LOAD httpfs")
        return
    except duckdb.Error:
        pass
    where = f"the extension directory {request.extension_directory}"
    if source == EXTENSION_SOURCE_NONE:
        raise ExtensionUnavailable(
            f"Query failed: the worker cannot load DuckDB's httpfs extension: it is not in "
            f"{where} and LAKANA_WORKER__EXTENSION_SOURCE=none forbids downloading it. Tell the "
            "operator: run the worker from the Lakana container image, which bundles it, or "
            "copy httpfs.duckdb_extension into that directory, or set "
            "LAKANA_WORKER__EXTENSION_SOURCE to an internal https mirror."
        )
    try:
        connection.execute(f"INSTALL httpfs FROM '{source}'")
        connection.execute("LOAD httpfs")
    except duckdb.Error as exc:
        raise ExtensionUnavailable(
            f"Query failed: the worker cannot load DuckDB's httpfs extension: it is not in "
            f"{where} and the mirror LAKANA_WORKER__EXTENSION_SOURCE={source} did not provide it. "
            "Tell the operator: check that the mirror is reachable from the worker and holds "
            "httpfs for this DuckDB version, or run the worker from the Lakana container "
            "image, which bundles it."
        ) from exc


def configure(connection: duckdb.DuckDBPyConnection, request: Request) -> None:
    """Apply the isolation settings and the limits, in their required order, then lock them.

    See the module's docstring for why each statement is there.

    Parameters
    ----------
    connection
        A fresh in-memory connection.
    request
        Supplies the extension directory, the CA bundle, the prefixes, the temp directory, the
        limits and the mode: a mode other than ``read`` pins the session time zone to UTC, so a
        ``TIMESTAMPTZ`` exports as a type a table can store.
    """
    connection.execute("SET extension_directories = ?", [[request.extension_directory]])
    connection.execute("SET allow_community_extensions = false")
    connection.execute("SET allow_extension_repositories = 'forbidden'")
    load_httpfs(connection, request)
    # The order matters: `tracked_metrics` needs profiling on, and without an output file DuckDB 2
    # prints the profile of every statement to standard error, where the error document goes.
    for statement in profiling_statements(os.path.join(request.temp_directory, PROFILE_FILE)):
        connection.execute(statement)
    for credential in ("s3_access_key_id", "s3_secret_access_key", "s3_session_token"):
        connection.execute(f"SET {credential} = ''")
    connection.execute("SET allow_persistent_secrets = false")
    connection.execute("SET autoinstall_known_extensions = false")
    connection.execute("SET autoload_known_extensions = false")
    connection.execute("SET ca_cert_file = ?", [request.ca_bundle])
    connection.execute("SET enable_server_cert_verification = true")
    connection.execute("SET temp_directory = ?", [request.temp_directory])
    connection.execute("SET allowed_paths = ?", [[request.ca_bundle]])
    connection.execute("SET allowed_directories = ?", [list(request.read_prefixes)])
    connection.execute("SET enable_external_access = false")
    connection.execute("SET memory_limit = ?", [request.memory_limit])
    connection.execute("SET threads = ?", [request.threads])
    connection.execute(
        "SET max_temp_directory_size = ?",
        [request.max_temp_directory_size if request.spill else "0KB"],
    )
    if request.mode != READ:
        connection.execute("SET TimeZone = 'UTC'")
    connection.execute("SET lock_configuration = true")


def _store_host(text: str, prefixes: Sequence[str]) -> str:
    """Name the store whose certificate failed, by host and port only.

    Parameters
    ----------
    text
        DuckDB's message, which may quote the presigned URL it asked for.
    prefixes
        The request's prefixes. Only a host one of them names is said; DuckDB's text is not
        trusted further than that.

    Returns
    -------
    str
        `` at <host>[:<port>]`` when the quoted URL's host and port are a prefix's, otherwise
        the empty string (the message then says "the object store" alone).
    """
    matched = _UNVERIFIED_CERTIFICATE.match(text)
    if matched is None or matched["url"] is None:
        return ""
    with contextlib.suppress(ValueError):
        netloc = urllib.parse.urlsplit(matched["url"]).netloc
        known = {urllib.parse.urlsplit(prefix).netloc for prefix in prefixes}
        if netloc and "@" not in netloc and netloc in known:
            return f" at {netloc}"
    return ""


def failure(
    exc: BaseException,
    asked: threading.Event,
    files: Sequence[str] = (),
    mapped: bool = False,
    *,
    spill: bool = False,
    ca_bundle: str = "",
    prefixes: Sequence[str] = (),
) -> ErrorDocument:
    """Classify a DuckDB failure and word it for the user.

    Parameters
    ----------
    exc
        The `duckdb.Error` or `OSError` that ended the statement.
    asked
        Set once the child was asked to stop; the failure is then `interrupted`, whatever DuckDB
        said. An interrupt DuckDB reports without that ask is a lost cause (`invalid`, as before,
        with a message that says so).
    files
        The file URLs of the reads, as variables carried them. DuckDB quotes a file by its
        name when it cannot read or convert it, and a URL can hold a signature, so each is
        replaced by ``<file>`` in the message.
    mapped
        Whether a read goes through a field-id map. DuckDB then converts a file's column to
        the mapped type while it reads, and a conversion error quotes the cell: under a row
        filter the text names no column, so any conversion error is replaced by a fixed one
        that also covers a cast of the query's own.
    spill
        Whether the request allowed spilling. Without it, running out of memory is worded as
        "cannot spill to disk": decided by the request, never by DuckDB's text.
    ca_bundle
        The request's CA bundle, named in the message of a refused bundle and of a store
        certificate it cannot verify.
    prefixes
        The request's prefixes. A store certificate that cannot be verified is named by host
        and port only, and only when one of these names that host and port.

    Returns
    -------
    ErrorDocument
        The kind and the message for the user. The message never echoes the SQL or a file.
    """
    if asked.is_set():
        return ErrorDocument(INTERRUPTED, _ASKED)
    if _is_interrupt(exc):
        return ErrorDocument(INVALID, _LOST_CAUSE)
    text = strip_duckdb_context(str(exc))
    for url in sorted({url for url in files if url}, key=len, reverse=True):
        text = text.replace(url, "<file>")
    cast = _READ_CAST.search(text)
    if cast is not None:
        # DuckDB appends the cell it could not convert, which can be a masked value or one of a
        # row a filter hides; only the column and the two types are safe to say.
        text = (
            f'a file of a table this query reads stores column "{cast["column"]}" as '
            f"{cast['stored']}, which cannot be read as {cast['wanted']}. Ask the owner of "
            "that table to write its rows again, so that every file stores the column in "
            "one type."
        )
    elif mapped and text.startswith("Conversion Error"):
        text = UNCONVERTED_VALUE
    text = _DIVISION_ADVICE.sub(".", text)
    message = f"Query failed: {text}"
    raw = str(exc)
    if isinstance(exc, (duckdb.IOException, OSError)) and _UNVERIFIED_CERTIFICATE.match(raw):
        # Not transient: a retry meets the same certificate. The text quotes a presigned URL.
        return ErrorDocument(
            INTERNAL,
            f"{STORE_CERTIFICATE_UNVERIFIED}{_store_host(raw, prefixes)} against the "
            f"certificate authorities in {ca_bundle or 'its bundle'}. Set AWS_CA_BUNDLE or "
            f"SSL_CERT_FILE on the worker to a PEM file of your object store's certificate "
            f"authority, then restart the worker; see {STORE_CERTIFICATES}.",
        )
    if _is_transient(exc):
        return ErrorDocument(TRANSIENT, message)
    if _is_out_of_memory(exc):
        if not spill:
            return ErrorDocument(OUT_OF_MEMORY, NO_SPILL)
        reason = text.split("\n", 1)[0].strip()
        return ErrorDocument(OUT_OF_MEMORY, f"Query failed: {reason} {MEMORY_LIMIT_ADVICE}")
    if _is_permission(exc):
        if _CA_REFUSAL in text:
            return ErrorDocument(
                INTERNAL,
                f"Query failed: the query process could not read its certificate authorities "
                f"from {ca_bundle}. Set AWS_CA_BUNDLE or SSL_CERT_FILE on the worker to a "
                f"readable PEM file of your object store's certificate authority; see "
                f"{NETWORK_SETTINGS}.",
            )
        return ErrorDocument(INVALID, REFUSED_LOCATION)
    if _is_internal(exc):
        return ErrorDocument(INTERNAL, message)
    return ErrorDocument(INVALID, message)


class Session:
    """The statement's connection, shared between its thread and the main thread.

    The connection is private; use `open`, `interrupt` and `close`.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._connection: duckdb.DuckDBPyConnection | None = None
        self.bytes_read: int | None = None
        """DuckDB's count of the bytes the statement read; set by `measure`, else `None`."""

    def open(self, config: ConnectionConfig) -> duckdb.DuckDBPyConnection:
        """Open the in-memory connection.

        Parameters
        ----------
        config
            The options it is opened with (`connection_config`).

        Returns
        -------
        duckdb.DuckDBPyConnection
            The connection, which `interrupt` and `close` now act on.
        """
        connection = duckdb.connect(":memory:", config=config)
        with self._lock:
            self._connection = connection
        return connection

    def interrupt(self) -> None:
        """Interrupt the running statement, if the connection is open."""
        with self._lock:
            if self._connection is not None:
                self._connection.interrupt()

    def measure(self) -> None:
        """Keep DuckDB's own count of the bytes the last statement read.

        Call it only when the statement finished successfully: after a failed one the profile is
        still the previous statement's. `bytes_read` stays `None` when DuckDB has no count (a
        statement answered from metadata).
        """
        with self._lock:
            connection = self._connection
        if connection is None:
            return
        try:
            document = json.loads(connection.get_profiling_information("json"))
        except (duckdb.Error, ValueError, TypeError):
            return
        io = document.get("io") if isinstance(document, dict) else None
        if isinstance(io, dict):
            count = io.get("total_bytes_read")
            if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
                self.bytes_read = count

    def close(self) -> None:
        """Close the connection; a later `interrupt` does nothing."""
        with self._lock:
            connection, self._connection = self._connection, None
        if connection is not None:
            connection.close()


def _stream(
    reader: pa.RecordBatchReader,
    sink: BinaryIO,
    asked: threading.Event,
    classify: Callable[[BaseException], ErrorDocument],
) -> ErrorDocument | None:
    """Write a result to the sink as an Arrow IPC stream, end marker included.

    An error from DuckDB (raised by `next`) is caught by the inner handler, an error from the
    sink (raised by a write) by the outer one: a broken pipe is an `OSError` too, and must not
    be reported as a storage error of the query.

    Parameters
    ----------
    reader
        The result as an Arrow reader.
    sink
        Where the stream goes (standard output).
    asked
        Set once the child was asked to stop; checked before each batch.
    classify
        Turns an error of DuckDB into the document (`failure`, with the request's details).

    Returns
    -------
    ErrorDocument or None
        `None` after the end marker was written; otherwise why the stream ends without it.
    """
    iterator = iter(reader)
    try:
        writer = pa.ipc.new_stream(sink, reader.schema)
        while True:
            if asked.is_set():
                return ErrorDocument(INTERRUPTED, _ASKED)
            try:
                batch = next(iterator)
            except StopIteration:
                break
            except (duckdb.Error, OSError) as exc:
                return classify(exc)
            writer.write_batch(batch)
        writer.close()
        sink.flush()
    except (OSError, pa.ArrowInvalid):
        return ErrorDocument(INTERRUPTED, _SINK_CLOSED)
    return None


def _check(connection: duckdb.DuckDBPyConnection, request: Request) -> ErrorDocument | None:
    """Try the two size settings and nothing else (the worker's start-up check).

    Parameters
    ----------
    connection
        A fresh in-memory connection.
    request
        Supplies ``memory_limit`` and ``max_temp_directory_size``.

    Returns
    -------
    ErrorDocument or None
        An `invalid` document whose message is the name of the first setting DuckDB cannot
        read; `None` if both are accepted.
    """
    for field in ("memory_limit", "max_temp_directory_size"):
        try:
            connection.execute(f"SET {field} = ?", [getattr(request, field)])
        except duckdb.Error:
            return ErrorDocument(INVALID, field)
    return None


def _report_unexpected(exc: BaseException) -> None:
    """Print where an unexpected exception was raised, to standard error.

    Only the frames (this module's own source lines) and the exception's type are printed, never
    its message: DuckDB's messages can quote the statement. The document the child sends is the
    last line, so the worker's log has the cause and the reader has the document.

    Parameters
    ----------
    exc
        The exception.
    """
    frames = "".join(traceback.format_tb(exc.__traceback__))
    with contextlib.suppress(Exception):
        sys.stderr.write(f"Traceback (most recent call last):\n{frames}{type(exc).__qualname__}\n")


def set_variables(
    connection: duckdb.DuckDBPyConnection, variables: Mapping[str, ParameterValue]
) -> None:
    """Set each value the statement reads as a DuckDB variable of that name.

    The value is bound, never written into SQL text, so no URL can appear in a statement
    DuckDB quotes back in an error. The statement then runs with no parameters, through the
    call whose result streams and keeps an Arrow export in DuckDB 2.

    Parameters
    ----------
    connection
        The configured connection.
    variables
        Name to value, as the request carries them.

    Raises
    ------
    ValueError
        If a name is not an identifier. The platform generates every name, so this is a
        defect of the platform, never a user's mistake.
    """
    for name, value in variables.items():
        if _VARIABLE_NAME.fullmatch(name) is None:
            raise ValueError(f"query variable {ascii(name)[:60]} is not an identifier")
        # `name` is an identifier, checked above; the value is bound.
        statement = f'SET VARIABLE "{name}" = '
        if isinstance(value, list):
            connection.execute(statement + "?::VARCHAR[]", [value])
        elif isinstance(value, tuple):
            # `read_parquet(schema = ...)` needs all three children (DuckDB 1.5 and 2); a NULL
            # default makes a column a file lacks read as NULL, as Iceberg says.
            connection.execute(
                statement + "MAP(?::INTEGER[], "
                "?::STRUCT(name VARCHAR, type VARCHAR, default_value VARCHAR)[])",
                [
                    [field_id for field_id, _name, _type in value],
                    [
                        {"name": name, "type": type_, "default_value": None}
                        for _field_id, name, type_ in value
                    ],
                ],
            )
        else:
            connection.execute(statement + "?", [value])


def _serialised(connection: duckdb.DuckDBPyConnection, sql: str) -> str:
    """Return DuckDB's parse tree of `sql` as JSON, or an empty text if it cannot give one.

    Parameters
    ----------
    connection
        The connection, not yet configured: only DuckDB's parser is used, nothing is read.
    sql
        The statement, bound as a value and never written into SQL text.

    Returns
    -------
    str
        The JSON document; empty (which `reads_only` refuses) when DuckDB raises or returns
        anything but one text.
    """
    try:
        row = connection.execute(SERIALISE, [sql]).fetchone()
    except duckdb.Error:
        return ""
    return row[0] if row is not None and isinstance(row[0], str) else ""


def reads_only(document: str) -> bool:
    """Tell whether a serialised statement is one statement made of reading query nodes only.

    Fails closed: a document that is not JSON, that reports an error, that holds anything but one
    statement, or that contains a query node of a type not in `READING_QUERY_NODES` anywhere in
    its tree is refused. A query node is an object whose ``type`` ends in ``_NODE``, or one with
    the fields every DuckDB query node has (``modifiers`` and ``cte_map``), so that a node type
    a later DuckDB names differently is refused as well. So is any object whose
    ``function_name`` is not a text or, lower-cased, is in `SQL_RUNNING_FUNCTIONS`: that covers
    function expressions and the function of a table function reference alike. The walk is
    iterative, so a deep tree cannot exhaust the stack.

    Parameters
    ----------
    document
        The output of `SERIALISE`.

    Returns
    -------
    bool
        `True` only if the statement can be run as a read.
    """
    try:
        tree = json.loads(document)
    except (ValueError, RecursionError):
        return False
    if not isinstance(tree, dict) or tree.get("error") is not False:
        return False
    statements = tree.get("statements")
    if not isinstance(statements, list) or len(statements) != 1:
        return False
    pending: list[object] = [statements[0]]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            kind = item.get("type")
            named = isinstance(kind, str)
            node = (named and kind.endswith("_NODE")) or ("modifiers" in item and "cte_map" in item)
            if node and not (named and kind in READING_QUERY_NODES):
                return False
            if "function_name" in item:
                function = item["function_name"]
                if not isinstance(function, str) or function.lower() in SQL_RUNNING_FUNCTIONS:
                    return False
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    return True


def serve(
    request: Request, sink: BinaryIO, session: Session, asked: threading.Event
) -> ErrorDocument | None:
    """Run one request to its end.

    Raises nothing: an exception that was not expected becomes an ``internal`` document (its type
    only), after its frames and type are printed to standard error. Closing the connection cannot
    raise into the caller either.

    Parameters
    ----------
    request
        What to run and how DuckDB is configured.
    sink
        Where the Arrow stream goes.
    session
        Holds the connection, so the main thread can interrupt it.
    asked
        Set once the child was asked to stop.

    Returns
    -------
    ErrorDocument or None
        `None` for a complete stream; otherwise why the statement failed. An unexpected
        exception becomes an `internal` document that names its class and nothing else.
    """
    try:
        if request.mode != CHECK and file_outside_prefixes(request):
            return ErrorDocument(INTERNAL, OUTSIDE_PREFIXES)
        connection = session.open(connection_config(request))
        files = [
            url for value in request.parameters.values() if isinstance(value, list) for url in value
        ]
        classify = functools.partial(
            failure,
            asked=asked,
            files=files,
            mapped=any(isinstance(value, tuple) for value in request.parameters.values()),
            spill=request.spill,
            ca_bundle=request.ca_bundle,
            prefixes=request.read_prefixes,
        )
        try:
            if request.mode == CHECK:
                refused = _check(connection, request)
                if refused is not None:
                    return refused
                reader = pa.RecordBatchReader.from_batches(pa.schema([]), [])
            else:
                statements = connection.extract_statements(request.sql)
                if not statements:
                    return ErrorDocument(INVALID, NO_RESULT_MESSAGE)
                if len(statements) != 1 or statements[0].type != duckdb.StatementType.SELECT:
                    return ErrorDocument(INTERNAL, NOT_ONE_SELECT)
                if not reads_only(_serialised(connection, request.sql)):
                    return ErrorDocument(INTERNAL, WRITES_IN_SELECT)
                configure(connection, request)
                if asked.is_set():
                    return ErrorDocument(INTERRUPTED, _ASKED)
                set_variables(connection, request.parameters)
                # `sql` and not `execute`: its relation streams through `to_arrow_reader`, and it
                # is the call whose result keeps an Arrow export in DuckDB 2. Nothing is bound to
                # the statement; what it reads was set as variables above. One SELECT always
                # gives a relation, so there is no `None` to handle here (an empty text was
                # answered above).
                reader = connection.sql(request.sql).to_arrow_reader(request.batch_rows)
        except ExtensionUnavailable as exc:
            return ErrorDocument(INVALID, str(exc))
        except (duckdb.Error, OSError) as exc:
            return classify(exc)
        document = _stream(reader, sink, asked, classify)
        if document is None:
            with contextlib.suppress(Exception):  # measuring must never fail a good query
                session.measure()
        return document
    except BaseException as exc:  # the statement's thread must hand back a document
        _report_unexpected(exc)
        return ErrorDocument(
            INTERNAL,
            f"Query failed: the query process hit an unexpected error ({type(exc).__name__}).",
        )
    finally:
        with contextlib.suppress(Exception):
            session.close()


def write_usage(path: str, bytes_read: int | None, peak_memory_bytes: int | None) -> None:
    """Write the figures the child measured about itself; a failure only loses them.

    Parameters
    ----------
    path
        The file; it must not exist, and a symbolic link is not followed.
    bytes_read
        Bytes the statement read, or `None` if DuckDB had no count.
    peak_memory_bytes
        The child's own peak resident memory, or `None` where it cannot be read.
    """
    document = json.dumps({"bytes_read": bytes_read, "peak_memory_bytes": peak_memory_bytes})
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            handle.write(document)
    except OSError:
        return


def main(
    stdin: BinaryIO,
    stream: BinaryIO,
    stderr: TextIO,
    asked: threading.Event,
    parent: Callable[[], int] = os.getppid,
    usage_path: str | None = None,
) -> int:
    """Read the request, run it in a second thread, and report how it ended.

    Parameters
    ----------
    stdin
        Where the request is read from.
    stream
        Where the Arrow stream is written.
    stderr
        Where a refusal or the error document is written.
    asked
        Set by the ``SIGTERM`` handler; the main thread then interrupts the connection every
        tenth of a second until the statement's thread ends.
    parent
        Returns the process id of the parent; compared with the request's ``parent_pid``.
    usage_path
        Where to write the figures the child measured (`write_usage`) once the statement's
        thread has ended; `None` writes nothing.

    Returns
    -------
    int
        `EXIT_OK`, `EXIT_REFUSED` (unreadable request, or the worker is gone) or `EXIT_FAILED`.
    """
    try:
        request = decode_request(stdin.read(MAX_REQUEST_BYTES + 1))
    except ValueError as exc:
        stderr.write(f"{exc}\n")
        return EXIT_REFUSED
    if parent() != request.parent_pid:
        stderr.write("the worker that started this query is gone\n")
        return EXIT_REFUSED
    session = Session()
    outcome: list[ErrorDocument | None] = []
    thread = threading.Thread(
        target=lambda: outcome.append(serve(request, stream, session, asked)),
        name="lakana-query",
        daemon=True,
    )
    thread.start()
    while thread.is_alive():
        thread.join(_INTERRUPT_SECONDS)
        if asked.is_set():
            session.interrupt()
    if usage_path is not None:
        write_usage(usage_path, session.bytes_read, own_peak_bytes())
    error = outcome[0]
    if error is None:
        return EXIT_OK
    stderr.write(encode_error(error) + "\n")
    return EXIT_FAILED


def die_with_parent() -> None:
    """Ask the kernel to kill this process when the worker that started it dies (Linux).

    The signal fires when the *thread* that started this process exits, so the worker keeps
    that thread alive until the process is reaped. A worker that died before this call is
    caught by the ``parent_pid`` check in `main`. Other platforms have no such request.

    Raises
    ------
    SystemExit
        With status `EXIT_REFUSED` if the kernel refuses the request.
    """
    if not sys.platform.startswith("linux"):
        return
    if ctypes.CDLL(None, use_errno=True).prctl(_PR_SET_PDEATHSIG, signal.SIGKILL) != 0:
        sys.stderr.write("could not ask the kernel to end this process with its worker\n")
        raise SystemExit(EXIT_REFUSED)


def run_cli() -> NoReturn:
    """Prepare the process, run `main`, and end the process at once.

    In order: the kernel request of `die_with_parent`; the ``SIGTERM`` handler; a private copy
    of descriptor 1 for the Arrow stream, and descriptor 1 itself pointed at standard error.
    The process ends with `os._exit`, so that no thread DuckDB or pyarrow left behind and no
    interpreter shutdown can delay it.
    """
    code = EXIT_REFUSED
    try:
        die_with_parent()
        asked = threading.Event()
        signal.signal(signal.SIGTERM, lambda signum, frame: asked.set())
        stream_fd = os.dup(1)
        os.dup2(2, 1)
        # Not closed: closing flushes, and a reader that went away makes that raise again behind
        # the error document. `_stream` flushes a complete result; `os._exit` below ends the rest.
        stream = os.fdopen(stream_fd, "wb")
        code = main(sys.stdin.buffer, stream, sys.stderr, asked, usage_path=USAGE_FILE)
    except Exception:
        traceback.print_exc(file=sys.stderr)
    finally:
        with contextlib.suppress(Exception):
            sys.stderr.flush()
        os._exit(code)


if __name__ == "__main__":
    run_cli()
