"""The task's record of the files it read is the record the server parses (#344).

The SDK (`lakana.context.ReadFile`, `files_read_payload`) cannot import the core
(`lakana_core.catalog.files_read`); both sides of the record must stay the same.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import uuid
from types import SimpleNamespace
from typing import cast

import pytest
import sqlalchemy
from sqlalchemy.orm import Session

from lakana import context
from lakana.ingest import _read_set
from lakana.ingest._directory import FileEntry
from lakana_core.auth import PrincipalRef
from lakana_core.catalog import files_read
from lakana_core.models import IncrementalFileRead
from lakana_core.projects.asset_model import MAX_CONNECTIONS

_MARK = dt.datetime(2026, 11, 2, 12, tzinfo=dt.UTC)
_FILES = [
    context.ReadFile("exports", "a.csv", 5, _MARK),
    context.ReadFile("exports", "naïve/日本-\U0001f511.csv", 0, _MARK, '"e"'),
    context.ReadFile("exports", "x" * 1024, 2**63 - 1, _MARK - dt.timedelta(days=29), "é" * 512),
]


def _task(*declared: str) -> PrincipalRef:
    """Act as a task bound to an asset that declares `exports`, or the connections given.

    Parameters
    ----------
    *declared
        The connections it declares; `exports` when none.

    Returns
    -------
    PrincipalRef
        Enough of one for `parse_files_read`.
    """
    bound = SimpleNamespace(connections=declared or ("exports",))
    return cast("PrincipalRef", SimpleNamespace(task_scope=SimpleNamespace(bound=bound)))


def test_the_server_parses_what_the_task_sends() -> None:
    state = context._TaskState(
        client=object(),  # ty: ignore[invalid-argument-type]
        environment="dev",
        asset="bronze.orders",
        declared=frozenset({"exports"}),
        connections={},
        target_watermark=_MARK.replace(tzinfo=None),
        files_read=[*_FILES, _FILES[0]],
    )
    context._set(state)
    try:
        payload = context.files_read_payload()
    finally:
        context._set(None)
    assert payload is not None
    report = files_read.parse_files_read(payload, _task())
    assert report.watermark == _MARK
    assert [f.to_json() for f in report.files] == [f.to_json() for f in _FILES]


def test_the_task_reads_what_the_worker_hands_it() -> None:
    for file in _FILES:
        core = files_read.FileRead(file.connection, file.name, file.size, file.modified, file.etag)
        assert context.ReadFile.from_json(core.to_json()) == file


def test_the_caps_are_the_same() -> None:
    assert context.MAX_FILES_READ == files_read.MAX_FILES_READ
    assert context.RECORD_WINDOW == files_read.RECORD_WINDOW
    assert context.FILES_READ_KEY.encode() == files_read.FILES_READ_METADATA_KEY
    assert _read_set.MAX_NAME_LENGTH == files_read.MAX_NAME_LENGTH
    assert context.MAX_ETAG == files_read._MAX_ETAG
    assert context.RECORD_BUDGET_BYTES == files_read.RECORD_BUDGET_BYTES
    assert context.RECORD_BUDGET_BYTES + 4096 < files_read.MAX_RECORD_BYTES
    assert context.CONNECTION_RESERVE == files_read.CONNECTION_RESERVE == MAX_CONNECTIONS
    # The reserved lines fit in the room between the budget and the server's cap.
    assert (
        context.RECORD_BUDGET_BYTES + MAX_CONNECTIONS * files_read.MAX_LINE_BYTES
        < files_read.MAX_RECORD_BYTES
    )


def test_the_worker_counts_a_line_as_the_task_encodes_it() -> None:
    """The byte budget's horizon relies on both sides counting the same bytes."""
    for file in _FILES:
        core = files_read.FileRead(file.connection, file.name, file.size, file.modified, file.etag)
        assert files_read.entry_bytes(core) == len(context._line(file.to_json()))


def _choose_all(
    state: context._TaskState,
    files: list[tuple[str, str, dt.datetime]],
    after: dt.datetime | None,
) -> list[tuple[str, str]]:
    """Run `choose` for both connections of the probe, as `files.read` does, and report.

    Parameters
    ----------
    state
        The task's state; the reported files are added to it.
    files
        ``(connection, name, time)`` of every file.
    after
        ``since``.

    Returns
    -------
    list of tuple
        ``(connection, name)`` of each file read.
    """
    read = []
    for connection in ("x", "y"):
        timed = [(FileEntry(n, 1, m), m) for c, n, m in files if c == connection]
        choice = _read_set.choose(
            connection, timed, after=after, lookback=dt.timedelta(hours=24), state=state
        )
        read += [(connection, entry.path) for entry, _ in choice.chosen]
        state.files_read.extend(choice.reported)
    return read


def test_a_quiet_connection_trimmed_out_keeps_its_newest_file_and_its_lookback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The record a run trims keeps one file of every connection: the next run reads its late file.

    A busy `x` and a quieter, older `y` in one big first run; the budget is small.
    """
    hour = dt.timedelta(hours=1)
    first = [("x", f"x{n}", _MARK + n * hour) for n in range(6)]
    first += [("y", f"y{n}", _MARK - (10 - n) * hour) for n in range(3)]
    line = len(context._line(context.ReadFile("x", "x0", 1, _MARK).to_json()))
    for module in (context, files_read):
        monkeypatch.setattr(module, "RECORD_BUDGET_BYTES", 3 * line + 10)
        monkeypatch.setattr(module, "CONNECTION_RESERVE", 0)
    # The worker's threshold (the budget less one line) is then just over two lines.
    monkeypatch.setattr(files_read, "MAX_LINE_BYTES", line + 5)
    state = context._TaskState(
        client=object(),  # ty: ignore[invalid-argument-type]
        environment="dev",
        asset="gold.t",
        declared=frozenset({"x", "y"}),
        connections={},
    )
    context._set(state)
    try:
        assert len(_choose_all(state, first, None)) == 9
        with contextlib.redirect_stderr(io.StringIO()):
            payload = context.files_read_payload()
    finally:
        context._set(None)
    assert payload is not None
    report = files_read.parse_files_read(payload, _task("x", "y"))
    assert sorted(f.name for f in report.files) == ["x3", "x4", "x5", "y2"]

    engine = sqlalchemy.create_engine("sqlite://")
    IncrementalFileRead.__table__.create(engine)  # ty: ignore[missing-attribute]
    table_id = uuid.uuid4()
    mark = _MARK + 5 * hour
    with Session(engine) as session:
        files_read.record_files_read(session, table_id, 1, report)
        loaded = files_read.files_read_for_task(
            session, table_id, f"timestamptz:{mark.isoformat()}"
        )
    assert loaded is not None and loaded.horizon is not None
    assert loaded.connections == ["x", "y"]

    assert loaded.horizon == _MARK + 3 * hour  # the oldest kept file of `x`
    second = [*first, ("x", "new", mark + hour), ("y", "ylate", mark - hour - hour / 2)]
    state = context._TaskState(
        client=object(),  # ty: ignore[invalid-argument-type]
        environment="dev",
        asset="gold.t",
        declared=frozenset({"x", "y"}),
        connections={},
        recorded=frozenset(context.ReadFile.from_json(f) for f in loaded.files),
        target_watermark=mark,
        record_horizon=loaded.horizon,
        recorded_connections=frozenset(loaded.connections),
    )
    assert sorted(_choose_all(state, second, mark)) == [("x", "new"), ("y", "ylate")]
