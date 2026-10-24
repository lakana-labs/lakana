"""A task is refused a table that was kept after a failed check.

`CatalogService.load_table` is the one place a task reads a managed table through, so the
refusal sits there. Ad-hoc readers are never stopped.
"""

from __future__ import annotations

import ast
import contextlib
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from core_test_helpers import (
    ActionAllowList,
    _bootstrap_principal,
    _config,
    _prepared_catalog,
    python_task_run,
)
from sqlalchemy import event, select, update
from sqlalchemy.orm import Session

import lakana_core
from lakana_core.auth import Action, AllowAllAuthorization, PrincipalRef, RunContext, TaskScope
from lakana_core.catalog import service as service_module
from lakana_core.catalog.service import CatalogService
from lakana_core.errors import KeptAfterCheckError, LakanaError, NotPublishedError, PermissionDenied
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.models import (
    JobVersion,
    QueryRun,
    QueryRunStatus,
    Run,
    Table,
    TaskRun,
    TaskStatus,
    WriteMode,
)
from lakana_core.storage.resolver import StorageResolver

_TABLE = "silver.orders"
_CHECK = "silver.orders/unique_id"
_REF = TableRef.from_dotted("dev", _TABLE)


def _k4(kept_run_id: uuid.UUID, *, check: str = _CHECK) -> str:
    """Build text K4 for ``silver.orders`` in ``dev``.

    Parameters
    ----------
    kept_run_id
        The run the note names.
    check
        What the note shows for the failing checks.

    Returns
    -------
    str
        The text.
    """
    return (
        f"`silver.orders` was kept after a failed check (`{check}`, run "
        f"{kept_run_id}) and has not passed its checks since, so this task does not read it. A "
        "run of the project that builds `silver.orders` clears this: build it again there "
        "(`lakana run --select silver.orders --env dev`), or repair the data and run that "
        "project. If no project builds it any more, drop it: "
        "`lakana table delete silver.orders --env dev`."
    )


@pytest.fixture
def catalog(session: Session, tmp_path: Path) -> CatalogService:
    """Provide a catalog with ``dev``, ``silver.orders`` and ``silver.plain`` created.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        Storage root of the ``dev`` environment.

    Returns
    -------
    CatalogService
        The catalog, under allow-all authorization.
    """
    return _prepared_catalog(
        session,
        tmp_path,
        AllowAllAuthorization(),
        namespaces=[NamespacePath.from_parts(["silver"])],
        tables=[_REF, TableRef.from_dotted("dev", "silver.plain")],
    )


@pytest.fixture
def kept_run_id(session: Session, catalog: CatalogService) -> uuid.UUID:
    """Put the note on ``silver.orders`` and provide the run it names.

    Parameters
    ----------
    session
        Database session; commits.
    catalog
        Requested so that the table exists first.

    Returns
    -------
    uuid.UUID
        The run the note names.
    """
    run_id = uuid.uuid4()
    session.execute(
        update(Table)
        .where(Table.name == "orders")
        .values(kept_run_id=run_id, kept_task_run_id=uuid.uuid4(), kept_check=_CHECK)
    )
    session.commit()
    return run_id


def _task_in(
    session: Session, first: TaskRun, asset: str, upstream: Sequence[str] = (), *, kind: str
) -> TaskRun:
    """Add a task run to the run of another.

    Parameters
    ----------
    session
        Database session; commits.
    first
        A task run of the run.
    asset
        The new task's asset.
    upstream
        Its upstreams in the run.
    kind
        ``asset`` or ``source_check``.

    Returns
    -------
    TaskRun
        The new task run, running under claim 1.
    """
    task = TaskRun(
        id=uuid.uuid4(),
        run_id=first.run_id,
        asset=asset,
        status=TaskStatus.RUNNING,
        claim_count=1,
        attempt=1,
        lease_owner="w1",
        max_attempts=3,
        timeout_seconds=60,
        retry_delay_seconds=0,
        upstream_json=list(upstream),
        kind=kind,
    )
    session.add(task)
    session.commit()
    return task


def _run_of(
    session: Session,
    principal: PrincipalRef,
    tasks: dict[str, Sequence[str]],
    *,
    kinds: dict[str, str] | None = None,
    environment: str = "dev",
    builds: Sequence[str] = (_TABLE,),
    manifest: object = None,
) -> dict[str, TaskRun]:
    """Insert a run with the given tasks, of a project version whose manifest has the given assets.

    Parameters
    ----------
    session
        Database session; commits.
    principal
        The run principal.
    tasks
        Asset to its upstreams (`task_runs.upstream_json`); at least one.
    kinds
        Asset to task kind, for the tasks that are not ``asset``.
    environment
        The run's environment.
    builds
        Assets of the run's project beside its tasks of kind ``asset``. ``silver.orders`` by
        default: the run is one of the project that builds the kept table. Empty for a
        project that only reads it.
    manifest
        The whole manifest, stored as it is in place of the one built from the tasks.

    Returns
    -------
    dict
        Asset to its task run.
    """
    kinds = kinds or {}
    names = list(tasks)
    first = python_task_run(
        session, principal.id, asset=names[0], inputs={}, kind="sql", environment=environment
    )
    first.upstream_json = list(tasks[names[0]])
    first.kind = kinds.get(names[0], "asset")
    run = session.get(Run, first.run_id)
    assert run is not None
    assets = sorted({*builds, *(name for name in names if kinds.get(name, "asset") == "asset")})
    session.execute(
        update(JobVersion)
        .where(JobVersion.id == run.job_version_id)
        .values(
            manifest_json={
                "version": 3,
                "assets": [{"table": name, "sql": ""} for name in assets],
                "edges": [],
            }
            if manifest is None
            else manifest
        )
    )
    session.commit()
    built = {names[0]: first}
    for name in names[1:]:
        built[name] = _task_in(session, first, name, tasks[name], kind=kinds.get(name, "asset"))
    return built


