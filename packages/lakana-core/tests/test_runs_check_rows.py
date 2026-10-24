"""Submitting the query that shows the rows a check found.

`submit_check_rows` decides who may ask and what is queued; the rows themselves are read by
the worker, in the worker's tests.
"""

from __future__ import annotations

import ast
import inspect
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from core_test_helpers import ActionAllowList, python_task_run
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from lakana_core.auth import (
    Action,
    AllowAllAuthorization,
    BoundTask,
    PrincipalRef,
    RunContext,
    Securable,
    TaskScope,
)
from lakana_core.catalog.query_service import QueryService
from lakana_core.catalog.service import CatalogService
from lakana_core.config import StorageConfig
from lakana_core.errors import (
    CheckNotInRunError,
    CheckSqlGoneError,
    ConflictError,
    NoCheckedSnapshotError,
    NotFoundError,
    PermissionDenied,
)
from lakana_core.models import (
    CheckOutcome,
    CheckResult,
    JobVersion,
    Namespace,
    QueryRun,
    QueryRunStatus,
    Run,
    Table,
    TableType,
)
from lakana_core.projects.checks import CheckSpec
from lakana_core.runs import pools
from lakana_core.runs.check_rows import submit_check_rows
from lakana_core.runs.checks import (
    CHECK_RESULT_REMOVED,
    CHECK_ROWS_NOT_EVERY_TABLE,
    CHECK_ROWS_NOT_FOR_A_TASK,
    COLUMN_NOT_SHOWN,
    NO_COLUMN_LEFT,
    POLICY_NEEDS_A_COLUMN,
    column_not_shown_message,
    no_column_left_message,
    policy_needs_a_column_message,
    record_result,
)
from lakana_core.runs.store import is_check_count
from lakana_core.storage.resolver import StorageResolver

_TABLE = "gold.orders"
_CHECK = f"{_TABLE}/unique_id"
_PATH = "checks/gold/orders/unique_id.sql"
_SQL = "SELECT id FROM gold.orders GROUP BY id HAVING count(*) > 1"
_SNAPSHOT = 4242
_RUN = uuid.UUID("6f1c2a52-3b8e-4d0a-9a55-0c1f5f1f7a10")
_SOURCES = Path(__file__).resolve().parents[2]
"""The directory that holds the four packages."""


@pytest.fixture
def dev(session: Session, principal: PrincipalRef, tmp_path: Path) -> uuid.UUID:
    """Provide the environment `dev` and return its id.

    Parameters
    ----------
    session
        Database session.
    principal
        The principal that creates it.
    tmp_path
        The environment's storage root.

    Returns
    -------
    uuid.UUID
        The environment's id.
    """
    catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
    env = catalog.create_environment(
        principal,
        name="dev",
        storage=StorageConfig(scheme="file", root=f"file://{tmp_path}", options={}),
    )
    return env.id


def _table_row(session: Session, environment_id: uuid.UUID, kind: TableType) -> uuid.UUID:
    """Insert a catalog row named ``gold.orders`` of a kind and return its id.

    Parameters
    ----------
    session
        Database session; commits.
    environment_id
        The environment.
    kind
        The row's kind.

    Returns
    -------
    uuid.UUID
        The row's id.
    """
    namespace_id = session.execute(
        select(Namespace.id).where(
            Namespace.environment_id == environment_id, Namespace.path == "gold"
        )
    ).scalar_one()
    row = Table(id=uuid.uuid4(), namespace_id=namespace_id, name="orders", table_type=kind)
    session.add(row)
    session.commit()
    return row.id


