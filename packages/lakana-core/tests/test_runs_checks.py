"""Check results: recording one for the claim that ran it, and the texts that describe it."""

from __future__ import annotations

import dataclasses
import inspect
import re
import shlex
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from core_test_helpers import _prepared_catalog, python_task_run
from sqlalchemy import select
from sqlalchemy.orm import Session

import lakana_core.runs.checks as checks_module
from lakana_core.auth import AllowAllAuthorization, PrincipalRef, SecurableKind
from lakana_core.catalog.service import (
    CHANGED_BY_ANOTHER_REASON,
    ROLLBACK_NOT_KNOWN_REASON,
    ROLLBACK_REFUSED_REASON,
    Rollback,
)
from lakana_core.db import db_now
from lakana_core.models import (
    AuditEvent,
    AuditEventType,
    CheckOutcome,
    CheckResult,
    RollbackOutcome,
    Run,
    TaskRun,
    TaskStatus,
)
from lakana_core.projects.checks import CheckSpec, GeneratedFrom
from lakana_core.runs.checks import (
    ASSET,
    ERROR_KEPT,
    ERROR_NO_CHECKS,
    ERROR_NO_PRINCIPAL,
    ERROR_NOT_PUBLISHED,
    ERROR_UNREADABLE_CHECK,
    RETRY_MESSAGE,
    ROLLBACK_PHRASES,
    SET_BACK_CHANGED,
    SET_BACK_DONE,
    SET_BACK_NOTHING,
    SOURCE_CHECK,
    Verdict,
    build_failure_message,
    error_of_exception,
    error_text,
    failure_message,
    kept_check_message,
    kept_failure_message,
    kept_no_checks_error,
    mark_rollback_failed,
    not_published_message,
    policy_note,
    record_result,
    refused_source_message,
    results_of_task,
    rows_sentences,
    set_back_failed_message,
    set_back_result_message,
    unchecked_retry_message,
    verdict,
    view_of,
    warning_note,
)

_TABLE = "silver.station_temperatures"
_STARTED = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)


def _spec(
    check: str = "unique_reading", on_failure: str = "block", table: str = _TABLE
) -> CheckSpec:
    """Build the spec a deploy would have stored for one check.

    Parameters
    ----------
    check
        The check's short name.
    on_failure
        ``block`` or ``warn``.
    table
        The checked table.

    Returns
    -------
    CheckSpec
        The spec.
    """
    return CheckSpec(
        name=f"{table}/{check}",
        table=table,
        check=check,
        path=f"checks/{table.replace('.', '/')}/{check}.sql",
        on_failure=on_failure,
        description=None,
        sql="SELECT 1",
    )


@pytest.fixture
def task(session: Session, tmp_path: Path, principal: PrincipalRef) -> TaskRun:
    """Provide a running task run of ``silver.station_temperatures``, claimed by ``w1``.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        Storage root of the ``dev`` environment.
    principal
        The run principal.

    Returns
    -------
    TaskRun
        The running task run (claim 1, lease owner ``w1``).
    """
    _prepared_catalog(session, tmp_path, AllowAllAuthorization())
    return python_task_run(session, principal.id, asset=_TABLE, kind="sql")


def _record(
    session: Session,
    task: TaskRun,
    spec: CheckSpec,
    outcome: CheckOutcome = CheckOutcome.PASSED,
    *,
    failing_rows: int | None = 0,
    error: str | None = None,
    worker: str = "w1",
    claim: int = 1,
    table_id: uuid.UUID | None = None,
    snapshot: int | None = 7,
) -> bool:
    """Record a result for `task` with the values most tests share.

    Parameters
    ----------
    session
        Database session.
    task
        The task run.
    spec
        The check.
    outcome
        What happened.
    failing_rows
        The count, if it ran.
    error
        The error text of an errored check.
    worker
        The worker claiming to hold the task.
    claim
        The claim it claims to hold.
    table_id
        The checked table's id, if resolved.
    snapshot
        The snapshot the check read, if it read one.

    Returns
    -------
    bool
        What `record_result` returned.
    """
    return record_result(
        session,
        task_run_id=task.id,
        worker=worker,
        claim=claim,
        spec=spec,
        outcome=outcome,
        failing_rows=failing_rows,
        error=error,
        table_id=table_id,
        checked_snapshot_id=snapshot,
        policy_applied=False,
        query_run_id=None,
        started_at=_STARTED,
    )


def _events(session: Session) -> list[AuditEvent]:
    """Return the check-failed audit events.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    list of AuditEvent
        The events.
    """
    return list(
        session.scalars(
            select(AuditEvent).where(AuditEvent.event_type == AuditEventType.CHECK_FAILED)
        )
    )


def test_the_kinds_of_a_task() -> None:
    assert (ASSET, SOURCE_CHECK) == ("asset", "source_check")


def test_a_result_is_recorded_once_per_check_and_task(
    session: Session, task: TaskRun, principal: PrincipalRef
) -> None:
    table_id = uuid.uuid4()
    query_run_id = None
    spec = _spec()
    before = db_now(session)
    assert record_result(
        session,
        task_run_id=task.id,
        worker="w1",
        claim=1,
        spec=spec,
        outcome=CheckOutcome.FAILED,
        failing_rows=3,
        error=None,
        table_id=table_id,
        checked_snapshot_id=42,
        policy_applied=True,
        query_run_id=query_run_id,
        started_at=_STARTED,
    )
    after = db_now(session)
    (row,) = results_of_task(session, task.id)
    assert (row.run_id, row.task_run_id) == (task.run_id, task.id)
    run = session.get(Run, task.run_id)
    assert run is not None
    assert row.environment_id == run.environment_id
    assert (row.check_name, row.check_path, row.table_name) == (
        spec.name,
        spec.path,
        _TABLE,
    )
    assert (row.table_id, row.on_failure, row.outcome) == (table_id, "block", CheckOutcome.FAILED)
    assert (row.failing_rows, row.error, row.checked_snapshot_id) == (3, None, 42)
    assert (row.rollback_outcome, row.policy_applied, row.query_run_id) == (None, True, None)
    assert row.started_at == _STARTED
    assert before <= row.finished_at <= after

    assert _record(session, task, spec, CheckOutcome.PASSED, failing_rows=0)
    (again,) = results_of_task(session, task.id)
    assert (again.outcome, again.failing_rows, again.policy_applied) == (
        CheckOutcome.PASSED,
        0,
        False,
    )
    assert again.id != row.id
    assert session.scalars(select(CheckResult)).all() == [again]


def test_a_result_says_whether_its_check_was_generated(session: Session, task: TaskRun) -> None:
    """The mark follows the spec, also when a second result replaces the first."""
    generated = dataclasses.replace(_spec("key_unique"), generated=GeneratedFrom("key", ("id",)))
    assert _record(session, task, generated)
    assert _record(session, task, _spec())
    marks = {row.check_name: row.generated for row in results_of_task(session, task.id)}
    assert marks == {generated.name: True, _spec().name: False}

    assert _record(session, task, dataclasses.replace(generated, generated=None))
    rows = {row.check_name: row for row in results_of_task(session, task.id)}
    assert rows[generated.name].generated is False
    assert _record(session, task, generated)
    (row,) = [r for r in results_of_task(session, task.id) if r.check_name == generated.name]
    assert view_of(row, _TABLE, None).generated is True
    (other,) = [r for r in results_of_task(session, task.id) if r.check_name != generated.name]
    assert view_of(other, _TABLE, None).generated is False


