"""Make the changes of an access plan through the services the single routes use (#46).

No side channel (rule 2): every change is one call of the service method its own route calls,
with the same authorization and validation, and with ``commit=False`` so the caller's
`applying` block commits them all at once or none.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.access.checks import policy_table
from lakana_core.access.model import (
    CiTrustEntry,
    ColumnMaskEntry,
    GrantEntry,
    GroupEntry,
    MembershipEntry,
    RowFilterEntry,
    ServiceAccountEntry,
    Subject,
)
from lakana_core.access.plan import PlannedChange, apply_order
from lakana_core.auth import Action, AuthorizationService, PrincipalRef, SecurableKind
from lakana_core.ci_oidc.rules import CiTrustRuleService
from lakana_core.grants.roles import Role
from lakana_core.grants.service import GrantService
from lakana_core.identity.groups import GroupService
from lakana_core.identity.pats import PatService
from lakana_core.identity.principals import PrincipalService
from lakana_core.models import Grant, Group, GroupMembership, PrincipalType
from lakana_core.policy.column_masks import ColumnMaskService
from lakana_core.policy.columns import ColumnSource
from lakana_core.policy.row_filters import RowFilterService


def _subject(subject: Subject) -> dict[str, str]:
    """Give a subject as the grant and policy services take it.

    Parameters
    ----------
    subject
        ``user:``, ``service:`` or ``group:``.

    Returns
    -------
    dict
        ``{"group": name}`` for a group, else ``{"principal": name}``.
    """
    return {"group": subject.name} if subject.kind == "group" else {"principal": subject.name}


def _admin_groups(session: Session) -> set[str]:
    """Name the groups that hold the instance ADMIN role.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    set of str
        The groups' names.
    """
    found = (
        select(Group.name)
        .join(Grant, Grant.group_id == Group.id)
        .where(
            Grant.role == Role.ADMIN,
            Grant.kind == SecurableKind.INSTANCE,
            Grant.revoked_at.is_(None),
        )
    )
    return set(session.scalars(found))


def _groups_of(session: Session, p: PrincipalRef) -> set[str]:
    """Name the groups a principal is a member of.

    Parameters
    ----------
    session
        Database session.
    p
        The principal.

    Returns
    -------
    set of str
        The groups' names.
    """
    found = (
        select(Group.name)
        .join(GroupMembership, GroupMembership.group_id == Group.id)
        .where(GroupMembership.principal_id == p.id)
    )
    return set(session.scalars(found))


def callers_admin_last(
    changes: Sequence[PlannedChange],
    p: PrincipalRef,
    *,
    caller_groups: set[str],
    admin_groups: set[str],
) -> list[PlannedChange]:
    """Move the removals that end the caller's own instance ADMIN role to the end.

    Each change is authorized as it is made, against the access the apply has left so far. A
    caller who hands the role over (`allow_lockout`) must keep it until everything else is
    done; a direct grant goes last of all, since a group path may still need its membership
    removed.

    Parameters
    ----------
    changes
        The changes in `apply_order`.
    p
        The acting principal.
    caller_groups
        The groups the caller is a member of now.
    admin_groups
        The groups that hold the instance ADMIN role now.

    Returns
    -------
    list of PlannedChange
        The same changes; the caller's ADMIN removals moved to the end, order kept otherwise.
    """

    def rank(change: PlannedChange) -> int:
        """Say how late a change must run.

        Parameters
        ----------
        change
            Any change.

        Returns
        -------
        int
            2 for the caller's own ADMIN grant, 1 for a group path of it, else 0.
        """
        entry = change.entry
        if change.op != "remove":
            return 0
        if isinstance(entry, MembershipEntry):
            return int(entry.member == p.name and entry.group in admin_groups)
        if not isinstance(entry, GrantEntry) or entry.role != Role.ADMIN.value:
            return 0
        if entry.on.environment is not None:
            return 0
        if entry.to.kind == "group":
            return int(entry.to.name in caller_groups)
        return 2 if entry.to.name == p.name else 0

    return sorted(changes, key=rank)


class AccessApplier:
    """Make planned changes through the services the single routes use (no side channel).

    Parameters
    ----------
    session
        The request's session; the caller holds `applying`.
    authz
        The request's authorization service.
    catalog
        The column source for the policy services.
    issuers
        The configured CI OIDC issuers.
    """

    def __init__(
        self,
        session: Session,
        authz: AuthorizationService,
        catalog: ColumnSource,
        *,
        issuers: Sequence[str],
    ) -> None:
        self._session = session
        self._groups = GroupService(session, authz)
        self._principals = PrincipalService(session, authz)
        self._pats = PatService(session, authz)
        self._grants = GrantService(session, authz)
        self._row_filters = RowFilterService(session, authz, catalog)
        self._column_masks = ColumnMaskService(session, authz, catalog)
        self._ci = CiTrustRuleService(session, authz, issuers=issuers)

    def run(self, p: PrincipalRef, changes: Sequence[PlannedChange]) -> None:
        """Make each change, creates first, removals last in reverse dependency order.

        The removals that end the caller's own ADMIN role run after every other change
        (`callers_admin_last`).

        Parameters
        ----------
        p
            The acting principal.
        changes
            The plan's changes.
        """
        ordered = callers_admin_last(
            apply_order(changes),
            p,
            caller_groups=_groups_of(self._session, p),
            admin_groups=_admin_groups(self._session),
        )
        for change in ordered:
            getattr(self, f"_{change.op}_{type(change.entry).__name__}")(p, change)

    def _create_GroupEntry(self, p: PrincipalRef, change: PlannedChange) -> None:
        """Create a group.

        Parameters
        ----------
        p
            The acting principal.
        change
            The change; its entry is a `GroupEntry`.
        """
        assert isinstance(change.entry, GroupEntry)  # nosec B101 -- dispatch by type name
        self._groups.create_group(p, name=change.entry.name, commit=False)

    def _create_MembershipEntry(self, p: PrincipalRef, change: PlannedChange) -> None:
        """Add a member to a group.

        Parameters
        ----------
        p
            The acting principal.
        change
            The change; its entry is a `MembershipEntry`.
        """
        assert isinstance(change.entry, MembershipEntry)  # nosec B101 -- dispatch by type name
        self._groups.add_member(p, change.entry.group, change.entry.member, commit=False)

    def _create_ServiceAccountEntry(self, p: PrincipalRef, change: PlannedChange) -> None:
        """Create a service account.

        Parameters
        ----------
        p
            The acting principal.
        change
            The change; its entry is a `ServiceAccountEntry`.
        """
        assert isinstance(change.entry, ServiceAccountEntry)  # nosec B101 -- dispatch by type name
        self._principals.create_principal(
            p, name=change.entry.name, type=PrincipalType.SERVICE_ACCOUNT, commit=False
        )

    def _create_GrantEntry(self, p: PrincipalRef, change: PlannedChange) -> None:
        """Grant a role or an action.

        Parameters
        ----------
        p
            The acting principal.
        change
            The change; its entry is a `GrantEntry`.
        """
        entry = change.entry
        assert isinstance(entry, GrantEntry)  # nosec B101 -- dispatch by type name
        self._grants.grant(
            p,
            **_subject(entry.to),
            role=Role(entry.role) if entry.role else None,
            action=Action(entry.action) if entry.action else None,
            kind=entry.on.kind,
            environment=entry.on.environment,
            namespace=entry.on.namespace_joined,
            table=entry.on.table,
            commit=False,
        )

    def _create_RowFilterEntry(self, p: PrincipalRef, change: PlannedChange) -> None:
        """Create a row filter.

        Parameters
        ----------
        p
            The acting principal.
        change
            The change; its entry is a `RowFilterEntry`.
        """
        entry = change.entry
        assert isinstance(entry, RowFilterEntry)  # nosec B101 -- dispatch by type name
        self._row_filters.create(
            p,
            **_subject(entry.to),
            **policy_table(entry.table),
            expression=entry.expression,
            commit=False,
        )

    def _create_ColumnMaskEntry(self, p: PrincipalRef, change: PlannedChange) -> None:
        """Create a column mask.

        Parameters
        ----------
        p
            The acting principal.
        change
            The change; its entry is a `ColumnMaskEntry`.
        """
        entry = change.entry
        assert isinstance(entry, ColumnMaskEntry)  # nosec B101 -- dispatch by type name
        self._column_masks.create(
            p,
            **_subject(entry.to),
            **policy_table(entry.table),
            column=entry.column,
            expression=entry.expression,
            commit=False,
        )

    def _create_CiTrustEntry(self, p: PrincipalRef, change: PlannedChange) -> None:
        """Create a CI trust rule, disabled when the file says so.

        Parameters
        ----------
        p
            The acting principal.
        change
            The change; its entry is a `CiTrustEntry`.
        """
        entry = change.entry
        assert isinstance(entry, CiTrustEntry)  # nosec B101 -- dispatch by type name
        view = self._ci.create(
            p,
            issuer=entry.issuer,
            principal=entry.as_,
            environment=entry.on,
            repository=entry.repository,
            kinds=entry.kinds,
            conditions=dict(entry.conditions),
            start_on_deploy_jobs=entry.start_on_deploy_jobs,
            commit=False,
        )
        if not entry.enabled:
            self._ci.disable(p, view.id, commit=False)

    def _change_CiTrustEntry(self, p: PrincipalRef, change: PlannedChange) -> None:
        """Bring each copy of a CI trust rule to the file's settings.

        Only what differs is changed: an update ends every token issued under the rule, so a
        rule that is only enabled or disabled keeps them.

        Parameters
        ----------
        p
            The acting principal.
        change
            The change; its entry and `before` are `CiTrustEntry`.
        """
        entry, before = change.entry, change.before
        assert isinstance(entry, CiTrustEntry)  # nosec B101 -- dispatch by type name
        assert isinstance(before, CiTrustEntry)  # nosec B101 -- a change carries the live rule
        settings = (entry.kinds, entry.start_on_deploy_jobs)
        for rule_id in change.ids:
            if settings != (before.kinds, before.start_on_deploy_jobs):
                self._ci.update(
                    p,
                    rule_id,
                    kinds=entry.kinds,
                    start_on_deploy_jobs=entry.start_on_deploy_jobs,
                    commit=False,
                )
            if entry.enabled != before.enabled:
                toggle = self._ci.enable if entry.enabled else self._ci.disable
                toggle(p, rule_id, commit=False)

    def _remove_GroupEntry(self, p: PrincipalRef, change: PlannedChange) -> None:
        """Delete a group; what named it was removed before.

        Parameters
        ----------
        p
            The acting principal.
        change
            The change; its entry is a `GroupEntry`.
        """
        assert isinstance(change.entry, GroupEntry)  # nosec B101 -- dispatch by type name
        self._groups.delete_group(p, change.entry.name, commit=False)

    def _remove_MembershipEntry(self, p: PrincipalRef, change: PlannedChange) -> None:
        """Remove a member from a group.

        Parameters
        ----------
        p
            The acting principal.
        change
            The change; its entry is a `MembershipEntry`.
        """
        assert isinstance(change.entry, MembershipEntry)  # nosec B101 -- dispatch by type name
        self._groups.remove_member(p, change.entry.group, change.entry.member, commit=False)

    def _remove_ServiceAccountEntry(self, p: PrincipalRef, change: PlannedChange) -> None:
        """Revoke a service account's tokens, then deactivate it.

        Parameters
        ----------
        p
            The acting principal.
        change
            The change; its entry is a `ServiceAccountEntry`.
        """
        assert isinstance(change.entry, ServiceAccountEntry)  # nosec B101 -- dispatch by type name
        name = change.entry.name
        for pat in self._pats.list(p, name):
            if pat.revoked_at is None:
                self._pats.revoke(p, name, pat.key_id, commit=False)
        self._principals.deactivate_principal(p, name, commit=False)

    def _remove_GrantEntry(self, p: PrincipalRef, change: PlannedChange) -> None:
        """Revoke every copy of a grant.

        Parameters
        ----------
        p
            The acting principal.
        change
            The change; `ids` names the copies.
        """
        for grant_id in change.ids:
            self._grants.revoke(p, grant_id, commit=False)

    def _remove_RowFilterEntry(self, p: PrincipalRef, change: PlannedChange) -> None:
        """Revoke every copy of a row filter.

        Parameters
        ----------
        p
            The acting principal.
        change
            The change; `ids` names the copies.
        """
        for row_filter_id in change.ids:
            self._row_filters.revoke(p, row_filter_id, commit=False)

    def _remove_ColumnMaskEntry(self, p: PrincipalRef, change: PlannedChange) -> None:
        """Revoke every copy of a column mask.

        Parameters
        ----------
        p
            The acting principal.
        change
            The change; `ids` names the copies.
        """
        for column_mask_id in change.ids:
            self._column_masks.revoke(p, column_mask_id, commit=False)

    def _remove_CiTrustEntry(self, p: PrincipalRef, change: PlannedChange) -> None:
        """Delete every copy of a CI trust rule.

        Parameters
        ----------
        p
            The acting principal.
        change
            The change; `ids` names the copies.
        """
        for rule_id in change.ids:
            self._ci.delete(p, rule_id, commit=False)
