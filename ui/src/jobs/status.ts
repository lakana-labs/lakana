/**
 * What a run's and a task's state looks like: its glyph, whether it is over, and its failure code.
 *
 * @packageDocumentation
 */
import { ACTIVE_RUN_STATUSES, type RunOut, type RunStatus, type TaskStatus } from "../api/jobs";
import type { StatusKind } from "../status/status";

/**
 * The glyph of a run's state; the kinds are named like the states.
 *
 * @param status - The run's status.
 * @returns The glyph kind.
 */
export function runStatusKind(status: RunStatus): StatusKind {
  return status;
}

/**
 * The glyph of a task's state: waiting tasks look queued, and the rest are named like their
 * states.
 *
 * @param status - The task's status.
 * @returns The glyph kind.
 */
export function taskStatusKind(status: TaskStatus): StatusKind {
  if (status === "pending" || status === "ready") {
    return "queued";
  }
  return status === "upstream_failed" ? "upstream-failed" : status;
}

/**
 * Whether a run is not over: the page keeps reading it.
 *
 * @param run - The run.
 * @param run.status - Its status.
 * @returns True while it is queued or running.
 */
export function isActive(run: Pick<RunOut, "status">): boolean {
  return ACTIVE_RUN_STATUSES.includes(run.status);
}

/** The failure codes the reference documents (`FailureOut`). */
export const FAILURE_CODES: readonly string[] = [
  "build_failed",
  "code_changed",
  "secret_refused",
  "no_runtime",
  "upstream_failed",
  "upstream_missing",
  "check_blocked",
  "check_error",
  "checks_unfinished",
  "query_invalid",
  "permission_denied",
  "not_found",
  "memory",
  "timeout",
  "cancelled",
  "worker_lost",
  "conflict",
  "internal",
];

/**
 * The code to show for a failure: a documented one as it is; `unknown` (old rows) and any code a
 * newer server sends as `internal`, as the reference says.
 *
 * @param code - The code the server sent.
 * @returns A documented code.
 */
export function failureCode(code: string): string {
  return FAILURE_CODES.includes(code) ? code : "internal";
}
