"""A table kept after a failed check: its note, the check task of a later run, its readers."""

from __future__ import annotations

import threading
import uuid

import pytest
import sqlalchemy.event
from check_run_helpers import (
    _DUPLICATE_IDS,
    _PASSING,
    _UNPUBLISHED,
    _after,
    _check,
    _checked,
    _claim_ready,
    _counting,
    _drain,
    _events,
    _expire_lease_and_tick,
    _ids,
    _keep_note_on,
    _marker,
    _outside_table,
    _refused,
    _refused_text,
    _results,
    _rows,
    _run_until,
    _started_by_an_older_server,
    _unique_rows,
)
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session
from task_run_helpers import (
    World,
    _attempts,
    _claim,
    _engine,
    _entry,
    _exists,
    _fake_run_one,
    _log,
    _set_cancel_requested,
    _start,
    _task,
)

import lakana_worker.checks as checks_module
import lakana_worker.executor as executor_module
import lakana_worker.task_checks as task_checks_module
import lakana_worker.tasks as tasks_module
from lakana_core.auth import (
    AllowAllAuthorization,
)
from lakana_core.catalog.identity import resolve_table_id
from lakana_core.catalog.json_schema import ColumnSpec
from lakana_core.catalog.service import (
    CatalogService,
    Rollback,
)
from lakana_core.errors import (
    KeptAfterCheckError,
    PermissionDenied,
)
from lakana_core.identifiers import TableRef
from lakana_core.kept_mark import KeptSeen
from lakana_core.models import (
    AuditEventType,
    CheckOutcome,
    CheckResult,
    QueryRun,
    QueryRunStatus,
    RollbackOutcome,
    Table,
    TaskRun,
    TaskStatus,
)
from lakana_core.runs.checks import (
    ERROR_KEPT,
    kept_failure_message,
)
from lakana_core.runs.kept import (
    KEPT_SNAPSHOT_UNKNOWN,
    KeptNote,
    clear_kept_in_attempt,
    kept_message,
)
from lakana_core.runs.selection import Selection
from lakana_core.runs.tasks import (
    ClaimedTask,
    FailureKind,
)
from lakana_core.storage.resolver import StorageResolver
from lakana_worker.executor import (
    QueryCancelled,
)
from lakana_worker.task_lease import LeaseKeeper
from lakana_worker.task_outcome import (
    classify,
)
from lakana_worker.tasks import run_claimed


def test_a_failing_keep_check_fails_the_task_and_keeps_the_table(world: World) -> None:
    _checked(world, _check("unique_id", _DUPLICATE_IDS, on_failure="keep"))
    run_id = _start(world)
    _drain(world)

    task = _task(world, "silver.orders")
    assert (task.status, task.attempt) == (TaskStatus.FAILED, 1)
    results = _results(world, task.id)
    assert task.error_message == kept_failure_message(
        kept=results, run_id=run_id, environment="dev"
    )
    assert task.error_message.startswith(
        "Check `silver.orders/unique_id` failed: 1 row (checks/silver/orders/unique_id.sql). "
        f"{_unique_rows(run_id)}"
        "The table keeps this run's data (`on_failure: keep`)"
    )
    assert task.failure_code == "check_blocked"
    (result,) = results
    assert (result.outcome, result.on_failure, result.rollback_outcome) == (
        CheckOutcome.FAILED,
        "keep",
        None,
    )
    assert _ids(world, "silver.orders") == [1, 1, 2]
    assert _marker(world, "silver.orders") is None
    assert _events(world, AuditEventType.TABLE_ROLLED_BACK) == []
    (failed,) = _events(world, AuditEventType.CHECK_FAILED)
    assert (failed["outcome"], failed["rows"]) == ("failed", 1)


def test_a_keep_check_that_cannot_run_stops_the_task(world: World) -> None:
    _checked(
        world, _check("broken", "SELECT nope FROM silver.orders WHERE nope > 0", on_failure="keep")
    )
    run_id = _start(world)
    _drain(world)

    task = _task(world, "silver.orders")
    assert task.status is TaskStatus.FAILED
    assert task.failure_code == "check_error"
    (result,) = _results(world, task.id)
    assert (result.outcome, result.on_failure, result.rollback_outcome) == (
        CheckOutcome.ERRORED,
        "keep",
        None,
    )
    assert task.error_message == kept_failure_message(
        kept=[result], run_id=run_id, environment="dev"
    )
    assert "could not run" in task.error_message
    assert _ids(world, "silver.orders") == [1, 1, 2]
    assert _events(world, AuditEventType.TABLE_ROLLED_BACK) == []


def test_a_block_failure_beside_a_keep_failure_rolls_back(world: World) -> None:
    _checked(
        world,
        _check("a_kept", _DUPLICATE_IDS, on_failure="keep"),
        _check("b_blocking", _DUPLICATE_IDS),
    )
    run_id = _start(world)
    _drain(world)

    task = _task(world, "silver.orders")
    assert (task.status, task.error_message) == (
        TaskStatus.FAILED,
        "Check `silver.orders/b_blocking` failed: 1 row "
        f"(checks/silver/orders/b_blocking.sql). {_UNPUBLISHED} "
        f"{_rows(run_id, 'silver.orders/b_blocking')}{_after(run_id)}",
    )
    outcomes = {
        r.check_name.rpartition("/")[2]: r.rollback_outcome for r in _results(world, task.id)
    }
    assert outcomes == {"a_kept": None, "b_blocking": RollbackOutcome.UNPUBLISHED}
    assert _marker(world, "silver.orders") == run_id
    assert len(_events(world, AuditEventType.TABLE_ROLLED_BACK)) == 1


def test_a_first_build_that_fails_only_keep_checks_is_published(world: World) -> None:
    _checked(world, _check("unique_id", _DUPLICATE_IDS, on_failure="keep"))
    _start(world)
    _drain(world)

    assert _task(world, "silver.orders").status is TaskStatus.FAILED
    assert _marker(world, "silver.orders") is None
    assert _ids(world, "silver.orders") == [1, 1, 2]


def test_downstream_of_a_kept_table_is_not_built_in_that_run(world: World) -> None:
    _checked(world, _check("unique_id", _DUPLICATE_IDS, on_failure="keep"))
    _start(world)
    _drain(world)

    assert _task(world, "gold.report").status is TaskStatus.UPSTREAM_FAILED
    assert not _exists(world, "gold.report")


def test_a_failing_keep_check_of_a_source_table_fails_its_task(world: World) -> None:
    _outside_table(world, "bronze.events", [1, 1, 2])
    world.set_manifest(
        _entry("silver.orders", "SELECT id FROM bronze.events", references=["bronze.events"]),
        checks=[
            _check(
                "unique_id",
                "SELECT id FROM bronze.events GROUP BY id HAVING count(*) > 1",
                table="bronze.events",
                on_failure="keep",
            )
        ],
    )
    run_id = _start(world)
    _drain(world)

    task = _task(world, "bronze.events")
    assert task.status is TaskStatus.FAILED
    assert task.error_message == (
        "Check `bronze.events/unique_id` failed: 1 row (checks/bronze/events/unique_id.sql). This "
        "project does not build `bronze.events`, so it was not changed; the assets that read it "
        f"were not built. {_rows(run_id, 'bronze.events/unique_id')}{_after(run_id)}"
    )
    assert _task(world, "silver.orders").status is TaskStatus.UPSTREAM_FAILED


def _note_of(world: World, dotted: str) -> tuple[uuid.UUID | None, uuid.UUID | None, str | None]:
    """Read the note of a kept table on a second session.

    Parameters
    ----------
    world
        The world.
    dotted
        The table.

    Returns
    -------
    tuple
        `Table.kept_run_id`, `kept_task_run_id` and `kept_check`.
    """
    ref = TableRef.from_dotted("dev", dotted)
    with world.session() as session:
        table_id = resolve_table_id(session, "dev", ref.namespace.joined(), ref.name)
        assert table_id is not None
        row = session.execute(
            select(Table.kept_run_id, Table.kept_task_run_id, Table.kept_check).where(
                Table.id == table_id
            )
        ).one()
        return row[0], row[1], row[2]


_STATEMENTS = {False: 62, True: 127}
"""Statements of a task's own thread, by whether it has checks (measured before the note).

Two of them look for the project's descriptions of the table the task creates. With checks, each
of the two check queries that name the table also counts a read of it (a lookup of the table and
an upsert), which is the four more. Two more look for the table's stored columns, one when the
table is created and one at the commit that gives it data. Five come with the claim of the new
table's location (#90): the database's time and the insert of the claim, the reload of the
namespace and the environment that its commit expired, and the publish fence's update. One more
is the share lock on the environment row that the publish takes before the fence (#213)."""


