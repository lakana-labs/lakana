/**
 * The mark on a redirect's history entry: an address that settles where a visit opens
 * (`/settings` to Status, an old Access address to its place in Settings), not a move the person
 * made.
 *
 * @packageDocumentation
 */

/** The history state a redirect carries, alone or spread into its own. */
export const REDIRECT = { redirect: true } as const;

/**
 * Whether a history entry was made by a redirect.
 *
 * @param state - The entry's state (`location.state`).
 * @returns True when it carries {@link REDIRECT}'s mark.
 */
export function isRedirect(state: unknown): boolean {
  return (state as { redirect?: unknown } | null)?.redirect === true;
}
