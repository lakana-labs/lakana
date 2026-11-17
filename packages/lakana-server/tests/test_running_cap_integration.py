"""On Postgres: what the unit tier cannot show about the cap on a principal's queries.

SQLite has one writer, so the unit tests can only show that the start update carries the cap.
Here two real connections claim runs of one principal at the same moment: the advisory lock
makes the second wait for the first, and the second then counts the first's run. No worker runs
in this module: the tests call `poller._next_query` and `pipeline._start` themselves. Every
wait has a bound.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from lakana_core.models import (
    Environment,
    Principal,
    PrincipalType,
    QueryOrigin,
    QueryRun,
    QueryRunStatus,
)
from lakana_core.runs import pools
from lakana_worker import pipeline, poller
from tests.integration_helpers import backend_pid, wait_for_lock_waiter
from tests.python_asset_helpers import QueryStack, query_stack

pytestmark = pytest.mark.integration

_WAIT_SECONDS = 10.0
_LONG_AGO = datetime(2000, 1, 1, tzinfo=UTC)
"""Queued test runs are older than any other, so the dequeue meets them first."""


@pytest.fixture(scope="module")
def stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[QueryStack]:
    """Start Postgres, SeaweedFS and a real HTTP server.

    Parameters
    ----------
    tmp_path_factory
        For the repo root.

    Yields
    ------
    QueryStack
        The stack.
    """
    with query_stack(tmp_path_factory) as built:
        yield built


@pytest.fixture(scope="module")
def environment_id(stack: QueryStack) -> uuid.UUID:
    """Create an environment and return its id.

    Parameters
    ----------
    stack
        The stack.

    Returns
    -------
    uuid.UUID
        The environment's id.
    """
    name = stack.environment()
    with Session(stack.db) as session:
        return session.execute(select(Environment.id).where(Environment.name == name)).scalar_one()


@dataclass
class World:
    """Principals and queued runs a test made, removed afterwards.

    Parameters
    ----------
    stack
        The stack.
    environment_id
        The environment of every run.
    principals
        The ids of the principals made.
    runs
        The ids of the runs made.
    """

    stack: QueryStack
    environment_id: uuid.UUID
    principals: list[uuid.UUID] = field(default_factory=list)
    runs: list[uuid.UUID] = field(default_factory=list)

    def principal(self) -> uuid.UUID:
        """Insert a user.

        Returns
        -------
        uuid.UUID
            Its id.
        """
        principal_id = uuid.uuid4()
        with Session(self.stack.db) as session:
            session.add(
                Principal(id=principal_id, type=PrincipalType.USER, name=f"cap-{principal_id.hex}")
            )
            session.commit()
        self.principals.append(principal_id)
        return principal_id

    def queued(self, principal_id: uuid.UUID, n: int = 1, first_age: int = 0) -> list[uuid.UUID]:
        """Insert queued ad-hoc runs of a principal.

        Parameters
        ----------
        principal_id
            The submitter.
        n
            How many.
        first_age
            Seconds after the start of the century that the first was created; later ones follow
            one second apart.

        Returns
        -------
        list[uuid.UUID]
            Their ids, oldest first.
        """
        ids = [uuid.uuid4() for _ in range(n)]
        with Session(self.stack.db) as session:
            for offset, run_id in enumerate(ids):
                session.add(
                    QueryRun(
                        origin=QueryOrigin.WRITTEN,
                        id=run_id,
                        environment_id=self.environment_id,
                        principal_id=principal_id,
                        sql_text="SELECT 1",
                        status=QueryRunStatus.QUEUED,
                        created_at=_LONG_AGO + timedelta(seconds=first_age + offset),
                    )
                )
            session.commit()
        self.runs.extend(ids)
        return ids

    def states(self, ids: list[uuid.UUID]) -> dict[uuid.UUID, tuple[QueryRunStatus, str | None]]:
        """Read status and worker id on a fresh session.

        Parameters
        ----------
        ids
            The runs.

        Returns
        -------
        dict
            Each run's status and worker id.
        """
        with Session(self.stack.db) as session:
            rows = session.execute(
                select(QueryRun.id, QueryRun.status, QueryRun.worker_id).where(QueryRun.id.in_(ids))
            )
            return {run_id: (status, worker) for run_id, status, worker in rows}


@pytest.fixture
def world(stack: QueryStack, environment_id: uuid.UUID) -> Iterator[World]:
    """Provide a `World` and delete what it made.

    Parameters
    ----------
    stack
        The stack.
    environment_id
        The environment.

    Yields
    ------
    World
        The world.
    """
    made = World(stack, environment_id)
    try:
        yield made
    finally:
        with Session(stack.db) as session:
            for run_id in made.runs:
                run = session.get(QueryRun, run_id)
                if run is not None:
                    session.delete(run)
            session.flush()
            for principal_id in made.principals:
                person = session.get(Principal, principal_id)
                if person is not None:
                    session.delete(person)
            session.commit()


class _HeldSession(Session):
    """A session whose commit stops, with the claim's transaction open, until it is released."""

    reached: threading.Event
    release: threading.Event
    pid: int | None = None

    def commit(self) -> None:
        """Record the backend, say that the commit was reached, wait to be released, commit."""
        self.pid = backend_pid(self)
        self.reached.set()
        assert self.release.wait(_WAIT_SECONDS), "the test never released the held claim"
        super().commit()


