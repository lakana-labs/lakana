"""Linking a person to the organisation's identity provider, and undoing it (#32, PR 2).

The sign-in design's "Linking" and "Break-glass":

- Only a person Lakana knows is linked, by the provider's ``iss`` and ``sub``; nobody is created
  by the provider's say-so, and e-mail addresses and names are shown, never matched.
- One identity per account and one account per identity: the unique constraint on
  ``(oidc_issuer, oidc_subject)`` decides between two links at once, and the loser is refused
  ``identity_taken``, in a sentence that names neither account.
- The server operator's account (the bootstrap principal) and service accounts are never linked.
- The last administrator who can sign in with a passkey is never linked
  (`lakana_core.grants.break_glass`); the count is taken under the administrator-count lock
  (`lakana_core.grants.admin_lock.lock_admin_count`), so two links of the last two take turns
  and the second sees the first's commit.
- Linking keeps the person's passkeys stored (they no longer sign in) and ends no session.
- An administrator's unlink (`OidcLinkService.unlink`, step-up) deletes the person's passkeys,
  revokes their personal tokens and ends their sessions: they come back with a recovery link.
- The operator's recovery link of a linked administrator unlinks them
  (`OidcLinkService.unlink_for_operator_recovery`) and tells every administrator.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from lakana_core.audit.service import AuditService
from lakana_core.auth import Action, AuthorizationService, PrincipalRef, SessionContext
from lakana_core.errors import ConflictError, NotFoundError, OidcSignInRefused
from lakana_core.grants.admin import authorize_admin_only
from lakana_core.grants.admin_lock import lock_admin_count
from lakana_core.grants.break_glass import breaks, unlinked_admin_ids
from lakana_core.grants.service import active_instance_admin_ids
from lakana_core.identity.link_withdrawal import withdraw_open_links
from lakana_core.identity.notices import NoticeService
from lakana_core.identity.oidc_flows import ProviderIdentity, principal_for
from lakana_core.identity.sessions import (
    SessionService,
    principal_named,
    require_step_up,
    token_window_ends_at,
    utc_now,
)
from lakana_core.identity.task_tokens import revoke_task_tokens_of_pat
from lakana_core.models import (
    AuditEventType,
    Passkey,
    PersonalAccessToken,
    Principal,
    PrincipalType,
)

LinkVia = Literal["invite", "profile", "recovery"]
"""How a person came to link: an invite, their profile, or a recovery link."""

SESSION_ONLY = (
    "Linking to the identity provider and confirming at it happen in the web interface: they "
    "belong to a browser session, and a token has none. Sign in to the web interface and do it "
    "there."
)
"""The 409 detail of linking from the profile and of provider step-up, for a token."""


def in_browser_session(p: PrincipalRef) -> SessionContext:
    """Read the browser session a link from the profile or a provider step-up belongs to.

    Parameters
    ----------
    p
        The caller.

    Returns
    -------
    SessionContext
        The session.

    Raises
    ------
    ConflictError
        With `SESSION_ONLY`, for a token.
    """
    if p.session is None:
        raise ConflictError(SESSION_ONLY)
    return p.session


def locked_principal(session: Session, principal_id: uuid.UUID) -> Principal:
    """Read a principal afresh and lock its row until the transaction ends.

    A provider sign-in, a recovery through the provider and an unlink all take this lock before
    they decide whether the person is linked, so a sign-in racing an unlink either starts its
    session before the unlink ends every session, or sees the person unlinked (review of #780).

    Parameters
    ----------
    session
        Database session.
    principal_id
        The principal; it must exist.

    Returns
    -------
    Principal
        The row as committed now, never the session's cached copy.
    """
    return session.execute(
        select(Principal)
        .where(Principal.id == principal_id)
        # FOR NO KEY UPDATE: it orders these three against each other, and still lets other
        # transactions insert rows that refer to the person (an audit event, a passkey).
        .with_for_update(key_share=True)
        .execution_options(populate_existing=True)
    ).scalar_one()


def linked_principal_for_sign_in(session: Session, identity: ProviderIdentity) -> Principal | None:
    """Find the person linked to a verified identity and lock their row, for a sign-in.

    Parameters
    ----------
    session
        Database session; the caller starts the session in this transaction.
    identity
        The verified identity.

    Returns
    -------
    Principal or None
        The linked person, read afresh under the row lock, still linked to this identity;
        `None` when nobody is (also when an unlink committed meanwhile).
    """
    found = principal_for(session, identity)
    if found is None:
        return None
    locked = locked_principal(session, found.id)
    if (locked.oidc_issuer, locked.oidc_subject) != (identity.issuer, identity.subject):
        return None
    return locked


def is_linked(principal: Principal) -> bool:
    """Say whether a person signs in through the identity provider.

    Parameters
    ----------
    principal
        The person.

    Returns
    -------
    bool
        Whether a provider identity is stored on them.
    """
    return principal.oidc_subject is not None


def linked_count(session: Session) -> int:
    """Count the active people linked to the identity provider.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    int
        How many there are.
    """
    return int(
        session.execute(
            select(func.count())
            .select_from(Principal)
            .where(Principal.oidc_subject.is_not(None), Principal.is_active.is_(True))
        ).scalar_one()
    )


def unlinked_admin_count(session: Session) -> int:
    """Count the administrators who can sign in without the identity provider.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    int
        Active instance administrators, not linked, with a passkey that is not disabled.
    """
    return len(unlinked_admin_ids(session, active_instance_admin_ids(session)))


@dataclass(frozen=True)
class ProviderLink:
    """What an administrator, or the person, sees of a person's link to the identity provider.

    Parameters
    ----------
    issuer
        The provider's issuer.
    email
        The e-mail address of the last provider sign-in, for showing only.
    name
        The name of the last provider sign-in, for showing only.
    last_sign_in_at
        When the person last signed in there.
    token_window_ends_at
        Until when the person's personal tokens work without another provider sign-in.
    """

    issuer: str
    email: str | None
    name: str | None
    last_sign_in_at: datetime | None
    token_window_ends_at: datetime | None


def provider_link_of(session: Session, principal: Principal, *, days: int) -> ProviderLink | None:
    """Describe a person's link to the identity provider.

    Parameters
    ----------
    session
        Database session.
    principal
        The person.
    days
        The token window (`lakana_core.identity.sessions.token_window_days`).

    Returns
    -------
    ProviderLink or None
        The link; `None` for a person who is not linked.
    """
    if principal.oidc_issuer is None or not is_linked(principal):
        return None
    return ProviderLink(
        issuer=principal.oidc_issuer,
        email=principal.oidc_email,
        name=principal.oidc_name,
        last_sign_in_at=principal.last_provider_sign_in_at,
        token_window_ends_at=token_window_ends_at(session, principal, days=days),
    )


def linking_refusal(principal: Principal, *, bootstrap_name: str) -> OidcSignInRefused | None:
    """Say why a person cannot be linked at all, before anyone goes to the provider.

    Parameters
    ----------
    principal
        The person.
    bootstrap_name
        The bootstrap principal's name (`LAKANA_BOOTSTRAP_ADMIN`).

    Returns
    -------
    OidcSignInRefused or None
        ``cannot_link_operator`` for the bootstrap principal or a service account,
        ``already_linked`` for a linked person; `None` otherwise.
    """
    if principal.type is not PrincipalType.USER or principal.name == bootstrap_name:
        return OidcSignInRefused("cannot_link_operator")
    if is_linked(principal):
        return OidcSignInRefused("already_linked")
    return None


class OidcLinkService:
    """Link people to the identity provider and unlink them.

    Parameters
    ----------
    session
        Database session. `link` and `unlink_for_operator_recovery` leave their changes for the
        caller's commit; `unlink` commits.
    authz
        Authorization service, asked before an administrator unlinks someone.
    clock
        The clock; `utc_now` by default.
    bootstrap_name
        The bootstrap principal's name (`LAKANA_BOOTSTRAP_ADMIN`): never linked.
    """

    def __init__(
        self,
        session: Session,
        authz: AuthorizationService,
        *,
        clock: Callable[[], datetime] = utc_now,
        bootstrap_name: str,
    ) -> None:
        self._session = session
        self._authz = authz
        self._clock = clock
        self._bootstrap_name = bootstrap_name

    def link(
        self, principal_id: uuid.UUID, identity: ProviderIdentity, *, via: LinkVia
    ) -> Principal:
        """Link a person to a verified provider identity; the caller commits.

        Also records the provider sign-in that proved it (`last_provider_sign_in_at`, the shown
        e-mail address and name), which opens the person's token window.

        Parameters
        ----------
        principal_id
            The person.
        identity
            The identity the provider just verified.
        via
            ``invite``, ``profile`` or ``recovery``, for the audit event.

        Returns
        -------
        Principal
            The person, now linked.

        Raises
        ------
        NotFoundError
            If no principal has the id.
        OidcSignInRefused
            ``cannot_link_operator`` (the bootstrap principal, a service account),
            ``already_linked``, ``identity_taken`` (another account holds the identity) or
            ``last_unlinked_admin`` (the last administrator who signs in with a passkey).
        """
        principal = self._session.get(Principal, principal_id)
        if principal is None:
            raise NotFoundError("No such principal.")
        # The count must see every other link and removal that committed before it.
        lock_admin_count(self._session)
        refusal = self.refusal_for(principal)
        if refusal is not None:
            raise refusal
        holder = principal_for(self._session, identity)
        if holder is not None and holder.id != principal.id:
            raise OidcSignInRefused("identity_taken")
        try:
            with self._session.begin_nested():
                # Only while still unlinked: a link another finish committed meanwhile (this
                # session may have read the row before it) is never overwritten.
                linked = self._session.execute(
                    update(Principal)
                    .where(Principal.id == principal.id, Principal.oidc_subject.is_(None))
                    .values(
                        oidc_issuer=identity.issuer,
                        oidc_subject=identity.subject,
                        oidc_email=identity.email,
                        oidc_name=identity.name,
                        last_provider_sign_in_at=self._clock(),
                    )
                    .execution_options(synchronize_session=False)
                ).rowcount  # ty: ignore[missing-attribute]
        except IntegrityError:
            raise OidcSignInRefused("identity_taken") from None
        if linked != 1:
            raise OidcSignInRefused("already_linked")
        self._session.refresh(principal)
        AuditService(self._session).record(
            principal.id,
            AuditEventType.PRINCIPAL_OIDC_LINKED,
            detail={
                "principal": principal.name,
                "iss": identity.issuer,
                "sub": identity.subject,
                "via": via,
            },
        )
        return principal

    def refusal_for(self, principal: Principal) -> OidcSignInRefused | None:
        """Say why a person cannot be linked now, whatever identity the provider verifies.

        `link` asks under the administrator-count lock; the start of a profile link asks without
        it, so a person is told before they go to the provider.

        Parameters
        ----------
        principal
            The person.

        Returns
        -------
        OidcSignInRefused or None
            `linking_refusal`'s, or ``last_unlinked_admin`` for the last administrator who signs
            in with a passkey; `None` when they may be linked.
        """
        refusal = linking_refusal(principal, bootstrap_name=self._bootstrap_name)
        if refusal is not None:
            return refusal
        admins = active_instance_admin_ids(self._session)
        if principal.id in admins and breaks(
            self._session, before=admins, after=admins, linking=principal.id
        ):
            return OidcSignInRefused("last_unlinked_admin")
        return None

    def _clear(self, principal: Principal) -> tuple[str | None, str | None]:
        """Remove a person's provider identity; the caller audits and commits.

        Parameters
        ----------
        principal
            The person.

        Returns
        -------
        tuple of (str or None, str or None)
            The issuer and subject they were linked to.
        """
        before = (principal.oidc_issuer, principal.oidc_subject)
        principal.oidc_issuer = None
        principal.oidc_subject = None
        principal.oidc_email = None
        principal.oidc_name = None
        principal.last_provider_sign_in_at = None
        return before

    def unlink(self, p: PrincipalRef, name: str) -> None:
        """Unlink a person from the identity provider, as an administrator; commits.

        In one transaction: the person's passkeys are deleted, every personal API token of theirs
        is revoked (with the task tokens minted from it), every session of theirs ends
        (``oidc_unlinked``), every open sign-in link bound to them is withdrawn (an earlier
        recovery link would bind an identity again) and the provider identity is cleared. They
        come back with a new recovery link. To offboard someone, deactivate them instead.

        Parameters
        ----------
        p
            The acting administrator, with a fresh step-up in a browser session.
        name
            The person to unlink.

        Raises
        ------
        PermissionDenied
            If `p` is not an instance administrator (`MANAGE_TOKEN` works only with the ADMIN
            role).
        StepUpRequired
            Without a fresh step-up in a browser session.
        NotFoundError
            If no principal has the name.
        ConflictError
            For oneself (use a recovery link instead) and for a person who is not linked.
        """
        # Unlinking revokes the person's tokens: administering tokens, an administrator's alone.
        authorize_admin_only(self._session, self._authz, p, Action.MANAGE_TOKEN)
        require_step_up(p, now=self._clock())
        # The administrator lock first, then the person's row, then their links: the order of
        # a link's use (`lakana_core.grants.admin_lock`), so an unlink and a redeem take turns.
        lock_admin_count(self._session)
        target = locked_principal(self._session, principal_named(self._session, name).id)
        if target.id == p.id:
            raise ConflictError(
                "You cannot unlink yourself: you would lose every way in at once. Ask another "
                "administrator, or ask the server operator for a recovery link."
            )
        if not is_linked(target):
            raise ConflictError(
                f"{name!r} is not linked to the identity provider, so there is nothing to unlink."
            )
        passkeys = self._session.execute(
            delete(Passkey)
            .where(Passkey.principal_id == target.id)
            .returning(Passkey.id)
            .execution_options(synchronize_session="fetch")
        ).all()
        audit = AuditService(self._session)
        revoked = self._revoke_tokens(p, target, source="oidc_unlinked")
        ended = SessionService(self._session, self._authz, clock=self._clock).end_for_principal(
            target.id, "oidc_unlinked", commit=False
        )
        # An open recovery link would bind an identity again: they come back with a new one.
        links = withdraw_open_links(
            self._session, target.id, now=self._clock(), reason="oidc_unlinked"
        )
        issuer, subject = self._clear(target)
        audit.record(
            p.id,
            AuditEventType.PRINCIPAL_OIDC_UNLINKED,
            detail={
                "principal": target.name,
                "iss": issuer,
                "sub": subject,
                "reason": "administrator",
                "actor": p.name,
                "passkeys_deleted": len(passkeys),
                "tokens_revoked": revoked,
                "sessions_ended": ended,
                "links_withdrawn": links,
            },
        )
        self._session.commit()

    def _revoke_tokens(self, actor: PrincipalRef, target: Principal, *, source: str) -> int:
        """Revoke every personal token of a person being unlinked, audited; the caller commits.

        Each is one `PAT_REVOKED`, as `PatService.revoke` records it, with `source`; the task
        tokens minted from it end with it. Unlinked, a person's tokens are no longer under the
        provider window, so none may outlive the link.

        Parameters
        ----------
        actor
            Who unlinks: the administrator, or the server operator who issued the recovery link.
        target
            The person.
        source
            ``oidc_unlinked`` or ``operator_recovery``.

        Returns
        -------
        int
            How many tokens were revoked.
        """
        now = self._clock()
        audit = AuditService(self._session)
        tokens = list(
            self._session.execute(
                select(PersonalAccessToken)
                .where(
                    PersonalAccessToken.principal_id == target.id,
                    PersonalAccessToken.revoked_at.is_(None),
                )
                .order_by(PersonalAccessToken.created_at, PersonalAccessToken.id)
            ).scalars()
        )
        for token in tokens:
            token.revoked_at = now
            audit.record(
                actor.id,
                AuditEventType.PAT_REVOKED,
                detail={
                    "actor": actor.name,
                    "subject": target.name,
                    "key_id": token.key_id,
                    "source": source,
                },
            )
            revoke_task_tokens_of_pat(self._session, actor, token)
        return len(tokens)

    def unlink_for_operator_recovery(
        self, principal: Principal, link_id: uuid.UUID, *, operator: PrincipalRef
    ) -> None:
        """Unlink an administrator whose operator recovery link is being used; the caller commits.

        The break-glass path: it works whatever the provider says. The person's personal tokens
        are revoked, as an administrator's unlink revokes them, so that none outside the provider
        window works again once they are unlinked. Every administrator, and the person, gets the
        notice ``oidc_unlinked``.

        Parameters
        ----------
        principal
            The person recovered.
        link_id
            The recovery link.
        operator
            The server operator who issued the link, for the audit events of the tokens.
        """
        revoked = self._revoke_tokens(operator, principal, source="operator_recovery")
        issuer, subject = self._clear(principal)
        AuditService(self._session).record(
            principal.id,
            AuditEventType.PRINCIPAL_OIDC_UNLINKED,
            detail={
                "principal": principal.name,
                "iss": issuer,
                "sub": subject,
                "reason": "operator_recovery",
                "link_id": str(link_id),
                "tokens_revoked": revoked,
            },
        )
        notices = NoticeService(self._session, clock=self._clock)
        detail = {"name": principal.name, "link_id": str(link_id)}
        for recipient in sorted({principal.id, *active_instance_admin_ids(self._session)}, key=str):
            notices.add(recipient, "oidc_unlinked", principal.id, detail)
