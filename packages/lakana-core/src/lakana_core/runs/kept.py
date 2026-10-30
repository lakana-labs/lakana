"""The note on a table that keeps data that failed a `keep` check.

A check with ``on_failure: keep`` leaves the run's data in the table. The worker sets a note
on the table's row after the verdict (`note_kept`) and clears it when a later check phase
passes, or when a rebuild finds the table without checks (`clear_kept`). A note that moves to a
newer run pushes the note it replaces onto the table's stack of earlier notes
(`Table.kept_earlier`; an empty entry when the data before had none), and a clear pushes the note
it removes, so the stack is the table's history of verdicts, newest last. Every state carries the
snapshot it was a verdict on (`Table.kept_snapshot_id`, `snapshot_id` of an entry). A set-back
to a snapshot that the table's current verdict does not describe takes the newest entry for that
snapshot back as the state (`restore_kept`), whoever set the current state: another run's passing
check may have cleared the note that the data now held carried. When no entry describes the
snapshot (the history ran out, or never existed), the table gets a note that says its checks are
not known (`Table.kept_unknown`): readers are refused, and the next passing checks clear it like
any other note.
Later tasks and readers act on the note; this module keeps
it, and answers the one question a reader's path asks of it: whether a task may read a table that
carries it (`task_may_read`, for `CatalogService.load_table`). Only a task of a run of the
project that builds the table may, and there only the table's own task, what comes after it in
the run, and the count of a check.
"""

from __future__ import annotations

import enum
import uuid
from collections.abc import Callable, Sequence
from typing import cast

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from lakana_core.audit.service import AuditService
from lakana_core.auth import Securable
from lakana_core.identifiers import NamespacePath, shown
from lakana_core.kept_mark import KeptSeen
from lakana_core.models import (
    AuditEventType,
    CheckOutcome,
    CheckResult,
    Environment,
    JobVersion,
    Namespace,
    QueryRun,
    Run,
    Table,
    TaskRun,
    TaskStatus,
)
from lakana_core.projects.manifest import manifest_assets
from lakana_core.projects.model_checks import REF_PREFIX
from lakana_core.runs.checks import ERROR_KEPT
from lakana_core.runs.store import is_check_count, upstream_names

__all__ = [
    "KEPT_EARLIER_LIMIT",
    "KEPT_SNAPSHOT_UNKNOWN",
    "KEPT_WAY_OUT",
    "KEPT_WHY",
    "KeptNote",
    "clear_kept",
    "clear_kept_in_attempt",
    "kept_message",
    "kept_run_of",
    "kept_tables",
    "kept_unchecked_of",
    "kept_unknown_of",
    "note_kept",
    "record_clean_in_attempt",
    "refused_message",
    "restore_kept",
    "shown_checks",
    "shown_kept_check",
    "task_may_read",
]


class KeptNote(enum.Enum):
    """What `note_kept` did.

    Attributes
    ----------
    NOTED
        The table carries the note now, or already did for this check phase.
    GONE
        The table was dropped since the checks ran: there is nothing to note.
    NOT_HELD
        The claim is no longer held: nothing was written.
    """

    NOTED = "noted"
    GONE = "gone"
    NOT_HELD = "not_held"


KEPT_EARLIER_LIMIT = 8
"""How many earlier states a table keeps.

Every change of the verdict (a new note, a clear, a passing write on a table with a history)
pushes one entry, and a set-back looks for the entry of the snapshot it goes back to. That entry
is the state the table had when the writer began, so it is only a few changes deep: the
overlapping runs of one project that write and judge the table between a write and its
set-back. Eight is more than that needs. The oldest entry goes when the limit is passed; a
set-back whose entry has gone does not guess, it marks the table as set back to data whose
checks are not known.
"""

KEPT_SNAPSHOT_UNKNOWN = -1
"""`Table.kept_snapshot_id` after a clear that did not know the snapshot its checks judged.

Never equal to a real snapshot id, so no restore trusts it; `None` means the table was never
marked or cleared, which is a different statement.
"""

_KEPT_STATE_UNKNOWN = (
    "Set back to data whose checks are not known (run {run} was set back): no passing check has "
    "judged the data the table holds. Tasks do not read it until its checks pass."
)
"""The state text of a table set back to data whose verdict the history does not show."""

KEPT_WHY = ("checks_passed", "rebuilt_without_checks")
"""Why a note was cleared: the values of the ``why`` in a ``TABLE_KEPT_CLEARED`` event."""

_KEPT_STATE = (
    "Kept after a failed check ({check}run {run}): the table holds the data that failed. "
    "Tasks do not read it until its checks pass."
)

_KEPT_STATE_UNCHECKED = (
    "Kept after a check that could not run ({check}run {run}): the table holds data no check "
    "could run on. Tasks do not read it until its checks pass."
)
"""The state text of a table whose new data no check could run on."""

