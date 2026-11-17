/**
 * The calls of the job, run and task pages: a job and its runs, a run with its tasks, a task's
 * log, starting, pausing and resuming a job, cancelling and resuming a run, and the rows a failed
 * check found. The same routes `lakana jobs`, `lakana runs` and `lakana check` use.
 *
 * @packageDocumentation
 */
import { api, type RequestOptions } from "./client";
import { environmentPath, withQuery, type CursorPage } from "./manage";

/** The state of a run. */
export type RunStatus = "queued" | "running" | "succeeded" | "failed" | "cancelled";

/** The state of one task of a run. */
export type TaskStatus =
  "pending" | "ready" | "running" | "succeeded" | "failed" | "upstream_failed" | "cancelled";

/** The states of a run that is not over: the ones a page keeps reading. */
export const ACTIVE_RUN_STATUSES: readonly RunStatus[] = ["queued", "running"];

/** How many runs one page of a job's runs holds. */
const RUNS_PAGE = 50;

/** Why a task failed, was cancelled or never ran: a stable code and two sentences. */
export interface FailureOut {
  /** The code; a client shows a code it does not know, and `unknown`, as `internal`. */
  readonly code: string;
  /** One sentence: what happened. */
  readonly cause: string;
  /** One sentence: what to do about it. */
  readonly next_step: string;
}

/** Where a job reports, without the address. */
export interface NotifyOut {
  /** The webhook's format. */
  readonly format: "json" | "slack" | "teams";
  /** The events the job receives, such as `failed` or `recovered`. */
  readonly events: readonly string[];
  /** The name of the secret that holds the URL; absent unless the reader may deploy. */
  readonly secret?: string;
}

/** One way a job starts. A client ignores a `kind` it does not know. */
export type TriggerOut =
  | {
      /** A schedule. */
      readonly kind: "schedule";
      /** The cron expression. */
      readonly cron: string;
      /** The schedule's IANA zone. */
      readonly timezone: string;
      /** Whether the schedule is paused. */
      readonly paused: boolean;
      /** The next scheduled time (UTC); null for a paused schedule. */
      readonly next_run_at: string | null;
    }
  | {
      /** After each deploy that changes the project. */
      readonly kind: "deploy";
    }
  | {
      /** Through the API, the SDK or the CLI. */
      readonly kind: "on_demand";
    };

/** Why something waits: a stable code and one sentence. */
export interface WaitingOut {
  /** The stable code, such as `upstream` or `job_waits`. */
  readonly code: string;
  /** The server's sentence. */
  readonly message: string;
}

/** One job and the platform's state for it, as `GET …/jobs/{job}` answers it. */
export interface JobOut {
  /** The job's name. */
  readonly name: string;
  /** The environment. */
  readonly environment: string;
  /** The repository. */
  readonly repository: string;
  /** The selection's patterns. */
  readonly select: readonly string[];
  /** Whether ancestors are added. */
  readonly upstream: boolean;
  /** Whether descendants are added. */
  readonly downstream: boolean;
  /** The cron expression; null for a job without a schedule here. */
  readonly cron: string | null;
  /** The schedule's IANA zone; null without a schedule. */
  readonly timezone: string | null;
  /** The state in words, such as `scheduled`, `paused` or `waiting`. */
  readonly state: string;
  /** Whether the schedule is paused. */
  readonly paused: boolean;
  /** The next scheduled time (UTC); null when none. */
  readonly next_run_at: string | null;
  /** Why a start waits or the schedule cannot start; null otherwise. */
  readonly waiting_for: WaitingOut | null;
  /** The run a waiting start waits for. */
  readonly waits_for_run_id: string | null;
  /** The job's latest run. */
  readonly last_run_id: string | null;
  /** The latest run's status. */
  readonly last_run_status: RunStatus | null;
  /** When the latest run was created. */
  readonly last_run_at: string | null;
  /** How the job starts, schedule first. */
  readonly triggers: readonly TriggerOut[];
  /** The commit of the project version deployed in the environment. */
  readonly commit_sha: string;
  /** The link to `lakana.yaml` at that commit; null without a known git host. */
  readonly source_url: string | null;
  /** Where the job reports; null for a job without a usable `notify:`. */
  readonly notify: NotifyOut | null;
  /** What the reader's rights allow; the page also looks at the state. */
  readonly actions: readonly ("run" | "pause" | "resume")[];
}

