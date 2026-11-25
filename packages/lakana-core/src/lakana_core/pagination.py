"""What the paged lists share: the cursor, the page, filters and a name search.

The lists are checks, runs, queries, deploys, grants, principals and groups. A cursor is the
position of the last item of a page: its time and its id, and the list it belongs to. A list
resumes strictly after that position, so an item that arrives while a client pages is never
repeated and none is skipped. The kind of the list is part of the cursor, so a cursor of one list
is refused by another instead of being misread.

A list ordered by a unique key instead (the glossary's terms) has a cursor of that key and the
list's kind, with the same refusal of another list's cursor.

A list the caller sorts by a key of their choice (the tables) has a cursor of that key's values,
the item's id, the list's kind and the sort, so a cursor of another sort is refused too. The
newest-first lists take a chosen sort as well (`parse_sort`, `sorted_page`): in their default
order they keep the cursor of a time and an id, in any other the sorted cursor, and each refuses
the other's.
"""

from __future__ import annotations

import base64
import binascii
import enum
import itertools
import json
import math
import unicodedata
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Generic, NamedTuple, TypeVar

from sqlalchemy import ColumnElement, Select, String, and_, func, literal, or_, true
from sqlalchemy.orm import InstrumentedAttribute, Session

from lakana_core.errors import ValidationError
from lakana_core.models.namespace import NAMESPACE_PATH_MAX
from lakana_core.models.table import TABLE_NAME_MAX

_E = TypeVar("_E", bound=enum.StrEnum)
_T = TypeVar("_T")

BAD_CURSOR = (
    "The cursor is not valid. Start the listing again without `cursor`; pass the `next_cursor` of "
    "a page only to continue that listing."
)
"""Why a cursor no list of this server could have issued is refused."""
_BAD_CURSOR = BAD_CURSOR
_OTHER_LIST = (
    "The cursor is not valid for the `{expected}` list: it came from the `{actual}` list. "
    "Start the listing again without `cursor`; pass the `next_cursor` of a page only to "
    "continue that listing."
)
_OTHER_SORT = (
    "The cursor is not valid for the list sorted by `{expected}`: it came from the list sorted "
    "by `{actual}`. Start the listing again without `cursor`."
)
_SORTED_NOT_DEFAULT = (
    "The cursor is not valid for the list in its default order: it came from the list sorted by "
    "`{actual}`. Send the same `sort` and `order` with every page, or start the listing again "
    "without `cursor`."
)
_DEFAULT_NOT_SORTED = (
    "The cursor is not valid for the list sorted by `{expected}`: it came from the list in its "
    "default order. Send the same `sort` and `order` with every page, or start the listing again "
    "without `cursor`."
)


@dataclass(frozen=True)
class Position:
    """Where a page ended.

    Parameters
    ----------
    at
        The time of the last item (UTC).
    id
        Its id.
    part
        Which part of a list made of several the item is in; 0 for a list of one part.
    """

    at: datetime
    id: uuid.UUID
    part: int = 0


class Page(NamedTuple, Generic[_T]):
    """One page of a list, and where the next one starts.

    Parameters
    ----------
    items
        The rows of this page.
    next_cursor
        The cursor of the next page, or `None` on the last.
    """

    items: list[_T]
    next_cursor: str | None


def encode_cursor(kind: str, at: datetime, row_id: uuid.UUID, part: int = 0) -> str:
    """Encode the position of a page's last item as an opaque cursor.

    Parameters
    ----------
    kind
        The list the cursor belongs to (``checks``, ``runs``, ``task-history``, ``queries``,
        ``deploys``, ``grants``, ``principals`` or ``groups``).
    at
        The time the list is ordered by, of the last item (timezone-aware).
    row_id
        The item's id.
    part
        The part of the list the item is in, for a list that is read in parts.

    Returns
    -------
    str
        Unpadded base64url of ``{"k": kind, "o": at, "i": id}``, with ``"p"`` when `part` is not 0.
    """
    body: dict[str, str | int] = {"k": kind, "o": at.isoformat(), "i": str(row_id)}
    if part:
        body["p"] = part
    return base64.urlsafe_b64encode(json.dumps(body).encode()).rstrip(b"=").decode()


