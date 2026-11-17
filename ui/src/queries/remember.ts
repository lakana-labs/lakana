/**
 * The choices a person last made on the Queries screen, kept in this browser: the environment
 * a query runs in, and the environment the lists of saved and recent queries are filtered by.
 *
 * @packageDocumentation
 */
import { browserStorage } from "../preferences/preferences";

/** The storage key of the environment queries run in. */
const ENVIRONMENT_KEY = "lakana.queries.environment";

/** The storage key of the lists' filter. */
const FILTER_KEY = "lakana.queries.filter";

/**
 * Reads a kept value.
 *
 * @param key - The storage key.
 * @param storage - Where it is kept; null when the browser has none.
 * @returns The value, or null when there is none or it cannot be read.
 */
function read(key: string, storage: Storage | null): string | null {
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
 * @param storage - Where to keep it; null when the browser has none.
 */
function write(key: string, value: string | null, storage: Storage | null): void {
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

/**
 * Reads the environment the person last ran a query in or chose.
 *
 * @param storage - Where it is kept; the browser's storage by default.
 * @returns The environment's name, or null.
 */
export function readEnvironment(storage: Storage | null = browserStorage()): string | null {
  return read(ENVIRONMENT_KEY, storage);
}

/**
 * Keeps the environment the person chose or ran a query in.
 *
 * @param environment - The environment's name.
 * @param storage - Where to keep it; the browser's storage by default.
 */
export function writeEnvironment(
  environment: string,
  storage: Storage | null = browserStorage(),
): void {
  write(ENVIRONMENT_KEY, environment, storage);
}

/**
 * Reads the environment the lists of saved and recent queries are filtered by.
 *
 * @param storage - Where it is kept; the browser's storage by default.
 * @returns The environment's name, or null for all environments.
 */
export function readFilter(storage: Storage | null = browserStorage()): string | null {
  return read(FILTER_KEY, storage);
}

/**
 * Keeps the lists' filter.
 *
 * @param environment - The environment's name, or null for all environments.
 * @param storage - Where to keep it; the browser's storage by default.
 */
export function writeFilter(
  environment: string | null,
  storage: Storage | null = browserStorage(),
): void {
  write(FILTER_KEY, environment, storage);
}