/** The result of one data quality check, as a task lists it. */
export interface CheckResultOut {
  /** `<table>/<check>`. */
  readonly name: string;
  /** The checked table, dotted. */
  readonly table: string;
  /** How the check ended. */
  readonly outcome: "passed" | "failed" | "warned" | "errored";
  /** The count of rows that are wrong, when the check ran. */
  readonly failing_rows: number | null;
}

/** One task of a run. */
export interface TaskRunOut {
  /** The dotted table name. */
  readonly asset: string;
  /** Its state. */
  readonly status: TaskStatus;
  /** Attempts counted. */
  readonly attempt: number;
  /** Retries plus one. */
  readonly max_attempts: number;
  /** First claim; null before it. */
  readonly started_at: string | null;
  /** Final state; null before it. */
  readonly finished_at: string | null;
  /** Rows it wrote; null when none. */
  readonly rows_written: number | null;
  /** Why a `pending` or `ready` task has not started; null otherwise. */
  readonly waiting_for: WaitingOut | null;
  /** `asset` for a table the run builds, `source_check` for one it only checks. */
  readonly kind: "asset" | "source_check";
  /** The task's check results. */
  readonly checks: readonly CheckResultOut[];
  /** Why it failed, was cancelled or never ran; null for a task with no error. */
  readonly failure: FailureOut | null;
}

/** One run, as `GET …/runs/{id}` answers it; a listing leaves `tasks` empty. */
export interface RunOut {
  /** The run's id. */
  readonly id: string;
  /** The environment. */
  readonly environment: string;
  /** The repository. */
  readonly repository: string;
  /** The commit it runs. */
  readonly commit_sha: string;
  /** What kind of run it is. */
  readonly kind: "full" | "selection" | "resume";
  /** Its state. */
  readonly status: RunStatus;
  /** Whether a cancel was requested. */
  readonly cancel_requested: boolean;
  /** Who started it. */
  readonly triggered_by: string | null;
  /** The patterns and flags it was started with. */
  readonly selection: Readonly<Record<string, unknown>>;
  /** The run-level failure, in the server's words; null for none. */
  readonly error_message: string | null;
  /** When it was created. */
  readonly created_at: string;
  /** When it started; null before. */
  readonly started_at: string | null;
  /** When it ended; null until then. */
  readonly finished_at: string | null;
  /** Its tasks; empty in a listing. */
  readonly tasks: readonly TaskRunOut[];
  /** The job it belongs to; null for an ad-hoc run. */
  readonly job: string | null;
  /** What started it; null for an ad-hoc run. */
  readonly trigger: "schedule" | "api" | "manual" | "deploy" | null;
  /** How many tasks ended `failed`; also in a listing. */
  readonly failed_tasks: number;
  /** How many tasks the run has. */
  readonly total_tasks: number;
  /** How many of them ended. */
  readonly ended_tasks: number;
  /** What the reader's rights allow; the page also looks at the state. */
  readonly actions: readonly ("cancel" | "resume")[];
}

/** One attempt of a task. */
export interface TaskAttemptOut {
  /** The attempt's number, from 1. */
  readonly attempt: number;
  /** The worker's id. */
  readonly worker: string;
  /** When it started. */
  readonly started_at: string;
  /** When it ended; null while it runs. */
  readonly finished_at: string | null;
  /** How it ended; null while it runs. */
  readonly outcome: string | null;
  /** Rows it wrote. */
  readonly rows_written: number | null;
  /** Why it failed, as free text. */
  readonly error_message: string | null;
  /** The code, cause and next step of an attempt that did not succeed. */
  readonly failure: FailureOut | null;
  /** Highest resident memory in bytes; null when not measured. */
  readonly peak_memory_bytes: number | null;
  /** Processor time in seconds; null when not measured. */
  readonly cpu_seconds: number | null;
  /** Bytes read; null when unknown. */
  readonly bytes_read: number | null;
}