def test_a_failed_blocking_check_is_audited(
    session: Session, task: TaskRun, principal: PrincipalRef
) -> None:
    spec = _spec()
    assert _record(session, task, spec, CheckOutcome.FAILED, failing_rows=3)
    (event,) = _events(session)
    assert event.principal_id == principal.id
    assert (
        event.securable_kind,
        event.securable_environment,
        event.securable_namespace,
        event.securable_table,
    ) == (SecurableKind.TABLE, "dev", "silver", "station_temperatures")
    assert event.detail == {
        "run_id": str(task.run_id),
        "task_run_id": str(task.id),
        "table": _TABLE,
        "check": "silver.station_temperatures/unique_reading",
        "path": "checks/silver/station_temperatures/unique_reading.sql",
        "outcome": "failed",
        "rows": 3,
    }


@pytest.mark.parametrize(
    ("outcome", "rows", "error"),
    [(CheckOutcome.FAILED, 2, None), (CheckOutcome.ERRORED, None, "boom")],
)
def test_a_failed_keep_check_is_audited_like_a_blocking_one(
    session: Session, task: TaskRun, outcome: CheckOutcome, rows: int | None, error: str | None
) -> None:
    assert _record(session, task, _spec(on_failure="keep"), outcome, failing_rows=rows, error=error)
    (event,) = _events(session)
    assert event.detail["outcome"] == outcome.value
    assert event.detail["rows"] == rows


def test_a_passed_keep_check_is_not_audited(session: Session, task: TaskRun) -> None:
    assert _record(session, task, _spec(on_failure="keep"), CheckOutcome.PASSED)
    assert _events(session) == []


def test_an_errored_blocking_check_is_audited_without_rows(session: Session, task: TaskRun) -> None:
    assert _record(session, task, _spec(), CheckOutcome.ERRORED, failing_rows=None, error="boom")
    (event,) = _events(session)
    assert event.detail["outcome"] == "errored"
    assert event.detail["rows"] is None


@pytest.mark.parametrize(
    ("outcome", "on_failure", "rows", "error"),
    [
        (CheckOutcome.PASSED, "block", 0, None),
        (CheckOutcome.WARNED, "warn", 12, None),
        (CheckOutcome.ERRORED, "warn", None, "boom"),
    ],
)
def test_a_passed_a_warned_and_an_errored_warning_check_are_not_audited(
    session: Session,
    task: TaskRun,
    outcome: CheckOutcome,
    on_failure: str,
    rows: int | None,
    error: str | None,
) -> None:
    assert _record(
        session, task, _spec(on_failure=on_failure), outcome, failing_rows=rows, error=error
    )
    assert len(results_of_task(session, task.id)) == 1
    assert _events(session) == []


@pytest.mark.parametrize("case", ["wrong-worker", "wrong-claim", "pending"])
def test_a_worker_that_lost_its_claim_records_nothing(
    session: Session, task: TaskRun, case: str
) -> None:
    worker, claim = "w1", 1
    if case == "wrong-worker":
        worker = "w2"
    elif case == "wrong-claim":
        claim = 2
    else:
        task.status = TaskStatus.PENDING
        session.commit()
    assert not _record(
        session, task, _spec(), CheckOutcome.FAILED, failing_rows=3, worker=worker, claim=claim
    )
    assert session.scalars(select(CheckResult)).all() == []
    assert _events(session) == []


def test_the_policy_note() -> None:
    assert policy_note("etl") == (
        "checked under a row filter or column mask of etl: "
        "rows and values it cannot see were not checked"
    )
    assert policy_note("e\x1b[31mtl").startswith(
        "checked under a row filter or column mask of e\\x1b[31mtl: "
    )
    assert policy_note(None) == (
        "checked under a row filter or column mask of the run's principal: "
        "rows and values it cannot see were not checked"
    )


def test_the_rollback_phrases() -> None:
    assert ROLLBACK_PHRASES == {
        "rolled_back": "rolled back",
        "unpublished": "not published",
        "nothing_written": "nothing was written",
        "not_a_table": "a view, nothing to set back",
        "changed_since": "left as it is, changed since",
        "failed": "rollback failed",
    }


def _look(run_id: uuid.UUID, *checks: str) -> str:
    """Build the sentences that name the command which shows the rows of failed checks.

    Parameters
    ----------
    run_id
        The run.
    *checks
        The short names of the checks, in the order the sentences come.

    Returns
    -------
    str
        One sentence per check, each followed by a space; empty for none.
    """
    return "".join(
        f"Look at the rows: `lakana check rows {run_id} {_TABLE}/{name} --env dev`. "
        for name in checks
    )


def _text(failed: str, run_id: uuid.UUID, rows: str = "") -> str:
    """Build the text a table that is not published reads as.

    Parameters
    ----------
    failed
        What its first build failed, as the middle of the sentence reads it.
    run_id
        The run.
    rows
        The sentences that name the command (`_look`), before the closing sentence.

    Returns
    -------
    str
        The text.
    """
    if failed == "a check":
        # No check to name: the results are gone, or the build itself failed and there never
        # were any. The text is true for both.
        return (
            f"`{_TABLE}` has not been published: its first build did not finish, or failed a "
            f"check (run {run_id}). {rows}It is published by the next run that builds it and "
            "passes its checks."
        )
    return (
        f"`{_TABLE}` has not been published: its first build failed {failed} (run {run_id}). "
        f"{rows}It is published by the next run in which its checks pass."
    )


@pytest.mark.parametrize(
    ("results", "failed", "looked"),
    [
        ([("unique_id", "block", CheckOutcome.FAILED)], "the check `unique_id`", ["unique_id"]),
        (
            [("b_range", "block", CheckOutcome.FAILED), ("a_null", "block", CheckOutcome.FAILED)],
            "the checks `a_null` and `b_range`",
            ["a_null", "b_range"],
        ),
        (
            [
                ("c", "block", CheckOutcome.FAILED),
                ("a", "block", CheckOutcome.FAILED),
                ("b", "block", CheckOutcome.FAILED),
            ],
            "the checks `a`, `b` and `c`",
            ["a", "b", "c"],
        ),
        (
            [(name, "block", CheckOutcome.FAILED) for name in ("e", "b", "d", "a", "c")],
            "the checks `a`, `b`, `c` and 2 more",
            ["a", "b", "c"],
        ),
        (
            [("broken", "block", CheckOutcome.ERRORED), ("alpha", "block", CheckOutcome.FAILED)],
            "the checks `alpha` and `broken`",
            ["alpha"],
        ),
        (
            [
                ("fine", "block", CheckOutcome.PASSED),
                ("soft", "warn", CheckOutcome.WARNED),
                ("hard", "block", CheckOutcome.FAILED),
            ],
            "the check `hard`",
            ["hard"],
        ),
        (
            [("fine", "block", CheckOutcome.PASSED), ("soft", "warn", CheckOutcome.WARNED)],
            "a check",
            [],
        ),
        ([], "a check", []),
    ],
    ids=[
        "one",
        "two",
        "three",
        "five",
        "an-errored-check-has-no-rows-to-look-at",
        "a-warned-and-a-passed-check-are-not",
        "only-warned-and-passed",
        "no-result-left",
    ],
)
def test_the_not_published_message(
    session: Session,
    task: TaskRun,
    results: list[tuple[str, str, CheckOutcome]],
    failed: str,
    looked: list[str],
) -> None:
    table_id = uuid.uuid4()
    for check, on_failure, outcome in results:
        _record(
            session,
            task,
            _spec(check, on_failure=on_failure),
            outcome,
            failing_rows=None if outcome is CheckOutcome.ERRORED else 1,
            error="boom" if outcome is CheckOutcome.ERRORED else None,
            table_id=table_id,
            snapshot=None if outcome is CheckOutcome.ERRORED else 7,
        )

    message = not_published_message(session, _TABLE, table_id, task.run_id, "dev")

    assert message == _text(failed, task.run_id, _look(task.run_id, *looked))


