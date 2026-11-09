"""Recovery links of the platform API: let a person who lost their passkeys back in.

An administrator issues a single-use link for another person (never for themself: Add passkey
is for that), valid for 1 hour. Using it removes the person's passkeys, ends their sessions and
notifies them and every administrator. Recovering an administrator is for the server operator
only (the bootstrap token, `lakana recovery create <name>` where the server runs). Issuing
needs the ADMIN role and, in a browser, a fresh passkey touch. A new link withdraws the
person's previous open one. Every route answers 409 while the interface is off.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Response

from lakana_core.auth import PrincipalRef
from lakana_core.identity.links import LinkService
from lakana_server.api.platform.schemas import OpenRecoveryLinkOut, RecoveryLinkOut
from lakana_server.api.responses import errors
from lakana_server.api.routing import AuthenticateFirstRoute
from lakana_server.browser_session import require_interface
from lakana_server.deps import get_link_service, get_principal

router = APIRouter(
    route_class=AuthenticateFirstRoute,
    prefix="/api/v1/principals",
    tags=["sign-in"],
    dependencies=[Depends(require_interface)],
    responses=errors(401, 403, 409),
)

links_router = APIRouter(
    route_class=AuthenticateFirstRoute,
    prefix="/api/v1/recovery-links",
    tags=["sign-in"],
    dependencies=[Depends(require_interface)],
    responses=errors(401, 403, 409),
)


@router.post(
    "/{name}/recovery-link",
    response_model=RecoveryLinkOut,
    status_code=201,
    responses=errors(404),
)
def create_recovery_link(
    name: str,
    principal: PrincipalRef = Depends(get_principal),
    links: LinkService = Depends(get_link_service),
) -> RecoveryLinkOut:
    """Issue a recovery link for a person who lost their passkeys.

    Answers the link to send them, shown only here: it works once, for 1 hour. Using it
    removes their passkeys, ends their sessions and tells them and every administrator. Needs
    the ADMIN role and, in a browser, a passkey touch in the last 15 minutes. 403 for anyone
    who holds or has ever held the ADMIN role, directly or through a group, unless you are the
    server operator (run `lakana recovery create <name>` with the bootstrap token where the
    server runs); 409 for yourself (use Add passkey), a service account or a deactivated
    account.

    Parameters
    ----------
    name
        The person's principal name.
    principal
        The authenticated caller.
    links
        The link service for this request.

    Returns
    -------
    RecoveryLinkOut
        The link.
    """
    issued = links.issue_recovery(principal, principal_name=name)
    return RecoveryLinkOut(id=issued.id, name=name, url=issued.url, expires_at=issued.expires_at)


@links_router.get("", response_model=list[OpenRecoveryLinkOut])
def list_recovery_links(
    principal: PrincipalRef = Depends(get_principal),
    links: LinkService = Depends(get_link_service),
) -> list[OpenRecoveryLinkOut]:
    """List the open recovery links, oldest first, without the links themselves.

    Needs the ADMIN role.

    Parameters
    ----------
    principal
        The authenticated caller.
    links
        The link service for this request.

    Returns
    -------
    list of OpenRecoveryLinkOut
        Links neither used, withdrawn nor expired.
    """
    return [
        OpenRecoveryLinkOut(
            id=row.id,
            name=links.name_of(row),
            created_at=row.created_at,
            expires_at=row.expires_at,
        )
        for row in links.list_open(principal, "recovery")
    ]


@links_router.delete("/{link_id}", status_code=204, response_class=Response, responses=errors(404))
def withdraw_recovery_link(
    link_id: uuid.UUID,
    principal: PrincipalRef = Depends(get_principal),
    links: LinkService = Depends(get_link_service),
) -> Response:
    """Withdraw an open recovery link; it stops working at once.

    Needs the ADMIN role. 409 when it was used or withdrawn already.

    Parameters
    ----------
    link_id
        The link's id.
    principal
        The authenticated caller.
    links
        The link service for this request.

    Returns
    -------
    Response
        204.
    """
    links.withdraw(principal, link_id, kind="recovery")
    return Response(status_code=204)
