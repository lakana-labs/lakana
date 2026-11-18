/**
 * Which deploys a person may roll a repository back to.
 *
 * @packageDocumentation
 */
import type { DeployOut } from "../api/jobs";

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
export function mayRollBackTo(deploy: DeployOut, current: ReadonlyMap<string, string>): boolean {
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
