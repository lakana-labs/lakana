/**
 * The result grid: the rows of a page as a data grid, with the Value panel that shows the focused
 * or clicked cell whole. Every cell is text with its invisible characters marked.
 *
 * @packageDocumentation
 */
import { type JSX, useEffect, useRef, useState } from "react";
import { Cell, Column, Row, Table, TableBody, TableHeader } from "react-aria-components";
import { useTranslation } from "react-i18next";

import type { ResultPageOut } from "../api";
import "./data-grid.css";
import "./gridStrings";
import { isNumberType, type PreviewText, previewValue } from "./previewValue";
import { markedText, VisibleText } from "./VisibleText";

/** Properties of {@link CellText}. */
interface CellTextProps {
  /** The cell as text. */
  readonly cell: PreviewText;
}

/**
 * One cell's text: the value with its invisible characters marked, or a value marker for SQL NULL
 * and binary (words that stand for the value, styled so the text "null" never looks like it),
 * and the mark of a cut value.
 *
 * @param props - The cell.
 * @param props.cell - The cell as text.
 * @returns The text.
 */
export function CellText({ cell }: CellTextProps): JSX.Element {
  const { t } = useTranslation();
  return (
    <>
      {cell.muted ? (
        <span className="value-marker">
          {cell.text === t("grid.null") ? (
            // Read as "SQL null", so a screen reader tells it from the text "null" too.
            <>
              <span aria-hidden="true">{cell.text}</span>
              <span className="visually-hidden">{t("grid.sqlNull")}</span>
            </>
          ) : (
            cell.text
          )}
        </span>
      ) : (
        <VisibleText text={cell.text} />
      )}
      {cell.cut && <span className="preview-muted">{t("grid.cutMark")}</span>}
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
export function pageCells(
  page: ResultPageOut,
  t: ReturnType<typeof useTranslation>["t"],
): PreviewText[][] {
  return page.rows.map((row, index) =>
    page.columns.map((column, at) =>
      previewValue(row[at], column.type, page.row_cut.includes(index), t),
    ),
  );
}

/** Properties of {@link ResultGrid}. */
interface ResultGridProps {
  /** The page. */
  readonly page: ResultPageOut;
  /** The page's cells as text, from {@link pageCells}. */
  readonly cells: readonly (readonly PreviewText[])[];
  /** The grid's name, as the region and the grid are named; its invisible characters are marked. */
  readonly label: string;
}

/**
 * The rows as a data grid with one tab stop and the arrow keys inside, and the Value panel that
 * shows the focused or clicked cell whole. A muted gutter numbers the rows: it is each row's
 * header for a screen reader, and neither a value nor part of copied cells.
 *
 * @param props - The page, its cells and the label.
 * @param props.page - The page.
 * @param props.cells - The page's cells as text.
 * @param props.label - The grid's name.
 * @returns The grid and the panel.
 */
export function ResultGrid({ page, cells, label }: ResultGridProps): JSX.Element {
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
      // The gutter, cell 0, is the row's number and not a value.
      if (
        target instanceof HTMLTableCellElement &&
        target.closest("tbody") !== null &&
        target.cellIndex > 0
      ) {
        const row = target.parentElement as HTMLTableRowElement;
        setChosen({ row: row.sectionRowIndex, column: target.cellIndex - 1 });
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
  const numbers = page.columns.map((one) => (isNumberType(one.type) ? "number" : undefined));
  return (
    <>
      <div
        ref={region}
        className="data-grid-scroll"
        role="region"
        aria-label={t("grid.region", { label: markedText(label) })}
      >
        <Table aria-label={t("grid.grid", { label: markedText(label) })} className="data-grid">
          <TableHeader>
            <Column id="row-number" isRowHeader className="row-number" textValue="#">
              #
            </Column>
            {page.columns.map((one, at) => (
              <Column
                key={at}
                id={`column-${String(at)}`}
                textValue={one.name}
                {...(numbers[at] === undefined ? {} : { className: numbers[at] })}
              >
                <VisibleText text={one.name} />
              </Column>
            ))}
          </TableHeader>
          <TableBody>
            {cells.map((row, index) => (
              <Row key={index} id={`row-${String(index)}`}>
                <Cell className="row-number" textValue={String(index + 1)}>
                  {index + 1}
                </Cell>
                {row.map((cell, at) => (
                  <Cell
                    key={`${String(index)}-${String(at)}`}
                    textValue={cell.text}
                    {...(numbers[at] === undefined ? {} : { className: numbers[at] })}
                  >
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
          aria-label={t("grid.valueOf", {
            column: markedText(column.name),
            row: chosen.row + 1,
          })}
        >
          <p className="second">
            {t("grid.value")} · <VisibleText text={column.name} />
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
        <p className="second">{t("grid.valueHint")}</p>
      )}
    </>
  );
}
