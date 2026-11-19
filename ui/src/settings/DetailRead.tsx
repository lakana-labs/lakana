/**
 * The states of a Settings detail page's read: missing, on its way, failed, or read.
 *
 * @packageDocumentation
 */
import type { JSX, ReactNode, RefObject } from "react";
import { useTranslation } from "react-i18next";

import { ReadFailed } from "../catalog/ReadFailed";
import type { Resource } from "../shell/useResource";

/** Properties of {@link DetailRead}. */
export interface DetailReadProps<T> {
  /** The page's read. */
  readonly read: Resource<T>;
  /** The page's heading, focused after Try again. */
  readonly heading: RefObject<HTMLHeadingElement | null>;
  /** The way on beside the server's sentence when the object does not exist (404). */
  readonly missing: ReactNode;
  /** Renders the page once it is read. */
  readonly children: (data: T) => ReactNode;
}

/**
 * Renders the server's sentence and the way on for a missing object, the wait in words, the
 * failure with Try again (which leaves the focus on the heading), or the page.
 *
 * @template T - What the page reads.
 * @param props - The read, the heading, the way on and the page.
 * @param props.read - The page's read.
 * @param props.heading - The page's heading.
 * @param props.missing - The way on for a missing object.
 * @param props.children - Renders the page.
 * @returns The state, or the page.
 */
export function DetailRead<T>({
  read,
  heading,
  missing,
  children,
}: DetailReadProps<T>): JSX.Element {
  const { t } = useTranslation();
  if (read.failure?.status === 404) {
    return (
      <div className="empty">
        <p role="alert">{read.error}</p>
        <p>{missing}</p>
      </div>
    );
  }
  if (read.data === undefined) {
    return read.error === null ? (
      <p className="step-status" role="status">
        {t("session.loading")}
      </p>
    ) : (
      <ReadFailed
        message={read.error}
        onRetry={() => {
          heading.current?.focus();
          read.reload();
        }}
      />
    );
  }
  return <>{children(read.data)}</>;
}
