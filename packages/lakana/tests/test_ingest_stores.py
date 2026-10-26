"""`lakana.ingest._stores`, `_http` and the object and HTTPS sources of `files.read`."""

from __future__ import annotations

import base64
import contextlib
import datetime as dt
import json
import os
import select
import socket
import ssl
import threading
from collections.abc import Generator, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

import obstore
import pytest
from obstore.store import MemoryStore
from tls_fixture import Authority, make_authority, serving

from lakana import context
from lakana.ingest import (
    CapExceeded,
    IngestError,
    SourceError,
    SourceRefused,
    _connections,
    _http,
    _stores,
    files,
)
from lakana.ingest.connections import Connection, MaskedMapping

_PROXY = "http://lakana:egress-token-0123456789@egress:3128"
_KEY = "secret-access-key-0123456789"
_BEARER = "bearer-0123456789"
_SAMPLE = "k-0123456789"


def _connection(
    kind: str,
    address: Mapping[str, object],
    settings: Mapping[str, object] | None = None,
    secret: Mapping[str, str] | None = None,
    proxy_url: str | None = None,
) -> Connection:
    """Build a connection as `connections.get` returns it.

    Parameters
    ----------
    kind
        The kind.
    address
        The address.
    settings
        The settings.
    secret
        The secret's values.
    proxy_url
        The task's proxy.

    Returns
    -------
    Connection
        The connection.
    """
    return Connection(
        name="src",
        kind=kind,
        address=address,
        settings=settings or {},
        endpoints=(),
        secret=None if secret is None else MaskedMapping(secret),
        proxy_url=proxy_url,
    )


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Remove the variables the helpers read; leave no task state behind.

    Parameters
    ----------
    monkeypatch
        Pytest's patcher.

    Yields
    ------
    None
        While the test runs.
    """
    for name in ("LAKANA_EGRESS_PROXY", "LAKANA_CA_BUNDLE", "HTTPS_PROXY", "https_proxy"):
        monkeypatch.delenv(name, raising=False)
    yield
    context._set(None)
    _connections.clear()


@pytest.fixture
def authority(tmp_path: Path) -> Authority:
    """Make the authority that signs the test server.

    Parameters
    ----------
    tmp_path
        Pytest's directory.

    Returns
    -------
    Authority
        The authority.
    """
    return make_authority(tmp_path)


_S3 = {"bucket": "landing", "region": "eu-central-1", "endpoint": "https://s3.example.com:9000"}


def test_s3_is_path_style_with_the_connections_own_endpoint_and_key() -> None:
    store = _stores.store_for(
        _connection(
            "s3",
            _S3,
            {"access_key_id": "AKID", "prefix": "in"},
            {"secret_access_key": _KEY, "session_token": "session-0123456789"},
        )
    )

    assert store.config["endpoint"] == "https://s3.example.com:9000"
    assert store.config["region"] == "eu-central-1"
    assert store.config["access_key_id"] == "AKID"
    assert store.config["secret_access_key"] == _KEY
    assert store.config["session_token"] == "session-0123456789"
    assert store.config["virtual_hosted_style_request"] == "false"
    assert store.prefix == "in"
    assert _KEY not in repr(store)


def test_the_proxy_goes_to_the_store_only_and_only_when_the_task_has_one() -> None:
    settings, secret = {"access_key_id": "AKID"}, {"secret_access_key": _KEY}
    direct = _stores.store_for(_connection("s3", _S3, settings, secret))
    proxied = _stores.store_for(_connection("s3", _S3, settings, secret, proxy_url=_PROXY))

    assert (direct.client_options or {})["proxy_url"] != _PROXY
    assert (direct.client_options or {})["proxy_excludes"] == "s3.example.com"
    assert (proxied.client_options or {})["proxy_url"] == _PROXY
    assert "proxy_excludes" not in (proxied.client_options or {})
    assert "HTTPS_PROXY" not in os.environ and "https_proxy" not in os.environ


def _listening() -> tuple[int, list[bytes]]:
    """Listen on loopback and record the first bytes of each connection.

    Returns
    -------
    tuple
        The port and what each connection sent first.
    """
    seen: list[bytes] = []
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(4)

    def accept() -> None:
        while True:
            client, _ = server.accept()
            with client:
                client.settimeout(5)
                seen.append(client.recv(5))

    threading.Thread(target=accept, daemon=True).start()
    return server.getsockname()[1], seen


def test_a_store_without_a_task_proxy_ignores_https_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The store's client would use ``HTTPS_PROXY``; without a task proxy it goes direct."""
    proxy_port, proxy_saw = _listening()
    store_port, store_saw = _listening()
    for name in ("HTTPS_PROXY", "https_proxy"):
        monkeypatch.setenv(name, f"http://127.0.0.1:{proxy_port}")
    address = {**_S3, "endpoint": f"https://127.0.0.1:{store_port}"}
    store = _stores.store_for(
        _connection("s3", address, {"access_key_id": "AKID"}, {"secret_access_key": _KEY})
    )

    with pytest.raises(obstore.exceptions.BaseError):
        next(iter(obstore.list(store)))

    assert proxy_saw == []
    assert store_saw and store_saw[0].startswith(b"\x16\x03")  # a TLS ClientHello, direct