def decode_cursor(kind: str, cursor: str) -> Position:
    """Decode a cursor produced by `encode_cursor` for the list `kind`.

    Parameters
    ----------
    kind
        The list being read.
    cursor
        The value a client sent back.

    Returns
    -------
    Position
        The position to resume after.

    Raises
    ------
    ValidationError
        If `cursor` is not one this server could have issued, belongs to another list, or came
        from the list sorted by a chosen key (`sorted_page`).
    """
    try:
        data = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        if isinstance(data, dict) and data.get("k") == kind and isinstance(data.get("s"), str):
            raise ValidationError(_SORTED_NOT_DEFAULT.format(actual=data["s"]))
        if not (
            isinstance(data, dict)
            and isinstance(data.get("k"), str)
            and isinstance(data.get("o"), str)
            and isinstance(data.get("i"), str)
            and isinstance(data.get("p", 0), int)
        ):
            raise ValidationError(_BAD_CURSOR)
        at = datetime.fromisoformat(data["o"])
        if at.tzinfo is None:
            raise ValidationError(_BAD_CURSOR)
        at = at.astimezone(UTC)
        row_id = uuid.UUID(data["i"])
    except (binascii.Error, ValueError, OverflowError) as exc:
        raise ValidationError(_BAD_CURSOR) from exc
    if data["k"] != kind:
        raise ValidationError(_OTHER_LIST.format(expected=kind, actual=data["k"]))
    return Position(at=at, id=row_id, part=data.get("p", 0))


def parse_choices(
    name: str, values: Sequence[str], members: type[_E], *, any_case: bool = False
) -> tuple[_E, ...]:
    """Turn the values of a filter into members of an enum, or refuse the first that is none.

    Parameters
    ----------
    name
        The parameter, as the caller wrote it.
    values
        The values sent.
    members
        The enum they must be a value of.
    any_case
        Whether a value may be written in any letter case (``queued`` for ``QUEUED``).

    Returns
    -------
    tuple of enum.StrEnum
        The members, in the order given.

    Raises
    ------
    ValidationError
        If a value is not one of the enum; the message names the parameter and the allowed
        values.
    """
    allowed = {(m.value.casefold() if any_case else m.value): m for m in members}
    chosen: list[_E] = []
    for value in values:
        key = value.casefold() if any_case else value
        if key not in allowed:
            names = ", ".join(m.value for m in members)
            raise ValidationError(f"Unknown {name} {value!r}: `{name}` must be one of {names}.")
        chosen.append(allowed[key])
    return tuple(chosen)


def newest_first(
    session: Session,
    query: Select[Any],
    *,
    kind: str,
    at: InstrumentedAttribute[datetime],
    row_id: InstrumentedAttribute[uuid.UUID],
    at_of: Callable[[Any], datetime],
    limit: int,
    cursor: str | None,
) -> Page[Any]:
    """Read one page of a list ordered by a time, newest first, equal times by id.

    Parameters
    ----------
    session
        Database session.
    query
        The rows of the list, already filtered; one entity per row.
    kind
        The list, as the cursor names it.
    at
        The column the list is ordered by.
    row_id
        The id column, which breaks ties.
    at_of
        Reads the time of a row, for the cursor of the next page.
    limit
        Most rows returned.
    cursor
        The `next_cursor` of the previous page, or `None` for the first.

    Returns
    -------
    Page
        The rows, and the cursor of the next page (`None` on the last).

    Raises
    ------
    ValidationError
        If `limit` is below 1 or `cursor` is not one this server issued for this list.
    """
    if limit < 1:
        raise ValidationError(f"limit must be at least 1, got {limit}.")
    if cursor is not None:
        position = decode_cursor(kind, cursor)
        query = query.where(or_(at < position.at, and_(at == position.at, row_id < position.id)))
    rows = list(
        session.execute(query.order_by(at.desc(), row_id.desc()).limit(limit + 1)).scalars()
    )
    page = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
        next_cursor = encode_cursor(kind, at_of(page[-1]), page[-1].id)
    return Page(page, next_cursor)


def _cursor_body(kind: str, cursor: str) -> dict[str, object]:
    """Read the JSON body of a key or sorted cursor and check the list it names.

    Parameters
    ----------
    kind
        The list being read.
    cursor
        The value a client sent back.

    Returns
    -------
    dict
        The body; its ``k`` is `kind`.

    Raises
    ------
    ValidationError
        If `cursor` is no base64url JSON object naming a list, or names another list.
    """
    try:
        data = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
    except (binascii.Error, ValueError) as exc:
        raise ValidationError(_BAD_CURSOR) from exc
    if not isinstance(data, dict) or not isinstance(data.get("k"), str):
        raise ValidationError(_BAD_CURSOR)
    if data["k"] != kind:
        raise ValidationError(_OTHER_LIST.format(expected=kind, actual=data["k"]))
    return data


