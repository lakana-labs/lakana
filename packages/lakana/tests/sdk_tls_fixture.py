"""HTTPS servers on loopback with certificates of a test authority, for the query process.

The query process reads presigned HTTPS URLs through DuckDB's httpfs, which loads a CA bundle
from the local disk. A store with TLS cannot be made to play every case a test needs (a second
host, a certificate of another authority, a redirect to another server), so this module plays
them in the test process: a certificate authority made with `cryptography`, a leaf certificate
for ``127.0.0.1`` and ``localhost`` signed by it, and a threaded `http.server` whose socket is
wrapped in an `ssl.SSLContext`. `SimpleHTTPRequestHandler` answers the ``Range`` requests
DuckDB's readers make.

`make_authority` makes an authority; `serving` starts a server with a leaf of the given
authority. A test of certificate verification starts a server of one authority and gives the
query process the file of another (`make_authority` twice).
"""

from __future__ import annotations

import datetime as dt
import functools
import http.server
import ipaddress
import socket
import socketserver
import ssl
import threading
from collections.abc import Callable, Generator, Iterable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

_VALID = dt.timedelta(days=2)


@dataclass(frozen=True)
class Authority:
    """A certificate authority of a test.

    Attributes
    ----------
    name
        Its common name.
    certificate
        Its self-signed certificate.
    key
        Its private key, which signs leaf certificates.
    ca_file
        A PEM file holding the certificate only: what the query process is given as its bundle.
    """

    name: str
    certificate: x509.Certificate
    key: ec.EllipticCurvePrivateKey
    ca_file: Path


def make_authority(directory: Path, name: str = "Lakana Test CA") -> Authority:
    """Make a certificate authority and write its certificate to a PEM file.

    Parameters
    ----------
    directory
        Where the PEM file is written (``<name>.pem``, spaces replaced).
    name
        The authority's common name; two authorities of one test need two names.

    Returns
    -------
    Authority
        The authority.
    """
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = dt.datetime.now(dt.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + _VALID)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(key, hashes.SHA256())
    )
    ca_file = directory / f"{name.replace(' ', '-')}.pem"
    ca_file.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    return Authority(name, certificate, key, ca_file)


def _leaf(authority: Authority, directory: Path) -> tuple[Path, Path]:
    """Issue a server certificate for ``127.0.0.1`` and ``localhost`` and write it with its key.

    Parameters
    ----------
    authority
        The authority that signs it.
    directory
        Where the two PEM files are written.

    Returns
    -------
    tuple of (Path, Path)
        The certificate file and the key file.
    """
    return issue_leaf(authority, directory)


def issue_leaf(
    authority: Authority,
    directory: Path,
    names: tuple[str, ...] = ("127.0.0.1", "localhost"),
) -> tuple[Path, Path]:
    """Issue a server certificate for some names and write it with its key.

    Parameters
    ----------
    authority
        The authority that signs it.
    directory
        Where the two PEM files are written (``<authority>-leaf.pem`` and ``.key``).
    names
        Its subject-alternative names: an IP literal becomes an ``IPAddress``, anything else a
        ``DNSName``. The first is also the common name.

    Returns
    -------
    tuple of (Path, Path)
        The certificate file and the key file.
    """
    alternatives: list[x509.GeneralName] = []
    for name in names:
        try:
            alternatives.append(x509.IPAddress(ipaddress.ip_address(name)))
        except ValueError:
            alternatives.append(x509.DNSName(name))
    key = ec.generate_private_key(ec.SECP256R1())
    now = dt.datetime.now(dt.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0])]))
        .issuer_name(authority.certificate.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + _VALID)
        .add_extension(
            x509.SubjectAlternativeName(alternatives),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        # Python 3.13's default context verifies strictly (VERIFY_X509_STRICT): a leaf without
        # its issuer's key identifier is refused, as a real company CA's leaf never is.
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(authority.key.public_key()),
            critical=False,
        )
        .add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
        .sign(authority.key, hashes.SHA256())
    )
    stem = authority.name.replace(" ", "-")
    cert_file = directory / f"{stem}-leaf.pem"
    key_file = directory / f"{stem}-leaf.key"
    cert_file.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return cert_file, key_file


class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """A throwaway HTTPS server whose threads never outlive the test."""

    daemon_threads = True


class _Handler(http.server.SimpleHTTPRequestHandler):
    """Serves files from a directory, answers listed paths with a redirect, logs nothing.

    Parameters
    ----------
    request
        The connection, as `socketserver` hands it over.
    client_address
        The client's address.
    server
        The server.
    directory
        The document root.
    redirects
        Request path to the URL its ``302`` answer names.
    seen
        Every request line is appended to it, so a test can tell what reached the server.
    """

    def __init__(
        self,
        request: socket.socket,
        client_address: tuple[str, int],
        server: socketserver.BaseServer,
        *,
        directory: str,
        redirects: Mapping[str, str],
        seen: list[str],
    ) -> None:
        self._redirects = redirects
        self._seen = seen
        super().__init__(request, client_address, server, directory=directory)

    def _redirect(self) -> bool:
        """Answer with a redirect if the path is listed.

        Returns
        -------
        bool
            Whether a redirect was sent.
        """
        self._seen.append(f"{self.command} {self.path}")
        target = self._redirects.get(self.path.split("?", 1)[0])
        if target is None:
            return False
        self.send_response(302)
        self.send_header("Location", target)
        self.send_header("Content-Length", "0")
        self.end_headers()
        return True

    def do_GET(self) -> None:
        """Answer a GET: a redirect or the file."""
        if not self._redirect():
            super().do_GET()

    def do_HEAD(self) -> None:
        """Answer a HEAD: a redirect or the file's headers."""
        if not self._redirect():
            super().do_HEAD()

    def log_message(self, format: str, *args: object) -> None:
        """Log nothing: a test's output is not the place for request lines.

        Parameters
        ----------
        format
            The format string, unused.
        *args
            Its arguments, unused.
        """


