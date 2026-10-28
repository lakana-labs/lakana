"""Read a SQL database through a connection the asset declares, inside a Lakana task.

Example::

    import datetime as dt

    from lakana import asset
    from lakana.ingest import sql


    @asset(connections=["crm-db"], write="append", watermark="updated_at")
    def customers(ctx):
        return sql.read(
            "crm-db",
            "SELECT id, name, updated_at FROM customers WHERE updated_at > :since",
            params={"since": ctx.watermark(default=dt.datetime(1970, 1, 1, tzinfo=dt.UTC))},
        )

`sql.read` returns a `pyarrow.Table`. It reads ``postgresql`` and ``sqlite-file`` connections
through the ADBC drivers and ``mariadb`` connections (MariaDB and MySQL) through PyMySQL. Values
are bound to ``:name`` placeholders, never written into the asset's SQL. Every read runs in a
read-only transaction or on a read-only file; MariaDB commits before DDL, so it gets only a
statement that starts with ``SELECT``, ``WITH``, ``VALUES`` or ``TABLE``. Under the Docker
runner a database is reached through the task's relay, while TLS checks the certificate for the
connection's real host name.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import decimal
import math
import os
import re
import socket
import ssl
import stat
import threading
import time
import types
from collections.abc import Generator, Iterable, Mapping
from typing import TYPE_CHECKING
from urllib.parse import quote

from lakana.ingest import _check, _directory
from lakana.ingest._connections import FetchedConnection, fetch
from lakana.ingest._errors import (
    CapExceeded,
    SourceError,
    SourceRefused,
    UsageError,
    missing_secret,
    safe_name,
    task_environment,
)
from lakana.ingest._params import code_text, translate
from lakana.ingest._stores import without_variables
from lakana.ingest._types import mariadb_batch, mariadb_schema, normalize_postgres, postgres_schema
from lakana.ingest.files import DEFAULT_MAX_BYTES, DEFAULT_MAX_ROWS, _positive

if TYPE_CHECKING:
    import pyarrow as pa
    from adbc_driver_manager.dbapi import Connection as AdbcConnection

KINDS = ("postgresql", "mariadb", "sqlite-file")
"""The connection kinds `read` reads."""
CONNECT_SECONDS = 10
"""How long a connection to the database may take."""
BATCH_ROWS = 10_000
"""Rows fetched at a time from MariaDB."""
BATCH_BYTES = 16 * 1024 * 1024
"""The size ADBC aims at for one batch of a PostgreSQL result read without parameters."""
_OPEN_WINDOW = threading.Lock()
"""Held while one read walks to its file, lists `/dev/fd`, lets SQLite open the file and lists
`/dev/fd` again: a read on another thread would otherwise open its walker's descriptor or its own
SQLite file inside this window, and the comparison would count it as a file SQLite opened instead
of the checked one. Only that window is held, never the query."""
_OPEN_ATTEMPTS = 3
"""How often a SQLite file is opened before a mismatch with the checked file is refused."""
_OPEN_BACKOFF_SECONDS = 0.05
"""The wait before the second attempt, doubled before the third."""
_SERVICE_VARIABLES = ("PGSERVICE", "PGSERVICEFILE", "PGSYSCONFDIR")
"""What would make libpq fill unset keywords from a service file (``~/.pg_service.conf``); its
bundled build has no ``servicefile`` keyword to point elsewhere, so they are out while it
connects."""
_MESSAGE = 1000
"""The most characters of a database's own message kept in an error."""
_OTHER_HELPER = {
    "http": "files.read or rest.read",
    "s3": "files.read",
    "gcs": "files.read",
    "azure": "files.read",
    "directory": "files.read",
    "tcp": "connections.get with the team's own driver",
}
_READING = ("SELECT", "WITH", "VALUES", "TABLE")
"""The first keywords of a MariaDB statement `read` sends: each one only reads."""
_KNOWN_MODES = frozenset(
    {
        "",
        "ALLOW_INVALID_DATES",
        "ANSI",
        "ANSI_QUOTES",
        "EMPTY_STRING_IS_NULL",
        "ERROR_FOR_DIVISION_BY_ZERO",
        "HIGH_NOT_PRECEDENCE",
        "IGNORE_BAD_TABLE_OPTIONS",
        "IGNORE_SPACE",
        "MYSQL323",
        "MYSQL40",
        "NO_AUTO_CREATE_USER",
        "NO_AUTO_VALUE_ON_ZERO",
        "NO_BACKSLASH_ESCAPES",
        "NO_DIR_IN_CREATE",
        "NO_ENGINE_SUBSTITUTION",
        "NO_FIELD_OPTIONS",
        "NO_KEY_OPTIONS",
        "NO_TABLE_OPTIONS",
        "NO_UNSIGNED_SUBTRACTION",
        "NO_ZERO_DATE",
        "NO_ZERO_IN_DATE",
        "ONLY_FULL_GROUP_BY",
        "PAD_CHAR_TO_FULL_LENGTH",
        "PIPES_AS_CONCAT",
        "REAL_AS_FLOAT",
        "SIMULTANEOUS_ASSIGNMENT",
        "STRICT_ALL_TABLES",
        "STRICT_TRANS_TABLES",
        "TIME_ROUND_FRACTIONAL",
        "TIME_TRUNCATE_FRACTIONAL",
        "TRADITIONAL",
    }
)
"""The sql_mode flags whose effect on how quotes and comments read the tokenizer knows: none, or
ANSI_QUOTES and NO_BACKSLASH_ESCAPES, which it follows. Any other (MSSQL's `[...]` names,
ORACLE's own parser) is refused."""
_NEEDS_ASSETS = "sql.read needs the assets extra: add lakana[assets] to the project's dependencies"
_NARROW = "narrow the query (add a WHERE clause or a LIMIT)"
_RELAY_CLOSED = (
    "`{name}`: the connection through the task's relay closed before {product} answered; the "
    "task's log has a `lakana-relay:` line that says why (an address inside the Lakana stack or "
    "on the Docker host is never reachable)"
)


