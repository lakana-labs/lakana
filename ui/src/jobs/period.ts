/**
 * The periods a list of runs can cover, as a job's runs and the Runs tab offer them.
 *
 * @packageDocumentation
 */

/** The periods a list of runs can cover, and their length in days; `all` has none. */
const PERIODS = { "24h": 1, "7d": 7, "30d": 30, all: null } as const;

/** A choice of the Period menu. */
export type Period = keyof typeof PERIODS;

/** The period shown when the address names none. */
export const DEFAULT_PERIOD: Period = "30d";

/** Every period, in the menu's order. */
export const PERIOD_CHOICES = Object.keys(PERIODS) as readonly Period[];

const DAY_MS = 86_400_000;

/**
 * Reads a period from the address; anything it does not know is the default.
 *
 * @param requested - The address's `period`, or null.
 * @returns The period.
 */
export function readPeriod(requested: string | null): Period {
  return PERIOD_CHOICES.find((choice) => choice === requested) ?? DEFAULT_PERIOD;
}

/**
 * The time a period starts, counted back from now.
 *
 * @param period - The period.
 * @returns The time (ISO 8601, UTC); null for all time.
 */
export function sinceOf(period: Period): string | null {
  const days = PERIODS[period];
  return days === null ? null : new Date(Date.now() - days * DAY_MS).toISOString();
}
