"""Row filter creation, listing, and revocation."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.audit.service import AuditService
from lakana_core.auth import Action, AuthorizationService, PrincipalRef, Securable, SecurableKind
from lakana_core.catalog.identity import (
    refuse_view_as_policy_target,
    resolve_environment_id,
    resolve_table_id,
)
from lakana_core.errors import NotFoundError, ValidationError
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.models import AuditEventType, Group, Principal, RowFilter
from lakana_core.policy.columns import (
    ColumnSource,
    binding_for,
    list_columns,
    read_table_description,
    type_checks_apply,
    validate_for_table,
)
from lakana_core.policy.expression_types import check_expression_types
from lakana_core.policy.expressions import columns_of
from lakana_core.policy.field_binding import FIELD_IDS, PolicyBinding


def _check_columns_exist(
    catalog: ColumnSource, p: PrincipalRef, ref: TableRef, environment: str, expression: str
) -> tuple[int | None, PolicyBinding]:
    """Refuse a row filter that reads a missing column or mixes types; bind it to the table.

    The comparison ignores letter case, as the database does when the filter runs, so a
    filter accepted here is a filter the query path can apply. Then it checks the filter's types
    against the same columns (`check_expression_types`), unless the table is a CSV or JSON file
    (`type_checks_apply`), so the schema id recorded is the one
    whose types were checked. A filter that reads no column (`TRUE`, `has_group('staff')`) has no
    column to look up and does not read the table; its types are still checked.

    Parameters
    ----------
    catalog
        Where to read the table's current columns from.
    p
        The acting principal, already authorized to manage policies.
    ref
        The table the filter is for.
    environment
        Its environment, named in the message.
    expression
        The filter, already validated: every column in it is a bare name.

    Returns
    -------
    tuple of int or None and PolicyBinding
        The id of the table's current schema as read for this check, so that the filter records
        the schema whose columns were checked (`None` for a filter that reads no column and for
        a table without field ids), and the binding of its columns to the field ids of that same
        read.

    Raises
    ------
    ValidationError
        If the filter reads a column the table does not have; the message names the first such
        column in alphabetical order, so it is the same on every attempt, and lists the table's
        columns; or if the filter compares values of different types or uses a non-boolean as a
        condition (the message names the column, both types and what to write instead).
    """
    wanted = columns_of(expression)
    if not wanted:
        check_expression_types(expression, kind="filter", columns=[])
        return None, PolicyBinding(FIELD_IDS, {}, None)
    description = read_table_description(catalog, p, ref, policy="row filter")
    names = [spec.name for spec in description.columns]
    have = {name.lower() for name in names}
    for column in sorted(wanted, key=str.lower):
        if column.lower() in have:
            continue
        dotted = f"{ref.namespace}.{ref.name}"
        message = (
            f"Table {dotted!r} in environment {environment!r} has no column "
            f"{column!r}, which the row filter reads. {list_columns(names)}"
        )
        if names:
            message += " Check the spelling."
        raise ValidationError(message)
    if type_checks_apply(description):
        check_expression_types(expression, kind="filter", columns=description.columns)
    return description.schema_id, binding_for(description, wanted)


class RowFilterService:
    """Create, list, and revoke row filters.

    Parameters
    ----------
    session
        Database session. Each mutating method commits internally.
    authz
        Authorization service consulted before every operation, including
        the list methods — matching `GrantService`'s precedent of gating
        list operations the same as mutations.
    catalog
        Where `create` reads a table's current columns from, so a filter on a column the
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

    def _subject_name(self, row: RowFilter) -> str:
        """Resolve a row filter's own subject (principal or group) to its name.

        Parameters
        ----------
        row
            The row filter whose `principal_id`/`group_id` to resolve.

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

    def create(
        self,
        p: PrincipalRef,
        *,
        principal: str | None = None,
        group: str | None = None,
        environment: str,
        namespace: str,
        table: str,
        expression: str,
    ) -> RowFilter:
        """Create a row filter.

        Parameters
        ----------
        p
            The acting principal.
        principal
            Name of the principal this filter applies to. Exactly one of
            `principal`/`group` must be given.
        group
            Name of the group this filter applies to.
        environment
            The environment the filtered table lives in.
        namespace
            The namespace, in its unit-separator-joined storage form.
        table
            The table name.
        expression
            The restricted-subset predicate — validated before the row is
            ever stored.

        Returns
        -------
        RowFilter
            The created row.

        Raises
        ------
        ValidationError
            If the target is a view, or subject fields don't satisfy the exactly-one-of rule, or
            `expression` is outside the restricted subset (a qualified column such as
            `t.region` is outside it), or reads a column the table does not have (compared
            without regard to letter case, after authorization), or compares values of different
            types or uses a non-boolean as a condition (checked against the table's current
            schema, after the columns).
        ConflictError
            If the filter reads a column and the table's columns cannot be read.
        NotFoundError
            If `principal`/`group` names a principal or group that doesn't
            exist, or `environment`/`namespace`/`table` names a table that
            doesn't exist.
        PermissionDenied
            If the acting principal may not manage policies, or the filter reads a column
            and the principal lacks READ_TABLE on the table (reading its columns needs it).
        """
        self._authz.authorize(p, Action.MANAGE_POLICY, Securable.instance_scope())
        if (principal is None) == (group is None):
            raise ValidationError("Exactly one of 'principal' or 'group' must be given.")
        validate_for_table(
            self._catalog,
            p,
            environment=environment,
            namespace=namespace,
            table=table,
            expression=expression,
            kind="filter",
        )
        principal_id = self._get_principal(principal).id if principal is not None else None
        group_id = self._get_group(group).id if group is not None else None
        environment_id = resolve_environment_id(self._session, environment)
        table_id = resolve_table_id(self._session, environment, namespace, table)
        if environment_id is None or table_id is None:
            raise NotFoundError(
                f"No table {table!r} in {environment!r}/{NamespacePath.parse(namespace)}."
            )
        refuse_view_as_policy_target(self._session, table_id, namespace, table)
        ref = TableRef(
            environment=environment, namespace=NamespacePath.parse(namespace), name=table
        )
        schema_id, binding = _check_columns_exist(self._catalog, p, ref, environment, expression)
        row = RowFilter(
            id=uuid.uuid4(),
            principal_id=principal_id,
            group_id=group_id,
            environment=environment,
            namespace=namespace,
            table=table,
            environment_id=environment_id,
            table_id=table_id,
            expression=expression,
            schema_id=schema_id,
            field_ids=binding.field_ids,
            binding=binding.binding,
            created_by=p.id,
            created_at=datetime.now(UTC),
        )
        self._session.add(row)
        AuditService(self._session).record(
            p.id,
            AuditEventType.ROW_FILTER_CREATED,
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

    def list_for_principal(self, p: PrincipalRef, principal_name: str) -> list[RowFilter]:
        """List a principal's own active, direct row filters.

        Parameters
        ----------
        p
            The acting principal.
        principal_name
            The principal whose filters to list.

        Returns
        -------
        list of RowFilter
            Every active filter made directly to this principal.

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
                select(RowFilter).where(
                    RowFilter.principal_id == principal.id, RowFilter.revoked_at.is_(None)
                )
            )
            .scalars()
            .all()
        )

    def list_for_group(self, p: PrincipalRef, group_name: str) -> list[RowFilter]:
        """List a group's active, direct row filters.

        Parameters
        ----------
        p
            The acting principal.
        group_name
            The group whose filters to list.

        Returns
        -------
        list of RowFilter
            Every active filter made directly to this group.

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
                select(RowFilter).where(
                    RowFilter.group_id == group.id, RowFilter.revoked_at.is_(None)
                )
            )
            .scalars()
            .all()
        )

    def list_for_table(
        self, p: PrincipalRef, *, environment: str, namespace: str, table: str
    ) -> list[RowFilter]:
        """List every active row filter on a specific table — an exact match.

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
        list of RowFilter
            Every active filter whose stored table exactly matches.

        Raises
        ------
        PermissionDenied
            If the acting principal may not manage policies.
        """
        self._authz.authorize(p, Action.MANAGE_POLICY, Securable.instance_scope())
        return list(
            self._session.execute(
                select(RowFilter).where(
                    RowFilter.environment == environment,
                    RowFilter.namespace == namespace,
                    RowFilter.table == table,
                    RowFilter.revoked_at.is_(None),
                )
            )
            .scalars()
            .all()
        )

    def revoke(self, p: PrincipalRef, row_filter_id: uuid.UUID) -> None:
        """Revoke a row filter.

        Idempotent: revoking an already-revoked row filter is a no-op — it
        leaves the existing `revoked_at`/`revoked_by` untouched and records
        no second `ROW_FILTER_REVOKED` event.

        Parameters
        ----------
        p
            The acting principal.
        row_filter_id
            The row filter's id.

        Raises
        ------
        NotFoundError
            If no row filter has that id.
        PermissionDenied
            If the acting principal may not manage policies.
        """
        self._authz.authorize(p, Action.MANAGE_POLICY, Securable.instance_scope())
        row = self._session.get(RowFilter, row_filter_id)
        if row is None:
            raise NotFoundError(f"No row filter {row_filter_id}.")
        if row.revoked_at is not None:
            return
        row.revoked_at = datetime.now(UTC)
        row.revoked_by = p.id
        AuditService(self._session).record(
            p.id,
            AuditEventType.ROW_FILTER_REVOKED,
            securable=Securable(
                kind=SecurableKind.TABLE,
                environment=row.environment,
                namespace=NamespacePath.parse(row.namespace),
                table=row.table,
            ),
            detail={"id": str(row.id), "subject": self._subject_name(row)},
        )
        self._session.commit()