def _checked(
    session: Session,
    principal: PrincipalRef,
    *,
    snapshot_id: int | None = _SNAPSHOT,
    table_id: uuid.UUID | None = None,
    outcome: CheckOutcome = CheckOutcome.FAILED,
    sql: str | None = _SQL,
) -> CheckResult:
    """Record the result of ``gold.orders/unique_id`` for a new run, as the worker does.

    Parameters
    ----------
    session
        Database session; commits.
    principal
        The run's principal.
    snapshot_id
        The snapshot the check read.
    table_id
        The id of the checked table, as the worker resolved it; a new one when `None`.
    outcome
        How the check ended.
    sql
        The check's SQL in the manifest of the run's project version; `None` leaves the check out
        of the manifest.

    Returns
    -------
    CheckResult
        The stored result.
    """
    task = python_task_run(session, principal.id, asset=_TABLE, kind="sql")
    task.has_checks = True
    spec = CheckSpec(
        name=_CHECK,
        table=_TABLE,
        check="unique_id",
        path=_PATH,
        on_failure="block",
        description=None,
        sql=_SQL if sql is None else sql,
    )
    version_id = session.execute(
        select(Run.job_version_id).where(Run.id == task.run_id)
    ).scalar_one()
    version = session.get(JobVersion, version_id)
    assert version is not None
    entry = {
        "name": spec.name,
        "table": spec.table,
        "check": spec.check,
        "path": spec.path,
        "on_failure": spec.on_failure,
        "sql": spec.sql,
    }
    version.manifest_json = {**version.manifest_json, "checks": [] if sql is None else [entry]}
    session.commit()
    assert record_result(
        session,
        task_run_id=task.id,
        worker="w1",
        claim=task.claim_count,
        spec=spec,
        outcome=outcome,
        failing_rows=None if outcome is CheckOutcome.ERRORED else 2,
        error="the check's query failed" if outcome is CheckOutcome.ERRORED else None,
        table_id=uuid.uuid4() if table_id is None else table_id,
        checked_snapshot_id=snapshot_id,
        policy_applied=False,
        query_run_id=None,
        started_at=datetime(2026, 10, 12, 9, 0, tzinfo=UTC),
    )
    return session.scalars(select(CheckResult).where(CheckResult.task_run_id == task.id)).one()


def _queued(session: Session) -> int:
    """Count the query runs that exist.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    int
        How many rows `query_runs` holds.
    """
    session.rollback()
    return session.execute(select(func.count()).select_from(QueryRun)).scalar_one()


def _submit(
    session: Session,
    principal: PrincipalRef,
    result: CheckResult,
    *,
    check: str = _CHECK,
) -> QueryRun:
    """Ask for the rows of a check of the result's run, with every right.

    Parameters
    ----------
    session
        Database session.
    principal
        The caller.
    result
        A result of the run to ask about.
    check
        The check's name.

    Returns
    -------
    QueryRun
        The query run.
    """
    return submit_check_rows(
        session,
        AllowAllAuthorization(),
        principal,
        environment="dev",
        run_id=result.run_id,
        check=check,
    )


# --- who may ask ---


@pytest.mark.parametrize(
    ("allowed", "refused", "asked"),
    [
        ((), "may not READ_ENVIRONMENT", [Action.READ_ENVIRONMENT]),
        ((Action.SUBMIT_QUERY,), "may not READ_ENVIRONMENT", [Action.READ_ENVIRONMENT]),
        (
            (Action.READ_ENVIRONMENT,),
            "may not SUBMIT_QUERY",
            [Action.READ_ENVIRONMENT, Action.SUBMIT_QUERY],
        ),
        ((Action.RUN,), "may not SUBMIT_QUERY", [Action.RUN, Action.SUBMIT_QUERY]),
    ],
)
def test_the_run_is_authorized_first_and_the_query_second(
    session: Session,
    principal: PrincipalRef,
    dev: uuid.UUID,
    allowed: tuple[Action, ...],
    refused: str,
    asked: list[Action],
) -> None:
    """Each missing right gives its own refusal: reading the run first, then submitting.

    Parameters
    ----------
    session
        Database session.
    principal
        The caller.
    dev
        The environment.
    allowed
        What the caller may do.
    refused
        The refusal's text.
    asked
        The actions that were asked for, in order.
    """
    result = _checked(session, principal)
    authz = ActionAllowList(*allowed)

    with pytest.raises(PermissionDenied) as denied:
        submit_check_rows(
            session, authz, principal, environment="dev", run_id=result.run_id, check=_CHECK
        )

    assert str(denied.value) == refused
    assert authz.seen == [(action, Securable.environment_scope("dev")) for action in asked]
    assert _queued(session) == 0


@pytest.mark.parametrize("allowed", [(), (Action.RUN,), (Action.SUBMIT_QUERY,)])
def test_a_caller_without_both_rights_learns_nothing_of_a_run_or_a_check(
    session: Session, principal: PrincipalRef, dev: uuid.UUID, allowed: tuple[Action, ...]
) -> None:
    """The refusal is the same for a run and a check that exist and for ones that do not.

    Parameters
    ----------
    session
        Database session.
    principal
        The caller.
    dev
        The environment.
    allowed
        What the caller may do: never both rights.
    """
    result = _checked(session, principal)
    answers = set()
    for run_id, check in (
        (result.run_id, _CHECK),
        (result.run_id, "gold.orders/no_such"),
        (uuid.UUID(int=7), _CHECK),
    ):
        with pytest.raises(PermissionDenied) as denied:
            submit_check_rows(
                session,
                ActionAllowList(*allowed),
                principal,
                environment="dev",
                run_id=run_id,
                check=check,
            )
        answers.add(str(denied.value))

    assert len(answers) == 1


