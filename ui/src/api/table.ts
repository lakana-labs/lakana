/**
 * One table, as the table page reads it: its columns, model, facts and health, and what the
 * person may do with it.
 *
 * @packageDocumentation
 */
import { api } from "./client";
import type { HealthSummaryOut, OwnerOut } from "./catalog";

/** Where a table is: the environment, the dotted namespace and the name. */
export interface TableRef {
  /** The environment's name. */
  readonly environment: string;
  /** The dotted namespace. */
  readonly namespace: string;
  /** The table's name. */
  readonly name: string;
}

/** Where a text of a table or column comes from. */
type TextSource = "project" | "edited";

/** A glossary term a table or column links to. */
interface TermRefOut {
  /** The term's id. */
  readonly key: string;
  /** The term's name in the reader's language. */
  readonly name: string;
}

/** One column of a table. */
export interface ColumnOut {
  /** The column's name. */
  readonly name: string;
  /** The Iceberg type in its JSON form, such as `long` or `decimal(10,2)`. */
  readonly type: string;
  /** Whether the column accepts nulls. */
  readonly nullable: boolean;
  /** The column's description, or null. */
  readonly description: string | null;
  /** Whether the description is the project's text or an edit; null without one. */
  readonly description_source: TextSource | null;
  /** The column's short name for people, or null. */
  readonly label: string | null;
  /** Whether the label is the project's text or an edit; null without one. */
  readonly label_source: TextSource | null;
  /** The label's language; null exactly when there is no label. */
  readonly label_language: string | null;
  /** The description's language; null exactly when there is no description. */
  readonly description_language: string | null;
  /** The glossary terms that link the column. */
  readonly terms: readonly TermRefOut[];
}

/** One reference a table's header declares. */
export interface ModelReferenceOut {
  /** The name of the reference's generated check. */
  readonly id: string;
  /** The dotted name of the table that declares it. */
  readonly table: string;
  /** The referencing column. */
  readonly column: string;
  /** The dotted name of the table it points at. */
  readonly target: string;
  /** The column of the target it must match. */
  readonly target_column: string;
  /** Whether a table of that name exists now. */
  readonly target_exists: boolean;
  /** The referencing table's time column when the target keeps history; else null. */
  readonly at: string | null;
}

/** One measure a table's header declares. */
interface MeasureOut {
  /** The measure's id. */
  readonly id: string;
  /** The measure's column. */
  readonly column: string;
  /** `sum`, `count`, `min`, `max`, `avg`, `count_distinct` or `none`. */
  readonly aggregation: string;
  /** `additive`, `semi` or `non`. */
  readonly additivity: string;
  /** The columns a `semi` measure must not be summed over. */
  readonly not_over: readonly string[];
  /** The unit, or null when it has a currency. */
  readonly unit: string | null;
  /** The ISO 4217 currency, or null when it has a unit. */
  readonly currency: string | null;
  /** A short name, or null. */
  readonly label: string | null;
  /** What it means, or null. */
  readonly description: string | null;
  /** The label's language. */
  readonly label_language?: string | null;
  /** The description's language. */
  readonly description_language?: string | null;
}

/** The newest result of one check of a table. */
interface CheckHealthOut {
  /** The check's name. */
  readonly name: string;
  /** The check's kind. */
  readonly check: string;
  /** The run that produced the result. */
  readonly run_id: string;
  /** How it ended, such as `passed`, `failed`, `warned` or `errored`. */
  readonly outcome: string;
  /** What a failure does. */
  readonly on_failure: string;
  /** How many rows broke it; null when not counted. */
  readonly failing_rows: number | null;
  /** Why it could not run, or null. */
  readonly error: string | null;
  /** What happened to the table after a failure, or null. */
  readonly rollback_outcome: string | null;
  /** Whether the table was kept. */
  readonly kept: boolean;
  /** Whether the table was not published. */
  readonly not_published: boolean;
  /** When the run finished. */
  readonly finished_at: string;
  /** Whether the check was generated from the header. */
  readonly generated?: boolean;
}

