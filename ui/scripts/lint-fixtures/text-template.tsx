/**
 * A violation for `lint-ban.test.ts`: a template literal with words as a JSX child. The lint run
 * ignores this directory; the test lints it on purpose.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";

/**
 * Renders a count in English only.
 *
 * @param props - The count.
 * @param props.count - How many rows.
 * @returns The paragraph.
 */
export function Count({ count }: { readonly count: number }): JSX.Element {
  return <p>{`${String(count)} rows`}</p>;
}
