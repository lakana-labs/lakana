"""What a running asset knows about its own run: `ctx`, and the state the helpers share.

The harness creates one `AssetContext` per attempt and passes it as the function's `ctx`
parameter when the function declares one. `current()` is how `lakana.ingest` finds the task's
client, the connections the asset declared and the values to hide; it exists only while the
harness runs the asset, and until the harness has written what the run left behind (its result
record and the trace of a failure), so the values to hide are still known then.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING
from urllib.parse import quote, quote_plus

if TYPE_CHECKING:
    from lakana.client import Client

WatermarkValue = int | Decimal | dt.date | dt.datetime
"""A watermark's value: what `AssetContext.watermark` returns."""
WATERMARK_TAGS = ("int", "long", "decimal", "date", "timestamp", "timestamptz")
"""The tags of the worker's encoding, one per column type a watermark may have."""
MIN_SECRET_CHARS = 4
"""Shortest value `register_secret` masks: a shorter one would mask ordinary text."""
FILES_READ_KEY = "lakana.files_read"
"""The upload's schema metadata key for the record; `lakana_core.catalog.files_read` reads it."""
MAX_FILES_READ = 100_000
"""The most files one run reports, and the most the worker hands a task; the server's cap too."""
RECORD_WINDOW = dt.timedelta(days=30)
"""The longest lookback, and how far before the watermark the worker's record reaches."""
MAX_ETAG = 512
CONNECTION_RESERVE = 16
"""Room the record keeps for the newest file of each connection a run read (an asset declares
at most 16); `lakana_core.catalog.files_read` has the same."""
RECORD_BUDGET_BYTES = 60 * 1024 * 1024
"""The most bytes of entries a run sends; the newest files that fit are kept, so a record of
long names never makes the server (whose cap is 64 MiB) refuse the run."""
"""The longest ETag the record holds; a longer one is sent as none."""


def decode_watermark(text: str) -> WatermarkValue:
    """Decode the worker's ``<tag>:<value>`` form of a watermark.

    Parameters
    ----------
    text
        The encoded watermark.

    Returns
    -------
    int, Decimal, date or datetime
        The value; a ``timestamptz`` is UTC-aware, a ``timestamp`` naive.

    Raises
    ------
    ValueError
        For an unknown tag or a malformed value; the message names neither.
    """
    tag, separator, raw = text.partition(":")
    if not separator or tag not in WATERMARK_TAGS:
        raise ValueError("not a watermark: expected <type>:<value>")
    try:
        if tag in ("int", "long"):
            return int(raw)
        if tag == "decimal":
            return Decimal(raw)
        if tag == "date":
            return dt.date.fromisoformat(raw)
        return dt.datetime.fromisoformat(raw)
    except (ValueError, InvalidOperation):
        raise ValueError(f"not a watermark: the value of a {tag} watermark is malformed") from None


@dataclass(frozen=True)
class AssetContext:
    """The context of one run of an asset, passed as `ctx`.

    Parameters
    ----------
    watermark_value
        The decoded watermark, or `None` when the table has none yet or the asset has no
        `watermark`.
    """

    watermark_value: WatermarkValue | None = None

    def watermark(self, default: WatermarkValue | None = None) -> WatermarkValue | None:
        """Return the largest value of the asset's `watermark` column that its table holds.

        Parameters
        ----------
        default
            What to return on a first run, such as ``datetime(2020, 1, 1, tzinfo=UTC)``.

        Returns
        -------
        int, Decimal, date, datetime or None
            The value; `default` when the table has none yet (or the asset has no
            `watermark`).
        """
        return default if self.watermark_value is None else self.watermark_value