def _said(text: str, secret: Mapping[str, str] | None) -> str:
    """Keep a database's own message: secrets masked, control characters escaped, bounded.

    Parameters
    ----------
    text
        The driver's message.
    secret
        The connection's secret, whose values are masked.

    Returns
    -------
    str
        One line of at most 1,000 characters.
    """
    for value in sorted((v for v in (secret or {}).values() if v), key=len, reverse=True):
        text = text.replace(value, "***")
    return safe_name(" ".join(text.split()))[:_MESSAGE]


def _authentication(name: str, product: str, said: str) -> SourceRefused:
    """Make the fixed authentication error.

    Parameters
    ----------
    name
        The connection.
    product
        ``PostgreSQL`` or ``MariaDB``.
    said
        The server's message, already made safe.

    Returns
    -------
    SourceRefused
        Naming the command that tests the secret.
    """
    return SourceRefused(
        f"`{name}`: authentication failed ({product} said: {said}). Check the secret with "
        f"`lakana connection test {name} --env {task_environment()}`."
    )


def _untrusted(name: str, host: str) -> SourceRefused:
    """Make the fixed TLS error.

    Parameters
    ----------
    name
        The connection.
    host
        The name the certificate was checked for.

    Returns
    -------
    SourceRefused
        Naming the command that gives the connection its CA.
    """
    return SourceRefused(
        f"`{name}`: the server's certificate is not trusted for `{host}`. Give the connection "
        f"your CA: `lakana connection update {name} --env {task_environment()} --ca-file ca.pem`."
    )


def _first_keyword(code: str) -> str:
    """Name the first keyword of a statement's code, after semicolons and parentheses.

    Parameters
    ----------
    code
        `code_text` of the query: its literals and comments are blanks.

    Returns
    -------
    str
        The keyword in capitals, or ``""`` when the code starts with none.
    """
    found = re.match(r"[\s;(]*([A-Za-z_]+)", code)
    return found.group(1).upper() if found else ""


def _relay_ip(name: str) -> str:
    """Resolve the task's relay.

    Parameters
    ----------
    name
        The connection, for the message.

    Returns
    -------
    str
        The relay's address.

    Raises
    ------
    SourceError
        When ``lakana-relay`` does not resolve (retried: the relay may still be starting).
    """
    try:
        return socket.gethostbyname(_check.RELAY_HOST)
    except OSError:
        raise SourceError(
            f"`{name}`: the task's relay ({_check.RELAY_HOST}) does not resolve; the task's log "
            "says why the launcher did not start it"
        ) from None


def _postgres_error(
    conn: FetchedConnection, exc: Exception, host: str, *, connecting: bool = False
) -> Exception:
    """Choose the error for what the PostgreSQL driver raised.

    A refused login is recognised by libpq's English words (ADBC gives no SQLSTATE at connect);
    one worded in another language is a retried `SourceError` whose message says what to check.

    Parameters
    ----------
    conn
        The connection.
    exc
        The driver's error.
    host
        The name the certificate is checked for.
    connecting
        Whether the error ended the login.

    Returns
    -------
    Exception
        The error to raise.
    """
    said = _said(str(exc), conn.secret)
    state = getattr(exc, "sqlstate", None) or ""
    text = str(exc)
    if (
        "password authentication failed" in text
        or "no pg_hba.conf entry" in text
        or state.startswith("28")
    ):
        return _authentication(conn.name, "PostgreSQL", said)
    if "certificate verify failed" in text or "does not match host name" in text:
        return _untrusted(conn.name, host)
    if "server closed the connection unexpectedly" in text and "relay_port" in conn.routing:
        return SourceError(_RELAY_CLOSED.format(name=conn.name, product="PostgreSQL"))
    if state == "25006":
        return UsageError(
            f"`{conn.name}`: sql.read runs every query in a read-only transaction, and this one "
            f"writes (PostgreSQL said: {said})"
        )
    if state == "42501":
        return SourceRefused(
            f"`{conn.name}`: the connection's user may not read this (PostgreSQL said: {said}); "
            "grant it SELECT, or read another table"
        )
    if state == "42883" and "operator does not exist" in text:
        return UsageError(
            f"`{conn.name}`: the query failed (PostgreSQL said: {said}). A value of params is "
            "compared with a column of another type (PostgreSQL receives a str as text): pass "
            "a value of the column's type, such as a datetime for a timestamp, or cast the "
            "placeholder, such as :since::timestamptz"
        )
    if state.startswith(("42", "0A", "22", "21")) or "does not exist" in text:
        return UsageError(f"`{conn.name}`: the query failed (PostgreSQL said: {said})")
    if connecting:
        return SourceError(f"`{conn.name}`: {said}; {_check.PG_LOGIN_HINT}")
    return SourceError(f"`{conn.name}`: {said}")


