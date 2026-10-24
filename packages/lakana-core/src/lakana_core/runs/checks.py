"""The results of the data quality checks of a run: kept, read, judged and described.

A result is a count and, for a check that could not run, its error text. It never holds a row
of a table, and nothing here may add one: `error_text` keeps the type of a failed query and
drops its sentence, because the query engine quotes values of the table in it.

The verdict (`verdict`) and every text a failed, warned or retried check produces live here,
because the worker writes them and the server shows them. Each text is a module constant.
"""

from __future__ import annotations

import re
import shlex
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from lakana_core.audit.service import AuditService
from lakana_core.auth import Securable
from lakana_core.db import db_now
from lakana_core.identifiers import TableRef, shown
from lakana_core.models import (
    AuditEventType,
    CheckOutcome,
    CheckResult,
    Environment,
    RollbackOutcome,
    Run,
    TaskRun,
    TaskStatus,
)
from lakana_core.policy.read_errors import is_policy_read_failure
from lakana_core.projects.checks import CheckSpec

if TYPE_CHECKING:
    from lakana_core.catalog.service import Rollback

SOURCE_CHECK = "source_check"
"""The kind of a task that only checks a table the project does not build."""

ASSET = "asset"
"""The kind of a task that builds a table."""

ROLLBACK_PHRASES: dict[str, str] = {
    "rolled_back": "rolled back",
    "unpublished": "not published",
    "nothing_written": "nothing was written",
    "not_a_table": "a view, nothing to set back",
    "changed_since": "left as it is, changed since",
    "failed": "rollback failed",
}
"""What each outcome of a rollback reads as, in one short phrase."""

ERROR_UNREADABLE = (
    "the run's principal `{principal}` may not read a table this check names, or the table does "
    "not exist. Grant it READ on the tables the check reads, or change the check."
)
"""A check's `error` when its count was refused or found no table (text E1).

The query path answers both the same way, so that a principal learns nothing about a table it
may not read; this text therefore names neither the table nor which of the two it was.
"""

ERROR_NOT_PUBLISHED = (
    "a table this check reads is not published: its first build did not finish, or failed a "
    "check, and no run has published it since."
)
"""A check's `error` when a table it reads is not published (names no table)."""

ERROR_KEPT = (
    "a table this check reads was kept after its checks failed or could not run, or was set back "
    "to data whose checks are not known, and has not passed its checks since, so the check could "
    "not read it. `lakana table get` on each table the check reads shows which one, and why."
)
"""A check's `error` when its count was refused a kept table (`KeptAfterCheckError`).

Names no table, check or run: whoever may read the run reads a check's `error`, and the note
belongs to the project that builds the kept table. The refusal in full is the message of the
count's query run."""

ERROR_NO_PRINCIPAL = (
    "the run's principal is deactivated, so the check was not run. Activate the principal, or "
    "bind an active one to the deployment."
)
"""A check's `error` when the run's principal was deactivated after the table was written."""

ERROR_NO_CHECKS = (
    "the task was created with checks, but its project version lists none that can be read. "
    "Deploy a new commit of the project: that writes the project version afresh."
)
"""The `error` of the one result of a task that should have checks and has none to run.

Such a task does not succeed unchecked: the result is blocking and `errored`.
"""

ERROR_UNREADABLE_CHECK = (
    "the project version lists a check that cannot be read, so this table cannot be checked "
    "completely. Deploy a new commit of the project: that writes the project version afresh."
)
"""The `error` of the one extra result of a task whose project version holds a check entry it cannot
read, beside the results of the checks that can be read.

Such a task does not succeed: the result is blocking and `errored`, as for `ERROR_NO_CHECKS`.
"""

_ERROR_UNEXPECTED = (
    "the check could not be counted: the worker hit an unexpected error ({error}). See the "
    "worker's log."
)
"""A check's `error` when counting it raised; `{error}` is the exception's class name only."""

_QUERY_FAILED = "the check's query failed"
"""What every error of a failed count says, whatever the query engine said."""

_SEE_YOURSELF = "Run the check's SQL yourself to see the full message."
"""The next step for a failed count: a result repeats nothing the query engine said."""

_CAUSE_WORDS: dict[str, str] = {
    "timeout": "it ran longer than the time limit of a query",
    "out_of_memory": "it ran out of memory",
    "invalid_query": "the query is not valid",
}
"""What a `QueryErrorCause` reads as in a check's `error`; any other cause adds nothing."""

_ENGINE_ERROR_TYPE = re.compile(r"^(?:Query failed: )?([A-Z][A-Za-z]*(?: [A-Za-z]+)*) Error:")
"""Matches the error type DuckDB puts in front of its message, as the query path stores it."""

_ENGINE_ERROR_TYPES = frozenset(
    {
        "Binder",
        "Catalog",
        "Constraint",
        "Conversion",
        "Dependency",
        "Invalid Input",
        "Invalid Type",
        "Mismatch Type",
        "Not implemented",
        "Out of Memory",
        "Out of Range",
        "Parser",
        "Sequence",
        "Serialization",
        "Type",
    }
)
"""The DuckDB error types a check's `error` may name.

A closed list: the type is read from the front of a text that also holds values of the table,
so nothing is taken from it that is not one of these words.
"""

_ONE_FAILED = "Check `{name}` failed: {rows} ({path})."
"""The first sentence of the task's message for one failed blocking check (text M1)."""

_ONE_ERRORED = "Check `{name}` could not run ({path}): {error}"
"""The first sentence for one blocking check that could not run (text M1, second form)."""

_UNCHECKED_NOT_PUBLISHED = "Data that could not be checked is not published."
"""Follows `_ONE_ERRORED` when the rollback took the run's data out of the table."""

_SEVERAL_FAILED = "{count} checks of `{table}` failed: {listing}."
"""The first sentence for several failed blocking checks (text M2)."""

_LISTED_CHECKS = 5
"""How many checks a message names before it counts the rest."""

ROLLBACK_SENTENCES: dict[RollbackOutcome, str] = {
    RollbackOutcome.ROLLED_BACK: (
        "The table was set back to what it was before this run, and nothing downstream was built."
    ),
    RollbackOutcome.UNPUBLISHED: (
        "The table is not published: it had no published data before this run. Nothing "
        "downstream was built."
    ),
    RollbackOutcome.NOTHING_WRITTEN: (
        "The table was not changed by this run, and nothing downstream was built."
    ),
    RollbackOutcome.NOT_A_TABLE: (
        "It is a view, so there is nothing to set back; nothing downstream was built."
    ),
    RollbackOutcome.CHANGED_SINCE: (
        "The table was left as it is, because another writer changed it while this run was "
        "working on it; it may hold data of this run {unchecked}. Nothing downstream was built."
    ),
    RollbackOutcome.FAILED: (
        "The rollback failed ({reason}), so the table still holds this run's data. Nothing "
        "downstream was built."
    ),
}
"""The second sentence of the task's message, by what the rollback did (text M3)."""

_NOT_SET_BACK = "The table was not set back ({reason}). Nothing downstream was built."
"""The second sentence for `changed_since` with a reason (the table was dropped meanwhile)."""

_ROLLBACK_NOT_KNOWN = (
    "The rollback was interrupted and whether it took effect is not known; look at the table "
    "with `lakana table get {table} --env {environment}`. Nothing downstream was built."
)
"""The second sentence when the catalog could not read back whether its rollback landed."""

