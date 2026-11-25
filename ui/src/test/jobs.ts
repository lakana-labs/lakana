/**
 * What the server answers the job, run and task pages and the Deploys tab with, for tests: a job
 * `analytics/nightly_sales` with a schedule, a run of it, a failed task, a log piece, a deploy and
 * a repository's current version.
 *
 * @packageDocumentation
 */
import type { EnvironmentOut } from "../api/environments";
import type {
  DeploymentOut,
  DeployOut,
  FailedRunOut,
  FailureOut,
  JobOut,
  RunOut,
  TaskAttemptOut,
  TaskLogOut,
  TaskRunOut,
} from "../api/jobs";

/** The id of the run `aJobRun` answers for. */
export const JOB_RUN_ID = "5d6e7f80-9a1b-4c2d-8e3f-405162738495";

/**
 * The job `nightly_sales` of the repository `analytics` in `prod`, scheduled, not paused, that the
 * reader may run, pause and resume.
 *
 * @param over - The fields that differ.
 * @returns The job.
 */
export function aJob(over: Partial<JobOut> = {}): JobOut {
  return {
    name: "nightly_sales",
    environment: "prod",
    repository: "analytics",
    select: ["silver.*"],
    upstream: true,
    downstream: false,
    cron: "0 2 * * *",
    timezone: "Europe/Berlin",
    state: "scheduled",
    paused: false,
    next_run_at: "2026-11-18T01:00:00Z",
    waiting_for: null,
    waits_for_run_id: null,
    last_run_id: JOB_RUN_ID,
    last_run_status: "succeeded",
    last_run_at: "2026-11-17T01:00:00Z",
    waiting_since: null,
    waiting_trigger: null,
    triggers: [
      {
        kind: "schedule",
        cron: "0 2 * * *",
        timezone: "Europe/Berlin",
        paused: false,
        next_run_at: "2026-11-18T01:00:00Z",
      },
      { kind: "on_demand" },
    ],
    commit_sha: "4f9d2a7c81b3e5f60718293a4b5c6d7e8f901234",
    source_url: "https://github.com/acme/analytics/blob/4f9d2a7/lakana.yaml",
    notify: null,
    actions: ["run", "pause", "resume"],
    ...over,
  };
}

/**
 * A task that succeeded.
 *
 * @param over - The fields that differ.
 * @returns The task.
 */
export function aTask(over: Partial<TaskRunOut> = {}): TaskRunOut {
  return {
    asset: "silver.orders",
    status: "succeeded",
    attempt: 1,
    max_attempts: 1,
    started_at: "2026-11-17T01:00:05Z",
    finished_at: "2026-11-17T01:00:35Z",
    rows_written: 1200,
    waiting_for: null,
    kind: "asset",
    checks: [],
    failure: null,
    ...over,
  };
}

/**
 * A failure with a code and the two sentences the server words for it.
 *
 * @param code - The failure's code.
 * @returns The failure.
 */
export function aFailure(code: string): FailureOut {
  return {
    code,
    cause: `The task failed with ${code}.`,
    next_step: "Fix the cause and resume the run.",
  };
}

/**
 * A run of `nightly_sales` that succeeded, with one task.
 *
 * @param over - The fields that differ.
 * @returns The run.
 */
export function aJobRun(over: Partial<RunOut> = {}): RunOut {
  return {
    id: JOB_RUN_ID,
    environment: "prod",
    repository: "analytics",
    commit_sha: "4f9d2a7c81b3e5f60718293a4b5c6d7e8f901234",
    kind: "full",
    status: "succeeded",
    cancel_requested: false,
    triggered_by: "ana.nowak",
    resumed_from_run_id: null,
    selection: {},
    error_message: null,
    created_at: "2026-11-17T01:00:00Z",
    started_at: "2026-11-17T01:00:02Z",
    finished_at: "2026-11-17T01:00:40Z",
    tasks: [aTask()],
    job: "nightly_sales",
    trigger: "schedule",
    failed_tasks: 0,
    total_tasks: 1,
    ended_tasks: 1,
    actions: [],
    ...over,
  };
}

/**
 * A row of the failed runs: a run of `nightly_sales` in `prod` whose task `silver.orders` failed
 * with `timeout`, which the reader may resume.
 *
 * @param over - The fields that differ.
 * @returns The row.
 */
