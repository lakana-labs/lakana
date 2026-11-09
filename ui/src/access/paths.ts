/**
 * The URLs of the Access area's objects.
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
  return `/access/people/${encodeURIComponent(name)}`;
}
