"""Read flat files through a connection the asset declares, inside a Lakana task.

Example::

    from lakana import asset
    from lakana.ingest import files


    @asset(connections=["exports"], write="append", watermark="_source_modified")
    def orders(ctx):
        return files.read("exports", "orders/*.csv", since=ctx.watermark())

`files.read` returns a `pyarrow.Table`: CSV, Parquet, JSON, NDJSON and spreadsheets, from an
``s3``, ``gcs`` or ``azure`` connection (objects below its prefix, through obstore), an ``http``
connection (one file below its base URL, through httpx) or a ``directory`` connection (below an
ingestion root, never following a symbolic link). Under the Docker runner the stores and the
HTTPS client go through the task's egress proxy; nothing else in the task uses it. With
``since=`` it reads only the files modified after it and adds the column ``_source_modified``.
"""

from __future__ import annotations

import builtins
import contextlib
import datetime as dt
import re
import sys
from collections.abc import Generator, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, NoReturn, Protocol
from urllib.parse import quote, urlsplit

from lakana import context
from lakana.ingest import _check, _directory, _formats, _http, _stores, connections
from lakana.ingest._connections import FetchedConnection, fetch
from lakana.ingest._errors import CapExceeded, SourceError, SourceRefused, UsageError, safe_name

if TYPE_CHECKING:
    import httpx
    import pyarrow as pa
    from obstore.store import AzureStore, GCSStore, S3Store

DEFAULT_MAX_BYTES = 256 * 1024 * 1024
"""The default of `max_file_bytes` and `max_total_bytes`: the server's default write cap."""
DEFAULT_MAX_ROWS = 10_000_000
"""The default of `max_rows`: about the most rows of a narrow table the server's 256 MiB append
takes, and the bound on what a compressed file (Parquet, a workbook) is decoded to."""
SOURCE_MODIFIED = "_source_modified"
"""The column `read` adds with ``since=``: each row's file's modification time, UTC."""
KINDS = ("http", "s3", "gcs", "azure", "directory")
"""The connection kinds `read` and `list` read."""
_OTHER_HELPER = {
    "postgresql": "sql.read",
    "mariadb": "sql.read",
    "sqlite-file": "sql.read",
    "tcp": "connections.get with the team's own driver",
}


class _Unset:
    """The type of `UNSET`."""

    def __repr__(self) -> str:
        """Name the sentinel.

        Returns
        -------
        str
            ``UNSET``.
        """
        return "UNSET"


UNSET: Final = _Unset()
"""``since=`` left out: read every file and add no ``_source_modified``."""


class _Source(Protocol):
    """Where the files of a connection are; one per connection kind."""

    def list(self, pattern: str, max_files: int) -> _directory.Listing:
        """List the files that match a pattern.

        Parameters
        ----------
        pattern
            A glob relative to the connection's path or prefix.
        max_files
            The most files to return.

        Returns
        -------
        Listing
            The files, sorted by path, and what was skipped.
        """
        ...

    def read(self, path: str, max_bytes: int) -> bytes:
        """Read one listed file.

        Parameters
        ----------
        path
            Its path as listed.
        max_bytes
            The most bytes to read.

        Returns
        -------
        bytes
            Its content.
        """
        ...

    def close(self) -> None:
        """Release what the source holds (an HTTPS client's connections)."""
        ...


@dataclass(frozen=True)
class _DirectorySource:
    """A ``directory`` connection: a path below an ingestion root.

    Parameters
    ----------
    root_dir
        The root as the task sees it.
    base
        The connection's path below it.
    """

    root_dir: str
    base: str

    def list(self, pattern: str, max_files: int) -> _directory.Listing:
        """List through the walker.

        Parameters
        ----------
        pattern
            The glob.
        max_files
            The cap.

        Returns
        -------
        Listing
            What the walk found.
        """
        return _directory.walk(self.root_dir, self.base, pattern, max_files=max_files)

    def read(self, path: str, max_bytes: int) -> bytes:
        """Read through the walker.

        Parameters
        ----------
        path
            The listed path.
        max_bytes
            The cap.

        Returns
        -------
        bytes
            The content.
        """
        return _directory.read(self.root_dir, self.base, path, max_bytes=max_bytes)

    def close(self) -> None:
        """Hold nothing: every walk and read opens and closes its own descriptors."""


