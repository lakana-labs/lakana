/**
 * Stubs for the end-to-end tests of the Jobs list and the job, run and task pages: the
 * environments `dev` and `prod` (protected); the job `daily_sales` of the repository `analytics`
 * in `prod`, scheduled and notifying Slack through a secret, and the job `orders_hourly` in `dev`,
 * paused; a failed run of seven tasks (one timed out, one never ran because of it, one blocked by
 * a failed check) and a failed run of no job with a run-level message; a running run that ends
 * once cancelled, a running run in `dev` and a queued run of no job; a run of a selection and a
 * resume of the failed run on a second page of every run; a task's log in three pieces; the
 * deploys of `analytics` in `prod` (the current one, an older successful deploy by an account
 * since deleted, a dry run, a failed deploy and an older rollback from before commit subjects were
 * stored) with the current version and
 * the jobs of each version; and what Run now, Pause, Resume schedule, Cancel, Resume and a
 * rollback answer, the rollback running at its first read and succeeded at the second. What a
 * control changes shows in the reads after it.
 *
 * @packageDocumentation
 */
import type { Page, Route } from "@playwright/test";

import type { EnvironmentOut } from "../src/api/environments";
import type {
  DeploymentOut,
  DeployOut,
  FailedRunOut,
  JobOut,
  RunOut,
  TaskAttemptOut,
  TaskLogOut,
  TaskRunOut,
} from "../src/api/jobs";

import { FAILED_RUN_ID, signInAsAdmin, STUBBED } from "./session";

/** The job's environment. */
export const ENV = "prod";

/** The job's repository. */
export const REPOSITORY = "analytics";

/** The job. */
export const JOB = "daily_sales";

/** The failed run, which Home's "Needs your attention" names. */
export const FAILED_ID = FAILED_RUN_ID;

/** The running run, the newest of the job's runs, so the job page selects it. */
export const RUNNING_ID = "1b2c3d4e-5f60-4718-8293-a4b5c6d7e8f9";

/** A job of {@link REPOSITORY} whose start waits for {@link RUNNING_ID} (`stubWaitingJob`). */
export const WAITING_JOB = "hourly_stock";

/** What {@link WAITING_JOB}'s start waits for, as the server says it. */
export const WAITING_MESSAGE = `A start of this job waits for run ${RUNNING_ID} of this project to finish; it starts then.`;

/** The run Run now queues. */
export const STARTED_ID = "2c3d4e5f-6071-4829-93a4-b5c6d7e8f9a0";

/** The run Resume of the failed run queues. */
export const RESUMED_ID = "3d4e5f60-7182-493a-a4b5-c6d7e8f9a0b1";

/** A run the server does not know: it answers 404 with its sentence. */
export const MISSING_ID = "4e5f6071-8293-4a4b-b5c6-d7e8f9a0b1c2";

/** The server's sentence for {@link MISSING_ID}. */
export const MISSING_SENTENCE = `Run ${MISSING_ID} does not exist in ${ENV}.`;

/** The query Show rows queues for the failed check. */
export const ROWS_QUERY_ID = "5f607182-93a4-4b5c-86d7-e8f9a0b1c2d3";

/** The task that timed out: the first failure, so the run's Logs opens it. */
export const TIMED_OUT = "silver.customers";

/** The task a failed check blocked; its table's definition is stubbed by `session.ts`. */
export const BLOCKED = "gold.sales.fact_sales";

/** The failed check of {@link BLOCKED}. */
export const FAILED_CHECK = `${BLOCKED}/unique_sale_id`;

/** The task that never ran because {@link TIMED_OUT} failed. */
const SKIPPED = "gold.sales.daily_sales";

/** The secret that holds the job's webhook. */
export const SECRET = "alerts-webhook";

/** Why the timed-out task failed, as the server words it. */
export const TIMEOUT_CAUSE = "The task ran longer than its limit of 30 minutes.";

/** The three pieces of the timed-out task's log, read one at each poll. */
export const LOG_PIECES = [
  "attempt 2 of 2 on worker w-7f3a\nreading silver.orders\n",
  "joined 1200 customers\nwriting silver.customers\n",
  "stopped: the task ran longer than 30 minutes\n",
] as const;

/** The other environment, which is not protected. */
export const DEV = "dev";

/** The job of {@link DEV}, whose schedule is paused. */
export const DEV_JOB = "orders_hourly";

/** The running run of {@link DEV_JOB}. */
const DEV_RUNNING_ID = "6071829a-a4b5-4c6d-97e8-f9a0b1c2d3e4";

