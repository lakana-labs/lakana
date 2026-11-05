"""The record of the files an incremental `files.read` handed to an asset (#344)."""

from __future__ import annotations

import datetime as dt
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_arrow_writes import _ids, _task

from lakana_core.auth import AllowAllAuthorization, PrincipalRef
from lakana_core.catalog import files_read
from lakana_core.catalog.files_read import (
    FilesReadReport,
    TaskRecord,
    files_read_for_task,
    forget_files_read,
    parse_files_read,
    record_files_read,
    split_files_read,
)
from lakana_core.catalog.service import CatalogService
from lakana_core.config import StorageConfig
from lakana_core.errors import ValidationError
from lakana_core.identifiers import TableRef
from lakana_core.models import IncrementalFileRead, WriteMode
from lakana_core.storage.resolver import StorageResolver

_T0 = dt.datetime(2026, 11, 1, 12, tzinfo=dt.UTC)


def _files(record: TaskRecord | None) -> list[dict[str, object]]:
    """Take the files of a loaded record.

    Parameters
    ----------
    record
        What `files_read_for_task` returned.

    Returns
    -------
    list of dict
        Its files; none for `None`.
    """
    return [] if record is None else record.files


def _report(*files: dict[str, object], watermark: str | None = None) -> bytes:
    """Encode a record as the SDK sends it: a header line, then one line per file.

    Parameters
    ----------
    *files
        The file entries.
    watermark
        The run's watermark, an ISO time, or ``None``.

    Returns
    -------
    bytes
        The newline-delimited JSON, ending in a newline.
    """
    lines = [{"version": 1, "watermark": watermark}, *files]
    return b"".join(json.dumps(line).encode() + b"\n" for line in lines)


def _file(
    name: str, modified: dt.datetime = _T0, connection: str = "exports", etag: str | None = None
) -> dict[str, object]:
    """Build one file entry of a record.

    Parameters
    ----------
    name
        The path relative to the connection.
    modified
        The time `files.read` used for it.
    connection
        The connection it was read from.
    etag
        Its ETag, or ``None``.

    Returns
    -------
    dict[str, object]
        The entry, four bytes in size.
    """
    return {
        "connection": connection,
        "name": name,
        "size": 4,
        "modified": modified.isoformat(),
        "etag": etag,
    }


def _malformed(what: str) -> str:
    """Spell the refusal of a malformed record.

    Parameters
    ----------
    what
        What is wrong with it.

    Returns
    -------
    str
        The error text.
    """
    return (
        "The record of the files read (Arrow schema metadata `lakana.files_read`) is malformed: "
        f"{what}. Use the same release of `lakana` in the project as on the server."
    )


@pytest.fixture
def catalog(session: Session, principal: PrincipalRef, tmp_path: Path) -> CatalogService:
    """Provide `dev` with the table `gold.t`, the target of the records.

    Parameters
    ----------
    session
        Database session.
    principal
        The acting principal.
    tmp_path
        The storage root.

    Returns
    -------
    CatalogService
        An allow-all catalog.
    """
    service = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
    root = StorageConfig(scheme="file", root=f"file://{tmp_path}", options={})
    service.create_environment(principal, name="dev", storage=root)
    service.write_batches(
        principal,
        TableRef.from_dotted("dev", "gold.t"),
        mode=WriteMode.CREATE,
        schema=_ids([1]).schema,
        batches=_ids([1]).to_batches(),
    )
    return service


def _table_id(catalog: CatalogService, principal: PrincipalRef) -> uuid.UUID:
    """Find the id of `gold.t`.

    Parameters
    ----------
    catalog
        The catalog holding it.
    principal
        The acting principal.

    Returns
    -------
    uuid.UUID
        The table's id.
    """
    return catalog.load_table(principal, TableRef.from_dotted("dev", "gold.t")).uuid


