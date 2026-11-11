/**
 * Starts the first-sign-in tour: once by itself for a person who never saw it, and again
 * whenever they ask (Help, the command palette). The tour's own code is loaded only then.
 *
 * @packageDocumentation
 */
import {
  createContext,
  type JSX,
  lazy,
  type ReactNode,
  Suspense,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import type { ShippedArea } from "../shell/areas";
import { useInterfaceState } from "../shell/InterfaceState";
import { useMe } from "../shell/me";

/** What {@link useTour} hands back. */
export interface TourValue {
  /**
   * Starts the tour at its first step.
   *
   * @param returnTo - Where focus goes when the tour ends; the element that has focus now by
   *   default, else the page's heading.
   */
  readonly start: (returnTo?: HTMLElement | null) => void;
  /** Whether the welcome or a step is showing. */
  readonly active: boolean;
}

const TourContext = createContext<TourValue | null>(null);

const Tour = lazy(async () => {
  const { Tour: loaded } = await import("./Tour");
  return { default: loaded };
});

/** Properties of {@link TourProvider}. */
export interface TourProviderProps {
  /** The areas the person sees, in sidebar order. */
  readonly areas: readonly ShippedArea[];
  /** The shell inside. */
  readonly children: ReactNode;
}

/**
 * Moves focus back after the tour: to where it was when the tour started while that is still
 * there, else to the page's heading. After the welcome a person never asked for, focus stays
 * where the dialog leaves it.
 *
 * @param target - Where focus was, or null after the welcome.
 */
function returnFocus(target: HTMLElement | null): void {
  if (target === null) {
    return;
  }
  if (target.isConnected) {
    target.focus();
    return;
  }
  document.querySelector<HTMLElement>("main h1")?.focus();
}

/**
 * Provides {@link useTour} and shows the tour while it runs. The welcome opens once, when the
 * person's state says they never saw the tour, and only once they have access (until then
 * Home asks them to ask an administrator, and the places would be closed to them); closing it
 * or the tour in any way records that.
 *
 * @param props - The areas and the shell.
 * @param props.areas - The areas the person sees.
 * @param props.children - The shell inside.
 * @returns The shell, and the tour while it runs.
 */
export function TourProvider({ areas, children }: TourProviderProps): JSX.Element {
  const { state } = useInterfaceState();
  const { noAccessYet } = useMe();
  const [mode, setMode] = useState<"welcome" | "steps" | null>(null);
  const welcomed = useRef(false);
  const opener = useRef<HTMLElement | null>(null);
  const [ended, setEnded] = useState(0);
  const [run, setRun] = useState(0);

  useEffect(() => {
    if (state?.tour_seen === false && !noAccessYet && !welcomed.current) {
      welcomed.current = true;
      opener.current = null;
      setMode("welcome");
    }
  }, [state, noAccessYet]);

  useEffect(() => {
    if (ended > 0) {
      returnFocus(opener.current);
    }
  }, [ended]);

  const start = useCallback((returnTo?: HTMLElement | null) => {
    welcomed.current = true;
    opener.current = returnTo ?? document.querySelector<HTMLElement>(":focus");
    setMode("steps");
    setRun((count) => count + 1);
  }, []);
  const close = useCallback(() => {
    setMode(null);
    setEnded((count) => count + 1);
  }, []);
  const value = useMemo(() => ({ start, active: mode !== null }), [start, mode]);

  return (
    <TourContext value={value}>
      {children}
      {mode !== null && (
        <Suspense fallback={null}>
          <Tour key={run} areas={areas} welcome={mode === "welcome"} onClose={close} />
        </Suspense>
      )}
    </TourContext>
  );
}

/**
 * Starts the tour, or says whether it runs.
 *
 * @returns `start` and `active`.
 * @throws {Error} outside a {@link TourProvider}.
 */
export function useTour(): TourValue {
  const value = useContext(TourContext);
  if (value === null) {
    throw new Error("useTour needs a TourProvider");
  }
  return value;
}
