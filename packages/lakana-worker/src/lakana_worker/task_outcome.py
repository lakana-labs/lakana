"""How a task attempt ended: the outcome to record, and what a failure means.

Holds `TaskOutcome` (an attempt, ready to be recorded), `TaskFailure` (a preparation that
failed, with its kind and a safe message), `classify` (an exception to what happens next),
`failure_message` and the two generic messages. The run engine, the Python runtime and the
check phase all end an attempt this way, so none of them has to import the others.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, NamedTuple

from sqlalchemy.orm import Session

from lakana_core.catalog.schema_compat import EVOLVE_ADVICE
from lakana_core.errors import ConflictError, LakanaError, PermissionDenied
from lakana_core.kept_mark import KeptSeen
from lakana_core.models import QueryErrorCause, WriteMode
from lakana_core.runs.failures import FailureCode
from lakana_core.runs.lockdown import WriteFenceRefused
from lakana_core.runs.tasks import FailureKind, Written
from lakana_core.sql_syntax import SQL_SYNTAX_ERRORS
from lakana_worker.executor import QueryCancelled, QueryTimeout, TransientQueryError
from lakana_worker.pipeline import failure_cause

INTERNAL_ERROR_MESSAGE = "internal error while preparing the task; see the worker log"


RUN_ERROR_MESSAGE = "internal error while running the task; see the worker log"
"""An unexpected worker error in a Python attempt, before or after its process ran."""


_FAILED_MESSAGE = "the task failed"
"""Shown when a failed write run stored no error text."""


NO_RUN_MESSAGE = "the run or its project version no longer exists"


NO_PRINCIPAL_MESSAGE = "the run principal is deactivated or deleted"


def classify(exc: BaseException | None) -> FailureKind:
    """Map the exception that failed an attempt to what happens next.

    Parameters
    ----------
    exc
        `run_one`'s return value, or a preparation error; `None` for a run
        refused before execution.

    Returns
    -------
    FailureKind
        Cancelled, timeout, transient, conflict or permanent. A write refused by the write
        fence (its run was ended because its worker was lost) is transient: it is not the
        task's fault, and running it again is what the sweep's message tells the user.
    """
    if exc is None:
        return FailureKind.PERMANENT
    if isinstance(exc, QueryCancelled):
        return FailureKind.CANCELLED
    if isinstance(exc, QueryTimeout):
        return FailureKind.TIMEOUT
    if isinstance(exc, (TransientQueryError, WriteFenceRefused)):
        return FailureKind.TRANSIENT
    if isinstance(exc, ConflictError):
        return FailureKind.CONFLICT
    if isinstance(exc, (LakanaError, *SQL_SYNTAX_ERRORS)):
        return FailureKind.PERMANENT
    return FailureKind.TRANSIENT


_CAUSE_CODES: Mapping[QueryErrorCause, FailureCode] = {
    QueryErrorCause.NOT_FOUND: FailureCode.NOT_FOUND,
    QueryErrorCause.PERMISSION_DENIED: FailureCode.PERMISSION_DENIED,
    QueryErrorCause.RESULT_TOO_LARGE: FailureCode.QUERY_INVALID,
    QueryErrorCause.OUT_OF_MEMORY: FailureCode.MEMORY,
    QueryErrorCause.INVALID_QUERY: FailureCode.QUERY_INVALID,
    QueryErrorCause.TIMEOUT: FailureCode.TIMEOUT,
    QueryErrorCause.CANCELLED: FailureCode.CANCELLED,
    QueryErrorCause.INTERNAL: FailureCode.INTERNAL,
    QueryErrorCause.WORKER_LOST: FailureCode.WORKER_LOST,
}
"""The task failure code of each cause a query run can store."""


def failure_code_of(exc: BaseException | None, cause: str | None = None) -> FailureCode:
    """Map what failed a query to the task failure code.

    The exception is classified as the query run's own cause is (`pipeline.failure_cause`), with a
    write conflict as the one addition: a query has no code for it, a task does.

    Parameters
    ----------
    exc
        The exception `run_one` returned or a preparation raised; `None` when a query run failed
        without one.
    cause
        The `QueryRun.error_cause` stored on the failed run, used only without `exc`.

    Returns
    -------
    FailureCode
        The code; `INTERNAL` for an exception nobody classifies, and for a missing or unknown
        `cause`.
    """
    if isinstance(exc, ConflictError):
        return FailureCode.CONFLICT
    if exc is not None:
        return _CAUSE_CODES[failure_cause(exc)]
    try:
        return _CAUSE_CODES[QueryErrorCause(cause)]
    except ValueError:
        return FailureCode.INTERNAL


def stop_code(kind: FailureKind) -> FailureCode | None:
    """Give the code of a stop that `cancel_kind` reported.

    Parameters
    ----------
    kind
        ``CANCELLED`` or ``RELEASED``, as `cancel_kind` returns.

    Returns
    -------
    FailureCode or None
        ``cancelled`` for a cancel; `None` for a release, which is no failure.
    """
    return FailureCode.CANCELLED if kind is FailureKind.CANCELLED else None


class TaskFailure(Exception):
    """A failure while preparing a task, with its kind, a safe message and its code.

    Parameters
    ----------
    kind
        What happens next.
    message
        The text for `error_message` and the log.
    code
        Why it failed; `None` only for a release (`stop_code`).
    in_transaction
        Work to land in the same transaction as the attempt's record (see
        `TaskOutcome.in_transaction`).
    """

    def __init__(
        self,
        kind: FailureKind,
        message: str,
        code: FailureCode | None,
        in_transaction: Callable[[Session], None] | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.code = code
        self.in_transaction = in_transaction


def failure_message(failure: BaseException | None, text: str | None) -> str:
    """Build the safe error text of a failed attempt.

    Only `text` is ever shown: `failure` contributes its type, never its
    message, because the exception `run_one` returns is unredacted.

    Parameters
    ----------
    failure
        The exception, if any (for its type only).
    text
        The redacted `QueryRun.error_message`, if any.

    Returns
    -------
    str
        `text` (or a generic fallback), prefixed with "permission denied: "
        for an authorization refusal that does not already say so.
    """
    base = text or _FAILED_MESSAGE
    if isinstance(failure, PermissionDenied) and not base.lower().startswith("permission denied"):
        return f"permission denied: {base}"
    return base


def sql_asset_mismatch_message(text: str, table: str, mode: WriteMode) -> str:
    """Give a SQL asset's schema mismatch the next step that asset can take.

    The server's text ends with `EVOLVE_ADVICE`, the step of a caller of the API or the CLI. A
    SQL asset that replaces its table cannot set `evolve_schema` (its front matter allows it
    with an append only); one that appends sets it in its front matter.

    Parameters
    ----------
    text
        The stored, redacted error of the write run.
    table
        The asset's dotted table name.
    mode
        The write run's mode.

    Returns
    -------
    str
        The text up to the advice, then the asset's own step; `text` unchanged when the advice
        is not where it is expected.
    """
    if not text.endswith(EVOLVE_ADVICE):
        return text
    detail = text[: -len(EVOLVE_ADVICE)].rstrip()
    if mode is WriteMode.APPEND:
        return (
            f"{detail} To let {table} take the new schema, add `evolve_schema: true` to the "
            "asset's front matter and deploy again (a column the result no longer has is then "
            "removed from the table); otherwise make its SELECT return the columns and types "
            f"{table} has."
        )
    return (
        f"{detail} A SQL asset that replaces its table cannot drop, rename or retype the "
        f"table's columns: make its SELECT return the columns and types {table} has (list them "
        "instead of `*`), then run it again."
    )


class KeptTable(NamedTuple):
    """A table that kept data that failed a check, and the note it carried when a build began.

    Parameters
    ----------
    table_id
        The table's id.
    seen
        The note as read before the build's write: the one the rebuild supersedes. A note that
        another run sets after this is about data the rebuild did not replace.
    """

    table_id: uuid.UUID
    seen: KeptSeen


@dataclass(frozen=True)
class TaskOutcome:
    """How an attempt ended, ready to be recorded.

    Parameters
    ----------
    kind
        `None` for success, else how it failed.
    message
        Safe error text.
    environment
        Environment name for the log, or `None` when unknown.
    mode
        The write mode used.
    watermark
        The incremental watermark used.
    rows
        Rows written. For a Python attempt this is the task's own report: a
        display value, never what decided that the attempt succeeded.
    output
        Redacted, sanitised process output of a Python attempt, for the log.
    snapshot_id
        The snapshot a Python attempt left its table at, as the worker read it.
    artifact_version
        The version of its artifact an artifact attempt committed, as the worker
        found it on the server (this task run, this claim).
    written
        What a successful Python table attempt did to its table: from the task run's
        baseline (or, without checks, what the worker read before the process) to what it read
        after it; the check phase stamps the task run with it. `None` for a SQL task, whose
        write's own commit stamps the task run.
    checks
        The log lines of the attempt's checks and of its rollback, if it had a check phase.
    kept_table
        The table a successful attempt built, with the note it carried when the attempt began
        (`KeptTable`), when that table kept data that failed a check; `None` otherwise. A SQL task
        sets it whether or not it has checks, a Python task only without checks. It is acted on
        only for a task without checks: the build supersedes the data and `_run_claimed` clears the
        note. A task with checks goes through the check phase, which clears the note when the
        checks pass.
    built_snapshot_id
        The snapshot a SQL task's write made, set with `kept_table` (a table that had a note):
        the note is cleared with it, as the snapshot the rebuild's data is.
    in_transaction
        Work that must land in the same transaction as the attempt's record, or not at all
        (`record_attempt`): a failed code integrity check fails the run with it, so a reclaim
        never finds the run failed and the attempt open, or the reverse.
    failure_code
        Why the attempt failed, for a failure; `None` for a success and a release. A failure
        without one is recorded as ``internal``.
    wrote_nothing
        A successful build that committed nothing (an append or merge whose source had nothing
        new, a merge that changed no row): a failed check then has nothing of this run to set
        back, which is not the same as a write whose record is missing.
    check_state
        What the check phase built `message` from besides the check results
        (`CheckMessageState.to_json`); `None` for a message the check phase did not build. It is
        stored with the message, so that the message can be built again for a reader from whom a
        result is withheld.
    """

    kind: FailureKind | None
    message: str | None
    environment: str | None
    mode: WriteMode | None = None
    watermark: str | None = None
    rows: int | None = None
    output: str | None = None
    snapshot_id: int | None = None
    artifact_version: int | None = None
    written: Written | None = None
    checks: tuple[str, ...] = ()
    kept_table: KeptTable | None = None
    built_snapshot_id: int | None = None
    in_transaction: Callable[[Session], None] | None = None
    failure_code: FailureCode | None = None
    wrote_nothing: bool = False
    check_state: dict[str, Any] | None = None
