/**
 * Reads a list a page at a time by cursor, for the Catalog's views.
 *
 * @packageDocumentation
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import type { CursorPage } from "../../api/manage";
import { problemText } from "../../sign-in/messages";

/** What {@link useCursorPages} hands back. */
export interface CursorPages<T> {
  /** The rows read so far; undefined until the first page of the current key arrives. */
  readonly items: readonly T[] | undefined;
  /** The cause and next step when the latest read failed, else null. */
  readonly error: string | null;
  /** Whether a page is on its way, the first or a later one. */
  readonly busy: boolean;
  /** Whether another page can be read. */
  readonly hasMore: boolean;
  /** Reads the next page and appends it. */
  readonly loadMore: () => Promise<void>;
  /** Reads the list again from its first page. */
  readonly reload: () => void;
}

interface State<T> {
  readonly key: string;
  readonly items: readonly T[] | undefined;
  readonly next: string | null;
  readonly error: string | null;
  readonly loading: "first" | "more" | null;
}

/**
 * Reads the first page whenever `key` changes and the next pages on `loadMore`. A new key
 * starts again and shows none of the old rows; an answer for an older key or round is dropped.
 *
 * @template T - A row.
 * @param key - What the list is of (an environment and a namespace); a new one starts again.
 * @param load - Reads a page from a cursor; null for the first.
 * @param order - The order the list is read in; a new one reads the first page again, and the
 *   rows shown stay, marked busy, until it arrives, so the header that sorted keeps the focus.
 * @returns The rows, the state of the read and the two ways to read on.
 */
export function useCursorPages<T>(
  key: string,
  load: (cursor: string | null) => Promise<CursorPage<T>>,
  order = "",
): CursorPages<T> {
  const { t } = useTranslation();
  const [state, setState] = useState<State<T>>({
    key,
    items: undefined,
    next: null,
    error: null,
    loading: "first",
  });
  const [round, setRound] = useState(0);
  const generation = useRef(0);
  const reader = useRef(load);
  useEffect(() => {
    reader.current = load;
  });

  useEffect(() => {
    generation.current += 1;
    const mine = generation.current;
    setState((previous) => ({
      key,
      items: previous.key === key ? previous.items : undefined,
      next: null,
      error: null,
      loading: "first",
    }));
    reader.current(null).then(
      (first) => {
        if (mine === generation.current) {
          setState({
            key,
            items: first.items,
            next: first.nextCursor,
            error: null,
            loading: null,
          });
        }
      },
      (failure: unknown) => {
        if (mine === generation.current) {
          setState({
            key,
            items: undefined,
            next: null,
            error: problemText(t, failure),
            loading: null,
          });
        }
      },
    );
  }, [key, order, round, t]);

  const current = state.key === key;
  const { next, items } = state;
  const loadMore = useCallback(async () => {
    if (!current || next === null || items === undefined) {
      return;
    }
    const mine = generation.current;
    setState((previous) => ({ ...previous, error: null, loading: "more" }));
    try {
      const more = await reader.current(next);
      if (mine === generation.current) {
        setState({
          key,
          items: [...items, ...more.items],
          next: more.nextCursor,
          error: null,
          loading: null,
        });
      }
    } catch (failure: unknown) {
      if (mine === generation.current) {
        setState((previous) => ({ ...previous, error: problemText(t, failure), loading: null }));
      }
    }
  }, [current, key, next, items, t]);

  const reload = useCallback(() => {
    setRound((previous) => previous + 1);
  }, []);

  return {
    items: current ? items : undefined,
    error: current ? state.error : null,
    busy: !current || state.loading !== null,
    hasMore: current && state.next !== null && state.loading !== "first",
    loadMore,
    reload,
  };
}
