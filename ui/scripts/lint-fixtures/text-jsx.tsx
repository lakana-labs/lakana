/**
 * A violation for `lint-ban.test.ts`: words written straight into JSX. The lint run ignores this
 * directory; the test lints it on purpose.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";

/**
 * Renders a heading in English only.
 *
 * @returns The heading.
 */
export function Heading(): JSX.Element {
  return <h1>Hard-coded heading</h1>;
}
