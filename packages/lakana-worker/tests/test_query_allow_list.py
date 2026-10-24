"""Allow-list mode, end to end: what a real query process opens, over HTTPS and in local mode.

The HTTPS cases run against `tls_fixture` servers in the test process (a test authority, a
leaf for ``127.0.0.1``), which exercise exactly what broke on DuckDB 2.0 (httpfs loading the
CA bundle under the restriction) and can play what a store cannot: a second host, a redirect.
"""

from __future__ import annotations

import contextlib
import functools
import http.server
import os
import socket
import threading
from collections.abc import Iterator
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from tls_fixture import Authority, Serving, make_authority, serving

from lakana_core.errors import ValidationError
from lakana_core.models import QueryErrorCause
from lakana_core.runs.failures import FailureCode
from lakana_core.runs.tasks import FailureKind
from lakana_worker.executor import INTERNAL_ADVICE, DuckDBInternalError, execute_query
from lakana_worker.pipeline import failure_cause
from lakana_worker.query_child import OUTSIDE_PREFIXES, REFUSED_LOCATION, STORE_CERTIFICATES
from lakana_worker.query_protocol import STORE_CERTIFICATE_UNVERIFIED
from lakana_worker.task_outcome import classify, failure_code_of

NOT_ONE_SELECT = (
    "Query failed: the query process was given something other than one SELECT statement."
)
WRITES_IN_SELECT = "Query failed: the query process was given a SELECT that may write"


