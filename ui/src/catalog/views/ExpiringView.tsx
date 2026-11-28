/**
 * The Expiring view: the managed tables that expire soon, with when and why.
 *
 * @packageDocumentation
 */
import { type JSX, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";

import { listExpiring } from "../../api/manage";
import type { ListSortSpec } from "../../shell/listSort";
import { SortHeader } from "../../shell/SortHeader";
import { useListSort } from "../../shell/useListSort";
import { markedName, VisibleText } from "../../shell/VisibleText";
import { openFromCell, When } from "../cells";
import { tablePath } from "../paths";
import { retentionWords } from "../retention";
import { useCursorPages } from "./useCursorPages";
import { ViewList } from "./ViewList";
import "./viewStrings";

/** The columns the expiring tables sort by, on the server; their own order is the soonest first. */
const EXPIRING_SORTS: ListSortSpec<"name" | "expires"> = {
  fields: { name: "asc", expires: "asc" },
  natural: { field: "expires", order: "asc" },
  param: "expiring",
};

/** Properties of {@link ExpiringView}. */
export interface ExpiringViewProps {
  /** The environment. */
  readonly environment: string;
  /** The dotted namespace; null for the whole environment. */
  readonly namespace: string | null;
}

/**
 * Renders the list, soonest first as the server sorts it, or by the column pressed. A table already past its expiry says
 * "Due now"; the why is the sentence of the Retention dialog.
 *
 * @param props - The place.
 * @param props.environment - The environment.
 * @param props.namespace - The dotted namespace; null for all.
 * @returns The view.
 */
export function ExpiringView({ environment, namespace }: ExpiringViewProps): JSX.Element {
  const { t } = useTranslation();
  const sorting = useListSort(EXPIRING_SORTS);
  const sort = sorting.choice;
  // A new sort reads the first page again; the rows stay until it arrives.
  const pages = useCursorPages(
    `${environment}\u0000${namespace ?? ""}`,
    (cursor) => listExpiring(environment, namespace, cursor, sort),
    JSON.stringify(sort),
  );
  // The present is read once per mount, so a render never depends on the clock.
  const [now] = useState(Date.now);
  const rows = pages.items ?? [];
  const place = markedName(namespace ?? environment);
  return (
    <div className="expiring-view">
      <p className="page-purpose">{t("catalog.views.expiring.purpose", { place })}</p>
      <ViewList
        pages={pages}
        rowCount={rows.length}
        empty={t("catalog.views.expiring.empty", { place })}
        region={t("catalog.views.expiring.region")}
        head={
          <tr>
            <SortHeader {...sorting.header("name")}>
              {t("catalog.views.expiring.columns.table")}
            </SortHeader>
            <SortHeader {...sorting.header("expires")}>
              {t("catalog.views.expiring.columns.when")}
            </SortHeader>
            {/* The why is a sentence made of the rule, with no order that means something. */}
            <th scope="col">{t("catalog.views.expiring.columns.why")}</th>
          </tr>
        }
      >
        {rows.map((row) => (
          <tr
            key={row.uuid}
            // The time sits above the row's link overlay so the pointer reaches its hover text; a
            // plain click on it is handed to the link. The keyboard has the link.
            onClick={openFromCell}
          >
            <td>
              <Link className="row-name" to={tablePath(environment, row.namespace, row.name)}>
                <VisibleText text={row.name} name />
              </Link>
              <span className="second">
                <VisibleText text={row.namespace} name />
              </span>
            </td>
            <td>
              {row.expires_at !== null && Date.parse(row.expires_at) <= now ? (
                t("catalog.views.expiring.dueNow")
              ) : (
                <When iso={row.expires_at} never={t("catalog.views.dropped.neverKnown")} ahead />
              )}
            </td>
            <td>{retentionWords(t, row)}</td>
          </tr>
        ))}
      </ViewList>
    </div>
  );
}
