/**
 * The line that says what the Queries screen just did: started a new query, or deleted a saved
 * one and started a new query.
 *
 * @packageDocumentation
 */
import { type JSX, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import "./queryStrings";

/** What just happened: a new query was started, or a saved query was deleted. */
export type QueryNoteSaid =
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

/** A note as the history entry carries it. */
export type QueryNoteState = QueryNoteSaid & {
  /** A number of its own, so a note that says the same as the one before is still a new one. */
  readonly id: number;
};

/** Properties of {@link QueryNote}. */
export interface QueryNoteProps {
  /** What happened; null keeps the line mounted and empty. */
  readonly note: QueryNoteState | null;
}

/**
 * Renders the note as a status line that stays mounted, empty and visually hidden without a note,
 * so a screen reader announces each note as it arrives while the focus goes to the editor. A new
 * note comes a frame after the line emptied, so one that says the same as the note before (a
 * second New query) is a change and is announced again.
 *
 * @param props - The note.
 * @param props.note - What happened, or null.
 * @returns The line.
 */
export function QueryNote({ note }: QueryNoteProps): JSX.Element {
  const { t } = useTranslation();
  // The note whose frame has come; until then the line is empty.
  const [ready, setReady] = useState<QueryNoteState | null>(null);
  useEffect(() => {
    if (note === null) {
      return undefined;
    }
    const frame = requestAnimationFrame(() => {
      setReady(note);
    });
    return () => {
      cancelAnimationFrame(frame);
    };
  }, [note]);
  let text = "";
  if (note !== null && ready === note) {
    text =
      note.kind === "new" ? t("queries.new.done") : t("queries.delete.done", { name: note.name });
  }
  return (
    <p className={text === "" ? "queries-note visually-hidden" : "queries-note"} role="status">
      {text}
    </p>
  );
}
