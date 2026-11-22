"""The grants of `lakana login` through a browser: loopback and device (#32, PR 3).

Both are Lakana's own grants over the browser session, not the identity provider's, so they work
with every provider and with a passkey session (an unlinked person, the break-glass
administrator).

- **Loopback** (RFC 8252's shape). The command line makes a PKCE verifier and sends the browser
  to the confirmation page with its S256 challenge, its port on ``127.0.0.1`` and its host name.
  The person confirms in a session with a fresh touch (`approve_loopback`), which answers a code:
  32 random bytes, stored as their SHA-256, for `CODE_LIFETIME`, one use. The page sends the
  browser to ``http://127.0.0.1:<port>/`` with the code; the command line exchanges it with the
  verifier (`exchange_loopback`). A code without the right verifier is burnt: another program
  that grabs the code from the loopback port cannot use it.
- **Device** (RFC 8628's shape). The command line starts a grant (`start_device`): a device code
  it keeps and a short user code it shows, both stored as their SHA-256, for `DEVICE_LIFETIME`.
  The person types the user code in a browser, sees the confirmation and approves
  (`approve_device`, fresh touch) or denies it; the command line asks every `POLL_INTERVAL`
  (`poll_device`) and receives the token once.

The token is issued at the exchange, for the person who approved, through
`lakana_core.identity.pats.PatService.issue` with that person's browser session as the actor: a
grant is worth nothing once the session ends, and the passkey touch is measured at the
approval. It is named `lakana login on <host>` (`token_name_for`) and lives `TOKEN_LIFETIME`;
running `lakana login` again makes a new one and never revokes an older one. No secret is ever
written to the audit log.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, Literal

from sqlalchemy import ColumnElement, delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from lakana_core.audit.service import AuditService
from lakana_core.auth import AuthorizationService, PrincipalRef, SessionContext
from lakana_core.errors import (
    AuthenticationError,
    CliGrantRefused,
    CliPollTooFast,
    ConflictError,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from lakana_core.identity.pats import IssuedPat, PatService
from lakana_core.identity.sessions import SessionService, require_step_up, utc_now
from lakana_core.identity.tokens import hash_secret
from lakana_core.models import AuditEventType, CliGrant, PersonalAccessToken, PrincipalType

CODE_LIFETIME = timedelta(minutes=2)
"""How long a loopback code may be exchanged after the confirmation."""

DEVICE_LIFETIME = timedelta(minutes=10)
"""How long a device grant may be approved and polled after its start."""

POLL_INTERVAL = timedelta(seconds=5)
"""How often a command line may ask about its device grant."""

POLL_TOLERANCE = timedelta(seconds=1)
"""How much sooner than `POLL_INTERVAL` a poll may arrive, for the network's jitter."""

TOKEN_LIFETIME = timedelta(days=90)
"""How long the token of `lakana login` works (maintainer, 2026-11-20)."""

PURGE_AFTER = timedelta(days=1)
"""How long a grant's row is kept past its expiry."""

USER_CODE_ALPHABET = "BCDFGHJKLMNPQRSTVWXZ"
"""The letters of a user code: consonants only, so that no word and no 0/O or 1/I is spelt."""

USER_CODE_LENGTH = 8
"""The letters of a user code, shown as ``XXXX-XXXX``."""

HOST_LENGTH = 64
"""The longest host name taken."""

MIN_PORT, MAX_PORT = 1024, 65535
"""The loopback ports a confirmation may send the browser back to."""

CHALLENGE = re.compile(r"^[A-Za-z0-9_-]{43}$")
"""An S256 PKCE challenge: the unpadded base64url of 32 bytes."""

HOST_NAME = re.compile(r"[A-Za-z0-9._-]{1,64}")
"""A host name as `socket.gethostname` gives one: the only names a grant takes."""

FROM_A_BROWSER = (
    "A command-line sign-in is confirmed in the web interface, in your browser: an API token "
    "cannot confirm one. Open the link lakana login shows in a browser where you are signed in."
)
"""The refusal of a token that tries to approve a grant."""

PEOPLE_ONLY = "A command-line sign-in is for people: a service account cannot confirm one."
"""The refusal of a service account."""

EXCHANGE_REFUSED = (
    "This sign-in code is not valid: it was used, it took longer than 2 minutes, it came "
    "without the command line's proof, or the browser session that confirmed it has ended. Run "
    "lakana login again."
)
"""The refusal of a loopback exchange, the same for every reason."""

