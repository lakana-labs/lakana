"""A loopback port that stays unserved for the whole test.

A port that was free a moment ago can be taken by a parallel test (`pytest -n`) before the
code under test connects to it. `closed_port` therefore keeps a socket bound to the port
and not listening: connects are refused (nothing listens), and no other socket can bind it
(the port is in use) until `release_closed_ports` runs. `packages/conftest.py` calls that after
every test, in all four packages.

On Linux a connect to such a socket is refused at once. On macOS (and other BSDs) the CI runs
showed connects to a bound, non-listening socket time out or reset instead of being refused, and
no documented BSD behaviour lets us rely on a refusal there. On every platform except Linux
`closed_port` therefore falls back to bind-and-close: connects are refused, but the number is free
again, so a parallel test (`pytest -n`) could in theory take it. That race is accepted there.

Standard library only, so every package's tests can import it.
"""

from __future__ import annotations

import socket
import sys

_HELD: list[socket.socket] = []


def closed_port() -> int:
    """Find a loopback port nothing listens on and nobody else can bind.

    On Linux the port stays reserved until `release_closed_ports` runs (the autouse fixture in
    `packages/conftest.py` does that after the test). On other platforms the socket is closed at
    once (connects are refused there only that way), so the port is not reserved.

    Returns
    -------
    int
        The port.
    """
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    if sys.platform == "linux":
        _HELD.append(sock)
    else:
        sock.close()
    return port


def release_closed_ports() -> None:
    """Free every port `closed_port` reserved."""
    while _HELD:
        _HELD.pop().close()
