"""The blocking health checks of the check phase: `health: {signal: block}` (#340)."""

from __future__ import annotations

import sys
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from check_run_helpers import (
    _built_once_then_redeployed,
    _drain,
    _events,
    _ids,
    _outside_table,
    _results,
    _run_until,
)
from sqlalchemy import select
from task_run_helpers import World, _engine, _start, _task

import lakana_worker.task_checks as task_checks_module
from lakana_core.catalog.health_read import Visibility, current_signals
from lakana_core.catalog.health_record import recent_observations
from lakana_core.catalog.identity import resolve_table_id
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
    assert judge(_health_spec("range", "qty"), 5)[0] is CheckOutcome.PASSED
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
        assert own(session, engine, "dev", None) == []
        assert own(session, engine, "dev", Written(table_id, None, None, publishes=False)) == []
        gone = Written(uuid.uuid4(), None, 7, publishes=False)
        assert own(session, engine, "dev", gone) == [7]  # no such table
        expired = Written(table_id, None, 4711, publishes=False)
        assert own(session, engine, "dev", expired) == [4711]  # not in the metadata


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
