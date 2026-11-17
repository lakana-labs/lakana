/**
 * Saving a file the browser has already read and checked: under the last part of its path, from
 * an object URL that is given back afterwards.
 *
 * @packageDocumentation
 */
import { INVISIBLE } from "./VisibleText";

/** The name a file is saved under when its path leaves none. */
const FALLBACK_NAME = "download";

/**
 * How long the object URL outlives the click, in milliseconds. A browser may start reading the
 * URL only after the click's task, so it is not given back at once; FileSaver.js waits the same.
 */
export const REVOKE_AFTER_MS = 40_000;

/**
 * The name to save a file of an artifact under: the last segment of its path, split at `/` and
 * at `\`, without invisible or control characters (tab and line feed included), so a path from the server cannot name a
 * directory or hide part of the name. A path that leaves no name, or only `.` or `..`, gives
 * `download`.
 *
 * @param path - The file's path inside the artifact, as the server sent it.
 * @returns The file name.
 */
export function fileName(path: string): string {
  const name = path
    .slice(Math.max(path.lastIndexOf("/"), path.lastIndexOf("\\")) + 1)
    .replace(INVISIBLE, "")
    .replace(/[\t\n]/gu, "");
  return name === "" || name === "." || name === ".." ? FALLBACK_NAME : name;
}

/**
 * Saves a file: makes an object URL for it, clicks a detached `<a download>` with it, and gives
 * the URL back once the click is done ({@link REVOKE_AFTER_MS}), so the bytes do not stay in the
 * page.
 *
 * @param blob - The file's bytes.
 * @param name - The name to save it under; see {@link fileName}.
 */
export function saveBlob(blob: Blob, name: string): void {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  link.click();
  setTimeout(() => {
    URL.revokeObjectURL(url);
  }, REVOKE_AFTER_MS);
}