/** The model a table's asset header declares. */
interface TableModelOut {
  /** `fact`, `dimension`, `wide` or `staging`; null when the header names none. */
  readonly kind: string | null;
  /** What one row is, in one sentence; null when the header says nothing. */
  readonly grain: string | null;
  /** The columns that identify one row. */
  readonly key: readonly string[];
  /** Whether the header says `key: none`. */
  readonly key_none: boolean;
  /** The references whose target the caller may read. */
  readonly references: readonly ModelReferenceOut[];
  /** How many references point at a table the caller may not read. */
  readonly hidden_references: number;
  /** Every measure the header declares. */
  readonly measures: readonly MeasureOut[];
  /** The newest result of each check the header generates. */
  readonly checks: readonly CheckHealthOut[];
  /** The grain's language. */
  readonly grain_language?: string | null;
}

/** How many of a table's main-language texts one language has. */
interface TranslationCoverageOut {
  /** The language. */
  readonly language: string;
  /** Texts it has. */
  readonly translated: number;
  /** Texts the table shows in its main language. */
  readonly total: number;
}

/** A table, as `GET .../tables/{name}` answers it; `schema` is the column list. */
export interface TableOut {
  /** The table's name. */
  readonly name: string;
  /** The dotted namespace. */
  readonly namespace: string;
  /** The table's id. */
  readonly uuid: string;
  /** What the entry is. */
  readonly kind: "TABLE" | "EXTERNAL" | "VIEW" | "ARTIFACT";
  /** Where the Iceberg metadata is; null for a view. */
  readonly metadata_location: string | null;
  /** The columns, in order. */
  readonly schema: readonly ColumnOut[];
  /** A view's SQL; null otherwise. */
  readonly sql: string | null;
  /** The tables a view reads. */
  readonly references: readonly { readonly id: string; readonly name: string }[] | null;
  /** How many tables a view reads that the caller may not. */
  readonly hidden_references: number;
  /** The newest snapshot's id; null without one. */
  readonly snapshot_id: number | null;
  /** Whether the table is published to readers. */
  readonly state: "published" | "not_published" | "kept";
  /** Why it is not published or kept; null otherwise. */
  readonly state_message: string | null;
  /** The table's description, or null. */
  readonly description: string | null;
  /** Whether the description is the project's text or an edit. */
  readonly description_source: TextSource | null;
  /** When it was last written. */
  readonly last_write_at: string | null;
  /** The rows in the newest snapshot, when known. */
  readonly row_count: number | null;
  /** The bytes of its data files, when known. */
  readonly size_bytes: number | null;
  /** How many data files it has, when known. */
  readonly file_count: number | null;
  /** When an artifact expires; null otherwise. */
  readonly expires_at: string | null;
  /** Whether an artifact expires soon. */
  readonly expires_soon: boolean;
  /** The model the header declares; null without one. */
  readonly model: TableModelOut | null;
  /** The table's short name for people, or null. */
  readonly label: string | null;
  /** Whether the label is the project's text or an edit. */
  readonly label_source: TextSource | null;
  /** The label's language. */
  readonly label_language: string | null;
  /** The description's language. */
  readonly description_language: string | null;
  /** The reader's language. */
  readonly language: string;
  /** The language the table's texts are written in. */
  readonly main_language: string;
  /** How much of the table's text each language has. */
  readonly translations: readonly TranslationCoverageOut[];
  /** The glossary terms that link the table. */
  readonly terms: readonly TermRefOut[];
  /** The health items by name, as the server words them. */
  readonly health: Readonly<Record<string, string>>;
  /** The health items the caller may not see. */
  readonly health_missing: readonly string[];
  /** The health items that were never judged. */
  readonly health_never_judged: Readonly<Record<string, string>>;
  /** The owner, if the table has one. */
  readonly owner: OwnerOut | null;
  /** When the table was created. */
  readonly created_at: string | null;
  /** When it was last read. */
  readonly last_read_at: string | null;
  /** How many people read it in the last seven days. */
  readonly readers_7d: number;
  /** The worst thing its checks found. */
  readonly health_summary: HealthSummaryOut | null;
}

