/**
 * The line that says what the Queries screen just did: started a new query, or deleted a saved
 * one and started a new query.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";
import { useTranslation } from "react-i18next";

import "./queryStrings";

/** What just happened: a new query was started, or a saved query was deleted. */
export type QueryNoteState =
  | {
      /** A new, empty query was started. */
      readonly kind: "new";
    }
  | {
      /** A saved query was deleted, and a new, empty query started. */
      readonly kind: "deleted";
      /** The deleted query's name. */
      readonly name: string;
    };

/** Properties of {@link QueryNote}. */
export interface QueryNoteProps {
  /** What happened; null keeps the line mounted and empty. */
  readonly note: QueryNoteState | null;
}

/**
 * Renders the note as a status line that stays mounted, empty without a note, so a screen reader
 * announces each note as it arrives while the focus goes to the editor.
 *
 * @param props - The note.
 * @param props.note - What happened, or null.
 * @returns The line.
 */
export function QueryNote({ note }: QueryNoteProps): JSX.Element {
  const { t } = useTranslation();
  let text = "";
  if (note?.kind === "new") {
    text = t("queries.new.done");
  } else if (note?.kind === "deleted") {
    text = t("queries.delete.done", { name: note.name });
  }
  return (
    <p className="queries-note" role="status">
      {text}
    </p>
  );
}
