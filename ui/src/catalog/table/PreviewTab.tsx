/**
 * The Preview tab: the first rows of a table, read as the person, with the columns masked for
 * them named. Every cell is text with its invisible characters marked; a cell cut to one line is
 * shown whole in the Value panel when it has the focus or is clicked.
 *
 * @packageDocumentation
 */
import { type JSX, useEffect, useRef, useState } from "react";
import { Cell, Column, Row, Table, TableBody, TableHeader } from "react-aria-components";
import { useTranslation } from "react-i18next";

import type { MaskedColumnOut, QueryRunStatus, ResultPageOut } from "../../api";
import { VisibleText } from "../../shell/VisibleText";
import { StatusGlyph } from "../../status/StatusGlyph";
import { type PreviewText, previewValue } from "./previewValue";
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

/** Properties of {@link Masked}. */
interface MaskedProps {
  /** The columns masked for the person. */
  readonly columns: readonly MaskedColumnOut[];
  /** How many more the page left out. */
  readonly more: number;
  /** The previewed table's dotted name. */
  readonly own: string;
}

/**
 * Names the columns masked for the person: a column of the table by its name, a column of a
 * table a view reads with that table.
 *
 * @param props - The columns, how many more, and the table's own name.
 * @param props.columns - The columns masked for the person.
 * @param props.more - How many more the page left out.
 * @param props.own - The previewed table's dotted name.
 * @returns The line.
 */
function Masked({ columns, more, own }: MaskedProps): JSX.Element {
  const { t } = useTranslation();
  const names = columns.map((masked) =>
    masked.table === own
      ? masked.column
      : t("table.preview.maskedOf", { column: masked.column, table: masked.table }),
  );
  const tail = more > 0 ? ` ${t("table.preview.maskedMore", { count: more })}` : "";
  return (
    <p className="second">
      <span>{t("table.preview.masked")}</span> <VisibleText text={names.join(", ") + tail} />
    </p>
  );
}

/** Properties of {@link CellText}. */
interface CellTextProps {
  /** The cell as text. */
  readonly cell: PreviewText;
}

/**
 * One cell's text: the value with its invisible characters marked, or the muted words for null
 * and binary, and the mark of a cut value.
 *
 * @param props - The cell.
 * @param props.cell - The cell as text.
 * @returns The text.
 */
function CellText({ cell }: CellTextProps): JSX.Element {
  const { t } = useTranslation();
  return (
    <>
      {cell.muted ? (
        <span className="preview-muted">{cell.text}</span>
      ) : (
        <VisibleText text={cell.text} />
      )}
      {cell.cut && <span className="preview-muted">{t("table.preview.cutMark")}</span>}
    </>
  );
}

/**
 * Every cell of a page as text, one entry per column: a cell the page budget left out of a cut
 * row reads as cut.
 *
 * @param page - The page.
 * @param t - The translation function.
 * @returns The rows of cells.
 */
function pageCells(
  page: ResultPageOut,
  t: ReturnType<typeof useTranslation>["t"],
): PreviewText[][] {
  return page.rows.map((row, index) =>
    page.columns.map((column, at) =>
      previewValue(row[at], column.type, page.row_cut.includes(index), t),
    ),
  );
}

/** Properties of {@link PreviewGrid}. */
interface PreviewGridProps {
  /** The page. */
  readonly page: ResultPageOut;
  /** The page's cells as text, from {@link pageCells}. */
  readonly cells: readonly (readonly PreviewText[])[];
  /** The table's name. */
  readonly name: string;
}

/**
 * The rows as a grid with one tab stop and the arrow keys inside, and the Value panel that shows
 * the focused or clicked cell whole.
 *
 * @param props - The page, its cells and the table's name.
 * @param props.page - The page.
 * @param props.cells - The page's cells as text.
 * @param props.name - The table's name.
 * @returns The grid and the panel.
 */