def test_the_not_published_message_names_no_rows_for_a_check_without_a_snapshot(
    session: Session, task: TaskRun
) -> None:
    table_id = uuid.uuid4()
    _record(session, task, _spec("seen"), CheckOutcome.FAILED, failing_rows=1, table_id=table_id)
    _record(
        session,
        task,
        _spec("unseen"),
        CheckOutcome.FAILED,
        failing_rows=1,
        table_id=table_id,
        snapshot=None,
    )

    message = not_published_message(session, _TABLE, table_id, task.run_id, "dev")

    assert message == _text(
        "the checks `seen` and `unseen`", task.run_id, _look(task.run_id, "seen")
    )


def test_the_not_published_message_counts_three_among_the_checks_that_have_rows(
    session: Session, task: TaskRun
) -> None:
    """A check without a snapshot does not use up one of the three sentences."""
    table_id = uuid.uuid4()
    for check in ("a", "b", "c"):
        _record(
            session,
            task,
            _spec(check),
            CheckOutcome.FAILED,
            failing_rows=1,
            table_id=table_id,
            snapshot=None,
        )
    for check in ("d", "e", "f", "g"):
        _record(session, task, _spec(check), CheckOutcome.FAILED, failing_rows=1, table_id=table_id)

    message = not_published_message(session, _TABLE, table_id, task.run_id, "dev")

    assert message == _text(
        "the checks `a`, `b`, `c` and 4 more", task.run_id, _look(task.run_id, "d", "e", "f")
    )


def test_the_not_published_message_names_only_this_table_and_this_run(
    session: Session, task: TaskRun, principal: PrincipalRef
) -> None:
    table_id = uuid.uuid4()
    _record(session, task, _spec("mine"), CheckOutcome.FAILED, failing_rows=1, table_id=table_id)
    _record(
        session,
        task,
        _spec("theirs", table="silver.other"),
        CheckOutcome.FAILED,
        failing_rows=1,
        table_id=uuid.uuid4(),
    )
    later = python_task_run(session, principal.id, asset=_TABLE, kind="sql")
    _record(session, later, _spec("later"), CheckOutcome.FAILED, failing_rows=1, table_id=table_id)

    message = not_published_message(session, _TABLE, table_id, task.run_id, "dev")

    assert message == _text("the check `mine`", task.run_id, _look(task.run_id, "mine"))


def test_the_not_published_message_escapes_what_a_name_may_carry(
    session: Session, task: TaskRun
) -> None:
    table_id = uuid.uuid4()
    _record(
        session,
        task,
        _spec("odd\x1b[31mname"),
        CheckOutcome.FAILED,
        failing_rows=1,
        table_id=table_id,
    )

    message = not_published_message(session, "silver.a\x1b[31mb", table_id, task.run_id, "d\x1bv")

    assert "\x1b" not in message
    assert "odd\\x1b[31mname" in message
    # An escaped name holds a backslash, so the shell is given it in quotes.
    assert "--env 'd\\x1bv'`" in message


def _result(
    check: str,
    outcome: CheckOutcome,
    *,
    on_failure: str = "block",
    rows: int | None = None,
    error: str | None = None,
) -> CheckResult:
    """Build an unsaved result of `silver.station_temperatures/<check>`.

    Parameters
    ----------
    check
        The check's short name.
    outcome
        How it ended.
    on_failure
        ``block`` or ``warn``.
    rows
        The count, if it ran.
    error
        Why it could not run.

    Returns
    -------
    CheckResult
        The result, not added to a session.
    """
    return CheckResult(
        check_name=f"silver.station_temperatures/{check}",
        check_path=f"checks/silver/station_temperatures/{check}.sql",
        table_name="silver.station_temperatures",
        on_failure=on_failure,
        outcome=outcome,
        failing_rows=rows,
        error=error,
    )


_RUN = uuid.UUID("6f0d7b1e-2c3a-4d5e-8f90-123456789abc")
_AFTER = (
    "After a fix, deploy and run again; if the project did not change: "
    f"`lakana runs resume {_RUN} --env dev`."
)
_E1 = (
    "the run's principal `etl` may not read a table this check names, or the table does not "
    "exist. Grant it READ on the tables the check reads, or change the check."
)
_SEE_YOURSELF = "Run the check's SQL yourself to see the full message."


def _message(
    blocking: list[CheckResult], rollback: Rollback | None, *, refused: bool = False
) -> str:
    """Build the task's message for failed blocking checks of the fixture's table.

    Parameters
    ----------
    blocking
        The failed blocking results.
    rollback
        What the rollback did; `None` for a source check.
    refused
        Passed through.

    Returns
    -------
    str
        The message.
    """
    return failure_message(
        blocking,
        table="silver.station_temperatures",
        run_id=_RUN,
        environment="dev",
        rollback=rollback,
        refused=refused,
    )


def test_the_verdict_table() -> None:
    passed = _result("a", CheckOutcome.PASSED, rows=0)
    warned = _result("b", CheckOutcome.WARNED, on_failure="warn", rows=12)
    warn_errored = _result("c", CheckOutcome.ERRORED, on_failure="warn", error="x.")
    failed = _result("d", CheckOutcome.FAILED, rows=3)
    errored = _result("e", CheckOutcome.ERRORED, error="x.")
    keep_passed = _result("f", CheckOutcome.PASSED, on_failure="keep", rows=0)
    keep_failed = _result("g", CheckOutcome.FAILED, on_failure="keep", rows=2)
    keep_errored = _result("h", CheckOutcome.ERRORED, on_failure="keep", error="x.")

    assert verdict([passed]) == Verdict((), (), ())
    assert verdict([passed, warned, warn_errored]) == Verdict((), (), (warned, warn_errored))
    assert verdict([errored, passed, failed, warned]) == Verdict((failed, errored), (), (warned,))
    assert verdict([keep_passed]) == Verdict((), (), ())
    assert verdict([keep_errored, keep_passed, keep_failed]) == Verdict(
        (), (keep_failed, keep_errored), ()
    )
    assert verdict(
        [keep_failed, warned, failed, keep_errored, warn_errored, errored, passed]
    ) == Verdict((failed, errored), (keep_failed, keep_errored), (warned, warn_errored))


def test_the_message_of_a_kept_table() -> None:
    text = kept_failure_message(
        kept=[_result("unique_id", CheckOutcome.FAILED, on_failure="keep", rows=12)],
        run_id=_RUN,
        environment="dev",
    )
    assert text == (
        "Check `silver.station_temperatures/unique_id` failed: 12 rows "
        "(checks/silver/station_temperatures/unique_id.sql). "
        "The table keeps this run's data (`on_failure: keep`), and nothing that reads it was "
        "built. Repair the data or the asset; after a fix, deploy and run again; if the project "
        f"did not change: `lakana runs resume {_RUN} --env dev`."
    )


def test_the_message_of_a_kept_table_whose_check_could_not_run_or_that_has_several() -> None:
    errored = kept_failure_message(
        kept=[_result("k", CheckOutcome.ERRORED, on_failure="keep", error="Binder Error.")],
        run_id=_RUN,
        environment="dev",
    )
    assert errored.startswith(
        "Check `silver.station_temperatures/k` could not run "
        "(checks/silver/station_temperatures/k.sql): Binder Error. The table keeps"
    )
    several = kept_failure_message(
        kept=[
            _result("a", CheckOutcome.FAILED, on_failure="keep", rows=1),
            _result("b", CheckOutcome.FAILED, on_failure="keep", rows=2),
        ],
        run_id=_RUN,
        environment="dev",
    )
    assert several.startswith("2 checks of `silver.station_temperatures` failed:")


