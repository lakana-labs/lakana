"""`lakana.ingest.files`: flat files from a ``directory`` connection, inside a task."""

from __future__ import annotations

import datetime as dt
import io
import os
import zipfile
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

import polars as pl
import pyarrow as pa
import pytest

from lakana import context
from lakana.ingest import CapExceeded, IngestError, SourceError, _connections, files
from lakana.ingest._directory import DirectoryError, DirectoryIOError, RootUnavailable

_UTC = dt.UTC


class _TaskConnections:
    """A fake `client.task_connections` answering from a dict of bodies.

    Parameters
    ----------
    bodies
        Name to the server's answer.
    """

    def __init__(self, bodies: dict[str, dict[str, object]]) -> None:
        self.bodies = bodies

    def get(self, name: str) -> dict[str, object]:
        """Answer one fetch.

        Parameters
        ----------
        name
            The connection's name.

        Returns
        -------
        dict
            Its body.
        """
        return self.bodies[name]


@dataclass(frozen=True)
class _Client:
    """A fake SDK client with only `task_connections`.

    Parameters
    ----------
    task_connections
        The fake resource.
    """

    task_connections: _TaskConnections


def _body(name: str, kind: str, address: Mapping[str, object]) -> dict[str, object]:
    """Build a fetch answer without a secret.

    Parameters
    ----------
    name
        The name.
    kind
        The kind.
    address
        The address.

    Returns
    -------
    dict
        The body.
    """
    return {
        "name": name,
        "kind": kind,
        "address": address,
        "settings": {},
        "endpoints": [],
        "secret": None,
    }


@pytest.fixture
def root(tmp_path: Path) -> Iterator[Path]:
    """Set the state of a task that declares `exports` (a directory) and `crm-db`.

    `exports` is root `shared`, path `in`, at ``tmp_path / "shared"``.

    Parameters
    ----------
    tmp_path
        pytest's directory.

    Yields
    ------
    Path
        The connection's directory, ``<root>/in``.
    """
    directory = tmp_path / "shared" / "in"
    directory.mkdir(parents=True)
    address = {"root": "shared", "path": "in"}
    bodies = {
        "exports": _body("exports", "directory", address),
        "crm-db": {
            **_body("crm-db", "postgresql", {"host": "db", "port": 5432, "database": "c"}),
            "endpoints": [{"host": "db", "port": 5432, "role": "service"}],
        },
    }
    routing = {
        "exports": {
            "kind": "directory",
            "root": "shared",
            "root_dir": str(tmp_path / "shared"),
            "path": "in",
            "root_path": str(directory),
        },
        "crm-db": {"kind": "postgresql", "grant": [["db", 5432]]},
    }
    context._set(
        context._TaskState(
            client=_Client(_TaskConnections(bodies)),  # ty: ignore[invalid-argument-type]
            environment="dev",
            asset="bronze.orders",
            declared=frozenset(bodies),
            connections=routing,
        )
    )
    yield directory
    context._set(None)
    _connections.clear()


def _write(path: Path, data: bytes, modified: dt.datetime | None = None) -> None:
    """Write a file and set its modification time.

    Parameters
    ----------
    path
        Where.
    data
        Its content.
    modified
        Its modification time; now when `None`.
    """
    path.write_bytes(data)
    if modified is not None:
        stamp = (modified - dt.datetime(1970, 1, 1, tzinfo=dt.UTC)) // dt.timedelta(microseconds=1)
        os.utime(path, ns=(stamp * 1000, stamp * 1000))


def _xlsx(frame: pl.DataFrame, sheet: str, position: str = "A1") -> bytes:
    """Write a frame as a workbook.

    Parameters
    ----------
    frame
        The rows.
    sheet
        The sheet's name.
    position
        The cell of the header row's first column.

    Returns
    -------
    bytes
        The workbook.
    """
    buffer = io.BytesIO()
    frame.write_excel(buffer, worksheet=sheet, position=position)
    return buffer.getvalue()


