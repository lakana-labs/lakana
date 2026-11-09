/**
 * Puts focus on a stable place after a change removed the control that had it.
 *
 * @packageDocumentation
 */
import { type RefObject, useCallback, useEffect, useRef } from "react";

/** The place to focus and the way to ask for it. */
export interface FocusAfter<T extends HTMLElement> {
  /** The element that receives focus; give it `tabIndex={-1}` unless it is focusable already. */
  readonly target: RefObject<T | null>;
  /** Asks for focus on `target` once `change` next changes. */
  readonly arm: () => void;
}

/**
 * Focuses `target` after the next change of `change` (for example the list read again after a
 * row was withdrawn), so focus never falls back to the page when the pressed button goes away.
 *
 * @template T - The element type of the target.
 * @param change - What changes once the removal shows on screen.
 * @returns The target and `arm`.
 */
export function useFocusAfter<T extends HTMLElement>(change: unknown): FocusAfter<T> {
  const target = useRef<T>(null);
  const armed = useRef(false);
  useEffect(() => {
    if (!armed.current) return undefined;
    armed.current = false;
    // A closing React Aria dialog hands focus back to its trigger in the next animation frame;
    // moving it in a frame too, scheduled after the dialog's, keeps the move from being undone.
    const frame = requestAnimationFrame(() => target.current?.focus());
    return () => {
      cancelAnimationFrame(frame);
    };
  }, [change]);
  const arm = useCallback(() => {
    armed.current = true;
  }, []);
  return { target, arm };
}
