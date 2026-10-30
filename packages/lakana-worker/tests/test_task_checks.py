"""The check phase: the verdict, the rollback and the log of a task's checks."""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest
from check_run_helpers import (
    _DUPLICATE_IDS,
    _PASSING,
    _SEE_YOURSELF,
    _UNPUBLISHED,
    _after,
    _built_once_then_redeployed,
    _check,
    _checked,
    _claim_ready,
    _counting,
    _counts,
    _drain,
    _events,
    _ids,
    _keep_note_on,
    _marker,
    _outside_table,
    _refused_text,
    _results,
    _rows,
    _run_until,
    _source_checked,
    _tick,
    _unique_rows,
    _writes,
)
from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from task_run_helpers import (
    World,
    _attempts,
    _engine,
    _entry,
    _exists,
    _log,
    _start,
    _task,
    _three_principals,
)

import lakana_worker.checks as checks_module
import lakana_worker.task_checks as task_checks_module
import lakana_worker.tasks as tasks_module
from lakana_core.auth import (
    Action,
    AllowAllAuthorization,
    PrincipalRef,
    Securable,
)
from lakana_core.catalog.identity import resolve_table_id
from lakana_core.catalog.service import (
    ROLLBACK_NOT_KNOWN_REASON,
    ROLLBACK_REFUSED_REASON,
    CatalogService,
    Rollback,
)
from lakana_core.errors import (
    PermissionDenied,
    ValidationError,
)
from lakana_core.identifiers import TableRef
from lakana_core.models import (
    AttemptOutcome,
    AuditEvent,
    AuditEventType,
    CheckOutcome,
    JobVersion,
    Principal,
    QueryRunStatus,
    RollbackOutcome,
    Run,
    RunStatus,
    TaskStatus,
    WriteMode,
)
from lakana_core.policy.read_errors import policy_read_failure
from lakana_core.projects.checks import CheckSpec, GeneratedFrom
from lakana_core.runs.check_message import BLOCKING, PASSED, CheckMessageState, check_message
from lakana_core.runs.scheduler import COMMITTED_BEFORE_RECORDED_MESSAGE
from lakana_core.runs.tasks import (
    FailureKind,
)
from lakana_worker.task_outcome import (
    TaskOutcome,
)
from lakana_worker.tasks import run_claimed


def test_a_failed_blocking_check_is_permanent_and_the_build_runs_once(world: World) -> None:
    """A failed check is never retried into a second write."""
    _checked(world, _check("unique_id", _DUPLICATE_IDS))
    run_id = _start(world)
    _drain(world)

    task = _task(world, "silver.orders")
    assert (task.status, task.attempt, task.max_attempts) == (TaskStatus.FAILED, 1, 3)
    assert task.error_message == (
        "Check `silver.orders/unique_id` failed: 1 row (checks/silver/orders/unique_id.sql). "
        f"{_UNPUBLISHED} {_unique_rows(run_id)}{_after(run_id)}"
    )
    assert len(_writes(world, task.id)) == 1
    (result,) = _results(world, task.id)
    assert (result.outcome, result.failing_rows, result.rollback_outcome) == (
        CheckOutcome.FAILED,
        1,
        RollbackOutcome.UNPUBLISHED,
    )
    assert (result.table_id, result.policy_applied, result.error) == (
        _writes(world, task.id)[0].target_table_id,
        False,
        None,
    )
    assert result.checked_snapshot_id == _writes(world, task.id)[0].snapshot_id
    (count,) = _counts(world, task.id)
    assert (result.query_run_id, count.status, count.result_location, count.principal_id) == (
        count.id,
        QueryRunStatus.SUCCEEDED,
        None,
        world.admin.id,
    )
    assert [a.outcome for a in _attempts(world, task.id)] == [AttemptOutcome.FAILED]
    assert (task.failure_code, _attempts(world, task.id)[0].failure_code) == (
        "check_blocked",
        "check_blocked",
    )
    assert _marker(world, "silver.orders") == run_id
    assert task.written_table_id is None  # the rollback cleared the stamp
    assert _task(world, "gold.report").status is TaskStatus.UPSTREAM_FAILED
    assert not _exists(world, "gold.report")
    (rolled,) = _events(world, AuditEventType.TABLE_ROLLED_BACK)
    assert (rolled["outcome"], rolled["checks"]) == ("unpublished", ["silver.orders/unique_id"])


@pytest.mark.parametrize("on_failure", ["block", "warn"])
def test_the_stored_state_gives_back_the_stored_message_from_the_results(
    world: World, on_failure: str
) -> None:
    """The worker words its message with the builder a reader's view uses, and stores its state."""
    _checked(world, _check("unique_id", _DUPLICATE_IDS, on_failure=on_failure))
    run_id = _start(world)
    _drain(world)

    task = _task(world, "silver.orders")
    state = CheckMessageState.from_json(task.check_message_state)
    assert state is not None
    assert state.branch == (BLOCKING if on_failure == "block" else PASSED)
    rebuilt = check_message(
        _results(world, task.id), state, table="silver.orders", run_id=run_id, environment="dev"
    )
    assert rebuilt == task.error_message is not None
    (attempt,) = _attempts(world, task.id)
    assert (attempt.error_message, attempt.check_message_state) == (
        task.error_message,
        task.check_message_state,
    )


@pytest.mark.slow
def test_passing_checks_leave_the_task_succeeded(world: World) -> None:
    _checked(
        world,
        _check("has_rows", "SELECT 1 AS missing WHERE (SELECT count(*) FROM silver.orders) = 0"),
        _check("no_nulls", "SELECT id FROM silver.orders WHERE id IS NULL"),
    )
    _start(world)
    _drain(world)

    task = _task(world, "silver.orders")
    assert (task.status, task.error_message, task.attempt) == (TaskStatus.SUCCEEDED, None, 1)
    results = _results(world, task.id)
    assert [(r.check_name, r.outcome, r.failing_rows, r.rollback_outcome) for r in results] == [
        ("silver.orders/has_rows", CheckOutcome.PASSED, 0, None),
        ("silver.orders/no_nulls", CheckOutcome.PASSED, 0, None),
    ]
    assert _ids(world, "silver.orders") == [1, 1, 2]
    assert _marker(world, "silver.orders") is None
    assert _task(world, "gold.report").status is TaskStatus.SUCCEEDED
    assert _events(world, AuditEventType.TABLE_ROLLED_BACK) == []
    assert _events(world, AuditEventType.CHECK_FAILED) == []
    (attempt,) = _attempts(world, task.id)
    assert (attempt.outcome, attempt.rows_written) == (AttemptOutcome.SUCCEEDED, 3)


