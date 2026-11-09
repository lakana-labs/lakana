"""Invites of the platform API: an administrator invites a new person by a single-use link.

The link creates the person's account (a user with no rights) when it is used, within 7 days.
Its name is reserved in any letter case while it is open. Issuing needs the ADMIN role and, in
a browser, a fresh passkey touch (step-up); listing and withdrawing need the ADMIN role. The
link is shown once, in the answer that issues it, and never listed. Every route answers 409
while the interface is off.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Response

from lakana_core.auth import PrincipalRef
from lakana_core.identity.links import LinkService
from lakana_server.api.platform.schemas import InviteIn, InviteOut, OpenInviteOut
from lakana_server.api.responses import errors
from lakana_server.api.routing import AuthenticateFirstRoute
from lakana_server.browser_session import require_interface
from lakana_server.deps import get_link_service, get_principal

router = APIRouter(
    route_class=AuthenticateFirstRoute,
    prefix="/api/v1/invites",
    tags=["sign-in"],
    dependencies=[Depends(require_interface)],
    responses=errors(401, 403, 409),
)


@router.post("", response_model=InviteOut, status_code=201)
def create_invite(
    body: InviteIn,
    principal: PrincipalRef = Depends(get_principal),
    links: LinkService = Depends(get_link_service),
) -> InviteOut:
    """Invite a new person.

    Answers the link to send them, shown only here: it works once, for 7 days, and creates
    their account with no rights; grant roles after you have confirmed who signed up. Needs the
    ADMIN role and, in a browser, a passkey touch in the last 15 minutes (403
    `StepUpRequired`). 409 when a principal (active or not) or another open invite has the name
    in any letter case, or it is the first administrator's name.

    Parameters
    ----------
    body
        ``{"name", "display_name"?}``.
    principal
        The authenticated caller.
    links
        The link service for this request.

    Returns
    -------
    InviteOut
        The invite and its link.
    """
    issued = links.issue_invite(principal, name=body.name, display_name=body.display_name)
    return InviteOut(
        id=issued.id,
        name=body.name,
        display_name=body.display_name.strip() if body.display_name else None,
        url=issued.url,
        expires_at=issued.expires_at,
    )


@router.get("", response_model=list[OpenInviteOut])
def list_invites(
    principal: PrincipalRef = Depends(get_principal),
    links: LinkService = Depends(get_link_service),
) -> list[OpenInviteOut]:
    """List the open invites, oldest first.

    Without their links, which are shown only when issued. Needs the ADMIN role.

    Parameters
    ----------
    principal
        The authenticated caller.
    links
        The link service for this request.

    Returns
    -------
    list of OpenInviteOut
        Invites neither used, withdrawn nor expired.
    """
    return [
        OpenInviteOut(
            id=row.id,
            name=row.invite_name or "",
            display_name=row.invite_display_name,
            created_at=row.created_at,
            expires_at=row.expires_at,
        )
        for row in links.list_open(principal, "invite")
    ]


@router.delete("/{invite_id}", status_code=204, response_class=Response, responses=errors(404))
def withdraw_invite(
    invite_id: uuid.UUID,
    principal: PrincipalRef = Depends(get_principal),
    links: LinkService = Depends(get_link_service),
) -> Response:
    """Withdraw an open invite; its link stops working and its name is free again.

    Needs the ADMIN role. 409 when it was used or withdrawn already.

    Parameters
    ----------
    invite_id
        The invite's id.
    principal
        The authenticated caller.
    links
        The link service for this request.

    Returns
    -------
    Response
        204.
    """
    links.withdraw(principal, invite_id, kind="invite")
    return Response(status_code=204)
