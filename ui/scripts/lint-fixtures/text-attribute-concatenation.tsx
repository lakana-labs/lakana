/**
 * A violation for `lint-ban.test.ts`: an accessible name joined from English words with `+`. The
 * lint run ignores this directory; the test lints it on purpose.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";

/**
 * Renders a button whose name is not translated.
 *
 * @param props - The table.
 * @param props.name - The table's name.
 * @returns The button.
 */
export function Remove({ name }: { readonly name: string }): JSX.Element {
  return <button type="button" aria-label={"Remove " + name} />;
}
