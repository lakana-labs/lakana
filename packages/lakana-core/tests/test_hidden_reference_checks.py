"""A generated reference check is withheld from a reader who may not read its target."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path

import pytest
from core_test_helpers import _prepared_catalog, python_task_run
from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.auth import (
    Action,
    AllowAllAuthorization,
    PrincipalRef,
    SecurableKind,
)
from lakana_core.catalog.service import CatalogService, Rollback
from lakana_core.errors import CheckNotInRunError
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.grants.roles import Role
from lakana_core.grants.service import GrantService
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.models import (
    AttemptOutcome,
    CheckOutcome,
    CheckResult,
    DeclaredReference,
    Environment,
    JobVersion,
    Principal,
    PrincipalType,
    RollbackOutcome,
    Run,
    Table,
    TaskAttempt,
    TaskRun,
)
from lakana_core.runs.check_rows import submit_check_rows
from lakana_core.runs.checks import (
    WITHHELD_TASK_MESSAGE,
    check_not_in_run_message,
    failure_message,
    warning_note,
)
from lakana_core.runs.hidden_checks import ReferenceCheckFilter
from lakana_core.runs.service import RunService
from lakana_core.storage.resolver import StorageResolver

_GOLD = NamespacePath.from_parts(["gold"])
_FACT = TableRef("dev", _GOLD, "fact_sales")
_DIM = TableRef("dev", _GOLD, "dim_store")
_REF = "gold.fact_sales/ref_store_id"
_NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)


class Star:
    """A run of `gold.fact_sales` with a key check, a reference check and a hand-written one.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        Storage root.
    admin
        The principal that sets it up and runs it.
    """

    def __init__(self, session: Session, tmp_path: Path, admin: PrincipalRef) -> None:
        self.session = session
        self.admin = admin
        _prepared_catalog(
            session, tmp_path, AllowAllAuthorization(), namespaces=[_GOLD], tables=[_FACT, _DIM]
        )
        task = python_task_run(session, admin.id, asset="gold.fact_sales", kind="sql")
        self.run_id = task.run_id
        version = session.get(
            JobVersion,
            session.execute(select(Run.job_version_id).where(Run.id == task.run_id)).scalar_one(),
        )
        assert version is not None
        version.manifest_json = {
            **version.manifest_json,
            "checks": [
                self._entry("key_unique", {"family": "key", "columns": ["store_id"]}),
                self._entry(
                    "ref_store_id",
                    {
                        "family": "references",
                        "columns": ["store_id"],
                        "target": "gold.dim_store",
                        "target_column": "store_id",
                    },
                ),
                self._entry("positive", None),
            ],
        }
        session.commit()
        fact_id = session.scalars(select(Table.id).where(Table.name == "fact_sales")).one()
        for check, generated in (("key_unique", True), ("ref_store_id", True), ("positive", False)):
            self.record(task, check, generated, fact_id)

    @staticmethod
    def _entry(check: str, generated: dict | None) -> dict:
        """Build a manifest entry of a check of the fact.

        Parameters
        ----------
        check
            Its short name.
        generated
            Its `generated` value; `None` for a hand-written check.

        Returns
        -------
        dict
            The entry.
        """
        entry = {
            "name": f"gold.fact_sales/{check}",
            "table": "gold.fact_sales",
            "check": check,
            "path": "assets/gold/fact_sales.sql:2",
            "on_failure": "block",
            "sql": "SELECT store_id FROM gold.fact_sales s "
            "LEFT JOIN gold.dim_store d USING (store_id) WHERE d.store_id IS NULL",
        }
        return entry if generated is None else {**entry, "generated": generated}

    def record(self, task: TaskRun, check: str, generated: bool, table_id: uuid.UUID) -> None:
        """Store one failed result, with two failing rows and a snapshot.

        Parameters
        ----------
        task
            The task run.
        check
            The check's short name.
        generated
            Whether it is generated.
        table_id
            The checked table.
        """
        environment = self.session.scalars(
            select(Environment).where(Environment.name == "dev")
        ).one()
        self.session.add(
            CheckResult(
                id=uuid.uuid4(),
                run_id=task.run_id,
                task_run_id=task.id,
                environment_id=environment.id,
                check_name=f"gold.fact_sales/{check}",
                check_path="assets/gold/fact_sales.sql:2",
                table_name="gold.fact_sales",
                table_id=table_id,
                on_failure="block",
                outcome=CheckOutcome.FAILED,
                failing_rows=2,
                checked_snapshot_id=42,
                started_at=_NOW - timedelta(seconds=1),
                finished_at=_NOW,
                generated=generated,
            )
        )
        self.session.commit()

    def person(self, *, tables: tuple[str, ...]) -> PrincipalRef:
        """Persist a user who may read the environment's runs and some tables of `gold`.

        Parameters
        ----------
        tables
            The tables it may read.

        Returns
        -------
        PrincipalRef
            The user.
        """
        name = f"p-{uuid.uuid4().hex[:8]}"
        ref = PrincipalRef(id=uuid.uuid4(), name=name, type=PrincipalType.USER)
        self.session.add(Principal(id=ref.id, type=ref.type, name=name, is_active=True))
        self.session.commit()
        grants = GrantService(self.session, AllowAllAuthorization())
        for action in (Action.READ_ENVIRONMENT, Action.SUBMIT_QUERY):
            grants.grant(
                self.admin,
                principal=name,
                action=action,
                kind=SecurableKind.ENVIRONMENT,
                environment="dev",
            )
        for table in tables:
            grants.grant(
                self.admin,
                principal=name,
                role=Role.VIEWER,
                kind=SecurableKind.TABLE,
                environment="dev",
                namespace="gold",
                table=table,
            )
        return ref

    def runs(self) -> RunService:
        """Build the run service under the real grants.

        Returns
        -------
        RunService
            The service.
        """
        return RunService(self.session, GrantAuthorization(self.session))

    def catalog(self) -> CatalogService:
        """Build the catalog under the real grants.

        Returns
        -------
        CatalogService
            The catalog.
        """
        return CatalogService(
            self.session, StorageResolver(self.session), GrantAuthorization(self.session)
        )


@pytest.fixture
def star(session: Session, tmp_path: Path, principal: PrincipalRef) -> Star:
    """Provide the run.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        Storage root.
    principal
        The administrator.

    Returns
    -------
    Star
        The run.
    """
    return Star(session, tmp_path, principal)


_EVERY = ["key_unique", "positive", "ref_store_id"]
_SHOWN = ["key_unique", "positive"]


def test_table_health_withholds_the_reference_check_and_counts_it(star: Star) -> None:
    reader = star.person(tables=("fact_sales",))
    health = star.catalog().table_health(reader, _FACT, days=30, now=_NOW)
    assert [c.check for c in health.checks] == _SHOWN
    assert health.hidden_checks == 1


def test_a_reader_of_the_target_sees_every_check(star: Star) -> None:
    both = star.person(tables=("fact_sales", "dim_store"))
    health = star.catalog().table_health(both, _FACT, days=30, now=_NOW)
    assert ([c.check for c in health.checks], health.hidden_checks) == (_EVERY, 0)
    allowed = CatalogService(star.session, StorageResolver(star.session), AllowAllAuthorization())
    every = allowed.table_health(star.admin, _FACT, days=30, now=_NOW)
    assert ([c.check for c in every.checks], every.hidden_checks) == (_EVERY, 0)


def test_runs_show_withholds_the_reference_check_and_counts_it(star: Star) -> None:
    reader = star.person(tables=("fact_sales",))
    (task,) = star.runs().get(reader, "dev", star.run_id).tasks
    assert [c.check for c in task.checks] == _SHOWN
    assert task.hidden_checks == 1
    (full,) = star.runs().get(star.person(tables=("dim_store",)), "dev", star.run_id).tasks
    assert ([c.check for c in full.checks], full.hidden_checks) == (_EVERY, 0)


def test_the_checks_of_a_run_withhold_the_reference_check(star: Star) -> None:
    reader = star.person(tables=("fact_sales",))
    assert [c.check for c in star.runs().checks(reader, "dev", star.run_id)] == _SHOWN


def test_check_list_withholds_the_reference_check_and_counts_it(star: Star) -> None:
    reader = star.person(tables=("fact_sales",))
    items, _, hidden = star.runs().list_checks(reader, "dev")
    assert sorted(c.check for c in items) == _SHOWN
    assert hidden == 1


def test_the_rows_of_a_withheld_check_are_refused_as_a_missing_check(star: Star) -> None:
    reader = star.person(tables=("fact_sales",))
    with pytest.raises(CheckNotInRunError) as withheld:
        submit_check_rows(
            star.session,
            GrantAuthorization(star.session),
            reader,
            environment="dev",
            run_id=star.run_id,
            check=_REF,
        )
    with pytest.raises(CheckNotInRunError) as missing:
        submit_check_rows(
            star.session,
            GrantAuthorization(star.session),
            reader,
            environment="dev",
            run_id=star.run_id,
            check="gold.fact_sales/ref_region_id",
        )
    assert str(withheld.value) == check_not_in_run_message(star.run_id, _REF, "dev")
    assert str(missing.value) == check_not_in_run_message(
        star.run_id, "gold.fact_sales/ref_region_id", "dev"
    )


def test_a_reference_check_whose_target_cannot_be_told_needs_every_table(star: Star) -> None:
    """A project version without the check's entry leaves only the environment-wide right."""
    version = star.session.get(
        JobVersion,
        star.session.execute(select(Run.job_version_id).where(Run.id == star.run_id)).scalar_one(),
    )
    assert version is not None
    version.manifest_json = {**version.manifest_json, "checks": []}
    star.session.commit()
    of_target = star.person(tables=("fact_sales", "dim_store"))
    authz = GrantAuthorization(star.session)
    assert ReferenceCheckFilter(star.session, authz, of_target, "dev").hides(
        star.run_id, _REF, True
    )
    assert not ReferenceCheckFilter(star.session, AllowAllAuthorization(), star.admin, "dev").hides(
        star.run_id, _REF, True
    )


