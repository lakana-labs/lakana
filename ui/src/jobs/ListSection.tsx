/**
 * The frame the Jobs list's three sections share: the heading, the states of the read, the
 * table in its own scrolling region and the line that says what a control did.
 *
 * @packageDocumentation
 */
import { type JSX, type ReactNode, useEffect, useId, useRef } from "react";
import { useTranslation } from "react-i18next";

import { ReadFailed } from "../catalog/ReadFailed";
import { type LivePoll, POLL_FAILURES_SHOWN } from "../shell/useLivePoll";
import { ControlNote, type ControlNoteValue } from "./ControlNote";
import "./jobStrings";
import "./listStrings";
import { Reading } from "./Reading";

/** Properties of {@link ListSection}. */
export interface ListSectionProps {
  /** The heading, such as "Running now (2)". */
  readonly heading: string;
  /** The read behind the rows. */
  readonly poll: LivePoll<unknown>;
  /** What is read, in words, for the skeleton. */
  readonly loading: string;
  /** How many rows `children` renders. */
  readonly rowCount: number;
  /** What the section says without rows; null for nothing, when a line above says why. */
  readonly empty: ReactNode;
  /** The name of the scrolling region. */
  readonly region: string;
  /** The class that sizes the region's table. */
  readonly scroll: string;
  /** The header cells of the columns before the controls; the section adds the last two. */
  readonly head: ReactNode;
  /** The body rows, each ending in {@link RowEnd}. */
  readonly children: ReactNode;
  /** What shows above the table, such as an environment that could not be read. */
  readonly before?: ReactNode;
  /** What shows under the table, such as a line that says the list is cut. */
  readonly after?: ReactNode;
  /** What a control of the section did, or why the server refused it. */
  readonly note: ControlNoteValue | null;
}

/**
 * Renders the section: its heading, the skeleton while the first read is open, the server's
 * sentence with Try again when it failed (once three reads in a row failed while rows are
 * shown), the empty sentence, the table whose rows open their object, and the control's note.
 * A row that leaves the list with the focus in it (a run that ended) hands the focus to the
 * heading, never to the page.
 *
 * @param props - The heading, the read, the sentences and the rows.
 * @param props.heading - The heading.
 * @param props.poll - The read.
 * @param props.loading - What is read.
 * @param props.rowCount - How many rows there are.
 * @param props.empty - What the section says without rows.
 * @param props.region - The region's name.
 * @param props.scroll - The class that sizes the table.
 * @param props.head - The header cells before the controls.
 * @param props.children - The body rows.
 * @param props.before - What shows above the table.
 * @param props.after - What shows under the table.
 * @param props.note - What a control did.
 * @returns The section.
 */
export function ListSection({
  heading,
  poll,
  loading,
  rowCount,
  empty,
  region,
  scroll,
  head,
  children,
  before,
  after,
  note,
}: ListSectionProps): JSX.Element {
  const { t } = useTranslation();
  const id = useId();
  const section = useRef<HTMLElement>(null);
  const title = useRef<HTMLHeadingElement>(null);
  const inside = useRef(false);
  useEffect(() => {
    const element = section.current;
    const onFocusIn = (): void => {
      inside.current = true;
    };
    const onFocusOut = (event: FocusEvent): void => {
      const { target, relatedTarget } = event;
      if (relatedTarget === null) {
        // Focus left for the page: a click on a blank part of it, or a removed row (Chrome tells
        // that blur too). Only an element still in the page was left by the person.
        setTimeout(() => {
          if (target instanceof Node && target.isConnected) {
            inside.current = false;
          }
        });
        return;
      }
      // A question the section's own control opened (a modal dialog) takes the focus out, and
      // gives it back when it closes: the person is still in the section meanwhile.
      if (relatedTarget instanceof Element && relatedTarget.closest('[role="dialog"]') !== null) {
        return;
      }
      inside.current = element?.contains(relatedTarget as Node) === true;
    };
    element?.addEventListener("focusin", onFocusIn);
    element?.addEventListener("focusout", onFocusOut);
    return () => {
      element?.removeEventListener("focusin", onFocusIn);
      element?.removeEventListener("focusout", onFocusOut);
    };
  }, []);
  useEffect(() => {
    if (!inside.current) {
      return undefined;
    }
    // On the next frame, after the dialog that just closed has handed the focus back to its
    // button (it does so on a frame of its own): only a focus still on the page is lost.
    const frame = requestAnimationFrame(() => {
      if (inside.current && document.activeElement === document.body) {
        title.current?.focus();
      }
    });
    return () => {
      cancelAnimationFrame(frame);
    };
  });
  return (
    <section className="flat-panel jobs-section" aria-labelledby={id} ref={section}>
      <h2 className="jobs-panel-heading" id={id} tabIndex={-1} ref={title}>
        {heading}
      </h2>
      {before}
      {poll.data === undefined && poll.error === null && <Reading label={loading} />}
      {poll.error !== null && (poll.data === undefined || poll.failures >= POLL_FAILURES_SHOWN) && (
        <ReadFailed message={poll.error} onRetry={poll.reload} />
      )}
      {poll.data !== undefined && rowCount === 0 && empty !== null && (
        <div className="empty">{empty}</div>
      )}
      {rowCount > 0 && (
        <div
          className={`list-scroll ${scroll}`}
          role="region"
          aria-label={region}
          // A scrolling region must be reachable by keyboard (WCAG 2.1.1), so it takes a tab stop.
          // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex
          tabIndex={0}
        >
          <table className="list jobs-list">
            <thead>
              <tr>
                {head}
                <th scope="col">
                  <span className="visually-hidden">{t("jobs.list.columns.actions")}</span>
                </th>
                <th scope="col">
                  <span className="visually-hidden">{t("list.open")}</span>
                </th>
              </tr>
            </thead>
            <tbody>{children}</tbody>
          </table>
        </div>
      )}
      {after}
      <ControlNote note={note} />
    </section>
  );
}

/** Properties of {@link RowEnd}. */
export interface RowEndProps {
  /** The row's controls; none for a person who may not use them. */
  readonly children: ReactNode;
}

/**
 * The last two cells of a row: its controls side by side, above the row's link, and the quiet
 * `›` that says the row opens its object.
 *
 * @param props - The controls.
 * @param props.children - The row's controls.
 * @returns The two cells.
 */
export function RowEnd({ children }: RowEndProps): JSX.Element {
  return (
    <>
      <td>
        <div className="jobs-row-controls">{children}</div>
      </td>
      <td>
        <span className="chevron" aria-hidden="true">
          ›
        </span>
      </td>
    </>
  );
}