def test_the_trust_bundle_is_added_to_the_stores_roots(
    authority: Authority, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LAKANA_CA_BUNDLE", str(authority.ca_file))
    store = _stores.store_for(
        _connection("s3", _S3, {"access_key_id": "AKID"}, {"secret_access_key": _KEY})
    )

    root = (store.client_options or {})["root_certificate"]
    assert root in (authority.ca_file.read_text(), authority.ca_file.read_bytes())


def test_an_unreadable_trust_bundle_names_the_setting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LAKANA_CA_BUNDLE", str(tmp_path / "missing.pem"))

    with pytest.raises(IngestError, match=r"LAKANA_CA_BUNDLE.*LAKANA_RUN__CA_BUNDLE"):
        _stores.store_for(
            _connection("s3", _S3, {"access_key_id": "AKID"}, {"secret_access_key": _KEY})
        )


def test_a_missing_secret_names_the_command_that_binds_one() -> None:
    with pytest.raises(IngestError) as caught:
        _stores.store_for(_connection("s3", _S3, {"access_key_id": "AKID"}))
    assert str(caught.value).startswith(
        "`src` has no secret with `secret_access_key`; give it one: `lakana connection update src"
    )


def _gcs_key(**extra: str) -> str:
    """Write a service-account key with a real RSA key, as Google gives it.

    Parameters
    ----------
    **extra
        Fields to add or replace.

    Returns
    -------
    str
        The key as JSON.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    key = {
        "type": "service_account",
        "project_id": "p",
        "private_key_id": "k1",
        "private_key": pem,
        "client_email": "reader@p.iam.gserviceaccount.com",
        "token_uri": "https://oauth2.googleapis.com/token",
        **extra,
    }
    return json.dumps(key)


def test_a_gcs_key_is_passed_on_with_its_signing_fields_only() -> None:
    key = _gcs_key(gcs_base_url="https://elsewhere.example", disable_oauth="true")

    store = _stores.store_for(
        _connection("gcs", {"bucket": "landing"}, {}, {"service_account_json": key})
    )

    passed = json.loads(store.config["service_account_key"])
    assert sorted(passed) == ["client_email", "private_key", "private_key_id"]
    assert store.config["base_url"] == _stores.GCS_BASE_URL == "https://storage.googleapis.com"


def test_a_secret_that_is_no_service_account_key_is_refused_without_showing_it() -> None:
    for value in ("not json at all", json.dumps({"type": "authorized_user", "private_key": "p"})):
        with pytest.raises(IngestError) as caught:
            _stores.store_for(
                _connection("gcs", {"bucket": "landing"}, {}, {"service_account_json": value})
            )
        assert "is not a service-account key" in str(caught.value)
        assert value not in str(caught.value)


def test_azure_takes_a_sas_token_without_its_question_mark_or_an_account_key() -> None:
    address = {"account": "acct", "container": "landing"}
    sas = _stores.store_for(_connection("azure", address, {}, {"sas_token": "?sv=1&sig=abc"}))
    key = _stores.store_for(_connection("azure", address, {}, {"account_key": "a2V5"}))

    assert sas.config["sas_key"] == "sv=1&sig=abc"
    assert key.config["account_key"] == "a2V5"
    assert sas.config["endpoint"] == "https://acct.blob.core.windows.net"
    assert sas.config["container_name"] == "landing"


def test_the_http_client_trusts_nothing_from_the_environment(
    authority: Authority, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://wrong.example:1")
    conn = _connection(
        "http",
        {"base_url": "https://api.example.com/v1"},
        {"auth": "bearer", "ca_certificate": authority.ca_file.read_text()},
        {"token": _BEARER},
        proxy_url=_PROXY,
    )

    with _http.client_for(conn) as client:
        assert client.trust_env is False
        assert client.follow_redirects is False
        assert client.headers["Authorization"] == f"Bearer {_BEARER}"
        assert str(client.base_url) == "https://api.example.com/v1/"


def test_basic_auth_registers_its_header_value_as_a_secret() -> None:
    state = context._TaskState(
        client=None,  # ty: ignore[invalid-argument-type]
        environment="dev",
        asset="bronze.x",
        declared=frozenset(),
        connections={},
    )
    context._set(state)
    conn = _connection(
        "http",
        {"base_url": "https://api.example.com"},
        {"auth": "basic", "username": "reader"},
        {"password": "basic-pass-0123"},
    )

    _http.client_for(conn).close()

    assert base64.b64encode(b"reader:basic-pass-0123").decode() in state.secrets


def test_api_key_goes_to_the_header_or_the_query() -> None:
    header = _connection(
        "http",
        {"base_url": "https://api.example.com"},
        {"auth": "api_key", "api_key_name": "X-Key", "api_key_in": "header"},
        {"api_key": _SAMPLE},
    )
    query = _connection(
        "http",
        {"base_url": "https://api.example.com"},
        {"auth": "api_key", "api_key_name": "key", "api_key_in": "query"},
        {"api_key": _SAMPLE},
    )

    with _http.client_for(header) as by_header, _http.client_for(query) as by_query:
        assert by_header.headers["X-Key"] == _SAMPLE
        assert by_query.params["key"] == _SAMPLE


def test_oauth2_is_refused_with_what_to_do_instead() -> None:
    conn = _connection(
        "http", {"base_url": "https://api.example.com"}, {"auth": "oauth2"}, {"client_secret": "s"}
    )

    with pytest.raises(IngestError, match=r"auth oauth2, which files\.read does not support yet"):
        _http.client_for(conn)


# --- files.read through the sources -------------------------------------------------------


class _TaskConnections:
    """A fake `client.task_connections`.

    Parameters
    ----------
    bodies
        Name to the server's answer.
    """

    def __init__(self, bodies: dict[str, dict[str, object]]) -> None:
        self.bodies = bodies

    def get(self, name: str) -> dict[str, object]:
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
    """A fake SDK client.

    Parameters
    ----------
    task_connections
        The fake resource.
    """

    task_connections: _TaskConnections


def _task(name: str, kind: str, address: dict[str, object], settings: dict[str, object]) -> None:
    """Set the state of a task that declares one connection, without a routing grant.

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
    """
    body = {
        "name": name,
        "kind": kind,
        "address": address,
        "settings": settings,
        "endpoints": [],
        "secret": {"secret_access_key": _KEY} if kind == "s3" else None,
    }
    context._set(
        context._TaskState(
            client=_Client(_TaskConnections({name: body})),  # ty: ignore[invalid-argument-type]
            environment="dev",
            asset="bronze.orders",
            declared=frozenset({name}),
            connections={name: {"kind": kind}},
        )
    )


@pytest.fixture
def memory(monkeypatch: pytest.MonkeyPatch) -> MemoryStore:
    """Declare an `s3` connection `lake` whose store is in memory.

    Parameters
    ----------
    monkeypatch
        Pytest's patcher.

    Returns
    -------
    MemoryStore
        The store `files.read` reads.
    """
    store = MemoryStore()
    monkeypatch.setattr(_stores, "store_for", lambda conn: store)
    _task("lake", "s3", dict(_S3), {"access_key_id": "AKID"})
    return store


def test_an_object_store_lists_and_reads_with_the_walks_glob(memory: MemoryStore) -> None:
    for key, body in {
        "orders/2026/a.csv": b"id\n1\n",
        "orders/2026/b.csv": b"id\n2\n",
        "orders/2026/notes.txt": b"x",
        "other/c.csv": b"id\n3\n",
    }.items():
        obstore.put(memory, key, body)

    table = files.read("lake", "orders/**/*.csv")
    listed = files.list("lake", "orders/*/*.csv")

    assert table.column("id").to_pylist() == [1, 2]
    assert listed.column("name").to_pylist() == ["orders/2026/a.csv", "orders/2026/b.csv"]


def test_an_object_store_since_uses_the_last_modified_time(memory: MemoryStore) -> None:
    obstore.put(memory, "a.csv", b"id\n1\n")
    (meta,) = next(iter(obstore.list(memory)))
    mark = meta["last_modified"]
    obstore.put(memory, "b.csv", b"id\n2\n")

    table = files.read("lake", "*.csv", since=mark)

    assert table.column("id").to_pylist() == [2]
    assert table.column("_source_modified").to_pylist()[0] > mark


def test_an_object_store_caps_name_the_cap(memory: MemoryStore) -> None:
    obstore.put(memory, "a.csv", b"id\n1\n2\n")
    obstore.put(memory, "b.csv", b"id\n3\n")

    with pytest.raises(CapExceeded, match=r"`lake`: 'a\.csv' is 7 bytes, more than max_file_bytes"):
        files.read("lake", "*.csv", max_file_bytes=6)
    with pytest.raises(CapExceeded, match=r"`lake`: the pattern '\*\.csv' matches more than"):
        files.read("lake", "*.csv", max_files=1)


def test_a_refused_credential_is_not_retried_and_names_the_test_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Denied:
        """A store that refuses every request."""

    def denied(*args: object, **kwargs: object) -> object:
        raise obstore.exceptions.PermissionDeniedError("Access Denied for landing")

    monkeypatch.setattr(_stores, "store_for", lambda conn: _Denied())
    monkeypatch.setattr(obstore, "list", denied)
    _task("lake", "s3", dict(_S3), {"access_key_id": "AKID"})

    with pytest.raises(SourceRefused) as caught:
        files.read("lake", "*.csv")
    assert str(caught.value) == (
        "`lake`: authentication failed (the store said: Access Denied for landing). Check the "
        "secret with `lakana connection test lake --env dev`."
    )
    assert SourceRefused.retryable is False


def _https(served: Path, authority: Authority, tmp_path: Path) -> Iterator[str]:
    """Serve a directory over HTTPS and declare an `http` connection `site` on it.

    Parameters
    ----------
    served
        The document root.
    authority
        Signs the server.
    tmp_path
        Where the certificates go.

    Yields
    ------
    str
        The base URL.
    """
    with serving(served, authority, tmp_path) as server:
        _task(
            "site",
            "http",
            {"base_url": f"{server.base}/exports"},
            {"auth": "none", "ca_certificate": authority.ca_file.read_text()},
        )
        yield server.base


def test_an_https_file_reads_with_the_connections_ca(authority: Authority, tmp_path: Path) -> None:
    served = tmp_path / "served"
    (served / "exports").mkdir(parents=True)
    (served / "exports" / "a.csv").write_bytes(b"id\n1\n")
    stamp = dt.datetime(2026, 10, 1, tzinfo=dt.UTC).timestamp()
    os.utime(served / "exports" / "a.csv", (stamp, stamp))

    for _ in _https(served, authority, tmp_path):
        table = files.read("site", "a.csv", since=None)
        old = files.read("site", "a.csv", since=dt.datetime(2026, 10, 2, tzinfo=dt.UTC))

    assert table.column("id").to_pylist() == [1]
    assert table.column("_source_modified").to_pylist() == [dt.datetime(2026, 10, 1, tzinfo=dt.UTC)]
    assert old.num_rows == 0


def test_an_https_file_must_be_one_path_without_wildcards(
    authority: Authority, tmp_path: Path
) -> None:
    served = tmp_path / "served"
    served.mkdir()

    for _ in _https(served, authority, tmp_path):
        for pattern in ("*.csv", "a.csv?x=1", "a%2e.csv", "../a.csv"):
            with pytest.raises(IngestError):
                files.read("site", pattern)


def test_an_https_file_that_is_not_there_names_the_status(
    authority: Authority, tmp_path: Path
) -> None:
    served = tmp_path / "served"
    (served / "exports").mkdir(parents=True)

    for _ in _https(served, authority, tmp_path):
        with pytest.raises(SourceError, match=r"`site`: HTTP 404 for a\.csv"):
            files.read("site", "a.csv")


def test_an_untrusted_https_server_is_refused_naming_the_ca(
    authority: Authority, tmp_path: Path
) -> None:
    served = tmp_path / "served"
    (served / "exports").mkdir(parents=True)
    (tmp_path / "other").mkdir()
    other = make_authority(tmp_path / "other")

    with serving(served, other, tmp_path / "other") as server:
        _task(
            "site",
            "http",
            {"base_url": f"{server.base}/exports"},
            {"auth": "none", "ca_certificate": authority.ca_file.read_text()},
        )
        with pytest.raises(SourceRefused, match=r"certificate is not trusted.*--ca-file ca\.pem"):
            files.read("site", "a.csv")


def test_the_http_tls_floor_is_1_2(authority: Authority) -> None:
    conn = _connection("http", {"base_url": "https://api.example.com"}, {"auth": "none"})

    with _http.client_for(conn) as client:
        transport = client._transport
        context = transport._pool._ssl_context  # ty: ignore[unresolved-attribute]
        assert isinstance(context, ssl.SSLContext)
        assert context.minimum_version == ssl.TLSVersion.TLSv1_2


@contextlib.contextmanager
def _connect_proxy() -> Generator[tuple[int, list[str]]]:
    """Run a CONNECT proxy on loopback that records each request head.

    Yields
    ------
    tuple
        Its port and the heads seen.
    """
    heads: list[str] = []
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(8)

    def tunnel(client: socket.socket) -> None:
        with client:
            head = b""
            while b"\r\n\r\n" not in head:
                head += client.recv(4096)
            heads.append(head.decode())
            host, port = head.split(b" ")[1].decode().rsplit(":", 1)
            with socket.create_connection((host, int(port))) as upstream:
                client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                pair = [client, upstream]
                while True:
                    ready, _, _ = select.select(pair, [], [], 5)
                    if not ready:
                        return
                    for side in ready:
                        data = side.recv(65536)
                        if not data:
                            return
                        (upstream if side is client else client).sendall(data)

    def accept() -> None:
        while True:
            try:
                client, _ = server.accept()
            except OSError:
                return
            threading.Thread(target=tunnel, args=(client,), daemon=True).start()

    threading.Thread(target=accept, daemon=True).start()
    try:
        yield server.getsockname()[1], heads
    finally:
        server.close()


def test_an_https_file_is_read_through_the_tasks_proxy(
    authority: Authority, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under the Docker runner the read goes through `LAKANA_EGRESS_PROXY` with its token."""
    served = tmp_path / "served"
    (served / "exports").mkdir(parents=True)
    (served / "exports" / "a.csv").write_bytes(b"id\n1\n")

    with _connect_proxy() as (port, heads):
        monkeypatch.setenv(
            "LAKANA_EGRESS_PROXY", f"http://lakana:proxy-token-0123@127.0.0.1:{port}"
        )
        for _ in _https(served, authority, tmp_path):
            table = files.read("site", "a.csv")

    assert table.column("id").to_pylist() == [1]
    token = base64.b64encode(b"lakana:proxy-token-0123").decode()
    assert heads and all(f"Basic {token}" in head for head in heads)
    assert all(head.startswith("CONNECT 127.0.0.1:") for head in heads)


# --- error classes, secrets in messages, control characters ---------------------------------


def _usage_error() -> type[IngestError]:
    """Import `UsageError` where the test needs it.

    Returns
    -------
    type
        `lakana.ingest.UsageError`.
    """
    from lakana.ingest import UsageError

    return UsageError


def test_a_connection_that_is_set_up_wrong_is_a_usage_error_that_is_not_retried() -> None:
    """oauth2, a missing secret and a secret that is no key fail every attempt alike."""
    from lakana import runner

    cases = [
        lambda: _http.client_for(
            _connection("http", {"base_url": "https://a.example"}, {"auth": "oauth2"}, {})
        ),
        lambda: _http.client_for(
            _connection("http", {"base_url": "https://a.example"}, {"auth": "bearer"})
        ),
        lambda: _stores.store_for(_connection("s3", _S3, {"access_key_id": "AKID"})),
        lambda: _stores.store_for(
            _connection("gcs", {"bucket": "b"}, {}, {"service_account_json": "{}"})
        ),
        lambda: _stores.store_for(_connection("azure", {"account": "a", "container": "c"}, {})),
    ]
    for case in cases:
        with pytest.raises(_usage_error()) as caught:
            case()
        assert caught.value.retryable is False
        assert runner._retryable("function", caught.value) is False


def test_a_missing_secret_names_the_tasks_environment() -> None:
    """Inside a task the command names the environment the task runs in."""
    _task("lake", "s3", dict(_S3), {"access_key_id": "AKID"})

    with pytest.raises(IngestError) as caught:
        _stores.store_for(_connection("s3", _S3, {"access_key_id": "AKID"}))
    assert "`lakana connection update src --env dev --values-stdin`" in str(caught.value)
    with pytest.raises(IngestError) as http_caught:
        _http.client_for(_connection("http", {"base_url": "https://a.example"}, {"auth": "bearer"}))
    assert "`lakana connection update src --env dev --password-stdin`" in str(http_caught.value)


def test_a_sas_token_is_masked_without_its_question_mark_too() -> None:
    """The store gets the token without ``?``; that form is masked in the task's output."""
    _task("lake", "s3", dict(_S3), {"access_key_id": "AKID"})

    _stores.store_for(
        _connection(
            "azure", {"account": "a", "container": "c"}, {}, {"sas_token": "?sv=1&sig=xyz9"}
        )
    )

    assert "sv=1&sig=xyz9" in context.current().secrets


def _raising(error: BaseException) -> object:
    """Make a stand-in for `obstore.list` or `obstore.get` that raises.

    Parameters
    ----------
    error
        What it raises.

    Returns
    -------
    object
        The function.
    """

    def call(*args: object, **kwargs: object) -> object:
        raise error

    return call


def _denied_task(monkeypatch: pytest.MonkeyPatch, error: BaseException) -> None:
    """Declare `lake`, whose store raises `error` on every request.

    Parameters
    ----------
    monkeypatch
        Pytest's patcher.
    error
        What the store raises.
    """
    monkeypatch.setattr(_stores, "store_for", lambda conn: object())
    monkeypatch.setattr(obstore, "list", _raising(error))
    _task("lake", "s3", dict(_S3), {"access_key_id": "AKID"})


def test_a_store_error_never_shows_a_credential_a_signature_or_a_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """URLs lose their query and user, signatures and known secrets become ``***``."""
    monkeypatch.setenv("LAKANA_EGRESS_PROXY", "http://lakana:proxy-token-0123@egress:3128")
    said = (
        "Generic S3 error: Error performing GET https://lakana:proxy-token-0123@s3.example.com/"
        "landing?list-type=2&X-Amz-Signature=abcdef0123&sig=SASSIG0123 in 1ms - Server returned "
        f"403: <Error><Code>SignatureDoesNotMatch</Code><StringToSign>AWS4 {_KEY}</StringToSign>"
        "<SignatureProvided>feedbeef0123</SignatureProvided></Error> Authorization: AWS4-HMAC "
        f"Credential=AKID {_KEY} proxy-token-0123"
    )
    _denied_task(monkeypatch, obstore.exceptions.GenericError(said))

    with pytest.raises(SourceError) as caught:
        files.read("lake", "*.csv")
    message = str(caught.value)
    for leaked in (_KEY, "proxy-token-0123", "abcdef0123", "SASSIG0123", "feedbeef0123"):
        assert leaked not in message, leaked
    assert message.startswith("`lake`: Generic S3 error: Error performing GET ")
    assert "https://s3.example.com/landing" in message
    assert "SignatureDoesNotMatch" in message
    assert type(caught.value) is SourceError
    assert caught.value.retryable is None


def test_a_certificate_problem_deeper_in_a_store_error_is_a_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The store names the certificate in the error's cause, after its first line."""
    said = (
        "Generic S3 error: Error performing list request\n\nDebug source:\n"
        "Custom { kind: InvalidData, error: InvalidCertificate(UnknownIssuer) }"
    )
    _denied_task(monkeypatch, obstore.exceptions.GenericError(said))

    with pytest.raises(SourceRefused) as caught:
        files.read("lake", "*.csv")
    assert str(caught.value).startswith(
        "`lake`: the server's certificate is not trusted (Generic S3 error: Error performing "
        "list request; InvalidCertificate(UnknownIssuer)). Add your CA to the workers' trust "
        "bundle (LAKANA_RUN__CA_BUNDLE)"
    )


def test_a_store_that_cannot_be_reached_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """A network failure may pass: plain `SourceError`, left to the harness's retry."""
    from lakana import runner

    _denied_task(monkeypatch, obstore.exceptions.GenericError("error sending request: timed out"))

    with pytest.raises(SourceError) as caught:
        files.read("lake", "*.csv")
    assert not isinstance(caught.value, SourceRefused)
    assert runner._retryable("function", caught.value) is True


def test_a_missing_object_and_a_bad_value_are_source_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The store raises `FileNotFoundError` and `ValueError` besides its own classes."""
    for error in (FileNotFoundError("gone: a.csv"), ValueError("bad\nvalue")):
        _connections.clear()
        _denied_task(monkeypatch, error)
        with pytest.raises(SourceError) as caught:
            files.read("lake", "*.csv")
        assert "\n" not in str(caught.value)


def test_an_object_key_with_control_characters_is_escaped(
    memory: MemoryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A key with a line feed or a bidirectional control cannot forge a line in a message."""
    key = "a\n[ERROR] forged\u202e.csv"
    now = dt.datetime.now(dt.UTC)
    monkeypatch.setattr(
        obstore,
        "list",
        lambda store, prefix=None, **kwargs: iter(
            [[{"path": key, "size": 1, "last_modified": now}]]
        ),
    )

    @dataclass(frozen=True)
    class _Big:
        """A result larger than the cap."""

        meta: Mapping[str, object]

    monkeypatch.setattr(obstore, "get", lambda store, path: _Big({"size": 100}))

    with pytest.raises(CapExceeded) as caught:
        files.read("lake", "*.csv", max_file_bytes=2)
    assert "\n" not in str(caught.value)
    assert "\u202e" not in str(caught.value)
    assert "a\\x0a[ERROR] forged\\u202e.csv" in str(caught.value)


def test_a_key_with_an_empty_or_a_dot_segment_is_skipped_and_said(
    memory: MemoryStore, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Such a key is not the path it looks like on another system; it is listed as skipped."""
    now = dt.datetime.now(dt.UTC)
    keys = ["x/../a.csv", "x//b.csv", "./c.csv", "d.csv"]
    monkeypatch.setattr(
        obstore,
        "list",
        lambda store, prefix=None, **kwargs: iter(
            [[{"path": k, "size": 1, "last_modified": now} for k in keys]]
        ),
    )

    obstore.put(memory, "d.csv", b"id\n1\n")

    listed = files.list("lake", "**/*.csv")
    table = files.read("lake", "**/*.csv")

    assert listed.column("name").to_pylist() == ["d.csv"]
    assert table.column("id").to_pylist() == [1]
    err = capsys.readouterr().err
    assert "skipped 'x/../a.csv'" in err
    assert "a key with an empty, . or .. segment" in err


def test_an_object_store_reads_only_below_the_patterns_literal_head(memory: MemoryStore) -> None:
    """The listing asks for the pattern's leading literal segments only."""
    obstore.put(memory, "orders/2026/a.csv", b"id\n1\n")
    asked: list[object] = []
    real = obstore.list

    def recording(store: object, prefix: object = None, **kwargs: object) -> object:
        asked.append(prefix)
        return real(store, prefix, **kwargs)  # ty: ignore[no-matching-overload]

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(obstore, "list", recording)
        files.list("lake", "orders/2026/*.csv")
        files.list("lake", "orders/**/a.csv")
        files.list("lake", "*.csv")

    assert asked == ["orders/2026", "orders", None]


def test_an_object_store_listing_is_bounded_in_objects(
    memory: MemoryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A listing looks at `MAX_ENTRIES` objects at most, matching or not."""
    from lakana.ingest import _directory

    monkeypatch.setattr(_directory, "MAX_ENTRIES", 3)
    for index in range(4):
        obstore.put(memory, f"x{index}.txt", b"x")

    with pytest.raises(IngestError, match=r"`lake`: the listing passed 3 objects; narrow"):
        files.list("lake", "*.csv")


def test_an_object_that_grows_while_it_is_read_stops_at_the_cap(
    memory: MemoryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stream is counted too, not only the size the store reported."""
    obstore.put(memory, "a.csv", b"id\n1\n")
    real = obstore.get

    class _Grown:
        """A result whose stream is longer than its reported size."""

        def __init__(self, result: object) -> None:
            self.meta = result.meta  # ty: ignore[unresolved-attribute]

        def stream(self) -> Iterator[bytes]:
            """Give more bytes than the size said.

            Yields
            ------
            bytes
                Chunks.
            """
            yield b"id\n1\n"
            yield b"2\n3\n4\n5\n"

    monkeypatch.setattr(obstore, "get", lambda store, path: _Grown(real(store, path)))

    with pytest.raises(CapExceeded, match=r"grew past max_file_bytes \(8\)"):
        files.read("lake", "a.csv", max_file_bytes=8)


def test_an_object_store_error_while_reading_is_named(
    memory: MemoryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read that the store refuses is a refusal like a refused listing."""
    obstore.put(memory, "a.csv", b"id\n1\n")
    monkeypatch.setattr(
        obstore, "get", _raising(obstore.exceptions.UnauthenticatedError("expired token"))
    )

    with pytest.raises(SourceRefused, match=r"authentication failed \(the store said: expired"):
        files.read("lake", "a.csv")


def test_an_object_store_reads_max_rows_at_most(memory: MemoryStore) -> None:
    """`max_rows` applies to every kind."""
    obstore.put(memory, "a.csv", b"id\n1\n2\n3\n")

    with pytest.raises(CapExceeded, match=r"more than max_rows \(2\) rows"):
        files.read("lake", "*.csv", max_rows=2)


# --- HTTPS: paths, redirects, statuses -----------------------------------------------------


def _mock_site(monkeypatch: pytest.MonkeyPatch, answer: object) -> list[str]:
    """Declare `site` whose client answers through a function instead of the network.

    Parameters
    ----------
    monkeypatch
        Pytest's patcher.
    answer
        Takes an `httpx.Request` and returns an `httpx.Response` (or raises).

    Returns
    -------
    list of str
        The URLs requested, filled as the test runs.
    """
    import httpx

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url}")
        return answer(request)  # ty: ignore[call-non-callable]

    def client_for(conn: Connection) -> httpx.Client:
        return httpx.Client(
            base_url=str(conn.address["base_url"]), transport=httpx.MockTransport(handler)
        )

    monkeypatch.setattr(_http, "client_for", client_for)
    _task("site", "http", {"base_url": "https://files.example.com/exports"}, {"auth": "none"})
    return seen


def test_an_https_path_never_leaves_the_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """A path that looks like a URL or a network path stays a path below the base URL."""
    import httpx

    seen = _mock_site(monkeypatch, lambda request: httpx.Response(404))

    for pattern in ("https://evil.example/a.csv", "a\\..\\..\\b.csv", "a.csv/http:"):
        with pytest.raises(SourceError, match=r"HTTP 404"):
            files.read("site", pattern)
    with pytest.raises(IngestError, match=r"must be relative to the connection's path"):
        files.read("site", "//evil.example/a.csv")

    assert seen
    assert all(url.startswith("HEAD https://files.example.com/exports/") for url in seen), seen


def test_an_https_path_with_control_characters_is_escaped_in_the_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The path is quoted on the wire and escaped in the message."""
    import httpx

    seen = _mock_site(monkeypatch, lambda request: httpx.Response(404))

    with pytest.raises(SourceError) as caught:
        files.read("site", "a\nERROR x.csv")
    assert "\n" not in str(caught.value)
    assert "a\\x0aERROR x.csv" in str(caught.value)
    assert seen == ["HEAD https://files.example.com/exports/a%0AERROR%20x.csv"]


def test_an_https_wildcard_is_a_usage_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A wildcard, query, fragment or percent sign fails before any request."""
    import httpx

    seen = _mock_site(monkeypatch, lambda request: httpx.Response(200))

    for pattern in ("*.csv", "a.csv?x=1", "a.csv#x", "a%2e.csv", "[ab].csv"):
        with pytest.raises(_usage_error(), match=r"reads one file of an http connection"):
            files.read("site", pattern)
    assert seen == []


def test_an_https_redirect_is_refused_naming_it(authority: Authority, tmp_path: Path) -> None:
    """``Location`` is never followed: the credential would go with the request."""
    served = tmp_path / "served"
    (served / "exports").mkdir(parents=True)

    with serving(
        served, authority, tmp_path, redirects={"/exports/a.csv": "https://elsewhere.example/a"}
    ) as server:
        _task(
            "site",
            "http",
            {"base_url": f"{server.base}/exports"},
            {"auth": "none", "ca_certificate": authority.ca_file.read_text()},
        )
        with pytest.raises(SourceRefused) as caught:
            files.read("site", "a.csv")

    assert str(caught.value) == (
        "`site`: a.csv answers with a redirect (HTTP 302), which files.read does not follow; use "
        "the address it names as the connection's base URL"
    )
    assert server.seen == ["HEAD /exports/a.csv"]


@pytest.mark.parametrize("status", [401, 403])
def test_an_https_credential_refusal_names_the_test_command(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    """401 and 403 are refused credentials: not retried, and the next step is the test."""
    import httpx

    _mock_site(monkeypatch, lambda request: httpx.Response(status))

    with pytest.raises(SourceRefused) as caught:
        files.read("site", "a.csv")
    assert str(caught.value) == (
        f"`site`: authentication failed (the store said: HTTP {status} for a.csv). Check the "
        "secret with `lakana connection test site --env dev`."
    )


def test_an_https_network_failure_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """A connection that fails may pass next time."""
    import httpx

    from lakana import runner

    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    _mock_site(monkeypatch, fail)

    with pytest.raises(SourceError) as caught:
        files.read("site", "a.csv")
    assert str(caught.value) == "`site`: connection refused"
    assert runner._retryable("function", caught.value) is True


def test_an_https_file_without_a_time_or_a_size(monkeypatch: pytest.MonkeyPatch) -> None:
    """No ``Last-Modified``: `since` by mtime is refused; no length: the stream is counted."""
    import httpx

    def answer(request: httpx.Request) -> httpx.Response:
        if request.method == "HEAD":
            return httpx.Response(200, headers={"Last-Modified": "not a date"})
        return httpx.Response(200, content=b"id\n1\n2\n3\n4\n")

    _mock_site(monkeypatch, answer)

    with pytest.raises(_usage_error(), match=r"does not say when 'a\.csv' was modified"):
        files.read("site", "a.csv", since=None)
    with pytest.raises(CapExceeded, match=r"'a\.csv' is more than max_file_bytes \(5\)"):
        files.read("site", "a.csv", max_file_bytes=5)
    assert files.read("site", "a.csv").num_rows == 4
    listed = files.list("site", "a.csv")
    assert listed.column("size").to_pylist() == [0]
    assert listed.column("modified").to_pylist() == [None]


def test_an_https_read_that_fails_after_the_head_is_named(monkeypatch: pytest.MonkeyPatch) -> None:
    """A GET that answers 500 or drops the connection is a retried `SourceError`."""
    import httpx

    def answer(request: httpx.Request) -> httpx.Response:
        if request.method == "HEAD":
            return httpx.Response(200)
        if "b.csv" in str(request.url):
            raise httpx.ReadError("connection reset", request=request)
        return httpx.Response(500)

    _mock_site(monkeypatch, answer)

    with pytest.raises(SourceError, match=r"^`site`: HTTP 500 for a\.csv$"):
        files.read("site", "a.csv")
    with pytest.raises(SourceError, match=r"^`site`: connection reset$"):
        files.read("site", "b.csv")


def test_an_http_ca_or_bundle_that_cannot_be_used_is_a_usage_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A CA that is no PEM certificate, and a bundle that is not there, each say what to fix."""
    usage = _usage_error()
    bad_ca = _connection(
        "http", {"base_url": "https://a.example"}, {"auth": "none", "ca_certificate": "not pem"}
    )
    with pytest.raises(usage, match=r"the CA certificate of `src` .* holds no PEM certificate"):
        _http.client_for(bad_ca)

    monkeypatch.setenv("LAKANA_CA_BUNDLE", str(tmp_path / "missing.pem"))
    plain = _connection("http", {"base_url": "https://a.example"}, {"auth": "none"})
    with pytest.raises(usage, match=r"LAKANA_CA_BUNDLE\) cannot be read .*LAKANA_RUN__CA_BUNDLE"):
        _http.client_for(plain)


def test_a_store_without_obstore_or_for_another_kind_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the assets extra the message names it; a kind without a store is named."""
    import sys

    usage = _usage_error()
    with pytest.raises(usage, match=r"`src` is http, which has no object store"):
        _stores.store_for(_connection("http", {"base_url": "https://a.example"}))
    monkeypatch.setitem(sys.modules, "obstore.store", None)
    with pytest.raises(usage, match=r"add lakana\[assets\] to the project's dependencies"):
        _stores.store_for(_connection("s3", _S3, {"access_key_id": "AKID"}))


def test_a_url_with_a_bad_port_and_a_bare_certificate_word_are_handled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A URL that cannot be parsed keeps only its scheme; a bare later mention is not appended."""
    said = "Error at https://lakana:pw-0123@h:99999/x?sig=s\nthe certificate chain is incomplete"
    _denied_task(monkeypatch, obstore.exceptions.GenericError(said))

    with pytest.raises(SourceRefused) as caught:
        files.read("lake", "*.csv")
    assert "(Error at https://…)." in str(caught.value)
    assert "pw-0123" not in str(caught.value)


def test_files_list_on_a_store_names_max_files(memory: MemoryStore) -> None:
    """`files.list` stops at its cap on a store as on a directory."""
    obstore.put(memory, "a.csv", b"id\n1\n")
    obstore.put(memory, "b.csv", b"id\n1\n")

    with pytest.raises(CapExceeded, match=r"`lake`: the pattern '\*\.csv' matches more than"):
        files.list("lake", "*.csv", max_files=1)


def test_a_403_in_a_generic_store_error_is_an_authentication_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 403 that obstore reports as a generic error is a refused credential.

    The store's code and message are kept, the XML around them dropped.
    """
    said = (
        "Generic S3 error: Error performing list request: Error performing GET "
        "https://s3.example.com/landing?list-type=2 in 3ms - Server returned non-2xx status "
        'code: 403 Forbidden: <?xml version="1.0" encoding="UTF-8"?>\n<Error><Code>'
        "SignatureDoesNotMatch</Code><Message>The request signature we calculated does not "
        "match.</Message></Error>\n\nDebug source:\nGeneric { status: 403 }"
    )
    _denied_task(monkeypatch, obstore.exceptions.GenericError(said))

    with pytest.raises(SourceRefused) as caught:
        files.read("lake", "*.csv")
    assert str(caught.value) == (
        "`lake`: authentication failed (the store said: Generic S3 error: Error performing list "
        "request: Error performing GET https://s3.example.com/landing?… in 3ms - Server "
        "returned non-2xx status code: 403 Forbidden; SignatureDoesNotMatch: The request "
        "signature we calculated does not match.). Check the secret with `lakana connection "
        "test lake --env dev`."
    )
