/**
 * The calls that manage tables and the catalog's views of them: drop, restore, rebuild,
 * maintain, retention, references, and the lists of dropped, expiring and orphaned tables.
 *
 * @packageDocumentation
 */
import { api, ApiError } from "./client";
import { tableApiPath, type ModelReferenceOut, type TableOut, type TableRef } from "./table";

/** The size of every page; it is the server's default, sent so a change there changes nothing here. */
const PAGE_SIZE = 100;

/** A dropped table whose files are not deleted yet. */
export interface DroppedTableOut {
  /** The table's name when it was dropped. */
  readonly name: string;
  /** The dotted namespace it was dropped from. */
  readonly namespace: string;
  /** The table's id, which a restore keeps. */
  readonly uuid: string;
  /** What it was. */
  readonly kind: string;
  /** When it was dropped. */
  readonly dropped_at: string;
  /** From when on its files can be deleted. */
  readonly purge_after: string;
  /** Whether a restore would still work. */
  readonly restorable: boolean;
  /** `undrop` when the caller's rights allow a restore. */
  readonly actions: readonly "undrop"[];
}

/** A table's retention and when it expires. */
export interface TableRetentionOut {
  /** The table's name. */
  readonly name: string;
  /** The dotted namespace. */
  readonly namespace: string;
  /** The table's id. */
  readonly uuid: string;
  /** Who sets the retention that applies; null when none does. */
  readonly source: "table" | "environment" | null;
  /** The days after its last activity it expires. */
  readonly days: number | null;
  /** The day it expires on. */
  readonly on: string | null;
  /** Whether the table says it never expires. */
  readonly never: boolean;
  /** Who set the retention; null when nobody did or the person is gone. */
  readonly set_by: string | null;
  /** When it was set. */
  readonly set_at: string | null;
  /** The later of the table's last change and its last read. */
  readonly last_activity_at: string;
  /** When it expires; null when it never does. */
  readonly expires_at: string | null;
  /** Why a table with a retention never expires; null otherwise. */
  readonly kept: "protected" | "project_asset" | null;
}

/** A table no project produces any more. */
export interface OrphanOut {
  /** The repository that used to produce it. */
  readonly repository: string;
  /** The dotted table name. */
  readonly table: string;
  /** What it is. */
  readonly kind: "TABLE" | "EXTERNAL" | "VIEW" | "ARTIFACT" | null;
  /** False for a dropped orphan that can still be restored. */
  readonly exists: boolean;
  /** `drop` when the caller's rights allow it. */
  readonly actions: readonly "drop"[];
}

/** A table's references, in both directions. */
export interface TableReferencesOut {
  /** The table's dotted name. */
  readonly table: string;
  /** What its header declares, where the caller may read the target. */
  readonly outgoing: readonly ModelReferenceOut[];
  /** How many of its references point at a table the caller may not read. */
  readonly hidden_outgoing: number;
  /** What other tables declare to it, from tables the caller may read. */
  readonly incoming: readonly ModelReferenceOut[];
  /** How many references to it come from tables the caller may not read. */
  readonly hidden_incoming: number;
}

/** What a rebuild did. */
export interface TableRebuildOut {
  /** The table's dotted name. */
  readonly table: string;
  /** The queued run that builds it again. */
  readonly run_id: string;
  /** The repository whose asset builds the table. */
  readonly repository: string;
  /** The asset's file; null to a caller who may not read the table. */
  readonly path: string | null;
}

/** What table maintenance keeps and deletes. */
export interface MaintenanceOptions {
  /** Snapshots younger than this many days are kept. */
  readonly keepDays: number;
  /** The most recent this many snapshots are kept. */
  readonly keepLast: number;
  /** Report what would happen without changing anything. */
  readonly dryRun: boolean;
  /** Unreferenced files younger than this many hours are never deleted. */
  readonly olderThanHours: number;
}

/** The server's defaults for maintenance. */
export const MAINTENANCE_DEFAULTS: MaintenanceOptions = {
  keepDays: 7,
  keepLast: 5,
  dryRun: false,
  olderThanHours: 72,
};

/** When a table expires: after some days, on a day, or never. */
export type RetentionRule = { days: number } | { on: string } | { never: true };

/** One page of a list, and the cursor of the next. */
export interface CursorPage<T> {
  /** The page's items. */
  readonly items: readonly T[];
  /** The cursor of the next page; null on the last. */
  readonly nextCursor: string | null;
}

