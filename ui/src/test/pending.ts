/**
 * Helpers for tests of buttons that wait for the server: a request held open, and the pending
 * sign a button shows meanwhile.
 *
 * @packageDocumentation
 */
import { expect } from "vitest";

/** An answer a held route gives once it is released. */
interface HeldAnswer {
  /** The status code. */
  readonly status: number;
  /** The JSON body, if any. */
  readonly body?: unknown;
}

/** A route that does not answer until a test releases it. */
export interface HeldRoute {
  /** The route, to put in a fake server's table. */
  readonly route: () => Promise<HeldAnswer>;
  /** Answers the request, once. */
  readonly release: (answer: HeldAnswer) => void;
}

/**
 * A route whose request stays open until released.
 *
 * @returns The route and the function that answers it.
 */
export function held(): HeldRoute {
  let release: (answer: HeldAnswer) => void = () => undefined;
  const pending = new Promise<HeldAnswer>((resolve) => {
    release = resolve;
  });
  return { route: () => pending, release };
}

/**
 * Asserts that a button shows the pending sign and refuses presses, as React Aria marks it.
 *
 * @param button - The button that started the request.
 */
export function expectPending(button: HTMLElement): void {
  expect(button.querySelector(".pending-sign")).not.toBeNull();
  expect(button).toHaveAttribute("data-pending");
  expect(button).toHaveAttribute("aria-disabled", "true");
}
