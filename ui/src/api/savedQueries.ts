/**
 * The calls of saved queries: the list across environments, and read, create, update and delete
 * within one.
 *
 * @packageDocumentation
 */
import { api } from "./client";
import { environmentPath, withQuery, type CursorPage } from "./manage";

/** Who may run a saved query: its owner alone, or everyone who may query its environment. */
export type SavedQueryVisibility = "private" | "everyone";

/** A saved query, as the reader sees it. */
export interface SavedQueryOut {
  /** Its id. */
  readonly id: string;
  /** Its environment. */
  readonly environment: string;
  /** Its name. */
  readonly name: string;
  /**
   * Its SQL; absent in every item of a list, which carries no SQL (read the saved query for it),
   * and when the reader may not read it (an administrator on someone else's private query).
   */
  readonly sql?: string;
  /** Who may run it. */
  readonly visibility: SavedQueryVisibility;
  /** The owner's name; null once the owner is deleted. */
  readonly owner: string | null;
  /** Whether the owner is deleted; nobody may then update it. */
  readonly former_owner: boolean;
  /** What the reader may do with it. */
  readonly actions: readonly ("update" | "delete")[];
  /** When it was created. */
  readonly created_at: string;
  /** When it was last changed. */
  readonly updated_at: string;
}

/** What creates or replaces a saved query: every field, none left to a default. */
export interface SavedQueryIn {
  /** The name, one line, unique among the owner's in the environment. */
  readonly name: string;
  /** The SQL. */
  readonly sql: string;
  /** Who may run it. */
  readonly visibility: SavedQueryVisibility;
}

/** How many saved queries a page lists. */
const PAGE_SIZE = 50;

/**
 * The path of an environment's saved queries, or of one.
 *
 * @param environment - The environment.
 * @param id - The saved query's id; left out for the collection.
 * @returns The path, every part encoded.
 */
function savedPath(environment: string, id?: string): string {
  return environmentPath(
    environment,
    id === undefined ? "/saved-queries" : `/saved-queries/${encodeURIComponent(id)}`,
  );
}

/**
 * Lists the saved queries the reader sees, by name, one cursor across environments.
 *
 * @param environment - Only this environment; null for all.
 * @param cursor - The cursor of the page, or null for the first.
 * @returns The page.
 * @throws {ApiError} for a refusal.
 */
export async function listSavedQueries(
  environment: string | null,
  cursor: string | null,
): Promise<CursorPage<SavedQueryOut>> {
  const query = new URLSearchParams({ limit: String(PAGE_SIZE) });
  if (environment !== null) {
    query.set("environment", environment);
  }
  if (cursor !== null) {
    query.set("cursor", cursor);
  }
  const body = await api.request<{
    items: readonly SavedQueryOut[];
    next_cursor: string | null;
  }>("GET", withQuery("/api/v1/saved-queries", query));
  return { items: body.items, nextCursor: body.next_cursor };
}

/**
 * Reads one saved query.
 *
 * @param environment - Its environment.
 * @param id - Its id.
 * @returns The saved query; without `sql` when the reader may not read it.
 * @throws {ApiError} for a refusal; 404 for one the reader does not see.
 */
export function getSavedQuery(environment: string, id: string): Promise<SavedQueryOut> {
  return api.request<SavedQueryOut>("GET", savedPath(environment, id));
}

/**
 * Saves a query as the person's own.
 *
 * @param environment - The environment it belongs to.
 * @param body - Its name, SQL and visibility.
 * @returns The saved query.
 * @throws {ApiError} for a refusal; 409 for a name the person already uses there.
 */
export function createSavedQuery(environment: string, body: SavedQueryIn): Promise<SavedQueryOut> {
  return api.request<SavedQueryOut>("POST", savedPath(environment), body);
}

/**
 * Replaces one of the person's own saved queries.
 *
 * @param environment - Its environment.
 * @param id - Its id.
 * @param body - Its name, SQL and visibility.
 * @returns The saved query.
 * @throws {ApiError} for a refusal; 409 for a name already used.
 */
export function updateSavedQuery(
  environment: string,
  id: string,
  body: SavedQueryIn,
): Promise<SavedQueryOut> {
  return api.request<SavedQueryOut>("PUT", savedPath(environment, id), body);
}

/**
 * Deletes a saved query.
 *
 * @param environment - Its environment.
 * @param id - Its id.
 * @returns Nothing.
 * @throws {ApiError} for a refusal.
 */
export async function deleteSavedQuery(environment: string, id: string): Promise<void> {
  await api.request<undefined>("DELETE", savedPath(environment, id));
}