def test_the_message_of_a_keep_failure_whose_table_was_dropped_keeps_nothing() -> None:
    text = kept_failure_message(
        kept=[_result("unique_id", CheckOutcome.FAILED, on_failure="keep", rows=12)],
        run_id=_RUN,
        environment="dev",
        dropped=True,
    )
    assert text == (
        "Check `silver.station_temperatures/unique_id` failed: 12 rows "
        "(checks/silver/station_temperatures/unique_id.sql). "
        "The table was dropped while its checks ran, so nothing is kept."
    )


_KEPT_RUN = uuid.UUID("11111111-2222-3333-4444-555555555555")


def test_the_message_of_a_later_run_that_checks_a_kept_table_again() -> None:
    text = kept_check_message(
        [_result("unique_id", CheckOutcome.FAILED, rows=12)],
        table=_TABLE,
        run_id=_RUN,
        environment="dev",
        kept_run_id=_KEPT_RUN,
        without_checks=False,
    )
    assert text == (
        "Check `silver.station_temperatures/unique_id` failed: 12 rows "
        "(checks/silver/station_temperatures/unique_id.sql). "
        f"`silver.station_temperatures` was kept after a failed check in run {_KEPT_RUN} and "
        "still fails, so the assets that read it were not built. Build it again "
        "(`lakana run --select silver.station_temperatures --env dev`), or repair the data and "
        "run again."
    )


def test_the_messages_of_a_table_kept_unchecked_say_that_a_check_could_not_run() -> None:
    """A mark that names a check that could not run is never described as a failure."""
    text = kept_check_message(
        [_result("unique_id", CheckOutcome.FAILED, rows=12)],
        table=_TABLE,
        run_id=_RUN,
        environment="dev",
        kept_run_id=_KEPT_RUN,
        without_checks=False,
        unchecked=True,
    )
    assert text.endswith(
        f"`silver.station_temperatures` was kept because its checks could not run in run "
        f"{_KEPT_RUN}, and now fails a check, so the assets that read it were "
        "not built. Build it again (`lakana run --select silver.station_temperatures --env dev`), "
        "or repair the data and run again."
    )
    error = kept_no_checks_error(
        table=_TABLE, kept_run_id=_KEPT_RUN, environment="dev", unchecked=True
    )
    assert error == (
        f"`silver.station_temperatures` was kept after a check that could not run in run "
        f"{_KEPT_RUN}, and the project has no check for it any more, so nothing can pass. Only "
        "building it again clears this: "
        "`lakana run --select silver.station_temperatures --env dev`."
    )


def test_the_message_of_a_kept_table_that_has_no_checks_left() -> None:
    error = kept_no_checks_error(table=_TABLE, kept_run_id=_KEPT_RUN, environment="dev")
    assert error == (
        f"`silver.station_temperatures` was kept after a failed check in run {_KEPT_RUN}, and "
        "the project has no check for it any more, so nothing can pass. Only building it again "
        "clears this: `lakana run --select silver.station_temperatures --env dev`."
    )
    text = kept_check_message(
        [_result("(none)", CheckOutcome.ERRORED, error=error)],
        table=_TABLE,
        run_id=_RUN,
        environment="dev",
        kept_run_id=_KEPT_RUN,
        without_checks=True,
    )
    # The text stands alone: nothing says that a check "could not run" or asks for a deploy.
    assert text == error


def test_the_text_of_a_kept_table_without_checks_escapes_its_names() -> None:
    error = kept_no_checks_error(table="silver.caf\u00e9", kept_run_id=_KEPT_RUN, environment="d\n")
    assert "`silver.caf\\xe9`" in error
    assert error.endswith("`lakana run --select silver.caf\\xe9 --env d\\n`.")


def test_the_message_of_a_table_without_checks_whose_note_is_gone() -> None:
    text = kept_check_message(
        [_result("(none)", CheckOutcome.ERRORED, error="no check.")],
        table=_TABLE,
        run_id=_RUN,
        environment="dev",
        kept_run_id=None,
        without_checks=True,
    )
    assert text == (
        "Check `silver.station_temperatures/(none)` could not run "
        f"(checks/silver/station_temperatures/(none).sql): no check. {_AFTER}"
    )


def _refused_a_kept_table(check: str) -> CheckResult:
    """Build the result of a check whose count was refused another project's kept table.

    Parameters
    ----------
    check
        The check's short name.

    Returns
    -------
    CheckResult
        The errored result, with `ERROR_KEPT`.
    """
    return _result(check, CheckOutcome.ERRORED, error=ERROR_KEPT)


_REFUSED_SENTENCE = (
    "Check `silver.station_temperatures/known_key` could not run "
    f"(checks/silver/station_temperatures/known_key.sql): {ERROR_KEPT}"
)


def test_a_check_task_of_a_kept_table_whose_only_check_was_refused_says_it_could_not_run() -> None:
    text = kept_check_message(
        [],
        table=_TABLE,
        run_id=_RUN,
        environment="dev",
        kept_run_id=_KEPT_RUN,
        without_checks=False,
        refused=[_refused_a_kept_table("known_key")],
    )
    assert text == (
        f"{_REFUSED_SENTENCE} "
        "The checks of `silver.station_temperatures` could not run, so nothing that reads it "
        "was built. After the kept table that a check reads is cleared, run again; if the project "
        "did not change: "
        f"`lakana runs resume {_RUN} --env dev`."
    )
    assert "still fails" not in text


def test_several_refused_checks_are_each_said_to_not_have_run_never_to_have_failed() -> None:
    refused = [_refused_a_kept_table("a"), _refused_a_kept_table("b")]
    text = kept_failure_message(kept=[], run_id=_RUN, environment="dev", refused=refused)

    assert "2 checks" not in text
    assert text.count("could not run") >= 2
    assert "`silver.station_temperatures/a`" in text
    assert "`silver.station_temperatures/b`" in text


def test_a_source_check_names_every_refusal_once() -> None:
    text = refused_source_message(
        [],
        table=_TABLE,
        refusals=[
            "`a.t` was kept.",
            "`b.t` was kept.",
        ],
    )
    assert text == "`a.t` was kept. `b.t` was kept."
    failed = refused_source_message(
        [_result("c", CheckOutcome.FAILED, rows=1)], table=_TABLE, refusals=["`a.t` was kept."]
    )
    assert failed.endswith(" `a.t` was kept.")
    assert failed.startswith("Check `silver.station_temperatures/c` failed")


def test_a_check_task_of_a_kept_table_names_the_real_failure_then_the_refused_check() -> None:
    text = kept_check_message(
        [_result("unique_id", CheckOutcome.FAILED, rows=12)],
        table=_TABLE,
        run_id=_RUN,
        environment="dev",
        kept_run_id=_KEPT_RUN,
        without_checks=False,
        refused=[_refused_a_kept_table("known_key")],
    )
    assert text == (
        "Check `silver.station_temperatures/unique_id` failed: 12 rows "
        "(checks/silver/station_temperatures/unique_id.sql). "
        f"{_REFUSED_SENTENCE} "
        f"`silver.station_temperatures` was kept after a failed check in run {_KEPT_RUN} and "
        "still fails, so the assets that read it were not built. Build it again "
        "(`lakana run --select silver.station_temperatures --env dev`), or repair the data and "
        "run again."
    )


def test_a_real_failure_and_a_refused_check_of_a_table_whose_note_is_gone() -> None:
    text = kept_check_message(
        [_result("unique_id", CheckOutcome.FAILED, rows=1)],
        table=_TABLE,
        run_id=_RUN,
        environment="dev",
        kept_run_id=None,
        without_checks=False,
        refused=[_refused_a_kept_table("known_key")],
    )
    assert text == (
        "Check `silver.station_temperatures/unique_id` failed: 1 row "
        f"(checks/silver/station_temperatures/unique_id.sql). {_REFUSED_SENTENCE} {_AFTER}"
    )


