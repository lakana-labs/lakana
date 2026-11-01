"""Deploy checks, run tasks to their check phase and read back the results.

For the tests of `lakana_worker.task_checks` and of the Python tasks that have checks; built on
`task_run_helpers`.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pyarrow as pa
import pytest
import sqlalchemy
from pyiceberg.table import StaticTable
from sqlalchemy import select, update
from sqlalchemy.orm import Session
from task_run_helpers import World, _engine, _entry, _start, _task

import lakana_worker.checks as checks_module
from lakana_core.auth import (
    AllowAllAuthorization,
)
from lakana_core.catalog.identity import namespace_exists, resolve_table_id
from lakana_core.catalog.service import (
    CatalogService,
)
from lakana_core.identifiers import TableRef
from lakana_core.models import (
    AuditEvent,
    AuditEventType,
    CheckResult,
    QueryRun,
    Run,
    RunStatus,
    Table,
    TaskRun,
    TaskStatus,
    WriteMode,
)
from lakana_core.runs.checks import (
    results_of_task,
)
from lakana_core.runs.selection import Selection
from lakana_core.runs.service import RunService
from lakana_core.runs.store import scheduler_tick
from lakana_core.runs.tasks import (
    ClaimedTask,
    claim_task,
)
from lakana_core.storage.resolver import StorageResolver
from lakana_worker.tasks import RunEngine, run_claimed

# -- the check phase: count, verdict, rollback --------------------------------------

_DUPLICATE_IDS = "SELECT id FROM silver.orders GROUP BY id HAVING count(*) > 1"
"""A check of `silver.orders` that finds one row in `_checked`'s world (id 1 is there twice)."""


_SEE_YOURSELF = "Run the check's SQL yourself to see the full message."


_UNPUBLISHED = (
    "The table is not published: it had no published data before this run. Nothing downstream "
    "was built."
)
"""The sentence of a rollback that left the table not published."""


def _check(
    name: str, sql: str, *, table: str = "silver.orders", on_failure: str = "block"
) -> dict[str, object]:
    """Build a manifest check entry, as a deploy stores it.

    Parameters
    ----------
    name
        The check's short name.
    sql
        Its SELECT.
    table
        The checked table.
    on_failure
        ``block``, ``keep`` or ``warn``.

    Returns
    -------
    dict
        The entry.
    """
    return {
        "name": f"{table}/{name}",
        "table": table,
        "check": name,
        "path": f"checks/{table.replace('.', '/')}/{name}.sql",
        "on_failure": on_failure,
        "description": None,
        "sql": sql,
        "references": [table],
    }


def _checked(
    world: World,
    *checks: dict[str, object],
    sql: str = "SELECT id FROM bronze.src",
    **extra: object,
) -> None:
    """Deploy `bronze.src` (ids 1, 2, 1) → `silver.orders` → `gold.report`, with checks.

    Parameters
    ----------
    world
        The world.
    *checks
        The check entries, of `silver.orders` unless they say otherwise.
    sql
        The SELECT of `silver.orders`.
    **extra
        Overrides of the entry of `silver.orders` (`retries` is 2, the retry delay 0).
    """
    world.set_manifest(
        _entry("bronze.src", "SELECT (i % 2 + 1)::BIGINT AS id FROM range(3) AS r(i)"),
        _entry("silver.orders", sql, **{"retry_delay_seconds": 0, **extra}),
        _entry("gold.report", "SELECT id FROM silver.orders"),
        edges=[["bronze.src", "silver.orders"], ["silver.orders", "gold.report"]],
        checks=list(checks),
    )


def _tick(world: World) -> None:
    """Run one scheduler tick.

    Parameters
    ----------
    world
        The world.
    """
    with world.session() as session:
        scheduler_tick(session, datetime.now(UTC), lambda: 0.5)


def _claim_ready(world: World) -> ClaimedTask | None:
    """Tick, then claim the next ready task as `w1`, if there is one.

    Parameters
    ----------
    world
        The world.

    Returns
    -------
    ClaimedTask or None
        The claim.
    """
    _tick(world)
    with world.session() as session:
        return claim_task(session, "w1", 60)


