/**
 * The calls of the Jobs screen: an environment's jobs, the runs of every environment and the
 * failed ones, a job and its runs, a run with its tasks, a task's log, starting, pausing and
 * resuming a job, cancelling and resuming a run, and the rows a failed check found. The same
 * routes `lakana jobs`, `lakana runs` and `lakana check` use.
 *
 * @packageDocumentation
 */
import { api, type RequestOptions } from "./client";
import { environmentPath, withQuery, type CursorPage } from "./manage";
import { type ListSortChoice, setSort } from "./sorting";

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

/** A job's schedule, as one of its triggers. */
interface ScheduleTriggerOut {
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

/** A job that starts after each deploy that changes the project. */
interface DeployTriggerOut {
  /** After each deploy. */
  readonly kind: "deploy";
}

/** A job that starts through the API, the SDK or the CLI. */
interface OnDemandTriggerOut {
  /** On demand. */
  readonly kind: "on_demand";
}

/**
 * One way a job starts. A client ignores a `kind` it does not know. Each is an interface of its
 * own, so the hidden-character check (`e2e/api-types.ts`) reads each `kind` as the code it is.
 */
export type TriggerOut = ScheduleTriggerOut | DeployTriggerOut | OnDemandTriggerOut;

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
  /** The name of the principal that paused the schedule; null when not paused or since deleted. */
  readonly paused_by: string | null;
  /** When the schedule was paused; null when it is not paused. */
  readonly paused_at: string | null;
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
  /** For a start that waits (`waiting`, `waiting to start`), when it was first asked for. */
  readonly waiting_since: string | null;
  /** For a start that waits, what asked for it. */
  readonly waiting_trigger: "schedule" | "api" | "manual" | "deploy" | null;
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
  /** The run a resume continues; null for a run that is not a resume. */
  readonly resumed_from_run_id: string | null;
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
  /** The sort (`created`, `status`); none for the newest first. */
  readonly sort?: ListSortChoice | null;
}

/** What started a run, as the list of all runs filters by it. */
type RunTrigger = "schedule" | "api" | "manual" | "deploy";

/** What narrows the runs of every environment; an empty list or null means no limit. */
export interface AllRunsFilter {
  /** Only this environment's runs. */
  readonly environment: string | null;
  /** Only runs in these states. */
  readonly status: readonly RunStatus[];
  /** Only runs started this way. */
  readonly trigger: readonly RunTrigger[];
  /** Only this job's runs. */
  readonly job: string | null;
  /** Only this repository's runs. */
  readonly repository: string | null;
  /** Only runs created at or after this time (ISO 8601 with a zone). */
  readonly since: string | null;
  /** The sort (`created`, `name`, `status`); none for the newest first. */
  readonly sort?: ListSortChoice | null;
}

/** One row of the failed runs: the run, its first failed task and why that task failed. */
export interface FailedRunOut {
  /** The run, without tasks, with its `actions`. */
  readonly run: RunOut;
  /** The asset of its first failed task; null when the run failed without one. */
  readonly task: string | null;
  /** That task's failure; null with `task`, and then `run.error_message` says why. */
  readonly failure: FailureOut | null;
}

/** The failed runs, by the rule of Home's Needs your attention. */
export interface FailedRunsOut {
  /** One row per job (or per repository, for its runs of no job), newest end first. */
  readonly items: readonly FailedRunOut[];
  /** Whether the server left rows out; choosing an environment shows them. */
  readonly truncated: boolean;
}

/** One page of the runs of every environment, as `GET /api/v1/runs` answers it. */
interface RunPageOut {
  /** The runs of the page, without tasks. */
  readonly items: readonly RunOut[];
  /** The cursor of the next page; null on the last. */
  readonly next_cursor: string | null;
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
  setSort(query, filter.sort ?? null);
  query.set("limit", String(RUNS_PAGE));
  if (cursor !== null) {
    query.set("cursor", cursor);
  }
  const page = await api.page(withQuery(environmentPath(environment, "/runs"), query), options);
  return { items: page.body as readonly RunOut[], nextCursor: page.nextCursor };
}

/**
 * Lists an environment's jobs; a poll, so it never counts as the person's activity. Jobs are not
 * paged.
 *
 * @param environment - The environment.
 * @param repository - Only this repository's jobs; all of them by default.
 * @param sort - The sort (`name`, `last_run`, `next_run`); null for repository, then name.
 * @returns The jobs.
 * @throws {ApiError} for a refusal; 404 for an environment or a repository that does not exist.
 */
