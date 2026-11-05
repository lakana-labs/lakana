"""The blocking health checks of the check phase: `health: {signal: block}` (#340)."""

from __future__ import annotations

import sys
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
from check_run_helpers import (
    _built_once_then_redeployed,
    _check,
    _drain,
    _events,
    _ids,
    _outside_table,
    _results,
    _run_until,
)
from pyiceberg.schema import Schema
from pyiceberg.table.refs import SnapshotRefType
from pyiceberg.table.snapshots import Operation, Snapshot, Summary
from pyiceberg.table.update import AddSnapshotUpdate, SetSnapshotRefUpdate
from pyiceberg.types import LongType, NestedField
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from task_run_helpers import World, _engine, _start, _task

import lakana_core.catalog.health_record as health_record_module
import lakana_worker.task_checks as task_checks_module
from lakana_core.catalog.attempt_stamp import CLAIM_PROPERTY, TASK_RUN_PROPERTY
from lakana_core.catalog.health_read import Visibility, current_signals
from lakana_core.catalog.health_record import recent_observations
from lakana_core.catalog.health_signals import unjudged_text, volume_signal
from lakana_core.catalog.identity import resolve_table_id
from lakana_core.catalog.metadata import apply_updates, build_table_metadata
from lakana_core.models import (
    AuditEventType,
    CheckOutcome,
    CheckResult,
    Environment,
    TableHealthSignal,
    TaskStatus,
)
from lakana_core.projects.checks import CheckSpec, GeneratedFrom
from lakana_core.projects.health_checks import health_specs
from lakana_core.runs.checks import health_blocked
from lakana_core.runs.tasks import Written
from lakana_worker.tasks import run_claimed


def _signal(
    world: World,
    dotted: str,
    signal: str,
    *,
    snapshot: int | None,
    state: str = "ok",
    column: str | None = None,
    effect: str = "warn",
    observed: dict[str, object] | None = None,
    day: int = 0,
) -> None:
    """Record one health signal row of a table in `dev`, as a commit would have.

    Parameters
    ----------
    world
        The world.
    dotted
        The table.
    signal
        The signal.
    snapshot
        The snapshot observed.
    state
        The row's state.
    column
        The column, for a column signal.
    effect
        The effect recorded with it.
    observed
        What was observed.
    day
        Days after 2026-10-01 it was observed.
    """
    with world.session() as session:
        table_id = resolve_table_id(session, "dev", *dotted.split("."))
        environment_id = session.scalars(
            select(Environment.id).where(Environment.name == "dev")
        ).one()
        session.add(
            TableHealthSignal(
                id=uuid.uuid4(),
                environment_id=environment_id,
                table_id=table_id,
                signal=signal,
                column_name=column,
                snapshot_id=snapshot,
                observed=observed,
                band=None,
                state=state,
                effect=effect,
                observed_at=datetime(2026, 10, 1, tzinfo=UTC) + timedelta(days=day),
            )
        )
        session.commit()


def _seed_volume(world: World, dotted: str, added: int) -> None:
    """Record eight past volume observations of a table, as eight commits would have.

    Parameters
    ----------
    world
        The world.
    dotted
        The table.
    added
        The rows each added.
    """
    for day in range(8):
        _signal(
            world,
            dotted,
            "volume",
            snapshot=day + 1,
            observed={"added": added, "total": added},
            day=day,
        )


