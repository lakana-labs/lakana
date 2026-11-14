/**
 * A violation for `lint-ban.test.ts`: raw HTML parsed by `DOMParser.parseFromString`. The lint run ignores this directory; the test lints
 * it on purpose.
 *
 * @packageDocumentation
 */

/**
 * Parses markup as HTML.
 *
 * @param html - The markup.
 * @returns The parsed document.
 */
export function raw(html: string): Document {
  return new DOMParser().parseFromString(html, "text/html");
}
