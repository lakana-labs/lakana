/**
 * Row filters and column masks: every one, filtered and paged, and their creation and deletion.
 *
 * @packageDocumentation
 */
import { api } from "./client";
import type { GrantWho } from "./grants";
import type { CursorPage } from "./manage";
import { type ListSortChoice, setSort } from "./sorting";
import { guarded } from "./stepUpFirst";
import type { ColumnMaskOut, RowFilterOut } from "./table";

/** The two kinds of policy: which rows someone sees, and what they see of a column. */
export type PolicyKind = "row-filter" | "column-mask";

/** The filters of the policy lists; each null for any. */
export interface PolicyFilter {
  /** Only the policies made to this principal or group. */
  readonly who: GrantWho | null;
  /** Only the policies in this environment. */
  readonly environment: string | null;
  /** Only the policies on tables of this dotted namespace; needs the environment. */
  readonly namespace: string | null;
  /** Only the policies on this table; needs the namespace. */
  readonly table: string | null;
}

/** The filter that lists every policy. */
export const NO_POLICY_FILTER: PolicyFilter = {
  who: null,
  environment: null,
  namespace: null,
  table: null,
};

/** How many policies a page of a list holds. */
const POLICIES_PAGE = 50;

/**
 * Writes a filter as the server's query.
 *
 * @param filter - The filter.
 * @param limit - The page size.
 * @returns The query, with the page size.
 */
export function policyQuery(filter: PolicyFilter, limit: number): URLSearchParams {
  const query = new URLSearchParams();
  const { who, environment, namespace, table } = filter;
  if (who !== null) {
    query.set(who.kind === "principal" ? "principal_name" : "group_name", who.name);
  }
  for (const [name, value] of [
    ["environment", environment],
    ["namespace", namespace],
    ["table", table],
  ] as const) {
    if (value !== null) {
      query.set(name, value);
    }
  }
  query.set("limit", String(limit));
  return query;
}

/**
 * Lists one page of a kind of policy, newest first.
 *
 * @template T - The answer's item.
 * @param path - The list's path.
 * @param filter - The filter.
 * @param cursor - The cursor of the page; null for the first.
 * @param limit - The page size.
 * @param sort - The sort; null for the newest first.
 * @returns The page and the cursor of the next one.
 */
async function listPolicyPage<T>(
  path: string,
  filter: PolicyFilter,
  cursor: string | null,
  limit: number,
  sort: ListSortChoice | null,
): Promise<CursorPage<T>> {
  const query = policyQuery(filter, limit);
  setSort(query, sort);
  if (cursor !== null) {
    query.set("cursor", cursor);
  }
  const page = await api.page(`${path}?${query.toString()}`);
  return { items: page.body as readonly T[], nextCursor: page.nextCursor };
}

/**
 * Lists one page of row filters, newest first or in a sort. Needs `MANAGE_POLICY` on the instance.
 *
 * @param filter - The filter.
 * @param cursor - The cursor of the page; null for the first.
 * @param limit - The page size.
 * @param sort - The sort; null for the newest first.
 * @returns The page and the cursor of the next one.
 * @throws {ApiError} for a refusal, such as 404 for an unknown principal or group.
 */
export function listRowFilterPage(
  filter: PolicyFilter,
  cursor: string | null,
  limit = POLICIES_PAGE,
  sort: ListSortChoice | null = null,
): Promise<CursorPage<RowFilterOut>> {
  return listPolicyPage<RowFilterOut>("/api/v1/row-filters", filter, cursor, limit, sort);
}

/**
 * Lists one page of column masks, newest first or in a sort. Needs `MANAGE_POLICY` on the instance.
 *
 * @param filter - The filter.
 * @param cursor - The cursor of the page; null for the first.
 * @param limit - The page size.
 * @param sort - The sort; null for the newest first.
 * @returns The page and the cursor of the next one.
 * @throws {ApiError} for a refusal, such as 404 for an unknown principal or group.
 */
export function listColumnMaskPage(
  filter: PolicyFilter,
  cursor: string | null,
  limit = POLICIES_PAGE,
  sort: ListSortChoice | null = null,
): Promise<CursorPage<ColumnMaskOut>> {
  return listPolicyPage<ColumnMaskOut>("/api/v1/column-masks", filter, cursor, limit, sort);
}

/** A new row filter, as `POST /api/v1/row-filters` takes it. */
export interface RowFilterCreateIn {
  /** The principal it is for, or null for a group. */
  readonly principal: string | null;
  /** The group it is for, or null for a principal. */
  readonly group: string | null;
  /** The environment of the table. */
  readonly environment: string;
  /** The dotted namespace of the table. */
  readonly namespace: string;
  /** The table. */
  readonly table: string;
  /** The SQL condition. */
  readonly expression: string;
}

/** A new column mask, as `POST /api/v1/column-masks` takes it. */
export interface ColumnMaskCreateIn extends RowFilterCreateIn {
  /** The masked column. */
  readonly column: string;
}

/**
 * Creates a row filter, after the passkey touch.
 *
 * @param body - Who, which table and the condition.
 * @returns The row filter.
 * @throws {ApiError} for a refusal: 404 for an unknown name, 409 in a managed scope, 422 for an
 *   expression that is not valid.
 */
export function createRowFilter(body: RowFilterCreateIn): Promise<RowFilterOut> {
  return guarded<RowFilterOut>("POST", "/api/v1/row-filters", body);
}

/**
 * Creates a column mask, after the passkey touch.
 *
 * @param body - Who, which column of which table and what they see instead.
 * @returns The column mask.
 * @throws {ApiError} for a refusal: 404 for an unknown name, 409 in a managed scope, 422 for an
 *   expression that is not valid or an unknown column.
 */
export function createColumnMask(body: ColumnMaskCreateIn): Promise<ColumnMaskOut> {
  return guarded<ColumnMaskOut>("POST", "/api/v1/column-masks", body);
}

/**
 * Deletes a row filter, after the passkey touch: its subject then sees more.
 *
 * @param id - The row filter's id.
 * @throws {ApiError} for a refusal: 404, or 409 in a managed scope.
 */
export async function deleteRowFilter(id: string): Promise<void> {
  await guarded("DELETE", `/api/v1/row-filters/${encodeURIComponent(id)}`);
}

/**
 * Deletes a column mask, after the passkey touch: its subject then sees the column as stored.
 *
 * @param id - The column mask's id.
 * @throws {ApiError} for a refusal: 404, or 409 in a managed scope.
 */
export async function deleteColumnMask(id: string): Promise<void> {
  await guarded("DELETE", `/api/v1/column-masks/${encodeURIComponent(id)}`);
}
