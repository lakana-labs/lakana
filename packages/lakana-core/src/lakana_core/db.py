"""Engine construction for the metadata database.

Every engine Lakana creates must come from here. SQLite does not enforce
declared foreign keys — including ``ondelete`` behaviour such as ``CASCADE``
and ``SET NULL`` — unless ``PRAGMA foreign_keys=ON`` is issued on each
connection, and ``lakana server start --local`` is a documented deployment
mode running on SQLite. Attaching the pragma here rather than in a test
fixture is what makes the cascade the catalog relies on real in that mode
and not only under pytest. A connection to a SQLite *file* (not the
in-memory database) also gets WAL journaling and a busy timeout, so local
mode's API and worker processes can share one database file instead of
racing each other with "database is locked" errors. Every engine also checks
a pooled connection before handing it out, so a connection that died while
idle is replaced and never fails the statement that happened to get it.
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
import ssl
from collections.abc import Callable, Generator
from datetime import UTC, datetime
from typing import Any, cast

import sqlalchemy
import sqlalchemy.exc
from sqlalchemy import event, func, select
from sqlalchemy.engine import CursorResult, Engine
from sqlalchemy.engine.interfaces import DBAPIConnection, ExceptionContext
from sqlalchemy.orm import Session

from lakana_core.config import DatabaseConfig
from lakana_core.errors import ConflictError
from lakana_core.log_setup import LOG_FORMAT, SafeFormatter

_BUSY_TIMEOUT_MS = 5000
"""How long a SQLite connection waits for another process's lock (M4c local mode)."""


def _configure_sqlite(dbapi_connection: object, connection_record: object) -> None:
    """Configure a newly opened SQLite connection.

    Foreign keys are always enforced. A connection to a database *file* also
    gets WAL journaling and a busy timeout, so local mode's API and worker
    processes can share it without "database is locked" errors; an in-memory
    database is single-process and keeps its default journal mode. A
    read-only file keeps its existing journal mode too: switching to WAL
    writes to the file, which a read-only open cannot do, so that pragma is
    best-effort and reads still work without it.

    Parameters
    ----------
    dbapi_connection
        The raw DBAPI connection just established.
    connection_record
        SQLAlchemy's pool record for the connection. Unused.
    """
    if not isinstance(dbapi_connection, sqlite3.Connection):
        return
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
    database = cursor.execute("PRAGMA database_list").fetchone()[2]
    if database:
        # A read-only file (e.g. chmod 0400) can't take the WAL switch, which
        # writes to the file; reads must still work without it.
        with contextlib.suppress(sqlite3.OperationalError):
            cursor.execute("PRAGMA journal_mode=WAL")
    cursor.close()


DEFAULT_LEASE_SECONDS = 60
"""The lease the keepalive values derive from when a caller does not pass one."""

logger = logging.getLogger(__name__)


