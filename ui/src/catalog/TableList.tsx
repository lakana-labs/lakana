/**
 * The catalog list as a table: one row per table, a header that sorts, and every cell saying
 * what it shows even where nothing is known.
 *
 * @packageDocumentation
 */
import type { JSX, MouseEvent } from "react";
import { Button } from "react-aria-components";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";

import { TABLE_SORTS } from "../api";
import type { TableListItemOut, TableSort } from "../api";
import { Avatar } from "../profile/Avatar";
import { EnvironmentLabel } from "../shell/EnvironmentLabel";
import { useWhen } from "../shell/useWhen";
import { formatBytes, formatRows, relativeTime } from "./format";
import { HealthWord } from "./HealthWord";
import { MatchLine, showsMatch } from "./MatchLine";
import { tablePath } from "./paths";
import type { CatalogView } from "./view";

// Created sorts the list but is not a column.
const COLUMNS = TABLE_SORTS.filter((sort) => sort !== "created");
const NUMBERS: readonly TableSort[] = ["rows", "size"];

/**
 * What a cell shows where nothing is known: a dash for the eye and the words for a screen reader.
 *
 * @param props - The words.
 * @param props.words - What is not known, in words.
 * @returns The dash and the words.
 */
function Unknown({ words }: { readonly words: string }): JSX.Element {
  return (
    <>
      <span aria-hidden="true">–</span>
      <span className="visually-hidden">{words}</span>
    </>
  );
}

/**
 * Hands a plain primary click on a row's cell to the row's link, so the cells that carry hover
 * text open the table page like the rest of the row. A click on the link or a button, one with
 * a modifier key, a middle click and the end of a text selection are left alone.
 *
 * @param event - The click on the row.
 */
function openFromCell(event: MouseEvent<HTMLTableRowElement>): void {
  const plain =
    event.button === 0 && !(event.ctrlKey || event.metaKey || event.shiftKey || event.altKey);
  const selecting = window.getSelection()?.isCollapsed === false;
  if (
    plain &&
    !selecting &&
    !event.defaultPrevented &&
    !(event.target as Element).closest("a, button")
  ) {
    event.currentTarget.querySelector<HTMLAnchorElement>("a.row-name")?.click();
  }
}

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
 * and an arrow. Times are relative with the absolute time in words, computed once per render.
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
  const when = useWhen();
  const language = i18n.language;
  const numbers = new Intl.NumberFormat(language);
  const now = Date.now();

  const time = (iso: string | null, never: string): JSX.Element => {
    if (iso === null) {
      return <Unknown words={never} />;
    }
    const absolute = when(iso);
    return (
      <>
        <time dateTime={iso} title={absolute}>
          {relativeTime(iso, now, language) ?? t("catalog.justNow")}
        </time>
        <span className="visually-hidden"> {absolute}</span>
      </>
    );
  };

  const rowCount = (value: number | null): JSX.Element => {
    if (value === null) {
      return <Unknown words={t("catalog.notKnown")} />;
    }
    const short = formatRows(value, language);
    const exact = numbers.format(value);
    return short === exact ? (
      <>{short}</>
    ) : (
      <>
        <span aria-hidden="true" title={exact}>
          {short}
        </span>
        <span className="visually-hidden">{exact}</span>
      </>
    );
  };

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
                  {row.name}
                </Link>
                <span className="second">
                  {row.match !== null && showsMatch(row.match) ? (
                    <MatchLine match={row.match} q={view.q} />
                  ) : (
                    t("catalog.secondLine", {
                      kind:
                        row.model_kind === null
                          ? t(`catalog.kind.${row.kind}`)
                          : t(`catalog.modelKind.${row.model_kind}`, {
                              defaultValue: t(`catalog.kind.${row.kind}`),
                            }),
                      namespace: row.namespace,
                    })
                  )}{" "}
                  <EnvironmentLabel environment={row.environment} />
                </span>
              </td>
              <td>
                {row.owner === null ? (
                  <Unknown words={t("catalog.noOwner")} />
                ) : (
                  <div className="person-cell">
                    <Avatar name={row.owner.name} displayName={null} etag={null} size={24} />
                    <span>{row.owner.name}</span>
                    {row.owner.kind === "SERVICE_ACCOUNT" && (
                      <span className="second">{t("catalog.serviceAccount")}</span>
                    )}
                  </div>
                )}
              </td>
              <td>
                <HealthWord summary={row.health_summary} />
              </td>
              <td className="number">{rowCount(row.row_count)}</td>
              <td className="number">
                {row.size_bytes === null ? (
                  <Unknown words={t("catalog.notKnown")} />
                ) : (
                  <span title={t("catalog.exactBytes", { bytes: numbers.format(row.size_bytes) })}>
                    {formatBytes(row.size_bytes, language)}
                  </span>
                )}
              </td>
              <td>{time(row.last_write_at, t("catalog.neverWritten"))}</td>
              <td>{time(row.last_read_at, t("catalog.neverRead"))}</td>
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