@dataclass
class _Claimer:
    """One claim on its own thread, with the result or the error it ended with.

    Parameters
    ----------
    stack
        The stack.
    run_id
        The run to claim.
    principal_id
        Its principal.
    cap
        The cap handed to `_start`.
    hold
        Whether the claim stops before its commit.
    before_start
        Called on the claimer's thread right before `_start`.
    """

    stack: QueryStack
    run_id: uuid.UUID
    principal_id: uuid.UUID
    cap: int
    hold: bool = False
    before_start: Callable[[], None] | None = None
    result: bool | None = None
    error: BaseException | None = None
    held: _HeldSession | None = None
    reached: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None

    def start(self) -> None:
        """Start the thread."""
        self.thread = threading.Thread(target=self._claim, name="lakana-test-claim", daemon=True)
        self.thread.start()

    def _claim(self) -> None:
        """Lock the run as the dequeue does, then run the start update."""
        try:
            if self.hold:
                session: Session = _HeldSession(self.stack.db)
                self.held = session
                self.held.reached, self.held.release = self.reached, self.release
            else:
                session = Session(self.stack.db)
            with session:
                run = session.get(QueryRun, self.run_id, with_for_update=True)
                assert run is not None
                if self.before_start is not None:
                    self.before_start()
                self.result = pipeline._start(
                    session, run, self.principal_id, worker_id="cap-test", running_cap=self.cap
                )
        except BaseException as error:
            self.error = error

    def join(self) -> bool | None:
        """Wait for the thread; re-raise what ended it.

        Returns
        -------
        bool or None
            What `_start` returned.
        """
        assert self.thread is not None
        self.thread.join(timeout=_WAIT_SECONDS)
        assert not self.thread.is_alive(), "a claim never ended"
        if self.error is not None:
            raise self.error
        return self.result


def _held_claim(
    stack: QueryStack, run_id: uuid.UUID, principal_id: uuid.UUID, cap: int
) -> tuple[_Claimer, int]:
    """Start a claim and wait until it is at its commit, holding the principal's lock.

    Parameters
    ----------
    stack
        The stack.
    run_id
        The run to claim.
    principal_id
        Its principal.
    cap
        The cap.

    Returns
    -------
    tuple[_Claimer, int]
        The claimer and its backend pid.
    """
    claimer = _Claimer(stack, run_id, principal_id, cap, hold=True)
    claimer.start()
    assert claimer.reached.wait(_WAIT_SECONDS), claimer.error
    assert claimer.held is not None and claimer.held.pid is not None
    return claimer, claimer.held.pid


def test_a_second_claim_waits_for_the_first_and_then_finds_the_cap_reached(
    stack: QueryStack, world: World
) -> None:
    """Cap 1, two runs of one principal: B waits behind A's lock, then counts A and is refused."""
    person = world.principal()
    first, second = world.queued(person, 2)
    a, pid_of_a = _held_claim(stack, first, person, 1)
    b = _Claimer(stack, second, person, 1)
    b.start()
    wait_for_lock_waiter(stack.db, pid_of_a, timeout=_WAIT_SECONDS)
    with stack.db.connect() as connection:
        advisory_waiters = connection.execute(
            text("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted")
        ).scalar_one()
    assert advisory_waiters >= 1, "B does not wait for the advisory lock"
    assert b.result is None, "B decided before A committed"
    a.release.set()
    assert a.join() is True
    assert b.join() is False
    states = world.states([first, second])
    assert states[first] == (QueryRunStatus.RUNNING, "cap-test")
    assert states[second] == (QueryRunStatus.QUEUED, None)