def test_a_task_is_refused_whatever_its_principal_may_do(
    session: Session, principal: PrincipalRef, dev: uuid.UUID
) -> None:
    """A principal with a run context, or a token bound to a task, gets no such query."""
    result = _checked(session, principal)
    as_task = PrincipalRef(
        id=principal.id,
        name=principal.name,
        type=principal.type,
        run_context=RunContext(task_run_id=result.task_run_id, claim=1),
    )
    with_bound_token = PrincipalRef(
        id=principal.id,
        name=principal.name,
        type=principal.type,
        task_scope=TaskScope(
            environment="dev",
            asset_scope=[],
            token_id=uuid.uuid4(),
            bound=BoundTask(
                task_run_id=result.task_run_id,
                claim=1,
                asset=_TABLE,
                inputs=(),
                readable=(),
            ),
        ),
    )

    for caller in (as_task, with_bound_token):
        with pytest.raises(PermissionDenied) as denied:
            _submit(session, caller, result)
        assert str(denied.value) == CHECK_ROWS_NOT_FOR_A_TASK

    assert _queued(session) == 0


def test_an_unbound_task_token_is_refused_like_a_pipeline_task(
    session: Session, principal: PrincipalRef, dev: uuid.UUID
) -> None:
    """A task-kind token made by hand, bound to no task, has no run context and is still a task."""
    result = _checked(session, principal)
    caller = PrincipalRef(
        id=principal.id,
        name=principal.name,
        type=principal.type,
        task_scope=TaskScope(environment="dev", asset_scope=[], token_id=uuid.uuid4()),
    )
    assert caller.run_context is None

    with pytest.raises(PermissionDenied) as denied:
        _submit(session, caller, result)

    assert str(denied.value) == CHECK_ROWS_NOT_FOR_A_TASK
    assert _queued(session) == 0


# --- what is found ---


def test_a_run_of_another_environment_is_no_run_here(
    session: Session, principal: PrincipalRef, dev: uuid.UUID
) -> None:
    """The run is looked for in the named environment only."""
    result = _checked(session, principal)
    CatalogService(session, StorageResolver(session), AllowAllAuthorization()).create_environment(
        principal,
        name="prod",
        storage=StorageConfig(scheme="file", root="file:///nowhere", options={}),
    )

    with pytest.raises(NotFoundError) as missing:
        submit_check_rows(
            session,
            AllowAllAuthorization(),
            principal,
            environment="prod",
            run_id=result.run_id,
            check=_CHECK,
        )

    assert str(missing.value) == f"No run {result.run_id} in prod."
    assert _queued(session) == 0


def test_a_check_the_run_has_no_result_for_is_named_with_the_next_step(
    session: Session, principal: PrincipalRef, dev: uuid.UUID
) -> None:
    """A check that is not among the run's results is not found, also when another run has it."""
    result = _checked(session, principal)
    other = _checked(session, principal)
    session.execute(
        update(CheckResult)
        .where(CheckResult.id == other.id)
        .values(check_name="gold.orders/only_there")
    )
    session.commit()

    for name in ("gold.orders/no_such", "gold.orders/only_there"):
        with pytest.raises(CheckNotInRunError) as missing:
            _submit(session, principal, result, check=name)
        assert str(missing.value) == (
            f"Run {result.run_id} has no result for the check `{name}`. Its checks: "
            f"`lakana check list --run {result.run_id} --env dev`."
        )

    assert isinstance(missing.value, NotFoundError)
    assert _queued(session) == 0


def test_a_check_name_is_escaped_in_the_message(
    session: Session, principal: PrincipalRef, dev: uuid.UUID
) -> None:
    """A name the caller sent cannot put a control character into the text."""
    result = _checked(session, principal)

    with pytest.raises(CheckNotInRunError) as missing:
        _submit(session, principal, result, check="a`b\x1b[31m")

    assert str(missing.value).startswith(
        f"Run {result.run_id} has no result for the check `a`b\\x1b[31m`."
    )


