"""PAT issuance, listing, revocation, and verification."""

from __future__ import annotations

import builtins
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from lakana_core.audit.service import AuditService
from lakana_core.auth import Action, AuthorizationService, PrincipalRef
from lakana_core.errors import AuthenticationError, ConflictError, NotFoundError
from lakana_core.grants.admin import authorize_admin_only
from lakana_core.identity.tokens import generate_token, hash_secret, verify_secret
from lakana_core.models import AuditEventType, PersonalAccessToken, Principal


@dataclass(frozen=True)
class IssuedPat:
    """A freshly issued PAT, returned exactly once.

    Parameters
    ----------
    key_id
        The public lookup key, safe to display afterward.
    name
        The human label given at issuance.
    token
        The full `lakana_pat_<key_id>_<secret>` value. Never retrievable
        again after this call returns.
    created_at
        When the PAT was issued.
    """

    key_id: str
    name: str
    token: str
    created_at: datetime


class PatService:
    """Issue, list, revoke, and verify PATs.

    Parameters
    ----------
    session
        Database session. Each mutating method commits internally.
    authz
        Authorization service consulted before every issue/list/revoke
        call. `verify` does not consult it — verification is what
        *produces* an authenticated principal, before any authorization
        question can be asked.

    Notes
    -----
    Issuing, listing and revoking need the `ADMIN` role on the instance, whoever
    the token is for: there is no self-service for tokens (#32).
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

    def issue(
        self,
        p: PrincipalRef,
        principal_name: str,
        *,
        name: str,
        expires_at: datetime | None = None,
        key_id: str | None = None,
        secret: str | None = None,
        commit: bool = True,
    ) -> IssuedPat:
        """Issue a new PAT for a principal.

        Parameters
        ----------
        p
            The acting principal.
        principal_name
            The principal the PAT authenticates as.
        name
            A human label for this PAT (e.g. "laptop", "CI").
        expires_at
            When the PAT stops working, or `None` for no expiry.
        key_id
            A specific key_id to use instead of a freshly generated one.
            Exists only for `lakana_core.bootstrap.bootstrap_if_needed`'s
            deterministic-test-credential path — never exposed through the
            platform API, whose request schema has no such field. A
            collision on this exact, caller-supplied value is never
            retried with a different one (the caller asked for this
            specific credential); it raises `ConflictError` instead.
        secret
            A specific secret to use instead of a freshly generated one.
            Same restriction as `key_id`.
        commit
            If `False`, add the row and its audit event to the session
            without committing, and without this method's own
            collision-retry loop — for `lakana_core.bootstrap`'s recovery
            path, which folds this PAT into a larger, single-commit
            transaction and has already ruled out the one realistic
            collision (a configured `key_id` reused from an earlier boot)
            before calling this. Every other caller keeps the default,
            one PAT per commit, with the retry-once-on-random-collision
            behaviour described below.

        Returns
        -------
        IssuedPat
            The new PAT, including its full token value — shown here and
            nowhere else afterward.

        Raises
        ------
        NotFoundError
            If no principal has `principal_name`.
        ConflictError
            If `key_id`/`secret` were given explicitly and that exact
            `key_id` already exists, or a randomly generated `key_id`
            collides twice in a row (astronomically rare, but a
            server-side generation conflict, not an authentication
            failure — see the retry-once behaviour below).
        PermissionDenied
            If the acting principal does not hold the `ADMIN` role on the instance
            (`MANAGE_TOKEN` works only with it).
        sqlalchemy.exc.IntegrityError
            If the commit fails for any reason other than a key_id collision
            — e.g. the acting principal's audit foreign key.
        """
        authorize_admin_only(self._session, self._authz, p, Action.MANAGE_TOKEN)
        principal = self._get_principal(principal_name)
        fixed_credential = key_id is not None and secret is not None
        if fixed_credential:
            resolved_key_id = key_id
            secret_hash = hash_secret(secret)
            full_token = f"lakana_pat_{key_id}_{secret}"
        else:
            token = generate_token("lakana_pat")
            resolved_key_id = token.key_id
            secret_hash = token.secret_hash
            full_token = token.full_token
        now = datetime.now(UTC)
        retried = False
        while True:
            pat = PersonalAccessToken(
                id=uuid.uuid4(),
                principal_id=principal.id,
                key_id=resolved_key_id,
                secret_hash=secret_hash,
                name=name,
                created_at=now,
                expires_at=expires_at,
            )
            self._session.add(pat)
            AuditService(self._session).record(
                p.id,
                AuditEventType.PAT_ISSUED,
                detail={"issuer": p.name, "subject": principal_name, "key_id": resolved_key_id},
            )
            if not commit:
                break
            try:
                self._session.commit()
            except IntegrityError as exc:
                self._session.rollback()
                if not self._key_id_taken(resolved_key_id):
                    raise
                if fixed_credential:
                    # The caller asked for this exact key_id/secret (the
                    # bootstrap-only override path) — silently substituting
                    # a different, randomly generated value would not honor
                    # that request. Two server replicas racing to insert the
                    # same deterministic bootstrap credential land here.
                    raise ConflictError(
                        f"A PAT with key_id {resolved_key_id!r} already exists."
                    ) from exc
                if retried:
                    raise ConflictError(
                        "Generated a duplicate key_id twice in a row; giving up."
                    ) from exc
                retried = True
                token = generate_token("lakana_pat")
                resolved_key_id = token.key_id
                secret_hash = token.secret_hash
                full_token = token.full_token
                continue
            break
        return IssuedPat(key_id=resolved_key_id, name=name, token=full_token, created_at=now)

    def _key_id_taken(self, key_id: str) -> bool:
        """Whether a PAT with this key_id exists, i.e. an insert failure was a collision.

        Asked after a rolled-back `IntegrityError` instead of parsing the
        driver's message or constraint name, which differ between SQLite and
        Postgres.

        Parameters
        ----------
        key_id
            The key_id the failed insert used.

        Returns
        -------
        bool
            True if another PAT already holds it.
        """
        return (
            self._session.execute(
                select(PersonalAccessToken.id).where(PersonalAccessToken.key_id == key_id)
            ).first()
            is not None
        )

    def list(self, p: PrincipalRef, principal_name: str) -> builtins.list[PersonalAccessToken]:
        """List every PAT issued to a principal.

        Parameters
        ----------
        p
            The acting principal.
        principal_name
            The principal whose PATs to list.

        Returns
        -------
        list of PersonalAccessToken
            Every PAT row for that principal. Rows carry only `secret_hash`,
            never a retrievable secret.

        Raises
        ------
        NotFoundError
            If no principal has `principal_name`.
        PermissionDenied
            If the acting principal does not hold the `ADMIN` role on the instance
            (`MANAGE_TOKEN` works only with it).
        """
        authorize_admin_only(self._session, self._authz, p, Action.MANAGE_TOKEN)
        principal = self._get_principal(principal_name)
        return list(
            self._session.execute(
                select(PersonalAccessToken).where(PersonalAccessToken.principal_id == principal.id)
            )
            .scalars()
            .all()
        )

    def revoke(self, p: PrincipalRef, principal_name: str, key_id: str) -> None:
        """Revoke a PAT before its natural expiry.

        Idempotent: revoking an already-revoked PAT is a no-op — it leaves
        the existing `revoked_at` untouched and records no second
        `PAT_REVOKED` event.

        Parameters
        ----------
        p
            The acting principal.
        principal_name
            The principal the PAT belongs to.
        key_id
            The PAT's key_id.

        Raises
        ------
        NotFoundError
            If no principal has `principal_name`, or it has no PAT with
            that `key_id`.
        PermissionDenied
            If the acting principal does not hold the `ADMIN` role on the instance
            (`MANAGE_TOKEN` works only with it).
        """
        authorize_admin_only(self._session, self._authz, p, Action.MANAGE_TOKEN)
        principal = self._get_principal(principal_name)
        pat = self._session.execute(
            select(PersonalAccessToken).where(
                PersonalAccessToken.principal_id == principal.id,
                PersonalAccessToken.key_id == key_id,
            )
        ).scalar_one_or_none()
        if pat is None:
            raise NotFoundError(f"No PAT {key_id!r} for principal {principal_name!r}.")
        if pat.revoked_at is not None:
            return
        pat.revoked_at = datetime.now(UTC)
        AuditService(self._session).record(
            p.id,
            AuditEventType.PAT_REVOKED,
            detail={"actor": p.name, "subject": principal_name, "key_id": key_id},
        )
        self._session.commit()

    def verify(self, key_id: str, secret: str) -> PrincipalRef:
        """Verify a presented PAT and resolve it to its principal.

        Parameters
        ----------
        key_id
            The PAT's public lookup key.
        secret
            The plaintext secret presented by the caller.

        Returns
        -------
        PrincipalRef
            The principal this PAT authenticates as.

        Raises
        ------
        AuthenticationError
            If `key_id` is unknown, the secret does not match, the PAT is
            expired or revoked, or its principal is deactivated.
        """
        pat = self._session.execute(
            select(PersonalAccessToken).where(PersonalAccessToken.key_id == key_id)
        ).scalar_one_or_none()
        if pat is None or not verify_secret(secret, pat.secret_hash):
            raise AuthenticationError("Invalid credentials.")
        if pat.revoked_at is not None:
            raise AuthenticationError("This PAT has been revoked.")
        if pat.expires_at is not None and pat.expires_at <= datetime.now(UTC):
            raise AuthenticationError("This PAT has expired.")
        principal = self._session.get(Principal, pat.principal_id)
        if principal is None or not principal.is_active:
            raise AuthenticationError("This principal is not active.")
        pat.last_used_at = datetime.now(UTC)
        self._session.commit()
        return PrincipalRef(
            id=principal.id, name=principal.name, type=principal.type, language=principal.language
        )