/** The queued run of no job, of the repository {@link REPOSITORY} in `prod`. */
const QUEUED_ID = "718293a4-b5c6-4d7e-88f9-a0b1c2d3e4f5";

/** The failed run of no job, of the repository `finance` in {@link DEV}. */
const ADHOC_FAILED_ID = "8293a4b5-c6d7-4e8f-99a0-b1c2d3e4f5a6";

/** The repository of {@link ADHOC_FAILED_ID}. */
export const ADHOC_REPOSITORY = "finance";

/** Why {@link ADHOC_FAILED_ID} failed, as the server words it: no task of it failed. */
export const ADHOC_MESSAGE = "The project could not be built: lakana.yaml, line 4.";

/** A run of a selection of assets, of no job, on the second page of every run. */
const SELECTION_ID = "93a4b5c6-d7e8-4f9a-8ab1-c2d3e4f5a6b7";

/** A resume of the failed run, on the second page of every run. */
const RESUME_OF_ID = "a4b5c6d7-e8f9-4a0b-9bc2-d3e4f5a6b7c8";

/** The selection's patterns. */
export const SELECTION_PATTERNS = ["silver.*", "gold.sales.*"];

/** The deploy whose version is current in {@link ENV}. */
const CURRENT_DEPLOY_ID = "b5c6d7e8-f9a0-4b1c-8d3e-4f5a6b7c8d9e";

/** An older successful deploy, by an account since deleted: the rollback's target. */
const OLDER_DEPLOY_ID = "c6d7e8f9-a0b1-4c2d-9e4f-5a6b7c8d9eaf";

/** A newer dry run. */
const DRY_RUN_ID = "d7e8f9a0-b1c2-4d3e-8f5a-6b7c8d9eafb0";

/** A failed deploy. */
const FAILED_DEPLOY_ID = "e8f9a0b1-c2d3-4e4f-9a6b-7c8d9eafb0c1";

/** An older successful rollback. */
const OLD_ROLLBACK_ID = "f9a0b1c2-d3e4-4f5a-8b7c-8d9eafb0c1d2";

/** The rollback a test asks for. */
const NEW_ROLLBACK_ID = "0a1b2c3d-4e5f-4a6b-9c8d-9eafb0c1d2e3";

/** The commit of the current version, and of the older deploy. */
const CURRENT_COMMIT = "9c1e5b3a2d4f6e8a0b1c2d3e4f5a6b7c8d9e0f12";

/** The commit of the older deploy, the rollback's target. */
const OLDER_COMMIT = "4f9d2a7c81b3e5f60718293a4b5c6d7e8f901234";

/** Why the failed deploy failed, as its first line. */
export const DEPLOY_FAILURE = "lakana.yaml: the job weekly_report selects nothing.";

/** The job only the older version defines. */
export const OLDER_ONLY_JOB = "weekly_report";

/** The project versions: the current one, the older deploy's and the older rollback's. */
const VERSIONS = {
  current: "1a2b3c4d-5e6f-4071-8293-a4b5c6d7e8f9",
  older: "2b3c4d5e-6f70-4182-93a4-b5c6d7e8f9a0",
  rollback: "3c4d5e6f-7081-4293-a4b5-c6d7e8f9a0b1",
};

/** The path of an environment's part. */
const AT = `/api/v1/environments/${ENV}`;

/** The path of the other environment's part. */
const DEV_AT = `/api/v1/environments/${DEV}`;

/** The environments the person may read. */
const ENVIRONMENTS_PATH = "/api/v1/environments";

/** The runs of every environment. */
const ALL_RUNS = "/api/v1/runs";

/** The failed runs of every environment. */
const FAILED_RUNS = "/api/v1/runs/failed";

/** The path of the job. */
const JOB_PATH = `${AT}/jobs/${JOB}`;

/** The path of the environment's runs. */
const RUNS = `${AT}/runs`;

/** Every run the stubs answer, for the paths below. */
const RUN_IDS = [FAILED_ID, RUNNING_ID, STARTED_ID, RESUMED_ID, MISSING_ID];

/** The assets of the failed run, whose logs the stubs answer. */
const ASSETS = [
  "bronze.orders_raw",
  "silver.orders",
  TIMED_OUT,
  "silver.stores",
  BLOCKED,
  SKIPPED,
  "gold.dim_store",
];

/** Every deploy the stubs answer one by one. */
const DEPLOY_IDS = [
  CURRENT_DEPLOY_ID,
  OLDER_DEPLOY_ID,
  DRY_RUN_ID,
  FAILED_DEPLOY_ID,
  OLD_ROLLBACK_ID,
  NEW_ROLLBACK_ID,
];