/**
 * The path of an environment's part.
 *
 * @param environment - The environment.
 * @param suffix - What follows it, such as `/orphans`.
 * @returns The path.
 */
function environmentPath(environment: string, suffix = ""): string {
  return `/api/v1/environments/${encodeURIComponent(environment)}${suffix}`;
}

/**
 * Appends a query to a path, if it has anything.
 *
 * @param path - The path.
 * @param query - The query.
 * @returns The path with its `?query`, or the path alone.
 */
function withQuery(path: string, query: URLSearchParams): string {
  const text = query.toString();
  return text === "" ? path : `${path}?${text}`;
}

/**
 * The query of a list page: its size, and its cursor when it is not the first.
 *
 * @param cursor - The cursor, or null for the first page.
 * @param query - A query to start from.
 * @returns The query.
 */
function pageQuery(cursor: string | null, query = new URLSearchParams()): URLSearchParams {
  query.set("limit", String(PAGE_SIZE));
  if (cursor !== null) {
    query.set("cursor", cursor);
  }
  return query;
}

/**
 * The query that carries a break-glass reason, when there is one.
 *
 * @param reason - The reason, or null.
 * @param query - A query to start from.
 * @returns The query.
 */
function breakGlassQuery(reason: string | null, query = new URLSearchParams()): URLSearchParams {
  if (reason !== null) {
    query.set("break_glass_reason", reason);
  }
  return query;
}

/**
 * Drops a table.
 *
 * @param ref - The table.
 * @param options - How to drop it.
 * @param options.purge - Delete its files at once instead of keeping them for a restore.
 * @param options.breakGlassReason - The reason, for a protected environment; null otherwise.
 * @returns Nothing.
 * @throws {ApiError} for a refusal.
 */
export async function dropTable(
  ref: TableRef,
  options: { purge: boolean; breakGlassReason: string | null },
): Promise<void> {
  const query = new URLSearchParams();
  if (options.purge) {
    query.set("purge", "true");
  }
  breakGlassQuery(options.breakGlassReason, query);
  await api.request<undefined>("DELETE", withQuery(tableApiPath(ref), query));
}

/**
 * Restores a dropped table.
 *
 * @param ref - The table, by the name it had.
 * @param options - How to restore it.
 * @param options.newName - Restore it under this name; null keeps the old one.
 * @param options.breakGlassReason - The reason, for a protected environment; null otherwise.
 * @returns The restored table.
 * @throws {ApiError} for a refusal; 409 when a table of that name exists.
 */
export function undropTable(
  ref: TableRef,
  options: { newName: string | null; breakGlassReason: string | null },
): Promise<TableOut> {
  return api.request<TableOut>("POST", tableApiPath(ref, "/undrop"), {
    new_name: options.newName,
    break_glass_reason: options.breakGlassReason,
  });
}

/**
 * Rebuilds a table a deployed project builds: drops it and queues the run that builds it.
 *
 * @param ref - The table.
 * @param reason - Why, for the audit log.
 * @param breakGlass - Whether the environment is protected, so the reason is also the
 *   break-glass reason.
 * @returns The run.
 * @throws {ApiError} for a refusal.
 */
export function rebuildTable(
  ref: TableRef,
  reason: string,
  breakGlass: boolean,
): Promise<TableRebuildOut> {
  const query = breakGlassQuery(breakGlass ? reason : null);
  return api.request<TableRebuildOut>("POST", withQuery(tableApiPath(ref, "/rebuild"), query), {
    reason,
  });
}

/**
 * Queues maintenance for a table.
 *
 * @param ref - The table.
 * @param options - What to keep and delete.
 * @returns The id of the query that does it.
 * @throws {ApiError} for a refusal.
 */
export async function maintainTable(ref: TableRef, options: MaintenanceOptions): Promise<string> {
  const answer = await api.request<{ query_id: string }>(
    "POST",
    tableApiPath(ref, "/maintenance"),
    {
      keep_days: options.keepDays,
      keep_last: options.keepLast,
      dry_run: options.dryRun,
      older_than_hours: options.olderThanHours,
    },
  );
  return answer.query_id;
}

/**
 * Reads a table's retention.
 *
 * @param ref - The table.
 * @returns The retention.
 * @throws {ApiError} for a refusal.
 */
export function getRetention(ref: TableRef): Promise<TableRetentionOut> {
  return api.request<TableRetentionOut>("GET", tableApiPath(ref, "/retention"));
}