_ROLLBACK_REFUSED = (
    "The rollback was refused ({reason}); it did not change the table. Nothing downstream was "
    "built."
)
"""The second sentence when the catalog refused the rollback (and the claim is still held).

It says nothing about what the table holds: the refusal can come after the catalog found that
there was nothing to set back.
"""

_NOT_SET_BACK_AT_ALL = (
    "The table could not be set back; it may hold data of this run that was not checked."
)
"""Follows the failure of a build whose set-back could not be prepared or carried out."""

RUN_CANCELLED_MESSAGE = "The run was cancelled."
"""`error_message` of a task that ended because a user cancelled its run, whichever way it was
stopped (its worker, the scheduler, or the set-back that follows)."""

RUN_STOPPED_MESSAGE = "The run was stopped because another task failed its code integrity check."
"""The same, for a run that `fail_run` failed: it sets the cancel flag too, but nobody cancelled."""

SET_BACK_DONE = "The table was set back to before this task ran."
"""Follows the lost or cancel message when the set-back took effect."""

SET_BACK_NOTHING = "The task had written nothing to its table, so nothing was set back."
"""Follows the lost or cancel message when the table was at its baseline and had no tip."""

SET_BACK_CHANGED = (
    "The table changed after this task wrote it, so it was not set back and may still hold rows "
    "no check has passed. Run again, which rebuilds the table, or fix the data by hand."
)
"""Follows the lost or cancel message when someone else wrote the table after the task did."""

SET_BACK_LOST = (
    "The table could not be set back: the worker was lost twice while setting it back. The table "
    "may still hold rows no check has passed. Run again, which rebuilds the table, or fix the "
    "data by hand."
)
"""`error_message` of a task whose set-back claims were lost until the limit was reached."""

_STILL_UNCHECKED = (
    "The table holds data of this run that is not checked yet; the checks are tried again. To "
    "stop: `lakana runs cancel {run} --env {environment}`."
)
"""Follows the failure of an attempt of a written task whose checks could not be run at all."""

_SOURCE_NOT_BUILT = (
    "This project does not build `{table}`, so it was not changed; the assets that read it "
    "were not built."
)
"""The second sentence of a source check's message (text M4)."""

_AFTER_A_FIX = (
    "After a fix, deploy and run again; if the project did not change: "
    "`lakana runs resume {run} --env {environment}`."
)
"""The closing sentence of the task's message (a run is not resumed after a new deploy)."""

_KEPT = (
    "The table keeps this run's data (`on_failure: keep`), and nothing that reads it was "
    "built. Repair the data or the asset; after a fix, deploy and run again; if the project "
    "did not change: `lakana runs resume {run} --env {environment}`."
)
"""The rest of the task's message when only `keep` checks failed (text K1)."""

_KEPT_DROPPED = "The table was dropped while its checks ran, so nothing is kept."
"""The rest of that message when the table is gone by the time the note would be set."""

_KEPT_NOT_CHECKED = (
    "The checks of `{table}` could not run, so nothing that reads it was built. After the kept "
    "table that a check reads is cleared, run again; if the project did not change: "
    "`lakana runs resume {run} --env {environment}`."
)
"""The rest of the task's message when no `keep` check failed and every one of them was refused
a kept table (`ERROR_KEPT`).

The table that is kept is the one a check reads, which another project builds. The task's own
table gets no new note, unless it carried one already: then the note moves to this run and
`lakana table get` says so. So the message says nothing about what the own table keeps."""

_KEPT_AGAIN_UNCHECKED = (
    "`{table}` was kept because its checks could not run in run {kept_run}, and now fails a "
    "check, so the assets that read it were not built. Build it again "
    "(`lakana run --select {table} --env {environment}`), or repair the data and run again."
)
"""`_KEPT_AGAIN` for a note that names a check that could not run."""

_KEPT_AGAIN_UNKNOWN = (
    "`{table}` was set back to data whose checks are not known (run {kept_run} was set back), "
    "and now fails a check, so the assets that read it were not built. Build it again "
    "(`lakana run --select {table} --env {environment}`), or repair the data and run again."
)
"""`_KEPT_AGAIN` for a note that says the table was set back to data whose checks are not known."""

_KEPT_AGAIN = (
    "`{table}` was kept after a failed check in run {kept_run} and still fails, so the assets "
    "that read it were not built. Build it again (`lakana run --select {table} --env "
    "{environment}`), or repair the data and run again."
)
"""The rest of the message of a check task of a kept table that fails again (text K3)."""

ERROR_KEPT_NO_CHECKS_UNCHECKED = (
    "`{table}` was kept after a check that could not run in run {kept_run}, and the project has "
    "no check for it any more, so nothing can pass. Only building it again clears this: "
    "`lakana run --select {table} --env {environment}`."
)
"""`ERROR_KEPT_NO_CHECKS` for a note that names a check that could not run."""

ERROR_KEPT_NO_CHECKS_UNKNOWN = (
    "`{table}` was set back to data whose checks are not known (run {kept_run} was set back), and "
    "the project has no check for it any more, so nothing can pass. Only building it again "
    "clears this: `lakana run --select {table} --env {environment}`."
)
"""`ERROR_KEPT_NO_CHECKS` for a note that says the table was set back to data whose checks are not
known."""

ERROR_KEPT_NO_CHECKS = (
    "`{table}` was kept after a failed check in run {kept_run}, and the project has no check "
    "for it any more, so nothing can pass. Only building it again clears this: "
    "`lakana run --select {table} --env {environment}`."
)
"""The `error` of the one result, and the whole message, of the check task of a kept table of
the project whose project version has no check for the table any more.

The project removed the table's checks after the table was kept. `ERROR_NO_CHECKS` would be
false there: nothing is wrong with the project version, and a new deploy changes nothing. The
table's readers stay stopped until a build of the table clears the note
(`KEPT_WHY`: ``rebuilt_without_checks``).
"""

_LOOK_AT_THE_ROWS_UNSHOWN = (
    "Look at the rows: `lakana check rows {run} CHECK --env {environment}`, with CHECK copied "
    "from `lakana check list --run {run} --env {environment}`."
)
"""The same sentence for a check whose name cannot be printed as it is.

A name with a non-ASCII letter, a backslash or a control character, or one longer than the
messages show, would be printed as an escape or cut, and the command would name another check.
"""

_LOOK_AT_THE_ROWS = "Look at the rows: `lakana check rows {run} {check} --env {environment}`."
"""The sentence that names the command which shows the rows a failed check found.

Said once for each failing check that read a snapshot (a check that read none has no rows to
show), before the closing sentence of a message. The check and the environment are quoted for
a shell where they need it, so that the command can be pasted: a check's name is its file's
name and may hold a space.
"""

_ROWS_NAMED = 3
"""For how many checks one message names the command."""

_WARNED = "{count} {checks} warned: {listing}."
"""The message of a task that succeeded although a `warn` check failed (text M5)."""

RETRY_MESSAGE = (
    "Check `{check}` could not be counted right now ({cause}); the checks run again on the next "
    "attempt. The table is not built again."
)
"""The message of an attempt whose count failed for a transient reason (text M6)."""