_GLOB = frozenset("*?[")
_PROBLEM_CHARS = 500
_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s<>\"'`]+")
_SIGNED_ELEMENT = re.compile(
    r"<(StringToSign(?:Bytes)?|CanonicalRequest(?:Bytes)?|SignatureProvided|Signature)>.*?</\1>",
    re.DOTALL,
)
_SIGNED_PARAMETER = re.compile(
    r"(?i)\b(sig|signature|x-amz-signature|x-amz-credential|x-amz-security-token"
    r"|x-goog-signature|x-goog-credential|access_token|token|api_key|key|code)=[^&\s\"'<>]+"
)
_AUTH_HEADER = re.compile(r"(?i)\b((?:proxy-)?authorization)\s*[:=]\s*[^\n]*")
_CERTIFICATE = re.compile(
    r"InvalidCertificate\(\s*\w+,?\s*\)|certificate verify failed[^(\n]*|certificate",
    re.IGNORECASE,
)
_XML_HEAD = re.compile(r":?\s*<\?xml[^>]*\?>\s*$")
_ERROR_CODE = re.compile(r"<Code>([^<]{1,100})</Code>")
_ERROR_MESSAGE = re.compile(r"<Message>([^<\n]{1,300})")
_REFUSED_STATUS = re.compile(r"status code: (?:401|403)\b")


def _utc(value: dt.datetime) -> dt.datetime:
    """Make a store's or a server's time UTC (Python's times are whole microseconds already).

    Parameters
    ----------
    value
        A time; a naive one is UTC.

    Returns
    -------
    datetime
        UTC-aware.
    """
    return value.replace(tzinfo=dt.UTC) if value.tzinfo is None else value.astimezone(dt.UTC)


def _quoted(path: str) -> str:
    """Quote an object key or a path for a message, its control characters escaped.

    Parameters
    ----------
    path
        The key or path, as the source or the asset gave it.

    Returns
    -------
    str
        ``'<path>'``.
    """
    return f"'{safe_name(path)}'"


def _url_without_secrets(match: re.Match[str]) -> str:
    """Keep a URL's scheme, host, port and path; drop its user, password, query and fragment.

    Parameters
    ----------
    match
        The URL.

    Returns
    -------
    str
        The URL without what could carry a credential (``?…`` marks a dropped query).
    """
    try:
        parts = urlsplit(match.group())
        port = f":{parts.port}" if parts.port is not None else ""
        host = parts.hostname or ""
    except ValueError:
        return match.group().split("://", 1)[0] + "://…"
    query = "?…" if parts.query else ""
    return f"{parts.scheme}://{host}{port}{parts.path}{query}"


def _scrubbed(text: str) -> str:
    """Remove every credential, signature and token a library's message may carry.

    The task's secrets (the connection's values, the egress token, derived values the helpers
    registered) become ``***``; URLs lose their user, password and query (a SAS token, a
    presigned signature); the signature elements of an S3 error body and an ``Authorization``
    header are cut.

    Parameters
    ----------
    text
        The message.

    Returns
    -------
    str
        The message without them.
    """
    for form in sorted(context.current().secrets, key=len, reverse=True):
        text = text.replace(form, "***")
    text = _URL.sub(_url_without_secrets, text)
    text = _SIGNED_ELEMENT.sub(lambda m: f"<{m.group(1)}>***</{m.group(1)}>", text)
    text = _SIGNED_PARAMETER.sub(lambda m: f"{m.group(1)}=***", text)
    return _AUTH_HEADER.sub(lambda m: f"{m.group(1)}: ***", text)


