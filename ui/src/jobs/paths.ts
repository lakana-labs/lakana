/**
 * The addresses of the Jobs screen: a job, a run, and a task of a run.
 *
 * @packageDocumentation
 */

/**
 * The address of a job's page; `?run={id}` on it selects one of its runs.
 *
 * @param environment - The job's environment.
 * @param repository - The job's repository, which may hold slashes.
 * @param job - The job's name.
 * @returns The path, each part encoded.
 */
export function jobPath(environment: string, repository: string, job: string): string {
  return `/jobs/${encodeURIComponent(environment)}/job/${encodeURIComponent(repository)}/${encodeURIComponent(job)}`;
}

/**
 * The address of a run's page.
 *
 * @param environment - The run's environment.
 * @param runId - The run's id.
 * @returns The path, each part encoded.
 */
export function jobRunPath(environment: string, runId: string): string {
  return `/jobs/${encodeURIComponent(environment)}/runs/${encodeURIComponent(runId)}`;
}

/**
 * The address of a task's page: its attempts and its log.
 *
 * @param environment - The run's environment.
 * @param runId - The run's id.
 * @param asset - The task's asset.
 * @returns The path, each part encoded.
 */
export function taskPath(environment: string, runId: string, asset: string): string {
  return `${jobRunPath(environment, runId)}/tasks/${encodeURIComponent(asset)}`;
}

/**
 * Splits a job's reference, `repository/job`, at its last slash: a job's name is an identifier,
 * while a repository's name may hold slashes.
 *
 * @param ref - Home's reference to a job.
 * @returns The repository and the job; null for a reference without a slash, or with an empty
 *   repository or job.
 */
export function splitJobRef(ref: string): { repository: string; job: string } | null {
  const slash = ref.lastIndexOf("/");
  const repository = ref.slice(0, Math.max(slash, 0));
  const job = ref.slice(slash + 1);
  return slash < 0 || repository === "" || job === "" ? null : { repository, job };
}