def test_a_fired_block_signal_fails_the_task_and_sets_the_table_back(world: World) -> None:
    """The check fails with a text that has no value; the table is set back as by any check.

    The fired row stays in the history, marked set back: it is neither the current state nor
    part of the band any more.

    Parameters
    ----------
    world
        The world.
    """
    _built_once_then_redeployed(world, health={"volume": "block"})
    _seed_volume(world, "silver.orders", 1_000)
    _start(world)
    _drain(world)

    task = _task(world, "silver.orders")
    assert task.status is TaskStatus.FAILED
    assert _ids(world, "silver.orders") == [1, 1, 2]  # set back to the first build
    (result,) = _results(world, task.id)
    assert (result.check_name, result.outcome, result.failing_rows, result.generated) == (
        "silver.orders/health_volume",
        CheckOutcome.FAILED,
        None,
        True,
    )
    assert result.error is not None
    assert result.error.startswith("health signal volume fired at snapshot ")
    assert "`lakana table health silver.orders --env dev --signals`" in result.error
    assert "1000" not in result.error and "1,000" not in result.error  # no values
    assert task.error_message is not None
    assert task.error_message.startswith(
        "Check `silver.orders/health_volume` failed: health signal volume fired at snapshot "
    )
    assert "lakana check rows" not in task.error_message
    (rolled,) = _events(world, AuditEventType.TABLE_ROLLED_BACK)
    assert rolled["checks"] == ["silver.orders/health_volume"]
    assert _task(world, "gold.report").status is TaskStatus.UPSTREAM_FAILED
    with world.session() as session:
        fired = session.scalars(
            select(TableHealthSignal).where(TableHealthSignal.state == "fired")
        ).one()
        assert (fired.signal, fired.snapshot_id) == ("volume", result.checked_snapshot_id)
        assert fired.set_back_at is not None
        current = current_signals(session, fired.table_id, "silver.orders", Visibility())
        assert fired.id not in {shown.id for shown in current}
        assert {shown.signal: shown.state for shown in current}["volume"] == "no_history"
        # The first build's and the eight seeded observations; not the one set back.
        assert len(recent_observations(session, fired.table_id)["volume", None]) == 9


def test_a_block_signal_that_did_not_fire_passes(world: World) -> None:
    """Without a firing each check passes and names the snapshot it judged; the chain is read once.

    Parameters
    ----------
    world
        The world.
    """
    _built_once_then_redeployed(world, health={"volume": "block", "schema": "block"})
    _start(world)
    _drain(world)
    task = _task(world, "silver.orders")
    assert task.status is TaskStatus.SUCCEEDED
    results = _results(world, task.id)
    assert sorted((r.check_name, r.outcome, r.error) for r in results) == [
        ("silver.orders/health_schema", CheckOutcome.PASSED, None),
        ("silver.orders/health_volume", CheckOutcome.PASSED, None),
    ]
    assert len({r.checked_snapshot_id for r in results}) == 1
    assert results[0].checked_snapshot_id is not None


def test_a_warn_signal_that_fired_does_not_stop_the_run(world: World) -> None:
    """A firing without `block` only warns: the task has no check at all.

    Parameters
    ----------
    world
        The world.
    """
    _built_once_then_redeployed(world)
    _seed_volume(world, "silver.orders", 1_000)
    _start(world)
    _drain(world)
    task = _task(world, "silver.orders")
    assert task.status is TaskStatus.SUCCEEDED
    assert _results(world, task.id) == []
    with world.session() as session:
        fired = session.scalars(
            select(TableHealthSignal).where(TableHealthSignal.state == "fired")
        ).one()
    assert (fired.signal, fired.effect, fired.set_back_at) == ("volume", "warn", None)


def _health_spec(*columns: str) -> CheckSpec:
    """Build the blocking health check of `bronze.events` for a signal and maybe a column.

    Parameters
    ----------
    *columns
        The signal, then the column when the check is about one.

    Returns
    -------
    CheckSpec
        The check, as `health_specs` builds it.
    """
    entry = ".".join(columns)
    manifest = {"assets": [{"table": "bronze.events", "health": {entry: "block"}}]}
    (spec,) = health_specs(manifest, "bronze.events")
    return spec