def _statements_of(world: World, claimed: ClaimedTask) -> int:
    """Run a claim and count the statements its own thread issued (not its lease keeper's).

    Parameters
    ----------
    world
        The world.
    claimed
        The claim.

    Returns
    -------
    int
        The number of statements.
    """
    seen: list[str] = []
    mine = threading.get_ident()

    def record(conn: object, cursor: object, statement: str, *rest: object) -> None:
        if threading.get_ident() == mine:
            seen.append(statement)

    sqlalchemy.event.listen(world.engine, "before_cursor_execute", record)
    try:
        run_claimed(_engine(world), claimed)
    finally:
        sqlalchemy.event.remove(world.engine, "before_cursor_execute", record)
    return len(seen)


def test_a_failing_keep_check_notes_it_on_the_table(world: World) -> None:
    _checked(world, _check("unique_id", _DUPLICATE_IDS, on_failure="keep"))
    run_id = _start(world)
    _drain(world)

    task = _task(world, "silver.orders")
    assert task.status is TaskStatus.FAILED
    assert _note_of(world, "silver.orders") == (run_id, task.id, "silver.orders/unique_id")
    (event,) = _events(world, AuditEventType.TABLE_KEPT)
    assert event == {
        "checks": ["silver.orders/unique_id"],
        "run_id": str(run_id),
        "task_run_id": str(task.id),
    }


def _source_keep_world(world: World, ids: list[int]) -> None:
    """Deploy `silver.orders` reading `bronze.events`, whose uniqueness check is `keep`.

    Parameters
    ----------
    world
        The world.
    ids
        The ids of `bronze.events`, created if it does not exist.
    """
    if not _exists(world, "bronze.events"):
        _outside_table(world, "bronze.events", ids)
    world.set_manifest(
        _entry("silver.orders", "SELECT id FROM bronze.events", references=["bronze.events"]),
        checks=[
            _check(
                "unique_id",
                "SELECT id FROM bronze.events GROUP BY id HAVING count(*) > 1",
                table="bronze.events",
                on_failure="keep",
            )
        ],
    )


def test_a_failing_keep_check_of_a_source_table_leaves_no_note(world: World) -> None:
    _source_keep_world(world, [1, 1, 2])
    run_id = _start(world)
    _drain(world)

    task = _task(world, "bronze.events")
    assert task.status is TaskStatus.FAILED
    assert task.error_message is not None and task.error_message.startswith(
        "Check `bronze.events/unique_id` failed: 1 row"
    )
    assert _note_of(world, "bronze.events") == (None, None, None)
    assert _events(world, AuditEventType.TABLE_KEPT) == []
    assert task.run_id == run_id


def test_a_source_check_never_moves_the_note_of_an_asset_task(world: World) -> None:
    _source_keep_world(world, [1, 1, 2])
    first_note = _keep_note_on(world, "bronze.events")
    before = _note_of(world, "bronze.events")
    _start(world)
    _drain(world)

    assert _task(world, "bronze.events").status is TaskStatus.FAILED
    assert _note_of(world, "bronze.events") == before
    assert before[0] == first_note
    assert _events(world, AuditEventType.TABLE_KEPT) == []
    assert _events(world, AuditEventType.TABLE_KEPT_CLEARED) == []


@pytest.mark.slow
def test_a_passing_check_phase_clears_the_note(world: World) -> None:
    _checked(world, _check("unique_id", _DUPLICATE_IDS, on_failure="keep"))
    first = _start(world)
    _drain(world)
    assert _note_of(world, "silver.orders")[0] == first

    _checked(
        world,
        _check("unique_id", _DUPLICATE_IDS, on_failure="keep"),
        sql="SELECT DISTINCT id FROM bronze.src",
    )
    second = _start(world)
    _drain(world)

    task = _task(world, "silver.orders")
    assert (task.run_id, task.status) == (second, TaskStatus.SUCCEEDED)
    assert _note_of(world, "silver.orders") == (None, None, None)
    (event,) = _events(world, AuditEventType.TABLE_KEPT_CLEARED)
    assert event == {
        "why": "checks_passed",
        "run_id": str(second),
        "task_run_id": str(task.id),
    }


@pytest.mark.slow
def test_a_check_phase_that_fails_again_moves_the_note_to_the_new_run(world: World) -> None:
    _checked(world, _check("unique_id", _DUPLICATE_IDS, on_failure="keep"))
    _start(world)
    _drain(world)
    second = _start(world)
    _drain(world)

    task = _task(world, "silver.orders")
    assert task.run_id == second
    assert _note_of(world, "silver.orders") == (second, task.id, "silver.orders/unique_id")
    assert _events(world, AuditEventType.TABLE_KEPT_CLEARED) == []
    assert len(_events(world, AuditEventType.TABLE_KEPT)) == 2


def _earlier_notes_of(world: World, dotted: str) -> list[dict[str, object]]:
    """Read the stack of earlier notes of a table on a second session.

    Parameters
    ----------
    world
        The world.
    dotted
        The table.

    Returns
    -------
    list of dict
        `Table.kept_earlier`.
    """
    ref = TableRef.from_dotted("dev", dotted)
    with world.session() as session:
        table_id = resolve_table_id(session, "dev", ref.namespace.joined(), ref.name)
        assert table_id is not None
        return list(session.scalars(select(Table.kept_earlier).where(Table.id == table_id)).one())


@pytest.mark.slow
def test_the_notes_of_later_runs_stack_and_a_passing_phase_stacks_the_mark_it_clears(
    world: World,
) -> None:
    _checked(world, _check("unique_id", _DUPLICATE_IDS, on_failure="keep"))
    first = _start(world)
    _drain(world)
    assert _earlier_notes_of(world, "silver.orders") == [{"snapshot_id": None}]  # a first build
    second = _start(world)
    _drain(world)

    stack = _earlier_notes_of(world, "silver.orders")
    assert [entry.get("run_id") for entry in stack] == [None, str(first)]
    assert stack[1]["snapshot_id"] is not None  # the snapshot the first run's verdict was on

    _checked(world)
    _start(world)
    _drain(world)
    assert _note_of(world, "silver.orders") == (None, None, None)
    # The clear keeps the mark it removed, as the verdict on the data it was about.
    stack = _earlier_notes_of(world, "silver.orders")
    assert [entry.get("run_id") for entry in stack] == [None, str(first), str(second)]