PEER_LENGTH = 64
"""The longest requesting address kept."""

DEVICE_REFUSED = (
    "This device code is not known: it was used or has expired. Run lakana login --device again."
)
"""The refusal of a poll for an unknown device code."""

SESSION_ENDED = (
    "The browser session that confirmed this sign-in has ended, so no token was created. Sign "
    "in again in the browser and run lakana login again."
)
"""The refusal of an exchange whose approving session is gone."""

WRONG_USER_CODE = (
    "No sign-in is waiting for this code. Check the code lakana login --device shows; it works "
    "for 10 minutes."
)
"""The 404 of a user code that names no open device grant."""

SLOW_DOWN = "Asked too soon: wait 5 seconds between two questions about one device code."
"""The 429 of a poll faster than `POLL_INTERVAL`."""

PollState = Literal["pending", "approved", "denied", "expired"]
"""What a poll answers about a device grant."""


@dataclass(frozen=True)
class DeviceStart:
    """A device grant just started; its codes are shown here and nowhere else.

    Parameters
    ----------
    device_code
        What the command line polls with: 32 random bytes, URL-safe.
    user_code
        What the person types in the browser, as ``XXXX-XXXX``.
    expires_at
        When the grant stops working.
    """

    device_code: str = field(repr=False)
    user_code: str = field(repr=False)
    expires_at: datetime


@dataclass(frozen=True)
class DeviceDescription:
    """What the confirmation of a device grant shows.

    Parameters
    ----------
    host
        The name the command line gave its computer, cleaned.
    requested_from
        The client address that started the grant, as the server saw it.
    requested_at
        When it was started.
    age_seconds
        How long ago, by the server's clock, never below 0.
    """

    host: str
    requested_from: str
    requested_at: datetime
    age_seconds: int


@dataclass(frozen=True)
class DevicePoll:
    """What a poll found about a device grant.

    Parameters
    ----------
    state
        ``pending``, ``approved``, ``denied`` or ``expired``.
    token
        The new token, only with ``approved``, and only once.
    """

    state: PollState
    token: IssuedPat | None = field(default=None, repr=False)


def check_host(host: str) -> None:
    """Refuse a host name that is not one.

    The name is the command line's own word about its computer, shown in the confirmation's
    question and kept in the token's name; only host-name characters may make it up, so it
    cannot write a sentence into the page.

    Parameters
    ----------
    host
        The name the command line sent.

    Raises
    ------
    ValidationError
        Unless it is 1 to `HOST_LENGTH` characters of ``A-Z a-z 0-9 . _ -``.
    """
    if not HOST_NAME.fullmatch(host):
        raise ValidationError(
            f"The computer's name must be 1 to {HOST_LENGTH} characters of A-Z, a-z, 0-9, '.', "
            "'_' and '-', as a host name is. lakana login sends this computer's name; set "
            "another one with the HOSTNAME of the system, or ask your administrator."
        )


def token_name_for(host: str, taken: Callable[[str], bool]) -> str:
    """Name the token of a command-line sign-in.

    Parameters
    ----------
    host
        The host name (`check_host`).
    taken
        Says whether the person already has a working token of a name.

    Returns
    -------
    str
        ``lakana login on <host>``, or with `` (2)``, `` (3)`` and so on when it is taken.
    """
    base = f"lakana login on {host}"
    name, number = base, 1
    while taken(name):
        number += 1
        name = f"{base} ({number})"
    return name


def s256(verifier: str) -> str:
    """Compute the S256 PKCE challenge of a verifier.

    Parameters
    ----------
    verifier
        The verifier.

    Returns
    -------
    str
        The unpadded base64url of the verifier's SHA-256.
    """
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def normalize_user_code(text: str) -> str:
    """Read a user code as a person typed it.

    Parameters
    ----------
    text
        The typed code.

    Returns
    -------
    str
        The code in capitals without dashes and spaces.
    """
    return "".join(char for char in text if char not in "- ").upper()


def _new_user_code() -> str:
    """Draw a user code.

    Returns
    -------
    str
        `USER_CODE_LENGTH` letters of `USER_CODE_ALPHABET`, without the dash.
    """
    return "".join(secrets.choice(USER_CODE_ALPHABET) for _ in range(USER_CODE_LENGTH))


