"""`sql.read` against a real PostgreSQL over TLS through the real relay, and a real SQLite file.

PostgreSQL 18 serves TLS with a leaf certificate for ``db.test`` from a test authority. The
task reaches it the way it does under the Docker runner: libpq dials ``hostaddr`` (the relay,
an in-process `lakana_worker.launcher.relay.serve` on ``127.0.0.6``, inside its plan's /29)
while TLS verifies the connection's real host name. The images, their digests and licences are
recorded in `docs/agent/testing.md` ("Database images").
"""

from __future__ import annotations

import contextlib
import ipaddress
import socket
import sqlite3
import sys
import threading
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pyarrow as pa
import pytest
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy
from tls_fixture import Authority, issue_leaf, make_authority

from lakana import context
from lakana.ingest import CapExceeded, SourceRefused, UsageError, _check, _connections, sql
from lakana_worker.launcher.egress import EgressSettings
from lakana_worker.launcher.relay import RelayPlan, serve
from lakana_worker.launcher.requests import RelayRoute
from lakana_worker.trust import build_trust_bundle

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        sys.platform != "linux", reason="the relay's /29 on 127.0.0.0/8 needs Linux's loopback"
    ),
]

POSTGRES = "postgres:18.6@sha256:edb0c08dc33befd0262ab3b5cf1c4f3f8cfd8b09e88c05b45adee29d6761ddb6"
RELAY_ADDRESS = "127.0.0.6"
RELAY_PORT = 15001
_READER_PASSWORD = "reader-pa55-0123456789"
_ADMIN_PASSWORD = "admin-pa55-0123456789"
_ROUTE_HEADER = "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\n"
_ROUTE_ATTACHED = "eth1\t0000090A\t00000000\t0001\t0\t0\t0\t00FFFFFF\n"
"""10.9.0.0/24: the network the relay reaches out through; loopback is not attached."""
_SETUP = """
CREATE ROLE reader LOGIN PASSWORD '{password}';
CREATE TABLE kinds (
    id uuid, amount numeric(12, 2), doc json, docb jsonb, tags int[], at timestamptz,
    n numeric, m money
);
INSERT INTO kinds VALUES (
    '00010203-0405-0607-0809-0a0b0c0d0e0f', 1234.50, '{{"a": 1}}', '{{"b": 2}}', '{{1,2}}',
    '2026-10-26 12:00:00+02', 1.5, 3.25
);
CREATE TABLE big AS SELECT g AS id, repeat('x', 100) AS pad FROM generate_series(1, 300000) g;
GRANT SELECT ON kinds, big TO reader;
GRANT INSERT ON kinds TO reader;
"""


@dataclass(frozen=True)
class _Pki:
    """The test authority, the server's certificate and key, and a trust bundle.

    Parameters
    ----------
    authority
        Signs the server's certificate.
    cert
        The leaf for ``db.test`` (PEM).
    key
        Its key (PEM).
    bundle
        Mozilla's roots and the authority, as the worker writes `LAKANA_CA_BUNDLE`.
    """

    authority: Authority
    cert: bytes
    key: bytes
    bundle: Path


@pytest.fixture(scope="module")
def pki(tmp_path_factory: pytest.TempPathFactory) -> _Pki:
    """Make the authority, a leaf for ``db.test`` and the bundle.

    Parameters
    ----------
    tmp_path_factory
        Pytest's directories.

    Returns
    -------
    _Pki
        The certificates.
    """
    directory = tmp_path_factory.mktemp("pki")
    authority = make_authority(directory)
    cert, key = issue_leaf(authority, directory, ("db.test",))
    bundle = directory / "bundle.pem"
    bundle.write_bytes(build_trust_bundle(authority.ca_file) or b"")
    return _Pki(authority, cert.read_bytes(), key.read_bytes(), bundle)


