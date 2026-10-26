"""`files.read` against object-store emulators over TLS, directly and through the egress proxy.

Each emulator serves TLS with a leaf certificate of a test authority, and the task trusts that
authority only through `LAKANA_CA_BUNDLE` (Mozilla's roots plus the authority, as the worker
builds it), the way a private-CA store is trusted in production. The images, their digests and
licences are recorded in `docs/agent/testing.md` ("Object-store emulators").
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import ipaddress
import json
import os
import socket
import ssl
import threading
import time
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from email.utils import formatdate
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

import httpx
import obstore
import pytest
import urllib3
from minio import Minio
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy
from tls_fixture import Authority, issue_leaf, make_authority

from lakana import context
from lakana.ingest import SourceError, SourceRefused, _connections, _stores, connections, files
from lakana_worker.launcher.egress import EgressServer, EgressSettings
from lakana_worker.launcher.egress_token import ensure_key, mint
from lakana_worker.trust import build_trust_bundle

pytestmark = [pytest.mark.integration]

SEAWEEDFS = (
    "chrislusf/seaweedfs:4.47"
    "@sha256:ce9e796f1fe6f06968f4c04bdaf8f678dad9c8acdfef3d244133d71bfa6bf882"
)
FAKE_GCS = (
    "fsouza/fake-gcs-server:1.56.0"
    "@sha256:4957e37e252c8720cf861eb14e0b5aa671845e833c49285a45465ec7fa24e981"
)
AZURITE = (
    "mcr.microsoft.com/azure-storage/azurite:3.37.0"
    "@sha256:830430c1da1a2d537e08f3e6764dd1f5ae00cf0346bcaf625b968ec3f0971fd5"
)
_S3_KEY_ID, _S3_SECRET = "lakanatest", "lakanatestsecret12345"
_AZURE_ACCOUNT = "devstoreaccount1"
# Azurite's well-known development key, published in its documentation; not a secret.
_AZURE_KEY = (
    "Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw=="
)
_BUCKET = "landing"
_WRONG = "wrong-0123456789-wrong"


@dataclass(frozen=True)
class _Pki:
    """The test authority, its server certificate, and the worker's trust bundle.

    Parameters
    ----------
    authority
        Signs the emulators' certificates.
    cert
        The leaf certificate (PEM) for ``127.0.0.1`` and ``localhost``.
    key
        Its private key (PEM).
    bundle
        Mozilla's roots and the authority, as `build_trust_bundle` writes them.
    """

    authority: Authority
    cert: bytes
    key: bytes
    bundle: Path


@pytest.fixture(scope="module")
def pki(tmp_path_factory: pytest.TempPathFactory) -> _Pki:
    """Make the authority, one leaf for every emulator, and the trust bundle.

    Parameters
    ----------
    tmp_path_factory
        Pytest's directories.

    Returns
    -------
    _Pki
        The certificates and the bundle file.
    """
    directory = tmp_path_factory.mktemp("pki")
    authority = make_authority(directory)
    cert, key = issue_leaf(authority, directory)
    bundle = directory / "bundle.pem"
    bundle.write_bytes(build_trust_bundle(authority.ca_file) or b"")
    return _Pki(authority, cert.read_bytes(), key.read_bytes(), bundle)


def _container(image: str, pki: _Pki, port: int, ready: str, command: str) -> DockerContainer:
    """Configure an emulator with the leaf certificate copied in at ``/certs``.

    Parameters
    ----------
    image
        The pinned image.
    pki
        The certificates.
    port
        The port it serves inside the container.
    ready
        A log line that says it serves.
    command
        Its command.

    Returns
    -------
    DockerContainer
        Not started.
    """
    container = DockerContainer(image, _wait_strategy=LogMessageWaitStrategy(ready))
    container.with_exposed_ports(port)
    container.with_copy_into_container(pki.cert, "/certs/leaf.pem")
    container.with_copy_into_container(pki.key, "/certs/leaf.key")
    container.with_command(command)
    return container


def _base(container: DockerContainer, port: int) -> str:
    """Name the emulator's HTTPS address on loopback.

    Parameters
    ----------
    container
        The running emulator.
    port
        Its port inside the container.

    Returns
    -------
    str
        ``https://127.0.0.1:<host port>`` (the certificate's name; never ``localhost``, which
        may resolve to ``::1``).
    """
    return f"https://127.0.0.1:{container.get_exposed_port(port)}"


@pytest.fixture(scope="module")
def seaweedfs(pki: _Pki) -> Iterator[str]:
    """Run SeaweedFS's S3 gateway over TLS with one identity and the bucket.

    Parameters
    ----------
    pki
        The certificates.

    Yields
    ------
    str
        Its endpoint.
    """
    identities = {
        "identities": [
            {
                "name": "lakanatest",
                "credentials": [{"accessKey": _S3_KEY_ID, "secretKey": _S3_SECRET}],
                "actions": ["Admin", "Read", "Write"],
            }
        ]
    }
    container = _container(
        SEAWEEDFS,
        pki,
        8333,
        "Starting S3 API Server",
        "server -s3 -s3.port=8333 -s3.config=/etc/seaweedfs/s3.json "
        "-s3.cert.file=/certs/leaf.pem -s3.key.file=/certs/leaf.key -dir=/data "
        "-ip.bind=0.0.0.0",
    )
    container.with_copy_into_container(json.dumps(identities).encode(), "/etc/seaweedfs/s3.json")
    with container:
        endpoint = _base(container, 8333)
        pool = urllib3.PoolManager(ca_certs=str(pki.authority.ca_file))
        client = Minio(
            endpoint.removeprefix("https://"),
            access_key=_S3_KEY_ID,
            secret_key=_S3_SECRET,
            secure=True,
            http_client=pool,
        )
        deadline = time.monotonic() + 60
        while True:  # the gateway logs that it starts before its filer answers
            try:
                client.make_bucket(_BUCKET)
                break
            except Exception:
                if time.monotonic() > deadline:
                    raise
                time.sleep(1)
        yield endpoint


@pytest.fixture(scope="module")
def fake_gcs(pki: _Pki) -> Iterator[str]:
    """Run fake-gcs-server over TLS, with the bucket.

    It serves its XML API (where obstore lists, reads and writes) only on its public host,
    which must be the host and port the client sends: the host port is chosen first and bound.

    Parameters
    ----------
    pki
        The certificates.

    Yields
    ------
    str
        Its base URL.
    """
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    container = _container(
        FAKE_GCS,
        pki,
        4443,
        "server started at",
        "-scheme https -port 4443 -cert-location /certs/leaf.pem "
        f"-private-key-location /certs/leaf.key -backend memory -public-host 127.0.0.1:{port}",
    )
    container.with_bind_ports(4443, port)
    with container:
        base = f"https://127.0.0.1:{port}"
        response = httpx.post(
            f"{base}/storage/v1/b",
            json={"name": _BUCKET},
            verify=ssl.create_default_context(cafile=pki.authority.ca_file),
            timeout=30,
        )
        response.raise_for_status()
        yield base


def _azure_signature(string_to_sign: str) -> str:
    """Sign text with Azurite's account key, as Shared Key and account SAS do.

    Parameters
    ----------
    string_to_sign
        The text.

    Returns
    -------
    str
        The base64 HMAC-SHA256.
    """
    digest = hmac.new(base64.b64decode(_AZURE_KEY), string_to_sign.encode(), hashlib.sha256)
    return base64.b64encode(digest.digest()).decode()


@pytest.fixture(scope="module")
def azurite(pki: _Pki) -> Iterator[str]:
    """Run Azurite's blob service over TLS, with the container.

    The container is created with one Shared Key request signed here (obstore creates none).

    Parameters
    ----------
    pki
        The certificates.

    Yields
    ------
    str
        The account's endpoint, ``https://127.0.0.1:<port>/devstoreaccount1``.
    """
    container = _container(
        AZURITE,
        pki,
        10000,
        "Azurite Blob service successfully listens on",
        "azurite-blob --blobHost 0.0.0.0 --blobPort 10000 --cert /certs/leaf.pem "
        "--key /certs/leaf.key --skipApiVersionCheck --loose -l /data",
    )
    with container:
        endpoint = f"{_base(container, 10000)}/{_AZURE_ACCOUNT}"
        date, version = formatdate(usegmt=True), "2021-08-06"
        resource = f"/{_AZURE_ACCOUNT}/{_AZURE_ACCOUNT}/{_BUCKET}\nrestype:container"
        signed = "\n".join(
            ["PUT", *[""] * 11, f"x-ms-date:{date}\nx-ms-version:{version}", resource]
        )
        response = httpx.put(
            f"{endpoint}/{_BUCKET}?restype=container",
            headers={
                "x-ms-date": date,
                "x-ms-version": version,
                "Authorization": f"SharedKey {_AZURE_ACCOUNT}:{_azure_signature(signed)}",
            },
            verify=ssl.create_default_context(cafile=pki.authority.ca_file),
            timeout=30,
        )
        assert response.status_code == 201, response.text
        yield endpoint


def _account_sas() -> str:
    """Make an account SAS for blob objects and containers, valid for an hour.

    Returns
    -------
    str
        The token with its leading ``?``, as a team pastes it from the portal.
    """
    expiry = (dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    fields = {"sp": "rwdlac", "ss": "b", "srt": "sco", "se": expiry, "spr": "https"}
    version = "2019-12-12"
    # account, permissions, services, resource types, start, expiry, IP, protocol, version.
    signed = "\n".join(
        [_AZURE_ACCOUNT, fields["sp"], fields["ss"], fields["srt"], "", expiry, "", "https"]
    )
    signed = f"{signed}\n{version}\n"
    query = {"sv": version, **fields, "sig": _azure_signature(signed)}
    return "?" + "&".join(f"{k}={quote(v, safe='')}" for k, v in query.items())


@pytest.fixture(autouse=True)
def task_environment(pki: _Pki, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Trust the authority through the bundle only; leave no task state behind.

    Parameters
    ----------
    pki
        The bundle.
    monkeypatch
        Pytest's patcher.

    Yields
    ------
    None
        While the test runs.
    """
    for name in ("LAKANA_EGRESS_PROXY", "HTTPS_PROXY", "https_proxy", "SSL_CERT_FILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LAKANA_CA_BUNDLE", str(pki.bundle))
    yield
    context._set(None)
    _connections.clear()