# The copy of `lakana_core.catalog.files_read.FileRead`: the SDK cannot import lakana_core, and
# `lakana-worker/tests/test_files_read_parity.py` keeps the two the same.
# jscpd:ignore-start
@dataclass(frozen=True)
class ReadFile:
    """One file `lakana.ingest.files.read` handed to the asset: what makes it the same file.

    Parameters
    ----------
    connection
        The connection's name.
    name
        Its path relative to the connection.
    size
        Its size in bytes, as listed.
    modified
        The time `files.read` used for it, UTC.
    etag
        Its ETag where the store reports one.
    """

    connection: str
    name: str
    size: int
    modified: dt.datetime
    etag: str | None = None

    def to_json(self) -> dict[str, object]:
        """Render the file as the record's JSON carries it.

        Returns
        -------
        dict[str, object]
            The five fields, the time in ISO 8601.
        """
        return {
            "connection": self.connection,
            "name": self.name,
            "size": self.size,
            "modified": self.modified.isoformat(),
            "etag": self.etag,
        }

    @classmethod
    def from_json(cls, value: object) -> ReadFile:
        """Read one file of the record the worker hands the task.

        Parameters
        ----------
        value
            The JSON value.

        Returns
        -------
        ReadFile
            The file, its time in UTC.

        Raises
        ------
        ValueError
            ``not a file read``, naming no value, for anything else.
        """
        bad = ValueError("not a file read")
        if not isinstance(value, dict):
            raise bad
        connection, name, size, etag = (
            value.get(k) for k in ("connection", "name", "size", "etag")
        )
        stamp = value.get("modified")
        try:
            modified = dt.datetime.fromisoformat(stamp) if isinstance(stamp, str) else None
        except ValueError:
            raise bad from None
        if (
            not isinstance(connection, str)
            or not connection
            or not isinstance(name, str)
            or not name
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or modified is None
            or modified.tzinfo is None
            or not (etag is None or (isinstance(etag, str) and len(etag) <= MAX_ETAG))
        ):
            raise bad
        return cls(connection, name, size, modified.astimezone(dt.UTC), etag)
        # jscpd:ignore-end


@dataclass
class _TaskState:
    """What the helpers share during one attempt.

    Parameters
    ----------
    client
        The SDK client holding the task's token.
    environment
        The environment's name.
    asset
        The asset's table.
    declared
        The connection names the asset declares.
    connections
        Routing per declared connection from the task description (`kind`, and from the
        Docker runner `relay_port`, `root_path`, `grant`).
    secrets
        Values the harness masks in everything it writes.
    recorded_connections
        The connections with any file recorded inside the window, from the worker; `None`
        from an older worker, when the connections of `recorded` count.
    recorded
        The files the target recorded as read inside 30 days of its watermark, from the
        worker; `None` when the worker sent none: `files.read` then reads strictly after
        ``since`` as before.
    target_watermark
        The target's watermark when it is a time (a naive one is UTC); seeds a connection
        without records and lets the server prune.
    files_read
        What the `files.read` calls with ``since=`` read or took as read, in order.
    record_horizon
        Set by the worker when it loaded only the newest `MAX_FILES_READ` files of the record:
        the oldest one's time. `files.read` takes every file at or before it as read.
    """

    client: Client
    environment: str
    asset: str
    declared: frozenset[str]
    connections: Mapping[str, Mapping[str, object]]
    secrets: set[str] = field(default_factory=set)
    recorded: frozenset[ReadFile] | None = None
    target_watermark: dt.datetime | None = None
    files_read: list[ReadFile] = field(default_factory=list)
    record_horizon: dt.datetime | None = None
    recorded_connections: frozenset[str] | None = None


_state: _TaskState | None = None


def current() -> _TaskState:
    """Return the running attempt's state.

    Returns
    -------
    _TaskState
        The state the harness set.

    Raises
    ------
    RuntimeError
        Outside a running task.
    """
    if _state is None:
        raise RuntimeError(
            "lakana.ingest reads connections only inside a Lakana task (a run of the deployed "
            "asset)"
        )
    return _state


