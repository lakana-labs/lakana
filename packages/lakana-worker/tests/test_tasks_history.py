"""A SQL asset with `write: history`, run by the worker from its first run to a set-back."""

from __future__ import annotations

import uuid

import pytest
from check_run_helpers import _append_ids, _check, _drain, _ids, _outside_table, _writes
from sqlalchemy import update
from task_run_helpers import World, _engine, _entry, _exists, _start, _task

from lakana_core.auth import AllowAllAuthorization
from lakana_core.catalog.history import RUN_WROTE_MESSAGE
from lakana_core.config import DEFAULT_MAX_HISTORY_OPEN_VERSIONS, QueryLimitsConfig
from lakana_core.models import (
    Environment,
    QueryRun,
    QueryRunStatus,
    Run,
    RunStatus,
    TaskRun,
    TaskStatus,
    WriteMode,
)
from lakana_worker import pipeline as pipeline_module
from lakana_worker.pipeline import install_history_cap, run_one


def _keeping(world: World, **extra: object) -> None:
    """Deploy `gold.dim_events` as a history asset on `id`, as a deploy writes it.

    Parameters
    ----------
    world
        The world.
    **extra
        Overrides of the manifest entry.
    """
    entry = {
        "references": ["bronze.events"],
        "materialize": "table",
        "write_mode": "history",
        "first_write_mode": "create",
        "incremental_key": None,
        "write": "history",
        "key": ["id"],
        "watermark": None,
        "history": {"valid_from": None, "close_missing": False},
        "retry_delay_seconds": 0,
        **extra,
    }
    world.set_manifest(_entry("gold.dim_events", "SELECT id FROM bronze.events", **entry))


def test_a_history_asset_creates_its_table_then_opens_versions_for_new_keys(world: World) -> None:
    _outside_table(world, "bronze.events", [1, 2, 3])
    _keeping(world)

    _start(world)
    _drain(world)

    first = _task(world, "gold.dim_events")
    assert first.status is TaskStatus.SUCCEEDED, first.error_message
    (write,) = _writes(world, first.id)
    assert (write.write_mode, write.history, write.rows_written) == (
        WriteMode.CREATE,
        {"key": ["id"], "valid_from": None, "close_missing": False},
        3,
    )
    _append_ids(world, "bronze.events", [4])

    _start(world)
    _drain(world)

    second = _task(world, "gold.dim_events")
    assert second.status is TaskStatus.SUCCEEDED, second.error_message
    (write,) = _writes(world, second.id)
    assert (write.write_mode, write.rows_written) == (WriteMode.HISTORY, 1)
    assert write.snapshot_id is not None
    assert _ids(world, "gold.dim_events") == [1, 2, 3, 4]


def test_a_failed_blocking_check_sets_a_history_write_back(world: World) -> None:
    _outside_table(world, "bronze.events", [1, 2])
    _keeping(world, retries=0)
    _start(world)
    _drain(world)
    assert _task(world, "gold.dim_events").status is TaskStatus.SUCCEEDED
    _append_ids(world, "bronze.events", [3])
    world.set_manifest(
        _entry(
            "gold.dim_events",
            "SELECT id FROM bronze.events",
            references=["bronze.events"],
            materialize="table",
            write_mode="history",
            first_write_mode="create",
            incremental_key=None,
            write="history",
            key=["id"],
            watermark=None,
            history={"valid_from": None, "close_missing": False},
            retry_delay_seconds=0,
            retries=0,
        ),
        checks=[
            _check(
                "no_threes", "SELECT id FROM gold.dim_events WHERE id = 3", table="gold.dim_events"
            )
        ],
    )

    _start(world)
    _drain(world)

    assert _task(world, "gold.dim_events").status is TaskStatus.FAILED
    assert _ids(world, "gold.dim_events") == [1, 2]


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (
            {"history": None},
            "the project version keeps the history of gold.dim_events without a readable key",
        ),
        (
            {"key": []},
            "the project version keeps the history of gold.dim_events without a readable key",
        ),
        (
            {"first_write_mode": "replace"},
            "the project version's write mode 'replace' for gold.dim_events is not valid",
        ),
    ],
    ids=["no-history", "no-key", "history-written-as-replace"],
)
def test_a_history_entry_that_cannot_be_read_fails_without_writing(
    world: World, extra: dict[str, object], message: str
) -> None:
    _outside_table(world, "bronze.events", [1, 2])
    _keeping(world, retries=0, **extra)

    _start(world)
    _drain(world)

    task = _task(world, "gold.dim_events")
    assert task.status is TaskStatus.FAILED
    assert task.error_message is not None and task.error_message.startswith(message)
    assert not _exists(world, "gold.dim_events")


def test_a_history_result_over_max_write_rows_writes_nothing(world: World) -> None:
    _outside_table(world, "bronze.events", [1, 2, 3])
    _keeping(world, retries=0)

    _start(world)
    _drain(world, _engine(world, limits=QueryLimitsConfig(max_write_rows=2)))

    task = _task(world, "gold.dim_events")
    assert task.status is TaskStatus.FAILED
    assert task.error_message is not None
    assert task.error_message.startswith(
        "The write exceeds max_write_rows (2); nothing was committed."
    )
    assert not _exists(world, "gold.dim_events")


