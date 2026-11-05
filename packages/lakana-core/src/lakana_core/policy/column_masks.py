"""Column mask creation, listing, and revocation."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.audit.service import AuditService
from lakana_core.auth import Action, AuthorizationService, PrincipalRef, Securable, SecurableKind
from lakana_core.errors import NotFoundError, ValidationError
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.models import AuditEventType, ColumnMask, Group, Principal
from lakana_core.policy.columns import (
    ColumnSource,
    PolicyTarget,
    binding_for,
    list_columns,
    policy_table,
    read_table_description,
    type_checks_apply,
    validate_for_table,
)
from lakana_core.policy.expression_types import check_expression_types
from lakana_core.policy.expressions import columns_of
from lakana_core.policy.field_binding import PolicyBinding
from lakana_core.policy.shown import PolicyShowing


def _check_column_exists(
    catalog: ColumnSource,
    p: PrincipalRef,
    ref: TableRef,
    environment: str,
    column: str,
    expression: str,
) -> tuple[int | None, bool, PolicyBinding]:
    """Refuse a mask on a missing column or of another type; give its schema, typed flag, binding.

    The comparison is exact, as it is when the mask is applied to a query, so
    a mask accepted here is a mask the query path can honour.

    Parameters
    ----------
    catalog
        Where to read the table's current columns from.
    p
        The acting principal, already authorized to manage policies.
    ref
        The table the mask is for.
    environment
        Its environment, named in the message.
    column
        The column the mask is for.
    expression
        The mask, already validated.

    Returns
    -------
    tuple of int or None, bool and PolicyBinding
        The id of the table's current schema as read for this check, so that the mask records
        the schema whose columns and types were checked (`None` for a table without field ids),
        and whether the types were checked: `False` for a CSV or JSON table, whose stored types
        are only a guess, and for a nanosecond timestamp column, which has no cast target; and
        the binding of the masked column and the columns the mask reads to the field ids of that
        same read.

    Raises
    ------
    ValidationError
        If the table has no column called `column`; or if the mask compares values of different
        types, or its own type differs from the column's (the message names the column, both
        types and a value that keeps the column's type).
    AmbiguousPolicyColumnError
        If the masked column or a column the mask reads matches two columns of the table
        without regard to case.
    PermissionDenied
        If `p` lacks READ_TABLE on the table, which reading its columns needs.
    ConflictError
        If the table's columns could not be read.
    """
    dotted = f"{ref.namespace}.{ref.name}"
    description = read_table_description(catalog, p, ref, policy="mask")
    names = [spec.name for spec in description.columns]
    if column in names:
        typed = type_checks_apply(description) and check_expression_types(
            expression, kind="mask", columns=description.columns, masked_column=column
        )
        # After the type check, so a column the table lacks gets its message, which names the
        # expression, before the binding's.
        binding = binding_for(description, columns_of(expression), masked_column=column)
        return description.schema_id, typed, binding
    message = f"Table {dotted!r} in environment {environment!r} has no column {column!r}. "
    message += list_columns(names)
    if names:
        message += " Check the spelling; column names are case-sensitive."
    lookalikes = [name for name in names if name.lower() == column.lower()]
    if lookalikes:
        message += f" Did you mean {lookalikes[0]!r}?"
    raise ValidationError(message)


class ColumnMaskService(PolicyShowing):
    """Create, list, and revoke column masks.

    Parameters
    ----------
    session
        Database session. Each mutating method commits internally.
    authz
        Authorization service consulted before every operation, including
        the list methods — matching `GrantService`'s precedent of gating
        list operations the same as mutations.
    catalog
        Where `create` reads a table's current columns from, so a mask on a column the
        table does not have is refused whoever calls it. `CatalogService` is one.
    """

    def __init__(
        self, session: Session, authz: AuthorizationService, catalog: ColumnSource
    ) -> None:
        self._session = session
        self._authz = authz
        self._catalog = catalog

    def _get_principal(self, name: str) -> Principal:
        """Look up a principal by name.

        Parameters
        ----------
        name
            Principal name.

        Returns
        -------
        Principal
            The matching row.

        Raises
        ------
        NotFoundError
            If no principal has that name.
        """
        principal = self._session.execute(
            select(Principal).where(Principal.name == name)
        ).scalar_one_or_none()
        if principal is None:
            raise NotFoundError(f"No principal named {name!r}.")
        return principal

    def _get_group(self, name: str) -> Group:
        """Look up a group by name.

        Parameters
        ----------
        name
            Group name.

        Returns
        -------
        Group
            The matching row.

        Raises
        ------
        NotFoundError
            If no group has that name.
        """
        group = self._session.execute(select(Group).where(Group.name == name)).scalar_one_or_none()
        if group is None:
            raise NotFoundError(f"No group named {name!r}.")
        return group

    def _subject_name(self, row: ColumnMask) -> str:
        """Resolve a column mask's own subject (principal or group) to its name.

        Parameters
        ----------
        row
            The column mask whose `principal_id`/`group_id` to resolve.

        Returns
        -------
        str
            The name of whichever subject the row is bound to.
        """
        if row.principal_id is not None:
            principal = self._session.get(Principal, row.principal_id)
            assert principal is not None  # nosec B101 -- FK guarantees the row still exists
            return principal.name
        group = self._session.get(Group, row.group_id)
        assert group is not None  # nosec B101 -- FK guarantees the row still exists
        return group.name

    def check_target(
        self,
        p: PrincipalRef,
        *,
        environment: str,
        namespace: str,
        table: str,
        column: str,
        expression: str,
    ) -> PolicyTarget:
        """Run every check `create` runs on a mask's table, column and expression, writing nothing.

        `create` calls it after authorizing; an access file plan (#46) calls it after its own
        authorization, so a planned mask is refused exactly as its single route would be.

        Parameters
        ----------
        p
            The acting principal, already authorized to manage policies.
        environment
            The environment the masked table lives in.
        namespace
            The namespace, in its unit-separator-joined storage form.
        table
            The table name.
        column
            The masked column.
        expression
            The restricted-subset replacement value.

        Returns
        -------
        PolicyTarget
            The ids, schema, binding and type-checked flag `create` stores.

        Raises
        ------
        ValidationError
            If the target is a view, or `expression` is outside the restricted subset, or the
            column does not exist or has another type than the expression.
        ConflictError
            If the table's columns cannot be read.
        NotFoundError
            If no such table exists.
        PermissionDenied
            If `p` lacks READ_TABLE on the table.
        """
        validate_for_table(
            self._catalog,
            p,
            environment=environment,
            namespace=namespace,
            table=table,
            expression=expression,
            kind="mask",
        )
        environment_id, table_id, ref = policy_table(self._session, environment, namespace, table)
        schema_id, typed, binding = _check_column_exists(
            self._catalog, p, ref, environment, column, expression
        )
        return PolicyTarget(environment_id, table_id, schema_id, binding, typed)

    def create(
        self,
        p: PrincipalRef,
        *,
        principal: str | None = None,
        group: str | None = None,
        environment: str,
        namespace: str,
        table: str,
        column: str,
        expression: str,
    ) -> ColumnMask:
        """Create a column mask.

        Parameters
        ----------
        p
            The acting principal.
        principal
            Name of the principal this mask applies to. Exactly one of
            `principal`/`group` must be given.
        group
            Name of the group this mask applies to.
        environment
            The environment the masked table lives in.
        namespace
            The namespace, in its unit-separator-joined storage form.
        table
            The table name.
        column
            The masked column's name.
        expression
            The replacement value — validated against the mask grammar
            (comparisons, boolean logic, literals, column references,
            context functions, plus `substr`/`substring`/`||`/`md5`)
            before the row is ever stored.

        Returns
        -------
        ColumnMask
            The created row.

        Raises
        ------
        ValidationError
            If the target is a view, or subject fields don't satisfy the exactly-one-of rule, or
            `expression` is outside the mask grammar, or the table has no column `column`
            (compared exactly, after authorization), or the mask compares values of different
            types or is not of the column's own type (checked against the table's current
            schema).
        ConflictError
            If the table's columns cannot be read.
        NotFoundError
            If `principal`/`group` names a principal or group that doesn't
            exist, or `environment`/`namespace`/`table` names a table that
            doesn't exist.
        PermissionDenied
            If the acting principal may not manage policies, or lacks READ_TABLE on the
            table (reading its columns needs it).
        """
        self._authz.authorize(p, Action.MANAGE_POLICY, Securable.instance_scope())
        if (principal is None) == (group is None):
            raise ValidationError("Exactly one of 'principal' or 'group' must be given.")
        principal_id = self._get_principal(principal).id if principal is not None else None
        group_id = self._get_group(group).id if group is not None else None
        target = self.check_target(
            p,
            environment=environment,
            namespace=namespace,
            table=table,
            column=column,
            expression=expression,
        )
        row = ColumnMask(
            id=uuid.uuid4(),
            principal_id=principal_id,
            group_id=group_id,
            environment=environment,
            namespace=namespace,
            table=table,
            environment_id=target.environment_id,
            table_id=target.table_id,
            column=column,
            expression=expression,
            schema_id=target.schema_id,
            field_ids=target.binding.field_ids,
            column_field_id=target.binding.column_field_id,
            binding=target.binding.binding,
            type_checked=target.type_checked,
            created_by=p.id,
            created_at=datetime.now(UTC),
        )
        self._session.add(row)
        AuditService(self._session).record(
            p.id,
            AuditEventType.COLUMN_MASK_CREATED,
            securable=Securable(
                kind=SecurableKind.TABLE,
                environment=environment,
                namespace=NamespacePath.parse(namespace),
                table=table,
            ),
            detail={"id": str(row.id), "subject": principal if principal is not None else group},
        )
        self._session.commit()
        return row

    def list_for_principal(self, p: PrincipalRef, principal_name: str) -> list[ColumnMask]:
        """List a principal's own active, direct column masks.

        Parameters
        ----------
        p
            The acting principal.
        principal_name
            The principal whose masks to list.

        Returns
        -------
        list of ColumnMask
            Every active mask made directly to this principal.

        Raises
        ------
        NotFoundError
            If no principal has `principal_name`.
        PermissionDenied
            If the acting principal may not manage policies.
        """
        self._authz.authorize(p, Action.MANAGE_POLICY, Securable.instance_scope())
        principal = self._get_principal(principal_name)
        return list(
            self._session.execute(
                select(ColumnMask).where(
                    ColumnMask.principal_id == principal.id, ColumnMask.revoked_at.is_(None)
                )
            )
            .scalars()
            .all()
        )

    def list_for_group(self, p: PrincipalRef, group_name: str) -> list[ColumnMask]:
        """List a group's active, direct column masks.

        Parameters
        ----------
        p
            The acting principal.
        group_name
            The group whose masks to list.

        Returns
        -------
        list of ColumnMask
            Every active mask made directly to this group.

        Raises
        ------
        NotFoundError
            If no group has `group_name`.
        PermissionDenied
            If the acting principal may not manage policies.
        """
        self._authz.authorize(p, Action.MANAGE_POLICY, Securable.instance_scope())
        group = self._get_group(group_name)
        return list(
            self._session.execute(
                select(ColumnMask).where(
                    ColumnMask.group_id == group.id, ColumnMask.revoked_at.is_(None)
                )
            )
            .scalars()
            .all()
        )

    def list_for_table(
        self, p: PrincipalRef, *, environment: str, namespace: str, table: str
    ) -> list[ColumnMask]:
        """List every active column mask on a specific table — an exact match.

        Parameters
        ----------
        p
            The acting principal.
        environment
            The environment to match.
        namespace
            The namespace (unit-separator-joined) to match.
        table
            The table to match.

        Returns
        -------
        list of ColumnMask
            Every active mask whose stored table exactly matches.

        Raises
        ------
        PermissionDenied
            If the acting principal may not manage policies.
        """
        self._authz.authorize(p, Action.MANAGE_POLICY, Securable.instance_scope())
        return list(
            self._session.execute(
                select(ColumnMask).where(
                    ColumnMask.environment == environment,
                    ColumnMask.namespace == namespace,
                    ColumnMask.table == table,
                    ColumnMask.revoked_at.is_(None),
                )
            )
            .scalars()
            .all()
        )

    def revoke(self, p: PrincipalRef, column_mask_id: uuid.UUID) -> None:
        """Revoke a column mask.

        Idempotent: revoking an already-revoked column mask is a no-op —
        it leaves the existing `revoked_at`/`revoked_by` untouched and
        records no second `COLUMN_MASK_REVOKED` event.

        Parameters
        ----------
        p
            The acting principal.
        column_mask_id
            The column mask's id.

        Raises
        ------
        NotFoundError
            If no column mask has that id.
        PermissionDenied
            If the acting principal may not manage policies.
        """
        self._authz.authorize(p, Action.MANAGE_POLICY, Securable.instance_scope())
        row = self._session.get(ColumnMask, column_mask_id)
        if row is None:
            raise NotFoundError(f"No column mask {column_mask_id}.")
        if row.revoked_at is not None:
            return
        row.revoked_at = datetime.now(UTC)
        row.revoked_by = p.id
        AuditService(self._session).record(
            p.id,
            AuditEventType.COLUMN_MASK_REVOKED,
            securable=Securable(
                kind=SecurableKind.TABLE,
                environment=row.environment,
                namespace=NamespacePath.parse(row.namespace),
                table=row.table,
            ),
            detail={"id": str(row.id), "subject": self._subject_name(row)},
        )
        self._session.commit()
