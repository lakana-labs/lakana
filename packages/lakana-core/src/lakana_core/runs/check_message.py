"""The message a task's check phase ends with, from its results and the state the worker saw.

The worker words a task's message from two things: the task's check results, and facts that the
results do not hold, such as what the rollback did, whether the table was dropped meanwhile, what a
kept table's note says, and which checks a kept table refused. `CheckMessageState` holds those
facts. The worker stores it beside the message (`TaskRun.check_message_state`, and the same on each
attempt), and `check_message` is the one function that builds the message from both. The worker
calls it with every result. `message_for_reader` calls it with only the results a reader may see,
so the reader gets the message the worker would have written had the withheld results not
existed.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from lakana_core.identifiers import shown
from lakana_core.models import CheckResult, RollbackOutcome
from lakana_core.runs.checks import (
    WITHHELD_TASK_MESSAGE,
    failure_message,
    kept_check_message,
    kept_failure_message,
    log_withheld_message,
    refused_source_message,
    verdict,
    warning_note,
)
from lakana_core.runs.hidden_checks import is_reference_check

if TYPE_CHECKING:
    from lakana_core.catalog.service import Rollback

PASSED = "passed"
"""No blocking or `keep` check failed: the message is the `warn` note, or the build's own."""

BLOCKING = "blocking"
"""A blocking check of a table the task built failed, and the worker set the table back."""

KEPT = "kept"
"""Only `keep` checks of a table the task built failed: the table keeps the run's data."""

SOURCE = "source"
"""A check of a ``source_check`` task failed: the task wrote nothing."""

BRANCHES = (PASSED, BLOCKING, KEPT, SOURCE)


class UndeterminedMessage(Exception):
    """The stored state does not cover the message these results would need."""


@dataclass(frozen=True)
class CheckMessageState:
    """What a task's check message was built from, besides its results.

    Parameters
    ----------
    branch
        Which kind of message it is: `PASSED`, `BLOCKING`, `KEPT` or `SOURCE`.
    source
        Whether the task is a ``source_check`` task.
    success_message
        For `PASSED`: the build's own message, shown when no `warn` check failed.
    rollback_outcome
        For `BLOCKING`: what the rollback did (a `RollbackOutcome` value).
    rollback_reason
        For `BLOCKING`: why, when the rollback says.
    rollback_refused
        For `BLOCKING`: whether the catalog refused the rollback.
    dropped
        For `KEPT`: whether the table was dropped while the checks ran.
    refusals
        For `KEPT` and `SOURCE`: the checks a kept table refused, each with the refusal's text.
    is_asset
        For `SOURCE`: whether the project builds the checked table (the check task of its own
        kept table).
    kept_run_id
        For `SOURCE`: the run the table's note names, when the project builds the table and the
        note was still there.
    unchecked
        For `SOURCE`: whether the note names a check that could not run.
    unknown
        For `SOURCE`: whether the note says the table was set back to data whose checks are not
        known.
    without_checks
        For `SOURCE`: whether the project version has no check for the table.
    """

    branch: str
    source: bool = False
    success_message: str | None = None
    rollback_outcome: str | None = None
    rollback_reason: str | None = None
    rollback_refused: bool = False
    dropped: bool = False
    refusals: Mapping[str, str] = field(default_factory=dict)
    is_asset: bool = False
    kept_run_id: str | None = None
    unchecked: bool = False
    unknown: bool = False
    without_checks: bool = False

    def to_json(self) -> dict[str, Any]:
        """Turn the state into what the database stores.

        Returns
        -------
        dict
            Every field, with `refusals` as a plain object.
        """
        return {
            "branch": self.branch,
            "source": self.source,
            "success_message": self.success_message,
            "rollback_outcome": self.rollback_outcome,
            "rollback_reason": self.rollback_reason,
            "rollback_refused": self.rollback_refused,
            "dropped": self.dropped,
            "refusals": dict(self.refusals),
            "is_asset": self.is_asset,
            "kept_run_id": self.kept_run_id,
            "unchecked": self.unchecked,
            "unknown": self.unknown,
            "without_checks": self.without_checks,
        }

    @classmethod
    def from_json(cls, data: object) -> CheckMessageState | None:
        """Read a stored state.

        Parameters
        ----------
        data
            What `to_json` stored, or anything else.

        Returns
        -------
        CheckMessageState or None
            The state; `None` for nothing stored, or for anything this release cannot read.
        """
        if not isinstance(data, dict) or data.get("branch") not in BRANCHES:
            return None
        try:
            refusals = data.get("refusals") or {}
            state = cls(
                branch=data["branch"],
                source=data.get("source") is True,
                success_message=_text(data.get("success_message")),
                rollback_outcome=_text(data.get("rollback_outcome")),
                rollback_reason=_text(data.get("rollback_reason")),
                rollback_refused=data.get("rollback_refused") is True,
                dropped=data.get("dropped") is True,
                refusals={str(k): str(v) for k, v in dict(refusals).items()},
                is_asset=data.get("is_asset") is True,
                kept_run_id=_text(data.get("kept_run_id")),
                unchecked=data.get("unchecked") is True,
                unknown=data.get("unknown") is True,
                without_checks=data.get("without_checks") is True,
            )
            if state.rollback_outcome is not None:
                RollbackOutcome(state.rollback_outcome)
            if state.kept_run_id is not None:
                uuid.UUID(state.kept_run_id)
        except (KeyError, TypeError, ValueError):
            return None
        return state


