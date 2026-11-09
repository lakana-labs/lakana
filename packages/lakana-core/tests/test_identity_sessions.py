"""Tests for the browser session service (`lakana_core.identity.sessions`)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.auth import (
    Action,
    AllowAllAuthorization,
    PrincipalRef,
    SecurableKind,
    SessionContext,
)
from lakana_core.bootstrap import ensure_bootstrap_admin_grant
from lakana_core.errors import (
    AuthenticationError,
    NotFoundError,
    PermissionDenied,
    SessionEndedError,
    StepUpRequired,
    ValidationError,
)
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.identity.pats import PatService
from lakana_core.identity.sessions import (
    LAST_SEEN_WRITE_INTERVAL,
    SESSION_BOUNDS,
    SESSION_DEFAULTS,
    STEP_UP_WINDOW,
    SessionService,
    StartedSession,
    require_step_up,
    utc_now,
)
from lakana_core.identity.tokens import hash_secret
from lakana_core.models import (
    AuditEvent,
    AuditEventType,
    BrowserSession,
    Grant,
    InstanceSetting,
    PersonalAccessToken,
    Principal,
    PrincipalType,
)

START = datetime(2026, 11, 8, 9, 0, tzinfo=UTC)


class _Clock:
    """A settable clock for the service."""

    def __init__(self) -> None:
        """Start at `START`."""
        self.now = START

    def __call__(self) -> datetime:
        """Read the clock.

        Returns
        -------
        datetime
            The time set.
        """
        return self.now


def _principal(
    session: Session,
    name: str,
    *,
    kind: PrincipalType = PrincipalType.USER,
    active: bool = True,
) -> Principal:
    """Insert a principal.

    Parameters
    ----------
    session
        Database session.
    name
        Its name.
    kind
        Its type.
    active
        Whether it is active.

    Returns
    -------
    Principal
        The row.
    """
    row = Principal(id=uuid.uuid4(), type=kind, name=name, is_active=active)
    session.add(row)
    session.commit()
    return row


def _ref(row: Principal) -> PrincipalRef:
    """Build a token-style reference to a principal.

    Parameters
    ----------
    row
        The principal.

    Returns
    -------
    PrincipalRef
        Without a session.
    """
    return PrincipalRef(id=row.id, name=row.name, type=row.type, credential="pat")


@pytest.fixture
def clock() -> _Clock:
    """Provide the service's clock.

    Returns
    -------
    _Clock
        Set `.now` to move time.
    """
    return _Clock()


@pytest.fixture
def svc(session: Session, clock: _Clock) -> SessionService:
    """Provide a session service that allows every action.

    Parameters
    ----------
    session
        Database session.
    clock
        The clock.

    Returns
    -------
    SessionService
        The service.
    """
    return SessionService(session, AllowAllAuthorization(), clock=clock)


@pytest.fixture
def alice(session: Session) -> Principal:
    """Provide a person.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    Principal
        ``alice``.
    """
    return _principal(session, "alice")


def _start(svc: SessionService, row: Principal, **kwargs: object) -> StartedSession:
    """Start and commit a passkey session.

    Parameters
    ----------
    svc
        The service; its database session is committed.
    row
        The principal.
    **kwargs
        Overrides of `start`'s arguments.

    Returns
    -------
    StartedSession
        The session.
    """
    arguments: dict = {
        "via": "passkey",
        "replacing": None,
        "peer": "127.0.0.1",
        "user_agent": "x",
        "touched": True,
    }
    arguments.update(kwargs)
    started = svc.start(row.id, **arguments)
    svc._session.commit()
    return started


def test_start_issues_a_fresh_id_and_never_reuses_the_presented_one(
    svc: SessionService, alice: Principal
) -> None:
    """A sign-in ends the session the browser presented and issues a new id (fixation)."""
    first = _start(svc, alice)
    second = _start(svc, alice, replacing=first.cookie_value)
    assert second.key_id != first.key_id
    with pytest.raises(SessionEndedError) as e:
        svc.verify(first.cookie_value, extend=True)
    assert e.value.reason == "replaced"
    assert svc.verify(second.cookie_value, extend=True).id == alice.id


def test_a_forged_replacing_value_ends_nothing(svc: SessionService, alice: Principal) -> None:
    """`replacing` with a wrong secret leaves the real session open."""
    first = _start(svc, alice)
    _start(svc, alice, replacing=f"{first.key_id}_wrong")
    assert svc.verify(first.cookie_value, extend=False).id == alice.id


def test_secret_is_stored_only_as_a_hash(
    svc: SessionService, alice: Principal, session: Session
) -> None:
    """No column of the row holds the secret, the cookie or the CSRF token."""
    started = _start(svc, alice)
    secret = started.cookie_value.split("_", 1)[1]
    row = session.execute(
        select(BrowserSession).where(BrowserSession.key_id == started.key_id)
    ).scalar_one()
    values = {str(getattr(row, c.key)) for c in BrowserSession.__table__.columns}
    assert not values & {secret, started.cookie_value, started.csrf_token}
    assert len(row.secret_hash) == 64


def test_key_id_has_no_underscore(svc: SessionService, alice: Principal) -> None:
    """The key id is 32 hex characters, so the cookie splits at its first underscore."""
    started = _start(svc, alice)
    assert len(started.key_id) == 32
    int(started.key_id, 16)
    assert started.cookie_value.startswith(f"{started.key_id}_")


def test_csrf_token_is_random_and_checked_against_its_hash(
    svc: SessionService, alice: Principal, session: Session
) -> None:
    """The CSRF token is random per session, stored only as a hash, and never derived.

    `check_csrf` says `None` for a cookie that names no session, so `verify` reports it.
    """
    a = _start(svc, alice, peer="p", user_agent="u")
    b = _start(svc, alice, peer="p", user_agent="u")
    assert a.csrf_token != b.csrf_token and len(a.csrf_token) >= 43
    row = session.execute(
        select(BrowserSession).where(BrowserSession.key_id == a.key_id)
    ).scalar_one()
    assert row.csrf_hash == hash_secret(a.csrf_token)
    assert a.csrf_token not in a.cookie_value
    assert svc.check_csrf(a.cookie_value, a.csrf_token) is True
    assert svc.check_csrf(b.cookie_value, a.csrf_token) is False
    assert svc.check_csrf(a.cookie_value, "ä-not-ascii") is False
    assert svc.check_csrf(a.cookie_value, "") is False
    assert svc.check_csrf("malformed", a.csrf_token) is None
    assert svc.check_csrf(f"{a.key_id}_wrong", a.csrf_token) is None


@pytest.mark.parametrize("cookie", ["", "nounderscore", "zz_secret", f"{'a' * 32}_wrong"])
def test_an_unknown_or_malformed_cookie_is_refused(
    svc: SessionService, alice: Principal, cookie: str
) -> None:
    """A cookie that names no session is an authentication failure, not an ended session."""
    _start(svc, alice)
    with pytest.raises(AuthenticationError) as e:
        svc.verify(cookie, extend=True)
    assert not isinstance(e.value, SessionEndedError)


def test_session_ends_after_idle(svc: SessionService, alice: Principal, clock: _Clock) -> None:
    """Twelve hours without a request end the session with reason ``idle``."""
    started = _start(svc, alice)
    clock.now = START + timedelta(hours=12, seconds=1)
    with pytest.raises(SessionEndedError) as e:
        svc.verify(started.cookie_value, extend=True)
    assert e.value.reason == "idle"
    assert "sign in again" in str(e.value).lower()
    with pytest.raises(SessionEndedError) as again:
        svc.verify(started.cookie_value, extend=True)
    assert again.value.reason == "idle"


def test_polls_do_not_extend_the_idle_timeout(
    svc: SessionService, alice: Principal, clock: _Clock
) -> None:
    """Hourly polls (`extend=False`) leave the idle clock running; it ends at 12 hours."""
    started = _start(svc, alice)
    for hour in range(1, 12):
        clock.now = START + timedelta(hours=hour)
        svc.verify(started.cookie_value, extend=False)
    clock.now = START + timedelta(hours=12)
    with pytest.raises(SessionEndedError) as e:
        svc.verify(started.cookie_value, extend=False)
    assert e.value.reason == "idle"


def test_requests_extend_the_idle_timeout(
    svc: SessionService, alice: Principal, clock: _Clock
) -> None:
    """A request the person started keeps the session alive past 12 hours."""
    started = _start(svc, alice)
    clock.now = START + timedelta(hours=11)
    svc.verify(started.cookie_value, extend=True)
    clock.now = START + timedelta(hours=22)
    assert svc.verify(started.cookie_value, extend=True).id == alice.id


def test_session_ends_at_its_absolute_lifetime_even_when_active(
    svc: SessionService, alice: Principal, clock: _Clock
) -> None:
    """A session in constant use ends after seven days with reason ``lifetime``."""
    started = _start(svc, alice)
    assert started.absolute_expires_at == START + timedelta(days=7)
    for step in range(1, 28):
        clock.now = START + timedelta(hours=6 * step)
        svc.verify(started.cookie_value, extend=True)
    clock.now = START + timedelta(days=7)
    with pytest.raises(SessionEndedError) as e:
        svc.verify(started.cookie_value, extend=True)
    assert e.value.reason == "lifetime"


def test_changed_limits_apply_to_new_sessions_only(
    svc: SessionService, alice: Principal, clock: _Clock, session: Session
) -> None:
    """New limits are stored as instance settings and bind only sessions started afterwards."""
    admin = _principal(session, "root")
    old = _start(svc, alice)
    limits = svc.set_limits(_ref(admin), idle=timedelta(hours=1), absolute=timedelta(days=2))
    assert (limits.idle, limits.absolute) == (timedelta(hours=1), timedelta(days=2))
    assert svc.get_limits() == limits
    stored = {
        row.key: row.value for row in session.execute(select(InstanceSetting)).scalars().all()
    }
    assert stored == {"sign_in.idle_seconds": 3600, "sign_in.absolute_seconds": 172800}
    new = _start(svc, alice)
    assert new.absolute_expires_at == START + timedelta(days=2)
    clock.now = START + timedelta(hours=2)
    assert svc.verify(old.cookie_value, extend=False).id == alice.id
    with pytest.raises(SessionEndedError) as e:
        svc.verify(new.cookie_value, extend=False)
    assert e.value.reason == "idle"
    [event] = (
        session.execute(
            select(AuditEvent).where(
                AuditEvent.event_type == AuditEventType.SIGN_IN_SETTINGS_CHANGED
            )
        )
        .scalars()
        .all()
    )
    assert event.detail == {
        "before": {"idle_seconds": 43200, "absolute_seconds": 604800},
        "after": {"idle_seconds": 3600, "absolute_seconds": 172800},
    }


def test_setting_only_one_limit_keeps_the_other(svc: SessionService, session: Session) -> None:
    """`None` keeps that limit as it is."""
    admin = _principal(session, "root")
    limits = svc.set_limits(_ref(admin), idle=None, absolute=timedelta(days=30))
    assert (limits.idle, limits.absolute) == (SESSION_DEFAULTS.idle, timedelta(days=30))
    limits = svc.set_limits(_ref(admin), idle=timedelta(hours=1), absolute=None)
    assert (limits.idle, limits.absolute) == (timedelta(hours=1), timedelta(days=30))
    assert svc.get_limits() == limits


@pytest.mark.parametrize(
    ("idle", "absolute", "named"),
    [
        (timedelta(hours=25), None, "at most 24 hours"),
        (timedelta(minutes=14), None, "at least 15 minutes"),
        (timedelta(seconds=890), None, "got idle 890 seconds"),
        (None, timedelta(days=31), "at most 30 days"),
        (None, timedelta(minutes=59), "at least 1 hour"),
        (timedelta(hours=3), timedelta(hours=2), "longer than the absolute lifetime"),
    ],
)
def test_limits_outside_the_bounds_are_refused_naming_the_bound(
    svc: SessionService,
    session: Session,
    idle: timedelta | None,
    absolute: timedelta | None,
    named: str,
) -> None:
    """A limit outside the bounds is refused with a message that names the bound."""
    admin = _principal(session, "root")
    with pytest.raises(ValidationError, match=named):
        svc.set_limits(_ref(admin), idle=idle, absolute=absolute)
    assert svc.get_limits() == SESSION_DEFAULTS


def test_bounds_are_the_plans_values() -> None:
    """The defaults and bounds carry the spec's numbers."""
    assert SESSION_DEFAULTS.idle == timedelta(hours=12)
    assert SESSION_DEFAULTS.absolute == timedelta(days=7)
    assert (SESSION_BOUNDS.idle_min, SESSION_BOUNDS.idle_max) == (
        timedelta(minutes=15),
        timedelta(hours=24),
    )
    assert (SESSION_BOUNDS.absolute_min, SESSION_BOUNDS.absolute_max) == (
        timedelta(hours=1),
        timedelta(days=30),
    )
    assert timedelta(minutes=15) == STEP_UP_WINDOW
    assert timedelta(seconds=60) == LAST_SEEN_WRITE_INTERVAL
    assert utc_now().tzinfo is not None


