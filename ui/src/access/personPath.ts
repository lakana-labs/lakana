/**
 * The URLs of the objects of Settings' Access places.
 *
 * @packageDocumentation
 */

/**
 * The URL of a person's page.
 *
 * @param name - The principal name.
 * @returns The path.
 */
export function personPath(name: string): string {
  return `/settings/access/people/${encodeURIComponent(name)}`;
}
