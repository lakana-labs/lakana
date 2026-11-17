/**
 * The environment the Connections list was last filtered by, kept in this browser for each
 * person.
 *
 * @packageDocumentation
 */
import { browserStorage } from "../preferences/preferences";

/**
 * The storage key of a person's filter.
 *
 * @param person - The person's principal name.
 * @returns The key.
 */
function key(person: string): string {
  return `lakana.connections.${person}`;
}

/**
 * Reads the environment the person last filtered the list by.
 *
 * @param person - The person's principal name.
 * @param storage - Where it is kept; the browser's storage by default.
 * @returns The environment's name, or null for all environments or when it cannot be read.
 */
export function readFilter(
  person: string,
  storage: Storage | null = browserStorage(),
): string | null {
  try {
    return storage?.getItem(key(person)) ?? null;
  } catch {
    return null;
  }
}

/**
 * Keeps the list's filter; a storage that refuses is ignored.
 *
 * @param person - The person's principal name.
 * @param environment - The environment's name, or null for all environments.
 * @param storage - Where to keep it; the browser's storage by default.
 */
export function writeFilter(
  person: string,
  environment: string | null,
  storage: Storage | null = browserStorage(),
): void {
  try {
    if (environment === null) {
      storage?.removeItem(key(person));
    } else {
      storage?.setItem(key(person), environment);
    }
  } catch {
    // A browser that refuses storage just forgets the choice.
  }
}