def test_only_a_bound_task_may_send_a_record(principal: PrincipalRef) -> None:
    """A personal token's upload with a record is refused.

    Parameters
    ----------
    principal
        A principal with no task scope.
    """
    with pytest.raises(ValidationError) as caught:
        parse_files_read(_report(_file("a.csv")), principal)
    assert str(caught.value) == (
        "Only a run of a deployed asset may send a record of the files it read (the Arrow "
        "schema metadata `lakana.files_read`); remove it from the upload."
    )


def test_a_connection_the_asset_does_not_declare_is_refused(principal: PrincipalRef) -> None:
    """A task may record files only from the connections its asset declares.

    Parameters
    ----------
    principal
        The run principal.
    """
    with pytest.raises(ValidationError) as caught:
        parse_files_read(_report(_file("a.csv", connection="other")), _task(principal, "exports"))
    assert str(caught.value) == (
        "The record of the files read names the connection 'other', which the asset does not "
        "declare; add it to @asset(connections=[...]) and deploy again."
    )


def _entry(**changes: object) -> bytes:
    """Encode a record of one file with some of its fields changed.

    Parameters
    ----------
    **changes
        The fields to replace.

    Returns
    -------
    bytes
        The newline-delimited JSON.
    """
    return _report({**_file("a.csv"), **changes})


_HEADER = b'{"version": 1, "watermark": null}\n'
_NOT_A_FILE = "file 1 is not a connection, name, size, modified time and ETag"


def test_brackets_inside_a_name_are_no_nesting(principal: PrincipalRef) -> None:
    """The nesting scan skips strings: a name of brackets and escaped quotes still parses.

    Parameters
    ----------
    principal
        The run principal.
    """
    name = '[[[[{{{{\\"[[[[.csv'
    report = parse_files_read(_report(_file(name)), _task(principal, "exports"))
    assert report.files[0].name == name


@pytest.mark.parametrize(
    ("raw", "what"),
    [
        (b"not json", "line 1 is not JSON"),
        (b"\xff", "line 1 is not JSON"),
        (b"", "line 1 is not JSON"),
        (b"[]", "the first line must be a header with version 1"),
        (b'{"version": 2, "watermark": null}', "the first line must be a header with version 1"),
        (
            b'{"version": 1, "watermark": "yesterday"}',
            "watermark must be null or a time with a zone",
        ),
        (b'{"version": 1, "watermark": 7}', "watermark must be null or a time with a zone"),
        (
            b'{"version": 1, "watermark": "2026-11-01T12:00:00"}',
            "watermark 2026-11-01T12:00:00 has no time zone; it must be UTC",
        ),
        (_HEADER + b"\xff\n", "line 2 is not JSON"),
        # Deeper than any interpreter's recursion limit, within the line cap: never a 500.
        (_HEADER + b"[" * 16000 + b"]" * 16000, "line 2 is not JSON"),
        (_HEADER + b'{"a": ' * 9 + b"1" + b"}" * 9, "line 2 is not JSON"),
        (_HEADER + b"\n" + json.dumps(_file("a.csv")).encode(), "line 2 is not JSON"),
        (_HEADER + b'{"connection": "exports"}', _NOT_A_FILE),
        (_HEADER + b'"a.csv"', _NOT_A_FILE),
        (_entry(size=True), _NOT_A_FILE),
        (_entry(name=""), _NOT_A_FILE),
        (
            _entry(connection="c" * 64),
            _NOT_A_FILE,
        ),
        (
            _entry(modified="2026-11-01T12:00:00"),
            _NOT_A_FILE,
        ),
        (_entry(etag=7), _NOT_A_FILE),
        (_entry(size=-1), "file 1 has the size -1, outside 0 to 9223372036854775807 bytes"),
        (
            _entry(size=2**63),
            "file 1 has the size 9223372036854775808, outside 0 to 9223372036854775807 bytes",
        ),
        (_entry(etag="e" * 513), "file 1 has an ETag longer than 512 characters"),
    ],
)
def test_a_malformed_record_names_what_is_wrong(
    principal: PrincipalRef, raw: bytes, what: str
) -> None:
    """Each malformed part is named, and the next step is to align the releases.

    Parameters
    ----------
    principal
        The run principal.
    raw
        The record sent.
    what
        The part the refusal names.
    """
    with pytest.raises(ValidationError) as caught:
        parse_files_read(raw, _task(principal, "exports"))
    assert str(caught.value) == _malformed(what)