/** The object a permission is on. */
export interface ScopeOut {
  /** `INSTANCE`, `ENVIRONMENT`, `NAMESPACE` or `TABLE`. */
  readonly kind: "INSTANCE" | "ENVIRONMENT" | "NAMESPACE" | "TABLE";
  /** The environment. */
  readonly environment: string | null;
  /** The dotted namespace. */
  readonly namespace: string | null;
  /** The table. */
  readonly table: string | null;
}

/** One grant that gives a permission, and how it reaches the person. */
export interface ViaOut {
  /** `grant`, `role` or `group`. */
  readonly kind: "grant" | "role" | "group";
  /** The grant's id. */
  readonly grant_id: string;
  /** The group's name, for `group`. */
  readonly group: string | null;
  /** The role granted, or null for a single action. */
  readonly role: "VIEWER" | "ANALYST" | "DATA_SCIENTIST" | "DATA_ENGINEER" | "ADMIN" | null;
  /** What the grant is on. */
  readonly scope: ScopeOut;
}

/** Whether a person may do one action on the object, and why. */
export interface PermissionOut {
  /** The action, such as `DROP_TABLE` or `SUBMIT_QUERY`. */
  readonly action: string;
  /** The answer. */
  readonly allowed: boolean;
  /** The grants that give it; empty when refused. */
  readonly via: readonly ViaOut[];
  /** Why it is refused; null when allowed. */
  readonly reason: string | null;
}

/** A person's effective permissions on one object. */
export interface PermissionsOut {
  /** The person's name. */
  readonly principal: string;
  /** Whether the person may sign in. */
  readonly active: boolean;
  /** The object asked about. */
  readonly scope: ScopeOut;
  /** One per action asked for on an object of this kind. */
  readonly permissions: readonly PermissionOut[];
  /** Set for a deactivated person. */
  readonly note: string | null;
}

/**
 * The API path of a table or one of its parts.
 *
 * @param ref - The table.
 * @param suffix - What follows the table's name, such as `/health`.
 * @returns The path.
 */
export function tableApiPath(ref: TableRef, suffix = ""): string {
  const environment = encodeURIComponent(ref.environment);
  const namespace = encodeURIComponent(ref.namespace);
  const name = encodeURIComponent(ref.name);
  return `/api/v1/environments/${environment}/namespaces/${namespace}/tables/${name}${suffix}`;
}

/**
 * Reads a table.
 *
 * @param ref - The table.
 * @returns The table.
 * @throws {ApiError} for a refusal; 404 when there is none that the person may read.
 */
export function getTable(ref: TableRef): Promise<TableOut> {
  return api.request<TableOut>("GET", tableApiPath(ref));
}

/**
 * Asks what a person may do on a table.
 *
 * @param person - The person's principal name.
 * @param scope - The object: an environment, a namespace in it, or a table.
 * @param scope.environment - The environment.
 * @param scope.namespace - The dotted namespace.
 * @param scope.table - The table.
 * @returns The answer, action by action.
 * @throws {ApiError} for a refusal.
 */
export function myPermissions(
  person: string,
  scope: { environment: string; namespace?: string; table?: string },
): Promise<PermissionsOut> {
  const query = new URLSearchParams({ environment: scope.environment });
  for (const key of ["namespace", "table"] as const) {
    const value = scope[key];
    if (value !== undefined) {
      query.set(key, value);
    }
  }
  return api.request<PermissionsOut>(
    "GET",
    `/api/v1/principals/${encodeURIComponent(person)}/permissions?${query.toString()}`,
  );
}

/** The person or service account a grant, row filter or column mask is for. */
interface SubjectPrincipalOut {
  /** The principal's id. */
  readonly id: string;
  /** The principal's name. */
  readonly name: string;
  /** `user` for a person, `service` for a service account. */
  readonly principal_type: "user" | "service";
}

