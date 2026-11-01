"""Tests for the kind of a query run: the one rule behind every worker pool."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import ColumnElement, event, func, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, aliased
from sqlalchemy.sql import ClauseElement

from lakana_core import config
from lakana_core.db import db_now
from lakana_core.models import (
    Environment,
    JobVersion,
    Principal,
    PrincipalType,
    QueryRun,
    QueryRunStatus,
    Repository,
    RepositoryKind,
    Run,
    RunKind,
    RunStatus,
    TaskRun,
    TaskStatus,
    WriteMode,
)
from lakana_core.runs.pools import (
    ALL,
    CAP_LOCK_KEY,
    INTERACTIVE,
    PIPELINE,
    cap_for,
    interactive_workers,
    is_interactive,
    is_pipeline,
    kind_of,
    lock_principal,
    pass_by,
    passed_by_until,
    running_and_passed_by,
    running_by_principal,
    running_count,
    takes_kind,
    under_cap,
)

ROW_KINDS: dict[str, str] = {
    "ad-hoc read": INTERACTIVE,
    "ad-hoc write": INTERACTIVE,
    "view creation": INTERACTIVE,
    "unbound task token": INTERACTIVE,
    "maintenance": PIPELINE,
    "input of a Python task": PIPELINE,
    "SQL task write": PIPELINE,
}
"""The kind of each row of the spec's table (section 4)."""

MODE_KINDS: dict[WriteMode | None, str] = {
    None: INTERACTIVE,
    WriteMode.CREATE: INTERACTIVE,
    WriteMode.REPLACE: INTERACTIVE,
    WriteMode.APPEND: INTERACTIVE,
    WriteMode.MERGE: INTERACTIVE,
    WriteMode.VIEW: INTERACTIVE,
    WriteMode.MAINTAIN: PIPELINE,
    WriteMode.HISTORY: INTERACTIVE,
}
"""The kind of a query run without a task run, by write mode: one entry per mode that exists."""


@pytest.fixture
def environment_id(session: Session) -> uuid.UUID:
    """Insert one environment and return its id.

    Parameters
    ----------
    session
        Database session to insert into.

    Returns
    -------
    uuid.UUID
        The environment's id.
    """
    env = Environment(
        id=uuid.uuid4(),
        name="dev",
        storage_root="file:///tmp/lakana-test",
        storage_options={},
        is_protected=False,
        properties={},
    )
    session.add(env)
    session.flush()
    return env.id


def _task_run(session: Session, environment_id: uuid.UUID) -> uuid.UUID:
    """Insert a running task run with the rows it needs.

    Parameters
    ----------
    session
        Database session.
    environment_id
        The environment of the run.

    Returns
    -------
    uuid.UUID
        The task run's id.
    """
    repo = Repository(
        id=uuid.uuid4(), name=f"r{uuid.uuid4().hex[:6]}", kind=RepositoryKind.HTTPS, url="https://h"
    )
    session.add(repo)
    session.flush()
    version = JobVersion(
        id=uuid.uuid4(),
        repository_id=repo.id,
        environment_id=environment_id,
        commit_sha="a" * 40,
        manifest_json={},
        code_uri="file:///tmp/code",
        code_sha256="b" * 64,
    )
    session.add(version)
    session.flush()
    run = Run(
        id=uuid.uuid4(),
        environment_id=environment_id,
        repository_id=repo.id,
        job_version_id=version.id,
        kind=RunKind.FULL,
        status=RunStatus.RUNNING,
        selection_json={},
    )
    session.add(run)
    session.flush()
    task = TaskRun(
        id=uuid.uuid4(),
        run_id=run.id,
        asset="a.b",
        status=TaskStatus.RUNNING,
        max_attempts=1,
        timeout_seconds=60,
        retry_delay_seconds=0,
        upstream_json=[],
    )
    session.add(task)
    session.flush()
    return task.id