def _worker_form(principal: PrincipalRef, task: TaskRun) -> PrincipalRef:
    """Build the principal of a query run the worker executes for a task.

    Parameters
    ----------
    principal
        The run principal.
    task
        The task run.

    Returns
    -------
    PrincipalRef
        The principal with the query-run form of the run context.
    """
    return PrincipalRef(
        id=principal.id,
        name=principal.name,
        type=principal.type,
        run_context=RunContext(task_run_id=task.id, query_run_id=uuid.uuid4()),
    )


def _count_form(
    session: Session,
    principal: PrincipalRef,
    task: TaskRun,
    *,
    of: uuid.UUID | None = None,
    columns: Mapping[str, Any] | None = None,
) -> PrincipalRef:
    """Insert the query run of a check's count and build the principal it is executed as.

    Parameters
    ----------
    session
        Database session; commits.
    principal
        The run principal.
    task
        The task run in the run context.
    of
        The task run on the query run's row; `task`'s when `None`.
    columns
        Columns of the query run that make it something else than a count.

    Returns
    -------
    PrincipalRef
        The principal with the query-run form of the run context, as `pipeline._run_context`
        builds it for a run the worker inserted for a task.
    """
    run = session.get(Run, task.run_id)
    assert run is not None
    query = QueryRun(
        id=uuid.uuid4(),
        environment_id=run.environment_id,
        principal_id=principal.id,
        sql_text="SELECT count(*) FROM (SELECT 1)",
        status=QueryRunStatus.RUNNING,
        task_run_id=task.id if of is None else of,
        worker_id="w1",
        **(columns or {}),
    )
    session.add(query)
    session.commit()
    return PrincipalRef(
        id=principal.id,
        name=principal.name,
        type=principal.type,
        run_context=RunContext(task_run_id=task.id, query_run_id=query.id),
    )


def _claim_form(principal: PrincipalRef, task: TaskRun) -> PrincipalRef:
    """Build the principal of a token bound to a task attempt.

    Parameters
    ----------
    principal
        The run principal.
    task
        The task run.

    Returns
    -------
    PrincipalRef
        The principal with the claim form of the run context.
    """
    return PrincipalRef(
        id=principal.id,
        name=principal.name,
        type=principal.type,
        run_context=RunContext(task_run_id=task.id, claim=task.claim_count),
    )


def test_an_ad_hoc_reader_is_never_refused(
    catalog: CatalogService, principal: PrincipalRef, kept_run_id: uuid.UUID
) -> None:
    assert principal.run_context is None

    assert catalog.load_table(principal, _REF).ref == _REF


def test_a_task_of_a_run_without_a_task_for_the_table_is_refused(
    session: Session, catalog: CatalogService, principal: PrincipalRef, kept_run_id: uuid.UUID
) -> None:
    tasks = _run_of(session, principal, {"gold.report": []})

    with pytest.raises(KeptAfterCheckError) as caught:
        catalog.load_table(_worker_form(principal, tasks["gold.report"]), _REF)

    assert str(caught.value) == (
        "`silver.orders` was kept after a failed check (`silver.orders/unique_id`, run "
        f"{kept_run_id}) and has not passed its checks since, so this task does not read it. A "
        "run of the project that builds `silver.orders` clears this: build it again there "
        "(`lakana run --select silver.orders --env dev`), or repair the data and run that "
        "project. If no project builds it any more, drop it: "
        "`lakana table delete silver.orders --env dev`."
    )
    assert isinstance(caught.value, LakanaError)
    assert not isinstance(caught.value, (NotPublishedError, PermissionDenied))


@pytest.mark.parametrize("kind", ["asset", "source_check"])
def test_the_tables_own_check_and_its_rebuild_read_it(
    session: Session,
    catalog: CatalogService,
    principal: PrincipalRef,
    kept_run_id: uuid.UUID,
    kind: str,
) -> None:
    tasks = _run_of(session, principal, {_TABLE: []}, kinds={_TABLE: kind})

    assert catalog.load_table(_worker_form(principal, tasks[_TABLE]), _REF).ref == _REF
    assert catalog.load_table(_claim_form(principal, tasks[_TABLE]), _REF).ref == _REF


