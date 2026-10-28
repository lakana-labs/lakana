"""`lakana.ingest.sql`: PostgreSQL through a fake ADBC driver, SQLite through the real one."""

from __future__ import annotations

import contextlib
import errno
import os
import socket
import sqlite3
import sys
import types
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest
from pymysql import converters as real_converters

from lakana import context
from lakana.ingest import (
    CapExceeded,
    SourceError,
    SourceRefused,
    UsageError,
    _check,
    _connections,
    _directory,
    _types,
    sql,
)
from lakana.ingest._directory import DirectoryError, RootUnavailable

_PASSWORD = "pa55-word"


class _TaskConnections:
    """A fake `client.task_connections` answering from a dict of bodies.

    Parameters
    ----------
    bodies
        Name to the server's answer.
    """

    def __init__(self, bodies: Mapping[str, Mapping[str, object]]) -> None:
        self.bodies = bodies

    def get(self, name: str) -> Mapping[str, object]:
        """Answer one fetch.

        Parameters
        ----------
        name
            The connection's name.

        Returns
        -------
        dict
            Its body.
        """
        return self.bodies[name]


@dataclass(frozen=True)
class _Client:
    """A fake SDK client with only `task_connections`.

    Parameters
    ----------
    task_connections
        The fake resource.
    """

    task_connections: _TaskConnections


