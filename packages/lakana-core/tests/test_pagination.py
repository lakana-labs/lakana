"""The cursor of the newest-first lists: one codec for checks, runs, queries and deploys."""

from __future__ import annotations

import base64
import enum
import json
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta, timezone

import pytest

from lakana_core.errors import ValidationError
from lakana_core.pagination import (
    _BAD_CURSOR,
    _OTHER_LIST,
    BIGINT_KEY,
    SMALLINT_KEY,
    CursorValue,
    KeyType,
    Page,
    Position,
    SortedPosition,
    decode_cursor,
    decode_key_cursor,
    decode_sorted_cursor,
    encode_cursor,
    encode_key_cursor,
    encode_sorted_cursor,
    instant_key,
    page_in_order,
    parse_choices,
)

_AT = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
ID = uuid.UUID("00000000-0000-0000-0000-000000000042")
_KeyType = KeyType


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


@pytest.mark.parametrize(
    ("values", "types"),
    [
        (("orders", "silver", "dev"), (str, str, str)),
        ((42,), (int,)),
        ((None,), (int,)),
        ((datetime(2026, 11, 12, tzinfo=UTC),), (datetime,)),
    ],
)
def test_a_sorted_cursor_round_trips_typed_values(
    values: tuple[CursorValue, ...], types: tuple[_KeyType, ...]
) -> None:
    c = encode_sorted_cursor("tables", "rows:desc", values, ID)
    assert decode_sorted_cursor("tables", "rows:desc", c, types) == SortedPosition(values, ID)


def test_a_cursor_of_another_sort_is_refused_naming_both() -> None:
    c = encode_sorted_cursor("tables", "rows:desc", (1,), ID)
    with pytest.raises(ValidationError, match=r"sorted by `size:desc`.*sorted by `rows:desc`"):
        decode_sorted_cursor("tables", "size:desc", c, (int,))


def test_a_value_of_the_wrong_type_is_refused() -> None:
    c = encode_sorted_cursor("tables", "rows:desc", ("x",), ID)
    with pytest.raises(ValidationError, match="cursor is not valid"):
        decode_sorted_cursor("tables", "rows:desc", c, (int,))


def test_a_sorted_cursor_of_another_list_is_refused_naming_both() -> None:
    c = encode_cursor("runs", _AT, ID)
    with pytest.raises(ValidationError, match=r"`tables` list: it came from the `runs` list"):
        decode_sorted_cursor("tables", "rows:desc", c, (int,))


def test_a_sorted_cursor_keeps_the_time_in_utc() -> None:
    local = datetime(2026, 11, 12, 14, tzinfo=timezone(timedelta(hours=2)))
    c = encode_sorted_cursor("tables", "created:desc", (local,), ID)
    at = decode_sorted_cursor("tables", "created:desc", c, (datetime,)).values[0]
    assert at == local
    assert isinstance(at, datetime) and at.utcoffset() == timedelta(0)


_GOOD_ID = '"' + str(ID) + '"'


@pytest.mark.parametrize(
    "body",
    [
        b"!",
        b'"a string"',
        b'{"k": "tables", "s": 1, "v": [], "i": ' + _GOOD_ID.encode() + b"}",
        b'{"k": "tables", "s": "rows:desc", "v": {}, "i": ' + _GOOD_ID.encode() + b"}",
        b'{"k": "tables", "s": "rows:desc", "v": [["i", 1]], "i": 7}',
        b'{"k": "tables", "s": "rows:desc", "v": [["i", 1]], "i": "not an id"}',
        b'{"k": "tables", "s": "rows:desc", "v": [], "i": ' + _GOOD_ID.encode() + b"}",
        b'{"k": "tables", "s": "rows:desc", "v": [["i", 1], ["i", 2]], "i": '
        + _GOOD_ID.encode()
        + b"}",
        b'{"k": "tables", "s": "rows:desc", "v": [["i"]], "i": ' + _GOOD_ID.encode() + b"}",
        b'{"k": "tables", "s": "rows:desc", "v": ["i"], "i": ' + _GOOD_ID.encode() + b"}",
        b'{"k": "tables", "s": "rows:desc", "v": [["i", true]], "i": ' + _GOOD_ID.encode() + b"}",
        b'{"k": "tables", "s": "rows:desc", "v": [["n", 1]], "i": ' + _GOOD_ID.encode() + b"}",
        b'{"k": "tables", "s": "rows:desc", "v": [["x", 1]], "i": ' + _GOOD_ID.encode() + b"}",
        b'{"k": "tables", "s": "rows:desc", "v": [["t", 1]], "i": ' + _GOOD_ID.encode() + b"}",
    ],
)
def test_a_sorted_cursor_that_was_not_issued_is_refused(body: bytes) -> None:
    with pytest.raises(ValidationError, match="Start the listing again"):
        decode_sorted_cursor("tables", "rows:desc", _raw(body), (int,))


