"""Why a task failed: a stable code, a short cause and the next step.

A task (and each of its attempts) that ends badly stores one `FailureCode` next to the free
text of `error_message`. The REST API, the CLI and the SDK show the code with the cause and
the next step from `FAILURES`, and keep the free text as the detail.

The codes are stable and documented (``docs/site/reference/why-did-it-fail.md``, which a test
compares with `FAILURES`); the sentences may be reworded, as the wait sentences are. A row
written before the codes existed has none: it shows as `UNKNOWN`, which is a display value and
is never stored. Codes are per task only; a run has no code of its own.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from lakana_core.models.check_result import CheckOutcome, CheckResult
from lakana_core.models.run import AttemptOutcome, TaskAttempt, TaskRun, TaskStatus


class FailureCode(enum.StrEnum):
    """The reason a task or one of its attempts failed, in a form a program can branch on.

    The value is what is stored in ``failure_code`` and what the REST API returns. A client must
    treat a value it does not know like `INTERNAL`.
    """

    BUILD_FAILED = "build_failed"
    """The task's code, or the Python environment it needs, failed to run."""
    CODE_CHANGED = "code_changed"
    """The deployed code is missing or no longer matches what was deployed."""
    SECRET_REFUSED = "secret_refused"  # nosec B105 -- a failure code, not a credential
    """A package index refused the credential, or has none registered."""
    NO_RUNTIME = "no_runtime"
    """The worker cannot run this kind of task."""
    UPSTREAM_FAILED = "upstream_failed"
    """A task this one reads from did not succeed, so it never ran."""
    UPSTREAM_MISSING = "upstream_missing"
    """A table or artifact this task reads has not been built."""
    CHECK_BLOCKED = "check_blocked"
    """A check of the table failed."""
    CHECK_ERROR = "check_error"
    """A check could not be counted, so the data was not judged."""
    QUERY_INVALID = "query_invalid"
    """The task's SQL is invalid or was refused."""
    PERMISSION_DENIED = "permission_denied"
    """The run's principal may not do what the task needs."""
    NOT_FOUND = "not_found"
    """Something the task needs does not exist."""
    MEMORY = "memory"
    """The task used more memory than the worker allows."""
    TIMEOUT = "timeout"
    """The task ran past its time limit."""
    CANCELLED = "cancelled"
    """The run was cancelled or stopped."""
    WORKER_LOST = "worker_lost"
    """The worker running the task stopped before it finished."""
    CONFLICT = "conflict"
    """Another writer changed the table, or a Python task's table holds no write of its own."""
    INTERNAL = "internal"
    """Anything else: the platform failed in a way it did not expect."""


UNKNOWN = "unknown"
"""What a failed row without a stored code shows. A display value, never stored."""


@dataclass(frozen=True)
class Failure:
    """A failure as the API shows it.

    Parameters
    ----------
    code
        A `FailureCode` value, or ``unknown``.
    cause
        One sentence: what happened.
    next_step
        One sentence: what to do about it.
    """

    code: str
    cause: str
    next_step: str

    def to_json(self) -> dict[str, str]:
        """Build the REST body.

        Returns
        -------
        dict[str, str]
            ``code``, ``cause`` and ``next_step``.
        """
        return {"code": self.code, "cause": self.cause, "next_step": self.next_step}


FAILURES: Mapping[FailureCode, tuple[str, str]] = {
    FailureCode.BUILD_FAILED: (
        "The task's code, or the Python environment it needs, failed.",
        "Read the task's log with 'lakana runs logs RUN_ID --task ASSET', fix the cause and "
        "deploy again.",
    ),
    FailureCode.CODE_CHANGED: (
        "The deployed code is missing, or no longer matches what was deployed.",
        "Deploy the project again; if it keeps happening, ask the operator to check the "
        "code storage.",
    ),
    FailureCode.SECRET_REFUSED: (
        "A package index refused the credential, or has none registered.",
        "Ask an administrator to check the index's credential secret with "
        "'lakana index update NAME --credential-secret SECRET', then run again.",
    ),
    FailureCode.NO_RUNTIME: (
        "This worker cannot run this kind of task.",
        "Ask the operator to run a worker that can (the detail names what is missing), then "
        "run again.",
    ),
    FailureCode.UPSTREAM_FAILED: (
        "A task this one reads from did not succeed, so this one never ran.",
        "Fix the upstream task first, then continue the run with 'lakana runs resume RUN_ID'.",
    ),
    FailureCode.UPSTREAM_MISSING: (
        "A table or artifact this task reads has not been built.",
        "Run the task that builds it first, or run the whole project.",
    ),
    FailureCode.CHECK_BLOCKED: (
        "A check of the table failed, so the data was not published.",
        "Read the failed checks with 'lakana runs show RUN_ID', fix the data or the check, "
        "then run again.",
    ),
    FailureCode.CHECK_ERROR: (
        "A check could not be counted, so the data was not judged.",
        "Read the check's error with 'lakana runs show RUN_ID', fix its SQL or the access it "
        "needs, then run again.",
    ),
    FailureCode.QUERY_INVALID: (
        "The task's SQL is invalid or was refused.",
        "Read the message in the task's log, fix the SQL and deploy again.",
    ),
    FailureCode.PERMISSION_DENIED: (
        "The principal that runs the task may not do what the task needs.",
        "Ask an administrator to grant the access, or to bind another run principal with "
        "'lakana deployments set-run-as'.",
    ),
    FailureCode.NOT_FOUND: (
        "Something the task needs does not exist.",
        "Check the names in the detail; build the missing object or deploy the project again.",
    ),
    FailureCode.MEMORY: (
        "The task used more memory than the worker allows, so it was stopped.",
        "Make the task handle less data at once, or ask the operator to raise the worker's "
        "memory limit.",
    ),
    FailureCode.TIMEOUT: (
        "The task ran longer than its time limit.",
        "Make the task faster, or raise its timeout in the project and deploy again.",
    ),
    FailureCode.CANCELLED: (
        "The run was cancelled or stopped before the task finished.",
        "If a person cancelled it, start the run again when you want it; if another task of the run"
        " failed with `code_changed`, deploy the project again first.",
    ),
    FailureCode.WORKER_LOST: (
        "The worker running the task stopped before it finished.",
        "Run again; if it repeats, ask the operator to check the worker's log and memory limit.",
    ),
    FailureCode.CONFLICT: (
        "Another writer changed the table at the same time, or a Python task's table holds no "
        "write of its own (another writer, a server older than the worker, or maintenance).",
        "Run again; if it repeats, find what else writes this table.",
    ),
    FailureCode.INTERNAL: (
        "The platform failed in a way it did not expect.",
        "Read the task's log; if the cause is not clear, ask the operator for the worker's log "
        "and report it with the run id.",
    ),
}
"""Each code's cause and next step. The sentences may change; the codes may not."""

