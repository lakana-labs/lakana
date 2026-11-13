/**
 * A violation for `lint-ban.test.ts`: raw HTML parsed by `createContextualFragment`. The lint run ignores this directory; the test lints
 * it on purpose.
 *
 * @packageDocumentation
 */

/**
 * Parses markup as HTML.
 *
 * @param html - The markup.
 * @returns The parsed fragment.
 */
export function raw(html: string): DocumentFragment {
  return document.createRange().createContextualFragment(html);
}
