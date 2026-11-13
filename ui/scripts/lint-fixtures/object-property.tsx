/**
 * A violation for `lint-ban.test.ts`: raw HTML through the property of a props object. The lint
 * run ignores this directory; the test lints it on purpose.
 *
 * @packageDocumentation
 */
import { createElement, type JSX } from "react";

/**
 * Renders markup as HTML.
 *
 * @param html - The markup.
 * @returns The element.
 */
export function raw(html: string): JSX.Element {
  return createElement("div", { dangerouslySetInnerHTML: { __html: html } });
}
