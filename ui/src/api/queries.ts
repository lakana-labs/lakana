/**
 * The calls of the Queries screen: writing a query, following its run, reading its result, the
 * list of the person's own recent queries, and the environments where one may run.
 *
 * @packageDocumentation
 */
import { api } from "./client";
import { environmentPath, withQuery, type CursorPage } from "./manage";
import { type QueryRunStatus, type ResultPageOut } from "./table";

/** The largest file the browser downloads; the one definition, which artifacts use too. */
export { BROWSER_DOWNLOAD_LIMIT } from "./artifacts";

/** Where a run came from. A client treats a value it does not know as `written`. */
export type QueryOrigin = "written" | "preview" | "check" | "write" | "maintenance" | "task";

/** Why a queued run has not started: a stable code and one sentence. */
export interface QueryWaitingOut {
  /** The stable code, such as `no_worker` or `user_limit`. */
  readonly code: string;
  /** The server's sentence. */
  readonly message: string;
}

/** A query run, as `GET …/queries/{id}` answers it (the fields the Queries screen reads). */
export interface QueryRunOut {
  /** The run's id. */
  readonly query_id: string;
  /** Its state. */
  readonly status: QueryRunStatus;
  /** Where it came from. */
  readonly origin: QueryOrigin;
  /** The text as submitted; null for a run that is not `written` or `write`. */
  readonly sql: string | null;
  /** Why a `FAILED` or `CANCELLED` run ended, in the server's words; else null. */
  readonly error_message: string | null;
  /** The cause of such a run, such as `invalid_query`; else null. */
  readonly error_cause: string | null;
  /** The line, from 1, where an `invalid_query` failure points; null when unknown. */
  readonly error_line: number | null;
  /** The column on that line, in characters from 1; null when unknown. */
  readonly error_column: number | null;
  /** Rows of the stored result; null before it exists and for a run from before the field. */
  readonly result_rows: number | null;
  /** Size in bytes of the stored result; null as `result_rows`. */
  readonly result_bytes: number | null;
  /** When the run was submitted. */
  readonly created_at: string;
  /** When a worker started it; null while queued. */
  readonly started_at: string | null;
  /** When it ended; null until then. */
  readonly finished_at: string | null;
  /** Why a `QUEUED` run waits; null for any other state. */
  readonly waiting_for: QueryWaitingOut | null;
}

/** One row of the list of queries, as `GET /api/v1/queries` answers it. Never the SQL. */
export interface QueryListItemOut {
  /** The run's id. */
  readonly id: string;
  /** Its environment. */
  readonly environment: string;
  /** Where it came from. */
  readonly origin: QueryOrigin;
  /** Who submitted it; null once that principal is deleted. */
  readonly submitter: string | null;
  /** `read`, `create`, `replace`, `append`, `view` or `maintain`. */
  readonly kind: string;
  /** A write run's target table; null for a read. */
  readonly target: string | null;
  /** Its state. */
  readonly status: QueryRunStatus;
  /** When it was submitted. */
  readonly created_at: string;
  /** When a worker started it. */
  readonly started_at: string | null;
  /** When it ended. */
  readonly finished_at: string | null;
  /** Why a `FAILED` or `CANCELLED` run ended; never the message. */
  readonly error_cause: string | null;
  /** Why a `QUEUED` run waits; null otherwise. */
  readonly waiting_for: QueryWaitingOut | null;
}

/** An environment where the person may run a query. */
export interface QueryEnvironmentOut {
  /** The environment's name. */
  readonly name: string;
  /** Whether it is protected. */
  readonly protected: boolean;
}

/** The formats of a whole result the browser downloads. */
export type ResultFormat = "csv" | "xlsx";

/** The media type the server answers each {@link ResultFormat} with. */
export const RESULT_MEDIA_TYPES: Record<ResultFormat, string> = {
  csv: "text/csv",
  xlsx: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
};

/** The most rows a spreadsheet holds below its header row; the server refuses more. */
export const XLSX_MAX_ROWS = 1_048_575;

/** How many days back the person's recent queries reach. */
export const RECENT_DAYS = 30;

/** How many recent queries a page lists. */
export const RECENT_PAGE = 50;

/** One day, in milliseconds. */
const DAY_MS = 86_400_000;