def _ods(rows: list[list[str]]) -> bytes:
    """Write rows as the smallest OpenDocument spreadsheet calamine reads.

    Parameters
    ----------
    rows
        Cells as text.

    Returns
    -------
    bytes
        The document.
    """
    cells = "".join(
        "<table:table-row>"
        + "".join(
            f'<table:table-cell office:value-type="string"><text:p>{v}</text:p></table:table-cell>'
            for v in row
        )
        + "</table:table-row>"
        for row in rows
    )
    content = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
        'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
        'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" office:version="1.2">'
        f'<office:body><office:spreadsheet><table:table table:name="S1">{cells}'
        "</table:table></office:spreadsheet></office:body></office:document-content>"
    )
    manifest = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0">'
        '<manifest:file-entry manifest:full-path="/" '
        'manifest:media-type="application/vnd.oasis.opendocument.spreadsheet"/>'
        '<manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml"/>'
        "</manifest:manifest>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "mimetype", "application/vnd.oasis.opendocument.spreadsheet", zipfile.ZIP_STORED
        )
        archive.writestr("META-INF/manifest.xml", manifest)
        archive.writestr("content.xml", content)
    return buffer.getvalue()


def test_each_text_format_reads(root: Path) -> None:
    """CSV, Parquet, JSON and NDJSON (also as .jsonl) each read into the same rows."""
    expected = pl.DataFrame({"id": [1, 2], "name": ["a", "b"]})
    buffer = io.BytesIO()
    expected.write_parquet(buffer)
    _write(root / "a.csv", b"id,name\n1,a\n2,b\n")
    _write(root / "a.parquet", buffer.getvalue())
    _write(root / "a.json", b'[{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]')
    _write(root / "a.ndjson", b'{"id": 1, "name": "a"}\n{"id": 2, "name": "b"}\n')
    _write(root / "a.jsonl", b'{"id": 1, "name": "a"}\n{"id": 2, "name": "b"}\n')

    for name in ("a.csv", "a.parquet", "a.json", "a.ndjson", "a.jsonl"):
        table = files.read("exports", name)
        assert isinstance(table, pa.Table)
        assert table.to_pylist() == expected.to_dicts(), name
        assert table.schema == expected.to_arrow().schema, name


def test_a_sheet_and_a_header_row_are_picked(root: Path) -> None:
    """`sheet="Q3"` picks the sheet; `header_row=3` reads the names from the third row."""
    frame = pl.DataFrame({"id": [1, 2], "name": ["a", "b"]})
    _write(root / "book.xlsx", _xlsx(frame, "Q3", position="A3"))
    _write(root / "book.ods", _ods([["id", "name"], ["1", "a"]]))

    table = files.read("exports", "book.xlsx", sheet="Q3", header_row=3)
    sheet = files.read("exports", "book.ods")

    assert table.to_pylist() == frame.to_dicts()
    assert sheet.column_names == ["id", "name"]
    assert sheet.num_rows == 1


def test_a_sheet_that_does_not_exist_names_the_file(root: Path) -> None:
    """A missing sheet is the file's problem, named with the file."""
    _write(root / "book.xlsx", _xlsx(pl.DataFrame({"id": [1]}), "Q3"))

    with pytest.raises(SourceError, match=r"`exports/book\.xlsx`: .*'Q4'"):
        files.read("exports", "book.xlsx", sheet="Q4")


def test_a_bad_value_names_the_file_the_column_and_the_problem(root: Path) -> None:
    """A value that does not fit `schema=` names file, column and value; not polars' advice."""
    _write(root / "a.csv", b"id,v\n1,2\n2,3\n3,abc\n")

    with pytest.raises(SourceError) as caught:
        files.read("exports", "a.csv", schema={"v": pl.Int64})

    message = str(caught.value)
    assert message.startswith("`exports/a.csv`: ")
    assert "`abc`" in message
    assert "'v'" in message
    assert "pass schema= with the column's type" in message
    assert "ignore_errors" not in message


def test_a_column_schema_names_must_be_in_the_file(root: Path) -> None:
    """A column `schema=` names that the file lacks is refused, naming both."""
    _write(root / "a.csv", b"id\n1\n")

    with pytest.raises(SourceError, match=r"`exports/a\.csv` has no column 'when'"):
        files.read("exports", "a.csv", schema={"when": pl.Datetime})


def test_an_unknown_extension_asks_for_format(root: Path) -> None:
    """Without `format=`, an extension that is no format is refused, listing them."""
    _write(root / "a.dat", b"id\n1\n")

    with pytest.raises(IngestError) as caught:
        files.read("exports", "a.dat")
    assert str(caught.value) == (
        "cannot tell the format of `exports/a.dat`; pass format= (csv, parquet, json, ndjson, "
        "xlsx, xlsm, xlsb, xls, ods)"
    )
    assert files.read("exports", "a.dat", format="csv").num_rows == 1


