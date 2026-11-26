/**
 * What deleting a version did, in the words `lakana artifact versions delete` prints.
 *
 * @packageDocumentation
 */
import type { TFunction } from "i18next";

import type { ArtifactKeptOut, ArtifactVersionDeletedOut } from "../../api";
import { markedText } from "../../shell/VisibleText";
import { formatBytes } from "../format";
import "./artifactStrings";

/**
 * A number of files and their size, such as "2 files (300 B)".
 *
 * @param t - The translation function.
 * @param language - The interface language.
 * @param files - How many files.
 * @param bytes - Their size.
 * @returns The words.
 */
function amount(t: TFunction, language: string, files: number, bytes: number): string {
  return t("table.artifact.delete.amount", {
    files: t("table.artifact.delete.files", { count: files }),
    size: formatBytes(bytes, language),
  });
}

/**
 * Why a group of files stayed, in words that follow "Kept". A reason this interface does not
 * know reads as the clean-up's, which is true of anything the delete did not remove.
 *
 * @param t - The translation function.
 * @param language - The interface language.
 * @param kept - One group of the answer.
 * @returns The clause.
 */
function keptClause(t: TFunction, language: string, kept: ArtifactKeptOut): string {
  const words = amount(t, language, kept.files, kept.bytes);
  switch (kept.reason) {
    case "other_version":
      return kept.version === null
        ? t("table.artifact.delete.keptByAnother", { amount: words })
        : t("table.artifact.delete.keptByVersion", {
            amount: words,
            version: String(kept.version),
          });
    case "open_upload":
      return t("table.artifact.delete.keptByUpload", { amount: words });
    default:
      return t("table.artifact.delete.keptForSweep", { amount: words });
  }
}

/**
 * Says what a delete freed and, when a file stayed, what and why, as the command line's
 * `deletion_summary` does; the reasons are joined as a list in the interface language.
 *
 * @param t - The translation function.
 * @param language - The interface language.
 * @param artifact - The artifact's dotted name, as the server sent it; its invisible characters
 *   are marked.
 * @param deleted - The server's answer.
 * @returns For example "Deleted version 3 of ml.model. Freed 2 files (300 B). Kept 1 file
 *   (100 B) that version 5 also uses."
 */
export function deletionWords(
  t: TFunction,
  language: string,
  artifact: string,
  deleted: ArtifactVersionDeletedOut,
): string {
  const head = t("table.artifact.delete.deleted", {
    version: String(deleted.version),
    artifact: markedText(artifact),
    freed:
      deleted.freed_files === 0
        ? t("table.artifact.delete.noFiles")
        : amount(t, language, deleted.freed_files, deleted.freed_bytes),
  });
  if (deleted.kept.length === 0) {
    return head;
  }
  const list = new Intl.ListFormat(language, { type: "conjunction" }).format(
    deleted.kept.map((kept) => keptClause(t, language, kept)),
  );
  return `${head} ${t("table.artifact.delete.kept", { list })}`;
}
