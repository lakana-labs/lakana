/**
 * Settings › Environments: the environments one may read, each opening its page, with its usage
 * for those who manage it; below them the deleted environments one manages; and for those who
 * manage environments on the instance the way to create one.
 *
 * @packageDocumentation
 */
import {
  type JSX,
  lazy,
  type RefObject,
  Suspense,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import { Button } from "react-aria-components";
import { useTranslation } from "react-i18next";
import { Link as RouterLink, useLocation } from "react-router";

import {
  type EnvironmentOut,
  type EnvironmentUsageOut,
  getUsage,
  listEnvironmentsFull,
} from "../api/environments";
import { formatBytes } from "../catalog/format";
import { ViewList } from "../catalog/views/ViewList";
import { inPool, READS_IN_FLIGHT } from "../shell/inPool";
import { type Resource, useResource } from "../shell/useResource";
import { useWhen } from "../shell/useWhen";
import { type DeletedRow, deletionKey, readDeleted } from "./deletedRows";
import type { DeletedAction, DeletedState } from "./EnvironmentDialogs";
import { VisibleText } from "../shell/VisibleText";
import "./environments.css";
import { environmentPath } from "./environmentPaths";
import "./environmentStrings";
import { managesEnvironment, mayCreateEnvironments, type SettingsViewer } from "./places";
import { SettingsFrame } from "./SettingsFrame";
import "./settingsStrings";
import { storageSummary } from "./storageSummary";
import { useLeave } from "./useLeave";
import { useSettingsViewer } from "./viewer";
import { useWholePages } from "./wholePages";

const P = "settings.environmentPages";

// Create environment loads when it is first opened, and the deleted environments only for
// those who manage one, so the list stays small.
const CreateEnvironmentDialog = lazy(async () => ({
  default: (await import("./EnvironmentDialogs")).CreateEnvironmentDialog,
}));

const DeletedEnvironments = lazy(async () => ({
  default: (await import("./DeletedEnvironments")).DeletedEnvironments,
}));

/**
 * Where the focus goes once a list is read again after a change: a restored environment's link
 * once the live list has its new answer, else the deleted environments' heading (or the place's,
 * when none is left) once theirs has.
 */
interface FocusTarget {
  /** The restored environment, or null for the deleted environments' heading. */
  readonly link: string | null;
  /** The answer read before the change; the focus moves once a newer one is in. */
  readonly before: unknown;
}

/** One environment's usage: read, or failed; absent while on its way. */
type UsageRead = EnvironmentUsageOut | "failed";

/**
 * Reads the usage of each environment named, at most {@link READS_IN_FLIGHT} at once; a failed
 * read is kept as failed and fails nothing else.
 *
 * @param names - The environments, or null until they are known.
 * @returns Each environment's read, by name.
 */
function useUsages(names: readonly string[] | null): ReadonlyMap<string, UsageRead> {
  const [usages, setUsages] = useState<ReadonlyMap<string, UsageRead>>(new Map());
  const key = names === null ? null : names.join("\n");
  useEffect(() => {
    if (key === null || key === "") return;
    void inPool(key.split("\n"), READS_IN_FLIGHT, async (name) => {
      let read: UsageRead;
      try {
        read = await getUsage(name);
      } catch {
        read = "failed";
      }
      // After the page is gone, React drops the update.
      setUsages((previous) => new Map(previous).set(name, read));
    });
  }, [key]);
  return usages;
}

/** Properties of {@link UsageCell}. */
interface UsageCellProps {
  /** The environment's read, or undefined while it is on its way or not asked for. */
  readonly read: UsageRead | undefined;
}

/**
 * An environment's total: "at least" before it when some sizes are not measured yet, a dash while
 * unread, and the quiet sentence when it could not be read.
 *
 * @param props - The read.
 * @param props.read - The environment's read.
 * @returns The cell's content.
 */
function UsageCell({ read }: UsageCellProps): JSX.Element {
  const { t, i18n } = useTranslation();
  if (read === undefined) return <>{t(`${P}.unread`)}</>;
  if (read === "failed")
    return <span className="environment-usage-failed">{t(`${P}.usageFailed`)}</span>;
  const size = formatBytes(read.total_bytes, i18n.language);
  return <>{read.complete ? size : t(`${P}.atLeast`, { size })}</>;
}

/**
 * The retention of an environment's tables in words.
 *
 * @param props - The environment.
 * @param props.environment - The environment.
 * @returns The words.
 */
export function RetentionWords({
  environment,
}: {
  /** The environment. */
  readonly environment: EnvironmentOut;
}): JSX.Element {
  const { t } = useTranslation();
  return (
    <>
      {environment.retention_days === null
        ? t(`${P}.keepsTables`)
        : t(`${P}.expires`, { count: environment.retention_days })}
    </>
  );
}

/**
 * The protected mark: the dot and the word in the colour of danger, never the colour alone.
 *
 * @returns The mark.
 */
export function ProtectedMark(): JSX.Element {
  const { t } = useTranslation();
  return (
    <span className="env-protected">
      <span aria-hidden="true">● </span>
      {t(`${P}.protected`)}
    </span>
  );
}

/** Properties of {@link EnvironmentList}. */
interface EnvironmentListProps {
  /** The live environments' read. */
  readonly read: Resource<readonly EnvironmentOut[]>;
  /** What the rules know about the person. */
  readonly viewer: SettingsViewer;
  /** The place's heading, focused after Try again. */
  readonly heading: RefObject<HTMLHeadingElement | null>;
}

/**
 * The environments in their own scrolling region, or the sentence that there is none. Each
 * name's link carries the name, so a restored environment's link can take the focus.
 *
 * @param props - The read, the viewer and the heading.
 * @param props.read - The live environments' read.
 * @param props.viewer - What the rules know about the person.
 * @param props.heading - The place's heading.
 * @returns The list.
 */
function EnvironmentList({ read, viewer, heading }: EnvironmentListProps): JSX.Element {
  const { t } = useTranslation();
  const rows = read.data ?? [];
  const managed = rows.filter((environment) => managesEnvironment(viewer, environment.name));
  const pages = useWholePages(read, heading);
  const usages = useUsages(
    read.data === undefined ? null : managed.map((environment) => environment.name),
  );
  if (read.data?.length === 0) {
    return (
      <div className="empty">
        <p>{t(mayCreateEnvironments(viewer) === true ? `${P}.noneCreator` : `${P}.noneReader`)}</p>
      </div>
    );
  }
  const showsUsage = managed.length > 0;
  return (
    <ViewList
      pages={pages}
      rowCount={rows.length}
      empty=""
      region={t(`${P}.region`)}
      rowsOpen
      scroll="environments-scroll"
      head={
        <tr>
          <th scope="col">{t("list.name")}</th>
          <th scope="col">{t(`${P}.protectionHead`)}</th>
          <th scope="col">{t(`${P}.storageHead`)}</th>
          <th scope="col">{t(`${P}.retentionHead`)}</th>
          {showsUsage && (
            <th scope="col" className="number">
              {t(`${P}.usageHead`)}
            </th>
          )}
          <th scope="col">
            <span className="visually-hidden">{t("list.open")}</span>
          </th>
        </tr>
      }
    >
      {rows.map((environment) => (
        <tr key={environment.name}>
          <td>
            <RouterLink
              className="row-name"
              to={environmentPath(environment.name)}
              data-environment={environment.name}
            >
              <VisibleText text={environment.name} />
            </RouterLink>
          </td>
          <td>{environment.is_protected && <ProtectedMark />}</td>
          <td className="environment-storage">{storageSummary(t, environment)}</td>
          <td>
            <RetentionWords environment={environment} />
          </td>
          {showsUsage && (
            <td className="number">
              <UsageCell read={usages.get(environment.name)} />
            </td>
          )}
          <td>
            <span className="chevron" aria-hidden="true">
              ›
            </span>
          </td>
        </tr>
      ))}
    </ViewList>
  );
}

/**
 * Renders the purpose, the list, the deleted environments for those who manage one, and for
 * those who manage environments on the instance the way to create one.
 *
 * @returns The place.
 */
export function EnvironmentsPlace(): JSX.Element {
  const { t } = useTranslation();
  const when = useWhen();
  const viewer = useSettingsViewer();
  const creates = mayCreateEnvironments(viewer) === true;
  // The place is shown once the facts are read, so the managed environments are known.
  const managesAny = creates || (viewer.managedEnvironments as readonly string[]).length > 0;
  const heading = useRef<HTMLHeadingElement>(null);
  const read = useResource(listEnvironmentsFull);
  const arrived = (useLocation().state as Partial<DeletedState> | null)?.deleted;
  // The deletions whose files were deleted here, by deletionKey: due whatever the browser's clock
  // says. A deletion with the files on the environment's page counts as one.
  const [purged] = useState(
    () =>
      new Set(
        arrived?.restorableUntil === null
          ? [deletionKey({ name: arrived.name, deleted_at: arrived.deletedAt })]
          : [],
      ),
  );
  const loadDeleted = useCallback(
    (): Promise<readonly DeletedRow[]> => (managesAny ? readDeleted(purged) : Promise.resolve([])),
    [managesAny, purged],
  );
  const deleted = useResource(loadDeleted);
  const [creating, setCreating] = useState(false);
  const closeCreate = (): void => {
    setCreating(false);
  };
  const leave = useLeave(closeCreate);
  const [note, setNote] = useState<string | null>(() =>
    arrived === undefined
      ? null
      : arrived.restorableUntil === null
        ? t(`${P}.deletedPurged`, { name: arrived.name })
        : t(`${P}.deletedKept`, { name: arrived.name, when: when(arrived.restorableUntil) }),
  );
  const focus = useRef<FocusTarget | null>(null);
  useEffect(() => {
    const target = focus.current;
    if (target === null || (target.link === null ? deleted.data : read.data) === target.before) {
      return;
    }
    focus.current = null;
    const element =
      target.link === null
        ? document.getElementById("deleted")
        : document.querySelector<HTMLElement>(`a[data-environment="${CSS.escape(target.link)}"]`);
    (element ?? heading.current)?.focus();
  }, [read.data, deleted.data]);
  const changed = (action: DeletedAction, environment: EnvironmentOut): void => {
    const { name } = environment;
    if (action === "purgeFiles") purged.add(deletionKey(environment));
    setNote(t(`${P}.done.${action}`, { name }));
    focus.current =
      action === "restore"
        ? { link: name, before: read.data }
        : { link: null, before: deleted.data };
    read.reload();
    deleted.reload();
  };
  return (
    <SettingsFrame
      title={t("settings.places.environments")}
      headingRef={heading}
      action={
        creates && (
          <Button
            className="button button-primary"
            onPress={() => {
              setCreating(true);
            }}
          >
            {t(`${P}.create`)}
          </Button>
        )
      }
    >
      {creating && (
        <Suspense fallback={null}>
          <CreateEnvironmentDialog onClose={closeCreate} onLeave={leave} />
        </Suspense>
      )}
      <p className="page-purpose">{t(`${P}.intro`)}</p>
      <p className="step-status" role="status">
        {note}
      </p>
      <EnvironmentList read={read} viewer={viewer} heading={heading} />
      {managesAny && (
        <Suspense fallback={null}>
          <DeletedEnvironments read={deleted} onChanged={changed} />
        </Suspense>
      )}
    </SettingsFrame>
  );
}
