"""Local servers for the egress tests: an HTTPS server with a throwaway CA, and a CONNECT proxy.

Shared by the worker's egress tests and the server's notification tests, so it lives in
`scripts/test_support` rather than in either package's `tests/` directory.
"""

from __future__ import annotations

import datetime
import http.server
import ipaddress
import select
import socket
import ssl
import threading
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import cast

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID


def _name(common_name: str) -> x509.Name:
    """Build a subject name.

    Parameters
    ----------
    common_name
        The common name.

    Returns
    -------
    x509.Name
        The name.
    """
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


@dataclass
class Authority:
    """A throwaway certificate authority.

    Parameters
    ----------
    directory
        Where the PEM files are written.
    """

    directory: Path
    key: ec.EllipticCurvePrivateKey = field(init=False)
    cert: x509.Certificate = field(init=False)
    bundle: Path = field(init=False)

    def __post_init__(self) -> None:
        self.key = ec.generate_private_key(ec.SECP256R1())
        now = datetime.datetime.now(datetime.UTC)
        self.cert = (
            x509.CertificateBuilder()
            .subject_name(_name("test ca"))
            .issuer_name(_name("test ca"))
            .public_key(self.key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
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
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(self.key.public_key()), False)
            .sign(self.key, hashes.SHA256())
        )
        self.bundle = self.directory / "ca.pem"
        self.bundle.write_bytes(self.cert.public_bytes(serialization.Encoding.PEM))

    def issue(self, name: str, *, ip: str | None = None) -> tuple[Path, Path]:
        """Issue a server certificate.

        Parameters
        ----------
        name
            The DNS name in the certificate. With `ip`, the DNS name is left out.
        ip
            An IP address to put in the certificate instead of a name.

        Returns
        -------
        tuple[Path, Path]
            The certificate and key PEM files.
        """
        key = ec.generate_private_key(ec.SECP256R1())
        now = datetime.datetime.now(datetime.UTC)
        san = x509.IPAddress(ipaddress.ip_address(ip)) if ip else x509.DNSName(name)
        cert = (
            x509.CertificateBuilder()
            .subject_name(_name(name))
            .issuer_name(self.cert.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([san]), critical=False)
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(self.key.public_key()), False
            )
            .sign(self.key, hashes.SHA256())
        )
        stem = (ip or name).replace(":", "_")
        cert_path = self.directory / f"{stem}.crt"
        key_path = self.directory / f"{stem}.key"
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        return cert_path, key_path


class _Handler(http.server.BaseHTTPRequestHandler):
    """Record the request and answer with the server's configured status (and body, for GET)."""

    def do_GET(self) -> None:
        """Answer a GET with the server's status, headers and body."""
        server = cast(Server, self.server)
        server.requests.append((self.path, dict(self.headers), b""))
        self.send_response(server.status)
        if server.location:
            self.send_header("Location", server.location)
        for name, value in server.headers.items():
            self.send_header(name, value)
        if server.send_length:
            self.send_header("Content-Length", str(len(server.body)))
        self.end_headers()
        self.wfile.write(server.body)

    def do_POST(self) -> None:
        """Answer a POST."""
        server = cast(Server, self.server)
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        server.requests.append((self.path, dict(self.headers), body))
        self.send_response(server.status)
        if server.location:
            self.send_header("Location", server.location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        """Stay quiet.

        Parameters
        ----------
        format
            Unused.
        *args
            Unused.
        """


class Server(http.server.ThreadingHTTPServer):
    """A loopback server, with TLS when a certificate is given.

    Parameters
    ----------
    certificate
        The certificate and key files, or `None` for plain http.
    """

    daemon_threads = True

    def __init__(self, certificate: tuple[Path, Path] | None = None) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.status = 200
        self.location: str | None = None
        # What a GET answers: the body, extra headers, and whether a Content-Length is sent
        # (without one, the end of the connection ends the body).
        self.body = b""
        self.headers: dict[str, str] = {}
        self.send_length = True
        self.requests: list[tuple[str, dict[str, str], bytes]] = []
        self.sni: list[str | None] = []
        self.context: ssl.SSLContext | None = None
        if certificate is not None:
            self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            self.context.load_cert_chain(certificate[0], certificate[1])
            self.context.sni_callback = self._record_sni
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)

    def _record_sni(self, _sock: ssl.SSLObject, name: str, _ctx: ssl.SSLContext) -> None:
        """Remember the name the client asked for.

        Parameters
        ----------
        _sock
            Unused.
        name
            The SNI name.
        _ctx
            Unused.
        """
        self.sni.append(name)

    def get_request(self) -> tuple[socket.socket, object]:
        """Accept a connection and start TLS on it when the server has a certificate.

        Returns
        -------
        tuple[socket.socket, object]
            The connection and the peer address.
        """
        conn, addr = super().get_request()
        if self.context is not None:
            conn = self.context.wrap_socket(conn, server_side=True)
        return conn, addr

    @property
    def port(self) -> int:
        """The port the server listens on.

        Returns
        -------
        int
            The port.
        """
        return int(self.server_address[1])

    def __enter__(self) -> Server:
        self._thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.shutdown()
        self.server_close()


class ConnectProxy:
    """A proxy that tunnels `CONNECT` requests to one fixed upstream and records their targets.

    Parameters
    ----------
    upstream_port
        The loopback port every tunnel leads to.
    certificate
        The certificate and key files to serve the proxy itself over TLS, or `None` for plain
        http.
    """

    def __init__(self, upstream_port: int, certificate: tuple[Path, Path] | None = None) -> None:
        self.targets: list[str] = []
        self._context: ssl.SSLContext | None = None
        if certificate is not None:
            self._context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            self._context.load_cert_chain(certificate[0], certificate[1])
        self._upstream = upstream_port
        self._listener = socket.socket()
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen()
        self.port = int(self._listener.getsockname()[1])
        self._thread = threading.Thread(target=self._accept, daemon=True)

    def _accept(self) -> None:
        """Serve connections until the listener closes."""
        while True:
            try:
                conn, _ = self._listener.accept()
            except OSError:
                return
            threading.Thread(target=self._tunnel, args=(conn,), daemon=True).start()

    def _tunnel(self, conn: socket.socket) -> None:
        """Answer one `CONNECT`, or forward one absolute-form request, and relay bytes both ways.

        Parameters
        ----------
        conn
            The client connection.
        """
        with conn:
            if self._context is not None:
                try:
                    conn = self._context.wrap_socket(conn, server_side=True)
                except OSError:
                    return
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                data += chunk
            method, target = data.split(b" ")[:2]
            self.targets.append(target.decode())
            with socket.create_connection(("127.0.0.1", self._upstream)) as upstream:
                if method == b"CONNECT":
                    conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                else:
                    upstream.sendall(data)
                while True:
                    ready, _, _ = select.select([conn, upstream], [], [], 5)
                    if not ready:
                        return
                    for source in ready:
                        chunk = source.recv(65536)
                        if not chunk:
                            return
                        (upstream if source is conn else conn).sendall(chunk)

    def __enter__(self) -> ConnectProxy:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._listener.close()
