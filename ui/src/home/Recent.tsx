/**
 * Home's Recently opened: the tables, queries, jobs and runs the person opened, kept on the
 * server so the list follows them to another browser.
 *
 * @packageDocumentation
 */
import type { TFunction } from "i18next";
import { type JSX, useCallback, useId } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";

import { listRecent, type RecentOut } from "../api";
import { Loaded } from "../shell/Loaded";
import { useMe } from "../shell/me";
import { useResource } from "../shell/useResource";
import { useWhen } from "../shell/useWhen";
import { objectPath } from "./links";
import { ObjectRow, ObjectTable } from "./ObjectRows";

/** How many characters of an id name a query or a run, as the command line shows them. */
const SHORT_ID = 8;

/**
 * The name a row shows: a table's or a job's own, a query's or a run's short id.
 *
 * @param t - The translation function.
 * @param item - What was opened.
 * @returns The name.
 */
function nameOf(t: TFunction, item: RecentOut): string {
  if (item.kind === "query" || item.kind === "run") {
    return t(`home.recent.${item.kind}Name`, { id: item.ref.slice(0, SHORT_ID) });
  }
  return item.ref;
}

/**
 * Lists what the person opened, newest first; read once when Home opens.
 *
 * @returns The section.
 */
export function Recent(): JSX.Element {
  const { t } = useTranslation();
  const when = useWhen();
  const heading = useId();
  const { name } = useMe();
  const load = useCallback(() => listRecent(name), [name]);
  const recent = useResource(load);
  return (
    <section className="home-section" aria-labelledby={heading}>
      <h2 id={heading}>{t("home.recent.title")}</h2>
      <Loaded resource={recent}>
        {(found) =>
          found.length === 0 ? (
            <p className="empty">
              {t("home.recent.none")} <Link to="/catalog">{t("home.recent.toCatalog")}</Link>
            </p>
          ) : (
            <ObjectTable column={t("home.recent.opened")}>
              {found.map((item) => (
                <ObjectRow
                  key={`${item.kind} ${item.environment} ${item.ref}`}
                  to={objectPath(item.kind, item.environment, item.ref)}
                  name={nameOf(t, item)}
                  environment={item.environment}
                  second={t(`home.recent.kinds.${item.kind}`)}
                  aside={when(item.opened_at)}
                />
              ))}
            </ObjectTable>
          )
        }
      </Loaded>
    </section>
  );
}
