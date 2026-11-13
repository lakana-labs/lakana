/**
 * Reads the catalog list a page at a time for a view.
 *
 * @packageDocumentation
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { listTables } from "../api";
import type { TableListItemOut } from "../api";
import { problemText } from "../sign-in/messages";
import { type CatalogView, viewQuery } from "./view";

/** What {@link useTablePages} hands back. */
export interface TablePages {
  /** The rows read so far; undefined until the first page of the first view arrives. */
  readonly items: readonly TableListItemOut[] | undefined;
  /** Whether a page is on its way, the first of a view or a later one. */
  readonly busy: boolean;
  /** The cause and next step when the latest read failed, else null. */
  readonly error: string | null;
  /** Whether the server stopped looking before it had seen every table. */
  readonly capped: boolean;
  /** Whether another page can be read; false while a new view's first page is on its way. */
  readonly hasMore: boolean;
  /** Reads the next page and appends it. */
  readonly loadMore: () => Promise<void>;
  /** Reads the view again from its first page. */
  readonly reload: () => void;
}

interface State {
  readonly items: readonly TableListItemOut[] | undefined;
  readonly next: string | null;
  readonly capped: boolean;
  readonly error: string | null;
  readonly loading: "first" | "more" | null;
}

/**
 * The key of a row, which no two rows of a list share.
 *
 * @param row - The row.
 * @returns The key.
 */
function keyOf(row: TableListItemOut): string {
  return JSON.stringify([row.environment, row.namespace, row.name]);
}

/**
 * Reads the first page of a view whenever the view changes and the next pages on `loadMore`. The
 * rows of the previous view stay while the new one loads; an answer for an older view or an
 * older round is dropped, so a slow answer never replaces a newer one; a cursor is never sent
 * to another view.
 *
 * @param view - The view to read.
 * @returns The rows, the state of the read and the two ways to read on.
 */
export function useTablePages(view: CatalogView): TablePages {
  const { t } = useTranslation();
  const [state, setState] = useState<State>({
    items: undefined,
    next: null,
    capped: false,
    error: null,
    loading: "first",
  });
  const [round, setRound] = useState(0);
  const generation = useRef(0);
  const { environment, namespace, q, sort, order } = view;

  useEffect(() => {
    generation.current += 1;
    const mine = generation.current;
    setState((previous) => ({ ...previous, error: null, loading: "first" }));
    listTables(viewQuery({ environment, namespace, q, sort, order }, null)).then(
      (page) => {
        if (mine === generation.current) {
          setState({
            items: page.items,
            next: page.next_cursor,
            capped: page.capped,
            error: null,
            loading: null,
          });
        }
      },
      (failure: unknown) => {
        if (mine === generation.current) {
          setState({
            items: undefined,
            next: null,
            capped: false,
            error: problemText(t, failure),
            loading: null,
          });
        }
      },
    );
  }, [environment, namespace, q, sort, order, round, t]);

  const { next, items } = state;
  const loadMore = useCallback(async () => {
    if (next === null || items === undefined) {
      return;
    }
    const mine = generation.current;
    setState((previous) => ({ ...previous, error: null, loading: "more" }));
    try {
      const page = await listTables(viewQuery({ environment, namespace, q, sort, order }, next));
      if (mine === generation.current) {
        const seen = new Set(items.map(keyOf));
        setState({
          items: [...items, ...page.items.filter((row) => !seen.has(keyOf(row)))],
          next: page.next_cursor,
          capped: page.capped,
          error: null,
          loading: null,
        });
      }
    } catch (failure: unknown) {
      if (mine === generation.current) {
        setState((previous) => ({ ...previous, error: problemText(t, failure), loading: null }));
      }
    }
  }, [environment, namespace, q, sort, order, next, items, t]);

  const reload = useCallback(() => {
    setRound((previous) => previous + 1);
  }, []);

  return {
    items: state.items,
    busy: state.loading !== null,
    error: state.error,
    capped: state.capped,
    hasMore: state.next !== null && state.loading !== "first",
    loadMore,
    reload,
  };
}
