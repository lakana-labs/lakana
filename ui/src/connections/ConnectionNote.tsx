/**
 * The line that says what the Connections screen just did: created, saved or deleted a
 * connection.
 *
 * @packageDocumentation
 */
import { type JSX, useEffect, useRef } from "react";
import { useTranslation } from "react-i18next";

import { markedText } from "../shell/VisibleText";
import "./connectionStrings";

/** What happened to a connection. */
export interface ConnectionNoteState {
  /** It was created, saved or deleted. */
  readonly kind: "created" | "saved" | "deleted";
  /** Its name. */
  readonly name: string;
}

/** Properties of {@link ConnectionNote}. */
export interface ConnectionNoteProps {
  /** What happened. */
  readonly note: ConnectionNoteState;
}

/**
 * Reads an address's state as a record, whatever it holds.
 *
 * @param state - The state a navigation carried.
 * @returns The state when it is an object, else an empty record.
 */
function record(state: unknown): Record<string, unknown> {
  return typeof state === "object" && state !== null ? (state as Record<string, unknown>) : {};
}

/**
 * The note a connection's page shows, from the state of the address it was reached by: Add
 * goes there with `{ created: true }`, Edit with `{ saved: true }`.
 *
 * @param state - The state of the address.
 * @param name - The connection's name.
 * @returns The note, or null when the person came by any other way.
 */
export function pageNote(state: unknown, name: string): ConnectionNoteState | null {
  const held = record(state);
  if (held.created === true) {
    return { kind: "created", name };
  }
  return held.saved === true ? { kind: "saved", name } : null;
}

/**
 * The note the list shows, from the state of the address it was reached by: the page of a
 * deleted connection goes there with `{ note: { kind: "deleted", name } }`.
 *
 * @param state - The state of the address.
 * @returns The note, or null when the person came by any other way.
 */
export function listNote(state: unknown): ConnectionNoteState | null {
  const note = record(record(state).note);
  return note.kind === "deleted" && typeof note.name === "string"
    ? { kind: "deleted", name: note.name }
    : null;
}

/**
 * Renders the note as a status line and puts the focus on it when it arrives, since the control
 * that caused it (Delete…, Save) is gone with the page it was on.
 *
 * @param props - The note.
 * @param props.note - What happened.
 * @returns The line.
 */
export function ConnectionNote({ note }: ConnectionNoteProps): JSX.Element {
  const { t } = useTranslation();
  const line = useRef<HTMLParagraphElement>(null);
  const { kind, name } = note;
  useEffect(() => {
    line.current?.focus();
  }, [kind, name]);
  return (
    <p className="connection-note" role="status" tabIndex={-1} ref={line}>
      {t(`connections.note.${kind}`, { name: markedText(name) })}
    </p>
  );
}