def test_a_task_downstream_of_the_tables_task_reads_it(
    session: Session, catalog: CatalogService, principal: PrincipalRef, kept_run_id: uuid.UUID
) -> None:
    tasks = _run_of(
        session,
        principal,
        {
            _TABLE: [],
            "gold.direct": ["silver.other", _TABLE],
            "gold.middle": ["gold.direct"],
            "gold.far": ["gold.middle"],
        },
        kinds={_TABLE: "source_check"},
    )

    for reader in ("gold.direct", "gold.middle", "gold.far"):
        assert catalog.load_table(_worker_form(principal, tasks[reader]), _REF).ref == _REF
        assert catalog.load_table(_claim_form(principal, tasks[reader]), _REF).ref == _REF


def test_a_task_of_the_same_run_that_is_not_downstream_is_refused(
    session: Session, catalog: CatalogService, principal: PrincipalRef, kept_run_id: uuid.UUID
) -> None:
    """The run has a task for the table, and that is not enough: the reader must come after it."""
    tasks = _run_of(
        session,
        principal,
        {
            _TABLE: ["bronze.raw"],
            "bronze.raw": [],
            "gold.reader": [_TABLE],
            "gold.sibling": ["bronze.raw"],
            "gold.alone": [],
            "silver.orders_old": [],
            "gold.orders": [],
            "gold.near": ["silver.orders_old", "gold.orders"],
        },
    )

    strangers = (
        "gold.sibling",
        "gold.alone",
        "bronze.raw",
        "silver.orders_old",
        "gold.orders",
        "gold.near",
    )
    for stranger in strangers:
        for form in (_worker_form, _claim_form):
            with pytest.raises(KeptAfterCheckError) as caught:
                catalog.load_table(form(principal, tasks[stranger]), _REF)
            assert str(caught.value) == _k4(kept_run_id)
    assert catalog.load_table(_worker_form(principal, tasks["gold.reader"]), _REF).ref == _REF


def test_a_foreign_projects_check_task_and_its_readers_are_refused(
    session: Session, catalog: CatalogService, principal: PrincipalRef, kept_run_id: uuid.UUID
) -> None:
    """The exemptions are for runs of the project that builds the table.

    Another project's check of the table may be weaker than the check that failed, so neither
    its ``source_check`` task (its asset is the table) nor a reader downstream of it reads.
    """
    tasks = _run_of(
        session,
        principal,
        {_TABLE: [], "gold.report": [_TABLE], "gold.far": ["gold.report"]},
        kinds={_TABLE: "source_check"},
        builds=(),
    )

    for task in tasks.values():
        for form in (_worker_form, _claim_form):
            with pytest.raises(KeptAfterCheckError) as caught:
                catalog.load_table(form(principal, task), _REF)
            assert str(caught.value) == _k4(kept_run_id)


@pytest.mark.parametrize(
    "manifest",
    [
        [],
        {"assets": "silver.orders"},
        {"assets": [{"table": 7}, "silver.orders", {"name": "silver.orders"}]},
        {"assets": [{"table": "silver.orders_old"}, {"table": "gold.orders"}]},
    ],
)
def test_a_manifest_that_does_not_list_the_table_gives_no_standing(
    session: Session,
    catalog: CatalogService,
    principal: PrincipalRef,
    kept_run_id: uuid.UUID,
    manifest: object,
) -> None:
    tasks = _run_of(
        session,
        principal,
        {_TABLE: [], "gold.report": [_TABLE]},
        kinds={_TABLE: "source_check"},
        manifest=manifest,
    )

    for task in tasks.values():
        with pytest.raises(KeptAfterCheckError):
            catalog.load_table(_worker_form(principal, task), _REF)


def test_the_count_of_a_check_reads_a_kept_table_of_its_own_project(
    session: Session, catalog: CatalogService, principal: PrincipalRef, kept_run_id: uuid.UUID
) -> None:
    """A count builds nothing, so it reads without being the table's task or below it.

    Two tables of one project whose checks read each other would otherwise lock each other.
    Anything else the same task does is still refused.
    """
    tasks = _run_of(session, principal, {"silver.other": []})
    task = tasks["silver.other"]

    assert catalog.load_table(_count_form(session, principal, task), _REF).ref == _REF
    for other in (_worker_form(principal, task), _claim_form(principal, task)):
        with pytest.raises(KeptAfterCheckError):
            catalog.load_table(other, _REF)


def test_a_foreign_projects_count_is_refused(
    session: Session, catalog: CatalogService, principal: PrincipalRef, kept_run_id: uuid.UUID
) -> None:
    tasks = _run_of(
        session,
        principal,
        {_TABLE: [], "gold.report": [_TABLE]},
        kinds={_TABLE: "source_check"},
        builds=(),
    )

    for task in tasks.values():
        with pytest.raises(KeptAfterCheckError) as caught:
            catalog.load_table(_count_form(session, principal, task), _REF)
        assert str(caught.value) == _k4(kept_run_id)