@dataclass(frozen=True)
class CheckResultView:
    """One check result, as the API shows it.

    Parameters
    ----------
    run_id
        The run.
    task
        The task run's asset (the checked table).
    name
        ``<table>/<check>``.
    check
        The check's short name.
    path
        Repository-relative path of the check's file.
    table
        The dotted name of the checked table.
    table_id
        The table's id, or `None` when it was not resolved.
    on_failure
        ``block`` or ``warn``.
    outcome
        ``passed``, ``failed``, ``warned`` or ``errored``.
    failing_rows
        The count of rows that are wrong, when the check ran.
    error
        Why a check could not run.
    checked_snapshot_id
        The snapshot the check read.
    rollback_outcome
        What the rollback did, if one followed.
    policy_applied
        Whether a row filter or column mask applied to the run's principal.
    policy_note
        Text saying so, or `None` when none applied.
    query_run_id
        The query that ran the check.
    started_at
        Start.
    finished_at
        End.
    """

    run_id: uuid.UUID
    task: str
    name: str
    check: str
    path: str
    table: str
    table_id: uuid.UUID | None
    on_failure: str
    outcome: str
    failing_rows: int | None
    error: str | None
    checked_snapshot_id: int | None
    rollback_outcome: str | None
    policy_applied: bool
    policy_note: str | None
    query_run_id: uuid.UUID | None
    started_at: datetime
    finished_at: datetime


def policy_note(principal: str | None) -> str:
    """Say that a check saw less than the whole table.

    Parameters
    ----------
    principal
        The run's principal name, or `None` when that principal no longer exists.

    Returns
    -------
    str
        The note, with the name escaped.
    """
    who = "the run's principal" if principal is None else shown(principal)
    return (
        f"checked under a row filter or column mask of {who}: "
        "rows and values it cannot see were not checked"
    )


def view_of(row: CheckResult, task: str, run_as: str | None) -> CheckResultView:
    """Build the view of one stored result.

    Parameters
    ----------
    row
        The result.
    task
        The asset of the task run it belongs to.
    run_as
        The run's principal name, or `None` when that principal no longer exists.

    Returns
    -------
    CheckResultView
        The view.
    """
    return CheckResultView(
        run_id=row.run_id,
        task=task,
        name=row.check_name,
        check=row.check_name.rpartition("/")[2],
        path=row.check_path,
        table=row.table_name,
        table_id=row.table_id,
        on_failure=row.on_failure,
        outcome=row.outcome.value,
        failing_rows=row.failing_rows,
        error=row.error,
        checked_snapshot_id=row.checked_snapshot_id,
        rollback_outcome=None if row.rollback_outcome is None else row.rollback_outcome.value,
        policy_applied=row.policy_applied,
        policy_note=policy_note(run_as) if row.policy_applied else None,
        query_run_id=row.query_run_id,
        started_at=row.started_at,
        finished_at=row.finished_at,
    )


@dataclass(frozen=True)
class Verdict:
    """What the results of a task's checks mean for the task.

    Parameters
    ----------
    blocking
        The `block` checks that failed or could not run, by check name. One is enough for the
        table to be rolled back and the task to fail.
    kept
        The `keep` checks that failed or could not run, by check name. They fail the task but
        leave the run's data in the table.
    warned
        The `warn` checks that failed or could not run, by check name.
    """

    blocking: tuple[CheckResult, ...]
    kept: tuple[CheckResult, ...]
    warned: tuple[CheckResult, ...]


def verdict(results: Sequence[CheckResult]) -> Verdict:
    """Split results into what blocks, what keeps and what only warns; each sorted by name.

    A `block` or `keep` check that could not run counts as failed: data that could not be
    checked is not published, or is kept and reported.

    Parameters
    ----------
    results
        The results of one task run.

    Returns
    -------
    Verdict
        The failed `block` checks, the failed `keep` checks and the `warn` checks that failed
        or could not run.
    """
    ordered = sorted(results, key=lambda result: result.check_name)
    failing = (CheckOutcome.FAILED, CheckOutcome.ERRORED)
    return Verdict(
        blocking=tuple(r for r in ordered if r.on_failure == "block" and r.outcome in failing),
        kept=tuple(r for r in ordered if r.on_failure == "keep" and r.outcome in failing),
        warned=tuple(
            r
            for r in ordered
            if r.on_failure == "warn" and r.outcome in (CheckOutcome.WARNED, CheckOutcome.ERRORED)
        ),
    )


def error_text(cause: str | None, stored: str | None, principal: str) -> str:
    """Word why a check's count failed, without anything the table holds.

    The stored text of a failed query can quote a value of the table ("Could not convert string
    'Alice Smith' to INT32"), and whoever may read the run may read a check's `error`. So the
    text is built from the cause and from the error type in front of the stored text, when that
    is one of the query engine's own type names, and from nothing else of it.

    Parameters
    ----------
    cause
        The count's `QueryRun.error_cause`, if any.
    stored
        The count's stored `QueryRun.error_message`, if any; only its leading error type is
        read.
    principal
        The run's principal name.

    Returns
    -------
    str
        `ERROR_UNREADABLE` for a refused or missing table; otherwise the engine's error type
        or the cause in words, and the next step.
    """
    if cause in {"permission_denied", "not_found"}:
        return ERROR_UNREADABLE.format(principal=shown(principal))
    if is_policy_read_failure(stored):
        # Fixed wording that names a table and no value: safe to show as it is.
        return stored or ""
    matched = _ENGINE_ERROR_TYPE.match(stored or "")
    if matched is not None and matched.group(1) in _ENGINE_ERROR_TYPES:
        return f"{matched.group(1)} Error: {_QUERY_FAILED}. {_SEE_YOURSELF}"
    words = _CAUSE_WORDS.get(cause or "")
    if words is not None:
        return f"{_QUERY_FAILED}: {words}. {_SEE_YOURSELF}"
    return f"{_QUERY_FAILED}. {_SEE_YOURSELF}"


def error_of_exception(exc: BaseException) -> str:
    """Word the `error` of a check whose counting raised: the exception's type, never its text.

    Parameters
    ----------
    exc
        What was raised while the check was counted.

    Returns
    -------
    str
        The text.
    """
    return _ERROR_UNEXPECTED.format(error=type(exc).__name__)


def _rows(count: int | None) -> str:
    """Word a count of failing rows.

    Parameters
    ----------
    count
        The count; `None` reads as 0.

    Returns
    -------
    str
        ``1 row`` or ``N rows``.
    """
    return "1 row" if count == 1 else f"{count or 0} rows"


def _listing(results: Sequence[CheckResult]) -> str:
    """List checks by their short names with what each found, sorted, at most five.

    Parameters
    ----------
    results
        The results to list.

    Returns
    -------
    str
        `` `a` (3 rows), `b` (could not run) ``, and `` and N more`` past five.
    """
    items = sorted(
        (
            shown(result.check_name.rpartition("/")[2]),
            "could not run"
            if result.outcome is CheckOutcome.ERRORED
            else _rows(result.failing_rows),
        )
        for result in results
    )
    listed = ", ".join(f"`{name}` ({found})" for name, found in items[:_LISTED_CHECKS])
    more = len(items) - _LISTED_CHECKS
    return listed if more <= 0 else f"{listed} and {more} more"


def _first_sentence(blocking: Sequence[CheckResult], table: str, *, set_back: bool) -> str:
    """Say which blocking checks failed (texts M1 and M2).

    Parameters
    ----------
    blocking
        The failed blocking results.
    table
        The checked table.
    set_back
        Whether the rollback took the run's data out of the table.

    Returns
    -------
    str
        The sentence, or for one check that could not run its error and, when the data is
        gone from the table, that unchecked data is not published.
    """
    if len(blocking) != 1:
        return _SEVERAL_FAILED.format(
            count=len(blocking), table=shown(table), listing=_listing(blocking)
        )
    (only,) = blocking
    name, path = shown(only.check_name), shown(only.check_path)
    if only.outcome is not CheckOutcome.ERRORED:
        return _ONE_FAILED.format(name=name, rows=_rows(only.failing_rows), path=path)
    error = only.error or _QUERY_FAILED
    sentence = _ONE_ERRORED.format(
        name=name, path=path, error=error if error.endswith(".") else f"{error}."
    )
    return f"{sentence} {_UNCHECKED_NOT_PUBLISHED}" if set_back else sentence


