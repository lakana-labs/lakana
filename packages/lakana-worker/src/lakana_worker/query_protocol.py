"""What the worker and a query child say to each other.

The worker starts one process per query (`lakana_worker.query_child`) and hands it one JSON
document on standard input: the rewritten SQL, the values it reads as variables and what
DuckDB is configured with. Nothing else crosses: no database URL, no storage credential, no
token, no secret. The child answers with an Arrow IPC stream on standard output and, when
the statement failed, one JSON line on standard error.

This module imports the standard library only. The child imports it, and every import the child
makes is paid by every query (importing `lakana_core.config` alone costs 130 ms).
"""

from __future__ import annotations

import datetime as dt
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

READ = "read"
WRITE = "write"
DESCRIBE = "describe"
CHECK = "check"
MODES = frozenset({READ, WRITE, DESCRIBE, CHECK})
"""What a child does: stream a result (`read`), stream one in UTC for a write (`write`), plan a
statement and stream its schema only (`describe`), or only apply the size settings (`check`)."""

END_OF_STREAM = b"\xff\xff\xff\xff\x00\x00\x00\x00"
"""The last eight bytes of a complete Arrow IPC stream. pyarrow's reader accepts a stream that
simply stops, so the worker checks these bytes itself."""

EXIT_OK = 0
EXIT_REFUSED = 2
"""The child did not run: its request was unreadable or its worker was gone."""
USAGE_FILE = "usage.json"
"""What the child tells the worker about itself at its end (``bytes_read``, ``peak_memory_bytes``),
written into its working directory; the worker reads at most `MAX_USAGE_BYTES` of it."""
MAX_USAGE_BYTES = 256
"""The most the worker reads of the usage file; a longer one is not trusted."""

EXIT_FAILED = 3
"""The statement failed; an error document is the last line on standard error."""

INVALID = "invalid"
TRANSIENT = "transient"
OUT_OF_MEMORY = "out_of_memory"
INTERNAL = "internal"
INTERRUPTED = "interrupted"
KINDS = frozenset({INVALID, TRANSIENT, OUT_OF_MEMORY, INTERNAL, INTERRUPTED})
"""How a child classifies a failure. `interrupted` means it stopped because it was asked to or
because the worker stopped reading; the worker then names the reason itself."""

UNCONVERTED_VALUE = (
    "a value could not be converted (the value is not shown, because it may come from "
    "data you cannot see). Check the casts in the query; otherwise a file of this table "
    "stores a column in a type the read cannot convert it to."
)
"""What a child says of a conversion error on a read through a field-id map, in place of the
engine's text (which quotes the cell). The worker matches on it to recognise such an error."""

MAX_ERROR_BYTES = 5_000
"""Longest error message a child sends and the worker accepts, in bytes of its UTF-8 text. A longer
message is cut on a character boundary and ends in an ellipsis. JSON escaping makes one byte of a
message at most six bytes of the line (a control character is ``\\u0001``), so the whole line
stays under 32 KiB, see `MAX_ERROR_LINE_BYTES`."""

MAX_ERROR_LINE_BYTES = 6 * MAX_ERROR_BYTES + 256
"""Longest line `encode_error` can return (30,256 bytes: the line is ASCII). The worker keeps at
least twice this much of a child's standard error, so a traceback before the document cannot push
the document out."""

MAX_REQUEST_BYTES = 64 * 1024 * 1024
"""Largest request a child reads (64 MiB); a larger one is refused.

The rewriter binds one presigned URL per data file, about 0.5 kB each: 64 MiB hold more than
100,000 files. The worker measures the request before it starts a process
(`lakana_worker.executor`) and tells the user how many files the query would read."""


MEMORY_LIMIT_ADVICE = (
    "Read or compute less (filter or aggregate earlier, or select fewer columns), or ask the "
    "operator to raise DuckDB's limit for one query (LAKANA_QUERY_LIMITS__MEMORY_LIMIT)."
)
"""What to do about an out-of-memory failure. Replaces DuckDB's own advice, which tells the
caller to run `SET` statements that a query is not allowed to run."""

STORE_CERTIFICATE_UNVERIFIED = (
    "Query failed: the query process could not verify the TLS certificate of the object store"
)
"""The start of the message for a store certificate the query process cannot verify (#277).
Lakana's own wording, with nothing of the engine's text in it, so it is kept under a policy."""

NO_RESULT_MESSAGE = "Query produced no result — check for empty or comment-only SQL."
"""The message for SQL that DuckDB runs without a result set."""

