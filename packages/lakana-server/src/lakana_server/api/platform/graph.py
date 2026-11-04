"""The metadata graph routes of the platform API: lineage, neighbours, impact, join path (#374)."""

from __future__ import annotations

import dataclasses
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request

from lakana_core.auth import PrincipalRef
from lakana_core.catalog.graph_model import (
    DEFAULT_GRAPH_DEPTH,
    MAX_GRAPH_PAGE,
    GraphPage,
    LineageWay,
)
from lakana_core.catalog.graph_service import MAX_GRAPH_CURSOR, GraphService
from lakana_server.api.platform.languages import LANG_DESCRIPTION, reader_languages
from lakana_server.api.platform.schemas import (
    GraphEdgeOut,
    GraphNodeOut,
    GraphOut,
    JoinPathOut,
    JoinPathsOut,
    JoinStepOut,
)
from lakana_server.api.responses import errors
from lakana_server.api.routing import AuthenticateFirstRoute
from lakana_server.deps import get_graph_service, get_main_language, get_principal

router = APIRouter(
    route_class=AuthenticateFirstRoute,
    prefix="/api/v1/environments/{env}/graph",
    tags=["graph"],
    responses=errors(401),
)

NODE_DESCRIPTION = (
    "A node id from an answer, or a table's dotted name such as gold.dim_store; a column as "
    "column:gold.dim_store/store_id."
)
LIMIT_DESCRIPTION = "Most nodes per page, 1 to 500."
CURSOR_DESCRIPTION = "The `next_cursor` of the previous page, sent with the same question."

_LANG = Query(default=None, description=LANG_DESCRIPTION)
_LIMIT = Query(default=MAX_GRAPH_PAGE, description=LIMIT_DESCRIPTION)
_CURSOR = Query(default=None, max_length=MAX_GRAPH_CURSOR, description=CURSOR_DESCRIPTION)
_PRINCIPAL = Depends(get_principal)
_SERVICE = Depends(get_graph_service)
_MAIN_LANGUAGE = Depends(get_main_language)


def graph_out(page: GraphPage) -> GraphOut:
    """Build the response body of a page.

    Parameters
    ----------
    page
        From `GraphService`.

    Returns
    -------
    GraphOut
        The body.
    """
    return GraphOut(
        nodes=[
            GraphNodeOut.model_validate(
                {
                    **dataclasses.asdict(n),
                    "kind": n.kind.value,
                    "key": None if n.key is None else list(n.key),
                }
            )
            for n in page.nodes
        ],
        edges=[
            GraphEdgeOut(kind=e.kind.value, source=e.source, target=e.target) for e in page.edges
        ],
        next_cursor=page.next_cursor,
    )


@router.get("/lineage", response_model=GraphOut, responses=errors(404, 503))
def graph_lineage(
    request: Request,
    env: str,
    node: str = Query(description=NODE_DESCRIPTION),
    direction: Literal["up", "down", "both"] = Query(
        default="both", description="Upstream (what feeds it), downstream (what it feeds), or both."
    ),
    depth: int = Query(
        default=DEFAULT_GRAPH_DEPTH, description="Assets to cross from the start, 1 to 10."
    ),
    lang: str | None = _LANG,
    limit: int = _LIMIT,
    cursor: str | None = _CURSOR,
    principal: PrincipalRef = _PRINCIPAL,
    service: GraphService = _SERVICE,
    main_language: str = _MAIN_LANGUAGE,
) -> GraphOut:
    """Walk the tables and assets upstream and downstream of a table, a column or an asset.

    The answer comes from what the projects declare (their assets' inputs and outputs, in
    every repository), not from runs. `depth` counts the assets crossed. A column starts at
    its table, plus the references that name it. You see a node only when you may: a table and
    its columns need `READ_TABLE`, an asset `READ_ENVIRONMENT` and its table. The walk stops at
    a node you may not see, and nothing in the answer counts or marks it. Answers 404 when the
    start or the environment does not exist or you may not see it (one answer for both), 422
    for a node, depth, limit or cursor out of bounds or a question too large to walk, and 503
    with `Retry-After` when the walk ran past its time.

    Parameters
    ----------
    request
        The request, for `Accept-Language`.
    env
        Environment name.
    node
        The start.
    direction
        Which way to walk.
    depth
        Assets to cross.
    lang
        The language of labels and grains.
    limit
        Nodes per page.
    cursor
        The previous page's cursor.
    principal
        The authenticated caller.
    service
        The graph reader.
    main_language
        The instance's main language.

    Returns
    -------
    GraphOut
        One page.
    """
    return graph_out(
        service.lineage(
            principal,
            env,
            node,
            direction=LineageWay(direction),
            depth=depth,
            languages=reader_languages(request, lang, principal, main_language),
            limit=limit,
            cursor=cursor,
        )
    )