@pytest.mark.parametrize(
    ("value", "type_"),
    [
        ('["t", "2026-11-12T00:00:00"]', datetime),
        ('["t", "not a time"]', datetime),
        ('["t", "9999-12-31T23:59:59-05:00"]', datetime),
        ('["t", "0001-01-01T00:00:00+05:00"]', datetime),
        ('["s", "a\\tb"]', str),
        ('["s", "a\\u007fb"]', str),
        ('["s", "a\\u0000b"]', str),
        ('["s", "a\\ud800"]', str),
        ('["s", 1]', str),
        ('["i", "2026-11-12T00:00:00+00:00"]', int),
        ('["i", 1]', str),
        ('["s", "2026-11-12T00:00:00+00:00"]', datetime),
    ],
)
def test_a_sorted_cursor_value_that_was_not_issued_is_refused(value: str, type_: _KeyType) -> None:
    body = '{"k": "tables", "s": "name:asc", "v": [' + value + '], "i": ' + _GOOD_ID + "}"
    with pytest.raises(ValidationError, match="cursor is not valid"):
        decode_sorted_cursor("tables", "name:asc", _raw(body.encode()), (type_,))


@pytest.mark.parametrize(
    ("value", "type_", "ok"),
    [
        (2**15 - 1, SMALLINT_KEY, True),
        (-(2**15), SMALLINT_KEY, True),
        (2**15, SMALLINT_KEY, False),
        (-(2**15) - 1, SMALLINT_KEY, False),
        (2**63 - 1, BIGINT_KEY, True),
        (-(2**63), BIGINT_KEY, True),
        (2**63, BIGINT_KEY, False),
        (-(2**63) - 1, BIGINT_KEY, False),
        (2**63, int, False),
        (2**63 - 1, int, True),
    ],
)
def test_an_integer_key_holds_only_what_its_column_holds(
    value: int, type_: _KeyType, ok: bool
) -> None:
    c = encode_sorted_cursor("tables", "rows:desc", (value,), ID)
    if ok:
        assert decode_sorted_cursor("tables", "rows:desc", c, (type_,)).values == (value,)
    else:
        with pytest.raises(ValidationError, match="cursor is not valid"):
            decode_sorted_cursor("tables", "rows:desc", c, (type_,))


def test_an_integer_key_wants_the_integer_tag() -> None:
    c = encode_sorted_cursor("tables", "health:asc", ("1",), ID)
    with pytest.raises(ValidationError, match="cursor is not valid"):
        decode_sorted_cursor("tables", "health:asc", c, (SMALLINT_KEY,))


@pytest.mark.parametrize("character", ["\x00", "\t", "\x1f", "\x7f", "\ud800"])
def test_both_cursor_decoders_refuse_the_same_characters(character: str) -> None:
    text = f"a{character}b"
    with pytest.raises(ValidationError, match="cursor is not valid"):
        decode_key_cursor("terms", encode_key_cursor("terms", text))
    with pytest.raises(ValidationError, match="cursor is not valid"):
        decode_sorted_cursor(
            "tables", "name:asc", encode_sorted_cursor("tables", "name:asc", (text,), ID), (str,)
        )


def _page(
    items: Sequence[str],
    *,
    cursor: str | None = None,
    limit: int = 2,
    descending: bool = False,
    kind: str = "things",
) -> Page[str]:
    return page_in_order(
        items,
        kind=kind,
        key_of=lambda s: (s,),
        limit=limit,
        cursor=cursor,
        descending=descending,
    )


def test_page_in_order_pages_one_by_one_without_repeating_or_skipping() -> None:
    items = ["a", "b", "c", "d", "e"]
    seen, cursor, sizes = [], None, []
    while True:
        page = _page(items, cursor=cursor)
        sizes.append(len(page.items))
        seen += page.items
        cursor = page.next_cursor
        if cursor is None:
            break
    assert sizes == [2, 2, 1]
    assert seen == items


def test_page_in_order_empty_exact_and_boundary() -> None:
    assert _page([]) == ([], None)
    assert _page(["a", "b"]) == (["a", "b"], None)
    page = _page(["a", "b", "c"])
    assert page.items == ["a", "b"]
    assert page.next_cursor is not None


def test_page_in_order_cursor_past_the_end() -> None:
    cursor = _page(["a", "b", "c"]).next_cursor
    assert _page(["a"], cursor=cursor) == ([], None)


def test_page_in_order_neither_repeats_nor_skips_when_the_last_row_vanishes() -> None:
    cursor = _page(["a", "b", "c", "d"]).next_cursor
    assert _page(["a", "c", "d"], cursor=cursor) == (["c", "d"], None)


