"""Tests for the worker registry and the "no alive worker holds this run" rule."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from lakana_core.db import db_now
from lakana_core.models import (
    DeployKind,
    DeployRun,
    DeployStatus,
    Environment,
    QueryRun,
    QueryRunStatus,
    Repository,
    RepositoryKind,
    Worker,
)
from lakana_core.runs import workers
from lakana_core.runs.workers import (
    WORKER_ROW_KEEP_SECONDS,
    alive_ids,
    alive_on_host,
    alive_takes,
    any_alive,
    beat,
    forget_lost,
    register,
    remove,
    without_alive_worker,
)

FIXED = datetime(2001, 2, 3, 4, 5, 6, tzinfo=UTC)


@pytest.fixture
def environment_id(session: Session) -> uuid.UUID:
    """Insert one environment and return its id.

    Parameters
    ----------
    session
        Database session to insert into.

    Returns
    -------
    uuid.UUID
        The environment's id.
    """
    env = Environment(
        id=uuid.uuid4(),
        name="dev",
        storage_root="file:///tmp/lakana-test",
        storage_options={},
        is_protected=False,
        properties={},
    )
    session.add(env)
    session.flush()
    return env.id


def _run(session: Session, environment_id: uuid.UUID, **fields: object) -> QueryRun:
    """Insert a query run with the given fields over a RUNNING default.

    Parameters
    ----------
    session
        Database session to insert into.
    environment_id
        The run's environment.
    **fields
        Column values that override the defaults.

    Returns
    -------
    QueryRun
        The flushed run.
    """
    values: dict[str, object] = {
        "id": uuid.uuid4(),
        "environment_id": environment_id,
        "sql_text": "SELECT 1",
        "status": QueryRunStatus.RUNNING,
    }
    values.update(fields)
    run = QueryRun(**values)
    session.add(run)
    session.flush()
    return run


def _worker(session: Session, worker_id: str, heartbeat_at: datetime) -> None:
    """Insert a worker row with the given heartbeat.

    Parameters
    ----------
    session
        Database session to insert into.
    worker_id
        The worker's id.
    heartbeat_at
        The heartbeat to store.
    """
    session.add(
        Worker(
            id=worker_id, host="h", version="1", started_at=heartbeat_at, heartbeat_at=heartbeat_at
        )
    )
    session.flush()


def test_a_run_is_without_an_alive_worker_exactly_when_the_spec_says(
    session: Session, environment_id: uuid.UUID
) -> None:
    now = db_now(session)
    old, lease = now - timedelta(seconds=61), 60
    session.add(Worker(id="live", host="h", version="1", started_at=old, heartbeat_at=now))
    session.add(Worker(id="dead", host="h", version="1", started_at=old, heartbeat_at=old))
    _worker(session, "edge", now - timedelta(seconds=lease))
    _worker(session, "young", now - timedelta(seconds=lease - 1))
    kept = [
        _run(session, environment_id, worker_id="live", started_at=old),
        _run(session, environment_id, worker_id=None, started_at=now),  # an old worker, young run
        _run(session, environment_id, worker_id="young", started_at=old),  # beat lease - 1 ago
        _run(session, environment_id, worker_id=None, started_at=now - timedelta(seconds=59)),
    ]
    lost = [
        _run(session, environment_id, worker_id="dead", started_at=old),
        _run(session, environment_id, worker_id="gone", started_at=now),  # its row was removed
        _run(session, environment_id, worker_id=None, started_at=old),
        _run(session, environment_id, worker_id=None, started_at=None, created_at=old),
        _run(session, environment_id, worker_id="edge", started_at=now),  # exactly a lease old
        _run(session, environment_id, worker_id=None, started_at=now - timedelta(seconds=lease)),
    ]
    found = set(
        session.scalars(select(QueryRun.id).where(without_alive_worker(QueryRun, now, lease)))
    )
    assert found == {run.id for run in lost}
    assert not found & {run.id for run in kept}


def test_the_predicate_works_for_deploy_runs_too(
    session: Session, environment_id: uuid.UUID
) -> None:
    now = db_now(session)
    old, lease = now - timedelta(seconds=61), 60
    _worker(session, "live", now)
    _worker(session, "dead", old)
    repo = Repository(
        id=uuid.uuid4(),
        name="r",
        kind=RepositoryKind.LOCAL,
        url="file:///tmp/r",
    )
    session.add(repo)
    session.flush()

    def deploy(**fields: object) -> DeployRun:
        values: dict[str, object] = {
            "id": uuid.uuid4(),
            "environment_id": environment_id,
            "repository_id": repo.id,
            "kind": DeployKind.DEPLOY,
            "status": DeployStatus.RUNNING,
        }
        values.update(fields)
        run = DeployRun(**values)
        session.add(run)
        session.flush()
        return run

    _worker(session, "edge", now - timedelta(seconds=lease))
    _worker(session, "young", now - timedelta(seconds=lease - 1))
    kept = [
        deploy(worker_id="live", started_at=old),
        deploy(worker_id=None, started_at=now),
        deploy(worker_id="young", started_at=old),
        deploy(worker_id=None, started_at=now - timedelta(seconds=lease - 1)),
    ]
    lost = [
        deploy(worker_id="dead", started_at=old),
        deploy(worker_id="gone", started_at=now),
        deploy(worker_id=None, started_at=old),
        deploy(worker_id=None, started_at=None, created_at=old),
        deploy(worker_id="edge", started_at=now),
        deploy(worker_id=None, started_at=now - timedelta(seconds=lease)),
    ]
    found = set(
        session.scalars(select(DeployRun.id).where(without_alive_worker(DeployRun, now, lease)))
    )
    assert found == {run.id for run in lost}
    assert not found & {run.id for run in kept}


def test_register_inserts_the_row_with_the_databases_time(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(workers, "db_now", lambda s: FIXED)
    started = register(session, "w1", host="box", version="1.2.3")
    row = session.get(Worker, "w1")
    assert row is not None
    assert started == FIXED
    assert (row.host, row.version) == ("box", "1.2.3")
    assert row.started_at == FIXED
    assert row.heartbeat_at == FIXED


def test_registering_again_keeps_started_at_and_refreshes_the_rest(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(workers, "db_now", lambda s: FIXED)
    register(session, "w1", host="box", version="1")
    later = FIXED + timedelta(seconds=30)
    monkeypatch.setattr(workers, "db_now", lambda s: later)
    started = register(session, "w1", host="other", version="2")
    row = session.get(Worker, "w1")
    assert row is not None
    assert started == FIXED
    assert (row.host, row.version, row.started_at, row.heartbeat_at) == (
        "other",
        "2",
        FIXED,
        later,
    )


def test_registering_with_a_started_at_uses_it(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(workers, "db_now", lambda s: FIXED)
    earlier = FIXED - timedelta(hours=2)
    assert register(session, "w1", host="box", version="1", started_at=earlier) == earlier
    row = session.get(Worker, "w1")
    assert row is not None
    assert (row.started_at, row.heartbeat_at) == (earlier, FIXED)


def test_register_cuts_host_and_version_to_their_columns(session: Session) -> None:
    register(session, "w1", host="h" * 300, version="v" * 100)
    row = session.get(Worker, "w1")
    assert row is not None
    assert (len(row.host), len(row.version)) == (255, 64)


def test_beat_writes_the_databases_time_and_says_whether_a_row_matched(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _worker(session, "w1", FIXED - timedelta(hours=1))
    monkeypatch.setattr(workers, "db_now", lambda s: FIXED)
    assert beat(session, "w1") is True
    assert session.scalar(select(Worker.heartbeat_at).where(Worker.id == "w1")) == FIXED
    assert beat(session, "unknown") is False
    assert session.scalar(select(Worker.id).where(Worker.id == "unknown")) is None


def test_remove_deletes_only_that_worker(session: Session) -> None:
    now = db_now(session)
    _worker(session, "a", now)
    _worker(session, "b", now)
    remove(session, "a")
    assert set(session.scalars(select(Worker.id))) == {"b"}


def test_forget_lost_removes_rows_older_than_an_hour_and_keeps_the_rest(session: Session) -> None:
    now = db_now(session)
    assert WORKER_ROW_KEEP_SECONDS == 3600
    _worker(session, "old", now - timedelta(seconds=3601))
    _worker(session, "recent", now - timedelta(seconds=3599))
    assert forget_lost(session, now) == 1
    assert set(session.scalars(select(Worker.id))) == {"recent"}


def test_forget_lost_keeps_a_row_exactly_an_hour_old(session: Session) -> None:
    now = db_now(session)
    _worker(session, "edge", now - timedelta(seconds=3600))
    assert forget_lost(session, now) == 0
    assert set(session.scalars(select(Worker.id))) == {"edge"}


def test_forget_lost_waits_for_a_lease_longer_than_an_hour(session: Session) -> None:
    now = db_now(session)
    _worker(session, "stalled", now - timedelta(seconds=3601))
    _worker(session, "gone", now - timedelta(seconds=7201))
    assert forget_lost(session, now, lease_seconds=7200) == 1
    assert set(session.scalars(select(Worker.id))) == {"stalled"}
    assert forget_lost(session, now, lease_seconds=60) == 1
    assert set(session.scalars(select(Worker.id))) == set()


def test_a_heartbeat_one_renew_interval_old_is_alive(session: Session) -> None:
    now = db_now(session)
    _worker(session, "w1", now - timedelta(seconds=20))
    assert alive_ids(session, now, 60) == {"w1"}


def test_a_heartbeat_exactly_a_lease_old_is_not_alive(session: Session) -> None:
    now = db_now(session)
    _worker(session, "w1", now - timedelta(seconds=60))
    assert alive_ids(session, now, 60) == set()


def _takes_of(session: Session, worker_id: str) -> str:
    """Read a worker's `takes` from the database, not from the identity map.

    Parameters
    ----------
    session
        Database session.
    worker_id
        The worker's id.

    Returns
    -------
    str
        The stored value.
    """
    session.flush()
    session.expire_all()
    return session.scalars(select(Worker.takes).where(Worker.id == worker_id)).one()


def test_register_writes_takes_on_insert_and_on_refresh(session: Session) -> None:
    register(session, "w1", host="box", version="1", takes="interactive")
    assert _takes_of(session, "w1") == "interactive"
    register(session, "w1", host="box", version="1", takes="pipeline")
    assert _takes_of(session, "w1") == "pipeline"


def test_register_without_takes_writes_all(session: Session) -> None:
    register(session, "w1", host="box", version="1")
    assert _takes_of(session, "w1") == "all"


def test_alive_takes_returns_the_alive_workers_with_what_they_take(session: Session) -> None:
    now = db_now(session)
    register(session, "young", host="h", version="1", takes="interactive")
    register(session, "old", host="h", version="1", takes="pipeline")
    for worker_id, age in (("young", 20), ("old", 61)):
        session.execute(
            update(Worker)
            .where(Worker.id == worker_id)
            .values(heartbeat_at=now - timedelta(seconds=age))
        )
    session.expire_all()
    assert alive_takes(session, now, 60) == {"young": "interactive"}


def test_alive_takes_and_alive_ids_name_the_same_workers(session: Session) -> None:
    now = db_now(session)
    _worker(session, "edge", now - timedelta(seconds=60))
    _worker(session, "fresh", now - timedelta(seconds=59))
    assert alive_takes(session, now, 60).keys() == alive_ids(session, now, 60) == {"fresh"}


def test_any_alive_is_true_while_one_heartbeat_is_younger_than_the_lease(
    session: Session,
) -> None:
    now = db_now(session)
    assert any_alive(session, now, 60) is False
    _worker(session, "edge", now - timedelta(seconds=60))
    assert any_alive(session, now, 60) is False
    _worker(session, "fresh", now - timedelta(seconds=59))
    assert any_alive(session, now, 60) is True


def test_alive_on_host_looks_only_at_the_workers_of_that_host(session: Session) -> None:
    now = db_now(session)
    register(session, "here", host="box-1", version="1")
    register(session, "there", host="box-2", version="1")
    session.execute(
        update(Worker).where(Worker.id == "here").values(heartbeat_at=now - timedelta(seconds=61))
    )
    session.expire_all()
    assert alive_on_host(session, "box-1", now, 60) is False
    assert alive_on_host(session, "box-2", now, 60) is True
    assert alive_on_host(session, "box-3", now, 60) is False


def test_alive_on_host_cuts_the_host_name_as_register_does(session: Session) -> None:
    now = db_now(session)
    long_host = "h" * 300
    register(session, "w1", host=long_host, version="1")
    assert alive_on_host(session, long_host, now + timedelta(seconds=1), 60) is True


def test_a_beat_with_cache_sizes_stores_them_with_the_databases_time(session: Session) -> None:
    register(session, "w1", host="h", version="v")
    session.commit()
    row = session.get(Worker, "w1")
    assert row is not None and row.cache_measured_at is None
    sizes = workers.CacheSizes(
        environments_bytes=10, packages_bytes=20, tasks_bytes=3, complete=False
    )
    assert beat(session, "w1", caches=sizes)
    session.commit()
    session.expire_all()
    row = session.get(Worker, "w1")
    assert row is not None
    assert (
        row.cache_environments_bytes,
        row.cache_packages_bytes,
        row.cache_tasks_bytes,
        row.cache_complete,
    ) == (10, 20, 3, False)
    assert row.cache_measured_at is not None
    measured = row.cache_measured_at
    assert beat(session, "w1")
    session.commit()
    session.expire_all()
    row = session.get(Worker, "w1")
    assert row is not None
    assert (row.cache_packages_bytes, row.cache_measured_at) == (20, measured)


def test_a_registration_with_cache_sizes_stores_them(session: Session) -> None:
    sizes = workers.CacheSizes(environments_bytes=1, packages_bytes=2, tasks_bytes=0, complete=True)
    register(session, "w1", host="h", version="v", caches=sizes)
    session.commit()
    register(session, "w1", host="h", version="v")
    session.commit()
    row = session.get(Worker, "w1")
    assert row is not None
    assert (row.cache_packages_bytes, row.cache_complete) == (2, True)
    assert workers.cache_sizes(row) == sizes
    assert workers.cache_sizes(Worker(id="new")) is None


def test_beat_writes_memory_with_the_database_time(session: Session) -> None:
    register(session, "w1", host="h", version="v")
    session.commit()
    row = session.get(Worker, "w1")
    assert row is not None and workers.memory_use(row) is None
    used = workers.MemoryUse(used_bytes=3 * 1024**3, limit_bytes=4 * 1024**3)
    assert beat(session, "w1", memory=used)
    session.commit()
    session.expire_all()
    row = session.get(Worker, "w1")
    assert row is not None
    assert (row.memory_used_bytes, row.memory_limit_bytes) == (3 * 1024**3, 4 * 1024**3)
    assert row.memory_measured_at is not None
    assert row.memory_measured_at == row.heartbeat_at
    assert workers.memory_use(row) == used


def test_beat_without_memory_leaves_the_stored_figures(session: Session) -> None:
    used = workers.MemoryUse(used_bytes=10, limit_bytes=None)
    register(session, "w1", host="h", version="v", memory=used)
    session.commit()
    row = session.get(Worker, "w1")
    assert row is not None
    measured = row.memory_measured_at
    assert measured is not None
    assert beat(session, "w1")
    register(session, "w1", host="h", version="v")
    session.commit()
    session.expire_all()
    row = session.get(Worker, "w1")
    assert row is not None
    assert (workers.memory_use(row), row.memory_measured_at) == (used, measured)