@pytest.mark.parametrize(
    "columns",
    [
        {"write_mode": WriteMode.REPLACE, "target_name": "silver.other"},
        {"write_mode": WriteMode.VIEW},
        {"target_name": "silver.other"},
        {"task_token_id": uuid.UUID("0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d")},
    ],
)
def test_a_query_run_that_is_not_a_count_gives_no_standing(
    session: Session,
    catalog: CatalogService,
    principal: PrincipalRef,
    kept_run_id: uuid.UUID,
    columns: dict[str, object],
) -> None:
    """A SQL task's write run has a target; a query submitted with a token carries the token."""
    tasks = _run_of(session, principal, {"silver.other": []})

    with pytest.raises(KeptAfterCheckError):
        catalog.load_table(
            _count_form(session, principal, tasks["silver.other"], columns=columns), _REF
        )


def test_the_count_of_another_task_gives_no_standing(
    session: Session, catalog: CatalogService, principal: PrincipalRef, kept_run_id: uuid.UUID
) -> None:
    """The query run must be the context's own task's: the two ids are checked together."""
    tasks = _run_of(session, principal, {"silver.other": [], "gold.report": []})
    reader = _count_form(session, principal, tasks["gold.report"], of=tasks["silver.other"].id)

    with pytest.raises(KeptAfterCheckError):
        catalog.load_table(reader, _REF)


def test_an_upstream_name_that_no_task_of_the_run_has_lets_nothing_through(
    session: Session, catalog: CatalogService, principal: PrincipalRef, kept_run_id: uuid.UUID
) -> None:
    """A name in `upstream_json` is not a task: only a task of the run for the table counts."""
    tasks = _run_of(session, principal, {"gold.report": [_TABLE], "gold.next": ["gold.report"]})

    for reader in ("gold.report", "gold.next"):
        with pytest.raises(KeptAfterCheckError):
            catalog.load_table(_worker_form(principal, tasks[reader]), _REF)


def test_upstreams_that_form_a_cycle_end_in_a_refusal(
    session: Session, catalog: CatalogService, principal: PrincipalRef, kept_run_id: uuid.UUID
) -> None:
    tasks = _run_of(session, principal, {"gold.a": ["gold.b"], "gold.b": ["gold.a"]})

    with pytest.raises(KeptAfterCheckError):
        catalog.load_table(_worker_form(principal, tasks["gold.a"]), _REF)


def test_a_malformed_upstream_list_lets_nothing_through(
    session: Session, catalog: CatalogService, principal: PrincipalRef, kept_run_id: uuid.UUID
) -> None:
    tasks = _run_of(session, principal, {_TABLE: [], "gold.report": []})
    session.execute(
        update(TaskRun)
        .where(TaskRun.id == tasks["gold.report"].id)
        .values(upstream_json={"silver.orders": True})
    )
    session.commit()

    with pytest.raises(KeptAfterCheckError):
        catalog.load_table(_worker_form(principal, tasks["gold.report"]), _REF)


def test_a_task_of_another_environment_with_the_same_asset_name_is_refused(
    session: Session,
    catalog: CatalogService,
    principal: PrincipalRef,
    kept_run_id: uuid.UUID,
    tmp_path: Path,
) -> None:
    """`silver.orders` of `prod` is another table: its task has no standing in `dev`."""
    catalog.create_environment(
        _bootstrap_principal(session), name="prod", storage=_config(tmp_path / "prod")
    )
    tasks = _run_of(session, principal, {_TABLE: [], "gold.report": [_TABLE]}, environment="prod")

    for task in tasks.values():
        with pytest.raises(KeptAfterCheckError) as caught:
            catalog.load_table(_claim_form(principal, task), _REF)
        assert str(caught.value) == _k4(kept_run_id)


def test_a_run_context_whose_task_run_is_gone_is_refused(
    catalog: CatalogService, principal: PrincipalRef, kept_run_id: uuid.UUID
) -> None:
    gone = PrincipalRef(
        id=principal.id,
        name=principal.name,
        type=principal.type,
        run_context=RunContext(task_run_id=uuid.uuid4(), claim=1),
    )

    with pytest.raises(KeptAfterCheckError):
        catalog.load_table(gone, _REF)


def test_an_unbound_task_token_is_refused_like_a_pipeline_task(
    catalog: CatalogService, principal: PrincipalRef, kept_run_id: uuid.UUID
) -> None:
    """A task-kind token bound to no task has no run context, and no standing either."""
    unbound = PrincipalRef(
        id=principal.id,
        name=principal.name,
        type=principal.type,
        task_scope=TaskScope(environment="dev", asset_scope=[], token_id=uuid.uuid4()),
    )
    assert unbound.run_context is None

    with pytest.raises(KeptAfterCheckError) as caught:
        catalog.load_table(unbound, _REF)

    assert str(caught.value).startswith(_k4(kept_run_id))
    assert "own token" in str(caught.value)


def test_the_note_shows_more_than_one_check_as_it_was_stored(
    session: Session, catalog: CatalogService, principal: PrincipalRef, kept_run_id: uuid.UUID
) -> None:
    session.execute(
        update(Table).where(Table.name == "orders").values(kept_check=f"{_CHECK} and 2 more")
    )
    session.commit()
    tasks = _run_of(session, principal, {"gold.report": []})

    with pytest.raises(KeptAfterCheckError) as caught:
        catalog.load_table(_worker_form(principal, tasks["gold.report"]), _REF)

    assert str(caught.value) == _k4(kept_run_id, check=f"{_CHECK} and 2 more")


