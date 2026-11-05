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


def _report(*files: dict[str, object], watermark: str | None = None) -> bytes:
    """Encode a record as the SDK sends it.

    Parameters
    ----------
    *files
        The file entries.
    watermark
        The run's watermark, an ISO time, or ``None``.

    Returns
    -------
    bytes
        The JSON document.
    """
    return json.dumps({"version": 1, "watermark": watermark, "files": list(files)}).encode()


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
        The JSON document.
    """
    return _report({**_file("a.csv"), **changes})


@pytest.mark.parametrize(
    ("raw", "what"),
    [
        (b"not json", "not JSON"),
        (b"\xff", "not JSON"),
        (b"[]", "version must be 1"),
        (b'{"version": 2, "watermark": null, "files": []}', "version must be 1"),
        (
            b'{"version": 1, "watermark": "yesterday", "files": []}',
            "watermark must be null or a time with a zone",
        ),
        (
            b'{"version": 1, "watermark": 7, "files": []}',
            "watermark must be null or a time with a zone",
        ),
        (
            b'{"version": 1, "watermark": "2026-11-01T12:00:00", "files": []}',
            "watermark 2026-11-01T12:00:00 has no time zone; it must be UTC",
        ),
        (b'{"version": 1, "watermark": null, "files": {}}', "files must be a list"),
        (
            b'{"version": 1, "watermark": null, "files": [{"connection": "exports"}]}',
            "file 1 is not a connection, name, size, modified time and ETag",
        ),
        (
            b'{"version": 1, "watermark": null, "files": ["a.csv"]}',
            "file 1 is not a connection, name, size, modified time and ETag",
        ),
        (_entry(size=True), "file 1 is not a connection, name, size, modified time and ETag"),
        (_entry(name=""), "file 1 is not a connection, name, size, modified time and ETag"),
        (
            _entry(connection="c" * 64),
            "file 1 is not a connection, name, size, modified time and ETag",
        ),
        (
            _entry(modified="2026-11-01T12:00:00"),
            "file 1 is not a connection, name, size, modified time and ETag",
        ),
        (_entry(etag=7), "file 1 is not a connection, name, size, modified time and ETag"),
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
    one = len(json.dumps(entry).encode()) + 2
    assert one * files_read.MAX_FILES_READ < files_read.MAX_RECORD_BYTES


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
    found = files_read_for_task(session, table_id, f"timestamptz:{_T0.isoformat()}", "gold.t")
    assert found == [report.files[0].to_json()]
    naive = _T0.replace(tzinfo=None).isoformat()
    assert files_read_for_task(session, table_id, f"timestamp:{naive}", "gold.t") == found
    assert len(files_read_for_task(session, table_id, None, "gold.t") or []) == 2


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
    found = files_read_for_task(session, table_id, None, "gold.t") or []
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
    assert files_read_for_task(session, table_id, "long:41", "gold.t") is None
    assert files_read_for_task(session, table_id, "date:2026-11-01", "gold.t") is None


def test_more_rows_than_the_cap_fail_the_task_naming_the_count(
    session: Session,
    principal: PrincipalRef,
    catalog: CatalogService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """More recorded rows than a run can check fail the task with the count and the remedy.

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
    report = parse_files_read(
        _report(_file("a.csv"), _file("b.csv"), _file("c.csv")), _task(principal, "exports")
    )
    record_files_read(session, table_id, 1, report)
    monkeypatch.setattr(files_read, "MAX_FILES_READ", 2)
    with pytest.raises(ValidationError) as caught:
        files_read_for_task(session, table_id, f"timestamptz:{_T0.isoformat()}", "gold.t")
    assert str(caught.value) == (
        "gold.t has 3 files recorded as read within 30 days of its watermark, more than the 2 a "
        "run can check; narrow the pattern of files.read, or split the asset so that each reads "
        "a part. The record shrinks as files age past 30 days."
    )