def test_a_stored_target_that_is_no_table_name_needs_every_table(star: Star) -> None:
    version = star.session.get(
        JobVersion,
        star.session.execute(select(Run.job_version_id).where(Run.id == star.run_id)).scalar_one(),
    )
    assert version is not None
    checks = [dict(c) for c in version.manifest_json["checks"]]
    checks[1]["generated"] = {**checks[1]["generated"], "target": "nodot"}
    version.manifest_json = {**version.manifest_json, "checks": checks}
    star.session.commit()
    reader = star.person(tables=("fact_sales", "dim_store"))
    assert ReferenceCheckFilter(
        star.session, GrantAuthorization(star.session), reader, "dev"
    ).hides(star.run_id, _REF, True)


def test_key_and_hand_written_checks_are_never_withheld(star: Star) -> None:
    nobody = star.person(tables=())
    only = ReferenceCheckFilter(star.session, GrantAuthorization(star.session), nobody, "dev")
    assert not only.hides(star.run_id, "gold.fact_sales/key_unique", True)
    assert not only.hides(star.run_id, "gold.fact_sales/ref_like_name", False)
    assert only.hides(star.run_id, _REF, True)


def test_the_filter_reads_a_runs_project_version_and_a_right_once(star: Star) -> None:
    reader = star.person(tables=("fact_sales",))
    only = ReferenceCheckFilter(star.session, GrantAuthorization(star.session), reader, "dev")
    assert only.hides(star.run_id, _REF, True)
    assert only.hides(star.run_id, _REF, True)


