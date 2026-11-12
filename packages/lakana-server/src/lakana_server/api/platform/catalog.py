"""The catalog list of the platform API: every table the caller may read, and the tree's counts."""

from __future__ import annotations

from typing import TypeVar

from fastapi import APIRouter, Depends, Query, Request

from lakana_core.auth import PrincipalRef
from lakana_core.catalog.table_listing import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    SortOrder,
    TableListing,
    TableSort,
)
from lakana_core.errors import ValidationError
from lakana_server.api.platform.languages import LANG_DESCRIPTION, reader_languages
from lakana_server.api.platform.schemas import TableListPageOut, TableTreeOut
from lakana_server.api.responses import errors
from lakana_server.api.routing import AuthenticateFirstRoute
from lakana_server.deps import get_main_language, get_principal, get_table_listing

router = APIRouter(
    route_class=AuthenticateFirstRoute,
    prefix="/api/v1/tables",
    tags=["catalog"],
    responses=errors(401),
)


def _no_nul(name: str, value: str | None) -> None:
    """Refuse a NUL character, which the database cannot store or compare.

    Parameters
    ----------
    name
        The query parameter's name.
    value
        Its value.

    Raises
    ------
    lakana_core.errors.ValidationError
        If `value` holds a NUL character.
    """
    if value is not None and "\x00" in value:
        raise ValidationError(
            f"`{name}` must not contain a NUL character. Remove it and send the request again."
        )


_Choice = TypeVar("_Choice", TableSort, SortOrder)


def _choice(kind: type[_Choice], name: str, value: str | None) -> _Choice | None:
    """Parse a sort or an order, naming the choices when it is none of them.

    Parameters
    ----------
    kind
        The enumeration.
    name
        The query parameter's name.
    value
        Its value.

    Returns
    -------
    TableSort or SortOrder or None
        The choice; `None` when `value` is.

    Raises
    ------
    lakana_core.errors.ValidationError
        If `value` is none of the choices.
    """
    if value is None:
        return None
    try:
        return kind(value)
    except ValueError:
        choices = ", ".join(c.value for c in kind)
        raise ValidationError(f"`{name}` must be one of: {choices}; got {value!r}.") from None


@router.get("", response_model=TableListPageOut, responses=errors(404))
def list_tables(
    request: Request,
    environment: str | None = Query(default=None, description="Only this environment."),
    namespace: str | None = Query(
        default=None, description="Only this namespace and the ones beneath it, dotted."
    ),
    q: str | None = Query(
        default=None,
        description="Only tables whose name, label, description, grain, or a column's name, "
        "label, description or measure contains this text; 1 to 200 characters.",
    ),
    model_kind: str | None = Query(
        default=None,
        description="Only tables whose asset header declares this kind: fact, dimension, wide "
        "or staging.",
    ),
    sort: str = Query(
        default=TableSort.NAME.value,
        description="What the list is sorted by: "
        + ", ".join(s.value for s in TableSort)
        + ". A table with no value sorts last, in either order.",
    ),
    order: str | None = Query(
        default=None,
        description="asc or desc; by default asc for name, owner and health, desc for the rest.",
    ),
    limit: int = Query(default=DEFAULT_LIMIT, description=f"Tables per page, 1 to {MAX_LIMIT}."),
    cursor: str | None = Query(
        default=None,
        description="The `next_cursor` of the previous page, sent with the same filters and sort.",
    ),
    lang: str | None = Query(default=None, description=LANG_DESCRIPTION),
    principal: PrincipalRef = Depends(get_principal),
    service: TableListing = Depends(get_table_listing),
    main_language: str = Depends(get_main_language),
) -> TableListPageOut:
    """List the tables you may read, across environments and namespaces.

    Filtered by environment, namespace, model kind and a search text, sorted and paged by a
    cursor. What you may not read is left out, not refused: each table needs `READ_TABLE` on
    it, a list writes no `AUTHORIZATION_DENIED`, and no count of hidden tables is given. Every
    table shows its owner's name and kind, when it was last read and by how many principals in
    the last 7 UTC days (never who), and its health in one word. A table with no known value for
    the sort key comes last. A search text matches the same texts as the environment search,
    only in tables you may read; `capped` says it matched more of them than one search keeps, so
    narrow it. Answers 404 when `environment` does not exist (to someone who may read
    environments; anyone else gets an empty page, as for one that exists), and 422 for a `sort`,
    `order`, `limit`, `q`, `model_kind`, `lang` or `cursor` that is not valid.

    Parameters
    ----------
    request
        The request, for `Accept-Language`.
    environment
        Only this environment.
    namespace
        Only this namespace and the ones beneath it, dotted.
    q
        The text to find, trimmed, 1 to 200 characters.
    model_kind
        Only tables that declare this kind in their asset header.
    sort
        What the list is sorted by.
    order
        Which way; the sort's default without it.
    limit
        Tables per page, 1 to 200.
    cursor
        The cursor of the page to show.
    lang
        The language to match translated texts in; see `reader_languages`.
    principal
        The authenticated caller.
    service
        The listing service for this request, with this request's authorization.
    main_language
        The instance's main language, the last language a reader falls back to.

    Returns
    -------
    TableListPageOut
        The page.

    Raises
    ------
    lakana_core.errors.ValidationError
        Mapped to 422 by `install_exception_handlers`.
    lakana_core.errors.NotFoundError
        Mapped to 404 by `install_exception_handlers`.
    """
    _no_nul("environment", environment)
    _no_nul("namespace", namespace)
    _no_nul("q", q)
    chosen_sort = _choice(TableSort, "sort", sort) or TableSort.NAME
    page = service.list(
        principal,
        environment=environment,
        namespace=namespace,
        q=q,
        model_kind=model_kind,
        sort=chosen_sort,
        order=_choice(SortOrder, "order", order),
        limit=limit,
        cursor=cursor,
        languages=reader_languages(request, lang, principal, main_language),
    )
    return TableListPageOut.model_validate(page, from_attributes=True)


@router.get("/tree", response_model=TableTreeOut)
def table_tree(
    principal: PrincipalRef = Depends(get_principal),
    service: TableListing = Depends(get_table_listing),
) -> TableTreeOut:
    """Count the tables you may read, by environment and namespace.

    A node's count is what the list answers for it without a search text: only tables you may
    read are counted, an environment is shown with `READ_ENVIRONMENT` or a readable table in it,
    a namespace with `READ_NAMESPACE` on its environment or a readable table in it or beneath
    it. Writes no audit event.

    Parameters
    ----------
    principal
        The authenticated caller.
    service
        The listing service for this request, with this request's authorization.

    Returns
    -------
    TableTreeOut
        The counts.
    """
    return TableTreeOut.model_validate(service.tree(principal), from_attributes=True)
