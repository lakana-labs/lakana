/**
 * A violation for `lint-ban.test.ts`: raw HTML through an `innerHTML` property copied onto an element. The lint run ignores this directory; the test lints
 * it on purpose.
 *
 * @packageDocumentation
 */

/**
 * Puts markup into an element as HTML.
 *
 * @param element - The element.
 * @param html - The markup.
 */
export function raw(element: HTMLElement, html: string): void {
  Object.assign(element, { innerHTML: html });
}
