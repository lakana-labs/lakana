"""The groups resource of the platform API."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response

from lakana_core.auth import PrincipalRef
from lakana_core.identity.groups import GroupService
from lakana_server.api.platform.listing import set_next_cursor
from lakana_server.api.platform.schemas import GroupCreate, GroupOut, PrincipalOut
from lakana_server.api.responses import errors
from lakana_server.api.routing import AuthenticateFirstRoute
from lakana_server.deps import get_group_service, get_managed_write_principal, get_principal

router = APIRouter(
    route_class=AuthenticateFirstRoute,
    prefix="/api/v1/groups",
    tags=["groups"],
    responses=errors(401),
)


@router.post("", response_model=GroupOut, status_code=201, responses=errors(403, 409))
def create_group(
    body: GroupCreate,
    principal: PrincipalRef = Depends(get_managed_write_principal),
    service: GroupService = Depends(get_group_service),
) -> GroupOut:
    """Create a group.

    Answers 409 when an access file manages the scope; `override_managed` with
    `override_reason` overrides it (ADMIN on the instance; audited).

    Needs the `ADMIN` role on the instance. Answers 409 when the name is taken.

    Parameters
    ----------
    body
        The group to create.
    principal
        The authenticated caller.
    service
        The group service for this request.

    Returns
    -------
    GroupOut
        The created group.

    Raises
    ------
    lakana_core.errors.AlreadyExistsError
        Mapped to 409 by `install_exception_handlers`.
    """
    created = service.create_group(principal, name=body.name)
    return GroupOut.model_validate(created)


@router.get("", response_model=list[GroupOut], responses=errors(403))
def list_groups(
    response: Response,
    q: str | None = Query(default=None, max_length=255),
    limit: int = Query(default=100, ge=1, le=200),
    cursor: str | None = Query(default=None, max_length=512),
    principal: PrincipalRef = Depends(get_principal),
    service: GroupService = Depends(get_group_service),
) -> list[GroupOut]:
    """List groups.

    Lists the groups, newest first. Needs the `ADMIN` role on the instance. The cursor of
    the next page is in the `X-Next-Cursor` response header and is absent on the last page.

    Parameters
    ----------
    response
        The response, for the cursor header.
    q
        Only groups whose name contains this text, in any letter case.
    limit
        Most groups returned (1 to 200).
    cursor
        The `X-Next-Cursor` of the previous page; send the same `q`.
    principal
        The authenticated caller.
    service
        The group service for this request.

    Returns
    -------
    list of GroupOut
        The groups of this page.
    """
    page = service.list_groups(principal, q=q, limit=limit, cursor=cursor)
    set_next_cursor(response, page.next_cursor)
    return [GroupOut.model_validate(row) for row in page.items]


@router.delete("/{name}", status_code=204, responses=errors(403, 404, 409))
def delete_group(
    name: str,
    principal: PrincipalRef = Depends(get_managed_write_principal),
    service: GroupService = Depends(get_group_service),
) -> None:
    """Delete a group.

    Answers 409 when an access file manages the scope; `override_managed` with
    `override_reason` overrides it (ADMIN on the instance; audited).

    Needs the `ADMIN` role on the instance. Answers 404 when there is no such group and 409
    while an active grant, row filter or column mask still names it.

    Parameters
    ----------
    name
        Group name.
    principal
        The authenticated caller.
    service
        The group service for this request.

    Raises
    ------
    lakana_core.errors.NotFoundError
        Mapped to 404 by `install_exception_handlers`.
    lakana_core.errors.ConflictError
        Mapped to 409 by `install_exception_handlers`, if any active grant,
        row filter, or column mask still references the group.
    """
    service.delete_group(principal, name)


@router.get("/{name}/members", response_model=list[PrincipalOut], responses=errors(403, 404))
def list_members(
    name: str,
    principal: PrincipalRef = Depends(get_principal),
    service: GroupService = Depends(get_group_service),
) -> list[PrincipalOut]:
    """List the members of a group.

    Needs the `ADMIN` role on the instance. Answers 404 when there is no such group.

    Parameters
    ----------
    name
        The group's name.
    principal
        The authenticated caller.
    service
        The group service for this request.

    Returns
    -------
    list of PrincipalOut
        Every member of the group, ordered by name.

    Raises
    ------
    lakana_core.errors.NotFoundError
        Mapped to 404 by `install_exception_handlers`.
    """
    return [PrincipalOut.model_validate(row) for row in service.list_members(principal, name)]


@router.put("/{name}/members/{principal_name}", status_code=204, responses=errors(403, 404, 409))
def add_member(
    name: str,
    principal_name: str,
    principal: PrincipalRef = Depends(get_managed_write_principal),
    service: GroupService = Depends(get_group_service),
) -> None:
    """Add a principal to a group.

    Answers 409 when an access file manages the scope; `override_managed` with
    `override_reason` overrides it (ADMIN on the instance; audited).

    Adding a principal that is a member already changes nothing. Needs the `ADMIN` role on
    the instance. Answers 404 when the group or the principal does not exist.

    Parameters
    ----------
    name
        Group name.
    principal_name
        Principal to add.
    principal
        The authenticated caller.
    service
        The group service for this request.

    Raises
    ------
    lakana_core.errors.NotFoundError
        Mapped to 404 by `install_exception_handlers`.
    """
    service.add_member(principal, name, principal_name)


@router.delete("/{name}/members/{principal_name}", status_code=204, responses=errors(403, 404, 409))
def remove_member(
    name: str,
    principal_name: str,
    principal: PrincipalRef = Depends(get_managed_write_principal),
    service: GroupService = Depends(get_group_service),
) -> None:
    """Remove a principal from a group.

    Answers 409 when an access file manages the scope; `override_managed` with
    `override_reason` overrides it (ADMIN on the instance; audited).

    Needs the `ADMIN` role on the instance. Answers 404 when the group or the principal does
    not exist.

    Parameters
    ----------
    name
        Group name.
    principal_name
        Principal to remove.
    principal
        The authenticated caller.
    service
        The group service for this request.

    Raises
    ------
    lakana_core.errors.NotFoundError
        Mapped to 404 by `install_exception_handlers`.
    """
    service.remove_member(principal, name, principal_name)