def _insert(session: Session, environment_id: uuid.UUID, **fields: object) -> QueryRun:
    """Insert one queued query run with the given fields.

    Parameters
    ----------
    session
        Database session.
    environment_id
        The run's environment.
    **fields
        Column values that override the defaults.

    Returns
    -------
    QueryRun
        The flushed run.
    """
    values: dict[str, object] = {
        "id": uuid.uuid4(),
        "environment_id": environment_id,
        "sql_text": "SELECT 1",
        "status": QueryRunStatus.QUEUED,
    }
    values.update(fields)
    run = QueryRun(**values)
    session.add(run)
    session.flush()
    return run


def _rows(
    session: Session, environment_id: uuid.UUID, task_run_id: uuid.UUID
) -> dict[str, tuple[QueryRun, str]]:
    """Insert one query run for each row of the spec's table.

    Parameters
    ----------
    session
        Database session.
    environment_id
        The runs' environment.
    task_run_id
        A real task run, for the rows that belong to one.

    Returns
    -------
    dict[str, tuple[QueryRun, str]]
        Each row's name to its run and the kind the spec gives it.
    """
    token = uuid.uuid4()
    fields: dict[str, dict[str, object]] = {
        "ad-hoc read": {},
        "ad-hoc write": {"write_mode": WriteMode.APPEND},
        "view creation": {"write_mode": WriteMode.VIEW},
        "unbound task token": {"task_token_id": token},
        "maintenance": {"write_mode": WriteMode.MAINTAIN},
        "input of a Python task": {"task_token_id": token, "task_run_id": task_run_id},
        "SQL task write": {
            "task_run_id": task_run_id,
            "write_mode": WriteMode.REPLACE,
            "status": QueryRunStatus.RUNNING,
        },
    }
    assert fields.keys() == ROW_KINDS.keys()
    return {
        name: (_insert(session, environment_id, **extra), ROW_KINDS[name])
        for name, extra in fields.items()
    }


def _ids(session: Session, condition: ColumnElement[bool]) -> set[uuid.UUID]:
    """Select the ids of the query runs a condition finds.

    Parameters
    ----------
    session
        Database session.
    condition
        A SQL condition over `QueryRun`.

    Returns
    -------
    set[uuid.UUID]
        The ids found.
    """
    return set(session.scalars(select(QueryRun.id).where(condition)))


def test_the_sql_condition_and_the_row_rule_agree_on_every_row_of_the_spec(
    session: Session, environment_id: uuid.UUID
) -> None:
    rows = _rows(session, environment_id, _task_run(session, environment_id))
    pipeline, interactive = _ids(session, is_pipeline()), _ids(session, is_interactive())
    for name, (run, kind) in rows.items():
        assert kind_of(run.task_run_id, run.write_mode) == kind, name
        assert (run.id in pipeline) == (kind == PIPELINE), name
        assert (run.id in interactive) == (kind == INTERACTIVE), name


def test_the_two_conditions_are_each_others_complement(
    session: Session, environment_id: uuid.UUID
) -> None:
    rows = _rows(session, environment_id, _task_run(session, environment_id))
    every = {run.id for run, _ in rows.values()}
    assert len(every) == 7
    assert _ids(session, ~is_pipeline()) == _ids(session, is_interactive())
    assert _ids(session, ~is_interactive()) == _ids(session, is_pipeline())
    assert _ids(session, is_pipeline()) | _ids(session, is_interactive()) == every
    assert not _ids(session, is_pipeline()) & _ids(session, is_interactive())


def test_every_write_mode_that_exists_is_classified_and_exactly_one_condition_holds(
    session: Session, environment_id: uuid.UUID
) -> None:
    """A write mode added later fails here until its kind is decided, instead of falling through."""
    assert set(MODE_KINDS) == {None, *WriteMode}
    task_run_id = _task_run(session, environment_id)
    for mode, kind in MODE_KINDS.items():
        for bound in (None, task_run_id):
            run = _insert(session, environment_id, write_mode=mode, task_run_id=bound)
            expected = PIPELINE if bound is not None else kind
            assert kind_of(run.task_run_id, run.write_mode) == expected, (mode, bound)
            assert (run.id in _ids(session, is_pipeline())) == (expected == PIPELINE)
            assert (run.id in _ids(session, is_interactive())) == (expected == INTERACTIVE)


