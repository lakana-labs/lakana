"""Browser sessions: the web interface's credential, their limits, and step-up.

A session is started by a sign-in (a link or a passkey) and sent to the browser as an
HttpOnly cookie ``<key_id>_<secret>``. Only the secret's SHA-256 digest is stored, and the
presented secret is compared in constant time (`lakana_core.identity.tokens.verify_secret`).
The CSRF token is a second, random secret of the session, unrelated to the cookie: the
sign-in that starts the session returns it once, the page keeps it in its origin's storage,
and every cookie request, reads too, carries it (`check_csrf`, against `csrf_hash`, in constant
time). Whoever holds only the cookie, such as another program on another `localhost` port
(cookies do not separate ports), therefore cannot act as the person.

The row is read on every request (`SessionService.verify`), so ending a session takes effect
at once. A session ends when it is signed out, ended by an administrator, idle for longer than
its idle timeout, older than its absolute lifetime, when its person is deactivated, or when the
token it was created through is revoked or expires. Only requests the person started extend
the idle timeout; background polls (`extend=False`) never do.
"""

from __future__ import annotations

import re
import secrets
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import ColumnElement, select, update
from sqlalchemy.orm import Session

from lakana_core.audit.service import AuditService
from lakana_core.auth import (
    Action,
    AuthorizationService,
    PrincipalRef,
    Securable,
    SessionContext,
)
from lakana_core.errors import (
    AuthenticationError,
    NotFoundError,
    PermissionDenied,
    SessionEndedError,
    StepUpRequired,
    ValidationError,
)
from lakana_core.grants.admin import acts_as_instance_admin
from lakana_core.identity.tokens import hash_secret, verify_secret
from lakana_core.models import (
    AuditEventType,
    BrowserSession,
    InstanceSetting,
    PersonalAccessToken,
    Principal,
    PrincipalType,
)


@dataclass(frozen=True)
class SessionLimits:
    """How long a session lasts.

    Parameters
    ----------
    idle
        How long a session lasts without a request the person started.
    absolute
        How long a session lasts at most, however active.
    """

    idle: timedelta
    absolute: timedelta


@dataclass(frozen=True)
class SessionBounds:
    """The limits an administrator may choose between.

    Parameters
    ----------
    idle_min
        The shortest idle timeout.
    idle_max
        The longest idle timeout.
    absolute_min
        The shortest absolute lifetime.
    absolute_max
        The longest absolute lifetime.
    """

    idle_min: timedelta
    idle_max: timedelta
    absolute_min: timedelta
    absolute_max: timedelta


SESSION_DEFAULTS = SessionLimits(idle=timedelta(hours=12), absolute=timedelta(days=7))
"""The limits of an instance whose administrator changed none."""

SESSION_BOUNDS = SessionBounds(
    idle_min=timedelta(minutes=15),
    idle_max=timedelta(hours=24),
    absolute_min=timedelta(hours=1),
    absolute_max=timedelta(days=30),
)
"""The bounds `SessionService.set_limits` checks; the idle timeout may not exceed the lifetime."""

STEP_UP_WINDOW = timedelta(minutes=15)
"""How old a passkey touch may be for an action that needs step-up."""

LAST_SEEN_WRITE_INTERVAL = timedelta(seconds=60)
"""How often a session's `last_seen_at` is written at most, so a request costs no write."""

IDLE_KEY = "sign_in.idle_seconds"
"""The instance setting that holds the idle timeout, in seconds."""

ABSOLUTE_KEY = "sign_in.absolute_seconds"
"""The instance setting that holds the absolute lifetime, in seconds."""

VIAS = frozenset({"setup", "invite", "recovery", "local", "passkey"})
"""How a session may begin."""

STEP_UP_MESSAGE = (
    "This needs a fresh passkey touch (within 15 minutes). Confirm with your passkey and try again."
)

_COOKIE = re.compile(r"([0-9a-f]{32})_(.+)", re.DOTALL)