_NAME_CHARS = 60
_ERROR_MARK = "lakana_query_error"
_TEXT_FIELDS = ("sql", "memory_limit", "temp_directory", "max_temp_directory_size", "ca_bundle")

FieldIdMap = tuple[tuple[int, str, str], ...]
"""An Iceberg read's field-id map: field id, column name and DuckDB type of each column."""

ParameterValue = list[str] | FieldIdMap | int | Decimal | dt.date | dt.datetime
"""What the platform sets as a variable: a list of file URLs, a field-id map, or an incremental
write's watermark."""


def _is_field_entry(entry: object) -> bool:
    """Tell whether one entry has the shape of a field-id map's entry.

    Parameters
    ----------
    entry
        A tuple (when encoding) or a list (when decoding).

    Returns
    -------
    bool
        Whether it is a field id (an `int`, never a `bool`), a name and a type.
    """
    return (
        isinstance(entry, (tuple, list))
        and len(entry) == 3
        and type(entry[0]) is int
        and isinstance(entry[1], str)
        and isinstance(entry[2], str)
    )


EXTENSION_SOURCE_DUCKDB = "duckdb"
"""`LAKANA_WORKER__EXTENSION_SOURCE` unset: DuckDB's own extension server."""

EXTENSION_SOURCE_NONE = "none"
"""`LAKANA_WORKER__EXTENSION_SOURCE=none`: never download an extension."""

_MIRROR = re.compile(r"https://[^\s'\"\\\x00-\x1f\x7f@]+")


def valid_extension_source(text: str) -> bool:
    """Say whether a request's extension source is one the child will use.

    The mirror is written into an ``INSTALL ... FROM '<url>'`` statement, so it may hold no
    quote, backslash, blank, control character or ``@`` (user information); the worker's own
    settings check says more, and this is the second line on the child's side.

    Parameters
    ----------
    text
        The request's value.

    Returns
    -------
    bool
        Whether it is ``duckdb``, ``none`` or an ``https://`` URL of that shape.
    """
    return text in {EXTENSION_SOURCE_DUCKDB, EXTENSION_SOURCE_NONE} or bool(_MIRROR.fullmatch(text))


@dataclass(frozen=True)
class Request:
    """Everything a query child is given.

    Attributes
    ----------
    mode
        One of `MODES`.
    sql
        The rewritten SQL.
    parameters
        The values the child sets as DuckDB variables, by name, before the statement runs.
    read_prefixes
        The locations the statement may open, each ending in ``/``: the directory (local mode)
        or the URL prefix (``http(s)://host[:port]/<path>/``) of every table the query reads.
        They become DuckDB's ``allowed_directories``; every file the variables name must lie
        under one of them.
    ca_bundle
        The PEM file of certificate authorities httpfs trusts (``ca_cert_file``), and the only
        file DuckDB may open outside the prefixes (``allowed_paths``).
    spill
        Whether DuckDB may spill to ``temp_directory`` (local ``file://`` mode only); otherwise
        ``max_temp_directory_size`` is ``0KB`` and a query over ``memory_limit`` fails.
    memory_limit
        DuckDB's ``memory_limit``.
    threads
        DuckDB's ``threads``.
    temp_directory
        DuckDB's ``temp_directory``.
    max_temp_directory_size
        DuckDB's ``max_temp_directory_size``.
    batch_rows
        Rows per Arrow batch.
    extension_directory
        Where the worker's installed DuckDB extensions are.
    parent_pid
        The worker's process id; a child whose parent is another process refuses to run.
    extension_source
        Where DuckDB may get the ``httpfs`` extension from (`EXTENSION_SOURCE_DUCKDB`,
        `EXTENSION_SOURCE_NONE` or an ``https://`` mirror), see `valid_extension_source`.
    """

    mode: str
    sql: str
    parameters: Mapping[str, ParameterValue]
    read_prefixes: tuple[str, ...]
    ca_bundle: str
    spill: bool
    memory_limit: str
    threads: int
    temp_directory: str
    max_temp_directory_size: str
    batch_rows: int
    extension_directory: str
    parent_pid: int
    extension_source: str = EXTENSION_SOURCE_DUCKDB


@dataclass(frozen=True)
class ErrorDocument:
    """Why a statement failed, as the child reports it.

    Attributes
    ----------
    kind
        One of `KINDS`.
    message
        The text for the user.
    """

    kind: str
    message: str