def test_the_conditions_work_on_an_alias(session: Session, environment_id: uuid.UUID) -> None:
    _rows(session, environment_id, _task_run(session, environment_id))
    other = aliased(QueryRun)
    assert session.scalar(select(func.count()).select_from(other).where(is_interactive(other))) == 4
    assert session.scalar(select(func.count()).select_from(other).where(is_pipeline(other))) == 3


@pytest.mark.parametrize(
    ("takes", "kind", "expected"),
    [
        (ALL, INTERACTIVE, True),
        (ALL, PIPELINE, True),
        (INTERACTIVE, INTERACTIVE, True),
        (INTERACTIVE, PIPELINE, False),
        (PIPELINE, INTERACTIVE, False),
        (PIPELINE, PIPELINE, True),
    ],
)
def test_takes_kind(takes: str, kind: str, expected: bool) -> None:
    assert takes_kind(takes, kind) is expected


def test_the_settings_values_are_the_three_constants() -> None:
    assert set(config.WORKER_TAKES) == {ALL, INTERACTIVE, PIPELINE}


def test_auto_reserves_a_worker_only_from_three_on() -> None:
    assert [cap_for("auto", n) for n in (0, 1, 2, 3, 4)] == [1, 1, 2, 2, 3]


def test_a_fixed_number_replaces_the_rule() -> None:
    assert cap_for(1, 2) == 1
    assert cap_for(5, 2) == 5
    assert cap_for(2, 9) == 2


def test_interactive_workers_counts_all_and_interactive_and_not_pipeline() -> None:
    assert interactive_workers(["all", "interactive", "pipeline", "pipeline"]) == 2
    assert interactive_workers([]) == 0
    assert interactive_workers(iter(["pipeline"])) == 0


def _principal(session: Session, name: str, kind: PrincipalType = PrincipalType.USER) -> uuid.UUID:
    """Insert a principal.

    Parameters
    ----------
    session
        Database session.
    name
        The principal's name.
    kind
        User or service account.

    Returns
    -------
    uuid.UUID
        The principal's id.
    """
    principal = Principal(id=uuid.uuid4(), type=kind, name=name)
    session.add(principal)
    session.flush()
    return principal.id


def test_the_count_is_the_principals_running_interactive_runs_of_every_environment(
    session: Session, environment_id: uuid.UUID
) -> None:
    """Two ad-hoc runs count, in two environments; maintenance, task writes, queued runs do not."""
    other_env = Environment(
        id=uuid.uuid4(),
        name="prod",
        storage_root="file:///tmp/lakana-test-2",
        storage_options={},
        is_protected=False,
        properties={},
    )
    session.add(other_env)
    session.flush()
    mine, other = _principal(session, "mine"), _principal(session, "other")
    task_run_id = _task_run(session, environment_id)
    running = QueryRunStatus.RUNNING
    _insert(session, environment_id, principal_id=mine, status=running)
    _insert(session, other_env.id, principal_id=mine, status=running, write_mode=WriteMode.APPEND)
    _insert(
        session, environment_id, principal_id=mine, status=running, write_mode=WriteMode.MAINTAIN
    )
    _insert(
        session,
        environment_id,
        principal_id=mine,
        status=running,
        write_mode=WriteMode.REPLACE,
        task_run_id=task_run_id,
    )
    _insert(session, environment_id, principal_id=mine)  # queued
    _insert(session, environment_id, principal_id=mine, status=QueryRunStatus.SUCCEEDED)
    _insert(session, environment_id, principal_id=other, status=running)
    _insert(session, environment_id, principal_id=None, status=running)
    _insert(session, environment_id, principal_id=None, status=running)
    assert running_count(session, mine) == 2
    assert running_count(session, other) == 1
    assert running_count(session, uuid.uuid4()) == 0
    assert running_by_principal(session) == {mine: 2, other: 1}
    assert running_by_principal(session, at_least=2) == {mine: 2}
    assert running_by_principal(session, at_least=3) == {}


