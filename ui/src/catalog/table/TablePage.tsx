/**
 * A table's page below its heading: the table is read, recorded as opened, and shown with its
 * tabs, or the reason it cannot be shown.
 *
 * @packageDocumentation
 */
import { type JSX, lazy, type RefObject, Suspense, useCallback, useEffect, useRef } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";

import { getTable, recordOpened, type TableRef } from "../../api";
import { useMe } from "../../shell/me";
import { type Resource, useResource } from "../../shell/useResource";
import type { TableOut } from "../../api";
import { catalogPath, type TableTab } from "../paths";
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
}

/** Properties of {@link Problem}. */
interface ProblemProps {
  /** The table that could not be read. */
  readonly place: TableRef;
  /** What the read answered. */
  readonly resource: Resource<TableOut>;
  /** The page's heading. */
  readonly headingRef: RefObject<HTMLHeadingElement | null>;
}

/**
 * Says why the table cannot be shown, with the next step: a table that is gone (404), no right
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
 * Reads the table when the page opens, records it as opened once, and shows its header and the
 * tab the address names.
 *
 * @param props - The table, the tab and the heading.
 * @param props.place - The table the address names.
 * @param props.tab - The tab the address names.
 * @param props.headingRef - The page's heading.
 * @param props.onWaiting - Told whether the table is still being read.
 * @returns The page's content.
 */
export function TablePage({ place, tab, headingRef, onWaiting }: TablePageProps): JSX.Element {
  const { t } = useTranslation();
  const me = useMe();
  const { environment, namespace, name } = place;
  const load = useCallback(
    () => getTable({ environment, namespace, name }),
    [environment, namespace, name],
  );
  const resource = useResource(load);
  const loaded = resource.data;
  const waiting = loaded === undefined && resource.error === null;
  useEffect(() => {
    onWaiting(waiting);
  }, [waiting, onWaiting]);
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

  if (loaded === undefined) {
    return resource.error === null ? (
      <TableSkeleton />
    ) : (
      <Problem place={place} resource={resource} headingRef={headingRef} />
    );
  }
  return (
    <TableProvider place={place} loaded={loaded}>
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
      </section>
    </TableProvider>
  );
}