@pytest.mark.slow
def test_a_warning_check_does_not_block(world: World) -> None:
    _checked(world, _check("unique_id", _DUPLICATE_IDS, on_failure="warn"))
    _start(world)
    _drain(world)

    task = _task(world, "silver.orders")
    assert (task.status, task.error_message) == (
        TaskStatus.SUCCEEDED,
        "1 check warned: `unique_id` (1 row).",
    )
    (result,) = _results(world, task.id)
    assert (result.outcome, result.failing_rows, result.rollback_outcome) == (
        CheckOutcome.WARNED,
        1,
        None,
    )
    assert _ids(world, "silver.orders") == [1, 1, 2]
    assert _marker(world, "silver.orders") is None
    assert _task(world, "gold.report").status is TaskStatus.SUCCEEDED
    assert _events(world, AuditEventType.TABLE_ROLLED_BACK) == []


@pytest.mark.slow
def test_every_check_runs_also_after_one_failed(world: World) -> None:
    """Spec section 5: one run reports every failure."""
    _checked(
        world,
        _check("a_unique", _DUPLICATE_IDS),
        _check("b_small", "SELECT id FROM silver.orders WHERE id > 1"),
        _check("c_no_nulls", "SELECT id FROM silver.orders WHERE id IS NULL"),
    )
    run_id = _start(world)
    _drain(world)

    task = _task(world, "silver.orders")
    results = _results(world, task.id)
    assert [(r.check_name.rpartition("/")[2], r.outcome, r.failing_rows) for r in results] == [
        ("a_unique", CheckOutcome.FAILED, 1),
        ("b_small", CheckOutcome.FAILED, 1),
        ("c_no_nulls", CheckOutcome.PASSED, 0),
    ]
    assert [r.rollback_outcome for r in results] == [
        RollbackOutcome.UNPUBLISHED,
        RollbackOutcome.UNPUBLISHED,
        None,
    ]
    assert (task.status, task.error_message) == (
        TaskStatus.FAILED,
        "2 checks of `silver.orders` failed: `a_unique` (1 row), `b_small` (1 row). "
        f"{_UNPUBLISHED} {_rows(run_id, 'silver.orders/a_unique', 'silver.orders/b_small')}"
        f"{_after(run_id)}",
    )
    assert len(_events(world, AuditEventType.CHECK_FAILED)) == 2


def test_a_check_that_cannot_run_blocks_and_rolls_back(world: World) -> None:
    """Spec section 5: data that could not be checked is not published."""
    _checked(world, _check("broken", "SELECT nope FROM silver.orders WHERE nope > 0"))
    run_id = _start(world)
    _drain(world)

    task = _task(world, "silver.orders")
    error = f"Binder Error: the check's query failed. {_SEE_YOURSELF}"
    assert (task.status, task.attempt) == (TaskStatus.FAILED, 1)
    assert task.failure_code == "check_error"
    assert task.error_message == (
        f"Check `silver.orders/broken` could not run (checks/silver/orders/broken.sql): {error} "
        f"Data that could not be checked is not published. {_UNPUBLISHED} {_after(run_id)}"
    )
    (result,) = _results(world, task.id)
    assert (result.outcome, result.failing_rows, result.error, result.rollback_outcome) == (
        CheckOutcome.ERRORED,
        None,
        error,
        RollbackOutcome.UNPUBLISHED,
    )
    assert _marker(world, "silver.orders") == run_id
    (failed,) = _events(world, AuditEventType.CHECK_FAILED)
    assert (failed["outcome"], failed["rows"]) == ("errored", None)


def test_a_warning_check_that_cannot_run_is_recorded_and_does_not_block(world: World) -> None:
    _checked(
        world,
        _check("broken", "SELECT nope FROM silver.orders WHERE nope > 0", on_failure="warn"),
    )
    _start(world)
    _drain(world)

    task = _task(world, "silver.orders")
    assert (task.status, task.error_message) == (
        TaskStatus.SUCCEEDED,
        "1 check warned: `broken` (could not run).",
    )
    (result,) = _results(world, task.id)
    assert (result.outcome, result.rollback_outcome) == (CheckOutcome.ERRORED, None)
    assert result.error == f"Binder Error: the check's query failed. {_SEE_YOURSELF}"
    assert _ids(world, "silver.orders") == [1, 1, 2]
    assert _task(world, "gold.report").status is TaskStatus.SUCCEEDED


class _NoSecret(AllowAllAuthorization):
    """Allow-all, except that nobody may read a table called `secret`."""

    def permits(self, principal: PrincipalRef, action: Action, securable: Securable) -> bool:
        """Refuse `READ_TABLE` on `secret`; permit anything else.

        Parameters
        ----------
        principal
            The actor.
        action
            The action.
        securable
            The object.

        Returns
        -------
        bool
            Whether the action is permitted.
        """
        return not (action is Action.READ_TABLE and securable.table == "secret")

    def authorize(self, principal: PrincipalRef, action: Action, securable: Securable) -> None:
        """Raise as `permits` answers.

        Parameters
        ----------
        principal
            The actor.
        action
            The action.
        securable
            The object.

        Raises
        ------
        PermissionDenied
            When `permits` is false.
        """
        if not self.permits(principal, action, securable):
            raise PermissionDenied("may not read gold.secret")


def _generated(name: str, sql: str, generated: dict[str, object]) -> dict[str, object]:
    """Build the manifest entry of a check generated from the header of `silver.orders`.

    Parameters
    ----------
    name
        The check's short name.
    sql
        Its SELECT.
    generated
        The stored ``generated`` value: the header field it comes from.

    Returns
    -------
    dict
        The entry, at the asset file and header line.
    """
    return {
        **_check(name, sql),
        "path": "assets/silver/orders.sql:1",
        "generated": {"target": None, "target_column": None, **generated},
    }


def test_a_generated_key_check_on_a_float_column_errors_without_a_count(world: World) -> None:
    """The pre-check reads the written column types; `block` sets the table back."""
    _checked(
        world,
        _generated(
            "key_unique",
            "SELECT amount FROM silver.orders GROUP BY amount HAVING count(*) > 1",
            {"family": "key", "columns": ["amount"]},
        ),
        sql="SELECT id, id::DOUBLE AS amount FROM bronze.src",
    )
    run_id = _start(world)
    _drain(world)

    task = _task(world, "silver.orders")
    error = (
        "Key column `amount` is double, a floating-point type, so it cannot identify a row; cast "
        "it in the asset, for example to DECIMAL, BIGINT or VARCHAR, and deploy again."
    )
    (result,) = _results(world, task.id)
    assert (result.outcome, result.error, result.failing_rows, result.generated) == (
        CheckOutcome.ERRORED,
        error,
        None,
        True,
    )
    assert result.rollback_outcome is RollbackOutcome.UNPUBLISHED
    assert _counts(world, task.id) == []
    assert (task.status, task.failure_code) == (TaskStatus.FAILED, "check_error")
    assert task.error_message == (
        "Check `silver.orders/key_unique` could not run (assets/silver/orders.sql:1): "
        f"{error} Data that could not be checked is not published. {_UNPUBLISHED} "
        f"{_after(run_id)}"
    )
    assert _marker(world, "silver.orders") == run_id