def _special_sentence(table: str, environment: str, rollback: Rollback) -> str | None:
    """Say the two things a rollback can leave that no outcome's own sentence covers.

    Parameters
    ----------
    table
        The checked table.
    environment
        The environment's name.
    rollback
        What the rollback did.

    Returns
    -------
    str or None
        That it is not known whether the rollback landed, or that the table is gone (dropped
        while the rollback was prepared) and holds nothing; `None` for any other result.
    """
    # Imported here: the catalog imports this module.
    from lakana_core.catalog.service import CHANGED_BY_ANOTHER_REASON, ROLLBACK_NOT_KNOWN_REASON

    if rollback.outcome is RollbackOutcome.FAILED and rollback.reason is ROLLBACK_NOT_KNOWN_REASON:
        return _ROLLBACK_NOT_KNOWN.format(table=shown(table), environment=shown(environment))
    if rollback.outcome is RollbackOutcome.CHANGED_SINCE and rollback.reason not in {
        None,
        CHANGED_BY_ANOTHER_REASON,
    }:
        return _NOT_SET_BACK.format(reason=rollback.reason)
    return None


def _second_sentence(
    table: str,
    environment: str,
    rollback: Rollback | None,
    refused: bool,
    unchecked: str = "that failed its checks",
) -> str:
    """Say what became of the table (texts M3 and M4).

    Parameters
    ----------
    table
        The checked table.
    environment
        The environment's name.
    rollback
        What the rollback did; `None` for a source check.
    refused
        Whether the catalog refused the rollback (`rollback.reason` says why).
    unchecked
        What the run's data that may still be in the table is: it failed its checks, or (after a
        failed build) it was never checked.

    Returns
    -------
    str
        The sentence.
    """
    if rollback is None:
        return _SOURCE_NOT_BUILT.format(table=shown(table))
    if refused:
        return _ROLLBACK_REFUSED.format(reason=rollback.reason or "unknown")
    special = _special_sentence(table, environment, rollback)
    if special is not None:
        # Not known whether it landed, or the table is gone (dropped meanwhile): it holds nothing.
        return special
    return ROLLBACK_SENTENCES[rollback.outcome].format(
        reason=rollback.reason or "unknown", unchecked=unchecked
    )


def rows_sentences(
    results: Sequence[CheckResult], run_id: uuid.UUID, environment: str
) -> list[str]:
    """Name the command that shows the rows of the failing checks that have some.

    Parameters
    ----------
    results
        The failing results a message is about.
    run_id
        The run.
    environment
        The environment's name.

    Returns
    -------
    list of str
        One sentence for each result that read a snapshot, sorted by check name, at most three:
        a check that read none has no rows to show, and a longer message would only repeat
        itself. The three are counted among the checks that read a snapshot. A name that a
        shell would split or interpret is in quotes; any other reads as it is.
    """
    names = sorted(r.check_name for r in results if r.checked_snapshot_id is not None)
    return [
        _LOOK_AT_THE_ROWS.format(
            run=run_id,
            check=shlex.quote(name),
            environment=shlex.quote(shown(environment)),
        )
        if shown(name) == name
        else _LOOK_AT_THE_ROWS_UNSHOWN.format(
            run=run_id, environment=shlex.quote(shown(environment))
        )
        for name in names[:_ROWS_NAMED]
    ]


def failure_message(
    blocking: Sequence[CheckResult],
    *,
    table: str,
    run_id: uuid.UUID,
    environment: str,
    rollback: Rollback | None,
    refused: bool = False,
) -> str:
    """Build the message of a task whose blocking checks failed.

    Which checks failed, what became of the table, one sentence per failing check that read a
    snapshot that names the command that shows its rows (at most three), and the next step. Every
    name and path is escaped. A rollback whose outcome the catalog could not read back
    (`ROLLBACK_NOT_KNOWN_REASON`, compared by identity) gets a sentence of its own: nobody
    knows then whether the table still holds the run's data.

    Parameters
    ----------
    blocking
        The failed blocking results (`Verdict.blocking`); at least one.
    table
        The checked table's dotted name.
    run_id
        The run, for the next step.
    environment
        The environment's name.
    rollback
        What the rollback did; `None` for a source check, whose table the run did not write.
    refused
        Whether the catalog refused the rollback while the claim was held; the sentence then
        says only that the rollback changed nothing.

    Returns
    -------
    str
        The message.
    """
    set_back = rollback is not None and rollback.outcome in {
        RollbackOutcome.ROLLED_BACK,
        RollbackOutcome.UNPUBLISHED,
    }
    return " ".join(
        (
            _first_sentence(blocking, table, set_back=set_back),
            _second_sentence(table, environment, rollback, refused),
            *rows_sentences(blocking, run_id, environment),
            _AFTER_A_FIX.format(run=run_id, environment=shown(environment)),
        )
    )


def kept_failure_message(
    *,
    kept: Sequence[CheckResult],
    run_id: uuid.UUID,
    environment: str,
    refused: Sequence[CheckResult] = (),
    dropped: bool = False,
) -> str:
    """Build the message of a task whose `keep` checks failed and whose table was left alone (K1).

    Parameters
    ----------
    kept
        The failed `keep` results the table is noted for, all of one table: `Verdict.kept`
        without those in `refused`. Empty only when `refused` is not.
    run_id
        The run, for the next step.
    environment
        The environment's name.
    refused
        The `keep` results of the same table that could not run because the count was refused
        a kept table (`ERROR_KEPT`): they set no note of their own. When the table carried a
        note already, the note moves to this run, marked as unchecked.
    dropped
        Whether the table was dropped while the checks ran: nothing is kept then, and no rows
        can be shown.

    Returns
    -------
    str
        Which checks failed, as for a blocking check, then one sentence per refused check, then
        that the table keeps the data and what to do. When no check failed and every one was
        refused: which checks could not run, then that nothing that reads the table was built;
        nothing says that the table keeps data (it carries no note, or its old note moved to
        this run as unchecked, which `lakana table get` says).
    """
    shown_env = shown(environment)
    if not kept:
        # One sentence per check: a sentence for all of them would say they failed.
        first = " ".join(
            _first_sentence([result], result.table_name, set_back=False) for result in refused
        )
        table = shown(refused[0].table_name)
        return f"{first} {_KEPT_NOT_CHECKED.format(table=table, run=run_id, environment=shown_env)}"
    sentences = [
        _first_sentence(kept, kept[0].table_name, set_back=False),
        *(_first_sentence([result], result.table_name, set_back=False) for result in refused),
    ]
    if dropped:
        return " ".join((*sentences, _KEPT_DROPPED))
    sentences += [
        *rows_sentences(kept, run_id, environment),
        _KEPT.format(run=run_id, environment=shown_env),
    ]
    return " ".join(sentences)


def refused_source_message(
    failed: Sequence[CheckResult], *, table: str, refusals: Sequence[str]
) -> str:
    """Build the message of a check task of another project's table that was refused that table.

    Parameters
    ----------
    failed
        The results of the table's checks that failed without being refused; may be empty.
    table
        The checked table's dotted name.
    refusals
        The texts of the refusals (K4), from the catalog: one for each kept table that a
        check's count was refused; at least one.

    Returns
    -------
    str
        Which checks failed, as for any failing check, then each refusal; the refusals alone
        when no check failed on its own.
    """
    refused = " ".join(refusals)
    if not failed:
        return refused
    return f"{_first_sentence(failed, table, set_back=False)} {refused}"