/** The paths these stubs answer, beside those of `session.ts`. */
export const JOBS_STUBBED = [
  ...STUBBED,
  ENVIRONMENTS_PATH,
  ALL_RUNS,
  FAILED_RUNS,
  `${AT}/deploys`,
  `${AT}/deployments`,
  ...DEPLOY_IDS.map((id) => `${AT}/deploys/${id}`),
  `${DEV_AT}/deploys`,
  `${DEV_AT}/deployments`,
  `${AT}/jobs`,
  `${DEV_AT}/jobs`,
  `${DEV_AT}/runs/${ADHOC_FAILED_ID}`,
  `${DEV_AT}/runs/${DEV_RUNNING_ID}`,
  JOB_PATH,
  `${AT}/jobs/${WAITING_JOB}`,
  `${JOB_PATH}/runs`,
  `${JOB_PATH}/pause`,
  `${JOB_PATH}/resume`,
  RUNS,
  ...RUN_IDS.flatMap((id) => [
    `${RUNS}/${id}`,
    `${RUNS}/${id}/cancel`,
    `${RUNS}/${id}/resume`,
    `${RUNS}/${id}/checks/rows`,
    ...ASSETS.map((asset) => `${RUNS}/${id}/tasks/${asset}/log`),
  ]),
];

/**
 * The job as `GET …/jobs/daily_sales` answers it.
 *
 * @param paused - Whether its schedule is paused.
 * @returns The job.
 */
function job(paused: boolean): JobOut {
  return {
    name: JOB,
    environment: ENV,
    repository: REPOSITORY,
    select: ["gold.sales.*"],
    upstream: true,
    downstream: false,
    cron: "0 2 * * *",
    timezone: "Europe/Berlin",
    state: paused ? "paused" : "scheduled",
    paused,
    next_run_at: paused ? null : "2026-11-18T01:00:00Z",
    waiting_for: null,
    waits_for_run_id: null,
    last_run_id: RUNNING_ID,
    last_run_status: "running",
    last_run_at: "2026-11-17T09:00:00Z",
    waiting_since: null,
    waiting_trigger: null,
    triggers: [
      {
        kind: "schedule",
        cron: "0 2 * * *",
        timezone: "Europe/Berlin",
        paused,
        next_run_at: paused ? null : "2026-11-18T01:00:00Z",
      },
      { kind: "on_demand" },
    ],
    commit_sha: "4f9d2a7c81b3e5f60718293a4b5c6d7e8f901234",
    source_url: "https://github.com/acme/analytics/blob/4f9d2a7/lakana.yaml",
    notify: {
      format: "slack",
      events: ["failed", "recovered", "columns_changed", "health"],
      secret: SECRET,
    },
    actions: ["run", "pause", "resume"],
  };
}

/**
 * A task of a run.
 *
 * @param asset - Its table.
 * @param over - The fields that differ from one that succeeded.
 * @returns The task.
 */