def test_under_cap_is_true_below_the_cap_and_false_at_it(
    session: Session, environment_id: uuid.UUID
) -> None:
    mine = _principal(session, "mine")
    for _ in range(2):
        _insert(session, environment_id, principal_id=mine, status=QueryRunStatus.RUNNING)
    queued = _insert(session, environment_id, principal_id=mine)
    found = select(QueryRun.id).where(QueryRun.id == queued.id, under_cap(mine, 3))
    assert session.scalars(found).all() == [queued.id]
    refused = select(QueryRun.id).where(QueryRun.id == queued.id, under_cap(mine, 2))
    assert session.scalars(refused).all() == []


def test_under_cap_stands_inside_an_update_of_the_same_table(
    session: Session, environment_id: uuid.UUID
) -> None:
    """The alias is what lets the count sit in the `WHERE` of an `UPDATE` of `query_runs`."""
    mine = _principal(session, "mine")
    _insert(session, environment_id, principal_id=mine, status=QueryRunStatus.RUNNING)
    queued = _insert(session, environment_id, principal_id=mine)
    for cap, matched in ((1, 0), (2, 1)):
        result = session.execute(
            update(QueryRun)
            .where(QueryRun.id == queued.id, under_cap(mine, cap))
            .values(sql_text="SELECT 2")
            .execution_options(synchronize_session=False)
        )
        assert result.rowcount == matched  # ty: ignore[unresolved-attribute]


def test_the_cap_lock_is_a_postgres_advisory_lock_with_two_keys() -> None:
    """On PostgreSQL: `pg_advisory_xact_lock(<fixed key>, <32 bits of the principal's id>)`.

    The behaviour under real contention is the integration tier's; here the statement is
    checked on a stub session, for an id whose first bit is set (a negative key).
    """
    statements: list[str] = []

    class StubSession:
        """Reports a PostgreSQL bind and records what it is asked to run."""

        def get_bind(self) -> SimpleNamespace:
            """Return a bind that looks like PostgreSQL.

            Returns
            -------
            SimpleNamespace
                An object with ``dialect.name``.
            """
            return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

        def execute(self, statement: ClauseElement) -> None:
            """Record the compiled statement.

            Parameters
            ----------
            statement
                The statement.
            """
            statements.append(str(statement.compile(compile_kwargs={"literal_binds": True})))

    principal_id = uuid.UUID(int=(1 << 127) | 5)
    assert lock_principal(StubSession(), principal_id) is True  # ty: ignore[invalid-argument-type]
    key = int.from_bytes(principal_id.bytes[:4], "big", signed=True)
    assert key < 0
    assert CAP_LOCK_KEY == 1818977073
    assert statements == [
        "SET LOCAL lock_timeout = '5s'",
        f"SELECT pg_advisory_xact_lock(CAST({CAP_LOCK_KEY} AS INTEGER), CAST({key} AS INTEGER)) "
        "AS pg_advisory_xact_lock_1",
    ]


def test_no_lock_statement_on_sqlite(session: Session) -> None:
    statements: list[str] = []
    event.listen(session.get_bind(), "before_cursor_execute", lambda *a: statements.append(a[2]))
    lock_principal(session, uuid.uuid4())
    assert statements == []


