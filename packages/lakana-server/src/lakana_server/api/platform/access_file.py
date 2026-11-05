"""Access as code: export the live access as a file and plan a file's changes (#46).

Both routes need the ADMIN role on the instance. The file is parsed on the server; the
caller sends its text and gets every problem back in one answer.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from lakana_core.access.plan import AccessPlan, PlannedChange
from lakana_core.access.service import AccessService
from lakana_core.auth import PrincipalRef
from lakana_server.api.platform.schemas import (
    AccessChangeOut,
    AccessDriftOut,
    AccessExportOut,
    AccessFileIn,
    AccessPlanOut,
)
from lakana_server.api.responses import errors
from lakana_server.api.routing import AuthenticateFirstRoute
from lakana_server.deps import get_access_service, get_principal

router = APIRouter(
    route_class=AuthenticateFirstRoute,
    prefix="/api/v1/access",
    tags=["access"],
    responses=errors(401),
)


def _change_out(change: PlannedChange) -> AccessChangeOut:
    """Convert one planned change to its response body.

    Parameters
    ----------
    change
        The planned change.

    Returns
    -------
    AccessChangeOut
        The change in words with the file entry it comes from.
    """
    return AccessChangeOut(op=change.op, entry=change.path, text=change.text)


def _plan_out(plan: AccessPlan) -> AccessPlanOut:
    """Convert a plan to its response body.

    Parameters
    ----------
    plan
        The plan from `AccessService`.

    Returns
    -------
    AccessPlanOut
        The plan; `changed` holds the in-place changes, not all of them.
    """
    return AccessPlanOut(
        file_name=plan.file_name,
        file_sha256=plan.file_sha256,
        manages=list(plan.manages),
        creates=[_change_out(c) for c in plan.creates],
        changed=[_change_out(c) for c in plan.changed],
        removals=[_change_out(c) for c in plan.removals],
        releases=list(plan.releases),
        drift=[
            AccessDriftOut(scope=d.scope, text=d.text, by=d.by, reason=d.reason, at=d.at)
            for d in plan.drift
        ],
        problems=list(plan.problems),
        fingerprint=plan.fingerprint,
    )


@router.get("/export", response_model=AccessExportOut, responses=errors(403))
def export_access(
    scope: Annotated[list[str], Query(max_length=64)] = ["instance"],  # ruff: ignore[mutable-argument-default]
    principal: PrincipalRef = Depends(get_principal),
    service: AccessService = Depends(get_access_service),
) -> AccessExportOut:
    """Write the live access of some scopes as an access file.

    Needs the ADMIN role on the instance. Planning the text unedited plans nothing.

    Parameters
    ----------
    scope
        ``instance`` (the default) alone, or one or more environment names; repeat the
        parameter.
    principal
        The authenticated caller.
    service
        The access service.

    Returns
    -------
    AccessExportOut
        The file's text.
    """
    return AccessExportOut(text=service.export(principal, scope))


@router.post("/plan", response_model=AccessPlanOut, responses=errors(403, 413))
def plan_access(
    body: AccessFileIn,
    principal: PrincipalRef = Depends(get_principal),
    service: AccessService = Depends(get_access_service),
) -> AccessPlanOut:
    """Say what applying an access file would change, and every problem that stops it.

    Needs the ADMIN role on the instance. A file with problems is a plan with problems, not
    an error: the answer is 200 and lists all of them. Nothing is changed.

    Parameters
    ----------
    body
        The file's name and text.
    principal
        The authenticated caller.
    service
        The access service.

    Returns
    -------
    AccessPlanOut
        The changes, drift and problems.
    """
    return _plan_out(
        service.plan(
            principal, body.text, file_name=body.file_name, allow_lockout=body.allow_lockout
        )
    )
