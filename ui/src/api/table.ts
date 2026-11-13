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
interface ModelReferenceOut {
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
interface ScopeOut {
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
interface ViaOut {
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
interface PermissionOut {
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