class CliGrantService:
    """Approve, exchange, start and poll the grants of `lakana login` through a browser.

    Parameters
    ----------
    session
        Database session. Each mutating method commits.
    authz
        Authorization service, for issuing the token.
    clock
        The clock; `lakana_core.identity.sessions.utc_now` by default.
    """

    def __init__(
        self,
        session: Session,
        authz: AuthorizationService,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._session = session
        self._authz = authz
        self._clock = clock

    @staticmethod
    def _approver(p: PrincipalRef) -> SessionContext:
        """Read the browser session a person approves in.

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
        PermissionDenied
            For any token (`FROM_A_BROWSER`) and for a service account (`PEOPLE_ONLY`).
        """
        if p.type is PrincipalType.SERVICE_ACCOUNT:
            raise PermissionDenied(PEOPLE_ONLY)
        if p.session is None or p.credential != "session":
            raise PermissionDenied(FROM_A_BROWSER)
        return p.session

    def _claim(
        self, conditions: list[ColumnElement[bool]], values: dict[str, Any]
    ) -> CliGrant | None:
        """Change one unexpired grant with one conditional ``UPDATE``.

        Parameters
        ----------
        conditions
            Conditions on `CliGrant`, besides being unexpired.
        values
            The columns to set.

        Returns
        -------
        CliGrant or None
            The grant this call changed, or `None` when none matched.
        """
        claimed = self._session.execute(
            update(CliGrant)
            .where(*conditions, CliGrant.expires_at > self._clock())
            .values(values)
            .returning(CliGrant.id)
            .execution_options(synchronize_session="fetch")
        ).scalar_one_or_none()
        if claimed is None:
            return None
        return self._session.get(CliGrant, claimed)

    def _audit(
        self, p_id: uuid.UUID | None, kind: AuditEventType, grant: CliGrant, **extra: str
    ) -> None:
        """Record an event about a grant, without any of its secrets.

        Parameters
        ----------
        p_id
            The person; `None` for a refused exchange of a grant nobody approved.
        kind
            The event's type.
        grant
            The grant.
        **extra
            Further fields (``key_id``, ``reason``, ``peer``).
        """
        detail = {"id": str(grant.id), "kind": grant.kind, "host": grant.host, **extra}
        if grant.requested_from is not None:
            # The address that started a device grant: an incident review's first question
            # after a phished code, kept here beyond the row's purge.
            detail["requested_from"] = grant.requested_from
        AuditService(self._session).record(p_id, kind, detail=detail)

    def purge(self, now: datetime) -> int:
        """Delete the grants more than `PURGE_AFTER` past their expiry.

        Parameters
        ----------
        now
            The time to count from.

        Returns
        -------
        int
            How many rows were deleted.
        """
        result = self._session.execute(
            delete(CliGrant)
            .where(CliGrant.expires_at < now - PURGE_AFTER)
            .execution_options(synchronize_session=False)
        )
        return int(getattr(result, "rowcount", 0) or 0)

    def approve_loopback(
        self, p: PrincipalRef, *, port: int, code_challenge: str, host: str
    ) -> str:
        """Confirm a loopback sign-in and answer its one-time code.

        Parameters
        ----------
        p
            The person, in a browser session.
        port
            The command line's port on ``127.0.0.1``.
        code_challenge
            The S256 challenge of the command line's PKCE verifier.
        host
            The command line's host name (`check_host`).

        Returns
        -------
        str
            The code, for `CODE_LIFETIME`, once, and only with the verifier.

        Raises
        ------
        PermissionDenied
            For a token or a service account.
        StepUpRequired
            Without a passkey or provider touch from the last 15 minutes.
        ValidationError
            For a port outside 1024 to 65535, a challenge that is no S256 challenge, or a host
            that is no host name (`check_host`).
        """
        context = self._approver(p)
        require_step_up(p)
        if not MIN_PORT <= port <= MAX_PORT:
            raise ValidationError(
                f"The port must be between {MIN_PORT} and {MAX_PORT} (got {port}): lakana login "
                "listens on such a port of 127.0.0.1."
            )
        check_host(host)
        if not CHALLENGE.match(code_challenge):
            raise ValidationError(
                "code_challenge must be the S256 challenge of a PKCE verifier: 43 characters of "
                "A-Z, a-z, 0-9, - and _."
            )
        now = self._clock()
        self.purge(now)
        code = secrets.token_urlsafe(32)
        grant = CliGrant(
            id=uuid.uuid4(),
            kind="loopback",
            host=host,
            code_hash=hash_secret(code),
            code_challenge=code_challenge,
            port=port,
            principal_id=p.id,
            session_id=context.session_id,
            created_at=now,
            expires_at=now + CODE_LIFETIME,
            approved_at=now,
        )
        self._session.add(grant)
        self._audit(p.id, AuditEventType.SIGN_IN_CLI_APPROVED, grant)
        self._session.commit()
        return code

    def exchange_loopback(
        self, code: str, code_verifier: str, *, peer: str | None = None
    ) -> IssuedPat:
        """Exchange a loopback code and its verifier for the new token, once.

        The code is used up by the first exchange, right or wrong.

        Parameters
        ----------
        code
            The code the browser brought to the loopback port.
        code_verifier
            The command line's PKCE verifier.
        peer
            The client address that exchanges, recorded with the token.

        Returns
        -------
        IssuedPat
            The new token, shown here only.

        Raises
        ------
        CliGrantRefused
            For an unknown, used or expired code, a wrong verifier, or an approving session
            that has ended.
        """
        claimed = None
        if code:
            claimed = self._claim(
                [
                    CliGrant.kind == "loopback",
                    CliGrant.code_hash == hash_secret(code),
                    CliGrant.used_at.is_(None),
                ],
                {"used_at": self._clock()},
            )
        if claimed is None:
            self._refused_code(code)
            self._session.commit()
            raise CliGrantRefused(EXCHANGE_REFUSED)
        self._session.commit()
        if not hmac.compare_digest(
            s256(code_verifier).encode("ascii"), (claimed.code_challenge or "").encode("ascii")
        ):
            self._audit_refusal(claimed, "wrong_verifier")
            raise CliGrantRefused(EXCHANGE_REFUSED)
        return self._issue(claimed, refusal=EXCHANGE_REFUSED, peer=peer)

    def _audit_refusal(self, grant: CliGrant, reason: str) -> None:
        """Record the first refused exchange of a grant that exists, and commit.

        One conditional ``UPDATE ... WHERE refused_at IS NULL`` decides, so a code replayed any
        number of times writes one row: the first refusal is the signal; the rest are noise.

        Parameters
        ----------
        grant
            The grant.
        reason
            ``wrong_verifier``, ``used``, ``expired`` or ``session_ended``.
        """
        first = self._session.execute(
            update(CliGrant)
            .where(CliGrant.id == grant.id, CliGrant.refused_at.is_(None))
            .values(refused_at=self._clock())
            .returning(CliGrant.id)
            .execution_options(synchronize_session="fetch")
        ).scalar_one_or_none()
        if first is not None:
            self._audit(
                grant.principal_id, AuditEventType.SIGN_IN_CLI_REFUSED, grant, reason=reason
            )
        self._session.commit()

    def _refused_code(self, code: str) -> None:
        """Record why a code that names a grant could not be exchanged; an unknown one is not.

        At most one row per grant (`_audit_refusal`): replaying a used or expired code writes
        nothing more, and an unknown code nothing at all.

        Parameters
        ----------
        code
            The code presented.
        """
        if not code:
            return
        grant = self._session.execute(
            select(CliGrant).where(
                CliGrant.kind == "loopback", CliGrant.code_hash == hash_secret(code)
            )
        ).scalar_one_or_none()
        if grant is not None:
            self._audit_refusal(grant, "used" if grant.used_at is not None else "expired")

    def _name_taken(self, principal_id: uuid.UUID, name: str) -> bool:
        """Say whether a person has a working token of a name.

        Parameters
        ----------
        principal_id
            The person.
        name
            The name.

        Returns
        -------
        bool
            `True` when a token of theirs of that name is neither revoked nor expired.
        """
        now = self._clock()
        return (
            self._session.execute(
                select(PersonalAccessToken.id).where(
                    PersonalAccessToken.principal_id == principal_id,
                    PersonalAccessToken.name == name,
                    PersonalAccessToken.revoked_at.is_(None),
                    (PersonalAccessToken.expires_at.is_(None))
                    | (PersonalAccessToken.expires_at > now),
                )
            ).first()
            is not None
        )

    def _approving(self, grant: CliGrant) -> PrincipalRef | None:
        """Read the browser session that approved a grant again, as a request would.

        Parameters
        ----------
        grant
            The grant.

        Returns
        -------
        PrincipalRef or None
            The person in that session; `None` when it has ended, idled out, its person was
            deactivated, or it is gone.
        """
        if grant.session_id is None:
            return None
        try:
            return SessionService(self._session, self._authz, clock=self._clock).verify_by_id(
                grant.session_id
            )
        except AuthenticationError:
            return None

    def _issue(
        self, grant: CliGrant, *, refusal: str = SESSION_ENDED, peer: str | None = None
    ) -> IssuedPat:
        """Issue the token of a grant just used, as the person in the session that approved it.

        Parameters
        ----------
        grant
            The grant, approved and now used.
        refusal
            The sentence of the refusal when the approving session has ended.
        peer
            The client address that collects the token, for the audit; `None` for none.

        Returns
        -------
        IssuedPat
            The new token.

        Raises
        ------
        CliGrantRefused
            When the approving session has ended, with `refusal`; audited as
            ``session_ended``.
        """
        approved_at = grant.approved_at
        ref = self._approving(grant)
        if ref is None or approved_at is None:
            self._audit_refusal(grant, "session_ended")
            raise CliGrantRefused(refusal)
        assert ref.session is not None  # nosec B101 -- a verified session's reference has it
        # The touch was checked at the approval: measure it there, not minutes later.
        ref = replace(ref, session=replace(ref.session, verified_at=approved_at))
        name = token_name_for(grant.host, lambda taken: self._name_taken(ref.id, taken))
        issued = PatService(self._session, self._authz).issue(
            ref, ref.name, name=name, expires_at=self._clock() + TOKEN_LIFETIME
        )
        grant.pat_id = self._session.execute(
            select(PersonalAccessToken.id).where(PersonalAccessToken.key_id == issued.key_id)
        ).scalar_one()
        extra = {"key_id": issued.key_id} | ({} if peer is None else {"peer": peer[:PEER_LENGTH]})
        self._audit(ref.id, AuditEventType.SIGN_IN_CLI_TOKEN_ISSUED, grant, **extra)
        self._session.commit()
        return issued

    def start_device(self, host: str, *, peer: str) -> DeviceStart:
        """Start a device grant for a command line without a browser.

        Parameters
        ----------
        host
            The command line's host name (`check_host`).
        peer
            The client address that asks, as the server sees it; kept for the confirmation, cut
            to `PEER_LENGTH` characters.

        Returns
        -------
        DeviceStart
            The device code to poll with and the user code to show.

        Raises
        ------
        ValidationError
            For a host that is no host name (`check_host`).
        ConflictError
            When three user codes in a row were taken (astronomically rare).
        """
        check_host(host)
        now = self._clock()
        self.purge(now)
        self._session.commit()
        for _ in range(3):
            device_code, user_code = secrets.token_urlsafe(32), _new_user_code()
            self._session.add(
                CliGrant(
                    id=uuid.uuid4(),
                    kind="device",
                    host=host,
                    device_code_hash=hash_secret(device_code),
                    user_code_hash=hash_secret(user_code),
                    requested_from=peer[:PEER_LENGTH],
                    created_at=now,
                    expires_at=now + DEVICE_LIFETIME,
                )
            )
            try:
                self._session.commit()
            except IntegrityError:
                self._session.rollback()
                continue
            return DeviceStart(
                device_code=device_code,
                user_code=f"{user_code[:4]}-{user_code[4:]}",
                expires_at=now + DEVICE_LIFETIME,
            )
        raise ConflictError("Could not draw a free device code; try again.")

    def _waiting(self, user_code: str) -> list[ColumnElement[bool]]:
        """Name the device grant a user code opens, while it waits for a decision.

        Parameters
        ----------
        user_code
            The code as typed.

        Returns
        -------
        list of ColumnElement
            The conditions on `CliGrant`.
        """
        return [
            CliGrant.kind == "device",
            CliGrant.user_code_hash == hash_secret(normalize_user_code(user_code)),
            CliGrant.approved_at.is_(None),
            CliGrant.denied_at.is_(None),
        ]

    def describe_device(self, p: PrincipalRef, user_code: str) -> DeviceDescription:
        """Describe the sign-in a user code would approve, for the confirmation.

        Parameters
        ----------
        p
            The person, in a browser session.
        user_code
            The code as typed: letters in any case, with or without the dash.

        Returns
        -------
        DeviceDescription
            The command line's host name, the address it asked from and when.

        Raises
        ------
        PermissionDenied
            For a token or a service account.
        NotFoundError
            When no unexpired grant waits for this code (`WRONG_USER_CODE`).
        """
        self._approver(p)
        grant = self._session.execute(
            select(CliGrant).where(*self._waiting(user_code), CliGrant.expires_at > self._clock())
        ).scalar_one_or_none()
        if grant is None:
            raise NotFoundError(WRONG_USER_CODE)
        age = self._clock() - grant.created_at
        return DeviceDescription(
            host=grant.host,
            requested_from=grant.requested_from or "?",
            requested_at=grant.created_at,
            age_seconds=max(0, int(age.total_seconds())),
        )

    def approve_device(self, p: PrincipalRef, user_code: str) -> None:
        """Approve a device grant: its command line receives the token at its next poll.

        Parameters
        ----------
        p
            The person, in a browser session.
        user_code
            The code as typed.

        Raises
        ------
        PermissionDenied
            For a token or a service account.
        StepUpRequired
            Without a passkey or provider touch from the last 15 minutes.
        NotFoundError
            When no unexpired grant waits for this code.
        """
        context = self._approver(p)
        require_step_up(p)
        grant = self._claim(
            self._waiting(user_code),
            {
                "approved_at": self._clock(),
                "principal_id": p.id,
                "session_id": context.session_id,
            },
        )
        if grant is None:
            self._session.commit()
            raise NotFoundError(WRONG_USER_CODE)
        self._audit(p.id, AuditEventType.SIGN_IN_CLI_APPROVED, grant)
        self._session.commit()

    def deny_device(self, p: PrincipalRef, user_code: str) -> None:
        """Refuse a device grant: its command line is told at its next poll.

        Parameters
        ----------
        p
            The person, in a browser session.
        user_code
            The code as typed.

        Raises
        ------
        PermissionDenied
            For a token or a service account.
        NotFoundError
            When no unexpired grant waits for this code.
        """
        self._approver(p)
        grant = self._claim(self._waiting(user_code), {"denied_at": self._clock()})
        if grant is None:
            self._session.commit()
            raise NotFoundError(WRONG_USER_CODE)
        self._audit(p.id, AuditEventType.SIGN_IN_CLI_DENIED, grant)
        self._session.commit()

    def poll_device(self, device_code: str, *, peer: str | None = None) -> DevicePoll:
        """Ask about a device grant; an approved one answers its token, once.

        Parameters
        ----------
        device_code
            The device code `start_device` answered.
        peer
            The client address that polls, recorded with the token it collects.

        Returns
        -------
        DevicePoll
            ``pending`` while nobody decided; ``approved`` with the token, once; ``denied``;
            ``expired`` past `DEVICE_LIFETIME` and after the token was handed out.

        Raises
        ------
        CliGrantRefused
            For a device code that names no grant (`DEVICE_REFUSED`), or an approving session
            that has ended.
        CliPollTooFast
            Sooner than `POLL_INTERVAL` after the last accepted poll (`SLOW_DOWN`); such a poll
            writes nothing.
        """
        grant = None
        if device_code:
            grant = self._session.execute(
                select(CliGrant).where(
                    CliGrant.kind == "device",
                    CliGrant.device_code_hash == hash_secret(device_code),
                )
            ).scalar_one_or_none()
        if grant is None:
            raise CliGrantRefused(DEVICE_REFUSED)
        now = self._clock()
        previous = grant.last_polled_at
        # A poll too soon writes nothing (RFC 8628's slow_down: the interval counts from the last
        # accepted poll), so a fast poller cannot drive a commit per request.
        if previous is not None and now - previous < POLL_INTERVAL - POLL_TOLERANCE:
            raise CliPollTooFast(SLOW_DOWN)
        grant.last_polled_at = now
        self._session.commit()
        state = self._state(grant, now)
        if state != "approved":
            return DevicePoll(state=state)
        used = self._claim(
            [CliGrant.id == grant.id, CliGrant.used_at.is_(None)], {"used_at": self._clock()}
        )
        self._session.commit()
        if used is None:
            return DevicePoll(state="expired")
        return DevicePoll(state="approved", token=self._issue(used, peer=peer))

    @staticmethod
    def _state(grant: CliGrant, now: datetime) -> PollState:
        """Read where a device grant stands.

        Parameters
        ----------
        grant
            The grant.
        now
            The time.

        Returns
        -------
        str
            ``expired`` once used or past its expiry, ``denied``, ``approved`` or ``pending``.
        """
        if grant.used_at is not None or grant.expires_at <= now:
            return "expired"
        if grant.denied_at is not None:
            return "denied"
        if grant.approved_at is not None:
            return "approved"
        return "pending"
