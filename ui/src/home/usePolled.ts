/**
 * Reads something for Home now and again at a calm interval, while the page is visible.
 *
 * @packageDocumentation
 */
import { useEffect } from "react";

import { type Resource, useResource } from "../shell/useResource";

/**
 * Reads with `load` now and every `everyMs` after, skipping a turn while the browser tab is
 * hidden, so a Home left open in a background tab costs the server nothing. `load` must be a
 * background request (`{ background: true }`), so the polls never keep a session alive.
 *
 * @template T - What is read.
 * @param load - Reads it; keep it stable (a module function or `useCallback`).
 * @param everyMs - The interval in milliseconds.
 * @returns The answer, the error and `reload`.
 */
export function usePolled<T>(load: () => Promise<T>, everyMs: number): Resource<T> {
  const resource = useResource(load);
  const { reload } = resource;
  useEffect(() => {
    const timer = setInterval(() => {
      if (document.visibilityState === "visible") {
        reload();
      }
    }, everyMs);
    return () => {
      clearInterval(timer);
    };
  }, [reload, everyMs]);
  return resource;
}