/**
 * Submits a query to run as the person.
 *
 * @param environment - The environment to run in.
 * @param sql - The SQL text.
 * @returns The id of the run.
 * @throws {ApiError} for a refusal: 403, 404 or 422 for text the guard refuses.
 */
export async function submitQuery(environment: string, sql: string): Promise<string> {
  const answer = await api.request<{ readonly query_id: string }>(
    "POST",
    environmentPath(environment, "/queries"),
    { sql },
  );
  return answer.query_id;
}

/**
 * Reads a run's state; a poll, so it never counts as the person's activity.
 *
 * @param environment - The environment.
 * @param id - The run's id.
 * @returns The run.
 * @throws {ApiError} for a refusal.
 */
export function getQueryRun(environment: string, id: string): Promise<QueryRunOut> {
  return api.request<QueryRunOut>("GET", runPath(environment, id), undefined, {
    background: true,
  });
}

/**
 * Reads one page of a finished run's result as JSON.
 *
 * @param environment - The environment.
 * @param id - The run's id.
 * @param offset - The first row of the page.
 * @param limit - The most rows of the page.
 * @returns The page.
 * @throws {ApiError} for a refusal; 409 for a run with no stored result.
 */
export function getResultPageAt(
  environment: string,
  id: string,
  offset: number,
  limit: number,
): Promise<ResultPageOut> {
  const query = new URLSearchParams({
    format: "json",
    limit: String(limit),
    offset: String(offset),
  });
  return api.request<ResultPageOut>(
    "GET",
    withQuery(runPath(environment, id, "/result"), query),
    undefined,
    { background: true },
  );
}

/**
 * Lists the person's own written queries of the last {@link RECENT_DAYS} days, newest first.
 *
 * @param me - The person's principal name.
 * @param environment - Only this environment; null for all.
 * @param cursor - The cursor of the page, or null for the first.
 * @param now - The time the days count back from.
 * @returns The page.
 * @throws {ApiError} for a refusal.
 */
export async function listMyQueries(
  me: string,
  environment: string | null,
  cursor: string | null,
  now: Date = new Date(),
): Promise<CursorPage<QueryListItemOut>> {
  const query = new URLSearchParams({
    submitter: me,
    origin: "written",
    include_ended: "true",
    since: new Date(now.getTime() - RECENT_DAYS * DAY_MS).toISOString(),
    limit: String(RECENT_PAGE),
  });
  if (environment !== null) {
    query.set("environment", environment);
  }
  if (cursor !== null) {
    query.set("cursor", cursor);
  }
  const body = await api.request<{
    items: readonly QueryListItemOut[];
    next_cursor: string | null;
  }>("GET", withQuery("/api/v1/queries", query));
  return { items: body.items, nextCursor: body.next_cursor };
}

/**
 * Lists the environments where the person may run a query, by name.
 *
 * @returns The environments; empty when there is none.
 * @throws {ApiError} for a refusal.
 */
export function listQueryEnvironments(): Promise<readonly QueryEnvironmentOut[]> {
  return api.request<readonly QueryEnvironmentOut[]>("GET", "/api/v1/queries/environments");
}

/**
 * Downloads a run's whole result, never a page of it.
 *
 * @remarks
 * The size limit is off: the page asks only after it has checked `result_bytes` against
 * `BROWSER_DOWNLOAD_LIMIT`, and the server caps what a spreadsheet may hold.
 * @param environment - The environment.
 * @param id - The run's id.
 * @param format - The file's format.
 * @returns The file.
 * @throws {ApiError} for a refusal, such as 422 for a spreadsheet that is too large.
 * @throws {WrongFileType} when the server sends something other than the format.
 * @throws {DownloadCut} when the body breaks off.
 */
export function downloadResult(
  environment: string,
  id: string,
  format: ResultFormat,
): Promise<Blob> {
  return api.download(
    withQuery(runPath(environment, id, "/result"), new URLSearchParams({ format })),
    Number.POSITIVE_INFINITY,
    RESULT_MEDIA_TYPES[format],
  );
}

/**
 * The path of a run of an environment, or of one of its parts.
 *
 * @param environment - The environment.
 * @param id - The run's id.
 * @param suffix - What follows the id, such as `/result`.
 * @returns The path, every part encoded.
 */
function runPath(environment: string, id: string, suffix = ""): string {
  return environmentPath(environment, `/queries/${encodeURIComponent(id)}${suffix}`);
}