def _problem(error: BaseException | str) -> tuple[str, bool]:
    """Say what a library or a server reported, safely, and whether it is about a certificate.

    obstore puts its summary on the first line, the store's XML error body after it and a
    debug dump of the cause (where a certificate problem is named) at the end; the first line
    is kept, with the store's error code and message and the certificate problem added.

    Parameters
    ----------
    error
        What obstore or httpx raised, or a text such as ``HTTP 403 for a.csv``.

    Returns
    -------
    tuple of (str, bool)
        The text, scrubbed, its control characters escaped, at most 500 characters; and
        whether the message names a certificate anywhere.
    """
    text = _scrubbed(str(error)).strip()
    summary = text.split("\n\nDebug source:", 1)[0]
    lines = summary.splitlines()
    parts = [_XML_HEAD.sub("", lines[0].strip()) if lines else type(error).__name__]
    code, said = _ERROR_CODE.search(summary), _ERROR_MESSAGE.search(summary)
    if code:
        parts.append(code.group(1) + (f": {said.group(1).strip()}" if said else ""))
    certificate = _CERTIFICATE.search(text)
    if certificate and not _CERTIFICATE.search(parts[0]):
        cause = certificate.group().strip()
        if cause.startswith("InvalidCertificate"):  # obstore's debug dump spreads it over lines
            cause = re.sub(r"[\s,]", "", cause)
        if cause.lower() != "certificate":
            parts.append(cause)
    return safe_name("; ".join(parts))[:_PROBLEM_CHARS], certificate is not None


def _refuse(
    name: str, environment: str, error: BaseException | str, *, credentials: bool = False
) -> NoReturn:
    """Raise a store's or a server's refusal as the error that names the next step.

    Parameters
    ----------
    name
        The connection.
    environment
        The task's environment, for the command.
    error
        What the store or server raised or said.
    credentials
        Whether the store or server refused the credential (an authentication or permission
        error, HTTP 401 or 403).

    Raises
    ------
    SourceRefused
        For a certificate or a credential (not retried).
    SourceError
        Otherwise (retried: a network failure or a store's own error may pass).
    """
    problem, certificate = _problem(error)
    if certificate:
        raise SourceRefused(
            f"`{name}`: the server's certificate is not trusted ({problem}). Add your CA to the "
            "workers' trust bundle (LAKANA_RUN__CA_BUNDLE); for an http connection, give the "
            f"connection your CA: `lakana connection update {name} --env {environment} "
            "--ca-file ca.pem`."
        ) from None
    if credentials:
        raise SourceRefused(
            f"`{name}`: authentication failed (the store said: {problem}). Check the secret with "
            f"`lakana connection test {name} --env {environment}`."
        ) from None
    raise SourceError(f"`{name}`: {problem}") from None


def _denied(error: BaseException) -> bool:
    """Say whether an obstore error is a refused credential.

    obstore raises its authentication and permission classes for some requests only; a list
    request that the store answers with 401 or 403 is a generic error that names the status.

    Parameters
    ----------
    error
        What obstore raised.

    Returns
    -------
    bool
        True for an authentication or a permission error, or a store's 401 or 403 answer.
    """
    import obstore

    return isinstance(
        error,
        obstore.exceptions.PermissionDeniedError | obstore.exceptions.UnauthenticatedError,
    ) or bool(_REFUSED_STATUS.search(str(error).split("\n\nDebug source:", 1)[0]))