/** Who a grant, row filter or column mask is for, as the server names them. */
interface SubjectOut {
  /** The principal's id; null for a group's. */
  readonly principal_id: string | null;
  /** The group's id; null for a principal's. */
  readonly group_id: string | null;
  /** The principal by name; null for a group's, or for a principal deleted while it was read. */
  readonly principal: SubjectPrincipalOut | null;
  /** The group's name; null for a principal's. */
  readonly group: string | null;
}

/** A grant, as `GET /api/v1/grants` answers it. */
export interface GrantOut extends SubjectOut {
  /** The grant's id. */
  readonly id: string;
  /** The role granted, or null for a single action. */
  readonly role: ViaOut["role"];
  /** The single action granted, or null for a role. */
  readonly action: string | null;
  /** The level the grant is on. */
  readonly kind: ScopeOut["kind"];
  /** The environment it is on, if any. */
  readonly environment: string | null;
  /** The dotted namespace it is on, if any. */
  readonly namespace: string | null;
  /** The table it is on, if any. */
  readonly table: string | null;
  /** The id of the principal who granted it. */
  readonly granted_by: string;
  /** When it was granted. */
  readonly granted_at: string;
  /** Why a single action has no effect, or null. */
  readonly note: string | null;
}

/** What a row filter and a column mask share, as the server answers them. */
interface PolicyOut extends SubjectOut {
  /** The policy's id. */
  readonly id: string;
  /** The environment of its table. */
  readonly environment: string;
  /** The dotted namespace of its table. */
  readonly namespace: string;
  /** Its table. */
  readonly table: string;
  /** The SQL expression, in the table's current column names. */
  readonly expression: string;
  /** The id of the principal who created it. */
  readonly created_by: string;
  /** When it was created. */
  readonly created_at: string;
  /** Whether it follows its columns by field id; not shown. */
  readonly bound: boolean;
}

/** A row filter, as `GET /api/v1/row-filters` answers it: the rows its subject may see. */
export type RowFilterOut = PolicyOut;

/** A column mask, as `GET /api/v1/column-masks` answers it: what its subject sees instead. */
export interface ColumnMaskOut extends PolicyOut {
  /** The masked column, as the table names it now. */
  readonly column: string;
}

/** The most grants one level of the Access tab reads; the server's largest page. */
const MOST_GRANTS = 200;

/** One level of the object hierarchy, matched exactly by the grants listing. */
export interface GrantLevel {
  /** The environment. */
  readonly environment: string;
  /** The dotted namespace; null for the environment itself. */
  readonly namespace: string | null;
  /** The table; null for a namespace or the environment. */
  readonly table: string | null;
}

/** The first page of the grants on one level. */
interface GrantPage {
  /** The grants, newest first. */
  readonly grants: readonly GrantOut[];
  /** Whether the level has more than this page. */
  readonly more: boolean;
}

/**
 * Lists the grants on exactly one object: a table, a namespace or an environment. Only an
 * administrator may list grants.
 *
 * @param level - The object.
 * @returns The first {@link MOST_GRANTS} grants on it, newest first, and whether the server has
 *   more (it names a next page).
 * @throws {ApiError} for a refusal.
 */
export async function listGrantsAt(level: GrantLevel): Promise<GrantPage> {
  const query = new URLSearchParams({ environment: level.environment });
  if (level.namespace !== null) {
    query.set("namespace", level.namespace);
    if (level.table !== null) {
      query.set("table", level.table);
    }
  }
  query.set("limit", String(MOST_GRANTS));
  const page = await api.page(`/api/v1/grants?${query.toString()}`);
  return { grants: page.body as readonly GrantOut[], more: page.nextCursor !== null };
}

/**
 * The query that names one table to the policy listings.
 *
 * @param ref - The table.
 * @returns The query, without its `?`.
 */
function tableQuery(ref: TableRef): string {
  return new URLSearchParams({
    environment: ref.environment,
    namespace: ref.namespace,
    table: ref.name,
  }).toString();
}

