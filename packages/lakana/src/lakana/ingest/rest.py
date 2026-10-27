"""Read a JSON REST API through an ``http`` connection the asset declares, inside a Lakana task.

Example::

    from lakana import asset
    from lakana.ingest import rest


    @asset(connections=["crm-api"])
    def customers():
        pages = rest.PageNumber(size_param="per_page", size=100)
        return rest.read("crm-api", "customers", paginate=pages)

`rest.read` returns a `pyarrow.Table`: one row per record, nested objects and lists kept as
structs and lists. It sends ``GET`` requests below the connection's base URL with the
connection's auth (`lakana.ingest._http.client_for`), follows the API's pages by page number,
cursor or next link, and retries what may pass within a time budget. A request never leaves the
base URL's scheme, host and port: an absolute ``path``, or a next link to another host, is
refused before anything is sent.
"""

from __future__ import annotations

import datetime as dt
import decimal
import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, NoReturn
from urllib.parse import quote, urlsplit

from lakana.ingest import _http, connections
from lakana.ingest._errors import (
    CapExceeded,
    FileShapeError,
    SourceError,
    SourceRefused,
    UsageError,
    safe_name,
    task_environment,
)
from lakana.ingest.files import DEFAULT_MAX_BYTES, DEFAULT_MAX_ROWS, _positive

if TYPE_CHECKING:
    import httpx
    import pyarrow as pa

MAX_PAGES = 10_000
"""The default of `max_pages`."""
RETRIES = 5
"""The default of `retries`: how many times one request may be sent again."""
RETRY_BUDGET_SECONDS = 300
"""The default of `retry_budget_seconds`: no attempt of a request starts later than this."""

_OTHER_HELPER = {
    "postgresql": "sql.read",
    "mariadb": "sql.read",
    "sqlite-file": "sql.read",
    "s3": "files.read",
    "gcs": "files.read",
    "azure": "files.read",
    "directory": "files.read",
    "tcp": "connections.get with the team's own client",
}
_BAD_PERCENT = re.compile(r"%(?![0-9A-Fa-f]{2})")
_SEGMENT_SAFE = "!$&'()*+,;=:@-._~%"
_MISSING = object()


def _name(value: object, argument: str) -> str:
    """Check a query parameter or field name of a paginator.

    Parameters
    ----------
    value
        The name.
    argument
        The argument, for the message.

    Returns
    -------
    str
        `value`.

    Raises
    ------
    UsageError
        For anything but a non-empty text without control characters.
    """
    if not isinstance(value, str) or not value or safe_name(value) != value:
        raise UsageError(f"{argument} must be a non-empty name, not {value!r}")
    return value


@dataclass(frozen=True)
class PageNumber:
    """Pages by number: ``?page=1``, ``?page=2``, … until a page without records.

    Parameters
    ----------
    param
        The query parameter of the page number.
    start
        The first page's number (0 or 1 for most APIs).
    size_param
        The query parameter of the page size, such as ``per_page``; with `size`.
    size
        The page size to ask for; with `size_param`.
    """

    param: str = "page"
    start: int = 1
    size_param: str | None = None
    size: int | None = None

    def __post_init__(self) -> None:
        """Check the fields.

        Raises
        ------
        UsageError
            For a name that is empty, a start that is no whole number from 0, or a size that is
            given without its parameter (or the other way round) or is not positive.
        """
        _name(self.param, "PageNumber(param=)")
        if isinstance(self.start, bool) or not isinstance(self.start, int) or self.start < 0:
            raise UsageError(
                f"PageNumber(start=) must be a whole number from 0, not {self.start!r}"
            )
        if (self.size_param is None) != (self.size is None):
            raise UsageError("PageNumber takes size_param= and size= together, or neither")
        if self.size_param is not None:
            _name(self.size_param, "PageNumber(size_param=)")
            _positive(**{"PageNumber(size=)": self.size})


