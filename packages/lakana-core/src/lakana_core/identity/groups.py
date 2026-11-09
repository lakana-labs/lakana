"""Group management: create, delete, list, and membership."""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from lakana_core.access.managed import guard_change
from lakana_core.audit.service import AuditService
from lakana_core.auth import Action, AuthorizationService, PrincipalRef, SecurableKind
from lakana_core.errors import AlreadyExistsError, ConflictError, NotFoundError
from lakana_core.grants.admin import authorize_admin_only
from lakana_core.grants.roles import Role
from lakana_core.grants.service import (
    assert_admin_headroom,
    mark_admin_holders,
    withdraw_links_of_lapsed_issuers,
)
from lakana_core.identity.sessions import require_step_up
from lakana_core.models import (
    AuditEventType,
    ColumnMask,
    Grant,
    Group,
    GroupMembership,
    Principal,
    RowFilter,
)
from lakana_core.pagination import Page, name_contains, newest_first


class GroupService:
    """Create, delete, and manage the membership of groups.

    Parameters
    ----------
    session
        Database session. Each method commits internally on success.
    authz
        Authorization service consulted before every mutating operation.
    """

    def __init__(self, session: Session, authz: AuthorizationService) -> None:
        self._session = session
        self._authz = authz

    def _get_group(self, name: str) -> Group:
        """Look up a group by name, without authorizing.

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

    def _get_member_principal(self, name: str) -> Principal:
        """Look up a principal by name, without authorizing.

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

    def create_group(self, p: PrincipalRef, *, name: str, commit: bool = True) -> Group:
        """Create a new group.

        Parameters
        ----------
        p
            The acting principal.
        name
            Unique name for the new group.
        commit
            If false, leave the change and its audit event in the session for the caller's commit.

        Returns
        -------
        Group
            The created row.

        Raises
        ------
        AlreadyExistsError
            If a group with that name already exists.
        PermissionDenied
            If the acting principal does not hold the `ADMIN` role on the instance
            (`MANAGE_GROUP` works only with it).
        """
        authorize_admin_only(self._session, self._authz, p, Action.MANAGE_GROUP)
        guard_change(self._session, p, environment=None, change=f"create group {name}")
        existing = self._session.execute(
            select(Group).where(Group.name == name)
        ).scalar_one_or_none()
        if existing is not None:
            raise AlreadyExistsError(f"Group {name!r} already exists.")
        group = Group(id=uuid.uuid4(), name=name)
        self._session.add(group)
        AuditService(self._session).record(
            p.id, AuditEventType.GROUP_CREATED, detail={"group": name}
        )
        if commit:
            self._session.commit()
        return group

    def delete_group(self, p: PrincipalRef, name: str, *, commit: bool = True) -> None:
        """Delete a group, along with its membership rows.

        Refused while any active grant, row filter, or column mask still
        references the group: `Grant.group_id`,
        `RowFilter.group_id`, and `ColumnMask.group_id` all cascade on
        delete, so deleting a group with a live grant or policy would
        otherwise silently remove it with no audit trail — a member who
        also holds a direct grant on the same table would then read it
        unfiltered and unmasked. The caller must revoke each one first
        (`GrantService.revoke`/`RowFilterService.revoke`/
        `ColumnMaskService.revoke`). A revoked reference is fine — its
        history lives in the audit log, not in a live row this delete would
        need to protect.

        Once permitted, records `GROUP_MEMBER_REMOVED` for every membership
        the delete cascades away, then `GROUP_DELETED`, all in the same
        transaction as the delete itself.

        The group row is locked (`SELECT ... FOR UPDATE`) before the active
        references are counted, the same pattern `CatalogService.
        create_external_table` uses around `check_claim_free` —
        without it, a grant or policy created on
        this group between the count and the eventual delete would be
        counted as zero and still cascade away uncounted and unaudited.
        SQLite has no row-level locking and treats this as a no-op, same as
        `create_external_table`'s own lock.

        Parameters
        ----------
        p
            The acting principal.
        name
            Name of the group to delete.
        commit
            If false, leave the change and its audit event in the session for the caller's commit.

        Raises
        ------
        NotFoundError
            If no group has that name.
        ConflictError
            If any active grant, row filter, or column mask still
            references the group. The message names the counts, never the
            individual rows.
        PermissionDenied
            If the acting principal does not hold the `ADMIN` role on the instance
            (`MANAGE_GROUP` works only with it).
        StepUpRequired
            In a browser session whose last passkey touch is older than 15 minutes.
        """
        # jscpd:ignore-start -- every public method calls the guard itself, then step-up
        # (test_admin_only_call_sites.py enumerates the calls), so this pair repeats by design.
        authorize_admin_only(self._session, self._authz, p, Action.MANAGE_GROUP)
        require_step_up(p)
        # jscpd:ignore-end
        group = self._get_group(name)
        guard_change(self._session, p, environment=None, change=f"delete group {name}")
        self._session.execute(
            select(Group).where(Group.id == group.id).with_for_update()
        ).scalar_one()
        active_grants = self._session.execute(
            select(func.count())
            .select_from(Grant)
            .where(Grant.group_id == group.id, Grant.revoked_at.is_(None))
        ).scalar_one()
        active_filters = self._session.execute(
            select(func.count())
            .select_from(RowFilter)
            .where(RowFilter.group_id == group.id, RowFilter.revoked_at.is_(None))
        ).scalar_one()
        active_masks = self._session.execute(
            select(func.count())
            .select_from(ColumnMask)
            .where(ColumnMask.group_id == group.id, ColumnMask.revoked_at.is_(None))
        ).scalar_one()
        if active_grants or active_filters or active_masks:
            raise ConflictError(
                f"Cannot delete group {name!r}: {active_grants} active grant(s), "
                f"{active_filters} active row filter(s), and {active_masks} active "
                "column mask(s) still reference it. Revoke them first."
            )
        memberships = (
            self._session.execute(
                select(GroupMembership).where(GroupMembership.group_id == group.id)
            )
            .scalars()
            .all()
        )
        audit = AuditService(self._session)
        for membership in memberships:
            member = self._session.get(Principal, membership.principal_id)
            assert member is not None  # nosec B101 -- FK guarantees the row still exists
            audit.record(
                p.id,
                AuditEventType.GROUP_MEMBER_REMOVED,
                detail={"group": name, "principal": member.name},
            )
        audit.record(p.id, AuditEventType.GROUP_DELETED, detail={"group": name})
        self._session.delete(group)
        if commit:
            self._session.commit()

    def list_groups(
        self,
        p: PrincipalRef,
        *,
        q: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> Page[Group]:
        """List groups, newest first, one page at a time.

        Parameters
        ----------
        p
            The acting principal.
        q
            Only groups whose name contains this text, in any letter case; ``%`` and ``_``
            are matched as themselves. Blank means every group.
        limit
            Most groups returned.
        cursor
            The `next_cursor` of the previous page. Send the same `q` with every page.

        Returns
        -------
        Page of Group
            The groups by creation time, newest first, equal times by id, and the cursor of
            the next page.

        Raises
        ------
        PermissionDenied
            If the acting principal does not hold the `ADMIN` role on the instance
            (`MANAGE_GROUP` works only with it).
        ValidationError
            If `limit` is below 1 or `cursor` is not one this server issued for the groups
            list.
        """
        authorize_admin_only(self._session, self._authz, p, Action.MANAGE_GROUP)
        query = select(Group)
        if q is not None and q.strip():
            query = query.where(name_contains(Group.name, q.strip()))
        return newest_first(
            self._session,
            query,
            kind="groups",
            at=Group.created_at,
            row_id=Group.id,
            at_of=lambda row: row.created_at,
            limit=limit,
            cursor=cursor,
        )

    def add_member(
        self, p: PrincipalRef, group_name: str, principal_name: str, *, commit: bool = True
    ) -> None:
        """Add a principal to a group, idempotently.

        Matches the router's `PUT` semantics: adding an existing member is
        not an error.

        Parameters
        ----------
        p
            The acting principal.
        group_name
            The group to add to.
        principal_name
            The principal to add.
        commit
            If false, leave the change and its audit event in the session for the caller's commit.

        Raises
        ------
        NotFoundError
            If the group or the principal does not exist.
        PermissionDenied
            If the acting principal does not hold the `ADMIN` role on the instance
            (`MANAGE_GROUP` works only with it).
        StepUpRequired
            In a browser session whose last passkey touch is older than 15 minutes.
        """
        # jscpd:ignore-start -- every public method calls the guard itself, then step-up
        # (test_admin_only_call_sites.py enumerates the calls), so this pair repeats by design.
        authorize_admin_only(self._session, self._authz, p, Action.MANAGE_GROUP)
        require_step_up(p)
        # jscpd:ignore-end
        group = self._get_group(group_name)
        principal = self._get_member_principal(principal_name)
        existing = self._session.execute(
            select(GroupMembership).where(
                GroupMembership.group_id == group.id,
                GroupMembership.principal_id == principal.id,
            )
        ).scalar_one_or_none()
        if existing is not None:
            return
        guard_change(
            self._session, p, environment=None, change=f"add {principal_name} to {group_name}"
        )
        self._session.add(GroupMembership(group_id=group.id, principal_id=principal.id))
        mark_admin_holders(self._session)
        AuditService(self._session).record(
            p.id,
            AuditEventType.GROUP_MEMBER_ADDED,
            detail={"group": group_name, "principal": principal_name},
        )
        if commit:
            self._session.commit()

    def remove_member(
        self, p: PrincipalRef, group_name: str, principal_name: str, *, commit: bool = True
    ) -> None:
        """Remove a principal from a group.

        Leaving a group that holds instance ADMIN withdraws the person's open invites and
        recovery links if that was their last way to ADMIN
        (`lakana_core.grants.service.withdraw_links_of_lapsed_issuers`).

        Parameters
        ----------
        p
            The acting principal.
        group_name
            The group to remove from.
        principal_name
            The principal to remove.
        commit
            If false, leave the change and its audit event in the session for the caller's commit.

        Raises
        ------
        NotFoundError
            If the group or the principal does not exist, or the principal
            is not a member of the group.
        ConflictError
            If removing this principal would leave zero active instance
            admins.
        PermissionDenied
            If the acting principal does not hold the `ADMIN` role on the instance
            (`MANAGE_GROUP` works only with it).
        StepUpRequired
            In a browser session whose last passkey touch is older than 15 minutes.
        """
        # jscpd:ignore-start -- every public method calls the guard itself, then step-up
        # (test_admin_only_call_sites.py enumerates the calls), so this pair repeats by design.
        authorize_admin_only(self._session, self._authz, p, Action.MANAGE_GROUP)
        require_step_up(p)
        # jscpd:ignore-end
        group = self._get_group(group_name)
        principal = self._get_member_principal(principal_name)
        membership = self._session.execute(
            select(GroupMembership).where(
                GroupMembership.group_id == group.id,
                GroupMembership.principal_id == principal.id,
            )
        ).scalar_one_or_none()
        if membership is None:
            raise NotFoundError(f"{principal_name!r} is not a member of {group_name!r}.")
        guard_change(
            self._session, p, environment=None, change=f"remove {principal_name} from {group_name}"
        )
        admin_group = self._grants_instance_admin(group.id)
        if admin_group:
            assert_admin_headroom(self._session, excluding_membership=(group.id, principal.id))
        self._session.delete(membership)
        AuditService(self._session).record(
            p.id,
            AuditEventType.GROUP_MEMBER_REMOVED,
            detail={"group": group_name, "principal": principal_name},
        )
        if admin_group:
            withdraw_links_of_lapsed_issuers(self._session, actor_id=p.id)
        if commit:
            self._session.commit()

    def _grants_instance_admin(self, group_id: uuid.UUID) -> bool:
        """Say whether a group holds an active instance ADMIN grant.

        Leaving a group without one cannot take the role from anyone, so the lockout guard is
        not asked; an access apply revokes an admin group's grant before its memberships.

        Parameters
        ----------
        group_id
            The group.

        Returns
        -------
        bool
            True with an unrevoked ADMIN grant of kind INSTANCE to the group.
        """
        found = select(Grant.id).where(
            Grant.group_id == group_id,
            Grant.role == Role.ADMIN,
            Grant.kind == SecurableKind.INSTANCE,
            Grant.revoked_at.is_(None),
        )
        return self._session.execute(found.limit(1)).first() is not None

    def list_members(self, p: PrincipalRef, group_name: str) -> list[Principal]:
        """List every principal in a group.

        Parameters
        ----------
        p
            The acting principal.
        group_name
            The group to list members of.

        Returns
        -------
        list of Principal
            Every member of the group, ordered by name.

        Raises
        ------
        NotFoundError
            If no group has that name.
        PermissionDenied
            If the acting principal does not hold the `ADMIN` role on the instance
            (`MANAGE_GROUP` works only with it).
        """
        authorize_admin_only(self._session, self._authz, p, Action.MANAGE_GROUP)
        group = self._get_group(group_name)
        return list(
            self._session.execute(
                select(Principal)
                .join(GroupMembership, GroupMembership.principal_id == Principal.id)
                .where(GroupMembership.group_id == group.id)
                .order_by(Principal.name)
            )
            .scalars()
            .all()
        )
