/**
 * The choices a person last made on the Queries screen, kept in this browser: the environment
 * a query runs in, and the environment the lists of saved and recent queries are filtered by.
 *
 * @packageDocumentation
 */
import { browserStorage } from "../preferences/preferences";
import { readRemembered, writeRemembered } from "../shell/remembered";

/** The storage key of the environment queries run in. */
const ENVIRONMENT_KEY = "lakana.queries.environment";

/** The storage key of the lists' filter. */
const FILTER_KEY = "lakana.queries.filter";

/**
 * Reads the environment the person last ran a query in or chose.
 *
 * @param storage - Where it is kept; the browser's storage by default.
 * @returns The environment's name, or null.
 */
export function readEnvironment(storage: Storage | null = browserStorage()): string | null {
  return readRemembered(ENVIRONMENT_KEY, storage);
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
  writeRemembered(ENVIRONMENT_KEY, environment, storage);
}

/**
 * Reads the environment the lists of saved and recent queries are filtered by.
 *
 * @param storage - Where it is kept; the browser's storage by default.
 * @returns The environment's name, or null for all environments.
 */
export function readFilter(storage: Storage | null = browserStorage()): string | null {
  return readRemembered(FILTER_KEY, storage);
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
  writeRemembered(FILTER_KEY, environment, storage);
}