def test_a_check_that_could_not_run_has_no_rows_and_says_where_its_error_is(
    session: Session, principal: PrincipalRef, dev: uuid.UUID
) -> None:
    """An errored check counted nothing; its error is in the list of the run's checks."""
    result = _checked(session, principal, snapshot_id=None, outcome=CheckOutcome.ERRORED)

    with pytest.raises(NoCheckedSnapshotError) as nothing:
        _submit(session, principal, result)

    assert str(nothing.value) == (
        f"This check has no rows to show from run {result.run_id}: the check could not run. "
        f"Its error is in `lakana check list --run {result.run_id} --env dev`."
    )
    assert _queued(session) == 0


def test_a_check_of_a_view_has_no_rows_and_names_its_file(
    session: Session, principal: PrincipalRef, dev: uuid.UUID
) -> None:
    """A view has no snapshots, so nothing was kept to read the rows from."""
    view_id = _table_row(session, dev, TableType.VIEW)
    result = _checked(session, principal, snapshot_id=None, table_id=view_id)

    with pytest.raises(NoCheckedSnapshotError) as nothing:
        _submit(session, principal, result)

    assert str(nothing.value) == (
        f"This check has no rows to show from run {result.run_id}: `gold.orders` is a view, so "
        "no snapshot was checked. Run the check's SQL yourself: "
        "`checks/gold/orders/unique_id.sql` in the project."
    )
    assert _queued(session) == 0


def test_a_check_whose_sql_does_not_read_its_table_has_no_rows_and_names_its_file(
    session: Session, principal: PrincipalRef, dev: uuid.UUID
) -> None:
    """A check that reads only other tables checked no snapshot of its own."""
    table_id = _table_row(session, dev, TableType.ICEBERG)
    result = _checked(
        session,
        principal,
        snapshot_id=None,
        table_id=table_id,
        sql="SELECT id FROM silver.payments WHERE id IS NULL",
    )

    with pytest.raises(NoCheckedSnapshotError) as nothing:
        _submit(session, principal, result)

    assert str(nothing.value) == (
        f"This check has no rows to show from run {result.run_id}: its SQL does not read "
        "`gold.orders`, so no snapshot of it was checked. Run the check's SQL yourself: "
        "`checks/gold/orders/unique_id.sql` in the project."
    )
    assert _queued(session) == 0


@pytest.mark.parametrize(
    "sql",
    [
        _SQL,
        None,
        "SELECT FROM WHERE",
    ],
    ids=["reads-the-table", "not-in-the-manifest", "not-sql"],
)
def test_a_result_without_a_snapshot_for_no_reason_that_is_known_claims_none(
    session: Session, principal: PrincipalRef, dev: uuid.UUID, sql: str | None
) -> None:
    """A table without a snapshot, or a check whose SQL is not known, gets no invented reason.

    Parameters
    ----------
    session
        Database session.
    principal
        The caller.
    dev
        The environment.
    sql
        The check's SQL in the manifest; `None` leaves the check out.
    """
    table_id = _table_row(session, dev, TableType.ICEBERG)
    result = _checked(session, principal, snapshot_id=None, table_id=table_id, sql=sql)

    with pytest.raises(NoCheckedSnapshotError) as nothing:
        _submit(session, principal, result)

    assert str(nothing.value) == (
        f"This check has no rows to show from run {result.run_id}: no snapshot of the table was "
        "checked."
    )
    assert _queued(session) == 0


@pytest.mark.parametrize("damage", ["no-entry", "no-manifest"])
def test_a_project_version_without_the_check_cannot_say_its_sql(
    session: Session, principal: PrincipalRef, dev: uuid.UUID, damage: str
) -> None:
    """The check's SQL is read from the run's project version; without it nothing is queued.

    Parameters
    ----------
    session
        Database session.
    principal
        The caller.
    dev
        The environment.
    damage
        What is wrong with the stored manifest.
    """
    result = _checked(session, principal, sql=None)
    if damage == "no-manifest":
        session.execute(update(JobVersion).values(manifest_json=[]))
        session.commit()

    with pytest.raises(CheckSqlGoneError) as gone:
        _submit(session, principal, result)

    assert str(gone.value) == (
        f"The project version of run {result.run_id} is no longer stored, so the check's SQL is "
        f"not known. The number of rows remains: `lakana check list --run {result.run_id} "
        "--env dev`."
    )
    assert isinstance(gone.value, ConflictError)
    assert _queued(session) == 0


