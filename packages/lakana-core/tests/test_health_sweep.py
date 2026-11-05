"""The hourly table health sweep: freshness, and the signals past their kept period (#340)."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

import pytest
from core_test_helpers import _prepared_catalog
from health_test_helpers import (
    _MANIFEST,
    _MANIFEST_WITHOUT_SCHEDULE,
    _deployed,
    _environment_id,
    _seed,
    _table_id,
    health_catalog,
)
from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from lakana_core.auth import AllowAllAuthorization, PrincipalRef
from lakana_core.catalog import health_signals, health_sweep
from lakana_core.catalog.health_sweep import HealthSweep, sweep_health
from lakana_core.catalog.service import CatalogService
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.models import JobSchedule, NotificationDelivery, Table, TableHealthSignal

_NOW = datetime(2026, 10, 20, tzinfo=UTC)
"""Twelve days after the newest of eight daily commits that `_seed` writes (1 to 8 October)."""

_COLUMNS = {"columns": [["id", "long"]]}
_ADDED = {"added": 1, "total": 1}
_GOLD = NamespacePath.from_parts(["gold"])


@pytest.fixture
def catalog(session: Session, tmp_path: Path, principal: PrincipalRef) -> CatalogService:
    """Provide an allow-all catalog with the empty table `silver.orders`.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        Storage root.
    principal
        The creator.

    Returns
    -------
    CatalogService
        The catalog.
    """
    return health_catalog(session, tmp_path, principal)


def _sweep(
    session: Session,
    now: datetime = _NOW,
    *,
    rotation: int = 0,
    should_stop: Callable[[], bool] = lambda: False,
) -> HealthSweep:
    """Run the sweep with a kept period of 90 days.

    Parameters
    ----------
    session
        Database session.
    now
        The sweep's time.
    rotation
        Which slice of the tables.
    should_stop
        Asked before each step.

    Returns
    -------
    HealthSweep
        What it did.
    """
    return sweep_health(
        session, now=now, retention_days=90, rotation=rotation, should_stop=should_stop
    )


def _freshness(session: Session) -> list[TableHealthSignal]:
    """Read the freshness rows, oldest first.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    list of TableHealthSignal
        The rows.
    """
    return list(
        session.scalars(
            select(TableHealthSignal)
            .where(TableHealthSignal.signal == "freshness")
            .order_by(TableHealthSignal.observed_at)
        )
    )


def _gold_tables(session: Session, tmp_path: Path, *names: str) -> None:
    """Create tables in `gold`, each with eight daily commits.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        Storage root.
    *names
        The tables' names.
    """
    _prepared_catalog(
        session,
        tmp_path,
        AllowAllAuthorization(),
        namespaces=[_GOLD],
        tables=[TableRef("dev", _GOLD, name) for name in names],
    )
    for name in names:
        _seed(session, "volume", _ADDED, table=name)


def _stop_after(calls: int) -> Callable[[], bool]:
    """Build a `should_stop` that answers no `calls` times, then yes.

    Parameters
    ----------
    calls
        How many times it says no.

    Returns
    -------
    callable
        The question.
    """
    answers: Iterator[bool] = iter([False] * calls)
    return lambda: next(answers, True)


def test_a_late_table_fires_once_and_is_notified_once(
    catalog: CatalogService, session: Session
) -> None:
    _deployed(session, _MANIFEST_WITHOUT_SCHEDULE)
    _seed(session, "volume", _ADDED)
    first = _sweep(session)
    second = _sweep(session, _NOW + timedelta(hours=1), rotation=1)
    (row,) = _freshness(session)
    assert (row.state, row.effect, row.snapshot_id) == ("fired", "warn", 8)
    assert row.sequence_number is None
    assert row.observed == {
        "seconds": 12 * 86_400,
        "last_commit_at": "2026-10-08T00:00:00+00:00",
        "source": "history",
        "job": None,
    }
    assert row.band == {"high": 3 * 86_400.0}
    assert (first.evaluated, first.recorded, second.evaluated, second.recorded) == (1, 1, 1, 0)
    (delivery,) = session.scalars(select(NotificationDelivery)).all()
    assert (delivery.event, delivery.health_signal_id) == ("health", row.id)
    assert delivery.job == "nightly"


def test_a_scheduled_job_sets_the_limit_and_is_named(
    catalog: CatalogService, session: Session
) -> None:
    _deployed(session, _MANIFEST)
    _seed(session, "volume", _ADDED)
    _sweep(session)
    (row,) = _freshness(session)
    assert row.observed is not None
    assert (row.state, row.band, row.observed["source"], row.observed["job"]) == (
        "fired",
        {"high": 2 * 86_400.0},
        "schedule",
        "nightly",
    )


def test_a_fresh_table_records_ok_and_a_new_commit_records_again(
    catalog: CatalogService, session: Session
) -> None:
    _seed(session, "volume", _ADDED)
    _sweep(session, datetime(2026, 10, 9, tzinfo=UTC))
    _seed(session, "volume", _ADDED, count=1, first_day=8)
    _sweep(session, datetime(2026, 10, 9, 12, tzinfo=UTC))
    assert [(r.state, r.snapshot_id) for r in _freshness(session)] == [("ok", 8), ("ok", 9)]


def test_a_table_that_stays_late_is_told_once(catalog: CatalogService, session: Session) -> None:
    """A commit that comes late again records a new fired row, but sends no second message."""
    _deployed(session, _MANIFEST_WITHOUT_SCHEDULE)
    _seed(session, "volume", _ADDED)
    _sweep(session)
    _seed(session, "volume", _ADDED, count=1, first_day=8)
    _sweep(session, _NOW + timedelta(days=4))
    assert [(r.state, r.snapshot_id) for r in _freshness(session)] == [("fired", 8), ("fired", 9)]
    assert len(session.scalars(select(NotificationDelivery)).all()) == 1


def test_a_young_table_has_no_freshness_yet(catalog: CatalogService, session: Session) -> None:
    _seed(session, "volume", _ADDED, count=7)
    swept = _sweep(session)
    (row,) = _freshness(session)
    assert (row.state, row.band, swept.recorded) == ("no_history", None, 1)


def test_freshness_that_is_off_records_off(catalog: CatalogService, session: Session) -> None:
    table = session.get(Table, _table_id(session))
    assert table is not None
    table.health_effects = {"freshness": "off"}
    session.commit()
    _seed(session, "volume", _ADDED)
    _sweep(session)
    (row,) = _freshness(session)
    assert (row.state, row.effect, row.observed, row.band) == ("off", "off", None, None)


_ONLY_SCHEDULED = {
    **_MANIFEST,
    "jobs": {k: v for k, v in _MANIFEST["jobs"].items() if k != "quiet"},
}
"""`_MANIFEST` without job ``quiet``, so every job that builds `silver.orders` is scheduled."""


def _pause_nightly(session: Session, repository_id: uuid.UUID) -> None:
    """Pause the job ``nightly`` of a repository in `dev`.

    Parameters
    ----------
    session
        Database session; committed.
    repository_id
        The repository.
    """
    session.add(
        JobSchedule(
            id=uuid.uuid4(),
            environment_id=_environment_id(session),
            repository_id=repository_id,
            job="nightly",
            paused=True,
        )
    )
    session.commit()


def test_a_paused_scheduled_job_records_off(catalog: CatalogService, session: Session) -> None:
    """A paused job is not expected to write: no firing, and so no message."""
    _pause_nightly(session, _deployed(session, _ONLY_SCHEDULED))
    _seed(session, "volume", _ADDED)
    _sweep(session)
    (row,) = _freshness(session)
    assert (row.state, row.observed, row.effect) == ("off", {"reason": "paused"}, "warn")
    assert session.scalars(select(NotificationDelivery)).all() == []


def test_an_unscheduled_building_job_keeps_freshness_on_while_the_scheduled_one_is_paused(
    catalog: CatalogService, session: Session
) -> None:
    """Job ``quiet`` has no schedule and can write `silver.orders` at any time: the history rule.

    Parameters
    ----------
    catalog
        The catalog.
    session
        Database session.
    """
    _pause_nightly(session, _deployed(session, _MANIFEST))
    _seed(session, "volume", _ADDED)
    _sweep(session)
    (row,) = _freshness(session)
    assert row.state != "off"
    assert row.observed is not None
    assert (row.observed["source"], row.observed["job"]) == ("history", None)


def test_a_compaction_does_not_make_a_stale_table_fresh(
    catalog: CatalogService, session: Session
) -> None:
    """Only a commit that adds data counts: a compaction records a schema row, no volume row."""
    _seed(session, "volume", _ADDED)
    _seed(session, "schema", _COLUMNS, count=1, first_day=18)
    _sweep(session)
    (row,) = _freshness(session)
    assert row.observed is not None
    assert (row.state, row.snapshot_id, row.observed["last_commit_at"]) == (
        "fired",
        8,
        "2026-10-08T00:00:00+00:00",
    )


def test_a_schedule_without_two_fires_falls_back_to_the_history(
    catalog: CatalogService, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(health_signals, "next_fire_after", lambda *args: None)
    _deployed(session, _MANIFEST)
    _seed(session, "volume", _ADDED)
    _sweep(session)
    (row,) = _freshness(session)
    assert row.observed is not None
    assert (row.band, row.observed["source"]) == ({"high": 3 * 86_400.0}, "history")


def test_the_newest_commit_counts_and_a_set_back_one_does_not(
    catalog: CatalogService, session: Session
) -> None:
    """Judge by the newest commit that is the table's data, and record again after a set-back.

    A set-back freshness row is no longer the table's state, so the next sweep records anew.
    """
    _seed(session, "volume", _ADDED, count=9)
    session.execute(
        update(TableHealthSignal)
        .where(TableHealthSignal.snapshot_id == 9)
        .values(set_back_at=datetime(2026, 10, 10, tzinfo=UTC))
    )
    session.commit()
    _sweep(session)
    session.execute(
        update(TableHealthSignal)
        .where(TableHealthSignal.signal == "freshness")
        .values(set_back_at=_NOW)
    )
    session.execute(
        update(TableHealthSignal).where(TableHealthSignal.snapshot_id == 9).values(set_back_at=None)
    )
    session.commit()
    _sweep(session, _NOW + timedelta(hours=1))
    assert [(r.state, r.snapshot_id) for r in _freshness(session)] == [("fired", 8), ("fired", 9)]


def test_a_table_without_commits_is_not_evaluated(
    catalog: CatalogService, session: Session
) -> None:
    _seed(session, "schema", _COLUMNS, count=1)
    swept = _sweep(session)
    assert (swept.evaluated, _freshness(session)) == (0, [])


def test_retention_deletes_old_rows_but_keeps_the_newest_of_each(
    catalog: CatalogService, session: Session
) -> None:
    _seed(session, "schema", _COLUMNS, count=3)
    _seed(session, "range", {}, count=1, column=None)
    _seed(session, "range", {}, count=2, column="id", first_day=1)
    _seed(session, "null_rate", {}, count=1, column="id", first_day=90)
    swept = _sweep(session, datetime(2027, 3, 1, tzinfo=UTC))
    remaining = session.execute(
        select(
            TableHealthSignal.signal, TableHealthSignal.column_name, TableHealthSignal.snapshot_id
        ).order_by(TableHealthSignal.signal, TableHealthSignal.column_name)
    ).all()
    assert sorted(remaining, key=str) == sorted(
        [("schema", None, 3), ("range", None, 1), ("range", "id", 3), ("null_rate", "id", 91)],
        key=str,
    )
    assert swept.deleted == 3


def test_retention_deletes_old_set_back_rows_and_keeps_the_newest_unmarked(
    catalog: CatalogService, session: Session
) -> None:
    _seed(session, "schema", _COLUMNS, count=3)
    session.execute(
        update(TableHealthSignal)
        .where(TableHealthSignal.snapshot_id == 3)
        .values(set_back_at=datetime(2026, 10, 4, tzinfo=UTC))
    )
    session.commit()
    swept = _sweep(session, datetime(2027, 3, 1, tzinfo=UTC))
    remaining = session.scalars(select(TableHealthSignal.snapshot_id)).all()
    assert (remaining, swept.deleted) == ([2], 2)


def test_retention_keeps_rows_within_the_kept_period(
    catalog: CatalogService, session: Session
) -> None:
    _seed(session, "schema", _COLUMNS, count=3)
    swept = _sweep(session, datetime(2026, 12, 31, tzinfo=UTC))
    assert swept.deleted == 1
    assert sorted(session.scalars(select(TableHealthSignal.snapshot_id)).all(), key=str) == [2, 3]


def test_retention_deletes_in_batches_and_stops_when_asked(
    catalog: CatalogService, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(health_sweep, "RETENTION_BATCH", 1)
    _seed(session, "schema", _COLUMNS, count=3)
    later = datetime(2027, 3, 1, tzinfo=UTC)
    assert _sweep(session, later, should_stop=_stop_after(2)).deleted == 1
    assert _sweep(session, later).deleted == 1
    assert session.scalars(select(TableHealthSignal.snapshot_id)).all() == [3]


def test_the_sweep_is_bounded_and_rotates(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(health_sweep, "MAX_SWEEP_TABLES", 1)
    _gold_tables(session, tmp_path, "a", "b")
    seen = {_sweep(session, rotation=r).evaluated for r in (0, 1)}
    assert seen == {1}
    tables = {row.table_id for row in _freshness(session)}
    assert tables == {_table_id(session, "a"), _table_id(session, "b")}


def test_the_rotation_wraps_around_without_a_table_twice(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(health_sweep, "MAX_SWEEP_TABLES", 2)
    _gold_tables(session, tmp_path, "a", "b", "c")
    first = sorted([_table_id(session, n) for n in "abc"])
    assert _sweep(session, rotation=1).evaluated == 2
    assert sorted(row.table_id for row in _freshness(session)) == [first[0], first[2]]


def test_the_sweep_stops_when_asked(catalog: CatalogService, session: Session) -> None:
    _seed(session, "volume", _ADDED)
    swept = _sweep(session, should_stop=lambda: True)
    assert swept == HealthSweep(evaluated=0, recorded=0, deleted=0)
    assert _freshness(session) == []


def test_the_sweep_stops_at_its_deadline(
    catalog: CatalogService, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(health_sweep, "HEALTH_SWEEP_MAX_SECONDS", -1.0)
    _seed(session, "volume", _ADDED)
    assert _sweep(session).evaluated == 0


def test_a_table_that_cannot_be_checked_is_logged_and_the_next_is(
    session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _gold_tables(session, tmp_path, "a", "b")

    def jobs(manifests: object, table: str) -> list[object]:
        """Fail for `gold.a`.

        Parameters
        ----------
        manifests
            Ignored.
        table
            The table.

        Returns
        -------
        list
            None.

        Raises
        ------
        RuntimeError
            For `gold.a`.
        """
        if table == "gold.a":
            raise RuntimeError("boom")
        return []

    monkeypatch.setattr(health_sweep, "building_jobs", jobs)
    with caplog.at_level(logging.WARNING):
        swept = _sweep(session)
    assert "The freshness of gold.a could not be checked (RuntimeError)." in caplog.text
    assert swept.evaluated == 1
    assert [row.table_id for row in _freshness(session)] == [_table_id(session, "b")]


def test_a_table_dropped_during_the_sweep_is_skipped(
    catalog: CatalogService, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(session, "volume", _ADDED)
    real = _table_id(session)
    monkeypatch.setattr(health_sweep, "_candidates", lambda *args: [uuid.uuid4(), real])
    assert _sweep(session).evaluated == 1


def test_a_sweep_without_signals_does_nothing(catalog: CatalogService, session: Session) -> None:
    assert _sweep(session) == HealthSweep(evaluated=0, recorded=0, deleted=0)


def test_a_database_error_ends_the_sweep(
    catalog: CatalogService, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing database fails every table, so the housekeeping logs it once.

    Parameters
    ----------
    catalog
        The catalog.
    session
        Database session.
    monkeypatch
        The fixture.
    """
    _seed(session, "volume", _ADDED)

    def failing(*args: object) -> list[object]:
        """Fail as the database would.

        Parameters
        ----------
        *args
            Ignored.

        Raises
        ------
        OperationalError
            Always.
        """
        raise OperationalError("SELECT", {}, Exception("gone"))

    monkeypatch.setattr(health_sweep, "deployed_manifests", failing)
    with pytest.raises(OperationalError):
        _sweep(session)


