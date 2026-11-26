/**
 * A violation for `lint-ban.test.ts`: English words joined to a value with `+` as a JSX child. The
 * lint run ignores this directory; the test lints it on purpose.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";

/**
 * Renders a count in English only.
 *
 * @param props - The name.
 * @param props.name - The table's name.
 * @returns The paragraph.
 */
export function Rows({ name }: { readonly name: string }): JSX.Element {
  return <p>{"Rows of " + name}</p>;
}
