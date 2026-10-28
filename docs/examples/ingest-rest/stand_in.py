"""A small HTTPS API in the style of OData, which the REST recipes read.

It serves `GET /api/items` from the file `items.json` in DIRECTORY, two items a page, each page's
list under `value` and the next page's link under `@odata.nextLink`. `updated_since` (ISO 8601)
keeps only the items whose `updated_at` is later. Every request must carry the bearer token in
`token.txt`; the base URL `/api` answers an authorised `GET` with an empty object, as the
connection test asks for it. TLS 1.2 or newer, with the certificate make_certs.py wrote. The port
is chosen by the system and written to `port` once the server listens.

Usage: python3 stand_in.py DIRECTORY
"""

import datetime as dt
import hmac
import json
import ssl
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

directory = Path(sys.argv[1])
token = (directory / "token.txt").read_text().strip()
PAGE = 2


class Handler(BaseHTTPRequestHandler):
    """Answer the API's two paths."""

    def do_GET(self) -> None:
        """Answer one request."""
        sent = self.headers.get("Authorization", "")
        if not hmac.compare_digest(sent.encode(), f"Bearer {token}".encode()):
            self._answer(401, {"error": "a bearer token is required"})
            return
        url = urlsplit(self.path)
        query = {name: values[-1] for name, values in parse_qs(url.query).items()}
        if url.path in ("/api", "/api/"):
            self._answer(200, {})
        elif url.path == "/api/items":
            self._answer(200, self._page(query))
        else:
            self._answer(404, {"error": f"no such path: {url.path}"})

    def _page(self, query: dict[str, str]) -> dict[str, object]:
        """Build one page of the items changed since `updated_since`.

        Parameters
        ----------
        query
            The request's query parameters.

        Returns
        -------
        dict
            The page.
        """
        items = json.loads((directory / "items.json").read_text())
        if "updated_since" in query:
            since = dt.datetime.fromisoformat(query["updated_since"])
            items = [i for i in items if dt.datetime.fromisoformat(i["updated_at"]) > since]
        items.sort(key=lambda item: item["id"])
        skip = int(query.get("skip", "0"))
        page: dict[str, object] = {"value": items[skip : skip + PAGE]}
        if skip + PAGE < len(items):
            page["@odata.nextLink"] = "items?" + urlencode({**query, "skip": skip + PAGE})
        return page

    def _answer(self, status: int, body: object) -> None:
        """Send a JSON answer.

        Parameters
        ----------
        status
            The HTTP status.
        body
            What to send as JSON.
        """
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: object) -> None:
        """Log nothing: the example's output is what the page shows.

        Parameters
        ----------
        format
            The message's format.
        *args
            Its values.
        """


class Server(ThreadingHTTPServer):
    """The server, quiet about a client that hangs up early (the connection test probes TLS)."""

    def handle_error(self, request: object, client_address: object) -> None:
        """Report an error, unless the client closed the connection.

        Parameters
        ----------
        request
            The client's socket.
        client_address
            Its address.
        """
        if not isinstance(sys.exc_info()[1], OSError):
            super().handle_error(request, client_address)  # type: ignore[arg-type]


context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
context.minimum_version = ssl.TLSVersion.TLSv1_2
context.load_cert_chain(directory / "server.pem", directory / "server-key.pem")
server = Server(("127.0.0.1", 0), Handler)
server.socket = context.wrap_socket(server.socket, server_side=True)
(directory / "port").write_text(f"{server.server_address[1]}\n")
server.serve_forever()
