/**
 * A violation for `lint-ban.test.ts`: raw HTML assigned to `outerHTML`. The lint run ignores this
 * directory; the test lints it on purpose.
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
  element.outerHTML = html;
}