@dataclass(frozen=True)
class _ObjectSource:
    """An ``s3``, ``gcs`` or ``azure`` connection: objects below its prefix.

    Parameters
    ----------
    store
        The connection's store (`_stores.store_for`), whose paths are below the prefix.
    name
        The connection, for messages.
    environment
        The task's environment, for messages.
    """

    store: S3Store | GCSStore | AzureStore
    name: str
    environment: str

    def list(self, pattern: str, max_files: int) -> _directory.Listing:
        """List the objects whose keys below the prefix match the pattern.

        Parameters
        ----------
        pattern
            The glob; its leading segments without a wildcard narrow the listing.
        max_files
            The cap.

        Returns
        -------
        Listing
            The matching objects, sorted by key; a matching key with an empty, ``.`` or ``..``
            segment is skipped (no file system or URL would read it as the same object).

        Raises
        ------
        CapExceeded
            For more matches than `max_files`.
        DirectoryError
            For a refused pattern, or more than `MAX_ENTRIES` objects listed.
        SourceError
            When the store fails (`SourceRefused` for a refused credential or certificate).
        """
        import obstore

        wanted = _directory.pattern_segments(pattern)
        literal: builtins.list[str] = []
        for part in wanted[:-1]:
            if _GLOB & set(part):
                break
            literal.append(part)
        listing, seen = _directory.Listing(), 0
        try:
            for batch in obstore.list(self.store, "/".join(literal) or None, chunk_size=1000):
                for meta in batch:
                    seen += 1
                    if seen > _directory.MAX_ENTRIES:
                        raise _directory.DirectoryError(
                            f"the listing passed {_directory.MAX_ENTRIES} objects; narrow the "
                            "pattern"
                        )
                    key = meta["path"].split("/")
                    if not _directory.matches(wanted, key):
                        continue
                    if any(part in ("", ".", "..") for part in key):
                        listing.skipped.append(meta["path"])
                        continue
                    if len(listing.files) == max_files:
                        raise CapExceeded(
                            f"the pattern {pattern!r} matches more than max_files ({max_files}) "
                            "files; narrow the pattern or pass since="
                        )
                    listing.files.append(
                        _directory.FileEntry(
                            meta["path"], meta["size"], _utc(meta["last_modified"])
                        )
                    )
        except (obstore.exceptions.BaseError, FileNotFoundError, ValueError) as exc:
            _refuse(self.name, self.environment, exc, credentials=_denied(exc))
        listing.files.sort(key=lambda entry: entry.path)
        return listing

    def read(self, path: str, max_bytes: int) -> bytes:
        """Read one object, stopping at the cap.

        Parameters
        ----------
        path
            Its key below the prefix.
        max_bytes
            The cap.

        Returns
        -------
        bytes
            The content.

        Raises
        ------
        CapExceeded
            When it is larger than `max_bytes`.
        SourceError
            When the store fails (`SourceRefused` for a refused credential or certificate).
        """
        import obstore

        try:
            result = obstore.get(self.store, path)
            size = result.meta["size"]
            if size > max_bytes:
                raise CapExceeded(
                    f"{_quoted(path)} is {size} bytes, more than max_file_bytes ({max_bytes})"
                )
            chunks, total = [], 0
            for chunk in result.stream():
                total += len(chunk)
                if total > max_bytes:
                    raise CapExceeded(
                        f"{_quoted(path)} grew past max_file_bytes ({max_bytes}) while it was read"
                    )
                chunks.append(bytes(chunk))
        except (obstore.exceptions.BaseError, FileNotFoundError, ValueError) as exc:
            _refuse(self.name, self.environment, exc, credentials=_denied(exc))
        return b"".join(chunks)

    def close(self) -> None:
        """Hold nothing to release: obstore keeps its connection pool with the store."""


