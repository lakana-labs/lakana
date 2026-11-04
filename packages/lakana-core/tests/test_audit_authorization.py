"""Tests for AuditingAuthorization."""

import uuid
from collections.abc import Callable

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.audit.authorization import AuditingAuthorization, flush_suppressed
from lakana_core.audit.denial_limiter import DenialAuditLimiter
from lakana_core.auth import Action, PrincipalRef, Securable
from lakana_core.errors import PermissionDenied
from lakana_core.identifiers import NamespacePath
from lakana_core.models import AuditEvent, AuditEventType, Principal, PrincipalType


def _add_principal(session: Session, name: str) -> PrincipalRef:
    """Insert a principal and return a PrincipalRef for it.

    Returns
    -------
    PrincipalRef
        A reference to the newly inserted principal.
    """
    row = Principal(id=uuid.uuid4(), type=PrincipalType.USER, name=name)
    session.add(row)
    session.commit()
    return PrincipalRef(id=row.id, name=row.name, type=row.type)


class _AllowAll:
    """A trivial AuthorizationService that permits everything."""

    def authorize(self, principal: PrincipalRef, action: Action, securable: Securable) -> None:
        """Permit unconditionally."""

    def permits(self, principal: PrincipalRef, action: Action, securable: Securable) -> bool:
        """Answer as `authorize` would, as a boolean.

        Parameters
        ----------
        principal
            The actor requesting the action.
        action
            The action requested.
        securable
            The object acted upon.

        Returns
        -------
        bool
            `False` if `authorize` raises `PermissionDenied`, else `True`.
        """
        try:
            self.authorize(principal, action, securable)
        except PermissionDenied:
            return False
        return True


class _DenyAll:
    """A trivial AuthorizationService that refuses everything."""

    def authorize(self, principal: PrincipalRef, action: Action, securable: Securable) -> None:
        """Refuse unconditionally.

        Raises
        ------
        PermissionDenied
            Always.
        """
        raise PermissionDenied("refused")

    def permits(self, principal: PrincipalRef, action: Action, securable: Securable) -> bool:
        """Answer as `authorize` would, as a boolean.

        Parameters
        ----------
        principal
            The actor requesting the action.
        action
            The action requested.
        securable
            The object acted upon.

        Returns
        -------
        bool
            `False` if `authorize` raises `PermissionDenied`, else `True`.
        """
        try:
            self.authorize(principal, action, securable)
        except PermissionDenied:
            return False
        return True


def test_a_permitted_call_passes_through_and_records_nothing(session: Session) -> None:
    """A successful authorize() records no audit event at all."""
    admin = _add_principal(session, "admin")
    wrapper = AuditingAuthorization(_AllowAll(), session, DenialAuditLimiter(10_000))

    wrapper.authorize(admin, Action.READ_TABLE, Securable.environment_scope("dev"))

    assert session.query(AuditEvent).count() == 0


def test_a_denied_call_still_raises_permission_denied(session: Session) -> None:
    """The wrapper never swallows the original exception."""
    admin = _add_principal(session, "admin")
    wrapper = AuditingAuthorization(_DenyAll(), session, DenialAuditLimiter(10_000))

    with pytest.raises(PermissionDenied):
        wrapper.authorize(admin, Action.READ_TABLE, Securable.environment_scope("dev"))


def test_a_denied_call_records_exactly_one_authorization_denied_event(session: Session) -> None:
    """The denial is recorded, with the exact action/securable checked."""
    admin = _add_principal(session, "admin")
    securable = Securable.table_scope("dev", NamespacePath.from_parts(["gold"]), "customers")
    wrapper = AuditingAuthorization(_DenyAll(), session, DenialAuditLimiter(10_000))

    with pytest.raises(PermissionDenied):
        wrapper.authorize(admin, Action.READ_TABLE, securable)

    events = session.query(AuditEvent).all()
    assert len(events) == 1
    assert events[0].event_type == AuditEventType.AUTHORIZATION_DENIED
    assert events[0].principal_id == admin.id
    assert events[0].securable_environment == "dev"
    assert events[0].securable_namespace == "gold"
    assert events[0].securable_table == "customers"
    assert events[0].detail["action"] == "READ_TABLE"


def test_a_denied_call_records_the_specific_action_that_was_checked(session: Session) -> None:
    """A different denied action is recorded as that action, not a fixed placeholder."""
    admin = _add_principal(session, "admin")
    securable = Securable.environment_scope("dev")
    wrapper = AuditingAuthorization(_DenyAll(), session, DenialAuditLimiter(10_000))

    with pytest.raises(PermissionDenied):
        wrapper.authorize(admin, Action.MANAGE_GRANT, securable)

    event = session.query(AuditEvent).one()
    assert event.detail["action"] == Action.MANAGE_GRANT.value


