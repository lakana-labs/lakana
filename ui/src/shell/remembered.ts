/**
 * A choice a person last made, kept in this browser under a key: reading and writing it never
 * fails, so a browser that refuses storage just forgets the choice.
 *
 * @packageDocumentation
 */
import { browserStorage } from "../preferences/preferences";

/**
 * Reads a kept value.
 *
 * @param key - The storage key.
 * @param storage - Where it is kept; the browser's storage by default, null when it has none.
 * @returns The value, or null when there is none or it cannot be read.
 */
export function readRemembered(
  key: string,
  storage: Storage | null = browserStorage(),
): string | null {
  try {
    return storage?.getItem(key) ?? null;
  } catch {
    return null;
  }
}

/**
 * Keeps a value, or forgets it; a storage that refuses is ignored.
 *
 * @param key - The storage key.
 * @param value - The value; null forgets it.
 * @param storage - Where to keep it; the browser's storage by default, null when it has none.
 */
export function writeRemembered(
  key: string,
  value: string | null,
  storage: Storage | null = browserStorage(),
): void {
  try {
    if (value === null) {
      storage?.removeItem(key);
    } else {
      storage?.setItem(key, value);
    }
  } catch {
    // A browser that refuses storage just forgets the choice.
  }
}
