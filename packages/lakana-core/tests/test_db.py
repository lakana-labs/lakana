"""Tests for engine construction.

The point of a shipped engine factory is that the pragma SQLite needs in
order to enforce foreign keys is attached by *production* code. A listener
that only exists in `conftest.py` proves the ORM declarations are right and
proves nothing at all about `lakana server start --local`, which is a
documented deployment mode running on SQLite. The same factory also puts a
SQLite *file* connection into WAL journaling with a busy timeout, so local
mode's API and worker processes can share one database file.
"""

import logging
import os
import ssl
import uuid
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy import create_engine as sqlalchemy_create_engine
from sqlalchemy import event, select, text, update
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from lakana_core.config import DatabaseConfig
from lakana_core.db import (
    _as_utc,
    _configure_sqlite,
    _keepalive_listener,
    create_engine,
    db_now,
    keepalive_settings,
    rowcount,
)
from lakana_core.models import Base, Environment, Namespace, SchedulerLease


def _environment() -> Environment:
    """Build a valid environment row.

    Returns
    -------
    Environment
        An environment with a generated id.
    """
    return Environment(
        id=uuid.uuid4(),
        name="dev",
        storage_root="file:///tmp/lakana",
        storage_options={},
        is_protected=False,
        properties={},
    )


def test_sqlite_engine_rejects_a_dangling_foreign_key() -> None:
    """A namespace pointing at no environment fails to insert.

    SQLite silently ignores declared foreign keys unless
    ``PRAGMA foreign_keys=ON`` is issued per connection. This is the
    positive direction: the factory issues it, so the constraint bites.
    """
    engine = create_engine(DatabaseConfig(url="sqlite://"))
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            Namespace(id=uuid.uuid4(), environment_id=uuid.uuid4(), path="gold", properties={})
        )
        with pytest.raises(IntegrityError):
            session.commit()
    engine.dispose()


def test_a_plain_sqlalchemy_engine_does_not() -> None:
    """The same insert succeeds on an engine built without the factory.

    This is the failing direction, and it is the whole point: without it,
    the test above would pass just as well if the listener were registered
    globally against SQLAlchemy's `Engine` class by some other import, or
    if SQLite had started enforcing foreign keys on its own. It proves the
    enforcement is attached by `create_engine` and by nothing else.
    """
    engine = sqlalchemy_create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            Namespace(id=uuid.uuid4(), environment_id=uuid.uuid4(), path="gold", properties={})
        )
        session.commit()
    engine.dispose()


def test_sqlite_engine_cascades_a_delete() -> None:
    """Deleting an environment removes its namespaces on SQLite.

    `ondelete="CASCADE"` is inert on SQLite without the pragma, so this
    covers the specific behaviour `delete_environment` depends on rather
    than only insert-time constraint checking.
    """
    engine = create_engine(DatabaseConfig(url="sqlite://"))
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        env = _environment()
        session.add(env)
        session.flush()
        ns_id = uuid.uuid4()
        session.add(Namespace(id=ns_id, environment_id=env.id, path="gold", properties={}))
        session.commit()

        session.delete(env)
        session.commit()

        remaining = session.execute(
            select(Namespace).where(Namespace.id == ns_id)
        ).scalar_one_or_none()
        assert remaining is None
    engine.dispose()


def test_echo_is_passed_through() -> None:
    """The configured `echo` flag reaches the engine."""
    engine = create_engine(DatabaseConfig(url="sqlite://", echo=True))
    assert engine.echo is True
    engine.dispose()


