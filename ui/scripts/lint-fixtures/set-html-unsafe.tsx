/**
 * A violation for `lint-ban.test.ts`: raw HTML through `setHTMLUnsafe`. The lint run ignores this directory; the test lints
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
  element.setHTMLUnsafe(html);
}