def _read_postgres(
    conn: FetchedConnection, query: str, params: Mapping[str, object] | None, caps: tuple[int, int]
) -> pa.Table:
    """Read a ``postgresql`` connection through ADBC.

    Parameters
    ----------
    conn
        The connection.
    query
        The asset's SQL.
    params
        The values of its placeholders.
    caps
        ``max_rows`` and ``max_bytes``.

    Returns
    -------
    pyarrow.Table
        The result.

    Raises
    ------
    UsageError
        Without the assets extra or a password, or for a query that fails or writes.
    SourceRefused
        When PostgreSQL refuses the password or its certificate is not trusted.
    SourceError
        When PostgreSQL fails or cannot be reached.
    """
    sql, values = translate(query, params, style="numeric")
    try:
        import adbc_driver_postgresql.dbapi as pg
    except ImportError:
        raise UsageError(_NEEDS_ASSETS) from None
    password = (conn.secret or {}).get("password")
    if not password:
        error = missing_secret(conn.name, "password", "--password-stdin")
        raise error
    host = str(conn.address["host"])
    relay_port = conn.routing.get("relay_port")
    parts = {"host": host, "port": str(conn.address["port"])}
    if isinstance(relay_port, int):
        parts.update(hostaddr=_relay_ip(conn.name), port=str(relay_port))
    tls = str(conn.settings.get("tls", "verify-full"))
    parts.update(
        dbname=str(conn.address["database"]),
        user=str(conn.settings["user"]),
        password=password,
        sslmode=tls,
        connect_timeout=str(CONNECT_SECONDS),
        options="-c default_transaction_read_only=on",
    )
    for key, value in parts.items():
        if "\x00" in value:
            raise UsageError(
                f"the `{key}` of `{conn.name}` holds a NUL character, which a connection string "
                f"cannot carry; set it again with `lakana connection update {conn.name}`"
            )
    own = conn.settings.get("ca_certificate")
    try:
        with _check.root_certificates(own if isinstance(own, str) else None) as roots:
            if tls == "verify-full":
                parts["sslrootcert"] = roots
            with without_variables(_SERVICE_VARIABLES):
                database = pg.connect(_check.conninfo(parts))
    except _check.BundleUnreadable:
        raise UsageError(
            f"the trust bundle `{os.environ.get('LAKANA_CA_BUNDLE')}` (LAKANA_CA_BUNDLE) cannot "
            "be read; check the worker's LAKANA_RUN__CA_BUNDLE"
        ) from None
    except pg.Error as exc:
        failure = _postgres_error(conn, exc, host, connecting=True)
        raise failure from None
    try:
        cursor = database.cursor()
        try:
            cursor.adbc_statement.set_options(
                **{"adbc.postgresql.batch_size_hint_bytes": str(BATCH_BYTES)}
            )
            # Without parameters ADBC streams the result (COPY); with them it is one batch.
            cursor.execute(sql, values) if values else cursor.execute(sql)
            reader = cursor.fetch_record_batch()
            schema = postgres_schema(reader.schema, conn.name)
            return _collect(
                (normalize_postgres(b, schema, conn.name) for b in reader), schema, conn, caps
            )
        except pg.Error as exc:
            failure = _postgres_error(conn, exc, host)
            raise failure from None
    finally:
        database.close()


def _collect(
    batches: Iterable[pa.RecordBatch],
    schema: pa.Schema,
    conn: FetchedConnection,
    caps: tuple[int, int],
) -> pa.Table:
    """Gather batches into a table, stopping at the caps.

    Parameters
    ----------
    batches
        An iterator of batches in `schema`.
    schema
        The table's schema.
    conn
        The connection, for messages.
    caps
        ``max_rows`` and ``max_bytes``.

    Returns
    -------
    pyarrow.Table
        The table.

    Raises
    ------
    CapExceeded
        Past either cap, naming it and how to narrow the query.
    """
    import pyarrow as pa

    max_rows, max_bytes = caps
    kept: list[pa.RecordBatch] = []
    rows = size = 0
    for batch in batches:
        rows, size = rows + batch.num_rows, size + batch.nbytes
        if rows > max_rows:
            raise CapExceeded(
                f"`{conn.name}`: the result has more than max_rows ({max_rows}) rows; {_NARROW}, "
                "or raise max_rows"
            )
        if size > max_bytes:
            raise CapExceeded(
                f"`{conn.name}`: the result is larger than max_bytes ({max_bytes} bytes); "
                f"{_NARROW}, or raise max_bytes"
            )
        kept.append(batch)
    return pa.Table.from_batches(kept, schema=schema)