def encode_key_cursor(kind: str, key: str) -> str:
    """Encode the key of a page's last item, for a list ordered by a unique key.

    Parameters
    ----------
    kind
        The list the cursor belongs to (``terms``).
    key
        The last item's key.

    Returns
    -------
    str
        Unpadded base64url of the UTF-8 JSON ``{"k": kind, "n": key}``.
    """
    body = json.dumps({"k": kind, "n": key}, ensure_ascii=False)
    # UTF-8 rather than JSON escapes: a key of non-ASCII names is at most four bytes a character,
    # not twelve. A lone surrogate, which no stored key holds, encodes as itself and reads back
    # as a cursor that is not valid.
    return base64.urlsafe_b64encode(body.encode("utf-8", "surrogatepass")).rstrip(b"=").decode()


def decode_key_cursor(kind: str, cursor: str) -> str:
    """Decode a cursor produced by `encode_key_cursor` for the list `kind`.

    Parameters
    ----------
    kind
        The list being read.
    cursor
        The value a client sent back.

    Returns
    -------
    str
        The key to resume after.

    Raises
    ------
    ValidationError
        If `cursor` is not one this server could have issued, belongs to another list, or came
        from the list sorted by a chosen key (`sorted_page`).
    """
    body = _cursor_body(kind, cursor)
    if isinstance(body.get("s"), str):
        raise ValidationError(_SORTED_NOT_DEFAULT.format(actual=body["s"]))
    key = body.get("n")
    if not isinstance(key, str):
        raise ValidationError(_BAD_CURSOR)
    return _stored_text(key)


def _stored_text(value: str) -> str:
    """Refuse a cursor's text that no stored name or key can hold.

    Parameters
    ----------
    value
        A text a client sent back in a cursor.

    Returns
    -------
    str
        `value`, unchanged.

    Raises
    ------
    ValidationError
        If it holds a control character or a lone surrogate: names and keys never do, and a NUL
        or a lone surrogate is one the database driver could not take.
    """
    if any(unicodedata.category(c) in ("Cc", "Cs") for c in value):
        raise ValidationError(_BAD_CURSOR)
    return value


def by_key(
    session: Session,
    query: Select[Any],
    *,
    kind: str,
    key: InstrumentedAttribute[str],
    key_of: Callable[[Any], str],
    limit: int,
    cursor: str | None,
) -> Page[Any]:
    """Read one page of a list ordered by a unique key, ascending.

    Unlike `newest_first`, no tie can occur, so the cursor holds the key alone.

    Parameters
    ----------
    session
        The session the query runs in.
    query
        What to page through, filters applied, selecting one entity.
    kind
        The list's name in the cursor (``terms``).
    key
        The unique column that orders the list.
    key_of
        Gives an entity's key, which the next cursor holds.
    limit
        The page size; at least 1.
    cursor
        Where the previous page ended, as it returned it; `None` starts at the first key.

    Returns
    -------
    Page
        This page's entities and, unless it is the last, the cursor that continues it.

    Raises
    ------
    ValidationError
        If `limit` is under 1, or `cursor` is malformed or was issued for another list.
    """
    if limit < 1:
        raise ValidationError(f"limit must be at least 1, got {limit}.")
    if cursor is not None:
        query = query.where(key > decode_key_cursor(kind, cursor))
    rows = list(session.execute(query.order_by(key).limit(limit + 1)).scalars())
    page = rows[:limit]
    next_cursor = encode_key_cursor(kind, key_of(page[-1])) if len(rows) > limit else None
    return Page(page, next_cursor)


CursorValue = str | int | datetime | None
"""One sort key of a row, as a sorted cursor holds it."""

_TAGS: dict[type, str] = {str: "s", int: "i", datetime: "t"}
"""The tag of each type a sorted cursor holds; ``n`` is null."""


class IntegerKey(NamedTuple):
    """An integer sort key, with the range its column holds.

    A cursor value outside it is refused: the database would refuse to compare with it.

    Parameters
    ----------
    low
        The smallest value.
    high
        The largest value.
    """

    low: int
    high: int


SMALLINT_KEY = IntegerKey(-(2**15), 2**15 - 1)
"""A key stored as a ``SMALLINT``."""

BIGINT_KEY = IntegerKey(-(2**63), 2**63 - 1)
"""A key stored as a ``BIGINT``; a key given as plain `int` holds this range too."""

