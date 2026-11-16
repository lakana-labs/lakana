/**
 * The format an external table's location suggests, by the end of its file pattern.
 *
 * @packageDocumentation
 */
import type { ExternalFormat } from "../../api";

/** Each ending a file pattern may have, and the format it means. */
const ENDINGS: readonly (readonly [string, ExternalFormat])[] = [
  [".csv", "csv"],
  [".json", "json"],
  [".jsonl", "json"],
  [".parquet", "parquet"],
];

/**
 * Reads the format from the end of a location, ignoring case: `.csv`, `.json` or `.jsonl`, and
 * `.parquet`. The server judges the files; this only picks the format the person most likely
 * means until they pick one.
 *
 * @param location - The location, such as `raw/orders/*.csv`.
 * @returns The format, or null when the pattern ends otherwise.
 */
export function formatFromLocation(location: string): ExternalFormat | null {
  const end = location.toLowerCase();
  return ENDINGS.find(([ending]) => end.endsWith(ending))?.[1] ?? null;
}