@pytest.fixture(scope="module")
def postgres(pki: _Pki) -> Iterator[tuple[str, int]]:
    """Run PostgreSQL 18 with TLS, a read-only role and the test tables.

    The key must belong to the server's user (uid 999) with mode 0600, which a file copied
    in cannot have, so the entry point copies it before it starts the server.

    Parameters
    ----------
    pki
        The certificates.

    Yields
    ------
    tuple of (str, int)
        The host and port it is published on.
    """
    start = (
        "install -d -o 999 -g 999 -m 0700 /etc/pg-ssl && "
        "install -o 999 -g 999 -m 0600 /certs/server.crt /certs/server.key /etc/pg-ssl/ && "
        "exec docker-entrypoint.sh postgres -c ssl=on "
        "-c ssl_cert_file=/etc/pg-ssl/server.crt -c ssl_key_file=/etc/pg-ssl/server.key"
    )
    container = DockerContainer(
        POSTGRES,
        # The server of the image's first-run setup listens on a socket only, never on TCP.
        _wait_strategy=LogMessageWaitStrategy('listening on IPv4 address "0.0.0.0"'),
        entrypoint=["bash", "-c", start],
    )
    container.with_env("POSTGRES_PASSWORD", _ADMIN_PASSWORD)
    container.with_exposed_ports(5432)
    container.with_copy_into_container(pki.cert, "/certs/server.crt")
    container.with_copy_into_container(pki.key, "/certs/server.key")
    with container:
        _wait_until_ready(container)
        result = container.exec(
            [
                "psql",
                "-v",
                "ON_ERROR_STOP=1",
                "-U",
                "postgres",
                "-c",
                _SETUP.format(password=_READER_PASSWORD),
            ]
        )
        assert result.exit_code == 0, result.output.decode()
        yield "127.0.0.1", int(container.get_exposed_port(5432))


def _wait_until_ready(container: DockerContainer) -> None:
    """Wait up to 30 seconds for the server to accept a TCP connection inside its container.

    Parameters
    ----------
    container
        The running PostgreSQL container.

    Raises
    ------
    AssertionError
        When it does not.
    """
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if container.exec(["pg_isready", "-h", "127.0.0.1", "-U", "postgres"]).exit_code == 0:
            return
        time.sleep(0.2)
    raise AssertionError("PostgreSQL did not accept connections within 30 seconds")


@pytest.fixture(scope="module")
def relay(
    postgres: tuple[str, int], tmp_path_factory: pytest.TempPathFactory
) -> Iterator[list[str]]:
    """Serve the task's relay in-process: ``127.0.0.6:15001`` to the PostgreSQL container.

    Parameters
    ----------
    postgres
        Where the container is published.
    tmp_path_factory
        For the routing table the relay reads.

    Yields
    ------
    list of str
        The relay's log lines.
    """
    directory = tmp_path_factory.mktemp("relay")
    route = directory / "route"
    route.write_text(_ROUTE_HEADER + _ROUTE_ATTACHED, encoding="ascii")
    plan = RelayPlan(
        address=ipaddress.IPv4Address(RELAY_ADDRESS),
        subnet=ipaddress.IPv4Network("127.0.0.0/29"),
        refused=frozenset(),
        routes=(RelayRoute(RELAY_PORT, *postgres),),
    )
    settings = EgressSettings(
        key=directory / "unused.key",
        reachable=frozenset({ipaddress.IPv4Network("127.0.0.0/8")}),
        route=route,
        route6=directory / "missing",
    )
    logs: list[str] = []
    ready, stop = threading.Event(), threading.Event()
    threading.Thread(
        target=serve,
        args=(plan, settings),
        kwargs={"ready": ready.set, "stop": stop, "log": logs.append},
        daemon=True,
    ).start()
    assert ready.wait(5)
    yield logs
    stop.set()


