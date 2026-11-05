"""`lakana.context`: what `ctx.watermark()` returns, and the state the helpers share."""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal

import pytest

from lakana import context
from lakana.context import AssetContext, decode_watermark


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("int:42", 42),
        ("long:9007199254740993", 9007199254740993),
        ("decimal:12.50", Decimal("12.50")),
        ("date:2026-10-22", dt.date(2026, 10, 22)),
        ("timestamp:2026-10-22T08:00:00", dt.datetime(2026, 10, 22, 8)),
        ("timestamptz:2026-10-22T08:00:00+00:00", dt.datetime(2026, 10, 22, 8, tzinfo=dt.UTC)),
    ],
)
def test_each_tag_decodes(text: str, value: object) -> None:
    """The worker's encoding of every incremental key type."""
    decoded = decode_watermark(text)
    assert decoded == value
    assert type(decoded) is type(value)


def test_a_timestamp_stays_naive_and_a_timestamptz_aware() -> None:
    naive = decode_watermark("timestamp:2026-10-22T08:00:00")
    assert isinstance(naive, dt.datetime)
    assert naive.tzinfo is None
    aware = decode_watermark("timestamptz:2026-10-22T08:00:00+00:00")
    assert isinstance(aware, dt.datetime)
    assert aware.utcoffset() == dt.timedelta(0)


@pytest.mark.parametrize(
    "text", ["", "42", "float:1.5", "int:", "int:x", "date:22.10.2026", "decimal:abc"]
)
def test_a_malformed_watermark_is_refused(text: str) -> None:
    with pytest.raises(ValueError, match="not a watermark"):
        decode_watermark(text)


def test_no_watermark_gives_the_default() -> None:
    """A first run (no table, or a table without rows) returns `default`."""
    since = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    assert AssetContext(None).watermark() is None
    assert AssetContext(None).watermark(default=since) == since
    assert AssetContext(7).watermark(default=0) == 7
    assert AssetContext(0).watermark(default=5) == 0


def test_current_outside_a_task_says_where_it_works() -> None:
    context._set(None)
    with pytest.raises(RuntimeError, match="only inside a Lakana task"):
        context.current()


def test_the_harness_state_is_shared_and_collects_secrets() -> None:
    state = context._TaskState(
        client=object(),  # ty: ignore[invalid-argument-type]
        environment="dev",
        asset="bronze.orders",
        declared=frozenset({"crm-db"}),
        connections={"crm-db": {"kind": "postgresql"}},
    )
    context._set(state)
    try:
        assert context.current() is state
        context.register_secret("s3cr3t-value")
        context.register_secret("abc")  # too short to mask without masking ordinary text
        assert state.secrets == {"s3cr3t-value"}
    finally:
        context._set(None)
    context.register_secret("ignored-outside-a-task")
    assert state.secrets == {"s3cr3t-value"}


_MARK = dt.datetime(2026, 11, 2, 12, tzinfo=dt.UTC)


def _state(**fields: object) -> context._TaskState:
    """Build a task state without a client.

    Parameters
    ----------
    **fields
        Fields to set besides the required ones.

    Returns
    -------
    _TaskState
        The state.
    """
    return context._TaskState(
        client=object(),  # ty: ignore[invalid-argument-type]
        environment="dev",
        asset="bronze.orders",
        declared=frozenset({"exports"}),
        connections={},
        **fields,  # ty: ignore[invalid-argument-type]
    )


def test_a_file_read_survives_its_json() -> None:
    plus_two = dt.timezone(dt.timedelta(hours=2))
    file = context.ReadFile("exports", "a/b.csv", 5, _MARK.astimezone(plus_two), '"x"')
    back = context.ReadFile.from_json(file.to_json())
    assert back == file
    assert back.modified.utcoffset() == dt.timedelta(0)
    assert context.ReadFile.from_json({**file.to_json(), "etag": None}).etag is None


@pytest.mark.parametrize(
    "value",
    [
        [],
        {"connection": "", "name": "a", "size": 1, "modified": _MARK.isoformat()},
        {"connection": "e", "name": "", "size": 1, "modified": _MARK.isoformat()},
        {"connection": "e", "name": "a", "size": 1, "modified": "2026-11-02T12:00:00"},
        {"connection": "e", "name": "a", "size": 1, "modified": 7},
        {"connection": "e", "name": "a", "size": 1, "modified": "yesterday"},
        {"connection": "e", "name": "a", "size": -1, "modified": _MARK.isoformat()},
        {"connection": "e", "name": "a", "size": True, "modified": _MARK.isoformat()},
        {
            "connection": "e",
            "name": "a",
            "size": 1,
            "modified": _MARK.isoformat(),
            "etag": "x" * 513,
        },
        {"connection": "e", "name": "a", "size": 1, "modified": _MARK.isoformat(), "etag": 3},
    ],
)
def test_what_is_no_file_read_is_refused_without_its_value(value: object) -> None:
    with pytest.raises(ValueError) as caught:
        context.ReadFile.from_json(value)
    assert str(caught.value) == "not a file read"


def test_no_state_or_nothing_read_gives_no_record() -> None:
    context._set(None)
    assert context.files_read_payload() is None
    context._set(_state())
    try:
        assert context.files_read_payload() is None
    finally:
        context._set(None)


def test_the_record_is_newline_delimited_json_with_a_utc_watermark() -> None:
    """F3: a naive `timestamp` watermark is UTC; each file once; text kept as UTF-8."""
    one = context.ReadFile("exports", "naïve.csv", 5, _MARK, '"e"')
    two = context.ReadFile("exports", "b.csv", 0, _MARK)
    context._set(_state(target_watermark=_MARK.replace(tzinfo=None), files_read=[one, two, one]))
    try:
        payload = context.files_read_payload()
    finally:
        context._set(None)
    assert payload is not None
    assert payload.endswith(b"\n")
    lines = payload.decode().splitlines()
    assert [json.loads(line) for line in lines] == [
        {"version": 1, "watermark": "2026-11-02T12:00:00+00:00"},
        one.to_json(),
        two.to_json(),
    ]
    assert "naïve.csv" in lines[1]


def test_an_aware_watermark_is_sent_in_utc_and_none_as_null() -> None:
    plus_two = dt.timezone(dt.timedelta(hours=2))
    file = context.ReadFile("exports", "a.csv", 5, _MARK)
    for mark, sent in ((_MARK.astimezone(plus_two), _MARK.isoformat()), (None, None)):
        context._set(_state(target_watermark=mark, files_read=[file]))
        try:
            payload = context.files_read_payload()
        finally:
            context._set(None)
        assert payload is not None
        assert json.loads(payload.splitlines()[0]) == {"version": 1, "watermark": sent}