def test_a_generated_reference_to_an_unreadable_target_says_gone_or_unreadable(
    world: World,
) -> None:
    """One text for a target that is gone and one the run's principal may not read."""
    _outside_table(world, "gold.secret", [1])
    _checked(
        world,
        _generated(
            "ref_id",
            "SELECT o.id FROM silver.orders o LEFT JOIN gold.secret s ON s.id = o.id "
            "WHERE o.id IS NOT NULL AND s.id IS NULL",
            {
                "family": "references",
                "columns": ["id"],
                "target": "gold.secret",
                "target_column": "id",
            },
        ),
    )
    _start(world)
    _drain(world, _engine(world, authz=lambda _s: _NoSecret()))

    task = _task(world, "silver.orders")
    (result,) = _results(world, task.id)
    assert (result.outcome, result.error, result.generated) == (
        CheckOutcome.ERRORED,
        "The reference `id` at `assets/silver/orders.sql:1` cannot be checked: its target is "
        "missing, not readable by the run's principal, or no longer has the key column; fix the "
        "reference or the grant and deploy again.",
        True,
    )
    assert _counts(world, task.id) == []
    assert task.status is TaskStatus.FAILED


def test_the_log_gives_a_reference_check_no_count_and_its_error_no_name(world: World) -> None:
    """The log is one text for every reader: a reference check's detail is in `runs show`."""
    _outside_table(world, "gold.dim", [1])
    _checked(
        world,
        _check("a_unique", _DUPLICATE_IDS),
        _generated(
            "ref_id",
            "SELECT o.id FROM silver.orders o LEFT JOIN gold.dim d ON d.id = o.id "
            "WHERE o.id IS NOT NULL AND d.id IS NULL",
            {
                "family": "references",
                "columns": ["id"],
                "target": "gold.dim",
                "target_column": "id",
            },
        ),
    )
    run_id = _start(world)
    _drain(world)

    task = _task(world, "silver.orders")
    assert [(r.check_name, r.failing_rows) for r in _results(world, task.id)] == [
        ("silver.orders/a_unique", 1),
        ("silver.orders/ref_id", 1),
    ]
    log = _log(world, run_id, "silver.orders")
    assert "  check a_unique: failed, 1 row\n" in log
    assert "  check ref_id: done; see lakana runs show\n" in log
    error = log.split("  error: ", 1)[1]
    assert error.startswith("Check `silver.orders/a_unique` failed: 1 row")
    assert "ref_id" not in error and "2 checks" not in error


@pytest.mark.parametrize(
    ("target", "target_column", "orders_sql"),
    [
        ("gold.gone", "id", "SELECT id FROM bronze.src"),
        ("gold.dim", "nope", "SELECT id FROM bronze.src"),
        ("gold.dim", "id", "SELECT id::VARCHAR AS id FROM bronze.src"),
    ],
    ids=["target-gone", "target-column-gone", "types-differ"],
)
def test_a_generated_reference_error_never_names_its_target(
    world: World, target: str, target_column: str, orders_sql: str
) -> None:
    """A reader of the run may not read the target: the error points at the header line only."""
    _outside_table(world, "gold.dim", [1])
    _checked(
        world,
        _generated(
            "ref_id",
            f"SELECT o.id FROM silver.orders o LEFT JOIN {target} t ON t.id = o.id "
            "WHERE o.id IS NOT NULL AND t.id IS NULL",
            {
                "family": "references",
                "columns": ["id"],
                "target": target,
                "target_column": target_column,
            },
        ),
        sql=orders_sql,
    )
    _start(world)
    _drain(world)

    task = _task(world, "silver.orders")
    (result,) = _results(world, task.id)
    assert (result.outcome, result.generated) == (CheckOutcome.ERRORED, True)
    assert result.error is not None
    assert result.error.startswith("The reference `id` at `assets/silver/orders.sql:1` ")
    assert target not in result.error
    assert target_column not in result.error.replace("`id`", "")
    assert "long" not in result.error.replace("longer", "")
    assert _counts(world, task.id) == []


def _precheck_lost_the_database(*args: object, **kwargs: object) -> str | None:
    """Stand in for a pre-check whose read lost the database.

    Parameters
    ----------
    *args
        Ignored.
    **kwargs
        Ignored.

    Raises
    ------
    OperationalError
        Always, with a text that must never be shown.
    """
    raise OperationalError("SELECT table_columns", {}, Exception("Alice went away"))


