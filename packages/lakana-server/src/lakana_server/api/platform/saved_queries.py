"""The saved query resource of the platform API.

A saved query is SQL text under a name in an environment, private to its owner or shared with
everyone who may query there. Running one is an ordinary `POST …/queries` by the runner, with
the runner's rights.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, Request, Response

from lakana_core.auth import PrincipalRef
from lakana_core.catalog.saved_queries import (
    SAVED_QUERY_LIST_DEFAULT,
    SAVED_QUERY_LIST_MAX,
    SavedQueryService,
    SavedQueryView,
)
from lakana_server.api.platform.schemas import (
    SavedQueryIn,
    SavedQueryOut,
    SavedQueryPageOut,
    SavedQueryUpdateIn,
)
from lakana_server.api.responses import errors
from lakana_server.api.routing import AuthenticateFirstRoute
from lakana_server.deps import check_statement, get_principal, get_saved_query_service

environment_router = APIRouter(
    route_class=AuthenticateFirstRoute,
    prefix="/api/v1/environments/{env}/saved-queries",
    tags=["saved-queries"],
    responses=errors(401),
)

router = APIRouter(
    route_class=AuthenticateFirstRoute,
    prefix="/api/v1/saved-queries",
    tags=["saved-queries"],
    responses=errors(401),
)
"""The saved queries of every environment the caller may see."""


def _out(view: SavedQueryView) -> SavedQueryOut:
    """Build the answer for one saved query.

    Parameters
    ----------
    view
        The saved query as the service shows it to the caller.

    Returns
    -------
    SavedQueryOut
        The answer. `sql` is set only when the caller may read it, so that
        `response_model_exclude_unset` leaves the key out otherwise.
    """
    out = SavedQueryOut(
        id=str(view.id),
        environment=view.environment,
        name=view.name,
        visibility=view.visibility,  # ty: ignore[invalid-argument-type]
        owner=view.owner,
        former_owner=view.former_owner,
        actions=list(view.actions),  # ty: ignore[invalid-argument-type]
        created_at=view.created_at,
        updated_at=view.updated_at,
    )
    if view.sql is not None:
        out.sql = view.sql
    return out


@environment_router.get(
    "",
    response_model=SavedQueryPageOut,
    response_model_exclude_unset=True,
    responses=errors(403, 404, 503),
)
def list_saved_queries(
    env: str,
    limit: int = Query(default=SAVED_QUERY_LIST_DEFAULT, ge=1, le=SAVED_QUERY_LIST_MAX),
    cursor: str | None = Query(default=None, max_length=512),
    principal: PrincipalRef = Depends(get_principal),
    service: SavedQueryService = Depends(get_saved_query_service),
) -> SavedQueryPageOut:
    """List the saved queries of an environment you see.

    You see your own saved queries (also if you may no longer query the environment), the shared
    ones (`visibility` `everyone`) if you hold `SUBMIT_QUERY` on it, and, as an instance
    administrator with a personal token or a session, every one. The items have no `sql` key:
    read a saved query (`GET .../saved-queries/{saved_query_id}`) for its SQL. A page that shows
    someone else's private query is recorded in the audit log (`ACTIVITY_VIEWED`); if that fails
    the answer is 503 and lists nothing. Each item's `actions` says what you may do with it. The
    order is the name in any letter case, then the id. Answers 403 if you may not query the
    environment, own no saved query there and are not an administrator, 404 if it does not
    exist, and 403 for a task's token. Send `next_cursor` back as `cursor` for the next page.

    Parameters
    ----------
    env
        Environment name.
    limit
        Most items returned (1 to 200).
    cursor
        The `next_cursor` of the previous page.
    principal
        The authenticated caller.
    service
        The saved query service for this request.

    Returns
    -------
    SavedQueryPageOut
        This page's saved queries and the cursor of the next page, `null` on the last.

    Raises
    ------
    lakana_core.errors.PermissionDenied
        Mapped to 403, for a task's token, or if you may not query the environment and see no
        saved query in it.
    lakana_core.errors.NotFoundError
        Mapped to 404, if the environment does not exist.
    lakana_core.errors.ValidationError
        Mapped to 422, for a malformed cursor or a cursor of the other list.
    lakana_core.errors.AuditUnavailableError
        Mapped to 503, if the page shows someone else's private query and that could not be
        recorded in the audit log.
    """
    page = service.list_page(principal, environment=env, limit=limit, cursor=cursor)
    return SavedQueryPageOut(items=[_out(v) for v in page.items], next_cursor=page.next_cursor)


@environment_router.post(
    "",
    status_code=201,
    response_model=SavedQueryOut,
    response_model_exclude_unset=True,
    responses=errors(403, 404, 409),
)
def create_saved_query(
    env: str,
    body: SavedQueryIn,
    request: Request,
    principal: PrincipalRef = Depends(get_principal),
    service: SavedQueryService = Depends(get_saved_query_service),
) -> SavedQueryOut:
    """Save SQL under a name in an environment.

    Needs `SUBMIT_QUERY` on the environment. You become the owner. A saved query is only text:
    running it is a query you submit with your own rights, never with the owner's. The name is
    stripped and must be one line of 1 to 200 visible characters, unique among your saved
    queries in the environment; the SQL must not be empty and at most
    `LAKANA_QUERY_LIMITS__MAX_STATEMENT_CHARS` long. Answers 403 without the right and for a
    task's token, 404 if the environment does not exist, 409 for a name you already use there
    (the message names that saved query's id) and 422 for a refused field.

    Parameters
    ----------
    env
        Environment name.
    body
        The name, SQL and visibility.
    request
        The request, for the statement length limit.
    principal
        The authenticated caller.
    service
        The saved query service for this request.

    Returns
    -------
    SavedQueryOut
        The saved query, with its `sql`.

    Raises
    ------
    lakana_core.errors.PermissionDenied
        Mapped to 403, if you lack `SUBMIT_QUERY` on the environment or use a task's token.
    lakana_core.errors.NotFoundError
        Mapped to 404, if the environment does not exist.
    lakana_core.errors.AlreadyExistsError
        Mapped to 409, if you already have a saved query of that name there.
    lakana_core.errors.ValidationError
        Mapped to 422, for a name that is not one visible line of 1 to 200 characters, empty or
        too long SQL, or SQL holding a NUL character.
    """
    check_statement(request, body.sql)
    view = service.create(principal, env, name=body.name, sql=body.sql, visibility=body.visibility)
    return _out(view)


@environment_router.get(
    "/{saved_query_id}",
    response_model=SavedQueryOut,
    response_model_exclude_unset=True,
    responses=errors(403, 404, 503),
)
def get_saved_query(
    env: str,
    saved_query_id: uuid.UUID,
    principal: PrincipalRef = Depends(get_principal),
    service: SavedQueryService = Depends(get_saved_query_service),
) -> SavedQueryOut:
    """Read one saved query.

    You read your own, a shared one if you hold `SUBMIT_QUERY` on its environment, and, as an
    instance administrator with a personal token or a session, any. An administrator reads the
    `sql` of a private query that is someone else's only with `READ_QUERY_TEXT`; without it the
    answer has no `sql` key. Reading someone else's private query is recorded in the audit log
    (`ACTIVITY_VIEWED`); if that fails the answer is 503 and shows nothing. Answers 404 for a
    saved query that does not exist and for one you may not see, in the same words, and 403 for
    a task's token.

    Parameters
    ----------
    env
        Environment name.
    saved_query_id
        The saved query's id.
    principal
        The authenticated caller.
    service
        The saved query service for this request.

    Returns
    -------
    SavedQueryOut
        The saved query and what you may do with it.

    Raises
    ------
    lakana_core.errors.PermissionDenied
        Mapped to 403, for a task's token.
    lakana_core.errors.NotFoundError
        Mapped to 404, if the environment does not exist, or holds no such saved query that you
        see.
    lakana_core.errors.AuditUnavailableError
        Mapped to 503, if it is someone else's private query and the read could not be recorded
        in the audit log.
    """
    return _out(service.get(principal, env, saved_query_id))


@environment_router.put(
    "/{saved_query_id}",
    response_model=SavedQueryOut,
    response_model_exclude_unset=True,
    responses=errors(403, 404, 409),
)
def update_saved_query(
    env: str,
    saved_query_id: uuid.UUID,
    body: SavedQueryUpdateIn,
    request: Request,
    principal: PrincipalRef = Depends(get_principal),
    service: SavedQueryService = Depends(get_saved_query_service),
) -> SavedQueryOut:
    """Replace a saved query's name, SQL and visibility.

    A whole replacement: send every field, `visibility` included. Only the owner may change a
    saved query, and only while they hold `SUBMIT_QUERY` on its environment; an administrator
    who is not the owner may only delete it, and nobody may change one whose owner is gone.
    The environment never changes: saving in another one is a new `POST`. Answers 403 for
    anyone but the owner (with a pointer to saving a copy), without the right and for a task's
    token, 404 for a saved query that does not exist or that you may not see, 409 for a name you
    already use for another saved query there, and 422 for a refused field or a missing one.

    Parameters
    ----------
    env
        Environment name.
    saved_query_id
        The saved query's id.
    body
        The new name, SQL and visibility.
    request
        The request, for the statement length limit.
    principal
        The authenticated caller.
    service
        The saved query service for this request.

    Returns
    -------
    SavedQueryOut
        The changed saved query.

    Raises
    ------
    lakana_core.errors.PermissionDenied
        Mapped to 403, if you are not the owner, lack `SUBMIT_QUERY` on the environment now, or
        use a task's token.
    lakana_core.errors.NotFoundError
        Mapped to 404, if the environment does not exist, or holds no such saved query that you
        see.
    lakana_core.errors.AlreadyExistsError
        Mapped to 409, if you already have another saved query of that name there.
    lakana_core.errors.ValidationError
        Mapped to 422, for a name that is not one visible line of 1 to 200 characters, empty or
        too long SQL, or SQL holding a NUL character.
    """
    check_statement(request, body.sql)
    view = service.update(
        principal,
        env,
        saved_query_id,
        name=body.name,
        sql=body.sql,
        visibility=body.visibility,
    )
    return _out(view)


@environment_router.delete(
    "/{saved_query_id}", status_code=204, response_class=Response, responses=errors(403, 404)
)
def delete_saved_query(
    env: str,
    saved_query_id: uuid.UUID,
    principal: PrincipalRef = Depends(get_principal),
    service: SavedQueryService = Depends(get_saved_query_service),
) -> Response:
    """Delete a saved query.

    The owner deletes their own, with or without `SUBMIT_QUERY` today; an instance administrator
    (personal token or session) deletes any, which is how a saved query of a deleted owner goes.
    Answers 403 for anyone else who sees it and for a task's token, and 404 for a saved query
    that does not exist or that you may not see.

    Parameters
    ----------
    env
        Environment name.
    saved_query_id
        The saved query's id.
    principal
        The authenticated caller.
    service
        The saved query service for this request.

    Returns
    -------
    Response
        Empty, with status 204.

    Raises
    ------
    lakana_core.errors.PermissionDenied
        Mapped to 403, if you are neither the owner nor an administrator, or use a task's token.
    lakana_core.errors.NotFoundError
        Mapped to 404, if the environment does not exist, or holds no such saved query that you
        see.
    """
    service.delete(principal, env, saved_query_id)
    return Response(status_code=204)


@router.get(
    "",
    response_model=SavedQueryPageOut,
    response_model_exclude_unset=True,
    responses=errors(403, 404, 503),
)
def list_all_saved_queries(
    environment: str | None = Query(default=None, max_length=255),
    limit: int = Query(default=SAVED_QUERY_LIST_DEFAULT, ge=1, le=SAVED_QUERY_LIST_MAX),
    cursor: str | None = Query(default=None, max_length=512),
    principal: PrincipalRef = Depends(get_principal),
    service: SavedQueryService = Depends(get_saved_query_service),
) -> SavedQueryPageOut:
    """List the saved queries of every environment you see.

    Without `environment`, those of every live environment, one cursor across all of them: your
    own, the shared ones of the environments where you hold `SUBMIT_QUERY`, and, as an instance
    administrator with a personal token or a session, every one. An environment where you see
    nothing contributes nothing, so this is never a 403 or a 404, except for a task's token.
    The items have no `sql` key: read a saved query in its environment for its SQL. A page that
    shows someone else's private query is recorded in the audit log (`ACTIVITY_VIEWED`, one event
    per environment shown); if that fails the answer is 503 and lists nothing. With `environment`
    it is the list of that environment (`GET /api/v1/environments/{env}/saved-queries`), with its
    403 and 404.
    Each item names its `environment`. The order is the name in any letter case, then the
    environment, then the id. Send `next_cursor` back as `cursor` for the next page; a cursor of
    the list of one environment is refused with 422 here.

    Parameters
    ----------
    environment
        Only this environment.
    limit
        Most items returned (1 to 200).
    cursor
        The `next_cursor` of the previous page.
    principal
        The authenticated caller.
    service
        The saved query service for this request.

    Returns
    -------
    SavedQueryPageOut
        This page's saved queries and the cursor of the next page, `null` on the last.

    Raises
    ------
    lakana_core.errors.PermissionDenied
        Mapped to 403, for a task's token, or, with `environment`, if you may not query it and
        see no saved query in it.
    lakana_core.errors.NotFoundError
        Mapped to 404, if the named environment does not exist.
    lakana_core.errors.ValidationError
        Mapped to 422, for a malformed cursor or a cursor of the other list.
    lakana_core.errors.AuditUnavailableError
        Mapped to 503, if the page shows someone else's private query and that could not be
        recorded in the audit log.
    """
    page = service.list_page(principal, environment=environment, limit=limit, cursor=cursor)
    return SavedQueryPageOut(items=[_out(v) for v in page.items], next_cursor=page.next_cursor)
