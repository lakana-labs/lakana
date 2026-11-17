/**
 * The listing of `raw/`, one folder a page at a time, the route `lakana raw ls` uses: New
 * external table's Browse… reads it to fill the location.
 *
 * @packageDocumentation
 */
import { api } from "./client";
import { type CursorPage, environmentPath, pageQuery, withQuery } from "./manage";

/** A folder or file directly in a folder of `raw/`, as the server lists it. */
export interface RawEntryOut {
  /** Its name in the folder. */
  readonly name: string;
  /** Whether it is a folder or a file. */
  readonly kind: "folder" | "file";
  /** A file's size in bytes; null for a folder. */
  readonly size: number | null;
  /**
   * The external table whose folder it is or lies in, so it cannot be chosen; null when free.
   * `table` is the table's dotted name when the person may read it, else null.
   */
  readonly claimed_by: { readonly table: string | null } | null;
}

/** One page of a folder of `raw/`. */
export interface RawPage extends CursorPage<RawEntryOut> {
  /** The folder listed, such as `raw/orders`. */
  readonly path: string;
}

/**
 * Lists one page of a folder of `raw/`: its folders and files, in the server's order.
 *
 * @param environment - The environment.
 * @param path - The folder: `raw` or a folder below it.
 * @param cursor - The cursor of the page, or null for the first.
 * @returns The page.
 * @throws {ApiError} for a refusal; 403 for a person who may register an external table in no
 *   namespace of the environment.
 */
export async function listRaw(
  environment: string,
  path: string,
  cursor: string | null,
): Promise<RawPage> {
  const query = pageQuery(cursor, new URLSearchParams({ path }));
  const body = await api.request<{
    path: string;
    items: readonly RawEntryOut[];
    next_cursor: string | null;
  }>("GET", withQuery(environmentPath(environment, "/raw"), query));
  return { path: body.path, items: body.items, nextCursor: body.next_cursor };
}