def _parquet(path: Path, value: int) -> None:
    """Write a one-row Parquet file with column ``a``.

    Parameters
    ----------
    path
        The file; its directory is created.
    value
        The cell.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table({"a": [value]}), path)


@pytest.fixture
def authority(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Authority:
    """Make a test authority and give its certificate to every query process as the bundle.

    Parameters
    ----------
    tmp_path
        Holds the certificate files.
    monkeypatch
        Replaces the worker's resolved bundle for the test.

    Returns
    -------
    Authority
        The authority whose certificate the query process trusts.
    """
    (tmp_path / "certs").mkdir()
    made = make_authority(tmp_path / "certs")
    monkeypatch.setattr("lakana_worker.executor.query_ca_bundle", lambda: str(made.ca_file))
    return made


@pytest.fixture
def store(tmp_path: Path, authority: Authority) -> Iterator[Serving]:
    """Serve a store over HTTPS: ``t1/a.parquet`` (1), ``t2/a.parquet`` (2), ``t10/a.parquet`` (10).

    Parameters
    ----------
    tmp_path
        Holds the served files.
    authority
        Signs the server's certificate.

    Yields
    ------
    Serving
        The running server.
    """
    for table, value in (("t1", 1), ("t2", 2), ("t10", 10)):
        _parquet(tmp_path / "store" / table / "a.parquet", value)
    with serving(tmp_path / "store", authority, tmp_path / "certs") as server:
        yield server


@pytest.fixture
def other(tmp_path: Path, authority: Authority) -> Iterator[Serving]:
    """Serve a second host over HTTPS: ``t9/a.parquet`` (9), never one of the query's tables.

    Parameters
    ----------
    tmp_path
        Holds the served files.
    authority
        Signs the server's certificate (the same authority: only the allow-list refuses it).

    Yields
    ------
    Serving
        The running server.
    """
    _parquet(tmp_path / "other" / "t9" / "a.parquet", 9)
    with serving(tmp_path / "other", authority, tmp_path / "certs") as server:
        yield server


def _read(url: str, prefixes: tuple[str, ...]) -> list[int]:
    """Read one Parquet file by its literal URL, as SQL that got past the guard would.

    Parameters
    ----------
    url
        The file.
    prefixes
        The locations the query process may open.

    Returns
    -------
    list of int
        Column ``a``.
    """
    result = execute_query(f"SELECT a FROM read_parquet('{url}')", read_prefixes=prefixes)
    return result.column("a").to_pylist()


def test_an_https_read_under_its_prefix_returns_rows(store: Serving) -> None:
    """Red on DuckDB 2.0 before allow-list mode: ``Cannot read a CA certificate bundle``."""
    assert _read(store.url("t1/a.parquet"), (store.url("t1/"),)) == [1]


def test_an_https_read_through_a_variable_returns_rows(store: Serving) -> None:
    """The way every production read goes: the file list is a variable, checked by the child."""
    result = execute_query(
        "SELECT a FROM read_parquet(getvariable('v_0_files'))",
        {"v_0_files": [store.url("t1/a.parquet") + "?X-Amz-Signature=abc"]},
        read_prefixes=(store.url("t1/"),),
    )
    assert result.column("a").to_pylist() == [1]


@pytest.mark.parametrize(
    "target",
    [
        pytest.param("sibling", id="sibling t10 against t1/"),
        pytest.param("other", id="a second server"),
        pytest.param("http", id="http against https of the same host"),
        pytest.param("http://169.254.169.254/latest/meta-data/", id="cloud metadata"),
        pytest.param("s3://bucket/t1/a.parquet", id="s3 scheme"),
        pytest.param("https://127.0.0.1:9/t1/a.parquet", id="another port"),
    ],
)
def test_an_address_outside_the_prefixes_is_refused_without_naming_it(
    store: Serving, other: Serving, target: str
) -> None:
    """Red on DuckDB 1.5 before allow-list mode: each of these was read (or tried)."""
    url = {
        "sibling": store.url("t10/a.parquet"),
        "other": other.url("t9/a.parquet"),
        "http": store.url("t1/a.parquet").replace("https://", "http://"),
    }.get(target, target)
    with pytest.raises(ValidationError) as caught:
        _read(url, (store.url("t1/"),))
    assert type(caught.value) is ValidationError
    assert str(caught.value) == REFUSED_LOCATION
    assert other.seen == []
    assert not [line for line in store.seen if "t10" in line]


def test_a_variable_that_leaves_its_prefix_is_refused_before_anything_runs(
    store: Serving,
) -> None:
    with pytest.raises(DuckDBInternalError) as caught:
        execute_query(
            "SELECT a FROM read_parquet(getvariable('v_0_files'))",
            {"v_0_files": [store.url("t1/../t2/a.parquet")]},
            read_prefixes=(store.url("t1/"),),
        )
    assert str(caught.value).startswith(OUTSIDE_PREFIXES)
    assert store.seen == []


def test_known_residual_a_literal_dotdot_reaches_the_same_server(store: Serving) -> None:
    """Known residual (#14): DuckDB compares the URL string; the request leaves the prefix.

    Production file lists are variables, which the child checks; only SQL that got past the
    guard can write a literal. What it reaches is what the same host serves to a request
    without a valid signature. Pinned so that a DuckDB change shows.
    """
    assert _read(store.url("t1/../t2/a.parquet"), (store.url("t1/"),)) == [2]


def test_known_residual_a_redirect_from_the_store_is_followed_to_another_host(
    tmp_path: Path, authority: Authority, other: Serving
) -> None:
    """Known residual (#14): httpfs follows a redirect without checking the allow-list.

    Observed on DuckDB 1.5.5 and 2.0.0. The reach is what the store itself redirects to;
    the query cannot choose the target. Pinned so that a DuckDB change shows.
    """
    _parquet(tmp_path / "redirecting" / "t1" / "a.parquet", 1)
    redirects = {"/t1/r.parquet": other.url("t9/a.parquet")}
    with serving(tmp_path / "redirecting", authority, tmp_path / "certs", redirects) as redirecting:
        assert _read(redirecting.url("t1/r.parquet"), (redirecting.url("t1/"),)) == [9]
    assert other.seen and all(line.endswith("/t9/a.parquet") for line in other.seen)


@pytest.fixture
def foreign(tmp_path: Path, authority: Authority) -> Iterator[tuple[Serving, Authority]]:
    """Serve ``t1/a.parquet`` (1) with a certificate of an authority the worker does not trust.

    Parameters
    ----------
    tmp_path
        Holds the served files and the certificates.
    authority
        The authority the query process trusts (requested so its bundle is the one in use).

    Yields
    ------
    tuple of (Serving, Authority)
        The running server and the foreign authority that signed its certificate.
    """
    stranger = make_authority(tmp_path / "certs", "Foreign Test CA")
    _parquet(tmp_path / "foreign" / "t1" / "a.parquet", 1)
    with serving(tmp_path / "foreign", stranger, tmp_path / "certs") as server:
        yield server, stranger


@pytest.mark.slow
def test_a_store_certificate_of_a_foreign_authority_fails_once_and_is_not_retried(
    foreign: tuple[Serving, Authority], authority: Authority
) -> None:
    """Red before #277: a transient storage error, which the worker retried, quoting the URL."""
    server, _stranger = foreign
    signed = server.url("t1/a.parquet") + "?X-Amz-Signature=SECRETSIG"
    with pytest.raises(DuckDBInternalError) as caught:
        execute_query(
            "SELECT a FROM read_parquet(getvariable('v_0_files'))",
            {"v_0_files": [signed]},
            read_prefixes=(server.url("t1/"),),
        )
    message = str(caught.value)
    assert message.startswith(STORE_CERTIFICATE_UNVERIFIED)
    for part in (
        server.base.removeprefix("https://"),
        str(authority.ca_file),
        "AWS_CA_BUNDLE",
        "SSL_CERT_FILE",
        STORE_CERTIFICATES,
    ):
        assert part in message
    assert "SECRETSIG" not in message
    assert "t1/a.parquet" not in message
    assert INTERNAL_ADVICE not in message
    # Not retried: the worker's retry path takes only transient failures.
    assert classify(caught.value) is FailureKind.PERMANENT
    assert failure_cause(caught.value) is QueryErrorCause.INTERNAL
    assert failure_code_of(caught.value) is FailureCode.INTERNAL
    # The handshake failed, so no request line reached the server.
    assert server.seen == []


def test_the_store_certificate_verifies_with_its_authority_in_the_bundle(
    foreign: tuple[Serving, Authority], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same server reads once the request's ``ca_bundle`` holds the authority that signed it."""
    server, stranger = foreign
    monkeypatch.setattr("lakana_worker.executor.query_ca_bundle", lambda: str(stranger.ca_file))
    result = execute_query(
        "SELECT a FROM read_parquet(getvariable('v_0_files'))",
        {"v_0_files": [server.url("t1/a.parquet") + "?X-Amz-Signature=abc"]},
        read_prefixes=(server.url("t1/"),),
    )
    assert result.column("a").to_pylist() == [1]


@pytest.fixture
def local_store(tmp_path: Path) -> Path:
    """Lay out a local store: two tables, a secret beside them, a symlink out of ``t1``.

    Parameters
    ----------
    tmp_path
        The test's directory.

    Returns
    -------
    Path
        The store's directory (``t1``, ``t2``, ``t10``; ``../secret/s.parquet`` beside it).
    """
    store = tmp_path / "store"
    _parquet(store / "t1" / "a.parquet", 1)
    _parquet(store / "t2" / "a.parquet", 2)
    _parquet(store / "t10" / "a.parquet", 10)
    _parquet(tmp_path / "secret" / "s.parquet", 42)
    os.symlink(tmp_path / "secret" / "s.parquet", store / "t1" / "link.parquet")
    os.symlink(tmp_path / "secret", store / "t1" / "linked-dir")
    return store


def test_local_mode_reads_a_tables_own_files(local_store: Path) -> None:
    assert _read(f"{local_store}/t1/a.parquet", (f"{local_store}/t1/",)) == [1]
    assert _read(f"{local_store}/t1/a.parquet", (f"{local_store}/t1/", f"{local_store}/t2/")) == [1]


@pytest.mark.parametrize(
    "relative",
    [
        pytest.param("../secret/s.parquet", id="a file outside the tables"),
        pytest.param("t1/../../secret/s.parquet", id="dotdot"),
        pytest.param("t1/link.parquet", id="a symlink out of a table directory"),
        pytest.param("t1/linked-dir/s.parquet", id="a symlinked directory out of a table"),
        pytest.param("t10/a.parquet", id="sibling t10 against t1/"),
        pytest.param("t2/a.parquet", id="another table"),
        pytest.param("*/a.parquet", id="a glob above the table"),
    ],
)
def test_local_mode_refuses_what_lies_outside_the_tables(local_store: Path, relative: str) -> None:
    """Red on DuckDB 1.5 before allow-list mode: local mode read anything the worker could."""
    with pytest.raises(ValidationError) as caught:
        _read(f"{local_store}/{relative}", (f"{local_store}/t1/",))
    assert str(caught.value) == REFUSED_LOCATION
    assert "secret" not in str(caught.value)


def test_local_mode_refuses_a_glob_that_meets_a_symlink_out(local_store: Path) -> None:
    """A glob inside the table directory names the symlink DuckDB refuses; the text does not."""
    with pytest.raises(ValidationError) as caught:
        _read(f"{local_store}/t1/*.parquet", (f"{local_store}/t1/",))
    assert str(caught.value) == REFUSED_LOCATION


class _Proxy:
    """A TCP listener that stands in for a proxy: records the first line of each request.

    It answers nothing and closes the connection, so a client that reaches it fails; what
    matters is that it was reached, and with what.
    """

    def __init__(self) -> None:
        self._socket = socket.create_server(("127.0.0.1", 0))
        self._socket.settimeout(0.2)
        self.port = self._socket.getsockname()[1]
        self.lines: list[str] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        """Accept connections until stopped and record their first line."""
        while not self._stop.is_set():
            try:
                connection, _ = self._socket.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with connection:
                connection.settimeout(2)
                with contextlib.suppress(OSError):
                    self.lines.append(connection.recv(4096).split(b"\r\n", 1)[0].decode())

    def close(self) -> None:
        """Stop accepting and close the listener."""
        self._stop.set()
        self._thread.join(timeout=5)
        self._socket.close()


@pytest.fixture
def proxy() -> Iterator[_Proxy]:
    """Run a proxy stand-in on loopback.

    Yields
    ------
    _Proxy
        The running stand-in; `lines` holds what reached it.
    """
    stand_in = _Proxy()
    try:
        yield stand_in
    finally:
        stand_in.close()


@pytest.fixture
def plain_store(tmp_path: Path) -> Iterator[str]:
    """Serve ``t1/a.parquet`` over plain HTTP, as the Compose bundle's store is reached.

    Parameters
    ----------
    tmp_path
        Holds the served file.

    Yields
    ------
    str
        ``http://127.0.0.1:<port>``.
    """
    _parquet(tmp_path / "plain" / "t1" / "a.parquet", 1)
    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler, directory=str(tmp_path / "plain")
    )
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("scheme", ["http", "https"])
def test_a_proxy_secret_in_the_statement_is_refused_before_anything_runs(
    scheme: str, plain_store: str, store: Serving, proxy: _Proxy
) -> None:
    """Red before the statement check: the proxy got the presigned URL (http) or a CONNECT.

    `lock_configuration` holds settings, and `CREATE SECRET (TYPE http, HTTP_PROXY ...)` is not
    a setting. The guard refuses it first; the child refuses any text that is not one SELECT.
    """
    base = plain_store if scheme == "http" else store.base
    url = f"{base}/t1/a.parquet?X-Amz-Signature=SECRETSIG"
    sql = (
        f"CREATE SECRET p (TYPE http, HTTP_PROXY '127.0.0.1:{proxy.port}'); "
        "SELECT a FROM read_parquet(getvariable('v_0_files'))"
    )
    with pytest.raises(DuckDBInternalError) as caught:
        execute_query(sql, {"v_0_files": [url]}, read_prefixes=(f"{base}/t1/",))
    assert str(caught.value).startswith(NOT_ONE_SELECT)
    assert proxy.lines == []
    assert "SECRETSIG" not in str(caught.value)


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE SECRET h (TYPE http, EXTRA_HTTP_HEADERS MAP {'X-Leak': 'v'}); SELECT 1",
        "SELECT 1; SELECT 2",
        "SET VARIABLE x = 1",
        "COPY (SELECT 1) TO '/tmp/x.csv'",
        "ATTACH ':memory:' AS m",
        "LOAD json",
        "INSTALL json",
        "PRAGMA threads = 1",
        "CALL pragma_version()",
        "SET threads = 1",
        "CREATE TABLE t AS SELECT 1",
        "EXPLAIN SELECT 1",
        "MERGE INTO t USING s ON t.a = s.a WHEN MATCHED THEN DELETE",
    ],
)
def test_anything_but_one_select_is_refused_by_the_child(sql: str) -> None:
    with pytest.raises(DuckDBInternalError) as caught:
        execute_query(sql)
    assert str(caught.value).startswith(NOT_ONE_SELECT)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1 AS a",
        "WITH x AS (SELECT 1 AS a) SELECT a FROM x",
        "FROM range(3)",
        "VALUES (1)",
        "SELECT 1 AS a;",
        "SELECT 1 AS a UNION ALL SELECT 2",
        "SELECT * FROM (SELECT 1 AS a, 2 AS b) PIVOT (sum(b) FOR a IN (1))",
        "SELECT * FROM (SELECT 1 AS a, 2 AS b) UNPIVOT (v FOR k IN (a, b))",
        "PIVOT (SELECT 1 AS a, 2 AS b) ON a IN (1) USING sum(b)",
        "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r WHERE n < 3) FROM r",
        "WITH x AS MATERIALIZED (SELECT 1 AS a) SELECT a FROM x",
        "SELECT * FROM (WITH x AS (SELECT 1 AS a) SELECT a FROM x) LIMIT 1",
        "SELECT list_transform([1, 2], LAMBDA x : x + 1) AS l",
        "SELECT list_reduce([1, 2], LAMBDA x, y : x + y) AS l",
        """SELECT '{"a": 1}'::JSON -> '$.a' AS j, '{"a": 1}'::JSON ->> 'a' AS k""",
        "SELECT sum(a) FILTER (WHERE a > 0) OVER (PARTITION BY a ORDER BY a) AS s "
        "FROM (SELECT 1 AS a)",
        "SELECT a, row_number() OVER w AS n FROM (SELECT 1 AS a) WINDOW w AS (ORDER BY a) "
        "QUALIFY true",
        "SELECT (SELECT max(x) FROM range(3) AS r(x)) AS m WHERE EXISTS (SELECT 1)",
        "SELECT 1 AS a INTERSECT SELECT 1 EXCEPT SELECT 2",
        "SELECT unnest([1, 2]) AS u, [x * 2 FOR x IN [1, 2]] AS c, {'k': 1} AS s",
    ],
)
def test_one_select_in_any_spelling_still_runs(sql: str) -> None:
    """Every form the guard lets through parses as one SELECT on 1.5.5 and 2.0.0.

    Known: on 1.5.5 a reading ``PRAGMA`` (``PRAGMA version``) also parses as a SELECT of its
    ``pragma_*`` function (on 2.0.0 it is a PRAGMA and refused); the guard refuses both forms,
    and none of them changes a setting or a secret.
    """
    assert execute_query(sql).num_rows >= 1


