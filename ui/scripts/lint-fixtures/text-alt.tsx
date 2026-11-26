/**
 * A violation for `lint-ban.test.ts`: an image's text written in English as a literal `alt`. The lint
 * run ignores this directory; the test lints it on purpose.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";

/**
 * Renders an element whose `alt` is not translated.
 *
 * @returns The element.
 */
export function Element(): JSX.Element {
  return <img src="/logo.svg" alt="The Lakana logo" />;
}