# --- the tables the check reads ---


class _ReadsOnly:
    """Permits every action, but READ_TABLE only on some tables.

    Parameters
    ----------
    *tables
        Dotted names of the tables the caller may read.
    """

    def __init__(self, *tables: str) -> None:
        self.tables = frozenset(tables)

    def permits(self, principal: PrincipalRef, action: Action, securable: Securable) -> bool:
        """Answer whether the action is allowed.

        Parameters
        ----------
        principal
            The caller.
        action
            The action.
        securable
            What it is about.

        Returns
        -------
        bool
            `False` only for READ_TABLE on a table not listed.
        """
        if action is not Action.READ_TABLE:
            return True
        namespace = securable.namespace
        dotted = ".".join([*(namespace.parts if namespace else ()), securable.table or ""])
        return dotted in self.tables

    def authorize(self, principal: PrincipalRef, action: Action, securable: Securable) -> None:
        """Refuse what `permits` refuses.

        Parameters
        ----------
        principal
            The caller.
        action
            The action.
        securable
            What it is about.

        Raises
        ------
        PermissionDenied
            If `permits` says no.
        """
        if not self.permits(principal, action, securable):
            raise PermissionDenied(f"may not {action.value}")


_JOINED = "SELECT o.id FROM gold.orders o JOIN hr.salaries s ON o.id = s.id"
_ELSEWHERE = "SELECT id FROM hr.salaries WHERE id IS NULL"


def _refused(session: Session, principal: PrincipalRef, result: CheckResult) -> str:
    """Ask for the rows as a caller who may read `gold.orders` only, and return the refusal.

    Parameters
    ----------
    session
        Database session.
    principal
        The caller.
    result
        A result of the run to ask about.

    Returns
    -------
    str
        The text of the `PermissionDenied`.
    """
    with pytest.raises(PermissionDenied) as denied:
        submit_check_rows(
            session,
            _ReadsOnly(_TABLE),
            principal,
            environment="dev",
            run_id=result.run_id,
            check=_CHECK,
        )
    return str(denied.value)


def test_a_caller_who_may_not_read_every_table_of_the_check_is_refused(
    session: Session, principal: PrincipalRef, dev: uuid.UUID
) -> None:
    """The query would hand the caller the check's SQL, which names a table they may not read."""
    result = _checked(session, principal, sql=_JOINED)

    message = _refused(session, principal, result)

    assert message == CHECK_ROWS_NOT_EVERY_TABLE
    assert _queued(session) == 0


def test_the_refusal_comes_before_any_reason_taken_from_the_sql(
    session: Session, principal: PrincipalRef, dev: uuid.UUID
) -> None:
    """Without a snapshot, "does not read its table" would tell the caller about the SQL."""
    table_id = _table_row(session, dev, TableType.ICEBERG)
    result = _checked(session, principal, snapshot_id=None, table_id=table_id, sql=_ELSEWHERE)

    assert _refused(session, principal, result) == CHECK_ROWS_NOT_EVERY_TABLE


def test_the_refusal_names_no_table_of_the_check_and_no_number(
    session: Session, principal: PrincipalRef, dev: uuid.UUID
) -> None:
    result = _checked(session, principal, sql=_JOINED)

    message = _refused(session, principal, result)

    for word in ("hr", "salaries", "gold", "orders", "1", "one"):
        assert word not in message.lower().replace(".", " ").split()
    assert "READ" in message
    assert "project repository" in message


def test_a_caller_who_may_read_every_table_of_the_check_gets_the_query(
    session: Session, principal: PrincipalRef, dev: uuid.UUID
) -> None:
    result = _checked(session, principal, sql=_JOINED)

    run = submit_check_rows(
        session,
        _ReadsOnly(_TABLE, "hr.salaries"),
        principal,
        environment="dev",
        run_id=result.run_id,
        check=_CHECK,
    )

    assert run.sql_text == _JOINED


def test_sql_whose_tables_cannot_be_listed_is_not_known(
    session: Session, principal: PrincipalRef, dev: uuid.UUID
) -> None:
    """A stored manifest is untrusted: SQL that does not parse is never handed to anyone."""
    result = _checked(session, principal, sql="SELECT * FROM hr.salaries WHERE (")

    with pytest.raises(CheckSqlGoneError) as unreadable:
        _submit(session, principal, result)

    assert str(unreadable.value) == (
        f"The SQL of this check stored with run {result.run_id} cannot be read, so its rows "
        "cannot be shown. Deploy the project again (`lakana deploy --env dev`) and run it; the "
        f"number of rows remains: `lakana check list --run {result.run_id} --env dev`."
    )
    assert "salaries" not in str(unreadable.value)
    assert _queued(session) == 0