def test_a_table_without_a_note_costs_no_query(
    session: Session, catalog: CatalogService, principal: PrincipalRef, kept_run_id: uuid.UUID
) -> None:
    """A task that reads a table without a note runs the statements an ad-hoc reader runs."""
    plain = TableRef.from_dotted("dev", "silver.plain")
    tasks = _run_of(session, principal, {"gold.report": []})
    readers = {
        "ad hoc": principal,
        "worker": _worker_form(principal, tasks["gold.report"]),
        "claim": _claim_form(principal, tasks["gold.report"]),
        "count": _count_form(session, principal, tasks["gold.report"]),
    }
    statements: list[str] = []

    def record(conn: object, cursor: object, statement: str, *rest: object) -> None:
        """Record one statement.

        Parameters
        ----------
        conn
            The connection; unused.
        cursor
            The cursor; unused.
        statement
            The SQL text.
        *rest
            The other arguments of the event; unused.
        """
        statements.append(statement)

    counts: dict[str, tuple[int, int]] = {}
    kept_counts: dict[str, tuple[int, int]] = {}
    event.listen(session.get_bind(), "before_cursor_execute", record)
    try:
        for name, reader in readers.items():
            for ref, into in ((plain, counts), (_REF, kept_counts)):
                session.rollback()
                statements.clear()
                with contextlib.suppress(KeptAfterCheckError):
                    catalog.load_table(reader, ref)
                into[name] = (
                    len(statements),
                    sum("task_runs" in text or "runs." in text for text in statements),
                )
    finally:
        event.remove(session.get_bind(), "before_cursor_execute", record)

    # The note is what costs. A table without one: a task runs exactly the statements an
    # ad-hoc reader runs, and none of them reads a task run. A kept table: an ad-hoc reader
    # still reads no task run. A task reads its own task run (with its run and the manifest, in
    # one statement) and the tasks of its run; the worker's form then looks for the count's
    # query run before it is refused, and a count that reads stops there too,
    # after one more read: is the task's write that run.
    assert counts["worker"] == counts["claim"] == counts["count"] == counts["ad hoc"]
    assert counts["ad hoc"][0] > 0
    assert {name: about_tasks for name, (_, about_tasks) in counts.items()} == {
        "ad hoc": 0,
        "worker": 0,
        "claim": 0,
        "count": 0,
    }
    assert {name: about_tasks for name, (_, about_tasks) in kept_counts.items()} == {
        "ad hoc": 0,
        "worker": 3,
        "claim": 2,
        "count": 4,
    }


def test_the_refusal_comes_after_the_read_grant(
    session: Session, tmp_path: Path, principal: PrincipalRef, kept_run_id: uuid.UUID
) -> None:
    """A task that may not read the table learns nothing about the note."""
    tasks = _run_of(session, principal, {"gold.report": []})
    authz = ActionAllowList()
    denied = CatalogService(session, StorageResolver(session), authz)

    for form in (_worker_form, _claim_form):
        with pytest.raises(PermissionDenied) as denial:
            denied.load_table(form(principal, tasks["gold.report"]), _REF)
        assert not isinstance(denial.value, KeptAfterCheckError)
        assert "kept" not in str(denial.value)
        assert str(kept_run_id) not in str(denial.value)
    assert {action for action, _ in authz.seen} == {Action.READ_TABLE}


def test_a_table_that_is_not_published_is_refused_as_that_first(
    session: Session, catalog: CatalogService, principal: PrincipalRef, kept_run_id: uuid.UUID
) -> None:
    session.execute(
        update(Table).where(Table.name == "orders").values(unpublished_run_id=uuid.uuid4())
    )
    session.commit()
    tasks = _run_of(session, principal, {"gold.report": []})

    with pytest.raises(NotPublishedError):
        catalog.load_table(_worker_form(principal, tasks["gold.report"]), _REF)


def test_both_forms_of_the_run_context_are_covered(
    session: Session, catalog: CatalogService, principal: PrincipalRef, kept_run_id: uuid.UUID
) -> None:
    """The worker's form (a SQL task, a check's count) and a bound token's are refused alike."""
    tasks = _run_of(session, principal, {"gold.report": []})

    for form in (_worker_form, _claim_form):
        with pytest.raises(KeptAfterCheckError) as caught:
            catalog.load_table(form(principal, tasks["gold.report"]), _REF)
        assert str(caught.value) == _k4(kept_run_id)


def test_a_table_is_read_again_once_the_note_is_cleared(
    session: Session, catalog: CatalogService, principal: PrincipalRef, kept_run_id: uuid.UUID
) -> None:
    tasks = _run_of(session, principal, {"gold.report": []})
    reader = _worker_form(principal, tasks["gold.report"])
    with pytest.raises(KeptAfterCheckError):
        catalog.load_table(reader, _REF)
    session.execute(
        update(Table)
        .where(Table.name == "orders")
        .values(kept_run_id=None, kept_task_run_id=None, kept_check=None)
    )
    session.commit()

    assert catalog.load_table(reader, _REF).ref == _REF


