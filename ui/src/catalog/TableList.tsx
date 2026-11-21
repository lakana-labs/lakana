/**
 * The catalog list as a table: one row per table, a header that sorts, and every cell saying
 * what it shows even where nothing is known.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";
import { Button } from "react-aria-components";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";

import { TABLE_SORTS } from "../api";
import type { TableListItemOut, TableSort } from "../api";
import { EnvironmentLabel } from "../shell/EnvironmentLabel";
import { markedName, VisibleText } from "../shell/VisibleText";
import { kindWord, openFromCell, OwnerName, RowCount, Unknown, When } from "./cells";
import { formatBytes } from "./format";
import { HealthWord } from "./HealthWord";
import { MatchLine, showsMatch } from "./MatchLine";
import { tablePath } from "./paths";
import type { CatalogView } from "./view";

// Created sorts the list but is not a column.
const COLUMNS = TABLE_SORTS.filter((sort) => sort !== "created");
const NUMBERS: readonly TableSort[] = ["rows", "size"];

/** Properties of {@link TableList}. */
export interface TableListProps {
  /** The rows to show, in order. */
  readonly items: readonly TableListItemOut[];
  /** The view whose sort the header shows. */
  readonly view: CatalogView;
  /** Called with the column whose header was pressed. */
  readonly onSort: (sort: TableSort) => void;
  /** Whether a new view is loading; the rows stay and are marked busy. */
  readonly busy: boolean;
}

/**
 * Renders the table inside a region that scrolls on its own, so a narrow screen scrolls the
 * list and never the page. Every header is a button; the sorted one says so with `aria-sort`
 * and an arrow. Times are relative with the absolute time in words.
 *
 * @param props - The rows, the view, the sort handler and the busy flag.
 * @param props.items - The rows to show.
 * @param props.view - The view whose sort the header shows.
 * @param props.onSort - Called with the column whose header was pressed.
 * @param props.busy - Whether a new view is loading.
 * @returns The list.
 */
export function TableList({ items, view, onSort, busy }: TableListProps): JSX.Element {
  const { t, i18n } = useTranslation();
  const language = i18n.language;
  const numbers = new Intl.NumberFormat(language);

  return (
    <div
      className="list-scroll"
      role="region"
      aria-label={t("catalog.tables")}
      aria-busy={busy}
      // A scrolling region must be reachable by keyboard (WCAG 2.1.1), so it takes a tab stop.
      // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex
      tabIndex={0}
    >
      <table className="list catalog-list">
        <thead>
          <tr>
            {COLUMNS.map((sort) => (
              <th
                scope="col"
                key={sort}
                className={NUMBERS.includes(sort) ? "number" : undefined}
                {...(view.sort === sort
                  ? { "aria-sort": view.order === "asc" ? "ascending" : "descending" }
                  : {})}
              >
                <Button
                  className="sort-head"
                  onPress={() => {
                    onSort(sort);
                  }}
                >
                  {t(`catalog.columns.${sort}`)}
                  {view.sort === sort && (
                    <span aria-hidden="true">{view.order === "asc" ? "▲" : "▼"}</span>
                  )}
                </Button>
              </th>
            ))}
            <th scope="col">
              <span className="visually-hidden">{t("catalog.open")}</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {items.map((row) => (
            <tr
              key={`${row.environment}/${row.namespace}/${row.name}`}
              // Cells with hover text sit above the row's link overlay so the pointer reaches
              // them; a plain click on one is handed to the link. The keyboard has the link.
              onClick={openFromCell}
            >
              <td>
                <Link className="row-name" to={tablePath(row.environment, row.namespace, row.name)}>
                  <VisibleText text={row.name} name />
                </Link>
                <span className="second">
                  {row.match !== null && showsMatch(row.match) ? (
                    <MatchLine match={row.match} q={view.q} />
                  ) : (
                    t("catalog.secondLine", {
                      kind: kindWord(t, row.kind, row.model_kind),
                      namespace: markedName(row.namespace),
                    })
                  )}{" "}
                  <EnvironmentLabel environment={row.environment} />
                </span>
              </td>
              <td>
                <OwnerName owner={row.owner} />
              </td>
              <td>
                <HealthWord summary={row.health_summary} />
              </td>
              <td className="number">
                <RowCount value={row.row_count} />
              </td>
              <td className="number">
                {row.size_bytes === null ? (
                  <Unknown words={t("catalog.notKnown")} />
                ) : (
                  <span title={t("catalog.exactBytes", { bytes: numbers.format(row.size_bytes) })}>
                    {formatBytes(row.size_bytes, language)}
                  </span>
                )}
              </td>
              <td>
                <When iso={row.last_write_at} never={t("catalog.neverWritten")} />
              </td>
              <td>
                <When iso={row.last_read_at} never={t("catalog.neverRead")} />
              </td>
              <td>
                <span className="chevron" aria-hidden="true">
                  ›
                </span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
