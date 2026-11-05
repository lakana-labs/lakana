"""The jobs that build a table, and the messages of its fired health signals (#340).

A fired health signal is told to the notification targets of the jobs that build its table
(`queue_health_notifications`): every job of the environment's current deployments whose
selection holds the table. A signal that a run's blocking health check judges is told by that
run's ``failed`` message instead (`judged_by_run`, `rows_to_tell`). This lives in the catalog, not
in `lakana_core.notifications.events`, which the catalog's own imports reach first.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import ColumnElement, ScalarSelect, and_, case, func, select
from sqlalchemy.orm import Session

from lakana_core.catalog.health_record import NEWEST_COMMIT, NOT_SET_BACK
from lakana_core.errors import ValidationError
from lakana_core.models import (
    EnvironmentDeployment,
    JobVersion,
    NotificationDelivery,
    Run,
    TableHealthSignal,
    TaskRun,
)
from lakana_core.notifications.events import HEALTH, new_delivery
from lakana_core.projects.health_checks import health_specs
from lakana_core.projects.manifest import (
    JobNotify,
    job_notify,
    job_schedule,
    manifest_assets,
    manifest_jobs,
)
from lakana_core.runs.selection import ManifestGraph, resolve_selection, selection_of


@dataclass(frozen=True)
class BuildingJob:
    """A job of a deployed project whose selection holds a table.

    Parameters
    ----------
    repository_id
        The project's repository.
    job
        The job's name.
    notify
        Where the job reports; `None` for a job without `notify:`.
    schedule
        Its cron expression and zone; `None` for a job without a schedule.
    """

    repository_id: uuid.UUID
    job: str
    notify: JobNotify | None
    schedule: tuple[str, str] | None


def deployed_manifests(
    session: Session, environment_id: uuid.UUID
) -> list[tuple[uuid.UUID, object]]:
    """Read the current manifest of every repository deployed to an environment.

    Parameters
    ----------
    session
        Database session; read without flushing.
    environment_id
        The environment.

    Returns
    -------
    list of tuple
        (repository id, manifest), by repository id.
    """
    with session.no_autoflush:
        rows = session.execute(
            select(EnvironmentDeployment.repository_id, JobVersion.manifest_json)
            .join(JobVersion, JobVersion.id == EnvironmentDeployment.current_job_version_id)
            .where(EnvironmentDeployment.environment_id == environment_id)
            .order_by(EnvironmentDeployment.repository_id)
        ).all()
    return [(repository_id, manifest) for repository_id, manifest in rows]


def building_jobs(manifests: Sequence[tuple[uuid.UUID, object]], table: str) -> list[BuildingJob]:
    """Find the jobs that build a table: those whose selection holds it.

    Parameters
    ----------
    manifests
        From `deployed_manifests`; untrusted.
    table
        The dotted table name.

    Returns
    -------
    list of BuildingJob
        By repository, then job name; empty for a table no deployed project builds. A job whose
        patterns cannot be resolved is left out.
    """
    found: list[BuildingJob] = []
    for repository_id, manifest in manifests:
        if not isinstance(manifest, Mapping) or table not in manifest_assets(manifest):
            continue
        graph = ManifestGraph.from_manifest(manifest)
        for name, entry in sorted(manifest_jobs(manifest).items()):
            try:
                selected = resolve_selection(graph, selection_of(entry))
            except ValidationError:
                continue
            if table in selected:
                found.append(
                    BuildingJob(
                        repository_id, name, job_notify(manifest, name), job_schedule(entry)
                    )
                )
    return found


def judged_by_run(
    session: Session, task_run_id: uuid.UUID | None, table: str
) -> frozenset[tuple[str, str | None]]:
    """Say which health signals of a table a task run's blocking health checks judge.

    The checks come from the run's own project version (`health_specs`), as the check phase
    builds them; a task run without checks (`TaskRun.has_checks`, a run started on a version
    without `health:`) judges none, so a signal that fired as ``block`` is still told.

    Parameters
    ----------
    session
        Database session; read without flushing.
    task_run_id
        The writing task run (`RunContext.task_run_id`); `None` for a write outside a run.
    table
        The dotted table name; a task judges only its own asset.

    Returns
    -------
    frozenset of tuple
        (signal, column): the column is `None` for a table signal and for the whole of a column
        signal (`range: block`), which judges every column of it.
    """
    if task_run_id is None:
        return frozenset()
    with session.no_autoflush:
        manifest = session.scalar(
            select(JobVersion.manifest_json)
            .join(Run, Run.job_version_id == JobVersion.id)
            .join(TaskRun, TaskRun.run_id == Run.id)
            .where(
                TaskRun.id == task_run_id,
                TaskRun.asset == table,
                TaskRun.has_checks.is_(True),
            )
        )
    named = [
        spec.generated.columns
        for spec in health_specs(manifest, table)
        if spec.generated is not None and spec.generated.columns
    ]
    return frozenset((names[0], names[1] if len(names) > 1 else None) for names in named)


def rows_to_tell(
    rows: Sequence[TableHealthSignal], *, judged: Collection[tuple[str, str | None]] = ()
) -> list[TableHealthSignal]:
    """Pick the signal rows of a commit that a message tells of.

    Parameters
    ----------
    rows
        The commit's rows.
    judged
        What the writing run's blocking health checks judge (`health_jobs.judged_by_run`):
        such a check fails the run on a fired ``block`` signal, and the run's ``failed``
        message tells of it. Empty outside a run, and for a run whose checks judge none (one
        started on a project version without them).

    Returns
    -------
    list of TableHealthSignal
        Fired rows whose effect is ``warn``, or ``block`` unless `judged` holds the signal and
        column, or the whole signal.
    """
    return [
        row
        for row in rows
        if row.state == "fired"
        and (
            row.effect == "warn"
            or (
                row.effect == "block"
                and (row.signal, row.column_name) not in judged
                and (row.signal, None) not in judged
            )
        )
    ]


def _still_fired(
    session: Session, rows: Sequence[TableHealthSignal]
) -> set[tuple[str, str | None]]:
    """Find the signals whose previous observation was already in the state of these rows.

    A fired row after a fired one, or a ``no_metrics`` row after one, is not told again.

    The previous observation of a key is found as `ranked` orders rows (by sequence number, a
    row without one last), but with one ordered seek per key on the index of
    ``(table_id, signal, sequence_number, observed_at, id)`` instead of a window over the
    table's whole history: this runs in the transaction of every commit that fires.

    Parameters
    ----------
    session
        Database session.
    rows
        Rows to tell of, of one table: fired, or ``no_metrics``.

    Returns
    -------
    set of tuple
        (signal, column) whose newest row before these, set-back rows left out (`NOT_SET_BACK`),
        has the same state as the row of that key.
    """
    table_id = rows[0].table_id
    own = [row.id for row in rows]
    keys = sorted({(row.signal, row.column_name) for row in rows}, key=repr)

    told = {(row.signal, row.column_name): row.state for row in rows}

    def newest(
        signal: str, column: str | None, *, numbered: bool, unjudged: bool
    ) -> ScalarSelect[str]:
        """Select the state of a key's newest earlier row, with or without a sequence number.

        Parameters
        ----------
        signal
            The signal.
        column
            The column; `None` for the whole table.
        numbered
            Whether the rows have a sequence number (a commit's) or not (the sweep's).
        unjudged
            For a ``no_metrics`` row its commit could not judge: the newest earlier row of the
            signal in any column, which counts as the same state only when it is again such a
            table-level row. A judged commit writes per-column rows only, so the previous
            table-level row alone would be the last unjudged one, however old.

        Returns
        -------
        ScalarSelect
            The state, or NULL without such a row.
        """
        number = TableHealthSignal.sequence_number
        state: ColumnElement[str] = TableHealthSignal.state.expression
        if unjudged:
            state = case(
                (
                    and_(
                        TableHealthSignal.column_name.is_(None),
                        TableHealthSignal.state == "no_metrics",
                    ),
                    "no_metrics",
                ),
                else_="judged",
            )
        where = [
            TableHealthSignal.table_id == table_id,
            TableHealthSignal.signal == signal,
            number.is_not(None) if numbered else number.is_(None),
            TableHealthSignal.id.not_in(own),
            NOT_SET_BACK,
        ]
        if not unjudged:
            where.append(
                TableHealthSignal.column_name.is_(None)
                if column is None
                else TableHealthSignal.column_name == column
            )
        return select(state).where(*where).order_by(*NEWEST_COMMIT).limit(1).scalar_subquery()

    states = session.execute(
        select(
            *(
                func.coalesce(
                    newest(signal, column, numbered=True, unjudged=unjudged),
                    newest(signal, column, numbered=False, unjudged=unjudged),
                )
                for signal, column in keys
                for unjudged in (told[signal, column] == "no_metrics",)
            )
        )
    ).one()
    return {key for key, state in zip(keys, states, strict=True) if state == told[key]}


def queue_health_notifications(
    session: Session,
    *,
    environment_id: uuid.UUID,
    table: str,
    rows: Sequence[TableHealthSignal],
    now: datetime,
    manifests: Sequence[tuple[uuid.UUID, object]] | None = None,
) -> list[NotificationDelivery]:
    """Build a message per fired signal and building job that notifies.

    A signal that keeps firing is told once: a row whose previous observation of the same
    signal and column fired too is not told again, until an observation that did not fire.
    Everything is read here, so a commit calls it before its pointer swap and only adds the
    rows after it, with the table's row locked for no longer than that.

    Parameters
    ----------
    session
        Database session; read without flushing, nothing is added or committed.
    environment_id
        The table's environment.
    table
        The dotted table name.
    rows
        The signal rows to tell of; only fired ones are queued. Their ids are set; they may be
        in the database already or still to be added.
    now
        When the first attempt is due.
    manifests
        The environment's current manifests, when the caller has read them; read here
        otherwise.

    Returns
    -------
    list of NotificationDelivery
        The new deliveries, not added to the session: to add after `rows`, for their foreign
        key. None for a table no job with `notify:` builds, and none for a pair already queued.
    """
    # A fired row; or a `no_metrics` row that a blocking check could not judge, which only the
    # check phase passes (`lakana_worker.task_checks._tell_unjudged`).
    fired = [row for row in rows if row.state in ("fired", "no_metrics")]
    if not fired:
        return []
    with session.no_autoflush:
        current = deployed_manifests(session, environment_id) if manifests is None else manifests
        jobs = [
            (job, job.notify) for job in building_jobs(current, table) if job.notify is not None
        ]
        if not jobs:
            return []
        still = _still_fired(session, fired)
        queued = {
            (signal_id, job)
            for signal_id, job in session.execute(
                select(NotificationDelivery.health_signal_id, NotificationDelivery.job).where(
                    NotificationDelivery.health_signal_id.in_([row.id for row in fired])
                )
            )
        }
    added = []
    for row in fired:
        if (row.signal, row.column_name) in still:
            continue
        for job, notify in jobs:
            if (row.id, job.job) in queued:
                continue
            delivery = new_delivery(
                notify,
                HEALTH,
                now,
                run_id=None,
                environment_id=environment_id,
                repository_id=job.repository_id,
                job=job.job,
            )
            delivery.health_signal_id = row.id
            added.append(delivery)
    return added