def test_the_health_result_reads_the_firings_of_the_own_chain(world: World) -> None:
    """Every branch of `_health_result`, on seeded rows (F16), over the task's own chain (I-1).

    Parameters
    ----------
    world
        The world.
    """
    _outside_table(world, "bronze.events", [1])
    _signal(world, "bronze.events", "volume", snapshot=5, state="fired")
    _signal(world, "bronze.events", "volume", snapshot=8, state="fired")
    _signal(world, "bronze.events", "range", snapshot=5, state="fired", column="price")
    _signal(
        world, "bronze.events", "range", snapshot=6, state="fired", column="qty", effect="block"
    )
    _signal(world, "bronze.events", "range", snapshot=7, state="fired", column="qty")
    _signal(world, "bronze.events", "null_rate", snapshot=5, state="ok", column="price")

    def judge(
        spec: CheckSpec, *chain: int, table: bool = True, kept: bool = True
    ) -> tuple[CheckOutcome, str | None, int | None]:
        """Judge one check over a chain of snapshots, the tip first.

        Parameters
        ----------
        spec
            The check.
        *chain
            The task's own snapshots, the tip first.
        table
            Whether the task has a table.
        kept
            Whether a set-back would keep the table's signals.

        Returns
        -------
        tuple
            What `_health_result` gives.
        """
        with world.session() as session:
            table_id = resolve_table_id(session, "dev", "bronze", "events") if table else None
            return task_checks_module._health_result(
                session, spec, table_id, list(chain), "dev", kept=kept
            )

    volume = _health_spec("volume")
    assert judge(volume, 5) == (
        CheckOutcome.FAILED,
        health_blocked("volume", None, 5, "bronze.events", "dev"),
        5,
    )
    assert judge(volume, 6) == (CheckOutcome.PASSED, None, 6)  # fired at another snapshot
    # An earlier commit of the chain fired: the check fails, naming the first that fired.
    assert judge(volume, 9, 8, 6, 5) == (
        CheckOutcome.FAILED,
        health_blocked("volume", None, 5, "bronze.events", "dev"),
        9,
    )
    assert judge(volume, 5, kept=False)[1] == health_blocked(
        "volume", None, 5, "bronze.events", "dev", kept=False
    )
    price = _health_spec("range", "price")
    assert judge(price, 5)[:2] == (
        CheckOutcome.FAILED,
        health_blocked("range", "price", 5, "bronze.events", "dev"),
    )
    # Snapshot 5 added data and recorded nothing for qty: not judged, so the check warns.
    assert judge(_health_spec("range", "qty"), 5) == (
        CheckOutcome.WARNED,
        unjudged_text("range", "qty", 5, "missing", "bronze.events", "dev"),
        5,
    )
    # The whole signal: any column whose firing was recorded as blocking.
    whole = _health_spec("range")
    assert judge(whole, 7, 6) == (
        CheckOutcome.FAILED,
        health_blocked("range", None, 6, "bronze.events", "dev"),
        7,
    )
    assert judge(whole, 7) == (CheckOutcome.PASSED, None, 7)  # only a warning column fired
    assert judge(_health_spec("null_rate"), 5) == (CheckOutcome.PASSED, None, 5)
    # Nothing to judge: no snapshot, no table, or a check that names no signal.
    assert judge(volume) == (CheckOutcome.PASSED, None, None)
    assert judge(volume, 5, table=False) == (CheckOutcome.PASSED, None, 5)
    unnamed = CheckSpec(
        name="bronze.events/health_x",
        table="bronze.events",
        check="health_x",
        path="assets/bronze/events",
        on_failure="block",
        description=None,
        sql="",
        generated=GeneratedFrom("health", ()),
    )
    assert judge(unnamed, 5) == (CheckOutcome.PASSED, None, 5)


def test_the_own_chain_falls_back_to_the_tip(world: World) -> None:
    """Without a tip nothing is judged; without its metadata or the tip in it, the tip alone.

    Parameters
    ----------
    world
        The world.
    """
    _outside_table(world, "bronze.events", [1])
    engine = _engine(world)
    with world.session() as session:
        table_id = resolve_table_id(session, "dev", "bronze", "events")
        assert table_id is not None
        own = task_checks_module._own_chain
        assert own(session, engine, "dev", None, replaces=False) == []
        assert (
            own(
                session,
                engine,
                "dev",
                Written(table_id, None, None, publishes=False),
                replaces=False,
            )
            == []
        )
        gone = Written(uuid.uuid4(), None, 7, publishes=False)
        assert own(session, engine, "dev", gone, replaces=False) == [7]  # no such table
        expired = Written(table_id, None, 4711, publishes=False)
        assert own(session, engine, "dev", expired, replaces=False) == [4711]  # not in the metadata


def test_the_log_line_of_a_failed_health_check_is_its_text() -> None:
    """A failed check with a text and no rows logs the text, not ``None rows``."""
    result = CheckResult(
        check_name="silver.orders/health_volume",
        check_path="assets/silver/orders.sql",
        table_name="silver.orders",
        on_failure="block",
        outcome=CheckOutcome.FAILED,
        failing_rows=None,
        error="health signal volume fired at snapshot 7",
        generated=True,
    )
    assert task_checks_module._check_lines([result], uuid.uuid4(), "dev") == (
        "  check health_volume: failed: health signal volume fired at snapshot 7",
    )


