"""Principal management: create, deactivate, look up, list."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.access.managed import guard_change, lock_access
from lakana_core.audit.service import ACCESS_APPLY_KEY, AuditService
from lakana_core.auth import Action, AuthorizationService, PrincipalRef, Securable
from lakana_core.errors import AlreadyExistsError, NotFoundError, PermissionDenied, ValidationError
from lakana_core.grants.service import assert_admin_headroom
from lakana_core.identity.links import (
    invisible_character_problem,
    name_length_problem,
    name_unavailable,
    withdraw_open_links,
)
from lakana_core.identity.sessions import SessionService, utc_now
from lakana_core.languages import canonical_language, language_problem
from lakana_core.models import AuditEventType, Principal, PrincipalType
from lakana_core.pagination import Page, name_contains, newest_first


class PrincipalService:
    """Create, deactivate, and look up principals.

    Parameters
    ----------
    session
        Database session. Each method commits internally on success, the
        same contract `CatalogService` uses.
    authz
        Authorization service consulted before every mutating operation.
    """

    def __init__(self, session: Session, authz: AuthorizationService) -> None:
        self._session = session
        self._authz = authz

    def create_principal(
        self, p: PrincipalRef, *, name: str, type: PrincipalType, commit: bool = True
    ) -> Principal:
        """Create a new principal.

        Parameters
        ----------
        p
            The acting principal.
        name
            Unique name for the new principal.
        type
            Whether this is a human user or a service account. Today,
            both authenticate identically, via PAT — `type` is a label for
            display and audit until M7 gives `USER` a second, distinct
            authentication path.
        commit
            If false, leave the change and its audit event in the session for the caller's commit.

        Returns
        -------
        Principal
            The created row, active by default.

        Raises
        ------
        AlreadyExistsError
            If a principal with that name already exists, one whose name differs only in
            letter case (active or deactivated), or an open invite reserves the name
            (`lakana_core.identity.links.name_unavailable`).
        ValidationError
            If the name or its normalised form is over 255 characters
            (`lakana_core.identity.links.name_length_problem`), or it holds a control or format
            character, or starts with a combining mark
            (`lakana_core.identity.links.invisible_character_problem`).
        PermissionDenied
            If the acting principal may not manage principals.
        """
        self._authz.authorize(p, Action.MANAGE_PRINCIPAL, Securable.instance_scope())
        existing = self._session.execute(
            select(Principal).where(Principal.name == name)
        ).scalar_one_or_none()
        if existing is not None:
            raise AlreadyExistsError(f"Principal {name!r} already exists.")
        problem = name_length_problem(name) or invisible_character_problem(name)
        if problem is not None:
            raise ValidationError(problem)
        problem = name_unavailable(self._session, name, now=utc_now())
        if problem is not None:
            raise AlreadyExistsError(problem)
        self._guard(p, type, f"create service account {name}")
        principal = Principal(id=uuid.uuid4(), type=type, name=name, is_active=True)
        self._session.add(principal)
        AuditService(self._session).record(
            p.id,
            AuditEventType.PRINCIPAL_CREATED,
            detail={"principal": name, "type": type.value},
        )
        if commit:
            self._session.commit()
        return principal

    def _guard(self, p: PrincipalRef, type: PrincipalType, change: str) -> None:
        """Pass a service account's change through the guard of the managed `instance` scope.

        People are never in an access file, so only service accounts are guarded, and an
        override asked for on a person is refused as a stale flag (as in a scope no file
        manages).

        Parameters
        ----------
        p
            The acting principal, already authorized.
        type
            The type of the principal changed.
        change
            What is changed, for a service account.

        Raises
        ------
        ValidationError
            If `p` asks for an override and the principal is a person.
        """
        if type is not PrincipalType.SERVICE_ACCOUNT:
            if p.managed_override_reason is not None:
                raise ValidationError(
                    "people are not managed by an access file; repeat without `--override-managed`"
                )
            return
        guard_change(
            self._session,
            p,
            environment=None,
            change=change,
            authz=self._authz,
            action=Action.MANAGE_PRINCIPAL,
        )

    def deactivate_principal(self, p: PrincipalRef, name: str, *, commit: bool = True) -> None:
        """Deactivate a principal, revoking its access without deleting it.

        Every open browser session of the principal ends in the same commit, with reason
        ``deactivated`` (`SessionService.end_for_principal`), and every open sign-in link bound
        to it is withdrawn (`lakana_core.identity.links.withdraw_open_links`).

        Idempotent: deactivating an already-inactive principal is a no-op
        — it records no second `PRINCIPAL_DEACTIVATED` event, and does not
        re-run the admin-headroom check (an already-inactive principal
        holds no headroom to lose).

        Parameters
        ----------
        p
            The acting principal.
        name
            Name of the principal to deactivate.
        commit
            If false, leave the change and its audit event in the session for the caller's commit.

        Raises
        ------
        NotFoundError
            If no principal has that name.
        ConflictError
            If deactivating this principal would leave zero active
            instance admins.
        PermissionDenied
            If the acting principal may not manage principals.
        """
        self._authz.authorize(p, Action.MANAGE_PRINCIPAL, Securable.instance_scope())
        principal = self.get_principal_by_name(p, name)
        if not principal.is_active:
            return
        self._guard(p, principal.type, f"deactivate service account {name}")
        if ACCESS_APPLY_KEY not in self._session.info:
            lock_access(self._session, shared=True)
        assert_admin_headroom(self._session, excluding_principal_id=principal.id)
        principal.is_active = False
        AuditService(self._session).record(
            p.id,
            AuditEventType.PRINCIPAL_DEACTIVATED,
            detail={"principal": name, "type": principal.type.value},
        )
        SessionService(self._session, self._authz).end_for_principal(
            principal.id, "deactivated", commit=False
        )
        withdraw_open_links(self._session, principal.id, now=utc_now(), reason="deactivated")
        if commit:
            self._session.commit()

    def get_principal_by_name(self, p: PrincipalRef, name: str) -> Principal:
        """Fetch a principal by name.

        Parameters
        ----------
        p
            The acting principal.
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
        PermissionDenied
            If the acting principal may not manage principals.
        """
        self._authz.authorize(p, Action.MANAGE_PRINCIPAL, Securable.instance_scope())
        principal = self._session.execute(
            select(Principal).where(Principal.name == name)
        ).scalar_one_or_none()
        if principal is None:
            raise NotFoundError(f"No principal named {name!r}.")
        return principal

    def list_principals(
        self,
        p: PrincipalRef,
        *,
        q: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> Page[Principal]:
        """List principals, newest first, one page at a time.

        Parameters
        ----------
        p
            The acting principal.
        q
            Only principals whose name contains this text, in any letter case; ``%`` and ``_``
            are matched as themselves. Blank means every principal.
        limit
            Most principals returned.
        cursor
            The `next_cursor` of the previous page. Send the same `q` with every page.

        Returns
        -------
        Page of Principal
            The principals by creation time, newest first, equal times by id, and the cursor of
            the next page.

        Raises
        ------
        PermissionDenied
            If the acting principal may not manage principals.
        ValidationError
            If `limit` is below 1 or `cursor` is not one this server issued for the principals
            list.
        """
        self._authz.authorize(p, Action.MANAGE_PRINCIPAL, Securable.instance_scope())
        query = select(Principal)
        if q is not None and q.strip():
            query = query.where(name_contains(Principal.name, q.strip()))
        return newest_first(
            self._session,
            query,
            kind="principals",
            at=Principal.created_at,
            row_id=Principal.id,
            at_of=lambda row: row.created_at,
            limit=limit,
            cursor=cursor,
        )

    def set_own_language(self, p: PrincipalRef, language: str | None) -> str | None:
        """Set or reset the caller's own preferred language for catalog texts.

        Not audited: a display preference of one's own, which changes nothing anyone else sees.

        Parameters
        ----------
        p
            The caller.
        language
            A tag, any case; `None` to reset.

        Returns
        -------
        str or None
            The stored canonical tag.

        Raises
        ------
        PermissionDenied
            For a caller signed in with a task or CI token.
        ValidationError
            For a tag outside the subset.
        NotFoundError
            If the caller's principal no longer exists.
        """
        if p.task_scope is not None or p.ci_scope is not None:
            raise PermissionDenied(
                "a task or CI token cannot change its principal's profile; "
                "use a personal access token"
            )
        canonical = None
        if language is not None:
            canonical = canonical_language(language.strip())
            if canonical is None:
                raise ValidationError(
                    f"language: {language_problem(language.strip())}; use the tag, for example `de`"
                )
        principal = self._session.get(Principal, p.id)
        if principal is None:
            raise NotFoundError("Your principal no longer exists.")
        principal.language = canonical
        self._session.commit()
        return canonical
