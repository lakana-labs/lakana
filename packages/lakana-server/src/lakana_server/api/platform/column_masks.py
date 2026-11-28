"""The column-masks resource of the platform API.

A mask's `namespace` is dot-joined (`gold.sales`) in requests and answers and
converted to and from the storage form in this module, as `grants.py` does for a
grant.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.orm import Session

from lakana_core.auth import PrincipalRef
from lakana_core.models import ColumnMask
from lakana_core.policy.column_masks import ColumnMaskService
from lakana_core.policy.shown import ShownPolicy
from lakana_server.api.platform.listing import (
    OrderParam,
    PlaceCursorParam,
    SortParam,
    set_next_cursor,
)
from lakana_server.api.platform.namespace_forms import dotted_namespace, joined_namespace
from lakana_server.api.platform.schemas import ColumnMaskCreate, ColumnMaskOut
from lakana_server.api.platform.subjects import SubjectNames, subject_names
from lakana_server.api.responses import errors
from lakana_server.api.routing import AuthenticateFirstRoute
from lakana_server.deps import (
    check_statement,
    get_column_mask_service,
    get_managed_write_principal,
    get_principal,
    get_session,
)

router = APIRouter(
    route_class=AuthenticateFirstRoute,
    prefix="/api/v1/column-masks",
    tags=["column-masks"],
    responses=errors(401),
)


def _to_out(row: ColumnMask, shown: ShownPolicy, names: SubjectNames) -> ColumnMaskOut:
    """Build a response body from a ColumnMask row.

    Parameters
    ----------
    row
        The row to convert.
    shown
        How a listing shows the mask: its column and its expression in the table's current
        column names, and whether the mask is bound.
    names
        The names of the row's principal or group.

    Returns
    -------
    ColumnMaskOut
        The response body, with `namespace` converted to its dotted form.
    """
    return ColumnMaskOut(
        id=row.id,
        principal_id=row.principal_id,
        group_id=row.group_id,
        environment=row.environment,
        namespace=dotted_namespace(row.namespace),
        table=row.table,
        column=shown.column or row.column,
        expression=shown.expression,
        created_by=row.created_by,
        created_at=row.created_at,
        bound=shown.bound,
        principal=names.principal(row.principal_id),
        group=names.group(row.group_id),
    )


@router.post("", response_model=ColumnMaskOut, status_code=201, responses=errors(403, 404, 409))
def create_column_mask(
    body: ColumnMaskCreate,
    request: Request,
    principal: PrincipalRef = Depends(get_managed_write_principal),
    service: ColumnMaskService = Depends(get_column_mask_service),
    session: Session = Depends(get_session),
) -> ColumnMaskOut:
    """Create a column mask.

    From the web interface it needs a passkey touch within the last 15 minutes (403
    `StepUpRequired`); a token never does.

    Answers 409 when an access file manages the scope; `override_managed` with
    `override_reason` overrides it (ADMIN on the instance; audited).

    Hides or rewrites a column of a table for a principal or group, by a SQL expression. The
    mask binds to the table, not its name. Needs `MANAGE_POLICY` on the instance. Answers
    404 when the principal, group or table does not exist and 422 for a column the table
    does not have, an expression that is not valid, or one longer than the server's
    statement limit.

    Parameters
    ----------
    body
        The column mask to create.
    request
        The request, used to reach the statement length limit.
    principal
        The authenticated caller.
    service
        The column mask service for this request.
    session
        The request's database session, to name the people and groups the answer is for.

    Returns
    -------
    ColumnMaskOut
        The created column mask.

    Raises
    ------
    lakana_core.errors.ValidationError
        Mapped to 422 by `install_exception_handlers`, including for a column the
        table does not have and for an expression longer than
        `LAKANA_QUERY_LIMITS__MAX_STATEMENT_CHARS`.
    lakana_core.errors.NotFoundError
        Mapped to 404.
    """
    check_statement(request, body.expression, "The column-mask expression")
    created = service.create(
        principal,
        principal=body.principal,
        group=body.group,
        environment=body.environment,
        namespace=joined_namespace(body.namespace),
        table=body.table,
        column=body.column,
        expression=body.expression,
    )
    return _to_out(
        created,
        service.shown(principal, [created])[created.id],
        subject_names(session, [created]),
    )


# Each list route declares and documents its own query parameters, which the OpenAPI document and
# the REST reference are built from; these repeat those of `list_row_filters`, with `column`.
# jscpd:ignore-start
@router.get("", response_model=list[ColumnMaskOut], responses=errors(403, 404))
def list_column_masks(
    response: Response,
    principal_name: str | None = None,
    group_name: str | None = None,
    environment: str | None = None,
    namespace: str | None = None,
    table: str | None = None,
    limit: int = Query(default=100, ge=1, le=200),
    cursor: PlaceCursorParam = None,
    sort: SortParam = None,
    order: OrderParam = None,
    principal: PrincipalRef = Depends(get_principal),
    service: ColumnMaskService = Depends(get_column_mask_service),
    session: Session = Depends(get_session),
) -> list[ColumnMaskOut]:
    """List column masks.

    Lists the active column masks, newest first, or in the order `sort` and `order` ask for.
    Every filter is optional, and they combine: `principal_name` or `group_name` (not both) for
    whom a column mask is made to, and `environment`, with `namespace` and then `table`, for
    where it applies (`namespace` needs `environment`, and `table` needs `namespace`; 422
    otherwise). Needs `MANAGE_POLICY` on the instance. Answers 404 when no principal or group
    has the name given. The cursor of the next page is in the `X-Next-Cursor` response header.
    Answers 422 for an unknown `sort` or `order`, or a cursor this list did not issue in this
    order; send the same sort with every page.

    Parameters
    ----------
    response
        The response, for the cursor header.
    principal_name
        Only the column masks made to this principal.
    group_name
        Only the column masks made to this group.
    environment
        Only the column masks in this environment.
    namespace
        Only the column masks on tables of this dot-joined namespace.
    table
        Only the column masks on this table.
    limit
        Most column masks returned (1 to 200).
    cursor
        The `X-Next-Cursor` of the previous page; send the same filters.
    sort
        What the list is sorted by: `created` (the default), `who`, `on` or `column`.
    order
        `asc` or `desc`; by default `desc` for `created`, `asc` for the others.
    principal
        The authenticated caller.
    service
        The column mask service for this request.
    session
        The request's database session, to name the people and groups the answer is for.

    Returns
    -------
    list of ColumnMaskOut
        The matching column masks.
    """
    page = service.list_page(
        principal,
        principal_name=principal_name,
        group_name=group_name,
        environment=environment,
        namespace=None if namespace is None else joined_namespace(namespace),
        table=table,
        limit=limit,
        cursor=cursor,
        sort=sort,
        order=order,
    )
    set_next_cursor(response, page.next_cursor)
    shown = service.shown(principal, page.items)
    names = subject_names(session, page.items)
    return [_to_out(row, shown[row.id], names) for row in page.items]
    # jscpd:ignore-end


@router.delete("/{column_mask_id}", status_code=204, responses=errors(403, 404, 409))
def revoke_column_mask(
    column_mask_id: uuid.UUID,
    principal: PrincipalRef = Depends(get_managed_write_principal),
    service: ColumnMaskService = Depends(get_column_mask_service),
) -> None:
    """Revoke a column mask.

    From the web interface it needs a passkey touch within the last 15 minutes (403
    `StepUpRequired`); a token never does.

    Answers 409 when an access file manages the scope; `override_managed` with
    `override_reason` overrides it (ADMIN on the instance; audited).

    Needs `MANAGE_POLICY` on the instance. Answers 404 when there is no such column mask.

    Parameters
    ----------
    column_mask_id
        The column mask's id.
    principal
        The authenticated caller.
    service
        The column mask service for this request.

    Raises
    ------
    lakana_core.errors.NotFoundError
        Mapped to 404 by `install_exception_handlers`.
    """
    service.revoke(principal, column_mask_id)
