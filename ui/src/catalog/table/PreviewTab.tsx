/**
 * The Preview tab: the first rows of a table, read as the person, with the columns masked for
 * them named. Every cell is text with its invisible characters marked; a cell cut to one line is
 * shown whole in the Value panel when it has the focus or is clicked.
 *
 * @packageDocumentation
 */
import { type JSX, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import type { QueryRunStatus, ResultPageOut } from "../../api";
import { pageCells, ResultGrid, ResultNotes } from "../../shell/ResultGrid";
import { markedName, markedText, VisibleText } from "../../shell/VisibleText";
import { StatusGlyph } from "../../status/StatusGlyph";
import { useTableData } from "./TableContext";
import { statusGlyph, usePreview } from "./usePreview";

/** How long a preview waits before the tab says it is waiting for a worker. */
const WAIT_WORDS_MS = 1_000;

/** The rows a preview shows at most; a page of this many is "the first" rows. */
const PREVIEW_ROWS = 100;

/** Properties of {@link Waiting}. */
interface WaitingProps {
  /** Whether the right to run queries is still being asked for, before any run exists. */
  readonly checking: boolean;
  /** The run's state; null before it is known. */
  readonly status: QueryRunStatus | null;
}

/**
 * The words while a preview waits, one line per step: asking for the right, queuing, queued
 * (after a second, waiting for a worker) or running with its glyph, and reading the rows.
 *
 * @param props - The step and the run's state.
 * @param props.checking - Whether the right to run queries is still being asked for.
 * @param props.status - The run's state; null before it is known.
 * @returns The words.
 */
function Waiting({ checking, status }: WaitingProps): JSX.Element {
  const { t } = useTranslation();
  const [slow, setSlow] = useState(false);
  useEffect(() => {
    const timer = setTimeout(() => {
      setSlow(true);
    }, WAIT_WORDS_MS);
    return () => {
      clearTimeout(timer);
    };
  }, []);
  if (checking) {
    return <>{t("table.preview.checking")}</>;
  }
  if (status === null) {
    return <>{t("table.preview.queuing")}</>;
  }
  if (status !== "QUEUED" && status !== "RUNNING") {
    // The run has finished: its page is being read, or read again while the server is busy.
    return <>{t("table.preview.reading")}</>;
  }
  return (
    <span className="preview-waiting">
      <StatusGlyph kind={statusGlyph[status]} />
      <span>{t(`table.preview.status.${status}`)}</span>
      {slow && status === "QUEUED" && <span>{t("table.preview.waiting")}</span>}
    </span>
  );
}

/**
 * The line over the rows: how many, that they were read as the person, and how long it took.
 *
 * @param page - The page.
 * @param t - The translation function.
 * @returns The line.
 */
function summary(page: ResultPageOut, t: ReturnType<typeof useTranslation>["t"]): string {
  const shown = page.rows.length;
  const count =
    shown < page.row_count
      ? t("table.preview.partOf", { shown, count: page.row_count })
      : page.row_count === PREVIEW_ROWS
        ? t("table.preview.first", { count: page.row_count })
        : t("table.preview.rows", { count: page.row_count });
  return page.duration_ms === null
    ? count
    : count + t("table.preview.time", { seconds: page.duration_ms / 1_000 });
}

/**
 * The Preview tab: runs the preview while it is open, cancels it when it is left, and shows the
 * rows, the masked columns and Refresh; a person who may not run queries gets one line and no
 * request. An artifact has no Preview tab.
 *
 * @returns The tab's content.
 */
export function PreviewTab(): JSX.Element {
  const { t } = useTranslation();
  const { ref } = useTableData();
  const preview = usePreview(ref);
  const { phase, status, page, error } = preview;
  if (phase === "not_allowed") {
    return (
      <p className="empty">
        {t("table.preview.notAllowed", { environment: markedText(ref.environment) })}
      </p>
    );
  }
  const busy = phase === "checking" || phase === "waiting";
  const shown = phase === "done" ? page : null;
  const cells = shown === null ? [] : pageCells(shown, t);
  return (
    <div className="preview">
      <div className="preview-bar">
        <p role="status" className="preview-line">
          {busy && <Waiting checking={phase === "checking"} status={status} />}
          {shown !== null && summary(shown, t)}
        </p>
        <button type="button" className="button" onClick={preview.refresh}>
          {t("table.preview.refresh")}
        </button>
      </div>
      {phase === "failed" && (
        <p role="alert">
          <VisibleText text={String(error)} />
        </p>
      )}
      {shown !== null && (
        <>
          <ResultNotes page={shown} cells={cells} own={`${ref.namespace}.${ref.name}`} />
          {shown.row_count === 0 ? (
            <p className="empty">{t("table.preview.empty")}</p>
          ) : (
            <ResultGrid
              page={shown}
              cells={cells}
              label={t("table.preview.label", { name: markedName(ref.name) })}
            />
          )}
        </>
      )}
    </div>
  );
}