def test_a_task_is_refused_a_table_set_back_to_data_whose_checks_are_not_known(
    session: Session, catalog: CatalogService, principal: PrincipalRef, kept_run_id: uuid.UUID
) -> None:
    tasks = _run_of(session, principal, {"gold.report": []})
    reader = _worker_form(principal, tasks["gold.report"])
    session.execute(update(Table).where(Table.name == "orders").values(kept_unknown=True))
    session.commit()

    with pytest.raises(KeptAfterCheckError) as caught:
        catalog.load_table(reader, _REF)

    assert str(caught.value) == (
        f"`silver.orders` was set back to data whose checks are not known (run {kept_run_id} was "
        "set back) and has not passed its checks since, so this task does not read it. A run of "
        "the project that builds `silver.orders` clears this: build it again there "
        "(`lakana run --select silver.orders --env dev`), or repair the data and run that "
        "project. If no project builds it any more, drop it: "
        "`lakana table delete silver.orders --env dev`."
    )


def test_the_refusal_writes_nothing(
    session: Session, catalog: CatalogService, principal: PrincipalRef, kept_run_id: uuid.UUID
) -> None:
    tasks = _run_of(session, principal, {"gold.report": []})

    with pytest.raises(KeptAfterCheckError):
        catalog.load_table(_worker_form(principal, tasks["gold.report"]), _REF)

    assert not session.new and not session.dirty
    assert session.scalars(select(Table.kept_run_id).where(Table.name == "orders")).one() == (
        kept_run_id
    )


# --- the enumeration: what hands a task table data or file locations ---

_THROUGH_THE_REFUSAL = frozenset({"load_table", "load_table_at_check"})
"""The public `CatalogService` methods that hand a caller what it takes to read a table's rows
(its current metadata and file locations), and so call `_require_not_kept`."""

_READS_WHAT_IS_NOT_PUBLISHED = {
    "load_table_at_check": (
        "reads the one snapshot that a stored check result of the table recorded, taken from "
        "the result and never from the caller, so the read cannot be taken for the table's "
        "current state; and it refuses every principal with a run context"
    ),
}
"""The readers among them that do not call `_require_published`, each with its own rule."""

_ASKS_THE_REFUSALS_AS_A_QUESTION = {
    "_may_see_history": (
        "decides whether the answer to a commit or a create may carry the table's history; a "
        "refusal there means the reduced answer, never an error, and it hands out nothing itself"
    ),
}
"""The private methods that call both refusals to ask whether `load_table` would refuse."""

_OWN_ASSET = (
    "a write. Over REST a task arrives with the claim form of the run context only, and the "
    "write guard holds that form to the task's own asset in every environment; the worker "
    "calls it for the target of the task's own write"
)
_NO_TABLE = "about an environment or a namespace; returns nothing of a table"

