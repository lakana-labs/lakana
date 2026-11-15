"""Git test helpers: build repositories and serve them over HTTPS with `git http-backend`.

The server emulates the providers Lakana talks to using only the git
protocol: basic auth with any username (GitHub PATs), a fixed username
(GitLab deploy tokens), a configurable status for a rejected credential
(GitHub answers 404 for a private repository with a bad token), protocol v2
or v0, and an optional delay to exercise timeouts. Plain helpers, not
fixtures; the module name is repo-unique (see `core_test_helpers`).
"""

from __future__ import annotations

import base64
import datetime as dt
import ipaddress
import os
import select
import socket
import socketserver
import ssl
import subprocess  # nosec B404 -- test helper driving the git binary
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

_IDENTITY = {
    "GIT_AUTHOR_NAME": "Lakana Test",
    "GIT_AUTHOR_EMAIL": "test@lakana.invalid",
    "GIT_COMMITTER_NAME": "Lakana Test",
    "GIT_COMMITTER_EMAIL": "test@lakana.invalid",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
}


def git(cwd: Path, *args: str) -> str:
    """Run git in `cwd` with a fixed identity and no user config.

    Parameters
    ----------
    cwd
        Working directory.
    *args
        Arguments after ``git``.

    Returns
    -------
    str
        Stripped stdout.
    """
    result = subprocess.run(  # nosec B603 B607 -- fixed argv in tests
        ["git", "-c", "init.defaultBranch=main", *args],
        cwd=cwd,
        env={**os.environ, **_IDENTITY},
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def make_repo(path: Path, files: dict[str, str]) -> str:
    """Create a repository at `path` with one commit of `files` on `main`.

    Parameters
    ----------
    path
        Directory to create.
    files
        Repository-relative path → content.

    Returns
    -------
    str
        The commit SHA.
    """
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "--quiet")
    return commit(path, files)


def commit(path: Path, files: dict[str, str], message: str = "change") -> str:
    """Write `files` into the work tree and commit everything.

    Parameters
    ----------
    path
        The repository.
    files
        Repository-relative path → content.
    message
        Commit message.

    Returns
    -------
    str
        The new commit SHA.
    """
    for relative, text in files.items():
        target = path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    git(path, "add", "--all")
    git(path, "commit", "--quiet", "--allow-empty", "-m", message)
    return git(path, "rev-parse", "HEAD")


def bare_copy(source: Path, served_root: Path, name: str) -> Path:
    """Clone `source` bare into `served_root/name.git`.

    Parameters
    ----------
    source
        A work-tree repository.
    served_root
        `GIT_PROJECT_ROOT` of the server.
    name
        Repository name.

    Returns
    -------
    Path
        The bare repository.
    """
    target = served_root / f"{name}.git"
    git(served_root, "clone", "--quiet", "--bare", str(source), str(target))
    return target


def self_signed_certificate(directory: Path) -> tuple[Path, Path]:
    """Write a self-signed certificate for 127.0.0.1 and its key.

    Parameters
    ----------
    directory
        Where to write ``cert.pem`` and ``key.pem``.

    Returns
    -------
    tuple of Path
        ``(cert, key)``.
    """
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = directory / "cert.pem", directory / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