/**
 * Lists a table's row filters. Needs the right to manage policies on the instance.
 *
 * @param ref - The table.
 * @returns The row filters.
 * @throws {ApiError} for a refusal.
 */
export function listRowFilters(ref: TableRef): Promise<readonly RowFilterOut[]> {
  return api.request<readonly RowFilterOut[]>("GET", `/api/v1/row-filters?${tableQuery(ref)}`);
}

/**
 * Lists a table's column masks. Needs the right to manage policies on the instance.
 *
 * @param ref - The table.
 * @returns The column masks.
 * @throws {ApiError} for a refusal.
 */
export function listColumnMasks(ref: TableRef): Promise<readonly ColumnMaskOut[]> {
  return api.request<readonly ColumnMaskOut[]>("GET", `/api/v1/column-masks?${tableQuery(ref)}`);
}

/** One health signal of a table, as the caller may see it. */
interface HealthSignalOut {
  /** The observation's id. */
  readonly id: string;
  /** `freshness`, `volume`, `null_rate`, `range` or `schema`. */
  readonly signal: string;
  /** The column, for a column signal. */
  readonly column: string | null;
  /** `ok`, `fired`, `no_history`, `no_metrics` or `off`. */
  readonly state: string;
  /** The effect that applied; `block` stops a publication. */
  readonly effect: string;
  /** The snapshot observed. */
  readonly snapshot_id: number | null;
  /** When it was recorded. */
  readonly observed_at: string;
  /** What was observed; null when hidden. */
  readonly observed: Readonly<Record<string, unknown>> | null;
  /** The usual band it was judged against; null when hidden or none. */
  readonly band: Readonly<Record<string, unknown>> | null;
  /** The server's sentence for a fired signal; null when it did not fire or its values are hidden. */
  readonly alert: string | null;
  /** Whether a column mask or row filter left out `observed`, `band` and `alert`. */
  readonly values_hidden: boolean;
  /** When it last fired; only in the current state. */
  readonly last_fired_at?: string | null;
  /** Whether a failed check set the observed snapshot back. */
  readonly set_back?: boolean;
}

/** A person's use of a table over the window. */
interface ReaderOut {
  /** The person's name. */
  readonly principal: string;
  /** How many of their queries named the table. */
  readonly read_count: number;
  /** When the last of them completed. */
  readonly last_read_at: string;
}

/** The last maintenance run that changed a table. */
interface LastMaintenanceOut {
  /** The query run that did it. */
  readonly run_id: string;
  /** When it ended. */
  readonly finished_at: string;
  /** `complete`, or `stopped` at a safe point. */
  readonly status: "complete" | "stopped";
  /** Snapshots it expired. */
  readonly snapshots_expired: number;
  /** Orphaned files it deleted. */
  readonly files_deleted: number;
  /** Their total size. */
  readonly bytes_deleted: number;
  /** Tags of failed checks it removed. */
  readonly tags_removed: number;
}

/** A table's health at a glance, as `GET .../tables/{name}/health` answers it. */
export interface TableHealthOut {
  /** The window the readers cover. */
  readonly days: number;
  /** The last maintenance that changed the table; null if there is none. */
  readonly last_maintenance: LastMaintenanceOut | null;
  /** The newest result of each check of the table. */
  readonly checks: readonly CheckHealthOut[];
  /** Who read the table, most reads first; null when left out or not visible to the caller. */
  readonly readers: readonly ReaderOut[] | null;
  /** How many results were left out of `checks`. */
  readonly hidden_checks: number;
  /** The current state of each health signal. */
  readonly signals: readonly HealthSignalOut[];
}

/**
 * Reads a table's health for the last seven days.
 *
 * @param ref - The table.
 * @param options - What to ask for.
 * @param options.readers - Whether to ask for the readers; false sends `readers=false`, so the
 *   server looks up no names and writes no audit entry.
 * @returns The health.
 * @throws {ApiError} for a refusal.
 */
export function getTableHealth(
  ref: TableRef,
  options: { readonly readers: boolean },
): Promise<TableHealthOut> {
  const query = options.readers ? "?days=7" : "?days=7&readers=false";
  return api.request<TableHealthOut>("GET", tableApiPath(ref, `/health${query}`));
}