@dataclass(frozen=True)
class Cursor:
    """Pages by cursor: each answer names the cursor of the next page, until it names none.

    Parameters
    ----------
    param
        The query parameter that sends the cursor, such as ``cursor``.
    cursor_field
        Where the answer holds the next cursor: a key, or a dotted path such as
        ``meta.next_cursor``.
    """

    param: str
    cursor_field: str

    def __post_init__(self) -> None:
        """Check the fields.

        Raises
        ------
        UsageError
            For an empty name.
        """
        _name(self.param, "Cursor(param=)")
        _name(self.cursor_field, "Cursor(cursor_field=)")


@dataclass(frozen=True)
class NextLink:
    """Pages by link: each answer holds the URL of the next page, until it holds none.

    Parameters
    ----------
    field
        Where the answer holds the link: a key such as ``@odata.nextLink`` (OData), or a dotted
        path such as ``links.next``.
    """

    field: str = "@odata.nextLink"

    def __post_init__(self) -> None:
        """Check the field.

        Raises
        ------
        UsageError
            For an empty name.
        """
        _name(self.field, "NextLink(field=)")


def _lookup(document: object, path: str) -> object:
    """Find a value in a JSON document by key or dotted path.

    A key that holds dots (``@odata.nextLink``) is looked up whole first.

    Parameters
    ----------
    document
        The document.
    path
        A key or a dotted path; ``""`` is the document itself.

    Returns
    -------
    object
        The value, or `_MISSING`.
    """
    if path == "":
        return document
    if isinstance(document, dict) and path in document:
        return document[path]
    current = document
    for key in path.split("."):
        if not isinstance(current, dict) or key not in current:
            return _MISSING
        current = current[key]
    return current


def _query_value(key: str, value: object) -> str:
    """Write one value of ``params=`` as query text.

    Parameters
    ----------
    key
        The parameter, for the message.
    value
        The value.

    Returns
    -------
    str
        ``true``/``false`` for a bool, ISO 8601 for a date or datetime, else ``str(value)``.

    Raises
    ------
    UsageError
        For `None` (a first run's watermark), a number that is not finite, or another type.
    """
    if value is None:
        raise UsageError(
            f"params= {key} is None, which would send an empty value; pass "
            "ctx.watermark(default=...) or leave the parameter out"
        )
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        raise UsageError(f"params= {key} is not a finite number")
    if isinstance(value, str | int | float | decimal.Decimal):
        return str(value)
    raise UsageError(
        f"params= {key} is a {type(value).__name__}; a value is a str, int, float, bool, "
        "Decimal, date or datetime"
    )


def _first_url(base_url: str, path: str) -> str:
    """Join a path to the base URL, each segment percent-encoded.

    Parameters
    ----------
    base_url
        The connection's base URL.
    path
        What the asset passed.

    Returns
    -------
    str
        The URL of the first page.

    Raises
    ------
    UsageError
        For a path that is not text, an absolute URL, a network path (``//host``), a query, a
        fragment, a backslash, a control character, an empty, ``.`` or ``..`` segment, or a
        ``%`` that is no escape.
    """
    if not isinstance(path, str):
        raise UsageError(f"path= is the path below the base URL, a str, not {type(path).__name__}")
    try:
        absolute = bool(urlsplit(path).scheme) or path.startswith("//")
    except ValueError:
        absolute = True
    if absolute:
        raise UsageError(
            f"path= is relative to the connection's base URL; {_http.shown_url(path)!r} is an "
            "absolute URL or a network path: pass the part below the base URL, so the request "
            "stays on the connection's host"
        )
    if safe_name(path) != path or set(path) & {"\\", "?", "#"} or _BAD_PERCENT.search(path):
        raise UsageError(
            f"path= {safe_name(path)!r} holds a control character, a backslash, a query or a "
            "fragment, or a % that is no escape; pass query values with params="
        )
    segments = path.removeprefix("/").split("/") if path not in ("", "/") else []
    if any(segment in ("", ".", "..") for segment in segments):
        raise UsageError(
            f"path= {safe_name(path)!r} has an empty, '.' or '..' segment; pass the path below "
            "the base URL as it is"
        )
    encoded = "/".join(quote(segment, safe=_SEGMENT_SAFE) for segment in segments)
    return base_url.rstrip("/") + (f"/{encoded}" if encoded else "")