def test_the_echo_and_statement_errors_never_show_parameters(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A bound value (a secret's ciphertext, nonce or wrapped key) is in no log line or error."""
    engine = create_engine(DatabaseConfig(url="sqlite://", echo=True))
    with (
        caplog.at_level(logging.INFO, logger="sqlalchemy.engine.Engine"),
        engine.connect() as conn,
    ):
        conn.execute(text("SELECT :value"), {"value": "SECRET-CIPHERTEXT"})
        with pytest.raises(OperationalError) as failed:
            conn.execute(text("SELECT * FROM missing WHERE x = :v"), {"v": "SECRET-NONCE"})
    engine.dispose()
    assert "SELECT" in caplog.text
    assert "SECRET-CIPHERTEXT" not in caplog.text
    assert "SECRET-NONCE" not in str(failed.value)
    assert "hidden due to hide_parameters" in str(failed.value)


def test_the_listener_leaves_non_sqlite_connections_alone() -> None:
    """The pragma is not issued against a Postgres connection.

    The same factory serves both backends, so the listener must be inert
    off SQLite. Exercising this directly rather than only through the
    Postgres integration test keeps the branch covered in the unit tier,
    where the integration marker is deselected.
    """

    class _ExplodingConnection:
        """A connection that fails if anything tries to use it."""

        def cursor(self) -> None:
            """Fail: the listener must not reach for a cursor here.

            Raises
            ------
            AssertionError
                Always.
            """
            raise AssertionError("The pragma must not be issued off SQLite.")

    _configure_sqlite(_ExplodingConnection(), object())


def test_sqlite_file_connections_use_wal_and_a_busy_timeout(tmp_path: Path) -> None:
    """Two processes can share a local-mode database."""
    engine = create_engine(DatabaseConfig(url=f"sqlite:///{tmp_path}/lakana.db"))
    with engine.connect() as conn:
        mode = conn.exec_driver_sql("PRAGMA journal_mode").scalar()
        timeout = conn.exec_driver_sql("PRAGMA busy_timeout").scalar()
    engine.dispose()

    assert mode == "wal"
    assert timeout == 5000


def test_in_memory_sqlite_is_left_alone(tmp_path: Path) -> None:
    """`sqlite://` (memory) keeps its default journal mode; WAL doesn't apply to it."""
    engine = create_engine(DatabaseConfig(url="sqlite://"))
    with engine.connect() as conn:
        mode = conn.exec_driver_sql("PRAGMA journal_mode").scalar()
    engine.dispose()

    assert mode == "memory"


@pytest.mark.skipif(
    os.name == "nt" or os.geteuid() == 0,
    reason="chmod does not restrict Windows or root accounts",
)
def test_a_read_only_sqlite_file_still_connects(tmp_path: Path) -> None:
    """A read-only file keeps working; only the WAL attempt is skipped.

    Parameters
    ----------
    tmp_path
        Pytest-provided temporary directory, holding the read-only database
        file.
    """
    db_path = tmp_path / "readonly.db"
    # A plain SQLAlchemy engine, not `lakana_core.db.create_engine`: the file
    # must start in its default (non-WAL) journal mode, or reissuing
    # `PRAGMA journal_mode=WAL` below would be a no-op and never touch disk.
    setup_engine = sqlalchemy_create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(setup_engine)
    setup_engine.dispose()

    os.chmod(db_path, 0o400)
    try:
        engine = create_engine(DatabaseConfig(url=f"sqlite:///{db_path}"))
        with engine.connect() as conn:
            mode = conn.exec_driver_sql("PRAGMA journal_mode").scalar()
            rows = conn.execute(select(Environment)).all()
        engine.dispose()

        assert mode != "wal"
        assert rows == []
    finally:
        os.chmod(db_path, 0o600)


def test_a_pooled_connection_that_died_while_idle_is_replaced_at_checkout(tmp_path: Path) -> None:
    """A long task leaves pooled connections idle; one the server closed must not be handed out."""
    engine = create_engine(DatabaseConfig(url=f"sqlite:///{tmp_path}/lakana.db"))
    try:
        with engine.connect() as connection:
            raw = connection.connection.dbapi_connection
            assert raw is not None
            assert connection.execute(text("SELECT 1")).scalar_one() == 1
        raw.close()  # what an idle timeout or a database restart does to a pooled connection
        with engine.connect() as connection:
            assert connection.execute(text("SELECT 1")).scalar_one() == 1
            assert connection.connection.dbapi_connection is not raw
    finally:
        engine.dispose()


def test_rowcount_reads_an_updates_matched_rows() -> None:
    """The shared helper returns the number of rows an UPDATE matched."""
    engine = create_engine(DatabaseConfig(url="sqlite://"))
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(SchedulerLease(id=1))
        session.flush()
        hit = session.execute(
            update(SchedulerLease).where(SchedulerLease.id == 1).values(holder="a")
        )
        miss = session.execute(
            update(SchedulerLease).where(SchedulerLease.id == 2).values(holder="a")
        )
        assert (rowcount(hit), rowcount(miss)) == (1, 0)
    engine.dispose()


def test_db_now_is_the_databases_clock_as_aware_utc() -> None:
    """SQLite's naive-UTC string comes back aware, close to the real UTC time."""
    engine = create_engine(DatabaseConfig(url="sqlite://"))
    with Session(engine) as session:
        now = db_now(session)
    engine.dispose()
    assert now.tzinfo is UTC
    assert abs(datetime.now(UTC) - now) < timedelta(seconds=5)


def test_as_utc_normalises_every_shape_a_driver_returns() -> None:
    """A SQLite string and naive datetime are UTC; Postgres's aware value is converted."""
    expected = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
    assert _as_utc("2026-10-03 12:00:00") == expected
    assert _as_utc(datetime(2026, 10, 3, 12, 0)) == expected
    plus_two = datetime(2026, 10, 3, 14, 0, tzinfo=timezone(timedelta(hours=2)))
    converted = _as_utc(plus_two)
    assert (converted, converted.tzinfo) == (expected, UTC)
    with pytest.raises(TypeError):
        _as_utc(12)


class _FakeCursor:
    """A DBAPI cursor that records statements and can refuse chosen settings.

    Parameters
    ----------
    connection
        The connection this cursor belongs to.
    """

    def __init__(self, connection: "_FakePostgresConnection") -> None:
        self._connection = connection

    def execute(self, statement: str) -> None:
        """Record a statement, or raise if its setting is one the server refuses.

        Parameters
        ----------
        statement
            The SQL text.

        Raises
        ------
        RuntimeError
            If the statement names a refused setting.
        """
        if any(name in statement for name in self._connection.refuse):
            raise RuntimeError("unrecognized configuration parameter")
        self._connection.statements.append(statement)

    def close(self) -> None:
        """Nothing to close."""


class _FakePostgresConnection:
    """A DBAPI connection that records statements, commits and rollbacks.

    Parameters
    ----------
    refuse
        Setting names whose statements raise.
    """

    def __init__(self, refuse: tuple[str, ...] = ()) -> None:
        self.refuse = refuse
        self.py_types: dict[object, object] = {str: str}  # read by SQLAlchemy's pg8000 on-connect
        self.statements: list[str] = []
        self.commits = 0
        self.rollbacks = 0
        self.rollback_fails = False

    def cursor(self) -> _FakeCursor:
        """Open a cursor.

        Returns
        -------
        _FakeCursor
            A recording cursor.
        """
        return _FakeCursor(self)

    def commit(self) -> None:
        """Count a commit."""
        self.commits += 1

    def rollback(self) -> None:
        """Count a rollback, or fail like a connection that is already gone.

        Raises
        ------
        RuntimeError
            If `rollback_fails` is set.
        """
        if self.rollback_fails:
            raise RuntimeError("connection closed")
        self.rollbacks += 1


def _dbapi(connection: _FakePostgresConnection) -> DBAPIConnection:
    """View a fake connection as the DBAPI connection the listener takes.

    Parameters
    ----------
    connection
        The fake.

    Returns
    -------
    DBAPIConnection
        The same object.
    """
    return cast("DBAPIConnection", connection)


def test_keepalive_settings_derive_from_the_lease() -> None:
    """A dead peer is noticed within two thirds of a lease, however long the lease is."""
    assert keepalive_settings(60) == {
        "tcp_keepalives_idle": 20,
        "tcp_keepalives_interval": 10,
        "tcp_keepalives_count": 2,
        "tcp_user_timeout": 40_000,
    }
    short = keepalive_settings(1)
    assert short["tcp_keepalives_idle"] == 1
    assert short["tcp_keepalives_interval"] == 1
    assert short["tcp_user_timeout"] == 3_000
    long = keepalive_settings(600)
    assert long["tcp_keepalives_idle"] == 200
    assert long["tcp_user_timeout"] == 400_000


def test_the_listener_issues_each_setting_and_commits_it() -> None:
    """Each setting is issued on its own and committed, so a rollback cannot undo it."""
    connection = _FakePostgresConnection()
    _keepalive_listener(60)(_dbapi(connection), None)
    assert connection.statements == [
        "SET tcp_keepalives_idle = 20",
        "SET tcp_keepalives_interval = 10",
        "SET tcp_keepalives_count = 2",
        "SET tcp_user_timeout = 40000",
    ]
    assert connection.commits == 4
    assert connection.rollbacks == 0


def test_the_keepalive_settings_follow_the_configured_lease() -> None:
    """A lease of 30 s gives 10/5/2 and a 20 s user timeout."""
    connection = _FakePostgresConnection()
    _keepalive_listener(30)(_dbapi(connection), None)
    assert connection.statements[0] == "SET tcp_keepalives_idle = 10"
    assert connection.statements[1] == "SET tcp_keepalives_interval = 5"
    assert connection.statements[3] == "SET tcp_user_timeout = 20000"


def test_only_a_postgres_engine_registers_the_keepalive_listener(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the Postgres dialect gets the listener, built with the engine's lease."""
    built: list[int] = []

    def record(lease_seconds: int) -> object:
        """Record the lease and return an inert listener.

        Parameters
        ----------
        lease_seconds
            The lease passed in.

        Returns
        -------
        object
            A listener that does nothing.
        """
        built.append(lease_seconds)
        return lambda dbapi_connection, record: None

    monkeypatch.setattr("lakana_core.db._keepalive_listener", record)
    sqlite = create_engine(DatabaseConfig(url="sqlite://"), lease_seconds=45)
    assert built == []
    postgres = create_engine(
        DatabaseConfig(url="postgresql+pg8000://u:p@localhost:1/db"), lease_seconds=45
    )
    assert built == [45]
    sqlite.dispose()
    postgres.dispose()


def test_a_refused_keepalive_setting_is_logged_once_and_the_rest_still_apply(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A refused setting costs one warning, not the connection, and not one per connection."""
    listener = _keepalive_listener(60)
    first = _FakePostgresConnection(refuse=("tcp_user_timeout",))
    second = _FakePostgresConnection(refuse=("tcp_user_timeout",))
    with caplog.at_level(logging.WARNING, logger="lakana_core.db"):
        listener(_dbapi(first), None)
        listener(_dbapi(second), None)
    assert first.statements[-1] == "SET tcp_keepalives_count = 2"
    assert len(first.statements) == 3
    assert first.rollbacks == 1
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "tcp_user_timeout" in warnings[0].getMessage()
    assert "scheduler" in warnings[0].getMessage()


def test_a_connection_that_cannot_even_roll_back_does_not_stop_the_start(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A dead connection while setting the values is the pool's business, not a crash here."""
    connection = _FakePostgresConnection(refuse=("tcp_",))

    connection.rollback_fails = True
    with caplog.at_level(logging.WARNING, logger="lakana_core.db"):
        _keepalive_listener(60)(_dbapi(connection), None)
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


def test_a_pre_ping_that_the_driver_answers_with_a_raw_reset_replaces_the_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pg8000 raises a bare `ConnectionResetError` from a ping; the pool must replace, not raise."""
    seen: list[object] = []

    def reset_once(self: object, dbapi_connection: object) -> bool:
        seen.append(dbapi_connection)
        if len(seen) == 1:
            raise ConnectionResetError("Connection reset by peer")
        return True

    monkeypatch.setattr(type(_probe_dialect()), "do_ping", reset_once)
    engine = create_engine(DatabaseConfig(url=f"sqlite:///{tmp_path}/lakana.db"))
    try:
        with engine.connect() as connection:
            first = connection.connection.dbapi_connection
        with engine.connect() as connection:  # the ping on this checkout raises the reset
            assert connection.execute(text("SELECT 1")).scalar_one() == 1
            assert connection.connection.dbapi_connection is not first
    finally:
        engine.dispose()


def test_a_raw_reset_during_a_statement_invalidates_the_connection(tmp_path: Path) -> None:
    """The statement that meets the reset fails, but the pool must not keep the dead connection."""
    engine = create_engine(DatabaseConfig(url=f"sqlite:///{tmp_path}/lakana.db"))
    try:

        def reset(*_args: object) -> None:
            raise ConnectionResetError("Connection reset by peer")

        with engine.connect() as connection:
            event.listen(engine, "before_cursor_execute", reset)
            with pytest.raises(ConnectionResetError):
                connection.execute(text("SELECT 1"))
            assert connection.invalidated
            event.remove(engine, "before_cursor_execute", reset)
        with engine.connect() as connection:
            assert connection.execute(text("SELECT 1")).scalar_one() == 1
    finally:
        engine.dispose()


def test_an_ordinary_statement_error_does_not_invalidate_the_connection(tmp_path: Path) -> None:
    """Only an `OSError` counts as a dead connection; a bad statement leaves it in the pool."""
    engine = create_engine(DatabaseConfig(url=f"sqlite:///{tmp_path}/lakana.db"))
    try:
        with engine.connect() as connection:
            with pytest.raises(OperationalError):
                connection.execute(text("SELECT * FROM missing_table"))
            assert not connection.invalidated
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "error",
    [ssl.SSLCertVerificationError("bad certificate"), PermissionError(), FileNotFoundError()],
)
def test_a_local_or_trust_error_is_not_taken_for_a_dead_connection(
    tmp_path: Path, error: OSError
) -> None:
    """A refused certificate or a missing file does not say the peer went away."""
    engine = create_engine(DatabaseConfig(url=f"sqlite:///{tmp_path}/lakana.db"))
    try:

        def fail(*_args: object) -> None:
            raise error

        with engine.connect() as connection:
            event.listen(engine, "before_cursor_execute", fail)
            with pytest.raises(type(error)):
                connection.execute(text("SELECT 1"))
            assert not connection.invalidated
            event.remove(engine, "before_cursor_execute", fail)
    finally:
        engine.dispose()


def _probe_dialect() -> object:
    """Return a SQLite dialect instance, to patch the class the engines under test use.

    Returns
    -------
    object
        A SQLite dialect.
    """
    return sqlalchemy_create_engine("sqlite://").dialect