def test_set_limits_needs_admin_and_step_up(session: Session, clock: _Clock) -> None:
    """A non-administrator is refused; an administrator's stale session needs a passkey touch."""
    admin = _principal(session, "root")
    bob = _principal(session, "bob")
    ensure_bootstrap_admin_grant(
        session,
        PrincipalRef(id=admin.id, name=admin.name, type=admin.type),
        AllowAllAuthorization(),
    )
    svc = SessionService(session, GrantAuthorization(session), clock=clock)
    with pytest.raises(PermissionDenied):
        svc.set_limits(_ref(bob), idle=timedelta(hours=1), absolute=None)
    started = _start(svc, admin, touched=False)
    as_admin = svc.verify(started.cookie_value, extend=True)
    with pytest.raises(StepUpRequired):
        svc.set_limits(as_admin, idle=timedelta(hours=1), absolute=None)
    assert svc.get_limits() == SESSION_DEFAULTS


def test_deactivated_principal_cannot_use_its_session(
    svc: SessionService, alice: Principal, session: Session
) -> None:
    """Deactivating the person ends the session at its next use."""
    started = _start(svc, alice)
    alice.is_active = False
    session.commit()
    with pytest.raises(SessionEndedError) as e:
        svc.verify(started.cookie_value, extend=True)
    assert e.value.reason == "deactivated"


