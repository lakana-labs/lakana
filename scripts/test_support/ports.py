"""A loopback port that stays unserved for the whole test.

A port that was free a moment ago can be taken by a parallel test (`pytest -n`) before the
code under test connects to it. `closed_port` therefore keeps a socket bound to the port
and not listening: connects are refused (nothing listens), and no other socket can bind it
(the port is in use) until `release_closed_ports` runs. The packages' `conftest.py` call that
after every test.

Standard library only, so every package's tests can import it.
"""

from __future__ import annotations

import socket

_HELD: list[socket.socket] = []


def closed_port() -> int:
    """Find a loopback port nothing listens on and nobody else can bind.

    The port stays reserved until `release_closed_ports` runs (the autouse fixture in each
    `conftest.py` does that after the test).

    Returns
    -------
    int
        The port.
    """
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    _HELD.append(sock)
    return int(sock.getsockname()[1])


def release_closed_ports() -> None:
    """Free every port `closed_port` reserved."""
    while _HELD:
        _HELD.pop().close()
