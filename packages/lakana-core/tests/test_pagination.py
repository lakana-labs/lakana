"""The cursor of the newest-first lists: one codec for checks, runs, queries and deploys."""

from __future__ import annotations

import base64
import enum
import uuid
from datetime import UTC, datetime, timedelta, timezone

import pytest

from lakana_core.errors import ValidationError
from lakana_core.pagination import (
    _BAD_CURSOR,
    _OTHER_LIST,
    Position,
    decode_cursor,
    decode_key_cursor,
    encode_cursor,
    encode_key_cursor,
    parse_choices,
)

_AT = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)


def _raw(payload: bytes) -> str:
    """Encode a hand-made cursor body the way the codec does.

    Parameters
    ----------
    payload
        The JSON bytes.

    Returns
    -------
    str
        Unpadded base64url.
    """
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode()


def test_a_cursor_round_trips_its_position() -> None:
    row_id = uuid.uuid4()
    cursor = encode_cursor("runs", _AT, row_id)
    assert decode_cursor("runs", cursor) == Position(at=_AT, id=row_id, part=0)


def test_a_cursor_keeps_its_part_and_normalises_the_zone_to_utc() -> None:
    row_id = uuid.uuid4()
    local = _AT.astimezone(timezone(timedelta(hours=2)))
    position = decode_cursor("queries", encode_cursor("queries", local, row_id, part=2))
    assert position == Position(at=_AT, id=row_id, part=2)
    assert position.at.utcoffset() == timedelta(0)


def test_a_cursor_of_another_list_is_refused_and_names_both_lists() -> None:
    cursor = encode_cursor("checks", _AT, uuid.uuid4())
    with pytest.raises(ValidationError) as caught:
        decode_cursor("runs", cursor)
    message = str(caught.value)
    assert "cursor is not valid" in message
    assert "`checks` list" in message
    assert "`runs` list" in message
    assert "Start the listing again" in message


@pytest.mark.parametrize(
    "cursor",
    [
        "!!not-base64",
        _raw(b'"a string"'),
        _raw(b'{"k": "runs", "o": 1, "i": "x"}'),
        _raw(b'{"k": "runs", "o": "2026-10-11T12:00:00", "i": "' + b"0" * 32 + b'"}'),
        _raw(b'{"k": "runs", "o": "not a time", "i": "x"}'),
        _raw(b'{"k": "runs", "o": "0001-01-01T00:00:00+23:00", "i": "' + b"0" * 32 + b'"}'),
        _raw(b'{"o": "2026-10-11T12:00:00+00:00", "i": "' + b"0" * 32 + b'"}'),
        _raw(
            b'{"k": "runs", "o": "2026-10-11T12:00:00+00:00", "i": "' + b"0" * 32 + b'", "p": "x"}'
        ),
    ],
)
def test_a_cursor_that_was_not_issued_is_refused(cursor: str) -> None:
    with pytest.raises(ValidationError, match="Start the listing again"):
        decode_cursor("runs", cursor)


class _Colour(enum.StrEnum):
    """Two members to choose from."""

    RED = "red"
    BLUE = "blue"


def test_parse_choices_returns_the_members_in_the_order_given() -> None:
    assert parse_choices("colour", ["blue", "red"], _Colour) == (_Colour.BLUE, _Colour.RED)
    assert parse_choices("colour", [], _Colour) == ()


def test_parse_choices_names_the_parameter_the_value_and_the_allowed_values() -> None:
    with pytest.raises(ValidationError) as caught:
        parse_choices("colour", ["red", "green"], _Colour)
    message = str(caught.value)
    assert "`colour`" in message
    assert "'green'" in message
    assert "red, blue" in message


def test_parse_choices_may_ignore_the_letter_case() -> None:
    class Upper(enum.StrEnum):
        """Members whose value is upper case."""

        QUEUED = "QUEUED"

    assert parse_choices("status", ["queued", "QUEUED"], Upper, any_case=True) == (
        Upper.QUEUED,
        Upper.QUEUED,
    )
    with pytest.raises(ValidationError, match="must be one of QUEUED"):
        parse_choices("status", ["nope"], Upper, any_case=True)
    with pytest.raises(ValidationError, match="must be one of QUEUED"):
        parse_choices("status", ["queued"], Upper)


def test_a_key_cursor_round_trips_and_refuses_another_list() -> None:
    assert decode_key_cursor("terms", encode_key_cursor("terms", "net_revenue")) == "net_revenue"
    with pytest.raises(ValidationError) as other:
        decode_key_cursor("terms", encode_cursor("runs", _AT, uuid.uuid4()))
    assert str(other.value) == _OTHER_LIST.format(expected="terms", actual="runs")
    nul, surrogate = (
        _raw(b'{"k": "terms", "n": "a\\u0000"}'),
        _raw(b'{"k": "terms", "n": "\\ud800"}'),
    )
    for bad in (
        "%%%",
        _raw(b"[1]"),
        _raw(b'{"k": 1}'),
        _raw(b'{"k": "terms"}'),
        _raw(b"{"),
        nul,
        surrogate,
    ):
        with pytest.raises(ValidationError) as caught:
            decode_key_cursor("terms", bad)
        assert str(caught.value) == _BAD_CURSOR


def test_a_key_of_non_ascii_names_is_encoded_compactly_and_reads_back() -> None:
    key = "\u00e9\U0001f600" * 100
    cursor = encode_key_cursor("terms", key)
    assert decode_key_cursor("terms", cursor) == key
    # UTF-8: two and four bytes a pair of characters, not the 6 + 12 of JSON escapes.
    assert len(cursor) < 4 * (len('{"k": "terms", "n": ""}') + 600) / 3 + 4


def test_a_key_with_a_lone_surrogate_encodes_and_is_refused_on_the_way_back() -> None:
    cursor = encode_key_cursor("terms", "a\ud800")
    with pytest.raises(ValidationError):
        decode_key_cursor("terms", cursor)
