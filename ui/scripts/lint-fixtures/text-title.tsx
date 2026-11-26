/**
 * A violation for `lint-ban.test.ts`: a title written in English as a literal `title`. The lint
 * run ignores this directory; the test lints it on purpose.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";

/**
 * Renders an element whose `title` is not translated.
 *
 * @returns The element.
 */
export function Element(): JSX.Element {
  return <span title="Coordinated Universal Time" />;
}