def test_sheet_and_header_row_are_refused_where_they_mean_nothing(root: Path) -> None:
    """`sheet=` on a CSV file and `header_row=` on JSON are mistakes, not ignored."""
    _write(root / "a.csv", b"id\n1\n")
    _write(root / "a.json", b'[{"id": 1}]')

    with pytest.raises(IngestError, match="sheet= applies to spreadsheets"):
        files.read("exports", "a.csv", sheet="Q3")
    with pytest.raises(IngestError, match="header_row= applies to CSV files and spreadsheets"):
        files.read("exports", "a.json", header_row=2)


def test_several_files_combine_and_fill_missing_columns(root: Path) -> None:
    """Files combine by column name; a column one file lacks is null in its rows."""
    _write(root / "a.csv", b"id,name\n1,a\n")
    _write(root / "b.csv", b"id,amount\n2,1.5\n")

    table = files.read("exports", "*.csv")

    assert table.to_pylist() == [
        {"id": 1, "name": "a", "amount": None},
        {"id": 2, "name": None, "amount": 1.5},
    ]


def test_a_column_that_does_not_fit_names_the_file_and_the_column(root: Path) -> None:
    """Types with no common type in two files: the later file and the column are named."""
    _write(root / "a.ndjson", b'{"id": 1, "tags": 1}\n')
    _write(root / "b.ndjson", b'{"id": 2, "tags": [1, 2]}\n')

    with pytest.raises(
        SourceError,
        match=r"`exports/b\.ndjson`: column 'tags' is List\(Int64\), which does not fit Int64",
    ):
        files.read("exports", "*.ndjson")


def test_since_none_reads_every_file_and_adds_source_modified(root: Path) -> None:
    """`since=None` (a first run) reads all files and adds each file's time, UTC."""
    first = dt.datetime(2026, 10, 1, tzinfo=_UTC)
    _write(root / "a.csv", b"id\n1\n", first)
    _write(root / "b.csv", b"id\n2\n", first + dt.timedelta(days=1))

    table = files.read("exports", "*.csv", since=None)

    assert table.schema.field("_source_modified").type == pa.timestamp("us", tz="UTC")
    assert table.column("_source_modified").to_pylist() == [first, first + dt.timedelta(days=1)]


def test_since_reads_only_files_modified_strictly_after_it(root: Path) -> None:
    """A file modified at the watermark is not read again; a naive `since` is UTC."""
    mark = dt.datetime(2026, 10, 1, 12, tzinfo=_UTC)
    _write(root / "old.csv", b"id\n1\n", mark - dt.timedelta(seconds=1))
    _write(root / "same.csv", b"id\n2\n", mark)
    _write(root / "new.csv", b"id\n3\n", mark + dt.timedelta(microseconds=1))

    aware = files.read("exports", "*.csv", since=mark)
    naive = files.read("exports", "*.csv", since=mark.replace(tzinfo=None))

    assert aware.column("id").to_pylist() == naive.column("id").to_pylist() == [3]