def keepalive_settings(lease_seconds: int) -> dict[str, int]:
    """Derive the PostgreSQL TCP settings that make the server notice a dead client.

    The server probes the client after ``idle`` seconds of silence, then every
    ``interval`` seconds, and drops the backend after ``count`` unanswered
    probes. `tcp_user_timeout` (milliseconds) bounds the same wait when the
    server has unacknowledged data to send, which keepalives do not cover; it
    is set to what the probes need, because on Linux it replaces the probe
    count as the rule that closes a silent connection. With the default lease
    of 60 s the values are 20 s, 10 s, 2 probes and 40 s: a dead client is
    noticed within two thirds of a lease, so the advisory lock of a vanished
    scheduler leader is freed before its lease would have run out twice.

    The values have limits. The lease has no upper bound in the configuration; above about
    98,000 s the kernel rejects the idle value silently. For a lease under 6 s the minimum of
    1 s per value makes detection take 3 s or more. A client that is alive can be cut in one
    case: it is frozen for longer than the user timeout while a result larger than the socket
    buffers is in transit to it (the peer's window stays zero, so nothing is acknowledged).

    Parameters
    ----------
    lease_seconds
        The task lease (`LAKANA_RUN__LEASE_SECONDS`).

    Returns
    -------
    dict of str to int
        PostgreSQL setting names to values, in the order they are issued.
    """
    idle = max(1, lease_seconds // 3)
    interval = max(1, lease_seconds // 6)
    count = 2
    return {
        "tcp_keepalives_idle": idle,
        "tcp_keepalives_interval": interval,
        "tcp_keepalives_count": count,
        "tcp_user_timeout": (idle + count * interval) * 1000,
    }


def _keepalive_listener(lease_seconds: int) -> Callable[[DBAPIConnection, object], None]:
    """Build the connect listener that sets the keepalive settings on a PostgreSQL session.

    A server that rejects a setting (a managed PostgreSQL, or a platform without
    the socket option) costs one warning per engine and never the connection:
    each setting is issued and committed on its own, so one refusal neither
    undoes the others nor leaves a transaction open.

    Parameters
    ----------
    lease_seconds
        The task lease the values derive from.

    Returns
    -------
    Callable
        The listener, with the signature SQLAlchemy's connect event expects.
    """
    settings = keepalive_settings(lease_seconds)
    warned: list[bool] = []

    def _set_keepalives(dbapi_connection: DBAPIConnection, connection_record: object) -> None:
        """Issue the settings on a newly opened connection.

        Parameters
        ----------
        dbapi_connection
            The raw DBAPI connection just established.
        connection_record
            SQLAlchemy's pool record for the connection. Unused.
        """
        refused: list[str] = []
        for name, value in settings.items():
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute(f"SET {name} = {int(value)}")
                dbapi_connection.commit()
            except Exception:
                refused.append(name)
                with contextlib.suppress(Exception):
                    dbapi_connection.rollback()
            finally:
                with contextlib.suppress(Exception):
                    cursor.close()
        if refused and not warned:
            warned.append(True)
            logger.warning(
                "The database refused %s on a connection. Without them PostgreSQL may keep the "
                "database session of a scheduler leader that vanished (power loss, a cut "
                "network) for hours, and nobody ends the runs it held until it does. "
                "Lakana keeps running; to recover, end that session in PostgreSQL "
                "(`lakana scheduler status` names the worker).",
                ", ".join(refused),
            )

    return _set_keepalives


_NOT_A_DISCONNECT = (ssl.SSLCertVerificationError, PermissionError, FileNotFoundError)
"""`OSError` subclasses that name a local or trust problem, not a connection the peer ended."""


def _ping_that_survives_a_reset(engine: Engine) -> None:
    """Make the pre-ping and statement errors treat a raw `OSError` as a dead connection.

    pg8000 reads the first bytes of every answer outside its error handling, so a
    connection the server ended surfaces as a bare `ConnectionResetError` instead of a
    driver error. SQLAlchemy only classifies driver errors, so the pre-ping let it through
    to the request, and a failing statement left the dead connection in the pool.

    Parameters
    ----------
    engine
        The engine to harden; its dialect's ping and error handling are changed.
    """
    dialect = engine.dialect
    ping = dialect.do_ping

    def _ping(dbapi_connection: DBAPIConnection) -> bool:
        try:
            return ping(dbapi_connection)
        except OSError:
            return False  # a false ping makes the pool discard the connection and open a new one

    dialect.do_ping = _ping

    @event.listens_for(engine, "handle_error")
    def _classify(context: ExceptionContext) -> None:
        error = context.original_exception
        # A refused certificate or a missing file says nothing about the peer going away.
        if isinstance(error, OSError) and not isinstance(error, _NOT_A_DISCONNECT):
            context.is_disconnect = True


def create_engine(config: DatabaseConfig, *, lease_seconds: int = DEFAULT_LEASE_SECONDS) -> Engine:
    """Create an engine with Lakana's connection-level guarantees attached.

    The pragma listener is registered against *this* engine rather than
    against SQLAlchemy's `Engine` class, so importing `lakana_core` does not
    silently change the behaviour of engines the host application created
    for its own databases.

    Every connection is checked when it leaves the pool (`pool_pre_ping`) and
    replaced if the database or a firewall closed it while it sat idle. A
    worker supervising a long Python task comes back to connections that have
    been idle for as long as the task ran; without the check, the first
    statement after the task (revoking the task's token, recording the
    outcome) would fail on a dead connection. A connection the server ended
    answers with a raw `OSError` from the driver; that counts as dead too, so
    the pool replaces it instead of failing the request that drew it.

    Parameters
    ----------
    config
        Database URL and echo flag.
    lease_seconds
        The task lease (`LAKANA_RUN__LEASE_SECONDS`). On PostgreSQL every connection
        of the engine asks the server to notice a vanished client within two thirds of
        it (`keepalive_settings`); SQLite ignores it.

    Returns
    -------
    Engine
        A configured engine. The caller owns it and must dispose of it.
    """
    echo_logger = logging.getLogger("sqlalchemy.engine.Engine")
    before = list(echo_logger.handlers)
    engine = sqlalchemy.create_engine(
        config.url, echo=config.echo, pool_pre_ping=True, hide_parameters=True
    )
    _ping_that_survives_a_reset(engine)
    event.listen(engine, "connect", _configure_sqlite)
    if engine.dialect.name == "postgresql":
        event.listen(engine, "connect", _keepalive_listener(lease_seconds))
    if config.echo:  # echo adds a handler of its own, which would bypass the escaping
        for handler in echo_logger.handlers:
            if handler not in before:  # only the one SQLAlchemy just added, not an embedder's
                handler.setFormatter(SafeFormatter(LOG_FORMAT))
    return engine


def rowcount(result: object) -> int:
    """Return the number of rows a DML statement matched.

    The one place that narrows `Session.execute`'s untyped result to a
    `CursorResult`, so compare-and-swap call sites check `rowcount != 1`
    without each repeating the cast.

    Parameters
    ----------
    result
        What `Session.execute` or `Connection.execute` returned for an
        ``UPDATE`` or ``DELETE``.

    Returns
    -------
    int
        Rows matched.
    """
    return cast("CursorResult[Any]", result).rowcount


def _as_utc(value: object) -> datetime:
    """Normalise what a driver returns for ``CURRENT_TIMESTAMP`` to aware UTC.

    Parameters
    ----------
    value
        SQLite returns a naive-UTC ``"YYYY-MM-DD HH:MM:SS"`` string; a driver
        may also return a naive datetime (UTC by SQLite's contract); Postgres
        returns an aware datetime in the session's time zone.

    Returns
    -------
    datetime
        The instant, timezone-aware, in UTC.

    Raises
    ------
    TypeError
        If `value` is none of those shapes.
    """
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime):
        raise TypeError(f"unexpected CURRENT_TIMESTAMP value: {value!r}")
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def db_now(session: Session) -> datetime:
    """Return the database server's current time.

    Lease and heartbeat times are the database's, never a worker's wall clock,
    so clock skew between workers cannot expire a live lease or keep a dead one.
    SQLite's ``CURRENT_TIMESTAMP`` has one-second resolution.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    datetime
        Aware UTC time.
    """
    return _as_utc(session.execute(select(func.current_timestamp())).scalar_one())


_CONCURRENT_STATES = frozenset({"40P01", "40001"})
"""SQLSTATEs of a deadlock and a serialization failure: a concurrent change, safe to retry."""


def sqlstate(error: BaseException | None) -> str | None:
    """Read the SQLSTATE a driver attached to its error.

    Parameters
    ----------
    error
        The driver's error (`DBAPIError.orig`).

    Returns
    -------
    str or None
        The five-character state: psycopg's ``pgcode``/``sqlstate``, or pg8000's ``"C"`` field
        of its first argument; `None` when it carries none.
    """
    for attribute in ("pgcode", "sqlstate"):
        value = getattr(error, attribute, None)
        if isinstance(value, str):
            return value
    args = getattr(error, "args", ())
    if args and isinstance(args[0], dict):
        value = args[0].get("C")
        return value if isinstance(value, str) else None
    return None


@contextlib.contextmanager
def refuse_concurrent_change(session: Session, what: str) -> Generator[None]:
    """Turn a deadlock or serialization failure inside the block into a `ConflictError`.

    Two requests that lock the same rows in opposite orders make Postgres abort one of them;
    that request changed nothing and may simply be sent again. The session is rolled back.

    Parameters
    ----------
    session
        The session the block writes with.
    what
        What changed, for the message (``connection `crm-db```).

    Yields
    ------
    None
        Control to the block.

    Raises
    ------
    ConflictError
        For SQLSTATE 40P01 (deadlock) or 40001 (serialization failure): "<what> changed at the
        same time; try again."
    DBAPIError
        Any other database error, unchanged.
    """
    try:
        yield
    except sqlalchemy.exc.DBAPIError as exc:
        if sqlstate(exc.orig) not in _CONCURRENT_STATES:
            raise
        session.rollback()
        raise ConflictError(f"{what} changed at the same time; try again.") from exc