/**
 * Sets when a table expires.
 *
 * @param ref - The table.
 * @param rule - After some days, on a day, or never.
 * @returns The retention.
 * @throws {ApiError} for a refusal or a rule the server rejects.
 */
export function setRetention(ref: TableRef, rule: RetentionRule): Promise<TableRetentionOut> {
  return api.request<TableRetentionOut>("PUT", tableApiPath(ref, "/retention"), rule);
}

/**
 * Reads a table's references, in both directions.
 *
 * @param ref - The table.
 * @returns The references.
 * @throws {ApiError} for a refusal.
 */
export function getReferences(ref: TableRef): Promise<TableReferencesOut> {
  return api.request<TableReferencesOut>("GET", tableApiPath(ref, "/references"));
}

/**
 * Drops an artifact.
 *
 * @param ref - The artifact; its `name` is the artifact's name.
 * @param breakGlassReason - The reason, for a protected environment; null otherwise.
 * @returns Nothing.
 * @throws {ApiError} for a refusal.
 */
export async function dropArtifact(ref: TableRef, breakGlassReason: string | null): Promise<void> {
  const namespace = encodeURIComponent(ref.namespace);
  const name = encodeURIComponent(ref.name);
  const path = environmentPath(ref.environment, `/namespaces/${namespace}/artifacts/${name}`);
  await api.request<undefined>("DELETE", withQuery(path, breakGlassQuery(breakGlassReason)));
}

/**
 * Reads whether an environment is protected.
 *
 * @param environment - The environment.
 * @returns Whether it is; null when the person may not read it or it does not exist.
 * @throws {ApiError} for any other failure.
 */
export async function isProtected(environment: string): Promise<boolean | null> {
  try {
    const body = await api.request<{ is_protected: boolean }>("GET", environmentPath(environment));
    return body.is_protected;
  } catch (error) {
    if (error instanceof ApiError && (error.status === 403 || error.status === 404)) {
      return null;
    }
    throw error;
  }
}

/**
 * Lists the dropped tables of an environment, or of one namespace of it.
 *
 * @param environment - The environment.
 * @param namespace - The dotted namespace; null for the whole environment.
 * @param cursor - The cursor of the page, or null for the first.
 * @returns The page; one namespace's list is a single page.
 * @throws {ApiError} for a refusal.
 */
export async function listDropped(
  environment: string,
  namespace: string | null,
  cursor: string | null,
): Promise<CursorPage<DroppedTableOut>> {
  if (namespace !== null) {
    const path = environmentPath(
      environment,
      `/namespaces/${encodeURIComponent(namespace)}/dropped-tables`,
    );
    const items = await api.request<readonly DroppedTableOut[]>("GET", path);
    return { items, nextCursor: null };
  }
  const path = withQuery(environmentPath(environment, "/dropped-tables"), pageQuery(cursor));
  const body = await api.request<{
    items: readonly DroppedTableOut[];
    next_cursor: string | null;
  }>("GET", path);
  return { items: body.items, nextCursor: body.next_cursor };
}

/**
 * Lists the managed tables that expire soon.
 *
 * @param environment - The environment.
 * @param namespace - Only this dotted namespace and below; null for all.
 * @param cursor - The cursor of the page, or null for the first.
 * @returns The page.
 * @throws {ApiError} for a refusal.
 */
export async function listExpiring(
  environment: string,
  namespace: string | null,
  cursor: string | null,
): Promise<CursorPage<TableRetentionOut>> {
  const query = new URLSearchParams();
  if (namespace !== null) {
    query.set("namespace", namespace);
  }
  pageQuery(cursor, query);
  const page = await api.page(withQuery(environmentPath(environment, "/expiring-tables"), query));
  return { items: page.body as readonly TableRetentionOut[], nextCursor: page.nextCursor };
}

/**
 * Lists the tables no project produces any more.
 *
 * @param environment - The environment.
 * @param cursor - The cursor of the page, or null for the first.
 * @returns The page, and how many orphans the person may not read.
 * @throws {ApiError} for a refusal.
 */
export async function listOrphans(
  environment: string,
  cursor: string | null,
): Promise<CursorPage<OrphanOut> & { hidden: number }> {
  const body = await api.request<{
    orphans: readonly OrphanOut[];
    hidden: number;
    next_cursor: string | null;
  }>("GET", withQuery(environmentPath(environment, "/orphans"), pageQuery(cursor)));
  return { items: body.orphans, hidden: body.hidden, nextCursor: body.next_cursor };
}
