/**
 * A violation for `lint-ban.test.ts`: English words in the `heading` prop of one of the
 * interface's own components, which renders it. The lint run ignores this directory; the test
 * lints it on purpose.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";

/**
 * An empty state, as the interface's own components take one.
 *
 * @param props - The words.
 * @param props.heading - The words shown.
 * @returns The paragraph.
 */
function Empty({ heading }: { readonly heading: string }): JSX.Element {
  return <p>{heading}</p>;
}

/**
 * Renders an empty list in English only.
 *
 * @returns The empty state.
 */
export function NoTables(): JSX.Element {
  return <Empty heading="No tables yet" />;
}
