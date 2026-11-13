/**
 * A table's health in one word: the worst thing its checks found, with a glyph, and how many
 * others there are.
 *
 * @packageDocumentation
 */
import type { JSX } from "react";
import { useTranslation } from "react-i18next";

import type { HealthSummaryOut } from "../api";
import { StatusGlyph } from "../status/StatusGlyph";
import type { StatusKind } from "../status/status";

const GLYPHS: Record<"failing" | "warning" | "healthy", StatusKind> = {
  failing: "failed",
  warning: "warning",
  healthy: "succeeded",
};

/** Properties of {@link HealthWord}. */
export interface HealthWordProps {
  /** The verdict from the list. */
  readonly summary: HealthSummaryOut;
}

/**
 * Renders the glyph and the word. A table not judged has a muted dash and no glyph. The others
 * are counted visibly (`+2`) and in words for a screen reader, who also hears that they are
 * listed on the table page; a word the interface does not know is the server's own.
 *
 * @param props - The verdict.
 * @param props.summary - The verdict from the list.
 * @returns The word.
 */
export function HealthWord({ summary }: HealthWordProps): JSX.Element {
  const { t } = useTranslation();
  const word = t(`catalog.health.${summary.item === "" ? summary.state : summary.item}`, {
    defaultValue: summary.text,
  });
  const described =
    summary.others > 0 ? t("catalog.healthMore", { word, count: summary.others }) : null;
  return (
    <span className="health-word">
      {summary.state === "not_judged" ? (
        <span className="health-none" aria-hidden="true">
          –
        </span>
      ) : (
        <StatusGlyph kind={GLYPHS[summary.state]} />
      )}
      {described === null ? (
        <span>{word}</span>
      ) : (
        <>
          <span className="health-text" aria-hidden="true" title={described}>
            {word}
            <span className="health-more">+{summary.others}</span>
          </span>
          <span className="visually-hidden">{described}</span>
        </>
      )}
    </span>
  );
}