KeyType = type[str] | type[int] | type[datetime] | IntegerKey
"""The type of one key of a sorted cursor."""


class SortedPosition(NamedTuple):
    """Where a page of a list sorted by chosen keys ended.

    Parameters
    ----------
    values
        The sort keys of the last item, in the order the list sorts by them; `None` for a null.
    id
        Its id, which breaks ties.
    """

    values: tuple[CursorValue, ...]
    id: uuid.UUID


def _tagged(value: CursorValue) -> list[str | int | None]:
    """Write one sort key with its type's tag.

    Parameters
    ----------
    value
        The key.

    Returns
    -------
    list
        ``[tag, value]``; a time as ISO text.
    """
    if value is None:
        return ["n", None]
    if isinstance(value, datetime):
        return ["t", value.isoformat()]
    if isinstance(value, str):
        return ["s", value]
    return ["i", value]


def encode_sorted_cursor(
    kind: str, sort: str, values: Sequence[CursorValue], row_id: uuid.UUID
) -> str:
    """Encode the position of a page's last item, for a list sorted by chosen keys.

    Parameters
    ----------
    kind
        The list the cursor belongs to (``tables``).
    sort
        The sort the page was read in, ``<key>:<asc|desc>``.
    values
        The item's sort keys, in order; a time must be timezone-aware.
    row_id
        The item's id.

    Returns
    -------
    str
        Unpadded base64url of ``{"k": kind, "s": sort, "v": [[tag, value], …], "i": id}``.
    """
    body = {"k": kind, "s": sort, "v": [_tagged(v) for v in values], "i": str(row_id)}
    # UTF-8 rather than JSON escapes, as `encode_key_cursor`: a non-ASCII name is at most four
    # bytes a character, not twelve, so the cursor stays within `CURSOR_MAX_LENGTH`.
    text = json.dumps(body, ensure_ascii=False)
    return base64.urlsafe_b64encode(text.encode("utf-8", "surrogatepass")).rstrip(b"=").decode()


def _untagged(item: object, wanted: KeyType) -> CursorValue:
    """Read one tagged sort key back, as the type the list expects.

    Parameters
    ----------
    item
        The ``[tag, value]`` pair a client sent back.
    wanted
        The type of the key at this place; an integer's range, or `int` for a ``BIGINT``.

    Returns
    -------
    str, int, datetime or None
        The key; a time in UTC.

    Raises
    ------
    ValidationError
        If the pair is malformed, of another type, or holds what no stored key does.
    """
    if not isinstance(item, list) or len(item) != 2:
        raise ValidationError(_BAD_CURSOR)
    tag, value = item
    if tag == "n" and value is None:
        return None
    if wanted is int or isinstance(wanted, IntegerKey):
        span = wanted if isinstance(wanted, IntegerKey) else BIGINT_KEY
        if (
            tag == "i"
            and isinstance(value, int)
            and not isinstance(value, bool)
            and span.low <= value <= span.high
        ):
            return value
        raise ValidationError(_BAD_CURSOR)
    if tag != _TAGS[wanted]:
        raise ValidationError(_BAD_CURSOR)
    if not isinstance(value, str):
        raise ValidationError(_BAD_CURSOR)
    if wanted is str:
        return _stored_text(value)
    try:
        at = datetime.fromisoformat(value)
        if at.tzinfo is None:
            raise ValidationError(_BAD_CURSOR)
        # A zone that moves a time past the first or last day the calendar holds overflows.
        return at.astimezone(UTC)
    except (ValueError, OverflowError) as exc:
        raise ValidationError(_BAD_CURSOR) from exc


def decode_sorted_cursor(
    kind: str,
    sort: str,
    cursor: str,
    types: Sequence[KeyType],
) -> SortedPosition:
    """Decode a cursor produced by `encode_sorted_cursor` for the list `kind` in the sort `sort`.

    Parameters
    ----------
    kind
        The list being read.
    sort
        The sort it is read in, ``<key>:<asc|desc>``.
    cursor
        The value a client sent back.
    types
        The type of each sort key, in order; an integer as the range of its column.

    Returns
    -------
    SortedPosition
        The position to resume after.

    Raises
    ------
    ValidationError
        If `cursor` is not one this server could have issued for these keys, or belongs to
        another list or another sort; the message says to start again without it.
    """
    data = _cursor_body(kind, cursor)
    if not isinstance(data.get("s"), str):
        raise ValidationError(_BAD_CURSOR)
    if data["s"] != sort:
        raise ValidationError(_OTHER_SORT.format(expected=sort, actual=data["s"]))
    values, raw_id = data.get("v"), data.get("i")
    if not isinstance(values, list) or len(values) != len(types) or not isinstance(raw_id, str):
        raise ValidationError(_BAD_CURSOR)
    try:
        row_id = uuid.UUID(raw_id)
    except ValueError as exc:
        raise ValidationError(_BAD_CURSOR) from exc
    return SortedPosition(
        tuple(_untagged(v, t) for v, t in zip(values, types, strict=True)), row_id
    )


