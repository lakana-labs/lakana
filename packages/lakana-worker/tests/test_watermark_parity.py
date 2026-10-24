"""The harness decodes a watermark exactly as the catalog encodes it.

The worker sends the watermark it read (`lakana_core.catalog.watermark.Watermark.encode`) in
the task description; the harness (`lakana.context.decode_watermark`, in the SDK, which must not
import core) hands the decoded value to the asset as `ctx.watermark()`. This test lives in the
worker's tests because it needs both packages.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from lakana.context import WATERMARK_TAGS, decode_watermark
from lakana_core.catalog.watermark import INCREMENTAL_KEY_TYPES, Watermark

_VALUES = [
    Watermark("int", 42),
    Watermark("long", 9007199254740993),
    Watermark("decimal", Decimal("12.50")),
    Watermark("decimal", Decimal("-0.001")),
    Watermark("date", dt.date(2026, 10, 22)),
    Watermark("timestamp", dt.datetime(2026, 10, 22, 8, 0, 0, 123456)),
    Watermark("timestamptz", dt.datetime(2026, 10, 22, 8, tzinfo=dt.UTC)),
]


@pytest.mark.parametrize("watermark", _VALUES, ids=lambda w: w.encode())
def test_the_harness_decodes_what_the_catalog_encodes(watermark: Watermark) -> None:
    text = watermark.encode()
    decoded = decode_watermark(text)
    assert decoded == Watermark.decode(text).value == watermark.value
    assert type(decoded) is type(watermark.value)
    if isinstance(watermark.value, dt.datetime):
        assert isinstance(decoded, dt.datetime)
        assert decoded.tzinfo == watermark.value.tzinfo


def test_the_two_sides_know_the_same_tags() -> None:
    assert WATERMARK_TAGS == INCREMENTAL_KEY_TYPES