def _declare(
    name: str,
    kind: str,
    address: Mapping[str, object],
    settings: Mapping[str, object],
    secret: Mapping[str, str],
) -> None:
    """Set the state of a task that declares one connection (as the harness does).

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
    """
    body = {
        "name": name,
        "kind": kind,
        "address": dict(address),
        "settings": dict(settings),
        "endpoints": [],
        "secret": dict(secret),
    }
    _connections.clear()
    context._set(
        context._TaskState(
            client=SimpleNamespace(task_connections=SimpleNamespace(get={name: body}.__getitem__)),  # ty: ignore[invalid-argument-type]
            environment="dev",
            asset="bronze.orders",
            declared=frozenset({name}),
            connections={name: {"kind": kind}},
        )
    )


@dataclass(frozen=True)
class _Kind:
    """How a test declares a connection of one kind against its emulator.

    Parameters
    ----------
    kind
        ``s3``, ``gcs`` or ``azure``.
    address
        The connection's address.
    settings
        Its settings, without ``prefix``.
    secret
        Its secret.
    """

    kind: str
    address: Mapping[str, object]
    settings: Mapping[str, object]
    secret: Mapping[str, str]

    def declare(self, prefix: str | None, **secret: str) -> None:
        """Declare `lake` with a prefix, and with secret values replaced.

        Parameters
        ----------
        prefix
            The connection's prefix; `None` for none.
        **secret
            Values to replace in the secret.
        """
        settings = dict(self.settings) | ({"prefix": prefix} if prefix else {})
        _declare("lake", self.kind, self.address, settings, {**self.secret, **secret})