KEPT_WAY_OUT = (
    "A run of the project that builds `{table}` clears this: build it again there "
    "(`lakana run --select {table} --env {environment}`), or repair the data and run that "
    "project. If no project builds it any more, drop it: "
    "`lakana table delete {table} --env {environment}`."
)
"""How a note is cleared when the reader cannot clear it (the rest of text K4).

The last sentence is for a table whose project removed the asset: no run clears its note, so
the way out is to drop the table.
"""

_KEPT_REFUSED = (
    "`{table}` was kept after a failed check ({check}run {run}) and has not passed its "
    "checks since, so this task does not read it."
)
"""Why a task is refused a kept table (the first sentence of text K4)."""

_KEPT_REFUSED_UNCHECKED = (
    "`{table}` was kept after a check that could not run ({check}run {run}) and has not "
    "passed its checks since, so this task does not read it."
)
"""Why a task is refused a table whose new data no check could run on."""

_KEPT_REFUSED_UNKNOWN = (
    "`{table}` was set back to data whose checks are not known (run {run} was set back) and has "
    "not passed its checks since, so this task does not read it."
)
"""Why a task is refused a table set back to data whose verdict the history does not show."""


def shown_checks(kept: Sequence[CheckResult]) -> str:
    """Name the failing `keep` checks as the state message shows them.

    Parameters
    ----------
    kept
        The failed `keep` results; at least one.

    Returns
    -------
    str
        The first check by name, escaped for a message, followed by ``" and N more"`` when
        more than one failed.
    """
    names = sorted(result.check_name for result in kept)
    text = shown(names[0])
    return text if len(names) == 1 else f"{text} and {len(names) - 1} more"


def _check_part(row: Table, check: str | None) -> str:
    """Word the check a note names, in front of its run.

    Parameters
    ----------
    row
        The table's row.
    check
        The check text to show (`shown_kept_check`); `None` for the row's own, and an empty text
        for none.

    Returns
    -------
    str
        ``"`<check>`, "``, or an empty text when no check is named.
    """
    text = row.kept_check if check is None else check
    return f"`{text}`, " if text else ""


def kept_message(row: Table, check: str | None = None) -> str | None:
    """Say that a table keeps data that failed a check (K2), from its row alone.

    Parameters
    ----------
    row
        The table's row.
    check
        The check text the reader may see (`shown_kept_check`); `None` for the row's own, and an
        empty text to name no check.

    Returns
    -------
    str or None
        The state message, or `None` when the row has no note. It names the first failing
        check (unless `check` is empty) and the run (a note of unknown checks, only the run),
        and does not need the run or the results to exist.
    """
    if row.kept_run_id is None:
        return None
    if row.kept_unknown:
        return _KEPT_STATE_UNKNOWN.format(run=row.kept_run_id)
    template = _KEPT_STATE_UNCHECKED if row.kept_unchecked else _KEPT_STATE
    return template.format(check=_check_part(row, check), run=row.kept_run_id)


def refused_message(row: Table, table: str, environment: str, check: str | None = None) -> str:
    """Say that a task does not read a kept table, and how the note is cleared (K4).

    Parameters
    ----------
    row
        The table's row; it carries the note.
    table
        The table's dotted name, as the reader addressed it.
    environment
        The environment's name.
    check
        The check text the text may show (`shown_kept_check`); `None` for the row's own, and an
        empty text to name no check.

    Returns
    -------
    str
        The text, built from the row alone: it does not need the run or the results to exist.
    """
    names = {"table": shown(table), "environment": shown(environment)}
    if row.kept_unknown:
        template = _KEPT_REFUSED_UNKNOWN
    else:
        template = _KEPT_REFUSED_UNCHECKED if row.kept_unchecked else _KEPT_REFUSED
    refused = template.format(check=_check_part(row, check), run=row.kept_run_id, **names)
    return f"{refused} {KEPT_WAY_OUT.format(**names)}"


def shown_kept_check(
    session: Session, row: Table, hides: Callable[[CheckResult], bool]
) -> str | None:
    """Word the check a table's note names for a reader from whom some results are withheld.

    The note names the first of the `keep` checks that failed and counts the others
    (`shown_checks`). When one of them is withheld (`hidden_checks.result_hider`), the text is
    built again from the visible ones, after the results were shown to give back the stored
    text, as `check_message.message_for_reader` does for a task's message. When they do not, or
    when no visible check is left, no check is named. Without the results (the run was deleted),
    a note that names a ``ref_`` check names none.

    Parameters
    ----------
    session
        Database session; read only.
    row
        The table's row, with a note.
    hides
        Whether a result is withheld from the reader.

    Returns
    -------
    str or None
        `None` to show the row's own text, an empty text to name no check, or the text to show.
    """
    if not row.kept_check:
        return None
    failed = [
        result
        for result in session.scalars(
            select(CheckResult)
            .where(
                CheckResult.task_run_id == row.kept_task_run_id,
                CheckResult.on_failure == "keep",
                CheckResult.outcome.in_([CheckOutcome.FAILED, CheckOutcome.ERRORED]),
            )
            .order_by(CheckResult.check_name)
        )
    ]
    if not failed:
        return "" if f"/{REF_PREFIX}" in row.kept_check else None
    if not any(hides(result) for result in failed):
        return None
    # The note named the checks that failed, or, when only checks refused a kept table failed,
    # those (`note_kept`'s `failed or found.kept`).
    ran = [r for r in failed if not (r.outcome is CheckOutcome.ERRORED and r.error == ERROR_KEPT)]
    for named in (failed, ran):
        if named and shown_checks(named) == row.kept_check:
            visible = [result for result in named if not hides(result)]
            return shown_checks(visible) if visible else ""
    return ""


