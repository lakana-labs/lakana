/**
 * The environment the Jobs list was last filtered by, kept in this browser.
 *
 * @packageDocumentation
 */
import { browserStorage } from "../preferences/preferences";
import { readRemembered, writeRemembered } from "../shell/remembered";

/** The storage key of the list's environment. */
const FILTER_KEY = "lakana.jobs.environment";

/**
 * What the key holds for all environments, a choice of its own that sticks across the tabs. No
 * environment name holds a `.` (the access file reads it as the start of a namespace, see
 * `environment_name_problem` in lakana-core), so an environment named `all` stays itself.
 */
const ALL = ".";

/**
 * Reads the environment the Jobs list was last filtered by.
 *
 * @param storage - Where it is kept; the browser's storage by default.
 * @returns The environment's name, or null for all environments (chosen, or nothing kept).
 */
export function readJobsFilter(storage: Storage | null = browserStorage()): string | null {
  const kept = readRemembered(FILTER_KEY, storage);
  return kept === ALL ? null : kept;
}

/**
 * Keeps the environment the Jobs list is filtered by.
 *
 * @param environment - The environment's name, or null for all environments, kept as `.`.
 * @param storage - Where to keep it; the browser's storage by default.
 */
export function writeJobsFilter(
  environment: string | null,
  storage: Storage | null = browserStorage(),
): void {
  writeRemembered(FILTER_KEY, environment ?? ALL, storage);
}
