/**
 * The Queries screen's Saved list: the saved queries the person sees, by name, as rows that open
 * them.
 *
 * @packageDocumentation
 */
import type { TFunction } from "i18next";
import type { JSX } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";

import { listSavedQueries, type SavedQueryOut } from "../api/savedQueries";
import { When } from "../catalog/cells";
import { ViewList } from "../catalog/views/ViewList";
import { EnvironmentLabel } from "../shell/EnvironmentLabel";
import type { ListSortSpec } from "../shell/listSort";
import { SortHeader } from "../shell/SortHeader";
import { useListSort } from "../shell/useListSort";
import { VisibleText } from "../shell/VisibleText";
import { savedPath } from "./paths";
import { useLibraryPages } from "./useLibraryPages";
import "./queryStrings";

/**
 * Who a saved query is for and whose it is, in words: "everyone" for a shared one, and "by
 * {{owner}}" when it is not the person's own, or "former owner" once its owner is deleted.
 *
 * @param t - The translation function.
 * @param saved - The saved query.
 * @param me - The person's principal name.
 * @returns The words, joined by a middle dot; empty for the person's own private query.
 */
export function savedWho(t: TFunction, saved: SavedQueryOut, me: string): string {
  const words: string[] = [];
  if (saved.visibility === "everyone") {
    words.push(t("queries.library.everyone"));
  }
  if (saved.former_owner) {
    words.push(t("queries.library.formerOwner"));
  } else if (saved.owner !== me) {
    words.push(t("queries.library.by", { owner: saved.owner }));
  }
  return words.join(" · ");
}

/** The columns the saved queries sort by: the name, A to Z, unless the address says otherwise. */
const SAVED_SORTS: ListSortSpec<"name" | "updated"> = {
  fields: { name: "asc", updated: "desc" },
  natural: { field: "name", order: "asc" },
};

/** Properties of {@link SavedList}. */
export interface SavedListProps {
  /** The person's principal name. */
  readonly me: string;
  /** The environment the list is filtered by, or null for all of them. */
  readonly filter: string | null;
  /** A number that changes when the list must be read again, as after a save or a delete. */
  readonly reloadKey: number;
}

/**
 * Renders the saved queries, 50 at a time with Show more: the name as the link to the saved
 * query, the environment, who it is for, and when it last changed. Name and Changed sort the
 * list on the server; the order stays in the address.
 *
 * @param props - The person, the filter and the reload key.
 * @param props.me - The person's principal name.
 * @param props.filter - The environment, or null for all.
 * @param props.reloadKey - Changes when the list must be read again.
 * @returns The list with its states.
 */
export function SavedList({ me, filter, reloadKey }: SavedListProps): JSX.Element {
  const { t } = useTranslation();
  const sorting = useListSort(SAVED_SORTS);
  const sort = sorting.choice;
  const pages = useLibraryPages(
    filter,
    reloadKey,
    (cursor) => listSavedQueries(filter, cursor, sort),
    JSON.stringify(sort),
  );
  const rows = pages.items ?? [];
  return (
    <ViewList
      pages={pages}
      rowCount={rows.length}
      empty={t("queries.library.noSaved")}
      region={t("queries.library.savedRegion")}
      rowsOpen
      scroll="queries-list-scroll"
      head={
        <tr>
          <SortHeader {...sorting.header("name")}>{t("queries.library.name")}</SortHeader>
          <SortHeader {...sorting.header("updated")}>{t("queries.library.changed")}</SortHeader>
          <th scope="col">
            <span className="visually-hidden">{t("list.open")}</span>
          </th>
        </tr>
      }
    >
      {rows.map((saved) => {
        const who = savedWho(t, saved, me);
        return (
          <tr key={saved.id}>
            <td>
              <Link className="row-name" to={savedPath(saved.environment, saved.id)}>
                <VisibleText text={saved.name} />
              </Link>{" "}
              <EnvironmentLabel environment={saved.environment} />
              {who !== "" && <span className="second">{who}</span>}
            </td>
            <td>
              <When iso={saved.updated_at} never="" />
            </td>
            <td>
              <span className="chevron" aria-hidden="true">
                ›
              </span>
            </td>
          </tr>
        );
      })}
    </ViewList>
  );
}
