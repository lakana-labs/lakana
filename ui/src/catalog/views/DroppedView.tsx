/**
 * The Dropped view: the tables dropped from an environment or one namespace of it that can
 * still be restored, with Undrop.
 *
 * @packageDocumentation
 */
import { type JSX, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useLocation } from "react-router";

import { ApiError, type TableOut } from "../../api";
import { type DroppedTableOut, listDropped, undropTable } from "../../api/manage";
import "../actions/actionStrings";
import { PendingButton } from "../../shell/PendingButton";
import { markedText, VisibleText } from "../../shell/VisibleText";
import { problemText } from "../../sign-in/messages";
import { UndropDialog } from "../actions/UndropDialog";
import { useProtected } from "../actions/useProtected";
import { When } from "../cells";
import { tablePath } from "../paths";
import { useCursorPages } from "./useCursorPages";
import { ViewList } from "./ViewList";
import "./viewStrings";

/** Properties of {@link DroppedView}. */
export interface DroppedViewProps {
  /** The environment. */
  readonly environment: string;
  /** The dotted namespace; null for the whole environment. */
  readonly namespace: string | null;
  /** Called after a table is restored, so the tree's counts can be read again. */
  readonly onChanged: () => void;
}

/** What the result line says, and where its link goes. */
interface Result {
  /** The words: the table, dotted. */
  readonly table: string;
  /** What happened. */
  readonly kind: "dropped" | "restored";
  /** The restored table's page. */
  readonly path?: string;
}

/**
 * Renders the list. Undrop acts at once; a protected environment asks for a reason first, and a
 * name that is taken asks for another. The result line takes the focus.
 *
 * @param props - The place.
 * @param props.environment - The environment.
 * @param props.namespace - The dotted namespace; null for all.
 * @param props.onChanged - Called after a restore.
 * @returns The view.
 */
