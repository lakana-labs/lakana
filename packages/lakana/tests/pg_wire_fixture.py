"""A fake PostgreSQL server that speaks the login part of the wire protocol, and refuses it.

It serves every connection on its own thread: an ``SSLRequest`` or ``GSSENCRequest`` is answered
``N`` (or ``S`` and TLS, with a server context), then the startup message is read and the login
runs as `Login` says. A password is checked as PostgreSQL checks it (cleartext, MD5, or
SCRAM-SHA-256 with the client's proof verified), so a client whose answer is wrong is refused
with 28P01 even where the test expects another SQLSTATE. Nothing after the login is spoken: the
server closes the connection once it has answered.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import os
import socket
import ssl
import struct
import threading
from collections.abc import Generator
from dataclasses import dataclass, field

_SSL_REQUEST = 80877103
_GSSENC_REQUEST = 80877104
_ROUNDS = 4096


@dataclass
class Login:
    """How the fake server answers a login.

    Attributes
    ----------
    auth
        ``now`` refuses before asking for a password; ``cleartext``, ``md5`` and ``scram`` ask
        for one that way.
    password
        The password the server accepts; any other is refused with 28P01.
    sqlstate
        The SQLSTATE of the refusal: of the login itself for ``now``, else of what follows a
        correct password (``None`` accepts the login).
    message
        The refusal's text, in the server's language.
    tls
        A server context: the ``SSLRequest`` is answered ``S`` and the login runs over TLS.
    seen
        Per connection that sent a startup message, its user.
    """

    auth: str
    password: str = ""
    sqlstate: str | None = "28P01"
    message: str = "Passwort-Authentifizierung für Benutzer »reader« fehlgeschlagen"
    tls: ssl.SSLContext | None = None
    seen: list[str] = field(default_factory=list)


def _read(sock: socket.socket, count: int) -> bytes:
    """Read exactly `count` bytes.

    Parameters
    ----------
    sock
        The socket.
    count
        How many.

    Returns
    -------
    bytes
        The bytes.

    Raises
    ------
    ConnectionError
        When the client closes first.
    """
    data = b""
    while len(data) < count:
        chunk = sock.recv(count - len(data))
        if not chunk:
            raise ConnectionError("the client closed the connection")
        data += chunk
    return data


def _message(kind: bytes, body: bytes) -> bytes:
    """Frame one backend message.

    Parameters
    ----------
    kind
        Its type byte.
    body
        Its body.

    Returns
    -------
    bytes
        The message.
    """
    return kind + struct.pack("!i", len(body) + 4) + body


def _auth(code: int, data: bytes = b"") -> bytes:
    """Frame an ``Authentication*`` message.

    Parameters
    ----------
    code
        The request's code.
    data
        What follows it.

    Returns
    -------
    bytes
        The message.
    """
    return _message(b"R", struct.pack("!i", code) + data)


def error_response(sqlstate: str, message: str) -> bytes:
    """Frame an ``ErrorResponse`` as PostgreSQL sends it.

    Parameters
    ----------
    sqlstate
        The ``C`` field.
    message
        The ``M`` field, in any language.

    Returns
    -------
    bytes
        The message.
    """
    fields = [b"SFATAL", b"VFATAL", b"C" + sqlstate.encode(), b"M" + message.encode()]
    return _message(b"E", b"\0".join(fields) + b"\0\0")


def _answer(sock: socket.socket) -> bytes:
    """Read the client's next ``p`` message.

    Parameters
    ----------
    sock
        The socket.

    Returns
    -------
    bytes
        Its body.
    """
    kind, length = struct.unpack("!ci", _read(sock, 5))
    assert kind == b"p"
    return _read(sock, length - 4)


def _scram(sock: socket.socket, password: str) -> bool:
    """Run SCRAM-SHA-256 as the server, verifying the client's proof.

    Parameters
    ----------
    sock
        The socket, after ``AuthenticationSASL``.
    password
        The password the server accepts.

    Returns
    -------
    bool
        Whether the proof is right.
    """
    initial = _answer(sock)
    mechanism, _, rest = initial.partition(b"\0")
    assert mechanism == b"SCRAM-SHA-256"
    client_first = rest[4:].decode()
    bare = client_first.split(",", 2)[2]
    nonce = dict(item.split("=", 1) for item in bare.split(","))["r"]
    salt = os.urandom(16)
    server_first = f"r={nonce}srv,s={base64.b64encode(salt).decode()},i={_ROUNDS}"
    sock.sendall(_auth(11, server_first.encode()))
    final = _answer(sock).decode()
    without_proof, _, proof = final.rpartition(",p=")
    salted = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _ROUNDS)
    client_key = hmac.new(salted, b"Client Key", "sha256").digest()
    message = f"{bare},{server_first},{without_proof}".encode()
    signature = hmac.new(hashlib.sha256(client_key).digest(), message, "sha256").digest()
    expected = bytes(a ^ b for a, b in zip(client_key, signature, strict=True))
    if not hmac.compare_digest(base64.b64decode(proof), expected):
        return False
    server_key = hmac.new(salted, b"Server Key", "sha256").digest()
    verifier = base64.b64encode(hmac.new(server_key, message, "sha256").digest())
    sock.sendall(_auth(12, b"v=" + verifier))
    return True


def _password_right(sock: socket.socket, login: Login, user: str) -> bool:
    """Ask for the password the way `login` says and check it.

    Parameters
    ----------
    sock
        The socket.
    login
        The script.
    user
        The startup message's user, which MD5 salts with.

    Returns
    -------
    bool
        Whether it is right.
    """
    if login.auth == "cleartext":
        sock.sendall(_auth(3))
        return _answer(sock) == login.password.encode() + b"\0"
    if login.auth == "md5":
        salt = os.urandom(4)
        sock.sendall(_auth(5, salt))
        inner = hashlib.md5((login.password + user).encode(), usedforsecurity=False).hexdigest()
        outer = hashlib.md5(inner.encode() + salt, usedforsecurity=False).hexdigest()
        return _answer(sock) == f"md5{outer}".encode() + b"\0"
    sock.sendall(_auth(10, b"SCRAM-SHA-256\0\0"))
    return _scram(sock, login.password)


def _serve(sock: socket.socket, login: Login) -> None:
    """Run one connection's login.

    Parameters
    ----------
    sock
        The accepted socket; closed here.
    login
        The script.
    """
    with contextlib.ExitStack() as stack:
        stack.enter_context(sock)
        sock.settimeout(5)
        length, code = struct.unpack("!ii", _read(sock, 8))
        while code in (_SSL_REQUEST, _GSSENC_REQUEST):
            if code == _SSL_REQUEST and login.tls is not None:
                sock.sendall(b"S")
                sock = stack.enter_context(login.tls.wrap_socket(sock, server_side=True))
            else:
                sock.sendall(b"N")
            length, code = struct.unpack("!ii", _read(sock, 8))
        items = _read(sock, length - 8).split(b"\0")
        user = dict(zip(items[::2], items[1::2], strict=False)).get(b"user", b"").decode()
        login.seen.append(user)
        if login.auth == "now":
            sock.sendall(error_response(str(login.sqlstate), login.message))
            return
        if not _password_right(sock, login, user):
            sock.sendall(error_response("28P01", login.message))
            return
        sock.sendall(_auth(0))
        if login.sqlstate is not None:
            sock.sendall(error_response(login.sqlstate, login.message))


@contextlib.contextmanager
def pg_login_server(login: Login) -> Generator[int]:
    """Serve `login` on a loopback port until the block ends.

    Parameters
    ----------
    login
        The script.

    Yields
    ------
    int
        The port.
    """
    server = socket.create_server(("127.0.0.1", 0))

    def run(sock: socket.socket) -> None:
        with contextlib.suppress(OSError, ssl.SSLError, AssertionError, ValueError, struct.error):
            _serve(sock, login)

    def accept() -> None:
        while True:
            try:
                sock, _ = server.accept()
            except OSError:
                return
            threading.Thread(target=run, args=(sock,), daemon=True).start()

    threading.Thread(target=accept, daemon=True).start()
    try:
        yield server.getsockname()[1]
    finally:
        server.close()
