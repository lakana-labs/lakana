"""The search route of the platform API: tables, columns, descriptions, runs and principals."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from lakana_core.auth import PrincipalRef
from lakana_core.catalog.search import CatalogSearch, SearchKind
from lakana_server.api.platform.schemas import SearchOut
from lakana_server.api.responses import errors
from lakana_server.api.routing import AuthenticateFirstRoute
from lakana_server.deps import get_catalog_search, get_principal

router = APIRouter(
    route_class=AuthenticateFirstRoute,
    prefix="/api/v1/environments/{env}",
    tags=["search"],
    responses=errors(401),
)


@router.get("/search", response_model=SearchOut, responses=errors(403, 404))
def search(
    env: str,
    q: str = Query(description="The text to find; 1 to 200 characters, case-insensitive."),
    kind: SearchKind | None = Query(default=None, description="Only this kind of hit."),
    model_kind: str | None = Query(
        default=None,
        description="Only tables whose asset header declares this kind: fact, dimension, wide "
        "or staging.",
    ),
    limit: int = Query(default=20, description="Hits per page, 1 to 100."),
    offset: int = Query(default=0, description="Hits to skip."),
    principal: PrincipalRef = Depends(get_principal),
    service: CatalogSearch = Depends(get_catalog_search),
) -> SearchOut:
    """Search an environment.

    Finds tables, columns, runs, jobs, repositories and principals whose names or
    descriptions, a table's declared grain, or the label or description of a column's
    declared measure contain `q` (1 to 200 characters, case-insensitive; run ids need four
    characters or more), one page at a time. What the caller may not read is left out of the
    hits, not refused: tables and columns need `READ_TABLE`, runs `READ_ENVIRONMENT`,
    principals the right to list them. Needs `READ_ENVIRONMENT`. Answers 404 when the
    environment does not exist and 422 for a `q`, `model_kind` or paging value out of range.

    Parameters
    ----------
    env
        Environment name.
    q
        The text to find in names and descriptions, in run ids (four characters or more), jobs
        and repositories, and in principal names. Trimmed, 1 to 200 characters.
    kind
        Only hits of this kind.
    model_kind
        Only tables that declare this kind in their asset header; other hits are left out.
    limit
        Hits per page, 1 to 100.
    offset
        Hits to skip.
    principal
        The authenticated caller.
    service
        The search service for this request.

    Returns
    -------
    SearchOut
        The page. A table and its columns appear only with `READ_TABLE` on the table, runs only
        with `READ_ENVIRONMENT`, principals only for a caller who may list them; what the caller
        may not see is left out, not refused.

    Raises
    ------
    lakana_core.errors.ValidationError
        Mapped to 422 by `install_exception_handlers`.
    lakana_core.errors.NotFoundError
        Mapped to 404 by `install_exception_handlers`.
    """
    page = service.search(
        principal, env, q, kind=kind, model_kind=model_kind, limit=limit, offset=offset
    )
    return SearchOut.model_validate(page)
