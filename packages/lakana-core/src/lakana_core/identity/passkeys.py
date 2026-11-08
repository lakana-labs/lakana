"""Passkeys: WebAuthn registration, usernameless sign-in, step-up, listing and removal.

The ceremonies follow the sign-in design's WebAuthn rules:

- The relying-party id is the host of the configured public address (never an IP address), and
  the origin is checked exactly, port included. A passkey stores the relying-party id it was
  registered for; one registered for an earlier public address is not usable here.
- User verification is required and checked; credentials are discoverable (``residentKey:
  required``), so sign-in sends an empty ``allowCredentials`` and never asks for a name before
  the touch; attestation is ``none``.
- Every challenge is used once and valid for `CHALLENGE_LIFETIME`, and is used up before the
  answer is checked, so a failed answer uses it up too.
- A registration or step-up challenge is 32 random bytes stored with the browser session it is
  bound to, consumed with one conditional update; expired rows go whenever a row is inserted.
- A sign-in challenge is stateless (`sign_challenge`): version, a 32-byte random nonce and the
  expiry, signed with HMAC-SHA256 over the purpose and the RP id by a key kept in the database
  (`CHALLENGE_KEY`). Nothing is stored when it is issued, so no number of anonymous calls can
  push out a real person's challenge. An answer's challenge is checked first (constant time,
  purpose, RP id, expiry, ceremony id); then its nonce is inserted as used, and a replay fails on
  the primary key. Only answered attempts are stored, for 5 minutes.
- A credential id already stored, for anyone, is refused.
- The library does not compare the user handle, so the service does (in constant time).
- A falling signature counter means a copied passkey. The library raises the same exception
  for that as for a bad signature, so the service gives it a stored count of 0 and applies the
  rule itself, *after* the signature verified: a forged assertion can never disable a passkey.
  Two zero counters (a synced passkey) are no clone.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import unicodedata
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import (
    base64url_to_bytes,
    bytes_to_base64url,
    parse_authentication_credential_json,
)
from webauthn.helpers.exceptions import WebAuthnException
from webauthn.helpers.structs import (
    AttestationConveyancePreference,
    AuthenticationCredential,
    AuthenticatorSelectionCriteria,
    CredentialDeviceType,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from lakana_core.audit.service import AuditService
from lakana_core.auth import Action, AuthorizationService, PrincipalRef, Securable, SessionContext
from lakana_core.errors import (
    AlreadyExistsError,
    AuthenticationError,
    ConflictError,
    NotFoundError,
    PasskeyRefused,
    PermissionDenied,
    StepUpRequired,
    ValidationError,
)
from lakana_core.grants.admin import acts_as_instance_admin
from lakana_core.grants.service import active_instance_admin_ids
from lakana_core.identity.links import has_passkey
from lakana_core.identity.notices import NoticeService
from lakana_core.identity.sessions import (
    SessionService,
    StartedSession,
    require_step_up,
    utc_now,
)
from lakana_core.models import (
    AuditEventType,
    InstanceSetting,
    Passkey,
    Principal,
    PrincipalType,
    WebauthnChallenge,
)

CHALLENGE_LIFETIME = timedelta(minutes=5)
"""How long a WebAuthn challenge can be answered."""

FIRST_PASSKEY_WINDOW = timedelta(minutes=15)
"""How long after a link sign-in the first passkey may be added without a touch."""

CLOCK_SKEW = timedelta(seconds=30)
"""How much later than `CHALLENGE_LIFETIME` from now a sign-in challenge's expiry may be, for
server processes whose clocks differ a little."""

CHALLENGE_KEY = "sign_in.challenge_key"
"""The instance setting holding the key that signs sign-in challenges (32 random bytes, base64).

In the database, not in the process: every server process and replica must accept a challenge
another one issued, and a restart must not break a sign-in in progress. Whoever can read the
database could forge a challenge, which still needs the passkey's signature to sign in."""

_CHALLENGE_VERSION = b"\x01"
_NONCE_BYTES = 32
_MAC_BYTES = 32
_TOKEN_BYTES = 1 + _NONCE_BYTES + 8 + _MAC_BYTES

OPEN_BODY_LIMIT = 16 * 1024
"""The largest body the two open passkey routes read, like the OIDC exchange."""

LINK_VIAS = frozenset({"setup", "invite", "recovery", "local"})
"""The sign-ins after which a person without a passkey may add their first one."""

NEEDS_PASSKEY_VIAS = frozenset({"setup", "invite", "recovery"})
"""The sign-ins after which a person without a usable passkey must register one first."""

_CHALLENGE_BYTES = 32
_MAX_NAME = 100
_ANSWER_ERRORS = (WebAuthnException, ValueError, TypeError, KeyError, IndexError)
"""What the library and its parsers raise for an answer that is malformed or does not verify."""

