"""The tokens resource of the platform API, nested under a principal."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from lakana_core.auth import PrincipalRef
from lakana_core.identity.pats import PatService
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


@router.post("", response_model=TokenIssueOut, status_code=201, responses=errors(403, 404))
def issue_token(
    name: str,
    body: TokenIssueIn,
    principal: PrincipalRef = Depends(get_principal),
    service: PatService = Depends(get_pat_service),
) -> TokenIssueOut:
    """Issue a personal access token.

    From the web interface it needs a passkey touch within the last 15 minutes (403
    `StepUpRequired`); a token never does.

    Issues a token that signs in as the principal. The answer holds the token's full value,
    which is shown this once and cannot be read again. Needs the `ADMIN` role on the
    instance. Answers 404 when there is no such principal.

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

    Lists the principal's personal access tokens. The token values are never included. Needs
    the `ADMIN` role on the instance. Answers 404 when there is no such principal.

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
    return [TokenOut.model_validate(row) for row in service.list(principal, name)]


@router.delete("/{key_id}", status_code=204, responses=errors(403, 404))
def revoke_token(
    name: str,
    key_id: str,
    principal: PrincipalRef = Depends(get_principal),
    service: PatService = Depends(get_pat_service),
) -> None:
    """Revoke a personal access token.

    The token stops working at once. Needs the `ADMIN` role on the instance. Answers 404
    when the principal or the token does not exist.

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
