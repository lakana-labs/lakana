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
  useState,
} from "react";

import { myPermissions, type PermissionsOut, type TableOut, type TableRef } from "../../api";
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
}

const TableContext = createContext<TableData | null>(null);

/** Properties of {@link TableProvider}. */
export interface TableProviderProps {
  /** Where the table is. */
  readonly place: TableRef;
  /** The table as the server answered it. */
  readonly loaded: TableOut;
  /** The tabs. */
  readonly children: ReactNode;
}

/**
 * Provides the table to the tabs and reads what the person may do with it. A table the server
 * answers after a change replaces the one read, until the page reads the table again.
 *
 * @param props - The table and the tabs.
 * @param props.place - Where the table is.
 * @param props.loaded - The table as the server answered it.
 * @param props.children - The tabs.
 * @returns The provider.
 */
export function TableProvider({ place, loaded, children }: TableProviderProps): JSX.Element {
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
    }),
    [place, table, replaceTable, permissions, me.name, mayDrop],
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
