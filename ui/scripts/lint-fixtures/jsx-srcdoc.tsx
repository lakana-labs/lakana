/**
 * A violation for `lint-ban.test.ts`: raw HTML through an iframe's `srcDoc` attribute. The lint run ignores this directory; the test lints
 * it on purpose.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";

/**
 * Renders markup as a document in a frame.
 *
 * @param props - The markup.
 * @param props.html - The markup.
 * @returns The frame.
 */
export function Raw({ html }: { readonly html: string }): JSX.Element {
  return <iframe title="raw" srcDoc={html} />;
}
