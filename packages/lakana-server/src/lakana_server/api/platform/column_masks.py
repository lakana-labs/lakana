"""The column-masks resource of the platform API.

A mask's `namespace` is dot-joined (`gold.sales`) in requests and answers and
converted to and from the storage form in this module, as `grants.py` does for a
grant.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from lakana_core.auth import PrincipalRef
from lakana_core.errors import ValidationError
from lakana_core.identifiers import NamespacePath
from lakana_core.models import ColumnMask
from lakana_core.policy.column_masks import ColumnMaskService
from lakana_core.policy.shown import ShownPolicy
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
        namespace=_joined_to_dotted(row.namespace),
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
        namespace=_dotted_to_joined(body.namespace),
        table=body.table,
        column=body.column,
        expression=body.expression,
    )
    return _to_out(
        created,
        service.shown(principal, [created])[created.id],
        subject_names(session, [created]),
    )


@router.get("", response_model=list[ColumnMaskOut], responses=errors(403, 404))
def list_column_masks(
    principal_name: str | None = None,
    group_name: str | None = None,
    environment: str | None = None,
    namespace: str | None = None,
    table: str | None = None,
    principal: PrincipalRef = Depends(get_principal),
    service: ColumnMaskService = Depends(get_column_mask_service),
    session: Session = Depends(get_session),
) -> list[ColumnMaskOut]:
    """List the column masks of a principal, group or table.

    Give exactly one of `principal_name`, `group_name` or `environment` with `namespace` and
    `table`; a column mask always belongs to one table, so there is no list of all of them.
    Needs `MANAGE_POLICY` on the instance. Answers 404 when no principal or group has the
    name given and 422 when none of those combinations is given.

    Parameters
    ----------
    principal_name
        List this principal's own direct masks.
    group_name
        List this group's own direct masks.
    environment
        The environment to match, for a table-scoped listing.
    namespace
        The dot-joined namespace to match, for a table-scoped listing.
    table
        The table to match, for a table-scoped listing.
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
    names = subject_names(session, rows)
    return [_to_out(row, shown[row.id], names) for row in rows]


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
