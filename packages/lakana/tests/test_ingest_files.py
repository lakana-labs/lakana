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
from lakana.ingest._directory import DirectoryError, RootUnavailable

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