def test_the_recorded_denial_event_is_committed_immediately(session: Session) -> None:
    """A denial's audit row survives even without the caller ever committing anything else."""
    admin = _add_principal(session, "admin")
    wrapper = AuditingAuthorization(_DenyAll(), session, DenialAuditLimiter(10_000))

    with pytest.raises(PermissionDenied):
        wrapper.authorize(admin, Action.READ_TABLE, Securable.environment_scope("dev"))
    session.rollback()

    assert session.query(AuditEvent).count() == 1


class _RecordingAuthz:
    """An AuthorizationService that records exactly what it was called with.

    `_AllowAll`/`_DenyAll` above decide only *whether* to permit — neither
    inspects its arguments, so a mutation that swaps `securable` for
    `action`, or drops one entirely, passes every other test in this file
    unnoticed. This double records the call instead, so a test can assert
    the wrapper forwards the exact objects it was given.
    """

    def __init__(self) -> None:
        self.authorize_calls: list[tuple[PrincipalRef, Action, Securable]] = []
        self.permits_calls: list[tuple[PrincipalRef, Action, Securable]] = []

    def authorize(self, principal: PrincipalRef, action: Action, securable: Securable) -> None:
        """Record the call; never denies.

        Parameters
        ----------
        principal
            The actor requesting the action.
        action
            The action requested.
        securable
            The object acted upon.
        """
        self.authorize_calls.append((principal, action, securable))

    def permits(self, principal: PrincipalRef, action: Action, securable: Securable) -> bool:
        """Record the call; always permits.

        Parameters
        ----------
        principal
            The actor the question is about.
        action
            The action in question.
        securable
            The object in question.

        Returns
        -------
        bool
            Always `True`.
        """
        self.permits_calls.append((principal, action, securable))
        return True


def test_authorize_passes_through_the_exact_principal_action_and_securable(
    session: Session,
) -> None:
    """The wrapped service sees the same objects the caller passed, unchanged."""
    admin = _add_principal(session, "admin")
    recorder = _RecordingAuthz()
    wrapper = AuditingAuthorization(recorder, session, DenialAuditLimiter(10_000))
    securable = Securable.table_scope("dev", NamespacePath.from_parts(["gold"]), "customers")

    wrapper.authorize(admin, Action.WRITE_TABLE, securable)

    assert recorder.authorize_calls == [(admin, Action.WRITE_TABLE, securable)]


def test_permits_passes_through_the_exact_principal_action_and_securable(session: Session) -> None:
    """The wrapped service sees the same objects the caller passed, unchanged."""
    admin = _add_principal(session, "admin")
    recorder = _RecordingAuthz()
    wrapper = AuditingAuthorization(recorder, session, DenialAuditLimiter(10_000))
    securable = Securable.table_scope("dev", NamespacePath.from_parts(["gold"]), "customers")

    wrapper.permits(admin, Action.WRITE_TABLE, securable)

    assert recorder.permits_calls == [(admin, Action.WRITE_TABLE, securable)]


def test_permits_delegates_and_records_nothing(session: Session) -> None:
    """A `False` from `permits` is a visibility filter, not a denied action: no audit row."""
    admin = _add_principal(session, "admin")
    denying = AuditingAuthorization(_DenyAll(), session, DenialAuditLimiter(10_000))
    allowing = AuditingAuthorization(_AllowAll(), session, DenialAuditLimiter(10_000))

    assert (
        denying.permits(admin, Action.READ_ENVIRONMENT, Securable.environment_scope("dev")) is False
    )
    assert allowing.permits(admin, Action.READ_ENVIRONMENT, Securable.environment_scope("dev"))
    assert session.query(AuditEvent).count() == 0


def test_a_flood_of_denials_by_one_principal_writes_a_bounded_number_of_rows(
    session: Session,
) -> None:
    """The audit log does not grow with the number of denials of one principal."""
    attacker = _add_principal(session, "attacker")
    wrapper = AuditingAuthorization(_DenyAll(), session, DenialAuditLimiter(per_minute=5))
    for _ in range(50):
        with pytest.raises(PermissionDenied):
            wrapper.authorize(attacker, Action.READ_TABLE, Securable.environment_scope("dev"))
    assert session.query(AuditEvent).count() == 5


