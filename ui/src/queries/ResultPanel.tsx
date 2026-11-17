/**
 * The result of a run that succeeded: how many rows and how long it took, the masked columns
 * and the cut note, the rows as the shared grid, and Show 100 more up to 1,000 rows.
 *
 * @packageDocumentation
 */
import { type JSX, useState } from "react";
import { useTranslation } from "react-i18next";

import type { ResultPageOut } from "../api";
import type { QueryRunOut } from "../api/queries";
import { formatRows } from "../catalog/format";
import { PendingButton } from "../shell/PendingButton";
import { pageCells, ResultGrid, ResultNotes } from "../shell/ResultGrid";
import { problemText } from "../sign-in/messages";
import "./queryStrings";

/** The most rows the screen shows; beyond them, the whole result is a download. */
const SHOWN_AT_MOST = 1_000;

/** Properties of {@link ResultPanel}. */
export interface ResultPanelProps {
  /** The run, which succeeded. */
  readonly run: QueryRunOut;
  /** The rows read so far; null when the run's result is no longer kept. */
  readonly page: ResultPageOut | null;
  /** The query's name, which names the grid ("Result of Daily sales"). */
  readonly name: string;
  /** Reads the next page and appends it; rejects with the refusal. */
  readonly onMore: () => Promise<void>;
}

/**
 * The status line: the rows and the run's duration, or, at the most rows shown, that the
 * whole result is a download.
 *
 * @param run - The run.
 * @param page - The rows read so far.
 * @param t - The translation function.
 * @param language - The interface language.
 * @returns The line.
 */
function summary(
  run: QueryRunOut,
  page: ResultPageOut,
  t: ReturnType<typeof useTranslation>["t"],
  language: string,
): string {
  const rows = formatRows(page.row_count, language);
  if (page.next_offset !== null && page.rows.length >= SHOWN_AT_MOST) {
    return t("queries.result.limit", { shown: formatRows(SHOWN_AT_MOST, language), rows });
  }
  if (run.started_at === null || run.finished_at === null) {
    return t("queries.result.rows", { count: page.row_count, rows });
  }
  const seconds = (Date.parse(run.finished_at) - Date.parse(run.started_at)) / 1_000;
  return t("queries.result.summary", { count: page.row_count, rows, seconds });
}

/**
 * Renders the result of a run that succeeded.
 *
 * @param props - The run, its rows, the query's name and the read of the next page.
 * @param props.run - The run.
 * @param props.page - The rows read so far, or null when the result is no longer kept.
 * @param props.name - The query's name.
 * @param props.onMore - Reads the next page and appends it.
 * @returns The panel.
 */
export function ResultPanel({ run, page, name, onMore }: ResultPanelProps): JSX.Element {
  const { t, i18n } = useTranslation();
  const [reading, setReading] = useState(false);
  const [refusal, setRefusal] = useState<string | null>(null);
  if (page === null) {
    return (
      <section className="queries-result">
        <p className="empty">{t("queries.result.gone")}</p>
      </section>
    );
  }
  const cells = pageCells(page, t);
  const more = page.next_offset !== null && page.rows.length < SHOWN_AT_MOST;
  return (
    <section className="queries-result">
      {/* Task 5 puts the downloads in this bar, after the line. */}
      <div className="queries-result-bar">
        <p role="status" className="queries-result-line">
          {summary(run, page, t, i18n.language)}
        </p>
      </div>
      <ResultNotes page={page} cells={cells} own={null} />
      {page.row_count === 0 ? (
        <p className="empty">{t("queries.result.empty")}</p>
      ) : (
        <ResultGrid page={page} cells={cells} label={t("queries.result.label", { name })} />
      )}
      {more && (
        <div className="queries-result-more">
          <PendingButton
            className="button"
            pending={reading}
            onPress={() => {
              setReading(true);
              setRefusal(null);
              onMore()
                .catch((error: unknown) => {
                  setRefusal(problemText(t, error));
                })
                .finally(() => {
                  setReading(false);
                });
            }}
          >
            {t("queries.result.more")}
          </PendingButton>
          {refusal !== null && <p role="alert">{refusal}</p>}
        </div>
      )}
    </section>
  );
}
