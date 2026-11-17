/**
 * Whether a while has passed since a component appeared: the words that follow a wait.
 *
 * @packageDocumentation
 */
import { useEffect, useState } from "react";

/** How long a wait goes without words: a second (Still Water). */
export const WORDS_AFTER_MS = 1_000;

/**
 * Turns true once `ms` have passed since the calling component was mounted.
 *
 * @param ms - How long to wait; {@link WORDS_AFTER_MS} by default.
 * @returns Whether the time has passed.
 */
export function useAfter(ms: number = WORDS_AFTER_MS): boolean {
  const [passed, setPassed] = useState(false);
  useEffect(() => {
    const timer = setTimeout(() => {
      setPassed(true);
    }, ms);
    return () => {
      clearTimeout(timer);
    };
  }, [ms]);
  return passed;
}
