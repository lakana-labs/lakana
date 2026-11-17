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
import { ViewList } from "../catalog/views/ViewList";
import { EnvironmentLabel } from "../shell/EnvironmentLabel";
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
 * query, the environment, and who it is for.
 *
 * @param props - The person, the filter and the reload key.
 * @param props.me - The person's principal name.
 * @param props.filter - The environment, or null for all.
 * @param props.reloadKey - Changes when the list must be read again.
 * @returns The list with its states.
 */
export function SavedList({ me, filter, reloadKey }: SavedListProps): JSX.Element {
  const { t } = useTranslation();
  const pages = useLibraryPages(filter, reloadKey, (cursor) => listSavedQueries(filter, cursor));
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
          <th scope="col">{t("queries.library.name")}</th>
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
