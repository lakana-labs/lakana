"""The stamp that names the task attempt which made a snapshot (#49).

A Python task writes its table through the REST API, so the worker cannot see the commit; it used
to count an attempt as succeeded when the table had changed. A change shows that somebody wrote,
not that this attempt did. Every snapshot committed under a bound task token's run context (the
claim form of `RunContext`) therefore carries the task run and the claim in its summary, and the
worker accepts a write only when it finds that stamp on the table's current branch, newer than
where the attempt started.

The stamp is written by the catalog only: `stamp_snapshots` removes both properties from every
snapshot a commit adds before it sets them, so a client of the Iceberg REST catalog cannot claim an
attempt's write as its own.

A SQL task's write run (the query-run form of `RunContext`, which only the worker builds, from
`QueryRun.task_run_id`) marks its snapshots with the task run alone, without a claim: a history
write reads that mark to refuse a second write of one run (`history.written_by_run`, #253), and a
mark without a claim never proves a Python attempt's write (`written_by_attempt` wants both).

A table created without rows has no snapshot to stamp. For it the evidence is the claim of its
location (`created_by_task_run`): the create records the task run that claimed it, and only the
current attempt of a task run can still commit (the write guard re-checks the claim under lock).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from pyiceberg.table.metadata import TableMetadata
from pyiceberg.table.snapshots import Snapshot, Summary
from pyiceberg.table.update import AddSnapshotUpdate, TableUpdate
from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.auth import RunContext
from lakana_core.models.table_location import TableLocation

TASK_RUN_PROPERTY = "lakana.task-run-id"
"""Snapshot summary property: the task run whose attempt committed the snapshot."""
CLAIM_PROPERTY = "lakana.task-claim"
"""Snapshot summary property: that attempt's claim (`task_runs.claim_count` when it was minted)."""
_RESERVED = (TASK_RUN_PROPERTY, CLAIM_PROPERTY)
MAX_ANCESTORS = 100
"""The most snapshots `written_by_attempt` walks back from the current one."""


def _stamped(snapshot: Snapshot, stamp: dict[str, str]) -> Snapshot:
    """Return a copy of a snapshot whose summary carries exactly `stamp` of the reserved keys.

    Parameters
    ----------
    snapshot
        The staged snapshot.
    stamp
        The reserved properties to set; empty to only remove them.

    Returns
    -------
    Snapshot
        `snapshot` itself when nothing changes, else a copy.
    """
    summary = snapshot.summary
    if summary is None:
        return snapshot
    kept = {k: v for k, v in summary.additional_properties.items() if k not in _RESERVED}
    wanted = {**kept, **stamp}
    if wanted == summary.additional_properties:
        return snapshot
    return snapshot.model_copy(update={"summary": Summary(summary.operation, **wanted)})


def stamp_snapshots(
    updates: Sequence[TableUpdate], context: RunContext | None
) -> list[TableUpdate]:
    """Stamp the snapshots a commit adds with the task attempt that commits them.

    Parameters
    ----------
    updates
        The commit's updates.
    context
        The acting principal's run context. The claim form (a bound task token) stamps the task
        run and the claim; the query-run form (a SQL task's write run) stamps the task run
        only; a context with both forms or neither, and none, only removes the reserved
        properties.

    Returns
    -------
    list of TableUpdate
        `updates`, each `AddSnapshotUpdate` replaced by one whose summary holds the stamp or
        no reserved property; the other updates are the same objects.
    """
    stamp: dict[str, str] = {}
    if context is not None and context.claim is not None and context.query_run_id is None:
        stamp = {TASK_RUN_PROPERTY: str(context.task_run_id), CLAIM_PROPERTY: str(context.claim)}
    elif context is not None and context.query_run_id is not None and context.claim is None:
        stamp = {TASK_RUN_PROPERTY: str(context.task_run_id)}
    stamped: list[TableUpdate] = []
    for update in updates:
        if isinstance(update, AddSnapshotUpdate):
            snapshot = _stamped(update.snapshot, stamp)
            if snapshot is not update.snapshot:
                update = AddSnapshotUpdate(snapshot=snapshot)
        stamped.append(update)
    return stamped


def written_by_attempt(
    metadata: TableMetadata, *, since: int | None, task_run_id: uuid.UUID, claim: int
) -> bool:
    """Tell whether an attempt's write is part of the table now.

    Walks from the current snapshot to its parents, at most `MAX_ANCESTORS`, and stops at
    `since`: a later writer may have committed on top of the attempt's snapshot, and a snapshot
    at or before `since` was there before the attempt started.

    Maintenance spares a running task's baseline, not the snapshot its attempt wrote: expired
    under a later writer's snapshot (`keep_days=0` while somebody else commits), the walk ends
    at the missing parent and the write reads as unproven. Rare, and the attempt then fails
    rather than counting somebody else's data as its own.

    Parameters
    ----------
    metadata
        The table's metadata, as read after the attempt.
    since
        The current snapshot when the attempt started; `None` when the table had none (or did
        not exist).
    task_run_id
        The attempt's task run.
    claim
        The attempt's claim.

    Returns
    -------
    bool
        Whether a snapshot on the current branch, newer than `since`, carries the attempt's stamp.
    """
    wanted = {TASK_RUN_PROPERTY: str(task_run_id), CLAIM_PROPERTY: str(claim)}
    snapshot_id = metadata.current_snapshot_id
    for _ in range(MAX_ANCESTORS):
        if snapshot_id is None or snapshot_id == since:
            return False
        snapshot = metadata.snapshot_by_id(snapshot_id)
        if snapshot is None:
            return False
        summary = snapshot.summary
        if summary is not None and all(summary.get(k) == v for k, v in wanted.items()):
            return True
        snapshot_id = snapshot.parent_snapshot_id
    return False


def created_by_task_run(session: Session, table_id: uuid.UUID, task_run_id: uuid.UUID) -> bool:
    """Tell whether a task run's write created a table, by the claim of the table's location.

    Parameters
    ----------
    session
        Database session; only reads.
    table_id
        The table.
    task_run_id
        The task run.

    Returns
    -------
    bool
        Whether the table's location record names `task_run_id` as the run that claimed it.
    """
    found = session.execute(
        select(TableLocation.id)
        .where(TableLocation.table_id == table_id, TableLocation.holder_run_id == task_run_id)
        .limit(1)
    ).scalar_one_or_none()
    return found is not None
