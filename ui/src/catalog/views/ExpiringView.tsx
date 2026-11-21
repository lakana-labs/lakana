/**
 * The Expiring view: the managed tables that expire soon, with when and why.
 *
 * @packageDocumentation
 */
import { type JSX, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";

import { listExpiring } from "../../api/manage";
import { markedName, VisibleText } from "../../shell/VisibleText";
import { openFromCell, When } from "../cells";
import { tablePath } from "../paths";
import { retentionWords } from "../retention";
import { useCursorPages } from "./useCursorPages";
import { ViewList } from "./ViewList";
import "./viewStrings";

/** Properties of {@link ExpiringView}. */
export interface ExpiringViewProps {
  /** The environment. */
  readonly environment: string;
  /** The dotted namespace; null for the whole environment. */
  readonly namespace: string | null;
}

/**
 * Renders the list, soonest first as the server sorts it. A table already past its expiry says
 * "Due now"; the why is the sentence of the Retention dialog.
 *
 * @param props - The place.
 * @param props.environment - The environment.
 * @param props.namespace - The dotted namespace; null for all.
 * @returns The view.
 */
export function ExpiringView({ environment, namespace }: ExpiringViewProps): JSX.Element {
  const { t } = useTranslation();
  const pages = useCursorPages(`${environment}\u0000${namespace ?? ""}`, (cursor) =>
    listExpiring(environment, namespace, cursor),
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
            <th scope="col">{t("catalog.views.expiring.columns.table")}</th>
            <th scope="col">{t("catalog.views.expiring.columns.when")}</th>
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
