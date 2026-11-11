"""Recently opened: what a person opened, kept per person and shown only while readable."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from core_test_helpers import ensure_table
from home_test_helpers import deployed_job, environment, grant, person, query_of, run_of
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from lakana_core.auth import Action, PrincipalRef
from lakana_core.errors import NotFoundError, PermissionDenied, ValidationError
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.grants.roles import Role
from lakana_core.home import recent as recent_module
from lakana_core.home.recent import RECENT_KEEP, RecentService, RecentView
from lakana_core.models import Environment, Grant, RecentOpen, Table


class _Clock:
    """A settable clock for the service."""

    def __init__(self) -> None:
        """Start at a fixed time."""
        self.now = datetime(2026, 11, 11, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        """Read the time.

        Returns
        -------
        datetime
            The current setting.
        """
        return self.now


@pytest.fixture
def clock() -> _Clock:
    """Provide the settable clock.

    Returns
    -------
    _Clock
        The clock; set `.now` to move time.
    """
    return _Clock()


@pytest.fixture
def recent(session: Session, clock: _Clock) -> RecentService:
    """Provide the service with real authorization.

    Parameters
    ----------
    session
        Database session.
    clock
        The clock it reads.

    Returns
    -------
    RecentService
        The service under test.
    """
    return RecentService(session, GrantAuthorization(session), clock=clock)


@pytest.fixture
def alice(session: Session) -> PrincipalRef:
    """Persist alice, a viewer of `dev`.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    PrincipalRef
        Her reference.
    """
    ref = person(session, "alice")
    grant(session, ref, role=Role.ANALYST, environment_name="dev")
    return ref


@pytest.fixture
def bob(session: Session) -> PrincipalRef:
    """Persist bob, who may read nothing.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    PrincipalRef
        His reference.
    """
    return person(session, "bob")


@pytest.fixture
def table(session: Session) -> str:
    """Persist the table `gold.sales` in `dev`.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    str
        Its dotted name.
    """
    ensure_table(session, "dev", "gold", "sales")
    return "gold.sales"


def test_recent_keeps_the_newest_twenty(
    recent: RecentService, alice: PrincipalRef, session: Session, clock: _Clock
) -> None:
    names = [f"t{i:02d}" for i in range(RECENT_KEEP + 2)]
    for name in names:
        ensure_table(session, "dev", "gold", name)
        recent.record(alice, "alice", kind="table", environment="dev", ref=f"gold.{name}")
        clock.now += timedelta(seconds=1)
    shown = recent.list(alice, "alice")
    assert [view.ref for view in shown] == [f"gold.{n}" for n in reversed(names)][:RECENT_KEEP]
    assert shown[0] == RecentView(
        kind="table", environment="dev", ref="gold.t21", opened_at=clock.now - timedelta(seconds=1)
    )
    stored = session.execute(select(func.count()).select_from(RecentOpen)).scalar()
    assert stored == RECENT_KEEP


def test_recent_record_within_a_minute_writes_nothing(
    recent: RecentService, alice: PrincipalRef, table: str, clock: _Clock
) -> None:
    first = clock.now
    recent.record(alice, "alice", kind="table", environment="dev", ref=table)
    clock.now = first + timedelta(seconds=59)
    recent.record(alice, "alice", kind="table", environment="dev", ref=table)
    assert [view.opened_at for view in recent.list(alice, "alice")] == [first]
    clock.now = first + timedelta(seconds=60)
    recent.record(alice, "alice", kind="table", environment="dev", ref=table)
    assert [view.opened_at for view in recent.list(alice, "alice")] == [clock.now]


def test_recent_refuses_an_object_the_person_cannot_read_as_not_found(
    recent: RecentService, bob: PrincipalRef, alice: PrincipalRef, table: str
) -> None:
    with pytest.raises(NotFoundError) as refused:
        recent.record(bob, "bob", kind="table", environment="dev", ref=table)
    assert str(refused.value) == "No table gold.sales in dev that you can read."
    with pytest.raises(NotFoundError) as missing:
        recent.record(alice, "alice", kind="table", environment="dev", ref="gold.nothing")
    assert str(missing.value) == "No table gold.nothing in dev that you can read."
    for ref in ("sales", "gold..sales"):
        with pytest.raises(NotFoundError):
            recent.record(alice, "alice", kind="table", environment="dev", ref=ref)
    with pytest.raises(NotFoundError, match="in prod"):
        recent.record(alice, "alice", kind="table", environment="prod", ref=table)


def test_recent_hides_an_object_the_person_can_no_longer_read(
    recent: RecentService, alice: PrincipalRef, table: str, session: Session
) -> None:
    recent.record(alice, "alice", kind="table", environment="dev", ref=table)
    assert len(recent.list(alice, "alice")) == 1
    session.execute(
        update(Grant).where(Grant.principal_id == alice.id).values(revoked_at=datetime.now(UTC))
    )
    session.commit()
    assert recent.list(alice, "alice") == []


def test_recent_hides_the_entries_of_a_deleted_environment(
    recent: RecentService, alice: PrincipalRef, table: str, session: Session
) -> None:
    recent.record(alice, "alice", kind="table", environment="dev", ref=table)
    session.execute(update(Environment).values(deleted_at=datetime.now(UTC)))
    session.commit()
    assert recent.list(alice, "alice") == []


def test_recent_refuses_a_nul_character(recent: RecentService, alice: PrincipalRef) -> None:
    for environment_name, ref in (("dev\x00", "gold.sales"), ("dev", "analytics\x00/nightly")):
        with pytest.raises(ValidationError, match="control character"):
            recent.record(alice, "alice", kind="job", environment=environment_name, ref=ref)


def test_recent_runs_are_readable_with_run_alone(recent: RecentService, session: Session) -> None:
    runner = person(session, "runner")
    grant(session, runner, action=Action.RUN, environment_name="dev")
    repo, version = deployed_job(session, "dev", "analytics", "nightly")
    run = run_of(session, repo, version)
    recent.record(runner, "runner", kind="run", environment="dev", ref=str(run.id))
    assert [v.ref for v in recent.list(runner, "runner")] == [str(run.id)]


def test_recent_hides_a_table_that_was_dropped(
    recent: RecentService, alice: PrincipalRef, table: str, session: Session
) -> None:
    recent.record(alice, "alice", kind="table", environment="dev", ref=table)
    session.execute(delete(Table))
    session.commit()
    assert recent.list(alice, "alice") == []


def test_recent_of_another_person_is_refused(recent: RecentService, alice: PrincipalRef) -> None:
    with pytest.raises(PermissionDenied, match="your own"):
        recent.list(alice, "bob")
    with pytest.raises(PermissionDenied, match="your own"):
        recent.record(alice, "bob", kind="table", environment="dev", ref="gold.sales")


def test_recent_refuses_an_unknown_kind_and_an_overlong_ref(
    recent: RecentService, alice: PrincipalRef
) -> None:
    with pytest.raises(ValidationError, match="table, query, job or run"):
        recent.record(alice, "alice", kind="chart", environment="dev", ref="x")
    with pytest.raises(ValidationError, match="1024 bytes"):
        recent.record(alice, "alice", kind="table", environment="dev", ref="ä" * 600)


def test_recent_jobs_and_runs_need_read_environment(
    recent: RecentService, alice: PrincipalRef, bob: PrincipalRef, session: Session
) -> None:
    repo, version = deployed_job(session, "dev", "analytics", "nightly")
    run = run_of(session, repo, version)
    recent.record(alice, "alice", kind="job", environment="dev", ref="analytics/nightly")
    recent.record(alice, "alice", kind="run", environment="dev", ref=str(run.id))
    assert {(v.kind, v.ref) for v in recent.list(alice, "alice")} == {
        ("job", "analytics/nightly"),
        ("run", str(run.id)),
    }
    for kind, ref in (("job", "analytics/nightly"), ("run", str(run.id))):
        with pytest.raises(NotFoundError, match=f"No {kind} "):
            recent.record(bob, "bob", kind=kind, environment="dev", ref=ref)
    for kind, ref in (
        ("job", "analytics/weekly"),
        ("job", "nightly"),
        ("job", "other/nightly"),
        ("run", "not-an-id"),
        ("run", "00000000-0000-0000-0000-000000000000"),
    ):
        with pytest.raises(NotFoundError):
            recent.record(alice, "alice", kind=kind, environment="dev", ref=ref)


def test_recent_queries_are_the_submitters_or_need_read_query_text(
    recent: RecentService, alice: PrincipalRef, bob: PrincipalRef, session: Session
) -> None:
    own = query_of(session, alice)
    recent.record(alice, "alice", kind="query", environment="dev", ref=str(own.id))
    with pytest.raises(NotFoundError, match="No query"):
        recent.record(bob, "bob", kind="query", environment="dev", ref=str(own.id))
    grant(session, bob, action=Action.READ_QUERY_TEXT)
    recent.record(bob, "bob", kind="query", environment="dev", ref=str(own.id))
    assert [v.ref for v in recent.list(bob, "bob")] == [str(own.id)]
    environment(session, "prod")
    with pytest.raises(NotFoundError):
        recent.record(alice, "alice", kind="query", environment="prod", ref=str(own.id))
    with pytest.raises(NotFoundError):
        recent.record(alice, "alice", kind="query", environment="dev", ref="nope")


def test_recent_hides_own_queries_once_the_person_may_not_query_there(
    recent: RecentService, alice: PrincipalRef, session: Session
) -> None:
    own = query_of(session, alice)
    recent.record(alice, "alice", kind="query", environment="dev", ref=str(own.id))
    session.execute(delete(Grant).where(Grant.principal_id == alice.id))
    session.commit()
    assert recent.list(alice, "alice") == []


def test_two_records_at_once_keep_one_row(
    recent: RecentService,
    alice: PrincipalRef,
    table: str,
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recent.record(alice, "alice", kind="table", environment="dev", ref=table)
    # The other request's row is not seen, as when both look before either writes.
    monkeypatch.setattr(recent_module, "_existing", lambda *args: None)
    recent.record(alice, "alice", kind="table", environment="dev", ref=table)
    assert session.execute(select(func.count()).select_from(RecentOpen)).scalar() == 1
    assert len(recent.list(alice, "alice")) == 1