def kept_no_checks_error(
    *,
    table: str,
    kept_run_id: uuid.UUID,
    environment: str,
    unchecked: bool = False,
    unknown: bool = False,
) -> str:
    """Word why the check task of a kept table that has no check left cannot pass.

    Parameters
    ----------
    table
        The kept table's dotted name.
    kept_run_id
        The run the table's note names.
    environment
        The environment's name.
    unchecked
        Whether the note names a check that could not run, not one that failed.
    unknown
        Whether the note says the table was set back to data whose checks are not known.

    Returns
    -------
    str
        `ERROR_KEPT_NO_CHECKS` (`ERROR_KEPT_NO_CHECKS_UNCHECKED` when `unchecked`,
        `ERROR_KEPT_NO_CHECKS_UNKNOWN` when `unknown`) with the names escaped: the `error` of the
        task's one result, and the task's whole message.
    """
    if unknown:
        template = ERROR_KEPT_NO_CHECKS_UNKNOWN
    else:
        template = ERROR_KEPT_NO_CHECKS_UNCHECKED if unchecked else ERROR_KEPT_NO_CHECKS
    return template.format(table=shown(table), kept_run=kept_run_id, environment=shown(environment))


def kept_check_message(
    failing: Sequence[CheckResult],
    *,
    table: str,
    run_id: uuid.UUID,
    environment: str,
    kept_run_id: uuid.UUID | None,
    without_checks: bool,
    refused: Sequence[CheckResult] = (),
    unchecked: bool = False,
    unknown: bool = False,
) -> str:
    """Build the message of the check task of a kept table that a later run could not clear (K3).

    Parameters
    ----------
    failing
        The results of the table's checks that failed or could not run (blocking or `keep`; the
        task writes nothing either way), without those in `refused`. Empty only when `refused`
        is not.
    table
        The kept table's dotted name.
    run_id
        This run, for the next step when the note is gone or no check could run.
    environment
        The environment's name.
    kept_run_id
        The run the table's note names, or `None` when the note is gone by now (a rebuild or a
        passing check cleared it while this task ran).
    without_checks
        Whether the project version has no check for the table, so that only a rebuild can clear the
        note. `failing` is then the one stand-in result.
    refused
        The results of the same table's checks whose count was refused a kept table of another
        project (`ERROR_KEPT`): they could not run, so they say nothing about this table.
    unchecked
        Whether the table's note names a check that could not run, not one that failed.
    unknown
        Whether the table's note says it was set back to data whose checks are not known.

    Returns
    -------
    str
        For a table without checks that carries the note: `kept_no_checks_error`, alone.
        Otherwise which checks failed, as for any failing check, then one sentence per refused
        check, then that the table was kept and still fails and what to do. When no check
        failed and every one was refused, the end says that the checks could not run instead:
        nothing is known to fail still. Without a note the end is the general next step,
        without the sentence that says the table was kept, which would be false.
    """
    shown_table, shown_env = shown(table), shown(environment)
    if without_checks and kept_run_id is not None:
        return kept_no_checks_error(
            table=table,
            kept_run_id=kept_run_id,
            environment=environment,
            unchecked=unchecked,
            unknown=unknown,
        )
    sentences = [_first_sentence(failing, table, set_back=False)] if failing else []
    sentences += [_first_sentence([result], table, set_back=False) for result in refused]
    if not failing:
        rest = _KEPT_NOT_CHECKED.format(table=shown_table, run=run_id, environment=shown_env)
    elif kept_run_id is None:
        rest = _AFTER_A_FIX.format(run=run_id, environment=shown_env)
    else:
        if unknown:
            again = _KEPT_AGAIN_UNKNOWN
        else:
            again = _KEPT_AGAIN_UNCHECKED if unchecked else _KEPT_AGAIN
        rest = again.format(table=shown_table, kept_run=kept_run_id, environment=shown_env)
    return " ".join((*sentences, *rows_sentences(failing, run_id, environment), rest))


def _with_stop(text: str | None) -> str:
    """End a failure text with a full stop, so that a sentence can follow it.

    Parameters
    ----------
    text
        The failure text, if any.

    Returns
    -------
    str
        The text, ``the task failed`` for none, ending in ``.``, ``!`` or ``?``.
    """
    base = text or "the task failed"
    return base if base.endswith((".", "!", "?")) else f"{base}."


def build_failure_message(
    failure: str | None,
    *,
    table: str,
    environment: str,
    rollback: Rollback,
    refused: bool = False,
) -> str:
    """Build the message of a task whose build failed for good after it had written.

    A Python asset can commit and then fail. When that failure ends the task, the worker sets
    the table back to the task run's baseline; the message says what the failure was and what
    became of the table, with the sentences of a failed check.

    Parameters
    ----------
    failure
        The build's own failure text.
    table
        The task's table.
    environment
        The environment's name.
    rollback
        What the rollback did.
    refused
        Whether the catalog refused the rollback.

    Returns
    -------
    str
        The failure, then the sentence about the table.
    """
    second = _second_sentence(table, environment, rollback, refused, "that was not checked")
    return f"{_with_stop(failure)} {second}"


def run_stop_message(run_error_message: str | None) -> str:
    """Say why a task ended because its run was asked to stop.

    A run failed by a code integrity check carries the check's message in its own
    `error_message` and has the cancel flag set; a cancel by a user sets only the flag. When a
    user cancelled and a code integrity check failed afterwards, the integrity text wins: the
    later cause is the stronger one and is worth showing.

    Parameters
    ----------
    run_error_message
        The run's `error_message`.

    Returns
    -------
    str
        `RUN_STOPPED_MESSAGE` when the run has an error message, otherwise
        `RUN_CANCELLED_MESSAGE`.
    """
    return RUN_CANCELLED_MESSAGE if run_error_message is None else RUN_STOPPED_MESSAGE


def set_back_ending(reason: str, sentence: str) -> str:
    """Build the message of a task whose table a set-back claim dealt with.

    Parameters
    ----------
    reason
        Why the task ended: the message of a lost worker, or of a cancel.
    sentence
        What became of the table: `SET_BACK_DONE`, `SET_BACK_NOTHING` or `SET_BACK_CHANGED`.

    Returns
    -------
    str
        The reason, then the sentence.
    """
    return f"{_with_stop(reason)} {sentence}"


def set_back_result_message(
    reason: str, rollback: Rollback, refused: bool, *, table: str, environment: str
) -> str:
    """Build the message of a task ended by a claim that only set its table back.

    Parameters
    ----------
    reason
        Why the task ended: the message of a lost worker, or of a cancel.
    rollback
        What the rollback did.
    refused
        Whether the catalog refused it.
    table
        The task's table.
    environment
        The environment's name.

    Returns
    -------
    str
        The reason, then what became of the table: set back, nothing to set back, changed
        since (or dropped), not known whether the rollback landed, or could not be set back.
    """
    # Imported here: the catalog imports this module.
    from lakana_core.catalog.service import ROLLBACK_NOT_KNOWN_REASON

    outcome = rollback.outcome
    if refused or (
        outcome is RollbackOutcome.FAILED and rollback.reason is not ROLLBACK_NOT_KNOWN_REASON
    ):
        return set_back_failed_message(reason)
    special = _special_sentence(table, environment, rollback)
    if special is not None:
        return set_back_ending(reason, special)
    if outcome is RollbackOutcome.CHANGED_SINCE:
        return set_back_ending(reason, SET_BACK_CHANGED)
    if outcome in {RollbackOutcome.ROLLED_BACK, RollbackOutcome.UNPUBLISHED}:
        return set_back_ending(reason, SET_BACK_DONE)
    return set_back_ending(reason, SET_BACK_NOTHING)


