/**
 * A violation for `lint-ban.test.ts`: raw HTML through a quoted `outerHTML` property key. The lint run ignores this directory; the test lints
 * it on purpose.
 *
 * @packageDocumentation
 */

/**
 * Replaces an element with markup as HTML.
 *
 * @param element - The element.
 * @param html - The markup.
 */
export function raw(element: HTMLElement, html: string): void {
  // prettier-ignore
  Object.assign(element, { "outerHTML": html });
}
