/**
 * Reads something from the server for a screen, and reads it again after a change.
 *
 * @packageDocumentation
 */
import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { problemText } from "../sign-in/messages";

/** What a screen read, or why it could not. */
export interface Resource<T> {
  /** The latest answer; undefined until the first one arrives. */
  readonly data: T | undefined;
  /** The cause and next step when the latest read failed, else null. */
  readonly error: string | null;
  /** Reads it again. */
  readonly reload: () => void;
}

/**
 * Reads with `load` now, whenever `load` changes, and on `reload()`. An answer that arrives after
 * a newer read started is dropped, so a slow answer never replaces a newer one.
 *
 * @template T - What is read.
 * @param load - Reads it; keep it stable (`useCallback`) so that it is not read on every render.
 * @returns The answer, the error and `reload`.
 */
export function useResource<T>(load: () => Promise<T>): Resource<T> {
  const { t } = useTranslation();
  const [state, setState] = useState<{ data: T | undefined; error: string | null }>({
    data: undefined,
    error: null,
  });
  const [round, setRound] = useState(0);
  useEffect(() => {
    let latest = true;
    load().then(
      (data) => {
        if (latest) {
          setState({ data, error: null });
        }
      },
      (failure: unknown) => {
        if (latest) {
          setState((previous) => ({ data: previous.data, error: problemText(t, failure) }));
        }
      },
    );
    return () => {
      latest = false;
    };
  }, [load, round, t]);
  const reload = useCallback(() => {
    setRound((previous) => previous + 1);
  }, []);
  return { data: state.data, error: state.error, reload };
}
