/**
 * The line that says what the Queries screen just did to a saved query: deleted it.
 *
 * @packageDocumentation
 */
import { type JSX, useEffect, useRef } from "react";
import { useTranslation } from "react-i18next";

import "./queryStrings";

/** What happened to a saved query. */
export interface QueryNoteState {
  /** It was deleted. */
  readonly kind: "deleted";
  /** Its name. */
  readonly name: string;
}

/** Properties of {@link QueryNote}. */
export interface QueryNoteProps {
  /** What happened. */
  readonly note: QueryNoteState;
}

/**
 * Renders the note as a status line and puts the focus on it when it arrives, since the control
 * that caused it (Delete…) is gone with the saved query.
 *
 * @param props - The note.
 * @param props.note - What happened.
 * @returns The line.
 */
export function QueryNote({ note }: QueryNoteProps): JSX.Element {
  const { t } = useTranslation();
  const line = useRef<HTMLParagraphElement>(null);
  const { kind, name } = note;
  useEffect(() => {
    line.current?.focus();
  }, [kind, name]);
  return (
    <p className="queries-note" role="status" tabIndex={-1} ref={line}>
      {t("queries.delete.done", { name })}
    </p>
  );
}