@pytest.mark.slow
def test_a_set_back_of_a_later_kept_run_puts_the_earlier_mark_back(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, first_task = _kept_first_run(world)
    kept_ids = _ids(world, "silver.orders")
    second = _start(world)
    claimed = _run_until(world, "silver.orders")
    died = threading.Event()
    real = tasks_module._finish

    def dying(*args: object) -> None:
        if not died.is_set():
            died.set()
            raise SystemExit("the worker died after the note, before the record")
        real(*args)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(tasks_module, "_finish", dying)
    with pytest.raises(SystemExit):
        run_claimed(_engine(world), claimed)
    assert _note_of(world, "silver.orders")[0] == second  # the note moved to the new run
    _set_cancel_requested(world, second)
    _expire_lease_and_tick(world, "silver.orders")
    again = _claim_ready(world)
    assert again is not None and again.setting_back

    run_claimed(_engine(world), again)

    assert _ids(world, "silver.orders") == kept_ids
    assert _note_of(world, "silver.orders") == (first, first_task, "silver.orders/unique_id")
    (event,) = _events(world, AuditEventType.TABLE_ROLLED_BACK)
    assert event["kept_restored"]["run_id"] == str(first)
    with world.session() as session:
        described = CatalogService(
            session, StorageResolver(session), AllowAllAuthorization()
        ).describe_table(world.admin, TableRef.from_dotted("dev", "silver.orders"))
    assert described.state == "kept" and str(first) in (described.state_message or "")


@pytest.mark.slow
def test_a_set_back_after_another_runs_clear_puts_the_earlier_mark_back(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A passing check of another run clears the mark, then the writer of the data is set back."""
    first, first_task = _kept_first_run(world)
    kept_ids = _ids(world, "silver.orders")
    second = _start(world)
    claimed = _run_until(world, "silver.orders")

    def dies(*args: object, **kwargs: object) -> KeptNote:
        raise SystemExit("the worker died after the write, before its verdict")

    monkeypatch.setattr(task_checks_module, "note_kept", dies)
    with pytest.raises(SystemExit):
        run_claimed(_engine(world), claimed)
    assert _note_of(world, "silver.orders")[0] == first  # the second run has not judged its data
    writer = _task(world, "silver.orders")
    assert writer.written_snapshot_after is not None
    ref = TableRef.from_dotted("dev", "silver.orders")
    with world.session() as session:
        table_id = resolve_table_id(session, "dev", ref.namespace.joined(), ref.name)
        assert table_id is not None
        seen = session.execute(
            select(Table.kept_run_id, Table.kept_task_run_id, Table.kept_snapshot_id).where(
                Table.id == table_id
            )
        ).one()
        assert clear_kept_in_attempt(
            session,
            task_run_id=writer.id,
            table_id=table_id,
            why="checks_passed",
            snapshot_id=writer.written_snapshot_after,
            seen=KeptSeen(seen.kept_run_id, seen.kept_task_run_id, seen.kept_snapshot_id),
        )
        session.commit()
    assert _note_of(world, "silver.orders") == (None, None, None)  # readers are let in
    _set_cancel_requested(world, second)
    _expire_lease_and_tick(world, "silver.orders")
    again = _claim_ready(world)
    assert again is not None and again.setting_back

    run_claimed(_engine(world), again)

    assert _ids(world, "silver.orders") == kept_ids
    assert _note_of(world, "silver.orders") == (first, first_task, "silver.orders/unique_id")
    (event,) = _events(world, AuditEventType.TABLE_ROLLED_BACK)
    assert event["kept_restored"]["run_id"] == str(first)


@pytest.mark.slow
def test_a_clear_lands_with_the_success_or_not_at_all(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A task whose success is not recorded has not cleared the note: it may still be set back."""
    first, first_task = _kept_first_run(world)
    _deploy(world, _check("unique_id", _PASSING, on_failure="keep"))
    _start(world)
    claimed = _run_until(world, "silver.orders")
    monkeypatch.setattr(tasks_module, "record_attempt", lambda *a, **k: False)

    run_claimed(_engine(world), claimed)

    assert _note_of(world, "silver.orders") == (first, first_task, "silver.orders/unique_id")
    assert _events(world, AuditEventType.TABLE_KEPT_CLEARED) == []


def _snapshot_verdict_of(world: World, dotted: str) -> int | None:
    """Read the snapshot the table's mark state is a verdict on, on a second session.

    Parameters
    ----------
    world
        The world.
    dotted
        The table.

    Returns
    -------
    int or None
        `Table.kept_snapshot_id`.
    """
    ref = TableRef.from_dotted("dev", dotted)
    with world.session() as session:
        table_id = resolve_table_id(session, "dev", ref.namespace.joined(), ref.name)
        assert table_id is not None
        return session.scalars(select(Table.kept_snapshot_id).where(Table.id == table_id)).one()


@pytest.mark.slow
def test_a_clear_does_not_remove_a_mark_noted_while_its_checks_ran(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The passing checks counted the data of the mark they began with, not a newer run's."""
    _kept_first_run(world)
    _deploy(world, _check("unique_id", _PASSING, on_failure="keep"))
    _start(world)
    claimed = _run_until(world, "silver.orders")
    real = task_checks_module.clear_kept_in_attempt
    other_run, other_task = uuid.uuid4(), uuid.uuid4()

    def after_another_note(session: Session, **kwargs: object) -> bool:
        session.execute(
            update(Table)
            .where(Table.name == "orders")
            .values(kept_run_id=other_run, kept_task_run_id=other_task, kept_check="x/other")
        )
        return real(session, **kwargs)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(task_checks_module, "clear_kept_in_attempt", after_another_note)

    run_claimed(_engine(world), claimed)

    assert _task(world, "silver.orders").status is TaskStatus.SUCCEEDED
    assert _note_of(world, "silver.orders") == (other_run, other_task, "x/other")
    assert _events(world, AuditEventType.TABLE_KEPT_CLEARED) == []


@pytest.mark.slow
def test_a_passing_write_records_its_verdict_when_another_run_cleared_the_mark_first(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mark was cleared (by a check that judged other data) while these checks ran."""
    _kept_first_run(world)
    _deploy(world, _check("unique_id", _PASSING, on_failure="keep"))
    _start(world)
    claimed = _run_until(world, "silver.orders")
    real = task_checks_module.clear_kept_in_attempt

    def after_another_clear(session: Session, **kwargs: object) -> bool:
        session.execute(
            update(Table)
            .where(Table.name == "orders")
            .values(
                kept_run_id=None,
                kept_task_run_id=None,
                kept_check=None,
                kept_snapshot_id=KEPT_SNAPSHOT_UNKNOWN,
            )
        )
        return real(session, **kwargs)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(task_checks_module, "clear_kept_in_attempt", after_another_clear)

    run_claimed(_engine(world), claimed)

    task = _task(world, "silver.orders")
    assert task.status is TaskStatus.SUCCEEDED
    assert _note_of(world, "silver.orders") == (None, None, None)
    assert _snapshot_verdict_of(world, "silver.orders") not in (None, KEPT_SNAPSHOT_UNKNOWN)


@pytest.mark.slow
def test_a_failing_clear_does_not_record_the_success(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, first_task = _kept_first_run(world)
    _deploy(world, _check("unique_id", _PASSING, on_failure="keep"))
    _start(world)
    claimed = _run_until(world, "silver.orders")

    def broken(*args: object, **kwargs: object) -> bool:
        raise RuntimeError("the database refused the clear")

    monkeypatch.setattr(task_checks_module, "clear_kept_in_attempt", broken)

    with pytest.raises(RuntimeError):
        run_claimed(_engine(world), claimed)

    assert _task(world, "silver.orders").status is TaskStatus.RUNNING  # nothing was recorded
    assert _note_of(world, "silver.orders") == (first, first_task, "silver.orders/unique_id")


@pytest.mark.slow
def test_clean_writes_keep_the_verdict_snapshot_of_an_unmarked_table_current(
    world: World,
) -> None:
    _kept_first_run(world)
    _deploy(world, _check("unique_id", _PASSING, on_failure="keep"))
    _start(world)
    _drain(world)  # clears the note
    cleared_at = _snapshot_verdict_of(world, "silver.orders")
    assert _note_of(world, "silver.orders") == (None, None, None) and cleared_at is not None

    _start(world)
    _drain(world)  # a clean write with no note to clear

    task = _task(world, "silver.orders")
    assert task.status is TaskStatus.SUCCEEDED
    refreshed = _snapshot_verdict_of(world, "silver.orders")
    assert refreshed == task.written_snapshot_after != cleared_at


@pytest.mark.slow
def test_a_later_block_failure_rolls_back_to_the_kept_data_and_the_note_stays(
    world: World,
) -> None:
    _checked(world, _check("unique_id", _DUPLICATE_IDS, on_failure="keep"))
    first = _start(world)
    _drain(world)
    first_task = _task(world, "silver.orders")
    kept_ids = _ids(world, "silver.orders")
    # A different build, so that the second run writes a new snapshot and then fails.
    _checked(
        world,
        _check("unique_id", _DUPLICATE_IDS),
        sql="SELECT id FROM bronze.src UNION ALL SELECT 1::BIGINT AS id",
    )
    second = _start(world)
    _drain(world)

    task = _task(world, "silver.orders")
    assert (task.run_id, task.status) == (second, TaskStatus.FAILED)
    assert [e["outcome"] for e in _events(world, AuditEventType.TABLE_ROLLED_BACK)] == [
        "rolled_back"
    ]
    assert _ids(world, "silver.orders") == kept_ids
    assert _note_of(world, "silver.orders") == (first, first_task.id, "silver.orders/unique_id")
    assert _events(world, AuditEventType.TABLE_KEPT_CLEARED) == []
    with world.session() as session:
        described = CatalogService(
            session, StorageResolver(session), AllowAllAuthorization()
        ).describe_table(world.admin, TableRef.from_dotted("dev", "silver.orders"))
    assert described.state == "kept"


@pytest.mark.slow
def test_a_block_failure_leaves_no_note(world: World) -> None:
    _checked(
        world,
        _check("a_kept", _DUPLICATE_IDS, on_failure="keep"),
        _check("b_blocking", _DUPLICATE_IDS),
    )
    _start(world)
    _drain(world)

    assert _task(world, "silver.orders").status is TaskStatus.FAILED
    assert _marker(world, "silver.orders") is not None
    assert _note_of(world, "silver.orders") == (None, None, None)
    assert _events(world, AuditEventType.TABLE_KEPT) == []


@pytest.mark.slow
def test_a_rebuild_without_checks_clears_the_note(world: World) -> None:
    _checked(world, _check("unique_id", _DUPLICATE_IDS, on_failure="keep"))
    _start(world)
    _drain(world)
    assert _note_of(world, "silver.orders")[0] is not None

    _checked(world)
    second = _start(world)
    _drain(world)

    task = _task(world, "silver.orders")
    assert (task.run_id, task.status) == (second, TaskStatus.SUCCEEDED)
    assert _note_of(world, "silver.orders") == (None, None, None)
    verdict = _snapshot_verdict_of(world, "silver.orders")
    assert verdict is not None and verdict > 0  # the snapshot of the rebuild, not "unknown"
    (event,) = _events(world, AuditEventType.TABLE_KEPT_CLEARED)
    assert event == {
        "why": "rebuilt_without_checks",
        "run_id": str(second),
        "task_run_id": str(task.id),
    }


@pytest.mark.slow
def test_a_rebuild_without_checks_does_not_clear_a_mark_another_run_noted_since(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rebuild superseded the mark it saw, not the one an overlapping run set after it."""
    _checked(world, _check("unique_id", _DUPLICATE_IDS, on_failure="keep"))
    _start(world)
    _drain(world)
    _checked(world)
    _start(world)
    claimed = _run_until(world, "silver.orders")
    real = tasks_module._clear_rebuilt
    other_run, other_task = uuid.uuid4(), uuid.uuid4()

    def after_another_note(*args: object) -> None:
        with world.session() as other:
            other.execute(
                update(Table)
                .where(Table.name == "orders")
                .values(kept_run_id=other_run, kept_task_run_id=other_task, kept_check="x/other")
            )
            other.commit()
        real(*args)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(tasks_module, "_clear_rebuilt", after_another_note)

    run_claimed(_engine(world), claimed)

    assert _note_of(world, "silver.orders") == (other_run, other_task, "x/other")
    assert _events(world, AuditEventType.TABLE_KEPT_CLEARED) == []


@pytest.mark.parametrize("checked", [False, True])
def test_a_task_without_a_note_writes_nothing_more(world: World, checked: bool) -> None:
    """A task whose table has no note pays no statement for the note: with or without checks.

    The counts are those of the same task before the note existed.
    """
    if checked:
        _checked(world, _check("no_nulls", "SELECT id FROM silver.orders WHERE id IS NULL"))
        _start(world)
        claimed = _run_until(world, "silver.orders")
    else:
        _start(world)
        claimed = _claim(world)

    assert _statements_of(world, claimed) == _STATEMENTS[checked]


@pytest.mark.slow
def test_the_next_claim_of_a_written_task_sets_the_note(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    _checked(world, _check("unique_id", _DUPLICATE_IDS, on_failure="keep"))
    real = task_checks_module.note_kept
    died = threading.Event()

    def dying(*args: object, **kwargs: object) -> KeptNote:
        if not died.is_set():
            died.set()
            raise SystemExit("the worker died before the note")
        return real(*args, **kwargs)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(task_checks_module, "note_kept", dying)
    run_id = _start(world)
    claimed = _run_until(world, "silver.orders")
    with pytest.raises(SystemExit):
        run_claimed(_engine(world), claimed)
    assert _results(world, claimed.task_run_id)
    assert _note_of(world, "silver.orders") == (None, None, None)
    _expire_lease_and_tick(world, "silver.orders")

    monkeypatch.setattr(tasks_module, "_dispatch", lambda *a, **k: pytest.fail("built again"))
    again = _claim_ready(world)
    assert again is not None and again.asset == "silver.orders"
    run_claimed(_engine(world), again)

    task = _task(world, "silver.orders")
    assert task.status is TaskStatus.FAILED
    assert _note_of(world, "silver.orders") == (run_id, task.id, "silver.orders/unique_id")
    assert len(_events(world, AuditEventType.TABLE_KEPT)) == 1


@pytest.mark.parametrize("on_failure", ["block", "keep", "warn"])
def test_a_foreign_source_check_of_a_kept_table_cannot_count_and_says_why(
    world: World, on_failure: str
) -> None:
    """Only a run of the project that builds a table reads it while it is kept.

    Another project's check of the table may be weaker than the check that failed, so its
    check task does not count the table at all. The task's message is the refusal itself:
    the table is kept, and the project that builds it clears that. A `warn` check that could
    not run does not fail the task, as any `warn` check; the reader is then refused the table.
    """
    _outside_table(world, "bronze.events", [1, 2])
    kept = _keep_note_on(world, "bronze.events")
    before = _note_of(world, "bronze.events")
    world.set_manifest(
        _entry("silver.orders", "SELECT id FROM bronze.events", references=["bronze.events"]),
        checks=[
            _check(
                "unique_id",
                "SELECT id FROM bronze.events GROUP BY id HAVING count(*) > 1",
                table="bronze.events",
                on_failure=on_failure,
            )
        ],
    )
    _start(world)
    _drain(world)

    task = _task(world, "bronze.events")
    (result,) = _results(world, task.id)
    assert (result.outcome, result.error) == (CheckOutcome.ERRORED, ERROR_KEPT)
    reader = _task(world, "silver.orders")
    if on_failure == "warn":
        assert task.status is TaskStatus.SUCCEEDED
        assert (reader.status, reader.error_message) == (
            TaskStatus.FAILED,
            _refused_text("bronze.events", kept),
        )
    else:
        assert (task.status, task.attempt) == (TaskStatus.FAILED, 1)
        assert task.error_message == _refused_text("bronze.events", kept)
        assert reader.status is TaskStatus.UPSTREAM_FAILED
    assert not _exists(world, "silver.orders")
    assert _note_of(world, "bronze.events") == before
    assert before[0] == kept
    assert _events(world, AuditEventType.TABLE_KEPT) == []
    assert _events(world, AuditEventType.TABLE_KEPT_CLEARED) == []


def test_a_source_check_with_a_block_and_a_keep_failure_names_both(world: World) -> None:
    """The task writes nothing either way, so the message must not hide the `keep` failure."""
    _outside_table(world, "bronze.events", [1, 2])
    world.set_manifest(
        _entry("silver.orders", "SELECT id FROM bronze.events", references=["bronze.events"]),
        checks=[
            _check("a_block", "SELECT id FROM bronze.events", table="bronze.events"),
            _check(
                "b_keep", "SELECT id FROM bronze.events", table="bronze.events", on_failure="keep"
            ),
        ],
    )
    _start(world)
    _drain(world)

    message = _task(world, "bronze.events").error_message
    assert message is not None
    assert message.startswith("2 checks of `bronze.events` failed:")
    assert "`a_block` (2 rows), `b_keep` (2 rows)" in message


def test_a_table_dropped_while_its_keep_check_ran_says_nothing_is_kept(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    _checked(world, _check("unique_id", _DUPLICATE_IDS, on_failure="keep"))
    real = task_checks_module.note_kept

    def after_a_drop(*args: object, **kwargs: object) -> object:
        with world.session() as session:
            session.execute(update(CheckResult).values(table_id=None))
            session.execute(delete(Table).where(Table.id == kwargs["table_id"]))
            session.commit()
        return real(*args, **kwargs)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(task_checks_module, "note_kept", after_a_drop)
    _start(world)
    _drain(world)

    task = _task(world, "silver.orders")
    assert task.status is TaskStatus.FAILED
    assert task.error_message is not None
    assert task.error_message.endswith(
        "The table was dropped while its checks ran, so nothing is kept."
    )
    assert "keeps this run's data" not in task.error_message
    assert _events(world, AuditEventType.TABLE_KEPT) == []


def test_a_keep_check_of_a_table_that_does_not_exist_fails_the_task_and_notes_nothing(
    world: World,
) -> None:
    world.set_manifest(
        _entry("silver.orders", "SELECT id FROM bronze.gone", references=["bronze.gone"]),
        checks=[
            _check("gone", "SELECT id FROM bronze.gone", table="bronze.gone", on_failure="keep")
        ],
    )
    _start(world)
    _drain(world)

    assert _task(world, "bronze.gone").status is TaskStatus.FAILED
    assert _events(world, AuditEventType.TABLE_KEPT) == []


@pytest.mark.slow
def test_the_next_rebuild_clears_a_note_that_survived_one(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    _checked(world, _check("unique_id", _DUPLICATE_IDS, on_failure="keep"))
    first = _start(world)
    _drain(world)
    _checked(world)
    real = tasks_module.clear_kept
    raised: list[bool] = []

    def once(*args: object, **kwargs: object) -> bool:
        if not raised:
            raised.append(True)
            raise RuntimeError("the database went away")
        return real(*args, **kwargs)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(tasks_module, "clear_kept", once)
    _start(world)
    _drain(world)
    assert _note_of(world, "silver.orders")[0] == first

    third = _start(world)
    _drain(world)

    task = _task(world, "silver.orders")
    assert (task.run_id, task.status) == (third, TaskStatus.SUCCEEDED)
    assert _note_of(world, "silver.orders") == (None, None, None)
    (event,) = _events(world, AuditEventType.TABLE_KEPT_CLEARED)
    assert event["why"] == "rebuilt_without_checks"


@pytest.mark.slow
def test_a_note_that_cannot_be_cleared_after_a_rebuild_does_not_fail_the_build(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    _checked(world, _check("unique_id", _DUPLICATE_IDS, on_failure="keep"))
    first = _start(world)
    _drain(world)
    _checked(world)

    def broken(*args: object, **kwargs: object) -> bool:
        raise RuntimeError("the database went away")

    monkeypatch.setattr(tasks_module, "clear_kept", broken)
    _start(world)
    _drain(world)

    assert _task(world, "silver.orders").status is TaskStatus.SUCCEEDED
    assert _note_of(world, "silver.orders")[0] == first


def test_a_note_that_cannot_be_set_does_not_succeed_the_task(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    _checked(world, _check("unique_id", _DUPLICATE_IDS, on_failure="keep"))
    monkeypatch.setattr(task_checks_module, "note_kept", lambda *a, **k: KeptNote.NOT_HELD)
    _start(world)
    claimed = _run_until(world, "silver.orders")

    run_claimed(_engine(world), claimed)

    task = _task(world, "silver.orders")
    assert task.status is TaskStatus.RUNNING  # nothing was recorded: the claim is not held
    assert [x.outcome for x in _attempts(world, task.id)] == [None]  # the claim's, still open
    assert _note_of(world, "silver.orders") == (None, None, None)


def test_a_lost_lease_the_keeper_saw_drops_the_phase_at_once(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The keeper of the checks saw the lease lost: nothing more is asked of the database."""
    _checked(world, _check("unique_id", _DUPLICATE_IDS))
    _start(world)
    claimed = _run_until(world, "silver.orders")

    def steal() -> None:
        with world.session() as session:
            session.execute(
                update(TaskRun).where(TaskRun.id == claimed.task_run_id).values(lease_owner="thief")
            )
            session.commit()
        event = executor_module._CANCEL.get()
        assert event is not None
        assert event.wait(5)

    _counting(monkeypatch, _fake_run_one(QueryCancelled("x"), steal))
    monkeypatch.setattr(
        task_checks_module, "record_result", lambda *a, **k: pytest.fail("a lost claim recorded")
    )

    run_claimed(_engine(world), claimed)

    task = _task(world, "silver.orders")
    assert (task.status, task.lease_owner) == (TaskStatus.RUNNING, "thief")
    assert _results(world, task.id) == []


def test_the_keeper_of_the_checks_is_stopped_before_the_outcome_is_recorded(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No renewal of the second keeper can race `record_attempt`, and both keepers ran."""
    _checked(world, _check("unique_id", _DUPLICATE_IDS))
    _start(world)
    claimed = _run_until(world, "silver.orders")
    alive: list[bool] = []
    started: list[LeaseKeeper] = []
    real = tasks_module.record_attempt
    real_start = LeaseKeeper.start

    def counting_start(self: LeaseKeeper) -> None:
        started.append(self)
        real_start(self)

    def spying(*args: object, **kwargs: object) -> bool:
        alive.append(any(t.name == "lakana-lease" for t in threading.enumerate()))
        return real(*args, **kwargs)  # ty: ignore[invalid-argument-type]

    during: list[bool] = []
    real_count = checks_module.run_one

    def counted(*args: object, **kwargs: object) -> BaseException | None:
        during.append(any(t.name == "lakana-lease" for t in threading.enumerate()))
        return real_count(*args, **kwargs)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(LeaseKeeper, "start", counting_start)
    monkeypatch.setattr(tasks_module, "record_attempt", spying)
    monkeypatch.setattr(checks_module, "run_one", counted)

    run_claimed(_engine(world), claimed)

    assert alive == [False]
    assert during == [True]
    assert len(started) == 2
    assert not any(t.name == "lakana-lease" for t in threading.enumerate())


def test_a_refused_rollback_that_cannot_be_noted_because_the_claim_went_records_nothing(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The claim was held when the refusal was judged and gone when it was to be noted."""
    _checked(world, _check("unique_id", _DUPLICATE_IDS))
    _start(world)
    claimed = _run_until(world, "silver.orders")

    def refuse(self: CatalogService, *args: object, **kwargs: object) -> Rollback:
        raise PermissionDenied("x")

    monkeypatch.setattr(CatalogService, "roll_back", refuse)
    monkeypatch.setattr(task_checks_module, "mark_rollback_failed", lambda *a, **k: False)

    run_claimed(_engine(world), claimed)

    task = _task(world, "silver.orders")
    assert (task.status, task.error_message) == (TaskStatus.RUNNING, None)
    assert _attempts(world, task.id)[0].finished_at is None


# -- the check task of a later run for a kept table ----------------------------------

_LATER = Selection(patterns=("gold.report",))


def _deploy(
    world: World,
    *checks: dict[str, object],
    sql: str = "SELECT id FROM bronze.src",
    **extra: object,
) -> None:
    """Deploy `_checked`'s world, with the `references` of a manifest that a deploy wrote.

    Parameters
    ----------
    world
        The world.
    *checks
        The check entries, of `silver.orders` unless they say otherwise.
    sql
        The SELECT of `silver.orders`.
    **extra
        Overrides of the entry of `silver.orders`.
    """
    world.set_manifest(
        _entry("bronze.src", "SELECT (i % 2 + 1)::BIGINT AS id FROM range(3) AS r(i)"),
        _entry(
            "silver.orders",
            sql,
            **{"retry_delay_seconds": 0, "references": ["bronze.src"], **extra},
        ),
        _entry("gold.report", "SELECT id FROM silver.orders", references=["silver.orders"]),
        edges=[["bronze.src", "silver.orders"], ["silver.orders", "gold.report"]],
        checks=list(checks),
    )


def _kept_first_run(world: World) -> tuple[uuid.UUID, uuid.UUID]:
    """Run once with a failing `keep` check, so that `silver.orders` carries the note.

    Parameters
    ----------
    world
        The world.

    Returns
    -------
    tuple
        The run, and the task run of `silver.orders` that kept the data.
    """
    _deploy(world, _check("unique_id", _DUPLICATE_IDS, on_failure="keep"))
    run_id = _start(world)
    _drain(world)
    task = _task(world, "silver.orders")
    assert _note_of(world, "silver.orders") == (run_id, task.id, "silver.orders/unique_id")
    return run_id, task.id


def _kept_message(first: uuid.UUID) -> str:
    """Build the sentence that closes the message of a kept table that fails again (K3).

    Parameters
    ----------
    first
        The run that kept the data.

    Returns
    -------
    str
        The sentence.
    """
    return (
        f"`silver.orders` was kept after a failed check in run {first} and still fails, so the "
        "assets that read it were not built. Build it again "
        "(`lakana run --select silver.orders --env dev`), or repair the data and run again."
    )


@pytest.mark.slow
def test_the_check_task_of_a_later_run_clears_the_note_when_it_passes(world: World) -> None:
    _kept_first_run(world)
    _deploy(world, _check("unique_id", _PASSING, on_failure="keep"))
    second = _start(world, _LATER)
    _drain(world)

    task = _task(world, "silver.orders")
    assert (task.run_id, task.kind, task.status) == (second, "source_check", TaskStatus.SUCCEEDED)
    assert _note_of(world, "silver.orders") == (None, None, None)
    (event,) = _events(world, AuditEventType.TABLE_KEPT_CLEARED)
    assert event == {"why": "checks_passed", "run_id": str(second), "task_run_id": str(task.id)}
    assert _task(world, "gold.report").status is TaskStatus.SUCCEEDED


@pytest.mark.parametrize("on_failure", ["keep", "block"])
def test_the_check_task_of_a_later_run_fails_and_keeps_the_note(
    world: World, on_failure: str
) -> None:
    first, first_task = _kept_first_run(world)
    ids = _ids(world, "silver.orders")
    _deploy(world, _check("unique_id", _DUPLICATE_IDS, on_failure=on_failure))
    second = _start(world, _LATER)
    _drain(world)

    task = _task(world, "silver.orders")
    assert (task.run_id, task.kind, task.status) == (second, "source_check", TaskStatus.FAILED)
    assert task.error_message is not None
    assert task.error_message.startswith(
        "Check `silver.orders/unique_id` failed: 1 row (checks/silver/orders/unique_id.sql). "
    )
    assert task.error_message.endswith(_kept_message(first))
    assert _note_of(world, "silver.orders") == (first, first_task, "silver.orders/unique_id")
    assert _ids(world, "silver.orders") == ids
    assert _events(world, AuditEventType.TABLE_ROLLED_BACK) == []
    assert [e["task_run_id"] for e in _events(world, AuditEventType.TABLE_KEPT)] == [
        str(first_task)
    ]
    assert _events(world, AuditEventType.TABLE_KEPT_CLEARED) == []
    (result,) = _results(world, task.id)
    assert result.rollback_outcome is None
    assert _task(world, "gold.report").status is TaskStatus.UPSTREAM_FAILED
    assert not _exists(world, "gold.report")


@pytest.mark.slow
def test_a_kept_table_without_checks_stops_its_readers_until_it_is_rebuilt(world: World) -> None:
    first, first_task = _kept_first_run(world)
    _deploy(world)
    second = _start(world, _LATER)
    _drain(world)

    task = _task(world, "silver.orders")
    assert (task.run_id, task.kind, task.status) == (second, "source_check", TaskStatus.FAILED)
    # The project removed the check: nothing was "created with checks", and no deploy helps.
    no_checks = (
        f"`silver.orders` was kept after a failed check in run {first}, and the project has no "
        "check for it any more, so nothing can pass. Only building it again clears this: "
        "`lakana run --select silver.orders --env dev`."
    )
    assert task.error_message == no_checks
    (result,) = _results(world, task.id)
    assert (result.check_name, result.outcome, result.on_failure, result.error) == (
        "silver.orders/(none)",
        CheckOutcome.ERRORED,
        "block",
        no_checks,
    )
    assert _note_of(world, "silver.orders") == (first, first_task, "silver.orders/unique_id")
    assert _task(world, "gold.report").status is TaskStatus.UPSTREAM_FAILED

    third = _start(world)  # a rebuild clears the note: nothing is left that could fail
    _drain(world)
    assert _task(world, "silver.orders").run_id == third
    assert _note_of(world, "silver.orders") == (None, None, None)
    assert _task(world, "gold.report").status is TaskStatus.SUCCEEDED


def test_the_note_that_went_away_while_the_task_ran_is_not_claimed_in_its_message(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, _ = _kept_first_run(world)
    _deploy(world, _check("unique_id", _DUPLICATE_IDS, on_failure="keep"))
    second = _start(world, _LATER)
    claimed = _run_until(world, "silver.orders")
    real = task_checks_module.kept_run_of

    def gone(session: Session, table_id: uuid.UUID) -> uuid.UUID | None:
        with world.session() as other:  # another project's rebuild cleared it meanwhile
            other.execute(update(Table).values(kept_run_id=None, kept_task_run_id=None))
            other.commit()
        return real(session, table_id)

    monkeypatch.setattr(task_checks_module, "kept_run_of", gone)
    run_claimed(_engine(world), claimed)

    task = _task(world, "silver.orders")
    assert (task.run_id, task.status) == (second, TaskStatus.FAILED)
    assert task.error_message is not None
    assert "was kept" not in task.error_message and "does not build" not in task.error_message
    assert task.error_message.endswith(_after(second))
    assert first != second


def test_the_check_task_of_a_kept_python_assets_table_never_starts_a_process(
    world: World,
) -> None:
    class Never:
        """A Python runtime that must not be reached."""

        def __init__(self) -> None:
            self.calls = 0

        def attempt(self, *_args: object, **_kwargs: object) -> None:
            """Count a call.

            Parameters
            ----------
            *_args
                Ignored.
            **_kwargs
                Ignored.
            """
            self.calls += 1

    first, first_task = _kept_first_run(world)
    _deploy(world, _check("unique_id", _DUPLICATE_IDS, on_failure="keep"), kind="python")
    second = _start(world, _LATER)
    engine = _engine(world)
    never = Never()
    engine.python = never
    _drain(world, engine)

    task = _task(world, "silver.orders")
    assert (task.run_id, task.kind, task.status) == (second, "source_check", TaskStatus.FAILED)
    assert never.calls == 0
    assert task.error_message is not None and task.error_message.endswith(_kept_message(first))
    assert _note_of(world, "silver.orders") == (first, first_task, "silver.orders/unique_id")


@pytest.mark.slow
def test_the_older_server_refusal_is_unchanged_by_a_check_task_of_a_kept_table(
    world: World,
) -> None:
    """A run of this code with the check task is not refused; an older server's still is."""
    _kept_first_run(world)
    _deploy(world, _check("unique_id", _PASSING, on_failure="keep"))
    _start(world, _LATER)  # this code: the check task of the kept table is there
    _drain(world)
    built = _task(world, "gold.report")
    assert (built.status, built.error_message) == (TaskStatus.SUCCEEDED, None)

    # The refusal fires where it fired before: a checked table without its task.
    _deploy(world, _check("unique_id", _DUPLICATE_IDS))
    _started_by_an_older_server(world)
    _drain(world)
    refused = _task(world, "silver.orders")
    assert refused.status is TaskStatus.FAILED
    assert refused.error_message == _refused("`silver.orders`")


def _clear_note_on(world: World, dotted: str) -> None:
    """Remove a table's note, as a rebuild by another run would have.

    Parameters
    ----------
    world
        The world.
    dotted
        The table.
    """
    ref = TableRef.from_dotted("dev", dotted)
    with world.session() as session:
        session.execute(
            update(Table)
            .where(Table.id == resolve_table_id(session, "dev", ref.namespace.joined(), ref.name))
            .values(kept_run_id=None, kept_task_run_id=None, kept_check=None)
        )
        session.commit()


def test_a_check_task_whose_note_is_gone_before_it_starts_fails_without_the_kept_sentence(
    world: World,
) -> None:
    _kept_first_run(world)
    _deploy(world, _check("unique_id", _DUPLICATE_IDS, on_failure="keep"))
    second = _start(world, _LATER)
    _clear_note_on(world, "silver.orders")
    _drain(world)

    task = _task(world, "silver.orders")
    assert (task.run_id, task.status) == (second, TaskStatus.FAILED)
    assert task.error_message == (
        "Check `silver.orders/unique_id` failed: 1 row (checks/silver/orders/unique_id.sql). "
        f"{_unique_rows(second)}{_after(second)}"
    )
    assert _note_of(world, "silver.orders") == (None, None, None)


@pytest.mark.slow
def test_a_check_task_whose_note_is_gone_before_it_starts_clears_and_audits_nothing(
    world: World,
) -> None:
    _kept_first_run(world)
    _deploy(world, _check("unique_id", _PASSING, on_failure="keep"))
    _start(world, _LATER)
    _clear_note_on(world, "silver.orders")
    _drain(world)

    assert _task(world, "silver.orders").status is TaskStatus.SUCCEEDED
    assert _events(world, AuditEventType.TABLE_KEPT_CLEARED) == []


def test_a_refusal_of_a_kept_table_is_a_permanent_failure() -> None:
    assert classify(KeptAfterCheckError("kept")) is FailureKind.PERMANENT


def test_a_sql_task_of_another_run_fails_with_the_kept_text(world: World) -> None:
    """The table was kept after the run started, so the run has no check task for it.

    Permanent: attempt 1 of 3, and the task's message is the refusal itself, not the opaque
    "Resolving the tables this query reads failed".
    """
    _outside_table(world, "bronze.events", [1, 2])
    world.set_manifest(
        _entry("silver.orders", "SELECT id FROM bronze.events", references=["bronze.events"])
    )
    run_id = _start(world)
    kept = _keep_note_on(world, "bronze.events")

    run_claimed(_engine(world), _claim(world))

    task = _task(world, "silver.orders")
    assert (task.run_id, task.status, task.attempt, task.max_attempts) == (
        run_id,
        TaskStatus.FAILED,
        1,
        3,
    )
    assert task.error_message == _refused_text("bronze.events", kept)
    assert not _exists(world, "silver.orders")
    with world.session() as session:
        write = session.get(QueryRun, task.query_run_id)
        assert write is not None
        assert (write.status, write.error_cause) == (QueryRunStatus.FAILED, "not_found")
        assert write.error_message == _refused_text("bronze.events", kept)


def test_a_kept_table_behind_a_view_is_refused_too(world: World) -> None:
    """A view is expanded as its reader, so the table under it is loaded as the task."""
    _outside_table(world, "bronze.events", [1, 2])
    with world.session() as session:
        catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
        catalog.create_or_replace_view(
            world.admin,
            TableRef.from_dotted("dev", "bronze.recent"),
            sql="SELECT id FROM bronze.events",
            references=catalog.resolve_lineage_sources(world.admin, "dev", ["bronze.events"]),
            columns=[ColumnSpec(name="id", type="long", nullable=True)],
        )
    world.set_manifest(
        _entry("silver.orders", "SELECT id FROM bronze.recent", references=["bronze.recent"])
    )
    _start(world)
    kept = _keep_note_on(world, "bronze.events")

    run_claimed(_engine(world), _claim(world))

    task = _task(world, "silver.orders")
    assert (task.status, task.attempt) == (TaskStatus.FAILED, 1)
    assert task.error_message == _refused_text("bronze.events", kept)
    assert not _exists(world, "silver.orders")


@pytest.mark.slow
def test_a_check_that_reads_a_kept_table_of_another_run_is_recorded_errored(world: World) -> None:
    """The count of a check runs as its task, and that task has no standing either."""
    _outside_table(world, "bronze.events", [1, 2])
    kept = _keep_note_on(world, "bronze.events")
    _checked(
        world,
        _check(
            "known_key",
            "SELECT o.id FROM silver.orders o LEFT JOIN bronze.events e ON e.id = o.id "
            "WHERE e.id IS NULL",
        ),
    )
    run_id = _start(world)
    _drain(world)

    task = _task(world, "silver.orders")
    (result,) = _results(world, task.id)
    assert (result.outcome, result.error) == (CheckOutcome.ERRORED, ERROR_KEPT)
    assert ERROR_KEPT == (
        "a table this check reads was kept after its checks failed or could not run, or was set "
        "back to data whose checks are not known, and has not passed its checks since, so the "
        "check could not read it. `lakana table get` on each table the check reads shows which "
        "one, and why."
    )
    assert task.status is TaskStatus.FAILED
    assert task.error_message is not None and task.error_message.startswith(
        "Check `silver.orders/known_key` could not run (checks/silver/orders/known_key.sql): "
        f"{ERROR_KEPT} "
    )
    # Whoever may read this run reads the check's `error` and the log; neither names the
    # kept table, the check that failed there or its run.
    log = _log(world, run_id, "silver.orders")
    for text in (result.error or "", task.error_message, log):
        assert "bronze" not in text and str(kept) not in text and "/old" not in text
    assert _task(world, "gold.report").status is TaskStatus.UPSTREAM_FAILED
    with world.session() as session:
        count = session.get(QueryRun, result.query_run_id)
        assert count is not None
        assert count.error_message == _refused_text("bronze.events", kept)


_READS_EVENTS = (
    "SELECT o.id FROM silver.orders o LEFT JOIN bronze.events e ON e.id = o.id WHERE e.id IS NULL"
)
"""A check of `silver.orders` that reads `bronze.events`, a table the project does not build."""


def test_a_keep_check_refused_a_kept_table_sets_no_note(world: World) -> None:
    """A `keep` check that could not run because a table it reads is kept keeps nothing.

    Otherwise the note would spread from table to table through checks. The task still fails,
    and what reads its table is not built.
    """
    _outside_table(world, "bronze.events", [1, 2])
    _keep_note_on(world, "bronze.events")
    _deploy(world, _check("known_key", _READS_EVENTS, on_failure="keep"))
    run_id = _start(world)
    _drain(world)

    task = _task(world, "silver.orders")
    (result,) = _results(world, task.id)
    assert (result.outcome, result.on_failure, result.error) == (
        CheckOutcome.ERRORED,
        "keep",
        ERROR_KEPT,
    )
    assert (task.status, task.attempt) == (TaskStatus.FAILED, 1)
    # No note is set, so the message must not say that the table keeps the run's data.
    assert task.error_message == (
        "Check `silver.orders/known_key` could not run (checks/silver/orders/known_key.sql): "
        "a table this check reads was kept after its checks failed or could not run, or was set "
        "back to data whose checks are not known, and has not passed its checks since, so the "
        "check could not read it. `lakana table get` on each table the check reads shows which "
        "one, and why. "
        "The checks of `silver.orders` could not run, so nothing that reads it was built. "
        "After the kept table that a check reads is cleared, run again; if the project did not "
        "change: "
        f"`lakana runs resume {run_id} --env dev`."
    )
    assert _note_of(world, "silver.orders") == (None, None, None)
    assert _events(world, AuditEventType.TABLE_KEPT) == []
    assert _task(world, "gold.report").status is TaskStatus.UPSTREAM_FAILED
    assert _ids(world, "silver.orders") == [1, 1, 2]


@pytest.mark.slow
def test_a_kept_table_rebuilt_while_its_keep_check_cannot_run_moves_the_mark(
    world: World,
) -> None:
    """The new data is unchecked; the mark names the new run and the check as not run."""
    _kept_first_run(world)
    _outside_table(world, "bronze.events", [1, 2])
    _keep_note_on(world, "bronze.events")
    _deploy(world, _check("known_key", _READS_EVENTS, on_failure="keep"))
    second = _start(world)
    _drain(world)

    task = _task(world, "silver.orders")
    assert task.status is TaskStatus.FAILED
    assert _note_of(world, "silver.orders") == (second, task.id, "silver.orders/known_key")
    ref = TableRef.from_dotted("dev", "silver.orders")
    with world.session() as session:
        row = session.get(Table, resolve_table_id(session, "dev", ref.namespace.joined(), ref.name))
        assert row is not None
        assert row.kept_unchecked is True
        assert kept_message(row) == (
            f"Kept after a check that could not run (`silver.orders/known_key`, run {second}): "
            "the table holds data no check could run on. "
            "Tasks do not read it until its checks pass."
        )
    (*_, event) = _events(world, AuditEventType.TABLE_KEPT)
    assert event["checks"] == ["silver.orders/known_key"]
    assert event["unchecked"] is True
    assert _task(world, "gold.report").status is TaskStatus.UPSTREAM_FAILED


def test_a_keep_check_that_really_failed_is_noted_without_the_refused_one(world: World) -> None:
    """The note names the checks that failed, not the one that was refused a table."""
    _outside_table(world, "bronze.events", [1, 2])
    _keep_note_on(world, "bronze.events")
    _deploy(
        world,
        _check("known_key", _READS_EVENTS, on_failure="keep"),
        _check("unique_id", _DUPLICATE_IDS, on_failure="keep"),
    )
    run_id = _start(world)
    _drain(world)

    task = _task(world, "silver.orders")
    assert {result.check_name: result.outcome for result in _results(world, task.id)} == {
        "silver.orders/known_key": CheckOutcome.ERRORED,
        "silver.orders/unique_id": CheckOutcome.FAILED,
    }
    assert _note_of(world, "silver.orders") == (run_id, task.id, "silver.orders/unique_id")
    (event,) = _events(world, AuditEventType.TABLE_KEPT)
    assert event["checks"] == ["silver.orders/unique_id"]
    # The table keeps the data because of the check that failed; the refused one is named
    # after it, as a check that could not run.
    assert task.error_message == (
        "Check `silver.orders/unique_id` failed: 1 row (checks/silver/orders/unique_id.sql). "
        "Check `silver.orders/known_key` could not run (checks/silver/orders/known_key.sql): "
        f"{ERROR_KEPT} {_unique_rows(run_id)}"
        "The table keeps this run's data (`on_failure: keep`), and nothing that reads it was "
        "built. Repair the data or the asset; after a fix, deploy and run again; if the project "
        f"did not change: `lakana runs resume {run_id} --env dev`."
    )
    assert _task(world, "gold.report").status is TaskStatus.UPSTREAM_FAILED
    assert not _exists(world, "gold.report")


def test_two_keep_checks_refused_a_kept_table_are_listed_and_keep_nothing(world: World) -> None:
    _outside_table(world, "bronze.events", [1, 2])
    _keep_note_on(world, "bronze.events")
    _deploy(
        world,
        _check("known_key", _READS_EVENTS, on_failure="keep"),
        _check("other_key", _READS_EVENTS, on_failure="keep"),
    )
    run_id = _start(world)
    _drain(world)

    task = _task(world, "silver.orders")
    message = task.error_message
    assert message is not None
    assert "2 checks" not in message
    assert message.count("could not run (") == 2
    assert "`silver.orders/known_key` could not run" in message
    assert "`silver.orders/other_key` could not run" in message
    assert message.endswith(
        "The checks of `silver.orders` could not run, so nothing that reads it was built. "
        "After the kept table that a check reads is cleared, run again; if the project did not "
        "change: "
        f"`lakana runs resume {run_id} --env dev`."
    )
    assert _note_of(world, "silver.orders") == (None, None, None)
    assert _task(world, "gold.report").status is TaskStatus.UPSTREAM_FAILED


@pytest.mark.parametrize("on_failure", ["block", "keep"])
def test_the_check_task_of_a_kept_table_refused_another_kept_table_says_it_could_not_run(
    world: World, on_failure: str
) -> None:
    """The project's own kept table is checked again, and its check reads another project's.

    Nothing "still fails": the check could not run, and the message says that.
    """
    first, first_task = _kept_first_run(world)
    _outside_table(world, "bronze.events", [1, 2])
    _keep_note_on(world, "bronze.events")
    _deploy(world, _check("known_key", _READS_EVENTS, on_failure=on_failure))
    second = _start(world, _LATER)
    _drain(world)

    task = _task(world, "silver.orders")
    assert (task.run_id, task.kind, task.status) == (second, "source_check", TaskStatus.FAILED)
    (result,) = _results(world, task.id)
    assert (result.outcome, result.error) == (CheckOutcome.ERRORED, ERROR_KEPT)
    assert task.error_message == (
        "Check `silver.orders/known_key` could not run (checks/silver/orders/known_key.sql): "
        f"{ERROR_KEPT} "
        "The checks of `silver.orders` could not run, so nothing that reads it was built. "
        "After the kept table that a check reads is cleared, run again; if the project did not "
        "change: "
        f"`lakana runs resume {second} --env dev`."
    )
    assert _note_of(world, "silver.orders") == (first, first_task, "silver.orders/unique_id")
    assert _task(world, "gold.report").status is TaskStatus.UPSTREAM_FAILED


@pytest.mark.parametrize("on_failure", ["block", "keep"])
def test_the_check_task_of_a_kept_table_names_its_real_failure_before_the_refused_check(
    world: World, on_failure: str
) -> None:
    first, first_task = _kept_first_run(world)
    _outside_table(world, "bronze.events", [1, 2])
    _keep_note_on(world, "bronze.events")
    _deploy(
        world,
        _check("known_key", _READS_EVENTS, on_failure=on_failure),
        _check("unique_id", _DUPLICATE_IDS, on_failure=on_failure),
    )
    second = _start(world, _LATER)
    _drain(world)

    task = _task(world, "silver.orders")
    assert (task.run_id, task.kind, task.status) == (second, "source_check", TaskStatus.FAILED)
    assert task.error_message == (
        "Check `silver.orders/unique_id` failed: 1 row (checks/silver/orders/unique_id.sql). "
        "Check `silver.orders/known_key` could not run (checks/silver/orders/known_key.sql): "
        f"{ERROR_KEPT} {_unique_rows(second)}{_kept_message(first)}"
    )
    assert _note_of(world, "silver.orders") == (first, first_task, "silver.orders/unique_id")
    assert _task(world, "gold.report").status is TaskStatus.UPSTREAM_FAILED


def test_a_refused_block_check_and_a_real_keep_failure_of_a_kept_table_name_both(
    world: World,
) -> None:
    """The refused `block` check could not run; the `keep` check really fails: say both."""
    first, first_task = _kept_first_run(world)
    _outside_table(world, "bronze.events", [1, 2])
    _keep_note_on(world, "bronze.events")
    _deploy(
        world,
        _check("known_key", _READS_EVENTS, on_failure="block"),
        _check("unique_id", _DUPLICATE_IDS, on_failure="keep"),
    )
    second = _start(world, _LATER)
    _drain(world)

    task = _task(world, "silver.orders")
    assert (task.run_id, task.kind, task.status) == (second, "source_check", TaskStatus.FAILED)
    assert task.error_message == (
        "Check `silver.orders/unique_id` failed: 1 row (checks/silver/orders/unique_id.sql). "
        "Check `silver.orders/known_key` could not run (checks/silver/orders/known_key.sql): "
        f"{ERROR_KEPT} {_unique_rows(second)}{_kept_message(first)}"
    )
    assert _note_of(world, "silver.orders") == (first, first_task, "silver.orders/unique_id")
    assert _task(world, "gold.report").status is TaskStatus.UPSTREAM_FAILED


def test_a_keep_check_that_errored_for_another_reason_still_sets_the_note(world: World) -> None:
    """Only the refusal of a kept table is set apart: data that could not be checked is kept."""
    _deploy(world, _check("known_key", "SELECT nope FROM silver.orders", on_failure="keep"))
    run_id = _start(world)
    _drain(world)

    task = _task(world, "silver.orders")
    (result,) = _results(world, task.id)
    assert result.outcome is CheckOutcome.ERRORED
    assert _note_of(world, "silver.orders") == (run_id, task.id, "silver.orders/known_key")


def _each_reads_the_other(world: World, *, checked: bool) -> None:
    """Deploy `silver.a` and `silver.b`, each with a `keep` check that reads the other.

    There is no edge between the two assets.

    Parameters
    ----------
    world
        The world.
    checked
        Whether the manifest has the two checks (both pass: every id has its match).
    """
    checks = [
        _check(
            "matched",
            f"SELECT x.id FROM silver.{own} x LEFT JOIN silver.{other} y ON y.id = x.id "
            "WHERE y.id IS NULL",
            table=f"silver.{own}",
            on_failure="keep",
        )
        for own, other in (("a", "b"), ("b", "a"))
    ]
    world.set_manifest(
        _entry("silver.a", "SELECT 1::BIGINT AS id"),
        _entry("silver.b", "SELECT 1::BIGINT AS id"),
        checks=checks if checked else None,
    )


@pytest.mark.slow
def test_two_kept_tables_whose_checks_read_each_other_are_cleared_by_one_run(world: World) -> None:
    """The count of a check reads a kept table of its own project.

    Both tables are kept. Without this, the check phase of each rebuild would be refused the
    other table, whichever is built first, and no run could ever clear either note.
    """
    _each_reads_the_other(world, checked=False)
    _start(world)
    _drain(world)
    _each_reads_the_other(world, checked=True)
    for table in ("silver.a", "silver.b"):
        _keep_note_on(world, table)

    run_id = _start(world)
    _drain(world)

    for table in ("silver.a", "silver.b"):
        task = _task(world, table)
        assert (task.run_id, task.status, task.error_message) == (
            run_id,
            TaskStatus.SUCCEEDED,
            None,
        )
        (result,) = _results(world, task.id)
        assert (result.outcome, result.failing_rows) == (CheckOutcome.PASSED, 0)
        assert _note_of(world, table) == (None, None, None)
    assert [event["why"] for event in _events(world, AuditEventType.TABLE_KEPT_CLEARED)] == [
        "checks_passed",
        "checks_passed",
    ]


@pytest.mark.slow
def test_a_reader_of_a_kept_table_is_built_after_its_check_passed(world: World) -> None:
    """The whole path: `RunService` orders the reader after the check task, and it reads."""
    _kept_first_run(world)
    assert not _exists(world, "gold.report")
    _deploy(
        world,
        _check("unique_id", "SELECT id FROM silver.orders WHERE id > 2", on_failure="keep"),
    )

    second = _start(world, _LATER)

    check, reader = _task(world, "silver.orders"), _task(world, "gold.report")
    assert (check.run_id, check.kind, check.upstream_json) == (second, "source_check", [])
    assert (reader.run_id, reader.kind, reader.upstream_json) == (
        second,
        "asset",
        ["silver.orders"],
    )
    claimed = _claim(world)
    assert (claimed.asset, claimed.kind) == ("silver.orders", "source_check")
    assert _claim_ready(world) is None  # the reader waits for the check
    run_claimed(_engine(world), claimed)
    _drain(world)

    assert _task(world, "silver.orders").status is TaskStatus.SUCCEEDED
    assert _note_of(world, "silver.orders") == (None, None, None)
    assert _task(world, "gold.report").status is TaskStatus.SUCCEEDED
    assert _ids(world, "gold.report") == _ids(world, "silver.orders") == [1, 1, 2]


def test_a_reader_of_a_kept_table_is_not_built_when_its_check_fails_again(world: World) -> None:
    first, _ = _kept_first_run(world)

    second = _start(world, _LATER)
    _drain(world)

    check, reader = _task(world, "silver.orders"), _task(world, "gold.report")
    assert (check.run_id, check.kind, check.status) == (second, "source_check", TaskStatus.FAILED)
    assert (reader.run_id, reader.status, reader.query_run_id) == (
        second,
        TaskStatus.UPSTREAM_FAILED,
        None,
    )
    assert not _exists(world, "gold.report")
    assert _note_of(world, "silver.orders")[0] == first


@pytest.mark.slow
def test_a_reader_below_the_check_task_reads_a_table_that_still_carries_the_note(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exemption (b) on the upstreams `RunService` wrote, not on hand-built ones.

    The check passed and the note was not cleared (here: the clearing is taken away). The
    reader is downstream of the table's task in its run, so it reads.
    """
    first, _ = _kept_first_run(world)
    _deploy(world, _check("unique_id", _PASSING, on_failure="keep"))
    monkeypatch.setattr(task_checks_module, "clear_kept_in_attempt", lambda *args, **kwargs: False)

    _start(world, _LATER)
    _drain(world)

    assert _task(world, "silver.orders").status is TaskStatus.SUCCEEDED
    assert _note_of(world, "silver.orders")[0] == first
    reader = _task(world, "gold.report")
    assert (reader.status, reader.error_message) == (TaskStatus.SUCCEEDED, None)
    assert _ids(world, "gold.report") == [1, 1, 2]
