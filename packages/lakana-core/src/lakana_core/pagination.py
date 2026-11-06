"""What the paged lists share: the cursor, the page, filters and a name search.

The lists are checks, runs, queries, deploys, grants, principals and groups. A cursor is the
position of the last item of a page: its time and its id, and the list it belongs to. A list
resumes strictly after that position, so an item that arrives while a client pages is never
repeated and none is skipped. The kind of the list is part of the cursor, so a cursor of one list
is refused by another instead of being misread.

A list ordered by a unique key instead (the glossary's terms) has a cursor of that key and the
list's kind, with the same refusal of another list's cursor.
"""

from __future__ import annotations

import base64
import binascii
import enum
import json
import unicodedata
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Generic, NamedTuple, TypeVar

from sqlalchemy import ColumnElement, Select, String, and_, func, literal, or_
from sqlalchemy.orm import InstrumentedAttribute, Session

from lakana_core.errors import ValidationError

_E = TypeVar("_E", bound=enum.StrEnum)
_T = TypeVar("_T")

_BAD_CURSOR = (
    "The cursor is not valid. Start the listing again without `cursor`; pass the `next_cursor` of "
    "a page only to continue that listing."
)
_OTHER_LIST = (
    "The cursor is not valid for the `{expected}` list: it came from the `{actual}` list. "
    "Start the listing again without `cursor`; pass the `next_cursor` of a page only to "
    "continue that listing."
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
        If `cursor` is not one this server could have issued, or belongs to another list.
    """
    try:
        data = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
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
        If `cursor` is not one this server could have issued, or belongs to another list.
    """
    try:
        data = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
    except (binascii.Error, ValueError) as exc:
        raise ValidationError(_BAD_CURSOR) from exc
    if not isinstance(data, dict) or not isinstance(data.get("k"), str):
        raise ValidationError(_BAD_CURSOR)
    if data["k"] != kind:
        raise ValidationError(_OTHER_LIST.format(expected=kind, actual=data["k"]))
    key = data.get("n")
    # A cursor this server issued holds a stored key, never a NUL or a lone surrogate, which
    # the database driver could not take.
    if not isinstance(key, str) or any(unicodedata.category(c) in ("Cc", "Cs") for c in key):
        raise ValidationError(_BAD_CURSOR)
    return key


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
    """Match a name that contains `text`, in any letter case, wildcards taken literally.

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