_CHALLENGE_GONE = (
    "This passkey request expired or was used already (each works once, for 5 minutes). "
    "Start again."
)
_NO_SESSION = (
    "Passkeys are added and confirmed in the web interface: a passkey ceremony belongs to a "
    "browser session, and a token has none. Sign in to the web interface and do it there."
)
_NOT_VERIFIED = (
    "Your browser's passkey answer did not verify: it came from another address than {origin}, "
    "without user verification (a PIN, fingerprint or face), or damaged. Open the interface at "
    "{origin} and try again."
)
_CLONE = (
    "This passkey was just disabled: its signature counter went backwards, which means it may "
    "have been copied. Every session of its account was ended. Sign in with another passkey, or "
    "ask an administrator for a recovery link. Your administrators were told."
)


def needs_passkey(session: Session, p: PrincipalRef, rp_id: str) -> bool:
    """Say whether a browser session must register a passkey before it may do anything else.

    True for a session that began with a setup, invite or recovery link while its person has
    no usable passkey; a local link and a token never need one.

    Parameters
    ----------
    session
        Database session.
    p
        The caller.
    rp_id
        The current relying-party id.

    Returns
    -------
    bool
        Whether only registering a passkey, reading the session and signing out are allowed.
    """
    return (
        p.session is not None
        and p.session.via in NEEDS_PASSKEY_VIAS
        and not has_passkey(session, p.id, rp_id=rp_id)
    )


def _passkey_name(name: str | None) -> str:
    """Check the name a person gives a passkey.

    Parameters
    ----------
    name
        The name, or `None` for the default.

    Returns
    -------
    str
        The name, stripped; ``Passkey`` when none was given.

    Raises
    ------
    ValidationError
        If it is longer than 100 characters or contains a control character.
    """
    text = (name or "").strip()
    if not text:
        return "Passkey"
    if len(text) > _MAX_NAME or any(unicodedata.category(ch).startswith("C") for ch in text):
        raise ValidationError(
            f"A passkey name has at most {_MAX_NAME} characters and no control characters. "
            "Choose a shorter name, such as the device it is on."
        )
    return text