@pytest.mark.parametrize(
    "sql",
    [
        "WITH x AS (COPY (SELECT 1) TO '/nonexistent/x.csv') SELECT 1",
        "SELECT * FROM (WITH x AS (COPY (SELECT 1) TO '/nonexistent/x.csv') SELECT 1)",
        "SELECT 1 UNION ALL (WITH x AS (COPY (SELECT 1) TO '/nonexistent/x.csv') SELECT 1)",
        "WITH x AS (DELETE FROM t RETURNING *) SELECT * FROM x",
        "WITH x AS (UPDATE t SET a = 1 RETURNING *) SELECT * FROM x",
        "WITH x AS (INSERT INTO t VALUES (1) RETURNING *) SELECT * FROM x",
        "SELECT * FROM json_execute_serialized_sql(json_serialize_sql('SELECT 1'))",
        "SELECT * FROM query('SELECT 1')",
        "SELECT * FROM query_table('t')",
    ],
)
def test_a_write_nested_in_a_select_is_refused_by_the_child(sql: str) -> None:
    """Red on 2.0.0 before the second layer: each parses as one SELECT and reached DuckDB.

    The last three run SQL given as a value, which the parse tree cannot show; the guard refuses
    all of them first.
    """
    with pytest.raises(DuckDBInternalError) as caught:
        execute_query(sql)
    assert str(caught.value).startswith(WRITES_IN_SELECT)