def test_the_message_of_a_kept_table_whose_note_is_gone_leaves_out_the_false_sentence() -> None:
    text = kept_check_message(
        [_result("unique_id", CheckOutcome.FAILED, rows=1)],
        table=_TABLE,
        run_id=_RUN,
        environment="dev",
        kept_run_id=None,
        without_checks=False,
    )
    assert "does not build" not in text
    assert "was kept" not in text
    assert text.endswith(_AFTER)


def test_the_message_of_one_failed_check_that_was_rolled_back() -> None:
    message = _message(
        [_result("unique_reading", CheckOutcome.FAILED, rows=3)],
        Rollback(RollbackOutcome.ROLLED_BACK),
    )
    assert message == (
        "Check `silver.station_temperatures/unique_reading` failed: 3 rows "
        "(checks/silver/station_temperatures/unique_reading.sql). The table was set back to what "
        f"it was before this run, and nothing downstream was built. {_AFTER}"
    )
    assert "check rows" not in message


def _seen(result: CheckResult, snapshot: int | None = 7) -> CheckResult:
    """Give an unsaved result the snapshot its check read, so that its rows can be shown.

    Parameters
    ----------
    result
        The result.
    snapshot
        The snapshot id; `None` for a check that read no snapshot.

    Returns
    -------
    CheckResult
        The same result.
    """
    result.checked_snapshot_id = snapshot
    return result


_ROWS_OF_A = (
    f"Look at the rows: `lakana check rows {_RUN} silver.station_temperatures/a --env dev`."
)


def test_the_message_of_a_failed_check_names_the_command_that_shows_its_rows() -> None:
    message = _message(
        [_seen(_result("a", CheckOutcome.FAILED, rows=3))], Rollback(RollbackOutcome.ROLLED_BACK)
    )
    assert message == (
        "Check `silver.station_temperatures/a` failed: 3 rows "
        "(checks/silver/station_temperatures/a.sql). The table was set back to what it was "
        f"before this run, and nothing downstream was built. {_ROWS_OF_A} {_AFTER}"
    )


def test_the_message_names_the_command_for_at_most_three_checks_and_none_without_a_snapshot() -> (
    None
):
    results = [
        _seen(_result("e", CheckOutcome.FAILED, rows=1)),
        _seen(_result("d", CheckOutcome.FAILED, rows=1)),
        _seen(_result("c", CheckOutcome.FAILED, rows=1)),
        _seen(_result("b", CheckOutcome.FAILED, rows=1)),
        _seen(_result("a", CheckOutcome.FAILED, rows=1)),
        _seen(_result("none", CheckOutcome.FAILED, rows=1), None),
    ]
    message = _message(results, Rollback(RollbackOutcome.NOTHING_WRITTEN))
    assert message.endswith(f"{_look(_RUN, 'a', 'b', 'c')}{_AFTER}")
    assert message.count("Look at the rows") == 3
    assert "silver.station_temperatures/none --env" not in message
    skipped = _message(
        [_seen(_result("none", CheckOutcome.FAILED, rows=1), None)],
        Rollback(RollbackOutcome.NOTHING_WRITTEN),
    )
    assert "Look at the rows" not in skipped


def test_the_message_of_a_kept_table_names_the_command_before_what_to_do() -> None:
    text = kept_failure_message(
        kept=[
            _seen(_result("a", CheckOutcome.FAILED, on_failure="keep", rows=12)),
            _seen(_result("n", CheckOutcome.FAILED, on_failure="keep", rows=1), None),
        ],
        run_id=_RUN,
        environment="dev",
    )
    assert text == (
        "2 checks of `silver.station_temperatures` failed: `a` (12 rows), `n` (1 row). "
        f"{_ROWS_OF_A} "
        "The table keeps this run's data (`on_failure: keep`), and nothing that reads it was "
        "built. Repair the data or the asset; after a fix, deploy and run again; if the project "
        f"did not change: `lakana runs resume {_RUN} --env dev`."
    )


def test_the_message_of_a_kept_table_whose_checks_were_refused_names_no_command() -> None:
    text = kept_failure_message(
        kept=[], run_id=_RUN, environment="dev", refused=[_refused_a_kept_table("known_key")]
    )
    assert "Look at the rows" not in text


def test_the_message_of_a_check_task_of_a_kept_table_names_the_command() -> None:
    text = kept_check_message(
        [_seen(_result("a", CheckOutcome.FAILED, rows=12))],
        table=_TABLE,
        run_id=_RUN,
        environment="dev",
        kept_run_id=_KEPT_RUN,
        without_checks=False,
    )
    assert text == (
        "Check `silver.station_temperatures/a` failed: 12 rows "
        "(checks/silver/station_temperatures/a.sql). "
        f"{_ROWS_OF_A} "
        f"`silver.station_temperatures` was kept after a failed check in run {_KEPT_RUN} and "
        "still fails, so the assets that read it were not built. Build it again "
        "(`lakana run --select silver.station_temperatures --env dev`), or repair the data and "
        "run again."
    )
    gone = kept_check_message(
        [_seen(_result("a", CheckOutcome.FAILED, rows=12))],
        table=_TABLE,
        run_id=_RUN,
        environment="dev",
        kept_run_id=None,
        without_checks=False,
    )
    assert gone.endswith(f"{_ROWS_OF_A} {_AFTER}")
    unseen = kept_check_message(
        [_seen(_result("a", CheckOutcome.FAILED, rows=12), None)],
        table=_TABLE,
        run_id=_RUN,
        environment="dev",
        kept_run_id=_KEPT_RUN,
        without_checks=False,
    )
    assert "Look at the rows" not in unseen


def test_the_name_in_the_sentence_that_names_the_command_is_escaped() -> None:
    odd = _seen(
        CheckResult(
            check_name="silver.t/od\x1b[31md",
            check_path="checks/silver/t/od.sql",
            table_name="silver.t",
            on_failure="block",
            outcome=CheckOutcome.FAILED,
            failing_rows=2,
        )
    )
    text = failure_message(
        [odd], table="silver.t", run_id=_RUN, environment="de\x1bv", rollback=None
    )
    assert f"`lakana check rows {_RUN} CHECK --env 'de\\x1bv'`, with CHECK copied from" in text
    assert "\x1b" not in text


@pytest.mark.parametrize(
    ("check", "environment", "command"),
    [
        (
            "silver.t/no missing",
            "dev",
            f"`lakana check rows {_RUN} 'silver.t/no missing' --env dev`",
        ),
        (
            "silver.t/it's",
            "my env",
            f"`lakana check rows {_RUN} 'silver.t/it'\"'\"'s' --env 'my env'`",
        ),
        (
            "silver.t/plain_name-1",
            "dev",
            f"`lakana check rows {_RUN} silver.t/plain_name-1 --env dev`",
        ),
    ],
    ids=["a-space", "a-single-quote", "nothing-to-quote"],
)
def test_the_command_in_the_sentence_pastes_as_the_arguments_it_names(
    check: str, environment: str, command: str
) -> None:
    """The check and the environment are quoted for a shell where they need it."""
    result = _seen(
        CheckResult(
            check_name=check,
            check_path="checks/silver/t/c.sql",
            table_name="silver.t",
            on_failure="block",
            outcome=CheckOutcome.FAILED,
            failing_rows=2,
        )
    )

    sentences = rows_sentences([result], _RUN, environment)

    assert sentences == [f"Look at the rows: {command}."]
    pasted = shlex.split(sentences[0].split("`")[1])
    assert pasted == ["lakana", "check", "rows", str(_RUN), check, "--env", environment]


