/**
 * The grants a person holds, directly and through groups, as a table in its own scrolling
 * region: What I may do and a person's page share it.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";

import type { HeldGrantOut } from "../api/grants";
import { type ListSortSpec, sortRows } from "../shell/listSort";
import { SortHeader } from "../shell/SortHeader";
import { useListSort } from "../shell/useListSort";
import { useWhen } from "../shell/useWhen";
import { VisibleText } from "../shell/VisibleText";
import "./access.css";
import "./accessStrings";
import { groupPath } from "./accessPaths";
import { roleSummary, whatWords, whereText } from "./words";

/** Properties of {@link HeldGrants}. */
export interface HeldGrantsProps {
  /** The grants. */
  readonly grants: readonly HeldGrantOut[];
  /** The name of the scrolling region. */
  readonly label: string;
  /** Whether a group's name opens its page (for an administrator). */
  readonly linkGroups: boolean;
}

/** The columns held grants sort by, in the browser: they are read whole. */
const HELD_SORT: ListSortSpec<"what" | "on" | "through" | "since"> = {
  fields: { what: "asc", on: "asc", through: "asc", since: "desc" },
  natural: null,
  param: "held",
};

/**
 * Renders one row per grant: what it gives (a role with its summary, or an action with the
 * server's note), what it is on, through which group or directly, and since when. A grant has
 * no page, so the rows open nothing.
 *
 * @param props - The grants, the region's name and whether groups are links.
 * @param props.grants - The grants.
 * @param props.label - The region's name.
 * @param props.linkGroups - Whether a group's name is a link.
 * @returns The table.
 */
export function HeldGrants({ grants, label, linkGroups }: HeldGrantsProps): JSX.Element {
  const { t } = useTranslation();
  const when = useWhen();
  const sorting = useListSort(HELD_SORT);
  return (
    <div
      className="list-scroll held-grants-scroll"
      role="region"
      aria-label={label}
      // A scrolling region must be reachable by keyboard (WCAG 2.1.1), so it takes a tab stop.
      // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex
      tabIndex={0}
    >
      <table className="list access-list">
        <thead>
          <tr>
            <SortHeader {...sorting.header("what")}>{t("access.held.what")}</SortHeader>
            <SortHeader {...sorting.header("on")}>{t("access.held.on")}</SortHeader>
            <SortHeader {...sorting.header("through")}>{t("access.held.through")}</SortHeader>
            <SortHeader {...sorting.header("since")}>{t("access.held.since")}</SortHeader>
          </tr>
        </thead>
        <tbody>
          {sortRows(grants, sorting.sort, {
            what: (grant) => whatWords(t, grant),
            on: (grant) => whereText(t, grant),
            // Held directly sorts before every group.
            through: (grant) => (grant.group === null ? "" : grant.group),
            since: (grant) => grant.granted_at,
          }).map((grant) => {
            const second = grant.role !== null ? roleSummary(t, grant.role) : grant.note;
            return (
              <tr key={grant.id}>
                <td>
                  <span className="row-name">{whatWords(t, grant)}</span>
                  {second !== null && <span className="second">{second}</span>}
                </td>
                <td>{whereText(t, grant)}</td>
                <td>
                  {grant.group === null ? (
                    t("access.held.directly")
                  ) : linkGroups ? (
                    <Link to={groupPath(grant.group)}>
                      <VisibleText text={grant.group} />
                    </Link>
                  ) : (
                    <VisibleText text={grant.group} />
                  )}
                </td>
                <td>
                  <time dateTime={grant.granted_at}>{when(grant.granted_at)}</time>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
