/**
 * What every tab of a table's page shares: the table, what the person may do with it, and the
 * way to show a changed table.
 *
 * @packageDocumentation
 */
import {
  createContext,
  type JSX,
  type ReactNode,
  useCallback,
  useContext,
  useMemo,
  useRef,
  useState,
} from "react";

import {
  type ArtifactOut,
  getArtifact,
  myPermissions,
  type PermissionsOut,
  type TableOut,
  type TableRef,
} from "../../api";
import { useMe } from "../../shell/me";
import { type Resource, useResource } from "../../shell/useResource";

/** What the tabs of a table's page share. */
export interface TableData {
  /** Where the table is. */
  readonly ref: TableRef;
  /** The table as it is now, with the person's own changes. */
  readonly table: TableOut;
  /** Shows a table the server answered after a change, in place of the one read at first. */
  readonly replaceTable: (next: TableOut) => void;
  /** What the person may do with the table; read beside it. */
  readonly permissions: Resource<PermissionsOut>;
  /** Whether the person may edit the table's texts: its owner, or whoever may drop it. */
  readonly mayEdit: boolean;
  /** The artifact, for an artifact's page, as last read; null for any other kind. */
  readonly artifact: ArtifactOut | null;
  /**
   * Reads the artifact again and shows it in place of the one read with the table, which stays
   * as it is; a failed read keeps the artifact shown. Nothing for any other kind.
   */
  readonly reloadArtifact: () => void;
}

const TableContext = createContext<TableData | null>(null);

/** Properties of {@link TableProvider}. */
export interface TableProviderProps {
  /** Where the table is. */
  readonly place: TableRef;
  /** The table as the server answered it. */
  readonly loaded: TableOut;
  /** The artifact as the server answered it, for an artifact; else null. */
  readonly artifact: ArtifactOut | null;
  /** The tabs. */
  readonly children: ReactNode;
}

/**
 * Provides the table to the tabs and reads what the person may do with it. A table the server
 * answers after a change replaces the one read, until the page reads the table again; so does an
 * artifact read again with `reloadArtifact`, the last one asked for when several are open.
 *
 * @param props - The table and the tabs.
 * @param props.place - Where the table is.
 * @param props.loaded - The table as the server answered it.
 * @param props.artifact - The artifact as the server answered it, or null.
 * @param props.children - The tabs.
 * @returns The provider.
 */
export function TableProvider({
  place,
  loaded,
  artifact: readArtifact,
  children,
}: TableProviderProps): JSX.Element {
  const me = useMe();
  const { environment, namespace, name } = place;
  const load = useCallback(
    () => myPermissions(me.name, { environment, namespace, table: name }),
    [me.name, environment, namespace, name],
  );
  const permissions = useResource(load);
  const [changed, setChanged] = useState<{ base: TableOut; next: TableOut } | null>(null);
  const replaceTable = useCallback(
    (next: TableOut) => {
      setChanged({ base: loaded, next });
    },
    [loaded],
  );
  const table = changed !== null && changed.base === loaded ? changed.next : loaded;
  const [reread, setReread] = useState<{ base: ArtifactOut; next: ArtifactOut } | null>(null);
  const lastRead = useRef(0);
  const reloadArtifact = useCallback(() => {
    if (readArtifact === null) {
      return;
    }
    lastRead.current += 1;
    const read = lastRead.current;
    getArtifact({ environment, namespace, name }).then(
      (next) => {
        // Two reads may answer out of order; only the last one asked for is shown.
        if (read === lastRead.current) {
          setReread({ base: readArtifact, next });
        }
      },
      // The page shows the artifact it has; the change that asked for this read already said what happened.
      () => undefined,
    );
  }, [readArtifact, environment, namespace, name]);
  const artifact = reread !== null && reread.base === readArtifact ? reread.next : readArtifact;
  const mayDrop =
    permissions.data?.permissions.some((one) => one.action === "DROP_TABLE" && one.allowed) ??
    false;
  const value = useMemo<TableData>(
    () => ({
      ref: place,
      table,
      replaceTable,
      permissions,
      mayEdit: table.owner?.name === me.name || mayDrop,
      artifact,
      reloadArtifact,
    }),
    [place, table, replaceTable, permissions, me.name, mayDrop, artifact, reloadArtifact],
  );
  return <TableContext value={value}>{children}</TableContext>;
}

/**
 * Reads the table the page shows.
 *
 * @returns The table, the person's permissions on it and whether they may edit it.
 * @throws {Error} outside a table's page.
 */
export function useTableData(): TableData {
  const data = useContext(TableContext);
  if (data === null) {
    throw new Error("useTableData is used outside a table's page.");
  }
  return data;
}