def _is_count(session: Session, query_run_id: uuid.UUID, task_run_id: uuid.UUID) -> bool:
    """Tell whether a query run is the count of a check of a task.

    The worker inserts two kinds of query run for a task itself, without a token: the write
    run of a SQL task, which has a write mode and a target, and the count of a check
    (`lakana_worker.checks.count_check`), which has neither. A query submitted with a bound
    task token (a Python task's input read) carries the token and comes with the claim form
    of the run context, never with a query run. No REST schema sets `task_run_id`.

    Parameters
    ----------
    session
        Database session. Starts no write.
    query_run_id
        The query run of the reader's run context.
    task_run_id
        The task run of the same context; the query run must be that task's.

    Returns
    -------
    bool
        Whether the run exists, belongs to the task, was submitted without a token and is a
        count by `is_check_count`.
    """
    run = session.execute(
        select(QueryRun).where(
            QueryRun.id == query_run_id,
            QueryRun.task_run_id == task_run_id,
            QueryRun.task_token_id.is_(None),
        )
    ).scalar_one_or_none()
    return run is not None and is_check_count(session, run)


def task_may_read(
    session: Session,
    task_run_id: uuid.UUID,
    query_run_id: uuid.UUID | None,
    environment_id: uuid.UUID,
    table: tuple[str, ...],
) -> bool:
    """Decide whether a task may read a table that carries the note.

    Only a task of a run of the project that builds the table may: the table is an asset of
    the manifest of the run's project version. Another project's check of the table may be weaker
    than the check that failed, so neither its check task nor what reads below it has
    standing. In such a run, a task may read when

    - its own asset is the table (the table's check task, its check phase or its rebuild: the
      checks must read what they judge, and a rebuild replaces it), or
    - a task of its run whose asset is the table is among its upstreams, directly or through
      other tasks, or
    - the read is the count of a check: a count builds nothing, and two tables whose checks
      read each other would otherwise each wait for the other to pass.

    Called only for a row that carries the note: it costs two reads, a third (and a
    fourth, for a count) for the worker's form of the run context when neither of the first two
    cases holds, and a table without a note costs none.

    Parameters
    ----------
    session
        Database session. Starts no write.
    task_run_id
        The reading task run, from the reader's run context.
    query_run_id
        The query run of the reader's run context; `None` for the claim form.
    environment_id
        The environment of the table.
    table
        The table's namespace components followed by its name. An asset is compared by its
        components, as the write guard compares it (`CatalogService._require_own_task`).

    Returns
    -------
    bool
        `False` also when the task run is gone, or its run belongs to another environment
        (an asset of the same name there is another table).
    """

    def is_table(asset: str) -> bool:
        """Tell whether an asset's name is the table's.

        Parameters
        ----------
        asset
            A dotted asset name.

        Returns
        -------
        bool
            Whether its components are the table's.
        """
        return tuple(asset.split(".")) == table

    reader = session.execute(
        select(
            TaskRun.run_id,
            TaskRun.asset,
            TaskRun.upstream_json,
            Run.environment_id,
            JobVersion.manifest_json,
        )
        .join(Run, Run.id == TaskRun.run_id)
        .join(JobVersion, JobVersion.id == Run.job_version_id)
        .where(TaskRun.id == task_run_id)
    ).one_or_none()
    if reader is None or reader.environment_id != environment_id:
        return False
    if not any(is_table(asset) for asset in manifest_assets(reader.manifest_json)):
        return False
    if is_table(reader.asset):
        return True
    upstream_of = {
        asset: upstream_names(upstream)
        for asset, upstream in session.execute(
            select(TaskRun.asset, TaskRun.upstream_json).where(TaskRun.run_id == reader.run_id)
        )
    }
    # The engine makes a task ready only when every upstream of it has succeeded
    # (`scheduler`: all of `task.upstream` are SUCCEEDED), and that holds for each upstream in
    # turn. So a reader that has the table's task above it runs only after that task succeeded:
    # the table's checks passed in this run, or it was rebuilt and checked. Any other task of
    # the run could run before or beside the table's task, and would read the data that failed.
    # A name in `upstream_json` that no task of the run has is not followed: only a task counts.
    seen: set[str] = set()
    waiting = [name for name in upstream_names(reader.upstream_json) if name in upstream_of]
    while waiting:
        name = waiting.pop()
        if is_table(name):
            return True
        if name not in seen:
            seen.add(name)
            waiting.extend(above for above in upstream_of[name] if above in upstream_of)
    return query_run_id is not None and _is_count(session, query_run_id, task_run_id)


