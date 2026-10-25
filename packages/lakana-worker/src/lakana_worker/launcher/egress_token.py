"""The egress proxy's token: what one client may reach, from where, and until when.

The launcher signs a token with a key it shares with the proxy through a volume; the proxy checks
the signature and the time and keeps no state. A token grants a *purpose* (``build`` today; an
ingestion task's connections later), the *network* its client connects from (the /29 the
launcher gave that build), exact *host:port pairs*, and an *expiry*. The client's code sees its
token (it is in the proxy URL), so a token is worth only what the launcher put in it: copied to
another container it is refused, because that container connects from another network.

Format: ``v1.<base64url of compact JSON>.<HMAC-SHA256 hex of "v1.<payload>">``, characters a URL's
userinfo carries unquoted. Standard library only, like the launcher.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

_VERSION = "v1"
MAX_TARGETS = 64
"""The most host:port pairs one token grants."""
MAX_PAYLOAD = 8192
"""The longest payload `verify` accepts; `mint` refuses a longer one. The proxy's 16 KiB head
limit holds a token of this size in base64 with room to spare."""
TOKEN_TOO_LONG = "the token would be too long for the proxy"  # nosec B105 -- a message, not a secret
"""How `mint`'s refusal of a payload over `MAX_PAYLOAD` begins; the worker recognises it."""
_TOKEN = re.compile(rf"\Av1\.[A-Za-z0-9_-]{{1,{MAX_PAYLOAD}}}\.[0-9a-f]{{64}}\Z")
_PURPOSE = re.compile(r"\A[a-z][a-z0-9-]{0,31}\Z")
_HOST = re.compile(
    r"\A(?=.{1,253}\Z)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*\Z"
)
"""A lower-case DNS name or IPv4 literal, without a trailing dot: one spelling per host."""
_KEY_BYTES = 32

Network = ipaddress.IPv4Network | ipaddress.IPv6Network
Target = tuple[str, int]


@dataclass(frozen=True)
class Grant:
    """What a valid token allows.

    Parameters
    ----------
    purpose
        What the client is, such as ``build``; it chooses the hint in a refusal, nothing else.
    network
        The network the client must connect from.
    targets
        The exact host and port pairs it may reach.
    expires
        Epoch seconds; the token and every tunnel it opened end then.
    """

    purpose: str
    network: Network
    targets: frozenset[Target]
    expires: int


def valid_host(host: str) -> bool:
    """Say whether a host has the one spelling a token and a request may use.

    Parameters
    ----------
    host
        A host name or IPv4 literal.

    Returns
    -------
    bool
        ``True`` for a lower-case name of letters, digits, hyphens and dots, no trailing dot.
    """
    return _HOST.match(host) is not None


def _sign(key: bytes, body: str) -> str:
    """Sign a token's body.

    Parameters
    ----------
    key
        The shared key.
    body
        ``v1.<payload>``.

    Returns
    -------
    str
        The HMAC-SHA256, hex.
    """
    return hmac.new(key, body.encode("ascii"), hashlib.sha256).hexdigest()


def mint(
    key: bytes, targets: Sequence[Target], expires: int, *, purpose: str, network: Network
) -> str:
    """Make a token.

    Parameters
    ----------
    key
        The shared key.
    targets
        Host and port pairs; each host lower case, as `valid_host` says.
    expires
        Epoch seconds.
    purpose
        A lower-case word, such as ``build``.
    network
        The network the client connects from.

    Returns
    -------
    str
        The token, safe unquoted in a URL's userinfo.

    Raises
    ------
    ValueError
        For a field the proxy could misread; the message names it.
    """
    if _PURPOSE.match(purpose) is None:
        raise ValueError(f"purpose {purpose!r} must be a lower-case word")
    if isinstance(expires, bool) or not isinstance(expires, int) or expires < 0:
        raise ValueError("expires must be epoch seconds")
    if len(targets) > MAX_TARGETS:
        raise ValueError(f"a token grants at most {MAX_TARGETS} host:port pairs")
    for host, port in targets:
        if not valid_host(host) or isinstance(port, bool) or not 0 < port < 65536:
            raise ValueError(f"{host}:{port} is not a lower-case host and a port from 1 to 65535")
    data = {
        "e": expires,
        "n": str(network),
        "p": purpose,
        "t": sorted({f"{host}:{port}" for host, port in targets}),
    }
    text = json.dumps(data, separators=(",", ":"), sort_keys=True)
    payload = base64.urlsafe_b64encode(text.encode("ascii")).decode("ascii").rstrip("=")
    if len(payload) > MAX_PAYLOAD:
        raise ValueError(
            f"{TOKEN_TOO_LONG} ({len(payload)} characters of payload, "
            f"at most {MAX_PAYLOAD}); grant fewer or shorter host names"
        )
    body = f"{_VERSION}.{payload}"
    return f"{body}.{_sign(key, body)}"


