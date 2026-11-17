"""The tokens resource of the platform API, nested under a principal."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response

from lakana_core.auth import PrincipalRef
from lakana_core.identity.pats import ListedPat, PatService
from lakana_server.api.platform.listing import set_next_cursor
from lakana_server.api.platform.schemas import TokenIssueIn, TokenIssueOut, TokenOut
from lakana_server.api.responses import errors
from lakana_server.api.routing import AuthenticateFirstRoute
from lakana_server.deps import get_pat_service, get_principal

router = APIRouter(
    route_class=AuthenticateFirstRoute,
    prefix="/api/v1/principals/{name}/tokens",
    tags=["tokens"],
    responses=errors(401),
)


all_router = APIRouter(
    route_class=AuthenticateFirstRoute,
    prefix="/api/v1/tokens",
    tags=["tokens"],
    responses=errors(401),
)


def _token_out(listed: ListedPat) -> TokenOut:
    """Build the response body of a listed token.

    Parameters
    ----------
    listed
        The token and its owner's name.

    Returns
    -------
    TokenOut
        The token's public fields, never its secret.
    """
    row = listed.token
    return TokenOut(
        key_id=row.key_id,
        name=row.name,
        owner=listed.owner,
        created_at=row.created_at,
        last_used_at=row.last_used_at,
        expires_at=row.expires_at,
        revoked_at=row.revoked_at,
    )


@router.post("", response_model=TokenIssueOut, status_code=201, responses=errors(403, 404))
def issue_token(
    name: str,
    body: TokenIssueIn,
    principal: PrincipalRef = Depends(get_principal),
    service: PatService = Depends(get_pat_service),
) -> TokenIssueOut:
    """Issue a personal access token.

    From the web interface it needs a passkey touch within the last 15 minutes (403
    `StepUpRequired`); a token is never asked for a touch, and no token (personal, task or
    CI) of a non-administrator can create a personal token.

    Issues a token that signs in as the principal. The answer holds the token's full value,
    which is shown this once and cannot be read again. For another principal it needs the
    `ADMIN` role on the instance. For oneself, unless one is an administrator, it works only
    from the web interface: no token (personal, task or CI) of a non-administrator can create a
    personal token, so a stolen token cannot mint replacements (403). Answers 404 when there is
    no such principal.

    Parameters
    ----------
    name
        The principal the PAT authenticates as.
    body
        The PAT's label.
    principal
        The authenticated caller.
    service
        The PAT service for this request.

    Returns
    -------
    TokenIssueOut
        The full token value — shown here exactly once.

    Raises
    ------
    lakana_core.errors.NotFoundError
        Mapped to 404 by `install_exception_handlers`.
    """
    issued = service.issue(principal, name, name=body.name)
    return TokenIssueOut(key_id=issued.key_id, name=issued.name, token=issued.token)


@router.get("", response_model=list[TokenOut], responses=errors(403, 404))
def list_tokens(
    name: str,
    principal: PrincipalRef = Depends(get_principal),
    service: PatService = Depends(get_pat_service),
) -> list[TokenOut]:
    """List the tokens of a principal.

    Lists the principal's personal access tokens, revoked and expired ones too, each with its
    owner and its creation, last-use, expiry and revocation times. The token values are never
    included. Anyone may list their own with a personal token or a web session; anyone else's
    needs the `ADMIN` role on the instance, and is refused alike whether or not the principal
    exists.
    Answers 404 when there is no such principal.

    Parameters
    ----------
    name
        The principal whose PATs to list.
    principal
        The authenticated caller.
    service
        The PAT service for this request.

    Returns
    -------
    list of TokenOut
        Every PAT for that principal.

    Raises
    ------
    lakana_core.errors.NotFoundError
        Mapped to 404 by `install_exception_handlers`.
    """
    return [_token_out(listed) for listed in service.list(principal, name)]


@all_router.get("", response_model=list[TokenOut], responses=errors(403, 404))
def list_all_tokens(
    response: Response,
    owner: str | None = Query(default=None, max_length=255),
    limit: int = Query(default=100, ge=1, le=200),
    cursor: str | None = Query(default=None, max_length=512),
    principal: PrincipalRef = Depends(get_principal),
    service: PatService = Depends(get_pat_service),
) -> list[TokenOut]:
    """List every owner's live tokens.

    Lists the personal access tokens that are neither revoked nor expired, newest first, each
    with its owner. The token values are never included. Needs the `ADMIN` role on the
    instance; a task or CI token is refused. The cursor of the next page is in the
    `X-Next-Cursor` response header and is absent on the last page. Answers 404 when `owner`
    names no principal.

    Parameters
    ----------
    response
        The response, for the cursor header.
    owner
        Only this principal's tokens.
    limit
        Most tokens returned (1 to 200).
    cursor
        The `X-Next-Cursor` of the previous page; send the same `owner`.
    principal
        The authenticated caller.
    service
        The PAT service for this request.

    Returns
    -------
    list of TokenOut
        The tokens of this page.

    Raises
    ------
    lakana_core.errors.NotFoundError
        Mapped to 404 by `install_exception_handlers`.
    """
    page = service.list_all(principal, owner=owner, limit=limit, cursor=cursor)
    set_next_cursor(response, page.next_cursor)
    return [_token_out(listed) for listed in page.items]


@router.delete("/{key_id}", status_code=204, responses=errors(403, 404))
def revoke_token(
    name: str,
    key_id: str,
    principal: PrincipalRef = Depends(get_principal),
    service: PatService = Depends(get_pat_service),
) -> None:
    """Revoke a personal access token.

    The token stops working at once. Anyone may revoke their own with a personal token or a
    web session, without a passkey touch; anyone else's needs the `ADMIN` role on the
    instance. Answers 404 when the principal or the token does not exist.

    Parameters
    ----------
    name
        The principal the PAT belongs to.
    key_id
        The PAT's key_id.
    principal
        The authenticated caller.
    service
        The PAT service for this request.

    Raises
    ------
    lakana_core.errors.NotFoundError
        Mapped to 404 by `install_exception_handlers`.
    """
    service.revoke(principal, name, key_id)