def test_claimers_released_at_once_never_exceed_the_cap(stack: QueryStack, world: World) -> None:
    """Six claims at one instant, cap 2, twenty rounds: exactly two run each time."""
    person = world.principal()
    for _ in range(20):
        ids = world.queued(person, 6)
        barrier = threading.Barrier(6)

        def at_the_same_moment(barrier: threading.Barrier = barrier) -> None:
            """Wait for the other five claimers."""
            barrier.wait(timeout=_WAIT_SECONDS)

        claimers = [
            _Claimer(stack, run_id, person, 2, before_start=at_the_same_moment) for run_id in ids
        ]
        for claimer in claimers:
            claimer.start()
        results = [claimer.join() for claimer in claimers]
        assert results.count(True) == 2, results
        states = world.states(ids)
        running = [
            run_id for run_id, (status, _) in states.items() if status is QueryRunStatus.RUNNING
        ]
        assert len(running) == 2
        assert all(
            state[0] is QueryRunStatus.QUEUED
            for run_id, state in states.items()
            if run_id not in running
        )
        with Session(stack.db) as session:
            for run_id in ids:
                session.delete(session.get(QueryRun, run_id))
            session.commit()
        world.runs.clear()


def test_claims_for_two_principals_do_not_wait_for_each_other(
    stack: QueryStack, world: World
) -> None:
    """A is held before its commit; another principal's claim goes through meanwhile."""
    one, other = world.principal(), world.principal()
    (mine,) = world.queued(one)
    (theirs,) = world.queued(other)
    a, _ = _held_claim(stack, mine, one, 1)
    b = _Claimer(stack, theirs, other, 1)
    b.start()
    assert b.join() is True, "B waited for a claim of another principal"
    assert not a.release.is_set()
    a.release.set()
    assert a.join() is True


def test_the_dequeue_passes_a_capped_principal_by_on_postgres(
    stack: QueryStack, world: World
) -> None:
    """`FOR UPDATE SKIP LOCKED` with the `NOT IN` list compiles, runs, and locks the row."""
    capped, other = world.principal(), world.principal()
    world.queued(capped, first_age=0)
    (theirs,) = world.queued(other, first_age=10)
    with Session(stack.db) as session:
        found = poller._next_query(session, "all", [capped])
        assert found is not None and found.id == theirs
        with Session(stack.db) as second:
            free = second.execute(
                select(QueryRun.id).where(QueryRun.id == theirs).with_for_update(skip_locked=True)
            ).scalar_one_or_none()
        assert free is None, "the dequeue did not lock the run it returned"


def test_a_claim_that_waits_longer_than_the_lock_timeout_is_refused_and_the_run_stays_queued(
    stack: QueryStack, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bound is patched down for the test, never removed: B gives up, A commits, C proceeds."""
    monkeypatch.setattr(pools, "CAP_LOCK_TIMEOUT_SECONDS", 1)
    person = world.principal()
    first, second = world.queued(person, 2)
    a, _ = _held_claim(stack, first, person, 2)
    b = _Claimer(stack, second, person, 2)
    b.start()
    assert b.join() is False, "B did not give up while A held the lock"
    assert world.states([second])[second] == (QueryRunStatus.QUEUED, None)
    assert not a.release.is_set()
    a.release.set()
    assert a.join() is True
    later = _Claimer(stack, second, person, 2)
    later.start()
    assert later.join() is True
    assert world.states([first, second]) == {
        first: (QueryRunStatus.RUNNING, "cap-test"),
        second: (QueryRunStatus.RUNNING, "cap-test"),
    }


def test_a_lock_timeout_passes_the_principal_by_so_the_next_poll_serves_another(
    stack: QueryStack, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A's claim times out behind a frozen claim; the next poll takes B's query.

    Without the memory, the oldest queued ad-hoc run is A's again and every poll spends the
    lock timeout on it while B's younger run waits.
    """
    from lakana_core.auth import AllowAllAuthorization

    monkeypatch.setattr(pools, "CAP_LOCK_TIMEOUT_SECONDS", 1)
    a_person, b_person = world.principal(), world.principal()
    frozen, waiting = world.queued(a_person, 2, first_age=0)
    (theirs,) = world.queued(b_person, first_age=10)
    holder, _ = _held_claim(stack, frozen, a_person, 5)
    passed_by = poller.PassedBy()
    pool = poller.WorkerPool(takes="interactive", running_per_principal=5, lease_seconds=60)

    def poll() -> bool:
        with Session(stack.db) as session:
            return poller.poll_once(
                session,
                AllowAllAuthorization(),
                worker_id="cap-test",
                pool=pool,
                passed_by=passed_by,
            )

    try:
        assert poll() is True, "A's claim is refused after waiting for the lock"
        assert world.states([waiting, theirs])[waiting] == (QueryRunStatus.QUEUED, None)
        assert passed_by.active() == [a_person]
        assert poll() is True, "the next poll serves B"
        assert world.states([waiting, theirs])[theirs][1] == "cap-test"
        assert world.states([waiting])[waiting] == (QueryRunStatus.QUEUED, None)
    finally:
        holder.release.set()
        holder.join()
