"""The loopback listener of `lakana login` (`lakana.cli._loopback`)."""

from __future__ import annotations

import http.client
import socket
import threading

import pytest

from lakana._pkce import s256
from lakana.cli._loopback import DONE_PAGE, LoopbackAnswer, LoopbackServer

STATE = "s" * 43


def _get(port: int, path: str, *, host: str | None = None) -> tuple[int, bytes, dict[str, str]]:
    """Send one GET to the listener.

    Parameters
    ----------
    port
        The listener's port.
    path
        The path and query.
    host
        The `Host` header; ``127.0.0.1:<port>`` by default.

    Returns
    -------
    tuple of (int, bytes, dict)
        The status, the body and the headers.
    """
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    connection.putrequest("GET", path, skip_host=True)
    connection.putheader("Host", host or f"127.0.0.1:{port}")
    connection.endheaders()
    response = connection.getresponse()
    answer = (response.status, response.read(), dict(response.getheaders()))
    connection.close()
    return answer


def _serve(
    server: LoopbackServer, *requests: tuple[str, str | None]
) -> tuple[LoopbackAnswer | None, list]:
    """Send requests from a thread while the listener waits.

    Parameters
    ----------
    server
        The listener.
    *requests
        Each request's path and `Host` (`None` for the right one).

    Returns
    -------
    tuple of (LoopbackAnswer or None, list)
        What the wait returned, and each request's status, body and headers.
    """
    answers: list = []

    def send() -> None:
        for path, host in requests:
            answers.append(_get(server.port, path, host=host))

    thread = threading.Thread(target=send)
    thread.start()
    answer = server.wait(5.0)
    thread.join()
    return answer, answers


def test_the_right_state_brings_the_code_and_a_page() -> None:
    with LoopbackServer(STATE) as server:
        answer, answers = _serve(server, (f"/?code=c0de&state={STATE}", None))

    assert answer == LoopbackAnswer(code="c0de", error=None)
    status, body, headers = answers[0]
    assert (status, body) == (200, DONE_PAGE)
    assert headers["Cache-Control"] == "no-store"
    assert headers["Referrer-Policy"] == "no-referrer"
    assert "c0de" not in repr(answer)


def test_cancel_brings_the_error() -> None:
    with LoopbackServer(STATE) as server:
        answer, _ = _serve(server, (f"/?error=access_denied&state={STATE}", None))

    assert answer == LoopbackAnswer(code=None, error="access_denied")


def test_wrong_state_is_refused_and_the_wait_goes_on() -> None:
    with LoopbackServer(STATE) as server:
        answer, answers = _serve(
            server,
            ("/?code=evil&state=other", None),
            ("/?code=evil", None),
            (f"/?code=evil&state={STATE}&state={STATE}", None),
            (f"/?code=good&state={STATE}", None),
        )

    assert [status for status, _, _ in answers] == [400, 400, 400, 200]
    assert answer is not None
    assert answer.code == "good"


def test_other_paths_and_hosts_are_refused() -> None:
    with LoopbackServer(STATE) as server:
        answer, answers = _serve(
            server,
            (f"/favicon.ico?state={STATE}", None),
            (f"/?code=c&state={STATE}", "evil.example:80"),
            (f"/?code=c&state={STATE}", None),
        )

    assert [status for status, _, _ in answers] == [404, 400, 200]
    assert answer is not None


def test_the_wait_ends_without_an_answer() -> None:
    with LoopbackServer(STATE) as server:
        assert server.wait(0.05) is None


def test_loopback_binds_only_127_0_0_1() -> None:
    with LoopbackServer(STATE) as server:
        assert 1024 <= server.port <= 65535
        own = server._listener.socket.getsockname()
        assert own[0] == "127.0.0.1"
        with pytest.raises(OSError), socket.create_connection(("::1", server.port), timeout=1):
            pass


def test_the_listener_writes_no_log_line(capsys: pytest.CaptureFixture[str]) -> None:
    with LoopbackServer(STATE) as server:
        _serve(server, (f"/?code=c0de&state={STATE}", None))

    captured = capsys.readouterr()
    assert "c0de" not in captured.out + captured.err


def test_s256_is_rfc_7636s_example() -> None:
    assert s256("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk") == (
        "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    )