def kept_tables(session: Session, environment_id: uuid.UUID) -> dict[str, uuid.UUID]:
    """Name every table of an environment that carries the note, with the run that kept it.

    Run creation reads this once per run start; an environment without a note costs this one
    query and nothing else.

    Parameters
    ----------
    session
        Database session. Starts no write.
    environment_id
        The environment.

    Returns
    -------
    dict
        The table's dotted name (namespace, a dot, table) to `Table.kept_run_id`.
    """
    rows = session.execute(
        select(Namespace.path, Table.name, Table.kept_run_id)
        .join(Namespace, Namespace.id == Table.namespace_id)
        .where(Namespace.environment_id == environment_id, Table.kept_run_id.is_not(None))
    ).all()
    return {
        ".".join((*NamespacePath.parse(path).parts, name)): cast("uuid.UUID", run_id)
        for path, name, run_id in rows  # the query keeps only rows with a note
    }


def kept_unchecked_of(session: Session, table_id: uuid.UUID) -> bool:
    """Say whether a table's note names a check that could not run (`Table.kept_unchecked`).

    Parameters
    ----------
    session
        Database session. Starts no write.
    table_id
        The table.

    Returns
    -------
    bool
        `False` also for a table without a note or one that is gone.
    """
    return bool(session.scalar(select(Table.kept_unchecked).where(Table.id == table_id)))


def kept_unknown_of(session: Session, table_id: uuid.UUID) -> bool:
    """Say whether a table's note says it was set back to data whose checks are not known.

    Parameters
    ----------
    session
        Database session. Starts no write.
    table_id
        The table.

    Returns
    -------
    bool
        `False` also for a table without a note or one that is gone.
    """
    return bool(session.scalar(select(Table.kept_unknown).where(Table.id == table_id)))


def kept_run_of(session: Session, table_id: uuid.UUID) -> uuid.UUID | None:
    """Read the run that a table's note names.

    Parameters
    ----------
    session
        Database session. Starts no write.
    table_id
        The table.

    Returns
    -------
    uuid.UUID or None
        `Table.kept_run_id`; `None` when the table has no note or is gone.
    """
    return session.scalar(select(Table.kept_run_id).where(Table.id == table_id))


def _entry_of(
    run_id: uuid.UUID | None,
    task_run_id: uuid.UUID | None,
    check: str | None,
    unchecked: bool,
    snapshot_id: int | None,
    unknown: bool = False,
) -> dict[str, object]:
    """Say what a table's note state is, as an entry of the stack of earlier notes.

    Parameters
    ----------
    run_id
        `Table.kept_run_id`.
    task_run_id
        `Table.kept_task_run_id`.
    check
        `Table.kept_check`.
    unchecked
        `Table.kept_unchecked`.
    snapshot_id
        The snapshot the state was a verdict on.
    unknown
        `Table.kept_unknown`.

    Returns
    -------
    dict
        The snapshot alone when there is no note, else the note's four values (ids as text) and
        the snapshot, with ``"unknown": True`` for a note of unknown checks.
    """
    if run_id is None:
        return {"snapshot_id": snapshot_id}
    entry: dict[str, object] = {
        "run_id": str(run_id),
        "task_run_id": None if task_run_id is None else str(task_run_id),
        "check": check,
        "unchecked": bool(unchecked),
        "snapshot_id": snapshot_id,
    }
    if unknown:
        entry["unknown"] = True
    return entry


def _is_snapshot(snapshot_id: object) -> bool:
    """Tell whether a stored snapshot id names a snapshot.

    Parameters
    ----------
    snapshot_id
        `Table.kept_snapshot_id` or the ``snapshot_id`` of an entry.

    Returns
    -------
    bool
        `False` for `None` and `KEPT_SNAPSHOT_UNKNOWN`, which describe no snapshot.
    """
    return snapshot_id is not None and snapshot_id != KEPT_SNAPSHOT_UNKNOWN


def _pushed(stack: list[dict[str, object]], entry: dict[str, object]) -> list[dict[str, object]]:
    """Put an entry on the stack of earlier states, newest last, within the limit.

    An older entry for the same snapshot goes: the newer verdict on a snapshot is the one a
    set-back to it must find, and the older one, left below, would be found once the newer one
    was used.

    Parameters
    ----------
    stack
        `Table.kept_earlier`.
    entry
        The entry to push.

    Returns
    -------
    list
        The new stack, at most `KEPT_EARLIER_LIMIT` entries.
    """
    snapshot_id = entry.get("snapshot_id")
    kept = [
        old
        for old in stack
        if not _is_snapshot(snapshot_id) or old.get("snapshot_id") != snapshot_id
    ]
    return [*kept, entry][-KEPT_EARLIER_LIMIT:]