@pytest.mark.parametrize(
    "check",
    ["silver.t/caf\u00e9", "silver.t/a\\b", "silver.t/a\x07b", "silver.t/" + "x" * 250],
    ids=["umlaut", "backslash", "control", "too-long"],
)
def test_a_name_that_cannot_be_shown_as_it_is_is_to_be_copied_from_the_list(check: str) -> None:
    """The printed command would name another check, so the sentence says where to copy it."""
    result = _seen(
        CheckResult(
            check_name=check,
            check_path="checks/silver/t/c.sql",
            table_name="silver.t",
            on_failure="block",
            outcome=CheckOutcome.FAILED,
            failing_rows=2,
        )
    )

    (sentence,) = rows_sentences([result], _RUN, "dev")

    assert sentence == (
        f"Look at the rows: `lakana check rows {_RUN} CHECK --env dev`, with CHECK copied from "
        f"`lakana check list --run {_RUN} --env dev`."
    )


@pytest.mark.parametrize(
    ("rollback", "second"),
    [
        (
            Rollback(RollbackOutcome.ROLLED_BACK),
            "The table was set back to what it was before this run, and nothing downstream was "
            "built.",
        ),
        (
            Rollback(RollbackOutcome.UNPUBLISHED),
            "The table is not published: it had no published data before this run. Nothing "
            "downstream was built.",
        ),
        (
            Rollback(RollbackOutcome.NOTHING_WRITTEN),
            "The table was not changed by this run, and nothing downstream was built.",
        ),
        (
            Rollback(RollbackOutcome.NOT_A_TABLE),
            "It is a view, so there is nothing to set back; nothing downstream was built.",
        ),
        (
            Rollback(RollbackOutcome.CHANGED_SINCE),
            "The table was left as it is, because another writer changed it while this run was "
            "working on it; it may hold data of this run that failed its checks. Nothing "
            "downstream was built.",
        ),
        (
            Rollback(RollbackOutcome.CHANGED_SINCE, CHANGED_BY_ANOTHER_REASON),
            "The table was left as it is, because another writer changed it while this run was "
            "working on it; it may hold data of this run that failed its checks. Nothing "
            "downstream was built.",
        ),
        (
            Rollback(RollbackOutcome.CHANGED_SINCE, "the table was dropped"),
            "The table was not set back (the table was dropped). Nothing downstream was built.",
        ),
        (
            Rollback(RollbackOutcome.FAILED, "the table's metadata kept changing"),
            "The rollback failed (the table's metadata kept changing), so the table still holds "
            "this run's data. Nothing downstream was built.",
        ),
        (
            Rollback(RollbackOutcome.FAILED, ROLLBACK_REFUSED_REASON),
            "The rollback failed (the database refused or interrupted the rollback), so the "
            "table still holds this run's data. Nothing downstream was built.",
        ),
        (
            Rollback(RollbackOutcome.FAILED),
            "The rollback failed (unknown), so the table still holds this run's data. Nothing "
            "downstream was built.",
        ),
        (
            None,
            "This project does not build `silver.station_temperatures`, so it was not changed; "
            "the assets that read it were not built.",
        ),
    ],
)
def test_the_second_sentence_follows_the_rollback(rollback: Rollback | None, second: str) -> None:
    message = _message([_result("unique_reading", CheckOutcome.FAILED, rows=3)], rollback)
    assert message == (
        "Check `silver.station_temperatures/unique_reading` failed: 3 rows "
        f"(checks/silver/station_temperatures/unique_reading.sql). {second} {_AFTER}"
    )


def test_a_rollback_whose_outcome_is_not_known_does_not_say_the_table_holds_the_data() -> None:
    """Addition B.5: that sentence is false when nobody knows whether the rollback landed."""
    message = _message(
        [_result("unique_reading", CheckOutcome.FAILED, rows=3)],
        Rollback(RollbackOutcome.FAILED, ROLLBACK_NOT_KNOWN_REASON),
    )
    assert message == (
        "Check `silver.station_temperatures/unique_reading` failed: 3 rows "
        "(checks/silver/station_temperatures/unique_reading.sql). The rollback was interrupted "
        "and whether it took effect is not known; look at the table with "
        "`lakana table get silver.station_temperatures --env dev`. Nothing downstream was built. "
        f"{_AFTER}"
    )
    assert "still holds" not in message


def test_the_not_known_wording_is_chosen_by_the_constant_itself_not_by_its_text() -> None:
    """Addition B.5: a reason that only reads the same is an ordinary failed rollback."""
    copy = "".join(list(ROLLBACK_NOT_KNOWN_REASON))
    assert copy == ROLLBACK_NOT_KNOWN_REASON and copy is not ROLLBACK_NOT_KNOWN_REASON
    message = _message(
        [_result("unique_reading", CheckOutcome.FAILED, rows=3)],
        Rollback(RollbackOutcome.FAILED, copy),
    )
    assert f"The rollback failed ({ROLLBACK_NOT_KNOWN_REASON}), so the table still holds" in message


def test_the_message_of_a_check_that_could_not_run() -> None:
    message = _message(
        [_result("known_key", CheckOutcome.ERRORED, error=_E1)],
        Rollback(RollbackOutcome.ROLLED_BACK),
    )
    assert message == (
        "Check `silver.station_temperatures/known_key` could not run "
        f"(checks/silver/station_temperatures/known_key.sql): {_E1} Data that could not be "
        "checked is not published. The table was set back to what it was before this run, and "
        f"nothing downstream was built. {_AFTER}"
    )


def test_an_error_without_a_full_stop_gets_one() -> None:
    message = _message(
        [_result("known_key", CheckOutcome.ERRORED, error="it broke")],
        Rollback(RollbackOutcome.UNPUBLISHED),
    )
    assert "known_key.sql): it broke. Data that could not be checked is not published. " in message
    without = _message([_result("known_key", CheckOutcome.ERRORED)], None)
    assert "known_key.sql): the check's query failed. This project does not build" in without


@pytest.mark.parametrize(
    "rollback",
    [
        None,
        Rollback(RollbackOutcome.NOTHING_WRITTEN),
        Rollback(RollbackOutcome.NOT_A_TABLE),
        Rollback(RollbackOutcome.CHANGED_SINCE),
        Rollback(RollbackOutcome.FAILED, "x"),
        Rollback(RollbackOutcome.FAILED, ROLLBACK_NOT_KNOWN_REASON),
    ],
)
def test_the_sentence_about_unchecked_data_is_left_out_where_it_would_be_false(
    rollback: Rollback | None,
) -> None:
    """The table still holds the data (or was never the run's): "is not published" would lie."""
    message = _message([_result("known_key", CheckOutcome.ERRORED, error=_E1)], rollback)
    assert "Data that could not be checked is not published." not in message
    assert message.startswith(
        "Check `silver.station_temperatures/known_key` could not run "
        f"(checks/silver/station_temperatures/known_key.sql): {_E1} "
    )


def test_the_message_of_several_failed_checks() -> None:
    two = _message(
        [
            _result("b", CheckOutcome.ERRORED, error="x."),
            _result("a", CheckOutcome.FAILED, rows=3),
        ],
        Rollback(RollbackOutcome.ROLLED_BACK),
    )
    assert two == (
        "2 checks of `silver.station_temperatures` failed: `a` (3 rows), `b` (could not run). "
        "The table was set back to what it was before this run, and nothing downstream was "
        f"built. {_AFTER}"
    )
    seven = _message(
        [_result(f"c{i}", CheckOutcome.FAILED, rows=i) for i in range(1, 8)],
        Rollback(RollbackOutcome.ROLLED_BACK),
    )
    assert seven.startswith(
        "7 checks of `silver.station_temperatures` failed: `c1` (1 row), `c2` (2 rows), "
        "`c3` (3 rows), `c4` (4 rows), `c5` (5 rows) and 2 more. The table was set back"
    )