export function listJobs(
  environment: string,
  repository?: string,
  sort: ListSortChoice | null = null,
): Promise<readonly JobOut[]> {
  const path = environmentPath(environment, "/jobs");
  const query = repository === undefined ? new URLSearchParams() : repositoryQuery(repository);
  setSort(query, sort);
  return api.request<readonly JobOut[]>(
    "GET",
    query.size === 0 ? path : withQuery(path, query),
    undefined,
    { background: true },
  );
}

/**
 * Lists the runs of every environment the person may read, or of one, newest first, one page at
 * a time; a poll unless asked otherwise.
 *
 * @param filter - What narrows the list.
 * @param cursor - The cursor of the page, or null for the first.
 * @param options - How many runs a page holds (50 by default), and whether the read is a
 *   background poll that never counts as the person's activity (yes by default; no for Show
 *   more).
 * @param options.limit - How many runs a page holds.
 * @param options.background - Whether the read is a background poll.
 * @returns The page, with the cursor from the body; its runs have no tasks.
 * @throws {ApiError} for a refusal; with an environment, 403 or 404 as for its own list.
 */
export async function listAllRuns(
  filter: AllRunsFilter,
  cursor: string | null,
  {
    limit = RUNS_PAGE,
    background = true,
  }: { readonly limit?: number; readonly background?: boolean } = {},
): Promise<CursorPage<RunOut>> {
  const query = new URLSearchParams();
  if (filter.environment !== null) {
    query.set("environment", filter.environment);
  }
  if (filter.status.length > 0) {
    query.set("status", filter.status.join(","));
  }
  if (filter.trigger.length > 0) {
    query.set("trigger", filter.trigger.join(","));
  }
  if (filter.job !== null) {
    query.set("job", filter.job);
  }
  if (filter.repository !== null) {
    query.set("repository", filter.repository);
  }
  if (filter.since !== null) {
    query.set("since", filter.since);
  }
  setSort(query, filter.sort ?? null);
  query.set("limit", String(limit));
  if (cursor !== null) {
    query.set("cursor", cursor);
  }
  const page = await api.request<RunPageOut>("GET", withQuery("/api/v1/runs", query), undefined, {
    background,
  });
  return { items: page.items, nextCursor: page.next_cursor };
}

/**
 * Lists the failed runs that need attention, by the rule of Home's Needs your attention; a poll.
 *
 * @param environment - Only this environment's; every environment the person may read by
 *   default.
 * @returns The rows in the server's order, and whether it left some out.
 * @throws {ApiError} for a refusal; with an environment, 403 or 404.
 */