def _drain(world: World, engine: RunEngine | None = None) -> None:
    """Claim and run every task that becomes ready, as a worker would.

    Parameters
    ----------
    world
        The world.
    engine
        The run engine; the default one when `None`.
    """
    for _ in range(12):
        claimed = _claim_ready(world)
        if claimed is None:
            _tick(world)
            return
        run_claimed(engine or _engine(world), claimed)
    pytest.fail("the run did not come to rest")


def _run_until(world: World, asset: str, engine: RunEngine | None = None) -> ClaimedTask:
    """Run the ready tasks until `asset` is claimed, and hand back that claim.

    Parameters
    ----------
    world
        The world.
    asset
        The task to stop at.
    engine
        The run engine; the default one when `None`.

    Returns
    -------
    ClaimedTask
        The claim of `asset`, not yet run.
    """
    for _ in range(12):
        claimed = _claim_ready(world)
        assert claimed is not None, f"{asset} never became ready"
        if claimed.asset == asset:
            return claimed
        run_claimed(engine or _engine(world), claimed)
    pytest.fail(f"{asset} was never claimed")


def _results(world: World, task_run_id: uuid.UUID) -> list[CheckResult]:
    """Read a task run's check results on a second session.

    Parameters
    ----------
    world
        The world.
    task_run_id
        The task run.

    Returns
    -------
    list of CheckResult
        By check name (detached).
    """
    with world.session() as session:
        rows = results_of_task(session, task_run_id)
        session.expunge_all()
        return rows


def _writes(world: World, task_run_id: uuid.UUID) -> list[QueryRun]:
    """Read the write runs of a task run.

    Parameters
    ----------
    world
        The world.
    task_run_id
        The task run.

    Returns
    -------
    list of QueryRun
        Its query runs that have a write mode (detached).
    """
    with world.session() as session:
        rows = list(
            session.scalars(
                select(QueryRun).where(
                    QueryRun.task_run_id == task_run_id, QueryRun.write_mode.is_not(None)
                )
            )
        )
        session.expunge_all()
        return rows


def _counts(world: World, task_run_id: uuid.UUID) -> list[QueryRun]:
    """Read the count runs of a task run.

    Parameters
    ----------
    world
        The world.
    task_run_id
        The task run.

    Returns
    -------
    list of QueryRun
        Its query runs without a write mode (detached).
    """
    with world.session() as session:
        rows = list(
            session.scalars(
                select(QueryRun).where(
                    QueryRun.task_run_id == task_run_id, QueryRun.write_mode.is_(None)
                )
            )
        )
        session.expunge_all()
        return rows


def _ids(world: World, dotted: str) -> list[int]:
    """Read a table's `id` column through the catalog, on a second session.

    Parameters
    ----------
    world
        The world.
    dotted
        The table.

    Returns
    -------
    list of int
        The ids, sorted.
    """
    with world.session() as session:
        loaded = CatalogService(
            session, StorageResolver(session), AllowAllAuthorization()
        ).load_table(world.admin, TableRef.from_dotted("dev", dotted))
        if loaded.metadata.current_snapshot_id is None:
            return []
        table = StaticTable.from_metadata(loaded.metadata_location).scan().to_arrow()
        return sorted(table.column("id").to_pylist())


def _marker(world: World, dotted: str) -> uuid.UUID | None:
    """Read the run a table is marked not published by.

    Parameters
    ----------
    world
        The world.
    dotted
        The table.

    Returns
    -------
    uuid.UUID or None
        `Table.unpublished_run_id`.
    """
    ref = TableRef.from_dotted("dev", dotted)
    with world.session() as session:
        table_id = resolve_table_id(session, "dev", ref.namespace.joined(), ref.name)
        assert table_id is not None
        return session.execute(
            select(Table.unpublished_run_id).where(Table.id == table_id)
        ).scalar_one()


def _events(world: World, event_type: AuditEventType) -> list[dict[str, Any]]:
    """Read the details of the audit events of one type.

    Parameters
    ----------
    world
        The world.
    event_type
        The type.

    Returns
    -------
    list of dict
        The details.
    """
    with world.session() as session:
        return [
            dict(event.detail)
            for event in session.scalars(
                select(AuditEvent).where(AuditEvent.event_type == event_type)
            )
        ]