def _name(name: object) -> str:
    """Quote a field or parameter name for a message, at most 60 characters.

    Parameters
    ----------
    name
        The name.

    Returns
    -------
    str
        ``ascii(name)`` cut to `_NAME_CHARS` characters.
    """
    return ascii(name)[:_NAME_CHARS]


def _cut(message: str) -> str:
    """Cut a message to `MAX_ERROR_BYTES` bytes of UTF-8 on a character boundary.

    Parameters
    ----------
    message
        The text.

    Returns
    -------
    str
        `message` itself if it fits; otherwise its start, followed by an ellipsis.
    """
    raw = message.encode("utf-8", "replace")
    if len(raw) <= MAX_ERROR_BYTES:
        return message
    return raw[: MAX_ERROR_BYTES - 3].decode("utf-8", "ignore") + "\u2026"


def _encode_value(name: str, value: object) -> dict[str, object]:
    """Tag one bound value with its type.

    Parameters
    ----------
    name
        The parameter's name, for the error message only.
    value
        The bound value.

    Returns
    -------
    dict
        A one-key object: the tag, and the value as text, a list of texts or a list of
        field-id entries.

    Raises
    ------
    TypeError
        If the value has a type the platform does not bind. The message names the parameter
        and the type, never the value.
    """
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return {"strings": list(value)}
    if isinstance(value, tuple) and all(_is_field_entry(entry) for entry in value):
        return {"field_ids": [list(entry) for entry in value]}
    if isinstance(value, bool):
        raise TypeError(
            f"Query parameter {_name(name)} has a type the query process cannot take: bool."
        )
    if isinstance(value, int):
        return {"int": str(value)}
    if isinstance(value, Decimal):
        return {"decimal": str(value)}
    if isinstance(value, dt.datetime):
        return {"timestamp": value.isoformat()}
    if isinstance(value, dt.date):
        return {"date": value.isoformat()}
    raise TypeError(
        f"Query parameter {_name(name)} has a type the query process cannot take: "
        f"{type(value).__name__}."
    )


def _decode_value(name: str, tagged: object) -> ParameterValue:
    """Read one tagged value back.

    Parameters
    ----------
    name
        The parameter's name, for the error message only.
    tagged
        The decoded JSON of the value.

    Returns
    -------
    list or tuple or int or Decimal or date or datetime
        The value, in the type its tag names.

    Raises
    ------
    ValueError
        If the value is not one tagged value, is unreadable, or has an unknown tag.
    """
    if not isinstance(tagged, dict) or len(tagged) != 1:
        raise ValueError(f"query request: parameter {_name(name)} is not one tagged value")
    ((tag, raw),) = tagged.items()
    if tag == "strings" and isinstance(raw, list) and all(isinstance(item, str) for item in raw):
        return list(raw)
    if tag == "field_ids" and isinstance(raw, list) and all(_is_field_entry(e) for e in raw):
        return tuple((entry[0], entry[1], entry[2]) for entry in raw)
    if not isinstance(raw, str):
        raise ValueError(f"query request: parameter {_name(name)} has an unreadable value")
    try:
        if tag == "int":
            return int(raw)
        if tag == "decimal":
            return Decimal(raw)
        if tag == "timestamp":
            return dt.datetime.fromisoformat(raw)
        if tag == "date":
            return dt.date.fromisoformat(raw)
    except (ValueError, InvalidOperation) as exc:
        raise ValueError(f"query request: parameter {_name(name)} has an unreadable value") from exc
    raise ValueError(f"query request: parameter {_name(name)} has an unknown type")


def encode_request(request: Request) -> bytes:
    """Serialise a request for a child's standard input.

    Parameters
    ----------
    request
        What the child is given.

    Returns
    -------
    bytes
        A JSON document, UTF-8 encoded.

    Raises
    ------
    TypeError
        If a bound value has a type the child cannot take (see `_encode_value`).
    """
    document = {
        "mode": request.mode,
        "sql": request.sql,
        "parameters": {
            name: _encode_value(name, value) for name, value in request.parameters.items()
        },
        "read_prefixes": list(request.read_prefixes),
        "ca_bundle": request.ca_bundle,
        "spill": request.spill,
        "memory_limit": request.memory_limit,
        "threads": request.threads,
        "temp_directory": request.temp_directory,
        "max_temp_directory_size": request.max_temp_directory_size,
        "batch_rows": request.batch_rows,
        "extension_directory": request.extension_directory,
        "parent_pid": request.parent_pid,
        "extension_source": request.extension_source,
    }
    return json.dumps(document).encode("utf-8")


