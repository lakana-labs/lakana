/**
 * The deleted environments of Settings › Environments, below the list, for those who manage one:
 * when each was deleted and when its files go, with Restore, Delete the files and, for storage
 * Lakana cannot reach, Remove from the catalog.
 *
 * @remarks
 * It loads on its own, only for those who manage an environment.
 * @packageDocumentation
 */
import { type ComponentType, type JSX, lazy, Suspense, useRef, useState } from "react";
import { Button } from "react-aria-components";
import { useTranslation } from "react-i18next";

import type { EnvironmentOut } from "../api/environments";
import { ViewList } from "../catalog/views/ViewList";
import type { Resource } from "../shell/useResource";
import { useWhen } from "../shell/useWhen";
import { markedText, VisibleText } from "../shell/VisibleText";
import type { DeletedAction, DeletedEnvironmentDialogProps } from "./EnvironmentDialogs";
import "./environments.css";
import "./environmentStrings";
import { managesEnvironment, mayCreateEnvironments } from "./places";
import { storageScheme } from "./storageSummary";
import { useSettingsViewer } from "./viewer";
import { useWholePages } from "./wholePages";

const P = "settings.environmentPages";

const DeletedEnvironmentDialog: ComponentType<DeletedEnvironmentDialogProps> = lazy(async () => ({
  default: (await import("./EnvironmentDialogs")).DeletedEnvironmentDialog,
}));

/** The storage kinds Lakana reaches: a deleted environment in one keeps its file purge. */
const REACHABLE = new Set(["file", "s3", "abfs"]);

/**
 * Whether the server may remove a deleted environment from the catalog: its root is in a scheme
 * Lakana never reached, and not an fsspec chain (older releases wrote to the part before `::`).
 * The server's 409 stays the authority.
 *
 * @param root - The environment's storage root.
 * @returns The answer.
 */
export function unreachable(root: string): boolean {
  return !root.includes("::") && !REACHABLE.has(storageScheme(root));
}

/** Properties of {@link DeletedEnvironments}. */
export interface DeletedEnvironmentsProps {
  /** The deleted environments' read. */
  readonly read: Resource<readonly EnvironmentOut[]>;
  /** Called once an action is done, so the place reads both lists again. */
  readonly onChanged: (action: DeletedAction, name: string) => void;
}

/**
 * The deleted environments the person manages, static rows with their dates and actions;
 * nothing when there are none.
 *
 * @param props - The read and the handler.
 * @param props.read - The deleted environments' read.
 * @param props.onChanged - Called once an action is done.
 * @returns The section, or nothing.
 */
export function DeletedEnvironments({
  read,
  onChanged,
}: DeletedEnvironmentsProps): JSX.Element | null {
  const { t } = useTranslation();
  const when = useWhen();
  const viewer = useSettingsViewer();
  const removes = mayCreateEnvironments(viewer) === true;
  const heading = useRef<HTMLHeadingElement>(null);
  const [open, setOpen] = useState<{
    readonly action: DeletedAction;
    readonly environment: EnvironmentOut;
  } | null>(null);
  const rows = (read.data ?? []).filter((environment) =>
    managesEnvironment(viewer, environment.name),
  );
  const pages = useWholePages(read, heading);
  if (read.data !== undefined && rows.length === 0) return null;
  /**
   * One action's button on a row.
   *
   * @param environment - The row's environment.
   * @param action - The action.
   * @returns The button.
   */
  const button = (environment: EnvironmentOut, action: DeletedAction): JSX.Element => (
    <Button
      className="button"
      aria-label={t(`${P}.${action}Of`, { name: markedText(environment.name) })}
      onPress={() => {
        setOpen({ action, environment });
      }}
    >
      {t(`${P}.${action}`)}
    </Button>
  );
  return (
    <section className="environments-deleted">
      <h2 id="deleted" tabIndex={-1} ref={heading}>
        {t(`${P}.deletedTitle`)}
      </h2>
      <p className="page-purpose">{t(`${P}.deletedIntro`)}</p>
      <ViewList
        pages={pages}
        rowCount={rows.length}
        empty=""
        region={t(`${P}.deletedRegion`)}
        scroll="environments-scroll"
        head={
          <tr>
            <th scope="col">{t("list.name")}</th>
            <th scope="col">{t(`${P}.deletedAt`)}</th>
            <th scope="col">{t(`${P}.purgeAfter`)}</th>
            <th scope="col">{t(`${P}.actionsHead`)}</th>
          </tr>
        }
      >
        {rows.map((environment) => (
          <tr key={environment.name}>
            <td>
              <span className="row-name">
                <VisibleText text={environment.name} />
              </span>
            </td>
            {/* The server answers both dates for every deleted environment. */}
            <td>{when(environment.deleted_at as string)}</td>
            <td>{when(environment.purge_after as string)}</td>
            <td>
              <div className="environment-actions">
                {button(environment, "restore")}
                {button(environment, "purgeFiles")}
                {removes &&
                  unreachable(environment.storage_root) &&
                  button(environment, "removeRows")}
              </div>
            </td>
          </tr>
        ))}
      </ViewList>
      {open !== null && (
        <Suspense fallback={null}>
          <DeletedEnvironmentDialog
            action={open.action}
            environment={open.environment}
            onDone={() => {
              setOpen(null);
              onChanged(open.action, open.environment.name);
            }}
            onClose={() => {
              setOpen(null);
            }}
          />
        </Suspense>
      )}
    </section>
  );
}