# The copy of `lakana_core.secrets.forms.secret_forms`: the SDK cannot import lakana_core, and
# `lakana-worker/tests/test_secret_forms_parity.py` keeps the two the same.
# jscpd:ignore-start
def secret_forms(value: str) -> set[str]:
    """List the forms text can carry a credential in.

    The same list as `lakana_core.secrets.forms.secret_forms`, which the worker hides in the log
    it captures (the SDK cannot import that package; a test compares the two).

    Parameters
    ----------
    value
        The credential.

    Returns
    -------
    set of str
        The value itself; as it appears inside a Python `repr` (either quote), a JSON string
        (with and without ``ensure_ascii``), a URL (`quote` with its default ``/`` kept and with
        nothing kept, `quote_plus`) and the `repr` of its UTF-8 bytes; and each JSON form again
        inside a `repr`, the `repr` of its bytes and a second JSON string (a JSON body printed
        as bytes, in a dict or encoded twice). Forms shorter than `MIN_SECRET_CHARS` are left
        out.

    Raises
    ------
    UnicodeEncodeError
        For a value that cannot be written as UTF-8 (a lone surrogate); no form of it could be
        found in a log.
    """
    ascii_json = json.dumps(value)[1:-1]
    text_json = json.dumps(value, ensure_ascii=False)[1:-1]
    found = {value, ascii_json, text_json, quote(value), quote(value, safe=""), quote_plus(value)}
    for text in (value, ascii_json, text_json):
        # Inside a repr, with either quote: a repr picks `"` for text that holds a `'` alone.
        found |= {repr(text)[1:-1], repr(text + "\"'")[1:-4]}
        data = text.encode()
        found |= {repr(data)[2:-1], repr(data + b"\"'")[2:-4]}
    for text in (ascii_json, text_json):
        found |= {json.dumps(text)[1:-1], json.dumps(text, ensure_ascii=False)[1:-1]}
    return {form for form in found if len(form) >= MIN_SECRET_CHARS}
    # jscpd:ignore-end


def _line(value: dict[str, object]) -> bytes:
    """Encode one line of the record, as `lakana_core.catalog.files_read.entry_bytes` counts it.

    Parameters
    ----------
    value
        The header or a file's JSON.

    Returns
    -------
    bytes
        Unescaped UTF-8 JSON and a newline: a record of long non-ASCII names stays small.
    """
    return json.dumps(value, ensure_ascii=False).encode() + b"\n"


def files_read_payload() -> bytes | None:
    """Encode the record of the files this attempt's `files.read` calls read, for the upload.

    Returns
    -------
    bytes or None
        Newline-delimited JSON, as `lakana_core.catalog.files_read` parses it: the header
        ``{"version": 1, "watermark": ...}`` (the target's watermark in UTC, or null), then
        each file once, one per line: the newest at most `MAX_FILES_READ` less
        `CONNECTION_RESERVE` whose lines fit in `RECORD_BUDGET_BYTES`, and the newest of every
        other connection read; `None` outside a task or when no call with ``since=`` read or
        reported a file.
    """
    if _state is None or not _state.files_read:
        return None
    mark = _state.target_watermark
    if mark is not None:
        mark = mark.replace(tzinfo=dt.UTC) if mark.tzinfo is None else mark.astimezone(dt.UTC)
    files = list(dict.fromkeys(_state.files_read))
    encoded = {file: _line(file.to_json()) for file in files}
    kept: set[ReadFile] = set()
    total = 0
    newest = sorted(files, key=lambda f: f.modified, reverse=True)
    for file in newest:
        # Never refuse a run for reading much: keep the newest that the record takes. The
        # worker's next load is then full, and its horizon takes the older ones as read.
        if (
            len(kept) == MAX_FILES_READ - CONNECTION_RESERVE
            or total + len(encoded[file]) > RECORD_BUDGET_BYTES
        ):
            break
        kept.add(file)
        total += len(encoded[file])
    # A connection trimmed out entirely keeps its newest file, in the room left below the
    # server's caps, so that the next run does not take it as new (and seed it).
    named = {file.connection for file in kept}
    for file in newest:
        if file.connection not in named:
            named.add(file.connection)
            kept.add(file)
    if len(kept) < len(files):
        print(
            f"files.read: this run read {len(files)} files with since=; Lakana records the newest "
            f"{len(kept)} (at most {MAX_FILES_READ} files and {RECORD_BUDGET_BYTES // 2**20} MiB "
            "of names), and the next run takes the older ones as read; see 'A busy table'",
            file=sys.stderr,
        )
    head = _line({"version": 1, "watermark": None if mark is None else mark.isoformat()})
    return head + b"".join(encoded[file] for file in files if file in kept)


def register_secret(value: str) -> None:
    """Have the harness mask a value, in each of its `secret_forms`, in every text it writes.

    Parameters
    ----------
    value
        A credential; values shorter than `MIN_SECRET_CHARS` are ignored (masking them would
        mask ordinary text), and so is every value outside a running task.
    """
    if _state is not None and len(value) >= MIN_SECRET_CHARS:
        _state.secrets.update(secret_forms(value))


def _set(state: _TaskState | None) -> None:
    """Set or clear the state (the harness only).

    Parameters
    ----------
    state
        The state, or `None` once the attempt has written its record.
    """
    global _state
    _state = state
