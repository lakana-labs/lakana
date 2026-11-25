/**
 * Which deploys a person may roll a repository back to.
 *
 * @packageDocumentation
 */
import type { DeployOut } from "../api/jobs";

/** A deploy a repository can be rolled back to: one with the commit it deployed. */
export type RollbackTarget = DeployOut & { readonly resolved_sha: string };

/**
 * Whether Roll back to this… stands beside a deploy: the server lists `rollback` for the reader
 * (`DEPLOY` in the environment, never a CI token), it deployed or rolled back to a commit, it
 * succeeded with a known commit and version, and that version is not the repository's current
 * one while the repository has one.
 *
 * @param deploy - The deploy.
 * @param current - Each deployed repository's current project version.
 * @returns True when the rollback is offered.
 */
export function mayRollBackTo(
  deploy: DeployOut,
  current: ReadonlyMap<string, string>,
): deploy is RollbackTarget {
  const now = current.get(deploy.repository);
  return (
    deploy.actions.includes("rollback") &&
    (deploy.kind === "deploy" || deploy.kind === "rollback") &&
    deploy.status === "succeeded" &&
    deploy.resolved_sha !== null &&
    deploy.project_version_id !== null &&
    now !== undefined &&
    now !== deploy.project_version_id
  );
}

/**
 * The deploys marked current: for each repository, the newest successful deploy or rollback in
 * the list, when it made the repository's current version. One row at most per repository, so
 * after a rollback to an earlier version only the rollback is current, not the deploy that first
 * made that version as well. The newest is read from each deploy's creation time, so a list
 * sorted otherwise (by state, the oldest first) marks the same row.
 *
 * @param deploys - The listed deploys, in any order.
 * @param current - Each deployed repository's current project version.
 * @returns The ids of the deploys marked current.
 */
export function currentDeploys(
  deploys: readonly DeployOut[],
  current: ReadonlyMap<string, string>,
): ReadonlySet<string> {
  const marked = new Set<string>();
  const seen = new Set<string>();
  // Stable: deploys of one time keep the list's order, which the server breaks by id.
  const newestFirst = [...deploys].sort((a, b) => b.created_at.localeCompare(a.created_at));
  for (const deploy of newestFirst) {
    if (
      seen.has(deploy.repository) ||
      deploy.kind === "dry_run" ||
      deploy.status !== "succeeded" ||
      deploy.project_version_id === null
    ) {
      continue;
    }
    seen.add(deploy.repository);
    if (current.get(deploy.repository) === deploy.project_version_id) {
      marked.add(deploy.id);
    }
  }
  return marked;
}