def sorted_order(
    keys: Sequence[ColumnElement[Any]], row_id: ColumnElement[uuid.UUID], descending: bool
) -> list[ColumnElement[Any]]:
    """Order a list by its keys and then its id, all in one direction, a null key last.

    Parameters
    ----------
    keys
        The sort keys, most significant first.
    row_id
        The id column, which breaks ties.
    descending
        Whether the list runs from the largest key down.

    Returns
    -------
    list of ColumnElement
        The ``ORDER BY`` terms.
    """
    if descending:
        return [*(k.desc().nulls_last() for k in keys), row_id.desc()]
    return [*(k.asc().nulls_last() for k in keys), row_id.asc()]


def after_sorted(
    keys: Sequence[ColumnElement[Any]],
    row_id: ColumnElement[uuid.UUID],
    descending: bool,
    at: SortedPosition | None,
) -> ColumnElement[bool]:
    """Keep the rows strictly after a position, in the order of `sorted_order`.

    Only a list sorted by one key can meet a null: it sorts last in both directions. A list
    sorted by several keys sorts by keys that are never null.

    Parameters
    ----------
    keys
        The sort keys, most significant first.
    row_id
        The id column, which breaks ties.
    descending
        Whether the list runs from the largest key down.
    at
        Where the previous page ended; `None` for the first page.

    Returns
    -------
    ColumnElement
        The condition; true for every row on the first page.
    """
    if at is None:
        return true()

    def beyond(left: ColumnElement[Any], right: object) -> ColumnElement[bool]:
        """Compare in the direction of the list.

        Parameters
        ----------
        left
            The column.
        right
            The value of the position.

        Returns
        -------
        ColumnElement
            True where `left` comes after `right`.
        """
        return left < right if descending else left > right

    if len(keys) == 1:
        key, value = keys[0], at.values[0]
        if value is None:
            return and_(key.is_(None), beyond(row_id, at.id))
        return or_(beyond(key, value), and_(key == value, beyond(row_id, at.id)), key.is_(None))
    condition = beyond(row_id, at.id)
    for key, value in reversed(list(zip(keys, at.values, strict=True))):
        condition = or_(beyond(key, value), and_(key == value, condition))
    return condition


LONGEST_KEY_CHARS = 600 + 255
"""The most characters the text keys of one cursor hold together: a saved query's folded name
(`NAME_KEY_MAX`, 600) and its environment's name (255). Every other list's keys are shorter: a
run's job or repository name and a principal's name are 255, a connection's 63."""

CURSOR_MAX_LENGTH = 4 * math.ceil((4 * LONGEST_KEY_CHARS + 512) / 3)
"""The longest cursor a list issues: base64 of the keys' text at four UTF-8 bytes a character, and
512 bytes for the JSON around them (the list's kind, the sort, a time, an id). The routes of the
lists whose cursors hold names take a `cursor` this long."""


_BYTES_A_CHARACTER = 5
"""The most bytes one character of a key's names takes in a key cursor of a list ordered in the
database, the parts' own quotes and commas counted in. A letter is at most four bytes of UTF-8,
and so is a quote or a backslash once the JSON of the key is a string in the cursor's JSON. A
part of one such character, the shortest, adds five more bytes of quotes and comma, so the
worst key is a path of one-character parts: nine bytes for each two characters, which five a
character covers. A control character never counts: `decode_key_cursor` refuses one."""


def key_cursor_max_length(chars: int) -> int:
    """Give the longest cursor `encode_tuple_cursor` issues for keys of `chars` characters.

    Parameters
    ----------
    chars
        The most characters the key's names hold together, separators between parts included.

    Returns
    -------
    int
        Base64 of the names at their widest, and 512 bytes for the JSON around them (the list's
        kind, a time, an id and the quotes and commas between the parts).
    """
    return 4 * math.ceil((_BYTES_A_CHARACTER * chars + 512) / 3)


