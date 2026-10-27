"""`lakana.ingest.rest` and the retries of `lakana.ingest._http.send`, against local TLS servers."""

from __future__ import annotations

import datetime as dt
import itertools
import json
import secrets
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pyarrow as pa
import pytest
from sdk_tls_fixture import Answer, Authority, Exchange, make_authority, scripted

from lakana import context
from lakana.ingest import (
    CapExceeded,
    FileShapeError,
    SourceError,
    SourceRefused,
    UsageError,
    _connections,
    _http,
    rest,
)

TOKEN = secrets.token_urlsafe(16)
API_KEY = secrets.token_urlsafe(16)
CLIENT_SECRET = secrets.token_urlsafe(16)


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Remove the variables the client could read; leave no task state behind.

    Parameters
    ----------
    monkeypatch
        Pytest's patcher.

    Yields
    ------
    None
        While the test runs.
    """
    for name in ("LAKANA_EGRESS_PROXY", "LAKANA_CA_BUNDLE", "HTTPS_PROXY", "https_proxy"):
        monkeypatch.delenv(name, raising=False)
    yield
    context._set(None)
    _connections.clear()


@pytest.fixture
def authority(tmp_path: Path) -> Authority:
    """Make the authority that signs the test servers.

    Parameters
    ----------
    tmp_path
        Pytest's directory.

    Returns
    -------
    Authority
        The authority.
    """
    return make_authority(tmp_path)


@dataclass
class Clock:
    """A fake clock that advances by what the retries sleep.

    Attributes
    ----------
    slept
        Each wait, in order.
    """

    slept: list[float] = field(default_factory=list)

    def sleep(self, seconds: float) -> None:
        """Record a wait.

        Parameters
        ----------
        seconds
            How long.
        """
        self.slept.append(seconds)

    def now(self) -> float:
        """Read the time: the sum of the waits.

        Returns
        -------
        float
            Seconds.
        """
        return sum(self.slept)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    """Replace the retries' sleep, clock and jitter.

    Parameters
    ----------
    monkeypatch
        Pytest's patcher.

    Returns
    -------
    Clock
        The fake.
    """
    fake = Clock()
    monkeypatch.setattr(_http, "_sleep", fake.sleep)
    monkeypatch.setattr(_http, "_clock", fake.now)
    monkeypatch.setattr(_http, "_rand", lambda: 0.0)
    monkeypatch.setattr(_http, "_wall", lambda: dt.datetime(2026, 10, 22, 8, 0, 0, tzinfo=dt.UTC))
    return fake


@dataclass(frozen=True)
class _Resource:
    """A fake `client.task_connections` answering one body.

    Attributes
    ----------
    body
        The body.
    """

    body: dict[str, object]

    def get(self, name: str) -> dict[str, object]:
        """Answer the fetch.

        Parameters
        ----------
        name
            The connection's name.

        Returns
        -------
        dict
            The body.
        """
        return self.body


class _Client:
    """A fake SDK client whose `task_connections` answers one body.

    Parameters
    ----------
    body
        The body.
    """

    def __init__(self, body: dict[str, object]) -> None:
        self.task_connections = _Resource(body)


def _task(
    base: str,
    authority: Authority,
    settings: Mapping[str, object] | None = None,
    secret: Mapping[str, str] | None = None,
    kind: str = "http",
) -> context._TaskState:
    """Set the state of a task that declares the connection ``api``.

    Parameters
    ----------
    base
        Its base URL.
    authority
        Whose CA it trusts.
    settings
        Its settings besides ``ca_certificate`` (``auth: none`` by default).
    secret
        Its secret.
    kind
        Its kind.

    Returns
    -------
    context._TaskState
        The state.
    """
    body = {
        "name": "api",
        "kind": kind,
        "address": {"base_url": base},
        "settings": {
            **(settings or {"auth": "none"}),
            "ca_certificate": authority.ca_file.read_text(),
        },
        "endpoints": [],
        "secret": dict(secret) if secret is not None else None,
    }
    state = context._TaskState(
        client=_Client(body),  # ty: ignore[invalid-argument-type]
        environment="dev",
        asset="bronze.x",
        declared=frozenset({"api"}),
        connections={"api": {"kind": kind}},
    )
    context._set(state)
    return state


def _json(document: object, status: int = 200, **headers: str) -> Answer:
    """Answer with a JSON document.

    Parameters
    ----------
    document
        The document.
    status
        The status.
    **headers
        More headers (``_`` for ``-``).

    Returns
    -------
    Answer
        The answer.
    """
    extra = {k.replace("_", "-"): v for k, v in headers.items()}
    return status, {"Content-Type": "application/json", **extra}, json.dumps(document).encode()


def _query(exchange: Exchange) -> dict[str, str]:
    """Read a request's query.

    Parameters
    ----------
    exchange
        The request.

    Returns
    -------
    dict
        Name to its (last) value.
    """
    return {k: v[-1] for k, v in parse_qs(urlsplit(exchange.target).query).items()}


# --- the retries --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("attempt", "retry_after", "expected"),
    [
        (0, None, (0.25, 0.5)),
        (3, None, (2.0, 4.0)),
        (10, None, (30.0, 60.0)),
        (400, None, (30.0, 60.0)),
        (0, "7", (7.0, 7.0)),
        (0, "Thu, 22 Oct 2026 08:00:30 GMT", (30.0, 30.0)),
        (0, "Thu, 22 Oct 2026 07:00:00 GMT", (0.0, 0.0)),
        (0, "garbage", (0.25, 0.5)),
        (0, "-5", (0.25, 0.5)),
    ],
)
def test_retry_delay(attempt: int, retry_after: str | None, expected: tuple[float, float]) -> None:
    """Backoff 0.5 * 2**attempt capped at 60, jittered into its upper half; Retry-After wins."""
    wall = dt.datetime(2026, 10, 22, 8, 0, 0, tzinfo=dt.UTC)
    low = _http.retry_delay(attempt, retry_after, rand=lambda: 0.0, wall=wall)
    high = _http.retry_delay(attempt, retry_after, rand=lambda: 1.0, wall=wall)
    assert (low, high) == expected


@pytest.mark.parametrize(
    ("url", "shown"),
    [
        ("https://api.test/v1/items?page=2&key=abc", "https://api.test/v1/items?page=…&key=…"),
        ("https://u:p@api.test:8443/x#frag", "https://api.test:8443/x"),
        ("https://api.test/a\nb", "https://api.test/a\\x0ab"),
        ("https://[::1/x", "<a URL that cannot be read>"),
    ],
)
def test_shown_url_drops_credentials_and_query_values(url: str, shown: str) -> None:
    assert _http.shown_url(url) == shown


def _flaky(failures: int, status: int = 503, **headers: str) -> Callable[[Exchange], Answer]:
    """Fail the first requests, then answer one page.

    Parameters
    ----------
    failures
        How many requests fail.
    status
        Their status.
    **headers
        Their headers.

    Returns
    -------
    Callable
        The answer.
    """
    count = itertools.count()

    def answer(exchange: Exchange) -> Answer:
        if next(count) < failures:
            return _json({}, status, **headers)
        return _json({"data": [{"id": 1}]})

    return answer


def test_a_503_is_retried_with_backoff_until_it_passes(
    authority: Authority, tmp_path: Path, clock: Clock
) -> None:
    with scripted(authority, tmp_path, _flaky(2)) as server:
        _task(server.base, authority)
        table = rest.read("api", "items")

    assert table.column("id").to_pylist() == [1]
    assert len(server.seen) == 3
    assert clock.slept == [0.25, 0.5]


@pytest.mark.parametrize(
    ("retry_after", "slept"), [("2", 2.0), ("Thu, 22 Oct 2026 08:00:09 GMT", 9.0)]
)
def test_retry_after_is_honoured_in_seconds_or_as_a_date(
    authority: Authority, tmp_path: Path, clock: Clock, retry_after: str, slept: float
) -> None:
    with scripted(authority, tmp_path, _flaky(1, 429, Retry_After=retry_after)) as server:
        _task(server.base, authority)
        rest.read("api", "items")

    assert clock.slept == [slept]


def test_the_retries_end_naming_the_attempts_and_the_time(
    authority: Authority, tmp_path: Path, clock: Clock
) -> None:
    settings = {"auth": "api_key", "api_key_name": "key", "api_key_in": "query"}
    with scripted(authority, tmp_path, _flaky(100)) as server:
        _task(server.base, authority, settings, {"api_key": API_KEY})
        with pytest.raises(SourceError) as caught:
            rest.read("api", "items", params={"q": "x"}, retries=3)

    assert type(caught.value) is SourceError
    assert str(caught.value) == (
        f"`api`: GET {server.base}/items?q=…&key=… answered 503 after 4 attempts in 2 s; the API "
        "is unavailable — try again later, or raise retries= or retry_budget_seconds="
    )
    assert API_KEY not in str(caught.value)
    assert len(server.seen) == 4


def test_a_retry_after_past_the_budget_gives_up_at_once(
    authority: Authority, tmp_path: Path, clock: Clock
) -> None:
    with scripted(authority, tmp_path, _flaky(100, Retry_After="400")) as server:
        _task(server.base, authority)
        with pytest.raises(SourceError, match="answered 503 after 1 attempt in 0 s"):
            rest.read("api", "items")

    assert (len(server.seen), clock.slept) == (1, [])


def test_the_budget_counts_the_waits(authority: Authority, tmp_path: Path, clock: Clock) -> None:
    with scripted(authority, tmp_path, _flaky(100, Retry_After="100")) as server:
        _task(server.base, authority)
        with pytest.raises(SourceError, match="after 3 attempts in 200 s"):
            rest.read("api", "items", retries=50, retry_budget_seconds=250)

    assert clock.slept == [100.0, 100.0]


@pytest.mark.parametrize(
    ("status", "error", "text"),
    [
        (404, SourceRefused, "the API refused the request (HTTP 404 for GET"),
        (400, SourceRefused, "check path= and params="),
        (302, SourceRefused, "answers with a redirect (HTTP 302), which rest.read does not follow"),
        (501, SourceError, "the API answered HTTP 501 for GET"),
    ],
)
def test_a_status_that_cannot_pass_is_not_retried(
    authority: Authority,
    tmp_path: Path,
    clock: Clock,
    status: int,
    error: type[Exception],
    text: str,
) -> None:
    with scripted(authority, tmp_path, _flaky(100, status)) as server:
        _task(server.base, authority)
        with pytest.raises(error) as caught:
            rest.read("api", "items")

    assert type(caught.value) is error
    assert text in str(caught.value)
    assert (len(server.seen), clock.slept) == (1, [])


@pytest.mark.parametrize("status", [401, 403])
def test_a_refused_credential_names_the_test_command_never_the_token(
    authority: Authority, tmp_path: Path, clock: Clock, status: int
) -> None:
    with scripted(authority, tmp_path, _flaky(100, status)) as server:
        _task(server.base, authority, {"auth": "bearer"}, {"token": TOKEN})
        with pytest.raises(SourceRefused) as caught:
            rest.read("api", "items")

    assert f"the API refused the credential (HTTP {status} for GET" in str(caught.value)
    assert "`lakana connection test api --env dev`" in str(caught.value)
    assert TOKEN not in str(caught.value)
    assert SourceRefused.retryable is False


def test_a_network_failure_is_retried_then_named(authority: Authority, clock: Clock) -> None:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    _task(f"https://127.0.0.1:{port}", authority)

    with pytest.raises(SourceError, match=r"failed \(ConnectError\) after 3 attempts"):
        rest.read("api", "items", retries=2)
    assert len(clock.slept) == 2


def test_an_untrusted_certificate_is_refused_without_a_retry(
    tmp_path: Path, authority: Authority, clock: Clock
) -> None:
    (tmp_path / "other").mkdir()
    other = make_authority(tmp_path / "other", "Other CA")
    with scripted(other, tmp_path / "other", _flaky(0)) as server:
        _task(server.base, authority)
        with pytest.raises(SourceRefused, match=r"certificate is not trusted for `127\.0\.0\.1`"):
            rest.read("api", "items")

    assert clock.slept == []


def test_an_oauth2_token_refused_by_the_api_is_fetched_again_once(
    authority: Authority, tmp_path: Path, clock: Clock
) -> None:
    issued = itertools.count(1)
    current: list[int] = []

    def answer(exchange: Exchange) -> Answer:
        if exchange.method == "POST":
            current.append(next(issued))
            return _json({"access_token": f"access-{current[-1]}-0123456789"})
        good = exchange.headers.get("authorization") == "Bearer access-2-0123456789"
        page = int(_query(exchange).get("page", "1"))
        if page == 1 and current[-1] == 1:
            return _json({"data": [{"id": 1}]})
        if not good:
            return _json({}, 401)
        return _json({"data": [{"id": page}] if page < 3 else []})

    with scripted(authority, tmp_path, answer) as server:
        _task(
            f"{server.base}/v1",
            authority,
            {"auth": "oauth2", "client_id": "c", "token_url": f"{server.base}/token"},
            {"client_secret": CLIENT_SECRET},
        )
        table = rest.read("api", "items", paginate=rest.PageNumber())

    assert table.column("id").to_pylist() == [1, 2]
    assert [s.method for s in server.seen].count("POST") == 2


def test_an_oauth2_token_refused_twice_is_a_refused_credential(
    authority: Authority, tmp_path: Path, clock: Clock
) -> None:
    def answer(exchange: Exchange) -> Answer:
        if exchange.method == "POST":
            return _json({"access_token": "access-0123456789"})
        return _json({}, 401)

    with scripted(authority, tmp_path, answer) as server:
        _task(
            server.base,
            authority,
            {"auth": "oauth2", "client_id": "c", "token_url": f"{server.base}/token"},
            {"client_secret": CLIENT_SECRET},
        )
        with pytest.raises(SourceRefused, match="refused the credential"):
            rest.read("api", "items")

    assert [s.method for s in server.seen] == ["POST", "GET", "POST", "GET"]


# --- pagination ----------------------------------------------------------------------------------


def _numbered(pages: int, size: int = 2) -> Callable[[Exchange], Answer]:
    """Serve pages by number from 1, then empty pages.

    Parameters
    ----------
    pages
        How many pages hold records.
    size
        Records per page.

    Returns
    -------
    Callable
        The answer.
    """

    def answer(exchange: Exchange) -> Answer:
        page = int(_query(exchange).get("page", "1"))
        rows = [{"id": (page - 1) * size + n} for n in range(size)] if page <= pages else []
        return _json({"data": rows})

    return answer


def test_page_numbers_stop_at_the_first_empty_page(authority: Authority, tmp_path: Path) -> None:
    with scripted(authority, tmp_path, _numbered(3)) as server:
        _task(server.base, authority)
        table = rest.read(
            "api",
            "items",
            paginate=rest.PageNumber(size_param="per_page", size=2),
            params={"status": "open"},
        )

    assert table.column("id").to_pylist() == list(range(6))
    assert [_query(s) for s in server.seen] == [
        {"status": "open", "page": str(n), "per_page": "2"} for n in range(1, 5)
    ]


def test_a_short_page_does_not_end_the_read(authority: Authority, tmp_path: Path) -> None:
    """An API that caps the page size below what was asked would otherwise lose records."""
    with scripted(authority, tmp_path, _numbered(3, size=1)) as server:
        _task(server.base, authority)
        table = rest.read("api", "items", paginate=rest.PageNumber(size_param="limit", size=100))

    assert table.num_rows == 3


@pytest.mark.parametrize(
    "end", [{}, {"next": None}, {"next": ""}], ids=["missing", "null", "empty"]
)
def test_a_cursor_is_followed_until_the_answer_names_none(
    authority: Authority, tmp_path: Path, end: dict[str, object]
) -> None:
    def answer(exchange: Exchange) -> Answer:
        cursor = _query(exchange).get("after", "")
        page = {"": 1, "c2": 2, "c3": 3}[cursor]
        meta = {"next": f"c{page + 1}"} if page < 3 else end
        return _json({"items": [{"id": page}], "meta": meta})

    with scripted(authority, tmp_path, answer) as server:
        _task(server.base, authority)
        table = rest.read(
            "api",
            "items",
            paginate=rest.Cursor(param="after", cursor_field="meta.next"),
            records="items",
            params={"q": "x"},
        )

    assert table.column("id").to_pylist() == [1, 2, 3]
    assert [_query(s) for s in server.seen] == [
        {"q": "x"},
        {"q": "x", "after": "c2"},
        {"q": "x", "after": "c3"},
    ]


def test_odata_next_links_absolute_and_relative(authority: Authority, tmp_path: Path) -> None:
    def answer(exchange: Exchange) -> Answer:
        query = _query(exchange)
        if "$skiptoken" not in query:
            return _json(
                {"value": [{"id": 1}], "@odata.nextLink": f"{base[0]}/v1/items?$skiptoken=2"}
            )
        if query["$skiptoken"] == "2":
            return _json({"value": [{"id": 2}], "@odata.nextLink": "items?$skiptoken=3"})
        return _json({"value": [{"id": 3}]})

    base: list[str] = []
    with scripted(authority, tmp_path, answer) as server:
        base.append(server.base)
        _task(f"{server.base}/v1", authority)
        table = rest.read(
            "api", "items", paginate=rest.NextLink(), records="value", params={"$top": 1}
        )

    assert table.column("id").to_pylist() == [1, 2, 3]
    assert [s.target for s in server.seen] == [
        "/v1/items?%24top=1",
        "/v1/items?$skiptoken=2",
        "/v1/items?$skiptoken=3",
    ]


@pytest.mark.parametrize(
    "link",
    [
        "https://evil.test/items?page=2",
        "//evil.test/items",
        "https://127.0.0.1:1/items",
        "https://user:pw@127.0.0.1:{port}/items",
        "http://127.0.0.1:{port}/items",
    ],
    ids=["other-host", "network-path", "other-port", "userinfo", "plain-http"],
)
def test_a_next_link_off_the_connections_origin_is_not_followed(
    authority: Authority, tmp_path: Path, link: str
) -> None:
    def answer(exchange: Exchange) -> Answer:
        port = exchange.headers["host"].rsplit(":", 1)[1]
        return _json({"data": [{"id": 1}], "next": link.format(port=port)})

    with scripted(authority, tmp_path, answer) as server:
        _task(server.base, authority, {"auth": "bearer"}, {"token": TOKEN})
        with pytest.raises(SourceRefused) as caught:
            rest.read("api", "items", paginate=rest.NextLink(field="next"))

    assert "the next link of page 1 points to" in str(caught.value)
    assert "it is not followed, so credentials never leave the connection's host" in str(
        caught.value
    )
    assert len(server.seen) == 1


def test_a_repeated_cursor_stops_the_read(authority: Authority, tmp_path: Path) -> None:
    """Review Focus 5: a cursor that repeats stops before another request."""

    def answer(exchange: Exchange) -> Answer:
        cursor = _query(exchange).get("cursor")
        page = {None: 1, "a": 2, "b": 3}[cursor]
        nxt = {1: "a", 2: "b", 3: "b"}[page]
        return _json({"data": [{"id": page}], "next_cursor": nxt})

    with scripted(authority, tmp_path, answer) as server:
        _task(server.base, authority)
        with pytest.raises(UsageError) as caught:
            rest.read("api", "items", paginate=rest.Cursor("cursor", "next_cursor"))

    assert str(caught.value) == (
        "`api`: page 3 repeats the cursor of page 2; the API does not advance — check paginate="
    )
    assert len(server.seen) == 3
    assert caught.value.retryable is False


def test_a_repeated_next_link_stops_the_read(authority: Authority, tmp_path: Path) -> None:
    def answer(exchange: Exchange) -> Answer:
        page = int(_query(exchange).get("p", "1"))
        return _json({"data": [{"id": page}], "next": f"items?p={min(page + 1, 2)}"})

    with scripted(authority, tmp_path, answer) as server:
        _task(server.base, authority)
        with pytest.raises(UsageError, match="page 2 repeats the next link of page 1"):
            rest.read("api", "items", paginate=rest.NextLink(field="next"))

    assert len(server.seen) == 2


def test_a_page_number_api_that_repeats_its_last_page_stops(
    authority: Authority, tmp_path: Path
) -> None:
    def answer(exchange: Exchange) -> Answer:
        page = min(int(_query(exchange)["page"]), 2)
        return _json({"data": [{"id": page}]})

    with scripted(authority, tmp_path, answer) as server:
        _task(server.base, authority)
        with pytest.raises(UsageError, match="page 3 repeats the records of page 2"):
            rest.read("api", "items", paginate=rest.PageNumber())

    assert len(server.seen) == 3


# --- caps ---------------------------------------------------------------------------------------


def test_max_pages_names_the_cap(authority: Authority, tmp_path: Path) -> None:
    with scripted(authority, tmp_path, _numbered(1000)) as server:
        _task(server.base, authority)
        with pytest.raises(CapExceeded) as caught:
            rest.read("api", "items", paginate=rest.PageNumber(), max_pages=3)

    assert str(caught.value) == (
        "`api`: 'items' has more than max_pages (3) pages; narrow with params= or raise the cap"
    )
    assert len(server.seen) == 3


def test_max_bytes_stops_an_answer_without_a_length(authority: Authority, tmp_path: Path) -> None:
    """A chunked or unterminated page is read up to the cap, never to its end."""

    def answer(exchange: Exchange) -> Answer:
        return 200, {"Content-Type": "application/json"}, itertools.repeat(b" " * 16384)

    with scripted(authority, tmp_path, answer) as server:
        _task(server.base, authority)
        with pytest.raises(CapExceeded, match=r"hold more than max_bytes \(100000\) bytes"):
            rest.read("api", "items", max_bytes=100_000)


def test_max_bytes_counts_every_page(authority: Authority, tmp_path: Path) -> None:
    with scripted(authority, tmp_path, _numbered(5)) as server:
        _task(server.base, authority)
        with pytest.raises(CapExceeded, match="narrow with params= or raise the cap"):
            rest.read("api", "items", paginate=rest.PageNumber(), max_bytes=80)

    assert 1 < len(server.seen) < 5


def test_max_rows_names_the_cap(authority: Authority, tmp_path: Path) -> None:
    with scripted(authority, tmp_path, _numbered(5)) as server:
        _task(server.base, authority)
        with pytest.raises(CapExceeded, match=r"more than max_rows \(3\) records"):
            rest.read("api", "items", paginate=rest.PageNumber(), max_rows=3)


# --- records and types --------------------------------------------------------------------------


def test_nested_json_stays_nested_and_keys_are_unioned(
    authority: Authority, tmp_path: Path
) -> None:
    def answer(exchange: Exchange) -> Answer:
        page = int(_query(exchange)["page"])
        rows = {
            1: [{"id": 1, "lines": [{"sku": "a", "qty": 2}]}, {"id": 2, "tags": ["x"]}],
            2: [{"id": 3.5, "owner": {"name": "n"}}],
        }.get(page, [])
        return _json({"data": rows})

    with scripted(authority, tmp_path, answer) as server:
        _task(server.base, authority)
        table = rest.read("api", "items", paginate=rest.PageNumber())

    assert table.schema.field("lines").type == pa.list_(
        pa.struct([("sku", pa.string()), ("qty", pa.int64())])
    )
    assert table.schema.field("id").type == pa.float64()
    assert table.column("tags").to_pylist() == [None, ["x"], None]
    assert table.column("owner").to_pylist() == [None, None, {"name": "n"}]


def test_a_top_level_list_needs_records_empty(authority: Authority, tmp_path: Path) -> None:
    with scripted(authority, tmp_path, lambda e: _json([{"id": 1}])) as server:
        _task(server.base, authority)
        assert rest.read("api", "items", records="").num_rows == 1
        with pytest.raises(UsageError) as caught:
            rest.read("api", "items")

    assert str(caught.value) == (
        '`api`: page 1 has no `data` (the page is a list: pass records=""); pass records= (the '
        'dotted path to the list, "" for a top-level list)'
    )


@pytest.mark.parametrize(
    ("document", "text"),
    [
        ({"data": {"id": 1}}, "`data` of page 1 is not a list of objects"),
        ({"data": [1, 2]}, "`data` of page 1 is not a list of objects"),
        ({"result": []}, "page 1 has no `data`; pass records="),
    ],
)
def test_records_must_lead_to_a_list_of_objects(
    authority: Authority, tmp_path: Path, document: object, text: str
) -> None:
    with scripted(authority, tmp_path, lambda e: _json(document)) as server:
        _task(server.base, authority)
        with pytest.raises(UsageError, match=text.replace("(", r"\(")):
            rest.read("api", "items")


@pytest.mark.parametrize(
    "pages",
    [
        {1: [{"id": 1}, {"id": "a"}]},
        {1: [{"id": 1}], 2: [{"id": "a"}]},
        {1: [{"id": 2**70}]},
    ],
    ids=["within-a-page", "across-pages", "beyond-64-bits"],
)
def test_a_field_without_one_type_is_a_shape_error(
    authority: Authority, tmp_path: Path, pages: dict[int, list[dict[str, object]]]
) -> None:
    def answer(exchange: Exchange) -> Answer:
        return _json({"data": pages.get(int(_query(exchange)["page"]), [])})

    with scripted(authority, tmp_path, answer) as server:
        _task(server.base, authority)
        with pytest.raises(FileShapeError) as caught:
            rest.read("api", "items", paginate=rest.PageNumber())

    assert "read it with connections.get and your own client" in str(caught.value)
    assert caught.value.retryable is False


def test_no_records_is_a_table_without_columns(authority: Authority, tmp_path: Path) -> None:
    def answer(exchange: Exchange) -> Answer:
        return (204, {}, b"") if "empty" in exchange.target else _json({"data": None})

    with scripted(authority, tmp_path, answer) as server:
        _task(server.base, authority)
        assert rest.read("api", "items").num_columns == 0
        assert rest.read("api", "empty").num_columns == 0


@pytest.mark.parametrize("body", [b"<html>maintenance</html>", b'{"data": [{"x": NaN}]}'])
def test_a_page_that_is_not_json_is_retried_by_the_harness(
    authority: Authority, tmp_path: Path, body: bytes
) -> None:
    with scripted(
        authority, tmp_path, lambda e: (200, {"Content-Type": "text/html"}, body)
    ) as server:
        _task(server.base, authority)
        with pytest.raises(SourceError) as caught:
            rest.read("api", "items")

    assert type(caught.value) is SourceError
    assert "page 1 of GET" in str(caught.value) and "is not JSON (text/html)" in str(caught.value)


# --- arguments, paths and parameters ------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "https://evil.test/x",
        "//evil.test/x",
        "HTTPS://evil.test/x",
        "a/../b",
        "a//b",
        "./a",
        "items?x=1",
        "items#top",
        "a\\b",
        "a\nb",
        "a%zz",
    ],
)
def test_a_path_that_could_leave_the_base_url_is_refused_before_any_request(
    authority: Authority, tmp_path: Path, path: str
) -> None:
    with scripted(authority, tmp_path, _numbered(1)) as server:
        _task(server.base, authority)
        with pytest.raises(UsageError):
            rest.read("api", path)

    assert server.seen == []


def test_a_path_is_encoded_below_the_base_path(authority: Authority, tmp_path: Path) -> None:
    with scripted(authority, tmp_path, lambda e: _json({"data": []})) as server:
        _task(f"{server.base}/v1/", authority)
        rest.read("api", "/groups/a%2Fb/my items")
        rest.read("api", "")

    assert [s.target for s in server.seen] == ["/v1/groups/a%2Fb/my%20items", "/v1"]


def test_params_none_is_refused_naming_the_default(authority: Authority) -> None:
    _task("https://api.test", authority)

    with pytest.raises(
        UsageError, match=r"params= since is None.*ctx\.watermark\(default=\.\.\.\)"
    ):
        rest.read("api", "items", params={"since": None})


def test_params_are_written_as_the_api_expects(authority: Authority, tmp_path: Path) -> None:
    with scripted(authority, tmp_path, lambda e: _json({"data": []})) as server:
        _task(server.base, authority)
        rest.read(
            "api",
            "items",
            params={
                "since": dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=dt.UTC),
                "day": dt.date(2026, 1, 2),
                "open": True,
                "n": 3,
            },
        )

    assert _query(server.seen[0]) == {
        "since": "2026-01-02T03:04:05+00:00",
        "day": "2026-01-02",
        "open": "true",
        "n": "3",
    }


@pytest.mark.parametrize(
    ("kwargs", "text"),
    [
        ({"params": {"page": 2}, "paginate": rest.PageNumber()}, "'page', which PageNumber sets"),
        ({"params": {"key": "x"}}, "'key', which the connection's API key sets"),
        ({"params": {"x": [1]}}, "params= x is a list"),
        ({"params": {"x": float("nan")}}, "params= x is not a finite number"),
        ({"params": [("x", 1)]}, "params= takes a mapping"),
        ({"paginate": "page"}, "paginate= takes rest.PageNumber"),
        ({"records": None}, "records= is a key or a dotted path"),
        ({"retries": -1}, "retries= must be a whole number from 0"),
        ({"retry_budget_seconds": float("inf")}, "retry_budget_seconds= must be a number"),
        ({"max_pages": 0}, "max_pages= must be a positive whole number"),
    ],
)
def test_arguments_are_checked_before_any_request(
    authority: Authority, kwargs: dict[str, object], text: str
) -> None:
    settings = {"auth": "api_key", "api_key_name": "key", "api_key_in": "query"}
    _task("https://api.test", authority, settings, {"api_key": API_KEY})

    with pytest.raises(UsageError) as caught:
        rest.read("api", "items", **kwargs)  # ty: ignore[invalid-argument-type]

    assert text in str(caught.value)
    assert caught.value.retryable is False


@pytest.mark.parametrize(
    ("make", "text"),
    [
        (lambda: rest.PageNumber(param=""), "PageNumber(param=) must be a non-empty name"),
        (lambda: rest.PageNumber(start=-1), "PageNumber(start=) must be a whole number from 0"),
        (lambda: rest.PageNumber(size=10), "takes size_param= and size= together"),
        (lambda: rest.PageNumber(size_param="n", size=0), "must be a positive whole number"),
        (lambda: rest.Cursor("", "next"), "Cursor(param=)"),
        (lambda: rest.Cursor("c", "a\nb"), "Cursor(cursor_field=)"),
        (lambda: rest.NextLink(field=""), "NextLink(field=)"),
    ],
)
def test_paginators_check_their_fields(make: Callable[[], object], text: str) -> None:
    with pytest.raises(UsageError) as caught:
        make()

    assert text in str(caught.value)


def test_a_connection_of_another_kind_names_its_helper(authority: Authority) -> None:
    _task("https://api.test", authority, kind="s3")

    with pytest.raises(
        UsageError, match=r"rest\.read reads http connections; `api` is s3: use files\.read"
    ):
        rest.read("api", "items")


@pytest.mark.parametrize(
    ("document", "paginate", "text"),
    [
        (
            {"data": [{"id": 1}], "next": {"x": 1}},
            rest.Cursor("c", "next"),
            "is a dict, not a cursor",
        ),
        ({"data": [{"id": 1}], "next": 5}, rest.NextLink(field="next"), "is a int, not a link"),
    ],
)
def test_a_cursor_or_link_of_the_wrong_type_is_named(
    authority: Authority, tmp_path: Path, document: object, paginate: object, text: str
) -> None:
    with scripted(authority, tmp_path, lambda e: _json(document)) as server:
        _task(server.base, authority)
        with pytest.raises(UsageError, match=text):
            rest.read("api", "items", paginate=paginate)  # ty: ignore[invalid-argument-type]


def test_a_whole_number_cursor_is_sent_as_text(authority: Authority, tmp_path: Path) -> None:
    def answer(exchange: Exchange) -> Answer:
        after = int(_query(exchange).get("after", "0"))
        return _json({"data": [{"id": after}], "next": after + 1 if after < 2 else None})

    with scripted(authority, tmp_path, answer) as server:
        _task(server.base, authority)
        table = rest.read("api", "items", paginate=rest.Cursor("after", "next"))

    assert table.column("id").to_pylist() == [0, 1, 2]


def test_the_wall_clock_is_utc() -> None:
    assert _http._wall().tzinfo is dt.UTC


@pytest.mark.parametrize(("status", "transient"), [("502", True), ("504", True), ("403", False)])
def test_only_the_proxys_gateway_failures_are_transient(status: str, transient: bool) -> None:
    assert _http._transient(httpx.ProxyError(f"{status} X")) is transient


def test_a_page_cut_short_is_retried_then_named(
    authority: Authority, tmp_path: Path, clock: Clock
) -> None:
    def answer(exchange: Exchange) -> Answer:
        return 200, {"Content-Length": "100"}, b'{"data": []}'

    with scripted(authority, tmp_path, answer) as server:
        _task(server.base, authority)
        with pytest.raises(SourceError, match=r"failed while answering \(RemoteProtocolError\)"):
            rest.read("api", "items", retries=1)

    assert len(server.seen) == 2


def test_an_error_while_reading_that_cannot_pass_is_not_retried(
    authority: Authority, tmp_path: Path, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(response: object, limit: int) -> bytes:
        raise httpx.LocalProtocolError("bad")

    monkeypatch.setattr(_http, "read_capped", refuse)
    with scripted(authority, tmp_path, lambda e: _json({"data": []})) as server:
        _task(server.base, authority)
        with pytest.raises(UsageError, match="refused to send"):
            rest.read("api", "items")

    assert clock.slept == []


@pytest.mark.parametrize("path", [5, "http://[::1"])
def test_a_path_that_is_no_text_or_no_url_is_refused(authority: Authority, path: object) -> None:
    _task("https://api.test", authority)

    with pytest.raises(UsageError):
        rest.read("api", path)  # ty: ignore[invalid-argument-type]


def test_a_next_link_that_cannot_be_read_is_not_followed(
    authority: Authority, tmp_path: Path
) -> None:
    document = {"data": [{"id": 1}], "next": "https://[::1/x"}
    with scripted(authority, tmp_path, lambda e: _json(document)) as server:
        _task(server.base, authority)
        with pytest.raises(SourceRefused, match="points to a URL that cannot be read"):
            rest.read("api", "items", paginate=rest.NextLink(field="next"))