def _check_params(
    params: Mapping[str, object] | None, reserved: Mapping[str, str]
) -> dict[str, str]:
    """Check ``params=`` and write its values as text.

    Parameters
    ----------
    params
        What the asset passed.
    reserved
        Query parameters the read sets itself, to what sets them.

    Returns
    -------
    dict
        Name to text.

    Raises
    ------
    UsageError
        For a value that is no mapping, a key that is no name, a key the paginator or the
        connection's API key uses, or a value `_query_value` refuses.
    """
    if params is None:
        return {}
    if not isinstance(params, Mapping):
        raise UsageError(
            f"params= takes a mapping of query parameter to value, not {type(params).__name__}"
        )
    found: dict[str, str] = {}
    for key, value in params.items():
        _name(key, "a key of params=")
        if key in reserved:
            raise UsageError(f"params= has {key!r}, which {reserved[key]} sets; leave it out")
        found[key] = _query_value(key, value)
    return found


def _page_table(name: str, page: int, records: list[dict[str, object]]) -> pa.Table:
    """Turn one page's records into a table: every key a column, nested values kept.

    Parameters
    ----------
    name
        The connection, for messages.
    page
        The page's number from 1, for messages.
    records
        The records.

    Returns
    -------
    pyarrow.Table
        One row per record; a column for every key of any record, null where a record lacks it.

    Raises
    ------
    FileShapeError
        For a field whose values have no common Arrow type (a number and a text) or that do not
        fit one (an integer beyond 64 bits).
    """
    import pyarrow as pa

    keys = list(dict.fromkeys(key for record in records for key in record))
    columns = {}
    for key in keys:
        try:
            columns[key] = pa.array([record.get(key) for record in records])
        except (pa.ArrowInvalid, pa.ArrowTypeError, OverflowError) as exc:
            raise FileShapeError(
                f"`{name}`: field {safe_name(key)!r} of page {page} holds values of different "
                f"types or a value Arrow cannot hold ({safe_name(str(exc))[:200]}); read it with "
                "connections.get and your own client to convert it"
            ) from None
    return pa.table(columns)


def _records(name: str, page: int, document: object, records: str) -> list[dict[str, object]]:
    """Find a page's records.

    Parameters
    ----------
    name
        The connection, for messages.
    page
        The page's number from 1.
    document
        The page's JSON.
    records
        The dotted path to the list, ``""`` for a top-level list.

    Returns
    -------
    list of dict
        The records; ``null`` is no records.

    Raises
    ------
    UsageError
        When the path is missing, or leads to something that is no list of objects.
    """
    found = _lookup(document, records)
    if found is _MISSING:
        hint = ' (the page is a list: pass records="")' if isinstance(document, list) else ""
        raise UsageError(
            f"`{name}`: page {page} has no `{safe_name(records)}`{hint}; pass records= (the "
            'dotted path to the list, "" for a top-level list)'
        )
    if found is None:
        return []
    if not isinstance(found, list) or not all(isinstance(item, dict) for item in found):
        raise UsageError(
            f"`{name}`: `{safe_name(records) or 'the page'}` of page {page} is not a list of "
            "objects; pass records= with the dotted path to the list of records"
        )
    return found


def _refusal(name: str, answer: _http.Answer) -> SourceError:
    """Make the error of an answer other than 2xx, which `_http.send` did not retry.

    Parameters
    ----------
    name
        The connection.
    answer
        The answer.

    Returns
    -------
    SourceError
        `SourceRefused` for 401 and 403 (naming the test command), a redirect and another 4xx;
        `SourceError` (retried by the harness) for another 5xx.
    """
    shown = f"GET {_http.shown_url(answer.url)}"
    status = answer.status
    if status in (401, 403):
        return SourceRefused(
            f"`{name}`: the API refused the credential (HTTP {status} for {shown}). Check the "
            f"secret with `lakana connection test {name} --env {task_environment()}`, and that "
            "it may read this path."
        )
    if 300 <= status < 400:
        return SourceRefused(
            f"`{name}`: {shown} answers with a redirect (HTTP {status}), which rest.read does not "
            "follow; use the path it names, or the address as the connection's base URL"
        )
    if 400 <= status < 500:
        return SourceRefused(
            f"`{name}`: the API refused the request (HTTP {status} for {shown}); check path= and "
            "params="
        )
    return SourceError(f"`{name}`: the API answered HTTP {status} for {shown}")