def test_a_stale_table_stays_fired_past_the_kept_period(
    catalog: CatalogService, session: Session
) -> None:
    """Retention keeps the last data commits, so a table that stopped stays late, not young."""
    _seed(session, "volume", _ADDED)
    _sweep(session)
    later = datetime(2027, 3, 1, tzinfo=UTC)
    _sweep(session, later)
    _sweep(session, later + timedelta(hours=1), rotation=1)
    assert [r.state for r in _freshness(session)] == ["fired"]
    volume = session.scalars(select(TableHealthSignal).where(TableHealthSignal.signal == "volume"))
    assert len(volume.all()) == 8


def test_a_table_written_every_20_days_builds_a_history(
    catalog: CatalogService, session: Session
) -> None:
    for commit in range(9):
        _seed(session, "volume", _ADDED, count=1, first_day=20 * commit)
    last = datetime(2026, 10, 1, tzinfo=UTC) + timedelta(days=160)
    _sweep(session, last + timedelta(days=30))
    _sweep(session, last + timedelta(days=30, hours=1), rotation=1)
    (row,) = _freshness(session)
    assert (row.state, row.band) == ("ok", {"high": 60 * 86_400.0})


def test_retention_spares_only_the_newest_data_commits(
    catalog: CatalogService, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(health_sweep, "HISTORY", 2)
    _seed(session, "volume", _ADDED, count=5)
    _seed(session, "range", {}, count=3, column="id")
    _sweep(session, datetime(2027, 3, 1, tzinfo=UTC))
    kept = session.execute(
        select(TableHealthSignal.signal, TableHealthSignal.snapshot_id).where(
            TableHealthSignal.signal != "freshness"
        )
    ).all()
    assert sorted(kept) == [("range", 3), ("volume", 3), ("volume", 4), ("volume", 5)]


def test_freshness_runs_before_retention_uses_up_the_deadline(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _gold_tables(session, tmp_path, "a", "b")
    for name in "ab":
        _seed(session, "range", {}, count=3, column="id", table=name)
    clock = [0.0]
    real = health_sweep._delete_old

    def slow(
        session: Session, table_id: uuid.UUID, cutoff: datetime, stopped: Callable[[], bool]
    ) -> int:
        """Delete one table's rows, then let the deadline pass.

        Parameters
        ----------
        session
            Database session.
        table_id
            The table.
        cutoff
            Rows observed before this go.
        stopped
            Asked after each full batch.

        Returns
        -------
        int
            How many rows were deleted.
        """
        deleted = real(session, table_id, cutoff, stopped)
        clock[0] = 1_000.0
        return deleted

    monkeypatch.setattr(health_sweep.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(health_sweep, "_delete_old", slow)
    swept = _sweep(session, datetime(2027, 3, 1, tzinfo=UTC))
    assert (swept.evaluated, swept.deleted) == (2, 2)
    assert len(_freshness(session)) == 2


def test_a_table_another_sweep_holds_is_skipped(
    catalog: CatalogService, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(health_sweep, "_lock_table", lambda *args: False)
    _seed(session, "volume", _ADDED)
    assert _sweep(session).evaluated == 0
    assert _freshness(session) == []


def test_a_change_of_the_off_reason_is_recorded(catalog: CatalogService, session: Session) -> None:
    repository_id = _deployed(session, _ONLY_SCHEDULED)
    table = session.get(Table, _table_id(session))
    assert table is not None
    table.health_effects = {"freshness": "off"}
    session.commit()
    _seed(session, "volume", _ADDED)
    _sweep(session)
    table.health_effects = {}
    _pause_nightly(session, repository_id)
    _sweep(session, _NOW + timedelta(hours=1))
    _sweep(session, _NOW + timedelta(hours=2))
    assert [(r.state, r.observed) for r in _freshness(session)] == [
        ("off", None),
        ("off", {"reason": "paused"}),
    ]


@pytest.mark.parametrize(("granted", "expected"), [(True, True), (False, False), (None, False)])
def test_the_freshness_lock_asks_postgres_with_two_keys(
    monkeypatch: pytest.MonkeyPatch, granted: bool | None, expected: bool
) -> None:
    """The statement itself runs on Postgres in `test_health_sweep_postgres_integration.py`."""
    monkeypatch.setattr(health_sweep, "on_postgres", lambda session: True)
    session = mock.MagicMock()
    session.scalar.return_value = granted
    assert health_sweep._lock_table(session, uuid.UUID(int=1 << 120)) is expected
    (statement,) = session.scalar.call_args.args
    assert "pg_try_advisory_xact_lock" in str(statement)
