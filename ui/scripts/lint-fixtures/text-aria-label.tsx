/**
 * A violation for `lint-ban.test.ts`: a button's accessible name written in English as a literal `aria-label`. The lint
 * run ignores this directory; the test lints it on purpose.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";

/**
 * Renders an element whose `aria-label` is not translated.
 *
 * @returns The element.
 */
export function Element(): JSX.Element {
  return <button type="button" aria-label="Close the dialog" />;
}
