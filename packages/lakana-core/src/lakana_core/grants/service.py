"""Grant creation, listing, revocation, and the admin-lockout guard."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from lakana_core.audit.service import AuditService
from lakana_core.auth import Action, AuthorizationService, PrincipalRef, Securable, SecurableKind
from lakana_core.catalog.identity import namespace_exists, resolve_environment_id, resolve_table_id
from lakana_core.errors import ConflictError, NotFoundError, ValidationError
from lakana_core.grants.admin import ADMIN_ONLY_ACTIONS, authorize_admin_only
from lakana_core.grants.explain import PermissionReport, explain_permissions, grant_scope
from lakana_core.grants.roles import ROLE_ACTIONS, Role
from lakana_core.identifiers import NamespacePath
from lakana_core.models import AuditEventType, Grant, Group, GroupMembership, Principal
from lakana_core.pagination import Page, newest_first

_EXPECTED_FIELDS: dict[SecurableKind, frozenset[str]] = {
    SecurableKind.INSTANCE: frozenset(),
    SecurableKind.ENVIRONMENT: frozenset({"environment"}),
    SecurableKind.NAMESPACE: frozenset({"environment", "namespace"}),
    SecurableKind.TABLE: frozenset({"environment", "namespace", "table"}),
}
"""Which of environment/namespace/table a securable of each kind must set."""


def _validate_securable_fields(
    kind: SecurableKind, environment: str | None, namespace: str | None, table: str | None
) -> None:
    """Raise if the given fields don't match what `kind` expects.

    Parameters
    ----------
    kind
        The securable kind a grant is being created for.
    environment
        The environment field as given.
    namespace
        The namespace field as given.
    table
        The table field as given.

    Raises
    ------
    ValidationError
        If a field is set that `kind` doesn't expect, or a field `kind`
        requires is missing. Explicit, rather than inferring `kind` from
        which fields are set, so a caller who forgets `environment` on an
        environment-scoped grant gets refused instead of silently getting
        an instance-wide one.
    """
    expected = _EXPECTED_FIELDS[kind]
    given = {"environment": environment, "namespace": namespace, "table": table}
    for field, value in given.items():
        if (value is not None) != (field in expected):
            raise ValidationError(
                f"A {kind.value} grant must set exactly {sorted(expected) or '(nothing)'}; "
                f"got environment={environment!r}, namespace={namespace!r}, table={table!r}."
            )


def scope_filter(
    environment: str | None, namespace: str | None, table: str | None
) -> Securable | None:
    """Read the object a grants listing or an explanation is about from its three names.

    The kind follows from the names given: `table` makes a table, else `namespace` a
    namespace, else `environment` an environment.

    Parameters
    ----------
    environment
        The environment's name, or `None`.
    namespace
        The namespace, dot-joined (``gold.sales``), or `None`.
    table
        The table's name, or `None`.

    Returns
    -------
    Securable or None
        The object, or `None` when no name is given (the instance, for an explanation; no
        filter, for a listing).

    Raises
    ------
    ValidationError
        If a namespace is given without its environment, a table without its namespace, or a
        namespace is not a valid path.
    """
    if namespace is not None and environment is None:
        raise ValidationError(
            "A namespace needs its environment: pass `environment` too (`--env` on the "
            "command line)."
        )
    if table is not None and namespace is None:
        raise ValidationError(
            "A table needs its namespace and environment: pass `namespace` and `environment` "
            "too (`--ns` and `--env` on the command line)."
        )
    if environment is None:
        return None
    if namespace is None:
        return Securable.environment_scope(environment)
    path = NamespacePath.from_parts(namespace.split("."))
    if table is None:
        return Securable.namespace_scope(environment, path)
    return Securable.table_scope(environment, path, table)


class GrantService:
    """Create, list, and revoke grants, and explain a principal's access.

    Parameters
    ----------
    session
        Database session. Each mutating method commits internally.
    authz
        Authorization service consulted before every operation, the listing
        included — matching `GroupService`/`PrincipalService`'s precedent of
        gating list operations the same as mutations.

    Notes
    -----
    Every operation needs the `ADMIN` role on the instance, through
    `authorize_admin_only`, except a principal explaining its own access.
    """

    def __init__(self, session: Session, authz: AuthorizationService) -> None:
        self._session = session
        self._authz = authz

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

    def _resolve_securable_ids(
        self, kind: SecurableKind, environment: str | None, namespace: str | None, table: str | None
    ) -> tuple[uuid.UUID | None, uuid.UUID | None]:
        """Resolve a grant's target names to the ids that will bind it.

        Parameters
        ----------
        kind
            Which tier of the securable hierarchy the grant applies to.
        environment
            The environment field as given to `grant`, already validated by
            `_validate_securable_fields` against `kind`.
        namespace
            The namespace field as given to `grant`.
        table
            The table field as given to `grant`.

        Returns
        -------
        tuple of (uuid.UUID or None, uuid.UUID or None)
            `(environment_id, table_id)`. Both are `None` for an
            `INSTANCE`-kind grant. `table_id` is `None` unless `kind` is
            `TABLE`.

        Raises
        ------
        NotFoundError
            If `kind` is not `INSTANCE` and no environment named
            `environment` exists; or `kind` is `NAMESPACE` and no namespace
            matching `environment`/`namespace` exists; or `kind` is `TABLE`
            and no table matching `environment`/`namespace`/`table` exists.
        """
        if kind == SecurableKind.INSTANCE:
            return None, None
        assert environment is not None  # nosec B101 -- guaranteed by _validate_securable_fields
        environment_id = resolve_environment_id(self._session, environment)
        if environment_id is None:
            raise NotFoundError(f"No environment named {environment!r}.")
        if kind == SecurableKind.NAMESPACE:
            assert namespace is not None  # nosec B101 -- see above
            if not namespace_exists(self._session, environment, namespace):
                raise NotFoundError(
                    f"No namespace {NamespacePath.parse(namespace)} in {environment!r}."
                )
            return environment_id, None
        if kind != SecurableKind.TABLE:
            return environment_id, None
        assert namespace is not None and table is not None  # nosec B101 -- see above
        table_id = resolve_table_id(self._session, environment, namespace, table)
        if table_id is None:
            raise NotFoundError(
                f"No table {table!r} in {environment!r}/{NamespacePath.parse(namespace)}."
            )
        return environment_id, table_id

    def check_target(
        self,
        *,
        role: Role | None,
        action: Action | None,
        kind: SecurableKind,
        environment: str | None,
        namespace: str | None,
        table: str | None,
    ) -> tuple[uuid.UUID | None, uuid.UUID | None]:
        """Run every check `grant` runs on what is granted and where, writing nothing.

        `grant` calls it after authorizing; an access file plan (#46) calls it after its own
        authorization, so a planned grant is refused exactly as its single route would be.

        Parameters
        ----------
        role
            A role template, or `None` when `action` is given.
        action
            A raw action, or `None` when `role` is given.
        kind
            Which tier of the securable hierarchy the grant applies to.
        environment
            The environment, or `None` for the instance.
        namespace
            The namespace in its joined storage form, or `None`.
        table
            The table, or `None`.

        Returns
        -------
        tuple of (uuid.UUID or None, uuid.UUID or None)
            `(environment_id, table_id)`, as `_resolve_securable_ids` gives them.

        Raises
        ------
        ValidationError
            If not exactly one of `role`/`action` is given, `action` comes only with the
            `ADMIN` role, `READ_QUERY_TEXT` is not on the instance, or the fields do not match
            `kind`.
        NotFoundError
            If the environment, namespace or table does not exist.
        """
        if (role is None) == (action is None):
            raise ValidationError("Exactly one of 'role' or 'action' must be given.")
        if action in ADMIN_ONLY_ACTIONS:
            raise ValidationError(
                f"{action.value} comes only with the ADMIN role on the instance; grant that "
                "role instead, or nothing."
            )
        if action is Action.READ_QUERY_TEXT and kind is not SecurableKind.INSTANCE:
            raise ValidationError(
                "READ_QUERY_TEXT is granted on the instance only, like the audit log it opens: "
                "lakana grant create --kind INSTANCE --principal NAME --action READ_QUERY_TEXT"
            )
        _validate_securable_fields(kind, environment, namespace, table)
        return self._resolve_securable_ids(kind, environment, namespace, table)

    def grant(
        self,
        p: PrincipalRef,
        *,
        principal: str | None = None,
        group: str | None = None,
        role: Role | None = None,
        action: Action | None = None,
        kind: SecurableKind,
        environment: str | None = None,
        namespace: str | None = None,
        table: str | None = None,
        commit: bool = True,
    ) -> Grant:
        """Create a grant.

        Parameters
        ----------
        p
            The acting principal.
        principal
            Name of the principal to grant to. Exactly one of `principal`/
            `group` must be given.
        group
            Name of the group to grant to.
        role
            A role template to grant. Exactly one of `role`/`action` must
            be given.
        action
            A raw action to grant.
        kind
            Which tier of the securable hierarchy this grant applies to.
        environment
            The environment, required for an ENVIRONMENT/NAMESPACE/
            TABLE-scoped grant, `None` otherwise.
        namespace
            The namespace, in its unit-separator-joined storage form,
            required for a NAMESPACE/TABLE-scoped grant, `None` otherwise.
        table
            The table name, required for a TABLE-scoped grant, `None`
            otherwise.
        commit
            If `False`, add the row and its audit event to the session
            without committing — for a caller (`lakana_core.bootstrap`)
            that folds this grant into a larger, single-commit
            transaction. Every other caller keeps the default, one grant
            per commit.

        Returns
        -------
        Grant
            The created row, added to the session — and committed, unless
            `commit` is `False`.

        Raises
        ------
        ValidationError
            If subject/grant/securable fields don't satisfy the
            exactly-one-of rules, `action` is `MANAGE_GRANT`,
            `MANAGE_GROUP` or `MANAGE_TOKEN`, which come only with the
            `ADMIN` role, or `action` is `READ_QUERY_TEXT` on anything but
            the instance.
        NotFoundError
            If `principal`/`group` names a principal or group that doesn't
            exist, or `kind` names an environment, namespace, or table that
            doesn't exist.
        PermissionDenied
            If the acting principal does not hold the `ADMIN` role on the instance
            (`MANAGE_GRANT` works only with it).
        """
        authorize_admin_only(self._session, self._authz, p, Action.MANAGE_GRANT)
        if (principal is None) == (group is None):
            raise ValidationError("Exactly one of 'principal' or 'group' must be given.")
        environment_id, table_id = self.check_target(
            role=role,
            action=action,
            kind=kind,
            environment=environment,
            namespace=namespace,
            table=table,
        )
        principal_id = self._get_principal(principal).id if principal is not None else None
        group_id = self._get_group(group).id if group is not None else None
        row = Grant(
            id=uuid.uuid4(),
            principal_id=principal_id,
            group_id=group_id,
            role=role,
            action=action,
            kind=kind,
            environment=environment,
            namespace=namespace,
            table=table,
            environment_id=environment_id,
            table_id=table_id,
            granted_by=p.id,
            granted_at=datetime.now(UTC),
        )
        AuditService(self._session).record(
            p.id,
            AuditEventType.GRANT_CREATED,
            securable=Securable(
                kind=kind,
                environment=environment,
                namespace=NamespacePath.parse(namespace) if namespace is not None else None,
                table=table,
            ),
            detail={
                "grant_id": str(row.id),
                "principal": principal,
                "group": group,
                "role": role.value if role is not None else None,
                "action": action.value if action is not None else None,
            },
        )
        self._session.add(row)
        if commit:
            self._session.commit()
        return row

    def list_grants(
        self,
        p: PrincipalRef,
        *,
        principal_name: str | None = None,
        group_name: str | None = None,
        action: Action | None = None,
        scope: Securable | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> Page[Grant]:
        """List active grants, newest first, one page at a time; every filter is optional.

        Literal rows, not expanded: a principal's filter does not add its groups' grants, and a
        scope's filter does not add grants on an ancestor of it — `explain` answers what a
        principal may do. Filters combine with AND.

        Parameters
        ----------
        p
            The acting principal.
        principal_name
            Only grants made to this principal.
        group_name
            Only grants made to this group.
        action
            Only grants that give this action: the action itself, or a role that carries it.
        scope
            Only grants on exactly this object.
        limit
            Most grants returned.
        cursor
            The `next_cursor` of the previous page. Send the same filters with every page.

        Returns
        -------
        Page of Grant
            The grants by time granted, newest first, equal times by id, and the cursor of the
            next page.

        Raises
        ------
        ValidationError
            If both `principal_name` and `group_name` are given, `limit` is below 1, or
            `cursor` is not one this server issued for the grants list.
        NotFoundError
            If no principal has `principal_name`, or no group has `group_name`.
        PermissionDenied
            If the acting principal does not hold the `ADMIN` role on the instance
            (`MANAGE_GRANT` works only with it).
        """
        authorize_admin_only(self._session, self._authz, p, Action.MANAGE_GRANT)
        if principal_name is not None and group_name is not None:
            raise ValidationError(
                "Pass at most one of `principal_name` and `group_name`: a grant is made to a "
                "principal or to a group, never to both."
            )
        query = select(Grant).where(Grant.revoked_at.is_(None))
        if principal_name is not None:
            query = query.where(Grant.principal_id == self._get_principal(principal_name).id)
        if group_name is not None:
            query = query.where(Grant.group_id == self._get_group(group_name).id)
        if action is not None:
            roles = [role for role, actions in ROLE_ACTIONS.items() if action in actions]
            query = query.where(or_(Grant.action == action, Grant.role.in_(roles)))
        if scope is not None:
            query = query.where(
                Grant.kind == scope.kind,
                Grant.environment == scope.environment,
                Grant.namespace
                == (scope.namespace.joined() if scope.namespace is not None else None),
                Grant.table == scope.table,
            )
        return newest_first(
            self._session,
            query,
            kind="grants",
            at=Grant.granted_at,
            row_id=Grant.id,
            at_of=lambda grant: grant.granted_at,
            limit=limit,
            cursor=cursor,
        )

    def explain(self, p: PrincipalRef, principal_name: str, scope: Securable) -> PermissionReport:
        """Say what a principal may do on an object, and through which grant or group.

        Parameters
        ----------
        p
            The acting principal. It may ask about itself with no right at all (not through a
            task token); about anyone else it needs what listing grants needs.
        principal_name
            The principal asked about.
        scope
            The object.

        Returns
        -------
        PermissionReport
            The answer of the authorizer the routes use, action by action.

        Raises
        ------
        PermissionDenied
            If `p` asks about another principal, or through a task or CI token, without the `ADMIN`
            role on the instance; before anything is looked up, so the refusal says nothing
            about whether the principal exists.
        NotFoundError
            If an administrator asks about a principal, environment, namespace or table that
            does not exist. Asking about oneself, an object that does not exist is answered
            like one no grant names, so the answer never tells whether it exists.
        """
        own = p.task_scope is None and p.ci_scope is None and principal_name == p.name
        if not own:
            authorize_admin_only(self._session, self._authz, p, Action.MANAGE_GRANT)
        target = self._get_principal(principal_name)
        if not own:
            self._resolve_securable_ids(
                scope.kind,
                scope.environment,
                scope.namespace.joined() if scope.namespace is not None else None,
                scope.table,
            )
        return explain_permissions(self._session, target, scope)

    def revoke(self, p: PrincipalRef, grant_id: uuid.UUID, *, commit: bool = True) -> None:
        """Revoke a grant.

        Idempotent: revoking an already-revoked grant is a no-op — it
        leaves the existing `revoked_at`/`revoked_by` untouched and records
        no second `GRANT_REVOKED` event, matching PATs, task tokens, row
        filters, and column masks.

        Parameters
        ----------
        p
            The acting principal.
        grant_id
            The grant's id.
        commit
            If false, leave the change and its audit event in the session for the caller's commit.

        Raises
        ------
        NotFoundError
            If no grant has that id.
        ConflictError
            If revoking this grant would leave zero active instance admins.
        PermissionDenied
            If the acting principal does not hold the `ADMIN` role on the instance
            (`MANAGE_GRANT` works only with it).
        """
        authorize_admin_only(self._session, self._authz, p, Action.MANAGE_GRANT)
        grant = self._session.get(Grant, grant_id)
        if grant is None:
            raise NotFoundError(f"No grant {grant_id}.")
        if grant.revoked_at is not None:
            return
        if grant.role == Role.ADMIN and grant.kind == SecurableKind.INSTANCE:
            assert_admin_headroom(self._session, excluding_grant_id=grant.id)
        AuditService(self._session).record(
            p.id,
            AuditEventType.GRANT_REVOKED,
            securable=grant_scope(grant),
            detail={
                "grant_id": str(grant.id),
                "principal_id": str(grant.principal_id) if grant.principal_id is not None else None,
                "group_id": str(grant.group_id) if grant.group_id is not None else None,
                "role": grant.role.value if grant.role is not None else None,
                "action": grant.action.value if grant.action is not None else None,
            },
        )
        grant.revoked_at = datetime.now(UTC)
        grant.revoked_by = p.id
        if commit:
            self._session.commit()


def active_instance_admin_ids(
    session: Session,
    *,
    exclude_grant_id: uuid.UUID | None = None,
) -> set[uuid.UUID]:
    """Collect the active principals holding an active instance ADMIN grant.

    Shared by `assert_admin_headroom` below (which calls this once with no
    exclusions to establish the current baseline, and once more with the
    exclusion under consideration) and `lakana_core.bootstrap.admin_exists`
    (which calls this with no exclusions and only checks the result is
    nonempty).

    Parameters
    ----------
    session
        Database session.
    exclude_grant_id
        A grant to treat as already revoked regardless of its stored
        `revoked_at`, for a revocation that hasn't been committed yet.

    Returns
    -------
    set of uuid.UUID
        Every principal, covered directly or through a group by some
        active, unrevoked, instance-scoped ADMIN grant not excluded
        above, whose own `Principal.is_active` is true.

    Notes
    -----
    `lakana_core.grants.admin.holds_instance_admin` asks the same question for
    one principal; a test asserts the two agree, so change them together.
    """
    admin_grants = (
        session.execute(
            select(Grant).where(
                Grant.role == Role.ADMIN,
                Grant.kind == SecurableKind.INSTANCE,
                Grant.revoked_at.is_(None),
            )
        )
        .scalars()
        .all()
    )
    ids: set[uuid.UUID] = set()
    for admin_grant in admin_grants:
        if admin_grant.id == exclude_grant_id:
            continue
        if admin_grant.principal_id is not None:
            ids.add(admin_grant.principal_id)
        else:
            member_ids = (
                session.execute(
                    select(GroupMembership.principal_id).where(
                        GroupMembership.group_id == admin_grant.group_id
                    )
                )
                .scalars()
                .all()
            )
            ids.update(member_ids)
    if not ids:
        return set()
    return set(
        session.execute(
            select(Principal.id).where(Principal.id.in_(ids), Principal.is_active.is_(True))
        )
        .scalars()
        .all()
    )


def assert_admin_headroom(
    session: Session,
    *,
    excluding_principal_id: uuid.UUID | None = None,
    excluding_grant_id: uuid.UUID | None = None,
) -> None:
    """Refuse if the given exclusion would leave zero active instance admins.

    Shared by `GrantService.revoke` (excluding the grant being revoked) and
    `lakana_core.identity.principals.PrincipalService.deactivate_principal`/
    `lakana_core.identity.groups.GroupService.remove_member` (excluding the
    principal being deactivated or losing membership) — all reduce to the
    same question: after this change, does at least one active principal
    still hold an active `admin` grant at instance scope, directly or
    through a group? `GroupService.delete_group` does not call this: it
    refuses outright while the group holds any active grant at all
    , so it can never reach a group that still holds
    a live admin grant for this to protect against.

    Parameters
    ----------
    session
        Database session.
    excluding_principal_id
        A principal to treat as inactive regardless of its stored
        `is_active`, for a deactivation or group-membership removal that
        hasn't been committed yet.
    excluding_grant_id
        A grant to treat as already revoked regardless of its stored
        `revoked_at`, for a revocation that hasn't been committed yet.

    Raises
    ------
    ConflictError
        If no active principal would hold an active instance-scoped
        `admin` grant after the exclusion.
    """
    # Only refuse when *this* exclusion is what would empty the set. An
    # instance that already has zero active admins (for example, before any
    # admin grant has ever been made) is a pre-existing condition this guard
    # does not need to protect — and must not block unrelated principal
    # changes over, or every deactivate/revoke call would fail forever.
    if not active_instance_admin_ids(session):
        return
    remaining_ids = active_instance_admin_ids(session, exclude_grant_id=excluding_grant_id)
    remaining_ids.discard(excluding_principal_id)
    if not remaining_ids:
        raise ConflictError("Cannot leave the instance with zero active admins.")
