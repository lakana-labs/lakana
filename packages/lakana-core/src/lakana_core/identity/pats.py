"""PAT issuance, listing, revocation, and verification."""

from __future__ import annotations

import builtins
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from lakana_core.audit.service import AuditService
from lakana_core.auth import Action, AuthorizationService, PrincipalRef, Securable
from lakana_core.errors import AuthenticationError, ConflictError, NotFoundError, PermissionDenied
from lakana_core.grants.admin import authorize_admin_only, may_manage_tokens
from lakana_core.identifiers import refuse_hidden_characters, refuse_outer_whitespace
from lakana_core.identity.sessions import require_step_up
from lakana_core.identity.tokens import generate_token, hash_secret, verify_secret
from lakana_core.models import AuditEventType, PersonalAccessToken, Principal
from lakana_core.pagination import Page, newest_first

OWN_TOKEN_FROM_BROWSER = (
    "A token cannot create a token. Create your own token in the web interface, under your "  # nosec B105 -- a refusal message, not a credential
    "name, after touching your passkey; or ask an administrator: lakana token issue NAME LABEL"
)
"""The refusal of a token that tries to create a token for its own owner."""


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


@dataclass(frozen=True)
class ListedPat:
    """A listed PAT, with the name of the principal it signs in as.

    Parameters
    ----------
    token
        The token's row. It carries only `secret_hash`, never a retrievable secret.
    owner
        The name of the principal the token authenticates as.
    """

    token: PersonalAccessToken
    owner: str


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
    An administrator (the `ADMIN` role on the instance) issues, lists and revokes the tokens of
    anyone, with a token or a browser session, as always; issuing in a browser asks for a fresh
    passkey touch. Everyone else may list and revoke their own tokens with a personal token or a
    browser session, and create one only from a browser session after the passkey touch: no
    token (personal, task or CI) of a non-administrator can create a personal token, so a stolen
    one cannot mint replacements. A task or CI token has no rights over its owner's tokens.
    Anything about another person's tokens needs the `ADMIN` role, and is refused before any
    lookup, so the answer never says whether that person exists.
    """

    def __init__(self, session: Session, authz: AuthorizationService) -> None:
        self._session = session
        self._authz = authz

    def _own(self, p: PrincipalRef, principal_name: str) -> bool:
        """Say whether `p` acts on its own tokens, as a person and not as an administrator.

        Parameters
        ----------
        p
            The acting principal.
        principal_name
            The principal whose tokens are asked about.

        Returns
        -------
        bool
            `True` if it is `p` itself, through a personal token or a browser session (not a
            task or CI token), and `p` is not an administrator: an administrator keeps the
            rule for administrators.
        """
        return (
            principal_name == p.name
            and p.task_scope is None
            and p.ci_scope is None
            and not may_manage_tokens(self._session, self._authz, p)
        )

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
        bootstrap: bool = False,
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
        bootstrap
            Mark the PAT as the operator's token (`PersonalAccessToken.bootstrap`). Passed only
            by `lakana_core.bootstrap.bootstrap_if_needed`; the platform API's request schema
            has no such field, so a PAT an administrator issues, whatever its name, never is.
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
        ValidationError
            If `name` holds an invisible or line-breaking character
            (`refuse_hidden_characters`) or starts or ends with a space
            (`refuse_outer_whitespace`).
        ConflictError
            If `key_id`/`secret` were given explicitly and that exact
            `key_id` already exists, or a randomly generated `key_id`
            collides twice in a row (astronomically rare, but a
            server-side generation conflict, not an authentication
            failure — see the retry-once behaviour below).
        PermissionDenied
            If the token is for someone else and the acting principal does not hold the `ADMIN`
            role on the instance (`MANAGE_TOKEN` works only with it); or it is for the acting
            principal itself, which is not an administrator, and the call did not come from a
            browser session (`OWN_TOKEN_FROM_BROWSER`, audited as `AUTHORIZATION_DENIED` with
            ``reason: not_from_a_browser``).
        sqlalchemy.exc.IntegrityError
            If the commit fails for any reason other than a key_id collision
            — e.g. the acting principal's audit foreign key.
        StepUpRequired
            In a browser session whose last passkey touch is older than 15 minutes.
        """
        if self._own(p, principal_name):
            if p.session is None:
                AuditService(self._session).record(
                    p.id,
                    AuditEventType.AUTHORIZATION_DENIED,
                    securable=Securable.instance_scope(),
                    detail={"operation": "issue_token", "reason": "not_from_a_browser"},
                    commit=True,
                )
                raise PermissionDenied(OWN_TOKEN_FROM_BROWSER)
        else:
            authorize_admin_only(self._session, self._authz, p, Action.MANAGE_TOKEN)
        require_step_up(p)
        refuse_hidden_characters(name, what="Token name")
        refuse_outer_whitespace(name, what="Token name")
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
                bootstrap=bootstrap,
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

    def list(self, p: PrincipalRef, principal_name: str) -> builtins.list[ListedPat]:
        """List every PAT issued to a principal.

        Parameters
        ----------
        p
            The acting principal.
        principal_name
            The principal whose PATs to list.

        Returns
        -------
        list of ListedPat
            Every PAT row for that principal, revoked and expired ones too, each with the
            principal's name. Rows carry only `secret_hash`, never a retrievable secret.

        Raises
        ------
        NotFoundError
            If no principal has `principal_name`.
        PermissionDenied
            If the principal is someone else and the acting principal does not hold the
            `ADMIN` role on the instance (`MANAGE_TOKEN` works only with it); or the call
            is made with a task or CI token.
        """
        if not self._own(p, principal_name):
            authorize_admin_only(self._session, self._authz, p, Action.MANAGE_TOKEN)
        principal = self._get_principal(principal_name)
        rows = self._session.execute(
            select(PersonalAccessToken).where(PersonalAccessToken.principal_id == principal.id)
        ).scalars()
        return [ListedPat(token=row, owner=principal.name) for row in rows]

    def list_all(
        self,
        p: PrincipalRef,
        *,
        owner: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> Page[ListedPat]:
        """List the live PATs of every principal, newest first, one page at a time.

        Parameters
        ----------
        p
            The acting principal.
        owner
            Only this principal's tokens; `None` for everyone's.
        limit
            Most tokens returned.
        cursor
            The `next_cursor` of the previous page. Send the same `owner` with every page.

        Returns
        -------
        Page of ListedPat
            The tokens neither revoked nor expired, by creation time, newest first, equal times
            by id, each with its owner's name, and the cursor of the next page.

        Raises
        ------
        PermissionDenied
            If the acting principal does not hold the `ADMIN` role on the instance
            (`MANAGE_TOKEN` works only with it); a task or CI token is refused too.
        NotFoundError
            If `owner` is given and no principal has that name.
        ValidationError
            If `limit` is below 1 or `cursor` is not one this server issued for the tokens
            list.
        """
        authorize_admin_only(self._session, self._authz, p, Action.MANAGE_TOKEN)
        query = select(PersonalAccessToken).where(
            PersonalAccessToken.revoked_at.is_(None),
            or_(
                PersonalAccessToken.expires_at.is_(None),
                PersonalAccessToken.expires_at > datetime.now(UTC),
            ),
        )
        if owner is not None:
            query = query.where(PersonalAccessToken.principal_id == self._get_principal(owner).id)
        page = newest_first(
            self._session,
            query,
            kind="tokens",
            at=PersonalAccessToken.created_at,
            row_id=PersonalAccessToken.id,
            at_of=lambda row: row.created_at,
            limit=limit,
            cursor=cursor,
        )
        names = {
            principal_id: principal_name
            for principal_id, principal_name in self._session.execute(
                select(Principal.id, Principal.name).where(
                    Principal.id.in_({row.principal_id for row in page.items})
                )
            )
        }
        return Page(
            [ListedPat(token=row, owner=names[row.principal_id]) for row in page.items],
            page.next_cursor,
        )

    def revoke(
        self, p: PrincipalRef, principal_name: str, key_id: str, *, commit: bool = True
    ) -> None:
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
        commit
            If false, leave the change and its audit event in the session for the caller's commit.

        Raises
        ------
        NotFoundError
            If no principal has `principal_name`, or it has no PAT with
            that `key_id`.
        PermissionDenied
            If the principal is someone else and the acting principal does not hold the
            `ADMIN` role on the instance (`MANAGE_TOKEN` works only with it); or the call
            is made with a task or CI token.
        """
        if not self._own(p, principal_name):
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
        if commit:
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
            The principal this PAT authenticates as, with ``credential="pat"`` and the PAT's
            row id as `token_id`.

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
            id=principal.id,
            name=principal.name,
            type=principal.type,
            language=principal.language,
            credential="pat",
            token_id=pat.id,
        )
