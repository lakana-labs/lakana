/**
 * No violation, for `lint-ban.test.ts`: reading markup and writing text stay allowed. The lint
 * run ignores this directory; the test lints it on purpose.
 *
 * @packageDocumentation
 */

/**
 * Reads an element's markup, then replaces its content with text.
 *
 * @param element - The element.
 * @param text - The text.
 * @returns The markup it had.
 */
export function replaceText(element: HTMLElement, text: string): string {
  const before = element.innerHTML;
  element.textContent = text;
  return before;
}