def test_page_in_order_descending() -> None:
    first = _page(["a", "b", "c", "d"], descending=True)
    assert first.items == ["d", "c"]
    second = _page(["a", "b", "c", "d"], descending=True, cursor=first.next_cursor)
    assert second == (["b", "a"], None)


def test_page_in_order_sorts_by_the_key_itself() -> None:
    first = _page(["c", "a", "d", "b"])
    assert first.items == ["a", "b"]
    assert _page(["c", "a", "d", "b"], cursor=first.next_cursor).items == ["c", "d"]


def test_page_in_order_breaks_equal_primary_keys_by_the_tiebreak() -> None:
    rows = [("t1", "b"), ("t1", "a"), ("t1", "c")]
    first = page_in_order(rows, kind="k", key_of=lambda r: r, limit=2, cursor=None)
    assert first.items == [("t1", "a"), ("t1", "b")]
    second = page_in_order(rows, kind="k", key_of=lambda r: r, limit=2, cursor=first.next_cursor)
    assert second == ([("t1", "c")], None)


def test_page_in_order_refuses_a_key_that_is_not_unique() -> None:
    with pytest.raises(ValueError, match="unique"):
        _page(["a", "a"])


@pytest.mark.parametrize("limit", [0, -1, 201])
def test_page_in_order_refuses_a_limit_out_of_range(limit: int) -> None:
    with pytest.raises(ValidationError, match=rf"limit must be between 1 and 200; got {limit}\."):
        _page(["a"], limit=limit)


def test_a_cursor_of_another_list_is_refused() -> None:
    cursor = _page(["a", "b", "c"], kind="orphans").next_cursor
    with pytest.raises(ValidationError) as exc:
        _page(["a"], cursor=cursor, kind="artifacts")
    assert str(exc.value) == _OTHER_LIST.format(expected="artifacts", actual="orphans")


def _forged(body: object) -> str:
    return base64.urlsafe_b64encode(json.dumps(body).encode()).rstrip(b"=").decode()


@pytest.mark.parametrize(
    "cursor",
    [
        base64.urlsafe_b64encode(b"x").decode(),
        _forged({"k": "things", "n": "[1]"}),
        _forged({"k": "things", "n": "not json"}),
        _forged({"k": "things", "n": '{"a": "b"}'}),
        "%%%",
    ],
)
def test_a_forged_cursor_is_refused(cursor: str) -> None:
    with pytest.raises(ValidationError) as exc:
        _page(["a"], cursor=cursor)
    assert str(exc.value) == _BAD_CURSOR
    assert "without `cursor`" in str(exc.value)


def test_a_variable_length_key_orders_like_its_tuple() -> None:
    rows = [("ab",), ("a", "b"), ("a",)]
    first = page_in_order(rows, kind="k", key_of=lambda r: r, limit=2, cursor=None)
    assert first.items == [("a",), ("a", "b")]
    second = page_in_order(rows, kind="k", key_of=lambda r: r, limit=2, cursor=first.next_cursor)
    assert second == ([("ab",)], None)


def test_instant_key_orders_like_the_times() -> None:
    plus2 = timezone(timedelta(hours=2))
    times = [
        datetime(2026, 1, 1, 10, 0, 0, 5, tzinfo=UTC),
        datetime(2026, 1, 1, 10, 0, 0, tzinfo=UTC),
        datetime(2026, 1, 1, 11, 0, 0, 1, tzinfo=plus2),
        datetime(2026, 1, 1, 12, 0, 0, tzinfo=plus2),
    ]
    assert sorted(times, key=instant_key) == sorted(times)
    assert instant_key(times[2]) == "2026-01-01T09:00:00.000001Z"


def test_a_name_is_ordered_in_lower_case_and_by_code_point_on_postgres() -> None:
    """Postgres is told ``"C"``; SQLite compares by code point already. No connection is opened."""
    import sqlalchemy as sa
    from sqlalchemy.orm import Session

    from lakana_core.models import Principal
    from lakana_core.pagination import name_order

    for url, collated in (("sqlite://", False), ("postgresql+pg8000://lakana@localhost/l", True)):
        engine = sa.create_engine(url)
        try:
            with Session(engine) as session:
                compiled = str(name_order(session, Principal.name).compile(dialect=engine.dialect))
        finally:
            engine.dispose()
        assert compiled.startswith("lower(principals.name)")
        assert compiled.endswith('COLLATE "C"') is collated


