"""Stable codes on the log lines worth an alert: `event=<code>`.

A log line an operator may want an alert for carries one of these codes, so that an alert rule
matches the code and not the wording, which may change. A call site passes `extra=log_event(...)`:

    logger.error("Memory limit exceeded: ...", total, extra=log_event(LogEvent.MEMORY_STOP))

The record then has the attribute `event` (for a handler that writes structured records), and
the formatter of `lakana_core.log_setup` ends the line's message with ` event=memory_stop`.

The codes are part of the interface: a code is never renamed or reused for another meaning, only
added or, with an upgrade note, removed. Each one is in the reference table of the
documentation (`docs/site/reference/log-events.md`), with the process that writes it
(`WHERE`); a test compares the two.
"""

from __future__ import annotations

from enum import StrEnum


class LogEvent(StrEnum):
    """The codes, by the name a call site uses."""

    MEMORY_STOP = "memory_stop"
    MEMORY_TASK_ENDED = "memory_task_ended"
    DATABASE_UNREACHABLE = "database_unreachable"
    HEARTBEAT_MISSED = "heartbeat_missed"
    HEARTBEAT_RECOVERED = "heartbeat_recovered"
    WORKER_LOST = "worker_lost"
    LEASE_LOST = "lease_lost"
    LEASE_RENEWAL_FAILED = "lease_renewal_failed"
    STORAGE_UNREACHABLE = "storage_unreachable"
    STORAGE_REFUSED = "storage_refused"
    READINESS_FAILED = "readiness_failed"
    READINESS_RECOVERED = "readiness_recovered"
    SCHEDULER_ROLE_TAKEN = "scheduler_role_taken"
    SCHEDULER_ROLE_LOST = "scheduler_role_lost"
    SCHEDULER_STEP_FAILED = "scheduler_step_failed"
    CHECKS_UNFINISHED = "checks_unfinished"
    SWEEP_FAILED = "sweep_failed"
    SWEEP_HUNG = "sweep_hung"
    POLL_FAILED = "poll_failed"
    INTERNAL_ERROR = "internal_error"
    PACKAGE_CACHE_TRIMMED = "package_cache_trimmed"
    PACKAGE_CACHE_OVER_LIMIT = "package_cache_over_limit"


WHERE: dict[LogEvent, str] = {
    LogEvent.MEMORY_STOP: "worker",
    LogEvent.MEMORY_TASK_ENDED: "worker",
    LogEvent.DATABASE_UNREACHABLE: "worker",
    LogEvent.HEARTBEAT_MISSED: "worker",
    LogEvent.HEARTBEAT_RECOVERED: "worker",
    LogEvent.WORKER_LOST: "worker",
    LogEvent.LEASE_LOST: "worker",
    LogEvent.LEASE_RENEWAL_FAILED: "worker",
    LogEvent.STORAGE_UNREACHABLE: "server, worker",
    LogEvent.STORAGE_REFUSED: "server",
    LogEvent.READINESS_FAILED: "server",
    LogEvent.READINESS_RECOVERED: "server",
    LogEvent.SCHEDULER_ROLE_TAKEN: "worker",
    LogEvent.SCHEDULER_ROLE_LOST: "worker",
    LogEvent.SCHEDULER_STEP_FAILED: "worker",
    LogEvent.CHECKS_UNFINISHED: "worker",
    LogEvent.SWEEP_FAILED: "worker",
    LogEvent.SWEEP_HUNG: "worker",
    LogEvent.POLL_FAILED: "worker",
    LogEvent.INTERNAL_ERROR: "worker",
    LogEvent.PACKAGE_CACHE_TRIMMED: "worker",
    LogEvent.PACKAGE_CACHE_OVER_LIMIT: "worker",
}
"""The process whose log has each code, as the reference table says it."""


def log_event(code: LogEvent) -> dict[str, str]:
    """Build the `extra` of a log call that carries a code.

    Parameters
    ----------
    code
        The code.

    Returns
    -------
    dict of str to str
        `{"event": code}`, for `logger.warning(..., extra=log_event(code))`.
    """
    return {"event": code.value}