def _read_sqlite(
    conn: FetchedConnection, query: str, params: Mapping[str, object] | None, caps: tuple[int, int]
) -> pa.Table:
    """Read a ``sqlite-file`` connection through ADBC, read-only.

    Parameters
    ----------
    conn
        The connection.
    query
        The asset's SQL.
    params
        The values of its placeholders.
    caps
        ``max_rows`` and ``max_bytes``.

    Returns
    -------
    pyarrow.Table
        The result.

    Raises
    ------
    UsageError
        When the task description has no root for the connection, or the query fails.
    SourceError
        When the driver cannot open the file, or it opened another file than the one checked
        (retried: a file replaced in place passes next time).
    """
    _plain_values(params, conn.kind)
    sql, values = translate(query, params, style="qmark")
    if _first_keyword(code_text(query, style="qmark")) in ("ATTACH", "DETACH"):
        raise UsageError(
            f"`{conn.name}`: sql.read refuses ATTACH and DETACH: SQLite creates the file an "
            "ATTACH names even on a read-only connection; read one database per connection"
        )
    root_dir, path = conn.routing.get("root_dir"), conn.routing.get("path")
    if not isinstance(root_dir, str) or not isinstance(path, str):
        raise UsageError(
            f"the task description has no ingestion root for `{conn.name}`; check that the "
            "worker runs the server's version of Lakana"
        )
    try:
        import adbc_driver_sqlite.dbapi as lite
    except ImportError:
        raise UsageError(_NEEDS_ASSETS) from None
    database = _open_sqlite(conn, lite, root_dir, path)
    try:
        cursor = database.cursor()
        try:
            cursor.execute(sql, values) if values else cursor.execute(sql)
            reader = cursor.fetch_record_batch()
            schema = reader.schema
            return _collect(iter(reader), schema, conn, caps)
        except (lite.Error, OSError) as exc:
            failure = _sqlite_error(conn, exc, schema if "schema" in locals() else None)
            raise failure from None
    finally:
        database.close()


def _walked(conn: FetchedConnection, root_dir: str, path: str) -> int:
    """Open a ``sqlite-file`` connection's file through the walker.

    Parameters
    ----------
    conn
        The connection.
    root_dir
        The ingestion root.
    path
        The file's path below it.

    Returns
    -------
    int
        The walker's descriptor of the regular file; the caller closes it.

    Raises
    ------
    RootUnavailable
        When this worker does not have the root.
    DirectoryError
        When the path holds a link, is missing, or is not a regular file.
    """
    try:
        return _directory.regular_file(root_dir, path)
    except _directory.RootUnavailable:
        root = str(conn.routing.get("root", conn.address.get("root", "")))
        missing = _directory.RootUnavailable(
            _check.MISSING_ROOT.format(connection=conn.name, root=root)
        )
        raise missing from None
    except _directory.DirectoryError as exc:
        # The same class, so its `retryable` survives (an I/O error may pass).
        named = type(exc)(f"`{conn.name}`: {exc}")
        raise named from None


def _identity(fd: int) -> tuple[int, int] | None:
    """Name the regular file a descriptor has open.

    Parameters
    ----------
    fd
        The descriptor.

    Returns
    -------
    tuple or None
        Its device and inode; `None` for a number not open, or not a regular file.
    """
    try:
        seen = os.fstat(fd)
    except OSError:
        return None
    return (seen.st_dev, seen.st_ino) if stat.S_ISREG(seen.st_mode) else None


def _open_files() -> dict[int, tuple[int, int]]:
    """Identify the regular files this process has open.

    Returns
    -------
    dict
        Descriptor to the file's device and inode (`_identity`).

    Raises
    ------
    UsageError
        When ``/dev/fd`` cannot be listed.
    """
    try:
        names = os.listdir("/dev/fd")
    except OSError:
        raise UsageError(
            "sql.read cannot list this process's open files (/dev/fd), so it cannot check which "
            "file SQLite opened; run the worker on Linux or macOS with /dev/fd"
        ) from None
    found = {int(name): _identity(int(name)) for name in names}
    return {fd: ident for fd, ident in found.items() if ident is not None}


def _opened_only(
    expected: tuple[int, int],
    handle: int,
    before: dict[int, tuple[int, int]],
    after: dict[int, tuple[int, int]],
) -> bool:
    """Say whether SQLite opened the checked file and nothing else.

    Parameters
    ----------
    expected
        The device and inode of the walker's descriptor.
    handle
        The walker's descriptor.
    before
        `_open_files` before SQLite opened the database.
    after
        `_open_files` after.

    Returns
    -------
    bool
        True when a descriptor besides the walker's names the checked file, and every other
        descriptor that is new, or now names another file (a number closed meanwhile and
        reused), was closed again by now: another thread's passing read (SQLite keeps its file
        open). A descriptor of the same file left by an earlier read and closed by the garbage
        collector meanwhile can hand SQLite its number unchanged, so that counts too.
    """
    if expected not in {ident for fd, ident in after.items() if fd != handle}:
        return False
    changed = (fd for fd, ident in after.items() if before.get(fd) != ident != expected)
    return all(_identity(fd) != after[fd] for fd in changed)


