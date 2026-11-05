"""The task's record of the files it read is the record the server parses (#344).

The SDK (`lakana.context.ReadFile`, `files_read_payload`) cannot import the core
(`lakana_core.catalog.files_read`); both sides of the record must stay the same.
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import cast

from lakana import context
from lakana.ingest import _read_set
from lakana_core.auth import PrincipalRef
from lakana_core.catalog import files_read

_MARK = dt.datetime(2026, 11, 2, 12, tzinfo=dt.UTC)
_FILES = [
    context.ReadFile("exports", "a.csv", 5, _MARK),
    context.ReadFile("exports", "naïve/日本-\U0001f511.csv", 0, _MARK, '"e"'),
    context.ReadFile("exports", "x" * 1024, 2**63 - 1, _MARK - dt.timedelta(days=29), "é" * 512),
]


def _task() -> PrincipalRef:
    """Act as a task bound to an asset that declares `exports`.

    Returns
    -------
    PrincipalRef
        Enough of one for `parse_files_read`.
    """
    bound = SimpleNamespace(connections=("exports",))
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
