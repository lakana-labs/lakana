"""The newest run that failed on a connection, for the Connections screen.

A task's failure names a connection when the asset's code ended with an error of
`lakana.ingest` that names one the asset declares (`TaskRun.failure_connection`, stored by
`lakana_core.runs.tasks.record_attempt`). A connection shows the newest such failed task of its
environment, as long as it is still the newest ending of its asset: a later success, or a later
failure, of the same asset in the same environment and repository means the asset's last run did
not fail on the connection, and the failure is no longer shown.

Only someone who may read the run sees it: the rule of `lakana_core.runs.service.authorize_run_read`
(`RUN` or `READ_ENVIRONMENT` on its environment), asked here without refusing and without an audit
event (`run_readers`).

This module imports only the models and authorization, so the connection service may use it
without an import cycle.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import and_, exists, func, select
from sqlalchemy.orm import Session, aliased

from lakana_core.auth import Action, AuthorizationService, PrincipalRef, Securable, permits_each
from lakana_core.models import Connection, Run, TaskRun, TaskStatus

_ENDINGS = (TaskStatus.SUCCEEDED, TaskStatus.FAILED)
"""The endings of a task that ran its code: a later one says whether the asset still fails."""


@dataclass(frozen=True)
class LastRunFailure:
    """The newest task that failed on a connection and is still its asset's newest ending.

    Parameters
    ----------
    run_id
        The run.
    asset
        The task's asset.
    finished_at
        When the task failed.
    """

    run_id: uuid.UUID
    asset: str
    finished_at: datetime


def run_readers(
    authz: AuthorizationService, p: PrincipalRef, environments: Iterable[str]
) -> set[str]:
    """Find the environments among some whose runs the caller may read.

    The rule of `lakana_core.runs.service.authorize_run_read`, `RUN` or `READ_ENVIRONMENT` on
    the environment, asked in one `permits_each` batch, which refuses nothing and records nothing.

    Parameters
    ----------
    authz
        Authorization service, asked with `permits_each` only.
    p
        The caller.
    environments
        The environments' names; one may repeat.

    Returns
    -------
    set of str
        The names of those whose runs the caller may read.
    """
    names = sorted(set(environments))
    questions = [
        (action, Securable.environment_scope(name))
        for name in names
        for action in (Action.RUN, Action.READ_ENVIRONMENT)
    ]
    answers = permits_each(authz, p, questions) if questions else []
    return {
        name for index, name in enumerate(names) if answers[2 * index] or answers[2 * index + 1]
    }


def last_run_failures(
    session: Session, connections: Sequence[Connection]
) -> dict[uuid.UUID, LastRunFailure]:
    """Read the newest run that failed on each connection, in one statement.

    A failure counts only after the connection's last change (`updated_at`): one before may be
    what the change fixed, and one of a deleted connection of the same name is not this one's.

    Parameters
    ----------
    session
        Database session.
    connections
        The connections; the caller has decided that it may show their runs.

    Returns
    -------
    dict
        Each connection's id to its `LastRunFailure`; a connection with none is absent. Equal
        times go to the larger task id.
    """
    if not connections:
        return {}
    later_task, later_run = aliased(TaskRun), aliased(Run)
    superseded = exists(
        select(later_task.id)
        .join(later_run, later_run.id == later_task.run_id)
        .where(
            later_task.asset == TaskRun.asset,
            later_task.status.in_(_ENDINGS),
            later_task.finished_at > TaskRun.finished_at,
            later_run.environment_id == Run.environment_id,
            later_run.repository_id == Run.repository_id,
        )
    )
    ranked = (
        select(
            Connection.id.label("connection_id"),
            TaskRun.run_id,
            TaskRun.asset,
            TaskRun.finished_at,
            func.row_number()
            .over(
                partition_by=Connection.id,
                order_by=(TaskRun.finished_at.desc(), TaskRun.id.desc()),
            )
            .label("position"),
        )
        .join(Run, Run.id == TaskRun.run_id)
        .join(
            Connection,
            and_(
                Connection.environment_id == Run.environment_id,
                Connection.name == TaskRun.failure_connection,
            ),
        )
        .where(
            Connection.id.in_([c.id for c in connections]),
            TaskRun.status == TaskStatus.FAILED,
            TaskRun.finished_at >= Connection.updated_at,
            ~superseded,
        )
        .subquery()
    )
    rows = session.execute(select(ranked).where(ranked.c.position == 1)).all()
    return {
        row.connection_id: LastRunFailure(
            run_id=row.run_id, asset=row.asset, finished_at=row.finished_at
        )
        for row in rows
    }