_NOTHING_OF_A_KEPT_TABLE = {
    "create_environment": _NO_TABLE,
    "get_environment": _NO_TABLE,
    "list_environments": _NO_TABLE,
    "delete_environment": _NO_TABLE,
    "restore_environment": (
        "brings the deleted environment's tables back as they were, notes included, and returns "
        "only the environment; reading one goes through `load_table`"
    ),
    "list_deleted_environments": _NO_TABLE,
    "find_orphaned_locations": (
        "lists directories that no table and no location record names, with file counts and "
        "sizes; no table's metadata and no file locations of a table"
    ),
    "delete_orphaned_locations": (
        "deletes only directories that no table and no location record names, and returns "
        "their counts; nothing of a table"
    ),
    "environment_usage": (
        "returns counts and byte totals per kind from the catalog database; no table's metadata "
        "and no file locations"
    ),
    "update_environment_storage": _NO_TABLE,
    "set_environment_protection": _NO_TABLE,
    "create_namespace": _NO_TABLE,
    "load_namespace": _NO_TABLE,
    "list_namespaces": _NO_TABLE,
    "drop_namespace": _NO_TABLE,
    "update_namespace_properties": _NO_TABLE,
    "create_table": "a write: the table is new, so it has no note",
    "create_external_table": "a write: the table is new, so it has no note",
    "load_table_row": (
        "the catalog row, for its kind. The worker's source resolution then calls `load_table` "
        "for a managed table and reads files itself only for an EXTERNAL one, which no task "
        "builds and which therefore never carries the note"
    ),
    "describe_table": "the state `kept`, the schema and snapshot ids; no rows, no file locations",
    "can_read_table": "a boolean about a grant",
    "table_health": (
        "the newest result of each check and the last maintenance's counts, which are numbers "
        "and names; no rows, no snapshot, no file location"
    ),
    "set_table_description": "a write of text; it returns nothing of the table",
    "set_column_description": "a write of text; it returns nothing of the table",
    "commit_answer": (
        "the answer to the caller's own commit or create: the whole metadata only where "
        "`load_table` would hand it out (`_may_see_history` asks `_require_published` and "
        "`_require_not_kept`), for a bound token on its own asset too; otherwise no snapshot, ref "
        "or file location"
    ),
    "table_exists": "a boolean",
    "list_tables": "names",
    "commit_table": _OWN_ASSET,
    "rename_table": _OWN_ASSET,
    "append": _OWN_ASSET,
    "replace": _OWN_ASSET,
    "merge": _OWN_ASSET,
    "write_batches": _OWN_ASSET,
    "check_can_append": "the precheck of `append`; returns nothing",
    "check_can_replace": "the precheck of `replace`; returns nothing",
    "check_can_merge": "the precheck of `merge`; returns nothing",
    "roll_back": "sets the task's own asset back (`_require_own_task`); returns an outcome",
    "read_watermark": (
        "one value of the target of an incremental append, for which it authorizes WRITE_TABLE: "
        "a bound token holds that on its own asset only, and the worker's own write run "
        "targets the task's own asset"
    ),
    "resolve_lineage_sources": "table ids for the lineage record",
    "find_view": (
        "a view's SQL text; every table it names is then resolved through `load_table` as "
        "the reader"
    ),
    "create_or_replace_view": (
        "stores a view's SQL text and returns its row; reads no table, and whoever reads the "
        "view later loads the tables under it as themselves"
    ),
    "maintain": "maintenance is never a task's run, and returns a report of counts",
    "drop_table": "a delete; returns nothing",
    "undrop_table": (
        "a restore for a caller holding DROP_TABLE and CREATE_TABLE; the write guard refuses it "
        "from every task's run context, since creating a table is not what a task may do"
    ),
    "list_dropped_tables": "names and dates of dropped tables; no rows, no file locations",
    "table_retention": "a table's retention rule and dates; no rows, no file locations",
    "set_table_retention": "a write of a retention rule; returns the rule and dates",
    "set_environment_retention": "a write of an environment's retention; returns the environment",
    "list_expiring_tables": "names, rules and dates of expiring tables; no rows, no file locations",
}
"""Every other public method, with why a task gets no kept data from it."""

_CALLS_OF_THE_REFUSED_READ = {
    ("lakana-server", "api/iceberg/router.py", "load_table"): "the Iceberg REST load",
    ("lakana-worker", "pipeline.py", "load_table"): "the query path's source resolution",
    ("lakana-worker", "pipeline.py", "load_table_at_check"): (
        "the same source resolution, for a query run that `submit_check_rows` bound to a check "
        "result and for the one table whose row has the id that result names; the id passed is "
        "the stored result's, never a value of the request, and such a query run has no task "
        "run, so it executes without a run context"
    ),
}
"""The readers outside core, each with what it is: the Iceberg REST load, the query path's
source resolution, and that resolution's read of a table at the snapshot of a check."""

_READS_METADATA_DIRECTLY = {
    "lakana-worker/src/lakana_worker/pipeline.py": (
        "`list_data_files` over the metadata that `load_table` returned, and so refused first; "
        "and `LoadedAt.data_files` over what `load_table_at_check` returned, with the path "
        "check of the table's own location"
    ),
    "lakana-worker/src/lakana_worker/python_tasks.py": (
        "the current snapshot id of the task's own target, for its baseline; kept in the "
        "worker, never handed to the task's process"
    ),
    "lakana-worker/src/lakana_worker/task_checks.py": (
        "the current snapshot id of the task's own asset after its build (`_table_now`); kept "
        "in the worker"
    ),
}
"""Modules outside core that read Iceberg metadata or list data files themselves."""


def _sources(package: str) -> list[tuple[str, ast.Module]]:
    """Parse every source file of a package.

    Parameters
    ----------
    package
        ``lakana-server``, ``lakana-worker`` or ``lakana``.

    Returns
    -------
    list of tuple
        The path below the import package, and the parsed module.
    """
    packages = Path(lakana_core.__file__).resolve().parents[3]
    root = packages / package / "src" / package.replace("-", "_")
    return [
        (str(path.relative_to(root)), ast.parse(path.read_text(encoding="utf-8")))
        for path in sorted(root.rglob("*.py"))
    ]


def _public_methods() -> set[str]:
    """Name the public methods of `CatalogService`, read from its source.

    Returns
    -------
    set of str
        Every function defined in the class body whose name has no leading underscore.
    """
    tree = ast.parse(Path(service_module.__file__).read_text(encoding="utf-8"))
    service = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "CatalogService"
    )
    return {
        node.name
        for node in service.body
        if isinstance(node, ast.FunctionDef) and not node.name.startswith("_")
    }


def _calls_of(package: str, methods: frozenset[str]) -> set[tuple[str, str, str]]:
    """Find every call of the given catalog methods in a package's sources.

    Parameters
    ----------
    package
        ``lakana-server`` or ``lakana-worker``.
    methods
        Method names, none of which a list or a string has too.

    Returns
    -------
    set of tuple
        ``(package, path below the import package, method)`` for every attribute call of one
        of the names, whatever the receiver is called.
    """
    return {
        (package, relative, node.func.attr)
        for relative, tree in _sources(package)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in methods
    }


