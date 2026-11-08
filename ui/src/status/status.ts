/**
 * The seven states a run, a task or a check can be in.
 *
 * @packageDocumentation
 */

/** One of the seven states that have a glyph and a word. */
export type StatusKind =
  "succeeded" | "failed" | "warning" | "running" | "queued" | "upstream-failed" | "cancelled";

/** Every {@link StatusKind}, in the order the interface lists them. */
export const STATUS_KINDS: readonly StatusKind[] = [
  "succeeded",
  "failed",
  "warning",
  "running",
  "queued",
  "upstream-failed",
  "cancelled",
];
