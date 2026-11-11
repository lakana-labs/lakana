/**
 * What the interface remembers for the signed-in person on the server (the tour, Get started,
 * the update note) and the version of the server, read once per session for every screen.
 *
 * @packageDocumentation
 */
import {
  createContext,
  type JSX,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";

import { changeInterfaceState, getInfo, getInterfaceState, type InterfaceStateOut } from "../api";

/** What {@link useInterfaceState} hands back. */
export interface InterfaceStateValue {
  /** The person's state; null until it arrived, and when it could not be read. */
  readonly state: InterfaceStateOut | null;
  /**
   * Changes the fields given and keeps the state the server answers. The server refuses the
   * whole request when one field is refused, so a caller sends `update_note_seen` (a release
   * version only) apart from every other field.
   *
   * @param change - The fields to change.
   * @throws {ApiError} when the server refused; the state stays as it was.
   */
  readonly change: (change: Partial<InterfaceStateOut>) => Promise<void>;
  /** The version of the running server; null until it arrived or when it could not be read. */
  readonly version: string | null;
}

const InterfaceStateContext = createContext<InterfaceStateValue | null>(null);

/** Properties of {@link InterfaceStateProvider}. */
export interface InterfaceStateProviderProps {
  /** The signed-in person's principal name. */
  readonly name: string;
  /** The screens that read the state. */
  readonly children: ReactNode;
}

/**
 * Reads the person's interface state and the server's version once, and keeps them for the
 * screens inside. A failed read leaves them null: the screens then show nothing that depends on
 * them (no update note), never an error of their own.
 *
 * @param props - The person and the screens.
 * @param props.name - The signed-in person's principal name.
 * @param props.children - The screens that read the state.
 * @returns The provider.
 */
export function InterfaceStateProvider({
  name,
  children,
}: InterfaceStateProviderProps): JSX.Element {
  const [state, setState] = useState<InterfaceStateOut | null>(null);
  const [version, setVersion] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    getInterfaceState(name).then(
      (found) => {
        if (live) {
          setState(found);
        }
      },
      () => undefined,
    );
    getInfo().then(
      (info) => {
        if (live) {
          setVersion(info.version);
        }
      },
      () => undefined,
    );
    return () => {
      live = false;
    };
  }, [name]);
  const change = useCallback(
    async (fields: Partial<InterfaceStateOut>) => {
      setState(await changeInterfaceState(name, fields));
    },
    [name],
  );
  const value = useMemo(() => ({ state, change, version }), [state, change, version]);
  return <InterfaceStateContext value={value}>{children}</InterfaceStateContext>;
}

/**
 * Reads the person's interface state and the server's version.
 *
 * @returns The state, `change` and the version.
 * @throws {Error} outside an {@link InterfaceStateProvider}.
 */
export function useInterfaceState(): InterfaceStateValue {
  const value = useContext(InterfaceStateContext);
  if (value === null) {
    throw new Error("useInterfaceState needs an InterfaceStateProvider");
  }
  return value;
}