def _task(
    bodies: Mapping[str, Mapping[str, object]], routing: Mapping[str, Mapping[str, object]]
) -> None:
    """Set the state of a running task.

    Parameters
    ----------
    bodies
        Name to the server's answer.
    routing
        Name to the task description's routing.
    """
    _connections.clear()
    context._set(
        context._TaskState(
            client=_Client(_TaskConnections(bodies)),  # ty: ignore[invalid-argument-type]
            environment="prod",
            asset="bronze.customers",
            declared=frozenset(bodies),
            connections=routing,
        )
    )


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Start without the variables the helper reads; clear the task state after each test.

    Parameters
    ----------
    monkeypatch
        Removes `LAKANA_CA_BUNDLE` and `LAKANA_EGRESS_PROXY` (other modules' tests set them).

    Yields
    ------
    None
        While the test runs.
    """
    monkeypatch.delenv("LAKANA_CA_BUNDLE", raising=False)
    monkeypatch.delenv("LAKANA_EGRESS_PROXY", raising=False)
    yield
    context._set(None)
    _connections.clear()


def _postgres(
    *,
    relay: bool,
    settings: Mapping[str, object] | None = None,
    database: str = "crm",
    password: str | None = _PASSWORD,
) -> None:
    """Declare `crm-db`, a PostgreSQL connection, under the Docker runner or locally.

    Parameters
    ----------
    relay
        Whether the task runs isolated (a relay port in its routing).
    settings
        The connection's settings; ``user: reader`` when `None`.
    database
        Its database.
    password
        The secret's password; no secret at all when `None`.
    """
    body = {
        "name": "crm-db",
        "kind": "postgresql",
        "address": {"host": "db.example.com", "port": 5432, "database": database},
        "settings": dict(settings or {"user": "reader"}),
        "endpoints": [{"host": "db.example.com", "port": 5432, "role": "service"}],
        "secret": None if password is None else {"password": password},
    }
    routing: dict[str, object] = {"kind": "postgresql", "grant": [["db.example.com", 5432]]}
    if relay:
        routing["relay_port"] = 15001
    _task({"crm-db": body}, {"crm-db": routing})


class _Error(Exception):
    """The fake driver's error, with an optional SQLSTATE."""

    def __init__(self, text: str, sqlstate: str | None = None) -> None:
        super().__init__(text)
        self.sqlstate = sqlstate


@dataclass
class _Statement:
    """A fake ADBC statement that records its options."""

    options: dict[str, str] = field(default_factory=dict)

    def set_options(self, **options: str) -> None:
        """Record options.

        Parameters
        ----------
        **options
            The options.
        """
        self.options.update(options)


@dataclass
class _Cursor:
    """A fake ADBC cursor: records what was executed, hands out the given batches."""

    batches: list[pa.RecordBatch]
    fail: Exception | None = None
    executed: list[tuple[object, ...]] = field(default_factory=list)
    adbc_statement: _Statement = field(default_factory=_Statement)

    def execute(self, *args: object) -> None:
        """Record a statement, or raise the configured error.

        Parameters
        ----------
        *args
            The SQL and, if given, its values.

        Raises
        ------
        Exception
            The configured error.
        """
        self.executed.append(args)
        if self.fail is not None:
            raise self.fail

    def fetch_record_batch(self) -> pa.RecordBatchReader:
        """Hand out the batches.

        Returns
        -------
        pyarrow.RecordBatchReader
            A reader of them.
        """
        return pa.RecordBatchReader.from_batches(self.batches[0].schema, self.batches)


@dataclass
class _Database:
    """A fake ADBC connection."""

    cursor_: _Cursor
    closed: bool = False

    def cursor(self) -> _Cursor:
        """Return the cursor.

        Returns
        -------
        _Cursor
            It.
        """
        return self.cursor_

    def close(self) -> None:
        """Note the close."""
        self.closed = True


@dataclass
class _Driver:
    """A fake ``adbc_driver_postgresql.dbapi``: records the connection string."""

    cursor: _Cursor
    refuse: Exception | None = None
    seen: list[str] = field(default_factory=list)
    roots: list[str] = field(default_factory=list)
    Error: type[Exception] = _Error
    database: _Database | None = None

    def connect(self, conninfo: str) -> _Database:
        """Record the connection string and the root file's content, or refuse.

        Parameters
        ----------
        conninfo
            The libpq connection string.

        Returns
        -------
        _Database
            The connection.

        Raises
        ------
        Exception
            The configured refusal.
        """
        self.seen.append(conninfo)
        for part in conninfo.split("' "):
            if part.startswith("sslrootcert='") and _check.NOWHERE not in part:
                path = part.removeprefix("sslrootcert='").rstrip("'")
                self.roots.append(Path(path).read_text() if path.startswith("/") else path)
        if self.refuse is not None:
            raise self.refuse
        self.database = _Database(self.cursor)
        return self.database


def _batch(**columns: list[object]) -> pa.RecordBatch:
    """Build a batch of plain columns.

    Parameters
    ----------
    **columns
        Name to values.

    Returns
    -------
    pyarrow.RecordBatch
        The batch.
    """
    return pa.RecordBatch.from_pydict(columns)


@pytest.fixture
def driver(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _Driver:
    """Put a fake PostgreSQL driver where `sql.read` imports it; the relay resolves.

    Parameters
    ----------
    monkeypatch
        Installs the module and the resolver.
    tmp_path
        For the trust bundle.

    Returns
    -------
    _Driver
        The fake, with one batch of two rows.
    """
    fake = _Driver(_Cursor([_batch(id=[1, 2])]))
    module = types.ModuleType("adbc_driver_postgresql.dbapi")
    module.connect = fake.connect  # ty: ignore[unresolved-attribute]
    module.Error = _Error  # ty: ignore[unresolved-attribute]
    monkeypatch.setitem(sys.modules, "adbc_driver_postgresql.dbapi", module)
    monkeypatch.setattr(sql.socket, "gethostbyname", lambda name: "10.207.0.3")
    bundle = tmp_path / "bundle.pem"
    bundle.write_text("BUNDLE\n")
    monkeypatch.setenv("LAKANA_CA_BUNDLE", str(bundle))
    return fake


def test_under_docker_libpq_dials_the_relay_and_verifies_the_real_host(driver: _Driver) -> None:
    """``hostaddr`` is the relay, ``host`` the real name TLS checks; the transaction reads only."""
    _postgres(relay=True)

    table = sql.read("crm-db", "SELECT id FROM customers")

    assert table.column("id").to_pylist() == [1, 2]
    bundle = os.environ["LAKANA_CA_BUNDLE"]
    assert driver.seen == [
        "host='db.example.com' port='15001' hostaddr='10.207.0.3' dbname='crm' user='reader' "
        f"password='{_PASSWORD}' sslmode='verify-full' connect_timeout='10' "
        f"options='-c default_transaction_read_only=on' sslrootcert='{bundle}' "
        f"sslcertmode='disable' sslcrl='{_check.NOWHERE}' passfile='{_check.NOWHERE}'"
    ]
    assert driver.cursor.executed == [("SELECT id FROM customers",)]
    assert driver.database is not None and driver.database.closed


def test_locally_libpq_dials_the_host_itself(driver: _Driver) -> None:
    """Without a relay port the connection string has no ``hostaddr``."""
    _postgres(relay=False)

    sql.read("crm-db", "SELECT id FROM customers")

    assert "hostaddr" not in driver.seen[0]
    assert "host='db.example.com' port='5432'" in driver.seen[0]


def test_a_connection_ca_is_trusted_next_to_the_bundle_and_its_file_removed(
    driver: _Driver,
) -> None:
    """The file libpq reads holds the bundle and the connection's CA, and is gone afterwards."""
    _postgres(relay=True, settings={"user": "reader", "ca_certificate": "OWN CA"})

    sql.read("crm-db", "SELECT 1")

    assert driver.roots == ["BUNDLE\n\nOWN CA\n"]
    path = driver.seen[0].split("sslrootcert='")[1].split("'")[0]
    assert not Path(path).exists()


@pytest.mark.parametrize(("tls", "mode"), [("require", "require"), ("disable", "disable")])
def test_require_and_disable_name_no_root_certificates(
    driver: _Driver, tls: str, mode: str
) -> None:
    """The ``require`` of libpq would verify the chain if it got a root file; ours must not.

    Not even ``~/.postgresql/root.crt`` of the worker's user: the root file is a path that
    cannot exist, and no client certificate is ever presented.
    """
    _postgres(relay=False, settings={"user": "reader", "tls": tls})

    sql.read("crm-db", "SELECT 1")

    assert f"sslmode='{mode}'" in driver.seen[0]
    assert f"sslrootcert='{_check.NOWHERE}'" in driver.seen[0]
    assert "sslcertmode='disable'" in driver.seen[0]


def test_a_database_name_cannot_add_a_keyword(driver: _Driver) -> None:
    """Every value is quoted: a quote in a name stays inside it."""
    _postgres(relay=False, database="crm' host='evil.example")

    sql.read("crm-db", "SELECT 1")

    assert "dbname='crm\\' host=\\'evil.example'" in driver.seen[0]


def test_parameters_are_bound_and_checked_before_any_connection(driver: _Driver) -> None:
    """`:name` becomes `$1`; a missing value fails without a connection."""
    _postgres(relay=False)

    sql.read("crm-db", "SELECT id FROM t WHERE u > :since", params={"since": 5})
    with pytest.raises(UsageError, match="placeholder :x has no value"):
        sql.read("crm-db", "SELECT :x")

    assert driver.cursor.executed == [("SELECT id FROM t WHERE u > $1", [5])]
    assert len(driver.seen) == 1


def test_a_none_parameter_is_refused_before_the_query_runs(driver: _Driver) -> None:
    """Review Focus 1, end to end: the first run's watermark is `None`."""
    _postgres(relay=False)

    with pytest.raises(UsageError, match=r"ctx\.watermark\(default=\.\.\.\)"):
        sql.read("crm-db", "SELECT * FROM t WHERE u > :since", params={"since": None})

    assert driver.seen == []


@pytest.mark.parametrize(
    ("max_rows", "max_bytes", "cap"), [(3, 1 << 30, "max_rows"), (10**9, 20, "max_bytes")]
)
def test_a_cap_stops_the_read_naming_how_to_narrow_it(
    driver: _Driver, max_rows: int, max_bytes: int, cap: str
) -> None:
    """Each cap is checked batch by batch."""
    driver.cursor.batches = [_batch(id=[1, 2]), _batch(id=[3, 4])]
    _postgres(relay=False)

    with pytest.raises(
        CapExceeded, match=rf"{cap}.*narrow the query \(add a WHERE clause or a LIMIT\)"
    ):
        sql.read("crm-db", "SELECT id FROM t", max_rows=max_rows, max_bytes=max_bytes)


def test_a_relay_reset_points_to_the_relay_line(driver: _Driver) -> None:
    """Review Focus 3: under Docker a reset means the relay refused; its log line says why."""
    driver.refuse = _Error(
        'IO: [libpq] Failed to connect: connection to server at "10.207.0.3", port 15001 failed: '
        "server closed the connection unexpectedly"
    )
    _postgres(relay=True)

    with pytest.raises(SourceError) as caught:
        sql.read("crm-db", "SELECT 1")

    assert str(caught.value) == (
        "`crm-db`: the connection through the task's relay closed before PostgreSQL answered; "
        "the task's log has a `lakana-relay:` line that says why (an address inside the Lakana "
        "stack or on the Docker host is never reachable)"
    )


def test_the_same_reset_without_a_relay_keeps_the_drivers_words(driver: _Driver) -> None:
    """Locally there is no relay to blame."""
    driver.refuse = _Error("server closed the connection unexpectedly")
    _postgres(relay=False)

    with pytest.raises(SourceError, match="server closed the connection unexpectedly"):
        sql.read("crm-db", "SELECT 1")


def test_an_authentication_failure_names_the_test_command_and_masks_the_password(
    driver: _Driver,
) -> None:
    """The fixed text, PostgreSQL's own words kept (at most 1,000 characters), never the secret."""
    driver.refuse = _Error(
        f'FATAL:  password authentication failed for user "reader" {_PASSWORD} ' + "x" * 2000
    )
    _postgres(relay=False)

    with pytest.raises(SourceRefused) as caught:
        sql.read("crm-db", "SELECT 1")

    text = str(caught.value)
    assert text.startswith("`crm-db`: authentication failed (PostgreSQL said: FATAL: password")
    assert text.endswith("Check the secret with `lakana connection test crm-db --env prod`.")
    assert _PASSWORD not in text and "***" in text
    assert len(text) < 1200
    assert caught.value.retryable is False


def test_a_login_refused_in_another_language_is_retried_with_the_hint(driver: _Driver) -> None:
    """The known limit: a refusal is recognised by libpq's English words only.

    In German it is a `SourceError`, retried like any failure to connect, and its message says
    what to check (docs/site/reference/ingest.md says the same).
    """
    driver.refuse = _Error(
        "FATAL:  Passwort-Authentifizierung für Benutzer »reader« fehlgeschlagen"
    )
    _postgres(relay=False)

    with pytest.raises(SourceError) as caught:
        sql.read("crm-db", "SELECT 1")

    assert type(caught.value) is SourceError
    assert "Passwort-Authentifizierung" in str(caught.value)
    assert str(caught.value).endswith(
        "if this is a refused login on a server whose messages are not in English, check the "
        "user and password"
    )


def test_a_query_error_without_a_sqlstate_has_no_login_hint(driver: _Driver) -> None:
    driver.cursor.fail = _Error("ERROR: something odd")
    _postgres(relay=False)

    with pytest.raises(SourceError) as caught:
        sql.read("crm-db", "SELECT 1")

    assert "check the user and password" not in str(caught.value)


@pytest.mark.parametrize(
    "said",
    [
        "SSL error: certificate verify failed",
        'server certificate for "db.example.com" does not match host name "db.example.com"',
    ],
)
def test_a_certificate_failure_gives_the_tls_text(driver: _Driver, said: str) -> None:
    """The fixed TLS text names the real host and the command that adds the CA."""
    driver.refuse = _Error(said)
    _postgres(relay=True)

    with pytest.raises(SourceRefused) as caught:
        sql.read("crm-db", "SELECT 1")

    assert str(caught.value) == (
        "`crm-db`: the server's certificate is not trusted for `db.example.com`. Give the "
        "connection your CA: `lakana connection update crm-db --env prod --ca-file ca.pem`."
    )


@pytest.mark.parametrize(
    ("sqlstate", "kind", "fragment"),
    [
        ("25006", UsageError, "read-only transaction, and this one writes"),
        ("42601", UsageError, "the query failed"),
        ("42501", SourceRefused, "may not read this"),
        ("57014", SourceError, "canceling statement"),
    ],
)
def test_a_query_error_is_classed_by_its_sqlstate(
    driver: _Driver, sqlstate: str, kind: type[Exception], fragment: str
) -> None:
    """A write or a syntax error repeats; a cancelled statement may pass."""
    driver.cursor.fail = _Error(f"ERROR: canceling statement {sqlstate}", sqlstate)
    _postgres(relay=False)

    with pytest.raises(kind, match=fragment) as caught:
        sql.read("crm-db", "SELECT 1")

    assert type(caught.value) is kind


def test_a_value_of_another_type_than_its_column_names_both_fixes(driver: _Driver) -> None:
    """42883: ADBC binds a str as text; the message says to pass a datetime or cast."""
    driver.cursor.fail = _Error(
        "ERROR: operator does not exist: timestamp with time zone > text", "42883"
    )
    _postgres(relay=False)

    with pytest.raises(UsageError, match=r"a datetime for a timestamp.*:since::timestamptz"):
        sql.read("crm-db", "SELECT 1 WHERE now() > :since", params={"since": "1970-01-01"})


def test_a_nul_in_the_connection_string_is_a_setup_error(driver: _Driver) -> None:
    """Libpq would cut the string at the NUL and fail on every attempt."""
    _postgres(relay=False, settings={"user": "read\x00er"})

    with pytest.raises(UsageError, match=r"`user` of `crm-db` holds a NUL character") as caught:
        sql.read("crm-db", "SELECT 1")

    assert caught.value.retryable is False
    assert driver.seen == []


def test_a_connection_of_another_kind_names_the_helper(driver: _Driver) -> None:
    """An ``s3`` connection is for files.read."""
    _task(
        {
            "landing": {
                "name": "landing",
                "kind": "s3",
                "address": {},
                "settings": {},
                "endpoints": [],
                "secret": None,
            }
        },
        {"landing": {}},
    )

    with pytest.raises(
        UsageError,
        match=r"sql.read reads postgresql, mariadb and sqlite-file connections; `landing` is s3",
    ):
        sql.read("landing", "SELECT 1")


def test_without_the_assets_extra_the_error_says_what_to_add(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A project without `lakana[assets]` cannot import the driver."""
    monkeypatch.setitem(sys.modules, "adbc_driver_postgresql.dbapi", None)
    _postgres(relay=False)

    with pytest.raises(UsageError, match=r"add lakana\[assets\] to the project's dependencies"):
        sql.read("crm-db", "SELECT 1")


@pytest.mark.parametrize(
    ("kwargs", "fragment"),
    [
        ({"max_rows": 0}, "max_rows= must be a positive whole number"),
        ({"max_bytes": True}, "max_bytes= must be a positive whole number"),
        ({"params": [1]}, "params= takes a mapping"),
    ],
)
def test_bad_arguments_are_refused_before_the_fetch(kwargs: dict[str, Any], fragment: str) -> None:
    """No task state is needed to refuse them."""
    with pytest.raises(UsageError, match=fragment):
        sql.read("crm-db", "SELECT 1", **kwargs)


@pytest.mark.parametrize("content", [None, "caf\u00e9"])
def test_an_unreadable_trust_bundle_names_the_worker_setting(
    driver: _Driver, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: str | None
) -> None:
    """A bundle the worker named but the task cannot read (missing, not ASCII) is a setup error."""
    bundle = tmp_path / "unreadable.pem"
    if content is not None:
        bundle.write_text(content, encoding="utf-8")
    monkeypatch.setenv("LAKANA_CA_BUNDLE", str(bundle))
    _postgres(relay=True)

    with pytest.raises(UsageError, match=r"trust bundle .*LAKANA_RUN__CA_BUNDLE") as caught:
        sql.read("crm-db", "SELECT 1")

    assert caught.value.retryable is False
    assert driver.seen == []


def test_a_ca_file_that_cannot_be_written_keeps_its_own_words(
    driver: _Driver, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the bundle's read is an unreadable bundle; a full /tmp is said as such."""

    def full(*args: object, **kwargs: object) -> object:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(_check.tempfile, "NamedTemporaryFile", full)
    _postgres(relay=True, settings={"user": "reader", "ca_certificate": "-----BEGIN"})

    with pytest.raises(OSError, match="No space left on device") as caught:
        sql.read("crm-db", "SELECT 1")

    assert "trust bundle" not in str(caught.value)
    assert driver.seen == []


def test_without_a_trust_bundle_libpq_uses_the_system_roots(
    driver: _Driver, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``sslrootcert=system``: libpq's own name for the operating system's store."""
    monkeypatch.delenv("LAKANA_CA_BUNDLE")
    _postgres(relay=False)

    sql.read("crm-db", "SELECT 1")

    assert "sslrootcert='system'" in driver.seen[0]


def test_a_relay_that_does_not_resolve_is_retried(
    driver: _Driver, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The relay may still be starting; the task's log says why it is not there."""

    def unresolved(name: str) -> str:
        raise socket.gaierror(name)

    monkeypatch.setattr(sql.socket, "gethostbyname", unresolved)
    _postgres(relay=True)

    with pytest.raises(SourceError, match=r"relay \(lakana-relay\) does not resolve") as caught:
        sql.read("crm-db", "SELECT 1")

    assert caught.value.retryable is None  # the harness's retry policy decides
    assert driver.seen == []


def test_a_connection_without_a_password_names_the_command_that_stores_it(
    driver: _Driver,
) -> None:
    """Nothing is dialled without a password."""
    _postgres(relay=False, password=None)

    with pytest.raises(
        UsageError,
        match=r"`crm-db` has no secret with `password`.*--env prod --password-stdin",
    ):
        sql.read("crm-db", "SELECT 1")

    assert driver.seen == []


def test_an_empty_query_is_refused() -> None:
    """Whitespace is not a statement."""
    with pytest.raises(UsageError, match=r"sql\.read needs a query"):
        sql.read("crm-db", "  \n")


# --- PostgreSQL types ---------------------------------------------------------------------------


def _pg_field(name: str, kind: pa.DataType, pg: str, typmod: int = -1) -> pa.Field:
    """Build a field as ADBC 1.13 reports it.

    Parameters
    ----------
    name
        The column.
    kind
        Its Arrow type.
    pg
        PostgreSQL's type name.
    typmod
        Its type modifier.

    Returns
    -------
    pyarrow.Field
        The field with ADBC's metadata.
    """
    return pa.field(
        name, kind, metadata={_types.PG_TYPE: pg.encode(), _types.PG_TYPMOD: str(typmod).encode()}
    )


def test_numeric_with_precision_becomes_a_decimal_and_uuid_and_json_strings() -> None:
    """``numeric(12,2)`` (typmod 786438) → ``decimal128(12, 2)``; metadata is dropped."""
    opaque = pa.string()
    schema = pa.schema(
        [
            _pg_field("amount", opaque, "numeric", 786438),
            _pg_field("id", pa.uuid(), "uuid"),
            _pg_field("doc", pa.json_(), "jsonb"),
            _pg_field("at", pa.timestamp("us", "UTC"), "timestamptz"),
            _pg_field("tags", pa.list_(pa.int32()), "_int4"),
        ]
    )
    target = _types.postgres_schema(schema, "crm-db")
    batch = pa.RecordBatch.from_arrays(
        [
            pa.array(["1.50", None]),
            pa.array([bytes(range(16)), None], pa.uuid()),
            pa.array(['{"a": 1}', None], pa.json_()),
            pa.array([0, None], pa.timestamp("us", "UTC")),
            pa.array([[1, 2], None], pa.list_(pa.int32())),
        ],
        schema=schema,
    )

    out = _types.normalize_postgres(batch, target, "crm-db")

    assert target == pa.schema(
        [
            ("amount", pa.decimal128(12, 2)),
            ("id", pa.string()),
            ("doc", pa.string()),
            ("at", pa.timestamp("us", "UTC")),
            ("tags", pa.list_(pa.int32())),
        ]
    )
    assert out.schema.metadata is None and all(f.metadata is None for f in out.schema)
    assert out.column("id").to_pylist() == ["00010203-0405-0607-0809-0a0b0c0d0e0f", None]
    assert str(out.column("amount")[0]) == "1.50"


@pytest.mark.parametrize(
    ("field", "cast"),
    [
        (_pg_field("n", pa.string(), "numeric"), "CAST(n AS numeric(38, 9))"),
        (_pg_field("n", pa.string(), "numeric", ((40 << 16) | 2) + 4), "CAST(n AS numeric(38, 2))"),
        (_pg_field("m", pa.int64(), "money"), "CAST(m AS numeric(19, 2))"),
        (_pg_field("i", pa.month_day_nano_interval(), "interval"), "EXTRACT(EPOCH FROM i)"),
        (
            _pg_field("ip", pa.binary(), "inet").with_type(
                pa.opaque(pa.binary(), "inet", "PostgreSQL")
            ),
            "CAST(ip AS text)",
        ),
        (_pg_field("ids", pa.list_(pa.uuid()), "_uuid"), "CAST(ids AS text)"),
    ],
)
def test_a_column_without_a_faithful_arrow_form_names_the_cast(field: pa.Field, cast: str) -> None:
    """Refused before any row is read; money would otherwise arrive as an integer of cents."""
    with pytest.raises(UsageError, match=r"cast it in the query, for example ") as caught:
        _types.postgres_schema(pa.schema([field]), "crm-db")

    assert cast in str(caught.value)
    assert caught.value.retryable is False


def test_a_negative_scale_is_refused_naming_the_cast() -> None:
    """PostgreSQL 15 allows ``numeric(5, -2)``, which no Arrow decimal can hold."""
    field = _pg_field("n", pa.string(), "numeric", ((5 << 16) | (-2 & 0x7FF)) + 4)

    with pytest.raises(UsageError, match=r"CAST\(n AS numeric\(38, 9\)\)"):
        _types.postgres_schema(pa.schema([field]), "crm-db")


def _meta(name: str, kind: pa.DataType) -> pa.Field:
    """Build a child field carrying metadata, as ADBC does inside nested types.

    Parameters
    ----------
    name
        The child's name.
    kind
        Its type.

    Returns
    -------
    pyarrow.Field
        The field with ``POSTGRESQL:type`` metadata.
    """
    return pa.field(name, kind, metadata={_types.PG_TYPE: b"int4"})


def test_nested_types_lose_their_childrens_metadata_at_every_depth() -> None:
    """Lists, large lists and structs keep their shape; no child keeps ADBC's metadata."""
    item = pa.struct([_meta("x", pa.int32()), pa.field("y", pa.list_(_meta("item", pa.int64())))])
    schema = pa.schema(
        [
            _pg_field("rows", pa.list_(_meta("item", item)), "_record"),
            _pg_field("big", pa.large_list(_meta("item", pa.int32())), "_int4"),
        ]
    )
    batch = pa.RecordBatch.from_arrays(
        [
            pa.array([[{"x": 1, "y": [2]}], None], schema.field("rows").type),
            pa.array([[1, 2], None], schema.field("big").type),
        ],
        schema=schema,
    )

    target = _types.postgres_schema(schema, "crm-db")
    out = _types.normalize_postgres(batch, target, "crm-db")

    bare = pa.struct([("x", pa.int32()), ("y", pa.list_(pa.int64()))])
    assert target == pa.schema([("rows", pa.list_(bare)), ("big", pa.large_list(pa.int32()))])
    assert out.schema.field("rows").type.value_field.metadata is None
    assert out.schema.field("rows").type.value_type.field("x").metadata is None
    assert out.column("rows").to_pylist() == [[{"x": 1, "y": [2]}], None]
    assert out.column("big").to_pylist() == [[1, 2], None]


def test_a_struct_holding_a_converted_type_is_refused() -> None:
    """A uuid inside a composite has no plain form; the whole column is cast to text."""
    field = _pg_field("r", pa.struct([pa.field("id", pa.uuid())]), "record")

    with pytest.raises(UsageError, match=r"CAST\(r AS text\)"):
        _types.postgres_schema(pa.schema([field]), "crm-db")


def test_a_nan_in_a_decimal_column_is_named() -> None:
    """``numeric(12,2)`` may hold NaN, which no decimal holds."""
    schema = pa.schema([_pg_field("amount", pa.string(), "numeric", 786438)])
    batch = pa.RecordBatch.from_arrays([pa.array(["NaN"])], schema=schema)

    with pytest.raises(UsageError, match="column `amount` holds a value"):
        _types.normalize_postgres(batch, _types.postgres_schema(schema, "crm-db"), "crm-db")


# --- SQLite through the real driver -------------------------------------------------------------


@pytest.fixture
def lite(tmp_path: Path) -> Path:
    """Declare `local-db`, a ``sqlite-file`` connection on ``<root>/data/app.db``.

    Parameters
    ----------
    tmp_path
        pytest's directory.

    Returns
    -------
    Path
        The database file (not yet created).
    """
    root = tmp_path / "shared"
    (root / "data").mkdir(parents=True)
    body = {
        "name": "local-db",
        "kind": "sqlite-file",
        "address": {"root": "shared", "path": "data/app.db"},
        "settings": {},
        "endpoints": [],
        "secret": None,
    }
    routing = {
        "kind": "sqlite-file",
        "root": "shared",
        "root_dir": str(root),
        "path": "data/app.db",
        "root_path": str(root / "data" / "app.db"),
    }
    _task({"local-db": body}, {"local-db": routing})
    return root / "data" / "app.db"


def _sqlite(path: Path, statements: list[str]) -> None:
    """Make a SQLite database.

    Parameters
    ----------
    path
        The file.
    statements
        SQL to run.
    """
    with contextlib.closing(sqlite3.connect(path)) as db, db:
        for statement in statements:
            db.execute(statement)


def test_sqlite_reads_read_only_with_bound_parameters(lite: Path) -> None:
    """``file:...?mode=ro``: a read works, a write fails and the file is unchanged."""
    _sqlite(lite, ["CREATE TABLE t(a INTEGER, b TEXT)", "INSERT INTO t VALUES (1, 'x'), (2, 'y')"])

    table = sql.read("local-db", "SELECT a, b FROM t WHERE a > :low", params={"low": 1})
    with pytest.raises(UsageError, match="read-only"):
        sql.read("local-db", "INSERT INTO t VALUES (3, 'z')")

    assert table.to_pylist() == [{"a": 2, "b": "y"}]
    with contextlib.closing(sqlite3.connect(lite)) as db:
        assert db.execute("SELECT count(*) FROM t").fetchone() == (2,)


def test_sqlite_refuses_a_link_at_the_file_path(lite: Path, tmp_path: Path) -> None:
    """The walker's rule: no symbolic link is followed, before the driver opens anything."""
    elsewhere = tmp_path / "elsewhere.db"
    _sqlite(elsewhere, ["CREATE TABLE t(a)"])
    lite.symlink_to(elsewhere)

    with pytest.raises(DirectoryError, match="symbolic link"):
        sql.read("local-db", "SELECT * FROM t")


def test_sqlite_refuses_a_link_swapped_in_between_the_check_and_the_open(
    lite: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The file SQLite opened is compared with the one the walker found; a swap is refused."""
    _sqlite(lite, ["CREATE TABLE t(v TEXT)", "INSERT INTO t VALUES ('inside')"])
    elsewhere = tmp_path / "elsewhere.db"
    _sqlite(elsewhere, ["CREATE TABLE t(v TEXT)", "INSERT INTO t VALUES ('outside')"])
    moved = lite.with_name("moved.db")
    checked = _directory.regular_file
    checks: list[str] = []

    def check_then_swap(root_dir: str, path: str) -> int:
        if lite.is_symlink():  # the attacker puts the file back for each check
            lite.unlink()
            moved.rename(lite)
        found = checked(root_dir, path)
        checks.append(path)
        lite.rename(moved)
        lite.symlink_to(elsewhere)
        return found

    monkeypatch.setattr(_directory, "regular_file", check_then_swap)

    with pytest.raises(SourceError, match=r"`local-db`: 'data/app.db' changed while") as caught:
        sql.read("local-db", "SELECT v FROM t")

    assert type(caught.value) is SourceError
    assert len(checks) == 3


def test_sqlite_reads_a_file_replaced_in_place_on_the_next_attempt(
    lite: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An export that renames a new file over the old one while it is opened is read anew."""
    _sqlite(lite, ["CREATE TABLE t(v TEXT)", "INSERT INTO t VALUES ('old')"])
    fresh = lite.with_name("fresh.db")
    _sqlite(fresh, ["CREATE TABLE t(v TEXT)", "INSERT INTO t VALUES ('new')"])
    checked = _directory.regular_file

    def check_then_replace(root_dir: str, path: str) -> int:
        found = checked(root_dir, path)
        if fresh.exists():
            fresh.replace(lite)
        return found

    monkeypatch.setattr(_directory, "regular_file", check_then_replace)

    assert sql.read("local-db", "SELECT v FROM t").column("v").to_pylist() == ["new"]


def test_sqlite_reads_with_no_descriptor_of_the_file_left_open(lite: Path) -> None:
    _sqlite(lite, ["CREATE TABLE t(a)"])
    seen = os.stat(lite)

    sql.read("local-db", "SELECT a FROM t")

    assert (seen.st_dev, seen.st_ino) not in sql._open_files().values()


def test_sqlite_refuses_when_this_process_cannot_list_its_open_files(
    lite: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without ``/dev/fd`` the file SQLite opened cannot be compared: strict, not a silent pass."""
    _sqlite(lite, ["CREATE TABLE t(a)"])

    def missing(path: str) -> list[str]:
        raise FileNotFoundError(2, "No such file or directory", path)

    monkeypatch.setattr(sql.os, "listdir", missing)

    with pytest.raises(UsageError, match=r"/dev/fd"):
        sql.read("local-db", "SELECT a FROM t")


def test_only_the_checked_file_counts_as_the_one_sqlite_opened(tmp_path: Path) -> None:
    """Another file still open is a mismatch; one closed again (another thread's) is not."""
    checked, earlier = (1, 10), (1, 12)
    walker = {4: checked}
    other = tmp_path / "other.db"
    other.write_bytes(b"")
    with contextlib.ExitStack() as stack:
        held = os.open(other, os.O_RDONLY)
        stack.callback(os.close, held)
        foreign = sql._identity(held)
        assert foreign is not None
        closed = held + 1000  # no descriptor this high is open

        assert sql._opened_only(checked, 4, walker, {**walker, 5: checked})
        assert sql._opened_only(checked, 4, {**walker, 5: earlier}, {**walker, 5: checked})
        assert sql._opened_only(checked, 4, {**walker, 5: checked}, {**walker, 5: checked})
        assert sql._opened_only(checked, 4, walker, {**walker, 5: checked, closed: foreign})
        assert not sql._opened_only(checked, 4, walker, {**walker, 5: checked, held: foreign})
        assert not sql._opened_only(checked, 4, walker, {**walker, held: foreign})
        assert not sql._opened_only(checked, 4, walker, walker)


def test_open_files_name_regular_files_only(tmp_path: Path) -> None:
    regular = tmp_path / "a.db"
    regular.write_bytes(b"")
    with contextlib.ExitStack() as stack:
        file_fd = os.open(regular, os.O_RDONLY)
        stack.callback(os.close, file_fd)
        directory_fd = os.open(tmp_path, os.O_RDONLY)
        stack.callback(os.close, directory_fd)

        found = sql._open_files()

    seen = os.stat(regular)
    assert found[file_fd] == (seen.st_dev, seen.st_ino)
    assert directory_fd not in found


def test_sqlite_names_a_column_that_changes_type_within_the_result(lite: Path) -> None:
    """ADBC fixes a column's type from its first rows; a later text in an integer column fails."""
    rows = ", ".join(["(1)"] * 2000)
    _sqlite(
        lite, ["CREATE TABLE t(a)", f"INSERT INTO t VALUES {rows}", "INSERT INTO t VALUES ('x')"]
    )

    with pytest.raises(UsageError, match=r"column `a` changes type.*CAST\(a AS TEXT\)"):
        sql.read("local-db", "SELECT a FROM t")


def test_sqlite_never_creates_a_missing_file(lite: Path) -> None:
    """A plain path would be created read-write; the walker refuses first."""
    with pytest.raises(DirectoryError, match="does not exist"):
        sql.read("local-db", "SELECT 1")

    assert not lite.exists()
    assert sorted(p.name for p in lite.parent.iterdir()) == []


def test_sqlite_without_an_ingestion_root_in_the_task_description(lite: Path) -> None:
    """An older worker sends no root; the message says to align the versions."""
    _task({"local-db": _lite_body()}, {"local-db": {"kind": "sqlite-file"}})

    with pytest.raises(UsageError, match="no ingestion root for `local-db`"):
        sql.read("local-db", "SELECT 1")


def _lite_body() -> dict[str, object]:
    """Return the server's answer for `local-db`.

    Returns
    -------
    dict
        The body.
    """
    return {
        "name": "local-db",
        "kind": "sqlite-file",
        "address": {"root": "shared", "path": "data/app.db"},
        "settings": {},
        "endpoints": [],
        "secret": None,
    }


def test_sqlite_without_the_assets_extra_says_what_to_add(
    lite: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The SQLite driver comes with `lakana[assets]` too."""
    monkeypatch.setitem(sys.modules, "adbc_driver_sqlite.dbapi", None)

    with pytest.raises(UsageError, match=r"add lakana\[assets\]"):
        sql.read("local-db", "SELECT 1")


def test_sqlite_on_a_root_this_worker_does_not_have_names_the_setting(
    lite: Path, tmp_path: Path
) -> None:
    """The worker's own text: which root, and which variable sets it."""
    _task(
        {"local-db": _lite_body()},
        {
            "local-db": {
                "kind": "sqlite-file",
                "root": "shared",
                "root_dir": str(tmp_path / "gone"),
                "path": "data/app.db",
            }
        },
    )

    with pytest.raises(RootUnavailable) as caught:
        sql.read("local-db", "SELECT 1")

    assert str(caught.value).startswith(
        "`local-db` names ingestion root `shared`, which this worker does not have."
    )


@pytest.mark.parametrize(
    ("make", "fragment"),
    [
        (lambda p: p.mkdir(), "is not a regular file"),
        (lambda p: os.mkfifo(p), "is not a regular file"),
    ],
)
def test_sqlite_refuses_a_path_that_is_not_a_regular_file(
    lite: Path, make: Callable[[Path], None], fragment: str
) -> None:
    """A directory or a pipe is refused before the driver opens it."""
    make(lite)

    with pytest.raises(DirectoryError, match=fragment) as caught:
        sql.read("local-db", "SELECT 1")

    assert str(caught.value).startswith("`local-db`: ")


def test_sqlite_needs_a_file_path(lite: Path) -> None:
    """A connection whose path names only the root has no file to open."""
    _task(
        {"local-db": _lite_body()},
        {
            "local-db": {
                "kind": "sqlite-file",
                "root": "shared",
                "root_dir": str(lite.parent.parent),
                "path": ".",
            }
        },
    )

    with pytest.raises(DirectoryError, match="a file path is needed"):
        sql.read("local-db", "SELECT 1")


def test_sqlite_a_driver_that_cannot_open_the_file_may_pass_next_time(
    lite: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An error at open is the source's, retried."""
    import adbc_driver_sqlite.dbapi as lite_driver
    from adbc_driver_manager import AdbcStatusCode

    _sqlite(lite, ["CREATE TABLE t(a)"])

    def refuse(uri: str) -> None:
        raise lite_driver.OperationalError(
            f"unable to open database file {uri}", status_code=AdbcStatusCode.IO
        )

    monkeypatch.setattr(lite_driver, "connect", refuse)

    with pytest.raises(SourceError, match="unable to open database file") as caught:
        sql.read("local-db", "SELECT 1")

    assert type(caught.value) is SourceError


@pytest.mark.parametrize(
    ("query", "kind", "fragment"),
    [
        ("SELEC 1", UsageError, "the query failed \\(SQLite said: .*syntax error"),
        ("SELECT * FROM missing", UsageError, "the query failed \\(SQLite said: .*no such table"),
    ],
)
def test_sqlite_a_query_error_is_classed(
    lite: Path, query: str, kind: type[Exception], fragment: str
) -> None:
    """Syntax and missing names repeat on every attempt."""
    _sqlite(lite, ["CREATE TABLE t(a)"])

    with pytest.raises(kind, match=fragment) as caught:
        sql.read("local-db", query)

    assert type(caught.value) is kind


def test_sqlite_a_file_that_is_not_a_database_is_the_sources_error(lite: Path) -> None:
    """SQLite says so only when it reads; the message keeps its words."""
    lite.write_bytes(b"not a database, just text" * 100)

    with pytest.raises(SourceError, match="not a database") as caught:
        sql.read("local-db", "SELECT 1")

    assert type(caught.value) is SourceError


# --- MariaDB through a fake PyMySQL -------------------------------------------------------------


@dataclass
class _Field:
    """A PyMySQL field description."""

    name: str
    type_code: int
    flags: int = 0
    scale: int = 0
    length: int = 0
    charsetnr: int = 45


@dataclass
class _Result:
    """PyMySQL's result, as far as `sql.read` reads it."""

    fields: list[_Field]


class _MyError(Exception):
    """PyMySQL's error: ``args`` is ``(code, message)``."""


def _literal(value: object, mode: str) -> str:
    """Escape one value as PyMySQL's `Connection.escape` does.

    Parameters
    ----------
    value
        The value.
    mode
        The session's ``sql_mode``; `NO_BACKSLASH_ESCAPES` changes how a str is escaped.

    Returns
    -------
    str
        The text PyMySQL writes into the query.
    """
    if isinstance(value, str):
        if "NO_BACKSLASH_ESCAPES" in mode:
            return "'" + value.replace("'", "''") + "'"
        return "'" + real_converters.escape_string(value) + "'"
    if isinstance(value, (bytes, bytearray)):
        return f"X'{value.hex()}'"
    return real_converters.escape_item(value, "utf8mb4")


_SQL_MODE = "SELECT @@SESSION.sql_mode"


@dataclass
class _MyCursor:
    """A fake unbuffered cursor that formats its query as PyMySQL does."""

    rows: list[tuple[object, ...]]
    fields: list[_Field]
    fail: Exception | None = None
    executed: list[tuple[str, object]] = field(default_factory=list)
    sent: list[str] = field(default_factory=list)
    _result: _Result | None = None
    fetched: list[int] = field(default_factory=list)
    mode: str = "STRICT_TRANS_TABLES"
    no_rows: bool = False

    @property
    def description(self) -> tuple[object, ...] | None:
        """Say whether the last statement returned rows.

        Returns
        -------
        tuple or None
            `None` for a statement without a result set.
        """
        if self._result is None or self.no_rows:
            return None
        return tuple(self._result.fields)

    def execute(self, query: str, args: object = None) -> None:
        """Record a statement and the text PyMySQL would send (``query % args`` with args).

        Parameters
        ----------
        query
            The SQL.
        args
            Its values.

        Raises
        ------
        Exception
            The configured error, for the asset's query.
        """
        self.executed.append((query, args))
        if isinstance(args, Mapping):
            query %= {key: _literal(value, self.mode) for key, value in args.items()}
        self.sent.append(query)
        if query not in (
            "SET SESSION TRANSACTION READ ONLY",
            "START TRANSACTION READ ONLY",
            _SQL_MODE,
        ):
            if self.fail is not None:
                raise self.fail
            self._result = _Result(self.fields)

    def fetchone(self) -> tuple[object, ...]:
        """Answer the ``sql_mode`` probe.

        Returns
        -------
        tuple
            The session's mode.
        """
        return (self.mode,)

    def fetchmany(self, size: int) -> list[tuple[object, ...]]:
        """Hand out up to `size` rows.

        Parameters
        ----------
        size
            The batch size.

        Returns
        -------
        list
            The rows.
        """
        self.fetched.append(size)
        out, self.rows = self.rows[:size], self.rows[size:]
        return out


@dataclass
class _MyConnection:
    """A fake `pymysql.connections.Connection`."""

    kwargs: dict[str, Any]
    cursor_: _MyCursor
    refuse: Exception | None = None
    status: int | None = None
    socks: list[object] = field(default_factory=list)

    @property
    def server_status(self) -> int:
        """Answer PyMySQL's status: NO_BACKSLASH_ESCAPES (512) as the session's mode says.

        Returns
        -------
        int
            The status, or the one a test set.
        """
        if self.status is not None:
            return self.status
        return 512 if "NO_BACKSLASH_ESCAPES" in self.cursor_.mode else 0

    def connect(self, sock: object = None) -> None:
        """Record the socket handed over, or refuse.

        Parameters
        ----------
        sock
            The connected socket under the Docker runner.

        Raises
        ------
        Exception
            The configured refusal.
        """
        self.socks.append(sock)
        if self.refuse is not None:
            raise self.refuse

    def cursor(self, kind: object = None) -> _MyCursor:
        """Return the cursor.

        Parameters
        ----------
        kind
            The cursor class.

        Returns
        -------
        _MyCursor
            It.
        """
        return self.cursor_

    def close(self) -> None:
        """Close nothing."""


@pytest.fixture
def mysql(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Put a fake PyMySQL where `sql.read` imports it; the relay resolves and accepts.

    Parameters
    ----------
    monkeypatch
        Installs the modules.

    Returns
    -------
    dict
        ``cursor`` (rows and fields to hand out), ``made`` (each connection made), ``dialed``
        (each address a socket was opened to), ``refuse`` and ``status`` to set.
    """
    state: dict[str, Any] = {
        "cursor": _MyCursor([(1, "x")], [_Field("id", 3), _Field("name", 253)]),
        "made": [],
        "dialed": [],
        "refuse": None,
        "status": None,
    }

    def make(**kwargs: object) -> _MyConnection:
        made = _MyConnection(kwargs, state["cursor"], state["refuse"], state["status"])
        state["made"].append(made)
        return made

    module = types.ModuleType("pymysql")
    module.connections = types.SimpleNamespace(Connection=make)  # ty: ignore[unresolved-attribute]
    module.cursors = types.SimpleNamespace(SSCursor=object)  # ty: ignore[unresolved-attribute]
    module.MySQLError = _MyError  # ty: ignore[unresolved-attribute]
    constants = types.ModuleType("pymysql.constants")
    constants.SERVER_STATUS = types.SimpleNamespace(  # ty: ignore[unresolved-attribute]
        SERVER_STATUS_NO_BACKSLASH_ESCAPES=512
    )
    monkeypatch.setitem(sys.modules, "pymysql", module)
    monkeypatch.setitem(sys.modules, "pymysql.constants", constants)
    monkeypatch.setitem(sys.modules, "pymysql.cursors", module.cursors)
    monkeypatch.setattr(sql.socket, "gethostbyname", lambda name: "10.207.0.3")
    monkeypatch.setattr(
        sql.socket,
        "create_connection",
        lambda address, timeout: state["dialed"].append(address) or "SOCKET",
    )
    return state


def _mariadb(*, relay: bool, tls: str = "verify-full") -> None:
    """Declare `shop-db`, a MariaDB connection.

    Parameters
    ----------
    relay
        Whether the task runs isolated.
    tls
        The connection's ``tls``.
    """
    body = {
        "name": "shop-db",
        "kind": "mariadb",
        "address": {"host": "db.example.com", "port": 3306, "database": "shop"},
        "settings": {"user": "reader", "tls": tls},
        "endpoints": [{"host": "db.example.com", "port": 3306, "role": "service"}],
        "secret": {"password": _PASSWORD},
    }
    routing: dict[str, object] = {"kind": "mariadb", "grant": [["db.example.com", 3306]]}
    if relay:
        routing["relay_port"] = 15002
    _task({"shop-db": body}, {"shop-db": routing})


def test_mariadb_under_docker_hands_pymysql_a_socket_to_the_relay(mysql: dict[str, Any]) -> None:
    """The real host for TLS; the relay's socket for the bytes; read-only, UTC, no LOCAL INFILE."""
    _mariadb(relay=True)

    table = sql.read("shop-db", "SELECT id, name FROM t")

    (made,) = mysql["made"]
    kwargs = made.kwargs
    assert kwargs["host"] == "db.example.com" and kwargs["port"] == 3306
    assert kwargs["user"] == "reader" and kwargs["database"] == "shop"
    assert kwargs["defer_connect"] is True and kwargs["local_infile"] is False
    assert kwargs["ssl_disabled"] is False and kwargs["ssl"].check_hostname is True
    assert kwargs["init_command"] == "SET time_zone = '+00:00'"
    assert mysql["dialed"] == [("10.207.0.3", 15002)] and made.socks == ["SOCKET"]
    assert mysql["cursor"].sent == [
        "SET SESSION TRANSACTION READ ONLY",
        "START TRANSACTION READ ONLY",
        _SQL_MODE,
        "SELECT id, name FROM t",
    ]
    assert table.to_pylist() == [{"id": 1, "name": "x"}]


def test_mariadb_locally_connects_itself_and_disable_turns_tls_off(mysql: dict[str, Any]) -> None:
    """PyMySQL would try TLS without verifying it unless told `ssl_disabled`."""
    _mariadb(relay=False, tls="disable")

    sql.read("shop-db", "SELECT id, name FROM t")

    (made,) = mysql["made"]
    assert made.socks == [None] and mysql["dialed"] == []
    assert made.kwargs["ssl"] is None and made.kwargs["ssl_disabled"] is True


def test_mariadb_binds_an_aware_time_as_utc_and_reads_in_batches(mysql: dict[str, Any]) -> None:
    """A DATETIME has no zone: 12:00+02:00 is bound as 10:00. Rows come `BATCH_ROWS` at a time."""
    import datetime as dt

    mysql["cursor"].rows = [(i, "x") for i in range(3)]
    _mariadb(relay=False)
    since = dt.datetime(2026, 1, 1, 12, tzinfo=dt.timezone(dt.timedelta(hours=2)))

    sql.read(
        "shop-db",
        "SELECT id, name FROM t WHERE u > :since AND n LIKE '5%'",
        params={"since": since},
    )

    assert mysql["cursor"].executed[-1] == (
        "SELECT id, name FROM t WHERE u > %(since)s AND n LIKE '5%%'",
        {"since": dt.datetime(2026, 1, 1, 10)},
    )
    assert mysql["cursor"].sent[-1] == (
        "SELECT id, name FROM t WHERE u > '2026-01-01 10:00:00' AND n LIKE '5%'"
    )
    assert mysql["cursor"].fetched[0] == sql.BATCH_ROWS


def test_mariadb_follows_the_servers_backslash_mode(mysql: dict[str, Any]) -> None:
    r"""With NO_BACKSLASH_ESCAPES a backslash ends nothing, so `'a\\'` is a whole literal."""
    mysql["cursor"].mode = "NO_BACKSLASH_ESCAPES"
    _mariadb(relay=False)

    sql.read("shop-db", "SELECT 'a\\' AS v, :x AS w", params={"x": "it's"})

    assert mysql["cursor"].sent[-1] == "SELECT 'a\\' AS v, 'it''s' AS w"


@pytest.mark.parametrize(
    ("error", "kind", "fragment"),
    [
        (
            _MyError(1045, "Access denied for user 'reader'"),
            SourceRefused,
            r"authentication failed \(MariaDB said: Access denied",
        ),
        (
            _MyError(1792, "Cannot execute statement in a READ ONLY transaction"),
            UsageError,
            "read-only transaction",
        ),
        (_MyError(1064, "You have an error in your SQL syntax"), UsageError, "the query failed"),
        (_MyError(1142, "SELECT command denied"), SourceRefused, "may not read this"),
        (_MyError(2003, "Can't connect to MySQL server"), SourceError, "Can't connect"),
    ],
)
def test_mariadb_errors_are_classed_by_their_code(
    mysql: dict[str, Any], error: Exception, kind: type[Exception], fragment: str
) -> None:
    """Access denied and syntax repeat; a connection failure may pass."""
    mysql["refuse"] = error if error.args[0] in (1045, 2003) else None
    mysql["cursor"].fail = None if error.args[0] in (1045, 2003) else error
    _mariadb(relay=False)

    with pytest.raises(kind, match=fragment) as caught:
        sql.read("shop-db", "SELECT 1")

    assert type(caught.value) is kind


def test_mariadb_a_certificate_failure_gives_the_tls_text(mysql: dict[str, Any]) -> None:
    """PyMySQL wraps the TLS error and keeps it as `original_exception`."""
    import ssl

    refused = _MyError(2003, "Can't connect to MySQL server on 'db.example.com'")
    refused.original_exception = ssl.SSLCertVerificationError("certificate verify failed")  # ty: ignore[unresolved-attribute]
    mysql["refuse"] = refused
    _mariadb(relay=True)

    with pytest.raises(SourceRefused, match=r"not trusted for `db\.example\.com`"):
        sql.read("shop-db", "SELECT 1")


def test_mariadb_a_lost_connection_under_docker_points_to_the_relay(mysql: dict[str, Any]) -> None:
    """Review Focus 3 for MariaDB."""
    mysql["refuse"] = _MyError(2013, "Lost connection to MySQL server during query")
    _mariadb(relay=True)

    with pytest.raises(SourceError, match="relay closed before MariaDB answered"):
        sql.read("shop-db", "SELECT 1")


def test_mariadb_schema_comes_from_the_field_descriptions() -> None:
    """Types from the description, never from the values (the integration test runs a server)."""
    fields = [
        _Field("t", 1),
        _Field("i", 3),
        _Field("u", 8, flags=32),
        _Field("d", 246, scale=2, length=12),
        _Field("f", 5),
        _Field("day", 10),
        _Field("at", 12),
        _Field("ts", 7),
        _Field("v", 253),
        _Field("j", 245),
        _Field("b", 252, charsetnr=63),
        _Field("tm", 11),
    ]

    schema = _types.mariadb_schema(fields, "shop-db")

    assert schema.types == [
        pa.int16(),
        pa.int64(),
        pa.decimal128(20, 0),
        pa.decimal128(10, 2),
        pa.float64(),
        pa.date32(),
        pa.timestamp("us"),
        pa.timestamp("us", "UTC"),
        pa.string(),
        pa.string(),
        pa.binary(),
        pa.time64("us"),
    ]


def test_mariadb_geometry_and_a_zero_date_are_named() -> None:
    """A GEOMETRY needs ST_AsText; `0000-00-00` arrives as text, which no date holds."""
    with pytest.raises(UsageError, match=r"ST_AsText\(g\)"):
        _types.mariadb_schema([_Field("g", 255)], "shop-db")
    schema = _types.mariadb_schema([_Field("at", 12)], "shop-db")
    with pytest.raises(UsageError, match=r"column `at` holds '0000-00-00 00:00:00'.*NULLIF"):
        _types.mariadb_batch([("0000-00-00 00:00:00",)], schema, "shop-db")


def test_mysqls_json_is_text_although_its_character_set_is_binary() -> None:
    """MySQL 8 describes a JSON column with the binary character set; PyMySQL decodes it."""
    schema = _types.mariadb_schema([_Field("j", 245, charsetnr=63)], "shop-db")

    assert schema.types == [pa.string()]
    assert _types.mariadb_batch([('{"a": 1}',)], schema, "shop-db").to_pylist() == [
        {"j": '{"a": 1}'}
    ]


def _shop_body(secret: Mapping[str, str] | None) -> None:
    """Declare `shop-db` locally with a given secret.

    Parameters
    ----------
    secret
        The secret's values, or `None` for no secret.
    """
    body = {
        "name": "shop-db",
        "kind": "mariadb",
        "address": {"host": "db.example.com", "port": 3306, "database": "shop"},
        "settings": {"user": "reader"},
        "endpoints": [],
        "secret": secret,
    }
    _task({"shop-db": body}, {"shop-db": {"kind": "mariadb"}})


def test_mariadb_without_a_password_names_the_command_that_stores_it(
    mysql: dict[str, Any],
) -> None:
    """Nothing is dialled without a password."""
    _shop_body(None)

    with pytest.raises(UsageError, match=r"`shop-db` has no secret with `password`"):
        sql.read("shop-db", "SELECT 1")

    assert mysql["made"] == []


def test_mariadb_without_the_assets_extra_says_what_to_add(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PyMySQL comes with `lakana[assets]`."""
    monkeypatch.setitem(sys.modules, "pymysql", None)
    _shop_body({"password": _PASSWORD})

    with pytest.raises(UsageError, match=r"add lakana\[assets\]"):
        sql.read("shop-db", "SELECT 1")


def test_mariadb_an_unreadable_trust_bundle_names_the_worker_setting(
    mysql: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The bundle is read for the TLS context, before any connection."""
    monkeypatch.setenv("LAKANA_CA_BUNDLE", str(tmp_path / "missing.pem"))
    _shop_body({"password": _PASSWORD})

    with pytest.raises(UsageError, match=r"trust bundle .*LAKANA_RUN__CA_BUNDLE"):
        sql.read("shop-db", "SELECT 1")

    assert mysql["made"] == []


def test_mariadb_caching_sha2_without_tls_names_both_ways_out(mysql: dict[str, Any]) -> None:
    """MySQL 8's default plugin without TLS needs `cryptography`, which PyMySQL lacks."""
    mysql["refuse"] = RuntimeError(
        "'cryptography' package is required for sha256_password or caching_sha2_password auth"
    )
    _mariadb(relay=False, tls="disable")

    with pytest.raises(UsageError, match=r"use tls: verify-full, or add cryptography"):
        sql.read("shop-db", "SELECT 1")


@pytest.mark.parametrize(
    ("field", "kind"),
    [(_Field("s", 2), pa.int32()), (_Field("m", 9), pa.int32()), (_Field("f", 4), pa.float32())],
)
def test_mariadb_narrow_types_widen_to_what_iceberg_holds(field: _Field, kind: pa.DataType) -> None:
    """SMALLINT and MEDIUMINT as int32, FLOAT as float32."""
    assert _types.mariadb_schema([field], "shop-db").types == [kind]


@pytest.mark.parametrize(
    ("field", "cast"),
    [
        (_Field("d", 246, scale=2, length=67), "CAST(d AS DOUBLE)"),
        (_Field("b", 16), "CAST(b AS UNSIGNED)"),
        (_Field("x", 242), "CAST(x AS CHAR)"),
    ],
)
def test_mariadb_a_wide_decimal_a_bit_and_an_unknown_type_are_refused(
    field: _Field, cast: str
) -> None:
    """Each names the column and a cast."""
    with pytest.raises(UsageError) as caught:
        _types.mariadb_schema([field], "shop-db")

    assert cast in str(caught.value)


def test_mariadb_a_time_of_day_is_read_and_a_longer_time_is_refused() -> None:
    """PyMySQL hands over a TIME as a timedelta, which may pass a day (`'30:00:00'`)."""
    import datetime as dt

    schema = _types.mariadb_schema([_Field("tm", 11)], "shop-db")
    rows: list[tuple[object, ...]] = [(dt.timedelta(hours=13, microseconds=5),), (None,)]

    batch = _types.mariadb_batch(rows, schema, "shop-db")

    assert batch.column(0).to_pylist() == [dt.time(13, 0, 0, 5), None]
    with pytest.raises(
        UsageError, match=r"column `tm` holds datetime.timedelta.*TIME_TO_SEC\(tm\)"
    ):
        _types.mariadb_batch([(dt.timedelta(hours=30),)], schema, "shop-db")


def test_mariadb_the_value_named_is_the_first_that_does_not_fit() -> None:
    """A good date before the zero date is not the one named."""
    import datetime as dt

    schema = _types.mariadb_schema([_Field("day", 10)], "shop-db")

    with pytest.raises(UsageError, match=r"holds '0000-00-00'"):
        _types.mariadb_batch([(dt.date(2026, 1, 1),), ("0000-00-00",)], schema, "shop-db")


@pytest.mark.parametrize(
    "query",
    [
        "ATTACH DATABASE '{dir}/x.db' AS o",
        "  -- a comment\n/* another */ attach 'file:{dir}/x.db?mode=rwc' AS o; SELECT 1",
        "DETACH DATABASE o",
        ";ATTACH DATABASE '{dir}/semi.db' AS o",
        ";;  ATTACH DATABASE '{dir}/semi.db' AS o",
        "/*x*/; -- y\n ATTACH DATABASE '{dir}/semi.db' AS o",
        "/* /* */ ATTACH DATABASE '{dir}/nest.db' AS o; */",
    ],
)
def test_sqlite_refuses_attach_and_detach(lite: Path, query: str) -> None:
    """ATTACH creates an empty file even read-only; only the first statement runs."""
    _sqlite(lite, ["CREATE TABLE t(a)"])

    with pytest.raises(UsageError, match=r"ATTACH and DETACH"):
        sql.read("local-db", query.format(dir=lite.parent))

    assert sorted(p.name for p in lite.parent.iterdir()) == ["app.db"]


def test_mariadb_without_parameters_sends_every_percent_as_written(mysql: dict[str, Any]) -> None:
    """PyMySQL formats `%%` back to `%` only when it is given values: it always is."""
    _mariadb(relay=False)
    query = "SELECT DATE_FORMAT(d, '%Y-%m') AS m, '100%' AS p, a % 2 AS r FROM t"

    sql.read("shop-db", query)

    assert mysql["cursor"].sent[-1] == query


@pytest.mark.parametrize(
    "value",
    [
        ["x' OR 1=1 -- "],
        ("a", "b"),
        {"a"},
        frozenset({"a"}),
        {"a": 1},
        Path("x' OR 1=1 #"),
        object(),
    ],
    ids=["list", "tuple", "set", "frozenset", "dict", "path", "object"],
)
def test_mariadb_refuses_a_parameter_that_is_not_a_plain_value(
    mysql: dict[str, Any], value: object
) -> None:
    """PyMySQL escapes such values with backslashes whatever the server's mode: refused first."""
    _mariadb(relay=False)

    with pytest.raises(UsageError, match=r"parameter `v` is a .*str, int, float, bool, Decimal"):
        sql.read("shop-db", "SELECT 1 FROM t WHERE a IN (:v)", params={"v": value})

    assert mysql["made"] == []


@pytest.mark.parametrize(
    "value", [float("nan"), float("inf"), __import__("decimal").Decimal("NaN")]
)
def test_mariadb_refuses_a_value_that_is_not_finite(mysql: dict[str, Any], value: object) -> None:
    """MariaDB has no NaN or infinity."""
    _mariadb(relay=False)

    with pytest.raises(UsageError, match=r"parameter `v` is not a finite number"):
        sql.read("shop-db", "SELECT :v", params={"v": value})


def test_mariadb_binds_each_plain_type(mysql: dict[str, Any]) -> None:
    """str, int, float, bool, Decimal, date, datetime, time and bytes reach PyMySQL as they are."""
    import datetime as dt
    import decimal

    mysql["cursor"].mode = "NO_BACKSLASH_ESCAPES"
    values: dict[str, object] = {
        "s": "x' OR 1=1 -- ",
        "i": 7,
        "f": 1.5,
        "b": True,
        "d": decimal.Decimal("1.25"),
        "day": dt.date(2026, 1, 2),
        "at": dt.datetime(2026, 1, 2, 3, 4, 5),
        "tm": dt.time(3, 4, 5),
        "raw": b"\x00'",
    }
    _mariadb(relay=False)

    sql.read("shop-db", "SELECT " + ", ".join(f":{name}" for name in values), params=values)

    assert mysql["cursor"].sent[-1] == (
        "SELECT 'x'' OR 1=1 -- ', 7, 1.5e0, 1, 1.25, '2026-01-02', '2026-01-02 03:04:05', "
        "'03:04:05', X'0027'"
    )


_QUOTE_PROBE = 'SELECT "a\\" FROM t WHERE x = ":v"'


@pytest.mark.parametrize(
    ("mode", "sent"),
    [
        ("STRICT_TRANS_TABLES", 'SELECT "a\\" FROM t WHERE x = "\'1\'"'),
        ("ANSI_QUOTES", None),
        ("NO_BACKSLASH_ESCAPES", None),
        ("REAL_AS_FLOAT,PIPES_AS_CONCAT,ANSI_QUOTES,IGNORE_SPACE,ANSI,NO_BACKSLASH_ESCAPES", None),
    ],
    ids=["default", "ansi-quotes", "no-backslash", "both"],
)
def test_mariadb_reads_double_quotes_as_the_sessions_sql_mode_says(
    mysql: dict[str, Any], mode: str, sent: str | None
) -> None:
    r"""``"a\"`` is a string with escapes, else an identifier or a string that ends at the `\`."""
    mysql["cursor"].mode = mode
    _mariadb(relay=False)

    if sent is None:
        with pytest.raises(UsageError, match="params has v, which the query does not use"):
            sql.read("shop-db", _QUOTE_PROBE, params={"v": "1"})
    else:
        sql.read("shop-db", _QUOTE_PROBE, params={"v": "1"})
        assert mysql["cursor"].sent[-1] == sent


@pytest.mark.parametrize(
    "code", [1037, 1038, 1041, 1053, 1158, 1159, 1160, 1161, 1203, 1226, 1927, 1969]
)
def test_mariadb_transient_server_errors_are_retried(mysql: dict[str, Any], code: int) -> None:
    """Out of memory, shutdown, network, limits, a killed connection, a statement timeout."""
    mysql["cursor"].fail = _MyError(code, "try again")
    _mariadb(relay=False)

    with pytest.raises(SourceError) as caught:
        sql.read("shop-db", "SELECT 1")

    assert type(caught.value) is SourceError


def test_mariadb_a_statement_without_rows_is_refused(mysql: dict[str, Any]) -> None:
    """SET, DO and SELECT ... INTO @v return no result set."""
    mysql["cursor"].no_rows = True
    _mariadb(relay=False)

    with pytest.raises(UsageError, match=r"sql\.read needs a statement that returns rows"):
        sql.read("shop-db", "SELECT 1 INTO @a")


@pytest.mark.parametrize(
    ("error", "fragment"),
    [
        (_MyError(2026, "SSL is required but the server doesn't support it"), "offers no TLS"),
        (
            _MyError(
                3159,
                "Connections using insecure transport are prohibited while "
                "--require_secure_transport=ON.",
            ),
            "requires TLS",
        ),
    ],
)
def test_mariadb_a_tls_mismatch_is_refused_with_a_tls_hint(
    mysql: dict[str, Any], error: Exception, fragment: str
) -> None:
    """Every attempt would fail the same way; the message names the `tls` setting."""
    mysql["refuse"] = error
    _mariadb(relay=False)

    with pytest.raises(SourceRefused, match=fragment) as caught:
        sql.read("shop-db", "SELECT 1")

    assert "lakana connection update shop-db --env prod --tls" in str(caught.value)


def test_mariadb_a_none_parameter_names_the_default_to_give(mysql: dict[str, Any]) -> None:
    """The type check lets `None` through to the message every kind gives."""
    _mariadb(relay=False)

    with pytest.raises(UsageError, match=r"ctx\.watermark\(default=\.\.\.\)"):
        sql.read("shop-db", "SELECT :since", params={"since": None})


def test_sqlite_a_column_name_in_a_message_cannot_forge_a_log_line(lite: Path) -> None:
    """The name comes from the file: a newline in it is shown escaped."""
    rows = ", ".join(["(1)"] * 2000)
    _sqlite(
        lite,
        [
            'CREATE TABLE t("a\nlakana: forged")',
            f"INSERT INTO t VALUES {rows}",
            "INSERT INTO t VALUES ('x')",
        ],
    )

    with pytest.raises(UsageError) as caught:
        sql.read("local-db", "SELECT * FROM t")

    assert "\n" not in str(caught.value)


_NOT_PLAIN: list[object] = [
    [1],
    {"a": 1},
    object(),
    float("nan"),
    float("inf"),
    __import__("decimal").Decimal("sNaN"),
    __import__("decimal").Decimal("Infinity"),
]


@pytest.mark.parametrize(
    "value", _NOT_PLAIN, ids=["list", "dict", "object", "nan", "inf", "snan", "dinf"]
)
def test_postgresql_refuses_a_value_that_is_not_a_plain_finite_scalar(
    driver: _Driver, value: object
) -> None:
    """The same rule as for MariaDB, before any connection."""
    _postgres(relay=False)

    with pytest.raises(UsageError, match=r"parameter `v` "):
        sql.read("crm-db", "SELECT :v", params={"v": value})

    assert driver.seen == []


@pytest.mark.parametrize(
    "value",
    [*_NOT_PLAIN, __import__("decimal").Decimal("1.5"), __import__("datetime").time(1, 2)],
    ids=["list", "dict", "object", "nan", "inf", "snan", "dinf", "decimal", "time"],
)
def test_sqlite_refuses_a_value_it_cannot_bind_faithfully(lite: Path, value: object) -> None:
    """SQLite binds NaN as NULL and cannot bind a Decimal or a time: refused, never retried."""
    _sqlite(lite, ["CREATE TABLE t(a)"])

    with pytest.raises(UsageError, match=r"parameter `v` "):
        sql.read("local-db", "SELECT a FROM t WHERE a > :v", params={"v": value})


@pytest.mark.parametrize("value", [float("nan"), __import__("decimal").Decimal("sNaN")])
def test_mariadb_refuses_a_signalling_or_quiet_nan(mysql: dict[str, Any], value: object) -> None:
    """`math.isfinite` raises on a signalling NaN; the check must not."""
    _mariadb(relay=False)

    with pytest.raises(UsageError, match=r"parameter `v` is not a finite number"):
        sql.read("shop-db", "SELECT :v", params={"v": value})


def test_a_parameter_name_that_is_not_text_is_refused(driver: _Driver) -> None:
    """A placeholder is a name: an int key cannot match one."""
    _postgres(relay=False)

    with pytest.raises(UsageError, match=r"params= keys are placeholder names"):
        sql.read("crm-db", "SELECT 1", params={1: 2})  # ty: ignore[invalid-argument-type]


@pytest.mark.parametrize(
    ("query", "keyword"),
    [
        ("DROP TABLE t", "DROP"),
        ("truncate t", "TRUNCATE"),
        ("CREATE TABLE x (a INT)", "CREATE"),
        ("SET STATEMENT transaction_read_only=0 FOR DROP TABLE t", "SET"),
        ("CALL p()", "CALL"),
        ("  /* c */ # c\n -- c\n ; ( DROP TABLE t", "DROP"),
        ("'x' DROP TABLE t", "DROP"),
    ],
)
def test_mariadb_runs_only_a_statement_that_reads(
    mysql: dict[str, Any], query: str, keyword: str
) -> None:
    """DDL commits before a READ ONLY transaction can stop it: refused before it is sent."""
    _mariadb(relay=False)

    with pytest.raises(UsageError, match=rf"starts with {keyword}") as caught:
        sql.read("shop-db", query)

    assert caught.value.retryable is False
    assert query not in mysql["cursor"].sent


@pytest.mark.parametrize(
    "query",
    [
        "SELECT 1",
        "(SELECT 1)",
        "WITH x AS (SELECT 1) SELECT * FROM x",
        "VALUES (1)",
        "TABLE t",
        "/* DROP */ select 'DROP TABLE t'",
    ],
)
def test_mariadb_a_statement_that_reads_is_sent(mysql: dict[str, Any], query: str) -> None:
    """SELECT, WITH, VALUES and TABLE; a keyword inside a comment or a literal does not count."""
    _mariadb(relay=False)

    sql.read("shop-db", query)

    assert mysql["cursor"].sent[-1] == query


@pytest.mark.parametrize(
    "query",
    ["SELECT a INTO OUTFILE '/tmp/x' FROM t", "SELECT a FROM t INTO  dumpfile '/tmp/x'"],
)
def test_mariadb_refuses_select_into_a_file(mysql: dict[str, Any], query: str) -> None:
    """A file on the server is a write the read-only transaction does not stop."""
    _mariadb(relay=False)

    with pytest.raises(UsageError, match=r"INTO OUTFILE"):
        sql.read("shop-db", query)

    assert mysql["cursor"].sent[-1] != query


def test_mariadb_sets_the_session_read_only_before_the_transaction(mysql: dict[str, Any]) -> None:
    """Both, and the sql_mode is read inside the transaction (a proxy pins it there)."""
    _mariadb(relay=False)

    sql.read("shop-db", "SELECT 1")

    assert mysql["cursor"].sent == [
        "SET SESSION TRANSACTION READ ONLY",
        "START TRANSACTION READ ONLY",
        _SQL_MODE,
        "SELECT 1",
    ]


@pytest.mark.parametrize("mode", ["MSSQL", "STRICT_TRANS_TABLES,ORACLE", "DB2"])
def test_mariadb_refuses_a_sql_mode_it_does_not_model(mysql: dict[str, Any], mode: str) -> None:
    """MSSQL reads `[...]` as a name, ORACLE has its own parser: no telling where a value lands."""
    mysql["cursor"].mode = mode
    _mariadb(relay=False)

    with pytest.raises(UsageError, match=r"sql_mode .*which sql\.read does not know"):
        sql.read("shop-db", "SELECT 1")


def test_mariadb_a_session_whose_status_and_mode_disagree_is_refused(
    mysql: dict[str, Any],
) -> None:
    """PyMySQL escapes by the status flag, the tokenizer by sql_mode: they must agree."""
    mysql["cursor"].mode = "NO_BACKSLASH_ESCAPES"
    mysql["status"] = 0
    _mariadb(relay=False)

    with pytest.raises(SourceError, match="disagree"):
        sql.read("shop-db", "SELECT :v", params={"v": "x"})

    assert mysql["cursor"].sent[-1] == _SQL_MODE


def test_mariadb_insecure_transport_in_a_syntax_error_is_not_a_tls_error(
    mysql: dict[str, Any],
) -> None:
    """Only the server's code 3159 means the server requires TLS."""
    mysql["cursor"].fail = _MyError(1064, "syntax error near 'insecure transport'")
    _mariadb(relay=False)

    with pytest.raises(UsageError, match="the query failed"):
        sql.read("shop-db", "SELECT 'insecure transport' x y")


def test_sqlite_refuses_a_datetime_that_would_compare_as_other_text(lite: Path) -> None:
    """ADBC binds `2026-01-01T06:00:00`; SQLite stores `2026-01-01 07:00:00`, which sorts lower."""
    import datetime as dt

    _sqlite(
        lite,
        [
            "CREATE TABLE t(a INTEGER, at TEXT)",
            "INSERT INTO t VALUES (1, '2026-01-01 05:00:00'), (2, '2026-01-02 00:00:00'), "
            "(3, '2026-01-01 07:00:00')",
        ],
    )
    query = "SELECT a FROM t WHERE at > :x ORDER BY a"

    with pytest.raises(UsageError, match=r"Pass a str in the column's own format.*datetime\(:x\)"):
        sql.read("local-db", query, params={"x": dt.datetime(2026, 1, 1, 6)})
    as_text = sql.read("local-db", query, params={"x": "2026-01-01 06:00:00"})
    with_function = sql.read(
        "local-db",
        "SELECT a FROM t WHERE datetime(at) > datetime(:x) ORDER BY a",
        params={"x": "2026-01-01T06:00:00"},
    )

    assert as_text.column("a").to_pylist() == [2, 3]
    assert with_function.column("a").to_pylist() == [2, 3]


def test_sqlite_binds_a_date_as_its_own_stored_text(lite: Path) -> None:
    """A date binds as `2026-01-02`, the format SQLite's date() writes: no mismatch."""
    import datetime as dt

    _sqlite(lite, ["CREATE TABLE t(a INTEGER, d TEXT)", "INSERT INTO t VALUES (1, '2026-01-02')"])

    table = sql.read("local-db", "SELECT a FROM t WHERE d = :d", params={"d": dt.date(2026, 1, 2)})

    assert table.column("a").to_pylist() == [1]
