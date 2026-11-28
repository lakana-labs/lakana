/**
 * The Orphans view: the tables no project produces any more, with Drop. It lists the whole
 * environment, whichever namespace is chosen, and says so.
 *
 * @packageDocumentation
 */
import { type JSX, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";

import type { TableRef } from "../../api";
import { type OrphanOut, listOrphans } from "../../api/manage";
import "../actions/actionStrings";
import type { ListSortSpec } from "../../shell/listSort";
import { PendingButton } from "../../shell/PendingButton";
import { SortHeader } from "../../shell/SortHeader";
import { useListSort } from "../../shell/useListSort";
import { markedName, VisibleText } from "../../shell/VisibleText";
import { DropDialog } from "../actions/DropDialog";
import { kindWord, Unknown } from "../cells";
import { catalogPath, splitDotted } from "../paths";
import { DEFAULT_VIEW } from "../view";
import { useCursorPages } from "./useCursorPages";
import { ViewList } from "./ViewList";
import "./viewStrings";

/** The columns the orphans sort by, on the server; their own order is by repository. */
const ORPHAN_SORTS: ListSortSpec<"name" | "kind" | "repository"> = {
  fields: { name: "asc", kind: "asc", repository: "asc" },
  natural: { field: "repository", order: "asc" },
  param: "orphans",
};

/** Properties of {@link OrphansView}. */
export interface OrphansViewProps {
  /** The environment. */
  readonly environment: string;
  /** The dotted namespace the Catalog is on; null for the whole environment. */
  readonly namespace: string | null;
  /** Called after an orphan is dropped, so the tree's counts can be read again. */
  readonly onChanged: () => void;
}

/**
 * Renders the list. An orphan that was dropped and can still be restored links to Dropped; the
 * others offer Drop when the server's `actions` allow it. After a drop the list is read again
 * and the result line takes the focus.
 *
 * @param props - The place.
 * @param props.environment - The environment.
 * @param props.namespace - The dotted namespace; null for all.
 * @param props.onChanged - Called after a drop.
 * @returns The view.
 */
export function OrphansView({ environment, namespace, onChanged }: OrphansViewProps): JSX.Element {
  const { t } = useTranslation();
  const [hidden, setHidden] = useState(0);
  const sorting = useListSort(ORPHAN_SORTS);
  const sort = sorting.choice;
  // A new sort reads the first page again; the rows stay until it arrives.
  const pages = useCursorPages(
    environment,
    async (cursor) => {
      const page = await listOrphans(environment, cursor, sort);
      setHidden(page.hidden);
      return page;
    },
    JSON.stringify(sort),
  );
  const [dropping, setDropping] = useState<{ row: OrphanOut; ref: TableRef } | null>(null);
  const [dropped, setDropped] = useState<string | null>(null);
  const status = useRef<HTMLParagraphElement>(null);

  useEffect(() => {
    if (dropped !== null) {
      status.current?.focus();
    }
  }, [dropped]);

  const rows = pages.items ?? [];
  const shownEnvironment = markedName(environment);
  return (
    <div className="orphans-view">
      <p className="page-purpose">
        {t("catalog.views.orphans.purpose", { environment: shownEnvironment })}
      </p>
      <p>
        {namespace === null
          ? t("catalog.views.orphans.inEnvironment", { environment: shownEnvironment })
          : t("catalog.views.orphans.wholeEnvironment", {
              environment: shownEnvironment,
              namespace: markedName(namespace),
            })}
      </p>
      <p className="catalog-result" role="status" tabIndex={-1} ref={status}>
        {dropped !== null && t("catalog.views.dropped.droppedNow", { table: markedName(dropped) })}
      </p>
      {hidden > 0 && <p>{t("catalog.views.orphans.hidden", { count: hidden })}</p>}
      <ViewList
        pages={pages}
        rowCount={rows.length}
        empty={t("catalog.views.orphans.empty", { environment: shownEnvironment })}
        region={t("catalog.views.orphans.region")}
        head={
          <tr>
            <SortHeader {...sorting.header("name")}>
              {t("catalog.views.orphans.columns.table")}
            </SortHeader>
            <SortHeader {...sorting.header("kind")}>
              {t("catalog.views.orphans.columns.kind")}
            </SortHeader>
            <SortHeader {...sorting.header("repository")}>
              {t("catalog.views.orphans.columns.repository")}
            </SortHeader>
            <th scope="col">
              <span className="visually-hidden">{t("catalog.views.dropped.columns.actions")}</span>
            </th>
          </tr>
        }
      >
        {rows.map((row) => {
          const parts = splitDotted(row.table);
          return (
            <tr key={`${row.repository}\u0000${row.table}`}>
              <td>
                <span className="row-name">
                  <VisibleText text={parts?.name ?? row.table} name />
                </span>
                {parts !== null && (
                  <span className="second">
                    <VisibleText text={parts.namespace} name />
                  </span>
                )}
              </td>
              <td>
                {row.kind === null ? (
                  <Unknown words={t("catalog.notKnown")} />
                ) : (
                  kindWord(t, row.kind, null)
                )}
              </td>
              <td>
                <VisibleText text={row.repository} />
              </td>
              <td>
                {!row.exists && (
                  <>
                    {t("catalog.views.orphans.droppedAlready")}{" "}
                    <Link
                      to={catalogPath({
                        ...DEFAULT_VIEW,
                        environment,
                        namespace: parts?.namespace ?? null,
                        view: "dropped",
                      })}
                      aria-label={t("catalog.views.orphans.restoreFor", {
                        table: markedName(row.table),
                      })}
                    >
                      {t("catalog.views.orphans.restore")}
                    </Link>
                  </>
                )}
                {row.exists && parts !== null && row.actions.includes("drop") && (
                  <PendingButton
                    className="button"
                    aria-label={t("catalog.views.orphans.dropFor", {
                      table: markedName(row.table),
                    })}
                    pending={false}
                    onPress={() => {
                      setDropping({ row, ref: { environment, ...parts } });
                    }}
                  >
                    {t("catalog.views.orphans.drop")}
                  </PendingButton>
                )}
              </td>
            </tr>
          );
        })}
      </ViewList>
      {dropping !== null && (
        <DropDialog
          target={{ ref: dropping.ref, kind: dropping.row.kind }}
          onDone={() => {
            setDropped(dropping.row.table);
            setDropping(null);
            pages.reload();
            onChanged();
          }}
          onCancel={() => {
            setDropping(null);
          }}
        />
      )}
    </div>
  );
}
