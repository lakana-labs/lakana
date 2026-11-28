"""The task-tokens resource of the platform API."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response

from lakana_core.auth import PrincipalRef
from lakana_core.identity.task_tokens import TaskTokenService
from lakana_server.api.platform.listing import CursorParam, OrderParam, SortParam, set_next_cursor
from lakana_server.api.platform.schemas import TaskTokenMintIn, TaskTokenMintOut, TaskTokenOut
from lakana_server.api.responses import errors
from lakana_server.api.routing import AuthenticateFirstRoute
from lakana_server.deps import get_principal, get_task_token_service

router = APIRouter(
    route_class=AuthenticateFirstRoute,
    prefix="/api/v1/task-tokens",
    tags=["task-tokens"],
    responses=errors(401),
)


@router.get("", response_model=list[TaskTokenOut], responses=errors(403))
def list_task_tokens(
    response: Response,
    environment: str | None = Query(default=None, max_length=255),
    limit: int = Query(default=100, ge=1, le=200),
    cursor: CursorParam = None,
    sort: SortParam = None,
    order: OrderParam = None,
    principal: PrincipalRef = Depends(get_principal),
    service: TaskTokenService = Depends(get_task_token_service),
) -> list[TaskTokenOut]:
    """List the live task tokens.

    Lists the task tokens that are neither revoked nor expired, newest first (or in the order
    `sort` and `order` ask for), each with the principal it signs in as, its environment and
    asset scope, its dates and, for a token a worker minted for a task, the run, job and asset.
    The token values are never included. Needs the `ADMIN` role on the instance; a task or CI
    token is refused. The cursor of the next page is in the `X-Next-Cursor` response header and
    is absent on the last page. An `environment` no token names gives an empty list, not a 404.
    Answers 422 for an unknown `sort` or `order`, or a cursor this list did not issue in this
    order; send the same sort with every page.

    Parameters
    ----------
    response
        The response, for the cursor header.
    environment
        Only the tokens of this environment; every environment when left out.
    limit
        Most tokens returned (1 to 200).
    cursor
        The `X-Next-Cursor` of the previous page; send the same `environment`.
    sort
        What the list is sorted by: `issued` (the default), `environment`, `purpose` (the
        job, else the asset; one minted by hand last), `principal` or `expires`.
    order
        `asc` or `desc`; by default `desc` for `issued`, `asc` for the others.
    principal
        The authenticated caller.
    service
        The task-token service for this request.

    Returns
    -------
    list of TaskTokenOut
        The tokens of this page.
    """
    page = service.list_live(
        principal, environment=environment, limit=limit, cursor=cursor, sort=sort, order=order
    )
    set_next_cursor(response, page.next_cursor)
    return [
        TaskTokenOut(
            key_id=listed.token.key_id,
            principal=listed.principal,
            environment=listed.token.environment,
            asset_scope=listed.token.asset_scope,
            issued_at=listed.token.issued_at,
            expires_at=listed.token.expires_at,
            run_id=listed.run_id,
            job=listed.job,
            asset=listed.asset,
        )
        for listed in page.items
    ]


@router.post("", response_model=TaskTokenMintOut, status_code=201, responses=errors(403, 404))
def mint_task_token(
    body: TaskTokenMintIn,
    principal: PrincipalRef = Depends(get_principal),
    service: TaskTokenService = Depends(get_task_token_service),
) -> TaskTokenMintOut:
    """Mint a task token.

    Mints a short-lived token that acts for a principal in one environment, limited to the
    assets named in the request. The answer holds the token's value, shown this once. Needs
    `MINT_TASK_TOKEN` on the instance; minting a token for a principal other than yourself
    also needs the `ADMIN` role (403 otherwise). Answers 404 when the principal or
    environment does not exist.

    Parameters
    ----------
    body
        The identity, environment, and asset scope to mint for.
    principal
        The authenticated caller.
    service
        The task-token service for this request.

    Returns
    -------
    TaskTokenMintOut
        The full token value — shown here exactly once.

    Raises
    ------
    lakana_core.errors.NotFoundError
        Mapped to 404 by `install_exception_handlers`.
    """
    minted = service.mint(
        principal,
        body.principal,
        environment=body.environment,
        asset_scope=body.asset_scope,
        ttl_seconds=body.ttl_seconds,
    )
    return TaskTokenMintOut(key_id=minted.key_id, token=minted.token)


@router.delete("/{key_id}", status_code=204, responses=errors(403, 404))
def revoke_task_token(
    key_id: str,
    principal: PrincipalRef = Depends(get_principal),
    service: TaskTokenService = Depends(get_task_token_service),
) -> None:
    """Revoke a task token.

    Ends the token before it expires. Revoking a token that is revoked already changes
    nothing. Needs `MINT_TASK_TOKEN` on the instance. An administrator may revoke any token
    and gets 404 for an unknown key id; anyone else may revoke only a token minted for
    themselves over REST and gets 403 otherwise, also for an unknown key id, so that key ids
    cannot be probed.

    Parameters
    ----------
    key_id
        The task token's key_id.
    principal
        The authenticated caller.
    service
        The task-token service for this request.

    Raises
    ------
    lakana_core.errors.NotFoundError
        Mapped to 404 by `install_exception_handlers`.
    """
    service.revoke(principal, key_id)
