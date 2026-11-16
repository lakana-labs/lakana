/**
 * The line that says what a place's menu just did: a namespace created or deleted.
 *
 * @packageDocumentation
 */
import { type JSX, useEffect, useRef } from "react";
import { useTranslation } from "react-i18next";
import { markedText } from "../../shell/VisibleText";
import "./placeStrings";

/** What the location state carries after a namespace was created or deleted. */
export interface PlaceNoteState {
  /** What happened: created, deleted, or created but left out of the reader's tree. */
  readonly placeNote: "created" | "deleted" | "notShown";
  /** The dotted namespace. */
  readonly namespace: string;
  /** The environment it is in. */
  readonly environment: string;
}

/** Properties of {@link PlaceNote}. */
export interface PlaceNoteProps {
  /** What happened. */
  readonly note: PlaceNoteState;
}

/**
 * Renders the note as a status line above the result line and puts the focus on it when it
 * arrives, since the control that caused it is gone. A namespace the tree leaves out (its
 * creator may not read the namespaces and it holds no readable table) is named with the reason.
 *
 * @param props - The note.
 * @param props.note - What happened.
 * @returns The line.
 */
export function PlaceNote({ note }: PlaceNoteProps): JSX.Element {
  const { t } = useTranslation();
  const line = useRef<HTMLParagraphElement>(null);
  const { placeNote, namespace, environment } = note;
  useEffect(() => {
    line.current?.focus();
  }, [placeNote, namespace, environment]);
  return (
    <p className="catalog-result" role="status" tabIndex={-1} ref={line}>
      {t(`catalog.places.note.${placeNote}`, {
        namespace: markedText(namespace),
        environment: markedText(environment),
      })}
    </p>
  );
}