def _after(run_id: uuid.UUID) -> str:
    """Build the closing sentence of a failed check's message.

    Parameters
    ----------
    run_id
        The run.

    Returns
    -------
    str
        The sentence.
    """
    return (
        "After a fix, deploy and run again; if the project did not change: "
        f"`lakana runs resume {run_id} --env dev`."
    )


def _rows(run_id: uuid.UUID, *checks: str) -> str:
    """Build the sentences that name the command which shows the rows of failed checks.

    Parameters
    ----------
    run_id
        The run.
    *checks
        The checks' full names (``silver.orders/unique_id``), in the order the sentences come
        (sorted by name).

    Returns
    -------
    str
        One sentence per check, each followed by a space.
    """
    return "".join(
        f"Look at the rows: `lakana check rows {run_id} {check} --env dev`. " for check in checks
    )


def _unique_rows(run_id: uuid.UUID) -> str:
    """Build the sentence for the check `silver.orders/unique_id`, which most tests fail.

    Parameters
    ----------
    run_id
        The run.

    Returns
    -------
    str
        The sentence, followed by a space.
    """
    return _rows(run_id, "silver.orders/unique_id")


def _expire_lease_and_tick(world: World, asset: str) -> None:
    """Let a task's lease run out and have the scheduler put the task back to ready.

    Parameters
    ----------
    world
        The world.
    asset
        The task.
    """
    with world.session() as session:
        session.execute(
            update(TaskRun)
            .where(TaskRun.asset == asset, TaskRun.status == TaskStatus.RUNNING)
            .values(lease_expires_at=datetime(2020, 1, 1, tzinfo=UTC))
        )
        session.commit()
    for _ in range(3):
        _tick(world)
    assert _task(world, asset).status is TaskStatus.READY


def _counting(
    monkeypatch: pytest.MonkeyPatch, *stand_ins: Callable[..., BaseException | None]
) -> list[uuid.UUID]:
    """Replace the first counts' `run_one` by stand-ins; later counts are real.

    Parameters
    ----------
    monkeypatch
        pytest's patcher.
    *stand_ins
        One per count, in order, each with `run_one`'s signature.

    Returns
    -------
    list of uuid.UUID
        The query runs counted so far, filled as they come.
    """
    seen: list[uuid.UUID] = []
    real = checks_module.run_one
    pending = list(stand_ins)

    def run(
        session: Session, query_run_id: uuid.UUID, *args: object, **kwargs: object
    ) -> BaseException | None:
        seen.append(query_run_id)
        if pending:
            return pending.pop(0)(session, query_run_id, *args, **kwargs)
        return real(session, query_run_id, *args, **kwargs)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(checks_module, "run_one", run)
    return seen


def _keep_note_on(world: World, dotted: str) -> uuid.UUID:
    """Put a note on a table, as an earlier run's failed `keep` check would have.

    Parameters
    ----------
    world
        The world.
    dotted
        The table.

    Returns
    -------
    uuid.UUID
        The run the note names.
    """
    ref = TableRef.from_dotted("dev", dotted)
    run_id = uuid.uuid4()
    with world.session() as session:
        session.execute(
            update(Table)
            .where(Table.id == resolve_table_id(session, "dev", ref.namespace.joined(), ref.name))
            .values(kept_run_id=run_id, kept_task_run_id=uuid.uuid4(), kept_check=f"{dotted}/old")
        )
        session.commit()
    return run_id


def _built_once_then_redeployed(
    world: World,
    *checks: dict[str, object],
    sql: str = "SELECT id + 10 AS id FROM bronze.src",
    **extra: object,
) -> None:
    """Build `silver.orders` (ids 1, 1, 2) in a run without checks, then deploy checks.

    Parameters
    ----------
    world
        The world.
    *checks
        The checks of the second deploy.
    sql
        The SELECT of `silver.orders` in the second deploy; ids 11, 11, 12 by default.
    **extra
        Overrides of the second deploy's entry of `silver.orders`.
    """
    _checked(world)
    first = _start(world)
    _drain(world)
    with world.session() as session:
        run = session.get(Run, first)
        assert run is not None and run.status is RunStatus.SUCCEEDED
    assert _ids(world, "silver.orders") == [1, 1, 2]
    _checked(world, *checks, sql=sql, **extra)


