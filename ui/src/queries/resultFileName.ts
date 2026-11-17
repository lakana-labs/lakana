/**
 * The name a downloaded result is saved under.
 *
 * @packageDocumentation
 */
import type { ResultFormat } from "../api/queries";
import { fileName } from "../shell/saveFile";
import { INVISIBLE } from "../shell/VisibleText";

/**
 * The date as `YYYY-MM-DD` in the person's local time.
 *
 * @param date - The moment.
 * @returns The date.
 */
function localDate(date: Date): string {
  const part = (value: number): string => String(value).padStart(2, "0");
  return `${String(date.getFullYear())}-${part(date.getMonth() + 1)}-${part(date.getDate())}`;
}

/**
 * The file name of a result: the query's name (or `query-{id}`), the day the run finished, and
 * the format's extension. The name cannot name a directory or hide part of itself: `/` and
 * `\` become `-`, runs of spaces become one `-`, invisible characters go
 * ({@link fileName}), and a name that leaves nothing gives `query-{id}`.
 *
 * @param base - The saved query's name; null for a query that is not saved.
 * @param id - The run's id.
 * @param finishedAt - When the run ended, as the server sent it; null takes `now`.
 * @param ext - The format, which is the extension.
 * @param now - The moment to take the date from when the run has no end.
 * @returns The file name, such as `Monthly-revenue-2026-11-16.csv`.
 */
export function resultFileName(
  base: string | null,
  id: string,
  finishedAt: string | null,
  ext: ResultFormat,
  now: Date = new Date(),
): string {
  const cleaned = (base ?? "")
    .replace(INVISIBLE, "")
    .replace(/[\\/]/gu, "-")
    .trim()
    .replace(/\s+/gu, "-");
  const named =
    cleaned === "" || cleaned === "." || cleaned === ".." ? `query-${id}` : fileName(cleaned);
  const date = localDate(finishedAt === null ? now : new Date(finishedAt));
  return `${named}-${date}.${ext}`;
}