class PasskeyService:
    """Register passkeys, sign in and step up with them, and list and remove them.

    Parameters
    ----------
    session
        Database session. Every mutating method commits.
    authz
        Authorization service, asked before an administrator lists or removes another
        person's passkeys.
    sessions
        The session service a sign-in starts its session with, and that records touches.
    notices
        Writes the notices of an added or disabled passkey.
    rp_id
        The relying-party id: the host of the public address (`Settings.public_origin().host`).
    origin
        The exact origin browsers use (`Settings.public_origin().origin`).
    rp_name
        The name authenticators show.
    clock
        The clock; `utc_now` by default.
    """

    def __init__(
        self,
        session: Session,
        authz: AuthorizationService,
        sessions: SessionService,
        notices: NoticeService,
        *,
        rp_id: str,
        origin: str,
        rp_name: str = "Lakana",
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._session = session
        self._authz = authz
        self._sessions = sessions
        self._notices = notices
        self._rp_id = rp_id
        self._origin = origin
        self._rp_name = rp_name
        self._clock = clock
        self._key: bytes | None = None

    # -- shared steps -----------------------------------------------------------------------

    def _session_of(self, p: PrincipalRef) -> SessionContext:
        """Read the browser session a ceremony binds to.

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
            If the caller authenticated with a token.
        """
        if p.session is None:
            raise ConflictError(_NO_SESSION)
        return p.session

    def _require_admin(self, p: PrincipalRef) -> None:
        """Refuse unless `p` is an instance administrator.

        Parameters
        ----------
        p
            The acting principal.

        Raises
        ------
        PermissionDenied
            If `p` does not hold the `ADMIN` role at instance scope.
        """
        self._authz.authorize(p, Action.MANAGE_PRINCIPAL, Securable.instance_scope())
        if acts_as_instance_admin(self._session, self._authz, p):
            return
        raise PermissionDenied(
            "Only an administrator (the ADMIN role on the instance) can see or remove another "
            "person's passkeys. Ask an administrator."
        )

    def _principal_named(self, name: str) -> Principal:
        """Look up a principal by name.

        Parameters
        ----------
        name
            The name.

        Returns
        -------
        Principal
            The row.

        Raises
        ------
        NotFoundError
            If no principal has that name.
        """
        row = self._session.execute(
            select(Principal).where(Principal.name == name)
        ).scalar_one_or_none()
        if row is None:
            raise NotFoundError(f"No principal named {name!r}. Check the name and try again.")
        return row

    def _new_challenge(
        self, purpose: str, *, session_id: uuid.UUID | None, principal_id: uuid.UUID | None
    ) -> WebauthnChallenge:
        """Store a fresh challenge, deleting every expired one in the same transaction.

        Parameters
        ----------
        purpose
            ``register``, ``sign_in`` or ``step_up``.
        session_id
            The browser session it binds to; `None` for sign-in.
        principal_id
            The person it is for; `None` for sign-in.

        Returns
        -------
        WebauthnChallenge
            The new row (not yet committed).
        """
        now = self._clock()
        self._session.execute(
            delete(WebauthnChallenge)
            .where(WebauthnChallenge.expires_at <= now)
            .execution_options(synchronize_session=False)
        )
        row = WebauthnChallenge(
            id=uuid.uuid4(),
            challenge=os.urandom(_CHALLENGE_BYTES),
            purpose=purpose,
            session_id=session_id,
            principal_id=principal_id,
            created_at=now,
            expires_at=now + CHALLENGE_LIFETIME,
        )
        self._session.add(row)
        return row

    def _consume(
        self,
        ceremony_id: uuid.UUID,
        purpose: str,
        *,
        session_id: uuid.UUID | None,
        error: type[AuthenticationError],
    ) -> bytes:
        """Use a challenge up, with one conditional update, and commit that at once.

        Committed before the answer is checked, so a failed answer cannot be tried again.

        Parameters
        ----------
        ceremony_id
            The challenge's id.
        purpose
            The purpose it must have.
        session_id
            The browser session it must be bound to; `None` for an unbound one.
        error
            The exception to raise when it cannot be used.

        Returns
        -------
        bytes
            The challenge.

        Raises
        ------
        AuthenticationError
            Of type `error`, if no unused, unexpired challenge of that purpose and session has
            that id.
        """
        now = self._clock()
        bound = (
            WebauthnChallenge.session_id.is_(None)
            if session_id is None
            else WebauthnChallenge.session_id == session_id
        )
        found = self._session.execute(
            update(WebauthnChallenge)
            .where(
                WebauthnChallenge.id == ceremony_id,
                WebauthnChallenge.purpose == purpose,
                WebauthnChallenge.used_at.is_(None),
                WebauthnChallenge.expires_at > now,
                bound,
            )
            .values(used_at=now)
            .returning(WebauthnChallenge.challenge)
            .execution_options(synchronize_session=False)
        ).scalar_one_or_none()
        self._session.commit()
        if found is None:
            raise error(_CHALLENGE_GONE)
        return found

    def _descriptors(self, principal_id: uuid.UUID) -> list[PublicKeyCredentialDescriptor]:
        """List a person's passkeys for this address as WebAuthn descriptors.

        Parameters
        ----------
        principal_id
            The person.

        Returns
        -------
        list of PublicKeyCredentialDescriptor
            One per passkey registered for the current relying-party id.
        """
        ids = self._session.execute(
            select(Passkey.credential_id).where(
                Passkey.principal_id == principal_id, Passkey.rp_id == self._rp_id
            )
        ).scalars()
        return [PublicKeyCredentialDescriptor(id=base64url_to_bytes(cid)) for cid in ids]

    def _authentication_options(
        self, purpose: str, *, p: PrincipalRef | None
    ) -> tuple[uuid.UUID, dict[str, Any]]:
        """Issue authentication options, user verification required.

        Parameters
        ----------
        purpose
            ``sign_in`` (a signed, unstored challenge, no credential list) or ``step_up`` (a
            stored challenge bound to `p`'s session).
        p
            The caller of a step-up; `None` for sign-in.

        Returns
        -------
        tuple of (uuid.UUID, dict)
            The ceremony id and the ``publicKey`` options in their JSON form.
        """
        if p is None:
            # Sign-in: a signed challenge, nothing stored, so a flood of anonymous calls can
            # neither grow a table nor push out a real person's challenge.
            nonce = os.urandom(_NONCE_BYTES)
            expires = int(self._clock().timestamp() + CHALLENGE_LIFETIME.total_seconds())
            challenge = sign_challenge(self._challenge_key(), purpose, self._rp_id, nonce, expires)
            ceremony_id = uuid.UUID(bytes=nonce[:16])
            descriptors: list[PublicKeyCredentialDescriptor] = []
        else:
            context = self._session_of(p)
            row = self._new_challenge(purpose, session_id=context.session_id, principal_id=p.id)
            challenge, ceremony_id = row.challenge, row.id
            descriptors = self._descriptors(p.id)
        options = generate_authentication_options(
            rp_id=self._rp_id,
            challenge=challenge,
            timeout=int(CHALLENGE_LIFETIME.total_seconds() * 1000),
            allow_credentials=descriptors,
            user_verification=UserVerificationRequirement.REQUIRED,
        )
        self._session.commit()
        return ceremony_id, _json(options)

    def _parse(
        self, credential_json: str, error: type[AuthenticationError]
    ) -> AuthenticationCredential:
        """Parse an assertion without trusting any of it yet.

        Parameters
        ----------
        credential_json
            The browser's ``credential.toJSON()``.
        error
            The exception to raise when it cannot be parsed.

        Returns
        -------
        AuthenticationCredential
            The parsed assertion.

        Raises
        ------
        AuthenticationError
            Of type `error`, for a malformed answer.
        """
        try:
            return parse_authentication_credential_json(credential_json)
        except _ANSWER_ERRORS:
            raise error(_NOT_VERIFIED.format(origin=self._origin)) from None

    def _assert(
        self,
        parsed: AuthenticationCredential,
        challenge: bytes,
        error: type[AuthenticationError],
        *,
        owner: uuid.UUID | None,
    ) -> tuple[Passkey, Principal]:
        """Check an assertion completely, then apply the clone rule and record the counter.

        Parameters
        ----------
        parsed
            The parsed assertion.
        challenge
            The consumed challenge.
        error
            The exception to raise when the assertion is refused.
        owner
            For a step-up, the person the passkey must belong to; `None` for sign-in.

        Returns
        -------
        tuple of (Passkey, Principal)
            The passkey, with its new counter and `last_used_at` (not yet committed), and its
            person.

        Raises
        ------
        AuthenticationError
            Of type `error`: an unknown passkey, another person's (step-up), a disabled one, one
            of an earlier public address, a wrong user handle, a person who cannot sign in, an
            answer that does not verify, or a falling counter (the passkey is then disabled and
            that is committed).
        """
        credential_id = bytes_to_base64url(parsed.raw_id)
        row = self._session.execute(
            select(Passkey).where(Passkey.credential_id == credential_id)
        ).scalar_one_or_none()
        if owner is not None and (row is None or row.principal_id != owner):
            raise error(
                "This passkey is not one of yours. Confirm with a passkey of the account you "
                "are signed in as."
            )
        if row is None:
            raise error(
                "This passkey is not registered here. Choose another passkey, or ask an "
                "administrator for a sign-in link."
            )
        if row.disabled_at is not None:
            raise error(
                "This passkey is disabled because its signature counter went backwards (it may "
                "have been copied). Use another passkey, or ask an administrator for a recovery "
                "link."
            )
        if row.rp_id != self._rp_id:
            raise error(
                f"This passkey was registered for an earlier public address ({row.rp_id}). "
                "Changing the address leaves passkeys behind: ask an administrator for a "
                "recovery link and add a new passkey."
            )
        principal = self._session.get(Principal, row.principal_id)
        assert principal is not None  # nosec B101 -- the foreign key cascades on delete
        presented = parsed.response.user_handle or b""
        stored = principal.webauthn_handle or b""
        if not presented or not stored or not hmac.compare_digest(presented, stored):
            raise error(_NOT_VERIFIED.format(origin=self._origin))
        if principal.type != PrincipalType.USER or not principal.is_active:
            # No name before the signature verified: the answer may be forged.
            raise error(
                "The account of this passkey is deactivated. Ask an administrator to reactivate it."
            )
        try:
            verified = verify_authentication_response(
                credential=parsed,
                expected_challenge=challenge,
                expected_rp_id=self._rp_id,
                expected_origin=self._origin,
                credential_public_key=row.public_key,
                # The library's own counter check raises what a bad signature raises; the
                # clone rule below runs only once the signature verified.
                credential_current_sign_count=0,
                require_user_verification=True,
            )
        except _ANSWER_ERRORS:
            raise error(_NOT_VERIFIED.format(origin=self._origin)) from None
        if _cross_origin(parsed.response.client_data_json):
            raise error(_NOT_VERIFIED.format(origin=self._origin))
        stored_count, new_count = row.sign_count, verified.new_sign_count
        if not (stored_count == 0 and new_count == 0) and new_count <= stored_count:
            self._disable_as_clone(row, principal, stored_count, new_count)
            raise error(_CLONE)
        # Conditional on the counter just read: of two uses at once, one wins.
        recorded = self._session.execute(
            update(Passkey)
            .where(Passkey.id == row.id, Passkey.sign_count == stored_count)
            .values(sign_count=new_count, last_used_at=self._clock())
            .execution_options(synchronize_session=False)
        ).rowcount  # ty: ignore[unresolved-attribute]
        if recorded != 1:
            self._session.rollback()
            raise error(
                "This passkey was used at the same moment elsewhere, so this use was refused. "
                "Try again; if it keeps happening, the passkey may have been copied."
            )
        self._session.refresh(row)
        return row, principal

    def _disable_as_clone(
        self, row: Passkey, principal: Principal, stored_count: int, new_count: int
    ) -> None:
        """Disable a copied passkey, end every session of its person, audit, tell everyone.

        Strict on purpose: any session of the person may be the copy's, and nothing records
        which passkey a session began with, so all of them end (``passkey_copied``).

        Parameters
        ----------
        row
            The passkey.
        principal
            Its person.
        stored_count
            The counter stored.
        new_count
            The counter presented.
        """
        row.disabled_at = self._clock()
        row.disabled_reason = "possible_clone"
        ended = self._sessions.end_for_principal(principal.id, "passkey_copied")
        AuditService(self._session).record(
            principal.id,
            AuditEventType.PASSKEY_DISABLED,
            detail={
                "passkey_id": str(row.id),
                "name": row.name,
                "reason": "possible_clone",
                "stored_count": stored_count,
                "presented_count": new_count,
                "sessions_ended": ended,
            },
        )
        detail = {
            "name": principal.name,
            "passkey": row.name,
            "passkey_id": str(row.id),
            "message": (
                "A copy of a passkey was detected; it is disabled and every session of "
                f"{principal.name} was ended."
            ),
        }
        for recipient in sorted({principal.id, *active_instance_admin_ids(self._session)}, key=str):
            self._notices.add(recipient, "passkey_disabled", principal.id, detail)
        self._session.commit()

    def _challenge_key(self) -> bytes:
        """Read the key that signs sign-in challenges, creating it on first use.

        Returns
        -------
        bytes
            32 bytes, the same for every server process of the instance.
        """
        if self._key is not None:
            return self._key
        row = self._session.get(InstanceSetting, CHALLENGE_KEY)
        if row is None:
            try:
                with self._session.begin_nested():
                    row = InstanceSetting(
                        key=CHALLENGE_KEY,
                        value=base64.b64encode(os.urandom(32)).decode("ascii"),
                        updated_at=self._clock(),
                    )
                    self._session.add(row)
            except IntegrityError:
                # Another process created it at the same moment: use theirs.
                row = self._session.get(InstanceSetting, CHALLENGE_KEY)
            self._session.commit()
        assert row is not None  # nosec B101 -- created or read just above
        self._key = base64.b64decode(str(row.value))
        return self._key

    def _use_sign_in_challenge(
        self, ceremony_id: uuid.UUID, parsed: AuthenticationCredential
    ) -> bytes:
        """Check a sign-in challenge's signature, purpose, RP id and expiry, then use it up.

        Nothing about the answer is trusted before the challenge's signature checked. Using it
        up is one insert of its nonce (the ceremony id is the primary key), committed at once:
        a replay fails on the key. Expired nonces are deleted in the same transaction, so the
        table holds at most the answered attempts of the last 5 minutes.

        Parameters
        ----------
        ceremony_id
            The id `sign_in_options` returned.
        parsed
            The parsed assertion, whose client data carries the challenge.

        Returns
        -------
        bytes
            The challenge, for the WebAuthn verification.

        Raises
        ------
        AuthenticationError
            If the challenge was not issued here for sign-in at this address, has expired,
            expires later than five minutes (and `CLOCK_SKEW`) from now, belongs to another
            ceremony, or was used.
        """
        gone = AuthenticationError(_CHALLENGE_GONE)
        try:
            data = json.loads(parsed.response.client_data_json)
            challenge = base64url_to_bytes(data["challenge"])
        except (*_ANSWER_ERRORS, AttributeError):
            raise gone from None
        now = self._clock()
        opened = open_challenge(self._challenge_key(), "sign_in", self._rp_id, challenge)
        if opened is None:
            raise gone
        nonce, expires = opened
        latest = now.timestamp() + (CHALLENGE_LIFETIME + CLOCK_SKEW).total_seconds()
        if not now.timestamp() < expires <= latest or uuid.UUID(bytes=nonce[:16]) != ceremony_id:
            # Never later than this server would have set it: whoever could forge one (a reader
            # of the key) gets nothing that outlives five minutes.
            raise gone
        self._session.execute(
            delete(WebauthnChallenge)
            .where(WebauthnChallenge.expires_at <= now)
            .execution_options(synchronize_session=False)
        )
        try:
            with self._session.begin_nested():
                self._session.add(
                    WebauthnChallenge(
                        id=ceremony_id,
                        challenge=nonce,
                        purpose="sign_in",
                        session_id=None,
                        principal_id=None,
                        created_at=now,
                        expires_at=datetime.fromtimestamp(expires, UTC),
                        used_at=now,
                    )
                )
        except IntegrityError:
            self._session.commit()
            raise gone from None
        self._session.commit()
        return challenge

    # -- registration -----------------------------------------------------------------------

    def registration_options(self, p: PrincipalRef) -> tuple[uuid.UUID, dict[str, Any]]:
        """Start adding a passkey in the caller's browser session.

        Allowed with a fresh passkey touch (step-up), or, for a person with no usable passkey,
        within `FIRST_PASSKEY_WINDOW` of a link sign-in. A session from a local link may add
        only the first passkey.

        Parameters
        ----------
        p
            The caller.

        Returns
        -------
        tuple of (uuid.UUID, dict)
            The ceremony id and the ``publicKey`` creation options in their JSON form.

        Raises
        ------
        ConflictError
            If the caller authenticated with a token.
        PermissionDenied
            For a local-link session of a person who has a passkey already.
        StepUpRequired
            Without a fresh touch, outside the first-passkey window.
        NotFoundError
            If the caller's principal no longer exists.
        """
        context = self._session_of(p)
        now = self._clock()
        usable = self.has_usable_passkey(p.id)
        if context.via == "local" and usable:
            raise PermissionDenied(
                "This session began with a local sign-in link, without a passkey touch, so it "
                "may add only a first passkey. Sign in with your passkey to add another."
            )
        first = not usable and context.via in LINK_VIAS
        if not (first and now - context.created_at < FIRST_PASSKEY_WINDOW):
            if not usable and context.via == "local":
                raise StepUpRequired(
                    "A first passkey can be added within 15 minutes of signing in, and this "
                    "session is older. Sign in again with `lakana server start --local` and add "
                    "the passkey right away."
                )
            if not usable and context.via in LINK_VIAS:
                raise StepUpRequired(
                    "A first passkey can be added within 15 minutes of signing in with a link, "
                    "and this session is older. Ask an administrator for a recovery link (or "
                    "the operator for a new setup link) and add the passkey right away."
                )
            require_step_up(p, now=now)
        principal = self._session.get(Principal, p.id)
        if principal is None:
            raise NotFoundError("No such principal.")
        if principal.webauthn_handle is None:
            principal.webauthn_handle = os.urandom(32)
        row = self._new_challenge("register", session_id=context.session_id, principal_id=p.id)
        options = generate_registration_options(
            rp_id=self._rp_id,
            rp_name=self._rp_name,
            user_id=principal.webauthn_handle,
            user_name=principal.name,
            user_display_name=principal.display_name or principal.name,
            challenge=row.challenge,
            timeout=int(CHALLENGE_LIFETIME.total_seconds() * 1000),
            attestation=AttestationConveyancePreference.NONE,
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.REQUIRED,
                user_verification=UserVerificationRequirement.REQUIRED,
            ),
            exclude_credentials=self._descriptors(p.id),
        )
        self._session.commit()
        return row.id, _json(options)

    def register(
        self,
        p: PrincipalRef,
        ceremony_id: uuid.UUID,
        credential_json: str,
        *,
        name: str | None,
        user_agent: str,
    ) -> Passkey:
        """Finish adding a passkey: verify the browser's answer and store the passkey.

        Counts as a passkey touch for step-up. A passkey added later than 15 minutes after a
        link sign-in leaves its person a notice. The step-up rule was checked when the options
        were issued; the answer is accepted for the challenge's 5 minutes even if the touch ages
        out meanwhile, because the challenge is bound to this session.

        Parameters
        ----------
        p
            The caller.
        ceremony_id
            The id `registration_options` returned.
        credential_json
            The browser's ``credential.toJSON()``.
        name
            What the person calls it; ``Passkey`` by default.
        user_agent
            The browser's `User-Agent`, cut to 255 characters.

        Returns
        -------
        Passkey
            The stored passkey.

        Raises
        ------
        ConflictError
            If the caller authenticated with a token.
        ValidationError
            For a name that is too long or has control characters.
        PasskeyRefused
            If the challenge is used, expired or another session's, or the answer does not
            verify (origin, relying-party id, user verification, format).
        AlreadyExistsError
            If the credential id is stored already, for anyone.
        """
        context = self._session_of(p)
        label = _passkey_name(name)
        challenge = self._consume(
            ceremony_id, "register", session_id=context.session_id, error=PasskeyRefused
        )
        try:
            verified = verify_registration_response(
                credential=credential_json,
                expected_challenge=challenge,
                expected_rp_id=self._rp_id,
                expected_origin=self._origin,
                require_user_verification=True,
            )
        except _ANSWER_ERRORS:
            raise PasskeyRefused(_NOT_VERIFIED.format(origin=self._origin)) from None
        if _cross_origin(_registration_client_data(credential_json)):
            raise PasskeyRefused(_NOT_VERIFIED.format(origin=self._origin))
        credential_id = bytes_to_base64url(verified.credential_id)
        taken = AlreadyExistsError(
            "This passkey is registered already. Sign in with it, or add a different one."
        )
        if self._session.execute(
            select(Passkey.id).where(Passkey.credential_id == credential_id)
        ).first():
            raise taken
        now = self._clock()
        row = Passkey(
            id=uuid.uuid4(),
            principal_id=p.id,
            credential_id=credential_id,
            public_key=verified.credential_public_key,
            sign_count=verified.sign_count,
            rp_id=self._rp_id,
            name=label,
            backup_eligible=verified.credential_device_type == CredentialDeviceType.MULTI_DEVICE,
            created_at=now,
            created_session_id=context.session_id,
            created_user_agent=user_agent[:255],
        )
        self._session.add(row)
        try:
            self._session.flush()
        except IntegrityError:
            self._session.rollback()
            raise taken from None
        self._sessions.touch(context.key_id)
        AuditService(self._session).record(
            p.id,
            AuditEventType.PASSKEY_ADDED,
            detail={
                "passkey_id": str(row.id),
                "name": label,
                "via": context.via,
                "backup_eligible": row.backup_eligible,
            },
        )
        fresh_link = context.via in LINK_VIAS and now - context.created_at < FIRST_PASSKEY_WINDOW
        if not fresh_link:
            self._notices.add(
                p.id, "passkey_added", p.id, {"passkey": label, "passkey_id": str(row.id)}
            )
        self._session.commit()
        return row

    # -- sign-in and step-up ----------------------------------------------------------------

    def sign_in_options(self) -> tuple[uuid.UUID, dict[str, Any]]:
        """Start a usernameless passkey sign-in.

        Returns
        -------
        tuple of (uuid.UUID, dict)
            The ceremony id and the ``publicKey`` request options, with an empty
            ``allowCredentials``.
        """
        return self._authentication_options("sign_in", p=None)

    def sign_in(
        self,
        ceremony_id: uuid.UUID,
        credential_json: str,
        *,
        replacing: str | None,
        peer: str,
        user_agent: str,
    ) -> StartedSession:
        """Sign in with a passkey and start a browser session with a touch.

        Parameters
        ----------
        ceremony_id
            The id `sign_in_options` returned.
        credential_json
            The browser's ``credential.toJSON()``.
        replacing
            The session cookie the browser presented, if any; ended if valid.
        peer
            The client's address.
        user_agent
            The browser's `User-Agent`.

        Returns
        -------
        StartedSession
            The new session, ``via="passkey"``.

        Raises
        ------
        AuthenticationError
            For a used or expired challenge, an unknown, disabled or copied passkey, a wrong
            user handle, a deactivated person or an answer that does not verify.
        """
        parsed = self._parse(credential_json, AuthenticationError)
        challenge = self._use_sign_in_challenge(ceremony_id, parsed)
        _, principal = self._assert(parsed, challenge, AuthenticationError, owner=None)
        started = self._sessions.start(
            principal.id,
            via="passkey",
            replacing=replacing,
            peer=peer,
            user_agent=user_agent,
            touched=True,
        )
        self._session.commit()
        return started

    def step_up_options(self, p: PrincipalRef) -> tuple[uuid.UUID, dict[str, Any]]:
        """Start confirming the caller's browser session with a passkey touch.

        Parameters
        ----------
        p
            The caller.

        Returns
        -------
        tuple of (uuid.UUID, dict)
            The ceremony id and the ``publicKey`` request options, listing the caller's
            passkeys.

        Raises
        ------
        ConflictError
            If the caller authenticated with a token: step-up is for browser sessions.
        """
        return self._authentication_options("step_up", p=p)

    def step_up(self, p: PrincipalRef, ceremony_id: uuid.UUID, credential_json: str) -> datetime:
        """Confirm the caller's browser session with a touch of one of their own passkeys.

        Parameters
        ----------
        p
            The caller.
        ceremony_id
            The id `step_up_options` returned.
        credential_json
            The browser's ``credential.toJSON()``.

        Returns
        -------
        datetime
            The touch time; step-up actions need no new touch for 15 minutes after it.

        Raises
        ------
        ConflictError
            If the caller authenticated with a token.
        PasskeyRefused
            For a used, expired or other session's challenge, another person's passkey, or any
            refusal `sign_in` would give.
        """
        context = self._session_of(p)
        challenge = self._consume(
            ceremony_id, "step_up", session_id=context.session_id, error=PasskeyRefused
        )
        parsed = self._parse(credential_json, PasskeyRefused)
        self._assert(parsed, challenge, PasskeyRefused, owner=p.id)
        self._sessions.touch(context.key_id)
        self._session.commit()
        return self._clock()

    # -- reading and removing ---------------------------------------------------------------

    def has_usable_passkey(self, principal_id: uuid.UUID) -> bool:
        """Say whether a person has a passkey that can sign in at this address.

        Parameters
        ----------
        principal_id
            The person.

        Returns
        -------
        bool
            Whether one is neither disabled nor registered for another relying-party id.
        """
        return has_passkey(self._session, principal_id, rp_id=self._rp_id)

    def list_passkeys(self, p: PrincipalRef, principal_name: str) -> list[Passkey]:
        """List a person's passkeys, oldest first, disabled ones included.

        Parameters
        ----------
        p
            The caller: the person, or an administrator.
        principal_name
            Whose passkeys.

        Returns
        -------
        list of Passkey
            The passkeys.

        Raises
        ------
        PermissionDenied
            If `p` is another person and not an administrator, whether or not the name exists.
        NotFoundError
            If no principal has the name.
        """
        if principal_name != p.name:
            self._require_admin(p)
        target = self._principal_named(principal_name)
        return list(
            self._session.execute(
                select(Passkey)
                .where(Passkey.principal_id == target.id)
                .order_by(Passkey.created_at, Passkey.id)
            ).scalars()
        )

    def remove(self, p: PrincipalRef, principal_name: str, passkey_id: uuid.UUID) -> None:
        """Remove a passkey: one's own, or, for an administrator, anyone's.

        Needs a fresh passkey touch in a browser session. A person cannot remove their own last
        passkey that works at this address (one of an earlier public address does not count).
        An administrator removing another person's passkey ends all of that person's sessions.

        Parameters
        ----------
        p
            The caller.
        principal_name
            Whose passkey.
        passkey_id
            The passkey.

        Raises
        ------
        PermissionDenied
            If `p` is another person and not an administrator.
        StepUpRequired
            Without a fresh passkey touch in a browser session.
        NotFoundError
            If the person has no passkey with that id.
        ConflictError
            If it is the caller's own last passkey that works at this address.
        """
        if principal_name != p.name:
            self._require_admin(p)
        require_step_up(p, now=self._clock())
        target = self._principal_named(principal_name)
        row = self._session.execute(
            select(Passkey).where(Passkey.id == passkey_id, Passkey.principal_id == target.id)
        ).scalar_one_or_none()
        if row is None:
            raise NotFoundError(
                f"{principal_name!r} has no passkey with this id. List the passkeys to see them."
            )
        own = target.id == p.id
        if own and row.disabled_at is None and row.rp_id == self._rp_id:
            # Only passkeys that can sign in at this address count: one of an earlier public
            # address is no way back in.
            others = self._session.execute(
                select(Passkey.id).where(
                    Passkey.principal_id == target.id,
                    Passkey.id != row.id,
                    Passkey.disabled_at.is_(None),
                    Passkey.rp_id == self._rp_id,
                )
            ).first()
            if others is None:
                raise ConflictError(
                    "This is your last passkey, and without it you could not sign in. Add "
                    "another passkey first, or ask an administrator for a recovery link."
                )
        self._session.delete(row)
        AuditService(self._session).record(
            p.id,
            AuditEventType.PASSKEY_REMOVED,
            detail={"passkey_id": str(row.id), "name": row.name, "subject": target.name},
        )
        if not own:
            self._sessions.end_for_principal(target.id, "passkey_removed")
        self._session.commit()