def _open_sqlite(
    conn: FetchedConnection, lite: types.ModuleType, root_dir: str, path: str
) -> AdbcConnection:
    """Open the file the walker finds, and refuse another file SQLite opened instead.

    SQLite opens by path and resolves links itself (given ``file:/proc/self/fd/N`` it opens the
    name the link holds), so the walker's descriptor cannot be handed over. The regular files
    this process opened while SQLite opened the database are compared with it instead, and it
    stays open until then, so its inode is not reused. A mismatch is tried again, up to
    `_OPEN_ATTEMPTS` times: a file replaced in place, or another thread's file, passes then.

    Parameters
    ----------
    conn
        The connection.
    lite
        ``adbc_driver_sqlite.dbapi``.
    root_dir
        The ingestion root.
    path
        The file's path below it.

    Returns
    -------
    adbc_driver_manager.dbapi.Connection
        The database, read-only.

    Raises
    ------
    SourceError
        When the driver cannot open the file, or SQLite opened another file each time.
    UsageError
        When this process cannot list its open files.
    """
    # Always a file: URI, so mode=ro applies (a plain path would be created read-write).
    uri = "file:" + quote(f"{root_dir.rstrip('/')}/{path.strip('/')}") + "?mode=ro"
    for attempt in range(_OPEN_ATTEMPTS):
        if attempt:
            # Another thread's file activity comes in bursts (a scan of /proc, a batch of
            # writes): the next attempt waits it out rather than meet it again.
            time.sleep(_OPEN_BACKOFF_SECONDS * attempt)
        with _OPEN_WINDOW:
            handle = _walked(conn, root_dir, path)
            try:
                seen = os.fstat(handle)
                before = _open_files()
                try:
                    database = lite.connect(uri)
                except lite.Error as exc:
                    raise SourceError(f"`{conn.name}`: {_said(str(exc), None)}") from None
                after = _open_files()
                matched = _opened_only((seen.st_dev, seen.st_ino), handle, before, after)
            finally:
                os.close(handle)
        if matched:
            return database
        database.close()
    raise SourceError(
        f"`{conn.name}`: {safe_name(repr(path.strip('/')))} changed while SQLite opened it "
        "(replaced, or swapped for a link); sql.read reads only the file it checked"
    )


def _sqlite_error(conn: FetchedConnection, exc: Exception, schema: pa.Schema | None) -> Exception:
    """Choose the error for what the SQLite driver raised.

    Parameters
    ----------
    conn
        The connection.
    exc
        The driver's error.
    schema
        The result's schema when reading had begun.

    Returns
    -------
    Exception
        The error to raise.
    """
    text = str(exc)
    said = _said(text, None)
    found = re.search(r"Type mismatch in column (\d+)", text)
    if found and schema is not None and int(found.group(1)) < len(schema):
        column = safe_name(schema.names[int(found.group(1))])
        return UsageError(
            f"`{conn.name}`: column `{column}` changes type within the result (SQLite keeps any "
            f"type in any column); cast it in the query, for example CAST({column} AS TEXT)"
        )
    if "readonly database" in text:
        return UsageError(
            f"`{conn.name}`: SQLite could not read the file read-only ({said}). sql.read never "
            "writes to the database: a query that writes fails, and so does a database in WAL "
            "mode or with a hot journal in a directory the worker cannot write; give the "
            "connection a copy made with `PRAGMA journal_mode=DELETE`"
        )
    if "syntax error" in text or "no such" in text:
        return UsageError(f"`{conn.name}`: the query failed (SQLite said: {said})")
    return SourceError(f"`{conn.name}`: {said}")


def _mariadb_context(conn: FetchedConnection) -> ssl.SSLContext | None:
    """Build the TLS context of a MariaDB connection as its ``tls`` setting says.

    Parameters
    ----------
    conn
        The connection.

    Returns
    -------
    ssl.SSLContext or None
        `_check.trust_context` for ``verify-full`` and ``require`` (``require`` checks neither
        chain nor name), `None` for ``disable``.

    Raises
    ------
    UsageError
        When the trust bundle cannot be read.
    """
    tls = str(conn.settings.get("tls", "verify-full"))
    if tls == "disable":
        return None
    own = conn.settings.get("ca_certificate")
    try:
        return _check.trust_context(own if isinstance(own, str) else None, verify=tls != "require")
    except (OSError, ssl.SSLError):
        raise UsageError(
            f"the trust bundle `{os.environ.get('LAKANA_CA_BUNDLE')}` (LAKANA_CA_BUNDLE) cannot "
            "be read; check the worker's LAKANA_RUN__CA_BUNDLE"
        ) from None