export function aFailedRun(over: Partial<FailedRunOut> = {}): FailedRunOut {
  return {
    run: aJobRun({
      status: "failed",
      tasks: [],
      failed_tasks: 1,
      total_tasks: 3,
      ended_tasks: 3,
      finished_at: "2026-11-17T01:14:00Z",
      actions: ["cancel", "resume"],
    }),
    task: "silver.orders",
    failure: aFailure("timeout"),
    ...over,
  };
}

/** What `GET /api/v1/environments` answers: `dev`, and `prod`, which is protected. */
export const ENVIRONMENTS: readonly EnvironmentOut[] = [
  {
    name: "dev",
    storage_root: "file:///srv/lakana/dev",
    storage_secret_ref: null,
    is_protected: false,
    properties: {},
    retention_days: null,
    deleted_at: null,
    purge_after: null,
  },
  {
    name: "prod",
    storage_root: "s3://lakana-prod/warehouse",
    storage_secret_ref: "prod-storage",
    is_protected: true,
    properties: {},
    retention_days: 30,
    deleted_at: null,
    purge_after: null,
  },
];

/**
 * One piece of a task's log, complete.
 *
 * @param over - The fields that differ.
 * @returns The piece.
 */
export function aTaskLog(over: Partial<TaskLogOut> = {}): TaskLogOut {
  return {
    text: "building silver.orders\ndone\n",
    attempts: [],
    next_offset: 27,
    more: false,
    complete: true,
    reset: false,
    ...over,
  };
}

/**
 * An attempt of `silver.orders` that succeeded on worker `w-7f3a`, with everything measured.
 *
 * @param over - The fields that differ.
 * @returns The attempt.
 */
export function anAttempt(over: Partial<TaskAttemptOut> = {}): TaskAttemptOut {
  return {
    attempt: 1,
    worker: "w-7f3a",
    started_at: "2026-11-17T01:00:05Z",
    finished_at: "2026-11-17T01:00:35Z",
    outcome: "succeeded",
    rows_written: 1200,
    error_message: null,
    failure: null,
    peak_memory_bytes: 268_435_456,
    cpu_seconds: 12,
    bytes_read: 52_428_800,
    ...over,
  };
}

/**
 * Loads the Jobs screen's chunk ahead of a file's tests (`beforeAll(preloadJobsScreen)`). `/jobs`
 * loads its screen on demand, and the first load of that chunk transforms the whole Jobs area:
 * under coverage on CI that takes longer than a `findBy` waits. Loaded here, a test that opens a
 * Jobs address waits only for the router, and still goes through the route's `lazy`.
 */
export async function preloadJobsScreen(): Promise<void> {
  await import("../jobs/JobsScreen");
}

/** The id of the deploy `aDeploy` answers for. */
export const DEPLOY_ID = "7a8b9c0d-1e2f-4a3b-8c4d-5e6f70819203";

/** The project version `aDeploy` made. */
export const DEPLOY_VERSION = "1b2c3d4e-5f60-4718-9a2b-3c4d5e6f7081";

/**
 * A deploy of `analytics` to `prod` that succeeded at commit `4f9d2a7`, asked for by
 * `ana.nowak`, which the reader may roll back to.
 *
 * @param over - The fields that differ.
 * @returns The deploy.
 */
export function aDeploy(over: Partial<DeployOut> = {}): DeployOut {
  return {
    id: DEPLOY_ID,
    environment: "prod",
    repository: "analytics",
    kind: "deploy",
    status: "succeeded",
    requested_ref: "main",
    resolved_sha: "4f9d2a7c81b3e5f60718293a4b5c6d7e8f901234",
    commit_subject: null,
    project_version_id: DEPLOY_VERSION,
    error_message: null,
    created_at: "2026-11-16T09:00:00Z",
    started_at: "2026-11-16T09:00:02Z",
    finished_at: "2026-11-16T09:00:40Z",
    submitted_by: "ana.nowak",
    jobs: null,
    actions: ["rollback"],
    ...over,
  };
}

/**
 * The current version of `analytics`: another version than `aDeploy`'s, at commit `9c1e5b3`.
 *
 * @param over - The fields that differ.
 * @returns The deployment.
 */
export function aDeployment(over: Partial<DeploymentOut> = {}): DeploymentOut {
  return {
    repository: "analytics",
    project_version_id: "9c1e5b30-7a2d-4e4f-8a6b-1c2d3e4f5061",
    commit_sha: "9c1e5b3a2d4f6e8a0b1c2d3e4f5a6b7c8d9e0f12",
    updated_at: "2026-11-17T08:00:00Z",
    ...over,
  };
}
