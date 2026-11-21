/**
 * The toolbar above a list: its count as the section's title and its search on the left, its
 * filters and Refresh on the right, every control one height (`.toolbar` in `screen.css`).
 *
 * @packageDocumentation
 */
import type { TFunction } from "i18next";
import { type JSX, type ReactNode, useCallback, useState } from "react";
import { useTranslation } from "react-i18next";

import { PendingButton } from "./PendingButton";

/**
 * The count of a list in words: "5 connections", or "The first 100 connections" while more can
 * be read; null until the list's first page is there.
 *
 * @param t - The translation function.
 * @param key - The list's strings: plural keys `<key>.count` and `<key>.countFirst`.
 * @param shown - How many rows are shown; undefined while the list is read.
 * @param more - Whether more rows can be read.
 * @returns The words, or null.
 */
export function countWords(
  t: TFunction,
  key: string,
  shown: number | undefined,
  more = false,
): string | null {
  if (shown === undefined) {
    return null;
  }
  return t(more ? `${key}.countFirst` : `${key}.count`, { count: shown });
}

/** What a list tells the toolbar above it, when the toolbar sits outside the list's own frame. */
export interface ListState {
  /** How many rows are shown; undefined while the first page is read. */
  readonly shown: number | undefined;
  /** Whether more rows can be read. */
  readonly more: boolean;
  /** Whether a read is on its way. */
  readonly busy: boolean;
}

/**
 * The state a list reports (`ViewList`'s `onState`) for a toolbar its page renders, so that a
 * search field above a list that remounts per search keeps its focus.
 *
 * @returns The state, and the function the list reports it with.
 */
export function useListState(): [ListState, (state: ListState) => void] {
  const [state, setState] = useState<ListState>({ shown: undefined, more: false, busy: true });
  const report = useCallback((next: ListState) => {
    setState((previous) =>
      previous.shown === next.shown && previous.more === next.more && previous.busy === next.busy
        ? previous
        : next,
    );
  }, []);
  return [state, report];
}

/** Properties of {@link ListToolbar}. */
export interface ListToolbarProps {
  /** How many rows the list has, in words ("5 connections"); null while it is read. */
  readonly count: string | null;
  /** The list's search field, after the count; none by default. */
  readonly search?: ReactNode;
  /** The list's filters, on the right before Refresh; none by default. */
  readonly filters?: ReactNode;
  /** Reads the list again; no Refresh without it. */
  readonly onRefresh?: (() => void) | undefined;
  /** Whether a read is on its way, which Refresh shows; no by default. */
  readonly refreshing?: boolean;
}

/**
 * Renders the count, the search, the filters and Refresh in one row that wraps on a narrow
 * screen. The count is plain text in the section step, so a screen reader finds it in reading
 * order; it is not a live region (a list read again would announce itself on every poll).
 *
 * @param props - The count, the controls and the read.
 * @param props.count - The count in words.
 * @param props.search - The search field.
 * @param props.filters - The filters.
 * @param props.onRefresh - Reads the list again.
 * @param props.refreshing - Whether a read is on its way.
 * @returns The toolbar.
 */
export function ListToolbar({
  count,
  search,
  filters,
  onRefresh,
  refreshing = false,
}: ListToolbarProps): JSX.Element {
  const { t } = useTranslation();
  return (
    <div className="toolbar list-toolbar">
      <p className="toolbar-count">{count}</p>
      {search}
      <div className="toolbar-end">
        {filters}
        {onRefresh !== undefined && (
          <PendingButton className="button" pending={refreshing} onPress={onRefresh}>
            {t("list.refresh")}
          </PendingButton>
        )}
      </div>
    </div>
  );
}
