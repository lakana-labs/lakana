/**
 * The frame the Catalog's list views share: the skeleton while the first page is read, the
 * server's sentence when a read fails, the empty sentence, the scrolling table and Show more.
 *
 * @packageDocumentation
 */
import { type JSX, type ReactNode, type Ref, useEffect, useImperativeHandle, useRef } from "react";
import { useTranslation } from "react-i18next";

import { PendingButton } from "../../shell/PendingButton";
import { ReadFailed } from "../cells";
import { ListSkeleton } from "../ListSkeleton";
import type { CursorPages } from "./useCursorPages";

/** Properties of {@link ViewList}. */
export interface ViewListProps {
  /** The pages being read. */
  readonly pages: CursorPages<unknown>;
  /** How many rows `children` renders; zero shows the empty sentence once reading is over. */
  readonly rowCount: number;
  /** The sentence for a list with no rows. */
  readonly empty: string;
  /** The name of the scrolling region. */
  readonly region: string;
  /** The header row. */
  readonly head: ReactNode;
  /** The body rows. */
  readonly children: ReactNode;
  /** Called when Show more is pressed, before the next page is read. */
  readonly onMore?: () => void;
  /** The scrolling region, for a caller that moves the focus into it. */
  readonly regionRef?: Ref<HTMLDivElement>;
}

/**
 * Renders the list's states and its table.
 *
 * @param props - The pages, the sentences and the rows.
 * @param props.pages - The pages being read.
 * @param props.rowCount - How many rows there are.
 * @param props.empty - The sentence for no rows.
 * @param props.region - The region's name.
 * @param props.head - The header row.
 * @param props.children - The body rows.
 * @param props.onMore - Called before the next page is read.
 * @param props.regionRef - The region's element.
 * @returns The frame.
 */
export function ViewList({
  pages,
  rowCount,
  empty,
  region,
  head,
  children,
  onMore,
  regionRef,
}: ViewListProps): JSX.Element {
  const { t } = useTranslation();
  const own = useRef<HTMLDivElement>(null);
  const focusFrom = useRef<number | null>(null);

  // When the last page arrives, Show more unmounts with the focus on it: the focus goes to the
  // first new row's control, or to the list when the page was empty, never to the page.
  // A caller that passes onMore moves the focus itself.
  useEffect(() => {
    const from = focusFrom.current;
    if (from === null) {
      return;
    }
    if (rowCount > from) {
      focusFrom.current = null;
      const row = own.current?.querySelectorAll("tbody tr")[from];
      const control = row?.querySelector<HTMLElement>("a[href], button");
      (control ?? own.current)?.focus();
    } else if (!pages.busy && !pages.hasMore) {
      focusFrom.current = null;
      own.current?.focus();
    }
  });

  useImperativeHandle(regionRef, () => own.current as HTMLDivElement);
  return (
    <>
      {pages.items === undefined && pages.error === null && <ListSkeleton />}
      {pages.items === undefined && pages.error !== null && (
        <ReadFailed message={pages.error} onRetry={pages.reload} />
      )}
      {pages.items !== undefined && rowCount === 0 && !pages.busy && !pages.hasMore && (
        <div className="empty">
          <p>{empty}</p>
        </div>
      )}
      {rowCount > 0 && (
        <div
          className="list-scroll dropped-scroll"
          role="region"
          aria-label={region}
          // A scrolling region must be reachable by keyboard (WCAG 2.1.1), so it takes a tab stop.
          // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex
          tabIndex={0}
          ref={own}
        >
          <table className="list list-static">
            <thead>{head}</thead>
            <tbody>{children}</tbody>
          </table>
        </div>
      )}
      {pages.items !== undefined && pages.error !== null && (
        <p className="step-error" role="alert">
          {pages.error}
        </p>
      )}
      {pages.hasMore && (
        <PendingButton
          className="button"
          pending={pages.busy}
          onPress={() => {
            if (onMore === undefined) {
              focusFrom.current = rowCount;
            } else {
              onMore();
            }
            void pages.loadMore();
          }}
        >
          {t("catalog.showMore")}
        </PendingButton>
      )}
    </>
  );
}