def _not_json(constant: str) -> NoReturn:
    """Refuse ``NaN`` and ``Infinity``, which are not JSON.

    Parameters
    ----------
    constant
        The constant.

    Raises
    ------
    ValueError
        Always.
    """
    raise ValueError(constant)


def _parse(name: str, page: int, answer: _http.Answer) -> object:
    """Parse a page's JSON.

    Parameters
    ----------
    name
        The connection, for messages.
    page
        The page's number from 1.
    answer
        The answer.

    Returns
    -------
    object
        The JSON; `None` for 204 or an empty body.

    Raises
    ------
    SourceError
        When the body is no JSON (retried: an API may answer a maintenance page for a while).
    """
    if answer.status == 204 or not answer.body.strip():
        return None
    try:
        return json.loads(answer.body, parse_constant=_not_json)
    except (ValueError, RecursionError):
        kind = safe_name(answer.headers.get("content-type", "no content type"))[:80]
        raise SourceError(
            f"`{name}`: page {page} of GET {_http.shown_url(answer.url)} is not JSON ({kind}); "
            "check path= against the API's documentation"
        ) from None


def _fingerprint(records: list[dict[str, object]]) -> str:
    """Hash a page's records, to see a page that repeats.

    Parameters
    ----------
    records
        The records.

    Returns
    -------
    str
        A SHA-256 of their canonical JSON.
    """
    text = json.dumps(records, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode()).hexdigest()


def _repeated(name: str, page: int, earlier: int, what: str) -> UsageError:
    """Make the error of a page that would repeat an earlier one.

    Parameters
    ----------
    name
        The connection.
    page
        The page that repeats.
    earlier
        The page it repeats.
    what
        ``records``, ``cursor`` or ``next link``.

    Returns
    -------
    UsageError
        Naming both pages.
    """
    return UsageError(
        f"`{name}`: page {page} repeats the {what} of page {earlier}; the API does not advance — "
        "check paginate="
    )


def _next_cursor(name: str, page: int, document: object, paginate: Cursor) -> str | None:
    """Read the next cursor from a page.

    Parameters
    ----------
    name
        The connection, for messages.
    page
        The page's number from 1.
    document
        The page's JSON.
    paginate
        The paginator.

    Returns
    -------
    str or None
        The cursor; `None` when the field is missing, null or empty.

    Raises
    ------
    UsageError
        For a cursor that is neither text nor a whole number.
    """
    value = _lookup(document, paginate.cursor_field)
    if value is _MISSING or value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, str | int):
        raise UsageError(
            f"`{name}`: `{safe_name(paginate.cursor_field)}` of page {page} is a "
            f"{type(value).__name__}, not a cursor; check Cursor(cursor_field=)"
        )
    return str(value)


def _next_link(
    name: str, page: int, document: object, paginate: NextLink, answer: _http.Answer, base: str
) -> httpx.URL | None:
    """Read and check the next link of a page.

    Parameters
    ----------
    name
        The connection, for messages.
    page
        The page's number from 1.
    document
        The page's JSON.
    paginate
        The paginator.
    answer
        The page's answer; a relative link is resolved against its URL.
    base
        The connection's base URL.

    Returns
    -------
    httpx.URL or None
        The next page's URL; `None` when the field is missing, null or empty.

    Raises
    ------
    UsageError
        For a link that is no text.
    SourceRefused
        For a link to another scheme, host or port, or with a user or password: it is not
        followed.
    """
    value = _lookup(document, paginate.field)
    if value is _MISSING or value is None or value == "":
        return None
    if not isinstance(value, str):
        raise UsageError(
            f"`{name}`: `{safe_name(paginate.field)}` of page {page} is a "
            f"{type(value).__name__}, not a link; check NextLink(field=)"
        )
    try:
        url = answer.url.join(value)
    except Exception:
        url = None
    if url is None or not _http.same_origin(url, base):
        target = safe_name(url.host if url is not None else "a URL that cannot be read")
        raise SourceRefused(
            f"`{name}`: the next link of page {page} points to {target}, not to "
            f"{safe_name(answer.url.host)} with the base URL's scheme and port; it is not "
            "followed, so credentials never leave the connection's host"
        )
    return url


