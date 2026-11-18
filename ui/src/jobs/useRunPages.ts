/**
 * The runs of a list that reads its first page again every 30 s until Show more read a further
 * page: a job's runs and the Runs tab.
 *
 * @packageDocumentation
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import type { RequestOptions } from "../api/client";
import type { RunOut } from "../api/jobs";
import type { CursorPage } from "../api/manage";
import type { CursorPages } from "../catalog/views/useCursorPages";
import { POLL_FAILURES_SHOWN, useLivePoll } from "../shell/useLivePoll";
import { problemText } from "../sign-in/messages";
import { LIST_EVERY_MS } from "./poll";

/** What {@link useRunPages} hands back. */
export interface RunPages extends CursorPages<RunOut> {
  /** Whether Show more read a page, so the first page is no longer read again. */
  readonly shown: boolean;
  /**
   * Drops the pages Show more read and reads the first page again at once; until it answers, the
   * list shows no rows, never the first page as it was read before.
   */
  readonly restart: () => void;
  /** Whether a restart's first read is still open. */
  readonly restarting: boolean;
}

/** No rows, the same array on every render. */
const NO_RUNS: readonly RunOut[] = [];

/** The first page before it was read. */
const NO_PAGE: CursorPage<RunOut> = { items: NO_RUNS, nextCursor: null };

/** The pages Show more read for one filter, after the first page they continue. */
interface More {
  /** The filter they belong to. */
  readonly key: string;
  /** The first page they continue, as it was when Show more was first pressed. */
  readonly first: CursorPage<RunOut>;
  /** Their rows. */
  readonly items: readonly RunOut[];
  /** The cursor of the next page; null after the last. */
  readonly next: string | null;
  /** Whether a page is on its way. */
  readonly busy: boolean;
  /** Why the latest page could not be read; null when it was. */
  readonly error: string | null;
  /** Whether a page arrived, so the list is longer than its first page. */
  readonly shown: boolean;
}

/**
 * Reads a list of runs: the first page as a background poll every 30 s (J2: never in a hidden tab,
 * at once when the tab shows again, waiting longer after a failure, the rows kept on screen and
 * the server's sentence only after three failures in a row), and the next pages on Show more.
 * While Show more reads, and once it read a page, the first page is not read again, so the list
 * never collapses or shifts under the person, and `revision` changes nothing; a Show more that
 * failed before any page arrived lets the poll go on. Another filter starts again from its first
 * page, and an answer for a filter no longer shown is dropped.
 *
 * @param key - The filter; a new one starts again from the first page.
 * @param load - Reads a page from a cursor, null for the first, as a background poll or not.
 * @param revision - A number that changes when the first page must be read again at once.
 * @returns The pages, in the shape the list frame reads, with whether Show more read a page and
 *   the way to start again from the first page.
 */
export function useRunPages(
  key: string,
  load: (cursor: string | null, options: RequestOptions) => Promise<CursorPage<RunOut>>,
  revision: number,
): RunPages {
  const { t } = useTranslation();
  const [more, setMore] = useState<More | null>(null);
  if (more !== null && more.key !== key) {
    setMore(null);
  }
  const current = useRef(key);
  useEffect(() => {
    current.current = key;
  }, [key]);
  const extra = more !== null && more.key === key ? more : null;
  // The filter whose first read failed last: the poll keeps its error when the filter changes,
  // and that error is not the new filter's.
  const [failedKey, setFailedKey] = useState<string | null>(null);
  const first = useCallback(async () => {
    try {
      return { key, page: await load(null, { background: true }) };
    } catch (failure: unknown) {
      setFailedKey(key);
      throw failure;
    }
  }, [key, load]);
  // The first page as it was when Read again was pressed, which is not shown again.
  const [stale, setStale] = useState<unknown>(undefined);
  const still = extra !== null && (extra.shown || extra.busy);
  const poll = useLivePoll(first, { everyMs: LIST_EVERY_MS, active: !still });
  const { reload } = poll;
  const seen = useRef(revision);
  useEffect(() => {
    if (seen.current !== revision) {
      seen.current = revision;
      // While Show more holds the list the poll is stopped, and this reads nothing.
      reload();
    }
  }, [revision, reload]);
  const data = poll.data?.key === key && poll.data !== stale ? poll.data.page : undefined;
  const pollError = failedKey === key ? poll.error : null;
  // What Show more goes on from: the pages it read, or the first page as it is now.
  const from =
    extra?.shown === true
      ? extra
      : { first: data ?? NO_PAGE, items: NO_RUNS, next: data?.nextCursor ?? null, shown: false };
  const { first: firstPage, items: moreItems, next } = from;
  const items = useMemo(
    () => (data === undefined ? undefined : [...firstPage.items, ...moreItems]),
    [data, firstPage, moreItems],
  );
  const quiet = poll.failures < POLL_FAILURES_SHOWN;
  const loadMore = async (): Promise<void> => {
    const base = { key, first: firstPage, items: moreItems, next, shown: from.shown };
    setMore({ ...base, busy: true, error: null });
    let after: More;
    try {
      const page = await load(next, {});
      after = {
        ...base,
        items: [...moreItems, ...page.items],
        next: page.nextCursor,
        shown: true,
        busy: false,
        error: null,
      };
    } catch (failure: unknown) {
      after = { ...base, busy: false, error: problemText(t, failure) };
    }
    // An answer for a filter no longer shown is dropped.
    if (current.current === key) {
      setMore(after);
    }
  };
  return {
    items,
    error: data === undefined ? pollError : (extra?.error ?? (quiet ? null : pollError)),
    busy: (data === undefined && pollError === null) || extra?.busy === true,
    hasMore: next !== null,
    loadMore,
    reload,
    shown: extra?.shown === true,
    restart: () => {
      setStale(poll.data);
      setMore(null);
    },
    restarting: stale !== undefined && poll.data === stale && pollError === null,
  };
}