def test_a_health_check_whose_metadata_cannot_be_read_is_retried_and_never_passes(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Storage that does not answer while the chain is read retries the check phase.

    Parameters
    ----------
    world
        The world.
    monkeypatch
        Breaks the metadata read of the chain.
    """
    _built_once_then_redeployed(world, health={"volume": "block"})
    _start(world)
    claimed = _run_until(world, "silver.orders")
    callers: list[str] = []

    def unreadable(*args: object, **kwargs: object) -> object:
        callers.append(sys._getframe(1).f_code.co_name)
        raise OSError("the storage did not answer")

    monkeypatch.setattr(task_checks_module, "read_metadata", unreadable)
    run_claimed(_engine(world), claimed)

    task = _task(world, "silver.orders")
    assert "_own_chain" in callers
    assert (task.status, task.failure_code) == (TaskStatus.PENDING, "internal")
    assert _results(world, task.id) == []  # nothing judged, nothing passed


def test_a_hand_written_check_that_takes_a_health_check_name_fails_the_task(world: World) -> None:
    """A manifest stored without the deploy's refusal never lets one result replace the other.

    The hand-written `health_volume` always fails; under the health check's name its result
    would be replaced by the health check's ``passed``. Instead one result errors, blocks and
    names both files, even for a hand-written check that only warns.

    Parameters
    ----------
    world
        The world.
    """
    _built_once_then_redeployed(
        world,
        _check("health_volume", "SELECT id FROM silver.orders", on_failure="warn"),
        health={"volume": "block"},
    )
    _start(world)
    _drain(world)

    task = _task(world, "silver.orders")
    assert task.status is TaskStatus.FAILED
    assert _ids(world, "silver.orders") == [1, 1, 2]  # set back
    (result,) = _results(world, task.id)
    clash = (
        "two checks of `silver.orders` are named `health_volume` "
        "(`checks/silver/orders/health_volume.sql` and `assets/silver/orders`), so only "
        "one result would count; rename the hand-written check and deploy again."
    )
    assert (result.check_name, result.outcome, result.on_failure, result.error) == (
        "silver.orders/health_volume",
        CheckOutcome.ERRORED,
        "block",
        clash,
    )
    assert task.error_message is not None
    assert clash in task.error_message


def test_a_retried_attempt_is_judged_on_its_own_commits(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A replacing retry is judged on its own commits; an appending one on the whole chain (R3).

    Attempt 1 committed snapshot 2 (half the rows, its volume fired) and failed; attempt 2
    committed 3. A write that replaces the table superseded 2, so only 3 is judged. An append or
    merge keeps 2 published, so it is judged and blocks.

    Parameters
    ----------
    world
        The world.
    monkeypatch
        Serves the stamped metadata of the chain.
    """
    _outside_table(world, "bronze.events", [1])
    task_run = uuid.uuid4()

    def snapshot(snapshot_id: int, claim: int | None) -> Snapshot:
        """Build a snapshot on the previous one, stamped by an attempt when `claim` is given.

        Parameters
        ----------
        snapshot_id
            Its id and sequence number.
        claim
            The attempt's claim; `None` for the baseline.

        Returns
        -------
        Snapshot
            The snapshot.
        """
        stamp = (
            {} if claim is None else {TASK_RUN_PROPERTY: str(task_run), CLAIM_PROPERTY: str(claim)}
        )
        return Snapshot(
            snapshot_id=snapshot_id,
            parent_snapshot_id=snapshot_id - 1 if snapshot_id > 1 else None,
            sequence_number=snapshot_id,
            timestamp_ms=1_700_000_000_000 + snapshot_id,
            manifest_list=f"file:///t/metadata/snap-{snapshot_id}.avro",
            summary=Summary(Operation.APPEND, **stamp),
            schema_id=0,
        )

    snapshots = [snapshot(1, None), snapshot(2, 1), snapshot(3, 2)]
    metadata = apply_updates(
        build_table_metadata(
            schema=Schema(NestedField(1, "id", LongType(), required=False)),
            location="file:///t",
            partition_spec=None,
            sort_order=None,
            properties={},
        ),
        [
            *(AddSnapshotUpdate(snapshot=s) for s in snapshots),
            SetSnapshotRefUpdate(ref_name="main", type=SnapshotRefType.BRANCH, snapshot_id=3),
        ],
    )
    monkeypatch.setattr(task_checks_module, "read_metadata", lambda *args: metadata)
    _signal(world, "bronze.events", "volume", snapshot=2, state="fired")
    _signal(world, "bronze.events", "volume", snapshot=3)
    with world.session() as session:
        table_id = resolve_table_id(session, "dev", "bronze", "events")
        assert table_id is not None
        written = Written(table_id, 1, 3, publishes=False)
        own = task_checks_module._own_chain
        replaced = own(session, _engine(world), "dev", written, replaces=True)
        assert replaced == [3]
        assert task_checks_module._health_result(
            session, _health_spec("volume"), table_id, replaced, "dev", kept=True
        ) == (CheckOutcome.PASSED, None, 3)
        # An append keeps attempt 1's commit published: it is judged, and it fired.
        appended = own(session, _engine(world), "dev", written, replaces=False)
        assert appended == [3, 2]
        assert (
            task_checks_module._health_result(
                session, _health_spec("volume"), table_id, appended, "dev", kept=True
            )[0]
            is CheckOutcome.FAILED
        )


def test_a_block_signal_that_could_not_be_judged_warns(world: World) -> None:
    """Unreadable files, a computing error or nothing recorded: the check warns and says why.

    A firing anywhere in the chain still fails; a compaction, which records only the schema,
    and a whole column signal with no table-level reason are judged.

    Parameters
    ----------
    world
        The world.
    """
    _outside_table(world, "bronze.events", [1])
    _signal(world, "bronze.events", "volume", snapshot=10)
    _signal(
        world,
        "bronze.events",
        "range",
        snapshot=10,
        state="no_metrics",
        observed={"reason": "unreadable"},
    )
    _signal(
        world,
        "bronze.events",
        "volume",
        snapshot=11,
        state="no_metrics",
        observed={"reason": "error"},
    )
    _signal(world, "bronze.events", "volume", snapshot=12, state="fired")
    _signal(world, "bronze.events", "schema", snapshot=13)  # a compaction
    _signal(world, "bronze.events", "volume", snapshot=14)
    _signal(world, "bronze.events", "range", snapshot=14, column="price")
    _signal(
        world,
        "bronze.events",
        "null_rate",
        snapshot=14,
        state="no_metrics",
        observed={"reason": "no_rows"},
        column="price",
    )

    def judge(spec: CheckSpec, *chain: int) -> tuple[CheckOutcome, str | None, int | None]:
        """Judge one check over a chain of snapshots, the tip first.

        Parameters
        ----------
        spec
            The check.
        *chain
            The task's own snapshots, the tip first.

        Returns
        -------
        tuple
            What `_health_result` gives.
        """
        with world.session() as session:
            table_id = resolve_table_id(session, "dev", "bronze", "events")
            return task_checks_module._health_result(
                session, spec, table_id, list(chain), "dev", kept=True
            )

    def warned(signal: str, column: str | None, snapshot: int, reason: str, tip: int) -> tuple:
        """Build the warned result of a check.

        Parameters
        ----------
        signal
            The signal.
        column
            The column, or `None`.
        snapshot
            The snapshot not judged.
        reason
            Why.
        tip
            The chain's tip.

        Returns
        -------
        tuple
            The expected result.
        """
        text = unjudged_text(signal, column, snapshot, reason, "bronze.events", "dev")
        return CheckOutcome.WARNED, text, tip

    assert judge(_health_spec("range", "price"), 10) == warned(
        "range", "price", 10, "unreadable", 10
    )
    assert judge(_health_spec("range"), 10) == warned("range", None, 10, "unreadable", 10)
    assert judge(_health_spec("volume"), 11) == warned("volume", None, 11, "error", 11)
    assert judge(_health_spec("volume"), 12, 11)[0] is CheckOutcome.FAILED  # a firing wins
    assert judge(_health_spec("schema"), 10) == warned("schema", None, 10, "missing", 10)
    assert judge(_health_spec("volume"), 13) == (CheckOutcome.PASSED, None, 13)
    assert judge(_health_spec("range", "price"), 14) == (CheckOutcome.PASSED, None, 14)
    assert judge(_health_spec("null_rate", "price"), 14) == (CheckOutcome.PASSED, None, 14)
    assert judge(_health_spec("range"), 14) == (CheckOutcome.PASSED, None, 14)
    assert judge(_health_spec("volume"), 99) == warned("volume", None, 99, "missing", 99)
    # `range: off` for the table and `{name: block}` on a string column: the table-level off row
    # does not judge the column, which records nothing (R4).
    _signal(world, "bronze.events", "volume", snapshot=15)
    _signal(world, "bronze.events", "range", snapshot=15, state="off", effect="off")
    assert judge(_health_spec("range", "name"), 15) == warned("range", "name", 15, "missing", 15)
    # A delete-only commit adds no files, so it records no column rows: not a gap (F1).
    _signal(world, "bronze.events", "volume", snapshot=16, observed={"added": 0, "total": 5})
    assert judge(_health_spec("range", "price"), 16) == (CheckOutcome.PASSED, None, 16)
    # The same with volume off: its row stores nothing, and the commit added no files either.
    _signal(world, "bronze.events", "volume", snapshot=17, state="off", effect="off")
    _signal(world, "bronze.events", "schema", snapshot=17)
    assert judge(_health_spec("range", "price"), 17) == (CheckOutcome.PASSED, None, 17)
    off = SimpleNamespace(
        snapshot_id=17, signal="volume", column_name=None, state="off", effect="off", observed=None
    )
    schema = SimpleNamespace(
        snapshot_id=17, signal="schema", column_name=None, state="ok", effect="warn", observed={}
    )
    rows = cast("list[Any]", [off, schema])
    assert task_checks_module._unjudged(rows, "range", "price", [17]) is None


def test_a_run_whose_block_signal_could_not_be_judged_goes_on_and_says_so(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A commit whose signals fail to compute: the run succeeds, the check warns, it is told.

    Parameters
    ----------
    world
        The world.
    monkeypatch
        Breaks the computing of the signals and records what is queued.
    """
    _built_once_then_redeployed(world, health={"volume": "block"})

    def broken(*args: object, **kwargs: object) -> object:
        """Fail as a damaged file would.

        Parameters
        ----------
        *args
            Ignored.
        **kwargs
            Ignored.

        Raises
        ------
        RuntimeError
            Always.
        """
        raise RuntimeError("a damaged file")

    told: list[tuple[str, str | None, str]] = []

    def queue(session: object, **kwargs: list[TableHealthSignal]) -> list[object]:
        """Record the rows a message would tell of.

        Parameters
        ----------
        session
            Ignored.
        **kwargs
            The call's arguments; ``rows`` is read.

        Returns
        -------
        list
            No deliveries.
        """
        told.extend((r.signal, r.column_name, r.state) for r in kwargs["rows"])
        return []

    monkeypatch.setattr(health_record_module, "_observe", broken)
    monkeypatch.setattr(task_checks_module, "queue_health_notifications", queue)
    _start(world)
    _drain(world)

    task = _task(world, "silver.orders")
    assert task.status is TaskStatus.SUCCEEDED
    assert _ids(world, "silver.orders") == [11, 11, 12]  # not set back
    (result,) = _results(world, task.id)
    assert (result.check_name, result.outcome, result.on_failure) == (
        "silver.orders/health_volume",
        CheckOutcome.WARNED,
        "warn",
    )
    assert result.error is not None
    assert result.error.startswith("health signal volume could not be judged: computing it failed")
    assert task.error_message is not None
    assert task.error_message.startswith(
        "1 check warned: `health_volume` (health signal volume could not be judged: computing "
    )
    assert told == [("volume", None, "no_metrics")]
    assert _task(world, "gold.report").status is TaskStatus.SUCCEEDED


def test_a_failure_to_tell_of_an_unjudged_signal_never_stops_the_checks(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A damaged manifest while queuing is logged; the run still ends as the checks say.

    Parameters
    ----------
    world
        The world.
    monkeypatch
        Breaks the computing of the signals and the queuing.
    """
    _built_once_then_redeployed(world, health={"volume": "block"})

    def broken(*args: object, **kwargs: object) -> object:
        """Fail as a damaged file would.

        Parameters
        ----------
        *args
            Ignored.
        **kwargs
            Ignored.

        Raises
        ------
        RuntimeError
            Always.
        """
        raise RuntimeError("damaged")

    monkeypatch.setattr(health_record_module, "_observe", broken)
    monkeypatch.setattr(task_checks_module, "queue_health_notifications", broken)
    _start(world)
    _drain(world)
    assert _task(world, "silver.orders").status is TaskStatus.SUCCEEDED


def test_telling_of_unjudged_signals_needs_a_recorded_reason_and_passes_database_errors(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a row that says why there is nothing to tell; a database error is not swallowed.

    Parameters
    ----------
    world
        The world.
    monkeypatch
        Breaks the queuing with a database error.
    """
    _outside_table(world, "bronze.events", [1])
    _signal(
        world,
        "bronze.events",
        "volume",
        snapshot=11,
        state="no_metrics",
        observed={"reason": "error"},
    )

    def lost(*args: object, **kwargs: object) -> object:
        """Fail as a lost database connection would.

        Parameters
        ----------
        *args
            Ignored.
        **kwargs
            Ignored.

        Raises
        ------
        OperationalError
            Always.
        """
        raise OperationalError("SELECT 1", {}, Exception("connection lost"))

    monkeypatch.setattr(task_checks_module, "queue_health_notifications", lost)
    tell = task_checks_module._tell_unjudged
    with world.session() as session:
        table_id = resolve_table_id(session, "dev", "bronze", "events")
        assert table_id is not None
        environment_id = session.scalars(
            select(Environment.id).where(Environment.name == "dev")
        ).one()
        tell(session, environment_id, "bronze.events", table_id, [12], [("volume", None)])
        with pytest.raises(OperationalError):
            tell(session, environment_id, "bronze.events", table_id, [11], [("volume", None)])


def _row(
    signal: str,
    column: str | None,
    state: str,
    observed: dict[str, object] | None = None,
    *,
    effect: str = "block",
) -> SimpleNamespace:
    """Build a row of snapshot 20 as `_health_result` reads it.

    Parameters
    ----------
    signal
        The signal.
    column
        The column, or `None`.
    state
        The state.
    observed
        What was observed.
    effect
        The effect recorded.

    Returns
    -------
    SimpleNamespace
        The row's columns, by name.
    """
    return SimpleNamespace(
        snapshot_id=20,
        signal=signal,
        column_name=column,
        state=state,
        effect=effect,
        observed=observed,
    )


@pytest.mark.parametrize(
    ("signal", "column", "observed", "reason"),
    [
        ("volume", None, None, "unstorable"),  # a number that did not fit
        ("volume", None, {"reason": "not_finite"}, "not_finite"),
        ("range", "price", None, "unstorable"),
        ("range", "price", {"reason": "not_recorded"}, "not_recorded"),
    ],
)
def test_an_own_row_without_metrics_is_not_judged(
    signal: str, column: str | None, observed: dict[str, object] | None, reason: str
) -> None:
    """A `no_metrics` row of the check's own signal or column warns, but for `no_rows`.

    Parameters
    ----------
    signal
        The check's signal.
    column
        Its column, or `None`.
    observed
        The row's observed values.
    reason
        The reason the check names.
    """
    volume = _row("volume", None, "ok", {"added": 3, "total": 3}, effect="warn")
    own = _row(signal, column, "no_metrics", observed)
    rows = [volume, own] if signal != "volume" else [own]
    assert task_checks_module._unjudged(cast("list[Any]", rows), signal, column, [20]) == (
        20,
        reason,
    )
    # The whole of the column signal judges the column rows that block.
    if column is not None:
        assert task_checks_module._unjudged(cast("list[Any]", rows), signal, None, [20]) == (
            20,
            reason,
        )


def test_no_rows_and_no_history_are_judged() -> None:
    """A column without values had nothing to judge, and a signal without history is not due."""
    volume = _row("volume", None, "no_history", {"added": 3, "total": 3})
    empty = _row("null_rate", "price", "no_metrics", {"reason": "no_rows"})
    rows = cast("list[Any]", [volume, empty])
    assert task_checks_module._unjudged(rows, "null_rate", "price", [20]) is None
    assert task_checks_module._unjudged(rows, "volume", None, [20]) is None


def test_a_column_without_recorded_bounds_warns_through_the_check(world: World) -> None:
    """`range: {price: block}` and a commit whose files carry no bounds for price: warned.

    Parameters
    ----------
    world
        The world.
    """
    _outside_table(world, "bronze.events", [1])
    _signal(world, "bronze.events", "volume", snapshot=21)
    _signal(
        world,
        "bronze.events",
        "range",
        snapshot=21,
        column="price",
        state="no_metrics",
        observed={"reason": "not_recorded"},
        effect="block",
    )
    with world.session() as session:
        table_id = resolve_table_id(session, "dev", "bronze", "events")
        assert task_checks_module._health_result(
            session, _health_spec("range", "price"), table_id, [21], "dev", kept=True
        ) == (
            CheckOutcome.WARNED,
            unjudged_text("range", "price", 21, "not_recorded", "bronze.events", "dev"),
            21,
        )


def test_an_own_row_without_metrics_is_told_like_an_unreadable_commit(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`not_recorded`, `not_finite` and an unstorable number have a row to name: it is told.

    A `no_rows` row and a row of a column no check names are not.

    Parameters
    ----------
    world
        The world.
    monkeypatch
        Records what is queued.
    """
    _outside_table(world, "bronze.events", [1])
    _signal(world, "bronze.events", "volume", snapshot=30, state="no_metrics")  # unstorable
    _signal(
        world,
        "bronze.events",
        "range",
        snapshot=30,
        column="price",
        state="no_metrics",
        observed={"reason": "not_recorded"},
        effect="block",
    )
    _signal(
        world,
        "bronze.events",
        "range",
        snapshot=30,
        column="qty",
        state="no_metrics",
        observed={"reason": "not_finite"},
        effect="warn",
    )
    _signal(
        world,
        "bronze.events",
        "null_rate",
        snapshot=30,
        column="price",
        state="no_metrics",
        observed={"reason": "no_rows"},
        effect="block",
    )
    told: list[tuple[str, str | None]] = []

    def queue(session: object, **kwargs: list[TableHealthSignal]) -> list[object]:
        """Record the rows a message would tell of.

        Parameters
        ----------
        session
            Ignored.
        **kwargs
            The call's arguments; ``rows`` is read.

        Returns
        -------
        list
            No deliveries.
        """
        told.extend((r.signal, r.column_name) for r in kwargs["rows"])
        return []

    monkeypatch.setattr(task_checks_module, "queue_health_notifications", queue)
    with world.session() as session:
        table_id = resolve_table_id(session, "dev", "bronze", "events")
        assert table_id is not None
        environment_id = session.scalars(
            select(Environment.id).where(Environment.name == "dev")
        ).one()
        checks = [("volume", None), ("range", "price"), ("null_rate", "price")]
        task_checks_module._tell_unjudged(
            session, environment_id, "bronze.events", table_id, [30], checks
        )
    assert sorted(told, key=repr) == [("range", "price"), ("volume", None)]


def test_a_commit_that_added_no_data_files_is_judged_and_not_told() -> None:
    """A real pyiceberg delete summary: volume is an observation of 0 rows, columns are skipped."""
    summary = Summary(
        Operation.DELETE,
        **{"deleted-records": "5", "deleted-data-files": "1", "total-records": "0"},
    )
    seen = volume_signal(summary, [], no_data_files=True)
    volume = _row("volume", None, seen.state, seen.observed)
    schema = _row("schema", None, "ok", {}, effect="warn")
    rows = cast("list[Any]", [volume, schema])
    assert task_checks_module._unjudged(rows, "range", "price", [20]) is None
    assert task_checks_module._unjudged(rows, "volume", None, [20]) is None
    assert task_checks_module._judges(cast("Any", volume), "volume", None)
    assert seen.state != "no_metrics"  # nothing for `_tell_unjudged` to name
    # Then an append in the same run: both commits judged, nothing fired, nothing to tell.
    appended = volume_signal({"added-records": "1000"}, [])
    load = SimpleNamespace(**{**vars(volume), "snapshot_id": 21, "observed": appended.observed})
    load.state = appended.state
    both = cast("list[Any]", [volume, schema, load])
    assert task_checks_module._unjudged(both, "volume", None, [21, 20]) is None


def test_one_message_per_check_names_its_columns(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`null_rate: block` with 200 columns not recorded: one row is told, not 200.

    Parameters
    ----------
    world
        The world.
    monkeypatch
        Records what is queued.
    """
    _outside_table(world, "bronze.events", [1])
    for index in range(200):
        _signal(
            world,
            "bronze.events",
            "null_rate",
            snapshot=40,
            column=f"c{index:03}",
            state="no_metrics",
            observed={"reason": "not_recorded"},
            effect="block",
        )
    told: list[tuple[str, str | None]] = []

    def queue(session: object, **kwargs: list[TableHealthSignal]) -> list[object]:
        """Record the rows a message would tell of.

        Parameters
        ----------
        session
            Ignored.
        **kwargs
            The call's arguments; ``rows`` is read.

        Returns
        -------
        list
            No deliveries.
        """
        told.extend((r.signal, r.column_name) for r in kwargs["rows"])
        return []

    monkeypatch.setattr(task_checks_module, "queue_health_notifications", queue)
    with world.session() as session:
        table_id = resolve_table_id(session, "dev", "bronze", "events")
        assert table_id is not None
        environment_id = session.scalars(
            select(Environment.id).where(Environment.name == "dev")
        ).one()
        task_checks_module._tell_unjudged(
            session, environment_id, "bronze.events", table_id, [40], [("null_rate", None)]
        )
    assert told == [("null_rate", "c000")]