@pytest.fixture(autouse=True)
def _task_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Resolve ``lakana-relay`` to the in-process relay; leave no task state behind.

    Parameters
    ----------
    monkeypatch
        Patches the resolver and removes `LAKANA_CA_BUNDLE`.

    Yields
    ------
    None
        While the test runs.
    """
    real = socket.gethostbyname

    def resolve(name: str) -> str:
        return RELAY_ADDRESS if name == _check.RELAY_HOST else real(name)

    monkeypatch.setattr(socket, "gethostbyname", resolve)
    monkeypatch.delenv("LAKANA_CA_BUNDLE", raising=False)
    yield
    context._set(None)
    _connections.clear()


def _declare(
    name: str,
    kind: str,
    address: Mapping[str, object],
    settings: Mapping[str, object],
    secret: Mapping[str, str] | None,
    routing: Mapping[str, object],
) -> None:
    """Set the state of a task that declares one connection, as the harness does.

    Parameters
    ----------
    name
        The connection.
    kind
        Its kind.
    address
        Its address.
    settings
        Its settings.
    secret
        Its secret's values.
    routing
        The task description's routing for it.
    """
    body = {
        "name": name,
        "kind": kind,
        "address": dict(address),
        "settings": dict(settings),
        "endpoints": [],
        "secret": None if secret is None else dict(secret),
    }
    _connections.clear()
    context._set(
        context._TaskState(
            client=SimpleNamespace(task_connections=SimpleNamespace(get={name: body}.__getitem__)),  # ty: ignore[invalid-argument-type]
            environment="dev",
            asset="bronze.orders",
            declared=frozenset({name}),
            connections={name: {"kind": kind, **routing}},
        )
    )


def _crm(
    pki: _Pki,
    *,
    host: str = "db.test",
    password: str = _READER_PASSWORD,
    own_ca: bool = True,
) -> None:
    """Declare `crm-db`: the container through the relay, ``verify-full``.

    Parameters
    ----------
    pki
        The authority, given as the connection's CA.
    host
        The host name TLS verifies.
    password
        The secret's password.
    own_ca
        Whether the connection carries the authority (else the bundle must).
    """
    settings: dict[str, object] = {"user": "reader", "tls": "verify-full"}
    if own_ca:
        settings["ca_certificate"] = pki.authority.ca_file.read_text()
    _declare(
        "crm-db",
        "postgresql",
        {"host": host, "port": 5432, "database": "postgres"},
        settings,
        {"password": password},
        {"relay_port": RELAY_PORT},
    )


@pytest.mark.parametrize("own_ca", [True, False])
def test_verify_full_through_the_relay_trusts_the_connection_or_the_bundle(
    pki: _Pki, relay: list[str], monkeypatch: pytest.MonkeyPatch, own_ca: bool
) -> None:
    """The relay carries libpq's TLS; the certificate is checked for ``db.test``."""
    if not own_ca:
        monkeypatch.setenv("LAKANA_CA_BUNDLE", str(pki.bundle))
    _crm(pki, own_ca=own_ca)

    table = sql.read("crm-db", "SELECT 1 AS one")

    assert table.to_pylist() == [{"one": 1}]
    assert not [line for line in relay if line.startswith("refused")]


def test_a_certificate_for_another_host_gives_the_tls_text(pki: _Pki, relay: list[str]) -> None:
    """``other.test`` is not the certificate's name."""
    _crm(pki, host="other.test")

    with pytest.raises(SourceRefused) as caught:
        sql.read("crm-db", "SELECT 1")

    assert str(caught.value) == (
        "`crm-db`: the server's certificate is not trusted for `other.test`. Give the "
        "connection your CA: `lakana connection update crm-db --env dev --ca-file ca.pem`."
    )


def test_a_wrong_password_gives_the_authentication_text(pki: _Pki, relay: list[str]) -> None:
    """PostgreSQL's words are kept; the wrong password is not."""
    _crm(pki, password="wrong-0123456789")

    with pytest.raises(SourceRefused) as caught:
        sql.read("crm-db", "SELECT 1")

    text = str(caught.value)
    assert text.startswith("`crm-db`: authentication failed (PostgreSQL said: ")
    assert 'password authentication failed for user "reader"' in text
    assert "wrong-0123456789" not in text


@pytest.mark.parametrize("params", [None, {"id": "00010203-0405-0607-0809-0a0b0c0d0e0f"}])
def test_a_data_modifying_cte_fails_in_the_read_only_transaction(
    pki: _Pki, relay: list[str], params: dict[str, object] | None
) -> None:
    """The user may insert, the transaction may not; with and without parameters."""
    _crm(pki)
    where = " WHERE id = CAST(:id AS uuid)" if params else ""
    query = (
        f"WITH x AS (INSERT INTO kinds (id) SELECT id FROM kinds{where} RETURNING id) "
        "SELECT * FROM x"
    )

    with pytest.raises(UsageError, match="read-only transaction, and this one writes"):
        sql.read("crm-db", query, params=params)