/** A node of the metadata graph, with the attributes the caller may see. */
export interface GraphNodeOut {
  /** Its stable id, such as `table:<uuid>`. */
  readonly id: string;
  /** `table`, `job`, `connection` and others. */
  readonly kind: string;
  /** A name to read: a dotted table name for a table. */
  readonly name: string;
  /** Steps from the start. */
  readonly distance: number;
  /** A table's `managed`, `external`, `view` or `artifact`. */
  readonly table_type?: string | null;
  /** A table's declared modelling kind. */
  readonly model_kind?: string | null;
  /** A table's label, in the reader's language. */
  readonly label?: string | null;
  /** A connection's kind. */
  readonly connection_kind?: string | null;
  /** The repository that declares an asset or a job, or builds a table. */
  readonly repository?: string | null;
}

/** An edge of the metadata graph, from `source` to `target`. */
interface GraphEdgeOut {
  /** `reads`, `builds`, `references` and others. */
  readonly kind: string;
  /** The node it leaves. */
  readonly source: string;
  /** The node it reaches. */
  readonly target: string;
}

/** One page of a metadata graph answer. */
export interface GraphOut {
  /** The nodes; the start is the first node of the first page. */
  readonly nodes: readonly GraphNodeOut[];
  /** The edges whose later endpoint is on this page. */
  readonly edges: readonly GraphEdgeOut[];
  /** Where the next page starts; null on the last. */
  readonly next_cursor: string | null;
}

/**
 * Reads what feeds a node, or what it feeds, a few steps out.
 *
 * @param environment - The environment's name.
 * @param node - The node's id, such as `table:<uuid>`.
 * @param direction - `up` for what feeds it, `down` for what it feeds.
 * @param depth - How many assets to cross, from 1 to 10.
 * @returns The first page, at most 500 nodes.
 * @throws {ApiError} for a refusal.
 */
export function getLineage(
  environment: string,
  node: string,
  direction: "up" | "down",
  depth: number,
): Promise<GraphOut> {
  const query = new URLSearchParams({ node, direction, depth: String(depth), limit: "500" });
  return api.request<GraphOut>(
    "GET",
    `/api/v1/environments/${encodeURIComponent(environment)}/graph/lineage?${query.toString()}`,
  );
}

/** A change to the texts of a table or column: a field left out stays, `null` removes its edit. */
export interface TextChange {
  /** The new description (1 to 4,000 characters), or null to go back to the project's text. */
  readonly description?: string | null;
  /** The new label (one line, 1 to 100 characters), or null to go back to the project's text. */
  readonly label?: string | null;
}

/**
 * Sets or resets a table's description and label in one language.
 *
 * @param ref - The table.
 * @param change - The fields to change.
 * @param lang - The language of the texts.
 * @returns The table as the server answers it after the change.
 * @throws {ApiError} for a refusal: 403 for a person who may not edit, 422 for a text that breaks
 *   a rule.
 */
export function describeTable(ref: TableRef, change: TextChange, lang: string): Promise<TableOut> {
  return api.request<TableOut>(
    "PATCH",
    tableApiPath(ref, `?lang=${encodeURIComponent(lang)}`),
    change,
  );
}

/**
 * Sets or resets a column's description and label in one language.
 *
 * @param ref - The table.
 * @param column - The column's name.
 * @param change - The fields to change.
 * @param lang - The language of the texts.
 * @returns The table as the server answers it after the change.
 * @throws {ApiError} for a refusal: 403 for a person who may not edit, 422 for a text that breaks
 *   a rule.
 */
export function describeColumn(
  ref: TableRef,
  column: string,
  change: TextChange,
  lang: string,
): Promise<TableOut> {
  const path = tableApiPath(
    ref,
    `/columns/${encodeURIComponent(column)}/description?lang=${encodeURIComponent(lang)}`,
  );
  return api.request<TableOut>("PUT", path, change);
}