NAMESPACE_CURSOR_MAX_LENGTH = key_cursor_max_length(NAMESPACE_PATH_MAX)
"""The longest cursor of the namespaces list: the last path, at the longest the catalog stores
(`NAMESPACE_PATH_MAX`). The route takes a `cursor` this long."""

TABLE_CURSOR_MAX_LENGTH = key_cursor_max_length(NAMESPACE_PATH_MAX + 1 + TABLE_NAME_MAX + 255)
"""The longest cursor of a list of tables: a namespace path, a dot and a table name, and for the
orphans a repository's name (255) besides. The routes of the artifacts, expiring tables and
orphans take a `cursor` this long."""


class SortKey(NamedTuple):
    """One key a list sorted by a chosen field orders by.

    Parameters
    ----------
    expression
        What is compared: a column, or an expression of the listed row's columns.
    type
        The type of its value, as the cursor holds it.
    descending
        A direction of its own, whichever way the list runs (such as the newest first among
        runs of one state); `None` follows the list.
    nullable
        Whether it can be null; a null sorts last, in either direction.
    """

    expression: ColumnElement[Any] | InstrumentedAttribute[Any]
    type: KeyType
    descending: bool | None = None
    nullable: bool = False


class SortField(NamedTuple):
    """A field a list may be sorted by: its keys and the direction it runs in by default.

    Parameters
    ----------
    keys
        The keys, most significant first; the row's id breaks the last tie.
    descending
        Whether the field runs from the largest value down when no `order` is given.
    """

    keys: tuple[SortKey, ...]
    descending: bool


class ListSort(NamedTuple):
    """The sort a list is read in: a field of the list and a direction.

    Parameters
    ----------
    field
        The field's name, one of the list's.
    descending
        Whether the list runs from the largest value down.
    """

    field: str
    descending: bool

    @property
    def name(self) -> str:
        """The sort as a cursor holds and a message names it, ``<field>:<asc|desc>``.

        Returns
        -------
        str
            The name.
        """
        return f"{self.field}:{'desc' if self.descending else 'asc'}"


SORT_ORDERS = ("asc", "desc")
"""The values of a list's `order`."""


def parse_sort(
    sort: str | None, order: str | None, fields: Mapping[str, bool], default: str
) -> ListSort:
    """Read a list's `sort` and `order`, refusing a field or a direction the list does not have.

    Parameters
    ----------
    sort
        The field asked for; `None` for the list's default.
    order
        ``asc`` or ``desc``; `None` for the field's own direction.
    fields
        The list's fields, each with whether it runs from the largest value down by default.
    default
        The field of the default order.

    Returns
    -------
    ListSort
        The sort.

    Raises
    ------
    ValidationError
        If `sort` is not one of `fields` or `order` is neither ``asc`` nor ``desc``; the
        message names the choices.
    """
    field = default if sort is None else sort
    if field not in fields:
        choices = ", ".join(sorted(fields))
        raise ValidationError(f"`sort` must be one of: {choices}; got {sort!r}.")
    if order is None:
        return ListSort(field, fields[field])
    if order not in SORT_ORDERS:
        raise ValidationError(f"`order` must be asc or desc; got {order!r}.")
    return ListSort(field, order == "desc")


def _key_order(key: SortKey, descending: bool) -> ColumnElement[Any]:
    """Order by one key, a null last.

    Parameters
    ----------
    key
        The key.
    descending
        The list's direction, which a key without one of its own follows.

    Returns
    -------
    ColumnElement
        The ``ORDER BY`` term.
    """
    down = descending if key.descending is None else key.descending
    return (key.expression.desc() if down else key.expression.asc()).nulls_last()


def _after_keys(
    keys: Sequence[SortKey],
    row_id: ColumnElement[uuid.UUID] | InstrumentedAttribute[uuid.UUID],
    descending: bool,
    at: SortedPosition,
) -> ColumnElement[bool]:
    """Keep the rows strictly after a position, in the order `_key_order` gives each key.

    Parameters
    ----------
    keys
        The keys, most significant first.
    row_id
        The id column, which breaks the last tie in the list's direction.
    descending
        The list's direction.
    at
        Where the previous page ended.

    Returns
    -------
    ColumnElement
        The condition.
    """
    condition: ColumnElement[bool] = row_id < at.id if descending else row_id > at.id
    for key, value in reversed(list(zip(keys, at.values, strict=True))):
        column = key.expression
        if value is None:
            # A null sorts last: no value comes after it, and the nulls tie among themselves.
            condition = and_(column.is_(None), condition)
            continue
        down = descending if key.descending is None else key.descending
        beyond = column < value if down else column > value
        if key.nullable:
            beyond = or_(beyond, column.is_(None))
        condition = or_(beyond, and_(column == value, condition))
    return condition


