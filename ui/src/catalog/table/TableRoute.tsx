/**
 * The route of a table's page: the heading and the breadcrumb come from the address at once,
 * and the table's own page loads on demand beneath them.
 *
 * @packageDocumentation
 */
import { type JSX, lazy, Suspense, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useParams } from "react-router";

import type { ArtifactOut, TableOut, TableRef } from "../../api";
import { NotFound } from "../../shell/NotFound";
import { Page } from "../../shell/Page";
import { ARTIFACT_TABS, catalogPath, TABLE_TABS, type TableTab } from "../paths";
import { DEFAULT_VIEW } from "../view";
import { LoadingStatus, TableSkeleton } from "./TableSkeleton";
import "./table.css";

const TablePage = lazy(async () => {
  const { TablePage: loaded } = await import("./TablePage");
  return { default: loaded };
});

// The menu, its dialogs and their strings load once a table is shown.
const TableActions = lazy(() => import("../actions/TableActions"));

/** Every tab of a table or an artifact: the kind is not known before the read. */
const ANY_TABS: readonly TableTab[] = [...TABLE_TABS, ...ARTIFACT_TABS];

/**
 * The tab an address names: Overview for none, else the segment when a tab of a table or an
 * artifact has it. The page sends a tab its entry does not have to Overview once it is read.
 *
 * @param segment - What follows the table's name in the address.
 * @returns The tab, or null for a segment that names none (Overview has no segment).
 */
function tabOf(segment: string | undefined): TableTab | null {
  if (segment === undefined || segment === "") {
    return "overview";
  }
  return ANY_TABS.find((tab) => tab !== "overview" && tab === segment) ?? null;
}

/** The tabs that show data or a drawing and take the whole window; the others keep the reading width. */
const WIDE_TABS: ReadonlySet<TableTab> = new Set<TableTab>(["preview", "lineage"]);

/** Properties of {@link TableView}. */
interface TableViewProps {
  /** The table the address names. */
  readonly place: TableRef;
  /** The tab the address names. */
  readonly tab: TableTab;
}

/**
 * The page frame: the breadcrumb and the heading, which stay put while the chunk, the table and
 * the tabs come and go, so that the focus the shell puts on the heading is never lost. The
 * Actions menu joins the heading once the table, and an artifact's own entry, are read.
 *
 * @param props - The table and the tab.
 * @param props.place - The table the address names.
 * @param props.tab - The tab the address names.
 * @returns The page.
 */
function TableView({ place, tab }: TableViewProps): JSX.Element {
  const { t } = useTranslation();
  const heading = useRef<HTMLHeadingElement>(null);
  const [waiting, setWaiting] = useState(true);
  const [loaded, setLoaded] = useState<{
    table: TableOut;
    artifact: ArtifactOut | null;
  } | null>(null);
  const { environment, namespace, name } = place;
  return (
    <div className={WIDE_TABS.has(tab) ? "table-page table-page-wide" : "table-page"}>
      <Page
        title={name}
        headingRef={heading}
        action={
          loaded !== null && (
            <Suspense fallback={null}>
              <TableActions place={place} table={loaded.table} artifact={loaded.artifact} />
            </Suspense>
          )
        }
        trail={[
          { to: catalogPath(DEFAULT_VIEW), label: t("areas.catalog.name") },
          { to: catalogPath({ ...DEFAULT_VIEW, environment }), label: environment },
          { to: catalogPath({ ...DEFAULT_VIEW, environment, namespace }), label: namespace },
        ]}
      >
        <Suspense fallback={<TableSkeleton />}>
          <TablePage
            place={place}
            tab={tab}
            headingRef={heading}
            onWaiting={setWaiting}
            onLoaded={setLoaded}
          />
        </Suspense>
        <LoadingStatus waiting={waiting} />
      </Page>
    </div>
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
  const environment = params["env"] as string;
  const namespace = params["namespace"] as string;
  const name = params["table"] as string;
  // One object for one table, so that pressing the tab one is on reads nothing again.
  const place = useMemo<TableRef>(
    () => ({ environment, namespace, name }),
    [environment, namespace, name],
  );
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
