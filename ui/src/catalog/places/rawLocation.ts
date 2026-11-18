/**
 * The location a choice in Browse… fills in: a folder of `raw/` and a file pattern, the same
 * shape `lakana table create-external --location` takes.
 *
 * @packageDocumentation
 */
import { formatFromLocation } from "./externalFormat";

/**
 * Writes text so that a file pattern matches it literally: `*`, `?` and `[` each in brackets.
 *
 * @param text - A file name or an ending.
 * @returns The pattern.
 */
function literal(text: string): string {
  return text.replace(/[*?[]/g, (character) => `[${character}]`);
}

/**
 * The ending of a file name: from its last dot, as written. A name without a dot, or whose only
 * dot starts it (`.hidden`), has none.
 *
 * @param name - The file's name.
 * @returns The ending, such as `.csv`, or the empty string.
 */
function ending(name: string): string {
  const dot = name.lastIndexOf(".");
  return dot > 0 ? name.slice(dot) : "";
}

/**
 * The location of a chosen file: its folder and every file there with its ending, or the file
 * itself when it has none.
 *
 * @param folder - The folder the file lies in, such as `raw/orders`.
 * @param name - The file's name.
 * @returns The location, such as `raw/orders/*.csv`.
 */
export function locationForFile(folder: string, name: string): string {
  const end = ending(name);
  return end === "" ? `${folder}/${literal(name)}` : `${folder}/*${literal(end)}`;
}

/**
 * The location of a chosen folder: the files with the ending most of its files have. On a tie,
 * an ending with a format (`.csv`, `.json`, `.jsonl`, `.parquet`) wins, then the first in
 * alphabetical order. Files without an ending count together, and give `*`.
 *
 * @param folder - The folder, such as `raw/feeds`.
 * @param files - The names of the files directly in it that the panel shows.
 * @returns The location, or null when the folder shows no file.
 */
export function locationForFolder(folder: string, files: readonly string[]): string | null {
  const counts = new Map<string, number>();
  for (const name of files) {
    const end = ending(name);
    counts.set(end, (counts.get(end) ?? 0) + 1);
  }
  const [best] = [...counts.entries()].sort(
    ([a, countA], [b, countB]) =>
      countB - countA ||
      Number(formatFromLocation(b) !== null) - Number(formatFromLocation(a) !== null) ||
      (a < b ? -1 : 1),
  );
  return best === undefined ? null : `${folder}/*${literal(best[0])}`;
}
