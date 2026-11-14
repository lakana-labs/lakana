"""The worker's scheduler thread."""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from lakana_core.auth import AllowAllAuthorization, PrincipalRef
from lakana_core.catalog.service import CatalogService
from lakana_core.config import DatabaseConfig, StorageConfig
from lakana_core.db import create_engine
from lakana_core.models import (
    Base,
    Environment,
    EnvironmentDeployment,
    JobVersion,
    Principal,
    PrincipalType,
    QueryRun,
    QueryRunStatus,
    Repository,
    RepositoryKind,
    Run,
    RunStatus,
    SchedulerLease,
    Worker,
)
from lakana_core.runs.leader import STALE_AFTER, LeaderElection
from lakana_core.runs.selection import Selection
from lakana_core.runs.service import RunService
from lakana_core.storage.resolver import StorageResolver
from lakana_worker import scheduler as scheduler_module
from lakana_worker.scheduler import SchedulerLoop

NOW = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[sqlalchemy.Engine]:
    """Build a file database with `dev`, a deployed one-asset repository and a queued run.

    Parameters
    ----------
    tmp_path
        Pytest's per-test directory.

    Yields
    ------
    sqlalchemy.Engine
        The engine, disposed of after the test.
    """
    built = create_engine(DatabaseConfig(url=f"sqlite:///{tmp_path}/lakana.db"))
    Base.metadata.create_all(built)
    with Session(built) as session:
        admin = PrincipalRef(id=uuid.uuid4(), name="admin", type=PrincipalType.USER)
        session.add(Principal(id=admin.id, type=admin.type, name=admin.name, is_active=True))
        session.commit()
        env = CatalogService(
            session, StorageResolver(session), AllowAllAuthorization()
        ).create_environment(
            admin,
            name="dev",
            storage=StorageConfig(scheme="file", root=f"file://{tmp_path / 's'}", options={}),
        )
        repo = Repository(
            id=uuid.uuid4(), name="analytics", kind=RepositoryKind.HTTPS, url="https://h/a.git"
        )
        session.add(repo)
        session.flush()
        version = JobVersion(
            id=uuid.uuid4(),
            repository_id=repo.id,
            environment_id=env.id,
            commit_sha="a" * 40,
            manifest_json={"version": 1, "assets": [{"table": "silver.clean"}], "edges": []},
            code_uri="file:///x",
            code_sha256="0" * 64,
        )
        session.add(version)
        session.flush()
        session.add(
            EnvironmentDeployment(
                environment_id=env.id, repository_id=repo.id, current_job_version_id=version.id
            )
        )
        session.commit()
        RunService(session, AllowAllAuthorization()).start(admin, "dev", "analytics", Selection())
    yield built
    built.dispose()


def _run_status(engine: sqlalchemy.Engine) -> RunStatus:
    """Read the one run's status.

    Parameters
    ----------
    engine
        The engine.

    Returns
    -------
    RunStatus
        Its status.
    """
    with Session(engine) as session:
        return session.scalars(select(Run.status)).one()


def test_the_holder_ticks_and_the_other_does_not(engine: sqlalchemy.Engine) -> None:
    """Two loops, one database: exactly one ticks."""
    first = SchedulerLoop(engine, "w1", clock=lambda: NOW, rand=lambda: 0.5)
    second = SchedulerLoop(engine, "w2", clock=lambda: NOW, rand=lambda: 0.5)
    assert first.step() is True
    assert second.step() is False
    assert _run_status(engine) is RunStatus.RUNNING


def test_a_failing_step_is_logged_and_survived(
    engine: sqlalchemy.Engine, caplog: pytest.LogCaptureFixture
) -> None:
    """Nothing a tick raises stops the scheduler thread."""

    class Broken(LeaderElection):
        def try_lead(self, now: datetime) -> bool:
            raise RuntimeError("database went away")

    loop = SchedulerLoop(engine, "w1", election=Broken(engine, "w1"))
    assert loop.step() is False
    assert "Scheduler step failed" in caplog.text
    assert "scheduler_step_failed" in {getattr(r, "event", None) for r in caplog.records}


