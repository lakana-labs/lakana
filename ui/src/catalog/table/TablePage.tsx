/**
 * A table's page below its heading: the table is read, recorded as opened, and shown with its
 * tabs, or the reason it cannot be shown.
 *
 * @packageDocumentation
 */
import { type JSX, lazy, type RefObject, Suspense, useCallback, useEffect, useRef } from "react";
import { useTranslation } from "react-i18next";
import { Link, Navigate } from "react-router";

import { getArtifact, getTable, recordOpened, type TableRef } from "../../api";
import { useMe } from "../../shell/me";
import { type Resource, useResource } from "../../shell/useResource";
import type { ArtifactOut, TableOut } from "../../api";
import { catalogPath, tablePath, type TableTab, tabsFor } from "../paths";
import { DEFAULT_VIEW } from "../view";
import { ColumnsTab } from "./ColumnsTab";
import { Overview } from "./Overview";
import { TableHeader } from "./TableHeader";
import { TableProvider } from "./TableContext";
import "./tableStrings";
import { TableSkeleton } from "./TableSkeleton";

// React Aria's grid is most of the tab's code, so the tab loads only when it is opened.
const PreviewTab = lazy(async () => {
  const { PreviewTab: loaded } = await import("./PreviewTab");
  return { default: loaded };
});

// The drawing, its layout and its strings load only when the Lineage tab is opened.
const LineageTab = lazy(() => import("./LineageTab"));

// References, Health and Access load only when their tab is opened.
const HealthTab = lazy(() => import("./HealthTab"));
const AccessTab = lazy(() => import("./AccessTab"));
const ReferencesTab = lazy(() => import("./ReferencesTab"));

/** What the page reads: the table, and for an artifact its own entry as well. */
export interface LoadedTable {
  /** The table as the server answered it. */
  readonly table: TableOut;
  /** The artifact, read only when the table's kind is `ARTIFACT`; else null. */
  readonly artifact: ArtifactOut | null;
}

/**
 * Reads the table, then, for an artifact, the artifact too: both need the right to read it, so
 * either failure fails the page.
 *
 * @param ref - The table the address names.
 * @returns The table and the artifact, or null for the artifact of anything else.
 */
async function readTable(ref: TableRef): Promise<LoadedTable> {
  const table = await getTable(ref);
  return { table, artifact: table.kind === "ARTIFACT" ? await getArtifact(ref) : null };
}

/** Properties of {@link TablePage}. */
export interface TablePageProps {
  /** The table the address names. */
  readonly place: TableRef;
  /** The tab the address names. */
  readonly tab: TableTab;
  /** The page's heading, which receives the focus when the table is read again. */
  readonly headingRef: RefObject<HTMLHeadingElement | null>;
  /** Told whether the table is still being read, so the frame can say it is loading once. */
  readonly onWaiting: (waiting: boolean) => void;
  /** Told the table and its artifact once they are read, and null while they are read or cannot be shown. */
  readonly onLoaded: (loaded: LoadedTable | null) => void;
}

/** Properties of {@link Problem}. */
interface ProblemProps {
  /** The table that could not be read. */
  readonly place: TableRef;
  /** What the read answered. */
  readonly resource: Resource<LoadedTable>;
  /** The page's heading. */
  readonly headingRef: RefObject<HTMLHeadingElement | null>;
}

/**
 * Says why the table cannot be shown, with the next step: no table by that name, whether it never
 * existed or was dropped or renamed (404, one answer for both), no right
 * to read it or no table by that name (403, the server's one answer for both), storage that cannot be reached (503), or the server's own
 * sentence for any other failure.
 *
 * @param props - The table, the failed read and the heading.
 * @param props.place - The table that could not be read.
 * @param props.resource - What the read answered.
 * @param props.headingRef - The page's heading.
 * @returns The sentence and the way on.
 */
