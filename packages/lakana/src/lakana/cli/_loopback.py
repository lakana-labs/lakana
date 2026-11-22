"""The loopback listener of `lakana login`: where the browser brings the one-time code back.

`LoopbackServer` listens on ``127.0.0.1`` only, on a port the system picks, and answers GET
requests of ``/`` alone (404 for any other path, 400 for another `Host`). A request whose
``state`` is not this sign-in's (compared in constant time) is answered 400 and ignored: a
program that does not know the ``state`` can neither end the wait nor answer it. The first
request with the right ``state`` is answered with a page saying the tab may be closed, and ends
the wait. The listener writes no log line: a request line holds the code. A code is worth
nothing without the PKCE verifier only this process holds.

The ``state`` is no secret from other users of the same computer: it is on the terminal and on
the command line of the browser launcher (``/proc/<pid>/cmdline``). One who reads it can approve
the same challenge in their own browser session and race the browser to the port with their own
code; the command line then saves a token of theirs, and says whose (``Logged in as ...``).
"""

from __future__ import annotations

import hmac
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import TracebackType
from urllib.parse import parse_qs, urlsplit

DONE_PAGE = (
    b"<!doctype html><html lang=en><meta charset=utf-8><title>Lakana</title>"
    b"<p>You can close this tab and go back to the terminal.</p></html>"
)
"""The page the browser shows once the command line has its answer."""


@dataclass(frozen=True)
class LoopbackAnswer:
    """What the browser brought back.

    Parameters
    ----------
    code
        The one-time code; `None` when the sign-in was cancelled. `repr=False`.
    error
        ``access_denied`` when the person pressed Cancel; `None` otherwise.
    """

    code: str | None = field(repr=False)
    error: str | None


def _one(query: dict[str, list[str]], key: str) -> str | None:
    """Read a query parameter that must appear once.

    Parameters
    ----------
    query
        The parsed query.
    key
        The parameter.

    Returns
    -------
    str or None
        Its value, or `None` when it is missing or repeated.
    """
    values = query.get(key, [])
    return values[0] if len(values) == 1 else None


class _Handler(BaseHTTPRequestHandler):
    """Answer the browser's one request; its server is a `_Listener`."""

    timeout = 5.0
    """Seconds a connection may take to send its request, so a silent one cannot hold the wait."""

    def log_message(self, format: str, *args: object) -> None:
        """Write nothing: the request line holds the one-time code.

        Parameters
        ----------
        format
            The message's format.
        *args
            Its values.
        """

    def _answer(self, status: int, body: bytes) -> None:
        """Send a small page that keeps no copy and sends no referrer.

        Parameters
        ----------
        status
            The status.
        body
            The page.
        """
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        """Take the code or the refusal of the right sign-in; refuse everything else."""
        listener = self.server
        assert isinstance(listener, _Listener)  # nosec B101 -- only a _Listener serves it
        parts = urlsplit(self.path)
        if parts.path != "/":
            self._answer(404, b"Not found.")
            return
        if self.headers.get("Host") != f"127.0.0.1:{listener.port}":
            self._answer(400, b"Wrong address.")
            return
        query = parse_qs(parts.query)
        state = _one(query, "state") or ""
        if not hmac.compare_digest(state.encode(), listener.state.encode()):
            self._answer(400, b"This answer is not for this sign-in.")
            return
        listener.answer = LoopbackAnswer(code=_one(query, "code"), error=_one(query, "error"))
        self._answer(200, DONE_PAGE)


class _Listener(HTTPServer):
    """An `HTTPServer` that remembers the sign-in's ``state`` and the answer."""

    def __init__(self, state: str) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.state = state
        self.port: int = self.server_address[1]
        self.answer: LoopbackAnswer | None = None


class LoopbackServer:
    """Listen on ``127.0.0.1`` for the browser's answer to one sign-in.

    Parameters
    ----------
    state
        The sign-in's random ``state``; only an answer that carries it counts.
    """

    def __init__(self, state: str) -> None:
        self._listener = _Listener(state)

    @property
    def port(self) -> int:
        """The port the system picked.

        Returns
        -------
        int
            The port, on ``127.0.0.1``.
        """
        return self._listener.port

    def __enter__(self) -> LoopbackServer:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._listener.server_close()

    def wait(self, timeout: float) -> LoopbackAnswer | None:
        """Answer requests until the right one came, or the time is up.

        Parameters
        ----------
        timeout
            Seconds to wait in all.

        Returns
        -------
        LoopbackAnswer or None
            The answer; `None` when none came in time.
        """
        deadline = time.monotonic() + timeout
        while self._listener.answer is None:
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            self._listener.timeout = left
            self._listener.handle_request()
        return self._listener.answer