export function DroppedView({ environment, namespace, onChanged }: DroppedViewProps): JSX.Element {
  const { t } = useTranslation();
  const key = `${environment}\u0000${namespace ?? ""}`;
  const pages = useCursorPages(key, (cursor) => listDropped(environment, namespace, cursor));
  const protectedEnvironment = useProtected(environment);
  const fromDrop = (useLocation().state as { dropped?: string } | null)?.dropped;
  const [restored, setRestored] = useState<Result | null>(null);
  const [removed, setRemoved] = useState<ReadonlySet<string>>(new Set());
  const [working, setWorking] = useState<ReadonlySet<string>>(new Set());
  const [refusals, setRefusals] = useState<Readonly<Record<string, string>>>({});
  const [dialog, setDialog] = useState<{ row: DroppedTableOut; nameTaken: string | null } | null>(
    null,
  );
  const status = useRef<HTMLParagraphElement>(null);
  const table = useRef<HTMLDivElement>(null);
  const focusFrom = useRef<number | null>(null);

  const result: Result | null =
    restored ?? (fromDrop === undefined ? null : { table: fromDrop, kind: "dropped" });
  const rows = (pages.items ?? []).filter((row) => !removed.has(row.uuid));

  useEffect(() => {
    if (result !== null) {
      status.current?.focus();
    }
    // The focus follows a new result, not every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [result?.table, result?.kind]);

  useEffect(() => {
    const from = focusFrom.current;
    if (from !== null && rows.length > from) {
      focusFrom.current = null;
      table.current?.querySelectorAll<HTMLElement>("tbody .row-name")[from]?.focus();
    } else if (from !== null && !pages.busy && !pages.hasMore) {
      // Show more came back empty and is gone: the focus goes to the result line, not the page.
      focusFrom.current = null;
      status.current?.focus();
    }
  });

  const finished = (row: DroppedTableOut, back: TableOut): void => {
    setRemoved((previous) => new Set([...previous, row.uuid]));
    setRestored({
      table: `${back.namespace}.${back.name}`,
      kind: "restored",
      path: tablePath(environment, back.namespace, back.name),
    });
    setDialog(null);
    onChanged();
  };

  const undrop = async (row: DroppedTableOut): Promise<void> => {
    if (protectedEnvironment === true) {
      setDialog({ row, nameTaken: null });
      return;
    }
    setWorking((previous) => new Set([...previous, row.uuid]));
    setRefusals((previous) => ({ ...previous, [row.uuid]: "" }));
    try {
      finished(
        row,
        await undropTable(
          { environment, namespace: row.namespace, name: row.name },
          { newName: null, breakGlassReason: null },
        ),
      );
    } catch (error) {
      if (error instanceof ApiError && error.type === "AlreadyExistsError") {
        setDialog({ row, nameTaken: error.detail });
      } else {
        const sentence = problemText(t, error);
        setRefusals((previous) => ({ ...previous, [row.uuid]: sentence }));
      }
    } finally {
      setWorking((previous) => new Set([...previous].filter((uuid) => uuid !== row.uuid)));
    }
  };

  const place = namespace ?? environment;
  return (
    <div className="dropped-view">
      <p className="page-purpose">
        {namespace === null
          ? t("catalog.views.dropped.inEnvironment", { environment })
          : t("catalog.views.dropped.fromNamespace", { namespace })}
      </p>
      <p className="catalog-result" role="status" tabIndex={-1} ref={status}>
        {result !== null && (
          <>
            {t(`catalog.views.dropped.${result.kind === "dropped" ? "droppedNow" : "restored"}`, {
              table: markedText(result.table),
            })}
            {result.path !== undefined && (
              <>
                {" "}
                <Link to={result.path}>{t("catalog.views.dropped.open")}</Link>
              </>
            )}
          </>
        )}
      </p>
      <ViewList
        pages={pages}
        rowCount={rows.length}
        empty={t("catalog.views.dropped.empty", { place })}
        region={t("catalog.views.dropped.region")}
        regionRef={table}
        onMore={() => {
          focusFrom.current = rows.length;
        }}
        head={
          <tr>
            <th scope="col">{t("catalog.views.dropped.columns.table")}</th>
            <th scope="col">{t("catalog.views.dropped.columns.dropped")}</th>
            <th scope="col">{t("catalog.views.dropped.columns.until")}</th>
            <th scope="col">
              <span className="visually-hidden">{t("catalog.views.dropped.columns.actions")}</span>
            </th>
          </tr>
        }
      >
        {rows.map((row) => (
          <tr key={row.uuid}>
            <td>
              <span className="row-name" tabIndex={-1}>
                <VisibleText text={row.name} />
              </span>
              <span className="second">
                <VisibleText text={row.namespace} />
              </span>
              {(refusals[row.uuid] ?? "") !== "" && (
                <p className="step-error" role="alert">
                  {refusals[row.uuid]}
                </p>
              )}
            </td>
            <td>
              <When iso={row.dropped_at} never={t("catalog.views.dropped.neverKnown")} />
            </td>
            <td>
              {row.restorable ? (
                <When iso={row.purge_after} never={t("catalog.views.dropped.neverKnown")} ahead />
              ) : (
                t("catalog.views.dropped.gone")
              )}
            </td>
            <td>
              {row.restorable && row.actions.includes("undrop") && (
                <PendingButton
                  className="button"
                  aria-label={t("table.actions.undrop.for", {
                    table: markedText(`${row.namespace}.${row.name}`),
                  })}
                  pending={working.has(row.uuid) || protectedEnvironment === undefined}
                  onPress={() => {
                    void undrop(row);
                  }}
                >
                  {t("table.actions.undrop.button")}
                </PendingButton>
              )}
            </td>
          </tr>
        ))}
      </ViewList>
      {dialog !== null && (
        <UndropDialog
          row={dialog.row}
          environment={environment}
          nameTaken={dialog.nameTaken}
          onDone={(back) => {
            finished(dialog.row, back);
          }}
          onCancel={() => {
            setDialog(null);
          }}
        />
      )}
    </div>
  );
}