# --- what is queued ---


def test_the_query_run_is_the_callers_own_and_carries_the_check(
    session: Session, principal: PrincipalRef, dev: uuid.UUID
) -> None:
    """Queued, submitted by the caller, without a task run, with the check's SQL and result."""
    result = _checked(session, principal)

    run = _submit(session, principal, result)

    session.rollback()
    stored = session.execute(
        select(QueryRun).where(QueryRun.id == run.id).execution_options(populate_existing=True)
    ).scalar_one()
    assert stored.status is QueryRunStatus.QUEUED
    assert stored.environment_id == dev
    assert stored.principal_id == principal.id
    assert stored.sql_text == _SQL
    assert stored.check_result_id == result.id
    assert (stored.task_run_id, stored.task_token_id) == (None, None)
    assert (stored.write_mode, stored.target_name, stored.worker_id) == (None, None, None)


def test_a_passed_check_is_allowed_and_queues_its_query(
    session: Session, principal: PrincipalRef, dev: uuid.UUID
) -> None:
    """A check that passed has a snapshot too; its query shows no rows."""
    result = _checked(session, principal, outcome=CheckOutcome.PASSED)

    run = _submit(session, principal, result)

    assert run.check_result_id == result.id


def test_the_newest_result_of_a_check_that_a_run_holds_twice_is_the_one_read(
    session: Session, principal: PrincipalRef, dev: uuid.UUID
) -> None:
    """Two task runs of one run with a result of the same name: the later one is bound."""
    first = _checked(session, principal)
    second = _checked(session, principal)
    session.execute(
        update(CheckResult)
        .where(CheckResult.id == second.id)
        .values(run_id=first.run_id, finished_at=datetime(2030, 1, 1, tzinfo=UTC))
    )
    session.commit()

    run = _submit(session, principal, first)

    assert run.check_result_id == second.id


def test_the_query_run_is_ad_hoc_work_and_no_count_of_a_task(
    session: Session, principal: PrincipalRef, dev: uuid.UUID
) -> None:
    """It waits in the interactive pool and nothing that tells a task's count by its run fits."""
    result = _checked(session, principal)

    run = _submit(session, principal, result)

    assert pools.kind_of(run.task_run_id, run.write_mode) == pools.INTERACTIVE
    interactive = session.execute(select(QueryRun.id).where(pools.is_interactive())).scalars().all()
    pipeline = session.execute(select(QueryRun.id).where(pools.is_pipeline())).scalars().all()
    assert (interactive, pipeline) == ([run.id], [])
    # What the sweep and the cancel route ask before `is_check_count`: a run without a task
    # run never reaches it.
    assert not (run.task_run_id is not None and run.task_token_id is None)
    assert is_check_count(session, run) is True  # hence the guard above is what decides


def test_a_run_with_a_target_is_no_count_even_without_a_write_mode(session: Session) -> None:
    """The one definition of a count, also the refusal of a kept table rests on: no target."""
    unbound = QueryRun(id=uuid.uuid4(), task_run_id=uuid.uuid4(), write_mode=None)
    targeted = QueryRun(
        id=uuid.uuid4(), task_run_id=uuid.uuid4(), write_mode=None, target_name="silver.t"
    )

    assert is_check_count(session, unbound) is True
    assert is_check_count(session, targeted) is False


# --- nothing else binds a query run to a check result ---


def _constructions(name: str) -> dict[str, list[bool]]:
    """Find every place in core, server and worker that constructs a class of this name.

    Parameters
    ----------
    name
        The class's name.

    Returns
    -------
    dict
        Source file (relative to the packages) to, for each construction in it, whether
        `check_result_id` is among its keywords. A construction with ``**`` keywords fails
        the test: its fields could not be read.
    """
    found: dict[str, list[bool]] = {}
    for package in ("lakana-core", "lakana-server", "lakana-worker"):
        for path in sorted((_SOURCES / package / "src").rglob("*.py")):
            for node in ast.walk(ast.parse(path.read_text())):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
                    continue
                if node.func.id != name:
                    continue
                assert all(keyword.arg is not None for keyword in node.keywords), path
                found.setdefault(str(path.relative_to(_SOURCES)), []).append(
                    any(keyword.arg == "check_result_id" for keyword in node.keywords)
                )
    return found