@pytest.mark.parametrize("how", ["revoked", "expired"])
def test_session_from_a_token_ends_when_the_token_is_revoked(
    svc: SessionService, alice: Principal, session: Session, clock: _Clock, how: str
) -> None:
    """A session created through a PAT ends once that PAT is revoked or expired."""
    admin = _principal(session, "root")
    issued = PatService(session, AllowAllAuthorization()).issue(
        _ref(admin), "alice", name="laptop", expires_at=START + timedelta(hours=1)
    )
    pat = session.execute(
        select(PersonalAccessToken).where(PersonalAccessToken.key_id == issued.key_id)
    ).scalar_one()
    started = _start(svc, alice, via="local", source_pat_id=pat.id, touched=False)
    ref = svc.verify(started.cookie_value, extend=True)
    assert ref.session is not None
    assert ref.session.source_pat_id == pat.id
    if how == "revoked":
        PatService(session, AllowAllAuthorization()).revoke(_ref(admin), "alice", issued.key_id)
    else:
        clock.now = START + timedelta(hours=1)
    with pytest.raises(SessionEndedError) as e:
        svc.verify(started.cookie_value, extend=True)
    assert e.value.reason == "token_revoked"


def test_a_session_cannot_come_from_another_principals_token(
    svc: SessionService, alice: Principal, session: Session
) -> None:
    """`source_pat_id` must be a token of the same principal."""
    admin = _principal(session, "root")
    _principal(session, "bob")
    issued = PatService(session, AllowAllAuthorization()).issue(_ref(admin), "bob", name="x")
    pat = session.execute(
        select(PersonalAccessToken).where(PersonalAccessToken.key_id == issued.key_id)
    ).scalar_one()
    with pytest.raises(PermissionDenied):
        _start(svc, alice, via="local", source_pat_id=pat.id)


