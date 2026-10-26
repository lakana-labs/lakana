"""Read a SQL database through a connection the asset declares, inside a Lakana task.

Example::

    from lakana import asset
    from lakana.ingest import sql


    @asset(connections=["crm-db"], write="append", watermark="updated_at")
    def customers(ctx):
        return sql.read(
            "crm-db",
            "SELECT id, name, updated_at FROM customers WHERE updated_at > :since",
            params={"since": ctx.watermark(default="1970-01-01")},
        )

`sql.read` returns a `pyarrow.Table`. It reads ``postgresql`` and ``sqlite-file`` connections
through the ADBC drivers. Values
are bound to ``:name`` placeholders, never written into the asset's SQL; every read runs in a
read-only transaction. Under the Docker runner a database is reached through the task's relay,
while TLS checks the certificate for the connection's real host name.
"""

from __future__ import annotations

import contextlib
import os
import socket
import tempfile
from collections.abc import Generator, Iterable, Mapping
from pathlib import Path
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
    task_environment,
)
from lakana.ingest._params import translate
from lakana.ingest._types import normalize_postgres, postgres_schema
from lakana.ingest.files import DEFAULT_MAX_BYTES, DEFAULT_MAX_ROWS, _positive

if TYPE_CHECKING:
    import pyarrow as pa

KINDS = ("postgresql", "sqlite-file")
"""The connection kinds `read` reads."""
CONNECT_SECONDS = 10
"""How long a connection to the database may take."""
BATCH_BYTES = 16 * 1024 * 1024
"""The size ADBC aims at for one batch of a PostgreSQL result read without parameters."""
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
    from lakana.ingest._errors import safe_name

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


@contextlib.contextmanager
def _root_certificates(conn: FetchedConnection) -> Generator[str]:
    """Hand libpq the certificates to trust: the trust bundle plus the connection's CA.

    CA certificates are not secret; a file of both is written only when the connection has a
    CA of its own, and removed once the connection is made.

    Parameters
    ----------
    conn
        The connection.

    Yields
    ------
    str
        A file, `LAKANA_CA_BUNDLE` itself, or ``system`` when neither is set.

    Raises
    ------
    UsageError
        When the file `LAKANA_CA_BUNDLE` names cannot be read.
    """
    bundle = os.environ.get("LAKANA_CA_BUNDLE")
    own = conn.settings.get("ca_certificate")
    try:
        trusted = Path(bundle).read_text(encoding="ascii") if bundle else ""
    except (OSError, UnicodeDecodeError):
        raise UsageError(
            f"the trust bundle `{bundle}` (LAKANA_CA_BUNDLE) cannot be read; check the "
            "worker's LAKANA_RUN__CA_BUNDLE"
        ) from None
    if not isinstance(own, str) or not own:
        yield bundle or "system"
        return
    with tempfile.NamedTemporaryFile("w", suffix=".pem", encoding="ascii") as handle:
        handle.write(trusted + "\n" + own + "\n")
        handle.flush()
        yield handle.name


def _conninfo(parts: Mapping[str, str]) -> str:
    r"""Write a libpq connection string, every value quoted.

    The ADBC driver takes only a connection string. Each value is single-quoted with ``\\`` and
    ``'`` escaped, so a database or user name can never add a keyword of its own.

    Parameters
    ----------
    parts
        Keyword to value.

    Returns
    -------
    str
        Such as ``host='db' port='5432'``.
    """

    def quoted(value: str) -> str:
        return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"

    return " ".join(f"{key}={quoted(value)}" for key, value in parts.items())


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


def _postgres_error(conn: FetchedConnection, exc: Exception, host: str) -> Exception:
    """Choose the error for what the PostgreSQL driver raised.

    Parameters
    ----------
    conn
        The connection.
    exc
        The driver's error.
    host
        The name the certificate is checked for.

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
    if state.startswith(("42", "0A", "22", "21")) or "does not exist" in text:
        return UsageError(f"`{conn.name}`: the query failed (PostgreSQL said: {said})")
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
    with _root_certificates(conn) as roots:
        if tls == "verify-full":
            parts["sslrootcert"] = roots
        try:
            database = pg.connect(_conninfo(parts))
        except pg.Error as exc:
            failure = _postgres_error(conn, exc, host)
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
        When the driver cannot open the file.
    """
    sql, values = translate(query, params, style="qmark")
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
    try:
        _directory.regular_file(root_dir, path)
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
    # Always a file: URI, so mode=ro applies (a plain path would be created read-write).
    uri = "file:" + quote(f"{root_dir.rstrip('/')}/{path.strip('/')}") + "?mode=ro"
    try:
        database = lite.connect(uri)
    except lite.Error as exc:
        raise SourceError(f"`{conn.name}`: {_said(str(exc), None)}") from None
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
    import re

    text = str(exc)
    said = _said(text, None)
    found = re.search(r"Type mismatch in column (\d+)", text)
    if found and schema is not None and int(found.group(1)) < len(schema):
        column = schema.names[int(found.group(1))]
        return UsageError(
            f"`{conn.name}`: column `{column}` changes type within the result (SQLite keeps any "
            f"type in any column); cast it in the query, for example CAST({column} AS TEXT)"
        )
    if "readonly database" in text:
        return UsageError(
            f"`{conn.name}`: SQLite could not read the file read-only ({said}). sql.read never "
            "writes: a query that writes fails, and so does a database in WAL mode or with a "
            "hot journal in a directory the worker cannot write; give the connection a copy "
            "made with `PRAGMA journal_mode=DELETE`"
        )
    if "syntax error" in text or "no such" in text:
        return UsageError(f"`{conn.name}`: the query failed (SQLite said: {said})")
    return SourceError(f"`{conn.name}`: {said}")


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
        A connection the asset declares, of kind ``postgresql`` or ``sqlite-file``.
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
    conn = fetch(connection)
    if conn.kind not in KINDS:
        helper = _OTHER_HELPER.get(conn.kind)
        hint = f": use {helper}" if helper else ""
        raise UsageError(
            f"sql.read reads postgresql and sqlite-file connections; `{conn.name}` is "
            f"{conn.kind}{hint}"
        )
    reader = {
        "postgresql": _read_postgres,
        "sqlite-file": _read_sqlite,
    }[conn.kind]
    return reader(conn, query, params, (max_rows, max_bytes))