def _mentions(source: str, path: str = "") -> set[str]:
    """Find the ways a piece of source code names `check_result_id` outside a construction.

    Parameters
    ----------
    source
        Python source.
    path
        The file's path relative to the packages; the audit key is exempt only in the run
        store.

    Returns
    -------
    set of str
        ``store`` for an attribute assignment; ``keyword`` for a keyword of that name passed
        to anything but the construction of a query run (``.values(check_result_id=...)``
        included); ``key`` for a dictionary keyed by an attribute of that name
        (``.values({QueryRun.check_result_id: ...})``); ``text`` for a string that contains
        the name and is not a docstring (raw SQL, `setattr`, a request's field alias);
        the key of the audit detail (``detail["check_result_id"] = ...``) is left out.
    """
    tree = ast.parse(source)
    docstrings = {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
    }
    # The one allowed string: the key under which an audit event's detail names the field.
    audit_keys = {
        id(node.slice)
        for node in ast.walk(tree)
        if path == _STORE
        and isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Name)
        and node.value.id == "detail"
        and isinstance(node.ctx, ast.Store)
        and isinstance(node.slice, ast.Constant)
        and node.slice.value == "check_result_id"
    }
    found: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "check_result_id"
            and isinstance(node.ctx, ast.Store)
        ):
            found.add("store")
        if (
            isinstance(node, ast.Call)
            and not (isinstance(node.func, ast.Name) and node.func.id == "QueryRun")
            and any(keyword.arg == "check_result_id" for keyword in node.keywords)
        ):
            found.add("keyword")
        if isinstance(node, ast.Dict) and any(
            isinstance(key, ast.Attribute) and key.attr == "check_result_id" for key in node.keys
        ):
            found.add("key")
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and "check_result_id" in node.value
            and id(node) not in docstrings
            and id(node) not in audit_keys
        ):
            found.add("text")
    return found


def _other_mentions() -> dict[str, set[str]]:
    """Find the files of core, server and worker that name `check_result_id` in a statement.

    Returns
    -------
    dict
        File (relative to the packages) to what `_mentions` found in it; files with nothing
        are left out. Migrations are not read: one adds the column.
    """
    found: dict[str, set[str]] = {}
    for package in ("lakana-core", "lakana-server", "lakana-worker"):
        for path in sorted((_SOURCES / package / "src").rglob("*.py")):
            if "migrations" in path.parts:
                continue
            mentions = _mentions(path.read_text(), str(path.relative_to(_SOURCES)))
            if mentions:
                found[str(path.relative_to(_SOURCES))] = mentions
    return found


@pytest.mark.slow
def test_only_the_rows_of_a_check_bind_a_query_run_to_a_check_result() -> None:
    """Every construction of a query run is listed, and one of them sets the binding.

    A query run that names a check result reads a table as it was, also when the table is
    not published. A new place that constructs a query run fails here until it is listed,
    and one that sets the field must be argued for.
    """
    assert _constructions("QueryRun") == {
        "lakana-core/src/lakana_core/catalog/query_service.py": [False, False, False],
        "lakana-core/src/lakana_core/runs/check_rows.py": [True],
        "lakana-worker/src/lakana_worker/checks.py": [False],
        "lakana-worker/src/lakana_worker/tasks.py": [False],
    }


@pytest.mark.slow
def test_nothing_sets_the_binding_on_a_query_run_that_exists() -> None:
    """No assignment, update, raw SQL or field of a request names the binding.

    One file is listed, and it only reads the field: the worker's query path hands the id of
    the run it executes on to its source resolution and to the catalog under a keyword of that
    name. The run store also names the key of that name in the detail of a query run's audit
    event (`query_completed_detail`), whose value it reads from the run; the walk exempts that
    one subscript of ``detail`` and nothing else, so any other string in that file is found. The
    audit log's list of field names (`lakana_core.audit.content.SHOWN_FIELDS`) names the key as a
    string too; it sets nothing.
    """
    assert _other_mentions() == {
        "lakana-core/src/lakana_core/audit/content.py": {"text"},
        "lakana-worker/src/lakana_worker/pipeline.py": {"keyword"},
    }