export function listFailedRuns(environment: string | null): Promise<FailedRunsOut> {
  const path = "/api/v1/runs/failed";
  return api.request<FailedRunsOut>(
    "GET",
    environment === null ? path : withQuery(path, new URLSearchParams({ environment })),
    undefined,
    { background: true },
  );
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

/** The state of a deploy. */
type DeployStatus = "queued" | "running" | "succeeded" | "failed";

/** One deploy of a repository to an environment, as `GET …/deploys` lists it. */
export interface DeployOut {
  /** The deploy's id. */
  readonly id: string;
  /** The environment. */
  readonly environment: string;
  /** The repository. */
  readonly repository: string;
  /** What it does: deploy a commit, try it without changing anything, or roll back to one. */
  readonly kind: "deploy" | "dry_run" | "rollback";
  /** Its state. */
  readonly status: DeployStatus;
  /** The ref asked for; null for a rollback. */
  readonly requested_ref: string | null;
  /** The commit it deployed; null before the worker resolved it. */
  readonly resolved_sha: string | null;
  /**
   * The first line of that commit's message; for a rollback, the one its target was deployed
   * with. Null before the worker resolved the commit and for a deploy from before it was stored.
   */
  readonly commit_subject: string | null;
  /** The project version it made; null before it made one, and for a dry run. */
  readonly project_version_id: string | null;
  /** Why it failed, in the server's words; null for none. */
  readonly error_message: string | null;
  /** When it was asked for. */
  readonly created_at: string;
  /** When it started; null before. */
  readonly started_at: string | null;
  /** When it ended; null until then. */
  readonly finished_at: string | null;
  /** Who asked for it; null for an account since deleted. */
  readonly submitted_by: string | null;
  /** The jobs its version defines; only when one deploy is read, null in a list. */
  readonly jobs: readonly string[] | null;
  /**
   * What the reader's rights allow on it: `rollback` to its commit, `undeploy` its repository
   * from the environment.
   */
  readonly actions: readonly ("rollback" | "undeploy")[];
}

/** The current version of one repository in an environment. */
export interface DeploymentOut {
  /** The repository. */
  readonly repository: string;
  /** Its current project version. */
  readonly project_version_id: string;
  /** That version's commit. */
  readonly commit_sha: string;
  /** When the version became current. */
  readonly updated_at: string;
  /** The service account its runs write as; null when none is bound. */
  readonly run_as: string | null;
}

/** What the server answers a deploy it accepted. */
export interface DeployAcceptedOut {
  /** The deploy's id. */
  readonly id: string;
  /** Its state, `queued`. */
  readonly status: string;
}

/** How many deploys one page holds. */
const DEPLOYS_PAGE = 50;

/**
 * Lists an environment's deploys, newest first, one page at a time.
 *
 * @param environment - The environment.
 * @param repository - Only this repository's deploys; null for all.
 * @param cursor - The cursor of the page, or null for the first.
 * @param sort - The sort (`created`, `status`); null for the newest first.
 * @returns The page, with the cursor from `X-Next-Cursor`.
 * @throws {ApiError} for a refusal; 404 for an environment or a repository that does not exist.
 */
export async function listDeploys(
  environment: string,
  repository: string | null,
  cursor: string | null,
  sort: ListSortChoice | null = null,
): Promise<CursorPage<DeployOut>> {
  const query = new URLSearchParams();
  if (repository !== null) {
    query.set("repository", repository);
  }
  setSort(query, sort);
  query.set("limit", String(DEPLOYS_PAGE));
  if (cursor !== null) {
    query.set("cursor", cursor);
  }
  const page = await api.page(withQuery(environmentPath(environment, "/deploys"), query));
  return { items: page.body as readonly DeployOut[], nextCursor: page.nextCursor };
}

/**
 * Reads one deploy with the jobs its version defines; a poll, so it never counts as the person's
 * activity.
 *
 * @param environment - The environment.
 * @param id - The deploy's id.
 * @returns The deploy.
 * @throws {ApiError} for a refusal; 404 for a deploy that does not exist.
 */
export function getDeploy(environment: string, id: string): Promise<DeployOut> {
  return api.request<DeployOut>(
    "GET",
    environmentPath(environment, `/deploys/${encodeURIComponent(id)}`),
    undefined,
    { background: true },
  );
}

/**
 * Lists the current version of each repository in an environment.
 *
 * @param environment - The environment.
 * @returns One entry per deployed repository.
 * @throws {ApiError} for a refusal.
 */
export function listDeployments(environment: string): Promise<readonly DeploymentOut[]> {
  return api.request<readonly DeploymentOut[]>("GET", environmentPath(environment, "/deployments"));
}

/**
 * Rolls a repository back to a commit already deployed to the environment.
 *
 * @param environment - The environment.
 * @param repository - The repository.
 * @param sha - The full commit to roll back to.
 * @returns The accepted rollback, queued.
 * @throws {ApiError} for a refusal; 403 without `DEPLOY`, 404 for a commit never deployed here.
 */
export function rollBack(
  environment: string,
  repository: string,
  sha: string,
): Promise<DeployAcceptedOut> {
  return api.request<DeployAcceptedOut>("POST", environmentPath(environment, "/deploys"), {
    repository,
    kind: "rollback",
    to_sha: sha,
  });
}

/**
 * Undeploys a repository from an environment: its jobs, their schedules and a waiting start go,
 * and so do its run principal, its glossary terms and its tables' health effects there; the
 * tables and their data stay, as orphans.
 *
 * @param environment - The environment.
 * @param repository - The repository.
 * @param projectVersionId - The project version the person saw as current: the server refuses
 *   the undeploy if the deployment points at another one by then. Omitted, whatever is current
 *   goes.
 * @returns Nothing.
 * @throws {ApiError} for a refusal; 403 without `DEPLOY` or for a CI token, 404 for a repository
 *   not deployed there, 409 while a run, deploy or rollback of it is queued or running there, or
 *   when its deployment is no longer at `projectVersionId`.
 */
export async function undeploy(
  environment: string,
  repository: string,
  projectVersionId?: string,
): Promise<void> {
  const query = new URLSearchParams();
  if (projectVersionId !== undefined) {
    query.set("expected_project_version_id", projectVersionId);
  }
  await api.request<undefined>(
    "DELETE",
    withQuery(
      environmentPath(environment, `/deployments/${encodeURIComponent(repository)}`),
      query,
    ),
  );
}