@dataclass(frozen=True)
class _HttpSource:
    """An ``http`` connection: one file at a path below its base URL.

    Parameters
    ----------
    client
        The connection's client (`_http.client_for`).
    name
        The connection, for messages.
    environment
        The task's environment, for messages.
    """

    client: httpx.Client
    name: str
    environment: str

    def _url(self, path: str) -> str:
        """Join a checked path to the base URL, each segment percent-encoded.

        Encoding every segment keeps a path that looks like a URL (``https://other/x``) or a
        network path a path below the base URL: the request never goes to another host.

        Parameters
        ----------
        path
            A path from `_path`.

        Returns
        -------
        str
            The absolute URL.
        """
        encoded = "/".join(quote(part, safe="") for part in path.split("/"))
        return f"{str(self.client.base_url).rstrip('/')}/{encoded}"

    def _path(self, pattern: str) -> str:
        """Check a path below the base URL: one file, no wildcard, no escape.

        Parameters
        ----------
        pattern
            What the asset passed.

        Returns
        -------
        str
            The path, relative to the base URL.

        Raises
        ------
        UsageError
            For a wildcard, a query, a fragment or a percent sign.
        DirectoryError
            For an empty path, ``..``, an absolute path or a NUL.
        """
        parts = _directory.pattern_segments(pattern)
        if set(pattern) & (_GLOB | {"#", "%"}):
            raise UsageError(
                "files.read reads one file of an http connection: pass its path relative to the "
                f"base URL, without wildcards, query or encoding (got {_quoted(pattern)})"
            )
        return "/".join(parts)

    def _check(self, response: httpx.Response, path: str) -> None:
        """Turn an answer other than 200 into the error that names it.

        Parameters
        ----------
        response
            The answer.
        path
            The path asked for.

        Raises
        ------
        SourceRefused
            For 401 and 403, and for a redirect.
        SourceError
            For any other status but 200.
        """
        if response.status_code == 200:
            return
        shown = safe_name(path)
        if response.status_code in (401, 403):
            _refuse(
                self.name,
                self.environment,
                f"HTTP {response.status_code} for {shown}",
                credentials=True,
            )
        if 300 <= response.status_code < 400:
            raise SourceRefused(
                f"`{self.name}`: {shown} answers with a redirect (HTTP {response.status_code}), "
                "which files.read does not follow; use the address it names as the connection's "
                "base URL"
            )
        raise SourceError(f"`{self.name}`: HTTP {response.status_code} for {shown}")

    def list(self, pattern: str, max_files: int) -> _directory.Listing:
        """Describe the one file with a ``HEAD`` request.

        Parameters
        ----------
        pattern
            The file's path.
        max_files
            Unused: one file at most.

        Returns
        -------
        Listing
            The file, with ``Content-Length`` (0 without one) and ``Last-Modified`` (`None`
            without one).

        Raises
        ------
        SourceError
            When the request fails or the answer is not 200 (`SourceRefused` for a refused
            credential, a certificate or a redirect).
        """
        import email.utils

        import httpx

        path = self._path(pattern)
        try:
            response = self.client.head(self._url(path))
        except httpx.HTTPError as exc:
            _refuse(self.name, self.environment, exc)
        self._check(response, path)
        stamp = response.headers.get("last-modified")
        try:
            modified = _utc(email.utils.parsedate_to_datetime(stamp)) if stamp else None
        except (TypeError, ValueError):
            modified = None
        length = response.headers.get("content-length", "")
        size = int(length) if length.isdigit() else 0
        return _directory.Listing(files=[_directory.FileEntry(path, size, modified)])

    def read(self, path: str, max_bytes: int) -> bytes:
        """Download the file, stopping at the cap.

        Parameters
        ----------
        path
            The file's path.
        max_bytes
            The cap.

        Returns
        -------
        bytes
            The content.

        Raises
        ------
        CapExceeded
            When it is larger than `max_bytes`.
        SourceError
            When the request fails or the answer is not 200.
        """
        import httpx

        try:
            with self.client.stream("GET", self._url(path)) as response:
                self._check(response, path)
                chunks, total = [], 0
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        raise CapExceeded(
                            f"{_quoted(path)} is more than max_file_bytes ({max_bytes})"
                        )
                    chunks.append(chunk)
        except httpx.HTTPError as exc:
            _refuse(self.name, self.environment, exc)
        return b"".join(chunks)

    def close(self) -> None:
        """Close the client and its connections."""
        self.client.close()


def _source(conn: FetchedConnection) -> _Source:
    """Choose the source of a connection's kind.

    Parameters
    ----------
    conn
        The fetched connection with its routing.

    Returns
    -------
    _Source
        Its source.

    Raises
    ------
    UsageError
        For a kind `read` does not read, naming the helper that does, a ``directory``
        connection the task description has no root for, or a store or client that cannot be
        built (`_stores.store_for`, `_http.client_for`).
    """
    if conn.kind not in KINDS:
        kinds = ", ".join(KINDS[:-1]) + " and " + KINDS[-1]
        helper = _OTHER_HELPER.get(conn.kind)
        hint = f": use {helper}" if helper else ""
        raise UsageError(
            f"files.read reads {kinds} connections; `{conn.name}` is {conn.kind}{hint}"
        )
    environment = context.current().environment
    if conn.kind == "http":
        return _HttpSource(_http.client_for(connections.get(conn.name)), conn.name, environment)
    if conn.kind != "directory":
        store = _stores.store_for(connections.get(conn.name))
        return _ObjectSource(store, conn.name, environment)
    root_dir, base = conn.routing.get("root_dir"), conn.routing.get("path")
    if not isinstance(root_dir, str) or not isinstance(base, str):
        raise UsageError(
            f"the task description has no ingestion root for `{conn.name}`; check that the "
            "worker runs the server's version of Lakana"
        )
    return _DirectorySource(root_dir, base)


