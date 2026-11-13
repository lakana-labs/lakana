/**
 * The catalog list: the tables a person may read, with their health, owner and use, and the
 * tree of environments and namespaces that narrows them.
 *
 * @packageDocumentation
 */
import { api } from "./client";

/** The columns the list can be sorted by, in the order of the columns. */
export type TableSort =
  "name" | "owner" | "health" | "rows" | "size" | "last_write" | "last_read" | "created";

/** The direction of a sort. */
export type SortOrder = "asc" | "desc";

/** Every sort key, in the order of the columns. */
export const TABLE_SORTS: readonly TableSort[] = [
  "name",
  "owner",
  "health",
  "rows",
  "size",
  "last_write",
  "last_read",
  "created",
];

/** How many rows one page of the list holds. */
const PAGE_SIZE = 50;

/** The worst thing a table's checks found, in one line. */
export interface HealthSummaryOut {
  /** The verdict. */
  readonly state: "failing" | "warning" | "healthy" | "not_judged";
  /** The word's name, such as `check_failed`, `late`, `healthy` or `not_judged`. */
  readonly item: string;
  /** The word as the server writes it, in English. */
  readonly text: string;
  /** How many more items share the verdict. */
  readonly others: number;
}

/** Who owns a table. */
export interface OwnerOut {
  /** The principal's name. */
  readonly name: string;
  /** Whether the owner is a person or a service account. */
  readonly kind: "USER" | "SERVICE_ACCOUNT";
}

/** Why a row matches the search. */
export interface TableMatchOut {
  /** Whose text matched: `table` for the table's own, `column` for one of its columns'. */
  readonly kind: string;
  /** The field that matched: `name`, `label`, `description`, `grain` or `measure`. */
  readonly matched: string;
  /** The column it matched in, if any. */
  readonly column: string | null;
  /** That column's type. */
  readonly column_type: string | null;
  /** A piece of the text around the match. */
  readonly snippet: string | null;
  /** The language of the text that matched. */
  readonly language: string | null;
  /** Whether the text is a translation of the one the author wrote. */
  readonly translated: boolean;
}

/** One row of the list. */
export interface TableListItemOut {
  /** The environment's name. */
  readonly environment: string;
  /** The dotted namespace. */
  readonly namespace: string;
  /** The table's name. */
  readonly name: string;
  /** The table's id. */
  readonly uuid: string;
  /** What the entry is. */
  readonly kind: "TABLE" | "EXTERNAL" | "VIEW" | "ARTIFACT";
  /** The modelling kind, such as `fact`, when the table has one. */
  readonly model_kind: string | null;
  /** Whether the table is published to readers. */
  readonly state: "published" | "not_published" | "kept";
  /** The owner, if the table has one. */
  readonly owner: OwnerOut | null;
  /** When the table was created. */
  readonly created_at: string;
  /** The rows in the newest snapshot, when known. */
  readonly row_count: number | null;
  /** The bytes of its data files, when known. */
  readonly size_bytes: number | null;
  /** When it was last written. */
  readonly last_write_at: string | null;
  /** When it was last read. */
  readonly last_read_at: string | null;
  /** How many people read it in the last seven days. */
  readonly readers_7d: number;
  /** The worst thing its checks found. */
  readonly health_summary: HealthSummaryOut;
  /** Why it matches the search; null without a search. */
  readonly match: TableMatchOut | null;
}

/** One page of the list, as `GET /api/v1/tables` answers it. */
export interface TableListPageOut {
  /** The rows. */
  readonly items: readonly TableListItemOut[];
  /** Where the next page starts; null on the last page. */
  readonly next_cursor: string | null;
  /** Whether the server stopped looking before it had seen every table. */
  readonly capped: boolean;
}

/** The counts of readable tables, as `GET /api/v1/tables/tree` answers them. */
export interface TableTreeOut {
  /** The readable tables in every environment. */
  readonly count: number;
  /** The environments shown, by name. */
  readonly environments: readonly {
    readonly name: string;
    readonly count: number;
    readonly namespaces: readonly { readonly path: string; readonly count: number }[];
  }[];
}

/** What to ask the list for. */
export interface ListTablesQuery {
  /** Only this environment; null for all. */
  readonly environment: string | null;
  /** Only this dotted namespace and below; null for all. */
  readonly namespace: string | null;
  /** The search text; empty for none. */
  readonly q: string;
  /** The column to sort by. */
  readonly sort: TableSort;
  /** The direction. */
  readonly order: SortOrder;
  /** Where the page starts; null for the first. */
  readonly cursor: string | null;
}

/**
 * Reads one page of the list.
 *
 * @param query - The filters, the sort and the cursor.
 * @returns The page.
 * @throws {ApiError} for a refusal.
 */
export function listTables(query: ListTablesQuery): Promise<TableListPageOut> {
  const params = new URLSearchParams();
  const optional: readonly (readonly [string, string | null])[] = [
    ["environment", query.environment],
    ["namespace", query.namespace],
    ["q", query.q],
    ["sort", query.sort],
    ["order", query.order],
    ["limit", String(PAGE_SIZE)],
    ["cursor", query.cursor],
  ];
  for (const [key, value] of optional) {
    if (value !== null && value !== "") {
      params.set(key, value);
    }
  }
  return api.request<TableListPageOut>("GET", `/api/v1/tables?${params.toString()}`);
}

/**
 * Reads the tree of environments and namespaces with the number of tables in each.
 *
 * @returns The tree.
 * @throws {ApiError} for a refusal.
 */
export function getTableTree(): Promise<TableTreeOut> {
  return api.request<TableTreeOut>("GET", "/api/v1/tables/tree");
}
