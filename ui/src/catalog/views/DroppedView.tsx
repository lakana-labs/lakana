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
import { ReadFailed, When } from "../cells";
import { tablePath } from "../paths";
import { useCursorPages } from "./useCursorPages";
import "./viewStrings";

/** Properties of {@link DroppedView}. */
export interface DroppedViewProps {
  /** The environment. */
  readonly environment: string;
  /** The dotted namespace; null for the whole environment. */
  readonly namespace: string | null;
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

const SKELETON_ROWS = 3;

/**
 * Renders the list. Undrop acts at once; a protected environment asks for a reason first, and a
 * name that is taken asks for another. The result line takes the focus.
 *
 * @param props - The place.
 * @param props.environment - The environment.
 * @param props.namespace - The dotted namespace; null for all.
 * @returns The view.
 */
export function DroppedView({ environment, namespace }: DroppedViewProps): JSX.Element {
  const { t } = useTranslation();
  const key = `${environment}\u0000${namespace ?? ""}`;
  const pages = useCursorPages(key, (cursor) => listDropped(environment, namespace, cursor));
  const protectedEnvironment = useProtected(environment);
  const fromDrop = (useLocation().state as { dropped?: string } | null)?.dropped;
  const [restored, setRestored] = useState<Result | null>(null);
  const [removed, setRemoved] = useState<ReadonlySet<string>>(new Set());
  const [working, setWorking] = useState<string | null>(null);
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
  };

  const undrop = async (row: DroppedTableOut): Promise<void> => {
    if (protectedEnvironment === true) {
      setDialog({ row, nameTaken: null });
      return;
    }
    setWorking(row.uuid);
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
      setWorking(null);
    }
  };

  const place = namespace ?? environment;
  const reading = pages.items === undefined && pages.error === null;
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
      {reading && (
        <div className="skeleton" aria-hidden="true">
          {Array.from({ length: SKELETON_ROWS }, (_row, index) => (
            <div className="skeleton-row" key={index} />
          ))}
        </div>
      )}
      {pages.items === undefined && pages.error !== null && (
        <ReadFailed message={pages.error} onRetry={pages.reload} />
      )}
      {pages.items !== undefined && rows.length === 0 && !pages.busy && (
        <div className="empty">
          <p>{t("catalog.views.dropped.empty", { place })}</p>
        </div>
      )}
      {rows.length > 0 && (
        <div
          className="list-scroll dropped-scroll"
          role="region"
          aria-label={t("catalog.views.dropped.region")}
          // A scrolling region must be reachable by keyboard (WCAG 2.1.1), so it takes a tab stop.
          // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex
          tabIndex={0}
          ref={table}
        >
          <table className="list list-static">
            <thead>
              <tr>
                <th scope="col">{t("catalog.views.dropped.columns.table")}</th>
                <th scope="col">{t("catalog.views.dropped.columns.dropped")}</th>
                <th scope="col">{t("catalog.views.dropped.columns.until")}</th>
                <th scope="col">
                  <span className="visually-hidden">
                    {t("catalog.views.dropped.columns.actions")}
                  </span>
                </th>
              </tr>
            </thead>
            <tbody>
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
                      <When iso={row.purge_after} never={t("catalog.views.dropped.neverKnown")} />
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
                        pending={working === row.uuid || protectedEnvironment === undefined}
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
            </tbody>
          </table>
        </div>
      )}
      {pages.items !== undefined && pages.error !== null && (
        <p className="step-error" role="alert">
          {pages.error}
        </p>
      )}
      {pages.hasMore && (
        <PendingButton
          className="button"
          pending={pages.busy}
          onPress={() => {
            focusFrom.current = rows.length;
            void pages.loadMore();
          }}
        >
          {t("catalog.showMore")}
        </PendingButton>
      )}
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