def test_the_thread_runs_until_stopped_and_then_resigns(engine: sqlalchemy.Engine) -> None:
    """`start` ticks in the background; `stop` ends the loop and frees the role."""
    loop = SchedulerLoop(engine, "w1", interval=0.01)
    thread = loop.start()
    deadline = time.monotonic() + 5
    while _run_status(engine) is RunStatus.QUEUED and time.monotonic() < deadline:
        time.sleep(0.02)
    loop.stop()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert _run_status(engine) is RunStatus.RUNNING
    with Session(engine) as session:
        lease = session.get(SchedulerLease, 1)
        assert lease is not None
        assert lease.holder is None


def test_a_failing_resignation_is_logged(
    engine: sqlalchemy.Engine, caplog: pytest.LogCaptureFixture
) -> None:
    """A database error while resigning does not raise out of the thread."""

    class Stubborn(LeaderElection):
        def resign(self) -> None:
            raise RuntimeError("gone")

    loop = SchedulerLoop(engine, "w1", election=Stubborn(engine, "w1"))
    loop.stop()
    loop.run()
    assert "Resigning the scheduler role failed" in caplog.text


def test_repeated_failures_back_off_and_log_one_traceback(
    engine: sqlalchemy.Engine, caplog: pytest.LogCaptureFixture
) -> None:
    """Failures double the wait up to a cap; only the first logs a traceback; recovery resets."""

    class Flaky(LeaderElection):
        failing = True

        def try_lead(self, now: datetime) -> bool:
            if self.failing:
                raise RuntimeError("database went away")
            return False

    election = Flaky(engine, "w1")
    loop = SchedulerLoop(engine, "w1", interval=1.0, election=election)
    waits = []
    for _ in range(8):
        loop.step()
        waits.append(loop.next_wait)
    assert waits == [2.0, 4.0, 8.0, 16.0, 30.0, 30.0, 30.0, 30.0]
    records = [r for r in caplog.records if r.name == "lakana_worker.scheduler"]
    assert [r.exc_info is not None for r in records] == [True] + [False] * 7
    election.failing = False
    loop.step()
    assert loop.next_wait == pytest.approx(1.0)
    election.failing = True
    loop.step()
    assert caplog.records[-1].exc_info is not None


def test_a_huge_failure_streak_neither_overflows_nor_stops_the_thread(
    engine: sqlalchemy.Engine,
) -> None:
    """After thousands of failures the wait stays at the cap and the loop keeps running."""

    class Down(LeaderElection):
        def try_lead(self, now: datetime) -> bool:
            raise RuntimeError("database went away")

    loop = SchedulerLoop(engine, "w1", interval=1.0, election=Down(engine, "w1"))
    loop._failures = 2000
    assert loop.next_wait == pytest.approx(30.0)
    thread = loop.start()
    time.sleep(0.2)
    assert thread.is_alive()
    loop.stop()
    thread.join(timeout=5)
    assert not thread.is_alive()