/** One piece of a task's log. */
export interface TaskLogOut {
  /** The log's text from the offset asked for. */
  readonly text: string;
  /** The task's attempts. */
  readonly attempts: readonly TaskAttemptOut[];
  /** The byte offset where `text` ends; send it as the next `offset`. */
  readonly next_offset: number;
  /** Whether stored bytes remain after `next_offset`. */
  readonly more: boolean;
  /** Whether the task has finished and `text` reaches the end of its log. */
  readonly complete: boolean;
  /** Whether the offset lay beyond the end of the log; start again from `next_offset`. */
  readonly reset: boolean;
}

/** What a start of a job did: the run (201) or the waiting start (202). */
export interface JobStartOut {
  /** The queued run; null when the start waits. */
  readonly run: RunOut | null;
  /** The waiting start; null when a run was queued. */
  readonly waiting: {
    /** The active run it waits for. */
    readonly waits_for_run_id: string;
    /** One sentence for a person. */
    readonly message: string;
  } | null;
}

/** What narrows a job's list of runs; null means no limit. */
export interface JobRunFilter {
  /** Only runs in this state. */
  readonly status: RunStatus | null;
  /** Only runs created at or after this time (ISO 8601 with a zone). */
  readonly since: string | null;
}

/**
 * The query that names a job in a repository.
 *
 * @param repository - The repository.
 * @returns The query.
 */
function repositoryQuery(repository: string): URLSearchParams {
  return new URLSearchParams({ repository });
}

/**
 * The path of a job, or of one of its parts.
 *
 * @param environment - The environment.
 * @param job - The job.
 * @param suffix - What follows it, such as `/pause`.
 * @returns The path, every part encoded.
 */
function jobApiPath(environment: string, job: string, suffix = ""): string {
  return environmentPath(environment, `/jobs/${encodeURIComponent(job)}${suffix}`);
}

/**
 * The path of a run, or of one of its parts.
 *
 * @param environment - The environment.
 * @param id - The run's id.
 * @param suffix - What follows it, such as `/cancel`.
 * @returns The path, every part encoded.
 */
function runApiPath(environment: string, id: string, suffix = ""): string {
  return environmentPath(environment, `/runs/${encodeURIComponent(id)}${suffix}`);
}

/**
 * Reads a job; a poll, so it never counts as the person's activity.
 *
 * @param environment - The environment.
 * @param repository - The repository the job is in.
 * @param job - The job's name.
 * @returns The job.
 * @throws {ApiError} for a refusal; 404 for a job that is not deployed.
 */
export function getJob(environment: string, repository: string, job: string): Promise<JobOut> {
  return api.request<JobOut>(
    "GET",
    withQuery(jobApiPath(environment, job), repositoryQuery(repository)),
    undefined,
    { background: true },
  );
}

/**
 * Lists a job's runs, newest first, one page at a time.
 *
 * @param environment - The environment.
 * @param repository - The repository the job is in.
 * @param job - The job's name.
 * @param filter - What narrows the list.
 * @param cursor - The cursor of the page, or null for the first.
 * @param options - Whether the read is a background poll (the first page read again), which
 *   never counts as the person's activity; no by default (Show more).
 * @returns The page; its runs have no tasks.
 * @throws {ApiError} for a refusal.
 */
export async function listJobRuns(
  environment: string,
  repository: string,
  job: string,
  filter: JobRunFilter,
  cursor: string | null,
  options: RequestOptions = {},
): Promise<CursorPage<RunOut>> {
  const query = new URLSearchParams({ job, repository });
  if (filter.status !== null) {
    query.set("status", filter.status);
  }
  if (filter.since !== null) {
    query.set("since", filter.since);
  }
  query.set("limit", String(RUNS_PAGE));
  if (cursor !== null) {
    query.set("cursor", cursor);
  }
  const page = await api.page(withQuery(environmentPath(environment, "/runs"), query), options);
  return { items: page.body as readonly RunOut[], nextCursor: page.nextCursor };
}

