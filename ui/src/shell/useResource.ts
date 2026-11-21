/**
 * Reads something from the server for a screen, and reads it again after a change.
 *
 * @packageDocumentation
 */
import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { ApiError } from "../api";
import { problemText } from "../sign-in/messages";

/** What a screen read, or why it could not. */
export interface Resource<T> {
  /** The latest answer; undefined until the first one arrives. */
  readonly data: T | undefined;
  /** The cause and next step when the latest read failed, else null. */
  readonly error: string | null;
  /** The server's refusal behind `error`, for a screen that words a status itself; else null. */
  readonly failure: ApiError | null;
  /** Reads it again. */
  readonly reload: () => void;
  /** Whether a read is on its way: the first, or one after `reload` or a new `load`. */
  readonly busy: boolean;
}

/**
 * Reads with `load` now, whenever `load` changes, and on `reload()`. An answer that arrives after
 * a newer read started is dropped, so a slow answer never replaces a newer one.
 *
 * @template T - What is read.
 * @param load - Reads it; keep it stable (`useCallback`) so that it is not read on every render.
 * @returns The answer, the error, the typed failure, `reload` and whether a read is on its way.
 */
export function useResource<T>(load: () => Promise<T>): Resource<T> {
  const { t } = useTranslation();
  const [round, setRound] = useState(0);
  // Which read the answer belongs to: a newer round or `load` means one is on its way.
  const [state, setState] = useState<{
    data: T | undefined;
    error: string | null;
    failure: ApiError | null;
    round: number;
    load: (() => Promise<T>) | null;
  }>({ data: undefined, error: null, failure: null, round: -1, load: null });
  useEffect(() => {
    let latest = true;
    load().then(
      (data) => {
        if (latest) {
          setState({ data, error: null, failure: null, round, load });
        }
      },
      (failure: unknown) => {
        if (latest) {
          setState((previous) => ({
            data: previous.data,
            error: problemText(t, failure),
            failure: failure instanceof ApiError ? failure : null,
            round,
            load,
          }));
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
  return {
    data: state.data,
    error: state.error,
    failure: state.failure,
    reload,
    busy: state.round !== round || state.load !== load,
  };
}