def _text(value: object) -> str | None:
    """Read an optional text of a stored state.

    Parameters
    ----------
    value
        The stored value.

    Returns
    -------
    str or None
        The text, or `None`.

    Raises
    ------
    TypeError
        If the value is neither a text nor null.
    """
    if value is None or isinstance(value, str):
        return value
    raise TypeError("not a text")


def branch_of(results: Sequence[CheckResult], *, source: bool) -> str:
    """Say which kind of message a task's results lead to, as the worker decides it.

    Parameters
    ----------
    results
        The task's results.
    source
        Whether the task is a ``source_check`` task.

    Returns
    -------
    str
        `SOURCE` for a failed check of a source task, `BLOCKING` for a failed blocking check,
        `KEPT` when only `keep` checks failed, else `PASSED`.
    """
    found = verdict(results)
    if not found.blocking and not found.kept:
        return PASSED
    if source:
        return SOURCE
    return BLOCKING if found.blocking else KEPT


def _rollback(state: CheckMessageState) -> Rollback:
    """Rebuild the rollback a `BLOCKING` message was built with.

    Parameters
    ----------
    state
        The stored state.

    Returns
    -------
    Rollback
        The rollback; the catalog's "not known" reason as the very object the message compares.

    Raises
    ------
    UndeterminedMessage
        If the state holds no rollback.
    """
    # Imported here: the catalog imports `lakana_core.runs.checks`, which this module needs.
    from lakana_core.catalog.service import ROLLBACK_NOT_KNOWN_REASON
    from lakana_core.catalog.service import Rollback as CatalogRollback

    if state.rollback_outcome is None:
        raise UndeterminedMessage
    reason = state.rollback_reason
    if reason == ROLLBACK_NOT_KNOWN_REASON:
        reason = ROLLBACK_NOT_KNOWN_REASON
    return CatalogRollback(RollbackOutcome(state.rollback_outcome), reason)


def check_message(
    results: Sequence[CheckResult],
    state: CheckMessageState,
    *,
    table: str,
    run_id: uuid.UUID,
    environment: str,
) -> str | None:
    """Build a task's check message from its results and the stored state.

    Parameters
    ----------
    results
        The results to word the message from.
    state
        What the worker saw besides the results.
    table
        The task's table.
    run_id
        The run.
    environment
        The environment's name.

    Returns
    -------
    str or None
        The message; `None` for a passed task whose build had none.

    Raises
    ------
    UndeterminedMessage
        If the results lead to another kind of message than the state was stored for.
    """
    if branch_of(results, source=state.source) != state.branch:
        raise UndeterminedMessage
    found = verdict(results)
    if state.branch == PASSED:
        return warning_note(found.warned) if found.warned else state.success_message
    if state.branch == BLOCKING:
        return failure_message(
            found.blocking,
            table=table,
            run_id=run_id,
            environment=environment,
            rollback=_rollback(state),
            refused=state.rollback_refused,
        )
    if state.branch == KEPT:
        return kept_failure_message(
            kept=[r for r in found.kept if r.check_name not in state.refusals],
            run_id=run_id,
            environment=environment,
            refused=[r for r in found.kept if r.check_name in state.refusals],
            dropped=state.dropped,
        )
    return source_message(
        [*found.blocking, *found.kept], state, table=table, run_id=run_id, environment=environment
    )