def test_one_row_is_singular() -> None:
    one = _message(
        [_result("unique_reading", CheckOutcome.FAILED, rows=1)],
        Rollback(RollbackOutcome.ROLLED_BACK),
    )
    assert "failed: 1 row (checks/" in one
    assert "failed: 0 rows (checks/" in _message(
        [_result("unique_reading", CheckOutcome.FAILED)], Rollback(RollbackOutcome.ROLLED_BACK)
    )


def test_the_sentence_of_a_table_that_is_not_published_is_true_for_a_rebuild_too() -> None:
    """No "built it for the first time"; an earlier run may have left it not published."""
    message = _message(
        [_result("unique_reading", CheckOutcome.ERRORED, error=ERROR_NOT_PUBLISHED)],
        Rollback(RollbackOutcome.UNPUBLISHED),
    )
    assert message == (
        "Check `silver.station_temperatures/unique_reading` could not run "
        "(checks/silver/station_temperatures/unique_reading.sql): a table this check reads is "
        "not published: its first build did not finish, or failed a check, and no run has "
        "published it since. "
        "Data that could not be checked is not published. The table is not published: it had "
        f"no published data before this run. Nothing downstream was built. {_AFTER}"
    )
    assert "for the first time" not in message


@pytest.mark.parametrize("reason", ["permission denied: x", ROLLBACK_NOT_KNOWN_REASON])
def test_a_refused_rollback_says_only_that_it_changed_nothing(reason: str) -> None:
    """The catalog may have found nothing to set back before it was refused."""
    message = _message(
        [_result("unique_reading", CheckOutcome.FAILED, rows=3)],
        Rollback(RollbackOutcome.FAILED, reason),
        refused=True,
    )
    assert message == (
        "Check `silver.station_temperatures/unique_reading` failed: 3 rows "
        "(checks/silver/station_temperatures/unique_reading.sql). The rollback was refused "
        f"({reason}); it did not change the table. Nothing downstream was built. {_AFTER}"
    )
    assert "still holds" not in message


@pytest.mark.parametrize(
    ("failure", "rollback", "refused", "expected"),
    [
        (
            "the task raised ValueError: boom",
            Rollback(RollbackOutcome.ROLLED_BACK),
            False,
            "the task raised ValueError: boom. The table was set back to what it was before "
            "this run, and nothing downstream was built.",
        ),
        (
            "It stopped.",
            Rollback(RollbackOutcome.UNPUBLISHED),
            False,
            "It stopped. The table is not published: it had no published data before this run. "
            "Nothing downstream was built.",
        ),
        (
            "boom",
            Rollback(RollbackOutcome.CHANGED_SINCE),
            False,
            "boom. The table was left as it is, because another writer changed it while this "
            "run was working on it; it may hold data of this run that was not checked. Nothing "
            "downstream was built.",
        ),
        (
            "boom",
            Rollback(RollbackOutcome.CHANGED_SINCE, CHANGED_BY_ANOTHER_REASON),
            False,
            "boom. The table was left as it is, because another writer changed it while this "
            "run was working on it; it may hold data of this run that was not checked. Nothing "
            "downstream was built.",
        ),
        (
            "boom",
            Rollback(RollbackOutcome.FAILED, ROLLBACK_NOT_KNOWN_REASON),
            False,
            "boom. The rollback was interrupted and whether it took effect is not known; look "
            "at the table with `lakana table get gold.scores --env dev`. Nothing downstream "
            "was built.",
        ),
        (
            None,
            Rollback(RollbackOutcome.FAILED, "x"),
            True,
            "the task failed. The rollback was refused (x); it did not change the table. "
            "Nothing downstream was built.",
        ),
    ],
)
def test_the_message_of_a_failed_build_that_was_set_back(
    failure: str | None, rollback: Rollback, refused: bool, expected: str
) -> None:
    """I1: what the build's failure was, and what became of the table."""
    assert (
        build_failure_message(
            failure, table="gold.scores", environment="dev", rollback=rollback, refused=refused
        )
        == expected
    )


def test_the_message_of_a_written_task_that_is_tried_again() -> None:
    """An endless retry says that the data is unchecked, and how to stop."""
    assert unchecked_retry_message("the run principal is deactivated or deleted", _RUN, "dev") == (
        "the run principal is deactivated or deleted. The table holds data of this run that is "
        "not checked yet; the checks are tried again. To stop: "
        f"`lakana runs cancel {_RUN} --env dev`."
    )
    assert unchecked_retry_message("Done.", _RUN, "de\x1bv").startswith("Done. The table holds")
    assert "\x1b" not in unchecked_retry_message("x", _RUN, "de\x1bv")


def test_the_warning_note() -> None:
    one = warning_note(
        [_result("plausible_range", CheckOutcome.WARNED, on_failure="warn", rows=12)]
    )
    assert one == "1 check warned: `plausible_range` (12 rows)."
    two = warning_note(
        [
            _result("b", CheckOutcome.ERRORED, on_failure="warn", error="x."),
            _result("a", CheckOutcome.WARNED, on_failure="warn", rows=12),
        ]
    )
    assert two == "2 checks warned: `a` (12 rows), `b` (could not run)."
    many = warning_note(
        [_result(f"w{i}", CheckOutcome.WARNED, on_failure="warn", rows=1) for i in range(6)]
    )
    assert many == (
        "6 checks warned: `w0` (1 row), `w1` (1 row), `w2` (1 row), `w3` (1 row), `w4` (1 row) "
        "and 1 more."
    )


def test_the_retry_message() -> None:
    assert RETRY_MESSAGE.format(check="silver.orders/unique_id", cause="internal") == (
        "Check `silver.orders/unique_id` could not be counted right now (internal); the checks "
        "run again on the next attempt. The table is not built again."
    )


@pytest.mark.parametrize("cause", ["permission_denied", "not_found"])
def test_the_error_text_names_the_principal_for_an_unreadable_table(cause: str) -> None:
    stored = "Resolving the tables this query reads failed (PermissionDenied)."
    assert error_text(cause, stored, "etl") == _E1
    assert error_text(cause, None, "e\x1b[31mtl").startswith("the run's principal `e\\x1b[31mtl` ")


@pytest.mark.parametrize(
    ("cause", "stored", "text"),
    [
        (
            "invalid_query",
            "Query failed: Conversion Error: Could not convert string 'Alice Smith' to INT32",
            f"Conversion Error: the check's query failed. {_SEE_YOURSELF}",
        ),
        (
            "invalid_query",
            'Query failed: Binder Error: Referenced column "nope" not found in FROM clause!',
            f"Binder Error: the check's query failed. {_SEE_YOURSELF}",
        ),
        (
            "out_of_memory",
            "Query failed: Out of Memory Error: could not allocate 'Alice Smith'",
            f"Out of Memory Error: the check's query failed. {_SEE_YOURSELF}",
        ),
        (
            "invalid_query",
            "Query failed: Alice Smith Error: 4111 1111 1111 1111",
            f"the check's query failed: the query is not valid. {_SEE_YOURSELF}",
        ),
        (
            "invalid_query",
            "View 'gold.v' no longer plans against its references (a): Conversion Error: 'Bob'",
            f"the check's query failed: the query is not valid. {_SEE_YOURSELF}",
        ),
        (
            "timeout",
            "Query exceeded the time limit of 0.5 s",
            f"the check's query failed: it ran longer than the time limit of a query. "
            f"{_SEE_YOURSELF}",
        ),
        (
            "out_of_memory",
            "the worker ran out of memory",
            f"the check's query failed: it ran out of memory. {_SEE_YOURSELF}",
        ),
        ("internal", "Unexpected error: Alice Smith", f"the check's query failed. {_SEE_YOURSELF}"),
        ("a cause from the future", "Alice", f"the check's query failed. {_SEE_YOURSELF}"),
        (None, None, f"the check's query failed. {_SEE_YOURSELF}"),
        (
            None,
            "Conversion Error: 'Alice'",
            f"Conversion Error: the check's query failed. {_SEE_YOURSELF}",
        ),
    ],
)
def test_the_error_text_never_repeats_what_the_query_said(
    cause: str | None, stored: str | None, text: str
) -> None:
    """Addition B.7: DuckDB quotes values of the table in its error sentence; a result has none."""
    produced = error_text(cause, stored, "etl")
    assert produced == text
    assert "Alice" not in produced and "Bob" not in produced and "4111" not in produced