@contextlib.contextmanager
def _naming(conn: FetchedConnection) -> Generator[None]:
    """Name the connection in a source's error, and give a missing root the fixed root text.

    Parameters
    ----------
    conn
        The connection.

    Yields
    ------
    None
        While the source is used.

    Raises
    ------
    RootUnavailable
        With the fixed root text, for a root this worker does not have.
    DirectoryError
        The source's own class (`DirectoryIOError` too), prefixed with the connection's name.
    CapExceeded
        The source's own, prefixed with the connection's name.
    """
    try:
        yield
    except _directory.RootUnavailable:
        root = str(conn.routing.get("root", conn.address.get("root", "")))
        raise _directory.RootUnavailable(
            _check.MISSING_ROOT.format(connection=conn.name, root=root)
        ) from None
    except (_directory.DirectoryError, CapExceeded) as exc:
        # The same class, so its `retryable` survives (an I/O error may pass; a missing path not).
        raise type(exc)(f"`{conn.name}`: {exc}") from None


def _positive(**caps: object) -> None:
    """Refuse a cap that is not a positive whole number.

    Parameters
    ----------
    **caps
        Argument name to the value the asset passed.

    Raises
    ------
    UsageError
        Naming the argument.
    """
    for argument, value in caps.items():
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise UsageError(f"{argument}= must be a positive whole number, not {value!r}")


def _as_utc(since: object) -> dt.datetime | None:
    """Read ``since=``: `None`, or a datetime (a naive one is UTC).

    Parameters
    ----------
    since
        The value.

    Returns
    -------
    datetime or None
        UTC-aware, or `None`.

    Raises
    ------
    UsageError
        For anything else.
    """
    if since is None:
        return None
    if not isinstance(since, dt.datetime):
        raise UsageError(
            f"since= takes a datetime or None, not {type(since).__name__}; pass "
            "ctx.watermark() of an asset with watermark='_source_modified'"
        )
    return since.replace(tzinfo=dt.UTC) if since.tzinfo is None else since.astimezone(dt.UTC)


def _name_time(path: str, pattern: str) -> dt.datetime | None:
    """Read the date or time in a file's name, as UTC.

    Parameters
    ----------
    path
        The file's path; only its name (after the last ``/``) is read.
    pattern
        A `datetime.strptime` pattern, such as ``export-%Y-%m-%d.csv``.

    Returns
    -------
    datetime or None
        The time (midnight for a date), UTC; `None` when the name does not fit.
    """
    try:
        found = dt.datetime.strptime(path.rsplit("/", 1)[-1], pattern)
    except ValueError:
        return None
    return found.replace(tzinfo=dt.UTC) if found.tzinfo is None else found.astimezone(dt.UTC)


def _empty(schema: Mapping[str, Any] | None, with_since: bool) -> pa.Table:
    """Build the table of a read that found no file to read.

    Parameters
    ----------
    schema
        ``schema=``, whose columns the table has; `None` for no columns.
    with_since
        Whether ``since=`` was passed (adds ``_source_modified``).

    Returns
    -------
    pyarrow.Table
        Zero rows.
    """
    import polars as pl

    columns: dict[str, Any] = dict(schema) if schema else {}
    if with_since and columns:
        columns[SOURCE_MODIFIED] = pl.Datetime("us", "UTC")
    return pl.DataFrame(schema=columns).to_arrow()