def _positive(document: dict[str, object], key: str) -> int:
    """Read a positive integer field.

    Parameters
    ----------
    document
        The decoded request.
    key
        The field.

    Returns
    -------
    int
        The field's value.

    Raises
    ------
    ValueError
        If the field is missing, not an integer (a boolean is not one), or below 1.
    """
    value = document.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"query request: {key} must be a positive integer")
    return value


def decode_request(data: bytes) -> Request:
    """Read a request from a child's standard input.

    Parameters
    ----------
    data
        What the child read from standard input.

    Returns
    -------
    Request
        The request, every field checked.

    Raises
    ------
    ValueError
        If the document is not a request. The message starts ``query request:`` and names the
        field, never its value.
    """
    if len(data) > MAX_REQUEST_BYTES:
        raise ValueError("query request: larger than 64 MiB")
    try:
        document = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise ValueError("query request: not a JSON document") from exc
    if not isinstance(document, dict):
        raise ValueError("query request: not a JSON object")
    known = {*_TEXT_FIELDS, "mode", "parameters", "read_prefixes", "spill", "threads"}
    known |= {"batch_rows", "extension_directory", "parent_pid", "extension_source"}
    unknown = sorted(set(document) - known)
    if unknown:
        raise ValueError(f"query request: unknown field {_name(unknown[0])}")
    for key in (*_TEXT_FIELDS, "extension_directory"):
        if not isinstance(document.get(key), str):
            raise ValueError(f"query request: {key} must be text")
    source = document.get("extension_source")
    if not isinstance(source, str) or not valid_extension_source(source):
        raise ValueError("query request: extension_source must be duckdb, none or an https URL")
    mode = document.get("mode")
    if not isinstance(mode, str) or mode not in MODES:
        raise ValueError("query request: mode must be read, write, describe or check")
    if not isinstance(document.get("spill"), bool):
        raise ValueError("query request: spill must be true or false")
    prefixes = document.get("read_prefixes")
    if not isinstance(prefixes, list) or not all(isinstance(item, str) for item in prefixes):
        raise ValueError("query request: read_prefixes must be a list of texts")
    parameters = document.get("parameters")
    if not isinstance(parameters, dict):
        raise ValueError("query request: parameters must be an object")
    return Request(
        mode=document["mode"],
        sql=document["sql"],
        parameters={name: _decode_value(name, value) for name, value in parameters.items()},
        read_prefixes=tuple(prefixes),
        ca_bundle=document["ca_bundle"],
        spill=document["spill"],
        memory_limit=document["memory_limit"],
        threads=_positive(document, "threads"),
        temp_directory=document["temp_directory"],
        max_temp_directory_size=document["max_temp_directory_size"],
        batch_rows=_positive(document, "batch_rows"),
        extension_directory=document["extension_directory"],
        parent_pid=_positive(document, "parent_pid"),
        extension_source=source,
    )


def encode_error(error: ErrorDocument) -> str:
    """Serialise an error document as one line.

    Parameters
    ----------
    error
        The error to report.

    Returns
    -------
    str
        A JSON object without a newline; the message is cut at `MAX_ERROR_BYTES`.
    """
    return json.dumps({_ERROR_MARK: 1, "kind": error.kind, "message": _cut(error.message)})


def decode_error(stderr: bytes) -> ErrorDocument | None:
    """Find the error document in what a child wrote to standard error.

    Never raises: whatever the child wrote, an input that is not a well-formed document gives
    `None`.

    Parameters
    ----------
    stderr
        Everything the child wrote to standard error (or its tail).

    Returns
    -------
    ErrorDocument or None
        The document on the last non-empty line, with a message cut at `MAX_ERROR_BYTES`; `None`
        if that line is anything else, longer than `MAX_ERROR_LINE_BYTES`, or not valid UTF-8.
    """
    lines = [line for line in stderr.splitlines() if line.strip()]
    if not lines or len(lines[-1]) > MAX_ERROR_LINE_BYTES:
        return None
    try:
        document = json.loads(lines[-1].decode("utf-8"))
    except (ValueError, RecursionError):
        return None
    if not isinstance(document, dict) or document.get(_ERROR_MARK) != 1:
        return None
    kind, message = document.get("kind"), document.get("message")
    if not isinstance(kind, str) or kind not in KINDS or not isinstance(message, str):
        return None
    return ErrorDocument(kind, _cut(message))
