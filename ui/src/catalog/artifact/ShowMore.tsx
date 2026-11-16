/**
 * A long list shown 100 rows at a time: the rows to render now, and Show more for the next ones,
 * which moves the focus to the first new row.
 *
 * @packageDocumentation
 */
import { type JSX, type RefObject, useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import "./artifactStrings";

/** How many rows a list shows at first, and how many more each Show more adds. */
const ROWS_AT_ONCE = 100;

/** What {@link useShowMore} hands back. */
export interface ShownRows {
  /** How many rows to render now. */
  readonly shown: number;
  /** How many rows are not rendered yet. */
  readonly hidden: number;
  /** Renders the next rows and moves the focus to the first of them. */
  readonly more: () => void;
}

/**
 * Shows the first {@link ROWS_AT_ONCE} rows of a list, and that many more on each `more()`. Once
 * the new rows render, the focus goes to the first of them: the element `selector` finds at its
 * index inside `region`, which must take the focus (`tabIndex={-1}`).
 *
 * @param total - How many rows the list has.
 * @param region - The element that holds the rows.
 * @param selector - Finds one element per row, in order, that takes the focus.
 * @returns The rows to render, how many are left and the way to show more.
 */
export function useShowMore(
  total: number,
  region: RefObject<HTMLElement | null>,
  selector: string,
): ShownRows {
  const [limit, setLimit] = useState(ROWS_AT_ONCE);
  const focusFrom = useRef<number | null>(null);
  const shown = Math.min(limit, total);

  useEffect(() => {
    const from = focusFrom.current;
    if (from !== null && shown > from) {
      focusFrom.current = null;
      region.current?.querySelectorAll<HTMLElement>(selector)[from]?.focus();
    }
  }, [shown, region, selector]);

  const more = useCallback(() => {
    focusFrom.current = shown;
    setLimit(shown + ROWS_AT_ONCE);
  }, [shown]);
  return { shown, hidden: total - shown, more };
}

/** Properties of {@link ShowMore}. */
export interface ShowMoreProps {
  /** The rows of {@link useShowMore}. */
  readonly rows: ShownRows;
}

/**
 * Show more, which names how many rows it adds; nothing once every row is shown.
 *
 * @param props - The rows.
 * @param props.rows - The rows of {@link useShowMore}.
 * @returns The button, or nothing.
 */
export function ShowMore({ rows }: ShowMoreProps): JSX.Element | null {
  const { t } = useTranslation();
  if (rows.hidden <= 0) {
    return null;
  }
  return (
    <button type="button" className="button show-more" onClick={rows.more}>
      {t("table.artifact.showMore", { count: Math.min(rows.hidden, ROWS_AT_ONCE) })}
    </button>
  );
}