function task(asset: string, over: Partial<TaskRunOut> = {}): TaskRunOut {
  return {
    asset,
    status: "succeeded",
    attempt: 1,
    max_attempts: 2,
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

/** The seven tasks of the failed run. */
const FAILED_TASKS: readonly TaskRunOut[] = [
  task("bronze.orders_raw", {
    kind: "source_check",
    rows_written: null,
    checks: [
      {
        name: "bronze.orders_raw/not_empty",
        table: "bronze.orders_raw",
        outcome: "passed",
        failing_rows: 0,
      },
    ],
  }),
  task("silver.orders"),
  task(TIMED_OUT, {
    status: "failed",
    attempt: 2,
    finished_at: "2026-11-17T01:31:00Z",
    rows_written: null,
    failure: {
      code: "timeout",
      cause: TIMEOUT_CAUSE,
      next_step: "Raise the task's timeout in lakana.yaml or make it faster, then resume the run.",
    },
  }),
  task("silver.stores", { rows_written: 48 }),
  task(BLOCKED, {
    status: "failed",
    finished_at: "2026-11-17T01:32:00Z",
    rows_written: null,
    checks: [{ name: FAILED_CHECK, table: BLOCKED, outcome: "failed", failing_rows: 3 }],
    failure: {
      code: "check_blocked",
      cause: `The check ${FAILED_CHECK} found 3 rows that are wrong, so the table was not published.`,
      next_step:
        "Look at the rows the check found, fix the data or the check, then resume the run.",
    },
  }),
  task(SKIPPED, {
    status: "upstream_failed",
    attempt: 0,
    started_at: null,
    finished_at: "2026-11-17T01:31:00Z",
    rows_written: null,
    failure: {
      code: "upstream_failed",
      cause: `It reads ${TIMED_OUT}, which failed.`,
      next_step: "Fix the task that failed first, then resume the run.",
    },
  }),
  task("gold.dim_store", { rows_written: 48 }),
];

/**
 * A run of the job.
 *
 * @param id - Its id.
 * @param over - The fields that differ from a queued run with no task.
 * @returns The run.
 */
function run(id: string, over: Partial<RunOut> = {}): RunOut {
  return {
    id,
    environment: ENV,
    repository: REPOSITORY,
    commit_sha: "4f9d2a7c81b3e5f60718293a4b5c6d7e8f901234",
    kind: "full",
    status: "queued",
    cancel_requested: false,
    triggered_by: "admin",
    resumed_from_run_id: null,
    selection: {},
    error_message: null,
    created_at: "2026-11-17T09:30:00Z",
    started_at: null,
    finished_at: null,
    tasks: [
      task("silver.orders", {
        status: "pending",
        attempt: 0,
        started_at: null,
        finished_at: null,
        rows_written: null,
      }),
    ],
    job: JOB,
    trigger: "manual",
    failed_tasks: 0,
    total_tasks: 1,
    ended_tasks: 0,
    actions: ["cancel", "resume"],
    ...over,
  };
}

/** The failed run: started by the schedule, two tasks failed and one never ran. */
const FAILED = run(FAILED_ID, {
  status: "failed",
  triggered_by: null,
  trigger: "schedule",
  created_at: "2026-11-17T01:00:00Z",
  started_at: "2026-11-17T01:00:02Z",
  finished_at: "2026-11-17T01:32:10Z",
  tasks: FAILED_TASKS,
  failed_tasks: 2,
  total_tasks: 7,
  ended_tasks: 7,
});

/**
 * The running run, or the cancelled one it becomes.
 *
 * @param cancelled - Whether its cancel was asked for and it has ended.
 * @returns The run.
 */
function running(cancelled: boolean): RunOut {
  return run(RUNNING_ID, {
    status: cancelled ? "cancelled" : "running",
    cancel_requested: cancelled,
    created_at: "2026-11-17T09:00:00Z",
    started_at: "2026-11-17T09:00:02Z",
    finished_at: cancelled ? "2026-11-17T09:20:00Z" : null,
    tasks: [
      task("silver.orders", {
        status: cancelled ? "cancelled" : "running",
        finished_at: cancelled ? "2026-11-17T09:20:00Z" : null,
        rows_written: null,
      }),
      task(BLOCKED, {
        status: cancelled ? "cancelled" : "pending",
        attempt: 0,
        started_at: null,
        finished_at: null,
        rows_written: null,
        waiting_for: { code: "upstream", message: "Waits for silver.orders to finish." },
      }),
    ],
    total_tasks: 2,
    ended_tasks: cancelled ? 2 : 0,
  });
}

/** The attempts of the timed-out task; the first measured nothing, the second only its use. */
const ATTEMPTS: readonly TaskAttemptOut[] = [1, 2].map((attempt) => ({
  attempt,
  worker: attempt === 1 ? "w-2b9c" : "w-7f3a",
  started_at: attempt === 1 ? "2026-11-17T00:30:00Z" : "2026-11-17T01:00:40Z",
  finished_at: attempt === 1 ? "2026-11-17T01:00:30Z" : "2026-11-17T01:31:00Z",
  outcome: "timed_out",
  rows_written: null,
  error_message: "Timed out after 30 minutes.",
  failure: {
    code: "timeout",
    cause: TIMEOUT_CAUSE,
    next_step: "Raise the task's timeout in lakana.yaml or make it faster, then resume the run.",
  },
  // The first attempt ran on a worker that measures nothing.
  peak_memory_bytes: attempt === 1 ? null : 805_306_368,
  cpu_seconds: attempt === 1 ? null : 1_795,
  bytes_read: attempt === 1 ? null : 52_428_800,
}));

/**
 * The piece of a task's log that starts at an offset: the timed-out task's log is read in three
 * pieces, one at each poll; any other task's log is one line, complete.
 *
 * @param asset - The task.
 * @param offset - The byte to read from.
 * @returns The piece.
 */
function logPiece(asset: string, offset: number): TaskLogOut {
  if (asset !== TIMED_OUT) {
    const text = offset === 0 ? `built ${asset}\n` : "";
    return {
      text,
      attempts: [],
      next_offset: offset + text.length,
      more: false,
      complete: true,
      reset: false,
    };
  }
  let start = 0;
  const index = LOG_PIECES.findIndex((piece) => {
    const here = start === offset;
    start += piece.length;
    return here;
  });
  const text = LOG_PIECES[index] ?? "";
  return {
    text,
    attempts: ATTEMPTS,
    next_offset: offset + text.length,
    more: false,
    complete: index < 0 || index === LOG_PIECES.length - 1,
    reset: false,
  };
}

/**
 * An environment as `GET /api/v1/environments` answers it.
 *
 * @param name - Its name.
 * @param protectedOne - Whether it is protected.
 * @returns The environment.
 */
function environment(name: string, protectedOne: boolean): EnvironmentOut {
  return {
    name,
    storage_root: `s3://lakana-${name}/warehouse`,
    storage_secret_ref: null,
    is_protected: protectedOne,
    properties: {},
    retention_days: null,
    deleted_at: null,
    purge_after: null,
  };
}

/** The job of {@link DEV}: hourly, paused, never run. */
const DEV_JOB_OUT: JobOut = {
  ...job(true),
  name: DEV_JOB,
  environment: DEV,
  cron: "15 * * * *",
  last_run_id: DEV_RUNNING_ID,
  last_run_status: "running",
  last_run_at: "2026-11-17T09:15:00Z",
  triggers: [
    {
      kind: "schedule",
      cron: "15 * * * *",
      timezone: "Europe/Berlin",
      paused: true,
      next_run_at: null,
    },
  ],
  notify: null,
};

/** The running run of {@link DEV_JOB}, which the reader may cancel. */
const DEV_RUNNING = run(DEV_RUNNING_ID, {
  environment: DEV,
  job: DEV_JOB,
  status: "running",
  trigger: "schedule",
  created_at: "2026-11-17T09:15:00Z",
  started_at: "2026-11-17T09:15:02Z",
  total_tasks: 4,
  ended_tasks: 1,
});

/** The queued run of no job. */
const QUEUED = run(QUEUED_ID, { job: null, trigger: null, created_at: "2026-11-17T09:25:00Z" });

/** The failed run of no job, without a failed task. */
const ADHOC_FAILED = run(ADHOC_FAILED_ID, {
  environment: DEV,
  repository: ADHOC_REPOSITORY,
  job: null,
  trigger: null,
  status: "failed",
  error_message: ADHOC_MESSAGE,
  created_at: "2026-11-17T03:00:00Z",
  started_at: "2026-11-17T03:00:01Z",
  finished_at: "2026-11-17T03:00:04Z",
  tasks: [],
  total_tasks: 0,
});

/** A run of a selection of assets, of no job, started by hand: it succeeded. */
const SELECTION = run(SELECTION_ID, {
  job: null,
  trigger: null,
  kind: "selection",
  status: "succeeded",
  selection: { patterns: SELECTION_PATTERNS, upstream: true, downstream: true },
  created_at: "2026-11-16T20:00:00Z",
  started_at: "2026-11-16T20:00:03Z",
  finished_at: "2026-11-16T20:07:40Z",
  tasks: [],
  total_tasks: 5,
  ended_tasks: 5,
});

/** A resume of the failed run: it succeeded. */
const RESUME_OF = run(RESUME_OF_ID, {
  kind: "resume",
  resumed_from_run_id: FAILED_ID,
  status: "succeeded",
  created_at: "2026-11-16T19:00:00Z",
  started_at: "2026-11-16T19:00:02Z",
  finished_at: "2026-11-16T19:03:10Z",
  tasks: [],
  total_tasks: 3,
  ended_tasks: 3,
});

/**
 * A deploy of the repository to {@link ENV}.
 *
 * @param id - Its id.
 * @param over - The fields that differ from a deploy of the current version that succeeded.
 * @returns The deploy.
 */
function deploy(id: string, over: Partial<DeployOut> = {}): DeployOut {
  return {
    id,
    environment: ENV,
    repository: REPOSITORY,
    kind: "deploy",
    status: "succeeded",
    requested_ref: "main",
    resolved_sha: CURRENT_COMMIT,
    commit_subject: "Review 4-6: four jobs",
    project_version_id: VERSIONS.current,
    error_message: null,
    created_at: "2026-11-17T08:00:00Z",
    started_at: "2026-11-17T08:00:02Z",
    finished_at: "2026-11-17T08:00:40Z",
    submitted_by: "admin",
    jobs: null,
    actions: ["rollback"],
    ...over,
  };
}

/** The deploys of the repository, newest first, before a rollback. */
const DEPLOYS: readonly DeployOut[] = [
  deploy(DRY_RUN_ID, {
    kind: "dry_run",
    requested_ref: "feature/weekly",
    resolved_sha: "aa11bb22cc33dd44ee55ff6677889900aabbccdd",
    commit_subject: "Add the weekly report job",
    project_version_id: null,
    created_at: "2026-11-17T10:00:00Z",
    submitted_by: "ana.nowak",
  }),
  deploy(CURRENT_DEPLOY_ID),
  deploy(FAILED_DEPLOY_ID, {
    status: "failed",
    resolved_sha: "77ab88cd99ef00aa11bb22cc33dd44ee55ff6677",
    commit_subject: "Select the weekly report by tag",
    project_version_id: null,
    error_message: `${DEPLOY_FAILURE}\nTraceback (most recent call last):`,
    created_at: "2026-11-16T15:00:00Z",
    submitted_by: "ana.nowak",
  }),
  deploy(OLDER_DEPLOY_ID, {
    resolved_sha: OLDER_COMMIT,
    commit_subject: "Rank stations by daily riders",
    project_version_id: VERSIONS.older,
    created_at: "2026-11-16T09:00:00Z",
    submitted_by: null,
  }),
  deploy(OLD_ROLLBACK_ID, {
    kind: "rollback",
    requested_ref: null,
    resolved_sha: "0be1f6a9d2c3b4a5968778695a4b3c2d1e0f9a8b",
    commit_subject: null,
    project_version_id: VERSIONS.rollback,
    created_at: "2026-11-15T09:00:00Z",
  }),
];

/** The jobs each version defines, by the deploy that made it. */
const DEPLOY_JOBS: Readonly<Record<string, readonly string[]>> = {
  [CURRENT_DEPLOY_ID]: [JOB],
  [OLDER_DEPLOY_ID]: [JOB, OLDER_ONLY_JOB],
  [OLD_ROLLBACK_ID]: [JOB],
  [DRY_RUN_ID]: [JOB],
  [FAILED_DEPLOY_ID]: [],
  [NEW_ROLLBACK_ID]: [JOB, OLDER_ONLY_JOB],
};

/**
 * The rollback a test asked for: running at its first read, succeeded from the second on.
 *
 * @param reads - How often it was read before.
 * @returns The rollback.
 */
function newRollback(reads: number): DeployOut {
  return deploy(NEW_ROLLBACK_ID, {
    kind: "rollback",
    status: reads === 0 ? "running" : "succeeded",
    requested_ref: null,
    resolved_sha: OLDER_COMMIT,
    commit_subject: "Rank stations by daily riders",
    project_version_id: VERSIONS.older,
    created_at: "2026-11-17T11:00:00Z",
    finished_at: reads === 0 ? null : "2026-11-17T11:00:30Z",
  });
}

/**
 * Answers the deploys of {@link ENV}: the list (a rollback first once one was asked for), one
 * deploy with its jobs, the current versions (the older one once the rollback succeeded) and a
 * rollback, which the server accepts.
 *
 * @param page - The page.
 */
async function stubDeploys(page: Page): Promise<void> {
  let rolledBack = false;
  let reads = 0;
  const succeeded = (): boolean => rolledBack && reads > 1;
  await page.route(
    (url) => url.pathname === `${AT}/deploys`,
    (route) => {
      if (route.request().method() === "POST") {
        rolledBack = true;
        return route.fulfill({ status: 202, json: { id: NEW_ROLLBACK_ID, status: "queued" } });
      }
      const only = new URL(route.request().url()).searchParams.get("repository");
      const deploys = [...(rolledBack ? [newRollback(Math.max(reads - 1, 0))] : []), ...DEPLOYS];
      return route.fulfill({
        json: deploys.filter((one) => only === null || one.repository === only),
      });
    },
  );
  await page.route(
    (url) => url.pathname.startsWith(`${AT}/deploys/`),
    (route) => {
      const id = new URL(route.request().url()).pathname.slice(`${AT}/deploys/`.length);
      if (id === NEW_ROLLBACK_ID) {
        const answer = newRollback(reads);
        reads += 1;
        return route.fulfill({ json: { ...answer, jobs: DEPLOY_JOBS[id] } });
      }
      const found = DEPLOYS.find((one) => one.id === id);
      return found === undefined
        ? route.fulfill({ status: 404, json: { type: "NotFoundError", detail: "No such deploy." } })
        : route.fulfill({ json: { ...found, jobs: DEPLOY_JOBS[id] ?? [] } });
    },
  );
  await page.route(
    (url) => url.pathname === `${AT}/deployments`,
    (route) => {
      const current: DeploymentOut = {
        repository: REPOSITORY,
        project_version_id: succeeded() ? VERSIONS.older : VERSIONS.current,
        commit_sha: succeeded() ? OLDER_COMMIT : CURRENT_COMMIT,
        updated_at: succeeded() ? "2026-11-17T11:00:30Z" : "2026-11-17T08:00:40Z",
      };
      return route.fulfill({ json: [current] });
    },
  );
  await page.route(
    (url) => url.pathname === `${DEV_AT}/deploys` || url.pathname === `${DEV_AT}/deployments`,
    (route) => route.fulfill({ json: [] }),
  );
}

/**
 * The failed runs, newest end first: the job's failed run with its first failed task, and the
 * run of no job.
 *
 * @returns The rows.
 */
function failedRows(): FailedRunOut[] {
  const timedOut = FAILED_TASKS.find((one) => one.asset === TIMED_OUT);
  return [
    { run: { ...ADHOC_FAILED, tasks: [] }, task: null, failure: null },
    { run: { ...FAILED, tasks: [] }, task: TIMED_OUT, failure: timedOut?.failure ?? null },
  ];
}

/** The state the stubbed server keeps between requests. */
interface JobsState {
  paused: boolean;
  cancelled: boolean;
  started: boolean;
  resumed: boolean;
}

/**
 * The run with an id, as the server knows it now.
 *
 * @param state - The server's state.
 * @param id - The run's id.
 * @returns The run, or undefined for one it does not know.
 */
function runNow(state: JobsState, id: string): RunOut | undefined {
  const runs: Record<string, RunOut | undefined> = {
    [FAILED_ID]: FAILED,
    [RUNNING_ID]: running(state.cancelled),
    [STARTED_ID]: state.started ? run(STARTED_ID) : undefined,
    [RESUMED_ID]: state.resumed
      ? run(RESUMED_ID, {
          kind: "resume",
          resumed_from_run_id: FAILED_ID,
          created_at: "2026-11-17T09:40:00Z",
        })
      : undefined,
  };
  return runs[id];
}

/**
 * Answers `…/runs/{id}` and its parts: the run, its cancel, its resume, a task's log and the
 * rows of a failed check.
 *
 * @param route - The route.
 * @param state - The server's state, which a cancel or a resume changes.
 * @returns Resolves once answered.
 */
function runAnswer(route: Route, state: JobsState): Promise<void> {
  const url = new URL(route.request().url());
  const [id = "", ...rest] = url.pathname.slice(RUNS.length + 1).split("/");
  const part = rest.join("/");
  const found = runNow(state, id);
  if (found === undefined) {
    return route.fulfill({
      status: 404,
      json: { type: "NotFoundError", detail: `Run ${id} does not exist in ${ENV}.` },
    });
  }
  if (part === "cancel") {
    state.cancelled = true;
    return route.fulfill({ json: { ...running(false), cancel_requested: true } });
  }
  if (part === "resume") {
    state.resumed = true;
    return route.fulfill({ status: 201, json: runNow(state, RESUMED_ID) });
  }
  if (part === "checks/rows") {
    return route.fulfill({ status: 202, json: { query_id: ROWS_QUERY_ID } });
  }
  const log = /^tasks\/([^/]+)\/log$/u.exec(part);
  if (log !== null) {
    const offset = Number(url.searchParams.get("offset") ?? "0");
    return route.fulfill({ json: logPiece(decodeURIComponent(log[1] ?? ""), offset) });
  }
  return route.fulfill({ json: found });
}

/**
 * Adds to {@link stubJobs} a second job of the environment whose start waits for the running run
 * of {@link JOB}, in the jobs list and on its own page (#786).
 *
 * @param page - The page.
 */
export async function stubWaitingJob(page: Page): Promise<void> {
  const waiting: JobOut = {
    ...job(false),
    name: WAITING_JOB,
    select: ["silver.stock.*"],
    state: "waiting",
    waiting_for: { code: "job_waits", message: WAITING_MESSAGE },
    waits_for_run_id: RUNNING_ID,
    last_run_id: FAILED_ID,
    last_run_status: "succeeded",
    last_run_at: "2026-11-17T08:00:00Z",
    waiting_since: "2026-11-17T09:12:00Z",
    waiting_trigger: "manual",
    notify: null,
  };
  await page.route(
    (url) => url.pathname === `${AT}/jobs`,
    (route) => route.fulfill({ json: [job(false), waiting] }),
  );
  await page.route(
    (url) => url.pathname === `${AT}/jobs/${WAITING_JOB}`,
    (route) => route.fulfill({ json: waiting }),
  );
}

/**
 * Signs in as an administrator and answers the job, run and task pages like a server would: the
 * job with its schedule, pause and resume; its runs newest first (the running run, a run Run now
 * or Resume queued, the failed run); each run, its cancel and its resume (the running run reads
 * cancelled once its cancel was asked for); a task's log, the timed-out task's in three pieces;
 * the query of a failed check's rows; a 404 with its sentence for {@link MISSING_ID}; every run,
 * the ended ones on a second page; and the deploys of {@link ENV} with a rollback.
 *
 * @param page - The page.
 * @returns The mutating requests that reached the stubs, in order, as `METHOD path`.
 */
export async function stubJobs(page: Page): Promise<string[]> {
  await signInAsAdmin(page);
  const sent: string[] = [];
  page.on("request", (request) => {
    const path = new URL(request.url()).pathname;
    if (request.method() !== "GET" && path.startsWith(AT)) {
      sent.push(`${request.method()} ${path}`);
    }
  });
  const state: JobsState = { paused: false, cancelled: false, started: false, resumed: false };

  await page.route(
    (url) => url.pathname.startsWith(JOB_PATH),
    (route) => {
      const part = new URL(route.request().url()).pathname.slice(JOB_PATH.length);
      if (part === "/runs") {
        state.started = true;
        return route.fulfill({
          status: 201,
          json: { run: runNow(state, STARTED_ID), waiting: null },
        });
      }
      if (part === "/pause" || part === "/resume") {
        state.paused = part === "/pause";
      }
      return route.fulfill({ json: job(state.paused) });
    },
  );
  await page.route(
    (url) => url.pathname === RUNS,
    (route) => {
      const status = new URL(route.request().url()).searchParams.get("status");
      const runs = [RESUMED_ID, STARTED_ID, RUNNING_ID, FAILED_ID]
        .map((id) => runNow(state, id))
        .filter(
          (one): one is RunOut => one !== undefined && (status === null || one.status === status),
        )
        .map((one) => ({ ...one, tasks: [] }));
      return route.fulfill({ json: runs });
    },
  );
  await page.route(
    (url) => url.pathname.startsWith(`${RUNS}/`),
    (route) => runAnswer(route, state),
  );
  await page.route(
    (url) => url.pathname === ENVIRONMENTS_PATH,
    (route) => route.fulfill({ json: [environment(DEV, false), environment(ENV, true)] }),
  );
  await page.route(
    (url) => url.pathname === ALL_RUNS,
    (route) => {
      const query = new URL(route.request().url()).searchParams;
      const statuses = (query.get("status") ?? "").split(",").filter((word) => word !== "");
      const triggers = (query.get("trigger") ?? "").split(",").filter((word) => word !== "");
      const only = query.get("environment");
      const job = query.get("job");
      // The runs that ended come on a second page, behind the cursor `p2`.
      const second = query.get("cursor") === "p2";
      const runs = (
        second
          ? [SELECTION, RESUME_OF, FAILED, ADHOC_FAILED]
          : [
              ...[RESUMED_ID, STARTED_ID, RUNNING_ID].map((id) => runNow(state, id)),
              QUEUED,
              DEV_RUNNING,
            ]
      )
        .filter((one): one is RunOut => one !== undefined)
        .filter((one) => statuses.length === 0 || statuses.includes(one.status))
        .filter((one) => triggers.length === 0 || triggers.includes(one.trigger ?? ""))
        .filter((one) => only === null || one.environment === only)
        .filter((one) => job === null || one.job === job)
        .map((one) => ({ ...one, tasks: [] }));
      // Only the unfiltered list has a second page.
      const more = !second && statuses.length === 0 && triggers.length === 0 && job === null;
      return route.fulfill({ json: { items: runs, next_cursor: more ? "p2" : null } });
    },
  );
  await page.route(
    (url) => url.pathname === FAILED_RUNS,
    (route) => {
      const only = new URL(route.request().url()).searchParams.get("environment");
      const items = failedRows().filter((row) => only === null || row.run.environment === only);
      return route.fulfill({ json: { items, truncated: false } });
    },
  );
  await page.route(
    (url) => url.pathname === `${AT}/jobs`,
    (route) => route.fulfill({ json: [job(state.paused)] }),
  );
  await page.route(
    (url) => url.pathname === `${DEV_AT}/jobs`,
    (route) => route.fulfill({ json: [DEV_JOB_OUT] }),
  );
  await page.route(
    (url) => url.pathname === `${DEV_AT}/runs/${ADHOC_FAILED_ID}`,
    (route) => route.fulfill({ json: ADHOC_FAILED }),
  );
  await page.route(
    (url) => url.pathname === `${DEV_AT}/runs/${DEV_RUNNING_ID}`,
    (route) => route.fulfill({ json: DEV_RUNNING }),
  );
  await stubDeploys(page);
  return sent;
}