def _gcs_key() -> str:
    """Write a service-account key with a real RSA key; the emulator does not check it.

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
    return json.dumps(
        {
            "type": "service_account",
            "private_key_id": "k1",
            "private_key": pem,
            "client_email": "reader@p.iam.gserviceaccount.com",
        }
    )


@pytest.fixture(params=["s3", "gcs", "azure-key", "azure-sas"])
def store(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> _Kind:
    """Start the emulator a case needs and say how to declare a connection to it.

    Parameters
    ----------
    request
        Which kind.
    monkeypatch
        Points the GCS and Azure endpoints at the emulators.

    Returns
    -------
    _Kind
        The kind's connection.
    """
    if request.param == "s3":
        endpoint = request.getfixturevalue("seaweedfs")
        return _Kind(
            "s3",
            {"bucket": _BUCKET, "region": "us-east-1", "endpoint": endpoint},
            {"access_key_id": _S3_KEY_ID},
            {"secret_access_key": _S3_SECRET},
        )
    if request.param == "gcs":
        monkeypatch.setattr(_stores, "GCS_BASE_URL", request.getfixturevalue("fake_gcs"))
        return _Kind("gcs", {"bucket": _BUCKET}, {}, {"service_account_json": _gcs_key()})
    monkeypatch.setattr(_stores, "AZURE_ENDPOINT", request.getfixturevalue("azurite"))
    secret = {"account_key": _AZURE_KEY} if request.param == "azure-key" else {}
    if request.param == "azure-sas":
        secret = {"sas_token": _account_sas()}
    return _Kind("azure", {"account": _AZURE_ACCOUNT, "container": _BUCKET}, {}, secret)


def _put(kind: _Kind, objects: Mapping[str, bytes]) -> None:
    """Put objects through obstore with the connection's own store, without a prefix.

    fake-gcs-server refuses obstore's upload of a key with ``/`` (it reads the escaped key as
    an upload without ``uploadType``), so GCS objects go through its JSON media upload; the
    reads under test are obstore's.

    Parameters
    ----------
    kind
        The connection.
    objects
        Key to content.
    """
    kind.declare(None)
    if kind.kind == "gcs":
        for key, data in objects.items():
            httpx.post(
                f"{_stores.GCS_BASE_URL}/upload/storage/v1/b/{_BUCKET}/o",
                params={"uploadType": "media", "name": key},
                content=data,
                verify=ssl.create_default_context(cafile=os.environ["LAKANA_CA_BUNDLE"]),
                timeout=30,
            ).raise_for_status()
        return
    store = _stores.store_for(connections.get("lake"))
    for key, data in objects.items():
        obstore.put(store, key, data)


def test_a_store_lists_and_reads_a_pattern_under_the_prefix(store: _Kind) -> None:
    """Only keys below the prefix are listed; the pattern is relative to it."""
    run = uuid.uuid4().hex[:8]
    _put(
        store,
        {
            f"{run}/in/orders/a.csv": b"id\n1\n",
            f"{run}/in/orders/b.csv": b"id\n2\n",
            f"{run}/in/orders/notes.txt": b"x",
            f"{run}/outside/orders/c.csv": b"id\n3\n",
        },
    )
    store.declare(f"{run}/in")

    table = files.read("lake", "orders/*.csv")
    listed = files.list("lake", "**/*.csv")

    assert sorted(table.column("id").to_pylist()) == [1, 2]
    assert listed.column("name").to_pylist() == ["orders/a.csv", "orders/b.csv"]


def test_a_store_since_uses_last_modified(store: _Kind) -> None:
    """`since` compares each object's last-modified time."""
    run = uuid.uuid4().hex[:8]
    _put(store, {f"{run}/a.csv": b"id\n1\n"})
    store.declare(run)
    mark = files.list("lake", "*.csv").column("modified").to_pylist()[0]
    time.sleep(1.1)  # emulators keep whole seconds
    _put(store, {f"{run}/b.csv": b"id\n2\n"})
    store.declare(run)

    table = files.read("lake", "*.csv", since=mark)

    assert table.column("id").to_pylist() == [2]
    assert table.column("_source_modified").to_pylist()[0] > mark