def test_the_fixed_error_texts() -> None:
    assert ERROR_NOT_PUBLISHED == (
        "a table this check reads is not published: its first build did not finish, or failed a "
        "check, and no run has published it since."
    )
    assert ERROR_NO_CHECKS == (
        "the task was created with checks, but its project version lists none that can be read. "
        "Deploy a new commit of the project: that writes the project version afresh."
    )
    assert ERROR_UNREADABLE_CHECK == (
        "the project version lists a check that cannot be read, so this table cannot be checked "
        "completely. Deploy a new commit of the project: that writes the project version afresh."
    )
    assert ERROR_NO_PRINCIPAL == (
        "the run's principal is deactivated, so the check was not run. Activate the principal, "
        "or bind an active one to the deployment."
    )
    assert error_of_exception(KeyError("Alice Smith")) == (
        "the check could not be counted: the worker hit an unexpected error (KeyError). See the "
        "worker's log."
    )


def test_names_in_the_messages_are_escaped() -> None:
    odd = CheckResult(
        check_name="silver.t/od\x1b[31md",
        check_path="checks/silver/t/od\x1b[31md.sql",
        table_name="silver.t",
        on_failure="block",
        outcome=CheckOutcome.FAILED,
        failing_rows=2,
    )
    one = failure_message(
        [odd], table="silver.t\x1b", run_id=_RUN, environment="de\x1bv", rollback=None
    )
    several = failure_message(
        [odd, odd],
        table="silver.t\x1b",
        run_id=_RUN,
        environment="dev",
        rollback=Rollback(RollbackOutcome.FAILED, ROLLBACK_NOT_KNOWN_REASON),
    )
    note = warning_note([odd])
    for text in (one, several, note):
        assert "\x1b" not in text
        assert "od\\x1b[31md" in text
    assert "`silver.t\\x1b`" in one and "--env de\\x1bv`" in one
    assert "2 checks of `silver.t\\x1b` failed" in several
    assert "`lakana table get silver.t\\x1b --env dev`" in several


_COMMANDS = {
    "check list",
    "check rows",
    "deploy",
    "run",
    "runs cancel",
    "runs resume",
    "table get",
}
"""The commands the texts of this module may name: every one exists in the CLI."""


def test_no_text_of_this_module_names_a_command_that_does_not_exist() -> None:
    named = {
        match.group(1)
        for match in re.finditer(
            r"`lakana ((?:[a-z]+)(?: [a-z]+)?)", inspect.getsource(checks_module)
        )
    }
    # `lakana run --select T` names `run` alone; its next word is an option.
    assert named - _COMMANDS == set(), "a text names a command that is not in the CLI"
    assert "check rows" in named


def test_a_failed_rollback_is_noted_on_the_failed_blocking_results_only(
    session: Session, task: TaskRun
) -> None:
    _record(session, task, _spec("a"), CheckOutcome.FAILED, failing_rows=3)
    _record(session, task, _spec("b"), CheckOutcome.ERRORED, failing_rows=None, error="x.")
    _record(session, task, _spec("c"), CheckOutcome.PASSED)
    _record(session, task, _spec("d", on_failure="warn"), CheckOutcome.WARNED, failing_rows=1)
    _record(session, task, _spec("e", on_failure="warn"), CheckOutcome.ERRORED, failing_rows=None)

    assert mark_rollback_failed(session, task_run_id=task.id, worker="w1", claim=1)

    session.rollback()
    noted = {
        r.check_name.rpartition("/")[2]: r.rollback_outcome
        for r in results_of_task(session, task.id)
    }
    assert noted == {
        "a": RollbackOutcome.FAILED,
        "b": RollbackOutcome.FAILED,
        "c": None,
        "d": None,
        "e": None,
    }


def test_an_outcome_the_catalog_recorded_is_not_overwritten(
    session: Session, task: TaskRun
) -> None:
    _record(session, task, _spec("a"), CheckOutcome.FAILED, failing_rows=3)
    (row,) = results_of_task(session, task.id)
    row.rollback_outcome = RollbackOutcome.ROLLED_BACK
    session.commit()

    assert mark_rollback_failed(session, task_run_id=task.id, worker="w1", claim=1)

    session.rollback()
    (row,) = results_of_task(session, task.id)
    assert row.rollback_outcome is RollbackOutcome.ROLLED_BACK


@pytest.mark.parametrize("case", ["wrong-worker", "wrong-claim", "pending"])
def test_a_worker_that_lost_its_claim_notes_no_failed_rollback(
    session: Session, task: TaskRun, case: str
) -> None:
    _record(session, task, _spec("a"), CheckOutcome.FAILED, failing_rows=3)
    worker, claim = "w1", 1
    if case == "wrong-worker":
        worker = "w2"
    elif case == "wrong-claim":
        claim = 2
    else:
        task.status = TaskStatus.PENDING
        session.commit()

    assert not mark_rollback_failed(session, task_run_id=task.id, worker=worker, claim=claim)

    session.rollback()
    (row,) = results_of_task(session, task.id)
    assert row.rollback_outcome is None


_DROPPED_REASON = "the table was dropped while the rollback was prepared"


@pytest.mark.parametrize(
    ("rollback", "refused", "expected"),
    [
        (Rollback(RollbackOutcome.ROLLED_BACK), False, f"Why. {SET_BACK_DONE}"),
        (Rollback(RollbackOutcome.UNPUBLISHED), False, f"Why. {SET_BACK_DONE}"),
        (Rollback(RollbackOutcome.NOTHING_WRITTEN), False, f"Why. {SET_BACK_NOTHING}"),
        (Rollback(RollbackOutcome.NOT_A_TABLE), False, f"Why. {SET_BACK_NOTHING}"),
        (
            Rollback(RollbackOutcome.CHANGED_SINCE, CHANGED_BY_ANOTHER_REASON),
            False,
            f"Why. {SET_BACK_CHANGED}",
        ),
        (
            Rollback(RollbackOutcome.CHANGED_SINCE, _DROPPED_REASON),
            False,
            f"Why. The table was not set back ({_DROPPED_REASON}). Nothing downstream was built.",
        ),
        (
            Rollback(RollbackOutcome.FAILED, ROLLBACK_NOT_KNOWN_REASON),
            False,
            "Why. The rollback was interrupted and whether it took effect is not known; look at "
            "the table with `lakana table get silver.orders --env dev`. Nothing downstream was "
            "built.",
        ),
        (Rollback(RollbackOutcome.FAILED, "x"), False, set_back_failed_message("Why.")),
        (Rollback(RollbackOutcome.FAILED, "x"), True, set_back_failed_message("Why.")),
    ],
)
def test_the_message_of_a_set_back_claim_says_what_became_of_the_table(
    rollback: Rollback, refused: bool, expected: str
) -> None:
    """The lost or cancel message, then the table's fate in the words the failed-check path uses."""
    assert (
        set_back_result_message("Why", rollback, refused, table="silver.orders", environment="dev")
        == expected
    )