def test_the_configured_open_versions_cap_reaches_the_history_write(world: World) -> None:
    """`LAKANA_RUN__MAX_HISTORY_OPEN_VERSIONS`, installed at start, caps the run's write."""
    _outside_table(world, "bronze.events", [1, 2, 3])
    _keeping(world, retries=0)
    install_history_cap(2)

    _start(world)
    _drain(world)

    refused = _task(world, "gold.dim_events")
    assert refused.status is TaskStatus.FAILED
    assert refused.error_message is not None
    assert refused.error_message.startswith(
        "gold.dim_events would start with 3 open versions, more than "
        "LAKANA_RUN__MAX_HISTORY_OPEN_VERSIONS (2) allows."
    )
    install_history_cap(3)

    _start(world)
    _drain(world)

    assert _task(world, "gold.dim_events").status is TaskStatus.SUCCEEDED
    assert _ids(world, "gold.dim_events") == [1, 2, 3]


def test_the_configured_open_bytes_cap_reaches_the_history_write(world: World) -> None:
    """`LAKANA_RUN__MAX_HISTORY_OPEN_BYTES`, installed at start, caps what the run reads."""
    _outside_table(world, "bronze.events", [1, 2, 3])
    _keeping(world, retries=0)
    _start(world)
    _drain(world)
    assert _task(world, "gold.dim_events").status is TaskStatus.SUCCEEDED
    install_history_cap(DEFAULT_MAX_HISTORY_OPEN_VERSIONS, open_bytes=1)

    _start(world)
    _drain(world)

    refused = _task(world, "gold.dim_events")
    assert refused.status is TaskStatus.FAILED
    assert refused.error_message is not None
    assert refused.error_message.startswith("Reading the open versions of gold.dim_events passed")
    assert "LAKANA_RUN__MAX_HISTORY_OPEN_BYTES (1) allows" in refused.error_message
    assert "the worker's memory limit (LAKANA_WORKER__MEMORY_LIMIT)" in refused.error_message


def _write_again(world: World, task_run_id: uuid.UUID) -> QueryRun:
    """Run a second history write of `gold.dim_events` for a task run, as a retried attempt does.

    The task run and its run are set back to running with the new write run as the task's
    current one: the state of a retry after an attempt whose commit landed but whose ending was
    lost.

    Parameters
    ----------
    world
        The world.
    task_run_id
        The task run whose attempt wrote the table already.

    Returns
    -------
    QueryRun
        The finished write run (detached).
    """
    with world.session() as session:
        environment = session.query(Environment).filter_by(name="dev").one()
        run = QueryRun(
            id=uuid.uuid4(),
            environment_id=environment.id,
            principal_id=world.admin.id,
            sql_text="SELECT id FROM bronze.events",
            status=QueryRunStatus.QUEUED,
            write_mode=WriteMode.HISTORY,
            target_name="gold.dim_events",
            history={"key": ["id"], "valid_from": None, "close_missing": False},
            task_run_id=task_run_id,
        )
        session.add(run)
        session.flush()
        task = session.get(TaskRun, task_run_id)
        assert task is not None
        task.status, task.query_run_id = TaskStatus.RUNNING, run.id
        session.execute(update(Run).where(Run.id == task.run_id).values(status=RunStatus.RUNNING))
        session.commit()
        run_one(session, run.id, AllowAllAuthorization())
        session.refresh(run)
        session.expunge(run)
        return run


def test_a_second_history_write_of_one_sql_task_run_is_refused(world: World) -> None:
    """The write run's task run marks its snapshot, so a retried attempt opens no second version."""
    _outside_table(world, "bronze.events", [1, 2])
    _keeping(world)
    _start(world)
    _drain(world)
    task = _task(world, "gold.dim_events")
    assert task.status is TaskStatus.SUCCEEDED, task.error_message
    _append_ids(world, "bronze.events", [3])

    again = _write_again(world, task.id)

    assert again.status is QueryRunStatus.FAILED
    assert again.error_message == RUN_WROTE_MESSAGE.format(table="gold.dim_events")
    assert _ids(world, "gold.dim_events") == [1, 2]


def test_a_history_write_gives_its_memory_back_whether_or_not_it_lands(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    trimmed: list[str] = []
    monkeypatch.setattr(pipeline_module, "trim_heap", lambda: trimmed.append("trim"))
    _outside_table(world, "bronze.events", [1, 2, 3])
    _keeping(world, retries=0)
    install_history_cap(2)
    _start(world)
    _drain(world)
    assert _task(world, "gold.dim_events").status is TaskStatus.FAILED
    assert trimmed == ["trim"]
    install_history_cap(3)

    _start(world)
    _drain(world)

    assert _task(world, "gold.dim_events").status is TaskStatus.SUCCEEDED
    assert trimmed == ["trim", "trim"]