/**
 * Where the environment's current deployment defines a table, as `GET .../definition` answers
 * it. Every field but `defined` is null for a table no current deployment defines.
 */
export interface TableDefinitionOut {
  /** Whether a current deployment defines the table. */
  readonly defined: boolean;
  /** The repository's name. */
  readonly repository: string | null;
  /** The file's path in the repository. */
  readonly path: string | null;
  /** The deployed commit, never the branch head. */
  readonly commit: string | null;
  /** The line the file defines the table on. */
  readonly line: number | null;
  /** The deployed project version. */
  readonly project_version_id: string | null;
  /** When that version was first recorded in the environment. */
  readonly deployed_at: string | null;
  /** The server's HTTPS link to the line on the git host, or null when it builds none. */
  readonly browse_url: string | null;
}

/**
 * A table's definition with the file's text, or the reason the text is not returned, as
 * `GET .../definition/file` answers it.
 */
export interface TableDefinitionFileOut extends TableDefinitionOut {
  /** The file's text (UTF-8, line-feed line endings), or null. */
  readonly content: string | null;
  /** The size of the text's UTF-8 encoding, set only with `content`. */
  readonly bytes: number | null;
  /** Why `content` is null; null with content and for a table no deployment defines. */
  readonly reason: "restricted" | "not_recorded" | "too_large" | null;
  /** The server's sentence for `reason`, naming no table, or null. */
  readonly message: string | null;
}

/**
 * Reads where a table is defined: repository, path, line, commit and the server's link.
 *
 * @param ref - The table.
 * @returns The location, or `defined: false`.
 * @throws {ApiError} for a refusal; 403 alike for a table the person may not read and one that
 *   does not exist.
 */
export function getDefinition(ref: TableRef): Promise<TableDefinitionOut> {
  return api.request<TableDefinitionOut>("GET", tableApiPath(ref, "/definition"));
}

/**
 * Reads the file that defines a table. The server records each read with a text in the audit
 * log, so call it only when the person asks to see the file.
 *
 * @param ref - The table.
 * @returns The location with the text, or with the reason the text is not returned.
 * @throws {ApiError} for a refusal; 503 when the read could not be recorded, and then no text.
 */
export function getDefinitionFile(ref: TableRef): Promise<TableDefinitionFileOut> {
  return api.request<TableDefinitionFileOut>("GET", tableApiPath(ref, "/definition/file"));
}

/** The states of a query run, in the server's upper case (`models/query_run.py`). */
export type QueryRunStatus = "QUEUED" | "RUNNING" | "SUCCEEDED" | "FAILED" | "CANCELLED";

/** The states of a run that has not ended: leaving it would leave it waiting or running. */
export const ACTIVE_STATUSES: readonly QueryRunStatus[] = ["QUEUED", "RUNNING"];

/** A query run's status, as `GET .../queries/{id}` answers it (the fields the interface reads). */
export interface QueryStatusOut {
  /** The query's id. */
  readonly query_id: string;
  /** Its state. */
  readonly status: QueryRunStatus;
  /** Why a `FAILED` or `CANCELLED` run ended, in the server's words; else null. */
  readonly error_message?: string | null;
  /** The cause of a `FAILED` or `CANCELLED` run, such as `timeout`; else null. */
  readonly error_cause?: string | null;
}

/** One column of a JSON result page. */
interface ResultColumnOut {
  /** The column's name. */
  readonly name: string;
  /** The column's Arrow type name, such as `int64`, `string` or `binary`, cut to 256 characters. */
  readonly type: string;
  /** True when the type name was cut; absent otherwise. */
  readonly type_cut?: boolean | null;
}

/** A column a mask was applied to for the reader, as the worker recorded it. */
export interface MaskedColumnOut {
  /** The masked table's dotted name: for a view, the table the view reads. */
  readonly table: string;
  /** The column, named as in the read. */
  readonly column: string;
}

/** The table a preview showed. */
interface PreviewOut {
  /** The dotted name, cut to 1,024 characters. */
  readonly table: string;
  /** True when the name was cut; absent otherwise. */
  readonly cut?: boolean | null;
}

