/**
 * The addresses of the Jobs screen: the list, the Runs and Deploys tabs, a job, a run, and a task
 * of a run.
 *
 * @packageDocumentation
 */
import type { AllRunsFilter } from "../api/jobs";

/**
 * The address of the Jobs list, filtered to one environment or showing all of them.
 *
 * @param environment - The environment; null for all.
 * @returns The path, the environment encoded.
 */
export function jobsListPath(environment: string | null): string {
  return environment === null
    ? "/jobs"
    : `/jobs?${new URLSearchParams({ environment }).toString()}`;
}

/**
 * The address of the Runs tab, filtered to one environment and to some states.
 *
 * @param filter - The environment (null or absent for all) and the states (absent or empty for
 *   all).
 * @returns The path, the filter encoded.
 */
export function runsTabPath(
  filter: Partial<Pick<AllRunsFilter, "environment" | "status">>,
): string {
  const query = new URLSearchParams();
  if (filter.environment !== undefined && filter.environment !== null) {
    query.set("environment", filter.environment);
  }
  if (filter.status !== undefined && filter.status.length > 0) {
    query.set("status", filter.status.join(","));
  }
  const search = query.toString();
  return search === "" ? "/jobs/runs" : `/jobs/runs?${search}`;
}

/**
 * The address of the Deploys tab, filtered to one environment and one repository.
 *
 * @param environment - The environment; null for the one the tab chooses.
 * @param repository - The repository; null or absent for all.
 * @returns The path, the filter encoded.
 */
export function deploysPath(environment: string | null, repository: string | null = null): string {
  const query = new URLSearchParams();
  if (environment !== null) {
    query.set("environment", environment);
  }
  if (repository !== null) {
    query.set("repository", repository);
  }
  const search = query.toString();
  return search === "" ? "/jobs/deploys" : `/jobs/deploys?${search}`;
}

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
