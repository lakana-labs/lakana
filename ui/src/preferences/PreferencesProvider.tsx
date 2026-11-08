/**
 * Holds the personal preferences for the whole interface and applies them to the page.
 *
 * @packageDocumentation
 */
import {
  createContext,
  type JSX,
  type ReactNode,
  useCallback,
  useContext,
  useLayoutEffect,
  useMemo,
  useState,
} from "react";

import {
  applyPreferences,
  browserStorage,
  type Preferences,
  readPreferences,
  writePreferences,
} from "./preferences";

type PreferencesValue = readonly [Preferences, (next: Partial<Preferences>) => void];

const PreferencesContext = createContext<PreferencesValue | null>(null);

/** Properties of {@link PreferencesProvider}. */
export interface PreferencesProviderProps {
  /** Where the preferences are kept; the browser's local storage by default. */
  readonly storage?: Storage | null;
  /** The interface. */
  readonly children: ReactNode;
}

/**
 * Reads the stored preferences once, applies them to `<html>` and stores every change.
 *
 * @param props - The storage and the interface.
 * @param props.storage - Where the preferences are kept.
 * @param props.children - The interface.
 * @returns The interface with the preferences available to {@link usePreferences}.
 */
export function PreferencesProvider({
  storage = browserStorage(),
  children,
}: PreferencesProviderProps): JSX.Element {
  const [preferences, setPreferences] = useState(() => readPreferences(storage));

  useLayoutEffect(() => {
    applyPreferences(document.documentElement, preferences);
  }, [preferences]);

  const update = useCallback(
    (next: Partial<Preferences>) => {
      setPreferences((current) => {
        const merged = { ...current, ...next };
        writePreferences(storage, merged);
        return merged;
      });
    },
    [storage],
  );

  const value = useMemo(() => [preferences, update] as const, [preferences, update]);
  return <PreferencesContext value={value}>{children}</PreferencesContext>;
}

/**
 * The current preferences and a function that changes some of them.
 *
 * @returns The preferences and the update function.
 */
export function usePreferences(): PreferencesValue {
  const value = useContext(PreferencesContext);
  if (value === null) {
    throw new Error("usePreferences needs a PreferencesProvider above it");
  }
  return value;
}