/**
 * One page of a query result as JSON (`GET .../result?format=json`). The server bounds it: at
 * most 4 MiB, each cell at most 64 KiB; a cut cell is `{"cut": true}`, or `{"cut": true, "text":
 * <prefix>}` for a string.
 */
export interface ResultPageOut {
  /** The result's columns, in order. */
  readonly columns: readonly ResultColumnOut[];
  /** How many columns, or cells of a cut row, were left out because the page was full. */
  readonly columns_cut: number;
  /** The columns masked for the reader; `[]` for none, null when not recorded. */
  readonly masked_columns: readonly MaskedColumnOut[] | null;
  /** How many further masked columns were left out because the page was full. */
  readonly masked_columns_cut: number;
  /**
   * The rows, each a list of cells in column order: 64-bit integers and decimals as strings,
   * binary as `{"binary": <bytes>}`, nested values as JSON.
   */
  readonly rows: readonly (readonly unknown[])[];
  /** The positions on this page of the rows that were cut inside. */
  readonly row_cut: readonly number[];
  /** The rows the result has, at most 100 for a preview. */
  readonly row_count: number;
  /** The offset of the next page, or null at the end. */
  readonly next_offset: number | null;
  /** How long the query ran, or null when unknown. */
  readonly duration_ms: number | null;
  /** The previewed table for a preview, null for any other query. */
  readonly preview: PreviewOut | null;
}

/**
 * The API path of an environment's query run or one of its parts.
 *
 * @param environment - The environment's name.
 * @param id - The query's id.
 * @param suffix - What follows the id, such as `/cancel`.
 * @returns The path.
 */
function queryPath(environment: string, id: string, suffix = ""): string {
  return `/api/v1/environments/${encodeURIComponent(environment)}/queries/${encodeURIComponent(id)}${suffix}`;
}

/**
 * Queues a preview of a table or view: its first 100 rows, read as the person. The server writes
 * the query and its limit; the request carries neither.
 *
 * @param ref - The table.
 * @returns The id of the preview's query run.
 * @throws {ApiError} for a refusal: 403 without the right to run queries (or to read the table),
 *   409 for an artifact.
 */
export async function submitPreview(ref: TableRef): Promise<string> {
  const answer = await api.request<{ readonly query_id: string }>(
    "POST",
    tableApiPath(ref, "/preview"),
  );
  return answer.query_id;
}

/**
 * Reads a query run's state; a poll, so it never counts as the person's activity.
 *
 * @param environment - The environment's name.
 * @param id - The query's id.
 * @returns The state.
 * @throws {ApiError} for a refusal.
 */
export function getQueryStatus(environment: string, id: string): Promise<QueryStatusOut> {
  return api.request<QueryStatusOut>("GET", queryPath(environment, id), undefined, {
    background: true,
  });
}

/**
 * Reads the first page of a finished run as bounded JSON, at most 100 rows.
 *
 * @param environment - The environment's name.
 * @param id - The query's id.
 * @returns The page.
 * @throws {ApiError} for a refusal; 503 with `Retry-After` while the server converts other pages.
 */
export function getResultPage(environment: string, id: string): Promise<ResultPageOut> {
  return api.request<ResultPageOut>(
    "GET",
    queryPath(environment, id, "/result?format=json&limit=100"),
    undefined,
    { background: true },
  );
}

/**
 * Cancels a query run that has not ended.
 *
 * @param environment - The environment's name.
 * @param id - The query's id.
 * @param options - What the request needs besides the cancel.
 * @param options.keepalive - Lets the request finish after the page is gone (on `pagehide`).
 * @throws {ApiError} for a refusal; 409 for a run that has already ended.
 */
export async function cancelQuery(
  environment: string,
  id: string,
  options: { readonly keepalive?: boolean } = {},
): Promise<void> {
  await api.request<unknown>("POST", queryPath(environment, id, "/cancel"), undefined, {
    background: true,
    ...options,
  });
}