def read(
    connection: str,
    pattern: str,
    *,
    format: str | None = None,
    sheet: str | None = None,
    header_row: int = 1,
    schema: Mapping[str, Any] | None = None,
    since: dt.datetime | _Unset | None = UNSET,
    modified_from: str = "mtime",
    max_file_bytes: int = DEFAULT_MAX_BYTES,
    max_total_bytes: int = DEFAULT_MAX_BYTES,
    max_files: int = 10_000,
    max_rows: int = DEFAULT_MAX_ROWS,
) -> pa.Table:
    """Read the files of a connection that match a pattern into one table.

    Parameters
    ----------
    connection
        A connection the asset declares, of kind ``s3``, ``gcs``, ``azure``, ``http`` or
        ``directory``.
    pattern
        A glob relative to the connection's path or prefix: ``*``, ``?`` and ``[...]`` within a
        name, ``**`` for any number of directories, such as ``"orders/*.csv"``. ``*`` matches
        names that start with a dot, matching is case-sensitive, and a hard link is read as a
        file. For ``http``, one path relative to the base URL, without wildcards.
    format
        ``csv``, ``parquet``, ``json``, ``ndjson`` (or ``jsonl``), ``xlsx``, ``xlsm``,
        ``xlsb``, ``xls`` or ``ods``; from each file's extension when `None`.
    sheet
        A spreadsheet's sheet by name; the first sheet when `None`.
    header_row
        The 1-based row of the column names in a CSV file or a sheet; ``0`` for none.
    schema
        Column name to polars type, such as ``{"id": pl.Int64}``; other columns are inferred.
    since
        Read only the files modified after this time (strictly after; a naive time is UTC),
        and add the column ``_source_modified``. `None` reads every file and adds the column
        (the first run of an asset with ``watermark="_source_modified"``). Left out: every
        file, no column.
    modified_from
        ``"mtime"`` (the file's modification time), or a `datetime.strptime` pattern for a
        date in the file's name, such as ``"export-%Y-%m-%d.csv"`` (midnight UTC); a name that
        does not fit is skipped with a line on standard error. Only with ``since=``.
    max_file_bytes
        The largest file read.
    max_total_bytes
        The most bytes read in all.
    max_files
        The most files read.
    max_rows
        The most rows read in all; each reader stops after it, so it also bounds what a
        compressed file (Parquet, a workbook) is decoded to.

    Returns
    -------
    pyarrow.Table
        Every file's rows; a column a file lacks is null in its rows. No file to read gives
        zero rows: with ``schema=`` its columns (and ``_source_modified`` with ``since=``),
        else no columns.

    Raises
    ------
    ConnectionNotDeclared
        For a connection the asset does not declare.
    UsageError
        For a connection of another kind or set up wrong, a bad argument, or a path or pattern
        the walker refuses (`DirectoryError`, not retried).
    CapExceeded
        When a cap is passed; the message names the cap and how to narrow.
    SourceError
        When the source fails (retried); `SourceRefused` when it refuses the credential or the
        certificate or answers with a redirect, `FileShapeError` when a file cannot be parsed or
        does not fit the others (neither retried).
    """
    _positive(
        max_files=max_files,
        max_file_bytes=max_file_bytes,
        max_total_bytes=max_total_bytes,
        max_rows=max_rows,
    )
    _formats.check_schema(schema)
    if modified_from != "mtime" and since is UNSET:
        raise UsageError("modified_from= applies only with since=")
    conn = fetch(connection)
    source = _source(conn)
    try:
        with _naming(conn):
            # The cap counts the files that will be read, so it applies after `since` filters.
            listing = source.list(pattern, _directory.MAX_ENTRIES)
        for skipped in listing.skipped:
            print(
                f"files.read: `{connection}`: skipped {skipped!r} (a symbolic link, not a regular "
                "file, too deep, or a key with an empty, . or .. segment)",
                file=sys.stderr,
            )
        with_since = not isinstance(since, _Unset)
        after = _as_utc(since) if with_since else None
        chosen: builtins.list[tuple[_directory.FileEntry, dt.datetime | None]] = []
        for entry in listing.files:
            modified = entry.modified
            if with_since and modified_from == "mtime" and modified is None:
                raise UsageError(
                    f"`{connection}` does not say when {entry.path!r} was modified; pass "
                    "modified_from= with the date pattern of its name"
                )
            if modified_from != "mtime":
                named = _name_time(entry.path, modified_from)
                if named is None:
                    print(
                        f"files.read: `{connection}`: skipped {entry.path!r}, whose name does not "
                        f"fit modified_from={modified_from!r}",
                        file=sys.stderr,
                    )
                    continue
                modified = named
            if after is None or (modified is not None and modified > after):
                if len(chosen) == max_files:
                    advice = "narrow the pattern" + ("" if with_since else " or pass since=")
                    raise CapExceeded(
                        f"`{connection}`: the pattern {pattern!r} matches more than max_files "
                        f"({max_files}) files to read; {advice}"
                    )
                chosen.append((entry, modified))
        if not chosen:
            return _empty(schema, with_since)
        import polars as pl

        too_many = CapExceeded(
            f"`{connection}`: the files matching {pattern!r} hold more than max_total_bytes "
            f"({max_total_bytes}); narrow the pattern or pass since= to read fewer files"
        )
        if sum(entry.size for entry, _ in chosen) > max_total_bytes:
            raise too_many
        frames, names, total, rows = [], [], 0, 0
        for entry, modified in chosen:
            with _naming(conn):
                data = source.read(entry.path, max_file_bytes)
            name = safe_name(f"{connection}/{entry.path}")
            kind = _formats.format_of(name, format)
            counted = (
                _formats.unpacked_size(data, max_file_bytes) if kind in _formats.ZIPPED else None
            )
            unpacked = None if counted is None else counted[0]
            if counted is not None and counted[1]:
                raise CapExceeded(
                    f"`{name}` ({len(data)} bytes as a file) unpacks to more than max_file_bytes "
                    f"({max_file_bytes}) bytes; split the workbook or pass a larger max_file_bytes"
                )
            total += max(len(data), unpacked or 0)
            if total > max_total_bytes:
                if unpacked is not None and unpacked > len(data):
                    raise CapExceeded(
                        f"`{name}` unpacks to {unpacked} bytes ({len(data)} bytes as a file); "
                        "with the files before it that is more than max_total_bytes "
                        f"({max_total_bytes}); narrow the pattern or pass since= to read fewer "
                        "files"
                    )
                raise too_many
            frame = _formats.parse(
                data,
                name=name,
                format=kind,
                sheet=sheet,
                header_row=header_row,
                schema=schema,
                max_rows=max_rows - rows + 1,
            )
            rows += frame.height
            if rows > max_rows:
                advice = "narrow the pattern" + (
                    " or" if with_since else ", pass since= to read fewer files, or"
                )
                raise CapExceeded(
                    f"`{connection}`: the files matching {pattern!r} hold more than max_rows "
                    f"({max_rows}) rows; {advice} pass a larger max_rows"
                )
            if with_since:
                frame = frame.with_columns(
                    pl.lit(modified, dtype=pl.Datetime("us", "UTC")).alias(SOURCE_MODIFIED)
                )
            frames.append(frame)
            names.append(name)
        return _formats.combine(frames, names).to_arrow()
    finally:
        source.close()


