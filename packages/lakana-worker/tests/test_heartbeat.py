"""Tests for the worker's heartbeat."""

from __future__ import annotations

import logging
import math
import threading
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from lakana_core.config import DatabaseConfig
from lakana_core.db import create_engine
from lakana_core.models import Base, Worker
from lakana_core.runs.workers import CacheSizes, MemoryUse
from lakana_worker.heartbeat import RETRY_SECONDS, WorkerHeartbeat

_THREAD_BOUND_SECONDS = 10.0


class _Clock:
    """A monotonic clock a test moves by hand."""

    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        """Return the current reading.

        Returns
        -------
        float
            The reading.
        """
        return self.now


def _heartbeat(
    session: Session,
    clock: _Clock,
    *,
    fail: list[bool] | None = None,
    takes: str = "all",
    caches: Callable[[], CacheSizes | None] | None = None,
    memory: Callable[[], MemoryUse | None] | None = None,
) -> WorkerHeartbeat:
    """Build a heartbeat on the test's session; `fail[0]` makes the next writes raise.

    Parameters
    ----------
    session
        The session every write uses.
    clock
        The heartbeat's monotonic clock.
    fail
        A one-element list; while its element is true the session factory raises.
    takes
        What the worker takes.
    caches
        Returns the latest cache sizes.
    memory
        Returns the latest memory use.

    Returns
    -------
    WorkerHeartbeat
        Worker `host:1:ab`, renew 20 s, lease 60 s.
    """

    @contextmanager
    def factory() -> Generator[Session, None, None]:
        if fail and fail[0]:
            raise OperationalError("UPDATE workers", {}, Exception("connection refused"))
        yield session

    return WorkerHeartbeat(
        factory,
        "host:1:ab",
        host="host",
        version="0.12.0",
        renew_seconds=20,
        lease_seconds=60,
        takes=takes,
        monotonic=clock,
        caches=caches,
        memory=memory,
    )


def test_nothing_may_be_claimed_before_the_first_registration(session: Session) -> None:
    heartbeat = _heartbeat(session, _Clock())
    assert heartbeat.stale() and not heartbeat.may_claim()
    assert heartbeat.beat_once()
    assert heartbeat.may_claim()
    row = session.get(Worker, "host:1:ab")
    assert row is not None
    assert (row.host, row.version) == ("host", "0.12.0")


def test_a_worker_silent_for_a_lease_claims_nothing_and_stops_its_query(session: Session) -> None:
    clock, fail = _Clock(), [False]
    heartbeat = _heartbeat(session, clock, fail=fail)
    assert heartbeat.beat_once()
    fail[0] = True
    clock.now += 59.0
    assert not heartbeat.beat_once()
    assert heartbeat.may_claim()  # the lease is not over: nothing declares the worker lost
    assert not heartbeat.stale()
    clock.now += 1.0
    assert not heartbeat.may_claim()
    assert heartbeat.stale()  # what the query supervisor asks (`install_heartbeat`)


def test_a_heartbeat_after_an_outage_lets_the_worker_claim_again(session: Session) -> None:
    clock, fail = _Clock(), [False]
    heartbeat = _heartbeat(session, clock, fail=fail)
    heartbeat.beat_once()
    fail[0] = True
    clock.now += 300.0
    heartbeat.beat_once()
    assert not heartbeat.may_claim()
    fail[0] = False
    assert heartbeat.beat_once()
    assert heartbeat.may_claim()