def _outside_table(world: World, dotted: str, ids: list[int]) -> None:
    """Create a table the project does not build.

    Parameters
    ----------
    world
        The world.
    dotted
        Its dotted name.
    ids
        Its `id` column.
    """
    data = pa.table({"id": pa.array(ids, pa.int64())})
    with world.session() as session:
        catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
        ref = TableRef.from_dotted("dev", dotted)
        if not namespace_exists(session, "dev", ref.namespace.joined()):
            catalog.create_namespace(world.admin, "dev", ref.namespace)
        catalog.write_batches(
            world.admin, ref, mode=WriteMode.CREATE, schema=data.schema, batches=data.to_batches()
        )


def _append_ids(world: World, dotted: str, ids: list[int]) -> None:
    """Append rows to a table the project does not build.

    Parameters
    ----------
    world
        The world.
    dotted
        Its dotted name.
    ids
        The `id` values to add.
    """
    data = pa.table({"id": pa.array(ids, pa.int64())})
    with world.session() as session:
        CatalogService(session, StorageResolver(session), AllowAllAuthorization()).write_batches(
            world.admin,
            TableRef.from_dotted("dev", dotted),
            mode=WriteMode.APPEND,
            schema=data.schema,
            batches=data.to_batches(),
        )


def _source_checked(world: World, check_sql: str) -> None:
    """Deploy `silver.orders` reading `bronze.events` (ids 1, 1, 2), and a check of that table.

    Parameters
    ----------
    world
        The world.
    check_sql
        The check of `bronze.events`.
    """
    _outside_table(world, "bronze.events", [1, 1, 2])
    world.set_manifest(
        _entry("silver.orders", "SELECT id FROM bronze.events", references=["bronze.events"]),
        checks=[_check("unique_id", check_sql, table="bronze.events")],
    )


_PASSING = "SELECT id FROM silver.orders WHERE id IS NULL"
"""A check of `silver.orders` that finds nothing."""


def _started_by_an_older_server(world: World, selection: Selection | None = None) -> uuid.UUID:
    """Start a run as a server that does not know checks would.

    Such a server sets no `has_checks` and creates no `source_check` task, so the readers of a
    checked table do not wait for one either.

    Parameters
    ----------
    world
        The world.
    selection
        The selection; everything when `None`.

    Returns
    -------
    uuid.UUID
        The run's id.
    """
    with world.session() as session:
        run = RunService(session, AllowAllAuthorization()).start(
            world.admin, "dev", "analytics", selection or Selection()
        )
        session.execute(
            sqlalchemy.delete(TaskRun).where(
                TaskRun.run_id == run.id, TaskRun.kind == "source_check"
            )
        )
        built = {t.asset for t in session.scalars(select(TaskRun).where(TaskRun.run_id == run.id))}
        for task in session.scalars(select(TaskRun).where(TaskRun.run_id == run.id)):
            task.has_checks = False
            task.upstream_json = [u for u in task.upstream_json if u in built]
        session.commit()
        run_id = run.id
    _tick(world)
    return run_id


def _refused(because: str) -> str:
    """Build the message of a task an older server started.

    Parameters
    ----------
    because
        What the project has checks for, as the message words it.

    Returns
    -------
    str
        The message.
    """
    return (
        "This run was started by a server that does not know checks, but the project has "
        f"checks for {because}. Upgrade the server to the version of the workers, then run "
        "again."
    )


def _refused_text(dotted: str, kept_run_id: uuid.UUID) -> str:
    """Build the text a task is refused a kept table with (K4), for a note of `_keep_note_on`.

    Parameters
    ----------
    dotted
        The kept table.
    kept_run_id
        The run its note names.

    Returns
    -------
    str
        The text.
    """
    return (
        f"`{dotted}` was kept after a failed check (`{dotted}/old`, run {kept_run_id}) and has "
        "not passed its checks since, so this task does not read it. A run of the project that "
        f"builds `{dotted}` clears this: build it again there "
        f"(`lakana run --select {dotted} --env dev`), or repair the data and run that project. "
        f"If no project builds it any more, drop it: `lakana table delete {dotted} --env dev`."
    )