def sorted_page(
    session: Session,
    query: Select[*tuple[Any, ...]],
    *,
    kind: str,
    sort: ListSort,
    field: SortField,
    row_id: ColumnElement[uuid.UUID] | InstrumentedAttribute[uuid.UUID],
    limit: int,
    cursor: str | None,
) -> Page[Any]:
    """Read one page of a list sorted by a chosen field, ordered and cut in the database.

    The cursor holds the last row's key values as the database gave them, its id, the list's
    kind and the sort's name, so a cursor of another list, another sort or the list's default
    order is refused, never misread.

    Parameters
    ----------
    session
        Database session.
    query
        The rows of the list, already filtered and joined to what the keys read.
    kind
        The list, as the cursor names it.
    sort
        The sort the page is read in.
    field
        Its field's keys.
    row_id
        The id column, which breaks the last tie.
    limit
        Most rows returned; at least 1.
    cursor
        The `next_cursor` of the previous page, or `None` for the first.

    Returns
    -------
    Page
        The rows as `query` selects them (one entity alone, or a row of several), and the
        cursor of the next page (`None` on the last).

    Raises
    ------
    ValidationError
        If `limit` is below 1, or `cursor` is not one this server issued for this list in
        this sort.
    """
    if limit < 1:
        raise ValidationError(f"limit must be at least 1, got {limit}.")
    width = len(query.column_descriptions)
    keys = field.keys
    if cursor is not None:
        if "s" not in _cursor_body(kind, cursor):
            raise ValidationError(_DEFAULT_NOT_SORTED.format(expected=sort.name))
        at = decode_sorted_cursor(kind, sort.name, cursor, [k.type for k in keys])
        query = query.where(_after_keys(keys, row_id, sort.descending, at))
    # Labelled, so a key never merges with a column of an entity the query already selects.
    extra = [k.expression.label(f"sort_key_{n}") for n, k in enumerate(keys)]
    ordered = query.add_columns(*extra, row_id.label("sort_key_id")).order_by(
        *(_key_order(k, sort.descending) for k in keys),
        row_id.desc() if sort.descending else row_id.asc(),
    )
    rows = list(session.execute(ordered.limit(limit + 1)).all())
    page = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
        last = page[-1]
        next_cursor = encode_sorted_cursor(kind, sort.name, last[width:-1], last[-1])
    return Page([row[0] if width == 1 else tuple(row[:width]) for row in page], next_cursor)


def name_order(
    session: Session, column: ColumnElement[str] | InstrumentedAttribute[str]
) -> ColumnElement[str]:
    """Order a name the way the lists sort it: in any letter case, by code point everywhere.

    Parameters
    ----------
    session
        The session the list is read in, for its database's dialect.
    column
        The name.

    Returns
    -------
    ColumnElement
        The name in lower case, collated ``"C"`` on Postgres (which compares under the
        database's collation otherwise); SQLite compares text by code point already.
    """
    lowered: ColumnElement[str] = func.lower(column, type_=String)
    if session.get_bind().dialect.name == "postgresql":
        return lowered.collate("C")
    return lowered


def escape_like(value: str) -> str:
    r"""Escape SQL ``LIKE`` wildcards in a literal.

    ``%`` and ``_`` are legal characters in a name (a namespace component, a principal);
    interpolating one unescaped into a ``LIKE`` pattern would let ``a_b`` match ``axb``.

    Parameters
    ----------
    value
        The literal to escape.

    Returns
    -------
    str
        The text with ``\``, ``%`` and ``_`` backslash-escaped, for use with ``escape="\\"``.
    """
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def name_contains(column: InstrumentedAttribute[str], text: str) -> ColumnElement[bool]:
    """Match a name that contains `text`, the letter case ignored, wildcards taken literally.

    The case is ignored with the database's own ``lower()``: for A to Z everywhere, and for other
    letters (``Ä`` and ``ä``) only on PostgreSQL, as far as its locale (``LC_CTYPE``) folds them;
    SQLite folds A to Z alone. The lists of principals, groups and tables share this rule.

    Parameters
    ----------
    column
        The name column.
    text
        What the name must contain.

    Returns
    -------
    ColumnElement
        True where the column contains `text`.
    """
    pattern = func.lower(literal("%" + escape_like(text) + "%", String))
    return func.lower(column).like(pattern, escape="\\")