def test_without_an_injected_clock_the_database_time_is_used(
    engine: sqlalchemy.Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The scheduler's `now` (heartbeat and ticks) comes from `db_now`, not the wall clock."""
    monkeypatch.setattr("lakana_worker.scheduler.db_now", lambda session: NOW)
    loop = SchedulerLoop(engine, "w1", rand=lambda: 0.5)
    assert loop.step() is True
    with Session(engine) as session:
        lease = session.get(SchedulerLease, 1)
        assert lease is not None
        assert lease.heartbeat_at == NOW
        run = session.scalars(select(Run)).one()
        assert run.started_at == NOW


@pytest.mark.parametrize("interval", [0.0001, 0.01, 1.0, 45.0])
def test_the_backoff_cap_is_reached_for_any_interval(
    engine: sqlalchemy.Engine, interval: float
) -> None:
    """A tiny interval must still back off to the cap, not stall at interval x 2^16."""
    loop = SchedulerLoop(engine, "w1", interval=interval)
    loop._failures = 40
    assert loop.next_wait == pytest.approx(max(30.0, interval))
    loop._failures = 10**6
    assert loop.next_wait == pytest.approx(max(30.0, interval))


_WAIT = 5.0
"""Longest any test thread waits; a test fails when it runs out."""


def _finished(loop: SchedulerLoop) -> None:
    """Wait for the loop's housekeeping thread to end; fail if it does not."""
    assert loop.join_housekeeping(_WAIT)


def test_housekeeping_runs_after_a_tick_at_most_once_per_interval(
    engine: sqlalchemy.Engine,
) -> None:
    seen: list[datetime] = []
    ticks = [0.0]
    loop = SchedulerLoop(
        engine,
        "w1",
        clock=lambda: NOW,
        rand=lambda: 0.5,
        housekeeping=lambda now, _stop: seen.append(now),
        housekeeping_interval=60.0,
        monotonic=lambda: ticks[0],
    )
    assert loop.step() is True
    _finished(loop)
    assert loop.step() is True
    _finished(loop)
    assert seen == [NOW]
    ticks[0] = 59.9
    loop.step()
    _finished(loop)
    assert seen == [NOW]
    ticks[0] = 60.0
    loop.step()
    _finished(loop)
    assert seen == [NOW, NOW]


def test_housekeeping_is_off_at_interval_zero_and_only_the_leader_runs_it(
    engine: sqlalchemy.Engine,
) -> None:
    seen: list[datetime] = []
    off = SchedulerLoop(
        engine,
        "w1",
        clock=lambda: NOW,
        housekeeping=lambda now, _stop: seen.append(now),
        housekeeping_interval=0.0,
    )
    assert off.step() is True
    assert off.step() is True
    _finished(off)
    follower = SchedulerLoop(
        engine,
        "w2",
        clock=lambda: NOW,
        housekeeping=lambda now, _stop: seen.append(now),
        housekeeping_interval=1.0,
    )
    assert follower.step() is False
    _finished(follower)
    assert seen == []


def test_housekeeping_follows_the_tick_it_belongs_to(engine: sqlalchemy.Engine) -> None:
    """The sweep starts after the scheduler's own work of the step."""
    order: list[str] = []
    loop = SchedulerLoop(
        engine,
        "w1",
        clock=lambda: NOW,
        rand=lambda: 0.5,
        housekeeping=lambda _now, _stop: order.append(
            f"housekeeping, run {_run_status(engine).value}"
        ),
        housekeeping_interval=1.0,
    )
    loop.step()
    _finished(loop)
    assert order == ["housekeeping, run running"]


def test_failing_housekeeping_is_logged_and_the_loop_goes_on(
    engine: sqlalchemy.Engine, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(_now: datetime, _stop: threading.Event) -> None:
        raise RuntimeError("sweep failed")

    loop = SchedulerLoop(
        engine, "w1", clock=lambda: NOW, housekeeping=broken, housekeeping_interval=1.0
    )
    with caplog.at_level(logging.ERROR):
        assert loop.step() is True
        _finished(loop)
    assert "Housekeeping failed; continuing." in caplog.text
    assert "sweep_failed" in {getattr(r, "event", None) for r in caplog.records}
    assert loop.next_wait == loop._interval


def test_a_failing_housekeeping_is_not_retried_on_every_step(engine: sqlalchemy.Engine) -> None:
    """The timer is recorded when it starts, so a failure waits out the interval too."""
    calls: list[int] = []

    def broken(_now: datetime, _stop: threading.Event) -> None:
        calls.append(1)
        raise RuntimeError("sweep failed")

    ticks = [0.0]
    loop = SchedulerLoop(
        engine,
        "w1",
        clock=lambda: NOW,
        housekeeping=broken,
        housekeeping_interval=60.0,
        monotonic=lambda: ticks[0],
    )
    for moment in (0.0, 1.0, 30.0, 59.0):
        ticks[0] = moment
        loop.step()
        _finished(loop)
    assert len(calls) == 1
    ticks[0] = 60.0
    loop.step()
    _finished(loop)
    assert len(calls) == 2


def test_a_blocked_housekeeping_neither_stalls_the_ticks_nor_starts_a_second(
    engine: sqlalchemy.Engine,
) -> None:
    started, release = threading.Event(), threading.Event()
    calls: list[int] = []

    def blocked(_now: datetime, _stop: threading.Event) -> None:
        calls.append(1)
        started.set()
        assert release.wait(_WAIT)

    ticks = [0.0]
    loop = SchedulerLoop(
        engine,
        "w1",
        clock=lambda: NOW,
        rand=lambda: 0.5,
        housekeeping=blocked,
        housekeeping_interval=1.0,
        monotonic=lambda: ticks[0],
    )
    try:
        assert loop.step() is True
        assert started.wait(_WAIT)
        for moment in (10.0, 20.0, 30.0):  # well past the interval, still running
            ticks[0] = moment
            assert loop.step() is True  # the step returns and still ticks
        assert calls == [1]
    finally:
        release.set()
    _finished(loop)
    ticks[0] = 40.0
    loop.step()
    _finished(loop)
    assert calls == [1, 1]


def test_the_stop_event_reaches_the_housekeeping_and_shutdown_waits_for_it(
    engine: sqlalchemy.Engine,
) -> None:
    started = threading.Event()
    seen: list[threading.Event] = []

    def until_stopped(_now: datetime, stop: threading.Event) -> None:
        seen.append(stop)
        started.set()
        assert stop.wait(_WAIT)

    loop = SchedulerLoop(
        engine,
        "w1",
        interval=0.01,
        clock=lambda: NOW,
        housekeeping=until_stopped,
        housekeeping_interval=1.0,
    )
    thread = loop.start()
    assert started.wait(_WAIT)
    loop.stop()
    thread.join(_WAIT)
    assert not thread.is_alive()
    assert seen[0].is_set()
    assert loop.join_housekeeping(_WAIT)


def test_shutdown_without_housekeeping_has_nothing_to_wait_for(engine: sqlalchemy.Engine) -> None:
    assert SchedulerLoop(engine, "w1").join_housekeeping(0.01) is True


def test_a_housekeeping_that_ignores_the_stop_event_is_reported_at_shutdown(
    engine: sqlalchemy.Engine, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("lakana_worker.scheduler.HOUSEKEEPING_JOIN_SECONDS", 0.05)
    started, release = threading.Event(), threading.Event()

    def deaf(_now: datetime, _stop: threading.Event) -> None:
        started.set()
        release.wait(_WAIT)

    loop = SchedulerLoop(
        engine, "w1", interval=0.01, clock=lambda: NOW, housekeeping=deaf, housekeeping_interval=1.0
    )
    thread = loop.start()
    try:
        assert started.wait(_WAIT)
        with caplog.at_level(logging.WARNING):
            loop.stop()
            thread.join(_WAIT)
        assert not thread.is_alive()
        assert "housekeeping thread did not stop" in caplog.text
    finally:
        release.set()
    _finished(loop)


def test_a_housekeeping_that_hangs_is_reported_once(
    engine: sqlalchemy.Engine, caplog: pytest.LogCaptureFixture
) -> None:
    started, release = threading.Event(), threading.Event()

    def hung(_now: datetime, _stop: threading.Event) -> None:
        started.set()
        assert release.wait(_WAIT)

    ticks = [0.0]
    loop = SchedulerLoop(
        engine,
        "w1",
        clock=lambda: NOW,
        rand=lambda: 0.5,
        housekeeping=hung,
        housekeeping_interval=10.0,
        monotonic=lambda: ticks[0],
        housekeeping_limit=100.0,
    )
    try:
        with caplog.at_level(logging.WARNING):
            loop.step()
            assert started.wait(_WAIT)
            for moment in (10.0, 150.0, 200.0):  # due, but not yet past twice the limit
                ticks[0] = moment
                loop.step()
            assert "Housekeeping has been running" not in caplog.text
            for moment in (201.0, 300.0, 400.0):  # past 200 s: reported, once
                ticks[0] = moment
                loop.step()
        reported = [r.getMessage() for r in caplog.records if "has been running" in r.getMessage()]
        assert "sweep_hung" in {getattr(r, "event", None) for r in caplog.records}
        assert reported == [
            "Housekeeping has been running for 201 s, more than twice its limit of 100 s, so no "
            "further run starts until it ends; it may be waiting on storage or the database."
        ]
    finally:
        release.set()
    _finished(loop)


def test_a_new_hung_run_is_reported_again(engine: sqlalchemy.Engine) -> None:
    """The report is once per hung run, not once for ever."""
    release = threading.Event()
    calls: list[int] = []

    def hung(_now: datetime, _stop: threading.Event) -> None:
        calls.append(1)
        assert release.wait(_WAIT)

    ticks = [0.0]
    loop = SchedulerLoop(
        engine,
        "w1",
        clock=lambda: NOW,
        housekeeping=hung,
        housekeeping_interval=10.0,
        monotonic=lambda: ticks[0],
        housekeeping_limit=1.0,
    )
    try:
        loop.step()
        ticks[0] = 50.0
        loop.step()
        assert loop._hang_reported is True
        release.set()
        _finished(loop)
        release.clear()
        ticks[0] = 100.0
        loop.step()
        assert loop._hang_reported is False
        assert len(calls) == 2
    finally:
        release.set()
    _finished(loop)


class _Clock:
    """A settable clock, in database time."""

    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        """Return the current test time.

        Returns
        -------
        datetime
            The time last set.
        """
        return self.now

    def at(self, seconds: float) -> datetime:
        """Set the time to `NOW` plus that many seconds and return it.

        Parameters
        ----------
        seconds
            Offset from `NOW`.

        Returns
        -------
        datetime
            The new time.
        """
        self.now = NOW + timedelta(seconds=seconds)
        return self.now


def test_a_step_hands_the_lease_to_the_tick(
    engine: sqlalchemy.Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[object, ...]] = []

    def record(*args: object, **kwargs: object) -> int:
        seen.append(args)
        return 0

    monkeypatch.setattr(scheduler_module, "scheduler_tick", record)
    clock = _Clock()
    loop = SchedulerLoop(engine, "w1", clock=clock, lease_seconds=60)
    clock.at(0)
    assert loop.step() is True  # a new leader's first tick is a late one: no sweep
    clock.at(1)
    assert loop.step() is True
    clock.at(26)  # the quiet period (renew 20 + 5 s) is over
    assert loop.step() is True
    other = SchedulerLoop(engine, "w1", clock=clock)  # no lease: never a sweep
    assert other.step() is True
    assert [args[1:2] + args[3:] for args in seen] == [
        (NOW, None),
        (NOW + timedelta(seconds=1), None),
        (NOW + timedelta(seconds=26), 60),
        (NOW + timedelta(seconds=26), None),
    ]


def test_the_tick_gets_the_databases_time_when_no_clock_is_injected(
    engine: sqlalchemy.Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R4.3: the sweep's ages are the database's, never this host's wall clock."""
    seen: list[datetime] = []
    database_time = datetime(2031, 1, 2, 3, 4, 5, tzinfo=UTC)
    monkeypatch.setattr(scheduler_module, "db_now", lambda session: database_time)
    monkeypatch.setattr(
        scheduler_module, "scheduler_tick", lambda session, now, *_, **__: seen.append(now) or 0
    )
    SchedulerLoop(engine, "w1", lease_seconds=60).step()
    assert seen == [database_time]


def _lost_run(engine: sqlalchemy.Engine) -> uuid.UUID:
    """Insert a `RUNNING` query run claimed by a worker that has no row.

    Parameters
    ----------
    engine
        The engine.

    Returns
    -------
    uuid.UUID
        The run's id.
    """
    with Session(engine) as session:
        env = session.scalars(select(Environment)).one()
        run = QueryRun(
            id=uuid.uuid4(),
            environment_id=env.id,
            sql_text="SELECT 1",
            status=QueryRunStatus.RUNNING,
            worker_id="gone",
        )
        session.add(run)
        session.commit()
        return run.id


def _query_status(engine: sqlalchemy.Engine, run_id: uuid.UUID) -> QueryRunStatus:
    """Read a query run's status.

    Parameters
    ----------
    engine
        The engine.
    run_id
        The run.

    Returns
    -------
    QueryRunStatus
        Its status.
    """
    with Session(engine) as session:
        return session.scalars(select(QueryRun.status).where(QueryRun.id == run_id)).one()


def _old_heartbeat(engine: sqlalchemy.Engine, run_id: uuid.UUID) -> None:
    """Give the run a worker whose heartbeat is 70 s old at `NOW`.

    Parameters
    ----------
    engine
        The engine.
    run_id
        The run to hand to that worker.
    """
    with Session(engine) as session:
        beat = NOW - timedelta(seconds=70)
        session.add(Worker(id="slow", host="h", version="1", started_at=beat, heartbeat_at=beat))
        session.flush()
        session.execute(update(QueryRun).where(QueryRun.id == run_id).values(worker_id="slow"))
        session.commit()


def test_only_the_leader_sweeps_and_only_with_a_lease(engine: sqlalchemy.Engine) -> None:
    run_id = _lost_run(engine)
    clock = _Clock()
    leader = SchedulerLoop(engine, "w1", clock=clock, rand=lambda: 0.5, lease_seconds=60)
    follower = SchedulerLoop(engine, "w2", clock=clock, rand=lambda: 0.5, lease_seconds=60)
    without = SchedulerLoop(engine, "w1", clock=clock, rand=lambda: 0.5)
    for second in (0, 1, 26, 27):
        clock.at(second)
        assert without.step() is True  # no lease, no sweep
    assert _query_status(engine, run_id) is QueryRunStatus.RUNNING
    assert follower.step() is False  # w1 holds the role: a follower sweeps nothing
    assert _query_status(engine, run_id) is QueryRunStatus.RUNNING
    clock.at(0)
    assert leader.step() is True  # a new leader's first tick is late: no sweep
    clock.at(26)
    assert leader.step() is True  # a gap of 26 s is under half the lease: sweeps
    assert _query_status(engine, run_id) is QueryRunStatus.FAILED


def test_after_a_stall_the_leader_lets_the_workers_beat_before_it_sweeps(
    engine: sqlalchemy.Engine,
) -> None:
    """I4: a worker's next heartbeat can be `renew` seconds away when the next tick is 1 s away."""
    run_id = _lost_run(engine)
    _old_heartbeat(engine, run_id)
    clock = _Clock()
    loop = SchedulerLoop(
        engine, "w1", clock=clock, rand=lambda: 0.5, lease_seconds=60, renew_seconds=20
    )
    clock.at(100)
    assert loop.step() is True  # a new leader: counts as a late tick
    for second in (101, 110, 124):
        clock.at(second)
        loop.step()
        assert _query_status(engine, run_id) is QueryRunStatus.RUNNING, second
    clock.at(125)  # renew + 5 = 25 s after the late tick at 100, and the heartbeat is 70+ s old
    loop.step()
    assert _query_status(engine, run_id) is QueryRunStatus.FAILED


def test_a_gap_of_more_than_half_the_lease_between_two_ticks_pauses_the_sweep(
    engine: sqlalchemy.Engine,
) -> None:
    clock = _Clock()
    loop = SchedulerLoop(
        engine, "w1", clock=clock, rand=lambda: 0.5, lease_seconds=60, renew_seconds=20
    )
    for second in (0, 25, 26):  # quiet ends at 25, a steady rhythm follows
        clock.at(second)
        loop.step()
    first = _lost_run(engine)
    _old_heartbeat(engine, first)
    clock.at(27)  # a steady rhythm: swept at once, as before
    loop.step()
    assert _query_status(engine, first) is QueryRunStatus.FAILED
    second_run = _lost_run(engine)
    with Session(engine) as session:
        session.execute(update(QueryRun).where(QueryRun.id == second_run).values(worker_id="slow"))
        session.commit()
    clock.at(27 + 32)  # a gap of 32 s less the 1 s interval is more than half the lease
    loop.step()
    assert _query_status(engine, second_run) is QueryRunStatus.RUNNING
    clock.at(27 + 32 + 24)
    loop.step()
    assert _query_status(engine, second_run) is QueryRunStatus.RUNNING
    clock.at(27 + 32 + 25)
    loop.step()
    assert _query_status(engine, second_run) is QueryRunStatus.FAILED


def test_a_gap_of_exactly_half_the_lease_is_not_a_stall(engine: sqlalchemy.Engine) -> None:
    clock = _Clock()
    loop = SchedulerLoop(engine, "w1", clock=clock, rand=lambda: 0.5, lease_seconds=60)
    for second in (0, 25):
        clock.at(second)
        loop.step()
    run_id = _lost_run(engine)
    clock.at(55)  # exactly 30 s after the previous tick
    loop.step()
    assert _query_status(engine, run_id) is QueryRunStatus.FAILED


def test_the_pause_is_measured_in_database_time_not_on_this_hosts_clock(
    engine: sqlalchemy.Engine,
) -> None:
    """The injected (database) clock jumps 45 s while the host's clock hardly moves."""
    clock = _Clock()
    loop = SchedulerLoop(engine, "w1", clock=clock, rand=lambda: 0.5, lease_seconds=60)
    run_id = _lost_run(engine)
    for second in (0, 25, 26):
        clock.at(second)
        loop.step()
    assert _query_status(engine, run_id) is QueryRunStatus.FAILED
    second_run = _lost_run(engine)
    clock.at(26 + 45)
    loop.step()
    assert _query_status(engine, second_run) is QueryRunStatus.RUNNING


def test_workers_silent_for_an_hour_are_forgotten_at_most_once_a_minute(
    engine: sqlalchemy.Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    forgot: list[bool] = []
    monkeypatch.setattr(
        scheduler_module,
        "scheduler_tick",
        lambda session, now, rand, lease, **kw: forgot.append(kw["forget"]) or 0,
    )
    clock = _Clock()
    loop = SchedulerLoop(engine, "w1", clock=clock, lease_seconds=60)
    for second in (0, 25, 26, 45, 65, 84, 85):
        clock.at(second)
        loop.step()
    # the first tick is quiet (no lease, no forgetting); 25 forgets; nothing again until a minute
    # of database time has passed since 25
    assert forgot == [False, True, False, False, False, False, True]


def test_an_interval_of_more_than_half_the_lease_still_sweeps(engine: sqlalchemy.Engine) -> None:
    """N1: the loop's own wait is not a stall; 31 s ticks under a 60 s lease must sweep."""
    run_id = _lost_run(engine)
    clock = _Clock()
    loop = SchedulerLoop(
        engine, "w1", clock=clock, rand=lambda: 0.5, lease_seconds=60, interval=31.0
    )
    for tick in range(40):
        clock.at(tick * 31)
        loop.step()
        if _query_status(engine, run_id) is QueryRunStatus.FAILED:
            break
    assert _query_status(engine, run_id) is QueryRunStatus.FAILED
    assert tick <= 2  # after the new leader's quiet period, not never


def test_a_one_second_lease_margin_interval_still_sweeps(engine: sqlalchemy.Engine) -> None:
    """N1: lease 2, renew 1, ticks 1.01 s apart."""
    run_id = _lost_run(engine)
    clock = _Clock()
    loop = SchedulerLoop(
        engine, "w1", clock=clock, rand=lambda: 0.5, lease_seconds=2, renew_seconds=1, interval=1.0
    )
    for tick in range(20):
        clock.at(tick * 1.01)
        loop.step()
    assert _query_status(engine, run_id) is QueryRunStatus.FAILED


def test_a_stall_is_still_a_stall_at_the_default_interval(engine: sqlalchemy.Engine) -> None:
    run_id = _lost_run(engine)
    clock = _Clock()
    loop = SchedulerLoop(engine, "w1", clock=clock, rand=lambda: 0.5, lease_seconds=60)
    for second in (0, 25, 26):
        clock.at(second)
        loop.step()
    with Session(engine) as session:
        session.execute(
            update(QueryRun).where(QueryRun.id == run_id).values(status=QueryRunStatus.RUNNING)
        )
        session.commit()
    clock.at(26 + 45)  # 45 s minus the 1 s interval is above 30 s
    loop.step()
    assert _query_status(engine, run_id) is QueryRunStatus.RUNNING


@pytest.mark.parametrize(("away", "late"), [(44, True), (20, False)])
def test_a_leader_that_took_the_role_back_is_late_only_after_a_long_gap(
    engine: sqlalchemy.Engine, away: int, late: bool
) -> None:
    """N7: the rule is the time since this loop's previous LEADER tick, not the flap itself."""
    clock = _Clock()
    loop = SchedulerLoop(engine, "w1", clock=clock, rand=lambda: 0.5, lease_seconds=60)
    for second in (0, 25, 26):
        clock.at(second)
        loop.step()
    run_id = _lost_run(engine)  # a lost run: swept at once unless the next tick is late
    clock.at(26 + away)
    loop.step()
    expected = QueryRunStatus.RUNNING if late else QueryRunStatus.FAILED
    assert _query_status(engine, run_id) is expected


def test_a_leader_tick_fires_due_jobs_after_the_run_engine_and_then_starts_waiting_ones(
    engine: sqlalchemy.Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    real = scheduler_module.scheduler_tick

    def tick(*args: object, **kwargs: object) -> int:
        calls.append("tick")
        return real(*args, **kwargs)  # ty: ignore[invalid-argument-type]

    def fire(session: Session, now: datetime, **kwargs: object) -> list[object]:
        calls.append(f"fire {now.isoformat()} {sorted(kwargs)}")
        return []

    def waiting(session: Session, **kwargs: object) -> list[object]:
        calls.append("waiting")
        return []

    monkeypatch.setattr(scheduler_module, "scheduler_tick", tick)
    monkeypatch.setattr(scheduler_module, "fire_due", fire)
    monkeypatch.setattr(scheduler_module, "start_waiting_jobs", waiting)
    leader = SchedulerLoop(engine, "w1", clock=lambda: NOW, rand=lambda: 0.5, lease_seconds=60)
    follower = SchedulerLoop(engine, "w2", clock=lambda: NOW, rand=lambda: 0.5)
    assert leader.step() is True
    assert follower.step() is False
    assert calls[0] == "tick" and calls[1].startswith(f"fire {NOW.isoformat()}")
    assert "lease_seconds" in calls[1] and calls[2] == "waiting" and len(calls) == 3


def test_a_failing_job_step_is_logged_and_does_not_fail_the_tick(
    engine: sqlalchemy.Engine, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    started: list[bool] = []

    def broken(session: Session, now: datetime, **kwargs: object) -> list[object]:
        raise RuntimeError("jobs table missing")

    def waiting(session: Session, **kwargs: object) -> list[object]:
        started.append(True)
        return []

    monkeypatch.setattr(scheduler_module, "fire_due", broken)
    monkeypatch.setattr(scheduler_module, "start_waiting_jobs", waiting)
    loop = SchedulerLoop(engine, "w1", clock=lambda: NOW, rand=lambda: 0.5)
    with caplog.at_level(logging.ERROR):
        assert loop.step() is True
    assert "jobs table missing" in caplog.text and started == [True]
    assert loop.next_wait == loop._interval


def test_the_job_steps_share_one_time_budget_below_the_leaders_stale_time(
    engine: sqlalchemy.Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    limits: list[float] = []

    def fire(session: Session, now: datetime, **kwargs: object) -> list[object]:
        limits.append(float(kwargs["time_limit"]))  # ty: ignore[invalid-argument-type]
        return []

    def waiting(session: Session, **kwargs: object) -> list[object]:
        limits.append(float(kwargs["time_limit"]))  # ty: ignore[invalid-argument-type]
        return []

    monkeypatch.setattr(scheduler_module, "fire_due", fire)
    monkeypatch.setattr(scheduler_module, "start_waiting_jobs", waiting)
    SchedulerLoop(engine, "w1", clock=lambda: NOW, rand=lambda: 0.5).step()
    assert (
        len(limits) == 2 and limits[0] > 0 and limits[0] + limits[1] < STALE_AFTER.total_seconds()
    )


def test_a_failing_waiting_start_step_is_logged_and_does_not_fail_the_tick(
    engine: sqlalchemy.Engine, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(session: Session, **kwargs: object) -> list[object]:
        raise RuntimeError("waiting starts unreadable")

    monkeypatch.setattr(scheduler_module, "start_waiting_jobs", broken)
    loop = SchedulerLoop(engine, "w1", clock=lambda: NOW, rand=lambda: 0.5)
    with caplog.at_level(logging.ERROR):
        assert loop.step() is True
    assert "waiting starts unreadable" in caplog.text


def test_taking_and_losing_the_role_is_logged_once_each(
    engine: sqlalchemy.Engine, caplog: pytest.LogCaptureFixture
) -> None:
    """A follower logs nothing; the leader logs when it takes the role and when it loses it."""
    leads = [True]

    class Switching(LeaderElection):
        def try_lead(self, now: datetime) -> bool:
            return leads[0] and super().try_lead(now)

    loop = SchedulerLoop(engine, "w1", clock=lambda: NOW, election=Switching(engine, "w1"))
    with caplog.at_level(logging.INFO, logger="lakana_worker.scheduler"):
        assert loop.step() is True
        assert loop.step() is True
        leads[0] = False
        assert loop.step() is False
        assert loop.step() is False

    roles = [
        (r.levelno, r.event)  # ty: ignore[missing-attribute]
        for r in caplog.records
        if getattr(r, "event", "").startswith("scheduler_role")
    ]
    assert roles == [
        (logging.INFO, "scheduler_role_taken"),
        (logging.WARNING, "scheduler_role_lost"),
    ]
