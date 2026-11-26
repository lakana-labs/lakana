/**
 * A violation for `lint-ban.test.ts`: a placeholder written in English as a literal `placeholder`. The lint
 * run ignores this directory; the test lints it on purpose.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";

/**
 * Renders an element whose `placeholder` is not translated.
 *
 * @param props - The label.
 * @param props.label - The field's name.
 * @returns The element.
 */
export function Element({ label }: { readonly label: string }): JSX.Element {
  return <input aria-label={label} placeholder="Search tables" />;
}
