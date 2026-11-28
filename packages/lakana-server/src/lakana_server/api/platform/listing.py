"""What the list routes share: filter values, the cursor header and the sort parameters.

These lists answer with a JSON array, as they did before they could be paged, so the cursor of the
next page travels in a response header instead of a body field.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Query, Response

from lakana_core.pagination import (
    CURSOR_MAX_LENGTH,
    NAMESPACE_CURSOR_MAX_LENGTH,
    PLACE_CURSOR_MAX_LENGTH,
    TABLE_CURSOR_MAX_LENGTH,
)

NEXT_CURSOR_HEADER = "X-Next-Cursor"
"""The response header that carries the cursor of the next page; absent on the last page."""


def split_values(values: list[str]) -> list[str]:
    """Flatten a repeatable, comma-separated filter into its values.

    ``?status=failed&status=cancelled`` and ``?status=failed,cancelled`` both give
    ``["failed", "cancelled"]``.

    Parameters
    ----------
    values
        The values of the query parameter, as sent.

    Returns
    -------
    list of str
        Each non-empty, stripped value.
    """
    return [part.strip() for value in values for part in value.split(",") if part.strip()]


def set_next_cursor(response: Response, next_cursor: str | None) -> None:
    """Put the cursor of the next page on the response, if there is one.

    Parameters
    ----------
    response
        The response being built.
    next_cursor
        The cursor, or `None` on the last page.
    """
    if next_cursor is not None:
        response.headers[NEXT_CURSOR_HEADER] = next_cursor


SortParam = Annotated[str | None, Query(max_length=32)]
"""A list's `sort`: the field it is sorted by; each route's docstring names the fields."""

OrderParam = Annotated[str | None, Query(max_length=8)]
"""A list's `order`: ``asc`` or ``desc``; each route's docstring says which way is the default."""

CursorParam = Annotated[str | None, Query(max_length=CURSOR_MAX_LENGTH)]
"""The `cursor` of a list whose cursor holds names: as long as the longest one it issues."""

NamespaceCursorParam = Annotated[str | None, Query(max_length=NAMESPACE_CURSOR_MAX_LENGTH)]
"""The `cursor` of the namespaces list: as long as the cursor of the longest path."""

TableCursorParam = Annotated[str | None, Query(max_length=TABLE_CURSOR_MAX_LENGTH)]
"""The `cursor` of a list of tables, artifacts or orphans: as long as the longest one it issues."""

PlaceCursorParam = Annotated[str | None, Query(max_length=PLACE_CURSOR_MAX_LENGTH)]
"""The `cursor` of a list that sorts by place (grants, policies, the audit log): as long as the
cursor of the longest environment, namespace and table names."""