def _target(text: object) -> Target | None:
    """Read one ``host:port`` of a signed payload.

    Parameters
    ----------
    text
        The JSON value.

    Returns
    -------
    tuple or None
        The pair; `None` if it is not in the form `mint` writes.
    """
    if not isinstance(text, str):
        return None
    host, _, port = text.rpartition(":")
    if not valid_host(host) or not port.isascii() or not port.isdigit() or port.startswith("0"):
        return None
    number = int(port)
    return (host, number) if number < 65536 else None


def verify(key: bytes, token: str, now: float) -> Grant | None:
    """Check a token and read its grant.

    Parameters
    ----------
    key
        The shared key.
    token
        The token from the proxy request.
    now
        Epoch seconds.

    Returns
    -------
    Grant or None
        The grant; `None` for a token that is malformed, signed with another key, changed or
        expired. Nothing malformed raises.
    """
    if _TOKEN.match(token) is None:
        return None
    body, _, signature = token.rpartition(".")
    if not hmac.compare_digest(_sign(key, body), signature):
        return None
    payload = body.removeprefix(f"{_VERSION}.")
    try:
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (binascii.Error, ValueError):
        return None
    if not isinstance(data, dict) or set(data) != {"e", "n", "p", "t"}:
        return None
    expires, network, purpose, targets = data["e"], data["n"], data["p"], data["t"]
    if type(expires) is not int or expires < now:
        return None
    if not isinstance(purpose, str) or _PURPOSE.match(purpose) is None:
        return None
    if not isinstance(targets, list) or len(targets) > MAX_TARGETS:
        return None
    pairs = [_target(item) for item in targets]
    if not isinstance(network, str) or any(pair is None for pair in pairs):
        return None
    try:
        parsed = ipaddress.ip_network(network)
    except ValueError:
        return None
    return Grant(
        purpose=purpose,
        network=parsed,
        targets=frozenset(pair for pair in pairs if pair is not None),
        expires=expires,
    )


def read_key(path: Path) -> bytes | None:
    """Read the shared key.

    Parameters
    ----------
    path
        The key file: 64 hex digits. A symbolic link there is not followed.

    Returns
    -------
    bytes or None
        The 32-byte key; `None` when the file is missing, a link, not a regular file, or
        malformed.
    """
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return None
        text = os.read(descriptor, 2 * _KEY_BYTES + 2)
    except OSError:
        return None
    finally:
        os.close(descriptor)
    try:
        key = bytes.fromhex(text.decode("ascii").strip())
    except (UnicodeDecodeError, ValueError):
        return None
    return key if len(key) == _KEY_BYTES else None


def _restrict(path: Path) -> None:
    """Set a key file's mode back to 0640, without following a link.

    Parameters
    ----------
    path
        The key file `read_key` just accepted.
    """
    with contextlib.suppress(OSError):
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        try:
            if stat.S_IMODE(os.fstat(descriptor).st_mode) != 0o640:
                os.fchmod(descriptor, 0o640)
        finally:
            os.close(descriptor)


def ensure_key(path: Path) -> bytes:
    """Make the shared key once, readable by the owner and the group only.

    A valid key is kept, its mode set back to 0640 if it changed. A missing one is written to a
    temporary file and linked into place, so the file never exists half written; a broken one
    (truncated, or something planted at the path) is replaced. Replacing invalidates the tokens
    of running builds, which fail with a proxy authentication error and run again.

    Parameters
    ----------
    path
        The key file, in the volume the launcher shares with the proxy, which only reads it.

    Returns
    -------
    bytes
        The key.
    """
    existing = read_key(path)
    if existing is not None:
        _restrict(path)
        return existing
    return _write_key(path, replace=os.path.lexists(path))


def rotate_key(path: Path) -> bytes:
    """Make a new shared key, replacing the old one in one step.

    The launcher calls it at every start: no token from before the restart stays valid, so the
    subnets an earlier launcher kept resting cannot be reached by a leaked token. The proxy reads
    the new file at its next connection (it compares the file's inode, time and size).

    Parameters
    ----------
    path
        The key file; a link planted there is replaced, never written through.

    Returns
    -------
    bytes
        The new key.
    """
    return _write_key(path, replace=True)


def _write_key(path: Path, *, replace: bool) -> bytes:
    """Write a new key to a temporary file, flushed, then move it into place.

    Parameters
    ----------
    path
        The key file.
    replace
        Replace what is at the path; otherwise link, and use the key of a launcher that linked
        first.

    Returns
    -------
    bytes
        The key now at the path.
    """
    key = secrets.token_bytes(_KEY_BYTES)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640)
    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as handle:
            handle.write(key.hex())
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o640)
        if replace:
            os.replace(temporary, path)
        else:
            try:
                os.link(temporary, path)
            except FileExistsError:
                # Another launcher on the same volume was first: use its key.
                raced = read_key(path)
                if raced is not None:
                    return raced
                os.replace(temporary, path)
    finally:
        with contextlib.suppress(OSError):
            temporary.unlink()
    return key
