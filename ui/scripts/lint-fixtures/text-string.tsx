/**
 * A violation for `lint-ban.test.ts`: a string literal as a JSX child. The lint run ignores this
 * directory; the test lints it on purpose.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";

/**
 * Renders a paragraph in English only.
 *
 * @returns The paragraph.
 */
export function Paragraph(): JSX.Element {
  return <p>{"Hard-coded sentence."}</p>;
}
