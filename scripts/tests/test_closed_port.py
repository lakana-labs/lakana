"""`closed_port` hands out a port that stays unserved, even when another test races for it."""

from __future__ import annotations

import socket
import sys
from collections.abc import Iterator

import pytest

from scripts.test_support.ports import closed_port, release_closed_ports


def _free_port_the_old_way() -> int:
    """Return a port that was free a moment ago (the helper this module replaced).

    Returns
    -------
    int
        The port.
    """
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _another_test_takes(port: int) -> socket.socket:
    """Bind and listen on a port, as a parallel test's server would (with `SO_REUSEADDR`).

    Parameters
    ----------
    port : int
        The port to take.

    Returns
    -------
    socket.socket
        The listening socket.
    """
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", port))
    server.listen(1)
    return server


@pytest.fixture(autouse=True)
def _release() -> Iterator[None]:
    """Release the reserved ports after each test.

    Yields
    ------
    None
        Control to the test.
    """
    yield
    release_closed_ports()


def test_the_old_helper_lets_another_test_take_the_port() -> None:
    port = _free_port_the_old_way()
    with _another_test_takes(port), socket.create_connection(("127.0.0.1", port), timeout=2):
        pass


@pytest.mark.skipif(
    sys.platform != "linux",
    reason="only Linux keeps the port bound; elsewhere it is bind-and-close",
)
def test_the_port_cannot_be_taken_by_another_test() -> None:
    port = closed_port()
    with pytest.raises(OSError):
        _another_test_takes(port)


def test_a_connect_to_the_port_is_refused() -> None:
    port = closed_port()
    with pytest.raises(ConnectionRefusedError):
        socket.create_connection(("127.0.0.1", port), timeout=2)


def test_two_ports_differ_and_release_frees_them() -> None:
    first, second = closed_port(), closed_port()
    assert first != second
    release_closed_ports()
    _another_test_takes(first).close()
