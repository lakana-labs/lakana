/**
 * How often the Jobs screen reads again.
 *
 * @packageDocumentation
 */

/** Milliseconds between reads of a queued or running run, and of a log that is not complete. */
export const LIVE_EVERY_MS = 2_000;

/** Milliseconds between reads of a job and its list of runs. */
export const LIST_EVERY_MS = 30_000;