@dataclass
class GitHttpServer:
    """`git http-backend` behind an in-process HTTPS server with basic auth.

    Parameters
    ----------
    project_root
        Directory of bare repositories served at ``/<name>.git``.
    certificate_dir
        Where the self-signed certificate is written.
    token
        The only accepted password.
    username
        The only accepted username, or `None` to accept any (GitHub mode).
    deny_status
        Status for a request whose credential is rejected (401, 403 or 404).
    protocol_v2
        Forward the client's `Git-Protocol` header; `False` serves v0.
    delay_seconds
        Sleep before answering every request.
    """

    project_root: Path
    certificate_dir: Path
    token: str = "s3cret-token-value"
    username: str | None = None
    deny_status: int = 401
    protocol_v2: bool = True
    delay_seconds: float = 0.0
    requests: list[tuple[str, str | None]] = field(default_factory=list)
    _server: _Server | None = None
    _thread: threading.Thread | None = None
    ca_file: Path | None = None

    def __enter__(self) -> GitHttpServer:
        cert, key = self_signed_certificate(self.certificate_dir)
        self.ca_file = cert
        server = _Server(("127.0.0.1", 0), _Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(cert, key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        server.git = self
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        assert self._server is not None
        self._server.shutdown()
        self._server.server_close()

    def url(self, name: str) -> str:
        """Return the HTTPS URL of a served repository.

        Parameters
        ----------
        name
            Repository name, without ``.git``.

        Returns
        -------
        str
            ``https://127.0.0.1:<port>/<name>.git``.
        """
        assert self._server is not None
        return f"https://127.0.0.1:{self._server.server_address[1]}/{name}.git"


class _Server(ThreadingHTTPServer):
    """A threading HTTP server that knows the `GitHttpServer` configuring it."""

    git: GitHttpServer


class _Handler(BaseHTTPRequestHandler):
    """Authenticates, then runs `git http-backend` as CGI."""

    def log_message(self, format: str, *args: object) -> None:
        """Silence the default stderr access log.

        Parameters
        ----------
        format
            Ignored.
        *args
            Ignored.
        """

    def do_GET(self) -> None:
        """Serve a GET."""
        self._serve()

    def do_POST(self) -> None:
        """Serve a POST."""
        self._serve()

    def _read_body(self) -> bytes:
        """Read a Content-Length or chunked request body.

        Returns
        -------
        bytes
            The body.
        """
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            chunks = []
            while True:
                size = int(self.rfile.readline().strip(), 16)
                if size == 0:
                    self.rfile.readline()
                    return b"".join(chunks)
                chunks.append(self.rfile.read(size))
                self.rfile.readline()
        return self.rfile.read(int(self.headers.get("Content-Length") or 0))

    def _serve(self) -> None:
        """Check basic auth and hand the request to `git http-backend`."""
        config = cast("_Server", self.server).git
        time.sleep(config.delay_seconds)
        header = self.headers.get("Authorization")
        user: str | None = None
        if header is None:
            config.requests.append((self.path, None))
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="git"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        user, _, password = base64.b64decode(header.split(" ", 1)[1]).decode().partition(":")
        config.requests.append((self.path, user))
        if password != config.token or (config.username is not None and user != config.username):
            self.send_response(config.deny_status)
            if config.deny_status == 401:
                self.send_header("WWW-Authenticate", 'Basic realm="git"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = self._read_body()
        path, _, query = self.path.partition("?")
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "GIT_PROJECT_ROOT": str(config.project_root),
            "GIT_HTTP_EXPORT_ALL": "1",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "PATH_INFO": path,
            "QUERY_STRING": query,
            "REQUEST_METHOD": self.command,
            "CONTENT_TYPE": self.headers.get("Content-Type", ""),
            "CONTENT_LENGTH": str(len(body)),
            "REMOTE_USER": user,
            "REMOTE_ADDR": "127.0.0.1",
        }
        if self.headers.get("Content-Encoding"):
            env["HTTP_CONTENT_ENCODING"] = self.headers["Content-Encoding"]
        if config.protocol_v2 and self.headers.get("Git-Protocol"):
            env["GIT_PROTOCOL"] = self.headers["Git-Protocol"]
        result = subprocess.run(  # nosec B603 B607 -- fixed argv in tests
            ["git", "http-backend"], input=body, env=env, capture_output=True, check=False
        )
        head, _, payload = result.stdout.partition(b"\r\n\r\n")
        status = 200
        headers = []
        for line in head.decode().split("\r\n"):
            name, _, value = line.partition(":")
            if name.lower() == "status":
                status = int(value.strip().split(" ")[0])
            elif name:
                headers.append((name, value.strip()))
        self.send_response(status)
        for name, value in headers:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@dataclass
class ConnectProxy:
    """A tiny in-process HTTP proxy that tunnels ``CONNECT`` (what git uses for HTTPS).

    Parameters
    ----------
    credentials
        ``user:password`` the proxy demands as ``Proxy-Authorization: Basic``;
        `None` accepts anyone.
    connects
        Every ``host:port`` a client asked to tunnel to, in order.
    authorizations
        The ``Proxy-Authorization`` header of each request (`None` when absent).
    """

    credentials: str | None = None
    connects: list[str] = field(default_factory=list)
    authorizations: list[str | None] = field(default_factory=list)
    _server: socketserver.ThreadingTCPServer | None = None

    def __enter__(self) -> ConnectProxy:
        proxy = self

        class Handler(socketserver.BaseRequestHandler):
            """Serve one CONNECT request, then relay bytes both ways."""

            def handle(self) -> None:
                """Authenticate, connect to the target and pipe."""
                reader = self.request.makefile("rb")
                while True:
                    request_line = reader.readline().decode().split()
                    if not request_line:
                        return
                    headers = {}
                    while (line := reader.readline().strip()) != b"":
                        name, _, value = line.decode().partition(":")
                        headers[name.lower()] = value.strip()
                    authorization = headers.get("proxy-authorization")
                    proxy.authorizations.append(authorization)
                    if request_line[0] != "CONNECT":
                        self.request.sendall(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
                        return
                    proxy.connects.append(request_line[1])
                    expected = (
                        None
                        if proxy.credentials is None
                        else "Basic " + base64.b64encode(proxy.credentials.encode()).decode()
                    )
                    if expected is None or authorization == expected:
                        break
                    self.request.sendall(
                        b"HTTP/1.1 407 Proxy Authentication Required\r\n"
                        b'Proxy-Authenticate: Basic realm="proxy"\r\nContent-Length: 0\r\n\r\n'
                    )
                host, _, port = request_line[1].rpartition(":")
                upstream = socket.create_connection((host, int(port)))
                self.request.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                sockets = [self.request, upstream]
                while True:
                    ready, _, _ = select.select(sockets, [], [], 30)
                    if not ready:
                        break
                    done = False
                    for source in ready:
                        data = source.recv(65536)
                        if not data:
                            done = True
                            break
                        (upstream if source is self.request else self.request).sendall(data)
                    if done:
                        break
                upstream.close()

        server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        self._server = server
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc: object) -> None:
        assert self._server is not None
        self._server.shutdown()
        self._server.server_close()

    def url(self, userinfo: str | None = None) -> str:
        """Return the proxy's URL.

        Parameters
        ----------
        userinfo
            Optional ``user:password`` (already percent-encoded) to embed.

        Returns
        -------
        str
            ``http://[userinfo@]127.0.0.1:<port>``.
        """
        assert self._server is not None
        prefix = f"{userinfo}@" if userinfo else ""
        return f"http://{prefix}127.0.0.1:{self._server.server_address[1]}"