def _utc_naive(value: object) -> object:
    """Bind an aware datetime as its UTC wall time: MariaDB's DATETIME has no zone.

    Parameters
    ----------
    value
        A parameter value.

    Returns
    -------
    object
        A naive UTC datetime for an aware one; the value otherwise.
    """
    if isinstance(value, dt.datetime) and value.tzinfo is not None:
        return value.astimezone(dt.UTC).replace(tzinfo=None)
    return value


_PLAIN_TYPES = (str, int, float, bool, decimal.Decimal, dt.date, dt.datetime, dt.time, bytes)
"""The parameter types PyMySQL escapes by the server's mode (or as digits, dates or hex)."""
_TRANSIENT = frozenset(
    {1037, 1038, 1040, 1041, 1053, 1158, 1159, 1160, 1161, 1203, 1205, 1213, 1226, 1317}
    | {1927, 1969}
)
"""MariaDB server errors that may pass: memory, shutdown, network, limits, locks, kills, time."""


def _plain_values(params: Mapping[str, object] | None, kind: str | None = None) -> None:
    """Refuse a parameter that is not a plain, finite value the driver binds faithfully.

    PyMySQL escapes a list, a set, a path or another object with backslashes, which a MariaDB
    server with ``NO_BACKSLASH_ESCAPES`` does not read as escapes: the value would end its
    literal. The ADBC drivers fail on such values, and SQLite binds a NaN as NULL and cannot bind
    a Decimal or a time at all. One rule for every kind keeps a value's meaning the same.

    Parameters
    ----------
    params
        The values.
    kind
        The connection's kind, once known: ``sqlite-file`` refuses Decimal, time and datetime
        too.

    Raises
    ------
    UsageError
        For a name that is not text, a value of another type, or a number that is not finite.
    """
    for name, value in (params or {}).items():
        if not isinstance(name, str):
            raise UsageError(
                f"params= keys are placeholder names (text), such as 'since'; not {name!r}"
            )
        if value is None:
            continue  # translate names the default to give
        if type(value) not in _PLAIN_TYPES:
            raise UsageError(
                f"parameter `{name}` is a {type(value).__name__}; a value is a str, int, float, "
                "bool, Decimal, date, datetime, time or bytes: give each value its own "
                "placeholder"
            )
        finite = value.is_finite() if isinstance(value, decimal.Decimal) else True
        if not finite or (isinstance(value, float) and not math.isfinite(value)):
            raise UsageError(
                f"parameter `{name}` is not a finite number; a database compares no NaN or "
                "infinity the way Python does"
            )
        if kind == "sqlite-file" and isinstance(value, (decimal.Decimal, dt.time)):
            raise UsageError(
                f"parameter `{name}` is a {type(value).__name__}, which SQLite cannot bind; "
                "pass a str or a float"
            )
        if kind == "sqlite-file" and type(value) is dt.datetime:
            raise UsageError(
                f"parameter `{name}` is a datetime, which SQLite receives as text such as "
                "2026-01-01T06:00:00.000000 and compares as text, while its own timestamps read "
                "2026-01-01 06:00:00: rows would be skipped. Pass a str in the column's own "
                f"format, such as \"2026-01-01 06:00:00\" (value.isoformat(sep=' ')), or compare "
                f"with datetime(:{name}) in the SQL"
            )


def _mariadb_error(conn: FetchedConnection, exc: Exception, host: str) -> Exception:
    """Choose the error for what PyMySQL raised.

    Parameters
    ----------
    conn
        The connection.
    exc
        PyMySQL's error (``args`` is ``(code, message)`` for a server error).
    host
        The name the certificate is checked for.

    Returns
    -------
    Exception
        The error to raise.
    """
    code = exc.args[0] if exc.args and isinstance(exc.args[0], int) else None
    said = _said(str(exc.args[1] if len(exc.args) > 1 else exc), conn.secret)
    if isinstance(exc, ssl.SSLCertVerificationError) or isinstance(
        getattr(exc, "original_exception", None), ssl.SSLCertVerificationError
    ):
        return _untrusted(conn.name, host)
    if code in (1045, 1044, 1698):  # access denied (user, database)
        return _authentication(conn.name, "MariaDB", said)
    if code == 1792:  # cannot execute statement in a READ ONLY transaction
        return UsageError(
            f"`{conn.name}`: sql.read runs every query in a read-only transaction, and this one "
            f"writes (MariaDB said: {said})"
        )
    if code == 1142 or code == 1143:  # command or column denied
        return SourceRefused(
            f"`{conn.name}`: the connection's user may not read this (MariaDB said: {said}); "
            "grant it SELECT, or read another table"
        )
    if code == 2026:  # the client requires TLS; the server offers none
        return SourceRefused(
            f"`{conn.name}`: the server offers no TLS, and the connection's tls is "
            f"{conn.settings.get('tls', 'verify-full')}. Enable TLS on the server, or, on a "
            f"network you trust, run `lakana connection update {conn.name} --env "
            f"{task_environment()} --tls disable`."
        )
    if code == 3159:  # ER_SECURE_TRANSPORT_REQUIRED, the same number on MariaDB and MySQL
        return SourceRefused(
            f"`{conn.name}`: the server requires TLS (MariaDB said: {said}). Run `lakana "
            f"connection update {conn.name} --env {task_environment()} --tls verify-full`."
        )
    if code is not None and 1000 <= code < 2000 and code not in _TRANSIENT:
        return UsageError(f"`{conn.name}`: the query failed (MariaDB said: {said})")
    if code in (2013, 2006) and "relay_port" in conn.routing:
        return SourceError(_RELAY_CLOSED.format(name=conn.name, product="MariaDB"))
    if isinstance(exc, RuntimeError) and "cryptography" in str(exc):
        return UsageError(
            f"`{conn.name}`: the server asks for caching_sha2_password over a connection without "
            "TLS, which needs the cryptography package; use tls: verify-full, or add "
            "cryptography to the project's dependencies"
        )
    return SourceError(f"`{conn.name}`: {said}")


