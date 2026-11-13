/**
 * The route of a table's page: the heading and the breadcrumb come from the address at once,
 * and the table's own page loads on demand beneath them.
 *
 * @packageDocumentation
 */
import { type JSX, lazy, Suspense, useRef } from "react";
import { useTranslation } from "react-i18next";
import { useParams } from "react-router";

import type { TableRef } from "../../api";
import { NotFound } from "../../shell/NotFound";
import { Page } from "../../shell/Page";
import { catalogPath, TABLE_TABS, type TableTab } from "../paths";
import { DEFAULT_VIEW } from "../view";
import { TableSkeleton } from "./TableSkeleton";
import "./table.css";

const TablePage = lazy(async () => {
  const { TablePage: loaded } = await import("./TablePage");
  return { default: loaded };
});

/**
 * The tab an address names: Overview for none, else the segment when a tab has it.
 *
 * @param segment - What follows the table's name in the address.
 * @returns The tab, or null for a segment that names none (Overview has no segment).
 */
function tabOf(segment: string | undefined): TableTab | null {
  if (segment === undefined || segment === "") {
    return "overview";
  }
  return TABLE_TABS.find((tab) => tab !== "overview" && tab === segment) ?? null;
}

/** Properties of {@link TableView}. */
interface TableViewProps {
  /** The table the address names. */
  readonly place: TableRef;
  /** The tab the address names. */
  readonly tab: TableTab;
}

/**
 * The page frame: the breadcrumb and the heading, which stay put while the chunk, the table and
 * the tabs come and go, so that the focus the shell puts on the heading is never lost.
 *
 * @param props - The table and the tab.
 * @param props.place - The table the address names.
 * @param props.tab - The tab the address names.
 * @returns The page.
 */
function TableView({ place, tab }: TableViewProps): JSX.Element {
  const { t } = useTranslation();
  const heading = useRef<HTMLHeadingElement>(null);
  const { environment, namespace, name } = place;
  return (
    <Page
      title={name}
      headingRef={heading}
      trail={[
        { to: catalogPath(DEFAULT_VIEW), label: t("areas.catalog.name") },
        { to: catalogPath({ ...DEFAULT_VIEW, environment }), label: environment },
        { to: catalogPath({ ...DEFAULT_VIEW, environment, namespace }), label: namespace },
      ]}
    >
      <Suspense fallback={<TableSkeleton />}>
        <TablePage place={place} tab={tab} headingRef={heading} />
      </Suspense>
    </Page>
  );
}

/**
 * Renders the table the address names, a new page for each table, so that nothing read or
 * recorded for one shows for another; an address with an unknown tab is not found.
 *
 * @returns The page.
 */
export function TableRoute(): JSX.Element {
  const params = useParams();
  const tab = tabOf(params["*"]);
  const place: TableRef = {
    environment: params["env"] as string,
    namespace: params["namespace"] as string,
    name: params["table"] as string,
  };
  if (tab === null) {
    return <NotFound />;
  }
  return (
    <TableView
      key={`${place.environment}/${place.namespace}/${place.name}`}
      place={place}
      tab={tab}
    />
  );
}