def test_a_database_error_in_the_precheck_is_retried_like_a_failed_count(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A blip while reading the stored columns is not a failed check; nothing is set back."""
    _checked(
        world,
        _generated(
            "key_not_null",
            "SELECT id FROM silver.orders WHERE id IS NULL",
            {"family": "key", "columns": ["id"]},
        ),
    )
    _start(world)
    claimed = _run_until(world, "silver.orders")
    monkeypatch.setattr(task_checks_module, "model_problem", _precheck_lost_the_database)
    monkeypatch.setattr(
        CatalogService, "roll_back", lambda *a, **k: pytest.fail("rolled back on a blip")
    )

    run_claimed(_engine(world), claimed)

    task = _task(world, "silver.orders")
    assert (task.status, task.attempt, task.error_message) == (
        TaskStatus.PENDING,
        1,
        "Check `silver.orders/key_not_null` could not be counted right now (internal); the checks "
        "run again on the next attempt. The table is not built again.",
    )
    assert _results(world, task.id) == []
    assert _counts(world, task.id) == []


def test_a_database_error_in_the_precheck_on_the_last_attempt_is_an_errored_check(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The last-attempt rule of a count holds for the pre-check: fail closed, name the type only."""
    _checked(
        world,
        _generated(
            "key_not_null",
            "SELECT id FROM silver.orders WHERE id IS NULL",
            {"family": "key", "columns": ["id"]},
        ),
        retries=0,
    )
    _start(world)
    claimed = _run_until(world, "silver.orders")
    monkeypatch.setattr(task_checks_module, "model_problem", _precheck_lost_the_database)

    run_claimed(_engine(world), claimed)

    task = _task(world, "silver.orders")
    (result,) = _results(world, task.id)
    assert (result.outcome, result.error, result.generated) == (
        CheckOutcome.ERRORED,
        "the check could not be counted: the worker hit an unexpected error (OperationalError). "
        "See the worker's log.",
        True,
    )
    assert (task.status, task.attempt) == (TaskStatus.FAILED, 1)
    assert task.error_message is not None and "Alice" not in task.error_message


def test_a_generated_check_that_passes_the_precheck_is_counted_and_marked(world: World) -> None:
    """A generated check is counted like any other; only its result's mark differs."""
    _checked(
        world,
        _generated(
            "key_not_null",
            "SELECT id FROM silver.orders WHERE id IS NULL",
            {"family": "key", "columns": ["id"]},
        ),
        _check("no_nulls", "SELECT id FROM silver.orders WHERE id IS NULL"),
    )
    _start(world)
    _drain(world)

    task = _task(world, "silver.orders")
    assert task.status is TaskStatus.SUCCEEDED
    assert [(r.check_name, r.outcome, r.generated) for r in _results(world, task.id)] == [
        ("silver.orders/key_not_null", CheckOutcome.PASSED, True),
        ("silver.orders/no_nulls", CheckOutcome.PASSED, False),
    ]
    assert len(_counts(world, task.id)) == 2


def test_a_check_the_run_principal_may_not_read_names_the_check(world: World) -> None:
    """The message names the check and the principal, and not the other table."""
    _outside_table(world, "gold.secret", [1])
    _checked(
        world,
        _check(
            "known_key",
            "SELECT o.id FROM silver.orders o LEFT JOIN gold.secret s ON s.id = o.id "
            "WHERE s.id IS NULL",
        ),
    )
    run_id = _start(world)
    _drain(world, _engine(world, authz=lambda _s: _NoSecret()))

    task = _task(world, "silver.orders")
    error = (
        "the run's principal `admin` may not read a table this check names, or the table does "
        "not exist. Grant it READ on the tables the check reads, or change the check."
    )
    (result,) = _results(world, task.id)
    assert (result.outcome, result.error) == (CheckOutcome.ERRORED, error)
    assert task.status is TaskStatus.FAILED
    assert task.error_message == (
        "Check `silver.orders/known_key` could not run (checks/silver/orders/known_key.sql): "
        f"{error} Data that could not be checked is not published. {_UNPUBLISHED} "
        f"{_after(run_id)}"
    )
    log = _log(world, run_id, "silver.orders")
    for text in (result.error or "", task.error_message or "", log):
        assert "secret" not in text and "gold." not in text


def test_a_rollback_refused_by_the_guard_is_reported_as_failed(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refusal while the claim is held and the run is live: the rollback failed, and why."""
    _checked(world, _check("unique_id", _DUPLICATE_IDS))
    run_id = _start(world)

    def refuse(self: CatalogService, *args: object, **kwargs: object) -> Rollback:
        raise PermissionDenied("x")

    monkeypatch.setattr(CatalogService, "roll_back", refuse)
    _drain(world)

    task = _task(world, "silver.orders")
    assert (task.status, task.error_message) == (
        TaskStatus.FAILED,
        "Check `silver.orders/unique_id` failed: 1 row (checks/silver/orders/unique_id.sql). "
        "The rollback was refused (permission denied: x); it did not change the table. "
        f"Nothing downstream was built. {_unique_rows(run_id)}{_after(run_id)}",
    )
    (result,) = _results(world, task.id)
    assert result.rollback_outcome is RollbackOutcome.FAILED
    assert _ids(world, "silver.orders") == [1, 1, 2]
    assert _task(world, "gold.report").status is TaskStatus.UPSTREAM_FAILED


def test_a_source_check_task_only_checks(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """Decision 7: a table the project only reads is checked, never built and never set back."""
    _source_checked(world, "SELECT id FROM bronze.events GROUP BY id HAVING count(*) > 1")
    run_id = _start(world)
    monkeypatch.setattr(
        CatalogService, "roll_back", lambda *a, **k: pytest.fail("a source check rolled back")
    )
    monkeypatch.setattr(tasks_module, "_dispatch", lambda *a, **k: pytest.fail("built a source"))
    claimed = _claim_ready(world)
    assert claimed is not None
    assert (claimed.asset, claimed.kind) == ("bronze.events", "source_check")

    run_claimed(_engine(world), claimed)

    task = _task(world, "bronze.events")
    assert (task.status, task.error_message) == (
        TaskStatus.FAILED,
        "Check `bronze.events/unique_id` failed: 1 row (checks/bronze/events/unique_id.sql). This "
        "project does not build `bronze.events`, so it was not changed; the assets that read it "
        f"were not built. {_rows(run_id, 'bronze.events/unique_id')}{_after(run_id)}",
    )
    assert _writes(world, task.id) == []
    (result,) = _results(world, task.id)
    assert (result.outcome, result.failing_rows, result.rollback_outcome) == (
        CheckOutcome.FAILED,
        1,
        None,
    )
    ref = TableRef.from_dotted("dev", "bronze.events")
    with world.session() as session:
        assert result.table_id == resolve_table_id(session, "dev", ref.namespace.joined(), "events")
    assert result.checked_snapshot_id is not None
    assert _ids(world, "bronze.events") == [1, 1, 2]
    _tick(world)
    assert _task(world, "silver.orders").status is TaskStatus.UPSTREAM_FAILED
    assert not _exists(world, "silver.orders")
    log = _log(world, run_id, "bronze.events")
    assert "  check unique_id: failed, 1 row\n" in log and "rollback" not in log


def test_a_source_check_that_passes_lets_its_readers_build(world: World) -> None:
    _source_checked(world, "SELECT id FROM bronze.events WHERE id IS NULL")
    _start(world)
    _drain(world)

    source = _task(world, "bronze.events")
    assert (source.status, source.error_message) == (TaskStatus.SUCCEEDED, None)
    assert [r.outcome for r in _results(world, source.id)] == [CheckOutcome.PASSED]
    assert _task(world, "silver.orders").status is TaskStatus.SUCCEEDED
    assert _ids(world, "silver.orders") == [1, 1, 2]


def test_a_source_check_of_a_deactivated_principal_fails_permanently(world: World) -> None:
    """Nothing was written, so the task fails as a build would: no count, no result."""
    _source_checked(world, "SELECT id FROM bronze.events WHERE id IS NULL")
    _start(world)
    with world.session() as session:
        session.execute(
            update(Principal).where(Principal.id == world.admin.id).values(is_active=False)
        )
        session.commit()
    claimed = _claim_ready(world)
    assert claimed is not None

    run_claimed(_engine(world), claimed)

    task = _task(world, "bronze.events")
    assert (task.status, task.attempt, task.error_message) == (
        TaskStatus.FAILED,
        1,
        "the run principal is deactivated or deleted",
    )
    assert task.failure_code == "permission_denied"
    assert _results(world, task.id) == []
    assert _counts(world, task.id) == []


def test_a_task_without_checks_runs_exactly_as_before(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No check, no second phase, no second keeper, no read of results."""
    monkeypatch.setattr(tasks_module, "check_phase", lambda *a: pytest.fail("a phase ran"))
    monkeypatch.setattr(tasks_module, "before_build", lambda *a: pytest.fail("results read"))
    run_id = _start(world)
    _drain(world)
    assert _task(world, "silver.clean").status is TaskStatus.SUCCEEDED
    assert _task(world, "gold.daily").status is TaskStatus.SUCCEEDED
    assert "check" not in _log(world, run_id, "gold.daily")


@pytest.mark.slow
def test_the_task_log_lists_the_checks_and_the_rollback(world: World) -> None:
    _checked(
        world,
        _check("a_unique", _DUPLICATE_IDS),
        _check("b_no_nulls", "SELECT id FROM silver.orders WHERE id IS NULL"),
        _check("c_small", "SELECT id FROM silver.orders WHERE id > 0", on_failure="warn"),
        _check("d_broken", "SELECT nope FROM silver.orders WHERE nope > 0", on_failure="warn"),
    )
    run_id = _start(world)
    _drain(world)

    log = _log(world, run_id, "silver.orders")
    assert (
        "  rows: 3\n"
        "  check a_unique: failed, 1 row\n"
        "  check b_no_nulls: passed\n"
        "  check c_small: warned, 3 rows\n"
        f"  check d_broken: errored: Binder Error: the check's query failed. {_SEE_YOURSELF}\n"
        "  rollback: unpublished\n"
        "  error: Check `silver.orders/a_unique` failed: 1 row"
    ) in log


def test_a_rollback_whose_outcome_is_not_known_is_not_called_again(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Additions B.4 and B.5: one call, no record of an outcome, the not-known wording."""
    _checked(world, _check("unique_id", _DUPLICATE_IDS))
    run_id = _start(world)
    calls: list[int] = []

    def not_known(self: CatalogService, *args: object, **kwargs: object) -> Rollback:
        calls.append(1)
        return Rollback(RollbackOutcome.FAILED, ROLLBACK_NOT_KNOWN_REASON)

    monkeypatch.setattr(CatalogService, "roll_back", not_known)
    monkeypatch.setattr(
        task_checks_module,
        "mark_rollback_failed",
        lambda *a, **k: pytest.fail("an outcome was recorded for a rollback nobody knows about"),
    )
    _drain(world)

    task = _task(world, "silver.orders")
    assert calls == [1]
    assert (task.status, task.attempt, task.error_message) == (
        TaskStatus.FAILED,
        1,
        "Check `silver.orders/unique_id` failed: 1 row (checks/silver/orders/unique_id.sql). "
        "The rollback was interrupted and whether it took effect is not known; look at the "
        "table with `lakana table get silver.orders --env dev`. Nothing downstream was built. "
        f"{_unique_rows(run_id)}{_after(run_id)}",
    )
    (result,) = _results(world, task.id)
    assert result.rollback_outcome is None
    assert "  rollback: not known\n" in _log(world, run_id, "silver.orders")
    assert _task(world, "gold.report").status is TaskStatus.UPSTREAM_FAILED


def test_a_rollback_the_database_refused_says_the_table_still_holds_the_data(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refused rollback changed nothing, so the message may say that the table holds the data."""
    _checked(world, _check("unique_id", _DUPLICATE_IDS))
    run_id = _start(world)
    monkeypatch.setattr(
        CatalogService,
        "roll_back",
        lambda self, *a, **k: Rollback(RollbackOutcome.FAILED, ROLLBACK_REFUSED_REASON),
    )
    _drain(world)

    task = _task(world, "silver.orders")
    assert task.error_message == (
        "Check `silver.orders/unique_id` failed: 1 row (checks/silver/orders/unique_id.sql). "
        "The rollback failed (the database refused or interrupted the rollback), so the table "
        f"still holds this run's data. Nothing downstream was built. {_unique_rows(run_id)}"
        f"{_after(run_id)}"
    )
    assert "  rollback: failed\n" in _log(world, run_id, "silver.orders")


def test_a_permanent_failure_reported_for_a_build_whose_write_landed_goes_through_the_verdict(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The write is in the table, so only the checks may end the task."""
    _checked(world, _check("unique_id", _DUPLICATE_IDS))
    run_id = _start(world)
    claimed = _run_until(world, "silver.orders")
    real = tasks_module._dispatch

    def built_and_then_failed(*args: object, **kwargs: object) -> TaskOutcome | None:
        outcome = real(*args, **kwargs)  # ty: ignore[invalid-argument-type]
        assert outcome is not None and outcome.kind is None
        return TaskOutcome(FailureKind.PERMANENT, "recording the write failed", outcome.environment)

    monkeypatch.setattr(tasks_module, "_dispatch", built_and_then_failed)

    run_claimed(_engine(world), claimed)

    task = _task(world, "silver.orders")
    assert task.status is TaskStatus.FAILED
    assert task.error_message is not None and task.error_message.startswith(
        "Check `silver.orders/unique_id` failed: 1 row"
    )
    assert [r.rollback_outcome for r in _results(world, task.id)] == [RollbackOutcome.UNPUBLISHED]
    assert _marker(world, "silver.orders") == run_id


def test_a_build_that_landed_and_was_reported_failed_succeeds_when_its_checks_pass(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    _checked(world, _check("no_nulls", "SELECT id FROM silver.orders WHERE id IS NULL"))
    _start(world)
    claimed = _run_until(world, "silver.orders")
    real = tasks_module._dispatch

    def built_and_then_failed(*args: object, **kwargs: object) -> TaskOutcome | None:
        outcome = real(*args, **kwargs)  # ty: ignore[invalid-argument-type]
        assert outcome is not None
        return TaskOutcome(FailureKind.PERMANENT, "recording the write failed", outcome.environment)

    monkeypatch.setattr(tasks_module, "_dispatch", built_and_then_failed)

    run_claimed(_engine(world), claimed)

    task = _task(world, "silver.orders")
    assert (task.status, task.error_message) == (
        TaskStatus.SUCCEEDED,
        COMMITTED_BEFORE_RECORDED_MESSAGE,
    )
    assert [r.outcome for r in _results(world, task.id)] == [CheckOutcome.PASSED]


def test_a_build_that_failed_for_good_without_a_write_stays_failed_and_unchecked(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control: no stamp, so there is nothing to check and nothing to set back."""
    _checked(world, _check("unique_id", _DUPLICATE_IDS), sql="SELECT nope FROM bronze.src")
    _start(world)
    seen = _counting(monkeypatch)
    _drain(world)

    task = _task(world, "silver.orders")
    assert (task.status, task.attempt) == (TaskStatus.FAILED, 1)
    assert task.error_message is not None and "nope" in task.error_message
    assert seen == [] and _results(world, task.id) == []
    assert not _exists(world, "silver.orders")


@pytest.mark.slow
def test_a_deactivated_principal_after_the_write_makes_every_check_errored(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A written table never fails for good without a verdict."""
    _built_once_then_redeployed(
        world,
        _check("a_unique", _DUPLICATE_IDS),
        _check("b_warns", "SELECT id FROM silver.orders", on_failure="warn"),
    )
    run_id = _start(world)
    claimed = _run_until(world, "silver.orders")
    real = tasks_module._dispatch

    def build_then_deactivate(*args: object, **kwargs: object) -> TaskOutcome | None:
        outcome = real(*args, **kwargs)  # ty: ignore[invalid-argument-type]
        with world.session() as session:
            session.execute(
                update(Principal).where(Principal.id == world.admin.id).values(is_active=False)
            )
            session.commit()
        return outcome

    monkeypatch.setattr(tasks_module, "_dispatch", build_then_deactivate)
    seen = _counting(monkeypatch)

    run_claimed(_engine(world), claimed)

    error = (
        "the run's principal is deactivated, so the check was not run. Activate the principal, "
        "or bind an active one to the deployment."
    )
    task = _task(world, "silver.orders")
    assert seen == []
    assert [(r.outcome, r.error) for r in _results(world, task.id)] == [
        (CheckOutcome.ERRORED, error),
        (CheckOutcome.ERRORED, error),
    ]
    assert task.status is TaskStatus.FAILED
    assert task.failure_code == "check_error"
    assert task.error_message == (
        f"Check `silver.orders/a_unique` could not run (checks/silver/orders/a_unique.sql): "
        f"{error} Data that could not be checked is not published. The table was set back to "
        f"what it was before this run, and nothing downstream was built. {_after(run_id)}"
    )
    assert _ids(world, "silver.orders") == [1, 1, 2]


def test_the_error_of_a_check_holds_no_value_of_the_table(world: World) -> None:
    """Result, message, log and audit carry the error's type, never its sentence."""
    world.set_manifest(
        _entry("silver.people", "SELECT 'Alice Smith' AS name"),
        checks=[
            _check(
                "numeric",
                "SELECT name FROM silver.people WHERE CAST(name AS INTEGER) > 0",
                table="silver.people",
            )
        ],
    )
    run_id = _start(world)
    _drain(world)

    task = _task(world, "silver.people")
    (result,) = _results(world, task.id)
    (count,) = _counts(world, task.id)
    # The read of a table is mapped by field id, so the query child already words a failed
    # conversion without the value; the count's run and the result both say so.
    assert count.error_message is not None
    assert count.error_message.startswith("Query failed: a value could not be converted")
    assert "Alice Smith" not in count.error_message
    assert result.error == (f"the check's query failed: the query is not valid. {_SEE_YOURSELF}")
    assert task.status is TaskStatus.FAILED and task.error_message is not None
    (attempt,) = _attempts(world, task.id)
    shown_to_run_readers = [
        result.error,
        task.error_message,
        attempt.error_message or "",
        _log(world, run_id, "silver.people"),
        repr(_events(world, AuditEventType.CHECK_FAILED)),
        repr(_events(world, AuditEventType.TABLE_ROLLED_BACK)),
    ]
    for text in shown_to_run_readers:
        assert "Alice" not in text and "Smith" not in text


@pytest.mark.slow
def test_no_rows_into_a_table_that_is_not_published_ends_failed_and_still_not_published(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The counts are refused, the verdict is clean, and the message is true."""
    _checked(world, _check("unique_id", _DUPLICATE_IDS))
    first = _start(world)
    _drain(world)
    assert _marker(world, "silver.orders") == first
    _checked(
        world,
        _check("unique_id", _DUPLICATE_IDS),
        sql="SELECT id FROM bronze.src WHERE id > 100",
        write_mode="append",
        retries=0,
    )
    run_id = _start(world)
    _drain(world)

    task = _task(world, "silver.orders")
    error = (
        "a table this check reads is not published: its first build did not finish, or failed a "
        "check, and no run has published it since."
    )
    (write,) = _writes(world, task.id)
    assert (write.write_mode, write.rows_written, write.snapshot_id) == (WriteMode.APPEND, 0, None)
    (result,) = _results(world, task.id)
    assert (result.outcome, result.error, result.rollback_outcome) == (
        CheckOutcome.ERRORED,
        error,
        RollbackOutcome.UNPUBLISHED,
    )
    assert (task.status, task.attempt) == (TaskStatus.FAILED, 1)
    assert task.error_message == (
        f"Check `silver.orders/unique_id` could not run (checks/silver/orders/unique_id.sql): "
        f"{error} Data that could not be checked is not published. {_UNPUBLISHED} "
        f"{_after(run_id)}"
    )
    assert _marker(world, "silver.orders") == run_id
    assert task.written_table_id is None
    assert _task(world, "gold.report").status is TaskStatus.UPSTREAM_FAILED
    with world.session() as session:
        run = session.get(Run, run_id)
        assert run is not None and run.status is RunStatus.FAILED  # nothing loops


@pytest.mark.slow
def test_no_new_rows_in_a_published_table_leave_it_alone_when_a_check_fails(
    world: World,
) -> None:
    """Spec section 6, third row: the run changed nothing, so nothing is set back."""
    _built_once_then_redeployed(
        world,
        _check("unique_id", _DUPLICATE_IDS),
        sql="SELECT id FROM bronze.src WHERE id > 100",
        write_mode="append",
    )
    run_id = _start(world)
    _drain(world)

    task = _task(world, "silver.orders")
    assert task.error_message == (
        "Check `silver.orders/unique_id` failed: 1 row (checks/silver/orders/unique_id.sql). "
        f"The table was not changed by this run, and nothing downstream was built. "
        f"{_unique_rows(run_id)}{_after(run_id)}"
    )
    (result,) = _results(world, task.id)
    assert result.rollback_outcome is RollbackOutcome.NOTHING_WRITTEN
    assert _ids(world, "silver.orders") == [1, 1, 2]


def test_a_task_created_with_checks_whose_job_version_lists_none_does_not_succeed(
    world: World,
) -> None:
    """Fail closed. One errored blocking result says so, and the verdict follows."""
    _checked(world, _check("unique_id", _DUPLICATE_IDS))
    run_id = _start(world)
    claimed = _run_until(world, "silver.orders")
    _checked(world, _check("other", "SELECT id FROM gold.report", table="gold.report"))

    run_claimed(_engine(world), claimed)

    task = _task(world, "silver.orders")
    error = (
        "the task was created with checks, but its project version lists none that can be read. "
        "Deploy a new commit of the project: that writes the project version afresh."
    )
    (result,) = _results(world, task.id)
    assert (result.check_name, result.check_path, result.on_failure) == (
        "silver.orders/(none)",
        "checks/silver/orders/",
        "block",
    )
    assert (result.outcome, result.error, result.rollback_outcome) == (
        CheckOutcome.ERRORED,
        error,
        RollbackOutcome.UNPUBLISHED,
    )
    assert (task.status, task.attempt) == (TaskStatus.FAILED, 1)
    assert task.error_message == (
        f"Check `silver.orders/(none)` could not run (checks/silver/orders/): {error} Data that "
        f"could not be checked is not published. {_UNPUBLISHED} {_after(run_id)}"
    )
    assert _marker(world, "silver.orders") == run_id


_UNREADABLE = (
    "the project version lists a check that cannot be read, so this table cannot be checked "
    "completely. Deploy a new commit of the project: that writes the project version afresh."
)


def _with_raw_check(world: World, raw: object) -> None:
    """Append an entry to the deployed manifest's `checks` as it is, damaged or not.

    Parameters
    ----------
    world
        The world.
    raw
        The entry.
    """
    with world.session() as session:
        version = session.get(JobVersion, world.version_id)
        assert version is not None
        version.manifest_json = {
            **version.manifest_json,
            "checks": [*version.manifest_json["checks"], raw],
        }
        session.commit()


def _without_sql(entry: dict[str, object]) -> dict[str, object]:
    """Damage an entry: take its SQL away.

    Parameters
    ----------
    entry
        A check entry.

    Returns
    -------
    dict
        The same entry without ``sql``.
    """
    return {key: value for key, value in entry.items() if key != "sql"}


@pytest.mark.parametrize(
    "damage",
    [
        _without_sql(_check("broken", "")),
        {**_check("broken", "SELECT 1"), "name": 7},
    ],
    ids=["missing sql", "name not a string"],
)
def test_an_entry_that_cannot_be_read_fails_the_task_beside_the_checks_that_run(
    world: World, damage: dict[str, object]
) -> None:
    """A damaged entry of the table is not skipped. The others still run and are recorded."""
    _checked(world, _check("not_null", _PASSING))
    _with_raw_check(world, damage)
    run_id = _start(world)

    _drain(world)

    task = _task(world, "silver.orders")
    assert (task.status, task.attempt) == (TaskStatus.FAILED, 1)
    first, second = sorted(_results(world, task.id), key=lambda r: r.check_name)
    assert (first.check_name, first.outcome) == ("silver.orders/(unreadable)", CheckOutcome.ERRORED)
    assert (first.error, first.on_failure) == (_UNREADABLE, "block")
    assert (second.check_name, second.outcome) == ("silver.orders/not_null", CheckOutcome.PASSED)
    assert task.error_message == (
        f"Check `silver.orders/(unreadable)` could not run (checks/silver/orders/): {_UNREADABLE} "
        f"Data that could not be checked is not published. {_UNPUBLISHED} {_after(run_id)}"
    )
    assert _marker(world, "silver.orders") == run_id
    assert _task(world, "gold.report").status is not TaskStatus.SUCCEEDED


def test_two_checks_of_one_name_fail_closed_and_the_first_failure_is_not_replaced(
    world: World,
) -> None:
    """Results are recorded by name; the second would have replaced the first's failure."""
    _checked(
        world,
        _check("dup", _DUPLICATE_IDS),
        _check("dup", "SELECT id FROM silver.orders WHERE false"),
    )
    run_id = _start(world)

    _drain(world)

    task = _task(world, "silver.orders")
    assert task.status is TaskStatus.FAILED
    (result,) = _results(world, task.id)
    assert (result.check_name, result.outcome) == (
        "silver.orders/(unreadable)",
        CheckOutcome.ERRORED,
    )
    assert _marker(world, "silver.orders") == run_id


def test_a_check_whose_name_is_not_its_table_and_check_fails_closed(world: World) -> None:
    """The name is what a result is recorded under, so a name that lies is damage."""
    _checked(world, {**_check("fine", _PASSING), "name": "silver.orders/other"})
    run_id = _start(world)

    _drain(world)

    task = _task(world, "silver.orders")
    assert task.status is TaskStatus.FAILED
    (result,) = _results(world, task.id)
    assert (result.check_name, result.error) == ("silver.orders/(unreadable)", _UNREADABLE)
    assert _marker(world, "silver.orders") == run_id


def test_an_entry_that_names_no_table_fails_the_first_task_and_so_the_run(world: World) -> None:
    """It may be any table's, so the task fails closed; a new commit repairs it."""
    _checked(world, _check("not_null", _PASSING))
    _with_raw_check(world, 7)
    _start(world)

    _drain(world)

    task = _task(world, "bronze.src")
    assert task.status is TaskStatus.FAILED
    (result,) = _results(world, task.id)
    assert (result.check_name, result.outcome, result.error) == (
        "bronze.src/(unreadable)",
        CheckOutcome.ERRORED,
        _UNREADABLE,
    )
    assert _task(world, "silver.orders").status is TaskStatus.UPSTREAM_FAILED
    assert not _exists(world, "silver.orders")


def test_a_manifest_without_damage_builds_and_checks_as_before(world: World) -> None:
    """No unreadable entry, no extra result."""
    _checked(world, _check("not_null", _PASSING))
    _start(world)

    _drain(world)

    task = _task(world, "silver.orders")
    assert task.status is TaskStatus.SUCCEEDED
    assert [r.check_name for r in _results(world, task.id)] == ["silver.orders/not_null"]


def test_a_source_check_task_whose_only_entry_cannot_be_read_fails_and_its_reader_does_not_build(
    world: World,
) -> None:
    """The source table gets its task, which fails closed, instead of an unchecked read."""
    _source_checked(world, "SELECT id FROM bronze.events WHERE id IS NULL")
    _with_raw_check(world, {**_check("two", "SELECT 1", table="bronze.events"), "sql": None})
    _start(world)

    _drain(world)

    source = _task(world, "bronze.events")
    assert source.status is TaskStatus.FAILED
    assert "bronze.events/(unreadable)" in {r.check_name for r in _results(world, source.id)}
    assert _task(world, "silver.orders").status is not TaskStatus.SUCCEEDED
    assert not _exists(world, "silver.orders")


def test_a_source_check_task_whose_job_version_lists_no_check_fails(world: World) -> None:
    _source_checked(world, "SELECT id FROM bronze.events WHERE id IS NULL")
    _start(world)
    claimed = _claim_ready(world)
    assert claimed is not None and claimed.kind == "source_check"
    world.set_manifest(
        _entry("silver.orders", "SELECT id FROM bronze.events", references=["bronze.events"]),
        checks=[],
    )

    run_claimed(_engine(world), claimed)

    task = _task(world, "bronze.events")
    assert task.status is TaskStatus.FAILED
    assert [r.outcome for r in _results(world, task.id)] == [CheckOutcome.ERRORED]
    assert _ids(world, "bronze.events") == [1, 1, 2]


def test_the_counts_run_under_the_installations_query_limits_as_the_run_principal(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec section 5: the query limits, not the task's own timeout, and the run's principal."""
    _checked(world, _check("unique_id", _DUPLICATE_IDS), timeout_seconds=5)
    _start(world)
    claimed = _run_until(world, "silver.orders")
    calls: list[dict[str, Any]] = []
    real = checks_module.count_check

    def spying(session: Session, **kwargs: object) -> checks_module.Counted:
        calls.append(kwargs)
        return real(session, **kwargs)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(checks_module, "count_check", spying)
    engine = _engine(world)

    run_claimed(engine, claimed)

    (call,) = calls
    assert call["limits"] is engine.settings.limits
    assert call["limits"].timeout_seconds == 900
    assert (call["principal_id"], call["task_run_id"], call["worker_id"]) == (
        world.admin.id,
        claimed.task_run_id,
        "w1",
    )
    assert call["spec"].name == "silver.orders/unique_id"


@pytest.mark.slow
def test_the_checks_run_as_the_run_principal_not_as_the_trigger_or_the_deployer(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The count, the rollback and the audit of a failed check are the run-as principal's."""
    _built_once_then_redeployed(world, _check("unique_id", _DUPLICATE_IDS))
    run_id = _start(world)
    ids = _three_principals(world, run_id)
    assert len({*ids.values(), world.admin.id}) == 4
    claimed = _run_until(world, "silver.orders")
    actors: list[PrincipalRef] = []
    real = CatalogService.roll_back

    def spying(self: CatalogService, p: PrincipalRef, *args: object, **kwargs: object) -> Rollback:
        actors.append(p)
        return real(self, p, *args, **kwargs)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(CatalogService, "roll_back", spying)

    run_claimed(_engine(world), claimed)

    task = _task(world, "silver.orders")
    assert task.status is TaskStatus.FAILED
    (count,) = _counts(world, task.id)
    assert count.principal_id == ids["runner"]
    (actor,) = actors
    assert (actor.id, actor.name) == (ids["runner"], "runner")
    with world.session() as session:
        by_type = {
            event.event_type: event.principal_id
            for event in session.scalars(
                select(AuditEvent).where(
                    AuditEvent.event_type.in_(
                        [AuditEventType.CHECK_FAILED, AuditEventType.TABLE_ROLLED_BACK]
                    )
                )
            )
        }
    assert by_type == {
        AuditEventType.CHECK_FAILED: ids["runner"],
        AuditEventType.TABLE_ROLLED_BACK: ids["runner"],
    }
    assert _ids(world, "silver.orders") == [1, 1, 2]


@pytest.mark.parametrize("on_failure", ["block", "keep"])
def test_a_foreign_check_task_names_its_real_failure_before_the_refusal(
    world: World, on_failure: str
) -> None:
    """One check of the table really fails and one is refused; the message has both."""
    _outside_table(world, "bronze.events", [1, 2])
    kept = _keep_note_on(world, "bronze.events")
    world.set_manifest(
        _entry("silver.orders", "SELECT id FROM bronze.events", references=["bronze.events"]),
        checks=[
            _check("always", "SELECT 1 AS id", table="bronze.events", on_failure=on_failure),
            _check(
                "unique_id",
                "SELECT id FROM bronze.events GROUP BY id HAVING count(*) > 1",
                table="bronze.events",
                on_failure=on_failure,
            ),
        ],
    )
    _start(world)
    _drain(world)

    task = _task(world, "bronze.events")
    assert {result.check_name: result.outcome for result in _results(world, task.id)} == {
        "bronze.events/always": CheckOutcome.FAILED,
        "bronze.events/unique_id": CheckOutcome.ERRORED,
    }
    assert task.status is TaskStatus.FAILED
    assert task.error_message == (
        "Check `bronze.events/always` failed: 1 row (checks/bronze/events/always.sql). "
        f"{_refused_text('bronze.events', kept)}"
    )
    assert _task(world, "silver.orders").status is TaskStatus.UPSTREAM_FAILED


def _spec(generated: GeneratedFrom | None) -> CheckSpec:
    """Build a check of `gold.fact_sales`.

    Parameters
    ----------
    generated
        Where it comes from; `None` for a hand-written check.

    Returns
    -------
    CheckSpec
        The check.
    """
    return CheckSpec(
        name="gold.fact_sales/ref_store_id",
        table="gold.fact_sales",
        check="ref_store_id",
        path="assets/gold/fact_sales.sql:6",
        on_failure="block",
        description=None,
        sql="SELECT 1",
        generated=generated,
    )


def test_only_a_reference_check_keeps_its_error_to_its_own_table() -> None:
    """Its query reads the target, so the error names no table but the checked one."""
    reference = GeneratedFrom("references", ("store_id",), "gold.dim_store", "store_id")
    key = GeneratedFrom("key", ("id",))
    assert task_checks_module._reads_another_table(_spec(reference)) == "gold.fact_sales"
    assert task_checks_module._reads_another_table(_spec(key)) is None
    assert task_checks_module._reads_another_table(_spec(None)) is None


def test_a_reference_checks_policy_failure_on_its_target_names_no_table(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_count_one` keeps a reference check's error to the checked table, end to end."""
    _checked(
        world,
        _generated(
            "ref_station",
            "SELECT id FROM silver.orders WHERE id IS NULL",
            {
                "family": "references",
                "columns": ["id"],
                "target": "bronze.stations",
                "target_column": "id",
            },
        ),
        retries=0,
    )
    _start(world)
    claimed = _run_until(world, "silver.orders")
    real = checks_module.count_check

    def failing_on_the_target(session: Session, **kwargs: object) -> checks_module.Counted:
        counted = real(session, **kwargs)  # ty: ignore[invalid-argument-type]
        return dataclasses.replace(
            counted,
            rows=None,
            failure=ValidationError("conversion"),
            error_message=policy_read_failure("bronze.stations"),
            error_cause="invalid_query",
        )

    monkeypatch.setattr(task_checks_module, "model_problem", lambda *a, **k: None)
    monkeypatch.setattr(checks_module, "count_check", failing_on_the_target)

    run_claimed(_engine(world), claimed)

    (result,) = _results(world, _task(world, "silver.orders").id)
    assert result.outcome is CheckOutcome.ERRORED
    assert result.error == policy_read_failure(None)
