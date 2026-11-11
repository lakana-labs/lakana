"""The principals resource of the platform API."""

from __future__ import annotations

from collections.abc import Iterable

from fastapi import APIRouter, Depends, Query, Response

from lakana_core.auth import PrincipalRef, Securable
from lakana_core.grants.service import GrantService, scope_filter
from lakana_core.identity.pictures import PictureService
from lakana_core.identity.principals import PrincipalService
from lakana_core.models import Principal, PrincipalType
from lakana_server.api.platform.grants import permissions_out
from lakana_server.api.platform.listing import set_next_cursor
from lakana_server.api.platform.schemas import PermissionsOut, PrincipalCreate, PrincipalOut
from lakana_server.api.responses import errors
from lakana_server.api.routing import AuthenticateFirstRoute
from lakana_server.deps import (
    get_grant_service,
    get_managed_write_principal,
    get_picture_service,
    get_principal,
    get_principal_service,
)

router = APIRouter(
    route_class=AuthenticateFirstRoute,
    prefix="/api/v1/principals",
    tags=["principals"],
    responses=errors(401),
)


def principals_out(rows: Iterable[Principal], pictures: PictureService) -> list[PrincipalOut]:
    """Describe principals, each with its picture's etag, read in one statement.

    Parameters
    ----------
    rows
        The principals, already authorized for the caller.
    pictures
        The picture service, asked for the etags.

    Returns
    -------
    list of PrincipalOut
        One body per principal, in order.
    """
    found = list(rows)
    etags = pictures.etags_of(row.id for row in found)
    return [
        PrincipalOut.model_validate(row).model_copy(update={"picture": etags.get(row.id)})
        for row in found
    ]


@router.post("", response_model=PrincipalOut, status_code=201, responses=errors(403, 409))
def create_principal(
    body: PrincipalCreate,
    principal: PrincipalRef = Depends(get_managed_write_principal),
    service: PrincipalService = Depends(get_principal_service),
) -> PrincipalOut:
    """Create a principal.

    From the web interface it needs a passkey touch within the last 15 minutes (403
    `StepUpRequired`); a token never does.

    Answers 409 when an access file manages the scope; `override_managed` with
    `override_reason` overrides it (ADMIN on the instance; audited). Only a service account is
    managed by a file: for a person the override answers 422.

    Creates a user or a service account. Needs `MANAGE_PRINCIPAL` on the instance. Answers
    409 when the name is taken.

    Parameters
    ----------
    body
        The principal to create.
    principal
        The authenticated caller.
    service
        The principal service for this request.

    Returns
    -------
    PrincipalOut
        The created principal.

    Raises
    ------
    lakana_core.errors.AlreadyExistsError
        Mapped to 409 by `install_exception_handlers`.
    """
    created = service.create_principal(principal, name=body.name, type=PrincipalType[body.type])
    return PrincipalOut.model_validate(created)


@router.get("/{name}", response_model=PrincipalOut, responses=errors(403, 404))
def get_principal_by_name(
    name: str,
    principal: PrincipalRef = Depends(get_principal),
    service: PrincipalService = Depends(get_principal_service),
    pictures: PictureService = Depends(get_picture_service),
) -> PrincipalOut:
    """Show a principal.

    Needs `MANAGE_PRINCIPAL` on the instance. Answers 404 when there is no principal of that
    name.

    Parameters
    ----------
    name
        Principal name.
    principal
        The authenticated caller.
    service
        The principal service for this request.
    pictures
        The picture service, for the principal's picture etag.

    Returns
    -------
    PrincipalOut
        The principal.

    Raises
    ------
    lakana_core.errors.NotFoundError
        Mapped to 404 by `install_exception_handlers`.
    """
    return principals_out([service.get_principal_by_name(principal, name)], pictures)[0]


@router.get("/{name}/permissions", response_model=PermissionsOut, responses=errors(403, 404))
def explain_permissions(
    name: str,
    environment: str | None = Query(default=None, max_length=255),
    namespace: str | None = Query(default=None, max_length=1024),
    table: str | None = Query(default=None, max_length=255),
    principal: PrincipalRef = Depends(get_principal),
    service: GrantService = Depends(get_grant_service),
) -> PermissionsOut:
    """Explain what a principal may do.

    Says, action by action, what the principal may do on an object and through which grant.
    The object is named like the filter of the grants list; none names the instance. A
    caller may ask about itself; about anyone else it needs the `ADMIN` role on the
    instance. Answers 404 to an administrator for a principal or object that does not exist,
    and 422 when the object is named incompletely.

    Parameters
    ----------
    name
        The principal asked about.
    environment
        The object's environment.
    namespace
        The object's dot-joined namespace.
    table
        The object's table.
    principal
        The authenticated caller.
    service
        The grant service for this request.

    Returns
    -------
    PermissionsOut
        The authorizer's answer for each action the routes ask for on such an object.

    Raises
    ------
    lakana_core.errors.PermissionDenied
        Mapped to 403: another principal, without the `ADMIN` role on the instance.
    lakana_core.errors.NotFoundError
        Mapped to 404, for an administrator: no such principal or object.
    """
    scope = scope_filter(environment, namespace, table) or Securable.instance_scope()
    return permissions_out(service.explain(principal, name, scope))


@router.get("", response_model=list[PrincipalOut], responses=errors(403))
def list_principals(
    response: Response,
    q: str | None = Query(default=None, max_length=255),
    limit: int = Query(default=100, ge=1, le=200),
    cursor: str | None = Query(default=None, max_length=512),
    principal: PrincipalRef = Depends(get_principal),
    service: PrincipalService = Depends(get_principal_service),
    pictures: PictureService = Depends(get_picture_service),
) -> list[PrincipalOut]:
    """List principals.

    Lists the principals, newest first. Needs `MANAGE_PRINCIPAL` on the instance. The cursor
    of the next page is in the `X-Next-Cursor` response header and is absent on the last
    page.

    Parameters
    ----------
    response
        The response, for the cursor header.
    q
        Only principals whose name contains this text, in any letter case.
    limit
        Most principals returned (1 to 200).
    cursor
        The `X-Next-Cursor` of the previous page; send the same `q`.
    principal
        The authenticated caller.
    service
        The principal service for this request.
    pictures
        The picture service, for each principal's picture etag.

    Returns
    -------
    list of PrincipalOut
        The principals of this page.
    """
    page = service.list_principals(principal, q=q, limit=limit, cursor=cursor)
    set_next_cursor(response, page.next_cursor)
    return principals_out(page.items, pictures)


@router.delete("/{name}", status_code=204, responses=errors(403, 404, 409))
def deactivate_principal(
    name: str,
    principal: PrincipalRef = Depends(get_managed_write_principal),
    service: PrincipalService = Depends(get_principal_service),
) -> None:
    """Deactivate a principal.

    Stops the principal from signing in. The record stays, with every grant, token and audit
    entry that refers to it. Needs `MANAGE_PRINCIPAL` on the instance. Answers 404 when
    there is no principal of that name and 409 when it is the last active administrator.

    Answers 409 when an access file manages the scope; `override_managed` with
    `override_reason` overrides it (ADMIN on the instance; audited). Only a service account is
    managed by a file: for a person the override answers 422.

    From the web interface it needs a passkey touch within the last 15 minutes (403
    `StepUpRequired`); a token never does.

    Parameters
    ----------
    name
        Principal name.
    principal
        The authenticated caller.
    service
        The principal service for this request.

    Raises
    ------
    lakana_core.errors.NotFoundError
        Mapped to 404 by `install_exception_handlers`.
    """
    service.deactivate_principal(principal, name)