function Problem({ place, resource, headingRef }: ProblemProps): JSX.Element {
  const { t } = useTranslation();
  const status = resource.failure?.status;
  const words = { namespace: place.namespace, name: place.name, environment: place.environment };
  if (status === 404 || status === 403) {
    return (
      <div className="table-problem">
        <p role="alert">{t(status === 404 ? "table.notFound" : "table.forbidden", words)}</p>
        <Link className="button" to={catalogPath(DEFAULT_VIEW)}>
          {t("table.toCatalog")}
        </Link>
      </div>
    );
  }
  return (
    <div className="table-problem">
      <p role="alert">{status === 503 ? t("table.unavailable") : resource.error}</p>
      <button
        type="button"
        className="button"
        onClick={() => {
          // The button goes once the table is read: the heading keeps the focus.
          headingRef.current?.focus();
          resource.reload();
        }}
      >
        {t("table.retry")}
      </button>
    </div>
  );
}

/**
 * Reads the table when the page opens, and an artifact's own entry with it, records it as opened
 * once, and shows its header and the tab the address names. A tab its kind does not have gives
 * way to its Overview.
 *
 * @param props - The table, the tab and the heading.
 * @param props.place - The table the address names.
 * @param props.tab - The tab the address names.
 * @param props.headingRef - The page's heading.
 * @param props.onWaiting - Told whether the table is still being read.
 * @param props.onLoaded - Told the table once it is read.
 * @returns The page's content.
 */
export function TablePage({
  place,
  tab,
  headingRef,
  onWaiting,
  onLoaded,
}: TablePageProps): JSX.Element {
  const { t } = useTranslation();
  const me = useMe();
  const { environment, namespace, name } = place;
  const load = useCallback(
    () => readTable({ environment, namespace, name }),
    [environment, namespace, name],
  );
  const resource = useResource(load);
  const read = resource.data;
  const loaded = read?.table;
  const waiting = loaded === undefined && resource.error === null;
  useEffect(() => {
    onWaiting(waiting);
  }, [waiting, onWaiting]);
  useEffect(() => {
    onLoaded(read ?? null);
  }, [read, onLoaded]);
  const recorded = useRef(false);
  useEffect(() => {
    if (loaded !== undefined && !recorded.current) {
      recorded.current = true;
      // Recently opened is a convenience: a failed record is not the person's concern.
      recordOpened(me.name, "table", environment, `${loaded.namespace}.${loaded.name}`).catch(
        () => undefined,
      );
    }
  }, [loaded, me.name, environment]);

  if (read === undefined) {
    return resource.error === null ? (
      <TableSkeleton />
    ) : (
      <Problem place={place} resource={resource} headingRef={headingRef} />
    );
  }
  if (!tabsFor(read.table.kind).includes(tab)) {
    // Preview on an artifact, or Versions on a table: the entry's Overview, in place of the address.
    return <Navigate replace to={tablePath(environment, namespace, name)} />;
  }
  return (
    <TableProvider place={place} loaded={read.table} artifact={read.artifact}>
      <TableHeader />
      <section className="table-tab" aria-label={t(`table.tabs.${tab}`)}>
        {tab === "overview" && <Overview />}
        {tab === "columns" && <ColumnsTab />}
        {tab === "preview" && (
          <Suspense fallback={<p role="status">{t("table.preview.starting")}</p>}>
            <PreviewTab />
          </Suspense>
        )}
        {tab === "lineage" && (
          <Suspense fallback={<p role="status">{t("table.loadingTab")}</p>}>
            <LineageTab />
          </Suspense>
        )}
        {tab === "references" && (
          <Suspense fallback={<p role="status">{t("table.loadingTab")}</p>}>
            <ReferencesTab />
          </Suspense>
        )}
        {tab === "health" && (
          <Suspense fallback={<p role="status">{t("table.loadingTab")}</p>}>
            <HealthTab />
          </Suspense>
        )}
        {tab === "access" && (
          <Suspense fallback={<p role="status">{t("table.loadingTab")}</p>}>
            <AccessTab />
          </Suspense>
        )}
      </section>
    </TableProvider>
  );
}