def test_a_failed_write_is_logged_once_with_its_cause_then_one_line_each(
    session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    fail = [True]
    heartbeat = _heartbeat(session, _Clock(), fail=fail)
    with caplog.at_level(logging.INFO, logger="lakana_worker.heartbeat"):
        heartbeat.beat_once()
        heartbeat.beat_once()
        fail[0] = False
        heartbeat.beat_once()
    first, second, third = caplog.records
    assert first.levelno == logging.ERROR and first.exc_info is not None
    assert "tried again every 5 s" in first.getMessage()
    assert second.levelno == logging.WARNING and second.exc_info is None
    assert "2 in a row" in second.getMessage()
    assert third.getMessage() == "The worker's heartbeat was written again after 2 failed attempts."
    assert [getattr(r, "event", None) for r in caplog.records] == [
        "database_unreachable",
        "database_unreachable",
        None,
    ]


def test_every_outage_is_logged_once_with_its_cause_and_once_recovered(
    session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    fail = [False]
    heartbeat = _heartbeat(session, _Clock(), fail=fail)
    with caplog.at_level(logging.INFO, logger="lakana_worker.heartbeat"):
        for outcome in (True, True, False, True, True, False):
            fail[0] = outcome
            heartbeat.beat_once()
    assert [r.levelno for r in caplog.records] == [
        logging.ERROR,
        logging.WARNING,
        logging.INFO,
        logging.ERROR,
        logging.WARNING,
        logging.INFO,
    ]
    assert caplog.records[5].getMessage().endswith("after 2 failed attempts.")


def test_the_first_failure_before_any_registration_says_nothing_is_claimed(
    session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    fail = [True]
    heartbeat = _heartbeat(session, _Clock(), fail=fail)
    with caplog.at_level(logging.INFO, logger="lakana_worker.heartbeat"):
        heartbeat.beat_once()
    assert "claims nothing until its first heartbeat is written" in caplog.records[0].getMessage()
    fail[0] = False
    heartbeat.beat_once()
    fail[0] = True
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="lakana_worker.heartbeat"):
        heartbeat.beat_once()
    assert "claims nothing until" not in caplog.records[0].getMessage()


def test_a_write_that_takes_time_does_not_make_the_worker_look_fresher(session: Session) -> None:
    """The success time is read before the write is sent, as the database's is."""
    clock = _Clock()

    @contextmanager
    def slow() -> Generator[Session, None, None]:
        clock.now += 30.0  # the write takes 30 s
        yield session

    heartbeat = WorkerHeartbeat(
        slow,
        "host:1:ab",
        host="host",
        version="0.12.0",
        renew_seconds=20,
        lease_seconds=60,
        monotonic=clock,
    )
    assert heartbeat.beat_once()
    assert not heartbeat.stale()  # 30 s since the reading before the write
    clock.now += 30.0
    assert heartbeat.stale()  # 60 s since it, not 30 s since the write's end


def test_turning_stale_and_fresh_again_is_logged_once_each(
    session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    clock = _Clock()
    heartbeat = _heartbeat(session, clock)
    with caplog.at_level(logging.INFO, logger="lakana_worker.heartbeat"):
        assert heartbeat.stale()  # never written: no transition to report
        heartbeat.beat_once()
        clock.now += 60.0
        assert heartbeat.stale() and heartbeat.stale() and not heartbeat.may_claim()
        stale_lines = [r.getMessage() for r in caplog.records]
        heartbeat.beat_once()
        assert not heartbeat.stale()
        clock.now += 60.0
        assert heartbeat.stale()
    assert len(stale_lines) == 1
    assert caplog.records[0].event == "heartbeat_missed"  # ty: ignore[unresolved-attribute]
    assert caplog.records[1].event == "heartbeat_recovered"  # ty: ignore[unresolved-attribute]
    assert "claims nothing and stops the query it runs" in stale_lines[0]
    assert "60 s" in stale_lines[0]
    messages = [r.getMessage() for r in caplog.records]
    assert messages[1] == "The worker's heartbeat is fresh again; it claims work again."
    assert len(messages) == 3 and "claims nothing" in messages[2]


def test_a_removed_row_is_registered_again_with_its_first_start_time(session: Session) -> None:
    heartbeat = _heartbeat(session, _Clock())
    heartbeat.beat_once()
    row = session.get(Worker, "host:1:ab")
    assert row is not None
    started = row.started_at
    session.execute(delete(Worker))
    session.commit()
    assert heartbeat.beat_once()
    again = session.get(Worker, "host:1:ab")
    assert again is not None
    assert again.started_at == started


def test_the_heartbeat_registers_what_the_worker_takes(session: Session) -> None:
    heartbeat = _heartbeat(session, _Clock(), takes="pipeline")
    assert heartbeat.beat_once()
    session.expire_all()
    assert session.scalars(select(Worker.takes)).one() == "pipeline"


def test_a_worker_whose_row_was_removed_registers_its_takes_again(session: Session) -> None:
    heartbeat = _heartbeat(session, _Clock(), takes="interactive")
    heartbeat.beat_once()
    session.execute(delete(Worker))
    session.commit()
    assert heartbeat.beat_once()
    session.expire_all()
    assert session.scalars(select(Worker.takes)).one() == "interactive"


def test_the_thread_beats_every_renew_interval_and_every_five_seconds_after_a_failure(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    heartbeat = _heartbeat(session, _Clock())
    outcomes = iter([True, False, True])
    monkeypatch.setattr(heartbeat, "beat_once", lambda: next(outcomes))
    waits: list[float] = []

    def wait(seconds: float) -> bool:
        waits.append(seconds)
        if len(waits) == 3:
            heartbeat.stop()
        return False

    monkeypatch.setattr(heartbeat._stop, "wait", wait)
    heartbeat.run()
    assert waits == [20, RETRY_SECONDS, 20]
    assert math.isclose(RETRY_SECONDS, 5.0)


def test_stop_ends_the_thread(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine = create_engine(DatabaseConfig(url=f"sqlite:///{tmp_path}/hb.db"))
    Base.metadata.create_all(engine)
    heartbeat = WorkerHeartbeat(
        lambda: Session(engine),
        "host:1:ab",
        host="host",
        version="0.12.0",
        renew_seconds=3600,
        lease_seconds=7200,
    )
    first_beat = threading.Event()
    real = heartbeat.beat_once

    def beat() -> bool:
        result = real()
        first_beat.set()
        return result

    monkeypatch.setattr(heartbeat, "beat_once", beat)
    thread = heartbeat.start()
    try:
        assert first_beat.wait(_THREAD_BOUND_SECONDS)
        assert thread.daemon and thread.is_alive()
        with Session(engine) as other:
            assert other.scalars(select(Worker.id)).all() == ["host:1:ab"]
    finally:
        heartbeat.stop()
        thread.join(_THREAD_BOUND_SECONDS)
        engine.dispose()
    assert not thread.is_alive()
    assert [t for t in threading.enumerate() if t.name == "lakana-heartbeat"] == []


def test_unregister_deletes_the_row(session: Session) -> None:
    heartbeat = _heartbeat(session, _Clock())
    heartbeat.beat_once()
    heartbeat.unregister()
    assert session.get(Worker, "host:1:ab") is None


def test_unregister_that_fails_is_logged_and_does_not_raise(
    session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    fail = [False]
    heartbeat = _heartbeat(session, _Clock(), fail=fail)
    heartbeat.beat_once()
    fail[0] = True
    with caplog.at_level(logging.WARNING, logger="lakana_worker.heartbeat"):
        heartbeat.unregister()
    (record,) = caplog.records
    assert "shown as lost for an hour and then removed" in record.getMessage()
    assert record.exc_info is not None


def test_the_heartbeat_stores_new_cache_sizes_and_again_after_registering(session: Session) -> None:
    latest: list[CacheSizes | None] = [None]
    heartbeat = _heartbeat(session, _Clock(), caches=lambda: latest[0])
    assert heartbeat.beat_once()
    assert session.scalars(select(Worker.cache_measured_at)).one() is None
    latest[0] = CacheSizes(environments_bytes=1, packages_bytes=2, tasks_bytes=3, complete=True)
    assert heartbeat.beat_once()
    session.expire_all()
    row = session.scalars(select(Worker)).one()
    assert (row.cache_packages_bytes, row.cache_complete) == (2, True)
    first = row.cache_measured_at
    assert first is not None
    session.execute(delete(Worker))
    session.commit()
    assert heartbeat.beat_once()
    session.expire_all()
    assert session.scalars(select(Worker.cache_packages_bytes)).one() == 2
    latest[0] = CacheSizes(environments_bytes=1, packages_bytes=5, tasks_bytes=3, complete=True)
    assert heartbeat.beat_once()
    session.expire_all()
    assert session.scalars(select(Worker.cache_packages_bytes)).one() == 5


def test_cache_sizes_that_could_not_be_written_are_written_with_the_next_beat(
    session: Session,
) -> None:
    sizes = CacheSizes(environments_bytes=1, packages_bytes=2, tasks_bytes=3, complete=True)
    fail = [False]
    heartbeat = _heartbeat(session, _Clock(), fail=fail, caches=lambda: sizes)
    heartbeat.beat_once()
    session.execute(delete(Worker))
    session.commit()
    fail[0] = True
    assert not heartbeat.beat_once()
    fail[0] = False
    assert heartbeat.beat_once()
    session.expire_all()
    assert session.scalars(select(Worker.cache_packages_bytes)).one() == 2


_MIB = 1024 * 1024


def _stored_memory(session: Session) -> tuple[int | None, int | None]:
    """Read the memory figures of the one worker row.

    Parameters
    ----------
    session
        The test's session.

    Returns
    -------
    tuple of (int or None, int or None)
        Used and limit bytes as stored.
    """
    session.expire_all()
    row = session.scalars(select(Worker)).one()
    return row.memory_used_bytes, row.memory_limit_bytes


def test_heartbeat_sends_the_watchdogs_latest_memory(session: Session) -> None:
    latest: list[MemoryUse | None] = [None]
    heartbeat = _heartbeat(session, _Clock(), memory=lambda: latest[0])
    assert heartbeat.beat_once()
    assert _stored_memory(session) == (None, None)
    latest[0] = MemoryUse(used_bytes=100 * _MIB, limit_bytes=4096 * _MIB)
    assert heartbeat.beat_once()
    assert _stored_memory(session) == (100 * _MIB, 4096 * _MIB)


def test_heartbeat_writes_memory_only_after_a_change_of_a_mebibyte(session: Session) -> None:
    latest = [MemoryUse(used_bytes=100 * _MIB, limit_bytes=4096 * _MIB)]
    heartbeat = _heartbeat(session, _Clock(), memory=lambda: latest[0])
    assert heartbeat.beat_once()
    latest[0] = MemoryUse(used_bytes=101 * _MIB - 1, limit_bytes=4096 * _MIB)
    assert heartbeat.beat_once()
    assert _stored_memory(session) == (100 * _MIB, 4096 * _MIB)
    latest[0] = MemoryUse(used_bytes=99 * _MIB, limit_bytes=4096 * _MIB)
    assert heartbeat.beat_once()
    assert _stored_memory(session) == (99 * _MIB, 4096 * _MIB)
    latest[0] = MemoryUse(used_bytes=99 * _MIB, limit_bytes=None)
    assert heartbeat.beat_once()
    assert _stored_memory(session) == (99 * _MIB, None)


def test_memory_is_written_again_with_a_new_registration(session: Session) -> None:
    latest = MemoryUse(used_bytes=5 * _MIB, limit_bytes=None)
    heartbeat = _heartbeat(session, _Clock(), memory=lambda: latest)
    assert heartbeat.beat_once()
    session.execute(delete(Worker))
    session.commit()
    assert heartbeat.beat_once()
    assert _stored_memory(session) == (5 * _MIB, None)
