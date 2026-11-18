/**
 * Reads something for Home now and again at a calm interval, while the page is visible.
 *
 * @packageDocumentation
 */
import { type Resource } from "../shell/useResource";
import { useLivePoll } from "../shell/useLivePoll";

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
  return useLivePoll(load, { everyMs, active: true });
}
