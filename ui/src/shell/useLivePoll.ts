/**
 * Reads something now and again at a calm interval, only while the page is visible and the
 * caller wants it, and waits longer after a failed read.
 *
 * @packageDocumentation
 */
import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { ApiError } from "../api";
import { problemText } from "../sign-in/messages";
import type { Resource } from "./useResource";

/** The longest wait between two reads after failures, in milliseconds. */
export const POLL_BACKOFF_MAX_MS = 60_000;

/** How many failed reads in a row a screen shows the server's sentence for, with Try again. */
export const POLL_FAILURES_SHOWN = 3;

/** A polled answer: the {@link Resource} plus how well the reads are going. */
export interface LivePoll<T> extends Resource<T> {
  /** True while the data shown is older than the latest read, because that read failed. */
  readonly stale: boolean;
  /** How many reads failed in a row; 0 after a success. */
  readonly failures: number;
}

interface State<T> {
  data: T | undefined;
  error: string | null;
  failure: ApiError | null;
  failures: number;
}

/**
 * Reads with `load` now and every `everyMs` after, as one chain of timeouts so that a slow answer
 * never overlaps the next read. No timer runs in a hidden tab: when the tab shows again it reads
 * at once and starts over. While `active` is false nothing is read. After a failed read the wait
 * doubles from `everyMs` up to {@link POLL_BACKOFF_MAX_MS}; the last data stays and `stale` is
 * true. The screen decides what to show from `failures` (see {@link POLL_FAILURES_SHOWN}) and
 * `error`. `load` must be a background request (`{ background: true }`), so the polls never keep a
 * session alive.
 *
 * @template T - What is read.
 * @param load - Reads it; keep it stable (a module function or `useCallback`).
 * @param options - How to read.
 * @param options.everyMs - The calm interval in milliseconds.
 * @param options.active - False stops the reads; true reads again at once.
 * @returns The answer, the error, the typed failure, `reload`, `stale` and `failures`.
 */
export function useLivePoll<T>(
  load: () => Promise<T>,
  options: { readonly everyMs: number; readonly active: boolean },
): LivePoll<T> {
  const { everyMs, active } = options;
  const { t } = useTranslation();
  const [state, setState] = useState<State<T>>({
    data: undefined,
    error: null,
    failure: null,
    failures: 0,
  });
  const [round, setRound] = useState(0);
  useEffect(() => {
    if (!active) {
      return undefined;
    }
    let latest = true;
    let reading = false;
    let failed = 0;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const visible = (): boolean => document.visibilityState === "visible";
    const arm = (): void => {
      const wait = Math.min(everyMs * 2 ** failed, POLL_BACKOFF_MAX_MS);
      timer = setTimeout(read, wait);
    };
    const schedule = (): void => {
      if (latest && visible()) {
        arm();
      }
    };
    function read(): void {
      clearTimeout(timer);
      reading = true;
      load().then(
        (data) => {
          reading = false;
          if (latest) {
            failed = 0;
            setState({ data, error: null, failure: null, failures: 0 });
            schedule();
          }
        },
        (cause: unknown) => {
          reading = false;
          if (latest) {
            failed += 1;
            setState((previous) => ({
              data: previous.data,
              error: problemText(t, cause),
              failure: cause instanceof ApiError ? cause : null,
              failures: failed,
            }));
            schedule();
          }
        },
      );
    }
    const onVisibility = (): void => {
      if (visible()) {
        if (!reading) {
          read();
        }
      } else {
        clearTimeout(timer);
      }
    };
    document.addEventListener("visibilitychange", onVisibility);
    if (visible()) {
      read();
    }
    return () => {
      latest = false;
      clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [load, everyMs, active, round, t]);
  const reload = useCallback(() => {
    setRound((previous) => previous + 1);
  }, []);
  return {
    data: state.data,
    error: state.error,
    failure: state.failure,
    reload,
    stale: state.failures > 0 && state.data !== undefined,
    failures: state.failures,
  };
}
