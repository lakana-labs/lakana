/**
 * A violation for `lint-ban.test.ts`: raw HTML through the JSX attribute. The lint run ignores
 * this directory; the test lints it on purpose.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";

/**
 * Renders markup as HTML.
 *
 * @param props - The markup.
 * @param props.html - The markup.
 * @returns The element.
 */
export function Raw({ html }: { readonly html: string }): JSX.Element {
  return <div dangerouslySetInnerHTML={{ __html: html }} />;
}