LIST_LIMIT_DEFAULT = 100
"""The page size of a list sorted in Python when the caller names none."""

LIST_LIMIT_MAX = 200
"""The largest page size of a list sorted in Python."""


def instant_key(moment: datetime) -> str:
    """Write a time as fixed-width text whose order is the time's order.

    Parameters
    ----------
    moment
        A timezone-aware time.

    Returns
    -------
    str
        The time in UTC, ``YYYY-MM-DDTHH:MM:SS.ffffffZ``.
    """
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def page_in_order(
    items: Sequence[_T],
    *,
    kind: str,
    key_of: Callable[[_T], tuple[str, ...]],
    limit: int,
    cursor: str | None,
    descending: bool = False,
) -> Page[_T]:
    """Page a list that is sorted in Python, by a cursor of the last item's key.

    The list is sorted here by exactly `key_of`, so the order a page resumes in is the order
    the cursor's key means, whatever order the database returned the rows in.

    Parameters
    ----------
    items
        Every item of the list, in any order.
    kind
        The list's name in the cursor.
    key_of
        An item's key: a tuple of strings, unique among the items. Tuples compare part by part,
        so ``("a",)`` comes before ``("a", "b")`` and ``("ab",)``.
    limit
        The page size, 1 to `LIST_LIMIT_MAX`.
    cursor
        The `next_cursor` of the previous page, or `None` for the first.
    descending
        Whether the list runs from the largest key down.

    Returns
    -------
    Page
        This page's items and, unless it is the last, the cursor that continues it. A cursor
        of an item that has since gone still resumes after where that item was.

    Raises
    ------
    ValidationError
        If `limit` is out of range, or `cursor` is malformed or came from another list.
    ValueError
        If two items have the same key, which would make a page skip or repeat items.
    """
    check_list_limit(limit)
    after: tuple[str, ...] | None = None
    if cursor is not None:
        after = decode_tuple_cursor(kind, cursor)
    keyed = sorted(((key_of(item), item) for item in items), key=lambda p: p[0], reverse=descending)
    if any(a[0] == b[0] for a, b in itertools.pairwise(keyed)):
        raise ValueError(f"The keys of the `{kind}` list are not unique; a page would skip items.")
    if after is not None:
        keyed = [p for p in keyed if (p[0] < after if descending else p[0] > after)]
    page = keyed[:limit]
    next_cursor = None
    if len(keyed) > limit:
        next_cursor = encode_tuple_cursor(kind, page[-1][0])
    return Page([item for _, item in page], next_cursor)


def check_list_limit(limit: int) -> None:
    """Refuse the page size of a list sorted by a tuple key, as `page_in_order` does.

    Parameters
    ----------
    limit
        The page size.

    Raises
    ------
    ValidationError
        If it is not 1 to `LIST_LIMIT_MAX`.
    """
    if not 1 <= limit <= LIST_LIMIT_MAX:
        raise ValidationError(f"limit must be between 1 and {LIST_LIMIT_MAX}; got {limit}.")


def encode_tuple_cursor(kind: str, key: tuple[str, ...]) -> str:
    """Encode the tuple key of a page's last item, the cursor `page_in_order` issues.

    A list that orders and cuts its pages in the database issues the same cursor, so it reads the
    cursors of a list `page_in_order` paged before.

    Parameters
    ----------
    kind
        The list the cursor belongs to.
    key
        The last item's key.

    Returns
    -------
    str
        A key cursor (`encode_key_cursor`) of the key as a JSON array.
    """
    return encode_key_cursor(kind, json.dumps(list(key), ensure_ascii=False))


def decode_tuple_cursor(kind: str, cursor: str) -> tuple[str, ...]:
    """Read the tuple key out of a cursor made by `encode_tuple_cursor`.

    Parameters
    ----------
    kind
        The list being read.
    cursor
        The value a client sent back.

    Returns
    -------
    tuple of str
        The key to resume after.

    Raises
    ------
    ValidationError
        If `cursor` is not one this server could have issued, or belongs to another list.
    """
    try:
        parts = json.loads(decode_key_cursor(kind, cursor))
    except ValueError as exc:
        raise ValidationError(_BAD_CURSOR) from exc
    if not isinstance(parts, list) or not all(isinstance(p, str) for p in parts):
        raise ValidationError(_BAD_CURSOR)
    return tuple(_stored_text(p) for p in parts)