def set_back_failed_message(failure: str | None) -> str:
    """Build the message of a failed build whose table could not be set back to its baseline.

    Parameters
    ----------
    failure
        The build's own failure text.

    Returns
    -------
    str
        The failure, then that the table may hold unchecked data of this run.
    """
    return f"{_with_stop(failure)} {_NOT_SET_BACK_AT_ALL}"


def unchecked_retry_message(failure: str | None, run_id: uuid.UUID, environment: str) -> str:
    """Build the message of an attempt that could not run the checks of a written task at all.

    Such a task is tried again without a bound (it cannot run out of attempts), so the message
    says what the table holds meanwhile and how to end it.

    Parameters
    ----------
    failure
        Why the checks could not be run.
    run_id
        The run, for the next step.
    environment
        The environment's name.

    Returns
    -------
    str
        The failure, then the state of the table and the command that stops the run.
    """
    stop = _STILL_UNCHECKED.format(run=run_id, environment=shown(environment))
    return f"{_with_stop(failure)} {stop}"


def warning_note(warned: Sequence[CheckResult]) -> str:
    """Build the message of a task that succeeded although `warn` checks failed (text M5).

    Parameters
    ----------
    warned
        The `warn` results that failed or could not run (`Verdict.warned`); at least one.

    Returns
    -------
    str
        The note, with the checks' short names.
    """
    return _WARNED.format(
        count=len(warned),
        checks="check" if len(warned) == 1 else "checks",
        listing=_listing(warned),
    )