@pytest.mark.parametrize(
    ("kind", "active"), [(PrincipalType.SERVICE_ACCOUNT, True), (PrincipalType.USER, False)]
)
def test_service_account_never_gets_a_session(
    svc: SessionService, session: Session, kind: PrincipalType, active: bool
) -> None:
    """A service account or a deactivated person is refused a session."""
    row = _principal(session, "robot", kind=kind, active=active)
    with pytest.raises(PermissionDenied):
        _start(svc, row)
    assert session.execute(select(BrowserSession)).first() is None


def test_start_refuses_an_unknown_principal_and_an_unknown_via(
    svc: SessionService, alice: Principal
) -> None:
    """An unknown principal is not found; an unknown `via` is a programming error."""
    with pytest.raises(NotFoundError):
        svc.start(
            uuid.uuid4(),
            via="passkey",
            replacing=None,
            peer="p",
            user_agent="u",
            touched=True,
        )
    with pytest.raises(ValueError, match="via"):
        _start(svc, alice, via="telepathy")


def test_end_all_of_another_person_needs_admin_and_step_up(session: Session, clock: _Clock) -> None:
    """Ending another person's sessions needs ADMIN and a fresh passkey touch."""
    admin = _principal(session, "root")
    bob = _principal(session, "bob")
    carol = _principal(session, "carol")
    ensure_bootstrap_admin_grant(
        session,
        PrincipalRef(id=admin.id, name=admin.name, type=admin.type),
        AllowAllAuthorization(),
    )
    svc = SessionService(session, GrantAuthorization(session), clock=clock)
    bobs = [_start(svc, bob), _start(svc, bob)]
    carols = _start(svc, carol)
    as_carol = svc.verify(carols.cookie_value, extend=True)
    with pytest.raises(PermissionDenied):
        svc.end_all(as_carol, "bob")
    admin_session = _start(svc, admin)
    clock.now = START + STEP_UP_WINDOW + timedelta(seconds=1)
    as_admin = svc.verify(admin_session.cookie_value, extend=True)
    with pytest.raises(StepUpRequired):
        svc.end_all(as_admin, "bob")
    svc.touch(admin_session.key_id)
    session.commit()
    as_admin = svc.verify(admin_session.cookie_value, extend=True)
    svc.end(as_admin, bobs[0].key_id)
    assert svc.end_all(as_admin, "bob") == 1
    for started in bobs:
        with pytest.raises(SessionEndedError) as e:
            svc.verify(started.cookie_value, extend=True)
        assert e.value.reason == "ended_by_admin"
    assert svc.verify(carols.cookie_value, extend=True).id == carol.id
    ended = (
        session.execute(
            select(AuditEvent).where(
                AuditEvent.event_type == AuditEventType.SESSION_ENDED,
                AuditEvent.detail["reason"].as_string() == "ended_by_admin",
            )
        )
        .scalars()
        .all()
    )
    assert sorted(e.detail["key_id"] for e in ended) == sorted(b.key_id for b in bobs)
    for event in ended:
        assert event.principal_id == admin.id
        assert event.detail["subject"] == "bob"