def test_a_store_without_the_trust_bundle_is_refused_as_a_certificate(
    store: _Kind, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without the bundle the private authority is not trusted: not retried, names the bundle."""
    store.declare(None)
    monkeypatch.delenv("LAKANA_CA_BUNDLE")

    with pytest.raises(SourceRefused, match=r"certificate is not trusted .*LAKANA_RUN__CA_BUNDLE"):
        files.list("lake", "*.csv")


@pytest.mark.parametrize("kind", ["s3", "azure-key"])
def test_a_wrong_secret_is_refused_as_authentication(
    kind: str, request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refused key fails at once, naming the test command, and never shows the secret.

    SeaweedFS and Azurite check signatures; fake-gcs-server accepts any key, so GCS is not
    asserted here.
    """
    if kind == "s3":
        endpoint = request.getfixturevalue("seaweedfs")
        _declare(
            "lake",
            "s3",
            {"bucket": _BUCKET, "region": "us-east-1", "endpoint": endpoint},
            {"access_key_id": _S3_KEY_ID},
            {"secret_access_key": _WRONG},
        )
    else:
        monkeypatch.setattr(_stores, "AZURE_ENDPOINT", request.getfixturevalue("azurite"))
        wrong = base64.b64encode(b"wrong-account-key-0123456789").decode()
        _declare(
            "lake",
            "azure",
            {"account": _AZURE_ACCOUNT, "container": _BUCKET},
            {},
            {"account_key": wrong},
        )

    with pytest.raises(SourceRefused) as caught:
        files.list("lake", "*.csv")
    message = str(caught.value)
    assert "authentication failed (the store said: " in message
    assert "Check the secret with `lakana connection test lake --env dev`." in message
    assert _WRONG not in message
    assert "d3Jvbmct" not in message  # the base64 of the wrong key


@pytest.fixture
def egress(tmp_path: Path) -> Iterator[tuple[int, bytes]]:
    """Run the real egress proxy on loopback, allowed to reach loopback as a test needs.

    Parameters
    ----------
    tmp_path
        For its key, its refused ranges and its routing tables.

    Yields
    ------
    tuple of (int, bytes)
        Its port and its signing key.
    """
    key = ensure_key(tmp_path / "egress.key")
    (tmp_path / "refused.json").write_text(json.dumps({"refused": []}), encoding="utf-8")
    # The proxy refuses every direct connection without its attached networks: one network,
    # 10.207.0.8/29, stands in for a task's (the IPv6 table may be missing, as on many hosts).
    route = tmp_path / "proxy-route"
    route.write_text(
        "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\n"
        "eth1\t0800CF0A\t00000000\t0001\t0\t0\t0\tF8FFFFFF\n",
        encoding="ascii",
    )
    server = EgressServer(
        EgressSettings(
            key=tmp_path / "egress.key",
            listen=("127.0.0.1", 0),
            reachable=frozenset({ipaddress.ip_network("127.0.0.0/8")}),
            route=route,
            route6=tmp_path / "no-ipv6-route",
        )
    )
    threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True).start()
    try:
        yield server.server_address[1], key
    finally:
        server.shutdown()
        server.server_close()