def list(connection: str, pattern: str, *, max_files: int = 10_000) -> pa.Table:
    """List the files of a connection that match a pattern, without reading them.

    Parameters
    ----------
    connection
        A connection the asset declares, of a kind `read` reads.
    pattern
        A glob, as for `read` (for ``http``, one path).
    max_files
        The most files listed.

    Returns
    -------
    pyarrow.Table
        ``name`` (the path relative to the connection's path or prefix), ``size`` (int64,
        bytes; 0 for an HTTPS file without ``Content-Length``) and ``modified`` (timestamp,
        UTC; null for an HTTPS file without ``Last-Modified``), sorted by name.

    Raises
    ------
    UsageError
        As `read`.
    CapExceeded
        For more matches than `max_files`.
    SourceError
        As `read`.
    """
    import pyarrow as pa

    _positive(max_files=max_files)
    conn = fetch(connection)
    source = _source(conn)
    try:
        with _naming(conn):
            listing = source.list(pattern, max_files)
    finally:
        source.close()
    return pa.table(
        {
            "name": pa.array([f.path for f in listing.files], pa.string()),
            "size": pa.array([f.size for f in listing.files], pa.int64()),
            "modified": pa.array([f.modified for f in listing.files], pa.timestamp("us", tz="UTC")),
        }
    )