def test_end_all_of_ones_own_sessions_needs_no_step_up(
    svc: SessionService, alice: Principal, clock: _Clock
) -> None:
    """Signing out everywhere is always allowed for oneself."""
    a = _start(svc, alice, touched=False)
    b = _start(svc, alice, touched=False)
    me = svc.verify(a.cookie_value, extend=True)
    assert svc.end_all(me, "alice") == 2
    for started in (a, b):
        with pytest.raises(SessionEndedError) as e:
            svc.verify(started.cookie_value, extend=True)
        assert e.value.reason == "sign_out_everywhere"


def test_end_signs_out_one_session(svc: SessionService, alice: Principal) -> None:
    """`end` closes one's own session; ending it again is a no-op."""
    a = _start(svc, alice)
    b = _start(svc, alice)
    me = svc.verify(a.cookie_value, extend=True)
    svc.end(me, a.key_id)
    svc.end(me, a.key_id)
    with pytest.raises(SessionEndedError) as e:
        svc.verify(a.cookie_value, extend=True)
    assert e.value.reason == "sign_out"
    assert svc.verify(b.cookie_value, extend=True).id == alice.id


def test_end_of_someone_elses_session_is_not_found_for_a_non_admin(
    session: Session, clock: _Clock
) -> None:
    """A person cannot end, or learn of, another person's session."""
    bob = _principal(session, "bob")
    carol = _principal(session, "carol")
    svc = SessionService(session, GrantAuthorization(session), clock=clock)
    bobs = _start(svc, bob)
    with pytest.raises(NotFoundError):
        svc.end(_ref(carol), bobs.key_id)
    with pytest.raises(NotFoundError):
        svc.end(_ref(carol), "f" * 32)
    assert svc.verify(bobs.cookie_value, extend=True).id == bob.id


