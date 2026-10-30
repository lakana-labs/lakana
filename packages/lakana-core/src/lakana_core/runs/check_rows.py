"""The query that shows a person the rows a check of a run found.

A check result holds a count and the snapshot of the table that was checked, never a row.
To look at the rows, the caller gets an ordinary ad-hoc query run of their own that executes
the check's SQL and is bound to the result (`QueryRun.check_result_id`). The worker reads the
one table the result names at the checked snapshot and every other table as it is now, under
the caller's own grants, row filters and column masks.

This module decides who may ask and what is queued, and is the only place that sets the
binding. Reading the table is `CatalogService.load_table_at_check`.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.auth import Action, AuthorizationService, PrincipalRef, Securable
from lakana_core.errors import (
    CheckNotInRunError,
    CheckSqlGoneError,
    NoCheckedSnapshotError,
    NotFoundError,
    PermissionDenied,
)
from lakana_core.models import (
    CheckOutcome,
    CheckResult,
    JobVersion,
    QueryRun,
    QueryRunStatus,
    Run,
    Table,
    TableType,
)
from lakana_core.projects.checks import (
    check_sql_references,
    may_read_every,
    read_manifest_checks,
)
from lakana_core.runs.checks import (
    CHECK_ROWS_NOT_EVERY_TABLE,
    CHECK_ROWS_NOT_FOR_A_TASK,
    check_not_in_run_message,
    check_sql_gone_message,
    check_sql_unreadable_message,
    no_checked_snapshot_message,
    no_snapshot_could_not_run_message,
    no_snapshot_not_read_message,
    no_snapshot_of_a_view_message,
)
from lakana_core.runs.hidden_checks import ReferenceCheckFilter
from lakana_core.runs.service import authorize_run_read


def _no_snapshot_reason(
    session: Session, result: CheckResult, names: tuple[str, ...] | None, environment: str
) -> str:
    """Word why a result without a checked snapshot has no rows to show.

    Parameters
    ----------
    session
        Database session; read only.
    result
        A check result of the run the caller may read, without a checked snapshot.
    names
        The tables the check's SQL names, as `check_sql_references` read them; `None` when
        the run's project version stores no SQL for the check, or SQL that cannot be read.
        A name that appears says only that: a check that reads its table through a view names
        the view.
    environment
        The environment's name.

    Returns
    -------
    str
        The reason and its next step when the result, the kind of its table or its SQL says
        which it is; else only that no snapshot was checked (a table that had no snapshot,
        an external table, a table read through a view), because a reason that is guessed
        would send the reader to the wrong place.
    """
    if result.outcome is CheckOutcome.ERRORED:
        return no_snapshot_could_not_run_message(result.run_id, environment)
    kind = session.execute(
        select(Table.table_type).where(Table.id == result.table_id)
    ).scalar_one_or_none()
    if kind is TableType.VIEW:
        return no_snapshot_of_a_view_message(result.run_id, result.table_name, result.check_path)
    if names is not None and result.table_name not in names:
        return no_snapshot_not_read_message(result.run_id, result.table_name, result.check_path)
    return no_checked_snapshot_message(result.run_id)


def submit_check_rows(
    session: Session,
    authorization: AuthorizationService,
    p: PrincipalRef,
    *,
    environment: str,
    run_id: uuid.UUID,
    check: str,
) -> QueryRun:
    """Queue the caller's own query that shows the rows a check of a run found.

    Reading the run is authorized first and submitting a query second; a caller without both
    is refused before a run, a result or a project version is looked at, so the refusal is
    the same whether or not any of them exists. The result is looked for among the results
    of that run only: holding the id or the name of another run's check gives nothing.

    The query's text is the check's SQL, so a caller who may not read every table that SQL
    names is refused next, before any reason derived from the SQL. Reading the tables is
    authorized again by the worker when it runs the query, as for every query, which also
    applies the caller's row filters and column masks.

    Parameters
    ----------
    session
        Database session; commits.
    authorization
        Authorization service.
    p
        The caller: the query run is theirs.
    environment
        Environment name.
    run_id
        The run whose check is asked about.
    check
        The check's name, ``<table>/<check>``, as `lakana check list` shows it.

    Returns
    -------
    QueryRun
        The inserted row: `QUEUED`, submitted by the caller, without a task run, with the
        check's SQL as its text and the result's id as `check_result_id`. A check that passed
        is queued like any other; its query returns no rows.

    Raises
    ------
    PermissionDenied
        Without RUN or READ_ENVIRONMENT on the environment; then without SUBMIT_QUERY; then
        for a task (a run context, or a task token, bound to a task or not): a blocking check keeps
        failing rows from everything downstream, so no pipeline is shown them; then, once the
        check is found, for a caller who may not read every table its SQL names.
    NotFoundError
        If the environment does not exist, or the run does not exist in it.
    CheckNotInRunError
        If the run has no result for the check.
    NoCheckedSnapshotError
        If the result names no snapshot: there are no rows to read.
    CheckSqlGoneError
        If the manifest of the run's project version holds no readable entry for the check,
        or, with its own text, SQL whose tables cannot be listed.
    """
    env = authorize_run_read(session, authorization, p, environment)
    authorization.authorize(p, Action.SUBMIT_QUERY, Securable.environment_scope(environment))
    # Any task-kind token is a task: one bound to no task has no run context, and is still
    # meant for automation, not for a person's reads.
    if p.run_context is not None or p.task_scope is not None:
        raise PermissionDenied(CHECK_ROWS_NOT_FOR_A_TASK)
    run = session.execute(
        select(Run).where(Run.id == run_id, Run.environment_id == env.id)
    ).scalar_one_or_none()
    if run is None:
        raise NotFoundError(f"No run {run_id} in {env.name}.")
    # A run normally holds one result per check name. Should it hold two (the name is unique
    # per task run, not per run), the one that ended last is the run's verdict on the check.
    result = session.execute(
        select(CheckResult)
        .where(CheckResult.run_id == run.id, CheckResult.check_name == check)
        .order_by(CheckResult.finished_at.desc(), CheckResult.id)
        .limit(1)
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    # A generated reference check whose target the caller may not read is not shown to them
    # anywhere, so asking for its rows is answered as for a check the run does not have.
    if result is None or ReferenceCheckFilter(session, authorization, p, env.name).hides(
        result.run_id, result.check_name, result.generated
    ):
        raise CheckNotInRunError(check_not_in_run_message(run.id, check, env.name))
    # The foreign key from a run to its project version removes the run with the version, so
    # a run that is found has one; a manifest that cannot be read holds no checks.
    manifest = session.execute(
        select(JobVersion.manifest_json).where(JobVersion.id == run.job_version_id)
    ).scalar_one_or_none()
    spec = next(
        (s for s in read_manifest_checks(manifest).specs if s.name == result.check_name), None
    )
    names = None if spec is None else check_sql_references(spec.sql)
    # SQL whose tables cannot be listed cannot be checked against the caller's grants: it is
    # never queued, and no reason is derived from it.
    if names is not None and not may_read_every(authorization, p, env.name, names):
        raise PermissionDenied(CHECK_ROWS_NOT_EVERY_TABLE)
    if result.checked_snapshot_id is None:
        raise NoCheckedSnapshotError(_no_snapshot_reason(session, result, names, env.name))
    if spec is None:
        raise CheckSqlGoneError(check_sql_gone_message(run.id, env.name))
    if names is None:
        raise CheckSqlGoneError(check_sql_unreadable_message(run.id, env.name))
    query = QueryRun(
        id=uuid.uuid4(),
        environment_id=env.id,
        principal_id=p.id,
        sql_text=spec.sql,
        status=QueryRunStatus.QUEUED,
        task_token_id=None,
        check_result_id=result.id,
    )
    session.add(query)
    session.commit()
    return query
