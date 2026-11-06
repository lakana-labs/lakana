"""The grants resource of the platform API.

`namespace` crosses this boundary dot-joined (`gold.sales`), converted to
and from the unit-separator storage form here — matching
`namespaces.py`'s existing convention for the same underlying value.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, Response

from lakana_core.auth import Action, PrincipalRef, Securable
from lakana_core.grants.admin import ineffective_note
from lakana_core.grants.explain import PermissionReport
from lakana_core.grants.service import GrantService, scope_filter
from lakana_core.identifiers import NamespacePath
from lakana_core.models import Grant
from lakana_core.pagination import parse_choices
from lakana_server.api.platform.listing import set_next_cursor
from lakana_server.api.platform.schemas import (
    GrantCreate,
    GrantOut,
    PermissionOut,
    PermissionsOut,
    ScopeOut,
    ViaOut,
)
from lakana_server.api.responses import errors
from lakana_server.api.routing import AuthenticateFirstRoute
from lakana_server.deps import get_grant_service, get_managed_write_principal, get_principal

router = APIRouter(
    route_class=AuthenticateFirstRoute,
    prefix="/api/v1/grants",
    tags=["grants"],
    responses=errors(401),
)


def _dotted_to_joined(namespace: str | None) -> str | None:
    """Convert a dot-joined namespace path to its unit-separator storage form.

    Parameters
    ----------
    namespace
        A dotted path such as ``"gold.sales"``, or `None`.

    Returns
    -------
    str or None
        The unit-separator-joined storage form, or `None` unchanged.
    """
    if namespace is None:
        return None
    return NamespacePath.from_parts(namespace.split(".")).joined()


def _joined_to_dotted(namespace: str | None) -> str | None:
    """Convert a stored unit-separator namespace path to its dotted form.

    Parameters
    ----------
    namespace
        The unit-separator-joined storage form, or `None`.

    Returns
    -------
    str or None
        The dot-joined display form, or `None` unchanged.
    """
    if namespace is None:
        return None
    return ".".join(NamespacePath.parse(namespace).parts)


def _to_out(grant: Grant) -> GrantOut:
    """Build a response body from a Grant row.

    Parameters
    ----------
    grant
        The row to convert.

    Returns
    -------
    GrantOut
        The response body, with `namespace` converted to its dotted form.
    """
    return GrantOut(
        id=grant.id,
        principal_id=grant.principal_id,
        group_id=grant.group_id,
        role=grant.role,
        action=grant.action,
        kind=grant.kind,
        environment=grant.environment,
        namespace=_joined_to_dotted(grant.namespace),
        table=grant.table,
        granted_by=grant.granted_by,
        granted_at=grant.granted_at,
        note=ineffective_note(grant.action),
    )


@router.post("", response_model=GrantOut, status_code=201, responses=errors(403, 404, 409))
def create_grant(
    body: GrantCreate,
    principal: PrincipalRef = Depends(get_managed_write_principal),
    service: GrantService = Depends(get_grant_service),
) -> GrantOut:
    """Grant a role or an action.

    Answers 409 when an access file manages the scope; `override_managed` with
    `override_reason` overrides it (ADMIN on the instance; audited).

    Gives a principal or a group a role or a single action on the instance, an environment,
    a namespace or a table. Needs the `ADMIN` role on the instance. Answers 404 when the
    principal, group or object does not exist and 422 when the grant is not valid for that
    object.

    Parameters
    ----------
    body
        The grant to create.
    principal
        The authenticated caller.
    service
        The grant service for this request.

    Returns
    -------
    GrantOut
        The created grant.

    Raises
    ------
    lakana_core.errors.ValidationError
        Mapped to 422 by `install_exception_handlers`.
    lakana_core.errors.NotFoundError
        Mapped to 404.
    """
    created = service.grant(
        principal,
        principal=body.principal,
        group=body.group,
        role=body.role,
        action=body.action,
        kind=body.kind,
        environment=body.environment,
        namespace=_dotted_to_joined(body.namespace),
        table=body.table,
    )
    return _to_out(created)


@router.get("", response_model=list[GrantOut], responses=errors(403, 404))
def list_grants(
    response: Response,
    principal_name: str | None = Query(default=None, max_length=255),
    group_name: str | None = Query(default=None, max_length=255),
    action: str | None = Query(default=None, max_length=64),
    environment: str | None = Query(default=None, max_length=255),
    namespace: str | None = Query(default=None, max_length=1024),
    table: str | None = Query(default=None, max_length=255),
    limit: int = Query(default=100, ge=1, le=200),
    cursor: str | None = Query(default=None, max_length=512),
    principal: PrincipalRef = Depends(get_principal),
    service: GrantService = Depends(get_grant_service),
) -> list[GrantOut]:
    """List grants.

    Lists the active grants, newest first. Every filter is optional and narrows the list,
    except that `principal_name` and `group_name` exclude each other (422 when both are
    given). The object filter matches exactly: `table` with `namespace` and `environment`
    lists the grants on that table, `namespace` with `environment` those on the namespace,
    `environment` alone those on the environment. Needs the `ADMIN` role on the instance.
    Answers 404 when no principal or group has the name given. The cursor of the next page
    is in the `X-Next-Cursor` response header.

    Parameters
    ----------
    response
        The response, for the cursor header.
    principal_name
        Only grants made to this principal.
    group_name
        Only grants made to this group.
    action
        Only grants that give this action (in any letter case): the action, or a role that
        carries it.
    environment
        The environment of the object filter.
    namespace
        The dot-joined namespace of the object filter.
    table
        The table of the object filter.
    limit
        Most grants returned (1 to 200).
    cursor
        The `X-Next-Cursor` of the previous page; send the same filters.
    principal
        The authenticated caller.
    service
        The grant service for this request.

    Returns
    -------
    list of GrantOut
        The matching grants.
    """
    actions = parse_choices("action", [action], Action, any_case=True) if action else ()
    page = service.list_grants(
        principal,
        principal_name=principal_name,
        group_name=group_name,
        action=actions[0] if actions else None,
        scope=scope_filter(environment, namespace, table),
        limit=limit,
        cursor=cursor,
    )
    set_next_cursor(response, page.next_cursor)
    return [_to_out(row) for row in page.items]


def scope_out(securable: Securable) -> ScopeOut:
    """Write an object for a response, its namespace dot-joined.

    Parameters
    ----------
    securable
        The object.

    Returns
    -------
    ScopeOut
        Its kind and names.
    """
    return ScopeOut(
        kind=securable.kind,
        environment=securable.environment,
        namespace=".".join(securable.namespace.parts) if securable.namespace is not None else None,
        table=securable.table,
    )


def permissions_out(report: PermissionReport) -> PermissionsOut:
    """Build the response body of an explanation.

    Parameters
    ----------
    report
        The explanation.

    Returns
    -------
    PermissionsOut
        The same, with every object written by `scope_out`.
    """
    return PermissionsOut(
        principal=report.principal,
        active=report.active,
        scope=scope_out(report.scope),
        permissions=[
            PermissionOut(
                action=p.action,
                allowed=p.allowed,
                via=[
                    ViaOut(
                        kind=v.kind,
                        grant_id=v.grant_id,
                        group=v.group,
                        role=v.role,
                        scope=scope_out(v.scope),
                    )
                    for v in p.via
                ],
                reason=p.reason,
            )
            for p in report.permissions
        ],
        note=report.note,
    )


@router.delete("/{grant_id}", status_code=204, responses=errors(403, 404, 409))
def revoke_grant(
    grant_id: uuid.UUID,
    principal: PrincipalRef = Depends(get_managed_write_principal),
    service: GrantService = Depends(get_grant_service),
) -> None:
    """Revoke a grant.

    Answers 409 when an access file manages the scope; `override_managed` with
    `override_reason` overrides it (ADMIN on the instance; audited).

    Needs the `ADMIN` role on the instance. Answers 404 when there is no such grant and 409
    when revoking it would leave the instance without an active administrator.

    Parameters
    ----------
    grant_id
        The grant's id.
    principal
        The authenticated caller.
    service
        The grant service for this request.

    Raises
    ------
    lakana_core.errors.NotFoundError
        Mapped to 404 by `install_exception_handlers`.
    lakana_core.errors.ConflictError
        Mapped to 409 — revoking this grant would leave zero active
        instance admins.
    """
    service.revoke(principal, grant_id)