def test_since_from_a_date_in_the_name(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """`modified_from` reads the name's date (midnight UTC); a name that does not fit is skipped."""
    _write(root / "export-2026-10-01.csv", b"id\n1\n")
    _write(root / "export-2026-10-02.csv", b"id\n2\n")
    _write(root / "export-latest.csv", b"id\n3\n")

    table = files.read(
        "exports",
        "export-*.csv",
        since=dt.datetime(2026, 10, 1, tzinfo=_UTC),
        modified_from="export-%Y-%m-%d.csv",
    )

    assert table.column("id").to_pylist() == [2]
    assert table.column("_source_modified").to_pylist() == [dt.datetime(2026, 10, 2, tzinfo=_UTC)]
    assert "skipped 'export-latest.csv', whose name does not fit" in capsys.readouterr().err


def test_modified_from_without_since_is_refused(root: Path) -> None:
    """A name pattern only means something with `since=`."""
    with pytest.raises(IngestError, match="modified_from= applies only with since="):
        files.read("exports", "*.csv", modified_from="export-%Y-%m-%d.csv")


def test_since_takes_a_datetime_or_none(root: Path) -> None:
    """A date or a number as `since=` names what to pass instead."""
    with pytest.raises(IngestError, match="since= takes a datetime or None, not date"):
        files.read("exports", "*.csv", since=dt.date(2026, 10, 1))  # ty: ignore[invalid-argument-type]


def test_files_read_with_no_new_file_returns_an_empty_table(root: Path) -> None:
    """Review Focus 2: nothing new → zero rows; `schema=` columns plus `_source_modified`."""
    _write(root / "a.csv", b"id\n1\n", dt.datetime(2026, 10, 1, tzinfo=_UTC))
    later = dt.datetime(2026, 10, 2, tzinfo=_UTC)

    bare = files.read("exports", "*.csv", since=later)
    typed = files.read("exports", "*.csv", since=later, schema={"id": pl.Int64})
    nothing = files.read("exports", "*.parquet")

    assert (bare.num_rows, bare.num_columns) == (0, 0)
    assert nothing.num_columns == 0
    assert typed.num_rows == 0
    assert typed.schema == pa.schema(
        [("id", pa.int64()), ("_source_modified", pa.timestamp("us", tz="UTC"))]
    )


def test_one_file_over_max_file_bytes_names_the_cap(root: Path) -> None:
    """A file over `max_file_bytes` stops the read, naming the connection, file and cap."""
    _write(root / "a.csv", b"id\n1\n2\n")

    with pytest.raises(
        CapExceeded, match=r"`exports`: 'a\.csv' is 7 bytes, more than max_file_bytes \(6\)"
    ):
        files.read("exports", "*.csv", max_file_bytes=6)


def test_the_sum_over_max_total_bytes_names_how_to_narrow(root: Path) -> None:
    """Files that together pass `max_total_bytes` stop the read, naming pattern and `since`."""
    _write(root / "a.csv", b"id\n1\n")
    _write(root / "b.csv", b"id\n2\n")

    with pytest.raises(CapExceeded) as caught:
        files.read("exports", "*.csv", max_total_bytes=9)
    assert str(caught.value) == (
        "`exports`: the files matching '*.csv' hold more than max_total_bytes (9); narrow the "
        "pattern or pass since= to read fewer files"
    )


def test_more_files_than_max_files_names_the_cap(root: Path) -> None:
    """More matches than `max_files` stop the read before any file is read."""
    for name in ("a", "b", "c"):
        _write(root / f"{name}.csv", b"id\n1\n")

    with pytest.raises(
        CapExceeded, match=r"`exports`: the pattern '\*\.csv' matches more than max_files \(2\)"
    ):
        files.read("exports", "*.csv", max_files=2)


def test_a_link_is_skipped_with_a_line_on_stderr(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A symbolic link below the root is not read; standard error names it."""
    _write(root / "a.csv", b"id\n1\n")
    (root / "b.csv").symlink_to(root / "a.csv")

    assert files.read("exports", "*.csv").num_rows == 1
    assert "`exports`: skipped 'b.csv' (a symbolic link" in capsys.readouterr().err


def test_a_refused_pattern_names_the_connection(root: Path) -> None:
    """`..` in a pattern is refused before any open, naming the connection."""
    with pytest.raises(DirectoryError, match=r"^`exports`: the pattern '\.\./x' must be relative"):
        files.read("exports", "../x")


def test_a_missing_root_gives_the_fixed_root_text(root: Path) -> None:
    """A root the worker does not have: the spec's root text, with names filled in."""
    root.rename(root.parent / "elsewhere")
    root.parent.rename(root.parent.parent / "gone")

    with pytest.raises(RootUnavailable) as caught:
        files.read("exports", "*.csv")
    assert str(caught.value) == (
        "`exports` names ingestion root `shared`, which this worker does not have. Set it in "
        "`LAKANA_LAUNCHER__INGESTION_ROOTS` (Docker runner) or `LAKANA_RUN__INGESTION_ROOTS` "
        "(local runner)."
    )


def test_another_kind_names_the_helper_that_reads_it(root: Path) -> None:
    """A database connection is refused, naming `sql.read`."""
    with pytest.raises(IngestError) as caught:
        files.read("crm-db", "*.csv")
    assert str(caught.value) == (
        "files.read reads directory connections; `crm-db` is postgresql: use sql.read"
    )


def test_files_list_returns_name_size_and_modified(root: Path) -> None:
    """`files.list` reads nothing: name, size and modified per matching file."""
    when = dt.datetime(2026, 10, 1, 8, tzinfo=_UTC)
    _write(root / "a.csv", b"id\n1\n", when)
    (root / "sub").mkdir()
    _write(root / "sub" / "b.csv", b"id\n22\n", when)

    table = files.list("exports", "**/*.csv")

    assert table.schema == pa.schema(
        [("name", pa.string()), ("size", pa.int64()), ("modified", pa.timestamp("us", tz="UTC"))]
    )
    assert table.to_pylist() == [
        {"name": "a.csv", "size": 5, "modified": when},
        {"name": "sub/b.csv", "size": 6, "modified": when},
    ]


def test_a_format_that_is_no_format_is_refused_listing_them(root: Path) -> None:
    """`format="xml"` is refused, naming the formats that are read."""
    _write(root / "a.csv", b"id\n1\n")

    with pytest.raises(IngestError, match=r"format='xml' is not a format files\.read reads \(csv,"):
        files.read("exports", "a.csv", format="xml")


def test_a_negative_header_row_is_refused(root: Path) -> None:
    """`header_row` counts from 1, or is 0 for none; below that is a mistake."""
    _write(root / "a.csv", b"id\n1\n")

    with pytest.raises(IngestError, match=r"header_row= is the 1-based row"):
        files.read("exports", "a.csv", header_row=-1)


def test_unset_has_a_readable_repr() -> None:
    """The sentinel prints as `UNSET` in signatures and messages."""
    assert repr(files.UNSET) == "UNSET"


def test_a_task_description_without_a_root_path_names_the_connection(root: Path) -> None:
    """Routing without `root_dir` is a launcher mistake the message names."""
    state = context.current()
    context._set(
        context._TaskState(
            client=state.client,
            environment=state.environment,
            asset=state.asset,
            declared=state.declared,
            connections={"exports": {"kind": "directory", "root": "shared"}},
        )
    )

    with pytest.raises(IngestError, match=r"no ingestion root for `exports`"):
        files.read("exports", "*.csv")


def test_a_source_without_a_time_is_refused_with_since_and_mtime(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A source that does not know a file's time cannot serve `since=` by mtime."""
    from lakana.ingest import _directory

    class _NoTimes:
        def list(self, pattern: str, max_files: int) -> _directory.Listing:
            return _directory.Listing([_directory.FileEntry("a.csv", 3, None)], [])

        def read(self, path: str, max_bytes: int) -> bytes:
            return b"id\n"

    monkeypatch.setattr(files, "_source", lambda conn: _NoTimes())

    with pytest.raises(
        IngestError, match=r"does not say when 'a\.csv' was modified; pass modified_from="
    ):
        files.read("exports", "*.csv", since=None)


def test_a_conflict_in_a_later_file_is_found_after_files_that_fit() -> None:
    """Files that fit are merged on the way; the one that does not is named."""
    from lakana.ingest import _formats

    frames = [
        pl.DataFrame({"x": [1]}),
        pl.DataFrame({"x": [2], "z": ["a"]}),
        pl.DataFrame({"x": [[3, 4]]}),
    ]

    with pytest.raises(
        SourceError, match=r"`c\.csv`: column 'x' is List\(Int64\), which does not fit Int64"
    ):
        _formats.combine(frames, ["a.csv", "b.csv", "c.csv"])


@pytest.mark.parametrize("name", ["a.json", "a.ndjson", "a.parquet", "a.xlsx", "a.csv"])
def test_a_schema_column_the_file_lacks_is_refused_in_every_format(root: Path, name: str) -> None:
    """The promised message, on every format: file and column named."""
    frame = pl.DataFrame({"id": [1]})
    if name.endswith(".json"):
        data = frame.write_json().encode()
    elif name.endswith(".ndjson"):
        data = frame.write_ndjson().encode()
    elif name.endswith(".parquet"):
        buffer = io.BytesIO()
        frame.write_parquet(buffer)
        data = buffer.getvalue()
    elif name.endswith(".xlsx"):
        data = _xlsx(frame, "Sheet1")
    else:
        data = b"id\n1\n"
    _write(root / name, data)

    with pytest.raises(SourceError) as caught:
        files.read("exports", name, schema={"when": pl.Datetime})
    assert str(caught.value) == f"`exports/{name}` has no column 'when', which schema= names"


@pytest.mark.parametrize(
    "order",
    [("date", "int", "dec"), ("date", "dec", "int")],
    ids=["date-int-decimal", "date-decimal-int"],
)
def test_types_that_only_fit_in_one_file_order_are_refused_in_every_order(order: str) -> None:
    """File order never decides whether a column is silently coerced."""
    from lakana.ingest import _formats

    kinds = {
        "date": pl.DataFrame({"x": [dt.date(2020, 1, 1)]}),
        "int": pl.DataFrame({"x": [1]}),
        "dec": pl.DataFrame({"x": [1]}).cast({"x": pl.Decimal(10, 2)}),
    }

    with pytest.raises(SourceError, match=r"column 'x'.*schema=") as caught:
        _formats.combine([kinds[k] for k in order], [f"{k}.csv" for k in order])
    assert "`" in str(caught.value)


def test_files_that_cannot_be_combined_are_named_with_their_types() -> None:
    """When no single pair is to blame the message lists each file's type."""
    from lakana.ingest import _formats

    frames = [
        pl.DataFrame({"x": [dt.date(2020, 1, 1)]}),
        pl.DataFrame({"x": [1]}),
        pl.DataFrame({"x": [1]}).cast({"x": pl.Decimal(10, 2)}),
    ]

    with pytest.raises(SourceError) as caught:
        _formats.combine(frames, ["a.csv", "b.csv", "c.csv"])
    message = str(caught.value)
    assert "`a.csv`" in message
    assert "`c.csv`" in message
    assert "pass schema=" in message


@pytest.mark.parametrize("argument", ["max_files", "max_file_bytes", "max_total_bytes"])
@pytest.mark.parametrize("value", [0, -1, 1.5, True, "10"])
def test_a_cap_must_be_a_positive_whole_number(root: Path, argument: str, value: object) -> None:
    """A zero or negative cap would disable or break the check; it is refused."""
    _write(root / "a.csv", b"id\n1\n")

    with pytest.raises(IngestError, match=rf"{argument}= must be a positive whole number"):
        files.read("exports", "*.csv", **{argument: value})  # ty: ignore[invalid-argument-type]
    with pytest.raises(IngestError, match=r"max_files= must be a positive whole number"):
        files.list("exports", "*.csv", max_files=0)


def test_a_schema_value_that_is_no_polars_type_names_the_column(root: Path) -> None:
    """`schema={"id": "int"}` says what `schema=` takes, naming the column and the value."""
    _write(root / "a.csv", b"id\n1\n")

    with pytest.raises(IngestError) as caught:
        files.read("exports", "a.csv", schema={"id": "int"})
    assert str(caught.value) == (
        "schema= maps a column to a Polars type such as pl.Int64; 'id' has 'int'"
    )
    assert files.read("exports", "a.csv", schema={"id": pl.Int64()}).num_rows == 1


@pytest.mark.parametrize("kind", ["http", "s3", "gcs", "azure"])
def test_a_store_or_web_connection_is_told_to_use_connections_get(
    root: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """Kinds `files.read` does not read yet point to the connection's own client."""
    from types import SimpleNamespace

    monkeypatch.setattr(files, "fetch", lambda name: SimpleNamespace(name=name, kind=kind))

    with pytest.raises(IngestError) as caught:
        files.read("store", "*.csv")
    assert str(caught.value) == (
        f"files.read reads directory connections; `store` is {kind}: "
        "use connections.get with the team's own client"
    )


def test_a_newline_in_a_file_name_cannot_forge_a_line_in_a_message(root: Path) -> None:
    """Control characters in a name are written as escapes in every message."""
    _write(root / "a\n[ERROR] forged.dat", b"id\n1\n")

    with pytest.raises(IngestError) as caught:
        files.read("exports", "*.dat")
    assert "\n" not in str(caught.value)
    assert "a\\x0a[ERROR] forged.dat" in str(caught.value)

    _write(root / "b\nforged.csv", b"id\n1\n1,2,3,\x00\n")
    with pytest.raises(SourceError) as parse_caught:
        files.read("exports", "b*.csv", schema={"zzz": pl.Int64})
    assert "\n" not in str(parse_caught.value)


def test_the_total_cap_is_checked_against_sizes_before_any_file_is_read(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file that would push the sum past the cap is refused before it is read."""
    _write(root / "a.csv", b"id\n1\n")
    _write(root / "b.csv", b"id\n2\n")
    reads: list[str] = []
    real = files._DirectorySource.read

    def counting(self: object, path: str, max_bytes: int) -> bytes:
        reads.append(path)
        return real(self, path, max_bytes)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(files._DirectorySource, "read", counting)

    with pytest.raises(CapExceeded, match="hold more than max_total_bytes"):
        files.read("exports", "*.csv", max_total_bytes=8)
    assert reads == []


def test_a_file_that_cannot_be_read_at_all_keeps_the_readers_message_with_a_schema(
    root: Path,
) -> None:
    """With `schema=`, a file that is no JSON says so; it is not mistaken for a missing column."""
    _write(root / "a.json", b"this is not json")

    with pytest.raises(SourceError, match=r"`exports/a\.json`: .*check the file"):
        files.read("exports", "a.json", schema={"id": pl.Int64})


def test_a_failure_of_the_file_by_file_merge_is_still_a_source_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If polars refuses the merge of two files that pass the column checks, say so plainly."""
    from lakana.ingest import _formats

    real = pl.concat

    def failing(items: object, *, how: str) -> pl.DataFrame:
        if how == "diagonal_relaxed":
            raise pl.exceptions.SchemaError("no common type")
        return real(items, how=how)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(pl, "concat", failing)
    frames = [pl.DataFrame({"x": [1]}), pl.DataFrame({"x": [2]}), pl.DataFrame({"x": [3]})]

    with pytest.raises(
        SourceError, match=r"the files cannot be combined: no common type; pass schema="
    ):
        _formats.combine(frames, ["a.csv", "b.csv", "c.csv"])


def test_a_file_that_grows_after_listing_still_counts_toward_the_total(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The listing's size passes the pre-check; what is really read is counted too."""
    _write(root / "a.csv", b"id\n1\n")
    monkeypatch.setattr(files._DirectorySource, "read", lambda self, path, max_bytes: b"x" * 20)

    with pytest.raises(CapExceeded, match="hold more than max_total_bytes"):
        files.read("exports", "*.csv", max_total_bytes=8)


def test_what_is_wrong_with_the_files_themselves_is_never_retried(root: Path) -> None:
    """A type conflict, a missing schema column, a bad schema value or an unparsable file.

    Each fails every attempt: `FileShapeError`, `retryable = False`, still a `SourceError`.
    """
    from lakana.ingest import FileShapeError, _formats

    assert issubclass(FileShapeError, SourceError)
    assert SourceError.retryable is None
    _write(root / "a.csv", b"id\n1\n")
    _write(root / "bad.json", b"this is not json")
    conflict = [pl.DataFrame({"x": [1]}), pl.DataFrame({"x": [[1]]})]
    cases = [
        lambda: files.read("exports", "a.csv", schema={"when": pl.Datetime}),
        lambda: files.read("exports", "a.csv", schema={"id": "int"}),
        lambda: files.read("exports", "bad.json"),
        lambda: _formats.combine(conflict, ["a", "b"]),
    ]
    for case in cases:
        with pytest.raises(FileShapeError) as caught:
            case()
        assert caught.value.retryable is False


def test_an_io_error_keeps_its_retry_type_through_files_read(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """EIO on a file is `DirectoryIOError` (`retryable = None`), named after the connection."""
    _write(root / "a.csv", b"id\n1\n")
    real = os.open

    def failing(path: str, *args: object, **kwargs: object) -> int:
        if path == "a.csv" and kwargs.get("dir_fd") is not None:
            raise OSError(5, "Input/output error")
        return real(path, *args, **kwargs)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(os, "open", failing)

    with pytest.raises(DirectoryError) as caught:
        files.read("exports", "*.csv")
    assert type(caught.value) is DirectoryIOError
    assert caught.value.retryable is None
    assert str(caught.value).startswith("`exports`: ")


def test_max_files_counts_the_files_that_will_be_read_not_the_listing(root: Path) -> None:
    """Many old files and a few new ones: with `since`, only the new ones count."""
    old = dt.datetime(2020, 1, 1, tzinfo=_UTC)
    for number in range(12):
        _write(root / f"old{number:02}.csv", b"id\n1\n", old)
    _write(root / "new1.csv", b"id\n2\n", dt.datetime(2026, 10, 1, tzinfo=_UTC))
    _write(root / "new2.csv", b"id\n3\n", dt.datetime(2026, 10, 2, tzinfo=_UTC))
    since = dt.datetime(2026, 1, 1, tzinfo=_UTC)

    table = files.read("exports", "*.csv", since=since, max_files=3)

    assert table.column("id").to_pylist() == [2, 3]
    with pytest.raises(CapExceeded) as caught:
        files.read("exports", "*.csv", since=since, max_files=1)
    assert "more than max_files (1)" in str(caught.value)
    assert "pass since=" not in str(caught.value)
    with pytest.raises(CapExceeded, match=r"narrow the pattern or pass since="):
        files.read("exports", "*.csv", max_files=3)


def test_a_workbook_without_a_worksheet_is_a_file_shape_error(root: Path) -> None:
    """A workbook that lists no sheet names the file and says what to do."""
    from lakana.ingest import FileShapeError

    source = zipfile.ZipFile(io.BytesIO(_xlsx(pl.DataFrame({"id": [1]}), "Sheet1")))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as target:
        for item in source.infolist():
            data = source.read(item.filename)
            if item.filename == "xl/workbook.xml":
                import re

                data = re.sub(rb"<sheets>.*</sheets>", b"<sheets/>", data, flags=re.S)
            target.writestr(item, data)
    _write(root / "empty.xlsx", buffer.getvalue())

    with pytest.raises(FileShapeError, match=r"`exports/empty\.xlsx`: .*check the file"):
        files.read("exports", "empty.xlsx")


def test_a_reader_that_panics_is_a_file_shape_error_but_an_interrupt_passes(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Anything a reader raises is the file's fault, except a request to stop."""
    from lakana.ingest import FileShapeError

    _write(root / "a.csv", b"id\n1\n")

    class Panic(BaseException):  # stands in for pyo3's PanicException
        pass

    for failure in (Panic("boom"), IndexError("list index out of range")):

        def fail(*args: object, failure: BaseException = failure, **kwargs: object) -> None:
            raise failure

        with monkeypatch.context() as patch:
            patch.setattr(pl, "read_csv", fail)
            with pytest.raises(FileShapeError, match=r"`exports/a\.csv`: "):
                files.read("exports", "a.csv")

    def stop(*args: object, **kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(pl, "read_csv", stop)
    with pytest.raises(KeyboardInterrupt):
        files.read("exports", "a.csv")


def test_a_missing_path_gets_a_next_step(root: Path) -> None:
    """A base that does not exist says where to look and what to do."""
    root.rename(root.parent / "moved")

    with pytest.raises(DirectoryError) as caught:
        files.read("exports", "*.csv")
    assert "'in' does not exist" in str(caught.value)
    assert "check the connection's path with `lakana connection show`" in str(caught.value)


def test_control_characters_in_a_cell_are_escaped_in_the_message(root: Path) -> None:
    """An escape sequence or a bidi override in a bad value never reaches a log raw."""
    _write(root / "a.csv", "x\nab\x1b[31mRED‮\n".encode())

    with pytest.raises(SourceError) as caught:
        files.read("exports", "a.csv", schema={"x": pl.Int64})
    message = str(caught.value)
    assert "\x1b" not in message
    assert "‮" not in message
    assert "\\x1b" in message


def test_a_small_workbook_that_unpacks_past_the_caps_is_refused_before_it_is_parsed(
    root: Path,
) -> None:
    """A zip bomb of a workbook: the unpacked size counts against both byte caps."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("xl/worksheets/sheet1.xml", b"a" * 5_000_000)
    _write(root / "bomb.xlsx", buffer.getvalue())
    assert len(buffer.getvalue()) < 20_000

    with pytest.raises(CapExceeded) as caught:
        files.read("exports", "bomb.xlsx", max_file_bytes=1_000_000)
    assert "`exports/bomb.xlsx` unpacks to 5000000 bytes" in str(caught.value)
    assert f"{len(buffer.getvalue())} bytes" in str(caught.value)
    assert "max_file_bytes (1000000)" in str(caught.value)
    with pytest.raises(CapExceeded, match=r"max_total_bytes \(1000000\)"):
        files.read("exports", "bomb.xlsx", max_total_bytes=1_000_000)


def test_a_file_that_is_no_zip_is_left_to_the_reader(root: Path) -> None:
    """A `.xlsx` that is not a zip gets the reader's error, not a size error."""
    from lakana.ingest import FileShapeError

    _write(root / "a.xlsx", b"not a zip")

    with pytest.raises(FileShapeError):
        files.read("exports", "a.xlsx")
