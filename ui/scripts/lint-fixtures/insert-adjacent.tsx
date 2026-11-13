/**
 * A violation for `lint-ban.test.ts`: raw HTML through `insertAdjacentHTML`. The lint run ignores
 * this directory; the test lints it on purpose.
 *
 * @packageDocumentation
 */

/**
 * Appends markup to an element as HTML.
 *
 * @param element - The element.
 * @param html - The markup.
 */
export function raw(element: HTMLElement, html: string): void {
  element.insertAdjacentHTML("beforeend", html);
}