def _mariadb_statement(
    conn: FetchedConnection,
    query: str,
    params: Mapping[str, object] | None,
    mode: str,
    status: int,
) -> tuple[str, dict[str, object]]:
    """Check a MariaDB query against the session's sql_mode and rewrite its placeholders.

    ``START TRANSACTION READ ONLY`` does not stop DDL: MariaDB and MySQL commit before a
    ``DROP``, ``TRUNCATE`` or ``CREATE`` and run it outside the transaction. So only a statement
    that starts with a reading keyword is sent, and ``INTO OUTFILE`` (a file on the server) is
    refused.

    Parameters
    ----------
    conn
        The connection.
    query
        The asset's SQL.
    params
        The values of its placeholders.
    mode
        The session's ``@@sql_mode``.
    status
        PyMySQL's server status, whose ``NO_BACKSLASH_ESCAPES`` bit (512) decides how it escapes
        a str.

    Returns
    -------
    tuple
        The rewritten query and its values, an aware datetime as its UTC wall time.

    Raises
    ------
    UsageError
        For a sql_mode the tokenizer does not know, a statement that does not only read, or
        ``INTO OUTFILE``.
    SourceError
        When the sql_mode and the status disagree about backslashes.
    """
    flags = {flag.strip() for flag in mode.upper().split(",")}
    unknown = sorted(flags - _KNOWN_MODES)
    if unknown:
        raise UsageError(
            f"`{conn.name}`: the session's sql_mode has {', '.join(unknown)}, which sql.read does "
            "not know: it changes how MariaDB reads quotes or names, so a value could land "
            "where MariaDB reads code. Remove it from the server's or the user's sql_mode."
        )
    backslashes = "NO_BACKSLASH_ESCAPES" not in flags
    if backslashes == bool(status & 512):
        raise SourceError(
            f"`{conn.name}`: the session's sql_mode and the server's status disagree about "
            "backslash escapes (a proxy between the worker and MariaDB?); nothing was sent"
        )
    style: dict[str, bool] = {
        "backslash_escapes": backslashes,
        "ansi_quotes": "ANSI_QUOTES" in flags,
    }
    sql, values = translate(query, params, style="pyformat", **style)
    code = code_text(query, style="pyformat", **style)
    first = _first_keyword(code)
    if first not in _READING:
        raise UsageError(
            f"`{conn.name}`: sql.read runs one statement that reads, starting with SELECT, WITH, "
            f"VALUES or TABLE; this one starts with {first or 'no keyword'}. MariaDB commits "
            "before DDL, so a read-only transaction cannot stop it."
        )
    if re.search(r"\bINTO\s+(OUTFILE|DUMPFILE)\b", code, re.I):
        raise UsageError(
            f"`{conn.name}`: sql.read refuses SELECT ... INTO OUTFILE (or DUMPFILE): it writes a "
            "file on the server"
        )
    assert isinstance(values, dict)  # nosec B101 -- pyformat returns a mapping
    return sql, {key: _utc_naive(value) for key, value in values.items()}