def test_table_get_withholds_a_result_whose_runs_target_is_hidden_after_the_target_changed(
    star: Star,
) -> None:
    """The run's version pointed `store_id` at `dim_store`; the header now points it elsewhere.

    The newest `ref_store_id` result still says something about `dim_store`, so a reader who may
    read the new target but not `dim_store` is not shown it in the Model block, as table health
    does not show it.
    """
    fact = star.session.scalars(select(Table).where(Table.name == "fact_sales")).one()
    fact.model_kind = "fact"
    fact.model_grain = "One row per store."
    fact.model_key = ["store_id"]
    star.session.add(
        DeclaredReference(
            id=uuid.uuid4(),
            environment_id=star.session.scalars(
                select(Environment.id).where(Environment.name == "dev")
            ).one(),
            source_table_id=fact.id,
            source_column="store_id",
            target_name="gold.fact_sales",
            target_column="store_id",
        )
    )
    star.session.commit()
    reader = star.person(tables=("fact_sales",))
    model = star.catalog().describe_table(reader, _FACT).model
    assert model is not None
    assert [r.id for r in model.references] == [_REF]
    assert [c.check for c in model.checks] == ["key_unique"]
    both = star.person(tables=("fact_sales", "dim_store"))
    shown = star.catalog().describe_table(both, _FACT).model
    assert shown is not None
    assert [c.check for c in shown.checks] == ["key_unique", "ref_store_id"]