def test_end_for_principal_ends_every_open_session(
    svc: SessionService, alice: Principal, session: Session
) -> None:
    """The internal call ends all of a principal's open sessions with the given reason."""
    a = _start(svc, alice)
    b = _start(svc, alice)
    svc.end(svc.verify(b.cookie_value, extend=True), b.key_id)
    assert svc.end_for_principal(alice.id, "deactivated", commit=True) == 1
    assert svc.end_for_principal(alice.id, "deactivated") == 0
    with pytest.raises(SessionEndedError) as e:
        svc.verify(a.cookie_value, extend=True)
    assert e.value.reason == "deactivated"


def test_list_sessions_shows_open_ones_to_self_and_admin_only(
    session: Session, clock: _Clock
) -> None:
    """Open sessions only; another person may not list them unless an administrator."""
    admin = _principal(session, "root")
    bob = _principal(session, "bob")
    carol = _principal(session, "carol")
    ensure_bootstrap_admin_grant(
        session,
        PrincipalRef(id=admin.id, name=admin.name, type=admin.type),
        AllowAllAuthorization(),
    )
    svc = SessionService(session, GrantAuthorization(session), clock=clock)
    idle_one = _start(svc, bob)
    ended = _start(svc, bob)
    svc.end(_ref(bob), ended.key_id)
    clock.now = START + timedelta(hours=1)
    fresh = _start(svc, bob)
    clock.now = START + timedelta(hours=12, minutes=30)
    assert [s.key_id for s in svc.list_sessions(_ref(bob), "bob")] == [fresh.key_id]
    assert [s.key_id for s in svc.list_sessions(_ref(admin), "bob")] == [fresh.key_id]
    with pytest.raises(PermissionDenied):
        svc.list_sessions(_ref(carol), "bob")
    with pytest.raises(NotFoundError):
        svc.list_sessions(_ref(admin), "nobody")
    assert idle_one.key_id not in {s.key_id for s in svc.list_sessions(_ref(bob), "bob")}


