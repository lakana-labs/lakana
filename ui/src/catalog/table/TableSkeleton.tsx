/**
 * What a table's page shows while its table is being read: quiet blocks in the place of the
 * content, and after a second the words that say what is going on.
 *
 * @packageDocumentation
 */
import { type JSX, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

/** How long a read takes before the page says it is loading, in milliseconds. */
const SAY_AFTER_MS = 1000;

/**
 * Renders the blocks, and "Loading the table…" as a status once a second has passed.
 *
 * @returns The placeholder.
 */
export function TableSkeleton(): JSX.Element {
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
  return (
    <div className="table-skeleton">
      <div className="table-skeleton-block table-skeleton-line" aria-hidden="true" />
      <div className="table-skeleton-block table-skeleton-tabs" aria-hidden="true" />
      <div className="table-skeleton-block table-skeleton-body" aria-hidden="true" />
      {slow && (
        <p className="step-status" role="status">
          {t("table.loading")}
        </p>
      )}
    </div>
  );
}
