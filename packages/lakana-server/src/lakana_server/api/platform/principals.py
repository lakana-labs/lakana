"""The principals resource of the platform API."""

from __future__ import annotations

from collections.abc import Iterable

from fastapi import APIRouter, Depends, Query, Response

from lakana_core.auth import PrincipalRef, Securable
from lakana_core.grants.admin import ineffective_note
from lakana_core.grants.service import GrantService, scope_filter
from lakana_core.identity.oidc_links import OidcLinkService, ProviderLink
from lakana_core.identity.pictures import PictureService
from lakana_core.identity.principals import PrincipalService
from lakana_core.models import Principal, PrincipalType
from lakana_server.api.platform.grants import permissions_out
from lakana_server.api.platform.listing import set_next_cursor
from lakana_server.api.platform.namespace_forms import dotted_namespace
from lakana_server.api.platform.schemas import (
    HeldGrantOut,
    PermissionsOut,
    PrincipalCreate,
    PrincipalOut,
    ProviderLinkOut,
)
from lakana_server.api.responses import errors
from lakana_server.api.routing import AuthenticateFirstRoute
from lakana_server.deps import (
    get_grant_service,
    get_managed_write_principal,
    get_oidc_link_service,
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


def principals_out(
    rows: Iterable[Principal],
    pictures: PictureService,
    service: PrincipalService,
    principal: PrincipalRef,
) -> list[PrincipalOut]:
    """Describe principals, each with its picture's etag, read in one statement.

    Parameters
    ----------
    rows
        The principals, already authorized for the caller.
    pictures
        The picture service, asked for the etags.
    service
        The principal service, asked which of them hold or have held ADMIN.
    principal
        The caller: `held_admin` is filled in only for an administrator, `provider_link` for an
        administrator or the person.

    Returns
    -------
    list of PrincipalOut
        One body per principal, in order.
    """
    found = list(rows)
    etags = pictures.etags_of(row.id for row in found)
    held = service.held_admin_of(principal, found)
    links = service.provider_links_of(principal, found)
    return [
        PrincipalOut.model_validate(row).model_copy(
            update={
                "picture": etags.get(row.id),
                "held_admin": None if held is None else held[row.id],
                "provider_link": _link_out(links[row.id]),
            }
        )
        for row in found
    ]


def _link_out(link: ProviderLink | None) -> ProviderLinkOut | None:
    """Describe a person's link to the identity provider.

    Parameters
    ----------
    link
        The link, or `None`.

    Returns
    -------
    ProviderLinkOut or None
        Its body.
    """
    if link is None:
        return None
    return ProviderLinkOut(
        issuer=link.issuer,
        email=link.email,
        name=link.name,
        last_sign_in_at=link.last_sign_in_at,
        token_window_ends_at=link.token_window_ends_at,
    )


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
    held = service.held_admin_of(principal, [created])
    return PrincipalOut.model_validate(created).model_copy(
        update={"held_admin": None if held is None else held[created.id]}
    )


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
    return principals_out(
        [service.get_principal_by_name(principal, name)], pictures, service, principal
    )[0]


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


@router.get("/{name}/grants", response_model=list[HeldGrantOut], responses=errors(403, 404))
def list_held_grants(
    name: str,
    principal: PrincipalRef = Depends(get_principal),
    service: GrantService = Depends(get_grant_service),
) -> list[HeldGrantOut]:
    """List the grants a principal holds.

    The live grants naming the principal and those of every group it belongs to now, newest
    first, each with the group's name (`null` for a direct grant). A caller may ask about
    itself with a session or a personal token, with no right at all; about anyone else it
    needs the `ADMIN` role on the instance, and a task or CI token may not ask even about
    its own principal. The refusal comes before any lookup, so it never says whether the
    principal exists; an administrator gets 404 for one that does not. It carries nothing
    about other people: no `granted_by`, no principal id.

    Parameters
    ----------
    name
        The principal asked about.
    principal
        The authenticated caller.
    service
        The grant service for this request.

    Returns
    -------
    list of HeldGrantOut
        The grants, not paged: one person's are few.

    Raises
    ------
    lakana_core.errors.PermissionDenied
        Mapped to 403: another principal, or a task or CI token, without the `ADMIN` role.
    lakana_core.errors.NotFoundError
        Mapped to 404, for an administrator: no such principal.
    """
    return [
        HeldGrantOut(
            id=held.grant.id,
            role=held.grant.role,
            action=held.grant.action,
            kind=held.grant.kind,
            environment=held.grant.environment,
            namespace=dotted_namespace(held.grant.namespace),
            table=held.grant.table,
            granted_at=held.grant.granted_at,
            note=ineffective_note(held.grant.action),
            group=held.group,
        )
        for held in service.held_grants(principal, name)
    ]


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
    return principals_out(page.items, pictures, service, principal)


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


@router.delete("/{name}/oidc-link", status_code=204, responses=errors(403, 404, 409))
def unlink_provider(
    name: str,
    principal: PrincipalRef = Depends(get_principal),
    links: OidcLinkService = Depends(get_oidc_link_service),
) -> None:
    """Unlink a person from the identity provider.

    Deletes the person's stored passkeys, revokes all their personal API tokens and ends their
    sessions, in one step; they come back with a recovery link (`lakana recovery create`), and
    then sign in with a passkey again until they link once more. To offboard someone, deactivate
    them instead. Needs the ADMIN role on the instance and, from the web interface, a passkey
    touch (or provider step-up) within the last 15 minutes (403 `StepUpRequired`). Answers 404
    for an unknown name and 409 for yourself or a person who is not linked.

    Parameters
    ----------
    name
        The person's principal name.
    principal
        The authenticated caller.
    links
        The link service for this request.
    """
    links.unlink(principal, name)