@pytest.mark.parametrize(
    ("source", "found"),
    [
        ("run.check_result_id = result.id\n", {"store"}),
        ("session.execute(update(QueryRun).values(check_result_id=result.id))\n", {"keyword"}),
        (
            "session.execute(update(QueryRun).values({QueryRun.check_result_id: result.id}))\n",
            {"key"},
        ),
        ('session.execute(text("UPDATE query_runs SET check_result_id = :id"))\n', {"text"}),
        ('setattr(run, "check_result_id", result.id)\n', {"text"}),
        ('session.execute(text(f"UPDATE query_runs SET check_result_id = {x}"))\n', {"text"}),
        ("QueryRun(id=1, check_result_id=result.id)\n", set()),
        ('values["check_result_id"] = result.id\n', {"text"}),
        ('session.execute(update(QueryRun).values({"check_result_id": result.id}))\n', {"text"}),
        ('detail["x"] = "UPDATE query_runs SET check_result_id = 1"\n', {"text"}),
        ('def f():\n    """Reads `check_result_id`."""\n    return run.check_result_id\n', set()),
    ],
    ids=[
        "assignment",
        "values-keyword",
        "values-dict",
        "raw-sql",
        "setattr",
        "f-string-sql",
        "construction",
        "another-string-key",
        "string-keyed-values",
        "raw-sql-beside-the-key",
        "a-read-and-a-docstring",
    ],
)
def test_every_way_to_set_the_binding_is_found(source: str, found: set[str]) -> None:
    """The guard has teeth: each way to write the field is reported, a read is not.

    Parameters
    ----------
    source
        A statement.
    found
        What the walk must report for it.
    """
    assert _mentions(source) == found


_AUDIT_KEY = 'detail["check_result_id"] = str(run.check_result_id)\n'
_STORE = "lakana-core/src/lakana_core/runs/store.py"


def test_the_audit_key_is_exempt_in_the_run_store_only() -> None:
    """A local `detail` in another file may be filled and written to a query run."""
    assert _mentions(_AUDIT_KEY, _STORE) == set()
    assert _mentions(_AUDIT_KEY, "lakana-worker/src/lakana_worker/pipeline.py") == {"text"}
    assert _mentions(_AUDIT_KEY) == {"text"}


def test_an_ordinary_query_cannot_name_a_check_result(
    session: Session, principal: PrincipalRef, dev: uuid.UUID
) -> None:
    """`submit_query` has no parameter for it and leaves the field empty."""
    service = QueryService(session, AllowAllAuthorization())

    run = service.submit_query(principal, "dev", "SELECT 1")

    assert run.check_result_id is None
    assert list(inspect.signature(QueryService.submit_query).parameters) == [
        "self",
        "p",
        "environment",
        "sql",
    ]


def test_the_texts_the_worker_gives_read_as_written() -> None:
    """The two texts of the worker's own, verbatim, with the names escaped."""
    assert CHECK_RESULT_REMOVED == (
        "The check result this query reads is no longer there: it was recorded again by a new "
        "attempt of its task, or removed with its run. Run `lakana check rows` again."
    )
    assert column_not_shown_message("no\x1bte", "gold.orders", _RUN, "dev") == (
        "The column `no\\x1bte` of the checked snapshot is not shown: `gold.orders` does not "
        "have it in this form any more (it was added by the run that failed, or dropped or "
        "changed since), so the rows this check found cannot be listed with it. The number of "
        f"rows the check found remains: `lakana check list --run {_RUN} --env dev`."
    )
    assert "check rows" not in COLUMN_NOT_SHOWN
    assert no_column_left_message("gold.or\x1bders", _RUN, "dev") == (
        "None of the columns of the checked snapshot is in `gold.or\\x1bders` in the same form "
        "any more, so the rows this check found cannot be listed. The number of rows the check "
        f"found remains: `lakana check list --run {_RUN} --env dev`."
    )
    assert policy_needs_a_column_message("Ti\x1ber", "gold.orders", _RUN, "dev") == (
        "A row filter or column mask of yours on `gold.orders` uses the column `Ti\\x1ber`, "
        "which is not the same column in the checked snapshot and in the table now (it was "
        "added, dropped or renamed since, or another column took its name), so the rows this "
        "check found cannot be shown under it. The number of rows the check found remains: "
        f"`lakana check list --run {_RUN} --env dev`."
    )
    for text in (NO_COLUMN_LEFT, POLICY_NEEDS_A_COLUMN):
        assert "check rows" not in text
