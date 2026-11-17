/**
 * The paging of the Queries screen's two lists, Saved and Recent.
 *
 * @packageDocumentation
 */
import { useEffect, useRef } from "react";

import type { CursorPage } from "../api/manage";
import { type CursorPages, useCursorPages } from "../catalog/views/useCursorPages";

/**
 * Reads a list a page at a time, from its first page again when the filter changes (none of the
 * old rows shown) or when `reloadKey` changes (the rows shown until the new ones arrive, as
 * after a save or a delete).
 *
 * @template T - A row.
 * @param filter - The environment the list is filtered by, or null for all of them.
 * @param reloadKey - A number that changes when the list must be read again.
 * @param load - Reads a page from a cursor; null for the first.
 * @returns The rows, the state of the read and the two ways to read on.
 */
export function useLibraryPages<T>(
  filter: string | null,
  reloadKey: number,
  load: (cursor: string | null) => Promise<CursorPage<T>>,
): CursorPages<T> {
  // No environment's name is empty, so "" stands for all of them.
  const pages = useCursorPages(filter ?? "", load);
  const { reload } = pages;
  const seen = useRef(reloadKey);
  useEffect(() => {
    if (seen.current !== reloadKey) {
      seen.current = reloadKey;
      reload();
    }
  }, [reload, reloadKey]);
  return pages;
}
