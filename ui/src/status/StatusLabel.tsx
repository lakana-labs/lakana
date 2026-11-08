/**
 * A status glyph with its word beside it.
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
}

/**
 * Shows the glyph and the translated word of a state.
 *
 * @param props - The state.
 * @param props.kind - The state to show.
 * @returns The glyph and the word, side by side.
 */
export function StatusLabel({ kind }: StatusLabelProps): JSX.Element {
  const { t } = useTranslation();
  return (
    <span className="status-label">
      <StatusGlyph kind={kind} />
      <span>{t(`status.${kind}`)}</span>
    </span>
  );
}
