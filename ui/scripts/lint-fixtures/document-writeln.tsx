/**
 * A violation for `lint-ban.test.ts`: raw HTML through `document.writeln`. The lint run ignores this directory; the test lints
 * it on purpose.
 *
 * @packageDocumentation
 */

/**
 * Writes markup into the document as HTML.
 *
 * @param html - The markup.
 */
export function raw(html: string): void {
  // The deprecation is reported by its own rule; this fixture is about the ban.
  // eslint-disable-next-line @typescript-eslint/no-deprecated
  document.writeln(html);
}