def test_a_flood_by_one_principal_does_not_hide_the_denials_of_another(session: Session) -> None:
    """Another principal's first denial is recorded while the attacker is being limited."""
    attacker = _add_principal(session, "attacker")
    victim = _add_principal(session, "victim")
    wrapper = AuditingAuthorization(_DenyAll(), session, DenialAuditLimiter(per_minute=1))
    for _ in range(20):
        with pytest.raises(PermissionDenied):
            wrapper.authorize(attacker, Action.READ_TABLE, Securable.environment_scope("dev"))
    with pytest.raises(PermissionDenied):
        wrapper.authorize(victim, Action.READ_TABLE, Securable.environment_scope("dev"))
    recorded = {event.principal_id for event in session.query(AuditEvent).all()}
    assert recorded == {attacker.id, victim.id}


def test_the_first_denial_of_each_action_is_recorded_even_when_the_rate_is_used_up(
    session: Session,
) -> None:
    """A principal limited on one action still has its first denial of another recorded."""
    p = _add_principal(session, "p")
    wrapper = AuditingAuthorization(_DenyAll(), session, DenialAuditLimiter(per_minute=1))
    for action in (Action.READ_TABLE, Action.READ_TABLE, Action.MANAGE_GRANT):
        with pytest.raises(PermissionDenied):
            wrapper.authorize(p, action, Securable.environment_scope("dev"))
    actions = [event.detail["action"] for event in session.query(AuditEvent).all()]
    assert sorted(actions) == ["MANAGE_GRANT", "READ_TABLE"]


def test_the_next_recorded_denial_says_how_many_were_not_recorded(session: Session) -> None:
    """A recorded denial carries `suppressed`, the count dropped since the last row."""
    clock = [0.0]
    p = _add_principal(session, "p")
    limiter = DenialAuditLimiter(per_minute=2, clock=lambda: clock[0])
    wrapper = AuditingAuthorization(_DenyAll(), session, limiter)
    for _ in range(6):
        with pytest.raises(PermissionDenied):
            wrapper.authorize(p, Action.READ_TABLE, Securable.environment_scope("dev"))
    clock[0] = 60.0
    with pytest.raises(PermissionDenied):
        wrapper.authorize(p, Action.READ_TABLE, Securable.environment_scope("dev"))
    details = [event.detail for event in session.query(AuditEvent).all()]
    assert sorted(d.get("suppressed", 0) for d in details) == [0, 0, 4]


_LOGGER = "lakana_core.audit.authorization"


