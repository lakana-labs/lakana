/**
 * A state as a small pill: its glyph with its word beside it.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";
import { useTranslation } from "react-i18next";

import { StatusGlyph } from "./StatusGlyph";
import type { StatusKind } from "./status";

/** Properties of {@link StatusLabel}. */
export interface StatusLabelProps {
  /** The state to show. */
  kind: StatusKind;
  /**
   * The words to name it with, such as "Deploy failed" for a failed deploy; the state's own word
   * when absent. The glyph and the tint stay the state's.
   */
  label?: string;
}

/**
 * The tint of each state's pill (`.pill-*` in `shell/screen.css`): the state's own colour where it
 * has one, a hairline where it has none to claim.
 */
export const PILL_TONES: Readonly<
  Record<StatusKind, "ok" | "bad" | "warn" | "accent" | "neutral">
> = {
  succeeded: "ok",
  failed: "bad",
  warning: "warn",
  running: "accent",
  queued: "neutral",
  "upstream-failed": "neutral",
  cancelled: "neutral",
};

/**
 * Shows the glyph and the translated word of a state, as a small pill tinted for the state. The
 * shape and the word carry the state; the tint only repeats it.
 *
 * @param props - The state and, optionally, its words.
 * @param props.kind - The state to show.
 * @param props.label - The words to name it with instead of the state's own.
 * @returns The pill.
 */
export function StatusLabel({ kind, label }: StatusLabelProps): JSX.Element {
  const { t } = useTranslation();
  return (
    <span className={`status-label pill pill-${PILL_TONES[kind]}`}>
      <StatusGlyph kind={kind} size={12} />
      <span>{label ?? t(`status.${kind}`)}</span>
    </span>
  );
}
