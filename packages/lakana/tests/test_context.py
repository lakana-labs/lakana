"""`lakana.context`: what `ctx.watermark()` returns, and the state the helpers share."""

from __future__ import annotations

import datetime as dt
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