def test_a_non_admin_cannot_tell_which_names_exist(session: Session, clock: _Clock) -> None:
    """Another person's sessions are refused before the name is looked up."""
    carol = _principal(session, "carol")
    _principal(session, "bob")
    svc = SessionService(session, GrantAuthorization(session), clock=clock)
    for name in ("bob", "nobody"):
        with pytest.raises(PermissionDenied):
            svc.list_sessions(_ref(carol), name)
        with pytest.raises(PermissionDenied):
            svc.end_all(_ref(carol), name)


def test_require_step_up_ignores_tokens_and_refuses_a_stale_session(
    svc: SessionService, alice: Principal, clock: _Clock
) -> None:
    """A token caller passes; a session touched more than 15 minutes ago does not."""
    require_step_up(_ref(alice))
    started = _start(svc, alice)
    me = svc.verify(started.cookie_value, extend=True)
    require_step_up(me, now=START + STEP_UP_WINDOW)
    with pytest.raises(StepUpRequired, match="passkey"):
        require_step_up(me, now=START + STEP_UP_WINDOW + timedelta(seconds=1))
    untouched = svc.verify(_start(svc, alice, touched=False).cookie_value, extend=True)
    with pytest.raises(StepUpRequired):
        require_step_up(untouched, now=START)


def test_touch_marks_a_fresh_passkey_touch(
    svc: SessionService, alice: Principal, clock: _Clock
) -> None:
    """`touch` sets `last_touch_at`; an unknown or ended session is not found."""
    started = _start(svc, alice, touched=False)
    clock.now = START + timedelta(minutes=5)
    svc.touch(started.key_id)
    svc._session.commit()
    me = svc.verify(started.cookie_value, extend=False)
    assert me.session is not None
    assert me.session.last_touch_at == START + timedelta(minutes=5)
    with pytest.raises(NotFoundError):
        svc.touch("0" * 32)


def test_verify_returns_a_session_principal(svc: SessionService, alice: Principal) -> None:
    """The reference carries the credential kind and the session's context."""
    started = _start(svc, alice, via="setup", touched=False)
    me = svc.verify(started.cookie_value, extend=True)
    assert me.credential == "session"
    assert me.token_id is None
    assert me.session is not None
    assert me.session == SessionContext(
        session_id=me.session.session_id,
        key_id=started.key_id,
        via="setup",
        created_at=START,
        last_touch_at=None,
        source_pat_id=None,
        verified_at=START,
    )
    assert started.principal.id == alice.id
    assert started.principal.credential == "session"


def test_last_seen_written_at_most_once_a_minute(
    svc: SessionService, alice: Principal, clock: _Clock, session: Session
) -> None:
    """`last_seen_at` moves only when the stored value is at least a minute old."""
    started = _start(svc, alice)

    def last_seen() -> datetime:
        session.expire_all()
        return session.execute(
            select(BrowserSession.last_seen_at).where(BrowserSession.key_id == started.key_id)
        ).scalar_one()

    clock.now = START + timedelta(seconds=59)
    svc.verify(started.cookie_value, extend=True)
    assert last_seen() == START
    clock.now = START + timedelta(seconds=60)
    svc.verify(started.cookie_value, extend=True)
    assert last_seen() == START + timedelta(seconds=60)
    clock.now = START + timedelta(seconds=200)
    svc.verify(started.cookie_value, extend=False)
    assert last_seen() == START + timedelta(seconds=60)