def _mac(key: bytes, purpose: str, rp_id: str, body: bytes) -> bytes:
    """Sign a challenge's body for one purpose at one relying-party id.

    Parameters
    ----------
    key
        The instance's challenge key.
    purpose
        What the challenge is for, such as ``sign_in``.
    rp_id
        The relying-party id.
    body
        Version, nonce and expiry.

    Returns
    -------
    bytes
        HMAC-SHA256.
    """
    message = b"\x00".join([b"lakana-webauthn-challenge", purpose.encode(), rp_id.encode(), body])
    return hmac.new(key, message, hashlib.sha256).digest()


def sign_challenge(key: bytes, purpose: str, rp_id: str, nonce: bytes, expires: int) -> bytes:
    """Build a signed challenge: version, nonce, expiry (Unix seconds), HMAC.

    Parameters
    ----------
    key
        The instance's challenge key.
    purpose
        What it is for.
    rp_id
        The relying-party id it is for.
    nonce
        32 random bytes.
    expires
        When it stops working, in Unix seconds.

    Returns
    -------
    bytes
        The challenge the browser signs over.
    """
    body = _CHALLENGE_VERSION + nonce + expires.to_bytes(8, "big")
    return body + _mac(key, purpose, rp_id, body)


def open_challenge(
    key: bytes, purpose: str, rp_id: str, challenge: bytes
) -> tuple[bytes, int] | None:
    """Check a signed challenge, in constant time.

    Parameters
    ----------
    key
        The instance's challenge key.
    purpose
        The purpose it must have been signed for.
    rp_id
        The relying-party id it must have been signed for.
    challenge
        What came back.

    Returns
    -------
    tuple of (bytes, int), or None
        The nonce and the expiry, or `None` when it was not signed with this key for this
        purpose and RP id.
    """
    if len(challenge) != _TOKEN_BYTES or challenge[:1] != _CHALLENGE_VERSION:
        return None
    body, mac = challenge[:-_MAC_BYTES], challenge[-_MAC_BYTES:]
    if not hmac.compare_digest(mac, _mac(key, purpose, rp_id, body)):
        return None
    return body[1 : 1 + _NONCE_BYTES], int.from_bytes(body[1 + _NONCE_BYTES :], "big")