@dataclass(frozen=True)
class Serving:
    """A running HTTPS server.

    Attributes
    ----------
    base
        ``https://127.0.0.1:<port>``, without a trailing slash.
    seen
        The request lines the server received (``GET /path``), in order.
    """

    base: str
    seen: list[str]

    def url(self, path: str) -> str:
        """Name a file the server serves.

        Parameters
        ----------
        path
            The path below the served directory, without a leading slash.

        Returns
        -------
        str
            Its URL.
        """
        return f"{self.base}/{path}"


@contextmanager
def serving(
    directory: Path,
    authority: Authority,
    certificates: Path,
    redirects: Mapping[str, str] | None = None,
) -> Generator[Serving, None, None]:
    """Serve a directory over HTTPS on loopback, with a leaf certificate of `authority`.

    Parameters
    ----------
    directory
        The document root.
    authority
        Signs the server's certificate.
    certificates
        Where the leaf certificate and key are written (not inside `directory`).
    redirects
        Request path (``/a/b.parquet``) to the absolute URL a ``302`` sends the client to.

    Yields
    ------
    Serving
        The server's base URL and the requests it received.
    """
    cert_file, key_file = _leaf(authority, certificates)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert_file, key_file)
    seen: list[str] = []
    handler = functools.partial(
        _Handler, directory=str(directory), redirects=dict(redirects or {}), seen=seen
    )
    server = _Server(("127.0.0.1", 0), handler)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield Serving(f"https://127.0.0.1:{server.server_address[1]}", seen)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class _Quiet(_Server):
    """A throwaway HTTPS server that prints nothing when a client hangs up early.

    A reachability check closes right after the handshake.
    """

    def handle_error(self, request: object, client_address: object) -> None:
        """Print nothing.

        Parameters
        ----------
        request
            Unused.
        client_address
            Unused.
        """


@dataclass(frozen=True)
class Exchange:
    """One request a scripted server received.

    Attributes
    ----------
    method
        ``GET``, ``POST`` or ``HEAD``.
    target
        The request target: path and query, as sent.
    headers
        The headers, names in lower case.
    body
        The request body.
    """

    method: str
    target: str
    headers: dict[str, str]
    body: bytes


Answer = tuple[int, Mapping[str, str], bytes | Iterable[bytes]]
"""A scripted answer: status, headers, and a body (bytes, or chunks written until done or the
client hangs up; without ``Content-Length`` the body ends when the connection closes)."""


@dataclass(frozen=True)
class Scripted:
    """A running scripted HTTPS server.

    Attributes
    ----------
    base
        ``https://<host>:<port>``, without a trailing slash.
    seen
        Every request it received, in order.
    """

    base: str
    seen: list[Exchange]


@contextmanager
def scripted(
    authority: Authority,
    certificates: Path,
    answer: Callable[[Exchange], Answer],
    host: str = "127.0.0.1",
) -> Generator[Scripted, None, None]:
    """Serve HTTPS on loopback, answering each request with what `answer` returns.

    The server speaks HTTP/1.0 (one request per connection), its TLS floor is 1.2, and its
    certificate, of `authority`, names ``127.0.0.1`` and ``localhost``.

    Parameters
    ----------
    authority
        Signs the server's certificate.
    certificates
        Where the leaf certificate and key are written.
    answer
        Called with each request; returns the status, headers and body.
    host
        The host the base URL names: ``127.0.0.1`` or ``localhost``.

    Yields
    ------
    Scripted
        The base URL and the requests seen.
    """
    cert_file, key_file = _leaf(authority, certificates)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.minimum_version = ssl.TLSVersion.TLSv1_2
    tls.load_cert_chain(cert_file, key_file)
    seen: list[Exchange] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        """Records each request and writes the scripted answer."""

        def _serve(self) -> None:
            """Record the request, then write the answer."""
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            exchange = Exchange(
                self.command, self.path, {k.lower(): v for k, v in self.headers.items()}, body
            )
            seen.append(exchange)
            status, headers, content = answer(exchange)
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            if isinstance(content, bytes) and "Content-Length" not in headers:
                self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            if self.command == "HEAD":
                return
            try:
                for chunk in [content] if isinstance(content, bytes) else content:
                    self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError, ssl.SSLError):
                return

        do_GET = do_POST = do_HEAD = _serve

        def log_message(self, format: str, *args: object) -> None:
            """Log nothing.

            Parameters
            ----------
            format
                Unused.
            *args
                Unused.
            """

    server = _Quiet(("127.0.0.1", 0), Handler)
    server.socket = tls.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield Scripted(f"https://{host}:{server.server_address[1]}", seen)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
