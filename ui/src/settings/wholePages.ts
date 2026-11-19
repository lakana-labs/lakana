/**
 * A list the server answers whole, as the list frame of the Catalog shows its pages.
 *
 * @packageDocumentation
 */
import type { RefObject } from "react";

import type { CursorPages } from "../catalog/views/useCursorPages";
import type { Resource } from "../shell/useResource";

/**
 * A hook: the one page of a list read whole: never more to read, and Try again reads it again with the
 * focus on the heading it belongs to, so the pressed button's going away loses nothing.
 *
 * @template T - A row.
 * @param read - The list's read.
 * @param heading - The heading that takes the focus on Try again.
 * @returns The pages for `ViewList`.
 */
export function useWholePages<T>(
  read: Resource<readonly T[]>,
  heading: RefObject<HTMLHeadingElement | null>,
): CursorPages<T> {
  const again = (): Promise<void> => {
    heading.current?.focus();
    read.reload();
    return Promise.resolve();
  };
  return {
    items: read.data,
    error: read.error,
    busy: read.data === undefined && read.error === null,
    hasMore: false,
    loadMore: again,
    reload: () => {
      void again();
    },
  };
}