def _read_mariadb(
    conn: FetchedConnection, query: str, params: Mapping[str, object] | None, caps: tuple[int, int]
) -> pa.Table:
    """Read a ``mariadb`` connection through PyMySQL, in a read-only transaction.

    Parameters
    ----------
    conn
        The connection.
    query
        The asset's SQL.
    params
        The values of its placeholders.
    caps
        ``max_rows`` and ``max_bytes``.

    Returns
    -------
    pyarrow.Table
        The result.

    Raises
    ------
    UsageError
        Without the assets extra or a password, for a parameter that is not a plain value, or
        for a query that fails, writes or returns no rows.
    SourceRefused
        When MariaDB refuses the password, its certificate is not trusted, or the connection's
        ``tls`` and the server disagree.
    SourceError
        When MariaDB fails or cannot be reached.
    """
    try:
        import pymysql
        import pymysql.cursors
    except ImportError:
        raise UsageError(_NEEDS_ASSETS) from None

    password = (conn.secret or {}).get("password")
    if not password:
        error = missing_secret(conn.name, "password", "--password-stdin")
        raise error
    host, port = str(conn.address["host"]), int(str(conn.address["port"]))
    context = _mariadb_context(conn)
    database = pymysql.connections.Connection(
        host=host,
        port=port,
        user=str(conn.settings["user"]),
        password=password,
        database=str(conn.address["database"]),
        ssl=context,
        ssl_disabled=context is None,
        local_infile=False,
        connect_timeout=CONNECT_SECONDS,
        init_command="SET time_zone = '+00:00'",
        charset="utf8mb4",
        defer_connect=True,
    )
    relay_port = conn.routing.get("relay_port")
    try:
        if isinstance(relay_port, int):
            sock = socket.create_connection((_relay_ip(conn.name), relay_port), CONNECT_SECONDS)
            database.connect(sock=sock)
        else:
            database.connect()
    except (pymysql.MySQLError, OSError, RuntimeError) as exc:
        failure = _mariadb_error(conn, exc, host)
        raise failure from None
    try:
        cursor = database.cursor(pymysql.cursors.SSCursor)
        try:
            probe = database.cursor()
            probe.execute("SET SESSION TRANSACTION READ ONLY")
            probe.execute("START TRANSACTION READ ONLY")
            # How quotes and backslashes read is the session's sql_mode (ANSI_QUOTES,
            # NO_BACKSLASH_ESCAPES), so the placeholders are translated once it is known. Read
            # inside the transaction: a proxy pins the server there.
            probe.execute("SELECT @@SESSION.sql_mode")
            (mode,) = probe.fetchone() or ("",)
            sql, bound = _mariadb_statement(conn, query, params, str(mode), database.server_status)
            # Always with values, even none: PyMySQL turns the doubled `%%` back into `%` only
            # when it formats the query.
            cursor.execute(sql, bound)
            if cursor.description is None:
                raise UsageError(
                    f"`{conn.name}`: sql.read needs a statement that returns rows, such as "
                    "SELECT; this one returns none (SET, DO, SELECT ... INTO)"
                )
            schema = mariadb_schema(cursor._result.fields, conn.name)

            def batches() -> Generator[pa.RecordBatch]:
                while rows := cursor.fetchmany(BATCH_ROWS):
                    yield mariadb_batch(rows, schema, conn.name)

            return _collect(batches(), schema, conn, caps)
        except (pymysql.MySQLError, OSError) as exc:
            failure = _mariadb_error(conn, exc, host)
            raise failure from None
    finally:
        # A result left unread (a cap, a refused type) would otherwise try to read its rest from
        # the closed connection when it is collected, and print a traceback to the task's log.
        with contextlib.suppress(AttributeError):  # no result yet: `_result` is None
            database._result.unbuffered_active = False  # ty: ignore[invalid-assignment]
        with contextlib.suppress(Exception):
            database.close()


def read(
    connection: str,
    query: str,
    *,
    params: Mapping[str, object] | None = None,
    max_rows: int = DEFAULT_MAX_ROWS,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> pa.Table:
    """Run one query on a connection the asset declares and return its result.

    Parameters
    ----------
    connection
        A connection the asset declares, of kind ``postgresql``, ``mariadb`` or
        ``sqlite-file``.
    query
        One SQL statement. Values go in ``:name`` placeholders, never into the text.
    params
        The value of each placeholder, such as ``{"since": ctx.watermark(default=...)}``. A
        `None` value is refused: comparing with NULL matches no row.
    max_rows
        The most rows read.
    max_bytes
        The most bytes of Arrow data read.

    Returns
    -------
    pyarrow.Table
        The result, its schema from the database's types.

    Raises
    ------
    UsageError
        For arguments or a connection of the wrong kind, a placeholder without a value, a query
        that fails or writes, a column type without a faithful Arrow form.
    SourceRefused
        When the database refuses the credential or its certificate is not trusted.
    SourceError
        When the database fails or cannot be reached; another attempt may end differently.
    CapExceeded
        Past `max_rows` or `max_bytes`.
    """
    _positive(max_rows=max_rows, max_bytes=max_bytes)
    if not isinstance(query, str) or not query.strip():
        raise UsageError("sql.read needs a query: one SQL statement, such as SELECT * FROM t")
    if params is not None and not isinstance(params, Mapping):
        raise UsageError(
            "params= takes a mapping of placeholder name to value, such as {'since': ...}, not "
            f"{type(params).__name__}"
        )
    _plain_values(params)
    conn = fetch(connection)
    if conn.kind not in KINDS:
        helper = _OTHER_HELPER.get(conn.kind)
        hint = f": use {helper}" if helper else ""
        raise UsageError(
            f"sql.read reads postgresql, mariadb and sqlite-file connections; `{conn.name}` is "
            f"{conn.kind}{hint}"
        )
    reader = {
        "postgresql": _read_postgres,
        "mariadb": _read_mariadb,
        "sqlite-file": _read_sqlite,
    }[conn.kind]
    return reader(conn, query, params, (max_rows, max_bytes))