function PreviewGrid({ page, cells, name }: PreviewGridProps): JSX.Element {
  const { t } = useTranslation();
  const region = useRef<HTMLDivElement>(null);
  const [chosen, setChosen] = useState<{ readonly row: number; readonly column: number } | null>(
    null,
  );
  useEffect(() => {
    const element = region.current;
    /* v8 ignore next 3 -- the region is rendered with the grid */
    if (element === null) {
      return undefined;
    }
    // The cell that has the focus or was clicked: React Aria's grid moves the focus itself, so
    // the panel follows the DOM rather than a callback of the grid.
    const follow = (event: Event): void => {
      // Focus and clicks inside the region always come from an element.
      const target = (event.target as Element).closest("td, th");
      if (target instanceof HTMLTableCellElement && target.closest("tbody") !== null) {
        const row = target.parentElement as HTMLTableRowElement;
        setChosen({ row: row.sectionRowIndex, column: target.cellIndex });
      }
    };
    element.addEventListener("focusin", follow);
    element.addEventListener("click", follow);
    return () => {
      element.removeEventListener("focusin", follow);
      element.removeEventListener("click", follow);
    };
  }, []);
  const whole = chosen === null ? undefined : cells[chosen.row]?.[chosen.column];
  const column = chosen === null ? undefined : page.columns[chosen.column];
  return (
    <>
      <div
        ref={region}
        className="list-scroll preview-scroll"
        role="region"
        aria-label={t("table.preview.region", { name })}
      >
        <Table aria-label={t("table.preview.grid", { name })} className="list preview-grid">
          <TableHeader>
            {page.columns.map((one, at) => (
              <Column
                key={at}
                id={`column-${String(at)}`}
                isRowHeader={at === 0}
                textValue={one.name}
              >
                <VisibleText text={one.name} />
              </Column>
            ))}
          </TableHeader>
          <TableBody>
            {cells.map((row, index) => (
              <Row key={index} id={`row-${String(index)}`}>
                {row.map((cell, at) => (
                  <Cell key={`${String(index)}-${String(at)}`} textValue={cell.text}>
                    <CellText cell={cell} />
                  </Cell>
                ))}
              </Row>
            ))}
          </TableBody>
        </Table>
      </div>
      {whole !== undefined && column !== undefined && chosen !== null ? (
        <section
          className="preview-value"
          aria-label={t("table.preview.valueOf", { column: column.name, row: chosen.row + 1 })}
        >
          <p className="second">
            {t("table.preview.value")} · <VisibleText text={column.name} />
          </p>
          <p
            className="preview-whole"
            // A long value scrolls here, and a scrolling region must be reachable by keyboard
            // (WCAG 2.1.1), so it takes a tab stop.
            // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex
            tabIndex={0}
          >
            <CellText cell={whole} />
          </p>
        </section>
      ) : (
        <p className="second">{t("table.preview.valueHint")}</p>
      )}
    </>
  );
}

/**
 * The Preview tab: runs the preview while it is open, cancels it when it is left, and shows the
 * rows, the masked columns and Refresh; a person who may not run queries, and an artifact, get
 * one line and no request.
 *
 * @returns The tab's content.
 */
export function PreviewTab(): JSX.Element {
  const { t } = useTranslation();
  const { ref, table } = useTableData();
  const preview = usePreview(ref, table.kind);
  const { phase, status, page, error } = preview;
  if (phase === "artifact") {
    return <p className="empty">{t("table.preview.artifact")}</p>;
  }
  if (phase === "not_allowed") {
    return (
      <p className="empty">{t("table.preview.notAllowed", { environment: ref.environment })}</p>
    );
  }
  const busy = phase === "checking" || phase === "waiting";
  const shown = phase === "done" ? page : null;
  const cells = shown === null ? [] : pageCells(shown, t);
  const anyCut =
    shown !== null &&
    (shown.row_cut.length > 0 ||
      shown.columns_cut > 0 ||
      cells.some((row) => row.some((cell) => cell.cut)));
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
          {shown.masked_columns !== null && shown.masked_columns.length > 0 && (
            <Masked
              columns={shown.masked_columns}
              more={shown.masked_columns_cut}
              own={`${ref.namespace}.${ref.name}`}
            />
          )}
          {anyCut && <p className="second">{t("table.preview.cutNote")}</p>}
          {shown.row_count === 0 ? (
            <p className="empty">{t("table.preview.empty")}</p>
          ) : (
            <PreviewGrid page={shown} cells={cells} name={ref.name} />
          )}
        </>
      )}
    </div>
  );
}
