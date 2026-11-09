"""Sign-in notices of the platform API: what the caller should know about sign-in events.

For example that a recovery link was used on their account, or on another person's account
(every administrator is told). Each person sees and dismisses only their own notices. Every
route answers 409 while the interface is off.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.auth import PrincipalRef
from lakana_core.identity.notices import NoticeService
from lakana_core.models import Principal
from lakana_server.api.platform.schemas import SignInNoticeOut
from lakana_server.api.responses import errors
from lakana_server.api.routing import AuthenticateFirstRoute
from lakana_server.browser_session import require_interface
from lakana_server.deps import get_notice_service, get_principal, get_session

router = APIRouter(
    route_class=AuthenticateFirstRoute,
    prefix="/api/v1/sign-in/notices",
    tags=["sign-in"],
    dependencies=[Depends(require_interface)],
    responses=errors(401, 409),
)


@router.get("", response_model=list[SignInNoticeOut])
def list_notices(
    principal: PrincipalRef = Depends(get_principal),
    notices: NoticeService = Depends(get_notice_service),
    session: Session = Depends(get_session),
) -> list[SignInNoticeOut]:
    """List your sign-in notices that you have not dismissed, newest first.

    A notice tells you about a sign-in event: `recovery_used` when a recovery link was used on
    your account or, for an administrator, on another person's. Dismiss one when you have read
    it.

    Parameters
    ----------
    principal
        The authenticated caller.
    notices
        The notice service for this request.
    session
        The request's database session, to name the people the notices are about.

    Returns
    -------
    list of SignInNoticeOut
        Your open notices.
    """
    rows = notices.list_for(principal)
    ids = {row.subject_id for row in rows if row.subject_id is not None}
    found = session.execute(select(Principal.id, Principal.name).where(Principal.id.in_(ids)))
    names = {principal_id: name for principal_id, name in found}
    return [
        SignInNoticeOut(
            id=row.id,
            kind=row.kind,
            subject=names.get(row.subject_id) if row.subject_id is not None else None,
            created_at=row.created_at,
        )
        for row in rows
    ]


@router.delete("/{notice_id}", status_code=204, response_class=Response, responses=errors(404))
def dismiss_notice(
    notice_id: uuid.UUID,
    principal: PrincipalRef = Depends(get_principal),
    notices: NoticeService = Depends(get_notice_service),
) -> Response:
    """Dismiss one of your sign-in notices.

    404 for a notice that is not yours or is dismissed already.

    Parameters
    ----------
    notice_id
        The notice's id.
    principal
        The authenticated caller.
    notices
        The notice service for this request.

    Returns
    -------
    Response
        204.
    """
    notices.dismiss(principal, notice_id)
    return Response(status_code=204)