def test_a_suppressed_denial_is_written_to_the_log_with_the_object(
    session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    """Past the rate the audit row is dropped, but the log still names the object."""
    attacker = _add_principal(session, "attacker")
    wrapper = AuditingAuthorization(_DenyAll(), session, DenialAuditLimiter(per_minute=1))
    secret_table = Securable.table_scope("dev", NamespacePath.parse("gold"), "payroll")
    caplog.set_level("INFO", logger=_LOGGER)
    with pytest.raises(PermissionDenied):
        wrapper.authorize(attacker, Action.READ_TABLE, Securable.environment_scope("dev"))
    assert caplog.records == []  # a recorded denial is in the table, not in the log
    with pytest.raises(PermissionDenied):
        wrapper.authorize(attacker, Action.READ_TABLE, secret_table)
    assert session.query(AuditEvent).count() == 1
    [record] = caplog.records
    assert record.levelname == "INFO"
    message = record.getMessage()
    assert str(attacker.id) in message
    assert "READ_TABLE" in message
    assert "dev.gold.payroll" in message
    assert "LAKANA_DENIAL_AUDIT__PER_PRINCIPAL_PER_MINUTE" in message


def test_the_log_line_of_a_suppressed_denial_is_escaped_and_bounded(
    session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    """A table name a caller chose cannot forge a log line or grow it without limit."""
    p = _add_principal(session, "p")
    wrapper = AuditingAuthorization(_DenyAll(), session, DenialAuditLimiter(per_minute=1))
    hostile = Securable.table_scope("dev", NamespacePath.parse("gold"), "x\nFORGED " + "y" * 5000)
    caplog.set_level("INFO", logger=_LOGGER)
    for _ in range(2):
        with pytest.raises(PermissionDenied):
            wrapper.authorize(p, Action.READ_TABLE, hostile)
    [record] = caplog.records
    message = record.getMessage()
    assert "\n" not in message
    assert len(message) < 1000


def test_pending_counts_are_written_as_one_row_per_principal_on_flush(session: Session) -> None:
    """A principal's suppressed denials that no later row carried are summarised at shutdown."""
    p = _add_principal(session, "p")
    quiet = _add_principal(session, "quiet")
    limiter = DenialAuditLimiter(per_minute=1)
    wrapper = AuditingAuthorization(_DenyAll(), session, limiter)
    for _ in range(4):
        with pytest.raises(PermissionDenied):
            wrapper.authorize(p, Action.READ_TABLE, Securable.environment_scope("dev"))
    with pytest.raises(PermissionDenied):
        wrapper.authorize(quiet, Action.READ_TABLE, Securable.environment_scope("dev"))
    before = session.query(AuditEvent).count()
    flush_suppressed(limiter, session)
    rows = session.query(AuditEvent).order_by(AuditEvent.occurred_at).all()
    assert len(rows) == before + 1
    summary = rows[-1]
    assert summary.principal_id == p.id
    assert summary.event_type == AuditEventType.AUTHORIZATION_DENIED
    assert summary.detail["suppressed"] == 3
    flush_suppressed(limiter, session)
    assert session.query(AuditEvent).count() == before + 1


def test_a_flush_that_cannot_write_logs_and_does_not_raise(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Shutdown goes on when the summary row cannot be written."""
    limiter = DenialAuditLimiter(per_minute=1)
    pid = str(uuid.uuid4())
    limiter.admit(pid, "READ_TABLE")
    limiter.admit(pid, "READ_TABLE")

    class _Broken:
        def add(self, row: object) -> None:
            raise RuntimeError("database gone")

        def rollback(self) -> None:
            pass

    caplog.set_level("WARNING", logger=_LOGGER)
    flush_suppressed(limiter, _Broken())  # ty: ignore[invalid-argument-type]
    assert "could not be written" in caplog.text


class _CountingSession:
    """A session double that counts the summary rows added and commits."""

    def __init__(self, on_commit: Callable[[], None] | None = None) -> None:
        self.added = 0
        self._on_commit = on_commit

    def add(self, row: object) -> None:
        self.added += 1

    def commit(self) -> None:
        if self._on_commit is not None:
            self._on_commit()

    def rollback(self) -> None:
        pass


def _limiter_with_pending(principals: int) -> DenialAuditLimiter:
    limiter = DenialAuditLimiter(per_minute=1)
    for _ in range(principals):
        pid = str(uuid.uuid4())
        limiter.admit(pid, "READ_TABLE")
        limiter.admit(pid, "READ_TABLE")
    return limiter


def test_a_flush_writes_at_most_100_principals_and_warns_about_the_rest(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """With more pending principals than the cap, the rest are counted in one warning."""
    limiter = _limiter_with_pending(130)
    session = _CountingSession()
    caplog.set_level("WARNING", logger=_LOGGER)
    flush_suppressed(limiter, session)  # ty: ignore[invalid-argument-type]
    assert session.added == 100
    [record] = caplog.records
    assert "30 principals" in record.getMessage()
    assert limiter.drain() == {}


def test_a_flush_stops_when_its_time_is_up_and_warns_about_the_rest(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A slow database cannot hold the stop for more than about 3 seconds."""
    limiter = _limiter_with_pending(10)
    now = [0.0]
    session = _CountingSession(on_commit=lambda: now.__setitem__(0, now[0] + 1.0))
    caplog.set_level("WARNING", logger=_LOGGER)
    flush_suppressed(limiter, session, clock=lambda: now[0])  # ty: ignore[invalid-argument-type]
    assert session.added == 3
    [record] = caplog.records
    assert "7 principals" in record.getMessage()


def test_a_flush_within_both_caps_warns_about_nothing(caplog: pytest.LogCaptureFixture) -> None:
    """The warning appears only when a count was left out."""
    limiter = _limiter_with_pending(5)
    session = _CountingSession()
    caplog.set_level("WARNING", logger=_LOGGER)
    flush_suppressed(limiter, session)  # ty: ignore[invalid-argument-type]
    assert session.added == 5
    assert caplog.records == []


def test_record_denial_writes_the_reason_and_respects_the_limiter(session: Session) -> None:
    principal = _add_principal(session, "bob")
    wrapper = AuditingAuthorization(_AllowAll(), session, DenialAuditLimiter(1))
    for _ in range(2):
        wrapper.record_denial(
            principal, Action.MANAGE_TOKEN, Securable.instance_scope(), reason="not_instance_admin"
        )
    events = session.execute(select(AuditEvent)).scalars().all()
    assert [event.detail for event in events] == [
        {"action": "MANAGE_TOKEN", "reason": "not_instance_admin"}
    ]


def test_permits_each_is_delegated_and_records_nothing(session: Session) -> None:
    alice = _add_principal(session, "alice")
    questions = [(Action.READ_TABLE, Securable.instance_scope())] * 2
    allowing = AuditingAuthorization(_AllowAll(), session, DenialAuditLimiter(10_000))
    refusing = AuditingAuthorization(_DenyAll(), session, DenialAuditLimiter(10_000))
    assert allowing.permits_each(alice, questions) == [True, True]
    assert refusing.permits_each(alice, questions) == [False, False]
    assert session.scalars(select(AuditEvent)).all() == []