UNKNOWN_FAILURE = Failure(
    UNKNOWN,
    "This failure was recorded before failure codes existed, so its cause is only in the detail.",
    "Read the detail and the task's log with 'lakana runs logs RUN_ID --task ASSET'; a new "
    "run records a code.",
)
"""What a failed row from before the codes shows."""


def failure_of(code: FailureCode) -> Failure:
    """Build the API's view of a code.

    Parameters
    ----------
    code
        The code.

    Returns
    -------
    Failure
        The code with its cause and next step.
    """
    cause, next_step = FAILURES[code]
    return Failure(code.value, cause, next_step)


def stored_failure(stored: str | None, *, failed: bool) -> Failure | None:
    """Say what a stored code, or the lack of one, shows.

    Parameters
    ----------
    stored
        The stored ``failure_code``; `None` for a row with none.
    failed
        Whether the row is a failure that should have one: a failed task, or an attempt that
        did not succeed. Only used when nothing is stored.

    Returns
    -------
    Failure or None
        The code's failure; `UNKNOWN_FAILURE` for a stored value this release does not know
        (written by a newer one) and for a failed row without one; `None` for a row that
        did not fail.
    """
    if stored is not None:
        try:
            return failure_of(FailureCode(stored))
        except ValueError:
            return UNKNOWN_FAILURE
    return UNKNOWN_FAILURE if failed else None


def check_failure_code(failing: Sequence[CheckResult]) -> FailureCode:
    """Pick the code of a task that failed because of its checks.

    Parameters
    ----------
    failing
        The failing results (`lakana_core.runs.checks.Verdict.blocking` or ``kept``): checks that
        failed or could not run.

    Returns
    -------
    FailureCode
        `CHECK_BLOCKED` when at least one check counted and failed, else `CHECK_ERROR`.
    """
    if any(result.outcome is CheckOutcome.FAILED for result in failing):
        return FailureCode.CHECK_BLOCKED
    return FailureCode.CHECK_ERROR


_BY_STATUS: Mapping[TaskStatus, FailureCode] = {
    TaskStatus.UPSTREAM_FAILED: FailureCode.UPSTREAM_FAILED,
    TaskStatus.CANCELLED: FailureCode.CANCELLED,
}
"""What the status alone says of a task that never ran, for a row without a stored code."""

_NOT_FAILURES = frozenset({AttemptOutcome.SUCCEEDED, AttemptOutcome.RELEASED})
"""The outcomes of an attempt that did not fail."""


def task_failure(task: TaskRun) -> Failure | None:
    """Say why a task failed, for the API.

    Parameters
    ----------
    task
        The task run.

    Returns
    -------
    Failure or None
        `None` for a task that succeeded, and for one that has no error. The stored code when
        there is one. For a row without one (written before the codes existed): the status says
        it exactly for ``upstream_failed`` and ``cancelled``; a ``failed`` task, or one with an
        error text, is ``unknown``.
    """
    if task.status is TaskStatus.SUCCEEDED:
        return None
    if task.failure_code is None and task.status in _BY_STATUS:
        return failure_of(_BY_STATUS[task.status])
    return stored_failure(
        task.failure_code, failed=task.status is TaskStatus.FAILED or task.error_message is not None
    )


def attempt_failure(attempt: TaskAttempt) -> Failure | None:
    """Say why an attempt did not succeed, for the API.

    Parameters
    ----------
    attempt
        The attempt.

    Returns
    -------
    Failure or None
        `None` while it runs, when it succeeded and when it was handed back at a drain. The
        stored code otherwise; ``unknown`` for a row without one.
    """
    if attempt.outcome is None or attempt.outcome in _NOT_FAILURES:
        return None
    return stored_failure(attempt.failure_code, failed=True)
