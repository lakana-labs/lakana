/**
 * The line that says what a place's menu just did, a namespace created or deleted, or what an
 * artifact's page did: the artifact dropped.
 *
 * @packageDocumentation
 */
import { type JSX, useEffect, useRef } from "react";
import { useTranslation } from "react-i18next";
import { markedText } from "../../shell/VisibleText";
import "./placeStrings";

/** What the location state carries after a namespace was created or deleted, or an artifact dropped. */
export interface PlaceNoteState {
  /**
   * What happened: created, deleted, created but left out of the reader's tree, or an artifact
   * of the namespace dropped.
   */
  readonly placeNote: "created" | "deleted" | "notShown" | "artifactDropped";
  /** The dotted namespace. */
  readonly namespace: string;
  /** The environment it is in. */
  readonly environment: string;
  /** The dropped artifact, dotted, for `artifactDropped`. */
  readonly artifact?: string;
}

/** Properties of {@link PlaceNote}. */
export interface PlaceNoteProps {
  /** What happened. */
  readonly note: PlaceNoteState;
}

/**
 * Renders the note as a status line above the result line and puts the focus on it when it
 * arrives, since the control that caused it is gone; also after the shell's move to the heading
 * of a new page. A namespace the tree leaves out (its creator may not read the namespaces and it
 * holds no readable table) is named with the reason.
 *
 * @param props - The note.
 * @param props.note - What happened.
 * @returns The line.
 */
export function PlaceNote({ note }: PlaceNoteProps): JSX.Element {
  const { t } = useTranslation();
  const line = useRef<HTMLParagraphElement>(null);
  const { placeNote, namespace, environment, artifact } = note;
  useEffect(() => {
    line.current?.focus();
    // After a move to another page the shell gives the new heading the focus in an effect that
    // runs after this one: take it back on the next frame, unless something else has it by then.
    const frame = requestAnimationFrame(() => {
      const focused = document.activeElement;
      if (focused === document.body || focused?.matches("h1.page-title") === true) {
        line.current?.focus();
      }
    });
    return () => {
      cancelAnimationFrame(frame);
    };
  }, [placeNote, namespace, environment, artifact]);
  return (
    <p className="catalog-result" role="status" tabIndex={-1} ref={line}>
      {t(`catalog.places.note.${placeNote}`, {
        namespace: markedText(namespace),
        environment: markedText(environment),
        artifact: markedText(artifact ?? ""),
      })}
    </p>
  );
}