def test_types_are_decided_from_postgresqls_metadata(pki: _Pki, relay: list[str]) -> None:
    """uuid, json and jsonb as strings, numeric(p, s) as a decimal, arrays without metadata."""
    _crm(pki)

    table = sql.read("crm-db", "SELECT id, amount, doc, docb, tags, at FROM kinds")

    assert table.schema == pa.schema(
        [
            ("id", pa.string()),
            ("amount", pa.decimal128(12, 2)),
            ("doc", pa.string()),
            ("docb", pa.string()),
            ("tags", pa.list_(pa.int32())),
            ("at", pa.timestamp("us", "UTC")),
        ]
    )
    assert all(f.metadata is None for f in table.schema)
    assert table.schema.field("tags").type.value_field.metadata is None
    row = table.to_pylist()[0]
    assert row["id"] == "00010203-0405-0607-0809-0a0b0c0d0e0f"
    assert str(row["amount"]) == "1234.50"
    assert row["docb"] == '{"b": 2}'
    assert row["tags"] == [1, 2]
    assert row["at"].isoformat() == "2026-10-26T10:00:00+00:00"


@pytest.mark.parametrize(
    ("column", "cast"),
    [("n", "CAST(n AS numeric(38, 9))"), ("m", "CAST(m AS numeric(19, 2))")],
)
def test_unconstrained_numeric_and_money_are_refused_naming_their_casts(
    pki: _Pki, relay: list[str], column: str, cast: str
) -> None:
    """Refused before a row is read, with the cast that fixes it."""
    _crm(pki)

    with pytest.raises(UsageError) as caught:
        sql.read("crm-db", f"SELECT {column} FROM kinds")

    assert cast in str(caught.value)


@pytest.mark.parametrize("column", ["amount", "id", "docb"])
def test_an_array_of_a_converted_type_is_refused_naming_a_text_cast(
    pki: _Pki, relay: list[str], column: str
) -> None:
    """numeric, uuid and jsonb need a conversion that is not made inside an array."""
    _crm(pki)

    with pytest.raises(UsageError, match=r"CAST\(a AS text\)"):
        sql.read("crm-db", f"SELECT ARRAY[{column}] AS a FROM kinds")


def test_the_advised_cast_reads_as_a_decimal(pki: _Pki, relay: list[str]) -> None:
    """The message's cast is one that works."""
    _crm(pki)

    table = sql.read("crm-db", "SELECT CAST(n AS numeric(38, 9)) AS n FROM kinds")

    assert table.schema.field("n").type == pa.decimal128(38, 9)
    assert str(table.column("n")[0]) == "1.500000000"


def test_without_parameters_the_byte_cap_stops_a_streamed_result(
    pki: _Pki, relay: list[str]
) -> None:
    """300,000 rows of about 110 bytes stream in batches; the cap stops them early."""
    _crm(pki)

    with pytest.raises(CapExceeded, match=r"max_bytes \(2000000 bytes\)"):
        sql.read("crm-db", "SELECT id, pad FROM big", max_bytes=2_000_000)


def test_with_a_parameter_the_row_cap_stops_the_result(pki: _Pki, relay: list[str]) -> None:
    """With values bound the result arrives as one batch, and the row cap counts it."""
    _crm(pki)

    with pytest.raises(CapExceeded, match=r"max_rows \(1000\)"):
        sql.read("crm-db", "SELECT id FROM big WHERE id > :low", params={"low": 0}, max_rows=1000)
    table = sql.read("crm-db", "SELECT id FROM big WHERE id > :low", params={"low": 299_998})

    assert table.column("id").to_pylist() == [299_999, 300_000]


def test_sqlite_reads_a_real_file_read_only(tmp_path: Path) -> None:
    """``file:...?mode=ro`` with a bound value; a write fails and the file keeps its rows."""
    root = tmp_path / "shared"
    (root / "data").mkdir(parents=True)
    path = root / "data" / "app.db"
    with contextlib.closing(sqlite3.connect(path)) as db, db:
        db.execute("CREATE TABLE t(a INTEGER, b TEXT)")
        db.execute("INSERT INTO t VALUES (1, 'x'), (2, 'y')")
    _declare(
        "local-db",
        "sqlite-file",
        {"root": "shared", "path": "data/app.db"},
        {},
        None,
        {"root": "shared", "root_dir": str(root), "path": "data/app.db"},
    )

    table = sql.read("local-db", "SELECT a, b FROM t WHERE a > :low", params={"low": 1})
    with pytest.raises(UsageError, match="read-only"):
        sql.read("local-db", "DELETE FROM t")

    assert table.to_pylist() == [{"a": 2, "b": "y"}]
    with contextlib.closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT count(*) FROM t").fetchone() == (2,)