def utc_now() -> datetime:
    """Read the clock the sign-in services use by default.

    Returns
    -------
    datetime
        The current time, aware, in UTC.
    """
    return datetime.now(UTC)


@dataclass(frozen=True)
class StartedSession:
    """A session just started; the cookie value is shown here and nowhere else.

    Parameters
    ----------
    key_id
        The public lookup key: 32 hexadecimal characters, no ``_``.
    cookie_value
        ``<key_id>_<secret>``, the cookie's value. Left out of the `repr`.
    csrf_token
        The session's CSRF token: random, stored only as `csrf_hash`, shown here and nowhere
        else. Left out of the `repr`.
    principal
        The principal, as `SessionService.verify` would return it now.
    absolute_expires_at
        When the session ends at the latest; the cookie's expiry.
    """

    key_id: str
    cookie_value: str = field(repr=False)
    csrf_token: str = field(repr=False)
    principal: PrincipalRef
    absolute_expires_at: datetime


def _split(cookie_value: str) -> tuple[str, str] | None:
    """Split a cookie value into its key id and secret.

    Parameters
    ----------
    cookie_value
        The presented value.

    Returns
    -------
    tuple of (str, str), or None
        ``(key_id, secret)``, or `None` when the value does not have that shape.
    """
    match = _COOKIE.fullmatch(cookie_value)
    return (match.group(1), match.group(2)) if match else None


def require_step_up(p: PrincipalRef, *, now: datetime | None = None) -> None:
    """Refuse an action unless the session's last passkey touch is fresh.

    A caller authenticated with a token has no session and passes: step-up guards what a
    browser can do, and a token holder could do the same with the CLI.

    Parameters
    ----------
    p
        The acting principal.
    now
        The time to measure against; by default the time the session was verified for this
        request (`SessionContext.verified_at`, the session service's clock), so services that
        pass no time and those that pass their clock agree.

    Raises
    ------
    StepUpRequired
        If `p` has a session whose last passkey touch is missing or older than
        `STEP_UP_WINDOW`.
    """
    if p.session is None:
        return
    touched = p.session.last_touch_at
    current = now if now is not None else p.session.verified_at
    if touched is None or current - touched > STEP_UP_WINDOW:
        raise StepUpRequired(STEP_UP_MESSAGE)


def _seconds(value: timedelta) -> int:
    """Count the whole seconds of a duration.

    Parameters
    ----------
    value
        The duration.

    Returns
    -------
    int
        Its seconds.
    """
    return int(value.total_seconds())


def _describe(value: timedelta) -> str:
    """Write a bound as a person reads it.

    Parameters
    ----------
    value
        A bound.

    Returns
    -------
    str
        ``15 minutes``, ``1 hour``, ``24 hours``, ``30 days``: whole days from two days on.
    """
    seconds = _seconds(value)
    for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if seconds % size == 0 and (seconds > size or unit != "day"):
            count = seconds // size
            return f"{count} {unit}{'' if count == 1 else 's'}"
    return f"{seconds} seconds"


def check_limits(limits: SessionLimits) -> None:
    """Refuse limits outside `SESSION_BOUNDS`, naming the bound.

    Parameters
    ----------
    limits
        The proposed limits.

    Raises
    ------
    ValidationError
        If the idle timeout or the lifetime lies outside its bounds, or the idle timeout is
        longer than the lifetime.
    """
    b = SESSION_BOUNDS
    checks = (
        (limits.idle < b.idle_min, f"The idle timeout must be at least {_describe(b.idle_min)}"),
        (limits.idle > b.idle_max, f"The idle timeout must be at most {_describe(b.idle_max)}"),
        (
            limits.absolute < b.absolute_min,
            f"The absolute lifetime must be at least {_describe(b.absolute_min)}",
        ),
        (
            limits.absolute > b.absolute_max,
            f"The absolute lifetime must be at most {_describe(b.absolute_max)}",
        ),
        (
            limits.idle > limits.absolute,
            "The idle timeout must not be longer than the absolute lifetime",
        ),
    )
    for failed, message in checks:
        if failed:
            raise ValidationError(
                f"{message}; got idle {_describe(limits.idle)} and absolute lifetime "
                f"{_describe(limits.absolute)}. Choose values within the limits and try again."
            )