def read(
    connection: str,
    path: str,
    *,
    paginate: PageNumber | Cursor | NextLink | None = None,
    params: Mapping[str, object] | None = None,
    records: str = "data",
    max_pages: int = MAX_PAGES,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_rows: int = DEFAULT_MAX_ROWS,
    retries: int = RETRIES,
    retry_budget_seconds: float = RETRY_BUDGET_SECONDS,
) -> pa.Table:
    """Read the records of a JSON REST API, page by page, into one table.

    Parameters
    ----------
    connection
        A connection the asset declares, of kind ``http``.
    path
        The path below the connection's base URL, such as ``customers`` (``""`` for the base
        URL itself); query values go in `params`.
    paginate
        `PageNumber`, `Cursor` or `NextLink`; `None` reads one page.
    params
        Query parameters: values are str, int, float, bool, Decimal, date or datetime (sent in
        ISO 8601). Sent with every request, except with `NextLink`, whose links carry their own.
    records
        Where a page holds its list of records: a key or a dotted path (``"value"`` for OData),
        ``""`` for a page that is a list.
    max_pages
        The most pages read.
    max_bytes
        The most bytes read in all (after decompression).
    max_rows
        The most records read in all.
    retries
        How many times one request may be sent again after a timeout, a network error, 408,
        429 or 5xx (500, 502, 503, 504).
    retry_budget_seconds
        No attempt of a request starts later than this many seconds after its first.

    Returns
    -------
    pyarrow.Table
        One row per record; a column per key of any record (null where a record lacks it);
        nested objects as structs, lists as lists. No records: a table without columns.

    Raises
    ------
    UsageError
        For arguments, a connection of another kind, a missing secret, records= that do not
        lead to a list of objects, a page that repeats an earlier page's records, cursor or
        next link.
    SourceRefused
        When the API refuses the credential (401, 403) or the request (another 4xx), answers
        with a redirect, its certificate is not trusted, or a next link names another host.
    SourceError
        When the API cannot be reached or keeps failing past the retries or the budget, or a
        page is not JSON; another attempt may end differently.
    FileShapeError
        For a field whose values have no common type.
    CapExceeded
        Past `max_pages`, `max_bytes` or `max_rows`.
    """
    import pyarrow as pa

    _positive(max_pages=max_pages, max_bytes=max_bytes, max_rows=max_rows)
    if isinstance(retries, bool) or not isinstance(retries, int) or retries < 0:
        raise UsageError(f"retries= must be a whole number from 0, not {retries!r}")
    if (
        isinstance(retry_budget_seconds, bool)
        or not isinstance(retry_budget_seconds, int | float)
        or not 0 <= retry_budget_seconds < math.inf
    ):
        raise UsageError(
            "retry_budget_seconds= must be a number of seconds from 0, not "
            f"{retry_budget_seconds!r}"
        )
    if paginate is not None and not isinstance(paginate, PageNumber | Cursor | NextLink):
        raise UsageError(
            "paginate= takes rest.PageNumber(...), rest.Cursor(...), rest.NextLink(...) or None, "
            f"not {type(paginate).__name__}"
        )
    if not isinstance(records, str) or safe_name(records) != records:
        raise UsageError(f'records= is a key or a dotted path ("" for a list), not {records!r}')
    conn = connections.get(connection)
    if conn.kind != "http":
        helper = _OTHER_HELPER.get(conn.kind)
        hint = f": use {helper}" if helper else ""
        raise UsageError(f"rest.read reads http connections; `{conn.name}` is {conn.kind}{hint}")
    base = str(conn.address["base_url"])
    url: httpx.URL | str = _first_url(base, path)
    reserved: dict[str, str] = {}
    if isinstance(paginate, PageNumber):
        reserved[paginate.param] = "PageNumber"
        if paginate.size_param:
            reserved[paginate.size_param] = "PageNumber"
    elif isinstance(paginate, Cursor):
        reserved[paginate.param] = "Cursor"
    if conn.settings.get("auth") == "api_key" and conn.settings.get("api_key_in") == "query":
        reserved[str(conn.settings["api_key_name"])] = "the connection's API key"
    query = _check_params(params, reserved)
    tables: list[pa.Table] = []
    seen_records: dict[str, int] = {}
    seen_next: dict[str, int] = {}
    read_bytes = rows = 0
    page = 1
    cursor: str | None = None
    with _http.client_for(conn) as client:
        while True:
            sent = dict(query)
            if isinstance(paginate, PageNumber):
                sent[paginate.param] = str(paginate.start + page - 1)
                if paginate.size_param:
                    sent[paginate.size_param] = str(paginate.size)
            elif isinstance(paginate, Cursor) and cursor is not None:
                sent[paginate.param] = cursor
            elif isinstance(paginate, NextLink) and page > 1:
                sent = {}
            request = client.build_request(
                "GET", url, params=sent or None, headers={"Accept": "application/json"}
            )
            try:
                answer = _http.send(
                    client,
                    request,
                    name=conn.name,
                    limit=max_bytes - read_bytes,
                    retries=retries,
                    budget=float(retry_budget_seconds),
                )
            except OverflowError:
                raise CapExceeded(
                    f"`{conn.name}`: the pages of {safe_name(path)!r} hold more than max_bytes "
                    f"({max_bytes}) bytes; narrow with params= or raise the cap"
                ) from None
            if not 200 <= answer.status < 300:
                refused = _refusal(conn.name, answer)
                raise refused
            read_bytes += len(answer.body)
            document = _parse(conn.name, page, answer)
            found = [] if document is None else _records(conn.name, page, document, records)
            if found:
                mark = _fingerprint(found)
                if mark in seen_records:
                    repeated = _repeated(conn.name, page, seen_records[mark], "records")
                    raise repeated
                seen_records[mark] = page
                rows += len(found)
                if rows > max_rows:
                    raise CapExceeded(
                        f"`{conn.name}`: the pages of {safe_name(path)!r} hold more than "
                        f"max_rows ({max_rows}) records; narrow with params= or raise the cap"
                    )
                tables.append(_page_table(conn.name, page, found))
            if isinstance(paginate, PageNumber):
                more = bool(found)
            elif isinstance(paginate, Cursor):
                cursor = _next_cursor(conn.name, page, document, paginate)
                if cursor is not None and cursor in seen_next:
                    repeated = _repeated(conn.name, page, seen_next[cursor], "cursor")
                    raise repeated
                if cursor is not None:
                    seen_next[cursor] = page
                more = cursor is not None
            elif isinstance(paginate, NextLink):
                link = _next_link(conn.name, page, document, paginate, answer, base)
                if link is not None and str(link) in seen_next:
                    repeated = _repeated(conn.name, page, seen_next[str(link)], "next link")
                    raise repeated
                if link is not None:
                    seen_next[str(link)] = page
                    url = link
                more = link is not None
            else:
                more = False
            if not more:
                break
            if page >= max_pages:
                raise CapExceeded(
                    f"`{conn.name}`: {safe_name(path)!r} has more than max_pages ({max_pages}) "
                    "pages; narrow with params= or raise the cap"
                )
            page += 1
    if not tables:
        return pa.table({})
    try:
        return pa.concat_tables(tables, promote_options="permissive")
    except (pa.ArrowInvalid, pa.ArrowTypeError) as exc:
        raise FileShapeError(
            f"`{conn.name}`: the pages of {safe_name(path)!r} cannot be combined "
            f"({safe_name(str(exc))[:200]}); read it with connections.get and your own client "
            "to convert it"
        ) from None