def _set_task_message(star: Star, message: str, *, warned: bool = False) -> None:
    """Store a task message as the worker would, and mark the reference result warned or not.

    Parameters
    ----------
    star
        The run.
    message
        The task's `error_message`.
    warned
        Whether the reference check is a `warn` check that found one row.
    """
    task = star.session.scalars(select(TaskRun).where(TaskRun.run_id == star.run_id)).one()
    task.error_message = message
    if warned:
        ref = star.session.scalars(select(CheckResult).where(CheckResult.check_name == _REF)).one()
        ref.outcome, ref.on_failure, ref.failing_rows = CheckOutcome.WARNED, "warn", 1
    star.session.commit()


def _results(star: Star) -> list[CheckResult]:
    """Read the run's results by name.

    Parameters
    ----------
    star
        The run.

    Returns
    -------
    list of CheckResult
        Every result of the run.
    """
    return list(
        star.session.scalars(
            select(CheckResult)
            .where(CheckResult.run_id == star.run_id)
            .order_by(CheckResult.check_name)
        )
    )


def test_the_task_message_leaves_out_a_withheld_warned_reference_check(star: Star) -> None:
    """The how-to's case: `note: 1 check warned: `ref_sku` (1 row).` names the count."""
    _set_task_message(star, "", warned=True)
    (ref,) = [r for r in _results(star) if r.check_name == _REF]
    _set_task_message(star, warning_note([ref]))
    reader = star.person(tables=("fact_sales",))
    (task,) = star.runs().get(reader, "dev", star.run_id).tasks
    assert task.error_message is None
    assert task.hidden_checks == 1
    both = star.person(tables=("fact_sales", "dim_store"))
    (full,) = star.runs().get(both, "dev", star.run_id).tasks
    assert full.error_message == "1 check warned: `ref_store_id` (1 row)."


def test_the_task_message_of_failed_checks_is_rebuilt_without_the_withheld_one(star: Star) -> None:
    every = _results(star)
    build = partial(
        failure_message,
        table="gold.fact_sales",
        run_id=star.run_id,
        environment="dev",
        rollback=Rollback(RollbackOutcome.ROLLED_BACK),
    )
    _set_task_message(star, build(every))
    reader = star.person(tables=("fact_sales",))
    (task,) = star.runs().get(reader, "dev", star.run_id).tasks
    visible = [r for r in every if r.check_name != _REF]
    assert task.error_message == build(visible)
    assert "ref_store_id" not in (task.error_message or "")
    both = star.person(tables=("fact_sales", "dim_store"))
    (full,) = star.runs().get(both, "dev", star.run_id).tasks
    assert full.error_message == build(every)


def test_a_task_message_that_names_a_withheld_check_otherwise_is_replaced(star: Star) -> None:
    _set_task_message(star, "Something about `gold.fact_sales/ref_store_id` the rebuild misses.")
    reader = star.person(tables=("fact_sales",))
    (task,) = star.runs().get(reader, "dev", star.run_id).tasks
    assert task.error_message == WITHHELD_TASK_MESSAGE


def test_an_attempts_message_in_the_task_log_leaves_out_the_withheld_check(star: Star) -> None:
    _set_task_message(star, "", warned=True)
    (ref,) = [r for r in _results(star) if r.check_name == _REF]
    task = star.session.scalars(select(TaskRun).where(TaskRun.run_id == star.run_id)).one()
    star.session.add(
        TaskAttempt(
            id=uuid.uuid4(),
            task_run_id=task.id,
            attempt=1,
            worker="w1",
            started_at=_NOW - timedelta(seconds=2),
            finished_at=_NOW,
            outcome=AttemptOutcome.SUCCEEDED,
            error_message=warning_note([ref]),
        )
    )
    star.session.commit()
    reader = star.person(tables=("fact_sales",))
    (attempt,) = star.runs().task_log(reader, "dev", star.run_id, "gold.fact_sales").attempts
    assert attempt.error_message is None
    both = star.person(tables=("fact_sales", "dim_store"))
    (full,) = star.runs().task_log(both, "dev", star.run_id, "gold.fact_sales").attempts
    assert full.error_message == "1 check warned: `ref_store_id` (1 row)."