def _cross_origin(client_data_json: bytes) -> bool:
    """Say whether a ceremony ran in a frame of another site.

    The origin alone is checked by the library; a page of ours framed by another site would
    still carry our origin, with ``crossOrigin: true`` (and a ``topOrigin``) beside it.

    Parameters
    ----------
    client_data_json
        The browser's ``clientDataJSON``.

    Returns
    -------
    bool
        `True` when ``crossOrigin`` is true or a ``topOrigin`` is present, and for data that
        cannot be read.
    """
    try:
        data = json.loads(client_data_json)
    except ValueError:
        return True
    return not isinstance(data, dict) or data.get("crossOrigin") is True or "topOrigin" in data


def _registration_client_data(credential_json: str) -> bytes:
    """Read ``clientDataJSON`` from a registration answer the library has already verified.

    Parameters
    ----------
    credential_json
        The browser's ``credential.toJSON()``.

    Returns
    -------
    bytes
        The client data; empty when it cannot be read (`_cross_origin` then refuses).
    """
    try:
        return base64url_to_bytes(json.loads(credential_json)["response"]["clientDataJSON"])
    except _ANSWER_ERRORS:
        return b""


def _json(options: object) -> dict[str, Any]:
    """Turn the library's options into their JSON form, as the browser parses them.

    Parameters
    ----------
    options
        Registration or authentication options.

    Returns
    -------
    dict
        The ``publicKey`` options (`PublicKeyCredential.parse*OptionsFromJSON` reads them).
    """
    return json.loads(options_to_json(options))  # ty: ignore[invalid-argument-type]