def test_each_store_reads_through_the_egress_proxy(
    store: _Kind, egress: tuple[int, bytes], monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the task's proxy the read goes through it; a token for another port is refused."""
    port, key = egress
    run = uuid.uuid4().hex[:8]
    _put(store, {f"{run}/a.csv": b"id\n1\n"})
    target = int(
        str(_stores.GCS_BASE_URL if store.kind == "gcs" else _target(store)).rsplit(":", 1)[1]
    )

    def proxied(token_port: int) -> None:
        token = mint(
            key,
            [("127.0.0.1", token_port)],
            int(time.time()) + 600,
            purpose="task",
            network=ipaddress.ip_network("127.0.0.1/32"),
        )
        monkeypatch.setenv("LAKANA_EGRESS_PROXY", f"http://lakana:{token}@127.0.0.1:{port}")
        store.declare(run)

    proxied(target)
    assert files.read("lake", "*.csv").column("id").to_pylist() == [1]

    proxied(target + 1)
    with pytest.raises(SourceError) as caught:
        files.list("lake", "*.csv")
    assert not isinstance(caught.value, SourceRefused)
    assert "lakana:" not in str(caught.value)


def _target(kind: _Kind) -> str:
    """Name the ``host:port`` part of an S3 or Azure emulator's address.

    Parameters
    ----------
    kind
        The connection.

    Returns
    -------
    str
        ``https://127.0.0.1:<port>``.
    """
    if kind.kind == "s3":
        return str(kind.address["endpoint"])
    return _stores.AZURE_ENDPOINT.rsplit("/", 1)[0]