@pytest.mark.parametrize("serialised", [False, True], ids=["nested COPY", "serialised COPY"])
def test_a_copy_nested_in_a_select_writes_nothing_into_a_table_directory(
    tmp_path: Path, serialised: bool
) -> None:
    """Red on 2.0.0 before the second layer: local mode wrote ``t1/b.parquet`` and returned 1.

    DuckDB may write wherever it may read, so a nested ``COPY`` could add a file to a table
    the query reads; the child refuses it before anything is configured. The serialised form
    (``json_execute_serialized_sql``) was red before the function check too. The guard refuses
    both first.
    """
    _parquet(tmp_path / "t1" / "a.parquet", 1)
    target = tmp_path / "t1" / "b.parquet"
    inner = f"WITH x AS (COPY (SELECT 9 AS a) TO ''{target}'') SELECT 1"
    sql = (
        f"SELECT * FROM json_execute_serialized_sql(json_serialize_sql('{inner}'))"
        if serialised
        else f"WITH x AS (COPY (SELECT 9 AS a) TO '{target}') SELECT 1 AS one"
    )
    with pytest.raises(DuckDBInternalError) as caught:
        execute_query(sql, read_prefixes=(f"{tmp_path / 't1'}/",))
    assert str(caught.value).startswith(WRITES_IN_SELECT)
    assert not target.exists()