/**
 * Reads a run with its tasks; a poll, so it never counts as the person's activity.
 *
 * @param environment - The environment.
 * @param id - The run's id.
 * @returns The run.
 * @throws {ApiError} for a refusal; 404 for a run that does not exist.
 */
export function getRun(environment: string, id: string): Promise<RunOut> {
  return api.request<RunOut>("GET", runApiPath(environment, id), undefined, {
    background: true,
  });
}

/**
 * Reads a task's log from a byte offset; a poll.
 *
 * @param environment - The environment.
 * @param id - The run's id.
 * @param asset - The task's dotted table name.
 * @param offset - The byte to read from.
 * @returns At most 1 MiB of text, and where to go on.
 * @throws {ApiError} for a refusal.
 */
export function getTaskLog(
  environment: string,
  id: string,
  asset: string,
  offset: number,
): Promise<TaskLogOut> {
  const query = new URLSearchParams({ offset: String(offset) });
  return api.request<TaskLogOut>(
    "GET",
    withQuery(runApiPath(environment, id, `/tasks/${encodeURIComponent(asset)}/log`), query),
    undefined,
    { background: true },
  );
}

/**
 * Starts a run of a job.
 *
 * @param environment - The environment.
 * @param repository - The repository the job is in.
 * @param job - The job's name.
 * @returns The queued run (201), or the waiting start (202) when the project is busy.
 * @throws {ApiError} for a refusal.
 */
export function startJob(
  environment: string,
  repository: string,
  job: string,
): Promise<JobStartOut> {
  return api.request<JobStartOut>("POST", jobApiPath(environment, job, "/runs"), { repository });
}

/**
 * Pauses a job's schedule.
 *
 * @param environment - The environment.
 * @param repository - The repository the job is in.
 * @param job - The job's name.
 * @returns The job.
 * @throws {ApiError} for a refusal; 409 for a job with no schedule here.
 */
export function pauseJob(environment: string, repository: string, job: string): Promise<JobOut> {
  return api.request<JobOut>("POST", jobApiPath(environment, job, "/pause"), { repository });
}

/**
 * Resumes a job's schedule.
 *
 * @param environment - The environment.
 * @param repository - The repository the job is in.
 * @param job - The job's name.
 * @returns The job.
 * @throws {ApiError} for a refusal.
 */
export function resumeJob(environment: string, repository: string, job: string): Promise<JobOut> {
  return api.request<JobOut>("POST", jobApiPath(environment, job, "/resume"), { repository });
}

/**
 * Asks a run to stop.
 *
 * @param environment - The environment.
 * @param id - The run's id.
 * @returns The run, with `cancel_requested`.
 * @throws {ApiError} for a refusal; 409 for a run that has ended.
 */
export function cancelRun(environment: string, id: string): Promise<RunOut> {
  return api.request<RunOut>("POST", runApiPath(environment, id, "/cancel"));
}

/**
 * Starts the failed, upstream-failed and cancelled part of an ended run again.
 *
 * @param environment - The environment.
 * @param id - The run to resume.
 * @returns The new run.
 * @throws {ApiError} for a refusal; 409 for a run that is still active.
 */
export function resumeRun(environment: string, id: string): Promise<RunOut> {
  return api.request<RunOut>("POST", runApiPath(environment, id, "/resume"));
}

/**
 * Queues a query for the rows a check found; it runs under the person's own rights.
 *
 * @param environment - The environment.
 * @param runId - The run's id.
 * @param check - The check, `<namespace>.<table>/<check>`.
 * @returns The id of the query, which the Queries screen shows.
 * @throws {ApiError} for a refusal; 403 without the right to submit queries.
 */
export async function showCheckRows(
  environment: string,
  runId: string,
  check: string,
): Promise<string> {
  const answer = await api.request<{ readonly query_id: string }>(
    "POST",
    runApiPath(environment, runId, "/checks/rows"),
    { check },
  );
  return answer.query_id;
}
