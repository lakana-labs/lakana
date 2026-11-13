/**
 * What a table's page shows while its table is being read: quiet blocks in the place of the
 * content, and, from the page's frame, after a second the words that say what is going on.
 *
 * @packageDocumentation
 */
import { type JSX, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

/** How long a read takes before the page says it is loading, in milliseconds. */
const SAY_AFTER_MS = 1000;

/**
 * Renders the blocks that stand in for the content.
 *
 * @returns The placeholder.
 */
export function TableSkeleton(): JSX.Element {
  return (
    <div className="table-skeleton">
      <div className="table-skeleton-block table-skeleton-line" aria-hidden="true" />
      <div className="table-skeleton-block table-skeleton-tabs" aria-hidden="true" />
      <div className="table-skeleton-block table-skeleton-body" aria-hidden="true" />
    </div>
  );
}

/** Properties of {@link LoadingStatus}. */
interface LoadingStatusProps {
  /** Whether the table is still being read. */
  readonly waiting: boolean;
}

/**
 * Says "Loading the table…" as a status once a second has passed and the table is still not
 * read. It belongs to the page's frame, which outlives the chunk's loading and the read, so the
 * status shows once and is announced once.
 *
 * @param props - Whether the table is still being read.
 * @param props.waiting - Whether the table is still being read.
 * @returns The status, or nothing.
 */
export function LoadingStatus({ waiting }: LoadingStatusProps): JSX.Element | null {
  const { t } = useTranslation();
  const [slow, setSlow] = useState(false);
  useEffect(() => {
    const timer = setTimeout(() => {
      setSlow(true);
    }, SAY_AFTER_MS);
    return () => {
      clearTimeout(timer);
    };
  }, []);
  return slow && waiting ? (
    <p className="step-status" role="status">
      {t("table.loading")}
    </p>
  ) : null;
}