def source_message(
    failing: Sequence[CheckResult],
    state: CheckMessageState,
    *,
    table: str,
    run_id: uuid.UUID,
    environment: str,
) -> str:
    """Build the message of a ``source_check`` task whose checks failed.

    A task of a table the project does not build says so. A task of a table the project builds
    is the check task of a kept table: it says what the note says, and of a check whose count was
    refused another project's kept table that it could not run, never that the table "still
    fails". A task of a table the project does not build whose count was refused a kept table
    says that, with the refusal's own text.

    Parameters
    ----------
    failing
        The failed results of the table's checks, blocking or `keep`.
    state
        The stored state (`refusals` and the note's facts).
    table
        The checked table.
    run_id
        The run.
    environment
        The environment's name.

    Returns
    -------
    str
        The message.
    """
    refusals = state.refusals
    if not state.is_asset:
        names = sorted(result.check_name for result in failing if result.check_name in refusals)
        if names:
            return refused_source_message(
                [result for result in failing if result.check_name not in refusals],
                table=table,
                refusals=list(dict.fromkeys(refusals[name] for name in names)),
            )
        return failure_message(
            failing, table=table, run_id=run_id, environment=environment, rollback=None
        )
    kept_run = None if state.kept_run_id is None else uuid.UUID(state.kept_run_id)
    return kept_check_message(
        [result for result in failing if result.check_name not in refusals],
        table=table,
        run_id=run_id,
        environment=environment,
        kept_run_id=kept_run,
        without_checks=state.without_checks,
        refused=[result for result in failing if result.check_name in refusals],
        unchecked=state.unchecked,
        unknown=state.unknown,
    )


def _names_withheld(text: str, hidden: set[str]) -> bool:
    """Say whether a text names a withheld check, by its full name or its short name in quotes.

    Parameters
    ----------
    text
        The text.
    hidden
        The full names of the withheld checks.

    Returns
    -------
    bool
        Whether any of them is named.
    """
    return any(
        shown(name) in text or f"`{shown(name.rpartition('/')[2])}`" in text for name in hidden
    )


def message_for_reader(
    message: str | None,
    stored_state: object,
    results: Sequence[CheckResult],
    hidden: set[str],
    *,
    table: str,
    run_id: uuid.UUID,
    environment: str,
    withheld: str = WITHHELD_TASK_MESSAGE,
) -> str | None:
    """Show a task's message to a reader from whom some of its check results are withheld.

    With a stored state that gives back the stored text from all the results, the message is
    built again by `check_message` from the results the reader may see, so that no name, row
    count, count of checks or "and N more" of a withheld result is in it. The reader gets
    `WITHHELD_TASK_MESSAGE` instead when the state does not give back the stored text, when the
    visible results lead to another kind of message (the withheld check was the only one that
    failed, say), and when nothing was stored and the text names a withheld check. A text without
    a state that names none (a build's failure, a stop) is shown as it is.

    Parameters
    ----------
    message
        The stored message.
    stored_state
        The stored `CheckMessageState`, as JSON, or `None`.
    results
        Every result of the task, the withheld ones included.
    hidden
        The full names of the withheld results.
    table
        The task's table.
    run_id
        The run.
    environment
        The environment's name.
    withheld
        What the reader gets when the message cannot be built again without the withheld
        results; `WITHHELD_TASK_MESSAGE` by default.

    Returns
    -------
    str or None
        The message as the reader may see it.
    """
    if message is None or not hidden:
        return message
    state = CheckMessageState.from_json(stored_state)
    if state is None:
        return withheld if _names_withheld(message, hidden) else message
    where = {"table": table, "run_id": run_id, "environment": environment}
    try:
        # The state must give back the stored text from every result: a text that a later
        # claim, the scheduler or a set-back wrote over it, or an earlier attempt's text whose
        # results have been counted again since, is not rebuilt.
        if check_message(results, state, **where) != message:
            return withheld
        visible = [r for r in results if r.check_name not in hidden]
        return check_message(visible, state, **where)
    except UndeterminedMessage:
        return withheld


def message_for_log(
    message: str | None,
    stored_state: object,
    results: Sequence[CheckResult],
    *,
    table: str,
    run_id: uuid.UUID,
    environment: str,
) -> str | None:
    """Word a task's message for its free-text log, which every reader of the run reads alike.

    The log cannot be filtered per reader (it is read by byte offsets), so it leaves out every
    generated reference check, as `message_for_reader` does for a reader who may read none of
    their targets. Hand-written and key checks are worded as in the task's message.

    Parameters
    ----------
    message
        The task's message.
    stored_state
        Its `CheckMessageState`, as JSON, or `None`.
    results
        Every result of the task.
    table
        The task's table.
    run_id
        The run.
    environment
        The environment's name.

    Returns
    -------
    str or None
        The message without any reference check's name or row count; `log_withheld_message`
        when it cannot be worded without them.
    """
    hidden = {r.check_name for r in results if is_reference_check(r.check_name, r.generated)}
    return message_for_reader(
        message,
        stored_state,
        results,
        hidden,
        table=table,
        run_id=run_id,
        environment=environment,
        withheld=log_withheld_message(run_id, environment),
    )