def record_result(
    session: Session,
    *,
    task_run_id: uuid.UUID,
    worker: str,
    claim: int,
    spec: CheckSpec,
    outcome: CheckOutcome,
    failing_rows: int | None,
    error: str | None,
    table_id: uuid.UUID | None,
    checked_snapshot_id: int | None,
    policy_applied: bool,
    query_run_id: uuid.UUID | None,
    started_at: datetime,
) -> bool:
    """Record one check's result for the claim that ran it.

    The task run is read ``FOR SHARE``, the lock a write takes, so a reclaim of the task waits
    for this transaction or is seen by it. A failed or errored `block` or `keep` check is audited as
    ``CHECK_FAILED`` by the run's principal. A second call for the same check replaces the
    result.

    Parameters
    ----------
    session
        Database session; commits.
    task_run_id
        The task run the check belongs to.
    worker
        The worker that claims to hold the task.
    claim
        The claim it claims to hold.
    spec
        The check.
    outcome
        How it ended.
    failing_rows
        The count of rows that are wrong, when the check ran.
    error
        Why it could not run, redacted.
    table_id
        The checked table's id, if resolved.
    checked_snapshot_id
        The snapshot the check read.
    policy_applied
        Whether a row filter or column mask of the run's principal applied.
    query_run_id
        The query that ran the check.
    started_at
        When the check started.

    Returns
    -------
    bool
        `False` when the claim is not held: nothing is recorded.
    """
    held = session.execute(
        select(TaskRun.run_id, Run.environment_id, Run.run_as_principal_id, Environment.name)
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
    if held is None:
        session.rollback()
        return False
    run_id, environment_id, principal_id, environment = held
    session.execute(
        delete(CheckResult).where(
            CheckResult.task_run_id == task_run_id, CheckResult.check_name == spec.name
        )
    )
    session.add(
        CheckResult(
            id=uuid.uuid4(),
            run_id=run_id,
            task_run_id=task_run_id,
            environment_id=environment_id,
            check_name=spec.name,
            check_path=spec.path,
            table_name=spec.table,
            table_id=table_id,
            on_failure=spec.on_failure,
            outcome=outcome,
            failing_rows=failing_rows,
            error=error,
            checked_snapshot_id=checked_snapshot_id,
            rollback_outcome=None,
            policy_applied=policy_applied,
            query_run_id=query_run_id,
            started_at=started_at,
            finished_at=db_now(session),
        )
    )
    if spec.on_failure != "warn" and outcome in (CheckOutcome.FAILED, CheckOutcome.ERRORED):
        ref = TableRef.from_dotted(environment, spec.table)
        AuditService(session).record(
            principal_id,
            AuditEventType.CHECK_FAILED,
            securable=Securable.table_scope(environment, ref.namespace, ref.name),
            detail={
                "run_id": str(run_id),
                "task_run_id": str(task_run_id),
                "table": spec.table,
                "check": spec.name,
                "path": spec.path,
                "outcome": outcome.value,
                "rows": failing_rows,
            },
        )
    session.commit()
    return True


def mark_rollback_failed(
    session: Session,
    *,
    task_run_id: uuid.UUID,
    worker: str,
    claim: int,
    outcome: RollbackOutcome = RollbackOutcome.FAILED,
) -> bool:
    """Note on a task's failed blocking results that their rollback failed, for the claim.

    For a rollback the catalog refused before it recorded anything, and, with `outcome`
    `NOTHING_WRITTEN`, for a run that wrote nothing, so there was nothing to set back. The
    task run is read ``FOR SHARE`` as `record_result` reads it. An outcome that is already
    there is the catalog's own record and is left alone.

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
    outcome
        What to note: `FAILED`, or `NOTHING_WRITTEN` for a run that wrote nothing.

    Returns
    -------
    bool
        `False` when the claim is not held: nothing is written.
    """
    held = session.execute(
        select(TaskRun.id)
        .where(
            TaskRun.id == task_run_id,
            TaskRun.status == TaskStatus.RUNNING,
            TaskRun.lease_owner == worker,
            TaskRun.claim_count == claim,
        )
        .with_for_update(read=True)
    ).first()
    if held is None:
        session.rollback()
        return False
    session.execute(
        update(CheckResult)
        .where(
            CheckResult.task_run_id == task_run_id,
            CheckResult.on_failure == "block",
            CheckResult.outcome.in_([CheckOutcome.FAILED, CheckOutcome.ERRORED]),
            CheckResult.rollback_outcome.is_(None),
        )
        .values(rollback_outcome=outcome)
        .execution_options(synchronize_session=False)
    )
    session.commit()
    return True


def results_of_task(session: Session, task_run_id: uuid.UUID) -> list[CheckResult]:
    """Return the results recorded for one task run, by check name.

    Parameters
    ----------
    session
        Database session.
    task_run_id
        The task run.

    Returns
    -------
    list of CheckResult
        Its results.
    """
    return list(
        session.scalars(
            select(CheckResult)
            .where(CheckResult.task_run_id == task_run_id)
            .order_by(CheckResult.check_name)
            .execution_options(populate_existing=True)
        )
    )


def not_published_message(
    session: Session, dotted: str, table_id: uuid.UUID, run_id: uuid.UUID, environment: str
) -> str:
    """Say why a table cannot be read, with the checks its first build failed, if any.

    Only the blocking checks of that run for that table that failed or could not run are named,
    sorted, at most three; more are counted. For each check that read a snapshot, at most three
    of them and whether or not it is among the three named, a sentence names the command that
    shows its rows (`rows_sentences`, as in the messages of a task). When there is no such
    result (the run was deleted; or the first build itself failed and the worker set the table
    back, so no check ever ran), the text says that the build did not finish or failed a check,
    and names none.

    Parameters
    ----------
    session
        Database session; read only.
    dotted
        The table's dotted name as the caller used it.
    table_id
        The table's id.
    run_id
        The run whose checks failed, `Table.unpublished_run_id`.
    environment
        The environment's name, for the command that shows the rows.

    Returns
    -------
    str
        The text. It carries the table's name, check names and a run id, and no storage
        location, no row, and no value a check computed.
    """
    found = sorted(
        session.scalars(
            select(CheckResult)
            .where(
                CheckResult.run_id == run_id,
                CheckResult.table_id == table_id,
                CheckResult.on_failure == "block",
                CheckResult.outcome.in_([CheckOutcome.FAILED, CheckOutcome.ERRORED]),
            )
            .execution_options(populate_existing=True)
        ),
        key=lambda result: result.check_name,
    )
    names = [result.check_name for result in found]
    short = [f"`{shown(name.rsplit('/', 1)[-1])}`" for name in names]
    if not short:
        # No check to name: the run's results are gone, or the build itself failed and was
        # set back, so there never were any. One text that is true for both.
        return (
            f"`{shown(dotted)}` has not been published: its first build did not finish, or "
            f"failed a check (run {run_id}). It is published by the next run that builds it and "
            "passes its checks."
        )
    if len(short) == 1:
        failed = f"the check {short[0]}"
    elif len(short) <= 3:
        failed = f"the checks {', '.join(short[:-1])} and {short[-1]}"
    else:
        failed = f"the checks {', '.join(short[:3])} and {len(short) - 3} more"
    rows = "".join(f"{sentence} " for sentence in rows_sentences(found, run_id, environment))
    return (
        f"`{shown(dotted)}` has not been published: its first build failed {failed} "
        f"(run {run_id}). {rows}It is published by the next run in which its checks pass."
    )


CHECKED_SNAPSHOT_GONE = (
    "The rows of this check can no longer be read: the snapshot it checked is no longer in "
    "`{table}`. Maintenance removes a failed snapshot after 7 days, and a table that is dropped "
    "and built again starts anew. The number of rows the check found remains: "
    "`lakana check list --run {run} --env {environment}`."
)
"""Why the rows a check found cannot be shown any more, and where their number remains.

The text of `CheckedSnapshotGoneError` as the catalog raises it. The 7 days are
`lakana_core.catalog.service.FAILED_SNAPSHOT_KEEP_DAYS`, which this module cannot import; a
test holds the two together.
"""

CHECK_RESULT_GONE = (
    "The rows of this check cannot be read from `{table}`: the check result is not one of this "
    "table as it exists now, or was removed with its run."
)
"""The text of `CheckResultGoneError`: the named table has no such check result.

One text for a result that does not exist, one of another table, one without a table, and one
of a table that was since dropped and built again under the name. It names only the table the
caller named, and no run and no check: a caller who may read this table and holds the id of
another table's result must learn nothing of that result, not even whether it exists.
"""

CHECK_ROWS_NOT_FOR_A_TASK = (
    "A task cannot read the rows a check found: they are shown to a person's own query, never "
    "to a pipeline. Run `lakana check rows` yourself, outside the task."
)
"""The refusal of a principal with a run context that asks for the rows of a check.

A blocking check keeps failing rows from everything downstream; a task that could read them
here would get them after all. It names no table, run or check: it is raised before the check
result is looked up.
"""

CHECK_ROWS_NOT_EVERY_TABLE = (
    "You may not read every table this check reads, so its rows and its SQL are not shown to "
    "you. Ask an administrator for READ on the tables the check reads, or run the check's file "
    "from the project repository yourself."
)
"""The refusal of a caller who asks for the rows of a check whose SQL names a table they may
not read.

The query would hand the caller the check's SQL, and its rows can come from any table it
reads. It names none of those tables. It leaves out how many there are as well, so that the
text is the same for every check; the number is no secret, `project-version show` gives it to
whoever may read the environment. It is raised before any reason derived from the SQL.
"""

NO_CHECKED_SNAPSHOT = "This check has no rows to show from run {run}: "
"""The start of every text of `NoCheckedSnapshotError`; a reason follows."""

_NOTHING_CHECKED = "no snapshot of the table was checked."
"""The reason the catalog gives: all the stored result says, without the check's definition."""

_CHECKED_A_VIEW = (
    "`{table}` is a view, so no snapshot was checked. Run the check's SQL yourself: `{path}` in "
    "the project."
)
"""The reason and next step for a check of a view: a view has no snapshots."""

_DID_NOT_READ_ITS_TABLE = (
    "its SQL does not read `{table}`, so no snapshot of it was checked. Run the check's SQL "
    "yourself: `{path}` in the project."
)
"""The reason and next step for a check whose SQL never reads the table it belongs to."""

_COULD_NOT_RUN = (
    "the check could not run. Its error is in `lakana check list --run {run} --env {environment}`."
)
"""The reason and next step for a check that ended with an error and counted nothing."""


def checked_snapshot_gone_message(table: str, run_id: uuid.UUID, environment: str) -> str:
    """Word why the rows of a check cannot be shown any more, and where its count remains.

    Parameters
    ----------
    table
        The checked table's dotted name, as the caller addressed it.
    run_id
        The run the check result belongs to.
    environment
        The environment's name.

    Returns
    -------
    str
        `CHECKED_SNAPSHOT_GONE` with the names escaped.
    """
    return CHECKED_SNAPSHOT_GONE.format(
        table=shown(table), run=run_id, environment=shown(environment)
    )


def check_result_gone_message(table: str) -> str:
    """Word that the named table has no such check result.

    Parameters
    ----------
    table
        The table's dotted name, as the caller addressed it.

    Returns
    -------
    str
        `CHECK_RESULT_GONE` with the name escaped.
    """
    return CHECK_RESULT_GONE.format(table=shown(table))


def no_checked_snapshot_message(run_id: uuid.UUID) -> str:
    """Word that a check result names no snapshot, without a reason.

    Parameters
    ----------
    run_id
        The run the check result belongs to.

    Returns
    -------
    str
        The text the catalog raises; a caller that knows the check words a reason with one
        of the three functions below.
    """
    return NO_CHECKED_SNAPSHOT.format(run=run_id) + _NOTHING_CHECKED


def no_snapshot_of_a_view_message(run_id: uuid.UUID, table: str, path: str) -> str:
    """Word why a check of a view has no rows to show.

    Parameters
    ----------
    run_id
        The run the check result belongs to.
    table
        The view's dotted name.
    path
        The path of the check's SQL file in the project.

    Returns
    -------
    str
        The text, with the names escaped.
    """
    return NO_CHECKED_SNAPSHOT.format(run=run_id) + _CHECKED_A_VIEW.format(
        table=shown(table), path=shown(path)
    )


def no_snapshot_not_read_message(run_id: uuid.UUID, table: str, path: str) -> str:
    """Word why a check whose SQL does not read its own table has no rows to show.

    Parameters
    ----------
    run_id
        The run the check result belongs to.
    table
        The dotted name of the table the check belongs to.
    path
        The path of the check's SQL file in the project.

    Returns
    -------
    str
        The text, with the names escaped.
    """
    return NO_CHECKED_SNAPSHOT.format(run=run_id) + _DID_NOT_READ_ITS_TABLE.format(
        table=shown(table), path=shown(path)
    )


def no_snapshot_could_not_run_message(run_id: uuid.UUID, environment: str) -> str:
    """Word why a check that could not run has no rows to show.

    Parameters
    ----------
    run_id
        The run the check result belongs to.
    environment
        The environment's name.

    Returns
    -------
    str
        The text, with the name escaped.
    """
    return NO_CHECKED_SNAPSHOT.format(run=run_id) + _COULD_NOT_RUN.format(
        run=run_id, environment=shown(environment)
    )


CHECK_NOT_IN_RUN = (
    "Run {run} has no result for the check `{check}`. Its checks: "
    "`lakana check list --run {run} --env {environment}`."
)
"""The text of `CheckNotInRunError`: the run a caller may read has no result of that name.

Said only to a caller who was allowed to read the run, and only about that run's own results:
a check that another run has a result for is answered the same way.
"""

CHECK_SQL_GONE = (
    "The project version of run {run} is no longer stored, so the check's SQL is not known. "
    "The number of rows remains: `lakana check list --run {run} --env {environment}`."
)
"""The text of `CheckSqlGoneError`: the run's project version holds no SQL for the check.

A check result stores a count and never the SQL; the SQL is read from the manifest of the
project version the run used.
"""

CHECK_RESULT_REMOVED = (
    "The check result this query reads is no longer there: it was recorded again by a new "
    "attempt of its task, or removed with its run. Run `lakana check rows` again."
)
"""Why a query that shows the rows of a check fails when its check result no longer exists.

The worker's own answer, given before any table is looked at, so it names none. Without it
the query would read every table as it is now and show rows the check never saw. Two things
remove a result: a task attempt that is claimed again records its results anew, under new
ids, and a run that is deleted takes its results along. The worker cannot tell which, and the
query run stores neither the run nor the check's name, so the command is named without its
arguments: run again, it finds the new result, or says that the run has none.
"""

COLUMN_NOT_SHOWN = (
    "The column `{column}` of the checked snapshot is not shown: `{table}` does not have it in "
    "this form any more (it was added by the run that failed, or dropped or changed since), so "
    "the rows this check found cannot be listed with it. The number of rows the check found "
    "remains: `lakana check list --run {run} --env {environment}`."
)
"""Why a query that shows the rows of a check cannot use a column of the checked snapshot.

The read at a checked snapshot shows only the columns that the table has now under the same
name, type and field id (the id tells a column from another one that was dropped and added
again, or renamed into its name): a column mask can only be set on a column the table has
now, so any other column of an older or failed snapshot could not be masked for anybody. A
check whose SQL names such a column fails with this text instead of the database's "column
not found", unless another table of the query has a column of that name: the database's
message then stands, because it may be about that table.
"""


def check_not_in_run_message(run_id: uuid.UUID, check: str, environment: str) -> str:
    """Word that a run has no result for a check, and where its checks are listed.

    Parameters
    ----------
    run_id
        The run the caller named and may read.
    check
        The check's name, as the caller sent it.
    environment
        The environment's name.

    Returns
    -------
    str
        `CHECK_NOT_IN_RUN` with the names escaped.
    """
    return CHECK_NOT_IN_RUN.format(run=run_id, check=shown(check), environment=shown(environment))


CHECK_SQL_UNREADABLE = (
    "The SQL of this check stored with run {run} cannot be read, so its rows cannot be shown. "
    "Deploy the project again (`lakana deploy --env {environment}`) and run it; the number of "
    "rows remains: `lakana check list --run {run} --env {environment}`."
)
"""The text of `CheckSqlGoneError` for SQL whose tables cannot be listed.

A deploy only stores SQL whose tables it could list, so such SQL was changed after the deploy
or was read by another version of the parser. Nobody can be checked against it, so it is never
queued; the text names nothing from it.
"""


def check_sql_gone_message(run_id: uuid.UUID, environment: str) -> str:
    """Word that a check's SQL is no longer stored, and where its count remains.

    Parameters
    ----------
    run_id
        The run the check result belongs to.
    environment
        The environment's name.

    Returns
    -------
    str
        `CHECK_SQL_GONE` with the name escaped.
    """
    return CHECK_SQL_GONE.format(run=run_id, environment=shown(environment))


def check_sql_unreadable_message(run_id: uuid.UUID, environment: str) -> str:
    """Word that a check's stored SQL cannot be read, and what to do about it.

    Parameters
    ----------
    run_id
        The run the check result belongs to.
    environment
        The environment's name.

    Returns
    -------
    str
        `CHECK_SQL_UNREADABLE` with the name escaped.
    """
    return CHECK_SQL_UNREADABLE.format(run=run_id, environment=shown(environment))


def column_not_shown_message(column: str, table: str, run_id: uuid.UUID, environment: str) -> str:
    """Word that a column of the checked snapshot is not shown, and where the count remains.

    Parameters
    ----------
    column
        The column's name, as the checked snapshot's schema spells it.
    table
        The checked table's dotted name, as the query addressed it.
    run_id
        The run the check result belongs to.
    environment
        The environment's name.

    Returns
    -------
    str
        `COLUMN_NOT_SHOWN` with the names escaped.
    """
    return COLUMN_NOT_SHOWN.format(
        column=shown(column), table=shown(table), run=run_id, environment=shown(environment)
    )


NO_COLUMN_LEFT = (
    "None of the columns of the checked snapshot is in `{table}` in the same form any more, so "
    "the rows this check found cannot be listed. The number of rows the check found remains: "
    "`lakana check list --run {run} --env {environment}`."
)
"""Why the rows of a check cannot be listed when the table was rebuilt with other columns.

The read at a checked snapshot shows only the columns the table has now (`COLUMN_NOT_SHOWN`);
when that leaves none there is nothing to list, whatever the check's SQL names.
"""

POLICY_NEEDS_A_COLUMN = (
    "A row filter or column mask of yours on `{table}` uses the column `{column}`, which is "
    "not the same column in the checked snapshot and in the table now (it was added, dropped "
    "or renamed since, or another column took its name), so the rows this check found cannot "
    "be shown under it. The number of rows the check found remains: "
    "`lakana check list --run {run} --env {environment}`."
)
"""Why the rows of a check are not shown to a reader whose policy cannot be applied to them.

A row filter and the expression of a column mask are written for the table as it is now and
name its columns. When a column they read is not the same column in the checked snapshot (it
was added, dropped, dropped and added again, or two columns were renamed into each other since),
the snapshot's rows would be judged by other data than the policy means, so nothing is shown.
A policy is never left out to make the read work.
"""


def no_column_left_message(table: str, run_id: uuid.UUID, environment: str) -> str:
    """Word that the table has none of the checked snapshot's columns any more.

    Parameters
    ----------
    table
        The checked table's dotted name, as the query addressed it.
    run_id
        The run the check result belongs to.
    environment
        The environment's name.

    Returns
    -------
    str
        `NO_COLUMN_LEFT` with the names escaped.
    """
    return NO_COLUMN_LEFT.format(table=shown(table), run=run_id, environment=shown(environment))


def policy_needs_a_column_message(
    column: str, table: str, run_id: uuid.UUID, environment: str
) -> str:
    """Word that a reader's row filter or column mask cannot be applied to the checked snapshot.

    Parameters
    ----------
    column
        The column the policy reads, spelled as in the policy.
    table
        The checked table's dotted name, as the query addressed it.
    run_id
        The run the check result belongs to.
    environment
        The environment's name.

    Returns
    -------
    str
        `POLICY_NEEDS_A_COLUMN` with the names escaped.
    """
    return POLICY_NEEDS_A_COLUMN.format(
        column=shown(column), table=shown(table), run=run_id, environment=shown(environment)
    )