def test_a_sorted_page_refuses_a_limit_below_one() -> None:
    import sqlalchemy as sa
    from sqlalchemy.orm import Session

    from lakana_core.models import Principal
    from lakana_core.pagination import ListSort, SortField, SortKey, sorted_page

    engine = sa.create_engine("sqlite://")
    try:
        with (
            Session(engine) as session,
            pytest.raises(ValidationError, match="limit must be at least 1"),
        ):
            sorted_page(
                session,
                sa.select(Principal),
                kind="principals",
                sort=ListSort("name", False),
                field=SortField((SortKey(Principal.name, str),), descending=False),
                row_id=Principal.id,
                limit=0,
                cursor=None,
            )
    finally:
        engine.dispose()


def test_the_longest_cursors_fit_the_routes_limit() -> None:
    """Keys of four-byte characters at every name's longest still fit `CURSOR_MAX_LENGTH`."""
    from lakana_core.models import Environment, Principal, Repository, Run
    from lakana_core.models.saved_query import NAME_KEY_MAX
    from lakana_core.pagination import (
        CURSOR_MAX_LENGTH,
        LONGEST_KEY_CHARS,
        decode_tuple_cursor,
        encode_tuple_cursor,
    )

    def length(column: object) -> int:
        """Read a text column's most characters.

        Parameters
        ----------
        column
            The mapped column.

        Returns
        -------
        int
            Its length.
        """
        found = getattr(getattr(column, "type", None), "length", None)
        assert isinstance(found, int)
        return found

    env_max = length(Environment.name)
    assert NAME_KEY_MAX + env_max == LONGEST_KEY_CHARS
    for column in (Principal.name, Repository.name, Run.job):
        assert length(column) <= LONGEST_KEY_CHARS
    wide = "\U0001d538"  # four bytes in UTF-8
    saved = encode_tuple_cursor("all-saved-queries", (wide * NAME_KEY_MAX, wide * env_max, str(ID)))
    assert len(saved) <= CURSOR_MAX_LENGTH
    assert decode_tuple_cursor("all-saved-queries", saved)[0] == wide * NAME_KEY_MAX
    run = encode_sorted_cursor("all-runs", "name:desc", [wide * 255, _AT], ID)
    assert len(run) <= CURSOR_MAX_LENGTH
    assert (
        decode_sorted_cursor("all-runs", "name:desc", run, [str, datetime]).values[0] == wide * 255
    )
    # A name of 45 Cyrillic letters once made a cursor of 532 characters, past the old 512.
    assert len(encode_sorted_cursor("runs", "name:asc", ["ж" * 45, _AT], ID)) < 512


def test_the_longest_namespace_and_table_cursors_fit_their_routes_limits() -> None:
    """The widest key of each list stays within the `cursor` its route takes (#804)."""
    from lakana_core.models import Namespace, Repository, Table
    from lakana_core.pagination import (
        NAMESPACE_CURSOR_MAX_LENGTH,
        TABLE_CURSOR_MAX_LENGTH,
        decode_tuple_cursor,
        encode_tuple_cursor,
    )

    path_max = Namespace.path.type.length
    name_max = Table.name.type.length
    repository_max = Repository.name.type.length
    assert path_max == 1024
    assert name_max == 255
    assert repository_max <= 255

    wide = "\U0001d538"  # four bytes in UTF-8
    for letter in (wide, "ж", "a", '"', "\\"):
        # One part of the longest path, and the most parts of one character (the unit separator
        # between them takes a character each).
        for parts in ((letter * path_max,), (letter,) * ((path_max + 1) // 2)):
            cursor = encode_tuple_cursor("namespaces", parts)
            assert len(cursor) <= NAMESPACE_CURSOR_MAX_LENGTH, (letter, len(parts))
            assert decode_tuple_cursor("namespaces", cursor) == parts
        artifact = (letter * path_max, "", letter * name_max)
        expiring = ("2026-10-11T12:00:00.000000+00:00", letter * (path_max + 1 + name_max), str(ID))
        orphan = (letter * repository_max, letter * (path_max + 1 + name_max))
        for kind, key in (
            ("artifacts", artifact),
            ("expiring_tables", expiring),
            ("orphans", orphan),
        ):
            cursor = encode_tuple_cursor(kind, key)
            assert len(cursor) <= TABLE_CURSOR_MAX_LENGTH, (kind, letter)
            assert decode_tuple_cursor(kind, cursor) == key


def test_a_namespace_cursor_issued_before_still_decodes() -> None:
    """The cursor's format did not change: one an older server issued reads back."""
    from lakana_core.pagination import decode_tuple_cursor

    issued = (
        base64.urlsafe_b64encode(
            json.dumps({"k": "namespaces", "n": json.dumps(["gold", "sales"])}).encode()
        )
        .rstrip(b"=")
        .decode()
    )
    assert decode_tuple_cursor("namespaces", issued) == ("gold", "sales")
