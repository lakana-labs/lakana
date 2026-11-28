/**
 * The Queries screen's Recent list: the queries the person wrote in the last 30 days, newest
 * first, as rows that open their run.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";

import { listMyQueries, RECENT_DAYS } from "../api/queries";
import { ViewList } from "../catalog/views/ViewList";
import { EnvironmentLabel } from "../shell/EnvironmentLabel";
import type { ListSortSpec } from "../shell/listSort";
import { SortHeader } from "../shell/SortHeader";
import { useListSort } from "../shell/useListSort";
import { useWhen } from "../shell/useWhen";
import type { StatusKind } from "../status/status";
import { StatusLabel } from "../status/StatusLabel";
import { runPath } from "./paths";
import { useLibraryPages } from "./useLibraryPages";
import "./queryStrings";

/**
 * The columns the Recent list sorts by, on the server. Its own order (running, queued, then the
 * ended ones newest first) is no column; Saved keeps the plain `sort` of the address.
 */
const RECENT_SORTS: ListSortSpec<"created" | "status"> = {
  fields: { created: "desc", status: "asc" },
  natural: null,
  param: "recent",
};

/** Properties of {@link RecentList}. */
export interface RecentListProps {
  /** The person's principal name. */
  readonly me: string;
  /** The environment the list is filtered by, or null for all of them. */
  readonly filter: string | null;
  /** A number that changes when the list must be read again. */
  readonly reloadKey: number;
}

/**
 * Renders the person's written queries, 50 at a time with Show more: when each was submitted, as
 * the link to its run, the environment, how long it ran once it ended, and its state as glyph
 * and word. The list carries no SQL, so none is shown.
 *
 * @param props - The person, the filter and the reload key.
 * @param props.me - The person's principal name.
 * @param props.filter - The environment, or null for all.
 * @param props.reloadKey - Changes when the list must be read again.
 * @returns The list with its states.
 */
export function RecentList({ me, filter, reloadKey }: RecentListProps): JSX.Element {
  const { t } = useTranslation();
  const when = useWhen();
  const sorting = useListSort(RECENT_SORTS);
  const sort = sorting.choice;
  // A new sort reads the first page again; the rows stay until it arrives. A query that ends
  // while the list is paged by state is shown once.
  const pages = useLibraryPages(
    filter,
    reloadKey,
    (cursor) => listMyQueries(me, filter, cursor, sort),
    JSON.stringify(sort),
    (run) => run.id,
  );
  const rows = pages.items ?? [];
  return (
    <ViewList
      pages={pages}
      rowCount={rows.length}
      empty={t("queries.library.noRecent", { days: RECENT_DAYS })}
      region={t("queries.library.recentRegion")}
      rowsOpen
      scroll="queries-list-scroll"
      head={
        <tr>
          <SortHeader {...sorting.header("created")}>{t("queries.library.when")}</SortHeader>
          <SortHeader {...sorting.header("status")}>{t("queries.library.state")}</SortHeader>
          <th scope="col">
            <span className="visually-hidden">{t("list.open")}</span>
          </th>
        </tr>
      }
    >
      {rows.map((run) => (
        <tr key={run.id}>
          <td>
            <Link className="row-name" to={runPath(run.environment, run.id)}>
              <time dateTime={run.created_at}>{when(run.created_at)}</time>
            </Link>{" "}
            <EnvironmentLabel environment={run.environment} />
            {run.started_at !== null && run.finished_at !== null && (
              <span className="second">
                {t("queries.library.seconds", {
                  seconds: (Date.parse(run.finished_at) - Date.parse(run.started_at)) / 1_000,
                })}
              </span>
            )}
          </td>
          <td>
            {/* The five states of a run are five of the glyphs' kinds, in lower case. */}
            <StatusLabel kind={run.status.toLowerCase() as StatusKind} />
          </td>
          <td>
            <span className="chevron" aria-hidden="true">
              ›
            </span>
          </td>
        </tr>
      ))}
    </ViewList>
  );
}