def test_session_events_are_audited_without_secrets(
    svc: SessionService, alice: Principal, session: Session
) -> None:
    """Start and end are audited with the facts that matter and never a secret."""
    started = _start(svc, alice, peer="10.0.0.1", user_agent="Firefox")
    svc.end(svc.verify(started.cookie_value, extend=True), started.key_id)
    events = (
        session.execute(
            select(AuditEvent)
            .where(
                AuditEvent.event_type.in_(
                    [AuditEventType.SESSION_STARTED, AuditEventType.SESSION_ENDED]
                )
            )
            .order_by(AuditEvent.occurred_at, AuditEvent.event_type.desc())
        )
        .scalars()
        .all()
    )
    by_type = {e.event_type: e for e in events}
    assert by_type[AuditEventType.SESSION_STARTED].detail == {
        "key_id": started.key_id,
        "via": "passkey",
        "link_id": None,
        "peer": "10.0.0.1",
        "user_agent": "Firefox",
    }
    assert by_type[AuditEventType.SESSION_ENDED].detail == {
        "key_id": started.key_id,
        "reason": "sign_out",
    }
    assert all(e.principal_id == alice.id for e in events)
    secret = started.cookie_value.split("_", 1)[1]
    text = repr([e.detail for e in events])
    assert secret not in text
    assert started.csrf_token not in text


def test_a_single_action_grant_is_not_enough_to_act_on_others(
    session: Session, clock: _Clock
) -> None:
    """`MANAGE_PRINCIPAL` granted alone does not make its holder an administrator here."""
    bob = _principal(session, "bob")
    dana = _principal(session, "dana")
    session.add(
        Grant(
            id=uuid.uuid4(),
            principal_id=dana.id,
            action=Action.MANAGE_PRINCIPAL,
            kind=SecurableKind.INSTANCE,
            granted_by=dana.id,
            granted_at=START,
        )
    )
    session.commit()
    svc = SessionService(session, GrantAuthorization(session), clock=clock)
    _start(svc, bob)
    with pytest.raises(PermissionDenied, match="Only an administrator"):
        svc.end_all(_ref(dana), "bob")


def test_started_session_hides_its_credentials_in_repr(
    svc: SessionService, alice: Principal
) -> None:
    """A logged or printed `StartedSession` shows neither the cookie nor the CSRF token."""
    started = _start(svc, alice)
    text = repr(started)
    assert started.cookie_value not in text
    assert started.csrf_token not in text


def test_a_cookie_with_a_trailing_newline_is_refused(svc: SessionService, alice: Principal) -> None:
    """The cookie's shape is checked exactly: a trailing newline is not the same value."""
    started = _start(svc, alice)
    with pytest.raises(AuthenticationError):
        svc.verify(started.cookie_value + "\n", extend=False)


def test_list_sessions_leaves_out_sessions_that_can_no_longer_be_used(
    svc: SessionService, alice: Principal, session: Session
) -> None:
    """A revoked token's session, or a deactivated person's, is not listed before its next use."""
    admin = _principal(session, "root")
    issued = PatService(session, AllowAllAuthorization()).issue(_ref(admin), "alice", name="l")
    pat = session.execute(
        select(PersonalAccessToken).where(PersonalAccessToken.key_id == issued.key_id)
    ).scalar_one()
    from_token = _start(svc, alice, via="local", source_pat_id=pat.id, touched=False)
    plain = _start(svc, alice)
    PatService(session, AllowAllAuthorization()).revoke(_ref(admin), "alice", issued.key_id)
    listed = [s.key_id for s in svc.list_sessions(_ref(alice), "alice")]
    assert listed == [plain.key_id]
    assert from_token.key_id not in listed
    alice.is_active = False
    session.commit()
    assert svc.list_sessions(_ref(admin), "alice") == []


def test_step_up_measures_against_the_clock_the_session_was_verified_with(
    svc: SessionService, alice: Principal, clock: _Clock
) -> None:
    """Without `now`, step-up uses the service clock of the request, not the wall clock.

    So a test that moves the app clock ages a touch for every route alike, whether its service
    passes a clock to `require_step_up` or not.
    """
    started = _start(svc, alice, touched=True)
    clock.now = START + STEP_UP_WINDOW
    require_step_up(svc.verify(started.cookie_value, extend=False))
    clock.now = START + STEP_UP_WINDOW + timedelta(seconds=1)
    with pytest.raises(StepUpRequired):
        require_step_up(svc.verify(started.cookie_value, extend=False))
