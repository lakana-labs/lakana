/**
 * Which jobs a rollback changes: every job of either version, and what becomes of it.
 *
 * @packageDocumentation
 */

/** One job a rollback changes, and how. */
export interface RollbackJob {
  /** The job's name. */
  readonly job: string;
  /** `kept` in both versions, `removed` by the rollback, or `back` with it. */
  readonly change: "kept" | "removed" | "back";
}

/**
 * The union of the current version's jobs and the target version's, each marked, sorted by name.
 *
 * @param current - The jobs the repository defines in the environment now.
 * @param target - The jobs of the version the rollback goes back to.
 * @returns The jobs, each once.
 */
export function rollbackJobs(
  current: readonly string[],
  target: readonly string[],
): readonly RollbackJob[] {
  const now = new Set(current);
  const then = new Set(target);
  return [...new Set([...current, ...target])]
    .sort((a, b) => (a < b ? -1 : a > b ? 1 : 0))
    .map((job) => ({
      job,
      change: now.has(job) ? (then.has(job) ? "kept" : "removed") : "back",
    }));
}
