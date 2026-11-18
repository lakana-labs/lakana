/**
 * Stubs for the end-to-end tests of the job, run and task pages: the job `daily_sales` of the
 * repository `analytics` in `prod`, scheduled and notifying Slack through a secret; a failed run
 * of seven tasks (one timed out, one never ran because of it, one blocked by a failed check); a
 * running run that ends once cancelled; a task's log in three pieces; and what Run now, Pause,
 * Resume schedule, Cancel and Resume answer. What a control changes shows in the reads after it.
 *
 * @packageDocumentation
 */
import type { Page, Route } from "@playwright/test";

import type { JobOut, RunOut, TaskAttemptOut, TaskLogOut, TaskRunOut } from "../src/api/jobs";

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

/** The path of an environment's part. */
const AT = `/api/v1/environments/${ENV}`;

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

/** The paths these stubs answer, beside those of `session.ts`. */
export const JOBS_STUBBED = [
  ...STUBBED,
  JOB_PATH,
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

/** The attempts of the timed-out task. */
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
  peak_memory_bytes: 805_306_368,
  cpu_seconds: 1_795,
  bytes_read: 52_428_800,
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
      ? run(RESUMED_ID, { kind: "resume", created_at: "2026-11-17T09:40:00Z" })
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
 * Signs in as an administrator and answers the job, run and task pages like a server would: the
 * job with its schedule, pause and resume; its runs newest first (the running run, a run Run now
 * or Resume queued, the failed run); each run, its cancel and its resume (the running run reads
 * cancelled once its cancel was asked for); a task's log, the timed-out task's in three pieces;
 * the query of a failed check's rows; and a 404 with its sentence for {@link MISSING_ID}.
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
  return sent;
}