class SessionService:
    """Start, verify, list and end browser sessions, and keep their limits.

    Parameters
    ----------
    session
        Database session. `start`, `touch` and `end_for_principal` (by default) leave their
        changes for the caller's commit; every other mutating method commits.
    authz
        Authorization service, asked before an administrator acts on another person's
        sessions or changes the limits.
    clock
        The clock; `utc_now` by default.
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

    def _require_admin(self, p: PrincipalRef) -> None:
        """Refuse unless `p` is an instance administrator.

        Parameters
        ----------
        p
            The acting principal.

        Raises
        ------
        PermissionDenied
            If `authz` refuses `MANAGE_PRINCIPAL` at instance scope, or `p` does not hold the
            `ADMIN` role there.
        """
        self._authz.authorize(p, Action.MANAGE_PRINCIPAL, Securable.instance_scope())
        if acts_as_instance_admin(self._session, self._authz, p):
            return
        raise PermissionDenied(
            "Only an administrator (the ADMIN role on the instance) can do this for another "
            "person. Ask an administrator."
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

    def _end_rows(
        self,
        condition: ColumnElement[bool],
        reason: str,
        *,
        actor: PrincipalRef | None = None,
        subject: str | None = None,
    ) -> int:
        """End every open session that matches, each with one audit event.

        One conditional ``UPDATE … WHERE ended_at IS NULL … RETURNING``: a session two
        callers end at once is ended, and audited, by exactly one of them.

        Parameters
        ----------
        condition
            A SQL condition on `BrowserSession`.
        reason
            The stored `end_reason`.
        actor
            The administrator ending another person's sessions: each event is then
            attributed to them, with the person in ``subject``. `None` attributes each event
            to the session's own person.
        subject
            The name of the person whose sessions end; required with `actor`.

        Returns
        -------
        int
            How many sessions this call ended.
        """
        ended = self._session.execute(
            update(BrowserSession)
            .where(condition, BrowserSession.ended_at.is_(None))
            .values(ended_at=self._clock(), end_reason=reason)
            .returning(BrowserSession.key_id, BrowserSession.principal_id)
            .execution_options(synchronize_session="fetch")
        ).all()
        audit = AuditService(self._session)
        for key_id, principal_id in sorted(ended):
            if actor is None:
                audit.record(
                    principal_id,
                    AuditEventType.SESSION_ENDED,
                    detail={"key_id": key_id, "reason": reason},
                )
            else:
                audit.record(
                    actor.id,
                    AuditEventType.SESSION_ENDED,
                    detail={
                        "key_id": key_id,
                        "reason": reason,
                        "actor": actor.name,
                        "subject": subject,
                    },
                )
        return len(ended)

    def get_limits(self) -> SessionLimits:
        """Read the instance's session limits.

        Returns
        -------
        SessionLimits
            The stored limits; `SESSION_DEFAULTS` for each one never set.
        """
        stored = dict(
            self._session.execute(
                select(InstanceSetting.key, InstanceSetting.value).where(
                    InstanceSetting.key.in_([IDLE_KEY, ABSOLUTE_KEY])
                )
            ).all()
        )
        idle = stored.get(IDLE_KEY)
        absolute = stored.get(ABSOLUTE_KEY)
        return SessionLimits(
            idle=timedelta(seconds=idle) if isinstance(idle, int) else SESSION_DEFAULTS.idle,
            absolute=(
                timedelta(seconds=absolute)
                if isinstance(absolute, int)
                else SESSION_DEFAULTS.absolute
            ),
        )

    def set_limits(
        self, p: PrincipalRef, *, idle: timedelta | None, absolute: timedelta | None
    ) -> SessionLimits:
        """Change the session limits; sessions already started keep theirs.

        Parameters
        ----------
        p
            The acting principal: an instance administrator, with a fresh passkey touch when
            acting from a browser session.
        idle
            The new idle timeout; `None` keeps the current one.
        absolute
            The new absolute lifetime; `None` keeps the current one.

        Returns
        -------
        SessionLimits
            The limits now in force for new sessions.

        Raises
        ------
        PermissionDenied
            If `p` is not an instance administrator.
        StepUpRequired
            If `p`'s session needs a fresh passkey touch.
        ValidationError
            If a limit lies outside `SESSION_BOUNDS` (the message names the bound).
        """
        self._require_admin(p)
        require_step_up(p, now=self._clock())
        before = self.get_limits()
        after = SessionLimits(
            idle=idle if idle is not None else before.idle,
            absolute=absolute if absolute is not None else before.absolute,
        )
        check_limits(after)
        now = self._clock()
        for key, value in ((IDLE_KEY, after.idle), (ABSOLUTE_KEY, after.absolute)):
            row = self._session.get(InstanceSetting, key)
            if row is None:
                row = InstanceSetting(key=key)
                self._session.add(row)
            row.value = _seconds(value)
            row.updated_at = now
            row.updated_by = p.id
        AuditService(self._session).record(
            p.id,
            AuditEventType.SIGN_IN_SETTINGS_CHANGED,
            securable=Securable.instance_scope(),
            detail={
                "before": {
                    "idle_seconds": _seconds(before.idle),
                    "absolute_seconds": _seconds(before.absolute),
                },
                "after": {
                    "idle_seconds": _seconds(after.idle),
                    "absolute_seconds": _seconds(after.absolute),
                },
            },
        )
        self._session.commit()
        return after

    def _find(self, cookie_value: str) -> BrowserSession | None:
        """Find the session a cookie names, if its secret matches.

        Parameters
        ----------
        cookie_value
            The presented value.

        Returns
        -------
        BrowserSession or None
            The row, ended or not; `None` for a malformed value, an unknown key id or a wrong
            secret.
        """
        parts = _split(cookie_value)
        if parts is None:
            return None
        key_id, secret = parts
        row = self._session.execute(
            select(BrowserSession).where(BrowserSession.key_id == key_id)
        ).scalar_one_or_none()
        if row is None or not verify_secret(secret, row.secret_hash):
            return None
        return row

    def start(
        self,
        principal_id: uuid.UUID,
        *,
        via: str,
        link_id: uuid.UUID | None = None,
        source_pat_id: uuid.UUID | None = None,
        replacing: str | None,
        peer: str,
        user_agent: str,
        touched: bool,
    ) -> StartedSession:
        """Start a session with a fresh id; the caller commits.

        Never adopts an id the browser presented: `replacing`, when it names a valid session,
        is ended with reason ``replaced`` (session fixation).

        Parameters
        ----------
        principal_id
            The person signing in.
        via
            How: ``setup``, ``invite``, ``recovery``, ``local`` or ``passkey``.
        link_id
            The link used, if any.
        source_pat_id
            The personal access token a local link was minted with; the session ends when that
            token is revoked or expires.
        replacing
            The session cookie the browser presented, if any.
        peer
            The client's address, as the server sees it.
        user_agent
            The browser's `User-Agent`, cut to 255 characters.
        touched
            Whether the sign-in included a passkey touch (it then counts for step-up).

        Returns
        -------
        StartedSession
            The new session, with its cookie value and CSRF token.

        Raises
        ------
        ValueError
            If `via` is not one of `VIAS`: a programming error.
        NotFoundError
            If no principal has `principal_id`.
        PermissionDenied
            If the principal is a service account, is deactivated, or `source_pat_id` is not
            an unrevoked token of the principal's.
        """
        if via not in VIAS:
            raise ValueError(f"via must be one of {sorted(VIAS)}, got {via!r}")
        principal = self._session.get(Principal, principal_id)
        if principal is None:
            raise NotFoundError("No such principal.")
        if principal.type != PrincipalType.USER:
            raise PermissionDenied(
                f"{principal.name!r} is a service account, and service accounts cannot sign in "
                "to the web interface. Use its API token with the CLI or the SDK."
            )
        if not principal.is_active:
            raise PermissionDenied(
                f"The account {principal.name!r} is deactivated. Ask an administrator to "
                "reactivate it."
            )
        now = self._clock()
        if source_pat_id is not None:
            pat = self._session.get(PersonalAccessToken, source_pat_id)
            if pat is None or pat.principal_id != principal.id or not _pat_valid(pat, now):
                raise PermissionDenied(
                    "This token cannot start a session for this account: it belongs to another "
                    "account, or it was revoked or expired."
                )
        if replacing:
            presented = self._find(replacing)
            if presented is not None:
                self._end_rows(BrowserSession.id == presented.id, "replaced")
        limits = self.get_limits()
        key_id = secrets.token_hex(16)
        secret = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        row = BrowserSession(
            id=uuid.uuid4(),
            key_id=key_id,
            secret_hash=hash_secret(secret),
            csrf_hash=hash_secret(csrf_token),
            principal_id=principal.id,
            via=via,
            link_id=link_id,
            source_pat_id=source_pat_id,
            created_at=now,
            last_seen_at=now,
            idle_seconds=_seconds(limits.idle),
            absolute_expires_at=now + limits.absolute,
            last_touch_at=now if touched else None,
            peer=peer[:64],
            user_agent=user_agent[:255],
        )
        self._session.add(row)
        AuditService(self._session).record(
            principal.id,
            AuditEventType.SESSION_STARTED,
            detail={
                "key_id": key_id,
                "via": via,
                "link_id": str(link_id) if link_id is not None else None,
                "peer": row.peer,
                "user_agent": row.user_agent,
            },
        )
        self._session.flush()
        cookie_value = f"{key_id}_{secret}"
        return StartedSession(
            key_id=key_id,
            cookie_value=cookie_value,
            csrf_token=csrf_token,
            principal=_ref(principal, row, now),
            absolute_expires_at=row.absolute_expires_at,
        )

    def verify(self, cookie_value: str, *, extend: bool) -> PrincipalRef:
        """Resolve a session cookie to its principal, or say why it no longer works.

        Parameters
        ----------
        cookie_value
            The presented cookie value.
        extend
            Whether the request was started by the person (it then extends the idle timeout);
            `False` for a background poll.

        Returns
        -------
        PrincipalRef
            The principal, with ``credential="session"`` and the session's context.

        Raises
        ------
        AuthenticationError
            If the value names no session or its secret does not match.
        SessionEndedError
            If the session has ended, or ends now: with the stored reason, or ``lifetime``,
            ``idle``, ``deactivated`` or ``token_revoked``.
        """
        row = self._find(cookie_value)
        if row is None:
            raise AuthenticationError("This session is not known. Sign in again.")
        if row.ended_at is not None:
            raise SessionEndedError(row.end_reason or "ended")
        now = self._clock()
        principal = self._session.get(Principal, row.principal_id)
        reason: str | None = None
        if now >= row.absolute_expires_at:
            reason = "lifetime"
        elif now >= row.last_seen_at + timedelta(seconds=row.idle_seconds):
            reason = "idle"
        elif principal is None or not principal.is_active:
            reason = "deactivated"
        elif not self._source_valid(row, now):
            reason = "token_revoked"
        if reason is not None:
            self._end_rows(BrowserSession.id == row.id, reason)
            self._session.commit()
            # A concurrent end may have won the update; name the reason it stored.
            raise SessionEndedError(self._stored_reason(row.id) or reason)
        assert principal is not None  # nosec B101 -- refused above when missing
        if extend and now - row.last_seen_at >= LAST_SEEN_WRITE_INTERVAL:
            row.last_seen_at = now
            self._session.commit()
        return _ref(principal, row, now)

    def _stored_reason(self, row_id: uuid.UUID) -> str | None:
        """Read the reason a session ended with.

        Parameters
        ----------
        row_id
            The session's id.

        Returns
        -------
        str or None
            The stored `end_reason`.
        """
        return self._session.execute(
            select(BrowserSession.end_reason).where(BrowserSession.id == row_id)
        ).scalar_one()

    def check_csrf(self, cookie_value: str, token: str) -> bool | None:
        """Say whether a request's CSRF token belongs to the session its cookie names.

        Reads the row but changes nothing, so a refused request never extends the session.

        Parameters
        ----------
        cookie_value
            The session cookie's value.
        token
            The token the request carried (``""`` for none).

        Returns
        -------
        bool or None
            `True` if `token` hashes to the session's `csrf_hash` (compared in constant time),
            `False` if it does not; `None` when the cookie names no session (malformed, unknown
            key id or wrong secret), which `verify` then reports.
        """
        row = self._find(cookie_value)
        if row is None:
            return None
        return verify_secret(token, row.csrf_hash)

    def touch(self, key_id: str) -> None:
        """Record a passkey touch in an open session; the caller commits.

        Parameters
        ----------
        key_id
            The session's key id.

        Raises
        ------
        NotFoundError
            If no open session has that key id.
        """
        row = self._session.execute(
            select(BrowserSession).where(
                BrowserSession.key_id == key_id, BrowserSession.ended_at.is_(None)
            )
        ).scalar_one_or_none()
        if row is None:
            raise NotFoundError("This session has ended. Sign in again.")
        row.last_touch_at = self._clock()

    def end(self, p: PrincipalRef, key_id: str, *, reason: str = "sign_out") -> None:
        """End one session: one's own, or, for an administrator, anyone's.

        Ending a session that has already ended does nothing.

        Parameters
        ----------
        p
            The acting principal.
        key_id
            The session's key id.
        reason
            The reason stored for one's own session; an administrator ending another
            person's session always stores ``ended_by_admin``.

        Raises
        ------
        NotFoundError
            If no session has that key id, or it is another person's and `p` is not an
            administrator (so nobody learns of other people's sessions).
        StepUpRequired
            If an administrator ends another person's session without a fresh passkey touch.
        """
        row = self._session.execute(
            select(BrowserSession).where(BrowserSession.key_id == key_id)
        ).scalar_one_or_none()
        missing = NotFoundError(
            f"No session {key_id!r}. Check the key id against the list of open sessions."
        )
        if row is None:
            raise missing
        if row.principal_id == p.id:
            self._end_rows(BrowserSession.id == row.id, reason)
        else:
            try:
                self._require_admin(p)
            except PermissionDenied:
                raise missing from None
            require_step_up(p, now=self._clock())
            owner = self._session.get(Principal, row.principal_id)
            self._end_rows(
                BrowserSession.id == row.id,
                "ended_by_admin",
                actor=p,
                subject=owner.name if owner is not None else None,
            )
        self._session.commit()

    def end_all(self, p: PrincipalRef, principal_name: str, *, reason: str | None = None) -> int:
        """End every open session of a person.

        Parameters
        ----------
        p
            The acting principal: the person themselves, or an administrator with a fresh
            passkey touch.
        principal_name
            Whose sessions.
        reason
            The reason stored when `p` ends their own sessions; ``sign_out_everywhere`` by
            default. An administrator ending another person's always stores
            ``ended_by_admin``.

        Returns
        -------
        int
            How many sessions ended.

        Raises
        ------
        NotFoundError
            If no principal has `principal_name` (asked only of an administrator).
        PermissionDenied
            If `p` is another person and not an administrator, whether or not the name exists.
        StepUpRequired
            If an administrator acts without a fresh passkey touch.
        """
        if principal_name != p.name:
            # Before the lookup, so a non-administrator cannot tell which names exist.
            self._require_admin(p)
            require_step_up(p, now=self._clock())
        target = self._principal_named(principal_name)
        condition = BrowserSession.principal_id == target.id
        if target.id == p.id:
            count = self._end_rows(condition, reason or "sign_out_everywhere")
        else:
            count = self._end_rows(condition, "ended_by_admin", actor=p, subject=target.name)
        self._session.commit()
        return count

    def end_for_principal(
        self, principal_id: uuid.UUID, reason: str, *, commit: bool = False
    ) -> int:
        """End every open session of a principal, without asking who acts.

        For the services that decide that themselves: deactivation, recovery, reactivation,
        and an administrator removing a person's passkeys.

        Parameters
        ----------
        principal_id
            Whose sessions.
        reason
            The stored reason.
        commit
            Whether to commit; by default the caller does, with its own change.

        Returns
        -------
        int
            How many sessions ended.
        """
        count = self._end_rows(BrowserSession.principal_id == principal_id, reason)
        if commit:
            self._session.commit()
        return count

    def list_sessions(self, p: PrincipalRef, principal_name: str) -> list[BrowserSession]:
        """List a person's open sessions, oldest first.

        A session past its idle timeout or lifetime, one whose token was revoked or expired,
        and every session of a deactivated person are left out even before a request ends
        them.

        Parameters
        ----------
        p
            The acting principal: the person themselves, or an administrator.
        principal_name
            Whose sessions.

        Returns
        -------
        list of BrowserSession
            The open sessions. Rows carry only `secret_hash`.

        Raises
        ------
        NotFoundError
            If no principal has `principal_name` (asked only of an administrator).
        PermissionDenied
            If `p` is another person and not an administrator, whether or not the name exists.
        """
        if principal_name != p.name:
            # Before the lookup, so a non-administrator cannot tell which names exist.
            self._require_admin(p)
        target = self._principal_named(principal_name)
        now = self._clock()
        rows = (
            self._session.execute(
                select(BrowserSession)
                .where(
                    BrowserSession.principal_id == target.id,
                    BrowserSession.ended_at.is_(None),
                    BrowserSession.absolute_expires_at > now,
                )
                .order_by(BrowserSession.created_at, BrowserSession.key_id)
            )
            .scalars()
            .all()
        )
        if not target.is_active:
            return []
        return [
            r
            for r in rows
            if now < r.last_seen_at + timedelta(seconds=r.idle_seconds)
            and self._source_valid(r, now)
        ]

    def _source_valid(self, row: BrowserSession, now: datetime) -> bool:
        """Say whether the token a session was created through still works.

        Parameters
        ----------
        row
            The session.
        now
            The time to check at.

        Returns
        -------
        bool
            `True` for a session made without a token, or one whose token is neither revoked
            nor expired.
        """
        if row.source_pat_id is None:
            return True
        pat = self._session.get(PersonalAccessToken, row.source_pat_id)
        return pat is not None and _pat_valid(pat, now)


def _pat_valid(pat: PersonalAccessToken, now: datetime) -> bool:
    """Say whether a token still works.

    Parameters
    ----------
    pat
        The token.
    now
        The time to check at.

    Returns
    -------
    bool
        `False` if it is revoked or expired.
    """
    return pat.revoked_at is None and (pat.expires_at is None or pat.expires_at > now)


def _ref(principal: Principal, row: BrowserSession, now: datetime) -> PrincipalRef:
    """Build the principal reference of a session.

    Parameters
    ----------
    principal
        The session's principal.
    row
        The session.
    now
        The service clock's time of this check (`SessionContext.verified_at`).

    Returns
    -------
    PrincipalRef
        With ``credential="session"`` and the session's context.
    """
    return PrincipalRef(
        id=principal.id,
        name=principal.name,
        type=principal.type,
        language=principal.language,
        credential="session",
        session=SessionContext(
            session_id=row.id,
            key_id=row.key_id,
            via=row.via,
            created_at=row.created_at,
            last_touch_at=row.last_touch_at,
            source_pat_id=row.source_pat_id,
            verified_at=now,
        ),
    )
