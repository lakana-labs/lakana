/**
 * The switch between the Catalog's views: all tables, dropped tables, tables about to expire
 * and orphans.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";

import { catalogPath } from "../paths";
import { type CatalogView, VIEW_KINDS } from "../view";

/** Properties of {@link ViewSwitch}. */
export interface ViewSwitchProps {
  /** The current view; every link keeps its place, search and sort. */
  readonly view: CatalogView;
}

/**
 * Renders the views as links, so each can be opened in a new tab and linked. The current one
 * carries `aria-current="page"`; a `NavLink` would not, as it ignores the query.
 *
 * @param props - The current view.
 * @param props.view - The current view.
 * @returns The navigation.
 */
export function ViewSwitch({ view }: ViewSwitchProps): JSX.Element {
  const { t } = useTranslation();
  return (
    <nav className="tabs" aria-label={t("catalog.view.label")}>
      {VIEW_KINDS.map((kind) => (
        <Link
          key={kind}
          className="tab"
          to={catalogPath({ ...view, view: kind })}
          {...(kind === view.view ? { "aria-current": "page" as const } : {})}
        >
          {t(`catalog.view.${kind}`)}
        </Link>
      ))}
    </nav>
  );
}