def restore_kept(
    session: Session,
    table_id: uuid.UUID,
    *,
    run_id: uuid.UUID,
    task_run_id: uuid.UUID,
    snapshot_before: int | None,
    snapshot_after: int | None,
) -> dict[str, object] | None:
    """Give a table set back to a snapshot the state that snapshot had, uncommitted.

    Called inside the transaction of the set-back, after its pointer swap: the table's row is
    locked by the swap (the pointer is a column of that row), and it is read ``FOR UPDATE`` here
    so that a note that moves at the same time cannot overwrite what is restored.

    The state of the row is a verdict on one snapshot (`Table.kept_snapshot_id`). When that is
    the snapshot the table goes back to, nothing changes. Otherwise the verdict is about data
    the set-back removed (the writer's own, which another run's passing check may have judged
    and cleared, or a note the writer set), and the newest entry of `Table.kept_earlier` for
    the snapshot the table goes back to is the state to put back, with the mark it carried or none.
    The entries above it describe data newer than that snapshot, which is gone, so they are
    dropped with it. With no entry for the snapshot (the history ran out, was never kept, or the
    entry cannot be read) nothing says whether the data passed its checks, so the table gets a
    note of unknown checks: it names the run and task run that were set back, refuses readers
    like every note, and is cleared by the next passing checks like every note. A table that
    never had a verdict (`Table.kept_snapshot_id` is `None`) and has no note is left alone: no
    check has ever judged it.

    A table with a note is touched only when the note is about the data the set-back removes: it
    names this run and task run, or it is a verdict on the snapshot the set-back removes
    (`snapshot_after`; a note of unknown checks that a set-back of another writer left on that
    data, when the writers are set back in the reverse order of their writes). Any other note is
    about other data and stays, whatever the entries say. A first build, which no snapshot or
    entry describes, leaves the note it has. A placeholder (`KEPT_SNAPSHOT_UNKNOWN`) is no
    snapshot: it matches nothing, so the table gets a note of unknown checks. In that case the
    entries about the removed data go, since nothing can be set back to them any more.

    Parameters
    ----------
    session
        Database session; the caller commits.
    table_id
        The table that was set back.
    run_id
        The run whose data was set back.
    task_run_id
        The task run whose data was set back.
    snapshot_before
        The snapshot the table is set back to; `None` for a first build, which no entry
        describes.
    snapshot_after
        The snapshot the set-back removes; `None` when the write made none.

    Returns
    -------
    dict or None
        `None` when nothing is written. Otherwise the part of the audit detail that says what
        became of the state: ``kept_restored`` with the entry now noted (`None` for no note),
        ``kept_unknown`` set to `True` for a note of unknown checks, or ``kept_not_restored``
        set to `True` for a first build that keeps its note.
    """
    found = session.execute(
        select(
            Table.kept_run_id,
            Table.kept_task_run_id,
            Table.kept_snapshot_id,
            Table.kept_earlier,
        )
        .where(Table.id == table_id)
        .with_for_update()
    ).one_or_none()
    if found is None:
        return None
    marked = found.kept_run_id is not None
    names_this = found.kept_run_id == run_id and (
        found.kept_task_run_id is None or found.kept_task_run_id == task_run_id
    )
    removed = _is_snapshot(snapshot_after) and found.kept_snapshot_id == snapshot_after
    if marked and not names_this and not removed:
        return None
    if snapshot_before is None:
        return {"kept_not_restored": True} if marked else None
    known = _is_snapshot(snapshot_before)
    if (known and found.kept_snapshot_id == snapshot_before) or (
        not marked and found.kept_snapshot_id is None
    ):
        return None
    stack = list(found.kept_earlier or [])
    entry: dict[str, object] | None = None
    values: dict[str, object] | None = None
    for index in reversed(range(len(stack))) if known else ():
        if stack[index].get("snapshot_id") == snapshot_before:
            entry = stack[index]
            values = _state_of(entry, snapshot_before, stack[:index])
            break
    unknown = values is None
    if unknown and _is_snapshot(snapshot_after):
        stack = [old for old in stack if old.get("snapshot_id") != snapshot_after]
    if values is None:
        values = {
            "kept_run_id": run_id,
            "kept_task_run_id": task_run_id,
            "kept_check": None,
            "kept_unchecked": False,
            "kept_unknown": True,
            "kept_snapshot_id": snapshot_before,
            "kept_earlier": stack,
        }
    session.execute(
        update(Table)
        .where(Table.id == table_id)
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    if unknown or entry is None:
        return {"kept_unknown": True}
    return {"kept_restored": entry if "run_id" in entry else None}


def _state_of(
    entry: dict[str, object], snapshot_id: int, rest: list[dict[str, object]]
) -> dict[str, object] | None:
    """Turn an entry of the stack back into the columns of the row.

    Parameters
    ----------
    entry
        The entry.
    snapshot_id
        The snapshot it describes, which is the verdict snapshot of the row afterwards.
    rest
        The stack below the entry, which becomes the stack of the row.

    Returns
    -------
    dict or None
        The columns to set, or `None` when the entry cannot be read.
    """
    values: dict[str, object] = {
        "kept_run_id": None,
        "kept_task_run_id": None,
        "kept_check": None,
        "kept_unchecked": False,
        "kept_unknown": False,
        "kept_snapshot_id": snapshot_id,
        "kept_earlier": rest,
    }
    if "run_id" not in entry:
        return values
    try:
        task_run = entry["task_run_id"]
        values.update(
            kept_run_id=uuid.UUID(cast("str", entry["run_id"])),
            kept_task_run_id=None if task_run is None else uuid.UUID(cast("str", task_run)),
            kept_check=entry["check"],
            kept_unchecked=bool(entry["unchecked"]),
            kept_unknown=bool(entry.get("unknown")),
        )
    except (KeyError, TypeError, ValueError):
        return None
    return values


def _held(
    session: Session, task_run_id: uuid.UUID, worker: str, claim: int
) -> tuple[uuid.UUID, uuid.UUID | None, str] | None:
    """Read the task's run, principal and environment name if the claim is held.

    Parameters
    ----------
    session
        Database session.
    task_run_id
        The task run.
    worker
        The worker that claims to hold the task.
    claim
        The claim it claims to hold.

    Returns
    -------
    tuple or None
        ``(run id, run-as principal id, environment name)``, or `None` when the claim is not
        held. The task run is read ``FOR SHARE``, as `record_result` reads it.
    """
    held = session.execute(
        select(TaskRun.run_id, Run.run_as_principal_id, Environment.name)
        .join(Run, Run.id == TaskRun.run_id)
        .join(Environment, Environment.id == Run.environment_id)
        .where(
            TaskRun.id == task_run_id,
            TaskRun.status == TaskStatus.RUNNING,
            TaskRun.lease_owner == worker,
            TaskRun.claim_count == claim,
        )
        .with_for_update(read=True, of=TaskRun)
    ).one_or_none()
    return None if held is None else (held[0], held[1], held[2])


def _securable_of(session: Session, environment: str, table_id: uuid.UUID) -> Securable | None:
    """Build a table's securable from its row.

    Parameters
    ----------
    session
        Database session.
    environment
        The environment's name.
    table_id
        The table.

    Returns
    -------
    Securable or None
        The securable, or `None` when the table was dropped.
    """
    found = session.execute(
        select(Namespace.path, Table.name)
        .join(Namespace, Namespace.id == Table.namespace_id)
        .where(Table.id == table_id)
    ).one_or_none()
    if found is None:
        return None
    return Securable.table_scope(environment, NamespacePath.parse(found[0]), found[1])


def note_kept(
    session: Session,
    *,
    task_run_id: uuid.UUID,
    worker: str,
    claim: int,
    table_id: uuid.UUID,
    kept: Sequence[CheckResult],
    unchecked: bool = False,
) -> KeptNote:
    """Note on a table that it keeps the data that failed `keep` checks, for the claim.

    Sets the columns of the note, pushes the state it replaces (the note, or an empty entry when
    the table had none) onto `Table.kept_earlier`, and audits ``TABLE_KEPT`` in one transaction.
    A table that was dropped since the checks ran has nothing to note: nothing is written, the
    claim is still held, and the result says so. A table whose note already names this task run
    and these checks (a reclaim repeats the call) is left as it is, with no second event.

    Parameters
    ----------
    session
        Database session; commits.
    task_run_id
        The task run whose check phase reached the verdict.
    worker
        The worker that claims to hold the task.
    claim
        The claim it claims to hold.
    table_id
        The checked table.
    kept
        The failed `keep` results; at least one.
    unchecked
        Whether `kept` are checks that could not run on this table's new data (they read another
        project's kept table), not checks that failed. The state text then says that the
        table holds data no check could run on.

    Returns
    -------
    KeptNote
        `NOT_HELD` when the claim is not held, `GONE` when the table was dropped (in both
        nothing is written), else `NOTED`.
    """
    held = _held(session, task_run_id, worker, claim)
    if held is None:
        session.rollback()
        return KeptNote.NOT_HELD
    run_id, principal_id, environment = held
    securable = _securable_of(session, environment, table_id)
    if securable is None:
        session.rollback()
        return KeptNote.GONE
    shown_text = shown_checks(kept)
    current = session.execute(
        select(
            Table.kept_run_id,
            Table.kept_task_run_id,
            Table.kept_check,
            Table.kept_unchecked,
            Table.kept_unknown,
            Table.kept_snapshot_id,
            Table.kept_earlier,
        )
        .where(Table.id == table_id)
        .with_for_update()  # a set-back that restores a note reads and writes the same stack
    ).one()
    if (current.kept_task_run_id, current.kept_check, current.kept_unchecked) == (
        task_run_id,
        shown_text,
        unchecked,
    ):
        session.rollback()  # a reclaim repeats the call; the note is there already
        return KeptNote.NOTED
    stamp = session.execute(
        select(TaskRun.written_snapshot_before, TaskRun.written_snapshot_after).where(
            TaskRun.id == task_run_id
        )
    ).one()
    earlier = list(current.kept_earlier or [])
    same_data = (
        current.kept_task_run_id == task_run_id
        if current.kept_task_run_id is not None
        else current.kept_run_id == run_id
    )
    if not same_data:
        # Another verdict of the same task run is about the same data and replaces nothing.
        # The state before is the note, as a verdict on its own snapshot. Without a note, it is
        # the unmarked state of the snapshot before this write, unless a clear has judged the
        # table since (`kept_snapshot_id` is then the snapshot that clear judged, and the
        # unmarked state is about that one): a clear that judged this run's own data, or a
        # snapshot of the past, says nothing about the data before the write.
        if current.kept_run_id is None and current.kept_snapshot_id is None:
            described = stamp.written_snapshot_before
        else:
            described = current.kept_snapshot_id
        entry = _entry_of(
            current.kept_run_id,
            current.kept_task_run_id,
            current.kept_check,
            current.kept_unchecked,
            described,
            current.kept_unknown,
        )
        earlier = _pushed(earlier, entry)
    session.execute(
        update(Table)
        .where(Table.id == table_id)
        .values(
            kept_run_id=run_id,
            kept_task_run_id=task_run_id,
            kept_check=shown_text,
            kept_unchecked=unchecked,
            kept_unknown=False,
            kept_snapshot_id=stamp.written_snapshot_after,
            kept_earlier=earlier,
        )
        .execution_options(synchronize_session=False)
    )
    AuditService(session).record(
        principal_id,
        AuditEventType.TABLE_KEPT,
        securable=securable,
        detail={
            "checks": sorted(result.check_name for result in kept),
            "run_id": str(run_id),
            "task_run_id": str(task_run_id),
            **({"unchecked": True} if unchecked else {}),
        },
    )
    session.commit()
    return KeptNote.NOTED


def _clear_row(
    session: Session,
    *,
    table_id: uuid.UUID,
    snapshot_id: int | None,
    principal_id: uuid.UUID | None,
    securable: Securable,
    why: str,
    run_id: uuid.UUID,
    task_run_id: uuid.UUID,
    seen: KeptSeen,
) -> bool:
    """Clear a table's note, keep it on the stack, and audit it, uncommitted.

    Parameters
    ----------
    session
        Database session; the caller commits.
    table_id
        The table.
    snapshot_id
        The snapshot the passing checks judged; `KEPT_SNAPSHOT_UNKNOWN` is stored when `None`.
    principal_id
        The run's principal, for the audit event.
    securable
        The table's securable.
    why
        One of `KEPT_WHY`.
    run_id
        The clearing run.
    task_run_id
        The clearing task run.
    seen
        The mark the clearing checks, or the rebuild, superseded, as read when they began. Only
        that mark is cleared: a mark another run noted since stays.

    Returns
    -------
    bool
        Whether the table had that note.
    """
    if seen.run_id is None:
        return False  # no note was seen, so there is none of theirs to clear
    current = session.execute(
        select(
            Table.kept_run_id,
            Table.kept_task_run_id,
            Table.kept_check,
            Table.kept_unchecked,
            Table.kept_unknown,
            Table.kept_snapshot_id,
            Table.kept_earlier,
        )
        .where(Table.id == table_id)
        .with_for_update()  # the clear pushes onto the stack a set-back reads and rewrites
    ).one_or_none()
    if (
        current is None
        or current.kept_run_id != seen.run_id
        or current.kept_task_run_id != seen.task_run_id
    ):
        return False
    # The mark goes onto the stack with the snapshot it was a verdict on: a writer that is set
    # back to that data later finds it there, even if this clear judged newer data.
    removed = _entry_of(
        current.kept_run_id,
        current.kept_task_run_id,
        current.kept_check,
        current.kept_unchecked,
        current.kept_snapshot_id,
        current.kept_unknown,
    )
    session.execute(
        update(Table)
        .where(Table.id == table_id)
        .values(
            kept_run_id=None,
            kept_task_run_id=None,
            kept_check=None,
            kept_unchecked=False,
            kept_unknown=False,
            kept_snapshot_id=KEPT_SNAPSHOT_UNKNOWN if snapshot_id is None else snapshot_id,
            kept_earlier=_pushed(list(current.kept_earlier or []), removed),
        )
        .execution_options(synchronize_session=False)
    )
    AuditService(session).record(
        principal_id,
        AuditEventType.TABLE_KEPT_CLEARED,
        securable=securable,
        detail={"why": why, "run_id": str(run_id), "task_run_id": str(task_run_id)},
    )
    return True


def clear_kept(
    session: Session,
    *,
    task_run_id: uuid.UUID,
    worker: str,
    claim: int,
    table_id: uuid.UUID,
    why: str,
    seen: KeptSeen,
    snapshot_id: int | None = None,
) -> bool:
    """Clear a table's note for the claim that rebuilt it, in its own transaction.

    Parameters
    ----------
    session
        Database session; commits.
    task_run_id
        The task run.
    worker
        The worker that claims to hold the task.
    claim
        The claim it claims to hold.
    table_id
        The table.
    why
        One of `KEPT_WHY`.
    seen
        The note the rebuild superseded, as read before its write: only that one is cleared.
    snapshot_id
        The snapshot the table's data is, when known (`Table.kept_snapshot_id`).

    Returns
    -------
    bool
        Whether the note was cleared: `False` when the claim is not held (nothing is written),
        and `False` when the table has no note (no event).
    """
    held = _held(session, task_run_id, worker, claim)
    if held is None:
        session.rollback()
        return False
    run_id, principal_id, environment = held
    securable = _securable_of(session, environment, table_id)
    if securable is None:
        session.rollback()
        return False
    changed = _clear_row(
        session,
        table_id=table_id,
        snapshot_id=snapshot_id,
        principal_id=principal_id,
        securable=securable,
        why=why,
        run_id=run_id,
        task_run_id=task_run_id,
        seen=seen,
    )
    session.commit()
    return changed


def clear_kept_in_attempt(
    session: Session,
    *,
    task_run_id: uuid.UUID,
    table_id: uuid.UUID,
    why: str,
    snapshot_id: int | None,
    seen: KeptSeen,
) -> bool:
    """Clear a table's note inside the transaction that records the task's success.

    For `record_attempt`'s ``in_transaction``: the attempt has been recorded by then (the task
    run is no longer running), so the claim is not asked again, and nothing is committed here.
    The clear lands with the success or not at all: a worker lost, or a task handed back,
    between a clear and its record could otherwise leave the table without a note while the
    task, still waiting, is set back to data that carried one.

    Parameters
    ----------
    session
        Database session; the caller commits.
    task_run_id
        The task run whose success is recorded.
    table_id
        The table.
    why
        One of `KEPT_WHY`.
    snapshot_id
        The snapshot the passing checks judged, when known.
    seen
        The mark the checks began with: only that mark is cleared (`_clear_row`).

    Returns
    -------
    bool
        Whether the table still had that note.
    """
    held = session.execute(
        select(Run.id, Run.run_as_principal_id, Environment.name)
        .join(TaskRun, TaskRun.run_id == Run.id)
        .join(Environment, Environment.id == Run.environment_id)
        .where(TaskRun.id == task_run_id)
    ).one_or_none()
    if held is None:
        return False
    securable = _securable_of(session, held[2], table_id)
    if securable is None:
        return False
    return _clear_row(
        session,
        table_id=table_id,
        snapshot_id=snapshot_id,
        principal_id=held[1],
        securable=securable,
        why=why,
        run_id=held[0],
        task_run_id=task_run_id,
        seen=seen,
    )


def record_clean_in_attempt(session: Session, table_id: uuid.UUID, snapshot_id: int) -> None:
    """Record that a snapshot passed its checks on a table with no note, uncommitted.

    For the success of a task that built a table with a history of notes: the verdict snapshot
    of an unmarked table would otherwise stay at the last clear, and a later set-back to the
    data that passed since could not be shown to be a verdict. The clean state it replaces goes
    onto the stack, as a verdict on the snapshot it was recorded for: a set-back of this write
    finds that verdict only when it is the data before the write (the verdict may be older, and
    then the set-back marks the checks unknown; nothing here claims a verdict for a snapshot no
    check judged). A table that has a note is left alone, so that a note which
    landed meanwhile is never overwritten, and neither is a failing verdict on the same snapshot
    that the stack holds: the phase may have counted the data before that verdict was reached, so
    it cannot show that its own is the newer one, and the table stays as it is. The caller asks
    only for a table whose `kept_snapshot_id` is set, so a table that never had a note pays no
    statement.

    Parameters
    ----------
    session
        Database session; the caller commits.
    table_id
        The table.
    snapshot_id
        The one snapshot the passing checks judged.
    """
    current = session.execute(
        select(Table.kept_run_id, Table.kept_snapshot_id, Table.kept_earlier)
        .where(Table.id == table_id)
        .with_for_update()
    ).one_or_none()
    if current is None or current.kept_run_id is not None:
        return
    if any(
        entry.get("snapshot_id") == snapshot_id and "run_id" in entry
        for entry in current.kept_earlier or []
    ):
        return  # a failing verdict on this snapshot is recorded; this one cannot show it is newer
    values: dict[str, object] = {"kept_snapshot_id": snapshot_id}
    if _is_snapshot(current.kept_snapshot_id) and current.kept_snapshot_id != snapshot_id:
        values["kept_earlier"] = _pushed(
            list(current.kept_earlier or []), {"snapshot_id": current.kept_snapshot_id}
        )
    session.execute(
        update(Table)
        .where(Table.id == table_id, Table.kept_run_id.is_(None))
        .values(**values)
        .execution_options(synchronize_session=False)
    )