def test_the_largest_size_and_etag_are_accepted(principal: PrincipalRef) -> None:
    """The bounds themselves are inside: a size of 2^63-1 and an ETag of 512 characters.

    Parameters
    ----------
    principal
        The run principal.
    """
    report = parse_files_read(
        _entry(size=2**63 - 1, etag="e" * 512, name="n" * 1024), _task(principal, "exports")
    )
    assert [(f.size, len(f.etag or ""), len(f.name)) for f in report.files] == [
        (2**63 - 1, 512, 1024)
    ]


def test_a_name_longer_than_the_cap_is_refused(principal: PrincipalRef) -> None:
    """A path over 1,024 characters names the file and how to leave it out.

    Parameters
    ----------
    principal
        The run principal.
    """
    with pytest.raises(ValidationError) as caught:
        parse_files_read(_report(_file("a.csv"), _file("n" * 1025)), _task(principal, "exports"))
    assert str(caught.value) == (
        "The record of the files read names a file whose path is longer than 1024 characters "
        "(file 2, beginning 'nnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnn'); "
        "move it to a shorter path, or narrow the pattern the asset passes to files.read so that "
        "it does not match."
    )


def test_a_record_larger_than_the_byte_cap_is_refused_before_it_is_parsed(
    principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The size of the raw record is checked before `json.loads` sees it.

    Parameters
    ----------
    principal
        The run principal.
    monkeypatch
        Lowers the cap and takes `json.loads` away.
    """
    raw = _report(_file("a.csv"))
    monkeypatch.setattr(files_read, "MAX_RECORD_BYTES", len(raw) - 1)

    # A `json` without `loads`: parsing the record would fail with AttributeError.
    monkeypatch.setattr(files_read, "json", SimpleNamespace())
    with pytest.raises(ValidationError) as caught:
        parse_files_read(raw, _task(principal, "exports"))
    assert str(caught.value) == (
        f"The record of the files read is {len(raw)} bytes, more than {len(raw) - 1}; narrow "
        "the pattern the asset passes to files.read."
    )


def test_the_byte_cap_fits_the_most_files_with_long_names() -> None:
    """100,000 entries with 300-character names and an ETag fit inside the byte cap."""
    entry = _file("n" * 300, etag='"' + "e" * 32 + '"')
    one = len(json.dumps(entry).encode()) + 1
    assert one * files_read.MAX_FILES_READ < files_read.MAX_RECORD_BYTES


_ASTRAL = "\U0001f600"
"""An emoji: twelve bytes as `json.dumps` escapes it by default, the most of any character."""


def test_the_worst_legal_entry_fits_a_line(principal: PrincipalRef) -> None:
    """Every field at its cap, in emoji, escaped by default `json.dumps`, fits and is accepted.

    Parameters
    ----------
    principal
        The run principal.
    """
    connection = _ASTRAL * 63
    entry = {
        **_file(
            _ASTRAL * 1024, _T0.replace(tzinfo=dt.timezone(dt.timedelta(hours=-14))), connection
        ),
        "etag": _ASTRAL * 512,
        "size": 2**63 - 1,
    }
    line = json.dumps(entry).encode()
    assert len(line) < files_read.MAX_LINE_BYTES
    report = parse_files_read(_HEADER + line, _task(principal, connection))
    assert [len(f.name) for f in report.files] == [1024]
    control = json.dumps({**_file("\x01" * 1024), "etag": "\x01" * 512}).encode()
    assert len(control) < len(line)


def test_a_line_one_byte_over_the_cap_is_refused(principal: PrincipalRef) -> None:
    """A line of 32 KiB + 1 is refused by its length, naming the line.

    Parameters
    ----------
    principal
        The run principal.
    """
    raw = _HEADER + b" " * (32 * 1024 + 1)
    with pytest.raises(ValidationError) as caught:
        parse_files_read(raw, _task(principal, "exports"))
    assert str(caught.value) == _malformed("line 2 is 32769 bytes, more than 32768")


def test_more_lines_than_the_cap_are_refused_before_they_are_parsed(
    principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lines are counted before any is split off or parsed.

    Parameters
    ----------
    principal
        The run principal.
    monkeypatch
        Lowers the cap and takes `json.loads` away.
    """
    monkeypatch.setattr(files_read, "MAX_FILES_READ", 2)
    monkeypatch.setattr(files_read, "json", SimpleNamespace())
    raw = _HEADER + b"{}\n" * 3
    with pytest.raises(ValidationError) as caught:
        parse_files_read(raw.rstrip(b"\n"), _task(principal, "exports"))
    assert str(caught.value) == (
        "The record of the files read lists 3 files, more than 2; narrow the pattern the asset "
        "passes to files.read."
    )


def test_a_hostile_line_is_refused_before_it_is_parsed(principal: PrincipalRef) -> None:
    """A hostile single line of 64 MiB is refused by its length, without `json.loads`.

    Parameters
    ----------
    principal
        The run principal.
    """
    raw = b"[" + b"[]," * ((files_read.MAX_RECORD_BYTES - 4) // 3) + b"[]]"
    assert len(raw) <= files_read.MAX_RECORD_BYTES
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(files_read, "json", SimpleNamespace())
        with pytest.raises(ValidationError) as caught:
            parse_files_read(raw, _task(principal, "exports"))
    assert str(caught.value) == _malformed(f"line 1 is {len(raw)} bytes, more than 32768")


def test_a_long_line_after_the_header_names_its_number(principal: PrincipalRef) -> None:
    """The refusal names the line, so the file it lists can be found.

    Parameters
    ----------
    principal
        The run principal.
    """
    raw = _report(_file("a.csv"), _file("n" * 40_000))
    with pytest.raises(ValidationError) as caught:
        parse_files_read(raw, _task(principal, "exports"))
    assert "line 3 is 40" in str(caught.value)


def test_more_files_than_the_cap_are_refused(
    principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A record listing more than `MAX_FILES_READ` files is refused.

    Parameters
    ----------
    principal
        The run principal.
    monkeypatch
        Lowers the cap.
    """
    monkeypatch.setattr(files_read, "MAX_FILES_READ", 2)
    raw = _report(_file("a.csv"), _file("b.csv"), _file("c.csv"))
    with pytest.raises(ValidationError) as caught:
        parse_files_read(raw, _task(principal, "exports"))
    assert str(caught.value) == (
        "The record of the files read lists 3 files, more than 2; narrow the pattern the asset "
        "passes to files.read."
    )


def test_the_same_file_twice_is_kept_once(principal: PrincipalRef) -> None:
    """Review Focus 2: two calls that list the same file report it twice.

    Parameters
    ----------
    principal
        The run principal.
    """
    report = parse_files_read(_report(_file("a.csv"), _file("a.csv")), _task(principal, "exports"))
    assert [f.name for f in report.files] == ["a.csv"]


def test_an_aware_watermark_is_kept_as_utc(principal: PrincipalRef) -> None:
    """A watermark with any zone is read as the same instant in UTC.

    Parameters
    ----------
    principal
        The run principal.
    """
    raw = _report(watermark="2026-11-01T14:00:00+02:00")
    report = parse_files_read(raw, _task(principal, "exports"))
    assert (report.watermark, report.watermark and report.watermark.tzinfo) == (_T0, dt.UTC)


def test_split_takes_only_the_key_off() -> None:
    """The record leaves the upload's metadata; other keys stay."""
    table = _ids([1]).replace_schema_metadata({b"lakana.files_read": b"{}", b"other": b"x"})
    rows, raw = split_files_read(table)
    assert (raw, rows.schema.metadata) == (b"{}", {b"other": b"x"})
    alone = _ids([1]).replace_schema_metadata({b"lakana.files_read": b"{}"})
    assert split_files_read(alone)[0].schema.metadata is None
    assert split_files_read(_ids([1])) == (_ids([1]), None)


def test_rows_are_recorded_and_pruned_by_the_runs_watermark(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """A run's watermark prunes the rows older than it less 30 days.

    Parameters
    ----------
    session
        Database session (SQLite: `read_at` must be aware there too).
    principal
        The run principal.
    catalog
        Holds `gold.t`.
    """
    table_id = _table_id(catalog, principal)
    old = parse_files_read(
        _report(_file("old.csv", _T0 - dt.timedelta(days=31))), _task(principal, "exports")
    )
    record_files_read(session, table_id, 1, old)
    new = parse_files_read(
        _report(_file("new.csv", etag='"e1"'), watermark=_T0.isoformat()),
        _task(principal, "exports"),
    )
    record_files_read(session, table_id, 2, new)
    rows = session.scalars(select(IncrementalFileRead)).all()
    assert [(r.name, r.snapshot_id, r.etag, r.modified) for r in rows] == [
        ("new.csv", 2, '"e1"', _T0)
    ]
    assert rows[0].read_at.tzinfo == dt.UTC


def test_an_empty_record_without_a_watermark_writes_nothing(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """No files and no watermark: nothing to add and nothing to prune.

    Parameters
    ----------
    session
        Database session.
    principal
        The run principal.
    catalog
        Holds `gold.t`.
    """
    table_id = _table_id(catalog, principal)
    record_files_read(session, table_id, 1, parse_files_read(_report(), _task(principal, "x")))
    forget_files_read(session, table_id, [])
    assert session.scalars(select(IncrementalFileRead)).all() == []


def test_forget_deletes_only_the_snapshots_set_back(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """Forgetting a snapshot deletes its rows and keeps the others.

    Parameters
    ----------
    session
        Database session.
    principal
        The run principal.
    catalog
        Holds `gold.t`.
    """
    table_id = _table_id(catalog, principal)
    for snapshot, name in ((1, "a.csv"), (2, "b.csv")):
        report = parse_files_read(_report(_file(name)), _task(principal, "exports"))
        record_files_read(session, table_id, snapshot, report)
    forget_files_read(session, table_id, [2])
    assert session.scalars(select(IncrementalFileRead.name)).all() == ["a.csv"]


def test_the_task_gets_the_rows_inside_30_days_of_its_watermark(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """Review Focus 1 and 5: a round trip compares equal; a naive timestamp watermark is UTC.

    Parameters
    ----------
    session
        Database session.
    principal
        The run principal.
    catalog
        Holds `gold.t`.
    """
    table_id = _table_id(catalog, principal)
    inside, outside = _T0 - dt.timedelta(days=29), _T0 - dt.timedelta(days=30)
    report = parse_files_read(
        _report(_file("in.csv", inside, etag='"x"'), _file("out.csv", outside)),
        _task(principal, "exports"),
    )
    record_files_read(session, table_id, 1, report)
    found = files_read_for_task(session, table_id, f"timestamptz:{_T0.isoformat()}")
    assert found == TaskRecord([report.files[0].to_json()], connections=["exports"])
    naive = _T0.replace(tzinfo=None).isoformat()
    assert files_read_for_task(session, table_id, f"timestamp:{naive}") == found
    assert len(_files(files_read_for_task(session, table_id, None))) == 2


def test_two_connections_of_one_asset_are_kept_apart(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """The same name on two connections is two files.

    Parameters
    ----------
    session
        Database session.
    principal
        The run principal.
    catalog
        Holds `gold.t`.
    """
    table_id = _table_id(catalog, principal)
    report = parse_files_read(
        _report(_file("a.csv"), _file("a.csv", connection="landing")),
        _task(principal, "exports", "landing"),
    )
    record_files_read(session, table_id, 1, report)
    found = _files(files_read_for_task(session, table_id, None))
    assert [(f["connection"], f["name"]) for f in found] == [
        ("exports", "a.csv"),
        ("landing", "a.csv"),
    ]


def test_a_watermark_that_is_no_time_gets_no_record(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """An integer or date watermark has no files-read record.

    Parameters
    ----------
    session
        Database session.
    principal
        The run principal.
    catalog
        Holds `gold.t`.
    """
    table_id = _table_id(catalog, principal)
    assert files_read_for_task(session, table_id, "long:41") is None
    assert files_read_for_task(session, table_id, "date:2026-11-01") is None


def test_more_rows_than_the_cap_load_the_newest_with_a_horizon(
    session: Session,
    principal: PrincipalRef,
    catalog: CatalogService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A busy table never stops its asset: the newest rows load, the oldest one's is the horizon.

    Parameters
    ----------
    session
        Database session.
    principal
        The run principal.
    catalog
        Holds `gold.t`.
    monkeypatch
        Lowers the cap.
    """
    table_id = _table_id(catalog, principal)
    hour = dt.timedelta(hours=1)
    report = parse_files_read(
        _report(_file("c.csv", _T0 - 3 * hour), _file("a.csv", _T0 - hour), _file("b.csv", _T0)),
        _task(principal, "exports"),
    )
    record_files_read(session, table_id, 1, report)
    mark = f"timestamptz:{_T0.isoformat()}"
    assert files_read_for_task(session, table_id, mark) == TaskRecord(
        [f.to_json() for f in sorted(report.files, key=lambda f: f.name)], connections=["exports"]
    )
    monkeypatch.setattr(files_read, "MAX_FILES_READ", 2)
    monkeypatch.setattr(files_read, "CONNECTION_RESERVE", 0)
    loaded = files_read_for_task(session, table_id, mark)
    assert loaded is not None
    assert [f["name"] for f in loaded.files] == ["a.csv", "b.csv"]
    assert loaded.horizon == _T0 - hour
    monkeypatch.setattr(files_read, "MAX_FILES_READ", 3)
    full = files_read_for_task(session, table_id, mark)
    assert full is not None
    assert (len(full.files), full.horizon) == (3, _T0 - 3 * hour)  # a full load sets it too
    monkeypatch.setattr(files_read, "MAX_FILES_READ", 100)
    line = files_read.entry_bytes(report.files[0])
    # Past the byte budget less one line: the run that wrote these may have dropped older files.
    monkeypatch.setattr(files_read, "RECORD_BUDGET_BYTES", files_read.MAX_LINE_BYTES + line + 1)
    by_bytes = files_read_for_task(session, table_id, mark)
    assert by_bytes is not None
    assert ([f["name"] for f in by_bytes.files], by_bytes.horizon) == (
        ["a.csv", "b.csv"],
        _T0 - hour,
    )
    assert by_bytes.connections == ["exports"]


def test_the_record_names_every_connection_with_a_row_in_the_window(
    session: Session,
    principal: PrincipalRef,
    catalog: CatalogService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A quiet connection whose rows all fall below the horizon is still named.

    Parameters
    ----------
    session
        Database session.
    principal
        The run principal.
    catalog
        Holds `gold.t`.
    monkeypatch
        Lowers the cap.
    """
    table_id = _table_id(catalog, principal)
    hour = dt.timedelta(hours=1)
    report = parse_files_read(
        _report(
            _file("q.csv", _T0 - 5 * hour, connection="quiet"),
            _file("a.csv", _T0 - hour),
            _file("b.csv", _T0),
        ),
        _task(principal, "exports", "quiet"),
    )
    record_files_read(session, table_id, 1, report)
    monkeypatch.setattr(files_read, "MAX_FILES_READ", 2)
    monkeypatch.setattr(files_read, "CONNECTION_RESERVE", 0)
    loaded = files_read_for_task(session, table_id, f"timestamptz:{_T0.isoformat()}")
    assert loaded is not None
    assert [f["connection"] for f in loaded.files] == ["exports", "exports"]
    assert loaded.connections == ["exports", "quiet"]


@pytest.mark.parametrize("chunk", [1000, 1, 2])
def test_a_file_already_recorded_is_not_recorded_twice(
    session: Session,
    principal: PrincipalRef,
    catalog: CatalogService,
    monkeypatch: pytest.MonkeyPatch,
    chunk: int,
) -> None:
    """A seeded or known file reported again adds no second row, across look-up chunks.

    Parameters
    ----------
    session
        Database session.
    principal
        The run principal.
    catalog
        Holds `gold.t`.
    monkeypatch
        Sets the look-up chunk.
    chunk
        Files per look-up.
    """
    monkeypatch.setattr(files_read, "_DEDUPE_CHUNK", chunk)
    table_id = _table_id(catalog, principal)
    task = _task(principal, "exports")
    record_files_read(session, table_id, 1, parse_files_read(_report(_file("a.csv")), task))
    again = parse_files_read(
        _report(_file("a.csv"), _file("a.csv", etag='"e"'), _file("b.csv")), task
    )
    record_files_read(session, table_id, 2, again)
    rows = session.scalars(select(IncrementalFileRead))
    assert {(r.name, r.etag, r.snapshot_id) for r in rows} == {
        ("a.csv", None, 1),
        ("a.csv", '"e"', 2),
        ("b.csv", None, 2),
    }


@pytest.mark.parametrize(
    "modified",
    [
        "0001-01-01T00:00:00+01:00",
        "9999-12-31T23:59:59-01:00",
        "0001-01-30T23:59:59+00:00",
        "9999-12-02T00:00:00+00:00",
    ],
)
def test_a_file_time_near_the_limits_is_refused(principal: PrincipalRef, modified: str) -> None:
    """A time within 30 days of year 1 or 9999, after its offset, is refused, not overflowed.

    Parameters
    ----------
    principal
        The run principal.
    modified
        The file's time as sent.
    """
    with pytest.raises(ValidationError) as caught:
        parse_files_read(_entry(modified=modified), _task(principal, "exports"))
    assert str(caught.value) == (
        f"The record of the files read gives file 1 the time {modified}, within 30 days of the "
        "earliest or latest time Lakana can store (years 1 and 9999); correct the file's "
        "modification time (or the date in its name), or narrow the pattern the asset passes "
        "to files.read so that it does not match."
    )


@pytest.mark.parametrize(
    "watermark", ["0001-01-01T00:00:00+01:00", "9999-12-31T23:59:59-01:00", "0001-01-05T00:00:00Z"]
)
def test_a_watermark_near_the_limits_is_refused(principal: PrincipalRef, watermark: str) -> None:
    """A header watermark within 30 days of the limits is refused, not overflowed.

    Parameters
    ----------
    principal
        The run principal.
    watermark
        The watermark as sent.
    """
    with pytest.raises(ValidationError) as caught:
        parse_files_read(_report(watermark=watermark), _task(principal, "exports"))
    assert str(caught.value) == (
        f"The record of the files read has the watermark {watermark}, within 30 days of the "
        "earliest or latest time Lakana can store (years 1 and 9999); correct that time in the "
        "target table's watermark column, then run the asset again."
    )


def test_the_times_just_inside_the_limits_are_accepted(principal: PrincipalRef) -> None:
    """Exactly 30 days from either limit is inside.

    Parameters
    ----------
    principal
        The run principal.
    """
    early, late = "0001-01-31T00:00:00+00:00", "9999-12-01T23:59:59.999999+00:00"
    report = parse_files_read(
        _report(_file("a.csv", dt.datetime.fromisoformat(late)), watermark=early),
        _task(principal, "exports"),
    )
    assert (report.watermark, report.files[0].modified.year) == (
        dt.datetime.fromisoformat(early),
        9999,
    )


def test_a_watermark_in_year_1_neither_prunes_nor_bounds_the_load(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """At commit and at load, a mark less than 30 days after year 1 does not overflow.

    Parameters
    ----------
    session
        Database session.
    principal
        The run principal.
    catalog
        Holds `gold.t`.
    """
    table_id = _table_id(catalog, principal)
    early = dt.datetime(1, 1, 5, tzinfo=dt.UTC)
    record_files_read(session, table_id, 1, parse_files_read(_entry(), _task(principal, "exports")))
    record_files_read(session, table_id, 2, FilesReadReport(early, ()))
    for mark in ("timestamptz:0001-01-05T00:00:00+00:00", "timestamp:0001-01-05T00:00:00"):
        assert [f["name"] for f in _files(files_read_for_task(session, table_id, mark))] == [
            "a.csv"
        ]


@pytest.mark.parametrize("name", ["a\x00b", "\ud800.csv", "\udcff"])
def test_a_name_lakana_cannot_store_is_refused(principal: PrincipalRef, name: str) -> None:
    """A NUL or a lone surrogate in a name is refused, the name shown escaped.

    Parameters
    ----------
    principal
        The run principal.
    name
        The file's name as sent.
    """
    with pytest.raises(ValidationError) as caught:
        parse_files_read(_report(_file(name)), _task(principal, "exports"))
    assert str(caught.value) == (
        f"The record of the files read names a file Lakana cannot store (file 1, {name!r}): its "
        "name holds a NUL character or is not valid UTF-8; rename the file; Lakana stores file "
        "names as UTF-8 text."
    )


@pytest.mark.parametrize(
    ("changes", "what"),
    [
        ({"connection": "ex\x00ports"}, _NOT_A_FILE),
        ({"connection": "\ud800"}, _NOT_A_FILE),
        ({"etag": "\x00"}, "file 1 has an ETag that is not UTF-8 text without NUL characters"),
        ({"etag": "\udcff"}, "file 1 has an ETag that is not UTF-8 text without NUL characters"),
    ],
)
def test_a_connection_or_etag_lakana_cannot_store_is_malformed(
    principal: PrincipalRef, changes: dict[str, object], what: str
) -> None:
    """A NUL or a lone surrogate in the connection or the ETag is a malformed record.

    Parameters
    ----------
    principal
        The run principal.
    changes
        The fields replaced.
    what
        The part the refusal names.
    """
    with pytest.raises(ValidationError) as caught:
        parse_files_read(_entry(**changes), _task(principal, "exports", "ex\x00ports", "\ud800"))
    assert str(caught.value) == _malformed(what)


@pytest.mark.parametrize("version", ["true", "1.0"])
def test_a_version_that_is_not_the_integer_1_is_refused(
    principal: PrincipalRef, version: str
) -> None:
    """`true` and `1.0` equal 1 in Python, but are not the version.

    Parameters
    ----------
    principal
        The run principal.
    version
        The JSON value of the header's version.
    """
    raw = f'{{"version": {version}, "watermark": null}}'.encode()
    with pytest.raises(ValidationError) as caught:
        parse_files_read(raw, _task(principal, "exports"))
    assert str(caught.value) == _malformed("the first line must be a header with version 1")
