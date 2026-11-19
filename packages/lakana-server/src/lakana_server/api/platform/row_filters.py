"""The row-filters resource of the platform API.

A filter's `namespace` is dot-joined (`gold.sales`) in requests and answers; this
module converts it to and from the form the database stores, like `grants.py`.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.orm import Session

from lakana_core.auth import PrincipalRef
from lakana_core.models import RowFilter
from lakana_core.policy.row_filters import RowFilterService
from lakana_core.policy.shown import ShownPolicy
from lakana_server.api.platform.listing import set_next_cursor
from lakana_server.api.platform.namespace_forms import dotted_namespace, joined_namespace
from lakana_server.api.platform.schemas import RowFilterCreate, RowFilterOut
from lakana_server.api.platform.subjects import SubjectNames, subject_names
from lakana_server.api.responses import errors
from lakana_server.api.routing import AuthenticateFirstRoute
from lakana_server.deps import (
    check_statement,
    get_managed_write_principal,
    get_principal,
    get_row_filter_service,
    get_session,
)

router = APIRouter(
    route_class=AuthenticateFirstRoute,
    prefix="/api/v1/row-filters",
    tags=["row-filters"],
    responses=errors(401),
)


def _to_out(row: RowFilter, shown: ShownPolicy, names: SubjectNames) -> RowFilterOut:
    """Build a response body from a RowFilter row.

    Parameters
    ----------
    row
        The row to convert.
    shown
        How a listing shows the filter: its expression in the table's current column names, and
        whether the filter is bound.
    names
        The names of the row's principal or group.

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
        namespace=dotted_namespace(row.namespace),
        table=row.table,
        expression=shown.expression,
        created_by=row.created_by,
        created_at=row.created_at,
        bound=shown.bound,
        principal=names.principal(row.principal_id),
        group=names.group(row.group_id),
    )


@router.post("", response_model=RowFilterOut, status_code=201, responses=errors(403, 404, 409))
def create_row_filter(
    body: RowFilterCreate,
    request: Request,
    principal: PrincipalRef = Depends(get_managed_write_principal),
    service: RowFilterService = Depends(get_row_filter_service),
    session: Session = Depends(get_session),
) -> RowFilterOut:
    """Create a row filter.

    From the web interface it needs a passkey touch within the last 15 minutes (403
    `StepUpRequired`); a token never does.

    Answers 409 when an access file manages the scope; `override_managed` with
    `override_reason` overrides it (ADMIN on the instance; audited).

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
    session
        The request's database session, to name the people and groups the answer is for.

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
        namespace=joined_namespace(body.namespace),
        table=body.table,
        expression=body.expression,
    )
    return _to_out(
        created,
        service.shown(principal, [created])[created.id],
        subject_names(session, [created]),
    )


@router.get("", response_model=list[RowFilterOut], responses=errors(403, 404))
def list_row_filters(
    response: Response,
    principal_name: str | None = None,
    group_name: str | None = None,
    environment: str | None = None,
    namespace: str | None = None,
    table: str | None = None,
    limit: int = Query(default=100, ge=1, le=200),
    cursor: str | None = Query(default=None, max_length=512),
    principal: PrincipalRef = Depends(get_principal),
    service: RowFilterService = Depends(get_row_filter_service),
    session: Session = Depends(get_session),
) -> list[RowFilterOut]:
    """List row filters.

    Lists the active row filters, newest first. Every filter is optional, and they combine:
    `principal_name` or `group_name` (not both) for whom a row filter is made to, and
    `environment`, with `namespace` and then `table`, for where it applies (`namespace` needs
    `environment`, and `table` needs `namespace`; 422 otherwise). Needs `MANAGE_POLICY` on the
    instance. Answers 404 when no principal or group has the name given. The cursor of the
    next page is in the `X-Next-Cursor` response header.

    Parameters
    ----------
    response
        The response, for the cursor header.
    principal_name
        Only the row filters made to this principal.
    group_name
        Only the row filters made to this group.
    environment
        Only the row filters in this environment.
    namespace
        Only the row filters on tables of this dot-joined namespace.
    table
        Only the row filters on this table.
    limit
        Most row filters returned (1 to 200).
    cursor
        The `X-Next-Cursor` of the previous page; send the same filters.
    principal
        The authenticated caller.
    service
        The row filter service for this request.
    session
        The request's database session, to name the people and groups the answer is for.

    Returns
    -------
    list of RowFilterOut
        The matching row filters.
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
    )
    set_next_cursor(response, page.next_cursor)
    shown = service.shown(principal, page.items)
    names = subject_names(session, page.items)
    return [_to_out(row, shown[row.id], names) for row in page.items]


@router.delete("/{row_filter_id}", status_code=204, responses=errors(403, 404, 409))
def revoke_row_filter(
    row_filter_id: uuid.UUID,
    principal: PrincipalRef = Depends(get_managed_write_principal),
    service: RowFilterService = Depends(get_row_filter_service),
) -> None:
    """Revoke a row filter.

    From the web interface it needs a passkey touch within the last 15 minutes (403
    `StepUpRequired`); a token never does.

    Answers 409 when an access file manages the scope; `override_managed` with
    `override_reason` overrides it (ADMIN on the instance; audited).

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