@router.get("/neighbours", response_model=GraphOut, responses=errors(404, 503))
def graph_neighbours(
    request: Request,
    env: str,
    node: str = Query(description=NODE_DESCRIPTION),
    kinds: list[str] | None = Query(
        default=None,
        description="Only edges of these kinds; repeat the parameter (kinds=reads&kinds=builds).",
    ),
    lang: str | None = _LANG,
    limit: int = _LIMIT,
    cursor: str | None = _CURSOR,
    principal: PrincipalRef = _PRINCIPAL,
    service: GraphService = _SERVICE,
    main_language: str = _MAIN_LANGUAGE,
) -> GraphOut:
    """List the nodes one edge away from a node, in either direction.

    The node comes first (distance 0), then its neighbours (distance 1). A row filter or column
    mask shows only to a caller who may manage policies and read its table. The other rules,
    and the 404, 422 and 503 answers, are lineage's.

    Parameters
    ----------
    request
        The request, for `Accept-Language`.
    env
        Environment name.
    node
        The node.
    kinds
        Only these edge kinds.
    lang
        The language of labels and names.
    limit
        Nodes per page.
    cursor
        The previous page's cursor.
    principal
        The authenticated caller.
    service
        The graph reader.
    main_language
        The instance's main language.

    Returns
    -------
    GraphOut
        One page.
    """
    return graph_out(
        service.neighbours(
            principal,
            env,
            node,
            kinds=kinds or (),
            languages=reader_languages(request, lang, principal, main_language),
            limit=limit,
            cursor=cursor,
        )
    )


@router.get("/impact", response_model=GraphOut, responses=errors(404, 503))
def graph_impact(
    request: Request,
    env: str,
    node: str = Query(description=NODE_DESCRIPTION),
    key: bool = Query(
        default=False,
        description="Ask about the table's declared key instead of the whole table.",
    ),
    lang: str | None = _LANG,
    limit: int = _LIMIT,
    cursor: str | None = _CURSOR,
    principal: PrincipalRef = _PRINCIPAL,
    service: GraphService = _SERVICE,
    main_language: str = _MAIN_LANGUAGE,
) -> GraphOut:
    """List everything the projects declare that depends on a table, a column or a table's key.

    The start is a table or a column; `key=true` asks about a table's declared key. The answer
    holds the references to it and their generated checks, the measures, masks and glossary
    terms on its columns, its checks, row filters and terms, the asset that builds it,
    everything downstream of it, and the jobs that run any asset listed. Each node but the start
    carries `reason`, such as `references gold.dim_store.store_id` or `downstream of
    silver.orders`. For a column or a key, what only may use it carries `may_use: true`: the
    table's hand-written checks, its row filters and everything downstream, because SQL is not
    parsed. You see what lineage and neighbours would show you, and nothing beyond a node you may
    not see. Answers 404 as lineage does, 422 for a start that is not a table or column, for
    `key=true` with a column or on a table that declares no key, and for a limit or cursor out of
    bounds or a question too large to walk, and 503 with `Retry-After` when the walk ran past its
    time.

    Parameters
    ----------
    request
        The request, for `Accept-Language`.
    env
        Environment name.
    node
        The start.
    key
        Whether to ask about the table's declared key.
    lang
        The language of labels and names.
    limit
        Nodes per page.
    cursor
        The previous page's cursor.
    principal
        The authenticated caller.
    service
        The graph reader.
    main_language
        The instance's main language.

    Returns
    -------
    GraphOut
        One page.
    """
    return graph_out(
        service.impact(
            principal,
            env,
            node,
            key=key,
            languages=reader_languages(request, lang, principal, main_language),
            limit=limit,
            cursor=cursor,
        )
    )


@router.get("/join-path", response_model=JoinPathsOut, responses=errors(404, 503))
def graph_join_path(
    request: Request,
    env: str,
    start: str = Query(alias="from", description="The table to start from: " + NODE_DESCRIPTION),
    goal: str = Query(alias="to", description="The table to reach, named the same way."),
    lang: str | None = _LANG,
    principal: PrincipalRef = _PRINCIPAL,
    service: GraphService = _SERVICE,
    main_language: str = _MAIN_LANGUAGE,
) -> JoinPathsOut:
    """Show how two tables join: every declared path of minimal length over references.

    A path has at most 4 steps, and at most 10 paths are returned, in the order of their
    references' ids (`more_paths` says when more exist). A step follows a reference either way:
    `many_to_one` from the table that declares it to its target, `one_to_many` with
    `fans_out: true` back, where each row may meet many. A step names the reference, the columns
    to join on, and both tables' kind and grain. No path, or none you may see, gives an empty
    `paths` with a `reason`; a path through a table you may not see is never returned, and
    nothing in the answer counts or marks it. Answers 404 when a table or the environment does
    not exist or you may not see it (one answer for both), 422 for something that is not a table
    or the same table twice, or a question too large to walk, and 503 with `Retry-After` when
    the walk ran past its time.

    Parameters
    ----------
    request
        The request, for `Accept-Language`.
    env
        Environment name.
    start
        The table to start from (`from`).
    goal
        The table to reach (`to`).
    lang
        The language of grains.
    principal
        The authenticated caller.
    service
        The graph reader.
    main_language
        The instance's main language.

    Returns
    -------
    JoinPathsOut
        The paths, or none with the reason.
    """
    answer = service.join_path(
        principal,
        env,
        start,
        goal,
        languages=reader_languages(request, lang, principal, main_language),
    )
    return JoinPathsOut(
        paths=[
            JoinPathOut(
                steps=[JoinStepOut.model_validate(dataclasses.asdict(s)) for s in path.steps]
            )
            for path in answer.paths
        ],
        reason=answer.reason,
        more_paths=answer.more_paths,
    )