def _listings_without_a_path_check(tree: ast.Module) -> list[int]:
    """Find the calls of `LoadedAt.data_files` that do not confine what they read.

    Parameters
    ----------
    tree
        A parsed module.

    Returns
    -------
    list of int
        The line of every attribute call named ``data_files`` that has no `is_valid_path`
        keyword, or passes a literal `None` for it.
    """
    return sorted(
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "data_files"
        and not any(
            keyword.arg == "is_valid_path"
            and not (isinstance(keyword.value, ast.Constant) and keyword.value.value is None)
            for keyword in node.keywords
        )
    )


def test_every_public_catalog_method_is_placed() -> None:
    """A new public method of `CatalogService` fails here until it is put on one side.

    Either it hands out what it takes to read a table's rows and calls the refusal, or it is
    listed with the reason why a task gets no kept data from it. The methods are read from the
    class, not from a list in this file.
    """
    public = _public_methods()

    assert _THROUGH_THE_REFUSAL.isdisjoint(_NOTHING_OF_A_KEPT_TABLE)
    assert public == _THROUGH_THE_REFUSAL | set(_NOTHING_OF_A_KEPT_TABLE), sorted(
        public ^ (_THROUGH_THE_REFUSAL | set(_NOTHING_OF_A_KEPT_TABLE))
    )
    assert all(reason.strip() for reason in _NOTHING_OF_A_KEPT_TABLE.values())


@pytest.mark.slow
def test_no_other_caller_reads_a_table_for_a_task() -> None:
    """A new way to a table's rows or files must go through `load_table`, or be listed here.

    Three statements. Outside core, the refused read is called from the listed readers and
    from nowhere else. Inside the catalog, the refusal is called exactly from the methods placed on
    that side; each of them also has the not-published refusal, or is listed with the rule
    that stands in its place. And no module outside core reads Iceberg metadata or lists data
    files on its own, except the listed ones; none of them lists the files of a checked
    snapshot (`LoadedAt.data_files`) without a path check.
    """
    calls = _calls_of("lakana-server", _THROUGH_THE_REFUSAL) | _calls_of(
        "lakana-worker", _THROUGH_THE_REFUSAL
    )

    assert calls == set(_CALLS_OF_THE_REFUSED_READ)
    assert all(reason.strip() for reason in _CALLS_OF_THE_REFUSED_READ.values())

    tree = ast.parse(Path(service_module.__file__).read_text(encoding="utf-8"))
    service = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "CatalogService"
    )

    def callers_of(name: str) -> list[str]:
        """Name the methods of `CatalogService` that call one of its methods.

        Parameters
        ----------
        name
            The called method.

        Returns
        -------
        list of str
            The calling methods, sorted.
        """
        return sorted(
            method.name
            for method in service.body
            if isinstance(method, ast.FunctionDef)
            and method.name != name
            and any(
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == name
                for call in ast.walk(method)
            )
        )

    assert callers_of("_require_not_kept") == sorted(
        _THROUGH_THE_REFUSAL | set(_ASKS_THE_REFUSALS_AS_A_QUESTION)
    )
    assert callers_of("_require_published") == sorted(
        {"load_table", *_ASKS_THE_REFUSALS_AS_A_QUESTION}
    )
    assert all(reason.strip() for reason in _ASKS_THE_REFUSALS_AS_A_QUESTION.values())
    assert sorted(_THROUGH_THE_REFUSAL - set(callers_of("_require_published"))) == sorted(
        _READS_WHAT_IS_NOT_PUBLISHED
    )
    assert all(reason.strip() for reason in _READS_WHAT_IS_NOT_PUBLISHED.values())

    packages = Path(lakana_core.__file__).resolve().parents[3]
    direct = sorted(
        str(path.relative_to(packages))
        for package in ("lakana-server", "lakana-worker")
        for path in (packages / package / "src").rglob("*.py")
        if any(
            isinstance(node, (ast.Name, ast.Attribute))
            and (node.id if isinstance(node, ast.Name) else node.attr)
            in ("list_data_files", "read_metadata", "data_files")
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        )
    )
    assert direct == sorted(_READS_METADATA_DIRECTLY), direct
    unconfined = sorted(
        str(path.relative_to(packages))
        for package in ("lakana-server", "lakana-worker")
        for path in (packages / package / "src").rglob("*.py")
        if _listings_without_a_path_check(ast.parse(path.read_text(encoding="utf-8")))
    )
    assert unconfined == [], unconfined


def test_a_listing_of_a_checked_snapshot_without_a_path_check_is_found() -> None:
    """The guard has teeth: `data_files` without `is_valid_path`, or with `None`, is reported."""
    source = (
        "a = loaded.data_files(io)\n"
        "b = loaded.data_files(io, is_valid_path=None)\n"
        "c = loaded.data_files(io, is_valid_path=check)\n"
        "d = other.files(io)\n"
    )

    assert _listings_without_a_path_check(ast.parse(source)) == [1, 2]
