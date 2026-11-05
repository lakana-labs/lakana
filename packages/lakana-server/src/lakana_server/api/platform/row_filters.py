"""The row-filters resource of the platform API.

A filter's `namespace` is dot-joined (`gold.sales`) in requests and answers; this
module converts it to and from the form the database stores, like `grants.py`.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request

from lakana_core.auth import PrincipalRef
from lakana_core.errors import ValidationError
from lakana_core.identifiers import NamespacePath
from lakana_core.models import RowFilter
from lakana_core.policy.row_filters import RowFilterService
from lakana_core.policy.shown import ShownPolicy
from lakana_server.api.platform.schemas import RowFilterCreate, RowFilterOut
from lakana_server.api.responses import errors
from lakana_server.api.routing import AuthenticateFirstRoute
from lakana_server.deps import check_statement, get_principal, get_row_filter_service

router = APIRouter(
    route_class=AuthenticateFirstRoute,
    prefix="/api/v1/row-filters",
    tags=["row-filters"],
    responses=errors(401),
)


def _dotted_to_joined(namespace: str) -> str:
    """Convert a dot-joined namespace path to its unit-separator storage form.

    Parameters
    ----------
    namespace
        A dotted path such as ``"gold.sales"``.

    Returns
    -------
    str
        The unit-separator-joined storage form.
    """
    return NamespacePath.from_parts(namespace.split(".")).joined()


def _joined_to_dotted(namespace: str) -> str:
    """Convert a stored unit-separator namespace path to its dotted form.

    Parameters
    ----------
    namespace
        The unit-separator-joined storage form.

    Returns
    -------
    str
        The dot-joined display form.
    """
    return ".".join(NamespacePath.parse(namespace).parts)


def _to_out(row: RowFilter, shown: ShownPolicy) -> RowFilterOut:
    """Build a response body from a RowFilter row.

    Parameters
    ----------
    row
        The row to convert.
    shown
        How a listing shows the filter: its expression in the table's current column names, and
        whether the filter is bound.

    Returns
    -------
    RowFilterOut
        The response body, with `namespace` converted to its dotted form.
    """
    return RowFilterOut(
        id=row.id,
        principal_id=row.principal_id,
        group_id=row.group_id,
        environment=row.environment,
        namespace=_joined_to_dotted(row.namespace),
        table=row.table,
        expression=shown.expression,
        created_by=row.created_by,
        created_at=row.created_at,
        bound=shown.bound,
    )


@router.post("", response_model=RowFilterOut, status_code=201, responses=errors(403, 404))
def create_row_filter(
    body: RowFilterCreate,
    request: Request,
    principal: PrincipalRef = Depends(get_principal),
    service: RowFilterService = Depends(get_row_filter_service),
) -> RowFilterOut:
    """Create a row filter.

    Restricts which rows of a table a principal or group sees, by a SQL condition. The
    filter binds to the table, not its name. Needs `MANAGE_POLICY` on the instance. Answers
    404 when the principal, group or table does not exist and 422 for a condition that is
    not valid or is longer than the server's statement limit.

    Parameters
    ----------
    body
        The row filter to create.
    request
        The request, used to reach the statement length limit.
    principal
        The authenticated caller.
    service
        The row filter service for this request.

    Returns
    -------
    RowFilterOut
        The created row filter.

    Raises
    ------
    lakana_core.errors.ValidationError
        Mapped to 422 by `install_exception_handlers`, including for an expression longer than
        `LAKANA_QUERY_LIMITS__MAX_STATEMENT_CHARS`.
    lakana_core.errors.NotFoundError
        Mapped to 404.
    """
    check_statement(request, body.expression, "The row-filter expression")
    created = service.create(
        principal,
        principal=body.principal,
        group=body.group,
        environment=body.environment,
        namespace=_dotted_to_joined(body.namespace),
        table=body.table,
        expression=body.expression,
    )
    return _to_out(created, service.shown(principal, [created])[created.id])


@router.get("", response_model=list[RowFilterOut], responses=errors(403, 404))
def list_row_filters(
    principal_name: str | None = None,
    group_name: str | None = None,
    environment: str | None = None,
    namespace: str | None = None,
    table: str | None = None,
    principal: PrincipalRef = Depends(get_principal),
    service: RowFilterService = Depends(get_row_filter_service),
) -> list[RowFilterOut]:
    """List the row filters of a principal, group or table.

    Give exactly one of `principal_name`, `group_name` or `environment` with `namespace` and
    `table`; a row filter always belongs to one table, so there is no list of all of them.
    Needs `MANAGE_POLICY` on the instance. Answers 404 when no principal or group has the
    name given and 422 when none of those combinations is given.

    Parameters
    ----------
    principal_name
        List this principal's own direct filters.
    group_name
        List this group's own direct filters.
    environment
        The environment to match, for a table-scoped listing.
    namespace
        The dot-joined namespace to match, for a table-scoped listing.
    table
        The table to match, for a table-scoped listing.
    principal
        The authenticated caller.
    service
        The row filter service for this request.

    Returns
    -------
    list of RowFilterOut
        The matching row filters.

    Raises
    ------
    lakana_core.errors.ValidationError
        If none of the valid parameter combinations is given.
    """
    if principal_name is not None:
        rows = service.list_for_principal(principal, principal_name)
    elif group_name is not None:
        rows = service.list_for_group(principal, group_name)
    elif environment is not None and namespace is not None and table is not None:
        rows = service.list_for_table(
            principal, environment=environment, namespace=_dotted_to_joined(namespace), table=table
        )
    else:
        raise ValidationError(
            "Provide 'principal_name', 'group_name', or 'environment'+'namespace'+'table' together."
        )
    shown = service.shown(principal, rows)
    return [_to_out(row, shown[row.id]) for row in rows]


@router.delete("/{row_filter_id}", status_code=204, responses=errors(403, 404))
def revoke_row_filter(
    row_filter_id: uuid.UUID,
    principal: PrincipalRef = Depends(get_principal),
    service: RowFilterService = Depends(get_row_filter_service),
) -> None:
    """Revoke a row filter.

    Needs `MANAGE_POLICY` on the instance. Answers 404 when there is no such row filter.

    Parameters
    ----------
    row_filter_id
        The row filter's id.
    principal
        The authenticated caller.
    service
        The row filter service for this request.

    Raises
    ------
    lakana_core.errors.NotFoundError
        Mapped to 404 by `install_exception_handlers`.
    """
    service.revoke(principal, row_filter_id)