class _FailingLockSession:
    """A PostgreSQL stand-in whose lock statement fails with the given error."""

    def __init__(self, failure: Exception) -> None:
        """Remember the failure and start recording.

        Parameters
        ----------
        failure
            What the lock statement raises.
        """
        self.failure = failure
        self.statements: list[str] = []

    def get_bind(self) -> SimpleNamespace:
        """Return a bind that looks like PostgreSQL.

        Returns
        -------
        SimpleNamespace
            An object with ``dialect.name``.
        """
        return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

    def execute(self, statement: ClauseElement) -> None:
        """Record the statement; the lock raises.

        Parameters
        ----------
        statement
            The statement.

        Raises
        ------
        Exception
            The configured failure, for the lock statement.
        """
        text_of = str(statement.compile(compile_kwargs={"literal_binds": True}))
        self.statements.append(text_of)
        if "pg_advisory_xact_lock" in text_of:
            raise self.failure


def test_a_lock_timeout_is_a_refused_claim_and_any_other_failure_is_not() -> None:
    """The bounded wait: a timeout (SQLSTATE 55P03) returns `False`; other errors propagate."""
    timeout = OperationalError(
        "SELECT 1", {}, Exception("{'C': '55P03', 'M': 'canceling statement due to lock timeout'}")
    )
    stub = _FailingLockSession(timeout)
    assert lock_principal(stub, uuid.uuid4()) is False  # ty: ignore[invalid-argument-type]
    assert [s.split()[0] for s in stub.statements] == ["SET", "SELECT"]
    broken = OperationalError("SELECT 1", {}, Exception("connection reset"))
    with pytest.raises(OperationalError):
        lock_principal(_FailingLockSession(broken), uuid.uuid4())  # ty: ignore[invalid-argument-type]


def _principal(session: Session, name: str) -> uuid.UUID:
    """Insert a user.

    Parameters
    ----------
    session
        Database session.
    name
        The user's name.

    Returns
    -------
    uuid.UUID
        The principal's id.
    """
    principal = Principal(id=uuid.uuid4(), type=PrincipalType.USER, name=name)
    session.add(principal)
    session.flush()
    return principal.id


def test_pass_by_writes_a_time_that_the_status_read_and_the_listing_see(session: Session) -> None:
    """#95: the time is the database's now plus the seconds; only a future one is read."""
    one, other, never = (_principal(session, n) for n in ("one", "other", "never"))
    before = db_now(session)
    pass_by(session, one, 30)
    after = db_now(session)

    count, until = running_and_passed_by(session, one, after)
    assert count == 0 and until is not None
    assert before + timedelta(seconds=30) <= until <= after + timedelta(seconds=30)
    assert running_and_passed_by(session, other, after) == (0, None)
    assert running_and_passed_by(session, one, until) == (0, None), "a time that is up is nothing"
    assert passed_by_until(session, [one, other, never], after) == {one: until}
    assert passed_by_until(session, [one], until) == {}
    assert passed_by_until(session, [], after) == {}


def test_pass_by_counts_the_running_queries_in_the_same_statement(
    session: Session, environment_id: uuid.UUID
) -> None:
    """The status read pays no statement more for the time."""
    who = _principal(session, "who")
    session.add(
        QueryRun(
            id=uuid.uuid4(),
            environment_id=environment_id,
            principal_id=who,
            sql_text="SELECT 1",
            status=QueryRunStatus.RUNNING,
        )
    )
    pass_by(session, who, 30)
    seen: list[str] = []
    event.listen(
        session.get_bind(),
        "before_cursor_execute",
        lambda conn, cursor, statement, *rest: seen.append(statement),
    )
    count, until = running_and_passed_by(session, who, datetime.now(UTC))
    assert (count, until is not None, len(seen)) == (1, True, 1)


def test_pass_by_leaves_the_principals_update_time_alone(session: Session) -> None:
    """A pass-by is not a change of the principal."""
    who = _principal(session, "who")
    stored = session.get(Principal, who)
    assert stored is not None
    stored.updated_at = datetime(2026, 1, 1, tzinfo=UTC)
    session.commit()
    pass_by(session, who, 30)
    session.expire_all()
    stored = session.get(Principal, who)
    assert stored is not None and stored.updated_at == datetime(2026, 1, 1, tzinfo=UTC)
